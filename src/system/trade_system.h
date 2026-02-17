#pragma once

#include <array>
#include <optional>
#include <string>
#include <vector>

#include "core/config.h"
#include "core/types.h"
#include "execution/execution_engine.h"
#include "market/market_data.h"
#include "oms/account_state.h"
#include "regime/regime_engine.h"
#include "risk/risk_engine.h"
#include "strategy/integrator_shadow.h"
#include "strategy/strategy_engine.h"

namespace ai_trade {

// Snapshot of the decision pipeline for auditing/logging
struct MarketDecision {
  RegimeState regime;
  Signal base_signal;
  Signal signal; // Final signal after integrator
  ShadowInference shadow;
  bool integrator_policy_applied{false};
  std::string integrator_policy_reason{"n/a"};
  double integrator_confidence{0.0};
  TargetPosition target;
  RiskAdjustedPosition risk_adjusted;
  std::optional<OrderIntent> intent;
};

/**
 * @brief Trade System (Pipeline Orchestrator)
 *
 * Coordinates the data flow:
 * Market -> Regime -> Strategy -> Integrator -> Risk -> Execution -> OMS
 */
class TradeSystem {
 public:
  explicit TradeSystem(const AppConfig& config);
  TradeSystem(double risk_cap_usd, double max_order_notional_usd,
              RiskThresholds risk_thresholds = {},
              StrategyConfig strategy_config = {},
              double min_rebalance_notional_usd = 0.0,
              RegimeConfig regime_config = {},
              IntegratorConfig integrator_config = {});

  // --- Main Pipeline ---

  /// Processes a market event and returns the full decision context.
  MarketDecision Evaluate(const MarketEvent& event, bool trade_ok = true);

  /// Simplified entry point returning just the order intent (if any).
  std::optional<OrderIntent> OnMarket(const MarketEvent& event, bool trade_ok = true);

  /// Helper for local replay/testing: generates event from price and processes it.
  bool OnPrice(double price, bool trade_ok = true);

  // --- State Updates ---

  void OnFill(const FillEvent& fill);
  void OnMarketSnapshot(const MarketEvent& event);

  // --- Remote Synchronization ---

  void SyncAccountFromRemotePositions(
      const std::vector<RemotePositionSnapshot>& positions,
      double baseline_cash_usd = 10000.0);
      
  void RefreshAccountRiskFromRemotePositions(
      const std::vector<RemotePositionSnapshot>& positions);
      
  void ForceSyncAccountPositionsFromRemote(
      const std::vector<RemotePositionSnapshot>& positions);
      
  void SyncAccountFromRemoteBalance(const RemoteAccountBalanceSnapshot& balance,
                                    bool reset_peak_to_equity);

  // --- Configuration & Control ---

  void EnableEvolution(bool enabled) { evolution_enabled_ = enabled; }
  
  bool SetEvolutionWeights(double trend_weight, double defensive_weight,
                           std::string* out_error);
  bool SetEvolutionWeightsForBucket(RegimeBucket bucket, double trend_weight,
                                    double defensive_weight,
                                    std::string* out_error);
                           
  EvolutionWeights GetEvolutionWeights(RegimeBucket bucket) const;
  
  // Integrator Control
  bool InitializeIntegratorShadow(std::string* out_error);
  IntegratorMode GetIntegratorMode() const { return integrator_config_.mode; }
  void SetIntegratorMode(IntegratorMode mode) { integrator_config_.mode = mode; }
  
  // Risk Control
  void ForceReduceOnly(bool enabled) { risk_.SetForcedReduceOnly(enabled); }
  RiskMode GetRiskMode() const { return risk_.mode(); }

  // Accessors
  const AccountState& GetAccount() const { return account_; }

  // Compatibility shims for legacy call sites.
  const AccountState& account() const { return GetAccount(); }
  IntegratorMode integrator_mode() const { return GetIntegratorMode(); }
  RiskMode risk_mode() const { return GetRiskMode(); }
  EvolutionWeights evolution_weights(RegimeBucket bucket) const {
    return GetEvolutionWeights(bucket);
  }
  std::array<EvolutionWeights, 3> evolution_weights_all() const {
    return evolution_weights_by_bucket_;
  }
  std::string integrator_shadow_model_version() const {
    return integrator_shadow_.model_version();
  }

 private:
  // Components
  MarketData market_generator_; // Only for OnPrice replay
  StrategyEngine strategy_;
  RegimeEngine regime_;
  RiskEngine risk_;
  ExecutionEngine execution_;
  IntegratorShadow integrator_shadow_;
  AccountState account_;

  // Configuration
  IntegratorConfig integrator_config_;
  double max_account_gross_notional_usd_;
  bool evolution_enabled_{false};
  std::array<EvolutionWeights, 3> evolution_weights_by_bucket_;

  // Helpers
  bool ApplyIntegratorPolicy(const ShadowInference& shadow, Signal* inout_signal,
                             double* out_confidence, std::string* out_reason) const;
};

}  // namespace ai_trade
