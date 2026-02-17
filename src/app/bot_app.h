#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "core/config.h"
#include "core/types.h"
#include "evolution/self_evolution_controller.h"
#include "exchange/exchange_adapter.h"
#include "execution/async_executor.h"
#include "execution/execution_engine.h"
#include "execution/order_throttle.h"
#include "monitor/gate_monitor.h"
#include "oms/order_manager.h"
#include "oms/reconciler.h"
#include "storage/wal_store.h"
#include "system/trade_system.h"
#include "universe/universe_selector.h"

namespace ai_trade {

/**
 * @brief 交易机器人主应用程序类
 *
 * 负责系统的生命周期管理，包括：
 * 1. 初始化各子模块 (WAL, Adapter, OMS, Risk, Strategy 等)
 * 2. 执行主事件循环 (Market -> Strategy -> Risk -> Execution)
 * 3. 管理异步执行与回报处理
 * 4. 协调对账与风控熔断
 */
class BotApplication {
 public:
  /**
   * @brief 构造函数
   * @param config 应用程序完整配置
   */
  explicit BotApplication(const AppConfig& config);

  /**
   * @brief 运行机器人
   *
   * 执行初始化流程，成功后进入主循环。
   * @return int 退出码 (0 表示正常退出, 非 0 表示异常)
   */
  int Run();

 private:
  // --- 初始化阶段 ---

  /**
   * @brief 系统初始化
   * @return true 初始化成功
   * @return false 初始化失败（如连接失败、WAL加载失败）
   */
  bool Initialize();

  /**
   * @brief 初始化交易标的池 (Universe)
   * 根据配置和交易所规则，筛选出可交易的 Symbol 列表。
   */
  void InitializeUniverse();

  /**
   * @brief 启动同步远端账户状态
   * 拉取持仓与资金口径，用于初始化本地 OMS/AccountState。
   */
  void SyncRemotePositions();

  // --- 运行阶段 ---

  /**
   * @brief 主事件循环
   * 持续轮询行情、成交和异步结果，驱动策略运转。
   */
  void RunLoop();

  /**
   * @brief 处理行情事件
   * 包含：Universe 更新、策略计算、风控检查、生成订单意图。
   */
  void ProcessMarketEvent(const MarketEvent& event);

  /**
   * @brief 处理成交回报事件
   * 更新 OMS、WAL 和 AccountState，并触发保护单逻辑。
   */
  void ProcessFillEvent(const FillEvent& fill);

  /**
   * @brief 处理异步执行结果
   * 处理下单/撤单的 ACK 或 Reject，更新 OMS 状态。
   */
  void ProcessAsyncResults();

  /// 估算单次开仓的 round-trip 成本阈值（bps）。
  double RoundTripCostBps() const;
  /// 基于当前 regime 与信号估算开仓边际（bps）。
  double EstimateEntryEdgeBps(const MarketDecision& decision,
                              const MarketEvent& event) const;
  /// 费率感知开仓过滤（仅 Entry 生效）。
  bool ShouldFilterByFeeAwareGate(const MarketDecision& decision,
                                  const MarketEvent& event,
                                  double* out_expected_edge_bps,
                                  double* out_required_edge_bps,
                                  double* out_base_required_edge_bps,
                                  double* out_adaptive_relax_bps,
                                  double* out_maker_relax_bps,
                                  double* out_quality_guard_penalty_bps,
                                  double* out_observed_filtered_ratio) const;
  /// 开仓成本门冷却是否生效（用于避免连续重复无效入场）。
  bool IsCostFilterCooldownActive(const std::string& symbol,
                                  int* out_remaining_ticks);
  /// 记录一次成本门拦截，必要时触发 symbol 级冷却。
  void OnCostFilterRejected(const std::string& symbol);
  /// 记录一次开仓候选通过（清理连续拦截计数）。
  void OnCostFilterAccepted(const std::string& symbol);
  /// 更新成本门观测比例（全局），供自适应门槛使用。
  void UpdateEntryGateObservedRatio(bool filtered);
  /// 执行质量守卫：根据窗口成交质量动态启停开仓惩罚。
  void EvaluateExecutionQualityGuard(std::uint64_t window_fills,
                                     double window_realized_net_per_fill_usd,
                                     double window_fee_bps_per_fill);
  /// 对账异常保护：连续异常触发 reduce-only / halt，自恢复窗口退出。
  void UpdateReconcileAnomalyProtection(bool anomaly_detected,
                                        const std::string& reason_code);

