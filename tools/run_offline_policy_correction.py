"""Single predeclared offline method-correction batch; no network or activation."""
import argparse
from collections import deque
import csv
import datetime as dt
import json
from pathlib import Path
import subprocess
import time

import run_bounded_learning as common
from offline_policy_correction import BASELINE, MS, features, replay, optimize, verdict

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.artifacts/offline-policy-correction-20260930'
CONTRACT = ROOT / 'docs/plans/2026-09-30-offline-policy-correction.contract.json'
PLAN = ROOT / 'docs/plans/2026-09-30-offline-policy-correction.md'
BINARY = ROOT / '.artifacts/execution-net-learning-20260930/build/bounded_learning_driver'
need, sha, save = common.need, common.sha, common.save
SOURCES = ('tools/offline_policy_correction.py', 'tools/run_offline_policy_correction.py',
           'tools/audit_offline_policy_correction.py', 'tools/test_offline_policy_correction.py',
           'tools/execution_net_learning.py', 'tools/run_bounded_learning.py',
           'tools/audit_execution_net_learning.py', 'tools/audit_bounded_learning.py',
           'tools/bounded_learning_driver.cpp')


def contract():
    c = json.loads(CONTRACT.read_text())
    elapsed = (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(c['started_utc'])).total_seconds()
    need(0 <= elapsed <= 16*3600, 'SHARED_SIXTEEN_HOUR_BUDGET')
    need(c['maximum_market_attempts'] == 1 and c['maximum_training_vectors'] == 25, 'ATTEMPT_LIMIT')
    need(c['grid'] == [-4,-2,-1,0,1,2,4] and c['coordinate_sweeps'] == 2
         and c['baseline'] == [1,0] and c['feature_bars'] == [12,288], 'METHOD_CONTRACT')
    need(c['confirmation'] == dict(interval=None, maximum_runs=0, learning=False), 'NO_CONFIRMATION')
    need(all(c[k] == 0 for k in ('network_requests','account_access','deployment','push_attempts')), 'NO_AUTHORITY')
    preserved = json.loads((RUN/'baseline.json').read_text())['preserved']
    need(all(sha(ROOT/p) == h for p,h in preserved.items()), 'PRESERVED_CHANGED')
    return c


