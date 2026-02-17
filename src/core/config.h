#pragma once

#include <string>
#include <vector>
#include "core/types.h"

namespace ai_trade {

// ============================================================================
// Sub-Configurations
// ============================================================================

struct ProtectionConfig {
  bool enabled{false};
  bool require_sl{true};
  bool enable_tp{true};
  int attach_timeout_ms{1500};
  double stop_loss_ratio{0.01};
  double take_profit_ratio{0.015};
};

struct ReconcileConfig {
  bool enabled{true};
  int interval_ticks{20};
  double tolerance_notional_usd{5.0};
  int mismatch_confirmations{2};
  int pending_order_stale_ms{30000};
  int anomaly_reduce_only_streak{0};
  int anomaly_halt_streak{0};
  int anomaly_resume_streak{3};
};

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

struct UniverseConfig {
  bool enabled{true};
  int update_interval_ticks{20};
  int max_active_symbols{3};
  int min_active_symbols{1};
  double min_turnover_usd{0.0};
  std::vector<std::string> fallback_symbols{"BTCUSDT"};
  std::vector<std::string> candidate_symbols{"BTCUSDT", "ETHUSDT", "SOLUSDT"};
};

struct SelfEvolutionConfig {
  bool enabled{false};
  int update_interval_ticks{60};
  int min_update_interval_ticks{120};
  double min_abs_window_pnl_usd{0.0};
  int min_bucket_ticks_for_update{0};
  bool use_virtual_pnl{false};
  bool use_counterfactual_search{false};
  double counterfactual_min_improvement_usd{0.0};
  double counterfactual_improvement_decay_per_filtered_signal_usd{0.0};
  int counterfactual_min_fill_count_for_update{0};
  int counterfactual_min_t_stat_samples_for_update{0};
  double counterfactual_min_t_stat_abs_for_update{0.0};
  double virtual_cost_bps{0.0};
  bool enable_factor_ic_adaptive_weights{false};
  int factor_ic_min_samples{120};
  double factor_ic_min_abs{0.01};
  bool enable_learnability_gate{false};
  int learnability_min_samples{120};
  double learnability_min_t_stat_abs{1.5};
  double objective_alpha_pnl{1.0};
  double objective_beta_drawdown{0.0};
  double objective_gamma_notional_churn{0.0};
  double max_single_strategy_weight{0.60};
  double max_weight_step{0.05};
  int rollback_degrade_windows{2};
  double rollback_degrade_threshold_score{0.0};
  int rollback_cooldown_ticks{240};
  double initial_trend_weight{0.50};
  double initial_defensive_weight{0.50};
};

struct IntegratorShadowConfig {
  bool enabled{false};
  bool log_model_score{true};
  std::string model_report_path{"./data/research/integrator_report.json"};
  std::string model_path{"./data/models/integrator_latest.cbm"};
  std::string active_meta_path{"./data/models/integrator_active.json"};
  bool require_model_file{false};
  bool require_active_meta{false};
  bool require_gate_pass{false};
  double min_auc_mean{0.50};
  double min_delta_auc_vs_baseline{0.0};
  int min_split_trained_count{1};
  double min_split_trained_ratio{0.5};
  double score_gain{1.0};
  int feature_window_ticks{300};
};

enum class IntegratorMode { kOff, kShadow, kCanary, kActive };

inline const char* ToString(IntegratorMode mode) {
  switch (mode) {
    case IntegratorMode::kOff: return "off";
    case IntegratorMode::kShadow: return "shadow";
    case IntegratorMode::kCanary: return "canary";
    case IntegratorMode::kActive: return "active";
  }
  return "unknown";
}

struct IntegratorConfig {
  bool enabled{false};
  std::string model_type{"catboost"};
  IntegratorMode mode{IntegratorMode::kShadow};
  double canary_notional_ratio{0.30};
  double canary_confidence_threshold{0.60};
  bool canary_allow_countertrend{false};
  double active_confidence_threshold{0.55};
  IntegratorShadowConfig shadow{};
};

struct RegimeConfig {
  bool enabled{true};
  int warmup_ticks{20};
  double ewma_alpha{0.20};
  double trend_threshold{0.0008};
  double extreme_threshold{0.0030};
  double volatility_threshold{0.0018};
};

struct StrategyConfig {
  double signal_notional_usd{1000.0};
  double signal_deadband_abs{0.1};
  int min_hold_ticks{0};
  int trend_ema_fast{12};
  int trend_ema_slow{26};
  double vol_target_pct{0.40};
  double defensive_notional_ratio{0.0};
  double defensive_entry_score{1.25};
  double defensive_trend_scale{0.35};
  double defensive_range_scale{1.00};
  double defensive_extreme_scale{0.55};
};

struct ExecutionConfig {
  double max_order_notional_usd{1000.0};
  double min_rebalance_notional_usd{0.0};
  int min_order_interval_ms{0};
  int reverse_signal_cooldown_ticks{0};
  bool enable_fee_aware_entry_gate{true};
  double entry_fee_bps{5.5};
  double exit_fee_bps{5.5};
  double expected_slippage_bps{1.0};
  double min_expected_edge_bps{1.0};
  double required_edge_cap_bps{0.0};
  bool adaptive_fee_gate_enabled{true};
  int adaptive_fee_gate_min_samples{120};
  double adaptive_fee_gate_trigger_ratio{0.75};
  double adaptive_fee_gate_max_relax_bps{2.0};
  bool maker_entry_enabled{false};
  bool maker_fallback_to_market{true};
  double maker_price_offset_bps{1.0};
  bool maker_post_only{true};
  double maker_edge_relax_bps{0.0};
  int cost_filter_cooldown_trigger_count{0};
  int cost_filter_cooldown_ticks{0};
  bool quality_guard_enabled{false};
  int quality_guard_min_fills{12};
  int quality_guard_bad_streak_to_trigger{2};
  int quality_guard_good_streak_to_release{2};
  double quality_guard_min_realized_net_per_fill_usd{-0.005};
  double quality_guard_max_fee_bps_per_fill{8.0};
  double quality_guard_required_edge_penalty_bps{1.5};
};

// ============================================================================
// Main Application Config
// ============================================================================

struct AppConfig {
  std::string mode{"replay"};
  std::string primary_symbol{"BTCUSDT"};
  int system_max_ticks{0};
  int system_status_log_interval_ticks{20};
  int system_remote_risk_refresh_interval_ticks{20};
  
