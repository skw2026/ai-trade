"""Synthetic tests only; no historical run or confirmation consumption."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import offline_diagnostic_safety as policy
import offline_policy_correction as old
import audit_offline_diagnostic_safety as auditor
from test_offline_policy_correction import sample
from run_bounded_learning import sha, save


class DiagnosticTests(unittest.TestCase):
    def audit(self, samples, vector=(1,0)):
        records = []
        result = policy.replay(samples,vector,records.append)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'trace.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in records))
            summary = dict(result,trace_file=path.name,trace_sha256=sha(path))
            checked = auditor.audit_trace(path,samples,summary)
        return result, checked, records

    def test_short_loss_is_diagnostic_but_legacy_rejection_is_permanent(self):
        rows = [sample(i,(.002*(-1)**(i//120),0)) for i in range(960)]
        result, audit, records = self.audit(rows)
        self.assertEqual(result['reason'],'COMPLETE')
        self.assertEqual(result['active_bars'],960)
        self.assertEqual(result['first_legacy_withdrawal_ms'],int(rows[480]['row']['timestamp']))
        self.assertEqual(result['legacy_path_status'],'REJECT_LEGACY_SAFETY')
        self.assertEqual(result['diagnostic_triggered_windows'],3)
        self.assertFalse(result['qualification'])
        self.assertLess(audit['economics'][0]['net'],0)
        self.assertFalse(any(r.get('safety',{}).get('withdrawn',False) for r in records if r.get('safety')))

    def test_old_path_is_unchanged_and_prefix_economics_identical(self):
        rows = [sample(i,(.002*(-1)**(i//120),0)) for i in range(700)]
        prior_events = []
        prior = old.replay(rows,(1,0),prior_events.append)
        result, _, events = self.audit(rows)
        self.assertEqual(prior['reason'],'SAFETY_WITHDRAWAL')
        self.assertEqual(prior['active_bars'],480)
        self.assertEqual(result['active_bars'],700)
        for a,b in zip(prior_events[:480],events[:480]):
            for key in ('target','receipts','wallets','timestamp'):
                self.assertEqual(a[key],b[key])
        self.assertEqual(prior['stop_at_ms'],result['first_legacy_withdrawal_ms'])

    def test_loss_counters_reset_on_nonloss_but_diagnostic_history_remains(self):
        diagnostic = policy.LossDiagnostic()
        results = []
        for receipt in (-1,-1,0,1,-1):
            for _ in range(240): diagnostic.observe(receipt,1)
            results.append(diagnostic.assess())
        self.assertEqual([r['would_withdraw'] for r in results],[False,True,False,False,False])
        self.assertEqual(diagnostic.triggered_windows,1)
        with self.assertRaisesRegex(ValueError,'WINDOW'): diagnostic.assess()
        with self.assertRaisesRegex(ValueError,'BUCKET'): diagnostic.observe(0,4)

    def test_hard_stop_after_diagnostics_is_not_rearmed(self):
        rows = [sample(i,(.002*(-1)**(i//120),0)) for i in range(1000)]
        rows[600]['row']['mark_high']='20000'
        rows[600]['row']['mark_low']='5000'
        result, _, records = self.audit(rows)
        self.assertEqual(result['reason'],'REFERENCE_RISK_STOP')
        self.assertEqual(result['active_bars'],601)
        self.assertEqual(result['exit_at_ms'],int(rows[601]['row']['timestamp']))
        self.assertEqual(result['legacy_path_status'],'REJECT_LEGACY_SAFETY')
        self.assertEqual(sum(r['kind']=='EXIT_OPEN' for r in records),1)
        self.assertEqual(result['wallets'][0]['qty'],0)

    def test_stress_book_alone_can_stop_both_paths(self):
        rows = [sample(i,(.002*(-1)**i,0)) for i in range(300)]
        result, _, _ = self.audit(rows)
        self.assertEqual(result['reason'],'REFERENCE_RISK_STOP')
        self.assertLess(result['wallets'][0]['max_drawdown'],.08)
        self.assertGreaterEqual(result['wallets'][1]['max_drawdown'],.08)
        self.assertEqual(result['legacy_path_status'],'REJECT_REFERENCE_RISK')

    def test_funding_stop_charges_old_holder_and_keeps_loss(self):
        rows = [sample(0),sample(1,(-.002,0),rate=1),sample(2),sample(3)]
        result, audit, _ = self.audit(rows)
        self.assertEqual(result['reason'],'REFERENCE_RISK_STOP')
        self.assertEqual(audit['economics'][0]['funding'],2500)
        self.assertLess(audit['economics'][0]['net'],-2500)
        self.assertEqual(audit['economics'][0]['fills'],2)

    def test_gap_loss_is_not_filled_at_fictitious_intrabar_price(self):
        rows = [sample(0),sample(1,price=5000),sample(2,price=20000)]
        rows[0]['row']['mark_low']='5000'
        result, audit, _ = self.audit(rows)
        self.assertEqual(result['active_bars'],1)
        self.assertLess(audit['economics'][0]['net'],-1250)

    def test_fixed_end_pending_stop_still_settles_once(self):
        rows = [sample(0)]
        rows[0]['row']['mark_low']='5000'
        result, audit, _ = self.audit(rows)
        self.assertEqual(result['reason'],'REFERENCE_RISK_STOP')
        self.assertEqual(audit['economics'][0]['fills'],2)
        self.assertEqual(result['exit_at_ms'],int(rows[0]['row']['timestamp'])+300000)

    def test_positive_week_and_partial_with_costs_reconcile(self):
        for n in (2016,2020):
            rows = [sample(i,price=10000+i*.7,closing=10000+(i+1)*.7) for i in range(n)]
            result, audit, _ = self.audit(rows)
            self.assertEqual(result['reason'],'COMPLETE')
            self.assertGreater(audit['economics'][1]['net'],0)
            self.assertEqual(len(result['weekly']),1)
            self.assertFalse(result['qualification'])

    def test_flat_or_relative_improvement_is_not_development_support(self):
        result, _, _ = self.audit([sample(i,(0,0)) for i in range(100)],(0,0))
        fixed = copy.deepcopy(result)
        for wallet in fixed['wallets']: wallet['cash']=9990
        self.assertEqual(old.verdict(dict(changed=True,winner=result),fixed,result)[0],
                         'NO_GO_NO_ABSOLUTE_LEARNING_EDGE')

    def test_positive_diagnostic_development_does_not_grant_legacy_qualification(self):
        fixed = dict(reason='COMPLETE',wallets=[dict(cash=10010)]*2,weekly=[[1,1]]*26)
        better = dict(reason='COMPLETE',wallets=[dict(cash=10020)]*2,weekly=[[2,2]]*26,
                      legacy_path_status='REJECT_LEGACY_SAFETY',qualification=False)
        self.assertEqual(old.verdict(dict(changed=True,winner=better),fixed,better)[0],
                         'DEVELOPMENT_SUPPORTED_CONFIRMATION_UNAVAILABLE')
        self.assertFalse(better['qualification'])
        better['reason']='REFERENCE_RISK_STOP'
        self.assertEqual(old.verdict(dict(changed=True,winner=better),fixed,better)[0],
                         'NO_GO_SAFETY_OR_REFERENCE_RISK')

    def test_domain_clock_causality_and_finality(self):
        for name in ('live','confirmation','demo'):
            with self.assertRaisesRegex(ValueError,'DEVELOPMENT_ONLY'): policy.DevelopmentPath((1,0),domain=name)
        with self.assertRaisesRegex(ValueError,'CLOCK'): policy.replay([sample(0),sample(2)],(1,0))
        a, b = policy.DevelopmentPath((1,0)), policy.DevelopmentPath((1,0))
        self.assertEqual(a.consume(sample(0))['target'],b.consume(sample(0,price=5000,closing=7000))['target'])
        a.finish()
        with self.assertRaisesRegex(ValueError,'FINISHED'): a.consume(sample(1))
        with self.assertRaisesRegex(ValueError,'FINISHED'): a.finish()

    def test_diagnostic_tamper_detected_with_rebound_trace_hash(self):
        rows = [sample(i,(.002*(-1)**(i//120),0)) for i in range(700)]
        result, _, records = self.audit(rows)
        records[479]['safety']['would_withdraw']=False
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'trace.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in records))
            summary=dict(result,trace_file=path.name,trace_sha256=sha(path))
            with self.assertRaisesRegex(ValueError,'DIAGNOSTIC_STATE'): auditor.audit_trace(path,rows,summary)

    def test_attempt_and_frozen_model_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ('market-attempt.json','frozen-model.json'):
                path=Path(d)/name
                save(path,{'scope':'DEVELOPMENT_ONLY'})
                with self.assertRaises(FileExistsError): save(path,{'scope':'confirmation'})

    def test_legacy_rejection_cannot_be_erased_with_rebound_summary(self):
        rows = [sample(i,(.002*(-1)**(i//120),0)) for i in range(700)]
        result, _, records = self.audit(rows)
        result['first_legacy_withdrawal_ms']=None
        result['legacy_path_status']='PATH_CONSTRAINTS_ONLY_NOT_QUALIFICATION'
        records[-1]['result']=result
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'trace.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in records))
            summary=dict(result,trace_file=path.name,trace_sha256=sha(path))
            with self.assertRaisesRegex(ValueError,'LEGACY_DIAGNOSTIC_BINDING'):
                auditor.audit_trace(path,rows,summary)

    def test_missing_stress_receipt_cannot_pass_zip_comparison(self):
        rows=[sample(i) for i in range(4)]
        result, _, records=self.audit(rows)
        records[0]['receipts'].pop()
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'trace.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in records))
            summary=dict(result,trace_file=path.name,trace_sha256=sha(path))
            with self.assertRaisesRegex(ValueError,'BOOK_COUNT'): auditor.audit_trace(path,rows,summary)


if __name__ == '__main__':
    unittest.main()
