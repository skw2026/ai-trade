// Offline research protocol only. No exchange, config loading or account access.
// Python owns historical execution/risk and sends one completed bar at a time.
#include <cmath>
#include <array>
#include <algorithm>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include "evolution/self_evolution_controller.h"
#include "regime/regime_engine.h"
#include "strategy/strategy_engine.h"

int main(int argc, char** argv) {
  using namespace ai_trade;
  try {
    if (argc < 2 || (std::string(argv[1]) != "development" &&
                      std::string(argv[1]) != "frozen"))
      throw std::runtime_error("development or frozen diagnostic mode required; no confirmation authority");
    const bool learn = std::string(argv[1]) == "development";
    if ((learn && argc != 2) || (!learn && argc != 5))
      throw std::runtime_error("frozen mode requires exactly three pinned regime weights");
    std::array<double,3> frozen{0.5,0.5,0.5};
    if (!learn) for(int i=0;i<3;++i) {
      std::size_t parsed=0;
      frozen[i]=std::stod(argv[i+2],&parsed);
      if(parsed!=std::string(argv[i+2]).size() || !std::isfinite(frozen[i]) ||
         frozen[i]<.4-1e-9 || frozen[i]>.6+1e-9 ||
         std::fabs(frozen[i]*20-std::round(frozen[i]*20))>1e-8)
        throw std::runtime_error("invalid frozen weight");
    }
    StrategyConfig s;
    s.signal_notional_usd = 2500;
    s.vol_target_pct = 0;
    s.default_tick_interval_ms = s.signal_valid_for_ms = 300000;
    s.defensive_notional_ratio = 1;
    s.defensive_rank_lookback_ticks = 20;
    StrategyEngine strategy(s);
    RegimeConfig r; r.bar_interval_ms = 300000;
    RegimeEngine regime(r);
    AccountState account;  // Fixed reference equity, never connected to an account.
    SelfEvolutionConfig c;
    c.enabled = learn;
    c.safety_withdrawal_enabled = true;
    c.clock_tick_interval_ms = 300000;
    c.update_interval_ticks = 240;
    c.min_update_interval_ticks = 72;
    c.use_virtual_pnl = c.use_counterfactual_search = true;
    c.counterfactual_require_temporal_holdout = true;
    c.counterfactual_superiority_min_samples_for_update = 10;
    c.counterfactual_superiority_min_t_stat_for_update = 1.5;
    c.enable_learnability_gate = true;
    c.virtual_cost_bps = 6.5;
    SelfEvolutionController controller(c);
    std::int64_t last = 0, tick = 0;
    std::string line;
    std::cout << std::setprecision(17);
    while (std::getline(std::cin, line)) {
      std::istringstream input(line);
      MarketEvent e;
      double rate = 0, dd = 0, equity = 0;
      int active = 0;
      if (!(input >> e.ts_ms >> e.open_price >> e.high_price >> e.low_price
                  >> e.price >> e.volume >> e.mark_price >> rate >> active >> dd >> equity))
        throw std::runtime_error("invalid protocol row");
      std::string extra;
      if (input >> extra) throw std::runtime_error("unexpected protocol fields");
      if (e.ts_ms <= 0 || (last && e.ts_ms != last+300000) ||
          (active != 0 && active != 1)) throw std::runtime_error("clock or domain violation");
      for (double x : {e.open_price,e.high_price,e.low_price,e.price,e.mark_price,equity})
        if (!std::isfinite(x) || x <= 0) throw std::runtime_error("invalid price/equity");
      if (!std::isfinite(e.volume) || e.volume < 0 || !std::isfinite(rate) ||
          !std::isfinite(dd) || dd < 0) throw std::runtime_error("invalid observation");
      if (e.low_price > std::min(e.price,e.open_price) ||
          e.high_price < std::max(e.price,e.open_price)) throw std::runtime_error("OHLC");
      last = e.ts_ms;
      e.interval_ms = 300000;
      e.completed_bar = true;
      const auto bucket = regime.OnMarket(e);
      const auto signal = strategy.OnMarket(e, account, bucket);
      std::optional<SelfEvolutionAction> a;
      if (active && learn) {
        if (!controller.initialized()) {
          std::string error;
          if (!controller.Initialize(0,10000,{0.5,0.5},&error,0,e.ts_ms))
            throw std::runtime_error(error);
        }
        a = controller.OnTick(tick,0,bucket.bucket,dd,0,
             signal.trend_notional_usd,signal.defensive_notional_usd,
             e.mark_price,"BTCUSDT",false,0,equity,6.5,rate,e.ts_ms);
      }
      const auto weights = controller.current_weights(bucket.bucket);
      const double w = learn ? (controller.initialized()?weights.trend_weight:0.5)
                             : frozen[static_cast<int>(bucket.bucket)];
      std::cout << "{\"ts\":" << e.ts_ms << ",\"trend\":" << signal.trend_notional_usd
                << ",\"defensive\":" << signal.defensive_notional_usd
                << ",\"weight\":" << w << ",\"bucket\":" << static_cast<int>(bucket.bucket)
                << ",\"withdrawn\":" << (controller.safety_withdrawn()?"true":"false")
                << ",\"action\":\"" << (a?a->reason_code:"") << "\",\"updated\":"
                << (a && a->type == SelfEvolutionActionType::kUpdated?"true":"false")
                << ",\"train_samples\":" << (a?a->counterfactual_train_samples:0)
                << ",\"holdout_samples\":" << (a?a->counterfactual_holdout_samples:0)
                << ",\"learnability_samples\":" << (a?a->learnability_samples:0)
                << ",\"virtual_pnl\":" << (a?a->window_virtual_pnl_usd:0)
                << ",\"weights\":[";
      for (int i=0;i<3;++i) {
        if(i) std::cout << ',';
        std::cout << (learn ? (controller.initialized()
          ? controller.current_weights(static_cast<RegimeBucket>(i)).trend_weight : 0.5) : frozen[i]);
      }
      std::cout << "]}" << std::endl;
      ++tick;
    }
    return 0;
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 2;
  }
}
