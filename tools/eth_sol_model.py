"""Sequential two-leg reference execution, never a trading adapter.

OHLC adverse scenarios and aggregate-volume bounds do NOT certify actual fills.
"""
from collections import deque
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

from execution_net_learning import Safety, WINDOW


def require(ok,reason):
    if not ok: raise ValueError(reason)


def floor_quantity(value,step):
    return float((Decimal(str(max(0,value)))/Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)*Decimal(str(step)))


def execution_price(bar,delta,stress=False):
    require(len(bar)==5 and all(math.isfinite(v) for v in bar) and min(bar[:4])>0 and bar[4]>=0,'EXECUTION_BAR')
    require(bar[1]>=max(bar[0],bar[3]) and bar[2]<=min(bar[0],bar[3]),'EXECUTION_OHLC')
    buy=delta>0
    raw=bar[1 if buy else 2] if stress else bar[0]
    slip=Decimal('0.0004' if stress else '0.0002')
    tick=Decimal('0.01')
    price=Decimal(str(raw))*(1+slip if buy else 1-slip)
    return float((price/tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR)*tick)


def estimate(hourly):
    """Only caller-supplied completed hours. Fixed 60-day window, no fitting grid."""
    require(len(hourly)==1440,'ESTIMATION_WINDOW')
    x=[math.log(p[1]) for p in hourly];y=[math.log(p[0]) for p in hourly]
    xm,ym=math.fsum(x)/len(x),math.fsum(y)/len(y)
    den=math.fsum((v-xm)**2 for v in x)
    if den<=1e-18:return None
    beta=math.fsum((a-xm)*(b-ym) for a,b in zip(x,y))/den
    alpha=ym-beta*xm
    sigma=math.sqrt(math.fsum((b-alpha-beta*a)**2 for a,b in zip(x,y))/len(x))
    if not (0.25<=beta<=4 and sigma>=0.000001):return None
    return alpha,beta,sigma


def score(model,prices):
    require(model is not None and min(prices)>0,'SCORE_INPUT')
    a,b,s=model
    return (math.log(prices[0])-a-b*math.log(prices[1]))/s


