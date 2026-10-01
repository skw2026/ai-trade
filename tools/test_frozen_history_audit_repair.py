"""Synthetic guards for the read-only numeric repair, no historical fixtures."""
import math
import unittest
from audit_frozen_history_transfer import compare_bounds
from audit_offline_policy_correction import lower_bound
from run_bounded_learning import hac_lower


class NumericAuditTests(unittest.TestCase):
    def test_identical_and_none(self):
        compare_bounds(None,None);compare_bounds({'x':None},{'x':None});compare_bounds({'x':0.0},{'x':0.0})
    def test_nonconstant_synthetic_hac(self):
        for i in range(1,40):
            values=[math.sin(j*i)*100+math.cos(j/3)*20+1000*(-1)**i for j in range(51)]
            compare_bounds({'x':hac_lower(values)},{'x':lower_bound(values)})
    def test_four_ulp_roundoff(self):
        value=7.0
        for _ in range(4):value=math.nextafter(value,math.inf)
        compare_bounds({'x':7.0},{'x':value})
    def test_material_difference_rejected(self):
        for value in (7.000001,7+16*math.ulp(7.0)):
            with self.assertRaisesRegex(ValueError,'STATISTICS_ULP'):compare_bounds({'x':7.0},{'x':value})
    def test_sign_or_zero_cannot_be_upgraded(self):
        for actual,expected in ((1e-320,-1e-320),(0,math.ulp(0.0)),(-math.ulp(0.0),0)):
            with self.assertRaisesRegex(ValueError,'STATISTICS_SIGN'):compare_bounds({'x':actual},{'x':expected})
    def test_nan_infinity_and_bool_rejected(self):
        for value in (float('nan'),float('inf'),-float('inf'),True):
            with self.assertRaisesRegex(ValueError,'STATISTICS_FINITE'):compare_bounds({'x':value},{'x':1.0})
    def test_key_substitution_rejected(self):
        with self.assertRaisesRegex(ValueError,'STATISTICS_KEYS'):compare_bounds({'x':1.0},{'y':1.0})
    def test_missing_bound_rejected(self):
        for actual,expected in ((None,{'x':1.0}),({'x':None},{'x':1.0})):
            with self.assertRaisesRegex(ValueError,'STATISTICS_NONE'):compare_bounds(actual,expected)


if __name__=='__main__':unittest.main()
