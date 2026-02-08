#include "execution/order_throttle.h"

#include <algorithm>

namespace ai_trade {

bool OrderThrottle::Allow(const OrderIntent& intent,
                          std::int64_t now_ms,
                          std::int64_t tick_index,
                          std::string* out_reason) const {
  if (intent.symbol.empty() || intent.direction == 0) {
    if (out_reason != nullptr) {
      *out_reason = "invalid_intent";
    }
    return false;
  }

  const auto it = state_by_symbol_.find(intent.symbol);
  if (it == state_by_symbol_.end()) {
    return true;
  }
  const SymbolState& state = it->second;

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
      return false;
    }
  }

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
      return false;
    }
  }

  return true;
}

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

}  // namespace ai_trade

