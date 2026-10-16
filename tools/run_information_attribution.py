"""One fixed 3 x 3 information diagnostic; no strategy replay, network or trading."""
import datetime as dt
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys
import time

import information_attribution as diagnostic
import audit_information_attribution as audit
import btc_old_output_model as old
import btc_fee_pressure as fee
import btc_address_participation as address
import audit_btc_fee_pressure as fee_audit
import audit_btc_address_participation as address_audit

ROOT=Path('.artifacts/information-attribution-20261016')
OLD=Path('.artifacts/data-first-mechanisms-20261016')
PLAN=Path('docs/plans/2026-10-16-information-attribution.md')
SOURCE_PATHS=[Path('tools')/name for name in ('information_attribution.py','audit_information_attribution.py',
    'test_information_attribution.py','run_information_attribution.py','btc_old_output_model.py',
    'btc_fee_pressure.py','btc_address_participation.py','audit_btc_fee_pressure.py','audit_btc_address_participation.py')]
DOMAINS={'2021':('2021-01-11','2021-12-30'), '2025':('2025-01-13','2026-01-01')}
PRICES={'2021':Path('.artifacts/frozen-history-transfer-20261001/replay.csv'),
        '2025':Path('.artifacts/mvp-reference-history-20260921/prepared/replay.csv')}


def read(p): return json.loads(Path(p).read_text())
def sha(p):
    with Path(p).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,v):
    with Path(p).open('x') as f: json.dump(v,f,indent=2,sort_keys=True,allow_nan=False);f.write('\n')
def guard():
    diagnostic.require(read(ROOT/'validation-state.json')['status']=='RUNNING','NEW_GATE_RUNNING_REQUIRED')
    diagnostic.require(dt.datetime.now(dt.timezone.utc)<dt.datetime(2026,10,16,16,15,tzinfo=dt.timezone.utc),'TIME_BUDGET')
def preserve():
    scope=read(ROOT/'freeze.json')
    diagnostic.require(sha(PLAN)==scope['plan_sha256'],'CONTRACT_CHANGED')
    diagnostic.require(all(sha(p)==h for p,h in scope['files'].items()),'OLD_EVIDENCE_CHANGED')
    return len(scope['files'])


def freeze():
    guard();preserve(); inputs={}
    for folder in ('.artifacts/btc-old-output-direction-20261015','.artifacts/btc-fee-pressure-20261016',str(OLD)):
        path=Path(folder)/'model-freeze.json'; frozen=read(path)
        diagnostic.require(all(sha(p)==h for p,h in dict(frozen['inputs'],**frozen['sources']).items()),'LEGACY_IDENTITY')
        inputs.update(frozen['inputs']);inputs[str(path)]=sha(path)
    inputs[str(PLAN)]=sha(PLAN)
    save(ROOT/'model-freeze.json',dict(inputs=inputs,sources={str(p):sha(p) for p in SOURCE_PATHS},
        frozen_utc=dt.datetime.now(dt.timezone.utc).isoformat(),cells=9,folds=2,maximum_fits=54,
        ridge=diagnostic.RIDGE,horizons_days=list(diagnostic.HORIZONS),diagnostic_attempts=1))
    print('DIAGNOSTIC_FROZEN_NO_TRADING_PATHS')


def old_feature_audit(ratios,features):
    for t,f in features.items():
        d=dt.date.fromisoformat(f['source_day'])
        history=[D(ratios[str(d-dt.timedelta(days=i))]) for i in range(1,29)]
        mu=sum(history)/28; sd=(sum((v-mu)**2 for v in history)/28).sqrt()
        expected=max(D(-3),min(D(3),(D(ratios[str(d)])-mu)/sd)) if sd>=D('.000001') else None
        if expected is None: diagnostic.require(f['z'] is None,'OLD_FEATURE_CONSTANT')
        else: audit.close(f['z'],expected,'OLD_FEATURE')


