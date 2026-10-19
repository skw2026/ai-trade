"""Exclusive fixed action-label evaluation of already frozen prediction streams."""
import datetime as dt
from pathlib import Path
import sys
import time

import run_open_market_prediction_diagnostic as previous
from open_market_learning import need

ROOT = Path('.artifacts/open-market-action-20261019')
PLAN = 'docs/plans/2026-10-19-open-market-action-value.md'
OLD_REPORT = 'docs/reviews/2026-10-19-open-market-rolling-learning-result.md'
SOURCES = ['tools/'+name for name in ('open_market_action.py', 'audit_open_market_action.py',
    'test_open_market_action.py', 'run_open_market_action.py')]
read, sha, save = previous.read, previous.sha, previous.save


def now():
    return dt.datetime.now(dt.timezone.utc)


def active():
    need(read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'ACTION_ACTIVE_GATE')


def initialize():
    active()
    previous.preserve()
    need(read(previous.prior.ROOT/'validation-state.json')['status'] == 'READY', 'PARENT_COMPLETE')
    files = {p: sha(p) for p in previous.SOURCES+[previous.PLAN, OLD_REPORT]}
    for p in previous.prior.ROOT.rglob('*'):
        if p.is_file() and '__pycache__' not in str(p):
            files[str(p)] = sha(p)
    save(ROOT/'protected.json', dict(files=files))
    save(ROOT/'scope.json', dict(started_utc=now().isoformat(),
        deadline_utc=(now()+dt.timedelta(hours=2)).isoformat(), plan_sha256=sha(PLAN),
        maximum_computations=1, fits=0, portfolio_paths=0, network=0))
    print('ACTION_SCOPE_REGISTERED', preserve())


def preserve():
    count = previous.preserve()
    need(sha(PLAN) == read(ROOT/'scope.json')['plan_sha256'], 'ACTION_PLAN_CHANGED')
    protected = read(ROOT/'protected.json')['files']
    need(all(sha(p) == h for p, h in protected.items()), 'ACTION_PRIOR_CHANGED')
    return count+len(protected)


def freeze():
    active()
    count = preserve()
    save(ROOT/'freeze.json', dict(files={p: sha(p) for p in SOURCES},
        protected_bindings=count, at=now().isoformat(), boundaries=previous.BOUNDARIES))
    print('ACTION_SOURCE_INPUT_FROZEN')


def execute():
    from open_market_action import evaluate
    from audit_open_market_action import audit
    active()
    preserve()
    frozen, scope = read(ROOT/'freeze.json'), read(ROOT/'scope.json')
    need(all(sha(p) == h for p, h in frozen['files'].items()), 'ACTION_SOURCE_CHANGED')
    need(now() < dt.datetime.fromisoformat(scope['deadline_utc']), 'ACTION_TIME_BUDGET')
    save(ROOT/'attempt.json', dict(at=now().isoformat(), freeze_sha256=sha(ROOT/'freeze.json')))
    begun = time.monotonic()
    labels = read(previous.ROOT/'labels.json')
    streams = read(previous.ROOT/'shadow-predictions.json')
    result = evaluate(labels, streams, previous.BOUNDARIES)
    receipt = audit(labels, streams, previous.BOUNDARIES, result)
    save(ROOT/'independent-audit.json', receipt)
    need(now() < dt.datetime.fromisoformat(scope['deadline_utc']) and time.monotonic()-begun < 600,
         'ACTION_TIME_BUDGET')
    need(sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file()) < 32*1024*1024,
         'ACTION_EVIDENCE_BUDGET')
    need(all(sha(p) == h for p, h in frozen['files'].items()), 'ACTION_POST_SOURCE_CHANGED')
    save(ROOT/'result.json', dict(status='ACTION_DIAGNOSTIC_COMPLETED', diagnostic=result,
        completed_utc=now().isoformat(), compute_seconds=time.monotonic()-begun,
        protected_bindings=preserve(), fits=0, portfolio_paths=0, network=0,
        freeze_sha256=sha(ROOT/'freeze.json')))
    print(result, flush=True)


if __name__ == '__main__':
    {'initialize': initialize, 'freeze': freeze, 'execute': execute}[sys.argv[1]]()
