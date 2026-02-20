#include "app/bot_app.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <sstream>
#include <thread>
#include <unordered_set>
#include <utility>

#include "core/log.h"
#include "app/intent_policy.h"
#include "exchange/binance_exchange_adapter.h"
#include "exchange/bybit_exchange_adapter.h"
#include "exchange/mock_exchange_adapter.h"

namespace ai_trade {

namespace {

constexpr double kNotionalEpsilon = 1e-9;
// 成交落地后，给远端持仓快照一个极短收敛窗口，避免瞬时对账误判。
constexpr int kReconcileRecentFillGraceTicks = 2;
// 自动远端重对齐最小间隔，避免短时间重复覆盖本地状态。
constexpr int kReconcileAutoResyncCooldownTicks = 40;

// 统一毫秒时间戳，供节流、日志和心跳逻辑复用。
std::int64_t CurrentTimestampMs() {
  const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now());
  return now.time_since_epoch().count();
}

// 去重并保序：确保 symbol 列表可直接用于配置下发与日志展示。
std::vector<std::string> UniqueSymbols(const std::vector<std::string>& symbols) {
  std::vector<std::string> out;
  std::unordered_set<std::string> seen;
  for (const auto& symbol : symbols) {
    if (!symbol.empty() && seen.insert(symbol).second) {
      out.push_back(symbol);
    }
  }
  return out;
}

bool HasExposure(double notional_usd) {
  return std::fabs(notional_usd) > kNotionalEpsilon;
}

// 将策略分支名义值按“可执行目标名义值”缩放，减少学习输入与执行结果的偏离。
std::pair<double, double> ScaleStrategyComponentsForExecution(
    const MarketDecision& decision) {
  const double base_blended_notional = decision.base_signal.suggested_notional_usd;
  const double executable_notional = decision.risk_adjusted.adjusted_notional_usd;
  if (!std::isfinite(base_blended_notional) || !std::isfinite(executable_notional) ||
      !HasExposure(base_blended_notional)) {
    return {0.0, 0.0};
  }
  constexpr double kEvolutionSignalScaleLimit = 4.0;
  const double scale = std::clamp(executable_notional / base_blended_notional,
                                  -kEvolutionSignalScaleLimit,
                                  kEvolutionSignalScaleLimit);
  return {decision.base_signal.trend_notional_usd * scale,
          decision.base_signal.defensive_notional_usd * scale};
}

bool IsNetPositionOrderPurpose(OrderPurpose purpose) {
  return purpose == OrderPurpose::kEntry || purpose == OrderPurpose::kReduce;
}

const char* RiskModeToString(RiskMode mode) {
  switch (mode) {
    case RiskMode::kNormal:
      return "normal";
    case RiskMode::kDegraded:
      return "degraded";
    case RiskMode::kCooldown:
      return "cooldown";
    case RiskMode::kFuse:
      return "fuse";
    case RiskMode::kReduceOnly:
      return "reduce_only";
  }
  return "unknown";
}

const char* EvolutionActionTypeToString(SelfEvolutionActionType type) {
  switch (type) {
    case SelfEvolutionActionType::kUpdated:
      return "updated";
    case SelfEvolutionActionType::kRolledBack:
      return "rolled_back";
    case SelfEvolutionActionType::kSkipped:
      return "skipped";
  }
  return "unknown";
}

const char* OrderStateToString(OrderState state) {
  switch (state) {
    case OrderState::kNew:
      return "new";
    case OrderState::kSent:
      return "sent";
    case OrderState::kPartial:
      return "partial";
    case OrderState::kFilled:
      return "filled";
    case OrderState::kRejected:
      return "rejected";
    case OrderState::kCancelled:
      return "cancelled";
  }
  return "unknown";
}

std::string FormatAccountPositions(const AccountState& account) {
  const auto symbols = account.GetActiveSymbols();
  if (symbols.empty()) {
    return "flat";
  }
  std::ostringstream oss;
  bool first = true;
  for (const auto& symbol : symbols) {
    const double qty = account.position_qty(symbol);
    if (std::fabs(qty) <= kNotionalEpsilon) {
      continue;
    }
    if (!first) {
      oss << ";";
    }
    first = false;
    const double mark = account.mark_price(symbol);
    const double notional = account.current_notional_usd(symbol);
    oss << symbol << ":qty=" << qty << ",mark=" << mark
        << ",notional=" << notional;
  }
  if (first) {
    return "flat";
  }
  return oss.str();
}

/**
 * @brief 执行层前置最小名义敞口过滤
 *
 * 目的：
 * - 在下单入队前提前拦截“达不到交易所最小名义金额”的订单；
 * - 避免反复触发交易所拒单导致抖动与无效重试。
 */
bool ViolatesMinNotionalGuard(const ExchangeAdapter* adapter,
                              const OrderIntent& intent,
                              const MarketEvent& event,
                              std::string* out_reason) {
  if (adapter == nullptr || intent.reduce_only) {
    return false;
  }
  SymbolInfo info;
  if (!adapter->GetSymbolInfo(intent.symbol, &info)) {
    return false;
  }
  if (!info.tradable || info.min_notional_usd <= 0.0) {
    return false;
  }
  const double ref_price =
      intent.price > 0.0 ? intent.price
                         : (event.mark_price > 0.0 ? event.mark_price : event.price);
  if (ref_price <= 0.0) {
    return false;
  }
  const double order_notional = intent.qty * ref_price;
  if (order_notional + 1e-9 >= info.min_notional_usd) {
    return false;
  }

  if (out_reason != nullptr) {
    *out_reason = "min_notional_guard(order_notional=" +
                  std::to_string(order_notional) +
                  ", min_notional=" + std::to_string(info.min_notional_usd) + ")";
  }
  return true;
}

/**
 * @brief 根据配置创建交易所适配器实例
 *
 * 约束：
 * 1. Universe 模式下将候选池与 fallback 合并后传入适配器；
 * 2. Bybit 模式注入完整账户模式预期，供启动门禁校验；
 * 3. 未识别交易所时退化到 Mock，保证本地可运行。
 */
std::unique_ptr<ExchangeAdapter> CreateAdapter(const AppConfig& config) {
  auto collect_symbols = [&config]() {
    std::vector<std::string> symbols;
    if (config.universe.enabled) {
      symbols = config.universe.candidate_symbols;
      symbols.insert(symbols.end(), config.universe.fallback_symbols.begin(),
                     config.universe.fallback_symbols.end());
    }
    symbols.push_back(config.primary_symbol);
    return UniqueSymbols(symbols);
  };

  if (config.exchange == "bybit") {
    BybitAdapterOptions options;
    options.testnet = config.bybit.testnet;
    options.demo_trading = config.bybit.demo_trading;
    options.mode = config.mode;
    options.category = config.bybit.category;
    options.account_type = config.bybit.account_type;
    options.primary_symbol = config.primary_symbol;
    options.public_ws_enabled = config.bybit.public_ws_enabled;
    options.public_ws_rest_fallback = config.bybit.public_ws_rest_fallback;
    options.private_ws_enabled = config.bybit.private_ws_enabled;
    options.private_ws_rest_fallback = config.bybit.private_ws_rest_fallback;
    options.ws_reconnect_interval_ms = config.bybit.ws_reconnect_interval_ms;
    options.execution_poll_limit = config.bybit.execution_poll_limit;
    options.maker_entry_enabled = config.execution_maker_entry_enabled;
    options.maker_fallback_to_market = config.execution_maker_fallback_to_market;
    options.maker_price_offset_bps = config.execution_maker_price_offset_bps;
    options.maker_post_only = config.execution_maker_post_only;
    options.symbols = collect_symbols();
    options.remote_account_mode = config.bybit.expected_account_mode;
    options.remote_margin_mode = config.bybit.expected_margin_mode;
    options.remote_position_mode = config.bybit.expected_position_mode;
    return std::make_unique<BybitExchangeAdapter>(options);
  }

  if (config.exchange == "binance") {
    return std::make_unique<BinanceExchangeAdapter>();
  }

  // Mock Adapter 默认行为
  std::vector<double> prices = {100.0, 100.5, 100.7, 100.2, 99.8, 100.1};
  return std::make_unique<MockExchangeAdapter>(prices, collect_symbols());
}

/**
 * @brief 启动前账户模式校验（Bybit 专用）
 *
 * 只有当账户模式、保证金模式、持仓模式全部匹配时才允许启动，
 * 防止策略在错误账户形态下误下单。
 */
bool ValidateAccountSnapshot(const AppConfig& config, ExchangeAdapter* adapter) {
  if (adapter == nullptr) return false;
  if (config.exchange != "bybit") return true;

  ExchangeAccountSnapshot snapshot;
  if (!adapter->GetAccountSnapshot(&snapshot)) {
    LogInfo("账户快照读取失败：Bybit 模式下拒绝启动以确保安全");
    return false;
  }

  const bool ok = (snapshot.account_mode == config.bybit.expected_account_mode &&
                   snapshot.margin_mode == config.bybit.expected_margin_mode &&
                   snapshot.position_mode == config.bybit.expected_position_mode);
  
  if (!ok) {
    LogInfo("账户模式校验失败: 请检查 Unified/Isolated/OneWay 设置");
  }
  return ok;
}

}  // namespace

BotApplication::BotApplication(const AppConfig& config)
    : config_(config),
      system_(config),
      execution_(config.execution_max_order_notional),
      order_throttle_({
          .min_order_interval_ms = config.execution_min_order_interval_ms,
          .reverse_signal_cooldown_ticks =
              config.execution_reverse_signal_cooldown_ticks,
      }),
      self_evolution_(config.self_evolution),
      reconciler_(config.reconcile.tolerance_notional_usd),
      gate_monitor_(config.gate),
      universe_selector_(config.universe, config.primary_symbol),
      wal_(config.data_path + "/trade.wal") {}

double BotApplication::RoundTripCostBps() const {
  const double entry_fee_bps = std::max(0.0, config_.execution_entry_fee_bps);
  const double exit_fee_bps = std::max(0.0, config_.execution_exit_fee_bps);
  const double slippage_bps =
      std::max(0.0, config_.execution_expected_slippage_bps);
  return entry_fee_bps + exit_fee_bps + 2.0 * slippage_bps;
}

double BotApplication::EstimateEntryEdgeBps(const MarketDecision& decision,
                                            const MarketEvent& event) const {
  if (!decision.intent.has_value() || !IsOpeningIntent(*decision.intent)) {
    return 0.0;
  }
  const int direction = decision.intent->direction;
  if (direction == 0) {
    return 0.0;
  }
  const double price = event.price > 0.0 ? event.price : decision.intent->price;
  const double deadband_bps =
      (price > 0.0 && config_.strategy_signal_deadband_abs > 0.0)
          ? std::fabs(config_.strategy_signal_deadband_abs) / price * 10000.0
          : 0.0;

  const double trend_edge_bps = std::max(
      0.0, decision.regime.trend_strength * static_cast<double>(direction) * 10000.0);
  const double instant_edge_bps = std::max(
      0.0, decision.regime.instant_return * static_cast<double>(direction) * 10000.0);
  const double regime_edge_bps = 0.6 * trend_edge_bps + 0.4 * instant_edge_bps;
  return std::max(deadband_bps, regime_edge_bps);
}

