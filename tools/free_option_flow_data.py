#!/usr/bin/env python3
"""Anonymous, append-only month-start data. No economic output in probe mode."""
import argparse
import csv
import gzip
import io
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from option_flow_probe import OLD, option
from screen_regional_session import need, sha, strict, number, stamp, ms, utc, save_new, transport

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.artifacts/free-option-flow-20260928'
GATE = RUN / 'validation-state.json'
PLAN = 'docs/plans/2026-09-28-free-option-flow.md'
GATES = OLD + ['option-flow-proxy-20260928/validation-state.json']
DATES = tuple(f'{2023+i//12:04d}-{i%12+1:02d}-01' for i in range(45))
ANCHORS = (DATES[0], DATES[22], DATES[-1])
DAY, STEP = 86400000, 300000
# Original 12 reads plus four official-document reads for the bounded size diagnosis.
DOC_GETS = 16
SOURCES = ('tools/free_option_flow_data.py', 'tools/test_free_option_flow_data.py',
           'tools/option_flow_probe.py', 'tools/screen_regional_session.py')


def old_hashes():
    return {p: sha((ROOT/'.artifacts'/p).read_bytes()) for p in GATES}


def running():
    s = strict(GATE.read_bytes())
    need(s['status'] == 'RUNNING', 'FORMAL_GATE_REQUIRED')
    return s


def freeze():
    running()
    for folder in ('attempts', 'receipts', 'raw', 'pages'):
        (RUN/folder).mkdir(exist_ok=False)
    save_new(RUN/'freeze.json', dict(started_utc='2026-09-28T13:12:06+00:00',
        frozen_utc=utc(time.time()*1000), plan_sha256=sha((ROOT/PLAN).read_bytes()),
        old_gates=old_hashes(), dates=DATES, documentation_gets=DOC_GETS,
        source_sha256={p:sha((ROOT/p).read_bytes()) for p in SOURCES}))


def frozen():
    f = strict((RUN/'freeze.json').read_bytes())
    need(old_hashes() == f['old_gates'], 'OLD_GATES_CHANGED')
    need(sha((ROOT/PLAN).read_bytes()) == f['plan_sha256'], 'PLAN_CHANGED')
    need(time.time()*1000-ms(f['started_utc']) <= 8*3600000, 'EIGHT_HOUR_BUDGET')
    return f


def specs(day):
    need(day in DATES, 'UNAPPROVED_DATE')
    start = ms(day+'T00:00:00Z')
    out = {'options':f'https://datasets.tardis.dev/v1/deribit/trades/{day.replace("-", "/")}/OPTIONS.csv.gz'}
    for kind, endpoint in (('trade','kline'), ('mark','mark-price-kline'), ('funding','funding/history')):
        funding = kind == 'funding'
        p = dict(category='linear', symbol='BTCUSDT', limit=200 if funding else 1000)
        p.update({'startTime' if funding else 'start':start, 'endTime' if funding else 'end':start+DAY-1})
        if not funding:
            p['interval'] = '5'
        out[kind] = 'https://api.bybit.com/v5/market/'+endpoint+'?'+urllib.parse.urlencode(p)
    return out


METADATA = 'https://api.tardis.dev/v1/exchanges/deribit'


def read_page(key):
    p = strict((RUN/'pages'/f'{key}.json').read_bytes())
    raw_path = RUN/'raw'/p['raw']
    need(sha(raw_path.read_bytes()) == p['sha256'], 'RAW_CHANGED')
    return raw_path


