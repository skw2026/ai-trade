#pragma once

#include <optional>
#include <string>
#include <vector>

#include "core/types.h"
#include "oms/account_state.h"
#include "oms/order_manager.h"

namespace ai_trade {

/// 对账结果：`ok=false` 表示本地净名义敞口与参考值偏差超出容差。
struct ReconcileResult {
  bool ok{true};
  double delta_notional_usd{0.0};  ///< 本地净名义敞口 - 参考净名义敞口。
  std::string reason_code;
};

/**
 * @brief 本地对账器
 *
 * 策略：
 * 1. 优先使用交易所侧快照做强对账；
 * 2. 无远端快照时退化为 OMS 与 AccountState 的自一致性检查。
 */
class Reconciler {
 public:
  explicit Reconciler(double tolerance_notional_usd)
      : tolerance_notional_usd_(tolerance_notional_usd) {}

  /// 执行一次对账检查（默认对比净名义敞口）。
  ReconcileResult Check(const AccountState& account,
                        const OrderManager& orders,
                        std::optional<double> remote_notional_usd =
                            std::nullopt) const;

  /// 将远端持仓快照换算为净名义敞口（USD, signed sum）。
  double ComputeRemoteNotionalUsd(
      const std::vector<RemotePositionSnapshot>& remote_positions) const;
  /// 生成 symbol 级差异报告（用于异常排查）。
  std::string BuildSymbolDeltaReport(
      const AccountState& account,
      const std::vector<RemotePositionSnapshot>& remote_positions,
      const std::vector<std::string>& tracked_symbols,
      double min_abs_notional_delta_usd = 1.0) const;

 private:
  double tolerance_notional_usd_{5.0};  ///< 允许的净名义敞口偏差容差（USD）。
};

}  // namespace ai_trade
