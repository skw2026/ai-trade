#include "core/config.h"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <sstream>

namespace ai_trade {

namespace {

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
        key == "stop_loss_ratio") {
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
        key == "take_profit_ratio") {
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
  if (config.gate.min_effective_signals_per_window < 0 ||
      config.gate.min_fills_per_window < 0) {
    if (out_error != nullptr) {
      *out_error = "gate 最小活跃度阈值不能为负数";
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
  if (config.execution_min_order_interval_ms < 0) {
    if (out_error != nullptr) {
      *out_error = "execution.min_order_interval_ms 不能为负数";
    }
    return false;
  }
  if (config.execution_reverse_signal_cooldown_ticks < 0) {
    if (out_error != nullptr) {
      *out_error = "execution.reverse_signal_cooldown_ticks 不能为负数";
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
  *out_config = config;
  return true;
}

}  // namespace ai_trade
