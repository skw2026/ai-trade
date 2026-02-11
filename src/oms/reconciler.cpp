#include "oms/reconciler.h"

#include <algorithm>
#include <cmath>
#include <sstream>
#include <unordered_map>
#include <unordered_set>

namespace ai_trade {

/**
 * @brief 执行对账检查
 * 比较本地净名义敞口与参考净名义敞口（远端优先）。
 * 
 * @param remote_notional_usd 可选远端净名义敞口。若提供，则以远端为准比对。
 */
ReconcileResult Reconciler::Check(const AccountState& account,
                                  const OrderManager& orders,
                                  std::optional<double> remote_notional_usd) const {
  ReconcileResult result;
  double expected_notional = 0.0;
  // 模式 1: 与交易所远端净名义敞口比对（强对账）。
  if (remote_notional_usd.has_value()) {
    expected_notional = *remote_notional_usd;
  } else {
    // 模式 2: 与本地 OMS 累计净成交量比对（弱对账 / 自一致性）。
    for (const auto& [symbol, qty] : orders.net_filled_qty_by_symbol()) {
      expected_notional += qty * account.mark_price(symbol);
    }
  }
  const double actual_notional = account.current_notional_usd();
  result.delta_notional_usd = actual_notional - expected_notional;
  
  // 允许一定容差，覆盖价格波动/四舍五入导致的微小差异。
  result.ok =
      std::fabs(result.delta_notional_usd) <= tolerance_notional_usd_;
  if (!result.ok) {
    result.reason_code = "OMS_RECONCILE_MISMATCH";
  }
  return result;
}

double Reconciler::ComputeRemoteNotionalUsd(
    const std::vector<RemotePositionSnapshot>& remote_positions) const {
  double total = 0.0;
  for (const auto& position : remote_positions) {
    if (position.symbol.empty() || std::fabs(position.qty) <= 1e-9) {
      continue;
    }
    const double mark = (position.mark_price > 0.0)
                            ? position.mark_price
                            : position.avg_entry_price;
    if (mark <= 0.0) {
      continue;
    }
    total += position.qty * mark;
  }
  return total;
}

/**
 * @brief 生成详细的 Symbol 级别差异报告
 * 用于在对账失败时，快速定位是哪个币对出了问题。
 */
std::string Reconciler::BuildSymbolDeltaReport(
    const AccountState& account,
    const std::vector<RemotePositionSnapshot>& remote_positions,
    const std::vector<std::string>& tracked_symbols,
    double min_abs_notional_delta_usd) const {
  std::unordered_map<std::string, RemotePositionSnapshot> remote_by_symbol;
  for (const auto& position : remote_positions) {
    if (position.symbol.empty()) {
      continue;
    }
    remote_by_symbol[position.symbol] = position;
  }

  std::unordered_set<std::string> symbols;
  for (const auto& symbol : tracked_symbols) {
    if (!symbol.empty()) {
      symbols.insert(symbol);
    }
  }
  for (const auto& symbol : account.symbols()) {
    if (!symbol.empty()) {
      symbols.insert(symbol);
    }
  }
  for (const auto& [symbol, snapshot] : remote_by_symbol) {
    (void)snapshot;
    symbols.insert(symbol);
  }

  std::vector<std::string> ordered_symbols(symbols.begin(), symbols.end());
  std::sort(ordered_symbols.begin(), ordered_symbols.end());

  std::ostringstream oss;
  bool has_delta = false;
  for (const auto& symbol : ordered_symbols) {
    const double local_qty = account.position_qty(symbol);
    const double local_mark = account.mark_price(symbol);
    const double local_notional = account.current_notional_usd(symbol);

    double remote_qty = 0.0;
    double remote_mark = 0.0;
    if (const auto it = remote_by_symbol.find(symbol); it != remote_by_symbol.end()) {
      remote_qty = it->second.qty;
      remote_mark = it->second.mark_price > 0.0
                        ? it->second.mark_price
                        : it->second.avg_entry_price;
    }
    if (remote_mark <= 0.0) {
      remote_mark = local_mark;
    }
    const double remote_notional = remote_qty * remote_mark;

    const double delta_qty = local_qty - remote_qty;
    const double delta_notional = local_notional - remote_notional;
    // 忽略微小差异 (Dust)
    if (std::fabs(delta_qty) <= 1e-9 &&
        std::fabs(delta_notional) < min_abs_notional_delta_usd) {
      continue;
    }

    if (has_delta) {
      oss << ";";
    }
    has_delta = true;
    oss << symbol << "{local_qty=" << local_qty
        << ",remote_qty=" << remote_qty
        << ",delta_qty=" << delta_qty
        << ",local_notional=" << local_notional
        << ",remote_notional=" << remote_notional
        << ",delta_notional=" << delta_notional << "}";
  }

  if (!has_delta) {
    return "none";
  }
  return oss.str();
}

}  // namespace ai_trade
