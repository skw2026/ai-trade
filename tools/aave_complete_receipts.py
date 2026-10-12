#!/usr/bin/env python3
"""Bind every annual indexed transaction to native receipts and block headers.

Acquisition is disabled until the deterministic annual catalog is complete.
Once-only bounded chunks, no economic calculations and no automatic retries.
"""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import subprocess
import time

import aave_complete_input as a
import aave_complete_catalog as c


def grouped(rows):
    groups={}
    for row in sorted(rows,key=c.key):
        groups.setdefault(row['transactionHash'],[]).append(row)
    return groups


def validate_transaction(receipt, rows):
    a.require(bool(rows),'EMPTY_TRANSACTION_GROUP')
    a.require(len({r['transactionHash'] for r in rows})==1,'MIXED_TRANSACTION_GROUP')
    for row in rows:
        a.validate_receipt(receipt,row)
    targets=[r for r in receipt['logs'] if r['address'].lower()==c.POOL and
             r['topics'][:2]==[c.TOPIC,'0x'+'0'*24+c.WETH[2:]]]
    a.require({int(r['logIndex'],16) for r in targets}=={int(r['logIndex'],16) for r in rows},
              'NATIVE_TARGETS_DIFFER_FROM_CATALOG')
    return len(receipt['logs'])


def catalog():
    value=json.loads((a.ROOT/'annual-catalog.json').read_text())
    rows,missing=c.layout()
    a.require(missing is None and value['events']==rows and
              value['rows_sha256']==a.digest(json.dumps(rows,sort_keys=True).encode()),
              'ANNUAL_CATALOG_NOT_VERIFIED')
    a.require(value['status']=='INDEXED_ANNUAL_CATALOG_COMPLETE_NOT_RECEIPT_ADMITTED',
              'ANNUAL_CATALOG_STATUS')
    return rows


def request_identity(method,params):
    return {'jsonrpc':'2.0','id':1,'method':method,'params':params}


def stored(name,method,params):
    out=a.ROOT/'operations'/name
    if not out.exists():
        return None
    a.require((out/'record.json').exists(),'PRIOR_RPC_OPERATION_INCOMPLETE')
    record=json.loads((out/'record.json').read_text())
    request_raw=(out/'request.json').read_bytes()
    raw=(out/'response.json').read_bytes()
    a.require(record['status'] in ('CONTROL_PASS','NATIVE_VALID') and record['endpoint']==a.ENDPOINT,
              'PRIOR_RPC_FAILED_OR_WRONG_SOURCE')
    a.require(a.digest(request_raw)==record['request_sha256'] and
              json.loads(request_raw)==request_identity(method,params),'RPC_REQUEST_CHANGED')
    a.require(a.digest(raw)==record['response_sha256'],'RPC_RESPONSE_CHANGED')
    return envelope(json.loads(raw))


def envelope(value):
    a.require(isinstance(value,dict) and value.get('jsonrpc')=='2.0' and type(value.get('id')) is int
              and value['id']==1 and 'error' not in value and 'result' in value,'RPC_ENVELOPE')
    return value['result']


def fetch(name,method,params,validator):
    a.require(method in ('eth_getTransactionReceipt','eth_getBlockByNumber'),'READ_ONLY_ALLOWLIST')
    c.budget()
    out=a.ROOT/'operations'/name
    out.mkdir(mode=0o700)
    a.save(out/'request.json',request_identity(method,params))
    record={'endpoint':a.ENDPOINT,'method':method,'public_operations':1,'status':'NATIVE_FAILED',
            'started_utc':a.now().isoformat(),'request_sha256':a.digest((out/'request.json').read_bytes()),
            'source_sha256':a.digest(Path(__file__).read_bytes())}
    try:
        time.sleep(1)
        r=subprocess.run(['curl','--silent','--show-error','--proto','=https','--connect-timeout','5',
             '--max-time','30','--max-filesize',str(a.MAX_BODY),'--retry','0','-H','Content-Type: application/json',
             '--data-binary','@'+str(out/'request.json'),'--dump-header',str(out/'headers.txt'),
             '--output',str(out/'response.json'),'--write-out','%{http_code}',a.ENDPOINT],
             capture_output=True,text=True,timeout=35)
        record.update(http_status=r.stdout.strip(),curl_returncode=r.returncode)
        if (out/'response.json').exists():
            raw=(out/'response.json').read_bytes()
            record.update(response_sha256=a.digest(raw),response_bytes=len(raw))
        a.require(r.returncode==0 and r.stdout.strip()=='200','NATIVE_TRANSPORT_OR_HTTP')
        value=envelope(json.loads(raw))
        validator(value)
        record['status']='NATIVE_VALID'
        return value
    except Exception as exc:
        record.update(error_type=type(exc).__name__,error=str(exc)[:240])
        raise
    finally:
        record['ended_utc']=a.now().isoformat()
        a.save(out/'record.json',record)
        print(json.dumps(record,sort_keys=True),flush=True)


