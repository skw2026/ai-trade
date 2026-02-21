#include "exchange/bybit_exchange_adapter.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <exception>
#include <iomanip>
#include <limits>
#include <optional>
#include <sstream>
#include <unordered_set>
#include <utility>

#include "core/json_utils.h"
#include "core/log.h"

namespace ai_trade {

namespace {

// 说明：
// 该文件承担 Bybit 适配器的核心编排逻辑，覆盖
// 1) 认证与通道初始化（REST/WS）；
// 2) symbol 规则加载与数量精度量化；
// 3) 行情/成交统一转换与去重。
bool IsReplayMode(const BybitAdapterOptions& options) {
  return options.mode == "replay";
}

std::string ReadEnvOrEmpty(const char* key) {
  if (key == nullptr) {
    return {};
  }
  const char* value = std::getenv(key);
  if (value == nullptr) {
    return {};
  }
  return value;
}

/**
 * @brief 解析 Bybit API 密钥
 * 优先级：Demo > Testnet > Mainnet > 通用回退
 */
void ResolveBybitCredentials(bool testnet,
                             bool demo_trading,
                             std::string* out_api_key,
                             std::string* out_api_secret) {
  if (out_api_key == nullptr || out_api_secret == nullptr) {
    return;
  }

  std::string api_key;
  std::string api_secret;
  if (demo_trading) {
    api_key = ReadEnvOrEmpty("AI_TRADE_BYBIT_DEMO_API_KEY");
    api_secret = ReadEnvOrEmpty("AI_TRADE_BYBIT_DEMO_API_SECRET");
  } else if (testnet) {
    api_key = ReadEnvOrEmpty("AI_TRADE_BYBIT_TESTNET_API_KEY");
    api_secret = ReadEnvOrEmpty("AI_TRADE_BYBIT_TESTNET_API_SECRET");
  } else {
    api_key = ReadEnvOrEmpty("AI_TRADE_BYBIT_MAINNET_API_KEY");
    api_secret = ReadEnvOrEmpty("AI_TRADE_BYBIT_MAINNET_API_SECRET");
  }

  // 兼容旧配置：若未提供按环境分离的变量，则回退到通用变量。
  if (api_key.empty()) {
    api_key = ReadEnvOrEmpty("AI_TRADE_API_KEY");
  }
  if (api_secret.empty()) {
    api_secret = ReadEnvOrEmpty("AI_TRADE_API_SECRET");
  }

  *out_api_key = std::move(api_key);
  *out_api_secret = std::move(api_secret);
}

std::string ToUpperCopy(const std::string& text) {
  std::string out = text;
  std::transform(out.begin(),
                 out.end(),
                 out.begin(),
                 [](unsigned char ch) {
                   return static_cast<char>(std::toupper(ch));
                 });
  return out;
}

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

/**
 * @brief 判断 Bybit 订单状态是否仍属于“活动中”
 *
 * 说明：
 * - `/v5/order/realtime` 在不同账户形态下可能返回非活动状态订单；
 * - 对账“在途单”收敛必须只看活动态，避免已终态订单阻塞本地 pending。
 */
bool IsBybitOrderStatusActive(const std::string& order_status) {
  const std::string normalized = ToUpperCopy(Trim(order_status));
  if (normalized.empty()) {
    // 缺失状态字段时保守放行，由 pending_order_stale_ms 再兜底收敛。
    return true;
  }
  static const std::unordered_set<std::string> kTerminalStatuses{
      "FILLED",
      "CANCELLED",
      "CANCELED",
      "REJECTED",
      "DEACTIVATED",
      "EXPIRED",
      "PARTIALLYFILLEDCANCELED",
      "PARTIALLYFILLEDCANCELLED",
  };
  return kTerminalStatuses.find(normalized) == kTerminalStatuses.end();
}

std::vector<std::string> NormalizeSymbols(const std::vector<std::string>& input,
                                          const std::string& primary_symbol) {
  std::vector<std::string> out;
  std::unordered_set<std::string> seen;
  for (const auto& symbol : input) {
    const std::string normalized = ToUpperCopy(Trim(symbol));
    if (normalized.empty()) {
      continue;
    }
    if (seen.insert(normalized).second) {
      out.push_back(normalized);
    }
  }

  const std::string fallback_primary =
      primary_symbol.empty() ? std::string("BTCUSDT")
                             : ToUpperCopy(Trim(primary_symbol));
  if (seen.insert(fallback_primary).second) {
    out.push_back(fallback_primary);
  }
  if (out.empty()) {
    out.push_back("BTCUSDT");
  }
  return out;
}

std::optional<JsonValue> ParseJsonBody(const std::string& body) {
  JsonValue root;
  std::string parse_error;
  if (!ParseJson(body, &root, &parse_error)) {
    return std::nullopt;
  }
  return root;
}

const JsonValue* JsonResultList(const JsonValue* root) {
  return JsonFindPath(root, {"result", "list"});
}

std::optional<double> JsonNumberField(const JsonValue* object,
                                      const std::string& key) {
  const JsonValue* field = JsonObjectField(object, key);
  if (field == nullptr) {
    return std::nullopt;
  }
  return JsonAsNumber(field);
}

std::optional<std::string> JsonStringField(const JsonValue* object,
                                           const std::string& key) {
  const JsonValue* field = JsonObjectField(object, key);
  if (field == nullptr) {
    return std::nullopt;
  }
  return JsonAsString(field);
}

std::optional<int> JsonIntField(const JsonValue* object,
                                const std::string& key) {
  const JsonValue* field = JsonObjectField(object, key);
  if (field == nullptr) {
    return std::nullopt;
  }
  if (const auto number = JsonAsNumber(field); number.has_value()) {
    return static_cast<int>(*number);
  }
  if (const auto text = JsonAsString(field); text.has_value()) {
    try {
      return std::stoi(*text);
    } catch (const std::exception&) {
      return std::nullopt;
    }
  }
  return std::nullopt;
}

std::int64_t ParseExecTimeMs(const JsonValue* row) {
  if (row == nullptr || row->type != JsonType::kObject) {
    return 0;
  }
  if (const auto raw = JsonStringField(row, "execTime"); raw.has_value()) {
    try {
      return std::stoll(*raw);
    } catch (const std::exception&) {
      return 0;
    }
  }
  if (const auto number = JsonNumberField(row, "execTime"); number.has_value()) {
    return static_cast<std::int64_t>(*number);
  }
  return 0;
}

FillLiquidity ParseFillLiquidity(const JsonValue* row) {
  if (row == nullptr || row->type != JsonType::kObject) {
    return FillLiquidity::kUnknown;
  }
  const JsonValue* maker_field = JsonObjectField(row, "isMaker");
  if (maker_field == nullptr) {
    return FillLiquidity::kUnknown;
  }
  if (const auto maker = JsonAsBool(maker_field); maker.has_value()) {
    return *maker ? FillLiquidity::kMaker : FillLiquidity::kTaker;
  }
  if (const auto maker_number = JsonAsNumber(maker_field);
      maker_number.has_value()) {
    return *maker_number != 0.0 ? FillLiquidity::kMaker
                                : FillLiquidity::kTaker;
  }
  if (const auto maker_text = JsonAsString(maker_field);
      maker_text.has_value()) {
    const std::string normalized = ToUpperCopy(Trim(*maker_text));
    if (normalized == "TRUE" || normalized == "1") {
      return FillLiquidity::kMaker;
    }
    if (normalized == "FALSE" || normalized == "0") {
      return FillLiquidity::kTaker;
    }
  }
  return FillLiquidity::kUnknown;
}

AccountMode ParseAccountMode(const std::string& account_info_body,
                             AccountMode fallback) {
  const std::optional<JsonValue> root = ParseJsonBody(account_info_body);
  if (!root.has_value()) {
    return fallback;
  }

  const JsonValue* unified_margin_status =
      JsonFindPath(&(*root), {"result", "unifiedMarginStatus"});
  if (unified_margin_status == nullptr) {
    return fallback;
  }

  int status = 0;
  if (const auto number = JsonAsNumber(unified_margin_status); number.has_value()) {
    status = static_cast<int>(*number);
  } else if (const auto text = JsonAsString(unified_margin_status);
             text.has_value()) {
    try {
      status = std::stoi(*text);
    } catch (const std::exception&) {
      return fallback;
    }
  } else {
    return fallback;
  }

  return status >= 3 ? AccountMode::kUnified : AccountMode::kClassic;
}

MarginMode ParseMarginMode(const std::string& account_info_body,
                           const std::string& positions_body,
                           MarginMode fallback) {
  if (const std::optional<JsonValue> account_root = ParseJsonBody(account_info_body);
      account_root.has_value()) {
    if (const auto margin_mode =
            JsonAsString(JsonFindPath(&(*account_root), {"result", "marginMode"}));
        margin_mode.has_value()) {
      const std::string normalized = ToUpperCopy(*margin_mode);
      if (normalized.find("PORTFOLIO") != std::string::npos) {
        return MarginMode::kPortfolio;
      }
      if (normalized.find("ISOLATED") != std::string::npos) {
        return MarginMode::kIsolated;
      }
      if (normalized.find("CROSS") != std::string::npos ||
          normalized.find("REGULAR") != std::string::npos) {
        return MarginMode::kCross;
      }
    }
  }

  const std::optional<JsonValue> positions_root = ParseJsonBody(positions_body);
  if (!positions_root.has_value()) {
    return fallback;
  }
  const JsonValue* list = JsonResultList(&(*positions_root));
  if (list == nullptr || list->type != JsonType::kArray) {
    return fallback;
  }

  for (const auto& row : list->array_value) {
    if (row.type != JsonType::kObject) {
      continue;
    }
    if (const auto trade_mode = JsonIntField(&row, "tradeMode");
        trade_mode.has_value()) {
      if (*trade_mode == 1) {
        return MarginMode::kIsolated;
      }
      if (*trade_mode == 0) {
        return MarginMode::kCross;
      }
    }
  }

  return fallback;
}

PositionMode ParsePositionMode(const std::string& positions_body,
                               PositionMode fallback) {
  const std::optional<JsonValue> positions_root = ParseJsonBody(positions_body);
  if (!positions_root.has_value()) {
    return fallback;
  }
  const JsonValue* list = JsonResultList(&(*positions_root));
  if (list == nullptr || list->type != JsonType::kArray) {
    return fallback;
  }

  bool has_hedge_idx = false;
  bool has_oneway_idx = false;
  for (const auto& row : list->array_value) {
    if (row.type != JsonType::kObject) {
      continue;
    }
    if (const auto position_idx = JsonIntField(&row, "positionIdx");
        position_idx.has_value()) {
      if (*position_idx == 0) {
        has_oneway_idx = true;
      } else if (*position_idx == 1 || *position_idx == 2) {
        has_hedge_idx = true;
      }
    }
  }

  if (has_hedge_idx) {
    return PositionMode::kHedge;
  }
  if (has_oneway_idx) {
    return PositionMode::kOneWay;
  }
  return fallback;
}

int SideToDirection(const std::string& side) {
  const std::string normalized = ToUpperCopy(side);
  if (normalized == "BUY") {
    return 1;
  }
  if (normalized == "SELL") {
    return -1;
  }
  return 0;
}

std::string EscapeJson(const std::string& raw) {
  std::string out;
  out.reserve(raw.size() + 8);
  for (char ch : raw) {
    if (ch == '\\' || ch == '"') {
      out.push_back('\\');
    }
    out.push_back(ch);
  }
  return out;
}

std::string ToDecimalString(double value, int precision = 8) {
  const int clamped_precision = std::clamp(precision, 0, 12);
  std::ostringstream oss;
  oss << std::fixed << std::setprecision(clamped_precision) << value;
  std::string text = oss.str();
  while (!text.empty() && text.back() == '0') {
    text.pop_back();
  }
  if (!text.empty() && text.back() == '.') {
    text.pop_back();
  }
  return text.empty() ? "0" : text;
}

std::int64_t Pow10Int(int exp) {
  std::int64_t result = 1;
  for (int i = 0; i < exp; ++i) {
    if (result > static_cast<std::int64_t>(9'000'000'000'000'000'000LL / 10LL)) {
      return 0;
    }
    result *= 10;
  }
  return result;
}

bool ParseDecimalToUnits(const std::string& raw,
                         std::int64_t* out_units,
                         std::int64_t* out_scale,
                         int* out_precision) {
  if (out_units == nullptr || out_scale == nullptr || out_precision == nullptr) {
    return false;
  }
  const std::string text = Trim(raw);
  if (text.empty()) {
    return false;
  }

  std::size_t pos = 0;
  bool negative = false;
  if (text[pos] == '+' || text[pos] == '-') {
    negative = text[pos] == '-';
    ++pos;
  }
  if (negative || pos >= text.size()) {
    return false;
  }

  const std::size_t dot = text.find('.', pos);
  std::string int_part = (dot == std::string::npos)
                             ? text.substr(pos)
                             : text.substr(pos, dot - pos);
  std::string frac_part = (dot == std::string::npos)
                              ? std::string()
                              : text.substr(dot + 1);

  if (int_part.empty()) {
    int_part = "0";
  }
  if (int_part.find_first_not_of("0123456789") != std::string::npos ||
      (!frac_part.empty() &&
       frac_part.find_first_not_of("0123456789") != std::string::npos)) {
    return false;
  }

  const int precision = static_cast<int>(frac_part.size());
  if (precision > 12) {
    return false;
  }
  const std::int64_t scale = Pow10Int(precision);
  if (scale <= 0) {
    return false;
  }

  std::int64_t int_units = 0;
  for (char ch : int_part) {
    const int digit = ch - '0';
    if (int_units > (std::numeric_limits<std::int64_t>::max() - digit) / 10) {
      return false;
    }
    int_units = int_units * 10 + digit;
  }

  std::int64_t frac_units = 0;
  for (char ch : frac_part) {
    const int digit = ch - '0';
    if (frac_units > (std::numeric_limits<std::int64_t>::max() - digit) / 10) {
      return false;
    }
    frac_units = frac_units * 10 + digit;
  }

  if (int_units > (std::numeric_limits<std::int64_t>::max() - frac_units) / scale) {
    return false;
  }
  *out_units = int_units * scale + frac_units;
  *out_scale = scale;
  *out_precision = precision;
  return true;
}

double QuantizeDownToStep(double qty, const BybitSymbolTradeRule& rule) {
  if (qty <= 0.0) {
    return qty;
  }
  if (rule.qty_scale > 0 && rule.qty_step_units > 0) {
    const long double scaled_ld =
        std::floor(static_cast<long double>(qty) *
                   static_cast<long double>(rule.qty_scale) + 1e-12L);
    if (scaled_ld <= 0.0L) {
      return 0.0;
    }
    const std::int64_t scaled_units = static_cast<std::int64_t>(scaled_ld);
    const std::int64_t quantized_units =
        (scaled_units / rule.qty_step_units) * rule.qty_step_units;
    if (quantized_units <= 0) {
      return 0.0;
    }
    return static_cast<double>(quantized_units) /
           static_cast<double>(rule.qty_scale);
  }

  if (rule.qty_step <= 0.0) {
    return qty;
  }
  const double units = std::floor((qty + 1e-12) / rule.qty_step);
  return units * rule.qty_step;
}

double QuantizePassivePrice(double price,
                            int direction,
                            const BybitSymbolTradeRule& rule) {
  if (price <= 0.0) {
    return price;
  }
  if (direction == 0) {
    return price;
  }
  if (rule.price_scale > 0 && rule.price_tick_units > 0) {
    const long double scaled_raw =
        static_cast<long double>(price) *
        static_cast<long double>(rule.price_scale);
    const std::int64_t scaled_units = direction > 0
                                          ? static_cast<std::int64_t>(
                                                std::floor(scaled_raw + 1e-12L))
                                          : static_cast<std::int64_t>(
                                                std::ceil(scaled_raw - 1e-12L));
    if (scaled_units <= 0) {
      return 0.0;
    }
    std::int64_t aligned_units = 0;
    if (direction > 0) {
      aligned_units =
          (scaled_units / rule.price_tick_units) * rule.price_tick_units;
    } else {
      aligned_units = ((scaled_units + rule.price_tick_units - 1) /
                       rule.price_tick_units) *
                      rule.price_tick_units;
    }
    if (aligned_units <= 0) {
      return 0.0;
    }
    return static_cast<double>(aligned_units) /
           static_cast<double>(rule.price_scale);
  }
  if (rule.price_tick > 0.0) {
    const double units = direction > 0
                             ? std::floor((price + 1e-12) / rule.price_tick)
                             : std::ceil((price - 1e-12) / rule.price_tick);
    if (units <= 0.0) {
      return 0.0;
    }
    return units * rule.price_tick;
  }
  return price;
}

bool IsStepAligned(double qty, const BybitSymbolTradeRule& rule) {
  if (qty <= 0.0) {
    return false;
  }
  if (rule.qty_scale > 0 && rule.qty_step_units > 0) {
    const long double scaled_ld =
        std::round(static_cast<long double>(qty) *
                   static_cast<long double>(rule.qty_scale));
    const std::int64_t scaled_units = static_cast<std::int64_t>(scaled_ld);
    return scaled_units > 0 &&
           (scaled_units % rule.qty_step_units) == 0;
  }
  if (rule.qty_step > 0.0) {
    const double units = qty / rule.qty_step;
    return std::fabs(units - std::round(units)) < 1e-9;
  }
  return true;
}

/**
 * @brief 解析 Bybit 交易规则 (Instrument Info)
 * 重点关注：最小下单量、价格精度、数量精度、最小名义价值
 */
bool ParseBybitSymbolTradeRule(const JsonValue* instrument,
                               BybitSymbolTradeRule* out_rule) {
  if (instrument == nullptr ||
      instrument->type != JsonType::kObject ||
      out_rule == nullptr) {
    return false;
  }
  const JsonValue* lot_filter = JsonObjectField(instrument, "lotSizeFilter");
  if (lot_filter == nullptr || lot_filter->type != JsonType::kObject) {
    return false;
  }

  BybitSymbolTradeRule rule;
  const std::string status =
      JsonStringField(instrument, "status").value_or("Trading");
  rule.tradable = (ToUpperCopy(status) == "TRADING");

  const std::string qty_step_raw =
      JsonStringField(lot_filter, "qtyStep")
          .value_or(ToDecimalString(
              JsonNumberField(lot_filter, "qtyStep").value_or(0.0), 12));
  rule.qty_step = JsonNumberField(lot_filter, "qtyStep").value_or(0.0);
  rule.min_order_qty = JsonNumberField(lot_filter, "minOrderQty").value_or(0.0);
  rule.max_mkt_order_qty =
      JsonNumberField(lot_filter, "maxMktOrderQty")
          .value_or(JsonNumberField(lot_filter, "maxOrderQty").value_or(0.0));
  rule.min_notional_value =
      JsonNumberField(lot_filter, "minNotionalValue").value_or(0.0);
  std::int64_t qty_step_units = 0;
  std::int64_t qty_scale = 0;
  int qty_precision = 0;
  if (ParseDecimalToUnits(qty_step_raw, &qty_step_units, &qty_scale, &qty_precision) &&
      qty_step_units > 0 && qty_scale > 0) {
    rule.qty_step_units = qty_step_units;
    rule.qty_scale = qty_scale;
    rule.qty_precision = qty_precision;
    rule.qty_step =
        static_cast<double>(qty_step_units) / static_cast<double>(qty_scale);
  }

  const JsonValue* price_filter = JsonObjectField(instrument, "priceFilter");
  if (price_filter != nullptr && price_filter->type == JsonType::kObject) {
    const std::string tick_size_raw =
        JsonStringField(price_filter, "tickSize")
            .value_or(ToDecimalString(
                JsonNumberField(price_filter, "tickSize").value_or(0.0), 12));
    rule.price_tick = JsonNumberField(price_filter, "tickSize").value_or(0.0);

    std::int64_t tick_units = 0;
    std::int64_t tick_scale = 0;
    int tick_precision = 0;
    if (ParseDecimalToUnits(tick_size_raw, &tick_units, &tick_scale, &tick_precision) &&
        tick_units > 0 && tick_scale > 0) {
      rule.price_tick_units = tick_units;
      rule.price_scale = tick_scale;
      rule.price_precision = tick_precision;
      rule.price_tick =
          static_cast<double>(tick_units) / static_cast<double>(tick_scale);
    }
  }

  if (rule.qty_step <= 0.0 && rule.min_order_qty <= 0.0 &&
      rule.max_mkt_order_qty <= 0.0 && rule.min_notional_value <= 0.0 &&
      rule.price_tick <= 0.0) {
    return false;
  }
  *out_rule = rule;
  return true;
}

bool LoadTradeRuleForSymbol(BybitRestClient* rest_client,
                            const std::string& category,
                            const std::string& symbol,
                            BybitSymbolTradeRule* out_rule,
                            std::string* out_error) {
  if (rest_client == nullptr || out_rule == nullptr) {
    if (out_error != nullptr) {
      *out_error = "LoadTradeRuleForSymbol 参数为空";
    }
    return false;
  }

  const std::string query = "category=" + category + "&symbol=" + symbol;
  std::string body;
  std::string error;
  if (!rest_client->GetPublic("/v5/market/instruments-info", query,
                              &body, &error)) {
    if (out_error != nullptr) {
      *out_error = error;
    }
    return false;
  }

  const std::optional<JsonValue> root = ParseJsonBody(body);
  if (!root.has_value()) {
    if (out_error != nullptr) {
      *out_error = "instruments-info JSON解析失败";
    }
    return false;
  }
  const JsonValue* list = JsonResultList(&(*root));
  if (list == nullptr || list->type != JsonType::kArray ||
      list->array_value.empty()) {
    if (out_error != nullptr) {
      *out_error = "instruments-info 返回空列表";
    }
    return false;
  }

  for (const auto& row : list->array_value) {
    if (row.type != JsonType::kObject) {
      continue;
    }
    const std::string row_symbol =
        JsonStringField(&row, "symbol").value_or(std::string());
    if (!row_symbol.empty() && row_symbol != symbol) {
      continue;
    }
    BybitSymbolTradeRule rule;
    if (ParseBybitSymbolTradeRule(&row, &rule)) {
      *out_rule = rule;
      return true;
    }
  }

  if (out_error != nullptr) {
    *out_error = "instruments-info 未找到可用 lotSizeFilter";
  }
  return false;
}

}  // namespace

/**
 * @brief 建立 Bybit 适配器连接并初始化运行通道
 *
 * 初始化顺序：
 * 1. 归一化 symbol 与本地状态清理；
 * 2. 鉴权检查与 REST 客户端初始化；
 * 3. 拉取交易规则与账户模式快照；
 * 4. 建立 Public/Private WS（失败按配置降级到 REST 轮询）；
 * 5. 预热 execution 游标，避免重启后误消费历史成交。
 */
bool BybitExchangeAdapter::Connect() {
  options_.symbols = NormalizeSymbols(options_.symbols, options_.primary_symbol);
  observed_exec_ids_.clear();
  pending_fills_.clear();
  pending_markets_.clear();
  last_public_ws_reconnect_attempt_ms_ = 0;
  last_private_ws_reconnect_attempt_ms_ = 0;
  execution_watermark_ms_ = 0;
  execution_cursor_primed_ = false;
  if (options_.replay_prices.empty()) {
    // 回放模式兜底价格序列
    options_.replay_prices = {100.0, 100.5, 100.3, 100.8, 100.4, 100.9};
  }

  if (options_.testnet && options_.demo_trading) {
    connected_ = false;
    LogInfo("Bybit 连接失败：demo_trading=true 不允许与 testnet=true 同时使用");
    return false;
  }

  const bool replay_mode = IsReplayMode(options_);
  std::string api_key;
  std::string api_secret;
  ResolveBybitCredentials(options_.testnet,
                          options_.demo_trading,
                          &api_key,
                          &api_secret);

  if (api_key.empty() || api_secret.empty()) {
    if (replay_mode && options_.allow_no_auth_in_replay) {
      connected_ = true;
      market_channel_ = MarketChannel::kReplay;
      fill_channel_ = FillChannel::kReplay;
      account_snapshot_ = ExchangeAccountSnapshot{
          .account_mode = options_.remote_account_mode,
          .margin_mode = options_.remote_margin_mode,
          .position_mode = options_.remote_position_mode,
      };
      LogInfo("bybit_stub 以无鉴权回放模式启动（仅本地骨架）: symbols=" +
              std::to_string(options_.symbols.size()));
      return true;
    }
    connected_ = false;
    LogInfo("Bybit 连接失败：缺少API密钥。请设置 demo/testnet/mainnet 专用变量，"
            "或回退使用 AI_TRADE_API_KEY/AI_TRADE_API_SECRET");
    return false;
  }

  if (replay_mode) {
    connected_ = true;
    market_channel_ = MarketChannel::kReplay;
    fill_channel_ = FillChannel::kReplay;
    account_snapshot_ = ExchangeAccountSnapshot{
        .account_mode = options_.remote_account_mode,
        .margin_mode = options_.remote_margin_mode,
        .position_mode = options_.remote_position_mode,
    };
    LogInfo("bybit_stub 回放模式启动: symbols=" +
            std::to_string(options_.symbols.size()));
    return true;
  }

  // 初始化 HTTP 传输层 (便于 Mock 注入)
  std::unique_ptr<BybitHttpTransport> transport;
  if (options_.http_transport_factory) {
    transport = options_.http_transport_factory();
  }
  rest_client_ = std::make_unique<BybitRestClient>(
      api_key,
      api_secret,
      options_.testnet,
      options_.demo_trading,
      std::move(transport));

  // 1. 加载交易规则 (Instrument Info)
  symbol_trade_rules_.clear();
  for (const auto& symbol : options_.symbols) {
    BybitSymbolTradeRule rule;
    std::string rule_error;
    if (!LoadTradeRuleForSymbol(rest_client_.get(),
                                options_.category,
                                symbol,
                                &rule,
                                &rule_error)) {
      LogInfo("Bybit 交易规则读取失败(" + symbol + "): " + rule_error +
              "，将使用原始下单数量");
      continue;
    }
    symbol_trade_rules_[symbol] = rule;
    LogInfo("Bybit 交易规则加载成功(" + symbol + "): qty_step=" +
            ToDecimalString(rule.qty_step) +
            ", min_order_qty=" + ToDecimalString(rule.min_order_qty) +
            ", price_tick=" + ToDecimalString(rule.price_tick) +
            ", min_notional=" + ToDecimalString(rule.min_notional_value) +
            ", tradable=" + std::string(rule.tradable ? "true" : "false"));
  }

  // 2. 加载账户与持仓模式 (Account Info)
  std::string account_info_body;
  std::string account_info_error;
  if (!rest_client_->GetPrivate("/v5/account/info", "", &account_info_body,
                                &account_info_error)) {
    connected_ = false;
    LogInfo("Bybit 账户信息读取失败: " + account_info_error);
    return false;
  }

  std::string positions_body;
  std::string positions_error;
  const std::string position_query =
      "category=" + options_.category + "&settleCoin=USDT";
  if (!rest_client_->GetPrivate("/v5/position/list", position_query,
                                &positions_body, &positions_error)) {
    positions_body.clear();
    LogInfo("Bybit 持仓信息读取失败，使用配置兜底: " + positions_error);
  }

  account_snapshot_.account_mode =
      ParseAccountMode(account_info_body, options_.remote_account_mode);
  account_snapshot_.margin_mode =
      ParseMarginMode(account_info_body, positions_body, options_.remote_margin_mode);
  account_snapshot_.position_mode =
      ParsePositionMode(positions_body, options_.remote_position_mode);

  // 3. 初始化行情通道 (Public WS 优先 -> REST 降级)
  market_channel_ = MarketChannel::kRestPolling;
  if (options_.public_ws_enabled) {
    BybitPublicStreamOptions ws_options;
    ws_options.testnet = options_.testnet;
    ws_options.enabled = true;
    ws_options.category = options_.category;
    ws_options.symbols = options_.symbols;

    if (options_.public_stream_factory) {
      public_stream_ = options_.public_stream_factory(ws_options);
    } else {
      public_stream_ = std::make_unique<BybitPublicStream>(std::move(ws_options));
    }

    std::string ws_error;
    if (public_stream_ != nullptr && public_stream_->Connect(&ws_error)) {
      market_channel_ = MarketChannel::kPublicWs;
      LogInfo("Bybit 公共WS连接成功：tickers 通道已启用");
    } else if (options_.public_ws_rest_fallback) {
      market_channel_ = MarketChannel::kRestPolling;
      LogInfo("BYBIT_PUBLIC_WS_DEGRADED: " + ws_error +
              ", 切换到 REST market/tickers 轮询");
    } else {
      connected_ = false;
      LogInfo("Bybit 公共WS连接失败且禁止回退: " + ws_error);
      return false;
    }
  } else {
    public_stream_.reset();
  }

  // 4. 初始化成交通道 (Private WS 优先 -> REST 降级)
  fill_channel_ = FillChannel::kRestPolling;
  if (options_.private_ws_enabled) {
    BybitPrivateStreamOptions ws_options;
    ws_options.testnet = options_.testnet;
    ws_options.demo_trading = options_.demo_trading;
    ws_options.enabled = true;
    ws_options.category = options_.category;
    ws_options.api_key = api_key;
    ws_options.api_secret = api_secret;

    if (options_.private_stream_factory) {
      private_stream_ = options_.private_stream_factory(ws_options);
    } else {
      private_stream_ = std::make_unique<BybitPrivateStream>(std::move(ws_options));
    }

    std::string ws_error;
    if (private_stream_ != nullptr && private_stream_->Connect(&ws_error)) {
      fill_channel_ = FillChannel::kPrivateWs;
      LogInfo("Bybit 私有WS连接成功：execution 通道已启用");
    } else if (options_.private_ws_rest_fallback) {
      fill_channel_ = FillChannel::kRestPolling;
      LogInfo("BYBIT_PRIVATE_WS_DEGRADED: " + ws_error +
              ", 切换到 REST execution/list 轮询");
    } else {
      connected_ = false;
      LogInfo("Bybit 私有WS连接失败且禁止回退: " + ws_error);
      return false;
    }
  } else {
    private_stream_.reset();
  }

  // 5. 预热成交游标 (防止重启后重复消费历史成交)
  if (options_.execution_skip_history_on_start) {
    if (!PrimeExecutionCursor()) {
      LogInfo("BYBIT_EXEC_CURSOR_PRIME_DEGRADED: 启动游标预热失败，后续将依赖execTime水位过滤");
    }
  }

  connected_ = true;

  std::string market_channel_name = "rest_polling";
  if (market_channel_ == MarketChannel::kPublicWs) {
    market_channel_name = "public_ws";
  }
  std::string fill_channel_name = "rest_polling";
  if (fill_channel_ == FillChannel::kPrivateWs) {
    fill_channel_name = "private_ws";
  }

  LogInfo("Bybit 已连接: category=" + options_.category +
          ", accountType=" + options_.account_type +
          ", testnet=" + std::string(options_.testnet ? "true" : "false") +
          ", demo_trading=" + std::string(options_.demo_trading ? "true" : "false") +
          ", symbols=" + std::to_string(options_.symbols.size()) +
          ", market_channel=" + market_channel_name +
          ", fill_channel=" + fill_channel_name);
  return true;
}

std::string BybitExchangeAdapter::Name() const {
  if (IsReplayMode(options_)) {
    return "bybit_stub";
  }
  if (market_channel_ == MarketChannel::kPublicWs &&
      fill_channel_ == FillChannel::kPrivateWs) {
    return "bybit_ws";
  }
  if (market_channel_ == MarketChannel::kPublicWs ||
      fill_channel_ == FillChannel::kPrivateWs) {
    return "bybit_mixed";
  }
  return "bybit_rest";
}

bool BybitExchangeAdapter::TradeOk() const {
  // 交易健康由“连接状态 + 当前通道健康 + 是否允许降级”共同决定。
  if (!connected_) {
    return false;
  }
  if (fill_channel_ == FillChannel::kPrivateWs &&
      private_stream_ != nullptr &&
      !private_stream_->Healthy() &&
      !options_.private_ws_rest_fallback) {
    return false;
  }
  if (market_channel_ == MarketChannel::kPublicWs &&
      public_stream_ != nullptr &&
      !public_stream_->Healthy() &&
      !options_.public_ws_rest_fallback) {
    return false;
  }
  return true;
}

std::int64_t BybitExchangeAdapter::CurrentTimestampMs() {
  const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now());
  return now.time_since_epoch().count();
}

