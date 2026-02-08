#include "exchange/websocket_client.h"

#include <array>
#include <chrono>
#include <string>
#include <thread>
#include <utility>

#include <curl/curl.h>

#if defined(AI_TRADE_ENABLE_BEAST_WS)
#include <boost/asio/connect.hpp>
#include <boost/asio/error.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/asio/ssl/stream.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/core/buffers_to_string.hpp>
#include <boost/beast/http/field.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/beast/websocket/stream.hpp>
#include <openssl/ssl.h>
#endif

#if defined(_WIN32)
#include <winsock2.h>
#else
#include <sys/select.h>
#endif

namespace ai_trade {

namespace {

std::string CurlCodeToString(CURLcode code) {
  return std::string(curl_easy_strerror(code));
}

bool WaitSocketReadable(CURL* curl,
                        long timeout_ms,
                        bool* out_readable,
                        std::string* out_error) {
  if (out_readable == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_readable 为空";
    }
    return false;
  }

  curl_socket_t socket_fd = CURL_SOCKET_BAD;
  if (curl_easy_getinfo(curl, CURLINFO_ACTIVESOCKET, &socket_fd) != CURLE_OK ||
      socket_fd == CURL_SOCKET_BAD) {
    if (out_error != nullptr) {
      *out_error = "获取 WebSocket 活跃 socket 失败";
    }
    return false;
  }

  fd_set read_fds;
  FD_ZERO(&read_fds);
  FD_SET(socket_fd, &read_fds);

  struct timeval tv;
  tv.tv_sec = timeout_ms / 1000;
  tv.tv_usec = static_cast<int>((timeout_ms % 1000) * 1000);

#if defined(_WIN32)
  const int ready = select(0, &read_fds, nullptr, nullptr, &tv);
#else
  const int ready = select(static_cast<int>(socket_fd + 1),
                           &read_fds,
                           nullptr,
                           nullptr,
                           &tv);
#endif

  if (ready < 0) {
    if (out_error != nullptr) {
      *out_error = "select 等待 WebSocket 数据失败";
    }
    return false;
  }

  *out_readable = ready > 0;
  return true;
}

#if defined(AI_TRADE_ENABLE_BEAST_WS)

bool WaitNativeSocketReadable(int socket_fd,
                              long timeout_ms,
                              bool* out_readable,
                              std::string* out_error) {
  if (out_readable == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_readable 为空";
    }
    return false;
  }
  if (socket_fd < 0) {
    if (out_error != nullptr) {
      *out_error = "WebSocket socket 非法";
    }
    return false;
  }

  fd_set read_fds;
  FD_ZERO(&read_fds);
  FD_SET(socket_fd, &read_fds);

  struct timeval tv;
  tv.tv_sec = timeout_ms / 1000;
  tv.tv_usec = static_cast<int>((timeout_ms % 1000) * 1000);

#if defined(_WIN32)
  const int ready = select(0, &read_fds, nullptr, nullptr, &tv);
#else
  const int ready =
      select(socket_fd + 1, &read_fds, nullptr, nullptr, &tv);
#endif

  if (ready < 0) {
    if (out_error != nullptr) {
      *out_error = "select 等待 Beast WebSocket 数据失败";
    }
    return false;
  }

  *out_readable = ready > 0;
  return true;
}

struct ParsedWsUrl {
  bool secure{true};
  std::string host;
  std::string port;
  std::string target;
};

bool ParseWsUrl(const std::string& url,
                ParsedWsUrl* out,
                std::string* out_error) {
  if (out == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out 为空";
    }
    return false;
  }

  const std::size_t scheme_pos = url.find("://");
  if (scheme_pos == std::string::npos) {
    if (out_error != nullptr) {
      *out_error = "URL 缺少 scheme";
    }
    return false;
  }

  const std::string scheme = url.substr(0, scheme_pos);
  if (scheme != "wss") {
    if (out_error != nullptr) {
      *out_error = "仅支持 wss 协议";
    }
    return false;
  }

  const std::size_t authority_begin = scheme_pos + 3;
  const std::size_t path_pos = url.find('/', authority_begin);
  const std::string authority =
      (path_pos == std::string::npos)
          ? url.substr(authority_begin)
          : url.substr(authority_begin, path_pos - authority_begin);
  std::string target =
      (path_pos == std::string::npos) ? std::string("/") : url.substr(path_pos);

  if (authority.empty()) {
    if (out_error != nullptr) {
      *out_error = "URL authority 为空";
    }
    return false;
  }

  std::string host;
  std::string port;
  if (!authority.empty() && authority.front() == '[') {
    const std::size_t end = authority.find(']');
    if (end == std::string::npos) {
      if (out_error != nullptr) {
        *out_error = "URL IPv6 authority 非法";
      }
      return false;
    }
    host = authority.substr(1, end - 1);
    if (end + 1 < authority.size() && authority[end + 1] == ':') {
      port = authority.substr(end + 2);
    }
  } else {
    const std::size_t colon = authority.rfind(':');
    if (colon != std::string::npos && authority.find(':') == colon) {
      host = authority.substr(0, colon);
      port = authority.substr(colon + 1);
    } else {
      host = authority;
    }
  }

  if (host.empty()) {
    if (out_error != nullptr) {
      *out_error = "URL host 为空";
    }
    return false;
  }
  if (port.empty()) {
    port = "443";
  }
  if (target.empty()) {
    target = "/";
  }

  out->secure = true;
  out->host = host;
  out->port = port;
  out->target = target;
  return true;
}

