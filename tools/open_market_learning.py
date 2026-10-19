"""Small, causal, paired development learner. No network, trading or promotion."""
import math
from decimal import Decimal, ROUND_FLOOR

from btc_old_output_model import DAY, STEP, fill_price, need
from information_attribution import solve

ARMS = ('P', 'PV', 'STALE')
RIDGES = (.1, 1., 10.)


def mean(xs):
    need(bool(xs), 'EMPTY_VALUES')
    return math.fsum(xs) / len(xs)


def sd(xs):
    mu = mean(xs)
    return math.sqrt(mean([(x-mu)**2 for x in xs]))


def features(rows, start, end):
    """All windows end at decision t, never at the current bar's end."""
    by = {r['timestamp']: r for r in rows}
    need(len(by) == len(rows), 'DUPLICATE_BAR')
    closes = {r['timestamp']+STEP: r['price'] for r in rows}

    def volume_state(t):
        recent = math.fsum(by[k]['volume'] for k in range(t-DAY, t, STEP))
        past = math.fsum(by[k]['volume'] for k in range(t-8*DAY, t-DAY, STEP))/7
        need(recent > 0 and past > 0, 'ZERO_VOLUME_CONTEXT')
        return math.log(recent/past)

    out = {}
    for t in range(start, end, DAY):
        need(t % DAY == 0 and t-15*DAY in by and t-STEP in by, 'FEATURE_CONTEXT')
        returns = [10000*math.log(closes[t]/closes[t-k*STEP]) for k in (12, 72, 288, 2016)]
        rs = [math.log(by[k]['price']/by[k-STEP]['price']) for k in range(t-7*DAY, t, STEP)]
        v1, v7 = sd(rs[-288:]), sd(rs)
        need(v1 > 0 and v7 > 0, 'ZERO_VOLATILITY_CONTEXT')
        high = max(by[k]['high'] for k in range(t-DAY, t, STEP))
        low = min(by[k]['low'] for k in range(t-DAY, t, STEP))
        loc = 2*(closes[t]-low)/(high-low)-1 if high > low else 0.
        price = returns + [v1*10000, v1/v7, loc, returns[2]*v1/v7]
        volume, stale = volume_state(t), volume_state(t-7*DAY)
        xs = {'P': price}
        for arm, value in (('PV', volume), ('STALE', stale)):
            xs[arm] = price + [value, returns[1]*value, returns[2]*value]
        need(all(math.isfinite(x) for values in xs.values() for x in values), 'NONFINITE_FEATURE')
        out[t] = dict(price=closes[t], latest_price_closed_at=t, x=xs,
                      volume_log_ratio=volume, stale_volume_log_ratio=stale)
    return out


def samples(rows, fs, start, end):
    """Mature hypothetical daily stress labels, not continuous portfolio returns."""
    by = {r['timestamp']: r for r in rows}
    out = []
    for t in range(start, end, DAY):
        entry, leave = t+STEP, t+DAY-2*STEP
        if leave+STEP > end:
            continue
        f = fs[t]
        amount = float((Decimal(2500)/Decimal(str(f['price']))/Decimal('.001'))
                       .to_integral_value(rounding=ROUND_FLOOR)*Decimal('.001'))
        need(amount > 0, 'ZERO_LABEL_QUANTITY')
        labels = []
        for direction in (1, -1):
            q = direction*amount
            a, b = fill_price(by[entry], q, True), fill_price(by[leave], -q, True)
            funding = math.fsum(q*by[k]['mark_open']*by[k]['funding_rate_per_interval']
                               for k in range(entry+STEP, leave+STEP, STEP))
            net = q*(b-a)-abs(q)*(.0011)*(a+b)-funding
            labels.append(net/2500)
        out.append(dict(t=t, label_end=leave+STEP, x=f['x'], y=labels))
    return out


def fit(samples_, arm, ridge):
    need(arm in ARMS and ridge in RIDGES and len(samples_) >= 60, 'FIT_SCOPE')
    x = [r['x'][arm] for r in samples_]
    y = [r['y'] for r in samples_]
    width = len(x[0])
    need(width == (8 if arm == 'P' else 11), 'FEATURE_WIDTH')
    need(all(len(r) == width for r in x) and all(len(r) == 2 for r in y), 'FIT_SHAPE')
    need(all(math.isfinite(v) for r in x+y for v in r), 'NONFINITE_FIT')
    center = [mean([r[j] for r in x]) for j in range(width)]
    scale = [sd([r[j] for r in x]) for j in range(width)]
    z = [[(v-mu)/s if s > 1e-12 else 0. for v, mu, s in zip(r, center, scale)] for r in x]
    matrix = [[mean([r[i]*r[j] for r in z])+(ridge if i == j else 0.)
               for j in range(width)] for i in range(width)]
    intercept = [mean([r[j] for r in y]) for j in range(2)]
    coefficients = [solve(matrix, [mean([r[j]*(value[k]-intercept[k]) for r, value in zip(z, y)])
                                   for j in range(width)]) for k in range(2)]
    return dict(arm=arm, ridge=ridge, center=center, scale=scale, intercept=intercept,
                coefficients=coefficients, training_rows=len(samples_),
                training_start=min(r['t'] for r in samples_), label_cutoff=max(r['label_end'] for r in samples_))


