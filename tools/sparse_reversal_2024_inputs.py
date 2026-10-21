"""Exactly two accepted public warmup pages; immutable original evidence."""
import argparse
from array import array
import csv
from datetime import datetime, timezone
from decimal import Decimal as D
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode

from sparse_reversal_2024_intake import WARM, START, END, BAR, MODEL, MODEL_SHA, ARCHIVE, RUN, PLAN
from cross_asset_inputs import need, read, save, sha, now, unpack, WIRE, old_btc
from compile_cross_asset_inputs import original_rows, verify_array
from mvp_reference_inputs import strict_json, candle, FIELDS

OUT = RUN/'continuation'
AMEND = Path('docs/plans/2026-10-21-sparse-reversal-warmup-amendment.md')
JOIN = START-3600000
DEADLINE = '2026-10-21T14:19:00+00:00'
SOURCES = ('tools/sparse_reversal_2024_inputs.py', 'tools/test_sparse_reversal_2024_inputs.py',
           'tools/sparse_reversal_2024_intake.py', 'tools/cross_asset_inputs.py',
           'tools/compile_cross_asset_inputs.py', 'tools/mvp_reference_inputs.py',
           'tools/aave_market_recovery.py')


def guard():
    need(read(RUN/'validation-state.json')['status'] == 'RUNNING', 'RUNNING_GATE_REQUIRED')
    need(datetime.now(timezone.utc) < datetime.fromisoformat(DEADLINE), 'TOTAL_ENGINEERING_BUDGET')
    need(sha(MODEL) == MODEL_SHA, 'MODEL_CHANGED')
    if (OUT/'input-freeze.json').exists():
        for p, h in read(OUT/'input-freeze.json')['files'].items():
            need(sha(p) == h, 'FROZEN_INPUT_SOURCE_CHANGED:'+p)


def requests():
    return [dict(kind=kind, start=WARM, stop=JOIN, url='https://api.bybit.com/v5/market/'+endpoint+'?'+
            urlencode(dict(category='linear', symbol='BTCUSDT', interval='5', start=WARM,
                           end=JOIN-1, limit=1000)))
            for kind, endpoint in [('trade', 'kline'), ('mark', 'mark-price-kline')]]


def validate(raw, request):
    o = strict_json(raw)
    need(type(o['retCode']) is int and o['retCode'] == 0, 'API_CODE')
    r = o['result']
    need(r['category'] == 'linear' and r['symbol'] == 'BTCUSDT', 'IDENTITY')
    need(type(o['time']) is int and o['time'] >= request['stop'], 'SERVER_CLOCK')
    rows = [candle(v, request['kind']) for v in r['list']]
    need([t for t, _ in rows] == list(range(request['start'], request['stop'], BAR))[::-1], 'GRID')
    return rows


def freeze():
    guard()
    previous = read(RUN/'intake-result.json')
    need(previous['status'] == 'INPUT_INSUFFICIENT' and previous['missing_warmup_bars'] == 276,
         'PRIOR_INTAKE_IDENTITY')
    need(sha(PLAN) == previous['plan_sha256'], 'ORIGINAL_PLAN_CHANGED')
    from run_open_market_selective import preserve
    need(preserve() == 8070, 'HISTORY_CHANGED')
    protected = [str(p) for p in RUN.iterdir() if p.is_file() and p.suffix != '.lock'
                 and p.name != 'validation-state.json']
    files = {p: sha(p) for p in list(SOURCES)+protected+[str(PLAN), str(AMEND), str(MODEL),
             str(ARCHIVE/'input-audit.json'), str(ARCHIVE/'raw-input-bindings.json'),
             str(ARCHIVE/'input-freeze.json'), '.artifacts/weekly-momentum-screen-20260921/source-manifest.json']}
    OUT.mkdir(mode=0o700)
    save(OUT/'input-freeze.json', dict(at=now(), files=files, requests=requests(),
         previous_hours_charged=2, remaining_hours=6, deadline=DEADLINE, requests_limit=2,
         economic_attempts=0, training_vectors=0))
    print('TWO_WARMUP_REQUESTS_FROZEN; original intake and 8070 bindings unchanged', flush=True)


