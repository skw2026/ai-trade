"""Frozen forecast-to-action label attribution, not a portfolio backtest."""
from fractions import Fraction as F

from open_market_prediction_diagnostic import ARMS, BOUNDARIES, DAY, rational, export

LOCKS = dict(independent_confirmation=False, statistical_significance_established=False,
             economic_profit_established=False, portfolio_simulated=False, tradable_returns=False,
             g4=False, qualified_candidates=0, online_learning_proven=False,
             demo_opening_lock_unchanged=True)


def need(ok, reason):
    if not ok:
        raise ValueError('ACTION_DIAGNOSTIC_'+reason)


def evaluate(labels, streams, boundaries):
    need(isinstance(boundaries, (list, tuple)) and tuple(boundaries) == BOUNDARIES and
         all(type(t) is int for t in boundaries), 'BOUNDARIES')
    dates = list(range(BOUNDARIES[0], BOUNDARIES[-1], DAY))
    need(isinstance(labels, list) and [row['t'] for row in labels] == dates and
         all(type(row['t']) is int for row in labels), 'LABEL_GRID')
    need(isinstance(streams, dict) and set(streams) == set(ARMS), 'STREAM_SET')
    targets = []
    for row in labels:
        need(isinstance(row['y'], list) and len(row['y']) == 2, 'LABEL_SHAPE')
        targets.append([rational(v) for v in row['y']])
    oracle = [max(F(0), *y) for y in targets]
    cells = {}
    for arm in ARMS:
        ps = streams[arm]
        need(isinstance(ps, list) and [p['t'] for p in ps] == dates, 'PREDICTION_GRID')
        cells[arm] = []
        for p, y in zip(ps, targets):
            need(set(p) == {'t', 'predicted_net_fraction', 'direction', 'model_id'} and
                 type(p['t']) is int and isinstance(p['model_id'], str) and bool(p['model_id']),
                 'PREDICTION_SCHEMA')
            need(isinstance(p['predicted_net_fraction'], list) and
                 len(p['predicted_net_fraction']) == 2, 'PREDICTION_SHAPE')
            values = [rational(v) for v in p['predicted_net_fraction']]
            action = 0 if max(values) <= 0 else (1 if values[0] >= values[1] else -1)
            need(type(p['direction']) is int and p['direction'] == action, 'PREDICTION_ACTION')
            index = 0 if action == 1 else 1
            utility = y[index] if action else F(0)
            predicted = values[index] if action else F(0)
            mse = sum(((v-z)**2 for v, z in zip(values, y)), F(0))/2
            cells[arm].append(dict(action=action, utility=utility, predicted=predicted, mse=mse))

    def summary(start, end):
        ix = [i for i, t in enumerate(dates) if start <= t < end]
        count = len(ix)
        arms = {}
        for name in ARMS:
            rows = [cells[name][i] for i in ix]
            utility = sum((r['utility'] for r in rows), F(0))
            predicted = sum((r['predicted'] for r in rows), F(0))
            arms[name] = dict(noncash_days=sum(r['action'] != 0 for r in rows),
                positive_action_labels=sum(r['action'] != 0 and r['utility'] > 0 for r in rows),
                negative_action_labels=sum(r['action'] != 0 and r['utility'] < 0 for r in rows),
                zero_action_labels=sum(r['action'] != 0 and r['utility'] == 0 for r in rows),
                missed_positive_cash=sum(cells[name][i]['action'] == 0 and oracle[i] > 0 for i in ix),
                utility_sum=utility, utility_mean=utility/count,
                regret_sum=sum((oracle[i]-cells[name][i]['utility'] for i in ix), F(0)),
                predicted_selected_sum=predicted, calibration_gap_sum=predicted-utility,
                pair_mse=sum((r['mse'] for r in rows), F(0))/count)
        comparisons = {}
        for control in ('recent2025', 'old2021'):
            def group(indices):
                deltas = [cells['rolling'][i]['utility']-cells[control][i]['utility'] for i in indices]
                return dict(days=len(indices),
                    utility_delta=sum(deltas, F(0)),
                    positive_delta_days=sum(v > 0 for v in deltas),
                    negative_delta_days=sum(v < 0 for v in deltas),
                    zero_delta_days=sum(v == 0 for v in deltas),
                    positive_delta_sum=sum((v for v in deltas if v > 0), F(0)),
                    negative_delta_sum=sum((v for v in deltas if v < 0), F(0)),
                    mse_reduction_sum=sum((cells[control][i]['mse']-cells['rolling'][i]['mse'] for i in indices), F(0)))
            total = group(ix)
            changed = [i for i in ix if cells[control][i]['action'] != cells['rolling'][i]['action']]
            unchanged = [i for i in ix if cells[control][i]['action'] == cells['rolling'][i]['action']]
            transitions = {str(a)+':'+str(b): group([i for i in ix if
                cells[control][i]['action'] == a and cells['rolling'][i]['action'] == b])
                for a in (-1, 0, 1) for b in (-1, 0, 1)}
            regret_reduction = arms[control]['regret_sum']-arms['rolling']['regret_sum']
            need(regret_reduction == total['utility_delta'], 'REGRET_IDENTITY')
            comparisons['rolling_minus_'+control] = dict(utility_delta=total['utility_delta'],
                regret_reduction=regret_reduction, changed_days=len(changed),
                mse_reduction_sum=total['mse_reduction_sum'], changed=group(changed),
                unchanged=group(unchanged), transitions=transitions)
        return dict(start=start, end=end, count=count,
                    oracle_sum=sum((oracle[i] for i in ix), F(0)), arms=arms, comparisons=comparisons)

    full = summary(BOUNDARIES[0], BOUNDARIES[-1])
    post = summary(BOUNDARIES[1], BOUNDARIES[-1])
    segments = [summary(a, b) for a, b in zip(BOUNDARIES, BOUNDARIES[1:])]
    monthly = [s['comparisons']['rolling_minus_recent2025']['utility_delta'] for s in segments[1:]]
    candidate_monthly = [s['arms']['rolling']['utility_sum'] for s in segments[1:]]
    total, positive = sum(monthly, F(0)), sum(v > 0 for v in monthly)
    candidate = post['arms']['rolling']
    without_best = total-max(monthly)
    absolute_without_best = candidate['utility_sum']-max(candidate_monthly)
    conditions = dict(positive_action_utility=candidate['utility_sum'] > 0,
        positive_increment=total > 0, at_least_three_positive_months=positive >= 3,
        positive_without_best_month=without_best > 0,
        action_capacity=candidate['noncash_days'] >= 30,
        positive_absolute_without_best_month=absolute_without_best > 0)
    return export(dict(status='ACTION_LABEL_IMPROVEMENT' if all(conditions.values()) else 'ACTION_LABEL_UNSUPPORTED',
        full=full, postinitial=post, segments=segments,
        support=dict(comparison='rolling_minus_recent2025', monthly_utility_deltas=monthly,
            positive_months=positive, utility_delta_total=total, without_best_month_delta=without_best,
            candidate_utility_sum=candidate['utility_sum'], candidate_noncash_days=candidate['noncash_days'],
            candidate_without_best_month_utility=absolute_without_best, conditions=conditions), **LOCKS))
