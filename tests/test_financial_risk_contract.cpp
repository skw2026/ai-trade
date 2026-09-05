#include <cmath>
#include <algorithm>
#include <iostream>
#include <limits>

#include "oms/account_state.h"
#include "risk/risk_engine.h"
#include "system/trade_system.h"

namespace {
int failures = 0;
void Check(bool ok, const char* message) {
  if (!ok) {
    std::cerr << message << '\n';
    ++failures;
  }
}
}  // namespace

int main() {
  using namespace ai_trade;
  // An otherwise safe account cannot conceal a single near-liquidation leg.
  TradeSystem system(1000.0, 1000.0, RiskThresholds{},
                     StrategyConfig{.signal_notional_usd = 900.0,
                                    .signal_deadband_abs = 0.1,
                                    .min_hold_ticks = 0}, 0.0);
  system.SyncAccountFromRemotePositions({
      {.symbol = "DANGER", .qty = 0.01, .avg_entry_price = 100.0,
       .mark_price = 100.0, .liquidation_price = 96.0},
      {.symbol = "SAFE", .qty = 5.0, .avg_entry_price = 100.0,
       .mark_price = 100.0, .liquidation_price = 50.0},
  });
  const auto decision = system.Evaluate(
      MarketEvent{1, "BTCUSDT", 100.0, 100.0}, true);
  Check(decision.risk_adjusted.reduce_only,
        "A small dangerous position must not be hidden by safe notional");

  TradeSystem unknown_system(1000.0, 1000.0);
  unknown_system.SyncAccountFromRemotePositions({
      {.symbol = "BTCUSDT", .qty = 1.0, .avg_entry_price = 100.0,
       .mark_price = 100.0, .liquidation_price = 0.0},
  });
  const auto unknown = unknown_system.Evaluate(
      MarketEvent{1, "BTCUSDT", 100.0, 100.0}, true);
  Check(unknown.risk_adjusted.reduce_only,
        "Unknown liquidation data on an open position must fail closed");
  Check(std::find(unknown.signal.reason_codes.begin(), unknown.signal.reason_codes.end(),
                  "RISK_LIQUIDATION_DATA_UNKNOWN") != unknown.signal.reason_codes.end(),
        "Unknown risk must have an auditable reason code");

  AccountState account;
  Check(account.minimum_liquidation_distance() == 1.0,
        "An empty account is distinct from an account with unknown risk");
  account.SyncFromRemotePositions({
      {.symbol = "SHORT", .qty = -1.0, .avg_entry_price = 100.0,
       .mark_price = 100.0, .liquidation_price = 105.0},
  });
  Check(account.minimum_liquidation_distance().has_value() &&
            std::fabs(*account.minimum_liquidation_distance() - 0.05) < 1e-12,
        "Short liquidation distance must use the opposite price direction");
  account.SyncFromRemotePositions({
      {.symbol = "MISSING_MARK", .qty = 1.0, .avg_entry_price = 100.0,
       .mark_price = 0.0, .liquidation_price = 90.0},
  });
  Check(!account.minimum_liquidation_distance().has_value(),
        "Average entry cost must not replace a missing risk mark");
  account.SyncFromRemotePositions({
      {.symbol = "BREACHED", .qty = 1.0, .avg_entry_price = 100.0,
       .mark_price = 100.0, .liquidation_price = 101.0},
  });
  Check(account.minimum_liquidation_distance() == 0.0,
        "A crossed liquidation level must not look like a positive distance");

  const TargetPosition target{"BTCUSDT", 500.0};
  RiskEngine risk(500.0);
  const auto fuse = risk.Apply(target, false, 0.21, 0.01);
  Check(fuse.risk_mode == RiskMode::kFuse && fuse.reduce_only &&
            fuse.adjusted_notional_usd == 0.0,
        "Connectivity/liquidation guards must not mask a drawdown fuse");
  risk.SetForcedReduceOnly(true);
  const auto held = risk.Apply(target, true, 0.17, 0.01);
  Check(held.risk_mode == RiskMode::kFuse &&
            held.adjusted_notional_usd == 0.0,
        "A forced reduce-only condition must preserve fuse hysteresis");

  RiskEngine interrupted(500.0);
  interrupted.Apply(target, true, 0.09, 0.5);
  Check(interrupted.Apply(target, false, 0.079, 0.5).reduce_only,
        "An outage must prohibit increases");
  Check(interrupted.Apply(target, true, 0.079, 0.5).risk_mode ==
            RiskMode::kDegraded,
        "An outage must not erase drawdown hysteresis");
  RiskEngine invalid(500.0);
  Check(invalid.Apply(target, true, 0.0,
                      std::numeric_limits<double>::quiet_NaN()).reduce_only,
        "Non-finite liquidation distance must fail closed");
  Check(invalid.Apply(target, true,
                      std::numeric_limits<double>::quiet_NaN(), 0.5).reduce_only,
        "Non-finite drawdown must fail closed");
  return failures ? 1 : 0;
}
