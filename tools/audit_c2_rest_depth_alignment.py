#!/usr/bin/env python3
"""Compare frozen REST cts/seq with the already verified ob200 archive.

Metadata alignment only. Does not infer intermediate books, compare update IDs
across depth streams, reuse future liquidity, or issue execution qualification.
"""
from __future__ import annotations
import argparse
import hashlib
import pathlib
import zipfile

import audit_bybit_readonly_evidence as wire
import audit_bybit_c2_depth as depth
import audit_c2_rest_anchors as rest

require = wire.require


def witness(row, number):
    require(row.get('topic') == 'orderbook.200.BTCUSDT' and row.get('type') in ('snapshot', 'delta') and
            row['data'].get('s') == 'BTCUSDT', 'ALIGNMENT_MESSAGE_SCOPE_INVALID')
    values = [row['ts'], row['cts'], row['data']['seq'], row['data']['u']]
    require(all(type(v) is int and v > 0 for v in values), 'ALIGNMENT_METADATA_INVALID')
    require(values[1] <= values[0], 'ALIGNMENT_ENGINE_TIME_INVALID')
    return {'row_number': number, 'ts_ms': values[0], 'cts_ms': values[1], 'cross_seq': values[2],
            'update_id': values[3], 'type': row['type']}


def classify(anchor, tracked):
    book = anchor['book']
    before, after, equal = tracked['cross_seq']['before'], tracked['cross_seq']['after'], tracked['cross_seq']['equal']
    missing = [key for key in ('cts', 'u', 'seq') if key not in book]
    if missing:
        status = 'REST_METADATA_MISSING'
    elif equal:
        matches = [r for r in equal if r['cts_ms'] == book['cts']]
        if not matches:
            status = 'EQUAL_SEQUENCE_ENGINE_TIME_CONFLICT'
        elif not any(r['ts_ms'] <= book['ts'] for r in matches):
            status = 'SEQUENCE_CTS_MATCH_ONLY_LATER_PUBLICATION'
        else:
            status = 'SEQUENCE_CTS_MATCH_REQUIRES_FULL_DEPTH_CHECK'
    elif before and after and not before['cts_ms'] <= book['cts'] <= after['cts_ms']:
        status = 'CROSS_SEQUENCE_ENGINE_TIME_BRACKET_CONFLICT'
    elif before and after:
        status = 'REST_STATE_BETWEEN_OB200_UPDATES'
    else:
        status = 'REST_SEQUENCE_OUTSIDE_ARCHIVE_COVERAGE'
    gaps = {}
    if before and after and not missing:
        gaps = {'rest_cts_after_preceding_ms': book['cts'] - before['cts_ms'],
                'following_cts_after_rest_ms': after['cts_ms'] - book['cts'],
                'rest_publication_after_preceding_ms': book['ts'] - before['ts_ms'],
                'following_publication_after_rest_ms': after['ts_ms'] - book['ts']}
    return {'ledger_seq': anchor['ledger_seq'], 'status': status, 'rest_book': book,
        'required_qty_btc': anchor['required_qty_btc'], 'exit_side': anchor['exit_side'],
        'source_snapshot': anchor['source'], 'missing_metadata': missing, 'brackets': tracked, 'time_gaps': gaps,
        'exact_cross_sequence_message_count': len(equal), 'rest_u_comparable_to_ob200_u': False,
        'full_depth_at_rest_state_verified': False, 'future_liquidity_used': False,
        'actual_order_fill_proven': False}


