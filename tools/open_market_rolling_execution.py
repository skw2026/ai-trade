"""Fixed-schedule PV model switching without resetting the reference account.

The schedule contains prefit models, not permission to train inside this loop.
The caller binds monthly day-16 dates; this executor requires ordered daily UTC
dates and causal model cutoffs. Daily execution is unchanged from the frozen
open_market_execution layer, including the final-day terminal exception.

The top-level ``rolling`` boolean declares the requested governance mode.
Governance schema:
  scheduled_updates: {t, model_id}, excluding the mandatory initial model.
  applied_updates: {t, model_id, reason=INITIAL|SCHEDULED}, including initial.
  blocked_updates: {t, model_id, reason}; priority STATIC_REFERENCE,
    EXECUTION_INSUFFICIENT, HARD_STOP, PERFORMANCE_FREEZE.
  active_model_by_date: {t, model_id} for every decision date.
  performance_freeze_time: first paired-account alert close time, or None,
    recorded even for the static reference. This latch never recovers.
After execution insufficiency only predictions/governance continue; account
observation remains truncated, exactly as in the frozen execution layer.
"""
import copy
import math
from decimal import Decimal, ROUND_FLOOR

from btc_old_output_model import DAY, STEP, need
from open_market_execution import ExactCapacityBook, HORIZON, TERMINAL_RESERVE_BARS
from open_market_learning import predict


def simulate(rows, fs, schedule, start, end, latency=1, rolling=True):
    need(type(start) is int and type(end) is int and
         start % DAY == end % DAY == 0 and start < end, 'PERIOD')
    need(type(latency) is int and latency in (1, 2), 'LATENCY')
    need(type(rolling) is bool, 'ROLLING_MODE')
    need(isinstance(schedule, list) and bool(schedule), 'SCHEDULE_REQUIRED')
    seen, previous = set(), None
    for entry in schedule:
        need(isinstance(entry, dict) and set(entry) == {'t', 'model_id', 'model'}, 'SCHEDULE_SCHEMA')
        t, identity, model = entry['t'], entry['model_id'], entry['model']
        need(type(t) is int and start <= t < end and t % DAY == 0 and
             (previous is None or t > previous), 'SCHEDULE_CLOCK')
        need(isinstance(identity, str) and bool(identity) and identity not in seen, 'MODEL_IDENTITY')
        need(isinstance(model, dict) and model['arm'] == 'PV' and
             type(model['label_cutoff']) is int and model['label_cutoff'] <= t, 'MODEL_CAUSALITY')
        seen.add(identity)
        previous = t
    need(schedule[0]['t'] == start, 'INITIAL_MODEL_AT_START')
    scheduled = {entry['t']: entry for entry in schedule[1:]}
    by_time = {row['timestamp']: row for row in rows}
    need(len(by_time) == len(rows), 'DUPLICATE_BAR')
    need(start - STEP in by_time and
         all(t in by_time for t in range(start, end, STEP)), 'FULL_BAR_DOMAIN')
    for t in range(start, end, DAY):
        need(t in fs, 'DAILY_INPUT_MISSING')
        f = fs[t]
        need(f['latest_price_closed_at'] == t and math.isfinite(float(f['price'])) and
             float(f['price']) > 0 and float(f['price']) == float(by_time[t - STEP]['price']),
             'CAUSAL_PRICE')
    active = schedule[0]
    governance = dict(
        scheduled_updates=[dict(t=e['t'], model_id=e['model_id']) for e in schedule[1:]],
        applied_updates=[dict(t=start, model_id=active['model_id'], reason='INITIAL')],
        blocked_updates=[], active_model_by_date=[], performance_freeze_time=None)
    books = [ExactCapacityBook(False, latency), ExactCapacityBook(True, latency)]
    decision_clocks, predictions = [None, None], []
    count, observation_halted = 0, False
    for t in range(start, end, STEP):
        if not observation_halted:
            count += 1
            for i, book in enumerate(books):
                if book.q:
                    if t >= end - TERMINAL_RESERVE_BARS * STEP:
                        book.request_exit(t, 'terminal')
                    elif t >= decision_clocks[i] + DAY - 3 * STEP:
                        book.request_exit(t, 'horizon')
        if t % DAY == 0:
            if t in scheduled:
                candidate = scheduled[t]
                reason = ('STATIC_REFERENCE' if not rolling else
                          'EXECUTION_INSUFFICIENT' if any(b.insufficient for b in books) else
                          'HARD_STOP' if any(b.stopped is not None for b in books) else
                          'PERFORMANCE_FREEZE' if governance['performance_freeze_time'] is not None else None)
                event = dict(t=t, model_id=candidate['model_id'])
                if reason is None:
                    active = candidate
                    governance['applied_updates'].append(dict(event, reason='SCHEDULED'))
                else:
                    governance['blocked_updates'].append(dict(event, reason=reason))
            prediction = dict(predict(active['model'], fs[t], t), model_id=active['model_id'])
            predictions.append(prediction)
            governance['active_model_by_date'].append(dict(t=t, model_id=active['model_id']))
            direction = prediction['direction']
            if not observation_halted:
                for i, book in enumerate(books):
                    if (direction and not book.q and book.pending is None and
                            book.stopped is None and book.insufficient is None):
                        size = (Decimal(2500) / Decimal(str(fs[t]['price'])) /
                                Decimal('.001')).to_integral_value(rounding=ROUND_FLOOR)
                        q = direction * float(size * Decimal('.001'))
                        if q:
                            decision_clocks[i] = t
                            book.pending = dict(kind='entry', due=t + latency * STEP, quantity=q)
                            book.event('entry_request', t, quantity=q, features=copy.deepcopy(fs[t]),
                                       direction=direction, decision_time=t,
                                       scheduled_exit_request=t + DAY - 3 * STEP)
        if observation_halted:
            continue
        row = by_time[t]
        for book in books:
            book.fund(row)
            book.execute(row)
        reasons = [book.observe(row) for book in books]
        if governance['performance_freeze_time'] is None and any(book.alert for book in books):
            governance['performance_freeze_time'] = t + STEP
        if any(reasons):
            reason = next(r for r in reasons if r)
            for book in books:
                book.stop(t + STEP, reason)
        if any(book.insufficient for book in books):
            observation_halted = True
    for book in books:
        if book.q and book.insufficient is None:
            book.insufficient = 'TERMINAL_RESIDUAL'
    path = dict(start=start, end=end, bars=count, latency=latency, initially_stopped=False,
                horizon=HORIZON, terminal_reserve_bars=TERMINAL_RESERVE_BARS,
                capital=10000, notional=2500, books=[book.summary() for book in books])
    return dict(rolling=rolling, path=path, predictions=predictions, governance=governance)
