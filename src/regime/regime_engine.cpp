#include "regime/regime_engine.h"

#include <algorithm>
#include <cmath>

namespace ai_trade {

namespace {

constexpr double kEpsilon = 1e-12;

double ClampAlpha(double alpha) {
  return std::clamp(alpha, 1e-6, 1.0);
}

double IntervalAwareAlpha(double base_alpha, std::int64_t interval_ms) {
  const double clamped_alpha = ClampAlpha(base_alpha);
  if (interval_ms <= 0) {
    return clamped_alpha;
  }
  // 基准按 5s tick 口径配置；间隔变长时提升等效 alpha，变短时降低。
  constexpr double kReferenceIntervalMs = 5000.0;
  const double ratio =
      std::clamp(static_cast<double>(interval_ms) / kReferenceIntervalMs,
                 0.02, 120.0);
  const double effective_alpha = 1.0 - std::pow(1.0 - clamped_alpha, ratio);
  return std::clamp(effective_alpha, 1e-6, 1.0);
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

  const double alpha = IntervalAwareAlpha(config_.ewma_alpha, event.interval_ms);
  symbol_state.ewma_return =
      (1.0 - alpha) * symbol_state.ewma_return + alpha * instant_return;
  symbol_state.ewma_abs_return =
      (1.0 - alpha) * symbol_state.ewma_abs_return +
      alpha * std::fabs(instant_return);
  const double volume = std::max(0.0, event.volume);
  symbol_state.ewma_volume =
      (1.0 - alpha) * symbol_state.ewma_volume + alpha * volume;

  state.instant_return = instant_return;
  state.trend_strength = symbol_state.ewma_return;
  state.volatility_level = symbol_state.ewma_abs_return;
  state.warmup = symbol_state.sample_ticks < std::max(0, config_.warmup_ticks);

  Regime raw_regime = Regime::kRange;
  if (!state.warmup) {
    const bool extreme_by_jump =
        std::fabs(instant_return) >= config_.extreme_threshold;
    const bool extreme_by_vol =
        symbol_state.ewma_abs_return >= config_.volatility_threshold;
    const bool extreme_by_volume =
        config_.volume_extreme_multiplier > 1.0 &&
        symbol_state.ewma_volume > kEpsilon &&
        volume >= symbol_state.ewma_volume * config_.volume_extreme_multiplier;
    const bool extreme_hit = config_.extreme_requires_both
                                 ? ((extreme_by_jump && extreme_by_vol) ||
                                    extreme_by_volume)
                                 : (extreme_by_jump || extreme_by_vol ||
                                    extreme_by_volume);
    if (extreme_hit) {
      raw_regime = Regime::kExtreme;
    } else if (symbol_state.ewma_return >= config_.trend_threshold) {
      raw_regime = Regime::kUptrend;
    } else if (symbol_state.ewma_return <= -config_.trend_threshold) {
      raw_regime = Regime::kDowntrend;
    }
  }

  Regime regime = Regime::kRange;
  if (state.warmup) {
    regime = Regime::kRange;
  } else {
    const int confirm_ticks = std::max(1, config_.switch_confirm_ticks);
    if (!symbol_state.has_confirmed_regime || confirm_ticks <= 1) {
      symbol_state.has_confirmed_regime = true;
      symbol_state.confirmed_regime = raw_regime;
      symbol_state.pending_regime = raw_regime;
      symbol_state.pending_regime_ticks = 0;
    } else if (raw_regime == symbol_state.confirmed_regime) {
      symbol_state.pending_regime = raw_regime;
      symbol_state.pending_regime_ticks = 0;
    } else {
      if (symbol_state.pending_regime != raw_regime) {
        symbol_state.pending_regime = raw_regime;
        symbol_state.pending_regime_ticks = 1;
      } else {
        ++symbol_state.pending_regime_ticks;
      }
      if (symbol_state.pending_regime_ticks >= confirm_ticks) {
        symbol_state.confirmed_regime = raw_regime;
        symbol_state.pending_regime_ticks = 0;
      }
    }
    regime = symbol_state.confirmed_regime;
  }

  state.regime = regime;
  state.bucket = ToBucket(regime);
  return state;
}

}  // namespace ai_trade
