"""One accepted local ETH/SOL development batch. No network or order entry."""
import argparse
from array import array
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

from mvp_reference_inputs import strict_json

ROOT = Path('.artifacts/eth-sol-complete-20261015')
PRIOR = Path('.artifacts/economic-experiment-proposal-20261015')
OLD = Path('.artifacts/eth-sol-program-20261015')
PROPOSAL = Path('docs/plans/2026-10-15-eth-sol-complete-economic-experiment.proposal.json')
ACCEPTANCE = Path('docs/plans/2026-10-15-eth-sol-complete-economic-execution.md')
START = dt.datetime(2026, 10, 15, 5, 44, 36, tzinfo=dt.timezone.utc)
SYMBOLS = ('ETHUSDT', 'SOLUSDT')
SOURCES = ('tools/eth_sol_complete_batch.py','tools/eth_sol_complete_model.py',
           'tools/audit_eth_sol_complete.py','tools/test_eth_sol_complete.py',
           'tools/eth_sol_model.py','tools/run_eth_sol_baseline.py','tools/execution_net_learning.py',
           'tools/eth_sol_market.py','tools/eth_sol_intake.py','tools/audit_eth_sol_input.py',
           'tools/mvp_reference_inputs.py','tools/validation_gate.py')
TEST_ARGV = ['python3','-m','unittest','discover','-s','tools','-p','test_eth_sol*.py','-v']


def need(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return strict_json(Path(path).read_bytes())


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')


def resources():
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform!='darwin':rss*=1024
    size=sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())
    need(rss<2*1024**3 and size<2*1024**3,'RESOURCE_BUDGET')
    return dict(peak_rss_bytes=rss,private_bytes=size,worker_processes=1)


def guard():
    need(read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'NEW_ACCEPTED_GATE_REQUIRED')
    need(START <= dt.datetime.now(dt.timezone.utc) < START+dt.timedelta(hours=18), 'TOTAL_ENGINEERING_BUDGET')
    if (ROOT/'freeze.json').exists():
        f = read(ROOT/'freeze.json')
        need(sha(PROPOSAL) == f['proposal_sha256'] and sha(ACCEPTANCE) == f['acceptance_sha256'], 'ACCEPTED_SCOPE_CHANGED')
    return read(PROPOSAL)


def preserve():
    protected = read(ROOT/'freeze.json')['protected']
    need(all(sha(p) == h for p,h in protected.items()), 'OLD_EVIDENCE_CHANGED')
    return len(protected)


def freeze():
    os.umask(0o077)
    p = guard()
    need(p['budget']['market_batches'] == 1 and p['budget']['engineering_hours_total_including_design'] == 20, 'ACCEPTED_BUDGET')
    protected = dict(read(PRIOR/'freeze.json')['protected'])
    for path in PRIOR.rglob('*'):
        if path.is_file() and path.suffix != '.lock' and '__pycache__' not in path.parts:
            protected[str(path)] = sha(path)
    for path in (PROPOSAL, PROPOSAL.with_suffix('').with_suffix('.md')):
        protected[str(path)] = sha(path)
    need(all(sha(path) == digest for path,digest in protected.items()), 'INITIAL_PRESERVATION')
    save(ROOT/'freeze.json', dict(protected=protected, proposal_sha256=sha(PROPOSAL),
         acceptance_sha256=sha(ACCEPTANCE), accepted_utc=START.isoformat(),
         design_reserved_hours=2, execution_hours_cap=18, computation_seconds_cap=7200,
         authority='User: 按照建议继续推进; accepted one named development reopening',
         market_attempts=0, baseline_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()))
    print(json.dumps(dict(status='NEW_DEVELOPMENT_SCOPE_FROZEN',protected_files=preserve())), flush=True)


