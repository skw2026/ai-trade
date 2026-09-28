#!/usr/bin/env python3
"""Capacity-first, anonymous attention screen input. No returns in this module."""
import argparse
import datetime as dt
import gzip
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from free_option_flow_data import GATES as PRIOR
from screen_regional_session import (need, sha, strict, ms, utc, save_new, transport,
                                     validate_page)

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.artifacts/capacity-first-20260928'
GATE = RUN / 'validation-state.json'
PLAN = 'docs/plans/2026-09-28-capacity-first-new-mechanisms.md'
CONTRACT = 'docs/contracts/attention_capacity_v1.json'
GATES = PRIOR + ['free-option-flow-20260928/validation-state.json']
DAY, HOUR = 86400000, 3600000
SOURCES = (PLAN, CONTRACT, 'docs/reviews/2026-09-28-capacity-first-source-review.md',
           'tools/attention_capacity.py', 'tools/test_attention_capacity.py',
           'tools/screen_regional_session.py', 'tools/free_option_flow_data.py',
           'tools/option_flow_probe.py')


def config():
    return strict((ROOT / CONTRACT).read_bytes())


def identities():
    return {p: sha((ROOT / '.artifacts' / p).read_bytes()) for p in GATES}


def running():
    need(strict(GATE.read_bytes())['status'] == 'RUNNING', 'FORMAL_GATE_REQUIRED')


def freeze():
    running()
    need(all(v is False for v in config()['authority'].values()), 'NO_ACTUATION')
    for folder in ('attempts', 'receipts', 'raw', 'pages'):
        (RUN / folder).mkdir(exist_ok=False)
    save_new(RUN / 'freeze.json', dict(started_utc='2026-09-28T13:59:33Z',
        frozen_utc=utc(time.time()*1000), source_sha256={p: sha((ROOT/p).read_bytes()) for p in SOURCES},
        old_gates=identities(), documentation_reads=23))


def frozen():
    f = strict((RUN / 'freeze.json').read_bytes())
    need(identities() == f['old_gates'], 'OLD_GATES_CHANGED')
    need(all(sha((ROOT/p).read_bytes()) == h for p,h in f['source_sha256'].items()), 'FROZEN_SOURCE_CHANGED')
    need(time.time()*1000 - ms(f['started_utc']) < 8*3600000, 'EIGHT_HOUR_BUDGET')
    return f


def specs(c):
    out = []
    a = ms('2021-12-29T00:00:00Z')
    end = ms('2025-12-30T00:00:00Z')
    while a < end:
        year = dt.datetime.fromtimestamp(a/1000, dt.timezone.utc).year
        b = min(end, ms(f'{year+1}-01-01T00:00:00Z'))
        fmt = lambda t: dt.datetime.fromtimestamp(t/1000, dt.timezone.utc).strftime('%Y%m%d00')
        url = ('https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/'
               f'en.wikipedia/all-access/user/Bitcoin/daily/{fmt(a)}/{fmt(b-DAY)}')
        out.append(dict(kind='views', start=a, end=b-1, step=DAY, url=url, key=sha(url.encode())))
        a = b
    m = c['market']
    for kind, endpoint in (('trade','kline'), ('mark','mark-price-kline'), ('funding','funding/history')):
        funding = kind == 'funding'
        step, limit = (8*HOUR, 200) if funding else (HOUR, 1000)
        a, end = ms(m['start']), ms(m['end_exclusive'])
        while a < end:
            b = min(end, a + step*limit)
            params = dict(category='linear', symbol='BTCUSDT', limit=limit)
            params.update({'startTime' if funding else 'start': a,
                           'endTime' if funding else 'end': b-1})
            if not funding:
                params['interval'] = '60'
            url = m['host']+'/v5/market/'+endpoint+'?'+urllib.parse.urlencode(params)
            out.append(dict(kind=kind, start=a, end=b-1, step=step, limit=limit,
                            url=url, key=sha(url.encode())))
            a = b
    need(len(out) + c['budget']['documentation_reads_used'] <= 200, 'PLANNED_BUDGET')
    return out


