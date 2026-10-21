"""Single immutable three-path development batch. Never trains or places orders."""
import argparse
import json
import os
from pathlib import Path
import resource
import sys
import time

import sparse_reversal_2024_inputs as inputs
from sparse_reversal_2024_inputs import OUT, START, END, guard, need, read, save, sha, now
from run_offline_policy_correction import samples, BINARY, SOURCES as POLICY_SOURCES
from frozen_history_transfer import FrozenPath, concentration
from sparse_reversal_2024_model import DelayedPath, decision
from audit_offline_policy_correction import read_domain
from audit_frozen_history_transfer import audit_trace, audit_concentration, audit_verdict, compare_bounds
from audit_sparse_reversal_2024 import audit_delay

SOURCES = sorted(set(POLICY_SOURCES+inputs.SOURCES+(
    'tools/frozen_history_transfer.py', 'tools/offline_diagnostic_safety.py',
    'tools/audit_frozen_history_transfer.py', 'tools/sparse_reversal_2024_model.py',
    'tools/audit_sparse_reversal_2024.py', 'tools/run_sparse_reversal_2024.py',
    'tools/test_sparse_reversal_2024_model.py')))


def check():
    guard()
    proof = read(OUT/'input-proof.json')
    need(proof['status'] == 'COMPLETE_DEVELOPMENT_INPUT' and proof['warmup_bars'] == 288 and
         proof['evaluation_bars'] == 105408 and proof['public_gets'] == 2, 'INPUT_PROOF')
    need(sha(OUT/'replay.csv') == proof['csv_sha256'] and
         sha(OUT/'input-freeze.json') == proof['input_freeze_sha256'], 'INPUT_BINDING')
    for p, h in proof['new_bindings'].items(): need(sha(p) == h, 'NEW_WIRE_CHANGED')
    old = read('.artifacts/execution-net-learning-20260930/execution-freeze.json')
    need(sha(BINARY) == old['binary_sha256'], 'FROZEN_BINARY')
    if (OUT/'execution-freeze.json').exists():
        f = read(OUT/'execution-freeze.json')
        need(f['sources'] == {p: sha(p) for p in SOURCES}, 'EXECUTION_SOURCE_CHANGED')
        need(f['input_proof_sha256'] == sha(OUT/'input-proof.json') and
             f['binary_sha256'] == sha(BINARY), 'FREEZE_IDENTITY')


def freeze():
    check()
    gate = read(inputs.RUN/'validation-state.json')
    need(any(e.get('label') == 'sparse-model-and-regression-tests' and e.get('exit_code') == 0
             for e in gate['history']), 'SYNTHETIC_REGRESSION_REQUIRED')
    save(OUT/'execution-freeze.json', dict(at=now(), sources={p: sha(p) for p in SOURCES},
         binary_sha256=sha(BINARY), input_proof_sha256=sha(OUT/'input-proof.json'),
         gate_before_freeze_sha256=sha(inputs.RUN/'validation-state.json'),
         maximum_attempts=1, paths=['normal','control','delay'], compute_seconds_limit=1800,
         memory_bytes_limit=2147483648, output_bytes_limit=1073741824,
         training_updates=0, qualification=False))
    print('THREE_PATH_EXECUTION_AND_INDEPENDENT_AUDIT_FROZEN', flush=True)