void BybitExchangeAdapter::MaybeReconnectPublicWs() {
  if (!connected_ ||
      market_channel_ != MarketChannel::kRestPolling ||
      !options_.public_ws_enabled ||
      public_stream_ == nullptr) {
    return;
  }

  if (options_.ws_reconnect_interval_ms > 0) {
    const std::int64_t now_ms = CurrentTimestampMs();
    if (last_public_ws_reconnect_attempt_ms_ > 0 &&
        now_ms - last_public_ws_reconnect_attempt_ms_ <
            options_.ws_reconnect_interval_ms) {
      return;
    }
    last_public_ws_reconnect_attempt_ms_ = now_ms;
  }

  std::string reconnect_error;
  if (public_stream_->Connect(&reconnect_error)) {
    market_channel_ = MarketChannel::kPublicWs;
    LogInfo("BYBIT_PUBLIC_WS_RECOVERED: 重连成功，切回 public_ws 行情通道");
    return;
  }

  LogInfo("BYBIT_PUBLIC_WS_RECONNECT_FAILED: " + reconnect_error);
}

void BybitExchangeAdapter::MaybeReconnectPrivateWs() {
  if (!connected_ ||
      fill_channel_ != FillChannel::kRestPolling ||
      !options_.private_ws_enabled ||
      private_stream_ == nullptr) {
    return;
  }

  if (options_.ws_reconnect_interval_ms > 0) {
    const std::int64_t now_ms = CurrentTimestampMs();
    if (last_private_ws_reconnect_attempt_ms_ > 0 &&
        now_ms - last_private_ws_reconnect_attempt_ms_ <
            options_.ws_reconnect_interval_ms) {
      return;
    }
    last_private_ws_reconnect_attempt_ms_ = now_ms;
  }

  std::string reconnect_error;
  if (private_stream_->Connect(&reconnect_error)) {
    fill_channel_ = FillChannel::kPrivateWs;
    LogInfo("BYBIT_PRIVATE_WS_RECOVERED: 重连成功，切回 private_ws 成交通道");
    return;
  }

  LogInfo("BYBIT_PRIVATE_WS_RECONNECT_FAILED: " + reconnect_error);
}

