#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "exchange/exchange_adapter.h"

namespace ai_trade {

/**
 * @brief 本地模拟交易所适配器
 *
 * 特性：
 * 1. 使用预置价格序列生成行情；
 * 2. 下单后本地生成成交回报（含部分成交）；
 * 3. 维护“远端仓位视图”以支持对账流程测试。
 *
 * 场景：
 * - 单元测试、回放验证、无外网开发环境。
 */
class MockExchangeAdapter : public ExchangeAdapter {
 public:
  explicit MockExchangeAdapter(
      std::vector<double> prices,
      std::vector<std::string> symbols = std::vector<std::string>{"BTCUSDT"})
      : prices_(std::move(prices)), symbols_(std::move(symbols)) {}

  explicit MockExchangeAdapter(std::vector<double> prices, std::string symbol)
      : prices_(std::move(prices)), symbols_(std::vector<std::string>{std::move(symbol)}) {}

  std::string Name() const override { return "mock"; }
  /// 初始化 mock 连接状态。
  bool Connect() override;
  /// 按价格序列与 symbol 轮询产出行情。
  bool PollMarket(MarketEvent* out_event) override;
  /// 本地模拟下单并入队成交回报。
  bool SubmitOrder(const OrderIntent& intent) override;
  /// 本地撤单（移除 pending 成交）。
  bool CancelOrder(const std::string& client_order_id) override;
  /// 轮询本地成交回报。
  bool PollFill(FillEvent* out_fill) override;
  /// 计算 mock 远端净名义敞口（USD, signed）。
  bool GetRemoteNotionalUsd(double* out_notional_usd) const override;
  /// 返回 mock 账户模式快照。
  bool GetAccountSnapshot(ExchangeAccountSnapshot* out_snapshot) const override;
  /// 返回 mock 远端持仓快照。
  bool GetRemotePositions(
      std::vector<RemotePositionSnapshot>* out_positions) const override;
  /// 返回 mock 账户资金快照（用于本地回归口径对齐）。
  bool GetRemoteAccountBalance(
      RemoteAccountBalanceSnapshot* out_balance) const override;
  /// 返回 mock 远端活动订单ID集合（由 pending fill 队列推导）。
  bool GetRemoteOpenOrderClientIds(
      std::unordered_set<std::string>* out_client_order_ids) const override;
  /// 返回 mock symbol 规则信息。
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
  std::unordered_map<std::string, std::int64_t> last_market_ts_ms_by_symbol_;
  std::unordered_map<std::string, double> remote_position_qty_by_symbol_;
  std::deque<FillEvent> pending_fills_;
};

}  // namespace ai_trade
