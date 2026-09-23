// TEST ONLY: real production components, synthetic fills, no exchange/network.
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "core/json_utils.h"
#include "evolution/self_evolution_controller.h"
#include "research/miner.h"
#include "system/trade_system.h"

namespace {
using namespace ai_trade;
constexpr std::int64_t kBar = 300000;
void Check(bool ok, const std::string& error) {
  if (!ok) throw std::runtime_error(error);
}
JsonValue ReadJson(const std::string& path) {
  std::ifstream in(path);
  std::ostringstream text;
  text << in.rdbuf();
  JsonValue value;
  std::string error;
  Check(ParseJson(text.str(), &value, &error), "JSON: " + error);
  return value;
}
std::vector<research::ResearchBar> Bars(const std::string& path) {
  std::vector<research::ResearchBar> bars;
  std::string error;
  Check(research::LoadResearchBarsFromCsv(path, &bars, &error), error);
  Check(bars.size() > 300, "fixture too short");
  for (std::size_t i = 0; i < bars.size(); ++i) {
    const auto& b = bars[i];
    Check(std::isfinite(b.close) && b.close > 0 && b.volume > 0 &&
          b.high >= std::max(b.open, b.close) &&
          b.low <= std::min(b.open, b.close) && b.low > 0 &&
          (i == 0 || b.ts_ms - bars[i-1].ts_ms == kBar),
          "INVALID_SYNTHETIC_TIME_AXIS_OR_OHLC");
  }
  return bars;
}
MarketEvent Event(const research::ResearchBar& b) {
  MarketEvent e;
  e.symbol = "SYNTHBTC";
  e.ts_ms = b.ts_ms;
  e.price = e.mark_price = b.close;
  e.open_price = b.open;
  e.high_price = b.high;
  e.low_price = b.low;
  e.volume = b.volume;
  e.interval_ms = kBar;
  e.completed_bar = true;
  return e;
}
// Separate strict-control probe. No fills or registry writes: production model
// signals feed the production controller's counterfactual virtual ledger.
// Keep the original execution/accounting fixture below unchanged.
void StrictReplay(int argc, char** argv) {
  Check(argc == 9, "strict REPORT MODEL CSV START INTERVAL VERSION TRACE");
  const auto report = ReadJson(argv[2]);
  Check(JsonAsBool(JsonObjectField(&report, "test_only")).value_or(false),
        "TEST_ONLY_REQUIRED");
  const auto version = JsonAsString(JsonObjectField(&report, "model_version"));
  Check(version && *version == argv[7], "CANDIDATE_IDENTITY_MISMATCH");
  const auto bars = Bars(argv[4]);
  const int start = std::stoi(argv[5]), interval = std::stoi(argv[6]);
  Check(start >= 300 && start + interval <= static_cast<int>(bars.size()) &&
        (interval == 12 || interval == 240), "invalid strict fixture window");
  IntegratorShadowConfig sc;
  sc.enabled = sc.require_model_file = true;
  sc.log_model_score = false;
  sc.model_report_path = argv[2];
  sc.model_path = argv[3];
  sc.active_meta_path.clear();
  sc.feature_window_ticks = 300;
  std::string error;
  IntegratorShadow takeover(sc), model(sc);
  Check(!takeover.Initialize(true, &error) && error.find("治理门槛") != std::string::npos,
        "strict synthetic takeover must remain rejected");
  Check(model.Initialize(false, &error), "model load: " + error);
  IntegratorConfig policy;
  policy.enabled = policy.canary_allow_independent_signal = true;
  policy.mode = IntegratorMode::kCanary;
  policy.canary_confidence_threshold = 0.5;
  policy.canary_independent_notional_usd = 80;
  SelfEvolutionConfig ec;
  ec.enabled = ec.use_virtual_pnl = ec.use_counterfactual_search = true;
  ec.counterfactual_require_temporal_holdout = true;
  ec.safety_withdrawal_enabled = std::string(argv[1]) == "safety";
  // Deliberately request the legacy fallback; strict mode must suppress it.
  ec.counterfactual_fallback_to_factor_ic = ec.enable_factor_ic_adaptive_weights = true;
  ec.enable_learnability_gate = true;
  ec.clock_tick_interval_ms = kBar;
  ec.update_interval_ticks = interval;
  ec.min_update_interval_ticks = 72;
  ec.counterfactual_superiority_min_samples_for_update = 10;
  ec.counterfactual_superiority_min_t_stat_for_update = 1.5;
  ec.virtual_cost_bps = 6.5;
  ec.rollback_cooldown_ticks = 288;
  SelfEvolutionController controller(ec);
  Check(controller.Initialize(0, 10000, {0.5, 0.5}, &error, 0,
                              bars[start].ts_ms - kBar), error);
  Signal flat;
  flat.symbol = "SYNTHBTC";
  RegimeState regime;
  regime.bucket = RegimeBucket::kRange;
  double delayed_signal = 0;
  std::ofstream out(argv[8]);
  Check(out.good(), "strict trace output unavailable");
  out << "index,p_up,model_signal,delayed_signal,price,action,action_type,before,weight,best,"
         "train_samples,holdout_samples,learn_samples,learn_t,learn_pass,hold_t,hold_n,"
         "superiority_pass,used_search,strict,fallback,virtual_pnl,objective,rolled_back,safety_withdrawn\n";
  out << std::setprecision(17);
  for (int i = 0; i < static_cast<int>(bars.size()); ++i) {
    const auto event = Event(bars[i]);
    model.OnMarket(event);
    if (i < start) continue;
    const auto inference = model.Infer(flat, regime);
    Check(inference.enabled && inference.model_version == *version, "strict inference identity");
    const auto proposed = EvaluateIntegratorPolicy(policy, inference, flat, 0, false, true);
    const double model_signal = proposed.applied ? proposed.signal.suggested_notional_usd : 0;
    const auto before = controller.current_weights().trend_weight;
    const auto action = controller.OnTick(i - start + 1, 0, RegimeBucket::kRange, 0, 0,
        delayed_signal, 0, event.price, "SYNTHBTC", false, 0, 10000, 6.5, 0.000025, event.ts_ms);
    out << i << ',' << inference.p_up << ',' << model_signal << ',' << delayed_signal
        << ',' << event.price << ',' << (action ? action->reason_code : "none")
        << ',' << (action ? static_cast<int>(action->type) : -1)
        << ',' << before << ',' << controller.current_weights().trend_weight
        << ',' << (action ? action->counterfactual_best_trend_weight : before)
        << ',' << (action ? action->counterfactual_train_samples : 0)
        << ',' << (action ? action->counterfactual_holdout_samples : 0)
        << ',' << (action ? action->learnability_samples : 0)
        << ',' << (action ? action->learnability_t_stat : 0)
        << ',' << (action && action->learnability_gate_passed)
        << ',' << (action ? action->counterfactual_superiority_t_stat : 0)
        << ',' << (action ? action->counterfactual_superiority_samples : 0)
        << ',' << (action && action->counterfactual_superiority_gate_passed)
        << ',' << (action && action->used_counterfactual_search)
        << ',' << (action && action->counterfactual_temporal_holdout_required)
        << ',' << (action && (action->counterfactual_fallback_to_factor_ic_enabled ||
                             action->counterfactual_fallback_to_factor_ic_used))
        << ',' << (action ? action->window_virtual_pnl_usd : 0)
        << ',' << (action ? action->window_objective_score : 0)
        << ',' << (action && action->rolled_back_to_baseline)
        << ',' << controller.safety_withdrawn() << '\n';
    delayed_signal = model_signal;  // Never send the next bar's inference early.
  }
  Check(out.good(), "strict trace write failed");
  std::cout << "TEST_ONLY strict virtual control; no fills/promotion\n";
}
void Replay(int argc, char** argv) {
  Check(argc == 10, "replay REPORT MODEL CSV START FEE_BPS ADAPTIVE VERSION TRACE");
  const auto report = ReadJson(argv[2]);
  Check(JsonAsBool(JsonObjectField(&report, "test_only")).value_or(false),
        "TEST_ONLY_REQUIRED");
  const auto version = JsonAsString(JsonObjectField(&report, "model_version"));
  Check(version.has_value() && *version == argv[8], "CANDIDATE_IDENTITY_MISMATCH");
  const auto bars = Bars(argv[4]);
  const int start = std::stoi(argv[5]);
  const double fee_bps = std::stod(argv[6]);
  const bool adaptive = std::stoi(argv[7]) != 0;
  Check(start >= 300 && start + 4 < static_cast<int>(bars.size()), "invalid start");
  Check(std::isfinite(fee_bps) && fee_bps >= 0, "invalid cost");
  IntegratorShadowConfig sc;
  sc.enabled = true;
  sc.log_model_score = false;
  sc.require_model_file = true;
  sc.model_report_path = argv[2];
  sc.model_path = argv[3];
  sc.active_meta_path.clear();
  sc.feature_window_ticks = 300;
  std::string error;
  // Synthetic reports must never pass the real takeover gate.
  IntegratorShadow strict(sc);
  Check(!strict.Initialize(true, &error), "synthetic report granted takeover");
  Check(error.find("治理门槛") != std::string::npos,
        "strict rejection must be a governance failure, not missing runtime");
  IntegratorShadow model(sc);
  Check(model.Initialize(false, &error), "model load: " + error);
  IntegratorConfig policy_config;
  policy_config.enabled = true;
  policy_config.mode = IntegratorMode::kCanary;
  policy_config.canary_confidence_threshold = 0.50;
  policy_config.canary_allow_independent_signal = true;
  policy_config.canary_independent_notional_usd = 80;
  AccountState account;
  RiskEngine risk(80);
  ExecutionEngine execution(80);
  SelfEvolutionConfig ec;
  ec.enabled = adaptive;
  // Event-time clock: assessment every hour, ordinary updates at most every 6h.
  ec.clock_tick_interval_ms = kBar;
  ec.update_interval_ticks = 12;
  ec.min_update_interval_ticks = 72;
  ec.rollback_degrade_windows = 2;
  ec.rollback_cooldown_ticks = 288;  // 24h on synthetic clock.
  // Fees/slippage/funding are already debited; retain a small, declared
  // additional churn penalty in this bounded synthetic controller fixture.
  ec.objective_gamma_notional_churn = 0.00001;
  SelfEvolutionController evolution(ec);
  Check(evolution.Initialize(0, 10000, {0.5, 0.5}, &error, 0, bars[start].ts_ms - kBar), error);
  Signal flat;
  flat.symbol = "SYNTHBTC";
  RegimeState regime;
  regime.bucket = RegimeBucket::kRange;
  int pending_at = -1, close_at = -1, fills = 0, episodes = 0, previous_fills = 0;
  int pending_direction = 0;
  double pending_notional = 0;
  std::int64_t last_update_tick = 0, cooldown_until = 0;
  std::ofstream out(argv[9]);
  Check(out.good(), "trace output unavailable");
  out << "index,p_up,applied,direction,episodes,fills,net,fee,funding,weight,action,cash,qty\n";
  out << std::setprecision(17);
  auto fill = [&](const OrderIntent& intent, double close) {
    FillEvent f;
    f.fill_id = "synthetic-" + std::to_string(++fills);
    f.client_order_id = f.fill_id;
    f.symbol = "SYNTHBTC";
    f.direction = intent.direction;
    f.qty = intent.qty;
    f.price = close * (1 + f.direction * 0.0001);  // adverse 1bp/fill.
    f.fee = std::abs(f.qty * f.price) * fee_bps / 10000;
    account.ApplyFill(f);
  };
  for (int i = 0; i < static_cast<int>(bars.size()); ++i) {
    const auto e = Event(bars[i]);
    account.OnMarket(e);
    if (i == close_at) {
      // Fixed synthetic settlement, not a claim about exchange funding.
      account.ApplyFunding("SYNTHBTC", 0.000025, true);
      TargetPosition zero{"SYNTHBTC", 0};
      auto intent = execution.BuildIntent(risk.Apply(zero, true, account.drawdown_pct()),
                                          account.current_notional_usd(), e.price);
      Check(intent.has_value() && intent->reduce_only, "terminal close missing");
      fill(*intent, e.price);
      close_at = -1;
      ++episodes;
    }
    if (i == pending_at) {
      // The production policy already returns a SIGNED notional.
      TargetPosition target{"SYNTHBTC", pending_notional};
      auto adjusted = risk.Apply(target, true, account.drawdown_pct());
      auto intent = execution.BuildIntent(adjusted, account.current_notional_usd(), e.price);
      Check(intent.has_value() && !intent->reduce_only, "entry missing");
      Check(intent->direction == pending_direction, "policy/intent direction mismatch");
      fill(*intent, e.price);
      pending_at = -1;
      close_at = i + 1;
    }
    model.OnMarket(e);
    if (i < start) continue;
    const std::int64_t tick = i - start + 1;
    const auto action = evolution.OnTick(tick, account.cumulative_realized_net_pnl_usd(),
        RegimeBucket::kRange, account.drawdown_pct(), account.current_notional_usd(),
        0, 0, e.price, "SYNTHBTC", false, fills - previous_fills, account.equity_usd(), 0, 0, e.ts_ms);
    if (adaptive) {
      Check(evolution.clock_tick() == tick && evolution.next_eval_tick() == (tick / 12 + 1) * 12,
            "hourly event-time assessment schedule violated");
      Check(action.has_value() == (tick % 12 == 0), "hourly active-window assessment missing/duplicated");
    }
    previous_fills = fills;
    std::string action_name = "none";
    if (action) {
      action_name = action->reason_code;
      Check(std::abs(action->trend_weight_after - action->trend_weight_before) <= 0.050000001 ||
            action->type == SelfEvolutionActionType::kRolledBack, "weight step violated");
      if (action->type == SelfEvolutionActionType::kUpdated) {
        Check(tick >= cooldown_until && tick - last_update_tick >= 72,
              "cooldown/minimum interval violated");
        last_update_tick = tick;
      }
      if (action->type == SelfEvolutionActionType::kRolledBack) {
        Check(action->rolled_back_to_baseline &&
              std::abs(action->trend_weight_after - 0.5) < 1e-10,
              "rollback failed to restore baseline");
        cooldown_until = tick + 288;
        last_update_tick = tick;
      }
    }
    // A disabled controller intentionally does not initialize its state. The
    // frozen arm must use its declared fixed weights, not inactive internals.
    const auto weights = adaptive ? evolution.current_weights() : EvolutionWeights{0.5, 0.5};
    Check(weights.trend_weight >= 0.4 - 1e-9 && weights.trend_weight <= 0.6 + 1e-9 &&
          std::abs(weights.trend_weight + weights.defensive_weight - 1) < 1e-9, "weight bounds violated");
    const auto inference = model.Infer(flat, regime);
    Check(inference.enabled && inference.model_version == *version, "inference unavailable/identity");
    const bool available = pending_at < 0 && close_at < 0;
    auto policy = EvaluateIntegratorPolicy(policy_config, inference, flat,
        account.current_notional_usd(), pending_at >= 0, true);
    // Each scored decision independently proves risk can veto it while flat.
    TargetPosition probe{"SYNTHBTC", 80};
    RiskEngine disconnected(80), missing_risk(80), fuse(80);
    Check(!execution.BuildIntent(disconnected.Apply(probe, false, 0), 0, e.price),
          "disconnected entry escaped risk");
    Check(!execution.BuildIntent(missing_risk.Apply(probe, true, 0, 0), 0, e.price),
          "unknown liquidation risk allowed entry");
    Check(!execution.BuildIntent(fuse.Apply(probe, true, 0.20), 0, e.price),
          "fuse allowed entry");
    bool applied = false;
    if (available && policy.applied && i + 2 < static_cast<int>(bars.size())) {
      pending_at = i + 1;  // Never fill on the feature bar.
      pending_direction = policy.signal.direction;
      pending_notional = policy.signal.suggested_notional_usd * weights.trend_weight;
      applied = true;
    }
    out << i << ',' << inference.p_up << ',' << applied << ',' << policy.signal.direction
        << ',' << episodes << ',' << fills << ',' << account.cumulative_realized_net_pnl_usd()
        << ',' << account.cumulative_fee_usd() << ',' << account.cumulative_funding_paid_usd()
        << ',' << weights.trend_weight << ',' << action_name << ',' << account.cash_usd()
        << ',' << account.position_qty("SYNTHBTC") << '\n';
  }
  Check(pending_at < 0 && close_at < 0 && std::abs(account.position_qty("SYNTHBTC")) < 1e-10,
        "terminal position not flat");
  Check(std::abs(account.cash_usd() - 10000 - account.cumulative_realized_net_pnl_usd()) < 1e-7,
        "cash economics mismatch");
  auto stale = Event(bars.back());
  stale.ts_ms += 3 * kBar;
  stale.open_price = stale.high_price = stale.low_price = 0;
  model.OnMarket(stale);
  Check(!model.Infer(flat, regime).enabled, "stale data still eligible");
  std::cout << "TEST_ONLY replay complete; production_authority=false\n";
}
}  // namespace

int main(int argc, char** argv) {
  try {
    Check(argc >= 2, "mode required");
    if (std::string(argv[1]) == "mine") {
      Check(argc == 4, "mine CSV REPORT");
      research::MinerConfig c;
      c.predict_horizon_bars = c.execution_latency_bars = 1;
      c.generations = 2;
      c.population_size = 24;
      c.top_k = 3;
      std::string error;
      Check(research::SaveMinerReport(research::Miner().Run(Bars(argv[2]), c), argv[3], &error), error);
    } else if (std::string(argv[1]) == "strict" || std::string(argv[1]) == "safety") {
      StrictReplay(argc, argv);
    } else {
      Check(std::string(argv[1]) == "replay", "unknown mode");
      Replay(argc, argv);
    }
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "OFFLINE_LEARNING_DRIVER_FAIL: " << e.what() << '\n';
    return 1;
  }
}
