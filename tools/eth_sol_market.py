"""Fixed ETH/SOL input archive, before economics; no accounts or order APIs."""
import argparse
from array import array
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode

import eth_sol_intake as intake
from aave_market_recovery import unpack, quota_wait, effective_record
from mvp_reference_inputs import candle, number, strict_json, timestamp

ROOT = intake.ROOT
CONTRACT = Path('docs/plans/2026-10-15-eth-sol-economic.contract.json')
OLD = Path('.artifacts/aave-economic-screen-20261013')
RECOVERED = Path('.artifacts/aave-market-recovery-20261013')
MINUTE = 60000
need, sha, save = intake.need, intake.sha, intake.save


def ms(value):
    return int(dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1000)


def sources():
    return {p:sha(p) for p in ('tools/eth_sol_market.py','tools/test_eth_sol_market.py')}


def verify_freeze(f):
    need(f['contract_sha256']==sha(CONTRACT),'ECONOMIC_CONTRACT_CHANGED')
    if f['sources']==sources():return
    correction=strict_json((ROOT/'reuse-correction.json').read_bytes())
    need(correction['scope']=='REUSE_EXISTING_HASH_BOUND_INTERPRETATION' and
        correction['old_sources']==f['sources'] and correction['new_sources']==sources(),'REUSE_CORRECTION_SCOPE')
    need(set(correction['new_sources'])=={'tools/eth_sol_market.py','tools/test_eth_sol_market.py'},'REUSE_CORRECTION_FILES')
    need(all(sha(p)==h for p,h in correction['originals'].items()),'ORIGINAL_INPUT_SOURCE_CHANGED')
    review=Path(correction['review_path']);record=strict_json(review.read_bytes())
    need(record['failure_id']==correction['failure_id'] and record['correction_manifest_sha256']==sha(ROOT/'reuse-correction.json'),'REUSE_REVIEW_BINDING')
    state=strict_json((ROOT/'validation-state.json').read_bytes())
    need(any(e.get('event')=='review' and e.get('decision')=='retry' and
        e.get('failure_id')==correction['failure_id'] and e.get('review_sha256')==sha(review)
        for e in state['history']),'REUSE_CORRECTION_NOT_REVIEWED')


def guard():
    intake.guard()
    c = strict_json(CONTRACT.read_bytes())
    if (ROOT/'market-freeze.json').exists():
        f = strict_json((ROOT/'market-freeze.json').read_bytes())
        verify_freeze(f)
    return c


def plan(c):
    start, end = ms(c['formation_start_utc'])-60*MINUTE, ms(c['evaluation_end_utc'])
    requests = []
    def append(symbol,kind,begin,stop,interval):
        funding = kind=='funding'
        endpoint = {'trade':'kline','mark':'mark-price-kline','funding':'funding/history'}[kind]
        limit = 200 if funding else 1000
        params = dict(category='linear',symbol=symbol,limit=limit)
        params.update({'startTime' if funding else 'start':begin,'endTime' if funding else 'end':stop-1})
        if not funding: params['interval']=str(interval)
        requests.append(dict(symbol=symbol,kind=kind,start=begin,end=stop-1,limit=limit,interval_minutes=interval,
            url='https://api.bybit.com/v5/market/'+endpoint+'?'+urlencode(params)))
    for begin in range(ms(c['formation_start_utc']),end,7*1440*MINUTE):
        append('SOLUSDT','funding',begin,min(end,begin+7*1440*MINUTE),0)
    for begin in range(start,end,1000*MINUTE):
        for kind in ('trade','mark'):
            append('SOLUSDT',kind,begin,min(end,begin+1000*MINUTE),1)
    for symbol in intake.SYMBOLS:
        for begin in range(ms(c['warmup_start_utc']),ms(c['formation_start_utc']),1000*60*MINUTE):
            append(symbol,'trade',begin,min(ms(c['formation_start_utc']),begin+1000*60*MINUTE),60)
    # Identity controls are followed by one fixed page for each input, not an economic selection.
    controls = []
    for key in [('SOLUSDT','funding',0),('SOLUSDT','trade',1),('SOLUSDT','mark',1),
                ('ETHUSDT','trade',60),('SOLUSDT','trade',60)]:
        controls.append(next(r for r in requests if (r['symbol'],r['kind'],r['interval_minutes'])==key))
    return controls+[r for r in requests if r not in controls]


