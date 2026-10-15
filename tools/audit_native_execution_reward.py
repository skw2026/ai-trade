"""Independent Decimal cash-flow audit; no strategy, native PnL, or market import.

Margin cash is reconciled to economic cash explicitly. Exit reserves are marks,
not cash debits. Every accepted event, including clones and duplicates, is checked.
"""
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
import copy

TOLERANCE = D('0.00000005')


def close(actual, expected, field):
    value = D(str(actual))
    if not value.is_finite() or abs(value - expected) > TOLERANCE:
        raise AssertionError((field, str(value), str(expected)))


def execution_price(quote, side, multiplier):
    ticks = D(str(quote)) * (1 + D(side * multiplier) / 10000) * 10
    return ticks.to_integral_value(rounding=ROUND_CEILING if side > 0 else ROUND_FLOOR) / 10


class DecimalBook:
    def __init__(self, multiplier):
        self.multiplier = multiplier
        self.cash = self.peak = self.last_liquidation = D(10000)
        self.qty = self.average = self.fees = self.funding = D(0)
        self.realized = self.slippage = self.turnover = self.drawdown = D(0)
        self.quote = self.mark = D(10000)
        self.manual_lock = self.hard_lock = False
        self.time = 0
        self.events, self.fills, self.orders = {}, {}, {}

    def copy(self):
        return copy.deepcopy(self)

    def liquidation(self):
        if not self.qty:
            return self.cash
        price = execution_price(self.quote, -1 if self.qty > 0 else 1, self.multiplier)
        return self.cash + self.qty * price - abs(self.qty) * price * D(self.multiplier) * D('.00055')

    def check(self, receipt, reward=D(0)):
        equity = self.cash + self.qty * self.mark
        liquidation = self.liquidation()
        fields = dict(qty=self.qty, average=self.average, economic_cash=self.cash,
                      margin_cash=self.cash + self.qty * self.average,
                      equity=equity, liquidation=liquidation, exit_reserve=equity-liquidation,
                      fees=self.fees, funding=self.funding, realized=self.realized,
                      slippage=self.slippage, turnover=self.turnover, peak=self.peak,
                      drawdown=self.drawdown, quote=self.quote, mark=self.mark, reward=reward)
        for name, value in fields.items():
            close(receipt[name], value, name)
        assert receipt['manual_lock'] == self.manual_lock
        assert receipt['hard_lock'] == self.hard_lock
        assert receipt['time'] == self.time
        assert receipt['events'] == len(self.events) and receipt['fills'] == len(self.fills)
        assert receipt['pending'] == sum(o['pending'] for o in self.orders.values())
        assert receipt['synthetic_only'] is True

    def event(self, event, receipt):
        eid, ts, kind, bucket, version, a, b, c, d, order, fill, side = event
        identity = tuple(event[1:])
        fill_identity = (order, fill, side, a, b, c, d)
        duplicate = eid in self.events or kind == 'FILL' and fill in self.fills
        assert receipt['duplicate'] == duplicate
        assert (receipt['event'], receipt['kind'], receipt['bucket'], receipt['version'],
                receipt['order_id'], receipt['fill_id']) == (eid, kind, bucket, version, order, fill)
        if eid in self.events:
            assert self.events[eid] == identity
            self.check(receipt)
            return
        if kind == 'FILL' and fill in self.fills:
            assert self.fills[fill] == fill_identity
            self.events[eid] = identity
            self.check(receipt)
            return
        assert ts >= self.time
        a, b, c, d = (D(str(x)) for x in (a, b, c, d))
        if kind == 'MARK':
            self.quote, self.mark = a, b
        elif kind == 'FUND':
            payment = self.qty * self.mark * a
            self.cash -= payment
            self.funding += payment
        elif kind == 'LOCK':
            self.manual_lock = True
        elif kind == 'TARGET':
            intent = receipt['intent']
            if intent:
                assert intent['id'] == order and order not in self.orders
                quantity = D(str(intent['qty']))
                assert quantity > 0 and intent['side'] in (-1, 1)
                if self.manual_lock or self.hard_lock:
                    assert intent['reduce_only'] and self.qty * intent['side'] < 0
                    assert quantity <= abs(self.qty) + TOLERANCE
                if intent['reduce_only']:
                    assert self.qty * intent['side'] < 0 and quantity <= abs(self.qty) + TOLERANCE
                self.orders[order] = dict(qty=quantity, filled=D(0), side=intent['side'],
                                          pending=True, version=str(version))
        elif kind == 'FILL':
            previous = self.qty
            delta = D(side) * a
            record = self.orders[order]
            assert side == record['side'] and record['filled'] + a <= record['qty'] + TOLERANCE
            self.cash -= delta * b + c
            self.qty += delta
            if previous == 0 or previous * delta > 0:
                self.average = (abs(previous) * self.average + a * b) / abs(self.qty)
            else:
                self.realized += min(abs(previous), a) * (b-self.average) * (1 if previous > 0 else -1)
                if self.qty == 0:
                    self.average = D(0)
                elif previous * self.qty < 0:
                    self.average = b
            self.fees += c
            self.slippage += delta * (b-d)
            self.turnover += a*b
            record['filled'] += a
            record['pending'] = record['filled'] < record['qty'] - TOLERANCE
            self.fills[fill] = fill_identity
            assert receipt['decision_version'] == record['version']
        elif kind == 'REJECT':
            self.orders[order]['pending'] = False
        elif kind in ('CANCEL', 'ACK'):
            assert self.orders[order]['pending']
        else:
            raise AssertionError(kind)
        equity = self.cash + self.qty*self.mark
        self.peak = max(self.peak, equity)
        self.drawdown = max(self.drawdown, 1-equity/self.peak)
        self.hard_lock |= self.drawdown >= D('.08')
        self.time = ts
        self.events[eid] = identity
        value = self.liquidation()
        reward = value - self.last_liquidation
        self.last_liquidation = value
        self.check(receipt, reward)
