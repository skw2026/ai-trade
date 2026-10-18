"""Independent Decimal account formulas and exact information-capacity controls.

No import of the cash engine, no real-market or account reader. Sign-test
calibration is not a qualification test of expected economic profit.
"""
from decimal import Decimal as D, localcontext
from fractions import Fraction as F
from math import comb


def reference(case):
    """Reconstruct from raw synthetic quotes/events, never engine output fields."""
    with localcontext() as ctx:
        ctx.prec = 40
        sf, ff, z = map(D, {'base': ('.001','.0005','.0002'),
                           'stress': ('.002','.001','.001')}[case.get('cost','stress')])
        face = D(2500)
        capital = D(str(case.get('capital',10000)))
        peak = D(str(case.get('peak',10000)))
        s0, f0 = D(str(case['spot'])), D(str(case['future']))
        q = face / (f0 * (1-z))
        committed = q * s0 * (1+z) * (1+sf)
        balance = capital - committed
        ddmax = D(0)
        margin_min = None
        reason = None
        pending = None
        obligations = case.get('liabilities',[])
        def debt(t):
            return (sum((D(str(x.get('usdt',0))) for x in obligations if x['time_ms'] <= t), D(0)),
                    sum((D(str(x.get('btc',0))) for x in obligations if x['time_ms'] <= t), D(0)))
        def liquidation(coins, s):
            # Independently expressed bid/ask cash multiplier.
            return coins*s*((1-z)*(1-sf) if coins >= 0 else (1+z)*(1+sf))
        for b in case.get('bars',[]):
            t = b['t']; usd, btc = debt(t+300000)
            ratios = [D(20)*(1-(q*ff+btc)*D(str(m))/face) for m in (b['ml'], b['mh'])]
            coins = [face*(1-ff)/(D(str(m))*(1+z))-q*ff-btc for m in (b['ml'], b['mh'])]
            values = [balance-usd+liquidation(c,D(str(s))) for c in coins for s in (b['sl'],b['sh'])]
            peak = max([peak]+values)
            ddmax = max(ddmax,(peak-min(values))/peak)
            margin = min(ratios)
            margin_min = margin if margin_min is None else min(margin_min,margin)
            if reason is None and (ddmax >= D('.08') or margin <= 2):
                reason = 'DRAWDOWN_8_PERCENT' if ddmax >= D('.08') else 'MARGIN_BUFFER'
                pending = t+300000
        t = case['exit_ms']; usd, btc = debt(t)
        raw_s, raw_f = D(str(case['exit_spot'])), D(str(case['exit_future']))
        f = raw_f if case.get('settlement',False) else raw_f*(1+z)
        coins = face*(1-ff)/f-q*ff-btc
        sx = raw_s*(1-z if coins >= 0 else 1+z)
        final = balance-usd+liquidation(coins,raw_s)
        peak = max(peak,final); ddmax = max(ddmax,(peak-final)/peak)
        if reason is None and ddmax >= D('.08'):
            reason = 'DRAWDOWN_8_PERCENT'; pending=t
        gross = face*raw_s/raw_f-face*s0/f0
        nofees = face*sx/f-face*s0*(1+z)/(f0*(1-z))
        fees = q*s0*(1+z)*sf+abs(coins)*sx*sf+(q*ff+face/f*ff)*sx
        funding = usd+btc*sx
        net = final-capital
        assert abs(net-(nofees-fees-funding)) < D('1e-30')
        charges = {str(r):committed*r*D(t-case['entry_ms'])/D(31536000000) for r in (D('.06'),D('.12'))}
        return dict(cash_net=net,end_cash=final,end_peak=peak,cash_drawdown=ddmax,
                    gross=gross,slippage_drag=gross-nofees,execution_fees=fees,
                    financing_cost=funding,benchmark_charges=charges,
                    minimum_margin_ratio=margin_min,risk_reason=reason,pending_exit_ms=pending)


def sign_capacity():
    """Best-case IID, equal-magnitude *component*, never real strategy power."""
    rows=[]
    for n in (4,8,12,20,40):
        masses=[F(comb(n,k),2**n) for k in range(n+1)]
        critical=next((k for k in range(n+1) if sum(masses[k:]) <= F(1,20)),n+1)
        powers={}
        for probability in (F(3,5),F(3,4)):
            direct=sum((F(comb(n,k))*probability**k*(1-probability)**(n-k)
                        for k in range(critical,n+1)),F(0))
            distribution=[F(1)]
            for _ in range(n):
                nxt=[F(0)]*(len(distribution)+1)
                for k,value in enumerate(distribution):
                    nxt[k]+=value*(1-probability); nxt[k+1]+=value*probability
                distribution=nxt
            assert direct == sum(distribution[critical:])
            powers[str(probability)]=float(direct)
        rows.append(dict(blocks=n,critical_wins=critical,all_positive_null_probability=float(F(1,2**n)),
                         size=float(sum(masses[critical:])),power=powers))
    return dict(status='CONFIRMATION_UNBOUND',component='IID_EQUAL_MAGNITUDE_SIGN_ONLY',
                rows=rows,current_2023_independent_confirmation_blocks=0,
                current_2023_settlement_events=4,
                historical_unconsumed_domain='UNBOUND',economic_mean_effect='UNKNOWN',
                economic_tail_bound='UNKNOWN_8_PERCENT_IS_NOT_LOSS_CAP',
                investable_benchmark='UNBOUND',affordable_confirmatory_endpoint='UNBOUND',
                win_rate_counterexample=dict(wins=9,win_size=1,losses=1,loss_size=-20,total=-11),
                market_payoff_admitted=False,confirmation_attempts=0,candidate_qualified=False,g4=False)
