#include "exchange/bybit_public_stream.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cmath>
#include <deque>
#include <exception>
#include <limits>
#include <optional>
#include <sstream>
#include <thread>
#include <unordered_set>
#include <utility>
#include <vector>

#include "core/json_utils.h"

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

std::optional<std::int64_t> JsonInt64Field(const JsonValue* object,
                                           const std::string& key) {
  const JsonValue* field = JsonObjectField(object, key);
  if (field == nullptr) {
    return std::nullopt;
  }
  if (const auto number = JsonAsNumber(field); number.has_value()) {
    return static_cast<std::int64_t>(*number);
  }
  if (const auto text = JsonAsString(field); text.has_value()) {
    try {
      return std::stoll(*text);
    } catch (const std::exception&) {
      return std::nullopt;
    }
  }
  return std::nullopt;
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

std::vector<std::string> NormalizeSymbols(
    const std::vector<std::string>& symbols) {
  std::vector<std::string> out;
  std::unordered_set<std::string> seen;
  for (const auto& symbol : symbols) {
    const std::string normalized = ToUpperCopy(Trim(symbol));
    if (normalized.empty()) {
      continue;
    }
    if (seen.insert(normalized).second) {
      out.push_back(normalized);
    }
  }
  if (out.empty()) {
    out.push_back("BTCUSDT");
  }
  return out;
}

std::string TopicToSymbol(const std::string& topic) {
  static const std::string kPrefix = "tickers.";
  if (topic.rfind(kPrefix, 0) != 0) {
    return std::string();
  }
  return topic.substr(kPrefix.size());
}

}  // namespace

BybitPublicStream::BybitPublicStream(BybitPublicStreamOptions options,
                                     std::unique_ptr<WebsocketClient> ws_client)
    : options_(std::move(options)),
      ws_client_(std::move(ws_client)) {}

/**
 * @brief 建立公共 WS 连接并完成订阅握手
 *
 * 关键步骤：
 * 1. 参数与客户端校验；
 * 2. WS 建连；
 * 3. 发送 subscribe 并等待 ACK；
 * 4. 初始化心跳状态。
 */
