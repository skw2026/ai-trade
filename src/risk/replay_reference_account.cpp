#include "risk/replay_reference_account.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace ai_trade {
namespace {
constexpr double kEps = 1e-8;
bool Finite(double x) { return std::isfinite(x); }
bool Same(double a, double b) { return std::fabs(a - b) < kEps; }
}
double ReplayReferenceRules::QuantizeQuantity(double qty) {
  if (!Finite(qty) || qty < 0) throw std::invalid_argument("REFERENCE_INVALID_QTY");
  return std::floor(qty / kQtyStep + 1e-10) * kQtyStep;
}
double ReplayReferenceRules::AdversePrice(double price, int direction) {
  if (!Finite(price) || price <= 0 || std::abs(direction) != 1)
    throw std::invalid_argument("REFERENCE_INVALID_PRICE");
  const double ticks = price / kPriceTick;
  return (direction > 0 ? std::ceil(ticks - 1e-10) : std::floor(ticks + 1e-10)) * kPriceTick;
}
[[noreturn]] void ReplayReferenceAccount::Stop(const std::string& reason) {
  if (stop_reason_.empty()) stop_reason_ = reason;
  throw std::runtime_error("REFERENCE_SCREEN_STOP:" + stop_reason_);
}
void ReplayReferenceAccount::Check(bool valid, const std::string& reason) {
  if (!stop_reason_.empty()) Stop(stop_reason_);
  if (!valid) Stop(reason);
}
double ReplayReferenceAccount::Boundary() const {
  const double k = ReplayReferenceRules::kMaintenance + ReplayReferenceRules::kExitLiability;
  const double n = std::fabs(qty_);
  if (n < kEps) return 0;
  return qty_ > 0 ? std::max(0.0, (n * entry_ - collateral_) / (n * (1-k))) :
                                (n * entry_ + collateral_) / (n * (1+k));
}
double ReplayReferenceAccount::DistanceAt(double mark) const {
  if (std::fabs(qty_) < kEps) return 1;
  return std::max(0.0, qty_ > 0 ? (mark - Boundary()) / mark : (Boundary() - mark) / mark);
}
std::optional<double> ReplayReferenceAccount::RiskDistance(const AccountState& account) const {
  if (!stop_reason_.empty() || !Same(qty_, account.position_qty("BTCUSDT")) ||
      !Finite(mark_) || mark_ <= 0 || !Same(mark_, account.mark_price("BTCUSDT")) || (std::fabs(qty_) > kEps &&
      (!Same(entry_, account.avg_entry_price("BTCUSDT")) || !Finite(collateral_))))
    return std::nullopt;
  return DistanceAt(mark_);
}
void ReplayReferenceAccount::OnFill(const FillEvent& fill, const AccountState& before) {
  Check(fill.symbol == "BTCUSDT" && std::abs(fill.direction) == 1 &&
        Finite(fill.qty) && fill.qty > 0 && Finite(fill.price) && fill.price > 0 &&
        Finite(fill.fee) && fill.fee >= 0 && !fill.fill_id.empty() &&
        fills_.count(fill.fill_id) == 0 && mark_ > 0 && last_ts_ > 0,
        "INSUFFICIENT_FILL_IDENTITY");
  Check(Same(qty_, before.position_qty("BTCUSDT")) &&
        (std::fabs(qty_) < kEps || Same(entry_, before.avg_entry_price("BTCUSDT"))),
        "INSUFFICIENT_LEDGER_DIVERGENCE");
  const double signed_qty = fill.direction * fill.qty;
  Check(std::fabs(qty_) < kEps || collateral_ + qty_ * (mark_ - entry_) >
        std::fabs(qty_) * mark_ * (ReplayReferenceRules::kMaintenance + ReplayReferenceRules::kExitLiability),
        "REJECT_REFERENCE_MAINTENANCE");
  const double next = qty_ + signed_qty;
  double next_collateral = collateral_, next_entry = entry_;
  if (qty_ * signed_qty >= 0) {
    const double reserve = fill.qty * fill.price / ReplayReferenceRules::kLeverage;
    Check(Finite(before.cash_usd()) && before.cash_usd() - collateral_ + kEps >= reserve &&
          reserve > fill.fee, "INSUFFICIENT_FREE_COLLATERAL");
    // Gross is mark-valued; slippage is entry cost, not whole-position value.
    Check(std::fabs(next) * mark_ <= ReplayReferenceRules::kGrossCap + kEps,
          "INSUFFICIENT_GAP_GROSS_LIMIT");
    next_entry = (std::fabs(qty_) * entry_ + fill.qty * fill.price) / std::fabs(next);
    next_collateral += reserve - fill.fee;
  } else {
    Check(fill.qty <= std::fabs(qty_) + kEps, "INSUFFICIENT_CROSS_ZERO_FILL");
    next_collateral *= std::max(0.0, std::fabs(next) / std::fabs(qty_));
    if (std::fabs(next) < kEps) next_entry = next_collateral = 0;
  }
  Check(Finite(next_collateral) && Finite(next_entry), "INSUFFICIENT_MARGIN_ARITHMETIC");
  const double realized = qty_ * signed_qty < 0 ?
      std::min(std::fabs(qty_), fill.qty) * (fill.price - entry_) * (qty_ > 0 ? 1 : -1) : 0;
  Check(before.cash_usd() + realized - fill.fee - next_collateral >= -kEps,
        "INSUFFICIENT_FREE_COLLATERAL");
  fills_.insert(fill.fill_id);
  if (last_ts_ == last_open_) opening_fill_seen_ = true;
  qty_ = std::fabs(next) < kEps ? 0 : next;
  entry_ = next_entry;
  collateral_ = next_collateral;
}
void ReplayReferenceAccount::OnFunding(double rate, double paid, const AccountState& before) {
  Check(Finite(rate) && Finite(paid) && Same(qty_, before.position_qty("BTCUSDT")),
        "INSUFFICIENT_FUNDING_INPUT");
  if (rate == 0) return;
  Check(last_open_ == last_ts_ && last_open_ > last_funding_ && !opening_fill_seen_ && mark_ > 0 &&
        Same(paid, qty_ * mark_ * rate), "INSUFFICIENT_FUNDING_ORDER");
  last_funding_ = last_open_;
  pending_funding_exposure_ = std::fabs(qty_ * rate);
  collateral_ -= paid;
}
void ReplayReferenceAccount::AfterAccounting(const AccountState& account) {
  Check(Finite(account.cash_usd()) && Finite(account.equity_usd()) &&
        Same(qty_, account.position_qty("BTCUSDT")) &&
        Same(mark_, account.mark_price("BTCUSDT")) &&
        (std::fabs(qty_) < kEps || Same(entry_, account.avg_entry_price("BTCUSDT"))),
        "INSUFFICIENT_LEDGER_DIVERGENCE");
  Check(account.cash_usd() - collateral_ >= -kEps, "INSUFFICIENT_FREE_COLLATERAL");
  Check(std::fabs(qty_) < kEps || collateral_ + qty_ * (mark_ - entry_) >
        std::fabs(qty_) * mark_ * (ReplayReferenceRules::kMaintenance + ReplayReferenceRules::kExitLiability),
        "REJECT_REFERENCE_MAINTENANCE");
  Check(account.drawdown_pct() < 0.12, "REJECT_REFERENCE_DRAWDOWN_LATCH");
  peak_upper_ = std::max(peak_upper_, account.equity_usd() + funding_uncertainty_);
  const double dd = std::max(0.0, (peak_upper_ - account.equity_usd() + funding_uncertainty_) / peak_upper_);
  max_drawdown_upper_ = std::max(max_drawdown_upper_, dd);
  Check(dd < 0.08 && (std::fabs(qty_) < kEps || DistanceAt(mark_) >= 0.08),
        "INSUFFICIENT_ACCOUNTING_CONTROL_PATH");
}
void ReplayReferenceAccount::OnMarket(const MarketEvent& event, const AccountState& account) {
  Check(event.symbol == "BTCUSDT" && event.ts_ms > 0 && event.ts_ms >= last_ts_ &&
        (event.execution_only != event.completed_bar) && Finite(event.mark_price) &&
        event.mark_price > 0 && Same(qty_, account.position_qty("BTCUSDT")) &&
        (std::fabs(qty_) < kEps || Same(entry_, account.avg_entry_price("BTCUSDT"))),
        "INSUFFICIENT_MARK_OR_LEDGER");
  if (last_ts_ == 0) Check(Same(account.cash_usd(), 10000) && qty_ == 0,
                          "INSUFFICIENT_INITIAL_CAPITAL");
  last_ts_ = event.ts_ms;
  mark_ = event.mark_price;
  if (event.execution_only && event.ts_ms != last_open_) {
    last_open_ = event.ts_ms;
    opening_fill_seen_ = false;
  }
  AfterAccounting(account);
  Check(std::fabs(qty_) * mark_ <= ReplayReferenceRules::kTierLimit,
        "INSUFFICIENT_REFERENCE_TIER");
  if (!event.completed_bar || event.ts_ms == last_closed_) return;
  Check(last_open_ + 300000 == event.ts_ms && Finite(event.mark_high_price) &&
        Finite(event.mark_low_price) && event.mark_low_price > 0 &&
        event.mark_high_price >= mark_ && event.mark_low_price <= mark_ &&
        event.mark_high_price >= event.mark_low_price,
        "INSUFFICIENT_MARK_RANGE");
  last_closed_ = event.ts_ms;
  funding_uncertainty_ += pending_funding_exposure_ *
      (event.mark_high_price - event.mark_low_price);
  pending_funding_exposure_ = 0;
  const double adverse = qty_ > 0 ? event.mark_low_price : event.mark_high_price;
  const double favorable = qty_ > 0 ? event.mark_high_price : event.mark_low_price;
  const double low_equity = account.cash_usd() + qty_ * (adverse - entry_) - funding_uncertainty_;
  const double high_equity = account.cash_usd() + qty_ * (favorable - entry_) + funding_uncertainty_;
  peak_upper_ = std::max(peak_upper_, high_equity);
  const double dd = std::max(0.0, (peak_upper_ - low_equity) / peak_upper_);
  max_drawdown_upper_ = std::max(max_drawdown_upper_, dd);
  Check(std::fabs(qty_) * std::max(event.mark_high_price, mark_) <= ReplayReferenceRules::kTierLimit,
        "INSUFFICIENT_REFERENCE_TIER");
  Check(dd < 0.08 && (std::fabs(qty_) < kEps || DistanceAt(adverse) >= 0.08),
        "INSUFFICIENT_INTRABAR_CONTROL_PATH");
}
}  // namespace ai_trade
