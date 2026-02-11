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

/// Bybit 私有 WS 配置（成交/订单/仓位/钱包通道）。
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

/**
 * @brief Bybit 私有成交流
 *
 * 负责：
 * 1. 私有 WS 鉴权与主题订阅；
 * 2. 解析 execution 并映射为标准 `FillEvent`；
 * 3. 执行去重、心跳和健康状态维护。
 */
class BybitPrivateStream {
 public:
  explicit BybitPrivateStream(
      BybitPrivateStreamOptions options,
      std::unique_ptr<WebsocketClient> ws_client = CreateCurlWebsocketClient());

  /**
   * @brief 建立私有 WS 连接并完成 auth/subscribe
   * @param out_error 失败原因（可选输出）
   */
  bool Connect(std::string* out_error);

  /**
   * @brief 轮询一条成交事件
   *
   * 行为：先消费内部 pending 队列；若为空则从 WS 拉取 execution 主题并解析。
   */
  bool PollExecution(FillEvent* out_fill);

  /// 返回当前私有链路健康状态（连接已建立且底层 WS 仍在线）。
  bool Healthy() const;

  const std::string& last_error() const { return last_error_; }

 private:
  /// 先消费内部 pending 队列，再返回单条成交。
  bool DrainPending(FillEvent* out_fill);
  /// 等待指定 `op` 的 ACK（auth/subscribe）。
  bool WaitForAck(const std::string& expected_op,
                  int timeout_ms,
                  std::string* out_error);
  /// 解析单条 WS 消息（控制帧 + 业务帧）。
  bool ParseMessage(const std::string& message);
  /// 解析 execution payload 并写入 pending 队列。
  bool ParseExecutionPayload(const JsonValue* data);

  /// 根据环境组合生成私有 WS URL。
  static std::string BuildPrivateUrl(bool testnet, bool demo_trading);
  /// 当前毫秒时间戳（用于签名过期与心跳）。
  static std::int64_t CurrentTimestampMs();
  /// side 文本映射为方向（Buy=1, Sell=-1）。
  static int SideToDirection(const std::string& side);

  /// 标记链路损坏并主动关闭底层连接。
  void MarkBroken(const std::string& error_message);

  BybitPrivateStreamOptions options_;  ///< 私有流配置快照。
  std::unique_ptr<WebsocketClient> ws_client_;  ///< 底层 WS 客户端。
  bool connected_{false};  ///< 当前连接状态。
  std::string last_error_;  ///< 最近一次错误描述。
  std::deque<FillEvent> pending_fills_;  ///< 已解析待消费的成交队列。
  std::unordered_set<std::string> seen_exec_ids_;  ///< 成交去重集合（execId）。
  std::int64_t last_ping_ts_ms_{0};  ///< 最近一次发送 ping 的时间戳。
};

}  // namespace ai_trade
