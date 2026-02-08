#pragma once

#include <string>
#include <vector>

#include "core/types.h"

namespace ai_trade {

// 交易所适配层统一接口：用于屏蔽 mock/实盘差异。
class ExchangeAdapter {
 public:
  virtual ~ExchangeAdapter() = default;

  virtual std::string Name() const = 0;
  virtual bool Connect() = 0;

  // 拉取一条行情；返回 false 表示当前无可用行情。
  virtual bool PollMarket(MarketEvent* out_event) = 0;

  // 提交订单意图到交易所。
  virtual bool SubmitOrder(const OrderIntent& intent) = 0;

  // 按 client_order_id 撤单。
  virtual bool CancelOrder(const std::string& client_order_id) = 0;

  // 拉取成交回报；返回 false 表示当前无成交。
  virtual bool PollFill(FillEvent* out_fill) = 0;

  // 获取交易所侧当前仓位名义值（USD）；返回 false 表示当前不可用。
  virtual bool GetRemoteNotionalUsd(double* out_notional_usd) const = 0;

  // 获取交易所账户模式快照（用于启动门禁与运行时校验）。
  virtual bool GetAccountSnapshot(ExchangeAccountSnapshot* out_snapshot) const = 0;

  // 获取交易所远端持仓快照（用于启动时账户状态对齐）。
  virtual bool GetRemotePositions(
      std::vector<RemotePositionSnapshot>* out_positions) const = 0;

  // 获取 symbol 级交易规则与可交易状态（用于筛币与下单前校验）。
  virtual bool GetSymbolInfo(const std::string& symbol,
                             SymbolInfo* out_info) const = 0;

  // 交易通道健康状态（映射到 trade_ok）。
  virtual bool TradeOk() const = 0;
};

}  // namespace ai_trade