class Collector:
    def __init__(self, limit=200, diagnostic=False):
        if diagnostic:
            state = strict(GATE.read_bytes())
            need(state['status'] == 'BLOCKED' and state['active']['failure_id'] ==
                 'c91367e825c14bc5ad3fe5deb57b2619', 'BOUND_DIAGNOSIS_ONLY')
        else:
            running()
        self.diagnostic = diagnostic
        frozen()
        self.limit = limit
        self.opener = transport()
        self.allowed = {u for d in DATES for u in specs(d).values()} | {METADATA}

    def get(self, key, url):
        need(url in self.allowed and '/' not in key and '..' not in key, 'UNAPPROVED_REQUEST')
        if self.diagnostic:
            need(key == 'metadata' and url == METADATA, 'METADATA_DIAGNOSIS_ONLY')
        page = RUN/'pages'/f'{key}.json'
        if page.exists():
            need(strict(page.read_bytes())['url'] == url, 'CACHE_IDENTITY')
            return read_page(key)
        attempts = list((RUN/'attempts').glob('*.json'))
        need(all((RUN/'receipts'/p.name).exists() for p in attempts), 'INTERRUPTED_GET')
        need(DOC_GETS+len(attempts) < self.limit, 'GET_BUDGET')
        previous = [strict(p.read_bytes()) for p in attempts]
        if any(p['key'] == key for p in previous):
            need(self.diagnostic or running()['active'].get('review_path'), 'REVIEWED_RETRY_ONLY')
        frozen()
        total_bytes = sum(p.stat().st_size for p in (RUN/'raw').iterdir())
        need(total_bytes < 4*1024**3, 'TOTAL_BYTES_BUDGET')
        seq = f'{len(attempts)+1:03d}'
        record = dict(key=key, url=url, started_utc=utc(time.time()*1000), method='GET')
        save_new(RUN/'attempts'/f'{seq}.json', record)
        raw_path = RUN/'raw'/f'{seq}.bin'
        started = time.monotonic()
        status, error, size, headers = None, None, 0, {}
        try:
            request_headers = {'User-Agent':'ai-trade-bounded-public-research/1.0'}
            if url == METADATA:
                request_headers['Accept-Encoding'] = 'gzip'
            record['request_headers'] = request_headers
            req = urllib.request.Request(url, headers=request_headers)
            try:
                response = self.opener.open(req, timeout=90)
            except urllib.error.HTTPError as exc:
                response = exc
            with response, raw_path.open('xb') as out:
                status = response.status
                headers = {k:response.headers[k] for k in ('Content-Type','Content-Length','Content-Encoding','Location') if k in response.headers}
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    size += len(chunk)
                    need(size <= 64*1024**2 and total_bytes+size <= 4*1024**3, 'RESPONSE_BYTES_BUDGET')
                    need(time.monotonic()-started <= 180, 'REQUEST_TIME_BUDGET')
            if 'Content-Length' in headers:
                need(size == int(headers['Content-Length']), 'TRUNCATED_HTTP')
            need(status == 200, f'HTTP_{status}')
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
        receipt = dict(record, status=status, bytes=size, headers=headers, error=error,
                       elapsed_seconds=time.monotonic()-started, raw=raw_path.name,
                       sha256=sha(raw_path.read_bytes()) if raw_path.exists() else None)
        save_new(RUN/'receipts'/f'{seq}.json', receipt)
        need(error is None, f'GET_FAILED:{seq}:{error}')
        save_new(page, receipt)
        print(json.dumps(dict(event='GET_COMPLETE', key=key, status=status, bytes=size,
                              public_gets=DOC_GETS+len(attempts)+1)), flush=True)
        return raw_path


def metadata(path):
    receipt = strict((RUN/'pages'/'metadata.json').read_bytes())
    encoding = receipt['headers'].get('Content-Encoding','identity')
    need(encoding in ('identity','gzip'), 'METADATA_ENCODING')
    if encoding == 'gzip':
        with gzip.open(path,'rb') as g:
            raw = g.read(256*1024**2+1)
    else:
        raw = path.read_bytes()
    need(len(raw) <= 256*1024**2, 'METADATA_EXPANDED_BUDGET')
    meta = strict(raw)
    need(meta['id'] == 'deribit', 'METADATA_IDENTITY')
    # Persist only a small derived view. Original compressed response remains unchanged.
    return {k:meta[k] for k in ('id','availableSince','incidentReports')} | {
        'datasets':{k:meta['datasets'][k] for k in ('exportedFrom','exportedUntil')},
        'normalized_bytes':len(raw)}


