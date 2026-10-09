"""Synthetic execution, independent Decimal audit and fixed decision tests."""
import copy
import unittest

import eth_queue_development_inputs as inp
import eth_queue_development_screen as s
from audit_eth_queue_development import exact_path, compare, audit_decision


def fixture(active=True, days=1):
    c = dict(initial_capital=10000, fee_bps_per_side=5.5, slippage_bps_per_side=1,
             quantity_step='0.01', entry_notional_fraction=.25, maximum_drawdown=.08,
             maximum_notional_fraction=.5, split_utc='1970-01-02T00:00:00Z')
    rows = [dict(entry_ms=i*inp.DAY, exit_ms=(i+1)*inp.DAY, active=active, group=0 if active else None) for i in range(days)]
    prices = {t: ['100', '100', '100', '100'] for t in range(0, (days*24+1)*inp.HOUR, inp.HOUR)}
    data = dict(trade=copy.deepcopy(prices), mark=copy.deepcopy(prices), funding={})
    return rows, data, c


class QueueEconomics(unittest.TestCase):
    def run_case(self, rows, data, c, multiplier=1, control=False):
        got = s.simulate(rows, data, c, multiplier, control)
        compare(got, exact_path(rows, data, c, multiplier, control))
        return got

    def test_flat_price_costs_and_exact_cash(self):
        rows, data, c = fixture()
        r = self.run_case(rows, data, c)
        self.assertTrue(r['complete'])
        self.assertLess(r['cash_lo'], 10000)
        self.assertGreater(r['fees'], 0)
        self.assertGreater(r['slippage'], 0)
        self.assertEqual(r['gross'], 0)
        self.assertEqual(r['fills'], 2)

    def test_no_trade_is_zero_not_learning(self):
        r = self.run_case(*fixture(False))
        self.assertEqual(r['cash_lo'], 10000)
        self.assertEqual(r['fills'], 0)

    def test_fixed_control_trades_on_flat_signal(self):
        r = self.run_case(*fixture(False), control=True)
        self.assertEqual(r['active_days'], 1)
        self.assertLess(r['cash_lo'], 10000)

    def test_daily_churn_not_hidden(self):
        r = self.run_case(*fixture(True, 2))
        self.assertEqual(r['fills'], 4)
        self.assertEqual(r['active_days'], 2)

    def test_stress_cost_is_higher(self):
        rows, data, c = fixture()
        a = self.run_case(rows, data, c)
        b = self.run_case(rows, data, c, 2)
        self.assertLess(b['cash_lo'], a['cash_lo'])

    def test_quantity_rounded_down_against_slipped_notional(self):
        rows, data, c = fixture()
        c['quantity_step'] = '10'
        self.assertEqual(s.quantity('100', c, 1), -20)
        self.run_case(rows, data, c)

    def test_short_profit_and_gross_decomposition(self):
        rows, data, c = fixture()
        data['trade'][inp.DAY] = ['99']*4
        data['mark'][inp.DAY] = ['99']*4
        r = self.run_case(rows, data, c)
        self.assertGreater(r['gross'], 0)
        self.assertAlmostEqual(r['cash_lo']-10000, r['gross']-r['fees']-r['slippage']+r['funding_lo'])

    def test_positive_funding_short_receives_bounds(self):
        rows, data, c = fixture()
        data['funding'][8*inp.HOUR] = '.001'
        data['mark'][8*inp.HOUR] = ['100','101','99','100']
        r = self.run_case(rows, data, c)
        self.assertGreater(r['funding_lo'], 0)
        self.assertGreater(r['funding_hi'], r['funding_lo'])

    def test_negative_funding_short_pays_bounds(self):
        rows, data, c = fixture()
        data['funding'][8*inp.HOUR] = '-.001'
        data['mark'][8*inp.HOUR] = ['100','101','99','100']
        r = self.run_case(rows, data, c)
        self.assertLess(r['funding_hi'], 0)
        self.assertLess(r['funding_lo'], r['funding_hi'])

    def test_definite_short_risk_no_stop_fill_or_future_read(self):
        rows, data, c = fixture()
        data['mark'][0] = ['100','140','100','140']
        del data['trade'][inp.DAY]
        r = self.run_case(rows, data, c)
        self.assertEqual(r['stop']['decision'], 'REJECT_REFERENCE_RISK')
        self.assertTrue(r['stop']['no_stop_fill_invented'])
        self.assertEqual(r['fills'], 1)
        self.assertIsNone(r['full_window_return_lo'])

    def test_unknown_intrabar_order_not_pass(self):
        rows, data, c = fixture()
        data['mark'][0] = ['100','117','80','80']
        r = self.run_case(rows, data, c)
        self.assertEqual(r['stop']['decision'], 'INSUFFICIENT_RISK_ORDER')
        self.assertLess(r['drawdown_lo'], .08)
        self.assertGreater(r['drawdown_hi'], .08)

    def test_favorable_price_precedes_close_risk(self):
        rows, data, c = fixture()
        data['mark'][0] = ['100','117','80','117']
        r = self.run_case(rows, data, c)
        self.assertEqual(r['stop']['decision'], 'REJECT_REFERENCE_RISK')

    def test_exit_gap_mark_stops_before_fake_fill(self):
        rows, data, c = fixture()
        data['mark'][inp.DAY] = ['140']*4
        r = self.run_case(rows, data, c)
        self.assertEqual(r['stop']['phase'], 'exit_mark')
        self.assertEqual(r['fills'], 1)

    def test_funding_can_stop_path(self):
        rows, data, c = fixture()
        data['funding'][8*inp.HOUR] = '-.5'
        r = self.run_case(rows, data, c)
        self.assertEqual(r['stop']['phase'], 'funding')

    def test_conservative_notional_limit(self):
        rows, data, c = fixture()
        c['maximum_notional_fraction'] = .2
        r = self.run_case(rows, data, c)
        self.assertEqual(r['stop']['decision'], 'REJECT_REFERENCE_RISK')

    def test_negative_economics_is_a_valid_reject_not_qualification(self):
        rows, data, c = fixture()
        paths = {prefix+cost: self.run_case(rows, data, c, mult, control)
                 for prefix, control in (('candidate_', False), ('control_', True))
                 for cost, mult in (('base', 1), ('stress', 2))}
        verdict = s.assess(paths, c)
        self.assertEqual(verdict['decision'], 'REJECT_NET_EDGE')
        self.assertFalse(verdict['qualification'])
        self.assertFalse(verdict['independent_confirmation_admitted'])

    def test_audit_rejects_cash_tampering(self):
        rows, data, c = fixture()
        r = s.simulate(rows, data, c, 1)
        r['cash_lo'] += 1
        with self.assertRaises(ValueError): compare(r, exact_path(rows, data, c, 1))

    def test_positive_and_concentration_decisions_have_independent_audit(self):
        c = fixture()[2]
        c['split_utc'] = '1970-04-01T00:00:00Z'
        candidate = dict(complete=True, stop=None, cash_lo=10280, cash_hi=10280,
            daily=[dict(entry_ms=t*inp.DAY, group=t//28, net_lo=1, net_hi=1) for t in range(280)])
        control = dict(complete=True, stop=None, cash_lo=10000, cash_hi=10000,
            daily=[dict(entry_ms=t*inp.DAY, group=None, net_lo=0, net_hi=0) for t in range(280)])
        paths = dict(candidate_base=copy.deepcopy(candidate), candidate_stress=copy.deepcopy(candidate),
                     control_base=copy.deepcopy(control), control_stress=copy.deepcopy(control))
        result = dict(**s.assess(paths, c), paths=paths)
        self.assertEqual(audit_decision(result, c), 'RETAIN_DEVELOPMENT_ONLY_NOT_CONFIRMED')
        for name in ('candidate_base', 'candidate_stress'):
            for row in paths[name]['daily']: row['group'] = 0
        result = dict(**s.assess(paths, c), paths=paths)
        self.assertEqual(audit_decision(result, c), 'INSUFFICIENT_CONCENTRATION')

    def test_stopped_baseline_cannot_supply_whole_window_comparison(self):
        c = fixture()[2]
        candidate = dict(complete=True, stop=None, cash_lo=10100)
        control = dict(complete=False, stop={'decision': 'REJECT_REFERENCE_RISK'}, cash_hi=9000)
        paths = dict(candidate_base=candidate, candidate_stress=candidate,
                     control_base=control, control_stress=control)
        result = dict(**s.assess(paths, c), paths=paths)
        self.assertEqual(audit_decision(result, c), 'INSUFFICIENT_CONTROL_PATH')


if __name__ == '__main__': unittest.main()
