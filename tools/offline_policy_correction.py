"""Bounded development-only policy fitting; never an activation or restart API.

Each trial owns a single permanently stoppable path. The optimizer, unlike a
trading path, may continue comparing predeclared hypotheses after a trial stops.
"""
import math

from execution_net_learning import ExecutionLedger, Safety, WINDOW
from run_bounded_learning import need, hac_lower

GRID = (-4, -2, -1, 0, 1, 2, 4)
BASELINE = (1, 0)
MS = 300000
WEEK = 2016


def features(history):
    """Only completed bars; return units are average fraction per 5-minute bar."""
    if len(history) < 288:
        return None
    last = float(history[-1]['price'])
    return tuple(max(-.01, min(.01, (last / float(history[-h]['open']) - 1) / h))
                 for h in (12, 288))


def direction(value):
    return 1 if value > 1e-8 else -1 if value < -1e-8 else 0


def decide(x, coefficients, previous_direction):
    need(previous_direction in (-1, 0, 1), 'DIRECTION')
    need(len(coefficients) == 2 and all(v in GRID for v in coefficients), 'COEFFICIENT_GRID')
    if x is None:
        return 0.0
    need(len(x) == 2 and all(math.isfinite(v) and abs(v) <= .01 for v in x), 'FEATURES')
    mean = sum(a * b for a, b in zip(x, coefficients))
    score = lambda d: d * mean - .00065 * (abs(d - previous_direction) + abs(d))
    # Stable tie: hold, then flat, then the remaining declared order.
    actions = list(dict.fromkeys((previous_direction, 0, -1, 1)))
    best = max(actions, key=score)
    return float(2500 * best)


class Path:
    """One paired base/stress trajectory; neither book can be rearmed."""
    def __init__(self, coefficients):
        need(len(coefficients) == 2 and all(v in GRID for v in coefficients), 'COEFFICIENT_GRID')
        self.coefficients = tuple(coefficients)
        self.books = [ExecutionLedger(), ExecutionLedger(2)]
        self.safety = Safety()
        self.reason = 'COMPLETE'
        self.stop_at = None
        self.exit_at = None
        self.pending_exit = False
        self.flat_latched = False
        self.visited = 0
        self.active = 0
        self.exposure = [0, 0, 0]  # short, flat, long; full fixed domain.
        self.weekly = []
        self.week_start = [10000., 10000.]
        self.last_row = None

    def snapshots(self, mark):
        return [b.snapshot(mark) for b in self.books]

    def _stop(self, reason, at):
        need(not self.flat_latched and not self.pending_exit, 'PATH_ALREADY_STOPPED')
        self.reason = reason
        self.stop_at = at
        self.pending_exit = True

    def consume(self, sample):
        need(not getattr(self, 'finished', False), 'PATH_FINISHED')
        row = sample['row']
        ts = int(row['timestamp'])
        if self.last_row is not None:
            need(ts == int(self.last_row['timestamp']) + MS, 'PATH_CLOCK')
        self.last_row = row
        self.visited += 1
        mark = float(row['mark_close'])
        event = None
        if self.pending_exit:
            for b in self.books:
                b.settle(row, True)
            self.pending_exit = False
            self.flat_latched = True
            self.exit_at = ts
            if any(b.wallet.drawdown >= .08 for b in self.books):
                self.reason = 'REFERENCE_RISK_STOP'
            event = dict(kind='EXIT_OPEN', timestamp=ts, reason=self.reason,
                         wallets=self.snapshots(mark))
        elif not self.flat_latched:
            # Signal choice occurs before reading any prices/funding of this bar.
            desired = decide(sample['features'], self.coefficients,
                             direction(self.books[0].last_target))
            for b in self.books:
                b.wallet.fund(float(row['mark_open']), float(row['funding_rate_per_interval']))
            opening_risk = any(b.wallet.drawdown >= .08 for b in self.books)
            receipts = [b.step(row, desired, funded=True, halt=opening_risk) for b in self.books]
            self.active += 1
            safety = None
            if any(b.wallet.drawdown >= .08 for b in self.books):
                self._stop('REFERENCE_RISK_STOP', ts + MS)
            else:
                self.safety.observe(receipts[0], sample['bucket'])
                if self.safety.count == WINDOW:
                    safety = self.safety.assess()
                    if safety['withdrawn']:
                        self._stop('SAFETY_WITHDRAWAL', ts + MS)
            event = dict(kind='BAR', timestamp=ts, target=desired,
                         opening_risk=opening_risk, receipts=receipts,
                         safety=safety, reason=self.reason, wallets=self.snapshots(mark))
        self.exposure[direction(self.books[0].wallet.qty) + 1] += 1
        if self.visited % WEEK == 0:
            equity = [b.last_liquidation for b in self.books]
            self.weekly.append([x - y for x, y in zip(equity, self.week_start)])
            self.week_start = equity
        return event

    def finish(self):
        need(self.last_row is not None, 'EMPTY_PATH')
        need(not getattr(self, 'finished', False), 'PATH_FINISHED')
        self.finished = True
        # Fixed end is known beforehand. No fictitious intrabar stop execution.
        if not self.flat_latched:
            for b in self.books:
                b.settle(self.last_row, False)
            if any(b.wallet.drawdown >= .08 for b in self.books):
                if self.stop_at is None:
                    self.stop_at = int(self.last_row['timestamp']) + MS
                self.reason = 'REFERENCE_RISK_STOP'
            self.exit_at = int(self.last_row['timestamp']) + MS
            self.pending_exit = False
        self.flat_latched = True
        mark = float(self.last_row['mark_close'])
        cash = [b.wallet.cash for b in self.books]
        # If the last bar closes a full week, include deterministic settlement.
        if self.visited % WEEK == 0:
            for i in range(2):
                self.weekly[-1][i] += cash[i] - self.week_start[i]
            self.week_start = cash
        return dict(coefficients=list(self.coefficients), reason=self.reason,
                    stop_at_ms=self.stop_at, exit_at_ms=self.exit_at,
                    domain_bars=self.visited, active_bars=self.active,
                    exposure_short_flat_long=self.exposure,
                    wallets=self.snapshots(mark), weekly=self.weekly,
                    partial_week=[x-y for x, y in zip(cash, self.week_start)],
                    objective=min(cash)-10000, terminal_flat=True)