def validate(raw,r):
    obj = strict_json(raw)
    need(type(obj.get('retCode')) is int and obj['retCode']==0,'MARKET_API_ERROR')
    result = obj['result']
    need(result['category']=='linear' and timestamp(obj['time'])>r['end'],'MARKET_IDENTITY_CLOCK')
    funding = r['kind']=='funding'
    if not funding: need(result['symbol']==r['symbol'],'MARKET_SYMBOL')
    rows = result['list']; need(isinstance(rows,list),'MARKET_LIST')
    normalized = []
    for row in rows:
        if funding:
            need(row['symbol']==r['symbol'],'FUNDING_SYMBOL')
            t,values = timestamp(row['fundingRateTimestamp']), str(number(row['fundingRate']))
            need(t%MINUTE==0,'UNSUPPORTED_SUBMINUTE_FUNDING')
        else:
            t,values = candle(row,r['kind'])
        normalized.append((t,values))
    times = [t for t,_ in normalized]
    need(times==sorted(set(times),reverse=True),'MARKET_DUPLICATE_ORDER')
    need(all(r['start']<=t<=r['end'] for t in times),'MARKET_RANGE')
    if funding:
        need(0<len(times)<r['limit'],'FUNDING_EMPTY_OR_SATURATED')
    else:
        need(times==list(range(r['start'],r['end']+1,r['interval_minutes']*MINUTE))[::-1], 'MARKET_GRID')
        need(len(times)<=r['limit'],'MARKET_LIMIT')
    return normalized


def freeze():
    os.umask(0o077)
    c=guard(); intake.preserve()
    need((ROOT/'identities.json').exists(),'IDENTITIES_REQUIRED')
    r=plan(c); need(len(r)+2+30<=1500,'REQUEST_BUDGET')
    old= strict_json((OLD/'freeze.json').read_bytes())
    recovered= strict_json((RECOVERED/'freeze.json').read_bytes())
    collection= strict_json((RECOVERED/'collection.json').read_bytes())
    existing={int(k):v for k,v in recovered['existing_records'].items()}
    need(not set(existing)&{int(k) for k in collection['records']},'REUSED_PAGE_OVERLAP')
    existing.update({int(k):v['folder'] for k,v in collection['records'].items()})
    need(set(existing)==set(range(len(old['requests']))),'REUSED_ARCHIVE_INCOMPLETE')
    (ROOT/'market-attempts').mkdir(mode=0o700)
    save(ROOT/'market-freeze.json',dict(contract_sha256=sha(CONTRACT),sources=sources(),requests=r,
        reuse_requests=old['requests'],reuse_folders=existing,
        frozen_utc=dt.datetime.now(dt.timezone.utc).isoformat(),prices_evaluated=0,
        control_count=5, historical_filters_verified=False))
    print(json.dumps(dict(status='ECONOMICS_AND_INPUT_FROZEN',new_pages=len(r),reused_pages=len(existing),
        protected_files=intake.preserve(),economic_attempts=0)),flush=True)


def interpret(wire,rc,r,c,page_attempt,received_ms):
    need(rc==0,'UNKNOWN_TRANSPORT_FAILURE')
    cfg=dict(maximum_body_bytes=c['body_byte_limit'],maximum_header_bytes=c['header_byte_limit'])
    http,headers,_,body=unpack(wire,cfg)
    need(http==200,'UNEXPECTED_HTTP_STATUS')
    obj=strict_json(body); code=obj.get('retCode')
    need(type(code) is int,'BUSINESS_CODE_TYPE')
    if code==0:
        rows=validate(body,r)
        return dict(status='VALID',http=http,code=code,rows=len(rows),
            wait_seconds=quota_wait(headers,obj.get('time'),received_ms,c))
    need(code in (10016,10006),'UNEXPECTED_BUSINESS_ERROR')
    need(page_attempt<c['maximum_page_attempts'],'PAGE_RECOVERY_EXHAUSTED')
    delay=2.0 if code==10016 else quota_wait(headers,obj.get('time'),received_ms,c,mandatory=True)
    return dict(status='RECOVERABLE_NOT_VALID',http=http,code=code,wait_seconds=delay)


