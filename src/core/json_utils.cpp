#include "core/json_utils.h"

#include <cctype>
#include <exception>
#include <sstream>

namespace ai_trade {

namespace {

class JsonParser {
 public:
  explicit JsonParser(const std::string& text) : text_(text) {}

  bool Parse(JsonValue* out_value, std::string* out_error) {
    if (out_value == nullptr) {
      if (out_error != nullptr) {
        *out_error = "out_value 为空";
      }
      return false;
    }
    SkipWhitespace();
    if (!ParseValue(out_value, out_error)) {
      return false;
    }
    SkipWhitespace();
    if (cursor_ != text_.size()) {
      if (out_error != nullptr) {
        *out_error = "JSON 尾部存在多余字符";
      }
      return false;
    }
    return true;
  }

 private:
  bool ParseValue(JsonValue* out_value, std::string* out_error) {
    if (cursor_ >= text_.size()) {
      return Fail("JSON 意外结束", out_error);
    }
    const char ch = text_[cursor_];
    if (ch == '{') {
      return ParseObject(out_value, out_error);
    }
    if (ch == '[') {
      return ParseArray(out_value, out_error);
    }
    if (ch == '"') {
      out_value->type = JsonType::kString;
      return ParseString(&out_value->string_value, out_error);
    }
    if (ch == 't' || ch == 'f') {
      out_value->type = JsonType::kBool;
      return ParseBool(&out_value->bool_value, out_error);
    }
    if (ch == 'n') {
      out_value->type = JsonType::kNull;
      return ParseNull(out_error);
    }
    if (ch == '-' || (ch >= '0' && ch <= '9')) {
      out_value->type = JsonType::kNumber;
      return ParseNumber(&out_value->number_value, out_error);
    }
    return Fail("JSON 非法值起始字符", out_error);
  }

  bool ParseObject(JsonValue* out_value, std::string* out_error) {
    if (!Expect('{', out_error)) {
      return false;
    }
    out_value->type = JsonType::kObject;
    out_value->object_value.clear();

    SkipWhitespace();
    if (ConsumeIf('}')) {
      return true;
    }

    while (cursor_ < text_.size()) {
      std::string key;
      if (!ParseString(&key, out_error)) {
        return false;
      }
      SkipWhitespace();
      if (!Expect(':', out_error)) {
        return false;
      }
      SkipWhitespace();

      JsonValue value;
      if (!ParseValue(&value, out_error)) {
        return false;
      }
      out_value->object_value[key] = std::move(value);

      SkipWhitespace();
      if (ConsumeIf('}')) {
        return true;
      }
      if (!Expect(',', out_error)) {
        return false;
      }
      SkipWhitespace();
    }
    return Fail("JSON 对象缺少结束符", out_error);
  }

  bool ParseArray(JsonValue* out_value, std::string* out_error) {
    if (!Expect('[', out_error)) {
      return false;
    }
    out_value->type = JsonType::kArray;
    out_value->array_value.clear();

    SkipWhitespace();
    if (ConsumeIf(']')) {
      return true;
    }

    while (cursor_ < text_.size()) {
      JsonValue item;
      if (!ParseValue(&item, out_error)) {
        return false;
      }
      out_value->array_value.push_back(std::move(item));

      SkipWhitespace();
      if (ConsumeIf(']')) {
        return true;
      }
      if (!Expect(',', out_error)) {
        return false;
      }
      SkipWhitespace();
    }
    return Fail("JSON 数组缺少结束符", out_error);
  }

  bool ParseString(std::string* out, std::string* out_error) {
    if (out == nullptr) {
      return Fail("ParseString 输出为空", out_error);
    }
    if (!Expect('"', out_error)) {
      return false;
    }
    out->clear();
    while (cursor_ < text_.size()) {
      const char ch = text_[cursor_++];
      if (ch == '"') {
        return true;
      }
      if (ch == '\\') {
        if (cursor_ >= text_.size()) {
          return Fail("JSON 字符串转义不完整", out_error);
        }
        const char esc = text_[cursor_++];
        switch (esc) {
          case '"':
          case '\\':
          case '/':
            out->push_back(esc);
            break;
          case 'b':
            out->push_back('\b');
            break;
          case 'f':
            out->push_back('\f');
            break;
          case 'n':
            out->push_back('\n');
            break;
          case 'r':
            out->push_back('\r');
            break;
          case 't':
            out->push_back('\t');
            break;
          case 'u': {
            if (cursor_ + 4 > text_.size()) {
              return Fail("JSON unicode 转义不完整", out_error);
            }
            unsigned int codepoint = 0;
            for (int i = 0; i < 4; ++i) {
              const char hex = text_[cursor_++];
              codepoint <<= 4U;
              if (hex >= '0' && hex <= '9') {
                codepoint += static_cast<unsigned int>(hex - '0');
              } else if (hex >= 'a' && hex <= 'f') {
                codepoint += static_cast<unsigned int>(hex - 'a' + 10);
              } else if (hex >= 'A' && hex <= 'F') {
                codepoint += static_cast<unsigned int>(hex - 'A' + 10);
              } else {
                return Fail("JSON unicode 转义非法", out_error);
              }
            }
            if (codepoint <= 0x7F) {
              out->push_back(static_cast<char>(codepoint));
            } else {
              out->push_back('?');
            }
            break;
          }
          default:
            return Fail("JSON 字符串转义字符非法", out_error);
        }
        continue;
      }
      out->push_back(ch);
    }
    return Fail("JSON 字符串缺少结束引号", out_error);
  }

