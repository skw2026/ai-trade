#include "research/time_series_operators.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>

namespace ai_trade::research {

namespace {

double NaN() {
  return std::numeric_limits<double>::quiet_NaN();
}

std::vector<double> MakeNaNVector(std::size_t size) {
  return std::vector<double>(size, NaN());
}

double PearsonCorrelation(const std::vector<double>& lhs,
                          const std::vector<double>& rhs) {
  const std::size_t n = lhs.size();
  if (n < 2 || n != rhs.size()) {
    return NaN();
  }

  const double lhs_sum = std::accumulate(lhs.begin(), lhs.end(), 0.0);
  const double rhs_sum = std::accumulate(rhs.begin(), rhs.end(), 0.0);
  const double lhs_mean = lhs_sum / static_cast<double>(n);
  const double rhs_mean = rhs_sum / static_cast<double>(n);

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

}  // namespace

bool IsFinite(double value) {
  return std::isfinite(value) != 0;
}

std::vector<double> TsDelay(const std::vector<double>& series, int delay) {
  std::vector<double> out = MakeNaNVector(series.size());
  if (delay <= 0) {
    return out;
  }
  for (std::size_t i = static_cast<std::size_t>(delay); i < series.size(); ++i) {
    out[i] = series[i - static_cast<std::size_t>(delay)];
  }
  return out;
}

std::vector<double> TsDelta(const std::vector<double>& series, int delay) {
  std::vector<double> out = MakeNaNVector(series.size());
  if (delay <= 0) {
    return out;
  }
  const std::size_t d = static_cast<std::size_t>(delay);
  for (std::size_t i = d; i < series.size(); ++i) {
    const double current = series[i];
    const double history = series[i - d];
    if (!IsFinite(current) || !IsFinite(history)) {
      continue;
    }
    out[i] = current - history;
  }
  return out;
}

std::vector<double> TsRank(const std::vector<double>& series, int window) {
  std::vector<double> out = MakeNaNVector(series.size());
  if (window <= 0) {
    return out;
  }
  const std::size_t w = static_cast<std::size_t>(window);
  if (w > series.size()) {
    return out;
  }

  for (std::size_t end = w - 1; end < series.size(); ++end) {
    const std::size_t begin = end + 1 - w;
    const double current = series[end];
    if (!IsFinite(current)) {
      continue;
    }

    int less = 0;
    int equal = 0;
    bool valid_window = true;
    for (std::size_t i = begin; i <= end; ++i) {
      const double value = series[i];
      if (!IsFinite(value)) {
        valid_window = false;
        break;
      }
      if (value < current) {
        ++less;
      } else if (value == current) {
        ++equal;
      }
    }
    if (!valid_window || equal <= 0) {
      continue;
    }
    // 使用 tie-aware 分位排名，保证重复值可稳定比较。
    const double rank = (static_cast<double>(less) +
                         0.5 * static_cast<double>(equal)) /
                        static_cast<double>(w);
    out[end] = rank;
  }
  return out;
}

std::vector<double> TsCorr(const std::vector<double>& lhs,
                           const std::vector<double>& rhs,
                           int window) {
  std::vector<double> out = MakeNaNVector(lhs.size());
  if (window <= 1 || lhs.size() != rhs.size()) {
    return out;
  }
  const std::size_t w = static_cast<std::size_t>(window);
  if (w > lhs.size()) {
    return out;
  }

  std::vector<double> lhs_window;
  std::vector<double> rhs_window;
  lhs_window.reserve(w);
  rhs_window.reserve(w);

  for (std::size_t end = w - 1; end < lhs.size(); ++end) {
    const std::size_t begin = end + 1 - w;
    lhs_window.clear();
    rhs_window.clear();
    bool valid_window = true;
    for (std::size_t i = begin; i <= end; ++i) {
      if (!IsFinite(lhs[i]) || !IsFinite(rhs[i])) {
        valid_window = false;
        break;
      }
      lhs_window.push_back(lhs[i]);
      rhs_window.push_back(rhs[i]);
    }
    if (!valid_window) {
      continue;
    }
    out[end] = PearsonCorrelation(lhs_window, rhs_window);
  }
  return out;
}

}  // namespace ai_trade::research
