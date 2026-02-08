#pragma once

#include <string>
#include <vector>

#include "core/types.h"

namespace ai_trade {

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
};

struct GateConfig {
  int min_effective_signals_per_window{24};
  int min_fills_per_window{4};
  int heartbeat_empty_signal_ticks{12};
  int window_ticks{288};
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
  int execution_poll_limit{50};
};

struct UniverseConfig {
  bool enabled{true};
  int update_interval_ticks{20};
  int max_active_symbols{3};
  int min_active_symbols{1};
  std::vector<std::string> fallback_symbols{"BTCUSDT"};
  std::vector<std::string> candidate_symbols{"BTCUSDT", "ETHUSDT", "SOLUSDT"};
};

struct AppConfig {
  std::string mode{"replay"};
  std::string primary_symbol{"BTCUSDT"};
  int system_max_ticks{0};
  int system_status_log_interval_ticks{20};
  double risk_max_abs_notional_usd{3000.0};
  RiskThresholds risk_thresholds{};
  double execution_max_order_notional{1000.0};
  int execution_min_order_interval_ms{0};
  int execution_reverse_signal_cooldown_ticks{0};
  std::string exchange{"mock"};
  std::string data_path{"data"};
  ProtectionConfig protection{};
  ReconcileConfig reconcile{};
  GateConfig gate{};
  BybitConfig bybit{};
  UniverseConfig universe{};
};

// 轻量 YAML 加载器：仅解析当前骨架运行所需关键字段。
// 解析失败时返回 false，并在 out_error 中写入原因。
bool LoadAppConfigFromYaml(const std::string& file_path,
                           AppConfig* out_config,
                           std::string* out_error);

}  // namespace ai_trade
