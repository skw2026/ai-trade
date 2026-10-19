"""Synthetic action-label diagnostics; formal execution belongs to root gate."""
import copy
import math
import unittest
from unittest import mock

import audit_open_market_action as independent
import open_market_action as action

DAY = 86400000
BOUNDARIES = list(independent.BOUNDARIES)
ARMS = ('old2021', 'recent2025', 'rolling')


def forecast(t, direction, identity='synthetic', values=None):
    if values is None:
        values = {1: [.02, -.01], -1: [-.01, .02], 0: [-.02, -.01]}[direction]
    inferred = 0 if max(values) <= 0 else (1 if values[0] >= values[1] else -1)
    return dict(t=t, predicted_net_fraction=list(values), direction=inferred, model_id=identity)


def fixture():
    dates = list(range(BOUNDARIES[0], BOUNDARIES[-1], DAY))
    labels = [dict(t=t, label_end=t+DAY-300000, x={}, y=[.01, -.01]) for t in dates]
    streams = {arm: [] for arm in ARMS}
    for t in dates:
        streams['old2021'].append(forecast(t, -1, 'old'))
        streams['recent2025'].append(forecast(t, 0, 'initial'))
        streams['rolling'].append(forecast(t, 0 if t < BOUNDARIES[1] else 1, 'shadow'))
    return labels, streams, list(BOUNDARIES)


def set_actions(streams, arm, direction, *, segment=None, values=None):
    left = BOUNDARIES[0] if segment is None else BOUNDARIES[segment]
    right = BOUNDARIES[-1] if segment is None else BOUNDARIES[segment+1]
    for i, prediction in enumerate(streams[arm]):
        if left <= prediction['t'] < right:
            streams[arm][i] = forecast(prediction['t'], direction, prediction['model_id'], values)


