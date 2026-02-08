#pragma once

#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace ai_trade {

enum class JsonType {
  kNull,
  kBool,
  kNumber,
  kString,
  kArray,
  kObject,
};

struct JsonValue {
  JsonType type{JsonType::kNull};
  bool bool_value{false};
  double number_value{0.0};
  std::string string_value;
  std::vector<JsonValue> array_value;
  std::unordered_map<std::string, JsonValue> object_value;
};

bool ParseJson(const std::string& text,
               JsonValue* out_value,
               std::string* out_error);

const JsonValue* JsonObjectField(const JsonValue* value, const std::string& key);
const JsonValue* JsonArrayAt(const JsonValue* value, std::size_t index);
const JsonValue* JsonFindPath(const JsonValue* value,
                              const std::vector<std::string>& object_path);

std::optional<std::string> JsonAsString(const JsonValue* value);
std::optional<double> JsonAsNumber(const JsonValue* value);
std::optional<bool> JsonAsBool(const JsonValue* value);

}  // namespace ai_trade

