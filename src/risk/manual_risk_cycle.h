#pragma once

#include <functional>
#include <string>
#include <unordered_set>

#include "oms/account_state.h"

namespace ai_trade {

struct ManualRiskApproval {
  std::string approval_id;
  std::string review_reference;
  std::uint64_t expected_cycle{0};
  std::int64_t observed_at_ms{0};
  int next_stage{1}; // 1 starts a new cycle; 2/3 advance the same cycle.
  double max_gross_notional_usd{0};
  double max_loss_usd{0};
};

struct ManualRiskCycleState {
  bool initialized{false};
  bool latched{false};
  bool lifetime_limit_breached{false};
  std::uint64_t sequence{0};
  std::uint64_t cycle{0};
  int stage{0}; // 0 = original cycle; 1/2/3 = 25/50/100% of approved cap.
  RiskMode latch_mode{RiskMode::kNormal};
  std::string reason{"INITIAL"};
  std::int64_t last_ts_ms{0};
  std::int64_t flat_since_ms{0};
  std::int64_t stage_since_ms{0};
  std::int64_t expires_at_ms{0};
  double equity_usd{0};
  double cumulative_net_usd{0};
  double cumulative_fees_usd{0};
  double cumulative_funding_usd{0};
  double lifetime_peak_usd{0};
  double lifetime_drawdown{0};
  double lifetime_max_drawdown{0};
  double cycle_start_equity_usd{0};
  double cycle_peak_usd{0};
  double cycle_drawdown{0};
  double cycle_max_drawdown{0};
  double approved_cap_usd{0};
  double max_loss_usd{0};
  bool flat_idle_healthy{false};
};

// Offline contract only. No identity provider, account reset, or auto-rearm.
// Existing journals are refused: full replay checkpoint/resume is NOT supported.
class ManualRiskCycle {
 public:
  static constexpr std::int64_t kDayMs = 86400000;
  static constexpr std::int64_t kCycleDurationMs = 7 * kDayMs;
  static constexpr double kRecoveryLossFraction = 0.02;
  using AuditSink = std::function<bool(const std::string&)>;

  explicit ManualRiskCycle(double cap, RiskThresholds thresholds = {},
                           AuditSink test_sink = {});
  ~ManualRiskCycle();
  ManualRiskCycle(const ManualRiskCycle&) = delete;
  ManualRiskCycle& operator=(const ManualRiskCycle&) = delete;

  bool OpenNewJournal(const std::string& path, std::string* error);
  void Observe(std::int64_t ts_ms, const AccountState& account,
               bool healthy, bool pending_orders);
  bool Approve(const ManualRiskApproval& approval, std::string* error);
  const ManualRiskCycleState& state() const { return state_; }
  double gross_cap_usd() const;
  double control_drawdown() const;

 private:
  void Commit(ManualRiskCycleState next, const std::string& event,
               const std::string& approval = "-", const std::string& review = "-");
  bool Write(const std::string& record);
  double original_cap_;
  RiskThresholds thresholds_;
  ManualRiskCycleState state_;
  std::unordered_set<std::string> used_approval_ids_;
  AuditSink sink_;
  int journal_fd_{-1};
  bool fatal_{false};
};

} // namespace ai_trade
