#include "risk/manual_risk_cycle.h"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <filesystem>
#include <iomanip>
#include <limits>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

namespace ai_trade {
namespace {
bool Token(const std::string& value) {
  return !value.empty() && value.size() <= 160 &&
      value.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:/") ==
          std::string::npos;
}
bool Fail(std::string* error, const char* message) {
  if (error) *error = message;
  return false;
}
double Drawdown(double peak, double equity) {
  return std::max(0.0, (peak - equity) / peak);
}
} // namespace

ManualRiskCycle::ManualRiskCycle(double cap, RiskThresholds thresholds, AuditSink sink)
    : original_cap_(cap), thresholds_(thresholds), sink_(std::move(sink)) {
  if (!std::isfinite(cap) || cap <= 0)
    throw std::invalid_argument("manual risk cycle: invalid original cap");
}
ManualRiskCycle::~ManualRiskCycle() {
  if (journal_fd_ >= 0) ::close(journal_fd_);
}

bool ManualRiskCycle::OpenNewJournal(const std::string& path, std::string* error) {
  if (fatal_ || sink_ || journal_fd_ >= 0 || state_.initialized || path.empty())
    return Fail(error, "manual risk journal must bind once before observations");
  // Never truncate, reuse or silently reset a prior run (including an empty
  // crash-created file). The owning application creates its data directory.
  journal_fd_ = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_APPEND | O_NOFOLLOW, 0600);
  if (journal_fd_ < 0) {
    fatal_ = true;
    return Fail(error, "manual risk journal exists or cannot be created; no automatic restart");
  }
  if (::flock(journal_fd_, LOCK_EX | LOCK_NB) != 0 ||
      !Write("MVP_MANUAL_RISK_V1\tmanual_only\t24h\t25/50/100\t2pct\t7d\n")) {
    fatal_ = true;
    return Fail(error, "manual risk journal cannot be durably initialized");
  }
  auto parent = std::filesystem::path(path).parent_path();
  if (parent.empty()) parent = ".";
  const int directory = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY);
  const bool durable = directory >= 0 && ::fsync(directory) == 0;
  if (directory >= 0) ::close(directory);
  if (!durable) {
    fatal_ = true;
    return Fail(error, "manual risk journal directory sync failed");
  }
  return true;
}

bool ManualRiskCycle::Write(const std::string& record) {
  if (sink_) return sink_(record);
  if (journal_fd_ < 0) return true; // component observations only; approvals require a journal
  std::size_t offset = 0;
  while (offset < record.size()) {
    const auto count = ::write(journal_fd_, record.data() + offset, record.size() - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) return false;
    offset += static_cast<std::size_t>(count);
  }
  return ::fsync(journal_fd_) == 0;
}

void ManualRiskCycle::Commit(ManualRiskCycleState next, const std::string& event,
                              const std::string& approval, const std::string& review) {
  if (fatal_) throw std::runtime_error("manual risk cycle failed closed");
  next.sequence = state_.sequence + 1;
  std::ostringstream out;
  out.imbue(std::locale::classic());
  out << std::setprecision(17) << event << '\t' << next.sequence << '\t'
      << next.last_ts_ms << '\t' << next.cycle << '\t' << next.stage << '\t'
      << next.latched << '\t' << static_cast<int>(next.latch_mode) << '\t' << next.reason << '\t'
      << next.equity_usd << '\t' << next.cumulative_net_usd << '\t'
      << next.cumulative_fees_usd << '\t' << next.cumulative_funding_usd << '\t'
      << next.lifetime_peak_usd << '\t' << next.lifetime_drawdown << '\t'
      << next.lifetime_max_drawdown << '\t' << next.lifetime_limit_breached << '\t'
      << next.cycle_start_equity_usd << '\t' << next.cycle_peak_usd << '\t'
      << next.cycle_drawdown << '\t' << next.cycle_max_drawdown << '\t'
      << next.approved_cap_usd << '\t' << next.max_loss_usd << '\t'
      << next.flat_since_ms << '\t' << next.stage_since_ms << '\t'
      << next.expires_at_ms << '\t' << next.flat_idle_healthy << '\t'
      << approval << '\t' << review << '\n';
  if (!Write(out.str())) {
    fatal_ = true;
    throw std::runtime_error("manual risk audit write failed; no state transition committed");
  }
  state_ = std::move(next);
}

