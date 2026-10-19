"""Independent causal feature, label and ridge audits; no learner imports or I/O.

Feature logarithms, moments, label cash and normal equations use 45-digit Decimal.
Comparisons allow only documented floating-point implementation error. This is not
an independent market sample, execution certification or an economic promotion.
"""
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR, localcontext

STEP = 300000
DAY = 86400000
ARMS = ('P', 'PV', 'STALE')
WIDTHS = {'P': 8, 'PV': 11, 'STALE': 11}
RIDGES = (D('.1'), D('1'), D('10'))


def require(condition, reason):
    if not condition:
        raise ValueError('AUDIT_' + reason)


def number(value):
    require(not isinstance(value, bool), 'BOOLEAN_NUMBER')
    try:
        out = D(str(value))
    except Exception as error:
        raise ValueError('AUDIT_NUMBER') from error
    require(out.is_finite(), 'NONFINITE')
    return out


def near(actual, expected, reason, absolute=D('2e-11'), relative=D('2e-11')):
    actual, expected = number(actual), number(expected)
    require(abs(actual - expected) <= absolute + relative * abs(expected), reason)


def feature_near(actual, expected):
    # Float logs of price ratios versus independent Decimal log differences.
    near(actual, expected, 'FEATURE_VALUE', absolute=D('2e-8'))


