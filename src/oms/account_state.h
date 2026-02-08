#pragma once

#include <string>
#include <unordered_map>
#include <vector>

#include "core/types.h"

namespace ai_trade {

struct PositionState {
  double qty{0.0};
  double avg_entry_price{0.0};
  double mark_price{0.0};
};

class AccountState {
 public:
  void OnMarket(const MarketEvent& event);
  void ApplyFill(const FillEvent& fill);
  void SyncFromRemotePositions(const std::vector<RemotePositionSnapshot>& positions,
                               double baseline_cash_usd = 10000.0);

  double current_notional_usd() const;
  double current_notional_usd(const std::string& symbol) const;
  double equity_usd() const;
  double drawdown_pct() const;
  double mark_price() const { return mark_price("BTCUSDT"); }
  double mark_price(const std::string& symbol) const;
  double position_qty() const { return position_qty("BTCUSDT"); }
  double position_qty(const std::string& symbol) const;

 private:
  void RefreshPeakEquity();
  static double EffectiveMarkPrice(const PositionState& position);

  double cash_usd_{10000.0};
  double peak_equity_usd_{10000.0};
  std::unordered_map<std::string, PositionState> positions_;
};

}  // namespace ai_trade
