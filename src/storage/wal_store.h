#pragma once

#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "core/types.h"

namespace ai_trade {

struct CandidateEpisodeClosureRecord {
  std::string position_episode_id;
  std::string decision_id;
  std::string candidate_id;
  std::string model_version;
  std::string mode;
  std::string policy_reason;
  std::string symbol;
  double realized_net_usd{0.0};
  double funding_paid_usd{0.0};
  int fill_event_count{0};
  int unique_order_count{0};
  bool evidence_complete{false};
  std::string activation_transaction_id;
  std::string boot_id;
  std::string runtime_config_sha256;
  std::string trade_bot_sha256;
  std::string closed_at_utc;
};

/**
 * @brief 本地 WAL（Write-Ahead Log）
 *
 * 语义：
 * 1. 先写意图/成交再推进内存状态；
 * 2. 支持进程重启恢复；
 * 3. 支持成交去重（依赖 fill_id）。
 */
class WalStore {
 public:
  explicit WalStore(std::string file_path) : file_path_(std::move(file_path)) {}

  /// 初始化 WAL：确保父目录存在并创建文件（若不存在）。
  bool Initialize(std::string* out_error) const;

  /// 追加一条订单意图记录。
  bool AppendIntent(const OrderIntent& intent, std::string* out_error) const;
  /// 追加一条成交记录。
  bool AppendFill(const FillEvent& fill, std::string* out_error) const;
  /// 在交易所确认空仓且无活动订单后，固化新的空仓恢复基线。
  bool AppendFlatPositionRebase(const std::string& boot_id,
                                const std::string& rebased_at_utc,
                                std::string* out_error) const;
  /// 追加候选交易 episode 闭合确认，防止重启后丢失或重复记账。
  bool AppendCandidateEpisodeClosure(const std::string& position_episode_id,
                                     std::string* out_error) const;
  bool AppendCandidateEpisodeClosure(
      const CandidateEpisodeClosureRecord& closure,
      std::string* out_error) const;

  /// 加载 WAL 中的意图与成交，用于重启恢复。
  bool LoadState(std::unordered_set<std::string>* out_intent_ids,
                 std::unordered_set<std::string>* out_fill_ids,
                 std::vector<FillEvent>* out_fills,
                 std::string* out_error,
                 std::unordered_map<std::string, OrderIntent>* out_intents =
                     nullptr,
                 std::unordered_set<std::string>* out_closed_episode_ids =
                     nullptr,
                 std::unordered_map<std::string, CandidateEpisodeClosureRecord>*
                     out_episode_closures = nullptr) const;

 private:
  /// 追加单行文本到 WAL 文件（append + fsync）。
  bool AppendLine(const std::string& line, std::string* out_error) const;
  std::string file_path_;  ///< WAL 文件路径。
};

}  // namespace ai_trade
