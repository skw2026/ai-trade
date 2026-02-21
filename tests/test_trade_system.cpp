#include <cmath>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <optional>
#include <unordered_set>
#include <utility>
#include <vector>

#include "app/intent_policy.h"
#include "core/config.h"
#include "exchange/bybit_exchange_adapter.h"
#include "exchange/bybit_private_stream.h"
#include "exchange/bybit_public_stream.h"
#include "exchange/bybit_rest_client.h"
#include "exchange/mock_exchange_adapter.h"
#include "exchange/websocket_client.h"
#include "evolution/self_evolution_controller.h"
#include "execution/execution_engine.h"
#include "execution/order_throttle.h"
#include "monitor/gate_monitor.h"
#include "oms/order_manager.h"
#include "oms/reconciler.h"
#include "regime/regime_engine.h"
#include "research/ic_evaluator.h"
#include "research/miner.h"
#include "research/online_feature_engine.h"
#include "research/time_series_operators.h"
#include "risk/risk_engine.h"
#include "storage/wal_store.h"
#include "system/trade_system.h"
#include "universe/universe_selector.h"

namespace {

// 该测试文件覆盖最小闭环关键链路：
// - 配置解析与运行时覆盖；
// - 策略/风控/执行/对账核心逻辑；
// - Bybit REST/WS 适配器与降级路径。
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

bool WriteIntegratorReportFile(const std::filesystem::path& path,
                               double auc_mean,
                               double delta_auc,
                               int split_trained_count,
                               int split_count,
                               std::string* out_error) {
  std::ofstream out(path);
  if (!out.is_open()) {
    if (out_error != nullptr) {
      *out_error = "无法写入 integrator_report: " + path.string();
    }
    return false;
  }
  out << "{\n"
      << "  \"model_version\": \"integrator_cb_v1_test\",\n"
      << "  \"metrics_oos\": {\n"
      << "    \"auc_mean\": " << auc_mean << ",\n"
      << "    \"delta_auc_vs_baseline\": " << delta_auc << ",\n"
      << "    \"split_trained_count\": " << split_trained_count << ",\n"
      << "    \"split_count\": " << split_count << "\n"
      << "  }\n"
      << "}\n";
  if (!out.good()) {
    if (out_error != nullptr) {
      *out_error = "写入 integrator_report 失败: " + path.string();
    }
    return false;
  }
  return true;
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

/**
 * @brief 多连接会话脚本 WS 客户端
 *
 * 用途：
 * 1. 精确模拟“首连可用 -> 断线 -> 重连恢复”的时序；
 * 2. 验证适配器在降级到 REST 后是否能自动切回 WS。
 */
class SessionScriptedWebsocketClient final : public ai_trade::WebsocketClient {
 public:
  explicit SessionScriptedWebsocketClient(
      std::vector<std::vector<ScriptedWsStep>> sessions,
      std::string expected_url = {})
      : sessions_(std::move(sessions)),
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
    if (session_index_ >= sessions_.size()) {
      if (out_error != nullptr) {
        *out_error = "no scripted ws session";
      }
      connected_ = false;
      return false;
    }
    connected_ = true;
    step_index_ = 0;
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
    if (session_index_ >= sessions_.size()) {
      out_payload->clear();
      return ai_trade::WsPollStatus::kNoMessage;
    }

    const auto& session = sessions_[session_index_];
    if (step_index_ >= session.size()) {
      out_payload->clear();
      return ai_trade::WsPollStatus::kNoMessage;
    }

    const ScriptedWsStep& step = session[step_index_++];
    switch (step.action) {
      case ScriptedWsAction::kText:
        *out_payload = step.payload;
        return ai_trade::WsPollStatus::kMessage;
      case ScriptedWsAction::kNoMessage:
        out_payload->clear();
        return ai_trade::WsPollStatus::kNoMessage;
      case ScriptedWsAction::kClosed:
        connected_ = false;
        ++session_index_;
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
  std::vector<std::vector<ScriptedWsStep>> sessions_;
  std::size_t session_index_{0};
  std::size_t step_index_{0};
  bool connected_{false};
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
    // 使用极短周期的 EMA 以便在第 2 个 tick 就能触发信号
    ai_trade::StrategyConfig fast_strategy;
    fast_strategy.trend_ema_fast = 1;
    fast_strategy.trend_ema_slow = 2;

    ai_trade::TradeSystem system(/*risk_cap_usd=*/500.0,
                                 /*max_order_notional_usd=*/200.0,
                                 ai_trade::RiskThresholds{}, fast_strategy);
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
    ai_trade::StrategyConfig fast_strategy;
    fast_strategy.trend_ema_fast = 1;
    fast_strategy.trend_ema_slow = 2;

    ai_trade::TradeSystem system(/*risk_cap_usd=*/500.0,
                                 /*max_order_notional_usd=*/200.0,
                                 ai_trade::RiskThresholds{}, fast_strategy);
    system.OnPrice(100.0, true);
    // 当 trade_ok=false 时，风控应进入只减仓路径。
    system.OnPrice(100.0, false);
    if (!NearlyEqual(system.account().current_notional_usd(), 0.0)) {
      std::cerr << "无有效信号时预期仓位保持 0\n";
      return 1;
    }
  }

  {
    ai_trade::StrategyConfig fast_strategy;
    fast_strategy.trend_ema_fast = 1;
    fast_strategy.trend_ema_slow = 2;

    ai_trade::TradeSystem system(/*risk_cap_usd=*/500.0,
                                 /*max_order_notional_usd=*/200.0,
                                 ai_trade::RiskThresholds{}, fast_strategy);
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
    // GetActiveSymbols 仅应返回非零仓位 symbol，避免零仓位污染集中度统计。
    ai_trade::AccountState account;
    account.OnMarket(ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0});
    account.OnMarket(ai_trade::MarketEvent{2, "ETHUSDT", 2000.0, 2000.0});

    ai_trade::FillEvent btc_open;
    btc_open.fill_id = "active-btc-open";
    btc_open.client_order_id = "active-btc-open-order";
    btc_open.symbol = "BTCUSDT";
    btc_open.direction = 1;
    btc_open.qty = 1.0;
    btc_open.price = 100.0;
    account.ApplyFill(btc_open);

    ai_trade::FillEvent btc_close;
    btc_close.fill_id = "active-btc-close";
    btc_close.client_order_id = "active-btc-close-order";
    btc_close.symbol = "BTCUSDT";
    btc_close.direction = -1;
    btc_close.qty = 1.0;
    btc_close.price = 100.0;
    account.ApplyFill(btc_close);

    ai_trade::FillEvent eth_open;
    eth_open.fill_id = "active-eth-open";
    eth_open.client_order_id = "active-eth-open-order";
    eth_open.symbol = "ETHUSDT";
    eth_open.direction = -1;
    eth_open.qty = 0.5;
    eth_open.price = 2000.0;
    account.ApplyFill(eth_open);

    const auto active_symbols = account.GetActiveSymbols();
    const std::unordered_set<std::string> active_set(active_symbols.begin(),
                                                     active_symbols.end());
    if (active_set.size() != 1 || active_set.count("ETHUSDT") != 1) {
      std::cerr << "活跃 symbol 仅应包含非零仓位 ETHUSDT\n";
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
        intent->purpose != ai_trade::OrderPurpose::kReduce ||
        intent->client_order_id.empty()) {
      std::cerr << "预期订单包含 reduce_only/purpose/client_order_id\n";
      return 1;
    }
  }

  {
    ai_trade::ExecutionEngine execution(ai_trade::ExecutionEngineConfig{
        .max_order_notional_usd = 500.0,
        .min_rebalance_notional_usd = 0.0,
    });
    const ai_trade::RiskAdjustedPosition reverse_target{
        .symbol = "BTCUSDT",
        .adjusted_notional_usd = 300.0,
        .reduce_only = false,
    };
    const auto reverse_intent = execution.BuildIntent(reverse_target,
                                                      /*current_notional_usd=*/-400.0,
                                                      /*price=*/100.0);
    if (!reverse_intent.has_value()) {
      std::cerr << "反向切仓第一步应产生平仓单\n";
      return 1;
    }
    if (!reverse_intent->reduce_only ||
        reverse_intent->purpose != ai_trade::OrderPurpose::kReduce ||
        reverse_intent->direction != 1 ||
        !NearlyEqual(reverse_intent->qty, 4.0)) {
      std::cerr << "反向切仓第一步语义不符合预期\n";
      return 1;
    }

    const auto open_intent = execution.BuildIntent(reverse_target,
                                                   /*current_notional_usd=*/0.0,
                                                   /*price=*/100.0);
    if (!open_intent.has_value()) {
      std::cerr << "反向切仓第二步应产生开仓单\n";
      return 1;
    }
    if (open_intent->reduce_only ||
        open_intent->purpose != ai_trade::OrderPurpose::kEntry ||
        open_intent->direction != 1 ||
        !NearlyEqual(open_intent->qty, 3.0)) {
      std::cerr << "反向切仓第二步语义不符合预期\n";
      return 1;
    }
  }

  {
    ai_trade::OrderIntent opening_intent;
    opening_intent.purpose = ai_trade::OrderPurpose::kEntry;
    opening_intent.reduce_only = false;
    if (!ai_trade::ShouldFilterInactiveSymbolIntent(opening_intent)) {
      std::cerr << "非活跃币对应拦截开仓意图\n";
      return 1;
    }
  }

  {
    ai_trade::OrderIntent reduce_intent;
    reduce_intent.purpose = ai_trade::OrderPurpose::kReduce;
    reduce_intent.reduce_only = true;
    if (ai_trade::ShouldFilterInactiveSymbolIntent(reduce_intent)) {
      std::cerr << "非活跃币对应放行减仓意图\n";
      return 1;
    }

    ai_trade::OrderIntent sl_intent;
    sl_intent.purpose = ai_trade::OrderPurpose::kSl;
    sl_intent.reduce_only = true;
    if (ai_trade::ShouldFilterInactiveSymbolIntent(sl_intent)) {
      std::cerr << "非活跃币对应放行保护单意图\n";
      return 1;
    }
  }

  {
    if (!ai_trade::ShouldSkipInactiveSymbolDecision(
            /*is_symbol_active=*/false,
            /*current_symbol_notional_usd=*/0.0,
            /*has_pending_symbol_net_orders=*/false)) {
      std::cerr << "inactive 且无仓位无在途单应跳过决策\n";
      return 1;
    }
    if (ai_trade::ShouldSkipInactiveSymbolDecision(
            /*is_symbol_active=*/true,
            /*current_symbol_notional_usd=*/0.0,
            /*has_pending_symbol_net_orders=*/false)) {
      std::cerr << "active symbol 不应跳过决策\n";
      return 1;
    }
    if (ai_trade::ShouldSkipInactiveSymbolDecision(
            /*is_symbol_active=*/false,
            /*current_symbol_notional_usd=*/10.0,
            /*has_pending_symbol_net_orders=*/false)) {
      std::cerr << "inactive 但有仓位不应跳过决策\n";
      return 1;
    }
    if (ai_trade::ShouldSkipInactiveSymbolDecision(
            /*is_symbol_active=*/false,
            /*current_symbol_notional_usd=*/0.0,
            /*has_pending_symbol_net_orders=*/true)) {
      std::cerr << "inactive 但有在途净仓位订单不应跳过决策\n";
      return 1;
    }
  }

  {
    ai_trade::ExecutionEngine execution(ai_trade::ExecutionEngineConfig{
        .max_order_notional_usd = 1000.0,
        .min_rebalance_notional_usd = 80.0,
    });
    const ai_trade::RiskAdjustedPosition small_adjust{
        .symbol = "ETHUSDT",
        .adjusted_notional_usd = 1050.0,
        .reduce_only = false,
    };
    const auto skipped = execution.BuildIntent(small_adjust,
                                               /*current_notional_usd=*/1000.0,
                                               /*price=*/100.0);
    if (skipped.has_value()) {
      std::cerr << "小于 min_rebalance_notional 的调仓应被抑制\n";
      return 1;
    }
  }

  {
    ai_trade::StrategyEngine strategy(ai_trade::StrategyConfig{
        .signal_notional_usd = 1000.0,
        .signal_deadband_abs = 0.2,
        .min_hold_ticks = 3,
        .trend_ema_fast = 1, // 快速响应测试
        .trend_ema_slow = 2,
    });
    ai_trade::AccountState dummy_account;
    ai_trade::RegimeState dummy_regime;

    // 预热数据以启动 EMA
    for (int i = 0; i < 10; ++i) {
        strategy.OnMarket(ai_trade::MarketEvent{
            1, "BTCUSDT", 100.0, 100.0}, dummy_account, dummy_regime);
    }

    const auto warmup = strategy.OnMarket(ai_trade::MarketEvent{
        1, "BTCUSDT", 100.0, 100.0}, dummy_account, dummy_regime);
    if (warmup.direction != 0) {
      std::cerr << "策略预热阶段不应输出方向\n";
      return 1;
    }

    // 价格上行: 100 -> 101. EMA(1)=101, EMA(2)=100.66 -> Cross Up
    const auto long_signal = strategy.OnMarket(ai_trade::MarketEvent{
        2, "BTCUSDT", 101.0, 101.0}, dummy_account, dummy_regime);
    if (long_signal.direction != 1) {
      std::cerr << "预期上行触发多头信号\n";
      return 1;
    }

    // 仍在最小持有期内，反向信号应被抑制并保持原方向。
    const auto suppressed_1 = strategy.OnMarket(ai_trade::MarketEvent{
        3, "BTCUSDT", 100.0, 100.0}, dummy_account, dummy_regime);
    const auto suppressed_2 = strategy.OnMarket(ai_trade::MarketEvent{
        4, "BTCUSDT", 99.0, 99.0}, dummy_account, dummy_regime);
    const auto suppressed_3 = strategy.OnMarket(ai_trade::MarketEvent{
        5, "BTCUSDT", 98.0, 98.0}, dummy_account, dummy_regime);
    if (suppressed_1.direction != 1 ||
        suppressed_2.direction != 1 ||
        suppressed_3.direction != 1) {
      std::cerr << "最小持有期内反向信号应被抑制\n";
      return 1;
    }

    // 超过最小持有期后，允许反向。
    const auto reversed = strategy.OnMarket(ai_trade::MarketEvent{
        6, "BTCUSDT", 97.0, 97.0}, dummy_account, dummy_regime);
    if (reversed.direction != -1 ||
        !NearlyEqual(reversed.suggested_notional_usd, -1000.0)) {
      std::cerr << "最小持有期后应允许反向信号\n";
      return 1;
    }
  }

  {
    // 防御分支：当阈值过高时，不应产生 defensive 分量。
    ai_trade::StrategyEngine strategy(ai_trade::StrategyConfig{
        .signal_notional_usd = 1000.0,
        .signal_deadband_abs = 0.0,
        .min_hold_ticks = 0,
        .trend_ema_fast = 1,
        .trend_ema_slow = 2,
        .defensive_notional_ratio = 1.0,
        .defensive_entry_score = 100.0,
        .defensive_trend_scale = 1.0,
        .defensive_range_scale = 1.0,
        .defensive_extreme_scale = 1.0,
    });
    ai_trade::AccountState dummy_account;
    ai_trade::RegimeState regime;
    regime.bucket = ai_trade::RegimeBucket::kRange;
    regime.warmup = false;
    regime.volatility_level = 0.001;

    for (int i = 0; i < 30; ++i) {
      strategy.OnMarket(ai_trade::MarketEvent{
          100 + i, "BTCUSDT", 100.0, 100.0}, dummy_account, regime);
    }
    const auto signal = strategy.OnMarket(ai_trade::MarketEvent{
        200, "BTCUSDT", 101.0, 101.0}, dummy_account, regime);
    if (signal.trend_notional_usd <= 0.0 ||
        !NearlyEqual(signal.defensive_notional_usd, 0.0, 1e-9)) {
      std::cerr << "防御阈值过高时不应产生 defensive 分量\n";
      return 1;
    }
  }

  {
    // 防御分支：偏离足够大时，方向应与 trend 相反（均值回归）。
    ai_trade::StrategyEngine strategy(ai_trade::StrategyConfig{
        .signal_notional_usd = 1000.0,
        .signal_deadband_abs = 0.0,
        .min_hold_ticks = 0,
        .trend_ema_fast = 1,
        .trend_ema_slow = 2,
        .defensive_notional_ratio = 1.0,
        .defensive_entry_score = 0.5,
        .defensive_trend_scale = 1.0,
        .defensive_range_scale = 1.0,
        .defensive_extreme_scale = 1.0,
    });
    ai_trade::AccountState dummy_account;
    ai_trade::RegimeState regime;
    regime.bucket = ai_trade::RegimeBucket::kRange;
    regime.warmup = false;
    regime.volatility_level = 0.001;

    for (int i = 0; i < 30; ++i) {
      strategy.OnMarket(ai_trade::MarketEvent{
          200 + i, "BTCUSDT", 100.0, 100.0}, dummy_account, regime);
    }
    const auto signal = strategy.OnMarket(ai_trade::MarketEvent{
        300, "BTCUSDT", 101.0, 101.0}, dummy_account, regime);
    if (signal.trend_notional_usd <= 0.0 ||
        signal.defensive_notional_usd >= 0.0 ||
        !NearlyEqual(signal.suggested_notional_usd,
                     signal.trend_notional_usd + signal.defensive_notional_usd,
                     1e-6)) {
      std::cerr << "防御分支方向或混合结果不符合预期\n";
      return 1;
    }
  }

  {
    // 防御分支：TREND 桶 scale=0 时，应完全关闭 defensive 分量。
    ai_trade::StrategyEngine strategy(ai_trade::StrategyConfig{
        .signal_notional_usd = 1000.0,
        .signal_deadband_abs = 0.0,
        .min_hold_ticks = 0,
        .trend_ema_fast = 1,
        .trend_ema_slow = 2,
        .defensive_notional_ratio = 1.0,
        .defensive_entry_score = 0.5,
        .defensive_trend_scale = 0.0,
        .defensive_range_scale = 1.0,
        .defensive_extreme_scale = 1.0,
    });
    ai_trade::AccountState dummy_account;
    ai_trade::RegimeState regime;
    regime.bucket = ai_trade::RegimeBucket::kTrend;
    regime.warmup = false;
    regime.volatility_level = 0.001;

    for (int i = 0; i < 30; ++i) {
      strategy.OnMarket(ai_trade::MarketEvent{
          300 + i, "BTCUSDT", 100.0, 100.0}, dummy_account, regime);
    }
    const auto signal = strategy.OnMarket(ai_trade::MarketEvent{
        400, "BTCUSDT", 101.0, 101.0}, dummy_account, regime);
    if (signal.trend_notional_usd <= 0.0 ||
        !NearlyEqual(signal.defensive_notional_usd, 0.0, 1e-9)) {
      std::cerr << "TREND 桶 defensive_scale=0 时应关闭防御分量\n";
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
    ai_trade::AccountState account;
    account.SyncFromRemotePositions({
        ai_trade::RemotePositionSnapshot{
            .symbol = "BTCUSDT",
            .qty = 1.0,
            .avg_entry_price = 100.0,
            .mark_price = 100.0,
        },
    });

    ai_trade::RemoteAccountBalanceSnapshot balance;
    balance.equity_usd = 12000.0;
    balance.wallet_balance_usd = 11000.0;
    balance.unrealized_pnl_usd = 1000.0;
    balance.has_equity = true;
    balance.has_wallet_balance = true;
    balance.has_unrealized_pnl = true;
    account.SyncFromRemoteAccountBalance(balance, /*reset_peak_to_equity=*/true);

    if (!NearlyEqual(account.equity_usd(), 12000.0, 1e-9)) {
      std::cerr << "远端资金同步后权益不符合预期，实际 "
                << account.equity_usd() << "\n";
      return 1;
    }
    if (!NearlyEqual(account.drawdown_pct(), 0.0, 1e-9)) {
      std::cerr << "启动同步后回撤应重置为0，实际 " << account.drawdown_pct()
                << "\n";
      return 1;
    }

    // 运行中同步到更低权益，不应重置峰值，应体现正回撤。
    ai_trade::RemoteAccountBalanceSnapshot lower_equity;
    lower_equity.equity_usd = 11900.0;
    lower_equity.has_equity = true;
    account.SyncFromRemoteAccountBalance(lower_equity,
                                         /*reset_peak_to_equity=*/false);
    if (account.drawdown_pct() <= 0.0) {
      std::cerr << "运行时权益低于峰值时，回撤应大于0\n";
      return 1;
    }
  }

  {
    ai_trade::AccountState account;
    ai_trade::FillEvent local_fill;
    local_fill.fill_id = "local-fill-1";
    local_fill.client_order_id = "local-order-1";
    local_fill.symbol = "BTCUSDT";
    local_fill.direction = 1;
    local_fill.qty = 1.0;
    local_fill.price = 100.0;
    local_fill.fee = 50.0;
    account.ApplyFill(local_fill);

    // 刷新远端风险字段时，不应重置现金基线；同时应补录远端新增 symbol 的风险样本。
    account.RefreshRiskFromRemotePositions({
        ai_trade::RemotePositionSnapshot{
            .symbol = "BTCUSDT",
            .qty = 1.0,
            .avg_entry_price = 100.0,
            .mark_price = 100.0,
            .liquidation_price = 90.0,
        },
        ai_trade::RemotePositionSnapshot{
            .symbol = "ETHUSDT",
            .qty = 2.0,
            .avg_entry_price = 2000.0,
            .mark_price = 2000.0,
            .liquidation_price = 1800.0,
        },
    });

    if (!NearlyEqual(account.position_qty("BTCUSDT"), 1.0) ||
        !NearlyEqual(account.position_qty("ETHUSDT"), 2.0)) {
      std::cerr << "运行时风险刷新后仓位视图不符合预期\n";
      return 1;
    }
    if (!NearlyEqual(account.equity_usd(), 9950.0)) {
      std::cerr << "运行时风险刷新不应重置现金基线，实际 equity="
                << account.equity_usd() << "\n";
      return 1;
    }
    if (!NearlyEqual(account.liquidation_distance_p95(), 0.10, 1e-9)) {
      std::cerr << "运行时风险刷新后强平距离统计不符合预期\n";
      return 1;
    }
  }

  {
    ai_trade::AccountState account;
    ai_trade::FillEvent local_fill;
    local_fill.fill_id = "force-sync-fill-1";
    local_fill.client_order_id = "force-sync-order-1";
    local_fill.symbol = "BTCUSDT";
    local_fill.direction = 1;
    local_fill.qty = 1.0;
    local_fill.price = 100.0;
    local_fill.fee = 10.0;
    account.ApplyFill(local_fill);

    const double equity_before = account.equity_usd();
    account.ForceSyncPositionsFromRemote({
        ai_trade::RemotePositionSnapshot{
            .symbol = "BTCUSDT",
            .qty = 0.5,
            .avg_entry_price = 100.0,
            .mark_price = 100.0,
            .liquidation_price = 80.0,
        },
    });

    if (!NearlyEqual(account.position_qty("BTCUSDT"), 0.5, 1e-9)) {
      std::cerr << "强制远端仓位对齐后数量不符合预期\n";
      return 1;
    }
    if (!NearlyEqual(account.equity_usd(), equity_before, 1e-9)) {
      std::cerr << "强制远端仓位对齐不应重置现金口径\n";
      return 1;
    }
  }

  {
    ai_trade::AccountState account;
    // 覆盖强平距离加权 P95 计算：
    // dist/weight 分别为：
    // 1) 0.08 / 100
    // 2) 0.30 / 200
    // 3) 0.04 / 100
    // 总权重 400，P95 对应权重阈值 380，预期命中 0.30。
    const std::vector<ai_trade::RemotePositionSnapshot> remote_positions = {
        ai_trade::RemotePositionSnapshot{
            .symbol = "A",
            .qty = 1.0,
            .avg_entry_price = 100.0,
            .mark_price = 100.0,
            .liquidation_price = 92.0,
        },
        ai_trade::RemotePositionSnapshot{
            .symbol = "B",
            .qty = 2.0,
            .avg_entry_price = 100.0,
            .mark_price = 100.0,
            .liquidation_price = 70.0,
        },
        ai_trade::RemotePositionSnapshot{
            .symbol = "C",
            .qty = 1.0,
            .avg_entry_price = 100.0,
            .mark_price = 100.0,
            .liquidation_price = 96.0,
        }};
    account.SyncFromRemotePositions(remote_positions);
    const double liq_p95 = account.liquidation_distance_p95();
    if (!NearlyEqual(liq_p95, 0.30, 1e-9)) {
      std::cerr << "强平距离加权P95计算不符合预期，实际 " << liq_p95 << "\n";
      return 1;
    }
  }

  {
    // 多币种账户级总名义敞口裁剪：
    // 风险上限 1000，其他币对已占用 500，则当前 symbol 最大可用 500。
    ai_trade::TradeSystem system(
        /*risk_cap_usd=*/1000.0,
        /*max_order_notional_usd=*/1000.0,
        ai_trade::RiskThresholds{},
        ai_trade::StrategyConfig{
            .signal_notional_usd = 900.0,
            .signal_deadband_abs = 0.1,
            .min_hold_ticks = 0,
        },
        /*min_rebalance_notional_usd=*/0.0);

    system.SyncAccountFromRemotePositions({
        ai_trade::RemotePositionSnapshot{
            .symbol = "ETHUSDT",
            .qty = 5.0,
            .avg_entry_price = 100.0,
            .mark_price = 100.0,
            .liquidation_price = 80.0,
        },
    });

    const auto warmup = system.Evaluate(
        ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0}, true);
    if (warmup.intent.has_value()) {
      std::cerr << "预热阶段不应产生订单意图\n";
      return 1;
    }

    const auto decision = system.Evaluate(
        ai_trade::MarketEvent{2, "BTCUSDT", 101.0, 101.0}, true);
    if (!NearlyEqual(decision.risk_adjusted.adjusted_notional_usd, 500.0, 1e-6)) {
      std::cerr << "账户级总名义敞口裁剪不符合预期，实际 "
                << decision.risk_adjusted.adjusted_notional_usd << "\n";
      return 1;
    }
  }

  {
    // 当强平距离低于阈值时，风控应切换为只减仓（reduce-only）。
    ai_trade::RiskThresholds thresholds;
    thresholds.min_liquidation_distance = 0.10;
    ai_trade::TradeSystem system(
        /*risk_cap_usd=*/1000.0,
        /*max_order_notional_usd=*/1000.0,
        thresholds,
        ai_trade::StrategyConfig{
            .signal_notional_usd = 1000.0,
            .signal_deadband_abs = 0.1,
            .min_hold_ticks = 0,
        },
        /*min_rebalance_notional_usd=*/0.0);

    system.SyncAccountFromRemotePositions({
        ai_trade::RemotePositionSnapshot{
            .symbol = "BTCUSDT",
            .qty = 1.0,
            .avg_entry_price = 100.0,
            .mark_price = 100.0,
            .liquidation_price = 95.0,  // 仅 5%
        },
    });

    system.Evaluate(ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0}, true);
    const auto decision = system.Evaluate(
        ai_trade::MarketEvent{2, "BTCUSDT", 101.0, 101.0}, true);
    if (decision.risk_adjusted.risk_mode != ai_trade::RiskMode::kReduceOnly ||
        !decision.risk_adjusted.reduce_only) {
      std::cerr << "低强平距离下预期切换到只减仓模式\n";
      return 1;
    }
  }

