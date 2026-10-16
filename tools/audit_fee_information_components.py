"""Independent Decimal check of input projection, frozen algebra and summaries."""
from decimal import Decimal, localcontext
import hashlib
import math

D = Decimal
DAY = 86400000


def near(a, b):
    assert math.isfinite(float(a)) and math.isfinite(float(b))
    assert abs(D(str(a))-D(str(b))) <= D('0.0000002')+D('0.00000001')*abs(D(str(b))), (a, b)


def average(xs): return sum(xs)/D(len(xs))


def solve(a, b):
    a = [list(row)+[value] for row, value in zip(a, b)]
    n = len(a)
    for k in range(n):
        p = max(range(k,n), key=lambda i: abs(a[i][k]))
        a[k], a[p] = a[p], a[k]
        assert a[k][k] != 0
        for i in range(k+1,n):
            factor = a[i][k]/a[k][k]
            for j in range(k,n+1): a[i][j] -= factor*a[k][j]
    out = [D(0)]*n
    for k in reversed(range(n)):
        out[k] = (a[k][n]-sum(a[k][j]*out[j] for j in range(k+1,n)))/a[k][k]
    return out


def checked_rows(artifact, result):
    with localcontext() as ctx:
        ctx.prec = 45
        base = artifact['result']['models']['price']
        aug = artifact['result']['models']['increment']
        def transform(r):
            return [(D(str(v))-D(str(c)))/D(str(s)) if s > 1e-12 else D(0)
                    for v,c,s in zip(r['x'],aug['center'],aug['scale'])]
        inputs = [transform(r) for r in artifact['train']]
        gram = [[average([r[i]*r[j] for r in inputs]) for j in range(3)] for i in range(3)]
        cross = [average([r[i]*r[3] for r in inputs]) for i in range(3)]
        a = solve(gram, cross)
        for x,y in zip(a,result['projection']): near(x,y)
        residual = [r[3]-sum(r[j]*a[j] for j in range(3)) for r in inputs]
        near(average([v*v for v in residual]), result['training_residual_variance'])
        assert result['training_rows'] == len(inputs)
        near(result['signal_coefficient_bps'], aug['coefficients'][3])
        assert len(result['rows']) == len(artifact['evaluation'])
        checked = []
        for raw, actual in zip(artifact['evaluation'], result['rows']):
            x = transform(raw)
            price = D(str(base['intercept']))+sum(D(str(b))*v for b,v in zip(base['coefficients'],x))
            inc = D(str(aug['intercept']))+sum(D(str(b))*v for b,v in zip(aug['coefficients'],x))
            delta = inc-price
            u = D(str(aug['coefficients'][3]))*(x[3]-sum(x[j]*a[j] for j in range(3)))
            v = delta-u
            e = D(str(raw['y']))-price
            # Independent symmetric allocation via subtracting half interaction.
            gu = e*e-(e-u)*(e-u)-u*v
            gv = e*e-(e-v)*(e-v)-u*v
            expected = dict(t=raw['t'],label_end=raw['label_end'],y=D(str(raw['y'])),
                price=price,increment=inc,u=u,v=v,delta=delta,total_gain=e*e-(e-delta)*(e-delta),
                residual_gain=gu,price_gain=gv)
            assert set(actual) == set(expected)
            for key,value in expected.items(): near(value,actual[key])
            checked.append(expected)
        return checked


def check_summary(rows, start, end, result):
    with localcontext() as ctx:
        ctx.prec = 45
        chosen = [r for r in rows if start <= r['t'] < end and r['label_end'] <= end]
        assert result['rows'] == len(chosen)
        identity = ','.join('%d:%d' % (r['t'],r['label_end']) for r in chosen)
        assert result['date_sha256'] == hashlib.sha256(identity.encode()).hexdigest()
        for key in ('total_gain','residual_gain','price_gain'):
            actual = result['components'][key]
            mu = average([r[key] for r in chosen]);near(mu,actual['mean_bps_squared'])
            centered = {r['t']:r[key]-mu for r in chosen}
            variance = sum(v*v for v in centered.values())
            for left, x in centered.items():
                for right, y in centered.items():
                    lag = (right-left)//DAY
                    if right-left == lag*DAY and 1 <= lag <= 7:
                        variance += 2*(1-D(lag)/8)*x*y
            near(max(D(0),variance).sqrt()/len(chosen), actual['hac7_se_bps_squared'])
            groups = sorted({(r['t']-start)//(28*DAY) for r in chosen})
            assert len(actual['leave_block_out']) == len(groups)
            values = []
            for index, observed in zip(groups,actual['leave_block_out']):
                retained = [r[key] for r in chosen if (r['t']-start)//(28*DAY) != index]
                value = average(retained);values.append(value)
                assert observed['block'] == index
                assert observed['start'] == start+index*28*DAY
                assert observed['end'] == min(end,start+(index+1)*28*DAY)
                assert observed['retained_rows'] == len(retained)
                assert observed['removed_rows'] == len(chosen)-len(retained)
                near(value,observed['retained_mean_bps_squared'])
            near(min(values),actual['minimum']);near(max(values),actual['maximum'])
            assert actual['opposite_sign_blocks'] == sum(v*mu < 0 for v in values)
        for key in ('increment','u','delta'):
            values = [abs(r[key]) for r in chosen]
            actual = result['forecast_scale'][key]
            near(average(values),actual['mean_abs_bps']);near(max(values),actual['max_abs_bps'])
            assert actual['above_15bps'] == sum(v > 15 for v in values)
            assert actual['above_30bps'] == sum(v > 30 for v in values)
        assert result['cost_status'] == 'COST_HEADROOM_NOT_ESTABLISHED'
        for key in ('independent_confirmation','causal_identification','promotion_eligible','g4'):
            assert result[key] is False
        assert result['profit_estimate'] is None and result['trading_paths_executed'] == 0
