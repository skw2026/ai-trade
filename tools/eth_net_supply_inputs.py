#!/usr/bin/env python3
"""Fixed anonymous supply snapshot and pre-price capacity upper bound; no returns."""
import argparse
from collections import Counter
import datetime as dt
import gzip
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

import eth_net_supply_contract as design
from screen_regional_session import need, sha, strict, save_new, transport, utc, ms

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.artifacts/eth-net-supply-screen-20260929'
GATE = RUN / 'validation-state.json'
CONTRACT_SHA = '0e11227b41fd8edf126e4bf77092878a40c3008486e6839f1c83911b59b210cd'
STARTED = '2026-09-29T03:16:33Z'
DOCUMENT_READS = 12
HOST = 'https://community-api.coinmetrics.io/v4/'
FIELDS = ('SplyCur', 'AssetCompletionTime', 'AssetEODCompletionTime')
SOURCES = ('docs/plans/2026-09-29-eth-net-supply-screen.md',
           'docs/reviews/2026-09-29-eth-net-supply-input-review.md',
           'tools/eth_net_supply_inputs.py', 'tools/test_eth_net_supply_inputs.py')


def frozen_contract():
    need(sha(design.CONTRACT.read_bytes()) == CONTRACT_SHA, 'CONTRACT_CHANGED')
    return design.contract()


def preserved():
    baseline = strict((RUN/'baseline.json').read_bytes())
    need(all(sha((ROOT/p).read_bytes()) == h for p,h in baseline['preserved_sha256'].items()),
         'OLD_EVIDENCE_CHANGED')
    return baseline


def running():
    need(strict(GATE.read_bytes())['status'] == 'RUNNING', 'FORMAL_GATE_REQUIRED')


def frozen():
    running()
    preserved()
    frozen_contract()
    f = strict((RUN/'freeze.json').read_bytes())
    need(all(sha((ROOT/p).read_bytes()) == h for p,h in f['source_sha256'].items()), 'SOURCE_CHANGED')
    need(time.time()*1000 - ms(STARTED) < 8*3600000, 'EIGHT_HOUR_BUDGET')


def freeze():
    running()
    preserved()
    frozen_contract()
    for folder in ('raw', 'attempts', 'receipts', 'pages'):
        (RUN/folder).mkdir(exist_ok=False)
    save_new(RUN/'freeze.json', dict(started_utc=STARTED, documentation_reads=DOCUMENT_READS,
        contract_sha256=CONTRACT_SHA, source_sha256={p:sha((ROOT/p).read_bytes()) for p in SOURCES}))
    print('SUPPLY_INPUTS_FROZEN; no economic attempt')


def specifications():
    day = dt.date(2022,12,24)
    end = dt.date(2025,12,28)
    specs = []
    while day < end:
        last = min(end, day+dt.timedelta(days=1000))-dt.timedelta(days=1)
        params = dict(assets='eth', metrics=','.join(FIELDS), frequency='1d',
                      start_time=day.isoformat(), end_time=last.isoformat(), page_size=1000)
        url = HOST+'timeseries/asset-metrics?'+urllib.parse.urlencode(params)
        specs.append(dict(start=day.isoformat(), end=last.isoformat(), url=url, key=sha(url.encode())))
        day = last+dt.timedelta(days=1)
    return specs


def parse_supply(raw, spec, retrieved):
    data = strict(raw)
    need(isinstance(data, dict) and isinstance(data.get('data'), list), 'SUPPLY_SCHEMA')
    rows = data['data']
    need(len(rows) <= 1000, 'PAGE_SIZE')
    out = {}
    first, last = dt.date.fromisoformat(spec['start']), dt.date.fromisoformat(spec['end'])
    for row in rows:
        need(row.get('asset') == 'eth', 'WRONG_ASSET')
        at = design.instant(row['time'])
        need(at == at.replace(hour=0,minute=0,second=0,microsecond=0), 'NOT_DAILY_BOUNDARY')
        day = at.date().isoformat()
        need(first <= at.date() <= last and day not in out, 'DUPLICATE_OR_OUT_OF_RANGE')
        normalized = dict(asset='eth',frequency='1d',day=day,known_revision=False)
        for field in FIELDS:
            value = row.get(field)
            normalized[field] = value
            if value is None:
                continue
            if field == 'SplyCur':
                design.positive(value)
            else:
                need(isinstance(value,str) and value.isascii() and value.isdigit(), 'CLOCK_FORMAT')
                clock = dt.datetime.fromtimestamp(int(value),dt.timezone.utc)
                need(at+design.DAY <= clock <= design.instant(retrieved), 'CLOCK_RANGE')
        # Absence is UNKNOWN, not evidence of an unmodified historical vintage.
        for key, value in row.items():
            if key.endswith('-status'):
                need(value in ('reviewed','flash',None), 'REVISION_STATUS_REQUIRES_REVIEW')
        out[day] = normalized
    full_size = (last-first).days+1
    need(not (data.get('next_page_token') or data.get('next_page_url')) or len(out) == full_size,
         'UNRESOLVED_PAGINATION')
    return out


