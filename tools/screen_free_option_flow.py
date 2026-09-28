#!/usr/bin/env python3
"""One frozen research-only monthly option-flow screen; no actuators."""
import argparse
from decimal import Decimal
import json
import math
import random
import time

import free_option_flow_data as f
from screen_regional_session import need, sha, strict, number, stamp, ms, utc, save_new, quantile

ECON_SOURCES = f.SOURCES + ('tools/screen_free_option_flow.py','tools/test_screen_free_option_flow.py')
FEE, SLIP, ALLOCATION, RISK = .0006, .0005, .25, .08
REPS, BLOCKS, SEED, ALPHA = 10000, (3,6), 20260928, .05/6


def incidents(meta, day):
    a, b = ms(day+'T07:25:00Z'), ms(day+'T08:00:00Z')
    overlapping, reversed_dates = [], []
    for i, event in enumerate(meta['incidentReports']):
        x,y = ms(event['from']),ms(event['to'])
        if x > y:
            reversed_dates.append(i)
        if min(x,y) < b and max(x,y) > a:
            overlapping.append(i)
    return overlapping, reversed_dates


def freeze():
    f.running(); f.frozen()
    need((f.RUN/'probe.json').exists(), 'FEASIBILITY_REQUIRED')
    save_new(f.RUN/'economy-freeze.json', dict(created_utc=utc(time.time()*1000),
        plan_sha256=sha((f.ROOT/f.PLAN).read_bytes()),
        metadata_diagnosis_sha256=sha((f.RUN/'metadata-diagnosis.json').read_bytes()),
        metadata_handling='reversed incident endpoints: conservative full uncertainty interval, no year correction',
        source_sha256={p:sha((f.ROOT/p).read_bytes()) for p in ECON_SOURCES},
        experiments_allowed=1, returns_observed_before_freeze=False))


def bound():
    f.running(); f.frozen()
    spec = strict((f.RUN/'economy-freeze.json').read_bytes())
    need(all(sha((f.ROOT/p).read_bytes()) == h for p,h in spec['source_sha256'].items()), 'ECONOMIC_SOURCE_CHANGED')
    return spec


def load_day(day, meta):
    data = {'option':f.options(f.read_page(day+'-options'),day,True)}
    for kind in ('trade','mark','funding'):
        rows = f.bybit(f.read_page(day+'-'+kind),day,kind)
        if kind == 'funding':
            data[kind] = {stamp(r['fundingRateTimestamp']):r['fundingRate'] for r in rows}
        else:
            data[kind] = {stamp(r[0]):r[1:5] for r in rows}
    overlap,_ = incidents(meta,day)
    data['incident_overlap'] = overlap
    return data


def collect():
    bound()
    c = f.Collector()
    meta = f.metadata(f.read_page('metadata'))
    need(ms(meta['datasets']['exportedFrom']) <= ms(f.DATES[0]+'T00:00:00Z') and
         ms(meta['datasets']['exportedUntil']) >= ms(f.DATES[-1]+'T00:00:00Z')+f.DAY, 'EXPORT_COVERAGE')
    quality = []
    for day in f.DATES:
        bound()
        for kind,url in f.specs(day).items():
            p = c.get(day+'-'+kind,url)
            if kind == 'options':
                row = f.options(p,day)
            else:
                f.bybit(p,day,kind)
        overlap,reversed_dates = incidents(meta,day)
        row.update(incident_overlap=overlap, malformed_incident_indices=reversed_dates)
        quality.append(row)
        print(json.dumps(dict(event='DAY_VALIDATED',date=day,quality=row['quality'],
                              expiring_calls=row['expiring_calls'],incident_overlap=overlap)),flush=True)
    inputs = {str(p.relative_to(f.RUN)):sha(p.read_bytes()) for folder in ('raw','pages','attempts','receipts')
              for p in sorted((f.RUN/folder).iterdir())}
    save_new(f.RUN/'collection.json',dict(quality=quality,input_sha256=inputs,
        public_gets=f.DOC_GETS+len(list((f.RUN/'attempts').glob('*.json'))),
        raw_market_gets=len(list((f.RUN/'attempts').glob('*.json'))),documentation_reads=f.DOC_GETS))


