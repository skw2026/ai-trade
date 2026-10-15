"""Frozen low-frequency development model. No network or trading adapter.

OHLC stress and a volume cap are reference assumptions, not fill certification.
All timestamps are UTC milliseconds; bar time means its opening time.
"""
import csv
import datetime as dt
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import itertools
import math

STEP=300000
DAY=86400000
CAPITAL=10000.

def need(ok,reason):
    if not ok:raise ValueError(reason)

def millis(date):
    return int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp()*1000)

def day(ts):
    return dt.datetime.fromtimestamp(ts/1000,dt.timezone.utc).date()

def clip(v):return max(-3.,min(3.,v))

def quantity(v):
    return float((Decimal(str(max(0,v)))/Decimal('.001')).to_integral_value(rounding=ROUND_FLOOR)*Decimal('.001'))

def fill_price(row,dq,stress):
    raw=row['high' if dq>0 else 'low'] if stress else row['open']
    value=Decimal(str(raw))*(1+Decimal('.0004' if stress else '.0002')*(1 if dq>0 else -1))
    return float((value/Decimal('.1')).to_integral_value(rounding=ROUND_CEILING if dq>0 else ROUND_FLOOR)*Decimal('.1'))

def load_rows(path):
    rows=[]
    with open(path) as f:
        for raw in csv.DictReader(f):
            row={k:float(v) for k,v in raw.items() if k not in ('symbol','timestamp')}
            row['timestamp']=int(raw['timestamp'])
            need(raw['symbol']=='BTCUSDT' and row['interval_ms']==STEP,'BAR_IDENTITY')
            need(not rows or row['timestamp']==rows[-1]['timestamp']+STEP,'BAR_GRID')
            need(all(math.isfinite(v) for v in row.values()),'FINITE_BAR')
            need(min(row[k] for k in ('open','high','low','price','mark_open','mark_close','mark_high','mark_low'))>0,'BAR_PRICE')
            need(row['volume']>=0 and row['high']>=max(row['open'],row['price']) and
                 row['low']<=min(row['open'],row['price']),'TRADE_OHLC')
            need(row['mark_high']>=max(row['mark_open'],row['mark_close']) and
                 row['mark_low']<=min(row['mark_open'],row['mark_close']),'MARK_OHLC')
            rows.append(row)
    return rows

def features(rows,ratios,start,end,lag_days=3):
    closes={r['timestamp']+STEP:r['price'] for r in rows}
    out={}
    for t in range(start,end,DAY):
        d=day(t)-dt.timedelta(days=lag_days)
        x=float(ratios[d.isoformat()])
        past=[float(ratios[(d-dt.timedelta(days=i)).isoformat()]) for i in range(1,29)]
        mu=math.fsum(past)/28
        sd=math.sqrt(math.fsum((v-mu)**2 for v in past)/28)
        z=clip((x-mu)/sd) if sd>=1e-6 else None
        need(t in closes and t-7*DAY in closes,'CAUSAL_PRICE_CONTEXT')
        r=clip(math.log(closes[t]/closes[t-7*DAY])/.05)
        out[t]=dict(z=z,r=r,price=closes[t],source_day=d.isoformat(),latest_price_closed_at=t)
    return out

def signal(f,params):
    a,b,h=params
    if a and f['z'] is None:return 0
    s=a*(f['z'] or 0.)+b*f['r']
    return (1 if s>0 else -1) if abs(s)>=h else 0

def grids():
    return (list(itertools.product((-1,1),(-1,0,1),(1,2))),
            list(itertools.product((0,),(-1,1),(1,2))))

