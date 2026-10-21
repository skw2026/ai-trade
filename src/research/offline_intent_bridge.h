#pragma once

#include <cstdint>
#include <map>
#include <set>
#include <string>

#include "exchange/mock_exchange_adapter.h"
#include "execution/async_executor.h"
#include "execution/execution_engine.h"
#include "oms/account_state.h"
#include "oms/order_manager.h"
#include "storage/wal_store.h"

namespace ai_trade::research {

// Test-only bridge: deliberately accepts MockExchangeAdapter, never the live
// ExchangeAdapter interface. Not linked into ai_trade_system or trade_bot.
// One main-thread owner; background tests must synchronize their mock adapter.
class OfflineIntentBridge {
 public:
  OfflineIntentBridge(MockExchangeAdapter& adapter, const std::string& directory,
                      bool operator_reduce_only = false,
                      AsyncExecutor::Mode mode = AsyncExecutor::Mode::kInlineReplay);
  ~OfflineIntentBridge();
  OfflineIntentBridge(const OfflineIntentBridge&) = delete;
  OfflineIntentBridge& operator=(const OfflineIntentBridge&) = delete;

  // Completed synthetic features only. The (0,-4) rule reads previous INTENT,
  // never actual fills or pending quantity. Same bar is idempotent if identical.
  void OnBar(std::int64_t timestamp, double short_feature, double long_feature,
             double price);
  void LatchRiskStop();
  void LatchOperatorReduceOnly();
  void PumpResults();
  void OnFill(const FillEvent& fill);
  // Offline quiescent mock cut: no transport ACK outstanding, positions and
  // all open order leaves must match. Never silently replace local positions.
  bool Reconcile();
  // No automatic retries. Refused/unknown sends block until reconciliation.
  bool Drive(double price);

  const OrderManager& oms() const { return oms_; }
  const AccountState& account() const { return account_; }
  double intent_target() const { return target_; }
  double goal_qty() const { return goal_qty_; }
  std::uint64_t version() const { return version_; }
  bool ready() const { return ready_; }
  bool stopped() const { return stopped_; }
  bool operator_locked() const { return operator_locked_; }
  std::size_t unique_fills() const { return fills_.size(); }

 private:
  void ApplyBar(std::int64_t timestamp, double short_feature, double long_feature,
                double price);
  void AppendState(const std::string& record);
  void CancelPending();
  void Recover();
  [[noreturn]] void FailClosed(const char* reason);
  MockExchangeAdapter& adapter_;
  AsyncExecutor executor_;
  ExecutionEngine engine_{2500.0};
  OrderManager oms_;
  AccountState account_;
  WalStore wal_;
  std::string journal_path_;
  int lock_fd_{-1};
  std::map<std::string, FillEvent> fills_;
  std::set<std::string> submit_wait_, cancel_wait_, cancelling_;
  std::uint64_t version_{0}, order_sequence_{0};
  std::int64_t timestamp_{-1};
  double target_{0}, goal_qty_{0}, short_feature_{0}, long_feature_{0}, bar_price_{0};
  double risk_peak_{10000.0};
  bool stopped_{false}, operator_locked_{false}, ready_{false}, faulted_{false};
};

}  // namespace ai_trade::research
