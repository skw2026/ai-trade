#!/usr/bin/env python3
"""Single frozen attention experiment; reference accounting, never an actuator."""
import argparse
from decimal import Decimal as D
import json
import math
import random
import time

import attention_capacity as a
from screen_regional_session import need, sha, strict, save_new, utc, quantile

SOURCES = ('tools/screen_attention.py', 'tools/test_screen_attention.py')


def unit(side, entry, exit_price, funding, fee, slip):
    ep, xp = entry*(1+side*slip), exit_price*(1-side*slip)
    gross = side*(exit_price-entry)/entry
    slipped = side*(xp-ep)/ep
    fees = fee*(1+xp/ep)
    carry = side*sum(mark*rate for mark,rate in funding)/ep
    return dict(net=slipped-fees-carry, raw_gross=gross, slippage=gross-slipped,
                fee=fees, funding=carry)


def path(signals, trade, mark, funding, c):
    fee,slip = c['cost']['fee_bps_per_side']/10000, c['cost']['slippage_bps_per_side']/10000
    allocation, limit = c['economy']['allocation'], c['economy']['reference_drawdown_stop']
    nav,peak,dd_lower,dd_upper = 1.,1.,0.,0.
    rows = []
    def observe(lo, hi, close, at, phase, entry, cash, quantity):
        nonlocal peak,dd_lower,dd_upper
        # Existing peak precedes this bar. Its high precedes its close but not necessarily its low.
        lower = max(1-lo/peak,1-close/max(peak,hi),0.)
        upper = max(1-lo/max(peak,hi),0.)
        dd_lower,dd_upper = max(dd_lower,lower),max(dd_upper,upper)
        peak = max(peak,hi)
        if lower >= limit or upper >= limit:
            return dict(status='REJECT' if lower >= limit else 'INSUFFICIENT_RISK_ORDER',
                        reason='REFERENCE_DRAWDOWN' if lower >= limit else 'INTRABAR_ORDER_UNKNOWN',
                        at=at, phase=phase, entry=entry, nav_low=lo, nav_high=hi,
                        marked_close=close, cash=cash, signed_quantity=quantity,
                        lower_drawdown=dd_lower, upper_drawdown=dd_upper,
                        no_fill_invented=True)
        return None
    for signal in signals:
        entry,exit_time,side = signal['entry'],signal['exit'],signal['side']
        start_nav = nav
        p = float(trade[entry][0])
        ep = p*(1+side*slip)
        q = allocation*nav/ep
        cash = nav-q*ep*fee
        for t in range(entry,exit_time,a.HOUR):
            o,h,l,cl = map(float,mark[t])
            opening = cash+side*q*(o-ep)
            stop = observe(opening,opening,opening,t,'mark_open',entry,cash,side*q)
            if stop:
                return summary(rows,nav,peak,dd_lower,dd_upper,stop)
            if t in funding:
                cash -= side*q*o*float(funding[t])
            opening = cash+side*q*(o-ep)
            stop = observe(opening,opening,opening,t,'funding',entry,cash,side*q)
            if stop:
                return summary(rows,nav,peak,dd_lower,dd_upper,stop)
            low_nav = cash+side*q*((l if side>0 else h)-ep)
            high_nav = cash+side*q*((h if side>0 else l)-ep)
            close_nav = cash+side*q*(cl-ep)
            stop = observe(low_nav,high_nav,close_nav,t,'mark_ohlc',entry,cash,side*q)
            if stop:
                return summary(rows,nav,peak,dd_lower,dd_upper,stop)
        pexit = float(trade[exit_time][0])
        xp = pexit*(1-side*slip)
        nav = cash+side*q*(xp-ep)-q*xp*fee
        stop = observe(nav,nav,nav,exit_time,'exit',entry,nav,0.)
        if stop:
            return summary(rows,nav,peak,dd_lower,dd_upper,stop)
        carry = [(float(mark[t][0]),float(funding[t])) for t in range(entry,exit_time,a.HOUR) if t in funding]
        result = unit(side,p,pexit,carry,fee,slip)
        controls = [unit(s,p,pexit,carry,fee,slip)['net'] for s in (signal['control'],1)]
        need(math.isclose(nav,start_nav*(1+allocation*result['net']),abs_tol=1e-12), 'CASH_UNIT_DISAGREE')
        rows.append(dict(**signal, **result, price_control_net=controls[0], long_control_net=controls[1],
                         nav=nav, cash=nav, reference_pre_entry_nav=start_nav))
    return summary(rows,nav,peak,dd_lower,dd_upper,None)


def summary(rows,nav,peak,lower,upper,stop):
    return dict(rows=rows,completed_positions=len(rows),last_reference_nav=nav,peak=peak,
                lower_drawdown=lower,upper_drawdown=upper,stop=stop)


