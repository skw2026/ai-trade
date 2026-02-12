#include "research/online_feature_engine.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cmath>
#include <iostream>
#include <sstream>
#include <stack>
#include <stdexcept>
#include <string_view>
#include <unordered_map>

#include "core/log.h"
#include "research/time_series_operators.h"

namespace ai_trade::research {

namespace {

// --- 辅助工具函数 ---

bool IsSpace(char c) {
  return std::isspace(static_cast<unsigned char>(c));
}

bool IsDigit(char c) {
  return std::isdigit(static_cast<unsigned char>(c));
}

bool IsAlpha(char c) {
  return std::isalpha(static_cast<unsigned char>(c));
}

// 向量二元运算辅助
std::vector<double> BinaryOp(const std::vector<double>& lhs,
                             const std::vector<double>& rhs,
                             char op) {
  const size_t n = std::min(lhs.size(), rhs.size());
  std::vector<double> res;
  res.reserve(n);
  for (size_t i = 0; i < n; ++i) {
    double val = 0.0;
    if (!IsFinite(lhs[i]) || !IsFinite(rhs[i])) {
      val = std::numeric_limits<double>::quiet_NaN();
    } else {
      switch (op) {
        case '+': val = lhs[i] + rhs[i]; break;
        case '-': val = lhs[i] - rhs[i]; break;
        case '*': val = lhs[i] * rhs[i]; break;
        case '/': 
          val = (std::abs(rhs[i]) > 1e-9) ? lhs[i] / rhs[i] : 0.0; 
          break;
        default: val = 0.0; break;
      }
    }
    res.push_back(val);
  }
  return res;
}

// --- 表达式解析器 ---

enum class TokenType {
  kNumber,
  kIdentifier,
  kLParen,
  kRParen,
  kComma,
  kPlus,
  kMinus,
  kMultiply,
  kDivide,
  kEnd
};

struct Token {
  TokenType type;
  std::string text;
  double number_value{0.0};
};

class ExpressionParser {
 public:
  ExpressionParser(const std::string& expression,
                   const std::unordered_map<std::string, RollingBuffer>& series,
                   size_t window_size)
      : expression_(expression), series_(series), window_size_(window_size) {
    Tokenize();
  }

  std::vector<double> Parse() {
    cursor_ = 0;
    return ParseExpression();
  }

 private:
  void Tokenize() {
    size_t i = 0;
    while (i < expression_.length()) {
      if (IsSpace(expression_[i])) {
        ++i;
        continue;
      }
      
      char c = expression_[i];
      if (IsDigit(c) || c == '.') {
        size_t start = i;
        bool has_dot = (c == '.');
        ++i;
        while (i < expression_.length() && (IsDigit(expression_[i]) || expression_[i] == '.')) {
          if (expression_[i] == '.') {
            if (has_dot) break; // 第二个点，停止
            has_dot = true;
          }
          ++i;
        }
        std::string num_str = expression_.substr(start, i - start);
        tokens_.push_back({TokenType::kNumber, num_str, std::stod(num_str)});
      } else if (IsAlpha(c) || c == '_') {
        size_t start = i;
        ++i;
        while (i < expression_.length() && (IsAlpha(expression_[i]) || IsDigit(expression_[i]) || expression_[i] == '_')) {
          ++i;
        }
        tokens_.push_back({TokenType::kIdentifier, expression_.substr(start, i - start), 0.0});
      } else {
        switch (c) {
          case '(': tokens_.push_back({TokenType::kLParen, "(", 0.0}); break;
          case ')': tokens_.push_back({TokenType::kRParen, ")", 0.0}); break;
          case ',': tokens_.push_back({TokenType::kComma, ",", 0.0}); break;
          case '+': tokens_.push_back({TokenType::kPlus, "+", 0.0}); break;
          case '-': tokens_.push_back({TokenType::kMinus, "-", 0.0}); break;
          case '*': tokens_.push_back({TokenType::kMultiply, "*", 0.0}); break;
          case '/': tokens_.push_back({TokenType::kDivide, "/", 0.0}); break;
          default: 
            // 忽略未知字符
            break;
        }
        ++i;
      }
    }
    tokens_.push_back({TokenType::kEnd, "", 0.0});
  }

  const Token& Peek() const {
    if (cursor_ < tokens_.size()) return tokens_[cursor_];
    return tokens_.back();
  }

  Token Consume() {
    Token t = Peek();
    if (cursor_ < tokens_.size()) ++cursor_;
    return t;
  }

