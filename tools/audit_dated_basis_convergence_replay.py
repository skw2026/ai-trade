"""Independent 40-digit reconstruction including a derived early-exit clock.

Does not import the execution engine, its quote projection or cash formulas.
Audits first opportunity, missing-data control, settlement tail, fees and the
carried portfolio. It is not a second economic selection attempt.
"""
from decimal import Decimal as D, localcontext
import itertools
import math

STEP=300000
DAY=86400000
N=D(2500)


def valid(b,t,need_trade=True):
    if b is None or b.get('complete') is not True or b.get('close_ms') != t+STEP-1:
        return False
    o,h,l,c,v=[D(str(b[k])) for k in ('o','h','l','c','v')]
    if not all(x.is_finite() for x in (o,h,l,c,v)) or min(o,h,l,c) <= 0 or v < 0 or not l <= min(o,c) <= max(o,c) <= h:
        raise ValueError('AUDIT_BAD_BAR')
    return not need_trade or v>0 and b['n']>0


def select(quarter):
    """Independently derive the first run, not a run chosen by the result file."""
    with localcontext() as ctx:
        ctx.prec=40
        series=quarter['series']; expiry=quarter['expiry']; streak=0
        for t in range(expiry-90*DAY-STEP,expiry-DAY,STEP):
            bars=[series[k].get(str(t),series[k].get(t)) for k in ('spot','future')]
            if not all(valid(b,t) for b in bars):
                streak=0
                continue
            s,f=(D(str(b['c'])) for b in bars)
            q=N/(f*D('.999'))
            # Algebraically reduced wallet, independent of Position's transfer form.
            cash=N*D('.999')/(s*D('1.0025'))*s*D('.999')*D('.998')-q*D('.001')*s*D('.999')*D('.998')-q*s*D('1.001')*D('1.002')
            streak=streak+1 if cash > D('1e-9') else 0
            if streak==12:
                return dict(decision_bar=t,available_ms=t+STEP,earliest_entry_ms=t+2*STEP,
                            forecast_cash=cash,policy='FIRST_12_STRESS_CASH_POSITIVE',actual_execution_qualified=False)
        return None


def state(equity=D(10000),peak=D(10000),halted=False,uncertain=False):
    return dict(equity=equity,peak=peak,halted=halted,uncertain=uncertain)


def reference_path(quarter,scenario,intention,initial=None):
    """Pure Decimal replay. No result-derived timing, prices or account inputs."""
    with localcontext() as ctx:
        ctx.prec=40
        return _path(quarter,scenario,intention,state() if initial is None else initial)


