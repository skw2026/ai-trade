"""Fixed three-asset weekly reference policy; no trading or online adaptation."""
from collections import deque
import copy
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from functools import cmp_to_key
import math
import time

from eth_sol_complete_model import Alert

D = lambda x: Decimal(str(x))
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
STEPS = tuple(map(D, ('.001', '.01', '.1')))
TICKS = tuple(map(D, ('.1', '.01', '.01')))
MINUTE, DAY, WEEK = 60000, 86400000, 604800000
INITIAL, FIRST, HALF, LAST, END = (1704067200000, 1704672000000,
                                  1719792000000, 1735516800000, 1735689600000)


def need(ok, why):
    if not ok:
        raise ValueError(why)


def choose(daily, t, horizon):
    need(horizon in (7, 14, 28), 'FIXED_HORIZONS')
    closes = [D(daily[s][str(t)]) for s in SYMBOLS]
    prior = [D(daily[s][str(t-horizon*DAY)]) for s in SYMBOLS]
    need(all(x.is_finite() and x > 0 for x in closes+prior), 'CLOSED_DAILY_PRICES')
    def compare(i, j):
        left, right = closes[i]*prior[j], closes[j]*prior[i]
        return (left > right)-(left < right)
    order = sorted(range(3), key=cmp_to_key(compare))
    q = [D(0)]*3
    tied = compare(order[0], order[1]) == 0 or compare(order[-1], order[-2]) == 0
    if not tied:
        for i, sign in ((order[0], -1), (order[-1], 1)):
            units = (D(1250)/closes[i]/STEPS[i]).to_integral_value(rounding=ROUND_FLOOR)
            q[i] = (sign*units*STEPS[i]).quantize(STEPS[i])
        if any(q[i] == 0 or abs(q[i])*closes[i] < 5 for i in (order[0], order[-1])):
            q = [D(0)]*3
    return dict(t=t, horizon=horizon, closes=list(map(str, closes)),
                prior=list(map(str, prior)), quantities=list(map(str, q)), tied=tied)


def price(bar, quantity, leg, stress):
    raw = D(bar[1 if quantity > 0 else 2] if stress else bar[0])
    slip = D('.0004' if stress else '.0002')
    tick = TICKS[leg]
    return (raw*(1+slip if quantity > 0 else 1-slip)/tick).to_integral_value(
        rounding=ROUND_CEILING if quantity > 0 else ROUND_FLOOR)*tick


