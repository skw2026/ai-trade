import copy
import datetime as dt
from decimal import Decimal
import unittest
import dated_basis_term_inputs as data
import dated_basis_term_screen as model
import audit_dated_basis_term as audit
from test_dated_basis_assumption_inputs import InputTests as OldFixture


def quarter(days=90, expiry=1695974400000, future='110'):
    row=dict(o='100',h='100',l='100',c='100',v='100',n=2,complete=True)
    times=range(expiry-days*model.DAY-model.BAR,expiry+3900000,model.BAR)
    spot={t:row.copy() for t in times}
    futures={t:dict(row,o=future,h=future,l=future,c=future) for t in times}
    return dict(expiry=expiry,settlement='100',series=dict(spot=spot,future=futures,mark=copy.deepcopy(futures)))


class TermInputTests(unittest.TestCase):
    def test_request_union_and_deduplication(self):
        requests=data.requests()
        self.assertEqual(len(requests),41)
        self.assertEqual(len({r['url'] for r in requests}),41)
        self.assertEqual(sum(r['kind']=='spot' for r in requests),13)

    def test_earliest_date_and_fixed_targets(self):
        self.assertEqual(min(r['month'] for r in data.requests()),'2022-12')
        self.assertEqual({r['symbol'] for r in data.requests() if r['kind']!='spot'},set(data.TARGETS))
        self.assertTrue(all('BTCUSDT' in r['filename'] for r in data.requests() if r['kind']=='spot'))

    def test_old_archive_same_bytes_new_scope(self):
        raw,checksum,old=OldFixture().archive()
        request=next(r for r in data.requests() if r['url']==old['url'])
        self.assertEqual(data.parse_archive(raw,checksum,request)[1677628800000]['c'],'100')

    def test_external_url_rejected(self):
        raw,checksum,old=OldFixture().archive()
        with self.assertRaises(ValueError):data.parse_archive(raw,checksum,dict(old,url='https://other.invalid'))

    def test_missing_critical_never_becomes_zero(self):
        q=quarter()
        t=q['expiry']-60*model.DAY-model.BAR
        del q['series']['spot'][t]
        report=data.quality(q['series'],q['expiry'],60)
        self.assertFalse(report['critical_complete'])
        self.assertEqual(report['critical_missing']['spot'],[t])

    def test_coverage_uses_full_horizon(self):
        q=quarter();t=q['expiry']-50*model.DAY
        q['series']['future'][t]['n']=0
        self.assertTrue(data.quality(q['series'],q['expiry'],30)['risk_path_observed'])
        self.assertFalse(data.quality(q['series'],q['expiry'],60)['risk_path_observed'])

    def test_horizon_scope_cannot_expand(self):
        q=quarter()
        for days in (7,31,120,True):
            with self.subTest(days=days),self.assertRaises(ValueError):data.quality(q['series'],q['expiry'],days)


class TermScreenTests(unittest.TestCase):
    def test_horizon_clock_and_capital_charge(self):
        q=model.normalized(quarter())
        expected=[]
        for days in model.HORIZONS:
            mask=model.decision(q,days)
            self.assertEqual(mask['decision_bar'],q['expiry']-days*model.DAY-model.BAR)
            expected.append(mask['expected_net'])
        self.assertGreater(expected[0],expected[1]);self.assertGreater(expected[1],expected[2])

    def test_decision_never_uses_future_settlement(self):
        q=model.normalized(quarter());before=model.decision(q,60)
        q['settlement']=1000;q['series']['spot'][q['expiry']]['o']=50
        self.assertEqual(before,model.decision(q,60))

    def test_fixed_horizon_not_parameter_search(self):
        for days in (7,31,120,True):
            with self.subTest(days=days),self.assertRaises(ValueError):model.sweep({},days)

    def test_independent_cash_and_risk_at_each_horizon(self):
        q=quarter();scenario=next(x for x in model.scenarios() if x['id']=='base_h8_b0_m0')
        for days in model.HORIZONS:
            result=model.quarter_path(model.normalized(q),scenario,days=days)
            expected=audit.reference(audit.prep(q),scenario,Decimal(10000),None,days)
            self.assertEqual(result['held_ms'],days*model.DAY-model.BAR)
            for k in ('net','gross','fees','opportunity_cost'):audit.close(result[k],expected[k],k)

    def test_unknown_risk_is_not_certified(self):
        q=quarter();del q['series']['mark'][q['expiry']-40*model.DAY]
        r=model.quarter_path(model.normalized(q),model.scenarios()[0],days=60)
        self.assertEqual(r['risk_status'],'UNKNOWN')

    def test_full_policy_inherits_capital_and_audits(self):
        q=quarter(days=30)
        bundle={'one':q,'two':quarter(days=30,expiry=q['expiry']+92*model.DAY)}
        r=model.sweep(bundle,30)
        self.assertEqual(audit.audit(bundle,r,30)['ledgers'],216)
        self.assertTrue(model.development_check(r)['supported'])
        for p in r['portfolios']:
            self.assertEqual(p['paths'][1]['ledger']['capital'],p['paths'][0]['ledger']['end_capital'])

    def test_single_quarter_cannot_pass_remove_best(self):
        paths=[dict(ledger=dict(net=100.0))]
        portfolios=[dict(scenario=s,net=100,halted=False,paths=paths) for s in model.scenarios()]
        result=model.development_check(dict(portfolios=portfolios))
        self.assertEqual(result['base_without_best'],0);self.assertFalse(result['supported'])

    def test_hard_stop_is_permanent_across_quarters(self):
        q=quarter(days=30);t=q['expiry']-30*model.DAY+model.BAR
        q['series']['mark'][t]['h']='200'
        bundle={'one':q,'two':quarter(days=30,expiry=q['expiry']+92*model.DAY)}
        r=model.sweep(bundle,30);audit.audit(bundle,r,30)
        self.assertTrue(all(p['halted'] and p['paths'][1]['status']=='HALTED' for p in r['portfolios']))

    def test_tampering_horizon_or_money_rejected(self):
        q=quarter(days=30,future='100');bundle={'one':q};r=model.sweep(bundle,30)
        with self.assertRaises(AssertionError):audit.audit(bundle,r,60)
        r['diagnostics']['one'][0]['net']+=1
        with self.assertRaises(AssertionError):audit.audit(bundle,r,30)

    def test_no_trade_is_not_support(self):
        r=dict(portfolios=[dict(scenario=s,net=0,halted=False,paths=[]) for s in model.scenarios()])
        self.assertFalse(model.development_check(r)['supported'])


if __name__=='__main__':unittest.main()
