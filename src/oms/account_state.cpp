#include "oms/account_state.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace ai_trade {

namespace {
constexpr double kEpsilon = 1e-9;

double Sign(double value) {
  if (value > kEpsilon) return 1.0;
  if (value < -kEpsilon) return -1.0;
  return 0.0;
}
}  // namespace

double AccountState::EffectiveMarkPrice(const PositionState& position) {
  if (position.mark_price > kEpsilon) {
    return position.mark_price;
  }
  return position.avg_entry_price;
}

double AccountState::equity_usd() const {
  return cash_usd_ + UnrealizedPnlUsd();
}

double AccountState::cumulative_realized_pnl_usd() const {
  return cumulative_realized_pnl_usd_;
}

double AccountState::cumulative_fee_usd() const {
  return cumulative_fee_usd_;
}

double AccountState::cumulative_realized_net_pnl_usd() const {
  return cumulative_realized_pnl_usd_ - cumulative_fee_usd_;
}

double AccountState::current_notional_usd() const {
  double total = 0.0;
  for (const auto& [_, position] : positions_) {
    total += position.qty * EffectiveMarkPrice(position);
  }
  return total;
}

double AccountState::gross_notional_usd() const {
  double total = 0.0;
  for (const auto& [_, position] : positions_) {
    total += std::fabs(position.qty * EffectiveMarkPrice(position));
  }
  return total;
}

double AccountState::current_notional_usd(const std::string& symbol) const {
  const auto it = positions_.find(symbol);
  if (it == positions_.end()) return 0.0;
  return it->second.qty * EffectiveMarkPrice(it->second);
}

double AccountState::mark_price(const std::string& symbol) const {
  const auto it = positions_.find(symbol);
  if (it == positions_.end()) return 0.0;
  return it->second.mark_price;
}

double AccountState::position_qty(const std::string& symbol) const {
  const auto it = positions_.find(symbol);
  if (it == positions_.end()) return 0.0;
  return it->second.qty;
}

std::vector<std::string> AccountState::GetActiveSymbols() const {
  std::vector<std::string> out;
  out.reserve(positions_.size());
  for (const auto& [symbol, position] : positions_) {
    if (std::fabs(position.qty) <= kEpsilon) {
      continue;
    }
    out.push_back(symbol);
  }
  return out;
}

double AccountState::drawdown_pct() const {
  if (peak_equity_usd_ <= kEpsilon) return 0.0;
  const double dd = (peak_equity_usd_ - equity_usd()) / peak_equity_usd_;
  return std::max(0.0, dd);
}

double AccountState::liquidation_distance_p95() const {
  struct Sample {
    double distance;
    double weight;
  };

  std::vector<Sample> samples;
  samples.reserve(positions_.size());
  double total_weight = 0.0;

  for (const auto& [_, position] : positions_) {
    if (std::fabs(position.qty) <= kEpsilon) continue;

    const double mark = EffectiveMarkPrice(position);
    const double liq = position.liquidation_price;
    
    if (mark <= kEpsilon || liq <= kEpsilon) continue;

    double distance = (position.qty > 0.0) ? (mark - liq) : (liq - mark);
    distance = std::max(0.0, distance / mark);

    const double notional_weight = std::fabs(position.qty * mark);
    if (notional_weight <= kEpsilon) continue;

    samples.push_back({distance, notional_weight});
    total_weight += notional_weight;
  }

  if (samples.empty() || total_weight <= kEpsilon) return 1.0;

  // Sort by distance ascending
  std::sort(samples.begin(), samples.end(),
            [](const Sample& a, const Sample& b) { return a.distance < b.distance; });

  const double target_weight = total_weight * 0.95;
  double cumulative_weight = 0.0;
  
  for (const auto& sample : samples) {
    cumulative_weight += sample.weight;
    if (cumulative_weight >= target_weight - kEpsilon) {
      return sample.distance;
    }
  }
  return samples.back().distance;
}

void AccountState::RefreshPeakEquity() {
  peak_equity_usd_ = std::max(peak_equity_usd_, equity_usd());
}

double AccountState::UnrealizedPnlUsd() const {
  double unrealized = 0.0;
  for (const auto& [_, position] : positions_) {
    const double mark = EffectiveMarkPrice(position);
    unrealized += position.qty * (mark - position.avg_entry_price);
  }
  return unrealized;
}

void AccountState::OnMarket(const MarketEvent& event) {
  auto& position = positions_[event.symbol];
  if (event.mark_price > kEpsilon) {
    position.mark_price = event.mark_price;
  } else if (event.price > kEpsilon) {
    position.mark_price = event.price;
  }
  RefreshPeakEquity();
}

