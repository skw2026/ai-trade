"""Fixed S2 development inputs. No account, training, return calculation or promotion."""
import argparse
import bisect
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from audit_validator_queue_history import validate_rows
from collect_mvp_reference_history import opener
from mvp_reference_inputs import candle, number, strict_json
from run_bounded_learning import need, now, save, sha

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.artifacts/eth-queue-development-20261009'
PRIOR = ROOT / '.artifacts/eth-queue-input-20261009'
OLD = ROOT / '.artifacts/perpetual-g0-restart-20261008'
PLAN = ROOT / 'docs/plans/2026-10-09-eth-queue-development-screen.md'
CONTRACT = PLAN.with_suffix('.contract.json')
DAY, HOUR = 86400000, 3600000
SOURCE_NAMES = ('tools/eth_queue_development_inputs.py', 'tools/test_eth_queue_development_inputs.py',
                'tools/audit_validator_queue_history.py', 'tools/collect_mvp_reference_history.py',
                'tools/mvp_reference_inputs.py', 'tools/run_bounded_learning.py')


def ms(value):
    return int(dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()*1000)


def versions():
    nodes = []
    for page in range(1, 9):
        d = strict_json((PRIOR / ('publication-metadata-%d.json' % page)).read_bytes())
        need(not d.get('errors'), 'METADATA_ERRORS')
        r = d['data']['repository']
        need(r['nameWithOwner'] == 'etheralpha/validatorqueue-com' and not r['isPrivate'], 'REPOSITORY')
        h = r['object']['history']
        need(h['pageInfo']['hasNextPage'] == (page != 8), 'PAGINATION')
        nodes.extend(h['nodes'])
    need(len(nodes) == len({n['oid'] for n in nodes}) == 702, 'VERSION_SET')
    fallback = {r['commit_id']: r['public_at'].replace(' ', 'T')+'Z'
                for r in strict_json((PRIOR/'missing-deployment-comments.json').read_bytes())['data']}
    sample = strict_json((PRIOR/'audited-result-with-archives.json').read_bytes())['samples']
    candidates = list(PRIOR.glob('queue-*.json')) + list((OLD/'input-diagnosis').glob('queue-history-*.json'))
    by_hash = {sha(p): str(p.relative_to(ROOT)) for p in candidates}
    reuse = {s['sha']: by_hash[s['sha256']] for s in sample}
    result = []
    for n in nodes:
        times = []
        for deploy in n['deployments']['nodes']:
            need(deploy['commitOid'] == n['oid'] and deploy['creator']['login'] == 'vercel', 'DEPLOYMENT_BINDING')
            times.extend(s['createdAt'] for s in deploy['statuses']['nodes'] if s['state'] == 'SUCCESS')
        at = min(times) if times else fallback[n['oid']]
        need(ms(at) >= ms(n['committedDate']), 'PUBLICATION_ORDER')
        result.append(dict(kind='queue', key=n['oid'], available_ms=ms(at),
            basis='deployment_success' if times else 'archived_public_comment',
            reuse=reuse.get(n['oid']), url='https://raw.githubusercontent.com/etheralpha/validatorqueue-com/'
            + n['oid'] + '/historical_data.json'))
    return sorted(result, key=lambda r: (r['available_ms'], r['key']))


