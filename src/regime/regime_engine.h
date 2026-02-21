#pragma once

#include <string>
#include <unordered_map>

#include "core/config.h"
#include "core/types.h"

namespace ai_trade {

/**
 * @brief Regime 轻量识别器
 *
 * 设计目标：
 * 1. 仅使用行情序列即可稳定输出四态 Regime；
 * 2. 输出同时包含四态与三桶（Trend/Range/Extreme）；
 * 3. 判定逻辑保持确定性（同输入回放两次输出一致）。
 */
class RegimeEngine {
 public:
  explicit RegimeEngine(RegimeConfig config = {}) : config_(config) {}

  /// 输入一笔行情并返回最新 Regime 状态。
  RegimeState OnMarket(const MarketEvent& event);

 private:
  struct SymbolState {
    bool has_last_price{false};     ///< 是否已拿到上一笔价格。
    double last_price{0.0};         ///< 上一笔价格（用于收益率计算）。
    int sample_ticks{0};            ///< 已处理样本数（用于 warmup）。
    double ewma_return{0.0};        ///< EWMA 收益率（有方向）。
    double ewma_abs_return{0.0};    ///< EWMA 绝对收益率（波动代理）。
    bool has_confirmed_regime{false};  ///< 是否已有确认态（非 warmup 后）。
    Regime confirmed_regime{Regime::kRange};  ///< 已确认 Regime。
    Regime pending_regime{Regime::kRange};    ///< 待确认 Regime。
    int pending_regime_ticks{0};  ///< 待确认 Regime 连续计数。
  };

  static RegimeBucket ToBucket(Regime regime);

  RegimeConfig config_;  ///< Regime 识别配置快照。
  std::unordered_map<std::string, SymbolState> symbol_state_;  ///< 多币对运行态。
};

}  // namespace ai_trade
