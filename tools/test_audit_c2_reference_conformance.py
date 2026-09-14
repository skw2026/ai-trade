#!/usr/bin/env python3
import unittest
from unittest.mock import patch
import audit_c2_reference_conformance as conformance


class ConformanceTest(unittest.TestCase):
    def test_published_numeric_cases_pass_but_conflicting_source_is_not_accepted(self):
        result = conformance.audit()
        self.assertEqual(result['passed_cases'], 2)
        source = result['quarantined_sources'][0]
        self.assertEqual(source['current_parameter_im_usdt'], '1302.50')
        self.assertEqual(source['page_parameter_im_usdt'], '2315.00')
        self.assertFalse(source['accepted_as_current_reference'])
        self.assertFalse(result['historical_margin_qualified'])

    def test_actual_calculation_mismatch_fails_instead_of_fixed_pass(self):
        original = conformance.cross.calculate
        def wrong(*args):
            result = original(*args)
            result['total_maintenance_margin_usd'] = '1'
            return result
        with patch.object(conformance.cross, 'calculate', side_effect=wrong):
            result = conformance.audit()
        self.assertEqual(result['status'], 'REFERENCE_CASE_MISMATCH')
        self.assertEqual(result['passed_cases'], 0)


if __name__ == '__main__':
    unittest.main()
