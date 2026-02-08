#pragma once

#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <unordered_set>

#include "core/types.h"
#include "exchange/websocket_client.h"

namespace ai_trade {

struct JsonValue;

struct BybitPrivateStreamOptions {
  bool testnet{true};
  bool demo_trading{false};
  bool enabled{true};
  std::string category{"linear"};
  std::string api_key;
  std::string api_secret;
  int ack_timeout_ms{2500};
  int heartbeat_interval_ms{20000};
};

class BybitPrivateStream {
 public:
  explicit BybitPrivateStream(
      BybitPrivateStreamOptions options,
      std::unique_ptr<WebsocketClient> ws_client = CreateCurlWebsocketClient());

  bool Connect(std::string* out_error);
  bool PollExecution(FillEvent* out_fill);
  bool Healthy() const;

  const std::string& last_error() const { return last_error_; }

 private:
  bool DrainPending(FillEvent* out_fill);
  bool WaitForAck(const std::string& expected_op,
                  int timeout_ms,
                  std::string* out_error);
  bool ParseMessage(const std::string& message);
  bool ParseExecutionPayload(const JsonValue* data);

  static std::string BuildPrivateUrl(bool testnet, bool demo_trading);
  static std::int64_t CurrentTimestampMs();
  static int SideToDirection(const std::string& side);

  void MarkBroken(const std::string& error_message);

  BybitPrivateStreamOptions options_;
  std::unique_ptr<WebsocketClient> ws_client_;
  bool connected_{false};
  std::string last_error_;
  std::deque<FillEvent> pending_fills_;
  std::unordered_set<std::string> seen_exec_ids_;
  std::int64_t last_ping_ts_ms_{0};
};

}  // namespace ai_trade
