#pragma once

#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "core/config.h"
#include "core/types.h"

namespace ai_trade {

struct SymbolScore {
  std::string symbol;  ///< 币对代码。
  double score{0.0};  ///< 综合评分（越大越优）。
};

struct UniverseUpdate {
  bool degraded_to_fallback{false};  ///< 是否降级到了 fallback 集合。
  std::string reason_code;  ///< 降级原因码（若有）。
  std::vector<std::string> active_symbols;  ///< 本轮生效 active universe。
  std::vector<SymbolScore> symbol_scores;  ///< 候选集评分明细。
};

/**
 * @brief 智能筛币模块 (Universe Selector)
 *
 * 负责根据市场行情动态筛选"活跃"的交易标的。
 * 逻辑：
 * 1. 过滤：剔除不可交易或不在白名单的标的。
 * 2. 打分：基于波动率 (Volatility) 和流动性 (Liquidity/Activity) 评分。
 * 3. 截断：保留 Top N 个标的作为 Active Universe。
 * 4. 降级：若筛选结果为空，回退到默认标的 (Fallback)。
 */
class UniverseSelector {
 public:
  UniverseSelector(UniverseConfig config, std::string primary_symbol);

  std::optional<UniverseUpdate> OnMarket(const MarketEvent& event);
  void SetAllowedSymbols(const std::vector<std::string>& symbols);
  bool IsActive(const std::string& symbol) const;
  const std::vector<std::string>& active_symbols() const { return active_symbols_; }

 private:
  struct MarketStats {
    bool has_last_price{false};  ///< 是否已有上一价格（用于收益率计算）。
    double last_price{0.0};  ///< 最近价格。
    double abs_return_sum{0.0};  ///< 绝对收益率累积和（波动度近似）。
    int return_count{0};  ///< 有效收益率样本数。
    int tick_count{0};  ///< 观测到的行情 tick 数（活跃度近似）。
    double last_turnover{0.0}; ///< 最近一次观测到的 24h 成交额 (USD)。
  };

  std::optional<UniverseUpdate> Refresh();
  static std::vector<std::string> UniqueSymbols(
      const std::vector<std::string>& symbols);
  bool IsAllowed(const std::string& symbol) const;
  void NormalizeActiveSymbols();
  void RebuildActiveSet();

  UniverseConfig config_;  ///< Universe 筛选配置。
  std::string primary_symbol_;  ///< 主交易标的（最终兜底）。
  // 距离上次 Universe 刷新已过去的行情 tick 数。
  int ticks_since_update_{0};
  std::unordered_map<std::string, MarketStats> stats_by_symbol_;  ///< symbol 统计状态。
  std::unordered_set<std::string> seen_symbols_;  ///< 已观测到的 symbol 集合。
  std::vector<std::string> active_symbols_;  ///< 当前活跃币对列表（有序）。
  std::unordered_set<std::string> active_symbol_set_;  ///< 当前活跃币对集合（快速查找）。
  bool allowed_symbol_filter_enabled_{false};  ///< 是否启用交易规则白名单过滤。
  std::unordered_set<std::string> allowed_symbol_set_;  ///< 交易规则过滤后的允许集合。
};

}  // namespace ai_trade
