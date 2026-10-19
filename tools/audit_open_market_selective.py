"""Independent Decimal/integers audit of the one frozen selective-action design.

No scorer import, model training, market I/O or portfolio calculations. Source
features, model causality and executable labels remain separately audited.
"""
from decimal import Decimal as D, localcontext
from math import gcd, isfinite

DAY = 86400000
STEP = 300000
START = 1625184000000  # 2021-07-02 UTC
END = 1633046400000    # 2021-10-01 UTC
APPLY_NOT_BEFORE = 1633132800000  # 2021-10-02 UTC
ELIGIBLE = 'ELIGIBLE_SELECTIVE_POLICY'
KEYS = {'schema_version', 'status', 'start', 'end', 'apply_not_before',
        'calendar_days', 'proposed_days', 'proposed_weeks', 'proposed_week_starts',
        'fit_count', 'alpha', 'beta', 'raw_beta', 'moments', 'kept_days',
        'vetoed_days', 'reasons'}


def require(condition, reason):
    if not condition:
        raise ValueError('AUDIT_OPEN_MARKET_SELECTIVE_'+reason)


def number(value):
    require(type(value) in (int, float), 'NUMBER_TYPE')
    try:
        finite = isfinite(value)
    except OverflowError:
        finite = False
    require(finite, 'NONFINITE')
    value = D(str(value))
    require(value.is_finite(), 'NONFINITE')
    return value


def ratio(numerator, denominator=1):
    require(denominator != 0, 'ZERO_DENOMINATOR')
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    common = gcd(numerator, denominator)
    return numerator//common, denominator//common


def plus(a, b):
    return ratio(a[0]*b[1]+b[0]*a[1], a[1]*b[1])


def minus(a, b):
    return plus(a, (-b[0], b[1]))


def times(a, b):
    return ratio(a[0]*b[0], a[1]*b[1])


def divided(a, b):
    return ratio(a[0]*b[1], a[1]*b[0])


def fraction_text(value):
    return str(value[0]) if value[1] == 1 else str(value[0])+'/'+str(value[1])


def parse(value):
    require(type(value) is str and value.count('/') <= 1, 'FRACTION_TYPE')
    parts = value.split('/')
    try:
        out = ratio(int(parts[0]), int(parts[1]) if len(parts) == 2 else 1)
    except (ValueError, TypeError):
        raise ValueError('AUDIT_OPEN_MARKET_SELECTIVE_FRACTION_FORMAT') from None
    require(fraction_text(out) == value, 'FRACTION_CANONICAL')
    return out


def decimal_ratio(value):
    return ratio(*value.as_integer_ratio())


