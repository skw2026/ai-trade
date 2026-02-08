#include "exchange/bybit_private_stream.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <optional>
#include <thread>
#include <utility>

#include "core/json_utils.h"
#include "exchange/bybit_rest_client.h"

namespace ai_trade {

namespace {

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

bool IsAckSuccess(const JsonValue& root) {
  const auto success = JsonAsBool(JsonObjectField(&root, "success"));
  if (success.has_value()) {
    return *success;
  }

  const auto ret_code = JsonAsNumber(JsonObjectField(&root, "retCode"));
  if (ret_code.has_value()) {
    return static_cast<int>(*ret_code) == 0;
  }

  return true;
}

}  // namespace

BybitPrivateStream::BybitPrivateStream(
    BybitPrivateStreamOptions options,
    std::unique_ptr<WebsocketClient> ws_client)
    : options_(std::move(options)), ws_client_(std::move(ws_client)) {}

bool BybitPrivateStream::Connect(std::string* out_error) {
  connected_ = false;
  last_error_.clear();
  pending_fills_.clear();
  seen_exec_ids_.clear();

  if (!options_.enabled) {
    last_error_ = "private ws disabled by config";
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }
  if (options_.api_key.empty() || options_.api_secret.empty()) {
    last_error_ = "missing api key/secret";
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }
  if (ws_client_ == nullptr) {
    last_error_ = "private ws client not set";
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  std::string connect_error;
  if (!ws_client_->Connect(
          BuildPrivateUrl(options_.testnet, options_.demo_trading),
          {},
          &connect_error)) {
    last_error_ = connect_error.empty() ? "private ws connect failed"
                                        : connect_error;
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  const std::int64_t expires_ms = CurrentTimestampMs() + 10000;
  const std::string auth_payload_raw =
      "GET/realtime" + std::to_string(expires_ms);
  std::string signature;
  std::string sign_error;
  if (!BybitRestClient::BuildHmacSha256Hex(options_.api_secret,
                                           auth_payload_raw,
                                           &signature,
                                           &sign_error)) {
    last_error_ = "private ws auth signature failed: " + sign_error;
    MarkBroken(last_error_);
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  const std::string auth_payload =
      "{\"op\":\"auth\",\"args\":[\"" +
      EscapeJson(options_.api_key) + "\"," + std::to_string(expires_ms) +
      ",\"" + signature + "\"]}";

  std::string send_error;
  if (!ws_client_->SendText(auth_payload, &send_error)) {
    last_error_ = send_error.empty() ? "private ws auth send failed" : send_error;
    MarkBroken(last_error_);
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  if (!WaitForAck("auth", options_.ack_timeout_ms, out_error)) {
    MarkBroken(last_error_);
    return false;
  }

  const std::string subscribe_payload =
      "{\"op\":\"subscribe\",\"args\":[\"execution\",\"order\","
      "\"position\",\"wallet\"]}";

  if (!ws_client_->SendText(subscribe_payload, &send_error)) {
    last_error_ = send_error.empty() ? "private ws subscribe send failed"
                                     : send_error;
    MarkBroken(last_error_);
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  if (!WaitForAck("subscribe", options_.ack_timeout_ms, out_error)) {
    MarkBroken(last_error_);
    return false;
  }

  connected_ = true;
  last_ping_ts_ms_ = CurrentTimestampMs();
  return true;
}

bool BybitPrivateStream::Healthy() const {
  return connected_ && ws_client_ != nullptr && ws_client_->IsConnected();
}

bool BybitPrivateStream::PollExecution(FillEvent* out_fill) {
  if (out_fill == nullptr || !Healthy()) {
    return false;
  }
  if (DrainPending(out_fill)) {
    return true;
  }

  const std::int64_t now_ms = CurrentTimestampMs();
  if (options_.heartbeat_interval_ms > 0 &&
      now_ms - last_ping_ts_ms_ >= options_.heartbeat_interval_ms) {
    std::string ping_error;
    if (!ws_client_->SendText("{\"op\":\"ping\"}", &ping_error)) {
      MarkBroken(ping_error.empty() ? "private ws ping failed" : ping_error);
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
    MarkBroken(poll_error.empty() ? "private ws poll failed" : poll_error);
    return false;
  }

  ParseMessage(message);
  return DrainPending(out_fill);
}

bool BybitPrivateStream::DrainPending(FillEvent* out_fill) {
  if (out_fill == nullptr || pending_fills_.empty()) {
    return false;
  }
  *out_fill = pending_fills_.front();
  pending_fills_.pop_front();
  return true;
}

bool BybitPrivateStream::WaitForAck(const std::string& expected_op,
                                    int timeout_ms,
                                    std::string* out_error) {
  const auto deadline =
      std::chrono::steady_clock::now() +
      std::chrono::milliseconds(std::max(500, timeout_ms));

  while (std::chrono::steady_clock::now() < deadline) {
    std::string message;
    std::string poll_error;
    const WsPollStatus status = ws_client_->PollText(&message, &poll_error);
    if (status == WsPollStatus::kNoMessage) {
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
      continue;
    }
    if (status == WsPollStatus::kClosed || status == WsPollStatus::kError) {
      last_error_ = poll_error.empty() ? "private ws ack failed" : poll_error;
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

    const auto op = JsonAsString(JsonObjectField(&root, "op"));
    if (op.has_value() && *op == expected_op) {
      if (!IsAckSuccess(root)) {
        last_error_ = "private ws " + expected_op + " rejected";
        if (const auto ret_msg = JsonAsString(JsonObjectField(&root, "ret_msg"));
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

  last_error_ = "private ws " + expected_op + " ack timeout";
  if (out_error != nullptr) {
    *out_error = last_error_;
  }
  return false;
}

bool BybitPrivateStream::ParseMessage(const std::string& message) {
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
    if (*op == "pong" || *op == "auth" || *op == "subscribe") {
      return true;
    }
  }

  const auto topic = JsonAsString(JsonObjectField(&root, "topic"));
  if (!topic.has_value()) {
    return false;
  }
  if (topic->rfind("execution", 0) != 0) {
    return true;
  }

  const JsonValue* data = JsonObjectField(&root, "data");
  return ParseExecutionPayload(data);
}

bool BybitPrivateStream::ParseExecutionPayload(const JsonValue* data) {
  if (data == nullptr) {
    return false;
  }

  auto consume = [&](const JsonValue* item) {
    if (item == nullptr || item->type != JsonType::kObject) {
      return;
    }

    const std::string exec_id =
        JsonStringField(item, "execId").value_or(std::string());
    if (exec_id.empty()) {
      return;
    }
    if (!seen_exec_ids_.insert(exec_id).second) {
      return;
    }

    const std::string side = JsonStringField(item, "side").value_or(std::string());
    const int direction = SideToDirection(side);
    const double qty = JsonNumberField(item, "execQty").value_or(0.0);
    const double price = JsonNumberField(item, "execPrice").value_or(0.0);
    if (direction == 0 || qty <= 0.0 || price <= 0.0) {
      return;
    }

    FillEvent fill;
    fill.fill_id = exec_id;
    fill.client_order_id =
        JsonStringField(item, "orderLinkId")
            .value_or(JsonStringField(item, "orderId").value_or(std::string()));
    fill.symbol = JsonStringField(item, "symbol").value_or("BTCUSDT");
    fill.direction = direction;
    fill.qty = qty;
    fill.price = price;
    fill.fee = JsonNumberField(item, "execFee").value_or(0.0);
    pending_fills_.push_back(std::move(fill));
  };

  if (data->type == JsonType::kObject) {
    consume(data);
    return true;
  }
  if (data->type == JsonType::kArray) {
    for (const auto& item : data->array_value) {
      consume(&item);
    }
    return true;
  }
  return false;
}

std::string BybitPrivateStream::BuildPrivateUrl(bool testnet,
                                                bool demo_trading) {
  if (demo_trading) {
    return "wss://stream-demo.bybit.com/v5/private";
  }
  return testnet ? "wss://stream-testnet.bybit.com/v5/private"
                 : "wss://stream.bybit.com/v5/private";
}

std::int64_t BybitPrivateStream::CurrentTimestampMs() {
  const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now());
  return now.time_since_epoch().count();
}

int BybitPrivateStream::SideToDirection(const std::string& side) {
  const std::string normalized = ToUpperCopy(side);
  if (normalized == "BUY") {
    return 1;
  }
  if (normalized == "SELL") {
    return -1;
  }
  return 0;
}

void BybitPrivateStream::MarkBroken(const std::string& error_message) {
  connected_ = false;
  last_error_ = error_message;
  if (ws_client_ != nullptr) {
    ws_client_->Close();
  }
}

}  // namespace ai_trade