std::string BybitExchangeAdapter::ChannelHealthSummary() const {
  const auto bool_to_string = [](bool value) -> const char* {
    return value ? "true" : "false";
  };
  std::string market_channel_name = "rest_polling";
  if (market_channel_ == MarketChannel::kReplay) {
    market_channel_name = "replay";
  } else if (market_channel_ == MarketChannel::kPublicWs) {
    market_channel_name = "public_ws";
  }

  std::string fill_channel_name = "rest_polling";
  if (fill_channel_ == FillChannel::kReplay) {
    fill_channel_name = "replay";
  } else if (fill_channel_ == FillChannel::kPrivateWs) {
    fill_channel_name = "private_ws";
  }

  std::string public_ws_health = "n/a";
  if (market_channel_ == MarketChannel::kPublicWs && public_stream_ != nullptr) {
    public_ws_health = bool_to_string(public_stream_->Healthy());
  }

  std::string private_ws_health = "n/a";
  if (fill_channel_ == FillChannel::kPrivateWs && private_stream_ != nullptr) {
    private_ws_health = bool_to_string(private_stream_->Healthy());
  }

  return "market_channel=" + market_channel_name +
         ", fill_channel=" + fill_channel_name +
         ", public_ws_healthy=" + public_ws_health +
         ", private_ws_healthy=" + private_ws_health;
}

