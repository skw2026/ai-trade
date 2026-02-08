#include "risk/risk_engine.h"

#include <algorithm>

namespace ai_trade {

RiskMode RiskEngine::ResolveMode(bool trade_ok, double drawdown_pct) const {
  if (!trade_ok || forced_reduce_only_) {
    return RiskMode::kReduceOnly;
  }
  if (drawdown_pct >= thresholds_.fuse_drawdown) {
    return RiskMode::kFuse;
  }
  if (drawdown_pct >= thresholds_.cooldown_drawdown) {
    return RiskMode::kCooldown;
  }
  if (drawdown_pct >= thresholds_.degraded_drawdown) {
    return RiskMode::kDegraded;
  }
  return RiskMode::kNormal;
}

RiskAdjustedPosition RiskEngine::Apply(const TargetPosition& target,
                                       bool trade_ok,
                                       double drawdown_pct) {
  RiskAdjustedPosition out;
  out.symbol = target.symbol;
  mode_ = ResolveMode(trade_ok, drawdown_pct);
  out.risk_mode = mode_;
  out.reduce_only = (mode_ == RiskMode::kReduceOnly ||
                     mode_ == RiskMode::kCooldown ||
                     mode_ == RiskMode::kFuse);

  const double capped = std::clamp(target.target_notional_usd,
                                   -max_abs_notional_usd_,
                                   max_abs_notional_usd_);

  if (mode_ == RiskMode::kFuse || mode_ == RiskMode::kCooldown) {
    out.adjusted_notional_usd = 0.0;
    return out;
  }
  if (mode_ == RiskMode::kDegraded) {
    out.adjusted_notional_usd = 0.5 * capped;
    return out;
  }

  out.adjusted_notional_usd = capped;
  return out;
}

}  // namespace ai_trade
