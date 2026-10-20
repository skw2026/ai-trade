"""One frozen, bounded development batch. No network or account access."""
import argparse
from array import array
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

import cross_asset_inputs as inp
from cross_asset_inputs import need, read, save, sha, ROOT
import cross_asset_relative_strength as model
import cross_asset_verdict as verdict
import audit_cross_asset as independent

SOURCES = tuple('tools/'+v for v in (
    'cross_asset_inputs.py', 'test_cross_asset_inputs.py', 'compile_cross_asset_inputs.py',
    'test_cross_asset_compile.py', 'cross_asset_relative_strength.py', 'test_cross_asset_model.py',
    'audit_cross_asset.py', 'cross_asset_verdict.py', 'test_cross_asset_verdict.py',
    'run_cross_asset.py', 'test_cross_asset_runner.py', 'eth_sol_complete_model.py'))
TEST_ARGV = ['python3', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_cross_asset*.py', '-v']


def active():
    need(read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'ACTIVE_BATCH_GATE')
    for p, h in inp.EXPECTED.items():
        need(sha(p) == h, 'ORIGINAL_SCOPE_CHANGED')


def preservation():
    from run_open_market_selective import preserve
    inp.preserve()
    return preserve()


def resources():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != 'darwin':
        rss *= 1024
    size = sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())
    need(rss < 2*1024**3 and size < 1024**3, 'RESOURCE_LIMIT')
    return dict(peak_rss_bytes=rss, private_bytes=size, compute_processes=1)


