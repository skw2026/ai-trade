#!/usr/bin/env python3
import copy
import pathlib
import subprocess
import sys
import tempfile
import unittest

import audit_option_exit_evidence as exit_evidence
import audit_bybit_readonly_evidence as wire
from test_audit_option_subaccount_ledger import fixture, CALL, PUT


class ExitEvidenceTest(unittest.TestCase):
    def test_complete_l1_is_not_full_history_qualification(self):
        report = exit_evidence.audit(fixture())
        self.assertEqual(report['status'], 'OBSERVED_L1_CHECKS_PASS')
        self.assertEqual(report['unqualified_checkpoint_count'], 0)
        self.assertFalse(report['historical_liquidity_qualified'])

    def test_zero_ask_preserved_and_neighbours_are_not_outage_duration(self):
        data = fixture()
        data['events'][3]['valuation'][CALL].update(ask='0', ask_size='0')
        saved = copy.deepcopy(data)
        report = exit_evidence.audit(data)
        r = report['findings'][0]
        self.assertEqual(r['reasons'], ['ZERO_EXIT_PRICE', 'ZERO_EXIT_SIZE'])
        self.assertEqual(r['exit_side'], 'ask')
        self.assertEqual(r['l1_capacity_fraction'], '0')
        self.assertEqual(r['neighbour_bracket_ms'], 2000)
        self.assertIsNone(r['continuous_outage_duration_ms'])
        self.assertFalse(report['actual_exit_failure_proven'])
        self.assertEqual(data, saved)

    def test_positive_l1_shortfall_is_not_missing_price_or_full_book_failure(self):
        data = fixture()
        data['events'][3]['valuation'][CALL]['ask_size'] = '0.005'
        report = exit_evidence.audit(data)
        r = report['findings'][0]
        self.assertEqual(r['reasons'], ['L1_SIZE_BELOW_POSITION'])
        self.assertEqual(r['shortfall_btc'], '0.005')
        self.assertEqual(r['l1_capacity_fraction'], '0.5')
        self.assertFalse(report['full_orderbook_capacity_known'])

    def test_expiry_sibling_delivery_is_separate_review_not_claimed_liquidation(self):
        data = fixture()
        data['events'][6]['valuation'][PUT].update(ask='0', ask_size='0')
        report = exit_evidence.audit(data)
        r = report['findings'][0]
        self.assertEqual(r['classification'], 'DELIVERY_ORDERING_REVIEW')
        self.assertTrue(r['same_timestamp_delivery_pending'])
        self.assertIsNone(r['next_full_l1_observation'])

    def test_repeated_quote_is_not_independent_observation(self):
        data = fixture()
        for seq in (3, 4):
            data['events'][seq]['valuation'][CALL].update(ask_size='0.001', ts_ms=data['events'][3]['ts_ms'])
        report = exit_evidence.audit(data)
        self.assertEqual(report['unqualified_checkpoint_count'], 2)
        self.assertEqual(report['distinct_bad_quote_observations'], 1)

    def test_invalid_source_is_rejected(self):
        data = fixture()
        data['events'][3]['execution_quote']['ask_size'] = '0'
        with self.assertRaisesRegex(ValueError, 'SOURCE_LEDGER_INVALID'):
            exit_evidence.audit(data)

    def test_cli_pins_source_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source, out = root / 'ledger.json', root / 'exit.json'
            source.write_bytes(wire.encode(fixture()))
            cmd = [sys.executable, exit_evidence.__file__, '--ledger', str(source),
                '--ledger-sha256', wire.digest(source.read_bytes()), '--output', str(out)]
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            saved = out.read_bytes()
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 2)
            self.assertEqual(out.read_bytes(), saved)
            cmd[cmd.index('--ledger-sha256') + 1] = '0' * 64
            self.assertIn(b'HASH_MISMATCH', subprocess.run(cmd, capture_output=True).stdout)


if __name__ == '__main__':
    unittest.main()
