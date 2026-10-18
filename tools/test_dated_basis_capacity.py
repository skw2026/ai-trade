import copy
from decimal import Decimal as D
import math
import unittest

import audit_dated_basis_capacity as audit
import dated_basis_opportunity_capacity as model

DAY = model.DAY
BAR = model.BAR
EXPIRY = 100*DAY
T = EXPIRY-30*DAY-BAR


def bar(t, price):
    return dict(c=str(price),v='1',n=3,complete=True,close_ms=t+BAR-1)


def fixture(count=1):
    return {'test':dict(expiry=EXPIRY,settlement='100',series={
        'spot':{str(T+i*BAR):bar(T+i*BAR,100) for i in range(count)},
        'future':{str(T+i*BAR):bar(T+i*BAR,110) for i in range(count)},
        'mark':{}})}


def target(projection, t=T):
    return next(r for r in projection['test'] if r['t']==t)


class CapacityTests(unittest.TestCase):
    def test_wallet_closed_form_matches(self):
        for s,f,h in ((100,110,30*DAY),(60000,60100,90*DAY-BAR),(1,2,DAY)):
            for key in model.LAYERS:
                audit.near(model.amount(s,f,h,key),audit.exact(str(s),str(f),h,key))

    def test_friction_parity_negative(self):
        for key in model.LAYERS:
            self.assertLess(model.amount(100,100,DAY,key),0)

    def test_positive_basis_can_cover_cost(self):
        self.assertGreater(model.amount(100,110,90*DAY,model.STRONG),0)

    def test_cost_and_conversion_monotonic(self):
        v={k:model.amount(100,110,30*DAY,k) for k in model.LAYERS}
        self.assertGreater(v['base_cash'],v['base_economic'])
        self.assertGreater(v['base_economic'],v['stress_economic'])
        self.assertGreater(v['stress_economic'],v['stress_terminal_economic'])
        self.assertGreater(v['stress_terminal_cash'],v['stress_terminal_economic'])

    def test_cash_time_invariant_economic_decreases(self):
        self.assertEqual(model.amount(100,110,DAY,'base_cash'),model.amount(100,110,90*DAY,'base_cash'))
        self.assertGreater(model.amount(100,110,DAY,'base_economic'),model.amount(100,110,90*DAY,'base_economic'))

    def test_invalid_price_rejected(self):
        for price in (0,-1,math.inf,math.nan):
            with self.assertRaises(ValueError):model.amount(price,100,DAY,'base_cash')
            with self.assertRaises(ValueError):model.amount(100,price,DAY,'base_cash')

    def test_invalid_time_and_layer(self):
        for time in (0,-1,math.inf,math.nan):
            with self.assertRaises(ValueError):model.amount(100,110,time,'base_cash')
        with self.assertRaises(ValueError):model.amount(100,110,DAY,'arbitrary')

    def test_terminal_price_and_mark_isolated(self):
        original=fixture();changed=copy.deepcopy(original)
        changed['test']['settlement']='9999999'
        changed['test']['series']['mark']={'secret':'not inspected'}
        self.assertEqual(model.project(original),model.project(changed))

    def test_future_quote_cannot_change_earlier_budget(self):
        original=fixture(2);changed=copy.deepcopy(original)
        changed['test']['series']['future'][str(T+BAR)]['c']='9999999'
        self.assertEqual(target(model.project(original)),target(model.project(changed)))

    def test_projection_drops_future_fields(self):
        q=fixture();q['test']['series']['spot'][str(T)].update(o='private',h='private',l='private')
        p=model.project(q)
        self.assertEqual(set(target(p)),{'t','expiry','reasons','spot','future'})
        self.assertEqual(len(p['test']),89*288+1)

    def test_missing_quote_is_unknown(self):
        q=fixture();q['test']['series']['future']={}
        self.assertEqual(target(model.project(q))['reasons'],['future_missing'])

    def test_zero_trade_and_incomplete_excluded(self):
        q=fixture();b=q['test']['series']['future'][str(T)]
        b.update(n=0,v='0',complete=False)
        self.assertEqual(target(model.project(q))['reasons'],['future_incomplete','future_no_trades'])

    def test_wrong_close_clock_excluded(self):
        q=fixture();q['test']['series']['spot'][str(T)]['close_ms']=T
        self.assertIn('spot_incomplete',target(model.project(q))['reasons'])

    def test_projection_rejects_invalid_prices(self):
        q=fixture();q['test']['series']['future'][str(T)]['c']='NaN'
        with self.assertRaises(ValueError):model.project(q)

    def test_runs_gaps_and_order(self):
        self.assertEqual(model.longest_runs([0,BAR,3*BAR]),[2,1])
        self.assertEqual(model.longest_runs([]),[])
        for values in ([BAR,0],[0,0]):
            with self.assertRaises(ValueError):model.longest_runs(values)

    def test_numerical_zero_not_positive(self):
        rows=[dict(t=i*BAR,values={'k':v}) for i,v in enumerate((-1,0,1e-10,1e-9,1e-8))]
        result=model.summarize(rows,'k',0)
        self.assertEqual(result['positive_quotes'],1)

    def test_persistence_boundary_and_missing_split(self):
        rows=[dict(t=i*BAR,values={'k':1}) for i in range(12)]
        self.assertEqual(model.summarize(rows,'k',0)['runs_at_least_1h'],1)
        rows[6]['values']=None
        result=model.summarize(rows,'k',0)
        self.assertEqual(result['runs_at_least_1h'],0)
        self.assertEqual(result['runs'],2)

    def test_quarter_not_bar_count_controls_exit(self):
        def quarter(count,runs):return {'layers':{model.STRONG:dict(positive_quotes=count,runs_at_least_1h=runs)}}
        self.assertEqual(model.classify({'a':quarter(0,0)}),'NO_OBSERVED_PRESSURE_HEADROOM')
        self.assertEqual(model.classify({'a':quarter(10000,100)}),'TRANSIENT_OR_SINGLE_EXPIRY_ONLY')
        self.assertEqual(model.classify({'a':quarter(100,1),'b':quarter(100,1)}),'MULTI_EXPIRY_PERSISTENT_HEADROOM')

    def test_maturity_boundary_partition(self):
        for days,b in ((1,'1_to_7'),(7,'7_to_30'),(30,'30_to_60'),(60,'60_to_90'),(90,'60_to_90')):
            self.assertEqual(model.bucket(days*DAY),b)
        for value in (DAY-1,90*DAY+1):
            with self.assertRaises(ValueError):model.bucket(value)

    def test_complete_scan_and_independent_audit(self):
        source=fixture(12);projection=model.project(source)
        summary,ledgers=model.scan(projection)
        result=audit.audit(source,projection,summary,ledgers)
        self.assertEqual(result['checked_amounts'],72)
        self.assertEqual(result['persistent_expiries'],1)
        self.assertEqual(summary['status'],'TRANSIENT_OR_SINGLE_EXPIRY_ONLY')
        self.assertFalse(summary['candidate_qualified'])

    def test_independent_audit_rejects_tampering(self):
        source=fixture();projection=model.project(source)
        summary,ledgers=model.scan(projection)
        targetrow=next(r for r in ledgers['test'] if r['t']==T)
        targetrow['values']['base_cash']+=.01
        with self.assertRaises(AssertionError):audit.audit(source,projection,summary,ledgers)

    def test_original_date_formula_equivalence(self):
        import dated_basis_term_screen as original
        for days in (30,60,90):
            t=EXPIRY-days*DAY-BAR
            q=dict(expiry=EXPIRY,series={'spot':{t:{'c':100.}},'future':{t:{'c':110.}}})
            expected=original.decision(q,days)['expected_net']
            self.assertAlmostEqual(model.amount(100,110,days*DAY-BAR,'base_economic'),expected,places=8)


if __name__=='__main__':unittest.main()
