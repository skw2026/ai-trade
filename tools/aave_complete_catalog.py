#!/usr/bin/env python3
"""Deterministic full-year indexed catalog; no prices or economic evaluation.

Every network response is exclusive and hashed. A window can yield WAIT_FOR_QUOTA,
which is explicitly not annual input acceptance. Failed requests never resume.
"""
import argparse
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode

import aave_complete_input as a
from probe_aave_forced_flow import POOL, WETH, TOPIC

MONTH_PLAN_SHA = 'c188f9b51fe9933dba634aecdd951e72b904d3ca6a690304d87fff57a8056b5e'
CAP = 1000


def quota(headers):
    values = {}
    for line in headers.splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            values[key.strip().lower()] = value.strip()
    a.require(all(k in values for k in ('date', 'x-ratelimit-limit',
              'x-ratelimit-remaining', 'x-ratelimit-reset')), 'MISSING_QUOTA_EVIDENCE')
    limit, remaining, reset = (int(values[k]) for k in
                              ('x-ratelimit-limit','x-ratelimit-remaining','x-ratelimit-reset'))
    a.require(0 < limit and 0 <= remaining <= limit and 0 <= reset <= 86400000,
              'INVALID_QUOTA_EVIDENCE')
    date = parsedate_to_datetime(values['date'])
    a.require(date.tzinfo is not None, 'NAIVE_SERVER_DATE')
    return {'limit': limit, 'remaining': remaining,
            'not_before_utc': (date + dt.timedelta(milliseconds=reset, seconds=6)).isoformat()}


def budget():
    config = json.loads((a.ROOT / 'control.json').read_text())
    stamp = a.now()
    a.require(stamp < a.DEADLINE, 'WALL_CLOCK_BUDGET')
    # Conservative: all wall time counts as active until an actual, completed,
    # non-overlapping quota wait has been evidenced. No deduction is fabricated.
    start = dt.datetime.fromisoformat(config['start_utc'].replace('Z','+00:00'))
    intervals = []
    for path in (a.ROOT / 'completed-waits').glob('*.json'):
        item = json.loads(path.read_text())
        a.require(item['kind'] == 'COMPLETED_QUOTA_WAIT', 'UNKNOWN_WAIT_KIND')
        low, high = (dt.datetime.fromisoformat(item[k]) for k in ('start_utc','end_utc'))
        a.require(start <= low < high <= stamp, 'INVALID_WAIT_INTERVAL')
        intervals.append((low, high))
    intervals.sort()
    a.require(all(x[1] <= y[0] for x,y in zip(intervals, intervals[1:])), 'OVERLAPPING_WAITS')
    active = (stamp-start).total_seconds()-sum((high-low).total_seconds() for low,high in intervals)
    a.require(active < config['max_active_seconds'], 'ACTIVE_TIME_BUDGET')
    used = config['imported_input_bytes'] + sum(p.stat().st_size for p in a.ROOT.rglob('*') if p.is_file())
    count = len(list((a.ROOT / 'operations').iterdir()))
    a.require(count < config['max_public_operations'], 'PUBLIC_OPERATION_BUDGET')
    a.require(used + a.MAX_BODY < config['max_input_bytes'], 'INPUT_BYTE_BUDGET')
    return {'active_seconds_conservative':active, 'public_operations':count,
            'input_bytes_conservative':used}


def load_months():
    raw = (a.OLD / 'month-requests.json').read_bytes()
    a.require(a.digest(raw) == MONTH_PLAN_SHA, 'MONTH_PLAN_CHANGED')
    return json.loads(raw)['months']


def key(row):
    return (int(row['blockNumber'],16), int(row['transactionIndex'],16), int(row['logIndex'],16))


def identity(row):
    return (row['transactionHash'], int(row['logIndex'],16))


