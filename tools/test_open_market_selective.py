"""Synthetic-only selective-calibration tests; root owns formal execution."""
import copy
import math
import unittest
from fractions import Fraction as F
from unittest import mock

import open_market_selective as s


def forecast(t, score=2, direction=1):
    values = [score, -1] if direction == 1 else [-1, score] if direction == -1 else [-1, 0]
    return dict(t=t, predicted_net_fraction=values, direction=direction)


def fixture(selected=None):
    labels, predictions = [], []
    selected = set(range(91)) if selected is None else set(selected)
    for i, t in enumerate(range(s.CALIBRATION_START, s.CALIBRATION_END, s.DAY)):
        score, direction = 1 + i % 2, -1 if i % 3 == 0 else 1
        predictions.append(forecast(t, score, direction) if i in selected else forecast(t, direction=0))
        utility = score - 1.5
        values = [utility, 99] if direction == 1 else [99, utility]
        labels.append(dict(t=t, label_end=t+s.DAY-s.STEP, x={}, y=values))
    return labels, predictions


def fitted():
    return s.fit(*fixture(), s.CALIBRATION_START, s.CALIBRATION_END)


class SelectiveTests(unittest.TestCase):
    def test_exact_known_affine_coefficients(self):
        c = fitted()
        self.assertEqual(c['status'], s.ELIGIBLE)
        self.assertEqual((c['alpha'], c['beta'], c['raw_beta']), ('-3/2', '1', '1'))
        self.assertEqual((c['kept_days'], c['vetoed_days'], c['fit_count']), (45, 46, 1))
        self.assertEqual(c['reasons'], [])

    def test_exact_91_day_metadata_and_canonical_moments(self):
        c = fitted()
        self.assertEqual(set(c), s.CALIBRATION_KEYS)
        self.assertEqual(c['calendar_days'], 91)
        self.assertEqual(c['proposed_days'], 91)
        self.assertEqual(c['proposed_weeks'], 14)
        self.assertEqual(c['apply_not_before'], s.APPLY_NOT_BEFORE)
        for value in [c['alpha'], c['beta'], c['raw_beta'], *c['moments'].values()]:
            self.assertEqual(str(F(value)), value)

    def test_selected_directions_are_pooled_not_separate_models(self):
        labels, predictions = fixture()
        for p, label in zip(predictions, labels):
            p['predicted_net_fraction'].reverse()
            p['direction'] *= -1
            label['y'].reverse()
        self.assertEqual(s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END), fitted())

    def test_cash_targets_do_not_enter_fit(self):
        labels, predictions = fixture(selected=range(0, 91, 2))
        a = s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)
        for label, p in zip(labels, predictions):
            if p['direction'] == 0:
                label['y'] = [1e100, -1e100]
        self.assertEqual(a, s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END))

    def test_unselected_direction_target_does_not_enter_fit(self):
        labels, predictions = fixture()
        for label, p in zip(labels, predictions):
            label['y'][1 if p['direction'] == 1 else 0] = -1e90
        self.assertEqual(s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END), fitted())

    def test_fewer_than_30_proposals_does_not_fit(self):
        c = s.fit(*fixture(range(0, 87, 3)), s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertEqual(c['status'], s.CAPACITY)
        self.assertEqual(c['proposed_days'], 29)
        self.assertEqual(c['fit_count'], 0)
        for key in ('alpha', 'beta', 'raw_beta', 'moments', 'kept_days', 'vetoed_days'):
            self.assertIsNone(c[key])
        self.assertEqual(c['reasons'], ['INSUFFICIENT_PROPOSALS'])

    def test_exactly_30_and_eight_weeks_admitted_before_degeneracy(self):
        # First 26 days span five UTC weeks; the final dates extend to eight.
        indices = list(range(26)) + [31, 38, 45, 46]
        c = s.fit(*fixture(indices), s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertEqual(c['proposed_days'], 30)
        self.assertEqual(c['proposed_weeks'], 8)
        self.assertEqual(c['fit_count'], 1)
        self.assertEqual(c['status'], s.ELIGIBLE)

    def test_many_proposals_in_fewer_than_eight_weeks_do_not_fit(self):
        c = s.fit(*fixture(range(30)), s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertEqual(c['status'], s.CAPACITY)
        self.assertEqual(c['fit_count'], 0)
        self.assertEqual(c['reasons'], ['INSUFFICIENT_WEEKS'])

    def test_all_cash_capacity_zero_no_division_or_fake_fit(self):
        c = s.fit(*fixture([]), s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertEqual(c['proposed_week_starts'], [])
        self.assertEqual(c['fit_count'], 0)
        self.assertEqual(c['reasons'], ['INSUFFICIENT_PROPOSALS', 'INSUFFICIENT_WEEKS'])

    def test_monday_utc_week_boundary(self):
        friday = s.CALIBRATION_START
        self.assertEqual(s.monday(friday), friday-4*s.DAY)
        self.assertEqual(s.monday(friday+2*s.DAY), friday-4*s.DAY)
        self.assertEqual(s.monday(friday+3*s.DAY), friday+3*s.DAY)

    def test_zero_score_variance_is_no_policy(self):
        labels, predictions = fixture()
        for p in predictions:
            p['predicted_net_fraction'][0 if p['direction'] == 1 else 1] = 1
        c = s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertEqual(c['status'], s.NO_POLICY)
        self.assertEqual(c['beta'], '0')
        self.assertIsNone(c['raw_beta'])
        self.assertIn('ZERO_SCORE_VARIANCE', c['reasons'])

    def test_negative_slope_clipped_to_zero(self):
        labels, predictions = fixture()
        for row in labels:
            row['y'] = [-v for v in row['y']]
        c = s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertEqual((c['raw_beta'], c['beta']), ('-1', '0'))
        self.assertEqual(c['status'], s.NO_POLICY)
        self.assertIn('NONPOSITIVE_SLOPE', c['reasons'])

    def test_zero_slope_is_no_policy(self):
        labels, predictions = fixture()
        for row in labels:
            row['y'] = [.01, .01]
        c = s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertEqual(c['beta'], '0')
        self.assertEqual(c['status'], s.NO_POLICY)

    def test_positive_slope_but_all_keep_is_no_policy(self):
        labels, predictions = fixture()
        for row in labels:
            row['y'] = [v+2 for v in row['y']]
        c = s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertGreater(F(c['beta']), 0)
        self.assertEqual(c['vetoed_days'], 0)
        self.assertEqual(c['status'], s.NO_POLICY)
        self.assertIn('NO_VETO_GROUP', c['reasons'])

    def test_positive_slope_but_all_veto_is_no_policy(self):
        labels, predictions = fixture()
        for row in labels:
            row['y'] = [v-2 for v in row['y']]
        c = s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertEqual(c['kept_days'], 0)
        self.assertEqual(c['status'], s.NO_POLICY)
        self.assertIn('NO_KEEP_GROUP', c['reasons'])

    def test_wrong_calibration_domain_rejected(self):
        labels, predictions = fixture()
        for start, end in ((s.CALIBRATION_START+s.DAY, s.CALIBRATION_END),
                           (s.CALIBRATION_START, s.CALIBRATION_END+s.DAY),
                           (float(s.CALIBRATION_START), s.CALIBRATION_END)):
            with self.assertRaises(ValueError):
                s.fit(labels, predictions, start, end)

    def test_missing_duplicate_or_reordered_inputs_rejected(self):
        for which in ('label_missing', 'label_duplicate', 'label_reverse',
                      'prediction_missing', 'prediction_duplicate', 'prediction_reverse'):
            labels, predictions = fixture()
            if which == 'label_missing': labels.pop()
            if which == 'label_duplicate': labels[1] = copy.deepcopy(labels[0])
            if which == 'label_reverse': labels.reverse()
            if which == 'prediction_missing': predictions.pop()
            if which == 'prediction_duplicate': predictions[1] = copy.deepcopy(predictions[0])
            if which == 'prediction_reverse': predictions.reverse()
            with self.subTest(which=which), self.assertRaises(ValueError):
                s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)

    def test_label_end_must_match_completed_source_bar(self):
        for change in (-s.STEP, s.STEP, s.DAY):
            labels, predictions = fixture()
            labels[-1]['label_end'] += change
            with self.assertRaises(ValueError):
                s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)

    def test_schema_and_action_mismatches_rejected(self):
        for change in ('extra_prediction', 'wrong_action', 'boolean_action', 'bad_shape', 'extra_label'):
            labels, predictions = fixture()
            if change == 'extra_prediction': predictions[0]['model_id'] = 'unexpected'
            if change == 'wrong_action': predictions[0]['direction'] = 0
            if change == 'boolean_action': predictions[0]['direction'] = True
            if change == 'bad_shape': predictions[0]['predicted_net_fraction'] = [1]
            if change == 'extra_label': labels[0]['unexpected'] = 1
            with self.subTest(change=change), self.assertRaises(ValueError):
                s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)

    def test_nonfinite_and_boolean_numerics_rejected_everywhere(self):
        for value in (math.nan, math.inf, -math.inf, True, '.01'):
            for field in ('prediction', 'selected_label', 'other_label'):
                labels, predictions = fixture()
                if field == 'prediction': predictions[0]['predicted_net_fraction'][1] = value
                elif field == 'selected_label': labels[0]['y'][1] = value
                else: labels[0]['y'][0] = value
                with self.subTest(value=value, field=field), self.assertRaises(ValueError):
                    s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)

    def test_apply_preserves_cash_and_never_adds_or_flips(self):
        predictions = [forecast(s.APPLY_NOT_BEFORE+i*s.DAY, score, direction)
                       for i, (score, direction) in enumerate(((2, 1), (2, -1), (1, 1), (1, -1), (0, 0)))]
        result = s.apply(fitted(), predictions)
        self.assertEqual([r['direction'] for r in result], [1, -1, 0, 0, 0])
        self.assertEqual([r['reason'] for r in result], ['KEEP', 'KEEP', 'VETO', 'VETO', 'CASH'])
        self.assertIsNone(result[-1]['calibrated_utility'])
        self.assertIsNone(result[-1]['selected_prediction'])
        self.assertFalse(result[-1]['kept'])
        self.assertFalse(result[-1]['vetoed'])

    def test_apply_equal_zero_veto_and_nearby_exact_sides(self):
        predictions = [forecast(s.APPLY_NOT_BEFORE+i*s.DAY, score)
                       for i, score in enumerate((1.5, 1.5000000000000002, 1.4999999999999998))]
        result = s.apply(fitted(), predictions)
        self.assertEqual([r['direction'] for r in result], [0, 1, 0])
        self.assertEqual(result[0]['calibrated_utility'], '0')
        self.assertEqual(F(result[1]['calibrated_utility']), F(1, 5*10**15))

    def test_apply_positive_prediction_tie_uses_original_long(self):
        p = dict(t=s.APPLY_NOT_BEFORE, predicted_net_fraction=[2, 2], direction=1)
        self.assertEqual(s.apply(fitted(), [p])[0]['direction'], 1)

    def test_apply_zero_prediction_tie_remains_cash(self):
        p = dict(t=s.APPLY_NOT_BEFORE, predicted_net_fraction=[0, 0], direction=0)
        self.assertEqual(s.apply(fitted(), [p])[0]['reason'], 'CASH')

    def test_apply_fixed_october_two_embargo(self):
        for t in (s.CALIBRATION_START, s.CALIBRATION_END):
            with self.assertRaises(ValueError):
                s.apply(fitted(), [forecast(t)])
        self.assertEqual(s.apply(fitted(), [forecast(s.APPLY_NOT_BEFORE)])[0]['t'], s.APPLY_NOT_BEFORE)

    def test_apply_only_eligible_policy(self):
        c = fitted()
        for status in (s.CAPACITY, s.NO_POLICY, 'UNKNOWN'):
            bad = dict(c, status=status)
            with self.assertRaises(ValueError):
                s.apply(bad, [forecast(s.APPLY_NOT_BEFORE)])

    def test_apply_empty_gapped_reordered_non_midnight_grid_rejected(self):
        for predictions in ([], [forecast(s.APPLY_NOT_BEFORE), forecast(s.APPLY_NOT_BEFORE+2*s.DAY)],
                            [forecast(s.APPLY_NOT_BEFORE+s.DAY), forecast(s.APPLY_NOT_BEFORE)],
                            [forecast(s.APPLY_NOT_BEFORE+s.STEP)]):
            with self.assertRaises(ValueError):
                s.apply(fitted(), predictions)

    def test_noncanonical_fraction_strings_rejected(self):
        for value in ('2/2', '1.0', '+1', ' 1', '1/0', 1., True):
            c = fitted()
            c['beta'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                s.apply(c, [forecast(s.APPLY_NOT_BEFORE)])

    def test_calibration_schema_counts_and_moment_mutations_rejected(self):
        for change in ('alpha', 'raw_beta', 'mean_score', 'count', 'week', 'fit_count', 'clock', 'extra', 'reason'):
            c = fitted()
            if change == 'alpha': c['alpha'] = '0'
            if change == 'raw_beta': c['raw_beta'] = '2'
            if change == 'mean_score': c['moments']['mean_score'] = '0'
            if change == 'count': c['kept_days'] += 1
            if change == 'week': c['proposed_week_starts'][0] += s.DAY
            if change == 'fit_count': c['fit_count'] = 0
            if change == 'clock': c['apply_not_before'] = s.CALIBRATION_END
            if change == 'extra': c['extra'] = True
            if change == 'reason': c['reasons'] = ['unexpected']
            with self.subTest(change=change), self.assertRaises(ValueError):
                s.apply(c, [forecast(s.APPLY_NOT_BEFORE)])

    def test_inputs_are_not_mutated(self):
        labels, predictions = fixture()
        original = copy.deepcopy((labels, predictions))
        c = s.fit(labels, predictions, s.CALIBRATION_START, s.CALIBRATION_END)
        self.assertEqual((labels, predictions), original)
        application = [forecast(s.APPLY_NOT_BEFORE)]
        before = copy.deepcopy((c, application))
        s.apply(c, application)
        self.assertEqual((c, application), before)

    def test_apply_does_not_refit(self):
        c = fitted()
        with mock.patch.object(s, 'fit', side_effect=AssertionError('no refit')):
            self.assertEqual(s.apply(c, [forecast(s.APPLY_NOT_BEFORE)])[0]['direction'], 1)

    def test_synthetic_fit_apply_existing_execution_and_decimal_risk_pipeline(self):
        import open_market_execution as execution
        import audit_open_market_execution as ledger
        from test_open_market_execution import fixture as execution_fixture, START

        calibration = s.fit(*fixture(), s.CALIBRATION_START, s.CALIBRATION_END)
        rows, fs, _ = execution_fixture(days=4)
        predictions = [forecast(START+i*s.DAY, score, direction)
                       for i, (score, direction) in enumerate(((2, 1), (1, 1), (2, 1), (0, 0)))]
        decisions = s.apply(calibration, predictions)
        actions = {row['t']: row['direction'] for row in decisions}
        self.assertEqual(list(actions.values()), [1, 0, 1, 0])
        ordinary = execution.simulate(rows, fs, actions, START, START+4*s.DAY)
        ledger.audit(ordinary, rows, fs, actions)
        for book in ordinary['books']:
            self.assertEqual([e['t'] for e in book['events'] if e['kind'] == 'entry_request'],
                             [START, START+2*s.DAY])
        # The later kept proposal cannot override a genuine first-day hard stop.
        rows[3]['mark_low'] = 6000.
        stopped = execution.simulate(rows, fs, actions, START, START+4*s.DAY)
        ledger.audit(stopped, rows, fs, actions)
        for book in stopped['books']:
            self.assertIsNotNone(book['stopped'])
            self.assertEqual(sum(e['kind'] == 'entry_request' for e in book['events']), 1)
        self.assertEqual(stopped['bars'], 4*s.DAY//s.STEP)


if __name__ == '__main__':
    unittest.main()