def views(raw, spec):
    data = strict(raw)
    rows = data['items']
    result = {}
    for row in rows:
        need((row['project'], row['article'], row['access'], row['agent'], row['granularity']) ==
             ('en.wikipedia', 'Bitcoin', 'all-access', 'user', 'daily'), 'WRONG_VIEWS_IDENTITY')
        t = dt.datetime.strptime(row['timestamp'], '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        t = int(t.timestamp()*1000)
        need(t not in result and type(row['views']) is int and row['views'] >= 0, 'BAD_VIEWS_COUNT')
        result[t] = row['views']
    need(sorted(result) == list(range(spec['start'], spec['end']+1, DAY)), 'VIEWS_COVERAGE')
    return result


def read(spec):
    page = strict((RUN/'pages'/f"{spec['key']}.json").read_bytes())
    raw = (RUN/'raw'/page['raw']).read_bytes()
    need(page['url'] == spec['url'] and sha(raw) == page['sha256'], 'INPUT_HASH_OR_URL')
    return raw


class Collector:
    def __init__(self):
        running()
        frozen()
        self.opener = transport()

    def get(self, spec):
        frozen()
        if (RUN/'pages'/f"{spec['key']}.json").exists():
            raw = read(spec)
            views(raw, spec) if spec['kind'] == 'views' else validate_page(raw, spec)
            return
        index = len(list((RUN/'attempts').glob('*.json'))) + 1
        need(index + 23 <= 200, 'PUBLIC_GET_BUDGET')
        name = f'{index:03d}'
        save_new(RUN/'attempts'/f'{name}.json', dict(**spec, started_utc=utc(time.time()*1000)))
        req = urllib.request.Request(spec['url'], headers={
            'User-Agent': 'ai-trade-capacity-research/1.0 (https://github.com/skw2026/ai-trade)',
            'Accept-Encoding': 'gzip', 'Accept': 'application/json'})
        status, raw, error, headers = None, b'', None, {}
        try:
            with self.opener.open(req, timeout=45) as response:
                status, headers = response.status, dict(response.headers)
                raw = response.read(16*1024*1024+1)
                need(len(raw) <= 16*1024*1024, 'BODY_LIMIT')
                if response.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                need(len(raw) <= 32*1024*1024, 'DECODED_LIMIT')
        except urllib.error.HTTPError as exc:
            status, raw, error = exc.code, exc.read(1024*1024), type(exc).__name__
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        with (RUN/'raw'/f'{name}.bin').open('xb') as f:
            f.write(raw)
        receipt = dict(url=spec['url'], status=status, raw=f'{name}.bin', sha256=sha(raw),
                       bytes=len(raw), error=error, finished_utc=utc(time.time()*1000),
                       headers={k:v for k,v in headers.items() if k.lower() in
                                ('date','last-modified','etag','content-type','content-encoding')})
        save_new(RUN/'receipts'/f'{name}.json', receipt)
        need(status == 200 and error is None, 'PUBLIC_INPUT_FETCH_FAILED '+name+' '+str(error))
        views(raw, spec) if spec['kind'] == 'views' else validate_page(raw, spec)
        save_new(RUN/'pages'/f"{spec['key']}.json", receipt)
        print(json.dumps(dict(get=index, kind=spec['kind'], bytes=len(raw))), flush=True)


def collect(phase):
    wanted = {'views','trade'} if phase == 'capacity' else {'mark','funding'}
    if phase == 'economy':
        need(strict((RUN/'capacity.json').read_bytes())['eligible'], 'CAPACITY_REQUIRED')
    collector = Collector()
    for s in specs(config()):
        if s['kind'] in wanted:
            collector.get(s)


def inputs(kinds):
    out = {k:{} for k in kinds}
    for s in specs(config()):
        k = s['kind']
        if k not in kinds:
            continue
        raw = read(s)
        if k == 'views':
            values = views(raw, s)
        else:
            rows = validate_page(raw, s)
            values = ({int(r['fundingRateTimestamp']): r['fundingRate'] for r in rows} if k == 'funding'
                      else {int(r[0]): r[1:5] for r in rows})
        need(not out[k].keys() & values.keys(), 'OVERLAPPING_PAGE')
        out[k].update(values)
    return out


def signals(v, prices, c):
    from decimal import Decimal
    excluded, valid = [], []
    first, end = ms(c['calendar']['first_entry']), ms(c['calendar']['end_exclusive'])
    incident = c['known_input_exclusion']
    a, b = ms(incident['from']+'T00:00:00Z'), ms(incident['through']+'T00:00:00Z')
    for entry in range(first, end, DAY):
        d = entry//DAY*DAY
        t, previous = d-2*DAY, d-3*DAY
        need(t in v and previous in v, 'SIGNAL_INPUT_MISSING')
        if a <= t <= b or a <= previous <= b:
            excluded.append(dict(entry=entry, reason='KNOWN_INPUT_LOSS'))
            continue
        change = v[t]-v[previous]
        change_price = Decimal(prices[d-DAY][0])-Decimal(prices[d-2*DAY][0])
        if change == 0 or change_price == 0:
            excluded.append(dict(entry=entry, reason='SIGNAL_TIE'))
            continue
        side = -1 if change > 0 else 1
        control = -1 if change_price > 0 else 1
        valid.append(dict(entry=entry, exit=entry+DAY, side=side, control=control,
                          cell=f'{side}_{control}', signal_day=t,
                          available_lag_hours=(entry-(t+DAY))/HOUR))
    return valid, excluded


def capacity():
    running()
    frozen()
    c, data = config(), inputs({'views','trade'})
    valid, excluded = signals(data['views'], data['trade'], c)
    cells = {f'{a}_{b}':sum(r['side'] == a and r['control'] == b for r in valid)
             for a in (-1,1) for b in (-1,1)}
    halves = [sum((r['entry'] < ms('2024-01-01T00:00:00Z')) == early for r in valid)
              for early in (True,False)]
    cap = c['capacity']
    eligible = (len(valid) >= cap['minimum_valid_days'] and
                min(cells.values()) >= cap['minimum_each_attention_price_sign_cell'] and
                min(halves) >= cap['minimum_each_two_year_half'])
    hashes = {str(p.relative_to(RUN)):sha(p.read_bytes()) for folder in ('raw','pages')
              for p in (RUN/folder).iterdir()}
    result = dict(scope='CAPACITY_ONLY_NO_RETURNS', eligible=eligible, valid_days=len(valid),
                  cells=cells, halves=halves, excluded=excluded, signals=valid,
                  thresholds=cap, input_sha256=hashes, candidate_qualified=False,
                  economic_experiments=0, market_gets=len(list((RUN/'attempts').glob('*.json'))))
    save_new(RUN/'capacity.json', result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('signals','input_sha256','excluded')}))
    print(json.dumps(dict(excluded_days=len(excluded))))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('freeze','collect-capacity','capacity','collect-economy'))
    action = p.parse_args().action
    if action == 'freeze':
        freeze()
    elif action == 'capacity':
        capacity()
    else:
        collect(action.removeprefix('collect-'))


if __name__ == '__main__':
    main()