bool BotApplication::ShouldFilterByFeeAwareGate(
    const MarketDecision& decision,
    const MarketEvent& event,
    double* out_expected_edge_bps,
    double* out_required_edge_bps,
    double* out_base_required_edge_bps,
    double* out_adaptive_relax_bps,
    double* out_maker_relax_bps,
    double* out_regime_adjust_bps,
    double* out_volatility_adjust_bps,
    double* out_liquidity_adjust_bps,
    double* out_quality_guard_penalty_bps,
    double* out_observed_filtered_ratio,
    double* out_edge_gap_bps,
    bool* out_near_miss,
    bool* out_near_miss_allowed) const {
  if (out_expected_edge_bps != nullptr) {
    *out_expected_edge_bps = 0.0;
  }
  if (out_required_edge_bps != nullptr) {
    *out_required_edge_bps = 0.0;
  }
  if (out_base_required_edge_bps != nullptr) {
    *out_base_required_edge_bps = 0.0;
  }
  if (out_adaptive_relax_bps != nullptr) {
    *out_adaptive_relax_bps = 0.0;
  }
  if (out_maker_relax_bps != nullptr) {
    *out_maker_relax_bps = 0.0;
  }
  if (out_regime_adjust_bps != nullptr) {
    *out_regime_adjust_bps = 0.0;
  }
  if (out_volatility_adjust_bps != nullptr) {
    *out_volatility_adjust_bps = 0.0;
  }
  if (out_liquidity_adjust_bps != nullptr) {
    *out_liquidity_adjust_bps = 0.0;
  }
  if (out_quality_guard_penalty_bps != nullptr) {
    *out_quality_guard_penalty_bps = 0.0;
  }
  if (out_observed_filtered_ratio != nullptr) {
    *out_observed_filtered_ratio = entry_gate_observed_filtered_ratio_;
  }
  if (out_edge_gap_bps != nullptr) {
    *out_edge_gap_bps = 0.0;
  }
  if (out_near_miss != nullptr) {
    *out_near_miss = false;
  }
  if (out_near_miss_allowed != nullptr) {
    *out_near_miss_allowed = false;
  }
  if (!decision.intent.has_value() || !IsOpeningIntent(*decision.intent)) {
    return false;
  }

  const double expected_edge_bps = EstimateEntryEdgeBps(decision, event);
  const double round_trip_cost_bps = RoundTripCostBps();
  const double base_required_edge_bps =
      round_trip_cost_bps + std::max(0.0, config_.execution_min_expected_edge_bps);
  double required_edge_bps = base_required_edge_bps;
  if (config_.execution_required_edge_cap_bps > 0.0) {
    required_edge_bps =
        std::min(required_edge_bps, config_.execution_required_edge_cap_bps);
  }
  double adaptive_relax_bps = 0.0;
  if (config_.execution_adaptive_fee_gate_enabled &&
      static_cast<int>(entry_gate_observed_samples_) >=
          config_.execution_adaptive_fee_gate_min_samples) {
    const double trigger_ratio =
        std::clamp(config_.execution_adaptive_fee_gate_trigger_ratio, 0.0, 1.0);
    if (entry_gate_observed_filtered_ratio_ > trigger_ratio &&
        trigger_ratio < 1.0) {
      const double scale =
          (entry_gate_observed_filtered_ratio_ - trigger_ratio) /
          (1.0 - trigger_ratio);
      adaptive_relax_bps =
          std::clamp(scale, 0.0, 1.0) *
          std::max(0.0, config_.execution_adaptive_fee_gate_max_relax_bps);
    }
  }
  const bool maker_entry_candidate =
      config_.execution_maker_entry_enabled &&
      decision.intent->purpose == OrderPurpose::kEntry && !decision.intent->reduce_only;
  const double maker_relax_bps =
      maker_entry_candidate ? std::max(0.0, config_.execution_maker_edge_relax_bps)
                            : 0.0;
  double regime_adjust_bps = 0.0;
  double volatility_adjust_bps = 0.0;
  double liquidity_adjust_bps = 0.0;
  if (config_.execution_dynamic_edge_enabled) {
    if (decision.regime.bucket == RegimeBucket::kTrend) {
      regime_adjust_bps =
          -std::max(0.0, config_.execution_dynamic_edge_regime_trend_relax_bps);
    } else if (decision.regime.bucket == RegimeBucket::kRange) {
      regime_adjust_bps =
          std::max(0.0, config_.execution_dynamic_edge_regime_range_penalty_bps);
    } else if (decision.regime.bucket == RegimeBucket::kExtreme) {
      regime_adjust_bps =
          std::max(0.0, config_.execution_dynamic_edge_regime_extreme_penalty_bps);
    }

    const double vol_threshold = std::max(1e-9, config_.regime.volatility_threshold);
    const double vol_ratio =
        std::clamp(decision.regime.volatility_level / vol_threshold, 0.0, 2.0);
    if (vol_ratio > 1.0) {
      volatility_adjust_bps = std::max(
          0.0, config_.execution_dynamic_edge_volatility_penalty_bps) *
          (vol_ratio - 1.0);
    } else if (vol_ratio < 1.0) {
      volatility_adjust_bps = -std::max(
          0.0, config_.execution_dynamic_edge_volatility_relax_bps) *
          (1.0 - vol_ratio);
    }

    if (recent_execution_window_maker_fill_ratio_ >=
        config_.execution_dynamic_edge_liquidity_maker_ratio_threshold) {
      const double maker_den =
          std::max(1e-9,
                   1.0 - config_.execution_dynamic_edge_liquidity_maker_ratio_threshold);
      const double maker_scale = std::clamp(
          (recent_execution_window_maker_fill_ratio_ -
           config_.execution_dynamic_edge_liquidity_maker_ratio_threshold) /
              maker_den,
          0.0, 1.0);
      liquidity_adjust_bps -=
          maker_scale *
          std::max(0.0, config_.execution_dynamic_edge_liquidity_relax_bps);
    }
    if (recent_execution_window_unknown_fill_ratio_ >=
        config_.execution_dynamic_edge_liquidity_unknown_ratio_threshold) {
      const double unknown_den = std::max(
          1e-9, 1.0 - config_.execution_dynamic_edge_liquidity_unknown_ratio_threshold);
      const double unknown_scale = std::clamp(
          (recent_execution_window_unknown_fill_ratio_ -
           config_.execution_dynamic_edge_liquidity_unknown_ratio_threshold) /
              unknown_den,
          0.0, 1.0);
      liquidity_adjust_bps +=
          unknown_scale *
          std::max(0.0, config_.execution_dynamic_edge_liquidity_penalty_bps);
    }
  }
  const double quality_guard_penalty_bps =
      std::max(0.0, execution_quality_required_edge_penalty_bps_);
  required_edge_bps = std::max(
      0.0,
      required_edge_bps - adaptive_relax_bps - maker_relax_bps +
          regime_adjust_bps + volatility_adjust_bps + liquidity_adjust_bps +
          quality_guard_penalty_bps);
  if (out_expected_edge_bps != nullptr) {
    *out_expected_edge_bps = expected_edge_bps;
  }
  if (out_required_edge_bps != nullptr) {
    *out_required_edge_bps = required_edge_bps;
  }
  if (out_base_required_edge_bps != nullptr) {
    *out_base_required_edge_bps = base_required_edge_bps;
  }
  if (out_adaptive_relax_bps != nullptr) {
    *out_adaptive_relax_bps = adaptive_relax_bps;
  }
  if (out_maker_relax_bps != nullptr) {
    *out_maker_relax_bps = maker_relax_bps;
  }
  if (out_regime_adjust_bps != nullptr) {
    *out_regime_adjust_bps = regime_adjust_bps;
  }
  if (out_volatility_adjust_bps != nullptr) {
    *out_volatility_adjust_bps = volatility_adjust_bps;
  }
  if (out_liquidity_adjust_bps != nullptr) {
    *out_liquidity_adjust_bps = liquidity_adjust_bps;
  }
  if (out_quality_guard_penalty_bps != nullptr) {
    *out_quality_guard_penalty_bps = quality_guard_penalty_bps;
  }
  if (!config_.execution_enable_fee_aware_entry_gate) {
    return false;
  }
  const double edge_gap_bps = required_edge_bps - expected_edge_bps;
  const double near_miss_tolerance_bps =
      std::max(0.0, config_.execution_entry_gate_near_miss_tolerance_bps);
  bool filtered = edge_gap_bps > near_miss_tolerance_bps + 1e-9;
  // 近阈值定义：落在“容差+附加带”内的样本，用于观测与可选 maker 放行。
  const double near_miss_band_bps =
      near_miss_tolerance_bps + std::max(0.05, near_miss_tolerance_bps);
  const bool near_miss = filtered && edge_gap_bps <= near_miss_band_bps;
  bool near_miss_allowed = false;
  if (near_miss && config_.execution_entry_gate_near_miss_maker_allow &&
      maker_entry_candidate && !config_.execution_maker_fallback_to_market) {
    const double allow_extra_gap_bps =
        std::max(0.0, config_.execution_entry_gate_near_miss_maker_max_gap_bps);
    const double allow_upper_gap_bps =
        near_miss_tolerance_bps + allow_extra_gap_bps;
    // 语义：maker_allow 配置是“在 tolerance 之上的附加 gap”。
    if (allow_extra_gap_bps > 0.0 &&
        edge_gap_bps <= allow_upper_gap_bps + 1e-9) {
      filtered = false;
      near_miss_allowed = true;
    }
  }
  if (out_edge_gap_bps != nullptr) {
    *out_edge_gap_bps = edge_gap_bps;
  }
  if (out_near_miss != nullptr) {
    *out_near_miss = near_miss;
  }
  if (out_near_miss_allowed != nullptr) {
    *out_near_miss_allowed = near_miss_allowed;
  }
  return filtered;
}

bool BotApplication::IsCostFilterCooldownActive(const std::string& symbol,
                                                int* out_remaining_ticks) {
  if (out_remaining_ticks != nullptr) {
    *out_remaining_ticks = 0;
  }
  if (symbol.empty()) {
    return false;
  }
  const auto it = cost_filter_cooldown_until_tick_by_symbol_.find(symbol);
  if (it == cost_filter_cooldown_until_tick_by_symbol_.end()) {
    return false;
  }
  if (market_tick_count_ >= it->second) {
    cost_filter_cooldown_until_tick_by_symbol_.erase(it);
    LogInfo("ORDER_COST_FILTER_COOLDOWN_EXIT: symbol=" + symbol +
            ", tick=" + std::to_string(market_tick_count_));
    return false;
  }
  const int remaining_ticks = it->second - market_tick_count_;
  if (out_remaining_ticks != nullptr) {
    *out_remaining_ticks = std::max(0, remaining_ticks);
  }
  return true;
}

void BotApplication::OnCostFilterRejected(const std::string& symbol) {
  if (symbol.empty()) {
    return;
  }
  auto& reject_streak = cost_filter_reject_streak_by_symbol_[symbol];
  ++reject_streak;
  const int trigger_count =
      std::max(0, config_.execution_cost_filter_cooldown_trigger_count);
  const int cooldown_ticks = std::max(0, config_.execution_cost_filter_cooldown_ticks);
  if (trigger_count <= 0 || cooldown_ticks <= 0 || reject_streak < trigger_count) {
    return;
  }
  cost_filter_cooldown_until_tick_by_symbol_[symbol] =
      market_tick_count_ + cooldown_ticks;
  reject_streak = 0;
  LogInfo("ORDER_COST_FILTER_COOLDOWN_ENTER: symbol=" + symbol +
          ", cooldown_ticks=" + std::to_string(cooldown_ticks) +
          ", until_tick=" + std::to_string(market_tick_count_ + cooldown_ticks));
}

void BotApplication::OnCostFilterAccepted(const std::string& symbol) {
  if (symbol.empty()) {
    return;
  }
  cost_filter_reject_streak_by_symbol_.erase(symbol);
}

void BotApplication::UpdateEntryGateObservedRatio(bool filtered,
                                                  bool near_miss,
                                                  bool near_miss_allowed) {
  ++entry_gate_observed_samples_;
  if (filtered) {
    ++entry_gate_observed_filtered_;
  }
  if (near_miss) {
    ++entry_gate_observed_near_miss_;
  }
  if (near_miss_allowed) {
    ++entry_gate_observed_near_miss_allowed_;
  }
  if (entry_gate_observed_samples_ > 0) {
    entry_gate_observed_filtered_ratio_ =
        static_cast<double>(entry_gate_observed_filtered_) /
        static_cast<double>(entry_gate_observed_samples_);
    entry_gate_observed_near_miss_ratio_ =
        static_cast<double>(entry_gate_observed_near_miss_) /
        static_cast<double>(entry_gate_observed_samples_);
    entry_gate_observed_near_miss_allowed_ratio_ =
        static_cast<double>(entry_gate_observed_near_miss_allowed_) /
        static_cast<double>(entry_gate_observed_samples_);
  } else {
    entry_gate_observed_filtered_ratio_ = 0.0;
    entry_gate_observed_near_miss_ratio_ = 0.0;
    entry_gate_observed_near_miss_allowed_ratio_ = 0.0;
  }
}

void BotApplication::EvaluateExecutionQualityGuard(
    std::uint64_t window_fills,
    double window_realized_net_per_fill_usd,
    double window_fee_bps_per_fill) {
  if (!config_.execution_quality_guard_enabled) {
    execution_quality_guard_active_ = false;
    execution_quality_required_edge_penalty_bps_ = 0.0;
    execution_quality_bad_streak_ = 0;
    execution_quality_good_streak_ = 0;
    return;
  }

  const std::uint64_t min_fills = static_cast<std::uint64_t>(std::max(
      0, config_.execution_quality_guard_min_fills));
  if (window_fills < min_fills || window_fills == 0) {
    return;
  }

  const bool bad_quality =
      window_realized_net_per_fill_usd <
          config_.execution_quality_guard_min_realized_net_per_fill_usd ||
      window_fee_bps_per_fill >
          config_.execution_quality_guard_max_fee_bps_per_fill;
  if (bad_quality) {
    ++execution_quality_bad_streak_;
    execution_quality_good_streak_ = 0;
    const int trigger_streak =
        std::max(0, config_.execution_quality_guard_bad_streak_to_trigger);
    if (!execution_quality_guard_active_ &&
        (trigger_streak == 0 || execution_quality_bad_streak_ >= trigger_streak)) {
      execution_quality_guard_active_ = true;
      execution_quality_required_edge_penalty_bps_ = std::max(
          0.0, config_.execution_quality_guard_required_edge_penalty_bps);
      LogInfo("EXECUTION_QUALITY_GUARD_ENTER: bad_streak=" +
              std::to_string(execution_quality_bad_streak_) +
              ", min_realized_net_per_fill_usd=" +
              std::to_string(
                  config_.execution_quality_guard_min_realized_net_per_fill_usd) +
              ", max_fee_bps_per_fill=" +
              std::to_string(config_.execution_quality_guard_max_fee_bps_per_fill) +
              ", applied_penalty_bps=" +
              std::to_string(execution_quality_required_edge_penalty_bps_));
    }
    return;
  }

  execution_quality_bad_streak_ = 0;
  if (!execution_quality_guard_active_) {
    execution_quality_good_streak_ = 0;
    execution_quality_required_edge_penalty_bps_ = 0.0;
    return;
  }
  ++execution_quality_good_streak_;
  const int release_streak =
      std::max(0, config_.execution_quality_guard_good_streak_to_release);
  if (release_streak == 0 || execution_quality_good_streak_ >= release_streak) {
    execution_quality_guard_active_ = false;
    execution_quality_required_edge_penalty_bps_ = 0.0;
    execution_quality_good_streak_ = 0;
    LogInfo("EXECUTION_QUALITY_GUARD_EXIT: release_streak=" +
            std::to_string(release_streak));
  }
}

void BotApplication::UpdateReconcileAnomalyProtection(bool anomaly_detected,
                                                      const std::string& reason_code) {
  if (anomaly_detected) {
    ++reconcile_anomaly_streak_;
    reconcile_healthy_streak_ = 0;
    const int reduce_only_threshold =
        std::max(0, config_.reconcile.anomaly_reduce_only_streak);
    const int halt_threshold = std::max(0, config_.reconcile.anomaly_halt_streak);
    if (!reconcile_forced_reduce_only_ && reduce_only_threshold > 0 &&
        reconcile_anomaly_streak_ >= reduce_only_threshold) {
      reconcile_forced_reduce_only_ = true;
      RefreshReduceOnlyMode();
      LogError("OMS_RECONCILE_ANOMALY_PROTECTION_ENTER: streak=" +
               std::to_string(reconcile_anomaly_streak_) +
               ", reason=" + reason_code);
    }
    if (!reconcile_halted_ && halt_threshold > 0 &&
        reconcile_anomaly_streak_ >= halt_threshold) {
      reconcile_halted_ = true;
      RefreshTradingHaltState();
      LogError("OMS_RECONCILE_ANOMALY_HALT_ENTER: streak=" +
               std::to_string(reconcile_anomaly_streak_) +
               ", reason=" + reason_code);
    }
    LogInfo("OMS_RECONCILE_ANOMALY_STREAK: streak=" +
            std::to_string(reconcile_anomaly_streak_) +
            ", reason=" + reason_code +
            ", reduce_only=" +
            std::string(reconcile_forced_reduce_only_ ? "true" : "false") +
            ", halted=" + std::string(reconcile_halted_ ? "true" : "false"));
    return;
  }

  reconcile_anomaly_streak_ = 0;
  ++reconcile_healthy_streak_;
  const int resume_threshold = std::max(0, config_.reconcile.anomaly_resume_streak);
  if (reconcile_forced_reduce_only_ && resume_threshold > 0 &&
      reconcile_healthy_streak_ >= resume_threshold) {
    reconcile_forced_reduce_only_ = false;
    RefreshReduceOnlyMode();
    LogInfo("OMS_RECONCILE_ANOMALY_PROTECTION_EXIT: healthy_streak=" +
            std::to_string(reconcile_healthy_streak_));
  }
}

