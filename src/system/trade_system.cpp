#include "system/trade_system.h"

#include "core/log.h"

namespace ai_trade {

bool TradeSystem::OnPrice(double price, bool trade_ok) {
  const MarketEvent event = market_.Next(price);
  const auto decision = Evaluate(event, trade_ok);
  auto intent = decision.intent;
  if (!intent.has_value()) {
    return false;
  }

  FillEvent fill;
  fill.fill_id = intent->client_order_id + "-sim-fill";
  fill.client_order_id = intent->client_order_id;
  fill.symbol = intent->symbol;
  fill.direction = intent->direction;
  fill.qty = intent->qty;
  fill.price = intent->price;

  // 便捷模式下将意图直接当作成交回报，便于快速验证闭环。
  OnFill(fill);
  LogInfo("骨架模式：订单已成交");
  return true;
}

MarketDecision TradeSystem::Evaluate(const MarketEvent& event, bool trade_ok) {
  MarketDecision decision;
  account_.OnMarket(event);
  decision.signal = strategy_.OnMarket(event);
  if (decision.signal.symbol.empty()) {
    decision.signal.symbol = event.symbol;
  }
  decision.target =
      TargetPosition{decision.signal.symbol, decision.signal.suggested_notional_usd};
  decision.risk_adjusted =
      risk_.Apply(decision.target, trade_ok, account_.drawdown_pct());
  decision.intent = execution_.BuildIntent(decision.risk_adjusted,
                                           account_.current_notional_usd(
                                               decision.risk_adjusted.symbol),
                                           event.price);
  return decision;
}

}  // namespace ai_trade
