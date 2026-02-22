#include "exchange/mock_exchange_adapter.h"

#include <cmath>

namespace ai_trade {

bool MockExchangeAdapter::Connect() {
  if (symbols_.empty()) {
    symbols_.push_back("BTCUSDT");
  }
  last_market_ts_ms_by_symbol_.clear();
  connected_ = true;
  return true;
}

bool MockExchangeAdapter::PollMarket(MarketEvent* out_event) {
  if (!connected_ || out_event == nullptr || cursor_ >= prices_.size()) {
    return false;
  }

  ++seq_;
  const double price = prices_[cursor_++];
  // 多 symbol 情况下轮询分发，保证每个 symbol 都能收到行情。
  const std::string& symbol = symbols_[symbol_cursor_ % symbols_.size()];
  ++symbol_cursor_;
  last_price_by_symbol_[symbol] = price;
  constexpr std::int64_t kMockIntervalMs = 5000;
  std::int64_t event_ts_ms = kMockIntervalMs;
  std::int64_t interval_ms = kMockIntervalMs;
  const auto ts_it = last_market_ts_ms_by_symbol_.find(symbol);
  if (ts_it != last_market_ts_ms_by_symbol_.end()) {
    event_ts_ms = ts_it->second + kMockIntervalMs;
    interval_ms = event_ts_ms - ts_it->second;
  }
  last_market_ts_ms_by_symbol_[symbol] = event_ts_ms;
  *out_event = MarketEvent{
      event_ts_ms,
      symbol,
      price,
      price,
      1000.0,
      interval_ms,
  };
  return true;
}

bool MockExchangeAdapter::SubmitOrder(const OrderIntent& intent) {
  if (!connected_) {
    return false;
  }
  // mock 里模拟部分成交：同一 client_order_id 拆成两笔 fill。
  const double first_qty = intent.qty * 0.6;
  const double second_qty = intent.qty - first_qty;

  FillEvent first;
  first.fill_id = intent.client_order_id + "-fill-" + std::to_string(fill_seq_++);
  first.client_order_id = intent.client_order_id;
  first.symbol = intent.symbol;
  first.direction = intent.direction;
  first.qty = first_qty;
  first.price = intent.price;
  pending_fills_.push_back(first);

  FillEvent second;
  second.fill_id = intent.client_order_id + "-fill-" + std::to_string(fill_seq_++);
  second.client_order_id = intent.client_order_id;
  second.symbol = intent.symbol;
  second.direction = intent.direction;
  second.qty = second_qty;
  second.price = intent.price;
  pending_fills_.push_back(second);

  return true;
}

bool MockExchangeAdapter::CancelOrder(const std::string& client_order_id) {
  if (!connected_) {
    return false;
  }

  std::deque<FillEvent> kept;
  kept.resize(0);
  for (const auto& fill : pending_fills_) {
    if (fill.client_order_id != client_order_id) {
      kept.push_back(fill);
    }
  }
  pending_fills_.swap(kept);
  return true;
}

bool MockExchangeAdapter::PollFill(FillEvent* out_fill) {
  if (!connected_ || out_fill == nullptr || pending_fills_.empty()) {
    return false;
  }

  *out_fill = pending_fills_.front();
  pending_fills_.pop_front();
  // 成交回放同时推进“交易所视角”的远端仓位，用于对账测试。
  remote_position_qty_by_symbol_[out_fill->symbol] +=
      static_cast<double>(out_fill->direction) * out_fill->qty;
  return true;
}

bool MockExchangeAdapter::GetRemoteNotionalUsd(double* out_notional_usd) const {
  if (!connected_ || out_notional_usd == nullptr) {
    return false;
  }
  double total_notional = 0.0;
  for (const auto& [symbol, qty] : remote_position_qty_by_symbol_) {
    const auto price_it = last_price_by_symbol_.find(symbol);
    if (price_it == last_price_by_symbol_.end() || price_it->second <= 0.0) {
      continue;
    }
    total_notional += qty * price_it->second;
  }
  *out_notional_usd = total_notional;
  return true;
}

bool MockExchangeAdapter::GetAccountSnapshot(
    ExchangeAccountSnapshot* out_snapshot) const {
  if (!connected_ || out_snapshot == nullptr) {
    return false;
  }
  *out_snapshot = ExchangeAccountSnapshot{
      .account_mode = AccountMode::kUnified,
      .margin_mode = MarginMode::kIsolated,
      .position_mode = PositionMode::kOneWay,
  };
  return true;
}

bool MockExchangeAdapter::GetRemotePositions(
    std::vector<RemotePositionSnapshot>* out_positions) const {
  if (!connected_ || out_positions == nullptr) {
    return false;
  }
  out_positions->clear();
  for (const auto& [symbol, qty] : remote_position_qty_by_symbol_) {
    if (std::fabs(qty) < 1e-9) {
      continue;
    }
    double mark_price = 0.0;
    const auto price_it = last_price_by_symbol_.find(symbol);
    if (price_it != last_price_by_symbol_.end() && price_it->second > 0.0) {
      mark_price = price_it->second;
    }
    out_positions->push_back(RemotePositionSnapshot{
        .symbol = symbol,
        .qty = qty,
        .avg_entry_price = mark_price,
        .mark_price = mark_price,
        .liquidation_price = 0.0,
    });
  }
  return true;
}

bool MockExchangeAdapter::GetRemoteAccountBalance(
    RemoteAccountBalanceSnapshot* out_balance) const {
  if (!connected_ || out_balance == nullptr) {
    return false;
  }
  // mock 模式下使用固定钱包基线，便于本地回归保持可重复性。
  *out_balance = RemoteAccountBalanceSnapshot{
      .equity_usd = 10000.0,
      .wallet_balance_usd = 10000.0,
      .unrealized_pnl_usd = 0.0,
      .has_equity = true,
      .has_wallet_balance = true,
      .has_unrealized_pnl = true,
  };
  return true;
}

bool MockExchangeAdapter::GetRemoteOpenOrderClientIds(
    std::unordered_set<std::string>* out_client_order_ids) const {
  if (!connected_ || out_client_order_ids == nullptr) {
    return false;
  }
  out_client_order_ids->clear();
  for (const auto& fill : pending_fills_) {
    if (fill.client_order_id.empty()) {
      continue;
    }
    out_client_order_ids->insert(fill.client_order_id);
  }
  return true;
}

bool MockExchangeAdapter::GetSymbolInfo(const std::string& symbol,
                                        SymbolInfo* out_info) const {
  if (!connected_ || out_info == nullptr) {
    return false;
  }
  out_info->symbol = symbol;
  out_info->tradable = true;
  out_info->qty_step = 0.001;
  out_info->min_order_qty = 0.001;
  out_info->min_notional_usd = 5.0;
  out_info->price_tick = 0.1;
  out_info->qty_precision = 3;
  out_info->price_precision = 1;
  return true;
}

}  // namespace ai_trade
