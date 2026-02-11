#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>

#include "core/types.h"

namespace ai_trade {

struct OrderThrottleConfig {
  // 同一 symbol 最小下单间隔，0 表示不启用该限制。
  int min_order_interval_ms{0};
  // 开仓方向反转后的冷静期（按行情 tick 计数），0 表示不启用。
  int reverse_signal_cooldown_ticks{0};
};

/// 节流统计快照：用于运行态命中率观测。
struct OrderThrottleStats {
  std::uint64_t checks{0};  // 准入检查总次数。
  std::uint64_t allowed{0};  // 放行次数。
  std::uint64_t rejected{0};  // 拒绝次数。
  std::uint64_t interval_rejects{0};  // 最小间隔命中次数。
  std::uint64_t reverse_rejects{0};  // 反向冷却命中次数。
  std::uint64_t invalid_rejects{0};  // 非法意图命中次数。
};

/**
 * @brief 下单节流器（防抖与限频）
 *
 * 目标：
 * 1. 限制同一 symbol 的最小下单间隔，避免高频重复下单；
 * 2. 限制开仓方向快速反转，降低来回打脸（whipsaw）风险；
 * 3. 仅做“准入判断”，不改变订单内容。
 */
class OrderThrottle {
 public:
  explicit OrderThrottle(OrderThrottleConfig config)
      : config_(config) {}

  /**
   * @brief 判断订单是否可放行
   *
   * @param intent 待检查的订单意图
   * @param now_ms 当前毫秒时间戳
   * @param tick_index 当前行情 tick 序号
   * @param out_reason 拒绝原因（可选输出）
   * @return true 允许下单
   * @return false 触发节流规则，拒绝下单
   */
  bool Allow(const OrderIntent& intent,
             std::int64_t now_ms,
             std::int64_t tick_index,
             std::string* out_reason);

  /**
   * @brief 更新节流状态
   *
   * 仅在订单成功进入执行队列后调用，确保节流统计与实际发单一致。
   */
  void OnAccepted(const OrderIntent& intent,
                  std::int64_t now_ms,
                  std::int64_t tick_index);

  /// 获取累计统计（进程生命周期内单调累加）。
  const OrderThrottleStats& total_stats() const { return total_stats_; }
  /// 获取窗口统计并清零（用于周期状态日志）。
  OrderThrottleStats ConsumeWindowStats();

 private:
  struct SymbolState {
    // 最近一次成功排队下单的时间戳（毫秒）。
    std::int64_t last_submit_ms{0};
    // 最近一次成功排队下单对应的行情 tick 序号。
    std::int64_t last_submit_tick{-1};
    // 最近一次“开仓类”订单方向（1=Buy, -1=Sell）。
    int last_entry_direction{0};
  };

  OrderThrottleConfig config_;
  std::unordered_map<std::string, SymbolState> state_by_symbol_;
  OrderThrottleStats total_stats_;  ///< 全量累计节流统计。
  OrderThrottleStats window_stats_;  ///< 自上次消费以来的窗口统计。
};

}  // namespace ai_trade
