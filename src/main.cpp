#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "app/bot_app.h"
#include "core/config.h"
#include "core/log.h"
#include "research/miner.h"
#include "research/online_feature_engine.h"

namespace {

// 运行时覆盖参数：用于在不改 YAML 的情况下快速切换运行行为。
struct RuntimeOptions {
  std::string config_path{"config/default.yaml"};
  std::string exchange_override;
  std::string data_path_override;
  std::string replay_market_data_path;
  std::string replay_price_column;
  std::string replay_volume_column;
  std::string replay_timestamp_column;
  std::string replay_symbol_column;
  std::string replay_interval_column;
  std::string replay_funding_rate_column;
  std::optional<int> replay_default_interval_ms;
  std::optional<int> max_ticks;
  std::optional<int> status_log_interval_ticks;
  std::optional<int> remote_risk_refresh_interval_ticks;
  bool run_forever{false};
  bool run_miner{false};
  std::string miner_csv_path;
  std::string miner_output_path{"./data/research/miner_report.json"};
  std::optional<int> miner_top_k;
  std::optional<int> miner_generations;
  std::optional<int> miner_population;
  std::optional<int> miner_elite;
  std::optional<int> miner_predict_horizon_bars;
  std::optional<int> miner_execution_latency_bars;
  bool run_feature_parity{false};
  std::string feature_parity_bars_path{
      "tools/fixtures/feature_parity_bars_v1.csv"};
  std::string feature_parity_expected_path{
      "tools/fixtures/feature_parity_expected_v1.tsv"};
  std::string feature_parity_output_path{
      "./data/reports/feature_parity_report.json"};
  bool check_startup{false};
  bool check_exchange{false};
};

bool ParseNonNegativeInt(const std::string& raw, int* out_value) {
  if (out_value == nullptr || raw.empty()) {
    return false;
  }
  try {
    std::size_t consumed = 0;
    const int parsed = std::stoi(raw, &consumed);
    if (consumed != raw.size() || parsed < 0) {
      return false;
    }
    *out_value = parsed;
    return true;
  } catch (...) {
    return false;
  }
}

std::string FormatSymbolList(const std::vector<std::string>& symbols) {
  if (symbols.empty()) {
    return "n/a";
  }
  std::ostringstream oss;
  for (std::size_t i = 0; i < symbols.size(); ++i) {
    if (i > 0) {
      oss << ",";
    }
    oss << symbols[i];
  }
  return oss.str();
}

std::string FormatDouble(double value) {
  std::ostringstream oss;
  oss << std::fixed << std::setprecision(6) << value;
  return oss.str();
}

// 解析形如 `--max_ticks=100` 或独立值字符串中的整型参数。
void ParseOptionalIntArg(const std::string& raw_value,
                         const std::string& option_name,
                         std::optional<int>* out_value) {
  if (out_value == nullptr) {
    return;
  }
  int parsed = 0;
  if (!ParseNonNegativeInt(raw_value, &parsed)) {
    ai_trade::LogInfo(option_name + " 参数非法，已忽略: " + raw_value);
    return;
  }
  *out_value = parsed;
}

/**
 * @brief 解析 CLI 参数
 *
 * 支持：
 * - `--config=...`
 * - `--exchange=...`
 * - `--data_path=...`
 * - `--replay_market_data=...`
 * - `--replay_price_column=...`
 * - `--replay_volume_column=...`
 * - `--replay_timestamp_column=...`
 * - `--replay_symbol_column=...`
 * - `--replay_interval_column=...`
 * - `--replay_funding_rate_column=...`
 * - `--replay_default_interval_ms=...` / `--replay_default_interval_ms ...`
 * - `--max_ticks=...` / `--max_ticks ...`
 * - `--status_log_interval_ticks=...` / `--status_log_interval_ticks ...`
 * - `--remote_risk_refresh_interval_ticks=...` / `--remote_risk_refresh_interval_ticks ...`
 * - `--run_forever`
 * - `--check_startup` / `--check-startup`
 * - `--check_exchange` / `--check-exchange`
 * - `--run_miner --miner_csv=... [--miner_output=...] [--miner_top_k=...]`
 *               [--miner_generations=...] [--miner_population=...]
 *               [--miner_elite=...]
 * - `--run_feature_parity --feature_parity_bars=...`
 *   `--feature_parity_expected=... --feature_parity_output=...`
 */
RuntimeOptions ParseOptions(int argc, char** argv) {
  RuntimeOptions options;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg.rfind("--config=", 0) == 0) {
      options.config_path = arg.substr(std::string("--config=").size());
      continue;
    }
    if (arg.rfind("--exchange=", 0) == 0) {
      options.exchange_override = arg.substr(std::string("--exchange=").size());
      continue;
    }
    if (arg.rfind("--data_path=", 0) == 0) {
      options.data_path_override =
          arg.substr(std::string("--data_path=").size());
      continue;
    }
    if (arg == "--data_path" && i + 1 < argc) {
      ++i;
      options.data_path_override = argv[i];
      continue;
    }
    if (arg.rfind("--replay_market_data=", 0) == 0) {
      options.replay_market_data_path =
          arg.substr(std::string("--replay_market_data=").size());
      continue;
    }
    if (arg == "--replay_market_data" && i + 1 < argc) {
      ++i;
      options.replay_market_data_path = argv[i];
      continue;
    }
    if (arg.rfind("--replay_price_column=", 0) == 0) {
      options.replay_price_column =
          arg.substr(std::string("--replay_price_column=").size());
      continue;
    }
    if (arg.rfind("--replay_volume_column=", 0) == 0) {
      options.replay_volume_column =
          arg.substr(std::string("--replay_volume_column=").size());
      continue;
    }
    if (arg.rfind("--replay_timestamp_column=", 0) == 0) {
      options.replay_timestamp_column =
          arg.substr(std::string("--replay_timestamp_column=").size());
      continue;
    }
    if (arg.rfind("--replay_symbol_column=", 0) == 0) {
      options.replay_symbol_column =
          arg.substr(std::string("--replay_symbol_column=").size());
      continue;
    }
    if (arg.rfind("--replay_interval_column=", 0) == 0) {
      options.replay_interval_column =
          arg.substr(std::string("--replay_interval_column=").size());
      continue;
    }
    if (arg.rfind("--replay_funding_rate_column=", 0) == 0) {
      options.replay_funding_rate_column =
          arg.substr(std::string("--replay_funding_rate_column=").size());
      continue;
    }
    if (arg.rfind("--replay_default_interval_ms=", 0) == 0) {
      ParseOptionalIntArg(
          arg.substr(std::string("--replay_default_interval_ms=").size()),
          "--replay_default_interval_ms",
          &options.replay_default_interval_ms);
      continue;
    }
    if (arg == "--replay_default_interval_ms" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(argv[i],
                          "--replay_default_interval_ms",
                          &options.replay_default_interval_ms);
      continue;
    }
    if (arg.rfind("--max_ticks=", 0) == 0) {
      ParseOptionalIntArg(arg.substr(std::string("--max_ticks=").size()),
                          "--max_ticks",
                          &options.max_ticks);
      continue;
    }
    if (arg == "--max_ticks" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(argv[i], "--max_ticks", &options.max_ticks);
      continue;
    }
    if (arg.rfind("--status_log_interval_ticks=", 0) == 0) {
      ParseOptionalIntArg(
          arg.substr(std::string("--status_log_interval_ticks=").size()),
          "--status_log_interval_ticks",
          &options.status_log_interval_ticks);
      continue;
    }
    if (arg == "--status_log_interval_ticks" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(argv[i],
                          "--status_log_interval_ticks",
                          &options.status_log_interval_ticks);
      continue;
    }
    if (arg.rfind("--remote_risk_refresh_interval_ticks=", 0) == 0) {
      ParseOptionalIntArg(
          arg.substr(std::string("--remote_risk_refresh_interval_ticks=").size()),
          "--remote_risk_refresh_interval_ticks",
          &options.remote_risk_refresh_interval_ticks);
      continue;
    }
    if (arg == "--remote_risk_refresh_interval_ticks" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(argv[i],
                          "--remote_risk_refresh_interval_ticks",
                          &options.remote_risk_refresh_interval_ticks);
      continue;
    }
    if (arg == "--run_forever" || arg == "--run-forever") {
      options.run_forever = true;
      continue;
    }
    if (arg == "--check_startup" || arg == "--check-startup") {
      options.check_startup = true;
      continue;
    }
    if (arg == "--check_exchange" || arg == "--check-exchange") {
      options.check_exchange = true;
      continue;
    }
    if (arg == "--run_miner" || arg == "--run-miner") {
      options.run_miner = true;
      continue;
    }
    if (arg == "--run_feature_parity" || arg == "--run-feature-parity") {
      options.run_feature_parity = true;
      continue;
    }
    if (arg.rfind("--feature_parity_bars=", 0) == 0) {
      options.feature_parity_bars_path =
          arg.substr(std::string("--feature_parity_bars=").size());
      continue;
    }
    if (arg.rfind("--feature_parity_expected=", 0) == 0) {
      options.feature_parity_expected_path =
          arg.substr(std::string("--feature_parity_expected=").size());
      continue;
    }
    if (arg.rfind("--feature_parity_output=", 0) == 0) {
      options.feature_parity_output_path =
          arg.substr(std::string("--feature_parity_output=").size());
      continue;
    }
    if (arg.rfind("--miner_csv=", 0) == 0) {
      options.miner_csv_path = arg.substr(std::string("--miner_csv=").size());
      continue;
    }
    if (arg == "--miner_csv" && i + 1 < argc) {
      ++i;
      options.miner_csv_path = argv[i];
      continue;
    }
    if (arg.rfind("--miner_output=", 0) == 0) {
      options.miner_output_path =
          arg.substr(std::string("--miner_output=").size());
      continue;
    }
    if (arg == "--miner_output" && i + 1 < argc) {
      ++i;
      options.miner_output_path = argv[i];
      continue;
    }
    if (arg.rfind("--miner_top_k=", 0) == 0) {
      ParseOptionalIntArg(arg.substr(std::string("--miner_top_k=").size()),
                          "--miner_top_k",
                          &options.miner_top_k);
      continue;
    }
    if (arg == "--miner_top_k" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(argv[i], "--miner_top_k", &options.miner_top_k);
      continue;
    }
    if (arg.rfind("--miner_generations=", 0) == 0) {
      ParseOptionalIntArg(arg.substr(std::string("--miner_generations=").size()),
                          "--miner_generations",
                          &options.miner_generations);
      continue;
    }
    if (arg == "--miner_generations" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(
          argv[i], "--miner_generations", &options.miner_generations);
      continue;
    }
    if (arg.rfind("--miner_population=", 0) == 0) {
      ParseOptionalIntArg(arg.substr(std::string("--miner_population=").size()),
                          "--miner_population",
                          &options.miner_population);
      continue;
    }
    if (arg == "--miner_population" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(argv[i], "--miner_population", &options.miner_population);
      continue;
    }
    if (arg.rfind("--miner_elite=", 0) == 0) {
      ParseOptionalIntArg(arg.substr(std::string("--miner_elite=").size()),
                          "--miner_elite",
                          &options.miner_elite);
      continue;
    }
    if (arg == "--miner_elite" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(argv[i], "--miner_elite", &options.miner_elite);
      continue;
    }
    if (arg.rfind("--miner_predict_horizon_bars=", 0) == 0) {
      ParseOptionalIntArg(
          arg.substr(std::string("--miner_predict_horizon_bars=").size()),
          "--miner_predict_horizon_bars",
          &options.miner_predict_horizon_bars);
      continue;
    }
    if (arg == "--miner_predict_horizon_bars" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(argv[i],
                          "--miner_predict_horizon_bars",
                          &options.miner_predict_horizon_bars);
      continue;
    }
    if (arg.rfind("--miner_execution_latency_bars=", 0) == 0) {
      ParseOptionalIntArg(
          arg.substr(std::string("--miner_execution_latency_bars=").size()),
          "--miner_execution_latency_bars",
          &options.miner_execution_latency_bars);
      continue;
    }
    if (arg == "--miner_execution_latency_bars" && i + 1 < argc) {
      ++i;
      ParseOptionalIntArg(argv[i],
                          "--miner_execution_latency_bars",
                          &options.miner_execution_latency_bars);
      continue;
    }
  }
  return options;
}

