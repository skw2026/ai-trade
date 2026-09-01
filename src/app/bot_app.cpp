#include "app/bot_app.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <unordered_set>
#include <utility>
#include <vector>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#elif defined(__linux__)
#include <unistd.h>
#endif

#include "core/log.h"
#include "app/intent_policy.h"
#include "exchange/binance_exchange_adapter.h"
#include "exchange/bybit_exchange_adapter.h"
#include "exchange/mock_exchange_adapter.h"

namespace ai_trade {

namespace {

constexpr double kNotionalEpsilon = 1e-9;
// 成交落地后，给远端持仓快照一个极短收敛窗口，避免瞬时对账误判。
constexpr int kReconcileRecentFillGraceTicks = 2;
// 若 symbol 级 delta 能被最近成交精确解释，则再给一段短暂宽限，避免刚撤出 pending 状态就被误重对齐。
constexpr int kReconcileFillLagExplainGraceTicks = 6;
// 自动远端重对齐最小间隔，避免短时间重复覆盖本地状态。
constexpr int kReconcileAutoResyncCooldownTicks = 40;
constexpr std::size_t kRecentFillObservationLimit = 32;
// funding 观测缺失时，保留最近有效值的最长 tick（默认约 1 小时@5s）。
constexpr int kFundingObservationStaleTicks = 720;
constexpr double kFillQtyAuditEpsilon = 1e-9;
constexpr double kFillOverrunToleranceMinQty = 1e-6;

double EffectiveQualityGuardMaxFeeBps(const AppConfig& config) {
  const double configured =
      std::max(0.0, config.execution_quality_guard_max_fee_bps_per_fill);
  const double fee_floor =
      std::max(std::max(0.0, config.execution_entry_fee_bps),
               std::max(0.0, config.execution_exit_fee_bps)) *
      1.10;
  return std::max(configured, fee_floor);
}

// 统一毫秒时间戳，供节流、日志和心跳逻辑复用。
std::int64_t CurrentTimestampMs() {
  const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now());
  return now.time_since_epoch().count();
}

std::string CurrentUtcIsoTimestamp() {
  const auto now = std::chrono::system_clock::now();
  const std::time_t t = std::chrono::system_clock::to_time_t(now);
  std::tm tm{};
#if defined(_WIN32)
  gmtime_s(&tm, &t);
#else
  gmtime_r(&t, &tm);
#endif
  std::ostringstream oss;
  oss << std::put_time(&tm, "%FT%TZ");
  return oss.str();
}

std::string BuildBootId() {
  return "boot-" + std::to_string(CurrentTimestampMs());
}

std::string ReadEnvValue(const char* key) {
  if (key == nullptr) {
    return {};
  }
  const char* value = std::getenv(key);
  return value == nullptr ? std::string() : std::string(value);
}

bool IsSha256Hex(const std::string& value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](unsigned char ch) {
           return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f');
         });
}

std::string Fnv1a64Hex(const std::string& value) {
  std::uint64_t hash = 14695981039346656037ULL;
  for (const unsigned char ch : value) {
    hash ^= static_cast<std::uint64_t>(ch);
    hash *= 1099511628211ULL;
  }
  std::ostringstream oss;
  oss << std::hex << std::setfill('0') << std::setw(16) << hash;
  return oss.str();
}

std::optional<std::string> CurrentExecutablePath() {
#if defined(__APPLE__)
  std::uint32_t size = 0;
  (void)_NSGetExecutablePath(nullptr, &size);
  if (size == 0) return std::nullopt;
  std::vector<char> path(size + 1, '\0');
  if (_NSGetExecutablePath(path.data(), &size) != 0) return std::nullopt;
  std::error_code error;
  const auto canonical = std::filesystem::weakly_canonical(path.data(), error);
  return error ? std::optional<std::string>(path.data())
               : std::optional<std::string>(canonical.string());
#elif defined(__linux__)
  std::vector<char> path(4096, '\0');
  const ssize_t count =
      readlink("/proc/self/exe", path.data(), path.size() - 1);
  if (count <= 0) return std::nullopt;
  path[static_cast<std::size_t>(count)] = '\0';
  return std::string(path.data());
#else
  return std::nullopt;
#endif
}

std::optional<std::string> Fnv1a64File(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input.is_open()) return std::nullopt;
  std::uint64_t hash = 14695981039346656037ULL;
  char buffer[64 * 1024];
  while (input.good()) {
    input.read(buffer, sizeof(buffer));
    const std::streamsize count = input.gcount();
    for (std::streamsize index = 0; index < count; ++index) {
      hash ^= static_cast<unsigned char>(buffer[index]);
      hash *= 1099511628211ULL;
    }
  }
  if (!input.eof()) return std::nullopt;
  std::ostringstream oss;
  oss << std::hex << std::setfill('0') << std::setw(16) << hash;
  return oss.str();
}

std::vector<std::string> SplitTabFields(const std::string& line) {
  std::vector<std::string> fields;
  std::size_t start = 0;
  while (start <= line.size()) {
    const std::size_t end = line.find('\t', start);
    fields.push_back(line.substr(
        start, end == std::string::npos ? std::string::npos : end - start));
    if (end == std::string::npos) break;
    start = end + 1;
  }
  return fields;
}

int SignOf(double value) {
  if (value > kNotionalEpsilon) {
    return 1;
  }
  if (value < -kNotionalEpsilon) {
    return -1;
  }
  return 0;
}

std::string BuildProtectionGroupId(const std::string& symbol) {
  static std::uint64_t seq = 0;
  return symbol + "-protect-" + std::to_string(CurrentTimestampMs()) + "-" +
         std::to_string(seq++);
}

// 去重并保序：确保 symbol 列表可直接用于配置下发与日志展示。
std::vector<std::string> UniqueSymbols(const std::vector<std::string>& symbols) {
  std::vector<std::string> out;
  std::unordered_set<std::string> seen;
  for (const auto& symbol : symbols) {
    if (!symbol.empty() && seen.insert(symbol).second) {
      out.push_back(symbol);
    }
  }
  return out;
}

bool HasExposure(double notional_usd) {
  return std::fabs(notional_usd) > kNotionalEpsilon;
}

double ClampNonNegative(double value) {
  if (!std::isfinite(value)) {
    return 0.0;
  }
  return std::max(0.0, value);
}

double SafeRatio(double numerator, double denominator) {
  if (!std::isfinite(numerator) || !std::isfinite(denominator) ||
      std::fabs(denominator) <= kNotionalEpsilon) {
    return 0.0;
  }
  return numerator / denominator;
}

double StepAwareExecutableNotionalFloor(const ExchangeAdapter* adapter,
                                        const std::string& symbol,
                                        double price,
                                        double min_notional_usd) {
  const double base_floor = std::max(0.0, min_notional_usd);
  if (base_floor <= kNotionalEpsilon || adapter == nullptr || symbol.empty() ||
      !std::isfinite(price) || price <= kNotionalEpsilon) {
    return base_floor;
  }

  SymbolInfo info;
  if (!adapter->GetSymbolInfo(symbol, &info) || !info.tradable ||
      info.qty_step <= kNotionalEpsilon) {
    return base_floor;
  }

  const double min_qty =
      std::max(base_floor / price, std::max(0.0, info.min_order_qty));
  const double steps =
      std::ceil(std::max(0.0, min_qty - 1e-12) / info.qty_step);
  if (!std::isfinite(steps) || steps <= 0.0) {
    return base_floor;
  }
  return std::max(base_floor, steps * info.qty_step * price);
}

bool HasReasonCode(const Signal& signal, const char* code) {
  if (code == nullptr || *code == '\0') {
    return false;
  }
  return std::find(signal.reason_codes.begin(),
                   signal.reason_codes.end(),
                   std::string(code)) != signal.reason_codes.end();
}

bool IsPolicySuppressedFlatSignal(const Signal& signal) {
  if (HasExposure(signal.suggested_notional_usd) ||
      HasExposure(signal.trend_notional_usd) ||
      HasExposure(signal.defensive_notional_usd)) {
    return false;
  }
  if (HasReasonCode(signal, "STR_RANGE_CONFIDENCE_BLOCK") ||
      HasReasonCode(signal, "STR_EXTREME_BLOCK")) {
    return true;
  }
  return HasReasonCode(signal, "STR_FLAT_SIGNAL") &&
         (HasReasonCode(signal, "REG_RANGE") ||
          HasReasonCode(signal, "REG_EXTREME"));
}

int TrendCandidateProbeDirection(const RegimeState& regime) {
  const int trend_direction = SignOf(regime.trend_strength);
  if (trend_direction != 0) {
    return trend_direction;
  }
  return SignOf(regime.instant_return);
}

// 将策略分支名义值按“可执行目标名义值”缩放，减少学习输入与执行结果的偏离。
std::pair<double, double> ScaleStrategyComponentsForExecution(
    const MarketDecision& decision) {
  const double base_blended_notional = decision.base_signal.suggested_notional_usd;
  const double executable_notional = decision.risk_adjusted.adjusted_notional_usd;
  if (!std::isfinite(base_blended_notional) || !std::isfinite(executable_notional) ||
      !HasExposure(base_blended_notional)) {
    return {0.0, 0.0};
  }
  constexpr double kEvolutionSignalScaleLimit = 4.0;
  const double scale = std::clamp(executable_notional / base_blended_notional,
                                  -kEvolutionSignalScaleLimit,
                                  kEvolutionSignalScaleLimit);
  return {decision.base_signal.trend_notional_usd * scale,
          decision.base_signal.defensive_notional_usd * scale};
}

bool IsNetPositionOrderPurpose(OrderPurpose purpose) {
  return purpose == OrderPurpose::kEntry || purpose == OrderPurpose::kReduce;
}

const char* OrderPurposeToString(OrderPurpose purpose) {
  switch (purpose) {
    case OrderPurpose::kEntry:
      return "entry";
    case OrderPurpose::kTp:
      return "take_profit";
    case OrderPurpose::kSl:
      return "stop_loss";
    case OrderPurpose::kReduce:
      return "strategy_reduce";
  }
  return "unknown";
}

double FavorableReturn(int direction, double entry_price, double price) {
  if (direction == 0 || entry_price <= 0.0 || price <= 0.0 ||
      !std::isfinite(entry_price) || !std::isfinite(price)) {
    return 0.0;
  }
  if (direction > 0) {
    return price / entry_price - 1.0;
  }
  return entry_price / price - 1.0;
}

bool IsTighterStopPrice(int direction,
                        double candidate_stop,
                        double active_stop,
                        double min_update_abs) {
  if (direction == 0 || candidate_stop <= 0.0 || !std::isfinite(candidate_stop)) {
    return false;
  }
  if (active_stop <= 0.0 || !std::isfinite(active_stop)) {
    return true;
  }
  if (direction > 0) {
    return candidate_stop > active_stop + std::max(0.0, min_update_abs);
  }
  return candidate_stop < active_stop - std::max(0.0, min_update_abs);
}

bool StopWouldTriggerNow(int entry_direction,
                         double stop_price,
                         double current_price) {
  if (entry_direction == 0 || stop_price <= 0.0 || current_price <= 0.0 ||
      !std::isfinite(stop_price) || !std::isfinite(current_price)) {
    return false;
  }
  return entry_direction > 0 ? current_price <= stop_price
                             : current_price >= stop_price;
}

const char* RiskModeToString(RiskMode mode) {
  switch (mode) {
    case RiskMode::kNormal:
      return "normal";
    case RiskMode::kDegraded:
      return "degraded";
    case RiskMode::kCooldown:
      return "cooldown";
    case RiskMode::kFuse:
      return "fuse";
    case RiskMode::kReduceOnly:
      return "reduce_only";
  }
  return "unknown";
}

const char* EvolutionActionTypeToString(SelfEvolutionActionType type) {
  switch (type) {
    case SelfEvolutionActionType::kUpdated:
      return "updated";
    case SelfEvolutionActionType::kRolledBack:
      return "rolled_back";
    case SelfEvolutionActionType::kSkipped:
      return "skipped";
  }
  return "unknown";
}

const char* OrderStateToString(OrderState state) {
  switch (state) {
    case OrderState::kNew:
      return "new";
    case OrderState::kSent:
      return "sent";
    case OrderState::kPartial:
      return "partial";
    case OrderState::kCancelPending:
      return "cancel_pending";
    case OrderState::kCancelConfirmed:
      return "cancel_confirmed";
    case OrderState::kFilled:
      return "filled";
    case OrderState::kRejected:
      return "rejected";
    case OrderState::kCancelled:
      return "cancelled";
  }
  return "unknown";
}

double FillOverrunToleranceQty(const OrderRecord& record) {
  return std::max(kFillOverrunToleranceMinQty, std::fabs(record.intent.qty) * 1e-6);
}

bool AccountAlreadyReflectsFill(const FillEvent& fill,
                                const OrderRecord* order_record,
                                double local_qty_before,
                                double oms_net_qty_before) {
  if (order_record == nullptr || !std::isfinite(fill.qty) ||
      fill.qty <= kFillQtyAuditEpsilon) {
    return false;
  }
  const double signed_qty = static_cast<double>(fill.direction) * fill.qty;
  const double tolerance_qty =
      std::max(kFillOverrunToleranceMinQty, std::fabs(fill.qty) * 1e-6);
  if (!std::isfinite(signed_qty) || std::fabs(signed_qty) <= tolerance_qty) {
    return false;
  }

  // REST position snapshots and WS fills are independently delivered. If the
  // OMS is behind the account in the same direction as this known fill and
  // applying the fill to the OMS closes that gap, the account side has already
  // incorporated it. This also handles a REST snapshot that contains several
  // fills which subsequently arrive one by one over WS.
  const double account_oms_gap = local_qty_before - oms_net_qty_before;
  if (!std::isfinite(account_oms_gap) ||
      SignOf(account_oms_gap) != SignOf(signed_qty)) {
    return false;
  }
  const double gap_before = std::fabs(account_oms_gap);
  const double gap_after = std::fabs(local_qty_before -
                                     (oms_net_qty_before + signed_qty));
  return gap_before + tolerance_qty >= std::fabs(signed_qty) &&
         gap_after + tolerance_qty < gap_before;
}

double EstimateFillRealizedPnlUsd(double position_qty_before,
                                  double avg_entry_price_before,
                                  const FillEvent& fill) {
  if (!std::isfinite(position_qty_before) ||
      !std::isfinite(avg_entry_price_before) ||
      !std::isfinite(fill.price) || !std::isfinite(fill.qty) ||
      fill.price <= kNotionalEpsilon || fill.qty <= kNotionalEpsilon) {
    return 0.0;
  }
  const double signed_qty = static_cast<double>(fill.direction) * fill.qty;
  if (std::fabs(position_qty_before) <= kNotionalEpsilon ||
      std::fabs(signed_qty) <= kNotionalEpsilon ||
      SignOf(position_qty_before) == SignOf(signed_qty)) {
    return 0.0;
  }
  const double close_qty = std::min(std::fabs(position_qty_before),
                                    std::fabs(signed_qty));
  return close_qty * (fill.price - avg_entry_price_before) *
         SignOf(position_qty_before);
}

std::string FormatFillSummary(const FillEvent& fill) {
  std::ostringstream oss;
  oss << "fill_id=" << fill.fill_id << ", client_order_id=" << fill.client_order_id
      << ", symbol=" << fill.symbol << ", direction=" << fill.direction
      << ", qty=" << fill.qty << ", price=" << fill.price << ", fee=" << fill.fee
      << ", liquidity=" << ToString(fill.liquidity);
  return oss.str();
}

std::string FormatAccountPositions(const AccountState& account) {
  const auto symbols = account.GetActiveSymbols();
  if (symbols.empty()) {
    return "flat";
  }
  std::ostringstream oss;
  bool first = true;
  for (const auto& symbol : symbols) {
    const double qty = account.position_qty(symbol);
    if (std::fabs(qty) <= kNotionalEpsilon) {
      continue;
    }
    if (!first) {
      oss << ";";
    }
    first = false;
    const double mark = account.mark_price(symbol);
    const double notional = account.current_notional_usd(symbol);
    oss << symbol << ":qty=" << qty << ",mark=" << mark
        << ",notional=" << notional;
  }
  if (first) {
    return "flat";
  }
  return oss.str();
}

std::string FormatRemoteBalanceSummary(const RemoteAccountBalanceSnapshot& balance) {
  std::ostringstream oss;
  oss << "remote={equity=";
  if (balance.has_equity) {
    oss << balance.equity_usd;
  } else {
    oss << "n/a";
  }
  oss << ", wallet=";
  if (balance.has_wallet_balance) {
    oss << balance.wallet_balance_usd;
  } else {
    oss << "n/a";
  }
  oss << ", unrealized=";
  if (balance.has_unrealized_pnl) {
    oss << balance.unrealized_pnl_usd;
  } else {
    oss << "n/a";
  }
  oss << "}";
  return oss.str();
}

std::string FormatLocalAccountLedgerSummary(const AccountState& account) {
  std::ostringstream oss;
  oss << "local={cash=" << account.cash_usd()
      << ", equity=" << account.equity_usd()
      << ", unrealized=" << account.unrealized_pnl_usd()
      << ", realized_pnl=" << account.cumulative_realized_pnl_usd()
      << ", fees=" << account.cumulative_fee_usd()
      << ", funding_paid=" << account.cumulative_funding_paid_usd()
      << ", realized_net=" << account.cumulative_realized_net_pnl_usd()
      << ", positions=" << FormatAccountPositions(account) << "}";
  return oss.str();
}

void LogAccountSyncSnapshot(const std::string& stage,
                            const RemoteAccountBalanceSnapshot& balance,
                            const AccountState& account) {
  LogInfo("ACCOUNT_SYNC_SNAPSHOT: stage=" + stage + ", " +
          FormatRemoteBalanceSummary(balance) + ", " +
          FormatLocalAccountLedgerSummary(account));
}

struct SymbolQtyDelta {
  std::string symbol;
  double local_qty{0.0};
  double remote_qty{0.0};
  double delta_qty{0.0};
  double delta_notional_usd{0.0};
};

std::vector<SymbolQtyDelta> CollectSymbolQtyDeltas(
    const AccountState& account,
    const std::vector<RemotePositionSnapshot>& remote_positions,
    const std::vector<std::string>& tracked_symbols,
    double min_abs_notional_delta_usd) {
  std::unordered_map<std::string, RemotePositionSnapshot> remote_by_symbol;
  for (const auto& position : remote_positions) {
    if (!position.symbol.empty()) {
      remote_by_symbol[position.symbol] = position;
    }
  }

  std::unordered_set<std::string> symbols;
  for (const auto& symbol : tracked_symbols) {
    if (!symbol.empty()) {
      symbols.insert(symbol);
    }
  }
  for (const auto& symbol : account.GetActiveSymbols()) {
    if (!symbol.empty()) {
      symbols.insert(symbol);
    }
  }
  for (const auto& [symbol, _] : remote_by_symbol) {
    if (!symbol.empty()) {
      symbols.insert(symbol);
    }
  }

  std::vector<SymbolQtyDelta> deltas;
  deltas.reserve(symbols.size());
  for (const auto& symbol : symbols) {
    const double local_qty = account.position_qty(symbol);
    const double local_mark = account.mark_price(symbol);
    const double local_notional = account.current_notional_usd(symbol);

    double remote_qty = 0.0;
    double remote_mark = local_mark;
    if (const auto it = remote_by_symbol.find(symbol); it != remote_by_symbol.end()) {
      remote_qty = it->second.qty;
      remote_mark = it->second.mark_price > 0.0 ? it->second.mark_price
                                                : it->second.avg_entry_price;
      if (remote_mark <= 0.0) {
        remote_mark = local_mark;
      }
    }
    const double remote_notional = remote_qty * remote_mark;
    const double delta_qty = local_qty - remote_qty;
    const double delta_notional = local_notional - remote_notional;
    if (std::fabs(delta_qty) <= kNotionalEpsilon &&
        std::fabs(delta_notional) < min_abs_notional_delta_usd) {
      continue;
    }
    deltas.push_back(SymbolQtyDelta{
        .symbol = symbol,
        .local_qty = local_qty,
        .remote_qty = remote_qty,
        .delta_qty = delta_qty,
        .delta_notional_usd = delta_notional,
    });
  }

  std::sort(deltas.begin(), deltas.end(), [](const SymbolQtyDelta& lhs,
                                             const SymbolQtyDelta& rhs) {
    return lhs.symbol < rhs.symbol;
  });
  return deltas;
}

std::string FormatSymbolList(const std::vector<std::string>& symbols) {
  if (symbols.empty()) {
    return "n/a";
  }
  std::ostringstream oss;
  for (std::size_t i = 0; i < symbols.size(); ++i) {
    if (i > 0) {
      oss << ",";
    }
    oss << symbols[i];
  }
  return oss.str();
}

std::string FormatSymbolScores(const std::vector<SymbolScore>& scores,
                               std::size_t max_items) {
  if (scores.empty()) {
    return "n/a";
  }
  std::ostringstream oss;
  oss << std::fixed << std::setprecision(4);
  const std::size_t limit = std::min(max_items, scores.size());
  for (std::size_t i = 0; i < limit; ++i) {
    if (i > 0) {
      oss << ",";
    }
    oss << scores[i].symbol << ":" << scores[i].score;
  }
  if (limit < scores.size()) {
    oss << ",...";
  }
  return oss.str();
}

struct ConcentrationSnapshot {
  double gross_notional_usd{0.0};
  double top1_abs_notional_usd{0.0};
  std::string top1_symbol{"n/a"};
  double top1_share{0.0};
  std::size_t symbol_count{0};
};

ConcentrationSnapshot BuildConcentrationSnapshot(
    const AccountState& account,
    const std::string* override_symbol = nullptr,
    double override_symbol_notional_usd = 0.0) {
  ConcentrationSnapshot snapshot;
  const bool has_override =
      override_symbol != nullptr && !override_symbol->empty();
  bool override_seen = false;
  const auto symbols = account.GetActiveSymbols();
  for (const auto& symbol : symbols) {
    double symbol_notional = account.current_notional_usd(symbol);
    if (has_override && symbol == *override_symbol) {
      symbol_notional = override_symbol_notional_usd;
      override_seen = true;
    }
    const double symbol_abs_notional = std::fabs(symbol_notional);
    if (symbol_abs_notional <= kNotionalEpsilon) {
      continue;
    }
    ++snapshot.symbol_count;
    snapshot.gross_notional_usd += symbol_abs_notional;
    if (symbol_abs_notional > snapshot.top1_abs_notional_usd) {
      snapshot.top1_abs_notional_usd = symbol_abs_notional;
      snapshot.top1_symbol = symbol;
    }
  }
  if (has_override && !override_seen &&
      std::fabs(override_symbol_notional_usd) > kNotionalEpsilon) {
    const double symbol_abs_notional = std::fabs(override_symbol_notional_usd);
    ++snapshot.symbol_count;
    snapshot.gross_notional_usd += symbol_abs_notional;
    if (symbol_abs_notional > snapshot.top1_abs_notional_usd) {
      snapshot.top1_abs_notional_usd = symbol_abs_notional;
      snapshot.top1_symbol = *override_symbol;
    }
  }
  if (snapshot.gross_notional_usd > kNotionalEpsilon) {
    snapshot.top1_share =
        snapshot.top1_abs_notional_usd / snapshot.gross_notional_usd;
  }
  return snapshot;
}

/**
 * @brief 交易所规则前置守卫（量化后数量 + 最小数量 + 最小名义金额）
 *
 * 目的：
 * - 在下单入队前按交易规则先做数量量化，提前拦截 qty=0 与 min qty/min notional 违规；
 * - 尽量与交易所规则保持一致，减少“提交后才被交易所拒绝”的噪声与重试抖动。
 *
 * 返回值：
 * - true: 命中守卫（应拦截）
 * - false: 可继续下单
 *
 * 副作用：
 * - 若量化后数量有效，会将 intent->qty 下修为量化后的可提交数量。
 */
bool ViolatesExchangePretradeGuard(const ExchangeAdapter* adapter,
                                   OrderIntent* intent,
                                   const MarketEvent& event,
                                   std::string* out_reason) {
  constexpr double kQtyEpsilon = 1e-12;
  if (adapter == nullptr || intent == nullptr) {
    return false;
  }
  if (!std::isfinite(intent->qty) || intent->qty <= 0.0) {
    if (out_reason != nullptr) {
      *out_reason = "invalid_qty(qty=" + std::to_string(intent->qty) + ")";
    }
    return true;
  }

  SymbolInfo info;
  if (!adapter->GetSymbolInfo(intent->symbol, &info)) {
    return false;
  }
  if (!info.tradable) {
    return false;
  }

  double submit_qty = intent->qty;
  if (info.qty_step > 0.0) {
    const double steps = std::floor((submit_qty + kQtyEpsilon) / info.qty_step);
    submit_qty = steps > 0.0 ? steps * info.qty_step : 0.0;
  }
  if (!std::isfinite(submit_qty) || submit_qty <= kQtyEpsilon) {
    if (out_reason != nullptr) {
      *out_reason = "qty_guard_quantized_zero(raw_qty=" +
                    std::to_string(intent->qty) + ", qty_step=" +
                    std::to_string(info.qty_step) + ")";
    }
    return true;
  }

  if (!intent->reduce_only && info.min_order_qty > 0.0 &&
      submit_qty + kQtyEpsilon < info.min_order_qty) {
    if (out_reason != nullptr) {
      *out_reason = "min_qty_guard(order_qty=" + std::to_string(submit_qty) +
                    ", min_order_qty=" + std::to_string(info.min_order_qty) + ")";
    }
    return true;
  }

  if (!intent->reduce_only && info.min_notional_usd > 0.0) {
    const double ref_price =
        intent->price > 0.0
            ? intent->price
            : (event.mark_price > 0.0 ? event.mark_price : event.price);
    if (ref_price > 0.0) {
      const double order_notional = submit_qty * ref_price;
      if (order_notional + 1e-9 < info.min_notional_usd) {
        if (out_reason != nullptr) {
          *out_reason = "min_notional_guard(order_notional=" +
                        std::to_string(order_notional) +
                        ", min_notional=" + std::to_string(info.min_notional_usd) +
                        ")";
        }
        return true;
      }
    }
  }

  // 量化后回写，确保后续执行链路使用与交易所一致的可提交数量。
  intent->qty = submit_qty;
  return false;
}

/**
 * @brief 根据配置创建交易所适配器实例
 *
 * 约束：
 * 1. Universe 模式下将候选池与 fallback 合并后传入适配器；
 * 2. Bybit 模式注入完整账户模式预期，供启动门禁校验；
 * 3. 未识别交易所时退化到 Mock，保证本地可运行。
 */
std::unique_ptr<ExchangeAdapter> CreateAdapter(const AppConfig& config) {
  auto collect_symbols = [&config]() {
    std::vector<std::string> symbols;
    if (config.universe.enabled) {
      symbols = config.universe.candidate_symbols;
      symbols.insert(symbols.end(), config.universe.fallback_symbols.begin(),
                     config.universe.fallback_symbols.end());
    }
    symbols.push_back(config.primary_symbol);
    return UniqueSymbols(symbols);
  };

  if (config.exchange == "bybit") {
    BybitAdapterOptions options;
    options.testnet = config.bybit.testnet;
    options.demo_trading = config.bybit.demo_trading;
    options.mode = config.mode;
    options.category = config.bybit.category;
    options.account_type = config.bybit.account_type;
    options.primary_symbol = config.primary_symbol;
    options.public_ws_enabled = config.bybit.public_ws_enabled;
    options.public_ws_rest_fallback = config.bybit.public_ws_rest_fallback;
    options.private_ws_enabled = config.bybit.private_ws_enabled;
    options.private_ws_rest_fallback = config.bybit.private_ws_rest_fallback;
    options.ws_reconnect_interval_ms = config.bybit.ws_reconnect_interval_ms;
    options.execution_poll_limit = config.bybit.execution_poll_limit;
    options.maker_entry_enabled = config.execution_maker_entry_enabled;
    options.maker_fallback_to_market = config.execution_maker_fallback_to_market;
    options.maker_price_offset_bps = config.execution_maker_price_offset_bps;
    options.maker_post_only = config.execution_maker_post_only;
    options.replay_market_data_path = config.bybit.replay_market_data_path;
    options.replay_timestamp_column = config.bybit.replay_timestamp_column;
    options.replay_symbol_column = config.bybit.replay_symbol_column;
    options.replay_price_column = config.bybit.replay_price_column;
    options.replay_volume_column = config.bybit.replay_volume_column;
    options.replay_interval_column = config.bybit.replay_interval_column;
    options.replay_funding_rate_column = config.bybit.replay_funding_rate_column;
    options.replay_default_interval_ms = config.bybit.replay_default_interval_ms;
    options.replay_entry_fee_bps = config.execution_entry_fee_bps;
    options.replay_exit_fee_bps = config.execution_exit_fee_bps;
    options.replay_expected_slippage_bps = config.execution_expected_slippage_bps;
    options.symbols = collect_symbols();
    options.remote_account_mode = config.bybit.expected_account_mode;
    options.remote_margin_mode = config.bybit.expected_margin_mode;
    options.remote_position_mode = config.bybit.expected_position_mode;
    return std::make_unique<BybitExchangeAdapter>(options);
  }

  if (config.exchange == "binance") {
    return std::make_unique<BinanceExchangeAdapter>();
  }

  // Mock Adapter 默认行为
  std::vector<double> prices = {100.0, 100.5, 100.7, 100.2, 99.8, 100.1};
  return std::make_unique<MockExchangeAdapter>(prices, collect_symbols());
}

/**
 * @brief 启动前账户模式校验（Bybit 专用）
 *
 * 只有当账户模式、保证金模式、持仓模式全部匹配时才允许启动，
 * 防止策略在错误账户形态下误下单。
 */
bool ValidateAccountSnapshot(const AppConfig& config, ExchangeAdapter* adapter) {
  if (adapter == nullptr) return false;
  if (config.exchange != "bybit") return true;

  ExchangeAccountSnapshot snapshot;
  if (!adapter->GetAccountSnapshot(&snapshot)) {
    LogInfo("账户快照读取失败：Bybit 模式下拒绝启动以确保安全");
    return false;
  }

  const bool ok = (snapshot.account_mode == config.bybit.expected_account_mode &&
                   snapshot.margin_mode == config.bybit.expected_margin_mode &&
                   snapshot.position_mode == config.bybit.expected_position_mode);
  
  if (!ok) {
    LogInfo("账户模式校验失败: 请检查 Unified/Isolated/OneWay 设置");
  }
  return ok;
}

}  // namespace

BotApplication::BotApplication(const AppConfig& config)
    : config_(config),
      startup_utc_(CurrentUtcIsoTimestamp()),
      boot_id_(BuildBootId()),
      system_(config),
      execution_(config.GetExecutionEngineConfig()),
      order_throttle_({
          .min_order_interval_ms = config.execution_min_order_interval_ms,
          .reverse_signal_cooldown_ticks =
              config.execution_reverse_signal_cooldown_ticks,
      }),
      self_evolution_(config.self_evolution),
      reconciler_(config.reconcile.tolerance_notional_usd),
      gate_monitor_(config.gate),
      universe_selector_(config.universe, config.primary_symbol),
      wal_(config.data_path + "/trade.wal") {}

std::string BotApplication::SelfEvolutionPolicyFingerprint() const {
  std::string payload;
  if (!config_.source_config_path.empty()) {
    std::ifstream input(config_.source_config_path, std::ios::binary);
    if (input.is_open()) {
      std::ostringstream buffer;
      buffer << input.rdbuf();
      payload = buffer.str();
    }
  }
  if (payload.empty()) {
    std::ostringstream fallback;
    fallback << std::setprecision(17)
             << config_.self_evolution.enabled << '|'
             << config_.self_evolution.update_interval_ticks << '|'
             << config_.self_evolution.min_update_interval_ticks << '|'
             << config_.self_evolution.max_single_strategy_weight << '|'
             << config_.self_evolution.max_weight_step << '|'
             << config_.self_evolution.initial_trend_weight << '|'
             << config_.self_evolution.initial_defensive_weight << '|'
             << config_.self_evolution.use_counterfactual_search << '|'
             << config_.self_evolution.counterfactual_require_temporal_holdout
             << '|' << config_.self_evolution.enable_learnability_gate;
    payload = fallback.str();
  }
  static const std::optional<std::string> executable_hash = [] {
    const auto executable_path = CurrentExecutablePath();
    return executable_path.has_value() ? Fnv1a64File(*executable_path)
                                       : std::optional<std::string>{};
  }();
  payload += "|executable_fnv1a64=" +
             executable_hash.value_or("unavailable");
  return Fnv1a64Hex(payload);
}

bool BotApplication::LoadSelfEvolutionWeights(
    std::array<EvolutionWeights, 3>* out_weights,
    bool* out_state_exists,
    std::string* out_error) const {
  if (out_weights == nullptr || out_state_exists == nullptr) {
    if (out_error != nullptr) *out_error = "自进化恢复输出参数为空";
    return false;
  }
  *out_state_exists = false;
  const std::filesystem::path path =
      std::filesystem::path(config_.data_path) /
      "self_evolution_weights_v1.tsv";
  if (!std::filesystem::exists(path)) {
    return true;
  }
  std::ifstream input(path, std::ios::binary);
  if (!input.is_open()) {
    if (out_error != nullptr) *out_error = "无法读取自进化权重状态";
    return false;
  }
  std::vector<std::string> lines;
  std::string line;
  while (std::getline(input, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    lines.push_back(line);
  }
  if (lines.size() != 6 || lines[0] != "AI_TRADE_SELF_EVOLUTION_STATE_V1") {
    if (out_error != nullptr) *out_error = "自进化权重状态 schema/行数非法";
    return false;
  }
  const auto policy_fields = SplitTabFields(lines[1]);
  const auto checksum_fields = SplitTabFields(lines[5]);
  if (policy_fields.size() != 2 || policy_fields[0] != "policy_fingerprint" ||
      checksum_fields.size() != 2 || checksum_fields[0] != "checksum_fnv1a64") {
    if (out_error != nullptr) *out_error = "自进化权重状态头部非法";
    return false;
  }
  std::string checksum_payload;
  for (std::size_t index = 0; index < 5; ++index) {
    checksum_payload += lines[index] + "\n";
  }
  if (checksum_fields[1] != Fnv1a64Hex(checksum_payload)) {
    if (out_error != nullptr) *out_error = "自进化权重状态 checksum 不匹配";
    return false;
  }
  if (policy_fields[1] != SelfEvolutionPolicyFingerprint()) {
    if (out_error != nullptr) *out_error = "policy_fingerprint_mismatch";
    return true;
  }

  constexpr std::array<const char*, 3> kBucketNames{
      "TREND", "RANGE", "EXTREME"};
  for (std::size_t index = 0; index < kBucketNames.size(); ++index) {
    const auto fields = SplitTabFields(lines[index + 2]);
    if (fields.size() != 3 || fields[0] != kBucketNames[index]) {
      if (out_error != nullptr) *out_error = "自进化权重分桶状态非法";
      return false;
    }
    try {
      std::size_t trend_consumed = 0;
      std::size_t defensive_consumed = 0;
      const double trend = std::stod(fields[1], &trend_consumed);
      const double defensive = std::stod(fields[2], &defensive_consumed);
      if (trend_consumed != fields[1].size() ||
          defensive_consumed != fields[2].size() || !std::isfinite(trend) ||
          !std::isfinite(defensive)) {
        throw std::invalid_argument("non-finite or trailing characters");
      }
      (*out_weights)[index] = EvolutionWeights{trend, defensive};
    } catch (const std::exception&) {
      if (out_error != nullptr) *out_error = "自进化权重数值非法";
      return false;
    }
  }
  *out_state_exists = true;
  return true;
}

bool BotApplication::PersistSelfEvolutionWeights(std::string* out_error) const {
  if (config_.mode == "replay" || !config_.self_evolution.enabled) return true;
  const std::filesystem::path directory(config_.data_path);
  std::error_code error_code;
  std::filesystem::create_directories(directory, error_code);
  if (error_code) {
    if (out_error != nullptr) {
      *out_error = "创建自进化状态目录失败: " + error_code.message();
    }
    return false;
  }
  const std::filesystem::path path =
      directory / "self_evolution_weights_v1.tsv";
  const std::filesystem::path temp =
      directory / ("self_evolution_weights_v1.tsv.tmp." + boot_id_);
  const auto weights = system_.evolution_weights_all();
  constexpr std::array<const char*, 3> kBucketNames{
      "TREND", "RANGE", "EXTREME"};
  std::ostringstream payload;
  payload << "AI_TRADE_SELF_EVOLUTION_STATE_V1\n"
          << "policy_fingerprint\t" << SelfEvolutionPolicyFingerprint() << "\n"
          << std::setprecision(17);
  for (std::size_t index = 0; index < weights.size(); ++index) {
    payload << kBucketNames[index] << '\t' << weights[index].trend_weight << '\t'
            << weights[index].defensive_weight << "\n";
  }
  const std::string payload_text = payload.str();
  std::ofstream output(temp, std::ios::binary | std::ios::trunc);
  if (!output.is_open()) {
    if (out_error != nullptr) *out_error = "创建自进化临时状态失败";
    return false;
  }
  output << payload_text << "checksum_fnv1a64\t" << Fnv1a64Hex(payload_text)
         << "\n";
  output.flush();
  if (!output.good()) {
    output.close();
    std::filesystem::remove(temp, error_code);
    if (out_error != nullptr) *out_error = "写入自进化临时状态失败";
    return false;
  }
  output.close();
  std::filesystem::rename(temp, path, error_code);
  if (error_code) {
    const std::string rename_error = error_code.message();
    std::error_code cleanup_error;
    std::filesystem::remove(temp, cleanup_error);
    if (out_error != nullptr) {
      *out_error = "提交自进化状态失败: " + rename_error;
    }
    return false;
  }
  return true;
}

double BotApplication::RoundTripCostBps() const {
  const double entry_fee_bps = std::max(0.0, config_.execution_entry_fee_bps);
  const double exit_fee_bps = std::max(0.0, config_.execution_exit_fee_bps);
  const double slippage_bps =
      std::max(0.0, config_.execution_expected_slippage_bps);
  return entry_fee_bps + exit_fee_bps + 2.0 * slippage_bps;
}

double BotApplication::EstimateEntryEdgeBps(const MarketDecision& decision,
                                            const MarketEvent& event) const {
  if (!decision.intent.has_value() || !IsOpeningIntent(*decision.intent)) {
    return 0.0;
  }
  const int direction = decision.intent->direction;
  if (direction == 0) {
    return 0.0;
  }
  if (decision.integrator_policy_reason == "canary_independent_signal" ||
      decision.integrator_policy_reason == "microstructure_demo_target" ||
      decision.integrator_policy_reason ==
          "microstructure_demo_target_pending") {
    if (!decision.shadow.expected_net_edge_available ||
        !std::isfinite(
            decision.shadow.expected_net_edge_per_trade_bps)) {
      return 0.0;
    }
    // The model report metric is already net of observed OOS turnover cost.
    // Convert it back to a gross-equivalent edge so the common fee gate can
    // compare it against the configured round-trip cost and safety margin.
    return ClampNonNegative(
        RoundTripCostBps() +
        decision.shadow.expected_net_edge_per_trade_bps);
  }
  const double price = event.price > 0.0 ? event.price : decision.intent->price;
  double deadband_bps = 0.0;
  if (config_.strategy_signal_deadband_bps > 0.0) {
    deadband_bps = std::fabs(config_.strategy_signal_deadband_bps);
  } else if (price > 0.0 && config_.strategy_signal_deadband_abs > 0.0) {
    deadband_bps =
        std::fabs(config_.strategy_signal_deadband_abs) / price * 10000.0;
  }

  const double trend_edge_bps = std::max(
      0.0, decision.regime.trend_strength * static_cast<double>(direction) * 10000.0);
  const double instant_edge_bps = std::max(
      0.0, decision.regime.instant_return * static_cast<double>(direction) * 10000.0);
  const double raw_regime_edge_bps = 0.6 * trend_edge_bps + 0.4 * instant_edge_bps;
  const double raw_trend_regime_edge_bps = 0.7 * trend_edge_bps + 0.3 * instant_edge_bps;
  const double raw_range_regime_edge_bps = 0.35 * trend_edge_bps + 0.65 * instant_edge_bps;

  // 用信号置信度与分支净合成比降噪，避免“分支互相对冲 + 低置信”样本被高估。
  const double confidence_scale =
      std::clamp(std::fabs(decision.signal.confidence), 0.0, 1.0);
  const double trend_abs = std::fabs(decision.base_signal.trend_notional_usd);
  const double defensive_abs = std::fabs(decision.base_signal.defensive_notional_usd);
  const double branch_abs_sum = trend_abs + defensive_abs;
  double net_blend_scale = 1.0;
  if (branch_abs_sum > kNotionalEpsilon) {
    net_blend_scale = std::clamp(
        std::fabs(decision.signal.suggested_notional_usd) / branch_abs_sum, 0.0,
        1.0);
  }
  const double trend_aligned_abs =
      SignOf(decision.base_signal.trend_notional_usd) == direction ? trend_abs : 0.0;
  const double defensive_aligned_abs =
      SignOf(decision.base_signal.defensive_notional_usd) == direction ? defensive_abs
                                                                       : 0.0;
  const double aligned_abs = trend_aligned_abs + defensive_aligned_abs;
  const double aligned_share =
      std::clamp(SafeRatio(aligned_abs, std::max(branch_abs_sum, kNotionalEpsilon)),
                 0.0, 1.0);
  const double executable_abs =
      std::max(std::fabs(decision.risk_adjusted.adjusted_notional_usd),
               std::fabs(decision.signal.suggested_notional_usd));
  const double trend_exec_scale =
      std::clamp(SafeRatio(trend_aligned_abs, std::max(1.0, executable_abs)),
                 0.0, 1.5);
  const double defensive_exec_scale =
      std::clamp(SafeRatio(defensive_aligned_abs, std::max(1.0, executable_abs)),
                 0.0, 1.25);

  const double fallback_regime_edge_bps =
      raw_regime_edge_bps * confidence_scale *
      std::max(net_blend_scale, aligned_share * 0.5);
  double fallback_deadband_scale = std::max(0.25, confidence_scale);
  if (decision.regime.bucket == RegimeBucket::kRange &&
      defensive_aligned_abs > kNotionalEpsilon) {
    fallback_deadband_scale = std::clamp(
        0.12 + 0.22 * confidence_scale + 0.08 * defensive_exec_scale, 0.12,
        0.42);
  } else if (decision.regime.bucket == RegimeBucket::kTrend &&
             trend_aligned_abs > kNotionalEpsilon) {
    fallback_deadband_scale = std::clamp(
        0.22 + 0.20 * confidence_scale + 0.10 * trend_exec_scale, 0.22, 0.85);
  }
  const double fallback_deadband_edge_bps =
      deadband_bps * fallback_deadband_scale;
  double expected_edge_bps =
      std::max(fallback_deadband_edge_bps, fallback_regime_edge_bps);

  if (decision.regime.bucket == RegimeBucket::kTrend &&
      trend_aligned_abs > kNotionalEpsilon) {
    const double trend_confidence = std::clamp(
        std::max(confidence_scale, trend_exec_scale) *
            std::max(0.35, aligned_share),
        0.0, 1.35);
    const double trend_dominance = std::clamp(
        SafeRatio(trend_aligned_abs, std::max(aligned_abs, kNotionalEpsilon)),
        0.0, 1.0);
    const double trend_deadband_floor =
        deadband_bps *
        std::clamp(0.25 + 0.45 * trend_dominance, 0.0, 0.75) *
        std::clamp(0.55 + 0.45 * trend_confidence, 0.0, 1.20);
    const double trend_regime_edge =
        raw_trend_regime_edge_bps *
        std::clamp(0.55 + 0.65 * trend_confidence, 0.0, 1.45);
    expected_edge_bps =
        std::max(expected_edge_bps,
                 std::max(trend_deadband_floor, trend_regime_edge));
  } else if (decision.regime.bucket == RegimeBucket::kRange &&
             defensive_aligned_abs > kNotionalEpsilon) {
    const double defensive_confidence = std::clamp(
        std::max(confidence_scale * 0.75, defensive_exec_scale) *
            std::max(0.25, aligned_share),
        0.0, 1.10);
    const double defensive_dominance = std::clamp(
        SafeRatio(defensive_aligned_abs, std::max(aligned_abs, kNotionalEpsilon)),
        0.0, 1.0);
    const double defensive_deadband_floor =
        deadband_bps *
        std::clamp(0.10 + 0.25 * defensive_dominance, 0.0, 0.45) *
        std::clamp(0.40 + 0.35 * defensive_confidence, 0.0, 0.85);
    const double defensive_regime_edge =
        raw_range_regime_edge_bps *
        std::clamp(0.35 + 0.45 * defensive_confidence, 0.0, 0.90);
    expected_edge_bps =
        std::max(expected_edge_bps,
                 std::max(defensive_deadband_floor, defensive_regime_edge));
  }

  return ClampNonNegative(expected_edge_bps);
}

bool BotApplication::ShouldFilterByFeeAwareGate(
    const MarketDecision& decision,
    const MarketEvent& event,
    double* out_expected_edge_bps,
    double* out_required_edge_bps,
    double* out_base_required_edge_bps,
    double* out_adaptive_relax_bps,
    double* out_maker_relax_bps,
    double* out_regime_adjust_bps,
    double* out_volatility_adjust_bps,
    double* out_liquidity_adjust_bps,
    double* out_concentration_adjust_bps,
    double* out_quality_guard_penalty_bps,
    double* out_observed_filtered_ratio,
    double* out_edge_gap_bps,
    bool* out_near_miss,
    bool* out_near_miss_allowed) const {
  if (out_expected_edge_bps != nullptr) {
    *out_expected_edge_bps = 0.0;
  }
  if (out_required_edge_bps != nullptr) {
    *out_required_edge_bps = 0.0;
  }
  if (out_base_required_edge_bps != nullptr) {
    *out_base_required_edge_bps = 0.0;
  }
  if (out_adaptive_relax_bps != nullptr) {
    *out_adaptive_relax_bps = 0.0;
  }
  if (out_maker_relax_bps != nullptr) {
    *out_maker_relax_bps = 0.0;
  }
  if (out_regime_adjust_bps != nullptr) {
    *out_regime_adjust_bps = 0.0;
  }
  if (out_volatility_adjust_bps != nullptr) {
    *out_volatility_adjust_bps = 0.0;
  }
  if (out_liquidity_adjust_bps != nullptr) {
    *out_liquidity_adjust_bps = 0.0;
  }
  if (out_concentration_adjust_bps != nullptr) {
    *out_concentration_adjust_bps = 0.0;
  }
  if (out_quality_guard_penalty_bps != nullptr) {
    *out_quality_guard_penalty_bps = 0.0;
  }
  if (out_observed_filtered_ratio != nullptr) {
    *out_observed_filtered_ratio = entry_gate_observed_filtered_ratio_;
  }
  if (out_edge_gap_bps != nullptr) {
    *out_edge_gap_bps = 0.0;
  }
  if (out_near_miss != nullptr) {
    *out_near_miss = false;
  }
  if (out_near_miss_allowed != nullptr) {
    *out_near_miss_allowed = false;
  }
  if (!decision.intent.has_value() || !IsOpeningIntent(*decision.intent)) {
    return false;
  }

  const double expected_edge_bps = EstimateEntryEdgeBps(decision, event);
  const double round_trip_cost_bps = RoundTripCostBps();
  const double base_required_edge_bps =
      round_trip_cost_bps + std::max(0.0, config_.execution_min_expected_edge_bps);
  const double cost_floor_required_edge_bps = base_required_edge_bps;
  double required_edge_bps = base_required_edge_bps;
  double adaptive_relax_bps = 0.0;
  const bool symbol_quality_guard_active =
      IsSymbolExecutionQualityGuardActive(decision.intent->symbol);
  const bool symbol_quality_memory =
      HasSymbolExecutionQualityMemory(decision.intent->symbol);
  const bool quality_guard_active =
      execution_quality_guard_active_ || symbol_quality_guard_active;
  if (config_.execution_adaptive_fee_gate_enabled &&
      static_cast<int>(entry_gate_observed_samples_) >=
          config_.execution_adaptive_fee_gate_min_samples) {
    const double trigger_ratio =
        std::clamp(config_.execution_adaptive_fee_gate_trigger_ratio, 0.0, 1.0);
    if (entry_gate_observed_filtered_ratio_ > trigger_ratio &&
        trigger_ratio < 1.0) {
      const double scale =
          (entry_gate_observed_filtered_ratio_ - trigger_ratio) /
          (1.0 - trigger_ratio);
      adaptive_relax_bps =
          std::clamp(scale, 0.0, 1.0) *
          std::max(0.0, config_.execution_adaptive_fee_gate_max_relax_bps);
    }
  }
  double relax_regime_scale = 0.65;
  if (decision.regime.bucket == RegimeBucket::kTrend) {
    relax_regime_scale = 1.0;
  } else if (decision.regime.bucket == RegimeBucket::kRange) {
    relax_regime_scale = 0.35;
  } else if (decision.regime.bucket == RegimeBucket::kExtreme) {
    relax_regime_scale = 0.15;
  }
  adaptive_relax_bps *= relax_regime_scale;
  // 执行质量守卫激活时，关闭“自适应放宽”，避免在低质量阶段继续放行弱边际样本。
  if (quality_guard_active) {
    adaptive_relax_bps = 0.0;
  }
  double maker_relax_scale = 0.7;
  if (decision.regime.bucket == RegimeBucket::kTrend) {
    maker_relax_scale = 1.0;
  } else if (decision.regime.bucket == RegimeBucket::kRange) {
    maker_relax_scale = 0.45;
  } else if (decision.regime.bucket == RegimeBucket::kExtreme) {
    maker_relax_scale = 0.0;
  }
  const bool maker_entry_candidate =
      config_.execution_maker_entry_enabled &&
      decision.intent->purpose == OrderPurpose::kEntry && !decision.intent->reduce_only;
  const double maker_relax_bps =
      maker_entry_candidate && !quality_guard_active
          ? std::max(0.0, config_.execution_maker_edge_relax_bps) *
                maker_relax_scale
          : 0.0;
  double regime_adjust_bps = 0.0;
  double volatility_adjust_bps = 0.0;
  double liquidity_adjust_bps = 0.0;
  double concentration_adjust_bps = 0.0;
  if (config_.execution_dynamic_edge_enabled) {
    if (decision.regime.bucket == RegimeBucket::kTrend) {
      regime_adjust_bps =
          -std::max(0.0, config_.execution_dynamic_edge_regime_trend_relax_bps);
    } else if (decision.regime.bucket == RegimeBucket::kRange) {
      regime_adjust_bps =
          std::max(0.0, config_.execution_dynamic_edge_regime_range_penalty_bps);
    } else if (decision.regime.bucket == RegimeBucket::kExtreme) {
      regime_adjust_bps =
          std::max(0.0, config_.execution_dynamic_edge_regime_extreme_penalty_bps);
    }

    const double vol_threshold = std::max(1e-9, config_.regime.volatility_threshold);
    const double vol_ratio =
        std::clamp(decision.regime.volatility_level / vol_threshold, 0.0, 2.0);
    if (vol_ratio > 1.0) {
      volatility_adjust_bps = std::max(
          0.0, config_.execution_dynamic_edge_volatility_penalty_bps) *
          (vol_ratio - 1.0);
    } else if (vol_ratio < 1.0 &&
               decision.regime.bucket == RegimeBucket::kTrend) {
      volatility_adjust_bps = -std::max(
          0.0, config_.execution_dynamic_edge_volatility_relax_bps) *
          (1.0 - vol_ratio);
    }

    if (recent_execution_window_maker_fill_ratio_ >=
        config_.execution_dynamic_edge_liquidity_maker_ratio_threshold) {
      const double maker_den =
          std::max(1e-9,
                   1.0 - config_.execution_dynamic_edge_liquidity_maker_ratio_threshold);
      const double maker_scale = std::clamp(
          (recent_execution_window_maker_fill_ratio_ -
           config_.execution_dynamic_edge_liquidity_maker_ratio_threshold) /
              maker_den,
          0.0, 1.0);
      liquidity_adjust_bps -=
          maker_scale *
          std::max(0.0, config_.execution_dynamic_edge_liquidity_relax_bps);
    } else if (recent_execution_window_liquidity_fill_count_ > 0 &&
               config_.execution_dynamic_edge_liquidity_maker_ratio_threshold >
                   kNotionalEpsilon) {
      // 在已有成交样本但 maker 占比持续偏低时，提高 required_edge，抑制低质量成交。
      const double maker_deficit_scale = std::clamp(
          (config_.execution_dynamic_edge_liquidity_maker_ratio_threshold -
           recent_execution_window_maker_fill_ratio_) /
              config_.execution_dynamic_edge_liquidity_maker_ratio_threshold,
          0.0, 1.0);
      liquidity_adjust_bps +=
          maker_deficit_scale *
          std::max(0.0, config_.execution_dynamic_edge_liquidity_penalty_bps);
    }
    if (recent_execution_window_unknown_fill_ratio_ >=
        config_.execution_dynamic_edge_liquidity_unknown_ratio_threshold) {
      const double unknown_den = std::max(
          1e-9, 1.0 - config_.execution_dynamic_edge_liquidity_unknown_ratio_threshold);
      const double unknown_scale = std::clamp(
          (recent_execution_window_unknown_fill_ratio_ -
           config_.execution_dynamic_edge_liquidity_unknown_ratio_threshold) /
              unknown_den,
          0.0, 1.0);
      liquidity_adjust_bps +=
          unknown_scale *
          std::max(0.0, config_.execution_dynamic_edge_liquidity_penalty_bps);
    }
  }
  if (config_.execution_concentration_penalty_bps > 0.0 &&
      config_.execution_concentration_top1_share_threshold < 1.0) {
    const ConcentrationSnapshot concentration =
        BuildConcentrationSnapshot(system_.account());
    const double current_symbol_notional =
        system_.account().current_notional_usd(decision.intent->symbol);
    double projected_symbol_notional = current_symbol_notional;
    const double reference_price =
        decision.intent->price > 0.0
            ? decision.intent->price
            : (event.mark_price > 0.0 ? event.mark_price : event.price);
    if (reference_price > 0.0 && decision.intent->qty > 0.0) {
      projected_symbol_notional += static_cast<double>(decision.intent->direction) *
                                   decision.intent->qty * reference_price;
    }
    const bool concentration_widening =
        std::fabs(projected_symbol_notional) >
        std::fabs(current_symbol_notional) + kNotionalEpsilon;
    const ConcentrationSnapshot concentration_projected =
        concentration_widening
            ? BuildConcentrationSnapshot(system_.account(),
                                         &decision.intent->symbol,
                                         projected_symbol_notional)
            : concentration;
    const ConcentrationSnapshot& concentration_for_penalty =
        concentration_widening ? concentration_projected : concentration;
    const int min_symbols = std::max(1, config_.execution_concentration_min_symbols);
    if (static_cast<int>(concentration_for_penalty.symbol_count) >= min_symbols &&
        concentration_for_penalty.top1_symbol == decision.intent->symbol &&
        concentration_for_penalty.top1_share >
            config_.execution_concentration_top1_share_threshold) {
      const double den = std::max(
          1e-9, 1.0 - config_.execution_concentration_top1_share_threshold);
      const double linear_scale =
          std::clamp((concentration_for_penalty.top1_share -
                      config_.execution_concentration_top1_share_threshold) /
                         den,
                     0.0, 1.0);
      // 用凸性放大高集中区间惩罚，避免 top1 占比长期高位却惩罚过弱。
      const double boosted_scale =
          std::clamp(linear_scale * linear_scale + 0.35 * linear_scale, 0.0, 1.0);
      concentration_adjust_bps =
          boosted_scale * std::max(0.0, config_.execution_concentration_penalty_bps);
    }
  }
  const double quality_guard_penalty_bps = std::max(
      std::max(0.0, execution_quality_required_edge_penalty_bps_),
      SymbolExecutionQualityPenaltyBps(decision.intent->symbol));
  const double quality_guard_floor_bps =
      quality_guard_active
          ? std::max(0.0, config_.execution_quality_guard_required_edge_floor_bps)
          : 0.0;
  required_edge_bps = std::max(
      0.0,
      required_edge_bps - adaptive_relax_bps - maker_relax_bps +
          regime_adjust_bps + volatility_adjust_bps + liquidity_adjust_bps +
          concentration_adjust_bps +
          quality_guard_penalty_bps);
  const double effective_required_edge_floor_bps =
      std::max(cost_floor_required_edge_bps, quality_guard_floor_bps);
  required_edge_bps = std::max(required_edge_bps, effective_required_edge_floor_bps);
  if (config_.execution_required_edge_cap_bps > effective_required_edge_floor_bps) {
    required_edge_bps =
        std::min(required_edge_bps, config_.execution_required_edge_cap_bps);
  }
  if (out_expected_edge_bps != nullptr) {
    *out_expected_edge_bps = expected_edge_bps;
  }
  if (out_required_edge_bps != nullptr) {
    *out_required_edge_bps = required_edge_bps;
  }
  if (out_base_required_edge_bps != nullptr) {
    *out_base_required_edge_bps = base_required_edge_bps;
  }
  if (out_adaptive_relax_bps != nullptr) {
    *out_adaptive_relax_bps = adaptive_relax_bps;
  }
  if (out_maker_relax_bps != nullptr) {
    *out_maker_relax_bps = maker_relax_bps;
  }
  if (out_regime_adjust_bps != nullptr) {
    *out_regime_adjust_bps = regime_adjust_bps;
  }
  if (out_volatility_adjust_bps != nullptr) {
    *out_volatility_adjust_bps = volatility_adjust_bps;
  }
  if (out_liquidity_adjust_bps != nullptr) {
    *out_liquidity_adjust_bps = liquidity_adjust_bps;
  }
  if (out_concentration_adjust_bps != nullptr) {
    *out_concentration_adjust_bps = concentration_adjust_bps;
  }
  if (out_quality_guard_penalty_bps != nullptr) {
    *out_quality_guard_penalty_bps = quality_guard_penalty_bps;
  }
  if (!config_.execution_enable_fee_aware_entry_gate) {
    return false;
  }
  const double edge_gap_bps = required_edge_bps - expected_edge_bps;
  const double near_miss_tolerance_bps =
      std::max(0.0, config_.execution_entry_gate_near_miss_tolerance_bps);
  bool filtered = edge_gap_bps > near_miss_tolerance_bps + 1e-9;
  // 近阈值定义：落在“容差+附加带+自适应放宽”内的样本，用于观测与可选 maker 放行。
  const double near_miss_band_bps =
      near_miss_tolerance_bps + std::max(0.05, near_miss_tolerance_bps) +
      std::max(0.0, adaptive_relax_bps);
  const bool near_miss = filtered && edge_gap_bps <= near_miss_band_bps;
  bool near_miss_allowed = false;
  const bool near_miss_maker_regime_allowed =
      decision.regime.bucket == RegimeBucket::kTrend;
  if (near_miss && config_.execution_entry_gate_near_miss_maker_allow &&
      near_miss_maker_regime_allowed && maker_entry_candidate &&
      !config_.execution_maker_fallback_to_market && !symbol_quality_memory) {
    const bool has_liquidity_window =
        recent_execution_window_liquidity_fill_count_ >= 6;
    const double maker_quality_threshold = std::clamp(
        std::max(0.45,
                 config_.execution_dynamic_edge_liquidity_maker_ratio_threshold),
        0.0, 1.0);
    const bool maker_quality_ok =
        !has_liquidity_window ||
        recent_execution_window_maker_fill_ratio_ + 1e-9 >=
            maker_quality_threshold;
    const double unknown_quality_threshold = std::clamp(
        config_.execution_dynamic_edge_liquidity_unknown_ratio_threshold + 0.10,
        0.10, 1.0);
    const bool unknown_quality_ok =
        !has_liquidity_window ||
        recent_execution_window_unknown_fill_ratio_ <=
            unknown_quality_threshold + 1e-9;
    const double allow_extra_gap_bps =
        std::max(0.0, config_.execution_entry_gate_near_miss_maker_max_gap_bps);
    // 质量守卫开启且尚未进入恢复窗口时，禁止 near-miss 放行，避免在低质量阶段继续加仓磨损。
    // 一旦守卫期出现连续 good_streak，再允许“更严格版本”的 near-miss 放行用于恢复流量。
    const double effective_allow_extra_gap_bps =
        quality_guard_active
            ? (execution_quality_good_streak_ > 0
                   ? std::min(allow_extra_gap_bps,
                              std::max(0.05, quality_guard_penalty_bps * 0.5))
                   : 0.0)
            : allow_extra_gap_bps;
    const double allow_upper_gap_bps =
        near_miss_tolerance_bps + effective_allow_extra_gap_bps +
        std::max(0.0, adaptive_relax_bps);
    // 语义：maker_allow 配置是“在 tolerance 之上的附加 gap”。
    if (effective_allow_extra_gap_bps > 0.0 && maker_quality_ok &&
        unknown_quality_ok &&
        edge_gap_bps <= allow_upper_gap_bps + 1e-9) {
      filtered = false;
      near_miss_allowed = true;
    }
  }
  if (out_edge_gap_bps != nullptr) {
    *out_edge_gap_bps = edge_gap_bps;
  }
  if (out_near_miss != nullptr) {
    *out_near_miss = near_miss;
  }
  if (out_near_miss_allowed != nullptr) {
    *out_near_miss_allowed = near_miss_allowed;
  }
  return filtered;
}

bool BotApplication::ShouldAllowCandidateProbeFeeOverride(
    const MarketDecision& decision,
    double expected_edge_bps,
    double entry_edge_gap_bps,
    double quality_guard_penalty_bps,
    bool has_quality_memory,
    bool* out_memory_recovery_allowed,
    bool* out_diagnostic_canary_allowed) const {
  if (out_memory_recovery_allowed != nullptr) {
    *out_memory_recovery_allowed = false;
  }
  if (out_diagnostic_canary_allowed != nullptr) {
    *out_diagnostic_canary_allowed = false;
  }

  const double max_edge_gap_bps =
      std::max(0.0, config_.execution_candidate_probe_max_edge_gap_bps);
  const bool quality_guard_override_blocked =
      quality_guard_penalty_bps > 1e-9 || has_quality_memory;
  const bool normal_probe_allowed =
      !quality_guard_override_blocked &&
      entry_edge_gap_bps <= max_edge_gap_bps + 1e-9;

  const double memory_max_edge_gap_bps = std::max(
      0.0, config_.execution_candidate_probe_memory_max_edge_gap_bps);
  const double memory_min_trend_ratio = std::max(
      0.0, config_.execution_candidate_probe_memory_min_trend_ratio);
  const bool memory_recovery_allowed =
      has_quality_memory && memory_max_edge_gap_bps > 0.0 &&
      memory_min_trend_ratio > 0.0 &&
      decision.regime.trend_threshold_ratio + 1e-9 >=
          memory_min_trend_ratio &&
      entry_edge_gap_bps <= memory_max_edge_gap_bps + 1e-9;
  if (out_memory_recovery_allowed != nullptr) {
    *out_memory_recovery_allowed = memory_recovery_allowed;
  }

  const double diagnostic_min_trend_ratio = std::max(
      0.0, config_.execution_candidate_probe_diagnostic_min_trend_ratio);
  const double diagnostic_max_edge_gap_bps = std::max(
      0.0, config_.execution_candidate_probe_diagnostic_max_edge_gap_bps);
  const double diagnostic_min_expected_edge_bps = std::max(
      0.0, config_.execution_candidate_probe_diagnostic_min_expected_edge_bps);
  const bool diagnostic_canary_allowed =
      config_.execution_candidate_probe_diagnostic_canary_enabled &&
      !normal_probe_allowed &&
      !memory_recovery_allowed &&
      !quality_guard_override_blocked &&
      diagnostic_max_edge_gap_bps > 0.0 &&
      expected_edge_bps + 1e-9 >= diagnostic_min_expected_edge_bps &&
      decision.regime.trend_threshold_ratio + 1e-9 >=
          diagnostic_min_trend_ratio &&
      entry_edge_gap_bps <= diagnostic_max_edge_gap_bps + 1e-9;
  if (out_diagnostic_canary_allowed != nullptr) {
    *out_diagnostic_canary_allowed = diagnostic_canary_allowed;
  }

  return normal_probe_allowed || memory_recovery_allowed ||
         diagnostic_canary_allowed;
}

bool BotApplication::IsCostFilterCooldownActive(const std::string& symbol,
                                                int* out_remaining_ticks) {
  if (out_remaining_ticks != nullptr) {
    *out_remaining_ticks = 0;
  }
  if (symbol.empty()) {
    return false;
  }
  const auto it = cost_filter_cooldown_until_tick_by_symbol_.find(symbol);
  if (it == cost_filter_cooldown_until_tick_by_symbol_.end()) {
    return false;
  }
  if (market_tick_count_ >= it->second) {
    cost_filter_cooldown_until_tick_by_symbol_.erase(it);
    LogInfo("ORDER_COST_FILTER_COOLDOWN_EXIT: symbol=" + symbol +
            ", tick=" + std::to_string(market_tick_count_));
    return false;
  }
  const int remaining_ticks = it->second - market_tick_count_;
  if (out_remaining_ticks != nullptr) {
    *out_remaining_ticks = std::max(0, remaining_ticks);
  }
  return true;
}

void BotApplication::OnCostFilterRejected(const std::string& symbol) {
  if (symbol.empty()) {
    return;
  }
  auto& reject_streak = cost_filter_reject_streak_by_symbol_[symbol];
  ++reject_streak;
  const int trigger_count =
      std::max(0, config_.execution_cost_filter_cooldown_trigger_count);
  const int cooldown_ticks = std::max(0, config_.execution_cost_filter_cooldown_ticks);
  if (trigger_count <= 0 || cooldown_ticks <= 0 || reject_streak < trigger_count) {
    return;
  }
  cost_filter_cooldown_until_tick_by_symbol_[symbol] =
      market_tick_count_ + cooldown_ticks;
  reject_streak = 0;
  LogInfo("ORDER_COST_FILTER_COOLDOWN_ENTER: symbol=" + symbol +
          ", cooldown_ticks=" + std::to_string(cooldown_ticks) +
          ", until_tick=" + std::to_string(market_tick_count_ + cooldown_ticks));
}

void BotApplication::OnCostFilterAccepted(const std::string& symbol) {
  if (symbol.empty()) {
    return;
  }
  cost_filter_reject_streak_by_symbol_.erase(symbol);
}

void BotApplication::UpdateEntryGateObservedRatio(bool filtered,
                                                  bool near_miss,
                                                  bool near_miss_allowed) {
  ++entry_gate_observed_samples_;
  if (filtered) {
    ++entry_gate_observed_filtered_;
  }
  if (near_miss) {
    ++entry_gate_observed_near_miss_;
  }
  if (near_miss_allowed) {
    ++entry_gate_observed_near_miss_allowed_;
  }
  if (entry_gate_observed_samples_ > 0) {
    entry_gate_observed_filtered_ratio_ =
        static_cast<double>(entry_gate_observed_filtered_) /
        static_cast<double>(entry_gate_observed_samples_);
    entry_gate_observed_near_miss_ratio_ =
        static_cast<double>(entry_gate_observed_near_miss_) /
        static_cast<double>(entry_gate_observed_samples_);
    entry_gate_observed_near_miss_allowed_ratio_ =
        static_cast<double>(entry_gate_observed_near_miss_allowed_) /
        static_cast<double>(entry_gate_observed_samples_);
  } else {
    entry_gate_observed_filtered_ratio_ = 0.0;
    entry_gate_observed_near_miss_ratio_ = 0.0;
    entry_gate_observed_near_miss_allowed_ratio_ = 0.0;
  }
}

bool BotApplication::TryApplyTrendCandidateProbe(
    MarketDecision* decision,
    const MarketEvent& event,
    bool trade_ok,
    double effective_symbol_notional_usd,
    bool has_pending_symbol_net_orders) {
  if (decision == nullptr || !config_.execution_candidate_probe_enabled) {
    return false;
  }
  if (!decision->regime.trend_candidate) {
    return false;
  }

  const auto skip_probe = [&](const std::string& reason,
                              std::uint64_t* counter,
                              const std::string& extra = "") {
    if (counter != nullptr) {
      ++(*counter);
    }
    LogInfo("TREND_CANDIDATE_PROBE_SKIPPED: symbol=" + event.symbol +
            ", reason=" + reason +
            ", trend_threshold_ratio=" +
            std::to_string(decision->regime.trend_threshold_ratio) +
            ", current_notional_usd=" +
            std::to_string(effective_symbol_notional_usd) +
            ", market_tick=" + std::to_string(market_tick_count_) + extra);
    return false;
  };

  if (!trade_ok) {
    return skip_probe("TRADE_NOT_OK",
                      &funnel_window_.candidate_probe_skipped_trade_not_ok);
  }
  if (decision->intent.has_value()) {
    return skip_probe("EXISTING_INTENT",
                      &funnel_window_.candidate_probe_skipped_existing_intent);
  }
  if (decision->regime.warmup) {
    return false;
  }
  if (active_candidate_probe_by_symbol_.find(event.symbol) !=
      active_candidate_probe_by_symbol_.end()) {
    return skip_probe("ACTIVE_PROBE",
                      &funnel_window_.candidate_probe_skipped_pending_orders);
  }
  if (has_pending_symbol_net_orders) {
    return skip_probe("PENDING_ORDERS",
                      &funnel_window_.candidate_probe_skipped_pending_orders);
  }
  if (HasExposure(effective_symbol_notional_usd) ||
      HasExposure(decision->base_signal.suggested_notional_usd) ||
      HasExposure(decision->base_signal.trend_notional_usd) ||
      HasExposure(decision->base_signal.defensive_notional_usd) ||
      HasExposure(decision->risk_adjusted.adjusted_notional_usd)) {
    return skip_probe("EXPOSURE",
                      &funnel_window_.candidate_probe_skipped_exposure);
  }
  if (decision->regime.trend_threshold_ratio + 1e-9 <
      config_.execution_candidate_probe_min_trend_ratio) {
    return skip_probe(
        "TREND_RATIO_LOW",
        &funnel_window_.candidate_probe_skipped_trend_ratio,
        ", min_trend_ratio=" +
            std::to_string(config_.execution_candidate_probe_min_trend_ratio));
  }
  const double strong_min_trend_ratio =
      std::max(0.0, config_.execution_candidate_probe_strong_min_trend_ratio);
  if (strong_min_trend_ratio > 0.0 &&
      decision->regime.trend_threshold_ratio + 1e-9 <
          strong_min_trend_ratio) {
    return skip_probe(
        "STRONG_TREND_RATIO_LOW",
        &funnel_window_.candidate_probe_skipped_strong_trend_ratio,
        ", strong_min_trend_ratio=" + std::to_string(strong_min_trend_ratio));
  }

  const int quality_guard_remaining_ticks =
      SymbolExecutionQualityActiveRemainingTicks(event.symbol);
  const bool quality_memory_active =
      IsSymbolExecutionQualityGuardActive(event.symbol);
  const double recovery_min_trend_ratio = std::max(
      0.0, config_.execution_candidate_probe_memory_min_trend_ratio);
  const bool recovery_probe_allowed =
      quality_memory_active && recovery_min_trend_ratio > 0.0 &&
      decision->regime.trend_threshold_ratio + 1e-9 >= recovery_min_trend_ratio;
  if (quality_memory_active && !recovery_probe_allowed) {
    return skip_probe(
        "QUALITY_GUARD_MEMORY",
        &funnel_window_.candidate_probe_skipped_cooldown,
        ", quality_guard_remaining_ticks=" +
            std::to_string(quality_guard_remaining_ticks) +
            ", quality_guard_trigger_count=" +
            std::to_string(SymbolExecutionQualityMemoryTriggerCount(event.symbol)));
  }

  const int max_per_window =
      std::max(0, config_.execution_candidate_probe_max_per_window);
  if (max_per_window > 0 &&
      funnel_window_.candidate_probe_enqueued >=
          static_cast<std::uint64_t>(max_per_window)) {
    return skip_probe("WINDOW_LIMIT",
                      &funnel_window_.candidate_probe_skipped_window_limit,
                      ", max_per_window=" + std::to_string(max_per_window) +
                          ", quota_basis=enqueued" +
                          ", window_used=" +
                          std::to_string(funnel_window_.candidate_probe_enqueued) +
                          ", filtered_fee=" +
                          std::to_string(
                              funnel_window_.candidate_probe_filtered_fee));
  }

  const auto cooldown_it =
      candidate_probe_cooldown_until_tick_by_symbol_.find(event.symbol);
  if (cooldown_it != candidate_probe_cooldown_until_tick_by_symbol_.end()) {
    if (market_tick_count_ < cooldown_it->second) {
      return skip_probe(
          "COOLDOWN",
          &funnel_window_.candidate_probe_skipped_cooldown,
          ", cooldown_remaining_ticks=" +
              std::to_string(cooldown_it->second - market_tick_count_));
    }
    candidate_probe_cooldown_until_tick_by_symbol_.erase(cooldown_it);
  }

  const int direction = TrendCandidateProbeDirection(decision->regime);
  if (direction == 0) {
    return skip_probe("DIRECTION_ZERO",
                      &funnel_window_.candidate_probe_skipped_direction);
  }
  const double price = event.price > 0.0 ? event.price : event.mark_price;
  if (!std::isfinite(price) || price <= 0.0) {
    return skip_probe("INVALID_PRICE",
                      &funnel_window_.candidate_probe_skipped_invalid_price);
  }
  const double quality_probe_scale =
      SymbolExecutionQualityProbeNotionalScale(event.symbol);
  const double base_configured_notional =
      std::max(0.0, config_.execution_candidate_probe_notional_usd);
  const double scaled_configured_notional =
      base_configured_notional * quality_probe_scale;
  const double configured_min_executable_notional =
      std::max(0.0, config_.execution_min_rebalance_notional_usd);
  const double min_executable_notional =
      StepAwareExecutableNotionalFloor(adapter_.get(),
                                       event.symbol,
                                       price,
                                       configured_min_executable_notional);
  double configured_notional = scaled_configured_notional;
  const bool probe_base_can_meet_execution_floor =
      base_configured_notional + kNotionalEpsilon >=
      configured_min_executable_notional;
  const bool probe_notional_floor_applied =
      min_executable_notional > kNotionalEpsilon &&
      probe_base_can_meet_execution_floor &&
      configured_notional > kNotionalEpsilon &&
      configured_notional + kNotionalEpsilon < min_executable_notional;
  if (probe_notional_floor_applied) {
    configured_notional = min_executable_notional;
  }
  if (configured_notional <= kNotionalEpsilon) {
    return skip_probe("NOTIONAL_ZERO",
                      &funnel_window_.candidate_probe_skipped_notional);
  }
  if (quality_probe_scale < 0.999) {
    LogInfo("TREND_CANDIDATE_PROBE_DOWNWEIGHT: symbol=" + event.symbol +
            ", trigger_count=" +
            std::to_string(SymbolExecutionQualityMemoryTriggerCount(event.symbol)) +
            ", base_notional_usd=" + std::to_string(base_configured_notional) +
            ", scaled_notional_usd=" +
            std::to_string(scaled_configured_notional) +
            ", executable_notional_usd=" +
            std::to_string(configured_notional) +
            ", execution_min_rebalance_notional_usd=" +
            std::to_string(configured_min_executable_notional) +
            ", step_aware_min_executable_notional_usd=" +
            std::to_string(min_executable_notional) +
            ", floor_applied=" +
            std::string(probe_notional_floor_applied ? "true" : "false") +
            ", scale=" + std::to_string(quality_probe_scale));
  }

  const double settled_gross_notional = system_.account().gross_notional_usd();
  const double settled_symbol_notional =
      system_.account().current_notional_usd(event.symbol);
  const double gross_with_inflight =
      std::max(0.0, settled_gross_notional +
                        std::fabs(effective_symbol_notional_usd) -
                        std::fabs(settled_symbol_notional));
  const double other_symbols_gross =
      std::max(0.0, gross_with_inflight - std::fabs(effective_symbol_notional_usd));
  const double symbol_budget =
      std::max(0.0, config_.risk_max_abs_notional_usd - other_symbols_gross);
  const double capped_notional =
      std::min({configured_notional,
                std::max(0.0, config_.execution_max_order_notional),
                symbol_budget});
  if (capped_notional <= kNotionalEpsilon) {
    return skip_probe("BUDGET_ZERO",
                      &funnel_window_.candidate_probe_skipped_budget,
                      ", symbol_budget=" + std::to_string(symbol_budget));
  }
  if (probe_base_can_meet_execution_floor &&
      capped_notional + kNotionalEpsilon < min_executable_notional) {
    return skip_probe(
        "MIN_REBALANCE_NOTIONAL",
        &funnel_window_.candidate_probe_skipped_notional,
        ", capped_notional_usd=" + std::to_string(capped_notional) +
            ", execution_min_rebalance_notional_usd=" +
            std::to_string(configured_min_executable_notional) +
            ", step_aware_min_executable_notional_usd=" +
            std::to_string(min_executable_notional) +
            ", symbol_budget=" + std::to_string(symbol_budget));
  }

  Signal probe_signal;
  probe_signal.symbol = event.symbol;
  probe_signal.suggested_notional_usd = direction * capped_notional;
  probe_signal.trend_notional_usd = direction * capped_notional;
  probe_signal.defensive_notional_usd = 0.0;
  probe_signal.direction = direction;
  probe_signal.confidence =
      std::clamp(decision->regime.trend_threshold_ratio, 0.0, 1.0);
  probe_signal.valid_until_ms =
      event.ts_ms +
      std::max<std::int64_t>(decision->regime.decision_interval_ms, 0);
  probe_signal.reason_codes = {
      "STR_TREND_CANDIDATE_PROBE",
      "REG_TREND_CANDIDATE",
      "EXEC_MAKER_PROBE",
  };
  if (strong_min_trend_ratio > 0.0) {
    probe_signal.reason_codes.push_back("EXEC_STRONG_TREND_PROBE");
  }

  decision->base_signal = probe_signal;
  decision->signal = probe_signal;
  decision->target = TargetPosition{event.symbol, probe_signal.suggested_notional_usd};
  decision->risk_adjusted = RiskAdjustedPosition{
      .symbol = event.symbol,
      .adjusted_notional_usd = probe_signal.suggested_notional_usd,
      .reduce_only = false,
      .risk_mode = system_.risk_mode(),
  };
  decision->intent =
      execution_.BuildIntent(decision->risk_adjusted,
                             effective_symbol_notional_usd,
                             price);
  if (!decision->intent.has_value() || !IsOpeningIntent(*decision->intent)) {
    decision->intent.reset();
    return skip_probe("BUILD_INTENT_FAILED",
                      &funnel_window_.candidate_probe_skipped_build_intent);
  }

  candidate_probe_intent_ids_.insert(decision->intent->client_order_id);
  ++funnel_window_.candidate_probe_signals;
  if (strong_min_trend_ratio > 0.0) {
    ++funnel_window_.candidate_probe_strong_signals;
  }
  ++funnel_window_.candidate_probe_intents;
  funnel_window_.candidate_probe_notional_abs_usd_sum += capped_notional;
  LogInfo("TREND_CANDIDATE_PROBE_SIGNAL: symbol=" + event.symbol +
          ", client_order_id=" + decision->intent->client_order_id +
          ", direction=" + std::to_string(direction) +
          ", notional_usd=" + std::to_string(capped_notional) +
          ", configured_notional_usd=" +
          std::to_string(base_configured_notional) +
          ", execution_min_rebalance_notional_usd=" +
          std::to_string(configured_min_executable_notional) +
          ", step_aware_min_executable_notional_usd=" +
          std::to_string(min_executable_notional) +
          ", notional_floor_applied=" +
          std::string(probe_notional_floor_applied ? "true" : "false") +
          ", quality_probe_scale=" + std::to_string(quality_probe_scale) +
          ", quality_guard_trigger_count=" +
          std::to_string(SymbolExecutionQualityMemoryTriggerCount(event.symbol)) +
          ", strong_filter=" +
          std::string(strong_min_trend_ratio > 0.0 ? "true" : "false") +
          ", strong_min_trend_ratio=" + std::to_string(strong_min_trend_ratio) +
          ", trend_threshold_ratio=" +
          std::to_string(decision->regime.trend_threshold_ratio) +
          ", instant_return=" + std::to_string(decision->regime.instant_return) +
          ", trend_strength=" + std::to_string(decision->regime.trend_strength) +
          ", current_notional_usd=" +
          std::to_string(effective_symbol_notional_usd));
  return true;
}

bool BotApplication::IsTrendCandidateProbeIntent(
    const std::string& client_order_id) const {
  return !client_order_id.empty() &&
         candidate_probe_intent_ids_.count(client_order_id) > 0;
}

bool BotApplication::NormalizeReduceIntentToActualPosition(
    MarketDecision* decision,
    const MarketEvent& event) {
  (void)event;
  if (decision == nullptr || !decision->intent.has_value()) {
    return true;
  }
  OrderIntent& intent = *decision->intent;
  if (intent.purpose != OrderPurpose::kReduce || !intent.reduce_only) {
    return true;
  }

  const double actual_position_qty = system_.account().position_qty(intent.symbol);
  const int actual_position_direction = SignOf(actual_position_qty);
  if (actual_position_direction == 0 ||
      actual_position_direction == intent.direction) {
    ++funnel_window_.intents_throttled;
    ++funnel_window_.reduce_without_position_blocked;
    LogInfo("ORDER_THROTTLED: symbol=" + intent.symbol +
            ", client_order_id=" + intent.client_order_id +
            ", reason=reduce_without_actual_position" +
            ", actual_position_qty=" + std::to_string(actual_position_qty) +
            ", intent_direction=" + std::to_string(intent.direction) +
            ", adjusted_notional_usd=" +
            std::to_string(decision->risk_adjusted.adjusted_notional_usd));
    decision->intent.reset();
    return false;
  }

  const double max_reduce_qty = std::fabs(actual_position_qty);
  if (intent.qty > max_reduce_qty + kNotionalEpsilon) {
    ++funnel_window_.reduce_qty_capped_to_position;
    LogInfo("REDUCE_QTY_CAPPED_TO_POSITION: symbol=" + intent.symbol +
            ", client_order_id=" + intent.client_order_id +
            ", old_qty=" + std::to_string(intent.qty) +
            ", capped_qty=" + std::to_string(max_reduce_qty) +
            ", actual_position_qty=" + std::to_string(actual_position_qty));
    intent.qty = max_reduce_qty;
  }
  if (intent.qty <= kNotionalEpsilon) {
    ++funnel_window_.intents_throttled;
    ++funnel_window_.reduce_without_position_blocked;
    LogInfo("ORDER_THROTTLED: symbol=" + intent.symbol +
            ", client_order_id=" + intent.client_order_id +
            ", reason=reduce_qty_zero_after_actual_position_cap");
    decision->intent.reset();
    return false;
  }
  return true;
}

void BotApplication::CancelConflictingMicrostructureEntries(
    const MarketDecision& decision) {
  if (executor_ == nullptr ||
      decision.shadow.source != "microstructure_demo" ||
      !decision.shadow.target_position_signal ||
      decision.shadow.model_version.empty()) {
    return;
  }
  const int target_direction =
      decision.shadow.fail_closed ||
              decision.integrator_policy_reason ==
                  "microstructure_demo_route_transition_flat"
          ? 0
          : std::clamp(decision.shadow.target_direction, -1, 1);
  const std::int64_t now_ms = CurrentTimestampMs();
  for (const auto& client_order_id : oms_.PendingNetPositionOrderIds()) {
    const OrderRecord* record = oms_.Find(client_order_id);
    if (record == nullptr || record->state == OrderState::kCancelPending ||
        record->intent.purpose != OrderPurpose::kEntry ||
        record->intent.reduce_only ||
        record->intent.symbol != decision.signal.symbol ||
        (target_direction != 0 &&
         record->intent.candidate_id == decision.shadow.model_version &&
         record->intent.direction == target_direction)) {
      continue;
    }
    const std::string pending_symbol = record->intent.symbol;
    const int pending_direction = record->intent.direction;
    oms_.MarkCancelPending(client_order_id);
    pending_net_order_enqueued_ms_[client_order_id] = now_ms;
    executor_->Cancel(client_order_id);
    LogInfo("MICROSTRUCTURE_DEMO_PENDING_ENTRY_CANCEL: candidate_id=" +
            decision.shadow.model_version +
            ", client_order_id=" + client_order_id +
            ", symbol=" + pending_symbol +
            ", pending_direction=" +
            std::to_string(pending_direction) +
            ", target_direction=" + std::to_string(target_direction) +
            ", reason=" + decision.integrator_policy_reason);
  }
}

void BotApplication::OnCandidateProbeCancelResult(
    const std::string& client_order_id,
    bool success) {
  if (client_order_id.empty()) {
    return;
  }
  for (auto& [symbol, state] : active_candidate_probe_by_symbol_) {
    if (state.client_order_id != client_order_id) {
      continue;
    }
    if (success) {
      ++funnel_window_.candidate_probe_cancel_ok;
      state.cancel_requested = false;
      state.cancel_confirmed = true;
      LogInfo("TREND_CANDIDATE_PROBE_CANCEL_OK: symbol=" + symbol +
              ", client_order_id=" + client_order_id +
              ", replacement_pending=" +
              std::string(state.replacement_pending ? "true" : "false") +
              ", replacement_taker=" +
              std::string(state.replacement_taker ? "true" : "false"));
    } else {
      ++funnel_window_.candidate_probe_cancel_failed;
      state.cancel_requested = false;
      state.cancel_confirmed = false;
      state.replacement_pending = false;
      state.created_tick = market_tick_count_;
      LogInfo("TREND_CANDIDATE_PROBE_CANCEL_FAILED: symbol=" + symbol +
              ", client_order_id=" + client_order_id +
              ", retry_after_tick=" + std::to_string(state.created_tick));
    }
    return;
  }
}

void BotApplication::OnStrategyReduceCancelResult(
    const std::string& client_order_id,
    bool success) {
  if (client_order_id.empty()) {
    return;
  }
  for (auto& [symbol, state] : active_strategy_reduce_by_symbol_) {
    if (state.client_order_id != client_order_id) {
      continue;
    }
    if (success) {
      ++funnel_window_.strategy_reduce_cancel_ok;
      state.cancel_requested = false;
      state.cancel_confirmed = true;
      LogInfo("STRATEGY_REDUCE_CANCEL_OK: symbol=" + symbol +
              ", client_order_id=" + client_order_id +
              ", replacement_pending=" +
              std::string(state.replacement_pending ? "true" : "false") +
              ", replacement_taker=" +
              std::string(state.replacement_taker ? "true" : "false"));
    } else {
      ++funnel_window_.strategy_reduce_cancel_failed;
      state.cancel_requested = false;
      state.cancel_confirmed = false;
      state.replacement_pending = false;
      state.created_tick = market_tick_count_;
      LogInfo("STRATEGY_REDUCE_CANCEL_FAILED: symbol=" + symbol +
              ", client_order_id=" + client_order_id +
              ", retry_after_tick=" + std::to_string(state.created_tick));
    }
    return;
  }
}

void BotApplication::ManageStrategyReduceLifecycle(const MarketEvent& event) {
  const int timeout_ticks =
      std::max(0, config_.execution_strategy_reduce_post_only_timeout_ticks);
  if (timeout_ticks <= 0 || event.symbol.empty()) {
    return;
  }
  auto it = active_strategy_reduce_by_symbol_.find(event.symbol);
  if (it == active_strategy_reduce_by_symbol_.end()) {
    return;
  }

  auto& state = it->second;
  const double actual_position_qty =
      system_.account().position_qty(event.symbol);
  const int actual_direction = SignOf(actual_position_qty);
  if (state.remaining_qty <= kNotionalEpsilon || actual_direction == 0) {
    active_strategy_reduce_by_symbol_.erase(it);
    return;
  }
  if (state.lineage_intent.direction == 0 ||
      actual_direction == state.lineage_intent.direction) {
    ++funnel_window_.strategy_reduce_lifecycle_aborted;
    LogInfo("STRATEGY_REDUCE_LIFECYCLE_ABORTED: symbol=" + event.symbol +
            ", client_order_id=" + state.client_order_id +
            ", reason=position_direction_changed" +
            ", actual_position_qty=" + std::to_string(actual_position_qty));
    active_strategy_reduce_by_symbol_.erase(it);
    return;
  }

  const OrderRecord* record = oms_.Find(state.client_order_id);
  const bool terminal =
      record == nullptr || OrderManager::IsTerminalState(record->state);
  if (terminal && !state.cancel_confirmed) {
    const int max_reprice_attempts =
        std::max(0, config_.execution_strategy_reduce_reprice_max_attempts);
    const bool can_reprice = state.attempts < max_reprice_attempts;
    const bool can_taker_fallback =
        config_.execution_strategy_reduce_taker_fallback_enabled &&
        !state.taker_fallback_used;
    state.cancel_requested = false;
    state.cancel_confirmed = true;
    state.replacement_pending = can_reprice || can_taker_fallback;
    state.replacement_taker = !can_reprice && can_taker_fallback;
    state.next_attempts = can_reprice ? state.attempts + 1 : state.attempts;
  }

  if (state.cancel_confirmed) {
    if (!state.replacement_pending) {
      active_strategy_reduce_by_symbol_.erase(it);
      return;
    }
    if (adapter_ == nullptr || executor_ == nullptr || !adapter_->TradeOk()) {
      return;
    }

    double reference_price = event.price > 0.0 ? event.price : event.mark_price;
    if (!std::isfinite(reference_price) || reference_price <= 0.0) {
      reference_price = state.reference_price;
    }
    if (!std::isfinite(reference_price) || reference_price <= 0.0) {
      ++funnel_window_.strategy_reduce_lifecycle_aborted;
      LogInfo("STRATEGY_REDUCE_LIFECYCLE_ABORTED: symbol=" + event.symbol +
              ", client_order_id=" + state.client_order_id +
              ", reason=invalid_replacement_price");
      active_strategy_reduce_by_symbol_.erase(it);
      return;
    }
    if (!state.replacement_taker && state.next_attempts > 0) {
      const double reprice_ratio =
          std::max(0.0, config_.execution_strategy_reduce_reprice_bps) /
          10000.0;
      const double aggressiveness =
          reprice_ratio * static_cast<double>(state.next_attempts);
      reference_price *= state.lineage_intent.direction > 0
                             ? (1.0 + aggressiveness)
                             : (1.0 - aggressiveness);
    }

    const double replacement_qty =
        std::min(state.remaining_qty, std::fabs(actual_position_qty));
    RiskAdjustedPosition flat_target{
        .symbol = event.symbol,
        .adjusted_notional_usd = 0.0,
        .reduce_only = false,
        .risk_mode = system_.risk_mode(),
    };
    auto replacement = execution_.BuildIntent(
        flat_target, actual_position_qty * reference_price, reference_price);
    if (!replacement.has_value() || replacement_qty <= kNotionalEpsilon) {
      ++funnel_window_.strategy_reduce_lifecycle_aborted;
      LogInfo("STRATEGY_REDUCE_LIFECYCLE_ABORTED: symbol=" + event.symbol +
              ", client_order_id=" + state.client_order_id +
              ", reason=replacement_build_failed");
      active_strategy_reduce_by_symbol_.erase(it);
      return;
    }
    replacement->purpose = OrderPurpose::kReduce;
    replacement->reduce_only = true;
    replacement->liquidity_preference =
        state.replacement_taker ? LiquidityPreference::kTaker
                                : LiquidityPreference::kMaker;
    replacement->direction = state.lineage_intent.direction;
    replacement->qty = replacement_qty;
    replacement->price = reference_price;
    replacement->parent_order_id = state.lineage_intent.parent_order_id;
    replacement->decision_id = state.lineage_intent.decision_id;
    replacement->candidate_id = state.lineage_intent.candidate_id;
    replacement->model_version = state.lineage_intent.model_version;
    replacement->integrator_mode = state.lineage_intent.integrator_mode;
    replacement->position_episode_id =
        state.lineage_intent.position_episode_id;
    replacement->integrator_policy_reason =
        state.lineage_intent.integrator_policy_reason;

    std::string guard_reason;
    if (ViolatesExchangePretradeGuard(adapter_.get(), &*replacement, event,
                                      &guard_reason)) {
      ++funnel_window_.strategy_reduce_lifecycle_aborted;
      LogInfo("STRATEGY_REDUCE_LIFECYCLE_ABORTED: symbol=" + event.symbol +
              ", client_order_id=" + state.client_order_id +
              ", replacement_client_order_id=" +
              replacement->client_order_id + ", reason=" + guard_reason);
      active_strategy_reduce_by_symbol_.erase(it);
      return;
    }

    const std::string replacement_id = replacement->client_order_id;
    const bool replacement_taker = state.replacement_taker;
    const int replacement_attempts = state.next_attempts;
    if (!EnqueueIntent(*replacement)) {
      ++funnel_window_.strategy_reduce_lifecycle_aborted;
      LogInfo("STRATEGY_REDUCE_LIFECYCLE_ABORTED: symbol=" + event.symbol +
              ", client_order_id=" + state.client_order_id +
              ", replacement_client_order_id=" + replacement_id +
              ", reason=replacement_enqueue_failed");
      active_strategy_reduce_by_symbol_.erase(event.symbol);
      return;
    }
    state.lineage_intent = *replacement;
    state.client_order_id = replacement_id;
    state.remaining_qty = replacement_qty;
    state.reference_price = reference_price;
    state.created_tick = market_tick_count_;
    state.attempts = replacement_attempts;
    state.taker_fallback_used =
        state.taker_fallback_used || replacement_taker;
    state.cancel_requested = false;
    state.cancel_confirmed = false;
    state.replacement_pending = false;
    state.replacement_taker = false;
    state.next_attempts = replacement_attempts;
    if (replacement_taker) {
      ++funnel_window_.strategy_reduce_taker_fallbacks;
      LogInfo("STRATEGY_REDUCE_TAKER_FALLBACK: symbol=" + event.symbol +
              ", client_order_id=" + replacement_id +
              ", qty=" + std::to_string(replacement_qty) +
              ", attempts=" + std::to_string(replacement_attempts));
    } else {
      ++funnel_window_.strategy_reduce_reprices;
      LogInfo("STRATEGY_REDUCE_REPRICE: symbol=" + event.symbol +
              ", client_order_id=" + replacement_id +
              ", qty=" + std::to_string(replacement_qty) +
              ", attempts=" + std::to_string(replacement_attempts) +
              ", reprice_bps=" +
              std::to_string(config_.execution_strategy_reduce_reprice_bps));
    }
    return;
  }

  if (state.cancel_requested || terminal ||
      market_tick_count_ - state.created_tick < timeout_ticks) {
    return;
  }

  const int max_reprice_attempts =
      std::max(0, config_.execution_strategy_reduce_reprice_max_attempts);
  const bool can_reprice = state.attempts < max_reprice_attempts;
  const bool can_taker_fallback =
      config_.execution_strategy_reduce_taker_fallback_enabled &&
      !state.taker_fallback_used;
  ++funnel_window_.strategy_reduce_pending_timeouts;
  state.cancel_requested = true;
  state.cancel_confirmed = false;
  state.replacement_pending = can_reprice || can_taker_fallback;
  state.replacement_taker = !can_reprice && can_taker_fallback;
  state.next_attempts = can_reprice ? state.attempts + 1 : state.attempts;
  oms_.MarkCancelPending(state.client_order_id);
  pending_net_order_enqueued_ms_[state.client_order_id] = CurrentTimestampMs();
  executor_->Cancel(state.client_order_id);
  ++funnel_window_.strategy_reduce_cancel_submitted;
  LogInfo("STRATEGY_REDUCE_PENDING_TIMEOUT: symbol=" + event.symbol +
          ", client_order_id=" + state.client_order_id +
          ", age_ticks=" +
          std::to_string(market_tick_count_ - state.created_tick) +
          ", timeout_ticks=" + std::to_string(timeout_ticks) +
          ", attempts=" + std::to_string(state.attempts) +
          ", replacement_pending=" +
          std::string(state.replacement_pending ? "true" : "false") +
          ", replacement_taker=" +
          std::string(state.replacement_taker ? "true" : "false"));
}

void BotApplication::ManageCandidateProbeLifecycle(const MarketEvent& event) {
  if (!config_.execution_candidate_probe_enabled || event.symbol.empty() ||
      executor_ == nullptr) {
    return;
  }
  auto it = active_candidate_probe_by_symbol_.find(event.symbol);
  if (it == active_candidate_probe_by_symbol_.end()) {
    return;
  }

  auto& state = it->second;
  const OrderRecord* record = oms_.Find(state.client_order_id);
  const bool waiting_cancel_ack =
      state.cancel_requested && !state.cancel_confirmed;
  const bool terminal =
      record == nullptr || OrderManager::IsTerminalState(record->state);
  const double actual_notional =
      system_.account().current_notional_usd(event.symbol);
  if ((record != nullptr && record->filled_qty > kNotionalEpsilon) ||
      HasExposure(actual_notional)) {
    active_candidate_probe_by_symbol_.erase(it);
    return;
  }
  if (!state.cancel_confirmed && !waiting_cancel_ack && terminal) {
    active_candidate_probe_by_symbol_.erase(it);
    return;
  }

  if (state.cancel_confirmed) {
    if (!state.replacement_pending) {
      active_candidate_probe_by_symbol_.erase(it);
      return;
    }
    if (trading_halted_ || IsForceReduceOnlyActive() ||
        adapter_ == nullptr || !adapter_->TradeOk()) {
      ++funnel_window_.candidate_probe_expired_without_fill;
      LogInfo("TREND_CANDIDATE_PROBE_EXPIRED_WITHOUT_FILL: symbol=" +
              event.symbol +
              ", client_order_id=" + state.client_order_id +
              ", reason=replacement_trade_not_ok");
      active_candidate_probe_by_symbol_.erase(it);
      return;
    }

    double reference_price = event.price > 0.0 ? event.price : event.mark_price;
    if (!std::isfinite(reference_price) || reference_price <= 0.0) {
      reference_price = state.reference_price;
    }
    if (!std::isfinite(reference_price) || reference_price <= 0.0) {
      ++funnel_window_.candidate_probe_expired_without_fill;
      LogInfo("TREND_CANDIDATE_PROBE_EXPIRED_WITHOUT_FILL: symbol=" +
              event.symbol +
              ", client_order_id=" + state.client_order_id +
              ", reason=replacement_invalid_price");
      active_candidate_probe_by_symbol_.erase(it);
      return;
    }

    const double reprice_ratio =
        std::max(0.0, config_.execution_candidate_probe_reprice_bps) / 10000.0;
    if (!state.replacement_taker && reprice_ratio > 0.0 &&
        state.next_attempts > 0) {
      const double aggressiveness =
          reprice_ratio * static_cast<double>(state.next_attempts);
      reference_price *= state.direction > 0 ? (1.0 + aggressiveness)
                                             : (1.0 - aggressiveness);
    }
    const double configured_min_executable_notional =
        std::max(0.0, config_.execution_min_rebalance_notional_usd);
    const double replacement_min_executable_notional =
        StepAwareExecutableNotionalFloor(adapter_.get(),
                                         event.symbol,
                                         reference_price,
                                         configured_min_executable_notional);
    double replacement_notional_usd = std::max(0.0, state.notional_usd);
    const bool replacement_floor_applied =
        configured_min_executable_notional > kNotionalEpsilon &&
        replacement_notional_usd > kNotionalEpsilon &&
        replacement_notional_usd + kNotionalEpsilon <
            replacement_min_executable_notional;
    if (replacement_floor_applied) {
      replacement_notional_usd = replacement_min_executable_notional;
    }
    RiskAdjustedPosition target{
        .symbol = event.symbol,
        .adjusted_notional_usd =
            static_cast<double>(state.direction) * replacement_notional_usd,
        .reduce_only = false,
        .risk_mode = system_.risk_mode(),
    };
    auto replacement = execution_.BuildIntent(target, 0.0, reference_price);
    if (!replacement.has_value() || !IsOpeningIntent(*replacement)) {
      ++funnel_window_.candidate_probe_expired_without_fill;
      LogInfo("TREND_CANDIDATE_PROBE_EXPIRED_WITHOUT_FILL: symbol=" +
              event.symbol +
              ", client_order_id=" + state.client_order_id +
              ", reason=replacement_build_failed" +
              ", state_notional_usd=" + std::to_string(state.notional_usd) +
              ", replacement_notional_usd=" +
              std::to_string(replacement_notional_usd) +
              ", reference_price=" + std::to_string(reference_price) +
              ", execution_min_rebalance_notional_usd=" +
              std::to_string(configured_min_executable_notional) +
              ", step_aware_min_executable_notional_usd=" +
              std::to_string(replacement_min_executable_notional) +
              ", replacement_floor_applied=" +
              std::string(replacement_floor_applied ? "true" : "false"));
      active_candidate_probe_by_symbol_.erase(it);
      return;
    }
    replacement->liquidity_preference =
        state.replacement_taker ? LiquidityPreference::kTaker
                                : LiquidityPreference::kMaker;
    std::string guard_reason;
    if (ViolatesExchangePretradeGuard(adapter_.get(), &*replacement, event,
                                      &guard_reason)) {
      ++funnel_window_.candidate_probe_expired_without_fill;
      LogInfo("TREND_CANDIDATE_PROBE_EXPIRED_WITHOUT_FILL: symbol=" +
              event.symbol +
              ", client_order_id=" + state.client_order_id +
              ", replacement_client_order_id=" +
              replacement->client_order_id +
              ", reason=" + guard_reason);
      active_candidate_probe_by_symbol_.erase(it);
      return;
    }

    const std::string replacement_id = replacement->client_order_id;
    candidate_probe_intent_ids_.insert(replacement_id);
    candidate_probe_attempt_by_intent_id_[replacement_id] = state.next_attempts;
    candidate_probe_taker_fallback_by_intent_id_[replacement_id] =
        state.replacement_taker;
    const bool replacement_taker = state.replacement_taker;
    const int replacement_attempts = state.next_attempts;
    if (!EnqueueIntent(*replacement)) {
      candidate_probe_attempt_by_intent_id_.erase(replacement_id);
      candidate_probe_taker_fallback_by_intent_id_.erase(replacement_id);
      ++funnel_window_.candidate_probe_expired_without_fill;
      LogInfo("TREND_CANDIDATE_PROBE_EXPIRED_WITHOUT_FILL: symbol=" +
              event.symbol +
              ", client_order_id=" + state.client_order_id +
              ", replacement_client_order_id=" + replacement_id +
              ", reason=replacement_enqueue_failed");
      active_candidate_probe_by_symbol_.erase(event.symbol);
      return;
    }
    if (replacement_taker) {
      ++funnel_window_.candidate_probe_taker_fallbacks;
      LogInfo("TREND_CANDIDATE_PROBE_TAKER_FALLBACK: symbol=" + event.symbol +
              ", client_order_id=" + replacement_id +
              ", attempts=" + std::to_string(replacement_attempts) +
              ", state_notional_usd=" + std::to_string(state.notional_usd) +
              ", replacement_notional_usd=" +
              std::to_string(replacement_notional_usd) +
              ", step_aware_min_executable_notional_usd=" +
              std::to_string(replacement_min_executable_notional) +
              ", replacement_floor_applied=" +
              std::string(replacement_floor_applied ? "true" : "false") +
              ", trend_threshold_ratio=" +
              std::to_string(state.trend_threshold_ratio));
    } else {
      ++funnel_window_.candidate_probe_reprices;
      LogInfo("TREND_CANDIDATE_PROBE_REPRICE: symbol=" + event.symbol +
              ", client_order_id=" + replacement_id +
              ", attempts=" + std::to_string(replacement_attempts) +
              ", state_notional_usd=" + std::to_string(state.notional_usd) +
              ", replacement_notional_usd=" +
              std::to_string(replacement_notional_usd) +
              ", step_aware_min_executable_notional_usd=" +
              std::to_string(replacement_min_executable_notional) +
              ", replacement_floor_applied=" +
              std::string(replacement_floor_applied ? "true" : "false") +
              ", reprice_bps=" +
              std::to_string(config_.execution_candidate_probe_reprice_bps));
    }
    return;
  }

  if (state.cancel_requested) {
    return;
  }

  const int timeout_ticks =
      std::max(0, config_.execution_candidate_probe_post_only_timeout_ticks);
  if (timeout_ticks <= 0 ||
      market_tick_count_ - state.created_tick < timeout_ticks) {
    return;
  }

  const int max_reprice_attempts =
      std::max(0, config_.execution_candidate_probe_reprice_max_attempts);
  const bool can_reprice = state.attempts < max_reprice_attempts;
  const double fallback_min_ratio = std::max(
      0.0, config_.execution_candidate_probe_taker_fallback_min_trend_ratio);
  const bool can_taker_fallback =
      config_.execution_candidate_probe_taker_fallback_enabled &&
      !state.taker_fallback_used &&
      state.trend_threshold_ratio + 1e-9 >= fallback_min_ratio;

  ++funnel_window_.candidate_probe_pending_timeouts;
  state.cancel_requested = true;
  state.cancel_confirmed = false;
  state.replacement_pending = can_reprice || can_taker_fallback;
  state.replacement_taker = !can_reprice && can_taker_fallback;
  state.next_attempts = can_reprice ? state.attempts + 1 : state.attempts;
  oms_.MarkCancelPending(state.client_order_id);
  pending_net_order_enqueued_ms_[state.client_order_id] = CurrentTimestampMs();
  executor_->Cancel(state.client_order_id);
  ++funnel_window_.candidate_probe_cancel_submitted;
  if (!state.replacement_pending) {
    ++funnel_window_.candidate_probe_expired_without_fill;
  }
  LogInfo("TREND_CANDIDATE_PROBE_PENDING_TIMEOUT: symbol=" + event.symbol +
          ", client_order_id=" + state.client_order_id +
          ", age_ticks=" +
          std::to_string(market_tick_count_ - state.created_tick) +
          ", timeout_ticks=" + std::to_string(timeout_ticks) +
          ", attempts=" + std::to_string(state.attempts) +
          ", replacement_pending=" +
          std::string(state.replacement_pending ? "true" : "false") +
          ", replacement_taker=" +
          std::string(state.replacement_taker ? "true" : "false") +
          ", trend_threshold_ratio=" +
          std::to_string(state.trend_threshold_ratio));
}

void BotApplication::EvaluateExecutionQualityGuard(
    std::uint64_t window_fills,
    double window_realized_net_per_fill_usd,
    double window_fee_delta_usd,
    double window_notional_abs_usd) {
  // 全局 entry guard 只评价开仓执行成本；净盈亏质量交给 symbol 级闭合成交判断。
  (void)window_realized_net_per_fill_usd;
  constexpr double kMinNotionalForFeeBpsUsd = 100.0;
  const double max_fee_bps_per_fill = EffectiveQualityGuardMaxFeeBps(config_);
  const double window_fee_bps =
      window_notional_abs_usd > 1e-9
          ? window_fee_delta_usd / window_notional_abs_usd * 10000.0
          : 0.0;
  const bool window_has_fee_bps_sample =
      window_notional_abs_usd >= kMinNotionalForFeeBpsUsd;
  constexpr bool window_has_net_quality_sample = false;
  if (!config_.execution_quality_guard_enabled) {
    execution_quality_guard_active_ = false;
    execution_quality_required_edge_penalty_bps_ = 0.0;
    execution_quality_bad_streak_ = 0;
    execution_quality_good_streak_ = 0;
    execution_quality_no_fill_windows_ = 0;
    execution_quality_pending_fills_ = 0;
    execution_quality_pending_realized_net_sum_usd_ = 0.0;
    execution_quality_pending_fee_usd_sum_ = 0.0;
    execution_quality_pending_notional_abs_usd_sum_ = 0.0;
    execution_quality_by_symbol_.clear();
    return;
  }

  if (window_fills > 0) {
    execution_quality_no_fill_windows_ = 0;
    execution_quality_pending_fills_ += window_fills;
    execution_quality_pending_fee_usd_sum_ += window_fee_delta_usd;
    execution_quality_pending_notional_abs_usd_sum_ += window_notional_abs_usd;
  }

  const std::uint64_t min_fills = static_cast<std::uint64_t>(std::max(
      0, config_.execution_quality_guard_min_fills));
  const bool severe_bad_window =
      window_fills > 0 &&
      ((window_has_net_quality_sample &&
        window_realized_net_per_fill_usd <
            config_.execution_quality_guard_min_realized_net_per_fill_usd * 2.0) ||
       (window_has_fee_bps_sample &&
        window_fee_bps > max_fee_bps_per_fill * 1.5));
  // 若守卫激活后长期无成交，且待评估 fills 仍不足以形成有效结论，
  // 允许按更长 release 窗口自动退出守卫并清空陈旧 pending 状态，
  // 避免“上一轮差成交残留 => 本轮无成交 => 永远不释放”锁死。
  if (execution_quality_guard_active_ && window_fills == 0 &&
      execution_quality_pending_fills_ < min_fills) {
    ++execution_quality_no_fill_windows_;
    const double trigger_ratio =
        std::clamp(config_.execution_adaptive_fee_gate_trigger_ratio, 0.0, 1.0);
    if (entry_gate_observed_filtered_ratio_ >= trigger_ratio) {
      ++execution_quality_good_streak_;
    } else {
      execution_quality_good_streak_ = 0;
    }
    const int base_release_streak =
        std::max(1, config_.execution_quality_guard_good_streak_to_release);
    const int stale_release_streak = base_release_streak * 12;
    if (execution_quality_good_streak_ >= stale_release_streak ||
        execution_quality_no_fill_windows_ >= stale_release_streak) {
      const int stale_no_fill_windows = execution_quality_no_fill_windows_;
      const auto stale_pending_fills = execution_quality_pending_fills_;
      const double stale_filtered_ratio = entry_gate_observed_filtered_ratio_;
      execution_quality_guard_active_ = false;
      execution_quality_required_edge_penalty_bps_ = 0.0;
      execution_quality_bad_streak_ = 0;
      execution_quality_good_streak_ = 0;
      execution_quality_no_fill_windows_ = 0;
      execution_quality_pending_fills_ = 0;
      execution_quality_pending_realized_net_sum_usd_ = 0.0;
      execution_quality_pending_fee_usd_sum_ = 0.0;
      execution_quality_pending_notional_abs_usd_sum_ = 0.0;
      cost_filter_reject_streak_by_symbol_.clear();
      cost_filter_cooldown_until_tick_by_symbol_.clear();
      LogInfo("EXECUTION_QUALITY_GUARD_EXIT_STALE: release_streak=" +
              std::to_string(stale_release_streak) +
              ", no_fill_windows=" +
              std::to_string(stale_no_fill_windows) +
              ", pending_fills=" +
              std::to_string(stale_pending_fills) +
              ", observed_filtered_ratio=" +
              std::to_string(stale_filtered_ratio) +
              ", trigger_ratio=" + std::to_string(trigger_ratio));
    }
    return;
  }
  if (window_fills == 0) {
    execution_quality_no_fill_windows_ = 0;
  }
  if (execution_quality_pending_fills_ == 0) {
    return;
  }
  if (!severe_bad_window && execution_quality_pending_fills_ < min_fills) {
    return;
  }

  const double eval_fills = static_cast<double>(execution_quality_pending_fills_);
  const double eval_realized_net_per_fill_usd =
      eval_fills > 0.0 ? execution_quality_pending_realized_net_sum_usd_ / eval_fills
                       : 0.0;
  const bool eval_has_fee_bps_sample =
      execution_quality_pending_notional_abs_usd_sum_ >= kMinNotionalForFeeBpsUsd;
  constexpr bool eval_has_net_quality_sample = false;
  const double eval_fee_bps_per_fill =
      execution_quality_pending_notional_abs_usd_sum_ > 1e-9
          ? execution_quality_pending_fee_usd_sum_ /
                execution_quality_pending_notional_abs_usd_sum_ * 10000.0
          : 0.0;
  const double eval_notional_abs_usd =
      execution_quality_pending_notional_abs_usd_sum_;
  execution_quality_pending_fills_ = 0;
  execution_quality_pending_realized_net_sum_usd_ = 0.0;
  execution_quality_pending_fee_usd_sum_ = 0.0;
  execution_quality_pending_notional_abs_usd_sum_ = 0.0;

  const bool bad_quality =
      (eval_has_net_quality_sample &&
       eval_realized_net_per_fill_usd <
           config_.execution_quality_guard_min_realized_net_per_fill_usd) ||
      (eval_has_fee_bps_sample &&
       eval_fee_bps_per_fill > max_fee_bps_per_fill);
  if (bad_quality) {
    execution_quality_no_fill_windows_ = 0;
    ++execution_quality_bad_streak_;
    execution_quality_good_streak_ = 0;
    const int configured_trigger_streak =
        std::max(0, config_.execution_quality_guard_bad_streak_to_trigger);
    const int trigger_streak =
        severe_bad_window
            ? 1
            : (configured_trigger_streak == 0
                   ? 0
                   : std::min(configured_trigger_streak, 2));
    if (!execution_quality_guard_active_ &&
        (trigger_streak == 0 || execution_quality_bad_streak_ >= trigger_streak)) {
      execution_quality_guard_active_ = true;
      execution_quality_required_edge_penalty_bps_ = std::max(
          0.0, config_.execution_quality_guard_required_edge_penalty_bps);
      LogInfo("EXECUTION_QUALITY_GUARD_ENTER: bad_streak=" +
              std::to_string(execution_quality_bad_streak_) +
              ", eval_fills=" + std::to_string(static_cast<int>(eval_fills)) +
              ", eval_realized_net_per_fill_usd=" +
              std::to_string(eval_realized_net_per_fill_usd) +
              ", eval_fee_bps_per_fill=" + std::to_string(eval_fee_bps_per_fill) +
              ", eval_notional_abs_usd=" + std::to_string(eval_notional_abs_usd) +
              ", eval_fee_bps_has_sample=" +
              std::string(eval_has_fee_bps_sample ? "true" : "false") +
              ", eval_net_quality_has_sample=" +
              std::string(eval_has_net_quality_sample ? "true" : "false") +
              ", min_realized_net_per_fill_usd=" +
              std::to_string(
                  config_.execution_quality_guard_min_realized_net_per_fill_usd) +
              ", max_fee_bps_per_fill=" +
              std::to_string(max_fee_bps_per_fill) +
              ", applied_penalty_bps=" +
              std::to_string(execution_quality_required_edge_penalty_bps_));
    }
    return;
  }

  execution_quality_bad_streak_ = 0;
  if (!execution_quality_guard_active_) {
    execution_quality_good_streak_ = 0;
    execution_quality_no_fill_windows_ = 0;
    execution_quality_required_edge_penalty_bps_ = 0.0;
    return;
  }
  ++execution_quality_good_streak_;
  execution_quality_no_fill_windows_ = 0;
  const int release_streak =
      std::max(0, config_.execution_quality_guard_good_streak_to_release);
  if (release_streak == 0 || execution_quality_good_streak_ >= release_streak) {
    execution_quality_guard_active_ = false;
    execution_quality_required_edge_penalty_bps_ = 0.0;
    execution_quality_good_streak_ = 0;
    execution_quality_no_fill_windows_ = 0;
    LogInfo("EXECUTION_QUALITY_GUARD_EXIT: release_streak=" +
            std::to_string(release_streak) +
            ", eval_fills=" + std::to_string(static_cast<int>(eval_fills)) +
            ", eval_realized_net_per_fill_usd=" +
            std::to_string(eval_realized_net_per_fill_usd) +
            ", eval_fee_bps_per_fill=" + std::to_string(eval_fee_bps_per_fill) +
            ", eval_notional_abs_usd=" + std::to_string(eval_notional_abs_usd) +
            ", eval_fee_bps_has_sample=" +
            std::string(eval_has_fee_bps_sample ? "true" : "false") +
            ", eval_net_quality_has_sample=" +
            std::string(eval_has_net_quality_sample ? "true" : "false"));
  }
}

bool BotApplication::IsSymbolExecutionQualityGuardActive(
    const std::string& symbol) const {
  const auto it = execution_quality_by_symbol_.find(symbol);
  if (it == execution_quality_by_symbol_.end()) {
    return false;
  }
  return it->second.guard_active ||
         it->second.cooldown_until_tick >= market_tick_count_ ||
         it->second.memory_until_tick >= market_tick_count_;
}

bool BotApplication::HasSymbolExecutionQualityMemory(
    const std::string& symbol) const {
  const auto it = execution_quality_by_symbol_.find(symbol);
  if (it == execution_quality_by_symbol_.end()) {
    return false;
  }
  return it->second.guard_active ||
         it->second.cooldown_until_tick >= market_tick_count_ ||
         it->second.memory_until_tick >= market_tick_count_ ||
         it->second.trigger_count > 0;
}

int BotApplication::SymbolExecutionQualityMemoryTriggerCount(
    const std::string& symbol) const {
  const auto it = execution_quality_by_symbol_.find(symbol);
  if (it == execution_quality_by_symbol_.end()) {
    return 0;
  }
  return std::max(0, it->second.trigger_count);
}

int BotApplication::SymbolExecutionQualityMemoryCooldownTicks(
    int trigger_count) const {
  const int configured_cooldown_ticks =
      std::max(0, config_.execution_candidate_probe_cooldown_ticks);
  const int release_window_ticks =
      std::max(1, config_.execution_quality_guard_good_streak_to_release) * 120;
  const int base_cooldown_ticks =
      std::max(120, std::max(configured_cooldown_ticks, release_window_ticks));
  return base_cooldown_ticks * std::clamp(trigger_count, 1, 3);
}

int BotApplication::SymbolExecutionQualityActiveRemainingTicks(
    const std::string& symbol) const {
  const auto it = execution_quality_by_symbol_.find(symbol);
  if (it == execution_quality_by_symbol_.end()) {
    return 0;
  }
  int until_tick = it->second.guard_active ? market_tick_count_ : -1000000;
  until_tick = std::max(until_tick, it->second.cooldown_until_tick);
  until_tick = std::max(until_tick, it->second.memory_until_tick);
  return std::max(0, until_tick - market_tick_count_);
}

double BotApplication::SymbolExecutionQualityProbeNotionalScale(
    const std::string& symbol) const {
  const int trigger_count = SymbolExecutionQualityMemoryTriggerCount(symbol);
  if (trigger_count <= 0) {
    return 1.0;
  }
  const double penalty_steps =
      static_cast<double>(std::clamp(trigger_count, 1, 4));
  return std::clamp(1.0 / (1.0 + 0.5 * penalty_steps), 0.33, 1.0);
}

bool BotApplication::ShouldThrottleSymbolQualityQuarantine(
    const MarketDecision& decision,
    int* out_remaining_ticks) const {
  if (out_remaining_ticks != nullptr) {
    *out_remaining_ticks = 0;
  }
  if (!decision.intent.has_value() || !IsOpeningIntent(*decision.intent)) {
    return false;
  }
  if (IsTrendCandidateProbeIntent(decision.intent->client_order_id)) {
    return false;
  }
  const std::string& symbol = decision.intent->symbol;
  if (!HasSymbolExecutionQualityMemory(symbol)) {
    return false;
  }
  const int remaining_ticks = SymbolExecutionQualityActiveRemainingTicks(symbol);
  if (out_remaining_ticks != nullptr) {
    *out_remaining_ticks = remaining_ticks;
  }
  // trigger_count soft memory still downweights recovery probes and blocks some
  // relaxed fee paths, but it should not hard-quarantine new entry samples once
  // the active cooldown/memory window has fully expired.
  return remaining_ticks > 0;
}

int BotApplication::SymbolExecutionQualityMinHoldRemainingTicks(
    const std::string& symbol) const {
  const auto it = execution_quality_by_symbol_.find(symbol);
  if (it == execution_quality_by_symbol_.end() ||
      it->second.last_maker_entry_tick < 0 ||
      !HasSymbolExecutionQualityMemory(symbol)) {
    return 0;
  }
  const int quality_hold_ticks =
      std::max(std::max(0, config_.strategy_min_hold_ticks),
               std::max(0, config_.execution_candidate_probe_cooldown_ticks) /
                   4);
  const int min_hold_ticks = std::max(60, quality_hold_ticks);
  return std::max(
      0, it->second.last_maker_entry_tick + min_hold_ticks - market_tick_count_);
}

bool BotApplication::ShouldThrottleSymbolQualityMinHold(
    const MarketDecision& decision,
    int* out_remaining_ticks) const {
  if (out_remaining_ticks != nullptr) {
    *out_remaining_ticks = 0;
  }
  if (!decision.intent.has_value() ||
      decision.intent->purpose != OrderPurpose::kReduce ||
      !decision.intent->reduce_only) {
    return false;
  }
  if (decision.risk_adjusted.reduce_only || IsForceReduceOnlyActive()) {
    return false;
  }
  const int remaining_ticks =
      SymbolExecutionQualityMinHoldRemainingTicks(decision.intent->symbol);
  if (out_remaining_ticks != nullptr) {
    *out_remaining_ticks = remaining_ticks;
  }
  return remaining_ticks > 0;
}

bool BotApplication::ShouldThrottleStrategyReduceCostGuard(
    const MarketDecision& decision,
    const MarketEvent& event,
    double* out_estimated_gross_bps,
    double* out_estimated_net_bps,
    double* out_required_net_bps,
    double* out_expected_exit_cost_bps,
    int* out_holding_ticks,
    std::string* out_bypass_reason) const {
  if (out_estimated_gross_bps != nullptr) {
    *out_estimated_gross_bps = 0.0;
  }
  if (out_estimated_net_bps != nullptr) {
    *out_estimated_net_bps = 0.0;
  }
  if (out_required_net_bps != nullptr) {
    *out_required_net_bps = 0.0;
  }
  if (out_expected_exit_cost_bps != nullptr) {
    *out_expected_exit_cost_bps = 0.0;
  }
  if (out_holding_ticks != nullptr) {
    *out_holding_ticks = 0;
  }
  if (out_bypass_reason != nullptr) {
    out_bypass_reason->clear();
  }

  if (!config_.execution_strategy_reduce_cost_guard_enabled ||
      !decision.intent.has_value() ||
      decision.intent->purpose != OrderPurpose::kReduce ||
      !decision.intent->reduce_only) {
    return false;
  }
  if (decision.risk_adjusted.reduce_only || IsForceReduceOnlyActive()) {
    if (out_bypass_reason != nullptr) {
      *out_bypass_reason = "forced_reduce_only";
    }
    return false;
  }
  if (decision.risk_adjusted.risk_mode != RiskMode::kNormal) {
    if (out_bypass_reason != nullptr) {
      *out_bypass_reason = std::string("risk_mode_") +
                           RiskModeToString(decision.risk_adjusted.risk_mode);
    }
    return false;
  }
  if (decision.regime.bucket == RegimeBucket::kExtreme) {
    if (out_bypass_reason != nullptr) {
      *out_bypass_reason = "extreme_regime";
    }
    return false;
  }

  const OrderIntent& intent = *decision.intent;
  const double position_qty = system_.account().position_qty(intent.symbol);
  const int entry_direction = SignOf(position_qty);
  if (entry_direction == 0 || entry_direction == intent.direction) {
    return false;
  }
  const double avg_entry_price = system_.account().avg_entry_price(intent.symbol);
  double exit_price = intent.price;
  if (!std::isfinite(exit_price) || exit_price <= kNotionalEpsilon) {
    exit_price = event.mark_price > kNotionalEpsilon ? event.mark_price
                                                     : event.price;
  }
  if (!std::isfinite(avg_entry_price) || avg_entry_price <= kNotionalEpsilon ||
      !std::isfinite(exit_price) || exit_price <= kNotionalEpsilon ||
      !std::isfinite(intent.qty) || intent.qty <= kNotionalEpsilon) {
    return false;
  }

  const double estimated_gross_bps =
      FavorableReturn(entry_direction, avg_entry_price, exit_price) * 10000.0;
  const bool maker_reduce =
      intent.liquidity_preference == LiquidityPreference::kMaker;
  const double expected_exit_cost_bps =
      std::max(0.0, config_.execution_exit_fee_bps) +
      (maker_reduce ? 0.0 : std::max(0.0, config_.execution_expected_slippage_bps));
  const double estimated_net_bps = estimated_gross_bps - expected_exit_cost_bps;

  int holding_ticks = 0;
  if (const auto state_it = managed_protection_by_symbol_.find(intent.symbol);
      state_it != managed_protection_by_symbol_.end()) {
    holding_ticks = std::max(0, market_tick_count_ - state_it->second.entry_tick);
  }
  const auto probe_position_it =
      candidate_probe_position_entry_tick_by_symbol_.find(intent.symbol);
  const bool candidate_probe_position =
      probe_position_it != candidate_probe_position_entry_tick_by_symbol_.end();
  if (candidate_probe_position && holding_ticks == 0) {
    holding_ticks = std::max(0, market_tick_count_ - probe_position_it->second);
  }
  double required_net_bps =
      std::max(0.0, config_.execution_strategy_reduce_min_net_bps);
  if (candidate_probe_position &&
      config_.execution_candidate_probe_reduce_min_net_bps > 0.0) {
    required_net_bps =
        std::max(0.0, config_.execution_candidate_probe_reduce_min_net_bps);
  }

  if (out_estimated_gross_bps != nullptr) {
    *out_estimated_gross_bps = estimated_gross_bps;
  }
  if (out_estimated_net_bps != nullptr) {
    *out_estimated_net_bps = estimated_net_bps;
  }
  if (out_required_net_bps != nullptr) {
    *out_required_net_bps = required_net_bps;
  }
  if (out_expected_exit_cost_bps != nullptr) {
    *out_expected_exit_cost_bps = expected_exit_cost_bps;
  }
  if (out_holding_ticks != nullptr) {
    *out_holding_ticks = holding_ticks;
  }

  if (estimated_net_bps >= required_net_bps) {
    return false;
  }

  const double probe_max_adverse_bps =
      std::max(0.0, config_.execution_candidate_probe_reduce_max_adverse_bps);
  if (candidate_probe_position && probe_max_adverse_bps > 0.0 &&
      estimated_net_bps <= -probe_max_adverse_bps) {
    if (out_bypass_reason != nullptr) {
      *out_bypass_reason = "candidate_probe_adverse_cut";
    }
    return false;
  }

  const double max_adverse_bps =
      std::max(0.0, config_.execution_strategy_reduce_max_adverse_bps);
  if (max_adverse_bps > 0.0 && estimated_net_bps <= -max_adverse_bps) {
    if (out_bypass_reason != nullptr) {
      *out_bypass_reason = "adverse_cut";
    }
    return false;
  }

  const int max_hold_ticks =
      std::max(0, config_.execution_strategy_reduce_guard_max_hold_ticks);
  if (max_hold_ticks > 0 && holding_ticks >= max_hold_ticks) {
    if (out_bypass_reason != nullptr) {
      *out_bypass_reason = "max_hold";
    }
    return false;
  }

  return true;
}

double BotApplication::SymbolExecutionQualityPenaltyBps(
    const std::string& symbol) const {
  const auto it = execution_quality_by_symbol_.find(symbol);
  if (it == execution_quality_by_symbol_.end() ||
      !IsSymbolExecutionQualityGuardActive(symbol)) {
    return 0.0;
  }
  return std::max(0.0, it->second.required_edge_penalty_bps);
}

int BotApplication::ActiveSymbolExecutionQualityGuardCount() const {
  int active_count = 0;
  for (const auto& [symbol, state] : execution_quality_by_symbol_) {
    (void)symbol;
    if (state.guard_active || state.cooldown_until_tick >= market_tick_count_ ||
        state.memory_until_tick >= market_tick_count_) {
      ++active_count;
    }
  }
  return active_count;
}

void BotApplication::EvaluateSymbolExecutionQualityGuard(
    const std::string& symbol,
    std::uint64_t window_fills,
    std::uint64_t window_net_quality_fills,
    double window_realized_net_sum_usd,
    double window_fee_delta_usd,
    double window_notional_abs_usd) {
  if (symbol.empty()) {
    return;
  }
  if (!config_.execution_quality_guard_enabled) {
    execution_quality_by_symbol_.clear();
    return;
  }

  constexpr double kMinNotionalForFeeBpsUsd = 100.0;
  const double max_fee_bps_per_fill = EffectiveQualityGuardMaxFeeBps(config_);
  const double min_notional_for_net_quality_usd =
      std::max(kMinNotionalForFeeBpsUsd,
               std::max(0.0, config_.strategy_signal_notional_usd) * 0.5);
  const double window_realized_net_per_fill_usd =
      window_net_quality_fills > 0
          ? window_realized_net_sum_usd /
                static_cast<double>(window_net_quality_fills)
          : 0.0;
  const double window_fee_bps =
      window_notional_abs_usd > 1e-9
          ? window_fee_delta_usd / window_notional_abs_usd * 10000.0
          : 0.0;
  const bool window_has_fee_bps_sample =
      window_notional_abs_usd >= kMinNotionalForFeeBpsUsd;
  const bool window_has_net_quality_sample =
      window_net_quality_fills > 0 &&
      window_notional_abs_usd >= min_notional_for_net_quality_usd;

  auto& state = execution_quality_by_symbol_[symbol];
  if (window_fills > 0) {
    state.no_fill_windows = 0;
    state.pending_fills += window_fills;
    state.pending_net_quality_fills += window_net_quality_fills;
    state.pending_realized_net_sum_usd += window_realized_net_sum_usd;
    state.pending_fee_usd_sum += window_fee_delta_usd;
    state.pending_notional_abs_usd_sum += window_notional_abs_usd;
  }

  const std::uint64_t configured_min_fills =
      static_cast<std::uint64_t>(
          std::max(0, config_.execution_quality_guard_min_fills));
  const std::uint64_t min_fills =
      configured_min_fills == 0 ? 0 : std::min<std::uint64_t>(configured_min_fills, 6);
  const bool severe_bad_window =
      window_fills > 0 &&
      ((window_has_net_quality_sample &&
        window_realized_net_per_fill_usd <
            config_.execution_quality_guard_min_realized_net_per_fill_usd * 2.0) ||
       (window_has_fee_bps_sample &&
        window_fee_bps > max_fee_bps_per_fill * 1.5 &&
        (!window_has_net_quality_sample || window_realized_net_per_fill_usd < 0.0)));

  if (state.guard_active && window_fills == 0 && state.pending_fills < min_fills) {
    ++state.no_fill_windows;
    const double trigger_ratio =
        std::clamp(config_.execution_adaptive_fee_gate_trigger_ratio, 0.0, 1.0);
    if (entry_gate_observed_filtered_ratio_ >= trigger_ratio) {
      ++state.good_streak;
    } else {
      state.good_streak = 0;
    }
    const int base_release_streak =
        std::max(1, config_.execution_quality_guard_good_streak_to_release);
    const int stale_release_streak = base_release_streak * 12;
    if (state.good_streak >= stale_release_streak ||
        state.no_fill_windows >= stale_release_streak) {
      const int stale_no_fill_windows = state.no_fill_windows;
      const auto stale_pending_fills = state.pending_fills;
      const double stale_filtered_ratio = entry_gate_observed_filtered_ratio_;
      state.guard_active = false;
      state.bad_streak = 0;
      state.good_streak = 0;
      state.no_fill_windows = 0;
      state.pending_fills = 0;
      state.pending_net_quality_fills = 0;
      state.pending_realized_net_sum_usd = 0.0;
      state.pending_fee_usd_sum = 0.0;
      state.pending_notional_abs_usd_sum = 0.0;
      const int memory_ticks =
          SymbolExecutionQualityMemoryCooldownTicks(state.trigger_count);
      const int memory_until_tick = market_tick_count_ + memory_ticks;
      state.cooldown_until_tick =
          std::max(state.cooldown_until_tick, memory_until_tick);
      state.memory_until_tick =
          std::max(state.memory_until_tick, memory_until_tick);
      LogInfo("EXECUTION_SYMBOL_QUALITY_GUARD_EXIT_STALE: symbol=" + symbol +
              ", release_streak=" + std::to_string(stale_release_streak) +
              ", no_fill_windows=" +
              std::to_string(stale_no_fill_windows) +
              ", pending_fills=" + std::to_string(stale_pending_fills) +
              ", observed_filtered_ratio=" +
              std::to_string(stale_filtered_ratio) +
              ", trigger_ratio=" + std::to_string(trigger_ratio) +
              ", cooldown_until_tick=" +
              std::to_string(state.cooldown_until_tick) +
              ", memory_until_tick=" +
              std::to_string(state.memory_until_tick) +
              ", cooldown_remaining_ticks=" +
              std::to_string(std::max(0, state.cooldown_until_tick -
                                             market_tick_count_)));
    }
    return;
  }
  if (window_fills == 0) {
    state.no_fill_windows = 0;
  }
  if (state.pending_fills == 0) {
    return;
  }
  if (!severe_bad_window && state.pending_fills < min_fills) {
    return;
  }

  const double eval_fills = static_cast<double>(state.pending_fills);
  const double eval_net_quality_fills =
      static_cast<double>(state.pending_net_quality_fills);
  const double eval_realized_net_per_fill_usd =
      eval_net_quality_fills > 0.0
          ? state.pending_realized_net_sum_usd / eval_net_quality_fills
          : 0.0;
  const bool eval_has_fee_bps_sample =
      state.pending_notional_abs_usd_sum >= kMinNotionalForFeeBpsUsd;
  const bool eval_has_net_quality_sample =
      state.pending_net_quality_fills > 0 &&
      state.pending_notional_abs_usd_sum >= min_notional_for_net_quality_usd;
  const double eval_fee_bps_per_fill =
      state.pending_notional_abs_usd_sum > 1e-9
          ? state.pending_fee_usd_sum / state.pending_notional_abs_usd_sum *
                10000.0
          : 0.0;
  const double eval_notional_abs_usd = state.pending_notional_abs_usd_sum;
  state.pending_fills = 0;
  state.pending_net_quality_fills = 0;
  state.pending_realized_net_sum_usd = 0.0;
  state.pending_fee_usd_sum = 0.0;
  state.pending_notional_abs_usd_sum = 0.0;

  const bool net_bad_quality =
      eval_has_net_quality_sample &&
      eval_realized_net_per_fill_usd <
          config_.execution_quality_guard_min_realized_net_per_fill_usd;
  const bool fee_bad_quality =
      eval_has_fee_bps_sample && eval_fee_bps_per_fill > max_fee_bps_per_fill &&
      (!eval_has_net_quality_sample || eval_realized_net_per_fill_usd < 0.0);
  const bool bad_quality = net_bad_quality || fee_bad_quality;
  if (bad_quality) {
    state.no_fill_windows = 0;
    ++state.bad_streak;
    state.good_streak = 0;
    const int configured_trigger_streak =
        std::max(0, config_.execution_quality_guard_bad_streak_to_trigger);
    const int trigger_streak =
        severe_bad_window
            ? 1
            : (configured_trigger_streak == 0
                   ? 0
                   : std::min(configured_trigger_streak, 2));
    const bool should_enter =
        !state.guard_active &&
        (trigger_streak == 0 || state.bad_streak >= trigger_streak);
    if (should_enter || state.guard_active) {
      state.trigger_count = std::min(state.trigger_count + 1, 12);
      state.guard_active = true;
      const double penalty_multiplier =
          static_cast<double>(std::clamp(state.trigger_count, 1, 3));
      state.required_edge_penalty_bps =
          std::max(0.0, config_.execution_quality_guard_required_edge_penalty_bps) *
          penalty_multiplier;
      const int memory_ticks =
          SymbolExecutionQualityMemoryCooldownTicks(state.trigger_count);
      const int memory_until_tick = market_tick_count_ + memory_ticks;
      state.cooldown_until_tick =
          std::max(state.cooldown_until_tick, memory_until_tick);
      state.memory_until_tick =
          std::max(state.memory_until_tick, memory_until_tick);
      LogInfo(std::string(should_enter ? "EXECUTION_SYMBOL_QUALITY_GUARD_ENTER"
                                       : "EXECUTION_SYMBOL_QUALITY_GUARD_REINFORCE") +
              ": symbol=" + symbol +
              ", bad_streak=" + std::to_string(state.bad_streak) +
              ", trigger_count=" + std::to_string(state.trigger_count) +
              ", eval_fills=" + std::to_string(static_cast<int>(eval_fills)) +
              ", eval_net_quality_fills=" +
              std::to_string(static_cast<int>(eval_net_quality_fills)) +
              ", eval_realized_net_per_fill_usd=" +
              std::to_string(eval_realized_net_per_fill_usd) +
              ", eval_fee_bps_per_fill=" + std::to_string(eval_fee_bps_per_fill) +
              ", eval_notional_abs_usd=" + std::to_string(eval_notional_abs_usd) +
              ", eval_fee_bps_has_sample=" +
              std::string(eval_has_fee_bps_sample ? "true" : "false") +
              ", eval_net_quality_has_sample=" +
              std::string(eval_has_net_quality_sample ? "true" : "false") +
              ", min_realized_net_per_fill_usd=" +
              std::to_string(
                  config_.execution_quality_guard_min_realized_net_per_fill_usd) +
              ", max_fee_bps_per_fill=" +
              std::to_string(max_fee_bps_per_fill) +
              ", applied_penalty_bps=" +
              std::to_string(state.required_edge_penalty_bps) +
              ", cooldown_until_tick=" +
              std::to_string(state.cooldown_until_tick) +
              ", memory_until_tick=" +
              std::to_string(state.memory_until_tick) +
              ", cooldown_remaining_ticks=" +
              std::to_string(std::max(0, state.cooldown_until_tick -
                                             market_tick_count_)));
    }
    return;
  }

  state.bad_streak = 0;
  if (!state.guard_active) {
    state.good_streak = 0;
    state.no_fill_windows = 0;
    const bool has_quality_sample =
        eval_has_net_quality_sample || eval_has_fee_bps_sample;
    if (has_quality_sample && state.trigger_count > 0) {
      const int trigger_count_before = state.trigger_count;
      state.trigger_count = std::max(0, state.trigger_count - 1);
      if (state.trigger_count == 0) {
        state.cooldown_until_tick = -1000000;
        state.memory_until_tick = -1000000;
        state.required_edge_penalty_bps = 0.0;
      } else if (IsSymbolExecutionQualityGuardActive(symbol)) {
        const int memory_until_tick =
            market_tick_count_ +
            SymbolExecutionQualityMemoryCooldownTicks(state.trigger_count);
        state.cooldown_until_tick =
            std::min(state.cooldown_until_tick, memory_until_tick);
        state.memory_until_tick =
            std::min(state.memory_until_tick, memory_until_tick);
        state.required_edge_penalty_bps =
            std::max(0.0,
                     config_.execution_quality_guard_required_edge_penalty_bps) *
            static_cast<double>(std::clamp(state.trigger_count, 1, 3));
      }
      LogInfo("EXECUTION_SYMBOL_QUALITY_MEMORY_DECAY: symbol=" + symbol +
              ", trigger_count_before=" +
              std::to_string(trigger_count_before) +
              ", trigger_count_after=" + std::to_string(state.trigger_count) +
              ", eval_fills=" + std::to_string(static_cast<int>(eval_fills)) +
              ", eval_realized_net_per_fill_usd=" +
              std::to_string(eval_realized_net_per_fill_usd) +
              ", eval_fee_bps_per_fill=" + std::to_string(eval_fee_bps_per_fill) +
              ", memory_until_tick=" + std::to_string(state.memory_until_tick));
    }
    if (!IsSymbolExecutionQualityGuardActive(symbol)) {
      state.required_edge_penalty_bps = 0.0;
    }
    return;
  }
  ++state.good_streak;
  state.no_fill_windows = 0;
  const int release_streak =
      std::max(0, config_.execution_quality_guard_good_streak_to_release);
  if (release_streak == 0 || state.good_streak >= release_streak) {
    const int trigger_count_before = state.trigger_count;
    const int trigger_count_after = std::max(0, trigger_count_before - 1);
    const int last_maker_entry_tick = state.last_maker_entry_tick;
    state = ExecutionQualityGuardState{};
    state.trigger_count = trigger_count_after;
    state.last_maker_entry_tick = last_maker_entry_tick;
    if (trigger_count_after > 0) {
      const int memory_until_tick =
          market_tick_count_ +
          SymbolExecutionQualityMemoryCooldownTicks(trigger_count_after);
      state.cooldown_until_tick = memory_until_tick;
      state.memory_until_tick = memory_until_tick;
      state.required_edge_penalty_bps =
          std::max(0.0,
                   config_.execution_quality_guard_required_edge_penalty_bps) *
          static_cast<double>(std::clamp(trigger_count_after, 1, 3));
    }
    LogInfo("EXECUTION_SYMBOL_QUALITY_GUARD_EXIT: symbol=" + symbol +
            ", release_streak=" + std::to_string(release_streak) +
            ", trigger_count_before=" + std::to_string(trigger_count_before) +
            ", trigger_count_after=" + std::to_string(trigger_count_after) +
            ", eval_fills=" + std::to_string(static_cast<int>(eval_fills)) +
            ", eval_net_quality_fills=" +
            std::to_string(static_cast<int>(eval_net_quality_fills)) +
            ", eval_realized_net_per_fill_usd=" +
            std::to_string(eval_realized_net_per_fill_usd) +
            ", eval_fee_bps_per_fill=" + std::to_string(eval_fee_bps_per_fill) +
            ", eval_notional_abs_usd=" + std::to_string(eval_notional_abs_usd) +
            ", eval_fee_bps_has_sample=" +
            std::string(eval_has_fee_bps_sample ? "true" : "false") +
            ", eval_net_quality_has_sample=" +
            std::string(eval_has_net_quality_sample ? "true" : "false") +
            ", memory_until_tick=" + std::to_string(state.memory_until_tick) +
            ", cooldown_remaining_ticks=" +
            std::to_string(std::max(0, state.cooldown_until_tick -
                                           market_tick_count_)));
  }
}

void BotApplication::UpdateReconcileAnomalyProtection(bool anomaly_detected,
                                                      const std::string& reason_code) {
  if (anomaly_detected) {
    ++reconcile_anomaly_streak_;
    reconcile_healthy_streak_ = 0;
    const int reduce_only_threshold =
        std::max(0, config_.reconcile.anomaly_reduce_only_streak);
    const int halt_threshold = std::max(0, config_.reconcile.anomaly_halt_streak);
    if (!reconcile_forced_reduce_only_ && reduce_only_threshold > 0 &&
        reconcile_anomaly_streak_ >= reduce_only_threshold) {
      reconcile_forced_reduce_only_ = true;
      RefreshReduceOnlyMode();
      LogError("OMS_RECONCILE_ANOMALY_PROTECTION_ENTER: streak=" +
               std::to_string(reconcile_anomaly_streak_) +
               ", reason=" + reason_code);
    }
    if (!reconcile_halted_ && halt_threshold > 0 &&
        reconcile_anomaly_streak_ >= halt_threshold) {
      reconcile_halted_ = true;
      RefreshTradingHaltState();
      LogError("OMS_RECONCILE_ANOMALY_HALT_ENTER: streak=" +
               std::to_string(reconcile_anomaly_streak_) +
               ", reason=" + reason_code);
    }
    LogInfo("OMS_RECONCILE_ANOMALY_STREAK: streak=" +
            std::to_string(reconcile_anomaly_streak_) +
            ", reason=" + reason_code +
            ", reduce_only=" +
            std::string(reconcile_forced_reduce_only_ ? "true" : "false") +
            ", halted=" + std::string(reconcile_halted_ ? "true" : "false"));
    return;
  }

  reconcile_anomaly_streak_ = 0;
  ++reconcile_healthy_streak_;
  const int resume_threshold = std::max(0, config_.reconcile.anomaly_resume_streak);
  if (resume_threshold <= 0 || reconcile_healthy_streak_ < resume_threshold) {
    return;
  }

  if (reconcile_forced_reduce_only_) {
    reconcile_forced_reduce_only_ = false;
    RefreshReduceOnlyMode();
    LogInfo("OMS_RECONCILE_ANOMALY_PROTECTION_EXIT: healthy_streak=" +
            std::to_string(reconcile_healthy_streak_));
  }
  if (reconcile_halted_) {
    reconcile_halted_ = false;
    RefreshTradingHaltState();
    LogInfo("OMS_RECONCILE_ANOMALY_HALT_EXIT: healthy_streak=" +
            std::to_string(reconcile_healthy_streak_) +
            ", reason=" + reason_code +
            ", trading_halted=" +
            std::string(trading_halted_ ? "true" : "false"));
  }
}

void BotApplication::AccumulateStats(DecisionFunnelStats* total,
                                     const DecisionFunnelStats& delta) {
  if (total == nullptr) {
    return;
  }
  total->raw_signals += delta.raw_signals;
  total->risk_adjusted_signals += delta.risk_adjusted_signals;
  total->intents_generated += delta.intents_generated;
  total->intents_filtered_inactive_symbol +=
      delta.intents_filtered_inactive_symbol;
  total->intents_filtered_min_notional += delta.intents_filtered_min_notional;
  total->intents_filtered_fee_aware += delta.intents_filtered_fee_aware;
  total->intents_filtered_fee_aware_near_miss +=
      delta.intents_filtered_fee_aware_near_miss;
  total->intents_passed_fee_aware_near_miss +=
      delta.intents_passed_fee_aware_near_miss;
  total->rebalance_gap_samples += delta.rebalance_gap_samples;
  total->rebalance_converged_within_min_notional +=
      delta.rebalance_converged_within_min_notional;
  total->intents_throttled_cost_cooldown +=
      delta.intents_throttled_cost_cooldown;
  total->intents_throttled_symbol_quality_quarantine +=
      delta.intents_throttled_symbol_quality_quarantine;
  total->strategy_reduce_cost_guard_blocked +=
      delta.strategy_reduce_cost_guard_blocked;
  total->strategy_reduce_cost_guard_bypassed +=
      delta.strategy_reduce_cost_guard_bypassed;
  total->strategy_reduce_pending_timeouts +=
      delta.strategy_reduce_pending_timeouts;
  total->strategy_reduce_cancel_submitted +=
      delta.strategy_reduce_cancel_submitted;
  total->strategy_reduce_cancel_ok += delta.strategy_reduce_cancel_ok;
  total->strategy_reduce_cancel_failed += delta.strategy_reduce_cancel_failed;
  total->strategy_reduce_reprices += delta.strategy_reduce_reprices;
  total->strategy_reduce_taker_fallbacks +=
      delta.strategy_reduce_taker_fallbacks;
  total->strategy_reduce_lifecycle_aborted +=
      delta.strategy_reduce_lifecycle_aborted;
  total->reduce_without_position_blocked +=
      delta.reduce_without_position_blocked;
  total->reduce_qty_capped_to_position +=
      delta.reduce_qty_capped_to_position;
  total->intents_throttled += delta.intents_throttled;
  total->intents_enqueued += delta.intents_enqueued;
  total->candidate_probe_signals += delta.candidate_probe_signals;
  total->candidate_probe_strong_signals +=
      delta.candidate_probe_strong_signals;
  total->candidate_probe_intents += delta.candidate_probe_intents;
  total->candidate_probe_cost_cooldown_bypass +=
      delta.candidate_probe_cost_cooldown_bypass;
  total->candidate_probe_fee_overrides += delta.candidate_probe_fee_overrides;
  total->candidate_probe_filtered_fee += delta.candidate_probe_filtered_fee;
  total->candidate_probe_enqueued += delta.candidate_probe_enqueued;
  total->candidate_probe_fills += delta.candidate_probe_fills;
  total->candidate_probe_pending_timeouts +=
      delta.candidate_probe_pending_timeouts;
  total->candidate_probe_cancel_submitted +=
      delta.candidate_probe_cancel_submitted;
  total->candidate_probe_cancel_ok += delta.candidate_probe_cancel_ok;
  total->candidate_probe_cancel_failed += delta.candidate_probe_cancel_failed;
  total->candidate_probe_reprices += delta.candidate_probe_reprices;
  total->candidate_probe_taker_fallbacks +=
      delta.candidate_probe_taker_fallbacks;
  total->candidate_probe_expired_without_fill +=
      delta.candidate_probe_expired_without_fill;
  total->candidate_probe_skipped_trade_not_ok +=
      delta.candidate_probe_skipped_trade_not_ok;
  total->candidate_probe_skipped_existing_intent +=
      delta.candidate_probe_skipped_existing_intent;
  total->candidate_probe_skipped_pending_orders +=
      delta.candidate_probe_skipped_pending_orders;
  total->candidate_probe_skipped_exposure +=
      delta.candidate_probe_skipped_exposure;
  total->candidate_probe_skipped_trend_ratio +=
      delta.candidate_probe_skipped_trend_ratio;
  total->candidate_probe_skipped_strong_trend_ratio +=
      delta.candidate_probe_skipped_strong_trend_ratio;
  total->candidate_probe_skipped_cooldown +=
      delta.candidate_probe_skipped_cooldown;
  total->candidate_probe_skipped_window_limit +=
      delta.candidate_probe_skipped_window_limit;
  total->candidate_probe_skipped_direction +=
      delta.candidate_probe_skipped_direction;
  total->candidate_probe_skipped_invalid_price +=
      delta.candidate_probe_skipped_invalid_price;
  total->candidate_probe_skipped_notional +=
      delta.candidate_probe_skipped_notional;
  total->candidate_probe_skipped_budget +=
      delta.candidate_probe_skipped_budget;
  total->candidate_probe_skipped_build_intent +=
      delta.candidate_probe_skipped_build_intent;
  total->async_submit_ok += delta.async_submit_ok;
  total->async_submit_failed += delta.async_submit_failed;
  total->fills_applied += delta.fills_applied;
  total->gate_alerts += delta.gate_alerts;
  total->self_evolution_updates += delta.self_evolution_updates;
  total->self_evolution_rollbacks += delta.self_evolution_rollbacks;
  total->self_evolution_skipped += delta.self_evolution_skipped;
  total->regime_trend_ticks += delta.regime_trend_ticks;
  total->regime_range_ticks += delta.regime_range_ticks;
  total->regime_extreme_ticks += delta.regime_extreme_ticks;
  total->regime_warmup_ticks += delta.regime_warmup_ticks;
  total->regime_trend_candidate_ticks += delta.regime_trend_candidate_ticks;
  total->regime_warmup_trend_candidate_ticks +=
      delta.regime_warmup_trend_candidate_ticks;
  total->integrator_scored += delta.integrator_scored;
  total->integrator_pred_up += delta.integrator_pred_up;
  total->integrator_pred_down += delta.integrator_pred_down;
  total->integrator_policy_proposed += delta.integrator_policy_proposed;
  total->integrator_policy_risk_accepted +=
      delta.integrator_policy_risk_accepted;
  total->integrator_policy_applied += delta.integrator_policy_applied;
  total->integrator_policy_canary += delta.integrator_policy_canary;
  total->integrator_policy_active += delta.integrator_policy_active;
  total->integrator_policy_filled += delta.integrator_policy_filled;
  total->entry_edge_samples += delta.entry_edge_samples;
  total->strategy_mix_samples += delta.strategy_mix_samples;
  total->strategy_policy_flat_samples += delta.strategy_policy_flat_samples;
  total->integrator_model_score_sum += delta.integrator_model_score_sum;
  total->integrator_p_up_sum += delta.integrator_p_up_sum;
  total->integrator_p_down_sum += delta.integrator_p_down_sum;
  total->entry_edge_bps_sum += delta.entry_edge_bps_sum;
  total->entry_base_required_edge_bps_sum +=
      delta.entry_base_required_edge_bps_sum;
  total->entry_required_edge_bps_sum += delta.entry_required_edge_bps_sum;
  total->entry_adaptive_relax_bps_sum += delta.entry_adaptive_relax_bps_sum;
  total->entry_maker_relax_bps_sum += delta.entry_maker_relax_bps_sum;
  total->entry_regime_adjust_bps_sum += delta.entry_regime_adjust_bps_sum;
  total->entry_volatility_adjust_bps_sum +=
      delta.entry_volatility_adjust_bps_sum;
  total->entry_liquidity_adjust_bps_sum +=
      delta.entry_liquidity_adjust_bps_sum;
  total->entry_concentration_adjust_bps_sum +=
      delta.entry_concentration_adjust_bps_sum;
  total->entry_quality_guard_penalty_bps_sum +=
      delta.entry_quality_guard_penalty_bps_sum;
  total->entry_edge_gap_bps_sum += delta.entry_edge_gap_bps_sum;
  total->candidate_probe_cost_gate_samples +=
      delta.candidate_probe_cost_gate_samples;
  total->candidate_probe_cost_gate_long_count +=
      delta.candidate_probe_cost_gate_long_count;
  total->candidate_probe_cost_gate_short_count +=
      delta.candidate_probe_cost_gate_short_count;
  total->candidate_probe_cost_gate_expected_edge_bps_sum +=
      delta.candidate_probe_cost_gate_expected_edge_bps_sum;
  total->candidate_probe_cost_gate_required_edge_bps_sum +=
      delta.candidate_probe_cost_gate_required_edge_bps_sum;
  total->candidate_probe_cost_gate_edge_gap_bps_sum +=
      delta.candidate_probe_cost_gate_edge_gap_bps_sum;
  total->candidate_probe_cost_gate_edge_gap_bps_max =
      std::max(total->candidate_probe_cost_gate_edge_gap_bps_max,
               delta.candidate_probe_cost_gate_edge_gap_bps_max);
  total->candidate_probe_cost_gate_trend_ratio_sum +=
      delta.candidate_probe_cost_gate_trend_ratio_sum;
  total->rebalance_gap_abs_usd_sum += delta.rebalance_gap_abs_usd_sum;
  total->rebalance_gap_abs_usd_max =
      std::max(total->rebalance_gap_abs_usd_max, delta.rebalance_gap_abs_usd_max);
  total->rebalance_gap_within_min_notional_abs_usd_sum +=
      delta.rebalance_gap_within_min_notional_abs_usd_sum;
  total->candidate_probe_notional_abs_usd_sum +=
      delta.candidate_probe_notional_abs_usd_sum;
  total->trend_notional_abs_sum += delta.trend_notional_abs_sum;
  total->defensive_notional_abs_sum += delta.defensive_notional_abs_sum;
  total->blended_notional_abs_sum += delta.blended_notional_abs_sum;
  total->fills_notional_abs_usd_sum += delta.fills_notional_abs_usd_sum;
  total->fills_maker_count += delta.fills_maker_count;
  total->fills_taker_count += delta.fills_taker_count;
  total->fills_unknown_liquidity_count += delta.fills_unknown_liquidity_count;
  total->fills_explicit_liquidity_count += delta.fills_explicit_liquidity_count;
  total->fills_fee_sign_fallback_count += delta.fills_fee_sign_fallback_count;
  total->fills_maker_fee_usd_sum += delta.fills_maker_fee_usd_sum;
  total->fills_taker_fee_usd_sum += delta.fills_taker_fee_usd_sum;
  total->fills_maker_notional_abs_usd_sum += delta.fills_maker_notional_abs_usd_sum;
  total->fills_taker_notional_abs_usd_sum += delta.fills_taker_notional_abs_usd_sum;
  total->entry_fills_applied += delta.entry_fills_applied;
  total->entry_fills_notional_abs_usd_sum += delta.entry_fills_notional_abs_usd_sum;
  total->entry_fills_maker_count += delta.entry_fills_maker_count;
  total->entry_fills_taker_count += delta.entry_fills_taker_count;
  total->entry_fills_unknown_liquidity_count +=
      delta.entry_fills_unknown_liquidity_count;
  total->entry_fills_explicit_liquidity_count +=
      delta.entry_fills_explicit_liquidity_count;
  total->entry_fills_fee_sign_fallback_count +=
      delta.entry_fills_fee_sign_fallback_count;
  total->entry_fills_maker_fee_usd_sum += delta.entry_fills_maker_fee_usd_sum;
  total->entry_fills_taker_fee_usd_sum += delta.entry_fills_taker_fee_usd_sum;
  total->entry_fills_maker_notional_abs_usd_sum +=
      delta.entry_fills_maker_notional_abs_usd_sum;
  total->entry_fills_taker_notional_abs_usd_sum +=
      delta.entry_fills_taker_notional_abs_usd_sum;
}

bool BotApplication::IsForceReduceOnlyActive() const {
  return protection_forced_reduce_only_ ||
         evidence_persistence_failed_ ||
         startup_protection_recovery_pending_ ||
         gate_forced_reduce_only_ || reconcile_forced_reduce_only_;
}

void BotApplication::RefreshReduceOnlyMode() {
  system_.ForceReduceOnly(IsForceReduceOnlyActive());
}

void BotApplication::RefreshProtectionReduceOnlyRelease(
    const std::string& reason) {
  if (!protection_forced_reduce_only_) {
    return;
  }

  const double gross_notional = system_.account().gross_notional_usd();
  const auto pending_net_order_ids = oms_.PendingNetPositionOrderIds();
  if (HasExposure(gross_notional) || !managed_protection_by_symbol_.empty() ||
      !pending_required_sl_attach_.empty() ||
      !pending_net_order_enqueued_ms_.empty() ||
      !pending_net_order_ids.empty()) {
    return;
  }

  protection_forced_reduce_only_ = false;
  RefreshReduceOnlyMode();
  LogInfo("PROTECTION_FORCE_REDUCE_ONLY_RELEASED: reason=" + reason +
          ", account_flat=true" +
          ", gross_notional_usd=" + std::to_string(gross_notional) +
          ", managed_protections=0" +
          ", pending_required_sl=0" +
          ", pending_net_orders=0");
}

void BotApplication::RefreshTradingHaltState() {
  trading_halted_ = reconcile_halted_ || gate_halted_;
}

void BotApplication::TickGateRuntimeCooldown() {
  if (gate_reduce_only_cooldown_ticks_left_ > 0) {
    --gate_reduce_only_cooldown_ticks_left_;
  }
  if (gate_halt_cooldown_ticks_left_ > 0) {
    --gate_halt_cooldown_ticks_left_;
  }
}

/**
 * @brief 应用主入口
 *
 * 执行顺序：Initialize -> RunLoop -> Shutdown。
 */
int BotApplication::Run() {
  LogInfo("PROCESS_START: boot_id=" + boot_id_ + ", startup_utc=" +
          startup_utc_ + ", primary_symbol=" + config_.primary_symbol);
  if (!Initialize()) {
    return 1;
  }
  RunLoop();
  Shutdown();
  return replay_terminal_settlement_failed_ ? 1 : 0;
}

int BotApplication::CheckStartup() {
  LogInfo("STARTUP_CHECK_START: boot_id=" + boot_id_ + ", startup_utc=" +
          startup_utc_ + ", primary_symbol=" + config_.primary_symbol);
  if (!Initialize()) {
    LogError("STARTUP_CHECK_FAILED");
    return 1;
  }
  Shutdown();
  LogInfo("STARTUP_CHECK_PASSED");
  return 0;
}

int BotApplication::CheckExchange() {
  LogInfo("EXCHANGE_CHECK_START: boot_id=" + boot_id_ +
          ", startup_utc=" + startup_utc_ +
          ", primary_symbol=" + config_.primary_symbol);
  adapter_ = CreateAdapter(config_);
  if (!adapter_ || !adapter_->Connect()) {
    LogError("EXCHANGE_CHECK_FAILED: stage=connect");
    adapter_.reset();
    return 1;
  }
  if (!ValidateAccountSnapshot(config_, adapter_.get())) {
    LogError("EXCHANGE_CHECK_FAILED: stage=account_mode");
    adapter_.reset();
    return 1;
  }

  std::vector<RemotePositionSnapshot> positions;
  if (!adapter_->GetRemotePositions(&positions)) {
    LogError("EXCHANGE_CHECK_FAILED: stage=positions");
    adapter_.reset();
    return 1;
  }
  std::vector<RemoteOpenOrderSnapshot> open_orders;
  if (!adapter_->GetRemoteOpenOrders(&open_orders)) {
    LogError("EXCHANGE_CHECK_FAILED: stage=open_orders");
    adapter_.reset();
    return 1;
  }
  RemoteAccountBalanceSnapshot balance;
  if (!adapter_->GetRemoteAccountBalance(&balance)) {
    LogError("EXCHANGE_CHECK_FAILED: stage=account_balance");
    adapter_.reset();
    return 1;
  }
  if (!adapter_->TradeOk()) {
    LogError("EXCHANGE_CHECK_FAILED: stage=trade_channel");
    adapter_.reset();
    return 1;
  }

  adapter_.reset();
  LogInfo("EXCHANGE_CHECK_PASSED");
  return 0;
}

/**
 * @brief 系统初始化
 *
 * 关键顺序（不可随意调整）：
 * 1. 初始化并恢复 WAL（先恢复状态再接入交易所）；
 * 2. 建立交易所连接并做账户门禁；
 * 3. 启动异步执行线程；
 * 4. 初始化 Universe 并同步远端持仓。
 */
bool BotApplication::Initialize() {
  std::string wal_error;
  if (!wal_.Initialize(&wal_error)) {
    LogError("WAL 初始化失败: " + wal_error);
    return false;
  }

  if (config_.mode == "replay") {
    LogInfo("replay 模式：跳过历史 WAL 恢复");
  } else {
    std::vector<FillEvent> historical_fills;
    WalLoadRecoveryStats wal_recovery_stats;
    if (!wal_.LoadState(&intent_ids_,
                        &fill_ids_,
                        &historical_fills,
                        &wal_error,
                        &persisted_intent_by_id_,
                        &persisted_closed_episode_ids_,
                        &persisted_episode_closures_,
                        &latest_account_equity_checkpoint_,
                        &wal_recovery_stats)) {
      LogError("WAL 加载失败: " + wal_error);
      return false;
    }
    if (wal_recovery_stats.skipped_nontrading_checkpoint_records > 0) {
      LogInfo(
          "WAL_NONTRADING_CHECKPOINT_RECOVERY: skipped_records=" +
          std::to_string(
              wal_recovery_stats.skipped_nontrading_checkpoint_records) +
          ", execution_records_skipped=0");
    }
    for (const auto& [episode_id, closure] :
         persisted_episode_closures_) {
      LogInfo(
          "INTEGRATOR_POLICY_EPISODE_RECOVERED: position_episode_id=" +
          episode_id +
          ", decision_id=" + closure.decision_id +
          ", candidate_id=" + closure.candidate_id +
          ", model_version=" + closure.model_version +
          ", mode=" + closure.mode +
          ", policy_reason=" + closure.policy_reason +
          ", symbol=" + closure.symbol +
          ", realized_net_usd=" +
          std::to_string(closure.realized_net_usd) +
          ", funding_paid_usd=" +
          std::to_string(closure.funding_paid_usd) +
          ", fill_event_count=" +
          std::to_string(closure.fill_event_count) +
          ", unique_order_count=" +
          std::to_string(closure.unique_order_count) +
          ", evidence_complete=" +
          std::string(closure.evidence_complete ? "true" : "false") +
          ", activation_transaction_id=" +
          closure.activation_transaction_id +
          ", evidence_boot_id=" + closure.boot_id +
          ", runtime_config_sha256=" + closure.runtime_config_sha256 +
          ", trade_bot_sha256=" + closure.trade_bot_sha256 +
          ", closed_at_utc=" + closure.closed_at_utc +
          ", recovered_after_restart=true");
    }
    for (const auto& [intent_id, intent] : persisted_intent_by_id_) {
      if (!oms_.RegisterIntent(intent)) {
        LogError("WAL OMS 恢复失败: client_order_id=" + intent_id);
        return false;
      }
      if (intent.candidate_id.empty()) {
        continue;
      }
      integrator_lineage_by_intent_id_[intent_id] =
          IntegratorCandidateLineage{
              .decision_id = intent.decision_id,
              .candidate_id = intent.candidate_id,
              .model_version = intent.model_version,
              .mode = intent.integrator_mode,
              .policy_reason = intent.integrator_policy_reason,
              .position_episode_id = intent.position_episode_id,
          };
    }
    // 回放成交恢复仓位、权益及尚未闭合的候选 episode。
    for (const auto& fill : historical_fills) {
      const double position_qty_before =
          system_.account().position_qty(fill.symbol);
      oms_.OnFill(fill);
      system_.OnFill(fill);
      const auto intent_it =
          persisted_intent_by_id_.find(fill.client_order_id);
      if (intent_it == persisted_intent_by_id_.end() ||
          intent_it->second.candidate_id.empty() ||
          intent_it->second.position_episode_id.empty()) {
        continue;
      }
      const OrderIntent& intent = intent_it->second;
      auto episode_it = integrator_episode_by_symbol_.find(fill.symbol);
      const bool entry_fill =
          intent.purpose == OrderPurpose::kEntry && !intent.reduce_only;
      if (entry_fill &&
          (episode_it == integrator_episode_by_symbol_.end() ||
           episode_it->second.lineage.position_episode_id !=
               intent.position_episode_id)) {
        IntegratorCandidateEpisode episode;
        episode.lineage = IntegratorCandidateLineage{
            .decision_id = intent.decision_id,
            .candidate_id = intent.candidate_id,
            .model_version = intent.model_version,
            .mode = intent.integrator_mode,
            .policy_reason = intent.integrator_policy_reason,
            .position_episode_id = intent.position_episode_id,
        };
        episode.entry_observed_from_flat =
            std::fabs(position_qty_before) <= kNotionalEpsilon;
        episode_it =
            integrator_episode_by_symbol_
                .insert_or_assign(fill.symbol, std::move(episode))
                .first;
      }
      if (episode_it == integrator_episode_by_symbol_.end() ||
          episode_it->second.lineage.position_episode_id !=
              intent.position_episode_id) {
        continue;
      }
      IntegratorCandidateEpisode& episode = episode_it->second;
      const double episode_qty_before = episode.signed_open_qty;
      ApplyCandidateEpisodeFill(&episode, fill);
      if (std::fabs(episode_qty_before) > kNotionalEpsilon &&
          std::fabs(episode.signed_open_qty) <= kNotionalEpsilon) {
        if (persisted_closed_episode_ids_.find(
                episode.lineage.position_episode_id) ==
            persisted_closed_episode_ids_.end()) {
          if (!RecordCandidateEpisodeClosure(fill.symbol, episode, true)) {
            return false;
          }
        }
        integrator_episode_by_symbol_.erase(episode_it);
      }
    }
    LogInfo("WAL 恢复完成: intents=" + std::to_string(intent_ids_.size()) +
            ", fills=" + std::to_string(fill_ids_.size()) +
            ", candidate_open_episodes=" +
            std::to_string(integrator_episode_by_symbol_.size()));
  }

  adapter_ = CreateAdapter(config_);
  if (!adapter_->Connect()) {
    LogError("交易所连接失败");
    return false;
  }
  LogInfo("适配器已连接: " + adapter_->Name());

  if (!ValidateAccountSnapshot(config_, adapter_.get())) {
    LogError("账户模式校验失败");
    return false;
  }

  // 执行通道单线程串行化，避免并发提交导致状态竞态。
  executor_ = std::make_unique<AsyncExecutor>(adapter_.get());
  executor_->Start();

  InitializeUniverse();
  if (!SyncRemotePositions()) {
    return false;
  }
  if (!RecoverStartupOrdersAndProtection()) {
    return false;
  }

  if (config_.integrator.enabled &&
      system_.integrator_mode() != IntegratorMode::kOff &&
      config_.integrator.shadow.enabled) {
    std::string shadow_error;
    if (system_.InitializeIntegratorShadow(&shadow_error)) {
      bool history_ready = true;
      if (config_.mode != "replay") {
        history_ready = system_.BootstrapIntegratorHistory(&shadow_error);
      }
      if (history_ready) {
        LogInfo("INTEGRATOR_INIT: mode=" +
                std::string(ToString(system_.integrator_mode())) +
                ", model_version=" +
                system_.integrator_shadow_model_version() +
                ", training_symbol=" +
                system_.integrator_training_symbol() +
                ", bar_interval_ms=" +
                std::to_string(system_.integrator_feature_bar_interval_ms()) +
                ", feature_samples=" +
                std::to_string(system_.integrator_feature_sample_count()));
      } else {
        LogInfo("INTEGRATOR_DEGRADED: " + shadow_error);
        if (system_.integrator_mode() == IntegratorMode::kCanary ||
            system_.integrator_mode() == IntegratorMode::kActive) {
          if (config_.integrator.shadow.strict_failure_degrade_to_off &&
              !config_.integrator.microstructure_demo.enabled) {
            system_.SetIntegratorMode(IntegratorMode::kOff);
            config_.integrator.mode = IntegratorMode::kOff;
            LogInfo(
                "INTEGRATOR_SAFE_OFF: strict history bootstrap failed; "
                "baseline runtime remains active and model orders are disabled");
          } else if (!config_.integrator.microstructure_demo.enabled) {
            LogError(
                "INTEGRATOR_STARTUP_BLOCKED: strict mode history bootstrap failed");
            return false;
          } else {
            LogInfo(
                "ALPHA_SOURCE_ROUTER_ARMED: legacy OHLCV source unavailable; "
                "microstructure demo source remains fail-closed until demo_ready");
          }
        }
      }
    } else {
      LogInfo("INTEGRATOR_DEGRADED: " + shadow_error);
      if (system_.integrator_mode() == IntegratorMode::kCanary ||
          system_.integrator_mode() == IntegratorMode::kActive) {
        if (config_.integrator.shadow.strict_failure_degrade_to_off &&
            !config_.integrator.microstructure_demo.enabled) {
          system_.SetIntegratorMode(IntegratorMode::kOff);
          config_.integrator.mode = IntegratorMode::kOff;
          LogInfo(
              "INTEGRATOR_SAFE_OFF: strict identity/model initialization "
              "failed; baseline runtime remains active and model orders are "
              "disabled");
        } else if (!config_.integrator.microstructure_demo.enabled) {
          LogError(
              "INTEGRATOR_STARTUP_BLOCKED: strict mode identity/model init failed");
          return false;
        } else {
          LogInfo(
              "ALPHA_SOURCE_ROUTER_ARMED: legacy OHLCV source unavailable; "
              "microstructure demo source remains fail-closed until demo_ready");
        }
      }
    }
  }

  // 自进化初始化必须在账户同步后进行，确保首个评估窗口的权益/已实现净盈亏基线准确。
  if (config_.self_evolution.enabled) {
    system_.EnableEvolution(true);
    std::string error;
    if (!system_.SetEvolutionWeights(config_.self_evolution.initial_trend_weight,
                                     config_.self_evolution.initial_defensive_weight,
                                     &error)) {
      LogError("自进化初始权重设置失败: " + error);
      return false;
    }
    if (!self_evolution_.Initialize(
            /*current_tick=*/0,
            system_.account().equity_usd(),
            {config_.self_evolution.initial_trend_weight,
             config_.self_evolution.initial_defensive_weight},
            &error,
            system_.account().cumulative_realized_net_pnl_usd())) {
      LogError("自进化控制器初始化失败: " + error);
      return false;
    }
    bool restored = false;
    std::array<EvolutionWeights, 3> restored_weights{};
    if (config_.mode != "replay") {
      std::string restore_error;
      if (!LoadSelfEvolutionWeights(
              &restored_weights, &restored, &restore_error)) {
        LogError("SELF_EVOLUTION_STATE_RESTORE_FAILED: boot_id=" + boot_id_ +
                 ", reason=" + restore_error);
        return false;
      }
      if (restored) {
        if (!self_evolution_.RestoreCurrentWeights(restored_weights, &error)) {
          LogError("SELF_EVOLUTION_STATE_RESTORE_FAILED: boot_id=" + boot_id_ +
                   ", reason=" + error);
          return false;
        }
        constexpr std::array<RegimeBucket, 3> kBuckets{
            RegimeBucket::kTrend,
            RegimeBucket::kRange,
            RegimeBucket::kExtreme,
        };
        for (std::size_t index = 0; index < kBuckets.size(); ++index) {
          if (!system_.SetEvolutionWeightsForBucket(
                  kBuckets[index],
                  restored_weights[index].trend_weight,
                  restored_weights[index].defensive_weight,
                  &error)) {
            LogError("SELF_EVOLUTION_STATE_RESTORE_FAILED: boot_id=" + boot_id_ +
                     ", reason=" + error);
            return false;
          }
        }
        LogInfo("SELF_EVOLUTION_STATE_RESTORED: boot_id=" + boot_id_ +
                ", path=" + config_.data_path +
                "/self_evolution_weights_v1.tsv, policy_fingerprint=" +
                SelfEvolutionPolicyFingerprint());
      } else if (!restore_error.empty()) {
        LogInfo("SELF_EVOLUTION_STATE_IGNORED: boot_id=" + boot_id_ +
                ", reason=" + restore_error + ", policy_fingerprint=" +
                SelfEvolutionPolicyFingerprint());
      }
      if (!PersistSelfEvolutionWeights(&error)) {
        LogError("SELF_EVOLUTION_STATE_PERSIST_FAILED: boot_id=" + boot_id_ +
                 ", reason=" + error);
        return false;
      }
    }
    const auto active_weights =
        self_evolution_.current_weights(RegimeBucket::kRange);
    LogInfo("SELF_EVOLUTION_INIT: trend_weight=" +
            std::to_string(active_weights.trend_weight) +
            ", defensive_weight=" +
            std::to_string(active_weights.defensive_weight) +
            ", restored=" + std::string(restored ? "true" : "false") +
            ", update_interval_ticks=" +
            std::to_string(config_.self_evolution.update_interval_ticks) +
            ", factor_ic_weighting=" +
            std::string(config_.self_evolution.enable_factor_ic_adaptive_weights
                            ? "true"
                            : "false") +
            ", learnability_gate=" +
            std::string(config_.self_evolution.enable_learnability_gate ? "true"
                                                                        : "false"));
  } else {
    system_.EnableEvolution(false);
  }

  return true;
}

/**
 * @brief 初始化 Universe 候选池
 *
 * 先按交易所 symbol 规则做可交易性过滤：
 * - 必须可交易；
 * - 必须有有效数量步长（qty_step > 0）。
 */
void BotApplication::InitializeUniverse() {
  std::vector<std::string> candidates = config_.universe.candidate_symbols;
  candidates.insert(candidates.end(), config_.universe.fallback_symbols.begin(),
                    config_.universe.fallback_symbols.end());
  candidates.push_back(config_.primary_symbol);
  candidates = UniqueSymbols(candidates);

  std::vector<std::string> allowed;
  for (const auto& symbol : candidates) {
    SymbolInfo info;
    // 过滤掉不可交易或规则异常的币对
    if (!adapter_->GetSymbolInfo(symbol, &info)) continue;
    if (!info.tradable || info.qty_step <= 0.0) continue;
    allowed.push_back(info.symbol);
  }

  if (!allowed.empty()) {
    universe_selector_.SetAllowedSymbols(allowed);
    tracked_symbols_ = allowed;
  } else {
    tracked_symbols_ = candidates;
    LogInfo("警告: 未获取到有效交易规则，使用原始候选列表");
  }
}

/**
 * @brief 启动时同步远端持仓
 *
 * 设计取舍：
 * - 非 replay 模式优先以交易所快照重建本地视图；
 * - 失败时继续运行，但明确记录“状态可能不准确”。
 */
bool BotApplication::SyncRemotePositions() {
  startup_remote_positions_.clear();
  startup_position_lineage_mismatches_.clear();
  if (config_.mode == "replay") return true;

  std::vector<RemotePositionSnapshot> remote_positions;
  bool position_sync_ok = false;
  if (adapter_->GetRemotePositions(&remote_positions)) {
    system_.SyncAccountFromRemotePositions(remote_positions);
    startup_remote_positions_ = remote_positions;
    if (!persisted_intent_by_id_.empty()) {
      std::unordered_map<std::string, double> remote_qty_by_symbol;
      std::unordered_set<std::string> symbols;
      for (const auto& remote : remote_positions) {
        remote_qty_by_symbol[remote.symbol] = remote.qty;
        symbols.insert(remote.symbol);
      }
      for (const auto& [_, intent] : persisted_intent_by_id_) {
        symbols.insert(intent.symbol);
      }
      for (const auto& symbol : symbols) {
        const double delta =
            remote_qty_by_symbol[symbol] - oms_.net_filled_qty(symbol);
        if (std::fabs(delta) <= kNotionalEpsilon) {
          continue;
        }
        startup_position_lineage_mismatches_.push_back(
            StartupPositionLineageMismatch{
                .symbol = symbol,
                .remote_qty = remote_qty_by_symbol[symbol],
                .wal_oms_qty = oms_.net_filled_qty(symbol),
                .delta_qty = delta,
            });
        LogInfo(
            "STARTUP_POSITION_LINEAGE_REBASE_PENDING: symbol=" + symbol +
            ", remote_qty=" + std::to_string(remote_qty_by_symbol[symbol]) +
            ", wal_oms_qty=" + std::to_string(oms_.net_filled_qty(symbol)) +
            ", delta_qty=" + std::to_string(delta));
      }
    }
    // 远端持仓是启动时唯一可执行仓位基线。WAL 差异需等活动订单也确认后，
    // 才能判定为空仓检查点或升级为硬失败。
    oms_.SeedNetPositionBaseline(remote_positions);
    RefreshReduceOnlyMode();
    position_sync_ok = true;
    LogInfo("远端持仓同步完成: count=" + std::to_string(remote_positions.size()));
  } else {
    LogError("无法同步远端持仓，拒绝开放交易");
    return false;
  }

  RemoteAccountBalanceSnapshot balance;
  if (adapter_->GetRemoteAccountBalance(&balance)) {
    // 启动阶段重置回撤峰值基线到远端权益，避免固定初始值引入伪回撤。
    system_.SyncAccountFromRemoteBalance(balance, /*reset_peak_to_equity=*/true);
    LogAccountSyncSnapshot("startup", balance, system_.account());
    if (!PersistRemoteAccountCheckpoint(
            "startup", balance, /*evaluate_cross_boot_continuity=*/true)) {
      return false;
    }
    if (balance.has_equity) {
      LogInfo("远端资金同步完成: equity=" + std::to_string(balance.equity_usd));
    } else if (balance.has_wallet_balance) {
      LogInfo("远端资金同步完成: wallet=" +
              std::to_string(balance.wallet_balance_usd));
    }
  } else if (position_sync_ok) {
    LogInfo("警告: 远端持仓已同步，但远端资金读取失败，回撤口径可能存在偏差");
  }
  return position_sync_ok;
}

bool BotApplication::PersistRemoteAccountCheckpoint(
    const std::string& stage,
    const RemoteAccountBalanceSnapshot& balance,
    bool evaluate_cross_boot_continuity) {
  AccountEquityCheckpointRecord checkpoint;
  checkpoint.boot_id = boot_id_;
  checkpoint.captured_at_utc = CurrentUtcIsoTimestamp();
  checkpoint.stage = stage;
  checkpoint.equity_usd = balance.equity_usd;
  checkpoint.wallet_balance_usd = balance.wallet_balance_usd;
  checkpoint.unrealized_pnl_usd = balance.unrealized_pnl_usd;
  checkpoint.has_equity = balance.has_equity;
  checkpoint.has_wallet_balance = balance.has_wallet_balance;
  checkpoint.has_unrealized_pnl = balance.has_unrealized_pnl;
  checkpoint.positions_flat = system_.account().GetActiveSymbols().empty();
  checkpoint.persisted_fill_count = fill_ids_.size();

  if (evaluate_cross_boot_continuity) {
    std::string status = "BASELINE_CREATED";
    std::string basis = "none";
    bool comparable = false;
    double previous_value = 0.0;
    double current_value = 0.0;
    double delta_usd = 0.0;
    std::string previous_boot_id = "none";
    std::string previous_captured_at_utc = "none";
    bool previous_positions_flat = false;
    std::uint64_t previous_fill_count = 0;
    if (latest_account_equity_checkpoint_.has_value()) {
      const auto& previous = *latest_account_equity_checkpoint_;
      previous_boot_id = previous.boot_id;
      previous_captured_at_utc = previous.captured_at_utc;
      previous_positions_flat = previous.positions_flat;
      previous_fill_count = previous.persisted_fill_count;
      if (previous.has_equity && checkpoint.has_equity) {
        basis = "equity";
        previous_value = previous.equity_usd;
        current_value = checkpoint.equity_usd;
        comparable = true;
      } else if (previous.has_wallet_balance &&
                 checkpoint.has_wallet_balance) {
        basis = "wallet";
        previous_value = previous.wallet_balance_usd;
        current_value = checkpoint.wallet_balance_usd;
        comparable = true;
      }
      if (!comparable) {
        status = "INSUFFICIENT_BALANCE_BASIS";
      } else {
        delta_usd = current_value - previous_value;
        constexpr double kContinuityToleranceUsd = 0.01;
        if (std::fabs(delta_usd) <= kContinuityToleranceUsd) {
          status = "MATCHED";
        } else if (checkpoint.positions_flat && previous.positions_flat &&
                   checkpoint.persisted_fill_count ==
                       previous.persisted_fill_count) {
          status = "UNATTRIBUTED_EXTERNAL_DELTA";
        } else if (checkpoint.persisted_fill_count >
                   previous.persisted_fill_count) {
          status = "WAL_ACTIVITY_PRESENT";
        } else {
          status = "UNATTRIBUTED_EVIDENCE_GAP";
        }
      }
    }
    LogInfo(
        "ACCOUNT_EQUITY_CONTINUITY: status=" + status +
        ", basis=" + basis +
        ", comparable=" + std::string(comparable ? "true" : "false") +
        ", previous_boot_id=" + previous_boot_id +
        ", current_boot_id=" + boot_id_ +
        ", previous_captured_at_utc=" + previous_captured_at_utc +
        ", current_captured_at_utc=" + checkpoint.captured_at_utc +
        ", previous_value_usd=" + std::to_string(previous_value) +
        ", current_value_usd=" + std::to_string(current_value) +
        ", delta_usd=" + std::to_string(delta_usd) +
        ", previous_fill_count=" + std::to_string(previous_fill_count) +
        ", current_fill_count=" +
        std::to_string(checkpoint.persisted_fill_count) +
        ", previous_positions_flat=" +
        std::string(previous_positions_flat ? "true" : "false") +
        ", current_positions_flat=" +
        std::string(checkpoint.positions_flat ? "true" : "false"));
  }

  std::string wal_error;
  if (!wal_.AppendAccountEquityCheckpoint(checkpoint, &wal_error)) {
    evidence_persistence_failed_ = true;
    RefreshReduceOnlyMode();
    LogError("CRITICAL: ACCOUNT_EQUITY_CHECKPOINT_FAILED: stage=" + stage +
             ", error=" + wal_error);
    return false;
  }
  latest_account_equity_checkpoint_ = checkpoint;
  LogInfo("ACCOUNT_EQUITY_CHECKPOINT_PERSISTED: stage=" + stage +
          ", boot_id=" + boot_id_ +
          ", captured_at_utc=" + checkpoint.captured_at_utc +
          ", fill_count=" +
          std::to_string(checkpoint.persisted_fill_count) +
          ", positions_flat=" +
          std::string(checkpoint.positions_flat ? "true" : "false"));
  return true;
}

bool BotApplication::RecoverStartupOrdersAndProtection() {
  if (config_.mode == "replay") {
    return true;
  }

  std::vector<RemoteOpenOrderSnapshot> remote_open_orders;
  if (!adapter_->GetRemoteOpenOrders(&remote_open_orders)) {
    LogError("STARTUP_ORDER_RECOVERY_FAILED: remote_open_orders_unavailable");
    return false;
  }
  std::unordered_set<std::string> remote_open_order_ids;
  std::unordered_map<std::string, const RemoteOpenOrderSnapshot*>
      remote_open_order_by_id;
  for (const auto& order : remote_open_orders) {
    if (order.client_order_id.empty()) {
      LogError("STARTUP_ORDER_RECOVERY_FAILED: remote_order_missing_client_id");
      return false;
    }
    remote_open_order_ids.insert(order.client_order_id);
    remote_open_order_by_id[order.client_order_id] = &order;
  }

  const bool remote_positions_flat =
      std::none_of(startup_remote_positions_.begin(),
                   startup_remote_positions_.end(),
                   [](const RemotePositionSnapshot& position) {
                     return std::fabs(position.qty) > kNotionalEpsilon;
                   });
  std::vector<std::string> recovered_net_orders_absent_on_remote;
  for (const auto& client_order_id : oms_.PendingNetPositionOrderIds()) {
    if (remote_open_order_ids.find(client_order_id) ==
        remote_open_order_ids.end()) {
      recovered_net_orders_absent_on_remote.push_back(client_order_id);
    }
  }

  if (!startup_position_lineage_mismatches_.empty() ||
      !recovered_net_orders_absent_on_remote.empty()) {
    if (remote_positions_flat && remote_open_orders.empty()) {
      std::string wal_error;
      if (!wal_.AppendFlatPositionRebase(
              boot_id_, CurrentUtcIsoTimestamp(), &wal_error)) {
        evidence_persistence_failed_ = true;
        RefreshReduceOnlyMode();
        LogError(
            "CRITICAL: STARTUP_POSITION_REBASE_FAILED: " + wal_error);
        return false;
      }
      const std::size_t mismatch_count =
          startup_position_lineage_mismatches_.size();
      for (const auto& client_order_id :
           recovered_net_orders_absent_on_remote) {
        oms_.MarkCancelled(client_order_id);
        pending_net_order_enqueued_ms_.erase(client_order_id);
        integrator_lineage_by_intent_id_.erase(client_order_id);
      }
      integrator_episode_by_symbol_.clear();
      startup_position_lineage_mismatches_.clear();
      LogInfo(
          "STARTUP_POSITION_REBASE_COMMITTED: state=flat, "
          "remote_open_orders=0, mismatch_symbols=" +
          std::to_string(mismatch_count) +
          ", recovered_pending_net_orders=" +
          std::to_string(recovered_net_orders_absent_on_remote.size()) +
          ", boot_id=" + boot_id_);
    } else {
      for (const auto& mismatch : startup_position_lineage_mismatches_) {
        // 非空仓或仍有活动订单时不能宣告空仓检查点。隔离旧 candidate
        // episode，并继续走下方的撤单与必需 SL 恢复；保护恢复失败仍会阻断启动。
        integrator_episode_by_symbol_.erase(mismatch.symbol);
        LogInfo(
            "STARTUP_POSITION_LINEAGE_REBASE_DEFERRED: symbol=" +
            mismatch.symbol +
            ", remote_qty=" + std::to_string(mismatch.remote_qty) +
            ", wal_oms_qty=" + std::to_string(mismatch.wal_oms_qty) +
            ", delta_qty=" + std::to_string(mismatch.delta_qty) +
            ", remote_open_orders=" +
            std::to_string(remote_open_orders.size()) +
            ", action=order_and_protection_recovery");
      }
      startup_position_lineage_mismatches_.clear();
    }
  }

  for (const auto& [remote_id, _] : remote_open_order_by_id) {
    const auto intent_it = persisted_intent_by_id_.find(remote_id);
    if (intent_it == persisted_intent_by_id_.end()) {
      if (!adapter_->CancelOrder(remote_id)) {
        LogError("STARTUP_ORDER_RECOVERY_FAILED: unknown_remote_order_cancel_failed"
                 ", client_order_id=" + remote_id);
        return false;
      }
      LogInfo("STARTUP_UNKNOWN_ORDER_CANCELLED: client_order_id=" + remote_id);
      continue;
    }
    const OrderIntent& intent = intent_it->second;
    if (intent.purpose == OrderPurpose::kEntry ||
        intent.purpose == OrderPurpose::kReduce) {
      if (!adapter_->CancelOrder(remote_id)) {
        LogError("STARTUP_ORDER_RECOVERY_FAILED: net_order_cancel_failed"
                 ", client_order_id=" + remote_id);
        return false;
      }
      oms_.MarkCancelled(remote_id);
      LogInfo("STARTUP_NET_ORDER_CANCELLED: client_order_id=" + remote_id +
              ", symbol=" + intent.symbol);
      continue;
    }
    oms_.MarkSent(remote_id);
  }

  if (!config_.protection.enabled || !config_.protection.require_sl) {
    return true;
  }

  for (const auto& remote : startup_remote_positions_) {
    if (remote.symbol.empty() ||
        std::fabs(remote.qty) <= kNotionalEpsilon) {
      continue;
    }
    const int position_direction = SignOf(remote.qty);
    const double position_qty = std::fabs(remote.qty);
    SymbolInfo symbol_info;
    const bool has_symbol_info =
        adapter_->GetSymbolInfo(remote.symbol, &symbol_info);
    const double qty_tolerance = std::max(
        kFillOverrunToleranceMinQty,
        has_symbol_info && symbol_info.qty_step > 0.0
            ? symbol_info.qty_step * 0.51
            : position_qty * 1e-6);
    const double price_tolerance =
        std::max(1e-8,
                 has_symbol_info && symbol_info.price_tick > 0.0
                     ? symbol_info.price_tick * 0.51
                     : std::fabs(remote.avg_entry_price) * 1e-8);
    const OrderIntent* recovered_sl = nullptr;
    const OrderIntent* recovered_tp = nullptr;
    for (const auto& remote_id : remote_open_order_ids) {
      const auto intent_it = persisted_intent_by_id_.find(remote_id);
      if (intent_it == persisted_intent_by_id_.end()) {
        continue;
      }
      const OrderIntent& intent = intent_it->second;
      if (intent.symbol != remote.symbol || !intent.reduce_only ||
          intent.direction != -position_direction) {
        continue;
      }
      if (std::fabs(intent.qty - position_qty) > qty_tolerance) {
        continue;
      }
      const auto snapshot_it = remote_open_order_by_id.find(remote_id);
      if (snapshot_it == remote_open_order_by_id.end() ||
          snapshot_it->second == nullptr) {
        continue;
      }
      const RemoteOpenOrderSnapshot& snapshot = *snapshot_it->second;
      const bool protection_intent =
          intent.purpose == OrderPurpose::kSl ||
          intent.purpose == OrderPurpose::kTp;
      const int expected_trigger_direction =
          intent.purpose == OrderPurpose::kSl
              ? (intent.direction < 0 ? 2 : 1)
              : (intent.direction < 0 ? 1 : 2);
      const bool snapshot_matches =
          protection_intent &&
          snapshot.symbol == intent.symbol &&
          snapshot.direction == intent.direction &&
          snapshot.reduce_only &&
          snapshot.close_on_trigger &&
          snapshot.original_qty > 0.0 &&
          std::fabs(snapshot.original_qty - position_qty) <= qty_tolerance &&
          snapshot.leaves_qty > 0.0 &&
          snapshot.trigger_price > 0.0 &&
          std::fabs(snapshot.trigger_price - intent.price) <=
              price_tolerance &&
          snapshot.trigger_direction == expected_trigger_direction;
      if (!snapshot_matches) {
        LogError(
            "STARTUP_PROTECTION_SNAPSHOT_MISMATCH: client_order_id=" +
            remote_id + ", symbol=" + intent.symbol +
            ", remote_symbol=" + snapshot.symbol +
            ", remote_direction=" +
            std::to_string(snapshot.direction) +
            ", remote_reduce_only=" +
            std::string(snapshot.reduce_only ? "true" : "false") +
            ", remote_close_on_trigger=" +
            std::string(snapshot.close_on_trigger ? "true" : "false") +
            ", remote_original_qty=" +
            std::to_string(snapshot.original_qty) +
            ", remote_leaves_qty=" +
            std::to_string(snapshot.leaves_qty) +
            ", remote_trigger_price=" +
            std::to_string(snapshot.trigger_price) +
            ", expected_trigger_price=" +
            std::to_string(intent.price));
        if (!adapter_->CancelOrder(remote_id)) {
          LogError(
              "STARTUP_ORDER_RECOVERY_FAILED: invalid_protection_cancel_failed"
              ", client_order_id=" +
              remote_id);
          return false;
        }
        oms_.MarkCancelled(remote_id);
        continue;
      }
      if (intent.purpose == OrderPurpose::kSl && intent.price > 0.0) {
        recovered_sl = &intent;
      } else if (intent.purpose == OrderPurpose::kTp && intent.price > 0.0) {
        recovered_tp = &intent;
      }
    }

    if (recovered_sl != nullptr) {
      ManagedProtectionState state;
      state.symbol = remote.symbol;
      state.protection_group_id =
          recovered_sl->parent_order_id.empty()
              ? BuildProtectionGroupId(remote.symbol)
              : recovered_sl->parent_order_id;
      state.direction = position_direction;
      state.qty = position_qty;
      state.avg_entry_price = remote.avg_entry_price;
      state.best_price =
          remote.mark_price > 0.0 ? remote.mark_price : remote.avg_entry_price;
      state.stop_loss_ratio = config_.protection.stop_loss_ratio;
      state.take_profit_ratio = config_.protection.take_profit_ratio;
      state.active_sl_client_order_id = recovered_sl->client_order_id;
      state.active_sl_price = recovered_sl->price;
      if (recovered_tp != nullptr) {
        state.active_tp_client_order_id = recovered_tp->client_order_id;
        state.active_tp_price = recovered_tp->price;
      }
      managed_protection_by_symbol_[remote.symbol] = std::move(state);
      LogInfo("STARTUP_PROTECTION_RECOVERED: symbol=" + remote.symbol +
              ", sl_client_order_id=" + recovered_sl->client_order_id);
    }

    const bool complete_existing_protection =
        recovered_sl != nullptr &&
        (!config_.protection.enable_tp || recovered_tp != nullptr);
    if (complete_existing_protection) {
      continue;
    }

    startup_protection_recovery_pending_ = true;
    RefreshReduceOnlyMode();
    RefreshManagedProtectionForSymbol(
        remote.symbol,
        remote.mark_price > 0.0 ? remote.mark_price : remote.avg_entry_price,
        "startup_remote_position_recovery");
    const auto state_it = managed_protection_by_symbol_.find(remote.symbol);
    if (state_it == managed_protection_by_symbol_.end() ||
        state_it->second.active_sl_client_order_id.empty()) {
      LogError("STARTUP_PROTECTION_RECOVERY_FAILED: symbol=" + remote.symbol +
               ", reason=sl_not_enqueued");
      return false;
    }
    if (remote_open_order_ids.find(
            state_it->second.active_sl_client_order_id) ==
        remote_open_order_ids.end()) {
      startup_protection_sl_ids_.insert(
          state_it->second.active_sl_client_order_id);
    }
  }

  if (startup_protection_recovery_pending_ &&
      startup_protection_sl_ids_.empty()) {
    LogError("STARTUP_PROTECTION_RECOVERY_FAILED: no_pending_sl_confirmation");
    return false;
  }
  return true;
}

bool BotApplication::RecordCandidateEpisodeClosure(
    const std::string& symbol,
    const IntegratorCandidateEpisode& episode,
    bool recovered_after_restart) {
  CandidateEpisodeClosureRecord closure;
  closure.position_episode_id =
      episode.lineage.position_episode_id;
  closure.decision_id = episode.lineage.decision_id;
  closure.candidate_id = episode.lineage.candidate_id;
  closure.model_version = episode.lineage.model_version;
  closure.mode = episode.lineage.mode;
  closure.policy_reason = episode.lineage.policy_reason;
  closure.symbol = symbol;
  closure.realized_net_usd = episode.realized_net_usd;
  closure.funding_paid_usd = episode.funding_paid_usd;
  closure.fill_event_count = episode.fill_event_count;
  closure.unique_order_count =
      static_cast<int>(episode.order_ids.size());
  closure.evidence_complete = episode.entry_observed_from_flat;
  const bool microstructure_demo_episode =
      closure.policy_reason.rfind("microstructure_demo_", 0) == 0;
  closure.activation_transaction_id =
      microstructure_demo_episode
          ? "microstructure-demo:" + closure.candidate_id
          : system_.integrator_activation_transaction_id();
  if (closure.activation_transaction_id.empty()) {
    closure.activation_transaction_id =
        ReadEnvValue("CLOSED_LOOP_ACTIVATION_TRANSACTION_ID");
  }
  closure.boot_id = boot_id_;
  closure.runtime_config_sha256 =
      system_.integrator_runtime_config_sha256();
  if (closure.runtime_config_sha256.empty()) {
    closure.runtime_config_sha256 =
        ReadEnvValue("AI_TRADE_RUNTIME_CONFIG_SHA256");
  }
  if (closure.runtime_config_sha256.empty()) {
    closure.runtime_config_sha256 =
        config_.integrator.shadow.source_runtime_config_sha256;
  }
  closure.trade_bot_sha256 =
      system_.integrator_trade_bot_sha256();
  if (closure.trade_bot_sha256.empty()) {
    closure.trade_bot_sha256 =
        ReadEnvValue("AI_TRADE_TRADE_BOT_SHA256");
  }
  closure.closed_at_utc = CurrentUtcIsoTimestamp();
  if (config_.mode != "replay" && closure.mode == "canary" &&
      (closure.activation_transaction_id.empty() ||
       closure.boot_id.empty() ||
       !IsSha256Hex(closure.runtime_config_sha256) ||
       !IsSha256Hex(closure.trade_bot_sha256))) {
    LogError(
        "INTEGRATOR_EPISODE_CLOSURE_WAL_FAILED: position_episode_id=" +
        closure.position_episode_id +
        ", error=incomplete_authoritative_candidate_identity");
    return false;
  }
  std::string wal_error;
  if (!wal_.AppendCandidateEpisodeClosure(closure, &wal_error)) {
    LogError("INTEGRATOR_EPISODE_CLOSURE_WAL_FAILED: position_episode_id=" +
             closure.position_episode_id + ", error=" + wal_error);
    return false;
  }
  persisted_closed_episode_ids_.insert(closure.position_episode_id);
  persisted_episode_closures_.insert_or_assign(
      closure.position_episode_id, closure);
  LogInfo(
      std::string(recovered_after_restart
                      ? "INTEGRATOR_POLICY_EPISODE_RECOVERED_CLOSED: "
                      : "INTEGRATOR_POLICY_EPISODE_CLOSED: ") +
      "position_episode_id=" +
      closure.position_episode_id +
      ", decision_id=" + closure.decision_id +
      ", candidate_id=" + closure.candidate_id +
      ", model_version=" + closure.model_version +
      ", mode=" + closure.mode +
      ", policy_reason=" + closure.policy_reason +
      ", symbol=" + symbol +
      ", realized_net_usd=" +
      std::to_string(closure.realized_net_usd) +
      ", funding_paid_usd=" +
      std::to_string(closure.funding_paid_usd) +
      ", fill_event_count=" +
      std::to_string(closure.fill_event_count) +
      ", unique_order_count=" +
      std::to_string(closure.unique_order_count) +
      ", evidence_complete=" +
      std::string(closure.evidence_complete ? "true" : "false") +
      ", activation_transaction_id=" +
      closure.activation_transaction_id +
      ", evidence_boot_id=" + closure.boot_id +
      ", runtime_config_sha256=" + closure.runtime_config_sha256 +
      ", trade_bot_sha256=" + closure.trade_bot_sha256 +
      ", closed_at_utc=" + closure.closed_at_utc +
      ", recovered_after_restart=" +
      std::string(recovered_after_restart ? "true" : "false") +
      ", wal_persisted=true");
  return true;
}

bool BotApplication::HasCandidateIsolationForSymbol(
    const std::string& symbol,
    const std::string& allowed_episode_id) const {
  if (const auto episode_it = integrator_episode_by_symbol_.find(symbol);
      episode_it != integrator_episode_by_symbol_.end() &&
      (allowed_episode_id.empty() ||
       episode_it->second.lineage.position_episode_id != allowed_episode_id)) {
    return true;
  }
  for (const auto& [intent_id, lineage] : integrator_lineage_by_intent_id_) {
    if (!allowed_episode_id.empty() &&
        lineage.position_episode_id == allowed_episode_id) {
      continue;
    }
    const auto persisted_it = persisted_intent_by_id_.find(intent_id);
    if (persisted_it == persisted_intent_by_id_.end() ||
        persisted_it->second.symbol != symbol) {
      continue;
    }
    const OrderRecord* record = oms_.Find(intent_id);
    if (record != nullptr && !OrderManager::IsTerminalState(record->state)) {
      return true;
    }
  }
  const auto grace_it =
      candidate_isolation_grace_until_tick_by_symbol_.find(symbol);
  return grace_it != candidate_isolation_grace_until_tick_by_symbol_.end() &&
         market_tick_count_ <= grace_it->second;
}

void BotApplication::ApplyCandidateEpisodeFill(
    IntegratorCandidateEpisode* episode,
    const FillEvent& fill) {
  if (episode == nullptr || fill.qty <= kNotionalEpsilon ||
      fill.price <= kNotionalEpsilon) {
    return;
  }
  const double signed_fill_qty =
      static_cast<double>(fill.direction) * fill.qty;
  const double before_qty = episode->signed_open_qty;
  const double before_avg = episode->avg_entry_price;
  episode->realized_net_usd +=
      EstimateFillRealizedPnlUsd(before_qty, before_avg, fill) - fill.fee;

  if (std::fabs(before_qty) <= kNotionalEpsilon ||
      SignOf(before_qty) == SignOf(signed_fill_qty)) {
    const double next_qty = before_qty + signed_fill_qty;
    const double total_abs_qty =
        std::fabs(before_qty) + std::fabs(signed_fill_qty);
    episode->avg_entry_price =
        total_abs_qty > kNotionalEpsilon
            ? (std::fabs(before_qty) * before_avg +
               std::fabs(signed_fill_qty) * fill.price) /
                  total_abs_qty
            : 0.0;
    episode->signed_open_qty = next_qty;
  } else if (std::fabs(signed_fill_qty) + kNotionalEpsilon <
             std::fabs(before_qty)) {
    episode->signed_open_qty = before_qty + signed_fill_qty;
  } else if (std::fabs(signed_fill_qty) <=
             std::fabs(before_qty) + kNotionalEpsilon) {
    episode->signed_open_qty = 0.0;
    episode->avg_entry_price = 0.0;
  } else {
    episode->signed_open_qty = before_qty + signed_fill_qty;
    episode->avg_entry_price = fill.price;
  }
  ++episode->fill_event_count;
  episode->order_ids.insert(fill.client_order_id);
}

/**
 * @brief 主循环
 *
 * 每轮处理顺序：
 * 1. 行情事件（驱动策略与下单决策）；
 * 2. 异步执行结果（ACK/Reject）；
 * 3. 成交回报（推进 OMS/账户）；
 * 4. 周期任务（远端风险刷新、对账、Gate、状态日志）。
 */
void BotApplication::RunLoop() {
  MarketEvent event;
  while (true) {
    const bool has_market =
        !replay_terminal_settlement_started_ && adapter_->PollMarket(&event);
    bool advanced_tick = false;
    bool has_fill = false;

    if (has_market) {
      advanced_tick = true;
      has_tick_strategy_signal_ = false;
      tick_cost_filtered_signal_ = false;
      tick_trend_notional_usd_ = 0.0;
      tick_defensive_notional_usd_ = 0.0;
      tick_strategy_signal_symbol_.clear();
      ProcessMarketEvent(event);
    }

    ProcessAsyncResults();

    FillEvent fill;
    while (adapter_->PollFill(&fill)) {
      has_fill = true;
      ProcessFillEvent(fill);
    }
    CheckPendingRequiredSlTimeouts();

    if (advanced_tick) {
      ++market_tick_count_;
      RunRemoteRiskRefresh();
      RunReconcile();
      RunGateMonitor();
      RunSelfEvolution();
      LogStatus();
    }

    if (ShouldExit(has_market, has_fill)) {
      break;
    }

    if (!has_market && !has_fill) {
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
  }
}

/**
 * @brief 周期刷新远端风险字段（liqPrice/mark）
 *
 * 只刷新风险评估相关字段，不重置现金/峰值权益，不清空本地仓位表。
 * 该机制用于降低“启动后持仓变化导致强平距离陈旧”的风险。
 */
void BotApplication::RunRemoteRiskRefresh() {
  if (config_.mode == "replay") return;
  if (adapter_ == nullptr) return;
  if (config_.system_remote_risk_refresh_interval_ticks <= 0) return;
  if (market_tick_count_ % config_.system_remote_risk_refresh_interval_ticks != 0) {
    return;
  }

  std::vector<RemotePositionSnapshot> remote_positions;
  if (!adapter_->GetRemotePositions(&remote_positions)) {
    LogInfo("REMOTE_RISK_REFRESH_DEGRADED: 获取远端持仓失败，保留本地风险视图");
    return;
  }

  system_.RefreshAccountRiskFromRemotePositions(remote_positions);
  RemoteAccountBalanceSnapshot balance;
  if (adapter_->GetRemoteAccountBalance(&balance)) {
    // 运行中只上调峰值，不重置峰值，避免人为清零回撤统计。
    system_.SyncAccountFromRemoteBalance(balance, /*reset_peak_to_equity=*/false);
    LogAccountSyncSnapshot("runtime_refresh", balance, system_.account());
    PersistRemoteAccountCheckpoint(
        "runtime_refresh", balance,
        /*evaluate_cross_boot_continuity=*/false);
  }
  LogInfo("REMOTE_RISK_REFRESH: count=" + std::to_string(remote_positions.size()));
}

/**
 * @brief 行情事件处理
 *
 * 业务顺序：
 * 1. 更新 Universe；
 * 2. 交易暂停时仅更新市值，不做新决策；
 * 3. 执行 Strategy->Risk->Execution；
 * 4. 应用 Universe 约束与下单节流；
 * 5. 满足条件则入队异步执行。
 */
void BotApplication::ProcessMarketEvent(const MarketEvent& event) {
  if (event.feature_only) {
    system_.OnIntegratorMarket(event);
    return;
  }

  if (const auto update = universe_selector_.OnMarket(event); update.has_value()) {
    std::string message =
        "Universe Updated: active_count=" +
        std::to_string(update->active_symbols.size()) +
        ", active_symbols=[" + FormatSymbolList(update->active_symbols) + "]" +
        ", degraded_to_fallback=" +
        std::string(update->degraded_to_fallback ? "true" : "false");
    if (!update->reason_code.empty()) {
      message += ", reason=" + update->reason_code;
    }
    if (!update->sticky_trend_reserve_symbols.empty()) {
      message += ", sticky_trend_reserve=[" +
                 FormatSymbolList(update->sticky_trend_reserve_symbols) + "]";
    }
    message += ", top_scores=[" + FormatSymbolScores(update->symbol_scores, 5) + "]";
    LogInfo(message);
  }

  const double mark_price_for_evolution =
      event.mark_price > 0.0 ? event.mark_price : event.price;
  if (std::isfinite(mark_price_for_evolution) && mark_price_for_evolution > 0.0) {
    latest_mark_price_usd_ = mark_price_for_evolution;
    has_latest_mark_price_ = true;
    latest_mark_price_by_symbol_[event.symbol] = mark_price_for_evolution;
  } else {
    has_latest_mark_price_ = false;
  }
  if (std::isfinite(event.funding_rate_per_interval)) {
    latest_funding_rate_per_tick_ = event.funding_rate_per_interval;
    has_latest_funding_rate_per_tick_ = true;
    latest_funding_rate_observed_tick_ = market_tick_count_;
  } else if (has_latest_funding_rate_per_tick_ &&
             market_tick_count_ - latest_funding_rate_observed_tick_ >
                 kFundingObservationStaleTicks) {
    has_latest_funding_rate_per_tick_ = false;
  }

  // 保护单和即时 reduce 需要用当前 tick 的账户名义值计算减仓数量；
  // Evaluate() 内部也会刷新一次 mark，重复刷新是幂等的，但这里必须先于保护逻辑。
  system_.OnMarketSnapshot(event);
  const double effective_funding_rate =
      std::isfinite(event.funding_rate_per_interval)
          ? event.funding_rate_per_interval
          : config_.self_evolution.virtual_funding_rate_per_tick;
  const double funding_paid =
      system_.ApplyFunding(event.symbol, effective_funding_rate);
  if (std::fabs(funding_paid) > kNotionalEpsilon) {
    LogInfo("FUNDING_APPLIED: symbol=" + event.symbol +
            ", rate_per_interval=" +
            std::to_string(effective_funding_rate) +
            ", funding_paid_usd=" + std::to_string(funding_paid) +
            ", source=" +
            std::string(std::isfinite(event.funding_rate_per_interval)
                            ? "market"
                            : "configured_fallback"));
  }
  if (const auto episode_it =
          integrator_episode_by_symbol_.find(event.symbol);
      episode_it != integrator_episode_by_symbol_.end() &&
      std::isfinite(mark_price_for_evolution) &&
      mark_price_for_evolution > kNotionalEpsilon) {
    const double episode_funding_paid =
        episode_it->second.signed_open_qty * mark_price_for_evolution *
        effective_funding_rate;
    episode_it->second.realized_net_usd -= episode_funding_paid;
    episode_it->second.funding_paid_usd += episode_funding_paid;
  }
  UpdateProfitProtection(event);
  ManageCandidateProbeLifecycle(event);
  ManageStrategyReduceLifecycle(event);
  RefreshProtectionReduceOnlyRelease("market_tick_flat_idle");

  // 对账硬停机时直接停止策略决策；Gate 停机仅阻断下单，不阻断观测统计。
  if (reconcile_halted_) {
    return;
  }

  // inactive symbol 且无持仓/无在途净仓位订单时，直接跳过整条决策链，
  // 降低无效信号评估与日志噪音。
  const bool symbol_active = universe_selector_.IsActive(event.symbol);
  const double symbol_notional = system_.account().current_notional_usd(event.symbol);
  const bool has_pending_symbol_net_orders =
      oms_.HasPendingNetPositionOrderForSymbol(event.symbol);
  if (ShouldSkipInactiveSymbolDecision(symbol_active,
                                       symbol_notional,
                                       has_pending_symbol_net_orders)) {
    return;
  }

  // Segment replay prepends causal history so stateful regime/strategy
  // features are warm before the measured segment. Those context bars update
  // state but must never create exposure.
  const bool trade_ok = adapter_->TradeOk() && !IsForceReduceOnlyActive() &&
                        !event.execution_disabled;
  double symbol_inflight_notional_usd = 0.0;
  if (config_.execution_include_inflight_notional_in_position) {
    const double effective_price =
        event.mark_price > 0.0 ? event.mark_price : event.price;
    if (std::isfinite(effective_price) && effective_price > 0.0) {
      const double inflight_qty =
          oms_.PendingNetPositionRemainingQtyForSymbol(event.symbol);
      symbol_inflight_notional_usd = inflight_qty * effective_price;
    }
  }
  std::string settled_position_candidate_id;
  std::string settled_position_policy_reason;
  if (const auto episode_it =
          integrator_episode_by_symbol_.find(event.symbol);
      episode_it != integrator_episode_by_symbol_.end()) {
    settled_position_candidate_id = episode_it->second.lineage.candidate_id;
    settled_position_policy_reason = episode_it->second.lineage.policy_reason;
  }
  auto decision = system_.Evaluate(
      event, trade_ok, symbol_inflight_notional_usd,
      has_pending_symbol_net_orders, settled_position_candidate_id,
      settled_position_policy_reason);
  CancelConflictingMicrostructureEntries(decision);
  constexpr double kRebalanceGapEpsilon = 1e-6;
  if (!decision.risk_adjusted.symbol.empty()) {
    const double settled_symbol_notional =
        system_.account().current_notional_usd(decision.risk_adjusted.symbol);
    const double effective_symbol_notional =
        config_.execution_include_inflight_notional_in_position
            ? settled_symbol_notional + symbol_inflight_notional_usd
            : settled_symbol_notional;
    const double rebalance_gap_abs_usd = std::fabs(
        decision.risk_adjusted.adjusted_notional_usd - effective_symbol_notional);
    if (!decision.risk_adjusted.reduce_only &&
        rebalance_gap_abs_usd > kRebalanceGapEpsilon) {
      ++funnel_window_.rebalance_gap_samples;
      funnel_window_.rebalance_gap_abs_usd_sum += rebalance_gap_abs_usd;
      funnel_window_.rebalance_gap_abs_usd_max =
          std::max(funnel_window_.rebalance_gap_abs_usd_max, rebalance_gap_abs_usd);
      if (!decision.intent.has_value() &&
          config_.execution_min_rebalance_notional_usd > 0.0 &&
          rebalance_gap_abs_usd + kRebalanceGapEpsilon <
              config_.execution_min_rebalance_notional_usd) {
        ++funnel_window_.rebalance_converged_within_min_notional;
        funnel_window_.rebalance_gap_within_min_notional_abs_usd_sum +=
            rebalance_gap_abs_usd;
      }
    }
  }
  if (decision.regime.warmup) {
    ++funnel_window_.regime_warmup_ticks;
  }
  if (decision.regime.trend_candidate) {
    ++funnel_window_.regime_trend_candidate_ticks;
  }
  if (decision.regime.warmup_trend_candidate) {
    ++funnel_window_.regime_warmup_trend_candidate_ticks;
    if (universe_selector_.RecordWarmupTrendCandidate(
            decision.regime.symbol,
            decision.regime.trend_threshold_ratio)) {
      LogInfo("UNIVERSE_WARMUP_TREND_RESERVE_PIN: symbol=" +
              decision.regime.symbol +
              ", trend_threshold_ratio=" +
              std::to_string(decision.regime.trend_threshold_ratio) +
              ", min_ratio=" +
              std::to_string(config_.universe.warmup_trend_reserve_min_ratio) +
              ", residency_refreshes=" +
              std::to_string(
                  config_.universe.trend_reserve_min_residency_refreshes));
    }
  }
  switch (decision.regime.bucket) {
    case RegimeBucket::kTrend:
      ++funnel_window_.regime_trend_ticks;
      break;
    case RegimeBucket::kRange:
      ++funnel_window_.regime_range_ticks;
      break;
    case RegimeBucket::kExtreme:
      ++funnel_window_.regime_extreme_ticks;
      break;
  }
  const bool regime_changed =
      !has_last_regime_state_ ||
      last_regime_state_.symbol != decision.regime.symbol ||
      last_regime_state_.regime != decision.regime.regime ||
      last_regime_state_.bucket != decision.regime.bucket ||
      last_regime_state_.raw_regime != decision.regime.raw_regime ||
      last_regime_state_.pending_regime != decision.regime.pending_regime ||
      last_regime_state_.pending_regime_ticks !=
          decision.regime.pending_regime_ticks ||
      last_regime_state_.pending_trend_confirmation !=
          decision.regime.pending_trend_confirmation ||
      last_regime_state_.warmup != decision.regime.warmup ||
      last_regime_state_.trend_candidate != decision.regime.trend_candidate ||
      last_regime_state_.warmup_trend_candidate !=
          decision.regime.warmup_trend_candidate;
  if (regime_changed) {
    LogInfo("REGIME_CHANGE: symbol=" + decision.regime.symbol +
            ", regime=" + std::string(ToString(decision.regime.regime)) +
            ", bucket=" + std::string(ToString(decision.regime.bucket)) +
            ", warmup=" + (decision.regime.warmup ? "true" : "false") +
            ", decision_interval_ms=" +
            std::to_string(decision.regime.decision_interval_ms) +
            ", aggregated_events=" +
            std::to_string(decision.regime.aggregated_event_count) +
            ", instant_return=" +
            std::to_string(decision.regime.instant_return) +
            ", trend_strength=" +
            std::to_string(decision.regime.trend_strength) +
            ", volatility=" +
            std::to_string(decision.regime.volatility_level) +
            ", trend_threshold_ratio=" +
            std::to_string(decision.regime.trend_threshold_ratio) +
            ", volatility_threshold_ratio=" +
            std::to_string(decision.regime.volatility_threshold_ratio) +
            ", trend_candidate=" +
            std::string(decision.regime.trend_candidate ? "true" : "false") +
            ", warmup_trend_candidate=" +
            std::string(decision.regime.warmup_trend_candidate ? "true"
                                                               : "false") +
            ", raw_regime=" +
            std::string(ToString(decision.regime.raw_regime)) +
            ", raw_bucket=" +
            std::string(ToString(decision.regime.raw_bucket)) +
            ", pending_regime=" +
            std::string(ToString(decision.regime.pending_regime)) +
            ", pending_bucket=" +
            std::string(ToString(decision.regime.pending_bucket)) +
            ", pending_regime_ticks=" +
            std::to_string(decision.regime.pending_regime_ticks) +
            ", confirm_ticks_required=" +
            std::to_string(decision.regime.confirm_ticks_required) +
            ", pending_regime_elapsed_ms=" +
            std::to_string(decision.regime.pending_regime_elapsed_ms) +
            ", confirm_elapsed_ms_required=" +
            std::to_string(decision.regime.confirm_elapsed_ms_required) +
            ", pending_trend_confirmation=" +
            std::string(decision.regime.pending_trend_confirmation ? "true"
                                                                   : "false"));
  }
  last_regime_state_ = decision.regime;
  has_last_regime_state_ = true;
  regime_state_by_symbol_[decision.regime.symbol] = decision.regime;
  last_strategy_signal_ = decision.base_signal;
  has_last_strategy_signal_ = true;
  const auto executable_components =
      ScaleStrategyComponentsForExecution(decision);
  tick_trend_notional_usd_ = executable_components.first;
  tick_defensive_notional_usd_ = executable_components.second;
  tick_strategy_signal_symbol_ =
      decision.signal.symbol.empty() ? event.symbol : decision.signal.symbol;
  has_tick_strategy_signal_ = !tick_strategy_signal_symbol_.empty();
  if (HasExposure(decision.base_signal.trend_notional_usd) ||
      HasExposure(decision.base_signal.defensive_notional_usd) ||
      HasExposure(decision.base_signal.suggested_notional_usd)) {
    ++funnel_window_.strategy_mix_samples;
    funnel_window_.trend_notional_abs_sum +=
        std::fabs(decision.base_signal.trend_notional_usd);
    funnel_window_.defensive_notional_abs_sum +=
        std::fabs(decision.base_signal.defensive_notional_usd);
    funnel_window_.blended_notional_abs_sum +=
        std::fabs(decision.base_signal.suggested_notional_usd);
  } else if (IsPolicySuppressedFlatSignal(decision.base_signal)) {
    ++funnel_window_.strategy_policy_flat_samples;
  }
  if (IsPolicySuppressedFlatSignal(decision.base_signal) &&
      std::fabs(symbol_notional) > kNotionalEpsilon) {
    LogInfo("POLICY_FLAT_RESIDUAL_POSITION: symbol=" + event.symbol +
            ", current_notional=" + std::to_string(symbol_notional) +
            ", has_reduce_intent=" +
            std::string(decision.intent.has_value() &&
                                decision.intent->reduce_only
                            ? "true"
                            : "false"));
  }
  if (decision.shadow.enabled) {
    ++funnel_window_.integrator_scored;
    funnel_window_.integrator_model_score_sum += decision.shadow.model_score;
    funnel_window_.integrator_p_up_sum += decision.shadow.p_up;
    funnel_window_.integrator_p_down_sum += decision.shadow.p_down;
    if (decision.shadow.p_up >= 0.55) {
      ++funnel_window_.integrator_pred_up;
    } else if (decision.shadow.p_down >= 0.55) {
      ++funnel_window_.integrator_pred_down;
    }
    last_shadow_inference_ = decision.shadow;
    has_last_shadow_inference_ = true;
  }
  std::string integrator_decision_id;
  if (decision.integrator_policy_applied) {
    integrator_decision_id =
        boot_id_ + ":" + std::to_string(market_tick_count_) + ":" +
        decision.signal.symbol + ":" + decision.shadow.model_version;
    ++funnel_window_.integrator_policy_proposed;
    LogInfo("INTEGRATOR_POLICY_PROPOSED: decision_id=" +
            integrator_decision_id +
            ", candidate_id=" + decision.shadow.model_version +
            ", mode=" +
            std::string(ToString(system_.integrator_mode())) +
            ", model_version=" + decision.shadow.model_version +
            ", source=" + decision.shadow.source +
            ", reason=" + decision.integrator_policy_reason +
            ", symbol=" + decision.signal.symbol +
            ", confidence=" + std::to_string(decision.integrator_confidence) +
            ", base_notional=" +
            std::to_string(decision.base_signal.suggested_notional_usd) +
            ", base_trend_component=" +
            std::to_string(decision.base_signal.trend_notional_usd) +
            ", base_defensive_component=" +
            std::to_string(decision.base_signal.defensive_notional_usd) +
            ", final_notional=" +
            std::to_string(decision.signal.suggested_notional_usd));
  }
  const double effective_symbol_notional_for_probe =
      config_.execution_include_inflight_notional_in_position
          ? symbol_notional + symbol_inflight_notional_usd
          : symbol_notional;
  TryApplyTrendCandidateProbe(&decision,
                              event,
                              trade_ok,
                              effective_symbol_notional_for_probe,
                              has_pending_symbol_net_orders);
  if (HasExposure(decision.base_signal.suggested_notional_usd)) {
    ++funnel_window_.raw_signals;
  }
  if (HasExposure(decision.risk_adjusted.adjusted_notional_usd)) {
    ++funnel_window_.risk_adjusted_signals;
  }
  IntegratorCandidateLineage integrator_lineage;
  const IntegratorCandidateLineage* integrator_lineage_ptr = nullptr;

  if (decision.intent.has_value()) {
    ++funnel_window_.intents_generated;
  }

  // 不在活跃池时仅禁止“开仓意图”；减仓/保护单必须放行，避免风险无法收敛。
  if (decision.intent.has_value() &&
      !universe_selector_.IsActive(decision.intent->symbol) &&
      ShouldFilterInactiveSymbolIntent(*decision.intent)) {
    ++funnel_window_.intents_filtered_inactive_symbol;
    decision.intent.reset();
  }

  NormalizeReduceIntentToActualPosition(&decision, event);

  if (decision.intent.has_value() && IsOpeningIntent(*decision.intent)) {
    int symbol_quality_remaining_ticks = 0;
    if (ShouldThrottleSymbolQualityQuarantine(
            decision, &symbol_quality_remaining_ticks)) {
      ++funnel_window_.intents_throttled;
      ++funnel_window_.intents_throttled_symbol_quality_quarantine;
      LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", reason=symbol_quality_quarantine_remaining_ticks=" +
              std::to_string(symbol_quality_remaining_ticks) +
              ", quality_guard_trigger_count=" +
              std::to_string(SymbolExecutionQualityMemoryTriggerCount(
                  decision.intent->symbol)) +
              ", allow_recovery_probe=true");
      decision.intent.reset();
    }
  }

  // 交易所规则前置保护：避免 qty=0/min_qty/min_notional 拒单循环。
  if (decision.intent.has_value()) {
    std::string guard_reason;
    if (ViolatesExchangePretradeGuard(adapter_.get(), &*decision.intent, event,
                                      &guard_reason)) {
      LogInfo("EXEC_FILTER_IGNORE: symbol=" + decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", reason=" + guard_reason);
      ++funnel_window_.intents_filtered_min_notional;
      decision.intent.reset();
    }
  }

  if (decision.intent.has_value() && IsOpeningIntent(*decision.intent)) {
    const bool candidate_probe_intent =
        IsTrendCandidateProbeIntent(decision.intent->client_order_id);
    int cooldown_ticks_remaining = 0;
    if (IsCostFilterCooldownActive(decision.intent->symbol,
                                   &cooldown_ticks_remaining)) {
      if (candidate_probe_intent) {
        ++funnel_window_.candidate_probe_cost_cooldown_bypass;
        LogInfo("TREND_CANDIDATE_PROBE_COST_COOLDOWN_BYPASS: symbol=" +
                decision.intent->symbol +
                ", client_order_id=" + decision.intent->client_order_id +
                ", cost_filter_cooldown_ticks_remaining=" +
                std::to_string(cooldown_ticks_remaining));
      } else {
        ++funnel_window_.intents_throttled;
        ++funnel_window_.intents_throttled_cost_cooldown;
        LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
                ", client_order_id=" + decision.intent->client_order_id +
                ", reason=cost_filter_cooldown_ticks_remaining=" +
                std::to_string(cooldown_ticks_remaining));
        decision.intent.reset();
      }
    }
  }

  if (decision.intent.has_value() && IsOpeningIntent(*decision.intent)) {
    double expected_edge_bps = 0.0;
    double required_edge_bps = 0.0;
    double base_required_edge_bps = 0.0;
    double adaptive_relax_bps = 0.0;
    double maker_relax_bps = 0.0;
    double regime_adjust_bps = 0.0;
    double volatility_adjust_bps = 0.0;
    double liquidity_adjust_bps = 0.0;
    double concentration_adjust_bps = 0.0;
    double quality_guard_penalty_bps = 0.0;
    double observed_filtered_ratio = 0.0;
    double entry_edge_gap_bps = 0.0;
    bool near_miss = false;
    bool near_miss_allowed = false;
    bool filtered = ShouldFilterByFeeAwareGate(
        decision,
        event,
        &expected_edge_bps,
        &required_edge_bps,
        &base_required_edge_bps,
        &adaptive_relax_bps,
        &maker_relax_bps,
        &regime_adjust_bps,
        &volatility_adjust_bps,
        &liquidity_adjust_bps,
        &concentration_adjust_bps,
        &quality_guard_penalty_bps,
        &observed_filtered_ratio,
        &entry_edge_gap_bps,
        &near_miss,
        &near_miss_allowed);
    const bool candidate_probe_intent =
        IsTrendCandidateProbeIntent(decision.intent->client_order_id);
    bool candidate_probe_fee_override = false;
    if (filtered && candidate_probe_intent) {
      const double max_edge_gap_bps =
          std::max(0.0, config_.execution_candidate_probe_max_edge_gap_bps);
      const bool has_quality_memory =
          HasSymbolExecutionQualityMemory(decision.intent->symbol);
      const bool quality_guard_override_blocked =
          quality_guard_penalty_bps > 1e-9 ||
          has_quality_memory;
      const double memory_max_edge_gap_bps = std::max(
          0.0, config_.execution_candidate_probe_memory_max_edge_gap_bps);
      const double memory_min_trend_ratio = std::max(
          0.0, config_.execution_candidate_probe_memory_min_trend_ratio);
      const double diagnostic_min_trend_ratio = std::max(
          0.0, config_.execution_candidate_probe_diagnostic_min_trend_ratio);
      const double diagnostic_max_edge_gap_bps = std::max(
          0.0, config_.execution_candidate_probe_diagnostic_max_edge_gap_bps);
      const double diagnostic_min_expected_edge_bps = std::max(
          0.0, config_.execution_candidate_probe_diagnostic_min_expected_edge_bps);
      ++funnel_window_.candidate_probe_cost_gate_samples;
      if (decision.intent->direction > 0) {
        ++funnel_window_.candidate_probe_cost_gate_long_count;
      } else if (decision.intent->direction < 0) {
        ++funnel_window_.candidate_probe_cost_gate_short_count;
      }
      funnel_window_.candidate_probe_cost_gate_expected_edge_bps_sum +=
          expected_edge_bps;
      funnel_window_.candidate_probe_cost_gate_required_edge_bps_sum +=
          required_edge_bps;
      funnel_window_.candidate_probe_cost_gate_edge_gap_bps_sum +=
          entry_edge_gap_bps;
      funnel_window_.candidate_probe_cost_gate_edge_gap_bps_max =
          std::max(funnel_window_.candidate_probe_cost_gate_edge_gap_bps_max,
                   entry_edge_gap_bps);
      funnel_window_.candidate_probe_cost_gate_trend_ratio_sum +=
          decision.regime.trend_threshold_ratio;
      const bool diagnostic_enabled =
          config_.execution_candidate_probe_diagnostic_canary_enabled;
      const std::string diagnostic_block_reason =
          !diagnostic_enabled
              ? "disabled"
              : (quality_guard_override_blocked
                     ? "quality_guard"
                     : (expected_edge_bps + 1e-9 <
                                diagnostic_min_expected_edge_bps
                            ? "expected_edge_low"
                            : (decision.regime.trend_threshold_ratio + 1e-9 <
                                       diagnostic_min_trend_ratio
                                   ? "trend_ratio_low"
                                   : (entry_edge_gap_bps >
                                              diagnostic_max_edge_gap_bps + 1e-9
                                          ? "edge_gap_high"
                                          : "unknown"))));
      bool memory_recovery_allowed = false;
      bool diagnostic_canary_allowed = false;
      if (ShouldAllowCandidateProbeFeeOverride(
              decision,
              expected_edge_bps,
              entry_edge_gap_bps,
              quality_guard_penalty_bps,
              has_quality_memory,
              &memory_recovery_allowed,
              &diagnostic_canary_allowed)) {
        filtered = false;
        near_miss_allowed = true;
        candidate_probe_fee_override = true;
        ++funnel_window_.candidate_probe_fee_overrides;
        LogInfo(std::string(memory_recovery_allowed
                            ? "TREND_CANDIDATE_PROBE_RECOVERY_FEE_OVERRIDE: symbol="
                            : "TREND_CANDIDATE_PROBE_FEE_OVERRIDE: symbol=") +
                decision.intent->symbol +
                ", client_order_id=" + decision.intent->client_order_id +
                ", expected_edge_bps=" + std::to_string(expected_edge_bps) +
                ", required_edge_bps=" + std::to_string(required_edge_bps) +
                ", edge_gap_bps=" + std::to_string(entry_edge_gap_bps) +
                ", max_edge_gap_bps=" + std::to_string(max_edge_gap_bps) +
                ", diagnostic_canary=" +
                std::string(diagnostic_canary_allowed ? "true" : "false") +
                ", direction=" + std::to_string(decision.intent->direction) +
                ", diagnostic_min_trend_ratio=" +
                std::to_string(diagnostic_min_trend_ratio) +
                ", diagnostic_max_edge_gap_bps=" +
                std::to_string(diagnostic_max_edge_gap_bps) +
                ", diagnostic_min_expected_edge_bps=" +
                std::to_string(diagnostic_min_expected_edge_bps) +
                ", memory_max_edge_gap_bps=" +
                std::to_string(memory_max_edge_gap_bps) +
                ", memory_min_trend_ratio=" +
                std::to_string(memory_min_trend_ratio) +
                ", trend_strength=" +
                std::to_string(decision.regime.trend_strength) +
                ", trend_threshold_ratio=" +
                std::to_string(decision.regime.trend_threshold_ratio));
      } else {
        ++funnel_window_.candidate_probe_filtered_fee;
        LogInfo("TREND_CANDIDATE_PROBE_FILTERED_FEE: symbol=" +
                decision.intent->symbol +
                ", client_order_id=" + decision.intent->client_order_id +
                ", direction=" + std::to_string(decision.intent->direction) +
                ", expected_edge_bps=" + std::to_string(expected_edge_bps) +
                ", required_edge_bps=" + std::to_string(required_edge_bps) +
                ", edge_gap_bps=" + std::to_string(entry_edge_gap_bps) +
                ", max_edge_gap_bps=" + std::to_string(max_edge_gap_bps) +
                ", quality_guard_override_blocked=" +
                std::string(quality_guard_override_blocked ? "true" : "false") +
                ", quality_guard_penalty_bps=" +
                std::to_string(quality_guard_penalty_bps) +
                ", diagnostic_canary_enabled=" +
                std::string(config_.execution_candidate_probe_diagnostic_canary_enabled
                                ? "true"
                                : "false") +
                ", diagnostic_min_trend_ratio=" +
                std::to_string(diagnostic_min_trend_ratio) +
                ", diagnostic_max_edge_gap_bps=" +
                std::to_string(diagnostic_max_edge_gap_bps) +
                ", diagnostic_min_expected_edge_bps=" +
                std::to_string(diagnostic_min_expected_edge_bps) +
                ", diagnostic_block_reason=" + diagnostic_block_reason +
                ", memory_max_edge_gap_bps=" +
                std::to_string(memory_max_edge_gap_bps) +
                ", memory_min_trend_ratio=" +
                std::to_string(memory_min_trend_ratio) +
                ", trend_strength=" +
                std::to_string(decision.regime.trend_strength) +
                ", trend_threshold_ratio=" +
                std::to_string(decision.regime.trend_threshold_ratio));
      }
    }
    UpdateEntryGateObservedRatio(filtered, near_miss, near_miss_allowed);
    ++funnel_window_.entry_edge_samples;
    funnel_window_.entry_edge_bps_sum += expected_edge_bps;
    funnel_window_.entry_base_required_edge_bps_sum += base_required_edge_bps;
    funnel_window_.entry_required_edge_bps_sum += required_edge_bps;
    funnel_window_.entry_adaptive_relax_bps_sum += adaptive_relax_bps;
    funnel_window_.entry_maker_relax_bps_sum += maker_relax_bps;
    funnel_window_.entry_regime_adjust_bps_sum += regime_adjust_bps;
    funnel_window_.entry_volatility_adjust_bps_sum += volatility_adjust_bps;
    funnel_window_.entry_liquidity_adjust_bps_sum += liquidity_adjust_bps;
    funnel_window_.entry_concentration_adjust_bps_sum += concentration_adjust_bps;
    funnel_window_.entry_quality_guard_penalty_bps_sum +=
        quality_guard_penalty_bps;
    funnel_window_.entry_edge_gap_bps_sum += entry_edge_gap_bps;
    if (filtered) {
      ++funnel_window_.intents_filtered_fee_aware;
      if (near_miss) {
        ++funnel_window_.intents_filtered_fee_aware_near_miss;
      }
      tick_cost_filtered_signal_ = true;
      OnCostFilterRejected(decision.intent->symbol);
      LogInfo("ORDER_FILTERED_COST: symbol=" + decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", expected_edge_bps=" + std::to_string(expected_edge_bps) +
              ", base_required_edge_bps=" +
              std::to_string(base_required_edge_bps) +
              ", adaptive_relax_bps=" + std::to_string(adaptive_relax_bps) +
              ", maker_relax_bps=" + std::to_string(maker_relax_bps) +
              ", regime_adjust_bps=" + std::to_string(regime_adjust_bps) +
              ", volatility_adjust_bps=" +
              std::to_string(volatility_adjust_bps) +
              ", liquidity_adjust_bps=" +
              std::to_string(liquidity_adjust_bps) +
              ", concentration_adjust_bps=" +
              std::to_string(concentration_adjust_bps) +
              ", quality_guard_penalty_bps=" +
              std::to_string(quality_guard_penalty_bps) +
              ", required_edge_bps=" + std::to_string(required_edge_bps) +
              ", edge_gap_bps=" + std::to_string(entry_edge_gap_bps) +
              ", near_miss=" + std::string(near_miss ? "true" : "false") +
              ", near_miss_tolerance_bps=" +
              std::to_string(config_.execution_entry_gate_near_miss_tolerance_bps) +
              ", round_trip_cost_bps=" + std::to_string(RoundTripCostBps()) +
              ", min_expected_edge_bps=" +
              std::to_string(config_.execution_min_expected_edge_bps) +
              ", required_edge_cap_bps=" +
              std::to_string(config_.execution_required_edge_cap_bps) +
              ", observed_filtered_ratio=" +
              std::to_string(observed_filtered_ratio));
      if (candidate_probe_intent) {
        candidate_probe_intent_ids_.erase(decision.intent->client_order_id);
        candidate_probe_attempt_by_intent_id_.erase(decision.intent->client_order_id);
        candidate_probe_taker_fallback_by_intent_id_.erase(
            decision.intent->client_order_id);
      }
      decision.intent.reset();
    } else {
      if (near_miss_allowed && !candidate_probe_fee_override) {
        ++funnel_window_.intents_passed_fee_aware_near_miss;
        LogInfo("ORDER_NEAR_MISS_MAKER_ALLOWED: symbol=" + decision.intent->symbol +
                ", client_order_id=" + decision.intent->client_order_id +
                ", expected_edge_bps=" + std::to_string(expected_edge_bps) +
                ", required_edge_bps=" + std::to_string(required_edge_bps) +
                ", edge_gap_bps=" + std::to_string(entry_edge_gap_bps) +
                ", near_miss_tolerance_bps=" +
                std::to_string(config_.execution_entry_gate_near_miss_tolerance_bps) +
                ", maker_allow_extra_gap_bps=" +
                std::to_string(config_.execution_entry_gate_near_miss_maker_max_gap_bps) +
                ", maker_allow_upper_gap_bps=" +
                std::to_string(config_.execution_entry_gate_near_miss_tolerance_bps +
                               config_.execution_entry_gate_near_miss_maker_max_gap_bps +
                               adaptive_relax_bps) +
                ", maker_allow_config_key=entry_gate_near_miss_maker_max_gap_bps" +
                ", maker_allow_config_value_bps=" +
                std::to_string(config_.execution_entry_gate_near_miss_maker_max_gap_bps));
      }
      OnCostFilterAccepted(decision.intent->symbol);
    }
  }

  if (config_.integrator.enabled && config_.integrator.shadow.log_model_score &&
      decision.shadow.enabled && decision.intent.has_value()) {
    LogInfo("INTEGRATOR_SHADOW_SCORE: symbol=" + decision.intent->symbol +
            ", model_version=" + decision.shadow.model_version +
            ", model_score=" + std::to_string(decision.shadow.model_score) +
            ", p_up=" + std::to_string(decision.shadow.p_up) +
            ", p_down=" + std::to_string(decision.shadow.p_down));
  }

  if (const auto gate_alert =
          gate_monitor_.OnDecision(decision.base_signal, decision.risk_adjusted,
                                   decision.intent);
      gate_alert.has_value()) {
    ++funnel_window_.gate_alerts;
    LogInfo("GATE_ALERT: code=" + *gate_alert +
            ", tick=" + std::to_string(market_tick_count_));
  }

  if (gate_halted_ && decision.intent.has_value()) {
    ++funnel_window_.intents_throttled;
    LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
            ", client_order_id=" + decision.intent->client_order_id +
            ", reason=gate_halted");
    decision.intent.reset();
  }

  if (decision.intent.has_value()) {
    double estimated_gross_bps = 0.0;
    double estimated_net_bps = 0.0;
    double required_net_bps = 0.0;
    double expected_exit_cost_bps = 0.0;
    int holding_ticks = 0;
    std::string bypass_reason;
    if (ShouldThrottleStrategyReduceCostGuard(decision,
                                              event,
                                              &estimated_gross_bps,
                                              &estimated_net_bps,
                                              &required_net_bps,
                                              &expected_exit_cost_bps,
                                              &holding_ticks,
                                              &bypass_reason)) {
      ++funnel_window_.intents_throttled;
      ++funnel_window_.strategy_reduce_cost_guard_blocked;
      LogInfo("STRATEGY_REDUCE_COST_GUARD_BLOCKED: symbol=" +
              decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", estimated_gross_bps=" +
              std::to_string(estimated_gross_bps) +
              ", estimated_net_bps=" + std::to_string(estimated_net_bps) +
              ", required_net_bps=" + std::to_string(required_net_bps) +
              ", expected_exit_cost_bps=" +
              std::to_string(expected_exit_cost_bps) +
              ", holding_ticks=" + std::to_string(holding_ticks) +
              ", max_hold_ticks=" +
              std::to_string(
                  std::max(0, config_.execution_strategy_reduce_guard_max_hold_ticks)) +
              ", max_adverse_bps=" +
              std::to_string(
                  std::max(0.0, config_.execution_strategy_reduce_max_adverse_bps)));
      LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", reason=strategy_reduce_cost_guard");
      decision.intent.reset();
    } else if (!bypass_reason.empty()) {
      ++funnel_window_.strategy_reduce_cost_guard_bypassed;
      LogInfo("STRATEGY_REDUCE_COST_GUARD_BYPASS: symbol=" +
              decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", reason=" + bypass_reason +
              ", estimated_gross_bps=" +
              std::to_string(estimated_gross_bps) +
              ", estimated_net_bps=" + std::to_string(estimated_net_bps) +
              ", required_net_bps=" + std::to_string(required_net_bps) +
              ", expected_exit_cost_bps=" +
              std::to_string(expected_exit_cost_bps) +
              ", holding_ticks=" + std::to_string(holding_ticks));
    }
  }

  if (decision.intent.has_value()) {
    int quality_min_hold_remaining_ticks = 0;
    if (ShouldThrottleSymbolQualityMinHold(
            decision, &quality_min_hold_remaining_ticks)) {
      ++funnel_window_.intents_throttled;
      LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
              ", client_order_id=" + decision.intent->client_order_id +
              ", reason=symbol_quality_min_hold_remaining_ticks=" +
              std::to_string(quality_min_hold_remaining_ticks));
      decision.intent.reset();
    }
  }

  if (decision.intent.has_value()) {
    if (decision.integrator_policy_applied &&
        !integrator_decision_id.empty()) {
      integrator_lineage = IntegratorCandidateLineage{
          .decision_id = integrator_decision_id,
          .candidate_id = decision.shadow.model_version,
          .model_version = decision.shadow.model_version,
          .mode = std::string(ToString(system_.integrator_mode())),
          .policy_reason = decision.integrator_policy_reason,
          .position_episode_id = integrator_decision_id,
      };
      integrator_lineage_ptr = &integrator_lineage;
      ++funnel_window_.integrator_policy_risk_accepted;
      LogInfo("INTEGRATOR_POLICY_RISK_ACCEPTED: decision_id=" +
              integrator_lineage.decision_id +
              ", candidate_id=" + integrator_lineage.candidate_id +
              ", model_version=" + integrator_lineage.model_version +
              ", mode=" + integrator_lineage.mode +
              ", client_order_id=" + decision.intent->client_order_id +
              ", symbol=" + decision.intent->symbol +
              ", purpose=" +
              std::string(OrderPurposeToString(decision.intent->purpose)) +
              ", reduce_only=" +
              std::string(decision.intent->reduce_only ? "true" : "false"));
    }

    // 同币种同方向在途单限额控制：
    // - Entry 仅统计同方向 Entry，在“先平后开”场景允许与同方向 Reduce 并存；
    // - Reduce 仍按净仓位在途单统计，避免连续平仓风暴。
    const int inflight_limit =
        std::max(0, config_.execution_max_inflight_orders_per_symbol_direction);
    if (inflight_limit > 0 && IsNetPositionOrderPurpose(decision.intent->purpose)) {
      int pending_same_direction = 0;
      if (decision.intent->purpose == OrderPurpose::kEntry &&
          !decision.intent->reduce_only) {
        pending_same_direction = oms_.PendingEntryOrderCountForSymbolDirection(
            decision.intent->symbol, decision.intent->direction);
      } else {
        pending_same_direction =
            oms_.PendingNetPositionOrderCountForSymbolDirection(
                decision.intent->symbol, decision.intent->direction);
      }
      if (pending_same_direction >= inflight_limit) {
        ++funnel_window_.intents_throttled;
        LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
                ", client_order_id=" + decision.intent->client_order_id +
                ", reason=pending_same_side_inflight_limit_reached=" +
                std::to_string(inflight_limit));
        return;
      }
    }

    std::string reason;
    const auto now = CurrentTimestampMs();
    if (order_throttle_.Allow(*decision.intent, now, market_tick_count_, &reason)) {
      if (EnqueueIntent(*decision.intent, integrator_lineage_ptr)) {
        order_throttle_.OnAccepted(*decision.intent, now, market_tick_count_);
      }
    } else {
      ++funnel_window_.intents_throttled;
      if (!reason.empty()) {
        LogInfo("ORDER_THROTTLED: symbol=" + decision.intent->symbol +
                ", client_order_id=" + decision.intent->client_order_id +
                ", reason=" + reason);
      }
    }
  }
}

/**
 * @brief 下单入队（WAL-first）
 *
 * 原子语义：
 * - 必须先 RegisterIntent + AppendIntent(WAL) 成功；
 * - 成功后才投递 AsyncExecutor；
 * - WAL 失败时立即标记 Rejected，防止“已发单但不可恢复”。
 */
bool BotApplication::EnqueueIntent(
    const OrderIntent& intent,
    const IntegratorCandidateLineage* integrator_lineage) {
  if (intent_ids_.count(intent.client_order_id)) return false;

  OrderIntent attributed_intent = intent;
  const bool closes_existing_position =
      attributed_intent.reduce_only ||
      attributed_intent.purpose == OrderPurpose::kReduce ||
      attributed_intent.purpose == OrderPurpose::kSl ||
      attributed_intent.purpose == OrderPurpose::kTp;
  bool inherited_active_episode = false;
  if (closes_existing_position) {
    const auto episode_it =
        integrator_episode_by_symbol_.find(attributed_intent.symbol);
    if (episode_it != integrator_episode_by_symbol_.end()) {
      const auto& lineage = episode_it->second.lineage;
      attributed_intent.decision_id = lineage.decision_id;
      attributed_intent.candidate_id = lineage.candidate_id;
      attributed_intent.model_version = lineage.model_version;
      attributed_intent.integrator_mode = lineage.mode;
      attributed_intent.position_episode_id = lineage.position_episode_id;
      attributed_intent.integrator_policy_reason = lineage.policy_reason;
      inherited_active_episode = true;
    }
  }
  if (!inherited_active_episode && integrator_lineage != nullptr) {
    attributed_intent.decision_id = integrator_lineage->decision_id;
    attributed_intent.candidate_id = integrator_lineage->candidate_id;
    attributed_intent.model_version = integrator_lineage->model_version;
    attributed_intent.integrator_mode = integrator_lineage->mode;
    attributed_intent.position_episode_id =
        integrator_lineage->position_episode_id;
    attributed_intent.integrator_policy_reason =
        integrator_lineage->policy_reason;
  }

  if (attributed_intent.purpose == OrderPurpose::kEntry &&
      !attributed_intent.reduce_only &&
      HasCandidateIsolationForSymbol(
          attributed_intent.symbol,
          attributed_intent.candidate_id.empty()
              ? ""
              : attributed_intent.position_episode_id)) {
    LogInfo("ORDER_REJECTED_CANARY_ISOLATION: symbol=" +
            attributed_intent.symbol +
            ", client_order_id=" + attributed_intent.client_order_id +
            ", reason=" +
            std::string(attributed_intent.candidate_id.empty()
                            ? "baseline_entry_during_candidate_reservation"
                            : "overlapping_candidate_episode"));
    return false;
  }

  if (!oms_.RegisterIntent(attributed_intent)) return false;

  std::string wal_err;
  if (!wal_.AppendIntent(attributed_intent, &wal_err)) {
    LogError("WAL Write Error: " + wal_err);
    oms_.MarkRejected(intent.client_order_id);
    return false;
  }
  intent_ids_.insert(intent.client_order_id);
  persisted_intent_by_id_.insert_or_assign(intent.client_order_id,
                                           attributed_intent);
  if (IsNetPositionOrderPurpose(intent.purpose)) {
    pending_net_order_enqueued_ms_[intent.client_order_id] = CurrentTimestampMs();
  }
  executor_->Submit(attributed_intent);
  ++funnel_window_.intents_enqueued;
  if (config_.execution_strategy_reduce_post_only_timeout_ticks > 0 &&
      attributed_intent.purpose == OrderPurpose::kReduce &&
      attributed_intent.reduce_only &&
      attributed_intent.liquidity_preference == LiquidityPreference::kMaker &&
      active_strategy_reduce_by_symbol_.find(attributed_intent.symbol) ==
          active_strategy_reduce_by_symbol_.end()) {
    const double reference_price =
        attributed_intent.price > 0.0
            ? attributed_intent.price
            : latest_mark_price_by_symbol_[attributed_intent.symbol];
    active_strategy_reduce_by_symbol_[attributed_intent.symbol] =
        StrategyReduceOrderState{
            .lineage_intent = attributed_intent,
            .client_order_id = attributed_intent.client_order_id,
            .remaining_qty = attributed_intent.qty,
            .reference_price = reference_price,
            .created_tick = market_tick_count_,
        };
    LogInfo("STRATEGY_REDUCE_LIFECYCLE_ENQUEUED: symbol=" +
            attributed_intent.symbol + ", client_order_id=" +
            attributed_intent.client_order_id + ", qty=" +
            std::to_string(attributed_intent.qty) + ", timeout_ticks=" +
            std::to_string(
                config_.execution_strategy_reduce_post_only_timeout_ticks));
  }
  if (!attributed_intent.candidate_id.empty()) {
    const IntegratorCandidateLineage persisted_lineage{
        .decision_id = attributed_intent.decision_id,
        .candidate_id = attributed_intent.candidate_id,
        .model_version = attributed_intent.model_version,
        .mode = attributed_intent.integrator_mode,
        .policy_reason = attributed_intent.integrator_policy_reason,
        .position_episode_id = attributed_intent.position_episode_id,
    };
    integrator_lineage_by_intent_id_[intent.client_order_id] =
        persisted_lineage;
    ++funnel_window_.integrator_policy_applied;
    if (persisted_lineage.mode == "canary") {
      ++funnel_window_.integrator_policy_canary;
    } else if (persisted_lineage.mode == "active") {
      ++funnel_window_.integrator_policy_active;
    }
    const std::string lineage =
        "decision_id=" + persisted_lineage.decision_id +
        ", candidate_id=" + persisted_lineage.candidate_id +
        ", model_version=" + persisted_lineage.model_version +
        ", mode=" + persisted_lineage.mode +
        ", position_episode_id=" +
        persisted_lineage.position_episode_id +
        ", client_order_id=" + intent.client_order_id +
        ", symbol=" + intent.symbol;
    LogInfo("INTEGRATOR_POLICY_ENQUEUED: " + lineage);
    // Compatibility event: its semantics are now explicitly WAL-persisted and enqueued.
    LogInfo("INTEGRATOR_POLICY_APPLIED: stage=enqueued, " + lineage);
  }
  if (IsTrendCandidateProbeIntent(intent.client_order_id)) {
    const auto attempt_it =
        candidate_probe_attempt_by_intent_id_.find(intent.client_order_id);
    const int attempts =
        attempt_it != candidate_probe_attempt_by_intent_id_.end()
            ? std::max(0, attempt_it->second)
            : 0;
    const auto fallback_it =
        candidate_probe_taker_fallback_by_intent_id_.find(intent.client_order_id);
    const bool taker_fallback =
        fallback_it != candidate_probe_taker_fallback_by_intent_id_.end() &&
        fallback_it->second;
    const double reference_price =
        intent.price > 0.0 ? intent.price
                           : latest_mark_price_by_symbol_[intent.symbol];
    const double notional_usd =
        std::fabs(intent.qty) *
        (reference_price > 0.0 ? reference_price : 0.0);
    double trend_threshold_ratio = 0.0;
    if (const auto regime_it = regime_state_by_symbol_.find(intent.symbol);
        regime_it != regime_state_by_symbol_.end()) {
      trend_threshold_ratio = regime_it->second.trend_threshold_ratio;
    }
    active_candidate_probe_by_symbol_[intent.symbol] = CandidateProbeOrderState{
        .symbol = intent.symbol,
        .client_order_id = intent.client_order_id,
        .direction = intent.direction,
        .qty = intent.qty,
        .notional_usd = notional_usd,
        .reference_price = reference_price,
        .trend_threshold_ratio = trend_threshold_ratio,
        .created_tick = market_tick_count_,
        .attempts = attempts,
        .taker_fallback_used = taker_fallback,
    };
    candidate_probe_attempt_by_intent_id_.erase(intent.client_order_id);
    candidate_probe_taker_fallback_by_intent_id_.erase(intent.client_order_id);
    if (config_.execution_candidate_probe_cooldown_ticks > 0) {
      candidate_probe_cooldown_until_tick_by_symbol_[intent.symbol] =
          market_tick_count_ + config_.execution_candidate_probe_cooldown_ticks;
    }
    ++funnel_window_.candidate_probe_enqueued;
    LogInfo("TREND_CANDIDATE_PROBE_ENQUEUED: symbol=" + intent.symbol +
            ", client_order_id=" + intent.client_order_id +
            ", direction=" + std::to_string(intent.direction) +
            ", qty=" + std::to_string(intent.qty) +
            ", price=" + std::to_string(intent.price) +
            ", attempts=" + std::to_string(attempts) +
            ", taker_fallback=" +
            std::string(taker_fallback ? "true" : "false"));
  }
  return true;
}

/**
 * @brief 处理异步执行结果
 *
 * 对关键保护单（SL）做升级处理：
 * - 若 require_sl=true 且 SL 提交失败，立即进入强制只减仓（reduce-only）。
 */
void BotApplication::ProcessAsyncResults() {
  std::vector<AsyncResult> results;
  executor_->PollResults(&results);
  for (const auto& res : results) {
    if (res.is_cancel) {
      const auto* record = oms_.Find(res.client_order_id);
      const bool net_position_order =
          record != nullptr &&
          IsNetPositionOrderPurpose(record->intent.purpose);
      if (res.success) {
        if (record != nullptr &&
            !record->intent.candidate_id.empty() &&
            record->intent.purpose == OrderPurpose::kEntry) {
          candidate_isolation_grace_until_tick_by_symbol_[
              record->intent.symbol] = market_tick_count_ + 12;
        }
        if (net_position_order && config_.mode != "replay") {
          // REST cancel 成功只代表交易所已受理。继续阻塞新净仓位订单，
          // 直到远端活动订单消失且迟到成交观察窗口结束。
          oms_.MarkCancelConfirmed(res.client_order_id);
          pending_net_order_enqueued_ms_[res.client_order_id] =
              CurrentTimestampMs();
        } else {
          oms_.MarkCancelled(res.client_order_id);
          pending_net_order_enqueued_ms_.erase(res.client_order_id);
          integrator_lineage_by_intent_id_.erase(res.client_order_id);
          OnCandidateProbeCancelResult(res.client_order_id, true);
          OnStrategyReduceCancelResult(res.client_order_id, true);
        }
      } else {
        oms_.MarkCancelFailed(res.client_order_id);
        if (net_position_order) {
          // 保留原始入队时间。若每次撤单失败都重置该时间，
          // 110001 等模糊结果会使陈旧单永远无法达到对账超时。
          pending_net_order_enqueued_ms_.try_emplace(
              res.client_order_id, CurrentTimestampMs());
        }
        LogError("Async Cancel Failed: " + res.error +
                 ", client_order_id=" + res.client_order_id);
        if (replay_terminal_settlement_started_) {
          replay_terminal_settlement_failed_ = true;
        }
        OnCandidateProbeCancelResult(res.client_order_id, false);
        OnStrategyReduceCancelResult(res.client_order_id, false);
      }
      continue;
    }

    if (res.success) {
      oms_.MarkSent(res.client_order_id);
      const auto* record = oms_.Find(res.client_order_id);
      if (record != nullptr && record->intent.purpose == OrderPurpose::kSl) {
        ClearPendingRequiredSl(res.client_order_id);
      }
      ++funnel_window_.async_submit_ok;
    } else {
      oms_.MarkRejected(res.client_order_id);
      const auto* rejected_record = oms_.Find(res.client_order_id);
      if (rejected_record != nullptr &&
          !rejected_record->intent.candidate_id.empty() &&
          rejected_record->intent.purpose == OrderPurpose::kEntry) {
        candidate_isolation_grace_until_tick_by_symbol_[
            rejected_record->intent.symbol] = market_tick_count_ + 12;
      }
      pending_net_order_enqueued_ms_.erase(res.client_order_id);
      integrator_lineage_by_intent_id_.erase(res.client_order_id);
      if (IsTrendCandidateProbeIntent(res.client_order_id)) {
        for (auto it = active_candidate_probe_by_symbol_.begin();
             it != active_candidate_probe_by_symbol_.end(); ++it) {
          if (it->second.client_order_id == res.client_order_id) {
            active_candidate_probe_by_symbol_.erase(it);
            break;
          }
        }
      }
      ++funnel_window_.async_submit_failed;
      LogError("Async Submit Failed: " + res.error);
      if (replay_terminal_close_order_ids_.count(res.client_order_id) != 0U) {
        replay_terminal_settlement_failed_ = true;
      }

      const auto* record = oms_.Find(res.client_order_id);
      if (record != nullptr && record->intent.purpose == OrderPurpose::kSl) {
        ClearPendingRequiredSl(res.client_order_id);
      }

      // 关键保护单失败触发只减仓，并输出标准审计事件码。
      if (record && record->intent.purpose == OrderPurpose::kSl &&
          config_.protection.enabled && config_.protection.require_sl) {
        protection_forced_reduce_only_ = true;
        RefreshReduceOnlyMode();
        LogError("EXEC_PROTECTIVE_ORDER_MISSING: reason=sl_submit_failed"
                 ", parent_order_id=" + record->intent.parent_order_id +
                 ", sl_client_order_id=" + res.client_order_id +
                 ", error=" + res.error +
                 ", forcing=reduce_only");
      } else if (record && record->intent.purpose == OrderPurpose::kTp &&
                 config_.protection.enabled && config_.protection.enable_tp) {
        LogError("EXEC_TP_ATTACH_FAILED: reason=tp_submit_failed"
                 ", parent_order_id=" + record->intent.parent_order_id +
                 ", tp_client_order_id=" + res.client_order_id +
                 ", error=" + res.error);
      }
    }
  }
}

/**
 * @brief 处理成交回报
 *
 * 先持久化再推进内存状态，保证崩溃恢复一致性：
 * - dedup(fill_id)；
 * - AppendFill(WAL)；
 * - OMS/Account/Gate 更新；
 * - 触发保护单逻辑。
 */
void BotApplication::ProcessFillEvent(const FillEvent& fill) {
  if (fill_ids_.count(fill.fill_id)) {
    LogInfo("FILL_DUPLICATE_DROP: stage=fill_id_dedupe, " +
            FormatFillSummary(fill));
    return;
  }
  if (fill.client_order_id.empty()) {
    if (!fill.fill_id.empty()) {
      fill_ids_.insert(fill.fill_id);
    }
    LogError("FILL_UNMAPPED_DROP: stage=empty_client_order_id, " +
             FormatFillSummary(fill) +
             ", reason=missing_orderLinkId_and_orderId");
    return;
  }

  const OrderRecord* fill_order_record_before = oms_.Find(fill.client_order_id);
  const double local_qty_before = system_.account().position_qty(fill.symbol);
  const double avg_entry_price_before =
      system_.account().avg_entry_price(fill.symbol);
  const double oms_net_qty_before = oms_.net_filled_qty(fill.symbol);
  const double order_filled_qty_before =
      fill_order_record_before != nullptr ? fill_order_record_before->filled_qty
                                         : 0.0;
  const OrderState order_state_before =
      fill_order_record_before != nullptr ? fill_order_record_before->state
                                          : OrderState::kNew;
  if (fill_order_record_before != nullptr &&
      fill_order_record_before->intent.qty > kFillQtyAuditEpsilon) {
    const double projected_filled_qty = order_filled_qty_before + fill.qty;
    const double tolerance_qty = FillOverrunToleranceQty(*fill_order_record_before);
    if (projected_filled_qty >
        fill_order_record_before->intent.qty + tolerance_qty) {
      LogError("FILL_OVERFILL_DROP: " + FormatFillSummary(fill) +
               ", order_state=" + OrderStateToString(order_state_before) +
               ", order_qty=" +
               std::to_string(fill_order_record_before->intent.qty) +
               ", filled_qty_before=" + std::to_string(order_filled_qty_before) +
               ", projected_filled_qty=" + std::to_string(projected_filled_qty) +
               ", tolerance_qty=" + std::to_string(tolerance_qty) +
               ", local_qty_before=" + std::to_string(local_qty_before) +
               ", oms_net_qty_before=" + std::to_string(oms_net_qty_before));
      return;
    }
  }

  const bool account_already_reflects_fill = AccountAlreadyReflectsFill(
      fill, fill_order_record_before, local_qty_before, oms_net_qty_before);
  double attribution_qty_before = local_qty_before;
  double attribution_avg_entry_price_before = avg_entry_price_before;
  if (account_already_reflects_fill) {
    attribution_qty_before = oms_net_qty_before;
    if (std::fabs(attribution_qty_before) > kNotionalEpsilon &&
        (!std::isfinite(attribution_avg_entry_price_before) ||
         attribution_avg_entry_price_before <= kNotionalEpsilon ||
         SignOf(local_qty_before) != SignOf(attribution_qty_before))) {
      if (const auto protection_it =
              managed_protection_by_symbol_.find(fill.symbol);
          protection_it != managed_protection_by_symbol_.end() &&
          protection_it->second.avg_entry_price > kNotionalEpsilon) {
        attribution_avg_entry_price_before =
            protection_it->second.avg_entry_price;
      }
    }
  }

  std::string wal_err;
  if (!wal_.AppendFill(fill, &wal_err)) {
    LogError("WAL Fill Error: " + wal_err);
    return;
  }
  fill_ids_.insert(fill.fill_id);
  oms_.OnFill(fill);
  if (account_already_reflects_fill) {
    system_.OnReflectedFill(fill,
                            attribution_qty_before,
                            attribution_avg_entry_price_before);
    LogInfo("FILL_ACCOUNT_ALREADY_REFLECTED: " + FormatFillSummary(fill) +
            ", local_qty_before=" + std::to_string(local_qty_before) +
            ", oms_net_qty_before=" + std::to_string(oms_net_qty_before) +
            ", attribution_qty_before=" +
            std::to_string(attribution_qty_before) +
            ", attribution_avg_entry_price_before=" +
            std::to_string(attribution_avg_entry_price_before));
  } else {
    system_.OnFill(fill);
  }
  gate_monitor_.OnFill(fill);
  const auto* fill_order_record = oms_.Find(fill.client_order_id);
  const auto persisted_intent_it =
      persisted_intent_by_id_.find(fill.client_order_id);
  const OrderIntent* attributed_fill_intent =
      fill_order_record != nullptr
          ? &fill_order_record->intent
          : (persisted_intent_it != persisted_intent_by_id_.end()
                 ? &persisted_intent_it->second
                 : nullptr);
  const bool candidate_probe_fill =
      IsTrendCandidateProbeIntent(fill.client_order_id);
  const double local_qty_after = system_.account().position_qty(fill.symbol);
  const double oms_net_qty_after = oms_.net_filled_qty(fill.symbol);
  const double order_filled_qty_after =
      fill_order_record != nullptr ? fill_order_record->filled_qty : 0.0;
  const char* order_state_after =
      fill_order_record != nullptr ? OrderStateToString(fill_order_record->state)
                                   : "missing";
  LogInfo("FILL_APPLIED: " + FormatFillSummary(fill) +
          ", order_state_before=" + OrderStateToString(order_state_before) +
          ", order_state_after=" + std::string(order_state_after) +
          ", order_filled_qty_before=" + std::to_string(order_filled_qty_before) +
          ", order_filled_qty_after=" + std::to_string(order_filled_qty_after) +
          ", local_qty_before=" + std::to_string(local_qty_before) +
          ", avg_entry_price_before=" +
          std::to_string(avg_entry_price_before) +
          ", local_qty_after=" + std::to_string(local_qty_after) +
          ", oms_net_qty_before=" + std::to_string(oms_net_qty_before) +
          ", oms_net_qty_after=" + std::to_string(oms_net_qty_after) +
          ", account_already_reflected=" +
          std::string(account_already_reflects_fill ? "true" : "false"));
  if (attributed_fill_intent != nullptr &&
      !attributed_fill_intent->candidate_id.empty()) {
    candidate_isolation_grace_until_tick_by_symbol_.erase(fill.symbol);
    const IntegratorCandidateLineage lineage{
        .decision_id = attributed_fill_intent->decision_id,
        .candidate_id = attributed_fill_intent->candidate_id,
        .model_version = attributed_fill_intent->model_version,
        .mode = attributed_fill_intent->integrator_mode,
        .policy_reason = attributed_fill_intent->integrator_policy_reason,
        .position_episode_id =
            attributed_fill_intent->position_episode_id,
    };
    ++funnel_window_.integrator_policy_filled;
    LogInfo("INTEGRATOR_POLICY_FILLED: decision_id=" +
            lineage.decision_id +
            ", candidate_id=" + lineage.candidate_id +
            ", model_version=" + lineage.model_version +
            ", mode=" + lineage.mode +
            ", position_episode_id=" + lineage.position_episode_id +
            ", client_order_id=" + fill.client_order_id +
            ", fill_id=" + fill.fill_id +
            ", symbol=" + fill.symbol +
            ", qty=" + std::to_string(fill.qty) +
            ", price=" + std::to_string(fill.price) +
            ", fee=" + std::to_string(fill.fee) +
            ", liquidity=" + std::string(ToString(fill.liquidity)));
  }
  if (attributed_fill_intent != nullptr &&
      attributed_fill_intent->purpose == OrderPurpose::kSl) {
    ClearPendingRequiredSl(fill.client_order_id);
  }
  if (candidate_probe_fill) {
    if (const auto active_it =
            active_candidate_probe_by_symbol_.find(fill.symbol);
        active_it != active_candidate_probe_by_symbol_.end() &&
        active_it->second.client_order_id == fill.client_order_id) {
      active_candidate_probe_by_symbol_.erase(active_it);
    }
    if (std::fabs(local_qty_after) > kNotionalEpsilon) {
      candidate_probe_position_entry_tick_by_symbol_[fill.symbol] =
          market_tick_count_;
    }
  }
  if (auto reduce_it = active_strategy_reduce_by_symbol_.find(fill.symbol);
      reduce_it != active_strategy_reduce_by_symbol_.end() &&
      reduce_it->second.client_order_id == fill.client_order_id) {
    reduce_it->second.remaining_qty =
        std::max(0.0, reduce_it->second.remaining_qty - std::fabs(fill.qty));
    if (reduce_it->second.remaining_qty <= kNotionalEpsilon ||
        std::fabs(local_qty_after) <= kNotionalEpsilon) {
      active_strategy_reduce_by_symbol_.erase(reduce_it);
    }
  }
  if (std::fabs(local_qty_after) <= kNotionalEpsilon) {
    candidate_probe_position_entry_tick_by_symbol_.erase(fill.symbol);
  }
  if (const auto* record = oms_.Find(fill.client_order_id);
      record != nullptr && OrderManager::IsTerminalState(record->state)) {
    pending_net_order_enqueued_ms_.erase(fill.client_order_id);
    integrator_lineage_by_intent_id_.erase(fill.client_order_id);
  }
  // 记录最近成交 tick，供对账阶段应用短暂宽限窗口。
  last_fill_tick_ = market_tick_count_;
  RememberRecentFillForReconcile(fill);
  ++funnel_window_.fills_applied;
  ++pending_fills_for_evolution_;
  if (std::isfinite(fill.price) && fill.price > 0.0 && std::isfinite(fill.qty)) {
    const double fill_notional_abs_usd = std::fabs(fill.price * std::fabs(fill.qty));
    funnel_window_.fills_notional_abs_usd_sum += fill_notional_abs_usd;
    if (candidate_probe_fill) {
      ++funnel_window_.candidate_probe_fills;
      LogInfo("TREND_CANDIDATE_PROBE_FILL: " + FormatFillSummary(fill) +
              ", notional_abs_usd=" + std::to_string(fill_notional_abs_usd));
    }
    const bool entry_fill =
        attributed_fill_intent != nullptr &&
        attributed_fill_intent->purpose == OrderPurpose::kEntry &&
        !attributed_fill_intent->reduce_only;
    const bool symbol_quality_fill =
        attributed_fill_intent != nullptr &&
        (attributed_fill_intent->purpose == OrderPurpose::kEntry ||
         attributed_fill_intent->purpose == OrderPurpose::kReduce ||
         attributed_fill_intent->purpose == OrderPurpose::kTp ||
         attributed_fill_intent->purpose == OrderPurpose::kSl);
    const double realized_pnl_usd =
        EstimateFillRealizedPnlUsd(attribution_qty_before,
                                   attribution_avg_entry_price_before,
                                   fill);
    if (attributed_fill_intent != nullptr &&
        !attributed_fill_intent->candidate_id.empty()) {
      const OrderIntent& attributed_intent = *attributed_fill_intent;
      auto episode_it =
          integrator_episode_by_symbol_.find(fill.symbol);
      if (entry_fill &&
          (episode_it == integrator_episode_by_symbol_.end() ||
           episode_it->second.lineage.position_episode_id !=
               attributed_intent.position_episode_id)) {
        IntegratorCandidateEpisode episode;
        episode.lineage = IntegratorCandidateLineage{
            .decision_id = attributed_intent.decision_id,
            .candidate_id = attributed_intent.candidate_id,
            .model_version = attributed_intent.model_version,
            .mode = attributed_intent.integrator_mode,
            .policy_reason = attributed_intent.integrator_policy_reason,
            .position_episode_id = attributed_intent.position_episode_id,
        };
        episode.entry_observed_from_flat =
            std::fabs(local_qty_before) <= kNotionalEpsilon;
        episode_it =
            integrator_episode_by_symbol_
                .insert_or_assign(fill.symbol, std::move(episode))
                .first;
      }
      if (episode_it != integrator_episode_by_symbol_.end() &&
          episode_it->second.lineage.position_episode_id ==
              attributed_intent.position_episode_id) {
        IntegratorCandidateEpisode& episode = episode_it->second;
        const double episode_qty_before = episode.signed_open_qty;
        ApplyCandidateEpisodeFill(&episode, fill);
        if (std::fabs(episode_qty_before) > kNotionalEpsilon &&
            std::fabs(episode.signed_open_qty) <= kNotionalEpsilon) {
          if (!RecordCandidateEpisodeClosure(fill.symbol, episode, false)) {
            evidence_persistence_failed_ = true;
            RefreshReduceOnlyMode();
          }
          integrator_episode_by_symbol_.erase(episode_it);
        }
      }
    }
    constexpr double kFeeSignEpsilon = 1e-12;
    const bool explicit_liquidity =
        fill.liquidity == FillLiquidity::kMaker ||
        fill.liquidity == FillLiquidity::kTaker;
    const bool fallback_by_fee =
        fill.liquidity == FillLiquidity::kUnknown &&
        (fill.fee < -kFeeSignEpsilon || fill.fee > kFeeSignEpsilon);
    const bool maker_fill =
        fill.liquidity == FillLiquidity::kMaker ||
        (fill.liquidity == FillLiquidity::kUnknown &&
         fill.fee < -kFeeSignEpsilon);
    const bool taker_fill =
        fill.liquidity == FillLiquidity::kTaker ||
        (fill.liquidity == FillLiquidity::kUnknown &&
         fill.fee > kFeeSignEpsilon);
    if (explicit_liquidity) {
      ++funnel_window_.fills_explicit_liquidity_count;
    } else if (fallback_by_fee) {
      ++funnel_window_.fills_fee_sign_fallback_count;
    }
    if (maker_fill) {
      ++funnel_window_.fills_maker_count;
      funnel_window_.fills_maker_fee_usd_sum += fill.fee;
      funnel_window_.fills_maker_notional_abs_usd_sum += fill_notional_abs_usd;
    } else if (taker_fill) {
      ++funnel_window_.fills_taker_count;
      funnel_window_.fills_taker_fee_usd_sum += fill.fee;
      funnel_window_.fills_taker_notional_abs_usd_sum += fill_notional_abs_usd;
    } else {
      ++funnel_window_.fills_unknown_liquidity_count;
    }
    if (symbol_quality_fill) {
      auto& symbol_quality =
          funnel_window_.symbol_fill_quality_by_symbol[fill.symbol];
      ++symbol_quality.fills;
      if (attributed_fill_intent->purpose != OrderPurpose::kEntry ||
          std::fabs(realized_pnl_usd) > kFillQtyAuditEpsilon) {
        ++symbol_quality.net_quality_fills;
      }
      symbol_quality.fee_usd_sum += fill.fee;
      symbol_quality.realized_net_sum_usd += realized_pnl_usd - fill.fee;
      symbol_quality.notional_abs_usd_sum += fill_notional_abs_usd;
    }
    if (fill_order_record != nullptr) {
      LogExitCaptureSample(fill,
                           *fill_order_record,
                           attribution_qty_before,
                           attribution_avg_entry_price_before,
                           realized_pnl_usd);
    }
    if (entry_fill) {
      if (maker_fill && config_.execution_quality_guard_enabled) {
        execution_quality_by_symbol_[fill.symbol].last_maker_entry_tick =
            market_tick_count_;
      }
      ++funnel_window_.entry_fills_applied;
      funnel_window_.entry_fills_notional_abs_usd_sum += fill_notional_abs_usd;
      auto& symbol_quality =
          funnel_window_.entry_fill_quality_by_symbol[fill.symbol];
      ++symbol_quality.fills;
      symbol_quality.fee_usd_sum += fill.fee;
      symbol_quality.realized_net_sum_usd -= fill.fee;
      symbol_quality.notional_abs_usd_sum += fill_notional_abs_usd;
      if (explicit_liquidity) {
        ++funnel_window_.entry_fills_explicit_liquidity_count;
      } else if (fallback_by_fee) {
        ++funnel_window_.entry_fills_fee_sign_fallback_count;
      }
      if (maker_fill) {
        ++funnel_window_.entry_fills_maker_count;
        funnel_window_.entry_fills_maker_fee_usd_sum += fill.fee;
        funnel_window_.entry_fills_maker_notional_abs_usd_sum +=
            fill_notional_abs_usd;
      } else if (taker_fill) {
        ++funnel_window_.entry_fills_taker_count;
        funnel_window_.entry_fills_taker_fee_usd_sum += fill.fee;
        funnel_window_.entry_fills_taker_notional_abs_usd_sum +=
            fill_notional_abs_usd;
      } else {
        ++funnel_window_.entry_fills_unknown_liquidity_count;
      }
    }
  }

  HandleProtectionOrders(fill);
}

void BotApplication::RememberRecentFillForReconcile(const FillEvent& fill) {
  const double signed_qty = static_cast<double>(fill.direction) * fill.qty;
  if (fill.symbol.empty() || std::fabs(signed_qty) <= kNotionalEpsilon) {
    return;
  }
  recent_fill_observations_.push_back(RecentFillObservation{
      .symbol = fill.symbol,
      .signed_qty = signed_qty,
      .abs_notional_usd = std::fabs(fill.price * std::fabs(fill.qty)),
      .tick = market_tick_count_,
  });
  PruneRecentFillObservations(market_tick_count_);
  while (recent_fill_observations_.size() > kRecentFillObservationLimit) {
    recent_fill_observations_.pop_front();
  }
}

void BotApplication::PruneRecentFillObservations(int current_tick) {
  while (!recent_fill_observations_.empty() &&
         current_tick - recent_fill_observations_.front().tick >
             kReconcileFillLagExplainGraceTicks) {
    recent_fill_observations_.pop_front();
  }
}

bool BotApplication::RecentFillsExplainReconcileMismatch(
    const std::vector<RemotePositionSnapshot>& remote_positions,
    std::string* out_explanation) const {
  const auto deltas = CollectSymbolQtyDeltas(system_.account(),
                                             remote_positions,
                                             tracked_symbols_,
                                             /*min_abs_notional_delta_usd=*/1.0);
  if (deltas.empty() || recent_fill_observations_.empty()) {
    return false;
  }

  std::ostringstream explanation;
  bool first = true;
  for (const auto& delta : deltas) {
    if (std::fabs(delta.delta_qty) <= kNotionalEpsilon) {
      continue;
    }

    double matched_qty = 0.0;
    double matched_abs_notional = 0.0;
    int matched_count = 0;
    for (auto it = recent_fill_observations_.rbegin();
         it != recent_fill_observations_.rend(); ++it) {
      if (market_tick_count_ - it->tick > kReconcileFillLagExplainGraceTicks) {
        continue;
      }
      if (it->symbol != delta.symbol ||
          SignOf(it->signed_qty) != SignOf(delta.delta_qty)) {
        continue;
      }
      matched_qty += it->signed_qty;
      matched_abs_notional += it->abs_notional_usd;
      ++matched_count;
      const double qty_tolerance =
          std::max(1e-6, std::fabs(delta.delta_qty) * 0.02);
      if (std::fabs(std::fabs(matched_qty) - std::fabs(delta.delta_qty)) <=
          qty_tolerance) {
        break;
      }
    }

    const double qty_tolerance =
        std::max(1e-6, std::fabs(delta.delta_qty) * 0.02);
    const double notional_tolerance =
        std::max(1.0, std::fabs(delta.delta_notional_usd) * 0.15);
    if (matched_count == 0 ||
        std::fabs(matched_qty - delta.delta_qty) > qty_tolerance ||
        matched_abs_notional + notional_tolerance <
            std::fabs(delta.delta_notional_usd)) {
      return false;
    }

    if (!first) {
      explanation << ";";
    }
    first = false;
    explanation << delta.symbol << "{delta_qty=" << delta.delta_qty
                << ", matched_recent_fill_qty=" << matched_qty
                << ", matched_recent_fill_count=" << matched_count << "}";
  }

  if (first) {
    return false;
  }
  if (out_explanation != nullptr) {
    *out_explanation = explanation.str();
  }
  return true;
}

void BotApplication::LogExitCaptureSample(const FillEvent& fill,
                                          const OrderRecord& record,
                                          double position_qty_before,
                                          double avg_entry_price_before,
                                          double realized_pnl_usd) {
  if (fill.symbol.empty() || fill.direction == 0 || fill.qty <= 0.0 ||
      fill.price <= 0.0 || avg_entry_price_before <= 0.0 ||
      !std::isfinite(position_qty_before) ||
      !std::isfinite(avg_entry_price_before) ||
      !std::isfinite(realized_pnl_usd)) {
    return;
  }

  const int entry_direction = SignOf(position_qty_before);
  if (entry_direction == 0 || entry_direction == fill.direction) {
    return;
  }

  const double close_qty =
      std::min(std::fabs(position_qty_before), std::fabs(fill.qty));
  if (close_qty <= kNotionalEpsilon) {
    return;
  }
  const double close_qty_ratio =
      std::clamp(close_qty / std::max(fill.qty, kNotionalEpsilon), 0.0, 1.0);
  const double fee_for_closed_qty = fill.fee * close_qty_ratio;
  const double entry_notional_abs_usd = close_qty * avg_entry_price_before;
  if (entry_notional_abs_usd <= kNotionalEpsilon) {
    return;
  }

  const auto state_it = managed_protection_by_symbol_.find(fill.symbol);
  const bool has_protection_state =
      state_it != managed_protection_by_symbol_.end() &&
      state_it->second.avg_entry_price > 0.0;
  const double best_price =
      has_protection_state && state_it->second.best_price > 0.0
          ? state_it->second.best_price
          : avg_entry_price_before;
  const double path_mfe_bps =
      std::max(0.0, FavorableReturn(entry_direction,
                                    avg_entry_price_before,
                                    best_price)) *
      10000.0;
  const double captured_gross_bps =
      FavorableReturn(entry_direction, avg_entry_price_before, fill.price) *
      10000.0;
  const double realized_net_usd = realized_pnl_usd - fee_for_closed_qty;
  const double captured_net_bps =
      realized_net_usd / entry_notional_abs_usd * 10000.0;
  const double fee_bps =
      std::fabs(fee_for_closed_qty) / entry_notional_abs_usd * 10000.0;
  const double capture_ratio =
      path_mfe_bps > 1e-9 ? captured_gross_bps / path_mfe_bps : 0.0;
  const double round_trip_cost_bps = RoundTripCostBps();
  const bool low_capture =
      path_mfe_bps > std::max(round_trip_cost_bps, 1.0) &&
      (capture_ratio < 0.20 || captured_net_bps < 0.0);
  const int holding_ticks =
      has_protection_state
          ? std::max(0, market_tick_count_ - state_it->second.entry_tick)
          : 0;

  LogInfo("EXIT_CAPTURE_SAMPLE: symbol=" + fill.symbol +
          ", client_order_id=" + fill.client_order_id +
          ", purpose=" + std::string(OrderPurposeToString(record.intent.purpose)) +
          ", entry_direction=" + std::to_string(entry_direction) +
          ", close_qty=" + std::to_string(close_qty) +
          ", avg_entry_price=" + std::to_string(avg_entry_price_before) +
          ", exit_price=" + std::to_string(fill.price) +
          ", best_price=" + std::to_string(best_price) +
          ", path_mfe_bps=" + std::to_string(path_mfe_bps) +
          ", captured_gross_bps=" + std::to_string(captured_gross_bps) +
          ", captured_net_bps=" + std::to_string(captured_net_bps) +
          ", fee_bps=" + std::to_string(fee_bps) +
          ", capture_ratio=" + std::to_string(capture_ratio) +
          ", low_capture=" + std::string(low_capture ? "true" : "false") +
          ", realized_pnl_usd=" + std::to_string(realized_pnl_usd) +
          ", realized_net_usd=" + std::to_string(realized_net_usd) +
          ", round_trip_cost_bps=" + std::to_string(round_trip_cost_bps) +
          ", holding_ticks=" + std::to_string(holding_ticks) +
          ", protection_state=" +
          std::string(has_protection_state ? "true" : "false"));
}

/**
 * @brief 保护单编排（symbol 级持仓 -> 动态 SL/TP -> 盈利保护）
 */
void BotApplication::HandleProtectionOrders(const FillEvent& fill) {
  const auto* record = oms_.Find(fill.client_order_id);
  if (!record) return;

  // 保护单任一侧成交后撤销另一侧，避免重复平仓（OCO）。
  if (record->intent.purpose == OrderPurpose::kSl ||
      record->intent.purpose == OrderPurpose::kTp) {
    auto sibling = oms_.FindOpenProtectiveSibling(record->intent.parent_order_id,
                                                  record->intent.purpose);
    if (sibling) {
      executor_->Cancel(*sibling);
      oms_.MarkCancelled(*sibling);
      ClearPendingRequiredSl(*sibling);
    }
  }

  if (!config_.protection.enabled) {
    return;
  }

  if (record->intent.purpose == OrderPurpose::kEntry ||
      record->intent.purpose == OrderPurpose::kReduce ||
      record->intent.purpose == OrderPurpose::kSl ||
      record->intent.purpose == OrderPurpose::kTp) {
    RefreshManagedProtectionForSymbol(
        fill.symbol,
        fill.price,
        record->intent.purpose == OrderPurpose::kEntry
            ? "entry_fill"
            : (record->intent.purpose == OrderPurpose::kReduce
                   ? "strategy_exit_fill"
                   : (record->intent.purpose == OrderPurpose::kSl
                          ? "stop_loss_fill"
                          : "take_profit_fill")));
  }
}

void BotApplication::RefreshManagedProtectionForSymbol(
    const std::string& symbol,
    double reference_price,
    const std::string& reason) {
  if (!config_.protection.enabled || symbol.empty()) {
    return;
  }

  const double position_qty = system_.account().position_qty(symbol);
  if (std::fabs(position_qty) <= kNotionalEpsilon) {
    CancelManagedProtectionForSymbol(symbol, reason + "_flat");
    return;
  }

  const double avg_entry_price = system_.account().avg_entry_price(symbol);
  if (!std::isfinite(avg_entry_price) || avg_entry_price <= 0.0) {
    return;
  }

  const double market_price =
      (latest_mark_price_by_symbol_.count(symbol) > 0 &&
       latest_mark_price_by_symbol_.at(symbol) > 0.0)
          ? latest_mark_price_by_symbol_.at(symbol)
          : reference_price;
  const int direction = SignOf(position_qty);
  if (const auto existing = managed_protection_by_symbol_.find(symbol);
      existing != managed_protection_by_symbol_.end() &&
      existing->second.direction != 0 &&
      existing->second.direction != direction) {
    CancelManagedProtectionForSymbol(symbol, reason + "_direction_flip");
  }
  auto& state = managed_protection_by_symbol_[symbol];
  const bool new_group =
      state.protection_group_id.empty() || state.direction != direction;
  if (new_group) {
    state = ManagedProtectionState{};
    state.symbol = symbol;
    state.protection_group_id = BuildProtectionGroupId(symbol);
    state.direction = direction;
    state.best_price = market_price > 0.0 ? market_price : avg_entry_price;
    state.entry_tick = market_tick_count_;
  }

  if (market_price > 0.0) {
    if (state.direction > 0) {
      state.best_price = std::max(state.best_price, market_price);
    } else {
      if (state.best_price <= 0.0) {
        state.best_price = market_price;
      } else {
        state.best_price = std::min(state.best_price, market_price);
      }
    }
  }

  const bool qty_changed = std::fabs(state.qty - std::fabs(position_qty)) > 1e-9;
  const bool price_changed =
      std::fabs(state.avg_entry_price - avg_entry_price) >
      std::max(1e-6, avg_entry_price * 1e-6);
  const bool need_attach =
      new_group || qty_changed || price_changed ||
      state.active_sl_client_order_id.empty() ||
      (config_.protection.enable_tp && state.active_tp_client_order_id.empty());
  if (!need_attach) {
    return;
  }

  if (!state.active_sl_client_order_id.empty()) {
    if (const auto* sl_record = oms_.Find(state.active_sl_client_order_id);
        sl_record != nullptr && !OrderManager::IsTerminalState(sl_record->state)) {
      if (executor_ != nullptr) {
        executor_->Cancel(state.active_sl_client_order_id);
      }
      oms_.MarkCancelled(state.active_sl_client_order_id);
    }
    ClearPendingRequiredSl(state.active_sl_client_order_id);
    state.active_sl_client_order_id.clear();
    state.active_sl_price = 0.0;
  }
  if (!state.active_tp_client_order_id.empty()) {
    if (const auto* tp_record = oms_.Find(state.active_tp_client_order_id);
        tp_record != nullptr && !OrderManager::IsTerminalState(tp_record->state)) {
      if (executor_ != nullptr) {
        executor_->Cancel(state.active_tp_client_order_id);
      }
      oms_.MarkCancelled(state.active_tp_client_order_id);
    }
    state.active_tp_client_order_id.clear();
    state.active_tp_price = 0.0;
  }

  const auto regime_it = regime_state_by_symbol_.find(symbol);
  const double dynamic_distance_ratio =
      (config_.protection.dynamic_distance_enabled &&
       config_.protection.dynamic_distance_volatility_multiplier > 0.0 &&
       regime_it != regime_state_by_symbol_.end())
          ? std::max(0.0,
                     regime_it->second.volatility_level *
                         config_.protection.dynamic_distance_volatility_multiplier)
          : 0.0;
  const double stop_loss_ratio = ComputeEffectiveProtectionDistanceRatio(
      config_.protection.stop_loss_ratio,
      dynamic_distance_ratio,
      config_.protection.dynamic_stop_loss_min_ratio,
      config_.protection.dynamic_stop_loss_max_ratio);
  const double take_profit_dynamic_ratio =
      stop_loss_ratio * config_.protection.dynamic_take_profit_rr_multiplier;
  const double take_profit_ratio = ComputeEffectiveProtectionDistanceRatio(
      config_.protection.take_profit_ratio,
      config_.protection.dynamic_distance_enabled ? take_profit_dynamic_ratio : 0.0,
      config_.protection.dynamic_take_profit_min_ratio,
      config_.protection.dynamic_take_profit_max_ratio);

  FillEvent synthetic_entry_fill;
  synthetic_entry_fill.client_order_id = state.protection_group_id;
  synthetic_entry_fill.symbol = symbol;
  synthetic_entry_fill.direction = direction;
  synthetic_entry_fill.qty = std::fabs(position_qty);
  synthetic_entry_fill.price = avg_entry_price;

  double initial_sl_price = ComputeProtectionPrice(direction,
                                                   avg_entry_price,
                                                   OrderPurpose::kSl,
                                                   stop_loss_ratio);
  double trailing_distance_ratio = config_.protection.trailing_distance_ratio;
  if (trailing_distance_ratio <= 0.0) {
    trailing_distance_ratio = stop_loss_ratio;
  }
  const auto initial_profit_stop = ComputeProfitProtectionStopPrice(
      direction,
      avg_entry_price,
      state.best_price,
      config_.protection.break_even_enabled,
      config_.protection.break_even_trigger_ratio,
      config_.protection.break_even_offset_ratio,
      config_.protection.trailing_enabled,
      config_.protection.trailing_trigger_ratio,
      trailing_distance_ratio);
  const double initial_reference_price =
      market_price > 0.0 ? market_price : state.best_price;
  const bool initial_profit_crossed =
      initial_profit_stop.has_value() &&
      StopWouldTriggerNow(direction, *initial_profit_stop, initial_reference_price);
  const bool initial_profit_armed =
      initial_profit_stop.has_value() &&
      !initial_profit_crossed &&
      IsTighterStopPrice(direction, *initial_profit_stop, initial_sl_price, 0.0);
  if (initial_profit_armed) {
    initial_sl_price = *initial_profit_stop;
  }
  if (initial_profit_crossed) {
    LogInfo("PROFIT_PROTECTION_CROSSED: symbol=" + symbol +
            ", protection_group_id=" + state.protection_group_id +
            ", reason=initial_refresh" +
            ", best_price=" + std::to_string(state.best_price) +
            ", current_price=" + std::to_string(initial_reference_price) +
            ", candidate_sl_price=" + std::to_string(*initial_profit_stop) +
            ", action=keep_initial_sl");
  }

  auto sl = execution_.BuildProtectionIntentAtPrice(
      synthetic_entry_fill, OrderPurpose::kSl, initial_sl_price);
  bool sl_ok = false;
  if (sl) {
    sl_ok = EnqueueIntent(*sl);
    if (sl_ok && config_.protection.require_sl) {
      TrackPendingRequiredSl(sl->client_order_id, state.protection_group_id);
      state.active_sl_client_order_id = sl->client_order_id;
      state.active_sl_price = sl->price;
    }
  }
  if (initial_profit_armed && sl_ok) {
    LogInfo("PROFIT_PROTECTION_ARMED: symbol=" + symbol +
            ", protection_group_id=" + state.protection_group_id +
            ", reason=initial_refresh" +
            ", best_price=" + std::to_string(state.best_price) +
            ", avg_entry_price=" + std::to_string(avg_entry_price) +
            ", armed_sl_price=" + std::to_string(initial_sl_price) +
            ", path_mfe_bps=" +
            std::to_string(std::max(0.0,
                                    FavorableReturn(direction,
                                                    avg_entry_price,
                                                    state.best_price)) *
                           10000.0) +
            ", break_even_trigger_ratio=" +
            std::to_string(config_.protection.break_even_trigger_ratio) +
            ", trailing_trigger_ratio=" +
            std::to_string(config_.protection.trailing_trigger_ratio));
  }
  if (!sl_ok && config_.protection.require_sl) {
    protection_forced_reduce_only_ = true;
    RefreshReduceOnlyMode();
    LogError("EXEC_PROTECTIVE_ORDER_MISSING: reason=managed_sl_enqueue_failed"
             ", symbol=" + symbol +
             ", protection_group_id=" + state.protection_group_id +
             ", context=" + reason +
             ", forcing=reduce_only");
    return;
  }

  if (config_.protection.enable_tp) {
    auto tp = execution_.BuildProtectionIntent(
        synthetic_entry_fill, OrderPurpose::kTp, take_profit_ratio);
    if (tp) {
      if (EnqueueIntent(*tp)) {
        state.active_tp_client_order_id = tp->client_order_id;
        state.active_tp_price = tp->price;
      } else {
        LogError("EXEC_TP_ATTACH_FAILED: reason=managed_tp_enqueue_failed"
                 ", symbol=" + symbol +
                 ", protection_group_id=" + state.protection_group_id +
                 ", context=" + reason);
      }
    } else {
      LogError("EXEC_TP_ATTACH_FAILED: reason=managed_tp_intent_invalid"
               ", symbol=" + symbol +
               ", protection_group_id=" + state.protection_group_id +
               ", context=" + reason);
    }
  }

  state.qty = std::fabs(position_qty);
  state.avg_entry_price = avg_entry_price;
  state.stop_loss_ratio = stop_loss_ratio;
  state.take_profit_ratio = take_profit_ratio;

  LogInfo("PROTECTION_REFRESH: symbol=" + symbol +
          ", reason=" + reason +
          ", direction=" + std::to_string(direction) +
          ", qty=" + std::to_string(state.qty) +
          ", avg_entry_price=" + std::to_string(state.avg_entry_price) +
          ", stop_loss_ratio=" + std::to_string(state.stop_loss_ratio) +
          ", take_profit_ratio=" + std::to_string(state.take_profit_ratio) +
          ", sl_price=" + std::to_string(state.active_sl_price) +
          ", tp_price=" + std::to_string(state.active_tp_price) +
          ", profit_protection_initial_armed=" +
          std::string(initial_profit_armed ? "true" : "false"));
}

void BotApplication::CancelManagedProtectionForSymbol(
    const std::string& symbol,
    const std::string& reason) {
  auto it = managed_protection_by_symbol_.find(symbol);
  if (it == managed_protection_by_symbol_.end()) {
    return;
  }
  auto& state = it->second;
  if (!state.active_sl_client_order_id.empty()) {
    if (const auto* sl_record = oms_.Find(state.active_sl_client_order_id);
        sl_record != nullptr && !OrderManager::IsTerminalState(sl_record->state)) {
      if (executor_ != nullptr) {
        executor_->Cancel(state.active_sl_client_order_id);
      }
      oms_.MarkCancelled(state.active_sl_client_order_id);
    }
    ClearPendingRequiredSl(state.active_sl_client_order_id);
  }
  if (!state.active_tp_client_order_id.empty()) {
    if (const auto* tp_record = oms_.Find(state.active_tp_client_order_id);
        tp_record != nullptr && !OrderManager::IsTerminalState(tp_record->state)) {
      if (executor_ != nullptr) {
        executor_->Cancel(state.active_tp_client_order_id);
      }
      oms_.MarkCancelled(state.active_tp_client_order_id);
    }
  }
  LogInfo("PROTECTION_CANCELLED: symbol=" + symbol +
          ", reason=" + reason +
          ", protection_group_id=" + state.protection_group_id);
  managed_protection_by_symbol_.erase(it);
  RefreshProtectionReduceOnlyRelease(reason + "_protection_cancelled");
}

void BotApplication::ReconcileProtectionAfterAuthoritativePositionSync(
    const std::string& reason) {
  std::unordered_set<std::string> symbols;
  for (const auto& [symbol, _] : managed_protection_by_symbol_) {
    symbols.insert(symbol);
  }
  for (const auto& symbol : system_.account().GetActiveSymbols()) {
    symbols.insert(symbol);
  }

  // Cancel any live protective order whose symbol is now authoritatively flat,
  // including legacy/orphan orders that are no longer present in the managed
  // protection map.
  for (const auto& client_order_id : oms_.PendingOrderIds()) {
    const OrderRecord* record = oms_.Find(client_order_id);
    if (record == nullptr ||
        (record->intent.purpose != OrderPurpose::kSl &&
         record->intent.purpose != OrderPurpose::kTp) ||
        HasExposure(system_.account().position_qty(record->intent.symbol))) {
      continue;
    }
    if (executor_ != nullptr) {
      executor_->Cancel(client_order_id);
    }
    oms_.MarkCancelled(client_order_id);
    ClearPendingRequiredSl(client_order_id);
    startup_protection_sl_ids_.erase(client_order_id);
    symbols.insert(record->intent.symbol);
    LogInfo("PROTECTION_ORPHAN_CANCELLED: symbol=" + record->intent.symbol +
            ", client_order_id=" + client_order_id +
            ", reason=" + reason + "_flat_snapshot");
  }

  for (const auto& symbol : symbols) {
    const double position_qty = system_.account().position_qty(symbol);
    if (!HasExposure(position_qty)) {
      CancelManagedProtectionForSymbol(symbol, reason + "_flat_snapshot");
      candidate_probe_position_entry_tick_by_symbol_.erase(symbol);
      active_candidate_probe_by_symbol_.erase(symbol);
      continue;
    }
    const double reference_price =
        latest_mark_price_by_symbol_.count(symbol) > 0
            ? latest_mark_price_by_symbol_.at(symbol)
            : system_.account().mark_price(symbol);
    RefreshManagedProtectionForSymbol(symbol,
                                      reference_price,
                                      reason + "_open_snapshot");
  }

  if (!HasExposure(system_.account().gross_notional_usd())) {
    pending_required_sl_attach_.clear();
    startup_protection_sl_ids_.clear();
    startup_protection_recovery_pending_ = false;
    candidate_probe_position_entry_tick_by_symbol_.clear();
    active_candidate_probe_by_symbol_.clear();
  }
  RefreshProtectionReduceOnlyRelease(reason + "_authoritative_sync");
}

bool BotApplication::TryEnqueueProfitProtectionImmediateReduce(
    ManagedProtectionState& state,
    const MarketEvent& event,
    double current_price,
    double candidate_stop,
    const std::string& reason) {
  if (!config_.protection.profit_protection_immediate_reduce_enabled ||
      state.symbol.empty() || state.direction == 0 || state.avg_entry_price <= 0.0 ||
      !std::isfinite(current_price) || current_price <= 0.0) {
    return false;
  }
  if (oms_.HasPendingNetPositionOrderForSymbol(state.symbol)) {
    LogInfo("PROFIT_PROTECTION_IMMEDIATE_REDUCE_SKIPPED: symbol=" +
            state.symbol +
            ", reason=pending_net_order" +
            ", protection_group_id=" + state.protection_group_id);
    return false;
  }

  const double current_notional =
      system_.account().current_notional_usd(state.symbol);
  if (!HasExposure(current_notional)) {
    return false;
  }
  const double current_favorable_bps =
      FavorableReturn(state.direction, state.avg_entry_price, current_price) *
      10000.0;
  const double required_gross_bps =
      std::max(0.0,
               config_.execution_entry_fee_bps + config_.execution_exit_fee_bps +
                   config_.execution_expected_slippage_bps +
                   config_.protection.profit_protection_immediate_min_net_bps);
  if (current_favorable_bps + 1e-9 < required_gross_bps) {
    LogInfo("PROFIT_PROTECTION_IMMEDIATE_REDUCE_SKIPPED: symbol=" +
            state.symbol +
            ", reason=insufficient_current_net_edge" +
            ", protection_group_id=" + state.protection_group_id +
            ", current_favorable_bps=" +
            std::to_string(current_favorable_bps) +
            ", required_gross_bps=" + std::to_string(required_gross_bps) +
            ", candidate_sl_price=" + std::to_string(candidate_stop) +
            ", current_price=" + std::to_string(current_price));
    return false;
  }

  RiskAdjustedPosition flat_target{
      .symbol = state.symbol,
      .adjusted_notional_usd = 0.0,
      .reduce_only = true,
      .risk_mode = system_.risk_mode(),
  };
  auto reduce = execution_.BuildIntent(flat_target, current_notional, current_price);
  if (!reduce.has_value() || reduce->purpose != OrderPurpose::kReduce ||
      !reduce->reduce_only) {
    LogInfo("PROFIT_PROTECTION_IMMEDIATE_REDUCE_SKIPPED: symbol=" +
            state.symbol +
            ", reason=build_reduce_failed" +
            ", protection_group_id=" + state.protection_group_id);
    return false;
  }
  reduce->liquidity_preference = LiquidityPreference::kTaker;
  reduce->price = current_price;
  std::string guard_reason;
  if (ViolatesExchangePretradeGuard(adapter_.get(), &*reduce, event,
                                    &guard_reason)) {
    LogInfo("PROFIT_PROTECTION_IMMEDIATE_REDUCE_SKIPPED: symbol=" +
            state.symbol +
            ", reason=" + guard_reason +
            ", protection_group_id=" + state.protection_group_id);
    return false;
  }
  if (!EnqueueIntent(*reduce)) {
    LogInfo("PROFIT_PROTECTION_IMMEDIATE_REDUCE_SKIPPED: symbol=" +
            state.symbol +
            ", reason=enqueue_failed" +
            ", protection_group_id=" + state.protection_group_id);
    return false;
  }

  if (!state.active_tp_client_order_id.empty()) {
    if (const auto* tp_record = oms_.Find(state.active_tp_client_order_id);
        tp_record != nullptr && !OrderManager::IsTerminalState(tp_record->state)) {
      executor_->Cancel(state.active_tp_client_order_id);
      oms_.MarkCancelled(state.active_tp_client_order_id);
    }
    state.active_tp_client_order_id.clear();
    state.active_tp_price = 0.0;
  }

  LogInfo("PROFIT_PROTECTION_IMMEDIATE_REDUCE: symbol=" + state.symbol +
          ", client_order_id=" + reduce->client_order_id +
          ", protection_group_id=" + state.protection_group_id +
          ", reason=" + reason +
          ", direction=" + std::to_string(state.direction) +
          ", current_notional_usd=" + std::to_string(current_notional) +
          ", qty=" + std::to_string(reduce->qty) +
          ", current_price=" + std::to_string(current_price) +
          ", candidate_sl_price=" + std::to_string(candidate_stop) +
          ", active_sl_price=" + std::to_string(state.active_sl_price) +
          ", current_favorable_bps=" +
          std::to_string(current_favorable_bps) +
          ", required_gross_bps=" + std::to_string(required_gross_bps));
  return true;
}

void BotApplication::UpdateProfitProtection(const MarketEvent& event) {
  if (!config_.protection.enabled || event.symbol.empty()) {
    return;
  }
  auto it = managed_protection_by_symbol_.find(event.symbol);
  if (it == managed_protection_by_symbol_.end()) {
    return;
  }
  auto& state = it->second;
  if (state.active_sl_client_order_id.empty() || state.avg_entry_price <= 0.0 ||
      state.qty <= 0.0) {
    return;
  }

  const double current_price =
      event.mark_price > 0.0 ? event.mark_price : event.price;
  if (!std::isfinite(current_price) || current_price <= 0.0) {
    return;
  }

  if (state.direction > 0) {
    if (current_price > state.best_price + 1e-9) {
      state.best_price = current_price;
    }
  } else if (state.direction < 0) {
    if (state.best_price <= 0.0 || current_price < state.best_price - 1e-9) {
      state.best_price = current_price;
    }
  }

  double trailing_distance_ratio = config_.protection.trailing_distance_ratio;
  if (trailing_distance_ratio <= 0.0) {
    trailing_distance_ratio = state.stop_loss_ratio;
  }
  const auto candidate_stop = ComputeProfitProtectionStopPrice(
      state.direction,
      state.avg_entry_price,
      state.best_price,
      config_.protection.break_even_enabled,
      config_.protection.break_even_trigger_ratio,
      config_.protection.break_even_offset_ratio,
      config_.protection.trailing_enabled,
      config_.protection.trailing_trigger_ratio,
      trailing_distance_ratio);
  if (!candidate_stop.has_value() || *candidate_stop <= 0.0) {
    return;
  }

  const double min_update_abs =
      std::max(state.avg_entry_price *
                   config_.protection.profit_protection_min_update_ratio,
               1e-6);
  const bool tighter_stop = IsTighterStopPrice(
      state.direction, *candidate_stop, state.active_sl_price, min_update_abs);
  if (!tighter_stop) {
    return;
  }
  if (StopWouldTriggerNow(state.direction, *candidate_stop, current_price)) {
    if (TryEnqueueProfitProtectionImmediateReduce(
            state, event, current_price, *candidate_stop, "candidate_stop_crossed")) {
      return;
    }
    LogInfo("PROFIT_PROTECTION_CROSSED: symbol=" + state.symbol +
            ", protection_group_id=" + state.protection_group_id +
            ", reason=update" +
            ", best_price=" + std::to_string(state.best_price) +
            ", current_price=" + std::to_string(current_price) +
            ", candidate_sl_price=" + std::to_string(*candidate_stop) +
            ", active_sl_price=" + std::to_string(state.active_sl_price) +
            ", action=skip_sl_update");
    return;
  }

  const double previous_sl_price = state.active_sl_price;
  if (const auto* sl_record = oms_.Find(state.active_sl_client_order_id);
      sl_record != nullptr && !OrderManager::IsTerminalState(sl_record->state)) {
    executor_->Cancel(state.active_sl_client_order_id);
    oms_.MarkCancelled(state.active_sl_client_order_id);
  }
  ClearPendingRequiredSl(state.active_sl_client_order_id);

  FillEvent synthetic_entry_fill;
  synthetic_entry_fill.client_order_id = state.protection_group_id;
  synthetic_entry_fill.symbol = state.symbol;
  synthetic_entry_fill.direction = state.direction;
  synthetic_entry_fill.qty = state.qty;
  synthetic_entry_fill.price = state.avg_entry_price;
  auto sl = execution_.BuildProtectionIntentAtPrice(
      synthetic_entry_fill, OrderPurpose::kSl, *candidate_stop);
  if (!sl || !EnqueueIntent(*sl)) {
    protection_forced_reduce_only_ = true;
    RefreshReduceOnlyMode();
    LogError("EXEC_PROTECTIVE_ORDER_MISSING: reason=profit_protection_sl_update_failed"
             ", symbol=" + state.symbol +
             ", protection_group_id=" + state.protection_group_id +
             ", forcing=reduce_only");
    return;
  }

  TrackPendingRequiredSl(sl->client_order_id, state.protection_group_id);
  state.active_sl_client_order_id = sl->client_order_id;
  state.active_sl_price = sl->price;

  LogInfo("PROFIT_PROTECTION_UPDATE: symbol=" + state.symbol +
          ", protection_group_id=" + state.protection_group_id +
          ", best_price=" + std::to_string(state.best_price) +
          ", avg_entry_price=" + std::to_string(state.avg_entry_price) +
          ", current_price=" + std::to_string(current_price) +
          ", path_mfe_bps=" +
          std::to_string(std::max(0.0,
                                  FavorableReturn(state.direction,
                                                  state.avg_entry_price,
                                                  state.best_price)) *
                         10000.0) +
          ", current_favorable_bps=" +
          std::to_string(FavorableReturn(state.direction,
                                         state.avg_entry_price,
                                         current_price) *
                         10000.0) +
          ", previous_sl_price=" + std::to_string(previous_sl_price) +
          ", new_sl_price=" + std::to_string(state.active_sl_price) +
          ", break_even_enabled=" +
          std::string(config_.protection.break_even_enabled ? "true" : "false") +
          ", trailing_enabled=" +
          std::string(config_.protection.trailing_enabled ? "true" : "false"));
}

void BotApplication::TrackPendingRequiredSl(
    const std::string& sl_client_order_id,
    const std::string& parent_order_id) {
  if (sl_client_order_id.empty() || !config_.protection.require_sl) {
    return;
  }
  const std::int64_t timeout_ms =
      static_cast<std::int64_t>(config_.protection.attach_timeout_ms);
  pending_required_sl_attach_[sl_client_order_id] = PendingRequiredSlAttach{
      .parent_order_id = parent_order_id,
      .deadline_ms = CurrentTimestampMs() + timeout_ms,
  };
}

void BotApplication::ClearPendingRequiredSl(
    const std::string& sl_client_order_id) {
  if (sl_client_order_id.empty()) {
    return;
  }
  pending_required_sl_attach_.erase(sl_client_order_id);
  if (startup_protection_sl_ids_.erase(sl_client_order_id) > 0 &&
      startup_protection_sl_ids_.empty()) {
    startup_protection_recovery_pending_ = false;
    RefreshReduceOnlyMode();
    LogInfo("STARTUP_PROTECTION_RECOVERY_CONFIRMED: all_required_sl_sent=true");
  }
  RefreshProtectionReduceOnlyRelease("required_sl_cleared");
}

void BotApplication::CheckPendingRequiredSlTimeouts() {
  if (!config_.protection.enabled || !config_.protection.require_sl ||
      config_.protection.attach_timeout_ms <= 0 ||
      pending_required_sl_attach_.empty()) {
    return;
  }

  const std::int64_t now_ms = CurrentTimestampMs();
  std::vector<std::string> to_remove;
  to_remove.reserve(pending_required_sl_attach_.size());
  for (const auto& [sl_client_order_id, pending] : pending_required_sl_attach_) {
    if (now_ms < pending.deadline_ms) {
      continue;
    }

    const auto* sl_record = oms_.Find(sl_client_order_id);
    const bool confirmed =
        sl_record != nullptr &&
        (sl_record->state == OrderState::kSent ||
         sl_record->state == OrderState::kPartial ||
         sl_record->state == OrderState::kFilled);
    if (confirmed) {
      to_remove.push_back(sl_client_order_id);
      continue;
    }

    protection_forced_reduce_only_ = true;
    RefreshReduceOnlyMode();
    LogError("EXEC_PROTECTIVE_ORDER_MISSING: reason=sl_attach_timeout"
             ", parent_order_id=" + pending.parent_order_id +
             ", sl_client_order_id=" + sl_client_order_id +
             ", sl_state=" +
             (sl_record != nullptr ? OrderStateToString(sl_record->state)
                                   : "missing") +
             ", timeout_ms=" +
             std::to_string(config_.protection.attach_timeout_ms) +
             ", forcing=reduce_only");
    to_remove.push_back(sl_client_order_id);
  }

  for (const auto& id : to_remove) {
    pending_required_sl_attach_.erase(id);
  }
}

/**
 * @brief 周期性对账
 *
 * 双阶段确认：
 * 1) 先用远端净名义敞口快照快速检查；
 * 2) 失败后主动刷新远端持仓再检查一次；
 * 连续超过阈值才熔断停止新下单，避免瞬时抖动误判。
 */
void BotApplication::RunReconcile() {
  if (!config_.reconcile.enabled || config_.reconcile.interval_ticks <= 0) return;
  if (++reconcile_tick_ % config_.reconcile.interval_ticks != 0) return;
  PruneRecentFillObservations(market_tick_count_);

  const bool flat_and_idle =
      !HasExposure(system_.account().current_notional_usd()) &&
      pending_net_order_enqueued_ms_.empty();
  if (reconcile_halted_ && !flat_and_idle) {
    LogInfo("OMS_RECONCILE_HALTED_WAITING: flat_idle=false, local_notional=" +
            std::to_string(system_.account().current_notional_usd()) +
            ", pending_net_orders=" +
            std::to_string(pending_net_order_enqueued_ms_.size()));
    return;
  }

  // 净仓位变更订单仍在途时，远端与本地可能天然存在短时偏差。
  // 但若订单长期未收敛（例如 reduce-only 部分成交尾量未终态），需要主动收敛以解除永久阻塞。
  int stale_net_orders = 0;
  int remote_missing_net_orders = 0;
  int fresh_net_orders = 0;
  const std::int64_t now_ms = CurrentTimestampMs();
  const std::int64_t stale_ms =
      static_cast<std::int64_t>(config_.reconcile.pending_order_stale_ms);
  std::unordered_set<std::string> remote_open_order_ids;
  const bool remote_open_orders_ok =
      adapter_ != nullptr &&
      adapter_->GetRemoteOpenOrderClientIds(&remote_open_order_ids);
  for (const auto& client_order_id : oms_.PendingNetPositionOrderIds()) {
    const auto it = pending_net_order_enqueued_ms_.find(client_order_id);
    const OrderRecord* record = oms_.Find(client_order_id);
    if (record != nullptr &&
        record->state == OrderState::kCancelPending) {
      ++fresh_net_orders;
      continue;
    }
    if (record != nullptr &&
        record->state == OrderState::kCancelConfirmed) {
      const bool observation_elapsed =
          it == pending_net_order_enqueued_ms_.end() ||
          now_ms - it->second > stale_ms;
      if (!observation_elapsed) {
        ++fresh_net_orders;
        continue;
      }
      const bool absent_on_remote =
          remote_open_orders_ok &&
          remote_open_order_ids.find(client_order_id) ==
              remote_open_order_ids.end();
      if (absent_on_remote) {
        oms_.MarkCancelled(client_order_id);
        pending_net_order_enqueued_ms_.erase(client_order_id);
        integrator_lineage_by_intent_id_.erase(client_order_id);
        OnCandidateProbeCancelResult(client_order_id, true);
        OnStrategyReduceCancelResult(client_order_id, true);
        ++stale_net_orders;
        ++remote_missing_net_orders;
        LogInfo("OMS_CANCEL_FINALIZED: client_order_id=" + client_order_id +
                ", observation_ms=" + std::to_string(stale_ms) +
                ", remote_absent=true");
        continue;
      }

      // 交易所仍报告活动订单或快照不可用，撤单确认不能当作终态。
      oms_.MarkCancelFailed(client_order_id);
      if (executor_ != nullptr) {
        oms_.MarkCancelPending(client_order_id);
        pending_net_order_enqueued_ms_[client_order_id] = now_ms;
        executor_->Cancel(client_order_id);
      }
      ++stale_net_orders;
      continue;
    }
    const bool missing_on_remote =
        remote_open_orders_ok &&
        remote_open_order_ids.find(client_order_id) ==
            remote_open_order_ids.end();
    bool is_stale = false;
    if (it == pending_net_order_enqueued_ms_.end()) {
      // WAL恢复或历史遗留订单：缺少本次进程入队时间，按陈旧单处理。
      is_stale = true;
    } else if (now_ms - it->second > stale_ms) {
      is_stale = true;
    }
    if (!is_stale) {
      ++fresh_net_orders;
      continue;
    }

    ++stale_net_orders;
    if (missing_on_remote) {
      ++remote_missing_net_orders;
      // 活动订单快照可靠、订单已超过迟到成交观察窗口，
      // 此时远端不存在就是本地终态的权威依据。不再重复发送
      // CancelOrder，避免 Bybit 110001 将 OMS 永久卡在 Sent/CancelPending。
      oms_.MarkCancelled(client_order_id);
      pending_net_order_enqueued_ms_.erase(client_order_id);
      integrator_lineage_by_intent_id_.erase(client_order_id);
      OnCandidateProbeCancelResult(client_order_id, true);
      OnStrategyReduceCancelResult(client_order_id, true);
      LogInfo("OMS_REMOTE_MISSING_FINALIZED: client_order_id=" +
              client_order_id + ", stale_ms=" +
              std::to_string(stale_ms));
      continue;
    }
    if (executor_ != nullptr) {
      oms_.MarkCancelPending(client_order_id);
      pending_net_order_enqueued_ms_[client_order_id] = now_ms;
      executor_->Cancel(client_order_id);
    }
  }

  if (stale_net_orders > 0) {
    LogInfo("OMS_STALE_PENDING_CANCEL_PROGRESS: count=" +
            std::to_string(stale_net_orders) +
            ", remote_missing=" + std::to_string(remote_missing_net_orders) +
            ", stale_ms=" + std::to_string(stale_ms));
  }

  if (fresh_net_orders > 0) {
    reconcile_streak_ = 0;
    LogInfo("OMS_RECONCILE_DEFERRED: pending_net_orders=" +
            std::to_string(fresh_net_orders));
    UpdateReconcileAnomalyProtection(false, "RECONCILE_DEFERRED");
    return;
  }

  std::optional<double> remote_notional;
  double val = 0.0;
  std::vector<RemotePositionSnapshot> remote_positions;
  bool remote_positions_fresh = false;
  if (adapter_->GetRemoteNotionalUsd(&val)) {
    remote_notional = val;
  } else if (adapter_->GetRemotePositions(&remote_positions)) {
    remote_notional = reconciler_.ComputeRemoteNotionalUsd(remote_positions);
    remote_positions_fresh = true;
  }

  // live/paper 模式下，远端快照不可用时跳过本轮对账，避免回退到弱口径导致误熔断。
  if (!remote_notional.has_value() && config_.mode != "replay") {
    reconcile_streak_ = 0;
    if (flat_and_idle) {
      LogInfo("OMS_RECONCILE_DEGRADED_FLAT_IDLE: 远端快照不可用，本地空仓且无净仓位在途订单，累计健康恢复窗口");
      UpdateReconcileAnomalyProtection(false, "RECONCILE_DEGRADED_FLAT_IDLE");
      return;
    }
    LogInfo("OMS_RECONCILE_DEGRADED: 远端快照不可用，跳过本轮对账");
    UpdateReconcileAnomalyProtection(true, "RECONCILE_DEGRADED");
    return;
  }

  auto result = reconciler_.Check(system_.account(), oms_, remote_notional);
  if (!result.ok) {
    // 成交刚落地时，远端持仓快照可能仍在最终一致性窗口内，先不累计失败次数。
    if (market_tick_count_ - last_fill_tick_ <= kReconcileRecentFillGraceTicks) {
      reconcile_streak_ = 0;
      LogInfo("OMS_RECONCILE_GRACE: recent_fill_tick=" +
              std::to_string(last_fill_tick_) +
              ", delta_notional=" + std::to_string(result.delta_notional_usd));
      UpdateReconcileAnomalyProtection(false, "RECONCILE_GRACE");
      return;
    }

    const double first_delta = result.delta_notional_usd;

    // Retry with fresh snapshot
    if (!remote_positions_fresh && adapter_->GetRemotePositions(&remote_positions)) {
      remote_positions_fresh = true;
    }
    if (remote_positions_fresh) {
      val = reconciler_.ComputeRemoteNotionalUsd(remote_positions);
      result = reconciler_.Check(system_.account(), oms_, val);
    }

    if (!result.ok) {
      std::string recent_fill_explanation;
      if (remote_positions_fresh &&
          RecentFillsExplainReconcileMismatch(remote_positions,
                                             &recent_fill_explanation)) {
        reconcile_streak_ = 0;
        LogInfo("OMS_RECONCILE_FILL_LAG_GRACE: delta_notional=" +
                std::to_string(result.delta_notional_usd) +
                ", recent_fills=" + recent_fill_explanation);
        UpdateReconcileAnomalyProtection(false, "RECONCILE_FILL_LAG_GRACE");
        return;
      }

      const double second_delta = result.delta_notional_usd;
      const std::string symbol_delta_report = remote_positions_fresh
                                                  ? reconciler_.BuildSymbolDeltaReport(
                                                        system_.account(),
                                                        remote_positions,
                                                        tracked_symbols_,
                                                        /*min_abs_notional_delta_usd=*/1.0)
                                                  : "none";
      LogInfo("OMS_RECONCILE_MISMATCH: first_delta_notional=" +
              std::to_string(first_delta) +
              ", second_delta_notional=" + std::to_string(second_delta) +
              ", remote_positions_refreshed=" +
              std::string(remote_positions_fresh ? "true" : "false") +
              ", symbol_deltas=" + symbol_delta_report);

      // 运行时自愈：远端快照可用且达到防抖间隔时，以远端仓位重建本地仓位视图与 OMS 基线。
      // 该路径用于“WS/REST 成交回报缺失导致本地仓位漂移”的根治兜底。
      const bool can_auto_resync =
          config_.mode != "replay" &&
          remote_positions_fresh &&
          (market_tick_count_ - last_auto_resync_tick_ >=
           kReconcileAutoResyncCooldownTicks);
      if (can_auto_resync) {
        system_.ForceSyncAccountPositionsFromRemote(remote_positions);
        oms_.SeedNetPositionBaseline(remote_positions);
        pending_net_order_enqueued_ms_.clear();
        ReconcileProtectionAfterAuthoritativePositionSync(
            "reconcile_autoresync");
        RemoteAccountBalanceSnapshot balance;
        if (adapter_->GetRemoteAccountBalance(&balance)) {
          system_.SyncAccountFromRemoteBalance(balance,
                                               /*reset_peak_to_equity=*/false);
          LogAccountSyncSnapshot("reconcile_autoresync", balance,
                                 system_.account());
        }
        last_auto_resync_tick_ = market_tick_count_;
        reconcile_streak_ = 0;
        LogInfo("OMS_RECONCILE_AUTORESYNC: applied=true, positions=" +
                std::to_string(remote_positions.size()) +
                ", cooldown_ticks=" +
                std::to_string(kReconcileAutoResyncCooldownTicks));
        UpdateReconcileAnomalyProtection(true, "RECONCILE_MISMATCH_AUTORESYNC");
        return;
      }

      if (++reconcile_streak_ >= config_.reconcile.mismatch_confirmations &&
          !reconcile_halted_) {
        reconcile_halted_ = true;
        RefreshTradingHaltState();
        LogError("CRITICAL: Reconcile mismatch confirmed. Halting trading.");
      }
      UpdateReconcileAnomalyProtection(true, "RECONCILE_MISMATCH_CONFIRMED");
    } else {
      reconcile_streak_ = 0;
      UpdateReconcileAnomalyProtection(false, "RECONCILE_OK");
    }
  } else {
    reconcile_streak_ = 0;
    UpdateReconcileAnomalyProtection(false, "RECONCILE_OK");
  }
}

// Gate 活跃度检查：支持“仅告警”与“运行时动作”两种模式。
void BotApplication::RunGateMonitor() {
  if (config_.gate.enforce_runtime_actions) {
    TickGateRuntimeCooldown();
  }

  // Gate 自动恢复“空仓稳态”判定：
  // 1) 账户当前无净敞口；
  // 2) 无净仓位相关在途订单（避免与落地中的订单竞争状态）；
  // 3) 连续满足最小 flat ticks 且冷却结束。
  const bool flat_and_idle =
      !HasExposure(system_.account().current_notional_usd()) &&
      pending_net_order_enqueued_ms_.empty();
  if (flat_and_idle) {
    ++gate_flat_ticks_streak_;
  } else {
    gate_flat_ticks_streak_ = 0;
  }

  if (config_.gate.enforce_runtime_actions &&
      config_.gate.auto_resume_when_flat &&
      (gate_halted_ || gate_forced_reduce_only_) &&
      !reconcile_halted_ && flat_and_idle &&
      gate_flat_ticks_streak_ >= config_.gate.auto_resume_flat_ticks &&
      gate_reduce_only_cooldown_ticks_left_ <= 0 &&
      gate_halt_cooldown_ticks_left_ <= 0) {
    gate_halted_ = false;
    gate_forced_reduce_only_ = false;
    gate_fail_windows_streak_ = 0;
    gate_pass_windows_streak_ = 0;
    RefreshReduceOnlyMode();
    RefreshTradingHaltState();
    LogInfo("GATE_RUNTIME_AUTO_RESUME: flat_ticks=" +
            std::to_string(gate_flat_ticks_streak_) +
            ", trading_halted=" +
            std::string(trading_halted_ ? "true" : "false") +
            ", reduce_only=" +
            std::string(IsForceReduceOnlyActive() ? "true" : "false"));
  }

  if (auto res = gate_monitor_.OnTick(); res.has_value()) {
    if (!res->pass) {
      std::ostringstream reasons;
      for (std::size_t i = 0; i < res->fail_reasons.size(); ++i) {
        if (i > 0) {
          reasons << ",";
        }
        reasons << res->fail_reasons[i];
      }
      LogInfo("GATE_CHECK_FAILED: raw_signals=" + std::to_string(res->raw_signals) +
              ", order_intents=" + std::to_string(res->order_intents) +
              ", effective_signals=" +
              std::to_string(res->effective_signals) +
              ", fills=" + std::to_string(res->fills) +
              ", policy_flat_signals=" +
              std::to_string(res->policy_flat_signals) +
              ", runtime_action_exempt=" +
              std::string(res->policy_flat_runtime_exempt ? "true" : "false") +
              ", fail_reasons=[" + reasons.str() + "]");
    } else {
      LogInfo("GATE_CHECK_PASSED: raw_signals=" +
              std::to_string(res->raw_signals) +
              ", order_intents=" + std::to_string(res->order_intents) +
              ", effective_signals=" +
              std::to_string(res->effective_signals) +
              ", fills=" + std::to_string(res->fills) +
              ", policy_flat_signals=" +
              std::to_string(res->policy_flat_signals) +
              ", policy_flat=" +
              std::string(res->policy_flat_pass ? "true" : "false"));
    }

    const bool runtime_action_pass =
        res->pass || res->policy_flat_runtime_exempt;
    if (runtime_action_pass) {
      if (!res->pass && res->policy_flat_runtime_exempt) {
        LogInfo("GATE_RUNTIME_POLICY_FLAT_EXEMPT: policy_flat_signals=" +
                std::to_string(res->policy_flat_signals) +
                ", fail_streak_before=" +
                std::to_string(gate_fail_windows_streak_));
      }
      gate_fail_windows_streak_ = 0;
      ++gate_pass_windows_streak_;
    } else {
      ++gate_fail_windows_streak_;
      gate_pass_windows_streak_ = 0;
    }

    if (!config_.gate.enforce_runtime_actions) {
      return;
    }

    if (!runtime_action_pass) {
      if (!gate_forced_reduce_only_ &&
          config_.gate.fail_to_reduce_only_windows > 0 &&
          gate_fail_windows_streak_ >=
              config_.gate.fail_to_reduce_only_windows) {
        gate_forced_reduce_only_ = true;
        gate_reduce_only_cooldown_ticks_left_ =
            config_.gate.reduce_only_cooldown_ticks;
        RefreshReduceOnlyMode();
        LogInfo("GATE_RUNTIME_REDUCE_ONLY_ENTER: fail_streak=" +
                std::to_string(gate_fail_windows_streak_) +
                ", cooldown_ticks=" +
                std::to_string(gate_reduce_only_cooldown_ticks_left_));
      }

      if (!gate_halted_ && config_.gate.fail_to_halt_windows > 0 &&
          gate_fail_windows_streak_ >= config_.gate.fail_to_halt_windows) {
        gate_halted_ = true;
        gate_halt_cooldown_ticks_left_ = config_.gate.halt_cooldown_ticks;
        RefreshTradingHaltState();
        LogError("GATE_RUNTIME_HALT_ENTER: fail_streak=" +
                 std::to_string(gate_fail_windows_streak_) +
                 ", cooldown_ticks=" +
                 std::to_string(gate_halt_cooldown_ticks_left_));
      }
      return;
    }

    const bool resume_windows_reached =
        config_.gate.pass_to_resume_windows <= 0 ||
        gate_pass_windows_streak_ >= config_.gate.pass_to_resume_windows;

    if (gate_forced_reduce_only_ && resume_windows_reached &&
        gate_reduce_only_cooldown_ticks_left_ <= 0) {
      gate_forced_reduce_only_ = false;
      RefreshReduceOnlyMode();
      LogInfo("GATE_RUNTIME_REDUCE_ONLY_EXIT: pass_streak=" +
              std::to_string(gate_pass_windows_streak_));
    }

    if (gate_halted_ && resume_windows_reached &&
        gate_halt_cooldown_ticks_left_ <= 0) {
      gate_halted_ = false;
      RefreshTradingHaltState();
      LogInfo("GATE_RUNTIME_HALT_EXIT: pass_streak=" +
              std::to_string(gate_pass_windows_streak_) +
              ", trading_halted=" +
              std::string(trading_halted_ ? "true" : "false"));
    }
  }
}

void BotApplication::RunSelfEvolution() {
  if (!config_.self_evolution.enabled) {
    return;
  }

  const RegimeBucket active_bucket =
      has_last_regime_state_ ? last_regime_state_.bucket : RegimeBucket::kRange;
  const double trend_signal_notional_usd =
      has_tick_strategy_signal_ ? tick_trend_notional_usd_ : 0.0;
  const double defensive_signal_notional_usd =
      has_tick_strategy_signal_ ? tick_defensive_notional_usd_ : 0.0;
  const double mark_price_usd = has_latest_mark_price_ ? latest_mark_price_usd_ : 0.0;
  const double observed_funding_rate_per_tick =
      has_latest_funding_rate_per_tick_
          ? latest_funding_rate_per_tick_
          : std::numeric_limits<double>::quiet_NaN();
  const std::string signal_symbol =
      has_tick_strategy_signal_ ? tick_strategy_signal_symbol_ : std::string();
  const double observed_turnover_cost_bps = std::max(0.0, 0.5 * RoundTripCostBps());
  const auto action =
      self_evolution_.OnTick(market_tick_count_,
                             system_.account().cumulative_realized_net_pnl_usd(),
                             active_bucket,
                             system_.account().drawdown_pct(),
                             system_.account().current_notional_usd(),
                             trend_signal_notional_usd,
                             defensive_signal_notional_usd,
                             mark_price_usd,
                             signal_symbol,
                             tick_cost_filtered_signal_,
                             std::max(0, pending_fills_for_evolution_),
                             system_.account().equity_usd(),
                             observed_turnover_cost_bps,
                             observed_funding_rate_per_tick);
  pending_fills_for_evolution_ = 0;
  if (!action.has_value()) {
    return;
  }

  if (action->type == SelfEvolutionActionType::kUpdated ||
      action->type == SelfEvolutionActionType::kRolledBack) {
    std::string set_error;
    if (!system_.SetEvolutionWeightsForBucket(action->regime_bucket,
                                              action->trend_weight_after,
                                              action->defensive_weight_after,
                                              &set_error)) {
      ++funnel_window_.self_evolution_skipped;
      LogInfo("PORT_WEIGHT_INVALID_REJECTED: reason=" + set_error +
              ", trend_weight=" + std::to_string(action->trend_weight_after) +
              ", defensive_weight=" +
              std::to_string(action->defensive_weight_after));
      return;
    }
    std::string persistence_error;
    if (!PersistSelfEvolutionWeights(&persistence_error)) {
      evidence_persistence_failed_ = true;
      RefreshReduceOnlyMode();
      LogError("CRITICAL: SELF_EVOLUTION_STATE_PERSIST_FAILED: boot_id=" +
               boot_id_ + ", reason=" + persistence_error);
    }
  }

  if (action->type == SelfEvolutionActionType::kUpdated) {
    ++funnel_window_.self_evolution_updates;
  } else if (action->type == SelfEvolutionActionType::kRolledBack) {
    ++funnel_window_.self_evolution_rollbacks;
  } else {
    ++funnel_window_.self_evolution_skipped;
  }

  const std::string direction_consistency_direction =
      action->direction_consistency_direction > 0
          ? "increase_trend"
          : (action->direction_consistency_direction < 0
                 ? "decrease_trend"
                 : "none");
  LogInfo("SELF_EVOLUTION_ACTION: type=" +
          std::string(EvolutionActionTypeToString(action->type)) +
          ", bucket=" + std::string(ToString(action->regime_bucket)) +
          ", reason=" + action->reason_code +
          ", bucket_ticks=" + std::to_string(action->window_bucket_ticks) +
          ", window_pnl_usd=" + std::to_string(action->window_pnl_usd) +
          ", window_realized_pnl_usd=" +
          std::to_string(action->window_realized_pnl_usd) +
          ", window_virtual_pnl_usd=" +
          std::to_string(action->window_virtual_pnl_usd) +
          ", pnl_source=" + std::string(action->used_virtual_pnl ? "virtual" : "realized") +
          ", counterfactual_search=" +
          std::string(action->used_counterfactual_search ? "true" : "false") +
          ", factor_ic_weighting=" +
          std::string(action->used_factor_ic_adaptive_weighting ? "true" : "false") +
          ", counterfactual_fallback={enabled=" +
          std::string(action->counterfactual_fallback_to_factor_ic_enabled
                          ? "true"
                          : "false") +
          ", used=" +
          std::string(action->counterfactual_fallback_to_factor_ic_used ? "true"
                                                                         : "false") +
          "}" +
          ", counterfactual_best_virtual_pnl_usd=" +
          std::to_string(action->counterfactual_best_virtual_pnl_usd) +
          ", counterfactual_best_weight={trend=" +
          std::to_string(action->counterfactual_best_trend_weight) +
          ",defensive=" +
          std::to_string(action->counterfactual_best_defensive_weight) + "}" +
          ", counterfactual_superiority={enabled=" +
          std::string(action->counterfactual_superiority_gate_enabled ? "true"
                                                                      : "false") +
          ", passed=" +
          std::string(action->counterfactual_superiority_gate_passed ? "true"
                                                                     : "false") +
          ", t_stat=" + std::to_string(action->counterfactual_superiority_t_stat) +
          ", samples=" +
          std::to_string(action->counterfactual_superiority_samples) + "}" +
          ", counterfactual_split={temporal_required=" +
          std::string(action->counterfactual_temporal_holdout_required ? "true"
                                                                       : "false") +
          ", train_samples=" +
          std::to_string(action->counterfactual_train_samples) +
          ", holdout_samples=" +
          std::to_string(action->counterfactual_holdout_samples) + "}" +
          ", factor_ic={trend=" + std::to_string(action->trend_factor_ic) +
          ", defensive=" + std::to_string(action->defensive_factor_ic) +
          ", samples=" + std::to_string(action->factor_ic_samples) + "}" +
          ", window_fill_count=" +
          std::to_string(action->window_fill_count) +
          ", cost_filtered_signals=" +
          std::to_string(action->window_cost_filtered_signals) +
          ", learnability={enabled=" +
          std::string(action->learnability_gate_enabled ? "true" : "false") +
          ", passed=" +
          std::string(action->learnability_gate_passed ? "true" : "false") +
          ", t_stat=" + std::to_string(action->learnability_t_stat) +
          ", samples=" + std::to_string(action->learnability_samples) + "}" +
          ", direction_consistency={required=" +
          std::to_string(action->direction_consistency_required) +
          ", streak=" + std::to_string(action->direction_consistency_streak) +
          ", direction=" + direction_consistency_direction + "}" +
          ", counterfactual_required_improvement_usd=" +
          std::to_string(action->counterfactual_required_improvement_usd) +
          ", window_objective_score=" +
          std::to_string(action->window_objective_score) +
          ", window_max_drawdown_pct=" +
          std::to_string(action->window_max_drawdown_pct) +
          ", window_notional_churn_usd=" +
          std::to_string(action->window_notional_churn_usd) +
          ", effective_turnover_cost_bps=" +
          std::to_string(action->effective_turnover_cost_bps) +
          ", funding_rate_per_tick=" +
          std::to_string(action->funding_rate_per_tick) +
          ", rollback_to_baseline=" +
          std::string(action->rolled_back_to_baseline ? "true" : "false") +
          ", weight_before={trend=" +
          std::to_string(action->trend_weight_before) +
          ",defensive=" + std::to_string(action->defensive_weight_before) +
          "}, weight_after={trend=" +
          std::to_string(action->trend_weight_after) +
          ",defensive=" + std::to_string(action->defensive_weight_after) +
          "}, candidate_trend_weight_delta=" +
          std::to_string(action->candidate_trend_weight_delta) +
          ", degrade_windows=" + std::to_string(action->degrade_windows) +
          ", cooldown_remaining_ticks=" +
          std::to_string(action->cooldown_remaining_ticks));
}

// 运行态摘要日志：用于线上巡检与回放定位。
void BotApplication::LogStatus() {
  if (config_.system_status_log_interval_ticks <= 0) return;
  if (market_tick_count_ % config_.system_status_log_interval_ticks != 0) return;

  const bool adapter_trade_ok = adapter_ != nullptr && adapter_->TradeOk();
  const bool force_reduce_only = IsForceReduceOnlyActive();
  const bool trade_ok =
      adapter_trade_ok && !trading_halted_ && !force_reduce_only;

  std::string ws_summary = "n/a";
  if (const auto* bybit =
          dynamic_cast<const BybitExchangeAdapter*>(adapter_.get());
      bybit != nullptr) {
    ws_summary = bybit->ChannelHealthSummary();
  } else if (adapter_ != nullptr) {
    ws_summary = "adapter=" + adapter_->Name();
  }

  const OrderThrottleStats throttle_window = order_throttle_.ConsumeWindowStats();
  const OrderThrottleStats& throttle_total = order_throttle_.total_stats();
  const double throttle_window_hit_rate =
      throttle_window.checks > 0
          ? static_cast<double>(throttle_window.rejected) /
                static_cast<double>(throttle_window.checks)
          : 0.0;
  const double throttle_total_hit_rate =
      throttle_total.checks > 0
          ? static_cast<double>(throttle_total.rejected) /
                static_cast<double>(throttle_total.checks)
          : 0.0;

  const DecisionFunnelStats funnel_window = funnel_window_;
  AccumulateStats(&funnel_total_, funnel_window_);
  funnel_window_ = DecisionFunnelStats{};
  const double shadow_avg_model_score =
      funnel_window.integrator_scored > 0
          ? funnel_window.integrator_model_score_sum /
                static_cast<double>(funnel_window.integrator_scored)
          : 0.0;
  const double shadow_avg_p_up =
      funnel_window.integrator_scored > 0
          ? funnel_window.integrator_p_up_sum /
                static_cast<double>(funnel_window.integrator_scored)
          : 0.0;
  const double shadow_avg_p_down =
      funnel_window.integrator_scored > 0
          ? funnel_window.integrator_p_down_sum /
                static_cast<double>(funnel_window.integrator_scored)
          : 0.0;
  const double entry_edge_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_edge_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_base_required_edge_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_base_required_edge_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_required_edge_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_required_edge_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_adaptive_relax_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_adaptive_relax_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_maker_relax_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_maker_relax_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_regime_adjust_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_regime_adjust_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_volatility_adjust_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_volatility_adjust_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_liquidity_adjust_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_liquidity_adjust_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_quality_guard_penalty_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_quality_guard_penalty_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double entry_edge_gap_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_edge_gap_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double candidate_probe_cost_gate_expected_edge_avg_bps =
      funnel_window.candidate_probe_cost_gate_samples > 0
          ? funnel_window.candidate_probe_cost_gate_expected_edge_bps_sum /
                static_cast<double>(funnel_window.candidate_probe_cost_gate_samples)
          : 0.0;
  const double candidate_probe_cost_gate_required_edge_avg_bps =
      funnel_window.candidate_probe_cost_gate_samples > 0
          ? funnel_window.candidate_probe_cost_gate_required_edge_bps_sum /
                static_cast<double>(funnel_window.candidate_probe_cost_gate_samples)
          : 0.0;
  const double candidate_probe_cost_gate_edge_gap_avg_bps =
      funnel_window.candidate_probe_cost_gate_samples > 0
          ? funnel_window.candidate_probe_cost_gate_edge_gap_bps_sum /
                static_cast<double>(funnel_window.candidate_probe_cost_gate_samples)
          : 0.0;
  const double candidate_probe_cost_gate_trend_ratio_avg =
      funnel_window.candidate_probe_cost_gate_samples > 0
          ? funnel_window.candidate_probe_cost_gate_trend_ratio_sum /
                static_cast<double>(funnel_window.candidate_probe_cost_gate_samples)
          : 0.0;
  const double filtered_cost_ratio =
      funnel_window.entry_edge_samples > 0
          ? static_cast<double>(funnel_window.intents_filtered_fee_aware) /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double filtered_cost_near_miss_ratio =
      funnel_window.entry_edge_samples > 0
          ? static_cast<double>(funnel_window.intents_filtered_fee_aware_near_miss) /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double passed_cost_near_miss_ratio =
      funnel_window.entry_edge_samples > 0
          ? static_cast<double>(funnel_window.intents_passed_fee_aware_near_miss) /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  const double rebalance_gap_avg_abs_usd =
      funnel_window.rebalance_gap_samples > 0
          ? funnel_window.rebalance_gap_abs_usd_sum /
                static_cast<double>(funnel_window.rebalance_gap_samples)
          : 0.0;
  const double rebalance_gap_within_min_notional_avg_abs_usd =
      funnel_window.rebalance_converged_within_min_notional > 0
          ? funnel_window.rebalance_gap_within_min_notional_abs_usd_sum /
                static_cast<double>(
                    funnel_window.rebalance_converged_within_min_notional)
          : 0.0;
  const double rebalance_gap_within_min_notional_ratio =
      funnel_window.rebalance_gap_samples > 0
          ? static_cast<double>(funnel_window.rebalance_converged_within_min_notional) /
                static_cast<double>(funnel_window.rebalance_gap_samples)
          : 0.0;
  const double strategy_trend_avg_abs_notional =
      funnel_window.strategy_mix_samples > 0
          ? funnel_window.trend_notional_abs_sum /
                static_cast<double>(funnel_window.strategy_mix_samples)
          : 0.0;
  const double strategy_defensive_avg_abs_notional =
      funnel_window.strategy_mix_samples > 0
          ? funnel_window.defensive_notional_abs_sum /
                static_cast<double>(funnel_window.strategy_mix_samples)
          : 0.0;
  const double strategy_blended_avg_abs_notional =
      funnel_window.strategy_mix_samples > 0
          ? funnel_window.blended_notional_abs_sum /
                static_cast<double>(funnel_window.strategy_mix_samples)
          : 0.0;
  const RegimeBucket active_bucket =
      has_last_regime_state_ ? last_regime_state_.bucket : RegimeBucket::kRange;
  const auto active_evolution_weights = system_.evolution_weights(active_bucket);
  const auto evolution_weights = system_.evolution_weights_all();
  const bool evolution_enabled =
      config_.self_evolution.enabled && self_evolution_.initialized();
  const bool evolution_cooldown =
      evolution_enabled && market_tick_count_ < self_evolution_.cooldown_until_tick();
  const int evolution_cooldown_remaining =
      evolution_cooldown
          ? static_cast<int>(self_evolution_.cooldown_until_tick() -
                             market_tick_count_)
          : 0;
  const double window_realized_net_delta_usd =
      has_last_status_account_snapshot_
          ? system_.account().cumulative_realized_net_pnl_usd() -
                last_status_realized_net_pnl_usd_
          : 0.0;
  const double window_fee_delta_usd =
      has_last_status_account_snapshot_
          ? system_.account().cumulative_fee_usd() - last_status_fee_usd_
          : 0.0;
  const double window_realized_net_per_fill_usd =
      funnel_window.fills_applied > 0
          ? window_realized_net_delta_usd /
                static_cast<double>(funnel_window.fills_applied)
          : 0.0;
  const double window_fee_bps_per_fill =
      funnel_window.fills_notional_abs_usd_sum > 1e-9
          ? window_fee_delta_usd / funnel_window.fills_notional_abs_usd_sum *
                10000.0
          : 0.0;
  const double window_entry_fee_delta_usd =
      funnel_window.entry_fills_maker_fee_usd_sum +
      funnel_window.entry_fills_taker_fee_usd_sum;
  const double window_maker_fee_bps =
      funnel_window.fills_maker_notional_abs_usd_sum > 1e-9
          ? funnel_window.fills_maker_fee_usd_sum /
                funnel_window.fills_maker_notional_abs_usd_sum * 10000.0
          : 0.0;
  const double window_taker_fee_bps =
      funnel_window.fills_taker_notional_abs_usd_sum > 1e-9
          ? funnel_window.fills_taker_fee_usd_sum /
                funnel_window.fills_taker_notional_abs_usd_sum * 10000.0
          : 0.0;
  const std::uint64_t liquidity_classified_fills =
      funnel_window.fills_maker_count + funnel_window.fills_taker_count +
      funnel_window.fills_unknown_liquidity_count;
  const std::uint64_t entry_liquidity_classified_fills =
      funnel_window.entry_fills_maker_count + funnel_window.entry_fills_taker_count +
      funnel_window.entry_fills_unknown_liquidity_count;
  const double window_maker_fill_ratio =
      liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.fills_maker_count) /
                static_cast<double>(liquidity_classified_fills)
          : 0.0;
  const double window_entry_maker_fill_ratio =
      entry_liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.entry_fills_maker_count) /
                static_cast<double>(entry_liquidity_classified_fills)
          : 0.0;
  const double window_unknown_fill_ratio =
      liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.fills_unknown_liquidity_count) /
                static_cast<double>(liquidity_classified_fills)
          : 0.0;
  const double window_entry_unknown_fill_ratio =
      entry_liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.entry_fills_unknown_liquidity_count) /
                static_cast<double>(entry_liquidity_classified_fills)
          : 0.0;
  const double window_explicit_liquidity_fill_ratio =
      liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.fills_explicit_liquidity_count) /
                static_cast<double>(liquidity_classified_fills)
          : 0.0;
  const double window_fee_sign_fallback_fill_ratio =
      liquidity_classified_fills > 0
          ? static_cast<double>(funnel_window.fills_fee_sign_fallback_count) /
                static_cast<double>(liquidity_classified_fills)
          : 0.0;
  const ConcentrationSnapshot concentration =
      BuildConcentrationSnapshot(system_.account());
  const double entry_concentration_adjust_avg_bps =
      funnel_window.entry_edge_samples > 0
          ? funnel_window.entry_concentration_adjust_bps_sum /
                static_cast<double>(funnel_window.entry_edge_samples)
          : 0.0;
  // 运行时开仓门控只参考开仓成交质量，不让 reduce-only / 保护出场污染 entry 质量反馈。
  recent_execution_window_maker_fill_ratio_ = window_entry_maker_fill_ratio;
  recent_execution_window_unknown_fill_ratio_ = window_entry_unknown_fill_ratio;
  recent_execution_window_liquidity_fill_count_ = entry_liquidity_classified_fills;
  EvaluateExecutionQualityGuard(funnel_window.entry_fills_applied,
                                0.0,
                                window_entry_fee_delta_usd,
                                funnel_window.entry_fills_notional_abs_usd_sum);
  std::unordered_set<std::string> symbol_quality_evaluated;
  for (const auto& [symbol, quality] : funnel_window.symbol_fill_quality_by_symbol) {
    symbol_quality_evaluated.insert(symbol);
    EvaluateSymbolExecutionQualityGuard(symbol,
                                        quality.fills,
                                        quality.net_quality_fills,
                                        quality.realized_net_sum_usd,
                                        quality.fee_usd_sum,
                                        quality.notional_abs_usd_sum);
  }
  std::vector<std::string> stale_symbol_quality_guards;
  stale_symbol_quality_guards.reserve(execution_quality_by_symbol_.size());
  for (const auto& [symbol, state] : execution_quality_by_symbol_) {
    if (state.guard_active && symbol_quality_evaluated.count(symbol) == 0) {
      stale_symbol_quality_guards.push_back(symbol);
    }
  }
  for (const auto& symbol : stale_symbol_quality_guards) {
    EvaluateSymbolExecutionQualityGuard(symbol, 0, 0, 0.0, 0.0, 0.0);
  }

  LogInfo("RUNTIME_STATUS: ticks=" + std::to_string(market_tick_count_) +
          ", trade_ok=" + std::string(trade_ok ? "true" : "false") +
          ", trading_halted=" +
          std::string(trading_halted_ ? "true" : "false") +
          ", risk_mode=" + RiskModeToString(system_.risk_mode()) +
          ", boot={id=" + boot_id_ + ", startup_utc=" + startup_utc_ + "}" +
          ", ws={" + ws_summary + "}" +
          ", trade_health={adapter_trade_ok=" +
          std::string(adapter_trade_ok ? "true" : "false") +
          ", force_reduce_only=" +
          std::string(force_reduce_only ? "true" : "false") +
          ", protection_reduce_only=" +
          std::string(protection_forced_reduce_only_ ? "true" : "false") +
          ", evidence_persistence_failed=" +
          std::string(evidence_persistence_failed_ ? "true" : "false") +
          ", gate_reduce_only=" +
          std::string(gate_forced_reduce_only_ ? "true" : "false") +
          ", reconcile_reduce_only=" +
          std::string(reconcile_forced_reduce_only_ ? "true" : "false") +
          ", trading_halted=" +
          std::string(trading_halted_ ? "true" : "false") + "}" +
          ", account={equity=" + std::to_string(system_.account().equity_usd()) +
          ", drawdown_pct=" + std::to_string(system_.account().drawdown_pct()) +
          ", notional=" + std::to_string(system_.account().current_notional_usd()) +
          ", realized_pnl=" +
          std::to_string(system_.account().cumulative_realized_pnl_usd()) +
          ", fees=" + std::to_string(system_.account().cumulative_fee_usd()) +
          ", realized_net=" +
          std::to_string(system_.account().cumulative_realized_net_pnl_usd()) +
          ", positions=" + FormatAccountPositions(system_.account()) + "}" +
          ", concentration={gross_notional_usd=" +
          std::to_string(concentration.gross_notional_usd) +
          ", top1_abs_notional_usd=" +
          std::to_string(concentration.top1_abs_notional_usd) +
          ", top1_symbol=" + concentration.top1_symbol +
          ", top1_share=" + std::to_string(concentration.top1_share) +
          ", symbol_count=" + std::to_string(concentration.symbol_count) + "}" +
          ", funnel_window={raw=" + std::to_string(funnel_window.raw_signals) +
          ", risk_adjusted=" +
          std::to_string(funnel_window.risk_adjusted_signals) +
          ", intents_generated=" +
          std::to_string(funnel_window.intents_generated) +
          ", intents_filtered_inactive_symbol=" +
          std::to_string(funnel_window.intents_filtered_inactive_symbol) +
          ", intents_filtered_min_notional=" +
          std::to_string(funnel_window.intents_filtered_min_notional) +
          ", intents_filtered_fee_aware=" +
          std::to_string(funnel_window.intents_filtered_fee_aware) +
          ", intents_filtered_fee_aware_near_miss=" +
          std::to_string(funnel_window.intents_filtered_fee_aware_near_miss) +
          ", intents_passed_fee_aware_near_miss=" +
          std::to_string(funnel_window.intents_passed_fee_aware_near_miss) +
          ", rebalance_gap_samples=" +
          std::to_string(funnel_window.rebalance_gap_samples) +
          ", rebalance_converged_within_min_notional=" +
          std::to_string(funnel_window.rebalance_converged_within_min_notional) +
          ", intents_throttled_cost_cooldown=" +
          std::to_string(funnel_window.intents_throttled_cost_cooldown) +
          ", intents_throttled_symbol_quality_quarantine=" +
          std::to_string(
              funnel_window.intents_throttled_symbol_quality_quarantine) +
          ", strategy_reduce_cost_guard_blocked=" +
          std::to_string(funnel_window.strategy_reduce_cost_guard_blocked) +
          ", strategy_reduce_cost_guard_bypassed=" +
          std::to_string(funnel_window.strategy_reduce_cost_guard_bypassed) +
          ", strategy_reduce_pending_timeouts=" +
          std::to_string(funnel_window.strategy_reduce_pending_timeouts) +
          ", strategy_reduce_cancel_submitted=" +
          std::to_string(funnel_window.strategy_reduce_cancel_submitted) +
          ", strategy_reduce_cancel_ok=" +
          std::to_string(funnel_window.strategy_reduce_cancel_ok) +
          ", strategy_reduce_cancel_failed=" +
          std::to_string(funnel_window.strategy_reduce_cancel_failed) +
          ", strategy_reduce_reprices=" +
          std::to_string(funnel_window.strategy_reduce_reprices) +
          ", strategy_reduce_taker_fallbacks=" +
          std::to_string(funnel_window.strategy_reduce_taker_fallbacks) +
          ", strategy_reduce_lifecycle_aborted=" +
          std::to_string(funnel_window.strategy_reduce_lifecycle_aborted) +
          ", reduce_without_position_blocked=" +
          std::to_string(funnel_window.reduce_without_position_blocked) +
          ", reduce_qty_capped_to_position=" +
          std::to_string(funnel_window.reduce_qty_capped_to_position) +
          ", throttled=" + std::to_string(funnel_window.intents_throttled) +
          ", enqueued=" + std::to_string(funnel_window.intents_enqueued) +
          ", candidate_probe_signals=" +
          std::to_string(funnel_window.candidate_probe_signals) +
          ", candidate_probe_strong_signals=" +
          std::to_string(funnel_window.candidate_probe_strong_signals) +
          ", candidate_probe_intents=" +
          std::to_string(funnel_window.candidate_probe_intents) +
          ", candidate_probe_cost_cooldown_bypass=" +
          std::to_string(funnel_window.candidate_probe_cost_cooldown_bypass) +
          ", candidate_probe_fee_overrides=" +
          std::to_string(funnel_window.candidate_probe_fee_overrides) +
          ", candidate_probe_filtered_fee=" +
          std::to_string(funnel_window.candidate_probe_filtered_fee) +
          ", candidate_probe_enqueued=" +
          std::to_string(funnel_window.candidate_probe_enqueued) +
          ", candidate_probe_fills=" +
          std::to_string(funnel_window.candidate_probe_fills) +
          ", candidate_probe_pending_timeouts=" +
          std::to_string(funnel_window.candidate_probe_pending_timeouts) +
          ", candidate_probe_cancel_submitted=" +
          std::to_string(funnel_window.candidate_probe_cancel_submitted) +
          ", candidate_probe_cancel_ok=" +
          std::to_string(funnel_window.candidate_probe_cancel_ok) +
          ", candidate_probe_cancel_failed=" +
          std::to_string(funnel_window.candidate_probe_cancel_failed) +
          ", candidate_probe_reprices=" +
          std::to_string(funnel_window.candidate_probe_reprices) +
          ", candidate_probe_taker_fallbacks=" +
          std::to_string(funnel_window.candidate_probe_taker_fallbacks) +
          ", candidate_probe_expired_without_fill=" +
          std::to_string(funnel_window.candidate_probe_expired_without_fill) +
          ", candidate_probe_skipped_trade_not_ok=" +
          std::to_string(funnel_window.candidate_probe_skipped_trade_not_ok) +
          ", candidate_probe_skipped_existing_intent=" +
          std::to_string(funnel_window.candidate_probe_skipped_existing_intent) +
          ", candidate_probe_skipped_pending_orders=" +
          std::to_string(funnel_window.candidate_probe_skipped_pending_orders) +
          ", candidate_probe_skipped_exposure=" +
          std::to_string(funnel_window.candidate_probe_skipped_exposure) +
          ", candidate_probe_skipped_trend_ratio=" +
          std::to_string(funnel_window.candidate_probe_skipped_trend_ratio) +
          ", candidate_probe_skipped_strong_trend_ratio=" +
          std::to_string(
              funnel_window.candidate_probe_skipped_strong_trend_ratio) +
          ", candidate_probe_skipped_cooldown=" +
          std::to_string(funnel_window.candidate_probe_skipped_cooldown) +
          ", candidate_probe_skipped_window_limit=" +
          std::to_string(funnel_window.candidate_probe_skipped_window_limit) +
          ", candidate_probe_skipped_direction=" +
          std::to_string(funnel_window.candidate_probe_skipped_direction) +
          ", candidate_probe_skipped_invalid_price=" +
          std::to_string(funnel_window.candidate_probe_skipped_invalid_price) +
          ", candidate_probe_skipped_notional=" +
          std::to_string(funnel_window.candidate_probe_skipped_notional) +
          ", candidate_probe_skipped_budget=" +
          std::to_string(funnel_window.candidate_probe_skipped_budget) +
          ", candidate_probe_skipped_build_intent=" +
          std::to_string(funnel_window.candidate_probe_skipped_build_intent) +
          ", candidate_probe_notional_abs_usd=" +
          std::to_string(funnel_window.candidate_probe_notional_abs_usd_sum) +
          ", async_ok=" + std::to_string(funnel_window.async_submit_ok) +
          ", async_failed=" +
          std::to_string(funnel_window.async_submit_failed) +
          ", fills=" + std::to_string(funnel_window.fills_applied) +
          ", fills_notional_abs_usd=" +
          std::to_string(funnel_window.fills_notional_abs_usd_sum) +
          ", gate_alerts=" + std::to_string(funnel_window.gate_alerts) +
          ", evolution_updates=" +
          std::to_string(funnel_window.self_evolution_updates) +
          ", evolution_rollbacks=" +
          std::to_string(funnel_window.self_evolution_rollbacks) +
          ", evolution_skipped=" +
          std::to_string(funnel_window.self_evolution_skipped) +
          ", entry_edge_samples=" +
          std::to_string(funnel_window.entry_edge_samples) +
          ", entry_edge_avg_bps=" + std::to_string(entry_edge_avg_bps) +
          ", entry_base_required_avg_bps=" +
          std::to_string(entry_base_required_edge_avg_bps) +
          ", entry_required_avg_bps=" +
          std::to_string(entry_required_edge_avg_bps) +
          ", entry_adaptive_relax_avg_bps=" +
          std::to_string(entry_adaptive_relax_avg_bps) +
          ", entry_maker_relax_avg_bps=" +
          std::to_string(entry_maker_relax_avg_bps) +
          ", entry_regime_adjust_avg_bps=" +
          std::to_string(entry_regime_adjust_avg_bps) +
          ", entry_volatility_adjust_avg_bps=" +
          std::to_string(entry_volatility_adjust_avg_bps) +
          ", entry_liquidity_adjust_avg_bps=" +
          std::to_string(entry_liquidity_adjust_avg_bps) +
          ", entry_concentration_adjust_avg_bps=" +
          std::to_string(entry_concentration_adjust_avg_bps) +
          ", entry_quality_guard_penalty_avg_bps=" +
          std::to_string(entry_quality_guard_penalty_avg_bps) +
          ", maker_fills=" + std::to_string(funnel_window.fills_maker_count) +
          ", taker_fills=" + std::to_string(funnel_window.fills_taker_count) +
          ", unknown_fills=" +
          std::to_string(funnel_window.fills_unknown_liquidity_count) +
          ", explicit_liquidity_fills=" +
          std::to_string(funnel_window.fills_explicit_liquidity_count) +
          ", fee_sign_fallback_fills=" +
          std::to_string(funnel_window.fills_fee_sign_fallback_count) + "}" +
          ", regime_window={trend_ticks=" +
          std::to_string(funnel_window.regime_trend_ticks) +
          ", range_ticks=" + std::to_string(funnel_window.regime_range_ticks) +
          ", extreme_ticks=" +
          std::to_string(funnel_window.regime_extreme_ticks) +
          ", warmup_ticks=" +
          std::to_string(funnel_window.regime_warmup_ticks) +
          ", trend_candidate_ticks=" +
          std::to_string(funnel_window.regime_trend_candidate_ticks) +
          ", warmup_trend_candidate_ticks=" +
          std::to_string(funnel_window.regime_warmup_trend_candidate_ticks) +
          "}" +
          ", regime_current={symbol=" +
          std::string(has_last_regime_state_ ? last_regime_state_.symbol : "n/a") +
          ", regime=" +
          std::string(has_last_regime_state_
                          ? ToString(last_regime_state_.regime)
                          : "n/a") +
          ", bucket=" +
          std::string(has_last_regime_state_
                          ? ToString(last_regime_state_.bucket)
                          : "n/a") +
          ", warmup=" +
          std::string(has_last_regime_state_ && last_regime_state_.warmup ? "true"
                                                                            : "false") +
          ", decision_interval_ms=" +
          std::to_string(has_last_regime_state_
                             ? last_regime_state_.decision_interval_ms
                             : 0) +
          ", aggregated_events=" +
          std::to_string(has_last_regime_state_
                             ? last_regime_state_.aggregated_event_count
                             : 0) +
          ", trend_threshold_ratio=" +
          std::to_string(has_last_regime_state_
                             ? last_regime_state_.trend_threshold_ratio
                             : 0.0) +
          ", volatility_threshold_ratio=" +
          std::to_string(has_last_regime_state_
                             ? last_regime_state_.volatility_threshold_ratio
                             : 0.0) +
          ", trend_candidate=" +
          std::string(has_last_regime_state_ &&
                              last_regime_state_.trend_candidate
                          ? "true"
                          : "false") +
          ", warmup_trend_candidate=" +
          std::string(has_last_regime_state_ &&
                              last_regime_state_.warmup_trend_candidate
                          ? "true"
                          : "false") +
          ", raw_regime=" +
          std::string(has_last_regime_state_
                          ? ToString(last_regime_state_.raw_regime)
                          : "n/a") +
          ", raw_bucket=" +
          std::string(has_last_regime_state_
                          ? ToString(last_regime_state_.raw_bucket)
                          : "n/a") +
          ", pending_regime=" +
          std::string(has_last_regime_state_
                          ? ToString(last_regime_state_.pending_regime)
                          : "n/a") +
          ", pending_bucket=" +
          std::string(has_last_regime_state_
                          ? ToString(last_regime_state_.pending_bucket)
                          : "n/a") +
          ", pending_regime_ticks=" +
          std::to_string(has_last_regime_state_
                             ? last_regime_state_.pending_regime_ticks
                             : 0) +
          ", confirm_ticks_required=" +
          std::to_string(has_last_regime_state_
                             ? last_regime_state_.confirm_ticks_required
                             : 0) +
          ", pending_regime_elapsed_ms=" +
          std::to_string(has_last_regime_state_
                             ? last_regime_state_.pending_regime_elapsed_ms
                             : 0) +
          ", confirm_elapsed_ms_required=" +
          std::to_string(has_last_regime_state_
                             ? last_regime_state_.confirm_elapsed_ms_required
                             : 0) +
          ", pending_trend_confirmation=" +
          std::string(has_last_regime_state_ &&
                              last_regime_state_.pending_trend_confirmation
                          ? "true"
                          : "false") +
          "}" +
          ", shadow_latest={enabled=" +
          std::string(has_last_shadow_inference_ &&
                              last_shadow_inference_.enabled
                          ? "true"
                          : "false") +
          ", model_version=" +
          std::string(has_last_shadow_inference_
                          ? last_shadow_inference_.model_version
                          : "n/a") +
          ", model_score=" +
          std::to_string(has_last_shadow_inference_
                             ? last_shadow_inference_.model_score
                             : 0.0) +
          ", p_up=" +
          std::to_string(has_last_shadow_inference_ ? last_shadow_inference_.p_up
                                                    : 0.0) +
          ", p_down=" +
          std::to_string(has_last_shadow_inference_
                             ? last_shadow_inference_.p_down
                             : 0.0) +
          "}" +
          ", shadow_window={scored=" +
          std::to_string(funnel_window.integrator_scored) +
          ", pred_up=" + std::to_string(funnel_window.integrator_pred_up) +
          ", pred_down=" + std::to_string(funnel_window.integrator_pred_down) +
          ", policy_proposed=" +
          std::to_string(funnel_window.integrator_policy_proposed) +
          ", policy_risk_accepted=" +
          std::to_string(funnel_window.integrator_policy_risk_accepted) +
          ", policy_applied=" +
          std::to_string(funnel_window.integrator_policy_applied) +
          ", policy_canary=" +
          std::to_string(funnel_window.integrator_policy_canary) +
          ", policy_active=" +
          std::to_string(funnel_window.integrator_policy_active) +
          ", policy_filled=" +
          std::to_string(funnel_window.integrator_policy_filled) +
          ", avg_model_score=" + std::to_string(shadow_avg_model_score) +
          ", avg_p_up=" + std::to_string(shadow_avg_p_up) +
          ", avg_p_down=" + std::to_string(shadow_avg_p_down) + "}" +
          ", strategy_mix={latest_trend_notional=" +
          std::to_string(has_last_strategy_signal_
                             ? last_strategy_signal_.trend_notional_usd
                             : 0.0) +
          ", latest_defensive_notional=" +
          std::to_string(has_last_strategy_signal_
                             ? last_strategy_signal_.defensive_notional_usd
                             : 0.0) +
          ", latest_blended_notional=" +
          std::to_string(has_last_strategy_signal_
                             ? last_strategy_signal_.suggested_notional_usd
                             : 0.0) +
          ", avg_abs_trend_notional=" +
          std::to_string(strategy_trend_avg_abs_notional) +
          ", avg_abs_defensive_notional=" +
          std::to_string(strategy_defensive_avg_abs_notional) +
          ", avg_abs_blended_notional=" +
          std::to_string(strategy_blended_avg_abs_notional) +
          ", samples=" + std::to_string(funnel_window.strategy_mix_samples) +
          ", policy_flat_samples=" +
          std::to_string(funnel_window.strategy_policy_flat_samples) + "}" +
          ", integrator_mode=" +
          std::string(ToString(system_.integrator_mode())) +
          ", gate_runtime={enabled=" +
          std::string(config_.gate.enforce_runtime_actions ? "true" : "false") +
          ", fail_streak=" + std::to_string(gate_fail_windows_streak_) +
          ", pass_streak=" + std::to_string(gate_pass_windows_streak_) +
          ", reduce_only=" +
          std::string(gate_forced_reduce_only_ ? "true" : "false") +
          ", reduce_only_cooldown_ticks=" +
          std::to_string(gate_reduce_only_cooldown_ticks_left_) +
          ", gate_halted=" + std::string(gate_halted_ ? "true" : "false") +
          ", halt_cooldown_ticks=" +
          std::to_string(gate_halt_cooldown_ticks_left_) +
          ", flat_ticks=" + std::to_string(gate_flat_ticks_streak_) + "}" +
          ", throttle_window={checks=" + std::to_string(throttle_window.checks) +
          ", rejected=" + std::to_string(throttle_window.rejected) +
          ", interval_rejects=" +
          std::to_string(throttle_window.interval_rejects) +
          ", reverse_rejects=" +
          std::to_string(throttle_window.reverse_rejects) +
          ", hit_rate=" + std::to_string(throttle_window_hit_rate) + "}" +
          ", throttle_total={checks=" + std::to_string(throttle_total.checks) +
          ", rejected=" + std::to_string(throttle_total.rejected) +
          ", hit_rate=" + std::to_string(throttle_total_hit_rate) + "}" +
          ", entry_gate={enabled=" +
          std::string(config_.execution_enable_fee_aware_entry_gate ? "true"
                                                                    : "false") +
          ", round_trip_cost_bps=" + std::to_string(RoundTripCostBps()) +
          ", min_expected_edge_bps=" +
          std::to_string(config_.execution_min_expected_edge_bps) +
          ", required_edge_cap_bps=" +
          std::to_string(config_.execution_required_edge_cap_bps) +
          ", adaptive_enabled=" +
          std::string(config_.execution_adaptive_fee_gate_enabled ? "true"
                                                                  : "false") +
          ", adaptive_trigger_ratio=" +
          std::to_string(config_.execution_adaptive_fee_gate_trigger_ratio) +
          ", adaptive_max_relax_bps=" +
          std::to_string(config_.execution_adaptive_fee_gate_max_relax_bps) +
          ", adaptive_min_samples=" +
          std::to_string(config_.execution_adaptive_fee_gate_min_samples) +
          ", maker_edge_relax_bps=" +
          std::to_string(config_.execution_maker_edge_relax_bps) +
          ", near_miss_tolerance_bps=" +
          std::to_string(config_.execution_entry_gate_near_miss_tolerance_bps) +
          ", near_miss_maker_allow=" +
          std::string(config_.execution_entry_gate_near_miss_maker_allow ? "true"
                                                                          : "false") +
          ", near_miss_maker_extra_gap_bps=" +
          std::to_string(config_.execution_entry_gate_near_miss_maker_max_gap_bps) +
          ", near_miss_maker_max_gap_bps=" +
          std::to_string(config_.execution_entry_gate_near_miss_maker_max_gap_bps) +
          ", quality_guard_penalty_bps=" +
          std::to_string(execution_quality_required_edge_penalty_bps_) +
          ", symbol_quality_guard_active_count=" +
          std::to_string(ActiveSymbolExecutionQualityGuardCount()) +
          ", quality_guard_floor_bps=" +
          std::to_string(config_.execution_quality_guard_required_edge_floor_bps) +
          ", concentration_top1_share_threshold=" +
          std::to_string(config_.execution_concentration_top1_share_threshold) +
          ", concentration_penalty_bps=" +
          std::to_string(config_.execution_concentration_penalty_bps) +
          ", concentration_min_symbols=" +
          std::to_string(config_.execution_concentration_min_symbols) +
          ", observed_filtered_ratio=" +
          std::to_string(entry_gate_observed_filtered_ratio_) +
          ", observed_near_miss_ratio=" +
          std::to_string(entry_gate_observed_near_miss_ratio_) +
          ", observed_near_miss_allowed_ratio=" +
          std::to_string(entry_gate_observed_near_miss_allowed_ratio_) +
          ", cooldown_trigger_count=" +
          std::to_string(config_.execution_cost_filter_cooldown_trigger_count) +
          ", cooldown_ticks=" +
          std::to_string(config_.execution_cost_filter_cooldown_ticks) +
          ", cooldown_symbols_active=" +
          std::to_string(cost_filter_cooldown_until_tick_by_symbol_.size()) + "}" +
          ", execution_window={filtered_cost_ratio=" +
          std::to_string(filtered_cost_ratio) +
          ", filtered_cost_near_miss_ratio=" +
          std::to_string(filtered_cost_near_miss_ratio) +
          ", passed_cost_near_miss_ratio=" +
          std::to_string(passed_cost_near_miss_ratio) +
          ", rebalance_gap_avg_abs_usd=" +
          std::to_string(rebalance_gap_avg_abs_usd) +
          ", rebalance_gap_max_abs_usd=" +
          std::to_string(funnel_window.rebalance_gap_abs_usd_max) +
          ", rebalance_within_min_notional_avg_abs_usd=" +
          std::to_string(rebalance_gap_within_min_notional_avg_abs_usd) +
          ", rebalance_within_min_notional_ratio=" +
          std::to_string(rebalance_gap_within_min_notional_ratio) +
          ", min_rebalance_notional_usd=" +
          std::to_string(config_.execution_min_rebalance_notional_usd) +
          ", same_side_rebalance_multiplier=" +
          std::to_string(config_.execution_same_side_rebalance_multiplier) +
          ", entry_edge_gap_avg_bps=" + std::to_string(entry_edge_gap_avg_bps) +
          ", candidate_probe_cost_gate_samples=" +
          std::to_string(funnel_window.candidate_probe_cost_gate_samples) +
          ", candidate_probe_cost_gate_long_count=" +
          std::to_string(funnel_window.candidate_probe_cost_gate_long_count) +
          ", candidate_probe_cost_gate_short_count=" +
          std::to_string(funnel_window.candidate_probe_cost_gate_short_count) +
          ", candidate_probe_cost_gate_expected_edge_avg_bps=" +
          std::to_string(candidate_probe_cost_gate_expected_edge_avg_bps) +
          ", candidate_probe_cost_gate_required_edge_avg_bps=" +
          std::to_string(candidate_probe_cost_gate_required_edge_avg_bps) +
          ", candidate_probe_cost_gate_edge_gap_avg_bps=" +
          std::to_string(candidate_probe_cost_gate_edge_gap_avg_bps) +
          ", candidate_probe_cost_gate_edge_gap_max_bps=" +
          std::to_string(funnel_window.candidate_probe_cost_gate_edge_gap_bps_max) +
          ", candidate_probe_cost_gate_trend_ratio_avg=" +
          std::to_string(candidate_probe_cost_gate_trend_ratio_avg) +
          ", realized_net_delta_usd=" +
          std::to_string(window_realized_net_delta_usd) +
          ", realized_net_per_fill=" +
          std::to_string(window_realized_net_per_fill_usd) +
          ", fee_delta_usd=" + std::to_string(window_fee_delta_usd) +
          ", fee_bps_per_fill=" + std::to_string(window_fee_bps_per_fill) +
          ", maker_fills=" + std::to_string(funnel_window.fills_maker_count) +
          ", taker_fills=" + std::to_string(funnel_window.fills_taker_count) +
          ", unknown_fills=" +
          std::to_string(funnel_window.fills_unknown_liquidity_count) +
          ", explicit_liquidity_fills=" +
          std::to_string(funnel_window.fills_explicit_liquidity_count) +
          ", fee_sign_fallback_fills=" +
          std::to_string(funnel_window.fills_fee_sign_fallback_count) +
          ", unknown_fill_ratio=" + std::to_string(window_unknown_fill_ratio) +
          ", explicit_liquidity_fill_ratio=" +
          std::to_string(window_explicit_liquidity_fill_ratio) +
          ", fee_sign_fallback_fill_ratio=" +
          std::to_string(window_fee_sign_fallback_fill_ratio) +
          ", maker_fee_bps=" + std::to_string(window_maker_fee_bps) +
          ", taker_fee_bps=" + std::to_string(window_taker_fee_bps) +
          ", maker_fill_ratio=" + std::to_string(window_maker_fill_ratio) +
          "}" +
          ", execution_quality_guard={enabled=" +
          std::string(config_.execution_quality_guard_enabled ? "true"
                                                              : "false") +
          ", active=" +
          std::string(execution_quality_guard_active_ ? "true" : "false") +
          ", bad_streak=" + std::to_string(execution_quality_bad_streak_) +
          ", good_streak=" + std::to_string(execution_quality_good_streak_) +
          ", no_fill_windows=" +
          std::to_string(execution_quality_no_fill_windows_) +
          ", min_fills=" +
          std::to_string(config_.execution_quality_guard_min_fills) +
          ", trigger_streak=" +
          std::to_string(config_.execution_quality_guard_bad_streak_to_trigger) +
          ", release_streak=" +
          std::to_string(
              config_.execution_quality_guard_good_streak_to_release) +
          ", min_realized_net_per_fill_usd=" +
          std::to_string(
              config_.execution_quality_guard_min_realized_net_per_fill_usd) +
          ", max_fee_bps_per_fill=" +
          std::to_string(EffectiveQualityGuardMaxFeeBps(config_)) +
          ", applied_penalty_bps=" +
          std::to_string(execution_quality_required_edge_penalty_bps_) +
          ", symbol_active_count=" +
          std::to_string(ActiveSymbolExecutionQualityGuardCount()) +
          ", symbol_state_count=" +
          std::to_string(execution_quality_by_symbol_.size()) + "}" +
          ", reconcile_runtime={anomaly_streak=" +
          std::to_string(reconcile_anomaly_streak_) +
          ", healthy_streak=" + std::to_string(reconcile_healthy_streak_) +
          ", anomaly_reduce_only=" +
          std::string(reconcile_forced_reduce_only_ ? "true" : "false") +
          ", anomaly_reduce_only_threshold=" +
          std::to_string(config_.reconcile.anomaly_reduce_only_streak) +
          ", anomaly_halt_threshold=" +
          std::to_string(config_.reconcile.anomaly_halt_streak) +
          ", anomaly_resume_threshold=" +
          std::to_string(config_.reconcile.anomaly_resume_streak) +
          ", anomaly_halted=" +
          std::string(reconcile_halted_ ? "true" : "false") + "}" +
          ", evolution={enabled=" +
          std::string(evolution_enabled ? "true" : "false") +
          ", objective={alpha_pnl=" +
          std::to_string(config_.self_evolution.objective_alpha_pnl) +
          ", beta_drawdown=" +
          std::to_string(config_.self_evolution.objective_beta_drawdown) +
          ", gamma_notional_churn=" +
          std::to_string(
              config_.self_evolution.objective_gamma_notional_churn) +
          "}" +
          ", factor_ic_weighting=" +
          std::string(config_.self_evolution.enable_factor_ic_adaptive_weights
                          ? "true"
                          : "false") +
          ", factor_ic_min_samples=" +
          std::to_string(config_.self_evolution.factor_ic_min_samples) +
          ", factor_ic_min_abs=" +
          std::to_string(config_.self_evolution.factor_ic_min_abs) +
          ", learnability_gate=" +
          std::string(config_.self_evolution.enable_learnability_gate ? "true"
                                                                      : "false") +
          ", learnability_min_samples=" +
          std::to_string(config_.self_evolution.learnability_min_samples) +
          ", learnability_min_t_stat_abs=" +
          std::to_string(config_.self_evolution.learnability_min_t_stat_abs) +
          ", min_effective_weight_delta=" +
          std::to_string(config_.self_evolution.min_effective_weight_delta) +
          ", active_bucket=" + std::string(ToString(active_bucket)) +
          ", active_trend_weight=" +
          std::to_string(active_evolution_weights.trend_weight) +
          ", active_defensive_weight=" +
          std::to_string(active_evolution_weights.defensive_weight) +
          ", by_bucket={trend=(" + std::to_string(evolution_weights[0].trend_weight) +
          "," + std::to_string(evolution_weights[0].defensive_weight) + ")" +
          ", range=(" + std::to_string(evolution_weights[1].trend_weight) +
          "," + std::to_string(evolution_weights[1].defensive_weight) + ")" +
          ", extreme=(" + std::to_string(evolution_weights[2].trend_weight) +
          "," + std::to_string(evolution_weights[2].defensive_weight) + ")}" +
          ", next_eval_tick=" +
          std::to_string(self_evolution_.next_eval_tick()) +
          ", cooldown=" + std::string(evolution_cooldown ? "true" : "false") +
          ", cooldown_remaining_ticks=" +
          std::to_string(evolution_cooldown_remaining) + "}");
  struct CandidateEpisodeSummary {
    std::string candidate_id;
    std::string model_version;
    std::string runtime_config_sha256;
    std::string trade_bot_sha256;
    int total_episode_count{0};
    int complete_episode_count{0};
    int positive_episode_count{0};
    double realized_net_usd{0.0};
    double realized_net_usd_sum_squares{0.0};
  };
  std::unordered_map<std::string, CandidateEpisodeSummary> candidate_summaries;
  for (const auto& [episode_id, closure] : persisted_episode_closures_) {
    (void)episode_id;
    const std::string key = closure.candidate_id + "|" +
                            closure.model_version + "|" +
                            closure.runtime_config_sha256 + "|" +
                            closure.trade_bot_sha256;
    auto& summary = candidate_summaries[key];
    summary.candidate_id = closure.candidate_id;
    summary.model_version = closure.model_version;
    summary.runtime_config_sha256 = closure.runtime_config_sha256;
    summary.trade_bot_sha256 = closure.trade_bot_sha256;
    ++summary.total_episode_count;
    if (closure.evidence_complete) {
      ++summary.complete_episode_count;
      summary.realized_net_usd += closure.realized_net_usd;
      summary.realized_net_usd_sum_squares +=
          closure.realized_net_usd * closure.realized_net_usd;
      if (closure.realized_net_usd > 0.0) {
        ++summary.positive_episode_count;
      }
    }
  }
  for (const auto& [identity, summary] : candidate_summaries) {
    (void)identity;
    LogInfo("INTEGRATOR_CANDIDATE_EPISODE_SUMMARY: candidate_id=" +
            summary.candidate_id + ", model_version=" +
            summary.model_version + ", runtime_config_sha256=" +
            summary.runtime_config_sha256 + ", trade_bot_sha256=" +
            summary.trade_bot_sha256 + ", total_episode_count=" +
            std::to_string(summary.total_episode_count) +
            ", complete_episode_count=" +
            std::to_string(summary.complete_episode_count) +
            ", positive_episode_count=" +
            std::to_string(summary.positive_episode_count) +
            ", realized_net_usd=" +
            std::to_string(summary.realized_net_usd) +
            ", realized_net_usd_sum_squares=" +
            std::to_string(summary.realized_net_usd_sum_squares));
  }
  last_status_realized_net_pnl_usd_ =
      system_.account().cumulative_realized_net_pnl_usd();
  last_status_fee_usd_ = system_.account().cumulative_fee_usd();
  has_last_status_account_snapshot_ = true;
}

/**
 * @brief 退出条件判断
 *
 * - live/paper: 受 system_max_ticks 控制；
 * - replay: 数据耗尽后自动退出。
 */
bool BotApplication::ShouldExit(bool has_market, bool has_fill) {
  if (config_.mode == "replay") {
    const bool data_exhausted = !has_market && !has_fill;
    const bool max_ticks_reached =
        config_.system_max_ticks > 0 &&
        market_tick_count_ >= config_.system_max_ticks;
    if (replay_terminal_settlement_started_ || data_exhausted ||
        max_ticks_reached) {
      return AdvanceReplayTerminalSettlement();
    }
    return false;
  }
  if (config_.system_max_ticks > 0 &&
      market_tick_count_ >= config_.system_max_ticks) {
    return true;
  }
  return false;
}

bool BotApplication::AdvanceReplayTerminalSettlement() {
  if (config_.mode != "replay") {
    return false;
  }
  if (replay_terminal_settlement_failed_) {
    LogError("REPLAY_TERMINAL_SETTLEMENT_FAILED: reason=prior_async_failure");
    return true;
  }
  if (++replay_terminal_settlement_idle_polls_ > 1000) {
    replay_terminal_settlement_failed_ = true;
    LogError("REPLAY_TERMINAL_SETTLEMENT_FAILED: reason=timeout");
    return true;
  }

  if (!replay_terminal_settlement_started_) {
    replay_terminal_settlement_started_ = true;
    const auto pending_order_ids = oms_.PendingOrderIds();
    for (const auto& order_id : pending_order_ids) {
      oms_.MarkCancelPending(order_id);
      executor_->Cancel(order_id);
    }
    LogInfo("REPLAY_TERMINAL_SETTLEMENT_START: pending_orders=" +
            std::to_string(pending_order_ids.size()) +
            ", gross_notional_usd=" +
            std::to_string(system_.account().gross_notional_usd()));
    return false;
  }

  if (oms_.HasPendingOrders()) {
    return false;
  }

  const auto active_symbols = system_.account().GetActiveSymbols();
  if (!active_symbols.empty() && !replay_terminal_close_submitted_) {
    replay_terminal_close_submitted_ = true;
    int submitted_count = 0;
    for (const auto& symbol : active_symbols) {
      const double position_qty = system_.account().position_qty(symbol);
      const double mark_price = system_.account().mark_price(symbol);
      if (std::fabs(position_qty) <= kNotionalEpsilon) {
        continue;
      }
      if (!std::isfinite(mark_price) || mark_price <= kNotionalEpsilon) {
        replay_terminal_settlement_failed_ = true;
        LogError("REPLAY_TERMINAL_SETTLEMENT_FAILED: reason=invalid_mark_price"
                 ", symbol=" +
                 symbol + ", mark_price=" + std::to_string(mark_price));
        return true;
      }

      OrderIntent close_intent;
      close_intent.client_order_id =
          "replay-terminal-close-" + std::to_string(market_tick_count_) +
          "-" + symbol;
      close_intent.symbol = symbol;
      close_intent.purpose = OrderPurpose::kReduce;
      close_intent.direction = position_qty > 0.0 ? -1 : 1;
      close_intent.qty = std::fabs(position_qty);
      close_intent.price = mark_price;
      close_intent.reduce_only = true;
      close_intent.liquidity_preference = LiquidityPreference::kTaker;
      replay_terminal_close_order_ids_.insert(close_intent.client_order_id);
      if (!EnqueueIntent(close_intent, nullptr)) {
        replay_terminal_settlement_failed_ = true;
        LogError("REPLAY_TERMINAL_SETTLEMENT_FAILED: reason=close_enqueue_failed"
                 ", symbol=" +
                 symbol);
        return true;
      }
      ++submitted_count;
    }
    LogInfo("REPLAY_TERMINAL_CLOSE_SUBMITTED: order_count=" +
            std::to_string(submitted_count));
    return false;
  }

  if (!system_.account().GetActiveSymbols().empty() ||
      oms_.HasPendingOrders()) {
    return false;
  }

  LogInfo("REPLAY_TERMINAL_SETTLEMENT_DONE: position_count=0"
          ", realized_net_usd=" +
          std::to_string(
              system_.account().cumulative_realized_net_pnl_usd()) +
          ", fees_usd=" +
          std::to_string(system_.account().cumulative_fee_usd()) +
          ", funding_paid_usd=" +
          std::to_string(system_.account().cumulative_funding_paid_usd()));
  return true;
}

// 停机顺序：先停执行线程，再输出结束日志。
void BotApplication::Shutdown() {
  if (executor_) executor_->Stop();
  LogInfo("Bot Shutdown.");
}

}  // namespace ai_trade
