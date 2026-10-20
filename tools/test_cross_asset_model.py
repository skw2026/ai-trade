import copy
from decimal import Decimal
import json
import unittest

import cross_asset_relative_strength as m
import audit_cross_asset as a


class Archive:
    def __init__(self):
        self.daily = {s: {} for s in m.SYMBOLS}
        for day in range(-60, 370):
            t = m.FIRST+day*m.DAY
            for s, base, slope in zip(m.SYMBOLS, (100, 200, 300), ('.5', '.1', '.05')):
                self.daily[s][str(t)] = str(Decimal(base)+day*Decimal(slope))

    def bars(self, t, kind):
        return [[v, v, v, v]+([100000] if kind == 'trade' else []) for v in (100., 200., 300.)]

    def rates(self, t):
        return {0: '.001', 2: '-.001'} if (t-m.FIRST)//m.MINUTE == 2 else {}


def snap(sim):
    return json.loads(json.dumps(sim.snapshot()))


class Policy(unittest.TestCase):
    def test_exact_ratio_and_independent_ranking(self):
        ar = Archive()
        for h in (7, 14, 28):
            intent = m.choose(ar.daily, m.FIRST, h)
            self.assertEqual(intent, a.intent(ar.daily, m.FIRST, h))
            self.assertGreater(Decimal(intent['quantities'][0]), 0)
            self.assertLess(Decimal(intent['quantities'][2]), 0)
            self.assertEqual(Decimal(intent['quantities'][1]), 0)

    def test_common_price_scale_preserves_direction(self):
        ar = Archive(); q = m.choose(ar.daily, m.FIRST, 7)['quantities']
        scaled = {s: {k: str(Decimal(v)*2) for k, v in row.items()} for s, row in ar.daily.items()}
        new = m.choose(scaled, m.FIRST, 7)['quantities']
        self.assertEqual([Decimal(x).compare(0) for x in q], [Decimal(x).compare(0) for x in new])

    def test_future_changes_do_not_change_signal(self):
        ar = Archive(); before = m.choose(ar.daily, m.FIRST, 7)
        for s in m.SYMBOLS:
            ar.daily[s][str(m.FIRST+m.DAY)] = '999999'
        self.assertEqual(before, m.choose(ar.daily, m.FIRST, 7))

    def test_tie_is_cash(self):
        ar = Archive()
        for s in m.SYMBOLS:
            ar.daily[s][str(m.FIRST)] = ar.daily[s][str(m.FIRST-7*m.DAY)]
        self.assertEqual(m.choose(ar.daily, m.FIRST, 7)['quantities'], ['0']*3)

    def test_closed_timestamp_required(self):
        ar = Archive()
        del ar.daily['BTCUSDT'][str(m.FIRST)]
        with self.assertRaises(KeyError):
            m.choose(ar.daily, m.FIRST, 7)

    def test_only_three_horizons(self):
        with self.assertRaises(ValueError):
            m.choose(Archive().daily, m.FIRST, 21)


