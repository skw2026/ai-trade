#pragma once

#include <string>

#include "exchange/exchange_adapter.h"

namespace ai_trade {

// 实盘接入最小占位：先打通接口与运行路径，后续替换为真实 WS/REST。
class BinanceExchangeAdapter : public ExchangeAdapter {
 public:
  std::string Name() const override { return "binance_stub"; }
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
  bool connected_{false};
};

}  // namespace ai_trade
