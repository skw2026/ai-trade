"""Bind original three-asset inputs to arrays and exact closed-daily feature prices.

No strategy or returns. Existing ETH/SOL arrays are read, never rewritten.
"""
from array import array
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time

import cross_asset_inputs as intake
from cross_asset_inputs import need, read, save, sha, ROOT, MINUTE, START, END
from mvp_reference_inputs import strict_json

DAY = 86400000
WARM = 1698883200000
ACCOUNT = 1704067200000
OLD = Path('.artifacts/eth-sol-program-20261015')
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')


def original_rows(raw, kind, symbol, start, stop, interval):
    """Separate field/grid interpretation, not the collector's validate function."""
    o = strict_json(raw)
    need(type(o['retCode']) is int and o['retCode'] == 0, 'AUDIT_API')
    r = o['result']
    need(r['category'] == 'linear' and r['symbol'] == symbol, 'AUDIT_IDENTITY')
    result = []
    width = 7 if kind == 'trade' else 5
    for v in r['list']:
        need(len(v) == width and all(isinstance(s, str) for s in v), 'AUDIT_ROW_SHAPE')
        t = int(v[0])
        prices = [Decimal(s) for s in v[1:5]]
        need(all(x.is_finite() and x > 0 for x in prices), 'AUDIT_FINITE_PRICE')
        op, high, low, close = prices
        need(low <= min(op, close) <= max(op, close) <= high, 'AUDIT_OHLC')
        if kind == 'trade':
            volumes = [Decimal(s) for s in v[5:]]
            need(all(x.is_finite() and x >= 0 for x in volumes), 'AUDIT_VOLUME')
        result.append((t, v[1:6] if kind == 'trade' else v[1:5]))
    need([t for t, _ in result] == list(range(start, stop, interval))[::-1], 'AUDIT_GRID')
    return result


def daily_accept(daily, symbol, t, interval, close):
    end = t+interval
    if end % DAY == 0:
        key = str(end)
        if key in daily[symbol]:
            need(Decimal(daily[symbol][key]) == Decimal(close), 'DAILY_SOURCE_CONFLICT')
        daily[symbol][key] = close


def verify_array(path, expected_hash, width, count):
    need(sha(path) == expected_hash and Path(path).stat().st_size == count*width*8,
         'COMPILED_IDENTITY')
    a = array('d')
    with Path(path).open('rb') as f:
        a.fromfile(f, count*width)
    return a