def validate_indexed(value, month, low, high):
    a.require(isinstance(value,dict), 'CATALOG_ENVELOPE')
    rows = value.get('result')
    a.require(isinstance(rows,list) and len(rows) <= CAP, 'CATALOG_ROWS_OR_LIMIT')
    a.require(value.get('status') == '1' or
              (value.get('status') == '0' and rows == [] and value.get('message') == 'No logs found'),
              'CATALOG_STATUS')
    start = dt.datetime.fromisoformat(month['start_utc'].replace('Z','+00:00')).timestamp()
    end = dt.datetime.fromisoformat(month['end_utc_exclusive'].replace('Z','+00:00')).timestamp()
    seen = set()
    previous = None
    for row in rows:
        a.require(isinstance(row,dict) and row.get('address','').lower() == POOL, 'CATALOG_POOL')
        topics = row.get('topics')
        a.require(isinstance(topics,list) and len(topics)==4 and all(a.hx(t,64) for t in topics)
                  and topics[:2]==[TOPIC,'0x'+'0'*24+WETH[2:]], 'CATALOG_TOPICS')
        a.require(all(t[2:26]=='0'*24 for t in topics[1:]), 'CATALOG_TOPIC_ADDRESS')
        a.require(a.hx(row.get('data'),256), 'CATALOG_DATA')
        data = row['data'][2:]
        a.require(data[128:152]=='0'*24 and int(data[192:256],16) in (0,1), 'CATALOG_ABI')
        a.require(a.hx(row.get('transactionHash'),64) and all(a.hx(row.get(k)) for k in
                  ('blockNumber','transactionIndex','logIndex','timeStamp')), 'CATALOG_IDENTITIES')
        a.require(low <= int(row['blockNumber'],16) <= high and
                  start <= int(row['timeStamp'],16) < end, 'CATALOG_OUT_OF_DOMAIN')
        k = key(row)
        a.require(previous is None or previous < k, 'CATALOG_ORDER_OR_DUPLICATE')
        a.require(identity(row) not in seen, 'CATALOG_DUPLICATE')
        previous = k
        seen.add(identity(row))
    return rows


def check_parent(parent, children):
    mapping = {identity(r):r for r in children}
    a.require(len(mapping)==len(children), 'CHILD_DUPLICATE')
    fields = ('address','topics','data','blockNumber','transactionHash','transactionIndex','logIndex','timeStamp')
    for row in parent:
        other = mapping.get(identity(row))
        a.require(other is not None and all(other.get(k)==row.get(k) for k in fields),
                  'PARENT_EVENT_MISSING_OR_CONFLICTING')


def node_name(month, low, high):
    return 'catalog-%02d-%d-%d' % (month['index'],low,high)


def request_url(low, high):
    params={'module':'logs','action':'getLogs','address':POOL,'topic0':TOPIC,
            'topic1':'0x'+'0'*24+WETH[2:],'topic0_1_opr':'and','fromBlock':low,'toBlock':high}
    return 'https://eth.blockscout.com/api?'+urlencode(params)


def cached(month, low, high):
    if month['index'] <= 7 and low==month['low'] and high==month['high']:
        path = a.OLD / ('month-%d.response.json' % month['index'])
        protected = json.loads((a.ROOT/'protected.json').read_text())
        a.require(a.digest(path.read_bytes()) == protected[str(path)], 'IMPORTED_RESPONSE_CHANGED')
    else:
        out = a.ROOT/'operations'/node_name(month,low,high)
        if not out.exists():
            return None
        a.require((out/'record.json').exists(), 'CATALOG_OPERATION_INCOMPLETE')
        record = json.loads((out/'record.json').read_text())
        path = out/'response.json'
        a.require(record['status']=='CATALOG_VALID' and a.digest(path.read_bytes())==record['response_sha256'],
                  'CATALOG_PRIOR_FAILURE_OR_CHANGED')
        a.require(record['month']==month['index'] and record['low']==low and record['high']==high,
                  'CATALOG_REQUEST_IDENTITY')
        request_raw=(out/'request.json').read_bytes()
        a.require(a.digest(request_raw)==record['request_sha256'] and
                  json.loads(request_raw)=={'method':'GET','url':request_url(low,high)},
                  'CATALOG_REQUEST_CHANGED')
    return validate_indexed(json.loads(path.read_text()),month,low,high)


def walk(month, low, high, depth=0):
    rows = cached(month,low,high)
    if rows is None:
        return [], {'month':month['index'],'low':low,'high':high,'depth':depth}
    if len(rows)<CAP:
        return rows, None
    a.require(low<high and depth<24, 'CATALOG_UNRESOLVABLE_CAP')
    midpoint = (low+high)//2
    left, missing = walk(month,low,midpoint,depth+1)
    if missing:
        return left, missing
    right, missing = walk(month,midpoint+1,high,depth+1)
    if missing:
        return left+right, missing
    check_parent(rows,left+right)
    return left+right, None


def layout():
    all_rows=[]
    for month in load_months():
        rows, missing = walk(month,month['low'],month['high'])
        all_rows.extend(rows)
        if missing:
            return all_rows, missing
    a.require(len({identity(r) for r in all_rows})==len(all_rows), 'ANNUAL_DUPLICATE')
    return all_rows,None


