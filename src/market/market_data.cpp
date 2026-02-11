#include "market/market_data.h"

namespace ai_trade {

MarketEvent MarketData::Next(double price, const std::string& symbol) {
  ++seq_;
  // 当前骨架将成交价与标记价保持一致，方便回放和测试。
  return MarketEvent{seq_, symbol, price, price};
}

}  // namespace ai_trade
