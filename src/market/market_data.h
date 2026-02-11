#pragma once

#include <string>

#include "core/types.h"

namespace ai_trade {

/**
 * @brief 轻量行情序列生成器
 *
 * 主要用于本地回放与单元测试，按调用顺序递增 `seq`。
 */
class MarketData {
 public:
  /**
   * @brief 生成下一条行情
   * @param price 最新价格
   * @param symbol 交易标的
   */
  MarketEvent Next(double price, const std::string& symbol = "BTCUSDT");

 private:
  std::int64_t seq_{0};
};

}  // namespace ai_trade