def main():
    os.umask(0o077)
    intake.guard()
    intake.preserve()
    from run_open_market_selective import preserve as preserve_history
    historical_bindings = preserve_history()
    collection_result = read(ROOT/'collection.json')
    need(collection_result['status'] == 'COLLECTED_NOT_COMPILED', 'COLLECTION_REQUIRED')
    need({p.name for p in (ROOT/'attempts').iterdir()} ==
         {r['id'] for r in intake.requests()}, 'EXACT_REQUEST_SET')
    wire_bytes = sum(p.stat().st_size for p in (ROOT/'attempts').glob('*/response.bin'))
    need(collection_result['request_attempts'] == 1056 and
         collection_result['wire_bytes'] == wire_bytes <= 268435456, 'ACTUAL_INPUT_BUDGET')
    save(ROOT/'input-compilation-attempt.json', dict(at=intake.now(), sources={
        p: sha(p) for p in ('tools/compile_cross_asset_inputs.py', 'tools/test_cross_asset_compile.py')},
        economics_attempts=0))
    begun = time.monotonic()
    n = (END-START)//MINUTE
    daily = {s: {} for s in SYMBOLS}
    funding = {}
    files = {}
    raw_bindings = {}
    btc = intake.old_btc()
    for t, row in btc['trade'].items():
        if WARM <= t < END:
            daily_accept(daily, 'BTCUSDT', t, 3600000, row[4])
    funding['BTCUSDT'] = {str(t): v['fundingRate'] for t, v in btc['funding'].items()
                          if ACCOUNT <= t < END}
    need(len(funding['BTCUSDT']) == 1098, 'BTC_FUNDING_COVERAGE')
    output = ROOT/'compiled'
    output.mkdir(mode=0o700)
    reqs = read(ROOT/'input-freeze.json')['requests']
    need(reqs == intake.requests(), 'FIXED_REQUEST_IDENTITY')
    for kind in ('trade', 'mark'):
        width = 5 if kind == 'trade' else 4
        a = array('d', [0.0])*(n*width); seen = bytearray(n)
        for req in (r for r in reqs if r['kind'] == kind):
            folder = ROOT/'attempts'/req['id']
            rec = read(folder/'record.json'); wire_path = folder/'response.bin'
            need(rec['status'] == 'VALID' and rec['request'] == req and
                 read(folder/'started.json')['request'] == req, 'BTC_ATTEMPT_BINDING')
            need(sha(wire_path) == rec['wire_sha256'] and
                 wire_path.stat().st_size == rec['wire_bytes'], 'BTC_WIRE_BINDING')
            status, _, _, body = intake.unpack(wire_path.read_bytes(), intake.WIRE)
            need(status == 200, 'BTC_HTTP')
            rows = original_rows(body, kind, 'BTCUSDT', req['start'], req['stop'], MINUTE)
            for t, values in rows:
                idx = (t-START)//MINUTE
                need(0 <= idx < n and not seen[idx], 'BTC_DUPLICATE')
                seen[idx] = 1
                a[idx*width:(idx+1)*width] = array('d', map(float, values))
                if kind == 'trade':
                    daily_accept(daily, 'BTCUSDT', t, MINUTE, values[3])
            for p in folder.iterdir():
                raw_bindings[str(p)] = sha(p)
        need(all(seen), 'BTC_MINUTE_GAP')
        path = output/f'BTCUSDT-{kind}.f64'
        with path.open('xb') as f:
            a.tofile(f)
        files[str(path)] = sha(path)
    # Read every original ETH/SOL response and compare against the reused arrays.
    import eth_sol_market as old_market
    from audit_eth_sol_input import verify_records
    f, collection = read(OLD/'market-freeze.json'), read(OLD/'market-collection.json')
    old_market.verify_freeze(f)
    raw_audit = verify_records(OLD, f, collection)
    need(raw_audit['files'] == read(OLD/'collection-audit.json')['files'], 'ETH_SOL_COLLECTION_CHANGED')
    raw_bindings.update(raw_audit['files'])
    old_audit = read(OLD/'input-audit.json')
    need(old_audit['byteorder'] == sys.byteorder == 'little', 'ARRAY_BYTEORDER')
    arrays = {}
    for s in SYMBOLS[1:]:
        for kind in ('trade', 'mark'):
            path = OLD/'compiled'/f'{s}-{kind}.f64'
            h = old_audit['files'][str(path)]
            arrays[s, kind] = verify_array(path, h, 5 if kind == 'trade' else 4, n)
            files[str(path)] = h
    seen = {key: bytearray(n) for key in arrays}
    funds = {s: {} for s in SYMBOLS[1:]}
    warm = {s: {} for s in SYMBOLS[1:]}
    raw_fields = 0
    def accept(r, body):
        nonlocal raw_fields
        s, kind = r['symbol'], r['kind']
        if kind == 'funding':
            # Old pure strict funding parser; independently compare its result to frozen values.
            for t, value in old_market.validate(body, r):
                need(str(t) not in funds[s], 'DUPLICATE_FUNDING')
                funds[s][str(t)] = value
            return
        interval = r['interval_minutes']*MINUTE
        for t, values in original_rows(body, kind, s, r['start'], r['end']+1, interval):
            if interval == 3600000:
                need(str(t) not in warm[s], 'WARMUP_DUPLICATE')
                warm[s][str(t)] = values[3]
            else:
                idx = (t-START)//MINUTE
                width = 5 if kind == 'trade' else 4
                need(0 <= idx < n and not seen[s, kind][idx], 'ETH_SOL_DUPLICATE')
                seen[s, kind][idx] = 1
                need(array('d', map(float, values)) == arrays[s, kind][idx*width:(idx+1)*width],
                     'RAW_ARRAY_DISAGREEMENT')
                raw_fields += width
            if kind == 'trade':
                daily_accept(daily, s, t, interval, values[3])
    for i, r0 in enumerate(f['reuse_requests']):
        req = dict(r0, symbol='ETHUSDT', interval_minutes=0 if r0['kind'] == 'funding' else 1)
        folder = Path(f['reuse_folders'][str(i)])
        accept(req, old_market.raw_page(folder, r0, True))
        for p in folder.iterdir():
            if p.is_file():
                raw_bindings[str(p)] = sha(p)
    for i, req in enumerate(f['requests']):
        accept(req, old_market.raw_page(collection['records'][str(i)]['folder'], req))
    need(all(all(v) for v in seen.values()), 'ETH_SOL_RAW_GAP')
    need(funds == read(OLD/'compiled/funding.json'), 'FUNDING_EXACT_MAPPING')
    need(warm == read(OLD/'compiled/warmup.json'), 'WARMUP_EXACT_MAPPING')
    funding.update(funds)
    expected = list(range(WARM+DAY, END+1, DAY))
    need(all(sorted(map(int, d)) == expected for d in daily.values()), 'DAILY_COVERAGE')
    for name, value in [('daily.json', daily), ('funding.json', funding)]:
        path = output/name
        save(path, value); files[str(path)] = sha(path)
    save(ROOT/'raw-input-bindings.json', dict(files=raw_bindings))
    intake.guard()
    intake.preserve()
    need(preserve_history() == historical_bindings, 'PRESERVATION_CHAIN')
    save(ROOT/'input-audit.json', dict(status='THREE_ASSET_INPUT_COMPLETE_DEVELOPMENT_ONLY',
        files=files, raw_bindings_sha256=sha(ROOT/'raw-input-bindings.json'),
        minute_rows_per_asset_kind=n, daily_rows_per_asset=len(expected),
        funding_counts={s: len(funding[s]) for s in SYMBOLS}, byteorder=sys.byteorder,
        start_ms=START, end_ms=END, original_eth_sol_fields_compared=raw_fields,
        historical_protected_bindings=historical_bindings, new_market_gets=1056,
        economics_attempts=0, compilation_seconds=time.monotonic()-begun))
    print(json.dumps(dict(status='THREE_ASSET_INPUT_COMPLETE_DEVELOPMENT_ONLY',
         minute_rows_per_asset_kind=n, original_fields_compared=raw_fields,
         daily_rows_per_asset=len(expected), historical_bindings=historical_bindings)), flush=True)


if __name__ == '__main__':
    main()
