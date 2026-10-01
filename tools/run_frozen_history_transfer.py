"""Bounded public inputs and one frozen historical-transfer attempt, no accounts."""
import argparse
import csv
import datetime as dt
from decimal import Decimal
import io
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

import plan_frozen_history_intake as prep
from collect_mvp_reference_history import opener
from mvp_reference_inputs import FIELDS, candle, strict_json
from run_bounded_learning import need, sha, save, now
from run_offline_policy_correction import samples, BINARY, SOURCES as OLD_SOURCES
from frozen_history_transfer import replay, concentration, verdict

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'.artifacts/frozen-history-transfer-20261001'
PRIOR=ROOT/'.artifacts/frozen-confirmation-20260930'
CONTRACT=ROOT/'docs/plans/2026-10-01-frozen-history-transfer.contract.json'
PLAN=ROOT/'docs/plans/2026-10-01-frozen-history-transfer.md'
SOURCES=tuple(dict.fromkeys(OLD_SOURCES+('tools/offline_diagnostic_safety.py',
    'tools/frozen_history_transfer.py','tools/run_frozen_history_transfer.py',
    'tools/audit_frozen_history_transfer.py','tools/test_frozen_history_transfer.py',
    'tools/plan_frozen_history_intake.py','tools/collect_mvp_reference_history.py',
    'tools/mvp_reference_inputs.py','tools/audit_offline_diagnostic_safety.py',
    'tools/test_offline_diagnostic_safety.py')))


def check():
    c=strict_json(CONTRACT.read_bytes());f=strict_json((RUN/'freeze.json').read_bytes())
    need(sha(CONTRACT)==f['contract_sha256'] and sha(PLAN)==f['plan_sha256'],'CONTRACT_CHANGED')
    need(all(sha(ROOT/p)==h for p,h in f['sources'].items()),'SOURCE_CHANGED')
    need(sha(prep.MODEL)==c['model_sha256']==f['model_sha256'] and sha(BINARY)==f['binary_sha256'],'MODEL_OR_BINARY_CHANGED')
    elapsed=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(c['started_utc'])).total_seconds()
    need(0<=elapsed<c['maximum_hours']*3600,'STAGE_DEADLINE')
    gate=strict_json((RUN/'validation-state.json').read_bytes())
    need(gate['status']=='RUNNING','FORMAL_GATE_REQUIRED')
    return c,f


def preservation():
    p=strict_json((RUN/'baseline.json').read_bytes())['preserved']
    need(all(sha(ROOT/k)==v for k,v in p.items()),'OLD_EVIDENCE_CHANGED')
    return len(p)


def freeze():
    c=strict_json(CONTRACT.read_bytes())
    need(c['provenance']['user_answer']=='没有' and c['provenance']['external_usage']==
         'USER_DECLARED_NOT_USED_FOR_THIS_PROJECT','USAGE_FACT_REQUIRED')
    need(sha(prep.MODEL)==prep.MODEL_SHA==c['model_sha256'],'FROZEN_MODEL')
    preparation=PRIOR/'input-preparation-20261001.json'
    need(sha(preparation)==c['preparation_sha256'],'PREPARATION_CHANGED')
    prepared=strict_json(preparation.read_bytes())
    prep.validate_request_set(prepared['requests'])
    need(prepared['planned_new_gets']==c['planned_api_gets']==216,'GET_COUNT')
    prior=strict_json((PRIOR/'baseline.json').read_bytes())['preserved']
    need(all(sha(ROOT/p)==h for p,h in prior.items()),'PRIOR_CHANGED')
    paths=set(prior)
    paths.update(str(p.relative_to(ROOT)) for p in PRIOR.iterdir() if p.is_file() and p.suffix!='.lock')
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('*/2026-10-01-frozen-*')
                 if p not in (CONTRACT,PLAN))
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('*/2026-09-30-frozen-*'))
    paths.update(('tools/plan_frozen_history_intake.py','tools/test_plan_frozen_history_intake.py'))
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT/'.artifacts').glob('*/validation-state.json') if p.parent!=RUN)
    save(RUN/'baseline.json',dict(preserved={p:sha(ROOT/p) for p in sorted(paths)}))
    save(RUN/'freeze.json',dict(contract_sha256=sha(CONTRACT),plan_sha256=sha(PLAN),
        sources={p:sha(ROOT/p) for p in SOURCES},binary_sha256=sha(BINARY),
        model_sha256=sha(prep.MODEL),preparation_sha256=sha(preparation),
        provenance=c['provenance'],frozen_utc=now(),document_reads=3))
    for folder in ('attempts','receipts','raw','pages'):(RUN/folder).mkdir(exist_ok=False)
    check()
    print('FROZEN',preservation(),'old files; model fixed; requests=216; economic_attempts<=1')


