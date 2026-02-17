#pragma once

#include <cmath>
#include <cstdint>
#include <string>
#include <vector>
#include <array>

namespace ai_trade {

// ============================================================================
// Fundamental Types & Enums
// ============================================================================

using Timestamp = std::int64_t;
using Price = double;
using Quantity = double;
using Money = double;

/// Trading Side (Type-safe wrapper around direction)
enum class Side {
  kBuy = 1,
  kSell = -1,
  kNone = 0,
};

inline int SideToInt(Side side) { return static_cast<int>(side); }
inline Side IntToSide(int direction) {
  if (direction > 0) return Side::kBuy;
  if (direction < 0) return Side::kSell;
  return Side::kNone;
}

enum class AccountMode { kClassic, kUnified };
enum class MarginMode { kIsolated, kCross, kPortfolio };
enum class PositionMode { kOneWay, kHedge };

enum class Regime { kUptrend, kDowntrend, kRange, kExtreme };
enum class RegimeBucket { kTrend, kRange, kExtreme };

enum class RiskMode { kNormal, kDegraded, kCooldown, kFuse, kReduceOnly };
enum class OrderPurpose { kEntry, kTp, kSl, kReduce };
enum class FillLiquidity { kUnknown, kMaker, kTaker };

// ============================================================================
// Data Structures
// ============================================================================

/// Market Data Event
struct MarketEvent {
  Timestamp ts_ms{0};
  std::string symbol{"BTCUSDT"};
  Price price{0.0};
  Price mark_price{0.0};
  Quantity volume{0.0};
};

/// Regime Analysis Snapshot
struct RegimeState {
  std::string symbol{"BTCUSDT"};
  Regime regime{Regime::kRange};
  RegimeBucket bucket{RegimeBucket::kRange};
  double instant_return{0.0};
  double trend_strength{0.0};
  double volatility_level{0.0};
  bool warmup{true};
};

/// Strategy Signal (Raw Alpha)
struct Signal {
  std::string symbol{"BTCUSDT"};
  Money suggested_notional_usd{0.0};
  Money trend_notional_usd{0.0};
  Money defensive_notional_usd{0.0};
  int direction{0}; // Kept as int for JSON compatibility (-1, 0, 1)
  double confidence{0.0}; // 0.0 to 1.0
};

/// Integrator / ML Model Inference
struct ShadowInference {
  bool enabled{false};
  std::string model_version{"n/a"};
  double model_score{0.0};
  double p_up{0.5};
  double p_down{0.5};
};

/// Risk Engine Input
struct TargetPosition {
  std::string symbol{"BTCUSDT"};
  Money target_notional_usd{0.0};
};

/// Risk Engine Output
struct RiskAdjustedPosition {
  std::string symbol{"BTCUSDT"};
  Money adjusted_notional_usd{0.0};
  bool reduce_only{false};
  RiskMode risk_mode{RiskMode::kNormal};
};

/// Execution Intent
struct OrderIntent {
  std::string client_order_id;
  std::string parent_order_id;
  std::string symbol{"BTCUSDT"};
  OrderPurpose purpose{OrderPurpose::kEntry};
  bool reduce_only{false};
  int direction{0};
  Quantity qty{0.0};
  Price price{0.0};
};

/// Execution Fill Report
struct FillEvent {
  std::string fill_id;
  std::string client_order_id;
  std::string symbol{"BTCUSDT"};
  int direction{0};
  Quantity qty{0.0};
  Price price{0.0};
  Money fee{0.0};
  FillLiquidity liquidity{FillLiquidity::kUnknown};
};

/// Exchange account mode snapshot used for startup/runtime guard checks.
struct ExchangeAccountSnapshot {
  AccountMode account_mode{AccountMode::kUnified};
  MarginMode margin_mode{MarginMode::kIsolated};
  PositionMode position_mode{PositionMode::kOneWay};
};

/// Remote State Snapshots
struct RemotePositionSnapshot {
  std::string symbol{"BTCUSDT"};
  Quantity qty{0.0};
  Price avg_entry_price{0.0};
  Price mark_price{0.0};
  Price liquidation_price{0.0};
};

struct RemoteAccountBalanceSnapshot {
  Money equity_usd{0.0};
  Money wallet_balance_usd{0.0};
  Money unrealized_pnl_usd{0.0};
  bool has_equity{false};
  bool has_wallet_balance{false};
  bool has_unrealized_pnl{false};
};

/// Symbol-level trading rule snapshot used by pre-trade checks.
struct SymbolInfo {
  std::string symbol{"BTCUSDT"};
  bool tradable{false};
  Quantity qty_step{0.0};
  Quantity min_order_qty{0.0};
  Money min_notional_usd{0.0};
  Price price_tick{0.0};
  int qty_precision{8};
  int price_precision{8};
};

struct EvolutionWeights {
  double trend_weight{0.0};
  double defensive_weight{0.0};
};

struct RiskThresholds {
  double degraded_drawdown{0.08};
  double cooldown_drawdown{0.12};
  double fuse_drawdown{0.20};
  double min_liquidation_distance{0.08};
};

// ============================================================================
// String Converters
// ============================================================================

inline const char* ToString(AccountMode m) {
  if (m == AccountMode::kUnified) return "UNIFIED";
  return "CLASSIC";
}
inline const char* ToString(MarginMode m) {
  if (m == MarginMode::kIsolated) return "ISOLATED";
  if (m == MarginMode::kCross) return "CROSS";
  return "PORTFOLIO";
}
inline const char* ToString(PositionMode m) {
  if (m == PositionMode::kHedge) return "HEDGE";
  return "ONE_WAY";
}
inline const char* ToString(Regime r) {
  switch (r) {
    case Regime::kUptrend: return "UPTREND";
    case Regime::kDowntrend: return "DOWNTREND";
    case Regime::kRange: return "RANGE";
    case Regime::kExtreme: return "EXTREME";
  }
  return "UNKNOWN";
}
inline const char* ToString(RegimeBucket b) {
  switch (b) {
    case RegimeBucket::kTrend: return "TREND";
    case RegimeBucket::kRange: return "RANGE";
    case RegimeBucket::kExtreme: return "EXTREME";
  }
  return "UNKNOWN";
}
inline const char* ToString(FillLiquidity l) {
  if (l == FillLiquidity::kMaker) return "MAKER";
  if (l == FillLiquidity::kTaker) return "TAKER";
  return "UNKNOWN";
}

}  // namespace ai_trade