  bool Match(TokenType type) {
    if (Peek().type == type) {
      Consume();
      return true;
    }
    return false;
  }

  // Grammar:
  // Expression -> Term { (+|-) Term }
  // Term -> Factor { (*|/) Factor }
  // Factor -> Number | Identifier | Identifier(Args) | (Expression) | -Factor

  std::vector<double> ParseExpression() {
    std::vector<double> lhs = ParseTerm();
    while (Peek().type == TokenType::kPlus || Peek().type == TokenType::kMinus) {
      Token op = Consume();
      std::vector<double> rhs = ParseTerm();
      lhs = BinaryOp(lhs, rhs, op.text[0]);
    }
    return lhs;
  }

  std::vector<double> ParseTerm() {
    std::vector<double> lhs = ParseFactor();
    while (Peek().type == TokenType::kMultiply || Peek().type == TokenType::kDivide) {
      Token op = Consume();
      std::vector<double> rhs = ParseFactor();
      lhs = BinaryOp(lhs, rhs, op.text[0]);
    }
    return lhs;
  }

  std::vector<double> ParseFactor() {
    Token t = Peek();
    
    if (t.type == TokenType::kNumber) {
      Consume();
      // 返回常数向量
      return std::vector<double>(window_size_, t.number_value);
    } 
    else if (t.type == TokenType::kIdentifier) {
      Consume();
      if (Peek().type == TokenType::kLParen) {
        // Function call: func(arg1, arg2...)
        return ParseFunctionCall(t.text);
      } else {
        // Variable
        return GetVariable(t.text);
      }
    } 
    else if (t.type == TokenType::kLParen) {
      Consume();
      std::vector<double> val = ParseExpression();
      if (!Match(TokenType::kRParen)) {
        // 简单的错误处理：返回空或 NaN 向量
        // LogError("Expected ')'");
      }
      return val;
    }
    else if (t.type == TokenType::kMinus) {
      Consume();
      std::vector<double> val = ParseFactor();
      for (auto& v : val) v = -v;
      return val;
    }

    // Error case
    return std::vector<double>(window_size_, std::numeric_limits<double>::quiet_NaN());
  }

  std::vector<double> ParseFunctionCall(const std::string& func_name) {
    Consume(); // '('
    std::vector<std::vector<double>> args;
    if (Peek().type != TokenType::kRParen) {
      args.push_back(ParseExpression());
      while (Match(TokenType::kComma)) {
        args.push_back(ParseExpression());
      }
    }
    Match(TokenType::kRParen);

    // Dispatch function
    if (func_name == "ts_delay" && args.size() == 2) {
      int delay = static_cast<int>(GetScalar(args[1]));
      return TsDelay(args[0], delay);
    }
    if (func_name == "ts_delta" && args.size() == 2) {
      int delay = static_cast<int>(GetScalar(args[1]));
      return TsDelta(args[0], delay);
    }
    if (func_name == "ts_rank" && args.size() == 2) {
      int window = static_cast<int>(GetScalar(args[1]));
      return TsRank(args[0], window);
    }
    if (func_name == "ts_corr" && args.size() == 3) {
      int window = static_cast<int>(GetScalar(args[2]));
      return TsCorr(args[0], args[1], window);
    }
    if (func_name == "rsi" && args.size() == 2) {
      int period = static_cast<int>(GetScalar(args[1]));
      return TsRsi(args[0], period);
    }
    if (func_name == "ema" && args.size() == 2) {
      int period = static_cast<int>(GetScalar(args[1]));
      // 精度警告：EMA 是递归算子，在滑动窗口上计算存在冷启动偏差。
      // 经验法则：窗口长度应至少为周期的 3 倍以保证误差 < 1% (对于 alpha=2/(N+1))。
      if (period > 0 && window_size_ < static_cast<size_t>(period * 3)) {
        static std::atomic<int> warn_throttle{0};
        // 限频日志，避免刷屏
        if (warn_throttle.fetch_add(1, std::memory_order_relaxed) % 100 == 0) {
           LogInfo("WARN: EMA period " + std::to_string(period) + 
                   " is too large for window " + std::to_string(window_size_) + 
                   ". Precision loss expected (recommend window >= 3*period).");
        }
      }
      return TsEma(args[0], period);
    }
    if (func_name == "abs" && args.size() == 1) {
      std::vector<double> res = args[0];
      for (auto& v : res) v = std::abs(v);
      return res;
    }

    // Unknown function or wrong args
    return std::vector<double>(window_size_, std::numeric_limits<double>::quiet_NaN());
  }

