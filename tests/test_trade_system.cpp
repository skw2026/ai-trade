#include <cmath>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <optional>
#include <unordered_set>
#include <utility>
#include <vector>

#include "core/config.h"
#include "exchange/bybit_exchange_adapter.h"
#include "exchange/bybit_private_stream.h"
#include "exchange/bybit_public_stream.h"
#include "exchange/bybit_rest_client.h"
#include "exchange/mock_exchange_adapter.h"
#include "exchange/websocket_client.h"
#include "execution/execution_engine.h"
#include "execution/order_throttle.h"
#include "monitor/gate_monitor.h"
#include "oms/order_manager.h"
#include "oms/reconciler.h"
#include "risk/risk_engine.h"
#include "storage/wal_store.h"
#include "system/trade_system.h"
#include "universe/universe_selector.h"

namespace {

bool NearlyEqual(double lhs, double rhs, double eps = 1e-6) {
  return std::fabs(lhs - rhs) < eps;
}

ai_trade::FillEvent ToFill(const ai_trade::OrderIntent& intent,
                           const std::string& fill_id) {
  ai_trade::FillEvent fill;
  fill.fill_id = fill_id;
  fill.client_order_id = intent.client_order_id;
  fill.symbol = intent.symbol;
  fill.direction = intent.direction;
  fill.qty = intent.qty;
  fill.price = intent.price;
  return fill;
}

class ScopedEnvVar {
 public:
  ScopedEnvVar(std::string key, std::string value) : key_(std::move(key)) {
    const char* existing = std::getenv(key_.c_str());
    if (existing != nullptr) {
      had_old_value_ = true;
      old_value_ = existing;
    }
    setenv(key_.c_str(), value.c_str(), 1);
  }

  ~ScopedEnvVar() {
    if (had_old_value_) {
      setenv(key_.c_str(), old_value_.c_str(), 1);
      return;
    }
    unsetenv(key_.c_str());
  }

 private:
  std::string key_;
  bool had_old_value_{false};
  std::string old_value_;
};

enum class ScriptedWsAction {
  kText,
  kNoMessage,
  kClosed,
  kError,
};

struct ScriptedWsStep {
  ScriptedWsAction action{ScriptedWsAction::kNoMessage};
  std::string payload;
  std::string error;
};

class ScriptedWebsocketClient final : public ai_trade::WebsocketClient {
 public:
  explicit ScriptedWebsocketClient(std::vector<ScriptedWsStep> script,
                                   bool connect_ok = true,
                                   std::string connect_error = {},
                                   std::string expected_url = {})
      : script_(std::move(script)),
        connect_ok_(connect_ok),
        connect_error_(std::move(connect_error)),
        expected_url_(std::move(expected_url)) {}

  bool Connect(
      const std::string& url,
      const std::vector<std::pair<std::string, std::string>>& headers,
      std::string* out_error) override {
    (void)headers;
    if (!expected_url_.empty() && url != expected_url_) {
      if (out_error != nullptr) {
        *out_error = "unexpected ws url: " + url;
      }
      connected_ = false;
      return false;
    }
    if (!connect_ok_) {
      if (out_error != nullptr) {
        *out_error = connect_error_.empty() ? "scripted connect failed"
                                            : connect_error_;
      }
      connected_ = false;
      return false;
    }
    connected_ = true;
    cursor_ = 0;
    return true;
  }

  bool SendText(const std::string& payload, std::string* out_error) override {
    if (!connected_) {
      if (out_error != nullptr) {
        *out_error = "scripted ws not connected";
      }
      return false;
    }
    sent_payloads_.push_back(payload);
    return true;
  }

  ai_trade::WsPollStatus PollText(std::string* out_payload,
                                  std::string* out_error) override {
    if (!connected_) {
      if (out_error != nullptr) {
        *out_error = "scripted ws closed";
      }
      return ai_trade::WsPollStatus::kClosed;
    }
    if (out_payload == nullptr) {
      if (out_error != nullptr) {
        *out_error = "out_payload 为空";
      }
      return ai_trade::WsPollStatus::kError;
    }
    if (cursor_ >= script_.size()) {
      out_payload->clear();
      return ai_trade::WsPollStatus::kNoMessage;
    }

    const ScriptedWsStep& step = script_[cursor_++];
    switch (step.action) {
      case ScriptedWsAction::kText:
        *out_payload = step.payload;
        return ai_trade::WsPollStatus::kMessage;
      case ScriptedWsAction::kNoMessage:
        out_payload->clear();
        return ai_trade::WsPollStatus::kNoMessage;
      case ScriptedWsAction::kClosed:
        connected_ = false;
        if (out_error != nullptr) {
          *out_error = step.error.empty() ? "scripted ws closed" : step.error;
        }
        return ai_trade::WsPollStatus::kClosed;
      case ScriptedWsAction::kError:
        if (out_error != nullptr) {
          *out_error = step.error.empty() ? "scripted ws error" : step.error;
        }
        return ai_trade::WsPollStatus::kError;
    }
    return ai_trade::WsPollStatus::kNoMessage;
  }

  bool IsConnected() const override {
    return connected_;
  }

  void Close() override {
    connected_ = false;
  }

 private:
  std::vector<ScriptedWsStep> script_;
  std::size_t cursor_{0};
  bool connect_ok_{true};
  bool connected_{false};
  std::string connect_error_;
  std::string expected_url_;
  std::vector<std::string> sent_payloads_;
};

class MockBybitHttpTransport final : public ai_trade::BybitHttpTransport {
 public:
  struct Route {
    std::string method;
    std::string url_contains;
    ai_trade::BybitHttpResponse response;
  };

  void AddRoute(const std::string& method,
                const std::string& url_contains,
                ai_trade::BybitHttpResponse response) {
    routes_.push_back(Route{method, url_contains, std::move(response)});
  }

  ai_trade::BybitHttpResponse Send(
      const std::string& method,
      const std::string& url,
      const std::vector<std::pair<std::string, std::string>>& headers,
      const std::string& body) const override {
    (void)headers;
    last_method_ = method;
    last_url_ = url;
    last_body_ = body;
    for (const auto& route : routes_) {
      if (route.method == method &&
          url.find(route.url_contains) != std::string::npos) {
        return route.response;
      }
    }
    ai_trade::BybitHttpResponse miss;
    miss.status_code = 404;
    miss.error = "mock route not found";
    return miss;
  }

  const std::string& last_method() const { return last_method_; }
  const std::string& last_url() const { return last_url_; }
  const std::string& last_body() const { return last_body_; }

 private:
  std::vector<Route> routes_;
  mutable std::string last_method_;
  mutable std::string last_url_;
  mutable std::string last_body_;
};

}  // namespace

