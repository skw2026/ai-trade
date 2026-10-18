from decimal import Decimal as D, localcontext
from fractions import Fraction as F
import unittest

from dated_basis_cash_boundary import conditional_lower_bound, delivery_cash, entry, proof_obligations, required_index_ratio
from dated_basis_dual_ledger import BAR, CashState, Position, pressure_forecast
from audit_dated_basis_dual_ledger import reference


def decimal_cash(s0, f0, s, i, usd=0, btc=0):
    with localcontext() as ctx:
        ctx.prec = 50
        s0,f0,s,i,usd,btc = map(lambda v:D(str(v)),(s0,f0,s,i,usd,btc))
        q=D(2500)/(f0*D('.999'))
        balance=q-q*D('.001')+D(2500)*(1/i-1/(f0*D('.999')))-D('2.5')/i-btc
        execution=s*(D('.999') if balance>=0 else D('1.001'))
        return balance*execution-abs(balance)*execution*D('.002')-q*s0*D('1.001')*D('1.002')-usd


class BoundaryTests(unittest.TestCase):
    receipts=[]

    def test_delivery_cash_decimal_grid_both_coin_branches(self):
        count=0
        for s in (50,100,200,4000,200000):
            for ratio in ('.99','1','1.0025','1.1','2'):
                for usd,btc in ((0,0),(2,'.01')):
                    i=D(s)*D(ratio)
                    exact=delivery_cash(100,104,s,i,usdt_owed=usd,btc_owed=btc)
                    dec=decimal_cash(100,104,s,i,usd,btc)
                    with localcontext() as ctx:
                        ctx.prec=50
                        self.assertLess(abs(D(exact.numerator)/D(exact.denominator)-dec),D('1e-38'))
                    count+=1
        self.assertEqual(count,50)

    def test_missing_bounds_stay_unbound(self):
        self.assertEqual(conditional_lower_bound(100,104)['status'],'UNBOUND')
        self.assertIsNone(conditional_lower_bound(100,104,spot_max=200,index_ratio_max='1.0025')['lower_cash'])

    def test_conditional_box_positive_is_not_market_admission(self):
        r=conditional_lower_bound(100,104,spot_max=200,index_ratio_max='1.0025',usdt_owed_max=0,btc_owed_max=0)
        self.assertGreater(r['lower_cash'],0)
        self.assertFalse(r['market_certified'])

    def test_box_minimum_including_negative_coin_branch(self):
        for smax in (200,200000):
            for ratio_max in (D('1.0025'),D(2)):
                bound=conditional_lower_bound(100,104,spot_max=smax,index_ratio_max=ratio_max,usdt_owed_max=3,btc_owed_max='.01')['lower_cash']
                for s in (1,50,smax):
                    for r in (D('.5'),D(1),ratio_max):
                        for usd,btc in ((0,0),(3,'.01')):
                            value=delivery_cash(100,104,s,D(s)*r,usdt_owed=usd,btc_owed=btc)
                            self.assertGreaterEqual(value,bound)
                self.assertEqual(bound,delivery_cash(100,104,smax,D(smax)*ratio_max,usdt_owed=3,btc_owed='.01'))

    def test_exact_breakeven_and_monotonicity(self):
        ratio=required_index_ratio(100,104,100)
        self.assertEqual(delivery_cash(100,104,100,100*ratio),0)
        self.assertLess(delivery_cash(100,104,100,100*ratio*F(10001,10000)),0)
        self.assertGreater(delivery_cash(100,104,100,100*ratio*F(9999,10000)),0)

    def test_coin_commission_leaves_negative_spot_sensitivity(self):
        slope=-entry(100,104)['fee_btc']*F(999,1000)*F(998,1000)
        self.assertEqual(delivery_cash(100,104,200,200)-delivery_cash(100,104,100,100),100*slope)

    def test_invalid_bounds_do_not_become_zero_cost(self):
        for s in (0,-1,'NaN','Infinity'):
            with self.subTest(s=s),self.assertRaises((ValueError,ZeroDivisionError)):
                conditional_lower_bound(100,104,spot_max=s,index_ratio_max=1,usdt_owed_max=0,btc_owed_max=0)
        with self.assertRaises(ValueError):delivery_cash(100,104,100,100,usdt_owed=-1)

    def test_identification_does_not_raise_product_to_no_loss_guarantee(self):
        p=proof_obligations()
        self.assertFalse(p['worst_case_guarantee_required_by_product'])
        self.assertFalse(p['new_economic_admission'])

    def test_four_counterfactuals_match_frozen_engine_and_audit(self):
        self.assertGreater(pressure_forecast(100,104),0)
        cases=[('flat_spot_assumed_index',100,100.25,100,100.25,True),
               ('index_gap',100,110,100,110,True),
               ('coin_fee_exposure',4000,4000,4000,4000,True),
               ('drawdown_exit_overshoot',100,160,100,250,False)]
        for name,os,om,es,ef,settle in cases:
            p=Position(CashState(),entry_ms=0,spot=100,future=104,financing_status='DECLARED_SELF_FINANCED_MODEL')
            observed=p.observe_bar(0,spot_low=os,spot_high=os,mark_low=om,mark_high=om)
            result=p.close(BAR,spot=es,future=ef,settlement=settle)
            case=dict(entry_ms=0,spot=100,future=104,exit_ms=BAR,exit_spot=es,exit_future=ef,settlement=settle,
                      bars=[dict(t=0,sl=os,sh=os,ml=om,mh=om)])
            audit=reference(case)
            self.assertAlmostEqual(result['cash_net'],float(audit['cash_net']),places=8)
            self.assertAlmostEqual(result['cash_drawdown'],float(audit['cash_drawdown']),places=10)
            self.assertEqual(result['risk_reason'],audit['risk_reason'])
            if settle:self.assertAlmostEqual(result['cash_net'],float(delivery_cash(100,104,es,ef)),places=8)
            if name=='flat_spot_assumed_index':self.assertGreater(result['cash_net'],0)
            else:self.assertLess(result['cash_net'],0)
            if name=='drawdown_exit_overshoot':
                self.assertEqual(observed['reason'],'DRAWDOWN_8_PERCENT')
                self.assertGreater(result['cash_drawdown'],.08)
                self.assertLess(result['cash_net'],observed['cash_low']-10000)
                self.assertTrue(result['cash_state'].halted)
            else:self.assertIsNone(result['risk_reason'])
            self.assertFalse(result['g4'])
            self.receipts.append(dict(name=name,forecast=pressure_forecast(100,104),cash_net=result['cash_net'],
                observed_cash=observed['cash_low'],drawdown=result['cash_drawdown'],minimum_margin=result['minimum_margin_ratio'],risk=result['risk_reason']))


if __name__=='__main__':unittest.main()
