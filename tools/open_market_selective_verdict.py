"""Frozen selective-calibration endpoints; no fitting, replay or I/O.

The classifier uses exact rationals on serialized ledger values.  Its independent
Decimal auditor does not call the classifier or its validation/aggregation code.
The separate execution audit remains responsible for rebuilding the cash ledger.
"""
import datetime as dt
import math
from decimal import Decimal, localcontext
from fractions import Fraction

DAY = 86400000
STEP = 300000
KEYS = ('2021-control', '2021-veto', '2025-control', '2025-veto', '2025-delay')
PERIODS = {'2021': (1633132800000, 1640822400000),
           '2025': (1736985600000, 1767225600000)}
MIDPOINT = 1751328000000


def _need(ok, reason):
    if not ok:
        raise ValueError('SELECTIVE_VERDICT_' + reason)


def _number(value):
    _need(type(value) in (int, float), 'NUMBER_TYPE')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    _need(finite, 'NONFINITE')
    return Fraction(str(value))


def _week(t):
    iso = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).isocalendar()
    return '%04d-W%02d' % (iso.year, iso.week)


def _export_number(value):
    try:
        out = float(value)
    except OverflowError as error:
        raise ValueError('SELECTIVE_VERDICT_OUTPUT_RANGE') from error
    _need(math.isfinite(out) and (out != 0 or value == 0), 'OUTPUT_RANGE')
    return out


def _validate(paths):
    _need(isinstance(paths, dict) and set(paths) == set(KEYS), 'PATH_SET')
    incomplete = []
    for key in KEYS:
        p = paths[key]
        start, end = PERIODS[key[:4]]
        _need(isinstance(p, dict) and
              {'start', 'end', 'latency', 'bars', 'books', 'capital', 'notional',
               'horizon', 'terminal_reserve_bars', 'initially_stopped'} <= set(p), 'PATH_SCHEMA')
        _need(type(p['start']) is int and type(p['end']) is int and
              (p['start'], p['end']) == (start, end), 'PERIOD_IDENTITY')
        _need(type(p['latency']) is int and p['latency'] == (2 if key == '2025-delay' else 1),
              'LATENCY_IDENTITY')
        _need(_number(p['capital']) == 10000 and _number(p['notional']) == 2500 and
              p['horizon'] == 'daily_cycle_2345_request' and
              type(p['terminal_reserve_bars']) is int and p['terminal_reserve_bars'] == 6 and
              p['initially_stopped'] is False, 'EXECUTION_IDENTITY')
        expected_bars = (end-start)//STEP
        _need(type(p['bars']) is int and 0 <= p['bars'] <= expected_bars, 'BAR_COUNT')
        _need(isinstance(p['books'], list) and len(p['books']) == 2, 'PAIRED_BOOKS')
        dates = list(range(start+DAY, end+DAY, DAY))
        weeks = {_week(t) for t in range(start, end, DAY)}
        full = p['bars'] == expected_bars
        for i, book in enumerate(p['books']):
            required = {'stress', 'cash', 'quantity', 'net', 'stopped', 'insufficient',
                        'cycles', 'days', 'active_weeks'}
            _need(isinstance(book, dict) and required <= set(book), 'BOOK_SCHEMA')
            _need(type(book['stress']) is bool and book['stress'] == bool(i), 'BOOK_ORDER')
            net, cash, q = (_number(book[k]) for k in ('net', 'cash', 'quantity'))
            _need(book['insufficient'] is None or
                  (isinstance(book['insufficient'], str) and bool(book['insufficient'])),
                  'INSUFFICIENT_SCHEMA')
            stop = book['stopped']
            if stop is not None:
                _need(isinstance(stop, dict) and {'t', 'reason'} <= set(stop) and
                      type(stop['t']) is int and start <= stop['t'] <= end and
                      stop['t'] % STEP == 0 and isinstance(stop['reason'], str) and
                      bool(stop['reason']), 'STOP_SCHEMA')
            _need(isinstance(book['days'], list), 'DAY_SCHEMA')
            clocks = []
            for row in book['days']:
                _need(isinstance(row, dict) and {'t', 'equity'} <= set(row) and
                      type(row['t']) is int and start < row['t'] <= end and
                      row['t'] % DAY == 0, 'DAY_CLOCK')
                _number(row['equity'])
                clocks.append(row['t'])
            _need(clocks == sorted(set(clocks)), 'DAY_ORDER')
            _need(isinstance(book['active_weeks'], list) and
                  all(isinstance(w, str) for w in book['active_weeks']) and
                  book['active_weeks'] == sorted(set(book['active_weeks'])) and
                  set(book['active_weeks']) <= weeks, 'ACTIVE_WEEKS')
            _need(isinstance(book['cycles'], list), 'CYCLES_SCHEMA')
            previous_end = start
            for cycle in book['cycles']:
                _need(isinstance(cycle, dict) and {'start', 'end', 'net', 'complete'} <= set(cycle)
                      and type(cycle['complete']) is bool and type(cycle['start']) is int and
                      type(cycle['end']) is int and previous_end <= cycle['start'] <= cycle['end'] < end
                      and cycle['start'] % STEP == 0 and cycle['end'] % STEP == 0, 'CYCLE_SCHEMA')
                _number(cycle['net'])
                previous_end = cycle['end']
            book_full = book['insufficient'] is None and q == 0 and clocks == dates
            if book_full:
                tolerance = Fraction('1e-8') + abs(net)*Fraction('1e-12')
                last = _number(book['days'][-1]['equity'])
                _need(abs(last-10000-net) <= tolerance and abs(cash-last) <= tolerance,
                      'FINAL_CASH_IDENTITY')
            full = full and book_full
        if not full:
            incomplete.append(key)
    return incomplete