def audit_features_and_labels(rows, fs, samples, start, end):
    """Audit exactly [start,end)'s daily feature grid and all mature labels.

    samples=None explicitly requests features only. An empty list does not waive
    expected labels. fs must be the exact requested window, not a larger cache.
    Raw rows can include the full source file and required historical context.
    """
    require(type(start) is int and type(end) is int and start % DAY == 0 and
            end > start, 'WINDOW')
    dates = list(range(start, end, DAY))
    require(isinstance(fs, dict) and set(fs) == set(dates) and
            all(type(t) is int for t in fs), 'FEATURE_GRID')
    by = {}
    previous = None
    for row in rows:
        t = row['timestamp']
        require(type(t) is int and t % STEP == 0 and t not in by, 'RAW_IDENTITY')
        require(previous is None or t == previous + STEP, 'RAW_GRID')
        by[t] = row
        previous = t

    with localcontext() as ctx:
        ctx.prec = 45
        prices, logarithms, volumes = {}, {}, {}

        def row_at(t):
            require(t in by, 'MISSING_RAW_BAR')
            return by[t]

        def price_at(t):
            if t not in prices:
                p = number(row_at(t)['price'])
                require(p > 0, 'PRICE_POSITIVE')
                prices[t] = p
            return prices[t]

        def log_at(t):
            if t not in logarithms:
                logarithms[t] = price_at(t).ln()
            return logarithms[t]

        def volume_at(t):
            if t not in volumes:
                v = number(row_at(t)['volume'])
                require(v >= 0, 'VOLUME_NONNEGATIVE')
                volumes[t] = v
            return volumes[t]

        def volume_condition(t):
            recent = sum((volume_at(k) for k in range(t-DAY, t, STEP)), D(0))
            prior = sum((volume_at(k) for k in range(t-8*DAY, t-DAY, STEP)), D(0)) / 7
            require(recent > 0 and prior > 0, 'VOLUME_CONTEXT')
            return recent.ln() - prior.ln()

        def deviation(values):
            # Independent moment identity, rather than the learner's deviations.
            n = D(len(values))
            first = sum(values, D(0)) / n
            variance = sum((x*x for x in values), D(0)) / n - first*first
            require(variance > 0, 'VOLATILITY_CONTEXT')
            return variance.sqrt()

        expected_x = {}
        for t in dates:
            f = fs[t]
            require(f['latest_price_closed_at'] == t, 'FEATURE_CLOCK')
            require(number(f['price']) == price_at(t-STEP), 'DECISION_PRICE')
            require(set(f['x']) == set(ARMS), 'FEATURE_ARMS')
            last_log = log_at(t-STEP)
            returns = [D(10000)*(last_log-log_at(t-STEP-lag))
                       for lag in (12*STEP, 72*STEP, DAY, 7*DAY)]
            rs = [log_at(k)-log_at(k-STEP) for k in range(t-7*DAY, t, STEP)]
            daily, weekly = deviation(rs[-288:]), deviation(rs)
            highs = [number(row_at(k)['high']) for k in range(t-DAY, t, STEP)]
            lows = [number(row_at(k)['low']) for k in range(t-DAY, t, STEP)]
            high, low = max(highs), min(lows)
            require(high >= low and low > 0, 'FEATURE_RANGE')
            position = 2*(price_at(t-STEP)-low)/(high-low)-1 if high > low else D(0)
            p = returns + [daily*10000, daily/weekly, position, returns[2]*daily/weekly]
            v, old = volume_condition(t), volume_condition(t-7*DAY)
            expected_x[t] = {'P': p, 'PV': p+[v, returns[1]*v, returns[2]*v],
                             'STALE': p+[old, returns[1]*old, returns[2]*old]}
            feature_near(f['volume_log_ratio'], v)
            feature_near(f['stale_volume_log_ratio'], old)
            for arm in ARMS:
                require(len(f['x'][arm]) == WIDTHS[arm], 'FEATURE_WIDTH')
                for actual, expected in zip(f['x'][arm], expected_x[t][arm]):
                    feature_near(actual, expected)

        label_count = 0
        if samples is not None:
            require(isinstance(samples, list), 'LABEL_SCHEMA')
            mature = [t for t in dates if t+DAY-STEP <= end]
            require([r['t'] for r in samples] == mature, 'LABEL_GRID')
            for sample in samples:
                t = sample['t']
                entry, leave = t+STEP, t+DAY-2*STEP
                require(sample['label_end'] == leave+STEP and
                        sample['label_end'] <= end, 'LABEL_MATURITY')
                require(set(sample['x']) == set(ARMS), 'LABEL_FEATURE_ARMS')
                for arm in ARMS:
                    require(len(sample['x'][arm]) == WIDTHS[arm], 'LABEL_FEATURE_WIDTH')
                    for actual, expected in zip(sample['x'][arm], expected_x[t][arm]):
                        feature_near(actual, expected)
                amount = (D(2500)/price_at(t-STEP)/D('.001')).to_integral_value(
                    rounding=ROUND_FLOOR)*D('.001')
                require(amount > 0 and len(sample['y']) == 2, 'LABEL_QUANTITY_OR_SHAPE')

                def fill(time, buy):
                    raw = number(row_at(time)['high' if buy else 'low'])
                    require(raw > 0, 'LABEL_PRICE_POSITIVE')
                    adjusted = raw*(D('1.0004') if buy else D('.9996'))
                    return (adjusted/D('.1')).to_integral_value(
                        rounding=ROUND_CEILING if buy else ROUND_FLOOR)*D('.1')

                funding_basis = D(0)
                for k in range(entry+STEP, leave+STEP, STEP):
                    r = row_at(k)
                    mark = number(r['mark_open'])
                    require(mark > 0, 'FUNDING_MARK')
                    funding_basis += mark*number(r['funding_rate_per_interval'])
                for j, sign in enumerate((1, -1)):
                    q = amount*sign
                    a, b = fill(entry, sign > 0), fill(leave, sign < 0)
                    wallet = -q*a - amount*a*D('.0011')
                    wallet -= q*funding_basis
                    wallet += q*b - amount*b*D('.0011')
                    near(sample['y'][j], wallet/2500, 'LABEL_CASH', absolute=D('2e-12'))
                    label_count += 1
    return dict(feature_dates=len(dates), feature_cells=len(dates)*30,
                labels=label_count, label_samples=label_count//2,
                features_only=samples is None, decimal_precision=45)


