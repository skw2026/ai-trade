#pragma once

#include <string>
#include <unordered_map>

#include "oms/account_state.h"
#include "research/online_feature_engine.h"
#include "core/types.h"

namespace ai_trade {

/**
 * @brief 策略配置参数
 */
struct StrategyConfig {
  double signal_notional_usd{1000.0}; ///< 信号触发时的目标净名义敞口基线（USD, signed）
  double signal_deadband_abs{0.1};    ///< 价格变化的绝对死区阈值 (防止微小波动频繁触发)
  int min_hold_ticks{0};              ///< 反向信号的最小持有 tick 数 (防止信号闪烁)
  int trend_ema_fast{12};             ///< 趋势策略：快线周期
  int trend_ema_slow{26};             ///< 趋势策略：慢线周期
  double vol_target_pct{0.40};        ///< 波动率目标策略：年化波动率目标 (e.g. 0.40)
  double defensive_notional_ratio{0.0};  ///< 防御策略名义值比例（相对 trend 基线）。
  double defensive_entry_score{1.25};    ///< 防御入场阈值（偏离评分绝对值）。
  double defensive_trend_scale{0.35};    ///< TREND 桶防御策略缩放。
  double defensive_range_scale{1.00};    ///< RANGE 桶防御策略缩放。
  double defensive_extreme_scale{0.55};  ///< EXTREME 桶防御策略缩放。
};

/**
 * @brief 策略引擎 (Strategy Engine)
 *
 * 负责接收市场行情 (MarketEvent)，根据配置的策略逻辑生成原始交易信号 (Signal)。
 * 实现逻辑：
 * 1. Trend: 基于 EMA 快慢线交叉判断方向 (ema_fast > ema_slow -> Long)。
 * 2. Defensive: 基于价格偏离 slow EMA 的均值回归分支（按 Regime 分桶缩放）。
 * 3. VolTarget: 基于 Regime 波动率动态调整目标仓位 (TargetVol / CurrentVol)。
 * 4. Signal: 包含信号防抖机制 (Min Hold Ticks)。
 */
class StrategyEngine {
 public:
  explicit StrategyEngine(StrategyConfig config = {}) : config_(config) {}

  /**
   * @brief 处理行情事件并生成信号
   * @param event 最新行情事件
   * @param account 账户状态（用于波动率目标计算仓位）
   * @param regime 市场状态（用于过滤或调整策略）
   * @return Signal 生成的交易信号 (direction=0 表示观望)
   */
  Signal OnMarket(const MarketEvent& event,
                  const AccountState& account,
                  const RegimeState& regime);

 private:
  struct SymbolState {
    double last_price{0.0};  ///< 上一笔用于比较的价格。
    bool has_last{false};  ///< 是否已完成暖机（拿到首个价格）。
    int effective_direction{0};  ///< 当前生效方向（防抖后）。
    int ticks_since_direction_change{0};  ///< 当前方向已持续的 tick 数。
    int defensive_effective_direction{0};  ///< 防御分支当前生效方向（防抖后）。
    int defensive_ticks_since_direction_change{0};  ///< 防御分支方向持续 tick 数。
    research::OnlineFeatureEngine feature_engine{100}; ///< 在线特征计算引擎
  };
  StrategyConfig config_{};  ///< 策略静态参数。
  std::unordered_map<std::string, SymbolState> symbol_state_;  ///< 多 symbol 运行态。
};

}  // namespace ai_trade
