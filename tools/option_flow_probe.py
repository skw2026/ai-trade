#!/usr/bin/env python3
"""Bounded anonymous data feasibility; never computes a signal or return."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from screen_regional_session import need, sha, strict, number, stamp, ms, utc, save_new, transport, OLD_GATES

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.artifacts/option-flow-proxy-20260928'
GATE = RUN / 'validation-state.json'
PLAN = 'docs/plans/2026-09-28-option-flow-proxy.md'
OLD = OLD_GATES + ['regional-session-screen-20260928/validation-state.json',
                   'candidate-intake-20260928/validation-state.json']
DATES = ('2026-08-03', '2026-08-28', '2026-09-06')
MONTHS = dict(zip(('JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'), range(1,13)))
HISTORY = 'https://history.deribit.com/api/v2/public/'


def old_hashes():
    return {x: sha((ROOT / '.artifacts' / x).read_bytes()) for x in OLD}


def option(name):
    m = re.fullmatch(r'BTC-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:\.\d+)?)-([CP])', name)
    need(m is not None, 'NOT_INVERSE_BTC_OPTION')
    day, month, year, strike, side = m.groups()
    expiry = dt.datetime(2000+int(year), MONTHS[month], int(day), 8, tzinfo=dt.timezone.utc)
    need(number(strike) > 0, 'STRIKE')
    return int(expiry.timestamp()*1000), side


def validate_trades(raw, start, end):
    data = strict(raw)
    need(data.get('jsonrpc') == '2.0' and 'error' not in data, 'DERIBIT_API_ERROR')
    r = data['result']
    rows, more = r['trades'], r['has_more']
    need(isinstance(rows, list) and len(rows) <= 1000 and type(more) is bool, 'PAGE_SCHEMA')
    ids = set()
    for t in rows:
        need(isinstance(t['trade_id'], str) and t['trade_id'].isdigit(), 'TRADE_ID')
        need(t['trade_id'] not in ids, 'DUPLICATE_TRADE')
        ids.add(t['trade_id'])
        need(start <= stamp(t['timestamp']) <= end, 'TRADE_TIME')
        option(t['instrument_name'])
        need(t['direction'] in ('buy','sell') and number(t['amount']) > 0, 'TRADE_FIELDS')
    need(not more or bool(rows), 'EMPTY_MORE')
    return rows, more


def split(start, end):
    need(start < end, 'SINGLE_MILLISECOND_OVERFLOW')
    middle = (start+end)//2
    return ((start,middle), (middle+1,end))


def check_gate():
    state = strict(GATE.read_bytes())
    need(state['status'] == 'RUNNING', 'FORMAL_GATE_REQUIRED')
    return state


def freeze():
    check_gate()
    for folder in ('attempts','receipts','raw','pages'):
        (RUN / folder).mkdir(exist_ok=False)
    save_new(RUN / 'probe-freeze.json', dict(created_at=utc(time.time()*1000),
        old_gates=old_hashes(), plan_sha256=sha((ROOT/PLAN).read_bytes()),
        sources={x:sha((ROOT/x).read_bytes()) for x in
                 ('tools/option_flow_probe.py','tools/test_option_flow_probe.py','tools/screen_regional_session.py')},
        dates=DATES, maximum_probe_gets=20, maximum_batch_gets=200))


class Collector:
    def __init__(self, limit=20, diagnostic=False):
        if diagnostic:
            state = strict(GATE.read_bytes())
            need(state['status'] == 'BLOCKED' and
                 state['active']['failure_id'] == '0ea648c079f54d959163721d1c0ba0ec', 'BOUND_DIAGNOSIS_ONLY')
        else:
            check_gate()
        frozen = strict((RUN/'probe-freeze.json').read_bytes())
        need(old_hashes() == frozen['old_gates'], 'OLD_GATE_CHANGED')
        need(sha((ROOT/PLAN).read_bytes()) == frozen['plan_sha256'], 'PLAN_CHANGED')
        self.limit, self.previous, self.opener = limit, 0.0, transport()

    def get(self, key, url, body=None):
        parsed = urllib.parse.urlsplit(url)
        need(parsed.scheme == 'https' and parsed.hostname in ('history.deribit.com','www.deribit.com','api.bybit.com')
             and parsed.username is None and parsed.password is None, 'PUBLIC_HOST_ONLY')
        need(parsed.path in ('/api/v2/public/get_last_trades_by_currency_and_time',
                             '/api/v2/public/get_last_trades_by_currency',
                             '/api/v2/public/get_instrument', '/v5/market/kline',
                             '/v5/market/mark-price-kline','/v5/market/funding/history'), 'PUBLIC_METHOD_ONLY')
        need(re.fullmatch(r'[A-Za-z0-9_.-]+', key), 'SAFE_KEY')
        cached = RUN/'pages'/f'{key}.json'
        if cached.exists():
            p = strict(cached.read_bytes())
            raw = (RUN/'raw'/p['raw']).read_bytes()
            need(p['url'] == url and p.get('body_sha256') == (sha(body) if body else None)
                 and sha(raw) == p['sha256'], 'CACHED_PAGE_CHANGED')
            return raw
        attempts = list((RUN/'attempts').glob('*.json'))
        need(len(attempts) < min(200,self.limit), 'PUBLIC_GET_BUDGET')
        need(all((RUN/'receipts'/p.name).exists() for p in attempts), 'INTERRUPTED_REQUEST')
        delay = 0.4-(time.monotonic()-self.previous)
        if delay > 0:
            time.sleep(delay)
        name = f'{len(attempts)+1:06d}.json'
        save_new(RUN/'attempts'/name, dict(key=key,url=url,body=body.decode() if body else None,at=utc(time.time()*1000)))
        self.previous = time.monotonic()
        raw, code, error = b'', None, None
        try:
            with self.opener.open(urllib.request.Request(url,data=body,method='GET',headers={
                    'User-Agent':'ai-trade-bounded-research/1.0','Content-Type':'application/json'}), timeout=25) as response:
                code = response.status
                raw = response.read(8_000_001)
                need(len(raw) <= 8_000_000, 'RESPONSE_BYTES')
        except urllib.error.HTTPError as exc:
            code, raw, error = exc.code, exc.read(8_000_001), str(exc)
        except Exception as exc:
            error = type(exc).__name__+': '+str(exc)
        raw_name = name.replace('.json','.bin')
        with (RUN/'raw'/raw_name).open('xb') as f:
            f.write(raw)
            f.flush()
            import os
            os.fsync(f.fileno())
        receipt = dict(url=url,key=key,body_sha256=sha(body) if body else None,
                       raw=raw_name,sha256=sha(raw),http_status=code,error=error)
        save_new(RUN/'receipts'/name, receipt)
        need(code == 200 and error is None, 'PUBLIC_GET_FAILED '+name+' '+str(code)+' '+str(error))
        save_new(cached, receipt)
        return raw


def trade_url(start, end, host=HISTORY):
    return host+'get_last_trades_by_currency_and_time?'+urllib.parse.urlencode(dict(
        currency='BTC',kind='option',start_timestamp=start,end_timestamp=end,
        count=1000,sorting='asc',include_old='true'))


def complete_trades(collector, start, end):
    rows, more = validate_trades(collector.get(f'options-{start}-{end}', trade_url(start,end)), start,end)
    if not more:
        return rows
    result = []
    for a,b in split(start,end):
        result.extend(complete_trades(collector,a,b))
    need(len({t['trade_id'] for t in result}) == len(result), 'CROSS_PAGE_DUPLICATE')
    return result


def probe():
    collector = Collector()
    report = []
    for day in DATES:
        start, end = ms(day+'T07:30:00Z'), ms(day+'T07:55:00Z')-1
        rows = complete_trades(collector,start,end)
        need(rows, 'EMPTY_HISTORICAL_OPTION_WINDOW '+day)
        target = ms(day+'T08:00:00Z')
        names = sorted({t['instrument_name'] for t in rows if option(t['instrument_name']) == (target,'C')})
        need(names, 'NO_EXPIRING_CALLS '+day)
        name = names[0]
        raw = collector.get('instrument-'+name, HISTORY+'get_instrument?'+urllib.parse.urlencode(dict(instrument_name=name)))
        r = strict(raw)
        need('error' not in r and r['jsonrpc'] == '2.0','INSTRUMENT_API')
        r = r['result']
        need(r['instrument_name'] == name and r['kind'] == 'option' and r['option_type'] == 'call'
             and stamp(r['expiration_timestamp']) == target and r['base_currency'] == 'BTC'
             and r['settlement_currency'] == 'BTC' and number(r['contract_size']) == 1, 'INSTRUMENT_MAPPING')
        item = dict(day=day,total_trades=len(rows),expiring_call_instruments=len(names),metadata_verified=True)
        report.append(item)
        print(json.dumps(item), flush=True)
    start, end = ms(DATES[0]+'T00:00:00Z'),ms(DATES[0]+'T23:59:59.999Z')
    for kind, method in (('trade','kline'),('mark','mark-price-kline'),('funding','funding/history')):
        funding = kind == 'funding'
        params = dict(category='linear',symbol='BTCUSDT',limit=200 if funding else 1000)
        params.update({'startTime' if funding else 'start':start,'endTime' if funding else 'end':end})
        if not funding:
            params['interval']='5'
        raw = collector.get('probe-bybit-'+kind,'https://api.bybit.com/v5/market/'+method+'?'+urllib.parse.urlencode(params))
        r = strict(raw)
        need(type(r['retCode']) is int and r['retCode'] == 0,'BYBIT_API')
        r = r['result']
        need(r['category'] == 'linear', 'BYBIT_CATEGORY')
        rows = r['list']
        times = [stamp(x['fundingRateTimestamp'] if funding else x[0]) for x in rows]
        need(len(times) == len(set(times)) and times == sorted(times,reverse=True), 'BYBIT_ORDER')
        need(times and all(start <= t <= end for t in times), 'BYBIT_TIME')
        if funding:
            need(len(rows) < 200 and all(x['symbol']=='BTCUSDT' for x in rows),'FUNDING_PAGE')
        else:
            need(r['symbol'] == 'BTCUSDT' and set(times) == set(range(start,end+1,300000)), 'BYBIT_GRID')
        report.append(dict(kind=kind,rows=len(rows)))
    save_new(RUN/'probe-result.json',dict(verdict='DATA_FEASIBILITY_PASS_NOT_ECONOMIC',items=report,
                                         market_gets=len(list((RUN/'attempts').glob('*.json')))))
    print(json.dumps(report),flush=True)


def diagnose():
    """One predeclared official main-host comparison; not a formal gate retry."""
    collector = Collector(limit=2, diagnostic=True)
    start, end = ms(DATES[0]+'T07:30:00Z'),ms(DATES[0]+'T07:55:00Z')-1
    raw = collector.get('diagnosis-main-same-window',trade_url(start,end,'https://www.deribit.com/api/v2/public/'))
    rows, more = validate_trades(raw,start,end)
    print(json.dumps(dict(scope='DIAGNOSIS_ONLY_NOT_ACCEPTANCE',http_status=200,
                          rows=len(rows),has_more=more,expiring_calls_present=any(
                              option(t['instrument_name'])==(ms(DATES[0]+'T08:00:00Z'),'C') for t in rows))),flush=True)


def diagnose_coverage():
    """Fixed four-way size/age/service comparison; all requests count, no returns."""
    collector = Collector(limit=6, diagnostic=True)
    old_start, old_end = ms(DATES[0]+'T07:30:00Z'),ms(DATES[0]+'T07:55:00Z')-1
    recent_start, recent_end = ms('2026-09-27T07:30:00Z'),ms('2026-09-27T07:55:00Z')-1
    specs = [
        ('diagnosis-history-count1',trade_url(old_start,old_end).replace('count=1000','count=1')),
        ('diagnosis-history-recent-count1',trade_url(recent_start,recent_end).replace('count=1000','count=1')),
        ('diagnosis-main-recent',trade_url(recent_start,recent_end,'https://www.deribit.com/api/v2/public/')),
        ('diagnosis-history-metadata',HISTORY+'get_instrument?instrument_name=BTC-PERPETUAL')]
    for key,url in specs:
        try:
            raw = collector.get(key,url)
            r = strict(raw)
            summary = dict(key=key,scope='DIAGNOSIS_ONLY',error=r.get('error'))
            if 'trades' in r.get('result',{}):
                summary.update(rows=len(r['result']['trades']),has_more=r['result']['has_more'])
            print(json.dumps(summary),flush=True)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            print(json.dumps(dict(key=key,scope='DIAGNOSIS_ONLY',error=str(exc))),flush=True)


def diagnose_methods():
    """Four official alternate-method / documented-contract controls, no strategy."""
    collector = Collector(limit=10, diagnostic=True)
    start,end=ms(DATES[0]+'T07:30:00Z'),ms(DATES[0]+'T07:55:00Z')-1
    main='https://www.deribit.com/api/v2/public/'
    specs=[
        ('diagnosis-main-latest',main+'get_last_trades_by_currency?currency=BTC&kind=option&count=1'),
        ('diagnosis-history-option-metadata',HISTORY+'get_instrument?instrument_name=BTC-24APR26-72000-C'),
        ('diagnosis-history-other-method',trade_url(start,end).replace('get_last_trades_by_currency_and_time','get_last_trades_by_currency').replace('count=1000','count=1')),
        ('diagnosis-main-other-method',trade_url(start,end,main).replace('get_last_trades_by_currency_and_time','get_last_trades_by_currency').replace('count=1000','count=1'))]
    for key,url in specs:
        try:
            r=strict(collector.get(key,url))
            out=dict(scope='DIAGNOSIS_ONLY',key=key,error=r.get('error'))
            result=r.get('result',{})
            if 'trades' in result:
                rows=result['trades']
                out.update(rows=len(rows),has_more=result['has_more'],timestamps=[t['timestamp'] for t in rows])
            else:
                out['metadata_returned']=bool(result)
            print(json.dumps(out),flush=True)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            print(json.dumps(dict(key=key,scope='DIAGNOSIS_ONLY',error=str(exc))),flush=True)


def diagnose_encoding():
    """Two documentation-style GET JSON-RPC bodies to exclude query encoding."""
    collector=Collector(limit=12, diagnostic=True)
    start,end=ms(DATES[0]+'T07:30:00Z'),ms(DATES[0]+'T07:55:00Z')-1
    specs=[('get_last_trades_by_currency_and_time',dict(currency='BTC',kind='option',
            start_timestamp=start,end_timestamp=end,count=1000,sorting='asc',include_old=True)),
           ('get_instrument',dict(instrument_name='BTC-24APR26-72000-C',include_old=True))]
    for method,params in specs:
        key='diagnosis-json-'+method
        body=json.dumps(dict(jsonrpc='2.0',id=1,method='public/'+method,params=params)).encode()
        try:
            r=strict(collector.get(key,HISTORY+method,body))
            result=r.get('result',{})
            print(json.dumps(dict(key=key,scope='DIAGNOSIS_ONLY',error=r.get('error'),
                                  result_present=bool(result),rows=len(result.get('trades',[])),
                                  has_more=result.get('has_more'))),flush=True)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            print(json.dumps(dict(key=key,scope='DIAGNOSIS_ONLY',error=str(exc))),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('freeze','probe','diagnose','diagnose-coverage','diagnose-methods','diagnose-encoding'))
    args=p.parse_args()
    try:
        {'freeze':freeze,'probe':probe,'diagnose':diagnose,'diagnose-coverage':diagnose_coverage,
         'diagnose-methods':diagnose_methods,'diagnose-encoding':diagnose_encoding}[args.action]()
        return 0
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print('OPTION_FLOW_STOP: '+str(exc),flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
