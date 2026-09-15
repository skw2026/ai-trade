#!/usr/bin/env python3
"""Synthetic alignment and fail-closed transport tests; not historical evidence."""
import copy
import pathlib
import subprocess
import tempfile
import textwrap
import unittest
import zipfile

import audit_c2_rest_depth_alignment as alignment

wire = alignment.wire


def message(ts, cts, seq, update=1):
    return {'topic': 'orderbook.200.BTCUSDT', 'type': 'snapshot' if update == 1 else 'delta',
            'ts': ts, 'cts': cts, 'data': {'s': 'BTCUSDT', 'seq': seq, 'u': update, 'b': [], 'a': []}}


def anchors():
    return [{'ledger_seq': i, 'book': {'s': 'BTCUSDT', 'ts': 150, 'cts': 145, 'seq': 15,
             'u': 999999, 'b': [['100', '0.002']], 'a': [['101', '0.003']]},
             'source': {'fixture': True}, 'exit_side': side, 'required_qty_btc': '0.005'}
            for i, side in ((752, 'bid'), (1177, 'ask'))]


def archive_rows():
    return [message(100, 95, 10), message(200, 195, 20, 2)]


class AlignmentTest(unittest.TestCase):
    def verdict(self, rows=None, sources=None):
        return alignment.compare_rows(rows or archive_rows(), sources or anchors())

    def test_between_updates_is_not_qualification_or_missing_packets(self):
        sources = anchors()
        old = copy.deepcopy(sources)
        result = self.verdict(sources=sources)
        self.assertEqual(result['between_updates_count'], 2)
        self.assertEqual(result['sequence_match_anchor_count'], 0)
        self.assertFalse(result['historical_c2_qualified'])
        self.assertFalse(any(result['authorities'].values()))
        row = result['anchors'][0]
        self.assertEqual(row['time_gaps']['rest_cts_after_preceding_ms'], 50)
        self.assertEqual(row['brackets']['cross_seq']['after']['cross_seq'], 20)
        self.assertFalse(row['future_liquidity_used'])
        self.assertFalse(row['full_depth_at_rest_state_verified'])
        self.assertFalse(row['actual_order_fill_proven'])
        self.assertFalse(row['rest_u_comparable_to_ob200_u'])
        self.assertEqual(sources, old)

    def test_equal_sequence_and_engine_time_still_requires_full_book(self):
        rows = [message(100, 95, 10), message(149, 145, 15, 2), message(200, 195, 20, 3)]
        report = self.verdict(rows)
        self.assertEqual(report['sequence_match_anchor_count'], 2)
        self.assertEqual(report['anchors'][0]['status'], 'SEQUENCE_CTS_MATCH_REQUIRES_FULL_DEPTH_CHECK')
        self.assertFalse(report['historical_c2_qualified'])

    def test_equal_state_published_later_is_not_available_liquidity(self):
        report = self.verdict([message(100, 95, 10), message(155, 145, 15, 2)])
        self.assertEqual(report['anchors'][0]['status'], 'SEQUENCE_CTS_MATCH_ONLY_LATER_PUBLICATION')
        self.assertFalse(report['anchors'][0]['future_liquidity_used'])

    def test_equal_sequence_conflicting_engine_time(self):
        result = self.verdict([message(100, 95, 10), message(149, 140, 15, 2)])
        self.assertEqual(result['anchors'][0]['status'], 'EQUAL_SEQUENCE_ENGINE_TIME_CONFLICT')

    def test_sequence_bracket_with_conflicting_engine_time_not_between(self):
        sources = anchors()
        sources[0]['book']['cts'] = 90
        self.assertEqual(self.verdict(sources=sources)['anchors'][0]['status'],
                         'CROSS_SEQUENCE_ENGINE_TIME_BRACKET_CONFLICT')

    def test_missing_metadata_not_zero_or_accepted(self):
        for key in ('cts', 'seq', 'u'):
            sources = anchors()
            del sources[0]['book'][key]
            with self.subTest(key=key):
                row = self.verdict(sources=sources)['anchors'][0]
                self.assertEqual(row['status'], 'REST_METADATA_MISSING')
                self.assertEqual(row['missing_metadata'], [key])
                self.assertNotIn(key, row['rest_book'])

    def test_outside_coverage_is_not_extrapolated(self):
        sources = anchors()
        sources[0]['book']['seq'] = 5
        sources[1]['book']['seq'] = 25
        self.assertTrue(all(r['status'] == 'REST_SEQUENCE_OUTSIDE_ARCHIVE_COVERAGE'
                            for r in self.verdict(sources=sources)['anchors']))

    def test_update_ids_are_not_compared_across_streams(self):
        first = self.verdict()
        sources = anchors()
        for source in sources:
            source['book']['u'] = 1
        second = self.verdict(sources=sources)
        self.assertEqual([a['status'] for a in first['anchors']], [a['status'] for a in second['anchors']])

    def test_metadata_regression_and_invalid_types_rejected(self):
        for key in ('ts', 'cts', 'seq'):
            rows = archive_rows()
            if key == 'ts': rows[1].update(ts=99, cts=95)
            elif key == 'cts': rows[1]['cts'] = 94
            else: rows[1]['data']['seq'] = 9
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'REGRESSION'):
                self.verdict(rows)
        for key in ('ts', 'cts', 'seq', 'u'):
            sources = anchors()
            sources[0]['book'][key] = True
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.verdict(sources=sources)
        rows = archive_rows()
        rows[0]['data']['u'] = True
        with self.assertRaises(ValueError): self.verdict(rows)

    def test_duplicate_empty_wrong_symbol_and_match_budget(self):
        sources = anchors()
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            self.verdict(sources=[sources[0], sources[0]])
        with self.assertRaisesRegex(ValueError, 'EMPTY'):
            alignment.compare_rows([], sources)
        rows = archive_rows()
        rows[0]['topic'] = 'orderbook.50.BTCUSDT'
        with self.assertRaisesRegex(ValueError, 'SCOPE'):
            self.verdict(rows)
        with self.assertRaisesRegex(ValueError, 'MATCH_BUDGET'):
            self.verdict([message(150, 145, 15)] * 101)

    def test_full_file_binding_and_tampering_rejected(self):
        # Synthetic archive uses real pin field names, not real historical results.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            archive, rest_path, proof_path = (root / p for p in ('depth.zip', 'rest.json', 'proof.json'))
            content = b''.join(wire.encode(row).replace(b'\n', b'') + b'\n' for row in archive_rows())
            with zipfile.ZipFile(archive, 'w') as handle:
                handle.writestr(alignment.depth.fetch.MEMBER, content)
            sources = anchors()
            original = {'status': 'REST_ANCHORS_BOUND_TO_FROZEN_SOURCE', 'anchors': sources,
                'target_snapshot_count': 1179, 'target_snapshot_set_sha256': alignment.rest.TARGET_SHA,
                'ledger_file_sha256': alignment.rest.LEDGER_SHA, 'source_ledger_modified': False,
                'historical_c2_qualified': False, 'authorities': {'order_submission': False}}
            rest_path.write_bytes(wire.encode(original))
            previous = {'status': 'DEPTH_REPLAYED_WITH_ALIGNMENT_VERDICTS', 'zip_crc_verified': True,
                'ledger_file_sha256': alignment.rest.LEDGER_SHA,
                'archive_sha256': wire.digest(archive.read_bytes()), 'member_sha256': wire.digest(content),
                'member_bytes': len(content), 'rows': 2,
                'anchors': [{'ledger_seq': a['ledger_seq'], 'anchor_quote_ts_ms': 150,
                    'required_qty_btc': a['required_qty_btc'], 'exit_side': a['exit_side'],
                    'original_l1_price_usdt': a['book']['b' if a['exit_side'] == 'bid' else 'a'][0][0],
                    'original_l1_size_btc': a['book']['b' if a['exit_side'] == 'bid' else 'a'][0][1],
                    'book_identity': {'ts_ms': 100, 'cts_ms': 95, 'cross_seq': 10, 'update_id': 1}}
                    for a in sources]}
            proof = {'local_report': previous, 'local_report_sha256': wire.digest(wire.encode(previous)),
                     'local_manifest': {'zip': alignment.depth.fetch.inspect_archive(archive)}}
            proof_path.write_bytes(wire.encode(proof))
            def run():
                return alignment.audit(archive, rest_path, wire.digest(rest_path.read_bytes()),
                                       proof_path, wire.digest(proof_path.read_bytes()))
            self.assertEqual(run()['archive_rows_checked'], 2)
            previous['member_sha256'] = '0' * 64
            proof['local_report_sha256'] = wire.digest(wire.encode(previous))
            proof_path.write_bytes(wire.encode(proof))
            with self.assertRaisesRegex(ValueError, 'MEMBER_MISMATCH'): run()
            with self.assertRaisesRegex(ValueError, 'INPUT_HASH'):
                alignment.pinned_json(rest_path, '0' * 64)
            archive.write_bytes(archive.read_bytes() + b'changed')
            with self.assertRaisesRegex(ValueError, 'ARCHIVE_IDENTITY'): run()


