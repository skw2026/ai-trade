import copy
import math
import unittest

from audit_eth_sol_baseline import audit_events, audit_marked_risk
from eth_sol_model import PairLedger
from run_eth_sol_baseline import simulate


class NoSignalArchive:
    warmup=[(100,100)]*1440
    def bars(self,t,kind):return [[100,100,100,100]+([100000] if kind=='trade' else []) for _ in (0,1)]
    def rates(self,t):return {}


class PersistentSignalArchive(NoSignalArchive):
    warmup=[(math.exp(1+1.2*(3+i*.0001)+.001*math.sin(i)),math.exp(3+i*.0001)) for i in range(1440)]


class BaselineTests(unittest.TestCase):
    def test_zero_activity_is_not_success(self):
        result,paths=simulate(NoSignalArchive(),0,60*60000,[2.,24])
        self.assertEqual(result['status'],'REJECT_FIXED_BASELINE_NET_OR_ACTIVITY')
        self.assertFalse(result['passed_to_learning']);self.assertEqual(result['processed_minutes'],60)
        self.assertEqual(result['base']['observed_five_minute_intervals'],12)
        for p in paths:audit_events(p.events,p.result(),p.stress)

    def test_bad_input_not_zero_filled(self):
        class Missing(NoSignalArchive):
            def bars(self,t,kind):raise ValueError('missing')
        with self.assertRaisesRegex(ValueError,'missing'):simulate(Missing(),0,60*60000,[2.,24])

    def test_full_cash_audit_and_mutations(self):
        for stress in (False,True):
            p=PairLedger(stress);p.decision(0,[103,100],(0,1,.01),10000)
            bars=[[100,101,99,100,10000]]*2
            p.process(1,bars);p.process(2,bars);p.fund(3,{0:.001,1:-.001},[100,100])
            p.begin_exit(4,'test');p.process(5,bars);p.process(6,bars)
            self.assertEqual(audit_events(p.events,p.result(),stress)['fills'],4)
            for field in ('fee','price','cash','delta'):
                e=copy.deepcopy(p.events);first=next(v for v in e if v['kind']=='fill');first[field]+=1
                with self.assertRaises(ValueError):audit_events(e,p.result(),stress)

    def test_partial_entry_audit(self):
        p=PairLedger();p.decision(0,[103,100],(0,1,.01),10000)
        p.process(1,[[100,100,100,100,10]]*2);p.process(2,[[100,100,100,100,10000]]*2)
        self.assertEqual(audit_events(p.events,p.result(),False)['completed_episodes'],0)

    def test_committed_second_leg_precedes_future_risk_signal(self):
        p=PairLedger();p.decision(0,[103,100],(0,1,.01),10000);p.process(1,[[100,100,100,100,10000]]*2)
        reasons=p.risk(2,[[100,1000,100,100]]*2,defer=True)
        self.assertTrue(reasons);p.process(2,[[100,100,100,100,10000]]*2)
        self.assertGreater(p.q[1],0)
        for reason in reasons:p.stop(2,reason)
        self.assertGreater(p.q[1],0);self.assertTrue(p.exiting)

    def test_execution_failure_still_contains_risk(self):
        p=PairLedger();p.decision(0,[103,100],(0,1,.01),10000);p.process(1,[[100,100,100,100,10000]]*2)
        p.stop(1,'RESEARCH_DRAWDOWN_8_PERCENT')
        for minute in range(2,8):p.process(minute,[[100,100,100,100,0]]*2)
        self.assertEqual(p.stopped,'RESEARCH_DRAWDOWN_8_PERCENT');self.assertIsNotNone(p.insufficient)
        self.assertNotEqual(p.q,[0,0])

    def test_full_runner_terminal_exit_independent_risk(self):
        archive=PersistentSignalArchive();result,paths=simulate(archive,0,20*60000,[2.,24])
        self.assertEqual(result['status'],'REJECT_FIXED_BASELINE_NET_OR_ACTIVITY')
        for name,p in zip(('base','stress'),paths):
            self.assertEqual(p.episodes,1);self.assertTrue(p.flat)
            audit_events(p.events,p.result(),p.stress)
            audit_marked_risk(archive,p.events,p.result(),result,p.stress)

    def test_runner_safety_stops_without_completing_domain(self):
        class Adverse(PersistentSignalArchive):
            def bars(self,t,kind):
                eth=100-max(0,t/60000)*.0005
                return [[price]*4+([100000] if kind=='trade' else []) for price in (eth,100)]
        archive=Adverse();result,paths=simulate(archive,0,2640*60000,[2.,24])
        self.assertEqual(result['status'],'REJECT_RISK_OR_SAFETY')
        self.assertLess(result['processed_minutes'],result['requested_minutes'])
        self.assertFalse(result['passed_to_learning'])
        for p in paths:
            audit_events(p.events,p.result(),p.stress)
            audit_marked_risk(archive,p.events,p.result(),result,p.stress)


if __name__=='__main__':unittest.main()
