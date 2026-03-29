#pragma once

#include <optional>

#include "core/types.h"

namespace ai_trade {

/**
 * @brief 执行引擎配置参数
 */
struct ExecutionProtectionConfig {
  bool enabled{false};
  bool require_sl{true};
  bool enable_tp{true};
  int attach_timeout_ms{1500};
  double stop_loss_ratio{0.01};
  double take_profit_ratio{0.015};
  bool dynamic_distance_enabled{false};
  double dynamic_distance_volatility_multiplier{0.0};
  double dynamic_stop_loss_min_ratio{0.0};
  double dynamic_stop_loss_max_ratio{0.0};
  double dynamic_take_profit_min_ratio{0.0};
  double dynamic_take_profit_max_ratio{0.0};
  double dynamic_take_profit_rr_multiplier{1.5};
  bool break_even_enabled{false};
  double break_even_trigger_ratio{0.0};
  double break_even_offset_ratio{0.0};
  bool trailing_enabled{false};
  double trailing_trigger_ratio{0.0};
  double trailing_distance_ratio{0.0};
  double profit_protection_min_update_ratio{0.001};
  bool cancel_opposite_on_fill{true};
};

struct RetryPolicyConfig {
  int max_attempts{3};
  int backoff_ms{500};
};

/**
 * @brief 对应 YAML 配置中的 "execution" 节点
 */
struct ExecutionEngineConfig {
  double max_order_notional_usd{5000.0};  ///< 单笔订单最大名义敞口（USD）。
  double min_rebalance_notional_usd{0.0};  ///< 小于该净名义敞口差值则忽略调仓。
  double same_side_rebalance_multiplier{1.0};  ///< 同向加仓时对最小调仓门槛的额外倍率。
  bool direct_flip_entry_enabled{false};  ///< true=反手时允许直接按净差额发开仓单（非先平后开）。
  int min_order_interval_ms{3000};
  int reverse_signal_cooldown_ticks{4};
  int max_slippage_bps{20};
  int order_timeout_ms{10000};
  ExecutionProtectionConfig protection{};
  RetryPolicyConfig retry_policy{};
};

/**
 * @brief 执行引擎 (ExecutionEngine)
 *
 * 负责根据策略生成的目标仓位（Target Position）和当前账户状态，
 * 计算出具体的订单意图（OrderIntent），包括开仓、平仓或调整仓位。
 * 
 * 核心功能：
 * 1. 净名义敞口差额计算 (Target - Current)
 * 2. 最小调仓阈值过滤
 * 3. 反向开仓处理（先平后开，使用只减仓）
 * 4. 保护单生成 (SL/TP)
 */
class ExecutionEngine {
 public:
  /**
   * @brief 构造函数：仅指定单笔上限，其他使用默认配置
   * @param max_order_notional_usd 单笔订单允许的最大名义敞口 (USD)
   */
  explicit ExecutionEngine(double max_order_notional_usd)
      : config_{.max_order_notional_usd = max_order_notional_usd} {}

  /**
   * @brief 构造函数：使用完整配置对象
   */
  explicit ExecutionEngine(ExecutionEngineConfig config)
      : config_(config) {}

  /**
   * @brief 构建交易意图
   *
   * @param target 经过风险控制调整后的目标净名义敞口（single-symbol, signed）
   * @param current_notional_usd 当前持仓净名义敞口（single-symbol, signed）
   * @param price 当前市场价格
   * @return std::optional<OrderIntent> 如果需要执行交易，返回订单意图；否则返回 std::nullopt
   */
  std::optional<OrderIntent> BuildIntent(const RiskAdjustedPosition& target,
                                         double current_notional_usd,
                                         double price) const;

  /**
   * @brief 构建保护性订单意图（止盈/止损）
   *
   * @param entry_fill 触发该保护逻辑的入场成交事件
   * @param purpose 订单目的 (例如 StopLoss 或 TakeProfit)
   * @param distance_ratio 保护价格距离入场价格的比例 (例如 0.01 代表 1%)
   * @return std::optional<OrderIntent> 保护性订单意图
   */
  std::optional<OrderIntent> BuildProtectionIntent(const FillEvent& entry_fill,
                                                   OrderPurpose purpose,
                                                   double distance_ratio) const;

  /**
   * @brief 以显式触发价格构建保护单（用于 break-even / trailing）
   */
  std::optional<OrderIntent> BuildProtectionIntentAtPrice(
      const FillEvent& entry_fill,
      OrderPurpose purpose,
      double trigger_price) const;

 private:
  ExecutionEngineConfig config_{};  ///< 执行层参数。
};

/**
 * @brief 计算保护单触发价格
 */
double ComputeProtectionPrice(int entry_direction,
                              Price anchor_price,
                              OrderPurpose purpose,
                              double distance_ratio);

/**
 * @brief 合并基础距离与动态距离，输出最终保护距离比例
 */
double ComputeEffectiveProtectionDistanceRatio(double base_ratio,
                                               double dynamic_ratio,
                                               double min_ratio,
                                               double max_ratio);

/**
 * @brief 计算盈利保护应抬升到的止损价格（仅返回盈利保护候选，不含初始止损）
 */
std::optional<double> ComputeProfitProtectionStopPrice(
    int entry_direction,
    double entry_price,
    double best_price,
    bool break_even_enabled,
    double break_even_trigger_ratio,
    double break_even_offset_ratio,
    bool trailing_enabled,
    double trailing_trigger_ratio,
    double trailing_distance_ratio);

}  // namespace ai_trade