void BotApplication::AccumulateStats(DecisionFunnelStats* total,
                                     const DecisionFunnelStats& delta) {
  if (total == nullptr) {
    return;
  }
  total->raw_signals += delta.raw_signals;
  total->risk_adjusted_signals += delta.risk_adjusted_signals;
  total->intents_generated += delta.intents_generated;
  total->intents_filtered_inactive_symbol +=
      delta.intents_filtered_inactive_symbol;
  total->intents_filtered_min_notional += delta.intents_filtered_min_notional;
  total->intents_filtered_fee_aware += delta.intents_filtered_fee_aware;
  total->intents_filtered_fee_aware_near_miss +=
      delta.intents_filtered_fee_aware_near_miss;
  total->intents_passed_fee_aware_near_miss +=
      delta.intents_passed_fee_aware_near_miss;
  total->intents_throttled_cost_cooldown +=
      delta.intents_throttled_cost_cooldown;
  total->intents_throttled += delta.intents_throttled;
  total->intents_enqueued += delta.intents_enqueued;
  total->async_submit_ok += delta.async_submit_ok;
  total->async_submit_failed += delta.async_submit_failed;
  total->fills_applied += delta.fills_applied;
  total->gate_alerts += delta.gate_alerts;
  total->self_evolution_updates += delta.self_evolution_updates;
  total->self_evolution_rollbacks += delta.self_evolution_rollbacks;
  total->self_evolution_skipped += delta.self_evolution_skipped;
  total->regime_trend_ticks += delta.regime_trend_ticks;
  total->regime_range_ticks += delta.regime_range_ticks;
  total->regime_extreme_ticks += delta.regime_extreme_ticks;
  total->regime_warmup_ticks += delta.regime_warmup_ticks;
  total->integrator_scored += delta.integrator_scored;
  total->integrator_pred_up += delta.integrator_pred_up;
  total->integrator_pred_down += delta.integrator_pred_down;
  total->integrator_policy_applied += delta.integrator_policy_applied;
  total->integrator_policy_canary += delta.integrator_policy_canary;
  total->integrator_policy_active += delta.integrator_policy_active;
  total->entry_edge_samples += delta.entry_edge_samples;
  total->strategy_mix_samples += delta.strategy_mix_samples;
  total->integrator_model_score_sum += delta.integrator_model_score_sum;
  total->integrator_p_up_sum += delta.integrator_p_up_sum;
  total->integrator_p_down_sum += delta.integrator_p_down_sum;
  total->entry_edge_bps_sum += delta.entry_edge_bps_sum;
  total->entry_base_required_edge_bps_sum +=
      delta.entry_base_required_edge_bps_sum;
  total->entry_required_edge_bps_sum += delta.entry_required_edge_bps_sum;
  total->entry_adaptive_relax_bps_sum += delta.entry_adaptive_relax_bps_sum;
  total->entry_maker_relax_bps_sum += delta.entry_maker_relax_bps_sum;
  total->entry_regime_adjust_bps_sum += delta.entry_regime_adjust_bps_sum;
  total->entry_volatility_adjust_bps_sum +=
      delta.entry_volatility_adjust_bps_sum;
  total->entry_liquidity_adjust_bps_sum +=
      delta.entry_liquidity_adjust_bps_sum;
  total->entry_quality_guard_penalty_bps_sum +=
      delta.entry_quality_guard_penalty_bps_sum;
  total->entry_edge_gap_bps_sum += delta.entry_edge_gap_bps_sum;
  total->trend_notional_abs_sum += delta.trend_notional_abs_sum;
  total->defensive_notional_abs_sum += delta.defensive_notional_abs_sum;
  total->blended_notional_abs_sum += delta.blended_notional_abs_sum;
  total->fills_notional_abs_usd_sum += delta.fills_notional_abs_usd_sum;
  total->fills_maker_count += delta.fills_maker_count;
  total->fills_taker_count += delta.fills_taker_count;
  total->fills_unknown_liquidity_count += delta.fills_unknown_liquidity_count;
  total->fills_explicit_liquidity_count += delta.fills_explicit_liquidity_count;
  total->fills_fee_sign_fallback_count += delta.fills_fee_sign_fallback_count;
  total->fills_maker_fee_usd_sum += delta.fills_maker_fee_usd_sum;
  total->fills_taker_fee_usd_sum += delta.fills_taker_fee_usd_sum;
  total->fills_maker_notional_abs_usd_sum += delta.fills_maker_notional_abs_usd_sum;
  total->fills_taker_notional_abs_usd_sum += delta.fills_taker_notional_abs_usd_sum;
}

bool BotApplication::IsForceReduceOnlyActive() const {
  return protection_forced_reduce_only_ || gate_forced_reduce_only_ ||
         reconcile_forced_reduce_only_;
}

void BotApplication::RefreshReduceOnlyMode() {
  system_.ForceReduceOnly(IsForceReduceOnlyActive());
}

void BotApplication::RefreshTradingHaltState() {
  trading_halted_ = reconcile_halted_ || gate_halted_;
}

void BotApplication::TickGateRuntimeCooldown() {
  if (gate_reduce_only_cooldown_ticks_left_ > 0) {
    --gate_reduce_only_cooldown_ticks_left_;
  }
  if (gate_halt_cooldown_ticks_left_ > 0) {
    --gate_halt_cooldown_ticks_left_;
  }
}

/**
 * @brief 应用主入口
 *
 * 执行顺序：Initialize -> RunLoop -> Shutdown。
 */
int BotApplication::Run() {
  if (!Initialize()) {
    return 1;
  }
  RunLoop();
  Shutdown();
  return 0;
}

/**
 * @brief 系统初始化
 *
 * 关键顺序（不可随意调整）：
 * 1. 初始化并恢复 WAL（先恢复状态再接入交易所）；
 * 2. 建立交易所连接并做账户门禁；
 * 3. 启动异步执行线程；
 * 4. 初始化 Universe 并同步远端持仓。
 */
bool BotApplication::Initialize() {
  std::string wal_error;
  if (!wal_.Initialize(&wal_error)) {
    LogError("WAL 初始化失败: " + wal_error);
    return false;
  }

  if (config_.mode == "replay") {
    LogInfo("replay 模式：跳过历史 WAL 恢复");
  } else {
    std::vector<FillEvent> historical_fills;
    if (!wal_.LoadState(&intent_ids_, &fill_ids_, &historical_fills, &wal_error)) {
      LogError("WAL 加载失败: " + wal_error);
      return false;
    }
    // 仅回放成交恢复仓位和权益，Intent 去重由 intent_ids_ 负责。
    for (const auto& fill : historical_fills) {
      oms_.OnFill(fill);
      system_.OnFill(fill);
    }
    LogInfo("WAL 恢复完成: intents=" + std::to_string(intent_ids_.size()) +
            ", fills=" + std::to_string(fill_ids_.size()));
  }

  adapter_ = CreateAdapter(config_);
  if (!adapter_->Connect()) {
    LogError("交易所连接失败");
    return false;
  }
  LogInfo("适配器已连接: " + adapter_->Name());

  if (!ValidateAccountSnapshot(config_, adapter_.get())) {
    LogError("账户模式校验失败");
    return false;
  }

  // 执行通道单线程串行化，避免并发提交导致状态竞态。
  executor_ = std::make_unique<AsyncExecutor>(adapter_.get());
  executor_->Start();

  InitializeUniverse();
  SyncRemotePositions();

  if (config_.integrator.enabled &&
      system_.integrator_mode() != IntegratorMode::kOff &&
      config_.integrator.shadow.enabled) {
    std::string shadow_error;
    if (system_.InitializeIntegratorShadow(&shadow_error)) {
      LogInfo("INTEGRATOR_INIT: mode=" +
              std::string(ToString(system_.integrator_mode())) +
              ", model_version=" +
              system_.integrator_shadow_model_version());
    } else {
      LogInfo("INTEGRATOR_DEGRADED: " + shadow_error);
      // 兜底策略：canary/active 在模型不可用时立即退回 off，避免“盲下单”。
      if (system_.integrator_mode() == IntegratorMode::kCanary ||
          system_.integrator_mode() == IntegratorMode::kActive) {
        system_.SetIntegratorMode(IntegratorMode::kOff);
        LogInfo("INTEGRATOR_FAILSAFE: mode switched to off");
      }
    }
  }

  // 自进化初始化必须在账户同步后进行，确保首个评估窗口的权益/已实现净盈亏基线准确。
  if (config_.self_evolution.enabled) {
    system_.EnableEvolution(true);
    std::string error;
    if (!system_.SetEvolutionWeights(config_.self_evolution.initial_trend_weight,
                                     config_.self_evolution.initial_defensive_weight,
                                     &error)) {
      LogError("自进化初始权重设置失败: " + error);
      return false;
    }
    if (!self_evolution_.Initialize(
            /*current_tick=*/0,
            system_.account().equity_usd(),
            {config_.self_evolution.initial_trend_weight,
             config_.self_evolution.initial_defensive_weight},
            &error,
            system_.account().cumulative_realized_net_pnl_usd())) {
      LogError("自进化控制器初始化失败: " + error);
      return false;
    }
    LogInfo("SELF_EVOLUTION_INIT: trend_weight=" +
            std::to_string(config_.self_evolution.initial_trend_weight) +
            ", defensive_weight=" +
            std::to_string(config_.self_evolution.initial_defensive_weight) +
            ", update_interval_ticks=" +
            std::to_string(config_.self_evolution.update_interval_ticks) +
            ", factor_ic_weighting=" +
            std::string(config_.self_evolution.enable_factor_ic_adaptive_weights
                            ? "true"
                            : "false") +
            ", learnability_gate=" +
            std::string(config_.self_evolution.enable_learnability_gate ? "true"
                                                                        : "false"));
  } else {
    system_.EnableEvolution(false);
  }

  return true;
}

/**
 * @brief 初始化 Universe 候选池
 *
 * 先按交易所 symbol 规则做可交易性过滤：
 * - 必须可交易；
 * - 必须有有效数量步长（qty_step > 0）。
 */
void BotApplication::InitializeUniverse() {
  std::vector<std::string> candidates = config_.universe.candidate_symbols;
  candidates.insert(candidates.end(), config_.universe.fallback_symbols.begin(),
                    config_.universe.fallback_symbols.end());
  candidates.push_back(config_.primary_symbol);
  candidates = UniqueSymbols(candidates);

  std::vector<std::string> allowed;
  for (const auto& symbol : candidates) {
    SymbolInfo info;
    // 过滤掉不可交易或规则异常的币对
    if (!adapter_->GetSymbolInfo(symbol, &info)) continue;
    if (!info.tradable || info.qty_step <= 0.0) continue;
    allowed.push_back(info.symbol);
  }

  if (!allowed.empty()) {
    universe_selector_.SetAllowedSymbols(allowed);
    tracked_symbols_ = allowed;
  } else {
    tracked_symbols_ = candidates;
    LogInfo("警告: 未获取到有效交易规则，使用原始候选列表");
  }
}

/**
 * @brief 启动时同步远端持仓
 *
 * 设计取舍：
 * - 非 replay 模式优先以交易所快照重建本地视图；
 * - 失败时继续运行，但明确记录“状态可能不准确”。
 */
void BotApplication::SyncRemotePositions() {
  if (config_.mode == "replay") return;

  std::vector<RemotePositionSnapshot> remote_positions;
  bool position_sync_ok = false;
  if (adapter_->GetRemotePositions(&remote_positions)) {
    system_.SyncAccountFromRemotePositions(remote_positions);
    // 启动基线：将远端持仓映射到 OMS 净仓位，避免远端快照暂不可用时弱对账误判。
    oms_.SeedNetPositionBaseline(remote_positions);
    position_sync_ok = true;
    LogInfo("远端持仓同步完成: count=" + std::to_string(remote_positions.size()));
  } else {
    LogInfo("警告: 无法同步远端持仓，账户状态可能不准确");
  }

  RemoteAccountBalanceSnapshot balance;
  if (adapter_->GetRemoteAccountBalance(&balance)) {
    // 启动阶段重置回撤峰值基线到远端权益，避免固定初始值引入伪回撤。
    system_.SyncAccountFromRemoteBalance(balance, /*reset_peak_to_equity=*/true);
    if (balance.has_equity) {
      LogInfo("远端资金同步完成: equity=" + std::to_string(balance.equity_usd));
    } else if (balance.has_wallet_balance) {
      LogInfo("远端资金同步完成: wallet=" +
              std::to_string(balance.wallet_balance_usd));
    }
  } else if (position_sync_ok) {
    LogInfo("警告: 远端持仓已同步，但远端资金读取失败，回撤口径可能存在偏差");
  }
}

/**
 * @brief 主循环
 *
 * 每轮处理顺序：
 * 1. 行情事件（驱动策略与下单决策）；
 * 2. 异步执行结果（ACK/Reject）；
 * 3. 成交回报（推进 OMS/账户）；
 * 4. 周期任务（远端风险刷新、对账、Gate、状态日志）。
 */
