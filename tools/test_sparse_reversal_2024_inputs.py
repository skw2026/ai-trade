import copy
from decimal import Decimal as D
import json
import unittest
from urllib.parse import parse_qs, urlsplit

import sparse_reversal_2024_inputs as m


class InputTests(unittest.TestCase):
    def payload(self, kind='trade'):
        r = m.requests()[0 if kind == 'trade' else 1]
        values = ['10', '12', '9', '11']+(['2', '20'] if kind == 'trade' else [])
        obj = dict(retCode=0, time=m.JOIN, result=dict(category='linear', symbol='BTCUSDT',
                   list=[[str(t)]+values for t in reversed(range(m.WARM, m.JOIN, m.BAR))]))
        return r, obj

    def test_exact_two_requests(self):
        rs = m.requests()
        self.assertEqual(len(rs), 2)
        for r in rs:
            u = urlsplit(r['url']); q = parse_qs(u.query)
            self.assertEqual(u.hostname, 'api.bybit.com')
            self.assertEqual(q['interval'], ['5'])
            self.assertEqual(q['symbol'], ['BTCUSDT'])
            self.assertEqual(int(q['end'][0])+1, m.JOIN)
            self.assertEqual((r['stop']-r['start'])//m.BAR, 276)

    def test_both_pages(self):
        for kind in ('trade', 'mark'):
            r, obj = self.payload(kind)
            self.assertEqual(len(m.validate(json.dumps(obj), r)), 276)

    def test_missing_or_duplicate(self):
        r, obj = self.payload()
        for rows in (obj['result']['list'][:-1], obj['result']['list']+[obj['result']['list'][0]]):
            bad = copy.deepcopy(obj); bad['result']['list'] = rows
            with self.assertRaises(ValueError): m.validate(json.dumps(bad), r)

    def test_wrong_identity(self):
        r, obj = self.payload(); obj['result']['symbol'] = 'ETHUSDT'
        with self.assertRaises(ValueError): m.validate(json.dumps(obj), r)

    def test_api_failure(self):
        r, obj = self.payload(); obj['retCode'] = 10016
        with self.assertRaises(ValueError): m.validate(json.dumps(obj), r)

    def test_invalid_ohlc(self):
        r, obj = self.payload(); obj['result']['list'][0][2] = '8'
        with self.assertRaises(ValueError): m.validate(json.dumps(obj), r)

    def test_nonfinite(self):
        r, obj = self.payload(); obj['result']['list'][0][1] = 'NaN'
        with self.assertRaises(ValueError): m.validate(json.dumps(obj), r)

    def test_wrong_clock(self):
        r, obj = self.payload(); obj['time'] = m.WARM
        with self.assertRaises(ValueError): m.validate(json.dumps(obj), r)

    def test_exact_aggregation(self):
        rows = [['10', '12', '9', '11', '.1'], ['11', '15', '8', '14', '.2']]
        self.assertEqual(list(map(D, m.aggregate(rows))), list(map(D, ['10','15','8','14','.3'])))
        self.assertEqual(m.aggregate([v[:4] for v in rows]), ['10','15','8','14'])

    def test_invalid_width(self):
        for rows in ([], [[1]], [[1, 2, 1, 2], [1, 2, 1, 2, 1]]):
            with self.assertRaises(ValueError): m.aggregate(rows)


if __name__ == '__main__': unittest.main()