  std::vector<double> GetVariable(const std::string& name) {
    auto it = series_.find(name);
    if (it != series_.end()) {
      // 获取最近 window_size_ 个数据
      std::vector<double> data = it->second.GetLast(window_size_);
      // 如果数据不足，前面补 NaN 以对齐长度，保证计算逻辑一致
      if (data.size() < window_size_) {
        std::vector<double> padded(window_size_ - data.size(), std::numeric_limits<double>::quiet_NaN());
        padded.insert(padded.end(), data.begin(), data.end());
        return padded;
      }
      return data;
    }
    return std::vector<double>(window_size_, std::numeric_limits<double>::quiet_NaN());
  }

  double GetScalar(const std::vector<double>& vec) {
    if (vec.empty()) return 0.0;
    // 通常常数向量所有值都一样，取最后一个即可
    return vec.back();
  }

  std::string expression_;
  const std::unordered_map<std::string, RollingBuffer>& series_;
  size_t window_size_;
  std::vector<Token> tokens_;
  size_t cursor_{0};
};

}  // namespace

// --- RollingBuffer 实现 ---

RollingBuffer::RollingBuffer(size_t capacity) : capacity_(capacity) {}

void RollingBuffer::Add(double value) {
  buffer_.push_back(value);
  if (buffer_.size() > capacity_) {
    buffer_.pop_front();
  }
}

std::vector<double> RollingBuffer::GetLast(size_t n) const {
  if (buffer_.empty()) {
    return {};
  }
  size_t count = std::min(n, buffer_.size());
  std::vector<double> result;
  result.reserve(count);
  
  // 从后往前取 count 个，保持时间正序
  auto it = buffer_.end();
  std::advance(it, -static_cast<std::ptrdiff_t>(count));
  result.insert(result.end(), it, buffer_.end());
  return result;
}

// --- OnlineFeatureEngine 实现 ---

OnlineFeatureEngine::OnlineFeatureEngine(size_t window_size)
    : window_size_(window_size) {
  // 预先初始化基础变量的 buffer
  series_.emplace("open", RollingBuffer(window_size));
  series_.emplace("high", RollingBuffer(window_size));
  series_.emplace("low", RollingBuffer(window_size));
  series_.emplace("close", RollingBuffer(window_size));
  series_.emplace("volume", RollingBuffer(window_size));
}

void OnlineFeatureEngine::OnMarket(const MarketEvent& event) {
  // 注意：MarketEvent 目前仅包含 price/mark_price。
  // 为了支持 Miner 的 OHLCV 因子，我们暂时将 price 映射到 OHLC，volume 设为 0。
  // 生产环境应通过 BarAggregator 或扩展 MarketEvent 来提供真实的 OHLCV。
  series_.at("open").Add(event.price);
  series_.at("high").Add(event.price);
  series_.at("low").Add(event.price);
  series_.at("close").Add(event.price);
  series_.at("volume").Add(event.volume);
}

double OnlineFeatureEngine::Evaluate(const std::string& expression) const {
  return EvaluateRecursive(expression);
}

std::vector<double> OnlineFeatureEngine::EvaluateBatch(
    const std::vector<std::string>& expressions) const {
  std::vector<double> results;
  results.reserve(expressions.size());
  for (const auto& expr : expressions) {
    results.push_back(Evaluate(expr));
  }
  return results;
}

bool OnlineFeatureEngine::IsReady() const {
  if (series_.empty()) return false;
  // 放宽限制：只要有数据即可尝试计算。
  // 具体算子（如 ts_rank）会在数据不足时返回 NaN，由上层处理。
  return !series_.at("close").empty();
}

double OnlineFeatureEngine::EvaluateRecursive(const std::string& expr) const {
  if (expr.empty()) return std::numeric_limits<double>::quiet_NaN();
  
  try {
    ExpressionParser parser(expr, series_, window_size_);
    std::vector<double> result_vec = parser.Parse();
    
    if (result_vec.empty()) return std::numeric_limits<double>::quiet_NaN();
    
    // 返回当前 tick 的值（向量的最后一个元素）
    return result_vec.back();
  } catch (const std::exception& e) {
    // 解析错误或计算错误，返回 0.0 并记录日志（实际生产中应限频日志）
    // LogError("Feature eval failed: " + std::string(e.what()));
    return std::numeric_limits<double>::quiet_NaN();
  }
}

}  // namespace ai_trade::research