def compare_rows(rows, anchors):
    require(len(anchors) == 2, 'ALIGNMENT_TWO_ANCHORS_REQUIRED')
    require(len({a['ledger_seq'] for a in anchors}) == 2, 'ALIGNMENT_DUPLICATE_ANCHOR')
    for anchor in anchors:
        book = anchor['book']
        require(book['s'] == 'BTCUSDT' and type(book['ts']) is int and book['ts'] > 0,
                'ALIGNMENT_REST_SCOPE_INVALID')
        require(all(type(book[k]) is int and book[k] > 0 for k in ('cts', 'seq', 'u') if k in book),
                'ALIGNMENT_REST_METADATA_INVALID')
        require('cts' not in book or 0 <= book['ts'] - book['cts'] <= 120000,
                'ALIGNMENT_REST_ENGINE_TIME_INVALID')
    trackers = [{name: {'before': None, 'after': None, 'equal': []}
                 for name in ('publication', 'engine_time', 'cross_seq')} for _ in anchors]
    last, count = None, 0
    for count, row in enumerate(rows, 1):
        require(count <= depth.MAX_ROWS, 'ALIGNMENT_ROW_BUDGET')
        seen = witness(row, count)
        if last:
            require(all(seen[key] >= last[key] for key in ('ts_ms', 'cts_ms', 'cross_seq')),
                    'ALIGNMENT_METADATA_REGRESSION')
        last = seen
        for anchor, tracks in zip(anchors, trackers):
            for name, key, field in (('publication', 'ts', 'ts_ms'), ('engine_time', 'cts', 'cts_ms'),
                                     ('cross_seq', 'seq', 'cross_seq')):
                target = anchor['book'].get(key)
                if target is None:
                    continue
                require(type(target) is int and target > 0, 'ALIGNMENT_REST_METADATA_INVALID')
                chain = tracks[name]
                if seen[field] < target:
                    chain['before'] = seen
                elif seen[field] == target:
                    require(len(chain['equal']) < 100, 'ALIGNMENT_EQUAL_MATCH_BUDGET')
                    chain['equal'].append(seen)
                elif chain['after'] is None:
                    chain['after'] = seen
    require(count > 0, 'ALIGNMENT_EMPTY_ARCHIVE')
    found = [classify(a, t) for a, t in zip(anchors, trackers)]
    return {'schema_version': 'c2_rest_depth_alignment_v1', 'status': 'REST_METADATA_ALIGNMENT_COMPLETED',
        'archive_rows_checked': count, 'anchors': found,
        'between_updates_count': sum(r['status'] == 'REST_STATE_BETWEEN_OB200_UPDATES' for r in found),
        'sequence_match_anchor_count': sum(r['exact_cross_sequence_message_count'] > 0 for r in found),
        'historical_c2_qualified': False, 'source_ledger_modified': False,
        'limits': ['metadata_comparison_not_full_depth_verifier', 'seq_delta_not_missing_packet_count',
                   'rest_u_and_ob200_u_different_streams', 'future_metadata_only_brackets_not_execution_input'],
        'authorities': {'order_submission': False, 'account_mode_change': False, 'promotion': False}}


def pinned_json(path, sha):
    raw = wire.safe_read(path)
    require(wire.SHA.fullmatch(sha) and wire.digest(raw) == sha, 'ALIGNMENT_INPUT_HASH_MISMATCH')
    return wire.decode(raw)


