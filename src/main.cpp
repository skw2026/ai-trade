#include <optional>
#include <string>
#include <vector>

#include "app/bot_app.h"
#include "core/config.h"
#include "core/log.h"
#include "research/miner.h"

namespace {

// 运行时覆盖参数：用于在不改 YAML 的情况下快速切换运行行为。
struct RuntimeOptions {
  std::string config_path{"config/default.yaml"};
  std::string exchange_override;
  std::optional<int> max_ticks;
  std::optional<int> status_log_interval_ticks;
  std::optional<int> remote_risk_refresh_interval_ticks;
  bool run_forever{false};
  bool run_miner{false};
  std::string miner_csv_path;
  std::string miner_output_path{"./data/research/miner_report.json"};
  std::optional<int> miner_top_k;
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
 * - `--max_ticks=...` / `--max_ticks ...`
 * - `--status_log_interval_ticks=...` / `--status_log_interval_ticks ...`
 * - `--remote_risk_refresh_interval_ticks=...` / `--remote_risk_refresh_interval_ticks ...`
 * - `--run_forever`
 * - `--run_miner --miner_csv=... [--miner_output=...] [--miner_top_k=...]`
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
    if (arg == "--run_miner" || arg == "--run-miner") {
      options.run_miner = true;
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
  ai_trade::LogInfo("MINER_START: bars=" + std::to_string(bars.size()) +
                    ", top_k=" + std::to_string(miner_config.top_k));

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

}  // namespace

int main(int argc, char** argv) {
  ai_trade::LogInfo("启动 ai-trade 最小闭环...");

  // 1) 解析 CLI 覆盖参数。
  const RuntimeOptions options = ParseOptions(argc, argv);
  if (options.run_miner) {
    return RunOfflineMiner(options);
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

  ai_trade::BotApplication app(config);
  return app.Run();
}
