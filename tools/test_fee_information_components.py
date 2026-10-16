import copy
import math
import unittest
from unittest.mock import patch

import audit_fee_information_components as audit
import fee_information_components as components
import information_attribution as original


def artifact(duplicate=False):
    rows = []
    for i in range(204):
        x = [math.sin(i*.73), math.cos(i*.31), math.sin(i*.17)+2]
        z = x[0] if duplicate else .7*x[0]+math.cos(i*1.19)
        y = 4*x[0]-3*x[1]+2*z+math.sin(i*.41)
        rows.append(dict(t=i*original.DAY,label_end=(i+1)*original.DAY,x=x+[z],y=y))
    train,evaluation = rows[:120],rows[120:]
    return dict(train=train,evaluation=evaluation,result=original.compare(train,evaluation))


class FeeComponentsTest(unittest.TestCase):
    def test_exact_identity_and_independent_audit(self):
        a = artifact();r = components.decompose(a)
        rows = audit.checked_rows(a,r)
        summary = components.summarize(r['rows'],120*original.DAY,204*original.DAY)
        audit.check_summary(rows,120*original.DAY,204*original.DAY,summary)
        for row in r['rows']:
            self.assertAlmostEqual(row['delta'],row['u']+row['v'])
            self.assertAlmostEqual(row['total_gain'],row['residual_gain']+row['price_gain'])

    def test_duplicate_has_no_residual_information(self):
        a = artifact(duplicate=True);r = components.decompose(a)
        audit.checked_rows(a,r)
        self.assertLess(r['training_residual_variance'],1e-20)
        self.assertTrue(all(abs(row['u']) < 1e-10 for row in r['rows']))
        self.assertGreater(max(abs(row['v']) for row in r['rows']),.01)

    def test_training_labels_never_read(self):
        a = artifact();r = components.decompose(a)
        for row in a['train']: row['y'] = float('nan')
        self.assertEqual(r,components.decompose(a))

    def test_evaluation_labels_do_not_change_projection(self):
        a = artifact();r = components.decompose(a)
        for row,p in zip(a['evaluation'],a['result']['predictions']):
            row['y'] = p['y'] = -3000
        new = components.decompose(a)
        self.assertEqual(new['projection'],r['projection'])
        self.assertEqual([row['u'] for row in new['rows']],[row['u'] for row in r['rows']])

    def test_never_calls_predictive_fit(self):
        a = artifact()
        with patch.object(original,'fit',side_effect=AssertionError('NO_REFIT')):
            components.decompose(a)

    def test_zero_signal_coefficient(self):
        a = artifact();base = a['result']['models']['price']
        a['result']['models']['increment']['coefficients'] = base['coefficients']+[0.]
        for row in a['result']['predictions']:row['increment'] = row['price']
        r = components.decompose(a);audit.checked_rows(a,r)
        self.assertTrue(all(row['delta'] == row['u'] == row['v'] == 0 for row in r['rows']))

    def test_prediction_tamper_rejected(self):
        a = artifact();a['result']['predictions'][0]['increment'] += 10
        with self.assertRaisesRegex(ValueError,'IDENTITY_MISMATCH'):components.decompose(a)

    def test_model_preprocessing_tamper_rejected(self):
        a = artifact();a['result']['models']['increment']['center'][0] += 1
        with self.assertRaisesRegex(ValueError,'FROZEN_MODEL_CONTRACT'):components.decompose(a)

    def test_nonfinite_input_rejected(self):
        a = artifact();a['train'][0]['x'][0] = float('inf')
        with self.assertRaisesRegex(ValueError,'INPUT_SHAPE_FINITE'):components.decompose(a)

    def test_singular_price_geometry_stops(self):
        a = artifact()
        for row in a['train']+a['evaluation']:row['x'][1] = row['x'][0]
        a['result'] = original.compare(a['train'],a['evaluation'])
        with self.assertRaisesRegex(ValueError,'SINGULAR_MATRIX'):components.decompose(a)

    def test_missing_days_not_compressed(self):
        a = artifact()
        del a['evaluation'][10:20];del a['result']['predictions'][10:20]
        r = components.decompose(a);rows = audit.checked_rows(a,r)
        s = components.summarize(r['rows'],120*original.DAY,204*original.DAY)
        audit.check_summary(rows,120*original.DAY,204*original.DAY,s)
        self.assertEqual([v['removed_rows'] for v in s['components']['total_gain']['leave_block_out']],[18,28,28])

    def test_partial_block_and_boundary_labels(self):
        a = artifact();r = components.decompose(a)
        s = components.summarize(r['rows'],120*original.DAY,190*original.DAY)
        self.assertEqual(s['rows'],70)
        self.assertEqual([v['removed_rows'] for v in s['components']['total_gain']['leave_block_out']],[28,28,14])

    def test_deleted_block_can_flip_sign(self):
        r = components.decompose(artifact())
        for i,row in enumerate(r['rows']):row['total_gain'] = 5 if i < 28 else -1
        s = components.summarize(r['rows'],120*original.DAY,204*original.DAY)
        self.assertEqual(s['components']['total_gain']['opposite_sign_blocks'],1)

    def test_audit_detects_component_tamper(self):
        a = artifact();r = components.decompose(a);r['rows'][0]['u'] += 1
        with self.assertRaises(AssertionError):audit.checked_rows(a,r)

    def test_audit_detects_summary_tamper(self):
        a = artifact();r = components.decompose(a);rows = audit.checked_rows(a,r)
        s = components.summarize(r['rows'],120*original.DAY,204*original.DAY)
        s['components']['total_gain']['hac7_se_bps_squared'] += 1
        with self.assertRaises(AssertionError):audit.check_summary(rows,120*original.DAY,204*original.DAY,s)

    def test_cost_scale_is_not_qualification(self):
        r = components.decompose(artifact())
        for row in r['rows']:row.update(increment=30,u=15,delta=31)
        s = components.summarize(r['rows'],120*original.DAY,204*original.DAY)
        self.assertEqual(s['forecast_scale']['u']['above_15bps'],0)
        self.assertEqual(s['forecast_scale']['increment']['above_30bps'],0)
        self.assertEqual(s['forecast_scale']['delta']['above_30bps'],84)
        self.assertFalse(s['g4']);self.assertIsNone(s['profit_estimate'])
        self.assertEqual(s['cost_status'],'COST_HEADROOM_NOT_ESTABLISHED')


if __name__ == '__main__':unittest.main()