def signal(data,day):
    option = data['option']
    if not option['quality'] or data['incident_overlap'] or not option['calls']:
        return dict(valid=False,A=False,B=False,C=False,pressure=None,decline=None)
    amounts = {side:sum((Decimal(r['amount']) for r in option['calls'] if r['side']==side),Decimal(0))
               for side in ('buy','sell')}
    pressure = (amounts['sell']-amounts['buy'])/(amounts['sell']+amounts['buy'])
    decline = Decimal(data['trade'][ms(day+'T07:55:00Z')][0]) / Decimal(data['trade'][ms(day+'T07:30:00Z')][0])-1
    A = pressure > 0 and decline < 0
    B = pressure <= 0 and decline < 0
    return dict(valid=True,A=A,B=B,C=not A,pressure=float(pressure),decline=float(decline))


def unit_return(data,day):
    entry,exit_t = ms(day+'T08:05:00Z'),ms(day+'T09:05:00Z')
    mid0,mid1 = number(data['trade'][entry][0]),number(data['trade'][exit_t][0])
    p0,p1 = mid0*(1+SLIP),mid1*(1-SLIP)
    funding = sum(number(rate)*number(data['mark'][t][0])/p0
                  for t,rate in data['funding'].items() if entry <= t < exit_t)
    fees = FEE*(p0+p1)/p0
    return dict(entry=entry,exit=exit_t,entry_fill=p0,exit_fill=p1,
                gross_mid=mid1/mid0-1,gross_after_slippage=p1/p0-1,
                fees=fees,funding_paid=funding,net=p1/p0-1-fees-funding)


def risk_trade(data,day,state):
    """Only candidate A holds risk. Preserve intrabar ordering uncertainty."""
    entry,exit_t = ms(day+'T08:05:00Z'),ms(day+'T09:05:00Z')
    p0 = number(data['trade'][entry][0])*(1+SLIP)
    qty = ALLOCATION*state['nav']/p0
    cash = state['nav']-qty*p0*FEE
    def observe(low,high,t):
        previous = state['peak']
        state['dd_lo'] = max(state['dd_lo'],(previous-low)/previous)
        possible_peak = max(previous,high)
        state['dd_hi'] = max(state['dd_hi'],(possible_peak-low)/possible_peak)
        state['peak'] = possible_peak
        if state['dd_lo'] >= RISK:
            return dict(decision='REJECT',reason='DEFINITE_REFERENCE_RISK_BREACH',last_bar=t)
        if state['dd_hi'] >= RISK:
            return dict(decision='INSUFFICIENT_EVIDENCE',reason='INSUFFICIENT_RISK_ORDER',last_bar=t)
        return None
    for t in range(entry,exit_t,f.STEP):
        bar = list(map(number,data['mark'][t]))
        if t in data['funding']:
            cash -= qty*number(data['funding'][t])*bar[0]
        stop = observe(cash+qty*(bar[0]-p0),cash+qty*(bar[0]-p0),t)
        if stop:
            return stop
        stop = observe(cash+qty*(bar[2]-p0),cash+qty*(bar[1]-p0),t)
        if stop:
            return stop
    p1 = number(data['trade'][exit_t][0])*(1-SLIP)
    state['nav'] = cash+qty*(p1-p0)-qty*p1*FEE
    return observe(state['nav'],state['nav'],exit_t)


def measures(rows):
    group = {k:[r['net'] for r in rows if r[k]] for k in ('A','B','C')}
    counts = {k:len(v) for k,v in group.items()}
    means = {k:sum(v)/len(v) if v else None for k,v in group.items()}
    out = None if any(v is None for v in means.values()) else (
        means['A'],means['A']-means['B'],means['A']-means['C'])
    return counts,means,out


