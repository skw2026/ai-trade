#include "research/offline_intent_bridge.h"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <sys/file.h>
#include <unistd.h>

namespace ai_trade::research {
namespace {
constexpr const char* kSymbol = "BTCUSDT";
constexpr double kEps = 1e-9;
void Need(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}
int Direction(double value) { return value > kEps ? 1 : value < -kEps ? -1 : 0; }
double RoundQty(double qty) {
  return std::copysign(std::floor(std::fabs(qty) / 0.001 + 1e-10) * 0.001, qty);
}
double Policy(double x, int previous) {
  const double mean = -4.0 * x;
  auto score = [=](int d) {
    return d * mean - .00065 * (std::abs(d - previous) + std::abs(d));
  };
  int best = previous;
  for (int d : {previous, 0, -1, 1}) {
    if (score(d) > score(best)) best = d;
  }
  return 2500.0 * best;
}
bool SameNumber(double a, double b) {
  return std::isfinite(a) && std::isfinite(b) &&
      std::fabs(a - b) <= 8 * std::numeric_limits<double>::epsilon() *
                             std::max(std::fabs(a), std::fabs(b));
}
bool SameFill(const FillEvent& a, const FillEvent& b) {
  return a.fill_id == b.fill_id && a.client_order_id == b.client_order_id &&
      a.symbol == b.symbol && a.direction == b.direction && SameNumber(a.qty, b.qty) &&
      SameNumber(a.price, b.price) && SameNumber(a.fee, b.fee) && a.liquidity == b.liquidity;
}
// Existing WAL uses six significant digits for order/fill numeric fields.
// This bridge refuses unsupported precision rather than silently losing it.
bool WalExact(double value) {
  std::ostringstream out;
  out << value;
  return SameNumber(std::stod(out.str()), value);
}
}  // namespace

OfflineIntentBridge::OfflineIntentBridge(MockExchangeAdapter& adapter,
    const std::string& directory, bool operator_reduce_only, AsyncExecutor::Mode mode)
    : adapter_(adapter), executor_(&adapter, mode, operator_reduce_only),
      wal_(directory + "/orders.wal"), journal_path_(directory + "/intent.events") {
  std::filesystem::create_directories(directory);
  lock_fd_ = ::open((directory + "/owner.lock").c_str(), O_CREAT | O_RDWR, 0600);
  if (lock_fd_ < 0 || ::flock(lock_fd_, LOCK_EX | LOCK_NB) != 0) {
    if (lock_fd_ >= 0) ::close(lock_fd_);
    lock_fd_ = -1;
    throw std::runtime_error("OFFLINE_BRIDGE_ALREADY_OWNED");
  }
  try {
    std::string error;
    Need(wal_.Initialize(&error), "WAL_INITIALIZE");
    Recover();
    if (operator_reduce_only && !operator_locked_) LatchOperatorReduceOnly();
    if (stopped_ || operator_locked_) executor_.LatchSafetyWithdrawal();
    executor_.Start();
  } catch (...) {
    ::close(lock_fd_);
    lock_fd_ = -1;
    throw;
  }
}

OfflineIntentBridge::~OfflineIntentBridge() {
  executor_.Stop();
  if (lock_fd_ >= 0) ::close(lock_fd_);
}

void OfflineIntentBridge::AppendState(const std::string& record) {
  const int fd = ::open(journal_path_.c_str(), O_WRONLY | O_APPEND | O_CREAT, 0600);
  if (fd < 0) {
    ready_ = false; faulted_ = true;
    throw std::runtime_error("STATE_OPEN");
  }
  const std::string payload = record + "\n";
  std::size_t written = 0;
  bool ok = true;
  while (written < payload.size()) {
    const auto count = ::write(fd, payload.data() + written, payload.size() - written);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) { ok = false; break; }
    written += static_cast<std::size_t>(count);
  }
  ok = (::fsync(fd) == 0) && ok;
  const int closed = ::close(fd);
  if (!ok || closed != 0) { ready_ = false; stopped_ = true; faulted_ = true; }
  Need(ok && closed == 0, "STATE_DURABILITY_FAILURE");
}

