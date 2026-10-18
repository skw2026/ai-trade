import copy
import unittest
from unittest.mock import patch

import dated_basis_event_panel as panel


def rows():
    return [dict(symbol=s, observed_complete=s != panel.GAPPED,
                 candidate_absolute_support=s != panel.GAPPED,
                 central_increment_support=s != panel.GAPPED) for s in panel.SYMBOLS]


def fake_result(symbol):
    portfolios = []
    for sc in panel.candidate.scenarios():
        portfolios.append(dict(scenario=sc, cash_net=10., uncertain=False,
            unresolved=False, halted=False, closed_quarters=1,
            paths={symbol: dict(status='CLOSED', cash_net=10.)}))
    return dict(intentions={symbol: None}, portfolios=portfolios)


class EventPanelTests(unittest.TestCase):
    def test_observed_support_never_qualifies_continuous_account(self):
        s = panel.summarize(rows())
        self.assertEqual(s['status'], 'REPEATED_OBSERVED_EVENT_SUPPORT')
        self.assertEqual(s['events_registered'], 4)
        self.assertEqual(s['observed_complete_events'], 3)
        self.assertEqual(s['unknown_or_unclosed_events'], 1)
        self.assertIsNone(s['annual_return'])
        self.assertEqual(s['continuous_account_status'], 'DATA_INSUFFICIENT_UNCHANGED')
        for key in ('actual_execution_qualified', 'candidate_qualified', 'g4', 'statistical_independence_established'):
            self.assertFalse(s[key])

    def test_any_new_target_unknown_is_insufficient(self):
        for i in (0, 2, 3):
            r = rows(); r[i]['observed_complete'] = False
            self.assertEqual(panel.summarize(r)['status'], 'EVENT_EVIDENCE_INSUFFICIENT')

    def test_any_target_absolute_failure_stops_support(self):
        for i in (0, 2, 3):
            r = rows(); r[i]['candidate_absolute_support'] = False
            self.assertEqual(panel.summarize(r)['status'], 'NO_REPEATABLE_EVENT_SUPPORT')

    def test_one_increment_event_is_not_recurrence(self):
        r = rows()
        for i in (2, 3): r[i]['central_increment_support'] = False
        self.assertEqual(panel.summarize(r)['status'], 'NO_REPEATABLE_EVENT_SUPPORT')

    def test_two_increment_events_suffice_only_for_stage_claim(self):
        r = rows(); r[3]['central_increment_support'] = False
        self.assertEqual(panel.summarize(r)['status'], 'REPEATED_OBSERVED_EVENT_SUPPORT')

    def test_missing_reordered_duplicate_and_extra_events_rejected(self):
        for r in (rows()[:-1], list(reversed(rows())), rows()+[rows()[0]], [rows()[0]]*4):
            with self.assertRaises(ValueError): panel.summarize(r)

    def test_scope_rejected_before_market_engine(self):
        with patch.object(panel.control, 'replay') as call:
            with self.assertRaises(ValueError): panel.replay({'BTCUSD_250328': {}})
            call.assert_not_called()

    def test_singletons_used_for_both_policies_and_audits(self):
        bundle = {s: {'fixed': s} for s in panel.SYMBOLS}
        def result(b):
            self.assertEqual(len(b), 1)
            return fake_result(next(iter(b)))
        with patch.object(panel.control, 'replay', side_effect=result) as c, \
             patch.object(panel.candidate, 'replay', side_effect=result) as r, \
             patch.object(panel.control_audit, 'verify', return_value={}) as ca, \
             patch.object(panel.candidate_audit, 'verify', return_value={}) as ra:
            actual = panel.replay(bundle)
            for call in (c, r, ca, ra): self.assertEqual(call.call_count, 4)
            self.assertEqual(list(actual['events']), list(panel.SYMBOLS))
            self.assertEqual(bundle, {s: {'fixed': s} for s in panel.SYMBOLS})

    def test_identical_control_is_not_positive_increment(self):
        s = panel.SYMBOLS[0]; r = fake_result(s)
        row = panel.describe(s, r, copy.deepcopy(r))
        self.assertTrue(row['candidate_absolute_support'])
        self.assertFalse(row['central_increment_support'])

    def test_negative_or_zero_pressure_cannot_support(self):
        s = panel.SYMBOLS[0]
        for cash in (0., -1.):
            r = fake_result(s); r['portfolios'][-1]['cash_net'] = cash
            self.assertFalse(panel.describe(s, r, fake_result(s))['candidate_absolute_support'])

    def test_halt_unknown_and_unresolved_remain_disqualifying(self):
        s = panel.SYMBOLS[0]
        for key in ('halted', 'uncertain', 'unresolved'):
            r = fake_result(s); r['portfolios'][-1][key] = True
            self.assertFalse(panel.describe(s, r, fake_result(s))['candidate_absolute_support'])

    def test_unknown_control_blocks_paired_support(self):
        s = panel.SYMBOLS[0]; r = fake_result(s); c = fake_result(s)
        c['portfolios'][0]['uncertain'] = True
        self.assertFalse(panel.describe(s, r, c)['observed_complete'])

    def test_missing_sensitivity_cell_rejected(self):
        s = panel.SYMBOLS[0]; r = fake_result(s); r['portfolios'].pop()
        with self.assertRaises(AssertionError): panel.describe(s, r, fake_result(s))

    def test_different_entry_intention_rejected(self):
        s = panel.SYMBOLS[0]; r = fake_result(s); r['intentions'][s] = {'changed': True}
        with self.assertRaises(AssertionError): panel.describe(s, r, fake_result(s))


if __name__ == '__main__': unittest.main()
