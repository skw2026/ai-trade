#include "core/log.h"

#include <chrono>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <mutex>

namespace ai_trade {

static std::mutex g_log_mutex;

void LogInfo(std::string_view message) {
  // 用全局互斥保证多线程日志不会交叉写入。
  std::lock_guard<std::mutex> lock(g_log_mutex);
  const auto now = std::chrono::system_clock::now();
  const std::time_t t = std::chrono::system_clock::to_time_t(now);
  std::tm tm{};
#if defined(_WIN32)
  localtime_s(&tm, &t);
#else
  localtime_r(&t, &tm);
#endif
  std::cout << std::put_time(&tm, "%F %T") << " [INFO] " << message << '\n';
}

void LogError(std::string_view message) {
  // 错误日志和信息日志共享同一把锁，保证时序可读。
  std::lock_guard<std::mutex> lock(g_log_mutex);
  const auto now = std::chrono::system_clock::now();
  const std::time_t t = std::chrono::system_clock::to_time_t(now);
  std::tm tm{};
#if defined(_WIN32)
  localtime_s(&tm, &t);
#else
  localtime_r(&t, &tm);
#endif
  std::cerr << std::put_time(&tm, "%F %T") << " [ERROR] " << message << '\n';
}

}  // namespace ai_trade