bool BybitExchangeAdapter::PollMarketFromRest(MarketEvent* out_event) {
  if (rest_client_ == nullptr || out_event == nullptr) {
    return false;
  }

  if (pending_markets_.empty()) {
    // 批量拉取 symbol 行情，写入 pending 队列后逐条吐出。
    for (const auto& symbol : options_.symbols) {
      const std::string query =
          "category=" + options_.category + "&symbol=" + symbol;
      std::string body;
      std::string error;
      if (!rest_client_->GetPublic("/v5/market/tickers", query, &body, &error)) {
        LogInfo("Bybit 行情拉取失败(" + symbol + "): " + error);
        continue;
      }

      const std::optional<JsonValue> root = ParseJsonBody(body);
      if (!root.has_value()) {
        continue;
      }
      const JsonValue* list = JsonResultList(&(*root));
      if (list == nullptr || list->type != JsonType::kArray ||
          list->array_value.empty()) {
        continue;
      }

      const JsonValue* row = &list->array_value.front();
      if (row->type != JsonType::kObject) {
        continue;
      }

      const double last_price = JsonNumberField(row, "lastPrice").value_or(0.0);
      if (last_price <= 0.0) {
        continue;
      }
      const double mark_price =
          JsonNumberField(row, "markPrice").value_or(last_price);
      const double final_mark = mark_price > 0.0 ? mark_price : last_price;
      const double volume = JsonNumberField(row, "volume24h").value_or(0.0);
      last_price_by_symbol_[symbol] = final_mark;

      ++replay_seq_;
      pending_markets_.push_back(
          MarketEvent{replay_seq_, symbol, last_price, final_mark, volume});
    }
  }

  if (pending_markets_.empty()) {
    return false;
  }
  *out_event = pending_markets_.front();
  pending_markets_.pop_front();
  return true;
}

/**
 * @brief 行情读取入口
 *
 * 优先级：Replay -> Public WS -> REST 轮询。
 * 当 WS 运行时故障且配置允许回退时，自动切换到 REST。
 */
