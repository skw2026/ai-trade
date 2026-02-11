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
    double integrator_model_score_sum{0.0};
    double integrator_p_up_sum{0.0};
    double integrator_p_down_sum{0.0};
  };

  static void AccumulateStats(DecisionFunnelStats* total,
                              const DecisionFunnelStats& delta);

  /// 当前是否存在任一“强制只减仓”来源（保护单失败或 Gate 运行时动作）。
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

  bool protection_forced_reduce_only_{
      false};  ///< 保护单关键路径触发的只减仓开关（高优先级，需人工介入恢复）。
  bool gate_forced_reduce_only_{
      false};  ///< Gate 运行时动作触发的只减仓开关（可自动恢复）。
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

  int market_tick_count_{0};       ///< 接收到的行情 tick 计数
  int last_fill_tick_{-1000000};   ///< 最近一次成交处理时的行情 tick（用于对账短暂宽限）
  int last_auto_resync_tick_{-1000000};  ///< 最近一次远端权威重对齐 tick（防抖）。
  int reconcile_tick_{0};          ///< 对账定时器计数
  int reconcile_streak_{0};        ///< 连续对账失败次数
  DecisionFunnelStats funnel_total_;  ///< 进程累计漏斗统计。
  DecisionFunnelStats funnel_window_;  ///< 日志窗口漏斗统计（周期清零）。
  RegimeState last_regime_state_;  ///< 最近一笔行情对应的 Regime 状态。
  bool has_last_regime_state_{false};  ///< 是否已有 Regime 状态可展示。
  ShadowInference last_shadow_inference_;  ///< 最近一次影子推理结果。
  bool has_last_shadow_inference_{false};  ///< 是否已有影子推理结果可展示。
};

}  // namespace ai_trade