// 将 CLI 参数覆盖到 YAML 配置（CLI 优先级更高）。
void ApplyRuntimeOverrides(const RuntimeOptions& options,
                           ai_trade::AppConfig* config) {
  if (config == nullptr) {
    return;
  }
  if (!options.exchange_override.empty()) {
    config->exchange = options.exchange_override;
  }
  if (!options.data_path_override.empty()) {
    config->data_path = options.data_path_override;
  }
  if (!options.replay_market_data_path.empty()) {
    config->bybit.replay_market_data_path = options.replay_market_data_path;
  }
  if (!options.replay_price_column.empty()) {
    config->bybit.replay_price_column = options.replay_price_column;
  }
  if (!options.replay_volume_column.empty()) {
    config->bybit.replay_volume_column = options.replay_volume_column;
  }
  if (!options.replay_timestamp_column.empty()) {
    config->bybit.replay_timestamp_column = options.replay_timestamp_column;
  }
  if (!options.replay_symbol_column.empty()) {
    config->bybit.replay_symbol_column = options.replay_symbol_column;
  }
  if (!options.replay_interval_column.empty()) {
    config->bybit.replay_interval_column = options.replay_interval_column;
  }
  if (!options.replay_funding_rate_column.empty()) {
    config->bybit.replay_funding_rate_column =
        options.replay_funding_rate_column;
  }
  if (options.replay_default_interval_ms.has_value()) {
    config->bybit.replay_default_interval_ms =
        *options.replay_default_interval_ms;
  }
  if (options.max_ticks.has_value()) {
    config->system_max_ticks = *options.max_ticks;
  }
  if (options.status_log_interval_ticks.has_value()) {
    config->system_status_log_interval_ticks = *options.status_log_interval_ticks;
  }
  if (options.remote_risk_refresh_interval_ticks.has_value()) {
    config->system_remote_risk_refresh_interval_ticks =
        *options.remote_risk_refresh_interval_ticks;
  }
  if (options.run_forever) {
    config->system_max_ticks = 0;
  }
}

