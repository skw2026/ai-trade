import copy
import unittest

import audit_open_market_execution as a
import audit_open_market_decision as decision_audit
import open_market_execution as m
import open_market_learning as learning

START = 1736726400000  # 2025-01-13 00:00 UTC


def fixture(days=3, direction=1):
    rows = [dict(timestamp=t, open=10000., high=10000., low=10000., price=10000.,
                 volume=100., mark_open=10000., mark_close=10000., mark_high=10000.,
                 mark_low=10000., funding_rate_per_interval=0.)
            for t in range(START - m.STEP, START + days * m.DAY, m.STEP)]
    fs = {t: dict(price=10000., latest_price_closed_at=t, modelvalues=[0.])
          for t in range(START, START + days * m.DAY, m.DAY)}
    return rows, fs, {t: direction for t in fs}


class ExecutionTests(unittest.TestCase):
    def run_case(self, days=3, latency=1, direction=1):
        rows, fs, decisions = fixture(days, direction)
        result = m.simulate(rows, fs, decisions, START, START + days * m.DAY, latency)
        a.audit(result, rows, fs, decisions)
        return result, rows, fs, decisions

    def test_daily_clocks_and_terminal_exception(self):
        r, _, _, _ = self.run_case()
        for b in r['books']:
            entries = [e['t'] for e in b['events'] if e['kind'] == 'entry_request']
            self.assertEqual(entries, [START + n * m.DAY for n in range(3)])
            exits = [e for e in b['events'] if e['kind'] == 'exit_request']
            self.assertEqual([e['t'] for e in exits],
                             [START + m.DAY - 3 * m.STEP,
                              START + 2 * m.DAY - 3 * m.STEP,
                              START + 3 * m.DAY - 6 * m.STEP])
            self.assertEqual([e['reason'] for e in exits], ['horizon', 'horizon', 'terminal'])
            self.assertEqual(len(b['cycles']), 3)

    def test_two_bar_delay_does_not_shift_decision_horizon(self):
        r, _, _, _ = self.run_case(latency=2, direction=-1)
        for b in r['books']:
            self.assertEqual(b['cycles'][0]['start'], START + 2 * m.STEP)
            self.assertEqual(b['cycles'][0]['end'], START + m.DAY - m.STEP)

    def test_no_trade_is_full_calendar(self):
        r, _, _, _ = self.run_case(direction=0)
        self.assertEqual(r['bars'], 3 * m.DAY // m.STEP)
        self.assertEqual(r['books'][0]['cycles'], [])
        self.assertEqual(r['books'][0]['net'], 0)

    def test_zero_fill_no_late_retry(self):
        rows, fs, decisions = fixture()
        for row in rows:
            row['volume'] = 0.
        r = m.simulate(rows, fs, decisions, START, START + 3 * m.DAY)
        a.audit(r, rows, fs, decisions)
        for b in r['books']:
            self.assertEqual(b['failed_entries'], 3)
            self.assertEqual(b['cycles'], [])

    def test_partial_entry_unwinds_once_per_day(self):
        rows, fs, decisions = fixture()
        rows[2]['volume'] = 1.
        r = m.simulate(rows, fs, decisions, START, START + 3 * m.DAY)
        a.audit(r, rows, fs, decisions)
        for b in r['books']:
            self.assertFalse(b['cycles'][0]['complete'])
            self.assertEqual(b['cycles'][0]['end'], START + 2 * m.STEP)
            self.assertEqual(len(b['cycles']), 3)

    def test_exact_decimal_volume_capacity_boundaries(self):
        for volume, capacity in ((.7, .007), (1.4, .014), (2.8, .028),
                                 (.6999, .006), (.7001, .007),
                                 (1.3999, .013), (1.4001, .014),
                                 (2.7999, .027), (2.8001, .028)):
            with self.subTest(volume=volume):
                rows, fs, decisions = fixture(days=1)
                rows[2]['volume'] = volume
                r = m.simulate(rows, fs, decisions, START, START + m.DAY)
                a.audit(r, rows, fs, decisions)
                for b in r['books']:
                    fill = next(e for e in b['events'] if e['kind'] == 'fill')
                    self.assertEqual(fill['capacity'], capacity)
                    self.assertEqual(fill['dq'], capacity)
                    self.assertFalse(b['cycles'][0]['complete'])
                    self.assertEqual(rows[2]['volume'], volume)

    def test_funding_before_same_bar_entry(self):
        rows, fs, decisions = fixture()
        rows[2]['funding_rate_per_interval'] = .001
        rows[3]['funding_rate_per_interval'] = -.001
        r = m.simulate(rows, fs, decisions, START, START + 3 * m.DAY)
        a.audit(r, rows, fs, decisions)
        self.assertEqual(r['books'][0]['funding'], -2.5)

    def test_risk_latches_but_calendar_continues(self):
        rows, fs, decisions = fixture()
        rows[3]['mark_low'] = 6000.
        r = m.simulate(rows, fs, decisions, START, START + 3 * m.DAY)
        a.audit(r, rows, fs, decisions)
        self.assertEqual(r['bars'], 3 * m.DAY // m.STEP)
        for b in r['books']:
            self.assertEqual(b['stopped']['t'], START + 3 * m.STEP)
            self.assertEqual(sum(e['kind'] == 'entry_request' for e in b['events']), 1)

    def test_exit_residual_stops_with_exposure(self):
        rows, fs, decisions = fixture()
        for row in rows:
            if row['timestamp'] >= START + m.DAY - 3 * m.STEP:
                row['volume'] = 0.
        r = m.simulate(rows, fs, decisions, START, START + 3 * m.DAY)
        a.audit(r, rows, fs, decisions)
        self.assertLess(r['bars'], 3 * m.DAY // m.STEP)
        self.assertEqual(r['books'][0]['insufficient'], 'EXIT_RESIDUAL')

    def test_audit_rejects_omission_direction_and_short_calendar(self):
        r, rows, fs, decisions = self.run_case()
        bad = copy.deepcopy(r)
        bad['books'][0]['events'].pop(0)
        with self.assertRaises(ValueError):
            a.audit(bad, rows, fs, decisions)
        wrong = dict(decisions)
        wrong[START] = -1
        with self.assertRaises(ValueError):
            a.audit(r, rows, fs, wrong)
        bad = copy.deepcopy(r)
        bad['bars'] -= 1
        with self.assertRaises(ValueError):
            a.audit(bad, rows, fs, decisions)

    def test_future_price_and_missing_decision_rejected(self):
        rows, fs, decisions = fixture()
        fs[START]['latest_price_closed_at'] += m.STEP
        with self.assertRaises(ValueError):
            m.simulate(rows, fs, decisions, START, START + 3 * m.DAY)
        rows, fs, decisions = fixture()
        del decisions[START]
        with self.assertRaises(ValueError):
            m.simulate(rows, fs, decisions, START, START + 3 * m.DAY)


class DecisionAuditTests(unittest.TestCase):
    def path(self, net=150., start=START, end=START+365*m.DAY, latency=1):
        book = dict(net=net, insufficient=None, quantity=0., stopped=None,
                    cycles=[dict(net=net/30, complete=True) for _ in range(30)],
                    active_weeks=['w'+str(i) for i in range(12)],
                    days=[dict(t=1751328000000, equity=10000+net/2),
                          dict(t=end, equity=10000+net)])
        return dict(start=start, end=end, latency=latency,
                    bars=(end-start)//m.STEP, books=[copy.deepcopy(book), copy.deepcopy(book)])

    def paths(self):
        paths = {period+'-'+arm: self.path(150. if arm == 'PV' else 50.)
                 for period in ('2021', '2025') for arm in ('P', 'PV', 'STALE')}
        paths['2025-PV-delay'] = self.path(latency=2)
        selection = {arm: dict(ridge=1., eligible=True, status='SELECTED')
                     for arm in ('P', 'PV', 'STALE')}
        return paths, selection

    def test_selection_tie_and_diagnostic_only(self):
        comparisons = [dict(self.path(), ridge=r) for r in (.1, 1., 10.)]
        selected = learning.select(comparisons)
        decision_audit.audit_selection(comparisons, selected)
        self.assertEqual(selected['ridge'], 10.)
        for path in comparisons:
            path['books'][0]['stopped'] = dict(t=START, reason='synthetic')
        selected = learning.select(comparisons)
        decision_audit.audit_selection(comparisons, selected)
        self.assertFalse(selected['eligible'])
        bad = dict(selected, eligible=True, status='SELECTED')
        with self.assertRaises(ValueError):
            decision_audit.audit_selection(comparisons, bad)

    def test_selection_missing_ridge_or_calendar_rejected(self):
        comparisons = [dict(self.path(), ridge=r) for r in (.1, 1., 10.)]
        selected = learning.select(comparisons)
        with self.assertRaises(ValueError):
            decision_audit.audit_selection(comparisons[:-1], selected)
        comparisons[0]['bars'] -= 1
        with self.assertRaises(ValueError):
            decision_audit.audit_selection(comparisons, selected)

    def test_supported_verdict_and_qualification_lock(self):
        paths, selection = self.paths()
        decision = learning.verdict(paths, selection)
        self.assertEqual(decision['status'], 'DEVELOPMENT_SUPPORTED')
        decision_audit.audit_verdict(paths, selection, decision)
        bad = dict(decision, g4=True)
        with self.assertRaises(ValueError):
            decision_audit.audit_verdict(paths, selection, bad)
        del paths['2025-STALE']
        with self.assertRaises(ValueError):
            decision_audit.audit_verdict(paths, selection, decision)

    def test_partial_cycles_are_not_capacity_and_still_concentrate(self):
        paths, selection = self.paths()
        book = paths['2025-PV']['books'][1]
        book['cycles'][0] = dict(net=160., complete=False)
        decision = learning.verdict(paths, selection)
        self.assertIn('EVENT_CAPACITY', decision['reasons'])
        self.assertIn('BEST_CYCLE_CONCENTRATION', decision['reasons'])
        decision_audit.audit_verdict(paths, selection, decision)

    def test_incomplete_calendar_has_no_support(self):
        paths, selection = self.paths()
        paths['2025-PV']['bars'] -= 1
        decision = learning.verdict(paths, selection)
        self.assertEqual(decision['status'], 'COMPARISON_EVIDENCE_INCOMPLETE')
        decision_audit.audit_verdict(paths, selection, decision)


if __name__ == '__main__':
    unittest.main()
