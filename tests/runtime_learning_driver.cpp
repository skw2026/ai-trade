// Deterministic LOCAL test actuator. Not linked into trade_bot; no config input.
// Replay-prefix copies include real initialization/WAL/OMS/fills, not account resets.
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

namespace {
using namespace ai_trade;
void Need(bool ok,const char* why) { if(!ok) throw std::runtime_error(why); }
struct Temp {
  std::filesystem::path path;
  Temp() {
    std::random_device r;
    for(int i=0;i<10;++i) {
      auto p=std::filesystem::temp_directory_path()/("runtime-learning-"+std::to_string(r()));
      if(std::filesystem::create_directory(p)) { path=p; return; }
    }
    throw std::runtime_error("TEMP_DIRECTORY");
  }
  ~Temp() { std::error_code e; std::filesystem::remove_all(path,e); }
};
MarketEvent Market(double price,double mark,Timestamp time,double rate=0) {
  MarketEvent m; m.symbol="BTCUSDT"; m.price=price; m.mark_price=mark;
  m.ts_ms=time; m.interval_ms=300000; m.volume=1000; m.funding_rate_per_interval=rate; return m;
}
AppConfig Config(const Temp& t,int multiplier,bool legacy,bool manual) {
  AppConfig c; c.mode="replay"; c.exchange="mock"; c.data_path=t.path.string();
  c.protection.enabled=false; c.universe.enabled=false;
  c.execution_operator_reduce_only=manual;
  c.risk_max_abs_notional_usd=2500; c.execution_max_order_notional=5000;
  c.execution_entry_fee_bps=c.execution_exit_fee_bps=5.5*multiplier;
  c.execution_expected_slippage_bps=multiplier;
  auto& s=c.self_evolution; s.enabled=legacy; s.use_virtual_pnl=true;
  s.use_counterfactual_search=true; s.counterfactual_require_temporal_holdout=true;
  s.update_interval_ticks=s.min_update_interval_ticks=240;
  s.enable_learnability_gate=true; s.min_bucket_ticks_for_update=120;
  s.counterfactual_min_t_stat_samples_for_update=10;
  s.counterfactual_min_t_stat_abs_for_update=1.5;
  s.counterfactual_superiority_min_samples_for_update=10;
  s.counterfactual_superiority_min_t_stat_for_update=1.5;
  return c;
}
void Receipt(std::ostream& o,const ShadowReceipt& r) {
  o<<"{\"seq\":"<<r.sequence<<",\"event\":"<<int(r.event)<<",\"valid\":"<<r.valid
   <<",\"clock\":"<<r.market_clock<<",\"cash\":"<<r.cash<<",\"equity\":"<<r.equity
   <<",\"liquidation\":"<<r.liquidation<<",\"exit_reserve\":"<<r.exit_reserve
   <<",\"fees\":"<<r.fees<<",\"funding\":"<<r.funding<<",\"realized\":"<<r.realized
   <<",\"funding_paid\":"<<r.funding_paid<<",\"funding_rate\":"<<r.funding_rate
   <<",\"funding_symbol\":"<<std::quoted(r.funding_symbol)<<",\"reward\":";
  if(r.reward) o<<*r.reward; else o<<"null";
  o<<",\"market\":";
  if(r.market) o<<"{\"symbol\":"<<std::quoted(r.market->symbol)<<",\"price\":"<<r.market->price
    <<",\"mark\":"<<r.market->mark_price<<"}"; else o<<"null";
  o<<",\"fill\":";
  if(r.fill) o<<"{\"symbol\":"<<std::quoted(r.fill->symbol)<<",\"side\":"<<r.fill->direction
    <<",\"qty\":"<<r.fill->qty<<",\"price\":"<<r.fill->price<<",\"fee\":"<<r.fill->fee
    <<",\"id\":"<<std::quoted(r.fill->fill_id)<<",\"order\":"<<std::quoted(r.fill->client_order_id)<<"}";
  else o<<"null";
  o<<",\"origin\":";
  if(r.origin) o<<"{\"id\":"<<std::quoted(r.origin->client_order_id)
    <<",\"version\":"<<std::quoted(r.origin->model_version)<<",\"bucket\":"<<r.origin_bucket
    <<",\"weight\":"<<r.origin_weights.trend_weight<<",\"qty\":"<<r.origin->qty
    <<",\"side\":"<<r.origin->direction<<"}"; else o<<"null";
  o<<",\"positions\":["; bool first=true;
  for(const auto& p:r.positions) {
    if(!first) o<<',';
    first=false;
    o<<"{\"symbol\":"<<std::quoted(p.symbol)<<",\"qty\":"<<p.qty<<",\"average\":"<<p.average
     <<",\"mark\":"<<p.mark<<",\"quote\":"<<p.quote<<",\"exit_price\":"<<p.exit_price
     <<",\"exit_fee\":"<<p.exit_fee<<"}";
  }
  o<<"]}";
}
struct Book {
  Temp temp;
  int multiplier; bool legacy,manual;
  std::shared_ptr<RuntimeRewardShadow> shadow;
  BotApplication app;
  std::vector<std::string> history;
  // The observer stores lineage internally and emits it on fills, not on kOrder.
  // Export the actual persisted order and submit-time policy separately at capture.
  std::map<std::size_t,ShadowReceipt> order_receipts;
  std::optional<FillEvent> delayed;
  std::size_t emitted=0,order_number=0;
  Timestamp time=0; double quote=10000,mark=10000,last_target=0,max_dd=0;
  int version=0,bucket=1,steps=0; bool hard_lock=false;
  Book(int m,bool l,bool lock):multiplier(m),legacy(l),manual(lock),shadow(
      std::make_shared<RuntimeRewardShadow>(std::map<std::string,ShadowExitCost>{
        {"BTCUSDT",{5.5*m,double(m),.1}}},30000,300000)),app(Config(temp,m,l,lock),shadow) {
    Need(app.Initialize(),"INITIALIZE"); app.reconcile_halted_=true;
  }
  ~Book() { app.Shutdown(); }
  void ObserveRisk() {
    max_dd=std::max(max_dd,app.system_.account().drawdown_pct());
    hard_lock=hard_lock || max_dd>=.08;
    app.system_.ForceReduceOnly(hard_lock || manual);
  }
  void Price(double p,double m,Timestamp ts,double rate=0,bool full=true) {
    quote=p; mark=m; time=ts;
    if(full) app.ProcessMarketEvent(Market(p,m,ts,rate));
    else app.system_.OnMarketSnapshot(Market(p,m,ts));
    ObserveRisk();
  }
  void Fill(FillEvent f) {
    f.fee=f.qty*f.price*.00055*multiplier;
    app.ProcessFillEvent(f);
    const auto n=shadow->receipts().size(); app.ProcessFillEvent(f);
    Need(shadow->receipts().size()==n,"DUPLICATE_REWARD"); ObserveRisk();
  }
  void Trade(double desired,int mode=1) {
    if(delayed) { Fill(*delayed); delayed.reset(); }
    if(hard_lock || manual) desired=0;
    for(int turn=0;turn<2;++turn) {
      const auto& a=app.system_.account(); const double q=a.position_qty("BTCUSDT");
      double cap=std::copysign(std::min(std::abs(desired),2500*quote/std::max(quote,mark)),desired);
      auto adjusted=app.system_.risk_.Apply(TargetPosition{"BTCUSDT",cap},true,a.drawdown_pct(),1.0);
      double target_qty=std::trunc(adjusted.adjusted_notional_usd/quote*1000+
          std::copysign(1e-9,adjusted.adjusted_notional_usd))/1000;
      adjusted.adjusted_notional_usd=target_qty*quote;
      auto intent=app.execution_.BuildIntent(adjusted,q*quote,quote);
      if(!intent) break;
      intent->qty=std::round(intent->qty*1000)/1000;
      if(intent->qty<=0) break;
      intent->client_order_id="order-"+std::to_string(++order_number);
      intent->model_version="v"+std::to_string(version);
      intent->decision_id="decision-"+std::to_string(steps);
      intent->liquidity_preference=LiquidityPreference::kTaker;
      double ticks=quote*(1+intent->direction*.0001*multiplier)/.1;
      intent->price=(intent->direction>0?std::ceil(ticks-1e-9):std::floor(ticks+1e-9))*.1;
      Need(app.EnqueueIntent(*intent),"ENQUEUE_REJECTED");
      auto submitted=shadow->receipts().back();
      Need(submitted.event==ShadowEvent::kOrder,"ORDER_CAPTURE");
      submitted.origin=app.persisted_intent_by_id_.at(intent->client_order_id);
      submitted.origin_bucket=bucket;
      submitted.origin_weights=app.system_.evolution_weights(static_cast<RegimeBucket>(bucket));
      order_receipts.emplace(submitted.sequence,std::move(submitted));
      FillEvent f; std::vector<FillEvent> fills;
      while(app.adapter_->PollFill(&f)) fills.push_back(f);
      Need(fills.size()==2,"PARTIAL_FILLS_MISSING");
      // No-fill control: exchange refuses before either execution is processed.
      if(mode==0) {
        app.ProcessAsyncResults(); app.oms_.MarkRejected(intent->client_order_id); break;
      }
      Fill(fills[0]); // Fill-before-ACK, actual application duplicate filter exercised.
      app.ProcessAsyncResults();
      if(mode==2) {
        delayed=fills[1]; app.oms_.MarkCancelPending(intent->client_order_id);
        app.executor_->Cancel(intent->client_order_id); app.ProcessAsyncResults(); break;
      }
      Fill(fills[1]);
      if(!(q*desired<0 && std::abs(a.position_qty("BTCUSDT"))<1e-9)) break;
    }
    last_target=desired;
  }
  void Apply(const std::string& command) {
    Need(history.size()<800,"HISTORY_LIMIT");
    std::istringstream in(command); std::string op,extra; in>>op;
    if(op=="STEP") {
      Timestamp ts; double p,c,mo,mc,hi,lo,rate,desired,trend,defensive,w;
      int b,v,mode;
      in>>ts>>p>>c>>mo>>mc>>hi>>lo>>rate>>desired>>b>>v>>w>>trend>>defensive>>mode;
      Need(bool(in) && !(in>>extra),"STEP_FIELDS");
      for(double x:{p,c,mo,mc,hi,lo}) Need(std::isfinite(x)&&x>=1&&x<=1e8,"PRICE");
      Need(ts>time && ts>0 && b>=0&&b<3&&v>=0 && mode>=0&&mode<=2,"METADATA");
      Need(std::isfinite(rate)&&std::abs(rate)<=1 && std::isfinite(desired)&&std::abs(desired)<=2500 &&
        std::isfinite(trend)&&std::abs(trend)<=2500 && std::isfinite(defensive)&&std::abs(defensive)<=2500 &&
        std::isfinite(w)&&w>=.4&&w<=.6,"FINITE_CAP");
      bucket=b;version=v; ++steps;
      app.has_last_regime_state_=true; app.last_regime_state_.bucket=static_cast<RegimeBucket>(b);
      if(!legacy) Need(app.system_.SetEvolutionWeightsForBucket(static_cast<RegimeBucket>(b),w,1-w,nullptr),"WEIGHTS");
      else {
        w=app.system_.evolution_weights(static_cast<RegimeBucket>(b)).trend_weight;
        desired=w*trend+(1-w)*defensive;
      }
      Price(p,mo,ts,rate);
      const bool late=delayed.has_value();
      if(late || hard_lock || manual || std::abs(desired-last_target)>1e-8 ||
          std::abs(app.system_.account().position_qty("BTCUSDT"))*std::max(p,mo)>2500+1e-8) Trade(desired,mode);
      if(legacy) {
        app.has_tick_strategy_signal_=true; app.tick_strategy_signal_symbol_="BTCUSDT";
        app.tick_trend_notional_usd_=trend; app.tick_defensive_notional_usd_=defensive;
        // Current signal is decided at the OPEN. Previous signal earned the
        // open-to-open return; never pair current-open signal with future close.
        app.latest_mark_price_usd_=mo; app.has_latest_mark_price_=true;
        app.RunSelfEvolution(ts,steps);
      }
      Price(p,hi,ts+1,0,false); Price(p,lo,ts+2,0,false); Price(c,mc,ts+3,0,false);
    } else if(op=="SETTLE") {
      Need(!(in>>extra),"SETTLE_FIELDS"); Trade(0);
    } else throw std::runtime_error("OPERATION");
    Need(shadow->valid(),shadow->failure());
    Need(!app.evidence_persistence_failed_&&!app.trading_halted_,"RUNTIME_PERSISTENCE_OR_HALT");
    history.push_back(command);
  }
  void CheckWal() {
    std::unordered_set<std::string> ids,fids,closed; std::vector<FillEvent> fills;
    std::unordered_map<std::string,OrderIntent> intents;
    std::unordered_map<std::string,CandidateEpisodeClosureRecord> closures; std::string error;
    Need(app.wal_.LoadState(&ids,&fids,&fills,&error,&intents,&closed,&closures),"WAL_RELOAD");
    Need(ids==app.intent_ids_ && fids==app.fill_ids_ && fills.size()==fids.size(),"WAL_ID_COVERAGE");
    Need(intents.size()==app.persisted_intent_by_id_.size(),"WAL_INTENTS");
  }
  std::string Output() {
    std::ostringstream o; o<<std::setprecision(17)<<std::boolalpha;
    const auto& a=app.system_.account();
    o<<"{\"ok\":true,\"qty\":"<<a.position_qty("BTCUSDT")<<",\"drawdown\":"<<max_dd
     <<",\"hard_lock\":"<<hard_lock<<",\"manual_lock\":"<<manual<<",\"pending_late\":"<<delayed.has_value()
     <<",\"fills\":"<<app.fill_ids_.size()<<",\"orders\":"<<app.intent_ids_.size()
     <<",\"legacy_weight\":"<<app.system_.evolution_weights(static_cast<RegimeBucket>(bucket)).trend_weight
     <<",\"legacy_virtual\":"<<app.self_evolution_.bucket_window_virtual_pnl_usd_[bucket]
     <<",\"legacy_updates\":"<<app.funnel_window_.self_evolution_updates<<",\"receipts\":[";
    bool first=true;
    for(;emitted<shadow->receipts().size();++emitted) {
      if(!first) o<<',';
      first=false;
      const auto it=order_receipts.find(emitted);
      Receipt(o,it==order_receipts.end()?shadow->receipts()[emitted]:it->second);
    }
    o<<"]}"; return o.str();
  }
};
}
int main(int argc,char** argv) {
  if(argc!=2||std::string(argv[1])!="--synthetic-only") return 2;
  std::map<std::string,std::unique_ptr<Book>> books;
  std::string line;
  try {
    while(std::getline(std::cin,line)) {
      Need(line.size()<2048,"LINE_LIMIT"); std::istringstream in(line);
      std::string op,id,extra; in>>op>>id;
      Need(bool(in)&&id.size()<40,"COMMAND");
      if(op=="NEW") {
        int m,l,lock; in>>m>>l>>lock;
        Need(bool(in)&&!(in>>extra)&&(m==1||m==2)&&(l==0||l==1)&&(lock==0||lock==1)&&
             !books.count(id)&&books.size()<256,"NEW");
        books[id]=std::make_unique<Book>(m,l,lock);
      } else if(op=="COPY") {
        std::string from; in>>from; Need(bool(in)&&!(in>>extra)&&!books.count(id)&&books.size()<256,"COPY");
        const auto& src=*books.at(from);
        auto copy=std::make_unique<Book>(src.multiplier,src.legacy,src.manual);
        for(const auto& command:src.history) copy->Apply(command);
        books[id]=std::move(copy);
      } else if(op=="DO") {
        std::string command; std::getline(in,command); books.at(id)->Apply(command);
      } else if(op=="CHECK") {
        Need(!(in>>extra),"CHECK"); books.at(id)->CheckWal();
      } else throw std::runtime_error("COMMAND_KIND");
      std::cout<<"RUNTIME_REPLY "<<books.at(id)->Output()<<std::endl;
    }
    for(auto& [id,b]:books) { (void)id; b->CheckWal(); }
  } catch(const std::exception& e) {
    std::cerr<<"[ERROR] RUNTIME_LEARNING_DRIVER "<<e.what()<<'\n'; return 1;
  }
}
