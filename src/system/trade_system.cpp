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

bool HasReasonCode(const Signal& signal, const std::string& code) {
  return std::find(signal.reason_codes.begin(), signal.reason_codes.end(), code) !=
         signal.reason_codes.end();
}

bool IsIndependentCanaryBaseEligible(const Signal& signal,
                                     bool signal_expired) {
  if (signal_expired || HasExposure(signal.suggested_notional_usd) ||
      HasExposure(signal.trend_notional_usd) ||
      HasExposure(signal.defensive_notional_usd) ||
      !HasReasonCode(signal, "STR_FLAT_SIGNAL")) {
    return false;
  }
  static constexpr std::array<const char*, 7> kSuppressionReasons = {
      "STR_WARMUP",
      "STR_FEATURE_NOT_READY",
      "STR_SIGNAL_EXPIRED",
      "STR_RANGE_CONFIDENCE_BLOCK",
      "STR_EXTREME_BLOCK",
      "STR_BREAKOUT_BLOCK",
      "STR_VOL_CAP_BLOCK",
  };
  return std::none_of(
      kSuppressionReasons.begin(), kSuppressionReasons.end(),
      [&signal](const char* code) { return HasReasonCode(signal, code); });
}

void PushReason(std::vector<std::string>* reasons, const std::string& code) {
  if (reasons == nullptr || code.empty()) {
    return;
  }
  if (std::find(reasons->begin(), reasons->end(), code) == reasons->end()) {
    reasons->push_back(code);
  }
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
      microstructure_demo_overlay_(config.integrator.microstructure_demo),
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
      microstructure_demo_overlay_(integrator_config.microstructure_demo),
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
    double symbol_inflight_notional_usd,
    bool has_pending_symbol_net_orders) {
  return Evaluate(event, trade_ok, symbol_inflight_notional_usd,
                  has_pending_symbol_net_orders)
      .intent;
}