bool BybitExchangeAdapter::PollMarket(MarketEvent* out_event) {
  if (!connected_ || out_event == nullptr) {
    return false;
  }

  if (IsReplayMode(options_)) {
    if (replay_cursor_ >= options_.replay_prices.size()) {
      return false;
    }
    const std::string& symbol =
        options_.symbols[replay_symbol_cursor_ % options_.symbols.size()];
    ++replay_symbol_cursor_;

    const double price = options_.replay_prices[replay_cursor_++];
    last_price_by_symbol_[symbol] = price;
    ++replay_seq_;
    *out_event = MarketEvent{replay_seq_, symbol, price, price, 10000.0}; // Mock volume
    return true;
  }

  if (market_channel_ == MarketChannel::kPublicWs) {
    if (public_stream_ != nullptr && public_stream_->PollTicker(out_event)) {
      last_price_by_symbol_[out_event->symbol] =
          out_event->mark_price > 0.0 ? out_event->mark_price : out_event->price;
      return true;
    }

    if (public_stream_ == nullptr || !public_stream_->Healthy()) {
      if (!options_.public_ws_rest_fallback) {
        return false;
      }
      market_channel_ = MarketChannel::kRestPolling;
      LogInfo("BYBIT_PUBLIC_WS_DEGRADED: 运行时WS不可用，切换到 REST market/tickers 轮询");
    } else {
      return false;
    }
  }

  // 处于降级模式时，按节流间隔尝试恢复 Public WS。
  if (market_channel_ == MarketChannel::kRestPolling &&
      options_.public_ws_rest_fallback) {
    MaybeReconnectPublicWs();
  }
  if (market_channel_ == MarketChannel::kPublicWs) {
    if (public_stream_ != nullptr && public_stream_->PollTicker(out_event)) {
      last_price_by_symbol_[out_event->symbol] =
          out_event->mark_price > 0.0 ? out_event->mark_price : out_event->price;
      return true;
    }
  }

  if (market_channel_ != MarketChannel::kRestPolling) {
    return false;
  }
  return PollMarketFromRest(out_event);
}

// 启动预热：拉取最近 execution，建立 exec_id 与 execTime 水位基线。
bool BybitExchangeAdapter::PrimeExecutionCursor() {
  if (rest_client_ == nullptr) {
    return false;
  }
  const std::string query =
      "category=" + options_.category + "&limit=" +
      std::to_string(options_.execution_poll_limit);
  std::string body;
  std::string error;
  if (!rest_client_->GetPrivate("/v5/execution/list", query, &body, &error)) {
    LogInfo("BYBIT_EXEC_CURSOR_PRIME_FAILED: " + error);
    return false;
  }

  const std::optional<JsonValue> root = ParseJsonBody(body);
  if (!root.has_value()) {
    LogInfo("BYBIT_EXEC_CURSOR_PRIME_FAILED: execution/list JSON解析失败");
    return false;
  }
  const JsonValue* list = JsonResultList(&(*root));
  if (list == nullptr || list->type != JsonType::kArray) {
    LogInfo("BYBIT_EXEC_CURSOR_PRIME_FAILED: execution/list result.list 非数组");
    return false;
  }

  std::size_t seeded = 0;
  std::int64_t max_exec_time_ms = execution_watermark_ms_;
  for (const auto& row : list->array_value) {
    if (row.type != JsonType::kObject) {
      continue;
    }
    const std::string exec_id =
        JsonStringField(&row, "execId").value_or(std::string());
    if (exec_id.empty()) {
      continue;
    }
    if (observed_exec_ids_.insert(exec_id).second) {
      ++seeded;
    }
    const std::int64_t exec_time_ms = ParseExecTimeMs(&row);
    if (exec_time_ms > max_exec_time_ms) {
      max_exec_time_ms = exec_time_ms;
    }
  }

  execution_watermark_ms_ = max_exec_time_ms;
  execution_cursor_primed_ = true;
  LogInfo("BYBIT_EXEC_CURSOR_PRIMED: seeded_exec_ids=" + std::to_string(seeded) +
          ", watermark_ms=" + std::to_string(execution_watermark_ms_));
  return true;
}

bool BybitExchangeAdapter::PollFillFromReplay(FillEvent* out_fill) {
  if (pending_fills_.empty()) {
    return false;
  }
  return DrainPendingFill(out_fill);
}

bool BybitExchangeAdapter::DrainPendingFill(FillEvent* out_fill) {
  if (out_fill == nullptr || pending_fills_.empty()) {
    return false;
  }
  *out_fill = pending_fills_.front();
  pending_fills_.pop_front();
  CanonicalizeFillClientOrderId(out_fill);
  remote_position_qty_by_symbol_[out_fill->symbol] +=
      static_cast<double>(out_fill->direction) * out_fill->qty;
  return true;
}

void BybitExchangeAdapter::RememberOrderIdMapping(
    const std::string& order_id,
    const std::string& client_order_id) {
  if (order_id.empty() || client_order_id.empty()) {
    return;
  }
  order_id_to_client_id_[order_id] = client_order_id;
}

std::string BybitExchangeAdapter::ResolveClientOrderId(
    const std::string& order_link_id,
    const std::string& order_id) const {
  if (!order_link_id.empty()) {
    if (!order_id.empty()) {
      order_id_to_client_id_[order_id] = order_link_id;
    }
    return order_link_id;
  }
  if (order_id.empty()) {
    return {};
  }
  const auto mapped = order_id_to_client_id_.find(order_id);
  if (mapped != order_id_to_client_id_.end() && !mapped->second.empty()) {
    return mapped->second;
  }
  // 映射缺失时退化为 orderId，至少保证回报可追踪。
  return order_id;
}

void BybitExchangeAdapter::CanonicalizeFillClientOrderId(FillEvent* fill) {
  if (fill == nullptr || fill->client_order_id.empty()) {
    return;
  }
  if (order_symbol_by_client_id_.find(fill->client_order_id) !=
      order_symbol_by_client_id_.end()) {
    return;
  }
  const auto mapped = order_id_to_client_id_.find(fill->client_order_id);
  if (mapped != order_id_to_client_id_.end() && !mapped->second.empty()) {
    fill->client_order_id = mapped->second;
  }
}

bool BybitExchangeAdapter::PollFillFromRest(FillEvent* out_fill) {
  if (rest_client_ == nullptr || out_fill == nullptr) {
    return false;
  }
  // 确保游标已预热
  if (options_.execution_skip_history_on_start && !execution_cursor_primed_) {
    if (!PrimeExecutionCursor()) {
      return false;
    }
  }
  if (pending_fills_.empty()) {
    const std::string query =
        "category=" + options_.category + "&limit=" +
        std::to_string(options_.execution_poll_limit);
    std::string body;
    std::string error;
    if (!rest_client_->GetPrivate("/v5/execution/list", query, &body, &error)) {
      return false;
    }

    const std::optional<JsonValue> root = ParseJsonBody(body);
    if (!root.has_value()) {
      return false;
    }
    const JsonValue* list = JsonResultList(&(*root));
    if (list == nullptr || list->type != JsonType::kArray) {
      return false;
    }

    // 轮询批次内做去重 + 水位过滤，最终写入 pending_fills_。
    for (const auto& row : list->array_value) {
      if (row.type != JsonType::kObject) {
        continue;
      }

      const std::string exec_id = JsonStringField(&row, "execId").value_or(std::string());
      if (exec_id.empty()) {
        continue;
      }
      // 去重检查：如果已处理过该 exec_id，跳过
      if (!observed_exec_ids_.insert(exec_id).second) {
        continue;
      }
      const std::int64_t exec_time_ms = ParseExecTimeMs(&row);
      // 水位检查：如果是启动前的历史成交，跳过
      if (options_.execution_skip_history_on_start &&
          exec_time_ms > 0 &&
          exec_time_ms <= execution_watermark_ms_) {
        continue;
      }

      const std::string side = JsonStringField(&row, "side").value_or(std::string());
      const int direction = SideToDirection(side);
      const double qty = JsonNumberField(&row, "execQty").value_or(0.0);
      const double price = JsonNumberField(&row, "execPrice").value_or(0.0);
      if (direction == 0 || qty <= 0.0 || price <= 0.0) {
        if (exec_time_ms > execution_watermark_ms_) {
          execution_watermark_ms_ = exec_time_ms;
        }
        continue;
      }

      FillEvent fill;
      fill.fill_id = exec_id;
      const std::string order_link_id =
          JsonStringField(&row, "orderLinkId").value_or(std::string());
      const std::string order_id =
          JsonStringField(&row, "orderId").value_or(std::string());
      fill.client_order_id = ResolveClientOrderId(order_link_id, order_id);
      if (fill.client_order_id.empty()) {
        if (exec_time_ms > execution_watermark_ms_) {
          execution_watermark_ms_ = exec_time_ms;
        }
        continue;
      }
      fill.symbol = JsonStringField(&row, "symbol").value_or("BTCUSDT");
      fill.direction = direction;
      fill.qty = qty;
      fill.price = price;
      fill.fee = JsonNumberField(&row, "execFee").value_or(0.0);
      fill.liquidity = ParseFillLiquidity(&row);
      pending_fills_.push_back(std::move(fill));
      if (exec_time_ms > execution_watermark_ms_) {
        execution_watermark_ms_ = exec_time_ms;
      }
    }
  }

  return DrainPendingFill(out_fill);
}

/**
 * @brief 下单入口
 *
 * Replay 模式：本地生成模拟成交。
 * Live/Paper 模式：应用 symbol 规则（步长/最小量/最小名义）后调用 REST 下单。
 */
