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
    runtime.baseline_trend_weight = runtime.current_trend_weight;
    runtime.baseline_defensive_weight = runtime.current_defensive_weight;
    runtime.degrade_windows.clear();
    runtime.pending_direction = 0;
    runtime.pending_direction_streak = 0;
  }

  last_observed_realized_net_pnl_usd_ = initial_realized_net_pnl_usd;
  has_last_observed_realized_net_pnl_ = true;
  last_observed_equity_usd_ = initial_equity_usd;
  has_last_observed_equity_ = true;
  last_observed_notional_usd_ = 0.0;
  has_last_observed_notional_ = false;
  signal_states_by_symbol_.clear();
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
    double mark_price_usd,
    const std::string& signal_symbol,
    bool entry_filtered_by_cost,
    int fill_count,
    double account_equity_usd,
    double observed_turnover_cost_bps) {
  if (!config_.enabled || !initialized_) {
    return std::nullopt;
  }

  const std::size_t active_index = BucketIndex(regime_bucket);
  if (account_equity_usd > 0.0 && std::isfinite(account_equity_usd)) {
    last_observed_equity_usd_ = account_equity_usd;
    has_last_observed_equity_ = true;
  }
  const double objective_equity_usd =
      has_last_observed_equity_ ? last_observed_equity_usd_ : 1.0;
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
  if (fill_count > 0) {
    bucket_window_fill_count_[active_index] += fill_count;
  }
  if (entry_filtered_by_cost) {
    bucket_window_cost_filtered_signals_[active_index] += 1;
  }
  bucket_window_max_drawdown_pct_[active_index] =
      std::max(bucket_window_max_drawdown_pct_[active_index],
               std::max(0.0, drawdown_pct));
  if (has_last_observed_notional_) {
    bucket_window_notional_churn_usd_[active_index] +=
        std::fabs(account_notional_usd - last_observed_notional_usd_);
  }

  double effective_turnover_cost_bps = std::max(0.0, config_.virtual_cost_bps);
  if (std::isfinite(observed_turnover_cost_bps) &&
      observed_turnover_cost_bps > 0.0) {
    effective_turnover_cost_bps =
        std::max(effective_turnover_cost_bps, observed_turnover_cost_bps);
  }
  if (config_.virtual_cost_dynamic_enabled &&
      bucket_window_ticks_[active_index] > 0) {
    const double cost_filtered_ratio =
        static_cast<double>(bucket_window_cost_filtered_signals_[active_index]) /
        static_cast<double>(bucket_window_ticks_[active_index]);
    const double max_multiplier =
        std::max(1.0, config_.virtual_cost_dynamic_max_multiplier);
    const double dynamic_multiplier =
        1.0 + std::clamp(cost_filtered_ratio, 0.0, 1.0) * (max_multiplier - 1.0);
    effective_turnover_cost_bps *= dynamic_multiplier;
  }
  const double turnover_cost_rate = effective_turnover_cost_bps / 10000.0;
  const double funding_rate_per_tick = config_.virtual_funding_rate_per_tick;
  const bool has_current_signal_state = !signal_symbol.empty() &&
                                        std::isfinite(mark_price_usd) &&
                                        mark_price_usd > 0.0;
  if (has_current_signal_state) {
    auto& signal_state = signal_states_by_symbol_[signal_symbol];
    if (signal_state.has_state && signal_state.mark_price_usd > 0.0) {
      const double forward_return = mark_price_usd / signal_state.mark_price_usd - 1.0;
      const BucketRuntime& runtime = bucket_runtime_[active_index];
      const double prev_blended_notional =
          BlendNotional(signal_state.trend_notional_usd,
                        signal_state.defensive_notional_usd,
                        runtime.current_trend_weight);
      const double curr_blended_notional =
          BlendNotional(trend_signal_notional_usd,
                        defensive_signal_notional_usd,
                        runtime.current_trend_weight);
      const double funding_pnl = -prev_blended_notional * funding_rate_per_tick;
      bucket_window_virtual_pnl_usd_[active_index] +=
          prev_blended_notional * forward_return + funding_pnl -
          std::fabs(curr_blended_notional - prev_blended_notional) *
              turnover_cost_rate;
      const double tick_virtual_pnl =
          prev_blended_notional * forward_return + funding_pnl -
          std::fabs(curr_blended_notional - prev_blended_notional) *
              turnover_cost_rate;
      if (config_.use_virtual_pnl) {
        auto& learnability_stats = bucket_window_learnability_stats_[active_index];
        learnability_stats.sum += tick_virtual_pnl;
        learnability_stats.sum_sq += tick_virtual_pnl * tick_virtual_pnl;
        ++learnability_stats.samples;
      }

      const double trend_component = signal_state.trend_notional_usd;
      const double defensive_component = signal_state.defensive_notional_usd;

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
        auto& bucket_counterfactual =
            bucket_window_virtual_pnl_by_candidate_[active_index];
        if (bucket_counterfactual.size() !=
            counterfactual_trend_weight_grid_.size()) {
          bucket_counterfactual.assign(counterfactual_trend_weight_grid_.size(), 0.0);
        }
        auto& bucket_counterfactual_diff_stats =
            bucket_window_counterfactual_diff_stats_by_candidate_[active_index];
        if (bucket_counterfactual_diff_stats.size() !=
            counterfactual_trend_weight_grid_.size()) {
          bucket_counterfactual_diff_stats.assign(
              counterfactual_trend_weight_grid_.size(),
              SampleAccumulator{});
        }
        for (std::size_t i = 0; i < counterfactual_trend_weight_grid_.size();
             ++i) {
          const double trend_weight = counterfactual_trend_weight_grid_[i];
          const double prev_counterfactual_notional =
              BlendNotional(signal_state.trend_notional_usd,
                            signal_state.defensive_notional_usd,
                            trend_weight);
          const double curr_counterfactual_notional =
              BlendNotional(trend_signal_notional_usd,
                            defensive_signal_notional_usd,
                            trend_weight);
          const double counterfactual_funding_pnl =
              -prev_counterfactual_notional * funding_rate_per_tick;
          const double counterfactual_tick_virtual_pnl =
              prev_counterfactual_notional * forward_return +
              counterfactual_funding_pnl -
              std::fabs(curr_counterfactual_notional - prev_counterfactual_notional) *
                  turnover_cost_rate;
          bucket_counterfactual[i] += counterfactual_tick_virtual_pnl;
          auto& diff_stats = bucket_counterfactual_diff_stats[i];
          const double diff_vs_current =
              counterfactual_tick_virtual_pnl - tick_virtual_pnl;
          diff_stats.sum += diff_vs_current;
          diff_stats.sum_sq += diff_vs_current * diff_vs_current;
          ++diff_stats.samples;
        }
      }
    }

    signal_state.trend_notional_usd = trend_signal_notional_usd;
    signal_state.defensive_notional_usd = defensive_signal_notional_usd;
    signal_state.mark_price_usd = mark_price_usd;
    signal_state.has_state = true;
  }

  last_observed_realized_net_pnl_usd_ = realized_net_pnl_usd;
  has_last_observed_realized_net_pnl_ = true;
  last_observed_notional_usd_ = account_notional_usd;
  has_last_observed_notional_ = true;

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
  const int window_bucket_ticks = bucket_window_ticks_[eval_index];
  const double window_objective_score =
      ComputeObjectiveScore(window_pnl_usd,
                            window_max_drawdown_pct,
                            window_notional_churn_usd,
                            objective_equity_usd,
                            window_bucket_ticks);

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
      config_.enable_factor_ic_adaptive_weights;
  action.counterfactual_fallback_to_factor_ic_enabled =
      action.used_counterfactual_search &&
      action.used_factor_ic_adaptive_weighting &&
      config_.counterfactual_fallback_to_factor_ic;
  action.counterfactual_fallback_to_factor_ic_used = false;
  action.counterfactual_required_improvement_usd =
      config_.counterfactual_min_improvement_usd;
  action.counterfactual_best_virtual_pnl_usd = window_virtual_pnl_usd;
  action.counterfactual_best_trend_weight = runtime.current_trend_weight;
  action.counterfactual_best_defensive_weight = runtime.current_defensive_weight;
  action.counterfactual_superiority_gate_enabled =
      action.used_counterfactual_search &&
      (config_.counterfactual_superiority_min_samples_for_update > 0 ||
       config_.counterfactual_superiority_min_t_stat_for_update > 0.0);
  action.counterfactual_superiority_gate_passed =
      !action.counterfactual_superiority_gate_enabled;
  action.counterfactual_superiority_t_stat = 0.0;
  action.counterfactual_superiority_samples = 0;
  action.window_fill_count = bucket_window_fill_count_[eval_index];
  action.window_cost_filtered_signals =
      bucket_window_cost_filtered_signals_[eval_index];
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
  action.effective_turnover_cost_bps = effective_turnover_cost_bps;
  action.funding_rate_per_tick = funding_rate_per_tick;
  action.rolled_back_to_baseline = false;
  action.candidate_trend_weight_delta = 0.0;
  action.direction_consistency_required =
      std::max(1, config_.min_consecutive_direction_windows);
  action.direction_consistency_streak =
      std::max(0, runtime.pending_direction_streak);
  action.direction_consistency_direction = runtime.pending_direction;
  action.degrade_windows = degrade_window_count(eval_bucket);
  std::optional<EvolutionWeights> best_counterfactual_candidate;
  bool counterfactual_improves = false;
  if (action.used_counterfactual_search) {
    const double required_by_ratio =
        std::max(0.0, config_.counterfactual_min_improvement_ratio_of_equity) *
        std::max(0.0, objective_equity_usd);
    action.counterfactual_required_improvement_usd =
        std::max(config_.counterfactual_min_improvement_usd, required_by_ratio);
    if (action.window_cost_filtered_signals > 0) {
      const double decay =
          std::max(0.0,
                   config_.counterfactual_improvement_decay_per_filtered_signal_usd);
      const double relaxed_threshold =
          action.counterfactual_required_improvement_usd -
          decay * static_cast<double>(action.window_cost_filtered_signals);
      action.counterfactual_required_improvement_usd =
          std::max(0.0, relaxed_threshold);
    }
    double best_virtual_pnl_usd = window_virtual_pnl_usd;
    std::size_t best_index = 0;
    best_counterfactual_candidate =
        BestCounterfactualWeights(
            eval_index, &best_virtual_pnl_usd, &best_index);
    action.counterfactual_best_virtual_pnl_usd = best_virtual_pnl_usd;
    if (best_counterfactual_candidate.has_value()) {
      action.counterfactual_best_trend_weight =
          best_counterfactual_candidate->trend_weight;
      action.counterfactual_best_defensive_weight =
          best_counterfactual_candidate->defensive_weight;
      counterfactual_improves =
          best_virtual_pnl_usd >
          window_virtual_pnl_usd + action.counterfactual_required_improvement_usd;
      if (eval_index < bucket_window_counterfactual_diff_stats_by_candidate_.size()) {
        const auto& bucket_diff_stats =
            bucket_window_counterfactual_diff_stats_by_candidate_[eval_index];
        if (best_index < bucket_diff_stats.size()) {
          const auto& diff_stats = bucket_diff_stats[best_index];
          action.counterfactual_superiority_t_stat =
              TStatFromAccumulator(diff_stats);
          action.counterfactual_superiority_samples = diff_stats.samples;
        }
      }
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

  bool force_factor_ic_candidate = false;
  if (action.used_counterfactual_search) {
    if (action.window_fill_count <
        config_.counterfactual_min_fill_count_for_update) {
      if (action.counterfactual_fallback_to_factor_ic_enabled) {
        // 低成交窗口下放弃反事实主路径，回退到 factor-IC 调权，
        // 避免“counterfactual 永久因 fill 不足而空转”。
        force_factor_ic_candidate = true;
        action.counterfactual_fallback_to_factor_ic_used = true;
      } else {
        action.type = SelfEvolutionActionType::kSkipped;
        action.reason_code = "EVOLUTION_COUNTERFACTUAL_FILL_COUNT_TOO_LOW";
        action.degrade_windows = degrade_window_count(eval_bucket);
        ResetWindowAttribution();
        return action;
      }
    }
    if (!force_factor_ic_candidate &&
        config_.counterfactual_min_t_stat_samples_for_update > 0 &&
        action.learnability_samples <
            config_.counterfactual_min_t_stat_samples_for_update) {
      action.type = SelfEvolutionActionType::kSkipped;
      action.reason_code = "EVOLUTION_COUNTERFACTUAL_TSTAT_SAMPLES_TOO_LOW";
      action.degrade_windows = degrade_window_count(eval_bucket);
      ResetWindowAttribution();
      return action;
    }
    if (!force_factor_ic_candidate &&
        config_.counterfactual_min_t_stat_abs_for_update > 0.0 &&
        std::fabs(action.learnability_t_stat) <
            config_.counterfactual_min_t_stat_abs_for_update) {
      action.type = SelfEvolutionActionType::kSkipped;
      action.reason_code = "EVOLUTION_COUNTERFACTUAL_TSTAT_TOO_LOW";
      action.degrade_windows = degrade_window_count(eval_bucket);
      ResetWindowAttribution();
      return action;
    }
    if (!force_factor_ic_candidate &&
        (!best_counterfactual_candidate.has_value() || !counterfactual_improves)) {
      if (action.counterfactual_fallback_to_factor_ic_enabled) {
        force_factor_ic_candidate = true;
        action.counterfactual_fallback_to_factor_ic_used = true;
      } else {
        action.type = SelfEvolutionActionType::kSkipped;
        action.reason_code = "EVOLUTION_COUNTERFACTUAL_IMPROVEMENT_TOO_SMALL";
        action.degrade_windows = degrade_window_count(eval_bucket);
        ResetWindowAttribution();
        return action;
      }
    }
    if (!force_factor_ic_candidate && action.counterfactual_superiority_gate_enabled) {
      if (action.counterfactual_superiority_samples <
          config_.counterfactual_superiority_min_samples_for_update) {
        if (action.counterfactual_fallback_to_factor_ic_enabled) {
          force_factor_ic_candidate = true;
          action.counterfactual_fallback_to_factor_ic_used = true;
        } else {
          action.type = SelfEvolutionActionType::kSkipped;
          action.reason_code =
              "EVOLUTION_COUNTERFACTUAL_SUPERIORITY_SAMPLES_TOO_LOW";
          action.degrade_windows = degrade_window_count(eval_bucket);
          ResetWindowAttribution();
          return action;
        }
      } else if (action.counterfactual_superiority_t_stat <
                 config_.counterfactual_superiority_min_t_stat_for_update) {
        if (action.counterfactual_fallback_to_factor_ic_enabled) {
          force_factor_ic_candidate = true;
          action.counterfactual_fallback_to_factor_ic_used = true;
        } else {
          action.type = SelfEvolutionActionType::kSkipped;
          action.reason_code = "EVOLUTION_COUNTERFACTUAL_SUPERIORITY_TSTAT_TOO_LOW";
          action.degrade_windows = degrade_window_count(eval_bucket);
          ResetWindowAttribution();
          return action;
        }
      } else {
        action.counterfactual_superiority_gate_passed = true;
      }
    }
  }

  PushDegradeWindow(&runtime,
                    window_objective_score <= ObjectiveThreshold());
  action.degrade_windows = degrade_window_count(eval_bucket);

  if (ShouldRollback(runtime)) {
    const bool rollback_to_baseline = config_.rollback_to_baseline_on_trigger;
    if (rollback_to_baseline) {
      runtime.current_trend_weight = runtime.baseline_trend_weight;
      runtime.current_defensive_weight = runtime.baseline_defensive_weight;
    } else {
      runtime.current_trend_weight = runtime.rollback_anchor_trend_weight;
      runtime.current_defensive_weight = runtime.rollback_anchor_defensive_weight;
    }
    runtime.pending_direction = 0;
    runtime.pending_direction_streak = 0;
    cooldown_until_tick_ = current_tick + config_.rollback_cooldown_ticks;
    runtime.degrade_windows.clear();

    action.type = SelfEvolutionActionType::kRolledBack;
    action.reason_code = "EVOLUTION_ROLLBACK_TRIGGERED";
    action.rolled_back_to_baseline = rollback_to_baseline;
    action.trend_weight_after = runtime.current_trend_weight;
    action.defensive_weight_after = runtime.current_defensive_weight;
    action.direction_consistency_streak = 0;
    action.direction_consistency_direction = 0;
    action.cooldown_remaining_ticks = config_.rollback_cooldown_ticks;
    action.degrade_windows = 0;
    ResetWindowAttribution();
    return action;
  }

  EvolutionWeights candidate{
      runtime.current_trend_weight,
      runtime.current_defensive_weight,
  };
  enum class CandidateSource {
    kObjective,
    kCounterfactual,
    kFactorIc,
  };
  CandidateSource candidate_source = CandidateSource::kObjective;
  std::optional<EvolutionWeights> factor_ic_candidate;
  if (action.used_counterfactual_search && !force_factor_ic_candidate) {
    candidate = *best_counterfactual_candidate;
    candidate_source = CandidateSource::kCounterfactual;
  } else if (action.used_factor_ic_adaptive_weighting) {
    factor_ic_candidate =
        ProposeFactorIcWeights(eval_index, runtime, &action);
    if (factor_ic_candidate.has_value()) {
      candidate = *factor_ic_candidate;
      candidate_source = CandidateSource::kFactorIc;
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
    if (candidate_source == CandidateSource::kCounterfactual &&
        action.counterfactual_fallback_to_factor_ic_enabled) {
      factor_ic_candidate = ProposeFactorIcWeights(eval_index, runtime, &action);
      if (factor_ic_candidate.has_value() &&
          (std::fabs(factor_ic_candidate->trend_weight -
                     runtime.current_trend_weight) > kWeightEpsilon ||
           std::fabs(factor_ic_candidate->defensive_weight -
                     runtime.current_defensive_weight) > kWeightEpsilon)) {
        candidate = *factor_ic_candidate;
        candidate_source = CandidateSource::kFactorIc;
        action.counterfactual_fallback_to_factor_ic_used = true;
      }
    }
  }

  if (std::fabs(candidate.trend_weight - runtime.current_trend_weight) <= kWeightEpsilon &&
      std::fabs(candidate.defensive_weight - runtime.current_defensive_weight) <=
          kWeightEpsilon) {
    runtime.pending_direction = 0;
    runtime.pending_direction_streak = 0;
    action.type = SelfEvolutionActionType::kSkipped;
    if (action.used_counterfactual_search) {
      action.reason_code =
          candidate_source == CandidateSource::kFactorIc
              ? "EVOLUTION_FACTOR_IC_WEIGHT_NOOP"
              : "EVOLUTION_COUNTERFACTUAL_WEIGHT_NOOP";
    } else if (candidate_source == CandidateSource::kFactorIc) {
      action.reason_code = "EVOLUTION_FACTOR_IC_WEIGHT_NOOP";
    } else {
      action.reason_code = "EVOLUTION_WEIGHT_NOOP";
    }
    action.direction_consistency_streak = 0;
    action.direction_consistency_direction = 0;
    ResetWindowAttribution();
    return action;
  }

  action.candidate_trend_weight_delta =
      std::fabs(candidate.trend_weight - runtime.current_trend_weight);
  if (action.candidate_trend_weight_delta + kWeightEpsilon <
      std::max(0.0, config_.min_effective_weight_delta)) {
    runtime.pending_direction = 0;
    runtime.pending_direction_streak = 0;
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "EVOLUTION_WEIGHT_DELTA_TOO_SMALL";
    action.direction_consistency_streak = 0;
    action.direction_consistency_direction = 0;
    ResetWindowAttribution();
    return action;
  }

  const int candidate_direction =
      candidate.trend_weight > runtime.current_trend_weight ? 1 : -1;
  if (candidate_direction == runtime.pending_direction) {
    runtime.pending_direction_streak += 1;
  } else {
    runtime.pending_direction = candidate_direction;
    runtime.pending_direction_streak = 1;
  }
  action.direction_consistency_streak = runtime.pending_direction_streak;
  action.direction_consistency_direction = runtime.pending_direction;
  if (action.direction_consistency_required > 1 &&
      runtime.pending_direction_streak < action.direction_consistency_required) {
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "EVOLUTION_DIRECTION_CONSISTENCY_PENDING";
    ResetWindowAttribution();
    return action;
  }

  std::string error;
  if (!ValidateWeights(candidate.trend_weight, candidate.defensive_weight, &error)) {
    runtime.pending_direction = 0;
    runtime.pending_direction_streak = 0;
    action.type = SelfEvolutionActionType::kSkipped;
    action.reason_code = "PORT_WEIGHT_INVALID_REJECTED";
    action.direction_consistency_streak = 0;
    action.direction_consistency_direction = 0;
    ResetWindowAttribution();
    return action;
  }

  // 成功更新前记录当前 bucket 的回滚锚点：回滚时恢复到“上一版权重”。
  runtime.rollback_anchor_trend_weight = runtime.current_trend_weight;
  runtime.rollback_anchor_defensive_weight = runtime.current_defensive_weight;
  runtime.current_trend_weight = candidate.trend_weight;
  runtime.current_defensive_weight = candidate.defensive_weight;

  action.type = SelfEvolutionActionType::kUpdated;
  if (candidate_source == CandidateSource::kCounterfactual) {
    action.reason_code = candidate.trend_weight > runtime.rollback_anchor_trend_weight
                             ? "EVOLUTION_COUNTERFACTUAL_INCREASE_TREND"
                             : "EVOLUTION_COUNTERFACTUAL_DECREASE_TREND";
  } else if (candidate_source == CandidateSource::kFactorIc) {
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
    double window_notional_churn_usd,
    double account_equity_usd,
    int window_ticks) const {
  const double normalized_equity =
      std::max(1.0, std::fabs(account_equity_usd));
  if (config_.objective_use_sharpe_like) {
    const int safe_ticks = std::max(1, window_ticks);
    const double window_return = window_pnl_usd / normalized_equity;
    const double per_tick_return =
        window_return / static_cast<double>(safe_ticks);
    const double drawdown_proxy =
        std::max(0.0, window_max_drawdown_pct) /
        std::sqrt(static_cast<double>(safe_ticks));
    const double sharpe_like =
        per_tick_return / std::max(drawdown_proxy, 1e-9);
    const double drawdown_penalty = std::max(0.0, window_max_drawdown_pct);
    const double churn_penalty =
        std::max(0.0, window_notional_churn_usd) / normalized_equity;
    return config_.objective_alpha_pnl * sharpe_like -
           config_.objective_beta_drawdown * drawdown_penalty -
           config_.objective_gamma_notional_churn * churn_penalty;
  }
  const double pnl_bps = window_pnl_usd / normalized_equity * 10000.0;
  const double drawdown_bps = std::max(0.0, window_max_drawdown_pct) * 10000.0;
  const double churn_bps =
      std::max(0.0, window_notional_churn_usd) / normalized_equity * 10000.0;
  return config_.objective_alpha_pnl * pnl_bps -
         config_.objective_beta_drawdown * drawdown_bps -
         config_.objective_gamma_notional_churn * churn_bps;
}

void SelfEvolutionController::InitializeCounterfactualGrid() {
  counterfactual_trend_weight_grid_.clear();
  if (!config_.use_counterfactual_search) {
    for (auto& bucket_window : bucket_window_virtual_pnl_by_candidate_) {
      bucket_window.clear();
    }
    for (auto& bucket_stats : bucket_window_counterfactual_diff_stats_by_candidate_) {
      bucket_stats.clear();
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
  for (auto& bucket_stats : bucket_window_counterfactual_diff_stats_by_candidate_) {
    bucket_stats.assign(counterfactual_trend_weight_grid_.size(),
                        SampleAccumulator{});
  }
}

std::optional<EvolutionWeights> SelfEvolutionController::BestCounterfactualWeights(
    std::size_t bucket_index,
    double* out_best_virtual_pnl_usd,
    std::size_t* out_best_index) const {
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
  if (out_best_index != nullptr) {
    *out_best_index = best_index;
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

  const bool trend_ic_strong =
      std::fabs(trend_ic) >= config_.factor_ic_min_abs;
  const bool defensive_ic_strong =
      std::fabs(defensive_ic) >= config_.factor_ic_min_abs;
  if (!trend_ic_strong && !defensive_ic_strong) {
    return std::nullopt;
  }

  // deadzone 内视为噪声，不驱动权重；超出 deadzone 后再线性放大。
  const double deadzone =
      std::clamp(std::max(config_.factor_ic_min_abs,
                          config_.factor_ic_deadzone_abs),
                 0.0, 0.99);
  const auto ic_to_score = [deadzone](double ic) {
    if (!std::isfinite(ic)) {
      return 0.5;
    }
    const double abs_ic = std::fabs(ic);
    if (abs_ic <= deadzone) {
      return 0.5;
    }
    const double signed_excess =
        std::copysign((abs_ic - deadzone) / std::max(1e-9, 1.0 - deadzone), ic);
    return std::clamp(0.5 + 0.5 * signed_excess, 0.0, 1.0);
  };
  const double trend_score = trend_ic_strong ? ic_to_score(trend_ic) : 0.5;
  const double defensive_score =
      defensive_ic_strong ? ic_to_score(defensive_ic) : 0.5;
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
  for (auto& bucket_stats : bucket_window_counterfactual_diff_stats_by_candidate_) {
    std::fill(bucket_stats.begin(), bucket_stats.end(), SampleAccumulator{});
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
  bucket_window_fill_count_.fill(0);
  bucket_window_cost_filtered_signals_.fill(0);
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
