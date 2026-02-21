#include "oms/order_manager.h"

#include <algorithm>
#include <cmath>

namespace ai_trade {

namespace {

constexpr double kEpsilon = 1e-9;

bool IsProtection(OrderPurpose purpose) {
  return purpose == OrderPurpose::kSl || purpose == OrderPurpose::kTp;
}

bool IsNetPositionMutating(OrderPurpose purpose) {
  return purpose == OrderPurpose::kEntry || purpose == OrderPurpose::kReduce;
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
  // 订单生命周期入口：先注册再允许异步发送。
  orders_.emplace(intent.client_order_id, OrderRecord{intent});
  return true;
}

// 仅允许 New -> Sent 转移，避免终态订单被错误覆盖。
void OrderManager::MarkSent(const std::string& client_order_id) {
  auto* record = FindMutable(client_order_id);
  if (record == nullptr) {
    return;
  }
  if (record->state == OrderState::kNew) {
    record->state = OrderState::kSent;
  }
}

// Reject 为终态之一；若已终态则忽略重复更新。
void OrderManager::MarkRejected(const std::string& client_order_id) {
  auto* record = FindMutable(client_order_id);
  if (record == nullptr) {
    return;
  }
  if (!IsTerminalState(record->state)) {
    record->state = OrderState::kRejected;
  }
}

// Cancel 为终态之一；若已终态则忽略重复更新。
void OrderManager::MarkCancelled(const std::string& client_order_id) {
  auto* record = FindMutable(client_order_id);
  if (record == nullptr) {
    return;
  }
  if (!IsTerminalState(record->state)) {
    record->state = OrderState::kCancelled;
  }
}

/**
 * @brief 消费成交回报并更新订单状态
 *
 * 规则：
 * 1. 先更新净成交统计（对账依赖）；
 * 2. 再更新对应订单填充量与状态；
 * 3. 终态订单收到重复回报时只保留净成交统计，状态不回滚。
 */
void OrderManager::OnFill(const FillEvent& fill) {
  // 净成交仓位是对账基准之一，先更新聚合统计。
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

void OrderManager::SeedNetPositionBaseline(
    const std::vector<RemotePositionSnapshot>& positions) {
  net_filled_qty_ = 0.0;
  net_filled_qty_by_symbol_.clear();
  for (const auto& position : positions) {
    if (position.symbol.empty() || std::fabs(position.qty) <= kEpsilon) {
      continue;
    }
    net_filled_qty_by_symbol_[position.symbol] += position.qty;
    net_filled_qty_ += position.qty;
  }
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

  // 当前实现 O(n) 扫描；MVP 阶段规模可接受，后续可按 parent_id 建索引优化。
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
  // 只要存在任一未终态保护单即可判定为“已有保护”。
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

bool OrderManager::HasPendingNetPositionOrders() const {
  return PendingNetPositionOrderCount() > 0;
}

int OrderManager::PendingNetPositionOrderCount() const {
  int count = 0;
  for (const auto& [order_id, record] : orders_) {
    (void)order_id;
    if (!IsNetPositionMutating(record.intent.purpose)) {
      continue;
    }
    if (IsTerminalState(record.state)) {
      continue;
    }
    ++count;
  }
  return count;
}

std::vector<std::string> OrderManager::PendingNetPositionOrderIds() const {
  std::vector<std::string> ids;
  for (const auto& [order_id, record] : orders_) {
    if (!IsNetPositionMutating(record.intent.purpose)) {
      continue;
    }
    if (IsTerminalState(record.state)) {
      continue;
    }
    ids.push_back(order_id);
  }
  return ids;
}

bool OrderManager::HasPendingNetPositionOrderForSymbolDirection(
    const std::string& symbol,
    int direction) const {
  return PendingNetPositionOrderCountForSymbolDirection(symbol, direction) > 0;
}

int OrderManager::PendingNetPositionOrderCountForSymbolDirection(
    const std::string& symbol,
    int direction) const {
  if (symbol.empty() || direction == 0) {
    return 0;
  }
  const int normalized_direction = direction > 0 ? 1 : -1;
  int count = 0;
  for (const auto& [order_id, record] : orders_) {
    (void)order_id;
    if (!IsNetPositionMutating(record.intent.purpose)) {
      continue;
    }
    if (IsTerminalState(record.state)) {
      continue;
    }
    if (record.intent.symbol != symbol || record.intent.direction == 0) {
      continue;
    }
    const int pending_direction = record.intent.direction > 0 ? 1 : -1;
    if (pending_direction == normalized_direction) {
      ++count;
    }
  }
  return count;
}

int OrderManager::PendingEntryOrderCountForSymbolDirection(
    const std::string& symbol,
    int direction) const {
  if (symbol.empty() || direction == 0) {
    return 0;
  }
  const int normalized_direction = direction > 0 ? 1 : -1;
  int count = 0;
  for (const auto& [order_id, record] : orders_) {
    (void)order_id;
    if (record.intent.purpose != OrderPurpose::kEntry) {
      continue;
    }
    if (IsTerminalState(record.state)) {
      continue;
    }
    if (record.intent.symbol != symbol || record.intent.direction == 0) {
      continue;
    }
    const int pending_direction = record.intent.direction > 0 ? 1 : -1;
    if (pending_direction == normalized_direction) {
      ++count;
    }
  }
  return count;
}

bool OrderManager::HasPendingNetPositionOrderForSymbol(
    const std::string& symbol) const {
  if (symbol.empty()) {
    return false;
  }
  for (const auto& [order_id, record] : orders_) {
    (void)order_id;
    if (!IsNetPositionMutating(record.intent.purpose)) {
      continue;
    }
    if (IsTerminalState(record.state)) {
      continue;
    }
    if (record.intent.symbol == symbol) {
      return true;
    }
  }
  return false;
}

double OrderManager::PendingNetPositionRemainingQtyForSymbol(
    const std::string& symbol) const {
  if (symbol.empty()) {
    return 0.0;
  }
  double remaining_signed_qty = 0.0;
  for (const auto& [order_id, record] : orders_) {
    (void)order_id;
    if (!IsNetPositionMutating(record.intent.purpose)) {
      continue;
    }
    if (IsTerminalState(record.state)) {
      continue;
    }
    if (record.intent.symbol != symbol || record.intent.direction == 0) {
      continue;
    }
    const double remaining_qty = std::max(0.0, record.intent.qty - record.filled_qty);
    if (remaining_qty <= kEpsilon) {
      continue;
    }
    const double direction = record.intent.direction > 0 ? 1.0 : -1.0;
    remaining_signed_qty += direction * remaining_qty;
  }
  return remaining_signed_qty;
}

}  // namespace ai_trade
