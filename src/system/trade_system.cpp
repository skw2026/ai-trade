#include "system/trade_system.h"

#include <algorithm>
#include <cmath>

#include "core/log.h"

namespace ai_trade {

namespace {

constexpr double kWeightEpsilon = 1e-9;
constexpr double kNotionalEpsilon = 1e-6;

bool HasExposure(double notional_usd) {
  return std::fabs(notional_usd) > kNotionalEpsilon;
}

int SignOf(double value) {
  if (value > kNotionalEpsilon) {
    return 1;
  }
  if (value < -kNotionalEpsilon) {
    return -1;
  }
  return 0;
}

}  // namespace

std::size_t TradeSystem::BucketIndex(RegimeBucket bucket) {
  switch (bucket) {
    case RegimeBucket::kTrend:
      return 0;
    case RegimeBucket::kRange:
      return 1;
    case RegimeBucket::kExtreme:
      return 2;
  }
  return 1;
}

/**
 * @brief 本地回放便捷入口
 *
 * 用于最小闭环验证：当策略产生意图时，直接构造模拟 Fill 回放到账户。
 */
bool TradeSystem::OnPrice(double price, bool trade_ok) {
  const MarketEvent event = market_.Next(price);
  const auto decision = Evaluate(event, trade_ok);
  auto intent = decision.intent;
  if (!intent.has_value()) {
    return false;
  }

  FillEvent fill;
  fill.fill_id = intent->client_order_id + "-sim-fill";
  fill.client_order_id = intent->client_order_id;
  fill.symbol = intent->symbol;
  fill.direction = intent->direction;
  fill.qty = intent->qty;
  fill.price = intent->price;

  // 便捷模式下将意图直接当作成交回报，便于快速验证闭环。
  OnFill(fill);
  LogInfo("骨架模式：订单已成交");
  return true;
}

/**
 * @brief 标准决策流水线
 *
 * 处理顺序：
 * 1) 用行情更新账户估值；
 * 2) 策略产出原始信号；
 * 3) 风控修正目标仓位；
 * 4) 应用账户级总名义敞口预算裁剪；
 * 5) 执行层生成订单意图。
 */
MarketDecision TradeSystem::Evaluate(const MarketEvent& event, bool trade_ok) {
  MarketDecision decision;
  account_.OnMarket(event);
  decision.regime = regime_.OnMarket(event);
  decision.base_signal = strategy_.OnMarket(event, account_, decision.regime);
  if (decision.base_signal.symbol.empty()) {
    decision.base_signal.symbol = event.symbol;
  }
  decision.signal = decision.base_signal;
  integrator_shadow_.OnMarket(event);
  decision.shadow = integrator_shadow_.Infer(decision.base_signal, decision.regime);
  decision.integrator_policy_applied = ApplyIntegratorPolicy(
      decision.shadow,
      &decision.signal,
      &decision.integrator_confidence,
      &decision.integrator_policy_reason);
  decision.target =
      TargetPosition{decision.signal.symbol, decision.signal.suggested_notional_usd};
  if (evolution_enabled_) {
    // Regime 分桶权重：按当前 bucket 读取 trend 权重缩放目标名义值。
    const auto bucket_weights = evolution_weights(decision.regime.bucket);
    decision.target.target_notional_usd *= bucket_weights.trend_weight;
  }
  
  // 从账户状态读取“强平距离加权 P95”（由远端持仓同步提供基础数据）。
  const double liq_dist = account_.liquidation_distance_p95();
  decision.risk_adjusted =
      risk_.Apply(decision.target, trade_ok, account_.drawdown_pct(), liq_dist);

  // 多币种账户级风险预算（gross）：
  // 先计算“其他币对已占用的总名义敞口”，再限制当前币对可用上限。
  const double symbol_current_notional =
      account_.current_notional_usd(decision.risk_adjusted.symbol);
  const double gross_notional = account_.gross_notional_usd();
  const double other_symbols_gross =
      std::max(0.0, gross_notional - std::fabs(symbol_current_notional));
  const double symbol_budget =
      std::max(0.0, max_account_gross_notional_usd_ - other_symbols_gross);
  if (std::fabs(decision.risk_adjusted.adjusted_notional_usd) > symbol_budget) {
    decision.risk_adjusted.adjusted_notional_usd = std::clamp(
        decision.risk_adjusted.adjusted_notional_usd, -symbol_budget, symbol_budget);
  }

  decision.intent = execution_.BuildIntent(decision.risk_adjusted,
                                           symbol_current_notional,
                                           event.price);
  return decision;
}

bool TradeSystem::SetEvolutionWeights(double trend_weight,
                                      double defensive_weight,
                                      std::string* out_error) {
  for (RegimeBucket bucket :
       {RegimeBucket::kTrend, RegimeBucket::kRange, RegimeBucket::kExtreme}) {
    if (!SetEvolutionWeightsForBucket(bucket,
                                      trend_weight,
                                      defensive_weight,
                                      out_error)) {
      return false;
    }
  }
  return true;
}

bool TradeSystem::SetEvolutionWeightsForBucket(RegimeBucket bucket,
                                               double trend_weight,
                                               double defensive_weight,
                                               std::string* out_error) {
  const double sum = trend_weight + defensive_weight;
  if (trend_weight < -kWeightEpsilon || defensive_weight < -kWeightEpsilon) {
    if (out_error != nullptr) {
      *out_error = "权重不能为负数";
    }
    return false;
  }
  if (std::fabs(sum - 1.0) > 1e-6) {
    if (out_error != nullptr) {
      *out_error = "权重和必须为1.0";
    }
    return false;
  }
  evolution_weights_by_bucket_[BucketIndex(bucket)] =
      EvolutionWeights{trend_weight, defensive_weight};
  return true;
}

EvolutionWeights TradeSystem::evolution_weights(
    RegimeBucket bucket) const {
  return evolution_weights_by_bucket_[BucketIndex(bucket)];
}

std::array<EvolutionWeights, 3> TradeSystem::evolution_weights_all()
    const {
  return evolution_weights_by_bucket_;
}

bool TradeSystem::ApplyIntegratorPolicy(const ShadowInference& shadow,
                                        Signal* inout_signal,
                                        double* out_confidence,
                                        std::string* out_reason) const {
  if (inout_signal == nullptr) {
    return false;
  }

  auto set_reason = [&](const std::string& reason) {
    if (out_reason != nullptr) {
      *out_reason = reason;
    }
  };
  auto set_confidence = [&](double confidence) {
    if (out_confidence != nullptr) {
      *out_confidence = confidence;
    }
  };

  set_confidence(0.0);
  if (integrator_config_.mode == IntegratorMode::kOff) {
    set_reason("mode_off");
    return false;
  }
  if (integrator_config_.mode == IntegratorMode::kShadow) {
    set_reason("mode_shadow_observe_only");
    return false;
  }
  if (!shadow.enabled) {
    set_reason("shadow_unavailable");
    return false;
  }
  if (!HasExposure(inout_signal->suggested_notional_usd)) {
    set_reason("flat_base_signal");
    return false;
  }

  const double confidence = shadow.p_up - shadow.p_down;
  const double confidence_abs = std::fabs(confidence);
  const int shadow_direction = SignOf(confidence);
  const int base_direction = SignOf(inout_signal->suggested_notional_usd);
  const double base_abs_notional = std::fabs(inout_signal->suggested_notional_usd);
  set_confidence(confidence);

  if (shadow_direction == 0) {
    set_reason("neutral_confidence");
    return false;
  }

  if (integrator_config_.mode == IntegratorMode::kCanary) {
    if (confidence_abs < integrator_config_.canary_confidence_threshold) {
      set_reason("canary_low_confidence");
      return false;
    }
    if (!integrator_config_.canary_allow_countertrend &&
        shadow_direction != base_direction) {
      set_reason("canary_countertrend_blocked");
      return false;
    }
    const double canary_ratio =
        std::clamp(integrator_config_.canary_notional_ratio, 0.0, 1.0);
    const double final_notional =
        static_cast<double>(shadow_direction) * base_abs_notional * canary_ratio;
    if (!HasExposure(final_notional - inout_signal->suggested_notional_usd)) {
      set_reason("canary_no_change");
      return false;
    }
    inout_signal->suggested_notional_usd = final_notional;
    inout_signal->direction = SignOf(final_notional);
    set_reason("canary_applied");
    return true;
  }

  // active: Integrator 主接管。低置信度直接转为空仓，高置信度按置信度缩放仓位。
  if (confidence_abs < integrator_config_.active_confidence_threshold) {
    if (!HasExposure(inout_signal->suggested_notional_usd)) {
      set_reason("active_low_confidence_no_change");
      return false;
    }
    inout_signal->suggested_notional_usd = 0.0;
    inout_signal->direction = 0;
    set_reason("active_low_confidence_to_flat");
    return true;
  }

  const double scaled_abs_notional = base_abs_notional * confidence_abs;
  const double final_notional =
      static_cast<double>(shadow_direction) * scaled_abs_notional;
  if (!HasExposure(final_notional - inout_signal->suggested_notional_usd)) {
    set_reason("active_no_change");
    return false;
  }
  inout_signal->suggested_notional_usd = final_notional;
  inout_signal->direction = SignOf(final_notional);
  set_reason("active_applied");
  return true;
}

}  // namespace ai_trade
