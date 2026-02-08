#include "oms/reconciler.h"

#include <cmath>

namespace ai_trade {

ReconcileResult Reconciler::Check(const AccountState& account,
                                  const OrderManager& orders,
                                  std::optional<double> remote_notional_usd) const {
  ReconcileResult result;
  double expected_notional = 0.0;
  if (remote_notional_usd.has_value()) {
    expected_notional = *remote_notional_usd;
  } else {
    for (const auto& [symbol, qty] : orders.net_filled_qty_by_symbol()) {
      expected_notional += qty * account.mark_price(symbol);
    }
  }
  const double actual_notional = account.current_notional_usd();
  result.delta_notional_usd = actual_notional - expected_notional;
  result.ok =
      std::fabs(result.delta_notional_usd) <= tolerance_notional_usd_;
  if (!result.ok) {
    result.reason_code = "OMS_RECONCILE_MISMATCH";
  }
  return result;
}

}  // namespace ai_trade