def collect():
    c,_=check();preservation()
    attempts=list((RUN/'attempts').glob('*.json'))
    if attempts:
        gate=strict_json((RUN/'validation-state.json').read_bytes())
        need(gate['active'].get('review_path'),'REVIEW_REQUIRED_BEFORE_REUSE')
        need(all((RUN/'receipts'/p.name).is_file() for p in attempts),'INTERRUPTED_ATTEMPT')
    else:
        save(RUN/'data-access-start.json',dict(at=now(),freeze_sha256=sha(RUN/'freeze.json'),
            prior_exposure_sha256=sha(PRIOR/'exposure.json'),known_probe_points=6,
            history_independence_basis=c['provenance']))
    transport=opener();records=[]
    total=sum(p.stat().st_size for p in (RUN/'raw').glob('*.json'))
    for index,req in enumerate(prep.canonical_requests()):
        check();cached=RUN/'pages'/f'{index:03d}.json'
        if cached.exists():
            r=strict_json(cached.read_bytes());raw=(RUN/'raw'/r['file']).read_bytes()
            need(r['url']==req['url'] and sha(RUN/'raw'/r['file'])==r['sha256'],'CACHE_CHANGED')
            prep.validate_page(raw,req,r['received_at_ms']);records.append(r);continue
        n=len(list((RUN/'attempts').glob('*.json')))+1
        need(n<=c['maximum_api_gets'],'GET_BUDGET')
        name=f'{n:04d}.json'
        save(RUN/'attempts'/name,dict(request_index=index,request=req,started_utc=now()))
        raw=b'';status=None;error=None
        try:
            request=urllib.request.Request(req['url'],method='GET',headers={'User-Agent':'ai-trade-frozen-transfer/1'})
            with transport.open(request,timeout=25) as response:
                status=response.status;raw=response.read(2*1024*1024+1)
        except urllib.error.HTTPError as exc:
            status=exc.code;raw=exc.read(2*1024*1024+1);error='HTTP_'+str(status)
        except Exception as exc:error=type(exc).__name__+': '+str(exc)
        received=int(time.time()*1000)
        with (RUN/'raw'/name).open('xb') as out:out.write(raw)
        total+=len(raw)
        record=dict(request_index=index,kind=req['kind'],url=req['url'],file=name,
            sha256=sha(RUN/'raw'/name),received_at_ms=received,http_status=status,error=error)
        if error is None:
            try:
                need(status==200 and total<=c['maximum_raw_bytes'],'HTTP_OR_BYTE_BUDGET')
                record['rows']=prep.validate_page(raw,req,received)
            except (ValueError,KeyError,TypeError) as exc:record['error']=str(exc)
        save(RUN/'receipts'/name,record)
        need(record['error'] is None,'PUBLIC_COLLECTION_STOP:'+str(record['error']))
        save(cached,record);records.append(record)
        if (index+1)%12==0 or index==215:
            print(json.dumps(dict(pages=index+1,total_pages=216,attempts=n,bytes=total)),flush=True)
    save(RUN/'manifest.json',dict(schema='frozen_transfer_public_archive_v1',
        freeze_sha256=sha(RUN/'freeze.json'),pages=records,total_raw_bytes=total,
        attempted_gets=len(list((RUN/'attempts').glob('*.json'))),completed_utc=now()))


def point(kind,row):
    if kind=='funding':return (int(row['fundingRateTimestamp']),row['symbol'],Decimal(row['fundingRate']))
    return (int(row[0]),)+tuple(Decimal(x) for x in row[1:])


def compile_pages(pages,read_raw):
    """Strict real-domain compiler; no synthetic override of the old contract."""
    requests=prep.canonical_requests()
    need(len(pages)==len(requests),'PAGE_COUNT')
    data={k:{} for k in ('trade','mark','funding')};seen=set();total=0
    import hashlib
    for index,(r,req) in enumerate(zip(pages,requests)):
        need(r['request_index']==index and r['kind']==req['kind'] and r['url']==req['url'],'PAGE_IDENTITY')
        need(r['http_status']==200 and r['error'] is None,'PAGE_ERROR')
        name=r['file']
        need(Path(name).name==name and name not in seen,'PAGE_FILE_ALIAS')
        seen.add(name);raw=read_raw(name);total+=len(raw)
        need(total<=256*1024*1024 and hashlib.sha256(raw).hexdigest()==r['sha256'],'RAW_HASH_OR_SIZE')
        count=prep.validate_page(raw,req,r['received_at_ms'])
        need(count==r['rows'],'ROW_RECEIPT')
        for row in strict_json(raw)['result']['list']:
            k=req['kind'];ts=point(k,row)[0]
            need(ts not in data[k],'DUPLICATE_TIMESTAMP')
            data[k][ts]=row
    start,evaluation,end=1609372800000,1609459200000,1640822400000
    for kind in data:
        step=28800000 if kind=='funding' else 300000
        need(sorted(data[kind])==list(range(start,end,step)),'FULL_GRID:'+kind)
    output=io.StringIO(newline='');writer=csv.DictWriter(output,fieldnames=FIELDS,lineterminator='\n');writer.writeheader()
    for ts,row in sorted(data['trade'].items()):
        _,(o,h,l,cl,v)=candle(row,'trade');_,(mo,mh,ml,mc,_)=candle(data['mark'][ts],'mark')
        rate=data['funding'][ts]['fundingRate'] if ts in data['funding'] else '0'
        writer.writerow(dict(zip(FIELDS,[ts,'BTCUSDT',o,h,l,cl,v,300000,rate,mo,mc,mh,ml,int(ts>=evaluation)])))
    return output.getvalue().encode(),data


