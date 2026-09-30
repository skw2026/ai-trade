"""Development-only risk-path comparison, never an online safety bypass API.

Reuse unchanged execution/risk/selection. Only the short-window development
stop is diagnostic; its first hit remains a permanent legacy rejection.
"""
from offline_policy_correction import Path, optimize, verdict
from run_bounded_learning import need

MODE = 'DEVELOPMENT_DIAGNOSTIC_ONLY'


class LossDiagnostic:
    def __init__(self):
        self.count = 0
        self.net = [0.]*4
        self.streak = [0]*4
        self.triggered_windows = 0

    def observe(self, receipt, bucket):
        need(bucket in (0, 1, 2), 'DIAGNOSTIC_BUCKET')
        self.net[bucket] += receipt
        self.net[3] += receipt
        self.count += 1

    def assess(self):
        need(self.count == 240, 'DIAGNOSTIC_WINDOW')
        self.streak = [n+1 if p < 0 else 0 for n, p in zip(self.streak, self.net)]
        would_withdraw = any(n >= 2 for n in self.streak)
        self.triggered_windows += int(would_withdraw)
        result = dict(mode=MODE, net_by_decision_bucket_and_total=self.net[:],
                      streak=self.streak[:], would_withdraw=would_withdraw,
                      withdrawn=False)
        self.count = 0
        self.net = [0.]*4
        return result


class DevelopmentPath(Path):
    def __init__(self, coefficients, *, domain='development'):
        need(domain == 'development', 'DEVELOPMENT_ONLY')
        super().__init__(coefficients)
        self.safety = LossDiagnostic()
        self.first_legacy_withdrawal = None

    def consume(self, sample):
        event = super().consume(sample)
        if event and event.get('safety') and event['safety']['would_withdraw']:
            if self.first_legacy_withdrawal is None:
                self.first_legacy_withdrawal = event['timestamp']+300000
        return event

    def finish(self):
        result = super().finish()
        status = ('REJECT_LEGACY_SAFETY' if self.first_legacy_withdrawal is not None else
                  'REJECT_REFERENCE_RISK' if self.reason != 'COMPLETE' else
                  'PATH_CONSTRAINTS_ONLY_NOT_QUALIFICATION')
        result.update(safety_mode=MODE, first_legacy_withdrawal_ms=self.first_legacy_withdrawal,
                      diagnostic_triggered_windows=self.safety.triggered_windows,
                      legacy_path_status=status, qualification=False)
        return result


def replay(samples, coefficients, emit=None):
    path = DevelopmentPath(coefficients)
    for sample in samples:
        event = path.consume(sample)
        if emit and event is not None:
            emit(event)
    result = path.finish()
    if emit:
        emit(dict(kind='TERMINAL', result=result))
    return result
