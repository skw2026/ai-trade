import copy
import datetime as dt
import math
import unittest

import btc_fee_pressure as m
import audit_btc_fee_pressure as independent
from audit_btc_old_output import audit as ledger_audit
from test_btc_old_output_model import fixture, START
from test_btc_old_output_decision import path


class FeeInputTests(unittest.TestCase):
    def raw(self,kind=0,value='1',date='2021-01-01',unit=None,version='OBM v0.1.0'):
        return (','.join(m.FIELDS)+'\n'+','.join([date,m.SERIES[kind],value,
            unit or ('BTC' if kind==0 else 'WU'),'daily',version])+'\n').encode()
    def admit(self,a=None,b=None):
        return m.admit(a or self.raw(),b or self.raw(1,'10000'),['2021-01-01'])
    def test_density_log_and_unit(self):
        self.assertAlmostEqual(self.admit()['2021-01-01'],math.log(10000))
    def test_required_days(self):
        self.assertEqual(len(set(m.required_dates())),790)
    def test_nonfinite_and_negative(self):
        for v in ('NaN','Infinity','-1'):
            with self.subTest(v=v),self.assertRaisesRegex(ValueError,'VALUE'):
                self.admit(self.raw(value=v))
    def test_zero_in_either_series(self):
        for kind in (0,1):
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'REQUIRED_ZERO'):
                self.admit(self.raw(value='0') if kind==0 else None,
                           self.raw(1,'0') if kind==1 else None)
    def test_precision_both_units(self):
        for kind,v in ((0,'0.000000001'),(1,'1.5')):
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'PRECISION'):
                self.admit(self.raw(value=v) if kind==0 else None,
                           self.raw(1,v) if kind==1 else None)
    def test_duplicate(self):
        a=self.raw(); a+=a.splitlines()[1]+b'\n'
        with self.assertRaisesRegex(ValueError,'DATE_ORDER'): self.admit(a)
    def test_missing(self):
        with self.assertRaisesRegex(ValueError,'DATE_MISSING'): self.admit(self.raw(date='2021-01-02'))
    def test_identity(self):
        with self.assertRaisesRegex(ValueError,'IDENTITY'): self.admit(self.raw(unit='USD'))
    def test_release(self):
        with self.assertRaisesRegex(ValueError,'RELEASE'): self.admit(self.raw(version='v2'))
    def test_schema_and_extra_field(self):
        with self.assertRaisesRegex(ValueError,'SCHEMA'): self.admit(b'bad\n')
        with self.assertRaisesRegex(ValueError,'ROW_WIDTH'):
            self.admit(self.raw().rstrip()+b',extra\n')
    def test_ratio_scale_invariance(self):
        self.assertEqual(self.admit(),self.admit(self.raw(value='2'),self.raw(1,'20000')))


class FeatureTests(unittest.TestCase):
    def fixture(self):
        start=m.execution.millis('2021-01-11'); end=start+m.execution.DAY
        rows=[dict(timestamp=t,price=10000.+i) for i,t in enumerate(
            range(start-8*m.execution.DAY,end,m.execution.STEP))]
        log={str(m.execution.day(start)+dt.timedelta(days=i)): math.sin(i/2.)+2
             for i in range(-40,10)}
        return rows,log,start,end
    def test_future_chain_and_prices_do_not_change_decision(self):
        rows,log,a,b=self.fixture(); original=m.features(rows,log,a,b)
        for r in rows:
            if r['timestamp']>=a:r['price']*=10
        for d in log:
            if d>=str(m.execution.day(a)-dt.timedelta(days=2)):log[d]+=100
        self.assertEqual(m.features(rows,log,a,b),original)
    def test_current_source_day_excluded_from_standardization(self):
        rows,log,a,b=self.fixture(); d=m.execution.day(a)-dt.timedelta(days=3)
        past=[log[str(d-dt.timedelta(days=i))] for i in range(1,29)]
        mu=sum(past)/28; sd=(sum((x-mu)**2 for x in past)/28)**.5
        log[str(d)]=mu+sd
        self.assertAlmostEqual(m.features(rows,log,a,b)[a]['z'],1.)
    def test_delayed_source_clock(self):
        rows,log,a,b=self.fixture()
        self.assertEqual(m.features(rows,log,a,b,4)[a]['source_day'],'2021-01-07')
        with self.assertRaisesRegex(ValueError,'FIXED_LAG'):m.features(rows,log,a,b,2)
    def test_constant_chain_only_disables_mechanism(self):
        rows,log,a,b=self.fixture();log={d:1. for d in log}
        f=m.features(rows,log,a,b)[a]
        self.assertIsNone(f['z']);self.assertEqual(m.execution.signal(f,(1,0,1)),0)
        f['r']=2.;self.assertEqual(m.execution.signal(f,(0,1,1)),1)
    def test_missing_chain_fails(self):
        rows,log,a,b=self.fixture(); del log['2021-01-08']
        with self.assertRaises(KeyError):m.features(rows,log,a,b)
    def test_independent_decimal_feature_and_mutation(self):
        rows,_,a,b=self.fixture(); days=[str(m.execution.day(a)+dt.timedelta(days=i)) for i in range(-40,10)]
        fees=','.join(m.FIELDS)+'\n';weights=','.join(m.FIELDS)+'\n'
        for i,d in enumerate(days):
            fees+=f'{d},{m.SERIES[0]},{i+1},BTC,daily,OBM v0.1.0\n'
            weights+=f'{d},{m.SERIES[1]},100000,WU,daily,OBM v0.1.0\n'
        log=m.admit(fees.encode(),weights.encode(),days);fs=m.features(rows,log,a,b)
        independent.features(fees.encode(),weights.encode(),rows,fs,a,b,3)
        fs[a]['z']+=.01
        with self.assertRaisesRegex(ValueError,'STANDARDIZATION'):
            independent.features(fees.encode(),weights.encode(),rows,fs,a,b,3)