def receipt_name(tx):
    first=a.first_transactions()
    for index,row in enumerate(first):
        if row['transactionHash']==tx:
            return 'receipt0-corrected' if index==0 else 'receipt%d'%index
    return 'native-receipt-'+tx[2:]


def block_name(number):
    return 'block' if number==a.first_transactions()[0]['blockNumber'] else 'native-block-'+str(int(number,16))


def estimate(rows):
    transactions=grouped(rows)
    blocks={r['blockNumber'] for r in rows}
    missing_receipts=sum(not (a.ROOT/'operations'/receipt_name(tx)).exists() for tx in transactions)
    missing_blocks=sum(not (a.ROOT/'operations'/block_name(b)).exists() for b in blocks)
    resources=c.budget()
    return dict(resources,transactions=len(transactions),blocks=len(blocks),
                missing_receipts=missing_receipts,missing_blocks=missing_blocks,
                minimum_remaining_native_calls=missing_receipts+missing_blocks,
                pool_and_market_costs='NOT_YET_ESTABLISHED')


def run_chunk(limit):
    a.protect()
    rows=catalog()
    resources=estimate(rows)
    a.require(resources['public_operations']+resources['minimum_remaining_native_calls']<10000,
              'KNOWN_NATIVE_INPUT_EXCEEDS_REQUEST_BUDGET')
    print(json.dumps({'event':'NATIVE_INPUT_ESTIMATE',**resources}),flush=True)
    count=0
    references={}
    by_block=defaultdict(list)
    receipts={}
    for tx,items in grouped(rows).items():
        name=receipt_name(tx)
        method,params='eth_getTransactionReceipt',[tx]
        value=stored(name,method,params)
        if value is None:
            if count>=limit:
                print(json.dumps({'status':'CHUNK_LIMIT_NOT_INPUT_PASS','next_method':method,
                                  'new_requests':count}),flush=True)
                a.protect()
                return
            value=fetch(name,method,params,lambda v:validate_transaction(v,items))
            count+=1
        validate_transaction(value,items)
        receipts[tx]=value
        by_block[items[0]['blockNumber']].append(items[0])
        references[name]=json.loads((a.ROOT/'operations'/name/'record.json').read_text())['response_sha256']
    for number,items in sorted(by_block.items(),key=lambda pair:int(pair[0],16)):
        name=block_name(number)
        method,params='eth_getBlockByNumber',[number,False]
        def check(value):
            for row in items:
                a.validate_block(value,row,receipts[row['transactionHash']])
        value=stored(name,method,params)
        if value is None:
            if count>=limit:
                print(json.dumps({'status':'CHUNK_LIMIT_NOT_INPUT_PASS','next_method':method,
                                  'new_requests':count}),flush=True)
                a.protect()
                return
            value=fetch(name,method,params,check)
            count+=1
        check(value)
        references[name]=json.loads((a.ROOT/'operations'/name/'record.json').read_text())['response_sha256']
    a.save(a.ROOT/'native-bindings.json',{'status':'INDEXED_EVENTS_NATIVE_BOUND_NOT_FLOW_IDENTIFIED',
           'catalog_sha256':a.digest((a.ROOT/'annual-catalog.json').read_bytes()),
           'events':len(rows),'transactions':len(receipts),'blocks':len(by_block),
           'response_bindings':references,'resources':c.budget()})
    a.protect()
    print(json.dumps({'status':'INDEXED_EVENTS_NATIVE_BOUND_NOT_FLOW_IDENTIFIED',
                      'events':len(rows),'transactions':len(receipts),'blocks':len(by_block)}),flush=True)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-requests',type=int,default=100)
    parser.add_argument('--estimate-only',action='store_true')
    args=parser.parse_args()
    a.require(1<=args.max_requests<=100,'CHUNK_LIMIT')
    if args.estimate_only:
        a.protect()
        print(json.dumps(estimate(catalog()),sort_keys=True))
    else:
        run_chunk(args.max_requests)


if __name__=='__main__':main()
