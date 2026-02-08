#include "core/log.h"

#include <chrono>
#include <ctime>
#include <iomanip>
#include <iostream>

namespace ai_trade {

void LogInfo(std::string_view message) {
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

}  // namespace ai_trade
