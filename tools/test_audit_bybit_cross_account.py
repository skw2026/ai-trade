#!/usr/bin/env python3
"""Hand-derived synthetic account cases; never a simulated exchange witness."""
import copy
from decimal import Decimal, localcontext
import unittest

import audit_bybit_cross_account as account
import bybit_cross_margin_reference as ref
from test_audit_option_subaccount_ledger import CALL, PUT, PERP, START, EXPIRY, fixture as ledger_fixture


def rules():
    return {"schema_version": "bybit_cross_rules_v1", "basis": "synthetic_fixture", "source_id": "hand-example-v1",
        "historical_applicability_qualified": False, "option_factors": copy.deepcopy(ref.BTC_REFERENCE),
        "usdt_collateral_ratio": "1", "linear": {"leverage": "10", "open_fee_rate": "0.00025",
            "close_reserve_fee_rate": "0.00055", "tiers": [
                {"limit_usdt": "100000", "mmr": "0.005", "mm_deduction_usdt": "0", "max_leverage": "100"},
                {"limit_usdt": "200000", "mmr": "0.01", "mm_deduction_usdt": "500", "max_leverage": "50"}]}}


def quote(value, now=START):
    return {"value": str(value), "ts_ms": now, "source_kind": "synthetic"}


def fixture():
    return {"schema_version": account.SCHEMA, "evidence_kind": "synthetic_fixture", "scope_id": "offline_subaccount",
        "margin_mode": "REGULAR_MARGIN", "currency": "USDT", "ts_ms": START, "price_max_age_ms": 1000,
        "wallet_usdt": "1019.4", "liabilities_usdt": "0", "other_assets": [],
        "instruments": ledger_fixture()["instruments"], "index": quote(80000), "usdt_usd": quote('0.999'),
        "marks": {CALL: quote(1000), PUT: quote(1000), PERP: quote(80000)},
        "positions": {CALL: {"qty": "-0.01", "entry": "1000"}, PUT: {"qty": "-0.01", "entry": "1000"},
                      PERP: {"qty": "0.01", "entry": "80000"}}, "orders": []}


def order(symbol=PERP, **changes):
    return {"id": "test-order", "symbol": symbol, "side": "Buy", "intent": "open",
            "remaining_qty": "0.01", "limit_price": "81000" if symbol == PERP else "1000", "created_ms": START, **changes}


