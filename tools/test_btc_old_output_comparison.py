import copy
import unittest

import btc_old_output_model as model
import run_btc_old_output_comparison as comparison
import test_btc_old_output_decision as decision_fixtures
from test_btc_old_output_decision import path


class ComparisonTests(unittest.TestCase):
    def training(self):
        mechanisms, prices = model.grids()
        results = [path(10+i, p) for i, p in enumerate(mechanisms+prices)]
        for i, result in enumerate(results):
            if i not in (2, 9):
                for b in result['books']:
                    b['stopped'] = {'reason': 'RESEARCH_DRAWDOWN_8_PERCENT', 't': 1}
        for b in results[12]['books']:
            b['net'] = 100
        return results

    def test_stopped_control_can_be_benchmark_not_candidate(self):
        controls = self.training()[12:]
        self.assertEqual(comparison.select_benchmark(controls), 0)
        self.assertIsNone(model.select(controls))
        self.assertFalse(model.eligible(controls[0]))

    def test_candidate_still_must_survive(self):
        training = self.training()
        training[9]['books'][1]['stopped'] = {'t': 1}
        with self.assertRaisesRegex(ValueError, 'CANDIDATE_NOT_ELIGIBLE'):
            comparison.specifications(training)

    def test_fixed_and_six_sensitivities_unchanged(self):
        specs = comparison.specifications(self.training())
        self.assertEqual(tuple(s[0] for s in specs), comparison.NAMES)
        self.assertEqual([s[1] for s in specs], [[1,0,2],[-1,0,1],[0,-1,1],[0,1,1],[1,0,2],[1,0,2]])
        self.assertEqual([s[2] for s in specs], [3,3,3,3,4,3])
        self.assertEqual(specs[-1][3], {'latency': 2})
        self.assertNotIn('initially_stopped', specs[2][3])

    def test_incomplete_control_is_not_rankable(self):
        controls = self.training()[12:]
        for field, value in [('quantity', .1), ('insufficient', 'EXIT_RESIDUAL')]:
            changed = copy.deepcopy(controls)
            changed[0]['books'][0][field] = value
            with self.assertRaisesRegex(ValueError, 'BENCHMARK_INCOMPLETE'):
                comparison.select_benchmark(changed)

    def test_only_price_controls_allowed(self):
        with self.assertRaisesRegex(ValueError, 'BENCHMARK_ONLY'):
            comparison.select_benchmark([path(100, (1,0,2))])

    def test_turnover_then_parameter_ties(self):
        controls = [path(-10, (0,1,1)), path(-10, (0,-1,1))]
        self.assertEqual(comparison.select_benchmark(controls), 1)
        controls[0]['books'][0]['turnover'] = 0
        self.assertEqual(comparison.select_benchmark(controls), 0)

    def test_original_market_result_not_a_retry(self):
        self.assertNotEqual(comparison.ROOT, comparison.original.ROOT)
        self.assertEqual(comparison.ROOT.parent, comparison.original.ROOT)

    def test_independent_decision_all_outcomes(self):
        cases = []
        p = decision_fixtures.DecisionTests().paths();cases.append(p)
        p = copy.deepcopy(p);p[0]['books'][1]['net'] = 199;cases.append(p)
        p = decision_fixtures.DecisionTests().paths();p[4]['books'][0]['stopped'] = {'t': 1};cases.append(p)
        p = decision_fixtures.DecisionTests().paths();p[0]['books'][1]['cycles'] = [];cases.append(p)
        p = decision_fixtures.DecisionTests().paths();p[0]['books'][0]['quantity'] = .1;cases.append(p)
        for paths in cases:
            with self.subTest(status=model.verdict(paths)['status']):
                self.assertEqual(comparison.audit_decision(paths, model.verdict(paths))['status'], 'INDEPENDENT_DECISION_VERIFIED')

    def test_stopped_benchmark_does_not_clear_candidate_risk(self):
        p = decision_fixtures.DecisionTests().paths()
        p[2]['books'][0]['stopped'] = {'t': 1}
        self.assertEqual(model.verdict(p)['status'], 'DEVELOPMENT_SUPPORTED')
        p[0]['books'][0]['stopped'] = {'t': 1}
        self.assertEqual(comparison.audit_decision(p, model.verdict(p))['decision'], 'NO_GO_RISK')

    def test_wrong_decision_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'INDEPENDENT_DECISION_MISMATCH'):
            comparison.audit_decision(decision_fixtures.DecisionTests().paths(), {'status': 'NO_GO_RISK'})


if __name__ == '__main__':
    unittest.main()
