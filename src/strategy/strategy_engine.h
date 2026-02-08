#pragma once

#include <string>
#include <unordered_map>

#include "core/types.h"

namespace ai_trade {

class StrategyEngine {
 public:
  Signal OnMarket(const MarketEvent& event);

 private:
  struct SymbolState {
    double last_price{0.0};
    bool has_last{false};
  };
  std::unordered_map<std::string, SymbolState> symbol_state_;
};

}  // namespace ai_trade
