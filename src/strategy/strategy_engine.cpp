#include "strategy/strategy_engine.h"

#include <cmath>

namespace ai_trade {

Signal StrategyEngine::OnMarket(const MarketEvent& event) {
  SymbolState& state = symbol_state_[event.symbol];
  // 初始化状态：第一帧行情仅用于记录基准价格，不生成信号
  if (!state.has_last) {
    state.has_last = true;
    state.last_price = event.price;
    Signal warmup_signal;
    warmup_signal.symbol = event.symbol;
    return warmup_signal;
  }

  const double delta = event.price - state.last_price;
  state.last_price = event.price;

  // 1. 死区过滤：只有价格变化超过阈值才认为有趋势
  int raw_direction = 0;
  if (std::fabs(delta) >= config_.signal_deadband_abs) {
    raw_direction = (delta > 0.0) ? 1 : -1;
  }

  // 2. 信号防抖：如果反向信号出现，必须满足最小持有期才能切换方向
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

  Signal signal;
  signal.symbol = event.symbol;
  signal.direction = final_direction;
  if (final_direction != 0) {
    signal.suggested_notional_usd =
        static_cast<double>(final_direction) * config_.signal_notional_usd;
  }
  return signal;
}

}  // namespace ai_trade