def audit(archive_path, rest_path, rest_sha, depth_evidence_path, depth_evidence_sha):
    original = pinned_json(rest_path, rest_sha)
    require(original['status'] == 'REST_ANCHORS_BOUND_TO_FROZEN_SOURCE' and
            original['target_snapshot_set_sha256'] == rest.TARGET_SHA and original['target_snapshot_count'] == 1179 and
            original['ledger_file_sha256'] == rest.LEDGER_SHA and original['source_ledger_modified'] is False and
            original['historical_c2_qualified'] is False and not any(original['authorities'].values()),
            'ALIGNMENT_REST_SOURCE_INVALID')
    evidence = pinned_json(depth_evidence_path, depth_evidence_sha)
    previous = evidence['local_report']
    require(wire.digest(wire.encode(previous)) == evidence['local_report_sha256'] and
            previous['status'] == 'DEPTH_REPLAYED_WITH_ALIGNMENT_VERDICTS' and
            previous['zip_crc_verified'] is True and previous['ledger_file_sha256'] == rest.LEDGER_SHA,
            'ALIGNMENT_DEPTH_REPLAY_IDENTITY_INVALID')
    require(len(original['anchors']) == len(previous['anchors']) == 2 and
            {r['ledger_seq'] for r in original['anchors']} == {r['ledger_seq'] for r in previous['anchors']},
            'ALIGNMENT_ANCHOR_SET_MISMATCH')
    require(rest.file_sha(archive_path, depth.fetch.MAX_COMPRESSED) == previous['archive_sha256'] and
            depth.fetch.inspect_archive(archive_path) == evidence['local_manifest']['zip'],
            'ALIGNMENT_ARCHIVE_IDENTITY_INVALID')
    for anchor in original['anchors']:
        prior = next(r for r in previous['anchors'] if r['ledger_seq'] == anchor['ledger_seq'])
        require(anchor['book']['ts'] == prior['anchor_quote_ts_ms'] and
                anchor['required_qty_btc'] == prior['required_qty_btc'] and anchor['exit_side'] == prior['exit_side'],
                'ALIGNMENT_ANCHOR_MISMATCH')
        side = 'b' if anchor['exit_side'] == 'bid' else 'a'
        require(wire.number(anchor['book'][side][0][0]) == wire.number(prior['original_l1_price_usdt']) and
                wire.number(anchor['book'][side][0][1]) == wire.number(prior['original_l1_size_btc']),
                'ALIGNMENT_FROZEN_L1_MISMATCH')
    digest, expanded = hashlib.sha256(), 0
    def rows(handle):
        nonlocal expanded
        while True:
            line = handle.readline(depth.MAX_LINE + 1)
            if not line:
                break
            require(len(line) <= depth.MAX_LINE and line.endswith(b'\n'), 'ALIGNMENT_LINE_INVALID')
            expanded += len(line)
            require(expanded <= depth.fetch.MAX_UNCOMPRESSED, 'ALIGNMENT_BYTE_BUDGET')
            digest.update(line)
            yield wire.decode(line)
    with zipfile.ZipFile(archive_path) as archive, archive.open(depth.fetch.MEMBER) as handle:
        report = compare_rows(rows(handle), original['anchors'])
    require(digest.hexdigest() == previous['member_sha256'] and expanded == previous['member_bytes'] and
            report['archive_rows_checked'] == previous['rows'], 'ALIGNMENT_VERIFIED_MEMBER_MISMATCH')
    # The independently replayed old source must identify the same preceding publication.
    for row in report['anchors']:
        prior = next(r for r in previous['anchors'] if r['ledger_seq'] == row['ledger_seq'])
        pub = row['brackets']['publication']['before']
        require(pub is not None and all(pub[key] == prior['book_identity'][key]
                for key in ('ts_ms', 'cts_ms', 'cross_seq', 'update_id')), 'ALIGNMENT_PREVIOUS_REPLAY_CONFLICT')
    return {**report, 'rest_report_sha256': rest_sha, 'depth_delivery_evidence_sha256': depth_evidence_sha,
        'archive_sha256': previous['archive_sha256'], 'member_sha256': digest.hexdigest(), 'member_bytes': expanded,
        'zip_crc_verified': True, 'target_snapshot_set_sha256': rest.TARGET_SHA, 'ledger_file_sha256': rest.LEDGER_SHA,
        'engine_sha256': wire.digest(pathlib.Path(__file__).read_bytes()),
        'dependency_sha256': {pathlib.Path(m.__file__).name: wire.digest(pathlib.Path(m.__file__).read_bytes())
                              for m in (wire, depth, rest, depth.fetch)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=pathlib.Path, required=True)
    parser.add_argument('--rest-report', type=pathlib.Path, required=True)
    parser.add_argument('--rest-report-sha256', required=True)
    parser.add_argument('--depth-evidence', type=pathlib.Path, required=True)
    parser.add_argument('--depth-evidence-sha256', required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        result = audit(args.archive, args.rest_report, args.rest_report_sha256, args.depth_evidence, args.depth_evidence_sha256)
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not args.output.parent.is_symlink(), 'ALIGNMENT_OUTPUT_PARENT_SYMLINK')
        wire.safe_file(args.output, wire.encode(result))
        print(wire.encode(result).decode(), end='')
        return 0
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError, zipfile.BadZipFile, StopIteration) as exc:
        print(wire.encode({'status': 'REST_ALIGNMENT_NOT_COMPLETED', 'reason': wire.error_code(exc),
                          'order_submission': False}).decode(), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
