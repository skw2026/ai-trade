#pragma once

#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "core/config.h"
#include "core/types.h"

namespace ai_trade {

struct SymbolScore {
  std::string symbol;
  double score{0.0};
};

struct UniverseUpdate {
  bool degraded_to_fallback{false};
  std::string reason_code;
  std::vector<std::string> active_symbols;
  std::vector<SymbolScore> symbol_scores;
};

class UniverseSelector {
 public:
  UniverseSelector(UniverseConfig config, std::string primary_symbol);

  std::optional<UniverseUpdate> OnMarket(const MarketEvent& event);
  void SetAllowedSymbols(const std::vector<std::string>& symbols);
  bool IsActive(const std::string& symbol) const;
  const std::vector<std::string>& active_symbols() const { return active_symbols_; }

 private:
  struct MarketStats {
    bool has_last_price{false};
    double last_price{0.0};
    double abs_return_sum{0.0};
    int return_count{0};
    int tick_count{0};
  };

  std::optional<UniverseUpdate> Refresh();
  static std::vector<std::string> UniqueSymbols(
      const std::vector<std::string>& symbols);
  bool IsAllowed(const std::string& symbol) const;
  void NormalizeActiveSymbols();
  void RebuildActiveSet();

  UniverseConfig config_;
  std::string primary_symbol_;
  int tick_count_{0};
  std::unordered_map<std::string, MarketStats> stats_by_symbol_;
  std::unordered_set<std::string> seen_symbols_;
  std::vector<std::string> active_symbols_;
  std::unordered_set<std::string> active_symbol_set_;
  bool allowed_symbol_filter_enabled_{false};
  std::unordered_set<std::string> allowed_symbol_set_;
};

}  // namespace ai_trade
