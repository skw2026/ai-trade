"""Synthetic-only method, temporal boundary, safety and independent audit tests."""
import copy
import csv
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import offline_policy_correction as policy
import run_offline_policy_correction as runner
import audit_offline_policy_correction as auditor
from run_bounded_learning import sha


def row(i, price=10000., closing=None, rate=0.):
    cl = price if closing is None else closing
    return dict(timestamp=str(1735689600000+i*300000),open=str(price),price=str(cl),
                high=str(max(price,cl)),low=str(min(price,cl)),volume='100',
                mark_open=str(price),mark_close=str(cl),mark_high=str(max(price,cl)),
                mark_low=str(min(price,cl)),funding_rate_per_interval=str(rate))


def sample(i, x=(.002,0), price=10000., closing=None, rate=0., bucket=1):
    return dict(row=row(i,price,closing,rate),features=x,bucket=bucket)


class PolicyTests(unittest.TestCase):
    def audit(self, samples, vector):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'trace.jsonl'
            with path.open('w') as out:
                result = policy.replay(samples,vector,lambda r:out.write(json.dumps(r)+'\n'))
            result.update(trace_file=path.name,trace_sha256=sha(path))
            audited = auditor.audit_trace(path,samples,result)
            return result,audited

    def test_completed_context_and_explicit_units(self):
        history = [row(i,10000+i,10001+i) for i in range(288)]
        self.assertIsNone(policy.features(history[:287]))
        a,b = policy.features(history)
        self.assertAlmostEqual(a,(10288/10276-1)/12)
        self.assertAlmostEqual(b,(10288/10000-1)/288)

    def test_cost_hurdle_hold_flat_and_reversal(self):
        self.assertEqual(policy.decide((.001,0),(1,0),0),0)
        self.assertEqual(policy.decide((.002,0),(1,0),0),2500)
        self.assertEqual(policy.decide((.0001,0),(1,0),1),2500)
        self.assertEqual(policy.decide((-.0001,0),(1,0),1),0)
        self.assertEqual(policy.decide((-.002,0),(1,0),1),-2500)

    def test_tie_holds_and_invalid_inputs_rejected(self):
        self.assertEqual(policy.decide((0,0),(1,0),1),2500)
        self.assertEqual(policy.decide((0,0),(1,0),0),0)
        self.assertEqual(policy.decide(None,(1,0),0),0)
        for vector in ((.5,0),(1,0,0)):
            with self.assertRaises(ValueError):policy.decide((.002,0),vector,0)
        with self.assertRaises(ValueError):policy.decide((float('nan'),0),(1,0),0)

    def test_current_bar_cannot_change_decision(self):
        a = policy.Path((1,0));b = policy.Path((1,0))
        x = sample(0);y = sample(0,price=5000,closing=7000,rate=.1)
        self.assertEqual(a.consume(x)['target'],b.consume(y)['target'])

    def test_negative_objective_can_be_learned(self):
        seen = []
        def score(v):
            seen.append(v)
            return dict(coefficients=list(v),objective=-10-(v[0]-2)**2-(v[1]+1)**2)
        model = policy.optimize(score)
        self.assertEqual(model['coefficients'],[2,-1])
        self.assertEqual(model['winner']['objective'],-10)
        self.assertTrue(model['changed'])
        self.assertLessEqual(len(seen),25)
        self.assertEqual(len(seen),len(set(seen)))
        auditor.audit_selection(model)

    def test_no_update_for_ties_and_no_eval_feedback(self):
        model = policy.optimize(lambda v:dict(objective=0))
        self.assertEqual(model['coefficients'],[1,0])
        self.assertFalse(model['changed'])
        for scope in ('confirmation','development_evaluation'):
            with self.assertRaisesRegex(ValueError,'TRAIN_DOMAIN_ONLY'):
                policy.optimize(lambda v:dict(objective=0),domain=scope)

    def test_safety_stops_path_but_reader_finishes_domain(self):
        samples = [sample(i,(.002*(-1)**(i//120),0)) for i in range(700)]
        result,audit = self.audit(samples,(1,0))
        self.assertEqual(result['reason'],'SAFETY_WITHDRAWAL')
        self.assertEqual(result['active_bars'],480)
        self.assertEqual(result['domain_bars'],700)
        self.assertEqual(result['exit_at_ms'],int(samples[480]['row']['timestamp']))
        self.assertTrue(result['terminal_flat'])
        self.assertEqual(result['exposure_short_flat_long'][1],220)
        self.assertLess(audit['economics'][0]['net'],0)

    def test_risk_keeps_loss_and_exits_next_open(self):
        rows = [sample(0),sample(1,price=5000),sample(2,price=20000)]
        rows[0]['row']['mark_low']='5000'
        result,audit = self.audit(rows,(1,0))
        self.assertEqual(result['reason'],'REFERENCE_RISK_STOP')
        self.assertEqual(result['active_bars'],1)
        self.assertEqual(result['wallets'][0]['qty'],0)
        self.assertLess(audit['economics'][0]['net'],-1250)
        self.assertEqual(result['exit_at_ms'],int(rows[1]['row']['timestamp']))

    def test_open_funding_stop_and_old_holder(self):
        rows = [sample(0),sample(1,(-.002,0),rate=1),sample(2),sample(3)]
        result,audit = self.audit(rows,(1,0))
        self.assertEqual(result['reason'],'REFERENCE_RISK_STOP')
        self.assertAlmostEqual(audit['economics'][0]['funding'],2500)
        self.assertEqual(audit['economics'][0]['fills'],2)

    def test_stopped_comparator_does_not_stop_other_path(self):
        rows = [sample(i,(.002*(-1)**(i//120),0)) for i in range(700)]
        stopped = policy.replay(rows,(1,0));flat = policy.replay(rows,(0,0))
        self.assertEqual(stopped['active_bars'],480)
        self.assertEqual(flat['active_bars'],700)
        self.assertEqual(flat['reason'],'COMPLETE')
        self.assertEqual(flat['objective'],0)

    def test_no_restart_no_second_finish(self):
        p = policy.Path((1,0))
        r = sample(0);r['row']['mark_low']='5000';p.consume(r)
        p.consume(sample(1));fills = p.books[0].wallet.fills
        for i in range(2,20):p.consume(sample(i,(.005,0)))
        self.assertEqual(p.books[0].wallet.fills,fills)
        p.finish()
        with self.assertRaisesRegex(ValueError,'FINISHED'):p.finish()
        with self.assertRaisesRegex(ValueError,'FINISHED'):p.consume(sample(20))

    def test_full_weeks_settlement_and_partial_are_reconciled(self):
        for n in (2016,2020):
            result,audit = self.audit([sample(i,(0,0)) for i in range(n)],(0,0))
            self.assertEqual(len(result['weekly']),1)
            self.assertEqual(result['partial_week'],[0,0])
            self.assertEqual(audit['economics'][0]['net'],0)

    def test_long_short_stress_and_funding_independent_decimal(self):
        rows = [sample(i,(.002 if i<10 else -.002,0),price=10000+i*.71,
                       closing=10000+i*.71+.5,rate=.0001) for i in range(25)]
        result,audit = self.audit(rows,(1,0))
        self.assertLess(audit['economics'][1]['net'],audit['economics'][0]['net'])
        self.assertEqual(result['reason'],'COMPLETE')

    def test_nonzero_full_week_and_cap_reductions_reconcile(self):
        rows = [sample(i,price=10000+i*.7,closing=10000+(i+1)*.7) for i in range(2016)]
        result,audit = self.audit(rows,(1,0))
        self.assertEqual(result['reason'],'COMPLETE')
        self.assertGreater(audit['economics'][1]['net'],0)
        self.assertGreater(result['wallets'][0]['fills'],2)
        self.assertAlmostEqual(result['weekly'][0][0],audit['economics'][0]['net'])

    def test_audit_rejects_training_vector_relabelling(self):
        model = policy.optimize(lambda v:dict(coefficients=list(v),objective=-sum(x*x for x in v)))
        model['trials'][0]['result']['coefficients']=[4,4]
        with self.assertRaisesRegex(ValueError,'TRIAL_MODEL_BINDING'):
            auditor.audit_selection(model)

    def test_whole_domain_clock_cannot_skip_bad_period(self):
        with self.assertRaisesRegex(ValueError,'CLOCK'):
            policy.replay([sample(0),sample(2)],(1,0))

    def test_receipt_tamper_detected_even_if_hash_rebound(self):
        rows = [sample(i) for i in range(4)]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'trace.jsonl'
            records = []
            result = policy.replay(rows,(1,0),records.append)
            records[0]['target'] = -2500
            path.write_text(''.join(json.dumps(r)+'\n' for r in records))
            result.update(trace_file=path.name,trace_sha256=sha(path))
            with self.assertRaisesRegex(ValueError,'causal_action'):
                auditor.audit_trace(path,rows,result)

    def test_positive_relative_or_flat_cannot_pass(self):
        path = policy.replay([sample(i,(0,0)) for i in range(28)],(0,0))
        model = dict(changed=True,winner=path)
        fixed = copy.deepcopy(path)
        for w in fixed['wallets']:w['cash']=9990
        self.assertEqual(policy.verdict(model,fixed,path)[0],'NO_GO_NO_ABSOLUTE_LEARNING_EDGE')

    def test_risk_latch_cannot_be_hidden_in_zero_tail(self):
        path = policy.replay([sample(i,(0,0)) for i in range(28)],(0,0))
        path['reason']='SAFETY_WITHDRAWAL'
        path['weekly']=[[10,10]]*39
        for w in path['wallets']:w['cash']=10100
        self.assertEqual(policy.verdict(dict(changed=True,winner=path),path,path)[0],
                         'NO_GO_SAFETY_OR_REFERENCE_RISK')

    def test_complete_positive_support_requires_all_lower_bounds(self):
        base = dict(reason='COMPLETE',wallets=[dict(cash=10010),dict(cash=10009)],weekly=[[1,1]]*26)
        better = dict(reason='COMPLETE',wallets=[dict(cash=10020),dict(cash=10018)],weekly=[[2,2]]*26)
        model = dict(changed=True,winner=better)
        self.assertEqual(policy.verdict(model,base,better)[0],'DEVELOPMENT_SUPPORTED_CONFIRMATION_UNAVAILABLE')
        better['weekly']=[[1,1]]*26
        self.assertEqual(policy.verdict(model,base,better)[0],'INSUFFICIENT_DEVELOPMENT_EVIDENCE')

    def test_native_sampling_and_independent_regime_parity(self):
        binary = Path(os.environ.get('OFFLINE_POLICY_TEST_DRIVER',str(runner.BINARY)))
        self.assertTrue(binary.is_file(),'native signal-only fixture required; do not skip')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'input.csv'
            rows = [row(i,10000+(i%7)*23,10000+((i+1)%7)*23) for i in range(900)]
            with path.open('w') as out:
                writer=csv.DictWriter(out,fieldnames=rows[0].keys());writer.writeheader();writer.writerows(rows)
                out.write('UNPARSEABLE_FUTURE_EVALUATION_RECORD\n')
            start=int(rows[288]['timestamp']);end=int(rows[-1]['timestamp'])+300000
            actual = runner.samples(path,start,end,binary,time.monotonic()+30)
            expected = auditor.read_domain(path,start,end)
            self.assertEqual(actual,expected)
            self.assertEqual(len(actual),612)


if __name__ == '__main__':
    unittest.main()