class ActionDiagnosticTests(unittest.TestCase):
    def checked(self, labels, streams, boundaries):
        result = action.evaluate(labels, streams, boundaries)
        receipt = independent.audit(labels, streams, boundaries, result)
        self.assertEqual(receipt['status'], 'INDEPENDENT_ACTION_DIAGNOSTIC_AUDIT_VERIFIED')
        self.assertTrue(receipt['paired_regret_identity_verified'])
        return result

    def test_positive_control_exact_calendar_and_six_conditions(self):
        result = self.checked(*fixture())
        self.assertEqual(result['status'], 'ACTION_LABEL_IMPROVEMENT')
        self.assertEqual(result['full']['count'], 169)
        self.assertEqual(result['postinitial']['count'], 138)
        self.assertEqual([s['count'] for s in result['segments']], [31, 31, 30, 31, 30, 16])
        self.assertEqual(len(result['support']['conditions']), 6)
        self.assertTrue(all(result['support']['conditions'].values()))
        self.assertEqual(result['support']['positive_months'], 5)

    def test_utility_mean_includes_cash_days_not_only_actions(self):
        result = self.checked(*fixture())
        full = result['full']['arms']['rolling']
        self.assertEqual(full['noncash_days'], 138)
        self.assertAlmostEqual(full['utility_sum'], 1.38)
        self.assertAlmostEqual(full['utility_mean'], 1.38/169)
        self.assertAlmostEqual(result['postinitial']['arms']['rolling']['utility_mean'], .01)

    def test_all_cash_oracle_includes_zero_when_both_targets_negative(self):
        labels, streams, boundaries = fixture()
        for label in labels:
            label['y'] = [-.01, -.02]
        for arm in ARMS:
            set_actions(streams, arm, 0)
        result = self.checked(labels, streams, boundaries)
        self.assertEqual(result['full']['oracle_sum'], 0.)
        for metrics in result['full']['arms'].values():
            self.assertEqual(metrics['noncash_days'], 0)
            self.assertEqual(metrics['zero_action_labels'], 0)
            self.assertEqual(metrics['regret_sum'], 0.)
            self.assertEqual(metrics['missed_positive_cash'], 0)
        self.assertEqual(result['status'], 'ACTION_LABEL_UNSUPPORTED')

    def test_cash_with_positive_oracle_is_missed_gain_not_zero_action(self):
        labels, streams, boundaries = fixture()
        for arm in ARMS:
            set_actions(streams, arm, 0)
        result = self.checked(labels, streams, boundaries)
        metrics = result['postinitial']['arms']['rolling']
        self.assertEqual(metrics['missed_positive_cash'], 138)
        self.assertEqual(metrics['noncash_days'], 0)
        self.assertEqual(metrics['zero_action_labels'], 0)
        self.assertAlmostEqual(metrics['regret_sum'], 1.38)
        self.assertEqual(result['status'], 'ACTION_LABEL_UNSUPPORTED')

    def test_positive_negative_zero_action_labels_partition_only_noncash(self):
        labels, streams, boundaries = fixture()
        set_actions(streams, 'rolling', 1)
        labels[1]['y'][0], labels[2]['y'][0] = -.02, 0.
        result = self.checked(labels, streams, boundaries)
        metrics = result['full']['arms']['rolling']
        self.assertEqual(metrics['noncash_days'], 169)
        self.assertEqual(metrics['positive_action_labels'], 167)
        self.assertEqual(metrics['negative_action_labels'], 1)
        self.assertEqual(metrics['zero_action_labels'], 1)

    def test_predicted_selected_and_calibration_use_cash_zero(self):
        result = self.checked(*fixture())
        recent = result['full']['arms']['recent2025']
        rolling = result['full']['arms']['rolling']
        self.assertEqual(recent['predicted_selected_sum'], 0.)
        self.assertEqual(recent['calibration_gap_sum'], 0.)
        self.assertAlmostEqual(rolling['predicted_selected_sum'], 2.76)
        self.assertAlmostEqual(rolling['calibration_gap_sum'], 1.38)

    def test_regret_reduction_is_same_paired_identity_not_extra_evidence(self):
        result = self.checked(*fixture())
        for summary in [result['full'], result['postinitial']]+result['segments']:
            for comparison in summary['comparisons'].values():
                self.assertEqual(comparison['utility_delta'], comparison['regret_reduction'])
                self.assertEqual(comparison['unchanged']['utility_delta'], 0.)
                self.assertEqual(comparison['changed']['utility_delta'], comparison['utility_delta'])

    def test_direction_flip_is_one_target_difference(self):
        result = self.checked(*fixture())
        comparison = result['postinitial']['comparisons']['rolling_minus_old2021']
        flip = comparison['transitions']['-1:1']
        self.assertEqual(flip['days'], 138)
        self.assertAlmostEqual(flip['utility_delta'], 138*(.01-(-.01)))
        self.assertEqual(flip['positive_delta_days'], 138)
        self.assertEqual(flip['negative_delta_days'], 0)
        self.assertEqual(comparison['changed_days'], 138)

    def test_all_nine_transition_cells_partition_days_and_metrics(self):
        labels, streams, boundaries = fixture()
        for arm in ('recent2025', 'rolling'):
            set_actions(streams, arm, 0)
        pairs = [(before, after) for before in (-1, 0, 1) for after in (-1, 0, 1)]
        for i, (before, after) in enumerate(pairs):
            t = labels[i]['t']
            streams['recent2025'][i] = forecast(t, before)
            streams['rolling'][i] = forecast(t, after)
        result = self.checked(labels, streams, boundaries)
        comparison = result['full']['comparisons']['rolling_minus_recent2025']
        self.assertEqual(set(comparison['transitions']), {str(a)+':'+str(b) for a, b in pairs})
        self.assertEqual(comparison['changed_days'], 6)
        self.assertEqual(sum(c['days'] for c in comparison['transitions'].values()), 169)
        self.assertEqual(comparison['transitions']['0:0']['days'], 161)
        self.assertAlmostEqual(sum(c['mse_reduction_sum'] for c in comparison['transitions'].values()),
                               comparison['mse_reduction_sum'])

    def test_cash_transitions_separate_avoided_loss_and_missed_gain(self):
        labels, streams, boundaries = fixture()
        for arm in ('recent2025', 'rolling'):
            set_actions(streams, arm, 0)
        # Two 1->0 transitions: avoid a loss, then miss a gain.
        for i, target in enumerate((-.02, .01)):
            labels[i]['y'][0] = target
            streams['recent2025'][i] = forecast(labels[i]['t'], 1)
        # Two 0->1 transitions: create a gain, then create a loss.
        for i, target in ((2, .03), (3, -.04)):
            labels[i]['y'][0] = target
            streams['rolling'][i] = forecast(labels[i]['t'], 1)
        result = self.checked(labels, streams, boundaries)
        transitions = result['full']['comparisons']['rolling_minus_recent2025']['transitions']
        self.assertEqual(transitions['1:0']['positive_delta_sum'], .02)
        self.assertEqual(transitions['1:0']['negative_delta_sum'], -.01)
        self.assertEqual(transitions['0:1']['positive_delta_sum'], .03)
        self.assertEqual(transitions['0:1']['negative_delta_sum'], -.04)
        for key in ('1:0', '0:1'):
            cell = transitions[key]
            self.assertEqual(cell['positive_delta_days'], 1)
            self.assertEqual(cell['negative_delta_days'], 1)
            self.assertEqual(cell['zero_delta_days'], 0)
            self.assertAlmostEqual(cell['positive_delta_sum']+cell['negative_delta_sum'], cell['utility_delta'])

    def test_lower_mse_can_choose_a_worse_action(self):
        labels, streams, boundaries = fixture()
        set_actions(streams, 'recent2025', 1, values=[.2, -.2])
        set_actions(streams, 'rolling', -1, values=[0., .001])
        result = self.checked(labels, streams, boundaries)
        comparison = result['postinitial']['comparisons']['rolling_minus_recent2025']
        self.assertGreater(comparison['mse_reduction_sum'], 0)
        self.assertLess(comparison['utility_delta'], 0)
        self.assertLess(result['postinitial']['arms']['rolling']['utility_sum'], 0)
        self.assertEqual(result['status'], 'ACTION_LABEL_UNSUPPORTED')

    def test_same_actions_can_explain_all_mse_gain_and_zero_action_gain(self):
        labels, streams, boundaries = fixture()
        set_actions(streams, 'recent2025', 1, values=[.2, -.2])
        set_actions(streams, 'rolling', 1, values=[.011, -.01])
        result = self.checked(labels, streams, boundaries)
        comparison = result['postinitial']['comparisons']['rolling_minus_recent2025']
        self.assertEqual(comparison['changed_days'], 0)
        self.assertEqual(comparison['utility_delta'], 0.)
        self.assertGreater(comparison['unchanged']['mse_reduction_sum'], 0)
        self.assertEqual(comparison['unchanged']['mse_reduction_sum'], comparison['mse_reduction_sum'])
        self.assertEqual(comparison['unchanged']['zero_delta_days'], 138)
        self.assertEqual(result['status'], 'ACTION_LABEL_UNSUPPORTED')

    def test_one_profitable_month_cannot_support_actions(self):
        labels, streams, boundaries = fixture()
        for segment in range(2, 6):
            set_actions(streams, 'rolling', 0, segment=segment)
        result = self.checked(labels, streams, boundaries)
        self.assertEqual(result['support']['candidate_noncash_days'], 31)
        self.assertEqual(result['support']['positive_months'], 1)
        self.assertEqual(result['support']['without_best_month_delta'], 0.)
        self.assertEqual(result['support']['candidate_without_best_month_utility'], 0.)
        self.assertEqual(result['status'], 'ACTION_LABEL_UNSUPPORTED')

    def test_increment_positive_after_best_removal_does_not_waive_absolute_concentration(self):
        labels, streams, boundaries = fixture()
        set_actions(streams, 'recent2025', -1)
        for label in labels:
            if boundaries[1] <= label['t'] < boundaries[2]:
                label['y'] = [.02, -.01]
            elif label['t'] >= boundaries[2]:
                label['y'] = [-.005, -.01]
        result = self.checked(labels, streams, boundaries)
        support = result['support']
        self.assertGreater(support['candidate_utility_sum'], 0)
        self.assertEqual(support['positive_months'], 5)
        self.assertGreater(support['without_best_month_delta'], 0)
        self.assertLess(support['candidate_without_best_month_utility'], 0)
        self.assertFalse(support['conditions']['positive_absolute_without_best_month'])
        self.assertEqual(result['status'], 'ACTION_LABEL_UNSUPPORTED')

    def test_less_loss_increment_is_not_positive_action_utility(self):
        labels, streams, boundaries = fixture()
        set_actions(streams, 'recent2025', -1)
        for label in labels:
            label['y'] = [-.005, -.01]
        result = self.checked(labels, streams, boundaries)
        self.assertGreater(result['support']['utility_delta_total'], 0)
        self.assertLess(result['support']['candidate_utility_sum'], 0)
        self.assertFalse(result['support']['conditions']['positive_action_utility'])
        self.assertEqual(result['status'], 'ACTION_LABEL_UNSUPPORTED')

    def test_action_capacity_thirty_is_postinitial_not_full_calendar(self):
        for count in (29, 30):
            labels, streams, boundaries = fixture()
            set_actions(streams, 'rolling', 0)
            selected = [t for start in boundaries[1:-1] for t in range(start, start+6*DAY, DAY)][:count]
            for i, prediction in enumerate(streams['rolling']):
                if prediction['t'] in selected:
                    streams['rolling'][i] = forecast(prediction['t'], 1)
            with self.subTest(count=count):
                result = self.checked(labels, streams, boundaries)
                self.assertEqual(result['support']['candidate_noncash_days'], count)
                self.assertEqual(result['status'], 'ACTION_LABEL_IMPROVEMENT' if count == 30
                                 else 'ACTION_LABEL_UNSUPPORTED')

    def test_initial_month_actions_do_not_supply_postinitial_capacity(self):
        labels, streams, boundaries = fixture()
        set_actions(streams, 'rolling', 0)
        set_actions(streams, 'rolling', 1, segment=0)
        result = self.checked(labels, streams, boundaries)
        self.assertEqual(result['full']['arms']['rolling']['noncash_days'], 31)
        self.assertEqual(result['support']['candidate_noncash_days'], 0)
        self.assertEqual(result['status'], 'ACTION_LABEL_UNSUPPORTED')

    def test_isolated_negative_label_does_not_create_a_fake_risk_stop_or_truncation(self):
        labels, streams, boundaries = fixture()
        set_actions(streams, 'rolling', 1)
        labels[31]['y'][0] = -2.
        result = self.checked(labels, streams, boundaries)
        metrics = result['full']['arms']['rolling']
        self.assertEqual(metrics['noncash_days'], 169)
        self.assertEqual(metrics['negative_action_labels'], 1)
        self.assertAlmostEqual(metrics['utility_sum'], -2+168*.01)
        self.assertEqual(result['full']['count'], 169)
        self.assertFalse(result['portfolio_simulated'])
        self.assertFalse(result['tradable_returns'])
        self.assertNotIn('drawdown', metrics)
        self.assertNotIn('stopped', metrics)

    def test_all_forecast_action_ties_follow_frozen_action_rule(self):
        labels, streams, boundaries = fixture()
        values_actions = (([0., 0.], 0), ([-.01, -.01], 0), ([.01, .01], 1), ([.01, .02], -1))
        for values, direction in values_actions:
            set_actions(streams, 'rolling', direction, values=values)
            result = self.checked(labels, streams, boundaries)
            self.assertEqual(result['full']['arms']['rolling']['noncash_days'], 0 if direction == 0 else 169)

    def test_missing_duplicate_or_reordered_label_and_prediction_grids_rejected(self):
        labels, streams, boundaries = fixture()
        result = action.evaluate(labels, streams, boundaries)
        for case in ('label_first', 'label_last', 'label_duplicate', 'prediction_first', 'prediction_reordered'):
            bad_labels, bad_streams = copy.deepcopy(labels), copy.deepcopy(streams)
            if case == 'label_first':
                bad_labels.pop(0)
            elif case == 'label_last':
                bad_labels.pop()
            elif case == 'label_duplicate':
                bad_labels[1] = copy.deepcopy(bad_labels[0])
            elif case == 'prediction_first':
                bad_streams['rolling'].pop(0)
            else:
                bad_streams['rolling'].reverse()
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    action.evaluate(bad_labels, bad_streams, boundaries)
                with self.assertRaises(ValueError):
                    independent.audit(bad_labels, bad_streams, boundaries, result)

    def test_wrong_boundary_or_missing_control_rejected(self):
        labels, streams, boundaries = fixture()
        result = action.evaluate(labels, streams, boundaries)
        bad_boundaries = boundaries[:2]+[boundaries[2]+DAY]+boundaries[3:]
        with self.assertRaises(ValueError):
            action.evaluate(labels, streams, bad_boundaries)
        with self.assertRaises(ValueError):
            independent.audit(labels, streams, bad_boundaries, result)
        del streams['old2021']
        with self.assertRaises(ValueError):
            action.evaluate(labels, streams, boundaries)
        with self.assertRaises(ValueError):
            independent.audit(labels, streams, boundaries, result)

    def test_nonfinite_or_boolean_labels_and_predictions_rejected(self):
        labels, streams, boundaries = fixture()
        result = action.evaluate(labels, streams, boundaries)
        for invalid in (math.nan, math.inf, -math.inf, True, '.01'):
            for field in ('label', 'prediction'):
                bad_labels, bad_streams = copy.deepcopy(labels), copy.deepcopy(streams)
                if field == 'label':
                    bad_labels[0]['y'][0] = invalid
                else:
                    bad_streams['rolling'][0]['predicted_net_fraction'][0] = invalid
                with self.subTest(invalid=invalid, field=field):
                    with self.assertRaises(ValueError):
                        action.evaluate(bad_labels, bad_streams, boundaries)
                    with self.assertRaises(ValueError):
                        independent.audit(bad_labels, bad_streams, boundaries, result)

    def test_forecast_action_mismatch_and_invalid_identity_rejected(self):
        labels, streams, boundaries = fixture()
        result = action.evaluate(labels, streams, boundaries)
        for field, value in (('direction', 1), ('direction', False), ('model_id', ''),
                             ('t', float(labels[0]['t']))):
            bad = copy.deepcopy(streams)
            bad['rolling'][0][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    action.evaluate(labels, bad, boundaries)
                with self.assertRaises(ValueError):
                    independent.audit(labels, bad, boundaries, result)

    def test_audit_rejects_arm_score_oracle_and_calibration_mutations(self):
        labels, streams, boundaries = fixture()
        result = action.evaluate(labels, streams, boundaries)
        for field in ('utility_sum', 'utility_mean', 'regret_sum', 'predicted_selected_sum',
                      'calibration_gap_sum', 'pair_mse', 'noncash_days', 'zero_action_labels'):
            bad = copy.deepcopy(result)
            bad['postinitial']['arms']['rolling'][field] += 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                independent.audit(labels, streams, boundaries, bad)
        bad = copy.deepcopy(result)
        bad['full']['oracle_sum'] += 1
        with self.assertRaises(ValueError):
            independent.audit(labels, streams, boundaries, bad)

    def test_audit_rejects_transition_double_count_and_mse_misattribution(self):
        labels, streams, boundaries = fixture()
        result = action.evaluate(labels, streams, boundaries)
        for case in ('double_utility', 'missing_cell', 'false_sign_count', 'false_sign_sum', 'mse_group'):
            bad = copy.deepcopy(result)
            comparison = bad['postinitial']['comparisons']['rolling_minus_recent2025']
            if case == 'double_utility':
                comparison['transitions']['0:1']['utility_delta'] *= 2
            elif case == 'missing_cell':
                del comparison['transitions']['0:0']
            elif case == 'false_sign_count':
                comparison['changed']['positive_delta_days'] -= 1
            elif case == 'false_sign_sum':
                comparison['transitions']['0:1']['negative_delta_sum'] = -.1
            else:
                comparison['unchanged']['mse_reduction_sum'] += .01
            with self.subTest(case=case), self.assertRaises(ValueError):
                independent.audit(labels, streams, boundaries, bad)

    def test_audit_rejects_support_concentration_and_qualification_mutations(self):
        labels, streams, boundaries = fixture()
        result = action.evaluate(labels, streams, boundaries)
        for field in ('without_best_month_delta', 'candidate_without_best_month_utility',
                      'utility_delta_total', 'candidate_noncash_days', 'positive_months'):
            bad = copy.deepcopy(result)
            bad['support'][field] += 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                independent.audit(labels, streams, boundaries, bad)
        for field, value in action.LOCKS.items():
            bad = copy.deepcopy(result)
            bad[field] = not value if type(value) is bool else 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                independent.audit(labels, streams, boundaries, bad)

    def test_independent_audit_does_not_call_main_evaluator(self):
        labels, streams, boundaries = fixture()
        result = action.evaluate(labels, streams, boundaries)
        with mock.patch.object(action, 'evaluate', side_effect=AssertionError('forbidden evaluator')):
            with mock.patch.object(action, 'rational', side_effect=AssertionError('forbidden rational helper')):
                receipt = independent.audit(labels, streams, boundaries, result)
        self.assertEqual(receipt['action_cells'], 169*3)
        self.assertEqual(receipt['transition_cells'], 8*2*9)


if __name__ == '__main__':
    unittest.main()
