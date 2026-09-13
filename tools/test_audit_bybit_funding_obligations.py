#!/usr/bin/env python3
"""Synthetic-only funding coverage and public transport tests."""
import copy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import audit_bybit_funding_obligations as audit
import audit_bybit_readonly_evidence as source
from test_audit_bybit_readonly_evidence import demo_fixture, FakeTransport, START, END, NOW

BOUNDARY = START + 30_000


def fixture(close_before=True):
    groups = demo_fixture()
    groups['transactions'] = groups['transactions'][:1]
    tx = groups['transactions'][0]
    tx.update(size='0.001', transactionTime=str(START + 10_000))
    ex = groups['executions'][0]
    ex['execTime'] = tx['transactionTime']
    stamp = START + (20_000 if close_before else 40_000)
    groups['transactions'].append({**tx, 'id': 'close-tx', 'tradeId': 'close-fill', 'orderId': 'close-order',
                                  'side': 'Sell', 'size': '0', 'transactionTime': str(stamp)})
    groups['executions'].append({**ex, 'execId': 'close-fill', 'orderId': 'close-order',
                                'side': 'Sell', 'execTime': str(stamp)})
    groups['orders'] = [{'symbol': 'BTCUSDT', 'orderId': ex['orderId'], 'positionIdx': 0}
                        for ex in groups['executions']]
    return groups


class CalendarFake:
    def get(self, request):
        p = request['params']
        rows = [{'symbol': p['symbol'], 'fundingRateTimestamp': str(BOUNDARY), 'fundingRate': '0.0001'}
                for _ in range(int(p['startTime'] <= BOUNDARY <= p['endTime']))]
        return source.encode({'retCode': 0, 'time': NOW, 'result': {'category': 'linear', 'list': rows}})


