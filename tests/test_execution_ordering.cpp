#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <memory>
#include <mutex>
#include <random>
#include <stdexcept>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#if defined(__clang__)
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wkeyword-macro"
#endif
#define private public
#include "app/bot_app.h"
#undef private
#if defined(__clang__)
#pragma clang diagnostic pop
#endif
#include "exchange/bybit_exchange_adapter.h"

namespace {
void Require(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}

bool Equal(double a, double b) { return std::fabs(a - b) < 1e-9; }

struct TempDirectory {
  std::filesystem::path path;
  TempDirectory() {
    std::random_device random;
    for (int attempt = 0; attempt < 10; ++attempt) {
      const auto candidate = std::filesystem::temp_directory_path() /
          ("ai-trade-ordering-" + std::to_string(random()) + "-" +
           std::to_string(random()));
      if (std::filesystem::create_directory(candidate)) {
        path = candidate;
        return;
      }
    }
    throw std::runtime_error("cannot create unique test directory");
  }
  ~TempDirectory() {
    std::error_code error;
    std::filesystem::remove_all(path, error);
  }
};

ai_trade::BybitAdapterOptions ReplayOptions() {
  ai_trade::BybitAdapterOptions options;
  options.mode = "replay";
  options.allow_no_auth_in_replay = true;
  options.symbols = {"BTCUSDT"};
  options.replay_prices = {100.0};
  options.replay_entry_fee_bps = 10.0;
  return options;
}

// Make the original race reproducible without timing sleeps: the real replay
// adapter has queued fills and released its state lock, but SubmitOrder has
// not yet returned to the executor to publish its result.
class DelayedAckAdapter : public ai_trade::BybitExchangeAdapter {
 public:
  DelayedAckAdapter() : BybitExchangeAdapter(ReplayOptions()) {}
  bool SubmitOrder(const ai_trade::OrderIntent& intent) override {
    const bool ok = BybitExchangeAdapter::SubmitOrder(intent);
    std::unique_lock<std::mutex> lock(mutex_);
    published_ = true;
    ready_.notify_all();
    timed_out_ = !ready_.wait_for(lock, std::chrono::seconds(3),
                                [this] { return released_; });
    return ok;
  }
  bool WaitForFillPublication() {
    std::unique_lock<std::mutex> lock(mutex_);
    return ready_.wait_for(lock, std::chrono::seconds(3),
                          [this] { return published_; });
  }
  void ReleaseAck() {
    std::lock_guard<std::mutex> lock(mutex_);
    released_ = true;
    ready_.notify_all();
  }
  bool TimedOut() const { return timed_out_; }  // Read only after worker join.

