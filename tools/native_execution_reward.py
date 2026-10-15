"""Synthetic native execution adapter for the *unchanged* LearningWindow.

No credential/config loading, network, historical data CLI, or promotion path.
The subprocess links only native account/OMS/risk/execution code. Journal replay
is an engineering test, not the production WAL or exchange reconciliation.
"""
import hashlib
import json
import math
from pathlib import Path
import select
import subprocess
from types import SimpleNamespace

from audit_native_execution_reward import DecimalBook, execution_price
from execution_net_learning import LearningWindow, target


class NativeSession:
    def __init__(self, driver):
        self.driver = str(Path(driver).resolve(strict=True))
        self.books, self.responses = {}, {}
        self.journal = []
        self.serial = self.audit_count = 0
        self.chain = '0'*64
        self._start()

    def _start(self):
        self.process = subprocess.Popen([self.driver, '--synthetic-only'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, bufsize=1)

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def identity(self, prefix='e'):
        self.serial += 1
        return prefix + str(self.serial)

    def _wire(self, line):
        self.process.stdin.write(line + '\n')
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], 5)[0]:
            raise TimeoutError('NATIVE_RECEIPT_MISSING')
        raw = self.process.stdout.readline()
        if not raw:
            raise RuntimeError('NATIVE_EXITED')
        result = json.loads(raw)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return result, digest

    def command(self, line, *, expect_error=False):
        result, digest = self._wire(line)
        self.chain = hashlib.sha256((self.chain + line + digest).encode()).hexdigest()
        self.journal.append((line, digest))
        if expect_error:
            assert result['ok'] is False, result
        elif not result['ok']:
            raise ValueError(result)
        return result

    def new(self, multiplier=1):
        book = self.identity('b')
        result = self.command('NEW {} {}'.format(book, multiplier))
        self.books[book] = DecimalBook(multiplier)
        self.books[book].check(result)
        self.responses[book] = result
        self.audit_count += 1
        return book

    def clone(self, book):
        dest = self.identity('b')
        result = self.command('CLONE {} {}'.format(book, dest))
        self.books[dest] = self.books[book].copy()
        self.books[dest].check(result)
        self.responses[dest] = result
        self.audit_count += 1
        return dest

    def view(self, book):
        result = self.command('VIEW ' + book)
        self.books[book].check(result)
        self.responses[book] = result
        self.audit_count += 1
        return result

    def event(self, book, kind, a=0, b=0, c=0, d=0, *, eid=None, timestamp=None,
              bucket=1, version=0, order='-', fill='-', side=0, expect_error=False):
        event = [eid or self.identity(),
                 self.responses[book]['time']+1 if timestamp is None else timestamp,
                 kind, bucket, version, a, b, c, d, order, fill, side]
        line = 'EV ' + book + ' ' + ' '.join(str(x) for x in event)
        result = self.command(line, expect_error=expect_error)
        if not expect_error:
            self.books[book].event(event, result)
            self.responses[book] = result
            self.audit_count += 1
        else:
            self.view(book)  # independent state check: rejected input did not mutate
        return result

    def restart(self):
        previous = self.process.pid
        self.close()
        self._start()
        assert self.process.pid != previous
        for line, expected in self.journal:
            _, digest = self._wire(line)
            if digest != expected:
                raise AssertionError('NATIVE_REPLAY_DIVERGENCE')
        for book in self.books:
            self.view(book)
        return previous, self.process.pid


