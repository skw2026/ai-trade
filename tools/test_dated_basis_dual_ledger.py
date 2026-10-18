from copy import deepcopy
from decimal import Decimal as D
import math
import unittest
from unittest.mock import patch

import dated_basis_dual_ledger as model
import audit_dated_basis_dual_ledger as audit


B = model.BAR
START = 100 * model.DAY


def case(**kw):
    data = dict(entry_ms=START,spot=100,future=104,exit_ms=START+90*model.DAY,
                exit_spot=100,exit_future=100,settlement=True)
    data.update(kw)
    return data


def position(c, state=None):
    state = state or model.CashState(c.get('capital',10000),c.get('peak',10000))
    return model.Position(state,entry_ms=c['entry_ms'],spot=c['spot'],future=c['future'],
                          cost=c.get('cost','stress'),financing_status='DECLARED_SELF_FINANCED_MODEL',
                          liabilities=[model.Liability(**x) for x in c.get('liabilities',[])])


def run(c):
    p = position(c)
    for b in c.get('bars',[]):
        p.observe_bar(b['t'],spot_low=b['sl'],spot_high=b['sh'],mark_low=b['ml'],mark_high=b['mh'])
    return p.close(c['exit_ms'],spot=c['exit_spot'],future=c['exit_future'],settlement=c.get('settlement',False))


def bar(t=START, sl=100, sh=100, ml=104, mh=104):
    return dict(t=t,sl=sl,sh=sh,ml=ml,mh=mh)


