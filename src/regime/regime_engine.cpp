#include "regime/regime_engine.h"

#include <algorithm>
#include <cmath>

namespace ai_trade {

namespace {

constexpr double kEpsilon = 1e-12;
constexpr double kTrendCandidateMinThresholdRatio = 0.60;

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

double SanitizeVolume(double volume) {
  if (!std::isfinite(volume)) {
    return 0.0;
  }
  return std::max(0.0, volume);
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

RegimeState RegimeEngine::ProcessSample(SymbolState& symbol_state,
                                        const std::string& symbol,
                                        double price,
                                        double volume,
                                        std::int64_t interval_ms,
                                        int aggregated_event_count) {
  RegimeState state;
  state.symbol = symbol;
  state.decision_interval_ms = std::max<std::int64_t>(0, interval_ms);
  state.aggregated_event_count = std::max(aggregated_event_count, 1);
  if (!symbol_state.has_last_price || symbol_state.last_price <= kEpsilon ||
      price <= kEpsilon) {
    symbol_state.has_last_price = true;
    symbol_state.last_price = price;
    symbol_state.sample_ticks = 1;
    state.warmup = true;
    symbol_state.last_emitted_state = state;
    symbol_state.has_last_emitted_state = true;
    return state;
  }

  const double instant_return = (price - symbol_state.last_price) / symbol_state.last_price;
  symbol_state.last_price = price;
  ++symbol_state.sample_ticks;

  const double alpha = IntervalAwareAlpha(config_.ewma_alpha, interval_ms);
  symbol_state.ewma_return =
      (1.0 - alpha) * symbol_state.ewma_return + alpha * instant_return;
  symbol_state.ewma_abs_return =
      (1.0 - alpha) * symbol_state.ewma_abs_return +
      alpha * std::fabs(instant_return);
  symbol_state.ewma_volume =
      (1.0 - alpha) * symbol_state.ewma_volume + alpha * volume;

  state.instant_return = instant_return;
  state.trend_strength = symbol_state.ewma_return;
  state.volatility_level = symbol_state.ewma_abs_return;
  state.trend_threshold_ratio =
      config_.trend_threshold > kEpsilon
          ? std::fabs(state.trend_strength) / config_.trend_threshold
          : 0.0;
  state.volatility_threshold_ratio =
      config_.volatility_threshold > kEpsilon
          ? state.volatility_level / config_.volatility_threshold
          : 0.0;
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
    const std::int64_t confirm_elapsed_ms =
        (config_.bar_interval_ms > 0 && confirm_ticks > 1)
            ? static_cast<std::int64_t>(confirm_ticks) *
                  static_cast<std::int64_t>(config_.bar_interval_ms)
            : 0;
    if (!symbol_state.has_confirmed_regime || confirm_ticks <= 1) {
      symbol_state.has_confirmed_regime = true;
      symbol_state.confirmed_regime = raw_regime;
      symbol_state.pending_regime = raw_regime;
      symbol_state.pending_regime_ticks = 0;
      symbol_state.pending_regime_elapsed_ms = 0;
    } else if (raw_regime == symbol_state.confirmed_regime) {
      symbol_state.pending_regime = raw_regime;
      symbol_state.pending_regime_ticks = 0;
      symbol_state.pending_regime_elapsed_ms = 0;
    } else {
      if (symbol_state.pending_regime != raw_regime) {
        symbol_state.pending_regime = raw_regime;
        symbol_state.pending_regime_ticks = 1;
        symbol_state.pending_regime_elapsed_ms =
            std::max<std::int64_t>(0, interval_ms);
      } else {
        ++symbol_state.pending_regime_ticks;
        symbol_state.pending_regime_elapsed_ms +=
            std::max<std::int64_t>(0, interval_ms);
      }
      if (symbol_state.pending_regime_ticks >= confirm_ticks ||
          (confirm_elapsed_ms > 0 &&
           symbol_state.pending_regime_elapsed_ms >= confirm_elapsed_ms)) {
        symbol_state.confirmed_regime = raw_regime;
        symbol_state.pending_regime_ticks = 0;
        symbol_state.pending_regime_elapsed_ms = 0;
      }
    }
    regime = symbol_state.confirmed_regime;
  }

  state.regime = regime;
  state.bucket = ToBucket(regime);
  state.trend_candidate =
      !state.warmup && state.bucket == RegimeBucket::kRange &&
      state.trend_threshold_ratio >= kTrendCandidateMinThresholdRatio;
  symbol_state.last_emitted_state = state;
  symbol_state.has_last_emitted_state = true;
  return state;
}

RegimeState RegimeEngine::OnMarket(const MarketEvent& event) {
  RegimeState state;
  state.symbol = event.symbol;

  if (!config_.enabled) {
    state.warmup = false;
    return state;
  }

  SymbolState& symbol_state = symbol_state_[event.symbol];
  if (config_.bar_interval_ms <= 0) {
    return ProcessSample(symbol_state,
                         event.symbol,
                         event.price,
                         SanitizeVolume(event.volume),
                         event.interval_ms,
                         1);
  }

  symbol_state.pending_price = event.price;
  symbol_state.pending_volume += SanitizeVolume(event.volume);
  symbol_state.pending_interval_ms += std::max<std::int64_t>(0, event.interval_ms);
  ++symbol_state.pending_event_count;

  if (symbol_state.pending_interval_ms < config_.bar_interval_ms) {
    if (symbol_state.has_last_emitted_state) {
      return symbol_state.last_emitted_state;
    }
    state.warmup = true;
    return state;
  }

  const double aggregated_price = symbol_state.pending_price;
  const double aggregated_volume = symbol_state.pending_volume;
  const std::int64_t aggregated_interval_ms = symbol_state.pending_interval_ms;
  const int aggregated_event_count = symbol_state.pending_event_count;

  symbol_state.pending_interval_ms = 0;
  symbol_state.pending_volume = 0.0;
  symbol_state.pending_price = 0.0;
  symbol_state.pending_event_count = 0;

  return ProcessSample(symbol_state,
                       event.symbol,
                       aggregated_price,
                       aggregated_volume,
                       aggregated_interval_ms,
                       aggregated_event_count);
}

}  // namespace ai_trade
