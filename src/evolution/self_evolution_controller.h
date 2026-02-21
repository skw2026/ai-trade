#pragma once

#include <array>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "core/config.h"

namespace ai_trade {

/// 自进化动作类型：更新、回滚、跳过（含冷却/无变化/非法候选）。
enum class SelfEvolutionActionType {
  kUpdated,
  kRolledBack,
  kSkipped,
};

/// 单次自进化评估结果（用于审计日志与运行态观测）。
struct SelfEvolutionAction {
  SelfEvolutionActionType type{SelfEvolutionActionType::kSkipped};
  std::string reason_code;
  std::int64_t tick{0};
  RegimeBucket regime_bucket{RegimeBucket::kRange};
  double window_pnl_usd{0.0};
  double window_realized_pnl_usd{0.0};
  double window_virtual_pnl_usd{0.0};
  double window_objective_score{0.0};
  double window_max_drawdown_pct{0.0};
  double window_notional_churn_usd{0.0};
  int window_bucket_ticks{0};
  bool used_virtual_pnl{false};
  bool used_counterfactual_search{false};
  bool used_factor_ic_adaptive_weighting{false};
  bool counterfactual_fallback_to_factor_ic_enabled{false};
  bool counterfactual_fallback_to_factor_ic_used{false};
  double counterfactual_best_virtual_pnl_usd{0.0};
  double counterfactual_required_improvement_usd{0.0};
  double counterfactual_best_trend_weight{0.0};
  double counterfactual_best_defensive_weight{0.0};
  int window_fill_count{0};
  int window_cost_filtered_signals{0};
  double trend_factor_ic{0.0};
  double defensive_factor_ic{0.0};
  int factor_ic_samples{0};
  bool learnability_gate_enabled{false};
  bool learnability_gate_passed{true};
  double learnability_t_stat{0.0};
  int learnability_samples{0};
  double trend_weight_before{0.0};
  double defensive_weight_before{0.0};
  double trend_weight_after{0.0};
  double defensive_weight_after{0.0};
  double effective_turnover_cost_bps{0.0};
  double funding_rate_per_tick{0.0};
  bool rolled_back_to_baseline{false};
  double candidate_trend_weight_delta{0.0};
  int direction_consistency_required{1};
  int direction_consistency_streak{0};
  int direction_consistency_direction{0};  // +1=trend up, -1=trend down, 0=none
  int cooldown_remaining_ticks{0};
  int degrade_windows{0};
};

/**
 * @brief 阶段2最小自进化控制器
 *
 * 设计边界：
 * 1. 仅管理双策略权重（trend + defensive）；
 * 2. 仅做受限更新（步长/上限/非负/和为1）；
 * 3. 在连续退化窗口下触发权重回滚与冷却。
 *
 * 非职责：
 * - 不做复杂模型训练；
 * - 不直接修改 RiskEngine 不可动层参数。
 */
class SelfEvolutionController {
 public:
  explicit SelfEvolutionController(SelfEvolutionConfig config);

  /**
   * @brief 初始化状态
   * @param current_tick 当前行情 tick 序号
   * @param initial_equity_usd 初始化权益（作为首个窗口参考）
   * @param initial_weights 初始权重 (trend, defensive)
   */
  bool Initialize(std::int64_t current_tick,
                  double initial_equity_usd,
                  const std::pair<double, double>& initial_weights,
                  std::string* out_error,
                  double initial_realized_net_pnl_usd = 0.0);

  /**
   * @brief 每个行情 tick 调用一次，按配置周期评估是否更新/回滚
   *
   * @return 无动作时返回 `std::nullopt`；有动作（更新/回滚/跳过）时返回详情。
   */
  std::optional<SelfEvolutionAction> OnTick(std::int64_t current_tick,
                                            double realized_net_pnl_usd,
                                            RegimeBucket regime_bucket =
                                                RegimeBucket::kRange,
                                            double drawdown_pct = 0.0,
                                            double account_notional_usd = 0.0,
                                            double trend_signal_notional_usd = 0.0,
                                            double defensive_signal_notional_usd = 0.0,
                                            double mark_price_usd = 0.0,
                                            const std::string& signal_symbol = "",
                                            bool entry_filtered_by_cost = false,
                                            int fill_count = 0,
                                            double account_equity_usd = 0.0,
                                            double observed_turnover_cost_bps = 0.0);