class Ledger:
    def __init__(self, stress=False, delay=0):
        need(delay in (0, 1), 'FIXED_LATENCY')
        self.stress, self.delay = stress, delay
        self.cash = D(10000)
        self.q = [D(0)]*3
        self.leg_cash = [D(0)]*3
        self.collateral = [D(0)]*3
        self.fees = self.funding = self.slippage = self.gross = self.turnover = D(0)
        self.queue = deque()
        self.exiting = False
        self.deadline = None
        self.stopped = self.insufficient = None
        self.peak = 10000.
        self.dd = 0.
        self.events = []
        self.cycles = []
        self.cycle = None
        self.complete_entry = False
        self.failed_entries = 0
        self.alert = Alert()
        self.last_sample = 10000.
        self.last_value = 10000.
        self.active_weeks = set()
        self.position_minutes = 0
        self.gross_integral = self.net_integral = self.btc_integral = 0.
        self.max_gross = self.max_abs_net = 0.

    @property
    def flat(self):
        return not any(self.q)

    def event(self, kind, m, **fields):
        self.events.append(dict(kind=kind, minute=m, **fields))

    def request_entry(self, m, intent):
        need(self.flat and not self.queue and self.cycle is None and not self.exiting, 'OVERLAP')
        need(not self.stopped and not self.insufficient, 'ENTRY_AFTER_STOP')
        q = list(map(D, intent['quantities']))
        active = [i for i, v in enumerate(q) if v]
        if not active:
            return
        need(len(active) == 2 and sum(v > 0 for v in q) == 1, 'TWO_LEGS')
        self.cycle = dict(start=m, cash=str(self.cash), fees=str(self.fees),
                          funding=str(self.funding), slippage=str(self.slippage),
                          quantities=list(map(str, q)))
        self.complete_entry = False
        self.queue.extend((m+1+self.delay+j, i, q[i], 'entry') for j, i in enumerate(active))
        self.event('entry_requested', m, intent=intent)

    def request_exit(self, m, reason):
        if self.exiting:
            return
        self.queue.clear()
        self.exiting = True
        self.deadline = m+5
        self.event('exit_requested', m, reason=reason, deadline=self.deadline)
        if self.flat:
            self.finish(m)
        else:
            self.queue.append((m+1+self.delay, next(i for i, q in enumerate(self.q) if q), None, 'exit'))

    def finish(self, m):
        need(self.flat, 'FALSE_FLAT')
        if self.cycle is not None:
            c = self.cycle
            self.cycles.append(dict(start=c['start'], end=m, completed=self.complete_entry,
                quantities=c['quantities'], net=str(self.cash-D(c['cash'])),
                fees=str(self.fees-D(c['fees'])), funding=str(self.funding-D(c['funding'])),
                slippage=str(self.slippage-D(c['slippage']))))
        self.event('flat', m, cash=str(self.cash), completed=self.complete_entry)
        self.cycle = None
        self.complete_entry = False
        self.queue.clear()
        self.exiting = False
        self.deadline = None
        self.leg_cash = [D(0)]*3
        self.collateral = [D(0)]*3

    def stop(self, m, reason):
        if self.stopped is None:
            self.stopped = dict(minute=m, reason=reason)
            self.event('stop', m, reason=reason)
        if not self.flat or self.queue:
            self.request_exit(m, reason)

    def fund(self, m, rates, marks):
        for i, rate in sorted(rates.items()):
            need(i in range(3) and D(rate).is_finite(), 'FUNDING_INPUT')
            if not self.q[i]:
                continue
            amount = self.q[i]*D(marks[i])*D(rate)
            self.cash -= amount
            self.leg_cash[i] -= amount
            self.funding += amount
            self.event('funding', m, leg=i, rate=str(rate), mark=str(marks[i]),
                       amount=str(amount), cash=str(self.cash))

    def process(self, m, bars):
        if self.insufficient and self.insufficient != 'PERFORMANCE_REVIEW_FAILED':
            return
        if self.exiting and m > self.deadline:
            self.insufficient = 'RESIDUAL_EXPOSURE_UNCLOSED'
            self.event('insufficient', m, reason=self.insufficient)
            return
        if not self.queue or self.queue[0][0] > m:
            return
        due, i, requested, phase = self.queue.popleft()
        need(due == m, 'MISSED_ORDER')
        if phase == 'exit':
            requested = -self.q[i]
        need(requested, 'EMPTY_ORDER')
        bar = bars[i]
        capacity = (D(bar[4])*D('.01')/STEPS[i]).to_integral_value(rounding=ROUND_FLOOR)*STEPS[i]
        qty = min(abs(requested), capacity)
        delta = qty if requested > 0 else -qty
        px = price(bar, requested, i, self.stress)
        if qty*px < 5:
            qty = delta = D(0)
        if qty:
            paid = qty*px*D('.0011' if self.stress else '.00055')
            if phase == 'entry':
                need(self.q[i] == 0, 'TOP_UP_ENTRY')
                self.collateral[i] = qty*px
            else:
                need(self.q[i]*delta < 0 and qty <= abs(self.q[i]), 'EXIT_INCREASE')
            self.q[i] += delta
            self.cash -= delta*px+paid
            self.leg_cash[i] -= delta*px+paid
            self.fees += paid
            self.slippage += delta*(px-D(bar[0]))
            self.gross -= delta*D(bar[0])
            self.turnover += qty*px
            self.event('fill', m, leg=i, phase=phase, requested=str(requested),
                       delta=str(delta), price=str(px), fee=str(paid), cash=str(self.cash))
        else:
            self.event('no_fill', m, leg=i, phase=phase, requested=str(requested))
        if phase == 'entry':
            if qty != abs(requested):
                self.failed_entries += 1
                self.request_exit(m, 'PARTIAL_OR_FAILED_ENTRY')
            elif not self.queue:
                self.complete_entry = True
        elif self.flat:
            self.finish(m)
        else:
            nxt = i if self.q[i] else next(j for j, q in enumerate(self.q) if q)
            self.queue.append((m+1, nxt, None, 'exit'))

    def liquidation(self, prices):
        fee = .0011 if self.stress else .00055
        slip = .0004 if self.stress else .0002
        return float(self.cash)+sum(float(q)*p-abs(float(q))*(p*(fee+slip)+float(tick))
                                   for q, p, tick in zip(self.q, prices, TICKS))

    def risk(self, marks):
        good = [b[1] if q >= 0 else b[2] for q, b in zip(self.q, marks)]
        bad = [b[2] if q >= 0 else b[1] for q, b in zip(self.q, marks)]
        self.peak = max(self.peak, self.liquidation(good))
        self.dd = max(self.dd, (self.peak-self.liquidation(bad))/self.peak)
        gross = sum(abs(float(q))*b[1] for q, b in zip(self.q, marks))
        reasons = []
        if self.dd >= .08:
            reasons.append('RESEARCH_DRAWDOWN_8_PERCENT')
        if gross > 5000:
            reasons.append('GROSS_EXPOSURE_CAP')
        for i, q in enumerate(self.q):
            if q:
                cushion = float(self.collateral[i]+self.leg_cash[i])+float(q)*bad[i]
                if cushion <= 0:
                    reasons.append('REFERENCE_COLLATERAL_EXHAUSTED')
                if cushion/(abs(float(q))*bad[i]) < .08:
                    reasons.append('REFERENCE_CUSHION_UNDER_8_PERCENT')
        return reasons

    def review(self):
        return (self.insufficient is None and
                abs(self.cash-(D(10000)+self.gross-self.slippage-self.fees-self.funding)) <= D('1e-12') and
                all(q % step == 0 for q, step in zip(self.q, STEPS)) and
                all(math.isfinite(x) for x in (float(self.cash), self.peak, self.dd)))

    def close(self, m, t, marks):
        self.last_value = self.liquidation(marks)
        self.peak = max(self.peak, self.last_value)
        self.dd = max(self.dd, (self.peak-self.last_value)/self.peak)
        if m % 5 == 0:
            if not self.stopped and not self.insufficient:
                if not self.alert.observe(self.last_value-self.last_sample, m, self.review):
                    self.insufficient = 'PERFORMANCE_REVIEW_FAILED'
                    self.stop(m-1, self.insufficient)
            self.last_sample = self.last_value
        if not self.flat:
            self.active_weeks.add((t-FIRST)//WEEK)
            self.position_minutes += 1
            exposures = [float(q)*p for q, p in zip(self.q, marks)]
            gross, net = sum(map(abs, exposures)), sum(exposures)
            self.gross_integral += gross
            self.net_integral += net
            self.btc_integral += exposures[0]
            self.max_gross = max(self.max_gross, gross)
            self.max_abs_net = max(self.max_abs_net, abs(net))

    def snapshot(self):
        result = dict(vars(self))
        for k in ('cash', 'fees', 'funding', 'slippage', 'gross', 'turnover'):
            result[k] = str(result[k])
        for k in ('q', 'leg_cash', 'collateral'):
            result[k] = list(map(str, result[k]))
        result['queue'] = [[m, i, None if q is None else str(q), p] for m, i, q, p in self.queue]
        result['alert'] = dict(vars(self.alert))
        result['active_weeks'] = sorted(self.active_weeks)
        return result

    @classmethod
    def restore(cls, snapshot):
        snapshot = copy.deepcopy(snapshot)
        b = cls(snapshot['stress'], snapshot['delay'])
        for k, v in snapshot.items():
            if k in ('cash', 'fees', 'funding', 'slippage', 'gross', 'turnover'):
                v = D(v)
            elif k in ('q', 'leg_cash', 'collateral'):
                v = list(map(D, v))
            elif k == 'queue':
                v = deque((m, i, None if q is None else D(q), p) for m, i, q, p in v)
            elif k == 'alert':
                vars(b.alert).update(v)
                continue
            elif k == 'active_weeks':
                v = set(v)
            setattr(b, k, v)
        return b


class Simulation:
    def __init__(self, archive, horizon, delay=0, start=INITIAL, end=END):
        self.archive, self.horizon, self.delay = archive, horizon, delay
        self.start, self.end, self.minute = start, end, 0
        self.books = [Ledger(s, delay) for s in (False, True)]
        self.signals = []
        self.daily = [[start, 10000., 10000.]]
        self.daily_cash = [[start, '10000', '10000']]

    def link(self, m):
        if any(b.stopped or b.insufficient for b in self.books):
            for b in self.books:
                if b.stopped is None:
                    b.stop(m, 'PEER_PATH_REJECTED')

    def run_to(self, stop, deadline=None):
        need(self.start+self.minute*MINUTE <= stop <= self.end and stop % MINUTE == 0,
             'CONTINUATION_DOMAIN')
        while self.start+self.minute*MINUTE < stop:
            m = self.minute; t = self.start+m*MINUTE
            if m % 1440 == 0 and deadline is not None:
                need(time.monotonic() < deadline, 'COMPUTE_BUDGET')
            if (any(b.insufficient and b.insufficient != 'PERFORMANCE_REVIEW_FAILED' for b in self.books) or
                (any(b.insufficient for b in self.books) and all(b.flat and not b.queue for b in self.books))):
                break
            if FIRST <= t < LAST and (t-FIRST) % WEEK == 0 and not any(b.stopped for b in self.books):
                intent = choose(self.archive.daily, t, self.horizon)
                self.signals.append(intent)
                for b in self.books:
                    b.request_entry(m, intent)
            if FIRST <= t < LAST and (t-FIRST) % WEEK == WEEK-10*MINUTE:
                for b in self.books:
                    if not b.flat or b.queue:
                        b.request_exit(m, 'WEEK_END')
            bars, marks = self.archive.bars(t, 'trade'), self.archive.bars(t, 'mark')
            rates = self.archive.rates(t)
            for b in self.books:
                if b.stopped and b.flat and not b.queue:
                    continue
                b.fund(m, rates, [v[0] for v in marks])
                before = b.risk(marks)
                b.process(m, bars)
                for reason in before+b.risk(marks):
                    b.stop(m, reason)
            self.link(m)
            for b in self.books:
                b.close(m+1, t, [v[3] for v in marks])
            self.link(m)
            self.minute += 1
            if (t+MINUTE) % DAY == 0:
                self.daily.append([t+MINUTE]+[b.last_value for b in self.books])
                self.daily_cash.append([t+MINUTE]+[str(b.cash) if b.flat else None for b in self.books])
        if self.start+self.minute*MINUTE == self.end:
            for b in self.books:
                if not b.flat or b.queue:
                    b.insufficient = b.insufficient or 'TERMINAL_RESIDUAL'
        return self

    def snapshot(self):
        return dict(start=self.start, end=self.end, minute=self.minute, horizon=self.horizon,
                    delay=self.delay, books=[b.snapshot() for b in self.books],
                    signals=self.signals, daily=self.daily, daily_cash=self.daily_cash)

    @classmethod
    def restore(cls, archive, snap):
        s = cls(archive, snap['horizon'], snap['delay'], snap['start'], snap['end'])
        s.minute = snap['minute']
        s.books = [Ledger.restore(v) for v in snap['books']]
        s.signals = list(snap['signals'])
        s.daily = list(snap['daily'])
        s.daily_cash = list(snap['daily_cash'])
        return s


def select(prefixes):
    eligible = {}
    for horizon, snap in prefixes.items():
        if snap['start']+snap['minute']*MINUTE != HALF:
            continue
        books = snap['books']
        if any(b['stopped'] or b['insufficient'] or any(map(D, b['q'])) or b['queue'] for b in books):
            continue
        eligible[horizon] = min(D(b['cash'])-10000 for b in books)
    if not eligible:
        return None
    return max(sorted(eligible), key=lambda h: eligible[h])