/**
 * @brief 执行离线 Miner（R1）并退出
 *
 * 该入口与交易闭环完全解耦，用于快速做因子挖掘实验与可复现验收。
 */
int RunOfflineMiner(const RuntimeOptions& options) {
  if (options.miner_csv_path.empty()) {
    ai_trade::LogError(
        "未提供 Miner 数据文件，请使用 --miner_csv=<path/to/ohlcv.csv>");
    return 1;
  }

  std::vector<ai_trade::research::ResearchBar> bars;
  std::string error;
  if (!ai_trade::research::LoadResearchBarsFromCsv(options.miner_csv_path, &bars,
                                                   &error)) {
    ai_trade::LogError("Miner 数据加载失败: " + error);
    return 1;
  }

  ai_trade::research::MinerConfig miner_config;
  if (options.miner_top_k.has_value() && *options.miner_top_k > 0) {
    miner_config.top_k = static_cast<std::size_t>(*options.miner_top_k);
  }
  if (options.miner_generations.has_value() && *options.miner_generations > 0) {
    miner_config.generations = *options.miner_generations;
  }
  if (options.miner_population.has_value() && *options.miner_population > 0) {
    miner_config.population_size = *options.miner_population;
  }
  if (options.miner_elite.has_value() && *options.miner_elite > 0) {
    miner_config.elite_size = *options.miner_elite;
  }
  if (options.miner_predict_horizon_bars.has_value() &&
      *options.miner_predict_horizon_bars > 0) {
    miner_config.predict_horizon_bars =
        *options.miner_predict_horizon_bars;
  }
  if (options.miner_execution_latency_bars.has_value() &&
      *options.miner_execution_latency_bars >= 0) {
    miner_config.execution_latency_bars =
        *options.miner_execution_latency_bars;
  }
  ai_trade::LogInfo("MINER_START: bars=" + std::to_string(bars.size()) +
                    ", top_k=" + std::to_string(miner_config.top_k) +
                    ", generations=" + std::to_string(miner_config.generations) +
                    ", population=" + std::to_string(miner_config.population_size) +
                    ", elite=" + std::to_string(miner_config.elite_size) +
                    ", horizon=" +
                    std::to_string(miner_config.predict_horizon_bars) +
                    ", latency=" +
                    std::to_string(miner_config.execution_latency_bars));

  ai_trade::research::Miner miner;
  const ai_trade::research::MinerReport report = miner.Run(bars, miner_config);
  if (report.factors.empty()) {
    ai_trade::LogError("Miner 运行完成但未产出有效因子，请检查样本质量与窗口大小");
    return 1;
  }

  if (!ai_trade::research::SaveMinerReport(report, options.miner_output_path,
                                           &error)) {
    ai_trade::LogError("Miner 报告写入失败: " + error);
    return 1;
  }

  ai_trade::LogInfo("MINER_DONE: factor_set_version=" + report.factor_set_version +
                    ", factors=" + std::to_string(report.factors.size()) +
                    ", output=" + options.miner_output_path);
  return 0;
}

