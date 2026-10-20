import json
import unittest

import compile_cross_asset_inputs as c
from test_cross_asset_inputs import fixture


class Compile(unittest.TestCase):
    def test_separate_decoder(self):
        for kind in ('trade', 'mark'):
            r, o = fixture(kind)
            rows = c.original_rows(json.dumps(o), kind, 'BTCUSDT', r['start'], r['stop'], 60000)
            self.assertEqual(len(rows), 60)
            self.assertEqual(len(rows[0][1]), 5 if kind == 'trade' else 4)

    def test_separate_decoder_rejects(self):
        for mutation in (
            lambda o: o.update(retCode=1),
            lambda o: o['result'].update(symbol='SOLUSDT'),
            lambda o: o['result']['list'].pop(),
            lambda o: o['result']['list'][0].__setitem__(4, '103'),
            lambda o: o['result']['list'][0].__setitem__(5, '-0.1'),
        ):
            r, o = fixture()
            mutation(o)
            with self.assertRaises(ValueError):
                c.original_rows(json.dumps(o), 'trade', 'BTCUSDT', r['start'], r['stop'], 60000)

    def test_daily_only_closed(self):
        daily = {'X': {}}
        c.daily_accept(daily, 'X', c.DAY-120000, 60000, '12')
        self.assertEqual(daily, {'X': {}})
        c.daily_accept(daily, 'X', c.DAY-60000, 60000, '13.100')
        self.assertEqual(daily['X'][str(c.DAY)], '13.100')

    def test_cross_frequency_identity(self):
        daily = {'X': {}}
        c.daily_accept(daily, 'X', c.DAY-3600000, 3600000, '100')
        c.daily_accept(daily, 'X', c.DAY-60000, 60000, '100.00')
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            c.daily_accept(daily, 'X', c.DAY-60000, 60000, '100.01')


if __name__ == '__main__':
    unittest.main()