def collect():
    os.umask(0o077)
    c=guard()['input']; intake.preserve()
    f=strict_json((ROOT/'market-freeze.json').read_bytes())
    need(not any((ROOT/'market-attempts').iterdir()),'MARKET_COLLECTION_ALREADY_STARTED')
    counts=dict(attempts=2,bytes=sum(p.stat().st_size for p in (ROOT/'identity-attempts').glob('*/response.bin')),
        known_errors=0,wait_seconds=0.0)
    records={}
    for i,r in enumerate(f['requests']):
        for page_attempt in range(1,c['maximum_page_attempts']+1):
            guard()
            need(counts['attempts']<c['total_new_attempts_including_identity'],'ATTEMPT_BUDGET')
            need(counts['bytes']+c['body_byte_limit']+c['header_byte_limit']+32<=c['total_new_wire_bytes'],'BYTE_BUDGET')
            folder=ROOT/'market-attempts'/f'{counts["attempts"]-2:04d}'
            folder.mkdir(mode=0o700)
            save(folder/'started.json',dict(index=i,request=r,page_attempt=page_attempt,
                at=dt.datetime.now(dt.timezone.utc).isoformat()))
            counts['attempts']+=1; wire=b''; record=dict(status='FAILED',index=i,request=r,page_attempt=page_attempt)
            start=time.monotonic()
            try:
                p=subprocess.run(['curl','--disable','--silent','--proto','=https','--include','--suppress-connect-headers',
                    '--retry','0','--connect-timeout','15','--max-time','30','--max-filesize',str(c['body_byte_limit']),
                    '--write-out','\n%{http_code}',r['url']],capture_output=True,timeout=35)
                wire=p.stdout; record['curl_returncode']=p.returncode
                record.update(interpret(wire,p.returncode,r,c,page_attempt,int(time.time()*1000)))
                counts['known_errors']+=record['status']!='VALID'
                counts['wait_seconds']+=record['wait_seconds']
                need(counts['known_errors']<=c['maximum_known_error_responses'],'KNOWN_ERROR_BUDGET')
                need(counts['wait_seconds']<=c['maximum_total_wait_seconds'],'WAIT_BUDGET')
            except Exception as exc:
                if isinstance(exc,subprocess.TimeoutExpired): wire=exc.stdout or b''
                record.update(status='FAILED',error_type=type(exc).__name__,error=str(exc)[:180])
                raise
            finally:
                with (folder/'response.bin').open('xb') as out: out.write(wire)
                counts['bytes']+=len(wire)
                record.update(wire_sha256=hashlib.sha256(wire).hexdigest(),bytes=len(wire),elapsed_seconds=time.monotonic()-start)
                save(folder/'record.json',record)
            delay=max(c['minimum_request_spacing_seconds'],record['wait_seconds'])
            until=time.monotonic()+delay
            while time.monotonic()<until:
                guard(); time.sleep(min(1,max(0,until-time.monotonic())))
            if record['status']=='VALID':
                records[str(i)]=dict(folder=str(folder),wire_sha256=record['wire_sha256']);break
        if i<5 or (i+1)%50==0 or i+1==len(f['requests']):
            print(json.dumps(dict(status='INPUT_COLLECTION',pages=i+1,total=len(f['requests']),**counts)),flush=True)
    save(ROOT/'market-collection.json',dict(status='COLLECTED_NOT_COMPILED',records=records,**counts))
    intake.preserve()


def raw_page(folder,request,reused=False):
    folder=Path(folder); record=strict_json((folder/'record.json').read_bytes())
    if reused and folder.parent==RECOVERED/'attempts':
        record=effective_record(folder)
    need(record['status']=='VALID','REUSED_OR_NEW_PAGE_NOT_VALID')
    need(record['request']==request,'PAGE_REQUEST_BINDING')
    if (folder/'response.bin').exists():
        raw=(folder/'response.bin').read_bytes(); need(sha(folder/'response.bin')==record['wire_sha256'],'WIRE_HASH')
        status,_,_,body=unpack(raw,intake.WIRE); need(status==200,'REUSED_HTTP')
    else:
        body=(folder/'response.json').read_bytes();need(sha(folder/'response.json')==record['sha256'],'BODY_HASH')
    return body