class BeastWebsocketClient final : public WebsocketClient {
 public:
  BeastWebsocketClient() = default;
  ~BeastWebsocketClient() override {
    Close();
  }

  bool Connect(
      const std::string& url,
      const std::vector<std::pair<std::string, std::string>>& headers,
      std::string* out_error) override {
    Close();
    last_error_.clear();

    ParsedWsUrl parsed;
    if (!ParseWsUrl(url, &parsed, &last_error_)) {
      if (out_error != nullptr) {
        *out_error = last_error_;
      }
      return false;
    }

    try {
      state_ = std::make_unique<State>();
      state_->ssl_ctx.set_default_verify_paths();
      state_->ssl_ctx.set_verify_mode(boost::asio::ssl::verify_peer);
    } catch (const std::exception& ex) {
      last_error_ = std::string("初始化 TLS 上下文失败: ") + ex.what();
      Close();
      if (out_error != nullptr) {
        *out_error = last_error_;
      }
      return false;
    }

    if (!SSL_set_tlsext_host_name(state_->ws.next_layer().native_handle(),
                                  parsed.host.c_str())) {
      last_error_ = "设置 TLS SNI 失败";
      Close();
      if (out_error != nullptr) {
        *out_error = last_error_;
      }
      return false;
    }

    state_->ws.set_option(
        boost::beast::websocket::stream_base::timeout::suggested(
            boost::beast::role_type::client));
    state_->ws.set_option(
        boost::beast::websocket::stream_base::decorator(
            [headers](boost::beast::websocket::request_type& req) {
              req.set(boost::beast::http::field::user_agent, "ai-trade/0.1");
              for (const auto& [key, value] : headers) {
                if (!key.empty()) {
                  req.set(key, value);
                }
              }
            }));

    boost::beast::error_code ec;
    const auto results = state_->resolver.resolve(parsed.host, parsed.port, ec);
    if (ec) {
      last_error_ = "WebSocket DNS 解析失败: " + ec.message();
      Close();
      if (out_error != nullptr) {
        *out_error = last_error_;
      }
      return false;
    }

    boost::beast::get_lowest_layer(state_->ws).connect(results, ec);
    if (ec) {
      last_error_ = "WebSocket TCP 连接失败: " + ec.message();
      Close();
      if (out_error != nullptr) {
        *out_error = last_error_;
      }
      return false;
    }

    state_->ws.next_layer().handshake(boost::asio::ssl::stream_base::client, ec);
    if (ec) {
      last_error_ = "WebSocket TLS 握手失败: " + ec.message();
      Close();
      if (out_error != nullptr) {
        *out_error = last_error_;
      }
      return false;
    }

    std::string host_header = parsed.host;
    if (parsed.port != "443") {
      host_header += ":" + parsed.port;
    }
    state_->ws.handshake(host_header, parsed.target, ec);
    if (ec) {
      last_error_ = "WebSocket 握手失败: " + ec.message();
      Close();
      if (out_error != nullptr) {
        *out_error = last_error_;
      }
      return false;
    }

    connected_ = true;
    return true;
  }

