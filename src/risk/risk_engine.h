#pragma once

#include "core/types.h"

namespace ai_trade {

class RiskEngine {
 public:
  explicit RiskEngine(double max_abs_notional_usd,
                      RiskThresholds thresholds = {})
      : max_abs_notional_usd_(max_abs_notional_usd),
        thresholds_(thresholds) {}

  RiskAdjustedPosition Apply(const TargetPosition& target,
                             bool trade_ok,
                             double drawdown_pct);

  void SetForcedReduceOnly(bool enabled) { forced_reduce_only_ = enabled; }
  RiskMode mode() const { return mode_; }

 private:
  RiskMode ResolveMode(bool trade_ok, double drawdown_pct) const;

  double max_abs_notional_usd_{3000.0};
  RiskThresholds thresholds_{};
  RiskMode mode_{RiskMode::kNormal};
  bool forced_reduce_only_{false};
};

}  // namespace ai_trade