 private:
  std::mutex mutex_;
  std::condition_variable ready_;
  bool published_{false};
  bool released_{false};
  bool timed_out_{false};
};

ai_trade::OrderIntent Entry() {
  ai_trade::OrderIntent intent;
  intent.client_order_id = "ordering-entry";
  intent.symbol = "BTCUSDT";
  intent.purpose = ai_trade::OrderPurpose::kEntry;
  intent.direction = 1;
  intent.qty = 1;
  intent.price = 100;
  intent.liquidity_preference = ai_trade::LiquidityPreference::kTaker;
  return intent;
}

// ACK before any fill, after partial fill, and after full fill. All cases use
// the production-default background executor with an offline Bybit adapter.
void CheckAckOrdering(int fills_before_ack) {
  TempDirectory directory;
  ai_trade::AppConfig config;
  config.data_path = directory.path.string();
  config.mode = "paper";
  config.protection.enabled = false;
  ai_trade::BotApplication app(config);
  std::string error;
  Require(app.wal_.Initialize(&error), "WAL init failed");
  auto adapter = std::make_unique<DelayedAckAdapter>();
  auto* delayed = adapter.get();
  Require(adapter->Connect(), "offline adapter connect failed");
  app.adapter_ = std::move(adapter);
  app.executor_ = std::make_unique<ai_trade::AsyncExecutor>(app.adapter_.get());
  app.executor_->Start();
  const auto intent = Entry();
  Require(app.EnqueueIntent(intent), "entry enqueue failed");
  Require(delayed->WaitForFillPublication(), "fill publication timeout");
  app.ProcessAsyncResults();
  Require(app.oms_.Find(intent.client_order_id)->state == ai_trade::OrderState::kNew,
          "blocked ACK must not be consumed or synthesized");

  std::vector<ai_trade::FillEvent> observed;
  auto consume = [&] {
    ai_trade::FillEvent fill;
    Require(app.adapter_->PollFill(&fill), "expected partial fill");
    app.ProcessFillEvent(fill);
    app.ProcessFillEvent(fill);  // Duplicate must not enter WAL/account twice.
    observed.push_back(fill);
  };
  for (int i = 0; i < fills_before_ack; ++i) consume();
  delayed->ReleaseAck();
  app.executor_->Stop();
  Require(!delayed->TimedOut(), "test barrier expired");
  app.ProcessAsyncResults();
  const auto expected = fills_before_ack == 0 ? ai_trade::OrderState::kSent
      : fills_before_ack == 1 ? ai_trade::OrderState::kPartial
                             : ai_trade::OrderState::kFilled;
  Require(app.oms_.Find(intent.client_order_id)->state == expected,
          "late ACK regressed partial/filled order");
  for (int i = fills_before_ack; i < 2; ++i) consume();
  ai_trade::FillEvent extra;
  Require(!app.adapter_->PollFill(&extra), "unexpected extra fill");
  Require(app.oms_.Find(intent.client_order_id)->state == ai_trade::OrderState::kFilled &&
              Equal(app.oms_.net_filled_qty(intent.symbol), 1) &&
              Equal(app.system_.account().position_qty(intent.symbol), 1) &&
              Equal(app.system_.account().cumulative_fee_usd(), 0.1) &&
              app.fill_ids_.size() == 2, "position or duplicate handling diverged");

  // Test the same persisted intent/fill recovery primitives as Initialize,
  // without connecting a real account or asserting full startup acceptance.
  TempDirectory recovered_directory;
  config.data_path = recovered_directory.path.string();
  ai_trade::BotApplication recovered(config);
  Require(recovered.wal_.Initialize(&error), "recovered WAL init failed");
  std::vector<ai_trade::FillEvent> fills;
  Require(app.wal_.LoadState(&recovered.intent_ids_, &recovered.fill_ids_,
                            &fills, &error, &recovered.persisted_intent_by_id_),
          "WAL reload failed");
  Require(fills.size() == 2 && recovered.fill_ids_.size() == 2 &&
              recovered.persisted_intent_by_id_.size() == 1,
          "WAL persisted duplicate/missing events");
  for (const auto& [id, persisted] : recovered.persisted_intent_by_id_) {
    (void)id;
    Require(recovered.oms_.RegisterIntent(persisted), "recover intent failed");
  }
  for (const auto& fill : fills) {
    recovered.oms_.OnFill(fill);
    recovered.system_.OnFill(fill);
  }
  for (const auto& fill : observed) recovered.ProcessFillEvent(fill);
  recovered.oms_.MarkSent(intent.client_order_id);
  Require(recovered.oms_.Find(intent.client_order_id)->state == ai_trade::OrderState::kFilled &&
              Equal(recovered.oms_.net_filled_qty(intent.symbol), 1) &&
              Equal(recovered.system_.account().position_qty(intent.symbol), 1) &&
              Equal(recovered.system_.account().cumulative_fee_usd(),
                    app.system_.account().cumulative_fee_usd()),
          "recovered position/fees or late ACK diverged");
}

void CheckInlineExecution() {
  ai_trade::BybitExchangeAdapter adapter(ReplayOptions());
  Require(adapter.Connect(), "inline adapter connect failed");
  ai_trade::AsyncExecutor executor(&adapter,
      ai_trade::AsyncExecutor::Mode::kInlineReplay);
  executor.Start();
  auto entry = Entry();
  executor.Submit(entry);
  std::vector<ai_trade::AsyncResult> results;
  executor.PollResults(&results);
  Require(results.size() == 1 && results[0].success && !results[0].is_cancel,
          "inline ACK not available immediately after submit");
  executor.Cancel(entry.client_order_id);
  executor.PollResults(&results);
  Require(results.size() == 1 && results[0].success && results[0].is_cancel,
          "inline cancel result missing");
  ai_trade::FillEvent fill;
  Require(!adapter.PollFill(&fill), "inline cancel left pending fills");
  entry.qty = 0;
  executor.Submit(entry);
  executor.PollResults(&results);
  Require(results.size() == 1 && !results[0].success &&
              results[0].error == "SubmitOrder returned false",
          "inline rejection must not be converted to success");
  executor.Stop();
  executor.Stop();
  executor.Start();
  entry.qty = 1;
  executor.Submit(entry);
  executor.PollResults(&results);
  Require(results.size() == 1 && results[0].success, "inline restart failed");
}
}  // namespace

int main() {
  try {
    CheckInlineExecution();
    for (int fills = 0; fills <= 2; ++fills) CheckAckOrdering(fills);
    std::cout << "execution ordering: inline + 3 ACK/fill/WAL cases PASS\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
