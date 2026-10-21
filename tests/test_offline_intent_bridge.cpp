#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iomanip>
#include <limits>
#include <memory>
#include <mutex>
#include <random>
#include <sstream>
#include <stdexcept>
#include <thread>

#include "research/offline_intent_bridge.h"

using ai_trade::research::OfflineIntentBridge;
namespace {
void Check(bool ok, const char* msg) { if (!ok) throw std::runtime_error(msg); }
bool Eq(double a, double b) { return std::fabs(a - b) < 1e-9; }
template<class F> void Reject(F action) {
  bool rejected = false;
  try { action(); } catch (const std::runtime_error&) { rejected = true; }
  Check(rejected, "expected explicit refusal");
}
struct Directory {
  std::filesystem::path path;
  Directory() {
    std::random_device r;
    for (int n = 0; n < 10; ++n) {
      auto p = std::filesystem::temp_directory_path() /
          ("offline-bridge-" + std::to_string(r()) + "-" + std::to_string(r()));
      if (std::filesystem::create_directory(p)) { path = p; return; }
    }
    throw std::runtime_error("temporary directory unavailable");
  }
  ~Directory() { std::error_code e; std::filesystem::remove_all(path, e); }
};

// Synchronizes the existing mock, without changing its two-fill, position or
// cancellation logic. Barriers inject ACK latency without racing mock memory.
class FixtureAdapter : public ai_trade::MockExchangeAdapter {
 public:
  FixtureAdapter() : MockExchangeAdapter({50000}) { Check(Connect(), "connect"); }
  mutable std::mutex state;
  std::mutex barrier;
  std::condition_variable cv;
  bool delay_ack{false}, ack_entered{false}, release_ack{false};
  bool delay_cancel{false}, cancel_entered{false}, release_cancel{false};
  bool reject_submit{false}, reject_cancel{false}, ambiguous_submit{false};
  int cost_multiplier{0};
  std::atomic<bool> timed_out{false};
  std::atomic<int> sends{0}, cancels{0};
  std::vector<ai_trade::OrderIntent> sent;