def independent_cash(signals, trade, mark, funding, c):
    """Decimal position/cash journal, independently iterating the chronological clock."""
    one = D(1)
    fee = D(str(c['cost']['fee_bps_per_side']))/10000
    slip = D(str(c['cost']['slippage_bps_per_side']))/10000
    weight,limit = D(str(c['economy']['allocation'])), D(str(c['economy']['reference_drawdown_stop']))
    capital,maximum,lower_max,upper_max = one,one,D(0),D(0)
    daily = []
    for sig in signals:
        direction = D(sig['side'])
        entry = D(trade[sig['entry']][0])*(one+direction*slip)
        quantity = direction*weight*capital/entry
        balance = capital-abs(quantity)*entry*fee
        for tick in range(sig['entry'], sig['exit']+a.HOUR, a.HOUR):
            if tick == sig['exit']:
                execution = D(trade[tick][0])*(one-direction*slip)
                balance += quantity*(execution-entry)-abs(quantity)*execution*fee
                stages = [('exit',balance,balance,balance)]
            else:
                op,high,low,cl = map(D,mark[tick])
                before = balance+quantity*(op-entry)
                if tick in funding:
                    balance -= quantity*op*D(funding[tick])
                after = balance+quantity*(op-entry)
                extremes = [balance+quantity*(low-entry),balance+quantity*(high-entry)]
                stages = [('mark_open',before,before,before),('funding',after,after,after),
                          ('mark_ohlc',min(extremes),max(extremes),balance+quantity*(cl-entry))]
            for phase,lo,hi,close in stages:
                prior = maximum
                maximum = max(maximum,hi)
                lower = max(D(0),(prior-lo)/prior,(maximum-close)/maximum)
                upper = max(D(0),(maximum-lo)/maximum)
                lower_max,upper_max = max(lower_max,lower),max(upper_max,upper)
                if upper >= limit:
                    return dict(completed=daily,at=tick,phase=phase,
                                status='REJECT' if lower>=limit else 'INSUFFICIENT_RISK_ORDER',
                                low=float(lo),high=float(hi),close=float(close),
                                lower=float(lower_max),upper=float(upper_max))
            if tick == sig['exit']:
                capital = balance
                daily.append(float(capital))
    return dict(completed=daily,at=None,lower=float(lower_max),upper=float(upper_max))


def statistics(rows,c,independent=False):
    n = len(rows)
    vectors = [(r['net'],r['net']-r['price_control_net'],r['net']-r['long_control_net']) for r in rows]
    spec = c['economy']['bootstrap']
    length = spec['block_days']
    rng = random.Random(spec['seed'])
    draws = [[],[],[]]
    prefix = [[0.] for _ in range(3)]
    if independent:
        for vector in vectors*2:
            for j,x in enumerate(vector):
                prefix[j].append(prefix[j][-1]+x)
    for _ in range(spec['replicates']):
        sums = [0.,0.,0.]
        remaining = n
        while remaining:
            start = rng.randrange(n)
            count = min(length,remaining)
            if independent:
                for j in range(3):
                    sums[j] += prefix[j][start+count]-prefix[j][start]
            else:
                for i in range(count):
                    for j in range(3):
                        sums[j] += vectors[(start+i)%n][j]
            remaining -= count
        for j in range(3):
            draws[j].append(sums[j]/n)
    means = [sum(v[j] for v in vectors)/n for j in range(3)]
    bounds = [quantile(v,spec['lower_quantile']) for v in draws]
    halves = [[r['net'] for r in rows if (r['entry']<a.ms('2024-01-01T00:00:00Z')) == early]
              for early in (True,False)]
    return dict(means=means,lower_bounds=bounds,half_means=[sum(x)/len(x) if x else None for x in halves],
                labels=['net','minus_price_control','minus_long_control'],bootstrap=spec)


def freeze():
    a.running()
    a.frozen()
    cap = strict((a.RUN/'capacity.json').read_bytes())
    need(cap['eligible'], 'CAPACITY_NOT_ADMITTED')
    # Validate every input grid before the irrevocable experiment marker.
    a.inputs({'views','trade','mark','funding'})
    hashes = {str(p.relative_to(a.RUN)):sha(p.read_bytes()) for folder in ('raw','pages')
              for p in (a.RUN/folder).iterdir()}
    save_new(a.RUN/'economy-freeze.json',dict(at=utc(time.time()*1000),input_sha256=hashes,
        capacity_sha256=sha((a.RUN/'capacity.json').read_bytes()),
        source_sha256={p:sha((a.ROOT/p).read_bytes()) for p in SOURCES}))


def bound():
    a.frozen()
    frozen = strict((a.RUN/'economy-freeze.json').read_bytes())
    need(all(sha((a.ROOT/p).read_bytes()) == h for p,h in frozen['source_sha256'].items()),'ECONOMIC_SOURCE_CHANGED')
    need(all(sha((a.RUN/p).read_bytes()) == h for p,h in frozen['input_sha256'].items()),'ECONOMIC_INPUT_CHANGED')
    need(sha((a.RUN/'capacity.json').read_bytes()) == frozen['capacity_sha256'],'CAPACITY_CHANGED')