class NativeLedger:
    def __init__(self, session, multiplier=1):
        self.session = session
        self.multiplier = multiplier
        self.book = session.new(multiplier)
        self.last_target = 0.0
        self.last_liquidation = 10000.0
        self.version = 0
        self.bucket = 1

    @property
    def state(self):
        return self.session.responses[self.book]

    @property
    def wallet(self):
        # Only the historical learner's drawdown accessor is needed.
        return SimpleNamespace(drawdown=self.state['drawdown'])

    def copy(self):
        child = object.__new__(NativeLedger)
        child.__dict__ = dict(self.__dict__)
        child.book = self.session.clone(self.book)
        return child

    def event(self, kind, *args, **kwargs):
        return self.session.event(self.book, kind, *args, bucket=self.bucket,
                                  version=self.version, **kwargs)

    def mark(self, price, mark, **kwargs):
        return self.event('MARK', float(price), float(mark), **kwargs)

    def full_fill(self, intent):
        # Synthetic deterministic fill. Not an exchange acknowledgement model.
        price = execution_price(self.state['quote'], intent['side'], self.multiplier)
        quantity = intent['qty']
        fee = float(price) * quantity * self.multiplier * .00055
        return self.event('FILL', quantity, float(price), fee, self.state['quote'],
                          order=intent['id'], fill=self.session.identity('f'), side=intent['side'])

    def trade(self, desired):
        for _ in range(2):
            before = self.state['qty']
            result = self.event('TARGET', float(desired), order=self.session.identity('o'))
            intent = result['intent']
            if intent is None:
                break
            self.full_fill(intent)
            # The native default flip must close and confirm before a new entry.
            if not (before * desired < 0 and abs(self.state['qty']) < 1e-10):
                break

    @staticmethod
    def validate_row(row):
        if row.get('_synthetic') is not True:
            raise ValueError('SYNTHETIC_EVENTS_ONLY')
        values = [float(row[k]) for k in ('open', 'price', 'mark_open', 'mark_close', 'mark_high', 'mark_low')]
        if not all(math.isfinite(x) and 1 <= x <= 1e8 for x in values):
            raise ValueError('SYNTHETIC_PRICES')
        rate = float(row['funding_rate_per_interval'])
        if not math.isfinite(rate) or abs(rate) > 1:
            raise ValueError('SYNTHETIC_FUNDING')

    def step(self, row, desired, *, funded=False, halt=False):
        self.validate_row(row)
        if not math.isfinite(desired):
            raise ValueError('TARGET_FINITE')
        self.mark(row['open'], row['mark_open'], timestamp=int(row['timestamp']))
        if not funded:
            self.event('FUND', float(row['funding_rate_per_interval']))
        if halt:
            self.event('LOCK')
        locked = self.state['hard_lock'] or self.state['manual_lock']
        cap = abs(self.state['qty'])*max(float(row['open']), float(row['mark_open'])) > 2500+1e-8
        changed = not math.isclose(desired, self.last_target, rel_tol=0, abs_tol=1e-8)
        if locked or cap or changed:
            self.trade(0 if locked else desired)
            self.last_target = 0 if locked else desired
        hi, lo = float(row['mark_high']), float(row['mark_low'])
        favorable, adverse = (hi, lo) if self.state['qty'] >= 0 else (lo, hi)
        self.mark(row['open'], favorable)
        self.mark(row['open'], adverse)
        self.mark(row['price'], row['mark_close'])
        value = self.state['liquidation']
        receipt = value - self.last_liquidation
        self.last_liquidation = value
        return receipt

    def settle(self, row, at_open=False):
        self.validate_row(row)
        self.mark(row['open' if at_open else 'price'], row['mark_open' if at_open else 'mark_close'])
        if at_open:
            self.event('FUND', float(row['funding_rate_per_interval']))
        self.trade(0)
        self.last_target = 0.0
        self.last_liquidation = self.state['liquidation']
        return dict(self.state)


class NativeLearningWindow(LearningWindow):
    """Route lineage only; selection/holdout/update rules remain inherited.

Policy versions are local to each book; LearningWindow.vectors binds each book
pair to its candidate vector. Neither this wrapper nor the native bridge has
promotion authority or modifies an actual trading policy.
"""
    def step(self, row, signal):
        NativeLedger.validate_row(row)
        if signal is not None:
            bucket = signal['bucket']
            if type(bucket) is not int or bucket not in (0, 1, 2):
                raise ValueError('DECISION_BUCKET')
            # Validate all proposals before the first candidate mutates.
            for vector in self.vectors.values():
                target(signal, vector)
            for books in self.books.values():
                for book in books:
                    book.bucket = bucket
        return super().step(row, signal)
