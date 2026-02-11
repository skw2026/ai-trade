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
  double score_gain{1.0};
};

/// Integrator 模块配置：当前以影子模式为主。
struct IntegratorConfig {
  bool enabled{false};
  std::string model_type{"catboost"};
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
  double risk_max_abs_notional_usd{3000.0};
  RiskThresholds risk_thresholds{};
  double execution_max_order_notional{1000.0};
  double execution_min_rebalance_notional_usd{0.0};
  int execution_min_order_interval_ms{0};
  int execution_reverse_signal_cooldown_ticks{0};
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
