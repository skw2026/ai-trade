#include "evolution/self_evolution_controller.h"

#include <algorithm>
#include <cmath>

namespace ai_trade {

namespace {

constexpr double kWeightEpsilon = 1e-9;

}  // namespace

SelfEvolutionController::SelfEvolutionController(SelfEvolutionConfig config)
    : config_(config) {}

std::size_t SelfEvolutionController::BucketIndex(RegimeBucket bucket) {
  switch (bucket) {
    case RegimeBucket::kTrend:
      return 0;
    case RegimeBucket::kRange:
      return 1;
    case RegimeBucket::kExtreme:
      return 2;
  }
  return 1;
}

RegimeBucket SelfEvolutionController::BucketFromIndex(std::size_t index) {
  switch (index) {
    case 0:
      return RegimeBucket::kTrend;
    case 1:
      return RegimeBucket::kRange;
    case 2:
      return RegimeBucket::kExtreme;
    default:
      return RegimeBucket::kRange;
  }
}

SelfEvolutionController::BucketRuntime& SelfEvolutionController::RuntimeFor(
    RegimeBucket bucket) {
  return bucket_runtime_[BucketIndex(bucket)];
}

const SelfEvolutionController::BucketRuntime&
SelfEvolutionController::RuntimeFor(RegimeBucket bucket) const {
  return bucket_runtime_[BucketIndex(bucket)];
}

bool SelfEvolutionController::Initialize(
    std::int64_t current_tick,
    double initial_equity_usd,
    const std::pair<double, double>& initial_weights,
    std::string* out_error) {
  if (!config_.enabled) {
    initialized_ = false;
    return true;
  }

  if (initial_equity_usd <= 0.0) {
    if (out_error != nullptr) {
      *out_error = "初始化权益必须大于0";
    }
    return false;
  }

  if (!ValidateWeights(initial_weights.first, initial_weights.second, out_error)) {
    return false;
  }

  for (auto& runtime : bucket_runtime_) {
    runtime.current_trend_weight = initial_weights.first;
    runtime.current_defensive_weight = initial_weights.second;
    runtime.rollback_anchor_trend_weight = runtime.current_trend_weight;
    runtime.rollback_anchor_defensive_weight = runtime.current_defensive_weight;
    runtime.degrade_windows.clear();
  }

  last_observed_equity_usd_ = initial_equity_usd;
  last_observed_notional_usd_ = 0.0;
  has_last_observed_notional_ = false;
  ResetWindowAttribution();
  next_eval_tick_ = current_tick + EffectiveUpdateIntervalTicks();
  cooldown_until_tick_ = current_tick;
  initialized_ = true;
  return true;
}

EvolutionWeights SelfEvolutionController::current_weights(
    RegimeBucket bucket) const {
  const BucketRuntime& runtime = RuntimeFor(bucket);
  return {runtime.current_trend_weight, runtime.current_defensive_weight};
}

EvolutionWeights SelfEvolutionController::rollback_anchor_weights(
    RegimeBucket bucket) const {
  const BucketRuntime& runtime = RuntimeFor(bucket);
  return {runtime.rollback_anchor_trend_weight,
          runtime.rollback_anchor_defensive_weight};
}

int SelfEvolutionController::degrade_window_count(RegimeBucket bucket) const {
  return static_cast<int>(RuntimeFor(bucket).degrade_windows.size());
}

std::optional<SelfEvolutionAction> SelfEvolutionController::OnTick(
    std::int64_t current_tick,
    double equity_usd,
    RegimeBucket regime_bucket,
    double drawdown_pct,
    double account_notional_usd) {
  if (!config_.enabled || !initialized_) {
    return std::nullopt;
  }

  // 先按当前 bucket 归因逐 tick 权益变化，避免整窗收益被误记到评估时所在 bucket。
  const std::size_t active_index = BucketIndex(regime_bucket);
  const double delta_equity = equity_usd - last_observed_equity_usd_;
  bucket_window_pnl_usd_[active_index] += delta_equity;
  bucket_window_ticks_[active_index] += 1;
  bucket_window_max_drawdown_pct_[active_index] =
      std::max(bucket_window_max_drawdown_pct_[active_index],
               std::max(0.0, drawdown_pct));
  if (has_last_observed_notional_) {
    bucket_window_notional_churn_usd_[active_index] +=
        std::fabs(account_notional_usd - last_observed_notional_usd_);
  }
  last_observed_notional_usd_ = account_notional_usd;
  has_last_observed_notional_ = true;
  last_observed_equity_usd_ = equity_usd;

  if (current_tick < next_eval_tick_) {
    return std::nullopt;
  }

  const std::size_t eval_index = SelectEvalBucket(active_index);
  const RegimeBucket eval_bucket = BucketFromIndex(eval_index);
  const double window_pnl_usd = bucket_window_pnl_usd_[eval_index];
  const double window_max_drawdown_pct = bucket_window_max_drawdown_pct_[eval_index];
  const double window_notional_churn_usd =
      bucket_window_notional_churn_usd_[eval_index];
  const double window_objective_score =
      ComputeObjectiveScore(window_pnl_usd,
                            window_max_drawdown_pct,
                            window_notional_churn_usd);
  const int window_bucket_ticks = bucket_window_ticks_[eval_index];

  // 固定周期评估：先结算窗口，再推进下一个评估点。
  next_eval_tick_ = current_tick + EffectiveUpdateIntervalTicks();

  BucketRuntime& runtime = RuntimeFor(eval_bucket);

  SelfEvolutionAction action;
  action.tick = current_tick;
  action.regime_bucket = eval_bucket;
  action.window_pnl_usd = window_pnl_usd;
  action.window_objective_score = window_objective_score;
  action.window_max_drawdown_pct = window_max_drawdown_pct;
  action.window_notional_churn_usd = window_notional_churn_usd;
  action.window_bucket_ticks = window_bucket_ticks;
  action.trend_weight_before = runtime.current_trend_weight;
  action.defensive_weight_before = runtime.current_defensive_weight;
  action.trend_weight_after = runtime.current_trend_weight;
  action.defensive_weight_after = runtime.current_defensive_weight;
  action.degrade_windows = degrade_window_count(eval_bucket);

  if (window_bucket_ticks <= 0) {
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "EVOLUTION_EMPTY_WINDOW";
    ResetWindowAttribution();
    return action;
  }

  if (window_bucket_ticks < config_.min_bucket_ticks_for_update) {
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "EVOLUTION_BUCKET_TICKS_INSUFFICIENT";
    ResetWindowAttribution();
    return action;
  }

  if (current_tick < cooldown_until_tick_) {
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "EVOLUTION_COOLDOWN_ACTIVE";
    action.cooldown_remaining_ticks =
        static_cast<int>(cooldown_until_tick_ - current_tick);
    action.degrade_windows = degrade_window_count(eval_bucket);
    ResetWindowAttribution();
    return action;
  }

  // 窗口盈亏振幅过小视为“样本不足”，跳过本轮学习更新。
  if (std::fabs(window_pnl_usd) < config_.min_abs_window_pnl_usd) {
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "EVOLUTION_WINDOW_PNL_TOO_SMALL";
    action.degrade_windows = degrade_window_count(eval_bucket);
    ResetWindowAttribution();
    return action;
  }

  PushDegradeWindow(&runtime,
                    window_objective_score <= ObjectiveThreshold());
  action.degrade_windows = degrade_window_count(eval_bucket);

  if (ShouldRollback(runtime)) {
    runtime.current_trend_weight = runtime.rollback_anchor_trend_weight;
    runtime.current_defensive_weight = runtime.rollback_anchor_defensive_weight;
    cooldown_until_tick_ = current_tick + config_.rollback_cooldown_ticks;
    runtime.degrade_windows.clear();

    action.type = SelfEvolutionActionType::kRolledBack;
    action.reason_code = "EVOLUTION_ROLLBACK_TRIGGERED";
    action.trend_weight_after = runtime.current_trend_weight;
    action.defensive_weight_after = runtime.current_defensive_weight;
    action.cooldown_remaining_ticks = config_.rollback_cooldown_ticks;
    action.degrade_windows = 0;
    ResetWindowAttribution();
    return action;
  }

  const auto candidate = ProposeWeights(window_objective_score, runtime);
  if (std::fabs(candidate.trend_weight - runtime.current_trend_weight) <= kWeightEpsilon &&
      std::fabs(candidate.defensive_weight - runtime.current_defensive_weight) <=
          kWeightEpsilon) {
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "EVOLUTION_WEIGHT_NOOP";
    ResetWindowAttribution();
    return action;
  }

  std::string error;
  if (!ValidateWeights(candidate.trend_weight, candidate.defensive_weight, &error)) {
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "PORT_WEIGHT_INVALID_REJECTED";
    ResetWindowAttribution();
    return action;
  }

  // 成功更新前记录当前 bucket 的回滚锚点：回滚时恢复到“上一版权重”。
  runtime.rollback_anchor_trend_weight = runtime.current_trend_weight;
  runtime.rollback_anchor_defensive_weight = runtime.current_defensive_weight;
  runtime.current_trend_weight = candidate.trend_weight;
  runtime.current_defensive_weight = candidate.defensive_weight;

  action.type = SelfEvolutionActionType::kUpdated;
  action.reason_code = window_objective_score > ObjectiveThreshold()
                           ? "EVOLUTION_WEIGHT_INCREASE_TREND"
                           : "EVOLUTION_WEIGHT_DECREASE_TREND";
  action.trend_weight_after = runtime.current_trend_weight;
  action.defensive_weight_after = runtime.current_defensive_weight;
  ResetWindowAttribution();
  return action;
}

int SelfEvolutionController::EffectiveUpdateIntervalTicks() const {
  return std::max(config_.update_interval_ticks,
                  config_.min_update_interval_ticks);
}

std::size_t SelfEvolutionController::SelectEvalBucket(
    std::size_t preferred_index) const {
  std::size_t selected = preferred_index;
  int max_ticks = bucket_window_ticks_[preferred_index];
  for (std::size_t i = 0; i < bucket_window_ticks_.size(); ++i) {
    if (bucket_window_ticks_[i] > max_ticks) {
      max_ticks = bucket_window_ticks_[i];
      selected = i;
    }
  }
  return selected;
}

double SelfEvolutionController::ComputeObjectiveScore(
    double window_pnl_usd,
    double window_max_drawdown_pct,
    double window_notional_churn_usd) const {
  const double drawdown_bps = std::max(0.0, window_max_drawdown_pct) * 10000.0;
  const double churn_usd = std::max(0.0, window_notional_churn_usd);
  return config_.objective_alpha_pnl * window_pnl_usd -
         config_.objective_beta_drawdown * drawdown_bps -
         config_.objective_gamma_notional_churn * churn_usd;
}

void SelfEvolutionController::ResetWindowAttribution() {
  bucket_window_pnl_usd_.fill(0.0);
  bucket_window_max_drawdown_pct_.fill(0.0);
  bucket_window_notional_churn_usd_.fill(0.0);
  bucket_window_ticks_.fill(0);
}

bool SelfEvolutionController::ValidateWeights(double trend_weight,
                                              double defensive_weight,
                                              std::string* out_error) const {
  if (trend_weight < -kWeightEpsilon || defensive_weight < -kWeightEpsilon) {
    if (out_error != nullptr) {
      *out_error = "权重不能为负数";
    }
    return false;
  }
  if (trend_weight > config_.max_single_strategy_weight + kWeightEpsilon ||
      defensive_weight > config_.max_single_strategy_weight + kWeightEpsilon) {
    if (out_error != nullptr) {
      *out_error = "权重超过单策略上限";
    }
    return false;
  }
  if (std::fabs((trend_weight + defensive_weight) - 1.0) > 1e-6) {
    if (out_error != nullptr) {
      *out_error = "权重和必须为1";
    }
    return false;
  }
  return true;
}

EvolutionWeights SelfEvolutionController::ProposeWeights(
    double objective_score,
    const BucketRuntime& runtime) const {
  const double direction =
      objective_score > ObjectiveThreshold() ? 1.0 : -1.0;
  const double min_trend_weight = 1.0 - config_.max_single_strategy_weight;
  const double max_trend_weight = config_.max_single_strategy_weight;

  double trend_weight =
      runtime.current_trend_weight + direction * config_.max_weight_step;
  trend_weight = std::clamp(trend_weight, min_trend_weight, max_trend_weight);
  const double defensive_weight = 1.0 - trend_weight;
  return EvolutionWeights{trend_weight, defensive_weight};
}

void SelfEvolutionController::PushDegradeWindow(BucketRuntime* runtime,
                                                bool degraded) {
  if (runtime == nullptr) {
    return;
  }
  runtime->degrade_windows.push_back(degraded);
  while (static_cast<int>(runtime->degrade_windows.size()) >
         config_.rollback_degrade_windows) {
    runtime->degrade_windows.pop_front();
  }
}

bool SelfEvolutionController::ShouldRollback(const BucketRuntime& runtime) const {
  if (static_cast<int>(runtime.degrade_windows.size()) <
      config_.rollback_degrade_windows) {
    return false;
  }
  for (bool degraded : runtime.degrade_windows) {
    if (!degraded) {
      return false;
    }
  }
  return true;
}

}  // namespace ai_trade
