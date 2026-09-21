#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <sstream>
#include <stdexcept>
#include <vector>

#include "app/bot_app.h"
#include "exchange/bybit_exchange_adapter.h"
#include "market/closed_bar_clock.h"
#include "monitor/gate_monitor.h"
#include "system/trade_system.h"

namespace {
using namespace ai_trade;
constexpr std::int64_t kStart = 1704067200000LL;
constexpr std::int64_t kBar = ClosedBarClock::kIntervalMs;
void Check(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}
bool Near(double a, double b) { return std::fabs(a - b) < 1e-8; }
template <typename F> void Reject(F action, const char* message) {
  bool rejected = false;
  try { action(); } catch (const std::invalid_argument&) { rejected = true; }
  Check(rejected, message);
}
struct Directory {
  std::filesystem::path path;
  Directory() {
    std::random_device random;
    for (int i = 0; i < 10; ++i) {
      path = std::filesystem::temp_directory_path() /
          ("ai-trade-mvp-test-" + std::to_string(random()) + "-" + std::to_string(random()));
      if (std::filesystem::create_directory(path)) return;
    }
    throw std::runtime_error("cannot create isolated fixture directory");
  }
  ~Directory() {
    std::error_code error;
    std::filesystem::remove_all(path, error);
  }
};
AppConfig Config() {
  AppConfig config;
  config.mode = "replay";
  config.exchange = "bybit";
  config.closed_bar_mvp = true;
  config.integrator.mode = IntegratorMode::kOff;
  config.self_evolution.enabled = false;
  config.execution_adaptive_fee_gate_enabled = false;
  config.universe.enabled = false;
  config.regime.bar_interval_ms = kBar;
  config.reconcile.enabled = false;
  config.system_remote_risk_refresh_interval_ticks = 0;
  return config;
}
MarketEvent Tick(std::int64_t offset, double price, double volume = 1) {
  MarketEvent event;
  event.ts_ms = kStart + offset;
  event.price = event.mark_price = price;
  event.volume = volume;
  event.interval_ms = 5000;
  event.funding_rate_per_interval = 0;
  return event;
}
MarketEvent Bar(int index, const std::string& symbol = "BTCUSDT") {
  auto event = Tick((index + 1) * kBar, 101 + index, 4);
  event.symbol = symbol;
  event.interval_ms = kBar;
  event.open_price = 100 + index;
  event.high_price = 102 + index;
  event.low_price = 99 + index;
  event.completed_bar = true;
  return event;
}
std::vector<MarketDecision> TickDecisions(bool dense, bool mutate_future) {
  TradeSystem system(Config());
  std::vector<MarketDecision> result;
  for (int i = 0; i < 41; ++i) {
    const double p = 100 + i + (mutate_future && i >= 39 ? 1000 : 0);
    for (const auto& event : {Tick(i * kBar, p),
                              Tick(i * kBar + 60000, p + 2),
                              Tick(i * kBar + 120000, p - 1),
                              Tick(i * kBar + 240000, p + 1)}) {
      const auto decision = system.Evaluate(event);
      if (decision.base_signal.new_decision) result.push_back(decision);
      else Check(!decision.intent, "intrabar event generated alpha/rebalance order");
      if (dense) {
        auto repeated = event;
        repeated.ts_ms += 1000;
        repeated.volume = 0;
        const auto intrabar = system.Evaluate(repeated);
        Check(!intrabar.base_signal.new_decision && !intrabar.intent,
              "extra tick changed strategy decision cadence");
      }
    }
  }
  return result;
}
void CheckSignalEqual(const MarketDecision& a, const MarketDecision& b) {
  Check(a.base_signal.direction == b.base_signal.direction &&
        Near(a.base_signal.suggested_notional_usd, b.base_signal.suggested_notional_usd) &&
        Near(a.base_signal.confidence, b.base_signal.confidence) &&
        Near(a.regime.volatility_level, b.regime.volatility_level) &&
        a.base_signal.valid_until_ms == b.base_signal.valid_until_ms,
        "closed-bar signal or sizing depends on tick subdivision/future data");
}
void CheckClocks() {
  const auto sparse = TickDecisions(false, false);
  const auto dense = TickDecisions(true, false);
  const auto future = TickDecisions(true, true);
  Check(sparse.size() == 40 && dense.size() == 40 && future.size() == 40,
        "wrong closed-bar decision count, including EOF partial bar");
  TradeSystem explicit_system(Config());
  TradeSystem multi_system(Config());
  for (int i = 0; i < 40; ++i) {
    const auto explicit_decision = explicit_system.Evaluate(Bar(i));
    CheckSignalEqual(sparse[i], dense[i]);
    CheckSignalEqual(sparse[i], explicit_decision);
    if (i < 39) CheckSignalEqual(sparse[i], future[i]);
    multi_system.Evaluate(Bar(i, "ETHUSDT"));
    CheckSignalEqual(sparse[i], multi_system.Evaluate(Bar(i)));
  }
  ClosedBarClock clock;
  auto first = Tick(1000, 100);
  Check(!clock.Push(first), "startup partial bar was closed");
  Check(!clock.Push(Tick(kBar + 1000, 101)), "startup partial bar was emitted");
  Check(clock.Push(Tick(2 * kBar + 1000, 102)).has_value(), "next full observed bucket lost");
  Reject([&] { clock.Push(Tick(kBar, 100)); }, "out-of-order tick accepted");
  ClosedBarClock duplicate;
  duplicate.Push(Bar(0));
  Reject([&] { duplicate.Push(Bar(0)); }, "duplicate closed bar accepted");
  Reject([&] { duplicate.Push(Bar(2)); }, "missing bar silently filled");
  auto unconfirmed = Bar(1);
  unconfirmed.completed_bar = false;
  Reject([&] { duplicate.Push(unconfirmed); }, "unconfirmed OHLC accepted as tick");
}
void CheckIntrabarRiskAndGate() {
  TradeSystem system(Config());
  system.SyncAccountFromRemotePositions({
      {.symbol="BTCUSDT", .qty=100, .avg_entry_price=100,
       .mark_price=100, .liquidation_price=50}});
  system.Evaluate(Tick(0, 100));
  const auto risk = system.Evaluate(Tick(5000, 87));
  Check(!risk.base_signal.new_decision && risk.intent && risk.intent->reduce_only &&
        risk.risk_adjusted.risk_mode == RiskMode::kCooldown,
        "waiting for 5m close masked immediate drawdown protection");
  GateConfig gate_config;
  gate_config.window_ticks = 1;
  GateMonitor gate(gate_config);
  Signal retained;
  retained.new_decision = false;
  retained.suggested_notional_usd = 1000;
  for (int i = 0; i < 100; ++i)
    gate.OnDecision(retained, RiskAdjustedPosition{"BTCUSDT", 1000}, std::nullopt);
  const auto window = gate.OnTick();
  Check(window && window->effective_signals == 0 && window->raw_signals == 0,
        "cached targets inflated activity evidence");
}
void WriteFixture(const std::filesystem::path& path, bool mutate_future = false) {
  std::ofstream out(path);
  out << "timestamp,symbol,open,high,low,price,volume,interval_ms,"
         "funding_rate_per_interval,mark_open,mark_close\n";
  const int openings[] = {100, 200, 250, 260};
  for (int i = 0; i < 4; ++i) {
    const int p = openings[i];
    const int close = p + 1 + ((mutate_future && i == 1) ? 50 : 0);
    out << kStart + i * kBar << ",BTCUSDT," << p << ',' << close << ',' << p
        << ',' << close << ",4,300000," << i * 0.01 << ',' << p - 1 << ',' << close - 1 << '\n';
  }
  Check(static_cast<bool>(out), "fixture write failed");
}
BybitAdapterOptions Options(const std::filesystem::path& path) {
  BybitAdapterOptions options;
  options.replay_causal_bars = true;
  options.replay_market_data_path = path.string();
  options.public_ws_enabled = options.private_ws_enabled = false;
  options.replay_entry_fee_bps = options.replay_exit_fee_bps = 5.5;
  options.replay_expected_slippage_bps = 1;
  options.http_transport_factory = []() -> std::unique_ptr<BybitHttpTransport> {
    throw std::runtime_error("network forbidden in MVP test");
  };
  return options;
}
OrderIntent Entry(const char* id) {
  OrderIntent intent;
  intent.client_order_id = id;
  intent.direction = 1;
  intent.qty = 1;
  intent.price = 101;
  return intent;
}
void CheckCausalFills(bool mutate_future) {
  Directory directory;
  const auto path = directory.path / "synthetic.csv";
  WriteFixture(path, mutate_future);
  BybitExchangeAdapter adapter(Options(path));
  Check(adapter.Connect(), "causal fixture did not load");
  MarketEvent event;
  FillEvent fill;
  Check(adapter.PollMarket(&event) && event.execution_only && std::isnan(event.high_price),
        "opening event exposed future OHLC");
  Check(adapter.PollMarket(&event) && event.completed_bar && event.price == 101,
        "first close not isolated from next bar");
  const auto entry = Entry("one-entry");
  Check(adapter.SubmitOrder(entry) && adapter.SubmitOrder(entry), "entry/duplicate submission failed");
  Check(!adapter.PollFill(&fill), "entry filled at decision close");
  auto early_terminal = Entry("early-terminal");
  early_terminal.reduce_only = true;
  early_terminal.purpose = OrderPurpose::kReduce;
  early_terminal.replay_terminal_settlement = true;
  Check(!adapter.SubmitOrder(early_terminal), "terminal exception allowed before EOF");
  Check(adapter.PollMarket(&event) && event.execution_only && event.price == 200 &&
        event.mark_price == 199 && std::isnan(event.high_price), "wrong next-open price/mark");
  AccountState account;
  account.OnMarket(event);
  Check(account.ApplyFunding(event.symbol, event.funding_rate_per_interval) == 0,
        "new entrant charged funding before owning position");
  int parts = 0;
  while (adapter.PollFill(&fill)) {
    ++parts;
    Check(Near(fill.price, 200.02), "future close or stale decision price affected fill");
    account.ApplyFill(fill);
  }
  Check(parts == 2 && Near(account.position_qty("BTCUSDT"), 1),
        "duplicate submission or partial-fill quantity error");
  Check(adapter.SubmitOrder(entry) && !adapter.PollFill(&fill),
        "already filled client ID was executed again");
  Check(adapter.PollMarket(&event) && event.completed_bar && event.funding_rate_per_interval == 0,
        "funding repeated at close");
  auto cancel = Entry("cancel-before-next-open");
  Check(adapter.SubmitOrder(cancel), "cancel fixture entry failed");
  std::vector<RemoteOpenOrderSnapshot> pending;
  Check(adapter.GetRemoteOpenOrders(&pending) && pending.size() == 1,
        "pending taker missing from open-order view");
  Check(adapter.CancelOrder(cancel.client_order_id), "queued taker cancel failed");
  Check(adapter.SubmitOrder(cancel), "idempotent cancelled ID retry rejected");
  auto exit = Entry("next-open-reduce");
  exit.direction = -1;
  exit.reduce_only = true;
  exit.purpose = OrderPurpose::kReduce;
  Check(adapter.SubmitOrder(exit) && !adapter.PollFill(&fill), "reduce filled on signal event");
  Check(adapter.PollMarket(&event) && event.execution_only && event.price == 250, "missing next opening");
  account.OnMarket(event);
  const double funding = account.ApplyFunding(event.symbol, event.funding_rate_per_interval);
  Check(Near(funding, 4.98), "old holder funding not based on boundary mark");
  parts = 0;
  while (adapter.PollFill(&fill)) {
    ++parts;
    Check(Near(fill.price, 249.975), "wrong adverse sell slippage");
    account.ApplyFill(fill);
  }
  Check(parts == 2 && account.position_qty("BTCUSDT") == 0, "cancelled entry filled or reduction wrong");
  Check(Near(account.cumulative_realized_net_pnl_usd(),
             249.975 - 200.02 - 4.98 - (249.975 + 200.02) * 0.00055),
        "fee/funding/quantity ledger does not reconcile");
}
void CheckTerminalAndApp() {
  Directory directory;
  const auto path = directory.path / "synthetic.csv";
  WriteFixture(path);
  BybitExchangeAdapter adapter(Options(path));
  Check(adapter.Connect(), "terminal fixture connect failed");
  MarketEvent event;
  FillEvent fill;
  adapter.PollMarket(&event);
  adapter.PollMarket(&event);
  Check(adapter.SubmitOrder(Entry("held")), "terminal held entry failed");
  adapter.PollMarket(&event);
  while (adapter.PollFill(&fill)) {}
  while (adapter.PollMarket(&event)) {}
  Check(adapter.SubmitOrder(Entry("tail-unfilled")) && !adapter.PollFill(&fill),
        "EOF invented a successor fill");
  adapter.CancelOrder("tail-unfilled");
  auto terminal = Entry("terminal-reduce");
  terminal.direction = -1;
  terminal.reduce_only = true;
  terminal.purpose = OrderPurpose::kReduce;
  terminal.replay_terminal_settlement = true;
  Check(adapter.SubmitOrder(terminal), "explicit terminal close rejected");
  double qty = 0;
  while (adapter.PollFill(&fill)) {
    Check(Near(fill.price, 261 * 0.9999) && fill.fee > 0, "terminal price/cost missing");
    qty += fill.qty;
  }
  Check(Near(qty, 1), "terminal left a synthetic position");
  auto config = Config();
  config.data_path = (directory.path / "app").string();
  config.bybit.replay_market_data_path = path.string();
  // Fixed synthetic integration fixture isolates execution; it is not the
  // frozen reference strategy and does not assert an economic gate passes.
  config.execution_enable_fee_aware_entry_gate = false;
  config.system_status_log_interval_ticks = 1;
  BotApplication app(config);
  std::ostringstream logs;
  auto* previous = std::cout.rdbuf(logs.rdbuf());
  int code = -1;
  try { code = app.Run(); }
  catch (...) { std::cout.rdbuf(previous); throw; }
  std::cout.rdbuf(previous);
  Check(code == 0, "complete causal application did not settle");
  const auto output = logs.str();
  Check(output.find("REPLAY_TERMINAL_SETTLEMENT_DONE: position_count=0") != std::string::npos,
        "application did not prove terminal flatness");
  const auto funding = output.find("source=causal_open_before_fills");
  Check(funding != std::string::npos &&
        output.find("FUNDING_APPLIED:") == output.rfind("FUNDING_APPLIED:"),
        "application boundary funding missing or counted more than once");
  Check(output.find("REPLAY_TERMINAL_CLOSE_SUBMITTED: order_count=1") != std::string::npos,
        "synthetic application did not exercise terminal exit costs");
  const auto journal = directory.path / "app" / "mvp_manual_risk_v1.journal";
  Check(std::filesystem::exists(journal) && std::filesystem::file_size(journal) > 100,
        "application did not persist manual risk lifecycle");
  const auto journal_size = std::filesystem::file_size(journal);
  BotApplication restart(config);
  Check(restart.Run() != 0 && std::filesystem::file_size(journal) == journal_size,
        "replay restart silently cleared risk history");
}
void CheckMultiSymbolExecutionAndDataGuard() {
  Directory directory;
  const auto path = directory.path / "multi.csv";
  {
    std::ofstream out(path);
    out << "timestamp,symbol,open,high,low,price,volume,interval_ms,"
           "funding_rate_per_interval,mark_open,mark_close\n";
    for (const auto* symbol : {"ETHUSDT", "BTCUSDT"}) {
      for (int i = 0; i < 3; ++i) {
        const int price = 100 + i;
        out << kStart + i * kBar << ',' << symbol << ',' << price << ',' << price + 1
            << ',' << price << ',' << price + 1 << ",4,300000,0,"
            << price << ',' << price + 1 << '\n';
      }
    }
  }
  BybitExchangeAdapter adapter(Options(path));
  Check(adapter.Connect(), "multi-symbol fixture load failed");
  MarketEvent event;
  FillEvent fill;
  bool submitted = false;
  bool filled = false;
  int batches = 0;
  while (adapter.PollMarket(&event)) {
    if (event.decision_batch_end) {
      ++batches;
      Check(event.completed_bar && event.symbol == "ETHUSDT",
            "daily gate advanced before all symbols closed");
    }
    if (!submitted && event.completed_bar) {
      auto entry = Entry("eth-after-btc-close");
      entry.symbol = "ETHUSDT";
      Check(adapter.SubmitOrder(entry), "multi-symbol entry failed");
      submitted = true;
    }
    while (adapter.PollFill(&fill)) {
      Check(event.execution_only && event.symbol == "ETHUSDT" &&
            event.ts_ms == kStart + kBar, "other symbol/close event filled pending taker");
      filled = true;
    }
  }
  Check(filled && batches == 3, "multi-symbol execution or calendar batch missing");
  {
    std::ofstream out(path, std::ios::app);
    out << kStart << ",BTCUSDT,100,100,100,100,1,300000,0,100,100\n";
  }
  BybitExchangeAdapter corrupt(Options(path));
  Check(!corrupt.Connect(), "duplicate/backwards source bars silently accepted");
}
void CheckConfiguration(const char* config_path) {
  AppConfig config;
  std::string error;
  Check(LoadAppConfigFromYaml(config_path, &config, &error), "frozen MVP config did not load");
  Check(config.closed_bar_mvp && config.trend_ema_fast == 12 && config.trend_ema_slow == 26 &&
        Near(config.vol_target_pct, 0.40), "MVP parameters were not fixed defaults");
  config.mode = "live";
  Check(!ValidateClosedBarMvpConfig(config, &error), "MVP enabled outside offline scope");
  config.mode = "replay";
  config.strategy_defensive_notional_ratio = .3;
  Check(!ValidateClosedBarMvpConfig(config, &error), "phase2 defensive policy accepted");
}
}  // namespace
int main(int argc, char** argv) {
  try {
    Check(argc == 2, "expected path to frozen MVP config");
    CheckConfiguration(argv[1]);
    CheckClocks();
    CheckIntrabarRiskAndGate();
    CheckCausalFills(false);
    CheckCausalFills(true);
    CheckTerminalAndApp();
    CheckMultiSymbolExecutionAndDataGuard();
    std::cout << "MVP_CLOCK_EXECUTION_CONTRACT_PASS (synthetic, not economic qualification)\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "MVP_CONTRACT_FAILURE: " << error.what() << '\n';
    return 1;
  }
}
