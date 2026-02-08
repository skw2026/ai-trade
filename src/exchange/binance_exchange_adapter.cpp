#include "exchange/binance_exchange_adapter.h"

#include <cstdlib>

#include "core/log.h"

namespace ai_trade {

bool BinanceExchangeAdapter::Connect() {
  const char* api_key = std::getenv("AI_TRADE_API_KEY");
  const char* api_secret = std::getenv("AI_TRADE_API_SECRET");

  if (api_key == nullptr || api_secret == nullptr) {
    LogInfo("binance_stub 连接失败：缺少 AI_TRADE_API_KEY/AI_TRADE_API_SECRET");
    connected_ = false;
    return false;
  }

  connected_ = true;
  LogInfo("binance_stub 已连接（占位实现，尚未接入真实网络）");
  return true;
}

bool BinanceExchangeAdapter::PollMarket(MarketEvent* out_event) {
  (void)out_event;
  // 占位实现：真实接入时改为 WS 拉流。
  return false;
}

bool BinanceExchangeAdapter::SubmitOrder(const OrderIntent& intent) {
  (void)intent;
  // 占位实现：真实接入时改为 REST 下单。
  if (!connected_) {
    return false;
  }
  LogInfo("binance_stub SubmitOrder 已调用（占位）");
  return true;
}

bool BinanceExchangeAdapter::CancelOrder(const std::string& client_order_id) {
  (void)client_order_id;
  if (!connected_) {
    return false;
  }
  LogInfo("binance_stub CancelOrder 已调用（占位）");
  return true;
}

bool BinanceExchangeAdapter::PollFill(FillEvent* out_fill) {
  (void)out_fill;
  // 占位实现：真实接入时改为 WS 成交回报。
  return false;
}

bool BinanceExchangeAdapter::GetRemoteNotionalUsd(
    double* out_notional_usd) const {
  (void)out_notional_usd;
  // 占位实现：真实接入时改为 REST 查询账户持仓并换算USD名义值。
  return false;
}

bool BinanceExchangeAdapter::GetAccountSnapshot(
    ExchangeAccountSnapshot* out_snapshot) const {
  (void)out_snapshot;
  // 占位实现：真实接入时改为账户接口查询。
  return false;
}

bool BinanceExchangeAdapter::GetRemotePositions(
    std::vector<RemotePositionSnapshot>* out_positions) const {
  (void)out_positions;
  // 占位实现：真实接入时改为持仓接口查询。
  return false;
}

bool BinanceExchangeAdapter::GetSymbolInfo(const std::string& symbol,
                                           SymbolInfo* out_info) const {
  (void)symbol;
  (void)out_info;
  // 占位实现：真实接入时改为 exchangeInfo 查询。
  return false;
}

}  // namespace ai_trade
