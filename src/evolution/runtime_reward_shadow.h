#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>

#include "evolution/self_evolution_controller.h"
#include "oms/account_state.h"

namespace ai_trade {

struct ShadowExitCost {
  double fee_bps{5.5};
  double slippage_bps{1.0};
  double tick_size{0.1};
};
enum class ShadowEvent { kBaseline, kMarket, kFill, kFunding, kRebase,
                         kReflectedFill, kOrder, kEvolution };
struct ShadowPosition {
  std::string symbol;
  double qty{}, average{}, mark{}, quote{}, exit_price{}, exit_fee{};
};
struct ShadowReceipt {
  std::size_t sequence{};
  ShadowEvent event{};
  // Local processing order, NOT an exchange execution timestamp.
  Timestamp market_clock{};
  bool valid{false};
  std::optional<double> reward;
  double cash{}, equity{}, liquidation{}, exit_reserve{}, fees{}, funding{}, realized{};
  std::vector<ShadowPosition> positions;
  std::optional<FillEvent> fill;
  std::optional<MarketEvent> market;
  std::optional<OrderIntent> origin;
  int origin_bucket{-1};
  EvolutionWeights origin_weights{};
  double funding_paid{};
  std::string funding_symbol;
  double funding_rate{};
  bool exact_funding{};
  std::optional<SelfEvolutionAction> legacy_action;
};

// Bounded, single event-loop thread, observational only. No adapter, mutable
// AccountState reference, callbacks, order actuator or learner feedback.
// No on-disk journal: restart/rebase starts a NEW measurement, not continuity.
class RuntimeRewardShadow {
 public:
  explicit RuntimeRewardShadow(std::map<std::string, ShadowExitCost> costs,
                               std::size_t capacity = 10000,
                               Timestamp max_quote_age_ms = 60000);
  void Capture(ShadowEvent event, const AccountState& account,
               const MarketEvent* market = nullptr, const FillEvent* fill = nullptr,
               const OrderIntent* intent = nullptr, int bucket = -1,
               EvolutionWeights weights = {}, double funding_paid = 0.0,
               const SelfEvolutionAction* action = nullptr,
               const std::string* funding_symbol = nullptr,
               double funding_rate = 0.0, bool exact_funding = false) noexcept;
  bool valid() const noexcept { return failure_ == nullptr; }
  const char* failure() const noexcept { return failure_ ? failure_ : "NONE"; }
  const std::vector<ShadowReceipt>& receipts() const noexcept { return receipts_; }
 private:
  struct Quote { double price; Timestamp time; };
  struct Origin { OrderIntent intent; int bucket; EvolutionWeights weights; };
  std::map<std::string, ShadowExitCost> costs_;
  std::map<std::string, Quote> quotes_;
  std::map<std::string, Origin> origins_;
  std::vector<ShadowReceipt> receipts_;
  std::size_t capacity_;
  Timestamp max_quote_age_, clock_{0};
  std::optional<double> previous_liquidation_;
  const char* failure_{nullptr};
};
}  // namespace ai_trade
