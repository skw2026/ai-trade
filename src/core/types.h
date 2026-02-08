#pragma once

#include <cstdint>
#include <string>

namespace ai_trade {

enum class AccountMode {
  kClassic,
  kUnified,
};

enum class MarginMode {
  kIsolated,
  kCross,
  kPortfolio,
};

enum class PositionMode {
  kOneWay,
  kHedge,
};

struct MarketEvent {
  std::int64_t ts_ms{0};
  std::string symbol{"BTCUSDT"};
  double price{0.0};
  double mark_price{0.0};
};

struct Signal {
  std::string symbol{"BTCUSDT"};
  double suggested_notional_usd{0.0};
  int direction{0};  // -1 代表做空，0 代表空仓，1 代表做多
};

struct TargetPosition {
  std::string symbol{"BTCUSDT"};
  double target_notional_usd{0.0};
};

enum class RiskMode {
  kNormal,
  kDegraded,
  kCooldown,
  kFuse,
  kReduceOnly,
};

struct RiskThresholds {
  double degraded_drawdown{0.08};
  double cooldown_drawdown{0.12};
  double fuse_drawdown{0.20};
};

struct RiskAdjustedPosition {
  std::string symbol{"BTCUSDT"};
  double adjusted_notional_usd{0.0};
  bool reduce_only{false};
  RiskMode risk_mode{RiskMode::kNormal};
};

enum class OrderPurpose {
  kEntry,
  kTp,
  kSl,
};

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

struct FillEvent {
  std::string fill_id;
  std::string client_order_id;
  std::string symbol{"BTCUSDT"};
  int direction{0};
  double qty{0.0};
  double price{0.0};
  double fee{0.0};
};

struct ExchangeAccountSnapshot {
  AccountMode account_mode{AccountMode::kUnified};
  MarginMode margin_mode{MarginMode::kIsolated};
  PositionMode position_mode{PositionMode::kOneWay};
};

struct RemotePositionSnapshot {
  std::string symbol{"BTCUSDT"};
  double qty{0.0};  // 带方向数量：多>0，空<0
  double avg_entry_price{0.0};
  double mark_price{0.0};
};

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

inline const char* ToString(AccountMode mode) {
  switch (mode) {
    case AccountMode::kClassic:
      return "CLASSIC";
    case AccountMode::kUnified:
      return "UNIFIED";
  }
  return "UNKNOWN";
}

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

inline const char* ToString(PositionMode mode) {
  switch (mode) {
    case PositionMode::kOneWay:
      return "ONE_WAY";
    case PositionMode::kHedge:
      return "HEDGE";
  }
  return "UNKNOWN";
}

}  // namespace ai_trade
