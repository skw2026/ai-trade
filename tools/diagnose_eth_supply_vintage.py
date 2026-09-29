#!/usr/bin/env python3
"""Bounded public Git-history diagnosis; never a strategy or publication retry."""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from screen_regional_session import need, strict, save_new, transport, sha, ms, utc

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'.artifacts/eth-supply-vintage-diagnosis-20260929'
API='https://api.github.com/repos/coinmetrics/data/'
RAW='https://raw.githubusercontent.com/coinmetrics/data/'
DEADLINES=('2023-09-18T12:00:00Z','2024-08-05T12:00:00Z')
SOURCES=('docs/plans/2026-09-29-eth-supply-vintage-diagnosis.md',
         'tools/diagnose_eth_supply_vintage.py','tools/test_diagnose_eth_supply_vintage.py')
DOCUMENT_READS=2


def preserved():
    b=strict((RUN/'baseline.json').read_bytes())
    need(all(sha((ROOT/p).read_bytes())==h for p,h in b['preserved_sha256'].items()),'OLD_EVIDENCE_CHANGED')
    return b


def check():
    need(strict((RUN/'validation-state.json').read_bytes())['status']=='RUNNING','FORMAL_GATE_REQUIRED')
    preserved()
    f=strict((RUN/'freeze.json').read_bytes())
    need(all(sha((ROOT/p).read_bytes())==h for p,h in f['source_sha256'].items()),'FROZEN_SOURCE_CHANGED')
    need(time.time()*1000-ms('2026-09-29T05:30:52Z')<3600000,'DIAGNOSIS_TIME_LIMIT')


def freeze():
    preserved()
    need(strict((RUN/'validation-state.json').read_bytes())['status']=='RUNNING','FORMAL_GATE_REQUIRED')
    for folder in ('attempts','raw','receipts'):
        (RUN/folder).mkdir(exist_ok=False)
    save_new(RUN/'freeze.json',dict(documentation_reads=DOCUMENT_READS,
        source_sha256={p:sha((ROOT/p).read_bytes()) for p in SOURCES}))
    print('VINTAGE_DIAGNOSIS_FROZEN; old screen remains closed')


def allowed(url):
    parsed=urllib.parse.urlsplit(url)
    return (parsed.scheme=='https' and not parsed.username and not parsed.password and
            not parsed.fragment and (url.startswith(API) or url.startswith(RAW)))


def get(key,url,limit=2*1024*1024):
    check()
    need(allowed(url),'OFFICIAL_READ_ONLY_URL_REQUIRED')
    need(re.fullmatch(r'[a-z0-9-]+',key),'BAD_KEY')
    need(not (RUN/'receipts'/f'{key}.json').exists(),'NO_REPEAT')
    count=len(list((RUN/'attempts').glob('*.json')))+1
    need(count<=12 and count+DOCUMENT_READS<=16,'DIAGNOSIS_READ_BUDGET')
    save_new(RUN/'attempts'/f'{count:02d}.json',dict(key=key,url=url,started=utc(time.time()*1000)))
    req=urllib.request.Request(url,headers={'Accept':'application/vnd.github+json',
                         'User-Agent':'ai-trade-vintage-diagnosis/1.0','X-GitHub-Api-Version':'2022-11-28'})
    status,raw,error,headers=None,b'',None,{}
    try:
        with transport().open(req,timeout=45) as response:
            status,headers=response.status,dict(response.headers)
            raw=response.read(limit+1)
            need(len(raw)<=limit,'BODY_LIMIT')
    except urllib.error.HTTPError as exc:
        status,raw=exc.code,exc.read(1024*1024)
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc)
    with (RUN/'raw'/f'{key}.bin').open('xb') as output:
        output.write(raw)
    receipt=dict(url=url,status=status,error=error,sha256=sha(raw),bytes=len(raw),
                 retrieved_utc=utc(time.time()*1000),raw=f'raw/{key}.bin',
                 headers={k:v for k,v in headers.items() if k.lower() in
                   ('date','etag','last-modified','link','x-github-request-id','x-ratelimit-remaining')})
    save_new(RUN/'receipts'/f'{key}.json',receipt)
    need(error is None and status in (200,404),'DIAGNOSTIC_FETCH_FAILURE '+key+' '+str(status)+' '+str(error))
    print(json.dumps(dict(key=key,status=status,bytes=len(raw),sha256=receipt['sha256'])),flush=True)
    return raw,status


def read(key):
    r=strict((RUN/'receipts'/f'{key}.json').read_bytes())
    raw=(RUN/r['raw']).read_bytes()
    need(sha(raw)==r['sha256'],'RECEIPT_IDENTITY')
    return raw,r['status']


