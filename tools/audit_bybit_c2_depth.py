#!/usr/bin/env python3
"""Replay the pinned public ob200 archive against two frozen C2 L1 findings.

Decimal price/quantity arithmetic; original order, no interpolation or future
book substitution. A prior depth state is conditional evidence, not an exact
REST-state match or proof that an actual market order would have filled.
"""
from __future__ import annotations
import argparse
from decimal import Decimal, localcontext
import hashlib
import json
import pathlib
import sys
import zipfile

import audit_bybit_readonly_evidence as wire
import fetch_bybit_c2_depth_archive as fetch

require = wire.require
DAY_START = 1788739200000
MAX_LINE = 1024 * 1024
MAX_ROWS = 1000000
MAX_ASOF_AGE_MS = 100  # Published ob200 cadence; not a latency/fill guarantee.
FILE_BOUNDARY_PADDING_MS = 1000  # Archive includes its final next-day snapshot.


def integer(value):
    require(type(value) is int and value >= 0, 'DEPTH_INTEGER_INVALID')
    return value


def levels(rows):
    require(isinstance(rows, list) and len(rows) <= 2000, 'DEPTH_LEVEL_COUNT_INVALID')
    out = {}
    for row in rows:
        require(isinstance(row, list) and len(row) == 2, 'DEPTH_LEVEL_INVALID')
        price, size = map(wire.number, row)
        require(price > 0 and size >= 0 and price not in out, 'DEPTH_LEVEL_VALUE_INVALID')
        out[price] = size
    return out


class Book:
    def __init__(self):
        self.bids, self.asks, self.last = {}, {}, None
        self.snapshot_ts = None
        self.segment_update_gaps = 0
        self.update_gap_events = self.snapshot_count = 0

    def apply(self, row):
        require(set(row) == {'topic', 'type', 'ts', 'cts', 'data'} and
                row['topic'] == 'orderbook.200.BTCUSDT' and row['type'] in ('snapshot', 'delta'),
                'DEPTH_MESSAGE_SCOPE_INVALID')
        data = row['data']
        require(isinstance(data, dict) and set(data) == {'s', 'b', 'a', 'u', 'seq'} and
                data['s'] == 'BTCUSDT', 'DEPTH_DATA_SCOPE_INVALID')
        stamp, cts, update, seq = map(integer, (row['ts'], row['cts'], data['u'], data['seq']))
        require(DAY_START - FILE_BOUNDARY_PADDING_MS <= cts <= stamp <=
                DAY_START + wire.DAY + FILE_BOUNDARY_PADDING_MS and update > 0, 'DEPTH_TIME_INVALID')
        reset = row['type'] == 'snapshot'
        require(reset or self.last is not None, 'DEPTH_DELTA_BEFORE_SNAPSHOT')
        require(reset or update != 1, 'DEPTH_RESTART_REQUIRES_SNAPSHOT')
        if self.last:
            require(stamp >= self.last['ts_ms'] and cts >= self.last['cts_ms'], 'DEPTH_TIME_REGRESSION')
            if not reset:
                require(update > self.last['update_id'] and seq >= self.last['cross_seq'],
                        'DEPTH_SEQUENCE_REGRESSION')
        bids, asks = ({}, {}) if reset else (self.bids.copy(), self.asks.copy())
        for target, updates in ((bids, levels(data['b'])), (asks, levels(data['a']))):
            if reset:
                require(all(size > 0 for size in updates.values()), 'DEPTH_ZERO_IN_SNAPSHOT')
            for price, size in updates.items():
                if size == 0:
                    target.pop(price, None)
                else:
                    target[price] = size
        require(0 < len(bids) <= 200 and 0 < len(asks) <= 200, 'DEPTH_RECONSTRUCTED_LEVELS_INVALID')
        require(max(bids) < min(asks), 'DEPTH_CROSSED_BOOK')
        # Do not sort/reorder messages to hide missing or out-of-order input.
        if reset:
            self.snapshot_count += 1
            self.snapshot_ts, self.segment_update_gaps = stamp, 0
        elif update != self.last['update_id'] + 1:
            self.segment_update_gaps += 1
            self.update_gap_events += 1
        self.bids, self.asks = bids, asks
        self.last = {'ts_ms': stamp, 'cts_ms': cts, 'update_id': update, 'cross_seq': seq}