class CrossAccountTest(unittest.TestCase):
    def test_hand_calculated_three_leg_account(self):
        out = account.calculate(fixture(), rules())
        expected = {"position_initial_margin_usdt": '260.396', "position_maintenance_margin_usdt": '75.596',
                    "total_equity_usd": '998.4006', "total_margin_balance_usd": '1018.3806',
                    "total_initial_margin_usd": '260.135604', "total_maintenance_margin_usd": '75.520404',
                    "total_available_balance_usd": '758.244996'}
        for key, value in expected.items():
            self.assertEqual(Decimal(out[key]), Decimal(value), key)
        self.assertEqual(out['status'], 'REFERENCE_CALCULATED')
        self.assertFalse(out['c2_qualified'])
        self.assertFalse(out['exchange_account_reconciled'])
        self.assertFalse(any(out['authorities'].values()))

    def test_linear_open_order_loss_and_separate_fee_rates(self):
        state = fixture()
        state['orders'] = [order()]
        out = account.calculate(state, rules())
        self.assertEqual(Decimal(out['order_initial_margin_usdt']), Decimal('81.60345'))
        self.assertEqual(Decimal(out['order_maintenance_margin_usdt']), Decimal('4.45095'))
        self.assertEqual(Decimal(out['order_loss_usd']), Decimal('-9.990'))
        self.assertEqual(Decimal(out['margin_rate_denominator_usd']), Decimal('1008.3906'))
        self.assertEqual(Decimal(out['total_available_balance_usd']) - Decimal(out['risk_headroom_after_order_loss_usd']), Decimal('9.99'))

    def test_official_linear_order_cost_example(self):
        state, policy = fixture(), rules()
        state['orders'] = [order(remaining_qty='1', limit_price='50000')]
        policy['linear']['open_fee_rate'] = '0.00055'
        self.assertEqual(Decimal(account.calculate(state, policy)['order_initial_margin_usdt']), Decimal('5052.25'))

    def test_option_sell_open_reserves_without_crediting_unfilled_premium(self):
        state = fixture()
        state['orders'] = [order(CALL, side='Sell')]
        out = account.calculate(state, rules())
        self.assertEqual(Decimal(out['order_initial_margin_usdt']), Decimal('80.24'))
        self.assertEqual(Decimal(out['wallet_usdt']), Decimal('1019.4'))
        self.assertEqual(Decimal(out['order_maintenance_margin_usdt']), 0)

    def test_multiple_buy_to_close_orders_share_existing_position(self):
        state = fixture()
        state['positions'][CALL]['qty'] = '-0.02'
        state['orders'] = [order(CALL, intent='reduce', remaining_qty='0.01', id='a'),
                           order(CALL, intent='reduce', remaining_qty='0.01', id='b')]
        self.assertEqual(Decimal(account.calculate(state, rules())['order_initial_margin_usdt']), 0)
        state['orders'][1]['remaining_qty'] = '0.02'
        with self.assertRaisesRegex(ValueError, 'OVERRESERVE'):
            account.calculate(state, rules())

    def test_collateral_deficient_close_uses_shared_position_im(self):
        state = fixture()
        state['wallet_usdt'] = '10'
        state['orders'] = [order(CALL, intent='reduce')]
        out = account.calculate(state, rules())
        with localcontext() as ctx:
            ctx.prec = 100
            expected = Decimal('10.24') - Decimal(10) / Decimal('260.396') * Decimal(90)
        self.assertEqual(Decimal(out['order_initial_margin_usdt']), expected)

    def test_buy_open_long_does_not_count_unfilled_option_as_asset(self):
        state = fixture()
        del state['positions'][CALL]
        state['orders'] = [order(CALL)]
        out = account.calculate(state, rules())
        self.assertEqual(Decimal(out['order_initial_margin_usdt']), Decimal('10.24'))
        self.assertEqual(Decimal(out['option_value_usdt']), -10)

    def test_long_option_value_does_not_increase_margin_balance(self):
        state = fixture()
        state['positions'][CALL]['qty'] = '0.01'
        out = account.calculate(state, rules())
        self.assertEqual(Decimal(out['total_margin_balance_usd']), Decimal('1018.3806'))
        self.assertEqual(Decimal(out['positions'][CALL]['im_usdt']), 0)

    def test_linear_reduce_order_has_order_loss_but_no_double_reserve(self):
        state = fixture()
        state['orders'] = [order(side='Sell', intent='reduce', limit_price='79000')]
        out = account.calculate(state, rules())
        self.assertEqual(Decimal(out['order_initial_margin_usdt']), 0)
        self.assertEqual(Decimal(out['order_loss_usd']), Decimal('-9.99'))

    def test_effective_tier_includes_active_order_notional(self):
        state = fixture()
        state['orders'] = [order(remaining_qty='1.5')]
        out = account.calculate(state, rules())
        # (800 position + 121500 order) is tier 2; only order MM uses its rate.
        self.assertEqual(Decimal(out['order_maintenance_margin_usdt']), Decimal('1275.1425'))

    def test_tier_deduction_and_leverage_rejected(self):
        state, policy = fixture(), rules()
        policy['linear']['tiers'][1]['mm_deduction_usdt'] = '499'
        with self.assertRaisesRegex(ValueError, 'DEDUCTION'):
            account.calculate(state, policy)
        policy = rules()
        policy['linear']['leverage'] = '60'
        state['orders'] = [order(remaining_qty='1.5')]
        with self.assertRaisesRegex(ValueError, 'LEVERAGE'):
            account.calculate(state, policy)

    def test_mixed_or_opposing_orders_are_not_silently_netted(self):
        state = fixture()
        state['orders'] = [order(id='a'), order(id='b', side='Sell', intent='reduce')]
        with self.assertRaisesRegex(ValueError, 'MIXED'):
            account.calculate(state, rules())
        del state['positions'][PERP]
        state['orders'][1]['intent'] = 'open'
        with self.assertRaisesRegex(ValueError, 'OPPOSING'):
            account.calculate(state, rules())

    def test_zero_or_negative_denominator_does_not_produce_fake_zero_rates(self):
        for cash in ('0', '-1'):
            state = fixture()
            state['wallet_usdt'] = cash
            out = account.calculate(state, rules())
            self.assertIsNone(out['account_im_rate'])
            self.assertIsNone(out['account_mm_rate'])
            self.assertEqual(out['status'], 'REFERENCE_RISK_REVIEW')

    def test_time_source_coverage_and_instrument_rejection(self):
        mutations = [lambda s: s['index'].update(ts_ms=START + 1),
                     lambda s: s['usdt_usd'].update(ts_ms=START - 1001),
                     lambda s: s['marks'].pop(PERP),
                     lambda s: s.update(liabilities_usdt='1'),
                     lambda s: s.update(margin_mode='ISOLATED_MARGIN'),
                     lambda s: s['instruments'][CALL].update(expiry_ts_ms=EXPIRY + 86400000),
                     lambda s: s['positions'][CALL].update(qty='-0.00001'),
                     lambda s: s.update(unknown=True)]
        for mutate in mutations:
            state = fixture()
            mutate(state)
            with self.assertRaises(ValueError):
                account.calculate(state, rules())

    def test_false_historical_qualification_and_missing_rates_rejected(self):
        policy = rules()
        policy['historical_applicability_qualified'] = True
        with self.assertRaises(ValueError): account.calculate(fixture(), policy)
        policy = rules()
        del policy['linear']['close_reserve_fee_rate']
        with self.assertRaises(ValueError): account.calculate(fixture(), policy)

    def test_partial_fill_cancel_lifecycle_and_rollback_on_error(self):
        book, meta = account.OrderBook(), fixture()['instruments']
        book.apply({'type': 'PLACE', 'ts_ms': START, 'order': order()}, meta)
        book.apply({'type': 'FILL', 'ts_ms': START + 1, 'id': 'test-order', 'qty': '0.004', 'price': '80900'}, meta)
        self.assertEqual(Decimal(book.active['test-order']['remaining_qty']), Decimal('0.006'))
        previous = copy.deepcopy(book.active)
        with self.assertRaises(ValueError):
            book.apply({'type': 'FILL', 'ts_ms': START + 2, 'id': 'test-order', 'qty': '0.007', 'price': '80900'}, meta)
        self.assertEqual(book.active, previous)
        self.assertEqual(book.last_ms, START + 1)
        book.apply({'type': 'CANCEL', 'ts_ms': START + 2, 'id': 'test-order'}, meta)
        self.assertEqual(book.active, {})
        with self.assertRaises(ValueError):
            book.apply({'type': 'PLACE', 'ts_ms': START + 2, 'order': order(created_ms=START + 2)}, meta)

    def test_fill_limit_and_time_causality(self):
        book, meta = account.OrderBook(), fixture()['instruments']
        book.apply({'type': 'PLACE', 'ts_ms': START, 'order': order()}, meta)
        for event in ({'type': 'FILL', 'ts_ms': START - 1, 'id': 'test-order', 'qty': '0.01', 'price': '80900'},
                      {'type': 'FILL', 'ts_ms': START + 1, 'id': 'test-order', 'qty': '0.01', 'price': '81001'},
                      {'type': 'CANCEL', 'ts_ms': START + 1, 'id': 'missing'}):
            with self.assertRaises(ValueError): book.apply(event, meta)
        book.apply({'type': 'FILL', 'ts_ms': START + 2, 'id': 'test-order', 'qty': '0.01', 'price': '81000'}, meta)
        self.assertEqual(book.active, {})

    def test_ambient_precision_independent_and_input_not_mutated(self):
        state, policy = fixture(), rules()
        prior = copy.deepcopy(state)
        expected = account.calculate(state, policy)
        with localcontext() as ctx:
            ctx.prec = 4
            self.assertEqual(account.calculate(state, policy), expected)
        self.assertEqual(state, prior)


if __name__ == '__main__':
    unittest.main()
