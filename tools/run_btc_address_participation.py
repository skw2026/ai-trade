"""One accepted, immutable local development batch. No input download or trading."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import time

import btc_address_participation as signal
import btc_old_output_model as execution
import audit_btc_address_participation as independent
import audit_btc_fee_pressure as rule_audit
from audit_btc_old_output import audit as audit_ledger

ROOT=Path('.artifacts/data-first-mechanisms-20261016')
BATCH_PLAN=Path('docs/plans/2026-10-16-data-first-mechanisms.md')
PROPOSAL=Path('docs/plans/2026-10-16-btc-address-participation.contract.json')
EXECUTION_PLAN=Path('docs/plans/2026-10-16-btc-address-participation.md')
SOURCES=[Path('tools')/name for name in ('btc_address_participation.py','audit_btc_address_participation.py',
    'run_btc_address_participation.py','test_btc_address_participation.py',
    'btc_fee_pressure.py','audit_btc_fee_pressure.py','btc_old_output_model.py','audit_btc_old_output.py')]


def read(p):return json.loads(Path(p).read_text())
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,value):
    with Path(p).open('x') as f:json.dump(value,f,indent=2,sort_keys=True,allow_nan=False);f.write('\n')
def guard():
    signal.require(read(ROOT/'validation-state.json')['status']=='RUNNING','ACTIVE_BATCH_GATE_REQUIRED')
    signal.require(dt.datetime.now(dt.timezone.utc)<dt.datetime(2026,10,16,15,28,29,tzinfo=dt.timezone.utc),'TIME_BUDGET')
def preserve():
    scope=read(ROOT/'freeze.json')
    signal.require(all(sha(p)==h for p,h in scope['files'].items()),'OLD_EVIDENCE_CHANGED')
    signal.require(sha(BATCH_PLAN)==scope['plan_sha256'],'ACCEPTANCE_CHANGED')
    return len(scope['files'])


def freeze():
    guard();preserve()
    p=read(PROPOSAL);inputs=dict(p['market_files'])
    inputs[p['input_file']]=p['input_sha256']
    signal.require(all(sha(path)==h for path,h in inputs.items()),'INPUT_IDENTITY')
    admitted=signal.inspect_input(Path(p['input_file']).read_bytes())
    save(ROOT/'model-input.json',admitted)
    signal.require(read(ROOT/'input-audit.json')['status']=='RECONSTRUCTED_DEVELOPMENT_INPUT_ADMITTED','INPUT_NOT_ADMITTED')
    for p in (PROPOSAL,EXECUTION_PLAN,BATCH_PLAN,ROOT/'input-audit.json',ROOT/'model-input.json'):
        inputs[str(p)]=sha(p)
    signal.require(all(sha(p)==h for p,h in inputs.items()),'INPUT_IDENTITY')
    save(ROOT/'model-freeze.json',dict(sources={str(p):sha(p) for p in SOURCES},inputs=inputs,
         frozen_utc=dt.datetime.now(dt.timezone.utc).isoformat(),maximum_vectors=8,
         maximum_evaluation_paths=6,maximum_economic_attempts=1))
    print('ADDRESS_MODEL_FROZEN_BEFORE_ECONOMIC_ATTEMPT')


def execute():
    guard();preserve();f=read(ROOT/'model-freeze.json')
    signal.require(all(sha(p)==h for p,h in dict(f['sources'],**f['inputs']).items()),'FROZEN_IDENTITY')
    save(ROOT/'economic-attempt.json',dict(freeze_sha256=sha(ROOT/'model-freeze.json'),
        started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),maximum_compute_seconds=1800))
    started=time.monotonic();audited=[];training=[];paths=[]
    def budget():
        guard();signal.require(time.monotonic()-started<1800,'COMPUTE_BUDGET')
        signal.require(sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())<256*1024*1024,'EVIDENCE_BUDGET')
    def one(name,rows,features,params,start,end,**kw):
        budget()
        save(ROOT/(name+'-attempt.json'),dict(name=name,parameters=list(params),start=start,end=end,
            model_freeze_sha256=sha(ROOT/'model-freeze.json')))
        result=execution.simulate(rows,features,params,start,end,**kw)
        save(ROOT/(name+'.json'),result)
        receipt=audit_ledger(result,rows);save(ROOT/(name+'-audit.json'),receipt);audited.append(name)
        signal.require(signal.complete(result),'EXECUTION_INCOMPLETE_DEPENDENCIES_STOPPED')
        budget()
        print(json.dumps(dict(path=name,parameters=params,net=[b['net'] for b in result['books']],
            stopped=[b['stopped'] for b in result['books']],
            cycles=[sum(c['complete'] for c in b['cycles']) for b in result['books']])),flush=True)
        return result
    p=read(PROPOSAL);raw=Path(p['input_file']).read_bytes()
    admitted=read(ROOT/'model-input.json');log_density=signal.log_values(admitted)
    completion=admitted['completion_times']
    rows=execution.load_rows('.artifacts/frozen-history-transfer-20261001/replay.csv')
    start,end=map(execution.millis,p['training_period_left_closed'])
    features=signal.features(rows,log_density,start,end,completion_times=completion)
    save(ROOT/'train-feature-audit.json',independent.features(raw,rows,features,start,end,3))
    for i,params in enumerate(sum(signal.grids(),[])):
        training.append(one('train-%02d'%i,rows,features,params,start,end))
    selection=signal.training_decision(training)
    save(ROOT/'selection.json',selection)
    save(ROOT/'selection-audit.json',rule_audit.selection(training,selection))
    if selection['status']!='TRAINING_ADMITTED':
        decision=dict(status=selection['status'],reasons=['NO_ELIGIBLE_TRAINING_CANDIDATE'] if
                      selection['status']=='NO_GO_TRAIN_RISK' else ['SELECTED_CANDIDATE_EVENT_CAPACITY'])
    else:
        specs=signal.specifications(selection,training)
        save(ROOT/'evaluation-freeze.json',dict(specifications=specs,
            training_sha256={str(ROOT/('train-%02d.json'%i)):sha(ROOT/('train-%02d.json'%i)) for i in range(8)},
            selection_sha256=sha(ROOT/'selection.json'),model_freeze_sha256=sha(ROOT/'model-freeze.json'),
            frozen_utc=dt.datetime.now(dt.timezone.utc).isoformat()))
        del rows,features
        rows=execution.load_rows('.artifacts/mvp-reference-history-20260921/prepared/replay.csv')
        start,end=map(execution.millis,p['evaluation_period_left_closed'])
        features={lag:signal.features(rows,log_density,start,end,lag,completion_times=completion) for lag in (3,4)}
        for lag,fs in features.items():
            save(ROOT/('eval-feature-lag%d-audit.json'%lag),independent.features(raw,rows,fs,start,end,lag))
        for name,params,lag,kw in specs:
            paths.append(one('eval-'+name,rows,features[lag],params,start,end,**kw))
        decision=signal.verdict(paths)
        save(ROOT/'decision-audit.json',rule_audit.decision(paths,decision))
    budget()
    result=dict(status='BATCH_COMPLETED',decision=decision,selection=selection,
        training_paths=len(training),evaluation_paths=len(paths),independent_books=2*len(audited),
        compute_seconds=time.monotonic()-started,protected_files=preserve(),
        completed_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        source_freeze_sha256=sha(ROOT/'model-freeze.json'),market_attempts=1,new_input_gets=2,
        account_operations=0,deployments=0,point_in_time_certified=False,independent_confirmation=False,qualified_candidates=0,g4=False)
    save(ROOT/'result.json',result);print(json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__':{'freeze':freeze,'execute':execute}[sys.argv[1]]()
