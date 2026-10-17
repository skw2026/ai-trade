import copy
import io
import unittest
import zipfile
from dated_basis_boundary_audit import archive_rows, compare
from dated_basis_input_probe import digest

EXPIRY = 1695974400000


def fixture():
    hour = EXPIRY - 3600000
    trades = [['1', '100', '2', '1', '2', str(hour), 'false'],
              ['2', '101', '3', '3', '3', str(EXPIRY+3000), 'true']]
    rows = []
    for t, price, qty, count, buy in ((hour, '100', '2', '2', '2'),
                                    (EXPIRY, '101', '3', '1', '0'),
                                    (EXPIRY+3600000, '101', '0', '0', '0')):
        rows.append([str(t), price, price, price, price, qty, str(t+3599999), '0', count, buy, '0', '0'])
    return trades, rows


class BoundaryTests(unittest.TestCase):
    def test_cross_format_is_not_identity(self):
        report = compare(*fixture(), EXPIRY)
        self.assertTrue(report['consistency_pass'])
        self.assertFalse(report['historical_identity_admitted'])
        self.assertFalse(report['settlement_price_proven'])
        self.assertEqual(report['post_expiry_trades'], 1)
        self.assertEqual(report['last_trade_delay_ms'], 3000)
        self.assertEqual(report['constant_zero_volume_rows'], 1)

    def test_mismatch_is_reported(self):
        trades, rows = fixture()
        rows[0][5] = '200'
        result = compare(trades, rows, EXPIRY)
        self.assertFalse(result['consistency_pass'])
        self.assertEqual(result['mismatched_hours'], [EXPIRY-3600000])

    def test_nonempty_hour_missing(self):
        trades, rows = fixture()
        rows[2][5] = '1'
        result = compare(trades, rows, EXPIRY)
        self.assertFalse(result['consistency_pass'])
        self.assertEqual(result['unmatched_nonempty_hours'], [EXPIRY+3600000])

    def test_exact_boundary_is_post(self):
        trades, rows = fixture()
        trades[1][5] = str(EXPIRY)
        self.assertEqual(compare(trades, rows, EXPIRY)['post_expiry_trades'], 1)

    def test_bad_order_time_and_value(self):
        for index, value in ((0, '1'), (1, 'NaN'), (2, '-3'), (2, '0.1'),
                             (3, '1'), (5, str(EXPIRY//1000)), (6, 'no')):
            trades, rows = fixture()
            trades[1][index] = value
            with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                compare(trades, rows, EXPIRY)

    def test_kline_grid_and_duplicate(self):
        trades, rows = fixture()
        with self.assertRaises(ValueError):
            compare(trades, rows + [rows[-1]], EXPIRY)
        rows[0][6] = str(EXPIRY)
        with self.assertRaises(ValueError):
            compare(trades, rows, EXPIRY)

    def test_gap_is_diagnostic(self):
        trades, rows = fixture()
        trades[1][3:5] = ['5', '5']
        self.assertEqual(compare(trades, rows, EXPIRY)['underlying_id_gaps'], 2)

    def test_checksum_and_member(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as out:
            out.writestr('sample.csv', 'agg_trade_id,price\n1,100\n')
        raw = stream.getvalue()
        checksum = (digest(raw)+'  sample.zip').encode()
        self.assertEqual(archive_rows(raw, checksum, 'sample.zip'), [['1', '100']])
        with self.assertRaises(ValueError):
            archive_rows(raw+b'x', checksum, 'sample.zip')
        with self.assertRaises(ValueError):
            archive_rows(raw, checksum, '../sample.zip')


if __name__ == '__main__':
    unittest.main()
