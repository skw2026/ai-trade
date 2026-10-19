"""Synthetic regression and mutation tests; formal execution belongs to root gate."""
import copy
import math
import unittest
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR

import audit_open_market_learning as audit
import open_market_learning as learner

DAY, STEP = 86400000, 300000


def raw_fixture(days=18):
    rows = []
    previous = 10000.
    for i in range(days*288):
        price = 10000+3*math.sin(i*.11)+2*math.cos(i*.013)+i*.003
        rows.append(dict(timestamp=i*STEP, open=previous, price=price,
                         high=max(previous, price)+.37, low=min(previous, price)-.31,
                         volume=100+(i % 17), mark_open=previous, mark_close=price,
                         mark_high=max(previous, price)+.9, mark_low=min(previous, price)-.9,
                         funding_rate_per_interval=.0001 if i % 96 == 0 else 0.))
        previous = price
    return rows


def training_fixture(count=90):
    out = []
    for i in range(count):
        p = [math.sin(i*.17+j*.31)+math.cos(i*.11*(j+1)) for j in range(8)]
        volume = math.sin(i*.29)
        stale = math.cos((i-7)*.29)
        x = {'P': p, 'PV': p+[volume, p[1]*volume, p[2]*volume],
             'STALE': p+[stale, p[1]*stale, p[2]*stale]}
        out.append(dict(t=i*DAY, label_end=(i+1)*DAY-STEP, x=x,
                        y=[.003*p[0]-.002*p[2]+.001*volume-.0007,
                           -.003*p[0]+.002*p[2]-.001*volume-.0007]))
    return out