void BotApplication::RunLoop() {
  MarketEvent event;
  while (true) {
    const bool has_market = adapter_->PollMarket(&event);
    bool advanced_tick = false;
    bool has_fill = false;

    if (has_market) {
      advanced_tick = true;
      has_tick_strategy_signal_ = false;
      tick_cost_filtered_signal_ = false;
      tick_trend_notional_usd_ = 0.0;
      tick_defensive_notional_usd_ = 0.0;
      tick_strategy_signal_symbol_.clear();
      ProcessMarketEvent(event);
    }

    ProcessAsyncResults();

    FillEvent fill;
    while (adapter_->PollFill(&fill)) {
      has_fill = true;
      ProcessFillEvent(fill);
    }
    CheckPendingRequiredSlTimeouts();

    if (advanced_tick) {
      ++market_tick_count_;
      RunRemoteRiskRefresh();
      RunReconcile();
      RunGateMonitor();
      RunSelfEvolution();
      LogStatus();
    }

    if (ShouldExit(has_market, has_fill)) {
      break;
    }

    if (!has_market && !has_fill) {
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
  }
}

/**
 * @brief 周期刷新远端风险字段（liqPrice/mark）
 *
 * 只刷新风险评估相关字段，不重置现金/峰值权益，不清空本地仓位表。
 * 该机制用于降低“启动后持仓变化导致强平距离陈旧”的风险。
 */
void BotApplication::RunRemoteRiskRefresh() {
  if (config_.mode == "replay") return;
  if (adapter_ == nullptr) return;
  if (config_.system_remote_risk_refresh_interval_ticks <= 0) return;
  if (market_tick_count_ % config_.system_remote_risk_refresh_interval_ticks != 0) {
    return;
  }

  std::vector<RemotePositionSnapshot> remote_positions;
  if (!adapter_->GetRemotePositions(&remote_positions)) {
    LogInfo("REMOTE_RISK_REFRESH_DEGRADED: 获取远端持仓失败，保留本地风险视图");
    return;
  }

  system_.RefreshAccountRiskFromRemotePositions(remote_positions);
  RemoteAccountBalanceSnapshot balance;
  if (adapter_->GetRemoteAccountBalance(&balance)) {
    // 运行中只上调峰值，不重置峰值，避免人为清零回撤统计。
    system_.SyncAccountFromRemoteBalance(balance, /*reset_peak_to_equity=*/false);
  }
  LogInfo("REMOTE_RISK_REFRESH: count=" + std::to_string(remote_positions.size()));
}

/**
 * @brief 行情事件处理
 *
 * 业务顺序：
 * 1. 更新 Universe；
 * 2. 交易暂停时仅更新市值，不做新决策；
 * 3. 执行 Strategy->Risk->Execution；
 * 4. 应用 Universe 约束与下单节流；
 * 5. 满足条件则入队异步执行。
 */
void BotApplication::ProcessMarketEvent(const MarketEvent& event) {
  if (const auto update = universe_selector_.OnMarket(event); update.has_value()) {
    LogInfo("Universe Updated: active_count=" + std::to_string(update->active_symbols.size()));
  }

  const double mark_price_for_evolution =
      event.mark_price > 0.0 ? event.mark_price : event.price;
  if (std::isfinite(mark_price_for_evolution) && mark_price_for_evolution > 0.0) {
    latest_mark_price_usd_ = mark_price_for_evolution;
    has_latest_mark_price_ = true;
  } else {
    has_latest_mark_price_ = false;
  }

  // 对账硬停机时直接停止策略决策；Gate 停机仅阻断下单，不阻断观测统计。
  if (reconcile_halted_) {
    system_.OnMarketSnapshot(event);
    return;
  }

  // inactive symbol 且无持仓/无在途净仓位订单时，直接跳过整条决策链，
  // 降低无效信号评估与日志噪音。
  const bool symbol_active = universe_selector_.IsActive(event.symbol);
  const double symbol_notional = system_.account().current_notional_usd(event.symbol);
  const bool has_pending_symbol_net_orders =
      oms_.HasPendingNetPositionOrderForSymbol(event.symbol);
  if (ShouldSkipInactiveSymbolDecision(symbol_active,
                                       symbol_notional,
                                       has_pending_symbol_net_orders)) {
    return;
  }

  const bool trade_ok = adapter_->TradeOk() && !IsForceReduceOnlyActive();
  auto decision = system_.Evaluate(event, trade_ok);
  if (decision.regime.warmup) {
    ++funnel_window_.regime_warmup_ticks;
  }
  switch (decision.regime.bucket) {
    case RegimeBucket::kTrend:
      ++funnel_window_.regime_trend_ticks;
      break;
    case RegimeBucket::kRange:
      ++funnel_window_.regime_range_ticks;
      break;
    case RegimeBucket::kExtreme:
      ++funnel_window_.regime_extreme_ticks;
      break;
  }
  const bool regime_changed =
      !has_last_regime_state_ ||
      last_regime_state_.symbol != decision.regime.symbol ||
      last_regime_state_.regime != decision.regime.regime ||
      last_regime_state_.bucket != decision.regime.bucket ||
      last_regime_state_.warmup != decision.regime.warmup;
  if (regime_changed) {
    LogInfo("REGIME_CHANGE: symbol=" + decision.regime.symbol +
            ", regime=" + std::string(ToString(decision.regime.regime)) +
            ", bucket=" + std::string(ToString(decision.regime.bucket)) +
            ", warmup=" + (decision.regime.warmup ? "true" : "false") +
            ", instant_return=" +
            std::to_string(decision.regime.instant_return) +
            ", trend_strength=" +
            std::to_string(decision.regime.trend_strength) +
            ", volatility=" +
            std::to_string(decision.regime.volatility_level));
  }
  last_regime_state_ = decision.regime;
  has_last_regime_state_ = true;
  last_strategy_signal_ = decision.base_signal;
  has_last_strategy_signal_ = true;
  const auto executable_components =
      ScaleStrategyComponentsForExecution(decision);
  tick_trend_notional_usd_ = executable_components.first;
  tick_defensive_notional_usd_ = executable_components.second;
  tick_strategy_signal_symbol_ =
      decision.signal.symbol.empty() ? event.symbol : decision.signal.symbol;
  has_tick_strategy_signal_ = !tick_strategy_signal_symbol_.empty();
  if (HasExposure(decision.base_signal.trend_notional_usd) ||
      HasExposure(decision.base_signal.defensive_notional_usd) ||
      HasExposure(decision.base_signal.suggested_notional_usd)) {
    ++funnel_window_.strategy_mix_samples;
    funnel_window_.trend_notional_abs_sum +=
        std::fabs(decision.base_signal.trend_notional_usd);
    funnel_window_.defensive_notional_abs_sum +=
        std::fabs(decision.base_signal.defensive_notional_usd);
    funnel_window_.blended_notional_abs_sum +=
        std::fabs(decision.base_signal.suggested_notional_usd);
  }
  if (decision.shadow.enabled) {
    ++funnel_window_.integrator_scored;
    funnel_window_.integrator_model_score_sum += decision.shadow.model_score;
    funnel_window_.integrator_p_up_sum += decision.shadow.p_up;
    funnel_window_.integrator_p_down_sum += decision.shadow.p_down;
    if (decision.shadow.p_up >= 0.55) {
      ++funnel_window_.integrator_pred_up;
    } else if (decision.shadow.p_down >= 0.55) {
      ++funnel_window_.integrator_pred_down;
    }
    last_shadow_inference_ = decision.shadow;
    has_last_shadow_inference_ = true;
  }
  if (decision.integrator_policy_applied) {
    ++funnel_window_.integrator_policy_applied;
    if (system_.integrator_mode() == IntegratorMode::kCanary) {
      ++funnel_window_.integrator_policy_canary;
    } else if (system_.integrator_mode() == IntegratorMode::kActive) {
      ++funnel_window_.integrator_policy_active;
    }
    LogInfo("INTEGRATOR_POLICY_APPLIED: mode=" +
            std::string(ToString(system_.integrator_mode())) +
            ", reason=" + decision.integrator_policy_reason +
            ", symbol=" + decision.signal.symbol +
            ", confidence=" + std::to_string(decision.integrator_confidence) +
            ", base_notional=" +
            std::to_string(decision.base_signal.suggested_notional_usd) +
            ", base_trend_component=" +
            std::to_string(decision.base_signal.trend_notional_usd) +
            ", base_defensive_component=" +
            std::to_string(decision.base_signal.defensive_notional_usd) +
            ", final_notional=" +
            std::to_string(decision.signal.suggested_notional_usd));
  }
  if (HasExposure(decision.base_signal.suggested_notional_usd)) {
    ++funnel_window_.raw_signals;
  }
  if (HasExposure(decision.risk_adjusted.adjusted_notional_usd)) {
    ++funnel_window_.risk_adjusted_signals;
  }
  if (decision.intent.has_value()) {
    ++funnel_window_.intents_generated;
  }

  // 不在活跃池时仅禁止“开仓意图”；减仓/保护单必须放行，避免风险无法收敛。
  if (decision.intent.has_value() &&
      !universe_selector_.IsActive(decision.intent->symbol) &&
      ShouldFilterInactiveSymbolIntent(*decision.intent)) {
    ++funnel_window_.intents_filtered_inactive_symbol;
    decision.intent.reset();
  }

  // 交易所最小名义敞口前置保护：避免“买不起/卖不动”导致的拒单循环。
  if (decision.intent.has_value()) {
    std::string guard_reason;
    if (ViolatesMinNotionalGuard(adapter_.get(), *decision.intent, event, &guard_reason)) {
      LogInfo("EXEC_FILTER_IGNORE: symbol=" + decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", reason=" + guard_reason);
      ++funnel_window_.intents_filtered_min_notional;
      decision.intent.reset();
    }
  }

  if (decision.intent.has_value() && IsOpeningIntent(*decision.intent)) {
    int cooldown_ticks_remaining = 0;
    if (IsCostFilterCooldownActive(decision.intent->symbol,
                                   &cooldown_ticks_remaining)) {
      ++funnel_window_.intents_throttled;
      ++funnel_window_.intents_throttled_cost_cooldown;
      LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", reason=cost_filter_cooldown_ticks_remaining=" +
              std::to_string(cooldown_ticks_remaining));
      decision.intent.reset();
    }
  }

  if (decision.intent.has_value() && IsOpeningIntent(*decision.intent)) {
    double expected_edge_bps = 0.0;
    double required_edge_bps = 0.0;
    double base_required_edge_bps = 0.0;
    double adaptive_relax_bps = 0.0;
    double maker_relax_bps = 0.0;
    double regime_adjust_bps = 0.0;
    double volatility_adjust_bps = 0.0;
    double liquidity_adjust_bps = 0.0;
    double quality_guard_penalty_bps = 0.0;
    double observed_filtered_ratio = 0.0;
    double entry_edge_gap_bps = 0.0;
    bool near_miss = false;
    bool near_miss_allowed = false;
    const bool filtered = ShouldFilterByFeeAwareGate(
        decision,
        event,
        &expected_edge_bps,
        &required_edge_bps,
        &base_required_edge_bps,
        &adaptive_relax_bps,
        &maker_relax_bps,
        &regime_adjust_bps,
        &volatility_adjust_bps,
        &liquidity_adjust_bps,
        &quality_guard_penalty_bps,
        &observed_filtered_ratio,
        &entry_edge_gap_bps,
        &near_miss,
        &near_miss_allowed);
    UpdateEntryGateObservedRatio(filtered, near_miss, near_miss_allowed);
    ++funnel_window_.entry_edge_samples;
    funnel_window_.entry_edge_bps_sum += expected_edge_bps;
    funnel_window_.entry_base_required_edge_bps_sum += base_required_edge_bps;
    funnel_window_.entry_required_edge_bps_sum += required_edge_bps;
    funnel_window_.entry_adaptive_relax_bps_sum += adaptive_relax_bps;
    funnel_window_.entry_maker_relax_bps_sum += maker_relax_bps;
    funnel_window_.entry_regime_adjust_bps_sum += regime_adjust_bps;
    funnel_window_.entry_volatility_adjust_bps_sum += volatility_adjust_bps;
    funnel_window_.entry_liquidity_adjust_bps_sum += liquidity_adjust_bps;
    funnel_window_.entry_quality_guard_penalty_bps_sum +=
        quality_guard_penalty_bps;
    funnel_window_.entry_edge_gap_bps_sum += entry_edge_gap_bps;
    if (filtered) {
      ++funnel_window_.intents_filtered_fee_aware;
      if (near_miss) {
        ++funnel_window_.intents_filtered_fee_aware_near_miss;
      }
      tick_cost_filtered_signal_ = true;
      OnCostFilterRejected(decision.intent->symbol);
      LogInfo("ORDER_FILTERED_COST: symbol=" + decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", expected_edge_bps=" + std::to_string(expected_edge_bps) +
              ", base_required_edge_bps=" +
              std::to_string(base_required_edge_bps) +
              ", adaptive_relax_bps=" + std::to_string(adaptive_relax_bps) +
              ", maker_relax_bps=" + std::to_string(maker_relax_bps) +
              ", regime_adjust_bps=" + std::to_string(regime_adjust_bps) +
              ", volatility_adjust_bps=" +
              std::to_string(volatility_adjust_bps) +
              ", liquidity_adjust_bps=" +
              std::to_string(liquidity_adjust_bps) +
              ", quality_guard_penalty_bps=" +
              std::to_string(quality_guard_penalty_bps) +
              ", required_edge_bps=" + std::to_string(required_edge_bps) +
              ", edge_gap_bps=" + std::to_string(entry_edge_gap_bps) +
              ", near_miss=" + std::string(near_miss ? "true" : "false") +
              ", near_miss_tolerance_bps=" +
              std::to_string(config_.execution_entry_gate_near_miss_tolerance_bps) +
              ", round_trip_cost_bps=" + std::to_string(RoundTripCostBps()) +
              ", min_expected_edge_bps=" +
              std::to_string(config_.execution_min_expected_edge_bps) +
              ", required_edge_cap_bps=" +
              std::to_string(config_.execution_required_edge_cap_bps) +
              ", observed_filtered_ratio=" +
              std::to_string(observed_filtered_ratio));
      decision.intent.reset();
    } else {
      if (near_miss_allowed) {
        ++funnel_window_.intents_passed_fee_aware_near_miss;
        LogInfo("ORDER_NEAR_MISS_MAKER_ALLOWED: symbol=" + decision.intent->symbol +
                ", client_order_id=" + decision.intent->client_order_id +
                ", expected_edge_bps=" + std::to_string(expected_edge_bps) +
                ", required_edge_bps=" + std::to_string(required_edge_bps) +
                ", edge_gap_bps=" + std::to_string(entry_edge_gap_bps) +
                ", near_miss_tolerance_bps=" +
                std::to_string(config_.execution_entry_gate_near_miss_tolerance_bps) +
                ", maker_allow_extra_gap_bps=" +
                std::to_string(config_.execution_entry_gate_near_miss_maker_max_gap_bps) +
                ", maker_allow_upper_gap_bps=" +
                std::to_string(config_.execution_entry_gate_near_miss_tolerance_bps +
                               config_.execution_entry_gate_near_miss_maker_max_gap_bps) +
                ", maker_allow_config_key=entry_gate_near_miss_maker_max_gap_bps" +
                ", maker_allow_config_value_bps=" +
                std::to_string(config_.execution_entry_gate_near_miss_maker_max_gap_bps));
      }
      OnCostFilterAccepted(decision.intent->symbol);
    }
  }

  if (config_.integrator.enabled && config_.integrator.shadow.log_model_score &&
      decision.shadow.enabled && decision.intent.has_value()) {
    LogInfo("INTEGRATOR_SHADOW_SCORE: symbol=" + decision.intent->symbol +
            ", model_version=" + decision.shadow.model_version +
            ", model_score=" + std::to_string(decision.shadow.model_score) +
            ", p_up=" + std::to_string(decision.shadow.p_up) +
            ", p_down=" + std::to_string(decision.shadow.p_down));
  }

  if (const auto gate_alert =
          gate_monitor_.OnDecision(decision.base_signal, decision.risk_adjusted,
                                   decision.intent);
      gate_alert.has_value()) {
    ++funnel_window_.gate_alerts;
    LogInfo("GATE_ALERT: code=" + *gate_alert +
            ", tick=" + std::to_string(market_tick_count_));
  }

  if (gate_halted_ && decision.intent.has_value()) {
    ++funnel_window_.intents_throttled;
    LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
            ", client_order_id=" + decision.intent->client_order_id +
            ", reason=gate_halted");
    decision.intent.reset();
  }

  if (decision.intent.has_value()) {
    // 同币种同方向已有在途净仓位订单时，不再重复入队，避免 pending 堆积和超时撤单抖动。
    if (IsNetPositionOrderPurpose(decision.intent->purpose) &&
        oms_.HasPendingNetPositionOrderForSymbolDirection(
            decision.intent->symbol, decision.intent->direction)) {
      ++funnel_window_.intents_throttled;
      LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", reason=pending_same_side_inflight");
      return;
    }

    std::string reason;
    const auto now = CurrentTimestampMs();
    if (order_throttle_.Allow(*decision.intent, now, market_tick_count_, &reason)) {
      if (EnqueueIntent(*decision.intent)) {
        order_throttle_.OnAccepted(*decision.intent, now, market_tick_count_);
      }
    } else {
      ++funnel_window_.intents_throttled;
      if (!reason.empty()) {
        LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
                ", client_order_id=" + decision.intent->client_order_id +
                ", reason=" + reason);
      }
    }
  }
}