class PairLedger:
    steps=(0.01,0.1)

    def __init__(self,stress=False,entry=2.,hours=24):
        self.stress=stress;self.threshold=entry;self.horizon=hours*60
        self.cash=10000.;self.q=[0.,0.];self.leg_cash=[0.,0.];self.collateral=[0.,0.]
        self.fees=0.;self.funding=0.;self.traded=0.;self.peak=10000.;self.dd=0.
        self.events=[];self.queue=deque();self.exiting=False;self.exit_deadline=None
        self.model=None;self.sign=0;self.entry_minute=None;self.complete=False
        self.episodes=0;self.failed_entries=0;self.stopped=None;self.insufficient=None
        self.safety=Safety();self.last_liquidation=10000.;self.safety_checks=[]
        self.last_flat_minute=None;self.observations=0

    @property
    def flat(self):return not any(self.q)

    def event(self,kind,minute,**fields):
        self.events.append(dict(kind=kind,minute=minute,**fields))

    def liquidation(self,marks):
        fee=0.0011 if self.stress else 0.00055
        slip=0.0004 if self.stress else 0.0002
        # Adverse price-tick plus fee reserve for every residual quantity.
        closing=sum(abs(q)*(p*(fee+slip)+0.01) for q,p in zip(self.q,marks))
        return self.cash+sum(q*p for q,p in zip(self.q,marks))-closing

    def fund(self,minute,rates,marks):
        for leg,rate in rates.items():
            require(leg in (0,1) and math.isfinite(rate) and marks[leg]>0,'FUNDING_INPUT')
            amount=self.q[leg]*marks[leg]*rate
            if self.q[leg]:
                self.cash-=amount;self.leg_cash[leg]-=amount;self.funding+=amount
                self.event('funding',minute,leg=leg,quantity=self.q[leg],mark=marks[leg],rate=rate,amount=amount,cash=self.cash)

    def begin_exit(self,minute,reason):
        if self.exiting:return
        self.queue.clear();self.exiting=True;self.exit_deadline=minute+5
        self.event('exit_requested',minute,reason=reason,quantities=list(self.q),deadline=self.exit_deadline)
        active=[i for i in (0,1) if self.q[i]]
        if active:self.queue.append((minute+1,active[0],None,'exit'))
        else:self.finish(minute)

    def finish(self,minute):
        require(self.flat,'FINISH_WITH_EXPOSURE')
        if self.complete:self.episodes+=1
        self.event('flat',minute,completed_episode=self.complete,cash=self.cash)
        self.queue.clear();self.exiting=False;self.exit_deadline=None
        self.model=None;self.entry_minute=None;self.complete=False;self.sign=0;self.last_flat_minute=minute
        self.leg_cash=[0.,0.];self.collateral=[0.,0.]

    def stop(self,minute,reason):
        if self.stopped is None:
            self.stopped=reason;self.event('stop',minute,reason=reason,drawdown=self.dd)
        self.begin_exit(minute,reason)

    def risk(self,minute,marks,defer=False):
        adverse=[bar[2] if q>=0 else bar[1] for q,bar in zip(self.q,marks)]
        favorable=[bar[1] if q>=0 else bar[2] for q,bar in zip(self.q,marks)]
        self.peak=max(self.peak,self.liquidation(favorable))
        value=self.liquidation(adverse)
        self.dd=max(self.dd,(self.peak-value)/self.peak)
        gross=sum(abs(q)*max(bar[0],bar[1]) for q,bar in zip(self.q,marks))
        reasons=[]
        if self.dd>=.08:reasons.append('RESEARCH_DRAWDOWN_8_PERCENT')
        if gross>5000+1e-8:reasons.append('GROSS_EXPOSURE_CAP')
        for leg in (0,1):
            if self.q[leg]:
                cushion=self.collateral[leg]+self.leg_cash[leg]+self.q[leg]*adverse[leg]
                if cushion<=0:reasons.append('REFERENCE_ISOLATED_COLLATERAL_EXHAUSTED')
                if cushion/(abs(self.q[leg])*adverse[leg])<.08:
                    reasons.append('REFERENCE_LIQUIDATION_DISTANCE_UNDER_8_PERCENT')
        # OHLC extremes become known at this minute's end: never cancel a fill
        # already scheduled for this minute's open using these future extremes.
        if not defer:
            for reason in reasons:self.stop(minute,reason)
        return reasons

    def fill(self,minute,leg,delta,bar,phase):
        require(delta and math.isfinite(delta),'INVALID_FILL_DELTA')
        quantity=min(abs(delta),floor_quantity(bar[4]*.01,self.steps[leg]))
        quantity=floor_quantity(quantity+1e-12,self.steps[leg])
        if not quantity:
            self.event('no_fill',minute,leg=leg,requested_delta=delta,phase=phase)
            return False
        actual=math.copysign(quantity,delta);price=execution_price(bar,actual,self.stress)
        fee=abs(actual)*price*(.0011 if self.stress else .00055)
        if not self.q[leg]:self.collateral[leg]=abs(actual)*price
        old=self.q[leg];self.q[leg]=float(Decimal(str(self.q[leg]))+Decimal(str(actual)))
        if abs(self.q[leg])<self.steps[leg]*1e-7:self.q[leg]=0.
        require(phase!='exit' or abs(self.q[leg])<=abs(old)+1e-9,'EXIT_INCREASED_EXPOSURE')
        self.cash-=actual*price+fee;self.leg_cash[leg]-=actual*price+fee
        self.fees+=fee;self.traded+=abs(actual)*price
        self.event('fill',minute,leg=leg,delta=actual,requested_delta=delta,price=price,fee=fee,
            quantity_after=self.q[leg],cash=self.cash,phase=phase,bar=list(bar))
        return math.isclose(actual,delta,rel_tol=0,abs_tol=1e-9)

    def process(self,minute,bars):
        if self.insufficient:return
        if self.exiting and minute>self.exit_deadline:
            self.insufficient='RESIDUAL_EXPOSURE_UNCLOSED';self.event('insufficient',minute,reason=self.insufficient,quantities=list(self.q));return
        if not self.queue or self.queue[0][0]>minute:return
        due,leg,delta,phase=self.queue.popleft();require(due==minute,'MISSED_EXECUTION_CLOCK')
        if phase=='exit':delta=-self.q[leg]
        complete=self.fill(minute,leg,delta,bars[leg],phase)
        if phase=='entry':
            if self.entry_minute is None and self.q[leg]:self.entry_minute=minute
            if not complete:
                self.failed_entries+=1;self.begin_exit(minute,'PARTIAL_OR_FAILED_ENTRY')
            elif leg==1:self.complete=True
        elif self.flat:self.finish(minute)
        else:
            remaining=leg if self.q[leg] else next(i for i in (0,1) if self.q[i])
            self.queue.append((minute+1,remaining,None,'exit'))

    def decision(self,minute,prices,model,end_minute):
        if self.insufficient:return
        if minute>=end_minute-5:
            if not self.flat or self.queue:self.begin_exit(minute,'TERMINAL_EXIT')
            return
        if self.stopped or self.exiting or self.queue:return
        if not self.flat:
            z=score(self.model,prices)
            if abs(z)<=.5 or z*self.sign<=0 or minute-self.entry_minute>=self.horizon:
                self.begin_exit(minute,'RESIDUAL_OR_HORIZON_EXIT')
            return
        if minute>=end_minute-10 or model is None or self.last_flat_minute==minute:return
        z=score(model,prices)
        if abs(z)<self.threshold:return
        beta=model[1];direction=-1 if z>0 else 1
        qty=[floor_quantity(2500/(1+beta)/prices[0],self.steps[0]),
             floor_quantity(2500*beta/(1+beta)/prices[1],self.steps[1])]
        if any(q<step or q*p<5 for q,p,step in zip(qty,prices,self.steps)):return
        self.model=model;self.sign=1 if z>0 else -1
        self.queue.extend([(minute+1,0,direction*qty[0],'entry'),(minute+2,1,-direction*qty[1],'entry')])
        self.event('entry_requested',minute,z=z,model=list(model),quantities=[direction*qty[0],-direction*qty[1]])

    def close_minute(self,minute,marks,completed_five=False):
        liquid=self.liquidation(marks)
        self.peak=max(self.peak,liquid)
        self.dd=max(self.dd,(self.peak-liquid)/self.peak)
        if completed_five and not self.safety.withdrawn and not self.stopped:
            self.safety.observe(liquid-self.last_liquidation,1);self.observations+=1
            if self.safety.count==WINDOW:
                check=self.safety.assess();check['minute']=minute;self.safety_checks.append(check)
                if check['withdrawn']:self.stop(minute,'SAFETY_WITHDRAWAL')
        if completed_five:self.last_liquidation=liquid
        return liquid

    def result(self):
        return dict(cash=self.cash,positions=list(self.q),fees=self.fees,funding=self.funding,
            traded_notional=self.traded,maximum_drawdown=self.dd,completed_episodes=self.episodes,
            failed_entries=self.failed_entries,stop=self.stopped,insufficient=self.insufficient,
            safety_checks=self.safety_checks,observed_five_minute_intervals=self.observations)