bool BybitExchangeAdapter::SubmitOrder(const OrderIntent& intent) {
  if (!connected_) {
    return false;
  }
  if (intent.client_order_id.empty() || intent.symbol.empty() ||
      intent.direction == 0 || intent.qty <= 0.0) {
    return false;
  }

  if (IsReplayMode(options_)) {
    double fill_price = intent.price;
    if (fill_price <= 0.0) {
      const auto it = last_price_by_symbol_.find(intent.symbol);
      if (it != last_price_by_symbol_.end() && it->second > 0.0) {
        fill_price = it->second;
      }
    }
    if (fill_price <= 0.0) {
      return false;
    }

    // 回放模式模拟部分成交：拆分为两笔 FillEvent。
    const double first_qty = intent.qty * 0.6;
    const double second_qty = intent.qty - first_qty;

    FillEvent first;
    first.fill_id =
        intent.client_order_id + "-fill-" + std::to_string(fill_seq_++);
    first.client_order_id = intent.client_order_id;
    first.symbol = intent.symbol;
    first.direction = intent.direction;
    first.qty = first_qty;
    first.price = fill_price;
    pending_fills_.push_back(first);

    if (second_qty > 1e-9) {
      FillEvent second;
      second.fill_id =
          intent.client_order_id + "-fill-" + std::to_string(fill_seq_++);
      second.client_order_id = intent.client_order_id;
      second.symbol = intent.symbol;
      second.direction = intent.direction;
      second.qty = second_qty;
      second.price = fill_price;
      pending_fills_.push_back(second);
    }
    return true;
  }

  if (rest_client_ == nullptr) {
    return false;
  }
  double submit_qty = intent.qty;
  int qty_precision = 8;
  int price_precision = 8;
  const std::string normalized_symbol = ToUpperCopy(intent.symbol);
  const BybitSymbolTradeRule* submit_rule = nullptr;
  
  // 应用交易规则：先截断，再量化，再做最小名义与最小数量校验。
  const auto rule_it = symbol_trade_rules_.find(normalized_symbol);
  if (rule_it != symbol_trade_rules_.end()) {
    const BybitSymbolTradeRule& rule = rule_it->second;
    submit_rule = &rule;
    if (!rule.tradable) {
      LogInfo("Bybit 下单拒绝：symbol 当前不可交易, symbol=" + normalized_symbol);
      return false;
    }
    qty_precision = rule.qty_precision > 0 ? rule.qty_precision : 8;
    price_precision = rule.price_precision > 0 ? rule.price_precision : 8;

    // 1. 上限截断
    if (rule.max_mkt_order_qty > 0.0) {
      submit_qty = std::min(submit_qty, rule.max_mkt_order_qty);
    }
    // 2. 步长量化 (向下取整)
    if (rule.qty_step > 0.0 || (rule.qty_scale > 0 && rule.qty_step_units > 0)) {
      submit_qty = QuantizeDownToStep(submit_qty, rule);
    }

    double ref_price = intent.price;
    if (ref_price <= 0.0) {
      const auto price_it = last_price_by_symbol_.find(normalized_symbol);
      if (price_it != last_price_by_symbol_.end()) {
        ref_price = price_it->second;
      }
    }

    // 3. 最小名义价值检查 (Min Notional)
    if (rule.min_notional_value > 0.0 &&
        ref_price > 0.0 &&
        !intent.reduce_only &&
        submit_qty * ref_price + 1e-9 < rule.min_notional_value) {
      LogInfo("Bybit 下单拒绝：数量对应名义金额低于最小要求, symbol=" +
              normalized_symbol +
              ", qty=" + ToDecimalString(submit_qty) +
              ", ref_price=" + ToDecimalString(ref_price) +
              ", min_notional=" + ToDecimalString(rule.min_notional_value));
      return false;
    }

    // 4. 最小数量检查 (Min Qty)
    if (rule.min_order_qty > 0.0 &&
        submit_qty + 1e-12 < rule.min_order_qty) {
      LogInfo("Bybit 下单拒绝：数量低于最小下单数量, symbol=" +
              normalized_symbol +
              ", qty=" + ToDecimalString(submit_qty, qty_precision) +
              ", min_order_qty=" + ToDecimalString(rule.min_order_qty, qty_precision));
      return false;
    }

    // 5. 步长对齐检查 (Double Check)
    if (!IsStepAligned(submit_qty, rule)) {
      LogInfo("Bybit 下单拒绝：数量不是qtyStep的整数倍, symbol=" +
              normalized_symbol +
              ", qty=" + ToDecimalString(submit_qty, qty_precision) +
              ", qty_step=" + ToDecimalString(rule.qty_step, qty_precision));
      return false;
    }
  }

  if (submit_qty <= 0.0) {
    LogInfo("Bybit 下单拒绝：数量在交易规则量化后<=0, symbol=" + normalized_symbol +
            ", raw_qty=" + ToDecimalString(intent.qty, qty_precision));
    return false;
  }

  std::string order_type = "Market";
  double submit_price = 0.0;
  std::string time_in_force;
  double trigger_price = 0.0;
  int trigger_direction = 0;
  const bool conditional_protection_order =
      intent.purpose == OrderPurpose::kSl || intent.purpose == OrderPurpose::kTp;
  if (conditional_protection_order) {
    trigger_price = intent.price;
    if (trigger_price <= 0.0) {
      LogInfo("Bybit 下单拒绝：保护单 trigger_price 非法, symbol=" +
              normalized_symbol + ", client_order_id=" + intent.client_order_id);
      return false;
    }
    if (intent.purpose == OrderPurpose::kSl) {
      trigger_direction = intent.direction < 0 ? 2 : 1;
    } else {
      trigger_direction = intent.direction < 0 ? 1 : 2;
    }
  }
  const bool allow_maker =
      intent.liquidity_preference != LiquidityPreference::kTaker;
  const bool maker_entry_order =
      !conditional_protection_order && allow_maker &&
      options_.maker_entry_enabled && !intent.reduce_only &&
      intent.purpose == OrderPurpose::kEntry;
  if (maker_entry_order) {
    double reference_price = intent.price;
    if (reference_price <= 0.0) {
      const auto it = last_price_by_symbol_.find(normalized_symbol);
      if (it != last_price_by_symbol_.end()) {
        reference_price = it->second;
      }
    }
    if (reference_price > 0.0) {
      const double offset_ratio =
          std::max(0.0, options_.maker_price_offset_bps) / 10000.0;
      const double target_price =
          intent.direction > 0 ? reference_price * (1.0 - offset_ratio)
                               : reference_price * (1.0 + offset_ratio);
      submit_price = submit_rule != nullptr
                         ? QuantizePassivePrice(target_price,
                                                intent.direction,
                                                *submit_rule)
                         : target_price;
      if (submit_price > 0.0) {
        order_type = "Limit";
        time_in_force = options_.maker_post_only ? "PostOnly" : "GTC";
      }
    } else {
      LogInfo("Bybit maker-first 回退市价: symbol=" + normalized_symbol +
              ", reason=missing_reference_price");
    }
  }

  order_symbol_by_client_id_[intent.client_order_id] = normalized_symbol;
  const std::string side = intent.direction > 0 ? "Buy" : "Sell";
  auto build_order_body = [&](const std::string& submit_order_type,
                              double limit_price,
                              const std::string& submit_time_in_force,
                              double submit_trigger_price,
                              int submit_trigger_direction,
                              bool close_on_trigger) {
    std::string body =
        "{\"category\":\"" + EscapeJson(options_.category) +
        "\",\"symbol\":\"" + EscapeJson(normalized_symbol) +
        "\",\"side\":\"" + side +
        "\",\"orderType\":\"" + submit_order_type + "\"" +
        ",\"qty\":\"" + ToDecimalString(submit_qty, qty_precision) + "\"" +
        ",\"reduceOnly\":" + std::string(intent.reduce_only ? "true" : "false") +
        ",\"orderLinkId\":\"" + EscapeJson(intent.client_order_id) + "\"";
    if (submit_order_type == "Limit" && limit_price > 0.0) {
      body +=
          ",\"price\":\"" + ToDecimalString(limit_price, price_precision) + "\"";
      if (!submit_time_in_force.empty()) {
        body += ",\"timeInForce\":\"" + submit_time_in_force + "\"";
      }
    }
    if (submit_trigger_price > 0.0 && submit_trigger_direction > 0) {
      body += ",\"triggerPrice\":\"" +
              ToDecimalString(submit_trigger_price, price_precision) + "\"";
      body += ",\"triggerDirection\":" + std::to_string(submit_trigger_direction);
      body += ",\"triggerBy\":\"MarkPrice\"";
      if (close_on_trigger) {
        body += ",\"closeOnTrigger\":true";
      }
    }
    body += "}";
    return body;
  };

  LogInfo("BYBIT_SUBMIT: symbol=" + normalized_symbol +
          ", client_order_id=" + intent.client_order_id +
          ", purpose=" + std::to_string(static_cast<int>(intent.purpose)) +
          ", order_type=" + order_type +
          ", liquidity_preference=" + std::string(ToString(intent.liquidity_preference)) +
          ", reduce_only=" + (intent.reduce_only ? std::string("true")
                                                 : std::string("false")) +
          ", qty=" + ToDecimalString(submit_qty, qty_precision) +
          (order_type == "Limit"
               ? ", price=" + ToDecimalString(submit_price, price_precision) +
                     ", time_in_force=" + time_in_force
               : std::string()) +
          (trigger_price > 0.0
               ? ", trigger_price=" +
                     ToDecimalString(trigger_price, price_precision) +
                     ", trigger_direction=" + std::to_string(trigger_direction)
               : std::string()));

  std::string body = build_order_body(order_type,
                                      submit_price,
                                      time_in_force,
                                      trigger_price,
                                      trigger_direction,
                                      conditional_protection_order);
  std::string response;
  std::string error;
  if (!rest_client_->PostPrivate("/v5/order/create", body, &response, &error)) {
    const bool post_only_rejected =
        (error.find("PostOnly") != std::string::npos) ||
        (error.find("post only") != std::string::npos) ||
        (error.find("post-only") != std::string::npos) ||
        (error.find("post_only") != std::string::npos) ||
        (error.find("would be filled immediately") != std::string::npos);
    if (options_.maker_fallback_to_market && maker_entry_order &&
        order_type == "Limit" && options_.maker_post_only && post_only_rejected) {
      std::string fallback_response;
      std::string fallback_error;
      LogInfo("Bybit maker-first 回退市价: symbol=" + normalized_symbol +
              ", client_order_id=" + intent.client_order_id +
              ", reason=post_only_rejected");
      LogInfo("BYBIT_SUBMIT: symbol=" + normalized_symbol +
              ", client_order_id=" + intent.client_order_id +
              ", order_type=Market, reduce_only=" +
              (intent.reduce_only ? std::string("true")
                                  : std::string("false")) +
              ", qty=" + ToDecimalString(submit_qty, qty_precision) +
              ", reason=maker_fallback_post_only");
      const std::string market_body =
          build_order_body("Market", 0.0, "", 0.0, 0, false);
      if (!rest_client_->PostPrivate("/v5/order/create", market_body,
                                     &fallback_response, &fallback_error)) {
        LogInfo("Bybit 下单失败: client_order_id=" + intent.client_order_id +
                ", error=" + fallback_error);
        return false;
      }
      response = fallback_response;
    } else {
      LogInfo("Bybit 下单失败: client_order_id=" + intent.client_order_id +
              ", error=" + error);
      return false;
    }
  }
  // 记录 orderId->clientOrderId 映射，解决私有回报仅携带 orderId 时的本地归一化问题。
  if (const std::optional<JsonValue> root = ParseJsonBody(response);
      root.has_value()) {
    const JsonValue* result = JsonObjectField(&(*root), "result");
    if (result != nullptr && result->type == JsonType::kObject) {
      const std::string order_id =
          JsonStringField(result, "orderId").value_or(std::string());
      const std::string order_link_id =
          JsonStringField(result, "orderLinkId")
              .value_or(intent.client_order_id);
      RememberOrderIdMapping(order_id, order_link_id);
    }
  }
  return true;
}