class FundingObligationsTest(unittest.TestCase):
    def evaluate(self, groups=None, when=BOUNDARY):
        return audit.audit(groups or fixture(), {'BTCUSDT': {when: '0.0001'}}, START, END)

    def test_flat_path_is_conditional_not_funding_sample(self):
        report = self.evaluate()
        self.assertEqual(report['status'], 'OBSERVED_BOUNDARIES_FLAT_CONDITIONAL')
        self.assertEqual(report['counts']['flat_boundaries'], 1)
        self.assertEqual(report['counts']['verified_position_paths'], 1)
        for key in ('no_funding_obligation_accountwide_proven', 'funding_fee_model_qualified',
                    'snapshot_used_as_window_endpoint', 'private_api_calls', 'promotion_authority'):
            self.assertFalse(report[key])

    def test_exposure_requires_record_even_if_snapshot_flat(self):
        report = self.evaluate(fixture(False))
        self.assertEqual(report['status'], 'OBSERVED_EXPOSURE_WITHOUT_SETTLEMENT_RECORD')
        self.assertEqual(report['counts']['exposed_boundaries'], 1)

    def test_five_second_inclusion_guard(self):
        for delta in (-5000, 0, 5000):
            report = self.evaluate(when=START + 20_000 + delta)
            self.assertEqual(report['counts']['ambiguous_boundaries'], 1)
        self.assertEqual(self.evaluate(when=START + 25_001)['counts']['flat_boundaries'], 1)

    def test_window_edges_are_ambiguous(self):
        for when in (START, START + 5000, END):
            self.assertEqual(self.evaluate(when=when)['counts']['ambiguous_boundaries'], 1)

    def test_opening_carry_is_inferred_not_forced_flat(self):
        groups = fixture()
        for tx in groups['transactions']:
            tx['size'] = str(source.number(tx['size']) + source.number('1'))
        report = self.evaluate(groups)
        self.assertEqual(report['counts']['inferred_opening_nonzero_symbols'], 1)
        self.assertEqual(report['counts']['inferred_ending_nonzero_symbols'], 1)
        self.assertEqual(report['counts']['exposed_boundaries'], 1)

    def test_short_position_signed_size(self):
        groups = fixture(False)
        for ex, tx in zip(groups['executions'], groups['transactions']):
            ex['side'] = tx['side'] = 'Sell' if ex['side'] == 'Buy' else 'Buy'
            tx['size'] = str(-source.number(tx['size']))
        self.assertEqual(self.evaluate(groups)['counts']['exposed_boundaries'], 1)

    def test_post_trade_chain_gap_cannot_be_flat(self):
        groups = fixture()
        groups['transactions'][1]['size'] = '0.001'
        report = self.evaluate(groups)
        self.assertIn('POST_TRADE_POSITION_CHAIN_BROKEN', report['gaps'])
        self.assertEqual(report['counts']['flat_boundaries'], 0)

    def test_size_missing_retains_gap(self):
        groups = fixture()
        del groups['transactions'][0]['size']
        self.assertIn('SIGNED_POST_TRADE_SIZE_MISSING', self.evaluate(groups)['gaps'])

    def test_same_timestamp_not_arbitrarily_ordered(self):
        groups = fixture()
        for ex, tx in zip(groups['executions'], groups['transactions']):
            ex['execTime'] = tx['transactionTime'] = str(START + 10_000)
        self.assertIn('SAME_TIMESTAMP_TRADE_ORDER_UNRESOLVED', self.evaluate(groups)['gaps'])

    def test_hedge_mode_and_missing_order_fail_closed(self):
        for value in (1, 2, None, False, '0'):
            groups = fixture()
            groups['orders'][0]['positionIdx'] = value
            self.assertEqual(self.evaluate(groups)['counts']['ambiguous_boundaries'], 1)
        groups['orders'] = []
        self.assertIn('ONE_WAY_ORDER_EVIDENCE_MISSING', self.evaluate(groups)['gaps'])

    def test_nontrade_position_event_not_silently_ignored(self):
        groups = fixture()
        groups['executions'].append({**groups['executions'][0], 'execType': 'BustTrade', 'execId': 'bust'})
        self.assertIn('NONTRADE_POSITION_EVENT_UNSUPPORTED', self.evaluate(groups)['gaps'])

    def test_transaction_time_mismatch(self):
        groups = fixture()
        groups['transactions'][0]['transactionTime'] = str(START + 10_001)
        self.assertIn('TRADE_TRANSACTION_TIME_MISMATCH', self.evaluate(groups)['gaps'])

    def test_unmatched_trade_stops_audit(self):
        groups = fixture()
        groups['transactions'].pop()
        with self.assertRaisesRegex(ValueError, 'TRADE_MATCH_COVERAGE'):
            self.evaluate(groups)

    def test_existing_funding_not_claimed_reconciled(self):
        groups = fixture()
        funding = demo_fixture()['transactions'][1]
        groups['transactions'].append(funding)
        self.assertEqual(self.evaluate(groups)['status'], 'FUNDING_RECORDS_REQUIRE_SEPARATE_RECONCILIATION')

    def test_no_private_fields_in_summary(self):
        rendered = json.dumps(self.evaluate())
        for secret in ('synthetic-fill', 'synthetic-order', 'BTCUSDT', '99999.123', '0.04', '0.001'):
            self.assertNotIn(secret, rendered)

    def test_calendar_plan_partitions_and_allowlist(self):
        plan = audit.calendar_plan(['BTCUSDT', 'ETHUSDT'], START, END)
        self.assertEqual(len(plan), 4)
        for symbols in ([], ['https://evil'], ['BTCUSDT', 'BTCUSDT'], [None]):
            with self.assertRaises((ValueError, TypeError)):
                audit.calendar_plan(symbols, START, END)

    def test_public_transport_get_no_credentials_or_redirect(self):
        transport = audit.PublicCalendarTransport()
        request = audit.calendar_plan(['ETHUSDT'], START, END)[0]
        raw = CalendarFake().get(request)
        class Reply:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, count): return raw
        with patch.object(transport.opener, 'open', return_value=Reply()) as call:
            transport.get(request)
        req = call.call_args.args[0]
        self.assertEqual(req.method, 'GET')
        self.assertTrue(req.full_url.startswith('https://api.bybit.com/v5/market/funding/history?'))
        self.assertEqual(set(dict(req.header_items())), {'User-agent'})
        for modified in ({**request, 'path': '/v5/order/create'},
                         {**request, 'params': {**request['params'], 'api_key': 'bad'}}):
            with self.assertRaises(ValueError):
                transport.get(modified)

    def test_public_archive_replay_tamper_and_partition_omission(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(source, 'now_ms', return_value=NOW):
            root = pathlib.Path(temp)
            demo_capture = source.collect(root / 'demo', 'demo', START, END, FakeTransport(fixture()))
            sha = source.digest((demo_capture / 'manifest.json').read_bytes())
            demo, groups = audit.load_demo(demo_capture, sha)
            cap = audit.collect_calendars(root / 'calendar', ['BTCUSDT'], START, END, sha, CalendarFake())
            manifest_path = cap / 'manifest.json'
            manifest = source.decode(manifest_path.read_bytes())
            calendar_sha = source.digest(manifest_path.read_bytes())
            calendars, meta = audit.replay_calendars(cap, calendar_sha, demo, ['BTCUSDT'])
            self.assertTrue(meta['calendar_partition_check'])
            self.assertEqual(len(calendars['BTCUSDT']), 1)
            self.assertEqual(cap.stat().st_mode & 0o777, 0o700)
            self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
            raw_path = cap / '0001.raw'
            original = raw_path.read_bytes()
            raw_path.write_bytes(original + b' ')
            with self.assertRaisesRegex(ValueError, 'RAW_HASH'):
                audit.replay_calendars(cap, calendar_sha, demo, ['BTCUSDT'])
            payload = source.decode(original)
            payload['result']['list'] = []
            raw_path.write_bytes(source.encode(payload))
            manifest['pages'][1]['sha256'] = source.digest(raw_path.read_bytes())
            manifest_path.write_bytes(source.encode(manifest))
            calendar_sha = source.digest(manifest_path.read_bytes())
            with self.assertRaisesRegex(ValueError, 'PARTITIONS_DISAGREE'):
                audit.replay_calendars(cap, calendar_sha, demo, ['BTCUSDT'])

    def test_calendar_saturation_duplicate_wrong_symbol(self):
        request = audit.calendar_plan(['BTCUSDT'], START, END)[0]
        payload = source.decode(CalendarFake().get(request))
        for rows in (payload['result']['list'] * 200, payload['result']['list'] * 2,
                     [{**payload['result']['list'][0], 'symbol': 'ETHUSDT'}]):
            changed = copy.deepcopy(payload)
            changed['result']['list'] = rows
            with self.assertRaises(ValueError):
                audit.calendar_rows(source.encode(changed), request)


if __name__ == '__main__':
    unittest.main()
