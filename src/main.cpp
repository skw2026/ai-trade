#include <chrono>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <memory>
#include <optional>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_set>
#include <vector>

#include "core/config.h"
#include "core/log.h"
#include "exchange/bybit_exchange_adapter.h"
#include "exchange/binance_exchange_adapter.h"
#include "exchange/exchange_adapter.h"
#include "exchange/mock_exchange_adapter.h"
#include "execution/execution_engine.h"
#include "execution/order_throttle.h"
#include "monitor/gate_monitor.h"
#include "oms/order_manager.h"
#include "oms/reconciler.h"
#include "storage/wal_store.h"
#include "system/trade_system.h"
#include "universe/universe_selector.h"

namespace {

struct RuntimeOptions {
  std::string config_path{"config/default.yaml"};
  std::string exchange_override;
  std::optional<int> max_ticks;
  std::optional<int> status_log_interval_ticks;
  bool run_forever{false};
};

std::vector<std::string> UniqueSymbols(const std::vector<std::string>& symbols);

bool ParseNonNegativeInt(const std::string& raw, int* out_value) {
  if (out_value == nullptr || raw.empty()) {
    return false;
  }
  try {
    std::size_t consumed = 0;
    const int parsed = std::stoi(raw, &consumed);
    if (consumed != raw.size()) {
      return false;
    }
    if (parsed < 0) {
      return false;
    }
    *out_value = parsed;
    return true;
  } catch (...) {
    return false;
  }
}

const char* RiskModeToString(ai_trade::RiskMode mode) {
  switch (mode) {
    case ai_trade::RiskMode::kNormal:
      return "normal";
    case ai_trade::RiskMode::kDegraded:
      return "degraded";
    case ai_trade::RiskMode::kCooldown:
      return "cooldown";
    case ai_trade::RiskMode::kFuse:
      return "fuse";
    case ai_trade::RiskMode::kReduceOnly:
      return "reduce_only";
  }
  return "unknown";
}

std::int64_t CurrentTimestampMs() {
  const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now());
  return now.time_since_epoch().count();
}

RuntimeOptions ParseOptions(int argc, char** argv) {
  RuntimeOptions options;
  auto parse_int_arg = [](const std::string& raw_value,
                          const std::string& option_name,
                          std::optional<int>* out_value) {
    int parsed = 0;
    if (!ParseNonNegativeInt(raw_value, &parsed)) {
      ai_trade::LogInfo(option_name + " 参数非法，已忽略: " + raw_value);
      return;
    }
    *out_value = parsed;
  };

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
      parse_int_arg(arg.substr(std::string("--max_ticks=").size()),
                    "--max_ticks",
                    &options.max_ticks);
      continue;
    }
    if (arg == "--max_ticks" && i + 1 < argc) {
      ++i;
      parse_int_arg(argv[i], "--max_ticks", &options.max_ticks);
      continue;
    }
    if (arg.rfind("--status_log_interval_ticks=", 0) == 0) {
      parse_int_arg(
          arg.substr(std::string("--status_log_interval_ticks=").size()),
          "--status_log_interval_ticks",
          &options.status_log_interval_ticks);
      continue;
    }
    if (arg == "--status_log_interval_ticks" && i + 1 < argc) {
      ++i;
      parse_int_arg(argv[i],
                    "--status_log_interval_ticks",
                    &options.status_log_interval_ticks);
      continue;
    }
    if (arg == "--run_forever" || arg == "--run-forever") {
      options.run_forever = true;
      continue;
    }
  }
  return options;
}

