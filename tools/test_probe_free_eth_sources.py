import json
import unittest
from probe_free_eth_sources import allowed, supply_summary


class FreeSourcesTest(unittest.TestCase):
    def test_public_hosts(self):
        self.assertTrue(allowed('https://api.github.com/repos/coinmetrics/data/forks?sort=oldest'))
        self.assertTrue(allowed('https://ultrasound.money/api/v2/fees/supply-over-time'))

    def test_forbidden_targets(self):
        for url in ('http://api.github.com/repos/x', 'https://u:p@api.github.com/repos/x',
                    'https://api.github.com:443/repos/x', 'https://api.github.com/user',
                    'https://api.github.com.evil.test/repos/x', 'https://api.bybit.com/v5/order/create',
                    'https://ultrasound.money/api/x#fragment', 'file:///etc/passwd'):
            with self.subTest(url=url):
                self.assertFalse(allowed(url))

    def sample(self, points):
        return json.dumps({'since_merge': points}).encode()

    def test_missing_days_not_admitted(self):
        result = supply_summary(self.sample([{'timestamp': '2023-09-09T00:00:00Z', 'supply': 100}]))
        self.assertEqual(result['required_days'], 1100)
        self.assertEqual(len(result['missing_days']), 1099)
        self.assertFalse(result['historical_publication_proven'])
        self.assertFalse(result['admitted_to_old_contract'])
        self.assertEqual(result['fixed_samples']['2023-09-18']['2023-09-09'], '100')

    def test_duplicate_daily(self):
        row = {'timestamp': '2023-09-09T00:00:00Z', 'supply': 100}
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            supply_summary(self.sample([row, row]))

    def test_appended_live_point_not_daily(self):
        result = supply_summary(self.sample([
            {'timestamp': '2023-09-09T00:00:00Z', 'supply': 100},
            {'timestamp': '2023-09-09T12:00:00Z', 'supply': 101}]))
        self.assertEqual(result['daily_points'], 1)

    def test_bad_values(self):
        for value in (True, -1, 0, 'NaN', 'Infinity'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                supply_summary(self.sample([{'timestamp': '2023-09-09T00:00:00Z', 'supply': value}]))

    def test_schema(self):
        with self.assertRaisesRegex(ValueError, 'SUPPLY_SCHEMA'):
            supply_summary(b'{"error":"access denied"}')

    def test_timezone(self):
        with self.assertRaisesRegex(ValueError, 'UTC_REQUIRED'):
            supply_summary(self.sample([{'timestamp': '2023-09-09T00:00:00+08:00', 'supply': 100}]))

    def test_wrapped(self):
        self.assertEqual(supply_summary(b'{"data":{"since_merge":[]}}')['daily_points'], 0)


if __name__ == '__main__':
    unittest.main()
