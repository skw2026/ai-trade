#pragma once

#include <optional>
#include <string>
#include <unordered_map>

#include "core/types.h"

namespace ai_trade {

enum class OrderState {
  kNew,
  kSent,
  kPartial,
  kFilled,
  kRejected,
  kCancelled,
};

struct OrderRecord {
  OrderIntent intent;
  OrderState state{OrderState::kNew};
  double filled_qty{0.0};
};

class OrderManager {
 public:
  bool RegisterIntent(const OrderIntent& intent);
  void MarkSent(const std::string& client_order_id);
  void MarkRejected(const std::string& client_order_id);
  void MarkCancelled(const std::string& client_order_id);
  void OnFill(const FillEvent& fill);

  const OrderRecord* Find(const std::string& client_order_id) const;
  OrderRecord* FindMutable(const std::string& client_order_id);
  std::optional<std::string> FindOpenProtectiveSibling(
      const std::string& parent_order_id,
      OrderPurpose purpose) const;
  bool HasOpenProtection(const std::string& parent_order_id) const;

  double net_filled_qty() const { return net_filled_qty_; }
  double net_filled_qty(const std::string& symbol) const;
  const std::unordered_map<std::string, double>& net_filled_qty_by_symbol() const {
    return net_filled_qty_by_symbol_;
  }

  static bool IsTerminalState(OrderState state);

 private:
  std::unordered_map<std::string, OrderRecord> orders_;
  double net_filled_qty_{0.0};
  std::unordered_map<std::string, double> net_filled_qty_by_symbol_;
};

}  // namespace ai_trade
