#pragma once

#include <cstdint>
#include <string>

namespace ai_trade {

/// 交易账户类型（跨交易所统一语义）。
enum class AccountMode {
  kClassic,
  kUnified,
};

/// 保证金模式（跨交易所统一语义）。
enum class MarginMode {
  kIsolated,
  kCross,
  kPortfolio,
};

/// 持仓模式（单向/双向）。
enum class PositionMode {
  kOneWay,
  kHedge,
};

/// 行情事件：策略与账户估值共用输入。
struct MarketEvent {
  std::int64_t ts_ms{0};
  std::string symbol{"BTCUSDT"};
  double price{0.0};
  double mark_price{0.0};
  double volume{0.0};  // 24h 成交量或当前 bar 成交量（取决于源）
};

/// Regime 四态：趋势上行、趋势下行、震荡、极端。
enum class Regime {
  kUptrend,
  kDowntrend,
  kRange,
  kExtreme,
};

/// Regime 训练/评估分桶：趋势、震荡、极端。
enum class RegimeBucket {
  kTrend,
  kRange,
  kExtreme,
};

/// Regime 运行态快照（用于策略/自进化/审计日志）。
struct RegimeState {
  std::string symbol{"BTCUSDT"};
  Regime regime{Regime::kRange};
  RegimeBucket bucket{RegimeBucket::kRange};
  double instant_return{0.0};   // 当前 tick 简单收益率。
  double trend_strength{0.0};   // EWMA 收益（带方向）。
  double volatility_level{0.0}; // EWMA 绝对收益（无方向）。
  bool warmup{true};            // 样本不足时为 true（默认按 range 处理）。
};

/// 策略原始输出：仅表达期望净名义敞口，不含风控约束。
struct Signal {
  std::string symbol{"BTCUSDT"};
  double suggested_notional_usd{0.0};
  int direction{0};  // -1 代表做空，0 代表空仓，1 代表做多
};

/// Integrator 影子推理输出（仅用于运行态观测，不直接驱动下单）。
struct ShadowInference {
  bool enabled{false};
  std::string model_version{"n/a"};
  double model_score{0.0};  // 连续分数，范围建议 [-6, 6]。
  double p_up{0.5};  // 上涨概率。
  double p_down{0.5};  // 下跌概率。
};

/// 风控输入目标净名义敞口（USD, signed）。
struct TargetPosition {
  std::string symbol{"BTCUSDT"};
  double target_notional_usd{0.0};
};

/// 风险状态机阶段。
enum class RiskMode {
  kNormal,
  kDegraded,
  kCooldown,
  kFuse,
  kReduceOnly,
};

/// 风险阈值集合（回撤与强平距离）。
struct RiskThresholds {
  double degraded_drawdown{0.08};
  double cooldown_drawdown{0.12};
  double fuse_drawdown{0.20};
  double min_liquidation_distance{0.08}; // 默认 8%
};

/// 风控输出：最终可执行目标净名义敞口。
struct RiskAdjustedPosition {
  std::string symbol{"BTCUSDT"};
  double adjusted_notional_usd{0.0};
  bool reduce_only{false};
  RiskMode risk_mode{RiskMode::kNormal};
};

/// 订单用途分类（开仓/止盈/止损/减仓）。
enum class OrderPurpose {
  kEntry,
  kTp,
  kSl,
  kReduce,
};

/// 订单意图：执行层标准输入。
struct OrderIntent {
  std::string client_order_id;
  std::string parent_order_id;
  std::string symbol{"BTCUSDT"};
  OrderPurpose purpose{OrderPurpose::kEntry};
  bool reduce_only{false};
  int direction{0};
  double qty{0.0};
  double price{0.0};
};

/// 成交回报：由交易所返回并驱动 OMS/账户状态更新。
struct FillEvent {
  std::string fill_id;
  std::string client_order_id;
  std::string symbol{"BTCUSDT"};
  int direction{0};
  double qty{0.0};
  double price{0.0};
  double fee{0.0};
};

/// 交易所账户模式快照（用于启动准入与运行时校验）。
struct ExchangeAccountSnapshot {
  AccountMode account_mode{AccountMode::kUnified};
  MarginMode margin_mode{MarginMode::kIsolated};
  PositionMode position_mode{PositionMode::kOneWay};
};

/// 远端持仓快照：用于启动同步和对账。
struct RemotePositionSnapshot {
  std::string symbol{"BTCUSDT"};
  double qty{0.0};  // 带方向数量：多>0，空<0
  double avg_entry_price{0.0};
  double mark_price{0.0};
  double liquidation_price{0.0};  // 强平价格；<=0 表示未知/不可用
};

/// 远端账户资金快照：用于现金基线/权益口径同步。
struct RemoteAccountBalanceSnapshot {
  double equity_usd{0.0};  // 账户总权益（含未实现盈亏）。
  double wallet_balance_usd{0.0};  // 钱包余额（不含未实现盈亏）。
  double unrealized_pnl_usd{0.0};  // 交易所报告的未实现盈亏。
  bool has_equity{false};
  bool has_wallet_balance{false};
  bool has_unrealized_pnl{false};
};

/// symbol 级交易规则（数量/价格精度与最小下单约束）。
struct SymbolInfo {
  std::string symbol{"BTCUSDT"};
  bool tradable{false};
  double qty_step{0.0};
  double min_order_qty{0.0};
  double min_notional_usd{0.0};
  double price_tick{0.0};
  int qty_precision{8};
  int price_precision{8};
};

/// AccountMode 文本化（用于日志展示）。
inline const char* ToString(AccountMode mode) {
  switch (mode) {
    case AccountMode::kClassic:
      return "CLASSIC";
    case AccountMode::kUnified:
      return "UNIFIED";
  }
  return "UNKNOWN";
}

/// MarginMode 文本化（用于日志展示）。
inline const char* ToString(MarginMode mode) {
  switch (mode) {
    case MarginMode::kIsolated:
      return "ISOLATED";
    case MarginMode::kCross:
      return "CROSS";
    case MarginMode::kPortfolio:
      return "PORTFOLIO";
  }
  return "UNKNOWN";
}

/// PositionMode 文本化（用于日志展示）。
inline const char* ToString(PositionMode mode) {
  switch (mode) {
    case PositionMode::kOneWay:
      return "ONE_WAY";
    case PositionMode::kHedge:
      return "HEDGE";
  }
  return "UNKNOWN";
}

/// Regime 文本化（用于日志展示）。
inline const char* ToString(Regime regime) {
  switch (regime) {
    case Regime::kUptrend:
      return "UPTREND";
    case Regime::kDowntrend:
      return "DOWNTREND";
    case Regime::kRange:
      return "RANGE";
    case Regime::kExtreme:
      return "EXTREME";
  }
  return "UNKNOWN";
}

/// Regime 分桶文本化（用于日志展示）。
inline const char* ToString(RegimeBucket bucket) {
  switch (bucket) {
    case RegimeBucket::kTrend:
      return "TREND";
    case RegimeBucket::kRange:
      return "RANGE";
    case RegimeBucket::kExtreme:
      return "EXTREME";
  }
  return "UNKNOWN";
}

/// 自进化权重结构体
struct EvolutionWeights {
  double trend_weight{0.0};
  double defensive_weight{0.0};
};

}  // namespace ai_trade
