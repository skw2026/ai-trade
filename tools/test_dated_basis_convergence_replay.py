import copy
from dataclasses import asdict
from decimal import Decimal as D, localcontext
import unittest

import dated_basis_convergence_replay as engine
import audit_dated_basis_convergence_replay as audit
from dated_basis_dual_ledger import BAR, DAY, CashState
from test_dated_basis_opportunity_replay import bar, fixture, scenario


class ConvergenceTests(unittest.TestCase):
    def checked(self,q,i,s=None,state=None):
        s=scenario() if s is None else s
        r=engine.quarter_path(q,s,i,state)
        original=None if state is None else {k:D(str(v)) if type(v) is float else v for k,v in asdict(state).items()}
        audit.equal(audit.reference_path(q,s,i,original),r)
        return r

    def signal(self,q,t):
        q['series']['future'][str(t)]=bar(t,100.1)

    def test_exact_boundary_and_sides(self):
        self.assertTrue(engine.convergence_exit('2002','2005'))
        self.assertTrue(engine.convergence_exit('2002','2004.999999'))
        self.assertFalse(engine.convergence_exit('2002','2005.000001'))

    def test_cash_order_positive_negative_and_cross_zero(self):
        with localcontext() as c:
            c.prec=50
            for index in (D(100),D(99900),D(200000)):
                for factor in (D('.999999'),D(1),D('1.000001')):
                    p=index*factor; s=index/D('1.0025')
                    def cash(x):
                        w=D(2500)*D('.999')/x-D(25)*D('.001')
                        return w*s*(D('.999')*D('.998') if w>=0 else D('1.001')*D('1.002'))
                    self.assertEqual(cash(p)>=cash(index),p<=index)

    def test_different_close_fee_invalidates_simplified_order(self):
        # General condition P <= I*(1-f_close)/(1-f_settle).
        p=D(99); index=D(100)
        self.assertLess(p,index)
        self.assertLess((1-D('.02'))/p,(1-D('.001'))/index)
        self.assertGreater(p,index*(1-D('.02'))/(1-D('.001')))

    def test_no_signal_preserves_delivery(self):
        q,i=fixture(); r=self.checked(q,i)
        self.assertIsNone(r['convergence_decision_bar'])
        self.assertEqual(r['exit_ms'],q['expiry'])

    def test_complete_signal_has_full_bar_latency(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR; self.signal(q,t)
        r=self.checked(q,i)
        self.assertEqual(r['convergence_decision_bar'],t)
        self.assertEqual(r['convergence_earliest_exit_ms'],t+2*BAR)
        self.assertEqual(r['exit_ms'],t+2*BAR)
        self.assertFalse(r['settled'])

    def test_first_signal_not_cancelled_for_later_better_price(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR; self.signal(q,t)
        self.signal(q,t+10*BAR)
        r=self.checked(q,i)
        self.assertEqual(r['exit_ms'],t+2*BAR)

    def test_no_profitable_only_exit_filter(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR; self.signal(q,t)
        q['series']['future'][str(t+2*BAR)]=bar(t+2*BAR,250)
        r=self.checked(q,i)
        self.assertEqual(r['exit_ms'],t+2*BAR)
        self.assertLess(r['cash_net'],0)
        self.assertTrue(r['cash_state']['halted'])

    def test_quiet_bar_cannot_emit_trading_signal(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR; self.signal(q,t)
        q['series']['future'][str(t)]['n']=0
        r=self.checked(q,i)
        self.assertIsNone(r['convergence_decision_bar'])
        self.assertEqual(r['exit_ms'],q['expiry'])

    def test_incomplete_bar_cannot_emit_signal(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR; self.signal(q,t)
        q['series']['future'][str(t)]['complete']=False
        self.assertIsNone(self.checked(q,i)['convergence_decision_bar'])

    def test_missing_valuation_precedes_discretionary_exit(self):
        q,i=fixture(); t=i['earliest_entry_ms']; self.signal(q,t)
        del q['series']['mark'][str(t)]
        r=self.checked(q,i)
        self.assertEqual(r['exit_ms'],t+BAR)
        self.assertTrue(r['cash_state']['uncertain'])

    def test_hard_risk_precedes_discretionary_exit(self):
        q,i=fixture(); t=i['earliest_entry_ms']; self.signal(q,t)
        q['series']['mark'][str(t)]=bar(t,160,n=0,volume=0)
        r=self.checked(q,i)
        self.assertEqual(r['exit_ms'],t+BAR)
        self.assertEqual(r['risk_reason'],'DRAWDOWN_8_PERCENT')

    def test_delivery_before_pending_signal_execution(self):
        q,i=fixture(); t=q['expiry']-BAR; self.signal(q,t)
        r=self.checked(q,i)
        self.assertTrue(r['settled'])
        self.assertEqual(r['exit_ms'],q['expiry'])
        self.assertEqual(r['convergence_earliest_exit_ms'],q['expiry']+BAR)

    def test_delivery_same_time_policy_exit_not_future_trade(self):
        q,i=fixture(); self.signal(q,q['expiry']-2*BAR)
        del q['series']['future'][str(q['expiry'])]
        r=self.checked(q,i,scenario(delay=60))
        self.assertTrue(r['settled'])
        self.assertEqual(r['exit_ms'],q['expiry'])

    def test_unavailable_policy_fill_waits_and_latches(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR; self.signal(q,t)
        q['series']['future'][str(t+2*BAR)]['n']=0
        r=self.checked(q,i)
        self.assertEqual(r['exit_ms'],t+3*BAR)
        self.assertEqual(r['unavailable_exit_count'],1)
        self.assertTrue(r['cash_state']['uncertain'])

    def test_never_fabricates_final_fill(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR; self.signal(q,t)
        for stamp in range(t+2*BAR,q['expiry']+3600000+BAR,BAR):
            del q['series']['spot'][str(stamp)]
        r=self.checked(q,i)
        self.assertEqual(r['status'],'UNRESOLVED')
        self.assertIsNone(r['cash_net'])

    def test_cash_and_peak_carry(self):
        q,i=fixture(); self.signal(q,i['earliest_entry_ms']+BAR)
        r=self.checked(q,i,state=CashState(9500.,10000.))
        self.assertEqual(r['initial_state']['equity'],9500.)
        self.assertGreaterEqual(r['cash_state']['peak'],10000.)

    def test_full_portfolio_decimal_audit_and_unqualified(self):
        q,i=fixture(); other,j=fixture(100*DAY)
        self.signal(q,i['earliest_entry_ms']+BAR); self.signal(other,j['earliest_entry_ms']+BAR)
        self.signal(q,i['earliest_entry_ms']+3*BAR); self.signal(other,j['earliest_entry_ms']+3*BAR)
        bundle={'Q1':q,'Q2':other}; r=engine.replay(bundle)
        audit.verify(bundle,r)
        self.assertEqual(r['summary']['status'],'CONDITIONAL_DEVELOPMENT_SUPPORTED')
        self.assertFalse(r['summary']['actual_execution_qualified'])
        self.assertFalse(r['summary']['g4'])

    def test_signal_audit_rejects_clock_tampering(self):
        q,i=fixture(); self.signal(q,i['earliest_entry_ms']+BAR)
        r=self.checked(q,i); r['convergence_decision_bar']-=BAR
        with self.assertRaises(AssertionError):audit.equal(audit.reference_path(q,scenario(),i),r)

    def test_invalid_signal_price_rejected(self):
        for value in ('0','-1','NaN','Infinity'):
            with self.assertRaises(ValueError):engine.convergence_exit(value,'100')

    def test_signal_decision_ignores_future_terminal(self):
        q,i=fixture(); self.signal(q,i['earliest_entry_ms']+BAR)
        a=self.checked(q,i); changed=copy.deepcopy(q); changed['settlement']='200000'
        b=self.checked(changed,i)
        self.assertEqual(a,b)


if __name__=='__main__':unittest.main()