  bool SendText(const std::string& payload,
                std::string* out_error) override {
    if (!connected_ || state_ == nullptr) {
      if (out_error != nullptr) {
        *out_error = "WebSocket 未连接";
      }
      return false;
    }

    for (int attempt = 0; attempt < 20; ++attempt) {
      boost::beast::error_code ec;
      state_->ws.text(true);
      state_->ws.write(boost::asio::buffer(payload), ec);
      if (!ec) {
        return true;
      }
      if (ec == boost::asio::error::would_block ||
          ec == boost::asio::error::try_again) {
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
        continue;
      }

      const std::string message = "WebSocket 发送失败: " + ec.message();
      MarkClosed(message);
      if (out_error != nullptr) {
        *out_error = message;
      }
      return false;
    }

    const std::string message = "WebSocket 发送失败: would_block 超时";
    MarkClosed(message);
    if (out_error != nullptr) {
      *out_error = message;
    }
    return false;
  }

  WsPollStatus PollText(std::string* out_payload,
                        std::string* out_error) override {
    if (!connected_ || state_ == nullptr) {
      if (out_error != nullptr) {
        *out_error = "WebSocket 未连接";
      }
      return WsPollStatus::kClosed;
    }
    if (out_payload == nullptr) {
      if (out_error != nullptr) {
        *out_error = "out_payload 为空";
      }
      return WsPollStatus::kError;
    }

    out_payload->clear();
    bool readable = false;
    std::string wait_error;
    const int socket_fd = static_cast<int>(
        boost::beast::get_lowest_layer(state_->ws).socket().native_handle());
    if (!WaitNativeSocketReadable(socket_fd, /*timeout_ms=*/5, &readable, &wait_error)) {
      MarkClosed(wait_error);
      if (out_error != nullptr) {
        *out_error = wait_error;
      }
      return WsPollStatus::kError;
    }
    if (!readable) {
      return WsPollStatus::kNoMessage;
    }

    boost::beast::error_code ec;
    state_->ws.read(state_->read_buffer, ec);
    if (ec == boost::beast::websocket::error::closed) {
      const std::string message = "WebSocket 对端关闭连接";
      MarkClosed(message);
      if (out_error != nullptr) {
        *out_error = message;
      }
      return WsPollStatus::kClosed;
    }
    if (ec) {
      const std::string message = "WebSocket 接收失败: " + ec.message();
      MarkClosed(message);
      if (out_error != nullptr) {
        *out_error = message;
      }
      return WsPollStatus::kError;
    }

    *out_payload = boost::beast::buffers_to_string(state_->read_buffer.cdata());
    state_->read_buffer.consume(state_->read_buffer.size());
    if (out_payload->empty()) {
      return WsPollStatus::kNoMessage;
    }
    return WsPollStatus::kMessage;
  }

  bool IsConnected() const override {
    return connected_;
  }

  void Close() override {
    if (state_ != nullptr) {
      boost::beast::error_code ec;
      if (state_->ws.is_open()) {
        state_->ws.close(boost::beast::websocket::close_code::normal, ec);
      }
      boost::beast::get_lowest_layer(state_->ws).socket().close(ec);
      state_.reset();
    }
    connected_ = false;
  }

 private:
  struct State {
    State()
        : ssl_ctx(boost::asio::ssl::context::tls_client),
          resolver(ioc),
          ws(ioc, ssl_ctx) {}

    boost::asio::io_context ioc;
    boost::asio::ssl::context ssl_ctx;
    boost::asio::ip::tcp::resolver resolver;
    boost::beast::websocket::stream<
        boost::beast::ssl_stream<boost::beast::tcp_stream>> ws;
    boost::beast::flat_buffer read_buffer;
  };

  void MarkClosed(const std::string& error_message) {
    last_error_ = error_message;
    Close();
  }

  std::unique_ptr<State> state_;
  bool connected_{false};
  std::string last_error_;
};

#endif  // AI_TRADE_ENABLE_BEAST_WS

}  // namespace

CurlWebsocketClient::CurlWebsocketClient() = default;

CurlWebsocketClient::~CurlWebsocketClient() {
  Close();
}

bool CurlWebsocketClient::Connect(
    const std::string& url,
    const std::vector<std::pair<std::string, std::string>>& headers,
    std::string* out_error) {
  static const bool kCurlGlobalInit = []() {
    return curl_global_init(CURL_GLOBAL_DEFAULT) == CURLE_OK;
  }();

  Close();
  last_error_.clear();

  if (!kCurlGlobalInit) {
    last_error_ = "curl_global_init 失败";
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  CURL* curl = curl_easy_init();
  if (curl == nullptr) {
    last_error_ = "curl_easy_init 失败";
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  struct curl_slist* header_list = nullptr;
  for (const auto& [key, value] : headers) {
    header_list = curl_slist_append(header_list, (key + ": " + value).c_str());
  }

  char curl_error[CURL_ERROR_SIZE] = {0};
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_CONNECT_ONLY, 2L);
  curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, curl_error);
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, header_list);
  curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L);
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 15000L);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, "ai-trade/0.1");
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);

  const CURLcode code = curl_easy_perform(curl);
  curl_slist_free_all(header_list);

  if (code != CURLE_OK) {
    last_error_ = "WebSocket 握手失败: ";
    if (curl_error[0] != '\0') {
      last_error_ += std::string(curl_error);
    } else {
      last_error_ += CurlCodeToString(code);
    }
    curl_easy_cleanup(curl);
    if (out_error != nullptr) {
      *out_error = last_error_;
    }
    return false;
  }

  curl_handle_ = curl;
  connected_ = true;
  return true;
}

