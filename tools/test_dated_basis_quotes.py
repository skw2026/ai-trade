import json
import unittest
from decimal import Decimal as D
from analyze_dated_basis_quotes import Book, levels, quote_pair


def row(U=10, u=11, pu=10, E=1000, mono=1, bids=None, asks=None):
    return dict(mono_ms=mono, wall_ms=E + 5, payload=dict(raw=json.dumps(
        dict(U=U, u=u, pu=pu, E=E, b=bids or [], a=asks or []))))


def book(kind='spot', last=10):
    b = Book(kind)
    b.snapshot(dict(lastUpdateId=last, bids=[['99', '20'], ['98', '20']],
                    asks=[['100', '30'], ['101', '30']]))
    return b


class QuoteTests(unittest.TestCase):
    def test_spot_bridge(self):
        b = book(); b.update(row(U=11)); self.assertTrue(b.bridged)

    def test_future_bridge_requires_snapshot_overlap(self):
        b = book('future')
        with self.assertRaisesRegex(ValueError, 'BRIDGE_GAP'):
            b.update(row(U=11))

    def test_future_bridge_and_chain(self):
        b = book('future'); b.update(row()); b.update(row(U=12, u=13, pu=11))
        self.assertEqual(b.last, 13)

    def test_future_gap(self):
        b = book('future'); b.update(row())
        with self.assertRaisesRegex(ValueError, 'FUTURE_SEQUENCE_GAP'):
            b.update(row(U=12, u=13, pu=9))

    def test_spot_gap(self):
        b = book(); b.update(row())
        with self.assertRaisesRegex(ValueError, 'SPOT_SEQUENCE_GAP'):
            b.update(row(U=13, u=14))

    def test_buffer_and_stale(self):
        b = Book('spot'); b.update(row(U=8, u=10)); b.update(row())
        b.snapshot(dict(lastUpdateId=10, bids=[['99', '20']], asks=[['100', '30']]))
        self.assertEqual((b.applied, b.discarded, len(b.buffer)), (1, 1, 0))

    def test_absolute_quantity_and_zero(self):
        b = book(); b.update(row(bids=[['99', '1'], ['97', '0']]))
        self.assertEqual(b.bids[D(99)], D(1)); self.assertNotIn(D(97), b.bids)

    def test_crossed_rejected(self):
        b = book()
        with self.assertRaisesRegex(ValueError, 'CROSSED'):
            b.update(row(bids=[['100', '1']]))

    def test_clock_regression_rejected(self):
        b = book(); b.update(row())
        with self.assertRaisesRegex(ValueError, 'CLOCK_REGRESSION'):
            b.update(row(U=12, u=12, E=999))

    def test_invalid_levels(self):
        for value in ('NaN', 'Infinity', '-1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                levels([['100', value]])
        with self.assertRaises(ValueError): levels([['0', '1']])
        with self.assertRaises(ValueError): levels([['1', '1'], ['1', '2']])

    def test_inverse_harmonic_depth(self):
        b = book('future'); price, last = b.sweep('bid', D(25), True)
        self.assertEqual(price, D(2500) / (D(2000) / 99 + D(500) / 98))
        self.assertEqual(last, D(98))

    def test_spot_depth_and_missing_tail(self):
        b = book(); self.assertEqual(b.sweep('ask', D(40))[0], D('100.25'))
        b.asks[D(102)] = D(1000)
        with self.assertRaisesRegex(ValueError, 'INSUFFICIENT'):
            b.sweep('ask', D(61))

    def test_unbridged_and_stale_pair(self):
        s, f = book(), book('future')
        self.assertEqual(quote_pair(s, f, 1)[1], 'UNBRIDGED')
        s.update(row()); f.update(row())
        self.assertEqual(quote_pair(s, f, 1002)[1], 'STALE_RECEIVE')
        self.assertEqual(quote_pair(s, f, 0)[1], 'STALE_RECEIVE')

    def test_event_skew_and_fixed_threshold(self):
        s, f = book(), book('future'); s.update(row()); f.update(row(E=1501))
        self.assertEqual(quote_pair(s, f, 2)[1], 'EVENT_SKEW')
        f.event['E'] = 1500
        self.assertIsNone(quote_pair(s, f, 2)[1])

    def test_pair_forecast_decimal_independent(self):
        s, f = book(), book('future'); s.update(row()); f.update(row())
        p, error = quote_pair(s, f, 2); self.assertIsNone(error)
        sp, fp = D(p['spot_vwap']), D(p['future_inverse_vwap'])
        q = D(2500) / (fp * D('.999'))
        i = sp * D('1.0025')
        btc = q * D('.999') + D(2500) * (1 / i - 1 / (fp * D('.999'))) - D('2.5') / i
        expected = btc * sp * D('.999') * D('.998') - q * sp * D('1.001') * D('1.002')
        self.assertAlmostEqual(p['conditional_pressure_cash_forecast'], float(expected), places=9)

    def test_second_snapshot_rejected(self):
        b = book()
        with self.assertRaisesRegex(ValueError, 'SECOND_SNAPSHOT'):
            b.snapshot({})


if __name__ == '__main__':
    unittest.main()