def replay(samples, coefficients, emit=None):
    path = Path(coefficients)
    for sample in samples:
        event = path.consume(sample)
        if emit and event is not None:
            emit(event)
    result = path.finish()
    if emit:
        emit(dict(kind='TERMINAL', result=result))
    return result


def optimize(evaluate, *, domain='development_train'):
    need(domain == 'development_train', 'TRAIN_DOMAIN_ONLY')
    current = BASELINE
    trials = {}
    steps = []

    def get(vector):
        if vector not in trials:
            need(len(trials) < 25, 'TRAINING_VECTOR_BUDGET')
            result = evaluate(vector)
            need(math.isfinite(result['objective']), 'FINITE_OBJECTIVE')
            trials[vector] = result
        return trials[vector]

    get(current)
    for sweep in range(2):
        for axis in range(2):
            before = current
            best = current
            for value in GRID:
                trial = list(before)
                trial[axis] = value
                trial = tuple(trial)
                if get(trial)['objective'] > get(best)['objective']:
                    best = trial
            current = best
            steps.append(dict(sweep=sweep, axis=axis, before=list(before),
                              after=list(current), objective=get(current)['objective']))
    return dict(coefficients=list(current), steps=steps, winner=get(current),
                trials=[dict(coefficients=list(k), result=v) for k, v in trials.items()],
                evaluations=len(trials), changed=current != BASELINE,
                scope='DEVELOPMENT_TRAIN_ONLY')


def verdict(model, fixed, learned):
    need(len(fixed['weekly']) == len(learned['weekly']), 'PAIRED_CALENDAR')
    lower = dict(stress=hac_lower([w[1] for w in learned['weekly']]),
                 paired_base=hac_lower([a[0]-b[0] for a,b in zip(learned['weekly'],fixed['weekly'])]),
                 paired_stress=hac_lower([a[1]-b[1] for a,b in zip(learned['weekly'],fixed['weekly'])]))
    if model['winner']['reason'] != 'COMPLETE' or learned['reason'] != 'COMPLETE':
        result = 'NO_GO_SAFETY_OR_REFERENCE_RISK'
    elif not model['changed'] or min(w['cash'] for w in learned['wallets']) <= 10000:
        result = 'NO_GO_NO_ABSOLUTE_LEARNING_EDGE'
    elif not all(v is not None and v > 0 for v in lower.values()):
        result = 'INSUFFICIENT_DEVELOPMENT_EVIDENCE'
    else:
        result = 'DEVELOPMENT_SUPPORTED_CONFIRMATION_UNAVAILABLE'
    return result, lower
