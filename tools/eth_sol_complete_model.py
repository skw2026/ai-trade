"""Frozen bounded development policy. It cannot send orders or change runtime locks."""
from collections import deque
from decimal import Decimal
import math
from statistics import NormalDist
import time

from eth_sol_model import PairLedger, estimate, require

D = lambda x: Decimal(str(x))
INITIAL = 10000.


class Alert:
    def __init__(self):
        self.count = 0
        self.total = 0.
        self.negative = 0
        self.positive = 0
        self.active = False
        self.first_legacy_withdrawal = None
        self.checks = []

    def observe(self, delta, minute, review):
        self.total += delta
        self.count += 1
        if self.count != 240:
            return True
        self.negative = self.negative+1 if self.total < 0 else 0
        self.positive = self.positive+1 if self.total > 0 else 0
        if self.negative >= 2:
            self.active = True
            if self.first_legacy_withdrawal is None:
                self.first_legacy_withdrawal = minute
        reviewed = review() if self.active else None
        if self.active and reviewed and self.positive >= 2:
            self.active = False
        self.checks.append(dict(minute=minute, net=self.total, negative=self.negative,
                               positive=self.positive, active=self.active, review_passed=reviewed))
        self.count = 0
        self.total = 0.
        return reviewed is not False


class CompleteLedger(PairLedger):
    """Reuse unchanged execution/risk, isolate the newly authorized alert semantics."""
    def __init__(self, stress=False, entry=2., hours=24, extra_delay=0):
        super().__init__(stress, entry, hours)
        require(extra_delay in (0,1), 'FIXED_DELAY_ONLY')
        self.extra_delay = extra_delay
        self.alert = Alert()
        self.audit_cash = D(10000)
        self.audit_q = [D(0),D(0)]
        self.gross_cash = D(0)
        self.slippage = D(0)
        self.cycle = None
        self.cycles = []

    def event(self, kind, minute, **fields):
        if kind == 'entry_requested':
            require(self.cycle is None, 'OVERLAPPING_EPISODES')
            self.cycle = dict(start=minute, cash=self.cash, fees=self.fees,
                              funding=self.funding, slippage=float(self.slippage))
            fields['decision_prices'] = list(self._decision_prices)
            fields['entry_delay'] = self.extra_delay
        elif kind == 'fill':
            leg = fields['leg']; delta = D(fields['delta']); price = D(fields['price'])
            self.audit_q[leg] += delta
            self.audit_cash -= delta*price+abs(delta)*price*D('.0011' if self.stress else '.00055')
            self.gross_cash -= delta*D(fields['bar'][0])
            self.slippage += delta*(price-D(fields['bar'][0]))
        elif kind == 'funding':
            self.audit_cash -= self.audit_q[fields['leg']]*D(fields['mark'])*D(fields['rate'])
        elif kind == 'flat' and self.cycle is not None:
            c = self.cycle
            self.cycles.append(dict(start=c['start'], end=minute, completed=fields['completed_episode'],
                net=self.cash-c['cash'], fees=self.fees-c['fees'], funding=self.funding-c['funding'],
                slippage=float(self.slippage)-c['slippage']))
            self.cycle = None
        super().event(kind, minute, **fields)

    def review(self):
        return (abs(self.audit_cash-D(self.cash)) <= D('0.000001') and
                all(abs(q-D(v)) <= D('0.000000001') for q,v in zip(self.audit_q,self.q)) and
                self.insufficient is None and all(math.isfinite(x) for x in (self.cash,self.peak,self.dd,*self.q)))

    def decision(self, minute, prices, model, end_minute):
        self._decision_prices = list(prices)
        before = len(self.events)
        super().decision(minute,prices,model,end_minute)
        if self.extra_delay and any(e['kind']=='entry_requested' for e in self.events[before:]):
            self.queue = deque((due+self.extra_delay,leg,q,phase) for due,leg,q,phase in self.queue)

    def begin_exit(self, minute, reason):
        before = self.exiting
        super().begin_exit(minute,reason)
        if self.extra_delay and not before and self.queue:
            due,leg,q,phase = self.queue.popleft()
            self.queue.appendleft((due+self.extra_delay,leg,q,phase))

    def close_minute(self, minute, marks, completed_five=False):
        liquid = super().close_minute(minute,marks,False)
        if completed_five:
            if not self.stopped and self.insufficient is None:
                old_checks = len(self.alert.checks)
                ok = self.alert.observe(liquid-self.last_liquidation,minute,self.review)
                self.observations += 1
                if len(self.alert.checks) != old_checks:
                    self.event('performance_check',minute,**{k:v for k,v in self.alert.checks[-1].items() if k!='minute'})
                if not ok:
                    self.insufficient = 'PERFORMANCE_REVIEW_FAILED'
                    self.stop(minute,'PERFORMANCE_REVIEW_FAILED')
            self.last_liquidation = liquid
        return liquid

    def snapshot(self):
        values = {k:v for k,v in vars(self).items() if k not in ('safety','alert','audit_cash','audit_q','gross_cash','slippage')}
        values['queue'] = [list(v) for v in self.queue]
        values['model'] = list(self.model) if self.model is not None else None
        values['alert'] = dict(vars(self.alert))
        values.update(audit_cash=str(self.audit_cash),audit_q=list(map(str,self.audit_q)),
                      gross_cash=str(self.gross_cash),slippage=str(self.slippage))
        return values

    @classmethod
    def restore(cls, value):
        p = cls(value['stress'],value['threshold'],value['horizon']/60,value['extra_delay'])
        for k,v in value.items():
            if k not in ('alert','audit_cash','audit_q','gross_cash','slippage','queue'):
                setattr(p,k,v)
        p.queue = deque(tuple(v) for v in value['queue'])
        vars(p.alert).update(value['alert'])
        p.audit_cash = D(value['audit_cash']); p.audit_q = list(map(D,value['audit_q']))
        p.gross_cash = D(value['gross_cash']); p.slippage = D(value['slippage'])
        return p

    def result(self):
        value = super().result()
        value.update(slippage=float(self.slippage),gross_cash=float(self.gross_cash),
                     performance_checks=self.alert.checks,first_legacy_withdrawal=self.alert.first_legacy_withdrawal,
                     cycles=self.cycles,online_updates=0,extra_delay_minutes=self.extra_delay)
        return value