def sweep(book, side, quantity):
    require(side in ('bid', 'ask') and quantity > 0, 'DEPTH_SWEEP_INVALID')
    rows = sorted((book.bids if side == 'bid' else book.asks).items(), reverse=side == 'bid')
    remaining, value, taken = quantity, Decimal(0), []
    with localcontext() as context:
        context.prec = 100
        for price, size in rows:
            amount = min(size, remaining)
            taken.append({'price_usdt': format(price, 'f'), 'qty_btc': format(amount, 'f')})
            value += price * amount
            remaining -= amount
            if remaining == 0:
                break
        complete = remaining == 0
        extra = (rows[0][0] * quantity - value if side == 'bid' else value - rows[0][0] * quantity) if complete else None
        return {'full_qty_in_displayed_book': complete, 'filled_qty_btc': format(quantity - remaining, 'f'),
            'unfilled_qty_btc': format(remaining, 'f'), 'levels_used': len(taken), 'fills': taken,
            'vwap_usdt': format(value / quantity, 'f') if complete else None,
            'gross_notional_usdt': format(value, 'f'),
            'extra_cost_vs_same_book_l1_usdt': format(extra, 'f') if extra is not None else None,
            'fees_and_market_impact_included': False}


def observe(book, anchor, next_stamp):
    result = {'ledger_seq': anchor['seq'], 'symbol': anchor['symbol'], 'exit_side': anchor['exit_side'],
        'anchor_quote_ts_ms': anchor['quote_ts_ms'], 'required_qty_btc': anchor['required_qty_btc'],
        'original_l1_price_usdt': anchor['price_usdt_per_btc'], 'original_l1_size_btc': anchor['l1_size_btc'],
        'next_publication_ts_ms': next_stamp, 'future_book_used': False,
        'exact_anchor_depth_supported': False, 'actual_order_fill_proven': False}
    if book.last is None:
        return {**result, 'status': 'NO_PRIOR_DEPTH_STATE'}
    age = anchor['quote_ts_ms'] - book.last['ts_ms']
    require(age >= 0, 'DEPTH_FUTURE_BOOK_FORBIDDEN')
    side = book.bids if anchor['exit_side'] == 'bid' else book.asks
    best = max(side) if anchor['exit_side'] == 'bid' else min(side)
    same_price = best == wire.number(anchor['price_usdt_per_btc'])
    same_size = side[best] == wire.number(anchor['l1_size_btc'])
    cost = sweep(book, anchor['exit_side'], wire.number(anchor['required_qty_btc']))
    gap_free = book.segment_update_gaps == 0
    exact = age == 0 and same_price and same_size and gap_free and next_stamp is not None
    if not gap_free:
        status = 'UPDATE_GAP_SINCE_SNAPSHOT'
    elif next_stamp is None:
        status = 'ARCHIVE_END_NOT_BRACKETED'
    elif age > MAX_ASOF_AGE_MS:
        status = 'PRIOR_DEPTH_TOO_OLD'
    elif not same_price or not same_size:
        status = 'PRIOR_DEPTH_L1_MISMATCH'
    elif age > 0:
        status = 'PRIOR_DEPTH_CONDITIONAL_ONLY'
    else:
        status = 'EXACT_PUBLISHED_STATE_DEPTH_AVAILABLE' if cost['full_qty_in_displayed_book'] else 'EXACT_PUBLISHED_STATE_DEPTH_INSUFFICIENT'
    return {**result, 'status': status, 'book_identity': book.last.copy(), 'age_ms': age,
        'initializing_snapshot_ts_ms': book.snapshot_ts, 'update_gaps_since_snapshot': book.segment_update_gaps,
        'l1_price_matches': same_price, 'l1_size_matches': same_size,
        'reconstructed_l1_price_usdt': format(best, 'f'), 'reconstructed_l1_size_btc': format(side[best], 'f'),
        'exact_anchor_depth_supported': exact and cost['full_qty_in_displayed_book'],
        'bid_levels': len(book.bids), 'ask_levels': len(book.asks), 'sweep': cost}


