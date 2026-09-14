#!/usr/bin/env python3
"""One public metadata request for the fixed C2 BTCUSDT depth date.

No auth, cookies, redirect, proxy, retries, account operations or bulk download.
Preserve raw bytes privately; emit only bounded public directory metadata.
"""
import argparse
import pathlib
from urllib.request import Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
import audit_bybit_readonly_evidence as wire

URL = ('https://www.bybit.com/x-api/quote/public/support/download/list-files'
       '?bizType=contract&productId=orderbook&symbols=BTCUSDT&interval=daily&periods='
       '&startDay=2026-09-07&endDay=2026-09-07')


def probe(root, opener=None):
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    wire.require(not root.is_symlink(), 'DEPTH_ROOT_SYMLINK')
    opener = opener or wire.Transport('public').opener
    report = {'schema_version': 'bybit_c2_depth_source_probe_v1', 'request_url': URL,
        'sent_ms': wire.now_ms(), 'engine_sha256': wire.digest(pathlib.Path(__file__).read_bytes()),
        'depth_downloaded': False, 'order_submission': False, 'historical_liquidity_qualified': False}
    try:
        with opener.open(Request(URL, headers={'User-Agent': 'ai-trade-c2-depth-probe/1'}, method='GET'), timeout=20) as response:
            report['http_status'] = response.status
            raw = response.read(65537)
        wire.require(0 < len(raw) <= 65536, 'DEPTH_METADATA_SIZE_INVALID')
        wire.safe_file(root / 'response.raw', raw)
        report['response_sha256'] = wire.digest(raw)
        data = wire.decode(raw)
        report['ret_code'] = data.get('ret_code')
        wire.require(data.get('ret_code') == 0, 'DEPTH_DIRECTORY_API_ERROR')
        files = data['result']['list']
        wire.require(isinstance(files, list) and len(files) <= 100, 'DEPTH_FILE_LIST_INVALID')
        summaries = []
        for row in files:
            wire.require(isinstance(row, dict), 'DEPTH_FILE_METADATA_INVALID')
            target = urlsplit(row.get('url', ''))
            # Do not publish signed query strings or accidentally turn metadata
            # into authority to download arbitrary URLs.
            summaries.append({'filename': row.get('filename'), 'host': target.hostname,
                              'path': target.path, 'has_query': bool(target.query),
                              'size': row.get('size', row.get('fileSize'))})
        report.update(status='PUBLIC_DEPTH_DIRECTORY_AVAILABLE' if files else 'PUBLIC_DEPTH_DIRECTORY_EMPTY', files=summaries)
    except HTTPError as exc:
        report.update(status='PUBLIC_DEPTH_DIRECTORY_HTTP_UNAVAILABLE', http_status=exc.code)
    except (ValueError, KeyError, TypeError, URLError, OSError, TimeoutError) as exc:
        report.update(status='PUBLIC_DEPTH_DIRECTORY_NOT_COMPLETED', reason=wire.error_code(exc))
    report['received_ms'] = wire.now_ms()
    wire.safe_file(root / 'summary.json', wire.encode(report))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=pathlib.Path, required=True)
    args = parser.parse_args()
    print(wire.encode(probe(args.root)).decode(), end='')