/**
 * @brief 撤单入口
 *
 * Replay 模式：从本地 pending fill 队列移除；
 * Live/Paper 模式：调用 `/v5/order/cancel`。
 */
bool BybitExchangeAdapter::CancelOrder(const std::string& client_order_id) {
  if (!connected_) {
    return false;
  }

  if (IsReplayMode(options_)) {
    std::deque<FillEvent> kept;
    for (const auto& fill : pending_fills_) {
      if (fill.client_order_id != client_order_id) {
        kept.push_back(fill);
      }
    }
    pending_fills_.swap(kept);
    return true;
  }

  if (rest_client_ == nullptr || client_order_id.empty()) {
    return false;
  }
  auto symbol_it = order_symbol_by_client_id_.find(client_order_id);
  const std::string symbol = (symbol_it == order_symbol_by_client_id_.end())
                                 ? options_.primary_symbol
                                 : symbol_it->second;
  const std::string body =
      "{\"category\":\"" + EscapeJson(options_.category) +
      "\",\"symbol\":\"" + EscapeJson(symbol) +
      "\",\"orderLinkId\":\"" + EscapeJson(client_order_id) + "\"}";
  std::string response;
  std::string error;
  if (!rest_client_->PostPrivate("/v5/order/cancel", body, &response, &error)) {
    // Bybit 110001: 订单不存在或已来不及撤销，按幂等成功处理即可。
    if (error.find("retCode 异常: 110001") != std::string::npos) {
      LogInfo("Bybit 撤单幂等成功: client_order_id=" + client_order_id +
              ", detail=" + error);
      order_symbol_by_client_id_.erase(client_order_id);
      return true;
    }
    LogInfo("Bybit 撤单失败: client_order_id=" + client_order_id +
            ", error=" + error);
    return false;
  }
  order_symbol_by_client_id_.erase(client_order_id);
  return true;
}

/**
 * @brief 成交读取入口
 *
 * 优先级：Replay -> Private WS -> REST 轮询。
 * WS 故障时按配置自动降级到 REST。
 */
bool BybitExchangeAdapter::PollFill(FillEvent* out_fill) {
  if (!connected_ || out_fill == nullptr) {
    return false;
  }
  if (fill_channel_ == FillChannel::kReplay) {
    return PollFillFromReplay(out_fill);
  }

  if (fill_channel_ == FillChannel::kPrivateWs) {
    if (private_stream_ != nullptr && private_stream_->PollExecution(out_fill)) {
      CanonicalizeFillClientOrderId(out_fill);
      // Private WS 重连后可能重复推送历史 execution，这里做全局去重保护。
      if (!observed_exec_ids_.insert(out_fill->fill_id).second) {
        return false;
      }
      remote_position_qty_by_symbol_[out_fill->symbol] +=
          static_cast<double>(out_fill->direction) * out_fill->qty;
      return true;
    }
    if (private_stream_ == nullptr || !private_stream_->Healthy()) {
      if (!options_.private_ws_rest_fallback) {
        return false;
      }
      fill_channel_ = FillChannel::kRestPolling;
      LogInfo("BYBIT_PRIVATE_WS_DEGRADED: 运行时WS不可用，切换到 REST execution/list 轮询");
    } else {
      return false;
    }
  }

  // 处于降级模式时，按节流间隔尝试恢复 Private WS。
  if (fill_channel_ == FillChannel::kRestPolling &&
      options_.private_ws_rest_fallback) {
    MaybeReconnectPrivateWs();
  }
  if (fill_channel_ == FillChannel::kPrivateWs) {
    if (private_stream_ != nullptr && private_stream_->PollExecution(out_fill)) {
      CanonicalizeFillClientOrderId(out_fill);
      if (!observed_exec_ids_.insert(out_fill->fill_id).second) {
        return false;
      }
      remote_position_qty_by_symbol_[out_fill->symbol] +=
          static_cast<double>(out_fill->direction) * out_fill->qty;
      return true;
    }
    return false;
  }

  if (fill_channel_ != FillChannel::kRestPolling) {
    return false;
  }
  if (!PollFillFromRest(out_fill)) {
    return false;
  }
  CanonicalizeFillClientOrderId(out_fill);
  return true;
}

// 获取交易所侧净名义敞口（USD, signed），供对账快速检查使用。
bool BybitExchangeAdapter::GetRemoteNotionalUsd(double* out_notional_usd) const {
  if (!connected_ || out_notional_usd == nullptr) {
    return false;
  }

  if (IsReplayMode(options_)) {
    double total_notional = 0.0;
    for (const auto& [symbol, qty] : remote_position_qty_by_symbol_) {
      const auto it = last_price_by_symbol_.find(symbol);
      if (it == last_price_by_symbol_.end() || it->second <= 0.0) {
        continue;
      }
      total_notional += qty * it->second;
    }
    *out_notional_usd = total_notional;
    return true;
  }

  if (rest_client_ == nullptr) {
    return false;
  }

  const std::string query = "category=" + options_.category + "&settleCoin=USDT";
  std::string body;
  std::string error;
  if (!rest_client_->GetPrivate("/v5/position/list", query, &body, &error)) {
    return false;
  }

  const std::optional<JsonValue> root = ParseJsonBody(body);
  if (!root.has_value()) {
    return false;
  }
  const JsonValue* list = JsonResultList(&(*root));
  if (list == nullptr || list->type != JsonType::kArray) {
    return false;
  }

  double total_notional = 0.0;
  for (const auto& row : list->array_value) {
    if (row.type != JsonType::kObject) {
      continue;
    }
    double signed_notional = JsonNumberField(&row, "positionValue").value_or(0.0);
    const std::string side = JsonStringField(&row, "side").value_or(std::string());
    if (SideToDirection(side) < 0) {
      signed_notional = -signed_notional;
    }
    total_notional += signed_notional;
  }

  *out_notional_usd = total_notional;
  return true;
}

bool BybitExchangeAdapter::GetAccountSnapshot(
    ExchangeAccountSnapshot* out_snapshot) const {
  if (!connected_ || out_snapshot == nullptr) {
    return false;
  }
  *out_snapshot = account_snapshot_;
  return true;
}

