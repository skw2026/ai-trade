#include "execution/execution_engine.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <sstream>
#include <random>

namespace ai_trade {

namespace {

// client_order_id 规则：
// symbol + 毫秒时间 + 进程实例随机串 + 单调序号。
// 目标是“单进程唯一 + 可排障可追溯”。
std::string BuildClientOrderId(const std::string& symbol) {
  static std::atomic<std::uint64_t> seq{0};
  // 生成一个进程生命周期内唯一的随机标识 (Instance ID)
  static const std::string instance_id = []() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<std::uint32_t> dist(1000, 9999);
    return std::to_string(dist(gen));
  }();

  const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now());
  const auto ts_ms = now.time_since_epoch().count();

  std::ostringstream oss;
  oss << symbol << "-" << ts_ms << "-" << instance_id << "-" << seq.fetch_add(1, std::memory_order_relaxed);
  return oss.str();
}

}  // namespace

double ComputeProtectionPrice(int entry_direction,
                              Price anchor_price,
                              OrderPurpose purpose,
                              double distance_ratio) {
  if ((purpose != OrderPurpose::kSl && purpose != OrderPurpose::kTp) ||
      anchor_price <= 0.0 || distance_ratio <= 0.0 || entry_direction == 0) {
    return 0.0;
  }
  if (entry_direction > 0) {
    return (purpose == OrderPurpose::kSl)
               ? anchor_price * (1.0 - distance_ratio)
               : anchor_price * (1.0 + distance_ratio);
  }
  return (purpose == OrderPurpose::kSl)
             ? anchor_price * (1.0 + distance_ratio)
             : anchor_price * (1.0 - distance_ratio);
}

double ComputeEffectiveProtectionDistanceRatio(double base_ratio,
                                               double dynamic_ratio,
                                               double min_ratio,
                                               double max_ratio) {
  double effective_ratio = std::max(base_ratio, dynamic_ratio);
  if (min_ratio > 0.0) {
    effective_ratio = std::max(effective_ratio, min_ratio);
  }
  if (max_ratio > 0.0) {
    effective_ratio = std::min(effective_ratio, max_ratio);
  }
  return effective_ratio;
}

std::optional<double> ComputeProfitProtectionStopPrice(
    int entry_direction,
    double entry_price,
    double best_price,
    bool break_even_enabled,
    double break_even_trigger_ratio,
    double break_even_offset_ratio,
    bool trailing_enabled,
    double trailing_trigger_ratio,
    double trailing_distance_ratio) {
  if (entry_direction == 0 || entry_price <= 0.0 || best_price <= 0.0) {
    return std::nullopt;
  }

  const bool is_long = entry_direction > 0;
  const double favorable_return =
      is_long ? (best_price / entry_price - 1.0)
              : (entry_price / best_price - 1.0);
  double candidate_stop = 0.0;
  bool has_candidate = false;

  if (break_even_enabled && break_even_trigger_ratio > 0.0 &&
      favorable_return >= break_even_trigger_ratio) {
    candidate_stop = is_long ? entry_price * (1.0 + break_even_offset_ratio)
                             : entry_price * (1.0 - break_even_offset_ratio);
    has_candidate = true;
  }

  if (trailing_enabled && trailing_trigger_ratio > 0.0 &&
      trailing_distance_ratio > 0.0 &&
      favorable_return >= trailing_trigger_ratio) {
    const double trailing_stop =
        is_long ? best_price * (1.0 - trailing_distance_ratio)
                : best_price * (1.0 + trailing_distance_ratio);
    if (!has_candidate) {
      candidate_stop = trailing_stop;
      has_candidate = true;
    } else if (is_long) {
      candidate_stop = std::max(candidate_stop, trailing_stop);
    } else {
      candidate_stop = std::min(candidate_stop, trailing_stop);
    }
  }

  if (!has_candidate || candidate_stop <= 0.0) {
    return std::nullopt;
  }
  return candidate_stop;
}

/**
 * @brief 根据目标仓位与当前仓位生成下单意图
 *
 * 核心原则：
 * 1. 只减仓（reduce-only）只允许净名义敞口向 0 收敛；
 * 2. 小于最小净名义敞口调仓门槛则忽略；
 * 3. 反手时先平旧仓，再由下一轮决策考虑开新仓。
 */
