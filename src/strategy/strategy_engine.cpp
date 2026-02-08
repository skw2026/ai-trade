#include "strategy/strategy_engine.h"

#include <cmath>

namespace ai_trade {

Signal StrategyEngine::OnMarket(const MarketEvent& event) {
  SymbolState& state = symbol_state_[event.symbol];
  if (!state.has_last) {
    state.has_last = true;
    state.last_price = event.price;
    Signal warmup_signal;
    warmup_signal.symbol = event.symbol;
    return warmup_signal;
  }

  const double delta = event.price - state.last_price;
  state.last_price = event.price;

  Signal signal;
  signal.symbol = event.symbol;
  if (std::fabs(delta) < 0.1) {
    signal.suggested_notional_usd = 0.0;
    signal.direction = 0;
    return signal;
  }

  signal.direction = (delta > 0.0) ? 1 : -1;
  signal.suggested_notional_usd = signal.direction * 1000.0;
  return signal;
}

}  // namespace ai_trade
