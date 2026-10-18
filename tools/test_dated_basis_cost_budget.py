import unittest
from decimal import Decimal as D
from fractions import Fraction as F

from dated_basis_cost_budget import cost_capacity, early_exit_fee_budget, number


def case(**changes):
    values=dict(face='2500',spot_spend_without_fee='2490',
                terminal_value_without_fee='2510',gross='21',slippage='1',
                annual='0.06',held_days='7',spot_rate='0.00075')
    values.update(changes)
    return cost_capacity(**values)


class CostBudgetTests(unittest.TestCase):
    def test_decimal_matches_independent_rational_budget(self):
        result=case()
        independent=(F(21)-1-F(2490)*F(6,100)*7/365)*4
        self.assertLess(abs(result['fee_budget_bps']-D(independent.numerator)/D(independent.denominator)),D('1e-24'))
        self.assertEqual(result['spot_fee_floor_bps'],D('15'))

    def test_one_bps_is_not_one_percent(self):
        self.assertEqual(number('0.0001')*10000,1)
        self.assertEqual(case(spot_rate='0.001')['spot_fee_floor_bps'],20)

    def test_equality_is_not_positive_advantage(self):
        result=case(gross='1',slippage='1',spot_spend_without_fee='2500',
                    terminal_value_without_fee='2500',annual='0',spot_rate='0')
        self.assertTrue(result['equal_is_not_positive_advantage'])
        self.assertFalse(result['floor_exceeds_budget'])

    def test_negative_gross_has_no_nonnegative_fee_solution(self):
        result=case(spot_spend_without_fee='2520',gross='-9')
        self.assertTrue(result['floor_exceeds_budget'])
        self.assertLess(result['maximum_spot_rate_for_nonnegative_budget'],0)

    def test_lower_fee_reduces_floor_not_slippage_or_carry(self):
        normal,discount=case(spot_rate='.001'),case()
        self.assertEqual(normal['all_in_cost_floor_bps']-discount['all_in_cost_floor_bps'],5)
        self.assertEqual(normal['capital_floor_bps'],discount['capital_floor_bps'])

    def test_future_fee_floor_identity(self):
        for spot in (F(0),F(75,100000),F(1,1000)):
            for future_cost in (F(0),F(1,10),F(5)):
                direct=(F(2510)-future_cost)*(1-spot)-F(2490)*(1+spot)
                fee_cost=F(20)-direct
                self.assertEqual(fee_cost,spot*5000+(1-spot)*future_cost)
                self.assertGreaterEqual(fee_cost,spot*5000)

    def test_unknown_rates_fail_not_zero(self):
        for value in (None, True, 'NaN', 'Infinity'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                case(spot_rate=value)

    def test_invalid_domain_and_identity_rejected(self):
        for patch in ({'face':'0'},{'annual':'-.1'},{'held_days':'-1'},
                      {'spot_rate':'1'},{'spot_rate':'-.001'},{'gross':'22'}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):case(**patch)

    def test_equal_taker_early_exit_no_fee_saving(self):
        self.assertEqual(early_exit_fee_budget(settlement_rate='.0005',closing_rate='.0005',close_slippage_rate='.0002'),D('-2'))

    def test_synthetic_maker_saving_cannot_erase_residual_basis(self):
        self.assertEqual(early_exit_fee_budget(settlement_rate='.0005',closing_rate='.0001',close_slippage_rate='.0002'),D('2'))
        with self.assertRaises(ValueError):
            early_exit_fee_budget(settlement_rate=None,closing_rate='.0001',close_slippage_rate='.0002')


if __name__=='__main__':unittest.main()