def classify(paths):
    """Classify exactly five fixed, already independently audited cash paths."""
    incomplete = _validate(paths)
    locks = dict(independent_confirmation=False, online_learning_proven=False,
                 g4=False, qualified_candidates=0, demo_opening_lock_unchanged=True)
    if incomplete:
        return dict(status='COMPARISON_EVIDENCE_INCOMPLETE',
                    reasons=['INCOMPLETE_' + key for key in incomplete],
                    pressure_increments=None, half_year_pressure_net=None, capacity=None,
                    pressure_without_best_cycle=None, conditions=None, **locks)
    reasons = []
    absolute = True
    for period in ('2021', '2025'):
        books = paths[period+'-veto']['books']
        if any(b['stopped'] is not None for b in books):
            reasons.append(period+'_VETO_HARD_STOP')
            absolute = False
        if any(_number(b['net']) <= 0 for b in books):
            reasons.append(period+'_VETO_NET_NONPOSITIVE')
            absolute = False
    stress = paths['2025-veto']['books'][1]
    net = _number(stress['net'])
    if net < 100:
        reasons.append('MINIMUM_ABSOLUTE_EFFECT')
        absolute = False
    delay_safe = all(b['stopped'] is None for b in paths['2025-delay']['books'])
    if not delay_safe:
        reasons.append('DELAY_HARD_STOP')
        absolute = False
    increments = {p: _number(paths[p+'-veto']['books'][1]['net']) -
                  _number(paths[p+'-control']['books'][1]['net']) for p in ('2021', '2025')}
    if increments['2021'] <= 0:
        reasons.append('2021_INCREMENT_NONPOSITIVE')
    if increments['2025'] < 25:
        reasons.append('2025_MINIMUM_INCREMENT')
    incremental = increments['2021'] > 0 and increments['2025'] >= 25
    count = sum(c['complete'] for c in stress['cycles'])
    active = len(stress['active_weeks'])
    capacity = count >= 30 and active >= 12
    if not capacity:
        reasons.append('EVENT_CAPACITY')
    # Partial-entry unwind cash is included in net: it must also enter concentration.
    without_best = net-max([_number(c['net']) for c in stress['cycles']] + [Fraction(0)])
    if without_best <= 0:
        reasons.append('BEST_CYCLE_CONCENTRATION')
    previous, halves = Fraction(10000), [Fraction(0), Fraction(0)]
    for row in stress['days']:
        equity = _number(row['equity'])
        halves[int(row['t'] > MIDPOINT)] += equity-previous
        previous = equity
    if min(halves) <= 0:
        reasons.append('HALF_YEAR_SIGN')
    delay_positive = _number(paths['2025-delay']['books'][1]['net']) > 0
    if not delay_positive:
        reasons.append('DELAY_NET_NONPOSITIVE')
    stable = capacity and without_best > 0 and min(halves) > 0 and delay_positive
    status = ('NET_OR_RISK_UNSUPPORTED' if not absolute else
              'INCREMENT_UNSUPPORTED' if not incremental else
              'FRAGILE_DEVELOPMENT_SIGNAL' if not stable else 'DEVELOPMENT_SUPPORTED')
    return dict(status=status, reasons=reasons,
                pressure_increments={k: _export_number(v) for k, v in increments.items()},
                half_year_pressure_net=[_export_number(v) for v in halves],
                capacity=dict(complete_cycles=count, active_weeks=active, supported=capacity),
                pressure_without_best_cycle=_export_number(without_best),
                conditions=dict(absolute_and_risk=absolute, incremental=incremental,
                                capacity=capacity, half_year_positive=min(halves) > 0,
                                without_best_cycle_positive=without_best > 0,
                                delay_safe=delay_safe, delay_pressure_positive=delay_positive),
                **locks)