def compile_archive():
    os.umask(0o077)
    c=guard(); intake.preserve()
    f=strict_json((ROOT/'market-freeze.json').read_bytes())
    collection=strict_json((ROOT/'market-collection.json').read_bytes())
    start,end=ms(c['formation_start_utc'])-60*MINUTE,ms(c['evaluation_end_utc'])
    n=(end-start)//MINUTE
    data={(s,k):array('d',[float('nan')])*(n*(5 if k=='trade' else 4)) for s in intake.SYMBOLS for k in ('trade','mark')}
    seen={(s,k):bytearray(n) for s,k in data}; funding={s:{} for s in intake.SYMBOLS}; warm={s:{} for s in intake.SYMBOLS}
    def accept(r,raw):
        rows=validate(raw,r);s,k=r['symbol'],r['kind']
        for t,values in rows:
            if k=='funding':
                need(t not in funding[s],'DUPLICATE_FUNDING');funding[s][t]=values
            elif r['interval_minutes']==60:
                need(t not in warm[s],'DUPLICATE_WARMUP');warm[s][t]=values[3]
            else:
                i=(t-start)//MINUTE; width=5 if k=='trade' else 4
                need(0<=i<n and not seen[s,k][i],'DUPLICATE_OR_OUTSIDE_MINUTE');seen[s,k][i]=1
                data[s,k][i*width:(i+1)*width]=array('d',(float(v) for v in values[:width]))
    for i,r0 in enumerate(f['reuse_requests']):
        r=dict(r0,symbol='ETHUSDT',interval_minutes=0 if r0['kind']=='funding' else 1)
        accept(r,raw_page(f['reuse_folders'][str(i)],r0,True))
    need(set(collection['records'])=={str(i) for i in range(len(f['requests']))},'NEW_ARCHIVE_INCOMPLETE')
    for i,r in enumerate(f['requests']): accept(r,raw_page(collection['records'][str(i)]['folder'],r))
    need(all(all(v) for v in seen.values()),'COMPILED_MINUTE_GAP')
    expected=list(range(ms(c['warmup_start_utc']),ms(c['formation_start_utc']),60*MINUTE))
    need(all(sorted(warm[s])==expected for s in intake.SYMBOLS),'WARMUP_GRID')
    output=ROOT/'compiled';output.mkdir(mode=0o700)
    files={}
    for (s,k),a in data.items():
        p=output/f'{s}-{k}.f64'
        with p.open('xb') as stream:a.tofile(stream)
        files[str(p)]=sha(p)
    save(output/'funding.json',funding);save(output/'warmup.json',warm)
    for p in (output/'funding.json',output/'warmup.json'):files[str(p)]=sha(p)
    save(ROOT/'input-audit.json',dict(status='G0_INPUT_COMPLETE_DEVELOPMENT_ONLY',start_ms=start,end_ms=end,
        minutes_per_asset=n,price_arrays=4,funding_counts={s:len(v) for s,v in funding.items()},
        warmup_hours_per_asset=len(expected),files=files,byteorder=sys.byteorder,array_type='float64',
        historical_filters_verified=False,actual_fills_verified=False,economic_attempts=0,
        protected_files=intake.preserve(),request_attempts=collection['attempts'],new_wire_bytes=collection['bytes']))
    print(json.dumps(dict(status='G0_INPUT_COMPLETE_DEVELOPMENT_ONLY',minutes_per_asset=n,
        funding_counts={s:len(v) for s,v in funding.items()},economic_attempts=0)),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('freeze','collect','compile'))
    a=p.parse_args()
    if a.action=='compile' and (ROOT/'reuse-correction.json').exists():
        need(subprocess.run([sys.executable,'-m','unittest','discover','-s','tools','-p','test_eth_sol*.py','-v']).returncode==0,'REUSE_REPAIR_REGRESSIONS')
    {'freeze':freeze,'collect':collect,'compile':compile_archive}[a.action]()