def preflight():
    from mvp_reference_inputs import compile_archive
    import hashlib
    c = contract()
    raw, proof = compile_archive(ROOT/'.artifacts/mvp-reference-history-20260921/archive/manifest.json')
    need(hashlib.sha256(raw).hexdigest() == c['input_sha256'] == sha(ROOT/c['input_path']), 'RAW_INPUT_IDENTITY')
    prior = json.loads((ROOT/'.artifacts/execution-net-learning-20260930/execution-freeze.json').read_text())
    need(sha(BINARY) == prior['binary_sha256'], 'FROZEN_NATIVE_CHANGED')
    need((c['evaluation_start_ms']-c['train_start_ms'])//MS == 25920, 'TRAIN_CAPACITY')
    need((c['end_ms']-c['evaluation_start_ms'])//MS == 79200, 'EVAL_CAPACITY')
    save(RUN/'execution-freeze.json', dict(contract_sha256=sha(CONTRACT), plan_sha256=sha(PLAN),
         sources={p:sha(ROOT/p) for p in SOURCES}, native_sha256=sha(BINARY),
         input_sha256=c['input_sha256'], raw_proof=proof, frozen_at=common.now(),
         train_bars=25920, evaluation_bars=79200, evaluation_complete_weeks=39))
    print('FROZEN train=25920 evaluation=79200 weeks=39; confirmation NOT_ADMITTED')


def samples(csv_path, start, end, binary, deadline):
    """Do not request even the first evaluation record during training."""
    need(start < end and (end-start) % MS == 0, 'DOMAIN')
    context = deque(maxlen=288)
    prior_bucket = 1
    rows = []
    last = None
    warmup = 0
    process = subprocess.Popen([str(binary),'frozen','.5','.5','.5'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        with Path(csv_path).open() as source:
            for row in csv.DictReader(source):
                ts = int(row['timestamp'])
                if ts < start-288*MS:
                    continue
                need(ts < end, 'DOMAIN_END_REACHED_WITHOUT_FINAL_BAR')
                need(last is None or ts == last+MS, 'INPUT_GAP')
                need(time.monotonic() < deadline, 'COMPUTE_BUDGET')
                if ts >= start:
                    need(len(context) == 288, 'CONTEXT_MISSING')
                    rows.append(dict(row=row, features=features(context), bucket=prior_bucket))
                else:
                    warmup += 1
                need(process.stdin is not None and process.stdout is not None, 'NATIVE_PIPES')
                process.stdin.write(common.protocol(row, False, common.Wallet()))
                process.stdin.flush()
                line = process.stdout.readline()
                need(bool(line), 'NATIVE_TERMINATED')
                signal = json.loads(line)
                need(signal['ts'] == ts+MS and not signal['updated'] and not signal['withdrawn'], 'NATIVE_CAUSALITY')
                prior_bucket = signal['bucket']
                need(prior_bucket in (0,1,2), 'BUCKET')
                context.append(row)
                last = ts
                if ts+MS == end:
                    break
        need(last is not None and last+MS == end and warmup == 288, 'INPUT_COVERAGE')
        need(len(rows) == (end-start)//MS, 'DOMAIN_COVERAGE')
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise ValueError('NATIVE_EXIT_TIMEOUT')
        error = process.stderr.read() if process.stderr else ''
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
        need(code == 0, 'NATIVE_FAILURE:'+error[:500])
    return rows


def execute():
    c = contract()
    frozen = json.loads((RUN/'execution-freeze.json').read_text())
    need(sha(CONTRACT) == frozen['contract_sha256'] and sha(PLAN) == frozen['plan_sha256'], 'CONTRACT_CHANGED')
    need(all(sha(ROOT/p) == h for p,h in frozen['sources'].items()), 'SOURCE_CHANGED')
    need(sha(BINARY) == frozen['native_sha256'] and sha(ROOT/c['input_path']) == frozen['input_sha256'], 'INPUT_CHANGED')
    save(RUN/'market-attempt.json', dict(started=common.now(), maximum_attempts=1,
         execution_freeze_sha256=sha(RUN/'execution-freeze.json'), confirmation_runs=0))
    started = time.monotonic()
    deadline = started+c['maximum_compute_seconds']
    train = samples(ROOT/c['input_path'],c['train_start_ms'],c['evaluation_start_ms'],BINARY,deadline)
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
        result = run_path(train, vector, f'train-{trial_number:02d}')
        print(json.dumps(dict(stage='TRAIN', trial=trial_number, coefficients=vector,
                              objective=result['objective'], reason=result['reason'],
                              active_bars=result['active_bars'])), flush=True)
        return result

    model = optimize(evaluate)
    model.update(frozen_at=common.now(), input_sha256=c['input_sha256'],
                 training_end_exclusive_ms=c['evaluation_start_ms'], confirmation_runs=0)
    save(RUN/'frozen-model.json', model)
    model_hash = sha(RUN/'frozen-model.json')
    del train
    save(RUN/'evaluation-start.json', dict(started=common.now(), frozen_model_sha256=model_hash,
                                         start_ms=c['evaluation_start_ms'], end_ms=c['end_ms']))
    evaluation = samples(ROOT/c['input_path'],c['evaluation_start_ms'],c['end_ms'],BINARY,deadline)
    fixed = run_path(evaluation,BASELINE,'evaluation-fixed')
    learned = run_path(evaluation,tuple(model['coefficients']),'evaluation-learned')
    need(sha(RUN/'frozen-model.json') == model_hash, 'MODEL_CHANGED_DURING_EVALUATION')
    decision, lower = verdict(model,fixed,learned)
    result = dict(schema='offline_policy_correction_result_v1', verdict=decision,
                  scope='DEVELOPMENT_ONLY', market_attempts=1, training_vectors=trial_number,
                  model_changed=model['changed'], coefficients=model['coefficients'],
                  model_sha256=model_hash, fixed=fixed, learned=learned, hac_lower=lower,
                  confirmation_runs=0, confirmation_interval=None,
                  candidate_status='NO_QUALIFIED_CANDIDATE', execution_freeze_sha256=sha(RUN/'execution-freeze.json'),
                  compute_seconds=time.monotonic()-started, completed=common.now(),
                  network_requests=0,account_access=0,deployment=0,push_attempts=0)
    save(RUN/'result.json',result)
    print(json.dumps(dict(verdict=decision,coefficients=model['coefficients'],training_vectors=trial_number,
                         model_changed=model['changed'], fixed_net=[w['cash']-10000 for w in fixed['wallets']],
                         learned_net=[w['cash']-10000 for w in learned['wallets']],
                         learned_active_bars=learned['active_bars'], complete_weeks=len(learned['weekly']),
                         compute_seconds=result['compute_seconds']),allow_nan=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight','run'))
    preflight() if parser.parse_args().action == 'preflight' else execute()
