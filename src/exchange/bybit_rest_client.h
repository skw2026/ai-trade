#pragma once

#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace ai_trade {

struct BybitHttpResponse {
  int status_code{0};
  std::string body;
  std::string error;
};

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
  BybitHttpResponse Send(
      const std::string& method,
      const std::string& url,
      const std::vector<std::pair<std::string, std::string>>& headers,
      const std::string& body) const override;
};

class BybitRestClient {
 public:
  BybitRestClient(std::string api_key,
                  std::string api_secret,
                  bool testnet,
                  bool demo_trading = false,
                  std::unique_ptr<BybitHttpTransport> transport =
                      std::make_unique<CurlBybitHttpTransport>());

  bool GetPublic(const std::string& path,
                 const std::string& query,
                 std::string* out_body,
                 std::string* out_error) const;
  bool GetPrivate(const std::string& path,
                  const std::string& query,
                  std::string* out_body,
                  std::string* out_error) const;
  bool PostPrivate(const std::string& path,
                   const std::string& body,
                   std::string* out_body,
                   std::string* out_error) const;

  static bool BuildV5Signature(const std::string& api_secret,
                               const std::string& timestamp_ms,
                               const std::string& api_key,
                               const std::string& recv_window,
                               const std::string& payload,
                               std::string* out_signature,
                               std::string* out_error);
  static bool BuildHmacSha256Hex(const std::string& secret,
                                 const std::string& payload,
                                 std::string* out_signature,
                                 std::string* out_error);

 private:
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

  std::string api_key_;
  std::string api_secret_;
  bool testnet_{true};
  bool demo_trading_{false};
  std::unique_ptr<BybitHttpTransport> transport_;
};

}  // namespace ai_trade
