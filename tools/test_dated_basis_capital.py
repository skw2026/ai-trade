from decimal import Decimal as D
import unittest
import diagnose_dated_basis_capital as diagnosis


class CapitalSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result=diagnosis.run_controls()

    def test_original_constants_restored(self):
        self.assertEqual(diagnosis.engine.PROFILES['stress']['annual'],.12)
        self.assertEqual(diagnosis.reference.PARAMS['stress'][-1],'.12')
        self.assertTrue(self.result['original_constants_restored'])

    def test_opportunity_debit_triggers_model_stop(self):
        x=self.result['controls']['opportunity_debited']
        self.assertEqual(x['risk_reason'],'DRAWDOWN_8_PERCENT')
        self.assertGreaterEqual(x['observed_drawdown'],.08)
        self.assertLess(x['exit_ms'],diagnosis.EXPIRY)

    def test_cash_equity_did_not_hit_eight_percent(self):
        analytic=self.result['analytic']
        self.assertGreater(D(analytic['cash_equity']),D(10800)*D('.92'))
        self.assertLess(D(analytic['cash_drawdown']),D('.08'))

    def test_cash_control_keeps_fees_and_reaches_expiry(self):
        x=self.result['controls']['cash_control']
        self.assertIsNone(x['risk_reason'])
        self.assertEqual(x['exit_ms'],diagnosis.EXPIRY)
        self.assertGreater(x['fees'],0)
        self.assertGreater(x['slippage_drag'],0)
        self.assertEqual(x['opportunity_cost'],0)

    def test_actual_cash_loss_still_stops(self):
        x=self.result['controls']['cash_loss_control']
        self.assertEqual(x['risk_reason'],'DRAWDOWN_8_PERCENT')
        self.assertGreaterEqual(x['observed_drawdown'],.08)
        self.assertEqual(x['exit_ms'],diagnosis.ENTRY+2*diagnosis.BAR)

    def test_no_edge_remains_negative(self):
        x=self.result['controls']['no_edge_control']
        self.assertLess(x['same_path_cash_net'],0)
        self.assertEqual(x['opportunity_cost'],0)

    def test_independent_timing_and_cash_reference(self):
        x=self.result['controls']['opportunity_debited'];a=self.result['analytic']
        self.assertEqual(x['exit_ms'],a['first_trigger_ms']+diagnosis.BAR)
        self.assertEqual(x['exit_ms'],a['expected_exit_ms'])
        self.assertAlmostEqual(x['same_path_cash_end'],float(a['cash_equity']),places=7)

    def test_same_path_cash_is_not_utility(self):
        x=self.result['controls']['opportunity_debited']
        self.assertGreater(x['same_path_cash_end'],x['end_capital'])
        self.assertAlmostEqual(x['same_path_cash_net'],x['gross']-x['fees']-x['slippage_drag'],places=7)

    def test_synthetic_evidence_has_no_market_qualification(self):
        self.assertEqual(self.result['market_payoff_attempts'],0)
        self.assertEqual(self.result['training_vectors'],0)
        self.assertEqual(self.result['decimal_checked_ledgers'],4)
        self.assertFalse(self.result['candidate_qualified'])
        self.assertFalse(self.result['g4'])


if __name__=='__main__':unittest.main()
