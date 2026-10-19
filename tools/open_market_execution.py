"""Fixed daily conditional execution using the unchanged historical Book.

No model fitting, networking or account adapter. OHLC fills/volume capacity remain
reference assumptions, not executable quotes. The final day has an explicit
23:30 terminal request overriding the normal 23:45 daily request.
"""
import copy
import math
from decimal import Decimal, ROUND_FLOOR

from btc_old_output_model import Book, DAY, STEP, day, fill_price, need

HORIZON = 'daily_cycle_2345_request'
TERMINAL_RESERVE_BARS = 6


class ExactCapacityBook(Book):
    """Same legacy execution state machine, with exact decimal 1% volume flooring.

    The old Book/source remains immutable. This new-scope adapter changes only
    capacity arithmetic; it neither alters source volume nor rounds it upward.
    """
    def execute(self, row):
        t = row['timestamp']
        if not self.pending or self.pending['due'] > t:
            return
        p, self.pending = self.pending, None
        entry = p['kind'] == 'entry'
        requested = p['quantity'] if entry else -self.q
        volume = Decimal(str(row['volume']))
        need(volume.is_finite() and volume >= 0, 'CAPACITY_VOLUME')
        cap = float((volume * Decimal('.01') / Decimal('.001'))
                    .to_integral_value(rounding=ROUND_FLOOR) * Decimal('.001'))
        amount = min(abs(requested), cap)
        dq = math.copysign(amount, requested) if amount else 0.
        if dq:
            price = fill_price(row, dq, self.stress)
            fee = abs(dq) * price * (.0011 if self.stress else .00055)
            slip = dq * (price - row['open'])
            if entry:
                self.before_entry, self.entry_time = self.cash, t
                self.entry_collateral, self.leg_cash = abs(dq) * price, 0.
                self.entry_full = abs(dq - requested) < 1e-9
            self.cash -= dq * price + fee
            self.leg_cash -= dq * price + fee
            self.gross_cash -= dq * row['open']
            self.fees += fee
            self.slip += slip
            self.turnover += abs(dq) * price
            self.q = round(self.q + dq, 9)
            self.event('fill', t, phase=p['kind'], dq=dq, requested=requested,
                       price=price, raw_open=row['open'], fee=fee, slippage=slip,
                       quantity_after=self.q, cash_after=self.cash, capacity=cap)
        if entry:
            if amount + 1e-9 < abs(requested):
                self.failed_entries += 1
                self.event('failed_entry', t, requested=requested, filled=dq)
                self.request_exit(t, 'partial_entry_unwind')
        elif self.q:
            self.pending = dict(kind='exit', due=t + STEP)
        else:
            self.cycles.append(dict(start=self.entry_time, end=t,
                                    net=self.cash-self.before_entry, complete=self.entry_full))
            self.event('flat', t, net=self.cash-self.before_entry, complete=self.entry_full)
            self.entry_time = self.before_entry = None
            self.entry_collateral = self.leg_cash = 0.
            self.entry_full, self.exit_deadline = False, None
            self.last_exit_day = day(t)
        if self.exit_deadline is not None and t >= self.exit_deadline and self.q:
            self.insufficient, self.pending = 'EXIT_RESIDUAL', None
            self.event('insufficient', t, reason=self.insufficient)


def simulate(rows, fs, decisions, start, end, latency=1):
    need(type(start) is int and type(end) is int and
         start % DAY == 0 and end % DAY == 0 and start < end, 'PERIOD')
    need(type(latency) is int and latency in (1, 2), 'LATENCY')
    by_time = {r['timestamp']: r for r in rows}
    need(len(by_time) == len(rows), 'DUPLICATE_BAR')
    need(start - STEP in by_time and
         all(t in by_time for t in range(start, end, STEP)), 'FULL_BAR_DOMAIN')
    for t in range(start, end, DAY):
        need(t in fs and t in decisions, 'DAILY_INPUT_MISSING')
        need(type(decisions[t]) is int and decisions[t] in (-1, 0, 1), 'DIRECTION')
        f = fs[t]
        need(f['latest_price_closed_at'] == t and
             math.isfinite(float(f['price'])) and float(f['price']) > 0 and
             float(f['price']) == float(by_time[t - STEP]['price']), 'CAUSAL_PRICE')
    books = [ExactCapacityBook(False, latency), ExactCapacityBook(True, latency)]
    decision_clocks = [None, None]
    count = 0
    for t in range(start, end, STEP):
        row = by_time[t]
        count += 1
        for i, b in enumerate(books):
            if b.q:
                if t >= end - TERMINAL_RESERVE_BARS * STEP:
                    b.request_exit(t, 'terminal')
                elif t >= decision_clocks[i] + DAY - 3 * STEP:
                    b.request_exit(t, 'horizon')
        if t % DAY == 0:
            direction = decisions[t]
            for i, b in enumerate(books):
                if (direction and not b.q and b.pending is None and
                        b.stopped is None and b.insufficient is None):
                    size = (Decimal(2500) / Decimal(str(fs[t]['price'])) /
                            Decimal('.001')).to_integral_value(rounding=ROUND_FLOOR)
                    q = direction * float(size * Decimal('.001'))
                    if q:
                        decision_clocks[i] = t
                        b.pending = dict(kind='entry', due=t + latency * STEP, quantity=q)
                        b.event('entry_request', t, quantity=q, features=copy.deepcopy(fs[t]),
                                direction=direction, decision_time=t,
                                scheduled_exit_request=t + DAY - 3 * STEP)
        for b in books:
            b.fund(row)
            b.execute(row)
        reasons = [b.observe(row) for b in books]
        if any(reasons):
            reason = next(r for r in reasons if r)
            for b in books:
                b.stop(t + STEP, reason)
        # A risk stop retains the whole calendar; missing execution evidence does not.
        if any(b.insufficient for b in books):
            break
    for b in books:
        if b.q and b.insufficient is None:
            b.insufficient = 'TERMINAL_RESIDUAL'
    return dict(start=start, end=end, bars=count, latency=latency,
                initially_stopped=False, horizon=HORIZON,
                terminal_reserve_bars=TERMINAL_RESERVE_BARS,
                capital=10000, notional=2500, books=[b.summary() for b in books])