void ManualRiskCycle::Observe(std::int64_t ts, const AccountState& account,
                               bool healthy, bool pending) {
  const double equity = account.equity_usd();
  const double gross = account.gross_notional_usd();
  if (fatal_ || ts <= 0 || ts < state_.last_ts_ms ||
      !std::isfinite(equity) || !std::isfinite(gross) ||
      !std::isfinite(account.peak_equity_usd()) || account.peak_equity_usd() <= 0 ||
      !std::isfinite(account.cumulative_realized_net_pnl_usd()) ||
      !std::isfinite(account.cumulative_fee_usd()) ||
      !std::isfinite(account.cumulative_funding_paid_usd())) {
    fatal_ = true;
    throw std::runtime_error("manual risk observation invalid; failed closed");
  }
  auto next = state_;
  if (!next.initialized) {
    next.initialized = true;
    next.cycle_start_equity_usd = account.peak_equity_usd();
    next.cycle_peak_usd = account.peak_equity_usd();
    next.approved_cap_usd = original_cap_;
  }
  next.last_ts_ms = ts;
  next.equity_usd = equity;
  next.cumulative_net_usd = account.cumulative_realized_net_pnl_usd();
  next.cumulative_fees_usd = account.cumulative_fee_usd();
  next.cumulative_funding_usd = account.cumulative_funding_paid_usd();
  next.lifetime_peak_usd = std::max({next.lifetime_peak_usd, account.peak_equity_usd(), equity});
  next.lifetime_drawdown = Drawdown(next.lifetime_peak_usd, equity);
  next.lifetime_max_drawdown = std::max(next.lifetime_max_drawdown, next.lifetime_drawdown);
  next.lifetime_limit_breached |= next.lifetime_max_drawdown >= thresholds_.fuse_drawdown;
  next.cycle_peak_usd = std::max(next.cycle_peak_usd, equity);
  next.cycle_drawdown = Drawdown(next.cycle_peak_usd, equity);
  next.cycle_max_drawdown = std::max(next.cycle_max_drawdown, next.cycle_drawdown);
  next.flat_idle_healthy = healthy && !pending && gross == 0.0 && equity > 0;
  if (!next.flat_idle_healthy) next.flat_since_ms = 0;
  else if (next.flat_since_ms == 0) next.flat_since_ms = ts;

  const bool fuse = next.cycle_drawdown >= thresholds_.fuse_drawdown;
  const bool cooldown = next.cycle_drawdown >= thresholds_.cooldown_drawdown;
  const bool budget = next.cycle > 0 &&
      std::max(next.cycle_start_equity_usd - equity, next.cycle_peak_usd - equity) >= next.max_loss_usd;
  const bool expired = next.cycle > 0 && ts >= next.expires_at_ms;
  if (fuse || (!next.latched && (cooldown || budget || expired))) {
    if (!next.latched) next.flat_since_ms = next.flat_idle_healthy ? ts : 0;
    next.latched = true;
    next.latch_mode = fuse ? RiskMode::kFuse : RiskMode::kCooldown;
    next.reason = fuse ? "CYCLE_DD_FUSE" : cooldown ? "CYCLE_DD_COOLDOWN" :
                  budget ? "CYCLE_LOSS_BUDGET" : "CYCLE_EXPIRED";
  }
  Commit(std::move(next), "OBSERVE");
}

bool ManualRiskCycle::Approve(const ManualRiskApproval& a, std::string* error) {
  if (fatal_ || (!sink_ && journal_fd_ < 0))
    return Fail(error, "manual approval requires healthy durable journal");
  if (!state_.initialized || !Token(a.approval_id) || !Token(a.review_reference) ||
      used_approval_ids_.count(a.approval_id) || a.expected_cycle != state_.cycle ||
      a.observed_at_ms != state_.last_ts_ms || !state_.flat_idle_healthy)
    return Fail(error, "manual approval identity, cycle, observation, or flat/idle/health invalid");
  if (!std::isfinite(a.max_gross_notional_usd) || a.max_gross_notional_usd <= 0 ||
      a.max_gross_notional_usd > original_cap_ || !std::isfinite(a.max_loss_usd) || a.max_loss_usd <= 0)
    return Fail(error, "manual approval has invalid explicit budget");
  auto next = state_;
  if (a.next_stage == 1) {
    if (!next.latched || next.flat_since_ms <= 0 ||
        next.last_ts_ms - next.flat_since_ms < kDayMs ||
        a.max_loss_usd > kRecoveryLossFraction * next.equity_usd ||
        next.last_ts_ms > std::numeric_limits<std::int64_t>::max() - kCycleDurationMs)
      return Fail(error, "new manual cycle requires latch, 24h flat cooldown and <=2pct loss budget");
    ++next.cycle;
    next.stage = 1;
    next.latched = false;
    next.latch_mode = RiskMode::kNormal;
    next.cycle_start_equity_usd = next.cycle_peak_usd = next.equity_usd;
    next.cycle_drawdown = next.cycle_max_drawdown = 0;
    next.approved_cap_usd = a.max_gross_notional_usd;
    next.max_loss_usd = a.max_loss_usd;
    next.expires_at_ms = next.last_ts_ms + kCycleDurationMs;
    next.reason = "MANUAL_NEW_CYCLE";
  } else {
    if (next.latched || next.cycle == 0 || a.next_stage != next.stage + 1 || a.next_stage > 3 ||
        next.last_ts_ms - next.stage_since_ms < kDayMs ||
        a.max_gross_notional_usd != next.approved_cap_usd || a.max_loss_usd != next.max_loss_usd)
      return Fail(error, "manual stage requires next step, 24h dwell and unchanged budget");
    next.stage = a.next_stage;
    next.reason = "MANUAL_STAGE_ADVANCE";
  }
  next.stage_since_ms = next.last_ts_ms;
  Commit(std::move(next), "APPROVE", a.approval_id, a.review_reference);
  used_approval_ids_.insert(a.approval_id);
  return true;
}

double ManualRiskCycle::gross_cap_usd() const {
  if (fatal_ || state_.latched) return 0;
  if (state_.stage == 0) return original_cap_;
  return state_.approved_cap_usd * (state_.stage == 1 ? .25 : state_.stage == 2 ? .5 : 1.0);
}
double ManualRiskCycle::control_drawdown() const {
  if (fatal_) return thresholds_.fuse_drawdown;
  if (!state_.latched) return state_.cycle_drawdown;
  return std::max(state_.cycle_drawdown, state_.latch_mode == RiskMode::kFuse ?
                  thresholds_.fuse_drawdown : thresholds_.cooldown_drawdown);
}
} // namespace ai_trade
