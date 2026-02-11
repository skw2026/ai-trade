#include "exchange/bybit_rest_client.h"

#include <chrono>
#include <cstring>
#include <exception>
#include <limits>

#if defined(__APPLE__)
#include <CommonCrypto/CommonHMAC.h>
#else
#include <openssl/evp.h>
#include <openssl/hmac.h>
#endif

#include <curl/curl.h>

#include "core/json_utils.h"

namespace ai_trade {

namespace {

std::string BytesToHex(const unsigned char* bytes, std::size_t size) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  out.resize(size * 2U);
  for (std::size_t i = 0; i < size; ++i) {
    const unsigned char v = bytes[i];
    out[i * 2U] = kHex[(v >> 4U) & 0x0FU];
    out[i * 2U + 1U] = kHex[v & 0x0FU];
  }
  return out;
}

std::string CurlCodeToString(CURLcode code) {
  return std::string(curl_easy_strerror(code));
}

std::size_t WriteToString(char* ptr, std::size_t size, std::size_t nmemb,
                          void* userdata) {
  if (ptr == nullptr || userdata == nullptr) {
    return 0;
  }
  std::string* out = static_cast<std::string*>(userdata);
  const std::size_t total = size * nmemb;
  out->append(ptr, total);
  return total;
}

}  // namespace

BybitHttpResponse CurlBybitHttpTransport::Send(
    const std::string& method,
    const std::string& url,
    const std::vector<std::pair<std::string, std::string>>& headers,
    const std::string& body) const {
  // 进程级初始化一次，后续请求复用。
  static const bool kCurlGlobalInit = []() {
    return curl_global_init(CURL_GLOBAL_DEFAULT) == CURLE_OK;
  }();

  BybitHttpResponse out;
  if (!kCurlGlobalInit) {
    out.error = "curl_global_init 失败";
    return out;
  }

  CURL* curl = curl_easy_init();
  if (curl == nullptr) {
    out.error = "curl_easy_init 失败";
    return out;
  }

  struct curl_slist* header_list = nullptr;
  for (const auto& [key, value] : headers) {
    header_list = curl_slist_append(header_list, (key + ": " + value).c_str());
  }

  std::string response_body;
  char curl_error[CURL_ERROR_SIZE] = {0};
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L);
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10000L);
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, header_list);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteToString);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response_body);
  curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, curl_error);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, "ai-trade/0.1");
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);

  const bool is_post = method == "POST";
  if (is_post) {
    curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "POST");
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE,
                     static_cast<long>(body.size()));
  } else {
    curl_easy_setopt(curl, CURLOPT_HTTPGET, 1L);
  }

  const CURLcode code = curl_easy_perform(curl);
  long status_code = 0;
  curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status_code);

  out.status_code = static_cast<int>(status_code);
  out.body = std::move(response_body);
  if (code != CURLE_OK) {
    const std::string detailed =
        (curl_error[0] != '\0') ? std::string(curl_error) : CurlCodeToString(code);
    out.error = "curl_easy_perform 失败: " + detailed;
  }

  curl_slist_free_all(header_list);
  curl_easy_cleanup(curl);
  return out;
}

BybitRestClient::BybitRestClient(std::string api_key,
                                 std::string api_secret,
                                 bool testnet,
                                 bool demo_trading,
                                 std::unique_ptr<BybitHttpTransport> transport)
    : api_key_(std::move(api_key)),
      api_secret_(std::move(api_secret)),
      testnet_(testnet),
      demo_trading_(demo_trading),
      transport_(std::move(transport)) {
  if (transport_ == nullptr) {
    transport_ = std::make_unique<CurlBybitHttpTransport>();
  }
}

bool BybitRestClient::GetPublic(const std::string& path,
                                const std::string& query,
                                std::string* out_body,
                                std::string* out_error) const {
  return SendRequest("GET", path, query, "", /*private_auth=*/false,
                     out_body, out_error);
}

bool BybitRestClient::GetPrivate(const std::string& path,
                                 const std::string& query,
                                 std::string* out_body,
                                 std::string* out_error) const {
  return SendRequest("GET", path, query, "", /*private_auth=*/true,
                     out_body, out_error);
}

bool BybitRestClient::PostPrivate(const std::string& path,
                                  const std::string& body,
                                  std::string* out_body,
                                  std::string* out_error) const {
  return SendRequest("POST", path, "", body, /*private_auth=*/true,
                     out_body, out_error);
}

