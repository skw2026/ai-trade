#pragma once

#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "core/types.h"
#include "exchange/websocket_client.h"

namespace ai_trade {

/// Bybit 公共 WS 配置（行情通道）。
struct BybitPublicStreamOptions {
  bool testnet{true};
  bool enabled{true};
  std::string category{"linear"};
  std::vector<std::string> symbols{"BTCUSDT"};
  int ack_timeout_ms{2500};
  int heartbeat_interval_ms{20000};
};

/**
 * @brief Bybit 公共行情流
 *
 * 负责：
 * 1. 建立公共 WS 并订阅 `tickers`；
 * 2. 将原始消息解析为统一 `MarketEvent`；
 * 3. 维护心跳与连接健康状态。
 */
class BybitPublicStream {
 public:
  explicit BybitPublicStream(
      BybitPublicStreamOptions options,
      std::unique_ptr<WebsocketClient> ws_client = CreateCurlWebsocketClient());

  /**
   * @brief 建立公共 WS 连接并完成 tickers 订阅
   * @param out_error 失败原因（可选输出）
   */
  bool Connect(std::string* out_error);

  /**
   * @brief 轮询一条行情事件
   *
   * 行为：先消费内部 pending 队列；若为空则从 WS 拉取并解析。
   */
  bool PollTicker(MarketEvent* out_event);

  /// 返回当前链路健康状态（连接已建立且底层 WS 仍在线）。
  bool Healthy() const;

  const std::string& last_error() const { return last_error_; }

 private:
  /// 先消费内部 pending 队列，再返回单条行情。
  bool DrainPending(MarketEvent* out_event);
  /// 等待 subscribe ACK。
  bool WaitForSubscribeAck(std::string* out_error);
  /// 解析单条 WS 消息（控制帧 + ticker 业务帧）。
  bool ParseMessage(const std::string& message);

  /// 标记链路损坏并主动关闭底层连接。
  void MarkBroken(const std::string& error_message);

  /// 根据环境和 category 生成公共 WS URL。
  static std::string BuildPublicUrl(bool testnet, const std::string& category);
  /// 当前毫秒时间戳（用于心跳节奏控制）。
  static std::int64_t CurrentTimestampMs();

  BybitPublicStreamOptions options_;  ///< 公共流配置快照。
  std::unique_ptr<WebsocketClient> ws_client_;  ///< 底层 WS 客户端。
  bool connected_{false};  ///< 当前连接状态。
  std::string last_error_;  ///< 最近一次错误描述。
  std::deque<MarketEvent> pending_events_;  ///< 已解析待消费行情队列。
  std::int64_t seq_{0};  ///< 本地生成的行情序号。
  std::int64_t last_ping_ts_ms_{0};  ///< 最近一次发送 ping 的时间戳。
  std::unordered_map<std::string, std::int64_t>
      last_event_ts_ms_by_symbol_;  ///< 每个 symbol 最近事件时间（用于 interval）。
  std::unordered_map<std::string, double>
      last_volume_24h_by_symbol_;  ///< 每个 symbol 最近 volume24h（用于增量 volume）。
};

}  // namespace ai_trade
