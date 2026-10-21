"""One authorized fixed-action counterfactual, not a new closed-loop policy."""
import argparse
import json
import os
from pathlib import Path
import resource
import sys
import time
from datetime import datetime, timezone

from cross_asset_inputs import need, read, save, sha, now
from sparse_reversal_2024_intake import START, END
from audit_offline_policy_correction import read_domain, lower_bound
from run_bounded_learning import hac_lower
from frozen_history_transfer import concentration
from audit_frozen_history_transfer import audit_concentration, compare_bounds
from fixed_intent_delay import FixedIntentPath, classify
from audit_fixed_intent_delay import audit, audit_source

RUN = Path('.artifacts/fixed-intent-delay-20261021')
PRIOR = Path('.artifacts/sparse-reversal-2024-20261020')
OLD = PRIOR/'continuation'
PLAN = Path('docs/plans/2026-10-21-fixed-intent-delay.md')
DEADLINE = '2026-10-21T13:55:00+00:00'
SOURCES = ('tools/fixed_intent_delay.py','tools/audit_fixed_intent_delay.py',
           'tools/run_fixed_intent_delay.py','tools/test_fixed_intent_delay.py',
           'tools/frozen_history_transfer.py','tools/offline_policy_correction.py',
           'tools/offline_diagnostic_safety.py','tools/execution_net_learning.py',
           'tools/run_bounded_learning.py','tools/audit_bounded_learning.py',
           'tools/audit_execution_net_learning.py','tools/audit_offline_policy_correction.py',
           'tools/audit_frozen_history_transfer.py','tools/cross_asset_inputs.py',
           'tools/mvp_reference_inputs.py','tools/validation_gate.py')


def active():
    need(read(RUN/'validation-state.json')['status'] == 'RUNNING', 'ACTIVE_GATE_REQUIRED')
    need(datetime.now(timezone.utc) < datetime.fromisoformat(DEADLINE), 'FOUR_HOUR_BUDGET')


def preservation():
    from run_open_market_selective import preserve
    historic = preserve()
    need(historic == 8070, 'HISTORICAL_BINDINGS')
    files = read(RUN/'preserved.json')['files']
    for p,h in files.items(): need(sha(p) == h, 'PREVIOUS_STAGE_CHANGED:'+p)
    return dict(historical_bindings=historic,additional_bindings=len(files))


def initialize():
    active()
    need(read(PRIOR/'validation-state.json')['status'] == 'READY', 'PREVIOUS_STAGE_FINAL')
    r = read(OLD/'result.json')
    need(r['decision']['status'] == 'REJECT_DELAY_RISK' and r['economic_attempts'] == 1, 'OLD_FAILURE_RETAINED')
    need(read(OLD/'publication-final.json')['sha'] == 'f06a576506e2a4298d64de22a3fba0cd6472eaa2', 'PREVIOUS_DELIVERY')
    for p,h in r['evidence'].items(): need(sha(p) == h,'OLD_RESULT_BINDING')
    oldfreeze = read(OLD/'execution-freeze.json')
    for p,h in oldfreeze['sources'].items(): need(sha(p) == h,'OLD_SOURCE_CHANGED')
    protected = {str(p):sha(p) for p in PRIOR.rglob('*') if p.is_file() and p.suffix != '.lock'}
    protected.update(oldfreeze['sources'])
    protected.update(read(OLD/'input-freeze.json')['files'])
    old = read('.artifacts/cross-asset-relative-strength-20261020/input-freeze.json')['protected']
    for name in ('aave_authenticated_batch.py','aave_authenticated_input.py','aave_complete_boundaries.py',
                 'test_aave_authenticated_batch.py','test_aave_authenticated_input.py','test_aave_complete_boundaries.py'):
        p = 'tools/'+name; need(sha(p) == old[p],'USER_DRAFT'); protected[p] = old[p]
    for p in ('docs/reviews/2026-10-21-sparse-reversal-2024-result.md',): protected[p] = sha(p)
    save(RUN/'preserved.json',dict(files=protected))
    save(RUN/'scope.json',dict(plan_sha256=sha(PLAN),started_utc='2026-10-21T09:55:00+00:00',
        deadline=DEADLINE,maximum_hours=4,new_market_gets=0,training_updates=0,
        maximum_market_attempts=1,maximum_paired_paths=1,maximum_compute_seconds=900,
        memory_limit_bytes=1073741824,output_limit_bytes=536870912))
    print('FIXED_INTENT_NEW_SCOPE_INITIALIZED',json.dumps(preservation()),flush=True)


def check():
    active()
    need(read(RUN/'scope.json')['plan_sha256'] == sha(PLAN),'PLAN_CHANGED')
    if (RUN/'freeze.json').exists():
        f = read(RUN/'freeze.json')
        need(f['sources'] == {p:sha(p) for p in SOURCES},'EXECUTION_OR_AUDIT_CHANGED')
        need(f['scope_sha256'] == sha(RUN/'scope.json') and
             f['preserved_sha256'] == sha(RUN/'preserved.json'),'SCOPE_BINDING')


def load_intents(trace, domain, summary):
    need(sha(trace) == summary['trace_sha256'] and summary['reason'] == 'COMPLETE' and
         summary['coefficients'] == [0,-4] and not summary['qualification'],'SOURCE_IDENTITY')
    intents = []
    with Path(trace).open() as f:
        rows = (json.loads(s) for s in f)
        for sample in domain:
            e = next(rows)
            need(e['kind'] == 'BAR' and e['timestamp'] == int(sample['row']['timestamp']),'SOURCE_GRID')
            need(type(e['target']) in (int,float) and e['target'] in (-2500,0,2500),'SOURCE_TARGET')
            intents.append(e['target'])
        last = next(rows)
        need(last['kind'] == 'TERMINAL' and next(rows,None) is None,'SOURCE_LENGTH')
        need(last['result'] == {k:v for k,v in summary.items() if k not in ('trace_file','trace_sha256')},'SOURCE_TERMINAL')
    return intents


