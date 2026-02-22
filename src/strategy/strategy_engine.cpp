#include "strategy/strategy_engine.h"

#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

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

void PushReason(std::vector<std::string>* reasons, const std::string& code) {
  if (reasons == nullptr || code.empty()) {
    return;
  }
  if (std::find(reasons->begin(), reasons->end(), code) == reasons->end()) {
    reasons->push_back(code);
  }
}

void AppendRegimeReason(const RegimeState& regime,
                        std::vector<std::string>* reasons) {
  if (reasons == nullptr) {
    return;
  }
  switch (regime.regime) {
    case Regime::kUptrend:
      PushReason(reasons, "REG_UPTREND");
      break;
    case Regime::kDowntrend:
      PushReason(reasons, "REG_DOWNTREND");
      break;
    case Regime::kRange:
      PushReason(reasons, "REG_RANGE");
      break;
    case Regime::kExtreme:
      PushReason(reasons, "REG_EXTREME");
      break;
  }
  if (regime.warmup) {
    PushReason(reasons, "REG_WARMUP");
  }
}

}  // namespace

Signal StrategyEngine::OnMarket(const MarketEvent& event,
                                const AccountState& account,
                                const RegimeState& regime) {
  SymbolState& state = symbol_states_[event.symbol];
  const int signal_valid_ms = std::max(1000, config_.signal_valid_for_ms);
  std::int64_t event_interval_ms = event.interval_ms;
  if (event_interval_ms <= 0 && state.last_event_ts_ms > 0 &&
      event.ts_ms > state.last_event_ts_ms) {
    event_interval_ms = event.ts_ms - state.last_event_ts_ms;
  }
  // 回放/测试场景可能使用逻辑序号做 ts，过小间隔会导致年化波动异常放大。
  if (event_interval_ms <= 0 || event_interval_ms < 100 ||
      event_interval_ms > 600000) {
    event_interval_ms = std::max(1, config_.default_tick_interval_ms);
  }
  if (event.ts_ms > 0) {
    state.last_event_ts_ms = event.ts_ms;
  } else if (state.last_event_ts_ms > 0) {
    state.last_event_ts_ms += event_interval_ms;
  } else {
    state.last_event_ts_ms = event_interval_ms;
  }

  auto make_base_signal = [&]() {
    Signal signal;
    signal.symbol = event.symbol;
    signal.valid_until_ms = state.last_event_ts_ms + signal_valid_ms;
    return signal;
  };

  // 1. 更新特征引擎
  state.feature_engine.OnMarket(event);

  // 初始化状态：第一帧行情仅用于记录基准价格，不生成信号
  if (!state.has_last) {
    state.has_last = true;
    state.last_price = event.price;
    Signal warmup_signal = make_base_signal();
    PushReason(&warmup_signal.reason_codes, "STR_WARMUP");
    AppendRegimeReason(regime, &warmup_signal.reason_codes);
    return warmup_signal;
  }
  const double safe_price = std::max(std::fabs(event.price), kEpsilon);
  const double price_delta_abs = std::fabs(event.price - state.last_price);
  state.last_price = event.price;

  // 2. 计算 EMA 指标 (Trend Strategy)
  // 确保窗口足够长以计算慢速 EMA
  if (!state.feature_engine.IsReady()) {
    Signal not_ready_signal = make_base_signal();
    PushReason(&not_ready_signal.reason_codes, "STR_FEATURE_NOT_READY");
    AppendRegimeReason(regime, &not_ready_signal.reason_codes);
    return not_ready_signal;
  }

  const double ema_fast = state.feature_engine.Evaluate("ema(close," + std::to_string(config_.trend_ema_fast) + ")");
  const double ema_slow = state.feature_engine.Evaluate("ema(close," + std::to_string(config_.trend_ema_slow) + ")");
  if (std::isfinite(ema_slow)) {
    state.ema_slow_history.push_back(ema_slow);
    const std::size_t max_history =
        static_cast<std::size_t>(std::max(4, config_.trend_slope_lookback_ticks + 4));
    while (state.ema_slow_history.size() > max_history) {
      state.ema_slow_history.pop_front();
    }
  }

  int raw_direction = 0;
  Signal signal = make_base_signal();
  PushReason(&signal.reason_codes, "STR_TREND_EVAL");
  if (std::isfinite(ema_fast) && std::isfinite(ema_slow)) {
    if (ema_fast > ema_slow) {
      raw_direction = 1;
      PushReason(&signal.reason_codes, "STR_TREND_EMA_UP");
    } else if (ema_fast < ema_slow) {
      raw_direction = -1;
      PushReason(&signal.reason_codes, "STR_TREND_EMA_DOWN");
    } else {
      PushReason(&signal.reason_codes, "STR_TREND_EMA_FLAT");
    }
  } else {
    PushReason(&signal.reason_codes, "STR_TREND_EMA_INVALID");
  }

  if (raw_direction != 0 && config_.trend_breakout_lookback_ticks > 1) {
    const int breakout_window = config_.trend_breakout_lookback_ticks;
    const double rank = state.feature_engine.Evaluate(
        "ts_rank(close," + std::to_string(breakout_window) + ")");
    if (!std::isfinite(rank)) {
      raw_direction = 0;
      PushReason(&signal.reason_codes, "STR_BREAKOUT_NOT_READY");
    } else {
      const double threshold =
          std::clamp(config_.trend_breakout_rank_threshold, 0.5, 1.0);
      const bool breakout_up = rank >= threshold;
      const bool breakout_down = rank <= (1.0 - threshold);
      const bool breakout_pass =
          raw_direction > 0 ? breakout_up : breakout_down;
      if (!breakout_pass) {
        raw_direction = 0;
        PushReason(&signal.reason_codes, "STR_BREAKOUT_BLOCK");
      } else {
        PushReason(&signal.reason_codes, "STR_BREAKOUT_PASS");
      }
    }
  }

  if (raw_direction != 0 && config_.trend_slope_min_abs > 0.0 &&
      config_.trend_slope_lookback_ticks > 0) {
    const int lookback = config_.trend_slope_lookback_ticks;
    if (state.ema_slow_history.size() <= static_cast<std::size_t>(lookback)) {
      raw_direction = 0;
      PushReason(&signal.reason_codes, "STR_SLOPE_NOT_READY");
    } else {
      const std::size_t idx =
          state.ema_slow_history.size() - 1 - static_cast<std::size_t>(lookback);
      const double ema_ref = state.ema_slow_history[idx];
      const double slope = (ema_slow - ema_ref) / safe_price;
      const double min_abs = std::max(0.0, config_.trend_slope_min_abs);
      const bool slope_pass =
          raw_direction > 0 ? (slope >= min_abs) : (slope <= -min_abs);
      if (!slope_pass) {
        raw_direction = 0;
        PushReason(&signal.reason_codes, "STR_SLOPE_BLOCK");
      } else {
        PushReason(&signal.reason_codes, "STR_SLOPE_PASS");
      }
    }
  }

  const double annual_factor =
      std::sqrt((365.0 * 24.0 * 3600.0 * 1000.0) /
                static_cast<double>(std::max<std::int64_t>(1, event_interval_ms)));
  const double current_vol_annual = regime.volatility_level * annual_factor;
  if (raw_direction != 0 && config_.trend_vol_cap_annual > 0.0 &&
      std::isfinite(current_vol_annual) &&
      current_vol_annual > config_.trend_vol_cap_annual) {
    raw_direction = 0;
    PushReason(&signal.reason_codes, "STR_VOL_CAP_BLOCK");
  }

  // 价格变化不足 deadband 时沿用当前方向，避免在微小波动中频繁反手。
  if (config_.signal_deadband_abs > 0.0 &&
      price_delta_abs < config_.signal_deadband_abs) {
    raw_direction = state.effective_direction;
    PushReason(&signal.reason_codes, "STR_DEADBAND_HOLD");
  }

  // 3. 信号防抖 (Signal Debounce)
  int final_direction = raw_direction;
  if (config_.min_hold_ticks > 0 &&
      state.effective_direction != 0 &&
      (raw_direction == 0 || raw_direction != state.effective_direction) &&
      state.ticks_since_direction_change < config_.min_hold_ticks) {
    final_direction = state.effective_direction;
    PushReason(&signal.reason_codes, "STR_MIN_HOLD_HOLD");
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
      PushReason(&signal.reason_codes, "STR_DEFENSIVE_TRIGGER");
    }
  }
  if (config_.signal_deadband_abs > 0.0 &&
      price_delta_abs < config_.signal_deadband_abs) {
    raw_defensive_direction = state.defensive_effective_direction;
    PushReason(&signal.reason_codes, "STR_DEFENSIVE_DEADBAND_HOLD");
  }

  int final_defensive_direction = raw_defensive_direction;
  if (config_.min_hold_ticks > 0 &&
      state.defensive_effective_direction != 0 &&
      (raw_defensive_direction == 0 ||
       raw_defensive_direction != state.defensive_effective_direction) &&
      state.defensive_ticks_since_direction_change < config_.min_hold_ticks) {
    final_defensive_direction = state.defensive_effective_direction;
    PushReason(&signal.reason_codes, "STR_DEFENSIVE_MIN_HOLD_HOLD");
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
    const double current_vol_annual_local = regime.volatility_level * annual_factor;

    // 限制杠杆倍数，防止低波动率时仓位过大。
    double max_leverage = std::max(0.1, config_.vol_target_max_leverage);
    if (config_.vol_target_low_vol_leverage_cap_enabled &&
        current_vol_annual_local <= config_.vol_target_low_vol_annual_threshold) {
      max_leverage =
          std::min(max_leverage, config_.vol_target_low_vol_max_leverage);
    }
    double leverage = config_.vol_target_pct / current_vol_annual_local;
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
  double trend_strength = 1.0;
  if (final_direction != 0) {
    if (config_.trend_strength_scale > 0.0 && std::isfinite(ema_fast) &&
        std::isfinite(ema_slow)) {
      const double ema_gap_ratio = std::fabs(ema_fast - ema_slow) / safe_price;
      const double vol_proxy =
          std::max(regime.volatility_level, std::max(price_delta_abs / safe_price, 1e-6));
      const double normalized =
          ema_gap_ratio /
          std::max(1e-6, config_.trend_strength_scale * vol_proxy);
      trend_strength = std::clamp(normalized, 0.0, 1.0);
    }
    trend_notional = static_cast<double>(final_direction) * target_notional *
                     std::clamp(trend_strength, 0.0, 1.0);
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

  signal.trend_notional_usd = trend_notional;
  signal.defensive_notional_usd = defensive_notional;
  signal.suggested_notional_usd = trend_notional + defensive_notional;
  signal.direction = SignOf(signal.suggested_notional_usd);
  signal.confidence =
      std::clamp(std::fabs(signal.suggested_notional_usd) /
                     std::max(1.0, std::fabs(target_notional)),
                 0.0, 1.0);
  if (signal.direction == 0) {
    PushReason(&signal.reason_codes, "STR_FLAT_SIGNAL");
  } else {
    PushReason(&signal.reason_codes, "STR_ACTIVE_SIGNAL");
  }
  AppendRegimeReason(regime, &signal.reason_codes);
  if (signal.reason_codes.empty()) {
    PushReason(&signal.reason_codes, "STR_NO_REASON");
  }
  return signal;
}

}  // namespace ai_trade
