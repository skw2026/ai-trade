"""Research-only intent/queue separation. No account, fitting or activation API."""
import math

from frozen_history_transfer import FrozenPath
from offline_policy_correction import decide, direction, MS, WEEK
from execution_net_learning import WINDOW
from run_bounded_learning import need


class IntentState:
    """Synthetic contract example only; the real experiment reads frozen intents."""
    def __init__(self):
        self.target = 0.0
        self.stopped = False

    def update(self, completed_features):
        need(not self.stopped, 'INTENT_STOPPED')
        self.target = decide(completed_features, (0,-4), direction(self.target))
        return self.target

    def stop(self):
        self.target = 0.0
        self.stopped = True


class OneBarQueue:
    """Consumes an exogenous causal target, never actual position feedback."""
    def __init__(self):
        self.intent = 0.0
        self.pending = 0.0
        self.last_ts = None
        self.stopped = False

    def advance(self, ts, intent):
        need(not self.stopped, 'QUEUE_STOPPED')
        need(type(ts) is int and ts % MS == 0, 'QUEUE_TIME')
        need(type(intent) in (int,float) and math.isfinite(intent) and intent in (-2500,0,2500), 'INTENT_VALUE')
        need(self.last_ts is None or ts == self.last_ts+MS, 'QUEUE_CLOCK')
        due, origin = self.pending, self.last_ts
        self.intent = float(intent); self.pending = float(intent); self.last_ts = ts
        return due, origin

    def cancel(self):
        self.pending = 0.0
        self.stopped = True


class FixedIntentPath(FrozenPath):
    def __init__(self):
        super().__init__((0,-4))
        self.queue = OneBarQueue()

    def consume(self, sample, intent):
        need(not getattr(self, 'finished', False), 'PATH_FINISHED')
        row = sample['row']; ts = int(row['timestamp'])
        if self.last_row is not None: need(ts == int(self.last_row['timestamp'])+MS, 'PATH_CLOCK')
        self.last_row = row; self.visited += 1
        mark = float(row['mark_close']); event = None
        if self.pending_exit:
            for b in self.books: b.settle(row, True)
            self.pending_exit = False; self.flat_latched = True; self.exit_at = ts
            self.queue.cancel()
            if any(b.wallet.drawdown >= .08 for b in self.books): self.reason = 'REFERENCE_RISK_STOP'
            event = dict(kind='EXIT_OPEN', timestamp=ts, reason=self.reason, wallets=self.snapshots(mark))
        elif not self.flat_latched:
            desired, origin = self.queue.advance(ts, intent)
            for b in self.books:
                b.wallet.fund(float(row['mark_open']), float(row['funding_rate_per_interval']))
            opening_risk = any(b.wallet.drawdown >= .08 for b in self.books)
            receipts = [b.step(row, desired, funded=True, halt=opening_risk) for b in self.books]
            self.active += 1; safety = None
            if any(b.wallet.drawdown >= .08 for b in self.books):
                self.queue.cancel(); self._stop('REFERENCE_RISK_STOP', ts+MS)
            else:
                self.safety.observe(receipts[0], sample['bucket'])
                if self.safety.count == WINDOW:
                    safety = self.safety.assess()
                    need(not safety['withdrawn'], 'DIAGNOSTIC_ONLY')
                    if safety['would_withdraw'] and self.first_legacy_withdrawal is None:
                        self.first_legacy_withdrawal = ts+MS
            event = dict(kind='BAR', timestamp=ts, target=desired, intent_target=float(intent),
                intent_origin_ms=origin, queue_cancelled=self.queue.stopped,
                opening_risk=opening_risk, receipts=receipts, safety=safety,
                reason=self.reason, wallets=self.snapshots(mark))
        self.exposure[direction(self.books[0].wallet.qty)+1] += 1
        if self.visited % WEEK == 0:
            equity = [b.last_liquidation for b in self.books]
            self.weekly.append([x-y for x,y in zip(equity,self.week_start)]); self.week_start = equity
        return event

    def finish(self):
        self.queue.cancel()
        return super().finish()


def classify(summary):
    need(summary['terminal_flat'] and len(summary['wallets']) == 2 and
         all(w['qty'] == 0 and math.isfinite(w['cash']) for w in summary['wallets']), 'INCOMPLETE_ACCOUNT')
    if summary['reason'] != 'COMPLETE': return 'REJECT_FIXED_INTENT_DELAY_RISK'
    if min(w['cash'] for w in summary['wallets']) <= 10000: return 'REJECT_FIXED_INTENT_DELAY_NET'
    return 'FIXED_INTENT_DELAY_SUPPORTED_NOT_CLOSED_LOOP'
