"""Synthetic-only tests; do not load market archives or old strategy runners."""
import copy
from fractions import Fraction
import itertools
import math
import unittest

import research_policy_design as policy


class EntryTests(unittest.TestCase):
    def entry(self, baseline=-10, **changes):
        flags = dict(input_valid=True, scope_complete=True, budget_available=True, closed=False)
        flags.update(changes)
        return policy.development_case(baseline, **flags)

    def test_negative_baseline_can_enter(self):
        self.assertEqual(self.entry(), 'CASE_BOUNDED_DEVELOPMENT_ALLOWED')

    def test_positive_baseline_has_no_special_privilege(self):
        self.assertEqual(self.entry(10), self.entry(-10))

    def test_missing_scope_blocks_even_profitable_baseline(self):
        self.assertEqual(self.entry(10, scope_complete=False), 'CASE_NOT_ADMITTED')

    def test_missing_input_blocks(self):
        self.assertEqual(self.entry(input_valid=False), 'CASE_NOT_ADMITTED')

    def test_budget_blocks(self):
        self.assertEqual(self.entry(budget_available=False), 'CASE_NOT_ADMITTED')

    def test_closed_route_not_reopened(self):
        self.assertEqual(self.entry(100, closed=True), 'CASE_CLOSED_ROUTE_REMAINS_CLOSED')

    def test_unknown_and_nonfinite_rejected(self):
        for value in (None, True, float('nan'), float('inf'), '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.entry(value)
        with self.assertRaises(ValueError):
            self.entry(scope_complete=None)


class EconomicTests(unittest.TestCase):
    def decide(self, base=10, stress=5, ref_base=-10, ref_stress=-20, **changes):
        flags = dict(learned=True, fills=10, evidence_complete=True,
                     identity_matches=True, hard_risk=False, closed=False)
        flags.update(changes)
        return policy.economic_case(base, stress, ref_base, ref_stress, **flags)

    def test_profit_over_losing_reference(self):
        self.assertEqual(self.decide(), 'CASE_ECONOMIC_SUPPORT_NOT_DEPLOYMENT_PERMISSION')

    def test_less_loss_is_not_profit(self):
        self.assertEqual(self.decide(-1, -2), 'CASE_REJECT_NET_OR_ACTIVITY')

    def test_stress_loss_rejects(self):
        self.assertEqual(self.decide(stress=-1), 'CASE_REJECT_NET_OR_ACTIVITY')

    def test_zero_net_rejects(self):
        self.assertEqual(self.decide(base=0), 'CASE_REJECT_NET_OR_ACTIVITY')

    def test_no_fills_rejects(self):
        self.assertEqual(self.decide(fills=0), 'CASE_REJECT_NET_OR_ACTIVITY')

    def test_no_increment_rejects_learning(self):
        self.assertEqual(self.decide(ref_base=10), 'CASE_REJECT_LEARNING_INCREMENT')
        self.assertEqual(self.decide(ref_stress=6), 'CASE_REJECT_LEARNING_INCREMENT')

    def test_fixed_strategy_need_not_claim_learning(self):
        self.assertEqual(self.decide(ref_base=10, learned=False),
                         'CASE_ECONOMIC_SUPPORT_NOT_DEPLOYMENT_PERMISSION')

    def test_evidence_and_identity_required(self):
        self.assertEqual(self.decide(evidence_complete=False), 'CASE_INSUFFICIENT')
        self.assertEqual(self.decide(identity_matches=False), 'CASE_INSUFFICIENT')

    def test_risk_cannot_be_downgraded_to_missing_evidence(self):
        self.assertEqual(self.decide(hard_risk=True, evidence_complete=False), 'CASE_REJECT_HARD_RISK')

    def test_closed_route_stays_closed(self):
        self.assertEqual(self.decide(closed=True), 'CASE_CLOSED_ROUTE_REMAINS_CLOSED')

    def test_invalid_inputs_reject(self):
        with self.assertRaises(ValueError):
            self.decide(stress=math.nan)
        with self.assertRaises(ValueError):
            self.decide(fills=True)
        with self.assertRaises(ValueError):
            self.decide(learned=1)


class PerformanceTests(unittest.TestCase):
    def action(self, **changes):
        fields = dict(operator_lock=False, existing_latch=False, hard_risk=False,
                      evidence_known=True, alert=False)
        fields.update(changes)
        return policy.performance_case(**fields)

    def test_alert_freezes_learning_not_permanent_path(self):
        self.assertEqual(self.action(alert=True), 'FREEZE_UPDATES_AND_REVIEW')

    def test_operator_lock_preserved(self):
        self.assertEqual(self.action(alert=True, operator_lock=True), 'RETAIN_EXISTING_REDUCE_ONLY_OR_LATCH')

    def test_existing_latch_never_clears(self):
        self.assertEqual(self.action(existing_latch=True), 'RETAIN_EXISTING_REDUCE_ONLY_OR_LATCH')

    def test_hard_risk_dominates_all_other_states(self):
        for lock, latch, known, alert in itertools.product((False, True), repeat=4):
            with self.subTest(lock=lock, latch=latch, known=known, alert=alert):
                self.assertEqual(self.action(hard_risk=True, operator_lock=lock,
                    existing_latch=latch, evidence_known=known, alert=alert), 'STOP_PATH_AND_UNWIND')

    def test_unknown_input_blocks_new_risk(self):
        self.assertEqual(self.action(evidence_known=False, alert=True), 'BLOCK_NEW_RISK_AND_RECONCILE')

    def test_no_alert_is_not_unlock(self):
        self.assertEqual(self.action(), 'NO_NEW_ACTION_NO_UNLOCK_PERMISSION')

    def test_unknown_flag_rejects(self):
        with self.assertRaises(ValueError):
            self.action(hard_risk=None)


class ProbabilityTests(unittest.TestCase):
    def test_small_exact_enumeration(self):
        for n in (1, 4, 8):
            expected = [Fraction(0)]*(n+1)
            for outcomes in itertools.product((0, 1), repeat=n):
                expected[sum(outcomes)] += Fraction(1, 2**n)
            self.assertEqual(policy.masses(n, '0.5'), expected)

    def test_independent_convolution(self):
        for p in ('0', '0.4', '0.5', '0.75', '1'):
            self.assertEqual(policy.masses(20, p), policy.independent_masses(20, p))

    def test_known_critical_value(self):
        self.assertEqual(policy.critical_wins(20), 15)
        self.assertEqual(sum(policy.masses(20, '0.5')[15:]), Fraction(21700, 2**20))

    def test_short_sample_cannot_reject(self):
        self.assertEqual(policy.critical_wins(1), 2)

    def test_null_control_and_cutoff_minimality(self):
        for n in (20, 50, 100, 200):
            k = policy.critical_wins(n); row = policy.masses(n, '0.5')
            self.assertLessEqual(sum(row[k:]), Fraction(1, 20))
            self.assertGreater(sum(row[k-1:]), Fraction(1, 20))

    def test_positive_control_can_be_detected(self):
        n = 50; k = policy.critical_wins(n)
        self.assertGreaterEqual(sum(policy.masses(n, '0.75')[k:]), Fraction(4, 5))

    def test_negative_control_not_more_likely_to_pass(self):
        n = 50; k = policy.critical_wins(n)
        self.assertLessEqual(sum(policy.masses(n, '0.4')[k:]), sum(policy.masses(n, '0.5')[k:]))

    def test_bounds_and_boolean_samples(self):
        for n in (0, 201, True):
            with self.assertRaises(ValueError):
                policy.masses(n, '0.5')
        with self.assertRaises(ValueError):
            policy.masses(10, '1.1')


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy.read(policy.POLICY)

    def test_approved_contract(self):
        policy.validate_policy(self.policy)

    def test_runtime_reopen_and_market_authority_reject(self):
        for field in ('runtime_applied', 'reopen_closed_routes', 'market_research_authorized'):
            changed = copy.deepcopy(self.policy); changed[field] = True
            with self.subTest(field=field), self.assertRaises(ValueError):
                policy.validate_policy(changed)

    def test_hard_risk_change_rejects(self):
        self.policy['risk']['research_drawdown_stop'] = '0.12'
        with self.assertRaises(ValueError):
            policy.validate_policy(self.policy)

    def test_automatic_unlock_rejects(self):
        self.policy['risk']['operator_lock_may_clear'] = True
        with self.assertRaises(ValueError):
            policy.validate_policy(self.policy)

    def test_baseline_profit_prerequisite_rejects(self):
        self.policy['development']['positive_baseline_required'] = True
        with self.assertRaises(ValueError):
            policy.validate_policy(self.policy)

    def test_fixed_wait_and_fabricated_effect_reject(self):
        for field, value in (('project_confirmation_duration', 182), ('project_effect_size_estimate', 2)):
            changed = copy.deepcopy(self.policy); changed['synthetic_calibration'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                policy.validate_policy(changed)

    def test_complete_scope_required(self):
        self.policy['next_market_scope_required'].remove('minimum_economic_effect_and_units')
        with self.assertRaises(ValueError):
            policy.validate_policy(self.policy)


if __name__ == '__main__':
    unittest.main()