def monday(t):
    return (t//DAY-(t//DAY+3) % 7)*DAY


def forecasts(predictions, dates=None):
    require(isinstance(predictions, list) and bool(predictions), 'PREDICTION_LIST')
    result = []
    for row in predictions:
        require(isinstance(row, dict) and
                set(row) == {'t', 'predicted_net_fraction', 'direction'}, 'PREDICTION_SCHEMA')
        t = row['t']
        require(type(t) is int and t % DAY == 0, 'PREDICTION_CLOCK')
        values = row['predicted_net_fraction']
        require(isinstance(values, list) and len(values) == 2, 'PREDICTION_SHAPE')
        long, short = [number(value) for value in values]
        action = 0 if max(long, short) <= 0 else (1 if long >= short else -1)
        require(type(row['direction']) is int and row['direction'] == action,
                'PREDICTION_ACTION')
        selected = long if action == 1 else short if action == -1 else None
        result.append((t, action, selected))
    actual_dates = [row[0] for row in result]
    require(all(right == left+DAY for left, right in zip(actual_dates, actual_dates[1:])),
            'PREDICTION_GRID')
    if dates is not None:
        require(actual_dates == dates, 'PREDICTION_GRID')
    return result


def equal(want, actual, name):
    if isinstance(want, dict):
        require(isinstance(actual, dict) and set(actual) == set(want), name+'_KEYS')
        for key in want:
            equal(want[key], actual[key], name+'.'+key)
    elif isinstance(want, list):
        require(isinstance(actual, list) and len(actual) == len(want), name+'_COUNT')
        for index, (left, right) in enumerate(zip(want, actual)):
            equal(left, right, name+'.'+str(index))
    else:
        require(type(actual) is type(want) and actual == want, name+'_VALUE')


def metadata(calibration):
    """Validate standalone receipt algebra; source binding belongs to audit_fit."""
    require(isinstance(calibration, dict) and set(calibration) == KEYS, 'CALIBRATION_SCHEMA')
    for key, value in dict(schema_version='open_market_selective_v1', start=START,
                           end=END, apply_not_before=APPLY_NOT_BEFORE, calendar_days=91).items():
        equal(value, calibration[key], 'METADATA_'+key)
    count, week_count = calibration['proposed_days'], calibration['proposed_weeks']
    require(type(count) is int and 0 <= count <= 91 and type(week_count) is int,
            'CAPACITY_TYPES')
    weeks = calibration['proposed_week_starts']
    possible = {monday(t) for t in range(START, END, DAY)}
    require(isinstance(weeks, list) and all(type(t) is int and t in possible for t in weeks)
            and weeks == sorted(set(weeks)) and len(weeks) == week_count,
            'WEEK_GRID')
    require((count == 0) == (week_count == 0) and week_count <= count and
            count <= sum(sum(monday(t) == w for t in range(START, END, DAY)) for w in weeks),
            'WEEK_CAPACITY')
    reasons = []
    if count < 30:
        reasons.append('INSUFFICIENT_PROPOSALS')
    if week_count < 8:
        reasons.append('INSUFFICIENT_WEEKS')
    if reasons:
        equal('CALIBRATION_CAPACITY_INSUFFICIENT', calibration['status'], 'CAPACITY_STATUS')
        equal(0, calibration['fit_count'], 'FIT_COUNT')
        for key in ('alpha', 'beta', 'raw_beta', 'moments', 'kept_days', 'vetoed_days'):
            equal(None, calibration[key], 'CAPACITY_'+key)
        equal(reasons, calibration['reasons'], 'CAPACITY_REASONS')
        return None
    equal(1, calibration['fit_count'], 'FIT_COUNT')
    moments = calibration['moments']
    require(isinstance(moments, dict) and
            set(moments) == {'mean_score', 'mean_utility', 'score_ss', 'cross_ss'}, 'MOMENTS_SCHEMA')
    values = {key: parse(value) for key, value in moments.items()}
    mean_s, mean_u = values['mean_score'], values['mean_utility']
    ss, cross = values['score_ss'], values['cross_ss']
    require(mean_s[0] > 0 and ss[0] >= 0, 'MOMENT_DOMAIN')
    if ss[0] == 0:
        require(cross[0] == 0, 'ZERO_VARIANCE_CROSS')
        equal(None, calibration['raw_beta'], 'RAW_BETA')
        beta = (0, 1)
        reasons.append('ZERO_SCORE_VARIANCE')
    else:
        raw = divided(cross, ss)
        equal(fraction_text(raw), calibration['raw_beta'], 'RAW_BETA')
        beta = raw if raw[0] > 0 else (0, 1)
    alpha = minus(mean_u, times(beta, mean_s))
    require(parse(calibration['alpha']) == alpha and parse(calibration['beta']) == beta,
            'COEFFICIENT_ALGEBRA')
    kept, vetoed = calibration['kept_days'], calibration['vetoed_days']
    require(type(kept) is int and type(vetoed) is int and kept >= 0 and vetoed >= 0
            and kept+vetoed == count, 'ACTION_COUNTS')
    if beta[0] <= 0:
        reasons.append('NONPOSITIVE_SLOPE')
    if kept == 0:
        reasons.append('NO_KEEP_GROUP')
    if vetoed == 0:
        reasons.append('NO_VETO_GROUP')
    equal(reasons, calibration['reasons'], 'IDENTIFICATION_REASONS')
    equal('NO_SELECTIVE_POLICY' if reasons else ELIGIBLE, calibration['status'], 'IDENTIFICATION_STATUS')
    return alpha, beta


def audit_fit(labels, predictions, start, end, calibration):
    """Independently reconstruct all proposal membership, clocks and OLS cells."""
    require(type(start) is int and type(end) is int and (start, end) == (START, END), 'FIT_SCOPE')
    dates = list(range(start, end, DAY))
    require(isinstance(labels, list) and len(labels) == len(dates), 'LABEL_GRID')
    proposals = []
    with localcontext() as ctx:
        ctx.prec = 1600
        predicted = forecasts(predictions, dates)
        for row, (t, action, score) in zip(labels, predicted):
            require(isinstance(row, dict) and {'t', 'label_end', 'y'} <= set(row)
                    and set(row) <= {'t', 'label_end', 'y', 'x'}, 'LABEL_SCHEMA')
            require(type(row['t']) is int and row['t'] == t, 'LABEL_GRID')
            require(type(row['label_end']) is int and row['label_end'] == t+DAY-STEP
                    and row['label_end'] <= end, 'LABEL_MATURITY')
            require(isinstance(row['y'], list) and len(row['y']) == 2, 'LABEL_SHAPE')
            target = [number(value) for value in row['y']]
            if action:
                proposals.append((t, score, target[0 if action == 1 else 1]))
        weeks = sorted({monday(row[0]) for row in proposals})
        count = len(proposals)
        reasons = (['INSUFFICIENT_PROPOSALS'] if count < 30 else []) + (
            ['INSUFFICIENT_WEEKS'] if len(weeks) < 8 else [])
        expected = dict(schema_version='open_market_selective_v1', status='CALIBRATION_CAPACITY_INSUFFICIENT',
                        start=start, end=end, apply_not_before=APPLY_NOT_BEFORE, calendar_days=91,
                        proposed_days=count, proposed_weeks=len(weeks), proposed_week_starts=weeks,
                        fit_count=0, alpha=None, beta=None, raw_beta=None, moments=None,
                        kept_days=None, vetoed_days=None, reasons=reasons)
        if not reasons:
            # Multiplication precedes division: these centered numerators are
            # exact for finite binary64 decimal-string input at precision 1600.
            sum_s = sum((s for _, s, _ in proposals), D(0))
            sum_u = sum((u for _, _, u in proposals), D(0))
            variance_n = count*sum((s*s for _, s, _ in proposals), D(0))-sum_s*sum_s
            covariance_n = count*sum((s*u for _, s, u in proposals), D(0))-sum_s*sum_u
            require(variance_n >= 0, 'DECIMAL_VARIANCE')
            mean_s = divided(decimal_ratio(sum_s), (count, 1))
            mean_u = divided(decimal_ratio(sum_u), (count, 1))
            ss = divided(decimal_ratio(variance_n), (count, 1))
            cross = divided(decimal_ratio(covariance_n), (count, 1))
            raw = divided(decimal_ratio(covariance_n), decimal_ratio(variance_n)) if variance_n else None
            beta = raw if raw is not None and raw[0] > 0 else (0, 1)
            alpha = minus(mean_u, times(beta, mean_s))
            # Exact integer sign avoids rounding an arbitrarily near-zero
            # calibrated value even when Decimal division has a recurring tail.
            kept = sum(plus(alpha, times(beta, decimal_ratio(s)))[0] > 0 for _, s, _ in proposals)
            reasons = []
            if not variance_n:
                reasons.append('ZERO_SCORE_VARIANCE')
            if beta[0] <= 0:
                reasons.append('NONPOSITIVE_SLOPE')
            if kept == 0:
                reasons.append('NO_KEEP_GROUP')
            if kept == count:
                reasons.append('NO_VETO_GROUP')
            expected.update(status='NO_SELECTIVE_POLICY' if reasons else ELIGIBLE,
                            fit_count=1, alpha=fraction_text(alpha), beta=fraction_text(beta),
                            raw_beta=fraction_text(raw) if raw is not None else None,
                            moments=dict(mean_score=fraction_text(mean_s), mean_utility=fraction_text(mean_u),
                                         score_ss=fraction_text(ss), cross_ss=fraction_text(cross)),
                            kept_days=kept, vetoed_days=count-kept, reasons=reasons)
        equal(expected, calibration, 'FIT')
        metadata(calibration)
    return dict(status='INDEPENDENT_SELECTIVE_FIT_AUDIT_VERIFIED', calendar_days=91,
                label_cells=182, proposed_days=count, proposed_weeks=len(weeks),
                decimal_precision=1600, exact_coefficient_strings_verified=True,
                decision_status=expected['status'])


def audit_apply(calibration, predictions, decisions):
    """Reconstruct veto-only decisions using exact integer-rational signs."""
    coefficients = metadata(calibration)
    require(calibration['status'] == ELIGIBLE and coefficients is not None, 'INELIGIBLE_APPLY')
    alpha, beta = coefficients
    with localcontext() as ctx:
        ctx.prec = 1600
        predicted = forecasts(predictions)
        require(predicted[0][0] >= APPLY_NOT_BEFORE, 'APPLY_CLOCK')
        require(isinstance(decisions, list) and len(decisions) == len(predicted), 'DECISION_GRID')
        kept_count = vetoed_count = cash_count = 0
        for prediction, decision in zip(predicted, decisions):
            t, action, selected = prediction
            value = plus(alpha, times(beta, decimal_ratio(selected))) if action else None
            kept = bool(action and value[0] > 0)
            vetoed = bool(action and not kept)
            expected = dict(t=t, original_direction=action, direction=action if kept else 0,
                            selected_prediction=fraction_text(decimal_ratio(selected)) if action else None,
                            calibrated_utility=fraction_text(value) if action else None,
                            kept=kept, vetoed=vetoed, reason='KEEP' if kept else 'VETO' if vetoed else 'CASH')
            equal(expected, decision, 'DECISION')
            kept_count += kept
            vetoed_count += vetoed
            cash_count += action == 0
    return dict(status='INDEPENDENT_SELECTIVE_APPLY_AUDIT_VERIFIED', dates=len(predicted),
                kept_days=kept_count, vetoed_days=vetoed_count, cash_days=cash_count,
                exact_boundary_verified=True, veto_only_verified=True, decimal_precision=1600)