class DualLedgerTests(unittest.TestCase):
    audit_receipts = []

    def checked(self, c):
        result = run(c)
        expected = audit.reference(c)
        for key in ('cash_net','cash_drawdown','gross','slippage_drag','execution_fees','financing_cost',
                    'minimum_margin_ratio','risk_reason'):
            value = expected[key]
            if isinstance(value,D):
                self.assertLessEqual(abs(D(str(result[key]))-value), max(D('1e-7'),abs(value)*D('1e-10')),key)
            else:
                self.assertEqual(result[key],value,key)
        self.assertAlmostEqual(result['cash_state'].equity,float(expected['end_cash']),places=7)
        self.assertAlmostEqual(result['cash_state'].peak,float(expected['end_peak']),places=7)
        for rate,value in expected['benchmark_charges'].items():
            self.assertAlmostEqual(result['benchmark_charges'][rate],float(value),places=7)
        self.audit_receipts.append(dict(checked_bars=len(c.get('bars',[])),cash_net=result['cash_net'],
            financing_cost=result['financing_cost'],benchmark_charges=result['benchmark_charges'],
            risk_reason=result['risk_reason'],cash_drawdown=result['cash_drawdown']))
        return result

    def test_full_90_day_cash_and_benchmark_separation(self):
        c=case(peak=10800,bars=[bar(t) for t in range(START,START+90*model.DAY,B)])
        r=self.checked(c)
        self.assertIsNone(r['risk_reason'])
        self.assertLess(r['cash_drawdown'],.08)
        self.assertGreater(r['execution_fees'],0)
        self.assertGreater(r['benchmark_charges']['0.12'],0)

    def test_benchmark_changes_cannot_change_cash_or_risk(self):
        c=case(peak=10800,bars=[bar()]); baseline=run(c)
        with patch.object(model,'BENCHMARKS',(0,.06,.12,.5)):
            changed=run(c)
        for key in ('cash_state','cash_net','cash_drawdown','risk_reason','minimum_margin_ratio'):
            self.assertEqual(baseline[key],changed[key])
        self.assertNotEqual(baseline['benchmark_charges'],changed['benchmark_charges'])

    def test_no_basis_still_loses(self):
        r=self.checked(case(future=100))
        self.assertLess(r['cash_net'],0)
        self.assertGreater(r['execution_fees'],0)
        self.assertGreater(r['slippage_drag'],0)

    def test_real_cash_shock_stops(self):
        r=self.checked(case(peak=10800,bars=[bar(sl=70,sh=70)],exit_ms=START+B,
                            exit_spot=70,exit_future=104,settlement=False))
        self.assertEqual(r['risk_reason'],'DRAWDOWN_8_PERCENT')
        self.assertTrue(r['cash_state'].halted)
        self.assertGreater(r['cash_drawdown'],.08)

    def test_known_usdt_obligation_hits_cash_stop(self):
        c=case(peak=10800,bars=[bar()],exit_ms=START+B,exit_future=104,settlement=False,
               liabilities=[dict(event_id='interest',time_ms=START+B,usdt=50)])
        r=self.checked(c)
        self.assertEqual(r['financing_cost'],50)
        self.assertEqual(r['risk_reason'],'DRAWDOWN_8_PERCENT')

    def test_btc_obligation_hits_cash_and_margin(self):
        base=self.checked(case(bars=[bar()]))
        r=self.checked(case(bars=[bar()],liabilities=[dict(event_id='btc-interest',time_ms=START,btc=.5)]))
        self.assertLess(r['minimum_margin_ratio'],base['minimum_margin_ratio'])
        self.assertGreater(r['financing_cost'],0)
        self.assertLess(r['cash_net'],base['cash_net'])

    def test_not_yet_accrued_obligation_not_debited(self):
        c=case(exit_ms=START+B,liabilities=[dict(event_id='later',time_ms=START+2*B,usdt=200)])
        r=self.checked(c)
        self.assertEqual(r['financing_cost'],0)

    def test_early_trade_close_retains_future_slippage(self):
        r=self.checked(case(exit_ms=START+B,settlement=False,exit_future=103))
        self.assertGreater(r['slippage_drag'],0)

    def test_negative_btc_balance_is_bought_at_ask(self):
        c=case(liabilities=[dict(event_id='coins',time_ms=START,btc=40)])
        r=self.checked(c)
        self.assertAlmostEqual(r['financing_cost'],40*100*1.001)
        self.assertTrue(r['cash_state'].halted)

    def test_margin_hard_stop_independent_of_total_cash_drawdown(self):
        c=case(capital=1000000,peak=1000000,bars=[bar(ml=1000000,mh=1000000)],
               exit_ms=START+B,exit_future=1000000,settlement=False)
        r=self.checked(c)
        self.assertEqual(r['risk_reason'],'MARGIN_BUFFER')
        self.assertLess(r['cash_drawdown'],.08)

    def test_cash_and_peak_inherited_not_benchmark_excess(self):
        first=self.checked(case(bars=[bar(t) for t in range(START,START+90*model.DAY,B)]))
        p=position(case(entry_ms=START+100*model.DAY),first['cash_state'])
        self.assertEqual(p.initial.equity,10000+first['cash_net'])
        self.assertEqual(p.initial.peak,first['cash_state'].peak)
        self.assertNotEqual(p.initial.equity,10000+first['benchmark_excess']['0.12'])

    def test_hard_stopped_state_cannot_reenter(self):
        with self.assertRaisesRegex(ValueError,'PATH_CANNOT_REENTER'):
            position(case(),model.CashState(halted=True))

    def test_inherited_drawdown_cannot_be_hidden_with_false_latch(self):
        with self.assertRaisesRegex(ValueError,'INHERITED_HARD_RISK'):
            position(case(),model.CashState(9000,10000))

    def test_unknown_financing_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'UNKNOWN_OR_UNSUPPORTED_FINANCING'):
            model.Position(model.CashState(),entry_ms=START,spot=100,future=104,financing_status='UNKNOWN')

    def test_borrowed_principal_is_not_assumed_free(self):
        for currency in ('opening_debt_usdt','opening_debt_btc'):
            with self.subTest(currency=currency),self.assertRaisesRegex(ValueError,'BORROWED_PRINCIPAL'):
                model.Position(model.CashState(),entry_ms=START,spot=100,future=104,
                               financing_status='DECLARED_SELF_FINANCED_MODEL',**{currency:1})

    def test_cash_shortfall_not_implicitly_borrowed(self):
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_SELF_FINANCED_CASH'):
            position(case(),model.CashState(100,100))

    def test_liability_unique_sorted_nonnegative_finite(self):
        bad_lists=[
            [dict(event_id='x',time_ms=START,usdt=-1)],
            [dict(event_id='x',time_ms=START,usdt=math.nan)],
            [dict(event_id='x',time_ms=START-B,usdt=1)],
            [dict(event_id='x',time_ms=START),dict(event_id='x',time_ms=START+B)],
            [dict(event_id='x',time_ms=START+B),dict(event_id='y',time_ms=START)],
        ]
        for liabilities in bad_lists:
            with self.subTest(liabilities=liabilities),self.assertRaises(ValueError):
                position(case(liabilities=liabilities))

    def test_missing_risk_blocks_following_entry_even_after_close(self):
        p=position(case());p.missing_risk_bar(START)
        r=p.close(START+B,spot=100,future=104)
        self.assertEqual(r['risk_status'],'UNKNOWN')
        with self.assertRaisesRegex(ValueError,'PATH_CANNOT_REENTER'):
            position(case(),r['cash_state'])

    def test_silent_risk_gap_cannot_be_reported_complete(self):
        p=position(case())
        p.observe_bar(START+B,spot_low=100,spot_high=100,mark_low=104,mark_high=104)
        r=p.close(START+2*B,spot=100,future=104)
        self.assertEqual(r['risk_status'],'UNKNOWN')
        self.assertTrue(r['cash_state'].uncertain)
        self.assertEqual(run(case())['risk_status'],'UNKNOWN')

    def test_bad_exit_quote_cannot_fake_fill_or_close(self):
        p=position(case())
        with self.assertRaises(ValueError):p.close(START+B,spot=math.nan,future=104)
        self.assertFalse(p.closed)
        self.assertEqual(p.last_ms,START)

    def test_no_early_fill_after_risk_bar(self):
        p=position(case(peak=10800))
        p.observe_bar(START,spot_low=70,spot_high=70,mark_low=104,mark_high=104)
        self.assertEqual(p.pending_exit_ms,START+B)
        with self.assertRaises(ValueError):p.close(START,spot=70,future=104)

    def test_duplicate_preentry_or_overlapping_observation_rejected(self):
        p=position(case())
        for t in (START-B,):
            with self.assertRaises(ValueError):
                p.observe_bar(t,spot_low=100,spot_high=100,mark_low=104,mark_high=104)
        p.observe_bar(START,spot_low=100,spot_high=100,mark_low=104,mark_high=104)
        with self.assertRaises(ValueError):p.missing_risk_bar(START)

    def test_closed_position_cannot_settle_twice(self):
        p=position(case());p.close(START+B,spot=100,future=104)
        with self.assertRaises(ValueError):p.close(START+2*B,spot=100,future=104)

    def test_never_claims_market_or_candidate_qualification(self):
        r=run(case())
        for key in ('actual_execution_qualified','candidate_qualified','g4'):
            self.assertIs(r[key],False)


