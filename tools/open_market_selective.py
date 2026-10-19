"""One exact monotone affine calibration of selected, mature daily proposals.

Pure functions only: no model fitting beyond the declared two coefficients,
portfolio simulation, I/O, threshold search or direction/size changes. Fraction
strings are canonical ``str(Fraction)`` values, never rounded action thresholds.
Input/model provenance and exact evaluation ranges are caller audit obligations.
"""
import math
from fractions import Fraction as F

DAY, STEP = 86400000, 300000
CALIBRATION_START, CALIBRATION_END = 1625184000000, 1633046400000
APPLY_NOT_BEFORE = 1633132800000
SCHEMA = 'open_market_selective_v1'
ELIGIBLE = 'ELIGIBLE_SELECTIVE_POLICY'
CAPACITY = 'CALIBRATION_CAPACITY_INSUFFICIENT'
NO_POLICY = 'NO_SELECTIVE_POLICY'
CALIBRATION_KEYS = {'schema_version', 'status', 'start', 'end', 'apply_not_before',
    'calendar_days', 'proposed_days', 'proposed_weeks', 'proposed_week_starts',
    'fit_count', 'alpha', 'beta', 'raw_beta', 'moments', 'kept_days', 'vetoed_days', 'reasons'}
MOMENT_KEYS = {'mean_score', 'mean_utility', 'score_ss', 'cross_ss'}


def need(ok, reason):
    if not ok:
        raise ValueError('SELECTIVE_' + reason)


def rational(value):
    need(type(value) in (int, float), 'NUMBER_TYPE')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    need(finite, 'NONFINITE')
    return F(str(value))


def canonical_fraction(value):
    need(type(value) is str, 'FRACTION_TYPE')
    try:
        parsed = F(value)
    except (ValueError, ZeroDivisionError, OverflowError) as error:
        raise ValueError('SELECTIVE_FRACTION_FORMAT') from error
    need(str(parsed) == value, 'FRACTION_CANONICAL')
    return parsed


def monday(t):
    days = t // DAY
    return (days - (days + 3) % 7) * DAY


def prediction_grid(predictions):
    need(isinstance(predictions, list) and bool(predictions), 'PREDICTION_LIST')
    dates, records = [], []
    for p in predictions:
        need(isinstance(p, dict) and set(p) == {'t', 'predicted_net_fraction', 'direction'},
             'PREDICTION_SCHEMA')
        t = p['t']
        need(type(t) is int and t % DAY == 0, 'PREDICTION_CLOCK')
        need(not dates or t == dates[-1] + DAY, 'PREDICTION_DAILY_GRID')
        values = p['predicted_net_fraction']
        need(isinstance(values, list) and len(values) == 2, 'PREDICTION_SHAPE')
        left, right = map(rational, values)
        direction = 0 if max(left, right) <= 0 else (1 if left >= right else -1)
        need(type(p['direction']) is int and p['direction'] == direction, 'PREDICTION_ACTION')
        dates.append(t)
        records.append((direction, left if direction == 1 else right if direction == -1 else None))
    return dates, records


