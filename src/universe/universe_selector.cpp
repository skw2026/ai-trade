#include "universe/universe_selector.h"

#include <algorithm>
#include <cmath>
#include <unordered_set>
#include <utility>

namespace ai_trade {

namespace {

constexpr double kEpsilon = 1e-9;

struct CandidateScore {
  std::string symbol;
  double score{0.0};
  double trend_score{0.0};
};

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
      const double ret = (price - stats.last_price) / stats.last_price;
      const double abs_ret = std::fabs(ret);
      stats.abs_return_sum += abs_ret;
      stats.signed_return_sum += ret;
      ++stats.return_count;
    }
    stats.last_price = price;
    stats.has_last_price = true;
  }
  ++stats.tick_count;
  stats.last_turnover = event.volume * price; // 估算 24h 成交额 (volume 为 24h 基础币种量)

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

  std::vector<CandidateScore> candidate_scores;
  candidate_scores.reserve(candidates.size());
  for (const auto& symbol : candidates) {
    if (!IsAllowed(symbol)) {
      continue;
    }
    const auto it = stats_by_symbol_.find(symbol);
    if (it == stats_by_symbol_.end()) {
      candidate_scores.push_back(CandidateScore{symbol, 0.0, 0.0});
      continue;
    }
    const MarketStats& stats = it->second;
    // 过滤：成交额不足
    if (stats.last_turnover < config_.min_turnover_usd) {
      continue;
    }
    // 主评分偏向稳健成交与波动；趋势评分用于保留探索槽，避免 live 长期锁死在
    // “高活跃但无趋势”的币对上。
    const double activity = Clamp01(static_cast<double>(stats.tick_count) / 10.0);
    const double volatility =
        (stats.return_count <= 0)
            ? 0.0
            : Clamp01((stats.abs_return_sum / static_cast<double>(stats.return_count)) *
                      200.0);
    const double trendiness =
        (stats.return_count <= 0 || stats.abs_return_sum <= kEpsilon)
            ? 0.0
            : Clamp01(std::fabs(stats.signed_return_sum) /
                      (stats.abs_return_sum + kEpsilon));
    const double score = 0.50 * activity + 0.30 * volatility + 0.20 * trendiness;
    const double trend_score =
        0.35 * activity + 0.15 * volatility + 0.50 * trendiness;
    candidate_scores.push_back(CandidateScore{symbol, score, trend_score});
  }

  // 按分数降序排列
  std::sort(candidate_scores.begin(), candidate_scores.end(),
            [](const CandidateScore& lhs, const CandidateScore& rhs) {
              if (std::fabs(lhs.score - rhs.score) > 1e-12) {
                return lhs.score > rhs.score;
              }
              return lhs.symbol < rhs.symbol;
            });

  std::vector<std::string> selected;
  selected.reserve(static_cast<std::size_t>(config_.max_active_symbols));
  std::vector<std::string> reserve_selected_symbols;
  std::vector<std::string> sticky_trend_reserve_symbols;
  std::unordered_set<std::string> candidate_score_symbols;
  candidate_score_symbols.reserve(candidate_scores.size());
  for (const auto& candidate : candidate_scores) {
    candidate_score_symbols.insert(candidate.symbol);
  }
  const int reserve_slots =
      (config_.trend_reserve_enabled ? config_.trend_reserve_slots : 0);
  const int core_slots = std::max(0, config_.max_active_symbols - reserve_slots);

  for (const auto& candidate : candidate_scores) {
    if (static_cast<int>(selected.size()) >= core_slots) {
      break;
    }
    selected.push_back(candidate.symbol);
  }

  if (reserve_slots > 0 &&
      static_cast<int>(selected.size()) < config_.max_active_symbols) {
    std::vector<std::pair<std::string, int>> sticky_candidates;
    if (config_.trend_reserve_min_residency_refreshes > 0) {
      for (const auto& [symbol, remaining] :
           trend_reserve_residency_remaining_) {
        if (remaining <= 0 || !IsAllowed(symbol) ||
            candidate_score_symbols.count(symbol) == 0U) {
          continue;
        }
        if (std::find(selected.begin(), selected.end(), symbol) !=
            selected.end()) {
          continue;
        }
        sticky_candidates.emplace_back(symbol, remaining);
      }
      std::sort(sticky_candidates.begin(),
                sticky_candidates.end(),
                [](const auto& lhs, const auto& rhs) {
                  if (lhs.second != rhs.second) {
                    return lhs.second > rhs.second;
                  }
                  return lhs.first < rhs.first;
                });
    }
    for (const auto& [symbol, remaining] : sticky_candidates) {
      (void)remaining;
      if (static_cast<int>(selected.size()) >= config_.max_active_symbols ||
          static_cast<int>(reserve_selected_symbols.size()) >= reserve_slots) {
        break;
      }
      selected.push_back(symbol);
      reserve_selected_symbols.push_back(symbol);
      sticky_trend_reserve_symbols.push_back(symbol);
    }

    std::vector<CandidateScore> trend_candidates = candidate_scores;
    std::sort(trend_candidates.begin(), trend_candidates.end(),
              [](const CandidateScore& lhs, const CandidateScore& rhs) {
                if (std::fabs(lhs.trend_score - rhs.trend_score) > 1e-12) {
                  return lhs.trend_score > rhs.trend_score;
                }
                if (std::fabs(lhs.score - rhs.score) > 1e-12) {
                  return lhs.score > rhs.score;
                }
                return lhs.symbol < rhs.symbol;
              });
    for (const auto& candidate : trend_candidates) {
      if (static_cast<int>(selected.size()) >= config_.max_active_symbols) {
        break;
      }
      if (static_cast<int>(reserve_selected_symbols.size()) >= reserve_slots) {
        break;
      }
      if (std::find(selected.begin(), selected.end(), candidate.symbol) !=
          selected.end()) {
        continue;
      }
      selected.push_back(candidate.symbol);
      reserve_selected_symbols.push_back(candidate.symbol);
    }
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

  std::unordered_map<std::string, int> next_residency;
  if (reserve_slots > 0 &&
      config_.trend_reserve_min_residency_refreshes > 0 && !degraded) {
    for (const auto& symbol : sticky_trend_reserve_symbols) {
      const auto it = trend_reserve_residency_remaining_.find(symbol);
      const int remaining =
          (it == trend_reserve_residency_remaining_.end())
              ? 0
              : std::max(0, it->second - 1);
      if (remaining > 0) {
        next_residency[symbol] = remaining;
      }
    }
    for (const auto& symbol : reserve_selected_symbols) {
      if (std::find(sticky_trend_reserve_symbols.begin(),
                    sticky_trend_reserve_symbols.end(),
                    symbol) != sticky_trend_reserve_symbols.end()) {
        continue;
      }
      if (active_symbol_set_.count(symbol) == 0U) {
        continue;
      }
      next_residency[symbol] =
          config_.trend_reserve_min_residency_refreshes;
    }
  }
  trend_reserve_residency_remaining_ = std::move(next_residency);

  std::vector<SymbolScore> scores;
  scores.reserve(candidate_scores.size());
  for (const auto& candidate : candidate_scores) {
    scores.push_back(SymbolScore{candidate.symbol, candidate.score});
  }

  if (config_.reset_stats_on_refresh) {
    ResetWindowStats();
  }

  UniverseUpdate update;
  update.degraded_to_fallback = degraded;
  update.reason_code = reason_code;
  update.active_symbols = active_symbols_;
  update.symbol_scores = std::move(scores);
  update.sticky_trend_reserve_symbols =
      std::move(sticky_trend_reserve_symbols);
  return update;
}

void UniverseSelector::ResetWindowStats() {
  for (auto& [symbol, stats] : stats_by_symbol_) {
    (void)symbol;
    stats.abs_return_sum = 0.0;
    stats.signed_return_sum = 0.0;
    stats.return_count = 0;
    stats.tick_count = 0;
  }
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
