#pragma once

#include <string>
#include <unordered_map>
#include <vector>

#include "core/types.h"

namespace ai_trade {

/// 单个 symbol 的本地持仓状态。
struct PositionState {
  double qty{0.0};
  double avg_entry_price{0.0};
  double mark_price{0.0};
  double liquidation_price{0.0};  ///< 强平价格；<=0 表示未知/不可用。
};

/**
 * @brief 账户状态聚合
 *
 * 负责：
 * 1. 维护多 symbol 持仓视图；
 * 2. 维护现金、权益与回撤；
 * 3. 由行情/成交增量更新状态。
 */
class AccountState {
 public:
  /// 行情驱动：更新 mark price 并刷新权益峰值。
  void OnMarket(const MarketEvent& event);
  /// 成交驱动：更新仓位、均价、现金和权益。
  void ApplyFill(const FillEvent& fill);
  /// 启动同步：用交易所持仓重建本地仓位视图。
  void SyncFromRemotePositions(const std::vector<RemotePositionSnapshot>& positions,
                               double baseline_cash_usd = 10000.0);
  /**
   * @brief 运行时风险字段刷新（不重置现金基线）
   *
   * 用途：
   * 1. 周期拉取远端持仓后，刷新 `liquidation_price` 与 `mark_price`；
   * 2. 当本地无该 symbol 持仓但远端存在仓位时，补录远端仓位用于风险评估；
   *
   * 不做的事：
   * - 不重置 `cash_usd_` / `peak_equity_usd_`；
   * - 不清空本地仓位表（避免远端瞬时空响应导致本地状态抖动）。
   */
  void RefreshRiskFromRemotePositions(
      const std::vector<RemotePositionSnapshot>& positions);
  /**
   * @brief 强制按远端持仓快照重建本地仓位（运行时自愈）
   *
   * 语义：
   * - 仅重建 `positions_`；
   * - 保留 `cash_usd_` 与 `peak_equity_usd_`；
   * - 用于“本地成交链路疑似漏回报”后的远端权威重对齐。
   */
  void ForceSyncPositionsFromRemote(
      const std::vector<RemotePositionSnapshot>& positions);
  /**
   * @brief 同步远端账户资金口径（equity/wallet/upnl）
   *
   * - 用于修正本地 `cash_usd_` 基线，避免固定初始值导致回撤口径失真；
   * - `reset_peak_to_equity=true` 仅建议在启动同步时使用；
   * - 运行中刷新建议使用 `reset_peak_to_equity=false`，只上调峰值不回退。
   */
  void SyncFromRemoteAccountBalance(
      const RemoteAccountBalanceSnapshot& balance,
      bool reset_peak_to_equity);

  /// 账户净名义敞口（USD, signed sum）。
  double current_notional_usd() const;
  /// 账户总名义敞口（USD, gross）：`sum(abs(symbol_notional))`。
  double gross_notional_usd() const;
  /// 单 symbol 净名义敞口（USD, signed）。
  double current_notional_usd(const std::string& symbol) const;
  /// 当前账户权益（USD）。
  double equity_usd() const;
  /// 累计已实现盈亏（未扣手续费，USD）。
  double cumulative_realized_pnl_usd() const;
  /// 累计手续费（USD）。
  double cumulative_fee_usd() const;
  /// 累计已实现净盈亏（已实现盈亏 - 手续费，USD）。
  double cumulative_realized_net_pnl_usd() const;
  /// 当前回撤比例（相对权益峰值）。
  double drawdown_pct() const;
  /// 账户级强平距离加权 P95（0~1）；无可用样本时返回 1.0。
  double liquidation_distance_p95() const;
  double mark_price() const { return mark_price("BTCUSDT"); }
  /// 单 symbol 标记价格。
  double mark_price(const std::string& symbol) const;
  double position_qty() const { return position_qty("BTCUSDT"); }
  /// 单 symbol 持仓数量（带方向）。
  double position_qty(const std::string& symbol) const;
  /// 当前持仓 symbol 列表。
  std::vector<std::string> symbols() const;

 private:
  void RefreshPeakEquity();
  double UnrealizedPnlUsd() const;
  static double EffectiveMarkPrice(const PositionState& position);

  double cash_usd_{10000.0};  ///< 账户现金（已包含已实现盈亏与手续费扣减）。
  double peak_equity_usd_{10000.0};  ///< 历史权益峰值（用于回撤计算）。
  double cumulative_realized_pnl_usd_{0.0};  ///< 成交累计已实现盈亏（不含手续费）。
  double cumulative_fee_usd_{0.0};  ///< 成交累计手续费。
  std::unordered_map<std::string, PositionState> positions_;  ///< 多 symbol 持仓视图。
};

}  // namespace ai_trade