class Simulation:
    """A single continuous year with a serializable, nonterminal Q1 checkpoint."""
    def __init__(self, archive, start, end, parameters, extra_delay=0):
        self.archive = archive; self.start = start; self.end = end; self.minute = 0
        self.parameters = list(parameters); self.extra_delay = extra_delay
        self.paths = [CompleteLedger(s,*parameters,extra_delay) for s in (False,True)]
        self.hours = deque(archive.warmup,maxlen=1440)
        require(len(self.hours)==1440, 'COMPLETE_WARMUP_REQUIRED')
        self.model = None; self.models = []; self.last_liquid = [INITIAL,INITIAL]
        self.daily = [[0,INITIAL,INITIAL]]; self.five = []
        self.active_weeks = [set(),set()]; self.position_minutes = [0,0]
        self.notional_minutes = [0.,0.]; self.invalid_model_days = 0
        self.stop_minute = None

    def link_stop(self, minute):
        if any(p.stopped or p.insufficient for p in self.paths):
            if self.stop_minute is None:
                self.stop_minute = minute
            for p in self.paths:
                if not p.stopped:
                    p.stop(minute,'PEER_PATH_REJECTED')

    def run_to(self, stop_minute, deadline=None):
        require(self.minute <= stop_minute <= (self.end-self.start)//60000, 'CONTINUATION_BOUNDARY')
        maximum = (self.end-self.start)//60000
        while self.minute < stop_minute:
            m = self.minute; t = self.start+m*60000
            if m%1440==0 and deadline is not None:
                require(time.monotonic() < deadline, 'ECONOMIC_COMPUTATION_BUDGET')
            if any(p.insufficient for p in self.paths):
                break
            prev_trade = self.archive.bars(t-60000,'trade')
            if m and m%60==0:
                self.hours.append(tuple(b[3] for b in prev_trade))
            if m%1440==0:
                self.model = estimate(self.hours)
                self.models.append(dict(minute=m,model=self.model))
                if self.model is None:
                    self.invalid_model_days += 1
            if m%5==0 and self.stop_minute is None:
                for p in self.paths:
                    p.decision(m,[b[3] for b in prev_trade],self.model,maximum)
            trade,mark = self.archive.bars(t,'trade'),self.archive.bars(t,'mark')
            rates = self.archive.rates(t)
            for p in self.paths:
                if p.stopped and p.flat and not p.queue:
                    continue
                p.fund(m,rates,[b[0] for b in mark])
                deferred = p.risk(m,mark,defer=True)
                p.process(m,trade)
                for reason in deferred:
                    p.stop(m,reason)
                p.risk(m,mark)
            self.link_stop(m)
            self.last_liquid = [p.close_minute(m+1,[b[3] for b in mark],(m+1)%5==0) for p in self.paths]
            self.link_stop(m+1)
            for i,p in enumerate(self.paths):
                if not p.flat:
                    week = ((t//60000)+3*1440)//(7*1440)
                    self.active_weeks[i].add(week)
                    self.position_minutes[i] += 1
                    self.notional_minutes[i] += sum(abs(q)*b[3] for q,b in zip(p.q,mark))
            self.minute += 1
            if self.minute%5==0:
                self.five.append([self.minute,*self.last_liquid])
            if self.minute%1440==0:
                self.daily.append([self.minute,*self.last_liquid])
        if self.minute == maximum:
            for p in self.paths:
                if not p.flat or p.queue:
                    p.insufficient = p.insufficient or 'DOMAIN_ENDED_WITH_RESIDUAL_OR_PENDING'
        return self

    def snapshot(self):
        return dict(start=self.start,end=self.end,minute=self.minute,parameters=self.parameters,
            extra_delay=self.extra_delay,paths=[p.snapshot() for p in self.paths],hours=[list(v) for v in self.hours],
            model=list(self.model) if self.model is not None else None,
            models=[dict(minute=v['minute'],model=list(v['model']) if v['model'] is not None else None) for v in self.models],
            last_liquid=self.last_liquid,daily=self.daily,five=self.five,
            active_weeks=[sorted(s) for s in self.active_weeks],position_minutes=self.position_minutes,
            notional_minutes=self.notional_minutes,invalid_model_days=self.invalid_model_days,stop_minute=self.stop_minute)

    @classmethod
    def restore(cls, archive, value):
        sim = cls(archive,value['start'],value['end'],value['parameters'],value['extra_delay'])
        for k,v in value.items():
            if k not in ('paths','hours','active_weeks'):
                setattr(sim,k,v)
        sim.paths = [CompleteLedger.restore(p) for p in value['paths']]
        sim.hours = deque((tuple(v) for v in value['hours']),maxlen=1440)
        sim.active_weeks = [set(v) for v in value['active_weeks']]
        return sim

    def result(self):
        return dict(parameters=self.parameters,extra_delay=self.extra_delay,start=self.start,end=self.end,
            processed_minutes=self.minute,requested_minutes=(self.end-self.start)//60000,
            base=self.paths[0].result(),stress=self.paths[1].result(),last_liquidation=self.last_liquid,
            daily=self.daily,active_weeks=[sorted(s) for s in self.active_weeks],
            position_minutes=self.position_minutes,notional_minutes=self.notional_minutes,
            invalid_model_days=self.invalid_model_days,stop_minute=self.stop_minute)


def select(results):
    eligible = [i for i,r in enumerate(results) if all(not r[k]['stop'] and not r[k]['insufficient'] for k in ('base','stress'))]
    if not eligible:
        return None
    scores = {i:min(results[i]['last_liquidation'])-INITIAL for i in eligible}
    top = max(scores.values())
    tied = [i for i in eligible if top-scores[i] <= 1e-6]
    return min(tied,key=lambda i:(i!=0,sum(results[i][k]['traded_notional'] for k in ('base','stress')),i))


def long_variance(values, lag):
    if len(values) <= lag:
        return None
    mean = math.fsum(values)/len(values)
    centered = [v-mean for v in values]
    value = math.fsum(v*v for v in centered)/len(values)
    value += 2*math.fsum((1-j/(lag+1))*math.fsum(centered[t]*centered[t-j] for t in range(j,len(values)))/len(values)
                        for j in range(1,lag+1))
    return value if value > 0 and math.isfinite(value) else None


def sharpe(daily, stopped):
    if stopped or len(daily)<9:
        return dict(naive=None,hac7=None,reason='STOPPED_OR_TOO_SHORT')
    returns = [(b-a)/a for a,b in zip(daily,daily[1:])]
    mean = math.fsum(returns)/len(returns)
    var = math.fsum((x-mean)**2 for x in returns)/(len(returns)-1)
    hac = long_variance(returns,7)
    return dict(naive=mean/math.sqrt(var)*math.sqrt(365) if var>0 else None,
                hac7=mean/math.sqrt(hac)*math.sqrt(365) if hac else None,
                reason='DEVELOPMENT_ESTIMATE_NOT_CONFIRMED')


def metrics(result, boundary=91*1440):
    daily = {row[0]:row[1:] for row in result['daily']}
    if boundary not in daily or result['requested_minutes'] not in daily:
        return None
    end = result['requested_minutes']; out = {}
    for index,name in enumerate(('base','stress')):
        path = result[name]; eval_initial = daily[boundary][index]
        weekly_marks = [daily[boundary+i*10080][index] for i in range(40)]
        weekly = [b-a for a,b in zip(weekly_marks,weekly_marks[1:])]
        tail = daily[end][index]-weekly_marks[-1]
        # Include the final partial week in concentration; do not hide its losses.
        all_week = weekly+[tail]
        positive_sum = sum(max(0,v) for v in all_week)
        cycles = [c for c in path['cycles'] if c['start'] >= boundary and c['completed']]
        positive_cycles = sum(max(0,c['net']) for c in cycles)
        first_week = ((result['start']//60000+boundary)+3*1440)//10080
        quarters = [daily[q][index] for q in (91*1440,182*1440,274*1440,366*1440)]
        out[name] = dict(full_net=path['cash']-INITIAL,net=daily[end][index]-eval_initial,
            quarter_net=[b-a for a,b in zip(quarters,quarters[1:])],weekly=weekly,tail_net=tail,
            episodes=len(cycles),active_weeks=sum(w>=first_week for w in result['active_weeks'][index]),
            max_episode_positive_share=max([max(0,c['net']) for c in cycles],default=0)/positive_cycles if positive_cycles>0 else None,
            max_week_positive_share=max(all_week)/positive_sum if positive_sum>0 else None,
            stress_net_without_best_week=daily[end][index]-eval_initial-max(all_week),
            sharpe=sharpe([daily[x][index] for x in sorted(daily) if x>=boundary],bool(path['stop'] or path['insufficient'])))
    return out


def economic_decision(candidate, baseline, latency, selected_index):
    paths = [r[k] for r in (candidate,latency) for k in ('base','stress')]
    if any(p['insufficient'] or any(p['positions']) for p in paths) or any(r['processed_minutes']!=r['requested_minutes'] for r in (candidate,latency)):
        return dict(status='TECHNICAL_OR_EXECUTION_INSUFFICIENT',failures=['MEASUREMENT_OR_EXIT'])
    if any(p['stop'] for p in paths):
        return dict(status='NO_GO_RISK',failures=['HARD_STOP'])
    c,b,l = metrics(candidate),metrics(baseline),metrics(latency)
    if any(x is None for x in (c,b,l)) or any(baseline[k]['insufficient'] for k in ('base','stress')):
        return dict(status='TECHNICAL_OR_EXECUTION_INSUFFICIENT',failures=['COMPARISON_INCOMPLETE'])
    failures=[]
    if not all(c[k]['full_net']>0 for k in c) or c['base']['net']<=0 or c['stress']['net']<200:
        failures.append('ABSOLUTE_NET_HURDLE')
    if selected_index!=0 and any(c[k]['net']-b[k]['net']<50 for k in c):
        failures.append('LEARNING_GAIN_HURDLE')
    if sum(x>0 for x in c['stress']['quarter_net'])<2 or min(c['stress']['quarter_net']) < -100:
        failures.append('QUARTER_STABILITY')
    if any(l[k]['full_net']<=0 or l[k]['net']<=0 for k in l):
        failures.append('LATENCY_SENSITIVITY')
    diversity=[]
    for k in c:
        if c[k]['episodes']<20 or c[k]['active_weeks']<8:
            diversity.append(k+'_EVENT_CAPACITY')
        if (c[k]['max_episode_positive_share'] is None or c[k]['max_episode_positive_share']>.35 or
            c[k]['max_week_positive_share'] is None or c[k]['max_week_positive_share']>.35):
            diversity.append(k+'_CONCENTRATION')
    if c['stress']['stress_net_without_best_week']<=0:
        diversity.append('BEST_WEEK_REMOVAL')
    return dict(status='NO_GO_ECONOMIC' if failures else 'INSUFFICIENT_EVENT_DIVERSITY' if diversity else 'DEVELOPMENT_SUPPORTED',
                failures=failures,diversity_failures=diversity,candidate=c,baseline=b,latency=l,
                selected_is_baseline=selected_index==0,learned_gain_claim=selected_index!=0 and not failures and not diversity)


def confirmation_plan(decision):
    if decision['status']!='DEVELOPMENT_SUPPORTED':
        return dict(status='NOT_JUSTIFIED_BY_DEVELOPMENT',observation_started=False)
    c,b=decision['candidate'],decision['baseline']
    series=[('base_absolute',c['base']['weekly'],200*7/275),('stress_absolute',c['stress']['weekly'],200*7/275)]
    if not decision['selected_is_baseline']:
        series += [(k+'_gain',[x-y for x,y in zip(c[k]['weekly'],b[k]['weekly'])],50*7/275) for k in ('base','stress')]
    m=len(series); z=NormalDist().inv_cdf(.95)+NormalDist().inv_cdf(1-.2/m)
    cases=[]
    for name,values,delta in series:
        for lag in (1,2,4):
            variance=long_variance(values,lag)
            if variance is None:
                return dict(status='NO_AFFORDABLE_CONFIRMATION_EXIT',reason='INVALID_LONG_RUN_VARIANCE',observation_started=False)
            for multiplier in (1,1.5,2):
                cases.append(dict(series=name,lag=lag,sd_multiplier=multiplier,
                    weeks=math.ceil((z*math.sqrt(variance)*multiplier/delta)**2)))
    required=max(v['weeks'] for v in cases)
    choice=next((v for v in (8,13,26) if v>=required),None)
    return dict(status='CONDITIONAL_PLANNING_ONLY' if choice else 'NO_AFFORDABLE_CONFIRMATION_EXIT',
                required_weeks=required,option_weeks=choice,cases=cases,verified_market_power=False,observation_started=False)
