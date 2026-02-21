#include "system/trade_system.h"

#include <algorithm>
#include <cmath>
#include <iostream>

#include "core/log.h"

namespace ai_trade {

namespace {

constexpr double kWeightEpsilon = 1e-9;
constexpr double kNotionalEpsilon = 1e-6;

bool HasExposure(double notional_usd) {
  return std::fabs(notional_usd) > kNotionalEpsilon;
}

int SignOf(double value) {
  if (value > kNotionalEpsilon) return 1;
  if (value < -kNotionalEpsilon) return -1;
  return 0;
}

std::size_t BucketToIndex(RegimeBucket bucket) {
  switch (bucket) {
    case RegimeBucket::kTrend: return 0;
    case RegimeBucket::kRange: return 1;
    case RegimeBucket::kExtreme: return 2;
  }
  return 1;
}

double BlendSignalNotional(const Signal& signal, const EvolutionWeights& weights) {
  if (!HasExposure(signal.trend_notional_usd) &&
      !HasExposure(signal.defensive_notional_usd)) {
    return signal.suggested_notional_usd;
  }
  return signal.trend_notional_usd * weights.trend_weight +
         signal.defensive_notional_usd * weights.defensive_weight;
}

}  // namespace

TradeSystem::TradeSystem(const AppConfig& config)
    : strategy_(config.GetStrategyConfig()),
      regime_(config.regime),
      risk_(config.risk_max_abs_notional_usd, config.risk_thresholds),
      execution_(config.GetExecutionEngineConfig()),
      integrator_shadow_(config.integrator.shadow),
      integrator_config_(config.integrator),
      max_account_gross_notional_usd_(config.risk_max_abs_notional_usd) {
  
  // Initialize default weights
  evolution_weights_by_bucket_.fill({1.0, 0.0});
}

TradeSystem::TradeSystem(double risk_cap_usd, double max_order_notional_usd,
                         RiskThresholds risk_thresholds,
                         StrategyConfig strategy_config,
                         double min_rebalance_notional_usd,
                         RegimeConfig regime_config,
                         IntegratorConfig integrator_config)
    : strategy_(strategy_config),
      regime_(regime_config),
      risk_(risk_cap_usd, risk_thresholds),
      execution_(ExecutionEngineConfig{
          .max_order_notional_usd = max_order_notional_usd,
          .min_rebalance_notional_usd = min_rebalance_notional_usd,
      }),
      integrator_shadow_(integrator_config.shadow),
      integrator_config_(integrator_config),
      max_account_gross_notional_usd_(risk_cap_usd) {
  evolution_weights_by_bucket_.fill({1.0, 0.0});
}

bool TradeSystem::OnPrice(double price, bool trade_ok) {
  const MarketEvent event = market_generator_.Next(price);
  const auto decision = Evaluate(event, trade_ok, 0.0);
  
  if (!decision.intent.has_value()) {
    return false;
  }

  // In replay mode, immediately fill the intent
  FillEvent fill;
  fill.fill_id = decision.intent->client_order_id + "-sim-fill";
  fill.client_order_id = decision.intent->client_order_id;
  fill.symbol = decision.intent->symbol;
  fill.direction = decision.intent->direction;
  fill.qty = decision.intent->qty;
  fill.price = decision.intent->price;

  OnFill(fill);
  LogInfo("Skeleton Mode: Order Filled");
  return true;
}

std::optional<OrderIntent> TradeSystem::OnMarket(
    const MarketEvent& event,
    bool trade_ok,
    double symbol_inflight_notional_usd) {
  return Evaluate(event, trade_ok, symbol_inflight_notional_usd).intent;
}

MarketDecision TradeSystem::Evaluate(const MarketEvent& event,
                                     bool trade_ok,
                                     double symbol_inflight_notional_usd) {
  MarketDecision decision;

  // 1. Update Account Valuation
  account_.OnMarket(event);

  // 2. Regime Analysis
  decision.regime = regime_.OnMarket(event);

  // 3. Strategy Signal Generation
  decision.base_signal = strategy_.OnMarket(event, account_, decision.regime);
  if (decision.base_signal.symbol.empty()) {
    decision.base_signal.symbol = event.symbol;
  }

  // 3.1. Evolution Weighting (Optional)
  if (evolution_enabled_) {
    const auto weights = GetEvolutionWeights(decision.regime.bucket);
    decision.base_signal.suggested_notional_usd =
        BlendSignalNotional(decision.base_signal, weights);
    decision.base_signal.direction = SignOf(decision.base_signal.suggested_notional_usd);
  }

  // 4. Integrator / ML Overlay
  decision.signal = decision.base_signal;
  integrator_shadow_.OnMarket(event);
  decision.shadow = integrator_shadow_.Infer(decision.base_signal, decision.regime);
  
  decision.integrator_policy_applied = ApplyIntegratorPolicy(
      decision.shadow,
      &decision.signal,
      &decision.integrator_confidence,
      &decision.integrator_policy_reason);

  // 5. Risk Management
  decision.target = TargetPosition{decision.signal.symbol, decision.signal.suggested_notional_usd};
  
  const double liq_dist = account_.liquidation_distance_p95();
  decision.risk_adjusted = risk_.Apply(decision.target, trade_ok, account_.drawdown_pct(), liq_dist);

  // 5.1. Global Account Gross Notional Check
  const double settled_symbol_notional =
      account_.current_notional_usd(decision.risk_adjusted.symbol);
  const double symbol_current_notional =
      settled_symbol_notional + symbol_inflight_notional_usd;
  const double settled_gross_notional = account_.gross_notional_usd();
  const double gross_notional =
      std::max(0.0, settled_gross_notional +
                        std::fabs(symbol_current_notional) -
                        std::fabs(settled_symbol_notional));
  const double other_symbols_gross =
      std::max(0.0, gross_notional - std::fabs(symbol_current_notional));
  const double symbol_budget = std::max(0.0, max_account_gross_notional_usd_ - other_symbols_gross);
  
  if (std::fabs(decision.risk_adjusted.adjusted_notional_usd) > symbol_budget) {
    decision.risk_adjusted.adjusted_notional_usd = std::clamp(
        decision.risk_adjusted.adjusted_notional_usd, -symbol_budget, symbol_budget);
  }

  // 6. Execution
  decision.intent = execution_.BuildIntent(decision.risk_adjusted,
                                           symbol_current_notional,
                                           event.price);
  return decision;
}

void TradeSystem::OnFill(const FillEvent& fill) {
  account_.ApplyFill(fill);
}

void TradeSystem::OnMarketSnapshot(const MarketEvent& event) {
  account_.OnMarket(event);
}

void TradeSystem::SyncAccountFromRemotePositions(
    const std::vector<RemotePositionSnapshot>& positions,
    double baseline_cash_usd) {
  account_.SyncFromRemotePositions(positions, baseline_cash_usd);
}

void TradeSystem::RefreshAccountRiskFromRemotePositions(
    const std::vector<RemotePositionSnapshot>& positions) {
  account_.RefreshRiskFromRemotePositions(positions);
}

void TradeSystem::ForceSyncAccountPositionsFromRemote(
    const std::vector<RemotePositionSnapshot>& positions) {
  account_.ForceSyncPositionsFromRemote(positions);
}

void TradeSystem::SyncAccountFromRemoteBalance(
    const RemoteAccountBalanceSnapshot& balance,
    bool reset_peak_to_equity) {
  account_.SyncFromRemoteAccountBalance(balance, reset_peak_to_equity);
}

bool TradeSystem::SetEvolutionWeights(double trend_weight,
                                      double defensive_weight,
                                      std::string* out_error) {
  if (trend_weight < -kWeightEpsilon || defensive_weight < -kWeightEpsilon) {
    if (out_error) *out_error = "Weights cannot be negative";
    return false;
  }
  if (std::fabs(trend_weight + defensive_weight - 1.0) > 1e-6) {
    if (out_error) *out_error = "Weights must sum to 1.0";
    return false;
  }

  const EvolutionWeights w{trend_weight, defensive_weight};
  evolution_weights_by_bucket_.fill(w);
  return true;
}

bool TradeSystem::SetEvolutionWeightsForBucket(RegimeBucket bucket,
                                               double trend_weight,
                                               double defensive_weight,
                                               std::string* out_error) {
  if (trend_weight < -kWeightEpsilon || defensive_weight < -kWeightEpsilon) {
    if (out_error) *out_error = "Weights cannot be negative";
    return false;
  }
  if (std::fabs(trend_weight + defensive_weight - 1.0) > 1e-6) {
    if (out_error) *out_error = "Weights must sum to 1.0";
    return false;
  }
  evolution_weights_by_bucket_[BucketToIndex(bucket)] = EvolutionWeights{
      trend_weight, defensive_weight};
  return true;
}

EvolutionWeights TradeSystem::GetEvolutionWeights(RegimeBucket bucket) const {
  return evolution_weights_by_bucket_[BucketToIndex(bucket)];
}

bool TradeSystem::InitializeIntegratorShadow(std::string* out_error) {
  const bool strict = (integrator_config_.mode == IntegratorMode::kCanary ||
                       integrator_config_.mode == IntegratorMode::kActive);
  return integrator_shadow_.Initialize(strict, out_error);
}

bool TradeSystem::ApplyIntegratorPolicy(const ShadowInference& shadow,
                                        Signal* inout_signal,
                                        double* out_confidence,
                                        std::string* out_reason) const {
  if (!inout_signal) return false;

  auto set_out = [&](double conf, const std::string& reason) {
    if (out_confidence) *out_confidence = conf;
    if (out_reason) *out_reason = reason;
  };

  set_out(0.0, "");

  if (integrator_config_.mode == IntegratorMode::kOff) {
    set_out(0.0, "mode_off");
    return false;
  }
  if (integrator_config_.mode == IntegratorMode::kShadow) {
    set_out(shadow.p_up - shadow.p_down, "mode_shadow_observe_only");
    return false;
  }
  if (!shadow.enabled) {
    set_out(0.0, "shadow_unavailable");
    return false;
  }
  if (!HasExposure(inout_signal->suggested_notional_usd)) {
    set_out(0.0, "flat_base_signal");
    return false;
  }

  const double confidence = shadow.p_up - shadow.p_down;
  const double confidence_abs = std::fabs(confidence);
  const int shadow_direction = SignOf(confidence);
  const int base_direction = SignOf(inout_signal->suggested_notional_usd);
  const double base_abs_notional = std::fabs(inout_signal->suggested_notional_usd);
  
  set_out(confidence, "");

  if (shadow_direction == 0) {
    set_out(confidence, "neutral_confidence");
    return false;
  }

  // Canary Mode
  if (integrator_config_.mode == IntegratorMode::kCanary) {
    if (confidence_abs < integrator_config_.canary_confidence_threshold) {
      set_out(confidence, "canary_low_confidence");
      return false;
    }
    if (!integrator_config_.canary_allow_countertrend &&
        shadow_direction != base_direction) {
      set_out(confidence, "canary_countertrend_blocked");
      return false;
    }
    
    const double canary_ratio = std::clamp(integrator_config_.canary_notional_ratio, 0.0, 1.0);
    const double scaled_abs_notional = base_abs_notional * canary_ratio;
    const double canary_min_notional_usd =
        std::max(0.0, integrator_config_.canary_min_notional_usd);
    if (canary_min_notional_usd > 0.0 &&
        scaled_abs_notional + kNotionalEpsilon < canary_min_notional_usd) {
      if (!HasExposure(inout_signal->suggested_notional_usd)) {
        set_out(confidence, "canary_below_min_notional_no_change");
        return false;
      }
      inout_signal->suggested_notional_usd = 0.0;
      inout_signal->direction = 0;
      set_out(confidence, "canary_below_min_notional_to_flat");
      return true;
    }
    const double final_notional =
        static_cast<double>(shadow_direction) * scaled_abs_notional;
    
    if (!HasExposure(final_notional - inout_signal->suggested_notional_usd)) {
      set_out(confidence, "canary_no_change");
      return false;
    }
    
    inout_signal->suggested_notional_usd = final_notional;
    inout_signal->direction = SignOf(final_notional);
    set_out(confidence, "canary_applied");
    return true;
  }

  // Active Mode
  if (confidence_abs < integrator_config_.active_confidence_threshold) {
    if (!HasExposure(inout_signal->suggested_notional_usd)) {
      set_out(confidence, "active_low_confidence_no_change");
      return false;
    }
    inout_signal->suggested_notional_usd = 0.0;
    inout_signal->direction = 0;
    set_out(confidence, "active_low_confidence_to_flat");
    return true;
  }

  const double active_full_notional_threshold = std::clamp(
      integrator_config_.active_full_notional_confidence_threshold,
      integrator_config_.active_confidence_threshold, 1.0);
  const double active_partial_notional_ratio =
      std::clamp(integrator_config_.active_partial_notional_ratio, 0.0, 1.0);
  const double notional_scale =
      confidence_abs >= active_full_notional_threshold
          ? 1.0
          : active_partial_notional_ratio;
  const double scaled_abs_notional = base_abs_notional * notional_scale;
  const double final_notional = static_cast<double>(shadow_direction) * scaled_abs_notional;
  
  if (!HasExposure(final_notional - inout_signal->suggested_notional_usd)) {
    set_out(confidence, "active_no_change");
    return false;
  }
  
  inout_signal->suggested_notional_usd = final_notional;
  inout_signal->direction = SignOf(final_notional);
  set_out(confidence,
          notional_scale >= 1.0 - kNotionalEpsilon ? "active_applied_full"
                                                    : "active_applied_partial");
  return true;
}

}  // namespace ai_trade