class Book:
    def __init__(self,stress=False,latency=1):
        self.stress=stress;self.latency=latency
        self.cash=CAPITAL;self.gross_cash=CAPITAL;self.q=0.
        self.fees=0.;self.slip=0.;self.funding=0.;self.turnover=0.
        self.peak=CAPITAL;self.dd=0.;self.close_peak=CAPITAL;self.close_dd=0.
        self.entry_time=None;self.entry_collateral=0.;self.leg_cash=0.
        self.before_entry=None;self.entry_full=False
        self.pending=None;self.exit_deadline=None;self.exit_day=None
        self.stopped=None;self.insufficient=None;self.events=[];self.cycles=[]
        self.days=[];self.weeks={};self.active_weeks=set();self.holding_bars=0
        self.long_bars=0;self.short_bars=0;self.exposure_sum=0.;self.failed_entries=0
        self.last_equity=CAPITAL;self.week_equity=CAPITAL;self.alert=False;self.negative_weeks=0
        self.last_exit_day=None

    def event(self,kind,t,**fields):
        self.events.append(dict(kind=kind,t=t,**fields))

    def equity(self,mark):
        return self.cash+self.q*mark-abs(self.q*mark)*.0015

    def request_exit(self,t,reason):
        if self.exit_deadline is not None:return
        self.pending=None
        if not self.q:return
        self.exit_deadline=t+6*STEP;self.exit_day=day(t)
        self.pending=dict(kind='exit',due=t+self.latency*STEP)
        self.event('exit_request',t,reason=reason,deadline=self.exit_deadline)

    def stop(self,t,reason):
        if self.stopped is None:
            self.stopped=dict(t=t,reason=reason);self.event('stop',t,reason=reason)
        self.request_exit(t,'hard_stop')

    def fund(self,row):
        rate=row['funding_rate_per_interval']
        if self.q and rate:
            amount=self.q*row['mark_open']*rate
            self.cash-=amount;self.leg_cash-=amount;self.funding+=amount
            self.event('funding',row['timestamp'],q=self.q,mark=row['mark_open'],rate=rate,amount=amount)

    def execute(self,row):
        t=row['timestamp']
        if not self.pending or self.pending['due']>t:return
        p=self.pending;self.pending=None
        entry=p['kind']=='entry'
        requested=p['quantity'] if entry else -self.q
        cap=quantity(row['volume']*.01)
        amount=min(abs(requested),cap)
        dq=math.copysign(amount,requested) if amount else 0.
        if dq:
            price=fill_price(row,dq,self.stress)
            fee=abs(dq)*price*(.0011 if self.stress else .00055)
            slip=dq*(price-row['open'])
            if entry:
                self.before_entry=self.cash;self.entry_time=t
                self.entry_collateral=abs(dq)*price;self.leg_cash=0.
                self.entry_full=abs(dq-requested)<1e-9
            self.cash-=dq*price+fee;self.leg_cash-=dq*price+fee
            self.gross_cash-=dq*row['open'];self.fees+=fee;self.slip+=slip
            self.turnover+=abs(dq)*price;self.q=round(self.q+dq,9)
            self.event('fill',t,phase=p['kind'],dq=dq,requested=requested,price=price,
                raw_open=row['open'],fee=fee,slippage=slip,quantity_after=self.q,
                cash_after=self.cash,capacity=cap)
        if entry:
            if amount+1e-9<abs(requested):
                self.failed_entries+=1
                self.event('failed_entry',t,requested=requested,filled=dq)
                self.request_exit(t,'partial_entry_unwind')
        elif self.q:
            self.pending=dict(kind='exit',due=t+STEP)
        else:
            self.cycles.append(dict(start=self.entry_time,end=t,net=self.cash-self.before_entry,
                                    complete=self.entry_full))
            self.event('flat',t,net=self.cash-self.before_entry,complete=self.entry_full)
            self.entry_time=None;self.entry_collateral=0.;self.leg_cash=0.;self.before_entry=None
            self.entry_full=False;self.exit_deadline=None;self.last_exit_day=day(t)
        if self.exit_deadline is not None and t>=self.exit_deadline and self.q:
            self.insufficient='EXIT_RESIDUAL';self.pending=None
            self.event('insufficient',t,reason=self.insufficient)

    def observe(self,row):
        t=row['timestamp']+STEP
        mark=row['mark_low'] if self.q>=0 else row['mark_high']
        adverse=self.equity(mark);close=self.equity(row['mark_close'])
        self.dd=max(self.dd,(self.peak-adverse)/self.peak)
        self.peak=max(self.peak,close)
        self.close_peak=max(self.close_peak,close)
        self.close_dd=max(self.close_dd,(self.close_peak-close)/self.close_peak)
        gross=abs(self.q)*row['mark_high']
        reason=None
        if self.dd>=.08:reason='RESEARCH_DRAWDOWN_8_PERCENT'
        elif gross>5000+1e-8:reason='GROSS_CAP'
        elif self.q:
            cushion=self.entry_collateral+self.leg_cash+self.q*mark-abs(self.q*mark)*.0015
            if cushion<=0:reason='REFERENCE_COLLATERAL_EXHAUSTED'
            elif cushion<.08*abs(self.q*mark):reason='REFERENCE_MARGIN_BUFFER'
        iso=day(row['timestamp']).isocalendar();week='%04d-W%02d'%(iso.year,iso.week)
        if self.q:
            self.active_weeks.add(week);self.holding_bars+=1
            self.long_bars+=int(self.q>0);self.short_bars+=int(self.q<0)
            self.exposure_sum+=abs(self.q)*row['mark_close']
        change=close-self.last_equity;self.weeks[week]=self.weeks.get(week,0.)+change
        self.last_equity=close
        if t%DAY==0:
            self.days.append(dict(t=t,equity=close))
            if day(t).weekday()==0:
                loss=close<self.week_equity
                self.negative_weeks=self.negative_weeks+1 if loss else 0
                if self.negative_weeks>=2 and not self.alert:
                    self.alert=True;self.event('performance_alert',t,action='FROZEN_INPUT_LEDGER_STATE_REVIEW')
                self.week_equity=close
        return reason

    def summary(self):
        return dict(stress=self.stress,cash=self.cash,quantity=self.q,net=self.last_equity-CAPITAL,
            gross=self.gross_cash-CAPITAL,fees=self.fees,slippage=self.slip,funding=self.funding,
            turnover=self.turnover,drawdown=self.dd,close_drawdown=self.close_dd,
            stopped=self.stopped,insufficient=self.insufficient,cycles=self.cycles,events=self.events,
            days=self.days,weeks=self.weeks,active_weeks=sorted(self.active_weeks),
            holding_bars=self.holding_bars,long_bars=self.long_bars,short_bars=self.short_bars,
            exposure_sum=self.exposure_sum,failed_entries=self.failed_entries)

