#include "storage/wal_store.h"

#include <filesystem>
#include <exception>
#include <fstream>
#include <sstream>
#include <vector>

namespace ai_trade {

namespace {

std::string SerializeIntent(const OrderIntent& order) {
  // 文本 WAL 格式：字段顺序固定，便于版本兼容与人工排障。
  std::ostringstream oss;
  oss << "INTENT"
      << '\t'
      << order.client_order_id
      << '\t'
      << order.symbol
      << '\t'
      << static_cast<int>(order.purpose)
      << '\t'
      << static_cast<int>(order.liquidity_preference)
      << '\t'
      << (order.reduce_only ? 1 : 0)
      << '\t'
      << order.direction
      << '\t'
      << order.qty
      << '\t'
      << order.price;
  return oss.str();
}

std::string SerializeFillV2(const FillEvent& fill) {
  // V2 显式存储 fill_id，解决旧格式无法稳定去重的问题。
  std::ostringstream oss;
  oss << "FILL2"
      << '\t'
      << fill.fill_id
      << '\t'
      << fill.client_order_id
      << '\t'
      << fill.symbol
      << '\t'
      << fill.direction
      << '\t'
      << fill.qty
      << '\t'
      << fill.price
      << '\t'
      << fill.fee;
  return oss.str();
}

std::vector<std::string> SplitTab(const std::string& line) {
  std::vector<std::string> parts;
  std::string current;
  std::istringstream iss(line);
  while (std::getline(iss, current, '\t')) {
    parts.push_back(current);
  }
  return parts;
}

bool ParseIntent(const std::vector<std::string>& fields,
                 OrderIntent* out_intent,
                 std::string* out_error) {
  if (out_intent == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_intent 为空";
    }
    return false;
  }
  if (fields.size() != 8 && fields.size() != 9) {
    if (out_error != nullptr) {
      *out_error = "INTENT WAL 字段数异常";
    }
    return false;
  }

  OrderIntent intent;
  intent.client_order_id = fields[1];
  intent.symbol = fields[2];

  try {
    intent.purpose = static_cast<OrderPurpose>(std::stoi(fields[3]));
    std::size_t cursor = 4;
    if (fields.size() == 9) {
      const int raw_pref = std::stoi(fields[cursor++]);
      if (raw_pref < static_cast<int>(LiquidityPreference::kAuto) ||
          raw_pref > static_cast<int>(LiquidityPreference::kTaker)) {
        if (out_error != nullptr) {
          *out_error = "INTENT WAL liquidity_preference 字段非法";
        }
        return false;
      }
      intent.liquidity_preference = static_cast<LiquidityPreference>(raw_pref);
    } else {
      intent.liquidity_preference = LiquidityPreference::kAuto;
    }
    intent.reduce_only = std::stoi(fields[cursor++]) != 0;
    intent.direction = std::stoi(fields[cursor++]);
    intent.qty = std::stod(fields[cursor++]);
    intent.price = std::stod(fields[cursor++]);
  } catch (const std::exception&) {
    if (out_error != nullptr) {
      *out_error = "INTENT WAL 字段解析失败";
    }
    return false;
  }

  *out_intent = intent;
  return true;
}

bool ParseFillV2(const std::vector<std::string>& fields,
                 FillEvent* out_fill,
                 std::string* out_error) {
  if (out_fill == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_fill 为空";
    }
    return false;
  }
  if (fields.size() != 8) {
    if (out_error != nullptr) {
      *out_error = "FILL2 WAL 字段数异常";
    }
    return false;
  }

  FillEvent fill;
  fill.fill_id = fields[1];
  fill.client_order_id = fields[2];
  fill.symbol = fields[3];

  try {
    fill.direction = std::stoi(fields[4]);
    fill.qty = std::stod(fields[5]);
    fill.price = std::stod(fields[6]);
    fill.fee = std::stod(fields[7]);
  } catch (const std::exception&) {
    if (out_error != nullptr) {
      *out_error = "FILL2 WAL 字段解析失败";
    }
    return false;
  }

  *out_fill = fill;
  return true;
}

bool ParseLegacyFillV1(const std::vector<std::string>& fields,
                       FillEvent* out_fill,
                       std::string* out_error) {
  if (out_fill == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_fill 为空";
    }
    return false;
  }
  if (fields.size() != 8) {
    if (out_error != nullptr) {
      *out_error = "FILL(V1) WAL 字段数异常";
    }
    return false;
  }

  FillEvent fill;
  fill.client_order_id = fields[1];
  fill.fill_id = fill.client_order_id + "-legacy";
  fill.symbol = fields[2];

  try {
    fill.direction = std::stoi(fields[5]);
    fill.qty = std::stod(fields[6]);
    fill.price = std::stod(fields[7]);
  } catch (const std::exception&) {
    if (out_error != nullptr) {
      *out_error = "FILL(V1) WAL 字段解析失败";
    }
    return false;
  }

  *out_fill = fill;
  return true;
}

}  // namespace

