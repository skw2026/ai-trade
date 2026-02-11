#pragma once

#include <string>
#include <unordered_set>
#include <vector>

#include "core/types.h"

namespace ai_trade {

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

  /// 加载 WAL 中的意图与成交，用于重启恢复。
  bool LoadState(std::unordered_set<std::string>* out_intent_ids,
                 std::unordered_set<std::string>* out_fill_ids,
                 std::vector<FillEvent>* out_fills,
                 std::string* out_error) const;

 private:
  /// 追加单行文本到 WAL 文件（append + flush）。
  bool AppendLine(const std::string& line, std::string* out_error) const;
  std::string file_path_;  ///< WAL 文件路径。
};

}  // namespace ai_trade
