"""One fixed rolling-versus-frozen development comparison, no network/order I/O."""
import datetime as dt
from pathlib import Path
import sys
import time

import btc_old_output_model as data
import open_market_learning as learning
import audit_open_market_learning as input_audit
from run_open_market_learning import read, sha, save
import run_open_market_learning as previous
from open_market_rolling_decision import classify

ROOT = Path('.artifacts/open-market-rolling-20261019')
OLD = previous.ROOT
PLAN = 'docs/plans/2026-10-19-open-market-rolling-learning.md'
OLD_REPORT = 'docs/reviews/2026-10-19-open-market-conditional-learning-result.md'
START, END = data.millis('2025-07-16'), data.millis('2026-01-01')
UPDATES = [data.millis('2025-%02d-16' % month) for month in range(7, 13)]
SOURCES = ['tools/'+name for name in ('open_market_rolling_execution.py',
    'test_open_market_rolling_execution.py', 'open_market_rolling_decision.py',
    'audit_open_market_rolling.py', 'test_open_market_rolling.py', 'run_open_market_rolling.py')]


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def active():
    learning.need(read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'ACTIVE_GATE')


def initialize():
    active()
    previous.preserve()
    learning.need(read(OLD/'result.json')['decision']['status'] == 'NET_INCREMENT_UNSUPPORTED', 'PRIOR_RESULT')
    learning.need(read(OLD/'validation-state.json')['status'] == 'READY', 'PRIOR_ENGINEERING_COMPLETE')
    files = dict(read(OLD/'freeze.json')['sources'])
    files[previous.PLAN] = sha(previous.PLAN)
    files[OLD_REPORT] = sha(OLD_REPORT)
    for p in OLD.rglob('*'):
        if p.is_file() and '__pycache__' not in str(p):
            files[str(p)] = sha(p)
    save(ROOT/'protected.json', dict(files=files, inherited_protection_sha256=sha(OLD/'protected.json')))
    save(ROOT/'scope.json', dict(started_utc=stamp(),
        deadline_utc=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=2)).isoformat(),
        plan_sha256=sha(PLAN), maximum_fits=6, maximum_paths=4, maximum_batches=1,
        market_requests=0, independent=False))
    print('FIXED_ROLLING_RESEARCH_REGISTERED', preserve())


def preserve():
    count = previous.preserve()
    p = read(ROOT/'protected.json')
    learning.need(sha(OLD/'protected.json') == p['inherited_protection_sha256'], 'PROTECTION_CHAIN')
    learning.need(sha(PLAN) == read(ROOT/'scope.json')['plan_sha256'], 'PLAN_CHANGED')
    for path, h in p['files'].items():
        learning.need(sha(path) == h, 'OLD_CHANGED:'+path)
    return count+len(p['files'])


def freeze():
    active()
    count = preserve()
    files = dict(read(OLD/'freeze.json')['sources'])
    files.update({p: sha(p) for p in SOURCES})
    files.update({str(OLD/p): sha(OLD/p) for p in ('evaluation-models.json', '2025-features.json')})
    files.update(previous.INPUTS)
    learning.need(all(sha(p) == h for p, h in files.items()), 'FROZEN_INPUT_IDENTITIES')
    save(ROOT/'freeze.json', dict(at=stamp(), files=files, plan_sha256=sha(PLAN),
        protected_bindings=count, updates=UPDATES, start=START, end=END,
        maximum_fits=6, maximum_paths=4, search_vectors=0))
    print('ROLLING_SOURCE_INPUT_POLICY_FROZEN')


