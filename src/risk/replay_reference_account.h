#pragma once
#include <optional>
#include <string>
#include <unordered_set>
#include "oms/account_state.h"

namespace ai_trade {
// Explicit research assumptions, never exchange risk snapshots. BTC-only v1.
struct ReplayReferenceRules {
  static constexpr double kLeverage = 2, kMaintenance = 0.01, kExitLiability = 0.0013;
  static constexpr double kTierLimit = 10000, kGrossCap = 3000;
  static constexpr double kQtyStep = 0.001, kMinEntry = 5, kPriceTick = 0.1;
  static double QuantizeQuantity(double qty);
  static double AdversePrice(double price, int direction);
};
class ReplayReferenceAccount {
 public:
  void OnMarket(const MarketEvent& event, const AccountState& account);
  void OnFill(const FillEvent& fill, const AccountState& before);
  void OnFunding(double rate, double paid, const AccountState& before);
  // Observe the actual cash ledger after fees/funding, including EOF settlement.
  void AfterAccounting(const AccountState& account);
  std::optional<double> RiskDistance(const AccountState& account) const;
  double collateral() const { return collateral_; }
  double funding_uncertainty() const { return funding_uncertainty_; }
  double peak_upper() const { return peak_upper_; }
  double max_drawdown_upper() const { return max_drawdown_upper_; }
  const std::string& stop_reason() const { return stop_reason_; }
 private:
  [[noreturn]] void Stop(const std::string& reason);
  void Check(bool valid, const std::string& reason);
  double Boundary() const;
  double DistanceAt(double mark) const;
  double qty_{0}, entry_{0}, collateral_{0}, mark_{0};
  double peak_upper_{10000}, max_drawdown_upper_{0}, funding_uncertainty_{0};
  double pending_funding_exposure_{0};
  Timestamp last_ts_{0}, last_closed_{0}, last_open_{0}, last_funding_{0};
  std::unordered_set<std::string> fills_;
  bool opening_fill_seen_{false};
  std::string stop_reason_;
};
}  // namespace ai_trade
