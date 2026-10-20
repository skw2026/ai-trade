"""One accepted BTC minute intake. No credentials, trading or economic evaluation."""
import argparse
from array import array
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit, parse_qs

from mvp_reference_inputs import strict_json, candle
from aave_market_recovery import unpack

ROOT = Path('.artifacts/cross-asset-relative-strength-20261020')
PLAN = Path('docs/plans/2026-10-19-cross-asset-relative-strength.md')
PROPOSAL = PLAN.with_suffix('.proposal.json')
REVIEW = Path('docs/reviews/2026-10-19-cross-asset-plan-review.md')
ACCEPTANCE = Path('docs/plans/2026-10-20-cross-asset-execution.md')
EXPECTED = {
    str(PLAN): '76bc2611ca55ab77d0f576e0280f933fb245d360b174173d349d16296668fd32',
    str(PROPOSAL): '536d5049dfa7e87a485d09e35bfc73e9d93660ba4d634a3ff72c3e46cf7dd7e0',
    str(REVIEW): 'bac82dbda8fb97f67bc52967d779af052b8c01788ec572b8c94ae6d6cf6104db'}
MINUTE = 60000
START, END = 1704063600000, 1735689600000
DEADLINE = '2026-10-20T06:38:00+00:00'
WIRE = dict(maximum_body_bytes=1024*1024, maximum_header_bytes=32768)


def need(ok, why):
    if not ok:
        raise ValueError(why)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    with Path(path).open('x') as f:
        json.dump(value, f, indent=2, sort_keys=True, allow_nan=False)
        f.write('\n')


def read(path):
    return strict_json(Path(path).read_bytes())


def now():
    return datetime.now(timezone.utc).isoformat()


def sources():
    return {p: sha(p) for p in (
        'tools/cross_asset_inputs.py', 'tools/test_cross_asset_inputs.py',
        'tools/mvp_reference_inputs.py', 'tools/aave_market_recovery.py',
        'tools/validation_gate.py')}


def guard():
    need(read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'RUNNING_GATE_REQUIRED')
    need(datetime.now(timezone.utc) < datetime.fromisoformat(DEADLINE), 'INPUT_TIME_BUDGET')
    for p, h in EXPECTED.items():
        need(sha(p) == h, 'ACCEPTED_DESIGN_CHANGED')
    if (ROOT/'input-freeze.json').exists():
        f = read(ROOT/'input-freeze.json')
        need(f['sources'] == sources(), 'INPUT_IMPLEMENTATION_CHANGED')
        need(f['acceptance_sha256'] == sha(ACCEPTANCE), 'ACCEPTANCE_CHANGED')


def preserve():
    f = read(ROOT/'input-freeze.json')
    for p, h in f['protected'].items():
        need(sha(p) == h, 'PROTECTED_CHANGED:'+p)
    return len(f['protected'])


def requests():
    pages = []
    for kind, endpoint in [('trade', 'kline'), ('mark', 'mark-price-kline')]:
        for i, begin in enumerate(range(START, END, 1000*MINUTE)):
            stop = min(END, begin+1000*MINUTE)
            q = dict(category='linear', symbol='BTCUSDT', interval='1',
                     start=begin, end=stop-1, limit=1000)
            pages.append(dict(id=f'{kind}-{i:03d}', kind=kind, index=i,
                              start=begin, stop=stop,
                              url='https://api.bybit.com/v5/market/'+endpoint+'?'+urlencode(q)))
    controls = [r for r in pages if r['index'] in (0, 263, 527)]
    return controls+[r for r in pages if r not in controls]


def validate(raw, request):
    obj = strict_json(raw)
    need(type(obj.get('retCode')) is int and obj['retCode'] == 0, 'API_ERROR')
    result = obj['result']
    need(result['category'] == 'linear' and result['symbol'] == 'BTCUSDT', 'MARKET_IDENTITY')
    need(type(obj['time']) is int and obj['time'] >= request['stop'], 'SERVER_CLOCK')
    rows = result['list']
    need(isinstance(rows, list) and 0 < len(rows) <= 1000, 'PAGE_SIZE')
    normalized = [candle(row, request['kind']) for row in rows]
    expected = list(range(request['start'], request['stop'], MINUTE))[::-1]
    need([r[0] for r in normalized] == expected, 'MINUTE_GRID_OR_ORDER')
    return normalized


