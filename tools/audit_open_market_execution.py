"""Independent Decimal reconstruction of the full fixed daily execution calendar.

Does not import an execution/model module or trust reported fills, decisions,
bar counts, risk triggers, omitted events, or summary totals as its input state.
"""
import datetime as dt
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR

STEP = 300000
DAY = 86400000
ZERO = D(0)
TOL = D('0.0000001')


def need(ok, why):
    if not ok:
        raise ValueError('AUDIT_' + why)


def number(value):
    out = D(str(value))
    need(out.is_finite(), 'FINITE')
    return out


def same(expected, actual, path='RESULT'):
    if isinstance(expected, dict):
        need(isinstance(actual, dict) and set(actual) == set(expected), path + '_KEYS')
        for key, value in expected.items():
            same(value, actual[key], path + '.' + str(key))
    elif isinstance(expected, (list, tuple)):
        need(isinstance(actual, (list, tuple)) and len(actual) == len(expected), path + '_COUNT')
        for index, (a, b) in enumerate(zip(expected, actual)):
            same(a, b, path + '.' + str(index))
    elif isinstance(expected, bool) or expected is None or isinstance(expected, str):
        need(type(actual) is type(expected) and actual == expected, path + '_VALUE')
    elif isinstance(expected, int):
        need(type(actual) is int and actual == expected, path + '_INTEGER')
    else:
        # Only this descriptive sum can accumulate >100,000 float additions.
        # Cash, fees, risk, prices and all other fields retain the absolute 1e-7 bound.
        tolerance = max(TOL, abs(number(expected)) * D('1e-12')) if path.endswith('.exposure_sum') else TOL
        need(not isinstance(actual, bool) and abs(number(expected) - number(actual)) <= tolerance,
             path + '_NUMERIC')


def date(t):
    return dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).date()


def step_quantity(value):
    return (value / D('.001')).to_integral_value(rounding=ROUND_FLOOR) * D('.001')


