#include "research/ic_evaluator.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>
#include <utility>
#include <vector>

#include "research/time_series_operators.h"

namespace ai_trade::research {

namespace {

double NaN() {
  return std::numeric_limits<double>::quiet_NaN();
}

double PearsonCorrelation(const std::vector<double>& lhs,
                          const std::vector<double>& rhs) {
  const std::size_t n = lhs.size();
  if (n < 2 || n != rhs.size()) {
    return NaN();
  }

  const double lhs_mean =
      std::accumulate(lhs.begin(), lhs.end(), 0.0) / static_cast<double>(n);
  const double rhs_mean =
      std::accumulate(rhs.begin(), rhs.end(), 0.0) / static_cast<double>(n);

  double cov = 0.0;
  double lhs_var = 0.0;
  double rhs_var = 0.0;
  for (std::size_t i = 0; i < n; ++i) {
    const double lhs_centered = lhs[i] - lhs_mean;
    const double rhs_centered = rhs[i] - rhs_mean;
    cov += lhs_centered * rhs_centered;
    lhs_var += lhs_centered * lhs_centered;
    rhs_var += rhs_centered * rhs_centered;
  }
  if (lhs_var <= 0.0 || rhs_var <= 0.0) {
    return NaN();
  }
  return cov / std::sqrt(lhs_var * rhs_var);
}

std::vector<double> RankWithAverageTies(const std::vector<double>& values) {
  const std::size_t n = values.size();
  std::vector<double> ranks(n, 0.0);
  if (n == 0) {
    return ranks;
  }

  std::vector<std::pair<double, std::size_t>> indexed;
  indexed.reserve(n);
  for (std::size_t i = 0; i < n; ++i) {
    indexed.emplace_back(values[i], i);
  }
  std::sort(indexed.begin(),
            indexed.end(),
            [](const auto& lhs, const auto& rhs) { return lhs.first < rhs.first; });

  std::size_t i = 0;
  while (i < n) {
    std::size_t j = i + 1;
    while (j < n && indexed[j].first == indexed[i].first) {
      ++j;
    }
    // 排名从 1 开始，重复值使用平均秩。
    const double avg_rank =
        0.5 * (static_cast<double>(i + 1) + static_cast<double>(j));
    for (std::size_t k = i; k < j; ++k) {
      ranks[indexed[k].second] = avg_rank;
    }
    i = j;
  }
  return ranks;
}

double Quantile(std::vector<double> values, double q) {
  values.erase(std::remove_if(values.begin(),
                              values.end(),
                              [](double value) { return !IsFinite(value); }),
               values.end());
  if (values.empty()) {
    return 0.0;
  }
  if (q <= 0.0) {
    return *std::min_element(values.begin(), values.end());
  }
  if (q >= 1.0) {
    return *std::max_element(values.begin(), values.end());
  }
  std::sort(values.begin(), values.end());
  const double pos = q * static_cast<double>(values.size() - 1);
  const std::size_t lo = static_cast<std::size_t>(std::floor(pos));
  const std::size_t hi = static_cast<std::size_t>(std::ceil(pos));
  if (lo == hi) {
    return values[lo];
  }
  const double weight = pos - static_cast<double>(lo);
  return values[lo] * (1.0 - weight) + values[hi] * weight;
}

}  // namespace

SpearmanIcResult ComputeSpearmanIC(const std::vector<double>& factor_values,
                                   const std::vector<double>& future_returns) {
  SpearmanIcResult result;
  if (factor_values.size() != future_returns.size()) {
    return result;
  }

  std::vector<double> lhs;
  std::vector<double> rhs;
  lhs.reserve(factor_values.size());
  rhs.reserve(future_returns.size());
  for (std::size_t i = 0; i < factor_values.size(); ++i) {
    if (!IsFinite(factor_values[i]) || !IsFinite(future_returns[i])) {
      continue;
    }
    lhs.push_back(factor_values[i]);
    rhs.push_back(future_returns[i]);
  }
  result.sample_count = static_cast<int>(lhs.size());
  if (lhs.size() < 3) {
    return result;
  }

  const std::vector<double> lhs_rank = RankWithAverageTies(lhs);
  const std::vector<double> rhs_rank = RankWithAverageTies(rhs);
  const double corr = PearsonCorrelation(lhs_rank, rhs_rank);
  if (IsFinite(corr)) {
    result.ic = corr;
  }
  return result;
}

std::vector<double> ComputeRollingSpearmanIC(
    const std::vector<double>& factor_values,
    const std::vector<double>& future_returns,
    int window) {
  std::vector<double> out(factor_values.size(), NaN());
  if (factor_values.size() != future_returns.size() || window <= 2) {
    return out;
  }

  const std::size_t w = static_cast<std::size_t>(window);
  if (w > factor_values.size()) {
    return out;
  }

  for (std::size_t end = w - 1; end < factor_values.size(); ++end) {
    const std::size_t begin = end + 1 - w;
    std::vector<double> f_slice;
    std::vector<double> r_slice;
    f_slice.reserve(w);
    r_slice.reserve(w);
    for (std::size_t i = begin; i <= end; ++i) {
      f_slice.push_back(factor_values[i]);
      r_slice.push_back(future_returns[i]);
    }
    const SpearmanIcResult ic = ComputeSpearmanIC(f_slice, r_slice);
    if (ic.sample_count >= 3) {
      out[end] = ic.ic;
    }
  }
  return out;
}

IcSummary SummarizeIcSeries(const std::vector<double>& ic_series) {
  IcSummary summary;
  std::vector<double> values;
  values.reserve(ic_series.size());
  for (double ic : ic_series) {
    if (IsFinite(ic)) {
      values.push_back(ic);
    }
  }
  summary.sample_count = static_cast<int>(values.size());
  if (values.empty()) {
    return summary;
  }

  const double mean =
      std::accumulate(values.begin(), values.end(), 0.0) /
      static_cast<double>(values.size());
  double var = 0.0;
  for (double value : values) {
    const double centered = value - mean;
    var += centered * centered;
  }
  var /= static_cast<double>(values.size());

  summary.mean = mean;
  summary.stdev = std::sqrt(var);
  summary.p10 = Quantile(values, 0.10);
  summary.p50 = Quantile(values, 0.50);
  summary.p90 = Quantile(values, 0.90);
  return summary;
}

}  // namespace ai_trade::research