def replay_rows(rows, anchors, *, progress=False):
    require(anchors and [a['quote_ts_ms'] for a in anchors] == sorted(a['quote_ts_ms'] for a in anchors),
            'DEPTH_ANCHOR_ORDER_INVALID')
    require(all(DAY_START <= a['quote_ts_ms'] < DAY_START + wire.DAY for a in anchors), 'DEPTH_ANCHOR_DATE_INVALID')
    book, findings, count, first, boundary_rows = Book(), [], 0, None, 0
    for row in rows:
        count += 1
        require(count <= MAX_ROWS, 'DEPTH_ROW_BUDGET')
        stamp = integer(row.get('ts'))
        boundary_rows += not DAY_START <= stamp < DAY_START + wire.DAY
        # Finalize against the preceding state before applying the next message.
        # Equal-timestamp messages remain in source order; retain the last one.
        while len(findings) < len(anchors) and anchors[len(findings)]['quote_ts_ms'] < stamp:
            findings.append(observe(book, anchors[len(findings)], stamp))
        book.apply(row)
        first = stamp if first is None else first
        if progress and count % 100000 == 0:
            print(f'Depth replay: rows={count}, anchors_observed={len(findings)}', file=sys.stderr, flush=True)
    require(count > 0, 'DEPTH_EMPTY_ARCHIVE')
    while len(findings) < len(anchors):
        findings.append(observe(book, anchors[len(findings)], None))
    return {'schema_version': 'bybit_c2_depth_replay_v1', 'status': 'DEPTH_REPLAYED_WITH_ALIGNMENT_VERDICTS',
        'rows': count, 'first_publication_ts_ms': first, 'last_publication_ts_ms': book.last['ts_ms'],
        'snapshot_count': book.snapshot_count, 'update_gap_events': book.update_gap_events,
        'file_boundary_padding_ms': FILE_BOUNDARY_PADDING_MS, 'outside_nominal_day_rows': boundary_rows,
        'anchors': findings, 'exact_anchor_depth_supported_count': sum(r['exact_anchor_depth_supported'] for r in findings),
        'source_row_order_preserved': True, 'independent_archive_completeness_proven': False,
        'historical_c2_qualified': False, 'source_ledger_modified': False,
        'limits': ['ob200_is_displayed_depth_not_all_liquidity', 'rpi_orders_excluded',
                   'prior_publication_is_not_exact_rest_snapshot', 'rest_cross_sequence_not_bound',
                   'no_execution_latency_or_market_impact_model', 'no_actual_exit_or_cashflow_rewrite'],
        'authorities': {'order_submission': False, 'account_mode_change': False, 'promotion': False}}


def pinned_anchors(path, expected_sha):
    raw = wire.safe_read(path)
    require(wire.SHA.fullmatch(expected_sha) and wire.digest(raw) == expected_sha, 'DEPTH_ANCHORS_HASH_MISMATCH')
    evidence = wire.decode(raw)
    chunks = evidence['summary_chunks']
    require(0 < len(chunks) <= 10 and [c['part'] for c in chunks] == list(range(len(chunks))) and
            all(c['total'] == len(chunks) and c['sha256'] == evidence['summary_sha256'] for c in chunks),
            'DEPTH_ANCHOR_CHUNKS_INVALID')
    merged = ''.join(c['text'] for c in chunks).encode()
    require(wire.digest(merged) == evidence['summary_sha256'], 'DEPTH_ANCHOR_SUMMARY_HASH_MISMATCH')
    original = wire.decode(merged)['exit_evidence']
    require(original == evidence['exit_evidence'] and original['ledger_file_sha256'] ==
            '4c642dc0a55b3caed712abce6016ffcbb9f56a078a4b81e211b1b37499ea7491', 'DEPTH_FROZEN_LEDGER_MISMATCH')
    anchors = original['findings']
    require(len(anchors) == 2 and all(a['symbol'] == 'BTCUSDT' and a['reasons'] == ['L1_SIZE_BELOW_POSITION']
                                    for a in anchors), 'DEPTH_ANCHOR_SCOPE_MISMATCH')
    return anchors, original['ledger_file_sha256']