  // --- 辅助逻辑 ---

  /**
   * @brief 将订单意图加入执行队列
   * 包含：WAL 持久化、OMS 注册、推送到异步执行器。
   */
  bool EnqueueIntent(const OrderIntent& intent);

  /**
   * @brief 处理成交后的保护单逻辑 (止盈/止损)
   * 针对开仓成交，挂出 SL/TP；针对保护单成交，撤销对侧单 (OCO)。
   */
  void HandleProtectionOrders(const FillEvent& fill);
  /// 检查“required SL 挂单确认”是否超时，超时则触发强制只减仓并审计。
  void CheckPendingRequiredSlTimeouts();
  /// 注册 required SL 的确认等待项（key=SL client_order_id）。
  void TrackPendingRequiredSl(const std::string& sl_client_order_id,
                              const std::string& parent_order_id);
  /// 清理 required SL 的确认等待项（已确认/失败/成交后调用）。
  void ClearPendingRequiredSl(const std::string& sl_client_order_id);

  /**
   * @brief 执行定期对账
   * 比较本地 OMS 状态与交易所远端状态，发现不一致时触发报警或熔断。
   */
  void RunReconcile();

  /**
   * @brief 周期刷新远端风险字段
   * 定期拉取远端持仓并刷新本地 `liqPrice/mark` 风险视图，避免强平距离陈旧。
   */
  void RunRemoteRiskRefresh();

  /**
   * @brief 执行 Gate 监控检查
   * 检查策略活跃度是否满足最小要求，并按配置驱动运行时动作。
   */
  void RunGateMonitor();

  /**
   * @brief 周期输出运行状态摘要
   * 用于线上健康巡检与问题回放定位。
   */
  void LogStatus();

  /**
   * @brief 周期执行自进化权重更新/回滚
   *
   * 仅触发组合层权重变化，不触碰风控不可动层参数。
   */
  void RunSelfEvolution();

  /**
   * @brief 判断主循环是否应退出
   * @param has_market 当前轮是否消费到行情
   * @param has_fill 当前轮是否消费到成交
   */
  bool ShouldExit(bool has_market, bool has_fill);

  /**
   * @brief 停机清理
   * 停止执行线程并输出结束日志。
   */
  void Shutdown();

  /// 订单漏斗统计（用于运行态可观测闭环）。
  struct DecisionFunnelStats {
    std::uint64_t raw_signals{0};
    std::uint64_t risk_adjusted_signals{0};
    std::uint64_t intents_generated{0};
    std::uint64_t intents_filtered_inactive_symbol{0};
    std::uint64_t intents_filtered_min_notional{0};
    std::uint64_t intents_filtered_fee_aware{0};
    std::uint64_t intents_throttled_cost_cooldown{0};
    std::uint64_t intents_throttled{0};
    std::uint64_t intents_enqueued{0};
    std::uint64_t async_submit_ok{0};
    std::uint64_t async_submit_failed{0};
    std::uint64_t fills_applied{0};
    std::uint64_t gate_alerts{0};
    std::uint64_t self_evolution_updates{0};
    std::uint64_t self_evolution_rollbacks{0};
    std::uint64_t self_evolution_skipped{0};
    std::uint64_t regime_trend_ticks{0};
    std::uint64_t regime_range_ticks{0};
    std::uint64_t regime_extreme_ticks{0};
    std::uint64_t regime_warmup_ticks{0};
    std::uint64_t integrator_scored{0};
    std::uint64_t integrator_pred_up{0};
    std::uint64_t integrator_pred_down{0};
    std::uint64_t integrator_policy_applied{0};
    std::uint64_t integrator_policy_canary{0};
    std::uint64_t integrator_policy_active{0};
    std::uint64_t entry_edge_samples{0};
    std::uint64_t strategy_mix_samples{0};
    double integrator_model_score_sum{0.0};
    double integrator_p_up_sum{0.0};
    double integrator_p_down_sum{0.0};
    double entry_edge_bps_sum{0.0};
    double entry_base_required_edge_bps_sum{0.0};
    double entry_required_edge_bps_sum{0.0};
    double entry_adaptive_relax_bps_sum{0.0};
    double entry_maker_relax_bps_sum{0.0};
    double entry_quality_guard_penalty_bps_sum{0.0};
    double trend_notional_abs_sum{0.0};
    double defensive_notional_abs_sum{0.0};
    double blended_notional_abs_sum{0.0};
    double fills_notional_abs_usd_sum{0.0};
    std::uint64_t fills_maker_count{0};
    std::uint64_t fills_taker_count{0};
    std::uint64_t fills_unknown_liquidity_count{0};
    std::uint64_t fills_explicit_liquidity_count{0};
    std::uint64_t fills_fee_sign_fallback_count{0};
    double fills_maker_fee_usd_sum{0.0};
    double fills_taker_fee_usd_sum{0.0};
    double fills_maker_notional_abs_usd_sum{0.0};
    double fills_taker_notional_abs_usd_sum{0.0};
  };