def screen():
    a.running()
    bound()
    c = a.config()
    cap = strict((a.RUN/'capacity.json').read_bytes())
    data = a.inputs({'trade','mark','funding'})
    save_new(a.RUN/'experiment-start.json',dict(at=utc(time.time()*1000),economic_experiments=1))
    started = time.monotonic()
    result = path(cap['signals'],data['trade'],data['mark'],data['funding'],c)
    stats = None
    if result['stop']:
        status,reason = result['stop']['status'],result['stop']['reason']
    else:
        stats = statistics(result['rows'],c)
        positive = all(x>0 for x in stats['lower_bounds']) and all(x is not None and x>0 for x in stats['half_means'])
        status = 'WORTH_INDEPENDENT_CHECK' if positive else 'INSUFFICIENT_EVIDENCE'
        reason = 'FIXED_STATISTICAL_SCREEN'
    result.update(status=status,reason=reason,statistics=stats,capacity_days=cap['valid_days'],
                  candidate_qualified=False,economic_experiments=1,scope='REFERENCE_RECONSTRUCTED_DATA_ONLY',
                  public_reads=23+len(list((a.RUN/'attempts').glob('*.json'))))
    need(time.monotonic()-started < 1800,'COMPUTE_BUDGET')
    save_new(a.RUN/'result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))


def audit():
    a.running()
    bound()
    c = a.config()
    cap = strict((a.RUN/'capacity.json').read_bytes())
    result = strict((a.RUN/'result.json').read_bytes())
    data = a.inputs({'views','trade','mark','funding'})
    # Independent direction/count derivation, with integer input and decimal price comparisons.
    derived = []
    incident = c['known_input_exclusion']
    lo,hi = a.ms(incident['from']+'T00:00:00Z'),a.ms(incident['through']+'T00:00:00Z')
    for entry in range(a.ms(c['calendar']['first_entry']),a.ms(c['calendar']['end_exclusive']),a.DAY):
        day = entry//a.DAY*a.DAY
        times = (day-3*a.DAY,day-2*a.DAY)
        if any(lo<=t<=hi for t in times):
            continue
        av,bv = [data['views'][t] for t in times]
        ap,bp = [D(data['trade'][t][0]) for t in (day-2*a.DAY,day-a.DAY)]
        if av==bv or ap==bp:
            continue
        derived.append((entry,1 if av>bv else -1,1 if ap>bp else -1))
    need(derived == [(r['entry'],r['side'],r['control']) for r in cap['signals']],'SIGNAL_AUDIT')
    other = independent_cash(cap['signals'],data['trade'],data['mark'],data['funding'],c)
    need(len(other['completed']) == len(result['rows']),'INDEPENDENT_POSITION_COUNT')
    error = max([abs(x-r['nav']) for x,r in zip(other['completed'],result['rows'])]+[0.])
    need(error < 1e-10,'INDEPENDENT_CASH_DISAGREES')
    stop = result['stop']
    need(other['at'] == (stop['at'] if stop else None),'INDEPENDENT_STOP_TIME')
    need(abs(other['lower']-result['lower_drawdown'])<1e-10 and
         abs(other['upper']-result['upper_drawdown'])<1e-10,'INDEPENDENT_RISK')
    if stop:
        need(other['status']==stop['status'] and other['phase']==stop['phase'],'INDEPENDENT_STOP_TYPE')
        need(abs(other['low']-stop['nav_low'])<1e-10 and abs(other['high']-stop['nav_high'])<1e-10,'INDEPENDENT_STOP_VALUE')
    # Independently recompute each completed row's unit net for candidate and both controls.
    max_unit_error = 0.
    for row in result['rows']:
        for side,key in ((row['side'],'net'),(row['control'],'price_control_net'),(1,'long_control_net')):
            f = D(str(c['cost']['fee_bps_per_side']))/10000
            sl = D(str(c['cost']['slippage_bps_per_side']))/10000
            ep = D(data['trade'][row['entry']][0])*(1+D(side)*sl)
            xp = D(data['trade'][row['exit']][0])*(1-D(side)*sl)
            carry = sum((D(data['mark'][t][0])*D(data['funding'][t]) for t in
                         range(row['entry'],row['exit'],a.HOUR) if t in data['funding']),D(0))
            net = (D(side)*(xp-ep)-f*(ep+xp)-D(side)*carry)/ep
            max_unit_error=max(max_unit_error,abs(float(net)-row[key]))
    need(max_unit_error<1e-10,'INDEPENDENT_UNIT_CASH')
    if result['statistics']:
        other_stats = statistics(result['rows'],c,independent=True)
        need(all(abs(x-y)<1e-10 for x,y in zip(other_stats['lower_bounds'],result['statistics']['lower_bounds'])),
             'INDEPENDENT_BOOTSTRAP')
    else:
        need(stop is not None,'NO_STATISTICS_REASON')
    report = dict(status='TECHNICAL_AUDIT_PASS_NOT_CANDIDATE_QUALIFICATION',capacity_days=len(derived),
                  cash_max_error=error,unit_max_error=max_unit_error,stop=other,
                  old_gates_unchanged=12, result_sha256=sha((a.RUN/'result.json').read_bytes()),
                  statistics_audited=result['statistics'] is not None)
    save_new(a.RUN/'audit.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='stop'},indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action',choices=('freeze','screen','audit'))
    globals()[p.parse_args().action]()