def latest_quota():
    records=[]
    for path in (a.ROOT/'operations').glob('catalog-*/record.json'):
        record=json.loads(path.read_text())
        a.require(record['status']=='CATALOG_VALID', 'PRIOR_CATALOG_FAILURE')
        records.append(record)
    if records:
        return max(records,key=lambda x:x['ended_utc'])['quota']
    return quota((a.OLD/'month-7.headers').read_text())


def fetch(node):
    budget()
    month=load_months()[node['month']]
    out=a.ROOT/'operations'/node_name(month,node['low'],node['high'])
    out.mkdir(mode=0o700)
    url=request_url(node['low'],node['high'])
    a.save(out/'request.json',{'method':'GET','url':url})
    record=dict(node,started_utc=a.now().isoformat(),status='CATALOG_FAILED',public_operations=1,
                request_sha256=a.digest((out/'request.json').read_bytes()),
                source_sha256=a.digest(Path(__file__).read_bytes()))
    try:
        time.sleep(1)
        r=subprocess.run(['curl','--silent','--show-error','--proto','=https','--connect-timeout','5',
             '--max-time','30','--max-filesize',str(a.MAX_BODY),'--retry','0','--dump-header',str(out/'headers.txt'),
             '--output',str(out/'response.json'),'--write-out','%{http_code}',url],capture_output=True,text=True,timeout=35)
        record.update(http_status=r.stdout.strip(),curl_returncode=r.returncode)
        if (out/'response.json').exists():
            raw=(out/'response.json').read_bytes()
            record.update(response_sha256=a.digest(raw),response_bytes=len(raw))
        a.require(r.returncode==0 and r.stdout.strip()=='200','CATALOG_TRANSPORT_OR_HTTP')
        rows=validate_indexed(json.loads(raw),month,node['low'],node['high'])
        record['quota']=quota((out/'headers.txt').read_text())
        record.update(status='CATALOG_VALID',rows=len(rows),capped=len(rows)==CAP)
    except Exception as exc:
        record.update(error_type=type(exc).__name__,error=str(exc)[:240])
        raise
    finally:
        record['ended_utc']=a.now().isoformat()
        a.save(out/'record.json',record)
        print(json.dumps(record,sort_keys=True),flush=True)


def save_catalog(output):
    raw=(json.dumps(output,sort_keys=True,indent=2)+'\n').encode()
    config=json.loads((a.ROOT/'control.json').read_text())
    used=config['imported_input_bytes']+sum(p.stat().st_size for p in a.ROOT.rglob('*') if p.is_file())
    a.require(used+len(raw)<=config['max_input_bytes'],'FINAL_CATALOG_BYTE_BUDGET')
    with (a.ROOT/'annual-catalog.json').open('xb') as handle:
        handle.write(raw)


def run_window(limit):
    a.protect()
    a.load_result('receipt2')
    count=0
    while True:
        resources=budget()
        rows,missing=layout()
        q=latest_quota()
        if missing is None:
            output={'status':'INDEXED_ANNUAL_CATALOG_COMPLETE_NOT_RECEIPT_ADMITTED',
                    'rows':len(rows),'unique_transactions':len({r['transactionHash'] for r in rows}),
                    'unique_blocks':len({r['blockNumber'] for r in rows}),
                    'rows_sha256':a.digest(json.dumps(rows,sort_keys=True).encode()),
                    'events':rows,'resources':resources}
            save_catalog(output)
            output=dict(output); output.pop('events')
            print(json.dumps(output,sort_keys=True),flush=True)
            break
        if q['remaining']==0 and a.now()<dt.datetime.fromisoformat(q['not_before_utc']):
            print(json.dumps({'status':'WAIT_FOR_QUOTA_NOT_INPUT_PASS','not_before_utc':q['not_before_utc'],
                              'next_node':missing,'resources':resources}),flush=True)
            break
        if count>=limit:
            print(json.dumps({'status':'WINDOW_LIMIT_NOT_INPUT_PASS','next_node':missing,
                              'quota':q,'resources':resources}),flush=True)
            break
        fetch(missing)
        count+=1
    a.protect()


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-requests',type=int,default=10)
    args=parser.parse_args()
    a.require(1<=args.max_requests<=10,'WINDOW_LIMIT')
    run_window(args.max_requests)


if __name__=='__main__':
    main()
