"""One frozen-model prediction diagnostic; no fit, portfolio, network or orders."""
import datetime as dt
from pathlib import Path
import sys
import time

import run_open_market_rolling as prior
import open_market_learning as model_code
import audit_open_market_learning as independent
import btc_old_output_model as data

ROOT = prior.ROOT/'prediction-diagnostic'
PLAN = 'docs/plans/2026-10-19-open-market-prediction-diagnostic.md'
SOURCES = ['tools/'+name for name in ('open_market_prediction_diagnostic.py',
    'test_open_market_prediction_diagnostic.py', 'run_open_market_prediction_diagnostic.py')]
BOUNDARIES = prior.UPDATES+[prior.END]
STARTED = dt.datetime.fromisoformat('2026-10-19T10:24:00.431312+00:00')
read, sha, save = prior.read, prior.sha, prior.save


def now():
    return dt.datetime.now(dt.timezone.utc)


def initialize():
    prior.active()
    prior.preserve()
    model_code.need(read(prior.ROOT/'result.json')['status'] == 'BATCH_COMPLETED', 'COMPLETED_PARENT')
    files = dict(read(prior.ROOT/'freeze.json')['files'])
    for p in prior.ROOT.glob('*.json'):
        if p.name != 'validation-state.json':
            files[str(p)] = sha(p)
    ROOT.mkdir(parents=True, exist_ok=False)
    save(ROOT/'scope.json', dict(started_utc=STARTED.isoformat(),
        deadline_utc=(STARTED+dt.timedelta(hours=1)).isoformat(), plan_sha256=sha(PLAN),
        maximum_computations=1, fits=0, portfolio_paths=0, network=0))
    save(ROOT/'protected.json', dict(files=files))
    print('ONE_PREDICTION_DIAGNOSTIC_REGISTERED')


def preserve():
    count = prior.preserve()
    model_code.need(sha(PLAN) == read(ROOT/'scope.json')['plan_sha256'], 'DIAGNOSTIC_PLAN')
    protected = read(ROOT/'protected.json')['files']
    model_code.need(all(sha(p) == h for p, h in protected.items()), 'DIAGNOSTIC_PRIOR_IDENTITIES')
    return count+len(protected)


def freeze():
    prior.active()
    count = preserve()
    save(ROOT/'freeze.json', dict(files={p: sha(p) for p in SOURCES},
        protected_bindings=count, boundaries=BOUNDARIES, at=now().isoformat()))
    print('PREDICTION_DIAGNOSTIC_FROZEN')


def execute():
    from open_market_prediction_diagnostic import evaluate, audit
    prior.active()
    preserve()
    frozen, scope = read(ROOT/'freeze.json'), read(ROOT/'scope.json')
    model_code.need(all(sha(p) == h for p, h in frozen['files'].items()), 'DIAGNOSTIC_SOURCE')
    model_code.need(now() < dt.datetime.fromisoformat(scope['deadline_utc']), 'DIAGNOSTIC_BUDGET')
    save(ROOT/'attempt.json', dict(at=now().isoformat(), freeze_sha256=sha(ROOT/'freeze.json')))
    begun = time.monotonic()
    rows = data.load_rows(list(prior.previous.INPUTS)[1])
    fs = {int(t): f for t, f in read(prior.OLD/'2025-features.json').items()
          if prior.START <= int(t) < prior.END}
    labels = model_code.samples(rows, fs, prior.START, prior.END)
    save(ROOT/'labels.json', labels)
    save(ROOT/'label-audit.json', independent.audit_features_and_labels(
        rows, fs, labels, prior.START, prior.END))
    schedule = read(prior.ROOT/'model-schedule.json')['schedule']
    old = read(prior.OLD/'evaluation-models.json')['models']['PV']
    model_code.need(len(schedule) == 6 and [e['t'] for e in schedule] == prior.UPDATES,
                    'DIAGNOSTIC_FROZEN_SCHEDULE')
    models = {e['model_id']: e['model'] for e in schedule}
    models['old-2021'] = old
    streams = {}
    for name in ('old2021', 'recent2025', 'rolling'):
        stream = []
        groups = {}
        for t in range(prior.START, prior.END, data.DAY):
            identity = ('old-2021' if name == 'old2021' else schedule[0]['model_id']
                        if name == 'recent2025' else next(e['model_id'] for e in reversed(schedule) if e['t'] <= t))
            prediction = model_code.predict(models[identity], fs[t], t)
            groups.setdefault(identity, []).append(prediction)
            stream.append(dict(prediction, model_id=identity))
        for identity, predictions in groups.items():
            independent.audit_predictions(models[identity], {p['t']: fs[p['t']] for p in predictions}, predictions)
        streams[name] = stream
    save(ROOT/'shadow-predictions.json', streams)
    result = evaluate(labels, streams, BOUNDARIES)
    receipt = audit(labels, streams, BOUNDARIES, result)
    save(ROOT/'independent-audit.json', receipt)
    model_code.need(all(sha(p) == h for p, h in frozen['files'].items()), 'DIAGNOSTIC_POST_SOURCE')
    model_code.need(now() < dt.datetime.fromisoformat(scope['deadline_utc']) and time.monotonic()-begun < 600,
                    'DIAGNOSTIC_BUDGET')
    model_code.need(sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file()) < 64*1024*1024,
                    'DIAGNOSTIC_EVIDENCE_BUDGET')
    save(ROOT/'result.json', dict(status='DIAGNOSTIC_COMPLETED', diagnostic=result,
        compute_seconds=time.monotonic()-begun, completed_utc=now().isoformat(),
        protected_bindings=preserve(), fits=0, portfolio_paths=0, network=0,
        freeze_sha256=sha(ROOT/'freeze.json')))
    print(result, flush=True)


if __name__ == '__main__':
    {'initialize': initialize, 'freeze': freeze, 'execute': execute}[sys.argv[1]]()