class OpportunityTests(unittest.TestCase):
    expiry=200*model.DAY

    def rows(self,n=30):
        return [dict(t=self.expiry-90*model.DAY-B+i*B,spot=100,future=104,valid=True) for i in range(n)]

    def choose(self,rows,now=None):
        return model.first_opportunity(rows,expiry=self.expiry,observed_through=now or self.expiry)

    def test_first_not_best_and_two_clock_latency(self):
        rows=self.rows();rows[-1]['future']=200
        r=self.choose(rows)
        self.assertEqual(r['decision_bar'],rows[11]['t'])
        self.assertEqual(r['earliest_entry_ms'],rows[11]['t']+2*B)
        self.assertEqual(r['available_ms'],rows[11]['t']+B)

    def test_incomplete_current_bar_cannot_trigger(self):
        rows=self.rows()
        self.assertIsNone(self.choose(rows,rows[11]['t']))
        self.assertIsNotNone(self.choose(rows,rows[11]['t']+B))

    def test_invalid_quotes_break_streak(self):
        rows=self.rows();rows[10]['valid']=False
        self.assertEqual(self.choose(rows)['decision_bar'],rows[22]['t'])

    def test_gap_breaks_streak(self):
        rows=self.rows();del rows[10]
        self.assertEqual(self.choose(rows)['decision_bar'],rows[21]['t'])

    def test_nonpositive_quotes_break_streak(self):
        rows=self.rows();rows[10]['future']=100
        self.assertEqual(self.choose(rows)['decision_bar'],rows[22]['t'])

    def test_rejects_duplicate_and_reversed_input(self):
        rows=self.rows()
        for bad in ([rows[0],rows[0]],list(reversed(rows[:3]))):
            with self.assertRaises(ValueError):self.choose(bad)

    def test_future_changes_cannot_change_past_intention(self):
        rows=self.rows();altered=deepcopy(rows)
        for row in altered[12:]:row.update(spot=200,future=50)
        self.assertEqual(self.choose(rows),self.choose(altered))

    def test_forecast_agrees_with_independent_decimal_formula(self):
        for s,f in ((100,104),(100,100),(65000,66000)):
            S,F=D(s),D(f)
            quantity=D(2500)/(F*D('.999'))
            spent=quantity*S*D('1.001')*D('1.002')
            coins=D(2500)*D('.999')/(S*D('1.0025'))-quantity*D('.001')
            expected=coins*S*D('.999')*D('.998')-spent
            self.assertAlmostEqual(model.pressure_forecast(s,f),float(expected),places=8)

    def test_domain_and_no_opportunity(self):
        rows=self.rows()
        for row in rows:row['t']=row['t']-91*model.DAY
        self.assertIsNone(self.choose(rows))
        rows=self.rows()
        for row in rows:row['future']=100
        self.assertIsNone(self.choose(rows))


class ConfirmationTests(unittest.TestCase):
    def test_four_all_wins_still_above_five_percent(self):
        r=audit.sign_capacity();first=r['rows'][0]
        self.assertEqual(first['all_positive_null_probability'],.0625)
        self.assertEqual(first['critical_wins'],5)
        self.assertEqual(first['power'],{'3/5':0,'3/4':0})

    def test_calibrations_do_not_admit_an_experiment(self):
        r=audit.sign_capacity()
        self.assertFalse(r['market_payoff_admitted'])
        self.assertEqual(r['current_2023_independent_confirmation_blocks'],0)
        self.assertLess(r['win_rate_counterexample']['total'],0)
        self.assertTrue(all(x['size']<=.05 for x in r['rows']))
        self.assertFalse(r['g4'])


if __name__=='__main__':unittest.main()
