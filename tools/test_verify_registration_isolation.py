#!/usr/bin/env python3
"""Fast synthetic checker tests; actual CatBoost deserialization is mandatory in CD."""
import json
from pathlib import Path
import tempfile
import unittest

import verify_registration_isolation as audit


class RegistrationIsolationTest(unittest.TestCase):
    def test_canonical_alias_is_allowed_but_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / 'registry'
            registry.mkdir()
            (registry / 'model').write_bytes(b'TEST_ONLY')
            alias = root / 'alias'
            alias.symlink_to(registry, target_is_directory=True)
            self.assertEqual(audit.confined_artifact(alias / 'model', alias), (registry / 'model').resolve())
            (root / 'outside').write_bytes(b'TEST_ONLY_OUTSIDE')
            (registry / 'escape').symlink_to(root / 'outside')
            with self.assertRaisesRegex(AssertionError, 'escaped isolated root'):
                audit.confined_artifact(registry / 'escape', registry)

    def fixture(self, root):
        source = root / 'learning'
        source.mkdir()
        for name in audit.FILES:
            (source / name).write_text('TEST_ONLY_UNIT_FIXTURE_' + name)
        metadata = {'test_only': True, 'production_promotion_authority': False,
                    'governance': {'pass': False}, 'model_version': 'TEST_ONLY_UNIT',
                    'model_sha256': audit.sha(source / 'learned.cbm'),
                    'data': {'training_csv_sha256': audit.sha(source / 'development.csv'),
                             'source_venue': 'synthetic', 'source_category': 'test_only',
                             'training_symbol': 'SYNTHBTC', 'bar_interval_ms': 300000}}
        (source / 'learned.json').write_text(json.dumps(metadata))
        receipt = {'status': 'PASS', 'scope': 'TEST_ONLY_OFFLINE_COMPONENT_INTEGRATION',
                   'production_promotion_authority': False, 'market_economic_evidence': False,
                   'demo_trading_authority': False, 'full_mechanism_valid': False,
                   'training_artifact_sha256': {n: audit.sha(source / n) for n in audit.FILES}}
        audit.write(source / 'result.json', receipt)
        return source, metadata, receipt

    def test_hash_tamper_rejected_before_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            source, _, _ = self.fixture(Path(directory))
            audit.inputs(source / 'result.json')
            (source / 'learned.cbm').write_bytes(b'changed')
            with self.assertRaisesRegex(AssertionError, 'artifact hash mismatch'):
                audit.inputs(source / 'result.json')

    def test_wrong_model_identity_and_source_are_not_accepted(self):
        for field in ('model', 'source', 'authority'):
            with tempfile.TemporaryDirectory() as directory:
                source, metadata, receipt = self.fixture(Path(directory))
                if field == 'model':
                    metadata['model_sha256'] = '0' * 64
                elif field == 'source':
                    metadata['data']['source_venue'] = 'bybit'
                else:
                    metadata['production_promotion_authority'] = True
                (source / 'learned.json').write_text(json.dumps(metadata))
                receipt['training_artifact_sha256']['learned.json'] = audit.sha(source / 'learned.json')
                (source / 'result.json').write_text(json.dumps(receipt))
                with self.assertRaises(AssertionError):
                    audit.inputs(source / 'result.json')

    def test_rejected_registration_preserves_previous_and_absent_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, _ = self.fixture(root)
            output = root / 'output'
            output.mkdir()
            before = audit.file_set(source)
            for previous in (False, True):
                result = audit.registration(source, output, previous)
                self.assertEqual(result['active_file_count'], 4 if previous else 0)
                self.assertEqual(result['decision'], 'REJECTED')
            self.assertEqual(audit.file_set(source), before)

    def test_zero_episode_and_fault_decisions_cannot_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, metadata, _ = self.fixture(root)
            output = root / 'output'
            output.mkdir()
            before = audit.file_set(source)
            for fault in ('zero_episodes', 'runtime_identity', 'staged_identity', 'artifact_corruption', 'deadline'):
                result = audit.transaction(source, output, metadata, fault, 'd' * 64)
                self.assertEqual(result['decision'], 'pending' if fault == 'zero_episodes' else 'rollback')
                if fault == 'zero_episodes':
                    safety = result['safety_interlock']
                    self.assertEqual(safety['status'], 'PASS')
                    self.assertFalse(safety['production_promotion_authority'])
                    self.assertEqual(len(safety['cases']), 7)
                    for name, case in safety['cases'].items():
                        self.assertEqual(case['decision'], 'commit' if name == 'clear_control' else 'rollback')
                        self.assertEqual(case['repeat_decision'], case['decision'])
                        self.assertEqual(case['synthetic_complete_episodes'], 30)
                self.assertFalse(result['rollback_service_executed'])
            self.assertEqual(audit.file_set(source), before)

    def test_existing_output_or_input_descendant_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, _ = self.fixture(root)
            before = audit.file_set(source)
            for output in (source, source / 'new', root):
                with self.assertRaisesRegex(AssertionError, 'new isolated sibling'):
                    audit.verify(source / 'result.json', output)
            self.assertEqual(audit.file_set(source), before)

    def test_cd_requires_isolation_after_learning_and_before_deploy(self):
        workflow = (audit.ROOT / '.github/workflows/cd.yml').read_text()
        learning = workflow.index('name: Verify Offline Learning Loop')
        isolation = workflow.index('--label registration-isolation')
        self.assertLess(learning, isolation)
        self.assertLess(isolation, workflow.index('name: Compute Deploy Gate'))
        block = workflow[isolation:workflow.index('name: Upload Offline Learning Evidence')]
        for value in ('--network none', '--read-only', '--cap-drop ALL', '--user "$(id -u):$(id -g)"',
                      '/app/tools/verify_registration_isolation.py', '--learning-result /evidence/acceptance/result.json'):
            self.assertIn(value, block)
        self.assertNotIn('continue-on-error', block)


if __name__ == '__main__':
    unittest.main()