def commit_choice(raw,deadline=None):
    values=strict(raw)
    need(isinstance(values,list) and len(values)<=1,'COMMIT_QUERY_SCHEMA')
    if not values:
        return None
    value=values[0]
    need(re.fullmatch('[0-9a-f]{40}',value['sha']),'COMMIT_SHA')
    if deadline:
        need(ms(value['commit']['committer']['date'])<=ms(deadline),'COMMIT_AFTER_DEADLINE')
    return dict(sha=value['sha'],author_date=value['commit']['author']['date'],
                committer_date=value['commit']['committer']['date'],
                verification=value['commit'].get('verification'),
                historical_publication_proven=False)


def history():
    for i,deadline in enumerate(DEADLINES):
        url=API+'commits?'+urllib.parse.urlencode(dict(path='csv/eth.csv',until=deadline,per_page=1))
        raw,status=get(f'history-{i}',url)
        result=commit_choice(raw,deadline) if status==200 else None
        save_new(RUN/f'history-{i}.json',dict(deadline=deadline,choice=result,http_status=status))
        print(json.dumps(dict(deadline=deadline,choice=result)))


def controls():
    raw,status=get('current-file-history',API+'commits?path=csv%2Feth.csv&per_page=1')
    current=commit_choice(raw) if status==200 else None
    raw,status=get('old-repository-history',API+'commits?'+urllib.parse.urlencode(dict(until=DEADLINES[-1],per_page=1)))
    old=commit_choice(raw,DEADLINES[-1]) if status==200 else None
    result=dict(current_file=current,old_repository=old)
    if current:
        commit=current['sha']
        raw,status=get('pipeline',RAW+commit+'/.gitlab-ci.yml')
        result['pipeline_status']=status
        if status==200:
            result['pipeline_source']=raw.decode('utf-8')
    save_new(RUN/'controls.json',result)
    print(json.dumps(result,indent=2))


def extract(raw,blob_sha,deadline):
    need(hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==blob_sha,'GIT_BLOB_MISMATCH')
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    fields=reader.fieldnames
    need(fields and len(fields)==len(set(fields)),'CSV_HEADER')
    if 'time' not in fields or 'SplyCur' not in fields:
        return dict(status='REQUIRED_COLUMNS_ABSENT',selected=[],columns=fields)
    day=dt.date.fromisoformat(deadline[:10])
    wanted={(day-dt.timedelta(days=i)).isoformat() for i in range(2,10)}
    selected={}
    for row in reader:
        key=row['time'][:10]
        if key not in wanted:
            continue
        need(key not in selected,'DUPLICATE_TARGET_DAY')
        selected[key]={k:row.get(k) for k in ('time','SplyCur','AssetCompletionTime','AssetEODCompletionTime')}
    return dict(status='VERSION_ROWS_FOUND' if len(selected)==8 else 'TARGET_DAYS_INCOMPLETE',
                selected=selected,missing_days=sorted(wanted-set(selected)),
                historical_publication_proven=False)


def snapshots():
    for i,deadline in enumerate(DEADLINES):
        entry=strict((RUN/f'history-{i}.json').read_bytes())
        choice=entry['choice']
        if choice is None:
            continue
        commit=choice['sha']
        raw,status=get(f'file-meta-{i}',API+f'contents/csv/eth.csv?ref={commit}')
        if status==404:
            save_new(RUN/f'snapshot-{i}.json',dict(status='FILE_NOT_FOUND'))
            continue
        meta=strict(raw)
        need(meta.get('type')=='file' and meta.get('path')=='csv/eth.csv','FILE_IDENTITY')
        need(type(meta.get('size')) is int and 0<meta['size']<=16*1024*1024,'SNAPSHOT_SIZE_LIMIT')
        need(re.fullmatch('[0-9a-f]{40}',meta.get('sha','')),'BLOB_SHA_FORMAT')
        raw,status=get(f'snapshot-{i}',RAW+commit+'/csv/eth.csv',16*1024*1024)
        need(status==200,'PINNED_BLOB_NOT_FOUND')
        result=extract(raw,meta['sha'],deadline)
        save_new(RUN/f'snapshot-{i}.json',dict(**result,commit=commit,blob_sha=meta['sha']))
        print(json.dumps({k:v for k,v in result.items() if k!='selected'}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('freeze','history','controls','snapshots'))
    action=parser.parse_args().action
    {'freeze':freeze,'history':history,'controls':controls,'snapshots':snapshots}[action]()
