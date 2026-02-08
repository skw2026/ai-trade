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

struct BybitSymbolTradeRule {
  bool tradable{true};
  double qty_step{0.0};
  double min_order_qty{0.0};
  double max_mkt_order_qty{0.0};
  double min_notional_value{0.0};
  double price_tick{0.0};
  int price_precision{8};
  std::int64_t price_scale{0};
  std::int64_t price_tick_units{0};
  int qty_precision{8};
  std::int64_t qty_scale{0};
  std::int64_t qty_step_units{0};
};

struct BybitAdapterOptions {
  bool testnet{true};
  bool demo_trading{false};
  bool allow_no_auth_in_replay{true};
  std::string mode{"replay"};
  std::string category{"linear"};
  std::string account_type{"UNIFIED"};
  std::string primary_symbol{"BTCUSDT"};
  bool public_ws_enabled{true};
  bool public_ws_rest_fallback{true};
  bool private_ws_enabled{true};
  bool private_ws_rest_fallback{true};
  // 启动时预热 execution 游标，避免把历史成交误当作新成交推进本地状态。
  bool execution_skip_history_on_start{true};
  int execution_poll_limit{50};
  std::vector<std::string> symbols{"BTCUSDT"};
  std::vector<double> replay_prices{100.0, 100.5, 100.3, 100.8, 100.4, 100.9};
  AccountMode remote_account_mode{AccountMode::kUnified};
  MarginMode remote_margin_mode{MarginMode::kIsolated};
  PositionMode remote_position_mode{PositionMode::kOneWay};
  std::function<std::unique_ptr<BybitHttpTransport>()> http_transport_factory;
  std::function<std::unique_ptr<BybitPrivateStream>(BybitPrivateStreamOptions)>
      private_stream_factory;
  std::function<std::unique_ptr<BybitPublicStream>(BybitPublicStreamOptions)>
      public_stream_factory;
};

// Bybit V5 接入占位：先完成接口语义对齐，后续替换成真实 WS/REST 实现。
class BybitExchangeAdapter : public ExchangeAdapter {
 public:
  explicit BybitExchangeAdapter(BybitAdapterOptions options)
      : options_(std::move(options)) {}

  std::string Name() const override;
  bool Connect() override;
  bool PollMarket(MarketEvent* out_event) override;
  bool SubmitOrder(const OrderIntent& intent) override;
  bool CancelOrder(const std::string& client_order_id) override;
  bool PollFill(FillEvent* out_fill) override;
  bool GetRemoteNotionalUsd(double* out_notional_usd) const override;
  bool GetAccountSnapshot(ExchangeAccountSnapshot* out_snapshot) const override;
  bool GetRemotePositions(
      std::vector<RemotePositionSnapshot>* out_positions) const override;
  bool GetSymbolInfo(const std::string& symbol,
                     SymbolInfo* out_info) const override;
  bool TradeOk() const override;
  std::string ChannelHealthSummary() const;

 private:
  enum class FillChannel {
    kReplay,
    kPrivateWs,
    kRestPolling,
  };
  enum class MarketChannel {
    kReplay,
    kPublicWs,
    kRestPolling,
  };

  bool PollFillFromReplay(FillEvent* out_fill);
  bool PollFillFromRest(FillEvent* out_fill);
  bool PollMarketFromRest(MarketEvent* out_event);
  bool DrainPendingFill(FillEvent* out_fill);
  bool PrimeExecutionCursor();

  BybitAdapterOptions options_;
  bool connected_{false};
  MarketChannel market_channel_{MarketChannel::kReplay};
  FillChannel fill_channel_{FillChannel::kReplay};
  std::unique_ptr<BybitRestClient> rest_client_;
  std::unique_ptr<BybitPublicStream> public_stream_;
  std::unique_ptr<BybitPrivateStream> private_stream_;
  ExchangeAccountSnapshot account_snapshot_{};
  std::size_t replay_cursor_{0};
  std::size_t replay_symbol_cursor_{0};
  std::int64_t replay_seq_{0};
  std::uint64_t fill_seq_{0};
  std::unordered_map<std::string, std::string> order_symbol_by_client_id_;
  std::unordered_set<std::string> observed_exec_ids_;
  std::deque<MarketEvent> pending_markets_;
  std::unordered_map<std::string, double> last_price_by_symbol_;
  std::unordered_map<std::string, double> remote_position_qty_by_symbol_;
  std::unordered_map<std::string, BybitSymbolTradeRule> symbol_trade_rules_;
  std::deque<FillEvent> pending_fills_;
  std::int64_t execution_watermark_ms_{0};
  bool execution_cursor_primed_{false};
};

}  // namespace ai_trade
