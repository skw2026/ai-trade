// Offline historical scheduler. Linked only with the mock-only bridge, no live adapter.
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include "research/offline_intent_bridge.h"

using ai_trade::research::OfflineIntentBridge;
namespace {
constexpr std::int64_t kStep=300000;
constexpr const char* kSymbol="BTCUSDT";
void Need(bool ok,const char* why) { if (!ok) throw std::runtime_error(why); }
struct Bar { std::int64_t ts; double open,close,mo,mh,ml,mc,rate; };
std::vector<std::string> Split(const std::string& line) {
  const std::string normalized=(!line.empty() && line.back()=='\r') ? line.substr(0,line.size()-1) : line;
  std::istringstream in(normalized); std::vector<std::string> fields; std::string field;
  while (std::getline(in,field,',')) fields.push_back(field);
  return fields;
}
double Number(const std::string& text) {
  std::size_t used=0; double n=std::stod(text,&used);
  Need(used==text.size() && std::isfinite(n),"CSV_NUMBER"); return n;
}
double FillPrice(double price,int side,int cost) {
  const double adverse=price*(1+side*cost*.0001);
  return (side>0 ? std::ceil(adverse*10-1e-9) : std::floor(adverse*10+1e-9))/10;
}
class CostMock : public ai_trade::MockExchangeAdapter {
 public:
  explicit CostMock(int cost) : MockExchangeAdapter({}),cost_(cost) { Need(Connect(),"CONNECT"); }
  bool SubmitOrder(const ai_trade::OrderIntent& order) override {
    ++orders; return MockExchangeAdapter::SubmitOrder(order);
  }
  bool PollFill(ai_trade::FillEvent* f) override {
    if (!MockExchangeAdapter::PollFill(f)) return false;
    double original=f->price; f->price=FillPrice(original,f->direction,cost_);
    f->fee=f->qty*f->price*cost_*.00055;
    slip+=f->qty*f->direction*(f->price-original); turnover+=f->qty*f->price; ++fills;
    return true;
  }
  int cost_; std::uint64_t orders{0},fills{0}; double slip{0},turnover{0};
};
class Book {
 public:
  Book(const std::filesystem::path& path,int delay,int cost)
      : mock(cost),path_(path),delay_(delay),trace(path.string()+".csv") {
    Need(trace.good(),"TRACE_OPEN"); Open();
    trace<<"event,ts,index,intent,due,qty,cash,equity,fees,funding,peak,dd,slippage,turnover,fills,orders,liquidation,stopped\n";
  }
  void Open() {
    bridge=std::make_unique<OfflineIntentBridge>(mock,path_.string(),false,
        ai_trade::AsyncExecutor::Mode::kInlineReplay,delay_);
    Need(bridge->Reconcile(),"RECONCILE");
  }
  void Execute(double price,double mark) {
    for (int count=0;count<8;++count) {
      if (!bridge->Drive(price,mark)) {
        Need(!bridge->oms().HasPendingNetPositionOrders() && bridge->ready(),"EXECUTION_INCOMPLETE");
        return;
      }
      ai_trade::FillEvent f;
      while (mock.PollFill(&f)) { bridge->OnFill(f); bridge->OnFill(f); }
      bridge->PumpResults(); Need(bridge->Reconcile(),"POST_FILL_RECONCILE");
      // A newly observed per-fill risk crossing stops further entry/flip legs.
      // Already-latched exits may continue bounded reduce-only fragmentation.
      if (!exiting && bridge->stopped()) return;
    }
    throw std::runtime_error("PER_OPEN_ORDER_BUDGET");
  }
  std::array<double,9> State(double price,double mark) const {
    const auto& a=bridge->account(); const double q=a.position_qty(kSymbol);
    const double cash=a.cash_usd()-q*a.avg_entry_price(kSymbol);
    double liquid=cash;
    if (std::fabs(q)>1e-10) {
      double fill=FillPrice(price,q>0?-1:1,mock.cost_);
      liquid+=q*fill-std::fabs(q)*fill*mock.cost_*.00055;
    }
    return {q,cash,cash+q*mark,a.cumulative_fee_usd(),a.cumulative_funding_paid_usd(),
            bridge->risk_peak(),bridge->risk_drawdown(),liquid,static_cast<double>(bridge->stopped())};
  }
  void Emit(const char* event,const Bar& bar,int index,double price,double mark) {
    auto s=State(price,mark);
    trace<<std::setprecision(17)<<event<<','<<bar.ts<<','<<index<<','<<bridge->intent_target()<<','
      <<bridge->execution_target();
    for (int i=0;i<7;++i) trace<<','<<s[i];
    trace<<','<<mock.slip<<','<<mock.turnover<<','<<mock.fills<<','<<mock.orders<<','<<s[7]<<','<<s[8]<<'\n';
    Need(trace.good(),"TRACE_WRITE");
  }
  void Restart(const Bar& bar,int index) {
    auto before=State(bar.close,bar.mc);
    double intent=bridge->intent_target(),due=bridge->execution_target(),goal=bridge->goal_qty();
    auto version=bridge->version(); bridge.reset(); Open();
    Need(State(bar.close,bar.mc)==before && bridge->intent_target()==intent &&
         bridge->execution_target()==due && bridge->goal_qty()==goal && bridge->version()==version,
         "RESTART_STATE_CHANGED");
    Emit("RECOVER",bar,index,bar.close,bar.mc);
  }
  CostMock mock; std::unique_ptr<OfflineIntentBridge> bridge; bool exiting{false};
  std::filesystem::path path_; int delay_; std::ofstream trace;
};
class Pair {
 public:
  Pair(const std::filesystem::path& root,const std::string& name,int delay)
      : books{std::make_unique<Book>(root/(name+"_base"),delay,1),
              std::make_unique<Book>(root/(name+"_stress"),delay,2)} {}
  bool Risk() const { return books[0]->bridge->stopped() || books[1]->bridge->stopped(); }
  void Latch() { for (auto& b:books) b->bridge->LatchRiskStop(); pending=true; }
  void Consume(const Bar& bar,int index,double xs,double xl) {
    if (flat) {
      for (auto& b:books) b->Emit("FLAT",bar,index,bar.close,bar.mc);
      return;
    }
    if (pending) {
      for (auto& b:books) {
        b->bridge->Funding(bar.ts,bar.rate,bar.mo);
        b->Emit("EXIT_FUND",bar,index,bar.open,bar.mo);
        b->exiting=true; b->Execute(bar.open,bar.mo);
        Need(std::fabs(b->bridge->account().position_qty(kSymbol))<1e-12,"EXIT_RESIDUAL");
        b->Emit("EXIT",bar,index,bar.open,bar.mo);
      }
      flat=true; pending=false; return;
    }
    for (auto& b:books) {
      b->bridge->OnBar(bar.ts,xs,xl,bar.open);
      b->bridge->Funding(bar.ts,bar.rate,bar.mo);
      b->Emit("OPEN",bar,index,bar.open,bar.mo);
    }
    bool opening=Risk();
    if (opening) Latch();
    for (auto& b:books) {
      if (!opening) b->Execute(bar.open,bar.mo);
      b->Emit("EXEC",bar,index,bar.open,bar.mo);
    }
    if (Risk()) Latch();
    for (auto& b:books) {
      if (!opening) b->bridge->ObserveRange(bar.mh,bar.ml);
      b->Emit("RANGE",bar,index,bar.open,bar.mo);
    }
    if (Risk()) Latch();
    for (auto& b:books) {
      if (!opening) b->bridge->Mark(bar.mc);
      b->Emit("CLOSE",bar,index,bar.close,bar.mc);
      if (index%8640==0) b->Restart(bar,index);
    }
  }
  void Finish(const Bar& bar,int index) {
    for (auto& b:books) {
      b->bridge->LatchRiskStop(); b->exiting=true;
      if (!flat) b->Execute(bar.close,bar.mc);
      Need(std::fabs(b->bridge->account().position_qty(kSymbol))<1e-12,"TERMINAL_RESIDUAL");
      b->Emit("TERMINAL",bar,index,bar.close,bar.mc);
    }
  }
  std::array<std::unique_ptr<Book>,2> books; bool pending{false},flat{false};
};
}  // namespace