std::unique_ptr<ai_trade::ExchangeAdapter> BuildAdapter(
    const ai_trade::AppConfig& config) {
  auto collect_symbols = [&config]() {
    std::vector<std::string> symbols;
    if (config.universe.enabled) {
      symbols = config.universe.candidate_symbols;
      symbols.insert(symbols.end(),
                     config.universe.fallback_symbols.begin(),
                     config.universe.fallback_symbols.end());
      symbols.push_back(config.primary_symbol);
    } else {
      symbols.push_back(config.primary_symbol);
    }
    return UniqueSymbols(symbols);
  };

  if (config.exchange == "bybit") {
    ai_trade::BybitAdapterOptions options;
    options.testnet = config.bybit.testnet;
    options.demo_trading = config.bybit.demo_trading;
    options.mode = config.mode;
    options.category = config.bybit.category;
    options.account_type = config.bybit.account_type;
    options.primary_symbol = config.primary_symbol;
    options.public_ws_enabled = config.bybit.public_ws_enabled;
    options.public_ws_rest_fallback = config.bybit.public_ws_rest_fallback;
    options.private_ws_enabled = config.bybit.private_ws_enabled;
    options.private_ws_rest_fallback = config.bybit.private_ws_rest_fallback;
    options.execution_poll_limit = config.bybit.execution_poll_limit;
    options.symbols = collect_symbols();
    if (options.symbols.empty()) {
      options.symbols.push_back(config.primary_symbol);
    }
    options.remote_account_mode = config.bybit.expected_account_mode;
    options.remote_margin_mode = config.bybit.expected_margin_mode;
    options.remote_position_mode = config.bybit.expected_position_mode;
    return std::make_unique<ai_trade::BybitExchangeAdapter>(options);
  }

  if (config.exchange == "binance") {
    return std::make_unique<ai_trade::BinanceExchangeAdapter>();
  }

  std::vector<double> prices = {100.0, 100.5, 100.7, 100.2, 99.8, 100.1};
  std::vector<std::string> symbols = collect_symbols();
  if (symbols.empty()) {
    symbols.push_back(config.primary_symbol);
  }
  return std::make_unique<ai_trade::MockExchangeAdapter>(prices, symbols);
}

bool ValidateAccountSnapshot(const ai_trade::AppConfig& config,
                             ai_trade::ExchangeAdapter* adapter) {
  if (adapter == nullptr) {
    return false;
  }

  ai_trade::ExchangeAccountSnapshot snapshot;
  if (!adapter->GetAccountSnapshot(&snapshot)) {
    if (config.exchange == "bybit") {
      ai_trade::LogInfo("账户快照读取失败：Bybit 模式下拒绝启动");
      return false;
    }
    ai_trade::LogInfo("账户快照暂不可用，继续启动（非Bybit模式）");
    return true;
  }

  if (config.exchange != "bybit") {
    return true;
  }

  const bool account_ok =
      snapshot.account_mode == config.bybit.expected_account_mode;
  const bool margin_ok = snapshot.margin_mode == config.bybit.expected_margin_mode;
  const bool position_ok =
      snapshot.position_mode == config.bybit.expected_position_mode;
  if (account_ok && margin_ok && position_ok) {
    ai_trade::LogInfo("账户模式校验通过: account_mode=" +
                      std::string(ai_trade::ToString(snapshot.account_mode)) +
                      ", margin_mode=" +
                      std::string(ai_trade::ToString(snapshot.margin_mode)) +
                      ", position_mode=" +
                      std::string(ai_trade::ToString(snapshot.position_mode)));
    return true;
  }

  ai_trade::LogInfo(
      "账户模式校验失败: expected(account=" +
      std::string(ai_trade::ToString(config.bybit.expected_account_mode)) +
      ", margin=" +
      std::string(ai_trade::ToString(config.bybit.expected_margin_mode)) +
      ", position=" +
      std::string(ai_trade::ToString(config.bybit.expected_position_mode)) +
      ") actual(account=" +
      std::string(ai_trade::ToString(snapshot.account_mode)) +
      ", margin=" + std::string(ai_trade::ToString(snapshot.margin_mode)) +
      ", position=" + std::string(ai_trade::ToString(snapshot.position_mode)) +
      ")");
  return false;
}

std::string BoolToString(bool value) {
  return value ? "true" : "false";
}

std::string JoinStrings(const std::vector<std::string>& items,
                        const std::string& sep) {
  std::string out;
  for (std::size_t i = 0; i < items.size(); ++i) {
    if (i > 0U) {
      out += sep;
    }
    out += items[i];
  }
  return out;
}

std::vector<std::string> UniqueSymbols(const std::vector<std::string>& symbols) {
  std::vector<std::string> out;
  std::unordered_set<std::string> seen;
  for (const auto& symbol : symbols) {
    if (symbol.empty()) {
      continue;
    }
    if (seen.insert(symbol).second) {
      out.push_back(symbol);
    }
  }
  return out;
}

