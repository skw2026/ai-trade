#pragma once

#include <array>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "execution/execution_engine.h"
#include "market/market_data.h"
#include "oms/account_state.h"
#include "regime/regime_engine.h"
#include "risk/risk_engine.h"
#include "strategy/integrator_shadow.h"
#include "strategy/strategy_engine.h"

namespace ai_trade {

// 单次行情评估的分层产物，便于监控漏斗和审计回放。
struct MarketDecision {
  RegimeState regime;  ///< 当前行情对应 Regime 判定结果。
  Signal base_signal;  ///< 原策略信号（未经过 Integrator 接管）。
  Signal signal;  ///< 最终信号（可能经过 Integrator 接管）。
  ShadowInference shadow;  ///< Integrator 影子推理输出（观测用途）。
  bool integrator_policy_applied{false};  ///< 本 tick 是否发生 Integrator 接管。
  std::string integrator_policy_reason{"n/a"};  ///< 接管/跳过原因。
  double integrator_confidence{0.0};  ///< p_up - p_down，范围 [-1, 1]。
  TargetPosition target;  ///< 组合层目标净名义敞口（single-symbol）。
  RiskAdjustedPosition risk_adjusted;  ///< 风控修正后的目标净名义敞口。
  std::optional<OrderIntent> intent;  ///< 执行层输出订单意图（如有）。
};

/**
 * @brief 交易决策流水线编排器
 *
 * 责任边界：
 * 1. 接收行情并驱动 Strategy -> Risk -> Execution；
 * 2. 对外暴露“是否下单”的标准决策结果；
 * 3. 接收成交回报并推进账户状态。
 *
 * 非职责：
 * - 不直接访问交易所；
 * - 不负责 WAL/OMS 持久化与去重。
 */
class TradeSystem {
 public:
  TradeSystem(double risk_cap_usd,
              double max_order_notional_usd,
              RiskThresholds thresholds = {},
              StrategyConfig strategy_config = {},
              double min_rebalance_notional_usd = 0.0,
              RegimeConfig regime_config = {},
              IntegratorConfig integrator_config = {})
      : strategy_(strategy_config),
        regime_(regime_config),
        risk_(risk_cap_usd, thresholds),
        max_account_gross_notional_usd_(risk_cap_usd),
        execution_(ExecutionEngineConfig{
            .max_order_notional_usd = max_order_notional_usd,
            .min_rebalance_notional_usd = min_rebalance_notional_usd,
        }),
        integrator_config_(integrator_config),
        integrator_shadow_(integrator_config.shadow) {}

  /// 便捷入口：仅用于本地快速回放，内部会把意图直接转成模拟成交。
  bool OnPrice(double price, bool trade_ok = true);

  /// 标准流水线：输出各层结果，便于监控漏斗与审计。
  MarketDecision Evaluate(const MarketEvent& event, bool trade_ok = true);

  /// 标准入口：输入外部行情，输出下单意图（不直接改账户仓位）。
  std::optional<OrderIntent> OnMarket(const MarketEvent& event,
                                      bool trade_ok = true) {
    return Evaluate(event, trade_ok).intent;
  }

