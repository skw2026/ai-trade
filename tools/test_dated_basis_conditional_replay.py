import copy
from dataclasses import asdict
import unittest

import audit_dated_basis_conditional_replay as audit
import dated_basis_conditional_replay as replay
from dated_basis_dual_ledger import BAR, DAY, CashState


def bar(t,value=100,*,n=1,volume=10,low=None,high=None):
    return dict(o=str(value),c=str(value),l=str(value if low is None else low),
                h=str(value if high is None else high),v=str(volume),n=n,
                close_ms=t+BAR-1,complete=True)


def fixture(shift=0):
    expiry=100*DAY+8*3600000+shift
    entry=expiry-2*DAY
    series={kind:{} for kind in ('spot','future','mark')}
    for t in range(entry-13*BAR,expiry+3600000+BAR,BAR):
        series['spot'][str(t)]=bar(t)
        series['future'][str(t)]=bar(t,104)
        series['mark'][str(t)]=bar(t,100,n=0,volume=0)
    q=dict(expiry=expiry,settlement='100',series=series)
    intention=dict(decision_bar=entry-2*BAR,available_ms=entry-BAR,earliest_entry_ms=entry,
                   forecast_cash=1.,policy='FIRST_12_STRESS_CASH_POSITIVE',actual_execution_qualified=False)
    return q,intention


def scenario(cost='stress',hour=8,bias=0,delay=0):
    return next(s for s in replay.scenarios() if s['cost']==cost and s['hour']==hour
                and s['bias_bps']==bias and s['delay_minutes']==delay)