def input_check():
    """Read every original page and compare directly with the immutable arrays."""
    p = guard(); preserve()
    need(dt.datetime.now(dt.timezone.utc) < START+dt.timedelta(hours=2), 'INPUT_STAGE_BUDGET')
    import eth_sol_market as market
    from audit_eth_sol_input import verify_records
    from run_eth_sol_baseline import Archive
    f, collection = read(OLD/'market-freeze.json'), read(OLD/'market-collection.json')
    market.verify_freeze(f)
    bound = verify_records(OLD,f,collection)
    need(bound['files'] == read(OLD/'collection-audit.json')['files'], 'ORIGINAL_COLLECTION_CHANGED')
    a = Archive()
    n = (a.end-a.start)//60000
    seen = {(s,k):bytearray(n) for s in SYMBOLS for k in ('trade','mark')}
    funding = {s:{} for s in SYMBOLS}; warm = {s:{} for s in SYMBOLS}
    values_checked = 0
    def accept(r, body):
        nonlocal values_checked
        for t,values in market.validate(body,r):
            s,k = r['symbol'],r['kind']
            if k == 'funding':
                need(t not in funding[s], 'DUPLICATE_RAW_FUNDING')
                funding[s][t] = float(values)
            elif r['interval_minutes'] == 60:
                need(t not in warm[s], 'DUPLICATE_RAW_WARMUP')
                warm[s][t] = float(values[3])
            else:
                index = (t-a.start)//60000; width = 5 if k == 'trade' else 4
                need(0 <= index < n and not seen[s,k][index], 'RAW_GRID_DUPLICATE')
                seen[s,k][index] = 1
                need(array('d',map(float,values[:width])) == a.arrays[s,k][index*width:(index+1)*width], 'RAW_COMPILED_VALUES')
                values_checked += width
    for i,r0 in enumerate(f['reuse_requests']):
        r = dict(r0,symbol='ETHUSDT',interval_minutes=0 if r0['kind']=='funding' else 1)
        accept(r,market.raw_page(f['reuse_folders'][str(i)],r0,True))
    for i,r in enumerate(f['requests']):
        accept(r,market.raw_page(collection['records'][str(i)]['folder'],r))
    need(all(all(v) for v in seen.values()), 'RAW_INPUT_GAP')
    need(funding == a.funding, 'RAW_FUNDING_MAPPING')
    times = list(range(market.ms(p['input']['warmup_start']),market.ms(p['input']['training_start']),3600000))
    need(all(sorted(warm[s]) == times for s in SYMBOLS), 'WARMUP_TIME_GRID')
    need([tuple(warm[s][t] for s in SYMBOLS) for t in times] == a.warmup, 'WARMUP_MAPPING')
    need(n == 527100 and all(len(v) == 1098 for v in funding.values()), 'COMPLETE_INPUT_COUNTS')
    for relative,digest in p['input']['identity'].items():
        need(sha(OLD/relative) == digest, 'PROPOSED_INPUT_IDENTITY')
    save(ROOT/'input-check.json', dict(status='EXISTING_INPUT_FULL_MAPPING_VERIFIED',
         files={str(OLD/k):v for k,v in p['input']['identity'].items()},
         original_new_pages=len(f['requests']), original_reused_pages=len(f['reuse_requests']),
         minute_rows_per_asset_kind=n, raw_price_values_compared=values_checked,
         funding_counts={s:len(v) for s,v in funding.items()},warmup_hours=1440,
         new_market_requests=0,market_attempts=0,protected_files=preserve()))
    print(json.dumps(dict(status='EXISTING_INPUT_FULL_MAPPING_VERIFIED',raw_price_values_compared=values_checked,
                         new_market_requests=0,market_attempts=0)),flush=True)


def model_freeze():
    guard();preserve()
    need(dt.datetime.now(dt.timezone.utc)<START+dt.timedelta(hours=8),'IMPLEMENTATION_STAGE_BUDGET')
    need(not (ROOT/'market-attempt.json').exists(),'MARKET_ALREADY_CONSUMED')
    from validation_gate import command_hash
    expected=command_hash(TEST_ARGV,str(Path.cwd().resolve()))
    tests=[e for e in read(ROOT/'validation-state.json')['history'] if e.get('event')=='validation' and
           e.get('exit_code')==0 and e.get('command_sha256')==expected]
    need(tests,'SYNTHETIC_TEST_EVIDENCE_REQUIRED')
    need(read(ROOT/'input-check.json')['status']=='EXISTING_INPUT_FULL_MAPPING_VERIFIED','INPUT_NOT_VERIFIED')
    save(ROOT/'model-freeze.json',dict(sources={p:sha(p) for p in SOURCES},
         regression_evidence=tests[-1],input_check_sha256=sha(ROOT/'input-check.json'),
         frozen_utc=dt.datetime.now(dt.timezone.utc).isoformat(),market_attempts_before=0,resources=resources()))
    print('NEW_REFERENCE_MODEL_FROZEN; MARKET_ATTEMPTS=0',flush=True)


def verify_model():
    f=read(ROOT/'model-freeze.json')
    need(all(sha(p)==h for p,h in f['sources'].items()),'FROZEN_MODEL_SOURCE_CHANGED')
    need(sha(ROOT/'input-check.json')==f['input_check_sha256'],'INPUT_CHECK_CHANGED')


