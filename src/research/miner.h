#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "research/ic_evaluator.h"

namespace ai_trade::research {

/// 研究用 K 线样本（最小字段集）。
struct ResearchBar {
  std::int64_t ts_ms{0};
  double open{0.0};
  double high{0.0};
  double low{0.0};
  double close{0.0};
  double volume{0.0};
};

/// 单个因子评估结果。
struct RankedFactor {
  std::string expression;
  double fitness_ic_train{0.0};
  double fitness_ic_oos{0.0};
  double complexity_score{0.0};
  double objective_score{0.0};
  bool invert_signal{false};
  std::string valid_universe{"ALL"};
  int random_seed{42};
  std::string search_space_version{"ts_ops_v1"};
  IcSummary rolling_ic_train{};
  IcSummary rolling_ic_oos{};
};

/// Miner 运行配置（R1 最小集）。
struct MinerConfig {
  int random_seed{42};
  std::size_t top_k{10};
  double train_split_ratio{0.7};
  double complexity_penalty{0.01};
  int rolling_ic_window{20};
  int random_baseline_trials{200};
};

/// Miner 产物报告（用于后续 Integrator 对接）。
struct MinerReport {
  std::string factor_set_version;
  int random_seed{42};
  std::string search_space_version{"ts_ops_v1"};
  std::vector<RankedFactor> factors;
  std::vector<std::string> candidate_expressions;
  IcSummary random_baseline_oos_abs_ic{};
  int random_baseline_trials{0};
  double oos_random_baseline_threshold_p90{0.0};
  double top_factor_oos_abs_ic{0.0};
  bool oos_not_worse_than_random{false};
};

/**
 * @brief 阶段 R1：离线因子挖掘器（MVP）
 *
 * 当前实现目标：
 * 1. 提供可执行、可复现的候选公式评估链路；
 * 2. 覆盖 `ts_delay/ts_delta/ts_rank/ts_corr` 的表达能力；
 * 3. 以 Spearman IC + 复杂度惩罚输出可审计排序结果。
 */
class Miner {
 public:
  MinerReport Run(const std::vector<ResearchBar>& bars,
                  const MinerConfig& config) const;
};

/**
 * @brief 从 CSV 加载研究样本
 *
 * 期望列：`timestamp,open,high,low,close,volume`（大小写不敏感，timestamp 可选）
 */
bool LoadResearchBarsFromCsv(const std::string& file_path,
                             std::vector<ResearchBar>* out_bars,
                             std::string* out_error);

/// 将 Miner 报告落盘为 JSON 文本，供审计和后续训练使用。
bool SaveMinerReport(const MinerReport& report,
                     const std::string& file_path,
                     std::string* out_error);

}  // namespace ai_trade::research