std::string FormatRemotePositions(
    const std::vector<ai_trade::RemotePositionSnapshot>& positions) {
  std::ostringstream oss;
  for (std::size_t i = 0; i < positions.size(); ++i) {
    if (i > 0U) {
      oss << ";";
    }
    oss << positions[i].symbol << ":qty=" << positions[i].qty
        << ",avg=" << positions[i].avg_entry_price
        << ",mark=" << positions[i].mark_price;
  }
  return oss.str();
}

std::string FormatLocalPositions(const ai_trade::AccountState& account,
                                 const std::vector<std::string>& symbols) {
  constexpr double kZeroTolerance = 1e-9;
  std::ostringstream oss;
  bool has_position = false;
  for (const auto& symbol : symbols) {
    const double qty = account.position_qty(symbol);
    if (std::fabs(qty) <= kZeroTolerance) {
      continue;
    }
    if (has_position) {
      oss << ";";
    }
    has_position = true;
    oss << symbol << ":qty=" << qty
        << ",mark=" << account.mark_price(symbol)
        << ",notional=" << account.current_notional_usd(symbol);
  }
  if (!has_position) {
    return "flat";
  }
  return oss.str();
}

std::string FormatRate(double ratio) {
  std::ostringstream oss;
  oss << std::fixed << std::setprecision(4) << ratio;
  return oss.str();
}

bool SubmitIntent(const ai_trade::OrderIntent& intent,
                  ai_trade::ExchangeAdapter* adapter,
                  ai_trade::WalStore* wal,
                  std::unordered_set<std::string>* intent_ids,
                  ai_trade::OrderManager* oms,
                  std::string* wal_error) {
  if (adapter == nullptr || wal == nullptr ||
      intent_ids == nullptr || oms == nullptr || wal_error == nullptr) {
    return false;
  }
  if (intent.client_order_id.empty()) {
    ai_trade::LogInfo("订单缺少 client_order_id，拒绝提交");
    return false;
  }
  if (intent_ids->count(intent.client_order_id) != 0U) {
    ai_trade::LogInfo("检测到重复 client_order_id，跳过提交: " +
                      intent.client_order_id);
    return false;
  }
  if (!oms->RegisterIntent(intent)) {
    ai_trade::LogInfo("OMS 注册订单失败（可能重复）: " +
                      intent.client_order_id);
    return false;
  }
  if (!wal->AppendIntent(intent, wal_error)) {
    ai_trade::LogInfo("WAL 写入订单意图失败，拒绝下单: " + *wal_error);
    oms->MarkRejected(intent.client_order_id);
    return false;
  }

  intent_ids->insert(intent.client_order_id);
  const bool submitted = adapter->SubmitOrder(intent);
  if (submitted) {
    oms->MarkSent(intent.client_order_id);
    return true;
  }

  oms->MarkRejected(intent.client_order_id);
  ai_trade::LogInfo("下单失败（由适配器返回）: " + intent.client_order_id);
  return false;
}

bool AttachProtectionOrders(const ai_trade::FillEvent& entry_fill,
                            const ai_trade::AppConfig& config,
                            ai_trade::ExecutionEngine* execution,
                            ai_trade::ExchangeAdapter* adapter,
                            ai_trade::WalStore* wal,
                            std::unordered_set<std::string>* intent_ids,
                            ai_trade::OrderManager* oms,
                            std::string* wal_error) {
  if (!config.protection.enabled || execution == nullptr) {
    return true;
  }

  const auto sl = execution->BuildProtectionIntent(entry_fill,
                                                   ai_trade::OrderPurpose::kSl,
                                                   config.protection.stop_loss_ratio);
  if (!sl.has_value()) {
    ai_trade::LogInfo("EXEC_PROTECTIVE_ORDER_MISSING: 无法构造SL保护单");
    return !config.protection.require_sl;
  }

  const bool sl_ok = SubmitIntent(*sl, adapter, wal, intent_ids, oms, wal_error);
  if (!sl_ok && config.protection.require_sl) {
    ai_trade::LogInfo("EXEC_PROTECTIVE_ORDER_MISSING: SL提交失败");
    return false;
  }

  if (!config.protection.enable_tp) {
    return sl_ok || !config.protection.require_sl;
  }

  const auto tp = execution->BuildProtectionIntent(entry_fill,
                                                   ai_trade::OrderPurpose::kTp,
                                                   config.protection.take_profit_ratio);
  if (!tp.has_value()) {
    ai_trade::LogInfo("EXEC_TP_ATTACH_FAILED: 无法构造TP保护单");
    return sl_ok || !config.protection.require_sl;
  }

  if (!SubmitIntent(*tp, adapter, wal, intent_ids, oms, wal_error)) {
    ai_trade::LogInfo("EXEC_TP_ATTACH_FAILED: TP提交失败");
  }
  return sl_ok || !config.protection.require_sl;
}

}  // namespace

