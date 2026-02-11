#include "research/miner.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <limits>
#include <random>
#include <sstream>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "research/ic_evaluator.h"
#include "research/time_series_operators.h"

namespace ai_trade::research {

namespace {

double NaN() {
  return std::numeric_limits<double>::quiet_NaN();
}

std::string ToLower(std::string text) {
  std::transform(text.begin(),
                 text.end(),
                 text.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return text;
}

std::string Trim(const std::string& text) {
  std::size_t begin = 0;
  while (begin < text.size() &&
         std::isspace(static_cast<unsigned char>(text[begin])) != 0) {
    ++begin;
  }
  std::size_t end = text.size();
  while (end > begin &&
         std::isspace(static_cast<unsigned char>(text[end - 1])) != 0) {
    --end;
  }
  return text.substr(begin, end - begin);
}

std::vector<std::string> SplitCsvLine(const std::string& line) {
  // MVP 阶段 CSV 解析：按逗号切分，不处理复杂引号嵌套。
  std::vector<std::string> out;
  std::string token;
  std::istringstream iss(line);
  while (std::getline(iss, token, ',')) {
    out.push_back(Trim(token));
  }
  return out;
}

bool ParseDouble(const std::string& text, double* out_value) {
  if (out_value == nullptr) {
    return false;
  }
  std::istringstream iss(text);
  double value = 0.0;
  iss >> value;
  if (iss.fail() || !iss.eof()) {
    return false;
  }
  *out_value = value;
  return true;
}

bool ParseInt64(const std::string& text, std::int64_t* out_value) {
  if (out_value == nullptr) {
    return false;
  }
  std::istringstream iss(text);
  std::int64_t value = 0;
  iss >> value;
  if (iss.fail() || !iss.eof()) {
    return false;
  }
  *out_value = value;
  return true;
}

std::string JsonEscape(const std::string& input) {
  std::ostringstream oss;
  for (char ch : input) {
    switch (ch) {
      case '\\':
        oss << "\\\\";
        break;
      case '"':
        oss << "\\\"";
        break;
      case '\n':
        oss << "\\n";
        break;
      case '\r':
        oss << "\\r";
        break;
      case '\t':
        oss << "\\t";
        break;
      default:
        oss << ch;
        break;
    }
  }
  return oss.str();
}

double SafeDiv(double lhs, double rhs) {
  if (!IsFinite(lhs) || !IsFinite(rhs) || std::fabs(rhs) < 1e-12) {
    return NaN();
  }
  return lhs / rhs;
}

std::vector<double> ElementWiseBinary(
    const std::vector<double>& lhs,
    const std::vector<double>& rhs,
    const std::function<double(double, double)>& fn) {
  const std::size_t n = std::min(lhs.size(), rhs.size());
  std::vector<double> out(n, NaN());
  for (std::size_t i = 0; i < n; ++i) {
    out[i] = fn(lhs[i], rhs[i]);
  }
  return out;
}

std::vector<double> ElementWiseAbs(const std::vector<double>& values) {
  std::vector<double> out(values.size(), NaN());
  for (std::size_t i = 0; i < values.size(); ++i) {
    if (!IsFinite(values[i])) {
      continue;
    }
    out[i] = std::fabs(values[i]);
  }
  return out;
}

std::vector<double> BuildForwardReturns(const std::vector<double>& close) {
  std::vector<double> out(close.size(), NaN());
  if (close.size() < 2) {
    return out;
  }
  for (std::size_t i = 0; i + 1 < close.size(); ++i) {
    out[i] = SafeDiv(close[i + 1] - close[i], close[i]);
  }
  return out;
}

std::uint64_t Fnv1a64(const std::string& text) {
  constexpr std::uint64_t kOffset = 1469598103934665603ULL;
  constexpr std::uint64_t kPrime = 1099511628211ULL;
  std::uint64_t hash = kOffset;
  for (unsigned char c : text) {
    hash ^= static_cast<std::uint64_t>(c);
    hash *= kPrime;
  }
  return hash;
}

struct Candidate {
  std::string expression;
  std::vector<double> values;
  double complexity{1.0};
};

struct CandidateEval {
  std::string expression;
  double ic_train{0.0};
  double ic_oos{0.0};
  double complexity{1.0};
  double objective{0.0};
  IcSummary rolling_ic_train{};
  IcSummary rolling_ic_oos{};
};

SpearmanIcResult ComputeIcInRange(const std::vector<double>& factor,
                                  const std::vector<double>& future_returns,
                                  std::size_t begin,
                                  std::size_t end) {
  if (begin >= end || end > factor.size() || end > future_returns.size()) {
    return {};
  }
  std::vector<double> f;
  std::vector<double> r;
  f.reserve(end - begin);
  r.reserve(end - begin);
  for (std::size_t i = begin; i < end; ++i) {
    f.push_back(factor[i]);
    r.push_back(future_returns[i]);
  }
  return ComputeSpearmanIC(f, r);
}

std::vector<double> Slice(const std::vector<double>& values,
                          std::size_t begin,
                          std::size_t end) {
  if (begin >= end || begin >= values.size()) {
    return {};
  }
  const std::size_t safe_end = std::min(end, values.size());
  return std::vector<double>(values.begin() + static_cast<std::ptrdiff_t>(begin),
                             values.begin() + static_cast<std::ptrdiff_t>(safe_end));
}

IcSummary ComputeRollingSummaryInRange(const std::vector<double>& factor_values,
                                       const std::vector<double>& future_returns,
                                       int window,
                                       std::size_t begin,
                                       std::size_t end) {
  if (window <= 2 || begin >= end || end > factor_values.size() ||
      end > future_returns.size()) {
    return {};
  }
  const std::vector<double> f = Slice(factor_values, begin, end);
  const std::vector<double> r = Slice(future_returns, begin, end);
  const std::vector<double> ic_series = ComputeRollingSpearmanIC(f, r, window);
  return SummarizeIcSeries(ic_series);
}

IcSummary BuildRandomBaselineAbsIcSummary(
    const std::vector<double>& future_returns,
    std::size_t begin,
    std::size_t end,
    int random_seed,
    int trials) {
  std::vector<double> abs_ics;
  if (begin >= end || end > future_returns.size() || trials <= 0) {
    return {};
  }
  const std::vector<double> returns_slice = Slice(future_returns, begin, end);
  if (returns_slice.size() < 5) {
    return {};
  }

  std::mt19937 rng(static_cast<std::mt19937::result_type>(random_seed));
  std::normal_distribution<double> dist(0.0, 1.0);
  abs_ics.reserve(static_cast<std::size_t>(trials));
  for (int i = 0; i < trials; ++i) {
    std::vector<double> random_factor(returns_slice.size(), NaN());
    for (double& value : random_factor) {
      value = dist(rng);
    }
    const SpearmanIcResult ic = ComputeSpearmanIC(random_factor, returns_slice);
    if (ic.sample_count >= 3 && IsFinite(ic.ic)) {
      abs_ics.push_back(std::fabs(ic.ic));
    }
  }
  return SummarizeIcSeries(abs_ics);
}

std::vector<Candidate> BuildCandidates(const std::vector<double>& close,
                                       const std::vector<double>& volume) {
  std::vector<Candidate> candidates;

  const std::vector<double> delay1 = TsDelay(close, 1);
  const std::vector<double> delta1 = TsDelta(close, 1);
  const std::vector<double> delta3 = TsDelta(close, 3);
  const std::vector<double> vdelta1 = TsDelta(volume, 1);
  const std::vector<double> rank5 = TsRank(close, 5);
  const std::vector<double> rank10 = TsRank(close, 10);
  const std::vector<double> rank20 = TsRank(close, 20);
  const std::vector<double> corr_cv_10 = TsCorr(close, volume, 10);
  const std::vector<double> corr_dv_10 = TsCorr(delta1, vdelta1, 10);
  const std::vector<double> rank_delta1_10 = TsRank(delta1, 10);

  candidates.push_back({"ts_delay(close,1)", delay1, 1.0});
  candidates.push_back({"ts_delta(close,1)", delta1, 1.0});
  candidates.push_back({"ts_delta(close,3)", delta3, 1.0});
  candidates.push_back({"ts_rank(close,10)", rank10, 1.0});
  candidates.push_back({"ts_corr(close,volume,10)", corr_cv_10, 1.0});
  candidates.push_back({"ts_rank(ts_delta(close,1),10)", rank_delta1_10, 2.0});
  candidates.push_back({"ts_corr(ts_delta(close,1),ts_delta(volume,1),10)",
                        corr_dv_10,
                        3.0});

  candidates.push_back({"ts_delta(close,1)-ts_delta(close,3)",
                        ElementWiseBinary(delta1,
                                          delta3,
                                          [](double a, double b) {
                                            if (!IsFinite(a) || !IsFinite(b)) {
                                              return NaN();
                                            }
                                            return a - b;
                                          }),
                        2.0});

  candidates.push_back({"ts_rank(close,20)-ts_rank(close,5)",
                        ElementWiseBinary(rank20,
                                          rank5,
                                          [](double a, double b) {
                                            if (!IsFinite(a) || !IsFinite(b)) {
                                              return NaN();
                                            }
                                            return a - b;
                                          }),
                        2.0});

  candidates.push_back(
      {"ts_delta(close,1)/(abs(ts_delay(close,1))+1e-9)",
       ElementWiseBinary(delta1,
                         ElementWiseAbs(delay1),
                         [](double a, double b) { return SafeDiv(a, b + 1e-9); }),
       2.0});

  return candidates;
}

}  // namespace

MinerReport Miner::Run(const std::vector<ResearchBar>& bars,
                       const MinerConfig& config) const {
  MinerReport report;
  if (bars.size() < 30) {
    return report;
  }
  report.random_seed = config.random_seed;
  report.search_space_version = "ts_ops_v1";
  report.random_baseline_trials = std::max(0, config.random_baseline_trials);

  std::vector<double> close;
  std::vector<double> volume;
  close.reserve(bars.size());
  volume.reserve(bars.size());
  for (const ResearchBar& bar : bars) {
    close.push_back(bar.close);
    volume.push_back(bar.volume);
  }
  const std::vector<double> future_returns = BuildForwardReturns(close);
  const std::vector<Candidate> candidates = BuildCandidates(close, volume);
  report.candidate_expressions.reserve(candidates.size());
  for (const Candidate& candidate : candidates) {
    report.candidate_expressions.push_back(candidate.expression);
  }

  std::size_t split_index =
      static_cast<std::size_t>(config.train_split_ratio *
                               static_cast<double>(bars.size()));
  // 为 OOS 留出最小样本，避免 split 极端导致结果无意义。
  split_index = std::clamp<std::size_t>(split_index, 10, bars.size() - 10);

  std::vector<CandidateEval> evaluations;
  evaluations.reserve(candidates.size());
  const int rolling_window = std::max(3, config.rolling_ic_window);
  for (const Candidate& candidate : candidates) {
    const SpearmanIcResult train_ic =
        ComputeIcInRange(candidate.values, future_returns, 0, split_index);
    const SpearmanIcResult oos_ic =
        ComputeIcInRange(candidate.values, future_returns, split_index, bars.size());
    const IcSummary rolling_train = ComputeRollingSummaryInRange(
        candidate.values, future_returns, rolling_window, 0, split_index);
    const IcSummary rolling_oos = ComputeRollingSummaryInRange(
        candidate.values, future_returns, rolling_window, split_index, bars.size());
    const double objective =
        std::fabs(oos_ic.ic) - config.complexity_penalty * candidate.complexity;
    evaluations.push_back({candidate.expression,
                           train_ic.ic,
                           oos_ic.ic,
                           candidate.complexity,
                           objective,
                           rolling_train,
                           rolling_oos});
  }

  std::sort(evaluations.begin(),
            evaluations.end(),
            [](const CandidateEval& lhs, const CandidateEval& rhs) {
              if (lhs.objective != rhs.objective) {
                return lhs.objective > rhs.objective;
              }
              return lhs.expression < rhs.expression;
            });

  const std::size_t top_k = std::max<std::size_t>(1, config.top_k);
  const std::size_t count = std::min<std::size_t>(top_k, evaluations.size());
  report.factors.reserve(count);
  for (std::size_t i = 0; i < count; ++i) {
    const CandidateEval& eval = evaluations[i];
    report.factors.push_back({eval.expression,
                              eval.ic_train,
                              eval.ic_oos,
                              eval.complexity,
                              eval.objective,
                              eval.ic_oos < 0.0,
                              "ALL",
                              config.random_seed,
                              "ts_ops_v1",
                              eval.rolling_ic_train,
                              eval.rolling_ic_oos});
  }

  report.random_baseline_oos_abs_ic = BuildRandomBaselineAbsIcSummary(
      future_returns,
      split_index,
      bars.size(),
      config.random_seed,
      report.random_baseline_trials);
  report.oos_random_baseline_threshold_p90 =
      report.random_baseline_oos_abs_ic.p90;
  if (!report.factors.empty()) {
    report.top_factor_oos_abs_ic = std::fabs(report.factors.front().fitness_ic_oos);
  }
  report.oos_not_worse_than_random =
      report.top_factor_oos_abs_ic >= report.oos_random_baseline_threshold_p90;

  std::ostringstream id_seed;
  id_seed << "seed=" << config.random_seed << "|bars=" << bars.size()
          << "|top=" << count;
  for (const RankedFactor& factor : report.factors) {
    id_seed << "|" << factor.expression << "|" << std::fixed
            << std::setprecision(6) << factor.fitness_ic_oos;
  }
  const std::uint64_t hash = Fnv1a64(id_seed.str());
  std::ostringstream version;
  version << "factor_set_v1_" << std::hex << hash;
  report.factor_set_version = version.str();
  return report;
}

bool LoadResearchBarsFromCsv(const std::string& file_path,
                             std::vector<ResearchBar>* out_bars,
                             std::string* out_error) {
  if (out_bars == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_bars 为空";
    }
    return false;
  }

