#include "market/closed_bar_clock.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace ai_trade {
std::optional<MarketEvent> ClosedBarClock::Push(const MarketEvent& event) {
  if (event.ts_ms <= 0 || event.symbol.empty() ||
      !std::isfinite(event.price) || event.price <= 0.0 ||
      !std::isfinite(event.volume) || event.volume < 0.0) {
    throw std::invalid_argument("MVP clock: invalid observation");
  }
  auto& state = states_[event.symbol];
  if (event.ts_ms < state.last_ts)
    throw std::invalid_argument("MVP clock: out-of-order observation");
  state.last_ts = event.ts_ms;
  if (event.execution_only) return std::nullopt;
  if (event.completed_bar) {
    if (state.ticks || event.interval_ms != kIntervalMs ||
        event.ts_ms % kIntervalMs != 0 ||
        (state.last_closed_end >= 0 &&
         event.ts_ms != state.last_closed_end + kIntervalMs) ||
        !std::isfinite(event.open_price) || event.open_price <= 0 ||
        !std::isfinite(event.high_price) || !std::isfinite(event.low_price) ||
        event.low_price <= 0 ||
        event.high_price < std::max(event.open_price, event.price) ||
        event.low_price > std::min(event.open_price, event.price)) {
      throw std::invalid_argument("MVP clock: invalid/discontinuous closed bar");
    }
    state.explicit_bars = true;
    state.last_closed_end = event.ts_ms;
    return event;
  }
  if (state.explicit_bars || std::isfinite(event.open_price) ||
      std::isfinite(event.high_price) || std::isfinite(event.low_price)) {
    throw std::invalid_argument("MVP clock: mixed or unconfirmed OHLC input");
  }
  state.ticks = true;
  const auto bucket = event.ts_ms / kIntervalMs;
  std::optional<MarketEvent> closed;
  if (bucket != state.bucket) {
    const bool startup_bucket = state.bucket < 0;
    if (state.bucket >= 0 && bucket != state.bucket + 1)
      throw std::invalid_argument("MVP clock: missing tick bucket");
    if (state.bucket >= 0 && state.full_bucket) {
      closed = state.bar;
      closed->ts_ms = bucket * kIntervalMs;
      closed->interval_ms = kIntervalMs;
      closed->completed_bar = true;
      state.last_closed_end = closed->ts_ms;
    }
    state.bucket = bucket;
    // Do not claim the first, partly observed startup bucket is a full bar.
    state.full_bucket = !startup_bucket || event.ts_ms % kIntervalMs == 0;
    state.bar = event;
    state.bar.open_price = event.price;
    state.bar.high_price = event.price;
    state.bar.low_price = event.price;
    state.bar.funding_rate_per_interval = 0.0;
  } else {
    state.bar.price = event.price;
    state.bar.mark_price = event.mark_price;
    state.bar.high_price = std::max(state.bar.high_price, event.price);
    state.bar.low_price = std::min(state.bar.low_price, event.price);
    state.bar.volume += event.volume;
  }
  return closed;
}
}  // namespace ai_trade