bool BybitRestClient::SendRequest(const std::string& method,
                                  const std::string& path,
                                  const std::string& query,
                                  const std::string& body,
                                  bool private_auth,
                                  std::string* out_body,
                                  std::string* out_error) const {
  if (out_body == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_body 为空";
    }
    return false;
  }

  const std::string url = BaseUrl(testnet_, demo_trading_) + path +
                          (query.empty() ? std::string() : ("?" + query));
  std::vector<std::pair<std::string, std::string>> headers;
  headers.emplace_back("Content-Type", "application/json");

  if (private_auth) {
    // V5 签名：timestamp + apiKey + recvWindow + payload。
    const std::string ts_ms = CurrentTimestampMs();
    const std::string recv_window = "5000";
    const std::string payload = (method == "GET") ? query : body;
    std::string signature;
    std::string sign_error;
    if (!BuildV5Signature(api_secret_, ts_ms, api_key_, recv_window, payload,
                          &signature, &sign_error)) {
      if (out_error != nullptr) {
        *out_error = "Bybit V5 签名失败: " + sign_error;
      }
      return false;
    }

    headers.emplace_back("X-BAPI-API-KEY", api_key_);
    headers.emplace_back("X-BAPI-SIGN", signature);
    headers.emplace_back("X-BAPI-TIMESTAMP", ts_ms);
    headers.emplace_back("X-BAPI-RECV-WINDOW", recv_window);
  }

  const BybitHttpResponse response = transport_->Send(method, url, headers, body);
  if (!response.error.empty() && response.status_code == 0) {
    if (out_error != nullptr) {
      *out_error = response.error;
    }
    return false;
  }
  if (response.status_code < 200 || response.status_code >= 300) {
    if (out_error != nullptr) {
      *out_error = "Bybit HTTP 状态异常: " +
                   std::to_string(response.status_code) +
                   (response.error.empty() ? std::string()
                                           : (", transport_error=" + response.error));
    }
    return false;
  }

  int ret_code = 0;
  // HTTP 成功后继续检查业务 retCode，确保语义成功。
  if (ParseRetCode(response.body, &ret_code) && ret_code != 0) {
    if (out_error != nullptr) {
      *out_error = "Bybit retCode 异常: " + std::to_string(ret_code) +
                   ", retMsg=" + ParseRetMsg(response.body);
    }
    return false;
  }
  *out_body = response.body;
  return true;
}

bool BybitRestClient::BuildHmacSha256Hex(const std::string& secret,
                                         const std::string& payload,
                                         std::string* out_signature,
                                         std::string* out_error) {
  if (out_signature == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_signature 为空";
    }
    return false;
  }

#if defined(__APPLE__)
  unsigned char digest[CC_SHA256_DIGEST_LENGTH];
  CCHmac(kCCHmacAlgSHA256,
         secret.data(),
         secret.size(),
         payload.data(),
         payload.size(),
         digest);
  *out_signature = BytesToHex(digest, CC_SHA256_DIGEST_LENGTH);
  return true;
#else
  if (secret.size() >
      static_cast<std::size_t>(std::numeric_limits<int>::max())) {
    if (out_error != nullptr) {
      *out_error = "secret 长度超出 OpenSSL HMAC 限制";
    }
    return false;
  }

  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int digest_len = 0;
  unsigned char* result = HMAC(EVP_sha256(),
                               secret.data(),
                               static_cast<int>(secret.size()),
                               reinterpret_cast<const unsigned char*>(payload.data()),
                               payload.size(),
                               digest,
                               &digest_len);
  if (result == nullptr || digest_len == 0U) {
    if (out_error != nullptr) {
      *out_error = "OpenSSL HMAC-SHA256 计算失败";
    }
    return false;
  }
  *out_signature = BytesToHex(digest, digest_len);
  return true;
#endif
}

bool BybitRestClient::BuildV5Signature(const std::string& api_secret,
                                       const std::string& timestamp_ms,
                                       const std::string& api_key,
                                       const std::string& recv_window,
                                       const std::string& payload,
                                       std::string* out_signature,
                                       std::string* out_error) {
  const std::string prehash = timestamp_ms + api_key + recv_window + payload;
  return BuildHmacSha256Hex(api_secret, prehash, out_signature, out_error);
}

std::string BybitRestClient::BaseUrl(bool testnet, bool demo_trading) {
  if (demo_trading) {
    return "https://api-demo.bybit.com";
  }
  return testnet ? "https://api-testnet.bybit.com" : "https://api.bybit.com";
}

std::string BybitRestClient::CurrentTimestampMs() {
  const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now());
  return std::to_string(now.time_since_epoch().count());
}

bool BybitRestClient::ParseRetCode(const std::string& body, int* out_ret_code) {
  if (out_ret_code == nullptr) {
    return false;
  }
  JsonValue root;
  std::string parse_error;
  if (!ParseJson(body, &root, &parse_error)) {
    return false;
  }
  const JsonValue* ret_code = JsonObjectField(&root, "retCode");
  if (ret_code == nullptr) {
    return false;
  }
  if (const auto number = JsonAsNumber(ret_code); number.has_value()) {
    *out_ret_code = static_cast<int>(*number);
    return true;
  }
  if (const auto text = JsonAsString(ret_code); text.has_value()) {
    try {
      *out_ret_code = std::stoi(*text);
      return true;
    } catch (const std::exception&) {
      return false;
    }
  }
  return false;
}

std::string BybitRestClient::ParseRetMsg(const std::string& body) {
  JsonValue root;
  std::string parse_error;
  if (!ParseJson(body, &root, &parse_error)) {
    return {};
  }
  const JsonValue* ret_msg = JsonObjectField(&root, "retMsg");
  if (ret_msg == nullptr) {
    return {};
  }
  if (const auto text = JsonAsString(ret_msg); text.has_value()) {
    return *text;
  }
  return {};
}

}  // namespace ai_trade