def economic_path(net=200., start=1736985600000, end=1767225600000):
    def book():
        return dict(net=net, insufficient=None, quantity=0., stopped=None,
                    cycles=[dict(net=net/30, complete=True) for _ in range(30)],
                    active_weeks=list(range(12)),
                    days=[dict(t=1751328000000, equity=10000+net/2),
                          dict(t=1767225600000, equity=10000+net)])
    return dict(start=start, end=end, bars=(end-start)//STEP, books=[book(), book()])


def supported_verdict_fixture():
    paths = {}
    for period in ('2021', '2025'):
        start, end = ((1633132800000, 1640822400000) if period == '2021' else
                      (1736985600000, 1767225600000))
        for arm, net in (('P', 60.), ('PV', 200.), ('STALE', 70.)):
            paths[period+'-'+arm] = economic_path(net, start, end)
    paths['2025-PV-delay'] = economic_path(160.)
    selection = {arm: dict(eligible=True, ridge=1.) for arm in learner.ARMS}
    return paths, selection


class FeatureLabelAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = raw_fixture()
        cls.start, cls.end = 16*DAY, 17*DAY

    def prepared(self, rows=None, end=None):
        rows = self.rows if rows is None else rows
        end = self.end if end is None else end
        fs = learner.features(rows, self.start, end)
        samples = learner.samples(rows, fs, self.start, end)
        return fs, samples

    def test_all_feature_cells_and_decimal_labels(self):
        fs, samples = self.prepared()
        result = audit.audit_features_and_labels(self.rows, fs, samples, self.start, self.end)
        self.assertEqual(result['feature_cells'], 30)
        self.assertEqual(result['labels'], 2)
        self.assertEqual(samples[0]['label_end'], self.start+DAY-STEP)

    def test_features_only_is_explicit_not_empty_labels(self):
        fs, _ = self.prepared()
        result = audit.audit_features_and_labels(self.rows, fs, None, self.start, self.end)
        self.assertTrue(result['features_only'])
        with self.assertRaisesRegex(ValueError, 'AUDIT_LABEL_GRID'):
            audit.audit_features_and_labels(self.rows, fs, [], self.start, self.end)

    def test_future_price_and_volume_cannot_change_current_features(self):
        before, old_samples = self.prepared()
        changed = copy.deepcopy(self.rows)
        for r in changed:
            if r['timestamp'] >= self.start:
                for key in ('price', 'open', 'high', 'low', 'mark_open'):
                    r[key] *= 1.02
                r['volume'] *= 5
        after, new_samples = self.prepared(changed)
        self.assertEqual(before, after)
        self.assertNotEqual(old_samples[0]['y'], new_samples[0]['y'])
        audit.audit_features_and_labels(changed, after, new_samples, self.start, self.end)

    def test_last_closed_bar_is_used_not_decision_bar(self):
        fs, _ = self.prepared()
        by = {r['timestamp']: r for r in self.rows}
        self.assertEqual(fs[self.start]['price'], by[self.start-STEP]['price'])
        self.assertNotEqual(fs[self.start]['price'], by[self.start]['price'])

    def test_volume_previous_seven_days_excludes_recent_day(self):
        before, _ = self.prepared()
        changed = copy.deepcopy(self.rows)
        for r in changed:
            if self.start-DAY <= r['timestamp'] < self.start:
                r['volume'] *= 2
        after, samples = self.prepared(changed)
        self.assertAlmostEqual(after[self.start]['volume_log_ratio']-
                               before[self.start]['volume_log_ratio'], math.log(2), places=12)
        self.assertEqual(after[self.start]['stale_volume_log_ratio'],
                         before[self.start]['stale_volume_log_ratio'])
        self.assertEqual(after[self.start]['x']['P'], before[self.start]['x']['P'])
        audit.audit_features_and_labels(changed, after, samples, self.start, self.end)

    def test_stale_volume_uses_old_condition_but_current_return(self):
        fs, _ = self.prepared()
        f = fs[self.start]
        self.assertAlmostEqual(f['x']['STALE'][9], f['x']['P'][1]*f['stale_volume_log_ratio'])
        self.assertAlmostEqual(f['x']['STALE'][10], f['x']['P'][2]*f['stale_volume_log_ratio'])

    def test_unmatured_exit_bar_is_excluded_and_exact_end_admitted(self):
        excluded = self.start+DAY-2*STEP
        fs, samples = self.prepared(end=excluded)
        self.assertEqual(samples, [])
        audit.audit_features_and_labels(self.rows, fs, samples, self.start, excluded)
        admitted = excluded+STEP
        fs, samples = self.prepared(end=admitted)
        self.assertEqual(len(samples), 1)
        audit.audit_features_and_labels(self.rows, fs, samples, self.start, admitted)

    def test_funding_excludes_entry_includes_exit_excludes_later(self):
        changed = copy.deepcopy(self.rows)
        by = {r['timestamp']: r for r in changed}
        entry, leave = self.start+STEP, self.start+DAY-2*STEP
        for r in changed:
            r['funding_rate_per_interval'] = 0.
        before, a = self.prepared(changed)
        by[entry]['funding_rate_per_interval'] = .3
        by[leave+STEP]['funding_rate_per_interval'] = .4
        fs, b = self.prepared(changed)
        self.assertEqual(a[0]['y'], b[0]['y'])
        by[leave]['funding_rate_per_interval'] = .001
        fs, c = self.prepared(changed)
        self.assertLess(c[0]['y'][0], b[0]['y'][0])
        self.assertGreater(c[0]['y'][1], b[0]['y'][1])
        audit.audit_features_and_labels(changed, fs, c, self.start, self.end)

    def test_tick_and_quantity_rounding_match_cash_wallet(self):
        changed = copy.deepcopy(self.rows)
        by = {r['timestamp']: r for r in changed}
        for r in changed:
            r['funding_rate_per_interval'] = 0.
        entry, leave = self.start+STEP, self.start+DAY-2*STEP
        by[entry]['high'], by[entry]['low'] = 10007.13, 9998.37
        by[leave]['high'], by[leave]['low'] = 10017.27, 10002.11
        fs, samples = self.prepared(changed)
        q = (D(2500)/D(str(fs[self.start]['price']))/D('.001')).to_integral_value(
            rounding=ROUND_FLOOR)*D('.001')
        buy = (D('10007.13')*D('1.0004')/D('.1')).to_integral_value(rounding=ROUND_CEILING)*D('.1')
        sell = (D('10002.11')*D('.9996')/D('.1')).to_integral_value(rounding=ROUND_FLOOR)*D('.1')
        expected = (q*(sell-buy)-q*D('.0011')*(buy+sell))/2500
        self.assertAlmostEqual(samples[0]['y'][0], float(expected), places=13)
        audit.audit_features_and_labels(changed, fs, samples, self.start, self.end)

    def test_feature_mutation_rejected(self):
        fs, samples = self.prepared()
        fs = copy.deepcopy(fs)
        fs[self.start]['x']['PV'][10] += .01
        with self.assertRaisesRegex(ValueError, 'AUDIT_FEATURE_VALUE'):
            audit.audit_features_and_labels(self.rows, fs, samples, self.start, self.end)

    def test_label_cash_mutation_rejected(self):
        fs, samples = self.prepared()
        samples = copy.deepcopy(samples)
        samples[0]['y'][0] += .000001
        with self.assertRaisesRegex(ValueError, 'AUDIT_LABEL_CASH'):
            audit.audit_features_and_labels(self.rows, fs, samples, self.start, self.end)

    def test_label_maturity_and_grid_mutation_rejected(self):
        fs, samples = self.prepared()
        bad = copy.deepcopy(samples)
        bad[0]['label_end'] -= STEP
        with self.assertRaisesRegex(ValueError, 'AUDIT_LABEL_MATURITY'):
            audit.audit_features_and_labels(self.rows, fs, bad, self.start, self.end)
        with self.assertRaisesRegex(ValueError, 'AUDIT_FEATURE_GRID'):
            audit.audit_features_and_labels(self.rows, {}, samples, self.start, self.end)

    def test_missing_raw_context_and_duplicate_bar_rejected(self):
        fs, samples = self.prepared()
        with self.assertRaisesRegex(ValueError, 'AUDIT_RAW_GRID'):
            audit.audit_features_and_labels(self.rows[1:100]+self.rows[101:], fs, samples,
                                            self.start, self.end)
        with self.assertRaisesRegex(ValueError, 'AUDIT_RAW_IDENTITY'):
            audit.audit_features_and_labels(self.rows+[self.rows[-1]], fs, samples,
                                            self.start, self.end)

    def test_zero_volume_is_not_a_zero_signal(self):
        changed = copy.deepcopy(self.rows)
        for r in changed:
            if self.start-DAY <= r['timestamp'] < self.start:
                r['volume'] = 0.
        with self.assertRaisesRegex(ValueError, 'ZERO_VOLUME_CONTEXT'):
            learner.features(changed, self.start, self.end)


class RidgePredictionAuditTest(unittest.TestCase):
    def test_all_arms_and_regularizations_have_independent_normal_equations(self):
        samples = training_fixture()
        for arm in learner.ARMS:
            for ridge in learner.RIDGES:
                with self.subTest(arm=arm, ridge=ridge):
                    model = learner.fit(samples, arm, ridge)
                    result = audit.audit_fit(samples, model)
                    self.assertTrue(result['normalized_normal_equations'])

    def test_mean_normalized_ridge_not_sum_normalized(self):
        samples = training_fixture()
        for i, row in enumerate(samples):
            x = -1. if i % 2 else 1.
            row['x']['P'] = [x]+[0.]*7
            row['y'] = [2*x+3, -x+4]
        model = learner.fit(samples, 'P', 1.)
        self.assertAlmostEqual(model['coefficients'][0][0], 1., places=12)
        self.assertAlmostEqual(model['coefficients'][1][0], -.5, places=12)
        self.assertEqual(model['intercept'], [3., 4.])
        audit.audit_fit(samples, model)

    def test_constant_columns_are_zero_and_intercept_unpenalized(self):
        samples = training_fixture()
        for row in samples:
            row['x']['P'] = [2.]*8
            row['y'] = [.003, -.005]
        model = learner.fit(samples, 'P', 10.)
        self.assertEqual(model['coefficients'], [[0.]*8, [0.]*8])
        self.assertAlmostEqual(model['intercept'][0], .003, places=14)
        audit.audit_fit(samples, model)

    def test_fit_rejects_nonfinite_and_small_sample(self):
        with self.assertRaisesRegex(ValueError, 'FIT_SCOPE'):
            learner.fit(training_fixture(59), 'P', 1.)
        samples = training_fixture()
        samples[0]['y'][0] = math.nan
        with self.assertRaisesRegex(ValueError, 'NONFINITE_FIT'):
            learner.fit(samples, 'P', 1.)

    def test_audit_rejects_coefficients_scalers_intercept_and_training_metadata(self):
        samples = training_fixture()
        model = learner.fit(samples, 'PV', .1)
        for field, reason in (('coefficients', 'NORMAL_EQUATION'), ('center', 'FIT_CENTER'),
                              ('scale', 'FIT_SCALE'), ('intercept', 'FIT_INTERCEPT'),
                              ('label_cutoff', 'FIT_METADATA')):
            bad = copy.deepcopy(model)
            if field == 'coefficients':
                bad[field][0][0] += .001
            elif field == 'label_cutoff':
                bad[field] += DAY
            else:
                bad[field][0] += .01
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, reason):
                audit.audit_fit(samples, bad)

    def prediction_fixture(self):
        samples = training_fixture()
        model = learner.fit(samples, 'PV', 1.)
        fs = {t: dict(x=copy.deepcopy(samples[i]['x']), latest_price_closed_at=t)
              for i, t in enumerate(range(90*DAY, 93*DAY, DAY))}
        predictions = {t: learner.predict(model, f, t) for t, f in fs.items()}
        return model, fs, predictions

    def test_complete_prediction_grid_and_actions(self):
        model, fs, predictions = self.prediction_fixture()
        self.assertEqual(audit.audit_predictions(model, fs, predictions)['predictions'], 3)
        audit.audit_predictions(model, fs, list(predictions.values()))

    def test_future_evaluation_features_do_not_refit_training_scalers(self):
        model, fs, _ = self.prediction_fixture()
        frozen = copy.deepcopy(model)
        for f in fs.values():
            f['x']['PV'] = [v*10000 for v in f['x']['PV']]
        predictions = {t: learner.predict(model, f, t) for t, f in fs.items()}
        self.assertEqual(model, frozen)
        audit.audit_predictions(model, fs, predictions)

    def test_prediction_mutations_missing_dates_values_action_and_clocks(self):
        model, fs, predictions = self.prediction_fixture()
        first = min(fs)
        bad = copy.deepcopy(predictions)
        del bad[first]
        with self.assertRaisesRegex(ValueError, 'PREDICTION_GRID'):
            audit.audit_predictions(model, fs, bad)
        bad = copy.deepcopy(predictions)
        bad[first]['predicted_net_fraction'][0] += .01
        with self.assertRaisesRegex(ValueError, 'PREDICTION_VALUE'):
            audit.audit_predictions(model, fs, bad)
        bad = copy.deepcopy(predictions)
        bad[first]['direction'] = 2
        with self.assertRaisesRegex(ValueError, 'PREDICTION_ACTION'):
            audit.audit_predictions(model, fs, bad)
        future = copy.deepcopy(model)
        future['label_cutoff'] = first+STEP
        with self.assertRaisesRegex(ValueError, 'PREDICTION_CLOCK'):
            audit.audit_predictions(future, fs, predictions)
        with self.assertRaisesRegex(ValueError, 'PREDICTION_CLOCK'):
            learner.predict(future, fs[first], first)

    def test_exact_action_ties_zero_and_negative_go_to_declared_actions(self):
        samples = training_fixture()
        model = learner.fit(samples, 'P', 1.)
        model['coefficients'] = [[0.]*8, [0.]*8]
        t = 100*DAY
        fs = {t: dict(x=samples[0]['x'], latest_price_closed_at=t)}
        for values, expected in (([0., 0.], 0), ([-1., -1.], 0),
                                 ([.1, .1], 1), ([.1, .2], -1)):
            model['intercept'] = values
            p = learner.predict(model, fs[t], t)
            self.assertEqual(p['direction'], expected)
            audit.audit_predictions(model, fs, [p])


