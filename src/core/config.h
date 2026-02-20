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
        execution_expected_slippage_bps
    };
  }
};

bool LoadAppConfigFromYaml(const std::string& file_path,
                           AppConfig* out_config,
                           std::string* out_error);

}  // namespace ai_trade
