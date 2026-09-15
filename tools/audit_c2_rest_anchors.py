#!/usr/bin/env python3
"""Bind archived REST metadata to the frozen C2 ledger and snapshot-set hash.

Reads finalized V4 public-market archives only. No account API, network,
credentials, production configuration changes or historical certification.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import lzma
import pathlib

import audit_bybit_readonly_evidence as wire
import audit_bybit_c2_depth as depth

require = wire.require
TARGET = 'btc-usdt-1788768000000-79750-a9f8f42224b2'
TARGET_SHA = '3bad663f05b17753a6dbb7c32c064783550c8215b89c8cb4c18a1fcd4999e17c'
LEDGER_SHA = '4c642dc0a55b3caed712abce6016ffcbb9f56a078a4b81e211b1b37499ea7491'
POLICY_SHA = '3057e78a46208d72ec4990ea9604744f0ed211e90b3c5f407a1132ee0927d253'
MANIFEST_SHA = 'ea77d54faa383ad5b5f21c520d2d90ecf8ea8b304af8be316f55602d16e20036'
START, END = 1788701451985, 1788768000000
ROOT_NAME = 'bybit_btc_option_lifecycle_v4'
EXPERIMENT = 'btc_bybit_usdt_option_lifecycle_capture_v4'
MAX_SEGMENT_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_TOTAL_EXPANDED = 128 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def file_sha(path, maximum):
    require(not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= maximum, 'REST_SOURCE_FILE_INVALID')
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def target_snapshots(root):
    require(not root.is_symlink() and root.name == ROOT_NAME and root.is_dir(), 'REST_CAPTURE_ROOT_INVALID')
    reports = root / 'reports' / 'BTC'
    require(reports.is_dir() and not reports.is_symlink(), 'REST_REPORTS_MISSING')
    paths = sorted(reports.glob('*.json'))
    require(0 < len(paths) <= 4096, 'REST_REPORT_COUNT_INVALID')
    selected, identities, expanded = {}, [], 0
    for path in paths:
        raw_report = wire.safe_read(path)
        report = wire.decode(raw_report)
        coverage = report.get('coverage', {})
        start, end = coverage.get('capture_started_epoch_ms'), coverage.get('capture_completed_epoch_ms')
        require(type(start) is int and type(end) is int and 0 < start <= end, 'REST_REPORT_COVERAGE_INVALID')
        if end < START or start > END + 180000:
            continue
        require(report.get('schema_version') == 'bybit_btc_option_lifecycle_capture_v4' and
                report.get('status') == 'PASS' and report.get('experiment_id') == EXPERIMENT and
                report.get('policy_canonical_sha256') == POLICY_SHA and
                report.get('manifest_canonical_sha256') == MANIFEST_SHA and
                report.get('raw_codec') == 'xz_lzma_preset1', 'REST_REPORT_CONTRACT_INVALID')
        require(len(identities) < 256, 'REST_SEGMENT_BUDGET')
        meta = report['raw']
        relative = f'raw/BTC/{path.stem}.jsonl.xz'
        require(meta.get('path') == relative, 'REST_RAW_PATH_INVALID')
        source = root / relative
        require(source.parent.resolve() == root.resolve() / 'raw' / 'BTC', 'REST_RAW_PARENT_INVALID')
        raw_sha = file_sha(source, MAX_SEGMENT_BYTES)
        require(raw_sha == meta['sha256'], 'REST_RAW_HASH_MISMATCH')
        rows, previous = 0, 0
        with lzma.open(source, 'rb') as handle:
            while True:
                line = handle.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                require(len(line) <= MAX_LINE_BYTES and line.endswith(b'\n'), 'REST_RAW_LINE_INVALID')
                expanded += len(line)
                require(expanded <= MAX_TOTAL_EXPANDED, 'REST_EXPANDED_BUDGET')
                rows += 1
                row = wire.decode(line)
                stamp = row.get('timestamp_epoch_ms')
                require(type(stamp) is int and max(start, previous) <= stamp <= end, 'REST_SNAPSHOT_TIME_INVALID')
                previous = stamp
                require(row.get('schema_version') == 'bybit_btc_option_lifecycle_snapshot_v4' and
                        row.get('experiment_id') == EXPERIMENT and row.get('policy_canonical_sha256') == POLICY_SHA and
                        row.get('manifest_canonical_sha256') == MANIFEST_SHA, 'REST_SNAPSHOT_CONTRACT_INVALID')
                lifecycle = row.get('active_lifecycle')
                if not isinstance(lifecycle, dict) or lifecycle.get('lifecycle_id') != TARGET:
                    continue
                require(stamp not in selected or canonical(selected[stamp]['snapshot']) == canonical(row),
                        'REST_CONFLICTING_DUPLICATE')
                selected.setdefault(stamp, {'snapshot': row, 'source': {'report_file': path.name,
                    'report_sha256': wire.digest(raw_report), 'raw_file': relative, 'raw_sha256': raw_sha,
                    'line': rows, 'snapshot_canonical_sha256': wire.digest(canonical(row))}})
        require(rows == meta.get('snapshot_count') == coverage.get('successful_poll_count') and rows > 0,
                'REST_SEGMENT_COUNT_MISMATCH')
        identities.append({'report_file': path.name, 'report_sha256': wire.digest(raw_report),
                           'raw_file': relative, 'raw_sha256': raw_sha, 'rows': rows})
    ordered = [selected[stamp] for stamp in sorted(selected)]
    require(len(ordered) == 1179 and wire.digest(canonical([r['snapshot'] for r in ordered])) == TARGET_SHA,
            'REST_FROZEN_SNAPSHOT_SET_MISMATCH')
    return ordered, identities


def extract_rows(rows, ledger, anchors):
    found = []
    for anchor in anchors:
        event = ledger['events'][anchor['seq']]
        quote = event['valuation']['BTCUSDT']
        require(event['seq'] == anchor['seq'] and event['ts_ms'] == anchor['ts_ms'] and
                quote['ts_ms'] == anchor['quote_ts_ms'], 'REST_LEDGER_ANCHOR_MISMATCH')
        matching = [r for r in rows if r['snapshot'].get('hedge_orderbook_l1', {}).get('ts') == quote['ts_ms'] and
                    max(r['snapshot']['snapshot_completed_epoch_ms'], quote['ts_ms']) == event['ts_ms']]
        require(len(matching) == 1, 'REST_UNIQUE_ANCHOR_SNAPSHOT_REQUIRED')
        source, snapshot = matching[0]['source'], matching[0]['snapshot']
        book = snapshot['hedge_orderbook_l1']
        require(book.get('s') == 'BTCUSDT' and len(book['b']) == len(book['a']) == 1, 'REST_L1_SCOPE_INVALID')
        for side, key in (('b', 'bid'), ('a', 'ask')):
            require(len(book[side][0]) == 2 and wire.number(book[side][0][0]) == wire.number(quote[key]) and
                    wire.number(book[side][0][1]) == wire.number(quote[key + '_size']), 'REST_LEDGER_BBO_MISMATCH')
        missing = [key for key in ('cts', 'u', 'seq') if key not in book]
        for key in ('cts', 'u', 'seq'):
            if key in book:
                require(type(book[key]) is int and book[key] > 0, 'REST_METADATA_INTEGER_INVALID')
        if 'cts' in book:
            require(0 <= book['ts'] - book['cts'] <= 120000, 'REST_ENGINE_TIME_INVALID')
        found.append({'ledger_seq': anchor['seq'], 'ledger_event_ts_ms': event['ts_ms'],
            'quote_ts_ms': quote['ts_ms'], 'exit_side': anchor['exit_side'],
            'required_qty_btc': anchor['required_qty_btc'], 'source': source,
            'poll_started_ms': snapshot['poll_started_epoch_ms'], 'poll_completed_ms': snapshot['snapshot_completed_epoch_ms'],
            'book': {key: book[key] for key in ('s', 'b', 'a', 'ts', 'cts', 'u', 'seq') if key in book},
            'missing_metadata': missing, 'ledger_both_sides_match': True})
    return found


def extract(root, ledger_path, evidence, evidence_sha):
    raw_ledger = wire.safe_read(ledger_path)
    require(wire.digest(raw_ledger) == LEDGER_SHA, 'REST_LEDGER_HASH_MISMATCH')
    anchors, ledger_sha = depth.pinned_anchors(evidence, evidence_sha)
    require(ledger_sha == LEDGER_SHA, 'REST_ANCHOR_LEDGER_MISMATCH')
    rows, identities = target_snapshots(root)
    found = extract_rows(rows, wire.decode(raw_ledger), anchors)
    return {'schema_version': 'c2_archived_rest_anchors_v1', 'status': 'REST_ANCHORS_BOUND_TO_FROZEN_SOURCE',
        'target_snapshot_set_sha256': TARGET_SHA, 'target_snapshot_count': len(rows),
        'ledger_file_sha256': LEDGER_SHA, 'anchor_evidence_sha256': evidence_sha,
        'segments_checked': len(identities), 'segment_identities_sha256': wire.digest(canonical(identities)),
        'anchors': found, 'rest_metadata_complete_count': sum(not a['missing_metadata'] for a in found),
        'source_ledger_modified': False, 'historical_c2_qualified': False,
        'engine_sha256': wire.digest(pathlib.Path(__file__).read_bytes()),
        'dependency_sha256': {pathlib.Path(m.__file__).name: wire.digest(pathlib.Path(m.__file__).read_bytes())
                              for m in (wire, depth, depth.fetch)},
        'authorities': {'order_submission': False, 'account_mode_change': False, 'promotion': False}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=pathlib.Path, required=True)
    parser.add_argument('--ledger', type=pathlib.Path, required=True)
    parser.add_argument('--anchor-evidence', type=pathlib.Path, required=True)
    parser.add_argument('--anchor-evidence-sha256', required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        result = extract(args.root, args.ledger, args.anchor_evidence, args.anchor_evidence_sha256)
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not args.output.parent.is_symlink(), 'REST_OUTPUT_PARENT_SYMLINK')
        wire.safe_file(args.output, wire.encode(result))
        print(wire.encode(result).decode(), end='')
        return 0
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError, lzma.LZMAError) as exc:
        print(wire.encode({'status': 'REST_ANCHOR_EXTRACTION_NOT_COMPLETED', 'reason': wire.error_code(exc),
                          'order_submission': False}).decode(), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
