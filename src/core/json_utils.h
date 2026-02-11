#pragma once

#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace ai_trade {

/// 轻量 JSON AST 节点类型，覆盖项目当前使用到的 JSON 子集。
enum class JsonType {
  kNull,
  kBool,
  kNumber,
  kString,
  kArray,
  kObject,
};

/// 轻量 JSON 值表示（对象/数组为递归结构）。
struct JsonValue {
  JsonType type{JsonType::kNull};
  bool bool_value{false};
  double number_value{0.0};
  std::string string_value;
  std::vector<JsonValue> array_value;
  std::unordered_map<std::string, JsonValue> object_value;
};

/**
 * @brief JSON 解析入口
 *
 * @param text 原始 JSON 文本
 * @param out_value 解析结果
 * @param out_error 失败原因（可选输出）
 * @return true 解析成功
 * @return false 解析失败
 */
bool ParseJson(const std::string& text,
               JsonValue* out_value,
               std::string* out_error);

/**
 * @brief 获取对象字段
 *
 * 仅做类型与边界检查，不抛异常；字段不存在返回 `nullptr`。
 */
const JsonValue* JsonObjectField(const JsonValue* value, const std::string& key);

/**
 * @brief 获取数组元素
 *
 * 仅做类型与边界检查，不抛异常；越界返回 `nullptr`。
 */
const JsonValue* JsonArrayAt(const JsonValue* value, std::size_t index);

/**
 * @brief 按对象路径查找子节点
 *
 * `object_path` 中每一段都按对象字段访问；任意一段缺失返回 `nullptr`。
 */
const JsonValue* JsonFindPath(const JsonValue* value,
                              const std::vector<std::string>& object_path);

/// 尝试将节点解释为字符串；失败返回 `std::nullopt`。
std::optional<std::string> JsonAsString(const JsonValue* value);
/// 尝试将节点解释为数值；失败返回 `std::nullopt`。
std::optional<double> JsonAsNumber(const JsonValue* value);
/// 尝试将节点解释为布尔值；失败返回 `std::nullopt`。
std::optional<bool> JsonAsBool(const JsonValue* value);

}  // namespace ai_trade
