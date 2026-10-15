#include "evolution/execution_reward_ledger.h"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace ai_trade::synthetic {
namespace {
constexpr const char* kSymbol = "SYNTH";
void Need(bool valid, const char* reason) {
  if (!valid) throw std::invalid_argument(reason);
}
double ExitPrice(double quote, int side, int multiplier) {
  const double ticks = quote * (1 + side * multiplier * .0001) * 10;
  return (side > 0 ? std::ceil(ticks - 1e-9) : std::floor(ticks + 1e-9)) / 10;
}
}  // namespace

std::string RewardEvent::Identity(bool fill_only) const {
  std::ostringstream s;
  s << std::setprecision(17);
  if (!fill_only) s << time << ' ' << kind << ' ' << bucket << ' ' << version << ' ';
  s << order << ' ' << fill << ' ' << side << ' ' << a << ' ' << b << ' ' << c << ' ' << d;
  return s.str();
}

bool ExecutionRewardLedger::ValidId(const std::string& s) {
  return !s.empty() && s.size() <= 64 &&
      std::all_of(s.begin(), s.end(), [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
               (c >= '0' && c <= '9') || c == '_' || c == '-';
      });
}

ExecutionRewardLedger::ExecutionRewardLedger(int multiplier) : multiplier_(multiplier) {
  Need(multiplier == 1 || multiplier == 2, "COST_MULTIPLIER");
  MarketEvent initial; initial.symbol = kSymbol; initial.price = quote_; initial.mark_price = mark_;
  account_.OnMarket(initial);
}

double ExecutionRewardLedger::Liquidation() const {
  const double q = account_.position_qty(kSymbol);
  if (std::abs(q) < 1e-10) return account_.cash_usd();
  const double price = ExitPrice(quote_, q > 0 ? -1 : 1, multiplier_);
  AccountState closing = account_;
  FillEvent f;
  f.symbol = kSymbol; f.direction = q > 0 ? -1 : 1;
  f.qty = std::abs(q); f.price = price; f.fee = f.qty * price * multiplier_ * .00055;
  closing.ApplyFill(f, true);
  return closing.cash_usd();
}

void ExecutionRewardLedger::ObserveRisk() {
  max_drawdown_ = std::max(max_drawdown_, account_.drawdown_pct());
  hard_lock_ = hard_lock_ || max_drawdown_ >= .08;
  risk_.SetForcedReduceOnly(manual_lock_ || hard_lock_);
}

std::string ExecutionRewardLedger::Snapshot(const RewardEvent& e, bool duplicate,
                                          double reward, std::optional<OrderIntent> intent) const {
  const double q = account_.position_qty(kSymbol), avg = account_.avg_entry_price(kSymbol);
  const double equity = account_.equity_usd(), liq = Liquidation();
  std::ostringstream o;
  o << std::setprecision(17) << std::boolalpha;
  o << "{\"ok\":true,\"synthetic_only\":true,\"event\":\"" << e.id
    << "\",\"kind\":\"" << e.kind << "\",\"time\":" << last_time_
    << ",\"bucket\":" << e.bucket << ",\"version\":" << e.version
    << ",\"order_id\":\"" << e.order << "\",\"fill_id\":\"" << e.fill << "\""
    << ",\"duplicate\":" << duplicate << ",\"reward\":" << reward
    << ",\"qty\":" << q << ",\"average\":" << avg
    << ",\"margin_cash\":" << account_.cash_usd()
    << ",\"economic_cash\":" << account_.cash_usd() - q * avg
    << ",\"equity\":" << equity << ",\"liquidation\":" << liq
    << ",\"exit_reserve\":" << equity - liq
    << ",\"fees\":" << account_.cumulative_fee_usd()
    << ",\"funding\":" << account_.cumulative_funding_paid_usd()
    << ",\"realized\":" << account_.cumulative_realized_pnl_usd()
    << ",\"slippage\":" << slippage_ << ",\"turnover\":" << turnover_
    << ",\"peak\":" << account_.peak_equity_usd() << ",\"drawdown\":" << max_drawdown_
    << ",\"manual_lock\":" << manual_lock_ << ",\"hard_lock\":" << hard_lock_
    << ",\"pending\":" << orders_.PendingNetPositionOrderCount()
    << ",\"mark\":" << mark_ << ",\"quote\":" << quote_
    << ",\"fills\":" << fills_.size() << ",\"events\":" << events_.size();
  const auto* record = orders_.Find(e.order);
  o << ",\"decision_version\":\"" << (record ? record->intent.model_version : "-") << "\"";
  o << ",\"decision_bucket\":\"" << (record ? record->intent.candidate_id : "-") << "\"";
  if (intent) {
    o << ",\"intent\":{\"id\":\"" << intent->client_order_id << "\",\"qty\":" << intent->qty
      << ",\"side\":" << intent->direction << ",\"reduce_only\":" << intent->reduce_only << "}";
  } else o << ",\"intent\":null";
  o << '}';
  return o.str();
}