def simulate(rows,fs,params,start,end,*,latency=1,passive=False,initially_stopped=False):
    need(start%DAY==0 and end%DAY==0 and start<end,'PERIOD')
    books=[Book(False,latency),Book(True,latency)]
    if initially_stopped:
        for b in books:b.stop(start,'TRAINING_HARD_STOP_RETAINED')
    by_time={r['timestamp']:r for r in rows}
    need(start-STEP in by_time and all(t in by_time for t in range(start,end,STEP)),'FULL_BAR_DOMAIN')
    count=0
    for t in range(start,end,STEP):
        row=by_time[t];count+=1
        # Decisions at t use the completed bar ending at t; no current OHLC.
        for b in books:
            if b.q and ((not passive and t>=b.entry_time+3*DAY) or t>=end-6*STEP):
                b.request_exit(t,'terminal' if t>=end-6*STEP else 'horizon')
        if t%DAY==0:
            f=fs[t]
            direction=1 if passive and t==start else (0 if passive else signal(f,params))
            for b in books:
                if (direction and not b.q and b.pending is None and b.stopped is None and
                    b.insufficient is None and b.last_exit_day!=day(t) and t<end-3*DAY-6*STEP):
                    q=direction*quantity(2500/f['price'])
                    if q:
                        b.pending=dict(kind='entry',due=t+latency*STEP,quantity=q)
                        b.event('entry_request',t,quantity=q,features=f,parameters=list(params),passive=passive)
        for b in books:
            b.fund(row);b.execute(row)
        reasons=[b.observe(row) for b in books]
        if any(reasons):
            reason=next(r for r in reasons if r)
            for b in books:b.stop(t+STEP,reason)
        # Evidence failures stop the entire paired reference path; retain exposure.
        if any(b.insufficient for b in books):break
    for b in books:
        if b.q and b.insufficient is None:b.insufficient='TERMINAL_RESIDUAL'
    return dict(parameters=list(params),start=start,end=end,latency=latency,passive=passive,
        initially_stopped=initially_stopped,bars=count,books=[b.summary() for b in books])