void AccountState::ApplyFill(const FillEvent& fill) {
  const double signed_qty = static_cast<double>(fill.direction) * fill.qty;
  if (std::fabs(signed_qty) < kEpsilon) return;

  auto& position = positions_[fill.symbol];
  cash_usd_ -= fill.fee;
  cumulative_fee_usd_ += fill.fee;

  const double old_qty = position.qty;
  const double old_abs = std::fabs(old_qty);

  // Check if increasing position (same sign or from zero)
  if (old_abs < kEpsilon || Sign(old_qty) == Sign(signed_qty)) {
    const double new_qty = old_qty + signed_qty;
    const double new_abs = std::fabs(new_qty);
    
    if (new_abs > kEpsilon) {
      if (old_abs < kEpsilon) {
        position.avg_entry_price = fill.price;
      } else {
        // Weighted Average Price
        position.avg_entry_price =
            (position.avg_entry_price * old_abs + fill.price * std::fabs(signed_qty)) /
            new_abs;
      }
    }
    position.qty = new_qty;
  } else {
    // Reducing or flipping position
    const double close_qty = std::min(old_abs, std::fabs(signed_qty));
    const double realized_pnl =
        close_qty * (fill.price - position.avg_entry_price) * Sign(old_qty);
    
    cash_usd_ += realized_pnl;
    cumulative_realized_pnl_usd_ += realized_pnl;

    position.qty = old_qty + signed_qty;
    
    if (std::fabs(position.qty) < kEpsilon) {
      position.qty = 0.0;
      position.avg_entry_price = 0.0;
    } else if (Sign(old_qty) != Sign(position.qty)) {
      // Flipped
      position.avg_entry_price = fill.price;
    }
  }

  if (fill.price > kEpsilon) {
    position.mark_price = fill.price;
  }
  RefreshPeakEquity();
}

void AccountState::SyncFromRemotePositions(
    const std::vector<RemotePositionSnapshot>& positions,
    double baseline_cash_usd) {
  positions_.clear();
  for (const auto& remote : positions) {
    if (remote.symbol.empty() || std::fabs(remote.qty) < kEpsilon) continue;

    PositionState state;
    state.qty = remote.qty;
    state.avg_entry_price = std::max(0.0, remote.avg_entry_price);
    state.mark_price = (remote.mark_price > kEpsilon) ? remote.mark_price : state.avg_entry_price;
    state.liquidation_price = std::max(0.0, remote.liquidation_price);
    positions_[remote.symbol] = state;
  }

  cash_usd_ = baseline_cash_usd;
  peak_equity_usd_ = baseline_cash_usd;
  cumulative_realized_pnl_usd_ = 0.0;
  cumulative_fee_usd_ = 0.0;
  RefreshPeakEquity();
}

void AccountState::RefreshRiskFromRemotePositions(
    const std::vector<RemotePositionSnapshot>& positions) {
  for (const auto& remote : positions) {
    if (remote.symbol.empty()) continue;

    auto it = positions_.find(remote.symbol);
    if (it == positions_.end()) {
      // Add missing position for risk tracking
      if (std::fabs(remote.qty) <= kEpsilon) continue;
      PositionState state;
      state.qty = remote.qty;
      state.avg_entry_price = std::max(0.0, remote.avg_entry_price);
      state.mark_price = (remote.mark_price > kEpsilon) ? remote.mark_price : state.avg_entry_price;
      state.liquidation_price = std::max(0.0, remote.liquidation_price);
      positions_[remote.symbol] = state;
      continue;
    }

    auto& local = it->second;
    if (remote.mark_price > kEpsilon) {
      local.mark_price = remote.mark_price;
    }
    local.liquidation_price = std::max(0.0, remote.liquidation_price);

    // Sync quantity if local is zero but remote is not (safety net)
    if (std::fabs(local.qty) <= kEpsilon && std::fabs(remote.qty) > kEpsilon) {
      local.qty = remote.qty;
      local.avg_entry_price = std::max(0.0, remote.avg_entry_price);
      if (local.mark_price <= kEpsilon) {
        local.mark_price = local.avg_entry_price;
      }
    }
  }
  RefreshPeakEquity();
}

void AccountState::ForceSyncPositionsFromRemote(
    const std::vector<RemotePositionSnapshot>& positions) {
  std::unordered_map<std::string, PositionState> synced;
  for (const auto& remote : positions) {
    if (remote.symbol.empty() || std::fabs(remote.qty) < kEpsilon) continue;

    PositionState state;
    state.qty = remote.qty;
    state.avg_entry_price = std::max(0.0, remote.avg_entry_price);
    state.mark_price = (remote.mark_price > kEpsilon) ? remote.mark_price : state.avg_entry_price;
    state.liquidation_price = std::max(0.0, remote.liquidation_price);
    synced[remote.symbol] = state;
  }
  positions_.swap(synced);
  RefreshPeakEquity();
}

void AccountState::SyncFromRemoteAccountBalance(
    const RemoteAccountBalanceSnapshot& balance,
    bool reset_peak_to_equity) {
  if (!balance.has_equity && !balance.has_wallet_balance) return;

  if (balance.has_equity) {
    cash_usd_ = balance.equity_usd - UnrealizedPnlUsd();
  } else if (balance.has_wallet_balance) {
    cash_usd_ = balance.wallet_balance_usd;
  }

  if (balance.has_equity) {
    if (reset_peak_to_equity) {
      peak_equity_usd_ = std::max(balance.equity_usd, kEpsilon);
    } else {
      peak_equity_usd_ = std::max(peak_equity_usd_, balance.equity_usd);
    }
  } else if (reset_peak_to_equity) {
    peak_equity_usd_ = std::max(equity_usd(), kEpsilon);
  }
}

}  // namespace ai_trade