def _path(quarter,sc,intention,initial):
    common=dict(initial_state=initial,actual_execution_qualified=False,candidate_qualified=False,g4=False)
    if initial['halted'] or initial['uncertain']:
        return dict(common,status='BLOCKED_PRIOR_STATE',cash_state=initial,cash_net=D(0))
    if intention is None:
        return dict(common,status='NO_INTENTION',cash_state=initial,cash_net=D(0))
    start=intention['earliest_entry_ms']; settle=quarter['expiry']+(sc['hour']-8)*3600000
    planned=settle+sc['delay_minutes']*60000; end=quarter['expiry']+3600000
    common.update(entry_ms=start,settlement_ms=settle,planned_exit_ms=planned,
                  last_exit_attempt_ms=end,decision_bar=intention['decision_bar'])
    series={k:{int(t):b for t,b in rows.items()} for k,rows in quarter['series'].items()}
    def good(k,t): return valid(series[k].get(t),t,k!='mark')
    def px(k,t,f): return D(str(series[k][t][f]))
    if not good('spot',start) or not good('future',start):
        return dict(common,status='ENTRY_UNVERIFIABLE',cash_state=dict(initial,uncertain=True),
                    cash_net=D(0),risk_status='UNKNOWN',first_unknown_ms=start)
    sf,ff,z=map(D,('.001','.0005','.0002') if sc['cost']=='base' else ('.002','.001','.001'))
    s0,f0=px('spot',start,'o'),px('future',start,'o')
    q=N/(f0*(1-z)); spent=q*s0*(1+z)*(1+sf); balance=initial['equity']-spent
    assert balance>=0
    index=D(str(quarter['settlement']))*(1+D(sc['bias_bps'])/10000)
    peak=initial['peak']; dd=D(0); margin_min=None; reason=None; pending=None
    uncertain=False; delivered=False; event=None; checked=tail=missing=unavailable=0
    first_unknown=trigger=None; unknown_reasons={}
    convergence_bar=convergence_ms=None
    def cash(coins,s):
        return balance+coins*s*((1-z)*(1-sf) if coins>=0 else (1+z)*(1+sf))
    def observe(values,margin,available):
        nonlocal peak,dd,margin_min,reason,pending,trigger
        peak=max([peak]+values); dd=max(dd,(peak-min(values))/peak)
        if margin is not None:
            margin_min=margin if margin_min is None else min(margin_min,margin)
        if reason is None and (dd>=D('.08') or margin is not None and margin<=2):
            reason='DRAWDOWN_8_PERCENT' if dd>=D('.08') else 'MARGIN_BUFFER'
            pending=available
            trigger=available
    def unknown(t,reasons):
        nonlocal first_unknown,uncertain
        uncertain=True
        if first_unknown is None: first_unknown=t
        for r in reasons: unknown_reasons[r]=unknown_reasons.get(r,0)+1
    for t in range(start,end+STEP,STEP):
        if t>=settle and not delivered:
            event=dict(time_ms=t,index=index,fee_btc=N*ff/index,
                       coins_before_later_liabilities=N*(1-ff)/index-q*ff)
            delivered=True
        due=(delivered and t>=planned or pending is not None and t>=pending
             or convergence_ms is not None and t>=convergence_ms)
        if due:
            if good('spot',t) and (delivered or good('future',t)):
                sout=px('spot',t,'o'); raw_f=index if delivered else px('future',t,'o')
                f=raw_f if delivered else raw_f*(1+z)
                coins=N*(1-ff)/f-q*ff
                final=cash(coins,sout); observe([final],None,t)
                sx=sout*(1-z if coins>=0 else 1+z)
                gross=N*sout/raw_f-N*s0/f0
                nofees=N*sx/f-N*s0*(1+z)/(f0*(1-z))
                fees=q*s0*(1+z)*sf+abs(coins)*sx*sf+(q*ff+N*ff/f)*sx
                net=final-initial['equity']
                assert abs(net-(nofees-fees))<D('1e-30')
                charges={str(r):spent*r*D(t-start)/D(31536000000) for r in (D('.06'),D('.12'))}
                return dict(common,status='CLOSED',cash_state=state(final,peak,reason is not None,uncertain),
                    cash_net=net,gross=gross,slippage_drag=gross-nofees,execution_fees=fees,financing_cost=D(0),
                    benchmark_charges=charges,benchmark_excess={r:net-v for r,v in charges.items()},
                    cash_drawdown=dd,minimum_margin_ratio=margin_min,risk_reason=reason,
                    exit_ms=t,spot_spend=spent,risk_status='UNKNOWN' if uncertain else 'MODEL_OBSERVED_ONLY',
                    exit_spot=sout,exit_future=raw_f,settled=delivered,settlement_event=event,
                    risk_bar_count=checked,tail_bar_count=tail,missing_bar_count=missing,
                    unavailable_exit_count=unavailable,first_unknown_ms=first_unknown,
                    first_risk_trigger_ms=trigger,unknown_reasons=unknown_reasons,
                    convergence_decision_bar=convergence_bar,convergence_earliest_exit_ms=convergence_ms)
            unavailable+=1; unknown(t,['EXIT_PROXY_UNAVAILABLE'])
        if t==end: break
        kinds=('spot',) if delivered else ('spot','mark')
        bad=[k for k in kinds if not valid(series[k].get(t),t,need_trade=False)]
        if bad:
            missing+=1; unknown(t,[k.upper()+'_VALUATION_UNOBSERVED' for k in bad])
            if pending is None: pending=t+STEP
        elif delivered:
            observe([cash(event['coins_before_later_liabilities'],px('spot',t,k)) for k in ('l','h')],None,t+STEP)
            checked+=1; tail+=1
        else:
            marks=[px('mark',t,k) for k in ('l','h')]
            ratios=[20*(1-q*ff*m/N) for m in marks]
            values=[cash(N*(1-ff)/(m*(1+z))-q*ff,px('spot',t,k)) for m in marks for k in ('l','h')]
            observe(values,min(ratios),t+STEP); checked+=1
        if (not delivered and convergence_ms is None and good("spot",t) and good("future",t)
                and px("future",t,"c")*2002 <= px("spot",t,"c")*2005):
            convergence_bar=t; convergence_ms=t+2*STEP
    return dict(common,status='UNRESOLVED',cash_state=None,cash_net=None,risk_status='UNKNOWN',
                settled=delivered,settlement_event=event,exit_ms=None,cash_drawdown=dd,
                minimum_margin_ratio=margin_min,risk_reason=reason,first_risk_trigger_ms=trigger,
                risk_bar_count=checked,tail_bar_count=tail,missing_bar_count=missing,
                unavailable_exit_count=unavailable,first_unknown_ms=first_unknown,unknown_reasons=unknown_reasons,
                    convergence_decision_bar=convergence_bar,convergence_earliest_exit_ms=convergence_ms)


def equal(expected,actual,where='root'):
    if isinstance(expected,dict):
        assert set(expected)==set(actual),(where,'KEYS',set(expected)^set(actual))
        for k,v in expected.items(): equal(v,actual[k],where+'.'+str(k))
    elif isinstance(expected,list):
        assert len(expected)==len(actual),where
        for i,(a,b) in enumerate(zip(expected,actual)): equal(a,b,where+f'[{i}]')
    elif isinstance(expected,D):
        assert type(actual) in (int,float) and math.isfinite(actual),(where,actual)
        assert math.isclose(float(expected),actual,rel_tol=1e-10,abs_tol=1e-8),(where,str(expected),actual)
    else:
        assert type(expected)==type(actual) and expected==actual,(where,expected,actual)


