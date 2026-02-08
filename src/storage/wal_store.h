#pragma once

#include <string>
#include <unordered_set>
#include <vector>

#include "core/types.h"

namespace ai_trade {

class WalStore {
 public:
  explicit WalStore(std::string file_path) : file_path_(std::move(file_path)) {}

  // 初始化 WAL：确保父目录存在并创建文件（若不存在）。
  bool Initialize(std::string* out_error) const;

  bool AppendIntent(const OrderIntent& intent, std::string* out_error) const;
  bool AppendFill(const FillEvent& fill, std::string* out_error) const;

  // 加载 WAL 中的意图与成交，供进程重启恢复使用。
  bool LoadState(std::unordered_set<std::string>* out_intent_ids,
                 std::unordered_set<std::string>* out_fill_ids,
                 std::vector<FillEvent>* out_fills,
                 std::string* out_error) const;

 private:
  bool AppendLine(const std::string& line, std::string* out_error) const;
  std::string file_path_;
};

}  // namespace ai_trade
