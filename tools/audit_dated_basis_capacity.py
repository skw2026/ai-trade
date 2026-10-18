"""Independent Decimal closed-form quote budgets and capacity aggregation."""
from decimal import Decimal as D, localcontext
import math

BAR = 300000
DAY = 86400000
SPEC = {
    'base_cash': ('0.001','0.0005','0.0002','0','0',0),
    'base_economic': ('0.001','0.0005','0.0002','0.06','0',0),
    'stress_cash': ('0.002','0.001','0.001','0','0',0),
    'stress_economic': ('0.002','0.001','0.001','0.12','0',0),
    'stress_terminal_cash': ('0.002','0.001','0.001','0','0.0025',3600000),
    'stress_terminal_economic': ('0.002','0.001','0.001','0.12','0.0025',3600000),
}


def exact(spot, future, held_ms, layer):
    # Algebraic reduction of inverse short plus covered coin balance; no wallet simulator.
    with localcontext() as ctx:
        ctx.prec = 40
        sf, ff, z, annual, bias = map(D, SPEC[layer][:5])
        duration = D(held_ms+SPEC[layer][5])/D(365*DAY)
        price_ratio = D(spot)/D(future)
        liquidation = (1-z)*(1-sf)
        conversion = (1-ff)/(1+bias)*liquidation
        drag = price_ratio/(1-z)*(ff*liquidation+(1+z)*(1+sf)*(1+annual*duration))
        return D(2500)*(conversion-drag)


def near(got, wanted):
    assert math.isfinite(got)
    assert abs(D(str(got))-D(str(wanted))) < D('1e-7'), (got, str(wanted))


def aggregate(rows, key, start):
    valid = [(t, v[key]) for t,b,v in rows if v is not None]
    positives = [(t,v) for t,v in valid if v>D('1e-9')]
    flags = {t for t,v in positives}
    beginnings = sorted(t for t in flags if t-BAR not in flags)
    lengths = []
    for first in beginnings:
        n = 0
        while first+n*BAR in flags:
            n += 1
        lengths.append(n)
    values = sorted(v for t,v in valid)
    pv = sorted(v for t,v in positives)
    def median(xs):
        return None if not xs else (xs[(len(xs)-1)//2]+xs[len(xs)//2])/2
    return dict(total_quotes=len(rows),valid_quotes=len(valid),positive_quotes=len(positives),
        positive_fraction=len(positives)/len(valid) if valid else None,
        positive_utc_days=len({(t+BAR)//DAY for t,v in positives}),
        positive_7day_blocks=len({(t-start)//(7*DAY) for t,v in positives}),
        runs=len(beginnings),runs_at_least_1h=sum(n>=12 for n in lengths),
        runs_at_least_24h=sum(n>=288 for n in lengths),longest_run_bars=max(lengths,default=0),
        margin_min=min(values,default=None),margin_median=median(values),margin_max=max(values,default=None),
        positive_margin_median=median(pv),first_positive_ms=min(flags,default=None),last_positive_ms=max(flags,default=None))


def compare(got, expected):
    assert set(got)==set(expected)
    for key,value in expected.items():
        if isinstance(value,D):
            near(got[key],value)
        else:
            assert got[key]==value,(key,got[key],value)


def audit(bundle, projection, result, ledgers):
    checked = 0
    valid_count = 0
    strong = 'stress_terminal_economic'
    persistent = 0
    positive = 0
    for symbol,q in bundle.items():
        expiry = q['expiry']
        source = projection[symbol]
        gotrows = ledgers[symbol]
        grid = list(range(expiry-90*DAY-BAR,expiry-DAY,BAR))
        assert [r['t'] for r in source]==grid==[r['t'] for r in gotrows]
        expected_rows = []
        reasons = {}
        for t,row,out in zip(grid,source,gotrows):
            why = []
            prices = {}
            for kind in ('spot','future'):
                original = q['series'][kind].get(str(t))
                if original is None:
                    why.append(kind+'_missing')
                    continue
                if original.get('complete') is not True or original['close_ms']!=t+BAR-1:
                    why.append(kind+'_incomplete')
                if not D(original['v']).is_finite() or D(original['v'])<=0 or original['n']<=0:
                    why.append(kind+'_no_trades')
                prices[kind] = original['c']
                assert D(row[kind])==D(original['c'])
            assert row['reasons']==why and row['expiry']==expiry
            for r in why:reasons[r]=reasons.get(r,0)+1
            remaining = D(expiry-t-BAR)/DAY
            b = ('1_to_7' if remaining<7 else '7_to_30' if remaining<30 else
                 '30_to_60' if remaining<60 else '60_to_90')
            assert out['bucket']==b
            values = None
            if why:
                assert out['values'] is None
            else:
                valid_count += 1
                values = {key:exact(prices['spot'],prices['future'],expiry-t-2*BAR,key) for key in SPEC}
                assert set(out['values'])==set(values)
                for key,value in values.items():
                    near(out['values'][key],value)
                    assert (out['values'][key]>1e-9)==(value>D('1e-9'))
                    checked += 1
            expected_rows.append((t,b,values))
        summary = result['quarters'][symbol]
        assert summary['reasons']==reasons
        for key in SPEC:
            compare(summary['layers'][key],aggregate(expected_rows,key,grid[0]))
            for b in ('1_to_7','7_to_30','30_to_60','60_to_90'):
                compare(summary['maturity_buckets'][b][key],
                        aggregate([r for r in expected_rows if r[1]==b],key,grid[0]))
        for days in (30,60,90):
            t = expiry-days*DAY-BAR
            assert summary['fixed_calendar_points'][str(days)]==gotrows[grid.index(t)]
        positive += summary['layers'][strong]['positive_quotes']
        persistent += summary['layers'][strong]['runs_at_least_1h']>0
    expected_status = ('NO_OBSERVED_PRESSURE_HEADROOM' if positive==0 else
                       'TRANSIENT_OR_SINGLE_EXPIRY_ONLY' if persistent<2 else
                       'MULTI_EXPIRY_PERSISTENT_HEADROOM')
    assert result['status']==expected_status
    assert result['economic_attempts']==0 and result['training_vectors']==0
    assert not result['g4'] and not result['candidate_qualified']
    assert result['observed_expiry_events']==len(bundle) and result['independent_events_not_established']
    return dict(status='INDEPENDENT_DECIMAL_AND_CAPACITY_MATCH',valid_quotes=valid_count,
                checked_amounts=checked,persistent_expiries=persistent,positive_quotes=positive)
