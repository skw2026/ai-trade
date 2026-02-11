#pragma once

#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace ai_trade {

enum class WsPollStatus {
  kMessage,
  kNoMessage,
  kClosed,
  kError,
};

/**
 * @brief WebSocket 客户端抽象接口
 *
 * 目标：
 * 1. 屏蔽底层实现差异（libcurl / Beast / mock）；
 * 2. 让上层流模块只依赖统一的 Connect/Send/Poll 语义。
 */
class WebsocketClient {
 public:
  virtual ~WebsocketClient() = default;

  virtual bool Connect(
      const std::string& url,
      const std::vector<std::pair<std::string, std::string>>& headers,
      std::string* out_error) = 0;

  virtual bool SendText(const std::string& payload,
                        std::string* out_error) = 0;

  virtual WsPollStatus PollText(std::string* out_payload,
                                std::string* out_error) = 0;

  virtual bool IsConnected() const = 0;
  virtual void Close() = 0;
};

class CurlWebsocketClient final : public WebsocketClient {
 public:
  CurlWebsocketClient();
  ~CurlWebsocketClient() override;

  bool Connect(
      const std::string& url,
      const std::vector<std::pair<std::string, std::string>>& headers,
      std::string* out_error) override;

  bool SendText(const std::string& payload,
                std::string* out_error) override;

  WsPollStatus PollText(std::string* out_payload,
                        std::string* out_error) override;

  bool IsConnected() const override { return connected_; }
  void Close() override;

 private:
  /// 记录连接关闭并缓存错误信息。
  void MarkClosed(const std::string& error_message);

  void* curl_handle_{nullptr};  ///< libcurl 句柄（void* 以隔离头文件依赖）。
  bool connected_{false};  ///< 当前连接状态。
  std::string last_error_;  ///< 最近一次关闭原因，供上层降级日志使用。
};

/// 工厂函数：按编译开关返回可用的 WebSocket 实现。
std::unique_ptr<WebsocketClient> CreateCurlWebsocketClient();

}  // namespace ai_trade