int main(int argc,char** argv) {
  try {
    Need(argc==5,"USAGE input.csv output-directory start-ms end-ms");
    const auto start=std::stoll(argv[3]),end=std::stoll(argv[4]);
    Need(start%kStep==0 && end>start && (end-start)%kStep==0,"DOMAIN");
    const std::filesystem::path root(argv[2]);
    Need(!std::filesystem::exists(root) && std::filesystem::create_directories(root),"OUTPUT_EXISTS");
    std::ifstream input(argv[1]); std::string line;
    Need(static_cast<bool>(std::getline(input,line)),"CSV_HEADER");
    const auto header=Split(line);
    const std::array<std::string,8> names={"timestamp","open","price","mark_open","mark_high","mark_low","mark_close","funding_rate_per_interval"};
    std::array<std::size_t,8> positions{};
    for (std::size_t i=0;i<names.size();++i) {
      auto found=std::find(header.begin(),header.end(),names[i]); Need(found!=header.end(),"CSV_COLUMN");
      positions[i]=static_cast<std::size_t>(found-header.begin());
    }
    Pair normal(root,"normal",0),delay(root,"delay",1);
    std::deque<Bar> history; std::int64_t expected=start-288*kStep; int index=0; Bar last{};
    while (std::getline(input,line)) {
      auto fields=Split(line); Need(fields.size()==header.size(),"CSV_FIELDS");
      std::array<double,8> n{};
      for (std::size_t i=0;i<n.size();++i) n[i]=Number(fields[positions[i]]);
      Need(n[0]==std::floor(n[0]),"NONINTEGER_TIME");
      Bar bar{static_cast<std::int64_t>(n[0]),n[1],n[2],n[3],n[4],n[5],n[6],n[7]};
      Need(bar.ts==expected && bar.ts<end,"BAR_GRID"); expected+=kStep;
      Need(bar.open>0 && bar.close>0 && bar.ml>0 && bar.mh>=std::max(bar.mo,bar.mc) &&
           bar.ml<=std::min(bar.mo,bar.mc),"PRICE_RANGE");
      if (bar.ts>=start) {
        Need(history.size()==288,"WARMUP"); ++index;
        const auto feature=[&](std::size_t h) {return std::clamp((history.back().close/history[288-h].open-1)/h,-.01,.01);};
        const double xs=feature(12),xl=feature(288);
        normal.Consume(bar,index,xs,xl); delay.Consume(bar,index,xs,xl); last=bar;
        if (index%10000==0) std::cerr<<"PROGRESS bars="<<index<<'\n';
      }
      history.push_back(bar); if (history.size()>288) history.pop_front();
    }
    Need(input.eof() && expected==end && index==(end-start)/kStep,"INCOMPLETE_DOMAIN");
    normal.Finish(last,index); delay.Finish(last,index);
    std::cout<<"NATIVE_FOUR_BOOKS_COMPLETE bars="<<index<<" training=0 qualification=false\n";
    return 0;
  } catch (const std::exception& e) { std::cerr<<"NATIVE_LOOP_ERROR "<<e.what()<<'\n';return 1; }
}