def predict(model, feature, t):
    need(feature['latest_price_closed_at'] == t and model['label_cutoff'] <= t, 'PREDICTION_CLOCK')
    x = feature['x'][model['arm']]
    need(len(x) == len(model['center']), 'PREDICT_SHAPE')
    z = [(v-mu)/s if s > 1e-12 else 0. for v, mu, s in zip(x, model['center'], model['scale'])]
    values = [a+math.fsum(c*v for c, v in zip(beta, z))
              for a, beta in zip(model['intercept'], model['coefficients'])]
    need(all(math.isfinite(v) for v in values), 'NONFINITE_PREDICTION')
    direction = 0 if max(values) <= 0 else (1 if values[0] >= values[1] else -1)
    return dict(t=t, predicted_net_fraction=values, direction=direction)


def complete(result):
    return all(b['insufficient'] is None and b['quantity'] == 0 for b in result['books'])


def eligible(result):
    return complete(result) and all(b['stopped'] is None for b in result['books'])


def select(results):
    need(len(results) == 3 and {r['ridge'] for r in results} == set(RIDGES), 'SELECTION_SET')
    allowed = [r for r in results if eligible(r)]
    pool = allowed or results
    best = max(pool, key=lambda r: (r['books'][1]['net'], r['ridge']))
    return dict(ridge=best['ridge'], eligible=bool(allowed),
                status='SELECTED' if allowed else 'DIAGNOSTIC_ONLY_NO_ELIGIBLE_PATH')


def verdict(paths, selection):
    """Exploratory screening only; independent confirmation is always false."""
    reasons = []
    pv = paths['2025-PV']
    comparison_complete = all(complete(p) and p['bars'] == (p['end']-p['start'])//STEP
                              for p in paths.values())
    for period in ('2021', '2025'):
        reference = paths[period+'-PV']
        comparison_complete = comparison_complete and all(
            (paths[period+'-'+arm]['start'], paths[period+'-'+arm]['end']) ==
            (reference['start'], reference['end']) for arm in ('P', 'STALE'))
    comparison_complete = comparison_complete and (
        paths['2025-PV-delay']['start'], paths['2025-PV-delay']['end']) == (pv['start'], pv['end'])
    if not comparison_complete:
        return dict(status='COMPARISON_EVIDENCE_INCOMPLETE', reasons=['INCOMPLETE_CONTINUOUS_CALENDAR'],
                    independent_confirmation=False, qualified_candidates=0, g4=False,
                    online_learning_proven=False, demo_opening_lock_unchanged=True)
    for period in ('2021', '2025'):
        candidate = paths[period+'-PV']
        if not eligible(candidate):
            reasons.append(period+'_RISK_OR_EXECUTION')
        if any(b['net'] <= 0 for b in candidate['books']):
            reasons.append(period+'_NET_NONPOSITIVE')
        for arm in ('P', 'STALE'):
            if candidate['books'][1]['net'] <= paths[period+'-'+arm]['books'][1]['net']:
                reasons.append(period+'_NO_INCREMENT_'+arm)
    if not selection['PV']['eligible']:
        reasons.append('SELECTION_RISK')
    central = not reasons
    stress = pv['books'][1]
    if stress['net'] < 100:
        reasons.append('MINIMUM_ABSOLUTE_EFFECT')
    for arm in ('P', 'STALE'):
        if stress['net']-paths['2025-'+arm]['books'][1]['net'] < 25:
            reasons.append('MINIMUM_INCREMENT_'+arm)
    cycles = [c for c in stress['cycles'] if c['complete']]
    capacity = len(cycles) >= 30 and len(stress['active_weeks']) >= 12
    if not capacity:
        reasons.append('EVENT_CAPACITY')
    if stress['net']-max([c['net'] for c in stress['cycles']]+[0.]) <= 0:
        reasons.append('BEST_CYCLE_CONCENTRATION')
    midpoint = 1751328000000  # 2025-07-01 UTC; same continuous account.
    previous = 10000.
    first = second = 0.
    for d in stress['days']:
        change = d['equity']-previous
        if d['t'] <= midpoint:
            first += change
        else:
            second += change
        previous = d['equity']
    if min(first, second) <= 0:
        reasons.append('HALF_YEAR_SIGN')
    delayed = paths['2025-PV-delay']
    if not eligible(delayed) or delayed['books'][1]['net'] <= 0:
        reasons.append('DELAY_SENSITIVITY')
    status = ('DEVELOPMENT_SUPPORTED' if not reasons else
              'INSUFFICIENT_EVENT_CAPACITY' if central and not capacity else
              'FRAGILE_DEVELOPMENT_SIGNAL' if central else 'NET_INCREMENT_UNSUPPORTED')
    return dict(status=status, reasons=reasons, half_year_pressure_net=[first, second],
                independent_confirmation=False, qualified_candidates=0, g4=False,
                online_learning_proven=False, demo_opening_lock_unchanged=True)