def compile_input():
    c,_=check();preservation();m=strict_json((RUN/'manifest.json').read_bytes())
    need(m['freeze_sha256']==sha(RUN/'freeze.json'),'ARCHIVE_FREEZE')
    def read_raw(name):
        p=RUN/'raw'/name
        need(not p.is_symlink(),'RAW_SYMLINK')
        return p.read_bytes()
    raw,data=compile_pages(m['pages'],read_raw)
    for r in strict_json(prep.EVIDENCE.read_bytes())['probes']:
        prior=PRIOR/('raw-'+r['date']+'-'+r['kind']+'.json')
        need(sha(prior)==r['raw_sha256'],'PRIOR_PROBE_CHANGED')
        previous=strict_json(prior.read_bytes())['result']['list'][0]
        t=point(r['kind'],previous)[0]
        need(point(r['kind'],data[r['kind']][t])==point(r['kind'],previous),'UNEXPLAINED_PROBE_REVISION')
    with (RUN/'replay.csv').open('xb') as out:out.write(raw)
    save(RUN/'input-proof.json',dict(complete=True,synthetic=False,source_bars=len(data['trade']),
        mark_bars=len(data['mark']),funding_events=len(data['funding']),prior_probes_matched=6,
        manifest_sha256=sha(RUN/'manifest.json'),csv_sha256=sha(RUN/'replay.csv'),
        source_start_ms=c['source_start_ms'],evaluation_start_ms=c['evaluation_start_ms'],
        end_exclusive_ms=c['end_exclusive_ms'],qualification=False))
    print('INPUT_COMPLETE source_bars=104832 funding=1092 prior_probes=6; no returns yet')


def execute():
    c,_=check();preservation();proof=strict_json((RUN/'input-proof.json').read_bytes())
    need(proof['complete'] and not proof['synthetic'] and sha(RUN/'replay.csv')==proof['csv_sha256'],'INPUT_IDENTITY')
    save(RUN/'economic-attempt.json',dict(at=now(),maximum_attempts=1,model_sha256=c['model_sha256'],
        freeze_sha256=sha(RUN/'freeze.json'),input_proof_sha256=sha(RUN/'input-proof.json'),
        scope='FROZEN_HISTORICAL_REVERSE_TIME_TRANSFER',training_vectors=0))
    start=time.monotonic();deadline=start+c['maximum_compute_seconds']
    domain=samples(RUN/'replay.csv',c['evaluation_start_ms'],c['end_exclusive_ms'],BINARY,deadline)
    groups={}
    for name,vector in (('learned',c['coefficients']),('fixed',c['baseline'])):
        path=RUN/(name+'.jsonl')
        with path.open('x') as out:
            def emit(event):
                need(time.monotonic()<deadline,'COMPUTE_BUDGET')
                out.write(json.dumps(event,allow_nan=False)+'\n')
            result=replay(domain,vector,emit)
        with path.open() as source:
            clustered=concentration((json.loads(line) for line in source),result)
        groups[name]=dict(result,trace_file=path.name,trace_sha256=sha(path))
        save(RUN/(name+'-concentration.json'),clustered)
        print(json.dumps(dict(group=name,reason=result['reason'],net=[b['cash']-10000 for b in result['wallets']],
            active_bars=result['active_bars'],episodes=clustered['episode_count'],clusters=clustered['separated_clusters'])),flush=True)
    decision,bounds=verdict(groups['learned'],groups['fixed'],strict_json((RUN/'learned-concentration.json').read_bytes()))
    need(time.monotonic()<deadline,'COMPUTE_BUDGET')
    save(RUN/'result.json',dict(schema='frozen_history_transfer_result_v1',verdict=decision,
        scope='FROZEN_HISTORICAL_REVERSE_TIME_TRANSFER',**groups,hac_lower=bounds,
        compute_seconds=time.monotonic()-start,completed_utc=now(),model_sha256=c['model_sha256'],
        input_proof_sha256=sha(RUN/'input-proof.json'),economic_attempts=1,training_vectors=0,
        historical_confirmation_attempts=1,prospective_confirmation_attempts=0,
        candidate_status='NO_QUALIFIED_CANDIDATE',qualification=False))
    print('VERDICT',decision,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('freeze','collect','compile','run','preservation'))
    action=parser.parse_args().action
    {'freeze':freeze,'collect':collect,'compile':compile_input,'run':execute,
     'preservation':lambda:print('PRESERVED',preservation())}[action]()
