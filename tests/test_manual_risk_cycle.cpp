#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <random>
#include <stdexcept>
#include <vector>

#include "risk/manual_risk_cycle.h"
#include "system/trade_system.h"

namespace {
using namespace ai_trade;
constexpr std::int64_t kStart = 1704067200000LL;
constexpr auto kDay = ManualRiskCycle::kDayMs;
void Check(bool ok, const char* error) { if (!ok) throw std::runtime_error(error); }
bool Near(double a, double b) { return std::fabs(a - b) < 1e-8; }
template <class F> void Throws(F action, const char* error) {
  bool threw = false;
  try { action(); } catch (const std::exception&) { threw = true; }
  Check(threw, error);
}
struct Directory {
  std::filesystem::path path;
  Directory() {
    std::random_device random;
    for (int i = 0; i < 10; ++i) {
      path = std::filesystem::temp_directory_path() /
          ("ai-trade-manual-risk-" + std::to_string(random()) + "-" + std::to_string(random()));
      if (std::filesystem::create_directory(path)) return;
    }
    throw std::runtime_error("cannot create test directory");
  }
  ~Directory() { std::error_code error; std::filesystem::remove_all(path, error); }
};
MarketEvent Market(std::int64_t ts, double price = 100) {
  MarketEvent event;
  event.ts_ms = ts;
  event.price = event.mark_price = price;
  event.interval_ms = 300000;
  event.funding_rate_per_interval = 0;
  return event;
}
FillEvent Fill(double qty, int direction, double price, double fee = 0) {
  FillEvent fill;
  fill.qty = qty;
  fill.direction = direction;
  fill.price = price;
  fill.fee = fee;
  return fill;
}
void Lose(AccountState& account, double percent) {
  account.ApplyFill(Fill(100, 1, 100));
  account.OnMarket(Market(kStart, 100 - percent));
  account.ApplyFill(Fill(100, -1, 100 - percent));
}
ManualRiskApproval Approval(const ManualRiskCycle& cycle, const char* id, int stage = 1) {
  ManualRiskApproval a;
  a.approval_id = id;
  a.review_reference = "synthetic-review-only";
  a.expected_cycle = cycle.state().cycle;
  a.observed_at_ms = cycle.state().last_ts_ms;
  a.next_stage = stage;
  a.max_gross_notional_usd = 2000;
  a.max_loss_usd = 100;
  return a;
}
void CheckPermanentLatch(double loss) {
  AccountState account;
  ManualRiskCycle risk(3000);
  risk.Observe(kStart, account, true, false);
  Lose(account, loss);
  risk.Observe(kStart + 300000, account, true, false);
  for (int i = 1; i <= 2016; ++i)
    risk.Observe(kStart + (i + 1LL) * 300000, account, true, false);
  Check(risk.state().latched && risk.gross_cap_usd() == 0 && risk.state().cycle == 0,
        "flat waiting silently rearmed a risk cycle");
  Check(risk.state().latch_mode == (loss > 20 ? RiskMode::kFuse : RiskMode::kCooldown),
        "wrong latch type");
  Check(Near(risk.state().lifetime_max_drawdown, loss / 100), "lifetime DD was lost");
  std::string error;
  Check(!risk.Approve(Approval(risk, "no-journal"), &error), "approval without durable audit accepted");
}
void CheckApprovalAndStages() {
  std::vector<std::string> records;
  ManualRiskCycle risk(3000, {}, [&](const auto& line) { records.push_back(line); return true; });
  AccountState account;
  Lose(account, 21);
  risk.Observe(kStart, account, true, false);
  std::string error;
  auto reject = [&](ManualRiskApproval a) {
    const auto cycle = risk.state().cycle;
    const auto stage = risk.state().stage;
    const auto sequence = risk.state().sequence;
    Check(!risk.Approve(a, &error) && !error.empty(), "invalid manual approval accepted");
    Check(risk.state().cycle == cycle && risk.state().stage == stage &&
          risk.state().sequence == sequence, "rejected approval changed risk state");
  };
  risk.Observe(kStart + kDay - 1, account, true, false);
  reject(Approval(risk, "too-early"));
  risk.Observe(kStart + kDay, account, true, false);
  auto a = Approval(risk, "manual-1");
  auto bad = a; bad.expected_cycle = 7; reject(bad);
  bad = a; --bad.observed_at_ms; reject(bad);
  bad = a; ++bad.observed_at_ms; reject(bad);
  bad = a; bad.approval_id.clear(); reject(bad);
  bad = a; bad.review_reference.clear(); reject(bad);
  bad = a; bad.review_reference = "forged\nAPPROVE"; reject(bad);
  bad = a; bad.max_gross_notional_usd = 3001; reject(bad);
  bad = a; bad.max_loss_usd = 159; reject(bad); // 2% of 7900 = 158
  bad = a; bad.max_loss_usd = std::numeric_limits<double>::quiet_NaN(); reject(bad);
  bad = a; bad.max_loss_usd = 0; reject(bad);
  const auto peak = account.peak_equity_usd();
  const auto net = account.cumulative_realized_net_pnl_usd();
  const auto dd = account.drawdown_pct();
  Check(risk.Approve(a, &error), "valid initial manual approval rejected");
  Check(risk.state().cycle == 1 && risk.state().stage == 1 && !risk.state().latched &&
        risk.gross_cap_usd() == 500 && risk.control_drawdown() == 0,
        "new risk cycle did not enter 25pct stage");
  Check(account.peak_equity_usd() == peak && account.cumulative_realized_net_pnl_usd() == net &&
        account.drawdown_pct() == dd && Near(risk.state().lifetime_max_drawdown, .21) &&
        risk.state().lifetime_limit_breached, "manual cycle hid historical losses or risk failure");
  reject(a);
  reject(Approval(risk, "duplicate-new-cycle"));
  risk.Observe(kStart + 2 * kDay, account, true, false);
  Check(risk.state().stage == 1 && risk.gross_cap_usd() == 500, "stage advanced automatically");
  reject(Approval(risk, "skip-stage", 3));
  bad = Approval(risk, "changed-budget", 2); bad.max_loss_usd = 120; reject(bad);
  bad = Approval(risk, "manual-1", 2); reject(bad);
  Check(risk.Approve(Approval(risk, "manual-2", 2), &error) && risk.gross_cap_usd() == 1000,
        "manual 50pct stage failed");
  risk.Observe(kStart + 3 * kDay - 1, account, true, false);
  reject(Approval(risk, "early-full", 3));
  risk.Observe(kStart + 3 * kDay, account, true, false);
  Check(risk.Approve(Approval(risk, "manual-3", 3), &error) && risk.gross_cap_usd() == 2000,
        "manual full approved cap failed");
  const auto expires = risk.state().expires_at_ms;
  Check(expires == kStart + 8 * kDay, "stage advance extended total cycle duration");
  risk.Observe(expires - 1, account, true, false);
  Check(!risk.state().latched, "cycle expired before boundary");
  risk.Observe(expires, account, true, false);
  Check(risk.state().latched && risk.state().reason == "CYCLE_EXPIRED", "expiry did not re-latch");
  reject(Approval(risk, "instant-renewal"));
  Check(records.size() > 10 && records.back().find("CYCLE_EXPIRED") != std::string::npos,
        "cycle transitions lack audit records");
}
void CheckHealthAndBudget() {
  ManualRiskCycle risk(3000, {}, [](const auto&) { return true; });
  AccountState account;
  Lose(account, 13);
  risk.Observe(kStart, account, true, false);
  risk.Observe(kStart + kDay, account, true, true);
  std::string error;
  Check(!risk.Approve(Approval(risk, "pending"), &error), "pending order ignored");
  risk.Observe(kStart + kDay, account, false, false);
  Check(!risk.Approve(Approval(risk, "unhealthy"), &error), "bad channel ignored");
  account.ApplyFill(Fill(1, 1, 100));
  risk.Observe(kStart + kDay, account, true, false);
  Check(!risk.Approve(Approval(risk, "nonflat"), &error), "open position ignored");
  account.ApplyFill(Fill(1, -1, 100));
  risk.Observe(kStart + kDay, account, true, false);
  Check(!risk.Approve(Approval(risk, "cooldown-reset"), &error), "flat cooldown was not restarted");
  risk.Observe(kStart + 2 * kDay, account, true, false);
  Check(risk.Approve(Approval(risk, "budget-cycle"), &error), "healthy recovery rejected");
  account.ApplyFill(Fill(1, 1, 100, 1));
  account.ApplyFunding("BTCUSDT", .01); // another synthetic unit of cost
  risk.Observe(kStart + 2 * kDay + 1, account, true, false);
  Check(Near(risk.state().cumulative_fees_usd, 1) && Near(risk.state().cumulative_funding_usd, 1),
        "fees/funding not retained across cycles");
  account.OnMarket(Market(kStart, 3)); // net loss 99, below 100
  risk.Observe(kStart + 2 * kDay + 2, account, true, false);
  Check(!risk.state().latched, "loss budget fired too early");
  account.OnMarket(Market(kStart, 2));
  risk.Observe(kStart + 2 * kDay + 3, account, true, false);
  Check(risk.state().latched && risk.state().reason == "CYCLE_LOSS_BUDGET" &&
        risk.gross_cap_usd() == 0, "cost-inclusive loss budget did not re-latch");
  account.ApplyFill(Fill(1, -1, 2));
  risk.Observe(kStart + 2 * kDay + 4, account, true, false);
  Check(!risk.Approve(Approval(risk, "budget-cycle"), &error), "old approval reopened failed cycle");
  risk.Observe(kStart + 3 * kDay + 4, account, true, false);
  Check(risk.Approve(Approval(risk, "separate-new-review"), &error) && risk.state().cycle == 2,
        "second explicit cycle failed");
  Check(Near(account.cumulative_realized_net_pnl_usd(), -1400), "new cycle erased old net loss");
}
void CheckDurabilityAndFailure() {
  Directory directory;
  const auto path = (directory.path / "risk.journal").string();
  AccountState account;
  Lose(account, 21);
  std::string error;
  std::uintmax_t size = 0;
  {
    ManualRiskCycle risk(3000);
    Check(risk.OpenNewJournal(path, &error), "journal creation failed");
    risk.Observe(kStart, account, true, false);
    risk.Observe(kStart + kDay, account, true, false);
    Check(risk.Approve(Approval(risk, "durable-approval"), &error), "durable approval failed");
    size = std::filesystem::file_size(path);
    std::ifstream input(path);
    const std::string text((std::istreambuf_iterator<char>(input)), {});
    Check(text.find("APPROVE\t") != std::string::npos && text.find("durable-approval") != std::string::npos,
          "manual approval not durable before acceptance");
  }
  ManualRiskCycle restarted(3000);
  Check(!restarted.OpenNewJournal(path, &error) && std::filesystem::file_size(path) == size,
        "restart erased or ignored prior journal");
  Check(restarted.gross_cap_usd() == 0, "journal rejection left risk enabled");
  bool fail = false;
  ManualRiskCycle broken(3000, {}, [&](const auto&) { return !fail; });
  broken.Observe(kStart, account, true, false);
  broken.Observe(kStart + kDay, account, true, false);
  fail = true;
  Throws([&] { broken.Approve(Approval(broken, "failed-write"), &error); }, "audit failure accepted approval");
  Check(broken.state().cycle == 0 && broken.state().latched && broken.gross_cap_usd() == 0,
        "failed write committed cycle release");
  fail = false;
  Check(!broken.Approve(Approval(broken, "later-write"), &error), "journal error auto-recovered");
  ManualRiskCycle invalid(3000);
  invalid.Observe(kStart, account, true, false);
  Throws([&] { invalid.Observe(kStart - 1, account, true, false); }, "time reversal accepted");
  Check(invalid.gross_cap_usd() == 0, "time error did not fail closed");
}

void CheckProfitDoesNotEraseRisk() {
  ManualRiskCycle risk(3000, {}, [](const auto&) { return true; });
  AccountState account;
  Lose(account, 21);
  risk.Observe(kStart, account, true, false);
  risk.Observe(kStart + kDay, account, true, false);
  std::string error;
  Check(risk.Approve(Approval(risk, "profit-path"), &error), "profit path approval failed");
  account.ApplyFill(Fill(10, 1, 100));
  account.OnMarket(Market(kStart, 400)); // equity 10900, above the old lifetime peak
  risk.Observe(kStart + kDay + 1, account, true, false);
  Check(risk.state().lifetime_drawdown == 0 && Near(risk.state().lifetime_max_drawdown, .21) &&
        risk.state().lifetime_limit_breached, "later gains erased prior risk breach");
  account.OnMarket(Market(kStart, 390)); // 100 off new peak, still profitable in cycle
  risk.Observe(kStart + kDay + 2, account, true, false);
  Check(risk.state().latched && risk.state().reason == "CYCLE_LOSS_BUDGET",
        "recovery budget ignored drawdown from profits");
  ManualRiskCycle jump(3000, {}, [](const auto&) { return true; });
  AccountState second;
  Lose(second, 13);
  jump.Observe(kStart, second, true, false);
  jump.Observe(kStart + kDay, second, true, false);
  Check(jump.Approve(Approval(jump, "jump-path"), &error), "jump approval failed");
  Lose(second, 20); // large synthetic gap: no claim the budget caps actual loss
  jump.Observe(kStart + kDay + 1, second, true, false);
  Check(jump.state().latched && jump.state().latch_mode == RiskMode::kFuse &&
        jump.state().lifetime_limit_breached, "new-cycle 20pct fuse was bypassed");
}
void CheckOrchestrator(const char* path) {
  AppConfig config;
  std::string error;
  Check(LoadAppConfigFromYaml(path, &config, &error), "MVP config failed");
  Directory directory;
  TradeSystem system(config);
  Check(system.OpenMvpRiskJournal((directory.path / "system.journal").string(), &error), "bind journal failed");
  Throws([&] { system.OnPrice(100); }, "legacy immediate fill helper bypassed MVP contract");
  system.OnFill(Fill(100, 1, 100));
  system.OnMarketSnapshot(Market(kStart, 79));
  system.OnFill(Fill(100, -1, 79));
  system.Evaluate(Market(kStart, 79));
  system.Evaluate(Market(kStart + 300000, 79));
  // Observe-only events keep risk time advancing without fabricating alpha bars.
  system.OnMarketSnapshot(Market(kStart + kDay, 79));
  ManualRiskApproval a{"system-approval", "synthetic-review", 0, kStart + kDay, 1, 2000, 100};
  system.ForceReduceOnly(true);
  Check(!system.ApproveMvpRiskCycle(a, &error), "forced reduce-only bypassed by approval");
  system.ForceReduceOnly(false);
  system.SetMvpPendingOrders(true);
  Check(!system.ApproveMvpRiskCycle(a, &error), "global pending bypassed by approval");
  system.SetMvpPendingOrders(false);
  system.OnMarketSnapshot(Market(kStart + 2 * kDay, 79));
  a.observed_at_ms = kStart + 2 * kDay;
  Check(system.ApproveMvpRiskCycle(a, &error), "orchestrator manual approval failed");
  Check(system.risk_mode() == RiskMode::kNormal && Near(system.account().drawdown_pct(), .21) &&
        Near(system.account().cumulative_realized_net_pnl_usd(), -2100), "release reset lifetime account risk");
  // Restart no clock: use execution-only observations until the previously
  // observed tick clock is intentionally not advanced; direct risk/ledger
  // checks prove permission and capital semantics independently of alpha.
  auto observation = Market(kStart + 2 * kDay + 1, 79);
  observation.execution_only = true;
  const auto waiting = system.Evaluate(observation);
  Check(!waiting.intent && !waiting.base_signal.new_decision, "cached pre-approval alpha executed");
  system.OnFill(Fill(1, 1, 79));
  observation.ts_ms++;
  const auto unknown = system.Evaluate(observation);
  Check(unknown.risk_adjusted.reduce_only, "new cycle bypassed unknown liquidation data");
  system.ApplyFunding("BTCUSDT", 2.0); // synthetic loss, not a market estimate
  observation.ts_ms++;
  const auto latched = system.Evaluate(observation);
  Check(latched.risk_adjusted.reduce_only && latched.risk_adjusted.adjusted_notional_usd == 0 &&
        system.mvp_risk_state().latched, "funding loss did not force a new latch");
  Throws([&] { system.SyncAccountFromRemotePositions({}); }, "account rebase erased cycle history");
  config.closed_bar_mvp = false;
  TradeSystem legacy(config);
  Check(!legacy.ApproveMvpRiskCycle(a, &error), "manual API enabled legacy/live behavior");
}

void CheckExecutableRecovery(const char* path) {
  AppConfig config;
  std::string error;
  Check(LoadAppConfigFromYaml(path, &config, &error), "MVP recovery config failed");
  Directory directory;
  TradeSystem system(config);
  Check(system.OpenMvpRiskJournal((directory.path / "recovery.journal").string(), &error),
        "executable recovery journal failed");
  system.OnFill(Fill(100, 1, 100));
  system.OnFill(Fill(100, -1, 79));
  auto bar = [](int i) {
    auto event = Market(kStart + (i + 1LL) * 300000, 500 + i);
    event.completed_bar = true;
    event.open_price = event.price - .5;
    event.high_price = event.price;
    event.low_price = event.open_price;
    return event;
  };
  for (int i = 0; i <= 288; ++i) {
    const auto held = system.Evaluate(bar(i));
    Check(held.risk_adjusted.reduce_only && !held.intent,
          "pre-approval alpha escaped latch");
  }
  ManualRiskApproval a{"real-decision-fixture", "synthetic-review", 0,
                       bar(288).ts_ms, 1, 2000, 100};
  Check(system.ApproveMvpRiskCycle(a, &error), "executable approval failed");
  const auto recovered = system.Evaluate(bar(289));
  Check(recovered.intent && recovered.intent->qty > 0 && !recovered.intent->reduce_only &&
        std::fabs(recovered.risk_adjusted.adjusted_notional_usd) <= 500 &&
        std::fabs(recovered.base_signal.suggested_notional_usd) > 500,
        "manual recovery did not actually enable a capped nonzero decision");
  system.SetMvpPendingOrders(true); // pending order on ANY symbol reserves execution
  const auto waiting = system.Evaluate(bar(290));
  Check(!waiting.intent && waiting.risk_adjusted.reduce_only,
        "other-symbol pending order allowed double spending risk budget");
  system.SetMvpPendingOrders(false);
  const auto resumed = system.Evaluate(bar(291));
  Check(resumed.intent && !resumed.intent->reduce_only, "settled pending state never released execution");
  // Risk accounting never interprets the manual release as historical PASS.
  Check(system.mvp_risk_state().lifetime_limit_breached &&
        Near(system.mvp_risk_state().lifetime_max_drawdown, .21),
        "executable recovery hid the historical qualification failure");
}
} // namespace
int main(int argc, char** argv) {
  try {
    Check(argc == 2, "expected MVP config path");
    CheckPermanentLatch(13);
    CheckPermanentLatch(21);
    CheckApprovalAndStages();
    CheckHealthAndBudget();
    CheckDurabilityAndFailure();
    CheckProfitDoesNotEraseRisk();
    CheckOrchestrator(argv[1]);
    CheckExecutableRecovery(argv[1]);
    std::cout << "MANUAL_RISK_CYCLE_PASS (synthetic offline only; lifetime losses retained)\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "MANUAL_RISK_CYCLE_FAILURE: " << e.what() << '\n';
    return 1;
  }
}