  std::ifstream input(file_path);
  if (!input.is_open()) {
    if (out_error != nullptr) {
      *out_error = "无法打开研究数据文件: " + file_path;
    }
    return false;
  }

  std::string header_line;
  if (!std::getline(input, header_line)) {
    if (out_error != nullptr) {
      *out_error = "研究数据文件为空";
    }
    return false;
  }
  const std::vector<std::string> headers = SplitCsvLine(header_line);
  if (headers.empty()) {
    if (out_error != nullptr) {
      *out_error = "CSV 头部为空";
    }
    return false;
  }

  std::unordered_map<std::string, std::size_t> column_index;
  for (std::size_t i = 0; i < headers.size(); ++i) {
    column_index[ToLower(headers[i])] = i;
  }
  const auto find_col = [&](const std::string& key) -> int {
    const auto it = column_index.find(key);
    if (it == column_index.end()) {
      return -1;
    }
    return static_cast<int>(it->second);
  };

  const int open_col = find_col("open");
  const int high_col = find_col("high");
  const int low_col = find_col("low");
  const int close_col = find_col("close");
  const int volume_col = find_col("volume");
  const int ts_col = find_col("timestamp");
  if (open_col < 0 || high_col < 0 || low_col < 0 || close_col < 0 ||
      volume_col < 0) {
    if (out_error != nullptr) {
      *out_error = "CSV 缺少必须列（open/high/low/close/volume）";
    }
    return false;
  }