def eligible(result):
    return all(not b['stopped'] and not b['insufficient'] and b['quantity']==0 for b in result['books'])

def select(results):
    best=None
    for i,r in enumerate(results):
        if not eligible(r):continue
        score=min(b['net'] for b in r['books']);turnover=sum(b['turnover'] for b in r['books'])
        key=(turnover,tuple(r['parameters']))
        if best is None or score>best[1]+1e-6 or (abs(score-best[1])<=1e-6 and key<best[2]):
            best=(i,score,key)
    return None if best is None else best[0]

def concentration(book):
    cycles=[c['net'] for c in book['cycles']]
    weeks=list(book['weeks'].values())
    def share(values):
        positives=[v for v in values if v>0]
        return max(positives)/math.fsum(positives) if positives else None
    return dict(complete_cycles=sum(c['complete'] for c in book['cycles']),
        active_weeks=len(book['active_weeks']),largest_cycle_share=share(cycles),
        largest_week_share=share(weeks),without_best_week=book['net']-max(weeks,default=0.))

def verdict(paths):
    candidate,fixed,price,passive,chain_delay,execution_delay=paths
    relevant=(candidate,chain_delay,execution_delay)
    if any(b['insufficient'] or b['quantity'] for r in paths for b in r['books']):
        return dict(status='INSUFFICIENT',reasons=['EXECUTION_OR_RESIDUAL'])
    if any(b['stopped'] for r in relevant for b in r['books']):
        return dict(status='NO_GO_RISK',reasons=['CANDIDATE_OR_SENSITIVITY_HARD_STOP'])
    reasons=[]
    base,stress=candidate['books']
    if base['net']<=0 or stress['net']<200:reasons.append('ABSOLUTE_NET')
    if any(b['net']-p['net']<50 for b,p in zip(candidate['books'],price['books'])):reasons.append('PRICE_INCREMENT')
    learned=candidate['parameters']!=[-1,0,1]
    if learned and any(b['net']-p['net']<50 for b,p in zip(candidate['books'],fixed['books'])):reasons.append('LEARNING_INCREMENT')
    boundary=millis('2025-07-01')
    mid=next(d['equity'] for d in stress['days'] if d['t']==boundary)-CAPITAL
    if mid<=0 or stress['net']-mid<=0:reasons.append('HALF_YEAR_STABILITY')
    if any(b['net']<=0 for r in (chain_delay,execution_delay) for b in r['books']):reasons.append('DELAY_NET')
    if reasons:return dict(status='NO_GO_ECONOMIC',reasons=reasons,learned=learned)
    c=concentration(stress)
    if (c['complete_cycles']<12 or c['active_weeks']<8 or
        any(c[k] is None or c[k]>.35 for k in ('largest_cycle_share','largest_week_share')) or c['without_best_week']<=0):
        return dict(status='INSUFFICIENT_DIVERSITY',reasons=['EVENTS_OR_CONCENTRATION'],learned=learned)
    return dict(status='DEVELOPMENT_SUPPORTED',reasons=[],learned=learned)

def sharpe(book):
    values=[CAPITAL]+[x['equity'] for x in book['days']]
    returns=[b/a-1 for a,b in zip(values,values[1:])]
    n=len(returns);mu=math.fsum(returns)/n if n else 0.
    if book['stopped'] or book['insufficient'] or n<9:return dict(ordinary=None,hac7=None)
    centered=[v-mu for v in returns];variance=math.fsum(v*v for v in centered)/n
    lr=variance+2*math.fsum((1-k/8)*math.fsum(centered[i]*centered[i-k] for i in range(k,n))/n for k in range(1,8))
    return dict(ordinary=mu/math.sqrt(variance)*math.sqrt(365) if variance>0 else None,
                hac7=mu/math.sqrt(lr)*math.sqrt(365) if lr>0 else None)
