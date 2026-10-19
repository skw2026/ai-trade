"""Synthetic endpoints only; no historical input, model fitting or network."""
import copy
import datetime as dt
import unittest
from unittest import mock

import open_market_selective_verdict as m


def week(t):
    iso = dt.datetime.fromtimestamp(t/1000, dt.timezone.utc).isocalendar()
    return '%04d-W%02d' % (iso.year, iso.week)


def path(year, net, latency=1):
    start, end = m.PERIODS[year]
    n = (end-start)//m.DAY
    count = 30 if year == '2025' else 10
    indices = [i*(n-1)//(count-1) for i in range(count)]
    cycles = [dict(start=start+i*m.DAY+m.STEP, end=start+(i+1)*m.DAY-2*m.STEP,
                   net=net/count, complete=True) for i in indices]
    books = []
    for stress in (False, True):
        books.append(dict(stress=stress, cash=10000.+net, quantity=0., net=float(net),
                          stopped=None, insufficient=None, cycles=copy.deepcopy(cycles),
                          active_weeks=sorted({week(c['start']) for c in cycles}),
                          days=[dict(t=start+i*m.DAY, equity=10000.+net*i/n)
                                for i in range(1, n+1)]))
    return dict(start=start, end=end, latency=latency, bars=n*m.DAY//m.STEP,
                capital=10000, notional=2500, horizon='daily_cycle_2345_request',
                terminal_reserve_bars=6, initially_stopped=False, books=books)


def fixture():
    return {'2021-control': path('2021', 20.), '2021-veto': path('2021', 40.),
            '2025-control': path('2025', 75.), '2025-veto': path('2025', 100.),
            '2025-delay': path('2025', 110., 2)}


def replace_net(paths, key, net, side=None):
    replacement = path(key[:4], net, paths[key]['latency'])
    if side is None:
        paths[key]['books'] = replacement['books']
    else:
        paths[key]['books'][side] = replacement['books'][side]


class SelectiveVerdictTests(unittest.TestCase):
    def decide(self, paths, expected):
        result = m.classify(paths)
        self.assertEqual(result['status'], expected)
        audited = m.audit_verdict(paths, result)
        self.assertEqual(audited['decision_status'], expected)
        return result

    def reject(self, paths):
        result = m.classify(fixture())
        with self.assertRaises(ValueError):
            m.classify(paths)
        with self.assertRaises(ValueError):
            m.audit_verdict(paths, result)

    def test_exact_effect_thresholds_and_locks(self):
        result = self.decide(fixture(), 'DEVELOPMENT_SUPPORTED')
        self.assertEqual(result['pressure_increments'], {'2021': 20., '2025': 25.})
        self.assertEqual(result['capacity']['complete_cycles'], 30)
        self.assertFalse(result['independent_confirmation'])
        self.assertFalse(result['online_learning_proven'])
        self.assertFalse(result['g4'])
        self.assertEqual(result['qualified_candidates'], 0)
        self.assertTrue(result['demo_opening_lock_unchanged'])

    def test_negative_or_zero_in_either_candidate_book(self):
        for key in ('2021-veto', '2025-veto'):
            for side in (0, 1):
                for value in (-1., 0.):
                    with self.subTest(key=key, side=side, value=value):
                        values = fixture()
                        replace_net(values, key, value, side)
                        result = self.decide(values, 'NET_OR_RISK_UNSUPPORTED')
                        self.assertIn(key[:4]+'_VETO_NET_NONPOSITIVE', result['reasons'])

    def test_less_loss_is_not_economic_support(self):
        values = fixture()
        replace_net(values, '2025-control', -200.)
        replace_net(values, '2025-veto', -100.)
        result = self.decide(values, 'NET_OR_RISK_UNSUPPORTED')
        self.assertEqual(result['pressure_increments']['2025'], 100.)

    def test_all_cash_does_not_qualify(self):
        values = fixture()
        for key in m.KEYS:
            replace_net(values, key, 0.)
            for book in values[key]['books']:
                book['cycles'] = []
                book['active_weeks'] = []
        result = self.decide(values, 'NET_OR_RISK_UNSUPPORTED')
        self.assertFalse(result['capacity']['supported'])

    def test_absolute_effect_below_100_fails(self):
        values = fixture()
        replace_net(values, '2025-veto', 99.99999999999999)
        replace_net(values, '2025-control', 0.)
        result = self.decide(values, 'NET_OR_RISK_UNSUPPORTED')
        self.assertIn('MINIMUM_ABSOLUTE_EFFECT', result['reasons'])

    def test_2021_increment_strict_and_2025_increment_inclusive(self):
        values = fixture()
        replace_net(values, '2021-control', 40.)
        result = self.decide(values, 'INCREMENT_UNSUPPORTED')
        self.assertIn('2021_INCREMENT_NONPOSITIVE', result['reasons'])
        values = fixture()
        replace_net(values, '2025-control', 75.00000000000001)
        result = self.decide(values, 'INCREMENT_UNSUPPORTED')
        self.assertIn('2025_MINIMUM_INCREMENT', result['reasons'])

    def test_losing_or_halted_controls_are_allowed_if_complete(self):
        values = fixture()
        for key in ('2021-control', '2025-control'):
            replace_net(values, key, -400.)
            for book in values[key]['books']:
                book['stopped'] = dict(t=values[key]['start']+m.DAY, reason='RISK')
        self.decide(values, 'DEVELOPMENT_SUPPORTED')

    def test_candidate_or_delay_risk_has_absolute_priority(self):
        for key in ('2021-veto', '2025-veto', '2025-delay'):
            for side in (0, 1):
                with self.subTest(key=key, side=side):
                    values = fixture()
                    values[key]['books'][side]['stopped'] = dict(t=values[key]['start']+m.DAY,
                                                               reason='RISK')
                    self.decide(values, 'NET_OR_RISK_UNSUPPORTED')

    def test_capacity_exact_boundaries_and_incomplete_cycles(self):
        values = fixture()
        stress = values['2025-veto']['books'][1]
        stress['active_weeks'] = stress['active_weeks'][:12]
        self.decide(values, 'DEVELOPMENT_SUPPORTED')
        stress['active_weeks'] = stress['active_weeks'][:11]
        self.decide(values, 'FRAGILE_DEVELOPMENT_SIGNAL')
        values = fixture()
        values['2025-veto']['books'][1]['cycles'][0]['complete'] = False
        result = self.decide(values, 'FRAGILE_DEVELOPMENT_SIGNAL')
        self.assertEqual(result['capacity']['complete_cycles'], 29)

    def test_partial_unwind_is_included_in_best_cycle_concentration(self):
        values = fixture()
        stress = values['2025-veto']['books'][1]
        t = values['2025-veto']['start']+m.DAY
        stress['cycles'].insert(1, dict(start=t+m.STEP, end=t+2*m.STEP,
                                       net=100., complete=False))
        result = self.decide(values, 'FRAGILE_DEVELOPMENT_SIGNAL')
        self.assertEqual(result['capacity']['complete_cycles'], 30)
        self.assertEqual(result['pressure_without_best_cycle'], 0.)
        self.assertIn('BEST_CYCLE_CONCENTRATION', result['reasons'])

    def test_half_year_boundary_and_zero_half(self):
        values = fixture()
        stress = values['2025-veto']['books'][1]
        for day in stress['days']:
            day['equity'] = 10040. if day['t'] <= m.MIDPOINT else 10100.
        result = self.decide(values, 'DEVELOPMENT_SUPPORTED')
        self.assertEqual(result['half_year_pressure_net'], [40., 60.])
        for day in stress['days']:
            day['equity'] = 10000. if day['t'] <= m.MIDPOINT else 10100.
        result = self.decide(values, 'FRAGILE_DEVELOPMENT_SIGNAL')
        self.assertIn('HALF_YEAR_SIGN', result['reasons'])

    def test_delay_nonpositive_is_fragile_without_a_stop(self):
        for net in (-10., 0.):
            values = fixture()
            replace_net(values, '2025-delay', net)
            result = self.decide(values, 'FRAGILE_DEVELOPMENT_SIGNAL')
            self.assertIn('DELAY_NET_NONPOSITIVE', result['reasons'])

    def test_incomplete_comparison_is_prior_to_economic_failure(self):
        values = fixture()
        replace_net(values, '2025-veto', -1.)
        values['2021-control']['books'][0]['insufficient'] = 'EXIT_RESIDUAL'
        values['2021-control']['books'][0]['quantity'] = .001
        result = self.decide(values, 'COMPARISON_EVIDENCE_INCOMPLETE')
        self.assertEqual(result['reasons'], ['INCOMPLETE_2021-control'])
        self.assertIsNone(result['pressure_increments'])

    def test_short_bars_and_missing_daily_record_are_incomplete(self):
        values = fixture()
        values['2025-control']['bars'] -= 1
        self.decide(values, 'COMPARISON_EVIDENCE_INCOMPLETE')
        values = fixture()
        values['2025-veto']['books'][1]['days'].pop(10)
        self.decide(values, 'COMPARISON_EVIDENCE_INCOMPLETE')

    def test_fixed_path_domain(self):
        values = fixture()
        del values['2021-control']
        self.reject(values)
        values = fixture()
        values['extra'] = values['2021-control']
        self.reject(values)
        for field, replacement in (('start', m.PERIODS['2025'][0]+m.DAY),
                                   ('end', m.PERIODS['2025'][1]-m.DAY),
                                   ('latency', 2), ('latency', True),
                                   ('capital', 20000), ('notional', 1250),
                                   ('horizon', 'changed'), ('terminal_reserve_bars', 5),
                                   ('initially_stopped', True), ('bars', -1)):
            with self.subTest(field=field, replacement=replacement):
                values = fixture()
                values['2025-veto'][field] = replacement
                self.reject(values)
        values = fixture()
        values['2025-delay']['latency'] = 1
        self.reject(values)

    def test_two_books_order_and_day_clock(self):
        values = fixture()
        values['2025-veto']['books'].reverse()
        self.reject(values)
        values = fixture()
        values['2025-veto']['books'].pop()
        self.reject(values)
        values = fixture()
        values['2025-veto']['books'][1]['days'][0]['t'] += m.STEP
        self.reject(values)
        values = fixture()
        days = values['2025-veto']['books'][1]['days']
        days.insert(1, copy.deepcopy(days[0]))
        self.reject(values)

    def test_invalid_numbers_and_false_active_weeks(self):
        for value in (True, float('nan'), float('inf'), '100'):
            values = fixture()
            values['2025-veto']['books'][1]['net'] = value
            self.reject(values)
        values = fixture()
        values['2025-veto']['books'][1]['active_weeks'].append('2040-W01')
        self.reject(values)
        values = fixture()
        values['2025-veto']['books'][1]['active_weeks'].append(
            values['2025-veto']['books'][1]['active_weeks'][0])
        self.reject(values)

    def test_final_cash_identity_and_cycle_metadata(self):
        values = fixture()
        values['2025-veto']['books'][1]['cash'] += 1.
        self.reject(values)
        values = fixture()
        values['2025-veto']['books'][1]['cycles'][0]['complete'] = 1
        self.reject(values)
        values = fixture()
        values['2025-veto']['books'][1]['cycles'][0]['end'] = values['2025-veto']['end']
        self.reject(values)

    def test_audit_is_independent_and_rejects_output_tampering(self):
        values = fixture()
        result = m.classify(values)
        with mock.patch.object(m, 'classify', side_effect=AssertionError('classifier called')), \
             mock.patch.object(m, '_validate', side_effect=AssertionError('validator called')):
            self.assertEqual(m.audit_verdict(values, result)['decision_status'], 'DEVELOPMENT_SUPPORTED')
        for field, replacement in (('independent_confirmation', True), ('online_learning_proven', True),
                                   ('g4', True), ('qualified_candidates', 1),
                                   ('demo_opening_lock_unchanged', False),
                                   ('status', 'FRAGILE_DEVELOPMENT_SIGNAL'), ('reasons', ['invented'])):
            with self.subTest(field=field):
                changed = copy.deepcopy(result)
                changed[field] = replacement
                with self.assertRaises(ValueError):
                    m.audit_verdict(values, changed)
        changed = copy.deepcopy(result)
        changed['extra'] = 1
        with self.assertRaises(ValueError):
            m.audit_verdict(values, changed)
        changed = copy.deepcopy(result)
        changed['pressure_increments']['2025'] += 1.
        with self.assertRaises(ValueError):
            m.audit_verdict(values, changed)
        changed = copy.deepcopy(result)
        changed['capacity']['complete_cycles'] = True
        with self.assertRaises(ValueError):
            m.audit_verdict(values, changed)


if __name__ == '__main__':
    unittest.main()
