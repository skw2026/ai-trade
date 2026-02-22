#include "market/market_data.h"

namespace ai_trade {

MarketEvent MarketData::Next(double price, const std::string& symbol) {
  ++seq_;
  // 当前骨架将成交价与标记价保持一致，方便回放和测试。
  constexpr std::int64_t kDefaultIntervalMs = 5000;
  return MarketEvent{seq_ * kDefaultIntervalMs,
                     symbol,
                     price,
                     price,
                     1000.0,
                     kDefaultIntervalMs};
}

}  // namespace ai_trade
