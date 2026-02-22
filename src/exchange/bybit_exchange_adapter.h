#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "exchange/bybit_public_stream.h"
#include "exchange/bybit_private_stream.h"
#include "exchange/bybit_rest_client.h"
#include "exchange/exchange_adapter.h"

namespace ai_trade {

/// Bybit symbol 交易规则（来自 instruments-info）。
struct BybitSymbolTradeRule {
  bool tradable{true};  ///< 当前 symbol 是否可交易。
  double qty_step{0.0};  ///< 下单数量步长（qtyStep）。
  double min_order_qty{0.0};  ///< 最小下单数量（minOrderQty）。
  double max_mkt_order_qty{0.0};  ///< 市价单最大数量（maxMktOrderQty）。
  double min_notional_value{0.0};  ///< 最小名义金额（minNotionalValue）。
  double price_tick{0.0};  ///< 价格最小变动单位（tickSize）。
  int price_precision{8};  ///< 价格格式化精度。
  std::int64_t price_scale{0};  ///< 价格小数位对应的 10^scale。
  std::int64_t price_tick_units{0};  ///< tick 在整数尺度下的单位值。
  int qty_precision{8};  ///< 数量格式化精度。
  std::int64_t qty_scale{0};  ///< 数量小数位对应的 10^scale。
  std::int64_t qty_step_units{0};  ///< qtyStep 在整数尺度下的单位值。
};

/**
 * @brief Bybit 适配器配置项
 *
 * 涵盖运行模式、WS 开关、执行轮询策略、账户模式期望与可测试注入点。
 */
struct BybitAdapterOptions {
  bool testnet{true};  ///< 是否连接 testnet。
  bool demo_trading{false};  ///< 是否连接 demo trading（mainnet demo）。
  bool allow_no_auth_in_replay{true};  ///< replay 下是否允许无 AK/SK 启动。
  std::string mode{"replay"};  ///< 运行模式：replay/paper/live。
  std::string category{"linear"};  ///< Bybit V5 category（默认 linear）。
  std::string account_type{"UNIFIED"};  ///< 账户类型（日志与校验用途）。
  std::string primary_symbol{"BTCUSDT"};  ///< 主交易标的（兜底 symbol）。
  bool public_ws_enabled{true};  ///< 是否启用公共 WS 行情。
  bool public_ws_rest_fallback{true};  ///< 公共 WS 异常时是否降级 REST。
  bool private_ws_enabled{true};  ///< 是否启用私有 WS 成交通道。
  bool private_ws_rest_fallback{true};  ///< 私有 WS 异常时是否降级 REST。
  int ws_reconnect_interval_ms{
      15000};  ///< WS 降级后重连尝试间隔（毫秒，0=每轮都尝试）。
  // 启动时预热 execution 游标，避免把历史成交误当作新成交推进本地状态。
  bool execution_skip_history_on_start{true};
  int execution_poll_limit{50};  ///< `/v5/execution/list` 轮询 limit。
  bool maker_entry_enabled{false};  ///< 开仓优先用 maker limit，减少 taker 成本。
  bool maker_fallback_to_market{
      true};  ///< maker PostOnly 被拒时是否自动回退一次市价单。
  double maker_price_offset_bps{1.0};  ///< maker 限价相对参考价偏移（bps）。
  bool maker_post_only{true};  ///< maker 模式是否强制 PostOnly。
  std::vector<std::string> symbols{"BTCUSDT"};  ///< 启动时关注的 symbol 列表。
  std::vector<double> replay_prices{100.0, 100.5, 100.3, 100.8, 100.4, 100.9};  ///< replay 行情序列。
  AccountMode remote_account_mode{AccountMode::kUnified};  ///< 期望远端账户模式。
  MarginMode remote_margin_mode{MarginMode::kIsolated};  ///< 期望远端保证金模式。
  PositionMode remote_position_mode{PositionMode::kOneWay};  ///< 期望远端持仓模式。
  std::function<std::unique_ptr<BybitHttpTransport>()> http_transport_factory;  ///< HTTP 注入点（测试用）。
  std::function<std::unique_ptr<BybitPrivateStream>(BybitPrivateStreamOptions)>
      private_stream_factory;  ///< 私有 WS 注入点（测试用）。
  std::function<std::unique_ptr<BybitPublicStream>(BybitPublicStreamOptions)>
      public_stream_factory;  ///< 公共 WS 注入点（测试用）。
};

/**
 * @brief Bybit V5 交易所适配器
 *
 * 核心职责：
 * 1. 建连初始化（规则加载、账户模式快照、通道选择）；
 * 2. 行情与成交统一抽象（WS 优先，按配置降级 REST）；
 * 3. 下单/撤单、symbol 规则校验、远端持仓/净名义敞口查询。
 */
class BybitExchangeAdapter : public ExchangeAdapter {
 public:
  explicit BybitExchangeAdapter(BybitAdapterOptions options)
      : options_(std::move(options)) {}

