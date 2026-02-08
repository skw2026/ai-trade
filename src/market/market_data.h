#pragma once

#include <string>

#include "core/types.h"

namespace ai_trade {

class MarketData {
 public:
  MarketEvent Next(double price, const std::string& symbol = "BTCUSDT");

 private:
  std::int64_t seq_{0};
};

}  // namespace ai_trade
