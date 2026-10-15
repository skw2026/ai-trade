"""One accepted, frozen 2025 comparison; no retraining or network access."""
import datetime as dt
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import time

import run_btc_old_output as original
import btc_old_output_model as model
from audit_btc_old_output import audit

ROOT = original.ROOT / 'control-continuation'
PLAN = Path('docs/plans/2026-10-15-btc-old-output-control-continuation.md')
SOURCES = [Path('tools/run_btc_old_output_comparison.py'),
           Path('tools/test_btc_old_output_comparison.py')]
NAMES = ('candidate', 'fixed', 'price', 'passive', 'chain-delay', 'execution-delay')


def select_benchmark(results):
    """Rank accounting-complete controls, without giving them candidate eligibility."""
    best = None
    for i, result in enumerate(results):
        model.need(result['parameters'][0] == 0, 'BENCHMARK_ONLY')
        model.need(all(not b['insufficient'] and b['quantity'] == 0 for b in result['books']),
                   'BENCHMARK_INCOMPLETE')
        score = min(b['net'] for b in result['books'])
        key = (sum(b['turnover'] for b in result['books']), tuple(result['parameters']))
        if best is None or score > best[1] + 1e-6 or (abs(score-best[1]) <= 1e-6 and key < best[2]):
            best = (i, score, key)
    model.need(best is not None, 'BENCHMARK_MISSING')
    return best[0]


def specifications(training):
    mechanisms, prices = model.grids()
    model.need([r['parameters'] for r in training] == [list(p) for p in mechanisms+prices], 'TRAINING_IDENTITY')
    model.need(model.select(training[:12]) == 9 and model.eligible(training[9]), 'CANDIDATE_NOT_ELIGIBLE')
    model.need(model.eligible(training[2]), 'FIXED_IDENTITY_CHANGED')
    model.need(select_benchmark(training[12:]) == 0, 'FROZEN_BENCHMARK_CHANGED')
    return [('candidate', [1, 0, 2], 3, {}), ('fixed', [-1, 0, 1], 3, {}),
            ('price', [0, -1, 1], 3, {}), ('passive', [0, 1, 1], 3, {'passive': True}),
            ('chain-delay', [1, 0, 2], 4, {}), ('execution-delay', [1, 0, 2], 3, {'latency': 2})]


def preserve():
    original.guard()
    original.preserve()
    frozen = original.read(ROOT / 'freeze.json')
    model.need(all(original.sha(p) == h for p, h in frozen['files'].items()), 'CONTINUATION_IDENTITY')
    history = original.read(original.ROOT / 'validation-state.json')['history']
    model.need(history[:len(frozen['gate_history'])] == frozen['gate_history'], 'GATE_HISTORY_CHANGED')
    return len(frozen['files'])


def freeze():
    original.guard()
    original.preserve()
    prior = original.read(original.ROOT / 'model-freeze.json')
    files = dict(prior['sources'], **prior['inputs'])
    model.need(all(original.sha(p) == h for p, h in files.items()), 'ORIGINAL_FROZEN_IDENTITY')
    model.need(not list(original.ROOT.glob('eval-*.json')), 'PREVIOUS_EVALUATION_EXISTS')
    for p in original.ROOT.rglob('*'):
        if p.is_file() and ROOT not in p.parents and p.name not in ('validation-state.json', 'validation-state.json.lock'):
            files[str(p)] = original.sha(p)
    for p in SOURCES + [PLAN, Path('docs/reviews/2026-10-15-btc-old-output-training-result.md')]:
        files[str(p)] = original.sha(p)
    training = [original.read(original.ROOT / ('train-%02d.json' % i)) for i in range(16)]
    specs = specifications(training)
    old_result = original.read(original.ROOT / 'market-result.json')
    model.need(old_result['decision']['status'] == 'NO_GO_RISK' and old_result['evaluation_paths'] == 0, 'ORIGINAL_RESULT')
    ROOT.mkdir(exist_ok=True)
    original.save(ROOT / 'freeze.json', dict(files=files, specifications=specs,
        gate_history=original.read(original.ROOT / 'validation-state.json')['history'],
        started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        prior_compute_seconds=old_result['compute_seconds'],
        candidate_index=9, fixed_index=2, benchmark_index=12, benchmark_promotion_authority=False))
    print('COMPARISON_FROZEN_NO_RETRAINING')


