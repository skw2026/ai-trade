#!/usr/bin/env python3
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import collect_c2_fx_proxy as fx
import audit_bybit_readonly_evidence as wire

START, END, NOW = 1788701451985, 1788768000000, 1789370000000


class Fake:
    def get(self, page):
        # Numeric JSON literals intentionally avoid binary-float formatting.
        return ('[' + ','.join('[' + str(t // 1000) + ',0.99989,1.00001,0.99990,0.99999,123.45]'
            for t in reversed(range(page['start_ms'], page['end_ms'] + fx.MINUTE, fx.MINUTE))) + ']').encode()


class FxProxyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.clock = patch.object(wire, 'now_ms', return_value=NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def capture(self):
        cap = fx.collect(self.root, START, END, Fake())
        return cap, wire.digest((cap / 'manifest.json').read_bytes())

    def mutate(self, cap, change):
        manifest = wire.decode((cap / 'manifest.json').read_bytes())
        file = cap / manifest['pages'][0]['file']
        rows = json.loads(file.read_bytes())
        change(rows)
        file.write_bytes(json.dumps(rows).encode())
        manifest['pages'][0]['sha256'] = wire.digest(file.read_bytes())
        (cap / 'manifest.json').write_bytes(wire.encode(manifest))
        return wire.digest((cap / 'manifest.json').read_bytes())

    def test_complete_decimal_replay_and_false_qualification(self):
        cap, sha = self.capture()
        data, report = fx.replay(cap, sha)
        self.assertEqual(report['candle_count'], 1112)
        self.assertEqual(report['response_pages'], 4)
        self.assertEqual(report['observed_low_usd_per_usdt'], '0.99989')
        self.assertEqual(report['observed_high_usd_per_usdt'], '1.00001')
        self.assertEqual(next(iter(data.values()))['close'], '0.99999')
        self.assertFalse(report['bybit_historical_fx_qualified'])
        self.assertFalse(report['unobserved_price_bound_proven'])
        self.assertEqual(cap.stat().st_mode & 0o777, 0o700)

    def test_missing_minutes_remain_gaps(self):
        cap, _ = self.capture()
        sha = self.mutate(cap, lambda rows: rows.pop())
        data, report = fx.replay(cap, sha)
        self.assertEqual(len(data), 1111)
        self.assertEqual(report['missing_minutes'], 1)
        self.assertEqual(report['status'], 'EXTERNAL_FX_PROXY_WITH_GAPS')

    def test_duplicate_bad_ohlc_and_nonfinite_rejected(self):
        for change in (lambda rows: rows.__setitem__(0, rows[1]),
                       lambda rows: rows[0].__setitem__(2, 0.99),
                       lambda rows: rows[0].__setitem__(1, float('nan'))):
            cap, _ = self.capture()
            sha = self.mutate(cap, change)
            with self.assertRaises(ValueError): fx.replay(cap, sha)

    def test_raw_hash_and_request_tamper_rejected(self):
        cap, sha = self.capture()
        (cap / '0000.raw').write_bytes(b'[]')
        with self.assertRaisesRegex(ValueError, 'RAW_HASH'): fx.replay(cap, sha)
        with self.assertRaisesRegex(ValueError, 'MANIFEST_HASH'): fx.replay(cap, '0' * 64)

    def test_transport_is_fixed_public_get_without_auth(self):
        transport = fx.Transport()
        page = fx.plan(START, END)[0]
        class Reply:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, n): return Fake().get(page)
        with patch.object(transport.opener, 'open', return_value=Reply()) as call:
            transport.get(page)
        req = call.call_args.args[0]
        self.assertTrue(req.full_url.startswith(fx.ENDPOINT + '?'))
        self.assertEqual(req.method, 'GET')
        self.assertEqual(set(dict(req.header_items())), {'User-agent'})

    def test_window_is_bounded_and_closed(self):
        with self.assertRaises(ValueError): fx.plan(1, END)
        with self.assertRaises(ValueError): fx.plan(START, START + 3 * wire.DAY)
        with self.assertRaisesRegex(ValueError, 'NOT_CLOSED'):
            fx.collect(self.root, NOW - 60000, NOW, Fake())


if __name__ == '__main__':
    unittest.main()
