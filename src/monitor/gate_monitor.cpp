#include "monitor/gate_monitor.h"

#include <cmath>

namespace ai_trade {

namespace {

constexpr double kNotionalEpsilon = 1e-6;

}  // namespace

bool GateMonitor::HasExposure(double notional_usd) {
  return std::fabs(notional_usd) > kNotionalEpsilon;
}

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

  std::optional<std::string> alert;
  if (HasExposure(adjusted.adjusted_notional_usd)) {
    ++effective_signals_;
    no_effective_signal_ticks_ = 0;
  } else {
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

  if (effective_signals_ < config_.min_effective_signals_per_window) {
    result.pass = false;
    result.fail_reasons.push_back("FAIL_LOW_ACTIVITY_SIGNALS");
  }
  if (fills_ < config_.min_fills_per_window) {
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
}

}  // namespace ai_trade