void OfflineIntentBridge::ApplyBar(std::int64_t timestamp, double xs, double xl,
                                  double price) {
  Need(timestamp >= 0 && timestamp % 300000 == 0 &&
       (timestamp_ < 0 || timestamp == timestamp_ + 300000), "BAR_CLOCK");
  Need(std::isfinite(xs) && std::isfinite(xl) && std::fabs(xs) <= .01 &&
       std::fabs(xl) <= .01 && std::isfinite(price) && price > 0, "BAR_INPUT");
  const double next = stopped_ ? 0 : Policy(xl, Direction(target_));
  if (next != target_) goal_qty_ = RoundQty(next / price);
  target_ = next;
  timestamp_ = timestamp;
  short_feature_ = xs;
  long_feature_ = xl;
  bar_price_ = price;
  ++version_;
}

void OfflineIntentBridge::OnBar(std::int64_t timestamp, double xs, double xl,
                               double price) {
  if (timestamp == timestamp_) {
    Need(xs == short_feature_ && xl == long_feature_ && price == bar_price_,
         "CONFLICTING_BAR");
    return;
  }
  // Validate the next pure state on a temporary scalar copy before persisting.
  Need(timestamp >= 0 && timestamp % 300000 == 0 &&
       (timestamp_ < 0 || timestamp == timestamp_ + 300000), "BAR_CLOCK");
  Need(std::isfinite(xs) && std::isfinite(xl) && std::fabs(xs) <= .01 &&
       std::fabs(xl) <= .01 && std::isfinite(price) && price > 0, "BAR_INPUT");
  std::ostringstream line;
  line << std::setprecision(17) << "B " << timestamp << ' ' << xs << ' ' << xl << ' ' << price;
  AppendState(line.str());
  ApplyBar(timestamp, xs, xl, price);
}

void OfflineIntentBridge::LatchRiskStop() {
  if (!stopped_) AppendState("R");
  stopped_ = true;
  target_ = goal_qty_ = 0;
  executor_.LatchSafetyWithdrawal();
}

void OfflineIntentBridge::LatchOperatorReduceOnly() {
  if (!operator_locked_) AppendState("O");
  operator_locked_ = true;
  executor_.LatchSafetyWithdrawal();
}

void OfflineIntentBridge::FailClosed(const char* reason) {
  ready_ = false;
  faulted_ = true;
  AppendState("F");
  throw std::runtime_error(reason);
}

void OfflineIntentBridge::Recover() {
  if (std::filesystem::exists(journal_path_)) {
    std::ifstream input(journal_path_, std::ios::binary);
    std::string bytes((std::istreambuf_iterator<char>(input)), {});
    Need(input.good() || input.eof(), "STATE_READ");
    Need(bytes.empty() || bytes.back() == '\n', "TORN_STATE");
    std::istringstream lines(bytes);
    std::string line;
    while (std::getline(lines, line)) {
      if (line == "R") { stopped_ = true; target_ = goal_qty_ = 0; }
      else if (line == "O") operator_locked_ = true;
      else if (line == "F") faulted_ = true;
      else if (line.rfind("P ", 0) == 0) {
        std::istringstream row(line.substr(2));
        double peak = 0;
        std::string extra;
        Need(static_cast<bool>(row >> peak) && !(row >> extra) &&
             std::isfinite(peak) && peak >= risk_peak_, "BAD_RISK_PEAK");
        risk_peak_ = peak;
      }
      else {
        std::istringstream row(line);
        std::string kind, extra;
        std::int64_t ts = 0;
        double xs = 0, xl = 0, price = 0;
        Need(static_cast<bool>(row >> kind >> ts >> xs >> xl >> price) &&
             kind == "B" && !(row >> extra), "BAD_STATE_RECORD");
        ApplyBar(ts, xs, xl, price);
      }
    }
  }
  std::unordered_set<std::string> orders, fill_ids;
  std::unordered_map<std::string, OrderIntent> intents;
  std::vector<FillEvent> fills;
  std::string error;
  Need(wal_.LoadState(&orders, &fill_ids, &fills, &error, &intents), "WAL_RECOVERY");
  order_sequence_ = intents.size();
  for (const auto& [id, intent] : intents) {
    Need(version_ > 0 && intent.symbol == kSymbol &&
         intent.model_version == "frozen-0-minus4" &&
         intent.integrator_mode == "offline-mock-only", "RECOVERY_LINEAGE");
    Need(intent.decision_id.rfind("intent-", 0) == 0 &&
         id.rfind("offline-", 0) == 0, "RECOVERY_IDENTITY");
    std::size_t consumed = 0;
    const auto v = std::stoull(intent.decision_id.substr(7), &consumed);
    Need(consumed == intent.decision_id.size() - 7 && v > 0 && v <= version_, "RECOVERY_VERSION");
    const auto n = std::stoull(id.substr(8), &consumed);
    Need(consumed == id.size() - 8 && n > 0 && n <= intents.size() &&
         std::isfinite(intent.qty) && intent.qty > 0 &&
         std::isfinite(intent.price) && intent.price > 0 &&
         (intent.direction == 1 || intent.direction == -1), "RECOVERY_ORDER_INPUT");
    Need(oms_.RegisterIntent(intent), "RECOVERY_ORDER");
  }
  for (const auto& fill : fills) {
    const auto* order = oms_.Find(fill.client_order_id);
    Need(order && fill.symbol == kSymbol && fill.direction == order->intent.direction &&
         std::isfinite(fill.qty) && fill.qty > 0 && std::isfinite(fill.price) && fill.price > 0 &&
         std::isfinite(fill.fee) && order->filled_qty + fill.qty <= order->intent.qty + kEps,
         "RECOVERY_UNKNOWN_OR_INVALID_FILL");
    Need(fills_.emplace(fill.fill_id, fill).second, "RECOVERY_DUPLICATE_FILL");
    oms_.OnFill(fill);
    account_.ApplyFill(fill);
  }
  ready_ = false;  // A recovered WAL never grants permission to send by itself.
}

