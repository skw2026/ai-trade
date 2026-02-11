#pragma once

#include <string>
#include <unordered_set>

#include "exchange/exchange_adapter.h"

namespace ai_trade {

/**
 * @brief Binance 适配器占位实现
 *
 * 用途：
 * 1. 验证系统在多交易所场景下的编译/运行链路；
 * 2. 为后续 Binance 真实接入保留稳定接口。
 *
 * 限制：
 * - 当前不进行真实网络交互，不可用于实盘。
 */
class BinanceExchangeAdapter : public ExchangeAdapter {
 public:
  std::string Name() const override { return "binance_stub"; }
  /// 建立占位连接（检查基础 AK/SK 环境变量）。
  bool Connect() override;
  /// 占位行情轮询（当前固定无行情）。
  bool PollMarket(MarketEvent* out_event) override;
  /// 占位下单接口（仅记录调用）。
  bool SubmitOrder(const OrderIntent& intent) override;
  /// 占位撤单接口（仅记录调用）。
  bool CancelOrder(const std::string& client_order_id) override;
  /// 占位成交轮询（当前固定无成交）。
  bool PollFill(FillEvent* out_fill) override;
  /// 占位远端净名义敞口查询（当前固定失败）。
  bool GetRemoteNotionalUsd(double* out_notional_usd) const override;
  /// 占位账户快照查询（当前固定失败）。
  bool GetAccountSnapshot(ExchangeAccountSnapshot* out_snapshot) const override;
  /// 占位远端持仓查询（当前固定失败）。
  bool GetRemotePositions(
      std::vector<RemotePositionSnapshot>* out_positions) const override;
  /// 占位账户资金查询（当前固定失败）。
  bool GetRemoteAccountBalance(
      RemoteAccountBalanceSnapshot* out_balance) const override;
  /// 占位远端活动订单查询（当前固定失败）。
  bool GetRemoteOpenOrderClientIds(
      std::unordered_set<std::string>* out_client_order_ids) const override;
  /// 占位 symbol 规则查询（当前固定失败）。
  bool GetSymbolInfo(const std::string& symbol,
                     SymbolInfo* out_info) const override;
  bool TradeOk() const override { return connected_; }

 private:
  bool connected_{false};
};

}  // namespace ai_trade
