import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fixed_intent_delay import IntentState, OneBarQueue, FixedIntentPath, classify
from audit_fixed_intent_delay import audit, audit_source
from frozen_history_transfer import concentration, replay
from audit_frozen_history_transfer import audit_concentration
from test_offline_policy_correction import sample
from run_bounded_learning import sha
import run_fixed_intent_delay as runner


class StateTests(unittest.TestCase):
    def test_strong_then_weak_same_side_never_oscillates(self):
        for side in (-1,1):
            policy, queue = IntentState(), OneBarQueue(); actions = []
            for i,mu in enumerate([.002]+[.0005]*30):
                intent = policy.update((0,-side*mu/4))
                actions.append(queue.advance(i*300000,intent)[0])
            self.assertEqual(actions,[0]+[2500*side]*30)

    def test_constant_target_and_single_reversal(self):
        q = OneBarQueue(); values = [2500]*4+[-2500]*4+[0]*3
        self.assertEqual([q.advance(i*300000,v)[0] for i,v in enumerate(values)],[0]+values[:-1])

    def test_all_short_sequences_shift_exactly(self):
        for values in itertools.product((-2500,0,2500),repeat=5):
            q = OneBarQueue()
            for i,v in enumerate(values):
                due, origin = q.advance(i*300000,v)
                self.assertEqual(due,values[i-1] if i else 0)
                self.assertEqual(origin,(i-1)*300000 if i else None)

    def test_intent_changes_do_not_depend_on_actual_position(self):
        # Neither API accepts actual quantity; the identical intent stream gives identical due targets.
        a,b = IntentState(),IntentState(); qa,qb = OneBarQueue(),OneBarQueue()
        for i,x in enumerate([(0,-.002),(0,-.0001),(0,0),(0,.002)]):
            actual_a,actual_b = 0,(-1)**i*.25
            self.assertNotEqual(actual_a,actual_b)
            self.assertEqual(qa.advance(i*300000,a.update(x)),qb.advance(i*300000,b.update(x)))

    def test_cancel_is_permanent(self):
        q = OneBarQueue(); q.advance(0,2500); q.cancel()
        self.assertEqual(q.pending,0)
        with self.assertRaisesRegex(ValueError,'STOPPED'): q.advance(300000,2500)
        p = IntentState(); p.update((0,-.002)); p.stop()
        self.assertEqual(p.target,0)
        with self.assertRaisesRegex(ValueError,'STOPPED'): p.update((0,-.002))

    def test_invalid_target_and_clock(self):
        for value in (True,float('nan'),2501,1,'2500'):
            with self.assertRaises(ValueError): OneBarQueue().advance(0,value)
        for ts in (True,1,0.0):
            with self.assertRaises(ValueError): OneBarQueue().advance(ts,0)
        q = OneBarQueue(); q.advance(0,0)
        for ts in (0,600000):
            with self.assertRaises(ValueError): q.advance(ts,0)


