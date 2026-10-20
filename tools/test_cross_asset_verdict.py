import copy
from decimal import Decimal
import unittest

import cross_asset_relative_strength as m
import cross_asset_verdict as v
import audit_cross_asset as audit


def fixture(horizon=7, daily_gain='2'):
    gain = Decimal(daily_gain)
    rows = [[m.INITIAL+i*m.DAY]+[str(Decimal(10000)+i*gain)]*2 for i in range(367)]
    cycles = [dict(start=(m.HALF+i*m.WEEK-m.INITIAL)//m.MINUTE, completed=True) for i in range(26)]
    book = dict(cash=rows[-1][1], q=['0']*3, queue=[], insufficient=None, stopped=None, cycles=cycles,
        active_weeks=list(range(25, 51)), gross='1000', fees='100', slippage='100',
        funding='68', turnover='200000', dd=.02, failed_entries=0,
        gross_integral=10000., net_integral=10., btc_integral=-100., max_gross=2600., max_abs_net=20.)
    return dict(start=m.INITIAL, end=m.END, minute=(m.END-m.INITIAL)//m.MINUTE,
                horizon=horizon, books=[copy.deepcopy(book), copy.deepcopy(book)], daily_cash=rows)


def paths(selected=14, baseline='2', chosen='3'):
    return {f'{mode}-{h}': fixture(h, baseline if h == 7 else chosen)
            for h in {7, selected} for mode in ('normal', 'delay')}


class Verdict(unittest.TestCase):
    def check(self, p, h):
        result = v.decide(p, h)
        audit.decision(p, h, result)
        return result

    def test_learned_positive_control(self):
        r = self.check(paths(), 14)
        self.assertEqual(r['status'], 'LEARNED_DEVELOPMENT_SUPPORTED')
        self.assertEqual(r['candidate'], 14)
        self.assertFalse(r['g4'])

    def test_fixed_positive_control(self):
        self.assertEqual(self.check(paths(7), 7)['status'], 'FIXED_DEVELOPMENT_SUPPORTED')

    def test_learning_increment_missing_falls_back(self):
        r = self.check(paths(chosen='2'), 14)
        self.assertEqual(r['candidate'], 7)
        self.assertIn('LEARNING_INCREMENT_HURDLE', r['checks']['14']['failures'])

    def test_fallback_requires_own_delay(self):
        p = paths(chosen='2')
        p['delay-7']['books'][1]['stopped'] = {'reason': 'RISK', 'minute': 1}
        r = self.check(p, 14)
        self.assertIsNone(r['candidate'])
        self.assertEqual(r['status'], 'NO_GO_RISK')

    def test_losing_absolute_net_rejected(self):
        r = self.check(paths(baseline='-.2', chosen='-.1'), 14)
        self.assertIsNone(r['candidate'])
        self.assertIn('ABSOLUTE_NET_HURDLE', r['failures'])

    def test_zero_activity_insufficient_not_profitable(self):
        p = paths(7)
        for b in p['normal-7']['books']:
            b['cycles'] = []; b['active_weeks'] = []
        self.assertEqual(self.check(p, 7)['status'], 'INSUFFICIENT_DIVERSITY')

    def test_residual_not_success(self):
        p = paths(7); p['normal-7']['books'][0]['q'][0] = '.001'
        self.assertEqual(self.check(p, 7)['status'], 'INPUT_OR_EXECUTION_INSUFFICIENT')

    def test_incomplete_calendar(self):
        p = paths(7); p['normal-7']['daily_cash'].pop()
        p['normal-7']['minute'] -= 1440
        self.assertEqual(self.check(p, 7)['status'], 'INPUT_OR_EXECUTION_INSUFFICIENT')

    def test_minimum_h2_pressure_exact(self):
        p = paths(7, baseline='.1')
        self.assertIn('ABSOLUTE_NET_HURDLE', self.check(p, 7)['failures'])

    def test_concentrated_profit_fails(self):
        p = paths(7)
        s = p['normal-7']
        for row in s['daily_cash']:
            if row[0] >= m.HALF+m.WEEK:
                row[2] = str(Decimal(row[2])+1000)
        s['books'][1]['cash'] = s['daily_cash'][-1][2]
        self.assertIn('CONCENTRATION_OR_HALF_STABILITY', self.check(p, 7)['failures'])

    def test_no_automatic_confirmation(self):
        r = self.check(paths(), 14)
        plan = v.confirmation_planning(r)
        self.assertFalse(plan['scheduled'])
        self.assertEqual(len(plan['scenarios']), 18)
        self.assertFalse(v.confirmation_planning({'candidate': None})['scheduled'])

    def test_mutated_verdict_or_metric_rejected(self):
        p = paths(); result = v.decide(p, 14)
        result['candidate'] = 7
        with self.assertRaises(ValueError):
            audit.decision(p, 14, result)
        result = v.decide(p, 14)
        result['checks']['14']['normal'][1]['weekly'][0] = '99999'
        with self.assertRaises(ValueError):
            audit.decision(p, 14, result)

    def test_all_selection_ineligible(self):
        p = {f'prefix-{h}': dict(books=[dict(stopped={'minute': 1})]*2) for h in (7, 14, 28)}
        self.assertEqual(self.check(p, None)['status'], 'NO_GO_RISK')


if __name__ == '__main__':
    unittest.main()
