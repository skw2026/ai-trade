"""One authorized development batch, local immutable inputs only."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import time

import btc_old_output_model as model
from audit_btc_old_output import audit

ROOT=Path('.artifacts/btc-old-output-direction-20261015')
PLAN=Path('docs/plans/2026-10-15-btc-old-output-direction.proposal.json')
SOURCES=[Path('tools')/s for s in ('btc_old_output_input.py','btc_old_output_model.py',
    'audit_btc_old_output.py','run_btc_old_output.py','test_btc_old_output_input.py',
    'test_btc_old_output_model.py','test_btc_old_output_audit.py','test_btc_old_output_decision.py')]

def read(p):return json.loads(Path(p).read_text())
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,x):
    with Path(p).open('x') as f:json.dump(x,f,indent=2,sort_keys=True,allow_nan=False);f.write('\n')
def guard():
    model.need(read(ROOT/'validation-state.json')['status']=='RUNNING','NEW_GATE_RUNNING_REQUIRED')
    model.need(dt.datetime.now(dt.timezone.utc)<dt.datetime(2026,10,15,15,10,2,tzinfo=dt.timezone.utc),'TIME_BUDGET')
def preserve():
    f=read(ROOT/'freeze.json')
    model.need(all(sha(p)==h for p,h in f['protected'].items()),'OLD_EVIDENCE_CHANGED')
    model.need(sha('docs/plans/2026-10-15-btc-old-output-execution.md')==f['execution_sha256'],'ACCEPTANCE_CHANGED')
    return len(f['protected'])
def freeze():
    guard();preserve()
    a=read(ROOT/'input-audit.json')
    model.need(a['status']=='DEVELOPMENT_INPUT_ADMITTED' and a['api_gets']==5,'INPUT_NOT_ADMITTED')
    inputs=dict(read(PLAN)['price_inputs'])
    for p in (ROOT/'input-audit.json',ROOT/'ratios.json',PLAN):inputs[str(p)]=sha(p)
    model.need(all(sha(p)==h for p,h in inputs.items()),'INPUT_IDENTITY')
    save(ROOT/'model-freeze.json',dict(sources={str(p):sha(p) for p in SOURCES},inputs=inputs,
        frozen_utc=dt.datetime.now(dt.timezone.utc).isoformat(),market_attempts=0))
    print('MODEL_FROZEN_BEFORE_MARKET_RESULTS')
def execute():
    guard();preserve();f=read(ROOT/'model-freeze.json')
    model.need(all(sha(p)==h for p,h in dict(f['sources'],**f['inputs']).items()),'FROZEN_IDENTITY')
    save(ROOT/'market-attempt.json',dict(model_freeze_sha256=sha(ROOT/'model-freeze.json'),
         started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),maximum_compute_seconds=1800))
    started=time.monotonic();ratios=read(ROOT/'ratios.json');audits=[];training=[]
    def budget():
        guard();model.need(time.monotonic()-started<1800,'COMPUTE_BUDGET')
        model.need(sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())<256*1024*1024,'EVIDENCE_BUDGET')
    def one(name,rows,fs,params,start,end,**kw):
        budget();result=model.simulate(rows,fs,params,start,end,**kw)
        save(ROOT/(name+'.json'),result)
        checked=audit(result,rows);save(ROOT/(name+'-audit.json'),checked);audits.append(name)
        budget()
        print(json.dumps(dict(path=name,parameters=params,net=[b['net'] for b in result['books']],
             stopped=[b['stopped'] for b in result['books']],insufficient=[b['insufficient'] for b in result['books']])),flush=True)
        model.need(not any(b['insufficient'] for b in result['books']),'EXECUTION_INSUFFICIENT_STOP_DEPENDENCIES')
        return result
    p=read(PLAN)
    rows=model.load_rows(next(iter(p['price_inputs'])))
    start,end=map(model.millis,p['training_period']);fs=model.features(rows,ratios,start,end)
    mechanisms,prices=model.grids()
    for i,params in enumerate(mechanisms+prices):
        training.append(one('train-%02d'%i,rows,fs,params,start,end))
    m=model.select(training[:12]);r=model.select(training[12:])
    selection=dict(mechanism_index=m,price_index=None if r is None else r+12,
        parameters=None if m is None else training[m]['parameters'])
    save(ROOT/'selection.json',selection)
    paths=[]
    if m is None or r is None:
        decision=dict(status='NO_GO_RISK',reasons=['NO_ELIGIBLE_TRAINING_PATH_IN_A_REQUIRED_GROUP'])
    else:
        rows=model.load_rows(list(p['price_inputs'])[1]);start,end=map(model.millis,p['evaluation_period'])
        fs=model.features(rows,ratios,start,end);fixed_index=mechanisms.index((-1,0,1))
        specs=[('candidate',training[m]['parameters'],{}),
            ('fixed',[-1,0,1],dict(initially_stopped=not model.eligible(training[fixed_index]))),
            ('price',training[r+12]['parameters'],{}),('passive',[0,1,1],dict(passive=True)),
            ('chain-delay',training[m]['parameters'],{}),('execution-delay',training[m]['parameters'],dict(latency=2))]
        for name,params,kw in specs:
            features=model.features(rows,ratios,start,end,lag_days=4) if name=='chain-delay' else fs
            paths.append(one('eval-'+name,rows,features,params,start,end,**kw))
        decision=model.verdict(paths)
    result=dict(status='BATCH_COMPLETED',decision=decision,selection=selection,
        training_paths=len(training),evaluation_paths=len(paths),independent_books=2*len(audits),
        compute_seconds=time.monotonic()-started,protected_files=preserve(),
        source_freeze_sha256=sha(ROOT/'model-freeze.json'),new_price_gets=0,api_gets=5,
        account_operations=0,deployments=0,market_attempts=1,independent_confirmation=False,
        qualified_candidates=0,g4=False)
    save(ROOT/'market-result.json',result);print(json.dumps(result,sort_keys=True),flush=True)

if __name__=='__main__':
    {'freeze':freeze,'execute':execute}[sys.argv[1]]()