def old_btc():
    manifest_path = Path('.artifacts/weekly-momentum-screen-20260921/source-manifest.json')
    manifest = read(manifest_path)
    streams = {k: {} for k in ('trade', 'mark', 'funding')}
    for rec in manifest['responses'].values():
        path = manifest_path.parent/rec['path']
        need(sha(path) == rec['sha256'], 'OLD_BTC_RESPONSE_CHANGED')
        u = urlsplit(rec['url']); q = parse_qs(u.query)
        need(u.scheme == 'https' and u.hostname == 'api.bybit.com' and
             q['symbol'] == ['BTCUSDT'] and q['category'] == ['linear'], 'OLD_BTC_IDENTITY')
        kind = ('funding' if u.path.endswith('/funding/history') else
                'mark' if u.path.endswith('/mark-price-kline') else 'trade')
        obj = read(path)
        need(obj['retCode'] == 0, 'OLD_BTC_CODE')
        if kind != 'funding':
            need(q['interval'] == ['60'] and obj['result']['symbol'] == 'BTCUSDT', 'OLD_BTC_INTERVAL')
        for row in obj['result']['list']:
            t = int(row['fundingRateTimestamp']) if kind == 'funding' else int(row[0])
            if t in streams[kind]:
                need(row == streams[kind][t], 'OLD_BTC_CONFLICT')
            streams[kind][t] = row
    return streams


