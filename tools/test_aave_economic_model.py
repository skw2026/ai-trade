import copy
import itertools
import json
import math
from pathlib import Path
import random
import unittest

import aave_economic_model as m
import audit_aave_economic_screen as audit
from run_bounded_learning import hac_lower


def contract():
    return json.loads(Path('docs/plans/2026-10-13-aave-economic-screen.contract.json').read_text())


def fixture():
    start = 120000
    rows = [dict(group=0, entry_ms=start, exit_ms=start+300000, candidate=True, baseline=False)]
    data = {k: {t: ['100']*4 for t in range(start-60000, start+360000, 60000)} for k in ('trade', 'mark')}
    data['funding'] = {}
    return rows, data, contract()


class CashRiskTests(unittest.TestCase):
    def compare(self, rows, data, c, mult=1, arm='candidate'):
        value = m.simulate(rows, data, c, mult, arm)
        exact = audit.exact_path(rows, data, c, mult, arm)
        audit.compare(value, exact)
        return value

    def test_two_sided_costs_and_stress(self):
        rows, data, c = fixture()
        for mult, loss in ((1, 3.25), (2, 6.5)):
            with self.subTest(mult=mult):
                result = self.compare(rows, data, c, mult)
                self.assertAlmostEqual(result['cash_lo'], 10000-loss, places=8)
                self.assertEqual(result['fills'], 2)
                self.assertTrue(result['complete'])

    def test_short_positive_funding_sign(self):
        rows, data, c = fixture()
        data['funding'][240000] = '0.001'
        result = self.compare(rows, data, c)
        self.assertAlmostEqual(result['funding_lo'], 2.5)
        self.assertEqual(result['funding_lo'], result['funding_hi'])
        self.assertAlmostEqual(result['cash_lo'], 9999.25)

    def test_boundary_funding_includes_zero_and_both_mark_minutes(self):
        rows, data, c = fixture()
        data['mark'][60000] = ['100', '110', '90', '100']
        for rate, expected in [('0.001', (0, 2.75)), ('-0.001', (-2.75, 0))]:
            data['funding'] = {120000: rate}
            result = self.compare(rows, data, c)
            self.assertAlmostEqual(result['funding_lo'], expected[0])
            self.assertAlmostEqual(result['funding_hi'], expected[1])

    def test_after_exit_within_five_seconds_is_optional(self):
        rows, data, c = fixture()
        data['funding'][424000] = '-0.001'
        result = self.compare(rows, data, c)
        self.assertAlmostEqual(result['funding_lo'], -2.5)
        self.assertEqual(result['funding_hi'], 0)
        data['funding'] = {426000: '-0.001'}
        result = self.compare(rows, data, c)
        self.assertEqual(result['funding_lo'], 0)

    def test_possible_intrabar_drawdown_stops_without_invented_close(self):
        rows, data, c = fixture()
        data['mark'][120000] = ['100', '120', '80', '80']
        result = self.compare(rows, data, c)
        self.assertEqual(result['stop']['decision'], 'INSUFFICIENT_RISK_ORDER')
        self.assertEqual(result['fills'], 1)
        self.assertNotEqual(result['quantity'], 0)
        self.assertIsNone(result['journal'][0]['net_lo'])

    def test_known_close_after_peak_makes_breach_definite(self):
        rows, data, c = fixture()
        data['mark'][120000] = ['100', '120', '80', '120']
        result = self.compare(rows, data, c)
        self.assertEqual(result['stop']['decision'], 'REJECT_REFERENCE_RISK')

    def test_exit_fee_risk_is_checked(self):
        rows, data, c = fixture()
        c['fee_bps_per_side'] = 2000
        result = self.compare(rows, data, c)
        self.assertEqual(result['stop']['phase'], 'exit_cash')
        self.assertEqual(result['fills'], 2)

    def test_quantity_zero_is_not_forced_to_one_lot(self):
        rows, data, c = fixture()
        data['trade'] = {t: ['1000000000']*4 for t in data['trade']}
        data['mark'] = copy.deepcopy(data['trade'])
        result = self.compare(rows, data, c)
        self.assertEqual(result['fills'], 0)
        self.assertTrue(result['journal'][0]['zero_rounded'])

    def test_flat_unknown_does_not_accrue_funding(self):
        rows, data, c = fixture()
        rows[0]['candidate'] = False
        data['funding'][240000] = '0.1'
        result = self.compare(rows, data, c)
        self.assertEqual(result['cash_lo'], 10000)
        self.assertEqual(result['fills'], 0)

    def test_non_compounding_and_cash_identity(self):
        rows, data, c = fixture()
        second = dict(rows[0], group=1, entry_ms=600000, exit_ms=900000)
        rows.append(second)
        for kind in ('trade', 'mark'):
            data[kind].update({t: ['100']*4 for t in range(540000, 960000, 60000)})
        result = self.compare(rows, data, c)
        self.assertEqual(result['journal'][0]['quantity'], result['journal'][1]['quantity'])
        self.assertAlmostEqual(result['cash_lo'], 9993.5)


