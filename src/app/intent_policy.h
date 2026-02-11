#pragma once

#include <cmath>

#include "core/types.h"

namespace ai_trade {

/**
 * @brief 判断订单意图是否属于“开仓意图”
 *
 * 语义约束：
 * 1. 仅 `kEntry` 且非 `reduce_only` 才视为开仓；
 * 2. `kReduce/kSl/kTp` 以及所有 `reduce_only=true` 都视为风险收敛路径。
 */
inline bool IsOpeningIntent(const OrderIntent& intent) {
  return intent.purpose == OrderPurpose::kEntry && !intent.reduce_only;
}

/**
 * @brief Universe 非活跃过滤规则
 *
 * 规则：仅拦截开仓意图；减仓/保护单必须放行，确保风险可收敛。
 */
inline bool ShouldFilterInactiveSymbolIntent(const OrderIntent& intent) {
  return IsOpeningIntent(intent);
}

/**
 * @brief 是否应跳过 inactive symbol 的整条决策链
 *
 * 仅在以下条件同时满足时返回 true：
 * 1. 当前 symbol 不在 active universe；
 * 2. 本地该 symbol 无持仓；
 * 3. 本地该 symbol 无在途净仓位订单。
 *
 * 目的：减少 inactive symbol 的无效决策与日志噪音。
 */
inline bool ShouldSkipInactiveSymbolDecision(
    bool is_symbol_active,
    double current_symbol_notional_usd,
    bool has_pending_symbol_net_orders) {
  return !is_symbol_active &&
         std::fabs(current_symbol_notional_usd) <= 1e-9 &&
         !has_pending_symbol_net_orders;
}

}  // namespace ai_trade