std::string ExecutionRewardLedger::Apply(const RewardEvent& e) {
  Need(ValidId(e.id) && e.id != "-" && ValidId(e.kind) && ValidId(e.order) && ValidId(e.fill), "IDENTITY");
  Need(e.bucket >= 0 && e.bucket < 3 && e.version >= 0 && e.time >= 0, "METADATA");
  Need(std::isfinite(e.a) && std::isfinite(e.b) && std::isfinite(e.c) && std::isfinite(e.d), "FINITE");
  auto seen = events_.find(e.id);
  if (seen != events_.end()) {
    Need(seen->second == e.Identity(), "EVENT_ID_CONFLICT");
    return Snapshot(e, true);
  }
  Need(events_.size() < 20000, "EVENT_LIMIT");
  if (e.kind == "FILL") {
    auto fill = fills_.find(e.fill);
    if (fill != fills_.end()) {
      Need(fill->second == e.Identity(true), "FILL_ID_CONFLICT");
      events_.emplace(e.id, e.Identity());
      return Snapshot(e, true);
    }
  }
  Need(e.time >= last_time_, "OUT_OF_ORDER");
  // Validate all fallible input before mutating the native account or OMS.
  const auto* record = orders_.Find(e.order);
  if (e.kind == "MARK") {
    Need(e.a >= 1 && e.b >= 1 && e.a <= 1e8 && e.b <= 1e8, "PRICE");
  } else if (e.kind == "TARGET") {
    Need(std::abs(e.a) <= 1e6 && e.order != "-" && !record, "TARGET");
  } else if (e.kind == "FILL") {
    Need(record && e.fill != "-", "UNKNOWN_ORDER");
    Need(record->state != OrderState::kRejected && record->state != OrderState::kFilled, "FILL_ORDER_STATE");
    Need(e.side == record->intent.direction && e.a >= .001 &&
         std::abs(e.a * 1000 - std::round(e.a * 1000)) < 1e-7 &&
         e.a <= record->intent.qty - record->filled_qty + 1e-9,
         "FILL_QUANTITY_SIDE");
    Need(e.b >= 1 && e.b <= 1e8 && e.c >= 0 && e.c <= 1e6 && e.d >= 1 && e.d <= 1e8, "FILL_PRICE_FEE");
    if (record->intent.reduce_only) {
      const double q = account_.position_qty(kSymbol);
      Need(q * e.side < 0 && e.a <= std::abs(q) + 1e-9, "REDUCE_FILL");
    }
  } else if (e.kind == "FUND") {
    Need(std::abs(e.a) <= 1, "FUNDING_RATE");
  } else if (e.kind == "CANCEL" || e.kind == "ACK" || e.kind == "REJECT") {
    Need(record && !OrderManager::IsTerminalState(record->state), "ORDER_STATE");
    if (e.kind == "ACK") Need(record->state == OrderState::kCancelPending, "CANCEL_SEQUENCE");
    if (e.kind == "REJECT") Need(record->filled_qty == 0, "REJECT_AFTER_FILL");
  } else Need(e.kind == "LOCK", "EVENT_KIND");

  std::optional<OrderIntent> intent;
  if (e.kind == "MARK") {
    quote_ = e.a; mark_ = e.b;
    MarketEvent m; m.symbol = kSymbol; m.price = quote_; m.mark_price = mark_; m.ts_ms = e.time;
    account_.OnMarket(m);
  } else if (e.kind == "FUND") {
    account_.ApplyFunding(kSymbol, e.a, true);
  } else if (e.kind == "LOCK") {
    manual_lock_ = true;
  } else if (e.kind == "TARGET") {
    ObserveRisk();
    if (!orders_.HasPendingNetPositionOrderForSymbol(kSymbol)) {
      // Explicit synthetic liquidation-distance assumption. Not a live risk assessment.
      const double capped = std::copysign(std::min(std::abs(e.a), 2500 * quote_ / std::max(quote_, mark_)), e.a);
      auto adjusted = risk_.Apply(TargetPosition{kSymbol, capped}, true, account_.drawdown_pct(), 1.0);
      const double desired_qty = std::trunc(adjusted.adjusted_notional_usd / quote_ * 1000 +
                                          std::copysign(1e-9, adjusted.adjusted_notional_usd)) / 1000;
      adjusted.adjusted_notional_usd = desired_qty * quote_;
      intent = execution_.BuildIntent(adjusted, account_.position_qty(kSymbol) * quote_, quote_);
      if (intent) {
        intent->qty = std::round(intent->qty * 1000) / 1000;
        if (intent->qty <= 0) intent.reset();
        else {
          intent->client_order_id = e.order; intent->decision_id = e.id;
          intent->model_version = std::to_string(e.version);
          intent->candidate_id = std::to_string(e.bucket);
          orders_.RegisterIntent(*intent); orders_.MarkSent(e.order);
        }
      }
    }
  } else if (e.kind == "FILL") {
    FillEvent f; f.symbol = kSymbol; f.fill_id = e.fill; f.client_order_id = e.order;
    f.direction = e.side; f.qty = e.a; f.price = e.b; f.fee = e.c;
    account_.ApplyFill(f, true); orders_.OnFill(f);
    slippage_ += e.a * e.side * (e.b - e.d); turnover_ += e.a * e.b;
    fills_.emplace(e.fill, e.Identity(true));
  } else if (e.kind == "CANCEL") orders_.MarkCancelPending(e.order);
  else if (e.kind == "ACK") orders_.MarkCancelConfirmed(e.order);
  else if (e.kind == "REJECT") orders_.MarkRejected(e.order);
  ObserveRisk();
  last_time_ = e.time;
  events_.emplace(e.id, e.Identity());
  const double value = Liquidation(), reward = value - last_liquidation_;
  last_liquidation_ = value;
  return Snapshot(e, false, reward, intent);
}
}  // namespace ai_trade::synthetic