std::optional<OrderIntent> ExecutionEngine::BuildIntent(
    const RiskAdjustedPosition& target,
    double current_notional_usd,
    double price) const {
  constexpr double kEpsilon = 1e-4;
  if (price <= 0.0) {
    return std::nullopt;
  }

  double effective_target = target.adjusted_notional_usd;
  // ReduceOnly 模式下，目标仓位必须向 0 收敛，禁止反向或加仓。
  if (target.reduce_only) {
    // reduce_only 语义：只能向 0 方向减仓，禁止加仓和反手开仓。
    if (std::fabs(current_notional_usd) < kEpsilon) {
      return std::nullopt;
    }
    if (current_notional_usd > 0.0) {
      effective_target = std::clamp(effective_target, 0.0, current_notional_usd);
    } else {
      effective_target = std::clamp(effective_target, current_notional_usd, 0.0);
    }
  }

  const double total_delta = effective_target - current_notional_usd;
  const bool same_side_add =
      !target.reduce_only &&
      std::fabs(current_notional_usd) >= kEpsilon &&
      std::fabs(effective_target) >= kEpsilon &&
      current_notional_usd * effective_target > kEpsilon &&
      std::fabs(effective_target) > std::fabs(current_notional_usd) + kEpsilon;
  double min_rebalance_notional_usd = config_.min_rebalance_notional_usd;
  if (same_side_add && min_rebalance_notional_usd > 0.0) {
    min_rebalance_notional_usd *=
        std::max(1.0, config_.same_side_rebalance_multiplier);
  }
  // 防抖：净名义敞口总变动过小则不下单，减少无效交易和手续费磨损。
  if (!target.reduce_only &&
      std::fabs(total_delta) < min_rebalance_notional_usd) {
    return std::nullopt;
  }

  // 反向切仓：
  // - 默认：先平后开，降低单笔跨方向复杂性；
  // - 可选 direct_flip：允许直接按净差额发单，减少 2x RTT。
  if (!target.reduce_only &&
      std::fabs(current_notional_usd) >= kEpsilon &&
      std::fabs(effective_target) >= kEpsilon &&
      (current_notional_usd * effective_target < -kEpsilon)) {
    if (config_.direct_flip_entry_enabled) {
      OrderIntent flip_intent;
      flip_intent.client_order_id = BuildClientOrderId(target.symbol);
      flip_intent.symbol = target.symbol;
      flip_intent.reduce_only = false;
      flip_intent.purpose = OrderPurpose::kEntry;
      // 反手窗口优先成交，避免先平后开导致的延迟累积。
      flip_intent.liquidity_preference = LiquidityPreference::kTaker;
      flip_intent.direction = (total_delta > 0.0) ? 1 : -1;
      const double flip_notional =
          std::min(std::fabs(total_delta), config_.max_order_notional_usd);
      flip_intent.qty = flip_notional / price;
      flip_intent.price = price;
      return flip_intent;
    }

    // 默认模式：第一步只平旧仓，不在同一笔意图中携带新方向开仓。
    const double close_notional =
        std::min(std::fabs(current_notional_usd), config_.max_order_notional_usd);

    OrderIntent close_intent;
    close_intent.client_order_id = BuildClientOrderId(target.symbol);
    close_intent.symbol = target.symbol;
    close_intent.reduce_only = true;
    close_intent.purpose = OrderPurpose::kReduce;
    close_intent.liquidity_preference = LiquidityPreference::kTaker;
    close_intent.direction = (current_notional_usd > 0.0) ? -1 : 1;
    close_intent.qty = close_notional / price;
    close_intent.price = price;
    return close_intent;
  }

  if (std::fabs(total_delta) < kEpsilon) {
    return std::nullopt;
  }

  OrderIntent intent;
  intent.client_order_id = BuildClientOrderId(target.symbol);
  intent.symbol = target.symbol;
  intent.reduce_only = target.reduce_only;
  intent.purpose = target.reduce_only ? OrderPurpose::kReduce : OrderPurpose::kEntry;
  intent.liquidity_preference =
      target.reduce_only ? LiquidityPreference::kTaker
                         : LiquidityPreference::kMaker;
  intent.direction = (total_delta > 0.0) ? 1 : -1;
  const double order_notional =
      std::min(std::fabs(total_delta), config_.max_order_notional_usd);
  intent.qty = order_notional / price;
  intent.price = price;
  return intent;
}

std::optional<OrderIntent> ExecutionEngine::BuildProtectionIntent(
    const FillEvent& entry_fill,
    OrderPurpose purpose,
    double distance_ratio) const {
  if ((purpose != OrderPurpose::kSl && purpose != OrderPurpose::kTp) ||
      distance_ratio <= 0.0 ||
      entry_fill.qty <= 0.0 ||
      entry_fill.price <= 0.0 ||
      entry_fill.direction == 0) {
    return std::nullopt;
  }

  const double protection_price = ComputeProtectionPrice(entry_fill.direction,
                                                         entry_fill.price,
                                                         purpose,
                                                         distance_ratio);
  return BuildProtectionIntentAtPrice(entry_fill, purpose, protection_price);
}

std::optional<OrderIntent> ExecutionEngine::BuildProtectionIntentAtPrice(
    const FillEvent& entry_fill,
    OrderPurpose purpose,
    double trigger_price) const {
  if ((purpose != OrderPurpose::kSl && purpose != OrderPurpose::kTp) ||
      entry_fill.qty <= 0.0 ||
      entry_fill.direction == 0 ||
      trigger_price <= 0.0) {
    return std::nullopt;
  }

  OrderIntent intent;
  intent.client_order_id = BuildClientOrderId(entry_fill.symbol);
  intent.parent_order_id = entry_fill.client_order_id;
  intent.symbol = entry_fill.symbol;
  intent.purpose = purpose;
  intent.liquidity_preference = LiquidityPreference::kTaker;
  intent.reduce_only = true;
  intent.direction = -entry_fill.direction;
  intent.qty = entry_fill.qty;
  intent.price = trigger_price;
  return intent;
}

}  // namespace ai_trade
