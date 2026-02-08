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

// 统一 WebSocket 客户端抽象，便于真实实现与测试替身注入。
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
  void MarkClosed(const std::string& error_message);

  void* curl_handle_{nullptr};
  bool connected_{false};
  std::string last_error_;
};

std::unique_ptr<WebsocketClient> CreateCurlWebsocketClient();

}  // namespace ai_trade
