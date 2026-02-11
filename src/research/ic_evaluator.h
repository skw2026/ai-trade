#pragma once

#include <vector>

namespace ai_trade::research {

/// 单次 Spearman IC 结果。
struct SpearmanIcResult {
  double ic{0.0};
  int sample_count{0};
};

/// IC 序列统计摘要（用于训练报告与审计）。
struct IcSummary {
  double mean{0.0};
  double stdev{0.0};
  double p10{0.0};
  double p50{0.0};
  double p90{0.0};
  int sample_count{0};
};

/**
 * @brief 计算 Spearman 等级相关 IC
 *
 * 仅使用双向有限值样本；有效样本不足 3 时返回 0。
 */
SpearmanIcResult ComputeSpearmanIC(const std::vector<double>& factor_values,
                                   const std::vector<double>& future_returns);

/**
 * @brief 计算滚动 Spearman IC 序列
 *
 * 训练时用于评估“IC 的稳定性分布”，输出长度与输入一致。
 * 窗口不足或样本无效位置返回 NaN。
 */
std::vector<double> ComputeRollingSpearmanIC(
    const std::vector<double>& factor_values,
    const std::vector<double>& future_returns,
    int window);

/// 统计 IC 序列分布，自动忽略 NaN/Inf。
IcSummary SummarizeIcSeries(const std::vector<double>& ic_series);

}  // namespace ai_trade::research
