import copy
import unittest
import dated_basis_assumption_screen as model


def quarter(expiry=1695974400000, future='102'):
    row = dict(o='100', h='100', l='100', c='100', v='1000', n=20, complete=True)
    times = range(expiry-7*model.DAY-model.BAR, expiry+3900000, model.BAR)
    spot = {t: row.copy() for t in times}
    futures = {t: dict(row, o=future, h=future, l=future, c=future) for t in times}
    return dict(expiry=expiry, settlement='100', series=dict(spot=spot, future=futures, mark=copy.deepcopy(futures)))


def scenario(cost='base', hour=8, bias=0, delay=0):
    return next(s for s in model.scenarios() if (s['cost'],s['hour'],s['bias_bps'],s['delay_minutes']) ==
                (cost,hour,bias,delay))


class ScreenTests(unittest.TestCase):
    def test_fixed_scenario_count(self):
        self.assertEqual(len(model.scenarios()),54)
        self.assertEqual(len({s['id'] for s in model.scenarios()}),54)

    def test_inverse_cash_identity(self):
        result=model.quarter_path(model.normalized(quarter()),scenario('zero_friction'))
        self.assertAlmostEqual(result['net'],2500-2500*100/102,places=9)
        self.assertAlmostEqual(result['btc_after_exit'],25,places=10)
        self.assertEqual(result['fees'],0)
        self.assertEqual(result['risk_status'],'MODEL_OBSERVED_ONLY')

    def test_costs_lower_net_and_reconcile(self):
        q=model.normalized(quarter())
        results=[model.quarter_path(q,scenario(cost)) for cost in model.PROFILES]
        self.assertGreater(results[0]['net'],results[1]['net'])
        self.assertGreater(results[1]['net'],results[2]['net'])
        for r in results:
            self.assertAlmostEqual(r['net'],r['gross']-r['fees']-r['slippage_drag']-r['opportunity_cost'],places=8)

    def test_settlement_and_spot_not_equal_by_assumption(self):
        q=quarter();q['settlement']='110'
        r=model.quarter_path(model.normalized(q),scenario('zero_friction'))
        self.assertAlmostEqual(r['net'],2500*100/110-2500*100/102,places=8)
        self.assertLess(r['net'],0)

    def test_decision_does_not_look_at_outcome(self):
        q=quarter();first=model.decision(model.normalized(q))
        q['settlement']='1000'
        q['series']['spot'][q['expiry']]['o']='50'
        self.assertEqual(first,model.decision(model.normalized(q)))
        self.assertTrue(first['enter'])

    def test_flat_basis_does_not_enter(self):
        self.assertFalse(model.decision(model.normalized(quarter(future='100')))['enter'])

    def test_delay_changes_spot_cashflow(self):
        q=quarter();q['series']['spot'][q['expiry']+300000]['o']='99'
        a=model.quarter_path(model.normalized(q),scenario('zero_friction'))
        b=model.quarter_path(model.normalized(q),scenario('zero_friction',delay=5))
        self.assertAlmostEqual(a['net']-b['net'],25,places=9)

    def test_price_bias_sensitivity(self):
        q=model.normalized(quarter())
        lo=model.quarter_path(q,scenario(bias=-25));hi=model.quarter_path(q,scenario(bias=25))
        self.assertGreater(lo['net'],hi['net'])

    def test_unknown_path_not_forward_filled(self):
        q=quarter();t=q['expiry']-3*model.DAY
        del q['series']['spot'][t]
        q['series']['future'][t]['n']=0
        r=model.quarter_path(model.normalized(q),scenario())
        self.assertEqual(r['risk_status'],'UNKNOWN')
        self.assertGreater(r['unknown_bars'],0)
        self.assertFalse(r['actual_execution_qualified'])

    def test_drawdown_exits_next_bar(self):
        q=quarter();t=q['expiry']-7*model.DAY+300000
        q['series']['mark'][t]=dict(q['series']['mark'][t],o='200',h='200',l='200',c='200')
        r=model.quarter_path(model.normalized(q),scenario())
        self.assertEqual(r['risk_reason'],'DRAWDOWN_8_PERCENT')
        self.assertEqual(r['exit_ms'],t+300000)

    def test_margin_buffer_not_ignored(self):
        q=quarter();t=q['expiry']-7*model.DAY+300000
        q['series']['mark'][t]=dict(q['series']['mark'][t],h='1000000')
        r=model.quarter_path(model.normalized(q),scenario())
        self.assertLess(r['minimum_margin_ratio'],2)
        self.assertIsNotNone(r['risk_reason'])

    def test_zero_trade_exit_is_unknown(self):
        q=quarter();q['series']['spot'][q['expiry']]['n']=0
        r=model.quarter_path(model.normalized(q),scenario())
        self.assertEqual(r['risk_status'],'UNKNOWN')

    def test_no_trade_cannot_pass(self):
        self.assertEqual(model.classify([],{'q':dict(enter=False)}),'NO_GO_NO_TRADES')

    def test_halted_portfolio_never_restarts(self):
        first=quarter();t=first['expiry']-7*model.DAY+300000
        first['series']['mark'][t]=dict(first['series']['mark'][t],h='200',l='200',o='200',c='200')
        result=model.sweep({'first':first,'second':quarter(first['expiry']+90*model.DAY)})
        for p in result['portfolios']:
            self.assertTrue(p['halted'])
            self.assertEqual(p['paths'][1]['status'],'HALTED')


if __name__=='__main__':unittest.main()