  // Flattened Strategy Config (for YAML compatibility)
  double strategy_signal_notional_usd{1000.0};
  double strategy_signal_deadband_abs{0.1};
  int strategy_min_hold_ticks{0};
  int trend_ema_fast{12};
  int trend_ema_slow{26};
  double vol_target_pct{0.40};
  double strategy_defensive_notional_ratio{0.0};
  double strategy_defensive_entry_score{1.25};
  double strategy_defensive_trend_scale{0.35};
  double strategy_defensive_range_scale{1.00};
  double strategy_defensive_extreme_scale{0.55};

  // Risk
  double risk_max_abs_notional_usd{3000.0};
  RiskThresholds risk_thresholds{};

  // Execution
  double execution_max_order_notional{1000.0};
  double execution_min_rebalance_notional_usd{0.0};
  int execution_min_order_interval_ms{0};
  int execution_reverse_signal_cooldown_ticks{0};
  bool execution_enable_fee_aware_entry_gate{true};
  double execution_entry_fee_bps{5.5};
  double execution_exit_fee_bps{5.5};
  double execution_expected_slippage_bps{1.0};
  double execution_min_expected_edge_bps{1.0};
  double execution_required_edge_cap_bps{0.0};
  bool execution_adaptive_fee_gate_enabled{true};
  int execution_adaptive_fee_gate_min_samples{120};
  double execution_adaptive_fee_gate_trigger_ratio{0.75};
  double execution_adaptive_fee_gate_max_relax_bps{2.0};
  bool execution_maker_entry_enabled{false};
  bool execution_maker_fallback_to_market{true};
  double execution_maker_price_offset_bps{1.0};
  bool execution_maker_post_only{true};
  double execution_maker_edge_relax_bps{0.0};
  int execution_cost_filter_cooldown_trigger_count{0};
  int execution_cost_filter_cooldown_ticks{0};
  bool execution_quality_guard_enabled{false};
  int execution_quality_guard_min_fills{12};
  int execution_quality_guard_bad_streak_to_trigger{2};
  int execution_quality_guard_good_streak_to_release{2};
  double execution_quality_guard_min_realized_net_per_fill_usd{-0.005};
  double execution_quality_guard_max_fee_bps_per_fill{8.0};
  double execution_quality_guard_required_edge_penalty_bps{1.5};

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

  // Helper to extract StrategyConfig
  StrategyConfig GetStrategyConfig() const {
    return StrategyConfig{
        strategy_signal_notional_usd,
        strategy_signal_deadband_abs,
        strategy_min_hold_ticks,
        trend_ema_fast,
        trend_ema_slow,
        vol_target_pct,
        strategy_defensive_notional_ratio,
        strategy_defensive_entry_score,
        strategy_defensive_trend_scale,
        strategy_defensive_range_scale,
        strategy_defensive_extreme_scale
    };
  }

  // Helper to extract ExecutionConfig
  ExecutionConfig GetExecutionConfig() const {
    return ExecutionConfig{
        execution_max_order_notional,
        execution_min_rebalance_notional_usd,
        execution_min_order_interval_ms,
        execution_reverse_signal_cooldown_ticks,
        execution_enable_fee_aware_entry_gate,
        execution_entry_fee_bps,
        execution_exit_fee_bps,
        execution_expected_slippage_bps,
        execution_min_expected_edge_bps,
        execution_required_edge_cap_bps,
        execution_adaptive_fee_gate_enabled,
        execution_adaptive_fee_gate_min_samples,
        execution_adaptive_fee_gate_trigger_ratio,
        execution_adaptive_fee_gate_max_relax_bps,
        execution_maker_entry_enabled,
        execution_maker_fallback_to_market,
        execution_maker_price_offset_bps,
        execution_maker_post_only,
        execution_maker_edge_relax_bps,
        execution_cost_filter_cooldown_trigger_count,
        execution_cost_filter_cooldown_ticks,
        execution_quality_guard_enabled,
        execution_quality_guard_min_fills,
        execution_quality_guard_bad_streak_to_trigger,
        execution_quality_guard_good_streak_to_release,
        execution_quality_guard_min_realized_net_per_fill_usd,
        execution_quality_guard_max_fee_bps_per_fill,
        execution_quality_guard_required_edge_penalty_bps
    };
  }
};

bool LoadAppConfigFromYaml(const std::string& file_path,
                           AppConfig* out_config,
                           std::string* out_error);

}  // namespace ai_trade
