#!/usr/bin/env python3
"""Append-only anonymous availability diagnosis, not historical-data admission."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from screen_regional_session import need, strict, save_new, sha

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.artifacts/free-eth-evidence-20260929'
PLAN = 'docs/plans/2026-09-29-free-eth-evidence.md'
START = dt.datetime(2026, 9, 29, 11, 29, 55, tzinfo=dt.timezone.utc).timestamp()
HOST_PATHS = {
    'api.github.com': '/repos/',
    'raw.githubusercontent.com': '/',
    'web.archive.org': '/',
    'archive.org': '/wayback/available',
    'archive.softwareheritage.org': '/api/1/',
    'ultrasound.money': '/api/',
    'api.ultrasound.money': '/',
}
SOURCE_FILES = (PLAN, 'tools/probe_free_eth_sources.py',
                'tools/test_probe_free_eth_sources.py', 'tools/screen_regional_session.py')


class NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def transport():
    ca = '/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').is_file() else None
    ctx = ssl.create_default_context(cafile=ca)
    need(ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED, 'TLS_REQUIRED')
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoFollow(),
                                      urllib.request.HTTPSHandler(context=ctx))


def allowed(url):
    p = urllib.parse.urlsplit(url)
    return (p.scheme == 'https' and p.hostname in HOST_PATHS and
            p.netloc == p.hostname and not p.fragment and
            p.path.startswith(HOST_PATHS[p.hostname]))


def initialized():
    f = strict((RUN / 'baseline.json').read_bytes())
    need(all(sha((ROOT / p).read_bytes()) == h for p, h in f['preserved'].items()),
         'OLD_EVIDENCE_CHANGED')
    need(all(sha((ROOT / p).read_bytes()) == h for p, h in f['sources'].items()),
         'FROZEN_SOURCE_CHANGED')
    return f


def freeze():
    old = strict((ROOT / '.artifacts/eth-supply-vintage-diagnosis-20260929/baseline.json').read_bytes())
    paths = set(old['preserved_sha256'])
    for p in (ROOT / '.artifacts/eth-supply-vintage-diagnosis-20260929').rglob('*'):
        if p.is_file() and p.suffix != '.lock':
            paths.add(str(p.relative_to(ROOT)))
    for p in (ROOT / '.artifacts').glob('*/validation-state.json'):
        if p.parent != RUN:
            paths.add(str(p.relative_to(ROOT)))
    need(all(sha((ROOT / p).read_bytes()) == h for p, h in old['preserved_sha256'].items()),
         'PREVIOUS_BASELINE_MISMATCH')
    RUN.mkdir(exist_ok=True)
    for folder in ('raw', 'receipts', 'attempts'):
        (RUN / folder).mkdir(exist_ok=False)
    save_new(RUN / 'baseline.json', dict(preserved={p: sha((ROOT / p).read_bytes()) for p in sorted(paths)},
             sources={p: sha((ROOT / p).read_bytes()) for p in SOURCE_FILES}))
    print('FREE_SOURCE_DIAGNOSIS_FROZEN', len(paths))


def probe(key, url):
    initialized()
    need(0 <= time.time() - START <= 5400, 'TIME_LIMIT')
    need(re.fullmatch('[a-z0-9-]+', key) and allowed(url), 'READ_ONLY_TARGET_REQUIRED')
    attempts = [strict(p.read_bytes()) for p in (RUN / 'attempts').glob('*.json')]
    need(len(attempts) < 30, 'GET_LIMIT')
    need(all(x['url'] != url and x['key'] != key for x in attempts), 'NO_REPEAT')
    save_new(RUN / 'attempts' / (key + '.json'), dict(url=url, key=key, started=time.time()))
    status, raw, error, headers = None, b'', None, {}
    req = urllib.request.Request(url, headers={'User-Agent': 'ai-trade-free-source-diagnosis/1.0',
                                               'Accept': '*/*'})
    # Shared transport disables redirects and environment credential/proxy discovery.
    try:
        with transport().open(req, timeout=35) as response:
            status, headers = response.status, dict(response.headers)
            raw = response.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        status, headers = exc.code, dict(exc.headers)
        raw = exc.read(8 * 1024 * 1024 + 1)
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)
    oversized = len(raw) > 8 * 1024 * 1024
    with (RUN / 'raw' / (key + '.bin')).open('xb') as output:
        output.write(raw)
    observation = dict(key=key, url=url, http_status=status, error=error, oversized=oversized,
        bytes=len(raw), sha256=sha(raw), retrieved_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        headers={k: v for k, v in headers.items() if k.lower() in (
            'date', 'content-type', 'etag', 'last-modified', 'link', 'location', 'memento-datetime',
            'x-github-request-id', 'x-ratelimit-remaining')},
        data_accepted=False, historical_publication_proven=False)
    save_new(RUN / 'receipts' / (key + '.json'), observation)
    print(json.dumps(observation, sort_keys=True))


def supply_summary(raw):
    obj = strict(raw)
    if isinstance(obj, dict) and isinstance(obj.get('data'), dict):
        obj = obj['data']
    need(isinstance(obj, dict) and isinstance(obj.get('since_merge'), list), 'SUPPLY_SCHEMA')
    points = obj['since_merge']
    days = {}
    timestamps = []
    for row in points:
        stamp = row['timestamp']
        moment = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        need(moment.utcoffset() == dt.timedelta(0), 'UTC_REQUIRED')
        from decimal import Decimal, InvalidOperation
        need(type(row['supply']) is not bool, 'SUPPLY_VALUE')
        try:
            value = Decimal(str(row['supply']))
        except InvalidOperation:
            raise ValueError('SUPPLY_VALUE') from None
        need(value.is_finite() and value > 0, 'SUPPLY_VALUE')
        key = moment.date().isoformat()
        # Endpoint appends the live last point; distinguish it from midnight observations.
        if moment.time().replace(tzinfo=None) == dt.time(0):
            need(key not in days, 'DUPLICATE_DAILY_POINT')
            days[key] = str(value)
        timestamps.append(stamp)
    first, final = dt.date(2022, 12, 24), dt.date(2025, 12, 27)
    wanted = [(first + dt.timedelta(days=i)).isoformat() for i in range((final-first).days+1)]
    samples = {}
    for decision in ('2023-09-18', '2024-08-05'):
        day = dt.date.fromisoformat(decision)
        samples[decision] = {str(day-dt.timedelta(days=i)): days.get(str(day-dt.timedelta(days=i)))
                             for i in range(9, 1, -1)}
    return dict(points=len(points), daily_points=len(days), first=min(timestamps) if timestamps else None,
        last=max(timestamps) if timestamps else None, required_days=len(wanted),
        missing_days=[d for d in wanted if d not in days], fixed_samples=samples,
        historical_publication_proven=False, admitted_to_old_contract=False)


def audit():
    f = initialized()
    attempts = list((RUN / 'attempts').glob('*.json'))
    receipts = list((RUN / 'receipts').glob('*.json'))
    need(len(attempts) == len(receipts) <= 30, 'RECEIPTS_INCOMPLETE')
    observations = []
    for p in sorted(receipts):
        r = strict(p.read_bytes())
        a = strict((RUN / 'attempts' / p.name).read_bytes())
        raw = (RUN / 'raw' / (p.stem + '.bin')).read_bytes()
        need(r['key'] == a['key'] == p.stem and r['url'] == a['url'] and allowed(r['url']), 'IDENTITY')
        need(len(raw) == r['bytes'] and sha(raw) == r['sha256'], 'RAW_IDENTITY')
        need(not r['data_accepted'] and not r['historical_publication_proven'], 'NO_AUTO_ADMISSION')
        observations.append(r)
    print(json.dumps(dict(preserved_files=len(f['preserved']), gets=len(attempts),
        status='DIAGNOSTIC_RECEIPTS_VERIFIED_ONLY', observations=observations), indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'probe', 'audit', 'supply'))
    parser.add_argument('--key')
    parser.add_argument('--url')
    args = parser.parse_args()
    if args.action == 'freeze':
        freeze()
    elif args.action == 'probe':
        probe(args.key, args.url)
    elif args.action == 'audit':
        audit()
    else:
        initialized()
        need(re.fullmatch('[a-z0-9-]+', args.key or ''), 'BAD_KEY')
        receipt = strict((RUN / 'receipts' / (args.key + '.json')).read_bytes())
        raw = (RUN / 'raw' / (args.key + '.bin')).read_bytes()
        need(receipt['http_status'] == 200 and receipt['error'] is None and not receipt['oversized']
             and sha(raw) == receipt['sha256'], 'SUPPLY_RESPONSE_REQUIRED')
        result = supply_summary(raw)
        save_new(RUN / (args.key + '-summary.json'), result)
        print(json.dumps(result, indent=2))
