// Deterministic source-behaviour diagnostic, NOT a strategy or market replay.
// Links the unmodified controller. Expected discrepancies are observations,
// not assertions that the production learning/execution contract is satisfied.
#include <array>
#include <cmath>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include "evolution/self_evolution_controller.h"

using namespace ai_trade;

static void require(bool ok, const std::string& reason) {
  if (!ok) throw std::runtime_error(reason);
}

static void close(double observed, double expected) {
  require(std::isfinite(observed) && std::abs(observed - expected) < 1e-10,
          "unexpected native result");
}

static SelfEvolutionAction observe(RegimeBucket first, RegimeBucket second,
                                   double p1, double p2, double cost_bps,
                                   bool filtered = false) {
  SelfEvolutionConfig c;
  c.enabled = true;
  c.use_virtual_pnl = true;
  c.update_interval_ticks = 2;
  c.min_update_interval_ticks = 100;
  c.min_abs_window_pnl_usd = 1000;  // Observe, do not update any weight.
  c.virtual_cost_bps = cost_bps;
  c.virtual_cost_dynamic_enabled = false;
  SelfEvolutionController controller(c);
  std::string error;
  require(controller.Initialize(0, 10000, {.5, .5}, &error), error);
  const std::array<EvolutionWeights, 3> weights{{{.6, .4}, {.4, .6}, {.5, .5}}};
  require(controller.RestoreCurrentWeights(weights, &error), error);
  auto a = controller.OnTick(1, 0, first, 0, 0, 100, 0, p1, "SYNTH",
                             filtered, 0, 10000, 0, 0);
  require(!a, "premature assessment");
  a = controller.OnTick(2, 0, second, 0, 0, 100, 0, p2, "SYNTH",
                         filtered, 0, 10000, 0, 0);
  require(a.has_value() && a->used_virtual_pnl &&
              a->type == SelfEvolutionActionType::kSkipped &&
              a->reason_code == "EVOLUTION_WINDOW_PNL_TOO_SMALL",
          "diagnostic must observe without updating");
  close(a->trend_weight_before, a->trend_weight_after);
  return *a;
}

static void emit(const char* name, double proxy, double comparator,
                 const char* comparator_kind) {
  std::cout << "{\"case\":\"" << name << "\",\"native_virtual\":" << proxy
            << ",\"comparator\":" << comparator
            << ",\"comparator_kind\":\"" << comparator_kind
            << "\",\"diagnostic_assertion\":\"PASS\"}\n";
}

int main() {
  try {
    std::cout << std::setprecision(17);
    auto a = observe(RegimeBucket::kTrend, RegimeBucket::kTrend, 100, 101, 0);
    close(a.window_virtual_pnl_usd, .6);
    emit("same_bucket_positive_control", a.window_virtual_pnl_usd, .6,
         "hypothetical_prior_60_notional_gross");

    a = observe(RegimeBucket::kTrend, RegimeBucket::kRange, 100, 100, 10);
    close(a.window_virtual_pnl_usd, 0);
    emit("bucket_switch_flat_price", a.window_virtual_pnl_usd, -.02,
         "hypothetical_60_to_40_switch_cost_at_10bps");

    a = observe(RegimeBucket::kTrend, RegimeBucket::kRange, 100, 101, 0);
    close(a.window_virtual_pnl_usd, .4);
    emit("bucket_switch_return", a.window_virtual_pnl_usd, .6,
         "hypothetical_prior_60_notional_gross");

    a = observe(RegimeBucket::kTrend, RegimeBucket::kTrend, 100, 101, 10, true);
    close(a.window_virtual_pnl_usd, .6);
    close(a.window_realized_pnl_usd, 0);
    require(a.window_fill_count == 0 && a.window_cost_filtered_signals == 2,
            "no-fill observation identity");
    emit("filtered_no_fill", a.window_virtual_pnl_usd, a.window_realized_pnl_usd,
         "supplied_realized_net_zero_not_exchange_receipt");

    a = observe(RegimeBucket::kTrend, RegimeBucket::kTrend, 100, 100, 10);
    close(a.window_virtual_pnl_usd, 0);
    emit("first_signal_entry", a.window_virtual_pnl_usd, -.06,
         "hypothetical_empty_to_60_entry_cost_at_10bps");
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "DIAGNOSTIC_FAILED: " << e.what() << '\n';
    return 1;
  }
}
