"""One frozen open-market development batch; no network/account operations."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import btc_old_output_model as data
import open_market_learning as model
import open_market_execution as execution
import audit_open_market_execution as ledger

ROOT = Path('.artifacts/open-market-learning-20261019')
OLD = Path('.artifacts/adjusted-transfer-official-control-20261018')
BASE = '15acc28814e3d7a52a133fa4a945f4a6420b26d4'
PLAN = 'docs/plans/2026-10-19-open-market-conditional-learning.md'
FRONTS = {'AGENTS.md', 'README.md', 'docs/CURRENT_STATE.md'}
INPUTS = {
    '.artifacts/frozen-history-transfer-20261001/replay.csv':
        'b192ef1bdcada0cce7f55bf5c2beda08bef6f82e7f8e6645bc00df9be41b50d3',
    '.artifacts/mvp-reference-history-20260921/prepared/replay.csv':
        '4de7105e0e79ea7679e0b72e45bc54a9908c1037e05d5a1592cd482abfa4cc47'}
SOURCES = ['tools/'+s for s in ('open_market_learning.py', 'open_market_execution.py',
    'audit_open_market_learning.py', 'audit_open_market_execution.py',
    'audit_open_market_decision.py',
    'test_open_market_learning.py', 'test_open_market_execution.py', 'test_open_market_pipeline.py',
    'run_open_market_learning.py', 'btc_old_output_model.py', 'information_attribution.py')]


def read(p):
    return json.loads(Path(p).read_text())


def sha(p):
    with Path(p).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(p, obj):
    with Path(p).open('x') as f:
        json.dump(obj, f, indent=2, sort_keys=True, allow_nan=False)
        f.write('\n')


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def active():
    model.need(read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'ACTIVE_GATE_REQUIRED')


def initialize():
    active()
    p = read(OLD/'protected.json')
    for path, h in read(OLD/'diagnostic-delivery.json')['files'].items():
        if path in FRONTS:
            p['git_bindings'].append(dict(ref=BASE+':'+path, sha256=h))
        else:
            p['files'][path] = h
    for path in OLD.rglob('*'):
        if path.is_file() and '__pycache__' not in str(path):
            p['files'][str(path)] = sha(path)
    save(ROOT/'protected.json', p)
    save(ROOT/'scope.json', dict(started_utc=stamp(), plan_sha256=sha(PLAN),
        deadline_utc=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=4)).isoformat(),
        maximum_economic_batches=1, maximum_compute_seconds=1800, maximum_paths=16,
        maximum_evidence_bytes=512*1024*1024, new_market_requests=0,
        official_documentation_reads=3, independent_confirmation=False))
    print('NEW_OPEN_MARKET_RESEARCH_REGISTERED', preserve())


def preserve():
    scope = read(ROOT/'scope.json')
    model.need(sha(PLAN) == scope['plan_sha256'], 'PLAN_CHANGED')
    p = read(ROOT/'protected.json')
    for path, h in p['files'].items():
        model.need(sha(path) == h, 'OLD_CHANGED:'+path)
    for b in p['git_bindings']:
        raw = subprocess.check_output(['git', 'show', b['ref']], timeout=20)
        model.need(hashlib.sha256(raw).hexdigest() == b['sha256'], 'GIT_BINDING_CHANGED')
    return len(p['files'])


def freeze():
    active()
    count = preserve()
    for p, h in INPUTS.items():
        model.need(sha(p) == h, 'INPUT_CHANGED')
    save(ROOT/'freeze.json', dict(at=stamp(), sources={p: sha(p) for p in SOURCES},
        inputs=INPUTS, plan_sha256=sha(PLAN), protected_files=count,
        design_review_sha256=sha(ROOT/'design-review.json'),
        arms=list(model.ARMS), ridges=list(model.RIDGES), market_batches=1))
    print('SOURCE_INPUT_RULES_FROZEN_BEFORE_ECONOMIC_BATCH')


def execute():
    import audit_open_market_learning as audit
    import audit_open_market_decision as decision_audit
    active()
    count = preserve()
    frozen = read(ROOT/'freeze.json')
    model.need(sha(ROOT/'design-review.json') == frozen['design_review_sha256'], 'DESIGN_REVIEW_CHANGED')
    for p, h in dict(frozen['sources'], **frozen['inputs']).items():
        model.need(sha(p) == h, 'FROZEN_IDENTITY:'+p)
    scope = read(ROOT/'scope.json')
    model.need(dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(scope['deadline_utc']), 'STAGE_BUDGET')
    save(ROOT/'economic-attempt.json', dict(at=stamp(), freeze_sha256=sha(ROOT/'freeze.json')))
    started = time.monotonic()
    paths = {}
    receipts = {}

    def budget():
        model.need(time.monotonic()-started < 1800, 'COMPUTE_BUDGET')
        model.need(dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(scope['deadline_utc']),
                   'STAGE_BUDGET')
        model.need(len(receipts) <= scope['maximum_paths'], 'PATH_BUDGET')
        model.need(sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file()) < 512*1024*1024, 'EVIDENCE_BUDGET')

    def one(name, rows, fs, learned, start, end, latency=1):
        budget()
        preds = [model.predict(learned, fs[t], t) for t in range(start, end, data.DAY)]
        audit.audit_predictions(learned, {t: fs[t] for t in range(start, end, data.DAY)}, preds)
        decisions = {p['t']: p['direction'] for p in preds}
        save(ROOT/(name+'-predictions.json'), preds)
        result = execution.simulate(rows, fs, decisions, start, end, latency=latency)
        # Policy metadata is stored outside the exact execution schema.
        save(ROOT/(name+'.json'), result)
        receipt = ledger.audit(result, rows, fs, decisions)
        save(ROOT/(name+'-audit.json'), receipt)
        receipts[name] = receipt
        model.need(model.complete(result), 'EXECUTION_EVIDENCE_INCOMPLETE')
        print(json.dumps(dict(path=name, net=[b['net'] for b in result['books']],
            stopped=[b['stopped'] for b in result['books']], cycles=[len(b['cycles']) for b in result['books']])), flush=True)
        budget()
        return result

    ms = data.millis
    rows = data.load_rows(next(iter(INPUTS)))
    fs = model.features(rows, ms('2021-01-16'), ms('2021-12-30'))
    train = model.samples(rows, fs, ms('2021-01-16'), ms('2021-07-01'))
    refit = model.samples(rows, fs, ms('2021-01-16'), ms('2021-10-01'))
    save(ROOT/'2021-features.json', fs)
    save(ROOT/'training-labels.json', train)
    save(ROOT/'refit-labels.json', refit)
    save(ROOT/'2021-input-audit.json', audit.audit_features_and_labels(
        rows, fs, None, ms('2021-01-16'), ms('2021-12-30')))
    for name, labels, cutoff in (('train', train, '2021-07-01'), ('refit', refit, '2021-10-01')):
        save(ROOT/(name+'-label-audit.json'), audit.audit_features_and_labels(
            rows, {t: f for t, f in fs.items() if t < ms(cutoff)}, labels,
            ms('2021-01-16'), ms(cutoff)))
    selection, selected = {}, {}
    for arm in model.ARMS:
        comparisons = []
        for i, ridge in enumerate(model.RIDGES):
            learned = model.fit(train, arm, ridge)
            audit.audit_fit(train, learned)
            name = 'select-'+arm+'-'+str(i)
            save(ROOT/(name+'-model.json'), learned)
            result = one(name, rows, fs, learned, ms('2021-07-02'), ms('2021-10-01'))
            comparisons.append(dict(result, ridge=ridge))
        selection[arm] = model.select(comparisons)
        save(ROOT/('selection-'+arm+'-audit.json'),
             decision_audit.audit_selection(comparisons, selection[arm]))
        selected[arm] = model.fit(refit, arm, selection[arm]['ridge'])
        audit.audit_fit(refit, selected[arm])
    save(ROOT/'selection.json', selection)
    save(ROOT/'evaluation-models.json', dict(at=stamp(), models=selected,
        selection_sha256=sha(ROOT/'selection.json'), independent=False))
    for arm in model.ARMS:
        name = '2021-'+arm
        paths[name] = one(name, rows, fs, selected[arm], ms('2021-10-02'), ms('2021-12-30'))
    del rows, fs, train, refit
    rows = data.load_rows(list(INPUTS)[1])
    fs = model.features(rows, ms('2025-01-16'), ms('2026-01-01'))
    save(ROOT/'2025-features.json', fs)
    save(ROOT/'2025-input-audit.json', audit.audit_features_and_labels(
        rows, fs, None, ms('2025-01-16'), ms('2026-01-01')))
    for arm in model.ARMS:
        name = '2025-'+arm
        paths[name] = one(name, rows, fs, selected[arm], ms('2025-01-16'), ms('2026-01-01'))
    paths['2025-PV-delay'] = one('2025-PV-delay', rows, fs, selected['PV'],
        ms('2025-01-16'), ms('2026-01-01'), latency=2)
    decision = model.verdict(paths, selection)
    save(ROOT/'decision-audit.json', decision_audit.audit_verdict(paths, selection, decision))
    model.need(len(receipts) == 16 and len(paths) == 7, 'COMPLETE_BATCH')
    for p, h in dict(frozen['sources'], **frozen['inputs']).items():
        model.need(sha(p) == h, 'POST_EXECUTION_FROZEN_IDENTITY:'+p)
    budget()
    compact = {name: [{k: b[k] for k in ('net', 'gross', 'fees', 'slippage', 'funding',
        'turnover', 'drawdown', 'close_drawdown', 'stopped', 'insufficient', 'holding_bars',
        'long_bars', 'short_bars', 'failed_entries')} | dict(
            complete_cycles=sum(c['complete'] for c in b['cycles']),
            active_weeks=len(b['active_weeks']),
            fills=sum(e['kind'] == 'fill' for e in b['events'])) for b in r['books']]
        for name, r in paths.items()}
    result = dict(status='BATCH_COMPLETED', decision=decision, selection=selection,
        paths=compact, audited_paths=len(receipts), decimal_books=2*len(receipts),
        fitting_vectors=9, frozen_refits=3, evaluation_paths=7, protected_files=preserve(),
        compute_seconds=time.monotonic()-started, completed_utc=stamp(),
        new_market_requests=0, account_operations=0, deployments=0,
        freeze_sha256=sha(ROOT/'freeze.json'))
    save(ROOT/'result.json', result)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    {'initialize': initialize, 'freeze': freeze, 'execute': execute}[sys.argv[1]]()