def audit_verdict(paths, result):
    """Independently rebuild endpoint checks with Decimal, not classifier helpers."""
    def check(ok, why):
        if not ok:
            raise ValueError('AUDIT_SELECTIVE_VERDICT_' + why)

    def number(value):
        check(type(value) in (int, float), 'NUMBER_TYPE')
        out = Decimal(str(value))
        check(out.is_finite(), 'NONFINITE')
        return out

    names = ('2021-control', '2021-veto', '2025-control', '2025-veto', '2025-delay')
    check(isinstance(paths, dict) and set(paths) == set(names), 'PATH_SET')
    with localcontext() as context:
        context.prec = 1600
        incomplete = []
        for name in names:
            path = paths[name]
            left, right = ((1633132800000, 1640822400000) if name.startswith('2021')
                           else (1736985600000, 1767225600000))
            required = {'start', 'end', 'latency', 'bars', 'books', 'capital', 'notional',
                        'horizon', 'terminal_reserve_bars', 'initially_stopped'}
            check(isinstance(path, dict) and required <= set(path), 'PATH_SCHEMA')
            check(type(path['start']) is int and type(path['end']) is int and
                  path['start'] == left and path['end'] == right, 'PERIOD')
            check(type(path['latency']) is int and
                  path['latency'] == (2 if name == '2025-delay' else 1), 'LATENCY')
            check(number(path['capital']) == 10000 and number(path['notional']) == 2500 and
                  path['initially_stopped'] is False and
                  path['horizon'] == 'daily_cycle_2345_request' and
                  type(path['terminal_reserve_bars']) is int and path['terminal_reserve_bars'] == 6,
                  'EXECUTION_IDENTITY')
            total_bars = (right-left)//300000
            check(type(path['bars']) is int and 0 <= path['bars'] <= total_bars, 'BARS')
            check(isinstance(path['books'], list) and len(path['books']) == 2, 'BOOKS')
            calendar = [left+i*86400000 for i in range(1, (right-left)//86400000+1)]
            allowed_weeks = set()
            for t in range(left, right, 86400000):
                iso = dt.datetime.fromtimestamp(t/1000, dt.timezone.utc).isocalendar()
                allowed_weeks.add('%04d-W%02d' % (iso.year, iso.week))
            full = path['bars'] == total_bars
            for side, book in enumerate(path['books']):
                fields = {'stress', 'cash', 'quantity', 'net', 'stopped', 'insufficient',
                          'cycles', 'days', 'active_weeks'}
                check(isinstance(book, dict) and fields <= set(book), 'BOOK_SCHEMA')
                check(type(book['stress']) is bool and book['stress'] == (side == 1), 'BOOK_ORDER')
                cash, net, q = number(book['cash']), number(book['net']), number(book['quantity'])
                check(book['insufficient'] is None or
                      (isinstance(book['insufficient'], str) and len(book['insufficient']) > 0),
                      'INSUFFICIENT_SCHEMA')
                stop = book['stopped']
                if stop is not None:
                    check(isinstance(stop, dict) and {'t', 'reason'} <= set(stop) and
                          type(stop['t']) is int and left <= stop['t'] <= right and
                          stop['t'] % 300000 == 0 and isinstance(stop['reason'], str) and
                          len(stop['reason']) > 0, 'STOP_SCHEMA')
                check(isinstance(book['days'], list), 'DAY_SCHEMA')
                timestamps = []
                for row in book['days']:
                    check(isinstance(row, dict) and {'t', 'equity'} <= set(row) and
                          type(row['t']) is int and left < row['t'] <= right and
                          row['t'] % 86400000 == 0, 'DAY_CLOCK')
                    number(row['equity'])
                    timestamps.append(row['t'])
                check(timestamps == sorted(set(timestamps)), 'DAY_ORDER')
                check(isinstance(book['active_weeks'], list) and
                      all(isinstance(w, str) for w in book['active_weeks']) and
                      book['active_weeks'] == sorted(set(book['active_weeks'])) and
                      set(book['active_weeks']) <= allowed_weeks, 'ACTIVE_WEEKS')
                check(isinstance(book['cycles'], list), 'CYCLE_SCHEMA')
                last_exit = left
                for cycle in book['cycles']:
                    check(isinstance(cycle, dict) and
                          {'start', 'end', 'net', 'complete'} <= set(cycle) and
                          type(cycle['complete']) is bool and type(cycle['start']) is int and
                          type(cycle['end']) is int and last_exit <= cycle['start'] <= cycle['end'] < right
                          and cycle['start'] % 300000 == 0 and cycle['end'] % 300000 == 0, 'CYCLE_SCHEMA')
                    number(cycle['net'])
                    last_exit = cycle['end']
                finished = q == 0 and book['insufficient'] is None and timestamps == calendar
                if finished:
                    last_value = number(book['days'][-1]['equity'])
                    tolerance = Decimal('1e-8')+abs(net)*Decimal('1e-12')
                    check(abs(last_value-net-10000) <= tolerance and
                          abs(cash-last_value) <= tolerance, 'FINAL_CASH_IDENTITY')
                full = full and finished
            if not full:
                incomplete.append(name)

        locks = dict(independent_confirmation=False, online_learning_proven=False,
                     g4=False, qualified_candidates=0, demo_opening_lock_unchanged=True)
        if incomplete:
            expected = dict(status='COMPARISON_EVIDENCE_INCOMPLETE',
                            reasons=['INCOMPLETE_'+name for name in incomplete],
                            pressure_increments=None, half_year_pressure_net=None, capacity=None,
                            pressure_without_best_cycle=None, conditions=None, **locks)
        else:
            reasons, absolute = [], True
            for year in ('2021', '2025'):
                candidate = paths[year+'-veto']['books']
                if any(book['stopped'] is not None for book in candidate):
                    reasons.append(year+'_VETO_HARD_STOP')
                    absolute = False
                if min(number(book['net']) for book in candidate) <= 0:
                    reasons.append(year+'_VETO_NET_NONPOSITIVE')
                    absolute = False
            target = paths['2025-veto']['books'][1]
            target_net = number(target['net'])
            if target_net < Decimal(100):
                reasons.append('MINIMUM_ABSOLUTE_EFFECT')
                absolute = False
            delay_safe = not any(book['stopped'] is not None for book in paths['2025-delay']['books'])
            if not delay_safe:
                reasons.append('DELAY_HARD_STOP')
                absolute = False
            gains = {}
            for year in ('2021', '2025'):
                gains[year] = number(paths[year+'-veto']['books'][1]['net'])
                gains[year] -= number(paths[year+'-control']['books'][1]['net'])
            if gains['2021'] <= 0:
                reasons.append('2021_INCREMENT_NONPOSITIVE')
            if gains['2025'] < 25:
                reasons.append('2025_MINIMUM_INCREMENT')
            incremental = gains['2021'] > 0 and gains['2025'] >= 25
            cycles = len([cycle for cycle in target['cycles'] if cycle['complete']])
            active = len(target['active_weeks'])
            capacity = cycles >= 30 and active >= 12
            if not capacity:
                reasons.append('EVENT_CAPACITY')
            largest = Decimal(0)
            for cycle in target['cycles']:
                largest = max(largest, number(cycle['net']))
            residual = target_net-largest
            if residual <= 0:
                reasons.append('BEST_CYCLE_CONCENTRATION')
            # Use the boundary equity directly, rather than classifier's daily deltas.
            mid_equity = next(number(row['equity']) for row in target['days']
                              if row['t'] == 1751328000000)
            final_equity = number(target['days'][-1]['equity'])
            halves = [mid_equity-Decimal(10000), final_equity-mid_equity]
            if halves[0] <= 0 or halves[1] <= 0:
                reasons.append('HALF_YEAR_SIGN')
            delay_positive = number(paths['2025-delay']['books'][1]['net']) > 0
            if not delay_positive:
                reasons.append('DELAY_NET_NONPOSITIVE')
            if not absolute:
                status = 'NET_OR_RISK_UNSUPPORTED'
            elif not incremental:
                status = 'INCREMENT_UNSUPPORTED'
            elif not (capacity and residual > 0 and min(halves) > 0 and delay_positive):
                status = 'FRAGILE_DEVELOPMENT_SIGNAL'
            else:
                status = 'DEVELOPMENT_SUPPORTED'
            expected = dict(status=status, reasons=reasons, pressure_increments=gains,
                            half_year_pressure_net=halves,
                            capacity=dict(complete_cycles=cycles, active_weeks=active, supported=capacity),
                            pressure_without_best_cycle=residual,
                            conditions=dict(absolute_and_risk=absolute, incremental=incremental,
                                            capacity=capacity, half_year_positive=min(halves) > 0,
                                            without_best_cycle_positive=residual > 0,
                                            delay_safe=delay_safe, delay_pressure_positive=delay_positive),
                            **locks)

        numeric_cells = 0

        def compare(want, actual, name):
            nonlocal numeric_cells
            if isinstance(want, dict):
                check(isinstance(actual, dict) and set(actual) == set(want), name+'_KEYS')
                for key in want:
                    compare(want[key], actual[key], name+'.'+key)
            elif isinstance(want, list):
                check(isinstance(actual, list) and len(actual) == len(want), name+'_LENGTH')
                for i, value in enumerate(want):
                    compare(value, actual[i], name+'.'+str(i))
            elif isinstance(want, Decimal):
                delta = abs(number(actual)-want)
                check(delta <= Decimal('1e-18')+abs(want)*Decimal('2e-12'), name+'_VALUE')
                numeric_cells += 1
            else:
                check(type(actual) is type(want) and actual == want, name+'_VALUE')

        compare(expected, result, 'RESULT')
    return dict(status='INDEPENDENT_SELECTIVE_VERDICT_AUDIT_VERIFIED', paths=5,
                decision_status=expected['status'], numeric_cells=numeric_cells)
