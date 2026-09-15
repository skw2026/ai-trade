#!/usr/bin/env python3
"""Fetch exactly one public C2 depth archive, with bounded disk/network use.

No keys, cookies, redirects, proxies, retry loop, account changes or trading.
The raw archive stays private/ignored; its SHA binds subsequent offline replay.
"""
from __future__ import annotations
import argparse
import hashlib
import os
import pathlib
import shutil
import time
import zipfile
from urllib.request import Request
from urllib.error import HTTPError, URLError

import audit_bybit_readonly_evidence as wire

URL = 'https://quote-saver.bycsi.com/orderbook/linear/BTCUSDT/2026-09-07_BTCUSDT_ob200.data.zip'
ARCHIVE = '2026-09-07_BTCUSDT_ob200.data.zip'
MEMBER = ARCHIVE[:-4]
MAX_COMPRESSED = 200 * 1024 * 1024
MAX_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
require = wire.require


def inspect_archive(path):
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        require(len(members) == 1 and members[0].filename == MEMBER, 'DEPTH_ZIP_MEMBER_INVALID')
        item = members[0]
        require(not item.flag_bits & 1 and item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                'DEPTH_ZIP_ENCODING_INVALID')
        require(0 < item.file_size <= MAX_UNCOMPRESSED and 0 < item.compress_size <= MAX_COMPRESSED,
                'DEPTH_ZIP_SIZE_INVALID')
        require(item.file_size <= item.compress_size * 100, 'DEPTH_ZIP_RATIO_INVALID')
        return {'member': item.filename, 'uncompressed_bytes': item.file_size,
                'compressed_member_bytes': item.compress_size, 'crc32': format(item.CRC, '08x')}


def collect(root, opener=None):
    require(not root.is_symlink(), 'DEPTH_CAPTURE_ROOT_SYMLINK')
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    opener = opener or wire.Transport('public').opener
    report = {'schema_version': 'bybit_c2_depth_archive_v1', 'source_url': URL,
        'sent_ms': wire.now_ms(), 'engine_sha256': wire.digest(pathlib.Path(__file__).read_bytes()),
        'source_access': 'public_https_no_credentials', 'order_submission': False,
        'historical_liquidity_qualified': False, 'complete': False}
    try:
        with opener.open(Request(URL, method='HEAD'), timeout=20) as response:
            require(response.status == 200, 'DEPTH_HEAD_NOT_OK')
            size = int(response.headers.get('Content-Length', '0'))
            etag = response.headers.get('ETag')
            require(0 < size <= MAX_COMPRESSED, 'DEPTH_DOWNLOAD_SIZE_INVALID')
            require(etag and len(etag) <= 200, 'DEPTH_ETAG_REQUIRED')
            report.update(head_http_status=response.status, expected_bytes=size, etag=etag,
                          last_modified=response.headers.get('Last-Modified'))
        require(shutil.disk_usage(root).free >= 2 * MAX_COMPRESSED, 'DEPTH_DISK_BUDGET_UNAVAILABLE')
        digest, count, deadline = hashlib.sha256(), 0, time.monotonic() + 300
        request = Request(URL, headers={'If-Match': etag, 'User-Agent': 'ai-trade-c2-depth-archive/1'}, method='GET')
        with opener.open(request, timeout=20) as response:
            require(response.status == 200 and response.headers.get('ETag') == etag,
                    'DEPTH_DOWNLOAD_IDENTITY_CHANGED')
            require(int(response.headers.get('Content-Length', '0')) == size, 'DEPTH_CONTENT_LENGTH_CHANGED')
            fd = os.open(root / ARCHIVE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                while True:
                    require(time.monotonic() <= deadline, 'DEPTH_DOWNLOAD_TIME_BUDGET')
                    block = response.read(min(1024 * 1024, size - count + 1))
                    if not block:
                        break
                    count += len(block)
                    require(count <= size, 'DEPTH_DOWNLOAD_EXCEEDS_DECLARED_SIZE')
                    handle.write(block)
                    digest.update(block)
        require(count == size, 'DEPTH_DOWNLOAD_TRUNCATED')
        report.update(archive_file=ARCHIVE, archive_sha256=digest.hexdigest(), archive_bytes=count,
                      zip=inspect_archive(root / ARCHIVE), complete=True, status='DEPTH_ARCHIVE_DOWNLOADED')
    except HTTPError as exc:
        report.update(status='DEPTH_ARCHIVE_UNAVAILABLE', http_status=exc.code)
    except (ValueError, KeyError, TypeError, URLError, OSError, zipfile.BadZipFile) as exc:
        report.update(status='DEPTH_ARCHIVE_NOT_COMPLETED', reason=wire.error_code(exc))
    report['received_ms'] = wire.now_ms()
    wire.safe_file(root / 'manifest.json', wire.encode(report))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=pathlib.Path, required=True)
    args = parser.parse_args()
    result = collect(args.root)
    print(wire.encode(result).decode(), end='')
    raise SystemExit(0 if result['complete'] else 2)
