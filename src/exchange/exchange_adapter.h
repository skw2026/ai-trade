#pragma once

#include <string>
#include <unordered_set>
#include <vector>

#include "core/types.h"

namespace ai_trade {

/**
 * @brief 交易所适配层统一接口
 *
 * 该接口用于屏蔽 mock/replay/paper/live 差异，让上层业务只依赖统一语义：
 * - 行情输入（PollMarket）
 * - 订单执行（SubmitOrder/CancelOrder）
 * - 成交回报（PollFill）
 * - 账户一致性与健康信息（GetRemote* / TradeOk）
 */
class ExchangeAdapter {
 public:
  virtual ~ExchangeAdapter() = default;

  /// @brief 适配器名称（用于日志与运行态诊断）。
  virtual std::string Name() const = 0;
  /// @brief 建立连接并完成必要初始化。
  virtual bool Connect() = 0;

  /// @brief 拉取一条行情；返回 false 表示当前无可用行情。
  virtual bool PollMarket(MarketEvent* out_event) = 0;

  /// @brief 提交订单意图到交易所。
  virtual bool SubmitOrder(const OrderIntent& intent) = 0;

  /// @brief 按 client_order_id 撤单。
  virtual bool CancelOrder(const std::string& client_order_id) = 0;

  /// @brief 拉取成交回报；返回 false 表示当前无成交。
  virtual bool PollFill(FillEvent* out_fill) = 0;

  /// @brief 获取交易所侧当前净名义敞口（USD, signed）；返回 false 表示当前不可用。
  virtual bool GetRemoteNotionalUsd(double* out_notional_usd) const = 0;

  /// @brief 获取交易所账户模式快照（用于启动门禁与运行时校验）。
  virtual bool GetAccountSnapshot(ExchangeAccountSnapshot* out_snapshot) const = 0;

  /// @brief 获取交易所远端持仓快照（用于启动时账户状态对齐）。
  virtual bool GetRemotePositions(
      std::vector<RemotePositionSnapshot>* out_positions) const = 0;

  /// @brief 获取交易所账户资金快照（用于现金基线与回撤口径同步）。
  virtual bool GetRemoteAccountBalance(
      RemoteAccountBalanceSnapshot* out_balance) const = 0;

  /**
   * @brief 获取远端“当前仍处于活动状态”的 client_order_id 集合
   *
   * 用途：
   * - 对账阶段识别“本地仍认为在途，但远端已无该订单”的陈旧订单；
   * - 触发本地状态快速收敛，减少 `RECONCILE_DEFERRED` 长时间阻塞。
   */
  virtual bool GetRemoteOpenOrderClientIds(
      std::unordered_set<std::string>* out_client_order_ids) const = 0;

  /// @brief 获取 symbol 级交易规则与可交易状态（用于筛币与下单前校验）。
  virtual bool GetSymbolInfo(const std::string& symbol,
                             SymbolInfo* out_info) const = 0;

  /// @brief 交易通道健康状态（映射到 `trade_ok`）。
  virtual bool TradeOk() const = 0;
};

}  // namespace ai_trade