class Archive:
    def __init__(self):
        audit = read(ROOT/'input-audit.json')
        need(audit['status'] == 'THREE_ASSET_INPUT_COMPLETE_DEVELOPMENT_ONLY' and
             audit['byteorder'] == sys.byteorder, 'INPUT_NOT_ADMITTED')
        need(all(sha(p) == h for p, h in audit['files'].items()), 'INPUT_HASH')
        self.start, self.end = audit['start_ms'], audit['end_ms']
        self.arrays = {}
        for s in model.SYMBOLS:
            for kind in ('trade', 'mark'):
                path = next(Path(p) for p in audit['files'] if p.endswith(f'/{s}-{kind}.f64'))
                a = array('d')
                with path.open('rb') as f:
                    a.fromfile(f, path.stat().st_size//8)
                self.arrays[s, kind] = a
        self.daily = read(ROOT/'compiled/daily.json')
        self.funding = {s: {int(t): v for t, v in r.items()} for s, r in read(ROOT/'compiled/funding.json').items()}

    def bars(self, t, kind):
        need(self.start <= t < self.end and (t-self.start) % 60000 == 0, 'INPUT_CLOCK')
        width = 5 if kind == 'trade' else 4
        idx = (t-self.start)//60000*width
        return [self.arrays[s, kind][idx:idx+width] for s in model.SYMBOLS]

    def rates(self, t):
        return {i: self.funding[s][t] for i, s in enumerate(model.SYMBOLS) if t in self.funding[s]}


def freeze():
    os.umask(0o077)
    active(); count = preservation()
    need(not (ROOT/'economic-attempt.json').exists(), 'ECONOMIC_BATCH_ALREADY_USED')
    from validation_gate import command_hash
    expected = command_hash(TEST_ARGV, str(Path.cwd().resolve()))
    tests = [v for v in read(ROOT/'validation-state.json')['history'] if
             v.get('event') == 'validation' and v.get('exit_code') == 0 and
             v.get('command_sha256') == expected]
    need(tests, 'SYNTHETIC_REGRESSION_REQUIRED')
    a = read(ROOT/'input-audit.json')
    need(a['status'] == 'THREE_ASSET_INPUT_COMPLETE_DEVELOPMENT_ONLY', 'INPUT_REQUIRED')
    bound = read(ROOT/'raw-input-bindings.json')['files']
    need(all(sha(p) == h for p, h in bound.items()), 'RAW_INPUT_CHANGED')
    need(all(sha(p) == h for p, h in a['files'].items()), 'INPUT_CHANGED')
    now = datetime.now(timezone.utc)
    # S2 first implementation followed the input audit in this same continuous turn.
    input_done = datetime.fromtimestamp((ROOT/'input-audit.json').stat().st_mtime, timezone.utc)
    need(now < input_done+timedelta(hours=10), 'IMPLEMENTATION_STAGE_BUDGET')
    save(ROOT/'model-freeze.json', dict(at=now.isoformat(), sources={p: sha(p) for p in SOURCES},
        input_audit_sha256=sha(ROOT/'input-audit.json'), raw_bindings_sha256=sha(ROOT/'raw-input-bindings.json'),
        acceptance_sha256=sha(inp.ACCEPTANCE), regression=tests[-1], protected_bindings=count,
        compute_deadline_utc=(now+timedelta(hours=4)).isoformat(), compute_seconds_limit=3600,
        max_paired_tasks=7, max_ledger_exports=14, resources=resources()))
    print('REFERENCE_MODEL_FROZEN; MARKET_BATCHES_CONSUMED=0', flush=True)


def claim_attempt(root, payload):
    """Exclusive file creation is the one-batch latch; no reset path."""
    save(Path(root)/'economic-attempt.json', payload)


def approved_resume(f):
    c = read(ROOT/'execution-audit-correction.json')
    need(c['scope'] == 'AUDIT_PROVEN_SIMULTANEOUS_EXIT_FAILURE_RESUME_MISSING_DELAY14' and
         c['failure_id'] == '8495cbb5832b4f9f97d60b540fbc08d8', 'NAMED_CORRECTION_ONLY')
    need(c['old_sources'] == f['sources'] and c['new_sources'] == {p: sha(p) for p in SOURCES},
         'CORRECTION_SOURCE_BINDING')
    changed = {p for p, h in c['old_sources'].items() if c['new_sources'][p] != h}
    need(changed == {'tools/audit_cross_asset.py', 'tools/run_cross_asset.py',
                    'tools/test_cross_asset_model.py', 'tools/test_cross_asset_runner.py'}, 'CORRECTION_FILE_SCOPE')
    need(all(sha(p) == h for p, h in c['original_sources'].items()), 'ORIGINAL_SOURCE_ARCHIVE')
    need(all(sha(p) == h for p, h in c['retained'].items()), 'EXISTING_ECONOMIC_EVIDENCE_CHANGED')
    review_path = Path(c['review_path'])
    review = read(review_path)
    need(review['correction_sha256'] == sha(ROOT/'execution-audit-correction.json') and
         review['failure_id'] == c['failure_id'], 'CORRECTION_REVIEW_BINDING')
    need(any(v.get('event') == 'review' and v.get('decision') == 'retry' and
             v.get('failure_id') == c['failure_id'] and v.get('review_sha256') == sha(review_path)
             for v in read(ROOT/'validation-state.json')['history']), 'CORRECTION_NOT_REVIEWED')
    need(c['remaining_simulations'] == ['delay-14'] and c['existing_selected_horizon'] == 14,
         'NO_NEW_PARAMETER_OR_BATCH')
    return c


def missing_tasks(selected, saved):
    expected = [f'prefix-{h}' for h in (7, 14, 28)]
    if selected is not None:
        expected += [f'{mode}-{h}' for mode in ('normal', 'delay') for h in sorted({7, selected})]
    need(set(saved) <= set(expected), 'UNEXPECTED_SAVED_TASK')
    return [name for name in expected if name not in saved]


def execute():
    os.umask(0o077)
    active(); count = preservation()
    f = read(ROOT/'model-freeze.json')
    corrected = not all(sha(p) == h for p, h in f['sources'].items())
    correction = approved_resume(f) if corrected else None
    need(sha(ROOT/'input-audit.json') == f['input_audit_sha256'] and
         sha(ROOT/'raw-input-bindings.json') == f['raw_bindings_sha256'] and
         sha(inp.ACCEPTANCE) == f['acceptance_sha256'], 'FROZEN_INPUT_SCOPE_CHANGED')
    need(datetime.now(timezone.utc) < datetime.fromisoformat(f['compute_deadline_utc']), 'ECONOMIC_PHASE_BUDGET')
    if corrected:
        need(subprocess.run(TEST_ARGV, timeout=180).returncode == 0, 'CORRECTION_REGRESSIONS')
    archive = Archive()
    if not corrected:
        claim_attempt(ROOT, dict(at=inp.now(), model_sha256=sha(ROOT/'model-freeze.json'), attempt=1))
    else:
        save(ROOT/'audit-resume-attempt.json', dict(at=inp.now(), failure_id=correction['failure_id'],
             economic_attempt_sha256=sha(ROOT/'economic-attempt.json'), new_market_batches=0))
    begun_utc = datetime.fromisoformat(read(ROOT/'economic-attempt.json')['at'])
    charged = max(0., (datetime.now(timezone.utc)-begun_utc).total_seconds())
    need(charged < 3600, 'ORIGINAL_CUMULATIVE_COMPUTE_BUDGET')
    started = time.monotonic(); deadline = started+3600-charged
    paths = {}; audits = {}; prefix = {}
    def record(name, sim):
        need(len(paths) < 7 and time.monotonic() < deadline, 'TASK_OR_COMPUTE_LIMIT')
        snapshot = json.loads(json.dumps(sim.snapshot(), allow_nan=False))
        save(ROOT/(name+'.json'), snapshot)
        checked = independent.audit(archive, snapshot, deadline)
        save(ROOT/(name+'-audit.json'), checked)
        paths[name] = snapshot; audits[name] = checked
        resources()
        print(json.dumps(dict(task=name, cash=[b['cash'] for b in snapshot['books']],
            stopped=[b['stopped'] for b in snapshot['books']], minutes=snapshot['minute'],
            decimal_audit='PASS')), flush=True)
        return snapshot
    if corrected:
        paths = {name: read(ROOT/(name+'.json')) for name in correction['existing_snapshots']}
        audits = {name: read(ROOT/(name+'-audit.json')) for name in correction['existing_audits']}
        need(missing_tasks(14, paths) == ['delay-14'], 'RESUME_TASK_SET')
        prefix = {h: paths[f'prefix-{h}'] for h in (7, 14, 28)}
        selected = read(ROOT/'selection.json')['selected']
        independent.selection(prefix, selected)
        checked = independent.audit(archive, paths['delay-7'], deadline)
        save(ROOT/'delay-7-audit.json', checked); audits['delay-7'] = checked
        print('EXISTING_DELAY7_AUDITED: RESIDUAL_EXPOSURE_UNCLOSED; NO_PATH_REPLAY', flush=True)
    else:
        for horizon in (7, 14, 28):
            sim = model.Simulation(archive, horizon).run_to(model.HALF, deadline)
            prefix[horizon] = record(f'prefix-{horizon}', sim)
        selected = model.select(prefix)
        save(ROOT/'selection-audit.json', independent.selection(prefix, selected))
        save(ROOT/'selection.json', dict(selected=selected, vectors=[7, 14, 28], at_utc='2024-07-01T00:00:00Z',
            prefix_hashes={str(h): sha(ROOT/f'prefix-{h}.json') for h in prefix}, reset=False))
    if selected is not None:
        for horizon in sorted({7, selected}):
            if f'normal-{horizon}' in paths:
                continue
            sim = model.Simulation.restore(archive, prefix[horizon]).run_to(model.END, deadline)
            record(f'normal-{horizon}', sim)
        for horizon in sorted({7, selected}):
            if f'delay-{horizon}' in paths:
                continue
            sim = model.Simulation(archive, horizon, delay=1).run_to(model.END, deadline)
            record(f'delay-{horizon}', sim)
    decision = verdict.decide(paths, selected)
    save(ROOT/'decision-audit.json', independent.decision(paths, selected, decision))
    planning = verdict.confirmation_planning(decision)
    need(time.monotonic() < deadline and
         datetime.now(timezone.utc) < datetime.fromisoformat(f['compute_deadline_utc']), 'ECONOMIC_PHASE_BUDGET')
    need(preservation() == count, 'PRESERVATION_CHAIN')
    save(ROOT/'result.json', dict(status='BOUNDED_DEVELOPMENT_BATCH_COMPLETED', decision=decision,
        confirmation_planning=planning, source_sha256=sha(ROOT/'model-freeze.json'),
        correction_sha256=sha(ROOT/'execution-audit-correction.json') if corrected else None,
        input_sha256=sha(ROOT/'input-audit.json'), market_batches=1, vectors=3,
        paired_tasks=len(paths), decimal_books=len(audits)*2,
        snapshot_hashes={name: sha(ROOT/(name+'.json')) for name in paths},
        audit_hashes={name: sha(ROOT/(name+'-audit.json')) for name in paths},
        compute_seconds_conservative_including_diagnosis=charged+time.monotonic()-started,
        resumed_without_replaying_existing_paths=corrected, resources=resources(), at=inp.now(),
        new_public_gets=1056, automatic_retries=0, confirmation_attempts=0, online_updates=0,
        account_reads=0, orders=0, deployments=0, protected_bindings=count))
    print(json.dumps(dict(status=decision['status'], candidate=decision['candidate'],
                         selected=selected, paired_tasks=len(paths),
                         compute_seconds=charged+time.monotonic()-started)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'execute'))
    {'freeze': freeze, 'execute': execute}[parser.parse_args().action]()