def audit_decision(paths, decision):
    """Decimal economic thresholds independent of the model's verdict function."""
    def net(path, cost):
        return D(str(path['books'][cost]['net']))
    incomplete = any(b['insufficient'] or b['quantity'] for r in paths for b in r['books'])
    risk = any(b['stopped'] for i in (0, 4, 5) for b in paths[i]['books'])
    a, fixed, price = paths[:3]
    failures = []
    if net(a, 0) <= 0 or net(a, 1) < 200:
        failures.append('ABSOLUTE_NET')
    if any(net(a, i)-net(price, i) < 50 for i in (0, 1)):
        failures.append('PRICE_INCREMENT')
    if a['parameters'] != [-1, 0, 1] and any(net(a, i)-net(fixed, i) < 50 for i in (0, 1)):
        failures.append('LEARNING_INCREMENT')
    stress = a['books'][1]
    mid = next((D(str(d['equity']))-10000 for d in stress['days'] if d['t'] == 1751328000000), None)
    if mid is None or mid <= 0 or net(a, 1)-mid <= 0:
        failures.append('HALF_YEAR_STABILITY')
    if any(net(paths[j], i) <= 0 for j in (4, 5) for i in (0, 1)):
        failures.append('DELAY_NET')
    def concentrated(values):
        positive = [D(str(v)) for v in values if D(str(v)) > 0]
        return not positive or max(positive) > D('.35')*sum(positive)
    diversity = (sum(c['complete'] for c in stress['cycles']) < 12 or len(stress['active_weeks']) < 8 or
        concentrated([c['net'] for c in stress['cycles']]) or concentrated(stress['weeks'].values()) or
        net(a, 1)-max(map(lambda v: D(str(v)), stress['weeks'].values()), default=D(0)) <= 0)
    expected = ('INSUFFICIENT' if incomplete else 'NO_GO_RISK' if risk else 'NO_GO_ECONOMIC' if failures
                else 'INSUFFICIENT_DIVERSITY' if diversity else 'DEVELOPMENT_SUPPORTED')
    model.need(decision['status'] == expected, 'INDEPENDENT_DECISION_MISMATCH')
    if expected == 'NO_GO_ECONOMIC':
        model.need(decision['reasons'] == failures, 'INDEPENDENT_ECONOMIC_REASONS')
    return dict(status='INDEPENDENT_DECISION_VERIFIED', decision=expected,
                economic_failures=failures, diversity_failed=diversity, hard_risk=bool(risk))


def execute():
    preserve()
    frozen = original.read(ROOT / 'freeze.json')
    original.save(ROOT / 'attempt.json', dict(freeze_sha256=original.sha(ROOT / 'freeze.json'),
        started_utc=dt.datetime.now(dt.timezone.utc).isoformat(), new_training_paths=0))
    started = time.monotonic()
    def budget():
        original.guard()
        model.need(time.monotonic()-started+frozen['prior_compute_seconds'] < 1800, 'TOTAL_COMPUTE_BUDGET')
        model.need(sum(p.stat().st_size for p in original.ROOT.rglob('*') if p.is_file()) < 256*1024*1024, 'TOTAL_EVIDENCE_BUDGET')
    p = original.read(original.PLAN)
    rows = model.load_rows(list(p['price_inputs'])[1])
    ratios = original.read(original.ROOT / 'ratios.json')
    start, end = map(model.millis, p['evaluation_period'])
    features = {lag: model.features(rows, ratios, start, end, lag_days=lag) for lag in (3, 4)}
    paths = []
    for name, params, lag, kw in frozen['specifications']:
        budget()
        result = model.simulate(rows, features[lag], params, start, end, **kw)
        original.save(ROOT / ('eval-'+name+'.json'), result)
        checked = audit(result, rows)
        original.save(ROOT / ('eval-'+name+'-audit.json'), checked)
        paths.append(result)
        print(json.dumps(dict(path=name, net=[b['net'] for b in result['books']],
            stopped=[b['stopped'] for b in result['books']],
            insufficient=[b['insufficient'] for b in result['books']])), flush=True)
        model.need(not any(b['insufficient'] or b['quantity'] for b in result['books']), 'EXECUTION_INSUFFICIENT_STOP_DEPENDENCIES')
        budget()
    decision = model.verdict(paths)
    checked = audit_decision(paths, decision)
    original.save(ROOT / 'decision-audit.json', checked)
    budget()
    result = dict(status='FROZEN_COMPARISON_COMPLETED', decision=decision, evaluation_paths=6,
        new_training_paths=0, independent_books=12, new_gets=0, account_operations=0, deployments=0,
        continuation_attempts=1, independent_confirmation=False, qualified_candidates=0, g4=False,
        compute_seconds=time.monotonic()-started, prior_compute_seconds=frozen['prior_compute_seconds'],
        original_status='NO_GO_RISK', original_training_paths=16, protected_continuation_files=preserve())
    original.save(ROOT / 'result.json', result)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    {'freeze': freeze, 'execute': execute}[sys.argv[1]]()
