"""One fixed public market archive; no credentials, strategy selection or retry loop."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode

from mvp_reference_inputs import candle, number, strict_json, timestamp
from aave_economic_model import require, MINUTE

ROOT = Path('.artifacts/aave-economic-screen-20261013')
PRIOR = Path('.artifacts/aave-cloud-input-20261013')
CONTRACT = Path('docs/plans/2026-10-13-aave-economic-screen.contract.json')
PLAN = CONTRACT.with_name('2026-10-13-aave-economic-screen.md')
SOURCE_NAMES = ('aave_market_input.py', 'aave_economic_model.py', 'aave_economic_screen.py',
    'audit_aave_economic_screen.py', 'test_aave_economic_model.py', 'test_aave_market_input.py',
    'mvp_reference_inputs.py', 'validation_gate.py')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open('x') as handle:
        json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write('\n')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sources():
    return {str(Path('tools', name)): sha(Path('tools', name)) for name in SOURCE_NAMES}


def guard():
    c = strict_json(CONTRACT.read_bytes())
    state = strict_json((ROOT/'validation-state.json').read_bytes())
    require(state['status'] == 'RUNNING', 'ECONOMIC_RUNNING_GATE_REQUIRED')
    require(dt.datetime.fromisoformat(c['started_utc'].replace('Z', '+00:00')) <= dt.datetime.now(dt.timezone.utc)
            < dt.datetime.fromisoformat(c['deadline_utc'].replace('Z', '+00:00')), 'ECONOMIC_STAGE_DEADLINE')
    if (ROOT/'freeze.json').exists():
        f = strict_json((ROOT/'freeze.json').read_bytes())
        require(f['contract_sha256'] == sha(CONTRACT) and f['plan_sha256'] == sha(PLAN), 'ECONOMIC_CONTRACT_CHANGED')
        require(f['sources'] == sources(), 'ECONOMIC_SOURCE_CHANGED')
    return c


def preserve():
    protected = strict_json((ROOT/'freeze.json').read_bytes())['protected']
    require(all(Path(p).is_file() and sha(p) == h for p, h in protected.items()), 'ECONOMIC_PREDECESSOR_CHANGED')
    return len(protected)


def requests(c):
    result = {kind: [] for kind in ('trade', 'mark', 'funding')}
    for kind in result:
        funding = kind == 'funding'
        start = c['evaluation_start_ms'] if funding else c['market_start_ms']
        end = c['evaluation_end_ms']
        step = 86400000 if funding else 1000*MINUTE
        limit = 200 if funding else 1000
        endpoint = {'trade': 'kline', 'mark': 'mark-price-kline', 'funding': 'funding/history'}[kind]
        for begin in range(start, end, step):
            stop = min(end, begin+step)-1
            params = dict(category='linear', symbol='ETHUSDT', limit=limit)
            params.update({'startTime' if funding else 'start': begin, 'endTime' if funding else 'end': stop})
            if not funding:
                params['interval'] = '1'
            result[kind].append(dict(kind=kind, start=begin, end=stop, limit=limit,
                url='https://api.bybit.com/v5/market/'+endpoint+'?'+urlencode(params)))
    order = [result[k][0] for k in ('funding', 'trade', 'mark')]
    order.extend(result['funding'][1:])
    for trade, mark in zip(result['trade'][1:], result['mark'][1:]):
        order.extend((trade, mark))
    require(len(result['trade']) == len(result['mark']), 'MARKET_PAGE_PAIRING')
    return order


def validate(raw, request):
    data = strict_json(raw)
    require(type(data.get('retCode')) is int and data['retCode'] == 0, 'MARKET_API_ERROR')
    result = data['result']
    require(result['category'] == 'linear', 'MARKET_CATEGORY')
    require(timestamp(data['time']) > request['end'], 'MARKET_NOT_HISTORICAL')
    funding = request['kind'] == 'funding'
    if not funding:
        require(result['symbol'] == 'ETHUSDT', 'MARKET_SYMBOL')
    rows = result['list']
    require(isinstance(rows, list), 'MARKET_LIST')
    times = []
    normalized = []
    for row in rows:
        if funding:
            require(row['symbol'] == 'ETHUSDT', 'FUNDING_SYMBOL')
            t = timestamp(row['fundingRateTimestamp'])
            values = str(number(row['fundingRate']))
        else:
            t, values = candle(row, request['kind'])
            values = values[:4]
        times.append(t)
        normalized.append((t, values))
    require(all(request['start'] <= t <= request['end'] for t in times), 'MARKET_TIMESTAMP_RANGE')
    require(times == sorted(set(times), reverse=True), 'MARKET_ORDER_OR_DUPLICATE')
    if funding:
        require(0 < len(rows) < request['limit'], 'FUNDING_EMPTY_OR_CENSORED')
    else:
        require(times == list(range(request['start'], request['end']+1, MINUTE))[::-1], 'MINUTE_GRID_INCOMPLETE')
        require(len(rows) <= request['limit'], 'MARKET_PAGE_LIMIT')
    return normalized


def freeze():
    os.umask(0o077)
    c = guard()
    require(not (ROOT/'freeze.json').exists(), 'ECONOMIC_ALREADY_FROZEN')
    import aave_cloud_local as previous
    previous.seed()
    require(sha(PRIOR/'causal-capacity-private.json') == c['causal_schedule_sha256']
            and sha(PRIOR/'causal-capacity-audit.json') == c['capacity_audit_sha256'], 'CAUSAL_CAPACITY_BINDING')
    require(strict_json((PRIOR/'causal-capacity-summary.json').read_bytes())['proposal_minimum_capacity'], 'CAPACITY_NOT_PASSED')
    protected = strict_json((PRIOR/'protected.json').read_bytes())
    for path in PRIOR.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            protected[str(path)] = sha(path)
    plan = requests(c)
    require(len(plan) == c['planned_market_gets'] == 1422, 'MARKET_REQUEST_COUNT')
    require(c['prior_operations_upper_bound']+c['maximum_new_market_attempts']+c['new_document_control_plane_reserve'] <= c['maximum_total_operations'], 'TOTAL_OPERATION_BUDGET')
    require(c['prior_logical_input_upper_bound']+c['maximum_new_market_raw_bytes'] < c['maximum_total_input_bytes'], 'INPUT_BYTE_BUDGET')
    require(c['prior_active_seconds_reserved']+c['remaining_active_seconds'] == c['maximum_original_active_seconds'], 'ACTIVE_BUDGET')
    (ROOT/'attempts').mkdir(mode=0o700)
    save(ROOT/'freeze.json', dict(contract_sha256=sha(CONTRACT), plan_sha256=sha(PLAN),
        sources=sources(), protected=protected, requests=plan, frozen_utc=now()))
    print(json.dumps(dict(status='ECONOMIC_AND_MARKET_CONTRACT_FROZEN_BEFORE_PRICES',
        planned_gets=len(plan), protected_files=preserve(), prices=0, economic_attempts=0)), flush=True)


def load_pages():
    f = strict_json((ROOT/'freeze.json').read_bytes())
    cache = {}
    failures = []
    total_bytes = 0
    directories = sorted((ROOT/'attempts').iterdir())
    for directory in directories:
        require((directory/'record.json').is_file(), 'INTERRUPTED_MARKET_ATTEMPT')
        rec = strict_json((directory/'record.json').read_bytes())
        raw = (directory/'response.json').read_bytes()
        require(hashlib.sha256(raw).hexdigest() == rec['sha256'], 'MARKET_RESPONSE_CHANGED')
        total_bytes += len(raw)
        if rec['status'] != 'VALID':
            failures.append(rec)
            continue
        i = rec['index']
        require(i not in cache and rec['request'] == f['requests'][i], 'MARKET_CACHE_IDENTITY')
        validate(raw, rec['request'])
        cache[i] = (directory, rec)
    return cache, failures, len(directories), total_bytes


def collect(limit):
    os.umask(0o077)
    c = guard()
    preserve()
    f = strict_json((ROOT/'freeze.json').read_bytes())
    cache, failures, attempts, total_bytes = load_pages()
    state = strict_json((ROOT/'validation-state.json').read_bytes())
    require(not failures or bool(state['active'].get('review_path')), 'MARKET_RETRY_REQUIRES_REVIEW')
    require(limit in (3, c['planned_market_gets']), 'FIXED_MARKET_STAGE')
    last_start = 0.0
    for i, req in enumerate(f['requests'][:limit]):
        if i in cache:
            continue
        guard()
        require(attempts < c['maximum_new_market_attempts'], 'MARKET_ATTEMPT_BUDGET')
        require(total_bytes+c['maximum_response_bytes'] <= c['maximum_new_market_raw_bytes'], 'MARKET_BYTE_BUDGET')
        time.sleep(max(0.0, 1-(time.monotonic()-last_start)))
        folder = ROOT/'attempts'/('%04d' % attempts)
        folder.mkdir(mode=0o700)
        save(folder/'started.json', dict(index=i, request=req, started_utc=now()))
        attempts += 1
        raw = b''
        record = dict(index=i, request=req, status='FAILED')
        try:
            last_start = time.monotonic()
            done = subprocess.run(['curl', '--disable', '--silent', '--proto', '=https', '--retry', '0',
                '--connect-timeout', '15', '--max-time', '30', '--max-filesize', str(c['maximum_response_bytes']),
                '--write-out', '\n%{http_code}', req['url']], capture_output=True, timeout=35)
            raw, separator, code = done.stdout.rpartition(b'\n')
            record.update(curl_returncode=done.returncode, http=code.decode('ascii', errors='replace'),
                          duration_seconds=time.monotonic()-last_start)
            require(separator and done.returncode == 0 and code == b'200', 'MARKET_TRANSPORT_OR_HTTP')
            require(len(raw) <= c['maximum_response_bytes'], 'MARKET_RESPONSE_SIZE')
            decoded = validate(raw, req)
            record.update(status='VALID', rows=len(decoded))
        except Exception as exc:
            record['error'] = type(exc).__name__+':'+str(exc)[:160]
            raise
        finally:
            with (folder/'response.json').open('xb') as handle:
                handle.write(raw)
            record.update(sha256=hashlib.sha256(raw).hexdigest(), response_bytes=len(raw), ended_utc=now())
            save(folder/'record.json', record)
            total_bytes += len(raw)
        cache[i] = (folder, record)
        if len(cache) % 50 == 0 or len(cache) in (3, len(f['requests'])):
            print(json.dumps(dict(event='MARKET_PROGRESS', completed_pages=len(cache), total_pages=len(f['requests']),
                attempted_gets=attempts, raw_bytes=total_bytes, economic_attempts=0)), flush=True)
    preserve()
    if limit == 3:
        save(ROOT/'controls.json', dict(status='THREE_FIXED_MARKET_CONTROLS_PASS', attempts=attempts,
            records=[cache[i][1] for i in range(3)], economic_attempts=0))
    else:
        require(len(cache) == len(f['requests']), 'MARKET_ARCHIVE_INCOMPLETE')
        save(ROOT/'market-collection.json', dict(status='COMPLETE_FIXED_MARKET_ARCHIVE', pages=len(cache),
            attempted_gets=attempts, raw_bytes=total_bytes, economic_attempts=0,
            records_sha256={str(folder/'record.json'): sha(folder/'record.json') for folder, _ in cache.values()}))


def market():
    c = guard()
    require((ROOT/'market-collection.json').is_file(), 'FULL_MARKET_REQUIRED')
    collection = strict_json((ROOT/'market-collection.json').read_bytes())
    require(all(sha(p) == h for p, h in collection['records_sha256'].items()), 'MARKET_RECORD_CHANGED')
    cache, _, attempts, raw_bytes = load_pages()
    f = strict_json((ROOT/'freeze.json').read_bytes())
    require(len(cache) == len(f['requests']) == 1422, 'FULL_MARKET_COUNT')
    opportunities = strict_json((PRIOR/'causal-capacity-private.json').read_bytes())['opportunities']
    needed = {kind: set() for kind in ('trade', 'mark')}
    for event in opportunities:
        if event['exclusion'] is not None:
            continue
        first, entry, end = (event[k]*1000 for k in ('first', 'entry', 'exit'))
        anchor = (first-1)//MINUTE*MINUTE
        needed['trade'].update(range(anchor-61*MINUTE, anchor, MINUTE))
        needed['trade'].update((entry, end))
        needed['mark'].update(range(entry-MINUTE, end+1, MINUTE))
    data = {kind: {} for kind in ('trade', 'mark', 'funding')}
    counts = dict.fromkeys(data, 0)
    for i, req in enumerate(f['requests']):
        folder, _ = cache[i]
        rows = validate((folder/'response.json').read_bytes(), req)
        counts[req['kind']] += len(rows)
        for t, values in rows:
            if req['kind'] == 'funding' or t in needed[req['kind']]:
                target = data[req['kind']]
                require(t not in target, 'DUPLICATE_MARKET_KEY')
                target[t] = values
    require(counts['trade'] == counts['mark'] == 527100, 'FULL_MINUTE_DOMAIN')
    require(all(set(data[k]) == needed[k] for k in needed), 'MISSING_REQUIRED_PRICE')
    return data, opportunities, dict(counts=counts, attempted_gets=attempts, raw_bytes=raw_bytes)


def audit():
    data, opportunities, counts = market()
    report = dict(status='FULL_MARKET_DOMAIN_VALIDATED_NO_ECONOMIC_ATTEMPT', **counts,
        selected_rows={k: len(v) for k, v in data.items()},
        market_collection_sha256=sha(ROOT/'market-collection.json'), protected_files=preserve(), economic_attempts=0)
    save(ROOT/'market-audit.json', report)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['freeze', 'collect', 'audit'])
    parser.add_argument('--limit', type=int, default=1422)
    args = parser.parse_args()
    if args.command == 'freeze': freeze()
    elif args.command == 'collect': collect(args.limit)
    else: audit()