def statistics(rows,deadline=math.inf):
    counts,means,point = measures(rows)
    result = dict(counts=counts,valid_days=sum(r['valid'] for r in rows),means=means,
                  point_measures=point,bootstrap=[])
    if result['valid_days'] < 30 or min(counts.values()) < 8:
        return result | dict(decision='INSUFFICIENT_EVIDENCE',reason='INSUFFICIENT_SAMPLE')
    n = len(rows)
    for block in BLOCKS:
        rng = random.Random(SEED+block)
        draws = [[],[],[]]
        for rep in range(REPS):
            if rep%256 == 0:
                need(time.monotonic() < deadline, 'COMPUTE_BUDGET')
            indices = []
            while len(indices) < n:
                start = rng.randrange(n)
                indices.extend((start+j)%n for j in range(min(block,n-len(indices))))
            _,_,m = measures([rows[i] for i in indices])
            if m is not None:
                for target,value in zip(draws,m):
                    target.append(value)
        result['bootstrap'].append(dict(block_months=block,valid_draws=len(draws[0]),
            bounds=[dict(low=quantile(v,ALPHA),high=quantile(v,1-ALPHA)) for v in draws] if draws[0] else []))
    if any(r['valid_draws'] < 9900 for r in result['bootstrap']):
        return result | dict(decision='INSUFFICIENT_EVIDENCE',reason='INSUFFICIENT_BOOTSTRAP_GROUPS')
    bounds = [b for r in result['bootstrap'] for b in r['bounds']]
    if any(b['high'] <= 0 for b in bounds):
        decision,reason = 'REJECT','NONPOSITIVE_NET_OR_INCREMENT_UPPER_BOUND'
    elif all(b['low'] > 0 for b in bounds):
        decision,reason = 'WORTH_FURTHER_REVIEW','NET_AND_BOTH_INCREMENT_LOWER_BOUNDS_POSITIVE'
    else:
        decision,reason = 'INSUFFICIENT_EVIDENCE','NET_OR_INCREMENT_LOWER_BOUND_NOT_POSITIVE'
    return result | dict(decision=decision,reason=reason)


def inputs():
    bound()
    c = strict((f.RUN/'collection.json').read_bytes())
    need(all(sha((f.RUN/p).read_bytes()) == h for p,h in c['input_sha256'].items()), 'INPUT_CHANGED')
    need(len(c['quality']) == 45 and c['public_gets'] <= 200, 'COLLECTION_CONTRACT')
    meta = f.metadata(f.read_page('metadata'))
    return c,meta