  bool SubmitOrder(const ai_trade::OrderIntent& order) override {
    bool ok = false;
    {
      std::lock_guard<std::mutex> guard(state);
      ++sends;
      sent.push_back(order);
      ok = !reject_submit && MockExchangeAdapter::SubmitOrder(order);
    }
    if (delay_ack) {
      std::unique_lock<std::mutex> guard(barrier);
      ack_entered = true; cv.notify_all();
      if (!cv.wait_for(guard, std::chrono::seconds(3), [&] { return release_ack; })) timed_out = true;
    }
    return ok && !ambiguous_submit;
  }
  bool CancelOrder(const std::string& id) override {
    ++cancels;
    if (delay_cancel) {
      std::unique_lock<std::mutex> guard(barrier);
      cancel_entered = true; cv.notify_all();
      if (!cv.wait_for(guard, std::chrono::seconds(3), [&] { return release_cancel; })) timed_out = true;
    }
    std::lock_guard<std::mutex> guard(state);
    return !reject_cancel && MockExchangeAdapter::CancelOrder(id);
  }
  bool PollFill(ai_trade::FillEvent* fill) override {
    std::lock_guard<std::mutex> guard(state);
    const bool ok = MockExchangeAdapter::PollFill(fill);
    if (ok) {
      fill->fee = .125;  // Synthetic nonzero cost for dedup/recovery checks.
      if (cost_multiplier) {
        const double adverse=fill->price*(1+fill->direction*cost_multiplier*.0001);
        fill->price=(fill->direction>0 ? std::ceil(adverse*10-1e-9) :
                                        std::floor(adverse*10+1e-9))/10;
        fill->fee=fill->qty*fill->price*cost_multiplier*.00055;
      }
    }
    return ok;
  }
  bool GetRemotePositions(std::vector<ai_trade::RemotePositionSnapshot>* p) const override {
    std::lock_guard<std::mutex> guard(state);
    return MockExchangeAdapter::GetRemotePositions(p);
  }
  bool GetRemoteOpenOrders(std::vector<ai_trade::RemoteOpenOrderSnapshot>* p) const override {
    std::lock_guard<std::mutex> guard(state);
    return MockExchangeAdapter::GetRemoteOpenOrders(p);
  }
  bool Wait(bool cancel = false) {
    std::unique_lock<std::mutex> guard(barrier);
    return cv.wait_for(guard, std::chrono::seconds(3), [&] { return cancel ? cancel_entered : ack_entered; });
  }
  void Release(bool cancel = false) {
    std::lock_guard<std::mutex> guard(barrier);
    if (cancel) release_cancel = true; else release_ack = true;
    cv.notify_all();
  }
};

ai_trade::FillEvent Fill(FixtureAdapter& a, OfflineIntentBridge& b) {
  ai_trade::FillEvent fill;
  Check(a.PollFill(&fill), "missing fill");
  b.OnFill(fill);
  b.OnFill(fill);
  return fill;
}
void Drain(FixtureAdapter& a, OfflineIntentBridge& b) {
  ai_trade::FillEvent fill;
  while (a.PollFill(&fill)) { b.OnFill(fill); b.OnFill(fill); }
  b.PumpResults();
}
void Ready(OfflineIntentBridge& b) { Check(b.Reconcile(), "initial/recovered reconciliation"); }
void Buy(OfflineIntentBridge& b) { b.OnBar(0, 0, -.001, 50000); Check(b.Drive(50000), "buy dispatch"); }

void AckOrdering(int before) {
  Directory dir;
  FixtureAdapter a;
  a.delay_ack = true;
  {
    OfflineIntentBridge b(a, dir.path.string(), false, ai_trade::AsyncExecutor::Mode::kBackground);
    Ready(b); Buy(b);
    Check(a.Wait(), "ACK barrier");
    b.PumpResults();
    for (int i = 0; i < before; ++i) Fill(a, b);
    for (int i = 1; i <= 8; ++i) {
      b.OnBar(i * 300000, 0, -.0002, 50000);
      Check(Eq(b.intent_target(), 2500) && !b.Drive(50000), "pending feedback churn");
    }
    Check(a.sends == 1 && !b.Reconcile(), "in-flight ACK must block reconciliation");
    a.Release();
    // Waiting only for the already-released worker, bounded independently.
    auto end = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (!b.Reconcile() && std::chrono::steady_clock::now() < end)
      std::this_thread::yield();
    Check(b.ready(), "ACK completion");
    const auto expected = before == 0 ? ai_trade::OrderState::kSent :
        before == 1 ? ai_trade::OrderState::kPartial : ai_trade::OrderState::kFilled;
    Check(b.oms().Find("offline-1")->state == expected, "ACK state regression");
    Drain(a, b);
    Check(Eq(b.account().position_qty("BTCUSDT"), .05) && b.unique_fills() == 2 &&
          Eq(b.account().cumulative_fee_usd(), .25) &&
          !b.Drive(50000) && a.sends == 1, "duplicate position/intent");
  }
  Check(!a.timed_out, "ACK barrier expired");
  OfflineIntentBridge recovered(a, dir.path.string());
  Check(!recovered.ready() && !recovered.Drive(50000), "restart before sync");
  Ready(recovered);
  Check(recovered.version() == 9 && Eq(recovered.intent_target(), 2500) &&
        Eq(recovered.account().position_qty("BTCUSDT"), .05) &&
        Eq(recovered.account().cumulative_fee_usd(), .25) &&
        !recovered.Drive(50000) && a.sends == 1, "recovered intent");
}

void PartialRestart() {
  Directory dir; FixtureAdapter a;
  ai_trade::FillEvent first;
  {
    OfflineIntentBridge b(a, dir.path.string()); Ready(b); Buy(b);
    first = Fill(a, b); b.PumpResults();
    Check(Eq(b.oms().PendingNetPositionRemainingQtyForSymbol("BTCUSDT"), .02), "remaining qty");
  }
  OfflineIntentBridge b(a, dir.path.string());
  b.OnFill(first); Ready(b);
  b.OnBar(300000, 0, -.0002, 50000);
  Check(!b.Drive(50000) && a.sends == 1 && b.unique_fills() == 1, "partial restart resend");
  Drain(a, b); Ready(b);
  Check(Eq(b.account().position_qty("BTCUSDT"), .05) && b.unique_fills() == 2, "partial finish");
}

void CancelLateFill() {
  Directory dir; FixtureAdapter a;
  a.delay_cancel = true;
  OfflineIntentBridge b(a, dir.path.string(), false, ai_trade::AsyncExecutor::Mode::kBackground);
  Ready(b); Buy(b);
  auto end = std::chrono::steady_clock::now() + std::chrono::seconds(2);
  while (a.sends == 0 && std::chrono::steady_clock::now() < end) std::this_thread::yield();
  Fill(a, b);
  b.OnBar(300000, 0, .001, 50000); // opposite signal cancels before replacing
  Check(!b.Drive(50000) && a.Wait(true), "cancel barrier");
  Check(!b.Drive(50000) && a.sends == 1 && !b.Reconcile(), "cancel in-flight resend");
  auto late = Fill(a, b); // real mock fill after cancellation requested
  a.Release(true);
  end = std::chrono::steady_clock::now() + std::chrono::seconds(2);
  while (!b.Reconcile() && std::chrono::steady_clock::now() < end) std::this_thread::yield();
  Check(b.ready() && !a.timed_out && a.cancels == 1, "cancel final reconcile");
  Check(b.Drive(50000), "reversal flatten");
  end = std::chrono::steady_clock::now() + std::chrono::seconds(2);
  while (a.sends < 2 && std::chrono::steady_clock::now() < end) std::this_thread::yield();
  // Serialize with SubmitOrder's mock mutation, not the ACK schedule.
  Drain(a, b);
  end = std::chrono::steady_clock::now() + std::chrono::seconds(2);
  while (!b.Reconcile() && std::chrono::steady_clock::now() < end) std::this_thread::yield();
  b.OnFill(late);
  Check(Eq(b.account().position_qty("BTCUSDT"), 0) && b.unique_fills() == 4,
        "late duplicate after flatten");
  Check(b.Drive(50000), "new side only after flat");
  end = std::chrono::steady_clock::now() + std::chrono::seconds(2);
  while (a.sends < 3 && std::chrono::steady_clock::now() < end) std::this_thread::yield();
  Drain(a, b);
  Check(Eq(b.account().position_qty("BTCUSDT"), -.05), "reversal final qty");
}

void MissingFillAndCancelFailure() {
  Directory dir; FixtureAdapter a;
  OfflineIntentBridge b(a, dir.path.string()); Ready(b); Buy(b);
  ai_trade::FillEvent withheld;
  Check(a.PollFill(&withheld), "withhold fill");
  Check(!b.Reconcile() && !b.Drive(50000), "missing fill must block");
  b.OnFill(withheld); Ready(b);
  a.reject_cancel = true;
  b.OnBar(300000, 0, .001, 50000);
  Check(!b.Drive(50000), "cancel send"); b.PumpResults();
  Check(!b.ready() && !b.Drive(50000) && a.cancels == 1 && a.sends == 1,
        "failed cancel blind retry");
  Drain(a, b); Ready(b); Check(b.Drive(50000), "flatten after remaining fill");
  Drain(a, b); Ready(b);
  Check(Eq(b.account().position_qty("BTCUSDT"), 0), "final flat");
}

void AmbiguousSend(bool accepted) {
  Directory dir; FixtureAdapter a;
  a.ambiguous_submit = accepted;
  a.reject_submit = !accepted;
  OfflineIntentBridge b(a, dir.path.string()); Ready(b); Buy(b); b.PumpResults();
  Check(!b.ready() && !b.Drive(50000) && a.sends == 1, "ambiguous duplicate send");
  Ready(b);
  if (accepted) { Check(!b.Drive(50000), "accepted despite negative ACK"); Drain(a, b); }
  else {
    a.reject_submit = false;
    Check(b.Drive(50000), "reconciled not accepted may retry residual"); Drain(a, b);
  }
  Check(Eq(b.account().position_qty("BTCUSDT"), .05), "ambiguous final qty");
}

void Safety(bool operator_lock, bool partial) {
  Directory dir; FixtureAdapter a;
  {
    OfflineIntentBridge b(a, dir.path.string()); Ready(b); Buy(b);
    if (partial) Fill(a, b); else Drain(a, b);
    if (operator_lock) b.LatchOperatorReduceOnly(); else b.LatchRiskStop();
    b.OnBar(300000, 0, .001, 50000);
    if (partial) { Check(!b.Drive(50000), "risk cancels entry"); b.PumpResults(); Ready(b); }
    Check(b.Drive(50000), "risk reduction"); Drain(a, b); Ready(b);
    Check(Eq(b.account().position_qty("BTCUSDT"), 0), "risk flat");
  }
  OfflineIntentBridge b(a, dir.path.string(), false);
  Ready(b); b.OnBar(600000, 0, -.001, 50000);
  Check(!b.Drive(50000) && (operator_lock ? b.operator_locked() : b.stopped()), "risk restored");
}

void BadFill(bool conflict) {
  Directory dir; FixtureAdapter a;
  {
    OfflineIntentBridge b(a, dir.path.string()); Ready(b); Buy(b);
    auto f = Fill(a, b);
    if (conflict) f.qty += .001; else { f.fill_id = "unknown"; f.client_order_id = "foreign"; }
    Reject([&] { b.OnFill(f); });
    Check(b.unique_fills() == 1 && !b.Reconcile() && !b.Drive(50000), "bad fill not quarantined");
  }
  OfflineIntentBridge b(a, dir.path.string());
  Check(!b.Reconcile() && !b.Drive(50000), "quarantine restart");
}

void InputAndOwnership() {
  Directory dir; FixtureAdapter a;
  {
    OfflineIntentBridge b(a, dir.path.string()); Ready(b);
    Reject([&] { OfflineIntentBridge other(a, dir.path.string()); });
    b.OnBar(0, 0, -.001, 50000); b.OnBar(0, 0, -.001, 50000);
    Check(b.version() == 1, "idempotent bar");
    Reject([&] { b.OnBar(0, 0, .001, 50000); });
    Reject([&] { b.OnBar(600000, 0, -.001, 50000); });
    Reject([&] { b.OnBar(300000, 0, .02, 50000); });
    Check(b.version() == 1 && Eq(b.intent_target(), 2500), "invalid input mutated intent");
  }
  { std::ofstream out(dir.path / "intent.events", std::ios::app); out << "B 300000"; }
  Reject([&] { OfflineIntentBridge b(a, dir.path.string()); });
}

void PriceAndRiskPeak() {
  Directory dir; FixtureAdapter a;
  {
    OfflineIntentBridge b(a, dir.path.string()); Ready(b); Buy(b); Drain(a, b);
    b.OnBar(300000, 0, -.0002, 40000);
    Check(!b.Drive(40000) && a.sends == 1, "same target must not buy price dip");
    Check(b.Drive(60000), "notional cap reduction"); Drain(a, b); Ready(b);
    Check(Eq(b.account().position_qty("BTCUSDT"), .041), "cap lattice");
  }
  OfflineIntentBridge b(a, dir.path.string()); Ready(b);
  Check(b.Drive(35000) && b.stopped(), "restored peak must trigger 8 percent stop");
  Drain(a, b); Ready(b);
  Check(Eq(b.account().position_qty("BTCUSDT"), 0), "stop final flat");
}

void StartupOperatorLock() {
  Directory dir; FixtureAdapter a;
  OfflineIntentBridge b(a, dir.path.string(), true); Ready(b);
  b.OnBar(0, 0, -.001, 50000);
  Check(!b.Drive(50000) && a.sends == 0, "operator startup opened");
}

void ExactFillPrecision() {
  Directory dir; FixtureAdapter a;
  ai_trade::FillEvent fill;
  double equity=0;
  {
    OfflineIntentBridge b(a, dir.path.string()); Ready(b); Buy(b);
    Check(a.PollFill(&fill), "precision fixture");
    fill.price = 50000.1234567; fill.fee=1.2345678901234567e-8;
    b.OnFill(fill); b.PumpResults(); Ready(b);
    equity=b.account().equity_usd();
    Check(b.account().avg_entry_price("BTCUSDT")==fill.price &&
          b.account().cumulative_fee_usd()==fill.fee,"precision input lost");
    Check(b.account().mark_price("BTCUSDT")==50000,"fill overwrote mark");
  }
  OfflineIntentBridge b(a, dir.path.string()); Ready(b); b.OnFill(fill);
  Check(b.account().avg_entry_price("BTCUSDT")==fill.price &&
        b.account().cumulative_fee_usd()==fill.fee && b.account().equity_usd()==equity &&
        b.unique_fills()==1,"precision recovery lost");
}

void WalRoundTrip() {
  Directory dir;
  const auto path=dir.path/"mixed.wal";
  // Existing six-digit INTENT2 and FILL3 remain readable, byte-for-byte.
  const std::string legacy="INTENT2\told\t-\tBTCUSDT\t0\t0\t0\t1\t0.123457\t50000.1\t-\t-\t-\t-\t-\t-\n"
      "FILL3\told-fill\told\tBTCUSDT\t1\t0.123457\t50000.1\t0.0123457\t0\n";
  { std::ofstream out(path); out<<legacy; }
  ai_trade::WalStore wal(path.string()); std::string error;
  Check(wal.Initialize(&error),"WAL initialization");
  const double values[]={.12345678901234567,50000.123456789,1.2345678901234567e-12,
                         std::nextafter(1.0,2.0),1234567890123.4567};
  for (std::size_t i=0;i<5;++i) {
    ai_trade::OrderIntent order; order.client_order_id="new-"+std::to_string(i);
    order.direction=1; order.qty=values[i]; order.price=values[4-i];
    ai_trade::FillEvent fill; fill.fill_id="fill-"+std::to_string(i);
    fill.client_order_id=order.client_order_id; fill.qty=order.qty;
    fill.price=order.price; fill.fee=-values[(i+2)%5]; fill.direction=1;
    Check(wal.AppendIntent(order,&error) && wal.AppendFill(fill,&error),"precise WAL append");
  }
  std::unordered_set<std::string> orders,ids;
  std::unordered_map<std::string,ai_trade::OrderIntent> intents;
  std::vector<ai_trade::FillEvent> fills;
  Check(wal.LoadState(&orders,&ids,&fills,&error,&intents),"mixed WAL recovery");
  Check(fills.size()==6 && intents.size()==6 && intents.at("old").qty==.123457 &&
        fills[0].fee==.0123457,"legacy WAL compatibility");
  for (std::size_t i=0;i<5;++i) {
    const auto& order=intents.at("new-"+std::to_string(i)); const auto& fill=fills[i+1];
    Check(order.qty==values[i] && order.price==values[4-i] && fill.qty==order.qty &&
          fill.price==order.price && fill.fee==-values[(i+2)%5],"WAL numeric bits changed");
  }
  std::ifstream in(path); std::string bytes((std::istreambuf_iterator<char>(in)),{});
  Check(bytes.substr(0,legacy.size())==legacy,"legacy records rewritten");
}

void CapNoReplenish() {
  Directory dir; FixtureAdapter a;
  {
    OfflineIntentBridge b(a,dir.path.string()); Ready(b); Buy(b); Drain(a,b);
    Check(b.Drive(50000,60000),"mark-price cap missing"); Drain(a,b); Ready(b);
    Check(Eq(b.goal_qty(),.041) && !b.Drive(50000) && a.sends==2,"cap refilled");
  }
  OfflineIntentBridge b(a,dir.path.string()); Ready(b);
  b.OnBar(300000,0,-.0002,50000);
  Check(!b.Drive(50000) && Eq(b.goal_qty(),.041) && a.sends==2,"restart cap refilled");
}

void FundingChronology() {
  Directory dir; FixtureAdapter a;
  double equity=0,paid=0;
  {
    OfflineIntentBridge b(a,dir.path.string()); Ready(b);
    b.Funding(0,.001,50000); Buy(b); Fill(a,b);
    b.Funding(300000,.001234567890123,51000); // only the first partial fill owns funding
    Drain(a,b); b.Funding(600000,-.0001234567890123,49000);
    paid=b.account().cumulative_funding_paid_usd(); equity=b.account().equity_usd();
    Check(Eq(paid,.03*51000*.001234567890123-.05*49000*.0001234567890123),"funding holder");
  }
  OfflineIntentBridge b(a,dir.path.string()); Ready(b);
  b.Funding(300000,.001234567890123,51000);
  Check(b.account().equity_usd()==equity && b.account().cumulative_funding_paid_usd()==paid,
        "funding reordered or repeated after restart");
  Reject([&]{b.Funding(300000,.002,51000);});
  Check(!b.Reconcile(),"conflicting funding not quarantined");
}

void JournalGap(bool missing_receipt) {
  Directory dir; FixtureAdapter a;
  {
    OfflineIntentBridge b(a,dir.path.string()); Ready(b); Buy(b);
    ai_trade::FillEvent f; Check(a.PollFill(&f),"gap fill");
    if (missing_receipt) {
      ai_trade::WalStore wal((dir.path/"orders.wal").string()); std::string error;
      Check(wal.AppendFill(f,&error),"inject WAL-before-event crash");
    } else {
      std::ofstream out(dir.path/"intent.events",std::ios::app); out<<"E missing-fill\n";
    }
  }
  Reject([&]{OfflineIntentBridge b(a,dir.path.string());});
}

void LegacyJournalRefused() {
  Directory dir; FixtureAdapter a;
  { std::ofstream out(dir.path/"intent.events"); out<<"B 0 0 -0.001 50000\n"; }
  Reject([&]{OfflineIntentBridge b(a,dir.path.string());});
}

void IntrabarStop() {
  Directory dir; FixtureAdapter a;
  {
    OfflineIntentBridge b(a,dir.path.string()); Ready(b); Buy(b); Drain(a,b);
    b.ObserveRange(70000,50000);
    Check(b.stopped() && a.sends==1 && b.account().position_qty("BTCUSDT")>.049,
          "intrabar observation executed before next open");
  }
  OfflineIntentBridge b(a,dir.path.string()); Ready(b);
  b.Funding(300000,.001,52000);
  Check(b.Drive(52000),"next open risk exit"); Drain(a,b); Ready(b);
  Check(Eq(b.account().position_qty("BTCUSDT"),.002) && a.sent.back().reduce_only,
        "first risk exit must respect per-order 2500 cap");
  Check(b.Drive(52000),"risk residual reduction"); Drain(a,b); Ready(b);
  Check(a.sent.back().reduce_only && a.sends==3,"risk residual opened or retried");
  Check(Eq(b.account().position_qty("BTCUSDT"),0) && !b.Drive(52000),"risk terminal reopened");
}

void ForeignRemoteOrder() {
  Directory dir; FixtureAdapter a;
  OfflineIntentBridge b(a, dir.path.string()); Ready(b);
  ai_trade::OrderIntent order;
  order.client_order_id = "foreign"; order.qty = .05; order.price = 50000; order.direction = 1;
  Check(a.SubmitOrder(order), "foreign fixture");
  b.OnBar(0, 0, -.001, 50000);
  Check(!b.Reconcile() && !b.Drive(50000) && a.sends == 1, "unknown order admitted");
}

void PolicyStream() {
  Directory dir; FixtureAdapter a;
  OfflineIntentBridge b(a, dir.path.string());
  std::int64_t ts;
  double xs, xl, p;
  while (std::cin >> ts >> xs >> xl >> p) {
    b.OnBar(ts, xs, xl, p);
    std::cout << b.intent_target() << '\n';
  }
  Check(std::cin.eof(), "policy fixture input");
}

void EventStream(int cost) {
  Check(cost==1 || cost==2,"cost multiplier");
  Directory dir; FixtureAdapter a; a.cost_multiplier=cost;
  auto b=std::make_unique<OfflineIntentBridge>(a,dir.path.string()); Ready(*b);
  std::string line;
  while (std::getline(std::cin,line)) {
    std::istringstream in(line); std::string kind,extra; in>>kind;
    if (kind=="B") {
      std::int64_t ts=0; double x=0,p=0,m=0;
      Check(static_cast<bool>(in>>ts>>x>>p>>m),"bar fixture");
      b->OnBar(ts,0,x,p);
      int count=0;
      while (b->Drive(p,m)) { Check(++count<=3,"event order loop"); Drain(a,*b); Ready(*b); }
    } else if (kind=="U") {
      std::int64_t ts=0; double r=0,m=0;
      Check(static_cast<bool>(in>>ts>>r>>m),"funding fixture"); b->Funding(ts,r,m);
    } else if (kind=="H") {
      double h=0,l=0; Check(static_cast<bool>(in>>h>>l),"range fixture"); b->ObserveRange(h,l);
    } else if (kind=="M") {
      double m=0; Check(static_cast<bool>(in>>m),"mark fixture"); b->Mark(m);
    } else if (kind=="R") {
      b.reset(); b=std::make_unique<OfflineIntentBridge>(a,dir.path.string()); Ready(*b);
    } else if (kind=="L") {
      b->LatchRiskStop();  // Paired-book safety withdrawal from the offline driver.
    } else if (kind=="X") {
      double p=0,m=0; Check(static_cast<bool>(in>>p>>m),"terminal fixture");
      // Terminal harness retires this bridge; the permanent reduction latch is
      // deliberately shared, not evidence that an 8% risk event actually happened.
      b->LatchRiskStop(); int count=0;
      while (b->Drive(p,m)) { Check(++count<=3,"terminal order loop"); Drain(a,*b); Ready(*b); }
      Check(Eq(b->account().position_qty("BTCUSDT"),0),"terminal residual position");
    } else throw std::runtime_error("unknown event fixture");
    Check(!(in>>extra),"extra event field");
    const auto& ac=b->account(); const double q=ac.position_qty("BTCUSDT");
    std::cout<<std::setprecision(17)<<q<<' '<<ac.cash_usd()-q*ac.avg_entry_price("BTCUSDT")
      <<' '<<ac.equity_usd()<<' '<<ac.cumulative_fee_usd()<<' '<<ac.cumulative_funding_paid_usd()
      <<' '<<b->risk_peak()<<' '<<b->risk_drawdown()<<' '<<b->stopped()<<'\n';
  }
}
}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc == 2 && std::string(argv[1]) == "--policy-stream") { PolicyStream(); return 0; }
    if (argc == 3 && std::string(argv[1]) == "--event-stream") { EventStream(std::stoi(argv[2])); return 0; }
    int count = 0;
    auto run = [&](const char* name, auto fn) { fn(); ++count; std::cout << "PASS " << name << '\n'; };
    for (int i = 0; i < 3; ++i) run("ack-order", [=] { AckOrdering(i); });
    run("partial-restart", PartialRestart);
    run("cancel-late-fill-reversal", CancelLateFill);
    run("missing-fill-cancel-failure", MissingFillAndCancelFailure);
    run("ambiguous-send-accepted", [] { AmbiguousSend(true); });
    run("ambiguous-send-not-accepted", [] { AmbiguousSend(false); });
    for (bool op : {false, true}) for (bool partial : {false, true})
      run("risk-operator-restart", [=] { Safety(op, partial); });
    run("conflicting-fill", [] { BadFill(true); });
    run("unknown-fill", [] { BadFill(false); });
    run("input-ownership-corruption", InputAndOwnership);
    run("price-cap-restart-risk-peak", PriceAndRiskPeak);
    run("startup-operator", StartupOperatorLock);
    run("exact-fill-precision", ExactFillPrecision);
    run("foreign-remote-order", ForeignRemoteOrder);
    run("mixed-precision-wal", WalRoundTrip);
    run("cap-no-replenish", CapNoReplenish);
    run("funding-chronology", FundingChronology);
    run("wal-without-event", []{JournalGap(true);});
    run("event-without-wal", []{JournalGap(false);});
    run("legacy-journal-refused", LegacyJournalRefused);
    run("intrabar-next-open-stop", IntrabarStop);
    std::cout << "OFFLINE_ORDER_LIFECYCLE_SUPPORTED cases=" << count << " economics=NOT_EVALUATED\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n'; return 1;
  }
}
