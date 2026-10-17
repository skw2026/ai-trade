"""Additional synthetic verification; frozen market runner and outputs unchanged."""
import unittest
import dated_basis_assumption_screen as engine
import audit_dated_basis_assumption as audit
from test_dated_basis_assumption_screen import quarter


class PolicyWalkTests(unittest.TestCase):
    def test_positive_synthetic_portfolio_inherits_capital(self):
        first=quarter()
        bundle={'one':first,'two':quarter(first['expiry']+90*engine.DAY)}
        result=engine.sweep(bundle)
        self.assertEqual(result['status'],'ASSUMPTION_ROBUST_DEVELOPMENT')
        self.assertEqual(audit.audit(bundle,result)['ledgers'],216)
        for path in result['portfolios']:
            self.assertEqual(path['paths'][1]['ledger']['capital'],path['paths'][0]['ledger']['end_capital'])

    def test_unknown_risk_prevents_robust_claim(self):
        raw=quarter();raw['series']['future'][raw['expiry']-600000]['n']=0
        result=engine.sweep({'one':raw})
        self.assertEqual(result['status'],'CONDITIONAL_ONLY')
        audit.audit({'one':raw},result)

    def test_stop_survives_to_next_quarter(self):
        raw=quarter();t=raw['expiry']-7*engine.DAY+300000
        raw['series']['mark'][t]['h']='200'
        bundle={'one':raw,'two':quarter(raw['expiry']+90*engine.DAY)}
        result=engine.sweep(bundle)
        self.assertEqual(result['status'],'NO_GO_EVEN_OPTIMISTIC')
        self.assertTrue(all(p['net']==0 for p in result['portfolios']
                            if p['scenario']['cost']=='zero_friction'))
        audit.audit(bundle,result)
        self.assertTrue(all(p['paths'][1]['status']=='HALTED' for p in result['portfolios']))

    def test_decision_tamper_cannot_create_trade(self):
        bundle={'one':quarter(future='100')};result=engine.sweep(bundle)
        result['decisions']['one']['enter']=True
        with self.assertRaises(AssertionError):audit.audit(bundle,result)


if __name__=='__main__':unittest.main()
