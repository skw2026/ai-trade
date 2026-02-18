#include "core/config.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <fstream>
#include <sstream>

namespace ai_trade {

namespace {

// 以下工具函数用于“轻量 YAML 解析”：
// - 通过缩进和键路径识别结构；
// - 仅覆盖当前项目使用到的配置字段。
std::string Trim(const std::string& text) {
  std::size_t begin = 0;
  while (begin < text.size() &&
         std::isspace(static_cast<unsigned char>(text[begin])) != 0) {
    ++begin;
  }

  std::size_t end = text.size();
  while (end > begin &&
         std::isspace(static_cast<unsigned char>(text[end - 1])) != 0) {
    --end;
  }
  return text.substr(begin, end - begin);
}

std::string StripInlineComment(const std::string& line) {
  // 仅剔除非引号上下文中的 `#` 注释，避免误伤字符串内容。
  bool in_single_quotes = false;
  bool in_double_quotes = false;
  for (std::size_t i = 0; i < line.size(); ++i) {
    const char ch = line[i];
    if (ch == '\'' && !in_double_quotes) {
      in_single_quotes = !in_single_quotes;
      continue;
    }
    if (ch == '"' && !in_single_quotes) {
      in_double_quotes = !in_double_quotes;
      continue;
    }
    if (ch == '#' && !in_single_quotes && !in_double_quotes) {
      return line.substr(0, i);
    }
  }
  return line;
}

std::string Unquote(const std::string& text) {
  if (text.size() < 2) {
    return text;
  }
  const bool single_quoted = text.front() == '\'' && text.back() == '\'';
  const bool double_quoted = text.front() == '"' && text.back() == '"';
  if (single_quoted || double_quoted) {
    return text.substr(1, text.size() - 2);
  }
  return text;
}

bool ParseDouble(const std::string& text, double* out_value) {
  if (out_value == nullptr) {
    return false;
  }
  std::istringstream iss(text);
  double value = 0.0;
  iss >> value;
  if (!iss.fail() && iss.eof()) {
    *out_value = value;
    return true;
  }
  return false;
}

bool ParseInt(const std::string& text, int* out_value) {
  if (out_value == nullptr) {
    return false;
  }
  std::istringstream iss(text);
  int value = 0;
  iss >> value;
  if (!iss.fail() && iss.eof()) {
    *out_value = value;
    return true;
  }
  return false;
}

bool ParseBool(const std::string& text, bool* out_value) {
  if (out_value == nullptr) {
    return false;
  }
  std::string lowered = text;
  std::transform(lowered.begin(), lowered.end(), lowered.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  if (lowered == "true" || lowered == "1" || lowered == "yes") {
    *out_value = true;
    return true;
  }
  if (lowered == "false" || lowered == "0" || lowered == "no") {
    *out_value = false;
    return true;
  }
  return false;
}

std::string ToLowerCopy(const std::string& text) {
  std::string lowered = text;
  std::transform(lowered.begin(), lowered.end(), lowered.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return lowered;
}

bool ParseAccountMode(const std::string& text, AccountMode* out_mode) {
  if (out_mode == nullptr) {
    return false;
  }
  const std::string lowered = ToLowerCopy(text);
  if (lowered == "unified" || lowered == "uta") {
    *out_mode = AccountMode::kUnified;
    return true;
  }
  if (lowered == "classic") {
    *out_mode = AccountMode::kClassic;
    return true;
  }
  return false;
}

bool ParseMarginMode(const std::string& text, MarginMode* out_mode) {
  if (out_mode == nullptr) {
    return false;
  }
  const std::string lowered = ToLowerCopy(text);
  if (lowered == "isolated" || lowered == "isolated_margin") {
    *out_mode = MarginMode::kIsolated;
    return true;
  }
  if (lowered == "cross" || lowered == "regular" || lowered == "regular_margin") {
    *out_mode = MarginMode::kCross;
    return true;
  }
  if (lowered == "portfolio" || lowered == "portfolio_margin") {
    *out_mode = MarginMode::kPortfolio;
    return true;
  }
  return false;
}

bool ParsePositionMode(const std::string& text, PositionMode* out_mode) {
  if (out_mode == nullptr) {
    return false;
  }
  const std::string lowered = ToLowerCopy(text);
  if (lowered == "one_way" || lowered == "oneway" || lowered == "0") {
    *out_mode = PositionMode::kOneWay;
    return true;
  }
  if (lowered == "hedge" || lowered == "3") {
    *out_mode = PositionMode::kHedge;
    return true;
  }
  return false;
}

bool ParseIntegratorMode(const std::string& text, IntegratorMode* out_mode) {
  if (out_mode == nullptr) {
    return false;
  }
  const std::string lowered = ToLowerCopy(text);
  if (lowered == "off" || lowered == "disabled") {
    *out_mode = IntegratorMode::kOff;
    return true;
  }
  if (lowered == "shadow" || lowered == "observe") {
    *out_mode = IntegratorMode::kShadow;
    return true;
  }
  if (lowered == "canary") {
    *out_mode = IntegratorMode::kCanary;
    return true;
  }
  if (lowered == "active" || lowered == "on") {
    *out_mode = IntegratorMode::kActive;
    return true;
  }
  return false;
}

bool ParseStringList(const std::string& text,
                     std::vector<std::string>* out_items) {
  if (out_items == nullptr) {
    return false;
  }

  std::string trimmed = Trim(text);
  if (trimmed.size() < 2 || trimmed.front() != '[' || trimmed.back() != ']') {
    return false;
  }
  trimmed = trimmed.substr(1, trimmed.size() - 2);
  out_items->clear();
  std::string token;
  std::istringstream iss(trimmed);
  while (std::getline(iss, token, ',')) {
    const std::string item = Trim(Unquote(Trim(token)));
    if (!item.empty()) {
      out_items->push_back(item);
    }
  }
  return true;
}

std::string NormalizeExchange(const std::string& exchange_value) {
  const std::string lowered = ToLowerCopy(exchange_value);
  if (lowered.find("binance") != std::string::npos) {
    return "binance";
  }
  if (lowered.find("bybit") != std::string::npos) {
    return "bybit";
  }
  if (lowered == "mock") {
    return "mock";
  }
  return lowered;
}

}  // namespace

bool LoadAppConfigFromYaml(const std::string& file_path,
                           AppConfig* out_config,
                           std::string* out_error) {
  if (out_config == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_config 为空";
    }
    return false;
  }

  std::ifstream input(file_path);
  if (!input.is_open()) {
    if (out_error != nullptr) {
      *out_error = "无法打开配置文件: " + file_path;
    }
    return false;
  }

  AppConfig config = *out_config;
  std::string current_section;
  std::string current_subsection;
  std::string line;
  int line_no = 0;
  while (std::getline(input, line)) {
    ++line_no;
    const std::string no_comment = Trim(StripInlineComment(line));
    if (no_comment.empty()) {
      continue;
    }

    const std::size_t indent = line.find_first_not_of(' ');
    if (indent == std::string::npos) {
      continue;
    }

    if (indent == 0 && no_comment.back() == ':') {
      current_section = Trim(no_comment.substr(0, no_comment.size() - 1));
      current_subsection.clear();
      continue;
    }

    if (indent < 2) {
      continue;
    }

    if (indent == 2 && no_comment.back() == ':') {
      current_subsection = Trim(no_comment.substr(0, no_comment.size() - 1));
      continue;
    }

    const std::size_t colon_pos = no_comment.find(':');
    if (colon_pos == std::string::npos) {
      continue;
    }
    const std::string key = Trim(no_comment.substr(0, colon_pos));
    const std::string raw_value = Trim(no_comment.substr(colon_pos + 1));
    if (raw_value.empty()) {
      continue;
    }
    const std::string value = Unquote(raw_value);
    if (indent <= 2) {
      current_subsection.clear();
    }

    if (current_section == "risk" && key == "max_abs_notional_usd") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "risk.max_abs_notional_usd 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.risk_max_abs_notional_usd = parsed;
      continue;
    }

    if (current_section == "risk" &&
        current_subsection == "max_drawdown" &&
        key == "degraded_threshold") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "risk.max_drawdown.degraded_threshold 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.risk_thresholds.degraded_drawdown = parsed;
      continue;
    }

