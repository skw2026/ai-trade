#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "exchange/exchange_adapter.h"

namespace ai_trade {

class MockExchangeAdapter : public ExchangeAdapter {
 public:
  explicit MockExchangeAdapter(
      std::vector<double> prices,
      std::vector<std::string> symbols = std::vector<std::string>{"BTCUSDT"})
      : prices_(std::move(prices)), symbols_(std::move(symbols)) {}

  explicit MockExchangeAdapter(std::vector<double> prices, std::string symbol)
      : prices_(std::move(prices)), symbols_(std::vector<std::string>{std::move(symbol)}) {}

  std::string Name() const override { return "mock"; }
  bool Connect() override;
  bool PollMarket(MarketEvent* out_event) override;
  bool SubmitOrder(const OrderIntent& intent) override;
  bool CancelOrder(const std::string& client_order_id) override;
  bool PollFill(FillEvent* out_fill) override;
  bool GetRemoteNotionalUsd(double* out_notional_usd) const override;
  bool GetAccountSnapshot(ExchangeAccountSnapshot* out_snapshot) const override;
  bool GetRemotePositions(
      std::vector<RemotePositionSnapshot>* out_positions) const override;
  bool GetSymbolInfo(const std::string& symbol,
                     SymbolInfo* out_info) const override;
  bool TradeOk() const override { return connected_; }

 private:
  std::vector<double> prices_;
  std::vector<std::string> symbols_;
  std::size_t cursor_{0};
  std::size_t symbol_cursor_{0};
  std::int64_t seq_{0};
  std::uint64_t fill_seq_{0};
  bool connected_{false};
  std::unordered_map<std::string, double> last_price_by_symbol_;
  std::unordered_map<std::string, double> remote_position_qty_by_symbol_;
  std::deque<FillEvent> pending_fills_;
};

}  // namespace ai_trade
