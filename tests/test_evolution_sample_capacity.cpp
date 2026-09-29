// Synthetic capacity/selection contract, no historical market data or accounts.
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <string>
#include "evolution/self_evolution_controller.h"
namespace {
using namespace ai_trade;
void Check(bool ok,const char* why) {if(!ok)throw std::runtime_error(why);}
SelfEvolutionAction Window(int minimum=120,bool strict=true,bool reversal=false,
                           bool safety=false,bool falling=false,int windows=1) {
  SelfEvolutionConfig c;
  c.enabled=c.use_virtual_pnl=c.use_counterfactual_search=true;
  c.counterfactual_require_temporal_holdout=strict;
  c.safety_withdrawal_enabled=safety;
  c.enable_learnability_gate=true;
  c.learnability_min_samples=minimum;
  c.update_interval_ticks=240;c.min_update_interval_ticks=0;
  SelfEvolutionController learner(c);
  std::string error;
  Check(learner.Initialize(0,10000,{.5,.5},&error),"initialize");
  std::optional<SelfEvolutionAction> last;
  double price=100;
  for(int i=1;i<=240*windows;++i) {
    const int phase=(i-1)%240+1;
    price*=1+((falling || (reversal && phase>168))?-.002:.001)*(1+.1*std::sin(i));
    auto a=learner.OnTick(i,0,i%4==0?RegimeBucket::kTrend:RegimeBucket::kRange,
                          0,0,80,0,price,"SYNTH",false,0,10000);
    if(a)last=a;
  }
  Check(last.has_value(),"no assessment");
  return *last;
}
}
int main(int argc,char** argv) {
  try {
    if(argc==2 && std::string(argv[1])=="--expect-original") {
      const auto old=Window();
      Check(old.regime_bucket==RegimeBucket::kTrend && old.learnability_samples==60 &&
        old.reason_code=="EVOLUTION_LEARNABILITY_INSUFFICIENT_SAMPLES", "original capacity omission not reproduced");
      std::cout<<"ORIGINAL_OMISSION_REPRODUCED: preferred60 ignores available179\n";
      return 0;
    }
    Check(argc==1,"unexpected argument");
    const auto improved=Window();
    Check(improved.regime_bucket==RegimeBucket::kRange && improved.learnability_samples==179 &&
      improved.type==SelfEvolutionActionType::kUpdated &&
      std::fabs(improved.trend_weight_after-.55)<1e-9,"adequate bucket starved or wrong applied candidate");
    const auto insufficient=Window(240);
    Check(insufficient.type==SelfEvolutionActionType::kSkipped &&
      insufficient.reason_code=="EVOLUTION_LEARNABILITY_INSUFFICIENT_SAMPLES","minimum lowered");
    const auto preferred=Window(50);
    Check(preferred.regime_bucket==RegimeBucket::kTrend,"adequate preferred bucket displaced");
    const auto legacy=Window(120,false);
    Check(legacy.regime_bucket==RegimeBucket::kTrend && legacy.type==SelfEvolutionActionType::kSkipped,
          "legacy selection changed");
    const auto reverse=Window(120,true,true);
    Check(reverse.type==SelfEvolutionActionType::kSkipped && reverse.trend_weight_after==.5,
          "adverse holdout bypassed");
    const auto safety=Window(120,true,false,true,true,2);
    Check(safety.type==SelfEvolutionActionType::kSafetyWithdrawn && !safety.rolled_back_to_baseline,
          "capacity selection bypassed independent withdrawal");
    std::cout<<"SAMPLE_CAPACITY_PASS: 6 synthetic contracts; no economic or confirmation claim\n";
    return 0;
  }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
