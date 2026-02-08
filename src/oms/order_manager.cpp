#include "oms/order_manager.h"

#include <cmath>

namespace ai_trade {

namespace {

constexpr double kEpsilon = 1e-9;

bool IsProtection(OrderPurpose purpose) {
  return purpose == OrderPurpose::kSl || purpose == OrderPurpose::kTp;
}

}  // namespace

bool OrderManager::IsTerminalState(OrderState state) {
  return state == OrderState::kFilled ||
         state == OrderState::kRejected ||
         state == OrderState::kCancelled;
}

bool OrderManager::RegisterIntent(const OrderIntent& intent) {
  if (intent.client_order_id.empty()) {
    return false;
  }
  if (orders_.find(intent.client_order_id) != orders_.end()) {
    return false;
  }
  orders_.emplace(intent.client_order_id, OrderRecord{intent});
  return true;
}

void OrderManager::MarkSent(const std::string& client_order_id) {
  auto* record = FindMutable(client_order_id);
  if (record == nullptr) {
    return;
  }
  if (record->state == OrderState::kNew) {
    record->state = OrderState::kSent;
  }
}

void OrderManager::MarkRejected(const std::string& client_order_id) {
  auto* record = FindMutable(client_order_id);
  if (record == nullptr) {
    return;
  }
  if (!IsTerminalState(record->state)) {
    record->state = OrderState::kRejected;
  }
}

void OrderManager::MarkCancelled(const std::string& client_order_id) {
  auto* record = FindMutable(client_order_id);
  if (record == nullptr) {
    return;
  }
  if (!IsTerminalState(record->state)) {
    record->state = OrderState::kCancelled;
  }
}

void OrderManager::OnFill(const FillEvent& fill) {
  const double signed_qty = static_cast<double>(fill.direction) * fill.qty;
  net_filled_qty_ += signed_qty;
  net_filled_qty_by_symbol_[fill.symbol] += signed_qty;

  auto* record = FindMutable(fill.client_order_id);
  if (record == nullptr || IsTerminalState(record->state)) {
    return;
  }

  record->filled_qty += fill.qty;
  if (record->filled_qty + kEpsilon >= record->intent.qty) {
    record->state = OrderState::kFilled;
    return;
  }
  record->state = OrderState::kPartial;
}

double OrderManager::net_filled_qty(const std::string& symbol) const {
  auto it = net_filled_qty_by_symbol_.find(symbol);
  if (it == net_filled_qty_by_symbol_.end()) {
    return 0.0;
  }
  return it->second;
}

const OrderRecord* OrderManager::Find(const std::string& client_order_id) const {
  auto it = orders_.find(client_order_id);
  if (it == orders_.end()) {
    return nullptr;
  }
  return &it->second;
}

OrderRecord* OrderManager::FindMutable(const std::string& client_order_id) {
  auto it = orders_.find(client_order_id);
  if (it == orders_.end()) {
    return nullptr;
  }
  return &it->second;
}

std::optional<std::string> OrderManager::FindOpenProtectiveSibling(
    const std::string& parent_order_id,
    OrderPurpose purpose) const {
  if (!IsProtection(purpose)) {
    return std::nullopt;
  }

  const OrderPurpose sibling_purpose =
      (purpose == OrderPurpose::kSl) ? OrderPurpose::kTp : OrderPurpose::kSl;

  for (const auto& [order_id, record] : orders_) {
    if (record.intent.parent_order_id != parent_order_id) {
      continue;
    }
    if (record.intent.purpose != sibling_purpose) {
      continue;
    }
    if (IsTerminalState(record.state)) {
      continue;
    }
    return order_id;
  }
  return std::nullopt;
}

bool OrderManager::HasOpenProtection(const std::string& parent_order_id) const {
  for (const auto& [order_id, record] : orders_) {
    (void)order_id;
    if (record.intent.parent_order_id != parent_order_id) {
      continue;
    }
    if (!IsProtection(record.intent.purpose)) {
      continue;
    }
    if (IsTerminalState(record.state)) {
      continue;
    }
    return true;
  }
  return false;
}

}  // namespace ai_trade
