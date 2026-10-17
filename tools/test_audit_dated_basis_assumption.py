import copy
import unittest
from decimal import localcontext
import audit_dated_basis_assumption as audit
import dated_basis_assumption_screen as engine
from test_dated_basis_assumption_screen import quarter, scenario


class AuditTests(unittest.TestCase):
    def test_independent_all_cost_and_clock_cases(self):
        raw=quarter()
        with localcontext() as ctx:
            ctx.prec=40
            for cost in ('zero_friction','base','stress'):
                for hour in (0,8):
                    result=engine.quarter_path(engine.normalized(raw),scenario(cost,hour,bias=25,delay=60))
                    audit.check_ledger(audit.prep(raw),result)

    def test_money_tampering_rejected(self):
        raw=quarter();result=engine.quarter_path(engine.normalized(raw),scenario())
        for key in ('net','fees','btc_after_exit','spot_spend','opportunity_cost'):
            changed=copy.deepcopy(result);changed[key]+=1
            with self.subTest(key=key),self.assertRaises(AssertionError):
                audit.check_ledger(audit.prep(raw),changed)

    def test_risk_and_clock_tampering_rejected(self):
        raw=quarter();result=engine.quarter_path(engine.normalized(raw),scenario())
        for key,value in (('exit_ms',result['exit_ms']+300000),('risk_status','UNKNOWN'),('checked_bars',1)):
            changed=copy.deepcopy(result);changed[key]=value
            with self.subTest(key=key),self.assertRaises(AssertionError):
                audit.check_ledger(audit.prep(raw),changed)

    def test_real_stop_recomputed(self):
        raw=quarter();t=raw['expiry']-7*engine.DAY+300000
        raw['series']['mark'][t]['h']='200'
        result=engine.quarter_path(engine.normalized(raw),scenario())
        audit.check_ledger(audit.prep(raw),result)
        result['risk_reason']=None
        with self.assertRaises(AssertionError):audit.check_ledger(audit.prep(raw),result)

    def test_full_no_entry_policy(self):
        bundle={'synthetic':quarter(future='100')}
        result=engine.sweep(bundle)
        report=audit.audit(bundle,result)
        self.assertEqual(report['verdict'],'NO_GO_NO_TRADES')
        self.assertEqual(report['ledgers'],54)


if __name__=='__main__':unittest.main()
