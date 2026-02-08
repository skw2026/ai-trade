#include "oms/account_state.h"

#include <algorithm>
#include <cmath>

namespace ai_trade {

namespace {

constexpr double kEpsilon = 1e-9;

double Sign(double value) {
  if (value > 0.0) {
    return 1.0;
  }
  if (value < 0.0) {
    return -1.0;
  }
  return 0.0;
}

}  // namespace

double AccountState::EffectiveMarkPrice(const PositionState& position) {
  if (position.mark_price > 0.0) {
    return position.mark_price;
  }
  return position.avg_entry_price;
}

double AccountState::equity_usd() const {
  double unrealized = 0.0;
  for (const auto& [symbol, position] : positions_) {
    (void)symbol;
    const double mark = EffectiveMarkPrice(position);
    unrealized += position.qty * (mark - position.avg_entry_price);
  }
  return cash_usd_ + unrealized;
}

double AccountState::current_notional_usd() const {
  double total = 0.0;
  for (const auto& [symbol, position] : positions_) {
    (void)symbol;
    total += position.qty * EffectiveMarkPrice(position);
  }
  return total;
}

double AccountState::current_notional_usd(const std::string& symbol) const {
  const auto it = positions_.find(symbol);
  if (it == positions_.end()) {
    return 0.0;
  }
  return it->second.qty * EffectiveMarkPrice(it->second);
}

double AccountState::mark_price(const std::string& symbol) const {
  const auto it = positions_.find(symbol);
  if (it == positions_.end()) {
    return 0.0;
  }
  return it->second.mark_price;
}

double AccountState::position_qty(const std::string& symbol) const {
  const auto it = positions_.find(symbol);
  if (it == positions_.end()) {
    return 0.0;
  }
  return it->second.qty;
}

double AccountState::drawdown_pct() const {
  if (peak_equity_usd_ <= 0.0) {
    return 0.0;
  }
  const double dd = (peak_equity_usd_ - equity_usd()) / peak_equity_usd_;
  return std::max(0.0, dd);
}

void AccountState::RefreshPeakEquity() {
  peak_equity_usd_ = std::max(peak_equity_usd_, equity_usd());
}

void AccountState::OnMarket(const MarketEvent& event) {
  auto& position = positions_[event.symbol];
  if (event.mark_price > 0.0) {
    position.mark_price = event.mark_price;
  } else if (event.price > 0.0) {
    position.mark_price = event.price;
  }
  RefreshPeakEquity();
}

void AccountState::ApplyFill(const FillEvent& fill) {
  const double signed_qty = static_cast<double>(fill.direction) * fill.qty;
  if (std::fabs(signed_qty) < kEpsilon) {
    return;
  }

  auto& position = positions_[fill.symbol];

  cash_usd_ -= fill.fee;
  const double old_qty = position.qty;
  const double old_abs = std::fabs(old_qty);

  if (old_abs < kEpsilon || Sign(old_qty) == Sign(signed_qty)) {
    const double new_qty = old_qty + signed_qty;
    const double new_abs = std::fabs(new_qty);
    if (new_abs > kEpsilon) {
      if (old_abs < kEpsilon) {
        position.avg_entry_price = fill.price;
      } else {
        position.avg_entry_price =
            (position.avg_entry_price * old_abs +
             fill.price * std::fabs(signed_qty)) /
            new_abs;
      }
    }
    position.qty = new_qty;
  } else {
    const double close_qty = std::min(old_abs, std::fabs(signed_qty));
    const double realized_pnl =
        close_qty * (fill.price - position.avg_entry_price) * Sign(old_qty);
    cash_usd_ += realized_pnl;

    position.qty = old_qty + signed_qty;
    if (std::fabs(position.qty) < kEpsilon) {
      position.qty = 0.0;
      position.avg_entry_price = 0.0;
    } else if (Sign(old_qty) != Sign(position.qty)) {
      position.avg_entry_price = fill.price;
    }
  }

  if (fill.price > 0.0) {
    position.mark_price = fill.price;
  }
  RefreshPeakEquity();
}

void AccountState::SyncFromRemotePositions(
    const std::vector<RemotePositionSnapshot>& positions,
    double baseline_cash_usd) {
  positions_.clear();
  for (const auto& remote : positions) {
    if (remote.symbol.empty() || std::fabs(remote.qty) < kEpsilon) {
      continue;
    }

    PositionState state;
    state.qty = remote.qty;
    state.avg_entry_price = std::max(0.0, remote.avg_entry_price);
    state.mark_price = remote.mark_price > 0.0
                           ? remote.mark_price
                           : state.avg_entry_price;
    positions_[remote.symbol] = state;
  }

  cash_usd_ = baseline_cash_usd;
  peak_equity_usd_ = baseline_cash_usd;
  RefreshPeakEquity();
}

}  // namespace ai_trade
