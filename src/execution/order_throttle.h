#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>

#include "core/types.h"

namespace ai_trade {

struct OrderThrottleConfig {
  int min_order_interval_ms{0};
  int reverse_signal_cooldown_ticks{0};
};

// 下单节流器：用于限制高频重复下单与方向抖动。
class OrderThrottle {
 public:
  explicit OrderThrottle(OrderThrottleConfig config)
      : config_(config) {}

  bool Allow(const OrderIntent& intent,
             std::int64_t now_ms,
             std::int64_t tick_index,
             std::string* out_reason) const;

  void OnAccepted(const OrderIntent& intent,
                  std::int64_t now_ms,
                  std::int64_t tick_index);

 private:
  struct SymbolState {
    std::int64_t last_submit_ms{0};
    std::int64_t last_submit_tick{-1};
    int last_entry_direction{0};
  };

  OrderThrottleConfig config_;
  std::unordered_map<std::string, SymbolState> state_by_symbol_;
};

}  // namespace ai_trade

