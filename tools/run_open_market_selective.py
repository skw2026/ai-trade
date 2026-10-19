"""One bounded selection-calibration batch; no network or account operations."""
import datetime as dt
import json
from pathlib import Path
import sys
import time

import run_open_market_action as previous
import run_open_market_learning as original
import btc_old_output_model as data
import open_market_learning as learning

ROOT = Path('.artifacts/open-market-selective-20261019')
PLAN = 'docs/plans/2026-10-19-open-market-selective-calibration.md'
OLD_REPORT = 'docs/reviews/2026-10-19-open-market-action-value-result.md'
SOURCES = ['tools/'+name for name in (
    'open_market_selective.py', 'test_open_market_selective.py',
    'audit_open_market_selective.py', 'test_open_market_selective_audit.py',
    'open_market_selective_verdict.py', 'test_open_market_selective_verdict.py',
    'run_open_market_selective.py', 'test_open_market_selective_runner.py')]
read, sha, save = original.read, original.sha, original.save
START, END = data.millis('2021-07-02'), data.millis('2021-10-01')


def now():
    return dt.datetime.now(dt.timezone.utc)


def active():
    learning.need(read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'SELECTIVE_ACTIVE_GATE')


def initialize():
    active()
    previous.preserve()
    learning.need(read(previous.ROOT/'validation-state.json')['status'] == 'READY', 'PARENT_READY')
    files = {p: sha(p) for p in previous.SOURCES+[previous.PLAN, OLD_REPORT]}
    for p in previous.ROOT.rglob('*'):
        if p.is_file() and '__pycache__' not in str(p):
            files[str(p)] = sha(p)
    save(ROOT/'protected.json', dict(files=files))
    save(ROOT/'scope.json', dict(started_utc=now().isoformat(),
        deadline_utc=(now()+dt.timedelta(hours=2)).isoformat(), plan_sha256=sha(PLAN),
        maximum_batches=1, maximum_calibrations=1, maximum_paths=5,
        base_model_fits=0, searches=0, network=0))
    print('SELECTIVE_SCOPE_REGISTERED', preserve())


def preserve():
    count = previous.preserve()
    learning.need(sha(PLAN) == read(ROOT/'scope.json')['plan_sha256'], 'SELECTIVE_PLAN_CHANGED')
    files = read(ROOT/'protected.json')['files']
    learning.need(all(sha(p) == h for p, h in files.items()), 'SELECTIVE_PRIOR_CHANGED')
    return count+len(files)


def freeze():
    active()
    count = preserve()
    save(ROOT/'freeze.json', dict(files={p: sha(p) for p in SOURCES},
        protected_bindings=count, at=now().isoformat(), calibration_start=START, calibration_end=END))
    print('SELECTIVE_SOURCE_INPUT_FROZEN')


