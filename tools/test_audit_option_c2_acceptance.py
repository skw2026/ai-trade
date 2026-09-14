#!/usr/bin/env python3
import copy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import audit_option_c2_acceptance as acceptance
import probe_bybit_c2_depth_source as depth_probe
from test_audit_option_c2_integration import fixture, CALL


class AcceptanceTest(unittest.TestCase):
    def test_large_notice_payload_reassembles_with_hash_and_bounded_count(self):
        payload = {'escaped': ('\\"\n测试' * 1000)}
        chunks = acceptance.annotation_chunks(payload)
        raw = ''.join(c['text'] for c in chunks)
        self.assertEqual(json.loads(raw), payload)
        self.assertLessEqual(len(chunks), 10)
        self.assertTrue(all(len(json.dumps(c, separators=(',', ':')).encode()) < 3500 for c in chunks))
        self.assertEqual(acceptance.wire.digest(raw.encode()), chunks[0]['sha256'])

    def test_unbounded_notice_count_fails_before_publishing_partial_evidence(self):
        with self.assertRaisesRegex(ValueError, 'COUNT_EXCEEDS'):
            acceptance.annotation_chunks({'too_large': 'x' * 40000})

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

    def test_cash_replay_does_not_hide_risk_rejection(self):
        data, market = fixture()
        data['illustrative_limits']['drawdown_exit'] = '0.0001'
        report = acceptance.audit(data, market, 'synthetic-market')
        self.assertTrue(report['research_replay_accepted'])
        self.assertFalse(report['observed_ledger_risk_passed'])
        self.assertEqual(report['source_ledger_status'], 'RISK_REJECTED_OFFLINE')
        check = next(c for c in report['checks'] if c['id'] == 'frozen_ledger_risk')
        self.assertEqual(check['state'], 'RISK_REJECTED')
        self.assertIn('NAV_OR_DRAWDOWN_EXIT', check['observation']['risk_breaches'])

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


class DepthProbeTest(unittest.TestCase):
    def test_public_metadata_is_not_downloaded_and_signed_query_not_published(self):
        payload = acceptance.wire.encode({'ret_code': 0, 'result': {'list': [
            {'filename': 'BTCUSDT.zip', 'url': 'https://example.com/depth.zip?secret=test', 'size': 100}]}})
        class Reply:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, size): return payload
        with tempfile.TemporaryDirectory() as temp:
            opener = unittest.mock.Mock()
            opener.open.return_value = Reply()
            report = depth_probe.probe(pathlib.Path(temp) / 'capture', opener)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, depth_probe.URL)
        self.assertEqual(req.method, 'GET')
        self.assertEqual(set(dict(req.header_items())), {'User-agent'})
        self.assertNotIn('secret=test', json.dumps(report))
        self.assertTrue(report['files'][0]['has_query'])
        self.assertFalse(report['depth_downloaded'])
        self.assertFalse(report['historical_liquidity_qualified'])

    def test_http_denial_stays_unavailable_not_empty_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / 'capture'
            opener = unittest.mock.Mock()
            opener.open.side_effect = HTTPError(depth_probe.URL, 403, 'Forbidden', {}, None)
            report = depth_probe.probe(root, opener)
            self.assertEqual(report['http_status'], 403)
            self.assertEqual(report['status'], 'PUBLIC_DEPTH_DIRECTORY_HTTP_UNAVAILABLE')
            self.assertNotIn('files', report)
            self.assertFalse((root / 'response.raw').exists())
            with self.assertRaises(FileExistsError): depth_probe.probe(root, opener)


if __name__ == '__main__':
    unittest.main()