bool CurlWebsocketClient::SendText(const std::string& payload,
                                   std::string* out_error) {
  if (!connected_ || curl_handle_ == nullptr) {
    if (out_error != nullptr) {
      *out_error = "WebSocket 未连接";
    }
    return false;
  }

  CURL* curl = static_cast<CURL*>(curl_handle_);
  std::size_t total_sent = 0;
  while (total_sent < payload.size()) {
    std::size_t sent = 0;
    const CURLcode code = curl_ws_send(curl,
                                       payload.data() + total_sent,
                                       payload.size() - total_sent,
                                       &sent,
                                       0,
                                       total_sent == 0 ? CURLWS_TEXT : CURLWS_CONT);
    if (code != CURLE_OK) {
      const std::string message = "WebSocket 发送失败: " + CurlCodeToString(code);
      MarkClosed(message);
      if (out_error != nullptr) {
        *out_error = message;
      }
      return false;
    }
    total_sent += sent;
  }

  return true;
}

WsPollStatus CurlWebsocketClient::PollText(std::string* out_payload,
                                           std::string* out_error) {
  if (!connected_ || curl_handle_ == nullptr) {
    if (out_error != nullptr) {
      *out_error = "WebSocket 未连接";
    }
    return WsPollStatus::kClosed;
  }
  if (out_payload == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_payload 为空";
    }
    return WsPollStatus::kError;
  }

  CURL* curl = static_cast<CURL*>(curl_handle_);
  bool readable = false;
  std::string wait_error;
  if (!WaitSocketReadable(curl, /*timeout_ms=*/5, &readable, &wait_error)) {
    MarkClosed(wait_error);
    if (out_error != nullptr) {
      *out_error = wait_error;
    }
    return WsPollStatus::kError;
  }
  if (!readable) {
    out_payload->clear();
    return WsPollStatus::kNoMessage;
  }

  out_payload->clear();
  bool got_data = false;
  for (int guard = 0; guard < 32; ++guard) {
    std::array<char, 4096> buffer{};
    std::size_t received = 0;
    const struct curl_ws_frame* meta = nullptr;

    const CURLcode code =
        curl_ws_recv(curl, buffer.data(), buffer.size(), &received, &meta);
    if (code == CURLE_AGAIN) {
      break;
    }
    if (code != CURLE_OK) {
      const std::string message = "WebSocket 接收失败: " + CurlCodeToString(code);
      MarkClosed(message);
      if (out_error != nullptr) {
        *out_error = message;
      }
      return WsPollStatus::kError;
    }

    got_data = true;
    if (meta != nullptr && (meta->flags & CURLWS_CLOSE) != 0U) {
      const std::string message = "WebSocket 对端关闭连接";
      MarkClosed(message);
      if (out_error != nullptr) {
        *out_error = message;
      }
      return WsPollStatus::kClosed;
    }

    if (received > 0U) {
      out_payload->append(buffer.data(), received);
    }

    if (meta == nullptr || meta->bytesleft == 0U) {
      break;
    }
  }

  if (!got_data || out_payload->empty()) {
    return WsPollStatus::kNoMessage;
  }
  return WsPollStatus::kMessage;
}

void CurlWebsocketClient::Close() {
  if (curl_handle_ != nullptr) {
    curl_easy_cleanup(static_cast<CURL*>(curl_handle_));
    curl_handle_ = nullptr;
  }
  connected_ = false;
}

void CurlWebsocketClient::MarkClosed(const std::string& error_message) {
  last_error_ = error_message;
  Close();
}

std::unique_ptr<WebsocketClient> CreateCurlWebsocketClient() {
#if defined(AI_TRADE_ENABLE_BEAST_WS)
  return std::make_unique<BeastWebsocketClient>();
#else
  return std::make_unique<CurlWebsocketClient>();
#endif
}

}  // namespace ai_trade