  static void AccumulateStats(DecisionFunnelStats* total,
                              const DecisionFunnelStats& delta);

  /// 当前是否存在任一“强制只减仓”来源（保护单失败、Gate 或对账异常保护）。
  bool IsForceReduceOnlyActive() const;
  /// 将强制只减仓合并态同步到风控引擎。
  void RefreshReduceOnlyMode();
  /// 将多个停机来源（Reconcile/Gate）合并为统一交易停机开关。
  void RefreshTradingHaltState();
  /// 每个行情 tick 驱动 Gate 运行时动作冷却计时。
  void TickGateRuntimeCooldown();

  // --- 成员变量 ---
  AppConfig config_;  ///< 应用级配置快照。
  TradeSystem system_;  ///< 策略->风控->执行决策流水线。
  ExecutionEngine execution_;  ///< 保护单意图生成等执行辅助逻辑。
  OrderThrottle order_throttle_;  ///< 下单节流器（最小间隔/反向冷却）。
  SelfEvolutionController self_evolution_;  ///< 阶段2自进化控制器（权重更新/回滚）。
  OrderManager oms_;  ///< 订单状态机与成交累计。
  Reconciler reconciler_;  ///< 本地/远端对账器。
  GateMonitor gate_monitor_;  ///< 活跃度门禁统计器。
  UniverseSelector universe_selector_;  ///< 多币种筛选器。
  WalStore wal_;  ///< WAL 持久化组件。

  std::unique_ptr<ExchangeAdapter> adapter_;  ///< 交易所适配器实例。
  std::unique_ptr<AsyncExecutor> executor_;  ///< 异步执行器（单线程串行发单）。

  // 状态追踪
  std::unordered_set<std::string> intent_ids_; ///< 已处理的订单 ID (去重)
  std::unordered_set<std::string> fill_ids_;   ///< 已处理的成交 ID (去重)
  std::vector<std::string> tracked_symbols_;   ///< 当前关注的 Symbol 列表
  // 仅跟踪“净仓位相关订单（Entry/Reduce）”的入队时间，用于超时收敛。
  std::unordered_map<std::string, std::int64_t> pending_net_order_enqueued_ms_;
  struct PendingRequiredSlAttach {
    std::string parent_order_id;
    std::int64_t deadline_ms{0};
  };
  std::unordered_map<std::string, PendingRequiredSlAttach>
      pending_required_sl_attach_;
  std::unordered_map<std::string, int> cost_filter_reject_streak_by_symbol_;
  std::unordered_map<std::string, int> cost_filter_cooldown_until_tick_by_symbol_;
  std::uint64_t entry_gate_observed_samples_{0};
  std::uint64_t entry_gate_observed_filtered_{0};
  double entry_gate_observed_filtered_ratio_{0.0};

