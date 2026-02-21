#pragma once

#include "core/types.h"

namespace ai_trade {

/**
 * @brief 风控引擎 (Risk Engine)
 *
 * 系统的"最高法庭"。负责在策略生成目标仓位后，根据账户当前的风险状况（回撤、强平距离等）
 * 进行最终的拦截或修正。
 *
 * 核心机制：
 * 1. 状态机：Normal -> Degraded -> Cooldown -> Fuse
 * 2. 硬约束：净名义敞口上限（单 symbol，signed）
 * 3. 强平保护：基于强平距离的强制减仓
 */
class RiskEngine {
 public:
  explicit RiskEngine(double max_abs_notional_usd,
                      RiskThresholds thresholds = {})
      : max_abs_notional_usd_(max_abs_notional_usd),
        thresholds_(thresholds) {}

  /**
   * @brief 应用风控规则，计算最终允许的持仓
   *
   * @param target 策略建议的目标净名义敞口（single-symbol, signed）
   * @param trade_ok 交易所交易通道是否健康
   * @param drawdown_pct 当前账户回撤百分比 (0.0 ~ 1.0)
   * @param liq_distance_pct 当前强平距离百分比 (0.0 ~ 1.0)
   * @return RiskAdjustedPosition 经风控修正后的目标净名义敞口
   */
  RiskAdjustedPosition Apply(const TargetPosition& target,
                             bool trade_ok,
                             double drawdown_pct,
                             double liq_distance_pct = 1.0);

  void SetForcedReduceOnly(bool enabled) { forced_reduce_only_ = enabled; }
  RiskMode mode() const { return mode_; }

 private:
  RiskMode ResolveMode(bool trade_ok, double drawdown_pct, double liq_distance_pct);

  double max_abs_notional_usd_{3000.0};  ///< 单 symbol 净名义敞口绝对值上限（USD）。
  RiskThresholds thresholds_{};  ///< 回撤与强平距离阈值集合。
  RiskMode mode_{RiskMode::kNormal};  ///< 当前风险状态机模式。
  bool forced_reduce_only_{false};  ///< 外部硬开关：强制只减仓（reduce-only）。
};

}  // namespace ai_trade
