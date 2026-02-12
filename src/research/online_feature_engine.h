#pragma once

#include <deque>
#include <string>
#include <unordered_map>
#include <vector>
#include <memory>
#include <functional>
#include <optional>

#include "core/types.h"

namespace ai_trade::research {

// 简单的环形缓冲区，用于存储 OHLCV 历史数据
class RollingBuffer {
 public:
  explicit RollingBuffer(size_t capacity);
  void Add(double value);
  // 获取最近 N 个数据（按时间正序，最新的在最后）
  // 如果数据不足 N 个，返回空 vector 或填充 NaN（视策略而定）
  std::vector<double> GetLast(size_t n) const;
  size_t size() const { return buffer_.size(); }
  double back() const { return buffer_.empty() ? 0.0 : buffer_.back(); }
  bool empty() const { return buffer_.empty(); }

 private:
  std::deque<double> buffer_;
  size_t capacity_;
};

// 在线特征计算引擎
// 职责：维护 OHLCV 窗口，解析因子表达式，计算当前 Tick 的特征向量
class OnlineFeatureEngine {
 public:
  explicit OnlineFeatureEngine(size_t window_size);

  // 接收实时行情更新缓冲区
  void OnMarket(const MarketEvent& event);

  // 计算单个表达式的值
  // 支持算子：ts_delay, ts_delta, ts_rank, ts_corr, +, -, *, /
  // 变量名：open, high, low, close, volume
  // 返回：计算结果，如果数据不足或计算错误返回 NaN
  double Evaluate(const std::string& expression) const;

  // 批量计算特征向量（用于模型推理）
  std::vector<double> EvaluateBatch(const std::vector<std::string>& expressions) const;

  // 检查缓冲区是否已满足最小计算长度
  bool IsReady() const;

 private:
  size_t window_size_;
  std::unordered_map<std::string, RollingBuffer> series_;
  
  // 简单的递归下降解析器或栈式求值器辅助函数
  // 实际实现中可能需要一个轻量级的 ExpressionParser 类
  double EvaluateRecursive(const std::string& expr) const;
};

}  // namespace ai_trade::research