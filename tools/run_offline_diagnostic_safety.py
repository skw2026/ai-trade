"""One authorized development batch; frozen prior model family, no transport."""
import argparse
import datetime as dt
import json
from pathlib import Path
import time

import run_bounded_learning as common
import run_offline_policy_correction as prior
from offline_policy_correction import BASELINE, optimize
from offline_diagnostic_safety import MODE, replay
from offline_policy_correction import verdict

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'.artifacts/offline-diagnostic-safety-20260930'
CONTRACT = ROOT/'docs/plans/2026-09-30-offline-diagnostic-safety.contract.json'
PLAN = ROOT/'docs/plans/2026-09-30-offline-diagnostic-safety.md'
SOURCES = tuple(dict.fromkeys(prior.SOURCES + (
    'tools/offline_diagnostic_safety.py', 'tools/run_offline_diagnostic_safety.py',
    'tools/audit_offline_diagnostic_safety.py', 'tools/test_offline_diagnostic_safety.py',
    'tools/mvp_reference_inputs.py', 'CMakeLists.txt')))
need, sha, save = common.need, common.sha, common.save


def contract():
    c = json.loads(CONTRACT.read_text())
    elapsed = (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(c['started_utc'])).total_seconds()
    need(0 <= elapsed <= 16*3600, 'SHARED_SIXTEEN_HOUR_BUDGET')
    old = json.loads(prior.CONTRACT.read_text())
    for k in ('maximum_engineering_hours','maximum_market_attempts','maximum_training_vectors',
              'maximum_compute_seconds','input_path','input_sha256','train_start_ms',
              'evaluation_start_ms','end_ms','scope','confirmation','grid','coordinate_sweeps',
              'baseline','feature_bars','reference_capital','notional','reference_drawdown_stop',
              'network_requests','account_access','deployment','push_attempts'):
        need(c[k] == old[k], 'UNCHANGED_CONTRACT:'+k)
    need(c['safety_mode'] == MODE and c['legacy_qualification_unchanged'] is True, 'DIAGNOSTIC_ONLY')
    preserved = json.loads((RUN/'baseline.json').read_text())['preserved']
    need(all(sha(ROOT/p) == h for p,h in preserved.items()), 'HISTORICAL_FILES_CHANGED')
    return c


def preflight():
    from mvp_reference_inputs import compile_archive
    import hashlib
    c = contract()
    raw, proof = compile_archive(ROOT/'.artifacts/mvp-reference-history-20260921/archive/manifest.json')
    need(hashlib.sha256(raw).hexdigest() == c['input_sha256'] == sha(ROOT/c['input_path']), 'INPUT_IDENTITY')
    old = json.loads((prior.RUN/'execution-freeze.json').read_text())
    need(sha(prior.BINARY) == old['native_sha256'], 'NATIVE_IDENTITY')
    save(RUN/'execution-freeze.json', dict(contract_sha256=sha(CONTRACT), plan_sha256=sha(PLAN),
         sources={p:sha(ROOT/p) for p in SOURCES}, native_sha256=sha(prior.BINARY),
         input_sha256=c['input_sha256'], raw_proof=proof, frozen_at=common.now(),
         train_bars=25920, evaluation_bars=79200, confirmation_runs=0))
    print('FROZEN diagnostic development; legacy qualification unchanged; confirmation=0')