class AccountTests(unittest.TestCase):
    def run_path(self,rows,intents):
        engine = FixedIntentPath(); events = []
        for row,intent in zip(rows,intents):
            e = engine.consume(row,intent)
            if e is not None: events.append(e)
        r = engine.finish(); events.append(dict(kind='TERMINAL',result=r))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'trace.jsonl'; p.write_text(''.join(json.dumps(e)+'\n' for e in events))
            checked = audit(p,rows,intents,dict(r,trace_sha256=sha(p)))
            audit_concentration(p,r,concentration(events,r))
        self.assertTrue(engine.queue.stopped); self.assertEqual(engine.queue.pending,0)
        self.assertEqual(classify(r),checked['verdict'])
        return r,events,checked

    def test_constant_intent_does_not_churn(self):
        r,e,a = self.run_path([sample(i) for i in range(200)],[2500]*200)
        self.assertEqual(a['economics'][0]['fills'],2)
        self.assertEqual([x['target'] for x in e[:-1]],[0]+[2500]*199)
        self.assertEqual(concentration(e,r)['episode_count'],1)

    def test_single_reversal(self):
        r,e,a = self.run_path([sample(i) for i in range(5)],[2500,2500,-2500,-2500,0])
        self.assertEqual([x['target'] for x in e[:-1]],[0,2500,2500,-2500,-2500])
        self.assertEqual(a['economics'][0]['fills'],3)

    def test_terminal_pending_entry_is_cancelled(self):
        r,_,a = self.run_path([sample(0)],[2500])
        self.assertEqual(a['economics'][0]['fills'],0)
        self.assertEqual(r['objective'],0)

    def test_terminal_settlement_once(self):
        r,_,a = self.run_path([sample(0),sample(1)],[2500,-2500])
        self.assertEqual(a['economics'][0]['fills'],2)
        self.assertEqual(r['wallets'][0]['qty'],0)

    def test_funding_holder_and_risk_cancel(self):
        rows = [sample(0),sample(1),sample(2,rate=1),sample(3),sample(4)]
        r,e,a = self.run_path(rows,[2500,-2500,-2500,-2500,-2500])
        self.assertEqual(r['reason'],'REFERENCE_RISK_STOP')
        self.assertEqual(r['exit_at_ms'],int(rows[3]['row']['timestamp']))
        self.assertEqual(a['economics'][0]['funding'],2500)
        self.assertEqual(a['economics'][0]['fills'],2)
        self.assertTrue(e[2]['queue_cancelled'])
        self.assertEqual(sum(x['kind'] == 'EXIT_OPEN' for x in e),1)

    def test_intrabar_risk_exits_next_open_with_gap_loss(self):
        rows = [sample(0),sample(1),sample(2,price=5000),sample(3)]
        rows[1]['row']['mark_low'] = '5000'
        r,_,a = self.run_path(rows,[2500]*4)
        self.assertEqual(r['active_bars'],2)
        self.assertLess(a['economics'][0]['net'],-1250)

    def test_final_bar_pending_risk_still_settles(self):
        rows = [sample(0),sample(1)]; rows[1]['row']['mark_low'] = '5000'
        r,_,a = self.run_path(rows,[2500]*2)
        self.assertEqual(r['reason'],'REFERENCE_RISK_STOP')
        self.assertEqual(a['economics'][0]['fills'],2)
        self.assertEqual(r['exit_at_ms'],int(rows[-1]['row']['timestamp'])+300000)

    def test_stress_book_stops_both(self):
        r,_,_ = self.run_path([sample(i) for i in range(350)],[2500*(-1)**i for i in range(350)])
        self.assertEqual(r['reason'],'REFERENCE_RISK_STOP')
        self.assertLess(r['wallets'][0]['max_drawdown'],.08)
        self.assertGreaterEqual(r['wallets'][1]['max_drawdown'],.08)

    def test_diagnostic_is_not_qualification(self):
        r,_,_ = self.run_path([sample(i) for i in range(720)],[2500*(-1)**(i//120) for i in range(720)])
        self.assertEqual(r['reason'],'COMPLETE')
        self.assertIsNotNone(r['first_legacy_withdrawal_ms'])
        self.assertFalse(r['qualification'])

    def test_full_week_tail_and_mandatory_cap_reduction(self):
        for n in (2016,2020):
            rows = [sample(i,price=10000+i*.7,closing=10000+(i+1)*.7) for i in range(n)]
            r,_,a = self.run_path(rows,[2500]*n)
            self.assertEqual(len(r['weekly']),1)
            self.assertGreater(a['economics'][1]['net'],0)
            self.assertGreater(a['economics'][0]['fills'],2)

    def test_price_and_feature_cannot_change_intent(self):
        a,b = FixedIntentPath(),FixedIntentPath()
        x = a.consume(sample(0,(0,-.002)),2500)
        y = b.consume(sample(0,(0,.002),price=5000,closing=7000,rate=1),2500)
        self.assertEqual(x['target'],y['target']); self.assertEqual(x['intent_target'],y['intent_target'])

    def test_finality_and_bar_gap(self):
        p = FixedIntentPath(); p.consume(sample(0),2500)
        with self.assertRaisesRegex(ValueError,'CLOCK'): p.consume(sample(2),2500)
        p.finish()
        with self.assertRaisesRegex(ValueError,'FINISHED'): p.finish()
        with self.assertRaisesRegex(ValueError,'FINISHED'): p.consume(sample(1),2500)

    def test_tampered_future_origin_target_cash_and_qualification(self):
        rows = [sample(i) for i in range(3)]; intents = [2500,2500,-2500]
        result,events,_ = self.run_path(rows,intents)
        for change in ('origin','target','intent','cash','qualification','missing_stress'):
            e = copy.deepcopy(events); r = copy.deepcopy(result)
            if change == 'origin': e[1]['intent_origin_ms'] += 300000
            elif change == 'target': e[0]['target'] = 2500
            elif change == 'intent': e[1]['intent_target'] = -2500
            elif change == 'cash': e[1]['wallets'][0]['cash'] += 1
            elif change == 'missing_stress': e[1]['receipts'].pop()
            else: r['qualification'] = True; e[-1]['result'] = r
            with tempfile.TemporaryDirectory() as d:
                p = Path(d)/'trace.jsonl'; p.write_text(''.join(json.dumps(x)+'\n' for x in e))
                with self.assertRaises(ValueError): audit(p,rows,intents,dict(r,trace_sha256=sha(p)))


class SourceAndVerdictTests(unittest.TestCase):
    def test_source_extraction_and_causality(self):
        rows = [sample(0,(0,0)),sample(1,(0,-.002)),sample(2,(0,-.0001))]; events = []
        result = replay(rows,(0,-4),events.append)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'normal.jsonl'; p.write_text(''.join(json.dumps(e)+'\n' for e in events))
            r = dict(result,trace_sha256=sha(p))
            self.assertEqual(runner.load_intents(p,rows,r),[0,2500,2500])
            self.assertEqual(audit_source(p,rows,r),[0,2500,2500])
            events[0]['target'] = 2500
            p.write_text(''.join(json.dumps(e)+'\n' for e in events)); r['trace_sha256'] = sha(p)
            with self.assertRaises(ValueError): audit_source(p,rows,r)

    def test_wrong_source_hash_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'normal.jsonl'; p.write_text('{}')
            with self.assertRaises(ValueError): runner.load_intents(p,[],dict(trace_sha256='bad'))

    def test_attempt_latches_before_any_source_loading(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'freeze.json').write_text('{}'); (root/'economic-attempt.json').write_text('{}')
            with mock.patch.object(runner,'RUN',root),mock.patch.object(runner,'check'), \
                 mock.patch.object(runner,'preservation'),mock.patch.object(runner,'read_domain',side_effect=AssertionError('REPLAY')):
                with self.assertRaises(FileExistsError): runner.execute()

    def test_positive_counterfactual_not_closed_loop(self):
        r = dict(reason='COMPLETE',terminal_flat=True,wallets=[dict(qty=0,cash=10001)]*2)
        self.assertEqual(classify(r),'FIXED_INTENT_DELAY_SUPPORTED_NOT_CLOSED_LOOP')
        r['reason'] = 'REFERENCE_RISK_STOP'
        self.assertEqual(classify(r),'REJECT_FIXED_INTENT_DELAY_RISK')
        r['reason'] = 'COMPLETE'; r['wallets'][0]['cash'] = 10000
        self.assertEqual(classify(r),'REJECT_FIXED_INTENT_DELAY_NET')

    def test_incomplete_account_rejected(self):
        for cash,qty in ((float('nan'),0),(10001,1)):
            with self.assertRaises(ValueError):
                classify(dict(reason='COMPLETE',terminal_flat=True,wallets=[dict(qty=qty,cash=cash)]*2))


if __name__ == '__main__': unittest.main()