    if (current_section == "risk" &&
        current_subsection == "max_drawdown" &&
        key == "cooldown_threshold") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "risk.max_drawdown.cooldown_threshold 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.risk_thresholds.cooldown_drawdown = parsed;
      continue;
    }

    if (current_section == "risk" &&
        current_subsection == "max_drawdown" &&
        key == "fuse_threshold") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "risk.max_drawdown.fuse_threshold 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.risk_thresholds.fuse_drawdown = parsed;
      continue;
    }

    if (current_section == "risk" &&
        current_subsection == "liquidation" &&
        key == "min_distance_p95") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "risk.liquidation.min_distance_p95 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.risk_thresholds.min_liquidation_distance = parsed;
      continue;
    }

    if (current_section == "execution" && key == "max_order_notional") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.max_order_notional 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_max_order_notional = parsed;
      continue;
    }

    if (current_section == "execution" &&
        (key == "min_rebalance_notional" ||
         key == "min_rebalance_notional_usd")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.min_rebalance_notional_usd 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_min_rebalance_notional_usd = parsed;
      continue;
    }

    if (current_section == "execution" && key == "min_order_interval_ms") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.min_order_interval_ms 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_min_order_interval_ms = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "reverse_signal_cooldown_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.reverse_signal_cooldown_ticks 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_reverse_signal_cooldown_ticks = parsed;
      continue;
    }

    if (current_section == "execution" &&
        (key == "enable_fee_aware_entry_gate" ||
         key == "fee_aware_entry_gate")) {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.enable_fee_aware_entry_gate 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_enable_fee_aware_entry_gate = parsed;
      continue;
    }

    if (current_section == "execution" &&
        (key == "entry_fee_bps" || key == "taker_fee_bps")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "execution.entry_fee_bps 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.execution_entry_fee_bps = parsed;
      continue;
    }

    if (current_section == "execution" && key == "exit_fee_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "execution.exit_fee_bps 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.execution_exit_fee_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        (key == "expected_slippage_bps" || key == "slippage_bps" ||
         key == "max_slippage_bps")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.expected_slippage_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_expected_slippage_bps = parsed;
      continue;
    }

    if (current_section == "execution" && key == "min_expected_edge_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.min_expected_edge_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_min_expected_edge_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        (key == "required_edge_cap_bps" || key == "max_required_edge_bps")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.required_edge_cap_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_required_edge_cap_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        (key == "adaptive_fee_gate_enabled" ||
         key == "adaptive_fee_aware_enabled")) {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.adaptive_fee_gate_enabled 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_adaptive_fee_gate_enabled = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "adaptive_fee_gate_min_samples") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.adaptive_fee_gate_min_samples 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_adaptive_fee_gate_min_samples = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "adaptive_fee_gate_trigger_ratio") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.adaptive_fee_gate_trigger_ratio 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_adaptive_fee_gate_trigger_ratio = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "adaptive_fee_gate_max_relax_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.adaptive_fee_gate_max_relax_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_adaptive_fee_gate_max_relax_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        (key == "maker_entry_enabled" || key == "maker_first")) {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "execution.maker_entry_enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.execution_maker_entry_enabled = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "maker_fallback_to_market") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.maker_fallback_to_market 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_maker_fallback_to_market = parsed;
      continue;
    }

    if (current_section == "execution" && key == "maker_price_offset_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.maker_price_offset_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_maker_price_offset_bps = parsed;
      continue;
    }

    if (current_section == "execution" && key == "maker_post_only") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "execution.maker_post_only 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.execution_maker_post_only = parsed;
      continue;
    }

    if (current_section == "execution" && key == "maker_edge_relax_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.maker_edge_relax_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_maker_edge_relax_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "cost_filter_cooldown_trigger_count") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.cost_filter_cooldown_trigger_count 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_cost_filter_cooldown_trigger_count = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "cost_filter_cooldown_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.cost_filter_cooldown_ticks 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_cost_filter_cooldown_ticks = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "quality_guard_enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.quality_guard_enabled 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_quality_guard_enabled = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "quality_guard_min_fills") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.quality_guard_min_fills 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_quality_guard_min_fills = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "quality_guard_bad_streak_to_trigger") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.quality_guard_bad_streak_to_trigger 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_quality_guard_bad_streak_to_trigger = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "quality_guard_good_streak_to_release") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.quality_guard_good_streak_to_release 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_quality_guard_good_streak_to_release = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "quality_guard_min_realized_net_per_fill_usd") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.quality_guard_min_realized_net_per_fill_usd 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_quality_guard_min_realized_net_per_fill_usd = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "quality_guard_max_fee_bps_per_fill") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.quality_guard_max_fee_bps_per_fill 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_quality_guard_max_fee_bps_per_fill = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "quality_guard_required_edge_penalty_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.quality_guard_required_edge_penalty_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_quality_guard_required_edge_penalty_bps = parsed;
      continue;
    }

    if (current_section == "execution" && key == "dynamic_edge_enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_enabled 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_enabled = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "dynamic_edge_regime_trend_relax_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_regime_trend_relax_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_regime_trend_relax_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "dynamic_edge_regime_range_penalty_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_regime_range_penalty_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_regime_range_penalty_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "dynamic_edge_regime_extreme_penalty_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_regime_extreme_penalty_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_regime_extreme_penalty_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "dynamic_edge_volatility_relax_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_volatility_relax_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_volatility_relax_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "dynamic_edge_volatility_penalty_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_volatility_penalty_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_volatility_penalty_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "dynamic_edge_liquidity_maker_ratio_threshold") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_liquidity_maker_ratio_threshold 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_liquidity_maker_ratio_threshold = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "dynamic_edge_liquidity_unknown_ratio_threshold") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_liquidity_unknown_ratio_threshold 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_liquidity_unknown_ratio_threshold = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "dynamic_edge_liquidity_relax_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_liquidity_relax_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_liquidity_relax_bps = parsed;
      continue;
    }

    if (current_section == "execution" &&
        key == "dynamic_edge_liquidity_penalty_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.dynamic_edge_liquidity_penalty_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.execution_dynamic_edge_liquidity_penalty_bps = parsed;
      continue;
    }

    if (current_section == "strategy" &&
        (key == "signal_notional" || key == "signal_notional_usd")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "strategy.signal_notional_usd 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.strategy_signal_notional_usd = parsed;
      continue;
    }

    if (current_section == "strategy" &&
        (key == "signal_deadband_abs" || key == "deadband_abs")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "strategy.signal_deadband_abs 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.strategy_signal_deadband_abs = parsed;
      continue;
    }

    if (current_section == "strategy" &&
        (key == "min_hold_ticks" || key == "reverse_min_hold_ticks")) {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "strategy.min_hold_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.strategy_min_hold_ticks = parsed;
      continue;
    }

    if (current_section == "strategy" &&
        key == "defensive_notional_ratio") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "strategy.defensive_notional_ratio 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.strategy_defensive_notional_ratio = parsed;
      continue;
    }

    if (current_section == "strategy" &&
        key == "defensive_entry_score") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "strategy.defensive_entry_score 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.strategy_defensive_entry_score = parsed;
      continue;
    }

    if (current_section == "strategy" &&
        key == "defensive_trend_scale") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "strategy.defensive_trend_scale 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.strategy_defensive_trend_scale = parsed;
      continue;
    }

    if (current_section == "strategy" &&
        key == "defensive_range_scale") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "strategy.defensive_range_scale 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.strategy_defensive_range_scale = parsed;
      continue;
    }

    if (current_section == "strategy" &&
        key == "defensive_extreme_scale") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "strategy.defensive_extreme_scale 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.strategy_defensive_extreme_scale = parsed;
      continue;
    }

    if (current_section == "strategy" &&
        current_subsection == "params" &&
        key == "ema_fast") {
      int parsed = 0;
      if (ParseInt(value, &parsed)) {
        config.trend_ema_fast = parsed;
      }
      continue;
    }
    if (current_section == "strategy" &&
        current_subsection == "params" &&
        key == "ema_slow") {
      int parsed = 0;
      if (ParseInt(value, &parsed)) {
        config.trend_ema_slow = parsed;
      }
      continue;
    }
    if (current_section == "strategy" &&
        current_subsection == "params" &&
        key == "target_vol") {
      double parsed = 0.0;
      if (ParseDouble(value, &parsed)) {
        config.vol_target_pct = parsed;
      }
      continue;
    }

    if (current_section == "execution" && key == "exchange") {
      config.exchange = NormalizeExchange(value);
      continue;
    }

    if (current_section == "exchange" && key == "platform") {
      config.exchange = NormalizeExchange(value);
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "testnet") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "exchange.bybit.testnet 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.bybit.testnet = parsed;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "demo_trading") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "exchange.bybit.demo_trading 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.bybit.demo_trading = parsed;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "category") {
      config.bybit.category = value;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "account_type") {
      config.bybit.account_type = value;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "expected_account_mode") {
      AccountMode mode = AccountMode::kUnified;
      if (!ParseAccountMode(value, &mode)) {
        if (out_error != nullptr) {
          *out_error =
              "exchange.bybit.expected_account_mode 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.bybit.expected_account_mode = mode;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "expected_margin_mode") {
      MarginMode mode = MarginMode::kIsolated;
      if (!ParseMarginMode(value, &mode)) {
        if (out_error != nullptr) {
          *out_error =
              "exchange.bybit.expected_margin_mode 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.bybit.expected_margin_mode = mode;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "expected_position_mode") {
      PositionMode mode = PositionMode::kOneWay;
      if (!ParsePositionMode(value, &mode)) {
        if (out_error != nullptr) {
          *out_error =
              "exchange.bybit.expected_position_mode 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.bybit.expected_position_mode = mode;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "public_ws_enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "exchange.bybit.public_ws_enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.bybit.public_ws_enabled = parsed;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "public_ws_rest_fallback") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "exchange.bybit.public_ws_rest_fallback 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.bybit.public_ws_rest_fallback = parsed;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "private_ws_enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "exchange.bybit.private_ws_enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.bybit.private_ws_enabled = parsed;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "private_ws_rest_fallback") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "exchange.bybit.private_ws_rest_fallback 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.bybit.private_ws_rest_fallback = parsed;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "ws_reconnect_interval_ms") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "exchange.bybit.ws_reconnect_interval_ms 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.bybit.ws_reconnect_interval_ms = parsed;
      continue;
    }

    if (current_section == "exchange" &&
        current_subsection == "bybit" &&
        key == "execution_poll_limit") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "exchange.bybit.execution_poll_limit 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.bybit.execution_poll_limit = parsed;
      continue;
    }

    if (current_section == "universe" && key == "enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "universe.enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.universe.enabled = parsed;
      continue;
    }

    if (current_section == "universe" &&
        (key == "update_interval_ticks" ||
         key == "update_interval_minutes")) {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "universe.update_interval_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.universe.update_interval_ticks = parsed;
      continue;
    }

    if (current_section == "universe" &&
        key == "max_active_symbols") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "universe.max_active_symbols 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.universe.max_active_symbols = parsed;
      continue;
    }

    if (current_section == "universe" &&
        key == "min_active_symbols") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "universe.min_active_symbols 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.universe.min_active_symbols = parsed;
      continue;
    }

    if (current_section == "universe" &&
        key == "min_turnover_usd") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "universe.min_turnover_usd 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.universe.min_turnover_usd = parsed;
      continue;
    }

    if (current_section == "universe" &&
        key == "fallback_symbols") {
      std::vector<std::string> parsed;
      if (!ParseStringList(raw_value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "universe.fallback_symbols 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.universe.fallback_symbols = std::move(parsed);
      continue;
    }

    if (current_section == "universe" &&
        key == "candidate_symbols") {
      std::vector<std::string> parsed;
      if (!ParseStringList(raw_value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "universe.candidate_symbols 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.universe.candidate_symbols = std::move(parsed);
      continue;
    }

    if (current_section == "system" && key == "data_path") {
      config.data_path = value;
      continue;
    }

    if (current_section == "system" && key == "mode") {
      config.mode = value;
      continue;
    }

    if (current_section == "system" && key == "primary_symbol") {
      config.primary_symbol = value;
      continue;
    }

    if (current_section == "system" && key == "max_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "system.max_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.system_max_ticks = parsed;
      continue;
    }

    if (current_section == "system" && key == "status_log_interval_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "system.status_log_interval_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.system_status_log_interval_ticks = parsed;
      continue;
    }

    if (current_section == "system" &&
        key == "remote_risk_refresh_interval_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "system.remote_risk_refresh_interval_ticks 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.system_remote_risk_refresh_interval_ticks = parsed;
      continue;
    }

    if (current_section == "system" &&
        current_subsection == "reconcile" &&
        key == "enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "system.reconcile.enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.reconcile.enabled = parsed;
      continue;
    }

    if (current_section == "system" &&
        current_subsection == "reconcile" &&
        key == "interval_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "system.reconcile.interval_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.reconcile.interval_ticks = parsed;
      continue;
    }

    if (current_section == "system" &&
        current_subsection == "reconcile" &&
        key == "tolerance_notional_usd") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "system.reconcile.tolerance_notional_usd 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.reconcile.tolerance_notional_usd = parsed;
      continue;
    }

    if (current_section == "system" &&
        current_subsection == "reconcile" &&
        key == "mismatch_confirmations") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "system.reconcile.mismatch_confirmations 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.reconcile.mismatch_confirmations = parsed;
      continue;
    }

    if (current_section == "system" &&
        current_subsection == "reconcile" &&
        key == "pending_order_stale_ms") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "system.reconcile.pending_order_stale_ms 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.reconcile.pending_order_stale_ms = parsed;
      continue;
    }

    if (current_section == "system" &&
        current_subsection == "reconcile" &&
        key == "anomaly_reduce_only_streak") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "system.reconcile.anomaly_reduce_only_streak 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.reconcile.anomaly_reduce_only_streak = parsed;
      continue;
    }

    if (current_section == "system" &&
        current_subsection == "reconcile" &&
        key == "anomaly_halt_streak") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "system.reconcile.anomaly_halt_streak 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.reconcile.anomaly_halt_streak = parsed;
      continue;
    }

    if (current_section == "system" &&
        current_subsection == "reconcile" &&
        key == "anomaly_resume_streak") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "system.reconcile.anomaly_resume_streak 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.reconcile.anomaly_resume_streak = parsed;
      continue;
    }

    if (current_section == "gate" &&
        (key == "min_effective_signals_per_window" ||
         key == "min_effective_signals_per_day")) {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "gate.min_effective_signals_per_window 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.gate.min_effective_signals_per_window = parsed;
      continue;
    }

    if (current_section == "gate" &&
        (key == "min_fills_per_window" ||
         key == "min_fills_per_day")) {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "gate.min_fills_per_window 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.gate.min_fills_per_window = parsed;
      continue;
    }

    if (current_section == "gate" &&
        key == "heartbeat_empty_signal_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "gate.heartbeat_empty_signal_ticks 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.gate.heartbeat_empty_signal_ticks = parsed;
      continue;
    }

    if (current_section == "gate" && key == "window_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "gate.window_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.gate.window_ticks = parsed;
      continue;
    }

    if (current_section == "gate" && key == "enforce_runtime_actions") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "gate.enforce_runtime_actions 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.gate.enforce_runtime_actions = parsed;
      continue;
    }

    if (current_section == "gate" && key == "fail_to_reduce_only_windows") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "gate.fail_to_reduce_only_windows 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.gate.fail_to_reduce_only_windows = parsed;
      continue;
    }

    if (current_section == "gate" && key == "fail_to_halt_windows") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "gate.fail_to_halt_windows 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.gate.fail_to_halt_windows = parsed;
      continue;
    }

    if (current_section == "gate" && key == "reduce_only_cooldown_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "gate.reduce_only_cooldown_ticks 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.gate.reduce_only_cooldown_ticks = parsed;
      continue;
    }

    if (current_section == "gate" && key == "halt_cooldown_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "gate.halt_cooldown_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.gate.halt_cooldown_ticks = parsed;
      continue;
    }

    if (current_section == "gate" &&
        (key == "pass_to_resume_windows" || key == "resume_pass_windows")) {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "gate.pass_to_resume_windows 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.gate.pass_to_resume_windows = parsed;
      continue;
    }

    if (current_section == "gate" && key == "auto_resume_when_flat") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "gate.auto_resume_when_flat 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.gate.auto_resume_when_flat = parsed;
      continue;
    }

    if (current_section == "gate" && key == "auto_resume_flat_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "gate.auto_resume_flat_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.gate.auto_resume_flat_ticks = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection.empty() &&
        key == "enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "integrator.enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.integrator.enabled = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection.empty() &&
        key == "model_type") {
      config.integrator.model_type = value;
      continue;
    }

    if (current_section == "integrator" && current_subsection.empty() &&
        key == "mode") {
      IntegratorMode parsed = IntegratorMode::kShadow;
      if (!ParseIntegratorMode(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "integrator.mode 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.integrator.mode = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection.empty() &&
        key == "canary_notional_ratio") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.canary_notional_ratio 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.canary_notional_ratio = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection.empty() &&
        key == "canary_confidence_threshold") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.canary_confidence_threshold 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.canary_confidence_threshold = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection.empty() &&
        key == "canary_allow_countertrend") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.canary_allow_countertrend 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.canary_allow_countertrend = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection.empty() &&
        key == "active_confidence_threshold") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.active_confidence_threshold 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.active_confidence_threshold = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "integrator.shadow.enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.enabled = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "log_model_score") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "integrator.shadow.log_model_score 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.log_model_score = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        (key == "model_report_path" || key == "report_path")) {
      config.integrator.shadow.model_report_path = value;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        (key == "model_path" || key == "active_model_path")) {
      config.integrator.shadow.model_path = value;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "active_meta_path") {
      config.integrator.shadow.active_meta_path = value;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "require_model_file") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.shadow.require_model_file 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.require_model_file = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "require_active_meta") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.shadow.require_active_meta 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.require_active_meta = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "require_gate_pass") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.shadow.require_gate_pass 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.require_gate_pass = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "min_auc_mean") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.shadow.min_auc_mean 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.min_auc_mean = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "min_delta_auc_vs_baseline") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.shadow.min_delta_auc_vs_baseline 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.min_delta_auc_vs_baseline = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "min_split_trained_count") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.shadow.min_split_trained_count 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.min_split_trained_count = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "min_split_trained_ratio") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "integrator.shadow.min_split_trained_ratio 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.min_split_trained_ratio = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "score_gain") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "integrator.shadow.score_gain 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.score_gain = parsed;
      continue;
    }

    if (current_section == "integrator" && current_subsection == "shadow" &&
        key == "feature_window_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "integrator.shadow.feature_window_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.integrator.shadow.feature_window_ticks = parsed;
      continue;
    }

    if (current_section == "self_evolution" && key == "enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "self_evolution.enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.enabled = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "update_interval_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.update_interval_ticks 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.update_interval_ticks = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "min_update_interval_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.min_update_interval_ticks 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.min_update_interval_ticks = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "min_abs_window_pnl_usd") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.min_abs_window_pnl_usd 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.min_abs_window_pnl_usd = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "min_bucket_ticks_for_update") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.min_bucket_ticks_for_update 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.min_bucket_ticks_for_update = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "use_virtual_pnl") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.use_virtual_pnl 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.use_virtual_pnl = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        (key == "use_counterfactual_search" ||
         key == "counterfactual_search_enabled")) {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.use_counterfactual_search 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.use_counterfactual_search = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "counterfactual_fallback_to_factor_ic") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.counterfactual_fallback_to_factor_ic 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.counterfactual_fallback_to_factor_ic = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "counterfactual_min_improvement_usd") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.counterfactual_min_improvement_usd 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.counterfactual_min_improvement_usd = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "counterfactual_improvement_decay_per_filtered_signal_usd") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.counterfactual_improvement_decay_per_filtered_signal_usd 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.counterfactual_improvement_decay_per_filtered_signal_usd =
          parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "counterfactual_min_fill_count_for_update") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.counterfactual_min_fill_count_for_update 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.counterfactual_min_fill_count_for_update = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "counterfactual_min_t_stat_samples_for_update") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.counterfactual_min_t_stat_samples_for_update 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.counterfactual_min_t_stat_samples_for_update = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "counterfactual_min_t_stat_abs_for_update") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.counterfactual_min_t_stat_abs_for_update 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.counterfactual_min_t_stat_abs_for_update = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "virtual_cost_bps") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.virtual_cost_bps 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.virtual_cost_bps = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        (key == "enable_factor_ic_adaptive_weights" ||
         key == "use_factor_ic_adaptive_weights")) {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.enable_factor_ic_adaptive_weights 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.enable_factor_ic_adaptive_weights = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "factor_ic_min_samples") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.factor_ic_min_samples 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.factor_ic_min_samples = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "factor_ic_min_abs") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.factor_ic_min_abs 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.factor_ic_min_abs = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        (key == "enable_learnability_gate" ||
         key == "learnability_gate_enabled")) {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.enable_learnability_gate 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.enable_learnability_gate = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "learnability_min_samples") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.learnability_min_samples 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.learnability_min_samples = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        (key == "learnability_min_t_stat_abs" ||
         key == "learnability_t_stat_threshold")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.learnability_min_t_stat_abs 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.learnability_min_t_stat_abs = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        (key == "objective_alpha_pnl" || key == "alpha_pnl")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.objective_alpha_pnl 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.objective_alpha_pnl = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        (key == "objective_beta_drawdown" || key == "beta_drawdown")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.objective_beta_drawdown 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.objective_beta_drawdown = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        (key == "objective_gamma_notional_churn" ||
         key == "gamma_notional_churn")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.objective_gamma_notional_churn 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.objective_gamma_notional_churn = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "max_single_strategy_weight") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.max_single_strategy_weight 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.max_single_strategy_weight = parsed;
      continue;
    }

    if (current_section == "self_evolution" && key == "max_weight_step") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.max_weight_step 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.max_weight_step = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "rollback_degrade_windows") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.rollback_degrade_windows 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.rollback_degrade_windows = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        (key == "rollback_degrade_threshold_score" ||
         key == "rollback_degrade_threshold_pnl")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.rollback_degrade_threshold_score 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.rollback_degrade_threshold_score = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "rollback_cooldown_ticks") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.rollback_cooldown_ticks 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.rollback_cooldown_ticks = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "initial_trend_weight") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.initial_trend_weight 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.initial_trend_weight = parsed;
      continue;
    }

    if (current_section == "self_evolution" &&
        key == "initial_defensive_weight") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "self_evolution.initial_defensive_weight 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.self_evolution.initial_defensive_weight = parsed;
      continue;
    }

    if (current_section == "regime" && key == "enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "regime.enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.regime.enabled = parsed;
      continue;
    }

    if (current_section == "regime" &&
        (key == "warmup_ticks" || key == "min_warmup_ticks")) {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "regime.warmup_ticks 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.regime.warmup_ticks = parsed;
      continue;
    }

    if (current_section == "regime" && key == "ewma_alpha") {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "regime.ewma_alpha 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.regime.ewma_alpha = parsed;
      continue;
    }

    if (current_section == "regime" &&
        (key == "trend_threshold" || key == "trend_return_threshold")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "regime.trend_threshold 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.regime.trend_threshold = parsed;
      continue;
    }

    if (current_section == "regime" &&
        (key == "extreme_threshold" || key == "extreme_return_threshold")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "regime.extreme_threshold 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.regime.extreme_threshold = parsed;
      continue;
    }

    if (current_section == "regime" &&
        (key == "volatility_threshold" ||
         key == "extreme_volatility_threshold")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "regime.volatility_threshold 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.regime.volatility_threshold = parsed;
      continue;
    }

    if (current_section == "execution" &&
        current_subsection == "protection" &&
        key == "enabled") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "execution.protection.enabled 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.protection.enabled = parsed;
      continue;
    }

    if (current_section == "execution" &&
        current_subsection == "protection" &&
        key == "require_sl") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "execution.protection.require_sl 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.protection.require_sl = parsed;
      continue;
    }

    if (current_section == "execution" &&
        current_subsection == "protection" &&
        key == "enable_tp") {
      bool parsed = false;
      if (!ParseBool(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error = "execution.protection.enable_tp 解析失败，行号: " +
                       std::to_string(line_no);
        }
        return false;
      }
      config.protection.enable_tp = parsed;
      continue;
    }

    if (current_section == "execution" &&
        current_subsection == "protection" &&
        key == "attach_timeout_ms") {
      int parsed = 0;
      if (!ParseInt(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.protection.attach_timeout_ms 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.protection.attach_timeout_ms = parsed;
      continue;
    }

    if (current_section == "execution" &&
        current_subsection == "protection" &&
        (key == "stop_loss_ratio" || key == "stop_loss_atr_mult")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.protection.stop_loss_ratio 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.protection.stop_loss_ratio = parsed;
      continue;
    }

    if (current_section == "execution" &&
        current_subsection == "protection" &&
        (key == "take_profit_ratio" || key == "take_profit_rr")) {
      double parsed = 0.0;
      if (!ParseDouble(value, &parsed)) {
        if (out_error != nullptr) {
          *out_error =
              "execution.protection.take_profit_ratio 解析失败，行号: " +
              std::to_string(line_no);
        }
        return false;
      }
      config.protection.take_profit_ratio = parsed;
    }
  }

  if (config.exchange.empty()) {
    config.exchange = "mock";
  }
  if (config.primary_symbol.empty()) {
    if (out_error != nullptr) {
      *out_error = "system.primary_symbol 不能为空";
    }
    return false;
  }
  if (config.universe.update_interval_ticks <= 0) {
    if (out_error != nullptr) {
      *out_error = "universe.update_interval_ticks 必须大于 0";
    }
    return false;
  }
  if (config.universe.min_active_symbols < 1 ||
      config.universe.max_active_symbols < config.universe.min_active_symbols) {
    if (out_error != nullptr) {
      *out_error = "universe min/max_active_symbols 配置非法";
    }
    return false;
  }
  if (config.universe.min_turnover_usd < 0.0) {
    if (out_error != nullptr) {
      *out_error = "universe.min_turnover_usd 不能为负数";
    }
    return false;
  }
  if (config.universe.fallback_symbols.empty()) {
    if (out_error != nullptr) {
      *out_error = "universe.fallback_symbols 不能为空";
    }
    return false;
  }
  if (config.universe.candidate_symbols.empty()) {
    config.universe.candidate_symbols = config.universe.fallback_symbols;
  }
  if (config.bybit.execution_poll_limit <= 0) {
    if (out_error != nullptr) {
      *out_error = "exchange.bybit.execution_poll_limit 必须大于 0";
    }
    return false;
  }
  if (config.bybit.ws_reconnect_interval_ms < 0) {
    if (out_error != nullptr) {
      *out_error = "exchange.bybit.ws_reconnect_interval_ms 不能为负数";
    }
    return false;
  }
  if (config.bybit.testnet && config.bybit.demo_trading) {
    if (out_error != nullptr) {
      *out_error =
          "exchange.bybit.demo_trading=true 时不允许 exchange.bybit.testnet=true";
    }
    return false;
  }
  if (config.gate.window_ticks <= 0) {
    if (out_error != nullptr) {
      *out_error = "gate.window_ticks 必须大于 0";
    }
    return false;
  }
  if (config.reconcile.mismatch_confirmations <= 0) {
    if (out_error != nullptr) {
      *out_error = "system.reconcile.mismatch_confirmations 必须大于 0";
    }
    return false;
  }
  if (config.reconcile.pending_order_stale_ms <= 0) {
    if (out_error != nullptr) {
      *out_error = "system.reconcile.pending_order_stale_ms 必须大于 0";
    }
    return false;
  }
  if (config.reconcile.anomaly_reduce_only_streak < 0 ||
      config.reconcile.anomaly_halt_streak < 0 ||
      config.reconcile.anomaly_resume_streak < 0) {
    if (out_error != nullptr) {
      *out_error = "system.reconcile.anomaly_* 参数不能为负数";
    }
    return false;
  }
  if (config.gate.min_effective_signals_per_window < 0 ||
      config.gate.min_fills_per_window < 0) {
    if (out_error != nullptr) {
      *out_error = "gate 最小活跃度阈值不能为负数";
    }
    return false;
  }
  if (config.gate.fail_to_reduce_only_windows < 0 ||
      config.gate.fail_to_halt_windows < 0 ||
      config.gate.reduce_only_cooldown_ticks < 0 ||
      config.gate.halt_cooldown_ticks < 0 ||
      config.gate.pass_to_resume_windows < 0 ||
      config.gate.auto_resume_flat_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "gate 运行时动作参数不能为负数";
    }
    return false;
  }
  if (config.integrator.shadow.score_gain <= 0.0) {
    if (out_error != nullptr) {
      *out_error = "integrator.shadow.score_gain 必须大于 0";
    }
    return false;
  }
  if (config.integrator.shadow.min_auc_mean < 0.0 ||
      config.integrator.shadow.min_auc_mean > 1.0) {
    if (out_error != nullptr) {
      *out_error = "integrator.shadow.min_auc_mean 必须在 [0,1] 区间";
    }
    return false;
  }
  if (config.integrator.shadow.min_split_trained_count < 1) {
    if (out_error != nullptr) {
      *out_error = "integrator.shadow.min_split_trained_count 必须 >= 1";
    }
    return false;
  }
  if (config.integrator.shadow.min_split_trained_ratio < 0.0 ||
      config.integrator.shadow.min_split_trained_ratio > 1.0) {
    if (out_error != nullptr) {
      *out_error =
          "integrator.shadow.min_split_trained_ratio 必须在 [0,1] 区间";
    }
    return false;
  }
  if (!config.integrator.enabled) {
    // 兼容旧配置：enabled=false 时强制等价 off。
    config.integrator.mode = IntegratorMode::kOff;
  }
  if (config.integrator.canary_notional_ratio < 0.0 ||
      config.integrator.canary_notional_ratio > 1.0) {
    if (out_error != nullptr) {
      *out_error = "integrator.canary_notional_ratio 必须在 [0,1] 区间";
    }
    return false;
  }
  if (config.integrator.canary_confidence_threshold < 0.0 ||
      config.integrator.canary_confidence_threshold > 1.0) {
    if (out_error != nullptr) {
      *out_error =
          "integrator.canary_confidence_threshold 必须在 [0,1] 区间";
    }
    return false;
  }
  if (config.integrator.active_confidence_threshold < 0.0 ||
      config.integrator.active_confidence_threshold > 1.0) {
    if (out_error != nullptr) {
      *out_error =
          "integrator.active_confidence_threshold 必须在 [0,1] 区间";
    }
    return false;
  }
  if (config.integrator.mode == IntegratorMode::kCanary &&
      config.integrator.canary_confidence_threshold < 0.5) {
    if (out_error != nullptr) {
      *out_error =
          "integrator.canary_confidence_threshold 在 canary 模式下应 >= 0.5";
    }
    return false;
  }
  if (config.integrator.mode == IntegratorMode::kActive &&
      config.integrator.active_confidence_threshold < 0.5) {
    if (out_error != nullptr) {
      *out_error =
          "integrator.active_confidence_threshold 在 active 模式下应 >= 0.5";
    }
    return false;
  }
  if (config.integrator.enabled && config.integrator.shadow.enabled &&
      config.integrator.shadow.model_report_path.empty()) {
    if (out_error != nullptr) {
      *out_error = "integrator.shadow.model_report_path 不能为空";
    }
    return false;
  }
  if (config.integrator.enabled && config.integrator.shadow.enabled &&
      config.integrator.shadow.require_model_file &&
      config.integrator.shadow.model_path.empty()) {
    if (out_error != nullptr) {
      *out_error = "integrator.shadow.model_path 不能为空";
    }
    return false;
  }
  if (config.integrator.enabled && config.integrator.shadow.enabled &&
      config.integrator.shadow.require_active_meta &&
      config.integrator.shadow.active_meta_path.empty()) {
    if (out_error != nullptr) {
      *out_error = "integrator.shadow.active_meta_path 不能为空";
    }
    return false;
  }
  if (config.protection.enabled &&
      (!config.protection.require_sl || config.protection.attach_timeout_ms <= 0)) {
    if (out_error != nullptr) {
      *out_error =
          "execution.protection 启用时必须满足 require_sl=true 且 attach_timeout_ms>0";
    }
    return false;
  }
  if (config.protection.enabled && config.protection.stop_loss_ratio <= 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.protection.stop_loss_ratio 必须大于 0";
    }
    return false;
  }
  if (config.protection.enabled && config.protection.enable_tp &&
      config.protection.take_profit_ratio <= 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.protection.take_profit_ratio 必须大于 0";
    }
    return false;
  }
  if (config.execution_min_order_interval_ms < 0) {
    if (out_error != nullptr) {
      *out_error = "execution.min_order_interval_ms 不能为负数";
    }
    return false;
  }
  if (config.execution_min_rebalance_notional_usd < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.min_rebalance_notional_usd 不能为负数";
    }
    return false;
  }
  if (config.execution_reverse_signal_cooldown_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "execution.reverse_signal_cooldown_ticks 不能为负数";
    }
    return false;
  }
  if (config.execution_entry_fee_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.entry_fee_bps 不能为负数";
    }
    return false;
  }
  if (config.execution_exit_fee_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.exit_fee_bps 不能为负数";
    }
    return false;
  }
  if (config.execution_expected_slippage_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.expected_slippage_bps 不能为负数";
    }
    return false;
  }
  if (config.execution_min_expected_edge_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.min_expected_edge_bps 不能为负数";
    }
    return false;
  }
  if (config.execution_required_edge_cap_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.required_edge_cap_bps 不能为负数";
    }
    return false;
  }
  if (config.execution_adaptive_fee_gate_min_samples < 0) {
    if (out_error != nullptr) {
      *out_error = "execution.adaptive_fee_gate_min_samples 不能为负数";
    }
    return false;
  }
  if (config.execution_adaptive_fee_gate_trigger_ratio < 0.0 ||
      config.execution_adaptive_fee_gate_trigger_ratio > 1.0) {
    if (out_error != nullptr) {
      *out_error =
          "execution.adaptive_fee_gate_trigger_ratio 必须在 [0,1] 范围内";
    }
    return false;
  }
  if (config.execution_adaptive_fee_gate_max_relax_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.adaptive_fee_gate_max_relax_bps 不能为负数";
    }
    return false;
  }
  if (config.execution_maker_price_offset_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.maker_price_offset_bps 不能为负数";
    }
    return false;
  }
  if (config.execution_maker_edge_relax_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.maker_edge_relax_bps 不能为负数";
    }
    return false;
  }
  if (config.execution_cost_filter_cooldown_trigger_count < 0) {
    if (out_error != nullptr) {
      *out_error = "execution.cost_filter_cooldown_trigger_count 不能为负数";
    }
    return false;
  }
  if (config.execution_cost_filter_cooldown_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "execution.cost_filter_cooldown_ticks 不能为负数";
    }
    return false;
  }
  if ((config.execution_cost_filter_cooldown_trigger_count > 0) !=
      (config.execution_cost_filter_cooldown_ticks > 0)) {
    if (out_error != nullptr) {
      *out_error =
          "execution.cost_filter_cooldown_trigger_count 与 execution.cost_filter_cooldown_ticks 需同时>0或同时为0";
    }
    return false;
  }
  if (config.execution_quality_guard_min_fills < 0 ||
      config.execution_quality_guard_bad_streak_to_trigger < 0 ||
      config.execution_quality_guard_good_streak_to_release < 0) {
    if (out_error != nullptr) {
      *out_error = "execution.quality_guard_* 整数参数不能为负数";
    }
    return false;
  }
  if (config.execution_quality_guard_max_fee_bps_per_fill < 0.0 ||
      config.execution_quality_guard_required_edge_penalty_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.quality_guard_* bps 参数不能为负数";
    }
    return false;
  }
  if (config.execution_dynamic_edge_regime_trend_relax_bps < 0.0 ||
      config.execution_dynamic_edge_regime_range_penalty_bps < 0.0 ||
      config.execution_dynamic_edge_regime_extreme_penalty_bps < 0.0 ||
      config.execution_dynamic_edge_volatility_relax_bps < 0.0 ||
      config.execution_dynamic_edge_volatility_penalty_bps < 0.0 ||
      config.execution_dynamic_edge_liquidity_relax_bps < 0.0 ||
      config.execution_dynamic_edge_liquidity_penalty_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "execution.dynamic_edge_* bps 参数不能为负数";
    }
    return false;
  }
  if (config.execution_dynamic_edge_liquidity_maker_ratio_threshold < 0.0 ||
      config.execution_dynamic_edge_liquidity_maker_ratio_threshold > 1.0 ||
      config.execution_dynamic_edge_liquidity_unknown_ratio_threshold < 0.0 ||
      config.execution_dynamic_edge_liquidity_unknown_ratio_threshold > 1.0) {
    if (out_error != nullptr) {
      *out_error = "execution.dynamic_edge_liquidity_*_threshold 必须在 [0,1] 区间";
    }
    return false;
  }
  if (config.strategy_signal_notional_usd < 0.0) {
    if (out_error != nullptr) {
      *out_error = "strategy.signal_notional_usd 不能为负数";
    }
    return false;
  }
  if (config.strategy_signal_deadband_abs < 0.0) {
    if (out_error != nullptr) {
      *out_error = "strategy.signal_deadband_abs 不能为负数";
    }
    return false;
  }
  if (config.strategy_min_hold_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "strategy.min_hold_ticks 不能为负数";
    }
    return false;
  }
  if (config.strategy_defensive_notional_ratio < 0.0) {
    if (out_error != nullptr) {
      *out_error = "strategy.defensive_notional_ratio 不能为负数";
    }
    return false;
  }
  if (config.strategy_defensive_entry_score <= 0.0) {
    if (out_error != nullptr) {
      *out_error = "strategy.defensive_entry_score 必须大于 0";
    }
    return false;
  }
  if (config.strategy_defensive_trend_scale < 0.0 ||
      config.strategy_defensive_range_scale < 0.0 ||
      config.strategy_defensive_extreme_scale < 0.0) {
    if (out_error != nullptr) {
      *out_error = "strategy.defensive_*_scale 不能为负数";
    }
    return false;
  }
  if (config.system_max_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "system.max_ticks 不能为负数";
    }
    return false;
  }
  if (config.system_status_log_interval_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "system.status_log_interval_ticks 不能为负数";
    }
    return false;
  }
  if (config.system_remote_risk_refresh_interval_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "system.remote_risk_refresh_interval_ticks 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.update_interval_ticks <= 0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.update_interval_ticks 必须大于 0";
    }
    return false;
  }
  if (config.self_evolution.min_update_interval_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.min_update_interval_ticks 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.min_abs_window_pnl_usd < 0.0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.min_abs_window_pnl_usd 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.min_bucket_ticks_for_update < 0) {
    if (out_error != nullptr) {
      *out_error =
          "self_evolution.min_bucket_ticks_for_update 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.counterfactual_min_improvement_usd < 0.0) {
    if (out_error != nullptr) {
      *out_error =
          "self_evolution.counterfactual_min_improvement_usd 不能为负数";
    }
    return false;
  }
  if (config.self_evolution
          .counterfactual_improvement_decay_per_filtered_signal_usd < 0.0) {
    if (out_error != nullptr) {
      *out_error =
          "self_evolution.counterfactual_improvement_decay_per_filtered_signal_usd 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.counterfactual_min_fill_count_for_update < 0 ||
      config.self_evolution.counterfactual_min_t_stat_samples_for_update < 0 ||
      config.self_evolution.counterfactual_min_t_stat_abs_for_update < 0.0) {
    if (out_error != nullptr) {
      *out_error =
          "self_evolution.counterfactual_*_for_update 参数不能为负数";
    }
    return false;
  }
  if (config.self_evolution.virtual_cost_bps < 0.0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.virtual_cost_bps 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.factor_ic_min_samples < 0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.factor_ic_min_samples 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.factor_ic_min_abs < 0.0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.factor_ic_min_abs 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.learnability_min_samples < 0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.learnability_min_samples 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.learnability_min_t_stat_abs < 0.0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.learnability_min_t_stat_abs 不能为负数";
    }
    return false;
  }
  if (config.self_evolution.objective_alpha_pnl < 0.0 ||
      config.self_evolution.objective_beta_drawdown < 0.0 ||
      config.self_evolution.objective_gamma_notional_churn < 0.0) {
    if (out_error != nullptr) {
      *out_error =
          "self_evolution objective 参数不能为负数";
    }
    return false;
  }
  if (config.self_evolution.objective_alpha_pnl <= 0.0 &&
      config.self_evolution.objective_beta_drawdown <= 0.0 &&
      config.self_evolution.objective_gamma_notional_churn <= 0.0) {
    if (out_error != nullptr) {
      *out_error =
          "self_evolution objective 参数不能同时为 0";
    }
    return false;
  }
  if (config.self_evolution.max_single_strategy_weight <= 0.0 ||
      config.self_evolution.max_single_strategy_weight > 1.0) {
    if (out_error != nullptr) {
      *out_error =
          "self_evolution.max_single_strategy_weight 必须在 (0,1] 范围内";
    }
    return false;
  }
  if (config.self_evolution.max_single_strategy_weight < 0.5) {
    if (out_error != nullptr) {
      *out_error =
          "self_evolution.max_single_strategy_weight 不能小于 0.5（双策略权重和=1）";
    }
    return false;
  }
  if (config.self_evolution.max_weight_step <= 0.0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.max_weight_step 必须大于 0";
    }
    return false;
  }
  if (config.self_evolution.rollback_degrade_windows <= 0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.rollback_degrade_windows 必须大于 0";
    }
    return false;
  }
  if (config.self_evolution.rollback_cooldown_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution.rollback_cooldown_ticks 不能为负数";
    }
    return false;
  }
  const double initial_weight_sum =
      config.self_evolution.initial_trend_weight +
      config.self_evolution.initial_defensive_weight;
  if (config.self_evolution.initial_trend_weight < 0.0 ||
      config.self_evolution.initial_defensive_weight < 0.0) {
    if (out_error != nullptr) {
      *out_error = "self_evolution 初始权重不能为负数";
    }
    return false;
  }
  if (std::fabs(initial_weight_sum - 1.0) > 1e-6) {
    if (out_error != nullptr) {
      *out_error = "self_evolution 初始权重和必须为 1.0";
    }
    return false;
  }
  if (config.self_evolution.initial_trend_weight >
          config.self_evolution.max_single_strategy_weight ||
      config.self_evolution.initial_defensive_weight >
          config.self_evolution.max_single_strategy_weight) {
    if (out_error != nullptr) {
      *out_error =
          "self_evolution 初始权重超过 max_single_strategy_weight";
    }
    return false;
  }
  if (config.regime.warmup_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "regime.warmup_ticks 不能为负数";
    }
    return false;
  }
  if (config.regime.ewma_alpha <= 0.0 || config.regime.ewma_alpha > 1.0) {
    if (out_error != nullptr) {
      *out_error = "regime.ewma_alpha 必须在 (0,1] 范围内";
    }
    return false;
  }
  if (config.regime.trend_threshold < 0.0 ||
      config.regime.extreme_threshold < 0.0 ||
      config.regime.volatility_threshold < 0.0) {
    if (out_error != nullptr) {
      *out_error = "regime 阈值参数不能为负数";
    }
    return false;
  }
  if (config.regime.extreme_threshold > 0.0 &&
      config.regime.trend_threshold > config.regime.extreme_threshold) {
    if (out_error != nullptr) {
      *out_error = "regime.trend_threshold 不能大于 regime.extreme_threshold";
    }
    return false;
  }
  *out_config = config;
  return true;
}

}  // namespace ai_trade