  bool ParseBool(bool* out_value, std::string* out_error) {
    if (MatchLiteral("true")) {
      *out_value = true;
      return true;
    }
    if (MatchLiteral("false")) {
      *out_value = false;
      return true;
    }
    return Fail("JSON 布尔值解析失败", out_error);
  }

  bool ParseNull(std::string* out_error) {
    if (MatchLiteral("null")) {
      return true;
    }
    return Fail("JSON null 解析失败", out_error);
  }

  bool ParseNumber(double* out_value, std::string* out_error) {
    const std::size_t begin = cursor_;
    if (ConsumeIf('-')) {
    }
    if (!ConsumeDigits()) {
      return Fail("JSON 数字解析失败", out_error);
    }
    if (ConsumeIf('.')) {
      if (!ConsumeDigits()) {
        return Fail("JSON 小数解析失败", out_error);
      }
    }
    if (ConsumeIf('e') || ConsumeIf('E')) {
      if (ConsumeIf('+') || ConsumeIf('-')) {
      }
      if (!ConsumeDigits()) {
        return Fail("JSON 指数解析失败", out_error);
      }
    }
    const std::string raw = text_.substr(begin, cursor_ - begin);
    try {
      *out_value = std::stod(raw);
      return true;
    } catch (const std::exception&) {
      return Fail("JSON 数字转换失败", out_error);
    }
  }

  bool ConsumeDigits() {
    const std::size_t begin = cursor_;
    while (cursor_ < text_.size() &&
           std::isdigit(static_cast<unsigned char>(text_[cursor_])) != 0) {
      ++cursor_;
    }
    return cursor_ > begin;
  }

  bool MatchLiteral(const char* literal) {
    const std::size_t len = std::char_traits<char>::length(literal);
    if (cursor_ + len > text_.size()) {
      return false;
    }
    if (text_.compare(cursor_, len, literal) != 0) {
      return false;
    }
    cursor_ += len;
    return true;
  }

  bool Expect(char ch, std::string* out_error) {
    if (cursor_ >= text_.size() || text_[cursor_] != ch) {
      std::ostringstream oss;
      oss << "JSON 期望字符 '" << ch << "'";
      return Fail(oss.str(), out_error);
    }
    ++cursor_;
    return true;
  }

  bool ConsumeIf(char ch) {
    if (cursor_ < text_.size() && text_[cursor_] == ch) {
      ++cursor_;
      return true;
    }
    return false;
  }

  void SkipWhitespace() {
    while (cursor_ < text_.size() &&
           std::isspace(static_cast<unsigned char>(text_[cursor_])) != 0) {
      ++cursor_;
    }
  }

  bool Fail(const std::string& message, std::string* out_error) const {
    if (out_error != nullptr) {
      *out_error = message + "，offset=" + std::to_string(cursor_);
    }
    return false;
  }

  const std::string& text_;
  std::size_t cursor_{0};
};

}  // namespace

bool ParseJson(const std::string& text,
               JsonValue* out_value,
               std::string* out_error) {
  JsonParser parser(text);
  return parser.Parse(out_value, out_error);
}

const JsonValue* JsonObjectField(const JsonValue* value, const std::string& key) {
  if (value == nullptr || value->type != JsonType::kObject) {
    return nullptr;
  }
  const auto it = value->object_value.find(key);
  if (it == value->object_value.end()) {
    return nullptr;
  }
  return &it->second;
}

const JsonValue* JsonArrayAt(const JsonValue* value, std::size_t index) {
  if (value == nullptr || value->type != JsonType::kArray) {
    return nullptr;
  }
  if (index >= value->array_value.size()) {
    return nullptr;
  }
  return &value->array_value[index];
}

const JsonValue* JsonFindPath(const JsonValue* value,
                              const std::vector<std::string>& object_path) {
  const JsonValue* cursor = value;
  for (const auto& key : object_path) {
    cursor = JsonObjectField(cursor, key);
    if (cursor == nullptr) {
      return nullptr;
    }
  }
  return cursor;
}

std::optional<std::string> JsonAsString(const JsonValue* value) {
  if (value == nullptr) {
    return std::nullopt;
  }
  if (value->type == JsonType::kString) {
    return value->string_value;
  }
  if (value->type == JsonType::kNumber) {
    return std::to_string(value->number_value);
  }
  if (value->type == JsonType::kBool) {
    return value->bool_value ? "true" : "false";
  }
  return std::nullopt;
}

std::optional<double> JsonAsNumber(const JsonValue* value) {
  if (value == nullptr) {
    return std::nullopt;
  }
  if (value->type == JsonType::kNumber) {
    return value->number_value;
  }
  if (value->type == JsonType::kString) {
    try {
      return std::stod(value->string_value);
    } catch (const std::exception&) {
      return std::nullopt;
    }
  }
  return std::nullopt;
}

std::optional<bool> JsonAsBool(const JsonValue* value) {
  if (value == nullptr) {
    return std::nullopt;
  }
  if (value->type == JsonType::kBool) {
    return value->bool_value;
  }
  if (value->type == JsonType::kString) {
    if (value->string_value == "true") {
      return true;
    }
    if (value->string_value == "false") {
      return false;
    }
  }
  return std::nullopt;
}

}  // namespace ai_trade