def freeze():
    check(); counts = preservation()
    need(any(e.get('label') == 'fixed-intent-synthetic-and-regression' and e.get('exit_code') == 0
             for e in read(RUN/'validation-state.json')['history']),'SYNTHETIC_TESTS_REQUIRED')
    save(RUN/'freeze.json',dict(sources={p:sha(p) for p in SOURCES},scope_sha256=sha(RUN/'scope.json'),
        preserved_sha256=sha(RUN/'preserved.json'),at=now(),preservation=counts,
        old_result_sha256=sha(OLD/'result.json'),source_trace_sha256=sha(OLD/'normal.jsonl')))
    print('ONE_FIXED_ACTION_PAIRED_PATH_FROZEN',flush=True)


def execute():
    check(); preservation()
    need((RUN/'freeze.json').is_file(),'FREEZE_REQUIRED')
    save(RUN/'economic-attempt.json',dict(at=now(),freeze_sha256=sha(RUN/'freeze.json'),attempt=1))
    begun = time.monotonic()
    def budget():
        need(time.monotonic()-begun < 900,'COMPUTE_BUDGET')
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform == 'darwin' else 1024)
        need(rss < 1073741824,'MEMORY_BUDGET')
        need(sum(p.stat().st_size for p in RUN.rglob('*') if p.is_file()) < 536870912,'OUTPUT_BUDGET')
    normal = read(OLD/'normal.json')
    proof = read(OLD/'input-proof.json')
    need(sha(OLD/'replay.csv') == proof['csv_sha256'] and proof['evaluation_bars'] == 105408,'INPUT_BINDING')
    domain = read_domain(OLD/'replay.csv',START,END)
    intents = load_intents(OLD/'normal.jsonl',domain,normal)
    independent_intents = audit_source(OLD/'normal.jsonl',domain,normal)
    need(intents == independent_intents,'INTENT_EXTRACTION_AUDIT')
    save(RUN/'source-audit.json',dict(rows=len(intents),normal_trace_sha256=sha(OLD/'normal.jsonl'),
         input_csv_sha256=sha(OLD/'replay.csv'),status='FROZEN_CAUSAL_INTENTS_BOUND',new_source_policy_runs=0))
    budget(); engine = FixedIntentPath(); trace = RUN/'fixed-delay.jsonl'
    with trace.open('x') as f:
        for i,(sample,intent) in enumerate(zip(domain,intents)):
            if i % 1000 == 0: budget()
            e = engine.consume(sample,intent)
            if e is not None: f.write(json.dumps(e,allow_nan=False)+'\n')
        summary = engine.finish(); f.write(json.dumps(dict(kind='TERMINAL',result=summary),allow_nan=False)+'\n')
    result = dict(summary,trace_file=trace.name,trace_sha256=sha(trace))
    save(RUN/'fixed-delay.json',result)
    budget()
    checked = audit(trace,domain,independent_intents,result)
    save(RUN/'account-audit.json',checked)
    need(classify(result) == checked['verdict'],'VERDICT_AUDIT')
    with trace.open() as f: groups = concentration((json.loads(s) for s in f),result)
    audit_concentration(trace,result,groups); save(RUN/'groups.json',groups)
    need(len(result['weekly']) == 52 and result['domain_bars'] == 105408,'FULL_CALENDAR')
    # Descriptive only: neither a new admission threshold nor independent evidence.
    bound = hac_lower([w[1] for w in result['weekly']])
    compare_bounds({'pressure':bound},{'pressure':lower_bound([w[1] for w in result['weekly']])})
    budget(); check(); counts = preservation()
    output = dict(status=checked['verdict'],paired_paths=1,decimal_books=2,economic_attempts=1,
        source_policy_runs=0,training_updates=0,new_market_gets=0,confirmation_runs=0,
        qualification=False,g4=False,closed_loop_proven=False,demo_opening_lock_unchanged=True,
        economics=checked['economics'],reason=result['reason'],active_bars=result['active_bars'],
        first_legacy_withdrawal_ms=result['first_legacy_withdrawal_ms'],legacy_path_status=result['legacy_path_status'],
        stop_at_ms=result['stop_at_ms'],exit_at_ms=result['exit_at_ms'],full_weeks=52,
        episode_count=groups['episode_count'],separated_clusters=groups['separated_clusters'],
        nonzero_full_blocks=groups['nonzero_full_blocks'],leave_largest_cluster_net=groups['leave_largest_cluster_net'],
        pressure_hac_lower_descriptive=bound,
        net_change_from_normal=[a['net']-(b['cash']-10000) for a,b in zip(checked['economics'],normal['wallets'])],
        old_batch_status=read(OLD/'result.json')['decision']['status'],preservation=counts,
        compute_seconds=time.monotonic()-begun,completed_utc=now(),
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform == 'darwin' else 1024),
        evidence={str(p):sha(p) for p in RUN.iterdir() if p.is_file() and p.name != 'validation-state.json' and p.suffix != '.lock'})
    save(RUN/'result.json',output)
    print(json.dumps(output,sort_keys=True),flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(); p.add_argument('action',choices=('initialize','freeze','execute'))
    {'initialize':initialize,'freeze':freeze,'execute':execute}[p.parse_args().action]()