  /// 由外部成交回报驱动账户状态更新。
  void OnFill(const FillEvent& fill) { account_.ApplyFill(fill); }
  void OnMarketSnapshot(const MarketEvent& event) { account_.OnMarket(event); }
  void SyncAccountFromRemotePositions(
      const std::vector<RemotePositionSnapshot>& positions,
      double baseline_cash_usd = 10000.0) {
    account_.SyncFromRemotePositions(positions, baseline_cash_usd);
  }
  /// 运行时风险字段刷新：仅更新远端风险相关字段（如强平价），不重置现金基线。
  void RefreshAccountRiskFromRemotePositions(
      const std::vector<RemotePositionSnapshot>& positions) {
    account_.RefreshRiskFromRemotePositions(positions);
  }
  /// 运行时强制远端仓位对齐（仅重建仓位，不重置现金与峰值权益）。
  void ForceSyncAccountPositionsFromRemote(
      const std::vector<RemotePositionSnapshot>& positions) {
    account_.ForceSyncPositionsFromRemote(positions);
  }
  /// 同步远端账户资金快照（equity/wallet/upnl）到本地账户口径。
  void SyncAccountFromRemoteBalance(const RemoteAccountBalanceSnapshot& balance,
                                    bool reset_peak_to_equity) {
    account_.SyncFromRemoteAccountBalance(balance, reset_peak_to_equity);
  }
  /// 自进化权重是否启用（启用后会缩放策略目标名义值）。
  void EnableEvolution(bool enabled) { evolution_enabled_ = enabled; }
  /// 设置所有 Regime bucket 的自进化权重（兼容旧调用）。
  bool SetEvolutionWeights(double trend_weight,
                           double defensive_weight,
                           std::string* out_error);
  /// 仅设置单个 Regime bucket 的自进化权重。
  bool SetEvolutionWeightsForBucket(RegimeBucket bucket,
                                    double trend_weight,
                                    double defensive_weight,
                                    std::string* out_error);
  /// 获取单个 Regime bucket 的自进化权重。
  EvolutionWeights evolution_weights(RegimeBucket bucket) const;
  /// 获取三桶权重快照（Trend/Range/Extreme）。
  std::array<EvolutionWeights, 3> evolution_weights_all() const;
  /// 兼容旧调用：默认返回 RANGE bucket 权重。
  EvolutionWeights evolution_weights() const {
    return evolution_weights(RegimeBucket::kRange);
  }
  bool InitializeIntegratorShadow(std::string* out_error) {
    const bool strict_takeover = (integrator_config_.mode == IntegratorMode::kCanary ||
                                  integrator_config_.mode == IntegratorMode::kActive);
    return integrator_shadow_.Initialize(strict_takeover, out_error);
  }
  IntegratorMode integrator_mode() const { return integrator_config_.mode; }
  void SetIntegratorMode(IntegratorMode mode) { integrator_config_.mode = mode; }
  bool integrator_shadow_enabled() const { return integrator_shadow_.enabled(); }
  std::string integrator_shadow_model_version() const {
    return integrator_shadow_.model_version();
  }
  void ForceReduceOnly(bool enabled) { risk_.SetForcedReduceOnly(enabled); }
  RiskMode risk_mode() const { return risk_.mode(); }

  const AccountState& account() const { return account_; }

 private:
  MarketData market_;  ///< 回放模式行情生成器。
  StrategyEngine strategy_;  ///< 策略引擎。
  RegimeEngine regime_;  ///< Regime 识别器。
  RiskEngine risk_;  ///< 风控引擎。
  bool evolution_enabled_{false};  ///< 是否启用自进化权重缩放。
  // Regime 分桶权重：索引顺序固定为 Trend/Range/Extreme。
  std::array<EvolutionWeights, 3> evolution_weights_by_bucket_{
      EvolutionWeights{1.0, 0.0},
      EvolutionWeights{1.0, 0.0},
      EvolutionWeights{1.0, 0.0},
  };
  // 账户级总名义敞口上限（gross），用于多币种场景的统一预算裁剪。
  double max_account_gross_notional_usd_{3000.0};
  ExecutionEngine execution_;  ///< 执行引擎。
  IntegratorConfig integrator_config_{};  ///< Integrator 接管配置（mode/阈值/比例）。
  IntegratorShadow integrator_shadow_;  ///< Integrator 影子推理器（观测用途）。
  AccountState account_;  ///< 账户状态聚合。

  static std::size_t BucketIndex(RegimeBucket bucket);
  bool ApplyIntegratorPolicy(const ShadowInference& shadow,
                             Signal* inout_signal,
                             double* out_confidence,
                             std::string* out_reason) const;
};

}  // namespace ai_trade
