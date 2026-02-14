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
  // 账户权益 = 现金 + 各仓位未实现盈亏。
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
  for (const auto& [symbol, position] : positions_) {
    (void)symbol;
    // 净名义敞口（signed）：多仓为正，空仓为负。
    total += position.qty * EffectiveMarkPrice(position);
  }
  return total;
}

double AccountState::gross_notional_usd() const {
  double total = 0.0;
  for (const auto& [symbol, position] : positions_) {
    (void)symbol;
    // 总名义敞口（gross）：逐 symbol 取绝对值后求和。
    total += std::fabs(position.qty * EffectiveMarkPrice(position));
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

std::vector<std::string> AccountState::symbols() const {
  std::vector<std::string> out;
  out.reserve(positions_.size());
  for (const auto& [symbol, position] : positions_) {
    (void)position;
    out.push_back(symbol);
  }
  return out;
}

double AccountState::drawdown_pct() const {
  if (peak_equity_usd_ <= 0.0) {
    return 0.0;
  }
  const double dd = (peak_equity_usd_ - equity_usd()) / peak_equity_usd_;
  return std::max(0.0, dd);
}

double AccountState::liquidation_distance_p95() const {
  struct Sample {
    double distance{0.0};
    double weight{0.0};
  };

  std::vector<Sample> samples;
  samples.reserve(positions_.size());
  double total_weight = 0.0;

  for (const auto& [symbol, position] : positions_) {
    (void)symbol;
    if (std::fabs(position.qty) <= kEpsilon) {
      continue;
    }

    const double mark = EffectiveMarkPrice(position);
    const double liq = position.liquidation_price;
    if (mark <= 0.0 || liq <= 0.0) {
      continue;
    }

    double distance = 0.0;
    if (position.qty > 0.0) {
      distance = (mark - liq) / mark;
    } else {
      distance = (liq - mark) / mark;
    }
    distance = std::max(0.0, distance);

    const double notional_weight = std::fabs(position.qty * mark);
    if (notional_weight <= kEpsilon) {
      continue;
    }

    samples.push_back(Sample{distance, notional_weight});
    total_weight += notional_weight;
  }

  if (samples.empty() || total_weight <= kEpsilon) {
    return 1.0;
  }

  std::sort(samples.begin(), samples.end(),
            [](const Sample& lhs, const Sample& rhs) {
              return lhs.distance < rhs.distance;
            });
  const double target_weight = total_weight * 0.95;
  double cumulative_weight = 0.0;
  for (const auto& sample : samples) {
    cumulative_weight += sample.weight;
    if (cumulative_weight + kEpsilon >= target_weight) {
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
  for (const auto& [symbol, position] : positions_) {
    (void)symbol;
    const double mark = EffectiveMarkPrice(position);
    unrealized += position.qty * (mark - position.avg_entry_price);
  }
  return unrealized;
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
  cumulative_fee_usd_ += fill.fee;
  const double old_qty = position.qty;
  const double old_abs = std::fabs(old_qty);

  if (old_abs < kEpsilon || Sign(old_qty) == Sign(signed_qty)) {
    // 同向成交：加仓或新开仓，按加权均价更新持仓成本。
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
    // 反向成交：先按可平部分结算已实现盈亏，再判断是否反手。
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
    if (remote.symbol.empty()) {
      continue;
    }

    auto it = positions_.find(remote.symbol);
    if (it == positions_.end()) {
      // 本地不存在该 symbol，但远端有仓位：补录到风险视图，避免遗漏强平风险。
      if (std::fabs(remote.qty) <= kEpsilon) {
        continue;
      }
      PositionState state;
      state.qty = remote.qty;
      state.avg_entry_price = std::max(0.0, remote.avg_entry_price);
      state.mark_price =
          remote.mark_price > 0.0 ? remote.mark_price : state.avg_entry_price;
      state.liquidation_price = std::max(0.0, remote.liquidation_price);
      positions_[remote.symbol] = state;
      continue;
    }

    auto& local = it->second;
    if (remote.mark_price > 0.0) {
      local.mark_price = remote.mark_price;
    }
    local.liquidation_price = std::max(0.0, remote.liquidation_price);

    // 本地空仓但远端有仓位时，补录数量/均价，确保强平距离统计不丢样本。
    if (std::fabs(local.qty) <= kEpsilon && std::fabs(remote.qty) > kEpsilon) {
      local.qty = remote.qty;
      local.avg_entry_price = std::max(0.0, remote.avg_entry_price);
      if (local.mark_price <= 0.0) {
        local.mark_price = local.avg_entry_price;
      }
    }
  }

  // 远端 mark 刷新可能抬高权益峰值，这里同步更新回撤基准。
  RefreshPeakEquity();
}

void AccountState::ForceSyncPositionsFromRemote(
    const std::vector<RemotePositionSnapshot>& positions) {
  std::unordered_map<std::string, PositionState> synced_positions;
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
    state.liquidation_price = std::max(0.0, remote.liquidation_price);
    synced_positions[remote.symbol] = state;
  }

  positions_.swap(synced_positions);
  RefreshPeakEquity();
}

void AccountState::SyncFromRemoteAccountBalance(
    const RemoteAccountBalanceSnapshot& balance,
    bool reset_peak_to_equity) {
  if (!balance.has_equity && !balance.has_wallet_balance) {
    return;
  }

  // 现金口径优先级：
  // 1) 优先对齐远端 equity（用本地估算upnl回推 cash，保证本地equity与远端一致）；
  // 2) 若无 equity 再回退钱包余额。
  if (balance.has_equity) {
    cash_usd_ = balance.equity_usd - UnrealizedPnlUsd();
  } else if (balance.has_wallet_balance) {
    cash_usd_ = balance.wallet_balance_usd;
  }

  if (balance.has_equity) {
    if (reset_peak_to_equity) {
      peak_equity_usd_ = std::max(balance.equity_usd, 0.0);
      if (peak_equity_usd_ <= kEpsilon) {
        peak_equity_usd_ = std::max(equity_usd(), kEpsilon);
      }
    } else {
      peak_equity_usd_ = std::max(peak_equity_usd_, balance.equity_usd);
      if (peak_equity_usd_ <= kEpsilon) {
        peak_equity_usd_ = std::max(equity_usd(), kEpsilon);
      }
    }
  } else if (reset_peak_to_equity) {
    peak_equity_usd_ = std::max(equity_usd(), kEpsilon);
  }
}

}  // namespace ai_trade
