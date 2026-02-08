#pragma once

#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <vector>

#include "core/types.h"
#include "exchange/websocket_client.h"

namespace ai_trade {

struct BybitPublicStreamOptions {
  bool testnet{true};
  bool enabled{true};
  std::string category{"linear"};
  std::vector<std::string> symbols{"BTCUSDT"};
  int ack_timeout_ms{2500};
  int heartbeat_interval_ms{20000};
};

class BybitPublicStream {
 public:
  explicit BybitPublicStream(
      BybitPublicStreamOptions options,
      std::unique_ptr<WebsocketClient> ws_client = CreateCurlWebsocketClient());

  bool Connect(std::string* out_error);
  bool PollTicker(MarketEvent* out_event);
  bool Healthy() const;

  const std::string& last_error() const { return last_error_; }

 private:
  bool DrainPending(MarketEvent* out_event);
  bool WaitForSubscribeAck(std::string* out_error);
  bool ParseMessage(const std::string& message);

  void MarkBroken(const std::string& error_message);

  static std::string BuildPublicUrl(bool testnet, const std::string& category);
  static std::int64_t CurrentTimestampMs();

  BybitPublicStreamOptions options_;
  std::unique_ptr<WebsocketClient> ws_client_;
  bool connected_{false};
  std::string last_error_;
  std::deque<MarketEvent> pending_events_;
  std::int64_t seq_{0};
  std::int64_t last_ping_ts_ms_{0};
};

}  // namespace ai_trade
