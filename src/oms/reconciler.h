#pragma once

#include <optional>
#include <string>

#include "oms/account_state.h"
#include "oms/order_manager.h"

namespace ai_trade {

struct ReconcileResult {
  bool ok{true};
  double delta_notional_usd{0.0};
  std::string reason_code;
};

class Reconciler {
 public:
  explicit Reconciler(double tolerance_notional_usd)
      : tolerance_notional_usd_(tolerance_notional_usd) {}

  ReconcileResult Check(const AccountState& account,
                        const OrderManager& orders,
                        std::optional<double> remote_notional_usd =
                            std::nullopt) const;

 private:
  double tolerance_notional_usd_{5.0};
};

}  // namespace ai_trade