MarketDecision TradeSystem::Evaluate(const MarketEvent& event,
                                     bool trade_ok,
                                     double symbol_inflight_notional_usd,
                                     bool has_pending_symbol_net_orders,
                                     const std::string& settled_position_candidate_id,
                                     const std::string& settled_position_policy_reason) {
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
  const bool base_signal_expired =
      decision.base_signal.valid_until_ms > 0 &&
      event.ts_ms > decision.base_signal.valid_until_ms;
  if (base_signal_expired) {
    decision.base_signal.suggested_notional_usd = 0.0;
    decision.base_signal.trend_notional_usd = 0.0;
    decision.base_signal.defensive_notional_usd = 0.0;
    decision.base_signal.direction = 0;
    decision.base_signal.confidence = 0.0;
    PushReason(&decision.base_signal.reason_codes, "STR_SIGNAL_EXPIRED");
  }

  // 3.1. Evolution Weighting (Optional)
  if (evolution_enabled_) {
    const auto weights = GetEvolutionWeights(decision.regime.bucket);
    decision.base_signal.suggested_notional_usd =
        BlendSignalNotional(decision.base_signal, weights);
    decision.base_signal.direction = SignOf(decision.base_signal.suggested_notional_usd);
    PushReason(&decision.base_signal.reason_codes, "PORT_EVOLUTION_BLEND");
  }

  // 4. Integrator / ML Overlay
  integrator_shadow_.OnMarket(event);
  decision.shadow = integrator_shadow_.Infer(decision.base_signal, decision.regime);
  const ShadowInference microstructure_demo =
      microstructure_demo_overlay_.Infer(event);
  // Source routing is deterministic and fail closed: a lifecycle-approved
  // microstructure target has priority in demo; before demo_ready, the legacy
  // OHLCV integrator remains the only possible source.
  if (microstructure_demo.target_position_signal) {
    decision.shadow = microstructure_demo;
  }
  const double settled_symbol_notional_usd =
      account_.current_notional_usd(event.symbol);
  const bool settled_exposure_owned_by_target_candidate =
      decision.shadow.target_position_signal &&
      decision.shadow.source == "microstructure_demo" &&
      settled_position_candidate_id == decision.shadow.model_version &&
      settled_position_policy_reason.rfind("microstructure_demo_", 0) == 0;
  const IntegratorPolicyDecision policy = EvaluateIntegratorPolicy(
      integrator_config_, decision.shadow, decision.base_signal,
      settled_symbol_notional_usd, has_pending_symbol_net_orders,
      IsIndependentCanaryBaseEligible(decision.base_signal,
                                      base_signal_expired),
      settled_exposure_owned_by_target_candidate);
  decision.signal = policy.signal;
  decision.integrator_policy_applied = policy.applied;
  decision.integrator_confidence = policy.confidence;
  decision.integrator_policy_reason = policy.reason;
  if (!decision.integrator_policy_reason.empty()) {
    PushReason(&decision.signal.reason_codes,
               "MODEL_" + decision.integrator_policy_reason);
  }
  if (decision.signal.reason_codes.empty()) {
    PushReason(&decision.signal.reason_codes, "STR_NO_REASON");
  }

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

void TradeSystem::OnReflectedFill(const FillEvent& fill,
                                  double position_qty_before,
                                  double avg_entry_price_before) {
  account_.RecordReflectedFillEconomics(fill,
                                        position_qty_before,
                                        avg_entry_price_before);
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

bool TradeSystem::BootstrapIntegratorHistory(std::string* out_error) {
  return integrator_shadow_.BootstrapHistory(out_error);
}

void TradeSystem::OnIntegratorMarket(const MarketEvent& event) {
  integrator_shadow_.OnMarket(event);
}

IntegratorPolicyDecision EvaluateIntegratorPolicy(
    const IntegratorConfig& config,
    const ShadowInference& shadow,
    const Signal& base_signal,
    double settled_symbol_notional_usd,
    bool pending_net_position_order_present,
    bool independent_base_signal_eligible,
    bool settled_exposure_owned_by_target_candidate) {
  IntegratorPolicyDecision result;
  result.signal = base_signal;
  const double confidence = shadow.p_up - shadow.p_down;
  const double confidence_abs = std::fabs(confidence);
  const int shadow_direction = SignOf(confidence);
  const int base_direction = SignOf(base_signal.suggested_notional_usd);
  const double base_abs_notional = std::fabs(base_signal.suggested_notional_usd);
  result.confidence = confidence;

  if (config.mode == IntegratorMode::kOff) {
    result.confidence = 0.0;
    result.reason = "mode_off";
    return result;
  }
  if (config.mode == IntegratorMode::kShadow) {
    result.reason = "mode_shadow_observe_only";
    return result;
  }
  if (!shadow.enabled) {
    result.confidence = 0.0;
    result.reason = "shadow_unavailable";
    return result;
  }
  if (shadow.target_position_signal) {
    if (config.mode != IntegratorMode::kCanary) {
      result.reason = "external_target_requires_canary";
      return result;
    }
    const int target_direction = std::clamp(shadow.target_direction, -1, 1);
    const bool settled_symbol_exposure_present =
        HasExposure(settled_symbol_notional_usd);
    result.signal.symbol = base_signal.symbol;
    result.signal.trend_notional_usd = 0.0;
    result.signal.defensive_notional_usd = 0.0;
    result.signal.valid_until_ms = shadow.target_valid_until_ms;
    if (target_direction == 0 || shadow.fail_closed) {
      result.signal.suggested_notional_usd = 0.0;
      result.signal.direction = 0;
      result.signal.confidence = 1.0;
      result.confidence = 0.0;
      result.applied = settled_symbol_exposure_present ||
                       HasExposure(base_signal.suggested_notional_usd) ||
                       pending_net_position_order_present;
      result.reason = shadow.fail_closed
                          ? "microstructure_demo_fail_closed_flat"
                          : "microstructure_demo_policy_flat";
      return result;
    }
    if (settled_symbol_exposure_present &&
        !settled_exposure_owned_by_target_candidate) {
      // A newly selected source may not inherit a position opened by the
      // previous candidate. Flatten it under the existing episode lineage;
      // only a later flat-account tick may begin the microstructure episode.
      result.signal.suggested_notional_usd = 0.0;
      result.signal.direction = 0;
      result.signal.confidence = 1.0;
      result.confidence = 0.0;
      result.applied = true;
      result.reason = "microstructure_demo_route_transition_flat";
      return result;
    }
    if (!config.canary_allow_independent_signal) {
      result.reason = "microstructure_demo_independent_signal_disabled";
      return result;
    }
    const double target_notional =
        std::max(0.0, shadow.target_notional_usd);
    if (!HasExposure(target_notional)) {
      result.reason = "microstructure_demo_notional_disabled";
      return result;
    }
    result.signal.suggested_notional_usd =
        static_cast<double>(target_direction) * target_notional;
    result.signal.direction = target_direction;
    result.signal.confidence = 1.0;
    result.confidence = static_cast<double>(target_direction);
    result.applied = true;
    result.reason = pending_net_position_order_present
                        ? "microstructure_demo_target_pending"
                        : "microstructure_demo_target";
    return result;
  }
  if (shadow_direction == 0) {
    result.reason = "neutral_confidence";
    return result;
  }

  if (config.mode == IntegratorMode::kCanary) {
    if (confidence_abs < config.canary_confidence_threshold) {
      result.reason = "canary_low_confidence";
      return result;
    }
    // Canary 是独立实验仓位，禁止缩放、减仓或接管任何既有 baseline 暴露。
    if (HasExposure(settled_symbol_notional_usd)) {
      result.reason = "canary_account_not_flat";
      return result;
    }
    if (pending_net_position_order_present) {
      result.reason = "canary_pending_order_present";
      return result;
    }
    if (!HasExposure(base_signal.suggested_notional_usd)) {
      if (!config.canary_allow_independent_signal) {
        result.reason = "flat_base_signal";
        return result;
      }
      if (!independent_base_signal_eligible) {
        result.reason = "canary_base_signal_ineligible";
        return result;
      }
      const double independent_notional =
          std::max(0.0, config.canary_independent_notional_usd);
      if (!HasExposure(independent_notional)) {
        result.reason = "canary_independent_notional_disabled";
        return result;
      }
      result.signal.suggested_notional_usd =
          static_cast<double>(shadow_direction) * independent_notional;
      result.signal.direction = shadow_direction;
      result.signal.confidence = confidence_abs;
      result.applied = true;
      result.reason = "canary_independent_signal";
      return result;
    }
    if (!config.canary_allow_countertrend &&
        shadow_direction != base_direction) {
      result.reason = "canary_countertrend_blocked";
      return result;
    }

    const double canary_ratio =
        std::clamp(config.canary_notional_ratio, 0.0, 1.0);
    const double scaled_abs_notional = base_abs_notional * canary_ratio;
    const double canary_min_notional_usd =
        std::max(0.0, config.canary_min_notional_usd);
    if (canary_min_notional_usd > 0.0 &&
        scaled_abs_notional + kNotionalEpsilon < canary_min_notional_usd) {
      result.signal.suggested_notional_usd = 0.0;
      result.signal.direction = 0;
      result.applied = true;
      result.reason = "canary_below_min_notional_to_flat";
      return result;
    }
    const double final_notional =
        static_cast<double>(shadow_direction) * scaled_abs_notional;

    if (!HasExposure(final_notional - base_signal.suggested_notional_usd)) {
      result.reason = "canary_no_change";
      return result;
    }

    result.signal.suggested_notional_usd = final_notional;
    result.signal.direction = SignOf(final_notional);
    result.applied = true;
    result.reason = "canary_applied";
    return result;
  }

  if (!HasExposure(base_signal.suggested_notional_usd)) {
    result.reason = "flat_base_signal";
    return result;
  }
  if (confidence_abs < config.active_confidence_threshold) {
    result.signal.suggested_notional_usd = 0.0;
    result.signal.direction = 0;
    result.applied = true;
    result.reason = "active_low_confidence_to_flat";
    return result;
  }

  const double active_full_notional_threshold = std::clamp(
      config.active_full_notional_confidence_threshold,
      config.active_confidence_threshold, 1.0);
  const double active_partial_notional_ratio =
      std::clamp(config.active_partial_notional_ratio, 0.0, 1.0);
  const double notional_scale =
      confidence_abs >= active_full_notional_threshold
          ? 1.0
          : active_partial_notional_ratio;
  const double scaled_abs_notional = base_abs_notional * notional_scale;
  const double active_min_notional_usd =
      std::max(0.0, config.active_min_notional_usd);
  if (active_min_notional_usd > 0.0 &&
      scaled_abs_notional + kNotionalEpsilon < active_min_notional_usd) {
    result.signal.suggested_notional_usd = 0.0;
    result.signal.direction = 0;
    result.applied = true;
    result.reason = "active_below_min_notional_to_flat";
    return result;
  }
  const double final_notional =
      static_cast<double>(shadow_direction) * scaled_abs_notional;

  if (!HasExposure(final_notional - base_signal.suggested_notional_usd)) {
    result.reason = "active_no_change";
    return result;
  }

  result.signal.suggested_notional_usd = final_notional;
  result.signal.direction = SignOf(final_notional);
  result.applied = true;
  result.reason = notional_scale >= 1.0 - kNotionalEpsilon
                      ? "active_applied_full"
                      : "active_applied_partial";
  return result;
}

}  // namespace ai_trade