class SelectionTests(unittest.TestCase):
    def training(self):return [path(-10-i,p) for i,p in enumerate(sum(m.grids(),[]))]
    def test_fixed_eight_vectors(self):
        a,b=m.grids();self.assertEqual(len(a)+len(b),8);self.assertIn(m.FIXED,a)
    def test_negative_training_allowed(self):
        t=self.training();s=m.training_decision(t)
        self.assertEqual(s['status'],'TRAINING_ADMITTED');self.assertEqual(s['mechanism_index'],0)
        independent.selection(t,s)
    def test_hard_stopped_control_not_blocker_or_candidate(self):
        t=self.training()
        for r in t[4:]:
            for b in r['books']:b['stopped']={'t':1}
        for b in t[0]['books']:b['stopped']={'t':1}
        s=m.training_decision(t);self.assertEqual(s['mechanism_index'],1);self.assertEqual(s['price_index'],4)
        self.assertEqual(len(m.specifications(s,t)),6);independent.selection(t,s)
    def test_all_candidates_stopped(self):
        t=self.training()
        for r in t[:4]:
            for b in r['books']:b['stopped']={'t':1}
        s=m.training_decision(t);self.assertEqual(s['status'],'NO_GO_TRAIN_RISK');independent.selection(t,s)
        with self.assertRaisesRegex(ValueError,'NOT_ADMITTED'):m.specifications(s,t)
    def test_capacity_does_not_switch_runner_up(self):
        t=self.training();t[0]['books'][0]['cycles']=[]
        s=m.training_decision(t);self.assertEqual(s['mechanism_index'],0)
        self.assertEqual(s['status'],'INSUFFICIENT_TRAIN_CAPACITY');independent.selection(t,s)
    def test_incomplete_control_rejected(self):
        t=self.training();t[4]['books'][0]['quantity']=.001
        with self.assertRaisesRegex(ValueError,'INCOMPLETE'):m.training_decision(t)
    def test_tie_prefers_low_turnover_then_lexical(self):
        t=self.training()
        for r in t:
            for b in r['books']:b['net']=-1;b['turnover']=200
        for b in t[1]['books']:b['turnover']=100
        s=m.training_decision(t);self.assertEqual(s['mechanism_index'],1);independent.selection(t,s)
    def test_selection_audit_catches_changed_index(self):
        t=self.training();s=m.training_decision(t);s['mechanism_index']=2
        with self.assertRaisesRegex(ValueError,'SELECTION'):independent.selection(t,s)


class VerdictTests(unittest.TestCase):
    def paths(self):return [path(300,(-1,0,1)),path(100,m.FIXED),path(100,(0,1,1)),path(400),path(250),path(250)]
    def check(self,p):
        r=m.verdict(p); independent.decision(p,r); return r
    def test_positive_only_development(self):self.assertEqual(self.check(self.paths())['status'],'DEVELOPMENT_SUPPORTED')
    def test_both_costs_diversity(self):
        p=self.paths();p[0]['books'][0]['cycles']=p[0]['books'][0]['cycles'][:1]
        self.assertEqual(self.check(p)['status'],'INSUFFICIENT_DIVERSITY')
    def test_no_trade_is_not_success(self):
        p=self.paths();p[0]=path(0,(-1,0,1));self.assertEqual(self.check(p)['status'],'NO_GO_ECONOMIC')
    def test_risk_and_incomplete_both_visible(self):
        p=self.paths();p[0]['books'][1]['stopped']={'t':1};p[0]['books'][0]['quantity']=.1
        r=self.check(p);self.assertTrue(r['hard_risk']);self.assertEqual(r['status'],'INSUFFICIENT_INPUT_OR_EXECUTION')
    def test_sensitivity_hard_stop(self):
        p=self.paths();p[4]['books'][0]['stopped']={'t':1};self.assertEqual(self.check(p)['status'],'NO_GO_RISK')
    def test_fixed_winner_not_learning(self):
        p=self.paths();p[0]['parameters']=list(m.FIXED);p[1]=copy.deepcopy(p[0])
        r=self.check(p);self.assertFalse(r['learned']);self.assertEqual(r['status'],'DEVELOPMENT_SUPPORTED')
    def test_base_increment_required(self):
        p=self.paths();p[2]['books'][0]['net']=299
        self.assertIn('PRICE_INCREMENT',self.check(p)['reasons'])
    def test_independent_decision_mutation(self):
        p=self.paths();r=m.verdict(p);r['status']='NO_GO_RISK'
        with self.assertRaisesRegex(ValueError,'ECONOMIC_VERDICT'):independent.decision(p,r)
    def test_reused_execution_decimal_identity(self):
        rows,fs=fixture()
        result=m.execution.simulate(rows,fs,(-1,0,1),START,START+6*m.execution.DAY)
        self.assertEqual(ledger_audit(result,rows)['status'],'DECIMAL_LEDGER_AND_RISK_VERIFIED')


if __name__=='__main__':unittest.main()
