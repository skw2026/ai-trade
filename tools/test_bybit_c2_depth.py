#!/usr/bin/env python3
"""Offline regression tests; no exchange or network dependency."""
import io
import json
import pathlib
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from urllib.error import HTTPError

import fetch_bybit_c2_depth_archive as fetch
import audit_bybit_c2_depth as audit
from decimal import Decimal, localcontext


def archive_bytes(content=b'{"fixture":true}\n', member=fetch.MEMBER):
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
    return target.getvalue()


class Response(io.BytesIO):
    def __init__(self, raw, size, etag='"test"'):
        super().__init__(raw)
        self.status = 200
        self.headers = {'Content-Length': str(size), 'ETag': etag}


class Opener:
    def __init__(self, raw, *, declared=None, get_etag='"test"'):
        self.raw, self.declared, self.get_etag = raw, len(raw) if declared is None else declared, get_etag
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        head = request.get_method() == 'HEAD'
        return Response(b'' if head else self.raw, self.declared, '"test"' if head else self.get_etag)


class DownloadTest(unittest.TestCase):
    def run_capture(self, opener):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / 'capture'
            result = fetch.collect(root, opener)
            self.assertEqual(json.loads((root / 'manifest.json').read_text()), result)
            return result

    def test_exact_public_file_hash_and_no_credentials(self):
        raw = archive_bytes()
        opener = Opener(raw)
        result = self.run_capture(opener)
        self.assertTrue(result['complete'])
        self.assertEqual(result['archive_sha256'], fetch.wire.digest(raw))
        self.assertEqual([r.get_method() for r in opener.requests], ['HEAD', 'GET'])
        self.assertEqual(opener.requests[1].get_header('If-match'), '"test"')
        self.assertTrue(all(r.full_url == fetch.URL for r in opener.requests))
        self.assertFalse(result['historical_liquidity_qualified'])

    def test_declared_oversize_rejected_before_get(self):
        opener = Opener(b'', declared=fetch.MAX_COMPRESSED + 1)
        result = self.run_capture(opener)
        self.assertFalse(result['complete'])
        self.assertEqual(len(opener.requests), 1)

    def test_truncated_changed_etag_and_wrong_member_are_rejected(self):
        for opener in [Opener(archive_bytes(), declared=99999),
                       Opener(archive_bytes(), get_etag='"changed"'),
                       Opener(archive_bytes(member='../escape.data'))]:
            with self.subTest(opener=opener):
                self.assertFalse(self.run_capture(opener)['complete'])

    def test_403_is_unavailable_not_empty_data(self):
        opener = Opener(b'')
        with patch.object(opener, 'open', side_effect=HTTPError(fetch.URL, 403, 'Forbidden', {}, None)):
            result = self.run_capture(opener)
        self.assertEqual(result['http_status'], 403)
        self.assertFalse(result['complete'])

    def test_existing_root_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileExistsError):
                fetch.collect(pathlib.Path(temporary), Opener(archive_bytes()))


def snapshot(stamp=100, update=10):
    return {'topic': 'orderbook.200.BTCUSDT', 'type': 'snapshot', 'ts': audit.DAY_START + stamp,
            'cts': audit.DAY_START + stamp - 2, 'data': {'s': 'BTCUSDT', 'u': update, 'seq': 100 + update,
            'b': [['100', '0.002'], ['99', '0.003']], 'a': [['101', '0.002'], ['102', '0.005']]}}


def delta(stamp=200, update=11, bids=None, asks=None):
    row = snapshot(stamp, update)
    row['type'] = 'delta'
    row['data'].update(b=bids or [], a=asks or [])
    return row


def anchor(stamp=100, side='bid'):
    return {'seq': 752, 'symbol': 'BTCUSDT', 'quote_ts_ms': audit.DAY_START + stamp, 'exit_side': side,
            'required_qty_btc': '0.005' if side == 'bid' else '0.007', 'price_usdt_per_btc': '100' if side == 'bid' else '101',
            'l1_size_btc': '0.002'}