class CausalAndStatisticsTests(unittest.TestCase):
    def test_only_completed_closes_before_event_are_used(self):
        first = 7201
        event = dict(first=first, entry=7320, exit=7620, exclusion=None, candidate_opportunity=True, signal='POSITIVE')
        data = dict(trade={t: ['100']*4 for t in range(0, 8000000, 60000)})
        data['trade'][7140000] = ['100', '100', '90', '90']
        # Forming minute's eventual close and post-event prices must not alter baseline.
        one = m.event_schedule([event], data)
        data['trade'][7200000] = ['200', '300', '1', '300']
        two = m.event_schedule([event], data)
        self.assertEqual(one, two)
        self.assertTrue(one[0]['baseline'])
        audit.compare(one, audit.exact_schedule([event], data))

    def test_exact_minute_event_uses_strictly_earlier_close(self):
        event = dict(first=7200, entry=7320, exit=7620, exclusion=None,
                     candidate_opportunity=False, signal='UNKNOWN')
        data = dict(trade={t: ['100']*4 for t in range(0, 8000000, 60000)})
        data['trade'][7140000] = ['100', '200', '1', '1']
        actual = m.event_schedule([event], data)
        audit.compare(actual, audit.exact_schedule([event], data))
        self.assertFalse(actual[0]['baseline'])
        self.assertFalse(actual[0]['candidate'])

    def test_independent_schedule_detects_modified_signal(self):
        event = dict(first=7201, entry=7320, exit=7620, exclusion=None,
                     candidate_opportunity=True, signal='POSITIVE')
        data = dict(trade={t: ['100']*4 for t in range(0, 8000000, 60000)})
        actual = m.event_schedule([event], data)
        actual[0]['baseline'] = True
        with self.assertRaisesRegex(ValueError, 'AUDIT_VALUE_MISMATCH'):
            audit.compare(actual, audit.exact_schedule([event], data))

    def test_point_intervals_reduce_to_original_hac(self):
        values = [math.sin(i)+0.1*i for i in range(52)]
        self.assertAlmostEqual(m.hac_box([(v, v) for v in values])['lower'], hac_lower(values), places=12)

    def test_interval_bound_dominates_enumerated_corners(self):
        mids = [0.01*i+math.cos(i) for i in range(26)]
        intervals = [(v-.3 if i < 4 else v, v+.3 if i < 4 else v) for i, v in enumerate(mids)]
        bound = m.hac_box(intervals)['lower']
        for signs in itertools.product((0, 1), repeat=4):
            point = [intervals[i][signs[i]] if i < 4 else mids[i] for i in range(26)]
            self.assertLessEqual(bound, hac_lower(point)+1e-12)
        audit.compare(m.hac_box(intervals), audit.exact_hac(intervals))

    def test_random_boxes_and_invalid_bounds(self):
        rng = random.Random(20261013)
        for _ in range(50):
            intervals = [(v := rng.uniform(-2, 2), v+rng.random()) for _ in range(52)]
            bound = m.hac_box(intervals)['lower']
            point = [rng.uniform(l, u) for l, u in intervals]
            self.assertLessEqual(bound, hac_lower(point)+1e-12)
            audit.compare(m.hac_box(intervals), audit.exact_hac(intervals))
        for intervals in ([(0, 1)]*25, [(1, 0)]*52, [(0, math.inf)]*52):
            with self.assertRaises(ValueError): m.hac_box(intervals)

    def test_decision_positive_and_negative_are_not_g4(self):
        c = contract()
        for pnl, expected in ((1, 'RETAIN_DEVELOPMENT_ONLY_NOT_CONFIRMED'), (-1, 'REJECT_NET_EDGE')):
            paths = {}
            for arm in ('candidate', 'baseline'):
                amount = pnl if arm == 'candidate' else 0
                journal = [dict(group=i, entry_ms=c['evaluation_start_ms']+i*604800000,
                                net_lo=amount, net_hi=amount) for i in range(52)]
                for cost in ('base', 'stress'):
                    paths[arm+'_'+cost] = dict(complete=True, stop=None, cash_lo=10000+52*amount,
                                             cash_hi=10000+52*amount, journal=journal)
            result = m.assess(paths, c)
            self.assertEqual(result['decision'], expected)
            verdict, statistics = audit.decision(paths, c)
            self.assertEqual(verdict, expected)
            audit.compare(result, statistics)
            self.assertFalse(result['qualification'])
            self.assertEqual(result['g4'], 'NOT_ACHIEVED')


if __name__ == '__main__': unittest.main()