def execute():
    check()
    f = read(OUT/'execution-freeze.json')
    need(f['paths'] == ['normal','control','delay'] and f['maximum_attempts'] == 1, 'BATCH_CONTRACT')
    save(OUT/'economic-attempt.json', dict(at=now(), freeze_sha256=sha(OUT/'execution-freeze.json'), attempt=1))
    begun = time.monotonic(); deadline = begun+1800

    def budget():
        need(time.monotonic() < deadline, 'COMPUTE_BUDGET')
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform == 'darwin' else 1024)
        need(peak < 2147483648, 'MEMORY_BUDGET')
        need(sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file()) < 1073741824, 'OUTPUT_BUDGET')

    domain = samples(OUT/'replay.csv', START, END, BINARY, deadline)
    independent = read_domain(OUT/'replay.csv', START, END)
    need(domain == independent and len(domain) == 105408, 'INDEPENDENT_CAUSAL_FEATURE_AND_BUCKET')
    budget(); results = {}; audits = {}; groups = {}
    for name, vector in [('normal',(0,-4)), ('control',(1,0)), ('delay',(0,-4))]:
        check(); budget()
        save(OUT/(name+'-attempt.json'), dict(at=now(), path=name, vector=vector))
        engine = DelayedPath() if name == 'delay' else FrozenPath(vector)
        trace = OUT/(name+'.jsonl')
        with trace.open('x') as out:
            for i, sample in enumerate(domain):
                if i % 1000 == 0: budget()
                event = engine.consume(sample)
                if event is not None: out.write(json.dumps(event, allow_nan=False)+'\n')
            result = engine.finish()
            out.write(json.dumps(dict(kind='TERMINAL', result=result), allow_nan=False)+'\n')
        summary = dict(result, trace_file=trace.name, trace_sha256=sha(trace))
        save(OUT/(name+'.json'), summary); results[name] = summary
        budget()
        audits[name] = (audit_delay if name == 'delay' else audit_trace)(trace, independent, summary)
        save(OUT/(name+'-audit.json'), audits[name])
        with trace.open() as source: groups[name] = concentration((json.loads(s) for s in source), summary)
        audit_concentration(trace, summary, groups[name])
        save(OUT/(name+'-groups.json'), groups[name])
        need(len(summary['weekly']) == 52 and summary['domain_bars'] == 105408, 'FULL_CALENDAR')
        print(json.dumps(dict(path=name, reason=summary['reason'], active_bars=summary['active_bars'],
            net=[w['cash']-10000 for w in summary['wallets']], episodes=groups[name]['episode_count'],
            separated_clusters=groups[name]['separated_clusters'], audit='VERIFIED')), flush=True)
        budget()
    outcome = decision(results['normal'], results['control'], results['delay'], groups['normal'])
    expected, bounds = audit_verdict(results['normal'], results['control'], groups['normal'])
    need(expected == outcome['normal_verdict'], 'NORMAL_VERDICT_AUDIT')
    compare_bounds(outcome['bounds'], bounds)
    delay = results['delay']
    expected_delay = ('REJECT_DELAY_RISK' if delay['reason'] != 'COMPLETE' else
                      'REJECT_DELAY_NET' if any(w['cash'] <= 10000 for w in delay['wallets']) else
                      'DELAY_REFERENCE_SUPPORTED')
    expected_final = (expected if expected != 'HISTORICAL_TRANSFER_SUPPORTED_NOT_DEPLOYABLE' else
                     'DEVELOPMENT_SUPPORTED_CONFIRMATION_NOT_ADMITTED' if expected_delay == 'DELAY_REFERENCE_SUPPORTED'
                     else expected_delay)
    need(outcome['delay_verdict'] == expected_delay and outcome['status'] == expected_final and
         not outcome['qualification'] and not outcome['g4'], 'FINAL_VERDICT_AUDIT')
    check(); budget()
    from run_open_market_selective import preserve
    need(preserve() == 8070, 'PRESERVATION')
    compact = {k: dict(reason=v['reason'], active_bars=v['active_bars'],
        stop_at_ms=v['stop_at_ms'], exit_at_ms=v['exit_at_ms'],
        first_legacy_withdrawal_ms=v['first_legacy_withdrawal_ms'],
        legacy_path_status=v['legacy_path_status'], exposure_short_flat_long=v['exposure_short_flat_long'],
        drawdown=[w['max_drawdown'] for w in v['wallets']], economics=audits[k]['economics'],
        full_weeks=len(v['weekly']), nonzero_full_blocks=groups[k]['nonzero_full_blocks'],
        episodes=groups[k]['episode_count'], separated_clusters=groups[k]['separated_clusters'],
        leave_largest_cluster_net=groups[k]['leave_largest_cluster_net']) for k,v in results.items()}
    result = dict(status='COMPLETE_AUDITED_DEVELOPMENT_BATCH', decision=outcome, paths=compact,
        economic_attempts=1, paired_paths=3, decimal_books=6, public_gets=2, training_updates=0,
        protected_bindings=8070, independent_confirmation_runs=0, demo_opening_lock_unchanged=True,
        compute_seconds=time.monotonic()-begun, completed_utc=now(),
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform == 'darwin' else 1024),
        output_bytes=sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file()),
        evidence={str(p): sha(p) for p in OUT.iterdir() if p.is_file()})
    save(OUT/'result.json', result)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('freeze','execute'))
    {'freeze': freeze, 'execute': execute}[parser.parse_args().action]()