def compare_hours(rows, hourly, kind):
    """Exact original Decimal OHLC and volume. Incomplete edge hours omitted only here."""
    grouped = defaultdict(list)
    for t, v in rows:
        grouped[t//3600000*3600000].append((t, v))
    mismatches = []
    compared = 0
    for t, minute in sorted(grouped.items()):
        if len(minute) != 60:
            continue
        minute.sort()
        need([x[0] for x in minute] == list(range(t, t+3600000, MINUTE)), 'HOUR_GRID')
        old = hourly[t]
        vals = [[Decimal(s) for s in v] for _, v in minute]
        agg = [vals[0][0], max(v[1] for v in vals), min(v[2] for v in vals), vals[-1][3]]
        expected = [Decimal(s) for s in old[1:5]]
        if kind == 'trade':
            agg.append(sum(v[4] for v in vals))
            expected.append(Decimal(old[5]))
        for field, actual, wanted in zip(('open', 'high', 'low', 'close', 'volume'), agg, expected):
            if actual != wanted:
                mismatches.append(dict(hour=t, field=field, minute=str(actual), hourly=str(wanted)))
        compared += 1
    return dict(compared_hours=compared, mismatches=mismatches)


def freeze():
    os.umask(0o077)
    guard()
    protected = dict(read('.artifacts/open-market-selective-20261019/protected.json')['files'])
    protected.update(read('.artifacts/open-market-selective-20261019/freeze.json')['files'])
    protected.update(read('.artifacts/cross-asset-plan-review-20261019/preservation.json')['protected'])
    for root in (Path('.artifacts/open-market-selective-20261019'),
                 Path('.artifacts/cross-asset-plan-review-20261019')):
        for p in root.rglob('*'):
            if p.is_file() and p.suffix != '.lock' and '__pycache__' not in p.parts:
                protected[str(p)] = sha(p)
    protected.update(EXPECTED)
    for p, h in protected.items():
        need(sha(p) == h, 'PRIOR_CHANGED:'+p)
    req = requests()
    need(len(req) == 1056 and len({r['url'] for r in req}) == 1056, 'REQUEST_PLAN')
    (ROOT/'attempts').mkdir(mode=0o700)
    save(ROOT/'input-freeze.json', dict(at=now(), sources=sources(), protected=protected,
        acceptance_sha256=sha(ACCEPTANCE), requests=req, design_hours_charged=4,
        input_deadline=DEADLINE, economics_attempts=0, network_retry_limit=0))
    print(json.dumps(dict(status='ACCEPTED_INPUT_FROZEN', pages=len(req),
                          protected_bindings=len(protected))), flush=True)


def fetch(request):
    """One attempt only, even on partial output. Persist evidence before propagating failure."""
    folder = ROOT/'attempts'/request['id']
    folder.mkdir(mode=0o700)
    save(folder/'started.json', dict(at=now(), request=request))
    wire = b''; rec = dict(status='FAILED', request=request, at=now())
    started = time.monotonic()
    try:
        p = subprocess.run([
            'curl', '--disable', '--silent', '--proto', '=https', '--include',
            '--suppress-connect-headers', '--retry', '0', '--connect-timeout', '10',
            '--max-time', '30', '--max-filesize', str(WIRE['maximum_body_bytes']),
            '--write-out', '\n%{http_code}', request['url']],
            capture_output=True, timeout=35, env={k: v for k, v in os.environ.items()
                                               if k in ('PATH', 'TMPDIR', 'LANG')})
        wire = p.stdout
        rec['curl_returncode'] = p.returncode
        need(p.returncode == 0, 'TRANSPORT_FAILURE')
        http, _, _, body = unpack(wire, WIRE)
        rec['http'] = http
        need(http == 200, 'HTTP_FAILURE')
        normalized = validate(body, request)
        rec.update(status='VALID', rows=len(normalized))
    except Exception as e:
        if isinstance(e, subprocess.TimeoutExpired):
            wire = e.stdout or b''
        rec.update(error_type=type(e).__name__, error=str(e)[:160])
        raise
    finally:
        with (folder/'response.bin').open('xb') as f:
            f.write(wire)
        rec.update(wire_sha256=hashlib.sha256(wire).hexdigest(),
                   wire_bytes=len(wire), elapsed_seconds=time.monotonic()-started)
        save(folder/'record.json', rec)
    return rec


def retained(request):
    folder = ROOT/'attempts'/request['id']
    r = read(folder/'record.json')
    need(r['status'] == 'VALID' and r['request'] == request and
         read(folder/'started.json')['request'] == request, 'RETAINED_REQUEST')
    need(sha(folder/'response.bin') == r['wire_sha256'], 'RETAINED_WIRE')
    status, _, _, body = unpack((folder/'response.bin').read_bytes(), WIRE)
    need(status == 200, 'RETAINED_HTTP')
    return validate(body, request)


def collect(controls):
    os.umask(0o077)
    guard(); preserve()
    req = read(ROOT/'input-freeze.json')['requests']
    need(req == requests(), 'REQUEST_PLAN_CHANGED')
    chosen = req[:6] if controls else req[6:]
    if controls:
        need(not any((ROOT/'attempts').iterdir()), 'CONTROLS_ALREADY_ATTEMPTED')
    else:
        need(read(ROOT/'controls.json')['status'] == 'CONTROL_INPUT_PASS', 'CONTROLS_REQUIRED')
        need(len(list((ROOT/'attempts').iterdir())) == 6, 'COLLECTION_ALREADY_ATTEMPTED')
    raw_bytes = sum(p.stat().st_size for p in (ROOT/'attempts').glob('*/response.bin'))
    for n, request in enumerate(chosen):
        guard()
        need(raw_bytes+WIRE['maximum_body_bytes']+WIRE['maximum_header_bytes']+32 <= 268435456,
             'RAW_BYTE_BUDGET')
        need(len(list((ROOT/'attempts').iterdir())) < 1200, 'REQUEST_BUDGET')
        r = fetch(request)
        raw_bytes += r['wire_bytes']
        if controls or (n+1) % 50 == 0 or n+1 == len(chosen):
            print(json.dumps(dict(status='INPUT_COLLECTION', completed=n+1, total=len(chosen),
                                  raw_bytes=raw_bytes)), flush=True)
        time.sleep(0.1)
    hourly = old_btc()
    all_rows = {k: [] for k in ('trade', 'mark')}
    for r in (req[:6] if controls else req):
        all_rows[r['kind']].extend(retained(r))
    checks = {k: compare_hours(v, hourly[k], k) for k, v in all_rows.items()}
    # Keep the exact mismatch report; do not silently round or choose a vintage.
    clean = all(not c['mismatches'] and c['compared_hours'] > 0 for c in checks.values())
    result = dict(status=('CONTROL_INPUT_PASS' if controls else 'COLLECTED_NOT_COMPILED')
                  if clean else 'INPUT_VERSION_MISMATCH', checks=checks,
                  request_attempts=len(list((ROOT/'attempts').iterdir())),
                  wire_bytes=raw_bytes, economic_attempts=0)
    save(ROOT/('controls.json' if controls else 'collection.json'), result)
    need(clean, 'INPUT_VERSION_MISMATCH')
    preserve()
    print(json.dumps({k: v for k, v in result.items() if k != 'checks'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'controls', 'collect'))
    action = parser.parse_args().action
    if action == 'freeze':
        freeze()
    else:
        collect(action == 'controls')