def market():
    os.umask(0o077);p=guard();preserve();verify_model();resources()
    need(dt.datetime.now(dt.timezone.utc)<START+dt.timedelta(hours=14),'EXPERIMENT_STAGE_BUDGET')
    need(not (ROOT/'market-attempt.json').exists(),'ONE_MARKET_ATTEMPT_ALREADY_CONSUMED')
    from eth_sol_market import ms
    from run_eth_sol_baseline import Archive
    from eth_sol_complete_model import Simulation,select,economic_decision,confirmation_plan
    timer=time.monotonic();deadline=timer+7200
    save(ROOT/'market-attempt.json',dict(started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
         model_freeze_sha256=sha(ROOT/'model-freeze.json'),market_attempt=1,q1_vectors_cap=9,
         continuations_cap=2,latency_pairs_cap=1,new_market_requests=0))
    archive=Archive();start=ms(p['input']['training_start']);end=ms(p['input']['evaluation_end'])
    boundary=(ms(p['input']['freeze_boundary'])-start)//60000;maximum=(end-start)//60000
    results=[];files={};paths={};checkpoints=[]
    def persist(label,simulation):
        checkpoint=ROOT/(label+'-snapshot.json');result=ROOT/(label+'-result.json')
        save(checkpoint,simulation.snapshot());save(result,simulation.result())
        files[str(checkpoint)]=sha(checkpoint);files[str(result)]=sha(result)
        paths[label]=dict(snapshot=str(checkpoint),result=str(result));resources()
        print(json.dumps(dict(stage=label,minutes=simulation.minute,
            cash=[b.cash for b in simulation.paths],liquidation=simulation.last_liquid,
            stops=[b.stopped for b in simulation.paths],elapsed=round(time.monotonic()-timer,2))),flush=True)
        return simulation.result()
    for i,parameters in enumerate(p['selection']['ordered_grid']):
        s=Simulation(archive,start,end,parameters).run_to(boundary,deadline)
        results.append(persist('q1-'+str(i),s));checkpoints.append(sha(ROOT/('q1-'+str(i)+'-snapshot.json')))
    selected=select(results)
    selection=dict(selected_index=selected,parameters=None if selected is None else p['selection']['ordered_grid'][selected],
         scores=[min(r['last_liquidation'])-10000 for r in results],q1_snapshot_hashes=checkpoints,
         eligible=[all(r[k]['stop'] is None and r[k]['insufficient'] is None for k in ('base','stress')) for r in results],
         selection_utc=dt.datetime.now(dt.timezone.utc).isoformat(),evaluation_started=False)
    save(ROOT/'selection.json',selection);files[str(ROOT/'selection.json')]=sha(ROOT/'selection.json')
    print(json.dumps(dict(stage='Q1_SELECTION_FROZEN',**selection)),flush=True)
    continued={};latency=None
    if selected is None:
        decision=dict(status='TECHNICAL_OR_EXECUTION_INSUFFICIENT' if any(r[k]['insufficient'] for r in results for k in ('base','stress')) else 'NO_GO_RISK',
                      failures=['NO_ELIGIBLE_Q1_PATH'])
    else:
        for i in dict.fromkeys((0,selected)):
            checkpoint=read(ROOT/('q1-'+str(i)+'-snapshot.json'))
            s=Simulation.restore(archive,checkpoint)
            need(s.snapshot()==checkpoint,'CONTINUATION_STATE_CHANGED')
            s.run_to(maximum,deadline);continued[i]=persist('full-'+str(i),s)
        # Execution insufficiency is a predeclared exit, never hidden by another run.
        if any(r[k]['insufficient'] for r in continued.values() for k in ('base','stress')):
            decision=dict(status='TECHNICAL_OR_EXECUTION_INSUFFICIENT',failures=['CONTINUATION_EXECUTION'])
        else:
            s=Simulation(archive,start,end,selection['parameters'],extra_delay=1).run_to(maximum,deadline)
            latency=persist('latency',s)
            decision=economic_decision(continued[selected],continued[0],latency,selected)
    elapsed=time.monotonic()-timer;need(elapsed<7200,'MARKET_COMPUTE_BUDGET')
    save(ROOT/'market-result.json',dict(status='PENDING_INDEPENDENT_AUDIT',decision=decision,
         confirmation_planning=confirmation_plan(decision),selection=selection,files=files,paths=paths,
         market_attempts=1,q1_vector_pairs=len(results),continuation_pairs=len(continued),latency_pairs=int(latency is not None),
         compute_seconds=elapsed,new_market_requests=0,account_operations=0,actual_fills_verified=False,
         all_development=True,independent_confirmation_attempts=0,resources=resources()))
    verify_model();preserve()
    print(json.dumps(dict(status='PENDING_INDEPENDENT_AUDIT',economic_status=decision['status'],compute_seconds=elapsed)),flush=True)


