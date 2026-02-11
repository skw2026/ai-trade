#pragma once

#include <vector>

namespace ai_trade::research {

/**
 * @brief 时序延迟算子：输出 x[t-d]
 *
 * 约束：
 * 1. 输出长度与输入一致；
 * 2. 前 d 个位置因缺少历史样本返回 NaN；
 * 3. d<=0 视为无效参数，返回全 NaN。
 */
std::vector<double> TsDelay(const std::vector<double>& series, int delay);

/**
 * @brief 时序差分算子：输出 x[t]-x[t-d]
 *
 * 约束：
 * 1. 输出长度与输入一致；
 * 2. 任一输入无效（NaN/Inf）时输出 NaN；
 * 3. d<=0 视为无效参数，返回全 NaN。
 */
std::vector<double> TsDelta(const std::vector<double>& series, int delay);

/**
 * @brief 滚动排名算子：输出 x[t] 在窗口 [t-d+1, t] 内的分位排名
 *
 * 返回区间：[0, 1]。窗口不足或窗口内存在无效值时输出 NaN。
 */
std::vector<double> TsRank(const std::vector<double>& series, int window);

/**
 * @brief 滚动相关系数算子：输出 corr(x, y) over [t-d+1, t]
 *
 * 返回区间：[-1, 1]。窗口不足、窗口含无效值或方差为 0 时输出 NaN。
 */
std::vector<double> TsCorr(const std::vector<double>& lhs,
                           const std::vector<double>& rhs,
                           int window);

/// 标准有限数值判断：用于过滤 NaN/Inf。
bool IsFinite(double value);

}  // namespace ai_trade::research
