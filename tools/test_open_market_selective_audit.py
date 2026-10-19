"""Synthetic independent selective-policy checks; root owns formal execution."""
import copy
import unittest
from unittest import mock

import audit_open_market_selective as independent
import open_market_selective as selective

DAY, STEP = independent.DAY, independent.STEP
START, END = independent.START, independent.END
APPLY = independent.APPLY_NOT_BEFORE


def forecast(t, score, direction=1):
    values = [score, -1] if direction == 1 else [-1, score] if direction == -1 else [-1, 0]
    return dict(t=t, predicted_net_fraction=values, direction=direction)


def fixture():
    labels, predictions = [], []
    for i, t in enumerate(range(START, END, DAY)):
        score, action = i % 4+1, 1 if i % 2 == 0 else -1
        utility = score-3
        labels.append(dict(t=t, label_end=t+DAY-STEP,
                           y=[utility, -100] if action == 1 else [-100, utility]))
        predictions.append(forecast(t, score, action))
    return labels, predictions


def keep_proposals(predictions, indices):
    selected = set(indices)
    for i, row in enumerate(predictions):
        if i not in selected:
            predictions[i] = forecast(row['t'], 0, 0)


class SelectiveIndependentAuditTests(unittest.TestCase):
    def checked(self, labels=None, predictions=None):
        if labels is None:
            labels, predictions = fixture()
        calibration = selective.fit(labels, predictions, START, END)
        receipt = independent.audit_fit(labels, predictions, START, END, calibration)
        self.assertEqual(receipt['status'], 'INDEPENDENT_SELECTIVE_FIT_AUDIT_VERIFIED')
        self.assertEqual(receipt['decimal_precision'], 1600)
        return calibration

    def decisions(self, calibration=None):
        calibration = self.checked() if calibration is None else calibration
        predictions = [forecast(APPLY, 2), forecast(APPLY+DAY, 3, -1),
                       forecast(APPLY+2*DAY, 4, -1), forecast(APPLY+3*DAY, 0, 0)]
        decisions = selective.apply(calibration, predictions)
        receipt = independent.audit_apply(calibration, predictions, decisions)
        self.assertEqual(receipt['status'], 'INDEPENDENT_SELECTIVE_APPLY_AUDIT_VERIFIED')
        return calibration, predictions, decisions

    def test_exact_ols_rebuilds_selected_long_and_short_labels(self):
        calibration = self.checked()
        self.assertEqual(calibration['status'], 'ELIGIBLE_SELECTIVE_POLICY')
        self.assertEqual(calibration['alpha'], '-3')
        self.assertEqual(calibration['beta'], '1')
        self.assertEqual(calibration['raw_beta'], '1')
        self.assertEqual(calibration['proposed_days'], 91)
        self.assertEqual(calibration['kept_days'], 22)
        self.assertEqual(calibration['vetoed_days'], 69)

    def test_decimal_rebuild_spans_finite_float_exponents_without_losing_small_terms(self):
        labels, predictions = fixture()
        for i, (label, prediction) in enumerate(zip(labels, predictions)):
            score = (5e-324, 1e308, 1e-300, 1.7e308)[i % 4]
            direction = prediction['direction']
            predictions[i] = forecast(prediction['t'], score, direction)
            label['y'][0 if direction == 1 else 1] = -1 if i % 2 == 0 else 1
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['status'], 'ELIGIBLE_SELECTIVE_POLICY')

    def test_empty_or_extra_key_predictions_and_label_fields_rejected(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        with self.assertRaises(ValueError):
            independent.audit_fit(labels, [], START, END, calibration)
        altered = copy.deepcopy(predictions)
        altered[0]['model_id'] = 'not-in-frozen-schema'
        with self.assertRaises(ValueError):
            independent.audit_fit(labels, altered, START, END, calibration)
        altered_labels = copy.deepcopy(labels)
        altered_labels[0]['extra'] = 0
        with self.assertRaises(ValueError):
            independent.audit_fit(altered_labels, predictions, START, END, calibration)

    def test_independent_auditor_does_not_call_main_fit_or_apply(self):
        labels, predictions = fixture()
        calibration = selective.fit(labels, predictions, START, END)
        _, future, decisions = self.decisions(calibration)
        with (mock.patch.object(selective, 'fit', side_effect=AssertionError('MAIN_FIT_CALLED')),
              mock.patch.object(selective, 'apply', side_effect=AssertionError('MAIN_APPLY_CALLED'))):
            independent.audit_fit(labels, predictions, START, END, calibration)
            independent.audit_apply(calibration, future, decisions)

    def test_capacity_29_proposals_is_insufficient_and_zero_fits(self):
        labels, predictions = fixture()
        keep_proposals(predictions, list(range(0, 90, 3))[:29])
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['status'], 'CALIBRATION_CAPACITY_INSUFFICIENT')
        self.assertIn('INSUFFICIENT_PROPOSALS', calibration['reasons'])
        self.assertEqual(calibration['fit_count'], 0)
        self.assertIsNone(calibration['alpha'])

    def test_capacity_30_spread_proposals_is_admitted(self):
        labels, predictions = fixture()
        keep_proposals(predictions, range(0, 90, 3))
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['proposed_days'], 30)
        self.assertEqual(calibration['status'], 'ELIGIBLE_SELECTIVE_POLICY')

    def test_seven_monday_weeks_insufficient_even_with_49_proposals(self):
        labels, predictions = fixture()
        keep_proposals(predictions, range(3, 52))  # Monday through seventh Sunday.
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['proposed_weeks'], 7)
        self.assertEqual(calibration['reasons'], ['INSUFFICIENT_WEEKS'])
        self.assertEqual(calibration['fit_count'], 0)

    def test_eight_monday_weeks_is_admitted(self):
        labels, predictions = fixture()
        keep_proposals(predictions, range(3, 59))
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['proposed_weeks'], 8)
        self.assertEqual(calibration['status'], 'ELIGIBLE_SELECTIVE_POLICY')

    def test_week_starts_are_monday_not_rolling_seven_day_buckets(self):
        calibration = self.checked()
        self.assertEqual(calibration['proposed_week_starts'][0], START-4*DAY)
        self.assertEqual(calibration['proposed_week_starts'][1], START+3*DAY)
        self.assertEqual(calibration['proposed_weeks'], 14)

    def test_all_cash_is_capacity_insufficient_not_a_fitted_policy(self):
        labels, predictions = fixture()
        keep_proposals(predictions, [])
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['proposed_days'], 0)
        self.assertEqual(calibration['proposed_week_starts'], [])
        self.assertEqual(calibration['reasons'], ['INSUFFICIENT_PROPOSALS', 'INSUFFICIENT_WEEKS'])

    def test_cash_and_unselected_targets_do_not_enter_ols(self):
        labels, predictions = fixture()
        keep_proposals(predictions, range(80))
        calibration = self.checked(labels, predictions)
        for i, (label, prediction) in enumerate(zip(labels, predictions)):
            if not prediction['direction']:
                label['y'] = [1e300, -1e300]
            else:
                label['y'][1 if prediction['direction'] == 1 else 0] = 1e300
        independent.audit_fit(labels, predictions, START, END, calibration)
        self.assertEqual(selective.fit(labels, predictions, START, END), calibration)

    def test_zero_score_variance_has_null_raw_beta_and_no_policy(self):
        labels, predictions = fixture()
        for i, row in enumerate(predictions):
            predictions[i] = forecast(row['t'], 2, row['direction'])
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['beta'], '0')
        self.assertIsNone(calibration['raw_beta'])
        self.assertEqual(calibration['status'], 'NO_SELECTIVE_POLICY')
        self.assertIn('ZERO_SCORE_VARIANCE', calibration['reasons'])

    def test_negative_covariance_clamps_beta_without_reversing_ranking(self):
        labels, predictions = fixture()
        for label, prediction in zip(labels, predictions):
            label['y'][0 if prediction['direction'] == 1 else 1] *= -1
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['raw_beta'], '-1')
        self.assertEqual(calibration['beta'], '0')
        self.assertEqual(calibration['status'], 'NO_SELECTIVE_POLICY')

    def test_positive_beta_all_keep_is_no_selective_policy(self):
        labels, predictions = fixture()
        for label, prediction in zip(labels, predictions):
            label['y'][0 if prediction['direction'] == 1 else 1] += 10
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['beta'], '1')
        self.assertEqual(calibration['reasons'], ['NO_VETO_GROUP'])

    def test_positive_beta_all_veto_is_no_selective_policy(self):
        labels, predictions = fixture()
        for label, prediction in zip(labels, predictions):
            label['y'][0 if prediction['direction'] == 1 else 1] -= 10
        calibration = self.checked(labels, predictions)
        self.assertEqual(calibration['beta'], '1')
        self.assertEqual(calibration['reasons'], ['NO_KEEP_GROUP'])

    def test_coefficient_and_moment_mutations_are_rejected(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        for key, value in (('alpha', '0'), ('beta', '2'), ('raw_beta', '2')):
            changed = copy.deepcopy(calibration)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                independent.audit_fit(labels, predictions, START, END, changed)
        changed = copy.deepcopy(calibration)
        changed['moments']['cross_ss'] = '0'
        with self.assertRaises(ValueError):
            independent.audit_fit(labels, predictions, START, END, changed)

    def test_noncanonical_fraction_strings_are_rejected(self):
        calibration, predictions, decisions = self.decisions()
        for value in ('2/2', '01', '+1', '1/1', '1/-1', ' 1', '1.0', '1/0'):
            changed = copy.deepcopy(calibration)
            changed['beta'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                independent.audit_apply(changed, predictions, decisions)

    def test_source_selected_target_mutation_is_rejected(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        labels[0]['y'][0] += 1
        with self.assertRaises(ValueError):
            independent.audit_fit(labels, predictions, START, END, calibration)

    def test_source_prediction_mutation_is_rejected(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        predictions[0]['predicted_net_fraction'][0] += .125
        with self.assertRaises(ValueError):
            independent.audit_fit(labels, predictions, START, END, calibration)

    def test_label_grid_omission_duplicate_and_wrong_order_are_rejected(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        for altered in (labels[:-1], [labels[0]]+labels[:-1], list(reversed(labels))):
            with self.subTest(length=len(altered)), self.assertRaises(ValueError):
                independent.audit_fit(altered, predictions, START, END, calibration)

    def test_label_maturity_is_exact_not_only_less_than_cutoff(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        for maturity in (END+STEP, labels[0]['label_end']-STEP, True):
            altered = copy.deepcopy(labels)
            altered[0]['label_end'] = maturity
            with self.subTest(maturity=maturity), self.assertRaises(ValueError):
                independent.audit_fit(altered, predictions, START, END, calibration)

    def test_fit_scope_cannot_shift_or_drop_embargo_boundary(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        for start, end in ((START+DAY, END), (START, END+DAY), (float(START), END)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                independent.audit_fit(labels, predictions, start, end, calibration)

    def test_noncash_forecast_action_mismatch_and_boolean_action_rejected(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        for action in (-1, 0, True):
            altered = copy.deepcopy(predictions)
            altered[0]['direction'] = action
            with self.subTest(action=action), self.assertRaises(ValueError):
                independent.audit_fit(labels, altered, START, END, calibration)

    def test_positive_prediction_tie_selects_long(self):
        labels, predictions = fixture()
        predictions[0]['predicted_net_fraction'] = [1, 1]
        calibration = self.checked(labels, predictions)
        predictions[0]['direction'] = -1
        with self.assertRaises(ValueError):
            independent.audit_fit(labels, predictions, START, END, calibration)

    def test_nonfinite_prediction_or_even_unselected_label_rejected(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        for value in (float('nan'), float('inf'), -float('inf'), True):
            altered = copy.deepcopy(labels)
            altered[0]['y'][1] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                independent.audit_fit(altered, predictions, START, END, calibration)
            altered_p = copy.deepcopy(predictions)
            altered_p[0]['predicted_net_fraction'][1] = value
            with self.assertRaises(ValueError):
                independent.audit_fit(labels, altered_p, START, END, calibration)

    def test_source_metadata_or_counts_mutation_rejected(self):
        labels, predictions = fixture()
        calibration = self.checked(labels, predictions)
        for key, value in (('proposed_days', 90), ('proposed_weeks', 13),
                           ('kept_days', 23), ('fit_count', True), ('apply_not_before', END)):
            altered = copy.deepcopy(calibration)
            altered[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                independent.audit_fit(labels, predictions, START, END, altered)

    def test_apply_zero_is_veto_positive_keeps_and_cash_stays_cash(self):
        _, _, decisions = self.decisions()
        self.assertEqual([row['direction'] for row in decisions], [0, 0, -1, 0])
        self.assertEqual(decisions[1]['calibrated_utility'], '0')
        self.assertEqual(decisions[1]['reason'], 'VETO')
        self.assertIsNone(decisions[3]['selected_prediction'])
        self.assertIsNone(decisions[3]['calibrated_utility'])
        self.assertFalse(decisions[3]['vetoed'])

    def test_apply_rejects_pre_october2_and_noncontinuous_calendar(self):
        calibration, predictions, decisions = self.decisions()
        altered = copy.deepcopy(predictions)
        for row in altered:
            row['t'] -= DAY
        with self.assertRaises(ValueError):
            independent.audit_apply(calibration, altered, decisions)
        for altered in (predictions[::2], predictions[::-1], [predictions[0]]+predictions[:-1]):
            with self.assertRaises(ValueError):
                independent.audit_apply(calibration, altered, decisions)
        with self.assertRaises(ValueError):
            independent.audit_apply(calibration, predictions, decisions[:-1])

    def test_apply_rejects_new_action_flip_and_omitted_veto(self):
        calibration, predictions, decisions = self.decisions()
        for index, action in ((3, 1), (2, 1), (0, 1)):
            altered = copy.deepcopy(decisions)
            altered[index]['direction'] = action
            with self.subTest(index=index), self.assertRaises(ValueError):
                independent.audit_apply(calibration, predictions, altered)

    def test_apply_rejects_decision_value_clock_flags_and_extra_field(self):
        calibration, predictions, decisions = self.decisions()
        for key, value in (('calibrated_utility', '-2'), ('t', APPLY+DAY),
                           ('kept', True), ('vetoed', False), ('reason', 'KEEP'), ('extra', 0)):
            altered = copy.deepcopy(decisions)
            altered[0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                independent.audit_apply(calibration, predictions, altered)

    def test_apply_never_accepts_capacity_or_degenerate_status(self):
        labels, predictions = fixture()
        for eligible_count in (0, 29):
            altered = copy.deepcopy(predictions)
            keep_proposals(altered, list(range(0, 90, 3))[:eligible_count])
            calibration = self.checked(labels, altered)
            with self.assertRaises(ValueError):
                independent.audit_apply(calibration, [forecast(APPLY, 4)], [])
        for label, prediction in zip(labels, predictions):
            label['y'][0 if prediction['direction'] == 1 else 1] += 10
        calibration = self.checked(labels, predictions)
        with self.assertRaises(ValueError):
            independent.audit_apply(calibration, [forecast(APPLY, 4)], [])

    def test_arbitrarily_near_zero_sign_does_not_round_to_zero(self):
        # A standalone, algebraically consistent receipt exercises application
        # exactness only; it is not claimed to come from the fixture's labels.
        calibration = self.checked()
        denominator = 10**1700
        for sign in (-1, 1):
            altered = copy.deepcopy(calibration)
            alpha = independent.ratio(-denominator+sign, denominator)
            altered['alpha'] = independent.fraction_text(alpha)
            altered['moments']['mean_utility'] = independent.fraction_text(independent.plus(
                independent.parse(altered['moments']['mean_score']), alpha))
            predictions = [forecast(APPLY, 1)]
            decisions = selective.apply(altered, predictions)
            receipt = independent.audit_apply(altered, predictions, decisions)
            self.assertTrue(receipt['exact_boundary_verified'])
            self.assertEqual(decisions[0]['direction'], 1 if sign > 0 else 0)
            self.assertEqual(decisions[0]['calibrated_utility'], str(sign)+'/'+str(denominator))

    def test_audit_metadata_rejects_week_schema_and_noncanonical_moments(self):
        calibration, predictions, decisions = self.decisions()
        altered = copy.deepcopy(calibration)
        altered['proposed_week_starts'][0] += DAY
        with self.assertRaises(ValueError):
            independent.audit_apply(altered, predictions, decisions)
        altered = copy.deepcopy(calibration)
        altered['moments']['mean_score'] = '2/2'
        with self.assertRaises(ValueError):
            independent.audit_apply(altered, predictions, decisions)


if __name__ == '__main__':
    unittest.main()
