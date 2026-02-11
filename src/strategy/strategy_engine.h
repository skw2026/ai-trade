#pragma once

#include <string>
#include <unordered_map>

#include "core/types.h"

namespace ai_trade {

/**
 * @brief 策略配置参数
 */
struct StrategyConfig {
  double signal_notional_usd{1000.0}; ///< 信号触发时的目标净名义敞口基线（USD, signed）
  double signal_deadband_abs{0.1};    ///< 价格变化的绝对死区阈值 (防止微小波动频繁触发)
  int min_hold_ticks{0};              ///< 反向信号的最小持有 tick 数 (防止信号闪烁)
};

/**
 * @brief 策略引擎 (Strategy Engine)
 *
 * 负责接收市场行情 (MarketEvent)，根据配置的策略逻辑生成原始交易信号 (Signal)。
 * 当前实现为一个基础的趋势跟踪策略：
 * 1. 比较当前价格与上一 tick 价格。
 * 2. 变化量超过 deadband 则生成方向信号。
 * 3. 包含信号防抖机制 (Min Hold Ticks)。
 */
class StrategyEngine {
 public:
  explicit StrategyEngine(StrategyConfig config = {}) : config_(config) {}

  /**
   * @brief 处理行情事件并生成信号
   * @param event 最新行情事件
   * @return Signal 生成的交易信号 (direction=0 表示观望)
   */
  Signal OnMarket(const MarketEvent& event);

 private:
  struct SymbolState {
    double last_price{0.0};  ///< 上一笔用于比较的价格。
    bool has_last{false};  ///< 是否已完成暖机（拿到首个价格）。
    int effective_direction{0};  ///< 当前生效方向（防抖后）。
    int ticks_since_direction_change{0};  ///< 当前方向已持续的 tick 数。
  };
  StrategyConfig config_{};  ///< 策略静态参数。
  std::unordered_map<std::string, SymbolState> symbol_state_;  ///< 多 symbol 运行态。
};

}  // namespace ai_trade