def execute():
    import open_market_rolling_execution as execution
    import audit_open_market_rolling as independent
    active()
    preserve()
    frozen, scope = read(ROOT/'freeze.json'), read(ROOT/'scope.json')
    learning.need(all(sha(p) == h for p, h in frozen['files'].items()), 'SOURCE_IDENTITY')
    learning.need(dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(scope['deadline_utc']), 'STAGE_BUDGET')
    save(ROOT/'economic-attempt.json', dict(at=stamp(), freeze_sha256=sha(ROOT/'freeze.json')))
    begun = time.monotonic()

    def budget():
        learning.need(time.monotonic()-begun < 1800, 'COMPUTE_BUDGET')
        learning.need(dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(scope['deadline_utc']), 'STAGE_BUDGET')
        learning.need(sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file()) < 256*1024*1024, 'EVIDENCE_BUDGET')

    rows = data.load_rows(list(previous.INPUTS)[1])
    fs = {int(t): f for t, f in read(OLD/'2025-features.json').items()}
    schedule = []
    for i, t in enumerate(UPDATES):
        budget()
        cutoff = t-data.DAY
        start = cutoff-180*data.DAY
        labels = learning.samples(rows, fs, start, cutoff)
        learning.need(len(labels) == 180 and max(r['label_end'] for r in labels) <= cutoff, 'MATURE_180_DAYS')
        window = {k: fs[k] for k in range(start, cutoff, data.DAY)}
        save(ROOT/('fit-%d-labels.json' % i), labels)
        save(ROOT/('fit-%d-input-audit.json' % i),
             input_audit.audit_features_and_labels(rows, window, labels, start, cutoff))
        model = learning.fit(labels, 'PV', .1)
        save(ROOT/('fit-%d-model-audit.json' % i), input_audit.audit_fit(labels, model))
        entry = dict(t=t, model_id='recent-%d' % i, model=model)
        save(ROOT/('fit-%d-model.json' % i), entry)
        schedule.append(entry)
    save(ROOT/'model-schedule.json', dict(schedule=schedule, applied_yet=False, at=stamp()))
    old = read(OLD/'evaluation-models.json')['models']['PV']
    old_schedule = [dict(t=START, model_id='old-2021', model=old)]
    fs = {t: f for t, f in fs.items() if START <= t < END}
    results = {}
    specs = [('old2021', old_schedule, False, 1), ('recent2025', schedule, False, 1),
             ('rolling', schedule, True, 1), ('delay', schedule, True, 2)]
    for name, models, rolling, latency in specs:
        budget()
        result = execution.simulate(rows, fs, models, START, END, latency=latency, rolling=rolling)
        learning.need(result['rolling'] == rolling, 'GOVERNANCE_SCOPE')
        save(ROOT/(name+'.json'), result)
        receipt = independent.audit_governance(result, rows, fs, models)
        save(ROOT/(name+'-audit.json'), receipt)
        learning.need(learning.complete(result['path']), 'EXECUTION_INCOMPLETE')
        results[name] = result
        b = result['path']['books']
        print(dict(path=name, net=[x['net'] for x in b], cycles=[len(x['cycles']) for x in b],
            applied=result['governance']['applied_updates'],
            performance_freeze_time=result['governance']['performance_freeze_time']), flush=True)
    decision = classify(results)
    save(ROOT/'decision-audit.json', independent.audit_verdict(results, decision))
    learning.need(len(schedule) == 6 and len(results) == 4, 'COMPLETE_BATCH')
    learning.need(all(sha(p) == h for p, h in frozen['files'].items()), 'POST_SOURCE_IDENTITY')
    budget()
    summary = {}
    for name, r in results.items():
        summary[name] = dict(governance=dict(r['governance']), books=[
            {k: b[k] for k in ('net', 'gross', 'fees', 'slippage', 'funding', 'drawdown',
                               'close_drawdown', 'stopped', 'insufficient', 'failed_entries')} |
            dict(complete_cycles=sum(c['complete'] for c in b['cycles']), active_weeks=len(b['active_weeks']),
                 fills=sum(e['kind'] == 'fill' for e in b['events'])) for b in r['path']['books']])
        del summary[name]['governance']['active_model_by_date']
    result = dict(status='BATCH_COMPLETED', decision=decision, paths=summary, unique_fits=6,
        searches=0, paths_count=4, decimal_books=8, calendar_days=169,
        protected_bindings=preserve(), compute_seconds=time.monotonic()-begun, completed_utc=stamp(),
        freeze_sha256=sha(ROOT/'freeze.json'), market_requests=0, account_operations=0, deployments=0)
    save(ROOT/'result.json', result)
    print(result, flush=True)


if __name__ == '__main__':
    {'initialize': initialize, 'freeze': freeze, 'execute': execute}[sys.argv[1]]()
