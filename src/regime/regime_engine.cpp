#include "regime/regime_engine.h"

#include <algorithm>
#include <cmath>

namespace ai_trade {

namespace {

constexpr double kEpsilon = 1e-12;

double ClampAlpha(double alpha) {
  return std::clamp(alpha, 1e-6, 1.0);
}

}  // namespace

RegimeBucket RegimeEngine::ToBucket(Regime regime) {
  switch (regime) {
    case Regime::kUptrend:
    case Regime::kDowntrend:
      return RegimeBucket::kTrend;
    case Regime::kRange:
      return RegimeBucket::kRange;
    case Regime::kExtreme:
      return RegimeBucket::kExtreme;
  }
  return RegimeBucket::kRange;
}

RegimeState RegimeEngine::OnMarket(const MarketEvent& event) {
  RegimeState state;
  state.symbol = event.symbol;

  if (!config_.enabled) {
    state.warmup = false;
    return state;
  }

  SymbolState& symbol_state = symbol_state_[event.symbol];
  if (!symbol_state.has_last_price || symbol_state.last_price <= kEpsilon ||
      event.price <= kEpsilon) {
    symbol_state.has_last_price = true;
    symbol_state.last_price = event.price;
    symbol_state.sample_ticks = 1;
    state.warmup = true;
    return state;
  }

  const double instant_return =
      (event.price - symbol_state.last_price) / symbol_state.last_price;
  symbol_state.last_price = event.price;
  ++symbol_state.sample_ticks;

  const double alpha = ClampAlpha(config_.ewma_alpha);
  symbol_state.ewma_return =
      (1.0 - alpha) * symbol_state.ewma_return + alpha * instant_return;
  symbol_state.ewma_abs_return =
      (1.0 - alpha) * symbol_state.ewma_abs_return +
      alpha * std::fabs(instant_return);

  state.instant_return = instant_return;
  state.trend_strength = symbol_state.ewma_return;
  state.volatility_level = symbol_state.ewma_abs_return;
  state.warmup = symbol_state.sample_ticks < std::max(0, config_.warmup_ticks);

  Regime regime = Regime::kRange;
  if (!state.warmup) {
    const bool extreme_by_jump =
        std::fabs(instant_return) >= config_.extreme_threshold;
    const bool extreme_by_vol =
        symbol_state.ewma_abs_return >= config_.volatility_threshold;
    if (extreme_by_jump || extreme_by_vol) {
      regime = Regime::kExtreme;
    } else if (symbol_state.ewma_return >= config_.trend_threshold) {
      regime = Regime::kUptrend;
    } else if (symbol_state.ewma_return <= -config_.trend_threshold) {
      regime = Regime::kDowntrend;
    }
  }

  state.regime = regime;
  state.bucket = ToBucket(regime);
  return state;
}

}  // namespace ai_trade
