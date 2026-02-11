#include "universe/universe_selector.h"

#include <algorithm>
#include <cmath>
#include <utility>

namespace ai_trade {

namespace {

constexpr double kEpsilon = 1e-9;

double Clamp01(double value) {
  return std::clamp(value, 0.0, 1.0);
}

}  // namespace

UniverseSelector::UniverseSelector(UniverseConfig config, std::string primary_symbol)
    : config_(std::move(config)), primary_symbol_(std::move(primary_symbol)) {
  config_.fallback_symbols = UniqueSymbols(config_.fallback_symbols);
  config_.candidate_symbols = UniqueSymbols(config_.candidate_symbols);
  if (config_.fallback_symbols.empty()) {
    config_.fallback_symbols.push_back(primary_symbol_);
  }
  if (config_.candidate_symbols.empty()) {
    config_.candidate_symbols = config_.fallback_symbols;
  }

  active_symbols_ = config_.fallback_symbols;
  if (static_cast<int>(active_symbols_.size()) > config_.max_active_symbols) {
    active_symbols_.resize(static_cast<std::size_t>(config_.max_active_symbols));
  }
  RebuildActiveSet();
}

std::optional<UniverseUpdate> UniverseSelector::OnMarket(const MarketEvent& event) {
  if (event.symbol.empty()) {
    return std::nullopt;
  }
  seen_symbols_.insert(event.symbol);

  auto& stats = stats_by_symbol_[event.symbol];
  const double price = (event.mark_price > 0.0) ? event.mark_price : event.price;
  if (price > 0.0) {
    if (stats.has_last_price && stats.last_price > kEpsilon) {
      const double abs_ret = std::fabs((price - stats.last_price) / stats.last_price);
      stats.abs_return_sum += abs_ret;
      ++stats.return_count;
    }
    stats.last_price = price;
    stats.has_last_price = true;
  }
  ++stats.tick_count;

  // 按 tick 间隔触发刷新，避免每个行情都重排 Universe。
  ++ticks_since_update_;
  if (config_.update_interval_ticks > 0 &&
      ticks_since_update_ < config_.update_interval_ticks) {
    return std::nullopt;
  }
  ticks_since_update_ = 0;
  return Refresh();
}

void UniverseSelector::SetAllowedSymbols(const std::vector<std::string>& symbols) {
  allowed_symbol_set_.clear();
  const std::vector<std::string> unique = UniqueSymbols(symbols);
  for (const auto& symbol : unique) {
    allowed_symbol_set_.insert(symbol);
  }
  allowed_symbol_filter_enabled_ = !allowed_symbol_set_.empty();
  NormalizeActiveSymbols();
}

bool UniverseSelector::IsActive(const std::string& symbol) const {
  if (!config_.enabled) {
    return true;
  }
  return active_symbol_set_.count(symbol) != 0U;
}

std::optional<UniverseUpdate> UniverseSelector::Refresh() {
  std::vector<std::string> candidates = config_.candidate_symbols;
  // 如果没有配置候选池，则从所有见过的 symbol 中筛选
  if (candidates.empty()) {
    for (const auto& symbol : seen_symbols_) {
      candidates.push_back(symbol);
    }
    candidates = UniqueSymbols(candidates);
  }

  std::vector<SymbolScore> scores;
  scores.reserve(candidates.size());
  for (const auto& symbol : candidates) {
    if (!IsAllowed(symbol)) {
      continue;
    }
    const auto it = stats_by_symbol_.find(symbol);
    if (it == stats_by_symbol_.end()) {
      scores.push_back(SymbolScore{symbol, 0.0});
      continue;
    }
    const MarketStats& stats = it->second;
    // 评分公式：0.6 * 活跃度 + 0.4 * 波动率
    const double activity = Clamp01(static_cast<double>(stats.tick_count) / 10.0);
    const double volatility =
        (stats.return_count <= 0)
            ? 0.0
            : Clamp01((stats.abs_return_sum / static_cast<double>(stats.return_count)) *
                      200.0);
    const double score = 0.6 * activity + 0.4 * volatility;
    scores.push_back(SymbolScore{symbol, score});
  }

  // 按分数降序排列
  std::sort(scores.begin(), scores.end(),
            [](const SymbolScore& lhs, const SymbolScore& rhs) {
              if (std::fabs(lhs.score - rhs.score) > 1e-12) {
                return lhs.score > rhs.score;
              }
              return lhs.symbol < rhs.symbol;
            });

  std::vector<std::string> selected;
  selected.reserve(static_cast<std::size_t>(config_.max_active_symbols));
  for (const auto& score : scores) {
    if (static_cast<int>(selected.size()) >= config_.max_active_symbols) {
      break;
    }
    selected.push_back(score.symbol);
  }

  bool degraded = false;
  std::string reason_code;
  // 如果筛选结果为空，触发降级逻辑：使用 Fallback Symbols
  if (selected.empty()) {
    degraded = true;
    reason_code = "UNIVERSE_SELECTOR_DEGRADED";
    for (const auto& fallback : config_.fallback_symbols) {
      if (!IsAllowed(fallback)) {
        continue;
      }
      selected.push_back(fallback);
    }
  }

  for (const auto& fallback : config_.fallback_symbols) {
    if (!IsAllowed(fallback)) {
      continue;
    }
    if (static_cast<int>(selected.size()) >= config_.min_active_symbols ||
        static_cast<int>(selected.size()) >= config_.max_active_symbols) {
      break;
    }
    if (std::find(selected.begin(), selected.end(), fallback) == selected.end()) {
      selected.push_back(fallback);
      degraded = true;
      reason_code = "UNIVERSE_SELECTOR_DEGRADED";
    }
  }

  if (selected.empty()) {
    if (IsAllowed(primary_symbol_)) {
      selected.push_back(primary_symbol_);
      degraded = true;
      reason_code = "UNIVERSE_SELECTOR_DEGRADED";
    }
  }

  selected = UniqueSymbols(selected);
  if (static_cast<int>(selected.size()) > config_.max_active_symbols) {
    selected.resize(static_cast<std::size_t>(config_.max_active_symbols));
  }

  active_symbols_ = std::move(selected);
  RebuildActiveSet();

  UniverseUpdate update;
  update.degraded_to_fallback = degraded;
  update.reason_code = reason_code;
  update.active_symbols = active_symbols_;
  update.symbol_scores = std::move(scores);
  return update;
}

std::vector<std::string> UniverseSelector::UniqueSymbols(
    const std::vector<std::string>& symbols) {
  std::vector<std::string> out;
  out.reserve(symbols.size());
  std::unordered_set<std::string> seen;
  for (const auto& symbol : symbols) {
    if (symbol.empty()) {
      continue;
    }
    if (seen.insert(symbol).second) {
      out.push_back(symbol);
    }
  }
  return out;
}

void UniverseSelector::RebuildActiveSet() {
  active_symbol_set_.clear();
  for (const auto& symbol : active_symbols_) {
    active_symbol_set_.insert(symbol);
  }
}

bool UniverseSelector::IsAllowed(const std::string& symbol) const {
  if (!allowed_symbol_filter_enabled_) {
    return true;
  }
  return allowed_symbol_set_.count(symbol) != 0U;
}

void UniverseSelector::NormalizeActiveSymbols() {
  std::vector<std::string> filtered;
  filtered.reserve(active_symbols_.size());
  for (const auto& symbol : active_symbols_) {
    if (!IsAllowed(symbol)) {
      continue;
    }
    filtered.push_back(symbol);
  }
  if (filtered.empty()) {
    for (const auto& fallback : config_.fallback_symbols) {
      if (!IsAllowed(fallback)) {
        continue;
      }
      filtered.push_back(fallback);
      if (static_cast<int>(filtered.size()) >= config_.max_active_symbols) {
        break;
      }
    }
  }
  active_symbols_ = UniqueSymbols(filtered);
  if (static_cast<int>(active_symbols_.size()) > config_.max_active_symbols) {
    active_symbols_.resize(static_cast<std::size_t>(config_.max_active_symbols));
  }
  RebuildActiveSet();
}

}  // namespace ai_trade
