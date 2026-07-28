#include "storage/wal_store.h"

#include <cerrno>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <exception>
#include <fcntl.h>
#include <fstream>
#include <sstream>
#include <unistd.h>
#include <vector>

namespace ai_trade {

namespace {

std::string EncodeWalText(const std::string& value) {
  return value.empty() ? "-" : value;
}

std::string DecodeWalText(const std::string& value) {
  return value == "-" ? "" : value;
}

std::string SerializeIntent(const OrderIntent& order) {
  // INTENT2 persists candidate lineage; ParseIntent still accepts legacy INTENT.
  std::ostringstream oss;
  oss << "INTENT2"
      << '\t'
      << order.client_order_id
      << '\t'
      << EncodeWalText(order.parent_order_id)
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
      << order.price
      << '\t'
      << EncodeWalText(order.decision_id)
      << '\t'
      << EncodeWalText(order.candidate_id)
      << '\t'
      << EncodeWalText(order.model_version)
      << '\t'
      << EncodeWalText(order.integrator_mode)
      << '\t'
      << EncodeWalText(order.position_episode_id)
      << '\t'
      << EncodeWalText(order.integrator_policy_reason);
  return oss.str();
}

std::string SerializeFillV3(const FillEvent& fill) {
  // V3 additionally persists maker/taker identity for restart-safe economics.
  std::ostringstream oss;
  oss << "FILL3"
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
      << fill.fee
      << '\t'
      << static_cast<int>(fill.liquidity);
  return oss.str();
}

std::string SerializeFlatPositionRebase(const std::string& boot_id,
                                        const std::string& rebased_at_utc) {
  return "POSITION_REBASE_FLAT\t" + EncodeWalText(boot_id) + "\t" +
         EncodeWalText(rebased_at_utc);
}

std::string SerializeCandidateEpisodeClosure(
    const std::string& position_episode_id) {
  return "EPISODE_CLOSED\t" + EncodeWalText(position_episode_id);
}

std::string SerializeCandidateEpisodeClosureV4(
    const CandidateEpisodeClosureRecord& closure) {
  std::ostringstream oss;
  oss << "EPISODE_CLOSED4"
      << '\t' << EncodeWalText(closure.position_episode_id)
      << '\t' << EncodeWalText(closure.decision_id)
      << '\t' << EncodeWalText(closure.candidate_id)
      << '\t' << EncodeWalText(closure.model_version)
      << '\t' << EncodeWalText(closure.mode)
      << '\t' << EncodeWalText(closure.policy_reason)
      << '\t' << EncodeWalText(closure.symbol)
      << '\t' << closure.realized_net_usd
      << '\t' << closure.funding_paid_usd
      << '\t' << closure.fill_event_count
      << '\t' << closure.unique_order_count
      << '\t' << (closure.evidence_complete ? 1 : 0)
      << '\t' << EncodeWalText(closure.activation_transaction_id)
      << '\t' << EncodeWalText(closure.boot_id)
      << '\t' << EncodeWalText(closure.runtime_config_sha256)
      << '\t' << EncodeWalText(closure.trade_bot_sha256)
      << '\t' << EncodeWalText(closure.closed_at_utc);
  return oss.str();
}

bool ParseCandidateEpisodeClosureV2(
    const std::vector<std::string>& fields,
    CandidateEpisodeClosureRecord* out_closure,
    std::string* out_error) {
  if (out_closure == nullptr || fields.size() != 11) {
    if (out_error != nullptr) {
      *out_error = "EPISODE_CLOSED2 WAL 字段数异常";
    }
    return false;
  }
  CandidateEpisodeClosureRecord closure;
  closure.position_episode_id = DecodeWalText(fields[1]);
  closure.decision_id = DecodeWalText(fields[2]);
  closure.candidate_id = DecodeWalText(fields[3]);
  closure.model_version = DecodeWalText(fields[4]);
  closure.mode = DecodeWalText(fields[5]);
  closure.symbol = DecodeWalText(fields[6]);
  try {
    closure.realized_net_usd = std::stod(fields[7]);
    closure.fill_event_count = std::stoi(fields[8]);
    closure.unique_order_count = std::stoi(fields[9]);
    closure.evidence_complete = std::stoi(fields[10]) != 0;
  } catch (const std::exception&) {
    if (out_error != nullptr) {
      *out_error = "EPISODE_CLOSED2 WAL 字段解析失败";
    }
    return false;
  }
  if (closure.position_episode_id.empty() ||
      closure.candidate_id.empty() || closure.model_version.empty() ||
      closure.symbol.empty() || closure.fill_event_count < 0 ||
      closure.unique_order_count < 0) {
    if (out_error != nullptr) {
      *out_error = "EPISODE_CLOSED2 WAL 必填字段异常";
    }
    return false;
  }
  *out_closure = std::move(closure);
  return true;
}

bool ParseCandidateEpisodeClosureV3(
    const std::vector<std::string>& fields,
    CandidateEpisodeClosureRecord* out_closure,
    std::string* out_error) {
  if (out_closure == nullptr || fields.size() != 16) {
    if (out_error != nullptr) {
      *out_error = "EPISODE_CLOSED3 WAL 字段数异常";
    }
    return false;
  }
  CandidateEpisodeClosureRecord closure;
  closure.position_episode_id = DecodeWalText(fields[1]);
  closure.decision_id = DecodeWalText(fields[2]);
  closure.candidate_id = DecodeWalText(fields[3]);
  closure.model_version = DecodeWalText(fields[4]);
  closure.mode = DecodeWalText(fields[5]);
  closure.symbol = DecodeWalText(fields[6]);
  try {
    closure.realized_net_usd = std::stod(fields[7]);
    closure.fill_event_count = std::stoi(fields[8]);
    closure.unique_order_count = std::stoi(fields[9]);
    closure.evidence_complete = std::stoi(fields[10]) != 0;
  } catch (const std::exception&) {
    if (out_error != nullptr) {
      *out_error = "EPISODE_CLOSED3 WAL 字段解析失败";
    }
    return false;
  }
  closure.activation_transaction_id = DecodeWalText(fields[11]);
  closure.boot_id = DecodeWalText(fields[12]);
  closure.runtime_config_sha256 = DecodeWalText(fields[13]);
  closure.trade_bot_sha256 = DecodeWalText(fields[14]);
  closure.closed_at_utc = DecodeWalText(fields[15]);
  if (closure.position_episode_id.empty() ||
      closure.candidate_id.empty() || closure.model_version.empty() ||
      closure.symbol.empty() || closure.fill_event_count < 0 ||
      closure.unique_order_count < 0 || closure.boot_id.empty() ||
      closure.closed_at_utc.empty()) {
    if (out_error != nullptr) {
      *out_error = "EPISODE_CLOSED3 WAL 必填字段异常";
    }
    return false;
  }
  *out_closure = std::move(closure);
  return true;
}

bool ParseCandidateEpisodeClosureV4(
    const std::vector<std::string>& fields,
    CandidateEpisodeClosureRecord* out_closure,
    std::string* out_error) {
  if (out_closure == nullptr || fields.size() != 18) {
    if (out_error != nullptr) {
      *out_error = "EPISODE_CLOSED4 WAL 字段数异常";
    }
    return false;
  }
  CandidateEpisodeClosureRecord closure;
  closure.position_episode_id = DecodeWalText(fields[1]);
  closure.decision_id = DecodeWalText(fields[2]);
  closure.candidate_id = DecodeWalText(fields[3]);
  closure.model_version = DecodeWalText(fields[4]);
  closure.mode = DecodeWalText(fields[5]);
  closure.policy_reason = DecodeWalText(fields[6]);
  closure.symbol = DecodeWalText(fields[7]);
  try {
    closure.realized_net_usd = std::stod(fields[8]);
    closure.funding_paid_usd = std::stod(fields[9]);
    closure.fill_event_count = std::stoi(fields[10]);
    closure.unique_order_count = std::stoi(fields[11]);
    closure.evidence_complete = std::stoi(fields[12]) != 0;
  } catch (const std::exception&) {
    if (out_error != nullptr) {
      *out_error = "EPISODE_CLOSED4 WAL 字段解析失败";
    }
    return false;
  }
  closure.activation_transaction_id = DecodeWalText(fields[13]);
  closure.boot_id = DecodeWalText(fields[14]);
  closure.runtime_config_sha256 = DecodeWalText(fields[15]);
  closure.trade_bot_sha256 = DecodeWalText(fields[16]);
  closure.closed_at_utc = DecodeWalText(fields[17]);
  if (closure.position_episode_id.empty() ||
      closure.candidate_id.empty() || closure.model_version.empty() ||
      closure.policy_reason.empty() || closure.symbol.empty() ||
      !std::isfinite(closure.realized_net_usd) ||
      !std::isfinite(closure.funding_paid_usd) ||
      closure.fill_event_count < 0 || closure.unique_order_count < 0 ||
      closure.boot_id.empty() || closure.closed_at_utc.empty()) {
    if (out_error != nullptr) {
      *out_error = "EPISODE_CLOSED4 WAL 必填字段异常";
    }
    return false;
  }
  *out_closure = std::move(closure);
  return true;
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

bool ParseIntentV2(const std::vector<std::string>& fields,
                   OrderIntent* out_intent,
                   std::string* out_error) {
  if (out_intent == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_intent 为空";
    }
    return false;
  }
  if (fields.size() != 16) {
    if (out_error != nullptr) {
      *out_error = "INTENT2 WAL 字段数异常";
    }
    return false;
  }

  OrderIntent intent;
  intent.client_order_id = fields[1];
  intent.parent_order_id = DecodeWalText(fields[2]);
  intent.symbol = fields[3];
  try {
    intent.purpose = static_cast<OrderPurpose>(std::stoi(fields[4]));
    const int raw_pref = std::stoi(fields[5]);
    if (raw_pref < static_cast<int>(LiquidityPreference::kAuto) ||
        raw_pref > static_cast<int>(LiquidityPreference::kTaker)) {
      if (out_error != nullptr) {
        *out_error = "INTENT2 WAL liquidity_preference 字段非法";
      }
      return false;
    }
    intent.liquidity_preference =
        static_cast<LiquidityPreference>(raw_pref);
    intent.reduce_only = std::stoi(fields[6]) != 0;
    intent.direction = std::stoi(fields[7]);
    intent.qty = std::stod(fields[8]);
    intent.price = std::stod(fields[9]);
  } catch (const std::exception&) {
    if (out_error != nullptr) {
      *out_error = "INTENT2 WAL 字段解析失败";
    }
    return false;
  }
  intent.decision_id = DecodeWalText(fields[10]);
  intent.candidate_id = DecodeWalText(fields[11]);
  intent.model_version = DecodeWalText(fields[12]);
  intent.integrator_mode = DecodeWalText(fields[13]);
  intent.position_episode_id = DecodeWalText(fields[14]);
  intent.integrator_policy_reason = DecodeWalText(fields[15]);
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

bool ParseFillV3(const std::vector<std::string>& fields,
                 FillEvent* out_fill,
                 std::string* out_error) {
  if (fields.size() != 9) {
    if (out_error != nullptr) {
      *out_error = "FILL3 WAL 字段数异常";
    }
    return false;
  }
  std::vector<std::string> v2_fields(fields.begin(), fields.begin() + 8);
  FillEvent fill;
  if (!ParseFillV2(v2_fields, &fill, out_error)) {
    return false;
  }
  try {
    const int raw_liquidity = std::stoi(fields[8]);
    if (raw_liquidity < static_cast<int>(FillLiquidity::kUnknown) ||
        raw_liquidity > static_cast<int>(FillLiquidity::kTaker)) {
      if (out_error != nullptr) {
        *out_error = "FILL3 WAL liquidity 字段非法";
      }
      return false;
    }
    fill.liquidity = static_cast<FillLiquidity>(raw_liquidity);
  } catch (const std::exception&) {
    if (out_error != nullptr) {
      *out_error = "FILL3 WAL liquidity 字段解析失败";
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
  const int fd =
      ::open(file_path_.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
  if (fd < 0) {
    if (out_error != nullptr) {
      *out_error =
          "WAL 打开失败: " + file_path_ + ": " + std::strerror(errno);
    }
    return false;
  }

  const std::string payload = line + '\n';
  std::size_t written = 0;
  while (written < payload.size()) {
    const ssize_t count =
        ::write(fd, payload.data() + written, payload.size() - written);
    if (count < 0 && errno == EINTR) {
      continue;
    }
    if (count <= 0) {
      const int write_errno = errno;
      ::close(fd);
      if (out_error != nullptr) {
        *out_error = "WAL 写入失败: " +
                     std::string(std::strerror(write_errno));
      }
      return false;
    }
    written += static_cast<std::size_t>(count);
  }
  if (::fsync(fd) != 0) {
    const int sync_errno = errno;
    ::close(fd);
    if (out_error != nullptr) {
      *out_error =
          "WAL fsync 失败: " + std::string(std::strerror(sync_errno));
    }
    return false;
  }
  if (::close(fd) != 0) {
    if (out_error != nullptr) {
      *out_error =
          "WAL 关闭失败: " + std::string(std::strerror(errno));
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
  return AppendLine(SerializeFillV3(fill), out_error);
}

bool WalStore::AppendFlatPositionRebase(const std::string& boot_id,
                                        const std::string& rebased_at_utc,
                                        std::string* out_error) const {
  if (boot_id.empty() || rebased_at_utc.empty()) {
    if (out_error != nullptr) {
      *out_error = "position rebase boot_id/rebased_at_utc 为空";
    }
    return false;
  }
  return AppendLine(SerializeFlatPositionRebase(boot_id, rebased_at_utc),
                    out_error);
}

bool WalStore::AppendCandidateEpisodeClosure(
    const std::string& position_episode_id,
    std::string* out_error) const {
  if (position_episode_id.empty()) {
    if (out_error != nullptr) {
      *out_error = "position_episode_id 为空";
    }
    return false;
  }
  return AppendLine(SerializeCandidateEpisodeClosure(position_episode_id),
                    out_error);
}

bool WalStore::AppendCandidateEpisodeClosure(
    const CandidateEpisodeClosureRecord& closure,
    std::string* out_error) const {
  if (closure.position_episode_id.empty() ||
      closure.candidate_id.empty() || closure.model_version.empty() ||
      closure.policy_reason.empty() || closure.symbol.empty() ||
      !std::isfinite(closure.realized_net_usd) ||
      !std::isfinite(closure.funding_paid_usd) ||
      closure.boot_id.empty() || closure.closed_at_utc.empty()) {
    if (out_error != nullptr) {
      *out_error = "candidate episode closure 必填字段为空";
    }
    return false;
  }
  return AppendLine(SerializeCandidateEpisodeClosureV4(closure), out_error);
}

bool WalStore::LoadState(std::unordered_set<std::string>* out_intent_ids,
                         std::unordered_set<std::string>* out_fill_ids,
                         std::vector<FillEvent>* out_fills,
                         std::string* out_error,
                         std::unordered_map<std::string, OrderIntent>*
                             out_intents,
                         std::unordered_set<std::string>*
                             out_closed_episode_ids,
                         std::unordered_map<
                             std::string,
                             CandidateEpisodeClosureRecord>*
                             out_episode_closures) const {
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
  if (out_intents != nullptr) {
    out_intents->clear();
  }
  if (out_closed_episode_ids != nullptr) {
    out_closed_episode_ids->clear();
  }
  if (out_episode_closures != nullptr) {
    out_episode_closures->clear();
  }

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
    if (type == "INTENT" || type == "INTENT2") {
      OrderIntent intent;
      std::string parse_error;
      const bool parsed =
          type == "INTENT2"
              ? ParseIntentV2(fields, &intent, &parse_error)
              : ParseIntent(fields, &intent, &parse_error);
      if (!parsed) {
        if (out_error != nullptr) {
          *out_error = "WAL 行解析失败（line=" + std::to_string(line_no) +
                       "）: " + parse_error;
        }
        return false;
      }
      out_intent_ids->insert(intent.client_order_id);
      if (out_intents != nullptr) {
        out_intents->insert_or_assign(intent.client_order_id, intent);
      }
      continue;
    }
    if (type == "FILL2" || type == "FILL3") {
      FillEvent fill;
      std::string parse_error;
      const bool parsed =
          type == "FILL3"
              ? ParseFillV3(fields, &fill, &parse_error)
              : ParseFillV2(fields, &fill, &parse_error);
      if (!parsed) {
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
    if (type == "POSITION_REBASE_FLAT") {
      if (fields.size() != 3 || DecodeWalText(fields[1]).empty() ||
          DecodeWalText(fields[2]).empty()) {
        if (out_error != nullptr) {
          *out_error = "WAL 行解析失败（line=" + std::to_string(line_no) +
                       "）: POSITION_REBASE_FLAT 字段异常";
        }
        return false;
      }
      // fill_ids 继续保留全历史去重集合；仓位与未闭合 episode 只允许从
      // 最近一次权威空仓检查点之后的成交重建。
      out_fills->clear();
      continue;
    }
    if (type == "EPISODE_CLOSED") {
      if (fields.size() != 2 || DecodeWalText(fields[1]).empty()) {
        if (out_error != nullptr) {
          *out_error = "WAL 行解析失败（line=" + std::to_string(line_no) +
                       "）: EPISODE_CLOSED 字段异常";
        }
        return false;
      }
      if (out_closed_episode_ids != nullptr) {
        out_closed_episode_ids->insert(DecodeWalText(fields[1]));
      }
      continue;
    }
    if (type == "EPISODE_CLOSED2") {
      CandidateEpisodeClosureRecord closure;
      std::string parse_error;
      if (!ParseCandidateEpisodeClosureV2(
              fields, &closure, &parse_error)) {
        if (out_error != nullptr) {
          *out_error = "WAL 行解析失败（line=" +
                       std::to_string(line_no) + "）: " + parse_error;
        }
        return false;
      }
      if (out_closed_episode_ids != nullptr) {
        out_closed_episode_ids->insert(closure.position_episode_id);
      }
      if (out_episode_closures != nullptr) {
        out_episode_closures->insert_or_assign(
            closure.position_episode_id, closure);
      }
      continue;
    }
    if (type == "EPISODE_CLOSED3") {
      CandidateEpisodeClosureRecord closure;
      std::string parse_error;
      if (!ParseCandidateEpisodeClosureV3(
              fields, &closure, &parse_error)) {
        if (out_error != nullptr) {
          *out_error = "WAL 行解析失败（line=" +
                       std::to_string(line_no) + "）: " + parse_error;
        }
        return false;
      }
      if (out_closed_episode_ids != nullptr) {
        out_closed_episode_ids->insert(closure.position_episode_id);
      }
      if (out_episode_closures != nullptr) {
        out_episode_closures->insert_or_assign(
            closure.position_episode_id, closure);
      }
      continue;
    }
    if (type == "EPISODE_CLOSED4") {
      CandidateEpisodeClosureRecord closure;
      std::string parse_error;
      if (!ParseCandidateEpisodeClosureV4(
              fields, &closure, &parse_error)) {
        if (out_error != nullptr) {
          *out_error = "WAL 行解析失败（line=" +
                       std::to_string(line_no) + "）: " + parse_error;
        }
        return false;
      }
      if (out_closed_episode_ids != nullptr) {
        out_closed_episode_ids->insert(closure.position_episode_id);
      }
      if (out_episode_closures != nullptr) {
        out_episode_closures->insert_or_assign(
            closure.position_episode_id, closure);
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