struct FeatureParityBar {
  double open{0.0};
  double high{0.0};
  double low{0.0};
  double close{0.0};
  double volume{0.0};
};

struct FeatureParityExpectation {
  std::size_t sample_count{0};
  std::string feature;
  std::string expression;
  double expected{0.0};
  double tolerance{0.0};
};

std::vector<std::string> SplitLine(const std::string& line, char delimiter) {
  std::vector<std::string> fields;
  std::stringstream stream(line);
  std::string field;
  while (std::getline(stream, field, delimiter)) {
    fields.push_back(field);
  }
  return fields;
}

std::string EscapeJson(const std::string& value) {
  std::string escaped;
  escaped.reserve(value.size());
  for (const char ch : value) {
    if (ch == '\\' || ch == '"') {
      escaped.push_back('\\');
    }
    escaped.push_back(ch);
  }
  return escaped;
}

bool LoadFeatureParityBars(const std::string& path,
                           std::vector<FeatureParityBar>* out,
                           std::string* error) {
  std::ifstream input(path);
  if (!input.is_open()) {
    *error = "cannot open bars fixture: " + path;
    return false;
  }
  std::string line;
  std::getline(input, line);
  while (std::getline(input, line)) {
    const auto fields = SplitLine(line, ',');
    if (fields.size() != 6U) {
      *error = "invalid bars fixture row";
      return false;
    }
    try {
      out->push_back({std::stod(fields[1]),
                      std::stod(fields[2]),
                      std::stod(fields[3]),
                      std::stod(fields[4]),
                      std::stod(fields[5])});
    } catch (...) {
      *error = "invalid numeric value in bars fixture";
      return false;
    }
  }
  if (out->empty()) {
    *error = "bars fixture is empty";
    return false;
  }
  return true;
}

