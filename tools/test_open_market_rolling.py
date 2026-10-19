"""Synthetic independent-governance and endpoint tests; run only via root gate."""
import copy
import unittest

import audit_open_market_rolling as audit
import open_market_rolling_decision as decision
import open_market_rolling_execution as execution
from test_open_market_rolling_execution import DAY, STEP, START, fixture, model


def scheduled(offsets, initial_direction=1):
    return [dict(t=START+offset*DAY, model_id='m'+str(offset),
                 model=model(initial_direction if offset == 0 else -1, START+offset*DAY))
            for offset in offsets]


class GovernanceAuditTests(unittest.TestCase):
    def run_case(self, days=4, offsets=(0, 1, 2), rolling=True):
        rows, fs, _ = fixture(days=days)
        schedule = scheduled(offsets)
        result = execution.simulate(rows, fs, schedule, START, START+days*DAY, rolling=rolling)
        receipt = audit.audit_governance(result, rows, fs, schedule)
        return result, rows, fs, schedule, receipt

    def test_full_calendar_predictions_and_real_update_count(self):
        result, _, _, _, receipt = self.run_case()
        self.assertEqual(receipt['prediction_dates'], 4)
        self.assertEqual(receipt['actual_postinitial_updates'], 2)
        self.assertEqual([p['model_id'] for p in result['predictions']], ['m0', 'm1', 'm2', 'm2'])

    def test_static_reference_has_no_applied_postinitial_update(self):
        result, _, _, _, receipt = self.run_case(rolling=False)
        self.assertEqual(receipt['actual_postinitial_updates'], 0)
        self.assertEqual(receipt['blocked_updates'], 2)
        self.assertTrue(all(p['model_id'] == 'm0' for p in result['predictions']))

    def test_audit_rejects_missing_prediction_and_shadow_model_use(self):
        result, rows, fs, schedule, _ = self.run_case()
        for case in ('missing', 'shadow_identity', 'prediction_value', 'direction'):
            bad = copy.deepcopy(result)
            if case == 'missing':
                del bad['predictions'][0]
            elif case == 'shadow_identity':
                bad['predictions'][0]['model_id'] = 'm2'
            elif case == 'prediction_value':
                bad['predictions'][0]['predicted_net_fraction'][0] += .1
            else:
                bad['predictions'][0]['direction'] = 0
            with self.subTest(case=case), self.assertRaises(ValueError):
                audit.audit_governance(bad, rows, fs, schedule)

    def test_future_alert_cannot_block_an_earlier_update(self):
        result, _, _, _, receipt = self.run_case(days=22, offsets=(0, 2, 11, 18))
        self.assertEqual(receipt['performance_freeze_time'], START+11*DAY)
        self.assertEqual(receipt['actual_postinitial_updates'], 1)
        self.assertEqual([v['model_id'] for v in result['governance']['applied_updates']], ['m0', 'm2'])
        self.assertEqual([v['reason'] for v in result['governance']['blocked_updates']],
                         ['PERFORMANCE_FREEZE', 'PERFORMANCE_FREEZE'])

    def test_governance_mutations_are_independently_rejected(self):
        result, rows, fs, schedule, _ = self.run_case(days=22, offsets=(0, 2, 11, 18))
        for case in ('freeze_removed', 'freeze_future', 'blocked_omitted',
                     'false_reason', 'applied_shadow', 'active_history', 'mode'):
            bad = copy.deepcopy(result)
            g = bad['governance']
            if case == 'freeze_removed':
                g['performance_freeze_time'] = None
            elif case == 'freeze_future':
                g['performance_freeze_time'] += STEP
            elif case == 'blocked_omitted':
                g['blocked_updates'].pop()
            elif case == 'false_reason':
                g['blocked_updates'][0]['reason'] = 'HARD_STOP'
            elif case == 'applied_shadow':
                g['applied_updates'].append(dict(t=START+11*DAY, model_id='m11', reason='SCHEDULED'))
            elif case == 'active_history':
                g['active_model_by_date'][-1]['model_id'] = 'm18'
            else:
                bad['rolling'] = False
            with self.subTest(case=case), self.assertRaises(ValueError):
                audit.audit_governance(bad, rows, fs, schedule)

    def test_pressure_book_alone_can_freeze_the_pair(self):
        rows, fs, _ = fixture(days=22)
        for row in rows:
            price = 10000*(1+.002*(row['timestamp']-START)/DAY)
            for key in ('open', 'price', 'high', 'low', 'mark_open', 'mark_close', 'mark_high', 'mark_low'):
                row[key] = price
        by = {row['timestamp']: row for row in rows}
        for t, f in fs.items():
            f['price'] = by[t-STEP]['price']
        schedule = scheduled((0, 11, 18))
        result = execution.simulate(rows, fs, schedule, START, START+22*DAY)
        audit.audit_governance(result, rows, fs, schedule)
        alerts = [[e for e in book['events'] if e['kind'] == 'performance_alert']
                  for book in result['path']['books']]
        self.assertEqual(alerts[0], [])
        self.assertEqual(len(alerts[1]), 1)
        self.assertEqual(result['governance']['performance_freeze_time'], START+11*DAY)
        self.assertEqual(len(result['governance']['applied_updates']), 1)

    def test_later_positive_weeks_do_not_automatically_unfreeze(self):
        rows, fs, _ = fixture(days=29)
        for row in rows:
            if row['timestamp'] >= START+12*DAY:
                price = 10000*(1+.005*(row['timestamp']-START-12*DAY)/DAY)
                for key in ('open', 'price', 'high', 'low', 'mark_open', 'mark_close', 'mark_high', 'mark_low'):
                    row[key] = price
        by = {row['timestamp']: row for row in rows}
        for t, f in fs.items():
            f['price'] = by[t-STEP]['price']
        schedule = scheduled((0, 18, 25))
        result = execution.simulate(rows, fs, schedule, START, START+29*DAY)
        audit.audit_governance(result, rows, fs, schedule)
        self.assertEqual(result['governance']['performance_freeze_time'], START+11*DAY)
        self.assertEqual(len(result['governance']['applied_updates']), 1)
        self.assertTrue(all(v['reason'] == 'PERFORMANCE_FREEZE'
                            for v in result['governance']['blocked_updates']))
        self.assertTrue(all(book['days'][-1]['equity'] > book['days'][12]['equity']
                            for book in result['path']['books']))

    def test_hard_stop_blocks_new_models_without_resetting_cash(self):
        rows, fs, _ = fixture(days=4)
        rows[3]['mark_low'] = 6000.
        schedule = scheduled((0, 1, 2))
        result = execution.simulate(rows, fs, schedule, START, START+4*DAY)
        receipt = audit.audit_governance(result, rows, fs, schedule)
        self.assertEqual(receipt['actual_postinitial_updates'], 0)
        self.assertEqual([v['reason'] for v in result['governance']['blocked_updates']], ['HARD_STOP']*2)
        bad = copy.deepcopy(result)
        bad['path']['books'][0]['cash'] = 10000.
        with self.assertRaises(ValueError):
            audit.audit_governance(bad, rows, fs, schedule)

    def test_execution_failure_does_not_retroactively_block_prior_decision(self):
        rows, fs, _ = fixture(days=4)
        for row in rows:
            if row['timestamp'] >= START+DAY-3*STEP:
                row['volume'] = 0.
        schedule = scheduled((0, 1, 2))
        result = execution.simulate(rows, fs, schedule, START, START+4*DAY)
        receipt = audit.audit_governance(result, rows, fs, schedule)
        self.assertEqual(receipt['actual_postinitial_updates'], 1)
        self.assertEqual(result['governance']['applied_updates'][-1]['t'], START+DAY)
        self.assertEqual(result['governance']['blocked_updates'][0]['reason'], 'EXECUTION_INSUFFICIENT')
        self.assertLess(result['path']['bars'], 4*DAY//STEP)
        self.assertEqual(len(result['predictions']), 4)

    def test_schedule_identity_maturity_and_frozen_regularization(self):
        result, rows, fs, schedule, _ = self.run_case()
        for case in ('future_label', 'wrong_ridge', 'duplicate_identity', 'missing_initial'):
            bad = copy.deepcopy(schedule)
            if case == 'future_label':
                bad[1]['model']['label_cutoff'] = bad[1]['t']+STEP
            elif case == 'wrong_ridge':
                bad[1]['model']['ridge'] = 1.
            elif case == 'duplicate_identity':
                bad[1]['model_id'] = bad[0]['model_id']
            else:
                bad.pop(0)
            with self.subTest(case=case), self.assertRaises(ValueError):
                audit.audit_governance(result, rows, fs, bad)

    def test_verified_alert_cannot_be_removed_from_execution_events(self):
        result, rows, fs, schedule, _ = self.run_case(days=22, offsets=(0, 2, 11, 18))
        bad = copy.deepcopy(result)
        for book in bad['path']['books']:
            book['events'] = [e for e in book['events'] if e['kind'] != 'performance_alert']
        with self.assertRaises(ValueError):
            audit.audit_governance(bad, rows, fs, schedule)


EVAL_START, EVAL_END, BOUNDARY = 1752624000000, 1767225600000, 1759276800000


def decision_fixture(updates=2):
    def one(net, latency=1, rolling=False):
        book = dict(net=net, insufficient=None, quantity=0., stopped=None,
                    cycles=[dict(net=net/30, complete=True) for _ in range(30)],
                    active_weeks=list(range(12)),
                    days=[dict(t=BOUNDARY, equity=10000+net/2),
                          dict(t=EVAL_END, equity=10000+net)])
        applied = [dict(t=EVAL_START, model_id='m0', reason='INITIAL')]
        if rolling:
            applied += [dict(t=EVAL_START+i*31*DAY, model_id='m'+str(i), reason='SCHEDULED')
                        for i in range(1, updates+1)]
        return dict(rolling=rolling, path=dict(start=EVAL_START, end=EVAL_END,
                    bars=(EVAL_END-EVAL_START)//STEP, latency=latency,
                    books=[copy.deepcopy(book), copy.deepcopy(book)]),
                    governance=dict(applied_updates=applied))
    return dict(old2021=one(50.), recent2025=one(100.),
                rolling=one(200., rolling=True), delay=one(150., latency=2, rolling=True))


class RollingVerdictAuditTests(unittest.TestCase):
    def checked(self, results):
        outcome = decision.classify(results)
        audit.audit_verdict(results, outcome)
        return outcome

    def test_supported_is_development_only_and_locks_remain(self):
        outcome = self.checked(decision_fixture())
        self.assertEqual(outcome['status'], 'DEVELOPMENT_SUPPORTED')
        self.assertEqual(outcome['economic_status'], 'BASE_AND_STRESS_POSITIVE')
        self.assertEqual(outcome['adaptation_status'], 'INCREMENT_DEVELOPMENT_SUPPORTED')
        self.assertEqual(outcome['applied_updates'], 2)
        self.assertFalse(outcome['independent_confirmation'])
        self.assertFalse(outcome['g4'])
        self.assertFalse(outcome['online_learning_proven'])
        self.assertEqual(outcome['qualified_candidates'], 0)
        self.assertTrue(outcome['demo_opening_lock_unchanged'])

    def test_zero_or_one_update_is_not_identified_not_algorithm_failure(self):
        for count in (0, 1):
            with self.subTest(count=count):
                outcome = self.checked(decision_fixture(count))
                self.assertEqual(outcome['status'], 'ADAPTATION_NOT_IDENTIFIED')
                self.assertEqual(outcome['adaptation_status'], 'ADAPTATION_NOT_IDENTIFIED')
                self.assertEqual(outcome['economic_status'], 'BASE_AND_STRESS_POSITIVE')
                self.assertIn('APPLIED_UPDATE_CAPACITY', outcome['reasons'])

    def test_no_update_does_not_hide_absolute_economic_failure(self):
        results = decision_fixture(0)
        results['rolling']['path']['books'][1]['net'] = -1.
        outcome = self.checked(results)
        self.assertEqual(outcome['adaptation_status'], 'ADAPTATION_NOT_IDENTIFIED')
        self.assertEqual(outcome['economic_status'], 'NET_OR_RISK_UNSUPPORTED')
        self.assertIn('ROLLING_NET_NONPOSITIVE', outcome['reasons'])

    def test_less_loss_relative_increment_is_not_full_support(self):
        results = decision_fixture()
        for name, net in (('old2021', -200.), ('recent2025', -100.), ('rolling', -50.)):
            for book in results[name]['path']['books']:
                book['net'] = net
        outcome = self.checked(results)
        self.assertEqual(outcome['adaptation_status'], 'INCREMENT_DEVELOPMENT_SUPPORTED')
        self.assertEqual(outcome['economic_status'], 'NET_OR_RISK_UNSUPPORTED')
        self.assertEqual(outcome['status'], 'NET_OR_RISK_UNSUPPORTED')

    def test_winning_old_model_only_does_not_identify_recurring_benefit(self):
        results = decision_fixture()
        results['recent2025']['path']['books'][1]['net'] = 200.
        outcome = self.checked(results)
        self.assertEqual(outcome['adaptation_status'], 'INCREMENT_UNSUPPORTED')
        self.assertIn('MINIMUM_INCREMENT_recent2025', outcome['reasons'])
        self.assertNotEqual(outcome['status'], 'DEVELOPMENT_SUPPORTED')

    def test_exact_absolute_and_increment_boundaries(self):
        results = decision_fixture()
        for name, net in (('old2021', 75.), ('recent2025', 75.), ('rolling', 100.)):
            for book in results[name]['path']['books']:
                book['net'] = net
                book['cycles'] = [dict(net=net/30, complete=True) for _ in range(30)]
                book['days'] = [dict(t=BOUNDARY, equity=10000+net/2),
                                dict(t=EVAL_END, equity=10000+net)]
        self.assertEqual(self.checked(results)['status'], 'DEVELOPMENT_SUPPORTED')
        results['rolling']['path']['books'][1]['net'] = 99.999
        outcome = self.checked(results)
        self.assertIn('MINIMUM_ABSOLUTE_EFFECT', outcome['reasons'])
        self.assertIn('MINIMUM_INCREMENT_recent2025', outcome['reasons'])

    def test_complete_stopped_control_is_allowed_incomplete_is_not(self):
        results = decision_fixture()
        results['old2021']['path']['books'][1]['stopped'] = dict(t=BOUNDARY, reason='RISK')
        self.assertEqual(self.checked(results)['status'], 'DEVELOPMENT_SUPPORTED')
        results['old2021']['path']['books'][1]['insufficient'] = 'UNKNOWN'
        self.assertEqual(self.checked(results)['status'], 'COMPARISON_EVIDENCE_INCOMPLETE')

    def test_missing_calendar_or_mismatched_period_is_incomplete(self):
        for case in ('bars', 'period'):
            results = decision_fixture()
            path = results['delay']['path']
            if case == 'bars':
                path['bars'] -= 1
            elif case == 'period':
                path['start'] += DAY
                path['bars'] -= DAY//STEP
            with self.subTest(case=case):
                self.assertEqual(self.checked(results)['status'], 'COMPARISON_EVIDENCE_INCOMPLETE')
        results = decision_fixture()
        outcome = decision.classify(results)
        results['delay']['path']['latency'] = 1
        with self.assertRaisesRegex(ValueError, 'LATENCY_IDENTITY'):
            decision.classify(results)
        with self.assertRaisesRegex(ValueError, 'LATENCY_IDENTITY'):
            audit.audit_verdict(results, outcome)

    def test_partial_winning_cycles_count_for_concentration_not_capacity(self):
        results = decision_fixture()
        book = results['rolling']['path']['books'][1]
        book['cycles'] = [dict(net=-1., complete=True) for _ in range(29)]
        book['cycles'].append(dict(net=229., complete=False))
        outcome = self.checked(results)
        self.assertIn('BEST_CYCLE_CONCENTRATION', outcome['reasons'])
        self.assertIn('EVENT_CAPACITY', outcome['reasons'])
        self.assertNotEqual(outcome['status'], 'DEVELOPMENT_SUPPORTED')

    def test_risk_delay_capacity_and_subperiod_negatives(self):
        for case in ('risk', 'delay', 'cycles', 'weeks', 'first_part', 'second_part'):
            results = decision_fixture()
            book = results['rolling']['path']['books'][1]
            if case == 'risk':
                book['stopped'] = dict(t=BOUNDARY, reason='RISK')
            elif case == 'delay':
                results['delay']['path']['books'][1]['net'] = 0.
            elif case == 'cycles':
                book['cycles'].pop()
            elif case == 'weeks':
                book['active_weeks'].pop()
            elif case == 'first_part':
                book['days'][0]['equity'] = 9999.
            else:
                book['days'][0]['equity'] = 10201.
            with self.subTest(case=case):
                self.assertNotEqual(self.checked(results)['status'], 'DEVELOPMENT_SUPPORTED')

    def test_audit_rejects_status_locks_increments_and_update_count_mutations(self):
        results = decision_fixture()
        outcome = decision.classify(results)
        for case in ('g4', 'status', 'economic_status', 'increment', 'updates', 'parts'):
            bad = copy.deepcopy(outcome)
            if case == 'g4':
                bad['g4'] = True
            elif case == 'status':
                bad['status'] = 'PASS'
            elif case == 'economic_status':
                bad['economic_status'] = 'QUALIFIED'
            elif case == 'increment':
                bad['pressure_increments']['recent2025'] += 1
            elif case == 'updates':
                bad['applied_updates'] += 1
            else:
                bad['half_period_pressure_net'][0] += 1
            with self.subTest(case=case), self.assertRaises(ValueError):
                audit.audit_verdict(results, bad)

    def test_complete_comparison_set_required(self):
        results = decision_fixture()
        outcome = decision.classify(results)
        del results['recent2025']
        with self.assertRaises(ValueError):
            decision.classify(results)
        with self.assertRaises(ValueError):
            audit.audit_verdict(results, outcome)


if __name__ == '__main__':
    unittest.main()