int main() {
  {
    ai_trade::TradeSystem system(/*risk_cap_usd=*/500.0,
                                 /*max_order_notional_usd=*/200.0);
    // 第一根行情用于策略预热，不应触发下单。
    const bool first = system.OnPrice(100.0, true);
    if (first) {
      std::cerr << "预期第一根行情不下单\n";
      return 1;
    }

    // 第二根行情应触发下单，且受单笔额度限制。
    const bool second = system.OnPrice(101.0, true);
    if (!second) {
      std::cerr << "预期第二根行情触发下单\n";
      return 1;
    }

    if (!NearlyEqual(system.account().current_notional_usd(), 200.0)) {
      std::cerr << "预期仓位名义金额为 200.0，实际为 "
                << system.account().current_notional_usd() << "\n";
      return 1;
    }
  }

  {
    ai_trade::TradeSystem system(/*risk_cap_usd=*/500.0,
                                 /*max_order_notional_usd=*/200.0);
    system.OnPrice(100.0, true);
    // 当 trade_ok=false 时，风控应进入只减仓路径。
    system.OnPrice(100.0, false);
    if (!NearlyEqual(system.account().current_notional_usd(), 0.0)) {
      std::cerr << "无有效信号时预期仓位保持 0\n";
      return 1;
    }
  }

  {
    ai_trade::TradeSystem system(/*risk_cap_usd=*/500.0,
                                 /*max_order_notional_usd=*/200.0);
    const ai_trade::MarketEvent first{1, "BTCUSDT", 100.0, 100.0};
    const auto i1 = system.OnMarket(first, true);
    if (i1.has_value()) {
      std::cerr << "预期首个行情事件不返回订单意图\n";
      return 1;
    }

    const ai_trade::MarketEvent second{2, "BTCUSDT", 101.0, 101.0};
    const auto i2 = system.OnMarket(second, true);
    if (!i2.has_value()) {
      std::cerr << "预期第二个行情事件返回订单意图\n";
      return 1;
    }

    // OnMarket 不应直接修改账户仓位，需由成交回报驱动更新。
    if (!NearlyEqual(system.account().current_notional_usd(), 0.0)) {
      std::cerr << "预期 OnMarket 不直接更新账户仓位\n";
      return 1;
    }

    system.OnFill(ToFill(*i2, "fill-1"));
    if (!NearlyEqual(system.account().current_notional_usd(), 200.0)) {
      std::cerr << "预期 OnFill 后仓位更新为 200.0\n";
      return 1;
    }
  }

  {
    ai_trade::ExecutionEngine execution(/*max_order_notional_usd=*/1000.0);
    const ai_trade::RiskAdjustedPosition reduce_to_open{
        .adjusted_notional_usd = 100.0, .reduce_only = true};
    const auto intent = execution.BuildIntent(reduce_to_open,
                                              /*current_notional_usd=*/0.0,
                                              /*price=*/100.0);
    if (intent.has_value()) {
      std::cerr << "reduce_only 下无持仓时不应开仓\n";
      return 1;
    }
  }

  {
    ai_trade::AccountState account;
    account.OnMarket(ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0});
    account.OnMarket(ai_trade::MarketEvent{2, "ETHUSDT", 2000.0, 2000.0});

    ai_trade::FillEvent btc_fill;
    btc_fill.fill_id = "multi-btc-fill";
    btc_fill.client_order_id = "multi-btc-order";
    btc_fill.symbol = "BTCUSDT";
    btc_fill.direction = 1;
    btc_fill.qty = 1.0;
    btc_fill.price = 100.0;
    account.ApplyFill(btc_fill);

    ai_trade::FillEvent eth_fill;
    eth_fill.fill_id = "multi-eth-fill";
    eth_fill.client_order_id = "multi-eth-order";
    eth_fill.symbol = "ETHUSDT";
    eth_fill.direction = -1;
    eth_fill.qty = 0.5;
    eth_fill.price = 2000.0;
    account.ApplyFill(eth_fill);

    if (!NearlyEqual(account.position_qty("BTCUSDT"), 1.0) ||
        !NearlyEqual(account.position_qty("ETHUSDT"), -0.5)) {
      std::cerr << "多symbol仓位数量计算不符合预期\n";
      return 1;
    }
    if (!NearlyEqual(account.current_notional_usd("BTCUSDT"), 100.0) ||
        !NearlyEqual(account.current_notional_usd("ETHUSDT"), -1000.0) ||
        !NearlyEqual(account.current_notional_usd(), -900.0)) {
      std::cerr << "多symbol名义仓位计算不符合预期\n";
      return 1;
    }
  }

  {
    ai_trade::ExecutionEngine execution(/*max_order_notional_usd=*/1000.0);
    const ai_trade::RiskAdjustedPosition reduce_target{
        .adjusted_notional_usd = 100.0, .reduce_only = true};
    const auto intent = execution.BuildIntent(reduce_target,
                                              /*current_notional_usd=*/500.0,
                                              /*price=*/100.0);
    if (!intent.has_value()) {
      std::cerr << "reduce_only 下有持仓时应产生减仓意图\n";
      return 1;
    }
    if (intent->direction != -1) {
      std::cerr << "预期 reduce_only 减仓方向为 -1\n";
      return 1;
    }
    if (!NearlyEqual(intent->qty, 4.0)) {
      std::cerr << "预期 reduce_only 数量为 4.0，实际为 " << intent->qty
                << "\n";
      return 1;
    }
    if (!intent->reduce_only ||
        intent->purpose != ai_trade::OrderPurpose::kSl ||
        intent->client_order_id.empty()) {
      std::cerr << "预期订单包含 reduce_only/purpose/client_order_id\n";
      return 1;
    }
  }

  {
    ai_trade::AccountState account;
    const std::vector<ai_trade::RemotePositionSnapshot> remote_positions = {
        ai_trade::RemotePositionSnapshot{
            .symbol = "BTCUSDT",
            .qty = 2.0,
            .avg_entry_price = 100.0,
            .mark_price = 101.0,
        },
        ai_trade::RemotePositionSnapshot{
            .symbol = "ETHUSDT",
            .qty = -1.0,
            .avg_entry_price = 2000.0,
            .mark_price = 1990.0,
        }};
    account.SyncFromRemotePositions(remote_positions);
    if (!NearlyEqual(account.position_qty("BTCUSDT"), 2.0) ||
        !NearlyEqual(account.position_qty("ETHUSDT"), -1.0)) {
      std::cerr << "远端持仓同步后仓位数量不符合预期\n";
      return 1;
    }
    if (!NearlyEqual(account.current_notional_usd(), -1788.0)) {
      std::cerr << "远端持仓同步后名义值不符合预期，实际 "
                << account.current_notional_usd() << "\n";
      return 1;
    }
  }

  {
    ai_trade::OrderThrottle throttle(ai_trade::OrderThrottleConfig{
        .min_order_interval_ms = 1000,
        .reverse_signal_cooldown_ticks = 0,
    });
    ai_trade::OrderIntent buy;
    buy.client_order_id = "throttle-interval-1";
    buy.symbol = "BTCUSDT";
    buy.direction = 1;
    buy.qty = 0.01;
    buy.price = 100.0;

    std::string reason;
    if (!throttle.Allow(buy, 1000, 10, &reason)) {
      std::cerr << "首次下单不应触发最小下单间隔拦截\n";
      return 1;
    }
    throttle.OnAccepted(buy, 1000, 10);

    buy.client_order_id = "throttle-interval-2";
    if (throttle.Allow(buy, 1500, 11, &reason)) {
      std::cerr << "间隔不足时应触发最小下单间隔拦截\n";
      return 1;
    }
    if (reason.find("min_order_interval_ms_remaining=") == std::string::npos) {
      std::cerr << "最小下单间隔拦截原因不符合预期: " << reason << "\n";
      return 1;
    }

    buy.client_order_id = "throttle-interval-3";
    if (!throttle.Allow(buy, 2200, 12, &reason)) {
      std::cerr << "间隔满足后应恢复下单，原因: " << reason << "\n";
      return 1;
    }
  }

  {
    ai_trade::OrderThrottle throttle(ai_trade::OrderThrottleConfig{
        .min_order_interval_ms = 0,
        .reverse_signal_cooldown_ticks = 3,
    });
    ai_trade::OrderIntent buy;
    buy.client_order_id = "throttle-reverse-buy-1";
    buy.symbol = "ETHUSDT";
    buy.direction = 1;
    buy.qty = 0.1;
    buy.price = 100.0;

    std::string reason;
    if (!throttle.Allow(buy, 1000, 20, &reason)) {
      std::cerr << "首次方向信号不应被冷却拦截\n";
      return 1;
    }
    throttle.OnAccepted(buy, 1000, 20);

    ai_trade::OrderIntent sell = buy;
    sell.client_order_id = "throttle-reverse-sell-1";
    sell.direction = -1;
    if (throttle.Allow(sell, 1200, 21, &reason)) {
      std::cerr << "反向信号应在冷却期内被拦截\n";
      return 1;
    }
    if (reason.find("reverse_signal_cooldown_ticks_remaining=") ==
        std::string::npos) {
      std::cerr << "反向冷却拦截原因不符合预期: " << reason << "\n";
      return 1;
    }

    sell.client_order_id = "throttle-reverse-sell-2";
    if (!throttle.Allow(sell, 1500, 23, &reason)) {
      std::cerr << "反向冷却结束后应恢复下单，原因: " << reason << "\n";
      return 1;
    }

    ai_trade::OrderIntent reduce_only = sell;
    reduce_only.client_order_id = "throttle-reduce-only";
    reduce_only.reduce_only = true;
    if (!throttle.Allow(reduce_only, 1300, 21, &reason)) {
      std::cerr << "reduce_only 订单不应被反向冷却拦截\n";
      return 1;
    }
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() / "ai_trade_test_config.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  mode: \"paper\"\n"
        << "  primary_symbol: \"ETHUSDT\"\n"
        << "  data_path: \"./tmp_data\"\n"
        << "universe:\n"
        << "  enabled: true\n"
        << "  update_interval_minutes: 30\n"
        << "  max_active_symbols: 2\n"
        << "  min_active_symbols: 1\n"
        << "  fallback_symbols: [\"ETHUSDT\"]\n"
        << "  candidate_symbols: [\"ETHUSDT\", \"BTCUSDT\"]\n"
        << "gate:\n"
        << "  min_effective_signals_per_day: 7\n"
        << "  min_fills_per_day: 3\n"
        << "  heartbeat_empty_signal_ticks: 9\n"
        << "  window_ticks: 48\n"
        << "exchange:\n"
        << "  platform: \"mock\"\n"
        << "  bybit:\n"
        << "    testnet: false\n"
        << "    demo_trading: true\n"
        << "    category: \"linear\"\n"
        << "    account_type: \"UNIFIED\"\n"
        << "    expected_account_mode: \"UNIFIED\"\n"
        << "    expected_margin_mode: \"PORTFOLIO\"\n"
        << "    expected_position_mode: \"HEDGE\"\n"
        << "    public_ws_enabled: false\n"
        << "    public_ws_rest_fallback: true\n"
        << "    private_ws_enabled: true\n"
        << "    private_ws_rest_fallback: false\n"
        << "    execution_poll_limit: 25\n"
        << "risk:\n"
        << "  max_abs_notional_usd: 4321\n"
        << "  max_drawdown:\n"
        << "    degraded_threshold: 0.05\n"
        << "    cooldown_threshold: 0.10\n"
        << "    fuse_threshold: 0.18\n"
        << "execution:\n"
        << "  max_order_notional: 876\n"
        << "  min_order_interval_ms: 2500\n"
        << "  reverse_signal_cooldown_ticks: 5\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (!ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "配置加载预期成功，错误: " << error << "\n";
      return 1;
    }
    if (!NearlyEqual(config.risk_max_abs_notional_usd, 4321.0) ||
        !NearlyEqual(config.execution_max_order_notional, 876.0) ||
        config.execution_min_order_interval_ms != 2500 ||
        config.execution_reverse_signal_cooldown_ticks != 5 ||
        config.exchange != "mock" ||
        config.mode != "paper" ||
        config.primary_symbol != "ETHUSDT" ||
        config.data_path != "./tmp_data" ||
        config.gate.min_effective_signals_per_window != 7 ||
        config.gate.min_fills_per_window != 3 ||
        config.gate.heartbeat_empty_signal_ticks != 9 ||
        config.gate.window_ticks != 48 ||
        config.universe.enabled != true ||
        config.universe.update_interval_ticks != 30 ||
        config.universe.max_active_symbols != 2 ||
        config.universe.min_active_symbols != 1 ||
        config.universe.fallback_symbols.size() != 1 ||
        config.universe.fallback_symbols[0] != "ETHUSDT" ||
        config.universe.candidate_symbols.size() != 2 ||
        config.universe.candidate_symbols[0] != "ETHUSDT" ||
        config.universe.candidate_symbols[1] != "BTCUSDT" ||
        config.bybit.testnet != false ||
        config.bybit.demo_trading != true ||
        config.bybit.category != "linear" ||
        config.bybit.account_type != "UNIFIED" ||
        config.bybit.expected_account_mode != ai_trade::AccountMode::kUnified ||
        config.bybit.expected_margin_mode != ai_trade::MarginMode::kPortfolio ||
        config.bybit.expected_position_mode != ai_trade::PositionMode::kHedge ||
        config.bybit.public_ws_enabled != false ||
        config.bybit.public_ws_rest_fallback != true ||
        config.bybit.private_ws_enabled != true ||
        config.bybit.private_ws_rest_fallback != false ||
        config.bybit.execution_poll_limit != 25 ||
        !NearlyEqual(config.risk_thresholds.degraded_drawdown, 0.05) ||
        !NearlyEqual(config.risk_thresholds.cooldown_drawdown, 0.10) ||
        !NearlyEqual(config.risk_thresholds.fuse_drawdown, 0.18)) {
      std::cerr << "配置字段解析结果不符合预期\n";
      return 1;
    }

    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_config.yaml";
    std::ofstream out(temp_path);
    out << "execution:\n"
        << "  protection:\n"
        << "    enabled: true\n"
        << "    require_sl: false\n"
        << "    attach_timeout_ms: 1500\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法保护单配置应加载失败\n";
      return 1;
    }
    if (error.find("execution.protection") == std::string::npos) {
      std::cerr << "非法保护单配置错误信息不符合预期\n";
      return 1;
    }

    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_universe_config.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  primary_symbol: \"BTCUSDT\"\n"
        << "universe:\n"
        << "  min_active_symbols: 3\n"
        << "  max_active_symbols: 2\n"
        << "  fallback_symbols: [\"BTCUSDT\"]\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 Universe 配置应加载失败\n";
      return 1;
    }
    if (error.find("universe") == std::string::npos) {
      std::cerr << "非法 Universe 配置错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_bybit_poll_limit.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  primary_symbol: \"BTCUSDT\"\n"
        << "exchange:\n"
        << "  platform: \"bybit\"\n"
        << "  bybit:\n"
        << "    execution_poll_limit: 0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 Bybit execution_poll_limit 配置应加载失败\n";
      return 1;
    }
    if (error.find("execution_poll_limit") == std::string::npos) {
      std::cerr << "非法 Bybit execution_poll_limit 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_bybit_demo_testnet.yaml";
    std::ofstream out(temp_path);
    out << "exchange:\n"
        << "  platform: \"bybit\"\n"
        << "  bybit:\n"
        << "    testnet: true\n"
        << "    demo_trading: true\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "demo_trading 与 testnet 同时开启应加载失败\n";
      return 1;
    }
    if (error.find("demo_trading") == std::string::npos) {
      std::cerr << "demo_trading 与 testnet 冲突错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_execution_throttle.yaml";
    std::ofstream out(temp_path);
    out << "execution:\n"
        << "  min_order_interval_ms: -1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 execution.min_order_interval_ms 配置应加载失败\n";
      return 1;
    }
    if (error.find("min_order_interval_ms") == std::string::npos) {
      std::cerr << "非法 execution.min_order_interval_ms 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path wal_path =
        std::filesystem::temp_directory_path() / "ai_trade_test_trade.wal";
    std::error_code ec;
    std::filesystem::remove(wal_path, ec);

    ai_trade::WalStore wal(wal_path.string());
    std::string error;
    if (!wal.Initialize(&error)) {
      std::cerr << "WAL 初始化失败: " << error << "\n";
      return 1;
    }

    ai_trade::OrderIntent intent;
    intent.client_order_id = "cid-1";
    intent.symbol = "BTCUSDT";
    intent.purpose = ai_trade::OrderPurpose::kEntry;
    intent.reduce_only = false;
    intent.direction = 1;
    intent.qty = 2.0;
    intent.price = 100.0;

    if (!wal.AppendIntent(intent, &error)) {
      std::cerr << "WAL 追加 intent 失败: " << error << "\n";
      return 1;
    }
    ai_trade::FillEvent fill_1 = ToFill(intent, "fill-1");
    fill_1.qty = 0.8;
    ai_trade::FillEvent fill_2 = ToFill(intent, "fill-2");
    fill_2.qty = 1.2;

    if (!wal.AppendFill(fill_1, &error)) {
      std::cerr << "WAL 追加 fill_1 失败: " << error << "\n";
      return 1;
    }
    if (!wal.AppendFill(fill_2, &error)) {
      std::cerr << "WAL 追加 fill_2 失败: " << error << "\n";
      return 1;
    }
    if (!wal.AppendFill(fill_1, &error)) {
      std::cerr << "WAL 追加重复 fill_1 失败: " << error << "\n";
      return 1;
    }

    std::unordered_set<std::string> intent_ids;
    std::unordered_set<std::string> fill_ids;
    std::vector<ai_trade::FillEvent> fills;
    if (!wal.LoadState(&intent_ids, &fill_ids, &fills, &error)) {
      std::cerr << "WAL 加载失败: " << error << "\n";
      return 1;
    }

    if (intent_ids.size() != 1 || fill_ids.size() != 2 || fills.size() != 2) {
      std::cerr << "WAL 去重或加载计数不符合预期\n";
      return 1;
    }

    ai_trade::TradeSystem recovered_system(/*risk_cap_usd=*/500.0,
                                           /*max_order_notional_usd=*/200.0);
    for (const auto& fill : fills) {
      recovered_system.OnFill(fill);
    }
    if (!NearlyEqual(recovered_system.account().current_notional_usd(), 200.0)) {
      std::cerr << "WAL 回放后仓位恢复不符合预期\n";
      return 1;
    }

    std::filesystem::remove(wal_path);
  }

  {
    ai_trade::RiskEngine risk(/*max_abs_notional_usd=*/500.0);
    const ai_trade::TargetPosition target{"BTCUSDT", 500.0};

    const auto normal = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.01);
    if (normal.risk_mode != ai_trade::RiskMode::kNormal ||
        normal.reduce_only ||
        !NearlyEqual(normal.adjusted_notional_usd, 500.0)) {
      std::cerr << "风控 NORMAL 模式结果不符合预期\n";
      return 1;
    }

    const auto degraded = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.09);
    if (degraded.risk_mode != ai_trade::RiskMode::kDegraded ||
        degraded.reduce_only ||
        !NearlyEqual(degraded.adjusted_notional_usd, 250.0)) {
      std::cerr << "风控 DEGRADED 模式结果不符合预期\n";
      return 1;
    }

    const auto cooldown = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.13);
    if (cooldown.risk_mode != ai_trade::RiskMode::kCooldown ||
        !cooldown.reduce_only ||
        !NearlyEqual(cooldown.adjusted_notional_usd, 0.0)) {
      std::cerr << "风控 COOLDOWN 模式结果不符合预期\n";
      return 1;
    }

    const auto fuse = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.21);
    if (fuse.risk_mode != ai_trade::RiskMode::kFuse ||
        !fuse.reduce_only ||
        !NearlyEqual(fuse.adjusted_notional_usd, 0.0)) {
      std::cerr << "风控 FUSE 模式结果不符合预期\n";
      return 1;
    }

    risk.SetForcedReduceOnly(true);
    const auto forced = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.0);
    if (forced.risk_mode != ai_trade::RiskMode::kReduceOnly ||
        !forced.reduce_only) {
      std::cerr << "风控强制 reduce-only 模式不符合预期\n";
      return 1;
    }
  }

  {
    ai_trade::OrderManager oms;
    ai_trade::OrderIntent intent;
    intent.client_order_id = "oms-order-1";
    intent.symbol = "BTCUSDT";
    intent.direction = 1;
    intent.qty = 10.0;
    intent.price = 100.0;
    intent.purpose = ai_trade::OrderPurpose::kEntry;
    if (!oms.RegisterIntent(intent)) {
      std::cerr << "OMS 注册订单失败\n";
      return 1;
    }
    oms.MarkSent(intent.client_order_id);

    ai_trade::FillEvent partial;
    partial.fill_id = "fill-a";
    partial.client_order_id = intent.client_order_id;
    partial.symbol = intent.symbol;
    partial.direction = 1;
    partial.qty = 4.0;
    partial.price = 100.0;
    oms.OnFill(partial);
    const auto* r1 = oms.Find(intent.client_order_id);
    if (r1 == nullptr || r1->state != ai_trade::OrderState::kPartial) {
      std::cerr << "OMS partial 状态不符合预期\n";
      return 1;
    }

    ai_trade::FillEvent rest = partial;
    rest.fill_id = "fill-b";
    rest.qty = 6.0;
    oms.OnFill(rest);
    const auto* r2 = oms.Find(intent.client_order_id);
    if (r2 == nullptr || r2->state != ai_trade::OrderState::kFilled) {
      std::cerr << "OMS filled 状态不符合预期\n";
      return 1;
    }
  }

  {
    ai_trade::OrderManager oms;
    ai_trade::AccountState account;
    ai_trade::Reconciler reconciler(/*tolerance_notional_usd=*/1.0);

    ai_trade::OrderIntent intent;
    intent.client_order_id = "rec-order";
    intent.symbol = "BTCUSDT";
    intent.direction = 1;
    intent.qty = 1.0;
    intent.price = 100.0;
    intent.purpose = ai_trade::OrderPurpose::kEntry;
    oms.RegisterIntent(intent);
    oms.MarkSent(intent.client_order_id);

    account.OnMarket(ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0});
    ai_trade::FillEvent fill = ToFill(intent, "rec-fill-1");
    oms.OnFill(fill);
    account.ApplyFill(fill);
    account.OnMarket(ai_trade::MarketEvent{2, "BTCUSDT", 101.0, 101.0});

    const auto ok = reconciler.Check(account, oms);
    if (!ok.ok) {
      std::cerr << "预期对账成功但失败\n";
      return 1;
    }

    ai_trade::AccountState mismatch_account;
    mismatch_account.OnMarket(ai_trade::MarketEvent{1, "BTCUSDT", 120.0, 120.0});
    const auto mismatch = reconciler.Check(mismatch_account, oms);
    if (mismatch.ok || mismatch.reason_code != "OMS_RECONCILE_MISMATCH") {
      std::cerr << "预期对账异常未触发\n";
      return 1;
    }
  }

  {
    ai_trade::UniverseConfig config;
    config.enabled = true;
    config.update_interval_ticks = 2;
    config.max_active_symbols = 2;
    config.min_active_symbols = 1;
    config.fallback_symbols = {"BTCUSDT"};
    config.candidate_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT"};
    ai_trade::UniverseSelector selector(config, "BTCUSDT");
    if (selector.active_symbols().empty()) {
      std::cerr << "Universe 初始 active_symbols 不应为空\n";
      return 1;
    }

    const auto first = selector.OnMarket(ai_trade::MarketEvent{
        1, "BTCUSDT", 100.0, 100.0});
    if (first.has_value()) {
      std::cerr << "Universe 未到更新间隔不应刷新\n";
      return 1;
    }
    const auto second = selector.OnMarket(ai_trade::MarketEvent{
        2, "ETHUSDT", 2000.0, 2000.0});
    if (!second.has_value()) {
      std::cerr << "Universe 到更新间隔应刷新\n";
      return 1;
    }
    if (second->active_symbols.empty() ||
        static_cast<int>(second->active_symbols.size()) > config.max_active_symbols) {
      std::cerr << "Universe 刷新后的 active_symbols 数量不符合预期\n";
      return 1;
    }
    if (!selector.IsActive(second->active_symbols.front())) {
      std::cerr << "Universe 活跃币对判定不符合预期\n";
      return 1;
    }
  }

  {
    ai_trade::UniverseConfig config;
    config.enabled = true;
    config.update_interval_ticks = 1;
    config.max_active_symbols = 3;
    config.min_active_symbols = 1;
    config.fallback_symbols = {"BTCUSDT", "ETHUSDT"};
    config.candidate_symbols = {"BTCUSDT", "ETHUSDT"};

    ai_trade::UniverseSelector selector(config, "BTCUSDT");
    selector.SetAllowedSymbols({"BTCUSDT"});
    const auto update = selector.OnMarket(ai_trade::MarketEvent{
        1, "ETHUSDT", 2000.0, 2000.0});
    if (!update.has_value() || update->active_symbols.empty()) {
      std::cerr << "Universe 允许集合过滤后应仍有活跃币对\n";
      return 1;
    }
    for (const auto& symbol : update->active_symbols) {
      if (symbol != "BTCUSDT") {
        std::cerr << "Universe 允许集合过滤结果包含非允许symbol\n";
        return 1;
      }
    }
  }

  {
    ai_trade::UniverseConfig config;
    config.enabled = true;
    config.update_interval_ticks = 1;
    config.max_active_symbols = 2;
    config.min_active_symbols = 2;
    config.fallback_symbols = {"BTCUSDT", "ETHUSDT"};
    config.candidate_symbols = {"SOLUSDT"};
    ai_trade::UniverseSelector selector(config, "BTCUSDT");
    const auto update = selector.OnMarket(ai_trade::MarketEvent{
        1, "SOLUSDT", 80.0, 80.0});
    if (!update.has_value()) {
      std::cerr << "Universe 预期应触发刷新\n";
      return 1;
    }
    if (!update->degraded_to_fallback || update->reason_code.empty()) {
      std::cerr << "Universe 预期应触发 fallback 降级\n";
      return 1;
    }
    if (update->active_symbols.size() != 2U) {
      std::cerr << "Universe fallback 后活跃币对应满足最小数量\n";
      return 1;
    }
  }

  {
    ai_trade::MockExchangeAdapter adapter({100.0, 101.0});
    if (!adapter.Connect()) {
      std::cerr << "mock 连接失败\n";
      return 1;
    }
    ai_trade::MarketEvent event;
    if (!adapter.PollMarket(&event)) {
      std::cerr << "mock 行情拉取失败\n";
      return 1;
    }

    ai_trade::OrderIntent intent;
    intent.client_order_id = "remote-notional-order";
    intent.symbol = "BTCUSDT";
    intent.direction = 1;
    intent.qty = 2.0;
    intent.price = 100.0;
    if (!adapter.SubmitOrder(intent)) {
      std::cerr << "mock 下单失败\n";
      return 1;
    }

    ai_trade::FillEvent fill;
    while (adapter.PollFill(&fill)) {
    }
    double remote_notional = 0.0;
    if (!adapter.GetRemoteNotionalUsd(&remote_notional)) {
      std::cerr << "mock 远端仓位读取失败\n";
      return 1;
    }
    if (!NearlyEqual(remote_notional, 200.0)) {
      std::cerr << "mock 远端名义仓位不符合预期，实际 "
                << remote_notional << "\n";
      return 1;
    }

    ai_trade::ExchangeAccountSnapshot snapshot;
    if (!adapter.GetAccountSnapshot(&snapshot)) {
      std::cerr << "mock 账户快照读取失败\n";
      return 1;
    }
    if (snapshot.account_mode != ai_trade::AccountMode::kUnified ||
        snapshot.margin_mode != ai_trade::MarginMode::kIsolated ||
        snapshot.position_mode != ai_trade::PositionMode::kOneWay) {
      std::cerr << "mock 账户快照模式不符合预期\n";
      return 1;
    }

    ai_trade::SymbolInfo symbol_info;
    if (!adapter.GetSymbolInfo("BTCUSDT", &symbol_info) ||
        !symbol_info.tradable ||
        symbol_info.qty_step <= 0.0 ||
        symbol_info.price_tick <= 0.0) {
      std::cerr << "mock SymbolInfo 读取不符合预期\n";
      return 1;
    }

    std::vector<ai_trade::RemotePositionSnapshot> remote_positions;
    if (!adapter.GetRemotePositions(&remote_positions) ||
        remote_positions.size() != 1U ||
        remote_positions.front().symbol != "BTCUSDT" ||
        !NearlyEqual(remote_positions.front().qty, 2.0)) {
      std::cerr << "mock 远端持仓快照读取不符合预期\n";
      return 1;
    }
  }

  {
    ai_trade::MockExchangeAdapter adapter({100.0});
    if (!adapter.Connect()) {
      std::cerr << "mock 连接失败\n";
      return 1;
    }
    ai_trade::MarketEvent event;
    adapter.PollMarket(&event);

    ai_trade::OrderIntent intent;
    intent.client_order_id = "cancel-order";
    intent.symbol = "BTCUSDT";
    intent.direction = 1;
    intent.qty = 2.0;
    intent.price = 100.0;
    if (!adapter.SubmitOrder(intent)) {
      std::cerr << "mock 下单失败\n";
      return 1;
    }
    if (!adapter.CancelOrder(intent.client_order_id)) {
      std::cerr << "mock 撤单失败\n";
      return 1;
    }
    ai_trade::FillEvent fill;
    if (adapter.PollFill(&fill)) {
      std::cerr << "撤单后不应再有成交\n";
      return 1;
    }
  }

  {
    ai_trade::BybitPrivateStreamOptions ws_options;
    ws_options.enabled = false;
    ws_options.api_key = "k";
    ws_options.api_secret = "s";
    ai_trade::BybitPrivateStream stream(std::move(ws_options));
    std::string error;
    if (stream.Connect(&error)) {
      std::cerr << "禁用 private ws 时不应连接成功\n";
      return 1;
    }
    if (stream.Healthy()) {
      std::cerr << "禁用 private ws 时健康状态应为 false\n";
      return 1;
    }
    if (error.empty()) {
      std::cerr << "禁用 private ws 时应返回错误原因\n";
      return 1;
    }
  }

  {
    ai_trade::BybitPrivateStreamOptions ws_options;
    ws_options.enabled = true;
    ws_options.testnet = true;
    ws_options.api_key = "k";
    ws_options.api_secret = "s";
    ws_options.ack_timeout_ms = 1000;

    std::vector<ScriptedWsStep> script = {
        {ScriptedWsAction::kText, R"({"op":"auth","success":true})", ""},
        {ScriptedWsAction::kText, R"({"op":"subscribe","success":true})", ""},
        {ScriptedWsAction::kText, "{", ""},
        {ScriptedWsAction::kText,
         R"({"topic":"execution","data":[{"execId":"exec-1","orderLinkId":"cid-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.01"},{"execId":"exec-1","orderLinkId":"cid-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.01"}]})",
         ""},
        {ScriptedWsAction::kClosed, "", "peer closed"},
    };

    ai_trade::BybitPrivateStream stream(
        ws_options,
        std::make_unique<ScriptedWebsocketClient>(std::move(script)));
    std::string error;
    if (!stream.Connect(&error)) {
      std::cerr << "private ws 脚本连接失败: " << error << "\n";
      return 1;
    }

    ai_trade::FillEvent fill;
    if (stream.PollExecution(&fill)) {
      std::cerr << "异常 JSON 包不应产生成交\n";
      return 1;
    }
    if (!stream.PollExecution(&fill)) {
      std::cerr << "有效 execution 消息应产生成交\n";
      return 1;
    }
    if (fill.fill_id != "exec-1" || fill.client_order_id != "cid-1" ||
        fill.direction != 1 || !NearlyEqual(fill.qty, 1.0)) {
      std::cerr << "private ws execution 解析结果不符合预期\n";
      return 1;
    }
    if (stream.PollExecution(&fill)) {
      std::cerr << "重复 execId 不应再次产生成交\n";
      return 1;
    }
    if (stream.Healthy()) {
      std::cerr << "连接关闭后 private ws 应标记为不健康\n";
      return 1;
    }
  }

  {
    ai_trade::BybitPrivateStreamOptions ws_options;
    ws_options.enabled = true;
    ws_options.testnet = false;
    ws_options.demo_trading = true;
    ws_options.api_key = "k";
    ws_options.api_secret = "s";
    ws_options.ack_timeout_ms = 1000;

    std::vector<ScriptedWsStep> script = {
        {ScriptedWsAction::kText, R"({"op":"auth","success":true})", ""},
        {ScriptedWsAction::kText, R"({"op":"subscribe","success":true})", ""},
    };

    ai_trade::BybitPrivateStream stream(
        ws_options,
        std::make_unique<ScriptedWebsocketClient>(
            std::move(script),
            true,
            "",
            "wss://stream-demo.bybit.com/v5/private"));
    std::string error;
    if (!stream.Connect(&error)) {
      std::cerr << "demo private ws URL 连接失败: " << error << "\n";
      return 1;
    }
  }

  {
    ai_trade::BybitPublicStreamOptions ws_options;
    ws_options.enabled = true;
    ws_options.testnet = true;
    ws_options.category = "linear";
    ws_options.symbols = {"BTCUSDT"};
    ws_options.ack_timeout_ms = 1000;

    std::vector<ScriptedWsStep> script = {
        {ScriptedWsAction::kText, R"({"op":"subscribe","success":true})", ""},
        {ScriptedWsAction::kText, "{", ""},
        {ScriptedWsAction::kText,
         R"({"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT","lastPrice":"123.4","markPrice":"123.5"}})",
         ""},
    };
    ai_trade::BybitPublicStream stream(
        ws_options,
        std::make_unique<ScriptedWebsocketClient>(std::move(script)));
    std::string error;
    if (!stream.Connect(&error)) {
      std::cerr << "public ws 脚本连接失败: " << error << "\n";
      return 1;
    }

    ai_trade::MarketEvent event;
    if (stream.PollTicker(&event)) {
      std::cerr << "异常 JSON 包不应产出行情\n";
      return 1;
    }
    if (!stream.PollTicker(&event)) {
      std::cerr << "有效 ticker 消息应产出行情\n";
      return 1;
    }
    if (event.symbol != "BTCUSDT" || !NearlyEqual(event.price, 123.4) ||
        !NearlyEqual(event.mark_price, 123.5)) {
      std::cerr << "public ws ticker 解析结果不符合预期\n";
      return 1;
    }
  }

  {
    ScopedEnvVar api_key("AI_TRADE_API_KEY", "k");
    ScopedEnvVar api_secret("AI_TRADE_API_SECRET", "s");

    MockBybitHttpTransport transport;
    transport.AddRoute(
        "GET",
        "/v5/account/info",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"unifiedMarginStatus":3,"marginMode":"ISOLATED"}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/position/list",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","tradeMode":1,"positionIdx":0,"side":"Buy","positionValue":"0"}]}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/execution/list",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"execId":"rest-exec-1","orderLinkId":"cid-rest-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.02"},{"execId":"rest-exec-1","orderLinkId":"cid-rest-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.02"}]}})",
            .error = "",
        });

    ai_trade::BybitAdapterOptions options;
    options.mode = "paper";
    options.testnet = true;
    options.symbols = {"BTCUSDT"};
    options.primary_symbol = "BTCUSDT";
    options.public_ws_enabled = false;
    options.private_ws_enabled = true;
    options.private_ws_rest_fallback = true;
    options.execution_skip_history_on_start = false;
    options.execution_poll_limit = 10;
    options.http_transport_factory = [transport]() {
      return std::make_unique<MockBybitHttpTransport>(transport);
    };
    options.private_stream_factory =
        [](ai_trade::BybitPrivateStreamOptions ws_options) {
          std::vector<ScriptedWsStep> ws_script = {
              {ScriptedWsAction::kText, R"({"op":"auth","success":true})", ""},
              {ScriptedWsAction::kText, R"({"op":"subscribe","success":true})", ""},
              {ScriptedWsAction::kClosed, "", "ws down"},
          };
          return std::make_unique<ai_trade::BybitPrivateStream>(
              std::move(ws_options),
              std::make_unique<ScriptedWebsocketClient>(std::move(ws_script)));
        };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "bybit adapter 连接失败（private ws fallback 测试）\n";
      return 1;
    }

    ai_trade::FillEvent fill;
    if (!adapter.PollFill(&fill) && !adapter.PollFill(&fill)) {
      std::cerr << "private ws 回退后应从 REST 获取成交\n";
      return 1;
    }
    if (fill.fill_id != "rest-exec-1" || fill.client_order_id != "cid-rest-1" ||
        fill.direction != 1 || !NearlyEqual(fill.qty, 1.0)) {
      std::cerr << "private ws 回退 REST 成交解析不符合预期\n";
      return 1;
    }
    if (adapter.PollFill(&fill)) {
      std::cerr << "重复 REST execId 不应重复产生成交\n";
      return 1;
    }
    if (!adapter.TradeOk()) {
      std::cerr << "private ws 回退后 trade_ok 预期应为 true\n";
      return 1;
    }
  }

  {
    ScopedEnvVar api_key("AI_TRADE_API_KEY", "k");
    ScopedEnvVar api_secret("AI_TRADE_API_SECRET", "s");

    MockBybitHttpTransport transport;
    transport.AddRoute(
        "GET",
        "/v5/account/info",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"unifiedMarginStatus":3,"marginMode":"ISOLATED"}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/position/list",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","tradeMode":1,"positionIdx":0,"side":"Buy","positionValue":"0"}]}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/execution/list",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"execId":"hist-exec-1","orderLinkId":"cid-hist-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.02","execTime":"1730000000000"}]}})",
            .error = "",
        });

    ai_trade::BybitAdapterOptions options;
    options.mode = "paper";
    options.testnet = true;
    options.symbols = {"BTCUSDT"};
    options.primary_symbol = "BTCUSDT";
    options.public_ws_enabled = false;
    options.private_ws_enabled = false;
    options.execution_skip_history_on_start = true;
    options.http_transport_factory = [transport]() {
      return std::make_unique<MockBybitHttpTransport>(transport);
    };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "bybit adapter 连接失败（execution 启动预热测试）\n";
      return 1;
    }

    ai_trade::FillEvent fill;
    if (adapter.PollFill(&fill)) {
      std::cerr << "启动预热后不应消费历史 execution 成交\n";
      return 1;
    }
  }

  {
    ScopedEnvVar api_key("AI_TRADE_API_KEY", "k");
    ScopedEnvVar api_secret("AI_TRADE_API_SECRET", "s");

    MockBybitHttpTransport transport;
    transport.AddRoute(
        "GET",
        "/v5/account/info",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"unifiedMarginStatus":3,"marginMode":"ISOLATED"}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/position/list",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","tradeMode":1,"positionIdx":0,"side":"Buy","positionValue":"0"}]}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/market/tickers",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","lastPrice":"101.2","markPrice":"101.3"}]}})",
            .error = "",
        });

    ai_trade::BybitAdapterOptions options;
    options.mode = "paper";
    options.testnet = true;
    options.symbols = {"BTCUSDT"};
    options.primary_symbol = "BTCUSDT";
    options.public_ws_enabled = true;
    options.public_ws_rest_fallback = true;
    options.private_ws_enabled = false;
    options.http_transport_factory = [transport]() {
      return std::make_unique<MockBybitHttpTransport>(transport);
    };
    options.public_stream_factory = [](ai_trade::BybitPublicStreamOptions ws_options) {
      std::vector<ScriptedWsStep> ws_script = {
          {ScriptedWsAction::kText, R"({"op":"subscribe","success":true})", ""},
          {ScriptedWsAction::kClosed, "", "ws down"},
      };
      return std::make_unique<ai_trade::BybitPublicStream>(
          std::move(ws_options),
          std::make_unique<ScriptedWebsocketClient>(std::move(ws_script)));
    };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "bybit adapter 连接失败（public ws fallback 测试）\n";
      return 1;
    }

    ai_trade::MarketEvent event;
    if (!adapter.PollMarket(&event) && !adapter.PollMarket(&event)) {
      std::cerr << "public ws 回退后应从 REST 获取行情\n";
      return 1;
    }
    if (event.symbol != "BTCUSDT" || !NearlyEqual(event.price, 101.2) ||
        !NearlyEqual(event.mark_price, 101.3)) {
      std::cerr << "public ws 回退 REST 行情解析不符合预期\n";
      return 1;
    }
    if (!adapter.TradeOk()) {
      std::cerr << "public ws 回退后 trade_ok 预期应为 true\n";
      return 1;
    }
  }

  {
    ScopedEnvVar testnet_key("AI_TRADE_BYBIT_TESTNET_API_KEY", "k-testnet");
    ScopedEnvVar testnet_secret("AI_TRADE_BYBIT_TESTNET_API_SECRET", "s-testnet");
    ScopedEnvVar api_key("AI_TRADE_API_KEY", "");
    ScopedEnvVar api_secret("AI_TRADE_API_SECRET", "");

    MockBybitHttpTransport transport;
    transport.AddRoute(
        "GET",
        "/v5/account/info",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"unifiedMarginStatus":3,"marginMode":"ISOLATED"}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/position/list",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","tradeMode":1,"positionIdx":0,"side":"Buy","positionValue":"0"}]}})",
            .error = "",
        });

    ai_trade::BybitAdapterOptions options;
    options.mode = "paper";
    options.testnet = true;
    options.symbols = {"BTCUSDT"};
    options.public_ws_enabled = false;
    options.private_ws_enabled = false;
    options.http_transport_factory = [transport]() {
      return std::make_unique<MockBybitHttpTransport>(transport);
    };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "Bybit testnet 专用环境变量连接失败\n";
      return 1;
    }
  }

  {
    ScopedEnvVar api_key("AI_TRADE_API_KEY", "k");
    ScopedEnvVar api_secret("AI_TRADE_API_SECRET", "s");

    auto* transport = new MockBybitHttpTransport();
    transport->AddRoute(
        "GET",
        "/v5/account/info",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"unifiedMarginStatus":3,"marginMode":"ISOLATED"}})",
            .error = "",
        });
    transport->AddRoute(
        "GET",
        "/v5/position/list",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","tradeMode":1,"positionIdx":0,"side":"Buy","positionValue":"0"}]}})",
            .error = "",
        });
    transport->AddRoute(
        "GET",
        "/v5/market/instruments-info",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","lotSizeFilter":{"minOrderQty":"0.001","maxMktOrderQty":"100","qtyStep":"0.001","minNotionalValue":"5"}}]}})",
            .error = "",
        });
    transport->AddRoute(
        "POST",
        "/v5/order/create",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"orderId":"oid-1"}})",
            .error = "",
        });

    ai_trade::BybitAdapterOptions options;
    options.mode = "paper";
    options.testnet = true;
    options.symbols = {"BTCUSDT"};
    options.public_ws_enabled = false;
    options.private_ws_enabled = false;
    options.http_transport_factory = [transport]() {
      return std::unique_ptr<ai_trade::BybitHttpTransport>(transport);
    };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "Bybit 交易规则量化测试连接失败\n";
      return 1;
    }

    ai_trade::OrderIntent ok_intent;
    ok_intent.client_order_id = "qty-ok-1";
    ok_intent.symbol = "BTCUSDT";
    ok_intent.direction = 1;
    ok_intent.qty = 0.123456;
    ok_intent.price = 100.0;
    if (!adapter.SubmitOrder(ok_intent)) {
      std::cerr << "Bybit 数量量化后订单应提交成功\n";
      return 1;
    }
    if (transport->last_body().find("\"qty\":\"0.123\"") == std::string::npos) {
      std::cerr << "Bybit 数量量化结果不符合预期，body=" << transport->last_body() << "\n";
      return 1;
    }

    ai_trade::OrderIntent small_intent = ok_intent;
    small_intent.client_order_id = "qty-small-1";
    small_intent.qty = 0.0004;
    if (adapter.SubmitOrder(small_intent)) {
      std::cerr << "Bybit 小于最小下单数量的订单应被拒绝\n";
      return 1;
    }
  }

  {
    ScopedEnvVar demo_key("AI_TRADE_BYBIT_DEMO_API_KEY", "k-demo");
    ScopedEnvVar demo_secret("AI_TRADE_BYBIT_DEMO_API_SECRET", "s-demo");
    ScopedEnvVar api_key("AI_TRADE_API_KEY", "");
    ScopedEnvVar api_secret("AI_TRADE_API_SECRET", "");

    MockBybitHttpTransport transport;
    transport.AddRoute(
        "GET",
        "https://api-demo.bybit.com/v5/account/info",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"unifiedMarginStatus":3,"marginMode":"ISOLATED"}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "https://api-demo.bybit.com/v5/position/list",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","tradeMode":1,"positionIdx":0,"side":"Buy","positionValue":"0"}]}})",
            .error = "",
        });

    ai_trade::BybitAdapterOptions options;
    options.mode = "paper";
    options.testnet = false;
    options.demo_trading = true;
    options.symbols = {"BTCUSDT"};
    options.public_ws_enabled = false;
    options.private_ws_enabled = false;
    options.http_transport_factory = [transport]() {
      return std::make_unique<MockBybitHttpTransport>(transport);
    };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "Bybit demo 专用环境变量连接失败\n";
      return 1;
    }
  }

  {
    std::string signature;
    std::string error;
    const bool ok = ai_trade::BybitRestClient::BuildV5Signature(
        "test-secret",
        "1700000000000",
        "test-key",
        "5000",
        "category=linear&symbol=BTCUSDT",
        &signature,
        &error);
    if (!ok || signature.size() != 64) {
      std::cerr << "Bybit V5 签名生成失败: " << error << "\n";
      return 1;
    }
    for (char ch : signature) {
      if (std::isxdigit(static_cast<unsigned char>(ch)) == 0) {
        std::cerr << "Bybit V5 签名格式不符合预期\n";
        return 1;
      }
    }
  }

  {
    ai_trade::BybitAdapterOptions options;
    options.mode = "replay";
    options.allow_no_auth_in_replay = true;
    options.symbols = {"BTCUSDT", "ETHUSDT"};
    options.replay_prices = {100.0, 101.0, 102.0, 103.0};
    options.remote_account_mode = ai_trade::AccountMode::kUnified;
    options.remote_margin_mode = ai_trade::MarginMode::kPortfolio;
    options.remote_position_mode = ai_trade::PositionMode::kHedge;
    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "bybit 回放占位连接失败\n";
      return 1;
    }

    ai_trade::MarketEvent e1;
    ai_trade::MarketEvent e2;
    if (!adapter.PollMarket(&e1) || !adapter.PollMarket(&e2)) {
      std::cerr << "bybit 回放行情拉取失败\n";
      return 1;
    }
    if (e1.symbol == e2.symbol) {
      std::cerr << "bybit 回放行情应支持多币种轮询\n";
      return 1;
    }

    ai_trade::OrderIntent intent;
    intent.client_order_id = "bybit-replay-order-1";
    intent.symbol = e1.symbol;
    intent.direction = 1;
    intent.qty = 2.0;
    intent.price = e1.price;
    if (!adapter.SubmitOrder(intent)) {
      std::cerr << "bybit 回放下单失败\n";
      return 1;
    }
    ai_trade::FillEvent fill;
    int fill_count = 0;
    while (adapter.PollFill(&fill)) {
      ++fill_count;
    }
    if (fill_count < 1) {
      std::cerr << "bybit 回放预期至少一笔成交\n";
      return 1;
    }
    double remote_notional = 0.0;
    if (!adapter.GetRemoteNotionalUsd(&remote_notional)) {
      std::cerr << "bybit 回放远端仓位读取失败\n";
      return 1;
    }
    if (!NearlyEqual(remote_notional, 200.0)) {
      std::cerr << "bybit 回放远端名义仓位不符合预期，实际 "
                << remote_notional << "\n";
      return 1;
    }

    ai_trade::ExchangeAccountSnapshot snapshot;
    if (!adapter.GetAccountSnapshot(&snapshot)) {
      std::cerr << "bybit 账户快照读取失败\n";
      return 1;
    }
    if (snapshot.account_mode != ai_trade::AccountMode::kUnified ||
        snapshot.margin_mode != ai_trade::MarginMode::kPortfolio ||
        snapshot.position_mode != ai_trade::PositionMode::kHedge) {
      std::cerr << "bybit 账户快照字段不符合预期\n";
      return 1;
    }
  }

  {
    ai_trade::GateConfig gate_config;
    gate_config.min_effective_signals_per_window = 2;
    gate_config.min_fills_per_window = 1;
    gate_config.heartbeat_empty_signal_ticks = 3;
    gate_config.window_ticks = 3;
    ai_trade::GateMonitor monitor(gate_config);

    ai_trade::Signal signal;
    ai_trade::RiskAdjustedPosition adjusted;
    std::optional<ai_trade::OrderIntent> intent;

    if (monitor.OnDecision(signal, adjusted, intent).has_value()) {
      std::cerr << "首个无信号tick不应触发心跳告警\n";
      return 1;
    }
    if (monitor.OnTick().has_value()) {
      std::cerr << "窗口未结束不应返回 Gate 结果\n";
      return 1;
    }

    monitor.OnDecision(signal, adjusted, intent);
    monitor.OnTick();

    const auto heartbeat = monitor.OnDecision(signal, adjusted, intent);
    if (!heartbeat.has_value() || *heartbeat != "WARN_SIGNAL_HEARTBEAT_GAP") {
      std::cerr << "连续无有效信号未触发预期心跳告警\n";
      return 1;
    }
    const auto gate_result = monitor.OnTick();
    if (!gate_result.has_value() || gate_result->pass) {
      std::cerr << "预期 Gate 失败但未失败\n";
      return 1;
    }
    if (gate_result->fail_reasons.size() != 2) {
      std::cerr << "预期 Gate 失败原因包含信号与成交两个维度\n";
      return 1;
    }
  }

  {
    ai_trade::GateConfig gate_config;
    gate_config.min_effective_signals_per_window = 2;
    gate_config.min_fills_per_window = 1;
    gate_config.heartbeat_empty_signal_ticks = 10;
    gate_config.window_ticks = 3;
    ai_trade::GateMonitor monitor(gate_config);

    ai_trade::Signal signal;
    signal.suggested_notional_usd = 100.0;
    signal.direction = 1;
    ai_trade::RiskAdjustedPosition adjusted;
    adjusted.adjusted_notional_usd = 100.0;
    std::optional<ai_trade::OrderIntent> intent;

    monitor.OnDecision(signal, adjusted, intent);
    ai_trade::FillEvent gate_fill;
    gate_fill.fill_id = "gate-fill-1";
    monitor.OnFill(gate_fill);
    if (monitor.OnTick().has_value()) {
      std::cerr << "窗口未结束不应返回 Gate 结果\n";
      return 1;
    }

    monitor.OnDecision(signal, adjusted, intent);
    monitor.OnTick();

    monitor.OnDecision(signal, adjusted, intent);
    const auto gate_result = monitor.OnTick();
    if (!gate_result.has_value() || !gate_result->pass) {
      std::cerr << "预期 Gate 通过但失败\n";
      return 1;
    }
    if (gate_result->raw_signals != 3 ||
        gate_result->order_intents != 0 ||
        gate_result->effective_signals != 3 ||
        gate_result->fills != 1) {
      std::cerr << "Gate 统计结果不符合预期\n";
      return 1;
    }
  }

  return 0;
}