bool LoadFeatureParityExpectations(
    const std::string& path,
    std::vector<FeatureParityExpectation>* out,
    std::string* error) {
  std::ifstream input(path);
  if (!input.is_open()) {
    *error = "cannot open expected fixture: " + path;
    return false;
  }
  std::string line;
  std::getline(input, line);
  while (std::getline(input, line)) {
    const auto fields = SplitLine(line, '\t');
    if (fields.size() != 5U) {
      *error = "invalid expected fixture row";
      return false;
    }
    try {
      out->push_back(
          {static_cast<std::size_t>(std::stoull(fields[0])),
           fields[1],
           fields[2],
           std::stod(fields[3]),
           std::stod(fields[4])});
    } catch (...) {
      *error = "invalid numeric value in expected fixture";
      return false;
    }
  }
  if (out->empty()) {
    *error = "expected fixture is empty";
    return false;
  }
  if (!std::is_sorted(
          out->begin(), out->end(),
          [](const auto& lhs, const auto& rhs) {
            return lhs.sample_count < rhs.sample_count;
          })) {
    *error = "expected fixture checkpoints are not sorted";
    return false;
  }
  return true;
}

int RunFeatureParity(const RuntimeOptions& options) {
  std::vector<FeatureParityBar> bars;
  std::vector<FeatureParityExpectation> expectations;
  std::string load_error;
  if (!LoadFeatureParityBars(options.feature_parity_bars_path, &bars,
                             &load_error) ||
      !LoadFeatureParityExpectations(options.feature_parity_expected_path,
                                     &expectations, &load_error)) {
    ai_trade::LogError("FEATURE_PARITY_LOAD_FAILED: " + load_error);
    return 1;
  }

  ai_trade::research::OnlineFeatureEngine engine(bars.size());
  std::vector<std::string> failures;
  std::size_t passed_count = 0;
  double max_abs_error = 0.0;
  std::size_t expectation_index = 0;
  for (std::size_t bar_index = 0; bar_index < bars.size(); ++bar_index) {
    const auto& bar = bars[bar_index];
    engine.AddCompletedBar(
        bar.open, bar.high, bar.low, bar.close, bar.volume);
    const std::size_t sample_count = bar_index + 1;
    while (expectation_index < expectations.size() &&
           expectations[expectation_index].sample_count == sample_count) {
      const auto& expectation = expectations[expectation_index];
      const double actual = engine.Evaluate(expectation.expression);
      const double error = std::abs(actual - expectation.expected);
      if (std::isfinite(error)) {
        max_abs_error = std::max(max_abs_error, error);
      }
      if (!std::isfinite(actual) || error > expectation.tolerance) {
        std::ostringstream reason;
        reason << sample_count << "/" << expectation.feature
               << ": actual=" << std::setprecision(17) << actual
               << ", expected=" << expectation.expected
               << ", tolerance=" << expectation.tolerance;
        failures.push_back(reason.str());
      } else {
        ++passed_count;
      }
      ++expectation_index;
    }
  }
  if (expectation_index != expectations.size()) {
    failures.push_back("one or more checkpoints exceed bars fixture");
  }

  const std::filesystem::path output_path(
      options.feature_parity_output_path);
  if (!output_path.parent_path().empty()) {
    std::filesystem::create_directories(output_path.parent_path());
  }
  const auto temp_path = output_path.string() + ".tmp";
  std::ofstream output(temp_path, std::ios::trunc);
  if (!output.is_open()) {
    ai_trade::LogError("FEATURE_PARITY_REPORT_WRITE_FAILED: " +
                       output_path.string());
    return 1;
  }
  output << "{\n"
         << "  \"schema_version\": \"feature_parity_report_v1\",\n"
         << "  \"status\": \"" << (failures.empty() ? "PASS" : "FAIL")
         << "\",\n"
         << "  \"engine\": \"cpp_online_feature_engine\",\n"
         << "  \"golden_source\": \"python_integrator_train\",\n"
         << "  \"bars_fixture\": \""
         << EscapeJson(options.feature_parity_bars_path) << "\",\n"
         << "  \"expected_fixture\": \""
         << EscapeJson(options.feature_parity_expected_path) << "\",\n"
         << "  \"check_count\": " << expectations.size() << ",\n"
         << "  \"passed_count\": " << passed_count << ",\n"
         << "  \"max_abs_error\": " << std::setprecision(17)
         << max_abs_error << ",\n"
         << "  \"failures\": [";
  for (std::size_t index = 0; index < failures.size(); ++index) {
    if (index > 0) {
      output << ",";
    }
    output << "\n    \"" << EscapeJson(failures[index]) << "\"";
  }
  if (!failures.empty()) {
    output << "\n  ";
  }
  output << "]\n}\n";
  output.close();
  std::filesystem::rename(temp_path, output_path);
  ai_trade::LogInfo(
      "FEATURE_PARITY_DONE: status=" +
      std::string(failures.empty() ? "PASS" : "FAIL") +
      ", checks=" + std::to_string(expectations.size()) +
      ", output=" + output_path.string());
  return failures.empty() ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
  ai_trade::LogInfo("启动 ai-trade 最小闭环...");

  // 1) 解析 CLI 覆盖参数。
  const RuntimeOptions options = ParseOptions(argc, argv);
  if (options.run_miner) {
    return RunOfflineMiner(options);
  }
  if (options.run_feature_parity) {
    return RunFeatureParity(options);
  }
  // 2) 加载 YAML 基础配置。
  ai_trade::AppConfig config;
  std::string config_error;
  if (!ai_trade::LoadAppConfigFromYaml(options.config_path, &config,
                                       &config_error)) {
    ai_trade::LogError("配置加载失败: " + config_error);
    return 1;
  }
  // 3) 应用 CLI 覆盖参数并启动应用。
  ApplyRuntimeOverrides(options, &config);
  ai_trade::LogInfo(
      "CONFIG_LOADED: path=" + options.config_path +
      ", mode=" + config.mode +
      ", exchange=" + config.exchange +
      ", data_path=" + config.data_path +
      ", regime={enabled=" + std::string(config.regime.enabled ? "true" : "false") +
      ", warmup_ticks=" + std::to_string(config.regime.warmup_ticks) +
      ", ewma_alpha=" + FormatDouble(config.regime.ewma_alpha) +
      ", bar_interval_ms=" + std::to_string(config.regime.bar_interval_ms) +
      ", switch_confirm_ticks=" +
      std::to_string(config.regime.switch_confirm_ticks) +
      ", trend_threshold=" + FormatDouble(config.regime.trend_threshold) +
      ", extreme_threshold=" + FormatDouble(config.regime.extreme_threshold) +
      ", volatility_threshold=" +
      FormatDouble(config.regime.volatility_threshold) +
      "}, universe={enabled=" +
      std::string(config.universe.enabled ? "true" : "false") +
      ", max_active_symbols=" +
      std::to_string(config.universe.max_active_symbols) +
      ", candidate_symbols=[" +
      FormatSymbolList(config.universe.candidate_symbols) + "]}");

  ai_trade::BotApplication app(config);
  if (options.check_exchange) {
    return app.CheckExchange();
  }
  if (options.check_startup) {
    return app.CheckStartup();
  }
  return app.Run();
}
