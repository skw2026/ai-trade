#pragma once

// Synthetic-only integration seam. Not linked into ai_trade_system or adapters.
#include <map>
#include <optional>
#include <string>
#include "execution/execution_engine.h"
#include "oms/account_state.h"
#include "oms/order_manager.h"
#include "risk/risk_engine.h"

namespace ai_trade::synthetic {
struct RewardEvent {
  std::string id, kind, order{"-"}, fill{"-"};
  Timestamp time{0};
  int bucket{0}, version{0}, side{0};
  double a{0}, b{0}, c{0}, d{0};
  std::string Identity(bool fill_only = false) const;
};

class ExecutionRewardLedger {
 public:
  explicit ExecutionRewardLedger(int cost_multiplier);
  std::string Apply(const RewardEvent& event);
  std::string Snapshot(const RewardEvent& event = {}, bool duplicate = false,
                       double reward = 0,
                       std::optional<OrderIntent> intent = std::nullopt) const;
  static bool ValidId(const std::string& value);
 private:
  double Liquidation() const;
  void ObserveRisk();
  AccountState account_;
  OrderManager orders_;
  RiskEngine risk_{2500};
  ExecutionEngine execution_{ExecutionEngineConfig{}};
  int multiplier_;
  double quote_{10000}, mark_{10000}, last_liquidation_{10000};
  double max_drawdown_{0}, slippage_{0}, turnover_{0};
  bool manual_lock_{false}, hard_lock_{false};
  Timestamp last_time_{0};
  std::map<std::string, std::string> events_, fills_;
};
}  // namespace ai_trade::synthetic
