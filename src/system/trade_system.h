#pragma once

#include <optional>
#include <vector>

#include "execution/execution_engine.h"
#include "market/market_data.h"
#include "oms/account_state.h"
#include "risk/risk_engine.h"
#include "strategy/strategy_engine.h"

namespace ai_trade {

struct MarketDecision {
  Signal signal;
  TargetPosition target;
  RiskAdjustedPosition risk_adjusted;
  std::optional<OrderIntent> intent;
};

class TradeSystem {
 public:
  TradeSystem(double risk_cap_usd,
              double max_order_notional_usd,
              RiskThresholds thresholds = {})
      : risk_(risk_cap_usd, thresholds), execution_(max_order_notional_usd) {}

  // 便捷入口：仅用于本地快速回放，内部会直接按意图成交。
  bool OnPrice(double price, bool trade_ok = true);

  // 标准流水线：输出每层结果，便于监控漏斗与审计。
  MarketDecision Evaluate(const MarketEvent& event, bool trade_ok = true);

  // 标准入口：输入外部行情，输出下单意图（不直接改账户仓位）。
  std::optional<OrderIntent> OnMarket(const MarketEvent& event,
                                      bool trade_ok = true) {
    return Evaluate(event, trade_ok).intent;
  }

  // 由外部成交回报驱动账户状态更新。
  void OnFill(const FillEvent& fill) { account_.ApplyFill(fill); }
  void OnMarketSnapshot(const MarketEvent& event) { account_.OnMarket(event); }
  void SyncAccountFromRemotePositions(
      const std::vector<RemotePositionSnapshot>& positions,
      double baseline_cash_usd = 10000.0) {
    account_.SyncFromRemotePositions(positions, baseline_cash_usd);
  }
  void ForceReduceOnly(bool enabled) { risk_.SetForcedReduceOnly(enabled); }
  RiskMode risk_mode() const { return risk_.mode(); }

  const AccountState& account() const { return account_; }

 private:
  MarketData market_;
  StrategyEngine strategy_;
  RiskEngine risk_;
  ExecutionEngine execution_;
  AccountState account_;
};

}  // namespace ai_trade