def market_requests(c):
    start, end = ms(c['decision_start_utc']), ms(c['terminal_utc'])+HOUR
    requests = []
    endpoints = {'trade': 'kline', 'mark': 'mark-price-kline', 'funding': 'funding/history'}
    for kind in ('funding', 'trade', 'mark'):
        funding = kind == 'funding'
        step, limit = (8*HOUR, 200) if funding else (HOUR, 1000)
        first = ((start+step-1)//step)*step
        for a in range(first, end, step*limit):
            b = min(end, a+step*limit)-1
            q = dict(category='linear', symbol='ETHUSDT', limit=limit)
            q.update({'startTime' if funding else 'start': a, 'endTime' if funding else 'end': b})
            if not funding:
                q['interval'] = '60'
            requests.append(dict(kind=kind, key=kind+'-'+str(a), start=a, end=b,
                step=step, limit=limit, reuse=None,
                url='https://api.bybit.com/v5/market/'+endpoints[kind]+'?'+urllib.parse.urlencode(q)))
    return requests


def validate(raw, req):
    data = strict_json(raw)
    if req['kind'] == 'queue':
        validate_rows(data)
        need(data[-1]['date'] <= dt.datetime.fromtimestamp(req['available_ms']/1000, dt.timezone.utc).date().isoformat(),
             'FUTURE_DATED_SNAPSHOT')
        # Freeze pre-Pectra count semantics: bool/fractional exit counts are not units.
        need(all(type(r['exit_queue']) is int and r['exit_queue'] >= 0 for r in data), 'EXIT_COUNT_UNIT')
        return data
    need(type(data.get('retCode')) is int and data['retCode'] == 0, 'API_ERROR')
    result = data['result']
    need(result['category'] == 'linear', 'MARKET_CATEGORY')
    funding = req['kind'] == 'funding'
    if not funding:
        need(result['symbol'] == 'ETHUSDT', 'MARKET_SYMBOL')
    rows = result['list']
    times = []
    for row in rows:
        if funding:
            need(row['symbol'] == 'ETHUSDT', 'FUNDING_SYMBOL')
            times.append(int(row['fundingRateTimestamp']))
            number(row['fundingRate'])
        else:
            times.append(candle(row, req['kind'])[0])
    first = ((req['start']+req['step']-1)//req['step'])*req['step']
    need(times == list(range(first, req['end']+1, req['step']))[::-1], 'EXACT_PAGE_GRID')
    need(len(rows) <= req['limit'], 'PAGE_LIMIT')
    return rows


def fixed_schedule(known, c):
    """As-of values at two historical decision clocks, never latest-row backfill."""
    times = [v['available_ms'] for v in known]
    need(times == sorted(times) and len(times) == len(set(times)), 'PUBLICATION_UNIQUENESS')
    def at(t):
        i = bisect.bisect_right(times, t-c['publication_guard_seconds']*1000)-1
        if i < 0:
            return None
        r = known[i]
        age = t//DAY - ms(r['date']+'T00:00:00Z')//DAY
        return r if 0 <= age <= c['maximum_age_days'] else None
    rows, groups = [], []
    for t in range(ms(c['decision_start_utc']), ms(c['last_entry_utc'])+1, DAY):
        current, past = at(t), at(t-c['lookback_days']*DAY)
        usable = current is not None and past is not None
        active = usable and current['exit_queue'] > past['exit_queue']
        row = dict(entry_ms=t, exit_ms=t+DAY, usable=usable, active=active,
                   current=current, past=past, group=None)
        if active:
            if not groups or t-groups[-1]['exit_ms'] >= c['group_empty_gap_days']*DAY:
                groups.append(dict(id=len(groups), entry_ms=t, exit_ms=t+DAY, days=0))
            groups[-1]['exit_ms'] = t+DAY
            groups[-1]['days'] += 1
            row['group'] = groups[-1]['id']
        rows.append(row)
    usable = sum(r['usable'] for r in rows)
    signals = sum(r['active'] for r in rows)
    sub = [sum(r['active'] and ((r['entry_ms'] < ms(c['split_utc'])) == (k == 0)) for r in rows) for k in (0, 1)]
    admitted = usable/len(rows) >= c['minimum_usable_fraction'] and signals >= c['minimum_signal_days'] and \
        len(groups) >= c['minimum_separated_groups'] and min(sub) >= c['minimum_signal_days_per_subperiod']
    return dict(rows=rows, groups=groups, usable_days=usable, planned_days=len(rows), signal_days=signals,
                subperiod_signal_days=sub, separated_groups=len(groups), capacity_admitted=admitted,
                decision='DEVELOPMENT_CAPACITY_ONLY' if admitted else 'INPUT_CAPACITY_INSUFFICIENT',
                independent_confirmation_admitted=False)


def guard():
    c = strict_json(CONTRACT.read_bytes())
    need(ms(c['started_utc']) <= time.time()*1000 < ms(c['deadline_utc']), 'STAGE_DEADLINE')
    state = strict_json((RUN/'validation-state.json').read_bytes())
    need(state['status'] == 'RUNNING', 'FORMAL_GATE_REQUIRED')
    if (RUN/'freeze.json').exists():
        f = strict_json((RUN/'freeze.json').read_bytes())
        need(sha(CONTRACT) == f['contract_sha256'] and sha(PLAN) == f['plan_sha256'], 'CONTRACT_CHANGED')
        need(all(sha(ROOT/p) == h for p, h in f['sources'].items()), 'SOURCE_CHANGED')
    return c


def preserve():
    f = strict_json((RUN/'freeze.json').read_bytes())
    need(all(sha(ROOT/p) == h for p, h in f['protected'].items()), 'PROTECTED_CHANGED')
    return len(f['protected'])


def freeze():
    c = guard()
    old = strict_json((OLD/'protected.json').read_bytes())
    old.update(strict_json((ROOT/'docs/reviews/2026-10-08-perpetual-input-diagnosis.evidence.json').read_bytes())['additional_protected_originals'])
    e = strict_json((ROOT/'docs/reviews/2026-10-09-eth-queue-input-admission.evidence.json').read_bytes())
    for p, identity in e['source_files'].items():
        old[str((PRIOR/p).relative_to(ROOT))] = identity['sha256']
    for folder, pattern in ((ROOT/'docs', '**/2026-10-09-eth-queue-input*'), (ROOT/'.artifacts', '*/validation-state.json')):
        for p in folder.glob(pattern):
            if p.parent != RUN:
                old[str(p.relative_to(ROOT))] = sha(p)
    need(all(sha(ROOT/p) == h for p, h in old.items()), 'PRIOR_EVIDENCE_CHANGED')
    requests = versions()
    market = market_requests(c)
    need(sum(r['reuse'] is None for r in requests)+len(market) <= c['maximum_input_gets'], 'REQUEST_BUDGET')
    for p in ('raw', 'attempts', 'receipts', 'pages'):
        (RUN/p).mkdir(exist_ok=False)
    save(RUN/'freeze.json', dict(contract_sha256=sha(CONTRACT), plan_sha256=sha(PLAN),
        sources={p: sha(ROOT/p) for p in SOURCE_NAMES}, protected=old, queue_requests=requests,
        market_requests=market, created_utc=now(), document_reads=3,
        independent_confirmation_admitted=False))
    print(json.dumps(dict(versions=len(requests), new_queue_gets=sum(r['reuse'] is None for r in requests),
                         market_pages=len(market), preserved=preserve())), flush=True)


def collect(kind):
    c = guard(); preserve()
    f = strict_json((RUN/'freeze.json').read_bytes())
    if kind == 'market':
        need(strict_json((RUN/'schedule.json').read_bytes())['capacity_admitted'], 'CAPACITY_NOT_ADMITTED')
    requests = f['queue_requests'] if kind == 'queue' else f['market_requests']
    previous = list((RUN/'attempts').glob('*.json'))
    need(all((RUN/'receipts'/p.name).exists() for p in previous), 'UNFINISHED_ATTEMPT')
    if any(not strict_json((RUN/'receipts'/p.name).read_bytes())['ok'] for p in previous):
        state = strict_json((RUN/'validation-state.json').read_bytes())
        need(state['active'].get('review_path'), 'REVIEWED_RETRY_REQUIRED')
    total = sum(p.stat().st_size for p in (RUN/'raw').iterdir())
    count = len(previous)
    def fetch(job):
        req, name = job
        raw, status, error = b'', None, None
        try:
            with opener().open(urllib.request.Request(req['url'], headers={'User-Agent': 'ai-trade-s2-development/1'}), timeout=25) as response:
                status = response.status
                raw = response.read(c['maximum_response_bytes']+1)
            need(status == 200 and len(raw) <= c['maximum_response_bytes'], 'HTTP_OR_BYTES')
            rows = validate(raw, req)
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                status = exc.code
                raw = exc.read(c['maximum_response_bytes']+1)
            error = type(exc).__name__+': '+str(exc)
            rows = []
        with (RUN/'raw'/name).open('xb') as out:
            out.write(raw)
        receipt = dict(key=req['key'], url=req['url'], raw=name, sha256=sha(RUN/'raw'/name),
                       bytes=len(raw), http_status=status, ok=error is None, error=error,
                       row_count=len(rows), received_utc=now())
        save(RUN/'receipts'/name, receipt)
        return receipt
    jobs = []
    for req in requests:
        cache = RUN/'pages'/(req['key']+'.json')
        if cache.exists():
            rec = strict_json(cache.read_bytes())
            path = ROOT/rec['reuse'] if rec.get('reuse') else RUN/'raw'/rec['raw']
            need(sha(path) == rec['sha256'], 'CACHE_CHANGED')
            validate(path.read_bytes(), req)
            continue
        if req['reuse']:
            p = ROOT/req['reuse']; validate(p.read_bytes(), req)
            save(cache, dict(key=req['key'], reuse=req['reuse'], sha256=sha(p)))
        else:
            jobs.append(req)
    with ThreadPoolExecutor(max_workers=4 if kind == 'queue' else 1) as pool:
        width = 4 if kind == 'queue' else 1
        for offset in range(0, len(jobs), width):
            guard()
            batch = []
            for req in jobs[offset:offset+width]:
                count += 1
                need(count <= c['maximum_input_gets'], 'GET_BUDGET')
                name = '%04d.json' % count
                save(RUN/'attempts'/name, dict(request=req, started_utc=now()))
                batch.append((req, name))
            receipts = list(pool.map(fetch, batch))
            total += sum(r['bytes'] for r in receipts)
            for (req, _), rec in zip(batch, receipts):
                if rec['ok']:
                    save(RUN/'pages'/(req['key']+'.json'), rec)
            need(total <= c['maximum_total_bytes'], 'TOTAL_BYTES')
            need(all(r['ok'] for r in receipts), 'COLLECTION_FAILED:'+json.dumps([r for r in receipts if not r['ok']]))
            if offset % 40 == 0 or offset+width >= len(jobs):
                print(json.dumps(dict(kind=kind, complete=min(offset+width, len(jobs)), planned=len(jobs),
                                      attempted_gets=count, total_bytes=total)), flush=True)
    save(RUN/(kind+'-collection.json'), dict(completed_utc=now(), attempted_gets=count, total_bytes=total))


def load(req):
    rec = strict_json((RUN/'pages'/(req['key']+'.json')).read_bytes())
    path = ROOT/rec['reuse'] if rec.get('reuse') else RUN/'raw'/rec['raw']
    need(sha(path) == rec['sha256'], 'INPUT_CHANGED')
    return validate(path.read_bytes(), req)


def schedule():
    c = guard(); preserve()
    f = strict_json((RUN/'freeze.json').read_bytes())
    need((RUN/'queue-collection.json').exists(), 'QUEUE_INCOMPLETE')
    known = []
    for req in f['queue_requests']:
        row = load(req)[-1]
        known.append(dict(sha=req['key'], available_ms=req['available_ms'], date=row['date'], exit_queue=row['exit_queue']))
    result = fixed_schedule(known, c)
    save(RUN/'schedule.json', result)
    print(json.dumps({k: v for k, v in result.items() if k not in ('rows', 'groups')}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('freeze', 'queue', 'schedule', 'market', 'preserve'))
    args = parser.parse_args()
    if args.command == 'freeze': freeze()
    elif args.command == 'schedule': schedule()
    elif args.command == 'preserve': print(preserve())
    else: collect(args.command)