class SelectionVerdictTest(unittest.TestCase):
    def test_selection_ties_choose_stronger_regularization(self):
        results = [dict(economic_path(10.), ridge=r) for r in learner.RIDGES]
        self.assertEqual(learner.select(results)['ridge'], 10.)

    def test_unprofitable_but_safe_selection_is_allowed(self):
        results = [dict(economic_path(-10.), ridge=r) for r in learner.RIDGES]
        self.assertTrue(learner.select(results)['eligible'])

    def test_stopped_paths_do_not_beat_eligible_paths(self):
        results = [dict(economic_path(10.), ridge=r) for r in learner.RIDGES]
        results[0]['books'][1]['net'] = 1000.
        results[0]['books'][1]['stopped'] = dict(reason='RISK')
        self.assertEqual(learner.select(results)['ridge'], 10.)
        for result in results:
            result['books'][1]['stopped'] = dict(reason='RISK')
        choice = learner.select(results)
        self.assertFalse(choice['eligible'])
        self.assertEqual(choice['ridge'], .1)

    def test_partial_selection_grid_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'SELECTION_SET'):
            learner.select([dict(economic_path(), ridge=1.)])

    def test_supported_is_development_only_never_promotion(self):
        paths, selection = supported_verdict_fixture()
        result = learner.verdict(paths, selection)
        self.assertEqual(result['status'], 'DEVELOPMENT_SUPPORTED')
        self.assertFalse(result['independent_confirmation'])
        self.assertFalse(result['g4'])
        self.assertFalse(result['online_learning_proven'])
        self.assertEqual(result['qualified_candidates'], 0)
        self.assertTrue(result['demo_opening_lock_unchanged'])

    def test_economic_nonpositive_and_no_increment_are_negative_exits(self):
        for case in ('nonpositive', 'price_tie', 'stale_tie', 'selection_risk'):
            paths, selection = supported_verdict_fixture()
            if case == 'nonpositive':
                paths['2025-PV']['books'][0]['net'] = 0.
            elif case == 'price_tie':
                paths['2025-P']['books'][1]['net'] = 200.
            elif case == 'stale_tie':
                paths['2025-STALE']['books'][1]['net'] = 200.
            else:
                selection['PV']['eligible'] = False
            with self.subTest(case=case):
                self.assertEqual(learner.verdict(paths, selection)['status'], 'NET_INCREMENT_UNSUPPORTED')

    def test_candidate_stops_unknown_and_residual_are_not_support(self):
        for field, value in (('stopped', {'reason': 'RISK'}), ('insufficient', 'MARK'), ('quantity', .01)):
            paths, selection = supported_verdict_fixture()
            paths['2025-PV']['books'][1][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(learner.verdict(paths, selection)['status'], 'DEVELOPMENT_SUPPORTED')

    def test_incomplete_control_cannot_certify_increment(self):
        for arm in ('P', 'STALE'):
            paths, selection = supported_verdict_fixture()
            paths['2025-'+arm]['books'][1]['insufficient'] = 'MISSING_MARK'
            with self.subTest(arm=arm):
                self.assertNotEqual(learner.verdict(paths, selection)['status'], 'DEVELOPMENT_SUPPORTED')

    def test_calendar_holes_and_unequal_control_windows_are_incomplete(self):
        for case in ('bars', 'price_start', 'stale_end', 'delay_start'):
            paths, selection = supported_verdict_fixture()
            if case == 'bars':
                paths['2025-PV']['bars'] -= 1
            elif case == 'price_start':
                paths['2021-P']['start'] += DAY
                paths['2021-P']['bars'] -= DAY//STEP
            elif case == 'stale_end':
                paths['2025-STALE']['end'] -= DAY
                paths['2025-STALE']['bars'] -= DAY//STEP
            else:
                paths['2025-PV-delay']['start'] += DAY
                paths['2025-PV-delay']['bars'] -= DAY//STEP
            with self.subTest(case=case):
                self.assertEqual(learner.verdict(paths, selection)['status'],
                                 'COMPARISON_EVIDENCE_INCOMPLETE')

    def test_risk_stopped_but_complete_control_is_not_candidate_precondition(self):
        paths, selection = supported_verdict_fixture()
        paths['2025-P']['books'][1]['stopped'] = {'reason': 'RISK'}
        selection['P']['eligible'] = False
        self.assertEqual(learner.verdict(paths, selection)['status'], 'DEVELOPMENT_SUPPORTED')

    def test_partial_winning_cycle_cannot_be_omitted_from_concentration(self):
        paths, selection = supported_verdict_fixture()
        stress = paths['2025-PV']['books'][1]
        stress['cycles'] = [dict(net=-2/3, complete=True) for _ in range(30)]
        stress['cycles'].append(dict(net=220., complete=False))
        self.assertAlmostEqual(sum(c['net'] for c in stress['cycles']), stress['net'])
        self.assertNotEqual(learner.verdict(paths, selection)['status'], 'DEVELOPMENT_SUPPORTED')

    def test_capacity_effect_delay_and_half_year_are_not_promotion(self):
        for case in ('cycles', 'weeks', 'effect', 'delay', 'half'):
            paths, selection = supported_verdict_fixture()
            stress = paths['2025-PV']['books'][1]
            if case == 'cycles':
                stress['cycles'] = stress['cycles'][:29]
            elif case == 'weeks':
                stress['active_weeks'] = list(range(11))
            elif case == 'effect':
                stress['net'] = 99.
            elif case == 'delay':
                paths['2025-PV-delay']['books'][1]['net'] = 0.
            else:
                stress['days'][0]['equity'] = 9999.
            with self.subTest(case=case):
                self.assertNotEqual(learner.verdict(paths, selection)['status'], 'DEVELOPMENT_SUPPORTED')


if __name__ == '__main__':
    unittest.main()
