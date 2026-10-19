"""Synthetic-only fixed shadow-loss tests. Formal execution belongs to root gate."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import open_market_prediction_diagnostic as m


def forecast(t, values, identity):
    direction = 0 if max(values) <= 0 else (1 if values[0] >= values[1] else -1)
    return dict(t=t, predicted_net_fraction=list(values), direction=direction, model_id=identity)


def fixture():
    boundaries = list(m.BOUNDARIES)
    dates = list(range(boundaries[0], boundaries[-1], m.DAY))
    labels = [dict(t=t, label_end=t+m.DAY-300000, x={}, y=[0., 0.]) for t in dates]
    streams = {arm: [] for arm in m.ARMS}
    for t in dates:
        segment = next(i for i in range(6) if boundaries[i] <= t < boundaries[i+1])
        streams['old2021'].append(forecast(t, [.03, -.03], 'old-2021'))
        streams['recent2025'].append(forecast(t, [.02, -.02], 'recent-0'))
        error = .02 if segment == 0 else .01
        streams['rolling'].append(forecast(t, [error, -error], 'recent-'+str(segment)))
    return labels, streams, boundaries


def set_segment_error(streams, arm, segment, error, *, common=False):
    left, right = m.BOUNDARIES[segment:segment+2]
    for i, prediction in enumerate(streams[arm]):
        if left <= prediction['t'] < right:
            streams[arm][i] = forecast(prediction['t'], [error, error if common else -error],
                                       prediction['model_id'])


class PredictionDiagnosticTests(unittest.TestCase):
    def checked(self, labels, streams, boundaries):
        result = m.evaluate(labels, streams, boundaries)
        receipt = m.audit(labels, streams, boundaries, result)
        self.assertEqual(receipt['status'], 'INDEPENDENT_SHADOW_PREDICTION_AUDIT_VERIFIED')
        self.assertTrue(receipt['exact_decomposition_verified'])
        return result

    def test_full_fixed_grid_and_six_month_partition(self):
        result = self.checked(*fixture())
        self.assertEqual(result['full']['count'], 169)
        self.assertEqual(result['postinitial']['count'], 138)
        self.assertEqual([row['count'] for row in result['segments']], [31, 31, 30, 31, 30, 16])
        self.assertEqual(result['status'], 'SHADOW_PREDICTION_IMPROVEMENT')
        self.assertEqual(result['support']['positive_months'], 5)

    def test_pair_mse_uses_half_sum_not_sum(self):
        labels, streams, boundaries = fixture()
        for arm in m.ARMS:
            streams[arm] = [forecast(p['t'], [.2, -.1], p['model_id']) for p in streams[arm]]
        result = self.checked(labels, streams, boundaries)
        scores = result['full']['arms']['rolling']
        self.assertAlmostEqual(scores['pair_mse'], .025, places=15)
        self.assertAlmostEqual(scores['direction_mse'], .0225, places=15)
        self.assertAlmostEqual(scores['common_mse'], .0025, places=15)
        self.assertEqual(result['status'], 'SHADOW_PREDICTION_UNSUPPORTED')

    def test_direction_common_decomposition_with_nonzero_targets(self):
        labels, streams, boundaries = fixture()
        for label in labels:
            label['y'] = [.01, -.03]
        for arm in m.ARMS:
            streams[arm] = [forecast(p['t'], [.04, -.02], p['model_id']) for p in streams[arm]]
        result = self.checked(labels, streams, boundaries)
        scores = result['postinitial']['arms']['rolling']
        self.assertAlmostEqual(scores['pair_mse'], .0005, places=15)
        self.assertAlmostEqual(scores['direction_mse'], .0001, places=15)
        self.assertAlmostEqual(scores['common_mse'], .0004, places=15)

    def test_all_gain_in_one_month_is_not_support(self):
        labels, streams, boundaries = fixture()
        for segment in range(1, 6):
            set_segment_error(streams, 'rolling', segment, .02)
        set_segment_error(streams, 'rolling', 1, .01)
        result = self.checked(labels, streams, boundaries)
        self.assertGreater(result['support']['total_reduction'], 0)
        self.assertEqual(result['support']['positive_months'], 1)
        self.assertEqual(result['support']['without_best_month_reduction'], 0.)
        self.assertEqual(result['status'], 'SHADOW_PREDICTION_UNSUPPORTED')

    def test_three_positive_months_boundary_is_explicit(self):
        for positive_count in (2, 3):
            labels, streams, boundaries = fixture()
            for segment in range(1, 6):
                set_segment_error(streams, 'rolling', segment, .01 if segment <= positive_count else .02)
            with self.subTest(positive_months=positive_count):
                result = self.checked(labels, streams, boundaries)
                self.assertEqual(result['support']['positive_months'], positive_count)
                self.assertEqual(result['status'], 'SHADOW_PREDICTION_IMPROVEMENT' if positive_count == 3
                                 else 'SHADOW_PREDICTION_UNSUPPORTED')

    def test_three_positive_months_do_not_waive_total_negative(self):
        labels, streams, boundaries = fixture()
        for segment in (4, 5):
            set_segment_error(streams, 'rolling', segment, .1)
        result = self.checked(labels, streams, boundaries)
        self.assertEqual(result['support']['positive_months'], 3)
        self.assertLess(result['support']['total_reduction'], 0)
        self.assertEqual(result['status'], 'SHADOW_PREDICTION_UNSUPPORTED')

    def test_initial_month_gain_cannot_support_recurring_updates(self):
        labels, streams, boundaries = fixture()
        set_segment_error(streams, 'rolling', 0, 0.)
        for segment in range(1, 6):
            set_segment_error(streams, 'rolling', segment, .02)
        result = self.checked(labels, streams, boundaries)
        self.assertGreater(result['full']['paired_reduction']['recent2025_minus_rolling']['sum'], 0)
        self.assertEqual(result['postinitial']['paired_reduction']['recent2025_minus_rolling']['sum'], 0.)
        self.assertEqual(result['status'], 'SHADOW_PREDICTION_UNSUPPORTED')

    def test_old_control_is_descriptive_not_an_alternate_selection_target(self):
        labels, streams, boundaries = fixture()
        for segment in range(6):
            set_segment_error(streams, 'old2021', segment, 0.)
        result = self.checked(labels, streams, boundaries)
        self.assertLess(result['postinitial']['paired_reduction']['old2021_minus_rolling']['sum'], 0)
        self.assertEqual(result['status'], 'SHADOW_PREDICTION_IMPROVEMENT')
        self.assertEqual(result['support']['comparison'], 'recent2025_minus_rolling')

    def test_exact_zero_total_is_not_float_cancellation_support(self):
        labels, streams, boundaries = fixture()
        for segment in range(6):
            set_segment_error(streams, 'recent2025', segment, .01)
            set_segment_error(streams, 'rolling', segment, .01)
        first = (boundaries[1]-boundaries[0])//m.DAY
        p = streams['rolling'][first]
        streams['rolling'][first] = forecast(p['t'], [0., 0.], p['model_id'])
        p = streams['recent2025'][first+1]
        streams['recent2025'][first+1] = forecast(p['t'], [0., 0.], p['model_id'])
        result = self.checked(labels, streams, boundaries)
        self.assertEqual(result['support']['total_reduction'], 0.)
        self.assertFalse(result['support']['conditions']['positive_total'])
        self.assertEqual(result['status'], 'SHADOW_PREDICTION_UNSUPPORTED')

    def test_common_cost_only_improvement_does_not_establish_profit_or_direction(self):
        labels, streams, boundaries = fixture()
        for label in labels:
            label['y'] = [-.01, -.01]
        for segment in range(6):
            set_segment_error(streams, 'old2021', segment, -.04, common=True)
            set_segment_error(streams, 'recent2025', segment, -.03, common=True)
            set_segment_error(streams, 'rolling', segment, -.03 if segment == 0 else -.02, common=True)
        result = self.checked(labels, streams, boundaries)
        self.assertEqual(result['status'], 'SHADOW_PREDICTION_IMPROVEMENT')
        self.assertTrue(all(p['direction'] == 0 for p in streams['rolling']))
        self.assertEqual(result['postinitial']['arms']['rolling']['direction_mse'], 0.)
        self.assertFalse(result['economic_profit_established'])
        self.assertFalse(result['independent_confirmation'])
        self.assertFalse(result['statistical_significance_established'])
        self.assertFalse(result['online_learning_proven'])
        self.assertFalse(result['g4'])
        self.assertEqual(result['qualified_candidates'], 0)
        self.assertTrue(result['demo_opening_lock_unchanged'])

    def test_omitted_duplicate_or_reordered_labels_are_rejected(self):
        labels, streams, boundaries = fixture()
        result = m.evaluate(labels, streams, boundaries)
        variants = [labels[1:], labels[:-1], labels+[copy.deepcopy(labels[-1])], list(reversed(labels))]
        for bad in variants:
            with self.subTest(count=len(bad)):
                with self.assertRaises(ValueError):
                    m.evaluate(bad, streams, boundaries)
                with self.assertRaises(ValueError):
                    m.audit(bad, streams, boundaries, result)

    def test_prediction_grid_and_stream_set_are_exact(self):
        labels, streams, boundaries = fixture()
        result = m.evaluate(labels, streams, boundaries)
        for case in ('omit_first', 'omit_last', 'reorder', 'duplicate', 'missing_arm', 'extra_arm'):
            bad = copy.deepcopy(streams)
            if case == 'omit_first':
                bad['rolling'].pop(0)
            elif case == 'omit_last':
                bad['rolling'].pop()
            elif case == 'reorder':
                bad['rolling'].reverse()
            elif case == 'duplicate':
                bad['rolling'][1] = copy.deepcopy(bad['rolling'][0])
            elif case == 'missing_arm':
                del bad['old2021']
            else:
                bad['alternative'] = copy.deepcopy(bad['rolling'])
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    m.evaluate(labels, bad, boundaries)
                with self.assertRaises(ValueError):
                    m.audit(labels, bad, boundaries, result)

    def test_undeclared_period_or_boundaries_are_rejected(self):
        labels, streams, boundaries = fixture()
        result = m.evaluate(labels, streams, boundaries)
        variants = [boundaries[:-1], [boundaries[0]+m.DAY]+boundaries[1:],
                    boundaries[:2]+[boundaries[2]+m.DAY]+boundaries[3:]]
        for bad in variants:
            with self.assertRaises(ValueError):
                m.evaluate(labels, streams, bad)
            with self.assertRaises(ValueError):
                m.audit(labels, streams, bad, result)

    def test_nonfinite_bool_and_non_numeric_cells_are_rejected(self):
        labels, streams, boundaries = fixture()
        result = m.evaluate(labels, streams, boundaries)
        for invalid in (math.nan, math.inf, -math.inf, True, '.01'):
            for target in ('labels', 'predictions'):
                bad_labels, bad_streams = copy.deepcopy(labels), copy.deepcopy(streams)
                if target == 'labels':
                    bad_labels[0]['y'][0] = invalid
                else:
                    bad_streams['rolling'][0]['predicted_net_fraction'][0] = invalid
                with self.subTest(invalid=invalid, target=target):
                    with self.assertRaises(ValueError):
                        m.evaluate(bad_labels, bad_streams, boundaries)
                    with self.assertRaises(ValueError):
                        m.audit(bad_labels, bad_streams, boundaries, result)

    def test_action_identity_and_integer_clocks_are_checked(self):
        labels, streams, boundaries = fixture()
        result = m.evaluate(labels, streams, boundaries)
        for case in ('action', 'bool_action', 'identity', 'clock'):
            bad = copy.deepcopy(streams)
            p = bad['rolling'][0]
            if case == 'action':
                p['direction'] = -1
            elif case == 'bool_action':
                p['direction'] = True
            elif case == 'identity':
                p['model_id'] = ''
            else:
                p['t'] = float(p['t'])
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    m.evaluate(labels, bad, boundaries)
                with self.assertRaises(ValueError):
                    m.audit(labels, bad, boundaries, result)

    def test_audit_rejects_decomposition_score_and_support_mutations(self):
        labels, streams, boundaries = fixture()
        result = m.evaluate(labels, streams, boundaries)
        for case in ('decomposition', 'pair_mse', 'monthly', 'positive_count', 'leave_best', 'status'):
            bad = copy.deepcopy(result)
            if case == 'decomposition':
                bad['full']['arms']['rolling']['common_mse'] += .0001
            elif case == 'pair_mse':
                bad['postinitial']['arms']['rolling']['pair_mse'] += .0001
            elif case == 'monthly':
                bad['support']['monthly_contribution_sums'][0] += .0001
            elif case == 'positive_count':
                bad['support']['positive_months'] -= 1
            elif case == 'leave_best':
                bad['support']['without_best_month_reduction'] += .0001
            else:
                bad['status'] = 'ECONOMIC_PASS'
            with self.subTest(case=case), self.assertRaises(ValueError):
                m.audit(labels, streams, boundaries, bad)

    def test_all_qualification_lock_mutations_are_rejected(self):
        labels, streams, boundaries = fixture()
        result = m.evaluate(labels, streams, boundaries)
        for key, value in m.LOCKS.items():
            bad = copy.deepcopy(result)
            bad[key] = not value if type(value) is bool else 1
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.audit(labels, streams, boundaries, bad)

    def test_missing_segment_and_unexpected_output_fields_are_rejected(self):
        labels, streams, boundaries = fixture()
        result = m.evaluate(labels, streams, boundaries)
        bad = copy.deepcopy(result)
        bad['segments'].pop()
        with self.assertRaises(ValueError):
            m.audit(labels, streams, boundaries, bad)
        bad = dict(result, portfolio_profit=100.)
        with self.assertRaises(ValueError):
            m.audit(labels, streams, boundaries, bad)

    def test_independent_audit_does_not_call_evaluator_or_rational_helpers(self):
        labels, streams, boundaries = fixture()
        result = m.evaluate(labels, streams, boundaries)
        with mock.patch.object(m, 'evaluate', side_effect=AssertionError('forbidden evaluator')):
            with mock.patch.object(m, 'rational', side_effect=AssertionError('forbidden rational helper')):
                receipt = m.audit(labels, streams, boundaries, result)
        self.assertEqual(receipt['dates'], 169)
        self.assertEqual(receipt['loss_cells'], 169*3*3)

    def test_initialization_creates_absent_nested_directory_and_never_overwrites(self):
        import run_open_market_prediction_diagnostic as runner

        def ready_read(path):
            if path.name == 'result.json':
                return {'status': 'BATCH_COMPLETED'}
            if path.name == 'freeze.json':
                return {'files': {'frozen-source': 'fixed-hash'}}
            raise AssertionError('Unexpected initialization read: '+str(path))

        with tempfile.TemporaryDirectory(prefix='prediction-diagnostic-init-') as temporary:
            parent = Path(temporary)/'parent'
            parent.mkdir()
            destination = parent/'absent'/'diagnostic'
            with mock.patch.object(runner, 'ROOT', destination), \
                    mock.patch.object(runner.prior, 'ROOT', parent), \
                    mock.patch.object(runner.prior, 'active'), \
                    mock.patch.object(runner.prior, 'preserve', return_value=0), \
                    mock.patch.object(runner, 'read', side_effect=ready_read), \
                    mock.patch.object(runner, 'sha', return_value='fixed-hash'), \
                    mock.patch('builtins.print'):
                self.assertFalse(destination.exists())
                runner.initialize()
                scope_before = (destination/'scope.json').read_bytes()
                protected_before = (destination/'protected.json').read_bytes()
                self.assertEqual(json.loads(scope_before)['portfolio_paths'], 0)
                self.assertEqual(json.loads(scope_before)['fits'], 0)
                self.assertEqual(json.loads(protected_before),
                                 {'files': {'frozen-source': 'fixed-hash'}})
                with self.assertRaises(FileExistsError):
                    runner.initialize()
                self.assertEqual((destination/'scope.json').read_bytes(), scope_before)
                self.assertEqual((destination/'protected.json').read_bytes(), protected_before)


if __name__ == '__main__':
    unittest.main()