def execute():
    guard();preserve(); frozen=read(ROOT/'model-freeze.json')
    diagnostic.require(all(sha(p)==h for p,h in dict(frozen['inputs'],**frozen['sources']).items()),'FROZEN_IDENTITY')
    save(ROOT/'diagnostic-attempt.json',dict(started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        model_freeze_sha256=sha(ROOT/'model-freeze.json'),trading_paths=0))
    started=time.monotonic()
    def budget():
        guard();diagnostic.require(time.monotonic()-started<600,'COMPUTE_BUDGET')
        diagnostic.require(sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())<64*1024*1024,'OUTPUT_BUDGET')
    ratios=read('.artifacts/btc-old-output-direction-20261015/ratios.json')
    density=read('.artifacts/btc-fee-pressure-20261016/log-density.json')
    admitted=read(OLD/'model-input.json'); address_logs=address.log_values(admitted)
    data={}; feature_receipts=[]
    for year,price_path in PRICES.items():
        budget();rows=old.load_rows(price_path);start,end=map(old.millis,DOMAINS[year])
        closes={r['timestamp']+old.STEP:r['price'] for r in rows}
        fs={'old_output':old.features(rows,ratios,start,end), 'fee_pressure':fee.features(rows,density,start,end),
            'address':address.features(rows,address_logs,start,end,completion_times=admitted['completion_times'])}
        old_feature_audit(ratios,fs['old_output'])
        feature_receipts.append(fee_audit.features(
            Path('.artifacts/btc-fee-pressure-design-20261015/request-02.body').read_bytes(),
            Path('.artifacts/btc-fee-pressure-design-20261015/request-04.body').read_bytes(),rows,fs['fee_pressure'],start,end,3))
        feature_receipts.append(address_audit.features((OLD/'get-02.body').read_bytes(),rows,fs['address'],start,end,3))
        data[year]={}
        for name,features in fs.items():
            data[year][name]={}
            for horizon in diagnostic.HORIZONS:
                built=diagnostic.build_samples(closes,features,start,end,horizon)
                audit.samples(built['rows'],closes,horizon)
                data[year][name][horizon]=built
    save(ROOT/'input-feature-audit.json',dict(status='LEGACY_FEATURES_AND_NEW_PRICE_LABELS_VERIFIED',
        decimal_receipts=feature_receipts,old_output_feature_decisions=706,point_in_time_certified=False))
    start21,end21=map(old.millis,DOMAINS['2021']);start25,end25=map(old.millis,DOMAINS['2025'])
    mid21=old.millis('2021-07-01');mid25=old.millis('2025-07-01')
    cells=[];fit_count=0;prediction_count=0
    for name in ('old_output','fee_pressure','address'):
        for horizon in diagnostic.HORIZONS:
            budget();r21=data['2021'][name][horizon]['rows'];r25=data['2025'][name][horizon]['rows']
            folds=[]
            for fold_name,train,evaluation in (
                ('2021_h2',diagnostic.window(r21,start21,mid21),diagnostic.window(r21,mid21,end21)),
                ('2025',r21,r25)):
                result=diagnostic.compare(train,evaluation)
                if result['models']:
                    receipt=audit.regression(train,evaluation,result['models'],result['predictions'])
                    audit.metrics(result['predictions'],result['metrics'])
                    fit_count+=receipt['fits'];prediction_count+=receipt['predictions']
                else: receipt=dict(status='INSUFFICIENT_MATCHED_ROWS')
                save(ROOT/('%s-%dd-%s.json'%(name,horizon,fold_name)),dict(
                    train=train,evaluation=evaluation,result=result,audit=receipt))
                folds.append(result)
            stage1=folds[0]['metrics']
            stage2=diagnostic.metrics(diagnostic.window(folds[1]['predictions'],start25,mid25))
            stage3=diagnostic.metrics(diagnostic.window(folds[1]['predictions'],mid25,end25))
            for predictions,stat in ((diagnostic.window(folds[1]['predictions'],start25,mid25),stage2),
                                     (diagnostic.window(folds[1]['predictions'],mid25,end25),stage3)):
                audit.metrics(predictions,stat)
            stages=[stage1,stage2,stage3];decision=diagnostic.classify(stages);audit.classification(stages,decision)
            cell=dict(signal=name,horizon_days=horizon,status=decision,
                training_rows=[f['train_rows'] for f in folds],
                samples={year:{k:len(data[year][name][horizon][k]) for k in ('rows','unavailable_dates','unmatured_dates')}
                         for year in DOMAINS},
                stages=dict(zip(('2021_h2','2025_h1','2025_h2'),stages)),full_2025=folds[1]['metrics'],
                standardized_signal_coefficients_bps=[f['models']['increment']['coefficients'][-1]
                    if f['models'] else None for f in folds],
                training_date_sha256=[f.get('train_date_sha256') for f in folds])
            cells.append(cell);print(json.dumps(dict(signal=name,horizon_days=horizon,status=decision,
                improvements=[s.get('increment_mse_reduction_percent') for s in stages])),flush=True)
    budget()
    result=dict(status='METHOD_DIAGNOSTIC_COMPLETE',cells=cells,independently_audited_fits=fit_count,
        audited_predictions=prediction_count,diagnostic_attempts=1,compute_seconds=time.monotonic()-started,
        protected_files=preserve(),model_freeze_sha256=sha(ROOT/'model-freeze.json'),
        descriptive_consistent_cells=sum(c['status']=='DESCRIPTIVE_INCREMENT_ONLY' for c in cells),
        limitations=['ALREADY_SEEN_DEVELOPMENT','RECONSTRUCTED_NOT_PIT','LINEAR_FIXED_RIDGE_ONLY',
            'ONE_PERTURBATION_NOT_A_RANDOMIZATION_TEST','OVERLAPPING_LABELS_HAC_IS_DESCRIPTIVE',
            'RIDGE_GAIN_MAY_INCLUDE_REGULARIZATION_EFFECT_NOT_CAUSAL_INFORMATION','NO_COST_OR_RISK_PAYOFF_TEST'],
        confirmation_exit='NOT_ADMITTED_NO_INDEPENDENT_SOURCE_OR_POWER_BOUND',
        new_data_gets=0,deployments=0,account_operations=0,
        completed_utc=dt.datetime.now(dt.timezone.utc).isoformat(),**diagnostic.boundaries())
    save(ROOT/'result.json',result);print(json.dumps({k:v for k,v in result.items() if k!='cells'},sort_keys=True))


if __name__=='__main__': {'freeze':freeze,'execute':execute}[sys.argv[1]]()
