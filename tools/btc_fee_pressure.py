"""Frozen fee/weight development signal. No network, account or promotion API."""
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import io
import itertools
import math

import btc_old_output_model as execution

SERIES = ('obm_fees_btc_daily', 'obm_block_weight_wu_daily')
FIELDS = ['date', 'series_id', 'value', 'unit', 'frequency', 'release_version']
FIXED = (1, 0, 1)
NAMES = ('candidate', 'fixed', 'price', 'passive', 'chain-delay', 'execution-delay')


def require(ok, why):
    if not ok:
        raise ValueError(why)


def parse_series(raw, name, unit):
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8')))
    require(reader.fieldnames == FIELDS, 'FEE_INPUT_SCHEMA')
    values = {}; versions = set(); previous = None
    for r in reader:
        require(None not in r and all(v is not None for v in r.values()), 'FEE_INPUT_ROW_WIDTH')
        date = dt.date.fromisoformat(r['date'])
        require(str(date) == r['date'] and (previous is None or previous < date), 'FEE_INPUT_DATE_ORDER')
        previous = date
        require((r['series_id'], r['unit'], r['frequency']) == (name, unit, 'daily'), 'FEE_INPUT_IDENTITY')
        try:
            value = Decimal(r['value'])
        except InvalidOperation as e:
            raise ValueError('FEE_INPUT_VALUE') from e
        require(value.is_finite() and value >= 0, 'FEE_INPUT_VALUE')
        scaled = value * (Decimal(100000000) if unit == 'BTC' else 1)
        require(scaled == scaled.to_integral_value(), 'FEE_INPUT_PRECISION')
        values[str(date)] = value; versions.add(r['release_version'])
    require(bool(values) and versions == {'OBM v0.1.0'}, 'FEE_INPUT_RELEASE')
    return values


def required_dates():
    days = []
    for a, b in (('2020-12-01', '2021-12-30'), ('2024-12-01', '2026-01-01')):
        a, b = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
        days.extend(str(a + dt.timedelta(days=i)) for i in range((b-a).days))
    return days


def admit(fees_raw, weight_raw, dates=None):
    fees = parse_series(fees_raw, SERIES[0], 'BTC')
    weight = parse_series(weight_raw, SERIES[1], 'WU')
    result = {}
    for d in required_dates() if dates is None else dates:
        require(d in fees and d in weight, 'FEE_REQUIRED_DATE_MISSING:' + d)
        require(fees[d] > 0 and weight[d] > 0, 'FEE_REQUIRED_ZERO:' + d)
        result[d] = math.log(float(100000000 * fees[d] / weight[d]))
        require(math.isfinite(result[d]), 'FEE_LOG_NONFINITE')
    return result


def features(rows, log_density, start, end, lag_days=3):
    require(lag_days in (3, 4), 'FEE_FIXED_LAG')
    return execution.features(rows, log_density, start, end, lag_days=lag_days)


def grids():
    mechanism = [(a, 0, h) for a, h in itertools.product((-1, 1), (1, 2))]
    prices = [(0, b, h) for b, h in itertools.product((-1, 1), (1, 2))]
    return mechanism, prices


def complete(result):
    return all(not b['insufficient'] and b['quantity'] == 0 for b in result['books'])


def select(results, *, benchmark=False):
    best = None
    for i, r in enumerate(results):
        require(complete(r), 'SELECTION_INCOMPLETE_PATH')
        if not benchmark and any(b['stopped'] for b in r['books']):
            continue
        score = min(b['net'] for b in r['books'])
        key = (sum(b['turnover'] for b in r['books']), tuple(r['parameters']))
        if best is None or score > best[1] + 1e-6 or (abs(score-best[1]) <= 1e-6 and key < best[2]):
            best = (i, score, key)
    return None if best is None else best[0]


def training_decision(training):
    mechanism, prices = grids()
    require([r['parameters'] for r in training] == [list(p) for p in mechanism+prices], 'TRAINING_GRID_IDENTITY')
    chosen = select(training[:4]); price = select(training[4:], benchmark=True)
    result = dict(mechanism_index=chosen, price_index=None if price is None else price+4,
                  fixed_index=mechanism.index(FIXED), parameters=None,
                  status='NO_GO_TRAIN_RISK')
    if chosen is not None:
        r = training[chosen]; result['parameters'] = r['parameters']
        capacity = all(sum(c['complete'] for c in b['cycles']) >= 12 and len(b['active_weeks']) >= 8
                       for b in r['books'])
        result['status'] = 'TRAINING_ADMITTED' if capacity else 'INSUFFICIENT_TRAIN_CAPACITY'
    require(price is not None, 'MISSING_COMPLETE_PRICE_CONTROL')
    return result


def specifications(selection, training):
    require(selection == training_decision(training) and selection['status'] == 'TRAINING_ADMITTED',
            'EVALUATION_NOT_ADMITTED')
    chosen = tuple(selection['parameters'])
    price = tuple(training[selection['price_index']]['parameters'])
    return [('candidate', chosen, 3, {}), ('fixed', FIXED, 3, {}), ('price', price, 3, {}),
            ('passive', (0, 1, 1), 3, {'passive': True}), ('chain-delay', chosen, 4, {}),
            ('execution-delay', chosen, 3, {'latency': 2})]


def verdict(paths):
    require(len(paths) == 6, 'SIX_EVALUATION_IDENTITIES')
    candidate, fixed, price, passive, chain_delay, execution_delay = paths
    risk = any(b['stopped'] for r in (candidate, chain_delay, execution_delay) for b in r['books'])
    if not all(complete(r) for r in paths):
        return dict(status='INSUFFICIENT_INPUT_OR_EXECUTION', hard_risk=bool(risk),
                    reasons=['EXECUTION_OR_RESIDUAL'])
    reasons = []; base, stress = candidate['books']
    if base['net'] <= 0 or stress['net'] < 200:
        reasons.append('ABSOLUTE_NET')
    if any(b['net']-p['net'] < 50 for b, p in zip(candidate['books'], price['books'])):
        reasons.append('PRICE_INCREMENT')
    learned = candidate['parameters'] != list(FIXED)
    if learned and any(b['net']-p['net'] < 50 for b, p in zip(candidate['books'], fixed['books'])):
        reasons.append('LEARNING_INCREMENT')
    mid = next((d['equity']-10000 for d in stress['days'] if d['t'] == execution.millis('2025-07-01')), None)
    if mid is None or mid <= 0 or stress['net']-mid <= 0:
        reasons.append('HALF_YEAR_STABILITY')
    if any(b['net'] <= 0 for r in (chain_delay, execution_delay) for b in r['books']):
        reasons.append('DELAY_NET')
    metrics = [execution.concentration(b) for b in candidate['books']]
    diversity = any(c['complete_cycles'] < 12 or c['active_weeks'] < 8 or
                    any(c[k] is None or c[k] > .35 for k in ('largest_cycle_share', 'largest_week_share'))
                    for c in metrics) or metrics[1]['without_best_week'] <= 0
    status = ('NO_GO_RISK' if risk else 'NO_GO_ECONOMIC' if reasons else
              'INSUFFICIENT_DIVERSITY' if diversity else 'DEVELOPMENT_SUPPORTED')
    return dict(status=status, reasons=['CANDIDATE_OR_SENSITIVITY_HARD_STOP'] if risk else
                reasons or (['EVENTS_OR_CONCENTRATION'] if diversity else []),
                economic_failures=reasons, hard_risk=bool(risk), diversity_failed=diversity, learned=learned)
