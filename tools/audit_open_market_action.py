"""Independent Decimal audit of fixed shadow action-label diagnostics.

No evaluator import, model fitting, execution replay, I/O or portfolio accounting.
Daily target utility remains an isolated label; it is never cumulative capital.
Source-label and model-causality verification are separate caller obligations.
"""
from decimal import Decimal as D, localcontext

DAY = 86400000
ARMS = ('old2021', 'recent2025', 'rolling')
BOUNDARIES = (1752624000000, 1755302400000, 1757980800000,
              1760572800000, 1763251200000, 1765843200000, 1767225600000)


def audit(labels, streams, boundaries, result):
    """Rebuild every action cell, nine-way transition and endpoint independently."""
    def require(ok, reason):
        if not ok:
            raise ValueError('AUDIT_OPEN_MARKET_ACTION_'+reason)

    def number(value):
        require(type(value) in (int, float), 'NUMBER_TYPE')
        out = D(str(value))
        require(out.is_finite(), 'NONFINITE')
        return out

    require(isinstance(boundaries, (list, tuple)) and tuple(boundaries) == BOUNDARIES and
            all(type(t) is int for t in boundaries), 'BOUNDARIES')
    dates = list(range(BOUNDARIES[0], BOUNDARIES[-1], DAY))
    require(len(dates) == 169 and isinstance(labels, list) and
            [row['t'] for row in labels] == dates and
            all(type(row['t']) is int for row in labels), 'LABEL_GRID')
    require(isinstance(streams, dict) and set(streams) == set(ARMS), 'STREAM_SET')
    with localcontext() as ctx:
        # Covers exact squared finite binary64 decimal strings before division.
        ctx.prec = 1600
        targets, oracles = [], []
        for row in labels:
            require(isinstance(row['y'], list) and len(row['y']) == 2, 'LABEL_SHAPE')
            target = [number(value) for value in row['y']]
            targets.append(target)
            oracles.append(max(D(0), *target))
        cells = {}
        for arm in ARMS:
            predictions = streams[arm]
            require(isinstance(predictions, list) and [row['t'] for row in predictions] == dates,
                    'PREDICTION_GRID')
            arm_cells = []
            for prediction, target, oracle in zip(predictions, targets, oracles):
                require(set(prediction) == {'t', 'predicted_net_fraction', 'direction', 'model_id'} and
                        type(prediction['t']) is int and isinstance(prediction['model_id'], str) and
                        bool(prediction['model_id']), 'PREDICTION_SCHEMA')
                values = prediction['predicted_net_fraction']
                require(isinstance(values, list) and len(values) == 2, 'PREDICTION_SHAPE')
                p0, p1 = number(values[0]), number(values[1])
                expected_action = 0 if max(p0, p1) <= 0 else (1 if p0 >= p1 else -1)
                action = prediction['direction']
                require(type(action) is int and action == expected_action, 'PREDICTION_ACTION')
                utility = target[0] if action == 1 else target[1] if action == -1 else D(0)
                selected = p0 if action == 1 else p1 if action == -1 else D(0)
                regret = oracle-utility
                require(regret >= 0, 'ORACLE_REGRET')
                # Direct errors, independent of action-dependent summaries.
                mse = ((p0-target[0])**2+(p1-target[1])**2)/2
                arm_cells.append(dict(action=action, utility=utility, selected=selected,
                                      regret=regret, mse=mse))
            cells[arm] = arm_cells

        def summary(left, right):
            first = (left-BOUNDARIES[0])//DAY
            last = (right-BOUNDARIES[0])//DAY
            count = last-first
            arms = {}
            for arm in ARMS:
                selected = cells[arm][first:last]
                utility = sum((row['utility'] for row in selected), D(0))
                predicted = sum((row['selected'] for row in selected), D(0))
                arms[arm] = dict(
                    noncash_days=sum(row['action'] != 0 for row in selected),
                    positive_action_labels=sum(row['action'] != 0 and row['utility'] > 0 for row in selected),
                    negative_action_labels=sum(row['action'] != 0 and row['utility'] < 0 for row in selected),
                    zero_action_labels=sum(row['action'] != 0 and row['utility'] == 0 for row in selected),
                    missed_positive_cash=sum(cells[arm][i]['action'] == 0 and oracles[i] > 0
                                             for i in range(first, last)),
                    utility_sum=utility, utility_mean=utility/count,
                    regret_sum=sum((row['regret'] for row in selected), D(0)),
                    predicted_selected_sum=predicted, calibration_gap_sum=predicted-utility,
                    pair_mse=sum((row['mse'] for row in selected), D(0))/count)
                require(arms[arm]['noncash_days'] == arms[arm]['positive_action_labels']+
                        arms[arm]['negative_action_labels']+arms[arm]['zero_action_labels'],
                        'ACTION_SIGN_PARTITION')
            comparisons = {}
            for control in ('recent2025', 'old2021'):
                transition = {}
                for before in (-1, 0, 1):
                    for after in (-1, 0, 1):
                        group = [i for i in range(first, last)
                                 if cells[control][i]['action'] == before and
                                 cells['rolling'][i]['action'] == after]
                        deltas = [cells['rolling'][i]['utility']-cells[control][i]['utility']
                                  for i in group]
                        transition[str(before)+':'+str(after)] = dict(
                            days=len(group),
                            utility_delta=sum(deltas, D(0)),
                            mse_reduction_sum=sum((cells[control][i]['mse']-cells['rolling'][i]['mse']
                                                   for i in group), D(0)),
                            positive_delta_days=sum(value > 0 for value in deltas),
                            negative_delta_days=sum(value < 0 for value in deltas),
                            zero_delta_days=sum(value == 0 for value in deltas),
                            positive_delta_sum=sum((value for value in deltas if value > 0), D(0)),
                            negative_delta_sum=sum((value for value in deltas if value < 0), D(0)))
                parts = {}
                for name, changed in (('changed', True), ('unchanged', False)):
                    group = [cell for key, cell in transition.items()
                             if (key.split(':')[0] != key.split(':')[1]) == changed]
                    parts[name] = dict(days=sum(cell['days'] for cell in group),
                                       utility_delta=sum((cell['utility_delta'] for cell in group), D(0)),
                                       mse_reduction_sum=sum((cell['mse_reduction_sum'] for cell in group), D(0)),
                                       positive_delta_days=sum(cell['positive_delta_days'] for cell in group),
                                       negative_delta_days=sum(cell['negative_delta_days'] for cell in group),
                                       zero_delta_days=sum(cell['zero_delta_days'] for cell in group),
                                       positive_delta_sum=sum((cell['positive_delta_sum'] for cell in group), D(0)),
                                       negative_delta_sum=sum((cell['negative_delta_sum'] for cell in group), D(0)))
                utility_delta = arms['rolling']['utility_sum']-arms[control]['utility_sum']
                regret_reduction = arms[control]['regret_sum']-arms['rolling']['regret_sum']
                mse_reduction = sum((cell['mse_reduction_sum'] for cell in transition.values()), D(0))
                require(regret_reduction == utility_delta, 'PAIRED_REGRET_IDENTITY')
                require(parts['unchanged']['utility_delta'] == 0, 'UNCHANGED_UTILITY_IDENTITY')
                require(parts['changed']['utility_delta'] == utility_delta and
                        sum(cell['days'] for cell in transition.values()) == count,
                        'TRANSITION_PARTITION')
                comparisons['rolling_minus_'+control] = dict(
                    utility_delta=utility_delta, regret_reduction=regret_reduction,
                    changed_days=parts['changed']['days'], mse_reduction_sum=mse_reduction,
                    changed=parts['changed'], unchanged=parts['unchanged'], transitions=transition)
            return dict(start=left, end=right, count=count,
                        oracle_sum=sum(oracles[first:last], D(0)), arms=arms, comparisons=comparisons)

        full = summary(BOUNDARIES[0], BOUNDARIES[-1])
        post = summary(BOUNDARIES[1], BOUNDARIES[-1])
        segments = [summary(BOUNDARIES[i], BOUNDARIES[i+1]) for i in range(6)]
        monthly = [segment['comparisons']['rolling_minus_recent2025']['utility_delta']
                   for segment in segments[1:]]
        total = sum(monthly, D(0))
        positives = sum(value > 0 for value in monthly)
        without_best = total-max(monthly)
        candidate = post['arms']['rolling']
        absolute_without_best = candidate['utility_sum']-max(
            segment['arms']['rolling']['utility_sum'] for segment in segments[1:])
        require(total == post['comparisons']['rolling_minus_recent2025']['utility_delta'],
                'POSTINITIAL_PARTITION')
        conditions = dict(positive_action_utility=candidate['utility_sum'] > 0,
                          positive_increment=total > 0,
                          at_least_three_positive_months=positives >= 3,
                          positive_without_best_month=without_best > 0,
                          positive_absolute_without_best_month=absolute_without_best > 0,
                          action_capacity=candidate['noncash_days'] >= 30)
        expected = dict(status='ACTION_LABEL_IMPROVEMENT' if all(conditions.values()) else 'ACTION_LABEL_UNSUPPORTED',
                        full=full, postinitial=post, segments=segments,
                        support=dict(comparison='rolling_minus_recent2025', monthly_utility_deltas=monthly,
                                     positive_months=positives, utility_delta_total=total,
                                     without_best_month_delta=without_best,
                                     candidate_utility_sum=candidate['utility_sum'],
                                     candidate_without_best_month_utility=absolute_without_best,
                                     candidate_noncash_days=candidate['noncash_days'], conditions=conditions),
                        independent_confirmation=False, statistical_significance_established=False,
                        economic_profit_established=False, portfolio_simulated=False, tradable_returns=False,
                        g4=False, qualified_candidates=0, online_learning_proven=False,
                        demo_opening_lock_unchanged=True)
        maximum_error, numeric_count = D(0), 0

        def compare(want, actual, name):
            nonlocal maximum_error, numeric_count
            if isinstance(want, dict):
                require(isinstance(actual, dict) and set(actual) == set(want), name+'_KEYS')
                for key in want:
                    compare(want[key], actual[key], name+'.'+key)
            elif isinstance(want, list):
                require(isinstance(actual, list) and len(actual) == len(want), name+'_COUNT')
                for i, (a, b) in enumerate(zip(want, actual)):
                    compare(a, b, name+'.'+str(i))
            elif isinstance(want, D):
                difference = abs(number(actual)-want)
                require(difference <= D('1e-18')+D('2e-12')*abs(want), name+'_NUMERIC')
                maximum_error = max(maximum_error, difference)
                numeric_count += 1
            else:
                require(type(actual) is type(want) and actual == want, name+'_VALUE')

        compare(expected, result, 'RESULT')
    return dict(status='INDEPENDENT_ACTION_DIAGNOSTIC_AUDIT_VERIFIED', dates=169, streams=3,
                action_cells=169*3, transition_cells=8*2*9,
                paired_regret_identity_verified=True, unchanged_action_utility_delta_zero=True,
                reported_numeric_cells=numeric_count, maximum_absolute_error=format(maximum_error, '.6E'),
                decision_status=expected['status'])
