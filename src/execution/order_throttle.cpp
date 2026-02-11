#include "execution/order_throttle.h"

#include <algorithm>

namespace ai_trade {

/**
 * @brief 检查是否允许下单 (Throttle Check)
 * 包含：最小下单间隔、反向信号冷却。
 */
bool OrderThrottle::Allow(const OrderIntent& intent,
                          std::int64_t now_ms,
                          std::int64_t tick_index,
                          std::string* out_reason) {
  auto on_allowed = [this]() {
    ++total_stats_.checks;
    ++total_stats_.allowed;
    ++window_stats_.checks;
    ++window_stats_.allowed;
  };
  auto on_rejected = [this](std::uint64_t OrderThrottleStats::*bucket_total,
                            std::uint64_t OrderThrottleStats::*bucket_window) {
    ++total_stats_.checks;
    ++total_stats_.rejected;
    ++window_stats_.checks;
    ++window_stats_.rejected;
    if (bucket_total != nullptr) {
      ++(total_stats_.*bucket_total);
    }
    if (bucket_window != nullptr) {
      ++(window_stats_.*bucket_window);
    }
  };

  if (intent.symbol.empty() || intent.direction == 0) {
    if (out_reason != nullptr) {
      *out_reason = "invalid_intent";
    }
    on_rejected(&OrderThrottleStats::invalid_rejects,
                &OrderThrottleStats::invalid_rejects);
    return false;
  }

  const auto it = state_by_symbol_.find(intent.symbol);
  if (it == state_by_symbol_.end()) {
    return true;
  }
  const SymbolState& state = it->second;

  // 规则 1: 最小下单间隔 (Min Order Interval)
  if (config_.min_order_interval_ms > 0 &&
      now_ms > 0 &&
      state.last_submit_ms > 0) {
    const std::int64_t elapsed = now_ms - state.last_submit_ms;
    if (elapsed >= 0 &&
        elapsed < static_cast<std::int64_t>(config_.min_order_interval_ms)) {
      if (out_reason != nullptr) {
        const std::int64_t remaining =
            static_cast<std::int64_t>(config_.min_order_interval_ms) - elapsed;
        *out_reason = "min_order_interval_ms_remaining=" +
                      std::to_string(std::max<std::int64_t>(remaining, 0));
      }
      on_rejected(&OrderThrottleStats::interval_rejects,
                  &OrderThrottleStats::interval_rejects);
      return false;
    }
  }

  // 规则 2: 反向信号冷却 (Reverse Signal Cooldown)
  // 防止策略在短时间内频繁反手 (Whipsaw Protection)
  if (config_.reverse_signal_cooldown_ticks > 0 &&
      !intent.reduce_only &&
      state.last_entry_direction != 0 &&
      intent.direction != state.last_entry_direction &&
      state.last_submit_tick >= 0) {
    const std::int64_t elapsed_ticks = tick_index - state.last_submit_tick;
    if (elapsed_ticks >= 0 &&
        elapsed_ticks < static_cast<std::int64_t>(
                            config_.reverse_signal_cooldown_ticks)) {
      if (out_reason != nullptr) {
        const std::int64_t remaining =
            static_cast<std::int64_t>(config_.reverse_signal_cooldown_ticks) -
            elapsed_ticks;
        *out_reason = "reverse_signal_cooldown_ticks_remaining=" +
                      std::to_string(std::max<std::int64_t>(remaining, 0));
      }
      on_rejected(&OrderThrottleStats::reverse_rejects,
                  &OrderThrottleStats::reverse_rejects);
      return false;
    }
  }

  on_allowed();
  return true;
}

// 更新状态：仅在订单被接受（Enqueue 成功）后调用
void OrderThrottle::OnAccepted(const OrderIntent& intent,
                               std::int64_t now_ms,
                               std::int64_t tick_index) {
  if (intent.symbol.empty()) {
    return;
  }
  SymbolState& state = state_by_symbol_[intent.symbol];
  state.last_submit_ms = now_ms;
  state.last_submit_tick = tick_index;
  if (!intent.reduce_only && intent.direction != 0) {
    state.last_entry_direction = intent.direction;
  }
}

OrderThrottleStats OrderThrottle::ConsumeWindowStats() {
  const OrderThrottleStats snapshot = window_stats_;
  window_stats_ = OrderThrottleStats{};
  return snapshot;
}

}  // namespace ai_trade