void OfflineIntentBridge::PumpResults() {
  std::vector<AsyncResult> results;
  executor_.PollResults(&results);
  for (const auto& result : results) {
    if (!oms_.Find(result.client_order_id)) FailClosed("UNKNOWN_ACK");
    if (result.is_cancel) {
      cancel_wait_.erase(result.client_order_id);
      if (result.success) oms_.MarkCancelConfirmed(result.client_order_id);
      else { oms_.MarkCancelFailed(result.client_order_id); ready_ = false; }
    } else {
      submit_wait_.erase(result.client_order_id);
      if (result.success) oms_.MarkSent(result.client_order_id);
      // false/transport errors may be ambiguous: do not turn them into
      // permission to resend. Reconcile the mock's authoritative state first.
      else ready_ = false;
    }
  }
}

void OfflineIntentBridge::OnFill(const FillEvent& fill) {
  auto previous = fills_.find(fill.fill_id);
  if (previous != fills_.end()) {
    if (!SameFill(previous->second, fill)) FailClosed("CONFLICTING_FILL");
    return;
  }
  const auto* order = oms_.Find(fill.client_order_id);
  const bool valid = !fill.fill_id.empty() && order && fill.symbol == kSymbol &&
      fill.fill_id.find_first_of(" \t\r\n") == std::string::npos &&
      fill.direction == order->intent.direction && std::isfinite(fill.qty) &&
      fill.qty > 0 && std::isfinite(fill.price) && fill.price > 0 &&
      std::isfinite(fill.fee) && order->filled_qty + fill.qty <= order->intent.qty + kEps &&
      WalExact(fill.qty) && WalExact(fill.price) && WalExact(fill.fee);
  if (!valid) FailClosed("INVALID_OR_UNKNOWN_FILL");
  std::string error;
  if (!wal_.AppendFill(fill, &error)) { ready_ = false; throw std::runtime_error("FILL_WAL"); }
  fills_.emplace(fill.fill_id, fill);
  oms_.OnFill(fill);
  account_.ApplyFill(fill);
}

