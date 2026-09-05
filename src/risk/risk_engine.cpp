#include "risk/risk_engine.h"

#include <algorithm>
#include <cmath>

namespace ai_trade {

/**
 * @brief 风险状态机判定
 *
 * 优先级从高到低：
 * 1. 回撤熔断 / 冷却（目标归零）；
 * 2. 通道异常 / 未知风险 / 强平距离不足 / 强制只减仓；
 * 3. 回撤降档 / 正常。回撤滞回独立保留。
 */
RiskMode RiskEngine::ResolveMode(bool trade_ok,
                                 double drawdown_pct,
                                 double liq_distance_pct) {
  const bool valid_drawdown = std::isfinite(drawdown_pct) && drawdown_pct >= 0.0;
  if (valid_drawdown) {
    if (drawdown_pct >= thresholds_.fuse_drawdown ||
        (drawdown_mode_ == RiskMode::kFuse &&
         drawdown_pct >= thresholds_.fuse_recover_drawdown)) {
      drawdown_mode_ = RiskMode::kFuse;
    } else if (drawdown_pct >= thresholds_.cooldown_drawdown ||
               (drawdown_mode_ == RiskMode::kCooldown &&
                drawdown_pct >= thresholds_.cooldown_recover_drawdown)) {
      drawdown_mode_ = RiskMode::kCooldown;
    } else if (drawdown_pct >= thresholds_.degraded_drawdown ||
               (drawdown_mode_ == RiskMode::kDegraded &&
                drawdown_pct >= thresholds_.degraded_recover_drawdown)) {
      drawdown_mode_ = RiskMode::kDegraded;
    } else {
      drawdown_mode_ = RiskMode::kNormal;
    }
  }
  if (drawdown_mode_ == RiskMode::kFuse ||
      drawdown_mode_ == RiskMode::kCooldown) {
    return drawdown_mode_;
  }
  if (!trade_ok || forced_reduce_only_ || !valid_drawdown ||
      !std::isfinite(liq_distance_pct) ||
      liq_distance_pct < thresholds_.min_liquidation_distance) {
    return RiskMode::kReduceOnly;
  }
  return drawdown_mode_;
}

/**
 * @brief 对策略目标仓位应用风控修正
 *
 * 输出语义：
 * - adjusted_notional_usd: 允许的最终净名义敞口（single-symbol, signed）；
 * - reduce_only: 是否只允许减仓（reduce-only）；
 * - risk_mode: 当前风险状态。
 */
RiskAdjustedPosition RiskEngine::Apply(const TargetPosition& target,
                                       bool trade_ok,
                                       double drawdown_pct,
                                       double liq_distance_pct) {
  RiskAdjustedPosition out;
  out.symbol = target.symbol;
  mode_ = ResolveMode(trade_ok, drawdown_pct, liq_distance_pct);
  out.risk_mode = mode_;
  out.reduce_only = (mode_ == RiskMode::kReduceOnly ||
                     mode_ == RiskMode::kCooldown ||
                     mode_ == RiskMode::kFuse);

  // 基础限额：先做净名义敞口绝对值裁剪（单 symbol 维度）。
  const double capped = std::clamp(target.target_notional_usd,
                                   -max_abs_notional_usd_,
                                   max_abs_notional_usd_);

  // 熔断/冷却：目标仓位强制归零，仅允许收敛风险。
  if (mode_ == RiskMode::kFuse || mode_ == RiskMode::kCooldown) {
    out.adjusted_notional_usd = 0.0;
    return out;
  }
  
  // 降级：允许交易但降杠杆，目标仓位打 5 折。
  if (mode_ == RiskMode::kDegraded) {
    out.adjusted_notional_usd = 0.5 * capped;
    return out;
  }

  out.adjusted_notional_usd = capped;
  return out;
}

}  // namespace ai_trade
