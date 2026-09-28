import copy
import unittest

import attention_capacity as a
import screen_attention as s


class EconomicTests(unittest.TestCase):
    def case(self, side=1):
        c = copy.deepcopy(a.config())
        c['cost'].update(fee_bps_per_side=0,slippage_bps_per_side=0)
        c['economy']['allocation'] = 0.25
        signal = dict(entry=0,exit=a.DAY,side=side,control=-side,cell=f'{side}_{-side}')
        trade = {0:['100','100','100','100'],a.DAY:['100','100','100','100']}
        mark = {t:['100','100','100','100'] for t in range(0,a.DAY,a.HOUR)}
        return c,[signal],trade,mark,{}

    def test_flat_roundtrip_cost(self):
        for side in (-1,1):
            got = s.unit(side,100,100,[],0.0006,0.0005)
            ep=100*(1+side*0.0005)
            xp=100*(1-side*0.0005)
            self.assertAlmostEqual(got['net'],(side*(xp-ep)-.0006*(ep+xp))/ep)
            self.assertLess(got['net'],0)

    def test_long_positive_funding_pays(self):
        self.assertAlmostEqual(s.unit(1,100,110,[(100,.01)],0,0)['net'],.09)

    def test_short_positive_funding_receives(self):
        self.assertAlmostEqual(s.unit(-1,100,90,[(100,.01)],0,0)['net'],.11)

    def test_negative_funding_signs(self):
        self.assertAlmostEqual(s.unit(1,100,100,[(100,-.01)],0,0)['net'],.01)
        self.assertAlmostEqual(s.unit(-1,100,100,[(100,-.01)],0,0)['net'],-.01)

    def test_flat_cash(self):
        c,sig,tr,m,f = self.case()
        got = s.path(sig,tr,m,f,c)
        self.assertEqual(got['completed_positions'],1)
        self.assertEqual(got['last_reference_nav'],1)
        self.assertIsNone(got['stop'])

    def test_entry_funding_in_exit_funding_out(self):
        c,sig,tr,m,f = self.case()
        f.update({0:'.01',a.DAY:'1000'})
        got = s.path(sig,tr,m,f,c)
        self.assertAlmostEqual(got['last_reference_nav'],.9975)
        self.assertAlmostEqual(got['rows'][0]['funding'],.01)

    def test_short_cash_independent(self):
        c,sig,tr,m,f = self.case(-1)
        c['cost'].update(fee_bps_per_side=6,slippage_bps_per_side=5)
        tr[a.DAY]=['99','99','99','99']
        f[8*a.HOUR]='-.0002'
        got=s.path(sig,tr,m,f,c)
        audit=s.independent_cash(sig,tr,m,f,c)
        self.assertAlmostEqual(got['last_reference_nav'],audit['completed'][0],places=13)

    def test_definite_long_risk_stops_before_exit_read(self):
        c,sig,tr,m,f = self.case()
        m[0]=['100','101','60','60']
        del tr[a.DAY]
        got=s.path(sig,tr,m,f,c)
        audit=s.independent_cash(sig,tr,m,f,c)
        self.assertEqual(got['stop']['status'],'REJECT')
        self.assertEqual(audit['status'],'REJECT')
        self.assertEqual(got['completed_positions'],0)
        self.assertEqual(audit['at'],0)

    def test_definite_short_risk(self):
        c,sig,tr,m,f = self.case(-1)
        m[0]=['100','140','100','140']
        got=s.path(sig,tr,m,f,c)
        self.assertEqual(got['stop']['status'],'REJECT')

    def test_ambiguous_ohlc(self):
        c,sig,tr,m,f = self.case()
        m[0]=['100','120','85','120']
        got=s.path(sig,tr,m,f,c)
        audit=s.independent_cash(sig,tr,m,f,c)
        self.assertEqual(got['stop']['status'],'INSUFFICIENT_RISK_ORDER')
        self.assertLess(got['stop']['lower_drawdown'],.08)
        self.assertGreater(got['stop']['upper_drawdown'],.08)
        self.assertEqual(audit['status'],'INSUFFICIENT_RISK_ORDER')

    def test_high_precedes_close_known(self):
        c,sig,tr,m,f = self.case()
        m[0]=['100','120','85','85']
        got=s.path(sig,tr,m,f,c)
        self.assertEqual(got['stop']['status'],'REJECT')

    def test_prior_peak_before_next_low(self):
        c,sig,tr,m,f = self.case()
        m[0]=['100','120','100','120']
        m[a.HOUR]=['120','120','85','120']
        got=s.path(sig,tr,m,f,c)
        self.assertEqual(got['stop']['status'],'REJECT')
        self.assertEqual(got['stop']['at'],a.HOUR)

    def test_funding_risk(self):
        c,sig,tr,m,f = self.case()
        f[0]='.4'
        got=s.path(sig,tr,m,f,c)
        self.assertEqual(got['stop']['phase'],'funding')
        self.assertEqual(got['stop']['status'],'REJECT')

    def test_exit_fee_risk(self):
        c,sig,tr,m,f = self.case()
        tr[a.DAY]=['60','60','60','60']
        got=s.path(sig,tr,m,f,c)
        audit=s.independent_cash(sig,tr,m,f,c)
        self.assertEqual(got['stop']['phase'],'exit')
        self.assertEqual(audit['at'],a.DAY)

    def test_reinvestment(self):
        c,sig,tr,m,f = self.case()
        tr[a.DAY]=['110','110','110','110']
        sig.append(dict(sig[0],entry=a.DAY,exit=2*a.DAY))
        tr[2*a.DAY]=['121','121','121','121']
        m.update({t:['110','110','110','110'] for t in range(a.DAY,2*a.DAY,a.HOUR)})
        got=s.path(sig,tr,m,f,c)
        self.assertAlmostEqual(got['last_reference_nav'],1.025**2)
        other=s.independent_cash(sig,tr,m,f,c)
        self.assertAlmostEqual(other['completed'][-1],got['last_reference_nav'])

    def test_independent_paired_bootstrap(self):
        c = copy.deepcopy(a.config())
        c['economy']['bootstrap'].update(replicates=30,block_days=3)
        rows=[dict(entry=a.ms(f'{2022+i//7}-01-01T12:00:00Z'),net=(i%3-1)/100,
                   price_control_net=(i%5-2)/120,long_control_net=.001) for i in range(28)]
        x,y=s.statistics(rows,c),s.statistics(rows,c,True)
        for u,v in zip(x['lower_bounds'],y['lower_bounds']):
            self.assertAlmostEqual(u,v,places=13)


if __name__ == '__main__':
    unittest.main()