def audit_fit(samples, model):
    """Rebuild scalers and verify mean-normalized ridge first-order conditions.

    Does not call fit(), solve(), or any learner helper. Positive ridge makes the
    checked quadratic strictly convex, including constant-feature directions.
    """
    arm = model['arm']
    require(arm in ARMS and number(model['ridge']) in RIDGES, 'FIT_SCOPE')
    require(len(samples) >= 60, 'FIT_COUNT')
    dates = [r['t'] for r in samples]
    require(all(type(t) is int and t % DAY == 0 for t in dates) and
            dates == sorted(set(dates)), 'FIT_DATES')
    require(all(type(r['label_end']) is int and r['label_end'] > r['t'] for r in samples),
            'FIT_LABEL_CLOCK')
    require(model['training_rows'] == len(samples) and
            model['training_start'] == min(dates) and
            model['label_cutoff'] == max(r['label_end'] for r in samples), 'FIT_METADATA')
    width = WIDTHS[arm]
    require(len(model['center']) == len(model['scale']) == width and
            len(model['intercept']) == len(model['coefficients']) == 2 and
            all(len(beta) == width for beta in model['coefficients']), 'FIT_SHAPE')
    require(all(len(r['x'][arm]) == width and len(r['y']) == 2 for r in samples), 'FIT_INPUT_SHAPE')
    with localcontext() as ctx:
        ctx.prec = 45
        n, ridge = D(len(samples)), number(model['ridge'])
        x = [[number(v) for v in r['x'][arm]] for r in samples]
        y = [[number(v) for v in r['y']] for r in samples]
        centers = [sum((r[j] for r in x), D(0))/n for j in range(width)]
        scales = [(sum(((r[j]-centers[j])**2 for r in x), D(0))/n).sqrt()
                  for j in range(width)]
        for j in range(width):
            near(model['center'][j], centers[j], 'FIT_CENTER')
            near(model['scale'][j], scales[j], 'FIT_SCALE')
        z = [[(r[j]-centers[j])/scales[j] if scales[j] > D('1e-12') else D(0)
              for j in range(width)] for r in x]
        worst = D(0)
        for direction in range(2):
            intercept = sum((r[direction] for r in y), D(0))/n
            near(model['intercept'][direction], intercept, 'FIT_INTERCEPT')
            beta = [number(v) for v in model['coefficients'][direction]]
            errors = [sum((b*v for b, v in zip(beta, row)), D(0))-
                      (target[direction]-intercept) for row, target in zip(z, y)]
            for j in range(width):
                residual = sum((row[j]*error for row, error in zip(z, errors)), D(0))/n + ridge*beta[j]
                scale = sum((abs(row[j]*error) for row, error in zip(z, errors)), D(0))/n + abs(ridge*beta[j])
                require(abs(residual) <= D('2e-11')+D('2e-9')*scale, 'FIT_NORMAL_EQUATION')
                worst = max(worst, abs(residual))
    return dict(training_rows=len(samples), coefficients=2*width,
                normalized_normal_equations=True, maximum_residual=str(worst))


def audit_predictions(model, fs, predictions):
    """Require the whole exact evaluation feature grid, including first/last day."""
    require(isinstance(fs, dict) and bool(fs), 'PREDICTION_FEATURE_GRID')
    dates = sorted(fs)
    require(all(type(t) is int and t % DAY == 0 for t in dates) and
            dates == list(range(dates[0], dates[-1]+DAY, DAY)), 'PREDICTION_DAILY_GRID')
    if isinstance(predictions, dict):
        require(set(predictions) == set(dates), 'PREDICTION_GRID')
        rows = [predictions[t] for t in dates]
    else:
        require(isinstance(predictions, list), 'PREDICTION_SCHEMA')
        rows = predictions
    require([r['t'] for r in rows] == dates, 'PREDICTION_GRID')
    arm = model['arm']
    require(arm in ARMS, 'PREDICTION_ARM')
    width = WIDTHS[arm]
    require(len(model['center']) == len(model['scale']) == width and
            len(model['intercept']) == len(model['coefficients']) == 2 and
            all(len(beta) == width for beta in model['coefficients']), 'PREDICTION_MODEL_SHAPE')
    require(type(model['label_cutoff']) is int, 'PREDICTION_CUTOFF')
    with localcontext() as ctx:
        ctx.prec = 45
        center, scale = map(lambda xs: [number(v) for v in xs], (model['center'], model['scale']))
        require(all(s >= 0 for s in scale), 'PREDICTION_SCALE')
        beta = [[number(v) for v in b] for b in model['coefficients']]
        intercept = [number(v) for v in model['intercept']]
        for t, prediction in zip(dates, rows):
            f = fs[t]
            require(f['latest_price_closed_at'] == t and model['label_cutoff'] <= t,
                    'PREDICTION_CLOCK')
            require(len(f['x'][arm]) == width, 'PREDICTION_FEATURE_WIDTH')
            z = [(number(value)-mu)/s if s > D('1e-12') else D(0)
                 for value, mu, s in zip(f['x'][arm], center, scale)]
            expected = [a+sum((b*v for b, v in zip(coefficients, z)), D(0))
                        for a, coefficients in zip(intercept, beta)]
            require(len(prediction['predicted_net_fraction']) == 2, 'PREDICTION_SHAPE')
            for actual, value in zip(prediction['predicted_net_fraction'], expected):
                near(actual, value, 'PREDICTION_VALUE')
            direction = 0 if max(expected) <= 0 else (1 if expected[0] >= expected[1] else -1)
            require(type(prediction['direction']) is int and prediction['direction'] == direction,
                    'PREDICTION_ACTION')
    return dict(predictions=len(rows), prediction_cells=2*len(rows), full_grid=True)
