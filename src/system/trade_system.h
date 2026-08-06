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

struct IntegratorPolicyDecision {
  Signal signal;
  bool applied{false};
  double confidence{0.0};
  std::string reason;
};

IntegratorPolicyDecision EvaluateIntegratorPolicy(
    const IntegratorConfig& config,
    const ShadowInference& shadow,
    const Signal& base_signal,
    bool settled_symbol_exposure_present,
    bool pending_net_position_order_present,
    bool independent_base_signal_eligible);

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
  MarketDecision Evaluate(const MarketEvent& event,
                          bool trade_ok = true,
                          double symbol_inflight_notional_usd = 0.0,
                          bool has_pending_symbol_net_orders = false);

  /// Simplified entry point returning just the order intent (if any).
  std::optional<OrderIntent> OnMarket(const MarketEvent& event,
                                      bool trade_ok = true,
                                      double symbol_inflight_notional_usd = 0.0,
                                      bool has_pending_symbol_net_orders = false);

  /// Helper for local replay/testing: generates event from price and processes it.
  bool OnPrice(double price, bool trade_ok = true);

  // --- State Updates ---

  void OnFill(const FillEvent& fill);
  void OnReflectedFill(const FillEvent& fill,
                       double position_qty_before,
                       double avg_entry_price_before);
  void OnMarketSnapshot(const MarketEvent& event);
  double ApplyFunding(const std::string& symbol,
                      double funding_rate_per_interval) {
    return account_.ApplyFunding(symbol, funding_rate_per_interval);
  }

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
  bool BootstrapIntegratorHistory(std::string* out_error);
  void OnIntegratorMarket(const MarketEvent& event);
  IntegratorMode GetIntegratorMode() const { return integrator_config_.mode; }
  void SetIntegratorMode(IntegratorMode mode) { integrator_config_.mode = mode; }
  const std::string& integrator_training_symbol() const {
    return integrator_shadow_.training_symbol();
  }
  std::int64_t integrator_feature_bar_interval_ms() const {
    return integrator_shadow_.feature_bar_interval_ms();
  }
  size_t integrator_feature_sample_count() const {
    return integrator_shadow_.feature_sample_count();
  }
  
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
  const std::string& integrator_activation_transaction_id() const {
    return integrator_shadow_.activation_transaction_id();
  }
  const std::string& integrator_runtime_config_sha256() const {
    return integrator_shadow_.runtime_config_sha256();
  }
  const std::string& integrator_trade_bot_sha256() const {
    return integrator_shadow_.trade_bot_sha256();
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

};

}  // namespace ai_trade
