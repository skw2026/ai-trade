#!/usr/bin/env python3
"""Bounded public Coinbase USDT/USD history; NOT Bybit's collateral index.

Archives raw numeric JSON and parses prices with Decimal, not binary floats.
Missing no-tick minutes remain missing. No credentials or account requests.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import pathlib
import tempfile
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request
from urllib.error import HTTPError, URLError

import audit_bybit_readonly_evidence as wire

ENDPOINT = 'https://api.exchange.coinbase.com/products/USDT-USD/candles'
SCHEMA = 'c2_external_fx_proxy_v1'
MINUTE = 60000
require = wire.require


def plan(start, end):
    wire.window(start, end)
    require(start >= 2 * MINUTE and end - start <= 2 * wire.DAY, 'FX_WINDOW_INVALID')
    first, last = start // MINUTE * MINUTE - MINUTE, end // MINUTE * MINUTE
    pages = []
    while first <= last:
        stop = min(last, first + 299 * MINUTE)
        pages.append({'start_ms': first, 'end_ms': stop})
        first = stop + MINUTE
    return pages


def iso(stamp):
    return dt.datetime.fromtimestamp(stamp // 1000, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Transport:
    def __init__(self):
        # Existing TLS, no-proxy, no-redirect public transport; only this fixed
        # public endpoint is constructed below, with no auth headers.
        self.opener = wire.Transport('public').opener

    def get(self, page):
        query = urlencode({'granularity': 60, 'start': iso(page['start_ms']), 'end': iso(page['end_ms'])})
        try:
            with self.opener.open(Request(ENDPOINT + '?' + query,
                    headers={'User-Agent': 'ai-trade-c2-fx-proxy/1'}, method='GET'), timeout=20) as response:
                require(response.status == 200, 'FX_HTTP_STATUS_INVALID')
                raw = response.read(wire.MAX_BYTES + 1)
        except HTTPError as exc:
            raise ValueError(f'FX_HTTP_{exc.code}') from None
        except (URLError, OSError, TimeoutError):
            raise ValueError('FX_TRANSPORT_ERROR') from None
        require(0 < len(raw) <= wire.MAX_BYTES, 'FX_RESPONSE_SIZE_INVALID')
        return raw


def collect(root, start, end, transport):
    pages = plan(start, end)
    require(wire.now_ms() > end // MINUTE * MINUTE + 2 * MINUTE, 'FX_WINDOW_NOT_CLOSED')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not root.is_symlink(), 'FX_ROOT_SYMLINK')
    capture = pathlib.Path(tempfile.mkdtemp(prefix='fx-proxy-', dir=root))
    manifest = {'schema_version': SCHEMA, 'endpoint': ENDPOINT, 'method': 'GET',
        'start_ms': start, 'end_ms': end, 'created_ms': wire.now_ms(), 'pages': [],
        'collector_sha256': wire.digest(pathlib.Path(__file__).read_bytes())}
    def checkpoint():
        wire.safe_file(capture / 'manifest.next', wire.encode(manifest))
        (capture / 'manifest.next').replace(capture / 'manifest.json')
    checkpoint()
    for page in pages:
        sent = wire.now_ms()
        raw = transport.get(page)
        name = f'{len(manifest["pages"]):04d}.raw'
        wire.safe_file(capture / name, raw)
        manifest['pages'].append({'request': page, 'file': name, 'sha256': wire.digest(raw),
            'sent_ms': sent, 'received_ms': wire.now_ms()})
        checkpoint()
    return capture


def replay(capture, sha):
    require(not capture.is_symlink(), 'FX_ROOT_SYMLINK')
    raw = wire.safe_read(capture / 'manifest.json')
    require(isinstance(sha, str) and wire.SHA.fullmatch(sha) and wire.digest(raw) == sha, 'FX_MANIFEST_HASH_MISMATCH')
    manifest = wire.decode(raw)
    require(manifest['schema_version'] == SCHEMA and manifest['endpoint'] == ENDPOINT and
            manifest['method'] == 'GET' and wire.SHA.fullmatch(manifest['collector_sha256']), 'FX_SOURCE_INVALID')
    start, end = manifest['start_ms'], manifest['end_ms']
    requests = plan(start, end)
    require(type(manifest['created_ms']) is int and manifest['created_ms'] > end // MINUTE * MINUTE + 2 * MINUTE,
            'FX_CAPTURE_NOT_CLOSED')
    require(len(requests) == len(manifest['pages']), 'FX_PAGE_CHAIN_INCOMPLETE')
    candles = {}
    for i, (request, page) in enumerate(zip(requests, manifest['pages'])):
        require(page['request'] == request and page['file'] == f'{i:04d}.raw', 'FX_REQUEST_PLAN_MISMATCH')
        require(type(page['sent_ms']) is int and type(page['received_ms']) is int and
            manifest['created_ms'] <= page['sent_ms'] <= page['received_ms'], 'FX_RECEIPT_CLOCK_INVALID')
        raw = wire.safe_read(capture / page['file'])
        require(wire.digest(raw) == page['sha256'], 'FX_RAW_HASH_MISMATCH')
        rows = json.loads(raw, parse_float=Decimal, parse_constant=lambda _: require(False, 'FX_NONFINITE'))
        require(isinstance(rows, list) and len(rows) <= 300, 'FX_ROWS_INVALID')
        seen = set()
        for row in rows:
            require(isinstance(row, list) and len(row) == 6 and type(row[0]) is int, 'FX_CANDLE_SHAPE_INVALID')
            stamp = row[0] * 1000
            require(stamp > 0 and stamp % MINUTE == 0 and stamp not in seen and stamp + MINUTE < manifest['created_ms'],
                    'FX_CANDLE_TIME_INVALID')
            seen.add(stamp)
            require(all(type(v) in (int, Decimal) for v in row[1:]), 'FX_PRICE_TYPE_INVALID')
            lo, hi, op, close, volume = map(Decimal, row[1:])
            require(all(v.is_finite() and abs(v) <= Decimal('1e18') for v in (lo, hi, op, close, volume)) and
                    0 < lo <= min(op, close) <= max(op, close) <= hi and volume >= 0, 'FX_OHLC_INVALID')
            # The API documents that some returned rows may precede start.
            if stamp < request['start_ms']:
                continue
            require(stamp <= request['end_ms'] and stamp not in candles, 'FX_CANDLE_OUTSIDE_REQUEST')
            candles[stamp] = {key: format(value, 'f') for key, value in
                              zip(('low', 'high', 'open', 'close'), (lo, hi, op, close))}
    expected = set(range(requests[0]['start_ms'], requests[-1]['end_ms'] + MINUTE, MINUTE))
    missing = sorted(expected - set(candles))
    require(candles, 'FX_HISTORY_EMPTY')
    summary = {'schema_version': SCHEMA, 'status': 'EXTERNAL_FX_PROXY_WITH_GAPS' if missing else 'EXTERNAL_FX_PROXY_REPLAYED',
        'endpoint': ENDPOINT, 'start_ms': start, 'end_ms': end, 'manifest_sha256': sha,
        'response_pages': len(requests), 'candle_count': len(candles), 'expected_candles': len(expected),
        'missing_minutes': len(missing), 'observed_low_usd_per_usdt': min((v['low'] for v in candles.values()), key=Decimal),
        'observed_high_usd_per_usdt': max((v['high'] for v in candles.values()), key=Decimal),
        'collector_sha256': manifest['collector_sha256'], 'replayer_sha256': wire.digest(pathlib.Path(__file__).read_bytes()),
        'bybit_historical_fx_qualified': False, 'unobserved_price_bound_proven': False,
        'independent_history_completeness_proven': False, 'order_submission': False, 'promotion_authority': False}
    return candles, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('collect', 'replay'))
    parser.add_argument('--root', type=pathlib.Path)
    parser.add_argument('--start-ms', type=int)
    parser.add_argument('--end-ms', type=int)
    parser.add_argument('--capture', type=pathlib.Path)
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    try:
        if args.action == 'collect':
            require(args.root is not None, 'FX_ROOT_REQUIRED')
            args.capture = collect(args.root, args.start_ms, args.end_ms, Transport())
            args.manifest_sha256 = wire.digest(wire.safe_read(args.capture / 'manifest.json'))
        require(args.capture is not None and args.manifest_sha256 is not None, 'FX_PIN_REQUIRED')
        _, report = replay(args.capture, args.manifest_sha256)
        if args.action == 'collect':
            wire.safe_file(args.capture / 'summary.json', wire.encode(report))
            report['capture_directory'] = str(args.capture)
        print(wire.encode(report).decode(), end='')
        return 0
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        print(wire.encode({'status': 'FX_PROXY_NOT_COMPLETED', 'reason': wire.error_code(exc),
                          'promotion_authority': False}).decode(), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
