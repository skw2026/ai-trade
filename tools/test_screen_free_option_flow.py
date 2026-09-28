#!/usr/bin/env python3
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import free_option_flow_data as f
import screen_free_option_flow as s

DAY='2023-01-01'


def synthetic():
    start=f.ms(DAY+'T00:00:00Z')
    bars={t:['100','100','100','100'] for t in range(start,start+f.DAY,f.STEP)}
    bars[f.ms(DAY+'T07:30:00Z')]=['101','101','101','101']
    return dict(option=dict(quality=True,calls=[dict(side='sell',amount='1')]),
                trade=copy.deepcopy(bars),mark=copy.deepcopy(bars),funding={},incident_overlap=[])


class ScreenTests(unittest.TestCase):
    def test_positive_flow_negative_price(self):
        r=s.signal(synthetic(),DAY)
        self.assertTrue(r['A']);self.assertFalse(r['B']);self.assertFalse(r['C'])

    def test_price_only_control(self):
        d=synthetic();d['option']['calls'][0]['side']='buy'
        r=s.signal(d,DAY)
        self.assertFalse(r['A']);self.assertTrue(r['B']);self.assertTrue(r['C'])

    def test_exact_zero_pressure(self):
        d=synthetic();d['option']['calls']=[dict(side='buy',amount='0.3'),dict(side='sell',amount='0.1'),dict(side='sell',amount='0.2')]
        r=s.signal(d,DAY)
        self.assertEqual(r['pressure'],0);self.assertTrue(r['B'])

    def test_zero_call_and_incident_not_zero_signal(self):
        d=synthetic();d['option']['calls']=[]
        self.assertFalse(s.signal(d,DAY)['valid'])
        d=synthetic();d['incident_overlap']=[1]
        self.assertFalse(s.signal(d,DAY)['valid'])

    def test_quality_excluded(self):
        d=synthetic();d['option']['quality']=False
        self.assertFalse(s.signal(d,DAY)['valid'])

    def test_reversed_incident_not_silently_fixed(self):
        meta=dict(incidentReports=[dict(**{'from':'2024-03-05T00:00:00Z','to':'2023-03-05T07:22:00Z'})])
        self.assertEqual(s.incidents(meta,'2023-06-01'),([0],[0]))
        self.assertEqual(s.incidents(meta,'2024-04-01'),([],[0]))

    def test_costs(self):
        r=s.unit_return(synthetic(),DAY)
        self.assertEqual(r['gross_mid'],0)
        self.assertLess(r['net'],-.00219)
        self.assertGreater(r['net'],-.00221)

    def test_funding_entry_in_exit_out(self):
        d=synthetic();t=f.ms(DAY+'T08:05:00Z');end=f.ms(DAY+'T09:05:00Z')
        d['funding']={t:'0.01',end:'0.02'}
        r=s.unit_return(d,DAY)
        self.assertAlmostEqual(r['funding_paid'],.01/1.0005)

    def test_reference_compounding(self):
        d=synthetic();state=dict(nav=1.,peak=1.,dd_lo=0.,dd_hi=0.)
        self.assertIsNone(s.risk_trade(d,DAY,state))
        self.assertAlmostEqual(state['nav'],1+.25*s.unit_return(d,DAY)['net'])
        self.assertIsNone(s.risk_trade(d,DAY,state))
        self.assertAlmostEqual(state['nav'],(1+.25*s.unit_return(d,DAY)['net'])**2)

    def test_definite_risk_stop(self):
        d=synthetic();t=f.ms(DAY+'T08:05:00Z');d['mark'][t]=['100','100','60','100']
        state=dict(nav=1.,peak=1.,dd_lo=0.,dd_hi=0.)
        stop=s.risk_trade(d,DAY,state)
        self.assertEqual(stop['reason'],'DEFINITE_REFERENCE_RISK_BREACH')
        self.assertEqual(state['nav'],1.)

    def test_order_uncertainty_stops(self):
        d=synthetic();t=f.ms(DAY+'T08:05:00Z');d['mark'][t]=['100','130','90','100']
        state=dict(nav=1.,peak=1.,dd_lo=0.,dd_hi=0.)
        stop=s.risk_trade(d,DAY,state)
        self.assertEqual(stop['reason'],'INSUFFICIENT_RISK_ORDER')

    def rows(self,a=.04):
        return [dict(valid=True,A=i%3==0,B=i%3==1,C=i%3!=0,
                     net=a if i%3==0 else .001 if i%3==1 else .004) for i in range(45)]

    def test_sample_floor_no_bootstrap(self):
        rows=self.rows()[:21]
        r=s.statistics(rows)
        self.assertEqual(r['reason'],'INSUFFICIENT_SAMPLE');self.assertFalse(r['bootstrap'])
        self.assertEqual(s.audit_statistics(rows,r),'PASS_SAMPLE_FLOOR')

    def test_fixed_bootstrap_and_independent_audit(self):
        rows=self.rows();r=s.statistics(rows)
        self.assertEqual(r['decision'],'WORTH_FURTHER_REVIEW')
        self.assertEqual(s.audit_statistics(rows,r),'PASS_INDEPENDENT_PREFIX_BOOTSTRAP')
        r['bootstrap'][0]['bounds'][0]['low']+=.01
        with self.assertRaisesRegex(ValueError,'AUDIT_QUANTILE'):
            s.audit_statistics(rows,r)

    def test_negative_net_rejected(self):
        r=s.statistics(self.rows(-.01))
        self.assertEqual(r['decision'],'REJECT')

    def test_metadata_gzip_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'pages').mkdir();p=root/'raw.gz'
            obj=dict(id='deribit',availableSince='2019-03-30',incidentReports=[],
                     datasets=dict(exportedFrom='2019-03-30',exportedUntil='2026-09-28'))
            with gzip.open(p,'wb') as g:g.write(json.dumps(obj).encode())
            (root/'pages/metadata.json').write_text(json.dumps(dict(headers={'Content-Encoding':'gzip'})))
            with patch.object(f,'RUN',root):
                self.assertEqual(f.metadata(p)['id'],'deribit')


if __name__=='__main__':
    unittest.main()