  bool protection_forced_reduce_only_{
      false};  ///< 保护单关键路径触发的只减仓开关（高优先级，需人工介入恢复）。
  bool gate_forced_reduce_only_{
      false};  ///< Gate 运行时动作触发的只减仓开关（可自动恢复）。
  bool reconcile_forced_reduce_only_{
      false};  ///< 对账异常连续触发的只减仓保护（可在健康窗口自动恢复）。
  bool reconcile_halted_{
      false};  ///< 对账确认失败触发的停机开关（权威兜底，不自动恢复）。
  bool gate_halted_{false};  ///< Gate 运行时动作触发的停机开关（可自动恢复）。
  bool trading_halted_{false};  ///< 聚合停机状态（reconcile_halted_ || gate_halted_）。

  int gate_fail_windows_streak_{0};  ///< Gate 连续失败窗口计数。
  int gate_pass_windows_streak_{0};  ///< Gate 连续通过窗口计数。
  int gate_reduce_only_cooldown_ticks_left_{
      0};  ///< Gate 只减仓冷却剩余 tick（0 表示已结束）。
  int gate_halt_cooldown_ticks_left_{
      0};  ///< Gate 停机冷却剩余 tick（0 表示已结束）。
  int gate_flat_ticks_streak_{0};  ///< 账户空仓且无净仓位在途订单的连续 tick 计数。

  int market_tick_count_{0};       ///< 接收到的行情 tick 计数
  int last_fill_tick_{-1000000};   ///< 最近一次成交处理时的行情 tick（用于对账短暂宽限）
  int last_auto_resync_tick_{-1000000};  ///< 最近一次远端权威重对齐 tick（防抖）。
  int pending_fills_for_evolution_{0};  ///< 待注入自进化的成交样本计数。
  int execution_quality_bad_streak_{0};  ///< 执行质量连续劣化窗口计数。
  int execution_quality_good_streak_{0};  ///< 执行质量连续恢复窗口计数。
  bool execution_quality_guard_active_{false};  ///< 执行质量守卫是否处于激活态。
  double execution_quality_required_edge_penalty_bps_{
      0.0};  ///< 执行质量守卫施加的额外入场边际门槛。
  int reconcile_anomaly_streak_{0};  ///< 连续对账异常窗口计数。
  int reconcile_healthy_streak_{0};  ///< 对账连续健康窗口计数。
  int reconcile_tick_{0};          ///< 对账定时器计数
  int reconcile_streak_{0};        ///< 连续对账失败次数
  DecisionFunnelStats funnel_total_;  ///< 进程累计漏斗统计。
  DecisionFunnelStats funnel_window_;  ///< 日志窗口漏斗统计（周期清零）。
  RegimeState last_regime_state_;  ///< 最近一笔行情对应的 Regime 状态。
  bool has_last_regime_state_{false};  ///< 是否已有 Regime 状态可展示。
  Signal last_strategy_signal_;  ///< 最近一次“策略+自进化混合后”的基础信号。
  bool has_last_strategy_signal_{false};  ///< 是否已有基础信号可展示。
  double latest_mark_price_usd_{0.0};  ///< 最近一笔行情的估值价格（mark 优先）。
  bool has_latest_mark_price_{false};  ///< 是否已有可用估值价格。
  double tick_trend_notional_usd_{0.0};  ///< 当前 tick 的趋势分支目标名义值。
  double tick_defensive_notional_usd_{0.0};  ///< 当前 tick 的防御分支目标名义值。
  std::string tick_strategy_signal_symbol_;  ///< 当前 tick 的策略信号 symbol。
  bool has_tick_strategy_signal_{false};  ///< 当前 tick 是否已有新鲜策略分支信号。
  bool tick_cost_filtered_signal_{false};  ///< 当前 tick 是否触发了成本门过滤（供进化统计）。
  ShadowInference last_shadow_inference_;  ///< 最近一次影子推理结果。
  bool has_last_shadow_inference_{false};  ///< 是否已有影子推理结果可展示。
  bool has_last_status_account_snapshot_{false};
  double last_status_realized_net_pnl_usd_{0.0};
  double last_status_fee_usd_{0.0};
};

}  // namespace ai_trade
