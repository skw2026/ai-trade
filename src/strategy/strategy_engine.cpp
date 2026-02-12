#include "strategy/strategy_engine.h"

#include <algorithm>
#include <cmath>
#include <string>

namespace ai_trade {

Signal StrategyEngine::OnMarket(const MarketEvent& event,
                                const AccountState& account,
                                const RegimeState& regime) {
  SymbolState& state = symbol_state_[event.symbol];
  
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
    
    // 限制杠杆倍数，防止低波动率时仓位过大
    const double max_leverage = 3.0; 
    double leverage = config_.vol_target_pct / current_vol_annual;
    leverage = std::clamp(leverage, 0.1, max_leverage);
    
    target_notional = equity * leverage;
  }

  Signal signal;
  signal.symbol = event.symbol;
  signal.direction = final_direction;
  if (final_direction != 0) {
    signal.suggested_notional_usd = static_cast<double>(final_direction) * target_notional;
  }
  return signal;
}

}  // namespace ai_trade
