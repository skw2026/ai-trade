import copy
import unittest
import audit_btc_old_output as audit
import btc_old_output_model as m
from test_btc_old_output_model import fixture,START

class AuditTests(unittest.TestCase):
    def case(self):
        rows,fs=fixture();rows[3]['funding_rate_per_interval']=.0001
        return rows,m.simulate(rows,fs,(1,0,1),START,START+6*m.DAY)
    def test_independent_reconstruction(self):
        rows,r=self.case();self.assertEqual(audit.audit(r,rows)['status'],'DECIMAL_LEDGER_AND_RISK_VERIFIED')
    def test_cash_tamper(self):
        rows,r=self.case();r['books'][0]['cash']+=1
        with self.assertRaisesRegex(ValueError,'CASH'):audit.audit(r,rows)
    def test_omitted_funding(self):
        rows,r=self.case();r['books'][0]['events']=[x for x in r['books'][0]['events'] if x['kind']!='funding']
        with self.assertRaisesRegex(ValueError,'FUNDING_EVENT'):audit.audit(r,rows)
    def test_price_tamper(self):
        rows,r=self.case();next(x for x in r['books'][0]['events'] if x['kind']=='fill')['price']+=.1
        with self.assertRaisesRegex(ValueError,'PRICE'):audit.audit(r,rows)
    def test_stop_tamper(self):
        rows,r=self.case();r['books'][0]['stopped']={'t':START,'reason':'invented'}
        with self.assertRaisesRegex(ValueError,'STOP_PRESENCE'):audit.audit(r,rows)
    def test_day_tamper(self):
        rows,r=self.case();r['books'][0]['days'][0]['equity']+=1
        with self.assertRaisesRegex(ValueError,'DAILY_EQUITY'):audit.audit(r,rows)
    def test_partial_and_stopped_paths(self):
        for mode in ('partial','risk','retained','passive','delay'):
            rows,fs=fixture()
            if mode=='partial':rows[2]['volume']=1
            if mode=='risk':
                for x in rows:
                    if x['timestamp']>=START+2*m.STEP:
                        for k in ('open','high','low','price','mark_open','mark_high','mark_low','mark_close'):x[k]=6000.
            r=m.simulate(rows,fs,(1,0,1),START,START+6*m.DAY,
                initially_stopped=mode=='retained',passive=mode=='passive',latency=2 if mode=='delay' else 1)
            with self.subTest(mode=mode):self.assertEqual(audit.audit(r,rows)['status'],'DECIMAL_LEDGER_AND_RISK_VERIFIED')
    def test_short_funding_and_cash(self):
        rows,fs=fixture();rows[3]['funding_rate_per_interval']=.001
        r=m.simulate(rows,fs,(-1,0,1),START,START+6*m.DAY)
        self.assertEqual(r['books'][0]['funding'],-2.5)
        self.assertEqual(audit.audit(r,rows)['status'],'DECIMAL_LEDGER_AND_RISK_VERIFIED')
    def test_unfinished_liability_not_forgiven(self):
        rows,fs=fixture()
        for r in rows:
            if r['timestamp']>=START+3*m.DAY:r['volume']=0.
        r=m.simulate(rows,fs,(1,0,1),START,START+6*m.DAY)
        self.assertIsNotNone(r['books'][0]['insufficient'])
        self.assertEqual(audit.audit(r,rows)['status'],'DECIMAL_LEDGER_AND_RISK_VERIFIED')

if __name__=='__main__':unittest.main()
