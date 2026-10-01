"""Synthetic-only guardrails before the one real historical transfer attempt."""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import frozen_history_transfer as p
import audit_frozen_history_transfer as a
import run_frozen_history_transfer as runner
import offline_diagnostic_safety as development
from test_offline_policy_correction import sample
from test_offline_diagnostic_safety import DiagnosticTests
from run_bounded_learning import sha, save


class FrozenTests(unittest.TestCase):
    def audit(self,rows,vector=(1,0)):
        events=[];result=p.replay(rows,vector,events.append)
        with tempfile.TemporaryDirectory() as d:
            trace=Path(d)/'trace.jsonl';trace.write_text(''.join(json.dumps(e)+'\n' for e in events))
            summary=dict(result,trace_file=trace.name,trace_sha256=sha(trace))
            checked=a.audit_trace(trace,rows,summary)
            clusters=p.concentration(events,result)
            a.audit_concentration(trace,result,clusters)
        return result,checked,events

    # Same non-weakened accounting/risk assertions, now on the separate frozen path.
    test_short_loss=DiagnosticTests.test_short_loss_is_diagnostic_but_legacy_rejection_is_permanent
    test_old_prefix=DiagnosticTests.test_old_path_is_unchanged_and_prefix_economics_identical
    test_hard_stop=DiagnosticTests.test_hard_stop_after_diagnostics_is_not_rearmed
    test_stress_stop=DiagnosticTests.test_stress_book_alone_can_stop_both_paths
    test_funding=DiagnosticTests.test_funding_stop_charges_old_holder_and_keeps_loss
    test_gap_loss=DiagnosticTests.test_gap_loss_is_not_filled_at_fictitious_intrabar_price
    test_terminal_stop=DiagnosticTests.test_fixed_end_pending_stop_still_settles_once
    test_week_partial=DiagnosticTests.test_positive_week_and_partial_with_costs_reconcile

    def test_only_two_vectors_and_new_domain(self):
        for vector in ((0,0),(1,1),(0,-3)):
            with self.assertRaisesRegex(ValueError,'FIXED_VECTORS_ONLY'):p.FrozenPath(vector)
        for name in ('live','demo','development','confirmation'):
            with self.assertRaisesRegex(ValueError,'FROZEN_RESEARCH_ONLY'):p.FrozenPath((0,-4),domain=name)
        with self.assertRaisesRegex(ValueError,'DEVELOPMENT_ONLY'):
            development.DevelopmentPath((0,-4),domain='frozen_historical_transfer')

    def test_learned_path_same_frozen_economics_as_development_implementation(self):
        rows=[sample(i,(0,.002*(-1)**(i//120))) for i in range(720)]
        result,_,events=self.audit(rows,(0,-4));old_events=[]
        old=development.replay(rows,(0,-4),old_events.append)
        for field in ('wallets','weekly','partial_week','active_bars','reason','first_legacy_withdrawal_ms'):
            self.assertEqual(result[field],old[field])
        self.assertEqual(result['safety_mode'],p.MODE)
        for x,y in zip(events[:-1],old_events[:-1]):
            for field in ('target','wallets','timestamp','receipts'):self.assertEqual(x[field],y[field])

    def test_clock_causality_finality(self):
        with self.assertRaisesRegex(ValueError,'CLOCK'):p.replay([sample(0),sample(2)],(1,0))
        x,y=p.FrozenPath((0,-4)),p.FrozenPath((0,-4))
        self.assertEqual(x.consume(sample(0,(0,.002)))['target'],
                         y.consume(sample(0,(0,.002),price=5000,closing=7000))['target'])
        x.finish()
        with self.assertRaisesRegex(ValueError,'FINISHED'):x.consume(sample(1))
        with self.assertRaisesRegex(ValueError,'FINISHED'):x.finish()

    def test_direct_reversals_are_one_episode(self):
        result,_,events=self.audit([sample(i,(0,.002*(-1)**i)) for i in range(6)],(0,-4))
        clusters=p.concentration(events,result)
        self.assertEqual(clusters['episode_count'],1)
        self.assertEqual(clusters['episodes'][0]['bars'],6)
        self.assertEqual(clusters['episodes'][0]['fills'][0],7)

    def test_flat_cash_is_zero_and_empty_clusters(self):
        result,_,events=self.audit([sample(i,(0,0)) for i in range(50)],(0,-4))
        self.assertEqual(result['objective'],0)
        self.assertEqual(p.concentration(events,result)['separated_clusters'],0)

    def test_seven_day_gap_boundary(self):
        for gap,count in ((p.GAP-1,1),(p.GAP,2)):
            books=lambda qty,cash,fills:[dict(qty=qty,cash=cash,fills=fills)]*2
            summary=dict(wallets=books(0,10002,4),exit_at_ms=gap+21,
                exposure_short_flat_long=[0,0,2],weekly=[])
            events=[dict(kind='BAR',timestamp=0,wallets=books(1,10000,1)),
                dict(kind='BAR',timestamp=10,wallets=books(0,10001,2)),
                dict(kind='BAR',timestamp=10+gap,wallets=books(1,10001,3)),
                dict(kind='TERMINAL',result=summary)]
            self.assertEqual(p.concentration(events,summary)['separated_clusters'],count)

    def test_tampered_accounting_diagnostic_and_qualification_rejected(self):
        rows=[sample(i,(.002*(-1)**(i//120),0)) for i in range(700)]
        result,_,events=self.audit(rows)
        for change in ('cash','diagnostic','qualification','missing_stress'):
            records=copy.deepcopy(events);summary=copy.deepcopy(result)
            if change=='cash':records[0]['wallets'][0]['cash']+=1
            elif change=='diagnostic':records[479]['safety']['would_withdraw']=False
            elif change=='missing_stress':records[0]['receipts'].pop()
            else:summary['qualification']=True;records[-1]['result']=summary
            with tempfile.TemporaryDirectory() as d:
                path=Path(d)/'trace.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in records))
                with self.assertRaises(ValueError):a.audit_trace(path,rows,dict(summary,trace_sha256=sha(path)))

    def test_attempt_exclusive_before_feature_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d);(path/'replay.csv').write_bytes(b'invented')
            save(path/'input-proof.json',dict(complete=True,synthetic=False,csv_sha256=sha(path/'replay.csv')))
            save(path/'freeze.json',{})
            save(path/'economic-attempt.json',dict(consumed=True))
            with mock.patch.multiple(runner,RUN=path),mock.patch.object(runner,'check',return_value=({'model_sha256':'x'},{})),\
                 mock.patch.object(runner,'preservation'),mock.patch.object(runner,'samples',side_effect=AssertionError('REPLAY')):
                with self.assertRaises(FileExistsError):runner.execute()


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.learned=dict(reason='COMPLETE',wallets=[dict(cash=10200)]*2,weekly=[[4,3]]*51)
        self.fixed=dict(reason='REFERENCE_RISK_STOP',wallets=[dict(cash=9900)]*2,weekly=[[-2,-3]]*51)
        self.clusters=dict(separated_clusters=5,leave_largest_cluster_net=[100,80])

    def check(self,wanted):
        v=p.verdict(self.learned,self.fixed,self.clusters)
        self.assertEqual(v,a.audit_verdict(self.learned,self.fixed,self.clusters))
        self.assertEqual(v[0],wanted)
        return v

    def test_positive_is_research_only(self):self.check('HISTORICAL_TRANSFER_SUPPORTED_NOT_DEPLOYABLE')
    def test_risk_precedes_positive_net_and_no_hac(self):
        self.learned['reason']='REFERENCE_RISK_STOP'
        self.assertIsNone(self.check('REJECT_REFERENCE_RISK')[1])
    def test_flat_and_relative_only_do_not_count(self):
        for cash in (10000,9990):
            self.learned['wallets']=[dict(cash=cash)]*2
            self.assertIsNone(self.check('REJECT_NET_EDGE')[1])
    def test_positive_but_underperforms_fixed(self):
        self.fixed['wallets']=[dict(cash=10300)]*2;self.check('REJECT_NET_EDGE')
    def test_sparse_precedes_hac(self):
        self.clusters['separated_clusters']=4;self.assertIsNone(self.check('INSUFFICIENT_EVENT_CAPACITY')[1])
    def test_single_cluster_dominance(self):
        self.clusters['leave_largest_cluster_net'][1]=0;self.check('INSUFFICIENT_CONCENTRATION')
    def test_nominal_bounds_not_positive(self):
        self.learned['weekly']=[[0,0]]*51;self.check('INSUFFICIENT_STATISTICAL_SUPPORT')


class InputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pages=[];cls.raw={}
        for i,req in enumerate(runner.prep.canonical_requests()):
            times=range(req['start'],req['end']+1,req['step'])
            rows=([dict(symbol='BTCUSDT',fundingRateTimestamp=str(t),fundingRate='-0.0001') for t in times]
                if req['kind']=='funding' else [[str(t),'100','101','99','100']+
                     (['1','100'] if req['kind']=='trade' else []) for t in times])
            raw=json.dumps(dict(retCode=0,result=dict(category='linear',symbol='BTCUSDT',list=rows[::-1]))).encode()
            name=str(i)+'.json';cls.raw[name]=raw
            cls.pages.append(dict(request_index=i,kind=req['kind'],url=req['url'],file=name,
                sha256=hashlib.sha256(raw).hexdigest(),rows=len(rows),received_at_ms=req['end']+1,
                http_status=200,error=None))

    def test_full_real_domain_compilation_from_invented_inputs(self):
        with mock.patch.object(runner,'opener',side_effect=AssertionError('NETWORK')):
            raw,data=runner.compile_pages(self.pages,self.raw.__getitem__)
        rows=list(csv.DictReader(io.StringIO(raw.decode())))
        self.assertEqual((len(rows),len(data['mark']),len(data['funding'])),(104832,104832,1092))
        self.assertEqual(sum(int(r['execution_enabled']) for r in rows),104544)
        self.assertEqual(int(rows[-1]['timestamp']),1640822400000-300000)
        self.assertEqual(rows[-1]['funding_rate_per_interval'],'0')
        self.assertEqual(rows[0]['funding_rate_per_interval'],'-0.0001')
        self.assertEqual(rows[1]['funding_rate_per_interval'],'0')

    def test_missing_or_reordered_pages(self):
        for pages in (self.pages[:-1],self.pages[::-1]):
            with self.assertRaises(ValueError):runner.compile_pages(pages,self.raw.__getitem__)

    def test_bad_hash_receipt_error_and_alias(self):
        for key,value in (('sha256','changed'),('http_status',500),('rows',0),('file','../x.json')):
            pages=copy.deepcopy(self.pages);pages[0][key]=value
            with self.assertRaises(ValueError):runner.compile_pages(pages,self.raw.__getitem__)

    def test_missing_funding_with_rebound_hash_still_fails(self):
        pages=copy.deepcopy(self.pages);raw=dict(self.raw);name=pages[0]['file']
        value=json.loads(raw[name]);value['result']['list'].pop();raw[name]=json.dumps(value).encode()
        pages[0]['sha256']=hashlib.sha256(raw[name]).hexdigest();pages[0]['rows']-=1
        with self.assertRaisesRegex(ValueError,'PAGE_GRID'):runner.compile_pages(pages,raw.__getitem__)

    def test_numeric_probe_comparison_covers_volume(self):
        self.assertEqual(runner.point('trade',['1','100','101']),runner.point('trade',['1','100.0','101.00']))
        self.assertNotEqual(runner.point('trade',['1','100','101','99','100','1','100']),
                            runner.point('trade',['1','100','101','99','100','2','100']))


if __name__=='__main__':unittest.main()
