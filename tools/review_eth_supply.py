#!/usr/bin/env python3
"""Bounded anonymous ETH input discovery, never price/signal/return research."""
import argparse
import gzip
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from attention_capacity import GATES as PRIOR
from screen_regional_session import need, sha, strict, save_new, utc, ms, transport

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.artifacts/eth-supply-review-20260928'
GATE = RUN / 'validation-state.json'
PLAN = 'docs/plans/2026-09-28-eth-supply-input-review.md'
GATES = PRIOR + ['capacity-first-20260928/validation-state.json']
METRICS = ('SplyBurntNtv','IssContNtv','IssTotNtv','PenaltyNtv','SlashedNtv',
           'SplyCur','AssetCompletionTime','AssetEODCompletionTime')
DATES = ('2023-01-01','2024-03-14','2025-06-01')
HOST = 'https://community-api.coinmetrics.io/v4/'


def identities():
    return {p:sha((ROOT/'.artifacts'/p).read_bytes()) for p in GATES}


def running():
    need(strict(GATE.read_bytes())['status']=='RUNNING','FORMAL_GATE_REQUIRED')


def freeze():
    running()
    for name in ('attempts','receipts','raw'):
        (RUN/name).mkdir(exist_ok=False)
    paths = (PLAN,'tools/review_eth_supply.py','tools/test_review_eth_supply.py',
             'tools/screen_regional_session.py','tools/attention_capacity.py',
             'tools/free_option_flow_data.py','tools/option_flow_probe.py')
    save_new(RUN/'freeze.json',dict(old_gates=identities(),started_utc='2026-09-28T14:34:39Z',
        frozen_utc=utc(time.time()*1000),source_sha256={p:sha((ROOT/p).read_bytes()) for p in paths}))


def frozen():
    f = strict((RUN/'freeze.json').read_bytes())
    need(identities()==f['old_gates'],'OLD_GATES_CHANGED')
    need(all(sha((ROOT/p).read_bytes())==h for p,h in f['source_sha256'].items()),'FROZEN_SOURCE_CHANGED')
    need(time.time()*1000-ms(f['started_utc'])<90*60000,'REVIEW_TIME_BUDGET')


def url_for(kind, date=None):
    params = dict(assets='eth,eth_cl',page_size=10000)
    if kind=='community':
        path='catalog-v2/asset-metrics'
    elif kind=='full':
        path='catalog-all-v2/asset-metrics'
        params['metrics']=','.join(METRICS)
    elif kind=='definitions':
        path='reference-data/asset-metrics'
        params=dict(metrics=','.join(METRICS),page_size=10000)
    elif kind=='sample':
        need(date in DATES,'SAMPLE_DATE_NOT_APPROVED')
        path='timeseries/asset-metrics'
        params=dict(assets='eth',metrics='IssTotNtv,SplyCur,AssetCompletionTime,AssetEODCompletionTime',
                    frequency='1d',start_time=date,end_time=date,page_size=1)
    else:
        raise ValueError('UNKNOWN_READ_ONLY_PROBE')
    return HOST+path+'?'+urllib.parse.urlencode(params)


def decode(raw,status):
    value = strict(raw)
    if status in (400,401,403,404):
        need(isinstance(value,dict) and 'error' in value,'UNEXPECTED_DENIAL_SCHEMA')
        return dict(outcome='CAPABILITY_OR_ACCESS_DENIED',error=value['error'])
    need(status==200 and isinstance(value,dict) and isinstance(value.get('data'),list),'UNEXPECTED_RESPONSE')
    need(not value.get('next_page_token') and not value.get('next_page_url'),'INCOMPLETE_METADATA_PAGE')
    return dict(outcome='READABLE',data=value['data'])


def fetch(kind,date,documentation_reads):
    running()
    frozen()
    need(0<=documentation_reads<=40,'DOCUMENT_BUDGET')
    key = kind+('-'+date if date else '')
    url = url_for(kind,date)
    need(not (RUN/'receipts'/f'{key}.json').exists(),'NO_AUTOMATIC_REPEAT')
    count=len(list((RUN/'attempts').glob('*.json')))+1
    need(count<=12 and count+documentation_reads<=40,'PUBLIC_READ_BUDGET')
    save_new(RUN/'attempts'/f'{count:02d}.json',dict(key=key,url=url,documentation_reads=documentation_reads,
              started_utc=utc(time.time()*1000)))
    request = urllib.request.Request(url,headers={'User-Agent':'ai-trade-input-review/1.0',
                                                 'Accept':'application/json','Accept-Encoding':'gzip'})
    raw,status,error=b'',None,None
    try:
        with transport().open(request,timeout=45) as response:
            status=response.status
            raw=response.read(4*1024*1024+1)
            need(len(raw)<=4*1024*1024,'BODY_LIMIT')
            if response.headers.get('Content-Encoding')=='gzip':
                raw=gzip.decompress(raw)
            need(len(raw)<=16*1024*1024,'DECODED_LIMIT')
    except urllib.error.HTTPError as exc:
        status,raw=exc.code,exc.read(1024*1024)
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc)
    with (RUN/'raw'/f'{key}.json').open('xb') as output:
        output.write(raw)
    receipt=dict(url=url,status=status,error=error,sha256=sha(raw),bytes=len(raw),
                 at=utc(time.time()*1000),raw=f'raw/{key}.json')
    save_new(RUN/'receipts'/f'{key}.json',receipt)
    need(error is None,'TECHNICAL_FETCH_FAILURE '+str(error))
    parsed=decode(raw,status)
    print(json.dumps(dict(key=key,**receipt,**parsed),indent=2))


def read(key):
    r=strict((RUN/'receipts'/f'{key}.json').read_bytes())
    raw=(RUN/r['raw']).read_bytes()
    need(sha(raw)==r['sha256'],'RAW_CHANGED')
    return decode(raw,r['status'])


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=('freeze','community','full','definitions','sample'))
    p.add_argument('--date',choices=DATES)
    p.add_argument('--documentation-reads',type=int,default=16)
    args=p.parse_args()
    freeze() if args.action=='freeze' else fetch(args.action,args.date,args.documentation_reads)