def fetch(r):
    folder = OUT/r['kind']
    folder.mkdir(mode=0o700)
    save(folder/'started.json', dict(at=now(), request=r))
    rec = dict(request=r, status='FAILED'); wire = b''
    try:
        p = subprocess.run(['curl', '--disable', '--silent', '--proto', '=https', '--include',
            '--suppress-connect-headers', '--retry', '0', '--connect-timeout', '10',
            '--max-time', '30', '--max-filesize', str(WIRE['maximum_body_bytes']),
            '--write-out', '\n%{http_code}', r['url']], capture_output=True, timeout=35,
            env={k: v for k, v in os.environ.items() if k in ('PATH', 'TMPDIR', 'LANG')})
        wire = p.stdout; rec['curl_returncode'] = p.returncode
        need(p.returncode == 0, 'TRANSPORT')
        status, _, _, body = unpack(wire, WIRE); rec['http'] = status
        need(status == 200, 'HTTP')
        rows = validate(body, r)
        rec.update(status='VALID', rows=len(rows))
    except Exception as e:
        if isinstance(e, subprocess.TimeoutExpired): wire = e.stdout or b''
        rec.update(error_type=type(e).__name__, error=str(e)[:160])
        raise
    finally:
        with (folder/'response.bin').open('xb') as f: f.write(wire)
        rec.update(wire_sha256=sha(folder/'response.bin'), wire_bytes=len(wire))
        save(folder/'record.json', rec)
    print(json.dumps(dict(kind=r['kind'], status='VALID', rows=len(rows))), flush=True)


def collect():
    guard()
    need(read(OUT/'input-freeze.json')['requests'] == requests(), 'FIXED_REQUESTS')
    need(not any((OUT/r['kind']).exists() for r in requests()), 'COLLECTION_ALREADY_ATTEMPTED')
    for r in requests():
        guard(); fetch(r)


def aggregate(values):
    """OHLC and optional volume, in chronological order, exact decimal arithmetic."""
    need(len(values) > 0, 'EMPTY_AGGREGATE')
    rows = [[D(str(x)) for x in v] for v in values]
    width = len(rows[0])
    need(width in (4, 5) and all(len(v) == width for v in rows), 'AGGREGATE_WIDTH')
    out = [rows[0][0], max(v[1] for v in rows), min(v[2] for v in rows), rows[-1][3]]
    if width == 5: out.append(sum(v[4] for v in rows))
    return [str(v) for v in out]


