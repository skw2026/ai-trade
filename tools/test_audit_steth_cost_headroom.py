from fractions import Fraction
import unittest

import audit_steth_cost_headroom as audit
import steth_cost_headroom as screen


class IndependentAuditTests(unittest.TestCase):
    def setUp(self):
        self.quotes = [10**18, 10**18+31*10**14, 10**18+31*10**14+1]
        self.result = screen.measure(self.quotes, screen.config())

    def check(self):
        audit.check_numbers(self.result, self.quotes, 10**18, Fraction(31), Fraction(62))

    def test_independent_rational_agreement(self):
        self.check()

    def test_each_numeric_field_tamper_fails(self):
        for key in ('samples', 'positive_gross_samples', 'reference_cost_covered', 'stress_cost_covered',
                    'economic_attempts', 'qualified_candidates'):
            original = self.result[key]
            self.result[key] += 1
            with self.assertRaisesRegex(ValueError, 'AUDIT_VALUE'): self.check()
            self.result[key] = original

    def test_extrema_or_cost_tamper_fails(self):
        for key in ('gross_min_bps', 'gross_max_bps', 'maximum_reference_headroom_bps', 'maximum_stress_headroom_bps'):
            original = self.result[key]; self.result[key] = '999'
            with self.assertRaisesRegex(ValueError, 'AUDIT_EXACT'): self.check()
            self.result[key] = original

    def test_mean_is_not_ignored(self):
        self.result['gross_mean_bps'] = '999'
        with self.assertRaisesRegex(ValueError, 'AUDIT_MEAN'): self.check()

    def test_cannot_invent_risk_or_profitability(self):
        for key in ('full_strategy_pnl', 'sharpe', 'maximum_drawdown'):
            self.result[key] = 0
            with self.assertRaisesRegex(ValueError, 'AUDIT_VALUE'): self.check()
            self.result[key] = None

    def test_wrong_decision_fails(self):
        self.result['decision'] = 'PASS'
        with self.assertRaisesRegex(ValueError, 'AUDIT_ADJUDICATION'): self.check()


if __name__ == '__main__':
    unittest.main()