def execute():
    import open_market_selective as selective
    import audit_open_market_selective as independent
    import open_market_execution as execution
    import audit_open_market_execution as ledger
    import audit_open_market_learning as input_audit
    import open_market_selective_verdict as verdict
    active()
    preserve()
    frozen, scope = read(ROOT/'freeze.json'), read(ROOT/'scope.json')
    learning.need(all(sha(p) == h for p, h in frozen['files'].items()), 'SELECTIVE_SOURCE_CHANGED')
    learning.need(now() < dt.datetime.fromisoformat(scope['deadline_utc']), 'SELECTIVE_TIME_BUDGET')
    save(ROOT/'economic-attempt.json', dict(at=now().isoformat(), freeze_sha256=sha(ROOT/'freeze.json')))
    begun = time.monotonic()
    paths = {}

    def budget():
        learning.need(time.monotonic()-begun < 1800 and
            now() < dt.datetime.fromisoformat(scope['deadline_utc']), 'SELECTIVE_TIME_BUDGET')
        learning.need(len(paths) <= 5 and sum(p.stat().st_size for p in ROOT.rglob('*')
            if p.is_file()) < 256*1024*1024, 'SELECTIVE_RESOURCE_BUDGET')

    model = read(original.ROOT/'select-PV-0-model.json')
    learning.need(model['arm'] == 'PV' and model['ridge'] == .1 and
        model['training_rows'] == 166 and model['label_cutoff'] <= data.millis('2021-07-01'),
        'SELECTIVE_FIXED_BASE_MODEL')
    save(ROOT/'base-model-audit.json', input_audit.audit_fit(
        read(original.ROOT/'training-labels.json'), model))
    labels = [r for r in read(original.ROOT/'refit-labels.json') if START <= r['t'] < END]
    predictions = read(original.ROOT/'select-PV-0-predictions.json')
    fs = {int(t): f for t, f in read(original.ROOT/'2021-features.json').items()}
    calibration_fs = {t: fs[t] for t in range(START, END, data.DAY)}
    save(ROOT/'calibration-prediction-audit.json', input_audit.audit_predictions(model, calibration_fs, predictions))
    calibration = selective.fit(labels, predictions, START, END)
    save(ROOT/'calibration.json', calibration)
    save(ROOT/'calibration-audit.json', independent.audit_fit(labels, predictions, START, END, calibration))
    budget()
    if calibration['status'] == 'ELIGIBLE_SELECTIVE_POLICY':
        for year, start, end in (('2021', data.millis('2021-10-02'), data.millis('2021-12-30')),
                                 ('2025', data.millis('2025-01-16'), data.millis('2026-01-01'))):
            rows = data.load_rows(list(original.INPUTS)[0 if year == '2021' else 1])
            fs = {int(t): f for t, f in read(original.ROOT/(year+'-features.json')).items()
                  if start <= int(t) < end}
            predictions = [learning.predict(model, fs[t], t) for t in range(start, end, data.DAY)]
            save(ROOT/(year+'-base-predictions.json'), predictions)
            save(ROOT/(year+'-base-prediction-audit.json'), input_audit.audit_predictions(model, fs, predictions))
            chosen = selective.apply(calibration, predictions)
            save(ROOT/(year+'-selective-decisions.json'), chosen)
            save(ROOT/(year+'-selective-audit.json'), independent.audit_apply(calibration, predictions, chosen))
            specs = [('control', 1, {p['t']: p['direction'] for p in predictions}),
                     ('veto', 1, {p['t']: p['direction'] for p in chosen})]
            if year == '2025':
                specs.append(('delay', 2, specs[1][2]))
            for mode, latency, decisions in specs:
                budget()
                name = year+'-'+mode
                path = execution.simulate(rows, fs, decisions, start, end, latency=latency)
                save(ROOT/(name+'.json'), path)
                save(ROOT/(name+'-audit.json'), ledger.audit(path, rows, fs, decisions))
                learning.need(learning.complete(path), 'SELECTIVE_EXECUTION_INCOMPLETE')
                paths[name] = path
                print(json.dumps(dict(path=name, net=[b['net'] for b in path['books']],
                    stopped=[b['stopped'] for b in path['books']])), flush=True)
        decision = verdict.classify(paths)
        save(ROOT/'decision-audit.json', verdict.audit_verdict(paths, decision))
    else:
        decision = dict(status=calibration['status'], economic_status='NOT_EVALUATED',
            independent_confirmation=False, qualified_candidates=0, g4=False,
            online_learning_proven=False, demo_opening_lock_unchanged=True)
    learning.need(len(paths) == (5 if calibration['status'] == 'ELIGIBLE_SELECTIVE_POLICY' else 0),
                  'SELECTIVE_COMPLETE_BATCH')
    budget()
    learning.need(all(sha(p) == h for p, h in frozen['files'].items()), 'SELECTIVE_POST_SOURCE_CHANGED')
    compact = {name: [{k: b[k] for k in ('net', 'gross', 'fees', 'slippage', 'funding',
        'drawdown', 'close_drawdown', 'stopped', 'insufficient', 'failed_entries')} |
        dict(complete_cycles=sum(c['complete'] for c in b['cycles']),
            active_weeks=len(b['active_weeks']), fills=sum(e['kind'] == 'fill' for e in b['events']))
        for b in path['books']] for name, path in paths.items()}
    result = dict(status='SELECTIVE_BATCH_COMPLETED', calibration=calibration, decision=decision,
        paths=compact, paired_paths=len(paths), decimal_books=2*len(paths),
        calibration_fits=calibration['fit_count'], base_model_fits=0, searches=0, network=0,
        protected_bindings=preserve(),
        compute_seconds=time.monotonic()-begun, completed_utc=now().isoformat(),
        freeze_sha256=sha(ROOT/'freeze.json'))
    save(ROOT/'result.json', result)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    {'initialize': initialize, 'freeze': freeze, 'execute': execute}[sys.argv[1]]()
