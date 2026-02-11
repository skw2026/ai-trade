#pragma once

#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace ai_trade {

/// HTTP 响应统一结构，便于 mock 与真实传输层复用。
struct BybitHttpResponse {
  int status_code{0};
  std::string body;
  std::string error;
};

/**
 * @brief HTTP 传输抽象
 *
 * 作用：
 * 1. 业务层不直接依赖 libcurl；
 * 2. 单元测试可注入 mock transport。
 */
class BybitHttpTransport {
 public:
  virtual ~BybitHttpTransport() = default;
  virtual BybitHttpResponse Send(
      const std::string& method,
      const std::string& url,
      const std::vector<std::pair<std::string, std::string>>& headers,
      const std::string& body) const = 0;
};

class CurlBybitHttpTransport final : public BybitHttpTransport {
 public:
  /// 使用 libcurl 发送 HTTP 请求。
  BybitHttpResponse Send(
      const std::string& method,
      const std::string& url,
      const std::vector<std::pair<std::string, std::string>>& headers,
      const std::string& body) const override;
};

/**
 * @brief Bybit V5 REST 客户端
 *
 * 负责：
 * 1. 请求签名与鉴权头构造；
 * 2. HTTP 请求发送与状态码校验；
 * 3. retCode 业务码统一校验。
 */
class BybitRestClient {
 public:
  BybitRestClient(std::string api_key,
                  std::string api_secret,
                  bool testnet,
                  bool demo_trading = false,
                  std::unique_ptr<BybitHttpTransport> transport =
                      std::make_unique<CurlBybitHttpTransport>());

  /// 公共接口 GET 请求（无需鉴权）。
  bool GetPublic(const std::string& path,
                 const std::string& query,
                 std::string* out_body,
                 std::string* out_error) const;
  /// 私有接口 GET 请求（带 V5 鉴权）。
  bool GetPrivate(const std::string& path,
                  const std::string& query,
                  std::string* out_body,
                  std::string* out_error) const;
  /// 私有接口 POST 请求（带 V5 鉴权）。
  bool PostPrivate(const std::string& path,
                   const std::string& body,
                   std::string* out_body,
                   std::string* out_error) const;

  /// 构造 Bybit V5 签名（timestamp+apiKey+recvWindow+payload）。
  static bool BuildV5Signature(const std::string& api_secret,
                               const std::string& timestamp_ms,
                               const std::string& api_key,
                               const std::string& recv_window,
                               const std::string& payload,
                               std::string* out_signature,
                               std::string* out_error);
  /// 通用 HMAC-SHA256 十六进制签名工具函数。
  static bool BuildHmacSha256Hex(const std::string& secret,
                                 const std::string& payload,
                                 std::string* out_signature,
                                 std::string* out_error);

 private:
  /// 统一请求发送入口（含鉴权、状态码与 retCode 校验）。
  bool SendRequest(const std::string& method,
                   const std::string& path,
                   const std::string& query,
                   const std::string& body,
                   bool private_auth,
                   std::string* out_body,
                   std::string* out_error) const;

  static std::string BaseUrl(bool testnet, bool demo_trading);
  static std::string CurrentTimestampMs();
  static bool ParseRetCode(const std::string& body, int* out_ret_code);
  static std::string ParseRetMsg(const std::string& body);

  std::string api_key_;  ///< API Key。
  std::string api_secret_;  ///< API Secret。
  bool testnet_{true};  ///< 是否使用 testnet endpoint。
  bool demo_trading_{false};  ///< 是否使用 demo trading endpoint。
  std::unique_ptr<BybitHttpTransport> transport_;  ///< HTTP 传输实现。
};

}  // namespace ai_trade