class DepthReplayTest(unittest.TestCase):
    def test_exact_snapshot_full_sweep_decimal_cost_not_actual_fill(self):
        out = audit.replay_rows([snapshot(), delta()], [anchor()])
        found = out['anchors'][0]
        self.assertTrue(found['exact_anchor_depth_supported'])
        self.assertEqual(Decimal(found['sweep']['vwap_usdt']), Decimal('99.4'))
        self.assertEqual(Decimal(found['sweep']['extra_cost_vs_same_book_l1_usdt']), Decimal('.003'))
        self.assertEqual(found['sweep']['levels_used'], 2)
        self.assertFalse(found['actual_order_fill_proven'])
        self.assertFalse(out['historical_c2_qualified'])
        self.assertFalse(any(out['authorities'].values()))

    def test_prior_depth_never_becomes_exact_or_uses_future_liquidity(self):
        rows = [snapshot(), delta(bids=[['100', '999']])]
        out = audit.replay_rows(rows, [anchor(150)])['anchors'][0]
        self.assertEqual(out['status'], 'PRIOR_DEPTH_CONDITIONAL_ONLY')
        self.assertFalse(out['exact_anchor_depth_supported'])
        self.assertEqual(out['age_ms'], 50)
        self.assertEqual(out['sweep']['levels_used'], 2)
        self.assertFalse(out['future_book_used'])

    def test_same_timestamp_messages_are_applied_in_original_order(self):
        rows = [snapshot(), delta(stamp=100, bids=[['99', '0']]), delta(update=12)]
        out = audit.replay_rows(rows, [anchor()])['anchors'][0]
        self.assertEqual(out['status'], 'EXACT_PUBLISHED_STATE_DEPTH_INSUFFICIENT')
        self.assertEqual(Decimal(out['sweep']['unfilled_qty_btc']), Decimal('.003'))

    def test_sequence_gap_is_not_accepted_and_snapshot_resets_state(self):
        rows = [snapshot(), delta(update=12), delta(stamp=300, update=13)]
        self.assertEqual(audit.replay_rows(rows, [anchor(200)])['anchors'][0]['status'], 'UPDATE_GAP_SINCE_SNAPSHOT')
        rows += [snapshot(400, 1), delta(500, 2)]
        out = audit.replay_rows(rows, [anchor(400)])
        self.assertEqual(out['update_gap_events'], 1)
        self.assertTrue(out['anchors'][0]['exact_anchor_depth_supported'])

    def test_price_or_size_mismatch_remains_an_alignment_failure(self):
        for bids in ([['100', '0.003']], [['100', '0'], ['100.5', '0.002']]):
            rows = [snapshot(), delta(bids=bids), delta(stamp=300, update=12)]
            found = audit.replay_rows(rows, [anchor(200)])['anchors'][0]
            self.assertEqual(found['status'], 'PRIOR_DEPTH_L1_MISMATCH')
            self.assertFalse(found['exact_anchor_depth_supported'])

    def test_old_missing_and_trailing_states_not_qualified(self):
        self.assertEqual(audit.replay_rows([snapshot(), delta(stamp=500)], [anchor(250)])['anchors'][0]['status'], 'PRIOR_DEPTH_TOO_OLD')
        self.assertEqual(audit.replay_rows([snapshot()], [anchor(50)])['anchors'][0]['status'], 'NO_PRIOR_DEPTH_STATE')
        self.assertEqual(audit.replay_rows([snapshot()], [anchor(100)])['anchors'][0]['status'], 'ARCHIVE_END_NOT_BRACKETED')

    def test_invalid_sequence_time_symbol_crossed_book_and_duplicate_levels_rejected(self):
        cases = [delta(update=10), delta(stamp=99), delta(bids=[['102', '1']]),
                 delta(bids=[['99', '1'], ['99', '2']])]
        foreign = delta()
        foreign['data']['s'] = 'ETHUSDT'
        cases.append(foreign)
        restart = delta(update=1)
        cases.append(restart)
        for row in cases:
            with self.subTest(row=row), self.assertRaises(ValueError):
                audit.replay_rows([snapshot(), row], [anchor()])
        with self.assertRaisesRegex(ValueError, 'BEFORE_SNAPSHOT'):
            audit.replay_rows([delta()], [anchor()])

    def test_ask_sweep_and_low_ambient_decimal_precision(self):
        expected = audit.replay_rows([snapshot(), delta()], [anchor(side='ask')])
        with localcontext() as context:
            context.prec = 3
            actual = audit.replay_rows([snapshot(), delta()], [anchor(side='ask')])
        self.assertEqual(actual, expected)
        self.assertEqual(Decimal(actual['anchors'][0]['sweep']['extra_cost_vs_same_book_l1_usdt']), Decimal('.005'))

    def test_bounded_next_day_archive_boundary_is_reported_not_silently_trimmed(self):
        out = audit.replay_rows([snapshot(), delta(stamp=86400000 + 129)], [anchor()])
        self.assertEqual(out['outside_nominal_day_rows'], 1)
        with self.assertRaisesRegex(ValueError, 'TIME_INVALID'):
            audit.replay_rows([snapshot(), delta(stamp=86400000 + 1001)], [anchor()])
        with self.assertRaisesRegex(ValueError, 'ANCHOR_DATE'):
            audit.replay_rows([snapshot()], [anchor(stamp=86400000 + 1)])


