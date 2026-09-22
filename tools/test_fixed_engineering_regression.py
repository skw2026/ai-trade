#!/usr/bin/env python3
"""Fixed synthetic timing fixtures: engineering rejects bad data, never qualifies it."""
import hashlib
import json
import lzma
from pathlib import Path
import tempfile
import unittest

import audit_option_lifecycle_v4 as audit
import capture_bybit_option_lifecycle_v4 as capture

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'config/option_lifecycle_capture_v4.json'
MANIFEST = ROOT / 'config/option_lifecycle_capture_manifest_v4.json'


class FixedEngineeringRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / capture.CAPTURE_ROOT_NAME
        for kind in ('reports', 'features', 'raw'):
            (self.root / kind / 'BTC').mkdir(parents=True)
        self.policy, self.manifest = capture.load_contract(POLICY, MANIFEST)
        self.start = int(self.manifest['observation_start_epoch_ms']) + 1000

    def segment(self, latency, index=0):
        name = f'20260922T0353{index:02d}.633790Z'
        raw = self.root / 'raw/BTC' / (name + '.jsonl.xz')
        feature = self.root / 'features/BTC' / (name + '.csv')
        report = self.root / 'reports/BTC' / (name + '.json')
        start = self.start + index * 600000
        snapshot = {
            'schema_version': capture.SNAPSHOT_SCHEMA_VERSION,
            'experiment_id': self.policy['experiment_id'],
            'policy_canonical_sha256': capture.canonical_sha256(self.policy),
            'manifest_canonical_sha256': capture.canonical_sha256(self.manifest),
            'timestamp_epoch_ms': start, 'poll_started_epoch_ms': start,
            'snapshot_completed_epoch_ms': start + latency,
            'active_lifecycle': None, 'tracked_options': [], 'delivery_prices': [],
        }
        raw.write_bytes(lzma.compress((json.dumps(snapshot) + '\n').encode()))
        rows = [{'timestamp_epoch_ms': start, 'snapshot_completed_epoch_ms': start + latency,
                 'poll_latency_ms': latency, 'tracked_observed_count': 0,
                 'paired_delivery_evidence_count': 0}]
        capture._write_feature_csv(feature, rows)
        state = capture.initial_state(policy=self.policy, manifest=self.manifest)
        payload = capture.build_report(root=self.root, raw_path=raw, feature_path=feature,
            features=rows, started=start, completed=start, termination_reason='duration_complete',
            policy=self.policy, manifest=self.manifest,
            state_before_sha256=capture.canonical_sha256(state), state_after=state)
        report.write_text(json.dumps(payload))
        return report, raw, snapshot

    def test_exact_boundary_and_one_ms_over(self):
        _, _, accepted = self.segment(120000)
        self.assertEqual(audit._validate_snapshot(accepted, policy=self.policy, manifest=self.manifest), self.start)
        _, _, rejected = self.segment(120001, 1)
        with self.assertRaisesRegex(ValueError, 'poll latency exceeds contract'):
            audit._validate_snapshot(rejected, policy=self.policy, manifest=self.manifest)

    def test_real_incident_latency_is_fixed_negative_not_pass(self):
        report_path, _, _ = self.segment(460494)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = audit.audit(root=self.root, policy_path=POLICY, manifest_path=MANIFEST,
                             executed_release_sha='a'*40, generated_at_epoch_ms=self.start + 600000)
        self.assertEqual(result['decision'], 'INVALID_OPTION_LIFECYCLE_ARCHIVE')
        integrity = result['archive_integrity']
        self.assertEqual((integrity['valid_segment_count'], integrity['invalid_segment_count']), (0, 1))
        self.assertEqual(integrity['invalid_segment_reason_counts'], {'POLL_LATENCY_EXCEEDS_CONTRACT': 1})
        detail = integrity['invalid_segments'][0]
        self.assertEqual(detail['report'], report_path.name)
        self.assertEqual(detail['report_sha256'], hashlib.sha256(report_path.read_bytes()).hexdigest())
        self.assertEqual(detail['snapshot_line'], 1)
        self.assertTrue(detail['artifact_checksums_verified'])
        self.assertFalse(result['demo_activation_authorized'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_more_normal_samples_do_not_cure_bad_segment(self):
        self.segment(460494)
        self.segment(1000, 1)
        result = audit.replay_capture_root(self.root, policy=self.policy, manifest=self.manifest)
        self.assertEqual((result['valid_segment_count'], result['invalid_segment_count']), (1, 1))
        self.assertEqual(result['eligible_snapshot_count'], 1)

    def test_checksum_failure_is_not_mislabeled_timing(self):
        _, raw, _ = self.segment(460494)
        raw.write_bytes(b'corrupt')
        result = audit.replay_capture_root(self.root, policy=self.policy, manifest=self.manifest)
        detail = result['invalid_segments'][0]
        self.assertEqual(detail['reason_code'], 'ARTIFACT_CHECKSUM_MISMATCH')
        self.assertFalse(detail['artifact_checksums_verified'])
        self.assertIsNone(detail['snapshot_line'])

    def test_unexpected_exception_text_and_filename_are_not_published(self):
        path = self.root / 'reports/BTC/PRIVATE_SENTINEL.json'
        path.write_text('{}')
        detail = audit._segment_diagnostic(path, ValueError('PRIVATE_SENTINEL'), 'report', 0, False)
        self.assertIsNone(detail['report'])
        self.assertEqual(detail['reason_code'], 'SEGMENT_VALIDATION_FAILURE')
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(detail))

    def test_detail_limit_does_not_hide_total_failures(self):
        for index in range(21):
            self.segment(460494, index)
        result = audit.replay_capture_root(self.root, policy=self.policy, manifest=self.manifest)
        self.assertEqual(result['invalid_segment_count'], 21)
        self.assertEqual(len(result['invalid_segments']), 20)
        self.assertTrue(result['invalid_segment_details_truncated'])
        self.assertEqual(result['invalid_segment_reason_counts'], {'POLL_LATENCY_EXCEEDS_CONTRACT': 21})


if __name__ == '__main__':
    unittest.main()