def audit():
    os.umask(0o077);guard();preserve();verify_model();timer=time.monotonic()
    r=read(ROOT/'market-result.json')
    need(all(sha(p)==h for p,h in r['files'].items()),'MARKET_OUTPUT_CHANGED')
    from run_eth_sol_baseline import Archive
    from audit_eth_sol_complete import audit_snapshot,bind_result,independent_selection,independent_verdict,audit_report_math
    archive=Archive();cache={};audits={};results={};snapshots={}
    for label,paths in r['paths'].items():
        need(time.monotonic()-timer+r['compute_seconds']<7200,'TOTAL_COMPUTATION_BUDGET')
        snap=read(paths['snapshot']);result=read(paths['result'])
        checked=audit_snapshot(archive,snap,cache);bind_result(snap,checked,result)
        audits[label]=checked;results[label]=result
        if label.startswith('full-'):
            original=read(ROOT/('q1-'+label.split('-')[1]+'-snapshot.json'))
            for key in ('models','daily','five'):
                need(snap[key][:len(original[key])]==original[key],'CONTINUATION_PREFIX:'+key)
            for a,b in zip(snap['paths'],original['paths']):
                need(a['events'][:len(b['events'])]==b['events'],'CONTINUATION_EVENT_PREFIX')
        save(ROOT/(label+'-independent-audit.json'),checked)
        print(json.dumps(dict(stage='INDEPENDENT_AUDIT',path=label,
             fills=[a['fills'] for a in checked],elapsed=round(time.monotonic()-timer,2))),flush=True)
        resources()
    selected=independent_selection([results['q1-'+str(i)] for i in range(9)])
    need(selected==r['selection']['selected_index'],'INDEPENDENT_SELECTION')
    if selected is None:
        verdict='TECHNICAL_OR_EXECUTION_INSUFFICIENT' if any(v[k]['insufficient'] for v in results.values() for k in ('base','stress')) else 'NO_GO_RISK'
    elif 'latency' not in results:
        need(any(v[k]['insufficient'] for label,v in results.items() if label.startswith('full-') for k in ('base','stress')),'MISSING_LATENCY_WITHOUT_EXECUTION_EXIT')
        verdict='TECHNICAL_OR_EXECUTION_INSUFFICIENT'
    else:
        verdict=independent_verdict(results['full-'+str(selected)],results['full-0'],results['latency'],selected)
        audit_report_math(r['decision'],r['confirmation_planning'],dict(candidate=results['full-'+str(selected)],
                          baseline=results['full-0'],latency=results['latency']))
    need(verdict==r['decision']['status'],'INDEPENDENT_ECONOMIC_VERDICT')
    if verdict!='DEVELOPMENT_SUPPORTED':
        need(r['confirmation_planning']==dict(status='NOT_JUSTIFIED_BY_DEVELOPMENT',observation_started=False),'UNSUPPORTED_WAIT')
    elapsed=time.monotonic()-timer
    need(elapsed+r['compute_seconds']<7200,'TOTAL_COMPUTATION_BUDGET')
    save(ROOT/'independent-audit.json',dict(status='INDEPENDENT_AUDIT_PASS',economic_status=verdict,
         selected_index=selected,paths=len(audits),books=sum(len(a) for a in audits.values()),
         market_result_sha256=sha(ROOT/'market-result.json'),compute_seconds=elapsed,
         total_compute_seconds=elapsed+r['compute_seconds'],protected_files=preserve(),resources=resources(),
         audit_files={str(ROOT/(label+'-independent-audit.json')):sha(ROOT/(label+'-independent-audit.json')) for label in audits},
         historical_failures_unchanged=True,g4=False,observation_started=False))
    print(json.dumps(dict(status='INDEPENDENT_AUDIT_PASS',economic_status=verdict,books=len(audits)*2)),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze','input','model-freeze','market','audit'))
    args = parser.parse_args()
    {'freeze':freeze,'input':input_check,'model-freeze':model_freeze,'market':market,'audit':audit}[args.action]()
