#!/usr/bin/env python3
import copy
import json
import lzma
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import audit_c2_rest_anchors as rest


def snapshot(stamp=rest.START):
    return {'schema_version': 'bybit_btc_option_lifecycle_snapshot_v4', 'experiment_id': rest.EXPERIMENT,
            'policy_canonical_sha256': rest.POLICY_SHA, 'manifest_canonical_sha256': rest.MANIFEST_SHA,
            'timestamp_epoch_ms': stamp, 'poll_started_epoch_ms': stamp,
            'snapshot_completed_epoch_ms': stamp + 300,
            'active_lifecycle': {'lifecycle_id': rest.TARGET},
            'hedge_orderbook_l1': {'s': 'BTCUSDT', 'ts': stamp + 200, 'cts': stamp + 198,
                                   'u': 123, 'seq': 456, 'b': [['100', '0.002']], 'a': [['101', '0.003']]}}


def extraction_fixture():
    row = snapshot()
    source = {'raw_file': 'synthetic.jsonl.xz', 'snapshot_canonical_sha256': rest.wire.digest(rest.canonical(row))}
    ledger = {'events': [{'seq': 0, 'ts_ms': rest.START + 300, 'valuation': {'BTCUSDT':
              {'ts_ms': rest.START + 200, 'bid': '100', 'ask': '101', 'bid_size': '0.002', 'ask_size': '0.003'}}}]}
    anchor = {'seq': 0, 'ts_ms': rest.START + 300, 'quote_ts_ms': rest.START + 200,
              'exit_side': 'bid', 'required_qty_btc': '0.005'}
    return [{'snapshot': row, 'source': source}], ledger, [anchor]


class RestAnchorsTest(unittest.TestCase):
    def test_both_sides_and_original_timestamps_bound_without_rewrite(self):
        rows, ledger, anchors = extraction_fixture()
        old = copy.deepcopy((rows, ledger, anchors))
        report = rest.extract_rows(rows, ledger, anchors)[0]
        self.assertEqual(report['book'], rows[0]['snapshot']['hedge_orderbook_l1'])
        self.assertTrue(report['ledger_both_sides_match'])
        self.assertEqual(report['missing_metadata'], [])
        self.assertEqual((rows, ledger, anchors), old)

    def test_missing_sequence_is_a_gap_not_fabricated_zero(self):
        rows, ledger, anchors = extraction_fixture()
        del rows[0]['snapshot']['hedge_orderbook_l1']['seq']
        result = rest.extract_rows(rows, ledger, anchors)[0]
        self.assertEqual(result['missing_metadata'], ['seq'])
        self.assertNotIn('seq', result['book'])

    def test_wrong_bbo_other_side_and_metadata_type_rejected(self):
        for change in ('ask', 'bid_size', 'timestamp', 'seq'):
            rows, ledger, anchors = extraction_fixture()
            book = rows[0]['snapshot']['hedge_orderbook_l1']
            if change == 'ask': book['a'][0][0] = '102'
            elif change == 'bid_size': book['b'][0][1] = '0.001'
            elif change == 'timestamp': book['ts'] += 1
            else: book['seq'] = True
            with self.subTest(change=change), self.assertRaises(ValueError):
                rest.extract_rows(rows, ledger, anchors)

    def test_nonunique_source_does_not_arbitrarily_select_one(self):
        rows, ledger, anchors = extraction_fixture()
        with self.assertRaisesRegex(ValueError, 'UNIQUE'):
            rest.extract_rows(rows + rows, ledger, anchors)

    def test_frozen_target_hash_and_raw_hash_are_both_checked(self):
        rows = [snapshot(rest.START + i * 1000) for i in range(1179)]
        target_sha = rest.wire.digest(rest.canonical(rows))
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / rest.ROOT_NAME
            (root / 'reports/BTC').mkdir(parents=True)
            (root / 'raw/BTC').mkdir(parents=True)
            raw_path = root / 'raw/BTC/fixture.jsonl.xz'
            raw_path.write_bytes(lzma.compress(b''.join((json.dumps(r) + '\n').encode() for r in rows)))
            report = {'schema_version': 'bybit_btc_option_lifecycle_capture_v4', 'status': 'PASS',
                'experiment_id': rest.EXPERIMENT, 'policy_canonical_sha256': rest.POLICY_SHA,
                'manifest_canonical_sha256': rest.MANIFEST_SHA, 'raw_codec': 'xz_lzma_preset1',
                'coverage': {'capture_started_epoch_ms': rows[0]['timestamp_epoch_ms'],
                    'capture_completed_epoch_ms': rows[-1]['snapshot_completed_epoch_ms'], 'successful_poll_count': 1179},
                'raw': {'path': 'raw/BTC/fixture.jsonl.xz', 'snapshot_count': 1179,
                    'sha256': rest.wire.digest(raw_path.read_bytes())}}
            report_path = root / 'reports/BTC/fixture.json'
            report_path.write_bytes(rest.wire.encode(report))
            with patch.object(rest, 'TARGET_SHA', target_sha):
                selected, identities = rest.target_snapshots(root)
                self.assertEqual(len(selected), 1179)
                self.assertEqual(len(identities), 1)
            with self.assertRaisesRegex(ValueError, 'FROZEN_SNAPSHOT_SET'):
                rest.target_snapshots(root)
            report['raw']['sha256'] = '0' * 64
            report_path.write_bytes(rest.wire.encode(report))
            with self.assertRaisesRegex(ValueError, 'RAW_HASH'):
                rest.target_snapshots(root)


if __name__ == '__main__':
    unittest.main()
