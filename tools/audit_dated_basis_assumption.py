"""Independent Decimal cashflow and risk replay from original input bars.

Does not import the simulation engine or use its prices, fees, exit or decision
as inputs. Compares them only after independent reconstruction.
"""
from decimal import Decimal, localcontext
import itertools

D = Decimal
B = 300000
DAY = 86400000
YEAR = D(31536000000)
FACE = D(2500)
PARAMS = {'zero_friction': ('0','0','0','0'),
          'base': ('.001','.0005','.0002','.06'), 'stress': ('.002','.001','.001','.12')}


def close(value, expected, label):
    value = D(str(value))
    assert value.is_finite() and abs(value-expected) <= max(D('0.0000001'),abs(expected)*D('1e-10')), label


def prep(quarter):
    return dict(quarter, series={k:{int(t):dict(row, **{
        f:D(str(row[f])) for f in ('o','h','l','c','v')}) for t,row in bars.items()}
        for k,bars in quarter['series'].items()},settlement=D(str(quarter['settlement'])))


def decision(quarter):
    t = quarter['expiry']-7*DAY-B
    s, f = quarter['series']['spot'][t]['c'], quarter['series']['future'][t]['c']
    sf, ff, slip, annual = map(D,PARAMS['base'])
    size = FACE/(f*(1-slip));spent = size*s*(1+slip)*(1+sf)
    # Algebraically eliminate the initial spot/short principal cancellation.
    terminal_btc = FACE*(1-ff)/s-size*ff
    return terminal_btc*s*(1-slip)*(1-sf)-spent-spent*annual*D(7*DAY-B)/YEAR


def reference(quarter, scenario, capital, prior_peak):
    sf, ff, slip, annual = map(D,PARAMS[scenario['cost']])
    entry = quarter['expiry']-7*DAY+B
    settlement = quarter['expiry']-28800000+scenario['hour']*3600000
    stop = settlement+scenario['delay_minutes']*60000
    price_i = quarter['settlement']*(1+D(scenario['bias_bps'])/10000)
    source = quarter['series'];s = source['spot'][entry]['o'];f = source['future'][entry]['o']
    sin, fin = s*(1+slip), f*(1-slip)
    size = FACE/fin;spent = size*sin*(1+sf);cash = capital-spent
    peak = max(capital, prior_peak if prior_peak is not None else capital)
    ddmax = D(0);minmargin = None;unknown = checked = 0;pending = None;reason = None;unavailable = False
    for t in range(entry,stop+B,B):
        settled = t >= settlement
        sb = source['spot'].get(t);fb = source['future'].get(t)
        if t == stop or pending is not None and t >= pending:
            usable = sb is not None and sb.get('complete',True)
            usable = usable and (settled or fb is not None and fb.get('complete',True))
            if usable:
                if not sb['n'] or not sb['v'] or not settled and (not fb['n'] or not fb['v']):unknown += 1
                raw_exit = price_i if settled else fb['o']
                execution_exit = raw_exit if settled else raw_exit*(1+slip)
                out = sb['o'];sout = out*(1-slip)
                terminal = FACE*(1-ff)/execution_exit-size*ff
                capital_cost = spent*annual*D(t-entry)/YEAR
                final = cash+terminal*sout*(1-sf)-capital_cost
                peak = max(peak,final);ddmax = max(ddmax,(peak-final)/peak)
                if reason is None and ddmax >= D('.08'):reason='EXIT_DRAWDOWN_8_PERCENT'
                gross = FACE*out/raw_exit-FACE*s/f
                nofees = FACE*sout/execution_exit-FACE*sin/fin
                charges = size*sin*sf+terminal*sout*sf+(size*ff+FACE/execution_exit*ff)*sout
                return dict(entry_ms=entry,settlement_ms=settlement,planned_exit_ms=stop,exit_ms=t,
                 end_capital=final,end_peak=peak,net=final-capital,gross=gross,slippage_drag=gross-nofees,
                 fees=charges,opportunity_cost=capital_cost,extra_cost_headroom_bps=(final-capital)*4,
                 gross_cost_capacity_bps=gross*4,spot_entry=s,future_entry=f,spot_exit=out,
                 execution_spot_entry=sin,execution_future_entry=fin,execution_future_exit=execution_exit,
                 raw_future_exit=raw_exit,execution_spot_exit=sout,btc_bought=size,btc_after_exit=terminal,
                 spot_spend=spent,spot_fee_in=size*sin*sf,spot_fee_out=terminal*sout*sf,
                 future_fee_in_btc=size*ff,future_fee_out_btc=FACE/execution_exit*ff,
                 held_ms=t-entry,risk_reason=reason,observed_drawdown=ddmax,minimum_margin_ratio=minmargin,
                 checked_bars=checked,unknown_bars=unknown,risk_exit_unavailable=unavailable,
                 risk_status='UNKNOWN' if unknown or unavailable else 'MODEL_OBSERVED_ONLY')
            unavailable=True;unknown += 1
        mb=source['mark'].get(t)
        if sb is None or not sb.get('complete',True) or not settled and (mb is None or not mb.get('complete',True)):
            unknown += 1
            continue
        if not sb['n'] or not sb['v'] or not settled and (fb is None or not fb.get('complete',True) or
                                                        not fb['n'] or not fb['v']):unknown += 1
        accrued = spent*annual*D(t+B-entry)/YEAR
        if settled:
            quantities = [FACE*(1-ff)/price_i-size*ff]
            margin = None
        else:
            marks=(mb['l'],mb['h'])
            quantities=[FACE*(1-ff)/(m*(1+slip))-size*ff for m in marks]
            margin=min(D(20)*(1-size*ff*m/FACE) for m in marks)
            minmargin=margin if minmargin is None else min(minmargin,margin)
        values=[cash+quantity*sp*(1-slip)*(1-sf)-accrued for quantity in quantities for sp in (sb['l'],sb['h'])]
        peak=max(peak,max(values));dd=(peak-min(values))/peak;ddmax=max(ddmax,dd);checked+=1
        if pending is None and (dd>=D('.08') or margin is not None and margin<=2):
            pending=t+B;reason='DRAWDOWN_8_PERCENT' if dd>=D('.08') else 'MARGIN_BUFFER'
    raise AssertionError('NO_AUDIT_EXIT')