def replay(capture, manifest_sha, evidence, evidence_sha):
    raw = wire.safe_read(capture / 'manifest.json')
    require(wire.SHA.fullmatch(manifest_sha) and wire.digest(raw) == manifest_sha, 'DEPTH_MANIFEST_HASH_MISMATCH')
    manifest = wire.decode(raw)
    require(manifest['complete'] is True and manifest['archive_file'] == fetch.ARCHIVE and
            manifest['source_url'] == fetch.URL, 'DEPTH_CAPTURE_NOT_COMPLETE')
    path = capture / fetch.ARCHIVE
    require(not path.is_symlink() and 0 < path.stat().st_size <= fetch.MAX_COMPRESSED, 'DEPTH_ARCHIVE_FILE_INVALID')
    require(path.stat().st_size == manifest['archive_bytes'], 'DEPTH_ARCHIVE_SIZE_MISMATCH')
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    require(digest.hexdigest() == manifest['archive_sha256'], 'DEPTH_ARCHIVE_HASH_MISMATCH')
    require(fetch.inspect_archive(path) == manifest['zip'], 'DEPTH_ZIP_MANIFEST_MISMATCH')
    anchors, ledger_sha = pinned_anchors(evidence, evidence_sha)
    raw_digest, raw_bytes = hashlib.sha256(), 0
    def rows(handle):
        nonlocal raw_bytes
        while True:
            line = handle.readline(MAX_LINE + 1)
            if not line:
                break
            require(len(line) <= MAX_LINE and line.endswith(b'\n'), 'DEPTH_LINE_INVALID')
            raw_bytes += len(line)
            require(raw_bytes <= fetch.MAX_UNCOMPRESSED, 'DEPTH_UNCOMPRESSED_BUDGET')
            raw_digest.update(line)
            yield wire.decode(line)
    with zipfile.ZipFile(path) as archive, archive.open(fetch.MEMBER) as handle:
        # Reading to EOF also verifies the ZIP member CRC, including after anchors.
        report = replay_rows(rows(handle), anchors, progress=True)
    require(raw_bytes == manifest['zip']['uncompressed_bytes'], 'DEPTH_UNCOMPRESSED_SIZE_MISMATCH')
    return {**report, 'archive_manifest_sha256': manifest_sha, 'archive_sha256': digest.hexdigest(),
        'archive_source_url': fetch.URL, 'member_sha256': raw_digest.hexdigest(), 'member_bytes': raw_bytes,
        'zip_crc_verified': True, 'anchor_evidence_sha256': evidence_sha, 'ledger_file_sha256': ledger_sha,
        'archive_collector_sha256': manifest['engine_sha256'],
        'engine_sha256': wire.digest(pathlib.Path(__file__).read_bytes()),
        'dependency_sha256': {pathlib.Path(m.__file__).name: wire.digest(pathlib.Path(m.__file__).read_bytes())
                              for m in (wire, fetch)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=pathlib.Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--anchor-evidence', type=pathlib.Path, required=True)
    parser.add_argument('--anchor-evidence-sha256', required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        report = replay(args.capture, args.manifest_sha256, args.anchor_evidence, args.anchor_evidence_sha256)
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not args.output.parent.is_symlink(), 'DEPTH_OUTPUT_PARENT_SYMLINK')
        wire.safe_file(args.output, wire.encode(report))
        print(wire.encode(report).decode(), end='')
        return 0
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError, zipfile.BadZipFile) as exc:
        print(wire.encode({'status': 'DEPTH_REPLAY_NOT_COMPLETED', 'reason': wire.error_code(exc),
                          'order_submission': False}).decode(), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