  std::vector<ResearchBar> bars;
  std::string line;
  int line_no = 1;
  while (std::getline(input, line)) {
    ++line_no;
    if (Trim(line).empty()) {
      continue;
    }
    const std::vector<std::string> cells = SplitCsvLine(line);
    if (cells.size() < headers.size()) {
      if (out_error != nullptr) {
        *out_error = "CSV 行字段数量不足，行号: " + std::to_string(line_no);
      }
      return false;
    }

    ResearchBar bar;
    if (ts_col >= 0) {
      if (!ParseInt64(cells[static_cast<std::size_t>(ts_col)], &bar.ts_ms)) {
        if (out_error != nullptr) {
          *out_error = "timestamp 解析失败，行号: " + std::to_string(line_no);
        }
        return false;
      }
    } else {
      bar.ts_ms = static_cast<std::int64_t>(bars.size());
    }

    if (!ParseDouble(cells[static_cast<std::size_t>(open_col)], &bar.open) ||
        !ParseDouble(cells[static_cast<std::size_t>(high_col)], &bar.high) ||
        !ParseDouble(cells[static_cast<std::size_t>(low_col)], &bar.low) ||
        !ParseDouble(cells[static_cast<std::size_t>(close_col)], &bar.close) ||
        !ParseDouble(cells[static_cast<std::size_t>(volume_col)], &bar.volume)) {
      if (out_error != nullptr) {
        *out_error = "OHLCV 解析失败，行号: " + std::to_string(line_no);
      }
      return false;
    }
    bars.push_back(bar);
  }

