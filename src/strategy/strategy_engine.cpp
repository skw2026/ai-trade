#include "strategy/strategy_engine.h"

#include <algorithm>
#include <cmath>
#include <string>

namespace ai_trade {

namespace {

constexpr double kEpsilon = 1e-9;

int SignOf(double value) {
  if (value > kEpsilon) {
    return 1;
  }
  if (value < -kEpsilon) {
    return -1;
  }
  return 0;
}

double DefensiveBucketScale(const StrategyConfig& config, RegimeBucket bucket) {
  switch (bucket) {
    case RegimeBucket::kTrend:
      return config.defensive_trend_scale;
    case RegimeBucket::kRange:
      return config.defensive_range_scale;
    case RegimeBucket::kExtreme:
      return config.defensive_extreme_scale;
  }
  return config.defensive_range_scale;
}

}  // namespace

Signal StrategyEngine::OnMarket(const MarketEvent& event,
                                const AccountState& account,
                                const RegimeState& regime) {
  SymbolState& state = symbol_states_[event.symbol];

  // 1. 更新特征引擎
  state.feature_engine.OnMarket(event);

  // 初始化状态：第一帧行情仅用于记录基准价格，不生成信号
  if (!state.has_last) {
    state.has_last = true;
    state.last_price = event.price;
    Signal warmup_signal;
    warmup_signal.symbol = event.symbol;
    return warmup_signal;
  }
  const double safe_price = std::max(std::fabs(event.price), kEpsilon);
  const double price_delta_abs = std::fabs(event.price - state.last_price);
  state.last_price = event.price;

  // 2. 计算 EMA 指标 (Trend Strategy)
  // 确保窗口足够长以计算慢速 EMA
  if (!state.feature_engine.IsReady()) {
    Signal not_ready_signal;
    not_ready_signal.symbol = event.symbol;
    return not_ready_signal;
  }

  const double ema_fast = state.feature_engine.Evaluate("ema(close," + std::to_string(config_.trend_ema_fast) + ")");
  const double ema_slow = state.feature_engine.Evaluate("ema(close," + std::to_string(config_.trend_ema_slow) + ")");

  int raw_direction = 0;
  if (std::isfinite(ema_fast) && std::isfinite(ema_slow)) {
    if (ema_fast > ema_slow) {
      raw_direction = 1;
    } else if (ema_fast < ema_slow) {
      raw_direction = -1;
    }
  }
  // 价格变化不足 deadband 时沿用当前方向，避免在微小波动中频繁反手。
  if (config_.signal_deadband_abs > 0.0 &&
      price_delta_abs < config_.signal_deadband_abs) {
    raw_direction = state.effective_direction;
  }

  // 3. 信号防抖 (Signal Debounce)
  int final_direction = raw_direction;
  if (config_.min_hold_ticks > 0 &&
      state.effective_direction != 0 &&
      (raw_direction == 0 || raw_direction != state.effective_direction) &&
      state.ticks_since_direction_change < config_.min_hold_ticks) {
    final_direction = state.effective_direction;
  }

  // 更新状态计数器
  if (final_direction == state.effective_direction) {
    if (state.effective_direction != 0) {
      ++state.ticks_since_direction_change;
    }
  } else {
    state.effective_direction = final_direction;
    state.ticks_since_direction_change = 0;
  }

  // 3. 防御策略 (Defensive Mean-Reversion)
  // 偏离评分：price 相对 slow EMA 的偏离，按 regime 波动率归一化。
  int raw_defensive_direction = 0;
  double defensive_score_abs = 0.0;
  if (std::isfinite(ema_slow)) {
    const double deviation_ratio = (event.price - ema_slow) / safe_price;
    const double fallback_scale =
        std::max(config_.signal_deadband_abs / safe_price, 1e-4);
    const double vol_scale =
        regime.volatility_level > kEpsilon ? regime.volatility_level : fallback_scale;
    const double defensive_score = deviation_ratio / std::max(vol_scale, kEpsilon);
    defensive_score_abs = std::fabs(defensive_score);
    if (defensive_score_abs >= std::max(config_.defensive_entry_score, kEpsilon)) {
      raw_defensive_direction = -SignOf(defensive_score);
    }
  }
  if (config_.signal_deadband_abs > 0.0 &&
      price_delta_abs < config_.signal_deadband_abs) {
    raw_defensive_direction = state.defensive_effective_direction;
  }

  int final_defensive_direction = raw_defensive_direction;
  if (config_.min_hold_ticks > 0 &&
      state.defensive_effective_direction != 0 &&
      (raw_defensive_direction == 0 ||
       raw_defensive_direction != state.defensive_effective_direction) &&
      state.defensive_ticks_since_direction_change < config_.min_hold_ticks) {
    final_defensive_direction = state.defensive_effective_direction;
  }
  if (final_defensive_direction == state.defensive_effective_direction) {
    if (state.defensive_effective_direction != 0) {
      ++state.defensive_ticks_since_direction_change;
    }
  } else {
    state.defensive_effective_direction = final_defensive_direction;
    state.defensive_ticks_since_direction_change = 0;
  }

  // 4. 波动率目标仓位计算 (VolTarget Strategy)
  // 目标名义价值 = 账户权益 * (目标波动率 / 当前波动率)
  // 当前波动率使用 Regime 模块计算的 ewma_abs_return (每 tick 波动)
  // 需将年化目标波动率转换为每 tick 波动率 (假设 5秒/tick -> 6.3M ticks/year)
  double target_notional = config_.signal_notional_usd; // 默认使用固定名义值

  if (config_.vol_target_pct > 0.0 && regime.volatility_level > 1e-6) {
    const double equity = account.equity_usd();
    // 简单估算：年化转每 tick (假设 24x365 小时交易，5秒一个tick)
    // sqrt(365 * 24 * 720) approx 2500
    const double annual_factor = 2500.0;
    const double current_vol_annual = regime.volatility_level * annual_factor;

    // 限制杠杆倍数，防止低波动率时仓位过大。
    double max_leverage = std::max(0.1, config_.vol_target_max_leverage);
    if (config_.vol_target_low_vol_leverage_cap_enabled &&
        current_vol_annual <= config_.vol_target_low_vol_annual_threshold) {
      max_leverage =
          std::min(max_leverage, config_.vol_target_low_vol_max_leverage);
    }
    double leverage = config_.vol_target_pct / current_vol_annual;
    leverage = std::clamp(leverage, 0.1, max_leverage);

    target_notional = equity * leverage;
  }

  // VolTarget 防抖：目标名义变化未超过绝对/相对阈值时，沿用上次目标，
  // 避免权益和波动率微小抖动触发连续小额调仓。
  const double proposed_target_notional = target_notional;
  if (!state.has_last_target_notional) {
    state.has_last_target_notional = true;
    state.last_target_notional = proposed_target_notional;
  } else {
    const double delta_abs =
        std::fabs(proposed_target_notional - state.last_target_notional);
    const double abs_threshold =
        std::max(0.0, config_.vol_target_rebalance_min_abs_usd);
    const double ratio_threshold =
        std::max(0.0, config_.vol_target_rebalance_min_ratio);
    const double denom = std::max(std::fabs(state.last_target_notional), 1.0);
    const double delta_ratio = delta_abs / denom;
    const bool abs_triggered =
        (abs_threshold <= 0.0) || (delta_abs >= abs_threshold);
    const bool ratio_triggered =
        (ratio_threshold <= 0.0) || (delta_ratio >= ratio_threshold);
    if (abs_triggered || ratio_triggered) {
      state.last_target_notional = proposed_target_notional;
    }
    target_notional = state.last_target_notional;
  }

  double trend_notional = 0.0;
  if (final_direction != 0) {
    trend_notional = static_cast<double>(final_direction) * target_notional;
  }

  double defensive_notional = 0.0;
  if (final_defensive_direction != 0 && config_.defensive_notional_ratio > 0.0) {
    const double score_threshold = std::max(config_.defensive_entry_score, kEpsilon);
    const double intensity =
        std::clamp(defensive_score_abs / score_threshold, 0.0, 1.0);
    double bucket_scale = DefensiveBucketScale(config_, regime.bucket);
    if (regime.warmup) {
      bucket_scale *= 0.5;
    }
    defensive_notional = static_cast<double>(final_defensive_direction) *
                         target_notional *
                         std::max(0.0, config_.defensive_notional_ratio) *
                         std::max(0.0, bucket_scale) * intensity;
  }

  Signal signal;
  signal.symbol = event.symbol;
  signal.trend_notional_usd = trend_notional;
  signal.defensive_notional_usd = defensive_notional;
  signal.suggested_notional_usd = trend_notional + defensive_notional;
  signal.direction = SignOf(signal.suggested_notional_usd);
  return signal;
}

}  // namespace ai_trade