class Account:
    """Audit-only state, driven by the source calendar rather than exported events."""
    def __init__(self, stress, latency):
        self.stress, self.latency = stress, latency
        self.cash = self.gross_cash = self.peak = self.close_peak = D(10000)
        self.q = self.fees = self.slip = self.funding = self.turnover = ZERO
        self.dd = self.close_dd = self.collateral = self.leg_cash = ZERO
        self.entry = self.before_entry = self.decision = None
        self.entry_full = False
        self.pending = self.deadline = self.stopped = self.insufficient = None
        self.events, self.cycles, self.days = [], [], []
        self.weeks, self.active_weeks = {}, set()
        self.holding = self.long = self.short = self.failed = 0
        self.exposure = ZERO
        self.last = self.week_equity = D(10000)
        self.alert, self.negative_weeks = False, 0

    def event(self, kind, t, **fields):
        self.events.append(dict(kind=kind, t=t, **fields))

    def exit(self, t, reason):
        if self.deadline is not None:
            return
        self.pending = None
        if not self.q:
            return
        self.deadline = t + 6 * STEP
        self.pending = dict(kind='exit', due=t + self.latency * STEP)
        self.event('exit_request', t, reason=reason, deadline=self.deadline)

    def stop(self, t, reason):
        if self.stopped is None:
            self.stopped = dict(t=t, reason=reason)
            self.event('stop', t, reason=reason)
        self.exit(t, 'hard_stop')

    def schedule(self, t, end, feature, direction):
        if self.q:
            if t >= end - 6 * STEP:
                self.exit(t, 'terminal')
            elif t >= self.decision + DAY - 3 * STEP:
                self.exit(t, 'horizon')
        if (t % DAY == 0 and direction and not self.q and self.pending is None and
                self.stopped is None and self.insufficient is None):
            q = direction * step_quantity(D(2500) / number(feature['price']))
            if q:
                self.decision = t
                self.pending = dict(kind='entry', due=t + self.latency * STEP, quantity=q)
                self.event('entry_request', t, quantity=q, features=feature,
                           direction=direction, decision_time=t,
                           scheduled_exit_request=t + DAY - 3 * STEP)

    def settle_and_fill(self, row):
        t = row['timestamp']
        rate = number(row['funding_rate_per_interval'])
        if self.q and rate:
            amount = self.q * number(row['mark_open']) * rate
            self.cash -= amount
            self.leg_cash -= amount
            self.funding += amount
            self.event('funding', t, q=self.q, mark=number(row['mark_open']),
                       rate=rate, amount=amount)
        if self.pending is None or self.pending['due'] > t:
            return
        p, self.pending = self.pending, None
        entry = p['kind'] == 'entry'
        requested = p['quantity'] if entry else -self.q
        cap = step_quantity(number(row['volume']) * D('.01'))
        amount = min(abs(requested), cap)
        dq = amount if requested > 0 else -amount
        if dq:
            raw = number(row[('high' if dq > 0 else 'low') if self.stress else 'open'])
            factor = D(1) + (D('.0004') if self.stress else D('.0002')) * (1 if dq > 0 else -1)
            price = (raw * factor / D('.1')).to_integral_value(
                rounding=ROUND_CEILING if dq > 0 else ROUND_FLOOR) * D('.1')
            fee = abs(dq) * price * (D('.0011') if self.stress else D('.00055'))
            slip = dq * (price - number(row['open']))
            if entry:
                need(self.q == 0, 'OVERLAPPING_ENTRY')
                self.before_entry, self.entry = self.cash, t
                self.collateral, self.leg_cash = abs(dq) * price, ZERO
                self.entry_full = dq == requested
            self.cash -= dq * price + fee
            self.leg_cash -= dq * price + fee
            self.gross_cash -= dq * number(row['open'])
            self.fees += fee
            self.slip += slip
            self.turnover += abs(dq) * price
            self.q += dq
            self.event('fill', t, phase=p['kind'], dq=dq, requested=requested,
                       price=price, raw_open=number(row['open']), fee=fee, slippage=slip,
                       quantity_after=self.q, cash_after=self.cash, capacity=cap)
        if entry:
            if amount < abs(requested):
                self.failed += 1
                self.event('failed_entry', t, requested=requested, filled=dq)
                self.exit(t, 'partial_entry_unwind')
        elif self.q:
            self.pending = dict(kind='exit', due=t + STEP)
        else:
            net = self.cash - self.before_entry
            self.cycles.append(dict(start=self.entry, end=t, net=net, complete=self.entry_full))
            self.event('flat', t, net=net, complete=self.entry_full)
            self.entry = self.before_entry = None
            self.collateral = self.leg_cash = ZERO
            self.entry_full, self.deadline = False, None
        if self.deadline is not None and t >= self.deadline and self.q:
            self.insufficient, self.pending = 'EXIT_RESIDUAL', None
            self.event('insufficient', t, reason=self.insufficient)

    def observe(self, row):
        t = row['timestamp'] + STEP
        adverse = number(row['mark_low'] if self.q >= 0 else row['mark_high'])
        mark = number(row['mark_close'])
        low_eq = self.cash + self.q * adverse - abs(self.q * adverse) * D('.0015')
        equity = self.cash + self.q * mark - abs(self.q * mark) * D('.0015')
        self.dd = max(self.dd, (self.peak - low_eq) / self.peak)
        self.peak = max(self.peak, equity)
        self.close_peak = max(self.close_peak, equity)
        self.close_dd = max(self.close_dd, (self.close_peak - equity) / self.close_peak)
        reason = None
        if self.dd >= D('.08'):
            reason = 'RESEARCH_DRAWDOWN_8_PERCENT'
        elif abs(self.q) * number(row['mark_high']) > D(5000) + D('0.00000001'):
            reason = 'GROSS_CAP'
        elif self.q:
            cushion = self.collateral + self.leg_cash + self.q * adverse - abs(self.q * adverse) * D('.0015')
            if cushion <= 0:
                reason = 'REFERENCE_COLLATERAL_EXHAUSTED'
            elif cushion < D('.08') * abs(self.q * adverse):
                reason = 'REFERENCE_MARGIN_BUFFER'
        iso = date(row['timestamp']).isocalendar()
        week = '%04d-W%02d' % (iso.year, iso.week)
        if self.q:
            self.active_weeks.add(week)
            self.holding += 1
            self.long += int(self.q > 0)
            self.short += int(self.q < 0)
            self.exposure += abs(self.q) * mark
        self.weeks[week] = self.weeks.get(week, ZERO) + equity - self.last
        self.last = equity
        if t % DAY == 0:
            self.days.append(dict(t=t, equity=equity))
            if date(t).weekday() == 0:
                self.negative_weeks = self.negative_weeks + 1 if equity < self.week_equity else 0
                if self.negative_weeks >= 2 and not self.alert:
                    self.alert = True
                    self.event('performance_alert', t, action='FROZEN_INPUT_LEDGER_STATE_REVIEW')
                self.week_equity = equity
        return reason

    def summary(self):
        return dict(stress=self.stress, cash=self.cash, quantity=self.q,
                    net=self.last - 10000, gross=self.gross_cash - 10000,
                    fees=self.fees, slippage=self.slip, funding=self.funding,
                    turnover=self.turnover, drawdown=self.dd, close_drawdown=self.close_dd,
                    stopped=self.stopped, insufficient=self.insufficient, cycles=self.cycles,
                    events=self.events, days=self.days, weeks=self.weeks,
                    active_weeks=sorted(self.active_weeks), holding_bars=self.holding,
                    long_bars=self.long, short_bars=self.short, exposure_sum=self.exposure,
                    failed_entries=self.failed)