bool BybitPublicStream::Connect(std::string* out_error) {
  connected_ = false;
  last_error_.clear();
  pending_events_.clear();
  seq_ = 0;
  last_event_ts_ms_by_symbol_.clear();
  last_volume_24h_by_symbol_.clear();

  options_.symbols = NormalizeSymbols(options_.symbols);

  if (!options_.enabled) {
    last_error_ = "public ws disabled by config";
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }
  if (ws_client_ == nullptr) {
    last_error_ = "public ws client not set";
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  const std::string url = BuildPublicUrl(options_.testnet, options_.category);
  std::string connect_error;
  if (!ws_client_->Connect(url, {}, &connect_error)) {
    last_error_ = connect_error.empty() ? "public ws connect failed"
                                        : connect_error;
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  std::string args;
  for (std::size_t i = 0; i < options_.symbols.size(); ++i) {
    if (i > 0U) {
      args += ",";
    }
    args += "\"tickers." + EscapeJson(options_.symbols[i]) + "\"";
  }
  // 单连接可订阅多个 symbol 的 ticker 主题。
  const std::string subscribe_payload =
      "{\"op\":\"subscribe\",\"args\":[" + args + "]}";

  std::string send_error;
  if (!ws_client_->SendText(subscribe_payload, &send_error)) {
    MarkBroken(send_error.empty() ? "public ws subscribe send failed" : send_error);
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  if (!WaitForSubscribeAck(out_error)) {
    MarkBroken(last_error_);
    return false;
  }

  connected_ = true;
  last_ping_ts_ms_ = CurrentTimestampMs();
  return true;
}

bool BybitPublicStream::Healthy() const {
  return connected_ && ws_client_ != nullptr && ws_client_->IsConnected();
}

/**
 * @brief 拉取一条行情事件
 *
 * 优先返回 pending 队列中的解析结果；若队列为空，则继续拉取 WS 消息并解析。
 */
bool BybitPublicStream::PollTicker(MarketEvent* out_event) {
  if (out_event == nullptr || !Healthy()) {
    return false;
  }
  if (DrainPending(out_event)) {
    return true;
  }

  const std::int64_t now_ms = CurrentTimestampMs();
  // 定期发送 ping，避免被网关空闲断开。
  if (options_.heartbeat_interval_ms > 0 &&
      now_ms - last_ping_ts_ms_ >= options_.heartbeat_interval_ms) {
    std::string ping_error;
    if (!ws_client_->SendText("{\"op\":\"ping\"}", &ping_error)) {
      MarkBroken(ping_error.empty() ? "public ws ping failed" : ping_error);
      return false;
    }
    last_ping_ts_ms_ = now_ms;
  }

  std::string message;
  std::string poll_error;
  const WsPollStatus status = ws_client_->PollText(&message, &poll_error);
  if (status == WsPollStatus::kNoMessage) {
    return false;
  }
  if (status == WsPollStatus::kClosed || status == WsPollStatus::kError) {
    MarkBroken(poll_error.empty() ? "public ws poll failed" : poll_error);
    return false;
  }

  ParseMessage(message);
  return DrainPending(out_event);
}

bool BybitPublicStream::DrainPending(MarketEvent* out_event) {
  if (out_event == nullptr || pending_events_.empty()) {
    return false;
  }
  *out_event = pending_events_.front();
  pending_events_.pop_front();
  return true;
}

bool BybitPublicStream::WaitForSubscribeAck(std::string* out_error) {
  const auto deadline =
      std::chrono::steady_clock::now() +
      std::chrono::milliseconds(std::max(500, options_.ack_timeout_ms));

  while (std::chrono::steady_clock::now() < deadline) {
    std::string message;
    std::string poll_error;
    const WsPollStatus status = ws_client_->PollText(&message, &poll_error);
    if (status == WsPollStatus::kNoMessage) {
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
      continue;
    }
    if (status == WsPollStatus::kClosed || status == WsPollStatus::kError) {
      last_error_ = poll_error.empty() ? "public ws ack failed" : poll_error;
      if (out_error != nullptr) {
        *out_error = last_error_;
      }
      return false;
    }

    JsonValue root;
    std::string parse_error;
    if (!ParseJson(message, &root, &parse_error)) {
      continue;
    }

    const std::optional<std::string> op =
        JsonAsString(JsonObjectField(&root, "op"));
    if (op.has_value() && *op == "subscribe") {
      const std::optional<bool> success =
          JsonAsBool(JsonObjectField(&root, "success"));
      if (success.has_value() && !*success) {
        last_error_ = "public ws subscribe rejected";
        if (const auto ret_msg =
                JsonAsString(JsonObjectField(&root, "ret_msg"));
            ret_msg.has_value()) {
          last_error_ += ": " + *ret_msg;
        }
        if (out_error != nullptr) {
          *out_error = last_error_;
        }
        return false;
      }
      return true;
    }

    ParseMessage(message);
  }

  last_error_ = "public ws subscribe ack timeout";
  if (out_error != nullptr) {
    *out_error = last_error_;
  }
  return false;
}

bool BybitPublicStream::ParseMessage(const std::string& message) {
  // 解析策略：先处理控制帧（ping/pong/subscribe），再处理业务 topic。
  JsonValue root;
  std::string parse_error;
  if (!ParseJson(message, &root, &parse_error)) {
    return false;
  }

  const auto op = JsonAsString(JsonObjectField(&root, "op"));
  if (op.has_value()) {
    if (*op == "ping") {
      std::string pong_error;
      ws_client_->SendText("{\"op\":\"pong\"}", &pong_error);
      return true;
    }
    if (*op == "pong" || *op == "subscribe") {
      return true;
    }
  }

  const auto topic = JsonAsString(JsonObjectField(&root, "topic"));
  if (!topic.has_value()) {
    return false;
  }
  const std::string topic_symbol = TopicToSymbol(*topic);
  if (topic_symbol.empty()) {
    return false;
  }

  const JsonValue* data = JsonObjectField(&root, "data");
  if (data == nullptr) {
    return false;
  }

  auto append_event = [&](const JsonValue* item) {
    if (item == nullptr || item->type != JsonType::kObject) {
      return;
    }
    const std::string symbol =
        JsonStringField(item, "symbol").value_or(topic_symbol);
    const double last_price = JsonNumberField(item, "lastPrice").value_or(0.0);
    if (last_price <= 0.0) {
      return;
    }
    const double mark_price =
        JsonNumberField(item, "markPrice").value_or(last_price);
    const double volume_24h = JsonNumberField(item, "volume24h").value_or(0.0);
    const double funding_rate_8h =
        JsonNumberField(item, "fundingRate")
            .value_or(std::numeric_limits<double>::quiet_NaN());

    std::int64_t event_ts_ms = 0;
    if (const auto row_ts = JsonInt64Field(item, "ts"); row_ts.has_value()) {
      event_ts_ms = *row_ts;
    } else if (const auto root_ts = JsonInt64Field(&root, "ts");
               root_ts.has_value()) {
      event_ts_ms = *root_ts;
    }
    if (event_ts_ms <= 0) {
      event_ts_ms = CurrentTimestampMs();
    }
    std::int64_t interval_ms = 0;
    const auto ts_it = last_event_ts_ms_by_symbol_.find(symbol);
    if (ts_it != last_event_ts_ms_by_symbol_.end() &&
        event_ts_ms > ts_it->second) {
      interval_ms = event_ts_ms - ts_it->second;
    }
    last_event_ts_ms_by_symbol_[symbol] = event_ts_ms;

    double interval_volume = 0.0;
    const auto volume_it = last_volume_24h_by_symbol_.find(symbol);
    if (std::isfinite(volume_24h) && volume_24h >= 0.0) {
      // volume24h 为滚动 24h 指标，不保证单调递增：
      // 1) 若单调上升，优先使用差分估算区间成交量；
      // 2) 若回落或跳变，回退到按 24h 均速折算，避免长时间被错误压成 0。
      constexpr double kOneDayMs = 24.0 * 60.0 * 60.0 * 1000.0;
      if (volume_it != last_volume_24h_by_symbol_.end() &&
          std::isfinite(volume_it->second)) {
        if (volume_24h >= volume_it->second) {
          interval_volume = volume_24h - volume_it->second;
        } else if (interval_ms > 0) {
          interval_volume =
              std::max(0.0, volume_24h) *
              (static_cast<double>(interval_ms) / kOneDayMs);
        }
      } else if (interval_ms > 0) {
        interval_volume =
            std::max(0.0, volume_24h) *
            (static_cast<double>(interval_ms) / kOneDayMs);
      }
      last_volume_24h_by_symbol_[symbol] = volume_24h;
    }

    ++seq_;
    double funding_rate_per_interval =
        std::numeric_limits<double>::quiet_NaN();
    if (std::isfinite(funding_rate_8h) && interval_ms > 0) {
      constexpr double kEightHoursMs = 8.0 * 60.0 * 60.0 * 1000.0;
      funding_rate_per_interval =
          funding_rate_8h *
          (static_cast<double>(interval_ms) / kEightHoursMs);
    }
    // volume 统一口径为“该事件间隔内的增量成交量”，避免把 24h 累计值喂给在线特征。
    pending_events_.push_back(MarketEvent{
        event_ts_ms,
        symbol,
        last_price,
        mark_price,
        std::max(0.0, interval_volume),
        interval_ms,
        funding_rate_per_interval,
    });
  };

  if (data->type == JsonType::kObject) {
    append_event(data);
    return true;
  }
  if (data->type == JsonType::kArray) {
    for (const auto& item : data->array_value) {
      append_event(&item);
    }
    return true;
  }
  return false;
}

void BybitPublicStream::MarkBroken(const std::string& error_message) {
  connected_ = false;
  last_error_ = error_message;
  if (ws_client_ != nullptr) {
    ws_client_->Close();
  }
}

std::string BybitPublicStream::BuildPublicUrl(bool testnet,
                                              const std::string& category) {
  const std::string base =
      testnet ? "wss://stream-testnet.bybit.com"
              : "wss://stream.bybit.com";
  return base + "/v5/public/" + (category.empty() ? "linear" : category);
}

std::int64_t BybitPublicStream::CurrentTimestampMs() {
  const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now());
  return now.time_since_epoch().count();
}

}  // namespace ai_trade
