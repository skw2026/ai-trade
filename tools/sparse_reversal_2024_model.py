"""One-bar action sensitivity only. Original normal/control engine is unchanged."""
from frozen_history_transfer import FrozenPath, verdict
from offline_policy_correction import decide, direction, MS, WEEK
from execution_net_learning import WINDOW
from run_bounded_learning import need


class DelayedPath(FrozenPath):
    def __init__(self):
        super().__init__((0, -4))
        self.queued = 0.0

    def consume(self, sample):
        need(not getattr(self, 'finished', False), 'PATH_FINISHED')
        row = sample['row']; ts = int(row['timestamp'])
        if self.last_row is not None:
            need(ts == int(self.last_row['timestamp'])+MS, 'PATH_CLOCK')
        self.last_row = row; self.visited += 1
        mark = float(row['mark_close']); event = None
        if self.pending_exit:
            for b in self.books: b.settle(row, True)
            self.pending_exit = False; self.flat_latched = True; self.exit_at = ts
            self.queued = 0.0
            if any(b.wallet.drawdown >= .08 for b in self.books): self.reason = 'REFERENCE_RISK_STOP'
            event = dict(kind='EXIT_OPEN', timestamp=ts, reason=self.reason, wallets=self.snapshots(mark))
        elif not self.flat_latched:
            # Both the new decision and the queued target precede current-bar observations.
            decision = decide(sample['features'], self.coefficients, direction(self.books[0].last_target))
            desired, self.queued = self.queued, decision
            for b in self.books:
                b.wallet.fund(float(row['mark_open']), float(row['funding_rate_per_interval']))
            opening_risk = any(b.wallet.drawdown >= .08 for b in self.books)
            receipts = [b.step(row, desired, funded=True, halt=opening_risk) for b in self.books]
            self.active += 1; safety = None
            if any(b.wallet.drawdown >= .08 for b in self.books):
                self.queued = 0.0
                self._stop('REFERENCE_RISK_STOP', ts+MS)
            else:
                self.safety.observe(receipts[0], sample['bucket'])
                if self.safety.count == WINDOW:
                    safety = self.safety.assess()
                    need(not safety['withdrawn'], 'DIAGNOSTIC_CANNOT_REARM_OR_WITHDRAW')
                    if safety['would_withdraw'] and self.first_legacy_withdrawal is None:
                        self.first_legacy_withdrawal = ts+MS
            event = dict(kind='BAR', timestamp=ts, target=desired, decision_target=decision,
                         opening_risk=opening_risk, receipts=receipts, safety=safety,
                         reason=self.reason, wallets=self.snapshots(mark))
        self.exposure[direction(self.books[0].wallet.qty)+1] += 1
        if self.visited % WEEK == 0:
            equity = [b.last_liquidation for b in self.books]
            self.weekly.append([x-y for x, y in zip(equity, self.week_start)])
            self.week_start = equity
        return event

    def finish(self):
        self.queued = 0.0  # Do not execute the last signal at or beyond the fixed endpoint.
        return super().finish()


def decision(normal, control, delay, clusters):
    base, bounds = verdict(normal, control, clusters)
    sensitivity = ('REJECT_DELAY_RISK' if delay['reason'] != 'COMPLETE' else
                   'REJECT_DELAY_NET' if min(w['cash'] for w in delay['wallets']) <= 10000 else
                   'DELAY_REFERENCE_SUPPORTED')
    result = base
    if base == 'HISTORICAL_TRANSFER_SUPPORTED_NOT_DEPLOYABLE':
        result = ('DEVELOPMENT_SUPPORTED_CONFIRMATION_NOT_ADMITTED'
                  if sensitivity == 'DELAY_REFERENCE_SUPPORTED' else sensitivity)
    return dict(status=result, normal_verdict=base, delay_verdict=sensitivity, bounds=bounds,
                qualification=False, g4=False, training_updates=0, independent_confirmation_runs=0)
