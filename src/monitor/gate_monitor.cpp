#include "monitor/gate_monitor.h"

#include <algorithm>
#include <cmath>

namespace ai_trade {

namespace {

constexpr double kNotionalEpsilon = 1e-6;

}  // namespace

bool GateMonitor::HasExposure(double notional_usd) {
  return std::fabs(notional_usd) > kNotionalEpsilon;
}

bool GateMonitor::IsPolicyFlatSignal(
    const Signal& signal,
    const RiskAdjustedPosition& adjusted,
    const std::optional<OrderIntent>& intent) {
  if (HasExposure(signal.suggested_notional_usd) ||
      HasExposure(adjusted.adjusted_notional_usd) || intent.has_value()) {
    return false;
  }
  const auto has_reason = [&signal](const char* code) {
    return std::find(signal.reason_codes.begin(),
                     signal.reason_codes.end(),
                     std::string(code)) != signal.reason_codes.end();
  };
  return has_reason("STR_RANGE_CONFIDENCE_BLOCK") ||
         has_reason("STR_EXTREME_BLOCK");
}

/**
 * @brief 处理每一帧决策结果
 * 统计原始信号、有效信号（风控后）和下单意图，用于活跃度评估。
 */
std::optional<std::string> GateMonitor::OnDecision(
    const Signal& signal,
    const RiskAdjustedPosition& adjusted,
    const std::optional<OrderIntent>& intent) {
  if (HasExposure(signal.suggested_notional_usd)) {
    ++raw_signals_;
  }
  if (intent.has_value()) {
    ++order_intents_;
  }
  if (IsPolicyFlatSignal(signal, adjusted, intent)) {
    ++policy_flat_signals_;
  }

  std::optional<std::string> alert;
  if (HasExposure(adjusted.adjusted_notional_usd)) {
    ++effective_signals_;
    no_effective_signal_ticks_ = 0;
  } else {
    // 连续 N 个 tick 无有效信号，触发心跳告警 (Dead Silence Check)
    ++no_effective_signal_ticks_;
    if (config_.heartbeat_empty_signal_ticks > 0 &&
        no_effective_signal_ticks_ == config_.heartbeat_empty_signal_ticks) {
      alert = "WARN_SIGNAL_HEARTBEAT_GAP";
    }
  }
  return alert;
}

void GateMonitor::OnFill(const FillEvent& fill) {
  if (fill.fill_id.empty()) {
    return;
  }
  ++fills_;
}

/**
 * @brief 周期性 Gate 检查 (Window Check)
 * 检查当前窗口内的活跃度是否满足最小要求 (Min Activity)。
 */
std::optional<GateWindowResult> GateMonitor::OnTick() {
  ++tick_in_window_;
  if (config_.window_ticks <= 0 || tick_in_window_ < config_.window_ticks) {
    return std::nullopt;
  }

  GateWindowResult result;
  result.raw_signals = raw_signals_;
  result.order_intents = order_intents_;
  result.effective_signals = effective_signals_;
  result.fills = fills_;
  result.policy_flat_signals = policy_flat_signals_;

  const bool policy_flat_pass =
      config_.allow_policy_flat_windows &&
      policy_flat_signals_ >= config_.min_effective_signals_per_window &&
      raw_signals_ == 0 && order_intents_ == 0 && fills_ == 0;
  result.policy_flat_pass = policy_flat_pass;

  // 检查 1: 有效信号数量是否达标 (防止策略“装死”)
  if (!policy_flat_pass &&
      effective_signals_ < config_.min_effective_signals_per_window) {
    result.pass = false;
    result.fail_reasons.push_back("FAIL_LOW_ACTIVITY_SIGNALS");
  }
  // 检查 2: 成交数量是否达标 (防止策略只发信号不成交)
  if (!policy_flat_pass && fills_ < config_.min_fills_per_window) {
    result.pass = false;
    result.fail_reasons.push_back("FAIL_LOW_ACTIVITY_FILLS");
  }

  ResetWindow();
  return result;
}

void GateMonitor::ResetWindow() {
  tick_in_window_ = 0;
  raw_signals_ = 0;
  order_intents_ = 0;
  effective_signals_ = 0;
  fills_ = 0;
  policy_flat_signals_ = 0;
}

}  // namespace ai_trade
