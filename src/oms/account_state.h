#pragma once

#include <string>
#include <unordered_map>
#include <vector>
#include <optional>

#include "core/types.h"

namespace ai_trade {

/// Single symbol position state.
struct PositionState {
  Quantity qty{0.0};
  Price avg_entry_price{0.0};
  Price mark_price{0.0};
  Price liquidation_price{0.0}; // <=0 means unknown/not applicable
};

/**
 * @brief Account State Manager
 *
 * Responsible for:
 * 1. Maintaining the portfolio state (positions, cash, equity).
 * 2. Processing fills and market updates to mutate state.
 * 3. Providing read-only views for Risk and Strategy engines.
 */
class AccountState {
 public:
  AccountState() = default;

  // --- State Mutators ---

  /// Updates mark prices and recalculates equity peak.
  void OnMarket(const MarketEvent& event);

  /// Applies a fill execution to the portfolio.
  void ApplyFill(const FillEvent& fill);

  /// Replaces local positions with a remote snapshot (e.g., from REST API).
  void SyncFromRemotePositions(const std::vector<RemotePositionSnapshot>& positions,
                               double baseline_cash_usd = 10000.0);

  /// Updates risk-related fields (mark/liq price) from remote without resetting cash.
  void RefreshRiskFromRemotePositions(
      const std::vector<RemotePositionSnapshot>& positions);

  /// Forces a hard overwrite of positions (for error recovery).
  void ForceSyncPositionsFromRemote(
      const std::vector<RemotePositionSnapshot>& positions);

  /// Syncs balance/equity from remote to correct drift.
  void SyncFromRemoteAccountBalance(
      const RemoteAccountBalanceSnapshot& balance,
      bool reset_peak_to_equity);

  // --- Accessors ---

  double current_notional_usd() const;
  double gross_notional_usd() const;
  double current_notional_usd(const std::string& symbol) const;
  
  double equity_usd() const;
  double cumulative_realized_pnl_usd() const;
  double cumulative_fee_usd() const;
  double cumulative_realized_net_pnl_usd() const;
  
  double drawdown_pct() const;
  
  /// Calculates the P95 liquidation distance (Risk Metric).
  double liquidation_distance_p95() const;

  double mark_price(const std::string& symbol) const;
  double position_qty(const std::string& symbol) const;
  
  std::vector<std::string> GetActiveSymbols() const;

 private:
  void RefreshPeakEquity();
  double UnrealizedPnlUsd() const;
  static double EffectiveMarkPrice(const PositionState& position);

  double cash_usd_{10000.0};
  double peak_equity_usd_{10000.0};
  double cumulative_realized_pnl_usd_{0.0};
  double cumulative_fee_usd_{0.0};
  
  std::unordered_map<std::string, PositionState> positions_;
};

}  // namespace ai_trade
