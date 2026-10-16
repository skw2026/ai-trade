"""Matched predictive diagnostics, never a trading or promotion evaluator.

Frozen linear models isolate incremental information from position sizing, costs
and stop rules. All outputs are descriptive on already-observed development data.
"""
import hashlib
import math

DAY = 86400000
HORIZONS = (1, 3, 7)
MODES = ('price', 'increment', 'placebo')
RIDGE = 0.1


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def mean(values):
    require(bool(values), 'EMPTY_VALUES')
    return math.fsum(values)/len(values)


def placebo_sign(t):
    key = 'information-attribution-v1:%d' % (t//(7*DAY))
    return 1 if hashlib.sha256(key.encode()).digest()[0] % 2 else -1


def design(row, mode):
    require(mode in MODES, 'UNKNOWN_MODE')
    x = row['x'][:3]
    if mode != 'price':
        x = x + [row['x'][3] * (placebo_sign(row['t']) if mode == 'placebo' else 1)]
    return x


def solve(matrix, target):
    n = len(target)
    a = [list(row)+[y] for row,y in zip(matrix, target)]
    require(n and all(len(row) == n+1 for row in a), 'MATRIX_SHAPE')
    for j in range(n):
        pivot = max(range(j,n), key=lambda i: abs(a[i][j]))
        a[j], a[pivot] = a[pivot], a[j]
        require(abs(a[j][j]) > 1e-12, 'SINGULAR_MATRIX')
        denominator = a[j][j]
        a[j] = [v/denominator for v in a[j]]
        for i in range(n):
            if i != j:
                weight = a[i][j]
                a[i] = [v-weight*w for v,w in zip(a[i], a[j])]
    return [row[-1] for row in a]


def fit(rows, mode):
    require(bool(rows), 'EMPTY_TRAINING')
    x = [design(r, mode) for r in rows]
    y = [r['y'] for r in rows]
    require(all(math.isfinite(v) for row in x for v in row) and
            all(math.isfinite(v) for v in y), 'NONFINITE_TRAINING')
    width = len(x[0])
    require(all(len(row) == width for row in x), 'DESIGN_SHAPE')
    center = [mean([row[j] for row in x]) for j in range(width)]
    scale = [math.sqrt(mean([(row[j]-center[j])**2 for row in x])) for j in range(width)]
    normalized = [[(v-mu)/sd if sd > 1e-12 else 0. for v,mu,sd in zip(row,center,scale)] for row in x]
    intercept = mean(y)
    matrix = [[mean([row[i]*row[j] for row in normalized]) + (RIDGE if i == j else 0.)
               for j in range(width)] for i in range(width)]
    target = [mean([row[j]*(value-intercept) for row,value in zip(normalized,y)]) for j in range(width)]
    return dict(mode=mode, center=center, scale=scale, coefficients=solve(matrix,target),
                intercept=intercept, ridge=RIDGE, training_rows=len(rows))


def predict(model, row):
    x = design(row, model['mode'])
    require(all(math.isfinite(v) for v in x), 'NONFINITE_PREDICTION')
    return model['intercept'] + math.fsum(beta*(v-mu)/sd if sd > 1e-12 else 0.
        for beta,v,mu,sd in zip(model['coefficients'],x,model['center'],model['scale']))


def build_samples(closes, features, start, end, horizon):
    require(horizon in HORIZONS and start % DAY == 0 and end % DAY == 0 and start < end, 'DOMAIN')
    rows = []; unavailable = []; unmatured = []
    for t in range(start, end, DAY):
        require(t in features, 'MISSING_FEATURE_DATE')
        f = features[t]
        require(f['latest_price_closed_at'] == t, 'FEATURE_CLOCK')
        needed = [t-i*DAY for i in range(8)]
        require(all(k in closes and math.isfinite(closes[k]) and closes[k] > 0 for k in needed), 'PRICE_HISTORY')
        if t+horizon*DAY > end:
            unmatured.append(t); continue
        require(t+horizon*DAY in closes and math.isfinite(closes[t+horizon*DAY]) and
                closes[t+horizon*DAY] > 0, 'MISSING_LABEL_PRICE')
        if f['z'] is None:
            unavailable.append(t); continue
        require(math.isfinite(f['z']), 'NONFINITE_SIGNAL')
        returns = [math.log(closes[t-i*DAY]/closes[t-(i+1)*DAY]) for i in range(7)]
        mu = mean(returns)
        x = [returns[0]*10000, math.log(closes[t]/closes[t-7*DAY])*10000,
             math.sqrt(mean([(v-mu)**2 for v in returns]))*10000, f['z']]
        rows.append(dict(t=t, label_end=t+horizon*DAY, x=x,
                         y=math.log(closes[t+horizon*DAY]/closes[t])*10000))
    return dict(rows=rows, unavailable_dates=unavailable, unmatured_dates=unmatured)


def window(rows, start, end):
    return [r for r in rows if start <= r['t'] < end and r['label_end'] <= end]


def date_hash(rows):
    return hashlib.sha256(','.join('%d:%d' % (r['t'],r['label_end']) for r in rows).encode()).hexdigest()


def hac7_se(values):
    """Calendar-lag covariance; missing dates aren't compressed. Descriptive only."""
    if len(values) < 2:
        return None
    require(len({t for t,_ in values}) == len(values), 'DUPLICATE_STATISTIC_DATE')
    mu = mean([v for _,v in values])
    residuals = {t: v-mu for t,v in values}
    variance_sum = math.fsum(v*v for v in residuals.values())
    for lag in range(1,8):
        covariance = math.fsum(v*residuals[t-lag*DAY] for t,v in residuals.items() if t-lag*DAY in residuals)
        variance_sum += 2*(1-lag/8)*covariance
    require(variance_sum >= -1e-8*max(1.,math.fsum(v*v for v in residuals.values())), 'NEGATIVE_HAC_VARIANCE')
    return math.sqrt(max(0.,variance_sum))/len(values)


def metrics(predictions):
    if not predictions:
        return dict(status='INSUFFICIENT_MATCHED_ROWS', rows=0)
    losses = {mode:[(r['y']-r[mode])**2 for r in predictions] for mode in MODES}
    mse = {mode:mean(values) for mode,values in losses.items()}
    differences = [(r['t'],a-b) for r,a,b in zip(predictions,losses['price'],losses['increment'])]
    delta = mean([v for _,v in differences])
    return dict(status='DESCRIPTIVE_ONLY' if len(predictions) >= 30 else 'INSUFFICIENT_MATCHED_ROWS',
        rows=len(predictions), date_sha256=date_hash(predictions), mse_bps_squared=mse,
        mae_bps={mode:mean([abs(r['y']-r[mode]) for r in predictions]) for mode in MODES},
        increment_mse_reduction_bps_squared=delta,
        increment_mse_reduction_percent=100*delta/mse['price'] if mse['price'] > 0 else None,
        placebo_mse_reduction_percent=100*(mse['price']-mse['placebo'])/mse['price'] if mse['price'] > 0 else None,
        paired_loss_difference_hac7_se=hac7_se(differences), p_value=None,
        independent_samples=False, profit_estimate=None)


def compare(training, evaluation):
    require(all(r['label_end'] <= min(e['t'] for e in evaluation) for r in training)
            if training and evaluation else True, 'TRAIN_LABEL_OVERLAP')
    if len(training) < 60 or len(evaluation) < 30:
        return dict(status='INSUFFICIENT_MATCHED_ROWS', train_rows=len(training), eval_rows=len(evaluation),
                    models={}, predictions=[], metrics=dict(status='INSUFFICIENT_MATCHED_ROWS', rows=len(evaluation)))
    require(len({r['t'] for r in training}) == len(training) and
            len({r['t'] for r in evaluation}) == len(evaluation), 'DUPLICATE_DATE')
    models = {mode:fit(training,mode) for mode in MODES}
    predictions = [dict(t=r['t'],label_end=r['label_end'],y=r['y'],
        **{mode:predict(model,r) for mode,model in models.items()}) for r in evaluation]
    return dict(status='DESCRIPTIVE_ONLY', train_rows=len(training), eval_rows=len(evaluation),
                train_date_sha256=date_hash(training), models=models, predictions=predictions,
                metrics=metrics(predictions))


def classify(stages):
    if any(s['status'] == 'INSUFFICIENT_MATCHED_ROWS' for s in stages):
        return 'INSUFFICIENT_MATCHED_ROWS'
    return ('DESCRIPTIVE_INCREMENT_ONLY' if all(s['increment_mse_reduction_bps_squared'] > 0 and
        s['mse_bps_squared']['increment'] < s['mse_bps_squared']['placebo'] for s in stages)
        else 'LINEAR_INCREMENT_NOT_STABLE')


def boundaries():
    return dict(promotion_eligible=False, independent_confirmation=False, qualified_candidates=0,
                g4=False, trading_paths_executed=0, risk_rules_changed=False,
                demo_opening_lock_unchanged=True, online_learning_proven=False)