bool OfflineIntentBridge::Reconcile() {
  PumpResults();
  ready_ = false;
  if (faulted_) return false;
  if (!submit_wait_.empty() || !cancel_wait_.empty()) return false;
  std::vector<RemotePositionSnapshot> positions;
  std::vector<RemoteOpenOrderSnapshot> orders;
  if (!adapter_.GetRemotePositions(&positions) || !adapter_.GetRemoteOpenOrders(&orders)) return false;
  double remote_qty = 0;
  for (const auto& position : positions) {
    if (position.symbol != kSymbol || !std::isfinite(position.qty)) return false;
    remote_qty += position.qty;
  }
  if (std::fabs(remote_qty - account_.position_qty(kSymbol)) > kEps ||
      std::fabs(remote_qty - oms_.net_filled_qty(kSymbol)) > kEps) return false;
  std::set<std::string> open;
  for (const auto& order : orders) {
    const auto* local = oms_.Find(order.client_order_id);
    if (!local || !open.insert(order.client_order_id).second ||
        OrderManager::IsTerminalState(local->state) || order.symbol != kSymbol ||
        order.direction != local->intent.direction ||
        order.reduce_only != local->intent.reduce_only ||
        !std::isfinite(order.leaves_qty) || !std::isfinite(order.original_qty) ||
        !std::isfinite(order.filled_qty) ||
        std::fabs(order.original_qty - local->intent.qty) > kEps ||
        std::fabs(order.filled_qty - local->filled_qty) > kEps ||
        std::fabs(order.leaves_qty - (local->intent.qty - local->filled_qty)) > kEps)
      return false;
  }
  for (const auto& id : oms_.PendingNetPositionOrderIds()) {
    if (!open.count(id)) oms_.MarkCancelled(id);
    else oms_.MarkSent(id);
  }
  cancelling_.clear();
  ready_ = true;
  return true;
}

void OfflineIntentBridge::CancelPending() {
  for (const auto& id : oms_.PendingNetPositionOrderIds()) {
    if (cancelling_.insert(id).second) {
      oms_.MarkCancelPending(id);
      cancel_wait_.insert(id);
      executor_.Cancel(id);
    }
  }
}

bool OfflineIntentBridge::Drive(double price) {
  Need(std::isfinite(price) && price > 0, "EXECUTION_PRICE");
  PumpResults();
  if (faulted_) return false;
  account_.OnMarket(MarketEvent{.ts_ms = std::max<std::int64_t>(0, timestamp_),
                              .symbol = kSymbol, .price = price, .mark_price = price});
  if (account_.equity_usd() > risk_peak_) {
    std::ostringstream record;
    record << std::setprecision(17) << "P " << account_.equity_usd();
    AppendState(record.str());
    risk_peak_ = account_.equity_usd();
  }
  if ((risk_peak_ - account_.equity_usd()) / risk_peak_ >= .08) LatchRiskStop();
  double desired = stopped_ ? 0 : goal_qty_;
  const double actual = account_.position_qty(kSymbol);
  if (operator_locked_) desired = actual > 0 ? std::clamp(desired, 0.0, actual)
                                            : std::clamp(desired, actual, 0.0);
  const double cap = std::fabs(RoundQty(2500 / price));
  desired = std::clamp(desired, -cap, cap);
  if (oms_.HasPendingNetPositionOrders()) {
    const double projected = actual + oms_.PendingNetPositionRemainingQtyForSymbol(kSymbol);
    if (stopped_ || operator_locked_ || std::fabs(projected - desired) > kEps)
      CancelPending();
    return false;
  }
  if (!ready_ || !submit_wait_.empty() || !cancel_wait_.empty() || !cancelling_.empty() || version_ == 0)
    return false;
  RiskAdjustedPosition target;
  target.symbol = kSymbol;
  target.adjusted_notional_usd = desired * price;
  target.reduce_only = stopped_ || operator_locked_ ||
      (Direction(desired) == Direction(actual) && std::fabs(desired) < std::fabs(actual));
  auto intent = engine_.BuildIntent(target, actual * price, price);
  if (!intent) return false;
  // Fixed local quantity lattice; don't create unexecutable sub-step residuals.
  intent->qty = std::stod(([&] {
    std::ostringstream out; out << std::fixed << std::setprecision(3) << std::fabs(RoundQty(intent->qty));
    return out.str();
  })());
  if (intent->qty < .001) return false;
  Need(WalExact(intent->qty) && WalExact(price), "WAL_PRECISION_UNSUPPORTED");
  intent->client_order_id = "offline-" + std::to_string(++order_sequence_);
  intent->decision_id = "intent-" + std::to_string(version_);
  intent->candidate_id = "synthetic-only";
  intent->model_version = "frozen-0-minus4";
  intent->integrator_mode = "offline-mock-only";
  intent->liquidity_preference = LiquidityPreference::kTaker;
  std::string error;
  if (!wal_.AppendIntent(*intent, &error)) { ready_ = false; throw std::runtime_error("ORDER_WAL"); }
  Need(oms_.RegisterIntent(*intent), "DUPLICATE_ORDER");
  submit_wait_.insert(intent->client_order_id);
  executor_.Submit(*intent);
  return true;
}

}  // namespace ai_trade::research