  {
    // 阶段2最小自进化：启用权重后，目标名义值应按 trend 权重缩放。
    ai_trade::TradeSystem system(
        /*risk_cap_usd=*/2000.0,
        /*max_order_notional_usd=*/2000.0,
        ai_trade::RiskThresholds{},
        ai_trade::StrategyConfig{
            .signal_notional_usd = 1000.0,
            .signal_deadband_abs = 0.1,
            .min_hold_ticks = 0,
        },
        /*min_rebalance_notional_usd=*/0.0);
    system.EnableEvolution(true);
    std::string error;
    if (!system.SetEvolutionWeights(0.60, 0.40, &error)) {
      std::cerr << "自进化权重设置失败: " << error << "\n";
      return 1;
    }

    // 预热 tick。
    system.Evaluate(ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0}, true);
    const auto decision =
        system.Evaluate(ai_trade::MarketEvent{2, "BTCUSDT", 101.0, 101.0}, true);
    if (!NearlyEqual(decision.target.target_notional_usd, 600.0, 1e-9)) {
      std::cerr << "自进化权重缩放后的目标名义值不符合预期，实际 "
                << decision.target.target_notional_usd << "\n";
      return 1;
    }
  }

  {
    // 双分支混合：evolution 权重应在 trend/defensive 分量之间真实切换。
    ai_trade::StrategyConfig dual_strategy;
    dual_strategy.signal_notional_usd = 1000.0;
    dual_strategy.signal_deadband_abs = 0.1;
    dual_strategy.min_hold_ticks = 0;
    dual_strategy.trend_ema_fast = 1;
    dual_strategy.trend_ema_slow = 2;
    dual_strategy.defensive_notional_ratio = 1.0;
    dual_strategy.defensive_entry_score = 0.5;
    dual_strategy.defensive_trend_scale = 1.0;
    dual_strategy.defensive_range_scale = 1.0;
    dual_strategy.defensive_extreme_scale = 1.0;

    ai_trade::RegimeConfig regime_config;
    regime_config.enabled = false;

    ai_trade::TradeSystem trend_only_system(
        /*risk_cap_usd=*/3000.0,
        /*max_order_notional_usd=*/3000.0,
        ai_trade::RiskThresholds{},
        dual_strategy,
        /*min_rebalance_notional_usd=*/0.0,
        regime_config);
    ai_trade::TradeSystem defensive_only_system(
        /*risk_cap_usd=*/3000.0,
        /*max_order_notional_usd=*/3000.0,
        ai_trade::RiskThresholds{},
        dual_strategy,
        /*min_rebalance_notional_usd=*/0.0,
        regime_config);
    trend_only_system.EnableEvolution(true);
    defensive_only_system.EnableEvolution(true);
    std::string error;
    if (!trend_only_system.SetEvolutionWeights(1.0, 0.0, &error) ||
        !defensive_only_system.SetEvolutionWeights(0.0, 1.0, &error)) {
      std::cerr << "双分支权重设置失败: " << error << "\n";
      return 1;
    }

    for (int i = 0; i < 12; ++i) {
      const ai_trade::MarketEvent warmup{1 + i, "BTCUSDT", 100.0, 100.0};
      (void)trend_only_system.Evaluate(warmup, true);
      (void)defensive_only_system.Evaluate(warmup, true);
    }

    const ai_trade::MarketEvent breakout{20, "BTCUSDT", 101.0, 101.0};
    const auto trend_decision = trend_only_system.Evaluate(breakout, true);
    const auto defensive_decision = defensive_only_system.Evaluate(breakout, true);
    if (!NearlyEqual(trend_decision.target.target_notional_usd,
                     trend_decision.base_signal.trend_notional_usd,
                     1e-6) ||
        !NearlyEqual(defensive_decision.target.target_notional_usd,
                     defensive_decision.base_signal.defensive_notional_usd,
                     1e-6)) {
      std::cerr << "双分支混合权重未正确映射到目标名义值\n";
      return 1;
    }
    if (std::fabs(trend_decision.base_signal.trend_notional_usd) <= 1e-6 ||
        std::fabs(defensive_decision.base_signal.defensive_notional_usd) <= 1e-6) {
      std::cerr << "双分支分量预期应均有有效信号\n";
      return 1;
    }
    if (trend_decision.target.target_notional_usd *
            defensive_decision.target.target_notional_usd >=
        0.0) {
      std::cerr << "趋势与防御分量预期在该场景下方向相反\n";
      return 1;
    }

    ai_trade::TradeSystem blend_system(
        /*risk_cap_usd=*/3000.0,
        /*max_order_notional_usd=*/3000.0,
        ai_trade::RiskThresholds{},
        dual_strategy,
        /*min_rebalance_notional_usd=*/0.0,
        regime_config);
    blend_system.EnableEvolution(true);
    if (!blend_system.SetEvolutionWeights(0.25, 0.75, &error)) {
      std::cerr << "双分支混合权重设置失败: " << error << "\n";
      return 1;
    }
    for (int i = 0; i < 12; ++i) {
      const ai_trade::MarketEvent warmup{40 + i, "BTCUSDT", 100.0, 100.0};
      (void)blend_system.Evaluate(warmup, true);
    }
    const auto blended = blend_system.Evaluate(
        ai_trade::MarketEvent{60, "BTCUSDT", 101.0, 101.0}, true);
    const double expected_blended =
        trend_decision.base_signal.trend_notional_usd * 0.25 +
        defensive_decision.base_signal.defensive_notional_usd * 0.75;
    if (!NearlyEqual(blended.base_signal.suggested_notional_usd,
                     expected_blended,
                     1e-6) ||
        !NearlyEqual(blended.target.target_notional_usd, expected_blended, 1e-6)) {
      std::cerr << "双分支加权混合结果不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：正收益窗口应提高 trend 权重（受 max_weight_step 约束）。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 2;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 1.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    if (controller.OnTick(1, 10010.0).has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(2, 10020.0);
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->reason_code != "EVOLUTION_WEIGHT_INCREASE_TREND" ||
        !NearlyEqual(action->trend_weight_after, 0.55, 1e-9) ||
        !NearlyEqual(action->defensive_weight_after, 0.45, 1e-9)) {
      std::cerr << "自进化正收益更新行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：窗口盈亏绝对值不足时应显式跳过更新。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 2;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 2.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    const auto action = controller.OnTick(2, 10001.0);
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kSkipped ||
        action->reason_code != "EVOLUTION_WINDOW_PNL_TOO_SMALL" ||
        !NearlyEqual(action->trend_weight_after, 0.50, 1e-9) ||
        !NearlyEqual(action->defensive_weight_after, 0.50, 1e-9)) {
      std::cerr << "自进化小盈亏跳过行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：方向一致性门控启用后，首个候选方向窗口仅记账不更新。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 2;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 1.0;
    config.min_consecutive_direction_windows = 2;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    if (controller.OnTick(1, 10010.0).has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto pending = controller.OnTick(2, 10020.0);
    if (!pending.has_value() ||
        pending->type != ai_trade::SelfEvolutionActionType::kSkipped ||
        pending->reason_code != "EVOLUTION_DIRECTION_CONSISTENCY_PENDING" ||
        pending->direction_consistency_required != 2 ||
        pending->direction_consistency_streak != 1 ||
        pending->direction_consistency_direction != 1 ||
        !NearlyEqual(pending->trend_weight_after, 0.50, 1e-9)) {
      std::cerr << "自进化方向一致性待确认行为不符合预期\n";
      return 1;
    }
    if (controller.OnTick(3, 10030.0).has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto updated = controller.OnTick(4, 10040.0);
    if (!updated.has_value() ||
        updated->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        updated->reason_code != "EVOLUTION_WEIGHT_INCREASE_TREND" ||
        updated->direction_consistency_required != 2 ||
        updated->direction_consistency_streak < 2 ||
        updated->direction_consistency_direction != 1 ||
        !NearlyEqual(updated->trend_weight_after, 0.55, 1e-9)) {
      std::cerr << "自进化方向一致性二次确认更新行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：bucket 样本 tick 数不足时应跳过更新。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 2;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 1.0;
    config.min_bucket_ticks_for_update = 3;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }

    if (controller.OnTick(1, 10010.0, ai_trade::RegimeBucket::kTrend).has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        2, 10020.0, ai_trade::RegimeBucket::kTrend);
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kSkipped ||
        action->reason_code != "EVOLUTION_BUCKET_TICKS_INSUFFICIENT") {
      std::cerr << "自进化 bucket 样本门槛行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：连续退化窗口触发回滚，并进入冷却期。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 2;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.rollback_degrade_windows = 1;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 3;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    // 第一个窗口正收益，权重上调到 0.55，回滚锚点为 0.50。
    const auto up = controller.OnTick(2, 10020.0);
    if (!up.has_value() ||
        up->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        !NearlyEqual(up->trend_weight_after, 0.55, 1e-9)) {
      std::cerr << "自进化首个更新结果不符合预期\n";
      return 1;
    }

    // 第二个窗口退化，触发回滚到锚点 0.50。
    const auto rollback = controller.OnTick(4, 10010.0);
    if (!rollback.has_value() ||
        rollback->type != ai_trade::SelfEvolutionActionType::kRolledBack ||
        rollback->reason_code != "EVOLUTION_ROLLBACK_TRIGGERED" ||
        !NearlyEqual(rollback->trend_weight_after, 0.50, 1e-9) ||
        rollback->cooldown_remaining_ticks != 3) {
      std::cerr << "自进化回滚行为不符合预期\n";
      return 1;
    }

    // 冷却期内到达评估点时应跳过更新。
    const auto cooldown = controller.OnTick(6, 10030.0);
    if (!cooldown.has_value() ||
        cooldown->type != ai_trade::SelfEvolutionActionType::kSkipped ||
        cooldown->reason_code != "EVOLUTION_COOLDOWN_ACTIVE") {
      std::cerr << "自进化冷却期行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：窗口按 bucket 归因，评估应选择样本 tick 数最多的 bucket。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 3;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 1.0;
    config.min_bucket_ticks_for_update = 2;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }

    // trend 桶 2 tick: +30；range 桶 1 tick: -15 => 应选择 trend 桶评估。
    if (controller.OnTick(1, 10010.0, ai_trade::RegimeBucket::kTrend).has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    if (controller.OnTick(2, 10030.0, ai_trade::RegimeBucket::kTrend).has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        3, 10015.0, ai_trade::RegimeBucket::kRange);
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->regime_bucket != ai_trade::RegimeBucket::kTrend ||
        action->window_bucket_ticks != 2 ||
        !NearlyEqual(action->window_pnl_usd, 30.0, 1e-9)) {
      std::cerr << "自进化 bucket 归因行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：风险调整目标函数可在正收益下因高回撤惩罚触发降权。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 2;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 1.0;
    config.min_bucket_ticks_for_update = 1;
    config.objective_alpha_pnl = 1.0;
    config.objective_beta_drawdown = 10.0;
    config.objective_gamma_notional_churn = 0.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }

    if (controller
            .OnTick(1,
                    10010.0,
                    ai_trade::RegimeBucket::kTrend,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/100.0)
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        2,
        10020.0,
        ai_trade::RegimeBucket::kTrend,
        /*drawdown_pct=*/0.02,
        /*account_notional_usd=*/100.0);
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->reason_code != "EVOLUTION_WEIGHT_DECREASE_TREND" ||
        !NearlyEqual(action->trend_weight_after, 0.45, 1e-9) ||
        action->window_objective_score >= 0.0) {
      std::cerr << "风险调整目标函数行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：启用 virtual PnL 后，即使 realized_net 不变也可产生学习动作。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 2;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 1.0;
    config.use_virtual_pnl = true;
    config.virtual_cost_bps = 0.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    if (controller
            .OnTick(1,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/1000.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/100.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        2,
        10000.0,
        ai_trade::RegimeBucket::kRange,
        /*drawdown_pct=*/0.0,
        /*account_notional_usd=*/0.0,
        /*trend_signal_notional_usd=*/1000.0,
        /*defensive_signal_notional_usd=*/0.0,
        /*mark_price_usd=*/101.0,
        /*signal_symbol=*/"BTCUSDT");
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->reason_code != "EVOLUTION_WEIGHT_INCREASE_TREND" ||
        !action->used_virtual_pnl ||
        !NearlyEqual(action->window_realized_pnl_usd, 0.0, 1e-9) ||
        action->window_virtual_pnl_usd <= 0.0 ||
        !NearlyEqual(action->trend_weight_after, 0.55, 1e-9)) {
      std::cerr << "virtual PnL 学习行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：virtual PnL 应按 symbol 独立累计，避免跨币种价格串扰。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 3;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 1.0;
    config.use_virtual_pnl = true;
    config.virtual_cost_bps = 0.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    if (controller
            .OnTick(1,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/1000.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/100.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    if (controller
            .OnTick(2,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/1000.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/200.0,
                    /*signal_symbol=*/"ETHUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        3,
        10000.0,
        ai_trade::RegimeBucket::kRange,
        /*drawdown_pct=*/0.0,
        /*account_notional_usd=*/0.0,
        /*trend_signal_notional_usd=*/1000.0,
        /*defensive_signal_notional_usd=*/0.0,
        /*mark_price_usd=*/101.0,
        /*signal_symbol=*/"BTCUSDT");
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->reason_code != "EVOLUTION_WEIGHT_INCREASE_TREND" ||
        !action->used_virtual_pnl ||
        action->window_virtual_pnl_usd <= 0.0) {
      std::cerr << "跨币种 virtual PnL 串扰防护行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：反事实搜索应选择虚拟PnL更优的权重，而非固定步长梯度。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 2;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 0.5;
    config.use_virtual_pnl = true;
    config.use_counterfactual_search = true;
    config.counterfactual_min_improvement_usd = 0.1;
    config.virtual_cost_bps = 0.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    if (controller
            .OnTick(1,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/1000.0,
                    /*defensive_signal_notional_usd=*/-1000.0,
                    /*mark_price_usd=*/100.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        2,
        10000.0,
        ai_trade::RegimeBucket::kRange,
        /*drawdown_pct=*/0.0,
        /*account_notional_usd=*/0.0,
        /*trend_signal_notional_usd=*/1000.0,
        /*defensive_signal_notional_usd=*/-1000.0,
        /*mark_price_usd=*/99.0,
        /*signal_symbol=*/"BTCUSDT");
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->reason_code != "EVOLUTION_COUNTERFACTUAL_DECREASE_TREND" ||
        !action->used_counterfactual_search ||
        !NearlyEqual(action->trend_weight_after, 0.40, 1e-9) ||
        !NearlyEqual(action->counterfactual_best_trend_weight, 0.40, 1e-9) ||
        action->counterfactual_best_virtual_pnl_usd <=
            action->window_virtual_pnl_usd) {
      std::cerr << "反事实搜索调权行为不符合预期: has="
                << (action.has_value() ? "true" : "false");
      if (action.has_value()) {
        std::cerr << ", type=" << static_cast<int>(action->type)
                  << ", reason=" << action->reason_code
                  << ", used_counterfactual_search="
                  << (action->used_counterfactual_search ? "true" : "false")
                  << ", trend_after=" << action->trend_weight_after
                  << ", best_trend=" << action->counterfactual_best_trend_weight
                  << ", best_virtual_pnl="
                  << action->counterfactual_best_virtual_pnl_usd
                  << ", current_virtual_pnl=" << action->window_virtual_pnl_usd;
      }
      std::cerr << "\n";
      return 1;
    }
  }

  {
    // 自进化控制器：启用因子 IC 自适应后，应按 IC 方向调整权重。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 3;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 0.1;
    config.use_virtual_pnl = true;
    config.use_counterfactual_search = false;
    config.enable_factor_ic_adaptive_weights = true;
    config.factor_ic_min_samples = 2;
    config.factor_ic_min_abs = 0.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    if (controller
            .OnTick(1,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/1000.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/100.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    if (controller
            .OnTick(2,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/500.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/101.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        3,
        10000.0,
        ai_trade::RegimeBucket::kRange,
        /*drawdown_pct=*/0.0,
        /*account_notional_usd=*/0.0,
        /*trend_signal_notional_usd=*/400.0,
        /*defensive_signal_notional_usd=*/0.0,
        /*mark_price_usd=*/101.101,
        /*signal_symbol=*/"BTCUSDT");
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->reason_code != "EVOLUTION_FACTOR_IC_INCREASE_TREND" ||
        !action->used_factor_ic_adaptive_weighting ||
        action->factor_ic_samples < 2 ||
        action->trend_factor_ic <= 0.0 ||
        !NearlyEqual(action->trend_weight_after, 0.55, 1e-9)) {
      std::cerr << "因子 IC 自适应调权行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：trend 因子 IC 为负时，应触发 trend 降权。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 3;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 0.1;
    config.use_virtual_pnl = true;
    config.use_counterfactual_search = false;
    config.enable_factor_ic_adaptive_weights = true;
    config.factor_ic_min_samples = 2;
    config.factor_ic_min_abs = 0.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    if (controller
            .OnTick(1,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/1000.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/100.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    if (controller
            .OnTick(2,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/600.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/95.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        3,
        10000.0,
        ai_trade::RegimeBucket::kRange,
        /*drawdown_pct=*/0.0,
        /*account_notional_usd=*/0.0,
        /*trend_signal_notional_usd=*/500.0,
        /*defensive_signal_notional_usd=*/0.0,
        /*mark_price_usd=*/94.05,
        /*signal_symbol=*/"BTCUSDT");
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->reason_code != "EVOLUTION_FACTOR_IC_DECREASE_TREND" ||
        !action->used_factor_ic_adaptive_weighting ||
        action->trend_factor_ic >= 0.0 ||
        !(action->trend_weight_after < action->trend_weight_before)) {
      std::cerr << "负相关 IC 下趋势降权行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：反事实模式下若无有效增益，在禁用 fallback 时应直接跳过更新。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 3;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 0.1;
    config.use_virtual_pnl = true;
    config.use_counterfactual_search = true;
    config.counterfactual_fallback_to_factor_ic = false;
    config.counterfactual_min_improvement_usd = 1000.0;
    config.enable_factor_ic_adaptive_weights = true;
    config.factor_ic_min_samples = 2;
    config.factor_ic_min_abs = 0.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    if (controller
            .OnTick(1,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/1000.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/100.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    if (controller
            .OnTick(2,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/500.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/101.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        3,
        10000.0,
        ai_trade::RegimeBucket::kRange,
        /*drawdown_pct=*/0.0,
        /*account_notional_usd=*/0.0,
        /*trend_signal_notional_usd=*/400.0,
        /*defensive_signal_notional_usd=*/0.0,
        /*mark_price_usd=*/101.101,
        /*signal_symbol=*/"BTCUSDT");
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kSkipped ||
        action->reason_code !=
            "EVOLUTION_COUNTERFACTUAL_IMPROVEMENT_TOO_SMALL" ||
        !action->used_counterfactual_search ||
        !action->used_factor_ic_adaptive_weighting ||
        action->factor_ic_samples < 2 ||
        action->trend_factor_ic <= 0.0 ||
        !NearlyEqual(action->trend_weight_after, 0.5, 1e-9)) {
      std::cerr << "反事实严格模式下的跳过行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：反事实无提升时，启用 fallback 可回退到 factor-IC 更新。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 3;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 0.1;
    config.use_virtual_pnl = true;
    config.use_counterfactual_search = true;
    config.counterfactual_fallback_to_factor_ic = true;
    config.counterfactual_min_improvement_usd = 1000.0;
    config.enable_factor_ic_adaptive_weights = true;
    config.factor_ic_min_samples = 2;
    config.factor_ic_min_abs = 0.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    controller.OnTick(1,
                      10000.0,
                      ai_trade::RegimeBucket::kRange,
                      /*drawdown_pct=*/0.0,
                      /*account_notional_usd=*/0.0,
                      /*trend_signal_notional_usd=*/1000.0,
                      /*defensive_signal_notional_usd=*/0.0,
                      /*mark_price_usd=*/100.0,
                      /*signal_symbol=*/"BTCUSDT");
    controller.OnTick(2,
                      10000.0,
                      ai_trade::RegimeBucket::kRange,
                      /*drawdown_pct=*/0.0,
                      /*account_notional_usd=*/0.0,
                      /*trend_signal_notional_usd=*/500.0,
                      /*defensive_signal_notional_usd=*/0.0,
                      /*mark_price_usd=*/101.0,
                      /*signal_symbol=*/"BTCUSDT");
    const auto action = controller.OnTick(
        3,
        10000.0,
        ai_trade::RegimeBucket::kRange,
        /*drawdown_pct=*/0.0,
        /*account_notional_usd=*/0.0,
        /*trend_signal_notional_usd=*/400.0,
        /*defensive_signal_notional_usd=*/0.0,
        /*mark_price_usd=*/101.101,
        /*signal_symbol=*/"BTCUSDT");
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->reason_code != "EVOLUTION_FACTOR_IC_INCREASE_TREND" ||
        !action->counterfactual_fallback_to_factor_ic_enabled ||
        !action->counterfactual_fallback_to_factor_ic_used ||
        !NearlyEqual(action->trend_weight_after, 0.55, 1e-9)) {
      std::cerr << "反事实 fallback 到 factor-IC 的更新行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：反事实 fill 不足时，启用 fallback 仍应走 factor-IC 更新。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 3;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 0.1;
    config.use_virtual_pnl = true;
    config.use_counterfactual_search = true;
    config.counterfactual_fallback_to_factor_ic = true;
    config.counterfactual_min_fill_count_for_update = 5;
    config.enable_factor_ic_adaptive_weights = true;
    config.factor_ic_min_samples = 2;
    config.factor_ic_min_abs = 0.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    controller.OnTick(1,
                      10000.0,
                      ai_trade::RegimeBucket::kRange,
                      /*drawdown_pct=*/0.0,
                      /*account_notional_usd=*/0.0,
                      /*trend_signal_notional_usd=*/1000.0,
                      /*defensive_signal_notional_usd=*/0.0,
                      /*mark_price_usd=*/100.0,
                      /*signal_symbol=*/"BTCUSDT");
    controller.OnTick(2,
                      10000.0,
                      ai_trade::RegimeBucket::kRange,
                      /*drawdown_pct=*/0.0,
                      /*account_notional_usd=*/0.0,
                      /*trend_signal_notional_usd=*/500.0,
                      /*defensive_signal_notional_usd=*/0.0,
                      /*mark_price_usd=*/101.0,
                      /*signal_symbol=*/"BTCUSDT");
    const auto action = controller.OnTick(
        3,
        10000.0,
        ai_trade::RegimeBucket::kRange,
        /*drawdown_pct=*/0.0,
        /*account_notional_usd=*/0.0,
        /*trend_signal_notional_usd=*/400.0,
        /*defensive_signal_notional_usd=*/0.0,
        /*mark_price_usd=*/101.101,
        /*signal_symbol=*/"BTCUSDT");
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kUpdated ||
        action->reason_code != "EVOLUTION_FACTOR_IC_INCREASE_TREND" ||
        !action->counterfactual_fallback_to_factor_ic_enabled ||
        !action->counterfactual_fallback_to_factor_ic_used ||
        !NearlyEqual(action->trend_weight_after, 0.55, 1e-9)) {
      std::cerr << "低 fill 回退到 factor-IC 的更新行为不符合预期\n";
      return 1;
    }
  }

  {
    // 自进化控制器：可学习性门控在低 t-stat 窗口应冻结学习。
    ai_trade::SelfEvolutionConfig config;
    config.enabled = true;
    config.update_interval_ticks = 3;
    config.min_update_interval_ticks = 0;
    config.max_single_strategy_weight = 0.60;
    config.max_weight_step = 0.05;
    config.min_abs_window_pnl_usd = 0.0;
    config.use_virtual_pnl = true;
    config.enable_learnability_gate = true;
    config.learnability_min_samples = 2;
    config.learnability_min_t_stat_abs = 2.0;
    config.rollback_degrade_windows = 2;
    config.rollback_degrade_threshold_score = 0.0;
    config.rollback_cooldown_ticks = 5;

    ai_trade::SelfEvolutionController controller(config);
    std::string error;
    if (!controller.Initialize(/*current_tick=*/0,
                               /*initial_equity_usd=*/10000.0,
                               {0.50, 0.50},
                               &error,
                               /*initial_realized_net_pnl_usd=*/10000.0)) {
      std::cerr << "自进化控制器初始化失败: " << error << "\n";
      return 1;
    }
    if (controller
            .OnTick(1,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/1000.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/100.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    if (controller
            .OnTick(2,
                    10000.0,
                    ai_trade::RegimeBucket::kRange,
                    /*drawdown_pct=*/0.0,
                    /*account_notional_usd=*/0.0,
                    /*trend_signal_notional_usd=*/1000.0,
                    /*defensive_signal_notional_usd=*/0.0,
                    /*mark_price_usd=*/101.0,
                    /*signal_symbol=*/"BTCUSDT")
            .has_value()) {
      std::cerr << "未到更新周期前不应返回自进化动作\n";
      return 1;
    }
    const auto action = controller.OnTick(
        3,
        10000.0,
        ai_trade::RegimeBucket::kRange,
        /*drawdown_pct=*/0.0,
        /*account_notional_usd=*/0.0,
        /*trend_signal_notional_usd=*/1000.0,
        /*defensive_signal_notional_usd=*/0.0,
        /*mark_price_usd=*/100.0,
        /*signal_symbol=*/"BTCUSDT");
    if (!action.has_value() ||
        action->type != ai_trade::SelfEvolutionActionType::kSkipped ||
        action->reason_code != "EVOLUTION_LEARNABILITY_TSTAT_TOO_LOW" ||
        !action->learnability_gate_enabled ||
        action->learnability_gate_passed ||
        action->learnability_samples < 2 ||
        std::fabs(action->learnability_t_stat) >= 2.0) {
      std::cerr << "可学习性门控行为不符合预期\n";
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
    const auto& first_stats = throttle.total_stats();
    if (first_stats.checks != 1 || first_stats.allowed != 1 ||
        first_stats.rejected != 0) {
      std::cerr << "首次放行后节流统计不符合预期: checks="
                << first_stats.checks << ", allowed=" << first_stats.allowed
                << ", rejected=" << first_stats.rejected << "\n";
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
        << "  max_ticks: 120\n"
        << "  status_log_interval_ticks: 9\n"
        << "  remote_risk_refresh_interval_ticks: 7\n"
        << "  reconcile:\n"
        << "    mismatch_confirmations: 3\n"
        << "    pending_order_stale_ms: 15000\n"
        << "    anomaly_reduce_only_streak: 2\n"
        << "    anomaly_halt_streak: 4\n"
        << "    anomaly_resume_streak: 3\n"
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
        << "  enforce_runtime_actions: true\n"
        << "  fail_to_reduce_only_windows: 2\n"
        << "  fail_to_halt_windows: 4\n"
        << "  reduce_only_cooldown_ticks: 30\n"
        << "  halt_cooldown_ticks: 60\n"
        << "  pass_to_resume_windows: 2\n"
        << "  auto_resume_when_flat: false\n"
        << "  auto_resume_flat_ticks: 6\n"
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
        << "    ws_reconnect_interval_ms: 17000\n"
        << "    execution_poll_limit: 25\n"
        << "risk:\n"
        << "  max_abs_notional_usd: 4321\n"
        << "  max_drawdown:\n"
        << "    degraded_threshold: 0.05\n"
        << "    degraded_recover_threshold: 0.04\n"
        << "    cooldown_threshold: 0.10\n"
        << "    cooldown_recover_threshold: 0.08\n"
        << "    fuse_threshold: 0.18\n"
        << "    fuse_recover_threshold: 0.15\n"
        << "execution:\n"
        << "  max_order_notional: 876\n"
        << "  min_rebalance_notional_usd: 45\n"
        << "  min_order_interval_ms: 2500\n"
        << "  reverse_signal_cooldown_ticks: 5\n"
        << "  required_edge_cap_bps: 8.5\n"
        << "  entry_gate_near_miss_tolerance_bps: 0.2\n"
        << "  adaptive_fee_gate_enabled: true\n"
        << "  adaptive_fee_gate_min_samples: 90\n"
        << "  adaptive_fee_gate_trigger_ratio: 0.8\n"
        << "  adaptive_fee_gate_max_relax_bps: 2.2\n"
        << "  maker_fallback_to_market: false\n"
        << "  maker_edge_relax_bps: 0.9\n"
        << "  cost_filter_cooldown_trigger_count: 6\n"
        << "  cost_filter_cooldown_ticks: 120\n"
        << "  quality_guard_enabled: true\n"
        << "  quality_guard_min_fills: 10\n"
        << "  quality_guard_bad_streak_to_trigger: 2\n"
        << "  quality_guard_good_streak_to_release: 1\n"
        << "  quality_guard_min_realized_net_per_fill_usd: -0.6\n"
        << "  quality_guard_max_fee_bps_per_fill: 7.5\n"
        << "  quality_guard_required_edge_penalty_bps: 1.2\n"
        << "  dynamic_edge_enabled: true\n"
        << "  dynamic_edge_regime_trend_relax_bps: 0.7\n"
        << "  dynamic_edge_regime_range_penalty_bps: 0.4\n"
        << "  dynamic_edge_regime_extreme_penalty_bps: 1.1\n"
        << "  dynamic_edge_volatility_relax_bps: 0.8\n"
        << "  dynamic_edge_volatility_penalty_bps: 1.3\n"
        << "  dynamic_edge_liquidity_maker_ratio_threshold: 0.6\n"
        << "  dynamic_edge_liquidity_unknown_ratio_threshold: 0.3\n"
        << "  dynamic_edge_liquidity_relax_bps: 0.5\n"
        << "  dynamic_edge_liquidity_penalty_bps: 1.4\n"
        << "strategy:\n"
        << "  signal_notional_usd: 1500\n"
        << "  signal_deadband_abs: 0.3\n"
        << "  min_hold_ticks: 4\n"
        << "  vol_target_pct: 0.35\n"
        << "  vol_target_max_leverage: 2.5\n"
        << "  vol_target_low_vol_leverage_cap_enabled: true\n"
        << "  vol_target_low_vol_annual_threshold: 0.12\n"
        << "  vol_target_low_vol_max_leverage: 1.3\n"
        << "  defensive_notional_ratio: 0.4\n"
        << "  defensive_entry_score: 1.1\n"
        << "  defensive_trend_scale: 0.3\n"
        << "  defensive_range_scale: 0.9\n"
        << "  defensive_extreme_scale: 0.5\n"
        << "integrator:\n"
        << "  enabled: true\n"
        << "  model_type: \"catboost\"\n"
        << "  mode: \"canary\"\n"
        << "  canary_notional_ratio: 0.35\n"
        << "  canary_min_notional_usd: 50\n"
        << "  canary_confidence_threshold: 0.62\n"
        << "  canary_allow_countertrend: true\n"
        << "  active_confidence_threshold: 0.57\n"
        << "  shadow:\n"
        << "    enabled: true\n"
        << "    log_model_score: false\n"
        << "    model_report_path: \"./data/research/integrator_report.json\"\n"
        << "    model_path: \"./data/models/integrator_latest.cbm\"\n"
        << "    active_meta_path: \"./data/models/integrator_active.json\"\n"
        << "    require_model_file: true\n"
        << "    require_active_meta: true\n"
        << "    require_gate_pass: true\n"
        << "    min_auc_mean: 0.53\n"
        << "    min_delta_auc_vs_baseline: 0.02\n"
        << "    min_split_trained_count: 3\n"
        << "    min_split_trained_ratio: 0.70\n"
        << "    score_gain: 1.2\n"
        << "self_evolution:\n"
        << "  enabled: true\n"
        << "  update_interval_ticks: 88\n"
        << "  min_update_interval_ticks: 40\n"
        << "  min_abs_window_pnl_usd: 2.5\n"
        << "  min_bucket_ticks_for_update: 9\n"
        << "  min_consecutive_direction_windows: 3\n"
        << "  use_virtual_pnl: true\n"
        << "  use_counterfactual_search: true\n"
        << "  counterfactual_fallback_to_factor_ic: false\n"
        << "  counterfactual_min_improvement_usd: 1.2\n"
        << "  counterfactual_improvement_decay_per_filtered_signal_usd: 0.05\n"
        << "  counterfactual_min_fill_count_for_update: 6\n"
        << "  counterfactual_min_t_stat_samples_for_update: 160\n"
        << "  counterfactual_min_t_stat_abs_for_update: 1.1\n"
        << "  virtual_cost_bps: 6.5\n"
        << "  enable_factor_ic_adaptive_weights: true\n"
        << "  factor_ic_min_samples: 180\n"
        << "  factor_ic_min_abs: 0.015\n"
        << "  enable_learnability_gate: true\n"
        << "  learnability_min_samples: 200\n"
        << "  learnability_min_t_stat_abs: 1.8\n"
        << "  objective_alpha_pnl: 1.5\n"
        << "  objective_beta_drawdown: 0.7\n"
        << "  objective_gamma_notional_churn: 0.02\n"
        << "  max_single_strategy_weight: 0.7\n"
        << "  max_weight_step: 0.03\n"
        << "  rollback_degrade_windows: 3\n"
        << "  rollback_degrade_threshold_score: -5\n"
        << "  rollback_cooldown_ticks: 66\n"
        << "  initial_trend_weight: 0.55\n"
        << "  initial_defensive_weight: 0.45\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (!ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "配置加载预期成功，错误: " << error << "\n";
      return 1;
    }
    if (!NearlyEqual(config.risk_max_abs_notional_usd, 4321.0) ||
        !NearlyEqual(config.execution_max_order_notional, 876.0) ||
        !NearlyEqual(config.execution_min_rebalance_notional_usd, 45.0) ||
        config.execution_min_order_interval_ms != 2500 ||
        config.execution_reverse_signal_cooldown_ticks != 5 ||
        !NearlyEqual(config.execution_required_edge_cap_bps, 8.5) ||
        !NearlyEqual(config.execution_entry_gate_near_miss_tolerance_bps, 0.2) ||
        config.execution_adaptive_fee_gate_enabled != true ||
        config.execution_adaptive_fee_gate_min_samples != 90 ||
        !NearlyEqual(config.execution_adaptive_fee_gate_trigger_ratio, 0.8) ||
        !NearlyEqual(config.execution_adaptive_fee_gate_max_relax_bps, 2.2) ||
        config.execution_maker_fallback_to_market != false ||
        !NearlyEqual(config.execution_maker_edge_relax_bps, 0.9) ||
        config.execution_cost_filter_cooldown_trigger_count != 6 ||
        config.execution_cost_filter_cooldown_ticks != 120 ||
        config.execution_quality_guard_enabled != true ||
        config.execution_quality_guard_min_fills != 10 ||
        config.execution_quality_guard_bad_streak_to_trigger != 2 ||
        config.execution_quality_guard_good_streak_to_release != 1 ||
        !NearlyEqual(
            config.execution_quality_guard_min_realized_net_per_fill_usd,
            -0.6) ||
        !NearlyEqual(config.execution_quality_guard_max_fee_bps_per_fill, 7.5) ||
        !NearlyEqual(config.execution_quality_guard_required_edge_penalty_bps,
                     1.2) ||
        config.execution_dynamic_edge_enabled != true ||
        !NearlyEqual(config.execution_dynamic_edge_regime_trend_relax_bps, 0.7) ||
        !NearlyEqual(config.execution_dynamic_edge_regime_range_penalty_bps, 0.4) ||
        !NearlyEqual(config.execution_dynamic_edge_regime_extreme_penalty_bps, 1.1) ||
        !NearlyEqual(config.execution_dynamic_edge_volatility_relax_bps, 0.8) ||
        !NearlyEqual(config.execution_dynamic_edge_volatility_penalty_bps, 1.3) ||
        !NearlyEqual(config.execution_dynamic_edge_liquidity_maker_ratio_threshold, 0.6) ||
        !NearlyEqual(config.execution_dynamic_edge_liquidity_unknown_ratio_threshold, 0.3) ||
        !NearlyEqual(config.execution_dynamic_edge_liquidity_relax_bps, 0.5) ||
        !NearlyEqual(config.execution_dynamic_edge_liquidity_penalty_bps, 1.4) ||
        !NearlyEqual(config.strategy_signal_notional_usd, 1500.0) ||
        !NearlyEqual(config.strategy_signal_deadband_abs, 0.3) ||
        config.strategy_min_hold_ticks != 4 ||
        !NearlyEqual(config.vol_target_pct, 0.35) ||
        !NearlyEqual(config.strategy_vol_target_max_leverage, 2.5) ||
        config.strategy_vol_target_low_vol_leverage_cap_enabled != true ||
        !NearlyEqual(config.strategy_vol_target_low_vol_annual_threshold, 0.12) ||
        !NearlyEqual(config.strategy_vol_target_low_vol_max_leverage, 1.3) ||
        !NearlyEqual(config.strategy_defensive_notional_ratio, 0.4) ||
        !NearlyEqual(config.strategy_defensive_entry_score, 1.1) ||
        !NearlyEqual(config.strategy_defensive_trend_scale, 0.3) ||
        !NearlyEqual(config.strategy_defensive_range_scale, 0.9) ||
        !NearlyEqual(config.strategy_defensive_extreme_scale, 0.5) ||
        config.system_max_ticks != 120 ||
        config.system_status_log_interval_ticks != 9 ||
        config.system_remote_risk_refresh_interval_ticks != 7 ||
        config.reconcile.mismatch_confirmations != 3 ||
        config.reconcile.pending_order_stale_ms != 15000 ||
        config.reconcile.anomaly_reduce_only_streak != 2 ||
        config.reconcile.anomaly_halt_streak != 4 ||
        config.reconcile.anomaly_resume_streak != 3 ||
        config.exchange != "mock" ||
        config.mode != "paper" ||
        config.primary_symbol != "ETHUSDT" ||
        config.data_path != "./tmp_data" ||
        config.gate.min_effective_signals_per_window != 7 ||
        config.gate.min_fills_per_window != 3 ||
        config.gate.heartbeat_empty_signal_ticks != 9 ||
        config.gate.enforce_runtime_actions != true ||
        config.gate.fail_to_reduce_only_windows != 2 ||
        config.gate.fail_to_halt_windows != 4 ||
        config.gate.reduce_only_cooldown_ticks != 30 ||
        config.gate.halt_cooldown_ticks != 60 ||
        config.gate.pass_to_resume_windows != 2 ||
        config.gate.auto_resume_when_flat != false ||
        config.gate.auto_resume_flat_ticks != 6 ||
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
        config.bybit.ws_reconnect_interval_ms != 17000 ||
        config.bybit.execution_poll_limit != 25 ||
        config.integrator.enabled != true ||
        config.integrator.model_type != "catboost" ||
        config.integrator.mode != ai_trade::IntegratorMode::kCanary ||
        !NearlyEqual(config.integrator.canary_notional_ratio, 0.35) ||
        !NearlyEqual(config.integrator.canary_min_notional_usd, 50.0) ||
        !NearlyEqual(config.integrator.canary_confidence_threshold, 0.62) ||
        config.integrator.canary_allow_countertrend != true ||
        !NearlyEqual(config.integrator.active_confidence_threshold, 0.57) ||
        config.integrator.shadow.enabled != true ||
        config.integrator.shadow.log_model_score != false ||
        config.integrator.shadow.model_report_path !=
            "./data/research/integrator_report.json" ||
        config.integrator.shadow.model_path !=
            "./data/models/integrator_latest.cbm" ||
        config.integrator.shadow.active_meta_path !=
            "./data/models/integrator_active.json" ||
        config.integrator.shadow.require_model_file != true ||
        config.integrator.shadow.require_active_meta != true ||
        config.integrator.shadow.require_gate_pass != true ||
        !NearlyEqual(config.integrator.shadow.min_auc_mean, 0.53) ||
        !NearlyEqual(config.integrator.shadow.min_delta_auc_vs_baseline, 0.02) ||
        config.integrator.shadow.min_split_trained_count != 3 ||
        !NearlyEqual(config.integrator.shadow.min_split_trained_ratio, 0.70) ||
        !NearlyEqual(config.integrator.shadow.score_gain, 1.2) ||
        config.self_evolution.enabled != true ||
        config.self_evolution.update_interval_ticks != 88 ||
        config.self_evolution.min_update_interval_ticks != 40 ||
        !NearlyEqual(config.self_evolution.min_abs_window_pnl_usd, 2.5) ||
        config.self_evolution.min_bucket_ticks_for_update != 9 ||
        config.self_evolution.min_consecutive_direction_windows != 3 ||
        config.self_evolution.use_virtual_pnl != true ||
        config.self_evolution.use_counterfactual_search != true ||
        config.self_evolution.counterfactual_fallback_to_factor_ic != false ||
        !NearlyEqual(config.self_evolution.counterfactual_min_improvement_usd,
                     1.2) ||
        !NearlyEqual(
            config.self_evolution
                .counterfactual_improvement_decay_per_filtered_signal_usd,
            0.05) ||
        config.self_evolution.counterfactual_min_fill_count_for_update != 6 ||
        config.self_evolution.counterfactual_min_t_stat_samples_for_update !=
            160 ||
        !NearlyEqual(
            config.self_evolution.counterfactual_min_t_stat_abs_for_update,
            1.1) ||
        !NearlyEqual(config.self_evolution.virtual_cost_bps, 6.5) ||
        config.self_evolution.enable_factor_ic_adaptive_weights != true ||
        config.self_evolution.factor_ic_min_samples != 180 ||
        !NearlyEqual(config.self_evolution.factor_ic_min_abs, 0.015) ||
        config.self_evolution.enable_learnability_gate != true ||
        config.self_evolution.learnability_min_samples != 200 ||
        !NearlyEqual(config.self_evolution.learnability_min_t_stat_abs, 1.8) ||
        !NearlyEqual(config.self_evolution.objective_alpha_pnl, 1.5) ||
        !NearlyEqual(config.self_evolution.objective_beta_drawdown, 0.7) ||
        !NearlyEqual(config.self_evolution.objective_gamma_notional_churn,
                     0.02) ||
        !NearlyEqual(config.self_evolution.max_single_strategy_weight, 0.7) ||
        !NearlyEqual(config.self_evolution.max_weight_step, 0.03) ||
        config.self_evolution.rollback_degrade_windows != 3 ||
        !NearlyEqual(config.self_evolution.rollback_degrade_threshold_score, -5.0) ||
        config.self_evolution.rollback_cooldown_ticks != 66 ||
        !NearlyEqual(config.self_evolution.initial_trend_weight, 0.55) ||
        !NearlyEqual(config.self_evolution.initial_defensive_weight, 0.45) ||
        !NearlyEqual(config.risk_thresholds.degraded_drawdown, 0.05) ||
        !NearlyEqual(config.risk_thresholds.degraded_recover_drawdown, 0.04) ||
        !NearlyEqual(config.risk_thresholds.cooldown_drawdown, 0.10) ||
        !NearlyEqual(config.risk_thresholds.cooldown_recover_drawdown, 0.08) ||
        !NearlyEqual(config.risk_thresholds.fuse_drawdown, 0.18) ||
        !NearlyEqual(config.risk_thresholds.fuse_recover_drawdown, 0.15)) {
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
        "ai_trade_test_protection_alias_config.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  primary_symbol: BTCUSDT\n"
        << "universe:\n"
        << "  fallback_symbols: [BTCUSDT]\n"
        << "execution:\n"
        << "  protection:\n"
        << "    enabled: true\n"
        << "    require_sl: true\n"
        << "    enable_tp: true\n"
        << "    attach_timeout_ms: 1200\n"
        << "    stop_loss_atr_mult: 0.02\n"
        << "    take_profit_rr: 0.03\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (!ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "兼容保护单别名字段配置应加载成功: " << error << "\n";
      return 1;
    }
    if (!NearlyEqual(config.protection.stop_loss_ratio, 0.02) ||
        !NearlyEqual(config.protection.take_profit_ratio, 0.03)) {
      std::cerr << "保护单别名字段未正确映射到 ratio 参数\n";
      return 1;
    }

    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_protection_ratio.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  primary_symbol: BTCUSDT\n"
        << "universe:\n"
        << "  fallback_symbols: [BTCUSDT]\n"
        << "execution:\n"
        << "  protection:\n"
        << "    enabled: true\n"
        << "    require_sl: true\n"
        << "    attach_timeout_ms: 1500\n"
        << "    stop_loss_ratio: 0.0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 stop_loss_ratio 配置应加载失败\n";
      return 1;
    }
    if (error.find("stop_loss_ratio") == std::string::npos) {
      std::cerr << "非法 stop_loss_ratio 错误信息不符合预期\n";
      return 1;
    }

    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_integrator_config.yaml";
    std::ofstream out(temp_path);
    out << "integrator:\n"
        << "  enabled: true\n"
        << "  mode: \"canary\"\n"
        << "  canary_notional_ratio: 1.2\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 Integrator 配置应加载失败\n";
      return 1;
    }
    if (error.find("integrator.canary_notional_ratio") == std::string::npos) {
      std::cerr << "非法 Integrator 配置错误信息不符合预期\n";
      return 1;
    }

    std::filesystem::remove(temp_path);
  }

  {
    // 兼容旧配置键：rollback_degrade_threshold_pnl -> rollback_degrade_threshold_score
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_self_evolution_threshold_alias.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  objective_alpha_pnl: 1.0\n"
        << "  objective_beta_drawdown: 0.0\n"
        << "  objective_gamma_notional_churn: 0.0\n"
        << "  rollback_degrade_windows: 2\n"
        << "  rollback_degrade_threshold_pnl: -3.5\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (!ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "旧键回滚阈值配置预期成功，错误: " << error << "\n";
      return 1;
    }
    if (!NearlyEqual(config.self_evolution.rollback_degrade_threshold_score,
                     -3.5)) {
      std::cerr << "旧键 rollback_degrade_threshold_pnl 兼容解析失败\n";
      return 1;
    }

    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_remote_risk_refresh_interval.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  remote_risk_refresh_interval_ticks: -1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 remote_risk_refresh_interval_ticks 配置应加载失败\n";
      return 1;
    }
    if (error.find("remote_risk_refresh_interval_ticks") == std::string::npos) {
      std::cerr << "非法 remote_risk_refresh_interval_ticks 错误信息不符合预期\n";
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
        "ai_trade_test_invalid_bybit_ws_reconnect_interval.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  primary_symbol: \"BTCUSDT\"\n"
        << "exchange:\n"
        << "  platform: \"bybit\"\n"
        << "  bybit:\n"
        << "    ws_reconnect_interval_ms: -1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 Bybit ws_reconnect_interval_ms 配置应加载失败\n";
      return 1;
    }
    if (error.find("ws_reconnect_interval_ms") == std::string::npos) {
      std::cerr << "非法 Bybit ws_reconnect_interval_ms 错误信息不符合预期\n";
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
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_required_edge_cap.yaml";
    std::ofstream out(temp_path);
    out << "execution:\n"
        << "  required_edge_cap_bps: -1.0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 execution.required_edge_cap_bps 配置应加载失败\n";
      return 1;
    }
    if (error.find("required_edge_cap_bps") == std::string::npos) {
      std::cerr << "非法 execution.required_edge_cap_bps 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_entry_gate_near_miss_tolerance.yaml";
    std::ofstream out(temp_path);
    out << "execution:\n"
        << "  entry_gate_near_miss_tolerance_bps: -0.1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr
          << "非法 execution.entry_gate_near_miss_tolerance_bps 配置应加载失败\n";
      return 1;
    }
    if (error.find("entry_gate_near_miss_tolerance_bps") == std::string::npos) {
      std::cerr
          << "非法 execution.entry_gate_near_miss_tolerance_bps 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_entry_gate_near_miss_maker_gap.yaml";
    std::ofstream out(temp_path);
    out << "execution:\n"
        << "  entry_gate_near_miss_maker_max_gap_bps: -0.1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr
          << "非法 execution.entry_gate_near_miss_maker_max_gap_bps 配置应加载失败\n";
      return 1;
    }
    if (error.find("entry_gate_near_miss_maker_max_gap_bps") ==
        std::string::npos) {
      std::cerr
          << "非法 execution.entry_gate_near_miss_maker_max_gap_bps 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_unknown_config_key.yaml";
    std::ofstream out(temp_path);
    out << "execution:\n"
        << "  unknown_key_for_test: 1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "未知配置项应加载失败\n";
      return 1;
    }
    if (error.find("未识别配置项") == std::string::npos ||
        error.find("execution.unknown_key_for_test") == std::string::npos) {
      std::cerr << "未知配置项错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_cost_filter_cooldown_pair.yaml";
    std::ofstream out(temp_path);
    out << "execution:\n"
        << "  cost_filter_cooldown_trigger_count: 3\n"
        << "  cost_filter_cooldown_ticks: 0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 execution.cost_filter_cooldown_* 配置应加载失败\n";
      return 1;
    }
    if (error.find("cost_filter_cooldown") == std::string::npos) {
      std::cerr
          << "非法 execution.cost_filter_cooldown_* 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_quality_guard_max_fee.yaml";
    std::ofstream out(temp_path);
    out << "execution:\n"
        << "  quality_guard_max_fee_bps_per_fill: -0.1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 execution.quality_guard_max_fee_bps_per_fill 配置应加载失败\n";
      return 1;
    }
    if (error.find("quality_guard") == std::string::npos) {
      std::cerr << "非法 execution.quality_guard_* 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_reconcile_confirmations.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  reconcile:\n"
        << "    mismatch_confirmations: 0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 reconcile.mismatch_confirmations 配置应加载失败\n";
      return 1;
    }
    if (error.find("mismatch_confirmations") == std::string::npos) {
      std::cerr << "非法 reconcile.mismatch_confirmations 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_reconcile_anomaly_streak.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  reconcile:\n"
        << "    anomaly_reduce_only_streak: -1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 reconcile.anomaly_* 配置应加载失败\n";
      return 1;
    }
    if (error.find("anomaly") == std::string::npos) {
      std::cerr << "非法 reconcile.anomaly_* 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_reconcile_pending_stale_ms.yaml";
    std::ofstream out(temp_path);
    out << "system:\n"
        << "  reconcile:\n"
        << "    pending_order_stale_ms: 0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 reconcile.pending_order_stale_ms 配置应加载失败\n";
      return 1;
    }
    if (error.find("pending_order_stale_ms") == std::string::npos) {
      std::cerr << "非法 reconcile.pending_order_stale_ms 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_gate_runtime_values.yaml";
    std::ofstream out(temp_path);
    out << "gate:\n"
        << "  fail_to_reduce_only_windows: -1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 gate 运行时动作配置应加载失败\n";
      return 1;
    }
    if (error.find("运行时动作") == std::string::npos) {
      std::cerr << "非法 gate 运行时动作错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_gate_auto_resume_flat_ticks.yaml";
    std::ofstream out(temp_path);
    out << "gate:\n"
        << "  auto_resume_flat_ticks: -1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 gate.auto_resume_flat_ticks 配置应加载失败\n";
      return 1;
    }
    if (error.find("运行时动作") == std::string::npos) {
      std::cerr << "非法 gate.auto_resume_flat_ticks 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_strategy_execution.yaml";
    std::ofstream out(temp_path);
    out << "execution:\n"
        << "  min_rebalance_notional_usd: -10\n"
        << "strategy:\n"
        << "  min_hold_ticks: -1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 strategy/execution 防抖配置应加载失败\n";
      return 1;
    }
    if (error.find("min_rebalance_notional_usd") == std::string::npos &&
        error.find("strategy.min_hold_ticks") == std::string::npos) {
      std::cerr << "非法 strategy/execution 防抖配置错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_strategy_defensive.yaml";
    std::ofstream out(temp_path);
    out << "strategy:\n"
        << "  defensive_notional_ratio: -0.1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 strategy.defensive_notional_ratio 配置应加载失败\n";
      return 1;
    }
    if (error.find("strategy.defensive_notional_ratio") == std::string::npos) {
      std::cerr << "非法 strategy.defensive_notional_ratio 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_strategy_defensive_entry_score.yaml";
    std::ofstream out(temp_path);
    out << "strategy:\n"
        << "  defensive_entry_score: 0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 strategy.defensive_entry_score 配置应加载失败\n";
      return 1;
    }
    if (error.find("strategy.defensive_entry_score") == std::string::npos) {
      std::cerr << "非法 strategy.defensive_entry_score 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_self_evolution_weights.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  initial_trend_weight: 0.8\n"
        << "  initial_defensive_weight: 0.3\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 self_evolution 初始权重和配置应加载失败\n";
      return 1;
    }
    if (error.find("初始权重和") == std::string::npos) {
      std::cerr << "非法 self_evolution 初始权重和错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_self_evolution_min_abs_window_pnl.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  min_abs_window_pnl_usd: -1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 self_evolution.min_abs_window_pnl_usd 配置应加载失败\n";
      return 1;
    }
    if (error.find("min_abs_window_pnl_usd") == std::string::npos) {
      std::cerr << "非法 self_evolution.min_abs_window_pnl_usd 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_self_evolution_min_bucket_ticks.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  min_bucket_ticks_for_update: -1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr
          << "非法 self_evolution.min_bucket_ticks_for_update 配置应加载失败\n";
      return 1;
    }
    if (error.find("min_bucket_ticks_for_update") == std::string::npos) {
      std::cerr
          << "非法 self_evolution.min_bucket_ticks_for_update 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_self_evolution_min_consecutive_direction_windows.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  min_consecutive_direction_windows: 0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr
          << "非法 self_evolution.min_consecutive_direction_windows 配置应加载失败\n";
      return 1;
    }
    if (error.find("min_consecutive_direction_windows") == std::string::npos) {
      std::cerr
          << "非法 self_evolution.min_consecutive_direction_windows 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_self_evolution_virtual_cost.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  virtual_cost_bps: -0.1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 self_evolution.virtual_cost_bps 配置应加载失败\n";
      return 1;
    }
    if (error.find("virtual_cost_bps") == std::string::npos) {
      std::cerr
          << "非法 self_evolution.virtual_cost_bps 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_self_evolution_factor_ic_min_abs.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  factor_ic_min_abs: -0.01\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 self_evolution.factor_ic_min_abs 配置应加载失败\n";
      return 1;
    }
    if (error.find("factor_ic_min_abs") == std::string::npos) {
      std::cerr
          << "非法 self_evolution.factor_ic_min_abs 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_self_evolution_learnability_tstat.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  learnability_min_t_stat_abs: -0.1\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr
          << "非法 self_evolution.learnability_min_t_stat_abs 配置应加载失败\n";
      return 1;
    }
    if (error.find("learnability_min_t_stat_abs") == std::string::npos) {
      std::cerr
          << "非法 self_evolution.learnability_min_t_stat_abs 错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_self_evolution_objective_zero.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  objective_alpha_pnl: 0\n"
        << "  objective_beta_drawdown: 0\n"
        << "  objective_gamma_notional_churn: 0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr << "非法 self_evolution objective 全零配置应加载失败\n";
      return 1;
    }
    if (error.find("objective") == std::string::npos) {
      std::cerr << "非法 self_evolution objective 全零错误信息不符合预期\n";
      return 1;
    }
    std::filesystem::remove(temp_path);
  }

  {
    const std::filesystem::path temp_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_invalid_self_evolution_enabled_objective_penalty.yaml";
    std::ofstream out(temp_path);
    out << "self_evolution:\n"
        << "  enabled: true\n"
        << "  objective_alpha_pnl: 1.0\n"
        << "  objective_beta_drawdown: 0\n"
        << "  objective_gamma_notional_churn: 0.005\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(temp_path.string(), &config, &error)) {
      std::cerr
          << "self_evolution.enabled=true 且风险惩罚为 0 的配置应加载失败\n";
      return 1;
    }
    if (error.find("objective_beta_drawdown") == std::string::npos) {
      std::cerr << "self_evolution 风险惩罚错误信息不符合预期\n";
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
    ai_trade::RiskThresholds thresholds;
    thresholds.degraded_drawdown = 0.08;
    thresholds.degraded_recover_drawdown = 0.06;
    thresholds.cooldown_drawdown = 0.12;
    thresholds.cooldown_recover_drawdown = 0.10;
    thresholds.fuse_drawdown = 0.20;
    thresholds.fuse_recover_drawdown = 0.16;
    ai_trade::RiskEngine risk(/*max_abs_notional_usd=*/500.0, thresholds);
    const ai_trade::TargetPosition target{"BTCUSDT", 500.0};

    const auto degraded_enter = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.081);
    if (degraded_enter.risk_mode != ai_trade::RiskMode::kDegraded) {
      std::cerr << "风控滞回：degraded 进入失败\n";
      return 1;
    }
    const auto degraded_hold = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.079);
    if (degraded_hold.risk_mode != ai_trade::RiskMode::kDegraded) {
      std::cerr << "风控滞回：degraded 保持失败\n";
      return 1;
    }
    const auto degraded_exit = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.059);
    if (degraded_exit.risk_mode != ai_trade::RiskMode::kNormal) {
      std::cerr << "风控滞回：degraded 恢复失败\n";
      return 1;
    }

    const auto fuse_enter = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.21);
    if (fuse_enter.risk_mode != ai_trade::RiskMode::kFuse) {
      std::cerr << "风控滞回：fuse 进入失败\n";
      return 1;
    }
    const auto fuse_hold = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.17);
    if (fuse_hold.risk_mode != ai_trade::RiskMode::kFuse) {
      std::cerr << "风控滞回：fuse 保持失败\n";
      return 1;
    }
    const auto fuse_release = risk.Apply(target, /*trade_ok=*/true, /*dd=*/0.15);
    if (fuse_release.risk_mode != ai_trade::RiskMode::kCooldown) {
      std::cerr << "风控滞回：fuse 释放到 cooldown 失败\n";
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
    if (!oms.HasPendingNetPositionOrders() ||
        oms.PendingNetPositionOrderCount() != 1) {
      std::cerr << "OMS 在途净仓位订单计数不符合预期\n";
      return 1;
    }
    if (!oms.HasPendingNetPositionOrderForSymbolDirection("BTCUSDT", 1) ||
        oms.HasPendingNetPositionOrderForSymbolDirection("BTCUSDT", -1) ||
        oms.HasPendingNetPositionOrderForSymbolDirection("ETHUSDT", 1)) {
      std::cerr << "OMS 同向在途净仓位判定不符合预期\n";
      return 1;
    }
    if (!oms.HasPendingNetPositionOrderForSymbol("BTCUSDT") ||
        oms.HasPendingNetPositionOrderForSymbol("ETHUSDT")) {
      std::cerr << "OMS 按 symbol 在途净仓位判定不符合预期\n";
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
    if (oms.HasPendingNetPositionOrders() ||
        oms.PendingNetPositionOrderCount() != 0) {
      std::cerr << "OMS 终态后在途净仓位订单应为 0\n";
      return 1;
    }
    if (oms.HasPendingNetPositionOrderForSymbolDirection("BTCUSDT", 1)) {
      std::cerr << "OMS 终态后不应存在同向在途净仓位订单\n";
      return 1;
    }
    if (oms.HasPendingNetPositionOrderForSymbol("BTCUSDT")) {
      std::cerr << "OMS 终态后不应存在同 symbol 在途净仓位订单\n";
      return 1;
    }
  }

  {
    ai_trade::OrderManager oms;
    ai_trade::OrderIntent sl_intent;
    sl_intent.client_order_id = "oms-sl-1";
    sl_intent.symbol = "BTCUSDT";
    sl_intent.direction = -1;
    sl_intent.qty = 1.0;
    sl_intent.price = 90.0;
    sl_intent.reduce_only = true;
    sl_intent.purpose = ai_trade::OrderPurpose::kSl;
    if (!oms.RegisterIntent(sl_intent)) {
      std::cerr << "OMS 注册保护单失败\n";
      return 1;
    }
    oms.MarkSent(sl_intent.client_order_id);
    if (oms.HasPendingNetPositionOrders() ||
        oms.PendingNetPositionOrderCount() != 0) {
      std::cerr << "保护单不应计入净仓位在途订单\n";
      return 1;
    }
  }

  {
    ai_trade::OrderManager oms;
    oms.SeedNetPositionBaseline({
        ai_trade::RemotePositionSnapshot{
            .symbol = "BTCUSDT",
            .qty = -0.02,
            .avg_entry_price = 70000.0,
            .mark_price = 70000.0,
        },
        ai_trade::RemotePositionSnapshot{
            .symbol = "ETHUSDT",
            .qty = 0.30,
            .avg_entry_price = 2000.0,
            .mark_price = 2000.0,
        },
    });
    if (!NearlyEqual(oms.net_filled_qty("BTCUSDT"), -0.02, 1e-9) ||
        !NearlyEqual(oms.net_filled_qty("ETHUSDT"), 0.30, 1e-9) ||
        !NearlyEqual(oms.net_filled_qty(), 0.28, 1e-9)) {
      std::cerr << "OMS 远端基线注入后净仓位不符合预期\n";
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

    const std::vector<ai_trade::RemotePositionSnapshot> remote_positions = {
        ai_trade::RemotePositionSnapshot{
            .symbol = "BTCUSDT",
            .qty = 0.8,
            .avg_entry_price = 100.0,
            .mark_price = 101.0,
        }};
    const double remote_notional =
        reconciler.ComputeRemoteNotionalUsd(remote_positions);
    if (!NearlyEqual(remote_notional, 80.8)) {
      std::cerr << "远端名义值计算不符合预期\n";
      return 1;
    }

    const auto report = reconciler.BuildSymbolDeltaReport(
        account, remote_positions, std::vector<std::string>{"BTCUSDT"}, 0.5);
    if (report.find("BTCUSDT") == std::string::npos ||
        report.find("delta_notional") == std::string::npos) {
      std::cerr << "symbol级对账差异报告不符合预期: " << report << "\n";
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
    config.min_turnover_usd = 100000.0; // 设置成交额门槛
    config.candidate_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT"};
    ai_trade::UniverseSelector selector(config, "BTCUSDT");
    if (selector.active_symbols().empty()) {
      std::cerr << "Universe 初始 active_symbols 不应为空\n";
      return 1;
    }

    const auto first = selector.OnMarket(ai_trade::MarketEvent{
        1, "BTCUSDT", 100.0, 100.0, 2000.0}); // Turnover = 200,000 > 100,000
    if (first.has_value()) {
      std::cerr << "Universe 未到更新间隔不应刷新\n";
      return 1;
    }
    const auto second = selector.OnMarket(ai_trade::MarketEvent{
        2, "ETHUSDT", 2000.0, 2000.0, 10.0}); // Turnover = 20,000 < 100,000
    if (!second.has_value()) {
      std::cerr << "Universe 到更新间隔应刷新\n";
      return 1;
    }
    if (second->active_symbols.empty() ||
        static_cast<int>(second->active_symbols.size()) > config.max_active_symbols) {
      std::cerr << "Universe 刷新后的 active_symbols 数量不符合预期\n";
      return 1;
    }
    // ETHUSDT 成交额不足，应被过滤，仅剩 BTCUSDT (或 fallback)
    if (selector.IsActive("ETHUSDT")) {
      std::cerr << "Universe 应过滤低成交额币对\n";
      return 1;
    }
    if (!selector.IsActive("BTCUSDT")) {
      std::cerr << "Universe 应保留高成交额币对\n";
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

    ai_trade::RemoteAccountBalanceSnapshot balance;
    if (!adapter.GetRemoteAccountBalance(&balance) ||
        !balance.has_equity ||
        !balance.has_wallet_balance ||
        !NearlyEqual(balance.equity_usd, 10000.0)) {
      std::cerr << "mock 远端资金快照读取不符合预期\n";
      return 1;
    }

    std::unordered_set<std::string> open_order_ids;
    if (!adapter.GetRemoteOpenOrderClientIds(&open_order_ids)) {
      std::cerr << "mock 远端活动订单读取失败\n";
      return 1;
    }
    if (!open_order_ids.empty()) {
      std::cerr << "mock 无挂单时活动订单集合应为空\n";
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
    std::unordered_set<std::string> open_order_ids;
    if (!adapter.GetRemoteOpenOrderClientIds(&open_order_ids)) {
      std::cerr << "mock 撤单后活动订单读取失败\n";
      return 1;
    }
    if (!open_order_ids.empty()) {
      std::cerr << "mock 撤单后活动订单集合应为空\n";
      return 1;
    }
    ai_trade::FillEvent fill;
    if (adapter.PollFill(&fill)) {
      std::cerr << "撤单后不应再有成交\n";
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
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[]}})",
            .error = "",
        });
    transport.AddRoute(
        "POST",
        "/v5/order/cancel",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":110001,"retMsg":"order not exists or too late to cancel"})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/order/realtime",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","orderLinkId":"cid-open-1","orderStatus":"New","leavesQty":"1"},{"symbol":"BTCUSDT","orderLinkId":"cid-zero-leaves-1","orderStatus":"New","leavesQty":"0"},{"symbol":"BTCUSDT","orderLinkId":"cid-filled-1","orderStatus":"Filled","leavesQty":"0"},{"symbol":"BTCUSDT","orderLinkId":"cid-cancelled-1","orderStatus":"Cancelled","leavesQty":"0"},{"symbol":"BTCUSDT","orderLinkId":"","orderStatus":"New","leavesQty":"1"}]}})",
            .error = "",
        });

    ai_trade::BybitAdapterOptions options;
    options.mode = "paper";
    options.testnet = true;
    options.symbols = {"BTCUSDT"};
    options.primary_symbol = "BTCUSDT";
    options.public_ws_enabled = false;
    options.private_ws_enabled = false;
    options.http_transport_factory = [transport]() {
      return std::make_unique<MockBybitHttpTransport>(transport);
    };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "bybit adapter 连接失败（撤单幂等测试）\n";
      return 1;
    }
    if (!adapter.CancelOrder("cid-stale-1")) {
      std::cerr << "Bybit 110001 撤单应按幂等成功处理\n";
      return 1;
    }
    std::unordered_set<std::string> open_order_ids;
    if (!adapter.GetRemoteOpenOrderClientIds(&open_order_ids) ||
        open_order_ids.find("cid-open-1") == open_order_ids.end() ||
        open_order_ids.find("cid-zero-leaves-1") != open_order_ids.end() ||
        open_order_ids.find("cid-filled-1") != open_order_ids.end() ||
        open_order_ids.find("cid-cancelled-1") != open_order_ids.end()) {
      std::cerr << "Bybit 活动订单集合读取不符合预期\n";
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
         R"({"topic":"execution","data":[{"execId":"exec-1","orderLinkId":"cid-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.01","isMaker":true},{"execId":"exec-1","orderLinkId":"cid-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.01","isMaker":true}]})",
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
        fill.direction != 1 || !NearlyEqual(fill.qty, 1.0) ||
        fill.liquidity != ai_trade::FillLiquidity::kMaker) {
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
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"execId":"rest-exec-1","orderLinkId":"cid-rest-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.02","isMaker":"0"},{"execId":"rest-exec-1","orderLinkId":"cid-rest-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.02","isMaker":"0"}]}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/account/wallet-balance",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"accountType":"UNIFIED","totalEquity":"12345.6","totalWalletBalance":"12000.0","totalPerpUPL":"345.6"}]}})",
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
        fill.direction != 1 || !NearlyEqual(fill.qty, 1.0) ||
        fill.liquidity != ai_trade::FillLiquidity::kTaker) {
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

    ai_trade::RemoteAccountBalanceSnapshot balance;
    if (!adapter.GetRemoteAccountBalance(&balance) ||
        !balance.has_equity ||
        !balance.has_wallet_balance ||
        !balance.has_unrealized_pnl ||
        !NearlyEqual(balance.equity_usd, 12345.6, 1e-9) ||
        !NearlyEqual(balance.wallet_balance_usd, 12000.0, 1e-9) ||
        !NearlyEqual(balance.unrealized_pnl_usd, 345.6, 1e-9)) {
      std::cerr << "bybit 远端资金快照解析不符合预期\n";
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
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[]}})",
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
    options.ws_reconnect_interval_ms = 0;
    options.execution_skip_history_on_start = false;
    options.http_transport_factory = [transport]() {
      return std::make_unique<MockBybitHttpTransport>(transport);
    };
    options.private_stream_factory =
        [](ai_trade::BybitPrivateStreamOptions ws_options) {
          std::vector<std::vector<ScriptedWsStep>> sessions = {
              {
                  {ScriptedWsAction::kText, R"({"op":"auth","success":true})", ""},
                  {ScriptedWsAction::kText, R"({"op":"subscribe","success":true})", ""},
                  {ScriptedWsAction::kClosed, "", "ws down once"},
              },
              {
                  {ScriptedWsAction::kText, R"({"op":"auth","success":true})", ""},
                  {ScriptedWsAction::kText, R"({"op":"subscribe","success":true})", ""},
                  {ScriptedWsAction::kText,
                   R"({"topic":"execution","data":[{"execId":"ws-recovered-exec-1","orderLinkId":"cid-ws-recovered-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.01"}]})",
                   ""},
              },
          };
          return std::make_unique<ai_trade::BybitPrivateStream>(
              std::move(ws_options),
              std::make_unique<SessionScriptedWebsocketClient>(std::move(sessions)));
        };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "bybit adapter 连接失败（private ws 自动重连测试）\n";
      return 1;
    }

    ai_trade::FillEvent fill;
    if (!adapter.PollFill(&fill)) {
      std::cerr << "private ws 断线后应重连并恢复成交读取\n";
      return 1;
    }
    if (fill.fill_id != "ws-recovered-exec-1" ||
        fill.client_order_id != "cid-ws-recovered-1") {
      std::cerr << "private ws 重连恢复后的成交解析不符合预期\n";
      return 1;
    }
    const std::string health = adapter.ChannelHealthSummary();
    if (health.find("fill_channel=private_ws") == std::string::npos) {
      std::cerr << "private ws 重连后通道状态应回到 private_ws，实际: "
                << health << "\n";
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
        "POST",
        "/v5/order/create",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"orderId":"oid-map-1","orderLinkId":"cid-map-1"}})",
            .error = "",
        });
    transport.AddRoute(
        "GET",
        "/v5/execution/list",
        ai_trade::BybitHttpResponse{
            .status_code = 200,
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"execId":"exec-map-1","orderId":"oid-map-1","orderLinkId":"","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0.01","isMaker":1}]}})",
            .error = "",
        });

    ai_trade::BybitAdapterOptions options;
    options.mode = "paper";
    options.testnet = true;
    options.symbols = {"BTCUSDT"};
    options.primary_symbol = "BTCUSDT";
    options.public_ws_enabled = false;
    options.private_ws_enabled = false;
    options.execution_skip_history_on_start = false;
    options.http_transport_factory = [transport]() {
      return std::make_unique<MockBybitHttpTransport>(transport);
    };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "bybit adapter 连接失败（orderId 映射测试）\n";
      return 1;
    }

    ai_trade::OrderIntent intent;
    intent.client_order_id = "cid-map-1";
    intent.symbol = "BTCUSDT";
    intent.direction = 1;
    intent.qty = 1.0;
    intent.price = 100.0;
    if (!adapter.SubmitOrder(intent)) {
      std::cerr << "orderId 映射测试下单失败\n";
      return 1;
    }

    ai_trade::FillEvent fill;
    if (!adapter.PollFill(&fill)) {
      std::cerr << "orderId 映射测试应获取到成交\n";
      return 1;
    }
    if (fill.client_order_id != intent.client_order_id ||
        fill.fill_id != "exec-map-1" ||
        fill.liquidity != ai_trade::FillLiquidity::kMaker) {
      std::cerr << "orderId 映射为 client_order_id 失败\n";
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
            .body = R"({"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT","lastPrice":"88.8","markPrice":"88.9"}]}})",
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
    options.ws_reconnect_interval_ms = 0;
    options.http_transport_factory = [transport]() {
      return std::make_unique<MockBybitHttpTransport>(transport);
    };
    options.public_stream_factory =
        [](ai_trade::BybitPublicStreamOptions ws_options) {
          std::vector<std::vector<ScriptedWsStep>> sessions = {
              {
                  {ScriptedWsAction::kText, R"({"op":"subscribe","success":true})", ""},
                  {ScriptedWsAction::kClosed, "", "ws down once"},
              },
              {
                  {ScriptedWsAction::kText, R"({"op":"subscribe","success":true})", ""},
                  {ScriptedWsAction::kText,
                   R"({"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT","lastPrice":"120.1","markPrice":"120.2"}})",
                   ""},
              },
          };
          return std::make_unique<ai_trade::BybitPublicStream>(
              std::move(ws_options),
              std::make_unique<SessionScriptedWebsocketClient>(std::move(sessions)));
        };

    ai_trade::BybitExchangeAdapter adapter(options);
    if (!adapter.Connect()) {
      std::cerr << "bybit adapter 连接失败（public ws 自动重连测试）\n";
      return 1;
    }

    ai_trade::MarketEvent event;
    if (!adapter.PollMarket(&event)) {
      std::cerr << "public ws 断线后应重连并恢复行情读取\n";
      return 1;
    }
    if (event.symbol != "BTCUSDT" || !NearlyEqual(event.price, 120.1) ||
        !NearlyEqual(event.mark_price, 120.2)) {
      std::cerr << "public ws 重连恢复后的行情解析不符合预期\n";
      return 1;
    }
    const std::string health = adapter.ChannelHealthSummary();
    if (health.find("market_channel=public_ws") == std::string::npos) {
      std::cerr << "public ws 重连后通道状态应回到 public_ws，实际: "
                << health << "\n";
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

  {
    ai_trade::RegimeConfig config;
    config.enabled = true;
    config.warmup_ticks = 2;
    config.ewma_alpha = 1.0;
    config.trend_threshold = 0.001;
    config.extreme_threshold = 0.010;
    config.volatility_threshold = 0.050;

    ai_trade::RegimeEngine engine(config);
    ai_trade::MarketEvent event;
    event.symbol = "BTCUSDT";
    event.price = 100.0;

    const auto s1 = engine.OnMarket(event);
    if (!s1.warmup || s1.regime != ai_trade::Regime::kRange) {
      std::cerr << "Regime warmup 判定不符合预期\n";
      return 1;
    }

    event.price = 100.3;  // +0.3%
    const auto s2 = engine.OnMarket(event);
    if (s2.warmup || s2.regime != ai_trade::Regime::kUptrend ||
        s2.bucket != ai_trade::RegimeBucket::kTrend) {
      std::cerr << "Regime 趋势判定不符合预期\n";
      return 1;
    }

    event.price = 98.9;  // -1.39%，触发 extreme
    const auto s3 = engine.OnMarket(event);
    if (s3.regime != ai_trade::Regime::kExtreme ||
        s3.bucket != ai_trade::RegimeBucket::kExtreme) {
      std::cerr << "Regime 极端判定不符合预期\n";
      return 1;
    }
  }

  {
    const std::filesystem::path cfg_path =
        std::filesystem::temp_directory_path() / "ai_trade_test_regime_valid.yaml";
    std::ofstream out(cfg_path);
    out << "regime:\n"
        << "  enabled: true\n"
        << "  warmup_ticks: 12\n"
        << "  ewma_alpha: 0.25\n"
        << "  trend_threshold: 0.0012\n"
        << "  extreme_threshold: 0.006\n"
        << "  volatility_threshold: 0.0025\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (!ai_trade::LoadAppConfigFromYaml(cfg_path.string(), &config, &error)) {
      std::cerr << "Regime 配置解析失败: " << error << "\n";
      return 1;
    }
    if (!config.regime.enabled || config.regime.warmup_ticks != 12 ||
        !NearlyEqual(config.regime.ewma_alpha, 0.25) ||
        !NearlyEqual(config.regime.trend_threshold, 0.0012) ||
        !NearlyEqual(config.regime.extreme_threshold, 0.006) ||
        !NearlyEqual(config.regime.volatility_threshold, 0.0025)) {
      std::cerr << "Regime 配置解析结果不符合预期\n";
      return 1;
    }
  }

  {
    const std::filesystem::path cfg_path =
        std::filesystem::temp_directory_path() / "ai_trade_test_regime_invalid.yaml";
    std::ofstream out(cfg_path);
    out << "regime:\n"
        << "  ewma_alpha: 0\n";
    out.close();

    ai_trade::AppConfig config;
    std::string error;
    if (ai_trade::LoadAppConfigFromYaml(cfg_path.string(), &config, &error)) {
      std::cerr << "非法 regime.ewma_alpha 配置应加载失败\n";
      return 1;
    }
    if (error.find("regime.ewma_alpha") == std::string::npos) {
      std::cerr << "非法 regime.ewma_alpha 错误信息不符合预期\n";
      return 1;
    }
  }

  {
    const std::vector<double> series{1.0, 2.0, 3.0, 4.0, 5.0};
    const std::vector<double> delayed = ai_trade::research::TsDelay(series, 2);
    if (!std::isnan(delayed[0]) || !std::isnan(delayed[1]) ||
        !NearlyEqual(delayed[2], 1.0) || !NearlyEqual(delayed[4], 3.0)) {
      std::cerr << "TsDelay 结果不符合预期\n";
      return 1;
    }

    const std::vector<double> delta = ai_trade::research::TsDelta(series, 2);
    if (!std::isnan(delta[0]) || !std::isnan(delta[1]) ||
        !NearlyEqual(delta[2], 2.0) || !NearlyEqual(delta[4], 2.0)) {
      std::cerr << "TsDelta 结果不符合预期\n";
      return 1;
    }

    const std::vector<double> rank = ai_trade::research::TsRank(series, 3);
    if (!std::isnan(rank[0]) || !std::isnan(rank[1]) ||
        !NearlyEqual(rank[2], (2.0 + 0.5) / 3.0) ||
        !NearlyEqual(rank[4], (2.0 + 0.5) / 3.0)) {
      std::cerr << "TsRank 结果不符合预期\n";
      return 1;
    }

    const std::vector<double> y{2.0, 4.0, 6.0, 8.0, 10.0};
    const std::vector<double> corr = ai_trade::research::TsCorr(series, y, 3);
    if (!std::isnan(corr[0]) || !std::isnan(corr[1]) ||
        !NearlyEqual(corr[2], 1.0) || !NearlyEqual(corr[4], 1.0)) {
      std::cerr << "TsCorr 结果不符合预期\n";
      return 1;
    }
  }

  {
    const std::vector<double> factor{1.0, 2.0, 3.0, 4.0, 5.0};
    const std::vector<double> future{10.0, 20.0, 30.0, 40.0, 50.0};
    const ai_trade::research::SpearmanIcResult ic =
        ai_trade::research::ComputeSpearmanIC(factor, future);
    if (ic.sample_count != 5 || !NearlyEqual(ic.ic, 1.0)) {
      std::cerr << "Spearman IC 正相关计算不符合预期\n";
      return 1;
    }

    const std::vector<double> reverse_future{50.0, 40.0, 30.0, 20.0, 10.0};
    const ai_trade::research::SpearmanIcResult reverse_ic =
        ai_trade::research::ComputeSpearmanIC(factor, reverse_future);
    if (reverse_ic.sample_count != 5 || !NearlyEqual(reverse_ic.ic, -1.0)) {
      std::cerr << "Spearman IC 负相关计算不符合预期\n";
      return 1;
    }
  }

  {
    // canary：高置信同向时应按比例接管。
    const std::filesystem::path report_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_integrator_report_canary_applied.json";
    std::string report_error;
    if (!WriteIntegratorReportFile(report_path,
                                   /*auc_mean=*/0.62,
                                   /*delta_auc=*/0.03,
                                   /*split_trained_count=*/5,
                                   /*split_count=*/5,
                                   &report_error)) {
      std::cerr << report_error << "\n";
      return 1;
    }

    ai_trade::IntegratorConfig integrator;
    integrator.enabled = true;
    integrator.mode = ai_trade::IntegratorMode::kShadow;
    integrator.canary_notional_ratio = 0.35;
    integrator.canary_confidence_threshold = 0.30;
    integrator.canary_allow_countertrend = false;
    integrator.shadow.enabled = true;
    integrator.shadow.model_report_path = report_path.string();
    integrator.shadow.active_meta_path.clear();
    integrator.shadow.score_gain = 1.0;

    ai_trade::RegimeConfig regime_config;
    regime_config.enabled = false;
    ai_trade::TradeSystem system(/*risk_cap_usd=*/3000.0,
                                 /*max_order_notional_usd=*/1000.0,
                                 ai_trade::RiskThresholds{},
                                 ai_trade::StrategyConfig{},
                                 /*min_rebalance_notional_usd=*/0.0,
                                 regime_config,
                                 integrator);
    std::string init_error;
    if (!system.InitializeIntegratorShadow(&init_error)) {
      std::cerr << "Integrator 初始化预期成功，错误: " << init_error << "\n";
      return 1;
    }
    system.SetIntegratorMode(ai_trade::IntegratorMode::kCanary);

    (void)system.Evaluate(ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0}, true);
    const auto decision =
        system.Evaluate(ai_trade::MarketEvent{2, "BTCUSDT", 101.0, 101.0}, true);
    if (!decision.integrator_policy_applied ||
        decision.integrator_policy_reason != "canary_applied") {
      std::cerr << "canary 高置信同向预期应接管\n";
      return 1;
    }
    if (!NearlyEqual(decision.base_signal.suggested_notional_usd, 1000.0) ||
        !NearlyEqual(decision.signal.suggested_notional_usd, 350.0)) {
      std::cerr << "canary 接管后名义值不符合预期\n";
      return 1;
    }

    std::filesystem::remove(report_path);
  }

  {
    // canary：若不允许反向且模型方向与原策略相反，应拒绝接管。
    const std::filesystem::path report_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_integrator_report_canary_countertrend.json";
    std::string report_error;
    if (!WriteIntegratorReportFile(report_path,
                                   /*auc_mean=*/0.62,
                                   /*delta_auc=*/0.03,
                                   /*split_trained_count=*/5,
                                   /*split_count=*/5,
                                   &report_error)) {
      std::cerr << report_error << "\n";
      return 1;
    }

    ai_trade::IntegratorConfig integrator;
    integrator.enabled = true;
    integrator.mode = ai_trade::IntegratorMode::kShadow;
    integrator.canary_notional_ratio = 0.35;
    integrator.canary_confidence_threshold = 0.30;
    integrator.canary_allow_countertrend = false;
    integrator.shadow.enabled = true;
    integrator.shadow.model_report_path = report_path.string();
    integrator.shadow.active_meta_path.clear();
    // 负增益用于构造“与 base 反向”的模型输出，验证拦截逻辑。
    integrator.shadow.score_gain = -1.0;

    ai_trade::RegimeConfig regime_config;
    regime_config.enabled = false;
    ai_trade::TradeSystem system(/*risk_cap_usd=*/3000.0,
                                 /*max_order_notional_usd=*/1000.0,
                                 ai_trade::RiskThresholds{},
                                 ai_trade::StrategyConfig{},
                                 /*min_rebalance_notional_usd=*/0.0,
                                 regime_config,
                                 integrator);
    std::string init_error;
    if (!system.InitializeIntegratorShadow(&init_error)) {
      std::cerr << "Integrator 初始化预期成功，错误: " << init_error << "\n";
      return 1;
    }
    system.SetIntegratorMode(ai_trade::IntegratorMode::kCanary);

    (void)system.Evaluate(ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0}, true);
    const auto decision =
        system.Evaluate(ai_trade::MarketEvent{2, "BTCUSDT", 101.0, 101.0}, true);
    if (decision.integrator_policy_applied ||
        decision.integrator_policy_reason != "canary_countertrend_blocked") {
      std::cerr << "canary 反向拦截逻辑不符合预期\n";
      return 1;
    }
    if (!NearlyEqual(decision.signal.suggested_notional_usd,
                     decision.base_signal.suggested_notional_usd)) {
      std::cerr << "canary 反向拦截后不应修改原策略信号\n";
      return 1;
    }

    std::filesystem::remove(report_path);
  }

  {
    // active：低置信应归零；高置信应按 |confidence| 缩放接管。
    const std::filesystem::path report_path =
        std::filesystem::temp_directory_path() /
        "ai_trade_test_integrator_report_active_policy.json";
    std::string report_error;
    if (!WriteIntegratorReportFile(report_path,
                                   /*auc_mean=*/0.62,
                                   /*delta_auc=*/0.03,
                                   /*split_trained_count=*/5,
                                   /*split_count=*/5,
                                   &report_error)) {
      std::cerr << report_error << "\n";
      return 1;
    }

    ai_trade::RegimeConfig regime_config;
    regime_config.enabled = false;

    {
      ai_trade::IntegratorConfig integrator_low;
      integrator_low.enabled = true;
      integrator_low.mode = ai_trade::IntegratorMode::kShadow;
      integrator_low.active_confidence_threshold = 0.60;
      integrator_low.shadow.enabled = true;
      integrator_low.shadow.model_report_path = report_path.string();
      integrator_low.shadow.active_meta_path.clear();
      integrator_low.shadow.score_gain = 1.0;

      ai_trade::TradeSystem system_low(/*risk_cap_usd=*/3000.0,
                                       /*max_order_notional_usd=*/1000.0,
                                       ai_trade::RiskThresholds{},
                                       ai_trade::StrategyConfig{},
                                       /*min_rebalance_notional_usd=*/0.0,
                                       regime_config,
                                       integrator_low);
      std::string init_error;
      if (!system_low.InitializeIntegratorShadow(&init_error)) {
        std::cerr << "Integrator 初始化预期成功，错误: " << init_error << "\n";
        return 1;
      }
      system_low.SetIntegratorMode(ai_trade::IntegratorMode::kActive);
      (void)system_low.Evaluate(
          ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0}, true);
      const auto decision_low =
          system_low.Evaluate(ai_trade::MarketEvent{2, "BTCUSDT", 101.0, 101.0},
                              true);
      if (!decision_low.integrator_policy_applied ||
          decision_low.integrator_policy_reason != "active_low_confidence_to_flat" ||
          !NearlyEqual(decision_low.signal.suggested_notional_usd, 0.0) ||
          decision_low.signal.direction != 0) {
        std::cerr << "active 低置信归零逻辑不符合预期\n";
        return 1;
      }
    }

    {
      ai_trade::IntegratorConfig integrator_high;
      integrator_high.enabled = true;
      integrator_high.mode = ai_trade::IntegratorMode::kShadow;
      integrator_high.active_confidence_threshold = 0.20;
      integrator_high.shadow.enabled = true;
      integrator_high.shadow.model_report_path = report_path.string();
      integrator_high.shadow.active_meta_path.clear();
      integrator_high.shadow.score_gain = 2.0;

      ai_trade::TradeSystem system_high(/*risk_cap_usd=*/3000.0,
                                        /*max_order_notional_usd=*/1000.0,
                                        ai_trade::RiskThresholds{},
                                        ai_trade::StrategyConfig{},
                                        /*min_rebalance_notional_usd=*/0.0,
                                        regime_config,
                                        integrator_high);
      std::string init_error;
      if (!system_high.InitializeIntegratorShadow(&init_error)) {
        std::cerr << "Integrator 初始化预期成功，错误: " << init_error << "\n";
        return 1;
      }
      system_high.SetIntegratorMode(ai_trade::IntegratorMode::kActive);
      (void)system_high.Evaluate(
          ai_trade::MarketEvent{1, "BTCUSDT", 100.0, 100.0}, true);
      const auto decision_high =
          system_high.Evaluate(ai_trade::MarketEvent{2, "BTCUSDT", 101.0, 101.0},
                               true);
      const double expected_notional =
          std::fabs(decision_high.integrator_confidence) *
          std::fabs(decision_high.base_signal.suggested_notional_usd);
      if (!decision_high.integrator_policy_applied ||
          decision_high.integrator_policy_reason != "active_applied" ||
          decision_high.integrator_confidence <=
              integrator_high.active_confidence_threshold ||
          !NearlyEqual(std::fabs(decision_high.signal.suggested_notional_usd),
                       expected_notional,
                       1e-6)) {
        std::cerr << "active 高置信缩放接管逻辑不符合预期\n";
        return 1;
      }
    }

    std::filesystem::remove(report_path);
  }

  {
    std::vector<ai_trade::research::ResearchBar> bars;
    bars.reserve(220);
    for (int i = 0; i < 220; ++i) {
      ai_trade::research::ResearchBar bar;
      bar.ts_ms = 1'700'000'000'000LL + static_cast<std::int64_t>(i) * 300'000LL;
      bar.close = 100.0 + static_cast<double>(i) * 0.12 +
                  std::sin(static_cast<double>(i) * 0.08) * 1.5;
      bar.open = bar.close - 0.20;
      bar.high = bar.close + 0.35;
      bar.low = bar.close - 0.40;
      bar.volume = 1200.0 + static_cast<double>(i % 11) * 23.0 +
                   std::cos(static_cast<double>(i) * 0.05) * 40.0;
      bars.push_back(bar);
    }

    ai_trade::research::MinerConfig config;
    config.random_seed = 42;
    config.top_k = 5;
    config.train_split_ratio = 0.7;
    config.complexity_penalty = 0.01;

    ai_trade::research::Miner miner;
    const ai_trade::research::MinerReport report_1 = miner.Run(bars, config);
    const ai_trade::research::MinerReport report_2 = miner.Run(bars, config);

    if (report_1.factors.size() != 5 || report_2.factors.size() != 5) {
      std::cerr << "Miner TopK 结果数量不符合预期\n";
      return 1;
    }
    if (report_1.random_seed != 42 ||
        report_1.search_space_version.empty() ||
        report_1.random_baseline_trials <= 0 ||
        report_1.random_baseline_oos_abs_ic.sample_count <= 0) {
      std::cerr << "Miner 报告元数据/随机基线统计不符合预期\n";
      return 1;
    }
    if (!ai_trade::research::IsFinite(report_1.oos_random_baseline_threshold_p90) ||
        report_1.oos_random_baseline_threshold_p90 < 0.0 ||
        !ai_trade::research::IsFinite(report_1.top_factor_oos_abs_ic)) {
      std::cerr << "Miner 随机基线阈值或 Top 因子 IC 非法\n";
      return 1;
    }
    if (report_1.factor_set_version.empty() ||
        report_1.factor_set_version != report_2.factor_set_version) {
      std::cerr << "Miner 同种子复现性不符合预期\n";
      return 1;
    }
    if (report_1.factors[0].expression != report_2.factors[0].expression) {
      std::cerr << "Miner Top 因子复现性不符合预期\n";
      return 1;
    }
    const auto has_delay =
        std::any_of(report_1.candidate_expressions.begin(),
                    report_1.candidate_expressions.end(),
                    [](const std::string& expr) {
                      return expr.find("ts_delay") != std::string::npos;
                    });
    const auto has_delta =
        std::any_of(report_1.candidate_expressions.begin(),
                    report_1.candidate_expressions.end(),
                    [](const std::string& expr) {
                      return expr.find("ts_delta") != std::string::npos;
                    });
    const auto has_rank =
        std::any_of(report_1.candidate_expressions.begin(),
                    report_1.candidate_expressions.end(),
                    [](const std::string& expr) {
                      return expr.find("ts_rank") != std::string::npos;
                    });
    const auto has_corr =
        std::any_of(report_1.candidate_expressions.begin(),
                    report_1.candidate_expressions.end(),
                    [](const std::string& expr) {
                      return expr.find("ts_corr") != std::string::npos;
                    });
    if (!has_delay || !has_delta || !has_rank || !has_corr) {
      std::cerr << "Miner 候选因子未覆盖必需时序算子\n";
      return 1;
    }
    if (report_1.factors.front().search_space_version != "ts_ops_v1" ||
        report_1.factors.front().valid_universe.empty() ||
        report_1.factors.front().rolling_ic_oos.sample_count <= 0) {
      std::cerr << "Miner 因子诊断字段不符合预期\n";
      return 1;
    }
  }

  {
    // OnlineFeatureEngine 单元测试
    ai_trade::research::OnlineFeatureEngine engine(50);
    
    // 1. 预热数据
    for (int i = 0; i < 20; ++i) {
      ai_trade::MarketEvent event;
      event.symbol = "BTCUSDT";
      event.price = 100.0 + i; // 100, 101, ... 119
      event.volume = 1000.0 + i * 100; // 1000, 1100, ...
      engine.OnMarket(event);
    }

    if (!engine.IsReady()) {
      std::cerr << "OnlineFeatureEngine 预热后应就绪\n";
      return 1;
    }

    // 2. 验证算子计算
    // ts_delay(close, 1) -> 118.0 (current is 119.0)
    double delay = engine.Evaluate("ts_delay(close, 1)");
    if (!NearlyEqual(delay, 118.0)) {
      std::cerr << "OnlineFeatureEngine ts_delay 计算错误: " << delay << "\n";
      return 1;
    }

    // ts_delta(close, 1) -> 1.0
    double delta = engine.Evaluate("ts_delta(close, 1)");
    if (!NearlyEqual(delta, 1.0)) {
      std::cerr << "OnlineFeatureEngine ts_delta 计算错误: " << delta << "\n";
      return 1;
    }

    // ts_rank(close, 5) -> 0.9 (strictly increasing: (4 + 0.5)/5)
    double rank = engine.Evaluate("ts_rank(close, 5)");
    if (!NearlyEqual(rank, 0.9)) {
      std::cerr << "OnlineFeatureEngine ts_rank 计算错误: " << rank << "\n";
      return 1;
    }

    // ts_corr(close, volume, 10) -> 1.0 (perfectly correlated)
    double corr = engine.Evaluate("ts_corr(close, volume, 10)");
    if (!NearlyEqual(corr, 1.0)) {
      std::cerr << "OnlineFeatureEngine ts_corr 计算错误: " << corr << "\n";
      return 1;
    }

    // rsi(close, 14) -> 100.0 (严格递增序列，无下跌，RSI=100)
    double rsi = engine.Evaluate("rsi(close, 14)");
    if (!NearlyEqual(rsi, 100.0)) {
      std::cerr << "OnlineFeatureEngine rsi 计算错误: 预期 100.0, 实际 " << rsi << "\n";
      return 1;
    }

    // ema(close, 5)
    // 简单验证：EMA 应接近最新价格
    double ema = engine.Evaluate("ema(close, 5)");
    if (!std::isfinite(ema) || std::abs(ema - 119.0) > 5.0) {
      std::cerr << "OnlineFeatureEngine ema 计算异常: " << ema << "\n";
      return 1;
    }

    // macd 组合表达式验证
    double macd = engine.Evaluate("ema(close,12)-ema(close,26)");
    if (!std::isfinite(macd)) {
      std::cerr << "OnlineFeatureEngine macd 组合计算异常\n";
      return 1;
    }

    // 验证：窗口不足时应返回 NaN 而非 0.0
    // ts_delay(close, 60) 需要 60+1 个数据，当前仅 20 个有效数据，padding 后窗口 50。
    double invalid_delay = engine.Evaluate("ts_delay(close, 60)");
    if (std::isfinite(invalid_delay)) {
      std::cerr << "OnlineFeatureEngine 数据不足时应返回 NaN，实际返回: " << invalid_delay << "\n";
      return 1;
    }
    
    // 3. 验证批量计算
    std::vector<std::string> exprs = {"close", "volume"};
    std::vector<double> results = engine.EvaluateBatch(exprs);
    if (results.size() != 2 || !NearlyEqual(results[0], 119.0) || !NearlyEqual(results[1], 2900.0)) {
       std::cerr << "OnlineFeatureEngine EvaluateBatch 计算错误\n";
       return 1;
    }
  }

  return 0;
}
