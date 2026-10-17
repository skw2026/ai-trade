import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import dated_basis_assumption_inputs as model
from dated_basis_input_probe import digest, TARGETS


class InputTests(unittest.TestCase):
    def archive(self, row=None):
        request = model.requests()[0]
        row = row or ['1677628800000', '100', '101', '99', '100', '2',
                      '1677629099999', '200', '3', '1', '100', '0']
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as out:
            out.writestr(request['filename'][:-4]+'.csv', ','.join(row)+'\n')
        raw = stream.getvalue()
        return raw, (digest(raw)+'  '+request['filename']).encode(), request

    def test_exact_request_scope(self):
        requests = model.requests()
        self.assertEqual(len(requests), 12)
        self.assertEqual({r['symbol'] for r in requests}, set(TARGETS))
        self.assertTrue(all(r['url'].startswith('https://data.binance.vision/data/') for r in requests))

    def test_archive_roundtrip(self):
        bars = model.parse_archive(*self.archive())
        self.assertEqual(list(bars), [1677628800000])
        self.assertEqual(bars[1677628800000]['c'], '100')

    def test_checksum_identity(self):
        raw, checksum, request = self.archive()
        with self.assertRaises(ValueError):
            model.parse_archive(raw+b'x', checksum, request)
        with self.assertRaises(ValueError):
            model.parse_archive(raw, checksum.replace(b'BTCUSDT', b'ETHUSDT'), request)

    def test_wrong_time_grid_ohlc_and_nonfinite(self):
        template = ['1677628800000', '100', '101', '99', '100', '2', '1677629099999', '200', '3', '1', '100', '0']
        for index, value in ((0, '1677628800'), (0, '1677628800001'), (2, '98'),
                             (4, 'NaN'), (5, '-1'), (6, '1677629100000'), (8, '1.0')):
            row = template.copy();row[index] = value
            with self.subTest(index=index), self.assertRaises(ValueError):
                model.parse_archive(*self.archive(row))

    def test_scope_not_extendable(self):
        raw, checksum, request = self.archive()
        with self.assertRaises(ValueError):
            model.parse_archive(raw, checksum, dict(request, url='https://other.invalid'))
        with self.assertRaises(ValueError):
            model.fetch(dict(request, symbol='ETHUSD'), '/no-write')

    def test_partial_bar_is_preserved_but_not_usable(self):
        row = ['1677628800000', '100', '101', '99', '100', '2',
               '1677628899999', '200', '3', '1', '100', '0']
        bars = model.parse_archive(*self.archive(row))
        self.assertFalse(bars[1677628800000]['complete'])
        self.assertEqual(bars[1677628800000]['close_ms'], 1677628899999)
        expiry = TARGETS['BTCUSD_230331']
        entry = expiry-7*86400000+300000
        report = model.quality({kind: {entry: bars[1677628800000]} for kind in ('spot','future','mark')}, expiry)
        self.assertIn(entry, report['critical_missing']['spot'])
        self.assertEqual(report['incomplete_bars']['spot'], 1)
        self.assertFalse(report['risk_path_observed'])

    def test_missing_not_filled(self):
        report = model.quality({kind: {} for kind in ('spot', 'future', 'mark')}, TARGETS['BTCUSD_230331'])
        self.assertFalse(report['critical_complete'])
        self.assertFalse(report['risk_path_observed'])
        self.assertFalse(report['historical_identity_admitted'])

    def test_zero_trade_risk_is_unknown(self):
        expiry = TARGETS['BTCUSD_230331']
        bars = {t: dict(o='100', h='100', l='100', c='100', v='1', n=1)
                for t in range(expiry-7*86400000-300000, expiry+3900000, 300000)}
        series = {kind: bars.copy() for kind in ('spot', 'future', 'mark')}
        self.assertTrue(model.quality(series, expiry)['risk_path_observed'])
        series['future'][expiry-600000] = dict(bars[expiry-600000], n=0)
        self.assertFalse(model.quality(series, expiry)['risk_path_observed'])
        self.assertTrue(model.quality(series, expiry)['critical_complete'])

    def test_404_record_no_retry(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(model.subprocess, 'run') as run:
            run.return_value = model.subprocess.CompletedProcess([], 0, b'404', b'')
            self.assertEqual(model.fetch(model.requests()[0], Path(folder)/'request')[0], '404')
            self.assertEqual(run.call_count, 1)
            self.assertTrue((Path(folder)/'request'/'receipt.json').exists())


if __name__ == '__main__':
    unittest.main()