def fit(labels, predictions, start, end):
    """Fit once on noncash proposals in the fixed 91-day calibration calendar."""
    need(type(start) is int and type(end) is int and
         (start, end) == (CALIBRATION_START, CALIBRATION_END), 'CALIBRATION_DOMAIN')
    dates, proposals = prediction_grid(predictions)
    need(dates == list(range(start, end, DAY)), 'CALIBRATION_PREDICTION_GRID')
    need(isinstance(labels, list) and len(labels) == len(dates), 'LABEL_GRID')
    selected, week_starts = [], set()
    for row, t, (direction, score) in zip(labels, dates, proposals):
        need(isinstance(row, dict) and {'t', 'label_end', 'y'} <= set(row) <=
             {'t', 'label_end', 'y', 'x'}, 'LABEL_SCHEMA')
        need(type(row['t']) is int and row['t'] == t, 'LABEL_GRID')
        need(type(row['label_end']) is int and row['label_end'] == t + DAY - STEP and
             row['label_end'] <= end, 'LABEL_MATURITY')
        need(isinstance(row['y'], list) and len(row['y']) == 2, 'LABEL_SHAPE')
        targets = list(map(rational, row['y']))
        if direction:
            selected.append((score, targets[0 if direction == 1 else 1]))
            week_starts.add(monday(t))
    count = len(selected)
    result = dict(schema_version=SCHEMA, status=CAPACITY, start=start, end=end,
                  apply_not_before=APPLY_NOT_BEFORE, calendar_days=len(dates),
                  proposed_days=count, proposed_weeks=len(week_starts),
                  proposed_week_starts=sorted(week_starts), fit_count=0,
                  alpha=None, beta=None, raw_beta=None, moments=None,
                  kept_days=None, vetoed_days=None, reasons=[])
    if count < 30:
        result['reasons'].append('INSUFFICIENT_PROPOSALS')
    if len(week_starts) < 8:
        result['reasons'].append('INSUFFICIENT_WEEKS')
    if result['reasons']:
        return result  # No coefficient/moment fit before capacity admission.
    mean_score = sum((s for s, _ in selected), F(0)) / count
    mean_utility = sum((u for _, u in selected), F(0)) / count
    score_ss = sum(((s - mean_score) ** 2 for s, _ in selected), F(0))
    cross_ss = sum(((s - mean_score) * (u - mean_utility) for s, u in selected), F(0))
    raw_beta = cross_ss / score_ss if score_ss else None
    beta = max(F(0), raw_beta) if raw_beta is not None else F(0)
    alpha = mean_utility - beta * mean_score
    kept = sum(alpha + beta * s > 0 for s, _ in selected)
    result.update(status=ELIGIBLE, fit_count=1, alpha=str(alpha), beta=str(beta),
                  raw_beta=str(raw_beta) if raw_beta is not None else None,
                  moments={key: str(value) for key, value in dict(mean_score=mean_score,
                      mean_utility=mean_utility, score_ss=score_ss, cross_ss=cross_ss).items()},
                  kept_days=kept, vetoed_days=count-kept)
    for failed, reason in ((score_ss == 0, 'ZERO_SCORE_VARIANCE'),
                           (beta <= 0, 'NONPOSITIVE_SLOPE'),
                           (kept == 0, 'NO_KEEP_GROUP'),
                           (kept == count, 'NO_VETO_GROUP')):
        if failed:
            result['reasons'].append(reason)
    if result['reasons']:
        result['status'] = NO_POLICY
    return result


def eligible_coefficients(calibration):
    """Reject malformed/degenerate policies; source-label binding is audit_fit's job."""
    c = calibration
    need(isinstance(c, dict) and set(c) == CALIBRATION_KEYS, 'CALIBRATION_SCHEMA')
    need(c['schema_version'] == SCHEMA and c['status'] == ELIGIBLE, 'POLICY_NOT_ELIGIBLE')
    for key, value in (('start', CALIBRATION_START), ('end', CALIBRATION_END),
                       ('apply_not_before', APPLY_NOT_BEFORE), ('calendar_days', 91), ('fit_count', 1)):
        need(type(c[key]) is int and c[key] == value, 'CALIBRATION_METADATA')
    count, weeks = c['proposed_days'], c['proposed_weeks']
    need(type(count) is int and 30 <= count <= 91 and type(weeks) is int and
         8 <= weeks <= count, 'CALIBRATION_CAPACITY')
    starts = c['proposed_week_starts']
    need(isinstance(starts, list) and len(starts) == weeks and
         all(type(t) is int and monday(t) == t and
             monday(CALIBRATION_START) <= t <= monday(CALIBRATION_END - DAY) for t in starts) and
         starts == sorted(set(starts)), 'CALIBRATION_WEEKS')
    keep, veto = c['kept_days'], c['vetoed_days']
    need(type(keep) is int and type(veto) is int and keep > 0 and veto > 0 and
         keep + veto == count and c['reasons'] == [], 'CALIBRATION_GROUPS')
    alpha, beta, raw_beta = [canonical_fraction(c[k]) for k in ('alpha', 'beta', 'raw_beta')]
    need(isinstance(c['moments'], dict) and set(c['moments']) == MOMENT_KEYS, 'MOMENT_SCHEMA')
    moments = {k: canonical_fraction(v) for k, v in c['moments'].items()}
    need(moments['mean_score'] > 0 and moments['score_ss'] > 0 and beta > 0 and
         beta == raw_beta == moments['cross_ss'] / moments['score_ss'] and
         alpha == moments['mean_utility'] - beta * moments['mean_score'], 'COEFFICIENT_IDENTITY')
    return alpha, beta


def apply(calibration, predictions):
    """Keep or veto an original action; cash remains cash, with no side/size changes."""
    alpha, beta = eligible_coefficients(calibration)
    dates, proposals = prediction_grid(predictions)
    need(dates[0] >= APPLY_NOT_BEFORE, 'APPLICATION_EMBARGO')
    output = []
    for t, (direction, score) in zip(dates, proposals):
        value = alpha + beta * score if direction else None
        keep = bool(direction and value > 0)
        veto = bool(direction and not keep)
        output.append(dict(t=t, original_direction=direction, direction=direction if keep else 0,
                           selected_prediction=str(score) if direction else None,
                           calibrated_utility=str(value) if direction else None,
                           kept=keep, vetoed=veto,
                           reason='CASH' if not direction else 'KEEP' if keep else 'VETO'))
    return output
