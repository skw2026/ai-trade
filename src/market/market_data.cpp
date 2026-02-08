#include "market/market_data.h"

namespace ai_trade {

MarketEvent MarketData::Next(double price, const std::string& symbol) {
  ++seq_;
  return MarketEvent{seq_, symbol, price, price};
}

}  // namespace ai_trade