def screen():
    c,meta = inputs()
    save_new(f.RUN/'experiment-start.json',dict(started_utc=utc(time.time()*1000),
        source_freeze_sha256=sha((f.RUN/'economy-freeze.json').read_bytes()),
        collection_sha256=sha((f.RUN/'collection.json').read_bytes()),experiments=1))
    deadline = time.monotonic()+1800
    rows,state = [],dict(nav=1.,peak=1.,dd_lo=0.,dd_hi=0.)
    stop = None
    for day in f.DATES:
        need(time.monotonic() < deadline, 'COMPUTE_BUDGET')
        data = load_day(day,meta)
        s = signal(data,day)
        if s['A']:
            stop = risk_trade(data,day,state)
            if stop:
                break
        r = unit_return(data,day) if s['valid'] else {'net':None}
        rows.append(dict(date=day,**s,**r))
    result = dict(rows=rows,reference=state,full_window_evaluated=stop is None,
                  final_date=day,public_gets=c['public_gets'],economic_experiments=1,
                  profitability_qualified=False,promotion_authority=False,stop_fill_simulated=False)
    result.update(stop if stop else statistics(rows,deadline))
    save_new(f.RUN/'result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k != 'rows'}),flush=True)


def audit():
    """Independently reconstruct raw group signs and Decimal cash; never rerun screen."""
    c,meta = inputs()
    result = strict((f.RUN/'result.json').read_bytes())
    D = lambda x:Decimal(str(x))
    nav=peak=D(1); ddlo=ddhi=D(0)
    audit_rows=[]; stopped=None
    for day in f.DATES:
        data = load_day(day,meta)
        op = data['option']
        usable = bool(op['quality'] and not data['incident_overlap'] and op['calls'])
        A=B=C=False
        if usable:
            imbalance = sum((D(r['amount'])*(1 if r['side']=='sell' else -1) for r in op['calls']),D(0))
            down = D(data['trade'][ms(day+'T07:55:00Z')][0]) < D(data['trade'][ms(day+'T07:30:00Z')][0])
            A,B = imbalance > 0 and down, imbalance <= 0 and down
            C = not A
        begin,end=ms(day+'T08:05:00Z'),ms(day+'T09:05:00Z')
        net=None
        if usable:
            buy=D(data['trade'][begin][0])*D('1.0005')
            sell=D(data['trade'][end][0])*D('0.9995')
            paid=sum((D(rate)*D(data['mark'][t][0]) for t,rate in data['funding'].items() if begin<=t<end),D(0))
            net=((sell-buy)-(buy+sell)*D('0.0006')-paid)/buy
        if A:
            qty=nav*D('0.25')/buy
            balance=nav-qty*buy*D('0.0006')
            observations=[]
            for t in range(begin,end,f.STEP):
                opn,high,low,close=map(D,data['mark'][t])
                balance-=qty*D(data['funding'].get(t,'0'))*opn
                observations.extend([(t,balance+qty*(opn-buy),balance+qty*(opn-buy)),
                                     (t,balance+qty*(low-buy),balance+qty*(high-buy))])
            closed=balance+qty*(sell-buy)-qty*sell*D('0.0006')
            observations.append((end,closed,closed))
            for t,lo,hi in observations:
                ddlo=max(ddlo,1-lo/peak)
                possible=max(peak,hi)
                ddhi=max(ddhi,1-lo/possible);peak=possible
                if ddlo>=D('.08') or ddhi>=D('.08'):
                    if t == end:
                        nav=closed
                    stopped=dict(date=day,last_bar=t,reason='DEFINITE_REFERENCE_RISK_BREACH' if ddlo>=D('.08') else 'INSUFFICIENT_RISK_ORDER')
                    break
            if stopped:
                break
            need(abs(closed-nav*(1+D('.25')*net)) < D('1e-22'), 'AUDIT_COMPOUND_IDENTITY')
            nav=closed
        audit_rows.append(dict(date=day,valid=usable,A=A,B=B,C=C,net=None if net is None else float(net)))
    need(len(result['rows']) == len(audit_rows), 'AUDIT_ROW_COUNT')
    for expected,actual in zip(audit_rows,result['rows']):
        need(all(expected[k] == actual[k] for k in ('date','valid','A','B','C')), 'AUDIT_GROUPS')
        need(expected['net'] is None and actual['net'] is None or expected['net'] is not None and
             abs(expected['net']-actual['net']) < 1e-12, 'AUDIT_UNIT_CASH')
    for key,value in (('nav',nav),('peak',peak),('dd_lo',ddlo),('dd_hi',ddhi)):
        need(abs(D(result['reference'][key])-value) < D('1e-11'), 'AUDIT_REFERENCE_'+key)
    need((stopped is None) == result['full_window_evaluated'], 'AUDIT_RISK_STOP')
    if stopped:
        need(result['reason'] == stopped['reason'] and result['last_bar'] == stopped['last_bar'], 'AUDIT_STOP_IDENTITY')
        stats_audit='NOT_APPLICABLE_RISK_STOP'
    else:
        stats_audit=audit_statistics(audit_rows,result)
    save_new(f.RUN/'audit.json',dict(status='PASS',method='independent Decimal raw-sign cash and mark-order reconstruction',
        result_sha256=sha((f.RUN/'result.json').read_bytes()),statistics=stats_audit,
        source_gate_sha256=f.old_hashes(),rows_checked=len(audit_rows),strategy_rerun=False))
    print(json.dumps(dict(audit='PASS',rows_checked=len(audit_rows),statistics=stats_audit)),flush=True)


def audit_statistics(rows,result):
    """Independent group prefix-sum bootstrap and quantile interpolation."""
    counts={k:sum(r[k] for r in rows) for k in ('A','B','C')}
    valid=sum(r['valid'] for r in rows)
    need(counts==result['counts'] and valid==result['valid_days'], 'AUDIT_SAMPLE_COUNTS')
    for k in ('A','B','C'):
        values=[Decimal(str(r['net'])) for r in rows if r[k]]
        mean=float(sum(values)/len(values)) if values else None
        need(mean is None and result['means'][k] is None or mean is not None and
             abs(mean-result['means'][k]) < 1e-12, 'AUDIT_GROUP_MEANS')
    if valid < 30 or min(counts.values()) < 8:
        need(result['reason']=='INSUFFICIENT_SAMPLE' and result['decision']=='INSUFFICIENT_EVIDENCE' and
             not result['bootstrap'], 'AUDIT_SAMPLE_EXIT')
        return 'PASS_SAMPLE_FLOOR'
    n=len(rows)
    need(len(result['bootstrap'])==2, 'AUDIT_BOOTSTRAP_BLOCKS')
    for actual,block in zip(result['bootstrap'],(3,6)):
        rng=random.Random(20260928+block)
        prefixes={}
        for k in ('A','B','C'):
            count=[0]; sums=[0.]
            for r in rows+rows:
                count.append(count[-1]+r[k]);sums.append(sums[-1]+(r['net'] if r[k] else 0))
            prefixes[k]=(count,sums)
        draws=[[],[],[]]
        for _ in range(10000):
            spans=[(rng.randrange(n),min(block,n-o)) for o in range(0,n,block)]
            means=[]
            for k in ('A','B','C'):
                cp,sp=prefixes[k]; count=sum(cp[i+j]-cp[i] for i,j in spans)
                means.append(sum(sp[i+j]-sp[i] for i,j in spans)/count if count else None)
            if all(x is not None for x in means):
                for series,value in zip(draws,(means[0],means[0]-means[1],means[0]-means[2])):
                    series.append(value)
        need(len(draws[0])==actual['valid_draws'] and block==actual['block_months'], 'AUDIT_RESAMPLES')
        need(len(actual['bounds'])==(3 if draws[0] else 0), 'AUDIT_BOUND_SHAPE')
        for series,b in zip(draws,actual['bounds']):
            series.sort()
            for key,q in (('low',.05/6),('high',1-.05/6)):
                ix=(len(series)-1)*q; left=math.floor(ix); right=math.ceil(ix)
                v=series[left]*(right-ix)+series[right]*(ix-left) if left!=right else series[left]
                need(abs(v-b[key]) < 1e-11, 'AUDIT_QUANTILE')
    bounds=[b for x in result['bootstrap'] for b in x['bounds']]
    expected=('INSUFFICIENT_EVIDENCE' if any(x['valid_draws']<9900 for x in result['bootstrap']) else
              'REJECT' if any(b['high']<=0 for b in bounds) else
              'WORTH_FURTHER_REVIEW' if all(b['low']>0 for b in bounds) else 'INSUFFICIENT_EVIDENCE')
    need(result['decision']==expected, 'AUDIT_STATISTICAL_DECISION')
    return 'PASS_INDEPENDENT_PREFIX_BOOTSTRAP'


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=('freeze','collect','screen','audit'))
    args=p.parse_args()
    {'freeze':freeze,'collect':collect,'screen':screen,'audit':audit}[args.command]()


if __name__ == '__main__':
    main()