class ArchiveReplayTest(unittest.TestCase):
    def test_pinned_archive_replay_crc_and_source_tampering(self):
        evidence = pathlib.Path(__file__).resolve().parents[1] / 'docs/reviews/2026-09-14-c2-acceptance-and-exit-evidence.evidence.json'
        evidence_sha = fetch.wire.digest(evidence.read_bytes())
        anchors, _ = audit.pinned_anchors(evidence, evidence_sha)
        rows = [snapshot()]
        for i, original in enumerate(anchors):
            row = snapshot(original['quote_ts_ms'] - audit.DAY_START, 20 + i)
            price = Decimal(original['price_usdt_per_btc'])
            row['data']['b'] = [[str(price), '0.002'], [str(price - 1), '1']]
            row['data']['a'] = [[str(price + 1), '0.002'], [str(price + 2), '1']]
            if original['exit_side'] == 'ask':
                row['data']['b'] = [[str(price - 1), '1']]
                row['data']['a'] = [[str(price), '0.002'], [str(price + 1), '1']]
            rows.append(row)
        rows.append(delta(rows[-1]['ts'] - audit.DAY_START + 100, 22))
        raw = archive_bytes(b''.join((json.dumps(r) + '\n').encode() for r in rows))
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / 'capture'
            fetch.collect(root, Opener(raw))
            manifest_sha = fetch.wire.digest((root / 'manifest.json').read_bytes())
            report = audit.replay(root, manifest_sha, evidence, evidence_sha)
            self.assertTrue(report['zip_crc_verified'])
            self.assertEqual(report['exact_anchor_depth_supported_count'], 2)
            self.assertEqual(report['archive_sha256'], fetch.wire.digest(raw))
            with self.assertRaisesRegex(ValueError, 'MANIFEST_HASH'):
                audit.replay(root, '0' * 64, evidence, evidence_sha)
            with self.assertRaisesRegex(ValueError, 'ANCHORS_HASH'):
                audit.replay(root, manifest_sha, evidence, '0' * 64)
            altered = bytearray(raw)
            altered[40] ^= 1
            (root / fetch.ARCHIVE).write_bytes(altered)
            with self.assertRaisesRegex(ValueError, 'ARCHIVE_HASH'):
                audit.replay(root, manifest_sha, evidence, evidence_sha)

    def test_anchor_summary_cannot_be_replaced_by_a_self_reported_finding(self):
        source = pathlib.Path(__file__).resolve().parents[1] / 'docs/reviews/2026-09-14-c2-acceptance-and-exit-evidence.evidence.json'
        altered = json.loads(source.read_text())
        altered['exit_evidence']['findings'][0]['required_qty_btc'] = '0.001'
        raw = fetch.wire.encode(altered)
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / 'evidence.json'
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'FROZEN_LEDGER'):
                audit.pinned_anchors(path, fetch.wire.digest(raw))


if __name__ == '__main__':
    unittest.main()
