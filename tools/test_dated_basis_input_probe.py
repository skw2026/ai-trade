import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dated_basis_input_probe as probe


class ProbeTests(unittest.TestCase):
    def raw(self, obj):
        return json.dumps(obj).encode()

    def test_fixed_targets(self):
        self.assertEqual(probe.TARGETS['BTCUSD_230929'], 1695974400000)
        self.assertEqual(len(probe.TARGETS), 4)

    def test_delivery_all_is_not_admission(self):
        rows = [dict(deliveryTime=t, deliveryPrice='27000.123456789') for t in probe.TARGETS.values()]
        result = probe.parse('delivery', self.raw(rows))
        self.assertEqual(len(result['target_matches']), 4)
        self.assertEqual(result['missing_targets'], [])
        self.assertEqual(result['status'], 'DELIVERY_ONLY_NOT_ADMITTED')

    def test_empty_or_wrong_clock_not_matched(self):
        self.assertEqual(len(probe.parse('delivery', b'[]')['missing_targets']), 4)
        row = dict(deliveryTime=1695945600000, deliveryPrice='27000')
        self.assertEqual(probe.parse('delivery', self.raw([row]))['target_matches'], {})

    def test_precision(self):
        raw = b'[{"deliveryTime":1695974400000,"deliveryPrice":27000.123456789012345}]'
        self.assertEqual(probe.parse('delivery', raw)['target_matches']['BTCUSD_230929'],
                         '27000.123456789012345')

    def test_invalid_prices(self):
        for value in ('NaN', 'Infinity', '-1', '0', True, None, 'abc'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probe.parse('delivery', self.raw([dict(deliveryTime=1695974400000, deliveryPrice=value)]))

    def test_timestamp_and_duplicate(self):
        for value in (1695974400, '1695974400000', True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probe.parse('delivery', self.raw([dict(deliveryTime=value, deliveryPrice='1')]))
        row = dict(deliveryTime=1695974400000, deliveryPrice='1')
        with self.assertRaises(ValueError):
            probe.parse('delivery', self.raw([row, row]))

    def test_duplicate_json_key(self):
        with self.assertRaises(ValueError):
            probe.parse('delivery', b'{"code":-1,"code":-2}')

    def test_errors_not_data(self):
        self.assertEqual(probe.parse('delivery', b'{"code":-1121,"msg":"bad symbol"}')['status'],
                         'API_ERROR_NOT_INPUT')
        for raw in (b'{}', b'null', b'<html>'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                probe.parse('delivery', raw)

    def test_current_info_not_history(self):
        row = dict(symbol='BTCUSD_230929', maintMarginPercent='2.5')
        report = probe.parse('exchange', self.raw(dict(symbols=[row])))
        self.assertFalse(report['historical_identity_proven'])
        self.assertFalse(report['historical_margin_proven'])
        with self.assertRaises(ValueError):
            probe.parse('exchange', self.raw(dict(symbols=[row, row])))

    def test_endpoint_and_size_restrictions(self):
        with self.assertRaises(ValueError):
            probe.fetch('account', '/nonexistent')
        with self.assertRaises(ValueError):
            probe.parse('delivery', b' ' * (probe.MAX_BYTES + 1))

    def test_fetch_access_denial_no_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(probe.subprocess, 'run') as run:
            run.return_value = probe.subprocess.CompletedProcess([], 0, b'451', b'')
            folder = Path(directory)/'attempt'
            self.assertEqual(probe.fetch('delivery', folder)['status'], 'ACCESS_DENIED_STOP_HOST')
            self.assertTrue((folder/'receipt.json').exists())
            self.assertEqual(run.call_count, 1)
            argv = run.call_args.args[0]
            self.assertEqual(argv[:2], ['curl', '--disable'])
            self.assertNotIn('--location', argv)
            with self.assertRaises(FileExistsError):
                probe.fetch('delivery', folder)

    def test_transport_and_server_failure_preserved(self):
        for rc, http in ((92, b'000'), (0, b'500')):
            with tempfile.TemporaryDirectory() as directory, patch.object(probe.subprocess, 'run') as run:
                run.return_value = probe.subprocess.CompletedProcess([], rc, http, b'error')
                folder = Path(directory)/'attempt'
                with self.assertRaises(ValueError):
                    probe.fetch('delivery', folder)
                self.assertEqual(json.loads((folder/'receipt.json').read_text())['returncode'], rc)


if __name__ == '__main__':
    unittest.main()
