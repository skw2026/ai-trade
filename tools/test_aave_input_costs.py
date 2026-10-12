import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import aave_input_costs as p


class FullInputCostTests(unittest.TestCase):
    def test_leap_year_market_full_grid(self):
        value = p.market_cost()
        self.assertEqual(value['minute_rows_per_series'], 366*1440+60)
        self.assertEqual(value['trade_pages'], 528)
        self.assertEqual(value['mark_pages'], 528)
        self.assertEqual(value['funding_day_pages'], 366)
        self.assertEqual(value['total_requests'], 1422)
        self.assertIn('STOP_INCOMPLETE', value['funding_capped_page'])

    def test_three_native_components_and_reserve(self):
        value = p.calculate(1817, 1250, 3, 3, 3, 64)
        self.assertEqual(value['native_requests'], 4308)
        self.assertEqual(value['projected_total_operations_with_reserve'], 5994)
        self.assertTrue(value['fits_request_budget'])
        self.assertFalse(value['input_admitted'])
        self.assertFalse(value['economic_admitted'])

    def test_receipt_header_only_fit_does_not_admit_actual_total(self):
        value = p.calculate(4000, 3000, 3, 3, 3, 64)
        self.assertLess(4000+3000+64, 10000)
        self.assertFalse(value['fits_request_budget'])

    def test_exact_boundary_and_over_boundary(self):
        initial = p.calculate(10, 7, 0, 0, 0, 5)
        total = initial['projected_total_operations_with_reserve']
        self.assertTrue(p.calculate(10, 7, 0, 0, 0, 5, total)['fits_request_budget'])
        self.assertFalse(p.calculate(10, 7, 0, 0, 0, 5, total-1)['fits_request_budget'])

    def test_no_double_charge_of_successful_cached_reads(self):
        before = p.calculate(10, 7, 3, 3, 3, 20)
        after = p.calculate(10, 7, 4, 4, 4, 23)
        self.assertEqual(before['projected_total_operations_with_reserve'],
                         after['projected_total_operations_with_reserve'])

    def test_invalid_counts(self):
        for values in [(1, 2, 0, 0, 0, 0), (1, 1, 2, 0, 0, 0),
                       (1, 1, 0, 2, 0, 0), (1, 1, 0, 0, 2, 0),
                       (1, 1, 0, 0, 0, -1), (True, 1, 0, 0, 0, 0)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                p.calculate(*values)

    def test_absent_plan_blocks_bulk_before_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(p.a, 'ROOT', Path(directory)):
            with self.assertRaisesRegex(ValueError, 'FULL_INPUT_COST_PLAN_REQUIRED'):
                p.require_bulk_plan()


if __name__ == '__main__':
    unittest.main()
