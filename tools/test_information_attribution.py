import copy
import math
import unittest

import information_attribution as model
import audit_information_attribution as audit

DAY = model.DAY


def fixture(start, count, relation=1, signal=True):
    rows=[]
    for i in range(count):
        j=start+i
        x=[math.sin(j*.41),math.cos(j*.29),math.sin(j*.11),math.cos(j*.73) if signal else 0.]
        rows.append(dict(t=j*DAY,label_end=(j+1)*DAY,x=x,y=2*x[0]-x[1]+relation*20*x[3]))
    return rows


class InformationAttributionTest(unittest.TestCase):
    def test_known_increment_and_independent_decimal(self):
        training=fixture(0,120); evaluation=fixture(120,80)
        result=model.compare(training,evaluation)
        self.assertGreater(result['metrics']['increment_mse_reduction_percent'],80)
        audit.regression(training,evaluation,result['models'],result['predictions'])
        audit.metrics(result['predictions'],result['metrics'])

    def test_no_signal_is_exact_same_price_model(self):
        result=model.compare(fixture(0,120,signal=False),fixture(120,80,signal=False))
        self.assertAlmostEqual(result['metrics']['increment_mse_reduction_bps_squared'],0,places=12)
        self.assertEqual(model.classify([result['metrics']]*3),'LINEAR_INCREMENT_NOT_STABLE')

    def test_reversal_not_reselected(self):
        training=fixture(0,120)
        regular=model.compare(training,fixture(120,80))
        reversed_result=model.compare(training,fixture(120,80,relation=-1))
        self.assertEqual(regular['models'],reversed_result['models'])
        self.assertLess(reversed_result['metrics']['increment_mse_reduction_bps_squared'],0)

    def test_evaluation_labels_cannot_change_coefficients_or_predictions(self):
        training=fixture(0,120); evaluation=fixture(120,80)
        before=model.compare(training,evaluation)
        for row in evaluation: row['y']+=9999
        after=model.compare(training,evaluation)
        self.assertEqual(before['models'],after['models'])
        for a,b in zip(before['predictions'],after['predictions']):
            for mode in model.MODES: self.assertEqual(a[mode],b[mode])

    def test_evaluation_features_do_not_set_training_scalers(self):
        training=fixture(0,120); evaluation=fixture(120,80)
        before=model.compare(training,evaluation)
        for row in evaluation: row['x']=[v*10000 for v in row['x']]
        self.assertEqual(before['models'],model.compare(training,evaluation)['models'])

    def test_nonfinite_input_rejected(self):
        rows=fixture(0,120); rows[2]['x'][3]=math.nan
        with self.assertRaisesRegex(ValueError,'NONFINITE'): model.fit(rows,'increment')
        rows=fixture(0,120); rows[2]['y']=math.inf
        with self.assertRaisesRegex(ValueError,'NONFINITE'): model.fit(rows,'price')

    def test_zero_variance_and_collinearity_are_defined(self):
        rows=fixture(0,120)
        for row in rows: row['x']=[2.,3.,4.,2.]
        result=model.compare(rows,rows[:0]+[dict(r,t=r['t']+120*DAY,label_end=r['label_end']+120*DAY) for r in rows])
        self.assertEqual(result['models']['increment']['coefficients'],[0.,0.,0.,0.])

    def test_redundant_feature_gain_is_not_information_or_profit_certification(self):
        training=fixture(0,120); evaluation=fixture(120,80)
        for row in training+evaluation: row['x'][3]=row['x'][0]; row['y']=20*row['x'][0]
        result=model.compare(training,evaluation)
        # A redundant ridge column can reduce shrinkage bias: do not call this causal alpha.
        self.assertGreater(result['metrics']['increment_mse_reduction_percent'],0)
        self.assertIsNone(result['metrics']['profit_estimate'])
        self.assertFalse(model.boundaries()['promotion_eligible'])

    def test_training_label_must_mature_before_evaluation(self):
        rows=fixture(0,120); rows[-1]['label_end']+=DAY
        with self.assertRaisesRegex(ValueError,'OVERLAP'): model.compare(rows,fixture(120,80))

    def test_duplicate_date_rejected(self):
        rows=fixture(0,120); rows[1]['t']=rows[0]['t']
        with self.assertRaisesRegex(ValueError,'DUPLICATE'): model.compare(rows,fixture(120,80))

    def test_insufficient_rows_are_not_zero_performance(self):
        result=model.compare(fixture(0,59),fixture(60,40))
        self.assertEqual(result['status'],'INSUFFICIENT_MATCHED_ROWS')
        self.assertEqual(result['models'],{})

    def price_fixture(self):
        closes={i*DAY:100+math.exp(i/100) for i in range(-10,31)}
        features={i*DAY:dict(z=1.,latest_price_closed_at=i*DAY) for i in range(20)}
        return closes,features

    def test_sample_clock_and_label_decimal(self):
        closes,features=self.price_fixture()
        sample=model.build_samples(closes,features,0,20*DAY,3)
        self.assertEqual(len(sample['rows']),18)
        self.assertEqual(sample['unmatured_dates'],[18*DAY,19*DAY])
        audit.samples(sample['rows'],closes,3)

    def test_missing_signal_removes_same_dates_from_all_models(self):
        closes,features=self.price_fixture(); features[5*DAY]['z']=None
        sample=model.build_samples(closes,features,0,20*DAY,1)
        self.assertEqual(sample['unavailable_dates'],[5*DAY])
        self.assertNotIn(5*DAY,[r['t'] for r in sample['rows']])

    def test_future_price_changes_labels_not_current_features(self):
        closes,features=self.price_fixture()
        before=model.build_samples(closes,features,0,20*DAY,3)['rows'][0]
        closes[3*DAY]*=2
        after=model.build_samples(closes,features,0,20*DAY,3)['rows'][0]
        self.assertEqual(before['x'],after['x']); self.assertNotEqual(before['y'],after['y'])

    def test_missing_price_is_not_silently_dropped(self):
        closes,features=self.price_fixture(); del closes[20*DAY]
        with self.assertRaisesRegex(ValueError,'LABEL_PRICE'): model.build_samples(closes,features,0,20*DAY,1)

    def test_future_feature_clock_rejected(self):
        closes,features=self.price_fixture(); features[0]['latest_price_closed_at']=DAY
        with self.assertRaisesRegex(ValueError,'FEATURE_CLOCK'): model.build_samples(closes,features,0,20*DAY,1)

    def test_half_boundary_drops_unmatured_labels(self):
        rows=fixture(0,10)
        for r in rows: r['label_end']=r['t']+3*DAY
        self.assertEqual([r['t']//DAY for r in model.window(rows,0,5*DAY)],[0,1,2])

    def test_placebo_is_causal_fixed_weekly_and_has_both_signs(self):
        values=[model.placebo_sign(i*7*DAY) for i in range(30)]
        self.assertEqual(set(values),{-1,1})
        self.assertEqual(model.placebo_sign(7*DAY),model.placebo_sign(13*DAY))

    def test_hac_uses_calendar_gap_not_compressed_index(self):
        distant=model.hac7_se([(0,1.),(10*DAY,-1.),(20*DAY,1.)])
        adjacent=model.hac7_se([(0,1.),(DAY,-1.),(2*DAY,1.)])
        self.assertNotEqual(distant,adjacent)

    def test_audit_rejects_tampered_coefficient(self):
        train=fixture(0,120); test=fixture(120,80); r=model.compare(train,test)
        r['models']['increment']['coefficients'][3]+=1
        with self.assertRaisesRegex(ValueError,'AUDIT'): audit.regression(train,test,r['models'],r['predictions'])

    def test_all_three_stages_required_and_no_promotion(self):
        good=model.compare(fixture(0,120),fixture(120,80))['metrics']
        bad=copy.deepcopy(good); bad['increment_mse_reduction_bps_squared']=-1
        self.assertEqual(model.classify([good,good,bad]),'LINEAR_INCREMENT_NOT_STABLE')
        self.assertEqual(model.classify([good]*3),'DESCRIPTIVE_INCREMENT_ONLY')
        self.assertFalse(model.boundaries()['g4']); self.assertEqual(model.boundaries()['trading_paths_executed'],0)


if __name__=='__main__': unittest.main()