def execute():
    c = contract()
    frozen = json.loads((RUN/'execution-freeze.json').read_text())
    need(sha(CONTRACT) == frozen['contract_sha256'] and sha(PLAN) == frozen['plan_sha256'], 'CONTRACT_CHANGED')
    need(all(sha(ROOT/p) == h for p,h in frozen['sources'].items()), 'SOURCE_CHANGED')
    need(sha(prior.BINARY) == frozen['native_sha256'] and sha(ROOT/c['input_path']) == frozen['input_sha256'], 'INPUT_CHANGED')
    save(RUN/'market-attempt.json', dict(started=common.now(), maximum_attempts=1,
         execution_freeze_sha256=sha(RUN/'execution-freeze.json'), confirmation_runs=0))
    started = time.monotonic()
    deadline = started+c['maximum_compute_seconds']
    train = prior.samples(ROOT/c['input_path'],c['train_start_ms'],c['evaluation_start_ms'],prior.BINARY,deadline)
    trial_number = 0

    def run_path(domain, vector, name):
        need(time.monotonic() < deadline, 'COMPUTE_BUDGET')
        path = RUN/(name+'.jsonl')
        with path.open('x') as out:
            def emit(event):
                need(time.monotonic() < deadline, 'COMPUTE_BUDGET')
                out.write(json.dumps(event,allow_nan=False)+'\n')
            result = replay(domain,vector,emit)
        need(time.monotonic() < deadline, 'COMPUTE_BUDGET')
        return dict(result, trace_file=path.name, trace_sha256=sha(path))

    def evaluate(vector):
        nonlocal trial_number
        trial_number += 1
        result = run_path(train,vector,f'train-{trial_number:02d}')
        print(json.dumps(dict(stage='TRAIN',trial=trial_number,coefficients=vector,
              objective=result['objective'],reason=result['reason'],active_bars=result['active_bars'],
              legacy_path_status=result['legacy_path_status'])),flush=True)
        return result

    model = optimize(evaluate)
    model.update(frozen_at=common.now(),input_sha256=c['input_sha256'],
                 training_end_exclusive_ms=c['evaluation_start_ms'],confirmation_runs=0)
    save(RUN/'frozen-model.json',model)
    model_hash = sha(RUN/'frozen-model.json')
    del train
    save(RUN/'evaluation-start.json',dict(started=common.now(),frozen_model_sha256=model_hash,
         start_ms=c['evaluation_start_ms'],end_ms=c['end_ms']))
    evaluation = prior.samples(ROOT/c['input_path'],c['evaluation_start_ms'],c['end_ms'],prior.BINARY,deadline)
    fixed = run_path(evaluation,BASELINE,'evaluation-fixed')
    learned = run_path(evaluation,tuple(model['coefficients']),'evaluation-learned')
    need(sha(RUN/'frozen-model.json') == model_hash, 'MODEL_CHANGED_DURING_EVALUATION')
    decision, lower = verdict(model,fixed,learned)
    # Shared verdict retains its old name; only hard risk can stop these new paths.
    if decision == 'NO_GO_SAFETY_OR_REFERENCE_RISK':
        decision = 'NO_GO_REFERENCE_RISK'
    result = dict(schema='offline_diagnostic_safety_result_v1',verdict=decision,
         scope='DEVELOPMENT_ONLY',safety_mode=MODE,market_attempts=1,training_vectors=trial_number,
         model_changed=model['changed'],coefficients=model['coefficients'],model_sha256=model_hash,
         fixed=fixed,learned=learned,hac_lower=lower,confirmation_runs=0,confirmation_interval=None,
         candidate_status='NO_QUALIFIED_CANDIDATE',qualification=False,
         execution_freeze_sha256=sha(RUN/'execution-freeze.json'),compute_seconds=time.monotonic()-started,
         completed=common.now(),network_requests=0,account_access=0,deployment=0,push_attempts=0)
    save(RUN/'result.json',result)
    print(json.dumps(dict(verdict=decision,coefficients=model['coefficients'],training_vectors=trial_number,
         fixed_net=[w['cash']-10000 for w in fixed['wallets']],
         learned_net=[w['cash']-10000 for w in learned['wallets']],
         learned_active_bars=learned['active_bars'],legacy_path_status=learned['legacy_path_status'],
         hac_lower=lower,compute_seconds=result['compute_seconds']),allow_nan=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('preflight','run'))
    preflight() if parser.parse_args().action == 'preflight' else execute()
