// TEST_ONLY: no exchange/network; production controller, journal, app and sender.
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <sstream>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <fcntl.h>
#include <unistd.h>
#if defined(__clang__)
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wkeyword-macro"
#endif
#define private public
#include "app/bot_app.h"
#undef private
#if defined(__clang__)
#pragma clang diagnostic pop
#endif
#include "exchange/mock_exchange_adapter.h"

namespace {
using namespace ai_trade;
void Check(bool ok, const std::string& why) {
  if (!ok) throw std::runtime_error(why);
}
struct Temp {
  std::filesystem::path path;
  Temp() {
    std::string pattern = (std::filesystem::temp_directory_path() / "evolution-safety-XXXXXX").string();
    char* made = ::mkdtemp(pattern.data());
    Check(made, "mkdtemp");
    path = made;
  }
  ~Temp() { std::error_code ec; std::filesystem::remove_all(path, ec); }
};
SelfEvolutionConfig Config() {
  SelfEvolutionConfig c;
  c.enabled = c.safety_withdrawal_enabled = c.use_virtual_pnl = true;
  c.use_counterfactual_search = c.counterfactual_require_temporal_holdout = true;
  c.enable_learnability_gate = true;
  c.update_interval_ticks = 4;  // Intentionally insufficient 10/10 holdout.
  c.min_update_interval_ticks = 1000;
  c.rollback_degrade_windows = 2;
  return c;
}
void Controller() {
  auto config = Config();
  SelfEvolutionController c(config);
  std::string error;
  Check(c.Initialize(0, 10000, {0.4, 0.6}, &error), error);
  double price = 100;
  for (int i = 1; i <= 8; ++i) {
    price *= 0.99;
    const auto a = c.OnTick(i, 0, RegimeBucket::kRange, 0, 0, 80, 0, price, "SYNTH");
    if (i == 4) Check(a && a->reason_code == "EVOLUTION_COUNTERFACTUAL_HOLDOUT_INSUFFICIENT", "first loss bypassed strict update gate");
    if (i == 8) Check(a && a->type == SelfEvolutionActionType::kSafetyWithdrawn &&
        a->trend_weight_after == 0.4 && !a->rolled_back_to_baseline, "no independent safety withdrawal");
  }
  Check(c.safety_withdrawn(), "missing latch");
  for (auto bucket : {RegimeBucket::kTrend, RegimeBucket::kRange, RegimeBucket::kExtreme}) {
    Check(!c.OnTick(100000, 1000, bucket, 0, 0, 80, 0, 100000, "SYNTH"), "clock/bucket unlocked learning");
  }
  Check(c.Initialize(0, 10000, {0.4, 0.6}, &error) && c.safety_withdrawn(), "reinitialize unlocked");

  // Inactive/flat/positive windows aren't negative economic evidence.
  for (double multiplier : {1.0, 1.01}) {
    SelfEvolutionController healthy(config);
    Check(healthy.Initialize(0, 10000, {0.5, 0.5}, &error), error);
    price = 100;
    for (int i = 1; i <= 24; ++i) {
      price *= multiplier;
      healthy.OnTick(i, 0, RegimeBucket::kRange, 0.1, 0, 80, 0, price, "SYNTH");
    }
    Check(!healthy.safety_withdrawn(), "non-loss withdrawal");
  }
  config.safety_withdrawal_enabled = false;
  SelfEvolutionController legacy(config);
  Check(legacy.Initialize(0, 10000, {0.5, 0.5}, &error), error);
  for (int i = 1; i <= 16; ++i)
    legacy.OnTick(i, 0, RegimeBucket::kRange, 0, 0, 80, 0, 100.0 / i, "SYNTH");
  Check(!legacy.safety_withdrawn(), "legacy semantics changed");

  config = Config();
  config.update_interval_ticks = 30;
  config.counterfactual_train_fraction = 0.5;
  SelfEvolutionController mixed(config);
  Check(mixed.Initialize(0, 10000, {0.5, 0.5}, &error), error);
  double losing_price = 100, healthy_price = 100;
  for (int i = 1; i <= 60; ++i) {
    const bool losing = i % 30 >= 1 && i % 30 <= 3;
    double& mark = losing ? losing_price : healthy_price;
    mark *= losing ? 0.99 : 1.001;
    const auto a = mixed.OnTick(i, 0, losing ? RegimeBucket::kTrend : RegimeBucket::kRange,
        0, 0, 80, 0, mark, losing ? "LOSS" : "HEALTHY");
    if (i == 30) Check(a && a->regime_bucket == RegimeBucket::kRange, "fixture did not exercise other qualified bucket");
    if (i == 60) Check(a && a->type == SelfEvolutionActionType::kSafetyWithdrawn &&
        a->regime_bucket == RegimeBucket::kTrend, "qualified bucket starved safety loss");
  }
  config.use_virtual_pnl = false;
  config.update_interval_ticks = 4;
  SelfEvolutionController realized(config);
  Check(realized.Initialize(0, 10000, {0.5, 0.5}, &error), error);
  double pnl = 0;
  for (int i = 1; i <= 16; ++i) {
    if (i <= 4 || i > 8) pnl -= 1;
    realized.OnTick(i, pnl);
    Check(realized.safety_withdrawn() == (i == 16), "idle window did not break consecutive realized loss");
  }
}
void Persistence() {
  std::string error;
  Temp clean;
  for (int i = 0; i < 2; ++i) {
    EvolutionSafetyJournal j;
    Check(j.Open(clean.path.string(), true, &error) && !j.withdrawn(), "clean restart latched");
    Check(j.CloseClean(&error), error);
  }
  Temp crash;
  { EvolutionSafetyJournal j; Check(j.Open(crash.path.string(), true, &error), error); }
  { EvolutionSafetyJournal j; Check(j.Open(crash.path.string(), false, &error) && j.withdrawn(), "crash/config disable unlocked"); }
  Temp withdrawn;
  {
    EvolutionSafetyJournal j;
    Check(j.Open(withdrawn.path.string(), true, &error), error);
    EvolutionSafetyJournal conflict;
    Check(!conflict.Open(withdrawn.path.string(), true, &error), "second writer admitted");
    Check(j.Withdraw(&error) && j.CloseClean(&error), error);
  }
  { EvolutionSafetyJournal j; Check(j.Open(withdrawn.path.string(), false, &error) && j.withdrawn(), "withdrawal unlocked"); }
  Temp broken;
  { std::ofstream out(broken.path / "evolution_safety_v1.journal"); out << "AI_TRADE_EVOLUTION_SAFETY_V1\nAR"; }
  { EvolutionSafetyJournal j; Check(j.Open(broken.path.string(), true, &error) && j.withdrawn(), "truncation unlocked"); }
  Temp write_failure;
  {
    EvolutionSafetyJournal j;
    Check(j.Open(write_failure.path.string(), true, &error), error);
    // Deterministic bad descriptor fault; old durable ARMED must survive it.
    ::close(j.fd_); j.fd_ = -1;
    Check(!j.Withdraw(&error) && j.withdrawn(), "write failure released gate");
  }
  { EvolutionSafetyJournal j; Check(j.Open(write_failure.path.string(), true, &error) && j.withdrawn(), "failed write restarted clear"); }
}
struct CountingAdapter : MockExchangeAdapter {
  CountingAdapter() : MockExchangeAdapter(std::vector<double>{100}) {}
  int sent{0}, cancelled{0};
  bool cancel_ok{true};
  bool TradeOk() const override { return true; }
  bool SubmitOrder(const OrderIntent&) override { ++sent; return true; }
  bool CancelOrder(const std::string&) override { ++cancelled; return cancel_ok; }
};
OrderIntent Intent(std::string id, bool reduce = false) {
  OrderIntent o;
  o.client_order_id = std::move(id); o.symbol = "BTCUSDT";
  o.direction = reduce ? -1 : 1; o.qty = 0.1; o.price = 100;
  o.reduce_only = reduce; o.purpose = reduce ? OrderPurpose::kReduce : OrderPurpose::kEntry;
  return o;
}
void Sender() {
  CountingAdapter adapter;
  AsyncExecutor sender(&adapter);
  sender.Submit(Intent("already_queued"));
  sender.LatchSafetyWithdrawal();
  sender.Submit(Intent("later"));
  sender.Submit(Intent("reduce", true));
  sender.Start(); sender.Stop();
  std::vector<AsyncResult> results;
  sender.PollResults(&results);
  Check(adapter.sent == 1 && results.size() == 3 && !results[0].success &&
        !results[1].success && results[2].success, "queued entries escaped sender latch");
}
void Application() {
  Temp root;
  AppConfig cfg;
  cfg.mode = "replay"; cfg.data_path = root.path.string();
  cfg.self_evolution = Config();
  std::string error;
  {
    BotApplication app(cfg);
    Check(app.wal_.Initialize(&error), error);
    Check(app.evolution_safety_journal_.Open(cfg.data_path, true, &error), error);
    Check(app.self_evolution_.Initialize(0, 10000, {0.4, 0.6}, &error), error);
    auto adapter = std::make_unique<CountingAdapter>();
    auto* observed = adapter.get();
    observed->cancel_ok = false;
    app.adapter_ = std::move(adapter);
    app.executor_ = std::make_unique<AsyncExecutor>(app.adapter_.get(), AsyncExecutor::Mode::kInlineReplay);
    Check(app.EnqueueIntent(Intent("pending")), "pre-latch enqueue");
    app.ProcessAsyncResults();
    // Same app evaluation path as the event's pre-submit observation.
    double price = 100;
    for (int i = 1; i <= 8; ++i) {
      app.has_tick_strategy_signal_ = app.has_latest_mark_price_ = true;
      app.tick_strategy_signal_symbol_ = "BTCUSDT";
      app.tick_trend_notional_usd_ = 80;
      app.latest_mark_price_usd_ = (price *= 0.99);
      app.RunSelfEvolution(i, i);
    }
    Check(app.evolution_safety_withdrawn_ && app.system_.evolution_safety_withdrawn(), "controller-to-app/system disconnected");
    Check(!app.EnqueueIntent(Intent("same_event")), "same event escaped app latch");
    app.ProcessAsyncResults();
    app.CancelEvolutionRiskOrders();
    Check(observed->cancelled == 1 && !OrderManager::IsTerminalState(app.oms_.Find("pending")->state), "cancel failure hidden or retry storm");
    FillEvent late;
    late.fill_id = "late"; late.client_order_id = "pending"; late.symbol = "BTCUSDT";
    late.direction = 1; late.qty = 0.1; late.price = 100;
    app.ProcessFillEvent(late);
    Check(app.system_.account().position_qty("BTCUSDT") == 0.1 && app.evolution_safety_withdrawn_, "late fill lost or unlocked");
    for (auto purpose : {OrderPurpose::kReduce, OrderPurpose::kSl, OrderPurpose::kTp}) {
      auto protective = Intent("protect-" + std::to_string(static_cast<int>(purpose)), true);
      protective.purpose = purpose;
      Check(app.EnqueueIntent(protective), "protective order blocked");
    }
    // New strategy decisions remain reduce-only even if unrelated gates clear.
    app.RefreshReduceOnlyMode();
    MarketEvent event; event.symbol = "BTCUSDT"; event.price = event.mark_price = 100; event.ts_ms = 1000;
    const auto decision = app.system_.Evaluate(event, true);
    Check(decision.risk_adjusted.reduce_only && (!decision.intent || decision.intent->reduce_only), "strategy boundary unlocked");
    app.Shutdown();
  }
  // Actual app startup with new policy / disabled learner still reads latch.
  cfg.self_evolution.enabled = cfg.self_evolution.safety_withdrawal_enabled = false;
  cfg.self_evolution.initial_trend_weight = 0.55;
  {
    BotApplication restarted(cfg);
    Check(restarted.Initialize(), "restart failed");
    Check(restarted.evolution_safety_withdrawn_ && restarted.self_evolution_.safety_withdrawn(), "restart/config unlocked app");
    Check(!restarted.EnqueueIntent(Intent("restart_entry")), "restart admitted entry");
    restarted.Shutdown();
  }
}

void EventBeforeReprice() {
  Temp root;
  AppConfig cfg;
  cfg.data_path = root.path.string(); cfg.mode = "replay";
  cfg.self_evolution = Config();
  cfg.execution_candidate_probe_enabled = true;
  BotApplication app(cfg);
  std::string error;
  Check(app.wal_.Initialize(&error), error);
  Check(app.evolution_safety_journal_.Open(cfg.data_path, true, &error), error);
  Check(app.self_evolution_.Initialize(0, 10000, {0.5, 0.5}, &error), error);
  auto adapter = std::make_unique<CountingAdapter>();
  auto* observed = adapter.get();
  app.adapter_ = std::move(adapter);
  app.executor_ = std::make_unique<AsyncExecutor>(app.adapter_.get(), AsyncExecutor::Mode::kInlineReplay);
  double price = 100;
  for (int i = 1; i <= 7; ++i) {
    app.has_tick_strategy_signal_ = app.has_latest_mark_price_ = true;
    app.tick_strategy_signal_symbol_ = "BTCUSDT";
    app.tick_trend_notional_usd_ = 80;
    app.latest_mark_price_usd_ = (price *= 0.99);
    app.RunSelfEvolution(i, i);
  }
  Check(!app.evolution_safety_withdrawn_, "fixture withdrew too early");
  Check(app.oms_.RegisterIntent(Intent("old_probe")), "probe registration");
  app.oms_.MarkCancelled("old_probe");
  auto& probe = app.active_candidate_probe_by_symbol_["BTCUSDT"];
  probe.symbol = "BTCUSDT"; probe.client_order_id = "old_probe";
  probe.direction = 1; probe.notional_usd = 20; probe.reference_price = price;
  probe.cancel_confirmed = probe.replacement_pending = true;
  app.market_tick_count_ = 7;
  MarketEvent event;
  event.symbol = "BTCUSDT"; event.price = event.mark_price = price * 0.99;
  event.ts_ms = 8;
  app.ProcessMarketEvent(event);
  Check(app.evolution_safety_withdrawn_ && observed->sent == 0,
        "same-event probe repricing outran safety assessment");
  app.Shutdown();
}
}
int main() {
  try {
    Controller(); Persistence(); Sender(); Application(); EventBeforeReprice();
    std::cout << "TEST_ONLY evolution safety controller/app/execution/persistence/restart PASS\n";
    return 0;
  } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