int main(int argc, char** argv) {
  ai_trade::LogInfo("启动 ai-trade 最小闭环...");
  const RuntimeOptions options = ParseOptions(argc, argv);

  ai_trade::AppConfig config;
  std::string config_error;
  if (!ai_trade::LoadAppConfigFromYaml(options.config_path, &config,
                                       &config_error)) {
    ai_trade::LogInfo("配置加载失败: " + config_error);
    return 1;
  }
  if (!options.exchange_override.empty()) {
    config.exchange = options.exchange_override;
  }
  if (options.max_ticks.has_value()) {
    config.system_max_ticks = *options.max_ticks;
  }
  if (options.status_log_interval_ticks.has_value()) {
    config.system_status_log_interval_ticks = *options.status_log_interval_ticks;
  }
  if (options.run_forever) {
    config.system_max_ticks = 0;
  }

  ai_trade::LogInfo("配置加载成功: risk.max_abs_notional_usd=" +
                    std::to_string(config.risk_max_abs_notional_usd) +
                    ", risk.max_drawdown={degraded:" +
                    std::to_string(config.risk_thresholds.degraded_drawdown) +
                    ", cooldown:" +
                    std::to_string(config.risk_thresholds.cooldown_drawdown) +
                    ", fuse:" +
                    std::to_string(config.risk_thresholds.fuse_drawdown) +
                    "}" +
                    ", execution.max_order_notional=" +
                    std::to_string(config.execution_max_order_notional) +
                    ", execution.min_order_interval_ms=" +
                    std::to_string(config.execution_min_order_interval_ms) +
                    ", execution.reverse_signal_cooldown_ticks=" +
                    std::to_string(config.execution_reverse_signal_cooldown_ticks) +
                    ", system.max_ticks=" +
                    std::to_string(config.system_max_ticks) +
                    ", system.status_log_interval_ticks=" +
                    std::to_string(config.system_status_log_interval_ticks) +
                    ", exchange=" + config.exchange +
                    ", symbol=" + config.primary_symbol +
                    ", mode=" + config.mode +
                    ", data_path=" + config.data_path +
                    ", protection.enabled=" +
                    BoolToString(config.protection.enabled) +
                    ", gate.min_effective_signals=" +
                    std::to_string(config.gate.min_effective_signals_per_window) +
                    ", gate.min_fills=" +
                    std::to_string(config.gate.min_fills_per_window) +
                    ", universe.enabled=" +
                    BoolToString(config.universe.enabled) +
                    ", universe.candidates=[" +
                    JoinStrings(config.universe.candidate_symbols, ",") + "]" +
                    ", bybit.expected={account:" +
                    std::string(ai_trade::ToString(config.bybit.expected_account_mode)) +
                    ", margin:" +
                    std::string(ai_trade::ToString(config.bybit.expected_margin_mode)) +
                    ", position:" +
                    std::string(ai_trade::ToString(config.bybit.expected_position_mode)) +
                    "}" +
                    ", bybit.demo_trading=" +
                    BoolToString(config.bybit.demo_trading) +
                    ", bybit.private_ws={enabled:" +
                    BoolToString(config.bybit.private_ws_enabled) +
                    ", fallback:" +
                    BoolToString(config.bybit.private_ws_rest_fallback) +
                    "}" +
                    ", bybit.public_ws={enabled:" +
                    BoolToString(config.bybit.public_ws_enabled) +
                    ", fallback:" +
                    BoolToString(config.bybit.public_ws_rest_fallback) +
                    "}" +
                    ", bybit.execution={poll_limit:" +
                    std::to_string(config.bybit.execution_poll_limit) + "}");

  ai_trade::TradeSystem system(/*risk_cap_usd=*/config.risk_max_abs_notional_usd,
                               /*max_order_notional_usd=*/config.execution_max_order_notional,
                               config.risk_thresholds);
  ai_trade::ExecutionEngine execution(config.execution_max_order_notional);
  ai_trade::OrderThrottle order_throttle(ai_trade::OrderThrottleConfig{
      .min_order_interval_ms = config.execution_min_order_interval_ms,
      .reverse_signal_cooldown_ticks =
          config.execution_reverse_signal_cooldown_ticks,
  });
  ai_trade::OrderManager oms;
  ai_trade::Reconciler reconciler(config.reconcile.tolerance_notional_usd);
  ai_trade::GateMonitor gate_monitor(config.gate);
  ai_trade::UniverseSelector universe_selector(config.universe,
                                               config.primary_symbol);
  bool forced_reduce_only = false;
  bool trading_halted = false;
  int reconcile_tick = 0;

  ai_trade::WalStore wal(config.data_path + "/trade.wal");
  std::string wal_error;
  if (!wal.Initialize(&wal_error)) {
    ai_trade::LogInfo("WAL 初始化失败: " + wal_error);
    return 1;
  }

  std::unordered_set<std::string> intent_ids;
  std::unordered_set<std::string> fill_ids;
  std::vector<ai_trade::FillEvent> historical_fills;
  if (config.mode == "replay") {
    ai_trade::LogInfo("replay 模式：跳过历史 WAL 恢复");
  } else {
    if (!wal.LoadState(&intent_ids, &fill_ids, &historical_fills, &wal_error)) {
      ai_trade::LogInfo("WAL 加载失败: " + wal_error);
      return 1;
    }
    for (const auto& fill : historical_fills) {
      oms.OnFill(fill);
      system.OnFill(fill);
    }
    ai_trade::LogInfo("WAL 恢复完成: intents=" +
                      std::to_string(intent_ids.size()) +
                      ", fills=" + std::to_string(fill_ids.size()));
  }

  auto adapter = BuildAdapter(config);
  if (!adapter->Connect()) {
    ai_trade::LogInfo("交易所连接失败，进程退出。");
    return 1;
  }

  ai_trade::LogInfo(std::string("使用适配器: ") + adapter->Name());
  if (!ValidateAccountSnapshot(config, adapter.get())) {
    ai_trade::LogInfo("账户模式不满足策略/风控要求，进程退出。");
    return 1;
  }

  std::vector<std::string> symbol_candidates = config.universe.candidate_symbols;
  symbol_candidates.insert(symbol_candidates.end(),
                           config.universe.fallback_symbols.begin(),
                           config.universe.fallback_symbols.end());
  symbol_candidates.push_back(config.primary_symbol);
  symbol_candidates = UniqueSymbols(symbol_candidates);

  std::vector<std::string> allowed_symbols;
  allowed_symbols.reserve(symbol_candidates.size());
  bool has_any_symbol_info = false;
  for (const auto& symbol : symbol_candidates) {
    ai_trade::SymbolInfo info;
    if (!adapter->GetSymbolInfo(symbol, &info)) {
      ai_trade::LogInfo("SYMBOL_INFO_UNAVAILABLE: symbol=" + symbol);
      continue;
    }
    has_any_symbol_info = true;
    if (!info.tradable) {
      ai_trade::LogInfo("SYMBOL_FILTERED_NOT_TRADABLE: symbol=" + info.symbol);
      continue;
    }
    if (info.qty_step <= 0.0 || info.min_order_qty <= 0.0) {
      ai_trade::LogInfo("SYMBOL_FILTERED_INVALID_RULE: symbol=" + info.symbol +
                        ", qty_step=" + std::to_string(info.qty_step) +
                        ", min_qty=" + std::to_string(info.min_order_qty));
      continue;
    }
    allowed_symbols.push_back(info.symbol);
    ai_trade::LogInfo("SYMBOL_INFO: symbol=" + info.symbol +
                      ", qty_step=" + std::to_string(info.qty_step) +
                      ", min_qty=" + std::to_string(info.min_order_qty) +
                      ", min_notional=" + std::to_string(info.min_notional_usd) +
                      ", price_tick=" + std::to_string(info.price_tick));
  }
  allowed_symbols = UniqueSymbols(allowed_symbols);
  if (has_any_symbol_info) {
    universe_selector.SetAllowedSymbols(allowed_symbols);
    if (allowed_symbols.empty()) {
      ai_trade::LogInfo("SYMBOL_FILTER_EMPTY: 无可交易币对，进程退出。");
      return 1;
    }
  } else {
    ai_trade::LogInfo("SYMBOL_FILTER_SKIPPED: 未获取到symbol信息，沿用原始候选币对");
  }

  const bool require_remote_position_sync =
      (config.mode == "paper" || config.mode == "live");
  if (config.mode != "replay") {
    std::vector<ai_trade::RemotePositionSnapshot> remote_positions;
    if (adapter->GetRemotePositions(&remote_positions)) {
      system.SyncAccountFromRemotePositions(remote_positions);
      ai_trade::LogInfo("REMOTE_POSITION_SYNC: count=" +
                        std::to_string(remote_positions.size()) +
                        (remote_positions.empty()
                             ? std::string()
                             : ", positions=" +
                                   FormatRemotePositions(remote_positions)));
    } else {
      ai_trade::LogInfo("REMOTE_POSITION_SYNC_SKIPPED: 交易所持仓快照不可用");
      if (require_remote_position_sync) {
        ai_trade::LogInfo("REMOTE_POSITION_SYNC_REQUIRED: mode=" + config.mode +
                          "，拒绝启动。");
        return 1;
      }
    }
  }

  const std::vector<std::string> tracked_symbols =
      !allowed_symbols.empty() ? allowed_symbols : symbol_candidates;

  if (config.system_max_ticks > 0) {
    ai_trade::LogInfo("运行模式: max_ticks=" +
                      std::to_string(config.system_max_ticks));
  } else {
    ai_trade::LogInfo("运行模式: run_forever=true (system.max_ticks=0)");
  }

  ai_trade::MarketEvent event;
  int market_tick_count = 0;
  std::uint64_t throttle_checks_total = 0;
  std::uint64_t throttle_hits_total = 0;
  std::uint64_t throttle_checks_last_status = 0;
  std::uint64_t throttle_hits_last_status = 0;
  while (true) {
    const bool has_market_event = adapter->PollMarket(&event);
    bool advanced_market_tick = false;
    bool has_fill_event = false;

    if (has_market_event) {
      ++market_tick_count;
      advanced_market_tick = true;

      if (const auto universe_update = universe_selector.OnMarket(event);
          universe_update.has_value()) {
        ai_trade::LogInfo("Universe更新: active_symbols=[" +
                          JoinStrings(universe_update->active_symbols, ",") +
                          "], degraded=" +
                          BoolToString(universe_update->degraded_to_fallback) +
                          (universe_update->reason_code.empty()
                               ? std::string()
                               : ", reason=" + universe_update->reason_code));
      }

      if (!trading_halted) {
        const bool trade_ok = adapter->TradeOk() && !forced_reduce_only;
        auto decision = system.Evaluate(event, trade_ok);
        const bool symbol_active = universe_selector.IsActive(event.symbol);
        if (!symbol_active) {
          decision.signal.suggested_notional_usd = 0.0;
          decision.signal.direction = 0;
          decision.risk_adjusted.adjusted_notional_usd = 0.0;
          decision.intent.reset();
        }
        auto intent = decision.intent;
        if (const auto alert = gate_monitor.OnDecision(decision.signal,
                                                       decision.risk_adjusted,
                                                       intent);
            alert.has_value()) {
          ai_trade::LogInfo(*alert + ": 连续 " +
                            std::to_string(config.gate.heartbeat_empty_signal_ticks) +
                            " 个tick未出现有效信号");
        }
        if (intent.has_value()) {
          ++throttle_checks_total;
          const std::int64_t now_ms = CurrentTimestampMs();
          std::string throttle_reason;
          if (!order_throttle.Allow(*intent,
                                    now_ms,
                                    market_tick_count,
                                    &throttle_reason)) {
            ++throttle_hits_total;
            ai_trade::LogInfo("ORDER_THROTTLED: symbol=" + intent->symbol +
                              ", client_order_id=" + intent->client_order_id +
                              ", reason=" + throttle_reason);
          } else if (SubmitIntent(*intent,
                                  adapter.get(),
                                  &wal,
                                  &intent_ids,
                                  &oms,
                                  &wal_error)) {
            order_throttle.OnAccepted(*intent, now_ms, market_tick_count);
          }
        }
      } else {
        system.OnMarketSnapshot(event);
      }
    }

    ai_trade::FillEvent fill;
    while (adapter->PollFill(&fill)) {
      has_fill_event = true;
      if (fill.fill_id.empty()) {
        ai_trade::LogInfo("收到空 fill_id 的成交，忽略");
        continue;
      }
      if (fill_ids.count(fill.fill_id) != 0U) {
        ai_trade::LogInfo("检测到重复成交回报，忽略: " + fill.fill_id);
        continue;
      }

      const ai_trade::OrderRecord* before_fill = oms.Find(fill.client_order_id);
      if (before_fill != nullptr &&
          before_fill->state == ai_trade::OrderState::kCancelled) {
        ai_trade::LogInfo("订单已被OCO撤销，忽略成交: " + fill.fill_id);
        continue;
      }

      if (!wal.AppendFill(fill, &wal_error)) {
        ai_trade::LogInfo("WAL 写入成交失败，拒绝推进账户状态: " + wal_error);
        continue;
      }
      fill_ids.insert(fill.fill_id);
      oms.OnFill(fill);
      system.OnFill(fill);
      gate_monitor.OnFill(fill);

      const ai_trade::OrderRecord* record = oms.Find(fill.client_order_id);
      if (record != nullptr &&
          record->intent.purpose == ai_trade::OrderPurpose::kEntry &&
          config.protection.enabled &&
          !oms.HasOpenProtection(record->intent.client_order_id)) {
        const bool protection_ok =
            AttachProtectionOrders(fill, config, &execution, adapter.get(),
                                   &wal, &intent_ids, &oms, &wal_error);
        if (!protection_ok && config.protection.require_sl) {
          forced_reduce_only = true;
          system.ForceReduceOnly(true);
          ai_trade::LogInfo("SL保护单失败，系统进入强制reduce-only");
        }
      }

      if (record != nullptr &&
          (record->intent.purpose == ai_trade::OrderPurpose::kSl ||
           record->intent.purpose == ai_trade::OrderPurpose::kTp)) {
        const auto sibling = oms.FindOpenProtectiveSibling(
            record->intent.parent_order_id, record->intent.purpose);
        if (sibling.has_value()) {
          adapter->CancelOrder(*sibling);
          oms.MarkCancelled(*sibling);
          ai_trade::LogInfo("OCO触发：撤销对侧保护单 " + *sibling);
        }
      }

      ai_trade::LogInfo("收到成交回报并更新账户状态");
    }

    if (advanced_market_tick &&
        config.reconcile.enabled &&
        config.reconcile.interval_ticks > 0 &&
        (++reconcile_tick % config.reconcile.interval_ticks) == 0) {
      std::optional<double> remote_notional_usd;
      double remote_notional = 0.0;
      if (adapter->GetRemoteNotionalUsd(&remote_notional)) {
        remote_notional_usd = remote_notional;
      }

      const auto reconcile_result = reconciler.Check(system.account(),
                                                     oms,
                                                     remote_notional_usd);
      if (!reconcile_result.ok) {
        if (!trading_halted) {
          trading_halted = true;
          ai_trade::LogInfo("OMS_RECONCILE_MISMATCH: delta_notional=" +
                            std::to_string(reconcile_result.delta_notional_usd));
          ai_trade::LogInfo("TRADING_HALTED: 对账不一致，停止新下单并等待人工处理");
        }
      }
    }

    if (advanced_market_tick) {
      if (const auto gate_result = gate_monitor.OnTick(); gate_result.has_value()) {
        if (gate_result->pass) {
          ai_trade::LogInfo("Gate窗口通过: raw_signals=" +
                            std::to_string(gate_result->raw_signals) +
                            ", order_intents=" +
                            std::to_string(gate_result->order_intents) +
                            ", effective_signals=" +
                            std::to_string(gate_result->effective_signals) +
                            ", fills=" + std::to_string(gate_result->fills));
        } else {
          std::string reasons;
          for (std::size_t i = 0; i < gate_result->fail_reasons.size(); ++i) {
            if (i > 0U) {
              reasons += ",";
            }
            reasons += gate_result->fail_reasons[i];
          }
          ai_trade::LogInfo("Gate窗口失败: raw_signals=" +
                            std::to_string(gate_result->raw_signals) +
                            ", order_intents=" +
                            std::to_string(gate_result->order_intents) +
                            ", effective_signals=" +
                            std::to_string(gate_result->effective_signals) +
                            ", fills=" + std::to_string(gate_result->fills) +
                            ", reasons=" + reasons);
        }
      }
    }

    if (advanced_market_tick &&
        config.system_status_log_interval_ticks > 0 &&
        (market_tick_count % config.system_status_log_interval_ticks) == 0) {
      const std::uint64_t interval_checks =
          throttle_checks_total - throttle_checks_last_status;
      const std::uint64_t interval_hits =
          throttle_hits_total - throttle_hits_last_status;
      const double interval_hit_rate =
          (interval_checks > 0)
              ? static_cast<double>(interval_hits) /
                    static_cast<double>(interval_checks)
              : 0.0;
      const double total_hit_rate =
          (throttle_checks_total > 0)
              ? static_cast<double>(throttle_hits_total) /
                    static_cast<double>(throttle_checks_total)
              : 0.0;
      throttle_checks_last_status = throttle_checks_total;
      throttle_hits_last_status = throttle_hits_total;

      std::string ws_health = "adapter=" + adapter->Name();
      if (const auto* bybit_adapter =
              dynamic_cast<const ai_trade::BybitExchangeAdapter*>(adapter.get());
          bybit_adapter != nullptr) {
        ws_health = bybit_adapter->ChannelHealthSummary();
      }

      ai_trade::LogInfo(
          "RUNTIME_STATUS: ticks=" + std::to_string(market_tick_count) +
          ", trade_ok=" + BoolToString(adapter->TradeOk()) +
          ", trading_halted=" + BoolToString(trading_halted) +
          ", risk_mode=" + std::string(RiskModeToString(system.risk_mode())) +
          ", ws={" + ws_health + "}" +
          ", account={equity=" +
          std::to_string(system.account().equity_usd()) +
          ", drawdown_pct=" + FormatRate(system.account().drawdown_pct()) +
          ", notional=" + std::to_string(system.account().current_notional_usd()) +
          ", positions=" + FormatLocalPositions(system.account(), tracked_symbols) +
          "}" +
          ", throttle={interval=" + std::to_string(interval_hits) + "/" +
          std::to_string(interval_checks) +
          ", interval_hit_rate=" + FormatRate(interval_hit_rate) +
          ", total=" + std::to_string(throttle_hits_total) + "/" +
          std::to_string(throttle_checks_total) +
          ", total_hit_rate=" + FormatRate(total_hit_rate) + "}");
    }

    if (config.system_max_ticks > 0 &&
        market_tick_count >= config.system_max_ticks) {
      ai_trade::LogInfo("达到 system.max_ticks 限制，结束运行: ticks=" +
                        std::to_string(market_tick_count));
      break;
    }

    if (!has_market_event && !has_fill_event && config.mode == "replay") {
      ai_trade::LogInfo("REPLAY_STREAM_EXHAUSTED: 行情与成交流均已耗尽，结束运行。");
      break;
    }

    if (!has_market_event && !has_fill_event) {
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
  }

  ai_trade::LogInfo("最小闭环运行结束。");
  return 0;
}
