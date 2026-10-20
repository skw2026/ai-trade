"""Synthetic input invariants; never fetch real data."""
import copy
from decimal import Decimal
import json
import unittest

import cross_asset_inputs as m


def fixture(kind='trade', count=60):
    r = dict(kind=kind, start=0, stop=count*60000)
    row = ['100', '102', '98', '101']
    rows = [[str(t)]+row+(['0.1', '10'] if kind == 'trade' else [])
            for t in range(0, r['stop'], 60000)][::-1]
    obj = dict(retCode=0, time=r['stop']+1,
               result=dict(category='linear', symbol='BTCUSDT', list=rows))
    return r, obj


class Inputs(unittest.TestCase):
    def test_plan(self):
        r = m.requests()
        self.assertEqual(len(r), 1056)
        self.assertEqual(len({x['url'] for x in r}), 1056)
        self.assertEqual([(x['kind'], x['index']) for x in r[:6]],
                         [(k, i) for k in ('trade', 'mark') for i in (0, 263, 527)])
        for k in ('trade', 'mark'):
            pages = sorted((x for x in r if x['kind'] == k), key=lambda x: x['start'])
            self.assertEqual(pages[0]['start'], m.START)
            self.assertEqual(pages[-1]['stop'], m.END)
            self.assertTrue(all(a['stop'] == b['start'] for a, b in zip(pages, pages[1:])))
            self.assertEqual(sum((x['stop']-x['start'])//60000 for x in pages), 527100)

    def test_valid(self):
        for kind in ('trade', 'mark'):
            r, o = fixture(kind)
            self.assertEqual(len(m.validate(json.dumps(o), r)), 60)

    def test_rejections(self):
        for mutation in (
            lambda o: o.update(retCode=10016),
            lambda o: o.update(retCode=False),
            lambda o: o.update(time=0),
            lambda o: o['result'].update(symbol='ETHUSDT'),
            lambda o: o['result'].update(category='inverse'),
            lambda o: o['result']['list'].pop(),
            lambda o: o['result']['list'].reverse(),
            lambda o: o['result']['list'][0].__setitem__(1, '0'),
            lambda o: o['result']['list'][0].__setitem__(2, '99'),
            lambda o: o['result']['list'][0].__setitem__(5, '-1'),
            lambda o: o['result']['list'][0].__setitem__(6, '-1'),
            lambda o: o['result']['list'][0].__setitem__(3, 'NaN'),
        ):
            r, o = fixture()
            mutation(o)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                m.validate(json.dumps(o), r)

    def test_duplicate_json(self):
        r, _ = fixture()
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            m.validate('{"retCode":0,"retCode":1}', r)

    def test_hour_exact(self):
        for kind in ('trade', 'mark'):
            r, o = fixture(kind)
            rows = m.validate(json.dumps(o), r)
            hour = {0: ['0', '100.0', '102', '98', '101']+
                    (['6.000', '600'] if kind == 'trade' else [])}
            result = m.compare_hours(rows, hour, kind)
            self.assertEqual(result, dict(compared_hours=1, mismatches=[]))

    def test_hour_mismatch_not_rounded(self):
        r, o = fixture()
        hour = {0: ['0', '100', '102', '98', '101', '6.00001', '600']}
        result = m.compare_hours(m.validate(json.dumps(o), r), hour, 'trade')
        self.assertEqual(result['mismatches'][0]['field'], 'volume')

    def test_incomplete_hour_not_coverage_pass(self):
        r, o = fixture(count=59)
        self.assertEqual(m.compare_hours(m.validate(json.dumps(o), r), {}, 'trade'),
                         dict(compared_hours=0, mismatches=[]))

    def test_missing_old_hour(self):
        r, o = fixture()
        with self.assertRaises(KeyError):
            m.compare_hours(m.validate(json.dumps(o), r), {}, 'trade')

    def test_duplicate_minute_rejected(self):
        r, o = fixture()
        o['result']['list'][0] = copy.deepcopy(o['result']['list'][1])
        with self.assertRaisesRegex(ValueError, 'GRID'):
            m.validate(json.dumps(o), r)

    def test_wire_no_error_becomes_data(self):
        c = m.WIRE
        status, _, _, body = m.unpack(b'HTTP/2 403\r\ncontent-type: text/plain\r\n\r\nno\n403', c)
        self.assertEqual(status, 403)
        self.assertEqual(body, b'no')


if __name__ == '__main__':
    unittest.main()
