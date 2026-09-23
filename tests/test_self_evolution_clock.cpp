// Deterministic scheduling tests: synthetic equity, no account or exchange.
#include <cmath>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include "evolution/self_evolution_controller.h"

namespace {
using namespace ai_trade;
constexpr std::int64_t kOrigin = 1704067200000;
constexpr int kBarMs = 300000;
void Check(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
SelfEvolutionConfig Config(bool timed = false) {
  SelfEvolutionConfig c;
  c.enabled = true;
  c.clock_tick_interval_ms = timed ? kBarMs : 0;
  c.update_interval_ticks = 12;
  c.min_update_interval_ticks = 72;
  c.rollback_cooldown_ticks = 288;
  c.objective_beta_drawdown = c.objective_gamma_notional_churn = 0;
  return c;
}
void Init(SelfEvolutionController& c, std::int64_t origin = 0) {
  std::string error;
  Check(c.Initialize(0, 10000, {0.5, 0.5}, &error, 0, origin), error.c_str());
}
auto Step(SelfEvolutionController& c, int tick, double pnl,
          RegimeBucket bucket = RegimeBucket::kRange, std::int64_t time = 0) {
  return c.OnTick(tick, pnl, bucket, 0, 10, 0, 0, 100, "SYNTH", false, 0, 10000, 0, 0, time);
}
void CadenceAndNoop() {
  SelfEvolutionController c(Config()); Init(c);
  Check(c.next_eval_tick() == 12 && c.next_update_tick() == 72, "startup clocks coupled");
  int evaluations = 0, updates = 0;
  for (int tick = 1; tick <= 216; ++tick) {
    const auto a = Step(c, tick, tick);
    Check(a.has_value() == (tick % 12 == 0), "missing/extra hourly assessment");
    if (!a) continue;
    ++evaluations;
    if (tick == 72 || tick == 144) {
      Check(a->type == SelfEvolutionActionType::kUpdated, "eligible update missing");
      ++updates;
    } else if (tick < 216) {
      Check(a->reason_code == "EVOLUTION_UPDATE_INTERVAL_PENDING", "early update allowed");
      Check(a->update_wait_remaining_ticks == c.next_update_tick() - tick, "wait telemetry incorrect");
    } else {
      Check(a->reason_code == "EVOLUTION_WEIGHT_NOOP" && c.next_update_tick() == 216,
            "no-op consumed another update interval");
    }
    Check(a->trend_weight_after <= 0.600000001 &&
          std::abs(a->trend_weight_after - a->trend_weight_before) <= 0.050000001,
          "step/weight bounds changed");
  }
  Check(evaluations == 18 && updates == 2, "evaluation/update counters confused");
}
void GlobalRateLimitAndRollback() {
  SelfEvolutionController c(Config()); Init(c);
  Check(Step(c, 72, 10, RegimeBucket::kTrend)->type == SelfEvolutionActionType::kUpdated,
        "first trend update missing");
  Check(Step(c, 84, 20, RegimeBucket::kRange)->reason_code == "EVOLUTION_UPDATE_INTERVAL_PENDING",
        "bucket rotation bypassed global update limit");
  Check(Step(c, 96, 15, RegimeBucket::kTrend)->reason_code == "EVOLUTION_UPDATE_INTERVAL_PENDING",
        "first loss should be assessed without ordinary update");
  const auto rollback = Step(c, 108, 10, RegimeBucket::kTrend);
  Check(rollback && rollback->type == SelfEvolutionActionType::kRolledBack &&
        std::abs(rollback->trend_weight_after - 0.5) < 1e-9 &&
        c.cooldown_until_tick() == 396 && c.next_update_tick() == 180,
        "rollback delayed by update limiter or failed to re-arm cooldown");
  for (int tick = 120; tick < 396; tick += 12) {
    const auto a = Step(c, tick, tick, RegimeBucket::kRange);
    Check(a && a->reason_code == "EVOLUTION_COOLDOWN_ACTIVE", "cooldown stopped evaluations/allowed update");
  }
  Check(Step(c, 396, 500)->type == SelfEvolutionActionType::kUpdated,
        "cooldown exact boundary did not release update");
}
void EventTimeNotMessageCount() {
  SelfEvolutionController c(Config(true)); Init(c, kOrigin);
  // Many symbols/messages at one event time cannot fast-forward six hours.
  for (int i = 1; i <= 5000; ++i) {
    Check(!Step(c, i, 1, i % 2 ? RegimeBucket::kRange : RegimeBucket::kTrend, kOrigin),
          "same-time burst accelerated clock");
  }
  Check(c.clock_tick() == 0 && c.next_eval_tick() == 12, "message count leaked into event clock");
  Check(!Step(c, 5001, 2, RegimeBucket::kRange, 0) &&
        !Step(c, 5002, 2, RegimeBucket::kRange, kOrigin - 1) &&
        c.rejected_event_time_count() == 2 && c.clock_tick() == 0,
        "invalid event time advanced controller");
  const auto hour = Step(c, 5003, 3, RegimeBucket::kRange, kOrigin + 3600000);
  Check(hour && hour->tick == 12 && hour->clock_tick_interval_ms == kBarMs &&
        hour->reason_code == "EVOLUTION_UPDATE_INTERVAL_PENDING", "one-hour event time not assessed");
  const auto six = Step(c, 5004, 9, RegimeBucket::kRange, kOrigin + 21600000);
  Check(six && six->type == SelfEvolutionActionType::kUpdated && six->tick == 72,
        "six-hour event time not update-eligible");
  Check(!Step(c, 5005, 9, RegimeBucket::kRange, kOrigin + 21600000), "same time evaluated twice");
  Check(!Step(c, 5006, 8, RegimeBucket::kRange, kOrigin + 3600000) && c.clock_tick() == 72,
        "late event reversed clock");
  const auto jump = Step(c, 5007, 20, RegimeBucket::kRange, kOrigin + 1000LL * kBarMs);
  Check(jump && jump->tick == 1000 && c.next_eval_tick() == 1012 &&
        !Step(c, 5008, 20, RegimeBucket::kRange, kOrigin + 1000LL * kBarMs),
        "time jump manufactured catch-up windows");
  SelfEvolutionController unanchored(Config(true)); Init(unanchored);
  Check(!Step(unanchored, 9000, 0, RegimeBucket::kRange, kOrigin) && unanchored.clock_tick() == 0,
        "first event did not anchor clock");
  Check(Step(unanchored, 9001, 10, RegimeBucket::kRange, kOrigin + 3600000)->tick == 12,
        "anchored one-hour cadence wrong");
}
void ExistingEvidenceGatesRemainClosed() {
  auto config = Config();
  config.enable_learnability_gate = true;
  config.learnability_min_samples = 100;
  SelfEvolutionController c(config); Init(c);
  const auto a = Step(c, 72, 10);
  Check(a && a->reason_code == "EVOLUTION_LEARNABILITY_INSUFFICIENT_SAMPLES" &&
        c.next_update_tick() == 72, "clock bypassed evidence gate or consumed update on rejection");
  config = Config();
  config.use_virtual_pnl = config.use_counterfactual_search = true;
  config.counterfactual_require_temporal_holdout = true;
  SelfEvolutionController strict(config); Init(strict);
  const auto insufficient = Step(strict, 72, -20);
  Check(insufficient && insufficient->reason_code == "EVOLUTION_COUNTERFACTUAL_HOLDOUT_INSUFFICIENT",
        "clock bypassed strict temporal holdout");
}
void ConfigParsing() {
  const auto path = std::filesystem::temp_directory_path() /
      ("ai-trade-clock-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()) + ".yaml");
  for (int interval : {0, kBarMs, -1}) {
    { std::ofstream out(path); out << "self_evolution:\n  clock_tick_interval_ms: " << interval << '\n'; }
    AppConfig config; std::string error;
    const bool loaded = LoadAppConfigFromYaml(path.string(), &config, &error);
    Check(loaded == (interval >= 0), "clock YAML validation mismatch");
    if (loaded) Check(config.self_evolution.clock_tick_interval_ms == interval, "clock YAML value lost");
    else Check(error.find("clock_tick_interval_ms") != std::string::npos, "wrong YAML rejection reason");
  }
  std::filesystem::remove(path);
}
}  // namespace
int main() {
  try {
    CadenceAndNoop(); GlobalRateLimitAndRollback(); EventTimeNotMessageCount();
    ExistingEvidenceGatesRemainClosed(); ConfigParsing();
    std::cout << "EVOLUTION_CLOCK_PASS: independent evaluation/update/event-time/cooldown; TEST_ONLY\n";
    return 0;
  } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