class OpportunityTests(unittest.TestCase):
    def check_path(self,q,i,s=None,state=None):
        s=scenario() if s is None else s
        result=replay.quarter_path(q,s,i,state)
        original=None if state is None else {k:audit.D(str(v)) if type(v) is float else v for k,v in asdict(state).items()}
        expected=audit.reference_path(q,s,i,original)
        audit.equal(expected,result)
        return result

    def test_settlement_cash_identity(self):
        q,i=fixture(); p=self.check_path(q,i)
        self.assertGreater(p['cash_net'],0)
        self.assertEqual(p['exit_ms'],q['expiry'])
        self.assertAlmostEqual(p['cash_net'],p['gross']-p['slippage_drag']-p['execution_fees'])

    def test_five_minute_tail(self):
        q,i=fixture(); p=self.check_path(q,i,scenario(delay=5))
        self.assertEqual(p['tail_bar_count'],1)
        self.assertEqual(p['settlement_event']['time_ms'],q['expiry'])

    def test_hour_tail(self):
        q,i=fixture(); p=self.check_path(q,i,scenario(delay=60))
        self.assertEqual(p['tail_bar_count'],12)
        self.assertEqual(p['exit_ms'],q['expiry']+3600000)

    def test_midnight_settlement(self):
        q,i=fixture(); p=self.check_path(q,i,scenario(hour=0,delay=60))
        self.assertEqual(p['exit_ms'],q['expiry']-7*3600000)

    def test_negative_coin_tail_buys_back(self):
        q,i=fixture(); q['settlement']='200000'
        p=self.check_path(q,i,scenario(delay=60))
        self.assertLess(p['settlement_event']['coins_before_later_liabilities'],0)
        self.assertTrue(p['cash_state']['halted'])
        self.assertGreater(p['execution_fees'],0)

    def test_tail_risk_exits_before_planned(self):
        q,i=fixture(); t=q['expiry']; q['series']['spot'][str(t)]=bar(t,5)
        q['series']['spot'][str(t+BAR)]=bar(t+BAR,4)
        p=self.check_path(q,i,scenario(delay=60))
        self.assertEqual(p['exit_ms'],t+BAR)
        self.assertEqual(p['first_risk_trigger_ms'],t+BAR)
        self.assertTrue(p['settled'])

    def test_risk_stop_not_loss_cap(self):
        q,i=fixture(); t=i['earliest_entry_ms']
        q['series']['mark'][str(t)]=bar(t,160,n=0,volume=0)
        q['series']['future'][str(t+BAR)]=bar(t+BAR,250)
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],t+BAR)
        self.assertLess(p['cash_net'],-800)
        self.assertFalse(p['settled'])

    def test_missing_risk_requests_next_bar_exit(self):
        q,i=fixture(); t=i['earliest_entry_ms']; del q['series']['mark'][str(t)]
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],t+BAR)
        self.assertEqual(p['missing_bar_count'],1)
        self.assertTrue(p['cash_state']['uncertain'])
        self.assertIsNone(p['risk_reason'])

    def test_no_future_trades_does_not_invalidate_mark(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR
        q['series']['future'][str(t)]['n']=0
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],q['expiry'])
        self.assertEqual(p['unknown_reasons'],{})
        self.assertFalse(p['actual_execution_qualified'])

    def test_unavailable_exit_keeps_risk(self):
        q,i=fixture(); t=i['earliest_entry_ms']; del q['series']['mark'][str(t)]
        del q['series']['future'][str(t+BAR)]
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],t+2*BAR)
        self.assertEqual(p['unavailable_exit_count'],1)
        self.assertEqual(p['missing_bar_count'],1)

    def test_missing_tail_spot_not_fake_fill(self):
        q,i=fixture(); t=q['expiry']; del q['series']['spot'][str(t)]
        p=self.check_path(q,i,scenario(delay=60))
        self.assertEqual(p['exit_ms'],t+BAR)
        self.assertTrue(p['cash_state']['uncertain'])

    def test_unresolved_null_return(self):
        q,i=fixture()
        for t in range(q['expiry'],q['expiry']+3600000+BAR,BAR):
            del q['series']['spot'][str(t)]
        p=self.check_path(q,i)
        self.assertEqual(p['status'],'UNRESOLVED')
        self.assertIsNone(p['cash_net'])
        self.assertIsNone(p['cash_state'])

    def test_first_entry_missing_is_not_reselected(self):
        q,i=fixture(); del q['series']['future'][str(i['earliest_entry_ms'])]
        p=self.check_path(q,i)
        self.assertEqual(p['status'],'ENTRY_UNVERIFIABLE')
        self.assertTrue(p['cash_state']['uncertain'])
        self.assertEqual(p['cash_net'],0)

    def test_first_entry_zero_volume(self):
        q,i=fixture(); q['series']['spot'][str(i['earliest_entry_ms'])]['v']='0'
        self.assertEqual(self.check_path(q,i)['status'],'ENTRY_UNVERIFIABLE')

    def test_incomplete_risk_bar(self):
        q,i=fixture(); q['series']['mark'][str(i['earliest_entry_ms'])]['complete']=False
        self.assertEqual(self.check_path(q,i)['risk_status'],'UNKNOWN')

    def test_zero_volume_mark_usable(self):
        q,i=fixture(); p=self.check_path(q,i)
        self.assertEqual(p['risk_status'],'MODEL_OBSERVED_ONLY')
        self.assertGreater(p['minimum_margin_ratio'],2)

    def test_delivery_precedes_same_time_risk_exit(self):
        q,i=fixture(); t=q['expiry']-BAR
        q['series']['mark'][str(t)]=bar(t,160,n=0,volume=0)
        del q['series']['future'][str(q['expiry'])]
        p=self.check_path(q,i,scenario(delay=60))
        self.assertTrue(p['settled'])
        self.assertEqual(p['exit_ms'],q['expiry'])
        self.assertEqual(p['exit_future'],100)

    def test_no_future_required_after_delivery(self):
        q,i=fixture()
        for t in range(q['expiry'],q['expiry']+3600000+BAR,BAR):
            del q['series']['future'][str(t)]; del q['series']['mark'][str(t)]
        p=self.check_path(q,i,scenario(delay=60))
        self.assertEqual(p['risk_status'],'MODEL_OBSERVED_ONLY')
        self.assertEqual(p['tail_bar_count'],12)

    def test_cash_peak_inherited(self):
        q,i=fixture(); p=self.check_path(q,i,state=CashState(9500.,10000.))
        self.assertGreater(p['cash_drawdown'],0)
        self.assertGreater(p['cash_state']['equity'],9500)
        self.assertEqual(p['initial_state']['peak'],10000)

    def test_halted_cannot_reenter(self):
        q,i=fixture(); p=self.check_path(q,i,state=CashState(halted=True))
        self.assertEqual(p['status'],'BLOCKED_PRIOR_STATE')

    def test_unknown_cannot_reenter(self):
        q,i=fixture(); p=self.check_path(q,i,state=CashState(uncertain=True))
        self.assertEqual(p['status'],'BLOCKED_PRIOR_STATE')

    def test_no_intention(self):
        q,_=fixture(); p=self.check_path(q,None)
        self.assertEqual(p['status'],'NO_INTENTION')

    def test_benchmark_does_not_change_cash(self):
        q,i=fixture(); p=self.check_path(q,i)
        self.assertEqual(set(p['benchmark_charges']),{'0.06','0.12'})
        for r in p['benchmark_charges']:
            self.assertAlmostEqual(p['benchmark_excess'][r]+p['benchmark_charges'][r],p['cash_net'])
        self.assertEqual(p['financing_cost'],0)

    def test_first_twelve_not_best(self):
        q,i=fixture(); mask=replay.intentions({'Q':q})
        self.assertEqual(mask['Q']['earliest_entry_ms'],i['earliest_entry_ms'])
        audit.equal(audit.select(q),mask['Q'])

    def test_decision_ignores_terminal_and_marks(self):
        q,_=fixture(); before=replay.intentions({'Q':q})
        q['settlement']='100000'; q['series']['mark']={}
        self.assertEqual(before,replay.intentions({'Q':q}))

    def test_gap_breaks_streak(self):
        q,i=fixture(); t=i['decision_bar']-5*BAR
        del q['series']['future'][str(t)]
        mask=replay.intentions({'Q':q})
        self.assertEqual(mask['Q']['earliest_entry_ms'],t+14*BAR)
        audit.equal(audit.select(q),mask['Q'])

    def test_no_trade_signals_not_success(self):
        q,_=fixture()
        for t in q['series']['future']: q['series']['future'][t]=bar(int(t),100)
        result=replay.replay({'Q':q})
        self.assertEqual(result['summary']['status'],'NO_TRADES')
        audit.verify({'Q':q},result)

    def test_full_portfolio_and_independent_audit(self):
        q,_=fixture(); other,_=fixture(100*DAY)
        result=replay.replay({'Q1':q,'Q2':other})
        receipt=audit.verify({'Q1':q,'Q2':other},result)
        self.assertEqual(receipt['quarter_paths'],72)
        self.assertEqual(result['summary']['status'],'CONDITIONAL_DEVELOPMENT_SUPPORTED')
        self.assertFalse(result['summary']['g4'])

    def test_unknown_portfolio_stops_next_quarter(self):
        q,i=fixture(); del q['series']['mark'][str(i['earliest_entry_ms'])]
        other,_=fixture(100*DAY); result=replay.replay({'Q1':q,'Q2':other})
        audit.verify({'Q1':q,'Q2':other},result)
        self.assertEqual(result['summary']['status'],'DATA_INSUFFICIENT')
        self.assertTrue(all(p['paths']['Q2']['status']=='BLOCKED_PRIOR_STATE' for p in result['portfolios']))

    def test_absent_future_trade_bar_not_absent_mark(self):
        q,i=fixture(); del q['series']['future'][str(i['earliest_entry_ms']+BAR)]
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],q['expiry'])
        self.assertFalse(p['cash_state']['uncertain'])

    def test_quiet_spot_holding_is_conditional_valuation(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR
        q['series']['spot'][str(t)]=bar(t,n=0,volume=0)
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],q['expiry'])
        self.assertFalse(p['actual_execution_qualified'])

    def test_quiet_spot_exit_is_not_fabricated_fill(self):
        q,i=fixture(); t=q['expiry']; q['series']['spot'][str(t)]=bar(t,n=0,volume=0)
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],t+BAR)
        self.assertEqual(p['missing_bar_count'],0)
        self.assertEqual(p['unavailable_exit_count'],1)
        self.assertTrue(p['cash_state']['uncertain'])

    def test_quiet_future_required_exit_waits(self):
        q,i=fixture(); t=i['earliest_entry_ms']; del q['series']['mark'][str(t)]
        q['series']['future'][str(t+BAR)]=bar(t+BAR,104,n=0,volume=0)
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],t+2*BAR)
        self.assertEqual(p['unavailable_exit_count'],1)
        self.assertEqual(p['missing_bar_count'],1)

    def test_quiet_spot_tail_has_no_premature_exit(self):
        q,i=fixture(); t=q['expiry']; q['series']['spot'][str(t)]=bar(t,n=0,volume=0)
        p=self.check_path(q,i,scenario(delay=60))
        self.assertEqual(p['tail_bar_count'],12)
        self.assertEqual(p['exit_ms'],t+3600000)
        self.assertFalse(p['cash_state']['uncertain'])

    def test_missing_spot_valuation_still_withdraws(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR; del q['series']['spot'][str(t)]
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],t+BAR)
        self.assertIn('SPOT_VALUATION_UNOBSERVED',p['unknown_reasons'])

    def test_mark_risk_still_halts_on_quiet_future_bar(self):
        q,i=fixture(); t=i['earliest_entry_ms']+BAR
        q['series']['future'][str(t)]=bar(t,104,n=0,volume=0)
        q['series']['mark'][str(t)]=bar(t,160,n=0,volume=0)
        p=self.check_path(q,i)
        self.assertEqual(p['exit_ms'],t+BAR)
        self.assertEqual(p['risk_reason'],'DRAWDOWN_8_PERCENT')
        self.assertTrue(p['cash_state']['halted'])

    def test_all_quiet_future_hold_bars_can_settle(self):
        q,i=fixture()
        for t in range(i['earliest_entry_ms']+BAR,q['expiry']+BAR,BAR):
            q['series']['future'][str(t)]=bar(t,104,n=0,volume=0)
        p=self.check_path(q,i)
        self.assertTrue(p['settled'])
        self.assertEqual(p['risk_status'],'MODEL_OBSERVED_ONLY')

    def test_audit_rejects_promoted_qualification(self):
        q,_=fixture(); r=replay.replay({'Q':q})
        r['summary']['actual_execution_qualified']=True
        with self.assertRaises(AssertionError): audit.verify({'Q':q},r)

    def test_audit_rejects_erased_tail(self):
        q,i=fixture(); s=scenario(delay=60); p=self.check_path(q,i,s)
        p['tail_bar_count']=0
        with self.assertRaises(AssertionError): audit.equal(audit.reference_path(q,s,i),p)

    def test_bad_clock_rejected(self):
        q,i=fixture(); i['earliest_entry_ms']-=BAR
        with self.assertRaisesRegex(ValueError,'INTENTION_CLOCK'):
            replay.quarter_path(q,scenario(),i)

    def test_nonfrozen_scenario_rejected(self):
        q,i=fixture(); s=scenario(); s['delay_minutes']=10
        with self.assertRaisesRegex(ValueError,'SCENARIO_SCOPE'):
            replay.quarter_path(q,s,i)

    def test_auditor_rejects_tampered_cash(self):
        q,i=fixture(); p=self.check_path(q,i); p['cash_net']+=.01
        with self.assertRaises(AssertionError): audit.equal(audit.reference_path(q,scenario(),i),p)

    def test_bad_ohlc_rejected(self):
        b=bar(0); b['h']='99'
        with self.assertRaises(ValueError): replay.usable(b,0)


if __name__=='__main__': unittest.main()
