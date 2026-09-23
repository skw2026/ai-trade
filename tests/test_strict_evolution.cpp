// TEST_ONLY deterministic counterfactual selection; no exchange or account.
#include <cmath>
#include <iostream>
#include <stdexcept>
#include "evolution/self_evolution_controller.h"

namespace {
using namespace ai_trade;
void Check(bool ok, const char* why) { if (!ok) throw std::runtime_error(why); }

SelfEvolutionAction Window(double initial, bool holdout_reversal, int interval = 30) {
  SelfEvolutionConfig config;
  config.enabled = config.use_virtual_pnl = config.use_counterfactual_search = true;
  config.counterfactual_require_temporal_holdout = true;
  config.counterfactual_train_fraction = 0.5;
  config.update_interval_ticks = interval;
  config.min_update_interval_ticks = 0;
  config.counterfactual_fallback_to_factor_ic = config.enable_factor_ic_adaptive_weights = true;
  SelfEvolutionController controller(config);
  std::string error;
  Check(controller.Initialize(0, 10000, {initial, 1 - initial}, &error), error.c_str());
  std::optional<SelfEvolutionAction> final;
  double price = 100;
  for (int tick = 1; tick <= interval; ++tick) {
    price *= (holdout_reversal && tick > interval / 2) ? 0.995 : 1.001;
    auto action = controller.OnTick(tick, 0, RegimeBucket::kRange, 0, 0,
                                    80, 0, price, "SYNTH", false, 0, 10000);
    Check(action.has_value() == (tick == interval), "unexpected assessment");
    if (action) final = action;
  }
  Check(final.has_value(), "missing assessment");
  Check(!final->counterfactual_fallback_to_factor_ic_enabled &&
        !final->counterfactual_fallback_to_factor_ic_used, "strict fallback enabled");
  Check(std::abs(final->trend_weight_after - initial) <= 0.050000001, "step limit bypassed");
  return *final;
}
}

int main() {
  try {
    const auto positive = Window(0.5, false);
    Check(positive.type == SelfEvolutionActionType::kUpdated &&
          std::abs(positive.trend_weight_after - 0.55) < 1e-9 &&
          std::abs(positive.counterfactual_best_trend_weight - 0.55) < 1e-9,
          "selected/applied different candidate or unreachable global winner");
    const auto off_grid = Window(0.52, false);
    Check(off_grid.type == SelfEvolutionActionType::kUpdated &&
          std::abs(off_grid.trend_weight_after - 0.55) < 1e-9,
          "off-grid restored weight bypassed reachable selection");
    const auto reversal = Window(0.5, true);
    Check(reversal.counterfactual_best_trend_weight == positive.counterfactual_best_trend_weight &&
          reversal.type == SelfEvolutionActionType::kSkipped &&
          reversal.reason_code == "EVOLUTION_COUNTERFACTUAL_IMPROVEMENT_TOO_SMALL" &&
          reversal.trend_weight_after == 0.5, "holdout leakage or adverse holdout accepted");
    const auto short_window = Window(0.5, false, 12);
    Check(short_window.reason_code == "EVOLUTION_COUNTERFACTUAL_HOLDOUT_INSUFFICIENT" &&
          short_window.trend_weight_after == 0.5, "sample shortage did not freeze");
    std::cout << "strict reachable-selection, off-grid, holdout and freeze PASS\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
