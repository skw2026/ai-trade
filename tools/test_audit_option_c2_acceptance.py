#!/usr/bin/env python3
import copy
import unittest
from unittest.mock import patch
import audit_option_c2_acceptance as acceptance
from test_audit_option_c2_integration import fixture, CALL


class AcceptanceTest(unittest.TestCase):
    def test_research_acceptance_is_separate_from_historical_verifier_capability(self):
        data, market = fixture()
        saved = copy.deepcopy(data)
        report = acceptance.audit(data, market, 'synthetic-market')
        self.assertTrue(report['research_replay_accepted'])
        self.assertTrue(report['observed_exit_l1_passed'])
        self.assertFalse(report['historical_verification_supported'])
        self.assertEqual(len(report['unsupported_verifiers']), 4)
        self.assertEqual(data, saved)
        self.assertFalse(any(report['authorities'].values()))

    def test_exit_constraints_are_not_hidden_by_successful_replay(self):
        data, market = fixture()
        data['events'][3]['valuation'][CALL]['ask_size'] = '0.001'
        report = acceptance.audit(data, market, 'synthetic-market')
        self.assertTrue(report['research_replay_accepted'])
        self.assertFalse(report['observed_exit_l1_passed'])
        self.assertEqual(report['exit_evidence']['unqualified_checkpoint_count'], 1)

    def test_reference_case_failure_changes_acceptance(self):
        data, market = fixture()
        original = acceptance.references.audit
        def wrong():
            result = original()
            result['passed_cases'] = 0
            return result
        with patch.object(acceptance.references, 'audit', side_effect=wrong):
            report = acceptance.audit(data, market, 'synthetic-market')
        self.assertFalse(report['research_replay_accepted'])
        self.assertEqual(report['status'], 'RESEARCH_REPLAY_REJECTED')

    def test_missing_funding_and_price_do_not_become_pass(self):
        for stream in ('funding', 'index'):
            data, market = fixture()
            market[stream].clear()
            with self.assertRaises(ValueError): acceptance.audit(data, market, 'synthetic-market')

    def test_caller_cannot_claim_historical_qualification(self):
        data, market = fixture()
        data['historical_qualified'] = True
        with self.assertRaisesRegex(ValueError, 'INVALID_LEDGER'):
            acceptance.audit(data, market, 'synthetic-market')

    def test_no_obligation_and_bounded_amount_have_distinct_states(self):
        data, market = fixture()
        first = acceptance.audit(data, market, 'synthetic-market')
        data['events'][4]['ts_ms'] -= 1
        data['events'][4]['execution_quote']['ts_ms'] -= 1
        for row in data['events'][4]['valuation'].values():
            row['ts_ms'] -= 1
        second = acceptance.audit(data, market, 'synthetic-market')
        state = lambda report: next(c['state'] for c in report['checks'] if c['id'] == 'funding_amount')
        self.assertEqual(state(first), 'BOUNDED_RESEARCH_ONLY')
        self.assertEqual(state(second), 'NO_OBLIGATION_ON_RETURNED_BOUNDARIES')


if __name__ == '__main__':
    unittest.main()
