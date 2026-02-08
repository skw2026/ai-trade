#include "execution/execution_engine.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <sstream>

namespace ai_trade {

namespace {

std::string BuildClientOrderId() {
  static std::atomic<std::uint64_t> seq{0};
  static const auto startup_nonce =
      std::chrono::steady_clock::now().time_since_epoch().count();
  const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now());
  const auto ts_ms = now.time_since_epoch().count();

  std::ostringstream oss;
  oss << "sim-" << startup_nonce << "-" << ts_ms << "-"
      << seq.fetch_add(1, std::memory_order_relaxed);
  return oss.str();
}

}  // namespace

std::optional<OrderIntent> ExecutionEngine::BuildIntent(
    const RiskAdjustedPosition& target,
    double current_notional_usd,
    double price) const {
  if (price <= 0.0) {
    return std::nullopt;
  }

  double effective_target = target.adjusted_notional_usd;
  if (target.reduce_only) {
    // reduce_only 语义：只能向 0 方向减仓，禁止加仓和反手开仓。
    if (std::fabs(current_notional_usd) < 1e-6) {
      return std::nullopt;
    }
    if (current_notional_usd > 0.0) {
      effective_target = std::clamp(effective_target, 0.0, current_notional_usd);
    } else {
      effective_target = std::clamp(effective_target, current_notional_usd, 0.0);
    }
  }

  const double delta = effective_target - current_notional_usd;
  if (std::fabs(delta) < 1e-6) {
    return std::nullopt;
  }

  OrderIntent intent;
  intent.client_order_id = BuildClientOrderId();
  intent.symbol = target.symbol;
  intent.reduce_only = target.reduce_only;
  intent.purpose = target.reduce_only ? OrderPurpose::kSl : OrderPurpose::kEntry;
  intent.direction = (delta > 0.0) ? 1 : -1;
  const double order_notional =
      std::min(std::fabs(delta), max_order_notional_usd_);
  intent.qty = order_notional / price;
  intent.price = price;
  return intent;
}

std::optional<OrderIntent> ExecutionEngine::BuildProtectionIntent(
    const FillEvent& entry_fill,
    OrderPurpose purpose,
    double distance_ratio) const {
  if ((purpose != OrderPurpose::kSl && purpose != OrderPurpose::kTp) ||
      distance_ratio <= 0.0 ||
      entry_fill.qty <= 0.0 ||
      entry_fill.price <= 0.0 ||
      entry_fill.direction == 0) {
    return std::nullopt;
  }

  double protection_price = entry_fill.price;
  if (entry_fill.direction > 0) {
    protection_price = (purpose == OrderPurpose::kSl)
                           ? entry_fill.price * (1.0 - distance_ratio)
                           : entry_fill.price * (1.0 + distance_ratio);
  } else {
    protection_price = (purpose == OrderPurpose::kSl)
                           ? entry_fill.price * (1.0 + distance_ratio)
                           : entry_fill.price * (1.0 - distance_ratio);
  }
  if (protection_price <= 0.0) {
    return std::nullopt;
  }

  OrderIntent intent;
  intent.client_order_id = BuildClientOrderId();
  intent.parent_order_id = entry_fill.client_order_id;
  intent.symbol = entry_fill.symbol;
  intent.purpose = purpose;
  intent.reduce_only = true;
  intent.direction = -entry_fill.direction;
  intent.qty = entry_fill.qty;
  intent.price = protection_price;
  return intent;
}

}  // namespace ai_trade
