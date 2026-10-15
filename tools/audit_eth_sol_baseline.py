"""Independent Decimal reconstruction; no re-running or re-selecting economics."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from collections import defaultdict
import json

import eth_sol_intake as intake
from mvp_reference_inputs import strict_json

D=lambda value:Decimal(str(value))


def check(ok,reason):
    if not ok:raise ValueError(reason)


def close(a,b):return abs(D(a)-D(b))<=Decimal('0.0000001')


def audit_events(events,result,stress):
    cash=Decimal(10000);qty=[Decimal(0),Decimal(0)];fees=Decimal(0);funding=Decimal(0);traded=Decimal(0)
    step=[D('.01'),D('.1')];fee_rate=D('.0011' if stress else '.00055');slip=D('.0004' if stress else '.0002')
    fills=0;last=-1;entries={};completed=0;episode_full=False
    for e in events:
        check(type(e['minute']) is int and e['minute']>=last,'EVENT_TIME_ORDER');last=e['minute']
        kind=e['kind']
        if kind=='entry_requested':
            check(not any(qty),'ENTRY_OVER_EXISTING_POSITION');entries={0:last+1,1:last+2};episode_full=False
        elif kind=='fill':
            leg=e['leg'];check(leg in (0,1),'LEG_IDENTITY')
            delta,price,fee=D(e['delta']),D(e['price']),D(e['fee']);bar=list(map(D,e['bar']))
            check(delta and delta%step[leg]==0,'QUANTITY_GRID')
            check(abs(delta)<=abs(D(e['requested_delta'])),'OVERFILL')
            cap=(bar[4]*D('.01')/step[leg]).to_integral_value(rounding=ROUND_FLOOR)*step[leg]
            check(abs(delta)<=cap,'VOLUME_REJECTION_BOUND')
            if e['phase']=='entry':check(entries.get(leg)==last,'SEQUENTIAL_ENTRY_CLOCK')
            else:check(qty[leg]*delta<0 and abs(delta)<=abs(qty[leg]),'EXIT_INCREASED_EXPOSURE')
            raw=bar[1 if delta>0 else 2] if stress else bar[0]
            px=raw*(1+slip if delta>0 else 1-slip)
            px=(px/D('.01')).to_integral_value(rounding=ROUND_CEILING if delta>0 else ROUND_FLOOR)*D('.01')
            check(price==px,'ADVERSE_PRICE_RULE')
            expected_fee=abs(delta)*price*fee_rate;check(close(fee,expected_fee),'FEE_RULE')
            cash-=delta*price+expected_fee;qty[leg]+=delta;fees+=expected_fee;traded+=abs(delta)*price;fills+=1
            check(close(qty[leg],e['quantity_after']) and close(cash,e['cash']),'FILL_CASH_OR_QUANTITY')
            if e['phase']=='entry' and leg==1 and close(delta,e['requested_delta']):episode_full=True
        elif kind=='funding':
            leg=e['leg'];check(close(qty[leg],e['quantity']),'FUNDING_POSITION')
            amount=qty[leg]*D(e['mark'])*D(e['rate']);cash-=amount;funding+=amount
            check(close(amount,e['amount']) and close(cash,e['cash']),'FUNDING_AMOUNT_OR_CASH')
        elif kind=='flat':
            check(not any(qty),'FALSE_FLAT');check(close(cash,e['cash']),'FLAT_CASH')
            check(e['completed_episode']==episode_full,'EPISODE_COMPLETION')
            if episode_full:completed+=1
            episode_full=False
    check(close(cash,result['cash']) and all(close(q,result['positions'][i]) for i,q in enumerate(qty)), 'TERMINAL_LEDGER')
    check(close(fees,result['fees']) and close(funding,result['funding']) and close(traded,result['traded_notional']), 'TERMINAL_COSTS')
    check(completed==result['completed_episodes'],'COMPLETED_EPISODE_COUNT')
    return dict(status='DECIMAL_CASH_AUDIT_PASS',fills=fills,cash=str(cash),fees=str(fees),funding=str(funding),
        positions=list(map(str,qty)),completed_episodes=completed)


def audit_marked_risk(archive,events,result,whole,stress):
    """Rebuild marked risk from frozen prices and cash events, not model risk()."""
    groups=defaultdict(list)
    for event in events:groups[event['minute']].append(event)
    cash=Decimal(10000);qty=[Decimal(0),Decimal(0)];peak=Decimal(10000);drawdown=Decimal(0)
    fee=D('.0011' if stress else '.00055');slip=D('.0004' if stress else '.0002')
    last_sample=Decimal(10000);last_close=Decimal(10000);window_sum=Decimal(0);count=0;streak=0
    checks=[];already_stopped=False;observations=0
    def liquid(prices):
        return cash+sum(q*D(p) for q,p in zip(qty,prices))-sum(abs(q)*(D(p)*(fee+slip)+D('.01')) for q,p in zip(qty,prices))
    def sample(minute):
        nonlocal last_sample,window_sum,count,streak,observations
        if not already_stopped:
            window_sum+=last_close-last_sample;count+=1;observations+=1
            if count==240:
                streak=streak+1 if window_sum<0 else 0
                checks.append(dict(minute=minute,net=window_sum,streak=streak))
                window_sum=Decimal(0);count=0
        last_sample=last_close
    for minute in range(whole['processed_minutes']):
        if minute and minute%5==0:sample(minute)
        mark=archive.bars(whole['formation_start_ms']+minute*60000,'mark')
        for e in groups[minute]:
            if e['kind']=='funding':cash-=qty[e['leg']]*D(e['mark'])*D(e['rate'])
        for stage in ('before','after'):
            if stage=='after':
                for e in groups[minute]:
                    if e['kind']=='fill':
                        delta,price=D(e['delta']),D(e['price']);cash-=delta*price+abs(delta)*price*fee;qty[e['leg']]+=delta
            favorable=[b[1] if q>=0 else b[2] for q,b in zip(qty,mark)]
            adverse=[b[2] if q>=0 else b[1] for q,b in zip(qty,mark)]
            peak=max(peak,liquid(favorable));drawdown=max(drawdown,(peak-liquid(adverse))/peak)
        last_close=liquid([b[3] for b in mark]);peak=max(peak,last_close);drawdown=max(drawdown,(peak-last_close)/peak)
        if any(e['kind']=='stop' for e in groups[minute]):already_stopped=True
    if whole['processed_minutes']==whole['requested_minutes'] and whole['processed_minutes']%5==0:sample(whole['processed_minutes'])
    check(abs(drawdown-D(result['maximum_drawdown']))<=D('0.000000001'),'INDEPENDENT_DRAWDOWN')
    check(observations==result['observed_five_minute_intervals'],'INDEPENDENT_SAFETY_OBSERVATIONS')
    check(len(checks)==len(result['safety_checks']),'INDEPENDENT_SAFETY_WINDOWS')
    for a,b in zip(checks,result['safety_checks']):
        check(a['minute']==b['minute'] and close(a['net'],b['net_by_decision_bucket_and_total'][1]) and
            close(a['net'],b['net_by_decision_bucket_and_total'][3]) and a['streak']==b['streak'][1]==b['streak'][3], 'INDEPENDENT_SAFETY_RECEIPTS')
    return dict(status='DECIMAL_MARK_AND_SAFETY_AUDIT_PASS',drawdown=str(drawdown),terminal_liquidation=str(last_close),
        safety_windows=len(checks),five_minute_observations=observations)


def main():
    from run_eth_sol_baseline import guard, ROOT, Archive
    guard();intake.preserve()
    r=strict_json((ROOT/'baseline-result.json').read_bytes())
    check(all(intake.sha(p)==h for p,h in r['event_hashes'].items()),'EVENT_HASH_CHANGED')
    audits={};archive=Archive()
    for name,stress in [('base',False),('stress',True)]:
        events=strict_json((ROOT/f'baseline-{name}-events.json').read_bytes())
        audits[name]=audit_events(events,r[name],stress)
        audits[name]['marked_risk']=audit_marked_risk(archive,events,r[name],r,stress)
    definite_risk=any(r[n]['stop'] for n in ('base','stress'))
    check(not r['passed_to_learning'] or (not definite_risk and r['processed_minutes']==r['requested_minutes']
        and all(r[n]['cash']>10000 and r[n]['completed_episodes']>0 and not r[n]['insufficient'] for n in ('base','stress'))), 'BASELINE_ADMISSION')
    intake.save(ROOT/'baseline-audit.json',dict(status='INDEPENDENT_CASH_AUDIT_PASS',audits=audits,
        economic_status=r['status'],passed_to_learning=r['passed_to_learning'],definite_risk_failure_preserved=definite_risk,
        actual_execution_verified=False,protected_files=intake.preserve(),result_sha256=intake.sha(ROOT/'baseline-result.json')))
    print(json.dumps(dict(status='INDEPENDENT_CASH_AUDIT_PASS',economic_status=r['status'],passed_to_learning=r['passed_to_learning'])),flush=True)


if __name__=='__main__':main()
