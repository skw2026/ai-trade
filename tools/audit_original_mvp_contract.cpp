// One-shot diagnostic for the 2026-09-21 investment decision, NOT a strategy
// experiment or a regression that blesses the observed contract gaps.
// Only synthetic prices and in-memory accounting are used. No network, model,
// credential file, production config mutation or live order is permitted.
#include <cmath>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>

#include "core/config.h"
#include "exchange/bybit_exchange_adapter.h"
#include "execution/execution_engine.h"
#include "oms/account_state.h"
#include "risk/risk_engine.h"
#include "strategy/strategy_engine.h"

namespace {
using namespace ai_trade;
constexpr std::int64_t kStart = 1704067200000LL;

void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

const char* RiskName(RiskMode mode) {
  switch (mode) {
    case RiskMode::kNormal: return "NORMAL";
    case RiskMode::kDegraded: return "DEGRADED";
    case RiskMode::kCooldown: return "COOLDOWN";
    case RiskMode::kFuse: return "FUSE";
    case RiskMode::kReduceOnly: return "REDUCE_ONLY";
  }
  throw std::runtime_error("unknown risk mode in audit output");
}

MarketEvent Tick(int index, double price, std::int64_t interval = 5000) {
  MarketEvent event;
  event.ts_ms = kStart + index * interval;
  event.symbol = "BTCUSDT";
  event.price = price;
  event.mark_price = price;
  event.interval_ms = interval;
  return event;
}

RegimeState FixedRegime() {
  RegimeState regime;
  regime.regime = Regime::kUptrend;
  regime.bucket = RegimeBucket::kTrend;
  regime.volatility_level = 0.0001;
  regime.warmup = false;
  return regime;
}

struct Cadence {
  int evaluated{0};
  int active{0};
  std::int64_t first_active_offset_ms{-1};
};

Cadence ObserveCadence(const StrategyConfig& config) {
  StrategyEngine strategy(config);
  AccountState synthetic_account;
  Cadence observed;
  for (int i = 0; i < 60; ++i) {
    const auto signal = strategy.OnMarket(
        Tick(i, 100.0 + 0.01 * i), synthetic_account, FixedRegime());
    for (const auto& reason : signal.reason_codes) {
      if (reason == "STR_TREND_EVAL") ++observed.evaluated;
    }
    if (signal.direction != 0) {
      ++observed.active;
      if (observed.first_active_offset_ms < 0)
        observed.first_active_offset_ms = i * 5000;
    }
  }
  return observed;
}

struct FillObservation {
  int before_next_event{0};
  int after_next_event{0};
  double same_event_vwap{0.0};
  double signal_price{0.0};
};

FillObservation ObserveReplay(const std::string& fixture, bool maker) {
  BybitAdapterOptions options;
  options.mode = "replay";
  options.public_ws_enabled = false;
  options.private_ws_enabled = false;
  options.replay_market_data_path = fixture;
  options.replay_prices.clear();
  options.replay_entry_fee_bps = 5.5;
  options.replay_expected_slippage_bps = 1.0;
  options.maker_entry_enabled = maker;
  // Connect must return through the offline branch, before transport creation.
  options.http_transport_factory = []() -> std::unique_ptr<BybitHttpTransport> {
    throw std::runtime_error("network transport forbidden in synthetic audit");
  };
  BybitExchangeAdapter adapter(options);
  Require(adapter.Connect(), "offline replay connect failed");
  StrategyEngine strategy;
  AccountState synthetic_account;
  RiskEngine risk(3000.0);
  ExecutionEngine execution(380.0);
  MarketEvent event;
  Require(adapter.PollMarket(&event), "missing first synthetic bar");
  strategy.OnMarket(event, synthetic_account, FixedRegime());
  Require(adapter.PollMarket(&event), "missing second synthetic bar");
  const auto signal = strategy.OnMarket(event, synthetic_account, FixedRegime());
  Require(signal.direction == 1, "synthetic rising close did not make a signal");
  const auto target = risk.Apply(
      TargetPosition{event.symbol, signal.suggested_notional_usd}, true, 0.0);
  auto intent = execution.BuildIntent(target, 0.0, event.price);
  Require(intent.has_value(), "synthetic signal did not make an intent");
  intent->client_order_id = maker ? "synthetic-maker" : "synthetic-taker";
  Require(adapter.SubmitOrder(*intent), "offline synthetic submission failed");
  FillObservation observed;
  observed.signal_price = event.price;
  FillEvent fill;
  double notional = 0.0;
  double quantity = 0.0;
  while (adapter.PollFill(&fill)) {
    ++observed.before_next_event;
    notional += fill.qty * fill.price;
    quantity += fill.qty;
  }
  if (quantity > 0.0) observed.same_event_vwap = notional / quantity;
  Require(adapter.PollMarket(&event), "missing third synthetic bar");
  while (adapter.PollFill(&fill)) ++observed.after_next_event;
  Require(!adapter.PollMarket(&event), "unexpected extra synthetic bar");
  return observed;
}

struct RecoveryObservation {
  double drawdown{0.0};
  std::string mode;
  int nonzero_targets{0};
};

RecoveryObservation ObserveFlatRecovery(double loss_fraction) {
  AccountState synthetic_account;
  synthetic_account.OnMarket(Tick(0, 100.0));
  FillEvent open;
  open.symbol = "BTCUSDT";
  open.direction = 1;
  open.qty = 100.0;
  open.price = 100.0;
  synthetic_account.ApplyFill(open);
  const double exit_price = 100.0 * (1.0 - loss_fraction);
  synthetic_account.OnMarket(Tick(1, exit_price));
  RiskEngine risk(3000.0);
  const TargetPosition target{"BTCUSDT", 500.0};
  const auto triggered = risk.Apply(target, true, synthetic_account.drawdown_pct());
  Require(triggered.reduce_only && triggered.adjusted_notional_usd == 0.0,
          "synthetic loss did not trigger a flatten target");
  FillEvent close = open;
  close.direction = -1;
  close.price = exit_price;
  synthetic_account.ApplyFill(close);
  Require(synthetic_account.position_qty("BTCUSDT") == 0.0,
          "synthetic close left a position");
  RecoveryObservation observed;
  // Seven synthetic days, not seven days of wall-clock waiting. No external
  // deposit, reset of high-water mark, restart or invented equity recovery.
  for (int i = 1; i <= 2016; ++i) {
    synthetic_account.OnMarket(Tick(i, 100.0 + (i % 17), 300000));
    const auto result = risk.Apply(target, true, synthetic_account.drawdown_pct());
    if (std::fabs(result.adjusted_notional_usd) > 1e-9)
      ++observed.nonzero_targets;
    observed.mode = RiskName(result.risk_mode);
  }
  observed.drawdown = synthetic_account.drawdown_pct();
  Require(std::fabs(observed.drawdown - loss_fraction) < 1e-12,
          "flat account unexpectedly changed drawdown");
  return observed;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    Require(argc == 3, "usage: audit_original_mvp_contract CONFIG SYNTHETIC_CSV");
    // Erase process-local credential variables without inspecting their values.
    // The adapter otherwise resolves env even for replay. No .env is loaded.
    for (const char* key : {"AI_TRADE_BYBIT_DEMO_API_KEY",
                            "AI_TRADE_BYBIT_DEMO_API_SECRET",
                            "AI_TRADE_BYBIT_TESTNET_API_KEY",
                            "AI_TRADE_BYBIT_TESTNET_API_SECRET",
                            "AI_TRADE_BYBIT_MAINNET_API_KEY",
                            "AI_TRADE_BYBIT_MAINNET_API_SECRET",
                            "AI_TRADE_API_KEY", "AI_TRADE_API_SECRET"}) {
      Require(unsetenv(key) == 0, "cannot erase process-local credential variable");
    }
    AppConfig app_config;
    std::string error;
    Require(LoadAppConfigFromYaml(argv[1], &app_config, &error),
            "cannot load tracked replay configuration");
    Require(app_config.mode == "replay", "only replay config allowed");
    const auto core_cadence = ObserveCadence(StrategyConfig{});
    const auto configured_cadence = ObserveCadence(app_config.GetStrategyConfig());
    const auto taker = ObserveReplay(argv[2], false);
    const auto maker = ObserveReplay(argv[2], true);
    const auto cooldown = ObserveFlatRecovery(0.13);
    const auto fuse = ObserveFlatRecovery(0.21);
    const bool cadence_gap = configured_cadence.evaluated > 0;
    const bool latency_gap = taker.before_next_event > 0;
    const bool recovery_gap = cooldown.nonzero_targets == 0 &&
                              fuse.nonzero_targets == 0;
    std::cout << std::setprecision(15)
              << "ORIGINAL_MVP_CONTRACT_AUDIT {"
              << "\"schema_version\":\"original_mvp_synthetic_contract_audit_v1\","
              << "\"synthetic_only\":true,\"historical_backtest\":false,"
              << "\"cadence\":{\"span_ms\":295000,\"events\":60,"
              << "\"core_default_evaluations\":" << core_cadence.evaluated
              << ",\"core_first_active_offset_ms\":" << core_cadence.first_active_offset_ms
              << ",\"configured_evaluations\":" << configured_cadence.evaluated
              << ",\"configured_active_signals\":" << configured_cadence.active
              << ",\"configured_first_active_offset_ms\":" << configured_cadence.first_active_offset_ms
              << "},\"latency\":{\"signal_close\":" << taker.signal_price
              << ",\"taker_fill_parts_before_next_event\":" << taker.before_next_event
              << ",\"taker_same_event_vwap\":" << taker.same_event_vwap
              << ",\"maker_fill_parts_before_next_event\":" << maker.before_next_event
              << ",\"maker_fill_parts_after_next_event\":" << maker.after_next_event
              << "},\"flat_recovery\":{\"simulated_5m_steps\":2016,"
              << "\"cooldown_drawdown\":" << cooldown.drawdown
              << ",\"cooldown_final_mode\":\"" << cooldown.mode
              << "\",\"cooldown_nonzero_targets\":" << cooldown.nonzero_targets
              << ",\"fuse_drawdown\":" << fuse.drawdown
              << ",\"fuse_final_mode\":\"" << fuse.mode
              << "\",\"fuse_nonzero_targets\":" << fuse.nonzero_targets
              << "},\"contract_aligned\":"
              << ((!cadence_gap && !latency_gap && !recovery_gap) ? "true" : "false")
              << ",\"economic_qualification\":false,\"next_action\":\"STOP_FOR_REVIEW\"}\n";
    // 0 means the observation command completed, never that the MVP passed.
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "SYNTHETIC_AUDIT_ERROR: " << error.what() << '\n';
    return 1;
  }
}