def verify(bundle,result):
    with localcontext() as ctx:
        ctx.prec=40
        mask={k:select(v) for k,v in bundle.items()}
        equal(mask,result['intentions'],'intentions')
        scenarios=[dict(id=f'{c}_h{h}_b{b}_m{m}',cost=c,hour=h,bias_bps=b,delay_minutes=m)
                   for c,h,b,m in itertools.product(('base','stress'),(0,8),(-25,0,25),(0,5,60))]
        assert len(result['portfolios'])==36
        path_count=risk_count=tail_count=closed=0
        rebuilt=[]
        for sc,actual in zip(scenarios,result['portfolios']):
            assert sc==actual['scenario']
            account=state(); unresolved=False; paths={}
            for symbol,quarter in sorted(bundle.items(),key=lambda x:x[1]['expiry']):
                if unresolved:
                    expected=dict(status='BLOCKED_UNRESOLVED',cash_state=None,cash_net=None)
                else:
                    expected=_path(quarter,sc,mask[symbol],account)
                    if expected['cash_state'] is None: unresolved=True
                    else: account=expected['cash_state']
                paths[symbol]=expected
                equal(expected,actual['paths'][symbol],sc['id']+'.'+symbol)
                path_count+=1; risk_count+=expected.get('risk_bar_count',0); tail_count+=expected.get('tail_bar_count',0)
                closed+=expected['status']=='CLOSED'
            done=[p for p in paths.values() if p['status']=='CLOSED']
            pnl=None if unresolved else account['equity']-10000
            expected=dict(scenario=sc,paths=paths,cash_net=pnl,cash_return_pct=None if pnl is None else pnl/100,
                          end_state=None if unresolved else account,closed_quarters=len(done),unresolved=unresolved,
                          observed_cash_drawdown=max((p.get('cash_drawdown',D(0)) for p in paths.values()),default=D(0)),
                          without_best_quarter=None if not done or unresolved else sum(p['cash_net'] for p in done)-max(p['cash_net'] for p in done),
                          benchmark_excess={r:None if unresolved else sum((p['benchmark_excess'][r] for p in done),D(0)) for r in ('0.06','0.12')},
                          gross=sum((p['gross'] for p in done),D(0)),slippage_drag=sum((p['slippage_drag'] for p in done),D(0)),
                          execution_fees=sum((p['execution_fees'] for p in done),D(0)),halted=account['halted'],uncertain=account['uncertain'] or unresolved)
            equal(expected,actual,sc['id'])
            rebuilt.append(expected)
        by_id={p['scenario']['id']:p for p in rebuilt}
        base=by_id['base_h8_b0_m0']; central=by_id['stress_h8_b0_m0']
        pressure=[p for p in rebuilt if p['scenario']['cost']=='stress']
        code='NET_UNSUPPORTED'
        if not any(mask.values()): code='NO_TRADES'
        elif any(p['uncertain'] or p['unresolved'] for p in rebuilt): code='DATA_INSUFFICIENT'
        elif any(p['halted'] for p in [base]+pressure): code='RISK_STOP'
        elif (base['cash_net']>0 and all(p['cash_net']>0 for p in pressure)
              and min(base['closed_quarters'],central['closed_quarters'])>=2
              and base['without_best_quarter']>0 and central['without_best_quarter']>0):
            code='CONDITIONAL_DEVELOPMENT_SUPPORTED'
        pnls=[p['cash_net'] for p in pressure if p['cash_net'] is not None]
        summary=dict(status=code,base_central_cash=base['cash_net'],stress_central_cash=central['cash_net'],
                     pressure_cash_min=min(pnls,default=None),pressure_cash_max=max(pnls,default=None),
                     base_closed_quarters=base['closed_quarters'],stress_closed_quarters=central['closed_quarters'],
                     uncertain_scenarios=sum(p['uncertain'] for p in rebuilt),halted_scenarios=sum(p['halted'] for p in rebuilt),
                     unresolved_scenarios=sum(p['unresolved'] for p in rebuilt),policy_count=1,economic_batch_count=1,
                     training_vectors=0,conditional_model=True,actual_execution_qualified=False,
                     exit_policy="FIRST_CONVERGENCE_CASH_COMPARISON",independent_confirmation_attempts=0,candidate_qualified=False,g4=False)
        equal(summary,result['summary'],'summary')
        return dict(status='DECIMAL_RECONSTRUCTION_MATCH',scenarios=36,quarter_paths=path_count,
                    closed_ledgers=closed,risk_bars=risk_count,post_settlement_bars=tail_count,
                    independent_intention_scans=len(mask),actual_execution_qualified=False,g4=False)