  if (bars.size() < 30) {
    if (out_error != nullptr) {
      *out_error = "研究样本不足，至少需要 30 条";
    }
    return false;
  }
  *out_bars = std::move(bars);
  return true;
}

bool SaveMinerReport(const MinerReport& report,
                     const std::string& file_path,
                     std::string* out_error) {
  std::error_code ec;
  const std::filesystem::path path(file_path);
  if (path.has_parent_path()) {
    std::filesystem::create_directories(path.parent_path(), ec);
    if (ec) {
      if (out_error != nullptr) {
        *out_error = "创建目录失败: " + ec.message();
      }
      return false;
    }
  }

  std::ofstream out(file_path);
  if (!out.is_open()) {
    if (out_error != nullptr) {
      *out_error = "无法写入报告文件: " + file_path;
    }
    return false;
  }

  const auto write_summary =
      [&out](const IcSummary& summary, int indent_spaces = 6) {
        const std::string pad(static_cast<std::size_t>(indent_spaces), ' ');
        out << pad << "{\n";
        out << pad << "  \"mean\": " << std::fixed << std::setprecision(8)
            << summary.mean << ",\n";
        out << pad << "  \"stdev\": " << std::fixed << std::setprecision(8)
            << summary.stdev << ",\n";
        out << pad << "  \"p10\": " << std::fixed << std::setprecision(8)
            << summary.p10 << ",\n";
        out << pad << "  \"p50\": " << std::fixed << std::setprecision(8)
            << summary.p50 << ",\n";
        out << pad << "  \"p90\": " << std::fixed << std::setprecision(8)
            << summary.p90 << ",\n";
        out << pad << "  \"sample_count\": " << summary.sample_count << "\n";
        out << pad << "}";
      };

  out << "{\n";
  out << "  \"factor_set_version\": \"" << JsonEscape(report.factor_set_version)
      << "\",\n";
  out << "  \"random_seed\": " << report.random_seed << ",\n";
  out << "  \"search_space_version\": \""
      << JsonEscape(report.search_space_version) << "\",\n";
  out << "  \"candidate_count\": " << report.candidate_expressions.size() << ",\n";
  out << "  \"random_baseline_trials\": " << report.random_baseline_trials
      << ",\n";
  out << "  \"oos_random_baseline_threshold_p90\": " << std::fixed
      << std::setprecision(8) << report.oos_random_baseline_threshold_p90 << ",\n";
  out << "  \"top_factor_oos_abs_ic\": " << std::fixed << std::setprecision(8)
      << report.top_factor_oos_abs_ic << ",\n";
  out << "  \"oos_not_worse_than_random\": "
      << (report.oos_not_worse_than_random ? "true" : "false") << ",\n";
  out << "  \"random_baseline_oos_abs_ic\":\n";
  write_summary(report.random_baseline_oos_abs_ic, 2);
  out << ",\n";
  out << "  \"factors\": [\n";
  for (std::size_t i = 0; i < report.factors.size(); ++i) {
    const RankedFactor& factor = report.factors[i];
    out << "    {\n";
    out << "      \"expression\": \"" << JsonEscape(factor.expression) << "\",\n";
    out << "      \"fitness_ic_train\": " << std::fixed << std::setprecision(8)
        << factor.fitness_ic_train << ",\n";
    out << "      \"fitness_ic_oos\": " << std::fixed << std::setprecision(8)
        << factor.fitness_ic_oos << ",\n";
    out << "      \"complexity_score\": " << std::fixed << std::setprecision(4)
        << factor.complexity_score << ",\n";
    out << "      \"objective_score\": " << std::fixed << std::setprecision(8)
        << factor.objective_score << ",\n";
    out << "      \"invert_signal\": " << (factor.invert_signal ? "true" : "false")
        << ",\n";
    out << "      \"valid_universe\": \"" << JsonEscape(factor.valid_universe)
        << "\",\n";
    out << "      \"random_seed\": " << factor.random_seed << ",\n";
    out << "      \"search_space_version\": \""
        << JsonEscape(factor.search_space_version) << "\",\n";
    out << "      \"rolling_ic_train\":\n";
    write_summary(factor.rolling_ic_train, 6);
    out << ",\n";
    out << "      \"rolling_ic_oos\":\n";
    write_summary(factor.rolling_ic_oos, 6);
    out << "\n";
    out << "    }";
    if (i + 1 < report.factors.size()) {
      out << ",";
    }
    out << "\n";
  }
  out << "  ],\n";
  out << "  \"candidate_expressions\": [\n";
  for (std::size_t i = 0; i < report.candidate_expressions.size(); ++i) {
    out << "    \"" << JsonEscape(report.candidate_expressions[i]) << "\"";
    if (i + 1 < report.candidate_expressions.size()) {
      out << ",";
    }
    out << "\n";
  }
  out << "  ]\n";
  out << "}\n";
  return true;
}

}  // namespace ai_trade::research
