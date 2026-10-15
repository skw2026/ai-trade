// Fixed local events only. No config loading, market files or live adapters.
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <random>
#include <sstream>
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
#include "exchange/mock_exchange_adapter.h"

namespace {
using namespace ai_trade;
int cases = 0;
void Need(bool ok, const char* reason) { if (!ok) throw std::runtime_error(reason); }
bool Near(double a, double b) { return std::abs(a-b) < 5e-8; }
struct Temp {
  std::filesystem::path path;
  Temp() {
    std::random_device r;
    for (int n=0;n<10;++n) {
      auto p=std::filesystem::temp_directory_path()/ ("runtime-shadow-"+std::to_string(r()));
      if (std::filesystem::create_directory(p)) { path=p; return; }
    }
    throw std::runtime_error("unique temporary directory");
  }
  ~Temp() { std::error_code e; std::filesystem::remove_all(path,e); }
};
std::shared_ptr<RuntimeRewardShadow> Shadow(std::size_t capacity=10000, Timestamp age=60000) {
  return std::make_shared<RuntimeRewardShadow>(
      std::map<std::string,ShadowExitCost>{{"BTCUSDT",{}},{"ETHUSDT",{}}},capacity,age);
}
MarketEvent Market(double price, Timestamp time, double funding=0, std::string symbol="BTCUSDT") {
  MarketEvent e; e.symbol=std::move(symbol); e.price=e.mark_price=price;
  e.ts_ms=time; e.interval_ms=5000; e.volume=1000; e.funding_rate_per_interval=funding;
  return e;
}
OrderIntent Order(std::string id, int side=1, double qty=.5, double price=100,
                  std::string symbol="BTCUSDT") {
  OrderIntent o; o.client_order_id=std::move(id); o.symbol=std::move(symbol);
  o.direction=side; o.qty=qty; o.price=price; o.model_version="frozen-v1";
  o.decision_id="decision-1"; o.candidate_id="synthetic-candidate";
  o.position_episode_id="synthetic-episode"; o.integrator_mode="shadow";
  o.integrator_policy_reason="synthetic-fixed-policy";
  o.liquidity_preference=LiquidityPreference::kTaker;
  return o;
}
FillEvent Fill(const OrderIntent& o, std::string id, double qty) {
  FillEvent f; f.symbol=o.symbol; f.fill_id=std::move(id); f.client_order_id=o.client_order_id;
  f.direction=o.direction; f.qty=qty; f.price=o.price; f.fee=qty*o.price*.00055; return f;
}
AppConfig Config(const Temp& temp) {
  AppConfig c; c.mode="replay"; c.exchange="mock"; c.data_path=temp.path.string();
  c.protection.enabled=false; c.universe.enabled=false; c.self_evolution.enabled=true;
  c.self_evolution.initial_trend_weight=.6; c.self_evolution.initial_defensive_weight=.4;
  c.self_evolution.update_interval_ticks=10; c.self_evolution.min_update_interval_ticks=10;
  c.self_evolution.use_virtual_pnl=true;
  return c;
}

// Serialize only fixed synthetic fixtures. No production logging/output sink.
void Dump(const std::string& name, const RuntimeRewardShadow& shadow) {
  for (const auto& r:shadow.receipts()) {
    std::cout<<std::setprecision(17)<<std::boolalpha<<"SHADOW_JSON {\"stream\":"<<std::quoted(name)
      <<",\"seq\":"<<r.sequence<<",\"event\":"<<static_cast<int>(r.event)
      <<",\"valid\":"<<r.valid<<",\"cash\":"<<r.cash<<",\"equity\":"<<r.equity
      <<",\"liquidation\":"<<r.liquidation<<",\"exit_reserve\":"<<r.exit_reserve
      <<",\"fees\":"<<r.fees<<",\"funding\":"<<r.funding<<",\"realized\":"<<r.realized
      <<",\"funding_paid\":"<<r.funding_paid<<",\"funding_rate\":"<<r.funding_rate
      <<",\"funding_symbol\":"<<std::quoted(r.funding_symbol)<<",\"reward\":";
    if(r.reward) std::cout<<*r.reward; else std::cout<<"null";
    std::cout<<",\"market\":";
    if(r.market) std::cout<<"{\"symbol\":"<<std::quoted(r.market->symbol)
       <<",\"price\":"<<r.market->price<<",\"mark\":"<<r.market->mark_price<<"}";
    else std::cout<<"null";
    std::cout<<",\"fill\":";
    if(r.fill) std::cout<<"{\"symbol\":"<<std::quoted(r.fill->symbol)
       <<",\"side\":"<<r.fill->direction<<",\"qty\":"<<r.fill->qty
       <<",\"price\":"<<r.fill->price<<",\"fee\":"<<r.fill->fee<<"}";
    else std::cout<<"null";
    std::cout<<",\"positions\":[";
    bool first=true;
    for(const auto& p:r.positions) {
      if(!first) std::cout<<',';
      first=false;
      std::cout<<"{\"symbol\":"<<std::quoted(p.symbol)<<",\"qty\":"<<p.qty
        <<",\"average\":"<<p.average<<",\"mark\":"<<p.mark<<",\"quote\":"<<p.quote
        <<",\"exit_price\":"<<p.exit_price<<",\"exit_fee\":"<<p.exit_fee<<"}";
    }
    std::cout<<"]}"<<'\n';
  }
}

std::string Signature(const BotApplication& app) {
  std::ostringstream s; s<<std::setprecision(17);
  const auto& a=app.system_.account();
  s<<a.cash_usd()<<','<<a.equity_usd()<<','<<a.cumulative_fee_usd()<<','
   <<a.cumulative_funding_paid_usd()<<','<<a.drawdown_pct()<<','
   <<a.position_qty("BTCUSDT")<<','<<static_cast<int>(app.system_.risk_mode())<<','
   <<app.evolution_safety_withdrawn_<<','<<app.evidence_persistence_failed_
   <<','<<app.trading_halted_<<','<<app.intent_ids_.size()
   <<','<<app.fill_ids_.size()<<','<<app.self_evolution_.clock_tick();
  for(int i=0;i<3;++i) {
    const auto b=static_cast<RegimeBucket>(i);
    s<<','<<app.system_.evolution_weights(b).trend_weight
     <<','<<app.self_evolution_.current_weights(b).trend_weight
     <<','<<app.self_evolution_.bucket_window_virtual_pnl_usd_[i];
  }
  // Normalize generated IDs only; preserve every economic/order-state field.
  std::vector<std::string> orders;
  for(const auto& [id,o]:app.persisted_intent_by_id_) {
    std::ostringstream row; row<<std::setprecision(17)<<o.symbol<<','<<o.direction<<','<<o.qty
      <<','<<o.price<<','<<o.reduce_only<<','<<static_cast<int>(o.purpose);
    if(const auto* record=app.oms_.Find(id)) row<<','<<static_cast<int>(record->state)<<','<<record->filled_qty;
    orders.push_back(row.str());
  }
  std::sort(orders.begin(),orders.end());
  for(const auto& o:orders) s<<'|'<<o;
  return s.str();
}

void FullRuntime() {
  Temp t1,t2,t3; auto shadow=Shadow();
  BotApplication plain(Config(t1)), observed(Config(t2),shadow);
  Need(plain.Run()==0 && observed.Run()==0,"full local application failed");
  Need(Signature(plain)==Signature(observed),"full runtime observer parity");
  Need(shadow->valid() && shadow->receipts().size()>15,"full runtime shadow missing");
  Need(observed.market_tick_count_==6,"full event loop coverage");
  Dump("full-runtime",*shadow); ++cases;
  auto exhausted=Shadow(1); BotApplication invalid(Config(t3),exhausted);
  Need(invalid.Run()==0 && !exhausted->valid() &&
       std::string(exhausted->failure())=="CAPACITY","observer failure not exercised");
  Need(Signature(plain)==Signature(invalid),"observer failure changed trading behavior");
  ++cases;
}

std::vector<std::string> Sequence(BotApplication& app, RuntimeRewardShadow* shadow) {
  Need(app.Initialize(),"sequence initialize");
  app.reconcile_halted_=true; // Keep market bookkeeping; fixed orders below only.
  std::vector<std::string> states;
  auto state=[&]{states.push_back(Signature(app));};
  app.ProcessMarketEvent(Market(100,5000)); state();
  app.has_tick_strategy_signal_=true; app.tick_strategy_signal_symbol_="BTCUSDT";
  app.tick_trend_notional_usd_=100; app.tick_defensive_notional_usd_=0;
  app.RunSelfEvolution(5000,1); state();
  const auto order=Order("fixed-entry");
  Need(app.EnqueueIntent(order),"fixed entry"); state();
  FillEvent first,late;
  Need(app.adapter_->PollFill(&first) && app.adapter_->PollFill(&late),"two partial fills");
  first.fee=first.qty*first.price*.00055; late.fee=late.qty*late.price*.00055;
  app.ProcessFillEvent(first); state(); // Actual fill BEFORE ACK.
  const auto count=shadow?shadow->receipts().size():0;
  app.ProcessFillEvent(first); state();
  if(shadow) Need(shadow->receipts().size()==count,"duplicate emitted reward");
  app.ProcessAsyncResults(); state();
  app.oms_.MarkCancelPending(order.client_order_id);
  app.executor_->Cancel(order.client_order_id); app.ProcessAsyncResults(); state();
  app.ProcessMarketEvent(Market(110,10000,.001)); state();
  app.RunSelfEvolution(10000,2); state();
  Need(app.system_.SetEvolutionWeights(.4,.6,nullptr),"test weight change");
  app.ProcessFillEvent(late); state();
  if(shadow) {
    const auto& r=shadow->receipts().back();
    Need(r.event==ShadowEvent::kFill && r.origin && r.origin->model_version=="frozen-v1" &&
      Near(r.origin_weights.trend_weight,.6),"late fill reattributed to current weight");
    Need(Near(app.system_.account().cumulative_funding_paid_usd(),.033),"funding before late fill");
  }
  app.ProcessMarketEvent(Market(110,15000)); state();
  auto close=Order("fixed-close",-1,.5,110); close.reduce_only=true; close.purpose=OrderPurpose::kReduce;
  Need(app.EnqueueIntent(close),"close enqueue"); app.ProcessAsyncResults(); state();
  FillEvent f;
  while(app.adapter_->PollFill(&f)) { f.fee=f.qty*f.price*.00055; app.ProcessFillEvent(f); state(); }
  Need(Near(app.system_.account().position_qty("BTCUSDT"),0),"not flat");
  Need(!app.evidence_persistence_failed_ && app.persisted_closed_episode_ids_.count("synthetic-episode"),
       "episode closure persistence failed");
  std::unordered_set<std::string> order_ids,fill_ids,closed_ids;
  std::vector<FillEvent> fills;
  std::unordered_map<std::string,OrderIntent> intents;
  std::unordered_map<std::string,CandidateEpisodeClosureRecord> closures;
  std::string error;
  Need(app.wal_.LoadState(&order_ids,&fill_ids,&fills,&error,&intents,&closed_ids,&closures),
       "WAL cannot reload");
  Need(order_ids.size()==2 && fill_ids.size()==4 && fills.size()==4 &&
       closed_ids.count("synthetic-episode") && closures.size()==1 &&
       closures.at("synthetic-episode").evidence_complete &&
       closures.at("synthetic-episode").fill_event_count==4 &&
       closures.at("synthetic-episode").unique_order_count==2,
       "persisted episode/order/fill coverage incomplete");
  if(shadow) {
    Need(shadow->valid(),shadow->failure());
    Need(Near(shadow->receipts().back().exit_reserve,0),"exit reserve double count");
  }
  app.Shutdown(); return states;
}
void AsyncSequence() {
  Temp t1,t2; auto s=Shadow(); BotApplication plain(Config(t1)),observed(Config(t2),s);
  Need(Sequence(plain,nullptr)==Sequence(observed,s.get()),"event-by-event trading parity");
  Dump("async-sequence",*s); ++cases;
}

void Portfolio() {
  TradeSystem sys(AppConfig{}); auto s=Shadow(); sys.AttachRewardShadow(s);
  sys.OnMarketSnapshot(Market(100,1)); sys.OnMarketSnapshot(Market(200,1,0,"ETHUSDT"));
  auto a=Order("long"), b=Order("short",-1,.2,200,"ETHUSDT");
  sys.ObserveRewardOrder(a,RegimeBucket::kRange); sys.OnFill(Fill(a,"a",.5));
  sys.ObserveRewardOrder(b,RegimeBucket::kTrend); sys.OnFill(Fill(b,"b",.2));
  sys.OnMarketSnapshot(Market(110,2)); sys.OnMarketSnapshot(Market(190,2,0,"ETHUSDT"));
  sys.ApplyFunding("BTCUSDT",.001); sys.ApplyFunding("ETHUSDT",.001);
  const auto& r=s->receipts().back();
  Need(s->valid() && r.positions.size()==2 && r.reward && *r.reward>0,"short funding receipt");
  Dump("multi-symbol",*s); ++cases;
}

void Invalidations() {
  auto setup=[](TradeSystem& sys, std::shared_ptr<RuntimeRewardShadow> s) {
    sys.AttachRewardShadow(s); sys.OnMarketSnapshot(Market(100,10));
    const auto o=Order("open"); sys.ObserveRewardOrder(o,RegimeBucket::kRange); sys.OnFill(Fill(o,"f",.5));
  };
  for(int scenario=0;scenario<9;++scenario) {
    TradeSystem sys(AppConfig{}); auto s=Shadow(100,10); setup(sys,s);
    const auto before=sys.account().cash_usd();
    if(scenario==0) sys.OnMarketSnapshot(Market(200,21,0,"ETHUSDT"));
    if(scenario==1) sys.OnMarketSnapshot(Market(100,9));
    if(scenario==2) sys.SyncAccountFromRemotePositions({},20000);
    if(scenario==3) sys.ForceSyncAccountPositionsFromRemote({});
    if(scenario==4) sys.RefreshAccountRiskFromRemotePositions({});
    if(scenario==5) { RemoteAccountBalanceSnapshot b; sys.SyncAccountFromRemoteBalance(b,false); }
    if(scenario==6) sys.OnReflectedFill(Fill(Order("open"),"reflected",.1),.5,100);
    if(scenario==7) sys.OnFill(Fill(Order("unknown"),"f2",.1));
    if(scenario==8) sys.OnMarketSnapshot(Market(0,11));
    Need(!s->valid() && !s->receipts().back().reward,"bad stream not invalidated");
    const auto size=s->receipts().size();
    sys.OnMarketSnapshot(Market(100,30));
    Need(s->receipts().size()==size && !s->valid(),"invalidity silently resumed");
    if(scenario==0||scenario==1||scenario==4||scenario==8)
      Need(Near(before,sys.account().cash_usd()),"observer altered cash");
    ++cases;
  }
  {
    TradeSystem sys(AppConfig{}); auto s=Shadow(1); sys.AttachRewardShadow(s);
    sys.OnMarketSnapshot(Market(100,1));
    Need(!s->valid() && std::string(s->failure())=="CAPACITY","capacity not bounded");
    ++cases;
  }
  {
    TradeSystem sys(AppConfig{}); auto s=Shadow(); sys.AttachRewardShadow(s);
    const auto o=Order("no-quote"); sys.ObserveRewardOrder(o,RegimeBucket::kRange);
    sys.OnFill(Fill(o,"no-quote-fill",.5));
    Need(!s->valid(),"unquoted position credited"); ++cases;
  }
  {
    TradeSystem sys(AppConfig{}); auto s=Shadow(); sys.AttachRewardShadow(s);
    sys.AttachRewardShadow(s); Need(!s->valid(),"reused baseline accepted"); ++cases;
  }
  {
    bool caught=false;
    try { RuntimeRewardShadow s({{"BTCUSDT",{5.5,1,0}}}); }
    catch(const std::invalid_argument&) { caught=true; }
    Need(caught,"invalid cost accepted"); ++cases;
  }
  {
    Temp t; auto c=Config(t); c.mode="paper"; bool caught=false;
    try { BotApplication app(c,Shadow()); } catch(const std::invalid_argument&) { caught=true; }
    Need(caught,"non-replay injection accepted"); ++cases;
  }
  for(int scenario=0;scenario<3;++scenario) {
    TradeSystem sys(AppConfig{}); auto s=Shadow(); setup(sys,s);
    auto f=Fill(Order("open"),"bad-input",.1);
    if(scenario==0) f.direction=-1;
    if(scenario==1) { sys.OnMarketSnapshot(Market(100,10,0,"ETHUSDT")); f.symbol="ETHUSDT"; }
    if(scenario==2) f.qty=0;
    sys.OnFill(f);
    Need(!s->valid() && !s->receipts().back().reward,"invalid fill was rewarded");
    Need(std::string(s->failure())==(scenario==2?"INVALID_FILL":"FILL_ORIGIN_MISMATCH"),
         "wrong invalid-fill reason"); ++cases;
  }
}

void OperatorLock() {
  Temp t1,t2; auto c1=Config(t1), c2=Config(t2);
  c1.execution_operator_reduce_only=c2.execution_operator_reduce_only=true;
  auto s=Shadow(); BotApplication a(c1),b(c2,s);
  Need(a.Initialize()&&b.Initialize(),"operator init");
  Need(!a.EnqueueIntent(Order("blocked"))&&!b.EnqueueIntent(Order("blocked")),"operator lock bypass");
  Need(Signature(a)==Signature(b) && s->valid(),"operator parity");
  a.Shutdown(); b.Shutdown(); ++cases;
}
void LegacyComparison() {
  Temp t; auto s=Shadow(); BotApplication app(Config(t),s);
  Need(app.Initialize(),"legacy comparison init"); app.reconcile_halted_=true;
  for(int tick=1;tick<=10;++tick) {
    app.ProcessMarketEvent(Market(99+tick,5000*tick));
    app.has_tick_strategy_signal_=true; app.tick_strategy_signal_symbol_="BTCUSDT";
    app.tick_trend_notional_usd_=100; app.tick_defensive_notional_usd_=0;
    app.RunSelfEvolution(5000*tick,tick);
    Need(s->valid() && s->receipts().back().reward &&
         Near(*s->receipts().back().reward,0),"no fill became execution profit");
    if(tick==2) Need(Near(app.self_evolution_.bucket_window_virtual_pnl_usd_[1],.6),
                    "legacy proxy comparison absent");
  }
  Need(s->receipts().back().legacy_action &&
       s->receipts().back().legacy_action->used_virtual_pnl &&
       s->receipts().back().legacy_action->window_virtual_pnl_usd>0,
       "legacy assessment not copied to shadow");
  Dump("legacy-no-fill",*s); app.Shutdown(); ++cases;
}
}  // namespace
int main() {
  try {
    FullRuntime(); AsyncSequence(); Portfolio(); Invalidations(); OperatorLock(); LegacyComparison();
    std::cout<<"RUNTIME_SHADOW_CASES "<<cases<<'\n'; return 0;
  } catch(const std::exception& e) { std::cerr<<"RUNTIME_SHADOW_FAIL "<<e.what()<<'\n'; return 1; }
}