/**
 * @brief 下单入队（WAL-first）
 *
 * 原子语义：
 * - 必须先 RegisterIntent + AppendIntent(WAL) 成功；
 * - 成功后才投递 AsyncExecutor；
 * - WAL 失败时立即标记 Rejected，防止“已发单但不可恢复”。
 */
bool BotApplication::EnqueueIntent(const OrderIntent& intent) {
  if (intent_ids_.count(intent.client_order_id)) return false;
  if (!oms_.RegisterIntent(intent)) return false;

  std::string wal_err;
  if (!wal_.AppendIntent(intent, &wal_err)) {
    LogError("WAL Write Error: " + wal_err);
    oms_.MarkRejected(intent.client_order_id);
    return false;
  }
  intent_ids_.insert(intent.client_order_id);
  if (IsNetPositionOrderPurpose(intent.purpose)) {
    pending_net_order_enqueued_ms_[intent.client_order_id] = CurrentTimestampMs();
  }
  executor_->Submit(intent);
  ++funnel_window_.intents_enqueued;
  return true;
}

/**
 * @brief 处理异步执行结果
 *
 * 对关键保护单（SL）做升级处理：
 * - 若 require_sl=true 且 SL 提交失败，立即进入强制只减仓（reduce-only）。
 */
void BotApplication::ProcessAsyncResults() {
  std::vector<AsyncResult> results;
  executor_->PollResults(&results);
  for (const auto& res : results) {
    if (res.is_cancel) continue;

    if (res.success) {
      oms_.MarkSent(res.client_order_id);
      const auto* record = oms_.Find(res.client_order_id);
      if (record != nullptr && record->intent.purpose == OrderPurpose::kSl) {
        ClearPendingRequiredSl(res.client_order_id);
      }
      ++funnel_window_.async_submit_ok;
    } else {
      oms_.MarkRejected(res.client_order_id);
      pending_net_order_enqueued_ms_.erase(res.client_order_id);
      ++funnel_window_.async_submit_failed;
      LogError("Async Submit Failed: " + res.error);

      const auto* record = oms_.Find(res.client_order_id);
      if (record != nullptr && record->intent.purpose == OrderPurpose::kSl) {
        ClearPendingRequiredSl(res.client_order_id);
      }

      // 关键保护单失败触发只减仓，并输出标准审计事件码。
      if (record && record->intent.purpose == OrderPurpose::kSl &&
          config_.protection.enabled && config_.protection.require_sl) {
        protection_forced_reduce_only_ = true;
        RefreshReduceOnlyMode();
        LogError("EXEC_PROTECTIVE_ORDER_MISSING: reason=sl_submit_failed"
                 ", parent_order_id=" + record->intent.parent_order_id +
                 ", sl_client_order_id=" + res.client_order_id +
                 ", error=" + res.error +
                 ", forcing=reduce_only");
      } else if (record && record->intent.purpose == OrderPurpose::kTp &&
                 config_.protection.enabled && config_.protection.enable_tp) {
        LogError("EXEC_TP_ATTACH_FAILED: reason=tp_submit_failed"
                 ", parent_order_id=" + record->intent.parent_order_id +
                 ", tp_client_order_id=" + res.client_order_id +
                 ", error=" + res.error);
      }
    }
  }
}

/**
 * @brief 处理成交回报
 *
 * 先持久化再推进内存状态，保证崩溃恢复一致性：
 * - dedup(fill_id)；
 * - AppendFill(WAL)；
 * - OMS/Account/Gate 更新；
 * - 触发保护单逻辑。
 */
void BotApplication::ProcessFillEvent(const FillEvent& fill) {
  if (fill_ids_.count(fill.fill_id)) return;

  std::string wal_err;
  if (!wal_.AppendFill(fill, &wal_err)) {
    LogError("WAL Fill Error: " + wal_err);
    return;
  }
  fill_ids_.insert(fill.fill_id);
  oms_.OnFill(fill);
  system_.OnFill(fill);
  gate_monitor_.OnFill(fill);
  const auto* fill_order_record = oms_.Find(fill.client_order_id);
  if (fill_order_record != nullptr &&
      fill_order_record->intent.purpose == OrderPurpose::kSl) {
    ClearPendingRequiredSl(fill.client_order_id);
  }
  if (const auto* record = oms_.Find(fill.client_order_id);
      record != nullptr && OrderManager::IsTerminalState(record->state)) {
    pending_net_order_enqueued_ms_.erase(fill.client_order_id);
  }
  // 记录最近成交 tick，供对账阶段应用短暂宽限窗口。
  last_fill_tick_ = market_tick_count_;
  ++funnel_window_.fills_applied;
  ++pending_fills_for_evolution_;
  if (std::isfinite(fill.price) && fill.price > 0.0 && std::isfinite(fill.qty)) {
    const double fill_notional_abs_usd = std::fabs(fill.price * std::fabs(fill.qty));
    funnel_window_.fills_notional_abs_usd_sum += fill_notional_abs_usd;
    constexpr double kFeeSignEpsilon = 1e-12;
    const bool explicit_liquidity =
        fill.liquidity == FillLiquidity::kMaker ||
        fill.liquidity == FillLiquidity::kTaker;
    const bool fallback_by_fee =
        fill.liquidity == FillLiquidity::kUnknown &&
        (fill.fee < -kFeeSignEpsilon || fill.fee > kFeeSignEpsilon);
    const bool maker_fill =
        fill.liquidity == FillLiquidity::kMaker ||
        (fill.liquidity == FillLiquidity::kUnknown &&
         fill.fee < -kFeeSignEpsilon);
    const bool taker_fill =
        fill.liquidity == FillLiquidity::kTaker ||
        (fill.liquidity == FillLiquidity::kUnknown &&
         fill.fee > kFeeSignEpsilon);
    if (explicit_liquidity) {
      ++funnel_window_.fills_explicit_liquidity_count;
    } else if (fallback_by_fee) {
      ++funnel_window_.fills_fee_sign_fallback_count;
    }
    if (maker_fill) {
      ++funnel_window_.fills_maker_count;
      funnel_window_.fills_maker_fee_usd_sum += fill.fee;
      funnel_window_.fills_maker_notional_abs_usd_sum += fill_notional_abs_usd;
    } else if (taker_fill) {
      ++funnel_window_.fills_taker_count;
      funnel_window_.fills_taker_fee_usd_sum += fill.fee;
      funnel_window_.fills_taker_notional_abs_usd_sum += fill_notional_abs_usd;
    } else {
      ++funnel_window_.fills_unknown_liquidity_count;
    }
  }

  HandleProtectionOrders(fill);
}

/**
 * @brief 保护单编排（Entry -> SL/TP，SL/TP 成交 -> OCO 撤单）
 */
void BotApplication::HandleProtectionOrders(const FillEvent& fill) {
  const auto* record = oms_.Find(fill.client_order_id);
  if (!record) return;

  // 开仓成交后挂保护单；若强制要求 SL 但挂单失败，进入强制只减仓。
  if (record->intent.purpose == OrderPurpose::kEntry &&
      config_.protection.enabled &&
      !oms_.HasOpenProtection(fill.client_order_id)) {
    auto sl = execution_.BuildProtectionIntent(
        fill, OrderPurpose::kSl, config_.protection.stop_loss_ratio);
    bool sl_ok = false;
    if (sl) {
      sl_ok = EnqueueIntent(*sl);
      if (sl_ok && config_.protection.require_sl) {
        TrackPendingRequiredSl(sl->client_order_id, fill.client_order_id);
      }
    }

    if (!sl_ok && config_.protection.require_sl) {
      protection_forced_reduce_only_ = true;
      RefreshReduceOnlyMode();
      LogError("EXEC_PROTECTIVE_ORDER_MISSING: reason=sl_enqueue_failed"
               ", parent_order_id=" + fill.client_order_id +
               ", forcing=reduce_only");
    }

    if (config_.protection.enable_tp) {
      auto tp = execution_.BuildProtectionIntent(
          fill, OrderPurpose::kTp, config_.protection.take_profit_ratio);
      if (tp) {
        const bool tp_ok = EnqueueIntent(*tp);
        if (!tp_ok) {
          LogError("EXEC_TP_ATTACH_FAILED: reason=tp_enqueue_failed"
                   ", parent_order_id=" + fill.client_order_id);
        }
      } else {
        LogError("EXEC_TP_ATTACH_FAILED: reason=tp_intent_invalid"
                 ", parent_order_id=" + fill.client_order_id);
      }
    }
  }

  // 保护单任一侧成交后撤销另一侧，避免重复平仓（OCO）。
  if (record->intent.purpose == OrderPurpose::kSl ||
      record->intent.purpose == OrderPurpose::kTp) {
    auto sibling = oms_.FindOpenProtectiveSibling(record->intent.parent_order_id,
                                                  record->intent.purpose);
    if (sibling) {
      executor_->Cancel(*sibling);
      oms_.MarkCancelled(*sibling);
    }
  }
}

void BotApplication::TrackPendingRequiredSl(
    const std::string& sl_client_order_id,
    const std::string& parent_order_id) {
  if (sl_client_order_id.empty() || !config_.protection.require_sl) {
    return;
  }
  const std::int64_t timeout_ms =
      static_cast<std::int64_t>(config_.protection.attach_timeout_ms);
  pending_required_sl_attach_[sl_client_order_id] = PendingRequiredSlAttach{
      .parent_order_id = parent_order_id,
      .deadline_ms = CurrentTimestampMs() + timeout_ms,
  };
}

void BotApplication::ClearPendingRequiredSl(
    const std::string& sl_client_order_id) {
  if (sl_client_order_id.empty()) {
    return;
  }
  pending_required_sl_attach_.erase(sl_client_order_id);
}

void BotApplication::CheckPendingRequiredSlTimeouts() {
  if (!config_.protection.enabled || !config_.protection.require_sl ||
      config_.protection.attach_timeout_ms <= 0 ||
      pending_required_sl_attach_.empty()) {
    return;
  }

  const std::int64_t now_ms = CurrentTimestampMs();
  std::vector<std::string> to_remove;
  to_remove.reserve(pending_required_sl_attach_.size());
  for (const auto& [sl_client_order_id, pending] : pending_required_sl_attach_) {
    if (now_ms < pending.deadline_ms) {
      continue;
    }

    const auto* sl_record = oms_.Find(sl_client_order_id);
    const bool confirmed =
        sl_record != nullptr &&
        (sl_record->state == OrderState::kSent ||
         sl_record->state == OrderState::kPartial ||
         sl_record->state == OrderState::kFilled);
    if (confirmed) {
      to_remove.push_back(sl_client_order_id);
      continue;
    }

    protection_forced_reduce_only_ = true;
    RefreshReduceOnlyMode();
    LogError("EXEC_PROTECTIVE_ORDER_MISSING: reason=sl_attach_timeout"
             ", parent_order_id=" + pending.parent_order_id +
             ", sl_client_order_id=" + sl_client_order_id +
             ", sl_state=" +
             (sl_record != nullptr ? OrderStateToString(sl_record->state)
                                   : "missing") +
             ", timeout_ms=" +
             std::to_string(config_.protection.attach_timeout_ms) +
             ", forcing=reduce_only");
    to_remove.push_back(sl_client_order_id);
  }

  for (const auto& id : to_remove) {
    pending_required_sl_attach_.erase(id);
  }
}

/**
 * @brief 周期性对账
 *
 * 双阶段确认：
 * 1) 先用远端净名义敞口快照快速检查；
 * 2) 失败后主动刷新远端持仓再检查一次；
 * 连续超过阈值才熔断停止新下单，避免瞬时抖动误判。
 */