def compile_inputs():
    guard()
    audit = read(ARCHIVE/'input-audit.json')
    need(audit['start_ms'] == JOIN and audit['end_ms'] == END and audit['byteorder'] == sys.byteorder == 'little',
         'OLD_DOMAIN')
    raw_bindings = read(ARCHIVE/'raw-input-bindings.json')['files']
    need(all(sha(p) == h for p, h in raw_bindings.items()), 'OLD_RAW_CHANGED')
    save(OUT/'compilation-attempt.json', dict(at=now(), economics_attempts=0))
    streams = {}; bindings = {}; compared = 0
    for r in requests():
        kind = r['kind']; width = 5 if kind == 'trade' else 4
        folder = OUT/kind; rec = read(folder/'record.json')
        need(rec['request'] == r and rec['status'] == 'VALID' and rec['curl_returncode'] == 0 and
             read(folder/'started.json')['request'] == r, 'NEW_RECEIPT')
        raw_path = folder/'response.bin'
        need(sha(raw_path) == rec['wire_sha256'], 'NEW_WIRE_CHANGED')
        status, _, _, body = unpack(raw_path.read_bytes(), WIRE)
        need(status == 200, 'NEW_HTTP')
        first = validate(body, r)
        separate = original_rows(body, kind, 'BTCUSDT', WARM, JOIN, BAR)
        need([(t, [D(x) for x in v[:width]]) for t, v in first] ==
             [(t, [D(x) for x in v]) for t, v in separate], 'INDEPENDENT_NEW_PARSE')
        stream = {t: v for t, v in separate}
        a_path = ARCHIVE/'compiled'/('BTCUSDT-'+kind+'.f64')
        n = (END-JOIN)//60000
        a = verify_array(a_path, audit['files'][str(a_path)], width, n)
        pending = []; expected_ts = JOIN
        # Control-first collection order is not time order.
        pages = sorted((q for q in read(ARCHIVE/'input-freeze.json')['requests'] if q['kind'] == kind),
                       key=lambda q: q['start'])
        for q in pages:
            old = ARCHIVE/'attempts'/q['id']; receipt = read(old/'record.json')
            need(receipt['status'] == 'VALID' and receipt['request'] == q and
                 read(old/'started.json')['request'] == q, 'OLD_RECEIPT')
            need(sha(old/'response.bin') == receipt['wire_sha256'], 'OLD_WIRE')
            http, _, _, data = unpack((old/'response.bin').read_bytes(), WIRE)
            need(http == 200, 'OLD_HTTP')
            rows = original_rows(data, kind, 'BTCUSDT', q['start'], q['stop'], 60000)
            for t, v in reversed(rows):
                need(t == expected_ts, 'MINUTE_CONTINUITY'); expected_ts += 60000
                idx = (t-JOIN)//60000
                need(array('d', map(float, v)) == a[idx*width:(idx+1)*width], 'ARRAY_RAW_MISMATCH')
                compared += width; pending.append(v)
                if len(pending) == 5:
                    ts = t-240000
                    stream[ts] = aggregate(pending); pending = []
        need(expected_ts == END and not pending, 'MINUTE_END')
        need(sorted(stream) == list(range(WARM, END, BAR)), 'FULL_FIVE_MINUTE_GRID')
        streams[kind] = stream
        bindings.update({str(p): sha(p) for p in folder.iterdir()})
    hourly = old_btc()
    for kind in streams:
        for t in range(WARM, END, 3600000):
            actual = aggregate([streams[kind][s] for s in range(t, t+3600000, BAR)])
            old = hourly[kind][t][1:6 if kind == 'trade' else 5]
            need([D(v) for v in actual] == [D(v) for v in old], 'HOURLY_CROSSCHECK')
    funding = {t: v['fundingRate'] for t, v in hourly['funding'].items() if WARM <= t < END}
    need(sorted(funding) == list(range(WARM, END, 28800000)), 'FUNDING_EVENT_GRID')
    funding_path = ARCHIVE/'compiled/funding.json'
    need(sha(funding_path) == audit['files'][str(funding_path)], 'FUNDING_HASH')
    need({str(t): v for t, v in funding.items() if t >= START} == read(funding_path)['BTCUSDT'], 'FUNDING_MAPPING')
    with (OUT/'replay.csv').open('x', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator='\n'); writer.writeheader()
        for t in range(WARM, END, BAR):
            o, h, l, c, v = streams['trade'][t]; mo, mh, ml, mc = streams['mark'][t]
            writer.writerow(dict(zip(FIELDS, [t, 'BTCUSDT', o, h, l, c, v, BAR,
                funding.get(t, '0'), mo, mc, mh, ml, int(t >= START)])))
    guard()
    save(OUT/'input-proof.json', dict(status='COMPLETE_DEVELOPMENT_INPUT', csv_sha256=sha(OUT/'replay.csv'),
         new_bindings=bindings, input_freeze_sha256=sha(OUT/'input-freeze.json'), original_fields_compared=compared,
         warmup_bars=288, evaluation_bars=(END-START)//BAR, hourly_crosschecks_per_kind=(END-WARM)//3600000,
         funding_events=len(funding), public_gets=2, economic_attempts=0, qualification=False))
    print('INPUT_COMPLETE; 288 warmup + 105408 evaluation bars; raw/array/hourly agreement', flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('freeze', 'collect', 'compile'))
    {'freeze': freeze, 'collect': collect, 'compile': compile_inputs}[parser.parse_args().action]()