bool BybitExchangeAdapter::GetRemotePositions(
    std::vector<RemotePositionSnapshot>* out_positions) const {
  if (!connected_ || out_positions == nullptr) {
    return false;
  }
  out_positions->clear();

  if (IsReplayMode(options_)) {
    for (const auto& [symbol, qty] : remote_position_qty_by_symbol_) {
      if (std::fabs(qty) < 1e-9) {
        continue;
      }
      double mark = 0.0;
      if (const auto it = last_price_by_symbol_.find(symbol);
          it != last_price_by_symbol_.end() && it->second > 0.0) {
        mark = it->second;
      }
      out_positions->push_back(RemotePositionSnapshot{
          .symbol = symbol,
          .qty = qty,
          .avg_entry_price = mark,
          .mark_price = mark,
          .liquidation_price = 0.0,
      });
    }
    return true;
  }

  if (rest_client_ == nullptr) {
    return false;
  }

  const std::string query = "category=" + options_.category + "&settleCoin=USDT";
  std::string body;
  std::string error;
  if (!rest_client_->GetPrivate("/v5/position/list", query, &body, &error)) {
    return false;
  }

  const std::optional<JsonValue> root = ParseJsonBody(body);
  if (!root.has_value()) {
    return false;
  }
  const JsonValue* list = JsonResultList(&(*root));
  if (list == nullptr || list->type != JsonType::kArray) {
    return false;
  }

  // 统一转换为“带方向数量 + 强平价”的内部快照（多>0，空<0）。
  for (const auto& row : list->array_value) {
    if (row.type != JsonType::kObject) {
      continue;
    }
    const std::string symbol =
        ToUpperCopy(JsonStringField(&row, "symbol").value_or(std::string()));
    if (symbol.empty()) {
      continue;
    }

    const double size = JsonNumberField(&row, "size").value_or(0.0);
    if (size <= 0.0) {
      continue;
    }

    int direction = SideToDirection(JsonStringField(&row, "side").value_or(std::string()));
    if (direction == 0) {
      const double signed_notional =
          JsonNumberField(&row, "positionValue").value_or(0.0);
      if (signed_notional > 0.0) {
        direction = 1;
      } else if (signed_notional < 0.0) {
        direction = -1;
      }
    }
    if (direction == 0) {
      continue;
    }

    const double avg_entry_price = JsonNumberField(&row, "avgPrice").value_or(0.0);
    double mark_price = JsonNumberField(&row, "markPrice").value_or(0.0);
    const double liquidation_price =
        JsonNumberField(&row, "liqPrice").value_or(0.0);
    if (mark_price <= 0.0) {
      if (const auto it = last_price_by_symbol_.find(symbol);
          it != last_price_by_symbol_.end() && it->second > 0.0) {
        mark_price = it->second;
      }
    }

    out_positions->push_back(RemotePositionSnapshot{
        .symbol = symbol,
        .qty = static_cast<double>(direction) * size,
        .avg_entry_price = avg_entry_price,
        .mark_price = mark_price,
        .liquidation_price = liquidation_price > 0.0 ? liquidation_price : 0.0,
    });
  }

  return true;
}

bool BybitExchangeAdapter::GetRemoteAccountBalance(
    RemoteAccountBalanceSnapshot* out_balance) const {
  if (!connected_ || out_balance == nullptr) {
    return false;
  }
  if (IsReplayMode(options_)) {
    return false;
  }
  if (rest_client_ == nullptr) {
    return false;
  }

  const std::string query = "accountType=" + options_.account_type;
  std::string body;
  std::string error;
  if (!rest_client_->GetPrivate("/v5/account/wallet-balance", query, &body,
                                &error)) {
    return false;
  }

  const std::optional<JsonValue> root = ParseJsonBody(body);
  if (!root.has_value()) {
    return false;
  }
  const JsonValue* list = JsonResultList(&(*root));
  if (list == nullptr || list->type != JsonType::kArray ||
      list->array_value.empty()) {
    return false;
  }

  const JsonValue* row = &list->array_value.front();
  if (row->type != JsonType::kObject) {
    return false;
  }

  RemoteAccountBalanceSnapshot snapshot;
  if (const auto v = JsonNumberField(row, "totalEquity"); v.has_value()) {
    snapshot.equity_usd = *v;
    snapshot.has_equity = true;
  }
  if (const auto v = JsonNumberField(row, "totalWalletBalance"); v.has_value()) {
    snapshot.wallet_balance_usd = *v;
    snapshot.has_wallet_balance = true;
  }
  if (const auto v = JsonNumberField(row, "totalPerpUPL"); v.has_value()) {
    snapshot.unrealized_pnl_usd = *v;
    snapshot.has_unrealized_pnl = true;
  }

  // 回退逻辑：部分账户类型可能仅返回 coin 级明细。
  const JsonValue* coins = JsonObjectField(row, "coin");
  if (coins != nullptr && coins->type == JsonType::kArray &&
      !coins->array_value.empty()) {
    if (!snapshot.has_wallet_balance) {
      double sum_wallet = 0.0;
      bool has_wallet = false;
      for (const auto& coin : coins->array_value) {
        if (coin.type != JsonType::kObject) {
          continue;
        }
        if (const auto usd = JsonNumberField(&coin, "usdValue");
            usd.has_value()) {
          sum_wallet += *usd;
          has_wallet = true;
          continue;
        }
        if (const auto wallet = JsonNumberField(&coin, "walletBalance");
            wallet.has_value()) {
          sum_wallet += *wallet;
          has_wallet = true;
        }
      }
      if (has_wallet) {
        snapshot.wallet_balance_usd = sum_wallet;
        snapshot.has_wallet_balance = true;
      }
    }
    if (!snapshot.has_unrealized_pnl) {
      double sum_upnl = 0.0;
      bool has_upnl = false;
      for (const auto& coin : coins->array_value) {
        if (coin.type != JsonType::kObject) {
          continue;
        }
        if (const auto upnl = JsonNumberField(&coin, "unrealisedPnl");
            upnl.has_value()) {
          sum_upnl += *upnl;
          has_upnl = true;
        }
      }
      if (has_upnl) {
        snapshot.unrealized_pnl_usd = sum_upnl;
        snapshot.has_unrealized_pnl = true;
      }
    }
  }

  if (!snapshot.has_equity) {
    if (snapshot.has_wallet_balance && snapshot.has_unrealized_pnl) {
      snapshot.equity_usd =
          snapshot.wallet_balance_usd + snapshot.unrealized_pnl_usd;
      snapshot.has_equity = true;
    } else if (const auto v = JsonNumberField(row, "totalMarginBalance");
               v.has_value()) {
      snapshot.equity_usd = *v;
      snapshot.has_equity = true;
    }
  }

  if (!snapshot.has_equity && !snapshot.has_wallet_balance) {
    return false;
  }
  *out_balance = snapshot;
  return true;
}

bool BybitExchangeAdapter::GetRemoteOpenOrderClientIds(
    std::unordered_set<std::string>* out_client_order_ids) const {
  if (!connected_ || out_client_order_ids == nullptr) {
    return false;
  }
  out_client_order_ids->clear();

  if (IsReplayMode(options_)) {
    for (const auto& fill : pending_fills_) {
      if (!fill.client_order_id.empty()) {
        out_client_order_ids->insert(fill.client_order_id);
      }
    }
    return true;
  }

  if (rest_client_ == nullptr) {
    return false;
  }

  const std::string query = "category=" + options_.category +
                            "&openOnly=0&limit=50";
  std::string body;
  std::string error;
  if (!rest_client_->GetPrivate("/v5/order/realtime", query, &body, &error)) {
    return false;
  }

  const std::optional<JsonValue> root = ParseJsonBody(body);
  if (!root.has_value()) {
    return false;
  }
  const JsonValue* list = JsonResultList(&(*root));
  if (list == nullptr || list->type != JsonType::kArray) {
    return false;
  }

  int total_rows = 0;
  int active_rows = 0;
  int skipped_terminal = 0;
  int skipped_zero_leaves = 0;
  int skipped_missing_client_id = 0;
  int mapped_from_order_id = 0;
  for (const auto& row : list->array_value) {
    if (row.type != JsonType::kObject) {
      continue;
    }
    ++total_rows;
    const std::string order_status =
        JsonStringField(&row, "orderStatus").value_or(std::string());
    if (!IsBybitOrderStatusActive(order_status)) {
      ++skipped_terminal;
      continue;
    }
    if (const auto leaves_qty = JsonNumberField(&row, "leavesQty");
        leaves_qty.has_value() && *leaves_qty <= 1e-12) {
      ++skipped_zero_leaves;
      continue;
    }
    const std::string order_link_id =
        JsonStringField(&row, "orderLinkId").value_or(std::string());
    const std::string order_id =
        JsonStringField(&row, "orderId").value_or(std::string());
    if (order_link_id.empty() && !order_id.empty()) {
      const auto mapped = order_id_to_client_id_.find(order_id);
      if (mapped != order_id_to_client_id_.end() && !mapped->second.empty()) {
        ++mapped_from_order_id;
      }
    }
    const std::string client_order_id =
        ResolveClientOrderId(order_link_id, order_id);
    if (client_order_id.empty()) {
      ++skipped_missing_client_id;
      continue;
    }
    out_client_order_ids->insert(client_order_id);
    ++active_rows;
  }

  const int filtered_rows =
      skipped_terminal + skipped_zero_leaves + skipped_missing_client_id;
  if ((filtered_rows > 0 || mapped_from_order_id > 0) &&
      (++open_order_diag_counter_ % 20 == 0)) {
    LogInfo("BYBIT_OPEN_ORDER_FILTER: total=" + std::to_string(total_rows) +
            ", active=" + std::to_string(active_rows) +
            ", skipped_terminal=" + std::to_string(skipped_terminal) +
            ", skipped_zero_leaves=" + std::to_string(skipped_zero_leaves) +
            ", skipped_missing_client_id=" +
            std::to_string(skipped_missing_client_id) +
            ", mapped_from_order_id=" + std::to_string(mapped_from_order_id));
  }

  return true;
}

// symbol 规则查询：供 Universe 过滤与执行层下单前校验复用。
bool BybitExchangeAdapter::GetSymbolInfo(const std::string& symbol,
                                         SymbolInfo* out_info) const {
  if (!connected_ || out_info == nullptr || symbol.empty()) {
    return false;
  }
  const std::string normalized = ToUpperCopy(symbol);
  const auto it = symbol_trade_rules_.find(normalized);
  if (it == symbol_trade_rules_.end()) {
    return false;
  }

  const BybitSymbolTradeRule& rule = it->second;
  out_info->symbol = normalized;
  out_info->tradable = rule.tradable;
  out_info->qty_step = rule.qty_step;
  out_info->min_order_qty = rule.min_order_qty;
  out_info->min_notional_usd = rule.min_notional_value;
  out_info->price_tick = rule.price_tick;
  out_info->qty_precision = rule.qty_precision;
  out_info->price_precision = rule.price_precision;
  return true;
}

}  // namespace ai_trade