def check_ledger(quarter, result):
    capital=D(str(result['capital']));peak=None if result['prior_peak'] is None else D(str(result['prior_peak']))
    expected=reference(quarter,result['scenario'],capital,peak)
    for key,value in expected.items():
        if isinstance(value,D):close(result[key],value,key)
        else:assert result[key]==value,key
    assert result['actual_execution_qualified'] is False
    return expected


def audit(bundle, result):
    with localcontext() as ctx:
        ctx.prec=40
        quarters={name:prep(q) for name,q in bundle.items()}
        ids={f'{p}_h{h}_b{b}_m{m}' for p,h,b,m in itertools.product(PARAMS,(0,8),(-25,0,25),(0,5,60))}
        assert result['scenario_count']==len(ids)==54
        assert result['independent_quarter_ledgers']==len(quarters)*54
        assert set(result['decisions'])==set(quarters)==set(result['diagnostics'])
        ledgers=bars=0
        for name,q in quarters.items():
            expected=decision(q)
            close(result['decisions'][name]['expected_net'],expected,'DECISION')
            assert result['decisions'][name]['enter']==(expected>0)
            assert {r['scenario']['id'] for r in result['diagnostics'][name]}==ids
            assert len(result['diagnostics'][name])==54
            for ledger in result['diagnostics'][name]:
                checked=check_ledger(q,ledger);ledgers+=1;bars+=checked['checked_bars']
        assert len(result['portfolios'])==54 and {p['scenario']['id'] for p in result['portfolios']}==ids
        summaries=[]
        ordered=sorted(quarters,key=lambda k:quarters[k]['expiry'])
        for portfolio in result['portfolios']:
            capital=peak=D(10000);halted=False;unknown=False
            assert [v['symbol'] for v in portfolio['paths']]==ordered
            for path in portfolio['paths']:
                if halted or not result['decisions'][path['symbol']]['enter']:
                    assert path['status']==('HALTED' if halted else 'NO_ENTRY')
                    assert 'ledger' not in path
                    continue
                ledger=path['ledger'];assert ledger['scenario']==portfolio['scenario']
                close(ledger['capital'],capital,'INHERITED_CAPITAL');close(ledger['prior_peak'],peak,'INHERITED_PEAK')
                checked=check_ledger(quarters[path['symbol']],ledger);ledgers+=1;bars+=checked['checked_bars']
                capital=checked['end_capital'];peak=checked['end_peak']
                halted=checked['risk_reason'] is not None;unknown=unknown or checked['risk_status']=='UNKNOWN'
            close(portfolio['net'],capital-10000,'PORTFOLIO_NET')
            close(portfolio['return_percent'],(capital/10000-1)*100,'RETURN_PERCENT')
            assert portfolio['halted']==halted and portfolio['risk_unknown']==unknown
            summaries.append(dict(portfolio,net=capital-10000))
        if not any(v['enter'] for v in result['decisions'].values()):verdict='NO_GO_NO_TRADES'
        elif max(p['net'] for p in summaries if p['scenario']['cost']=='zero_friction')<=0:verdict='NO_GO_EVEN_OPTIMISTIC'
        else:
            base=next(p for p in summaries if p['scenario']['id']=='base_h8_b0_m0')
            if base['net']<=0 or base['halted']:verdict='NO_GO_BASE'
            elif any(p['net']<=0 or p['halted'] or p['risk_unknown'] for p in summaries if p['scenario']['cost']=='stress'):
                verdict='CONDITIONAL_ONLY'
            else:verdict='ASSUMPTION_ROBUST_DEVELOPMENT'
        assert result['status']==verdict and not result['g4'] and not result['candidate_qualified']
        return dict(status='DECIMAL_CASH_RISK_AND_POLICY_MATCH',ledgers=ledgers,risk_bars=bars,
                    verdict=verdict,independent_precision=40,absolute_tolerance='0.0000001')
