#include "evolution/runtime_reward_shadow.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace ai_trade {
RuntimeRewardShadow::RuntimeRewardShadow(std::map<std::string, ShadowExitCost> costs,
    std::size_t capacity, Timestamp max_quote_age_ms)
    : costs_(std::move(costs)), capacity_(capacity), max_quote_age_(max_quote_age_ms) {
  if (costs_.empty() || costs_.size() > 64 || capacity == 0 || capacity > 100000 ||
      max_quote_age_ms < 0) throw std::invalid_argument("SHADOW_BOUNDS");
  for (const auto& [symbol, c] : costs_)
    if (symbol.empty() || symbol.size() > 64 || !std::isfinite(c.fee_bps) ||
        c.fee_bps < 0 || c.fee_bps > 1000 || !std::isfinite(c.slippage_bps) ||
        c.slippage_bps < 0 || c.slippage_bps > 1000 || !std::isfinite(c.tick_size) ||
        c.tick_size <= 0) throw std::invalid_argument("SHADOW_COST");
}

void RuntimeRewardShadow::Capture(ShadowEvent event, const AccountState& account,
    const MarketEvent* market, const FillEvent* fill, const OrderIntent* intent,
    int bucket, EvolutionWeights weights, double funding_paid,
    const SelfEvolutionAction* action, const std::string* funding_symbol,
    double funding_rate, bool exact_funding) noexcept {
  if (!valid()) return;  // Sticky invalidation; never silently resume.
  try {
    if (receipts_.size() >= capacity_) { failure_ = "CAPACITY"; return; }
    if (event == ShadowEvent::kRebase || event == ShadowEvent::kReflectedFill)
      failure_ = "EXTERNAL_ACCOUNT_CONTINUITY_UNPROVEN";
    if (event == ShadowEvent::kBaseline && !receipts_.empty())
      failure_ = "BASELINE_REUSE";
    if (market) {
      if (market->ts_ms < clock_ || market->ts_ms < 0) failure_ = "CLOCK_REGRESSION";
      else clock_ = market->ts_ms;
      if (!std::isfinite(market->price) || market->price <= 0 ||
          !std::isfinite(market->mark_price) || market->mark_price < 0 ||
          !costs_.count(market->symbol)) failure_ = "INVALID_QUOTE";
      else quotes_.insert_or_assign(market->symbol, Quote{market->price, clock_});
    }
    if (intent) {
      if (origins_.count(intent->client_order_id) || intent->client_order_id.empty())
        failure_ = "ORDER_ID_REUSE";
      else origins_.emplace(intent->client_order_id, Origin{*intent, bucket, weights});
    }
    ShadowReceipt r;
    r.sequence = receipts_.size(); r.event = event; r.market_clock = clock_;
    if (market) r.market = *market;
    r.cash = account.cash_usd(); r.equity = account.equity_usd();
    r.fees = account.cumulative_fee_usd(); r.funding = account.cumulative_funding_paid_usd();
    r.realized = account.cumulative_realized_pnl_usd(); r.funding_paid = funding_paid;
    if (funding_symbol) r.funding_symbol = *funding_symbol;
    r.funding_rate = funding_rate; r.exact_funding = exact_funding;
    if (action) r.legacy_action = *action;
    if (fill) {
      r.fill = *fill;
      if (fill->fill_id.empty() || !std::isfinite(fill->qty) || fill->qty <= 0 ||
          !std::isfinite(fill->price) || fill->price <= 0 || !std::isfinite(fill->fee) ||
          (fill->direction != 1 && fill->direction != -1)) failure_ = "INVALID_FILL";
      const auto origin = origins_.find(fill->client_order_id);
      if (origin != origins_.end()) {
        r.origin = origin->second.intent; r.origin_bucket = origin->second.bucket;
        r.origin_weights = origin->second.weights;
        if (fill->symbol != r.origin->symbol || fill->direction != r.origin->direction)
          failure_ = "FILL_ORIGIN_MISMATCH";
      }
      // Portfolio reward may be known, but missing lineage must NEVER be
      // assigned to a current policy. This bounded bridge rejects the stream.
      else failure_ = "UNKNOWN_FILL_ORIGIN";
    }
    // Same native close-copy semantics as ExecutionRewardLedger. No real fill
    // is generated: hypothetical exit fee/slippage only values the open risk.
    auto closing = account;
    auto symbols = account.GetActiveSymbols();
    std::sort(symbols.begin(), symbols.end());
    for (const auto& symbol : symbols) {
      const auto quote = quotes_.find(symbol);
      if (!costs_.count(symbol) || quote == quotes_.end() ||
          clock_ - quote->second.time > max_quote_age_) {
        failure_ = "MISSING_OR_STALE_QUOTE"; break;
      }
      const auto& c = costs_.at(symbol);
      ShadowPosition p;
      p.symbol = symbol; p.qty = account.position_qty(symbol);
      p.average = account.avg_entry_price(symbol); p.mark = account.mark_price(symbol);
      p.quote = quote->second.price;
      const int side = p.qty > 0 ? -1 : 1;
      const double ticks = p.quote * (1 + side * c.slippage_bps / 10000) / c.tick_size;
      p.exit_price = (side > 0 ? std::ceil(ticks - 1e-9) : std::floor(ticks + 1e-9)) * c.tick_size;
      p.exit_fee = std::abs(p.qty) * p.exit_price * c.fee_bps / 10000;
      if (!std::isfinite(p.qty) || !std::isfinite(p.average) || p.average <= 0 ||
          !std::isfinite(p.mark) || p.mark <= 0 || !std::isfinite(p.exit_price) ||
          p.exit_price <= 0 || !std::isfinite(p.exit_fee)) { failure_ = "INVALID_POSITION"; break; }
      FillEvent exit;
      exit.symbol = symbol; exit.direction = side; exit.qty = std::abs(p.qty);
      exit.price = p.exit_price; exit.fee = p.exit_fee;
      closing.ApplyFill(exit, true);
      r.positions.push_back(p);
    }
    r.liquidation = closing.cash_usd(); r.exit_reserve = r.equity - r.liquidation;
    if (!std::isfinite(r.cash) || !std::isfinite(r.equity) ||
        !std::isfinite(r.liquidation) || !std::isfinite(r.fees) ||
        !std::isfinite(r.funding) || !std::isfinite(r.realized)) failure_ = "NONFINITE_ACCOUNT";
    r.valid = valid();
    if (r.valid) {
      if (previous_liquidation_) r.reward = r.liquidation - *previous_liquidation_;
      previous_liquidation_ = r.liquidation;
    }
    receipts_.push_back(std::move(r));
  } catch (...) { failure_ = "OBSERVER_EXCEPTION"; }  // Cannot alter trading control flow.
}
}  // namespace ai_trade
