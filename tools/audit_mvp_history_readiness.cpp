// Synthetic diagnostic only. A successful process is NOT historical readiness.
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <vector>

#include "core/config.h"
#include "exchange/bybit_exchange_adapter.h"
#include "execution/execution_engine.h"
#include "oms/account_state.h"
#include "risk/risk_engine.h"
#include "system/trade_system.h"

namespace {
using namespace ai_trade;
void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
bool Near(double a, double b) { return std::fabs(a - b) < 1e-8; }

void Observe(const AppConfig& config, const char* fixture, int direction) {
  BybitAdapterOptions options;
  options.mode = "replay";
  options.replay_causal_bars = true;
  options.replay_market_data_path = fixture;
  options.public_ws_enabled = options.private_ws_enabled = false;
  options.public_ws_rest_fallback = options.private_ws_rest_fallback = false;
  options.replay_entry_fee_bps = config.execution_entry_fee_bps;
  options.replay_exit_fee_bps = config.execution_exit_fee_bps;
  options.replay_expected_slippage_bps = config.execution_expected_slippage_bps;
  options.http_transport_factory = []() -> std::unique_ptr<BybitHttpTransport> {
    throw std::runtime_error("network forbidden in synthetic audit");
  };
  BybitExchangeAdapter adapter(options);
  Require(adapter.Connect(), "synthetic causal fixture failed to connect");
  AccountState account;
  TradeSystem system(config);
  RiskEngine risk(config.risk_max_abs_notional_usd, config.risk_thresholds);
  ExecutionEngine execution(config.GetExecutionEngineConfig());
  MarketEvent event;
  Require(adapter.PollMarket(&event) && event.execution_only,
          "missing first opening");
  Require(adapter.PollMarket(&event) && event.completed_bar,
          "missing first close");
  account.OnMarket(event);
  const auto entry_target = risk.Apply({"BTCUSDT", direction * 100.0}, true,
      account.drawdown_pct(), account.minimum_liquidation_distance().value_or(0.0));
  const auto entry = execution.BuildIntent(entry_target, 0.0, event.price);
  Require(entry && !entry->reduce_only && Near(entry->qty, 1.0),
          "flat reference account did not allow synthetic entry");
  Require(adapter.SubmitOrder(*entry), "synthetic entry submit failed");
  FillEvent fill;
  Require(!adapter.PollFill(&fill), "entry executed before next open");
  Require(adapter.PollMarket(&event) && event.execution_only,
          "missing second opening");
  account.OnMarket(event);
  system.OnMarketSnapshot(event);
  int parts = 0;
  while (adapter.PollFill(&fill)) {
    account.ApplyFill(fill);
    system.OnFill(fill);
    ++parts;
  }
  Require(parts > 0 && Near(account.position_qty("BTCUSDT"), direction),
          "entry did not fully settle");
  Require(!account.minimum_liquidation_distance().has_value(),
          "diagnosed missing risk no longer reproduced: reassess readiness");
  std::vector<RemotePositionSnapshot> positions;
  Require(adapter.GetRemotePositions(&positions) && positions.size() == 1,
          "missing simulated remote position");
  Require(positions[0].liquidation_price == 0.0,
          "simulated adapter now supplies risk: reassess readiness");
  account.RefreshRiskFromRemotePositions(positions);
  Require(!account.minimum_liquidation_distance().has_value(),
          "risk refresh unexpectedly repaired missing risk");
  const auto decision = system.Evaluate(event);
  Require(decision.risk_adjusted.reduce_only &&
      std::find(decision.signal.reason_codes.begin(), decision.signal.reason_codes.end(),
                "RISK_LIQUIDATION_DATA_UNKNOWN") != decision.signal.reason_codes.end(),
      "TradeSystem did not expose the diagnosed unknown-risk block");

  const double current = account.current_notional_usd("BTCUSDT");
  auto intent_for = [&](double target) {
    const auto adjusted = risk.Apply({"BTCUSDT", target}, true,
        account.drawdown_pct(), account.minimum_liquidation_distance().value_or(0.0));
    Require(adjusted.risk_mode == RiskMode::kReduceOnly,
            "unknown risk did not remain reduce-only");
    return execution.BuildIntent(adjusted, current, event.price);
  };
  Require(!intent_for(2 * current), "unknown-risk same-side add allowed");
  Require(!intent_for(current), "holding target unexpectedly forced a close");
  const auto reduce = intent_for(current / 2);
  Require(reduce && reduce->reduce_only && reduce->direction == -direction &&
          Near(reduce->qty, 0.5), "partial reduction not preserved");
  const auto reverse = intent_for(-current);
  Require(reverse && reverse->reduce_only && reverse->direction == -direction &&
          Near(reverse->qty, 1.0), "reverse target opened risk instead of closing only");

  // Independent counterfactual input, NEVER injected into the account or adapter.
  RiskEngine known_risk(config.risk_max_abs_notional_usd, config.risk_thresholds);
  const auto known_target = known_risk.Apply({"BTCUSDT", 2 * current}, true,
                                            account.drawdown_pct(), 0.5);
  const auto known_add = execution.BuildIntent(known_target, current, event.price);
  Require(known_add && !known_add->reduce_only && known_add->direction == direction &&
          Near(known_add->qty, 1.0), "known-safe synthetic control did not allow add");
  std::cout << "MVP_HISTORY_READINESS_OBSERVATION {\"synthetic_only\":true,"
      << "\"direction\":" << direction << ",\"fill_parts\":" << parts
      << ",\"flat_entry_allowed\":true,\"position_risk_known\":false,"
      << "\"adapter_liquidation_price\":0,\"refresh_repairs_risk\":false,"
      << "\"system_unknown_reason\":true,\"same_side_add_blocked\":true,"
      << "\"holding_forced_flat\":false,\"partial_reduce_preserved\":true,"
      << "\"reverse_closes_only\":true,\"known_safe_control_add_allowed\":true}\n";
}
}  // namespace

int main(int argc, char** argv) {
  try {
    Require(argc == 3, "usage: mvp_history_readiness_audit CONFIG SYNTHETIC_CSV");
    // Remove process-local credential variables without reading their values.
    for (const char* key : {"AI_TRADE_BYBIT_DEMO_API_KEY", "AI_TRADE_BYBIT_DEMO_API_SECRET",
                           "AI_TRADE_BYBIT_TESTNET_API_KEY", "AI_TRADE_BYBIT_TESTNET_API_SECRET",
                           "AI_TRADE_BYBIT_MAINNET_API_KEY", "AI_TRADE_BYBIT_MAINNET_API_SECRET",
                           "AI_TRADE_API_KEY", "AI_TRADE_API_SECRET"}) {
      Require(unsetenv(key) == 0, "cannot erase process-local credential variable");
    }
    AppConfig config;
    std::string error;
    Require(LoadAppConfigFromYaml(argv[1], &config, &error), "cannot load MVP config");
    Require(config.mode == "replay" && config.closed_bar_mvp && config.exchange == "bybit",
            "only closed-bar Bybit offline MVP allowed");
    Observe(config, argv[2], 1);
    Observe(config, argv[2], -1);
    std::cout << "MVP_HISTORY_READINESS_RESULT {\"diagnostic_complete\":true,"
                 "\"historical_screen_ready\":false,\"economic_qualification\":false,"
                 "\"decision\":\"INSUFFICIENT_REPLAY_CAPITAL_CONTRACT\"}\n";
    return 0;  // Completed observation, explicitly NOT a business PASS.
  } catch (const std::exception& error) {
    std::cerr << "MVP_HISTORY_READINESS_ERROR: " << error.what() << '\n';
    return 1;
  }
}
