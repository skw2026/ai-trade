import copy
import datetime as dt
import unittest
import btc_old_output_model as m

START=m.millis('2025-01-13')

def row(t,p=10000.,volume=100.):
    return dict(timestamp=t,open=p,high=p,low=p,price=p,volume=volume,
        mark_open=p,mark_high=p,mark_low=p,mark_close=p,funding_rate_per_interval=0.)

def fixture(days=6,z=2.,r=0.):
    rows=[row(t) for t in range(START-m.STEP,START+days*m.DAY,m.STEP)]
    fs={t:dict(z=z,r=r,price=10000.,source_day=(m.day(t)-dt.timedelta(days=3)).isoformat(),
        latest_price_closed_at=t) for t in range(START,START+days*m.DAY,m.DAY)}
    return rows,fs

class ModelTests(unittest.TestCase):
    def run_case(self,days=6,**kw):
        rows,fs=fixture(days)
        return m.simulate(rows,fs,(1,0,1),START,START+days*m.DAY,**kw)
    def test_grid_and_fixed(self):
        a,b=m.grids();self.assertEqual((len(a),len(b)),(12,4));self.assertIn((-1,0,1),a)
    def test_directions_and_small_sd_ablation(self):
        self.assertEqual(m.signal(dict(z=2,r=-1),(1,1,1)),1)
        self.assertEqual(m.signal(dict(z=None,r=-2),(0,1,1)),-1)
        self.assertEqual(m.signal(dict(z=None,r=2),(1,1,1)),0)
    def test_rounding_and_adverse_ticks(self):
        self.assertEqual(m.quantity(.1239),.123)
        b=row(START,10000);b['high']=10100;b['low']=9900
        self.assertEqual(m.fill_price(b,1,False),10002.)
        self.assertEqual(m.fill_price(b,-1,False),9998.)
        self.assertGreater(m.fill_price(b,1,True),10100)
        self.assertLess(m.fill_price(b,-1,True),9900)
    def test_flat_price_costs_and_complete_exit(self):
        r=self.run_case()
        for b in r['books']:
            self.assertEqual(b['quantity'],0);self.assertEqual(len(b['cycles']),1)
            self.assertTrue(b['cycles'][0]['complete']);self.assertLess(b['net'],0)
            self.assertAlmostEqual(b['net'],b['gross']-b['fees']-b['slippage']-b['funding'])
        self.assertLess(r['books'][1]['net'],r['books'][0]['net'])
    def test_decision_and_horizon_latency(self):
        b=self.run_case()['books'][0]
        fills=[e for e in b['events'] if e['kind']=='fill']
        self.assertEqual(fills[0]['t'],START+m.STEP)
        self.assertEqual(fills[1]['t'],START+3*m.DAY+2*m.STEP)
    def test_delay_is_not_reselected(self):
        b=self.run_case(latency=2)['books'][0]
        fills=[e for e in b['events'] if e['kind']=='fill']
        self.assertEqual(fills[0]['t'],START+2*m.STEP)
        self.assertEqual(fills[1]['t'],START+3*m.DAY+4*m.STEP)
    def test_same_timestamp_funding_precedes_entry(self):
        rows,fs=fixture();rows[2]['funding_rate_per_interval']=.01
        rows[3]['funding_rate_per_interval']=.01
        r=m.simulate(rows,fs,(1,0,1),START,START+6*m.DAY)
        e=[x for x in r['books'][0]['events'] if x['kind']=='funding']
        self.assertEqual(len(e),1);self.assertEqual(e[0]['t'],START+2*m.STEP)
        self.assertEqual(e[0]['amount'],25.)
    def test_negative_funding_income(self):
        rows,fs=fixture();rows[3]['funding_rate_per_interval']=-.001
        r=m.simulate(rows,fs,(1,0,1),START,START+6*m.DAY)
        self.assertEqual(r['books'][0]['funding'],-2.5)
    def test_partial_entry_unwinds(self):
        rows,fs=fixture();rows[2]['volume']=1.
        r=m.simulate(rows,fs,(1,0,1),START,START+6*m.DAY)
        for b in r['books']:
            self.assertGreaterEqual(b['failed_entries'],1)
            self.assertFalse(b['cycles'][0]['complete'])
            self.assertEqual(b['cycles'][0]['end'],START+2*m.STEP)
    def test_zero_fill_not_a_complete_cycle(self):
        rows,fs=fixture()
        for x in rows:x['volume']=0.
        r=m.simulate(rows,fs,(1,0,1),START,START+6*m.DAY)
        self.assertEqual(r['books'][0]['cycles'],[]);self.assertEqual(r['books'][0]['net'],0)
    def test_exit_residual_is_insufficient(self):
        rows,fs=fixture()
        for x in rows:
            if x['timestamp']>=START+3*m.DAY:x['volume']=0.
        r=m.simulate(rows,fs,(1,0,1),START,START+6*m.DAY)
        self.assertEqual(r['books'][0]['insufficient'],'EXIT_RESIDUAL')
        self.assertGreater(r['books'][0]['quantity'],0)
    def test_coupled_hard_risk_stays_stopped(self):
        rows,fs=fixture()
        for x in rows:
            if x['timestamp']>=START+2*m.STEP:
                x.update(open=6000.,price=6000.,high=6000.,low=6000.,mark_open=6000.,
                         mark_close=6000.,mark_high=6000.,mark_low=6000.)
        r=m.simulate(rows,fs,(1,0,1),START,START+6*m.DAY)
        for b in r['books']:
            self.assertIsNotNone(b['stopped']);self.assertEqual(b['quantity'],0)
            self.assertEqual(sum(e['kind']=='entry_request' for e in b['events']),1)
            self.assertEqual(b['stopped']['t'],START+3*m.STEP)
    def test_initial_stop_cannot_restart(self):
        r=self.run_case(initially_stopped=True)
        self.assertEqual(r['books'][0]['net'],0);self.assertEqual(r['books'][0]['cycles'],[])
        self.assertFalse(m.eligible(r))
    def test_no_intrabar_favorable_peak_assumption(self):
        b=m.Book();b.q=.25;b.cash=7500.;b.leg_cash=-2500.;b.entry_collateral=2500.
        x=row(START);x['mark_high']=20000.
        b.observe(x)
        self.assertLess(b.dd,.001)
    def test_passive_does_not_exit_after_72h(self):
        b=self.run_case(passive=True)['books'][0]
        self.assertGreater(b['cycles'][0]['end'],START+5*m.DAY)
    def test_no_same_day_exit_reentry(self):
        b=self.run_case(days=10)['books'][0]
        entries=[e['t'] for e in b['events'] if e['kind']=='entry_request']
        self.assertEqual(entries,[START,START+4*m.DAY])
    def test_selection_negative_allowed_and_tie_order(self):
        r=self.run_case();s=copy.deepcopy(r);s['parameters']=[-1,0,1]
        self.assertEqual(m.select([r,s]),1)
        s['books'][0]['stopped']={'reason':'risk'}
        self.assertEqual(m.select([r,s]),0)
    def test_features_are_strictly_lagged(self):
        rows=[row(t,10000+((t//m.DAY)%17)*100) for t in range(START-8*m.DAY,START+2*m.DAY,m.STEP)]
        ratios={(m.day(START)-dt.timedelta(days=i)).isoformat():str(.05+i*.001) for i in range(-3,40)}
        f=m.features(rows,ratios,START,START+2*m.DAY)
        changed=copy.deepcopy(rows)
        for x in changed:
            if x['timestamp']>=START:x['price']=90000.
        rr=dict(ratios);rr[m.day(START).isoformat()]='.9'
        g=m.features(changed,rr,START,START+2*m.DAY)
        self.assertEqual(f[START],g[START])
        self.assertEqual(f[START]['source_day'],'2025-01-10')
    def test_missing_causal_input_rejected(self):
        rows,fs=fixture()
        with self.assertRaises((KeyError,ValueError)):m.features(rows,{},START,START+m.DAY)
    def test_no_constant_return_sharpe(self):
        b=self.run_case()['books'][0];b['stopped']={'reason':'test'}
        self.assertEqual(m.sharpe(b),dict(ordinary=None,hac7=None))

if __name__=='__main__':unittest.main()
