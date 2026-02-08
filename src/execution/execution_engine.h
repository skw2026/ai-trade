#pragma once

#include <optional>

#include "core/types.h"

namespace ai_trade {

class ExecutionEngine {
 public:
  explicit ExecutionEngine(double max_order_notional_usd)
      : max_order_notional_usd_(max_order_notional_usd) {}

  std::optional<OrderIntent> BuildIntent(const RiskAdjustedPosition& target,
                                         double current_notional_usd,
                                         double price) const;
  std::optional<OrderIntent> BuildProtectionIntent(const FillEvent& entry_fill,
                                                   OrderPurpose purpose,
                                                   double distance_ratio) const;

 private:
  double max_order_notional_usd_{5000.0};
};

}  // namespace ai_trade
