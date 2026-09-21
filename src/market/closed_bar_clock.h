#pragma once

#include <optional>
#include <unordered_map>

#include "core/types.h"

namespace ai_trade {

// One clock shared by tick-fed and explicit-OHLC MVP runs. Explicit bars use
// end timestamps; raw ticks use observation timestamps. Never flush a partial
// bar at EOF or use the next bucket's first price as the previous close.
class ClosedBarClock {
 public:
  static constexpr std::int64_t kIntervalMs = 300000;
  std::optional<MarketEvent> Push(const MarketEvent& event);

 private:
  struct State {
    std::int64_t last_ts{-1};
    std::int64_t last_closed_end{-1};
    std::int64_t bucket{-1};
    bool full_bucket{false};
    bool explicit_bars{false};
    bool ticks{false};
    MarketEvent bar;
  };
  std::unordered_map<std::string, State> states_;
};
}  // namespace ai_trade
