import unittest

import sparse_reversal_2024_intake as intake


class IntakeTests(unittest.TestCase):
    def test_year_is_not_warmup(self):
        r = intake.assess(intake.START-3600000, intake.END)
        self.assertEqual(r['status'], 'INPUT_INSUFFICIENT')
        self.assertTrue(r['evaluation_coverage'])
        self.assertFalse(r['warmup_coverage'])
        self.assertEqual(r['missing_warmup_bars'], 276)
        self.assertEqual(r['evaluation_bars'], 105408)
        self.assertFalse(r['qualification'])
        self.assertEqual(r['economic_attempts'], 0)

    def test_full_warmup_is_only_coverage(self):
        r = intake.assess(intake.WARM, intake.END)
        self.assertEqual(r['status'], 'COVERAGE_READY_NOT_ECONOMICALLY_ADMITTED')
        self.assertFalse(r['qualification'])

    def test_no_warmup(self):
        self.assertEqual(intake.assess(intake.START, intake.END)['missing_warmup_bars'], 288)

    def test_missing_year_end_rejects(self):
        self.assertEqual(intake.assess(intake.WARM, intake.END-intake.BAR)['status'],
                         'INPUT_INSUFFICIENT')

    def test_rejects_unaligned(self):
        with self.assertRaisesRegex(ValueError, 'BAR_ALIGNMENT'):
            intake.assess(intake.WARM+1, intake.END)

    def test_rejects_empty(self):
        with self.assertRaisesRegex(ValueError, 'INPUT_BOUNDS'):
            intake.assess(intake.END, intake.END)

    def url(self, **changes):
        from urllib.parse import urlencode
        q = dict(category='linear', symbol='BTCUSDT', interval='5',
                 start=intake.WARM, end=intake.START-3600001, limit=1000)
        q.update(changes)
        return 'https://api.bybit.com/v5/market/kline?'+urlencode(q)

    def test_nested_request_found_once(self):
        url = self.url()
        self.assertEqual(intake.overlapping_requests({'a': [url, {'b': url}]}), [url])

    def test_hour_cannot_replace_five_minute(self):
        self.assertEqual(intake.overlapping_requests(self.url(interval='60')), [])

    def test_wrong_symbol_or_market_ignored(self):
        for change in (dict(symbol='ETHUSDT'), dict(category='spot'),
                       dict(start=intake.START, end=intake.END)):
            self.assertEqual(intake.overlapping_requests(self.url(**change)), [])

    def test_lookalike_host_not_trusted(self):
        self.assertEqual(intake.overlapping_requests(
            self.url().replace('api.bybit.com/', 'api.bybit.com.attacker/')), [])


if __name__ == '__main__':
    unittest.main()
