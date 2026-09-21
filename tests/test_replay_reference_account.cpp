#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <random>
#include <sstream>
#include <stdexcept>
#include "app/bot_app.h"
#include "exchange/bybit_exchange_adapter.h"
#include "risk/replay_reference_account.h"
#include "system/trade_system.h"

namespace {
using namespace ai_trade;
constexpr Timestamp kStart = 1704067200000LL, kBar = 300000;
void Check(bool ok, const char* why) { if (!ok) throw std::runtime_error(why); }
bool Near(double a, double b) { return std::fabs(a-b) < 1e-8; }
template<class F> void Reject(F f, const std::string& reason) {
  try { f(); } catch (const std::exception& e) {
    Check(std::string(e.what()).find(reason) != std::string::npos, e.what()); return;
  }
  throw std::runtime_error("expected rejection: " + reason);
}
MarketEvent Open(int i, double mark = 100) {
  MarketEvent e;
  e.ts_ms = kStart + i*kBar;
  e.price = e.mark_price = mark;
  e.execution_only = true;
  e.funding_rate_per_interval = 0;
  return e;
}
MarketEvent Close(int i, double mark = 100, double high = 101, double low = 99) {
  auto e = Open(i+1, mark);
  e.execution_only = false; e.completed_bar = true; e.interval_ms = kBar;
  e.open_price = 100; e.high_price = std::max(100.0, mark)+1;
  e.low_price = std::min(100.0, mark)-1;
  e.mark_high_price = high; e.mark_low_price = low;
  return e;
}
FillEvent Fill(std::string id, int side, double qty, double price=100, double bps=5.5) {
  FillEvent f;
  f.fill_id = f.client_order_id = id; f.direction=side; f.qty=qty; f.price=price;
  f.fee = qty*price*bps/10000; return f;
}
struct Ledger {
  AccountState account;
  ReplayReferenceAccount model;
  Ledger() { Market(Open(0)); }
  void Market(const MarketEvent& e) { account.OnMarket(e); model.OnMarket(e, account); }
  void Apply(const FillEvent& f) {
    model.OnFill(f, account); account.ApplyFill(f, true); model.AfterAccounting(account);
  }
  void Funding(double rate) {
    const double paid=account.position_qty("BTCUSDT")*account.mark_price("BTCUSDT")*rate;
    model.OnFunding(rate, paid, account);
    Check(Near(account.ApplyFunding("BTCUSDT",rate,true),paid),"funding double ledger mismatch");
    model.AfterAccounting(account);
  }
};
void Accounting(int side) {
  Ledger x;
  x.Apply(Fill("first",side,10));
  Check(Near(x.model.collateral(),499.45),"entry reserve/fee wrong");
  Check(!x.account.minimum_liquidation_distance(),"reference forged exchange liquidation field");
  Check(x.model.RiskDistance(x.account).value_or(0)>0.4,"valid reference risk unavailable");
  x.Apply(Fill("add",side,5));
  Check(Near(x.model.collateral(),749.175),"same-side collateral wrong");
  x.Market(Close(0)); x.Market(Open(1)); x.Funding(0.001);
  Check(Near(x.model.collateral(),749.175-side*1.5),"funding not debited isolated");
  Check(Near(x.account.cash_usd(),10000-0.825-side*1.5),"funding/fee counted twice");
  const double half=x.model.collateral()/2;
  x.Apply(Fill("half",-side,7.5));
  Check(Near(x.model.collateral(),half),"partial release wrong");
  x.Market(Close(1,100,102,98));
  Check(Near(x.model.funding_uncertainty(),0.06),"old holder funding range lost after partial close");
  x.Market(Open(2)); x.Apply(Fill("all",-side,7.5));
  Check(x.model.collateral()==0 && x.account.position_qty("BTCUSDT")==0,"terminal not flat");
  Check(Near(x.account.cash_usd(),10000-1.65-side*1.5),"final fee ledger wrong");
}
void NegativeCases() {
  { Ledger x; x.Apply(Fill("entry",1,10));
    Check(Near(x.model.max_drawdown_upper(),0.55/10000),"entry fee missing from point DD");
    x.Apply(Fill("exit",-1,10));
    Check(Near(x.model.max_drawdown_upper(),1.1/10000),"terminal fee missing from DD"); }
  { Ledger x; x.Apply(Fill("first",1,10));x.Apply(Fill("second",1,5,110));
    Check(Near(x.account.avg_entry_price("BTCUSDT"),1550.0/15),"weighted entry wrong");
    const double b=775-1550*0.00055;
    const double boundary=(1550-b)/(15*(1-0.0113));
    Check(Near(x.model.collateral(),b),"weighted collateral wrong");
    Check(Near(x.model.RiskDistance(x.account).value(),(100-boundary)/100),"boundary equation wrong"); }
  { Ledger x; x.Apply(Fill("one",1,1));
    Reject([&]{x.Apply(Fill("one",1,1));},"FILL_IDENTITY"); }
  { Ledger x; x.Apply(Fill("one",1,1));
    Reject([&]{x.Funding(0.001);},"FUNDING_ORDER"); }
  { Ledger x; x.Apply(Fill("one",1,1));
    Reject([&]{x.Apply(Fill("flip",-1,2));},"CROSS_ZERO_FILL");
    Check(x.account.position_qty("BTCUSDT")==1,"rejected fill changed ledger"); }
  { Ledger x; Reject([&]{x.Apply(Fill("gap",1,31));},"GAP_GROSS_LIMIT"); }
  { Ledger x; auto f=Fill("nan",1,1); f.price=std::nan("");
    Reject([&]{x.Apply(f);},"FILL_IDENTITY"); }
  { Ledger x; auto e=Open(1);e.mark_price=0;
    Reject([&]{x.Market(e);},"MARK_OR_LEDGER"); }
  { Ledger x; x.Market(Open(1));
    Reject([&]{x.Market(Open(0));},"MARK_OR_LEDGER"); }
  { Ledger x; x.Apply(Fill("position",1,10));x.Market(Close(0));x.Market(Open(1));
    // Plenty of free cash remains, but isolated collateral is not topped up.
    Reject([&]{x.Funding(0.5);},"REJECT_REFERENCE_MAINTENANCE"); }
  { Ledger x; x.Apply(Fill("long",1,10));
    Reject([&]{x.Market(Open(1,40));},"REJECT_REFERENCE_MAINTENANCE");
    Reject([&]{x.Market(Open(2,100));},"REJECT_REFERENCE_MAINTENANCE"); }
  { Ledger x; x.Apply(Fill("short",-1,10));
    Reject([&]{x.Market(Open(1,160));},"REJECT_REFERENCE_MAINTENANCE"); }
  { Ledger x; x.Apply(Fill("long",1,10));
    Reject([&]{x.Market(Close(0,100,101,50));},"INTRABAR_CONTROL_PATH"); }
  { Ledger x; x.Apply(Fill("short",-1,10));
    Reject([&]{x.Market(Close(0,100,160,99));},"INTRABAR_CONTROL_PATH"); }
  { Ledger x; x.Apply(Fill("long",1,10));
    Reject([&]{x.Market(Close(0,100,1100,99));},"REFERENCE_TIER"); }
  { Ledger x; x.Apply(Fill("one",1,1)); x.Market(Close(0));x.Market(Open(1));
    x.Funding(1e-10);
    Check(x.account.cumulative_funding_paid_usd()>0,"tiny funding silently zeroed");
    Reject([&]{x.Funding(1e-10);},"FUNDING_ORDER"); }
  Check(Near(ReplayReferenceRules::QuantizeQuantity(0.00199),0.001),"qty rounds up");
  Check(Near(ReplayReferenceRules::AdversePrice(100.01,1),100.1),"buy tick not adverse");
  Check(Near(ReplayReferenceRules::AdversePrice(99.99,-1),99.9),"sell tick not adverse");
}
struct Directory {
  std::filesystem::path path;
  Directory() {
    std::random_device r;
    for(int i=0;i<10;++i) {
      path=std::filesystem::temp_directory_path()/ ("mvp-reference-test-"+std::to_string(r()));
      if(std::filesystem::create_directory(path))return;
    }
    throw std::runtime_error("temporary directory unavailable");
  }
  ~Directory(){std::error_code e;std::filesystem::remove_all(path,e);}
};
void AdapterRules(bool stress) {
  Directory dir; const auto path=dir.path/"rules.csv";
  { std::ofstream out(path);
    out<<"timestamp,symbol,open,high,low,price,volume,interval_ms,funding_rate_per_interval,mark_open,mark_close,mark_high,mark_low\n";
    for(int i=0;i<3;++i)
      out<<kStart+i*kBar<<",BTCUSDT,100.01,100.5,100,100.01,1,300000,0,100.01,100.01,100.5,100\n";
  }
  BybitAdapterOptions o;o.replay_causal_bars=true;o.replay_reference_account=true;
  o.replay_market_data_path=path.string();o.public_ws_enabled=o.private_ws_enabled=false;
  o.replay_entry_fee_bps=o.replay_exit_fee_bps=stress?11:5.5;
  o.replay_expected_slippage_bps=stress?2:1;
  o.http_transport_factory=[]()->std::unique_ptr<BybitHttpTransport>{
    throw std::runtime_error("network forbidden in reference rules test");
  };
  BybitExchangeAdapter a(o);Check(a.Connect(),"rules fixture failed");
  MarketEvent e;FillEvent f;
  Check(a.PollMarket(&e) && std::isnan(e.mark_high_price) && std::isnan(e.mark_low_price),"future mark bounds at open");
  Check(a.PollMarket(&e) && Near(e.mark_high_price,100.5),"mark bounds absent at close");
  OrderIntent order;order.client_order_id="entry";order.direction=1;order.qty=0.05;order.price=100.01;
  auto bad=order;bad.qty=0.049;Check(!a.SubmitOrder(bad),"min entry ignored");
  bad.qty=0.0501;Check(!a.SubmitOrder(bad),"quantity step ignored");
  bad.qty=10;Check(!a.SubmitOrder(bad),"max order ignored");
  bad.qty=0.0009;Check(!a.SubmitOrder(bad),"min qty ignored");
  bad=order;bad.price=0;Check(!a.SubmitOrder(bad),"zero price accepted");
  Check(a.SubmitOrder(order) && !a.PollFill(&f),"entry not deferred");
  Check(a.PollMarket(&e) && e.execution_only,"next open absent");
  double total=0;int parts=0;
  while(a.PollFill(&f)) {
    ++parts;total+=f.qty;
    Check(Near(f.qty,ReplayReferenceRules::QuantizeQuantity(f.qty)),"partial off step");
    Check(Near(f.price,100.1),"buy slippage/tick wrong");
    Check(Near(f.fee,f.qty*f.price*(stress?0.0011:0.00055)),"entry fee wrong");
  }
  Check(parts==2 && Near(total,0.05),"partial sizes wrong");
  Check(a.PollMarket(&e) && e.completed_bar,"close absent");
  order.client_order_id="tiny-reduce";order.direction=-1;order.qty=0.001;
  order.reduce_only=true;order.purpose=OrderPurpose::kReduce;
  Check(a.SubmitOrder(order),"valid one-quantum reduction refused");
  Check(a.PollMarket(&e),"final open absent");parts=0;
  while(a.PollFill(&f)) {
    ++parts;Check(Near(f.qty,0.001) && Near(f.price,99.9),"one quantum split or sell tick wrong");
    Check(Near(f.fee,f.qty*f.price*(stress?0.0011:0.00055)),"exit fee wrong");
  }
  Check(parts==1,"one quantum must have one fill");
  Check(a.PollMarket(&e) && !a.PollMarket(&e),"EOF expected");
  order.client_order_id="terminal-dust";order.replay_terminal_settlement=true;order.qty=0.0009;
  Check(!a.SubmitOrder(order),"terminal dust silently written off");
  order.client_order_id="terminal";order.qty=0.049;
  Check(a.SubmitOrder(order),"terminal reduction rejected");total=0;
  while(a.PollFill(&f))total+=f.qty;
  Check(Near(total,0.049),"terminal reduction over/under filled");
}
void Integrated(const AppConfig& config) {
  TradeSystem system(config);
  system.OnMarketSnapshot(Open(0));
  system.OnFill(Fill("entry",1,1));
  const auto d=system.Evaluate(Open(0));
  Check(!d.risk_adjusted.reduce_only,"reference remains stuck in unknown risk");
  Check(!system.account().minimum_liquidation_distance(),"reference wrote fake exchange risk");
  std::string error;
  Check(!system.ApproveMvpRiskCycle({},&error),"screen allowed manual recovery");
  Reject([&]{system.RefreshAccountRiskFromRemotePositions({});},"exchange snapshots");
  Reject([&]{system.SyncAccountFromRemotePositions({});},"remote rebase");
  auto legacy=config;legacy.replay_reference_account=false;
  TradeSystem old(legacy);old.OnMarketSnapshot(Open(0));old.OnFill(Fill("legacy",1,1));
  Check(old.Evaluate(Open(0)).risk_adjusted.reduce_only,"legacy unknown-risk behavior changed");

  Directory dir; const auto csv=dir.path/"synthetic.csv";
  {std::ofstream out(csv);
    out<<"timestamp,symbol,open,high,low,price,volume,interval_ms,funding_rate_per_interval,mark_open,mark_close,mark_high,mark_low\n";
    for(int i=0;i<400;++i) {
      // Deterministic synthetic trend, sufficient warmup; never historical prices.
      const double p=500+i;
      out<<kStart+i*kBar<<",BTCUSDT,"<<p<<','<<p+1<<','<<p<<','<<p+1
         <<",1,300000,"<<(i==300?0.0001:0)<<','<<p<<','<<p+1<<','<<p+1<<','<<p<<'\n';
    }
  }
  auto c=config;c.data_path=(dir.path/"app").string();c.bybit.replay_market_data_path=csv.string();
  BotApplication app(c);
  std::ostringstream logs;auto* previous=std::cout.rdbuf(logs.rdbuf());int result;
  try {result=app.Run();}catch(...){std::cout.rdbuf(previous);throw;}
  std::cout.rdbuf(previous);
  Check(result==0,logs.str().c_str());
  Check(logs.str().find("REFERENCE_FILL_JSON")!=std::string::npos,"end-to-end fixture made no fills");
  Check(logs.str().find("REFERENCE_TERMINAL_JSON {\"flat\":true")!=std::string::npos,"missing terminal proof");
  Check(logs.str().find("REFERENCE_STOP_JSON")==std::string::npos,"reference aborted silently");
}
}
int main(int argc,char**argv) {
  try {
    Check(argc==2,"configuration argument required");
    AppConfig c;std::string error;
    Check(LoadAppConfigFromYaml(argv[1],&c,&error),error.c_str());
    Check(c.replay_reference_account,"reference flag missing");
    Accounting(1);Accounting(-1);NegativeCases();AdapterRules(false);AdapterRules(true);Integrated(c);
    auto invalid=c;invalid.mode="live";Check(!ValidateClosedBarMvpConfig(invalid,&error),"live allowed");
    invalid=c;invalid.closed_bar_mvp=false;Check(!ValidateClosedBarMvpConfig(invalid,&error),"legacy allowed");
    std::cout<<"REFERENCE_ACCOUNT_SYNTHETIC_PASS\n";return 0;
  } catch(const std::exception&e){std::cerr<<e.what()<<'\n';return 1;}
}