  /// 返回当前通道组合名称（`bybit_ws/bybit_mixed/bybit_rest/bybit_stub`）。
  std::string Name() const override;
  /// 建立连接并完成初始化（失败时返回 false）。
  bool Connect() override;
  /// 拉取一条行情（Replay/WS/REST 统一入口）。
  bool PollMarket(MarketEvent* out_event) override;
  /// 提交一笔订单（内部应用 symbol 规则与数量校验）。
  bool SubmitOrder(const OrderIntent& intent) override;
  /// 按 client_order_id 撤单。
  bool CancelOrder(const std::string& client_order_id) override;
  /// 拉取一条成交回报（Replay/WS/REST 统一入口）。
  bool PollFill(FillEvent* out_fill) override;
  /// 获取远端净名义敞口（USD, signed），用于快速对账。
  bool GetRemoteNotionalUsd(double* out_notional_usd) const override;
  /// 获取远端账户模式快照。
  bool GetAccountSnapshot(ExchangeAccountSnapshot* out_snapshot) const override;
  /// 获取远端持仓快照（多 symbol）。
  bool GetRemotePositions(
      std::vector<RemotePositionSnapshot>* out_positions) const override;
  /// 获取远端账户资金快照（equity/wallet/upnl）。
  bool GetRemoteAccountBalance(
      RemoteAccountBalanceSnapshot* out_balance) const override;
  /// 获取远端活动订单 client_order_id 集合（来自 `/v5/order/realtime`）。
  bool GetRemoteOpenOrderClientIds(
      std::unordered_set<std::string>* out_client_order_ids) const override;
  /// 获取 symbol 交易规则（步长/最小量/最小名义等）。
  bool GetSymbolInfo(const std::string& symbol,
                     SymbolInfo* out_info) const override;
  /// 交易通道健康检查，映射到上游 `trade_ok`。
  bool TradeOk() const override;
  /// 通道健康摘要（用于运行状态日志）。
  std::string ChannelHealthSummary() const;

 private:
  enum class FillChannel {
    kReplay,      ///< 回放模式（本地虚拟成交）。
    kPrivateWs,   ///< 私有 WS 成交通道。
    kRestPolling, ///< REST execution/list 轮询通道。
  };
  enum class MarketChannel {
    kReplay,      ///< 回放模式（本地行情序列）。
    kPublicWs,    ///< 公共 WS 行情通道。
    kRestPolling, ///< REST market/tickers 轮询通道。
  };