def fetch_supply():
    frozen()
    for spec in specifications():
        frozen()
        page = RUN/'pages'/f"{spec['key']}.json"
        if page.exists():
            read_page(spec)
            continue
        count = len(list((RUN/'attempts').glob('*.json')))+1
        need(count+DOCUMENT_READS <= 120, 'READ_BUDGET')
        name = f'{count:03d}'
        save_new(RUN/'attempts'/f'{name}.json', dict(**spec,started_utc=utc(time.time()*1000)))
        raw, error, status, headers = b'', None, None, {}
        req = urllib.request.Request(spec['url'], headers={'User-Agent':'ai-trade-net-supply-research/1.0',
                                       'Accept':'application/json','Accept-Encoding':'gzip'})
        try:
            with transport().open(req,timeout=45) as response:
                status,headers = response.status,dict(response.headers)
                raw = response.read(16*1024*1024+1)
                need(len(raw)<=16*1024*1024,'BODY_LIMIT')
                if response.headers.get('Content-Encoding')=='gzip':
                    raw=gzip.decompress(raw)
                need(len(raw)<=32*1024*1024,'DECODED_LIMIT')
        except urllib.error.HTTPError as exc:
            status,raw,error = exc.code,exc.read(1024*1024),type(exc).__name__
        except Exception as exc:
            error=type(exc).__name__+': '+str(exc)
        with (RUN/'raw'/f'{name}.json').open('xb') as output:
            output.write(raw)
        receipt = dict(url=spec['url'],raw=f'raw/{name}.json',status=status,error=error,
                       sha256=sha(raw),bytes=len(raw),retrieved_utc=design.stamp(dt.datetime.now(dt.timezone.utc)),
                       headers={k:v for k,v in headers.items() if k.lower() in
                                ('date','etag','last-modified','content-type','cf-ray')})
        save_new(RUN/'receipts'/f'{name}.json',receipt)
        need(status==200 and error is None,'SUPPLY_FETCH_FAILED '+name+' '+str(error))
        parsed=parse_supply(raw,spec,receipt['retrieved_utc'])
        save_new(page,receipt)
        print(json.dumps(dict(get=count,status=status,rows=len(parsed),sha256=receipt['sha256'])),flush=True)


def read_page(spec):
    receipt = strict((RUN/'pages'/f"{spec['key']}.json").read_bytes())
    raw = (RUN/receipt['raw']).read_bytes()
    need(sha(raw)==receipt['sha256'] and receipt['url']==spec['url'],'RAW_IDENTITY')
    return parse_supply(raw,spec,receipt['retrieved_utc']),receipt


def supply_inputs():
    out,receipts={},[]
    for spec in specifications():
        rows,receipt=read_page(spec)
        need(not set(out)&set(rows),'PAGE_OVERLAP')
        out.update(rows)
        receipts.append(receipt)
    return out,max(r['retrieved_utc'] for r in receipts)


def clock_upper_bound(c, values, retrieved):
    """Assume every future price sign is usable: a strict upper bound, not PASS."""
    weeks=[]
    for entry in design.calendar(c):
        decision=design.instant(entry)
        old=decision.replace(hour=0)+dt.timedelta(days=-9)
        days=[(old+i*design.DAY).date().isoformat() for i in range(8)]
        # Fictitious non-tied past prices solely to exercise supply/clock semantics.
        # They never enter any market result or economic calculation.
        prices={design.stamp(old+design.DAY):'1', design.stamp(old+8*design.DAY):'2'}
        o=design.observe(c,entry,[values[d] for d in days if d in values],prices,retrieved)
        o.pop('price_direction',None)
        weeks.append(o)
    halves=[sum(o['valid'] and ((o['entry'] < c['calendar']['split']) == early) for o in weeks)
            for early in (True,False)]
    total=sum(o['valid'] for o in weeks)
    impossible=(total<c['capacity']['minimum_valid_weeks'] or
                min(halves)<c['capacity']['minimum_each_calendar_half'])
    return dict(status='CAPACITY_INSUFFICIENT' if impossible else 'PRICE_CAPACITY_STILL_REQUIRED',
                scope='SUPPLY_CLOCK_UPPER_BOUND_ONLY',maximum_valid_weeks=total,
                maximum_half_counts=halves,excluded_weeks=len(weeks)-total,
                reasons=dict(Counter(r for o in weeks for r in o['reasons'])),weeks=weeks,
                price_cells='NOT_EVALUATED',economic_attempts=0,candidate_qualified=False)


def upper_bound():
    frozen()
    data,retrieved=supply_inputs()
    result=clock_upper_bound(frozen_contract(),data,retrieved)
    result.update(supply_rows=len(data),documentation_reads=DOCUMENT_READS,
                  actual_gets=len(list((RUN/'attempts').glob('*.json'))),
                  input_sha256={str(p.relative_to(RUN)):sha(p.read_bytes())
                    for folder in ('raw','pages') for p in (RUN/folder).iterdir()})
    save_new(RUN/'clock-capacity.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('weeks','input_sha256')},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('freeze','supply','upper-bound'))
    action=parser.parse_args().action
    {'freeze':freeze,'supply':fetch_supply,'upper-bound':upper_bound}[action]()