bool WalStore::Initialize(std::string* out_error) const {
  const std::filesystem::path path(file_path_);
  const auto parent = path.parent_path();
  std::error_code ec;
  if (!parent.empty()) {
    std::filesystem::create_directories(parent, ec);
    if (ec) {
      if (out_error != nullptr) {
        *out_error = "创建 WAL 目录失败: " + ec.message();
      }
      return false;
    }
  }

  std::ofstream out(file_path_, std::ios::app);
  if (!out.is_open()) {
    if (out_error != nullptr) {
      *out_error = "创建/打开 WAL 文件失败: " + file_path_;
    }
    return false;
  }
  return true;
}

bool WalStore::AppendLine(const std::string& line, std::string* out_error) const {
  std::ofstream out(file_path_, std::ios::app);
  if (!out.is_open()) {
    if (out_error != nullptr) {
      *out_error = "WAL 打开失败: " + file_path_;
    }
    return false;
  }

  out << line << '\n';
  out.flush();
  if (!out.good()) {
    if (out_error != nullptr) {
      *out_error = "WAL 写入失败";
    }
    return false;
  }
  return true;
}

bool WalStore::AppendIntent(const OrderIntent& intent,
                            std::string* out_error) const {
  return AppendLine(SerializeIntent(intent), out_error);
}

bool WalStore::AppendFill(const FillEvent& fill, std::string* out_error) const {
  return AppendLine(SerializeFillV2(fill), out_error);
}

bool WalStore::LoadState(std::unordered_set<std::string>* out_intent_ids,
                         std::unordered_set<std::string>* out_fill_ids,
                         std::vector<FillEvent>* out_fills,
                         std::string* out_error) const {
  if (out_intent_ids == nullptr || out_fill_ids == nullptr ||
      out_fills == nullptr) {
    if (out_error != nullptr) {
      *out_error = "LoadState 输出参数为空";
    }
    return false;
  }

  out_intent_ids->clear();
  out_fill_ids->clear();
  out_fills->clear();

  std::ifstream in(file_path_);
  if (!in.is_open()) {
    // 文件不存在或无法打开视为“无历史”，由 Initialize 负责创建。
    return true;
  }

  std::string line;
  int line_no = 0;
  while (std::getline(in, line)) {
    ++line_no;
    if (line.empty()) {
      continue;
    }

    const auto fields = SplitTab(line);
    if (fields.empty()) {
      continue;
    }

    const std::string& type = fields[0];
    if (type == "INTENT") {
      OrderIntent intent;
      std::string parse_error;
      if (!ParseIntent(fields, &intent, &parse_error)) {
        if (out_error != nullptr) {
          *out_error = "WAL 行解析失败（line=" + std::to_string(line_no) +
                       "）: " + parse_error;
        }
        return false;
      }
      out_intent_ids->insert(intent.client_order_id);
      continue;
    }
    if (type == "FILL2") {
      FillEvent fill;
      std::string parse_error;
      if (!ParseFillV2(fields, &fill, &parse_error)) {
        if (out_error != nullptr) {
          *out_error = "WAL 行解析失败（line=" + std::to_string(line_no) +
                       "）: " + parse_error;
        }
        return false;
      }
      // 以 fill_id 去重，避免重复回放导致仓位漂移。
      const bool inserted = out_fill_ids->insert(fill.fill_id).second;
      if (inserted) {
        out_fills->push_back(fill);
      }
      continue;
    }
    if (type == "FILL") {
      FillEvent fill;
      std::string parse_error;
      if (!ParseLegacyFillV1(fields, &fill, &parse_error)) {
        if (out_error != nullptr) {
          *out_error = "WAL 行解析失败（line=" + std::to_string(line_no) +
                       "）: " + parse_error;
        }
        return false;
      }
      const bool inserted = out_fill_ids->insert(fill.fill_id).second;
      if (inserted) {
        out_fills->push_back(fill);
      }
      continue;
    }

    if (out_error != nullptr) {
      *out_error = "未知 WAL 事件类型（line=" + std::to_string(line_no) + ")";
    }
    return false;
  }

  return true;
}

}  // namespace ai_trade
