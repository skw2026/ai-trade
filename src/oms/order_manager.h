#pragma once

#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include "core/types.h"

namespace ai_trade {

enum class OrderState {
  kNew,       ///< 已创建，尚未发送。
  kSent,      ///< 已进入执行通道，等待成交/拒绝。
  kPartial,   ///< 部分成交。
  kFilled,    ///< 全部成交（终态）。
  kRejected,  ///< 被拒绝（终态）。
  kCancelled, ///< 已撤销（终态）。
};

/**
 * @brief 订单记录
 */
struct OrderRecord {
  OrderIntent intent;  ///< 原始订单意图快照。
  OrderState state{OrderState::kNew};  ///< 当前状态机状态。
  double filled_qty{0.0};  ///< 累计成交数量。
};

/**
 * @brief 订单管理系统 (OMS)
 *
 * 负责维护全生命周期的订单状态。
 * 1. 记录发出的订单意图 (Intent)
 * 2. 追踪订单状态变化 (New -> Sent -> Filled/Cancelled)
 * 3. 维护净成交数量 (Net Filled Qty) 用于对账
 * 4. 管理保护单 (SL/TP) 的关联关系
 */
class OrderManager {
 public:
  /// 注册新订单意图；`client_order_id` 为空或重复时返回 false。
  bool RegisterIntent(const OrderIntent& intent);
  /// 状态迁移：New -> Sent。
  void MarkSent(const std::string& client_order_id);
  /// 状态迁移：非终态 -> Rejected。
  void MarkRejected(const std::string& client_order_id);
  /// 状态迁移：非终态 -> Cancelled。
  void MarkCancelled(const std::string& client_order_id);
  /// 消费成交回报，推进订单状态并更新净成交统计。
  void OnFill(const FillEvent& fill);

  /**
   * @brief 用远端持仓快照重建净成交基线（仅用于启动同步）
   *
   * 说明：
   * - 该基线用于“远端快照暂不可用”时的弱对账退化口径；
   * - 不会生成订单记录，也不会修改已有订单状态机。
   */
  void SeedNetPositionBaseline(
      const std::vector<RemotePositionSnapshot>& positions);

  const OrderRecord* Find(const std::string& client_order_id) const;
  OrderRecord* FindMutable(const std::string& client_order_id);

  /**
   * @brief 查找同一父订单下的另一侧保护单 (OCO逻辑)
   * 例如：已知 SL 订单，查找对应的 TP 订单。
   */
  std::optional<std::string> FindOpenProtectiveSibling(
      const std::string& parent_order_id,
      OrderPurpose purpose) const;

  /**
   * @brief 检查父订单是否已关联了保护单
   */
  bool HasOpenProtection(const std::string& parent_order_id) const;

  /**
   * @brief 是否存在“在途净仓位订单”（用于对账门控）
   *
   * 定义：
   * - 订单用途为 `kEntry/kReduce`；
   * - 订单状态为非终态（New/Sent/Partial）。
   *
   * 说明：
   * - `kSl/kTp` 保护单通常会长时间挂单，不应阻塞周期对账。
   */
  bool HasPendingNetPositionOrders() const;
  /// 返回“在途净仓位订单”数量，便于日志诊断。
  int PendingNetPositionOrderCount() const;
  /// 返回“在途净仓位订单”ID列表，供上层做超时收敛处理。
  std::vector<std::string> PendingNetPositionOrderIds() const;
  /**
   * @brief 是否存在“同 symbol 同方向”的在途净仓位订单
   *
   * 用途：
   * - 防止短时间重复生成同向净仓位订单，降低 pending 堆积与超时撤单频率。
   */
  bool HasPendingNetPositionOrderForSymbolDirection(const std::string& symbol,
                                                    int direction) const;
  /**
   * @brief 是否存在“同 symbol（任意方向）”的在途净仓位订单
   *
   * 用途：
   * - 在 Universe 场景下，inactive symbol 若仍有在途净仓位订单，不应跳过决策/对账收敛。
   */
  bool HasPendingNetPositionOrderForSymbol(const std::string& symbol) const;

  /// 全局净成交数量（跨 symbol 聚合，signed qty）。
  double net_filled_qty() const { return net_filled_qty_; }
  /// 单 symbol 净成交数量（signed qty）。
  double net_filled_qty(const std::string& symbol) const;
  /// 多 symbol 净成交数量字典（symbol -> signed qty）。
  const std::unordered_map<std::string, double>& net_filled_qty_by_symbol() const {
    return net_filled_qty_by_symbol_;
  }

  /// 是否为终态（Filled/Rejected/Cancelled）。
  static bool IsTerminalState(OrderState state);

 private:
  std::unordered_map<std::string, OrderRecord> orders_;  ///< 订单主表（client_order_id 索引）。
  double net_filled_qty_{0.0};  ///< 全局净成交数量（signed qty）。
  std::unordered_map<std::string, double> net_filled_qty_by_symbol_;  ///< 单 symbol 净成交数量。
};

}  // namespace ai_trade
