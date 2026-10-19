"""One fixed shadow-forecast diagnostic; no fitting, payoff path or I/O.

The evaluator uses exact rational arithmetic on serialized input numbers. Its
auditor rebuilds the aggregates independently using Decimal and different loss
algebra. Strict positive support predicates are evaluated before float export.
Neither predictive improvement nor this arithmetic audit confers qualification.
"""
import math
from decimal import Decimal as D, localcontext
from fractions import Fraction as F

DAY = 86400000
ARMS = ('old2021', 'recent2025', 'rolling')
BOUNDARIES = (1752624000000, 1755302400000, 1757980800000,
              1760572800000, 1763251200000, 1765843200000, 1767225600000)
LOCKS = dict(independent_confirmation=False, statistical_significance_established=False,
             economic_profit_established=False, g4=False, qualified_candidates=0,
             online_learning_proven=False, demo_opening_lock_unchanged=True)
PREDICTION_KEYS = {'t', 'predicted_net_fraction', 'direction', 'model_id'}


def need(ok, reason):
    if not ok:
        raise ValueError('PREDICTION_DIAGNOSTIC_'+reason)


def rational(value):
    need(type(value) in (int, float), 'NUMBER_TYPE')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    need(finite, 'NONFINITE')
    return F(str(value))


def export(value):
    if isinstance(value, F):
        try:
            out = float(value)
        except OverflowError as error:
            raise ValueError('PREDICTION_DIAGNOSTIC_OUTPUT_RANGE') from error
        need(math.isfinite(out) and (out != 0 or value == 0), 'OUTPUT_RANGE')
        return out
    if isinstance(value, dict):
        return {key: export(item) for key, item in value.items()}
    if isinstance(value, list):
        return [export(item) for item in value]
    return value


def evaluate(labels, streams, boundaries):
    """Score all 169 daily targets once, with no selection of dates or models."""
    need(isinstance(boundaries, (list, tuple)) and tuple(boundaries) == BOUNDARIES and
         all(type(t) is int for t in boundaries), 'BOUNDARIES')
    dates = list(range(BOUNDARIES[0], BOUNDARIES[-1], DAY))
    need(isinstance(labels, list) and [row['t'] for row in labels] == dates and
         all(type(row['t']) is int for row in labels), 'LABEL_GRID')
    need(isinstance(streams, dict) and set(streams) == set(ARMS), 'STREAM_SET')
    targets = []
    for row in labels:
        need(isinstance(row['y'], list) and len(row['y']) == 2, 'LABEL_SHAPE')
        targets.append([rational(value) for value in row['y']])
    losses = {arm: [] for arm in ARMS}
    for arm in ARMS:
        predictions = streams[arm]
        need(isinstance(predictions, list) and [row['t'] for row in predictions] == dates,
             'PREDICTION_GRID')
        for prediction, target in zip(predictions, targets):
            need(set(prediction) == PREDICTION_KEYS and type(prediction['t']) is int and
                 isinstance(prediction['model_id'], str) and bool(prediction['model_id']),
                 'PREDICTION_SCHEMA')
            values = prediction['predicted_net_fraction']
            need(isinstance(values, list) and len(values) == 2, 'PREDICTION_SHAPE')
            left, right = [rational(value) for value in values]
            action = 0 if max(left, right) <= 0 else (1 if left >= right else -1)
            need(type(prediction['direction']) is int and prediction['direction'] == action,
                 'PREDICTION_ACTION')
            pair = ((left-target[0])**2+(right-target[1])**2)/2
            directional = ((left-right)/2-(target[0]-target[1])/2)**2
            common = ((left+right)/2-(target[0]+target[1])/2)**2
            need(pair == directional+common, 'EXACT_DECOMPOSITION')
            losses[arm].append((pair, directional, common))

    def summary(start, end):
        indices = [i for i, t in enumerate(dates) if start <= t < end]
        count = len(indices)
        need(count > 0, 'EMPTY_SEGMENT')
        arms = {arm: {key: sum((losses[arm][i][column] for i in indices), F(0))/count
                      for column, key in enumerate(('pair_mse', 'direction_mse', 'common_mse'))}
                for arm in ARMS}
        reductions = {}
        for control in ('recent2025', 'old2021'):
            total = sum((losses[control][i][0]-losses['rolling'][i][0] for i in indices), F(0))
            reductions[control+'_minus_rolling'] = dict(sum=total, mean=total/count)
        return dict(start=start, end=end, count=count, arms=arms, paired_reduction=reductions)

    full = summary(BOUNDARIES[0], BOUNDARIES[-1])
    post = summary(BOUNDARIES[1], BOUNDARIES[-1])
    segments = [summary(start, end) for start, end in zip(BOUNDARIES, BOUNDARIES[1:])]
    monthly = [segment['paired_reduction']['recent2025_minus_rolling']['sum']
               for segment in segments[1:]]
    total, positive = sum(monthly, F(0)), sum(value > 0 for value in monthly)
    without_best = total-max(monthly)
    conditions = dict(positive_total=total > 0, at_least_three_positive_months=positive >= 3,
                      positive_without_best_month=without_best > 0)
    need(total == post['paired_reduction']['recent2025_minus_rolling']['sum'],
         'POSTINITIAL_PARTITION')
    supported = all(conditions.values())
    result = dict(status='SHADOW_PREDICTION_IMPROVEMENT' if supported else 'SHADOW_PREDICTION_UNSUPPORTED',
                  full=full, postinitial=post, segments=segments,
                  support=dict(comparison='recent2025_minus_rolling',
                               monthly_contribution_sums=monthly, positive_months=positive,
                               total_reduction=total, without_best_month_reduction=without_best,
                               conditions=conditions), **LOCKS)
    return export(result)