  bool enabled() const { return config_.enabled; }
  bool initialized() const { return initialized_; }
  EvolutionWeights current_weights(RegimeBucket bucket) const;
  EvolutionWeights rollback_anchor_weights(RegimeBucket bucket) const;
  EvolutionWeights current_weights() const {
    return current_weights(RegimeBucket::kRange);
  }
  EvolutionWeights rollback_anchor_weights() const {
    return rollback_anchor_weights(RegimeBucket::kRange);
  }
  std::int64_t next_eval_tick() const { return next_eval_tick_; }
  std::int64_t cooldown_until_tick() const { return cooldown_until_tick_; }
  int degrade_window_count() const {
    return degrade_window_count(RegimeBucket::kRange);
  }
  int degrade_window_count(RegimeBucket bucket) const;

 private:
  struct BucketRuntime {
    double current_trend_weight{1.0};
    double current_defensive_weight{0.0};
    double rollback_anchor_trend_weight{1.0};
    double rollback_anchor_defensive_weight{0.0};
    double baseline_trend_weight{1.0};
    double baseline_defensive_weight{0.0};
    std::deque<bool> degrade_windows;
    int pending_direction{0};  // +1=trend up, -1=trend down, 0=none
    int pending_direction_streak{0};
  };
  struct CorrelationAccumulator {
    double sum_x{0.0};
    double sum_y{0.0};
    double sum_x2{0.0};
    double sum_y2{0.0};
    double sum_xy{0.0};
    int samples{0};
  };
  struct SampleAccumulator {
    double sum{0.0};
    double sum_sq{0.0};
    int samples{0};
  };
  struct SignalState {
    double trend_notional_usd{0.0};
    double defensive_notional_usd{0.0};
    double mark_price_usd{0.0};
    bool has_state{false};
  };

  static std::size_t BucketIndex(RegimeBucket bucket);
  static RegimeBucket BucketFromIndex(std::size_t index);
  BucketRuntime& RuntimeFor(RegimeBucket bucket);
  const BucketRuntime& RuntimeFor(RegimeBucket bucket) const;
  int EffectiveUpdateIntervalTicks() const;
  std::size_t SelectEvalBucket(std::size_t preferred_index) const;
  double ComputeObjectiveScore(double window_pnl_usd,
                               double window_max_drawdown_pct,
                               double window_notional_churn_usd,
                               double account_equity_usd) const;
  void InitializeCounterfactualGrid();
  std::optional<EvolutionWeights> BestCounterfactualWeights(
      std::size_t bucket_index,
      double* out_best_virtual_pnl_usd) const;
  double CorrelationFromAccumulator(
      const CorrelationAccumulator& accumulator) const;
  double TStatFromAccumulator(const SampleAccumulator& accumulator) const;
  std::optional<EvolutionWeights> ProposeFactorIcWeights(
      std::size_t bucket_index,
      const BucketRuntime& runtime,
      SelfEvolutionAction* out_action) const;
  double ObjectiveThreshold() const {
    return config_.rollback_degrade_threshold_score;
  }
  void ResetWindowAttribution();
  bool ValidateWeights(double trend_weight,
                       double defensive_weight,
                       std::string* out_error) const;
  EvolutionWeights ProposeWeights(double objective_score,
                                  const BucketRuntime& runtime) const;
  void PushDegradeWindow(BucketRuntime* runtime, bool degraded);
  bool ShouldRollback(const BucketRuntime& runtime) const;

  SelfEvolutionConfig config_;

  bool initialized_{false};
  std::array<BucketRuntime, 3> bucket_runtime_{};
  std::array<double, 3> bucket_window_realized_pnl_usd_{};
  std::array<double, 3> bucket_window_virtual_pnl_usd_{};
  std::array<std::vector<double>, 3> bucket_window_virtual_pnl_by_candidate_{};
  std::array<CorrelationAccumulator, 3> bucket_window_trend_factor_ic_{};
  std::array<CorrelationAccumulator, 3> bucket_window_defensive_factor_ic_{};
  std::array<SampleAccumulator, 3> bucket_window_learnability_stats_{};
  std::array<int, 3> bucket_window_fill_count_{};
  std::array<int, 3> bucket_window_cost_filtered_signals_{};
  std::array<double, 3> bucket_window_max_drawdown_pct_{};
  std::array<double, 3> bucket_window_notional_churn_usd_{};
  std::array<int, 3> bucket_window_ticks_{};
  std::vector<double> counterfactual_trend_weight_grid_{};

  double last_observed_realized_net_pnl_usd_{0.0};
  bool has_last_observed_realized_net_pnl_{false};
  double last_observed_equity_usd_{0.0};
  bool has_last_observed_equity_{false};
  double last_observed_notional_usd_{0.0};
  bool has_last_observed_notional_{false};
  std::unordered_map<std::string, SignalState> signal_states_by_symbol_;
  std::int64_t next_eval_tick_{0};
  std::int64_t cooldown_until_tick_{0};
};

}  // namespace ai_trade