  /// 回放模式成交读取。
  bool PollFillFromReplay(FillEvent* out_fill);
  /// REST 模式成交读取（execution/list）。
  bool PollFillFromRest(FillEvent* out_fill);
  /// REST 模式行情读取（market/tickers）。
  bool PollMarketFromRest(MarketEvent* out_event);
  /// 从 pending 成交队列取出一条成交。
  bool DrainPendingFill(FillEvent* out_fill);
  /// 启动预热 execution 游标，避免历史成交重放。
  bool PrimeExecutionCursor();
  /// 记录 `orderId -> clientOrderId` 映射，用于回报归一化。
  void RememberOrderIdMapping(const std::string& order_id,
                              const std::string& client_order_id);
  /// 按 `orderLinkId` 优先、`orderId` 次之解析本地 client_order_id。
  std::string ResolveClientOrderId(const std::string& order_link_id,
                                   const std::string& order_id) const;
  /// 将成交中的 orderId 回写为本地 client_order_id（若可映射）。
  void CanonicalizeFillClientOrderId(FillEvent* fill);
  /// 若当前为 REST 行情降级，按间隔尝试恢复 Public WS。
  void MaybeReconnectPublicWs();
  /// 若当前为 REST 成交降级，按间隔尝试恢复 Private WS。
  void MaybeReconnectPrivateWs();
  /// 当前毫秒时间戳（用于重连节流）。
  static std::int64_t CurrentTimestampMs();

  BybitAdapterOptions options_;  ///< 适配器配置快照。
  bool connected_{false};  ///< 建连状态。
  MarketChannel market_channel_{MarketChannel::kReplay};  ///< 当前行情通道。
  FillChannel fill_channel_{FillChannel::kReplay};  ///< 当前成交通道。
  std::unique_ptr<BybitRestClient> rest_client_;  ///< REST 客户端。
  std::unique_ptr<BybitPublicStream> public_stream_;  ///< 公共 WS 流。
  std::unique_ptr<BybitPrivateStream> private_stream_;  ///< 私有 WS 流。
  ExchangeAccountSnapshot account_snapshot_{};  ///< 启动阶段采样的账户模式快照。
  std::size_t replay_cursor_{0};  ///< replay 价格游标。
  std::size_t replay_symbol_cursor_{0};  ///< replay symbol 轮询游标。
  std::int64_t replay_seq_{0};  ///< replay 行情序号计数器。
  std::uint64_t fill_seq_{0};  ///< replay 成交序号计数器。
  std::unordered_map<std::string, std::string> order_symbol_by_client_id_;  ///< clientId->symbol 映射（撤单用）。
  mutable std::unordered_map<std::string, std::string>
      order_id_to_client_id_;  ///< orderId->clientId 映射（回报归一化/对账收敛）。
  std::unordered_set<std::string> observed_exec_ids_;  ///< 已处理 execution id 去重集合。
  std::deque<MarketEvent> pending_markets_;  ///< REST 批量行情待消费队列。
  std::unordered_map<std::string, double> last_price_by_symbol_;  ///< 最近标记价格缓存。
  std::unordered_map<std::string, std::int64_t>
      last_market_ts_ms_by_symbol_;  ///< 每个 symbol 最近行情时间戳（用于 interval）。
  std::unordered_map<std::string, double>
      last_volume_24h_by_symbol_;  ///< 每个 symbol 最近 volume24h（用于增量 volume）。
  std::unordered_map<std::string, double> remote_position_qty_by_symbol_;  ///< 远端仓位数量（signed）。
  std::unordered_map<std::string, BybitSymbolTradeRule> symbol_trade_rules_;  ///< symbol 交易规则缓存。
  std::deque<FillEvent> pending_fills_;  ///< 待消费成交队列。
  std::int64_t execution_watermark_ms_{0};  ///< execution 时间水位（去历史用）。
  bool execution_cursor_primed_{false};  ///< execution 游标是否已完成预热。
  std::int64_t last_public_ws_reconnect_attempt_ms_{
      0};  ///< 最近一次公共 WS 重连尝试时间。
  std::int64_t last_private_ws_reconnect_attempt_ms_{
      0};  ///< 最近一次私有 WS 重连尝试时间。
  mutable std::uint64_t open_order_diag_counter_{
      0};  ///< open-order 过滤诊断日志计数器（限频用）。
};

}  // namespace ai_trade