def audit(labels, streams, boundaries, result):
    """Independent Decimal reconstruction; never calls evaluate or its helpers.

    Prediction causal clocks and target-generation cash arithmetic are separately
    verified by the caller. This audit verifies the complete fixed scoring domain,
    all score cells, decomposition, support predicates and qualification locks.
    """
    def require(ok, reason):
        if not ok:
            raise ValueError('AUDIT_PREDICTION_DIAGNOSTIC_'+reason)

    def decimal(value):
        require(type(value) in (int, float), 'NUMBER_TYPE')
        out = D(str(value))
        require(out.is_finite(), 'NONFINITE')
        return out

    require(isinstance(boundaries, (list, tuple)) and tuple(boundaries) == BOUNDARIES and
            all(type(t) is int for t in boundaries), 'BOUNDARIES')
    grid = list(range(BOUNDARIES[0], BOUNDARIES[-1], DAY))
    require(len(grid) == 169 and isinstance(labels, list) and
            [row['t'] for row in labels] == grid and
            all(type(row['t']) is int for row in labels), 'LABEL_GRID')
    require(isinstance(streams, dict) and set(streams) == {'old2021', 'recent2025', 'rolling'},
            'STREAM_SET')
    # More than the exact digit span of squared finite binary64 decimal strings;
    # protects strict sign tests even for severe cancellation in synthetic cases.
    with localcontext() as ctx:
        ctx.prec = 1600
        cells = {}
        for arm in ('old2021', 'recent2025', 'rolling'):
            stream = streams[arm]
            require(isinstance(stream, list) and [row['t'] for row in stream] == grid,
                    'PREDICTION_GRID')
            arm_cells = []
            for target, prediction in zip(labels, stream):
                require(isinstance(target['y'], list) and len(target['y']) == 2, 'LABEL_SHAPE')
                require(set(prediction) == {'t', 'predicted_net_fraction', 'direction', 'model_id'} and
                        type(prediction['t']) is int and isinstance(prediction['model_id'], str) and
                        bool(prediction['model_id']), 'PREDICTION_SCHEMA')
                values = prediction['predicted_net_fraction']
                require(isinstance(values, list) and len(values) == 2, 'PREDICTION_SHAPE')
                p0, p1 = decimal(values[0]), decimal(values[1])
                y0, y1 = decimal(target['y'][0]), decimal(target['y'][1])
                action = (1 if p0 >= p1 else -1) if max(p0, p1) > 0 else 0
                require(type(prediction['direction']) is int and prediction['direction'] == action,
                        'PREDICTION_ACTION')
                e0, e1 = p0-y0, p1-y1
                direction = (e0-e1)**2/4
                common = (e0+e1)**2/4
                pair = (e0*e0+e1*e1)/2
                require(pair == direction+common, 'DECOMPOSITION_IDENTITY')
                arm_cells.append((pair, direction, common))
            cells[arm] = arm_cells

        def aggregate(left, right):
            first = (left-BOUNDARIES[0])//DAY
            last = (right-BOUNDARIES[0])//DAY
            n = last-first
            by_arm = {}
            for arm in ('old2021', 'recent2025', 'rolling'):
                totals = [D(0), D(0), D(0)]
                for row in cells[arm][first:last]:
                    for j in range(3):
                        totals[j] += row[j]
                by_arm[arm] = dict(pair_mse=totals[0]/n,
                                   direction_mse=totals[1]/n, common_mse=totals[2]/n)
            reductions = {}
            for control in ('recent2025', 'old2021'):
                # Subtract independently accumulated stream totals, rather than
                # sharing the evaluator's paired-row reduction implementation.
                total = sum((row[0] for row in cells[control][first:last]), D(0))
                total -= sum((row[0] for row in cells['rolling'][first:last]), D(0))
                reductions[control+'_minus_rolling'] = dict(sum=total, mean=total/n)
            return dict(start=left, end=right, count=n, arms=by_arm, paired_reduction=reductions)

        all_period = aggregate(BOUNDARIES[0], BOUNDARIES[-1])
        post_period = aggregate(BOUNDARIES[1], BOUNDARIES[-1])
        monthly_periods = [aggregate(BOUNDARIES[i], BOUNDARIES[i+1]) for i in range(6)]
        contributions = [row['paired_reduction']['recent2025_minus_rolling']['sum']
                         for row in monthly_periods[1:]]
        total = sum(contributions, D(0))
        positives = len([value for value in contributions if value > 0])
        remaining = total-max(contributions)
        conditions = dict(positive_total=total > 0, at_least_three_positive_months=positives >= 3,
                          positive_without_best_month=remaining > 0)
        supported = total > 0 and positives >= 3 and remaining > 0
        expected = dict(status='SHADOW_PREDICTION_IMPROVEMENT' if supported else 'SHADOW_PREDICTION_UNSUPPORTED',
                        full=all_period, postinitial=post_period, segments=monthly_periods,
                        support=dict(comparison='recent2025_minus_rolling',
                                     monthly_contribution_sums=contributions, positive_months=positives,
                                     total_reduction=total, without_best_month_reduction=remaining,
                                     conditions=conditions),
                        independent_confirmation=False, statistical_significance_established=False,
                        economic_profit_established=False, g4=False, qualified_candidates=0,
                        online_learning_proven=False, demo_opening_lock_unchanged=True)
        maximum_error = D(0)
        metric_count = 0

        def compare(want, actual, name):
            nonlocal maximum_error, metric_count
            if isinstance(want, dict):
                require(isinstance(actual, dict) and set(actual) == set(want), name+'_KEYS')
                for key in want:
                    compare(want[key], actual[key], name+'.'+key)
            elif isinstance(want, list):
                require(isinstance(actual, list) and len(actual) == len(want), name+'_COUNT')
                for i, (a, b) in enumerate(zip(want, actual)):
                    compare(a, b, name+'.'+str(i))
            elif isinstance(want, D):
                difference = abs(decimal(actual)-want)
                require(difference <= D('1e-18')+D('2e-12')*abs(want), name+'_NUMERIC')
                maximum_error = max(maximum_error, difference)
                metric_count += 1
            else:
                require(type(actual) is type(want) and actual == want, name+'_VALUE')

        compare(expected, result, 'RESULT')
    return dict(status='INDEPENDENT_SHADOW_PREDICTION_AUDIT_VERIFIED', dates=169,
                streams=3, loss_cells=169*3*3, reported_numeric_cells=metric_count,
                exact_decomposition_verified=True, maximum_absolute_error=format(maximum_error, '.6E'),
                decision_status=expected['status'])
