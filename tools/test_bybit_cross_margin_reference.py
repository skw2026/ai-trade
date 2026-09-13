#!/usr/bin/env python3
import unittest
from decimal import Decimal, localcontext

import bybit_cross_margin_reference as margin


class CrossMarginReferenceTest(unittest.TestCase):
    def position(self, **changes):
        return margin.option_position(**{'kind': 'call', 'qty': '-1', 'index': '30000',
            'mark': '300', 'strike': '31000', 'entry': '350', 'rules': margin.BTC_REFERENCE, **changes})

    def order(self, **changes):
        return margin.option_order(**{'kind': 'call', 'action': 'sell_to_open', 'qty': '1',
            'index': '30000', 'mark': '300', 'strike': '31000', 'price': '350',
            'rules': margin.BTC_REFERENCE, **changes})

    def linear(self, **changes):
        return margin.linear_position(**{'qty': '0.5', 'mark': '50500', 'entry': '50000', 'leverage': '10',
            'taker_fee_rate': '0.00055', 'tiers': [{'limit_usdt': '100000', 'mmr': '0.02',
                'max_leverage': '25', 'mm_deduction_usdt': '0'}], **changes})

    def balances(self, **changes):
        return margin.cross_balances(**{'margin_mode': 'REGULAR_MARGIN', 'currency': 'USDT',
            'wallet': '10000', 'perp_upl': '10', 'option_value': '-600', 'usdt_usd': '0.999',
            'collateral_ratio': '1', 'liabilities': '0', 'other_assets': [], 'open_orders': [], **changes})

    def test_current_official_option_examples_1_and_5(self):
        self.assertEqual(self.position()['mm_usdt'], 1260)
        self.assertEqual(self.position()['im_usdt'], 2350)

    def test_current_official_option_examples_2_3_4(self):
        self.assertEqual(self.order(action='buy_to_open', price='300')['order_im_usdt'], 309)
        self.assertEqual(self.order()['order_im_usdt'], 2009)
        self.assertEqual(self.order(action='buy_to_close', closing_position_size='2',
            closing_position_im='2000', account_position_im='2000', margin_balance='10000')['order_im_usdt'], 0)

    def test_long_option_not_double_charged_premium(self):
        result = self.position(qty='1')
        self.assertEqual((result['im_usdt'], result['mm_usdt'], result['option_value_usdt']), (0, 0, 300))

    def test_put_otm_and_mm_floor(self):
        self.assertEqual(self.position(kind='put', strike='29000')['im_usdt'], 2350)
        self.assertEqual(self.position(strike='90000', mark='100000')['im_usdt'], 103060)

    def test_fee_cap_and_collateral_deficient_close(self):
        self.assertEqual(self.order(price='1')['fee_reserve_usdt'], Decimal('0.07'))
        self.assertEqual(self.order(action='buy_to_close', closing_position_size='2',
            closing_position_im='2000', account_position_im='2000', margin_balance='100')['order_im_usdt'], 309)

    def test_close_position_overflow_and_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            self.order(action='buy_to_close', closing_position_size='0.5', closing_position_im='2000',
                       account_position_im='2000', margin_balance='10000')
        with self.assertRaises(ValueError):
            self.order(action='sell_to_close')

    def test_invalid_option_input_rejected(self):
        for changes in ({'kind': 'straddle'}, {'index': 'NaN'}, {'mark': '-1'}, {'qty': 1.0},
                        {'rules': {**margin.BTC_REFERENCE, 'settlement': 'USDC'}},
                        {'rules': {**margin.BTC_REFERENCE, 'mm_factor': '0.2'}}):
            with self.assertRaises(ValueError):
                self.position(**changes)

    def test_official_linear_initial_margin_long_short(self):
        result = self.linear()
        self.assertEqual(result['im_base_usdt'], 2525)
        self.assertEqual(result['estimated_close_fee_usdt'], Decimal('12.375'))
        self.assertEqual(result['im_position_tab_usdt'], Decimal('2537.375'))
        self.assertEqual(self.linear(qty='-0.5')['im_position_tab_usdt'], Decimal('2540.125'))

    def test_official_tiered_mm_example_and_fee_display(self):
        tiers = [{'limit_usdt': str(cap), 'mmr': rate, 'max_leverage': lev, 'mm_deduction_usdt': str(deduction)}
                 for cap, rate, lev, deduction in [(100000, '0.02', '25', 0), (200000, '0.025', '20', 500),
                    (300000, '0.03', '16.67', 1500), (400000, '0.035', '14.29', 3000)]]
        result = self.linear(qty='-100', mark='4000', entry='4000', tiers=tiers)
        self.assertEqual(result['mm_base_usdt'], 11000)
        self.assertEqual(result['mm_position_tab_usdt'], 11242)
        self.assertEqual(self.linear(qty='50', mark='4000', entry='4000', tiers=tiers)['mm_base_usdt'], 4500)
        tiers[1]['mm_deduction_usdt'] = '499'
        with self.assertRaisesRegex(ValueError, 'DEDUCTION'):
            self.linear(tiers=tiers)

    def test_risk_scope_and_leverage_rejected(self):
        for changes in ({'qty': '100'}, {'leverage': '26'}, {'leverage': '0.5'}, {'tiers': []}):
            with self.assertRaises(ValueError):
                self.linear(**changes)

    def test_cross_balance_excludes_option_value_and_does_not_assume_fx_one(self):
        result = self.balances()
        self.assertEqual(result['margin_balance_usd'], Decimal('9999.990'))
        self.assertEqual(result['equity_usd'], Decimal('9400.590'))
        self.assertFalse(result['historical_margin_qualified'])
        self.assertFalse(result['account_margin_rates_computed'])
        self.assertFalse(margin.BTC_REFERENCE['historical_applicability_qualified'])

    def test_cross_rejects_isolated_liabilities_other_assets_and_orders(self):
        for changes in ({'margin_mode': 'ISOLATED_MARGIN'}, {'margin_mode': 'PORTFOLIO_MARGIN'},
                        {'liabilities': '1'}, {'currency': 'USDC'}, {'wallet': '-1'}, {'other_assets': ['BTC']},
                        {'open_orders': [{}]}, {'collateral_ratio': '0.99'}, {'usdt_usd': '0'}):
            with self.assertRaises(ValueError):
                self.balances(**changes)

    def test_arithmetic_independent_of_ambient_precision(self):
        expected = self.linear(leverage='3')
        with localcontext() as ctx:
            ctx.prec = 4
            self.assertEqual(self.linear(leverage='3'), expected)


if __name__ == '__main__':
    unittest.main()