def options(path, day, include_rows=False):
    start = ms(day+'T00:00:00Z')*1000
    a, b = start+(7*60+30)*60000000, start+(7*60+55)*60000000
    expiry = start//1000+8*3600000
    count, first, last, seen, duplicates = 0, None, None, {}, 0
    window, before, after, eligible, late = 0, 0, 0, [], 0
    expected = ['exchange','symbol','timestamp','local_timestamp','id','side','price','amount']
    # A bounded decompression prevents large/invalid gzip expansion, before CSV parsing.
    with gzip.open(path, 'rb') as g:
        raw = g.read(256*1024**2+1)
    need(len(raw) <= 256*1024**2, 'EXPANDED_BYTES_BUDGET')
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8')))
    need(reader.fieldnames == expected, 'CSV_SCHEMA')
    for r in reader:
        count += 1
        need(count <= 2000000 and None not in r and all(v is not None for v in r.values()), 'CSV_ROWS')
        local, ts = stamp(r['local_timestamp']), stamp(r['timestamp'])
        need(r['exchange'] == 'deribit' and start <= local < start+DAY*1000, 'CSV_IDENTITY_TIME')
        need(last is None or local >= last, 'LOCAL_ORDER')
        first, last = local if first is None else first, local
        if not r['symbol'].startswith('BTC-'):
            continue
        ex, cp = option(r['symbol'])
        need(r['id'].isdigit() and r['side'] in ('buy','sell'), 'BTC_ID_SIDE')
        need(number(r['amount']) > 0 and number(r['price']) > 0, 'BTC_AMOUNT_PRICE')
        need(ts <= local+1000000 and ts > 0, 'EXCHANGE_CLOCK')
        key = (r['symbol'], r['id'])
        identity = tuple(r[k] for k in expected if k != 'local_timestamp')
        if key in seen:
            need(identity == seen[key], 'CONFLICTING_DUPLICATE')
            duplicates += 1
            continue
        seen[key] = identity
        before += a-300000000 <= local < a
        after += b <= local < b+300000000
        window += a <= local < b
        if ex == expiry and cp == 'C' and a <= ts < b:
            if local < b:
                eligible.append(r)
            else:
                late += 1
    need(count > 0 and window > 0, 'EMPTY_OR_MISSING_BTC_WINDOW')
    quality = first <= start+1800000000 and last >= start+(DAY-1800000)*1000 and before > 0 and after > 0
    result = dict(date=day, rows=count, unique_btc_trades=len(seen), duplicates=duplicates,
                  first_local=first, last_local=last, window_btc_trades=window,
                  before_count=before, after_count=after, quality=quality,
                  expiring_calls=len(eligible), late_calls_excluded=late)
    if include_rows:
        result['calls'] = eligible
    return result


def bybit(path, day, kind):
    raw = strict(path.read_bytes())
    need(type(raw.get('retCode')) is int and raw['retCode'] == 0, 'BYBIT_API')
    result = raw['result']
    need(result['category'] == 'linear', 'BYBIT_CATEGORY')
    rows = result['list']
    start = ms(day+'T00:00:00Z')
    funding = kind == 'funding'
    times = [stamp(r['fundingRateTimestamp'] if funding else r[0]) for r in rows]
    need(times and times == sorted(set(times), reverse=True), 'BYBIT_ORDER_UNIQUE')
    need(all(start <= t < start+DAY for t in times), 'BYBIT_TIME')
    if funding:
        need(len(rows) < 200 and all(r['symbol'] == 'BTCUSDT' for r in rows), 'FUNDING_IDENTITY')
        for r in rows:
            need(abs(number(r['fundingRate'])) < 1, 'FUNDING_VALUE')
        asc = sorted(times)
        need(asc[0]-start < 8*3600000 and start+DAY-asc[-1] <= 8*3600000 and
             all(0 < y-x <= 8*3600000 for x,y in zip(asc,asc[1:])), 'FUNDING_COVERAGE')
        need(all(t%STEP == 0 for t in times), 'FUNDING_MARK_GRID')
    else:
        need(kind in ('trade','mark') and result['symbol'] == 'BTCUSDT', 'KLINE_IDENTITY')
        need(set(times) == set(range(start,start+DAY,STEP)), 'KLINE_FULL_GRID')
        for r in rows:
            need(len(r) == (7 if kind == 'trade' else 5), 'KLINE_SCHEMA')
            op,hi,lo,cl = map(number,r[1:5])
            need(0 < lo <= min(op,cl) <= max(op,cl) <= hi, 'OHLC')
            if kind == 'trade':
                need(number(r[5]) >= 0 and number(r[6]) >= 0, 'VOLUME')
    return rows


def probe():
    need(time.time()*1000-ms(frozen()['started_utc']) <= 2*3600000, 'FEASIBILITY_TWO_HOURS')
    c = Collector(limit=DOC_GETS+20)
    reports = []
    for day in ANCHORS:
        report = options(c.get(day+'-options', specs(day)['options']), day)
        need(report['quality'] and report['expiring_calls'] > 0, 'ANCHOR_DATA_FEASIBILITY')
        reports.append(report)
        print(json.dumps(report), flush=True)
    for kind in ('trade','mark','funding'):
        rows = bybit(c.get(DATES[0]+'-'+kind,specs(DATES[0])[kind]), DATES[0], kind)
        print(json.dumps(dict(kind=kind, rows=len(rows))), flush=True)
    meta = metadata(c.get('metadata', METADATA))
    save_new(RUN/'probe.json', dict(verdict='FEASIBILITY_ONLY_NOT_ECONOMIC', anchors=reports,
                                   metadata_keys=sorted(meta)))
    print(json.dumps(dict(metadata_keys=sorted(meta))), flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=('freeze','probe'))
    args = p.parse_args()
    {'freeze':freeze,'probe':probe}[args.command]()


if __name__ == '__main__':
    main()
