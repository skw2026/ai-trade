#include "evolution/self_evolution_controller.h"

#include <algorithm>
#include <cmath>

namespace ai_trade {

namespace {

constexpr double kWeightEpsilon = 1e-9;
constexpr double kStatsEpsilon = 1e-12;

double BlendNotional(double trend_notional_usd,
                     double defensive_notional_usd,
                     double trend_weight) {
  return trend_notional_usd * trend_weight +
         defensive_notional_usd * (1.0 - trend_weight);
}

}  // namespace

SelfEvolutionController::SelfEvolutionController(SelfEvolutionConfig config)
    : config_(config) {
  InitializeCounterfactualGrid();
}

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
    std::string* out_error,
    double initial_realized_net_pnl_usd) {
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

  last_observed_realized_net_pnl_usd_ = initial_realized_net_pnl_usd;
  has_last_observed_realized_net_pnl_ = true;
  last_observed_notional_usd_ = 0.0;
  has_last_observed_notional_ = false;
  has_last_signal_state_ = false;
  last_signal_trend_notional_usd_ = 0.0;
  last_signal_defensive_notional_usd_ = 0.0;
  last_signal_price_usd_ = 0.0;
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
    double realized_net_pnl_usd,
    RegimeBucket regime_bucket,
    double drawdown_pct,
    double account_notional_usd,
    double trend_signal_notional_usd,
    double defensive_signal_notional_usd,
    double mark_price_usd) {
  if (!config_.enabled || !initialized_) {
    return std::nullopt;
  }

  const std::size_t active_index = BucketIndex(regime_bucket);
  double delta_realized_net_pnl_usd = 0.0;
  if (has_last_observed_realized_net_pnl_) {
    delta_realized_net_pnl_usd =
        realized_net_pnl_usd - last_observed_realized_net_pnl_usd_;
  }
  bucket_window_realized_pnl_usd_[active_index] += delta_realized_net_pnl_usd;
  if (!config_.use_virtual_pnl) {
    auto& learnability_stats = bucket_window_learnability_stats_[active_index];
    learnability_stats.sum += delta_realized_net_pnl_usd;
    learnability_stats.sum_sq +=
        delta_realized_net_pnl_usd * delta_realized_net_pnl_usd;
    ++learnability_stats.samples;
  }
  bucket_window_ticks_[active_index] += 1;
  bucket_window_max_drawdown_pct_[active_index] =
      std::max(bucket_window_max_drawdown_pct_[active_index],
               std::max(0.0, drawdown_pct));
  if (has_last_observed_notional_) {
    bucket_window_notional_churn_usd_[active_index] +=
        std::fabs(account_notional_usd - last_observed_notional_usd_);
  }

  const double turnover_cost_rate = std::max(0.0, config_.virtual_cost_bps) / 10000.0;
  const bool has_current_signal_state =
      std::isfinite(mark_price_usd) && mark_price_usd > 0.0;
  if (has_last_signal_state_ && has_current_signal_state &&
      last_signal_price_usd_ > 0.0) {
    const double forward_return = mark_price_usd / last_signal_price_usd_ - 1.0;
    const BucketRuntime& runtime = bucket_runtime_[active_index];
    const double prev_blended_notional =
        BlendNotional(last_signal_trend_notional_usd_,
                      last_signal_defensive_notional_usd_,
                      runtime.current_trend_weight);
    const double curr_blended_notional =
        BlendNotional(trend_signal_notional_usd,
                      defensive_signal_notional_usd,
                      runtime.current_trend_weight);
    bucket_window_virtual_pnl_usd_[active_index] +=
        prev_blended_notional * forward_return -
        std::fabs(curr_blended_notional - prev_blended_notional) *
            turnover_cost_rate;
    const double tick_virtual_pnl =
        prev_blended_notional * forward_return -
        std::fabs(curr_blended_notional - prev_blended_notional) *
            turnover_cost_rate;
    if (config_.use_virtual_pnl) {
      auto& learnability_stats = bucket_window_learnability_stats_[active_index];
      learnability_stats.sum += tick_virtual_pnl;
      learnability_stats.sum_sq += tick_virtual_pnl * tick_virtual_pnl;
      ++learnability_stats.samples;
    }

    const double trend_component = last_signal_trend_notional_usd_;
    const double defensive_component = last_signal_defensive_notional_usd_;

    auto& trend_factor_ic = bucket_window_trend_factor_ic_[active_index];
    trend_factor_ic.sum_x += trend_component;
    trend_factor_ic.sum_y += forward_return;
    trend_factor_ic.sum_x2 += trend_component * trend_component;
    trend_factor_ic.sum_y2 += forward_return * forward_return;
    trend_factor_ic.sum_xy += trend_component * forward_return;
    ++trend_factor_ic.samples;

    auto& defensive_factor_ic =
        bucket_window_defensive_factor_ic_[active_index];
    defensive_factor_ic.sum_x += defensive_component;
    defensive_factor_ic.sum_y += forward_return;
    defensive_factor_ic.sum_x2 += defensive_component * defensive_component;
    defensive_factor_ic.sum_y2 += forward_return * forward_return;
    defensive_factor_ic.sum_xy += defensive_component * forward_return;
    ++defensive_factor_ic.samples;

    if (config_.use_counterfactual_search &&
        !counterfactual_trend_weight_grid_.empty()) {
      auto& bucket_counterfactual = bucket_window_virtual_pnl_by_candidate_[active_index];
      if (bucket_counterfactual.size() != counterfactual_trend_weight_grid_.size()) {
        bucket_counterfactual.assign(counterfactual_trend_weight_grid_.size(), 0.0);
      }
      for (std::size_t i = 0; i < counterfactual_trend_weight_grid_.size(); ++i) {
        const double trend_weight = counterfactual_trend_weight_grid_[i];
        const double prev_counterfactual_notional =
            BlendNotional(last_signal_trend_notional_usd_,
                          last_signal_defensive_notional_usd_,
                          trend_weight);
        const double curr_counterfactual_notional =
            BlendNotional(trend_signal_notional_usd,
                          defensive_signal_notional_usd,
                          trend_weight);
        bucket_counterfactual[i] +=
            prev_counterfactual_notional * forward_return -
            std::fabs(curr_counterfactual_notional - prev_counterfactual_notional) *
                turnover_cost_rate;
      }
    }
  }

  last_observed_realized_net_pnl_usd_ = realized_net_pnl_usd;
  has_last_observed_realized_net_pnl_ = true;
  last_observed_notional_usd_ = account_notional_usd;
  has_last_observed_notional_ = true;
  if (has_current_signal_state) {
    last_signal_trend_notional_usd_ = trend_signal_notional_usd;
    last_signal_defensive_notional_usd_ = defensive_signal_notional_usd;
    last_signal_price_usd_ = mark_price_usd;
    has_last_signal_state_ = true;
  } else {
    has_last_signal_state_ = false;
  }

  if (current_tick < next_eval_tick_) {
    return std::nullopt;
  }

  const std::size_t eval_index = SelectEvalBucket(active_index);
  const RegimeBucket eval_bucket = BucketFromIndex(eval_index);
  const double window_realized_pnl_usd = bucket_window_realized_pnl_usd_[eval_index];
  const double window_virtual_pnl_usd = bucket_window_virtual_pnl_usd_[eval_index];
  const bool use_virtual_pnl = config_.use_virtual_pnl;
  const double window_pnl_usd =
      use_virtual_pnl ? window_virtual_pnl_usd : window_realized_pnl_usd;
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
  action.window_realized_pnl_usd = window_realized_pnl_usd;
  action.window_virtual_pnl_usd = window_virtual_pnl_usd;
  action.window_objective_score = window_objective_score;
  action.window_max_drawdown_pct = window_max_drawdown_pct;
  action.window_notional_churn_usd = window_notional_churn_usd;
  action.window_bucket_ticks = window_bucket_ticks;
  action.used_virtual_pnl = use_virtual_pnl;
  action.used_counterfactual_search =
      config_.use_counterfactual_search && use_virtual_pnl;
  action.used_factor_ic_adaptive_weighting =
      config_.enable_factor_ic_adaptive_weights &&
      !action.used_counterfactual_search;
  action.counterfactual_best_virtual_pnl_usd = window_virtual_pnl_usd;
  action.counterfactual_best_trend_weight = runtime.current_trend_weight;
  action.counterfactual_best_defensive_weight = runtime.current_defensive_weight;
  action.trend_factor_ic =
      CorrelationFromAccumulator(bucket_window_trend_factor_ic_[eval_index]);
  action.defensive_factor_ic =
      CorrelationFromAccumulator(bucket_window_defensive_factor_ic_[eval_index]);
  action.factor_ic_samples =
      std::min(bucket_window_trend_factor_ic_[eval_index].samples,
               bucket_window_defensive_factor_ic_[eval_index].samples);
  action.learnability_gate_enabled = config_.enable_learnability_gate;
  action.learnability_t_stat =
      TStatFromAccumulator(bucket_window_learnability_stats_[eval_index]);
  action.learnability_samples =
      bucket_window_learnability_stats_[eval_index].samples;
  action.learnability_gate_passed = !config_.enable_learnability_gate;
  action.trend_weight_before = runtime.current_trend_weight;
  action.defensive_weight_before = runtime.current_defensive_weight;
  action.trend_weight_after = runtime.current_trend_weight;
  action.defensive_weight_after = runtime.current_defensive_weight;
  action.degrade_windows = degrade_window_count(eval_bucket);
  std::optional<EvolutionWeights> best_counterfactual_candidate;
  bool counterfactual_improves = false;
  if (action.used_counterfactual_search) {
    double best_virtual_pnl_usd = window_virtual_pnl_usd;
    best_counterfactual_candidate =
        BestCounterfactualWeights(eval_index, &best_virtual_pnl_usd);
    action.counterfactual_best_virtual_pnl_usd = best_virtual_pnl_usd;
    if (best_counterfactual_candidate.has_value()) {
      action.counterfactual_best_trend_weight =
          best_counterfactual_candidate->trend_weight;
      action.counterfactual_best_defensive_weight =
          best_counterfactual_candidate->defensive_weight;
      counterfactual_improves =
          best_virtual_pnl_usd >
          window_virtual_pnl_usd + config_.counterfactual_min_improvement_usd;
    }
  }

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
  double sample_abs_window_pnl_usd = std::fabs(window_pnl_usd);
  if (action.used_counterfactual_search) {
    sample_abs_window_pnl_usd =
        std::max(sample_abs_window_pnl_usd,
                 std::fabs(action.counterfactual_best_virtual_pnl_usd));
  }
  if (sample_abs_window_pnl_usd < config_.min_abs_window_pnl_usd) {
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "EVOLUTION_WINDOW_PNL_TOO_SMALL";
    action.degrade_windows = degrade_window_count(eval_bucket);
    ResetWindowAttribution();
    return action;
  }

  if (config_.enable_learnability_gate) {
    if (action.learnability_samples < config_.learnability_min_samples) {
      action.type = SelfEvolutionActionType::kSkipped;
      action.reason_code = "EVOLUTION_LEARNABILITY_INSUFFICIENT_SAMPLES";
      action.degrade_windows = degrade_window_count(eval_bucket);
      ResetWindowAttribution();
      return action;
    }
    if (std::fabs(action.learnability_t_stat) <
        config_.learnability_min_t_stat_abs) {
      action.type = SelfEvolutionActionType::kSkipped;
      action.reason_code = "EVOLUTION_LEARNABILITY_TSTAT_TOO_LOW";
      action.degrade_windows = degrade_window_count(eval_bucket);
      ResetWindowAttribution();
      return action;
    }
    action.learnability_gate_passed = true;
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

  EvolutionWeights candidate{
      runtime.current_trend_weight,
      runtime.current_defensive_weight,
  };
  std::optional<EvolutionWeights> factor_ic_candidate;
  if (action.used_counterfactual_search) {
    if (best_counterfactual_candidate.has_value() && counterfactual_improves) {
      candidate = *best_counterfactual_candidate;
    }
  } else if (action.used_factor_ic_adaptive_weighting) {
    factor_ic_candidate =
        ProposeFactorIcWeights(eval_index, runtime, &action);
    if (factor_ic_candidate.has_value()) {
      candidate = *factor_ic_candidate;
    } else {
      action.type = SelfEvolutionActionType::kSkipped;
      action.reason_code =
          action.factor_ic_samples < config_.factor_ic_min_samples
              ? "EVOLUTION_FACTOR_IC_INSUFFICIENT_SAMPLES"
              : "EVOLUTION_FACTOR_IC_SIGNAL_WEAK";
      ResetWindowAttribution();
      return action;
    }
  } else {
    candidate = ProposeWeights(window_objective_score, runtime);
  }

  if (std::fabs(candidate.trend_weight - runtime.current_trend_weight) <= kWeightEpsilon &&
      std::fabs(candidate.defensive_weight - runtime.current_defensive_weight) <=
          kWeightEpsilon) {
    action.type = SelfEvolutionActionType::kSkipped;
    if (action.used_counterfactual_search) {
      action.reason_code = "EVOLUTION_COUNTERFACTUAL_WEIGHT_NOOP";
    } else if (action.used_factor_ic_adaptive_weighting) {
      action.reason_code = "EVOLUTION_FACTOR_IC_WEIGHT_NOOP";
    } else {
      action.reason_code = "EVOLUTION_WEIGHT_NOOP";
    }
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
  if (action.used_counterfactual_search) {
    action.reason_code = candidate.trend_weight > runtime.rollback_anchor_trend_weight
                             ? "EVOLUTION_COUNTERFACTUAL_INCREASE_TREND"
                             : "EVOLUTION_COUNTERFACTUAL_DECREASE_TREND";
  } else if (action.used_factor_ic_adaptive_weighting) {
    action.reason_code = candidate.trend_weight > runtime.rollback_anchor_trend_weight
                             ? "EVOLUTION_FACTOR_IC_INCREASE_TREND"
                             : "EVOLUTION_FACTOR_IC_DECREASE_TREND";
  } else {
    action.reason_code = window_objective_score > ObjectiveThreshold()
                             ? "EVOLUTION_WEIGHT_INCREASE_TREND"
                             : "EVOLUTION_WEIGHT_DECREASE_TREND";
  }
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

void SelfEvolutionController::InitializeCounterfactualGrid() {
  counterfactual_trend_weight_grid_.clear();
  if (!config_.use_counterfactual_search) {
    for (auto& bucket_window : bucket_window_virtual_pnl_by_candidate_) {
      bucket_window.clear();
    }
    return;
  }

  const double min_trend_weight = 1.0 - config_.max_single_strategy_weight;
  const double max_trend_weight = config_.max_single_strategy_weight;
  const double step = std::max(config_.max_weight_step, 1e-4);
  for (double weight = min_trend_weight; weight <= max_trend_weight + 1e-9;
       weight += step) {
    counterfactual_trend_weight_grid_.push_back(
        std::clamp(weight, min_trend_weight, max_trend_weight));
  }
  if (counterfactual_trend_weight_grid_.empty() ||
      std::fabs(counterfactual_trend_weight_grid_.back() - max_trend_weight) >
          1e-9) {
    counterfactual_trend_weight_grid_.push_back(max_trend_weight);
  }

  for (auto& bucket_window : bucket_window_virtual_pnl_by_candidate_) {
    bucket_window.assign(counterfactual_trend_weight_grid_.size(), 0.0);
  }
}

std::optional<EvolutionWeights> SelfEvolutionController::BestCounterfactualWeights(
    std::size_t bucket_index,
    double* out_best_virtual_pnl_usd) const {
  if (bucket_index >= bucket_window_virtual_pnl_by_candidate_.size() ||
      counterfactual_trend_weight_grid_.empty()) {
    return std::nullopt;
  }

  const auto& bucket_window = bucket_window_virtual_pnl_by_candidate_[bucket_index];
  if (bucket_window.size() != counterfactual_trend_weight_grid_.size() ||
      bucket_window.empty()) {
    return std::nullopt;
  }

  std::size_t best_index = 0;
  double best_virtual_pnl = bucket_window[0];
  for (std::size_t i = 1; i < bucket_window.size(); ++i) {
    if (bucket_window[i] > best_virtual_pnl) {
      best_virtual_pnl = bucket_window[i];
      best_index = i;
    }
  }

  if (out_best_virtual_pnl_usd != nullptr) {
    *out_best_virtual_pnl_usd = best_virtual_pnl;
  }
  const double trend_weight = counterfactual_trend_weight_grid_[best_index];
  return EvolutionWeights{
      trend_weight,
      1.0 - trend_weight,
  };
}

double SelfEvolutionController::CorrelationFromAccumulator(
    const CorrelationAccumulator& accumulator) const {
  if (accumulator.samples < 2) {
    return 0.0;
  }
  const double n = static_cast<double>(accumulator.samples);
  const double cov_num =
      n * accumulator.sum_xy - accumulator.sum_x * accumulator.sum_y;
  const double var_x_num =
      n * accumulator.sum_x2 - accumulator.sum_x * accumulator.sum_x;
  const double var_y_num =
      n * accumulator.sum_y2 - accumulator.sum_y * accumulator.sum_y;
  if (var_x_num <= kStatsEpsilon || var_y_num <= kStatsEpsilon) {
    return 0.0;
  }
  const double corr = cov_num / std::sqrt(var_x_num * var_y_num);
  if (!std::isfinite(corr)) {
    return 0.0;
  }
  return std::clamp(corr, -1.0, 1.0);
}

double SelfEvolutionController::TStatFromAccumulator(
    const SampleAccumulator& accumulator) const {
  if (accumulator.samples < 2) {
    return 0.0;
  }
  const double n = static_cast<double>(accumulator.samples);
  const double mean = accumulator.sum / n;
  const double variance =
      std::max(0.0, accumulator.sum_sq / n - mean * mean);
  if (variance <= kStatsEpsilon) {
    return 0.0;
  }
  const double std_error = std::sqrt(variance / n);
  if (std_error <= kStatsEpsilon) {
    return 0.0;
  }
  return mean / std_error;
}

std::optional<EvolutionWeights> SelfEvolutionController::ProposeFactorIcWeights(
    std::size_t bucket_index,
    const BucketRuntime& runtime,
    SelfEvolutionAction* out_action) const {
  if (!config_.enable_factor_ic_adaptive_weights) {
    return std::nullopt;
  }
  if (bucket_index >= bucket_window_trend_factor_ic_.size() ||
      bucket_index >= bucket_window_defensive_factor_ic_.size()) {
    return std::nullopt;
  }

  const auto& trend_ic_acc = bucket_window_trend_factor_ic_[bucket_index];
  const auto& defensive_ic_acc = bucket_window_defensive_factor_ic_[bucket_index];
  const int samples = std::min(trend_ic_acc.samples, defensive_ic_acc.samples);
  const double trend_ic = CorrelationFromAccumulator(trend_ic_acc);
  const double defensive_ic = CorrelationFromAccumulator(defensive_ic_acc);
  if (out_action != nullptr) {
    out_action->trend_factor_ic = trend_ic;
    out_action->defensive_factor_ic = defensive_ic;
    out_action->factor_ic_samples = samples;
  }
  if (samples < config_.factor_ic_min_samples) {
    return std::nullopt;
  }

  const double trend_score =
      std::fabs(trend_ic) >= config_.factor_ic_min_abs ? std::max(0.0, trend_ic)
                                                       : 0.0;
  const double defensive_score =
      std::fabs(defensive_ic) >= config_.factor_ic_min_abs
          ? std::max(0.0, defensive_ic)
          : 0.0;
  const double score_sum = trend_score + defensive_score;
  if (score_sum <= kStatsEpsilon) {
    return std::nullopt;
  }

  const double min_trend_weight = 1.0 - config_.max_single_strategy_weight;
  const double max_trend_weight = config_.max_single_strategy_weight;
  double target_trend_weight = trend_score / score_sum;
  target_trend_weight =
      std::clamp(target_trend_weight, min_trend_weight, max_trend_weight);

  const double trend_step = std::clamp(
      target_trend_weight - runtime.current_trend_weight,
      -config_.max_weight_step,
      config_.max_weight_step);
  const double trend_weight = std::clamp(
      runtime.current_trend_weight + trend_step, min_trend_weight, max_trend_weight);
  return EvolutionWeights{trend_weight, 1.0 - trend_weight};
}

void SelfEvolutionController::ResetWindowAttribution() {
  bucket_window_realized_pnl_usd_.fill(0.0);
  bucket_window_virtual_pnl_usd_.fill(0.0);
  for (auto& bucket_window : bucket_window_virtual_pnl_by_candidate_) {
    std::fill(bucket_window.begin(), bucket_window.end(), 0.0);
  }
  for (auto& trend_ic : bucket_window_trend_factor_ic_) {
    trend_ic = CorrelationAccumulator{};
  }
  for (auto& defensive_ic : bucket_window_defensive_factor_ic_) {
    defensive_ic = CorrelationAccumulator{};
  }
  for (auto& learnability_stats : bucket_window_learnability_stats_) {
    learnability_stats = SampleAccumulator{};
  }
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