class TransportTest(unittest.TestCase):
    def script(self):
        workflow = pathlib.Path(__file__).resolve().parents[1] / '.github/workflows/c2-rest-anchors.yml'
        content = workflow.read_text()
        self.assertNotIn('appleboy/scp-action', content)
        return textwrap.dedent(content.rsplit('        run: |\n', 1)[1])

    def test_shell_syntax_and_missing_fingerprint_stop_before_any_external_command(self):
        script = self.script()
        self.assertEqual(subprocess.run(['/bin/bash', '-n'], input=script, text=True, capture_output=True).returncode, 0)
        result = subprocess.run(['/bin/bash', '-c', script], text=True, capture_output=True,
            env={'PATH': '', 'AUDIT_RUN_ID': '123-1', 'BUNDLE_SHA': 'a' * 64, 'ECS_HOST': 'example.invalid',
                 'ECS_USER': 'test', 'ECS_PORT': '22', 'ECS_FINGERPRINT': ''})
        self.assertEqual(result.returncode, 1)
        self.assertIn('REST_HOST_FINGERPRINT_NOT_CONFIGURED', result.stdout)
        self.assertEqual(result.stderr, '')

    def test_only_pinned_host_key_trusted_before_copy(self):
        script = self.script()
        self.assertLess(script.index('REST_HOST_FINGERPRINT_MISMATCH'), script.index('tar -czf'))
        self.assertLess(script.index('REST_HOST_FINGERPRINT_NOT_CONFIGURED'), script.index('ssh-keyscan'))
        self.assertIn('> "${KEY_DIR}/scanned"', script)
        self.assertIn('>> "${KEY_DIR}/known_hosts"', script)
        for option in ('StrictHostKeyChecking=yes', 'GlobalKnownHostsFile=/dev/null',
                       'UpdateHostKeys=no', 'BatchMode=yes', 'IdentitiesOnly=yes'):
            self.assertIn(option, script)
        self.assertEqual(script.count('ssh "${ssh_options[@]}"'), 2)


if __name__ == '__main__':
    unittest.main()
