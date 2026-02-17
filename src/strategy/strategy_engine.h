#pragma once

#include <string>
#include <unordered_map>
#include <memory>

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
    
    // Trend Logic State
    int effective_direction{0};
    int ticks_since_direction_change{0};
    
    // Defensive Logic State
    int defensive_effective_direction{0};
    int defensive_ticks_since_direction_change{0};
    
    // Feature Engine
    research::OnlineFeatureEngine feature_engine{100};
  };

  StrategyConfig config_;
  std::unordered_map<std::string, SymbolState> symbol_states_;
};

}  // namespace ai_trade
