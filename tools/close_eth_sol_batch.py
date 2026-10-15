"""Summarize only the consumed baseline prefix; no new strategy or outcome run."""
from collections import deque
from decimal import Decimal
import datetime as dt
import json
import math

import eth_sol_intake as intake
from mvp_reference_inputs import strict_json
from run_eth_sol_baseline import Archive, guard, ROOT


def main():
    guard();intake.preserve()
    r=strict_json((ROOT/'baseline-result.json').read_bytes())
    a=strict_json((ROOT/'baseline-audit.json').read_bytes())
    intake.need(a['status']=='INDEPENDENT_CASH_AUDIT_PASS' and a['result_sha256']==intake.sha(ROOT/'baseline-result.json'),'CLOSURE_AUDIT_REQUIRED')
    intake.need(not r['passed_to_learning'] and r['status']=='REJECT_RISK_OR_SAFETY','CLOSURE_ONLY_KNOWN_NEGATIVE_EXIT')
    archive=Archive();hours=deque(archive.warmup,maxlen=1440);fits=[]
    # Diagnose only fits actually available before the stop; never consume the later outcome domain.
    for minute in range(0,r['processed_minutes'],60):
        if minute:
            hours.append(tuple(b[3] for b in archive.bars(r['formation_start_ms']+(minute-1)*60000,'trade')))
        if minute%1440:continue
        x=[math.log(p[1]) for p in hours];y=[math.log(p[0]) for p in hours]
        xm,ym=math.fsum(x)/1440,math.fsum(y)/1440
        variance=math.fsum((v-xm)**2 for v in x)
        beta=math.fsum((u-xm)*(v-ym) for u,v in zip(x,y))/variance
        alpha=ym-beta*xm;sigma=math.sqrt(math.fsum((v-alpha-beta*u)**2 for u,v in zip(x,y))/1440)
        fits.append(dict(minute=minute,beta=beta,sigma=sigma,eligible=.25<=beta<=4 and sigma>=.000001))
    arms={}
    for name in ('base','stress'):
        events=strict_json((ROOT/f'baseline-{name}-events.json').read_bytes())
        fills=[e for e in events if e['kind']=='fill'];v=r[name]
        d=lambda value:Decimal(str(value))
        gross=-sum(d(e['delta'])*d(e['bar'][0]) for e in fills)
        adverse=sum(d(e['delta'])*(d(e['price'])-d(e['bar'][0])) for e in fills)
        fees=d(a['audits'][name]['fees']);funding=d(a['audits'][name]['funding'])
        net=d(a['audits'][name]['cash'])-10000
        intake.need(abs(gross-adverse-fees-funding-net)<=d('0.0000001'),'PNL_COMPONENT_IDENTITY')
        arms[name]=dict(reference_net_return=float(net/10000),reference_drawdown=v['maximum_drawdown'],
            gross_price_pnl=str(gross),adverse_execution_cost=str(adverse),fees=str(fees),funding_paid=str(funding),
            net_pnl=str(net),fills=len(fills),completed_episodes=v['completed_episodes'],
            partial_exit_fills=sum(abs(e['delta'])<abs(e['requested_delta']) for e in fills),
            first_fill_minute=fills[0]['minute'],last_fill_minute=fills[-1]['minute'],
            terminal_positions=v['positions'],stop=v['stop'],last_three_safety_checks=v['safety_checks'][-3:])
    summary=dict(status='CLOSED_NO_GO',reason=r['status'],g0='DEVELOPMENT_INPUT_AND_CONTRACT_PASSED',
        g1='REFERENCE_EXECUTION_AND_INDEPENDENT_ACCOUNTING_PASSED_NOT_ACTUAL_FILL_CERTIFICATION',
        baseline='REJECTED_BY_ORIGINAL_SAFETY',g2='NOT_STARTED',g3='NOT_STARTED',g4='NOT_ACHIEVED',
        formation_minutes_evaluated=r['processed_minutes'],formation_minutes_requested=r['requested_minutes'],
        stop_utc=dt.datetime.fromtimestamp((r['formation_start_ms']+r['stop_minute']*60000)/1000,dt.timezone.utc).isoformat(),
        arms=arms,fit_diagnostics=dict(daily_fits=len(fits),eligible_fits=sum(v['eligible'] for v in fits),
            ineligible_beta=sum(not .25<=v['beta']<=4 for v in fits),first_eligible_minute=next((v['minute'] for v in fits if v['eligible']),None)),
        fixed_baseline_vectors=1,learned_vectors=0,development_comparisons=0,confirmation_attempts=0,
        candidate_count=0,forward_scheduled=False,account_requests=0,orders=0,demo_unlock=False,
        used_prefix_only=True,original_safety_and_costs_unchanged=True,reference_not_actual_pnl=True,
        protected_files=intake.preserve(),result_sha256=intake.sha(ROOT/'baseline-result.json'),
        audit_sha256=intake.sha(ROOT/'baseline-audit.json'),summary_source_sha256=intake.sha(__file__))
    intake.save(ROOT/'closed.json',summary)
    print(json.dumps(summary,sort_keys=True),flush=True)


if __name__=='__main__':main()