def audit(result, rows, fs, decisions):
    start, end, latency = result['start'], result['end'], result['latency']
    need(type(start) is int and type(end) is int and start % DAY == end % DAY == 0 and
         start < end, 'PERIOD')
    need(type(latency) is int and latency in (1, 2), 'LATENCY')
    need(result['initially_stopped'] is False and
         result['horizon'] == 'daily_cycle_2345_request' and
         result['terminal_reserve_bars'] == 6 and
         result['capital'] == 10000 and result['notional'] == 2500, 'CONTRACT')
    by_time = {r['timestamp']: r for r in rows}
    need(len(by_time) == len(rows), 'DUPLICATE_BAR')
    need(start - STEP in by_time and
         all(t in by_time for t in range(start, end, STEP)), 'FULL_BAR_DOMAIN')
    for t in range(start, end, DAY):
        need(t in fs and t in decisions, 'DAILY_INPUT_MISSING')
        need(type(decisions[t]) is int and decisions[t] in (-1, 0, 1), 'DIRECTION')
        need(fs[t]['latest_price_closed_at'] == t and number(fs[t]['price']) > 0 and
             number(fs[t]['price']) == number(by_time[t - STEP]['price']), 'CAUSAL_PRICE')
    accounts = [Account(False, latency), Account(True, latency)]
    count = 0
    for t in range(start, end, STEP):
        row = by_time[t]
        count += 1
        for a in accounts:
            a.schedule(t, end, fs[t] if t % DAY == 0 else None,
                       decisions[t] if t % DAY == 0 else 0)
        for a in accounts:
            a.settle_and_fill(row)
        reasons = [a.observe(row) for a in accounts]
        if any(reasons):
            reason = next(r for r in reasons if r)
            for a in accounts:
                a.stop(t + STEP, reason)
        if any(a.insufficient for a in accounts):
            break
    for a in accounts:
        if a.q and a.insufficient is None:
            a.insufficient = 'TERMINAL_RESIDUAL'
    need(type(result['bars']) is int and result['bars'] == count, 'COMPLETE_CALENDAR')
    need(isinstance(result['books'], list) and len(result['books']) == 2, 'PAIRED_BOOKS')
    for i, a in enumerate(accounts):
        same(a.summary(), result['books'][i], 'BOOK_' + str(i))
        if not a.q:
            need(abs(a.cash - a.gross_cash + a.fees + a.slip + a.funding) <= TOL,
                 'COST_IDENTITY')
    return dict(status='DECIMAL_CALENDAR_LEDGER_AND_RISK_VERIFIED', bars=count,
                books=[dict(net=str(a.last - 10000), cash=str(a.cash),
                            fills=sum(e['kind'] == 'fill' for e in a.events),
                            funding=str(a.funding), drawdown=str(a.dd)) for a in accounts])
