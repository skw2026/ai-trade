import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sparse_reversal_2024_model import DelayedPath, decision
from audit_sparse_reversal_2024 import audit_delay
from frozen_history_transfer import concentration
from audit_frozen_history_transfer import audit_concentration
from test_offline_policy_correction import sample
from run_bounded_learning import sha
import run_sparse_reversal_2024 as runner


class DelayTests(unittest.TestCase):
    def run_path(self, rows):
        path = DelayedPath(); events = []
        for row in rows:
            e = path.consume(row)
            if e is not None: events.append(e)
        result = path.finish(); events.append(dict(kind='TERMINAL', result=result))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'trace.jsonl'; p.write_text(''.join(json.dumps(e)+'\n' for e in events))
            audit = audit_delay(p, rows, dict(result, trace_sha256=sha(p)))
            audit_concentration(p, result, concentration(events, result))
        return result, events, audit

    def test_queue_and_actual_previous_direction(self):
        r, e, _ = self.run_path([sample(0,(0,-.002)), sample(1,(0,.002)), sample(2,(0,0))])
        self.assertEqual([x['target'] for x in e[:-1]], [0,2500,-2500])
        self.assertEqual([x['decision_target'] for x in e[:-1]], [2500,-2500,2500])
        self.assertTrue(r['terminal_flat'])

    def test_last_unexecuted_signal_cancelled(self):
        r, _, a = self.run_path([sample(0,(0,-.002))])
        self.assertEqual(a['economics'][0]['fills'], 0)
        self.assertEqual(r['objective'], 0)

    def test_terminal_position_closed_once(self):
        r, _, a = self.run_path([sample(i,(0,-.002)) for i in range(2)])
        self.assertEqual(a['economics'][0]['fills'], 2)
        self.assertEqual(r['wallets'][0]['qty'], 0)

    def test_funding_precedes_queued_reversal_and_risk_not_delayed(self):
        rows = [sample(0,(0,-.002)),sample(1,(0,.002)),sample(2,(0,.002),rate=1),sample(3),sample(4)]
        r, e, a = self.run_path(rows)
        self.assertEqual(r['reason'], 'REFERENCE_RISK_STOP')
        self.assertEqual(r['exit_at_ms'], int(rows[3]['row']['timestamp']))
        self.assertEqual(a['economics'][0]['funding'], 2500)
        self.assertEqual(a['economics'][0]['fills'], 2)
        self.assertEqual(sum(x['kind'] == 'EXIT_OPEN' for x in e), 1)

    def test_gap_stop_keeps_loss(self):
        rows = [sample(0,(0,-.002)),sample(1,(0,-.002)),sample(2,price=5000),sample(3)]
        rows[1]['row']['mark_low'] = '5000'
        r, _, a = self.run_path(rows)
        self.assertEqual(r['active_bars'], 2)
        self.assertLess(a['economics'][0]['net'], -1250)

    def test_stress_alone_stops_both(self):
        r, _, _ = self.run_path([sample(i,(0,.002*(-1)**i)) for i in range(350)])
        self.assertEqual(r['reason'], 'REFERENCE_RISK_STOP')
        self.assertLess(r['wallets'][0]['max_drawdown'], .08)
        self.assertGreaterEqual(r['wallets'][1]['max_drawdown'], .08)

    def test_diagnostic_is_recorded_not_used_to_rearm(self):
        r, _, _ = self.run_path([sample(i,(0,.002*(-1)**(i//120))) for i in range(720)])
        self.assertEqual(r['reason'], 'COMPLETE')
        self.assertIsNotNone(r['first_legacy_withdrawal_ms'])
        self.assertEqual(r['legacy_path_status'], 'REJECT_LEGACY_SAFETY')
        self.assertFalse(r['qualification'])

    def test_week_and_partial_sum(self):
        for n in (2016,2020):
            r, _, a = self.run_path([sample(i,(0,-.002),price=10000+i*.7,closing=10000+(i+1)*.7) for i in range(n)])
            self.assertGreater(a['economics'][1]['net'], 0)
            self.assertEqual(len(r['weekly']), 1)

    def test_current_bar_does_not_change_signal(self):
        a, b = DelayedPath(), DelayedPath()
        x = a.consume(sample(0,(0,-.002)))
        y = b.consume(sample(0,(0,-.002),price=5000,closing=7000,rate=1))
        self.assertEqual(x['decision_target'], y['decision_target'])
        self.assertEqual(x['target'], y['target'])

    def test_finality_and_gap(self):
        p = DelayedPath(); p.consume(sample(0))
        with self.assertRaisesRegex(ValueError, 'CLOCK'): p.consume(sample(2))
        p.finish()
        with self.assertRaisesRegex(ValueError, 'FINISHED'): p.finish()
        with self.assertRaisesRegex(ValueError, 'FINISHED'): p.consume(sample(1))

    def test_tamper_signal_queue_cash_or_qualification(self):
        rows = [sample(i,(0,-.002)) for i in range(3)]
        r, events, _ = self.run_path(rows)
        for kind in ('signal','queue','cash','qualification'):
            e = copy.deepcopy(events); result = copy.deepcopy(r)
            if kind == 'signal': e[0]['decision_target'] = 0
            elif kind == 'queue': e[0]['target'] = 2500
            elif kind == 'cash': e[1]['wallets'][0]['cash'] += 1
            else: result['qualification'] = True; e[-1]['result'] = result
            with tempfile.TemporaryDirectory() as d:
                p = Path(d)/'trace.jsonl'; p.write_text(''.join(json.dumps(x)+'\n' for x in e))
                with self.assertRaises(ValueError): audit_delay(p, rows, dict(result,trace_sha256=sha(p)))

    def test_attempt_latches_before_features(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p/'execution-freeze.json').write_text(json.dumps(dict(paths=['normal','control','delay'],maximum_attempts=1)))
            (p/'economic-attempt.json').write_text('{}')
            with mock.patch.object(runner,'OUT',p), mock.patch.object(runner,'check'), \
                 mock.patch.object(runner,'samples',side_effect=AssertionError('ECONOMIC_REPLAY')):
                with self.assertRaises(FileExistsError): runner.execute()


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.normal = dict(reason='COMPLETE',wallets=[dict(cash=10200)]*2,weekly=[[4,3]]*52)
        self.control = dict(reason='REFERENCE_RISK_STOP',wallets=[dict(cash=9900)]*2,weekly=[[-2,-3]]*52)
        self.groups = dict(separated_clusters=5,leave_largest_cluster_net=[100,80])

    def test_support_never_qualifies(self):
        r = decision(self.normal,self.control,self.normal,self.groups)
        self.assertEqual(r['status'], 'DEVELOPMENT_SUPPORTED_CONFIRMATION_NOT_ADMITTED')
        self.assertFalse(r['g4']); self.assertFalse(r['qualification'])

    def test_delay_not_selected_as_winner(self):
        for why, expected in (('COMPLETE','REJECT_DELAY_NET'),('REFERENCE_RISK_STOP','REJECT_DELAY_RISK')):
            delay = dict(reason=why,wallets=[dict(cash=9999)]*2)
            self.assertEqual(decision(self.normal,self.control,delay,self.groups)['status'], expected)

    def test_normal_rejection_kept(self):
        self.normal['wallets'] = [dict(cash=9990)]*2
        delay = dict(reason='COMPLETE',wallets=[dict(cash=11000)]*2)
        self.assertEqual(decision(self.normal,self.control,delay,self.groups)['status'], 'REJECT_NET_EDGE')


if __name__ == '__main__': unittest.main()
