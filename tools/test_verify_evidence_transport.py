#!/usr/bin/env python3
"""Synthetic transfer positives and hard negatives; no exchange/account access."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import verify_evidence_transport as transfer

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def fixture(root):
    source = root / 'source'
    learning, registration = source / 'acceptance', source / 'registration'
    learning.mkdir(parents=True)
    registration.mkdir()
    (learning / 'learned.cbm').write_bytes(b'TEST_ONLY_MODEL_BYTES_NOT_A_REAL_MODEL')
    write(learning / 'learned.json', {
        'test_only': True, 'production_promotion_authority': False, 'governance': {'pass': False},
        'data': {'source_venue': 'synthetic', 'source_category': 'test_only'}})
    inputs = {p.name: transfer.digest(p) for p in learning.iterdir()}
    common = {'status': 'PASS', 'verification_git_sha': SHA,
              **{key: False for key in transfer.AUTHORITY}}
    write(learning / 'result.json', {**common, 'scope': 'TEST_ONLY_OFFLINE_COMPONENT_INTEGRATION',
                                   'artifact_sha256': inputs, 'training_artifact_sha256': inputs})
    artifacts = {}
    for name in sorted(transfer.LOCKS):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'')
        artifacts[path.relative_to(registration).as_posix()] = transfer.digest(path)
    write(registration / 'registration-result.json', {
        **common, 'scope': 'TEST_ONLY_REGISTRATION_ISOLATION', 'production_registry_written': False,
        'original_inputs_unchanged': True, 'learning_verification_git_sha': SHA,
        'learning_report_sha256': transfer.digest(learning / 'result.json'),
        'input_sha256': inputs, 'artifact_sha256': artifacts})
    return source, root / 'trusted/manifest.json'


class EvidenceTransportTest(unittest.TestCase):
    def test_complete_zip_roundtrip_preserves_hidden_files_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest = fixture(root)
            before = transfer.inventory(source)
            transfer.prepare(source, manifest, SHA)
            with zipfile.ZipFile(root / 'evidence.zip', 'w') as archive:
                for name in before:
                    archive.write(source / name, name)
            with zipfile.ZipFile(root / 'evidence.zip') as archive:
                archive.extractall(root / 'download')  # Only this locally generated fixture ZIP.
            result = transfer.verify(root / 'download', manifest, SHA, 12, 34, 1)
            self.assertEqual(result['hidden_lock_count'], 2)
            self.assertEqual(result['file_count'], len(before))
            self.assertEqual((result['artifact_id'], result['run_id'], result['run_attempt']), (12, 34, 1))
            self.assertTrue(all(result[key] is False for key in transfer.AUTHORITY))
            self.assertEqual(transfer.inventory(source), before)

    def test_either_missing_hidden_lock_is_rejected(self):
        for missing in sorted(transfer.LOCKS):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                source, manifest = fixture(Path(directory))
                transfer.prepare(source, manifest, SHA)
                (source / missing).unlink()
                with self.assertRaisesRegex(ValueError, 'TRANSFER_FILE_SET_MISMATCH'):
                    transfer.verify(source, manifest, SHA)

    def test_changed_file_or_extra_file_is_rejected(self):
        for change in ('acceptance/learned.cbm', 'acceptance/extra.json', 'acceptance/.env'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                source, manifest = fixture(Path(directory))
                transfer.prepare(source, manifest, SHA)
                (source / change).write_bytes(b'TEST_ONLY_CHANGED')
                with self.assertRaises(ValueError):
                    transfer.verify(source, manifest, SHA)

    def test_extra_files_cannot_be_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            source, manifest = fixture(Path(directory))
            (source / 'acceptance/extra.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'INCOMPLETE_OR_CHANGED_FILE_SET'):
                transfer.prepare(source, manifest, SHA)
            self.assertFalse(manifest.exists())

    def test_symlink_file_and_directory_are_rejected(self):
        for target_is_dir in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, manifest = fixture(root)
                external = root / 'outside'
                if target_is_dir:
                    external.mkdir()
                else:
                    external.write_bytes(b'TEST_ONLY_OUTSIDE')
                (source / 'registration/escape').symlink_to(external, target_is_directory=target_is_dir)
                with self.assertRaisesRegex(ValueError, 'SYMLINK_FORBIDDEN'):
                    transfer.prepare(source, manifest, SHA)

    def test_unsafe_manifest_paths_are_rejected(self):
        for relative in ('../escape.json', '/absolute.json', 'x//y.json', './x.json',
                         'x\\y.json', 'x:y.json', '.env', '../acceptance/result.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                source, manifest = fixture(Path(directory))
                path = source / 'registration/registration-result.json'
                report = transfer.read(path)
                report['artifact_sha256'][relative] = '0' * 64
                write(path, report)
                with self.assertRaises(ValueError):
                    transfer.prepare(source, manifest, SHA)

    def test_wrong_sha_authority_or_cross_report_link_is_rejected(self):
        for field, value in (('verification_git_sha', 'b' * 40),
                             ('production_promotion_authority', True),
                             ('learning_report_sha256', '0' * 64),
                             ('production_registry_written', True),
                             ('input_sha256', {})):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                source, manifest = fixture(Path(directory))
                path = source / 'registration/registration-result.json'
                report = transfer.read(path)
                report[field] = value
                write(path, report)
                with self.assertRaises(ValueError):
                    transfer.prepare(source, manifest, SHA)

    def test_deleting_locks_from_both_report_and_disk_cannot_weaken_check(self):
        with tempfile.TemporaryDirectory() as directory:
            source, manifest = fixture(Path(directory))
            path = source / 'registration/registration-result.json'
            report = transfer.read(path)
            for name in transfer.LOCKS:
                (source / name).unlink()
            report['artifact_sha256'] = {'registration-result.json': '0' * 64}
            write(path, report)
            with self.assertRaises(ValueError):
                transfer.prepare(source, manifest, SHA)

    def test_manifest_inside_upload_and_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source, manifest = fixture(Path(directory))
            with self.assertRaisesRegex(ValueError, 'MANIFEST_INSIDE_UPLOAD'):
                transfer.prepare(source, source / 'manifest.json', SHA)
            transfer.prepare(source, manifest, SHA)
            original = manifest.read_bytes()
            with self.assertRaisesRegex(ValueError, 'MANIFEST_ALREADY_EXISTS'):
                transfer.prepare(source, manifest, SHA)
            self.assertEqual(manifest.read_bytes(), original)

    def test_manifest_tamper_or_wrong_expected_sha_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source, manifest = fixture(Path(directory))
            transfer.prepare(source, manifest, SHA)
            with self.assertRaisesRegex(ValueError, 'MANIFEST_IDENTITY'):
                transfer.verify(source, manifest, 'b' * 40)
            data = transfer.read(manifest)
            data['files']['acceptance/learned.cbm'] = '0' * 64
            write(manifest, data)
            with self.assertRaisesRegex(ValueError, 'TRANSFER_FILE_SET_MISMATCH'):
                transfer.verify(source, manifest, SHA)

    def test_actual_cli_prepare_verify_and_failure_propagation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest = fixture(root)
            base = [sys.executable, str(ROOT / 'tools/verify_evidence_transport.py')]
            args = ['--root', str(source), '--manifest', str(manifest), '--expected-sha', SHA]
            prepared = subprocess.run([*base, 'prepare', *args], capture_output=True, text=True)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            receipt = root / 'receipt.json'
            verified = subprocess.run([*base, 'verify', *args, '--output', str(receipt)],
                                      capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(transfer.read(receipt)['status'], 'PASS')
            (source / sorted(transfer.LOCKS)[0]).unlink()
            rejected = subprocess.run([*base, 'verify', *args], capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 1)
            self.assertIn('TRANSFER_FILE_SET_MISMATCH', rejected.stderr)

    def test_cd_transfer_is_mandatory_and_precedes_deploy(self):
        workflow = (ROOT / '.github/workflows/cd.yml').read_text()
        steps = ('Verify Offline Learning Loop', 'Prepare Offline Evidence Transfer',
                 'Upload Offline Learning Evidence', 'Download Offline Evidence Roundtrip',
                 'Verify Offline Evidence Roundtrip', 'Upload Offline Evidence Transfer Receipt',
                 'Compute Deploy Gate')
        positions = [workflow.index('name: ' + name) for name in steps]
        self.assertEqual(positions, sorted(positions))
        block = workflow[positions[1]:positions[-1]]
        self.assertNotIn('continue-on-error', block)
        self.assertNotIn('if: always()', block)
        self.assertIn('include-hidden-files: true', block)
        self.assertIn('path: .artifacts/offline-learning-cd/', block)
        self.assertIn('artifact-ids: ${{ steps.offline_evidence.outputs.artifact-id }}', block)
        self.assertIn('merge-multiple: true', block)
        self.assertEqual(block.count('tools/validation_gate.py run'), 2)
        self.assertEqual(block.count('--expected-sha "${GITHUB_SHA}"'), 2)
        self.assertIn('needs: build-test-push', workflow)
        self.assertIn('grep -q "evidence_transport_test"', (ROOT / '.github/workflows/ci.yml').read_text())


if __name__ == '__main__':
    unittest.main()
