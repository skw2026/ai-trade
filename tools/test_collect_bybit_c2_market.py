#!/usr/bin/env python3
import copy
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import collect_bybit_c2_market as market
import audit_bybit_readonly_evidence as wire
from test_audit_option_subaccount_ledger import CALL

START, END, NOW = 1788701451985, 1788768000000, 1789370000000


class Fake:
    def get(self, request):
        p, group = request['params'], request['group']
        result = {'category': p['category'], 'symbol': p['symbol'], 'list': []}
        if group == 'risk':
            result['list'] = [{'symbol': 'BTCUSDT'}]
        elif group == 'funding':
            result['list'] = [{'symbol': 'BTCUSDT', 'fundingRateTimestamp': str(END), 'fundingRate': '0.0001'}]
        else:
            result['list'] = [[str(t), '80000', '80100', '79900', '80001']
                              for t in reversed(range(p['start'], p['end'] + market.MINUTE, market.MINUTE))]
        return wire.encode({'retCode': 0, 'time': NOW, 'result': result})


class C2MarketTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.clock = patch.object(wire, 'now_ms', return_value=NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def capture(self, options=()):
        cap = market.collect(self.root, START, END, options, Fake())
        return cap, wire.digest((cap / 'manifest.json').read_bytes())

    def mutate(self, cap, index, change):
        manifest = wire.decode((cap / 'manifest.json').read_bytes())
        page = manifest['pages'][index]
        path = cap / page['file']
        raw = wire.decode(path.read_bytes())
        change(raw)
        path.write_bytes(wire.encode(raw))
        page['sha256'] = wire.digest(path.read_bytes())
        (cap / 'manifest.json').write_bytes(wire.encode(manifest))
        return wire.digest((cap / 'manifest.json').read_bytes())

    def test_exact_page_plan_and_minute_coverage(self):
        requests = market.plan(START, END)
        self.assertEqual(len(requests), 6)
        cap, sha = self.capture()
        data, report = market.replay(cap, sha)
        self.assertEqual(len(data['mark']), 1112)
        self.assertEqual(set(data['mark']), set(data['index']))
        self.assertEqual(report['status'], 'PUBLIC_MARKET_REPLAYED')
        self.assertFalse(report['exact_settlement_marks_qualified'])
        self.assertFalse(report['historical_risk_tiers_qualified'])
        self.assertEqual(cap.stat().st_mode & 0o777, 0o700)
        self.assertEqual((cap / 'manifest.json').stat().st_mode & 0o777, 0o600)

    def test_missing_required_candle_fails_even_with_new_hash(self):
        cap, _ = self.capture()
        sha = self.mutate(cap, 0, lambda raw: raw['result']['list'].pop())
        with self.assertRaisesRegex(ValueError, 'COVERAGE'):
            market.replay(cap, sha)

    def test_duplicate_or_wrong_symbol_fails(self):
        for change in (lambda raw: raw['result']['list'].__setitem__(0, raw['result']['list'][1]),
                       lambda raw: raw['result'].update(symbol='ETHUSDT')):
            cap, _ = self.capture()
            sha = self.mutate(cap, 0, change)
            with self.assertRaises(ValueError): market.replay(cap, sha)

    def test_optional_option_history_gaps_do_not_hide_core_results(self):
        cap, _ = self.capture([CALL])
        sha = self.mutate(cap, 4, lambda raw: raw['result']['list'].clear())
        data, report = market.replay(cap, sha)
        self.assertEqual(report['option_history_incomplete'], ['option:' + CALL])
        self.assertEqual(len(data['index']), 1112)
        self.assertEqual(report['status'], 'PUBLIC_MARKET_REPLAYED_WITH_GAPS')

    def test_request_manifest_tamper_rejected(self):
        cap, sha = self.capture()
        raw = (cap / '0000.raw').read_bytes()
        (cap / '0000.raw').write_bytes(raw + b' ')
        with self.assertRaisesRegex(ValueError, 'RAW_HASH'):
            market.replay(cap, sha)

    def test_option_api_failure_is_an_explicit_gap_not_zero_price(self):
        cap, _ = self.capture([CALL])
        sha = self.mutate(cap, 4, lambda raw: raw.update(retCode=10001, result={}))
        data, report = market.replay(cap, sha)
        self.assertEqual(report['option_history_error_codes'], {'option:' + CALL: [10001]})
        self.assertEqual(len(data['mark']), 1112)
        self.assertEqual(len(data['option:' + CALL]), 612)

    def test_transport_public_get_no_auth_and_rejects_injected_query(self):
        requests = market.plan(START, END)
        transport = market.Transport(requests)
        class Reply:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, count): return Fake().get(requests[0])
        with patch.object(transport.opener, 'open', return_value=Reply()) as call:
            transport.get(requests[0])
        req = call.call_args.args[0]
        self.assertEqual(req.method, 'GET')
        self.assertTrue(req.full_url.startswith('https://api.bybit.com/'))
        self.assertEqual(set(dict(req.header_items())), {'User-agent'})
        changed = copy.deepcopy(requests[0])
        changed['params']['api_key'] = 'bad'
        with self.assertRaisesRegex(ValueError, 'NOT_ALLOWED'): transport.get(changed)

    def test_invalid_window_and_unknown_option_rejected(self):
        for start, end, symbols in ((END, START, []), (START, START + 3 * wire.DAY, []),
                                    (START, END, ['ETH-1JAN27-1000-C-USDT'])):
            with self.assertRaises(ValueError): market.plan(start, end, symbols)


if __name__ == '__main__':
    unittest.main()
