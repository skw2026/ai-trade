#pragma once

#include <string>
#include <vector>

#include "core/types.h"

namespace ai_trade {

/// 保护单参数：用于入场后自动挂 SL/TP。
struct ProtectionConfig {
  bool enabled{false};
  bool require_sl{true};
  bool enable_tp{true};
  int attach_timeout_ms{1500};
  double stop_loss_ratio{0.01};
  double take_profit_ratio{0.015};
};

/// 本地对账参数：控制频率、容差、确认次数。
struct ReconcileConfig {
  bool enabled{true};
  int interval_ticks{20};
  double tolerance_notional_usd{5.0};
  int mismatch_confirmations{2};
  int pending_order_stale_ms{30000};
};

/// Gate 活跃度门禁：用于检测“有信号但不交易”等异常状态。
struct GateConfig {
  int min_effective_signals_per_window{24};
  int min_fills_per_window{4};
  int heartbeat_empty_signal_ticks{12};
  int window_ticks{288};
  bool enforce_runtime_actions{false};
  int fail_to_reduce_only_windows{0};
  int fail_to_halt_windows{0};
  int reduce_only_cooldown_ticks{0};
  int halt_cooldown_ticks{0};
  int pass_to_resume_windows{1};
  bool auto_resume_when_flat{true};
  int auto_resume_flat_ticks{120};
};

/// Bybit 接入参数与账户模式约束。
struct BybitConfig {
  bool testnet{true};
  bool demo_trading{false};
  std::string category{"linear"};
  std::string account_type{"UNIFIED"};
  AccountMode expected_account_mode{AccountMode::kUnified};
  MarginMode expected_margin_mode{MarginMode::kIsolated};
  PositionMode expected_position_mode{PositionMode::kOneWay};
  bool public_ws_enabled{true};
  bool public_ws_rest_fallback{true};
  bool private_ws_enabled{true};
  bool private_ws_rest_fallback{true};
  int ws_reconnect_interval_ms{15000};
  int execution_poll_limit{50};
};

/// Universe 筛币参数：控制刷新频率与激活币对数量范围。
struct UniverseConfig {
  bool enabled{true};
  int update_interval_ticks{20};
  int max_active_symbols{3};
  int min_active_symbols{1};
  double min_turnover_usd{0.0};  ///< 最小 24h 成交额过滤 (USD)
  std::vector<std::string> fallback_symbols{"BTCUSDT"};
  std::vector<std::string> candidate_symbols{"BTCUSDT", "ETHUSDT", "SOLUSDT"};
};

/// 阶段2最小自进化配置：仅做权重约束更新与性能回滚。
struct SelfEvolutionConfig {
  bool enabled{false};
  int update_interval_ticks{60};
  int min_update_interval_ticks{120};
  double min_abs_window_pnl_usd{0.0};
  int min_bucket_ticks_for_update{0};
  // 是否使用“虚拟PnL”作为学习目标（替代已实现净盈亏）。
  bool use_virtual_pnl{false};
  // 是否在评估窗口内基于反事实网格直接选择更优权重。
  bool use_counterfactual_search{false};
  // 反事实候选优于当前权重所需的最小虚拟PnL改进（USD）。
  double counterfactual_min_improvement_usd{0.0};
  // 虚拟PnL成本估计（按名义值变化收取，单位 bps）。
  double virtual_cost_bps{0.0};
  // 是否按“在线因子 IC”自适应调整 trend/defensive 权重。
  bool enable_factor_ic_adaptive_weights{false};
  // 因子 IC 样本数门槛（低于该值不调权）。
  int factor_ic_min_samples{120};
  // 因子 IC 绝对值门槛（低于该值视为无有效预测力）。
  double factor_ic_min_abs{0.01};
  // 是否启用“可学习性门控”（低信噪窗口冻结学习）。
  bool enable_learnability_gate{false};
  // 可学习性最小样本门槛（基于窗口内逐tick收益贡献样本）。
  int learnability_min_samples{120};
  // 可学习性门槛：|t-stat| 低于该值则跳过学习。
  double learnability_min_t_stat_abs{1.5};
  // 目标函数权重：score = alpha*pnl - beta*drawdown_bps - gamma*notional_churn_usd
  double objective_alpha_pnl{1.0};
  double objective_beta_drawdown{0.0};
  double objective_gamma_notional_churn{0.0};
  double max_single_strategy_weight{0.60};
  double max_weight_step{0.05};
  int rollback_degrade_windows{2};
  // 回滚判定阈值（目标函数分数口径）
  double rollback_degrade_threshold_score{0.0};
  int rollback_cooldown_ticks{240};
  double initial_trend_weight{0.50};
  double initial_defensive_weight{0.50};
};

/// Integrator 影子推理配置：用于运行态可观测（不直接下单）。
struct IntegratorShadowConfig {
  bool enabled{false};
  bool log_model_score{true};
  std::string model_report_path{"./data/research/integrator_report.json"};
  // 线上激活模型文件路径（用于运行时存在性校验）。
  std::string model_path{"./data/models/integrator_latest.cbm"};
  // 线上激活元信息路径（由 model_registry.py 产出）。
  std::string active_meta_path{"./data/models/integrator_active.json"};
  // 是否要求模型文件必须存在且非空。
  bool require_model_file{false};
  // 是否要求 active_meta 必须存在（canary/active 会强制启用）。
  bool require_active_meta{false};
  // 是否要求治理门槛通过（canary/active 会强制启用）。
  bool require_gate_pass{false};
  // 治理阈值：最小 AUC 均值。
  double min_auc_mean{0.50};
  // 治理阈值：最小 Delta AUC（相对 baseline）。
  double min_delta_auc_vs_baseline{0.0};
  // 治理阈值：最少完成训练的 split 数。
  int min_split_trained_count{1};
  // 治理阈值：完成训练 split 比例下限（0~1）。
  double min_split_trained_ratio{0.5};
  double score_gain{1.0};
  // 特征计算窗口长度（tick），需覆盖模型中最长因子的 lookback。
  int feature_window_ticks{300};
};

/// Integrator 接管模式：从纯观测到实际接管逐级放量。
enum class IntegratorMode {
  kOff,     // 完全关闭 Integrator，对策略输出不做任何影响。
  kShadow,  // 仅观测打分，不改变策略输出（默认）。
  kCanary,  // 小流量接管：仅在高置信时按比例覆盖策略输出。
  kActive,  // 主接管：以 Integrator 置信度驱动方向与仓位比例。
};

/// IntegratorMode 文本化（用于日志展示）。
inline const char* ToString(IntegratorMode mode) {
  switch (mode) {
    case IntegratorMode::kOff:
      return "off";
    case IntegratorMode::kShadow:
      return "shadow";
    case IntegratorMode::kCanary:
      return "canary";
    case IntegratorMode::kActive:
      return "active";
  }
  return "unknown";
}

/// Integrator 模块配置：当前以影子模式为主。
struct IntegratorConfig {
  bool enabled{false};
  std::string model_type{"catboost"};
  IntegratorMode mode{IntegratorMode::kShadow};
  // canary 模式：最终名义值上限比例（相对原策略绝对名义值）。
  double canary_notional_ratio{0.30};
  // canary 模式：最低置信阈值（|p_up-p_down|），低于阈值不接管。
  double canary_confidence_threshold{0.60};
  // canary 是否允许与原策略反向（默认不允许，优先保守）。
  bool canary_allow_countertrend{false};
  // active 模式：最低置信阈值（|p_up-p_down|），低于阈值转空仓。
  double active_confidence_threshold{0.55};
  IntegratorShadowConfig shadow{};
};

/// Regime 识别配置：用于趋势/震荡/极端状态的轻量判定。
struct RegimeConfig {
  bool enabled{true};
  int warmup_ticks{20};
  double ewma_alpha{0.20};
  double trend_threshold{0.0008};
  double extreme_threshold{0.0030};
  double volatility_threshold{0.0018};
};

/// 应用主配置：聚合策略、风控、执行、交易所和系统运行参数。
struct AppConfig {
  std::string mode{"replay"};
  std::string primary_symbol{"BTCUSDT"};
  int system_max_ticks{0};
  int system_status_log_interval_ticks{20};
  int system_remote_risk_refresh_interval_ticks{20};
  double strategy_signal_notional_usd{1000.0};
  double strategy_signal_deadband_abs{0.1};
  int strategy_min_hold_ticks{0};
  // 趋势策略参数
  int trend_ema_fast{12};
  int trend_ema_slow{26};
  double vol_target_pct{0.40}; // 年化波动率目标 (e.g. 40%)
  // 防御策略参数（均值回归分支）
  double strategy_defensive_notional_ratio{0.0};
  double strategy_defensive_entry_score{1.25};
  double strategy_defensive_trend_scale{0.35};
  double strategy_defensive_range_scale{1.00};
  double strategy_defensive_extreme_scale{0.55};
  double risk_max_abs_notional_usd{3000.0};
  RiskThresholds risk_thresholds{};
  double execution_max_order_notional{1000.0};
  double execution_min_rebalance_notional_usd{0.0};
  int execution_min_order_interval_ms{0};
  int execution_reverse_signal_cooldown_ticks{0};
  bool execution_enable_fee_aware_entry_gate{true};
  double execution_entry_fee_bps{5.5};
  double execution_exit_fee_bps{5.5};
  double execution_expected_slippage_bps{1.0};
  double execution_min_expected_edge_bps{1.0};
  double execution_required_edge_cap_bps{
      0.0};  // 费率门槛上限（0=关闭，不限制）
  bool execution_maker_entry_enabled{false};
  double execution_maker_price_offset_bps{1.0};
  bool execution_maker_post_only{true};
  std::string exchange{"mock"};
  std::string data_path{"data"};
  ProtectionConfig protection{};
  ReconcileConfig reconcile{};
  GateConfig gate{};
  BybitConfig bybit{};
  UniverseConfig universe{};
  IntegratorConfig integrator{};
  SelfEvolutionConfig self_evolution{};
  RegimeConfig regime{};
};

/**
 * @brief 轻量 YAML 配置加载器
 *
 * 仅解析当前项目运行所需关键字段；解析失败返回 `false` 并写入 `out_error`。
 */
bool LoadAppConfigFromYaml(const std::string& file_path,
                           AppConfig* out_config,
                           std::string* out_error);

}  // namespace ai_trade
