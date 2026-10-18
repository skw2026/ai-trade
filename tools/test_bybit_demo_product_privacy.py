"""Execute the actual workflow disclosure block with synthetic private facts."""
import json
import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / '.github/workflows/bybit-demo-product-facts.yml'


class PrivacyTests(unittest.TestCase):
    def block(self):
        source = WORKFLOW.read_text()
        marker = 'FACTS_COLLECTOR_SHA="${collector_sha}" FACTS_BASE_SHA="${base_sha}" python3 - <<\'PY\'\n'
        raw = source.split(marker, 1)[1].split('\n          PY\n', 1)[0]
        return textwrap.dedent(raw)

    def run_block(self, report):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / '.artifacts'; path.mkdir()
            (path / 'demo-product-facts.json').write_text(json.dumps(report))
            env = dict(os.environ, FACTS_COLLECTOR_SHA='a' * 64, FACTS_BASE_SHA='b' * 64)
            return subprocess.run(['python3', '-c', self.block()], cwd=temp, env=env,
                                  capture_output=True, text=True, timeout=10)

    def report(self):
        return {'schema_version': 'bybit_demo_product_facts_v1', 'status': 'READONLY_FACTS_OBSERVED',
                'response_pages': 3, 'collector_sha256': 'a' * 64, 'base_sha256': 'b' * 64,
                'manifest_sha256': 'c' * 64, 'order_submission': False, 'account_mutation': False,
                'balances_published': False, 'marginMode': 'PRIVATE_MODE_SENTINEL',
                'unifiedMarginStatus': 987654321, 'coins': {'BTC': {'fields': {'walletBalance': 'PRIVATE_ASSET_SIGN'}}},
                'btc_collateral': {'collateralSwitch': 'PRIVATE_COLLATERAL_SENTINEL'},
                'uid': 'PRIVATE_UID_SENTINEL', 'unexpected': 'PRIVATE_EXTRA_SENTINEL'}

    def test_success_only_engineering_allowlist(self):
        run = self.run_block(self.report())
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(run.stdout.startswith('::notice title=Bybit Demo product receipt::'))
        public = json.loads(run.stdout.split('::', 2)[2])
        self.assertEqual(set(public), {'schema_version', 'status', 'response_pages', 'collector_sha256',
                                     'base_sha256', 'manifest_sha256', 'order_submission',
                                     'account_mutation', 'balances_published'})
        self.assertNotIn('PRIVATE_', run.stdout + run.stderr)
        self.assertNotIn('987654321', run.stdout + run.stderr)

    def test_failure_does_not_publish_error_or_account_facts(self):
        r = self.report(); r.update(status='INCOMPLETE', reason='PRIVATE_ERROR_SENTINEL')
        run = self.run_block(r)
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn('PRIVATE_', run.stdout + run.stderr)

    def test_hash_mismatch_stops_before_annotation(self):
        r = self.report(); r['collector_sha256'] = 'd' * 64
        run = self.run_block(r)
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn('::notice', run.stdout)
        self.assertNotIn('PRIVATE_', run.stdout + run.stderr)


if __name__ == '__main__':
    unittest.main()
