#include "exchange/binance_exchange_adapter.h"

#include <cstdlib>

#include "core/log.h"

namespace ai_trade {

bool BinanceExchangeAdapter::Connect() {
  // 复用通用环境变量，保持与现有运行脚本兼容。
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
  // 占位实现：真实接入时替换为 Public WS 行情。
  return false;
}

bool BinanceExchangeAdapter::SubmitOrder(const OrderIntent& intent) {
  (void)intent;
  // 占位实现：真实接入时替换为 Private REST 下单。
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
  // 占位实现：真实接入时替换为 Private WS 成交回报。
  return false;
}

bool BinanceExchangeAdapter::GetRemoteNotionalUsd(
    double* out_notional_usd) const {
  (void)out_notional_usd;
  // 占位实现：真实接入时改为 REST 查询账户持仓并换算 USD 净名义敞口。
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

bool BinanceExchangeAdapter::GetRemoteAccountBalance(
    RemoteAccountBalanceSnapshot* out_balance) const {
  (void)out_balance;
  // 占位实现：真实接入时改为账户余额接口查询。
  return false;
}

bool BinanceExchangeAdapter::GetRemoteOpenOrderClientIds(
    std::unordered_set<std::string>* out_client_order_ids) const {
  (void)out_client_order_ids;
  // 占位实现：真实接入时改为 open orders 接口查询。
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