void BotApplication::RunReconcile() {
  if (!config_.reconcile.enabled || config_.reconcile.interval_ticks <= 0) return;
  if (reconcile_halted_) return;
  if (++reconcile_tick_ % config_.reconcile.interval_ticks != 0) return;

  // 净仓位变更订单仍在途时，远端与本地可能天然存在短时偏差。
  // 但若订单长期未收敛（例如 reduce-only 部分成交尾量未终态），需要主动收敛以解除永久阻塞。
  int stale_net_orders = 0;
  int remote_missing_net_orders = 0;
  int fresh_net_orders = 0;
  const std::int64_t now_ms = CurrentTimestampMs();
  const std::int64_t stale_ms =
      static_cast<std::int64_t>(config_.reconcile.pending_order_stale_ms);
  std::unordered_set<std::string> remote_open_order_ids;
  const bool remote_open_orders_ok =
      adapter_ != nullptr &&
      adapter_->GetRemoteOpenOrderClientIds(&remote_open_order_ids);
  for (const auto& client_order_id : oms_.PendingNetPositionOrderIds()) {
    const auto it = pending_net_order_enqueued_ms_.find(client_order_id);
    bool is_stale = false;
    bool missing_on_remote = false;
    if (remote_open_orders_ok &&
        remote_open_order_ids.find(client_order_id) ==
            remote_open_order_ids.end()) {
      // 远端活动订单列表中已不存在该订单，优先按陈旧单收敛。
      is_stale = true;
      missing_on_remote = true;
    }
    if (it == pending_net_order_enqueued_ms_.end()) {
      // WAL恢复或历史遗留订单：缺少本次进程入队时间，按陈旧单处理。
      is_stale = true;
    } else if (now_ms - it->second > stale_ms) {
      is_stale = true;
    }
    if (!is_stale) {
      ++fresh_net_orders;
      continue;
    }

    ++stale_net_orders;
    if (missing_on_remote) {
      ++remote_missing_net_orders;
    }
    if (executor_ != nullptr) {
      executor_->Cancel(client_order_id);
    }
    oms_.MarkCancelled(client_order_id);
    pending_net_order_enqueued_ms_.erase(client_order_id);
  }

  if (stale_net_orders > 0) {
    LogInfo("OMS_STALE_PENDING_CLOSED: count=" + std::to_string(stale_net_orders) +
            ", remote_missing=" + std::to_string(remote_missing_net_orders) +
            ", stale_ms=" + std::to_string(stale_ms));
  }

  if (fresh_net_orders > 0) {
    reconcile_streak_ = 0;
    LogInfo("OMS_RECONCILE_DEFERRED: pending_net_orders=" +
            std::to_string(fresh_net_orders));
    UpdateReconcileAnomalyProtection(false, "RECONCILE_DEFERRED");
    return;
  }

  std::optional<double> remote_notional;
  double val = 0.0;
  std::vector<RemotePositionSnapshot> remote_positions;
  bool remote_positions_fresh = false;
  if (adapter_->GetRemoteNotionalUsd(&val)) {
    remote_notional = val;
  } else if (adapter_->GetRemotePositions(&remote_positions)) {
    remote_notional = reconciler_.ComputeRemoteNotionalUsd(remote_positions);
    remote_positions_fresh = true;
  }

  // live/paper 模式下，远端快照不可用时跳过本轮对账，避免回退到弱口径导致误熔断。
  if (!remote_notional.has_value() && config_.mode != "replay") {
    reconcile_streak_ = 0;
    LogInfo("OMS_RECONCILE_DEGRADED: 远端快照不可用，跳过本轮对账");
    UpdateReconcileAnomalyProtection(true, "RECONCILE_DEGRADED");
    return;
  }

  auto result = reconciler_.Check(system_.account(), oms_, remote_notional);
  if (!result.ok) {
    // 成交刚落地时，远端持仓快照可能仍在最终一致性窗口内，先不累计失败次数。
    if (market_tick_count_ - last_fill_tick_ <= kReconcileRecentFillGraceTicks) {
      reconcile_streak_ = 0;
      LogInfo("OMS_RECONCILE_GRACE: recent_fill_tick=" +
              std::to_string(last_fill_tick_) +
              ", delta_notional=" + std::to_string(result.delta_notional_usd));
      UpdateReconcileAnomalyProtection(false, "RECONCILE_GRACE");
      return;
    }

    const double first_delta = result.delta_notional_usd;

    // Retry with fresh snapshot
    if (!remote_positions_fresh && adapter_->GetRemotePositions(&remote_positions)) {
      remote_positions_fresh = true;
    }
    if (remote_positions_fresh) {
      val = reconciler_.ComputeRemoteNotionalUsd(remote_positions);
      result = reconciler_.Check(system_.account(), oms_, val);
    }

    if (!result.ok) {
      const double second_delta = result.delta_notional_usd;
      const std::string symbol_delta_report = remote_positions_fresh
                                                  ? reconciler_.BuildSymbolDeltaReport(
                                                        system_.account(),
                                                        remote_positions,
                                                        tracked_symbols_,
                                                        /*min_abs_notional_delta_usd=*/1.0)
                                                  : "none";
      LogInfo("OMS_RECONCILE_MISMATCH: first_delta_notional=" +
              std::to_string(first_delta) +
              ", second_delta_notional=" + std::to_string(second_delta) +
              ", remote_positions_refreshed=" +
              std::string(remote_positions_fresh ? "true" : "false") +
              ", symbol_deltas=" + symbol_delta_report);

      // 运行时自愈：远端快照可用且达到防抖间隔时，以远端仓位重建本地仓位视图与 OMS 基线。
      // 该路径用于“WS/REST 成交回报缺失导致本地仓位漂移”的根治兜底。
      const bool can_auto_resync =
          config_.mode != "replay" &&
          remote_positions_fresh &&
          (market_tick_count_ - last_auto_resync_tick_ >=
           kReconcileAutoResyncCooldownTicks);
      if (can_auto_resync) {
        system_.ForceSyncAccountPositionsFromRemote(remote_positions);
        oms_.SeedNetPositionBaseline(remote_positions);
        pending_net_order_enqueued_ms_.clear();
        RemoteAccountBalanceSnapshot balance;
        if (adapter_->GetRemoteAccountBalance(&balance)) {
          system_.SyncAccountFromRemoteBalance(balance,
                                               /*reset_peak_to_equity=*/false);
        }
        last_auto_resync_tick_ = market_tick_count_;
        reconcile_streak_ = 0;
        LogInfo("OMS_RECONCILE_AUTORESYNC: applied=true, positions=" +
                std::to_string(remote_positions.size()) +
                ", cooldown_ticks=" +
                std::to_string(kReconcileAutoResyncCooldownTicks));
        UpdateReconcileAnomalyProtection(true, "RECONCILE_MISMATCH_AUTORESYNC");
        return;
      }

      if (++reconcile_streak_ >= config_.reconcile.mismatch_confirmations &&
          !reconcile_halted_) {
        reconcile_halted_ = true;
        RefreshTradingHaltState();
        LogError("CRITICAL: Reconcile mismatch confirmed. Halting trading.");
      }
      UpdateReconcileAnomalyProtection(true, "RECONCILE_MISMATCH_CONFIRMED");
    } else {
      reconcile_streak_ = 0;
      UpdateReconcileAnomalyProtection(false, "RECONCILE_OK");
    }
  } else {
    reconcile_streak_ = 0;
    UpdateReconcileAnomalyProtection(false, "RECONCILE_OK");
  }
}

// Gate 活跃度检查：支持“仅告警”与“运行时动作”两种模式。
void BotApplication::RunGateMonitor() {
  if (config_.gate.enforce_runtime_actions) {
    TickGateRuntimeCooldown();
  }

  // Gate 自动恢复“空仓稳态”判定：
  // 1) 账户当前无净敞口；
  // 2) 无净仓位相关在途订单（避免与落地中的订单竞争状态）；
  // 3) 连续满足最小 flat ticks 且冷却结束。
  const bool flat_and_idle =
      !HasExposure(system_.account().current_notional_usd()) &&
      pending_net_order_enqueued_ms_.empty();
  if (flat_and_idle) {
    ++gate_flat_ticks_streak_;
  } else {
    gate_flat_ticks_streak_ = 0;
  }

  if (config_.gate.enforce_runtime_actions &&
      config_.gate.auto_resume_when_flat &&
      (gate_halted_ || gate_forced_reduce_only_) &&
      !reconcile_halted_ && flat_and_idle &&
      gate_flat_ticks_streak_ >= config_.gate.auto_resume_flat_ticks &&
      gate_reduce_only_cooldown_ticks_left_ <= 0 &&
      gate_halt_cooldown_ticks_left_ <= 0) {
    gate_halted_ = false;
    gate_forced_reduce_only_ = false;
    gate_fail_windows_streak_ = 0;
    gate_pass_windows_streak_ = 0;
    RefreshReduceOnlyMode();
    RefreshTradingHaltState();
    LogInfo("GATE_RUNTIME_AUTO_RESUME: flat_ticks=" +
            std::to_string(gate_flat_ticks_streak_) +
            ", trading_halted=" +
            std::string(trading_halted_ ? "true" : "false") +
            ", reduce_only=" +
            std::string(IsForceReduceOnlyActive() ? "true" : "false"));
  }

  if (auto res = gate_monitor_.OnTick(); res.has_value()) {
    if (!res->pass) {
      std::ostringstream reasons;
      for (std::size_t i = 0; i < res->fail_reasons.size(); ++i) {
        if (i > 0) {
          reasons << ",";
        }
        reasons << res->fail_reasons[i];
      }
      LogInfo("GATE_CHECK_FAILED: raw_signals=" + std::to_string(res->raw_signals) +
              ", order_intents=" + std::to_string(res->order_intents) +
              ", effective_signals=" +
              std::to_string(res->effective_signals) +
              ", fills=" + std::to_string(res->fills) +
              ", fail_reasons=[" + reasons.str() + "]");
    } else {
      LogInfo("GATE_CHECK_PASSED: raw_signals=" +
              std::to_string(res->raw_signals) +
              ", order_intents=" + std::to_string(res->order_intents) +
              ", effective_signals=" +
              std::to_string(res->effective_signals) +
              ", fills=" + std::to_string(res->fills));
    }

    if (res->pass) {
      gate_fail_windows_streak_ = 0;
      ++gate_pass_windows_streak_;
    } else {
      ++gate_fail_windows_streak_;
      gate_pass_windows_streak_ = 0;
    }

    if (!config_.gate.enforce_runtime_actions) {
      return;
    }

    if (!res->pass) {
      if (!gate_forced_reduce_only_ &&
          config_.gate.fail_to_reduce_only_windows > 0 &&
          gate_fail_windows_streak_ >=
              config_.gate.fail_to_reduce_only_windows) {
        gate_forced_reduce_only_ = true;
        gate_reduce_only_cooldown_ticks_left_ =
            config_.gate.reduce_only_cooldown_ticks;
        RefreshReduceOnlyMode();
        LogInfo("GATE_RUNTIME_REDUCE_ONLY_ENTER: fail_streak=" +
                std::to_string(gate_fail_windows_streak_) +
                ", cooldown_ticks=" +
                std::to_string(gate_reduce_only_cooldown_ticks_left_));
      }

      if (!gate_halted_ && config_.gate.fail_to_halt_windows > 0 &&
          gate_fail_windows_streak_ >= config_.gate.fail_to_halt_windows) {
        gate_halted_ = true;
        gate_halt_cooldown_ticks_left_ = config_.gate.halt_cooldown_ticks;
        RefreshTradingHaltState();
        LogError("GATE_RUNTIME_HALT_ENTER: fail_streak=" +
                 std::to_string(gate_fail_windows_streak_) +
                 ", cooldown_ticks=" +
                 std::to_string(gate_halt_cooldown_ticks_left_));
      }
      return;
    }

    const bool resume_windows_reached =
        config_.gate.pass_to_resume_windows <= 0 ||
        gate_pass_windows_streak_ >= config_.gate.pass_to_resume_windows;

    if (gate_forced_reduce_only_ && resume_windows_reached &&
        gate_reduce_only_cooldown_ticks_left_ <= 0) {
      gate_forced_reduce_only_ = false;
      RefreshReduceOnlyMode();
      LogInfo("GATE_RUNTIME_REDUCE_ONLY_EXIT: pass_streak=" +
              std::to_string(gate_pass_windows_streak_));
    }

    if (gate_halted_ && resume_windows_reached &&
        gate_halt_cooldown_ticks_left_ <= 0) {
      gate_halted_ = false;
      RefreshTradingHaltState();
      LogInfo("GATE_RUNTIME_HALT_EXIT: pass_streak=" +
              std::to_string(gate_pass_windows_streak_) +
              ", trading_halted=" +
              std::string(trading_halted_ ? "true" : "false"));
    }
  }
}

void BotApplication::RunSelfEvolution() {
  if (!config_.self_evolution.enabled) {
    return;
  }

  const RegimeBucket active_bucket =
      has_last_regime_state_ ? last_regime_state_.bucket : RegimeBucket::kRange;
  const double trend_signal_notional_usd =
      has_tick_strategy_signal_ ? tick_trend_notional_usd_ : 0.0;
  const double defensive_signal_notional_usd =
      has_tick_strategy_signal_ ? tick_defensive_notional_usd_ : 0.0;
  const double mark_price_usd = has_latest_mark_price_ ? latest_mark_price_usd_ : 0.0;
  const std::string signal_symbol =
      has_tick_strategy_signal_ ? tick_strategy_signal_symbol_ : std::string();
  const auto action =
      self_evolution_.OnTick(market_tick_count_,
                             system_.account().cumulative_realized_net_pnl_usd(),
                             active_bucket,
                             system_.account().drawdown_pct(),
                             system_.account().current_notional_usd(),
                             trend_signal_notional_usd,
                             defensive_signal_notional_usd,
                             mark_price_usd,
                             signal_symbol,
                             tick_cost_filtered_signal_,
                             std::max(0, pending_fills_for_evolution_));
  pending_fills_for_evolution_ = 0;
  if (!action.has_value()) {
    return;
  }

  if (action->type == SelfEvolutionActionType::kUpdated ||
      action->type == SelfEvolutionActionType::kRolledBack) {
    std::string set_error;
    if (!system_.SetEvolutionWeightsForBucket(action->regime_bucket,
                                              action->trend_weight_after,
                                              action->defensive_weight_after,
                                              &set_error)) {
      ++funnel_window_.self_evolution_skipped;
      LogInfo("PORT_WEIGHT_INVALID_REJECTED: reason=" + set_error +
              ", trend_weight=" + std::to_string(action->trend_weight_after) +
              ", defensive_weight=" +
              std::to_string(action->defensive_weight_after));
      return;
    }
  }

  if (action->type == SelfEvolutionActionType::kUpdated) {
    ++funnel_window_.self_evolution_updates;
  } else if (action->type == SelfEvolutionActionType::kRolledBack) {
    ++funnel_window_.self_evolution_rollbacks;
  } else {
    ++funnel_window_.self_evolution_skipped;
  }

  const std::string direction_consistency_direction =
      action->direction_consistency_direction > 0
          ? "increase_trend"
          : (action->direction_consistency_direction < 0
                 ? "decrease_trend"
                 : "none");
  LogInfo("SELF_EVOLUTION_ACTION: type=" +
          std::string(EvolutionActionTypeToString(action->type)) +
          ", bucket=" + std::string(ToString(action->regime_bucket)) +
          ", reason=" + action->reason_code +
          ", bucket_ticks=" + std::to_string(action->window_bucket_ticks) +
          ", window_pnl_usd=" + std::to_string(action->window_pnl_usd) +
          ", window_realized_pnl_usd=" +
          std::to_string(action->window_realized_pnl_usd) +
          ", window_virtual_pnl_usd=" +
          std::to_string(action->window_virtual_pnl_usd) +
          ", pnl_source=" + std::string(action->used_virtual_pnl ? "virtual" : "realized") +
          ", counterfactual_search=" +
          std::string(action->used_counterfactual_search ? "true" : "false") +
          ", factor_ic_weighting=" +
          std::string(action->used_factor_ic_adaptive_weighting ? "true" : "false") +
          ", counterfactual_fallback={enabled=" +
          std::string(action->counterfactual_fallback_to_factor_ic_enabled
                          ? "true"
                          : "false") +
          ", used=" +
          std::string(action->counterfactual_fallback_to_factor_ic_used ? "true"
                                                                         : "false") +
          "}" +
          ", counterfactual_best_virtual_pnl_usd=" +
          std::to_string(action->counterfactual_best_virtual_pnl_usd) +
          ", counterfactual_best_weight={trend=" +
          std::to_string(action->counterfactual_best_trend_weight) +
          ",defensive=" +
          std::to_string(action->counterfactual_best_defensive_weight) + "}" +
          ", factor_ic={trend=" + std::to_string(action->trend_factor_ic) +
          ", defensive=" + std::to_string(action->defensive_factor_ic) +
          ", samples=" + std::to_string(action->factor_ic_samples) + "}" +
          ", window_fill_count=" +
          std::to_string(action->window_fill_count) +
          ", cost_filtered_signals=" +
          std::to_string(action->window_cost_filtered_signals) +
          ", learnability={enabled=" +
          std::string(action->learnability_gate_enabled ? "true" : "false") +
          ", passed=" +
          std::string(action->learnability_gate_passed ? "true" : "false") +
          ", t_stat=" + std::to_string(action->learnability_t_stat) +
          ", samples=" + std::to_string(action->learnability_samples) + "}" +
          ", direction_consistency={required=" +
          std::to_string(action->direction_consistency_required) +
          ", streak=" + std::to_string(action->direction_consistency_streak) +
          ", direction=" + direction_consistency_direction + "}" +
          ", counterfactual_required_improvement_usd=" +
          std::to_string(action->counterfactual_required_improvement_usd) +
          ", window_objective_score=" +
          std::to_string(action->window_objective_score) +
          ", window_max_drawdown_pct=" +
          std::to_string(action->window_max_drawdown_pct) +
          ", window_notional_churn_usd=" +
          std::to_string(action->window_notional_churn_usd) +
          ", weight_before={trend=" +
          std::to_string(action->trend_weight_before) +
          ",defensive=" + std::to_string(action->defensive_weight_before) +
          "}, weight_after={trend=" +
          std::to_string(action->trend_weight_after) +
          ",defensive=" + std::to_string(action->defensive_weight_after) +
          "}, degrade_windows=" + std::to_string(action->degrade_windows) +
          ", cooldown_remaining_ticks=" +
          std::to_string(action->cooldown_remaining_ticks));
}

