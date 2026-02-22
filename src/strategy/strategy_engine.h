#pragma once

#include <string>
#include <unordered_map>
#include <memory>
#include <deque>

#include "core/config.h"
#include "core/types.h"
#include "oms/account_state.h"
#include "research/online_feature_engine.h"

namespace ai_trade {

/**
 * @brief Strategy Engine
 *
 * Core logic component that transforms Market Data into Trading Signals.
 * 
 * Responsibilities:
 * 1. Maintain per-symbol state (indicators, features).
 * 2. Execute strategy logic (Trend, Mean Reversion, etc.).
 * 3. Output raw signals (direction + notional) without risk constraints.
 */
class StrategyEngine {
 public:
  explicit StrategyEngine(StrategyConfig config = {}) : config_(config) {}

  /**
   * @brief Main entry point for signal generation.
   * 
   * @param event Current market data.
   * @param account Current portfolio state (for sizing).
   * @param regime Current market regime (for filtering/weighting).
   * @return Signal The generated signal.
   */
  Signal OnMarket(const MarketEvent& event,
                  const AccountState& account,
                  const RegimeState& regime);

 private:
  // Internal state for a single symbol
  struct SymbolState {
    double last_price{0.0};
    bool has_last{false};
    std::int64_t last_event_ts_ms{0};
    
    // Trend Logic State
    int effective_direction{0};
    int ticks_since_direction_change{0};
    std::deque<double> ema_slow_history{};
    
    // Defensive Logic State
    int defensive_effective_direction{0};
    int defensive_ticks_since_direction_change{0};

    // VolTarget 防抖状态：记录上次生效的目标名义值，抑制微小抖动反复调仓。
    bool has_last_target_notional{false};
    double last_target_notional{0.0};
    
    // Feature Engine
    research::OnlineFeatureEngine feature_engine{100};
  };

  StrategyConfig config_;
  std::unordered_map<std::string, SymbolState> symbol_states_;
};

}  // namespace ai_trade
