#!/usr/bin/env python3
"""Small offline fixtures for the read-only single-segment diagnostic."""

import contextlib
import io
import json
import lzma
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import diagnose_option_lifecycle_segment as diagnostic


class SegmentDiagnosticTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.release_sha = 'a' * 40
        self.release = base / self.release_sha
        (self.release / 'config').mkdir(parents=True)
        policy_bytes = (Path(__file__).resolve().parents[1] /
                        'config/option_lifecycle_capture_v4.json').read_bytes()
        (self.release / 'config/option_lifecycle_capture_v4.json').write_bytes(policy_bytes)
        self.root = base / 'bybit_btc_option_lifecycle_v4'
        self.name = '20260922T035334.633790Z.json'
        for kind in ('reports', 'raw', 'features'):
            (self.root / kind / 'BTC').mkdir(parents=True)
        self.report_path = self.root / 'reports/BTC' / self.name

    def fixture(self, latencies):
        rows = []
        for index, latency in enumerate(latencies):
            start = 1790049214633 + index * 600000
            rows.append({'timestamp_epoch_ms': start, 'poll_started_epoch_ms': start,
                         'snapshot_completed_epoch_ms': start + latency,
                         'not_for_output': 'PRIVATE_SENTINEL'})
        self.raw = self.root / 'raw/BTC' / (self.name[:-5] + '.jsonl.xz')
        self.feature = self.root / 'features/BTC' / (self.name[:-5] + '.csv')
        self.raw.write_bytes(lzma.compress(('\n'.join(json.dumps(row) for row in rows) + '\n').encode()))
        self.feature.write_bytes(b'PRIVATE_SENTINEL\n')
        self.report = {
            'schema_version': 'bybit_btc_option_lifecycle_capture_v4',
            'policy_canonical_sha256': diagnostic.POLICY_SHA, 'status': 'PASS',
            'coverage': {'successful_poll_count': len(rows)},
            'quality': {'maximum_poll_latency_ms': max(latencies)},
            'raw': {'path': self.raw.relative_to(self.root).as_posix(),
                    'sha256': diagnostic.digest(self.raw.read_bytes()), 'snapshot_count': len(rows)},
            'features': {'path': self.feature.relative_to(self.root).as_posix(),
                         'sha256': diagnostic.digest(self.feature.read_bytes()), 'row_count': len(rows)},
        }
        self.seal_report()

    def seal_report(self):
        self.report_path.write_text(json.dumps(self.report))
        self.report_sha = diagnostic.digest(self.report_path.read_bytes())

    def run_diagnosis(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = diagnostic.diagnose(self.root, self.release, self.name, self.report_sha, self.release_sha)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
        self.assertFalse(result['archive_acceptance_evaluated'])
        return result

    def test_confirm_over_limit_without_promoting_or_writing(self):
        self.fixture([120000, 120001, 460494])
        result = self.run_diagnosis()
        self.assertEqual(result['maximum_poll_latency_ms'], 460494)
        self.assertEqual(result['over_limit_snapshot_count'], 2)
        self.assertEqual([r['line'] for r in result['over_limit_examples']], [2, 3])
        self.assertTrue(result['artifact_checksums_match'])
        self.assertTrue(result['metadata_max_latency_matches_raw'])

    def test_boundary_is_not_archive_pass(self):
        self.fixture([120000])
        self.assertEqual(self.run_diagnosis()['timing_diagnosis'], 'NO_TIMING_VIOLATION_FOUND')

    def test_metadata_disagreement_remains_visible(self):
        self.fixture([460494])
        self.report['quality']['maximum_poll_latency_ms'] = 1000
        self.report['coverage']['successful_poll_count'] = 2
        self.seal_report()
        result = self.run_diagnosis()
        self.assertFalse(result['metadata_max_latency_matches_raw'])
        self.assertFalse(result['report_counts_match_raw'])

    def test_report_hash_mismatch(self):
        self.fixture([460494])
        self.report_sha = '0' * 64
        with self.assertRaisesRegex(diagnostic.DiagnosticError, 'PINNED_REPORT_CHANGED'):
            self.run_diagnosis()

    def test_artifact_checksum_mismatch(self):
        self.fixture([460494])
        self.raw.write_bytes(b'changed')
        with self.assertRaisesRegex(diagnostic.DiagnosticError, 'ARTIFACT_CHECKSUM_MISMATCH'):
            self.run_diagnosis()

    def test_report_path_injection(self):
        self.fixture([460494])
        self.name = '../../credentials.json'
        with self.assertRaisesRegex(diagnostic.DiagnosticError, 'REPORT_NAME_INVALID'):
            self.run_diagnosis()

    def test_artifact_path_injection(self):
        self.fixture([460494])
        self.report['raw']['path'] = '/etc/passwd'
        self.seal_report()
        with self.assertRaisesRegex(diagnostic.DiagnosticError, 'ARTIFACT_PATH_MISMATCH'):
            self.run_diagnosis()

    def test_symlink_input_rejected(self):
        self.fixture([460494])
        original = self.raw.with_suffix('.original')
        self.raw.rename(original)
        self.raw.symlink_to(original)
        with self.assertRaisesRegex(diagnostic.DiagnosticError, 'ARTIFACT_PATH_UNSAFE'):
            self.run_diagnosis()

    def test_read_budget(self):
        self.fixture([460494])
        with mock.patch.object(diagnostic, 'MAX_FILE', 1):
            with self.assertRaisesRegex(diagnostic.DiagnosticError, 'FILE_BYTE_LIMIT'):
                self.run_diagnosis()

    def test_decode_budget(self):
        self.fixture([460494])
        with mock.patch.object(diagnostic, 'MAX_DECODED', 1):
            with self.assertRaisesRegex(diagnostic.DiagnosticError, 'DECOMPRESSION_BUDGET_EXCEEDED'):
                self.run_diagnosis()

    def test_wrong_release(self):
        self.fixture([460494])
        self.release_sha = 'b' * 40
        with self.assertRaisesRegex(diagnostic.DiagnosticError, 'DEPLOYED_RELEASE_MISMATCH'):
            self.run_diagnosis()

    def test_errors_do_not_echo_external_messages(self):
        args = ['diagnostic', '--report', self.name, '--report-sha256', '0' * 64,
                '--expected-release-sha', self.release_sha]
        output = io.StringIO()
        with mock.patch('sys.argv', args), contextlib.redirect_stdout(output):
            with mock.patch.object(diagnostic, 'diagnose', side_effect=OSError('PRIVATE_SENTINEL')):
                self.assertEqual(diagnostic.main(), 2)
        self.assertNotIn('PRIVATE_SENTINEL', output.getvalue())
        self.assertFalse(json.loads(output.getvalue())['diagnosis_completed'])


if __name__ == '__main__':
    unittest.main()