// 运行态摘要日志：用于线上巡检与回放定位。
void BotApplication::LogStatus() {
  if (config_.system_status_log_interval_ticks <= 0) return;
  if (market_tick_count_ % config_.system_status_log_interval_ticks != 0) return;

  const bool trade_ok =
      adapter_ != nullptr && adapter_->TradeOk() && !trading_halted_ &&
      !IsForceReduceOnlyActive();

  std::string ws_summary = "n/a";
  if (const auto* bybit =
          dynamic_cast<const BybitExchangeAdapter*>(adapter_.get());
      bybit != nullptr) {
    ws_summary = bybit->ChannelHealthSummary();
  } else if (adapter_ != nullptr) {
    ws_summary = "adapter=" + adapter_->Name();
  }

  const OrderThrottleStats throttle_window = order_throttle_.ConsumeWindowStats();
  const OrderThrottleStats& throttle_total = order_throttle_.total_stats();
  const double throttle_window_hit_rate =
      throttle_window.checks > 0
          ? static_cast<double>(throttle_window.rejected) /
                static_cast<double>(throttle_window.checks)
          : 0.0;
  const double throttle_total_hit_rate =
      throttle_total.checks > 0
          ? static_cast<double>(throttle_total.rejected) /
                static_cast<double>(throttle_total.checks)
          : 0.0;

  const DecisionFunnelStats funnel_window = funnel_window_;
  AccumulateStats(&funnel_total_, funnel_window_);
  funnel_window_ = DecisionFunnelStats{};
  const double shadow_avg_model_score =
      funnel_window.integrator_scored > 0
          ? funnel_window.integrator_model_score_sum /
                static_cast<double>(funnel_window.integrator_scored)
          : 0.0;
  const double shadow_avg_p_up =
      funnel_window.integrator_scored > 0
          ? funnel_window.integrator_p_up_sum /
                static_cast<double>(funnel_window.integrator_scored)
          : 0.0;
  const double shadow_avg_p_down =
      funnel_window.integrator_scored > 0
          ? funnel_window.integrator_p_down_sum /
                static_cast<double>(funnel_window.integrator_scored)
          : 0.0;
  const double entry_edge_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_edge_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_base_required_edge_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_base_required_edge_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_required_edge_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_required_edge_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_adaptive_relax_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_adaptive_relax_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_maker_relax_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_maker_relax_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_regime_adjust_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_regime_adjust_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_volatility_adjust_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_volatility_adjust_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_liquidity_adjust_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_liquidity_adjust_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_quality_guard_penalty_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_quality_guard_penalty_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_edge_gap_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_edge_gap_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double filtered_cost_ratio =
      funnel_window.entry_edge_samples > 0
          ? static_cast<double>(funnel_window.intents_filtered_fee_aware) /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double filtered_cost_near_miss_ratio =
      funnel_window.entry_edge_samples > 0
          ? static_cast<double>(funnel_window.intents_filtered_fee_aware_near_miss) /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double passed_cost_near_miss_ratio =
      funnel_window.entry_edge_samples > 0
          ? static_cast<double>(funnel_window.intents_passed_fee_aware_near_miss) /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double strategy_trend_avg_abs_notional =
      funnel_window.strategy_mix_samples > 0
          ? funnel_window.trend_notional_abs_sum /
                static_cast<double>(funnel_window.strategy_mix_samples)
          : 0.0;
  const double strategy_defensive_avg_abs_notional =
      funnel_window.strategy_mix_samples > 0
          ? funnel_window.defensive_notional_abs_sum /
                static_cast<double>(funnel_window.strategy_mix_samples)
          : 0.0;
  const double strategy_blended_avg_abs_notional =
      funnel_window.strategy_mix_samples > 0
          ? funnel_window.blended_notional_abs_sum /
                static_cast<double>(funnel_window.strategy_mix_samples)
          : 0.0;
  const RegimeBucket active_bucket =
      has_last_regime_state_ ? last_regime_state_.bucket : RegimeBucket::kRange;
  const auto active_evolution_weights = system_.evolution_weights(active_bucket);
  const auto evolution_weights = system_.evolution_weights_all();
  const bool evolution_enabled =
      config_.self_evolution.enabled && self_evolution_.initialized();
  const bool evolution_cooldown =
      evolution_enabled && market_tick_count_ < self_evolution_.cooldown_until_tick();
  const int evolution_cooldown_remaining =
      evolution_cooldown
          ? static_cast<int>(self_evolution_.cooldown_until_tick() -
                             market_tick_count_)
          : 0;
  const double window_realized_net_delta_usd =
      has_last_status_account_snapshot_
          ? system_.account().cumulative_realized_net_pnl_usd() -
                last_status_realized_net_pnl_usd_
          : 0.0;
  const double window_fee_delta_usd =
      has_last_status_account_snapshot_
          ? system_.account().cumulative_fee_usd() - last_status_fee_usd_
          : 0.0;
  const double window_realized_net_per_fill_usd =
      funnel_window.fills_applied > 0
          ? window_realized_net_delta_usd /
                static_cast<double>(funnel_window.fills_applied)
          : 0.0;
  const double window_fee_bps_per_fill =
      funnel_window.fills_notional_abs_usd_sum > 1e-9
          ? window_fee_delta_usd / funnel_window.fills_notional_abs_usd_sum *
                10000.0
          : 0.0;
  const double window_maker_fee_bps =
      funnel_window.fills_maker_notional_abs_usd_sum > 1e-9
          ? funnel_window.fills_maker_fee_usd_sum /
                funnel_window.fills_maker_notional_abs_usd_sum * 10000.0
          : 0.0;
  const double window_taker_fee_bps =
      funnel_window.fills_taker_notional_abs_usd_sum > 1e-9
          ? funnel_window.fills_taker_fee_usd_sum /
                funnel_window.fills_taker_notional_abs_usd_sum * 10000.0
          : 0.0;
  const std::uint64_t liquidity_classified_fills =
      funnel_window.fills_maker_count + funnel_window.fills_taker_count +
      funnel_window.fills_unknown_liquidity_count;
  const double window_maker_fill_ratio =
      liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.fills_maker_count) /
                static_cast<double>(liquidity_classified_fills)
          : 0.0;
  const double window_unknown_fill_ratio =
      liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.fills_unknown_liquidity_count) /
                static_cast<double>(liquidity_classified_fills)
          : 0.0;
  const double window_explicit_liquidity_fill_ratio =
      liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.fills_explicit_liquidity_count) /
                static_cast<double>(liquidity_classified_fills)
          : 0.0;
  const double window_fee_sign_fallback_fill_ratio =
      liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.fills_fee_sign_fallback_count) /
                static_cast<double>(liquidity_classified_fills)
          : 0.0;
  double concentration_gross_notional_usd = 0.0;
  double concentration_top1_abs_notional_usd = 0.0;
  std::string concentration_top1_symbol = "n/a";
  const auto active_symbols = system_.account().GetActiveSymbols();
  for (const auto& symbol : active_symbols) {
    const double symbol_abs_notional =
        std::fabs(system_.account().current_notional_usd(symbol));
    concentration_gross_notional_usd += symbol_abs_notional;
    if (symbol_abs_notional > concentration_top1_abs_notional_usd) {
      concentration_top1_abs_notional_usd = symbol_abs_notional;
      concentration_top1_symbol = symbol;
    }
  }
  const double concentration_top1_share =
      concentration_gross_notional_usd > 1e-9
          ? concentration_top1_abs_notional_usd / concentration_gross_notional_usd
          : 0.0;
  recent_execution_window_maker_fill_ratio_ = window_maker_fill_ratio;
  recent_execution_window_unknown_fill_ratio_ = window_unknown_fill_ratio;
  EvaluateExecutionQualityGuard(funnel_window.fills_applied,
                                window_realized_net_per_fill_usd,
                                window_fee_bps_per_fill);

  LogInfo("RUNTIME_STATUS: ticks=" + std::to_string(market_tick_count_) +
          ", trade_ok=" + std::string(trade_ok ? "true" : "false") +
          ", trading_halted=" +
          std::string(trading_halted_ ? "true" : "false") +
          ", risk_mode=" + RiskModeToString(system_.risk_mode()) +
          ", ws={" + ws_summary + "}" +
          ", account={equity=" + std::to_string(system_.account().equity_usd()) +
          ", drawdown_pct=" + std::to_string(system_.account().drawdown_pct()) +
          ", notional=" + std::to_string(system_.account().current_notional_usd()) +
          ", realized_pnl=" +
          std::to_string(system_.account().cumulative_realized_pnl_usd()) +
          ", fees=" + std::to_string(system_.account().cumulative_fee_usd()) +
          ", realized_net=" +
          std::to_string(system_.account().cumulative_realized_net_pnl_usd()) +
          ", positions=" + FormatAccountPositions(system_.account()) + "}" +
          ", concentration={gross_notional_usd=" +
          std::to_string(concentration_gross_notional_usd) +
          ", top1_abs_notional_usd=" +
          std::to_string(concentration_top1_abs_notional_usd) +
          ", top1_symbol=" + concentration_top1_symbol +
          ", top1_share=" + std::to_string(concentration_top1_share) +
          ", symbol_count=" + std::to_string(active_symbols.size()) + "}" +
          ", funnel_window={raw=" + std::to_string(funnel_window.raw_signals) +
          ", risk_adjusted=" +
          std::to_string(funnel_window.risk_adjusted_signals) +
          ", intents_generated=" +
          std::to_string(funnel_window.intents_generated) +
          ", intents_filtered_inactive_symbol=" +
          std::to_string(funnel_window.intents_filtered_inactive_symbol) +
          ", intents_filtered_min_notional=" +
          std::to_string(funnel_window.intents_filtered_min_notional) +
          ", intents_filtered_fee_aware=" +
          std::to_string(funnel_window.intents_filtered_fee_aware) +
          ", intents_filtered_fee_aware_near_miss=" +
          std::to_string(funnel_window.intents_filtered_fee_aware_near_miss) +
          ", intents_passed_fee_aware_near_miss=" +
          std::to_string(funnel_window.intents_passed_fee_aware_near_miss) +
          ", intents_throttled_cost_cooldown=" +
          std::to_string(funnel_window.intents_throttled_cost_cooldown) +
          ", throttled=" + std::to_string(funnel_window.intents_throttled) +
          ", enqueued=" + std::to_string(funnel_window.intents_enqueued) +
          ", async_ok=" + std::to_string(funnel_window.async_submit_ok) +
          ", async_failed=" +
          std::to_string(funnel_window.async_submit_failed) +
          ", fills=" + std::to_string(funnel_window.fills_applied) +
          ", fills_notional_abs_usd=" +
          std::to_string(funnel_window.fills_notional_abs_usd_sum) +
          ", gate_alerts=" + std::to_string(funnel_window.gate_alerts) +
          ", evolution_updates=" +
          std::to_string(funnel_window.self_evolution_updates) +
          ", evolution_rollbacks=" +
          std::to_string(funnel_window.self_evolution_rollbacks) +
          ", evolution_skipped=" +
          std::to_string(funnel_window.self_evolution_skipped) +
          ", entry_edge_samples=" +
          std::to_string(funnel_window.entry_edge_samples) +
          ", entry_edge_avg_bps=" + std::to_string(entry_edge_avg_bps) +
          ", entry_base_required_avg_bps=" +
          std::to_string(entry_base_required_edge_avg_bps) +
          ", entry_required_avg_bps=" +
          std::to_string(entry_required_edge_avg_bps) +
          ", entry_adaptive_relax_avg_bps=" +
          std::to_string(entry_adaptive_relax_avg_bps) +
          ", entry_maker_relax_avg_bps=" +
          std::to_string(entry_maker_relax_avg_bps) +
          ", entry_regime_adjust_avg_bps=" +
          std::to_string(entry_regime_adjust_avg_bps) +
          ", entry_volatility_adjust_avg_bps=" +
          std::to_string(entry_volatility_adjust_avg_bps) +
          ", entry_liquidity_adjust_avg_bps=" +
          std::to_string(entry_liquidity_adjust_avg_bps) +
          ", entry_quality_guard_penalty_avg_bps=" +
          std::to_string(entry_quality_guard_penalty_avg_bps) +
          ", maker_fills=" + std::to_string(funnel_window.fills_maker_count) +
          ", taker_fills=" + std::to_string(funnel_window.fills_taker_count) +
          ", unknown_fills=" +
          std::to_string(funnel_window.fills_unknown_liquidity_count) +
          ", explicit_liquidity_fills=" +
          std::to_string(funnel_window.fills_explicit_liquidity_count) +
          ", fee_sign_fallback_fills=" +
          std::to_string(funnel_window.fills_fee_sign_fallback_count) + "}" +
          ", regime_window={trend_ticks=" +
          std::to_string(funnel_window.regime_trend_ticks) +
          ", range_ticks=" + std::to_string(funnel_window.regime_range_ticks) +
          ", extreme_ticks=" +
          std::to_string(funnel_window.regime_extreme_ticks) +
          ", warmup_ticks=" +
          std::to_string(funnel_window.regime_warmup_ticks) + "}" +
          ", regime_current={symbol=" +
          std::string(has_last_regime_state_ ? last_regime_state_.symbol : "n/a") +
          ", regime=" +
          std::string(has_last_regime_state_
                          ? ToString(last_regime_state_.regime)
                          : "n/a") +
          ", bucket=" +
          std::string(has_last_regime_state_
                          ? ToString(last_regime_state_.bucket)
                          : "n/a") +
          ", warmup=" +
          std::string(has_last_regime_state_ && last_regime_state_.warmup ? "true"
                                                                            : "false") +
          "}" +
          ", shadow_latest={enabled=" +
          std::string(has_last_shadow_inference_ &&
                              last_shadow_inference_.enabled
                          ? "true"
                          : "false") +
          ", model_version=" +
          std::string(has_last_shadow_inference_
                          ? last_shadow_inference_.model_version
                          : "n/a") +
          ", model_score=" +
          std::to_string(has_last_shadow_inference_
                             ? last_shadow_inference_.model_score
                             : 0.0) +
          ", p_up=" +
          std::to_string(has_last_shadow_inference_ ? last_shadow_inference_.p_up
                                                    : 0.0) +
          ", p_down=" +
          std::to_string(has_last_shadow_inference_
                             ? last_shadow_inference_.p_down
                             : 0.0) +
          "}" +
          ", shadow_window={scored=" +
          std::to_string(funnel_window.integrator_scored) +
          ", pred_up=" + std::to_string(funnel_window.integrator_pred_up) +
          ", pred_down=" + std::to_string(funnel_window.integrator_pred_down) +
          ", policy_applied=" +
          std::to_string(funnel_window.integrator_policy_applied) +
          ", policy_canary=" +
          std::to_string(funnel_window.integrator_policy_canary) +
          ", policy_active=" +
          std::to_string(funnel_window.integrator_policy_active) +
          ", avg_model_score=" + std::to_string(shadow_avg_model_score) +
          ", avg_p_up=" + std::to_string(shadow_avg_p_up) +
          ", avg_p_down=" + std::to_string(shadow_avg_p_down) + "}" +
          ", strategy_mix={latest_trend_notional=" +
          std::to_string(has_last_strategy_signal_
                             ? last_strategy_signal_.trend_notional_usd
                             : 0.0) +
          ", latest_defensive_notional=" +
          std::to_string(has_last_strategy_signal_
                             ? last_strategy_signal_.defensive_notional_usd
                             : 0.0) +
          ", latest_blended_notional=" +
          std::to_string(has_last_strategy_signal_
                             ? last_strategy_signal_.suggested_notional_usd
                             : 0.0) +
          ", avg_abs_trend_notional=" +
          std::to_string(strategy_trend_avg_abs_notional) +
          ", avg_abs_defensive_notional=" +
          std::to_string(strategy_defensive_avg_abs_notional) +
          ", avg_abs_blended_notional=" +
          std::to_string(strategy_blended_avg_abs_notional) +
          ", samples=" + std::to_string(funnel_window.strategy_mix_samples) + "}" +
          ", integrator_mode=" +
          std::string(ToString(system_.integrator_mode())) +
          ", gate_runtime={enabled=" +
          std::string(config_.gate.enforce_runtime_actions ? "true" : "false") +
          ", fail_streak=" + std::to_string(gate_fail_windows_streak_) +
          ", pass_streak=" + std::to_string(gate_pass_windows_streak_) +
          ", reduce_only=" +
          std::string(gate_forced_reduce_only_ ? "true" : "false") +
          ", reduce_only_cooldown_ticks=" +
          std::to_string(gate_reduce_only_cooldown_ticks_left_) +
          ", gate_halted=" + std::string(gate_halted_ ? "true" : "false") +
          ", halt_cooldown_ticks=" +
          std::to_string(gate_halt_cooldown_ticks_left_) +
          ", flat_ticks=" + std::to_string(gate_flat_ticks_streak_) + "}" +
          ", throttle_window={checks=" + std::to_string(throttle_window.checks) +
          ", rejected=" + std::to_string(throttle_window.rejected) +
          ", interval_rejects=" +
          std::to_string(throttle_window.interval_rejects) +
          ", reverse_rejects=" +
          std::to_string(throttle_window.reverse_rejects) +
          ", hit_rate=" + std::to_string(throttle_window_hit_rate) + "}" +
          ", throttle_total={checks=" + std::to_string(throttle_total.checks) +
          ", rejected=" + std::to_string(throttle_total.rejected) +
          ", hit_rate=" + std::to_string(throttle_total_hit_rate) + "}" +
          ", entry_gate={enabled=" +
          std::string(config_.execution_enable_fee_aware_entry_gate ? "true"
                                                                    : "false") +
          ", round_trip_cost_bps=" + std::to_string(RoundTripCostBps()) +
          ", min_expected_edge_bps=" +
          std::to_string(config_.execution_min_expected_edge_bps) +
          ", required_edge_cap_bps=" +
          std::to_string(config_.execution_required_edge_cap_bps) +
          ", adaptive_enabled=" +
          std::string(config_.execution_adaptive_fee_gate_enabled ? "true"
                                                                  : "false") +
          ", adaptive_trigger_ratio=" +
          std::to_string(config_.execution_adaptive_fee_gate_trigger_ratio) +
          ", adaptive_max_relax_bps=" +
          std::to_string(config_.execution_adaptive_fee_gate_max_relax_bps) +
          ", adaptive_min_samples=" +
          std::to_string(config_.execution_adaptive_fee_gate_min_samples) +
          ", maker_edge_relax_bps=" +
          std::to_string(config_.execution_maker_edge_relax_bps) +
          ", near_miss_tolerance_bps=" +
          std::to_string(config_.execution_entry_gate_near_miss_tolerance_bps) +
          ", near_miss_maker_allow=" +
          std::string(config_.execution_entry_gate_near_miss_maker_allow ? "true"
                                                                          : "false") +
          ", near_miss_maker_extra_gap_bps=" +
          std::to_string(config_.execution_entry_gate_near_miss_maker_max_gap_bps) +
          ", near_miss_maker_max_gap_bps=" +
          std::to_string(config_.execution_entry_gate_near_miss_maker_max_gap_bps) +
          ", quality_guard_penalty_bps=" +
          std::to_string(execution_quality_required_edge_penalty_bps_) +
          ", observed_filtered_ratio=" +
          std::to_string(entry_gate_observed_filtered_ratio_) +
          ", observed_near_miss_ratio=" +
          std::to_string(entry_gate_observed_near_miss_ratio_) +
          ", observed_near_miss_allowed_ratio=" +
          std::to_string(entry_gate_observed_near_miss_allowed_ratio_) +
          ", cooldown_trigger_count=" +
          std::to_string(config_.execution_cost_filter_cooldown_trigger_count) +
          ", cooldown_ticks=" +
          std::to_string(config_.execution_cost_filter_cooldown_ticks) +
          ", cooldown_symbols_active=" +
          std::to_string(cost_filter_cooldown_until_tick_by_symbol_.size()) + "}" +
          ", execution_window={filtered_cost_ratio=" +
          std::to_string(filtered_cost_ratio) +
          ", filtered_cost_near_miss_ratio=" +
          std::to_string(filtered_cost_near_miss_ratio) +
          ", passed_cost_near_miss_ratio=" +
          std::to_string(passed_cost_near_miss_ratio) +
          ", entry_edge_gap_avg_bps=" + std::to_string(entry_edge_gap_avg_bps) +
          ", realized_net_delta_usd=" +
          std::to_string(window_realized_net_delta_usd) +
          ", realized_net_per_fill=" +
          std::to_string(window_realized_net_per_fill_usd) +
          ", fee_delta_usd=" + std::to_string(window_fee_delta_usd) +
          ", fee_bps_per_fill=" + std::to_string(window_fee_bps_per_fill) +
          ", maker_fills=" + std::to_string(funnel_window.fills_maker_count) +
          ", taker_fills=" + std::to_string(funnel_window.fills_taker_count) +
          ", unknown_fills=" +
          std::to_string(funnel_window.fills_unknown_liquidity_count) +
          ", explicit_liquidity_fills=" +
          std::to_string(funnel_window.fills_explicit_liquidity_count) +
          ", fee_sign_fallback_fills=" +
          std::to_string(funnel_window.fills_fee_sign_fallback_count) +
          ", unknown_fill_ratio=" + std::to_string(window_unknown_fill_ratio) +
          ", explicit_liquidity_fill_ratio=" +
          std::to_string(window_explicit_liquidity_fill_ratio) +
          ", fee_sign_fallback_fill_ratio=" +
          std::to_string(window_fee_sign_fallback_fill_ratio) +
          ", maker_fee_bps=" + std::to_string(window_maker_fee_bps) +
          ", taker_fee_bps=" + std::to_string(window_taker_fee_bps) +
          ", maker_fill_ratio=" + std::to_string(window_maker_fill_ratio) +
          "}" +
          ", execution_quality_guard={enabled=" +
          std::string(config_.execution_quality_guard_enabled ? "true"
                                                              : "false") +
          ", active=" +
          std::string(execution_quality_guard_active_ ? "true" : "false") +
          ", bad_streak=" + std::to_string(execution_quality_bad_streak_) +
          ", good_streak=" + std::to_string(execution_quality_good_streak_) +
          ", min_fills=" +
          std::to_string(config_.execution_quality_guard_min_fills) +
          ", trigger_streak=" +
          std::to_string(config_.execution_quality_guard_bad_streak_to_trigger) +
          ", release_streak=" +
          std::to_string(
              config_.execution_quality_guard_good_streak_to_release) +
          ", min_realized_net_per_fill_usd=" +
          std::to_string(
              config_.execution_quality_guard_min_realized_net_per_fill_usd) +
          ", max_fee_bps_per_fill=" +
          std::to_string(config_.execution_quality_guard_max_fee_bps_per_fill) +
          ", applied_penalty_bps=" +
          std::to_string(execution_quality_required_edge_penalty_bps_) + "}" +
          ", reconcile_runtime={anomaly_streak=" +
          std::to_string(reconcile_anomaly_streak_) +
          ", healthy_streak=" + std::to_string(reconcile_healthy_streak_) +
          ", anomaly_reduce_only=" +
          std::string(reconcile_forced_reduce_only_ ? "true" : "false") +
          ", anomaly_reduce_only_threshold=" +
          std::to_string(config_.reconcile.anomaly_reduce_only_streak) +
          ", anomaly_halt_threshold=" +
          std::to_string(config_.reconcile.anomaly_halt_streak) +
          ", anomaly_resume_threshold=" +
          std::to_string(config_.reconcile.anomaly_resume_streak) + "}" +
          ", evolution={enabled=" +
          std::string(evolution_enabled ? "true" : "false") +
          ", objective={alpha_pnl=" +
          std::to_string(config_.self_evolution.objective_alpha_pnl) +
          ", beta_drawdown=" +
          std::to_string(config_.self_evolution.objective_beta_drawdown) +
          ", gamma_notional_churn=" +
          std::to_string(
              config_.self_evolution.objective_gamma_notional_churn) +
          "}" +
          ", factor_ic_weighting=" +
          std::string(config_.self_evolution.enable_factor_ic_adaptive_weights
                          ? "true"
                          : "false") +
          ", factor_ic_min_samples=" +
          std::to_string(config_.self_evolution.factor_ic_min_samples) +
          ", factor_ic_min_abs=" +
          std::to_string(config_.self_evolution.factor_ic_min_abs) +
          ", learnability_gate=" +
          std::string(config_.self_evolution.enable_learnability_gate ? "true"
                                                                      : "false") +
          ", learnability_min_samples=" +
          std::to_string(config_.self_evolution.learnability_min_samples) +
          ", learnability_min_t_stat_abs=" +
          std::to_string(config_.self_evolution.learnability_min_t_stat_abs) +
          ", active_bucket=" + std::string(ToString(active_bucket)) +
          ", active_trend_weight=" +
          std::to_string(active_evolution_weights.trend_weight) +
          ", active_defensive_weight=" +
          std::to_string(active_evolution_weights.defensive_weight) +
          ", by_bucket={trend=(" + std::to_string(evolution_weights[0].trend_weight) +
          "," + std::to_string(evolution_weights[0].defensive_weight) + ")" +
          ", range=(" + std::to_string(evolution_weights[1].trend_weight) +
          "," + std::to_string(evolution_weights[1].defensive_weight) + ")" +
          ", extreme=(" + std::to_string(evolution_weights[2].trend_weight) +
          "," + std::to_string(evolution_weights[2].defensive_weight) + ")}" +
          ", next_eval_tick=" +
          std::to_string(self_evolution_.next_eval_tick()) +
          ", cooldown=" + std::string(evolution_cooldown ? "true" : "false") +
          ", cooldown_remaining_ticks=" +
          std::to_string(evolution_cooldown_remaining) + "}");
  last_status_realized_net_pnl_usd_ =
      system_.account().cumulative_realized_net_pnl_usd();
  last_status_fee_usd_ = system_.account().cumulative_fee_usd();
  has_last_status_account_snapshot_ = true;
}

/**
 * @brief 退出条件判断
 *
 * - live/paper: 受 system_max_ticks 控制；
 * - replay: 数据耗尽后自动退出。
 */
bool BotApplication::ShouldExit(bool has_market, bool has_fill) {
  if (config_.system_max_ticks > 0 &&
      market_tick_count_ >= config_.system_max_ticks) {
    return true;
  }
  if (config_.mode == "replay" && !has_market && !has_fill) {
    return true;
  }
  return false;
}

// 停机顺序：先停执行线程，再输出结束日志。
void BotApplication::Shutdown() {
  if (executor_) executor_->Stop();
  LogInfo("Bot Shutdown.");
}

}  // namespace ai_trade
