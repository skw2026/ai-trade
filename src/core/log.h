#pragma once

#include <string>
#include <string_view>

namespace ai_trade {

/**
 * @brief 输出 INFO 级日志
 *
 * 行为：
 * 1. 线程安全串行写入；
 * 2. 输出到 `stdout`；
 * 3. 自动附加本地时间戳和 `[INFO]` 前缀。
 */
void LogInfo(std::string_view message);

/**
 * @brief 输出 ERROR 级日志
 *
 * 行为：
 * 1. 线程安全串行写入；
 * 2. 输出到 `stderr`；
 * 3. 自动附加本地时间戳和 `[ERROR]` 前缀。
 */
void LogError(std::string_view message);

}  // namespace ai_trade