class Execution(unittest.TestCase):
    def run_small(self, minutes=10, delay=0, archive=None):
        ar = archive or Archive()
        return ar, m.Simulation(ar, 7, delay, m.FIRST, m.FIRST+m.WEEK).run_to(m.FIRST+minutes*m.MINUTE)

    def test_sequential_and_funding_before_fill(self):
        ar, sim = self.run_small(3)
        b = sim.books[0]
        events = b.events
        self.assertEqual([(e['minute'], e.get('leg')) for e in events if e['kind'] == 'fill'], [(1, 0), (2, 2)])
        self.assertEqual([(e['minute'], e['leg']) for e in events if e['kind'] == 'funding'], [(2, 0)])
        self.assertEqual(sim.books[0].cycle['quantities'], sim.books[1].cycle['quantities'])
        a.audit(ar, snap(sim))

    def test_delay(self):
        ar, sim = self.run_small(4, 1)
        self.assertEqual([e['minute'] for e in sim.books[0].events if e['kind'] == 'fill'], [2, 3])
        a.audit(ar, snap(sim))

    def test_all_ticks_lots_and_cost_identity(self):
        for i, tick in enumerate(m.TICKS):
            for stress in (False, True):
                for sign in (-1, 1):
                    px = m.price([100, 101, 99, 100, 1000], Decimal(sign), i, stress)
                    self.assertEqual(px % tick, 0)
                    self.assertTrue(px > 100 if sign > 0 and not stress else px < 100 if sign < 0 else True)
        ar, sim = self.run_small(3)
        for b in sim.books:
            self.assertTrue(b.review())
            self.assertEqual(b.cash-10000, b.gross-b.slippage-b.fees-b.funding)

    def test_partial_entry_cancel_and_unwind(self):
        class Partial(Archive):
            def bars(self, t, kind):
                out = super().bars(t, kind)
                if kind == 'trade' and t == m.FIRST+m.MINUTE:
                    out[0][4] = 10
                return out
        ar, sim = self.run_small(5, archive=Partial())
        for b in sim.books:
            self.assertTrue(b.flat); self.assertEqual(b.failed_entries, 1)
            self.assertFalse(b.cycles[0]['completed'])
            self.assertLess(b.cash, 10000)
            self.assertFalse(any(e.get('leg') == 2 for e in b.events if e['kind'] == 'fill'))
        a.audit(ar, snap(sim))

    def test_minimum_notional_no_fabricated_fill(self):
        b = m.Ledger()
        b.request_entry(0, m.choose(Archive().daily, m.FIRST, 7))
        b.process(1, [[100, 100, 100, 100, .1]]*3)
        self.assertTrue(b.flat); self.assertEqual(b.cash, 10000)
        self.assertEqual(b.failed_entries, 1)

    def test_exit_deadline_not_extended_by_delay(self):
        ar, sim = self.run_small(4, 1)
        b = sim.books[0]; q = list(b.q)
        b.request_exit(4, 'TEST')
        self.assertEqual(b.deadline, 9)
        for minute in range(5, 11):
            b.process(minute, [[100, 100, 100, 100, 0]]*3)
        self.assertEqual(b.q, q)
        self.assertEqual(b.insufficient, 'RESIDUAL_EXPOSURE_UNCLOSED')

    def test_pair_rotation_handles_eth_as_first_leg(self):
        b = m.Ledger(); ar = Archive()
        for q in (['0', '5', '-4'], ['10', '-5', '0'], ['-10', '0', '4']):
            intent = dict(t=m.FIRST, horizon=7, closes=['100', '200', '300'],
                          prior=['100', '200', '300'], quantities=q, tied=False)
            b.request_entry(0, intent)
            b.process(1, ar.bars(m.FIRST, 'trade')); b.process(2, ar.bars(m.FIRST, 'trade'))
            self.assertTrue(b.complete_entry)
            b.request_exit(3, 'TEST')
            b.process(4, ar.bars(m.FIRST, 'trade')); b.process(5, ar.bars(m.FIRST, 'trade'))
            self.assertTrue(b.flat); self.assertTrue(b.review())
        self.assertEqual(len(b.cycles), 3)

    def test_linked_hard_stop_no_restart(self):
        class Crash(Archive):
            def bars(self, t, kind):
                out = super().bars(t, kind)
                if t >= m.FIRST+3*m.MINUTE:
                    out[0][:4] = [1., 1., 1., 1.]
                return out
        ar, sim = self.run_small(15, archive=Crash())
        self.assertTrue(all(b.stopped for b in sim.books))
        self.assertTrue(all(b.flat for b in sim.books))
        self.assertEqual(len(sim.signals), 1)
        a.audit(ar, snap(sim))

    def test_performance_failure_keeps_reduction_available(self):
        ar, sim = self.run_small(3)
        b = sim.books[0]
        b.insufficient = 'PERFORMANCE_REVIEW_FAILED'
        b.stop(3, b.insufficient)
        b.process(4, ar.bars(m.FIRST, 'trade')); b.process(5, ar.bars(m.FIRST, 'trade'))
        self.assertTrue(b.flat)
        with self.assertRaises(ValueError):
            b.request_entry(6, m.choose(ar.daily, m.FIRST, 7))

    def test_checkpoint_no_reset(self):
        ar, sim = self.run_small(2)
        original = snap(sim)
        restored = m.Simulation.restore(ar, original)
        self.assertEqual(snap(restored), original)
        sim.run_to(m.FIRST+15*m.MINUTE); restored.run_to(m.FIRST+15*m.MINUTE)
        self.assertEqual(snap(sim), snap(restored))
        a.audit(ar, snap(restored))

    def test_checkpoint_source_is_immutable_after_continuation(self):
        ar, sim = self.run_small(2)
        frozen = snap(sim); before = copy.deepcopy(frozen)
        restored = m.Simulation.restore(ar, frozen)
        restored.run_to(m.FIRST+15*m.MINUTE)
        self.assertEqual(frozen, before)

    def test_half_year_cash_and_peak_are_carried(self):
        ar = Archive()
        sim = m.Simulation(ar, 7, start=m.HALF-m.WEEK, end=m.HALF+m.DAY).run_to(m.HALF)
        frozen = snap(sim)
        self.assertLess(sim.books[0].cash, 10000)
        restored = m.Simulation.restore(ar, frozen)
        self.assertEqual(restored.books[0].cash, sim.books[0].cash)
        self.assertEqual(restored.books[0].peak, sim.books[0].peak)
        sim.run_to(m.HALF+10*m.MINUTE); restored.run_to(m.HALF+10*m.MINUTE)
        self.assertEqual(snap(sim), snap(restored))
        a.audit(ar, snap(restored))

    def test_performance_alert_independently_audited(self):
        class Falling(Archive):
            def bars(self, t, kind):
                bars = super().bars(t, kind)
                px = 100-(t-m.FIRST)//m.MINUTE*.0001
                bars[0][:4] = [px]*4
                return bars
        ar, sim = self.run_small(2405, archive=Falling())
        self.assertTrue(sim.books[0].alert.active)
        self.assertIsNone(sim.books[0].stopped)
        a.audit(ar, snap(sim))

    def test_simultaneous_exit_insufficiency_is_audited_as_failure(self):
        class Unclosed(Archive):
            def bars(self, t, kind):
                bars = super().bars(t, kind)
                if kind == 'trade' and t == m.FIRST+2*m.MINUTE:
                    bars[2][4] = 10
                if kind == 'trade' and t >= m.FIRST+3*m.MINUTE:
                    for bar in bars:
                        bar[4] = 0
                return bars
        ar, sim = self.run_small(15, archive=Unclosed())
        result = a.audit(ar, snap(sim))
        self.assertTrue(all(v['insufficient'] == 'RESIDUAL_EXPOSURE_UNCLOSED' for v in result))
        self.assertTrue(all(any(map(Decimal, v['residual'])) for v in result))
        self.assertTrue(all(v['first_execution_failure']['minute'] == 8 for v in result))

    def test_two_peer_labels_without_verified_cause_are_rejected(self):
        v = dict(stopped=dict(minute=8, reason='PEER_PATH_REJECTED'), residual=['0', '0', '.1'],
                 insufficient='RESIDUAL_EXPOSURE_UNCLOSED', first_execution_failure=None)
        with self.assertRaisesRegex(ValueError, 'LINKED_STOP'):
            a.validate_stop_links([copy.deepcopy(v), copy.deepcopy(v)])
        v['first_execution_failure'] = dict(minute=7, reason='RESIDUAL_EXPOSURE_UNCLOSED')
        with self.assertRaisesRegex(ValueError, 'LINKED_STOP'):
            a.validate_stop_links([copy.deepcopy(v), copy.deepcopy(v)])

    def test_weekly_exit_and_reentry_even_same_pair(self):
        ar = Archive()
        sim = m.Simulation(ar, 7, start=m.FIRST, end=m.FIRST+2*m.WEEK).run_to(m.FIRST+m.WEEK+5*m.MINUTE)
        for b in sim.books:
            self.assertEqual(len(b.cycles), 1)
            self.assertEqual(b.cycles[0]['end'], 10072)
            self.assertEqual(sum(e['kind'] == 'entry_requested' for e in b.events), 2)
        a.audit(ar, snap(sim))

    def test_mutated_trace_and_summary_rejected(self):
        ar, sim = self.run_small(10)
        original = snap(sim)
        for field in ('cash', 'fees', 'slippage', 'peak', 'dd'):
            bad = copy.deepcopy(original)
            bad['books'][0][field] = str(Decimal(str(bad['books'][0][field]))+1)
            with self.subTest(field=field), self.assertRaises(ValueError):
                a.audit(ar, bad)
        bad = copy.deepcopy(original)
        bad['books'][0]['events'][1]['minute'] = 0
        with self.assertRaises(ValueError):
            a.audit(ar, bad)

    def test_zero_signal_audited_not_profit(self):
        ar = Archive()
        ar.daily = {s: {k: '100' for k in row} for s, row in ar.daily.items()}
        sim = m.Simulation(ar, 7, start=m.FIRST, end=m.FIRST+m.DAY).run_to(m.FIRST+m.DAY)
        self.assertEqual(sim.books[0].cash, 10000)
        self.assertEqual(sim.books[0].position_minutes, 0)
        a.audit(ar, snap(sim))


class Selection(unittest.TestCase):
    def fixture(self, cash, stopped=None):
        return dict(start=m.INITIAL, minute=(m.HALF-m.INITIAL)//m.MINUTE,
                    books=[dict(cash=str(cash), stopped=stopped, insufficient=None,
                                q=['0']*3, queue=[]) for _ in range(2)])

    def test_negative_profits_eligible(self):
        self.assertEqual(m.select({7: self.fixture(9900), 14: self.fixture(9950), 28: self.fixture(9800)}), 14)

    def test_exact_ties_prefer_shorter(self):
        self.assertEqual(m.select({28: self.fixture(9900), 7: self.fixture(9900), 14: self.fixture(9900)}), 7)

    def test_no_float_tolerance_selection(self):
        self.assertEqual(m.select({7: self.fixture('9900'), 14: self.fixture('9900.00000000001')}), 14)

    def test_all_stopped_none(self):
        self.assertIsNone(m.select({h: self.fixture(11000, 'risk') for h in (7, 14, 28)}))

    def test_pending_not_eligible(self):
        f = self.fixture(11000); f['books'][0]['queue'] = [[1]]
        self.assertIsNone(m.select({7: f}))


if __name__ == '__main__':
    unittest.main()
