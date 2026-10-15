import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eth_sol_complete_model import (Alert, CompleteLedger, Simulation, select,
    metrics, economic_decision, confirmation_plan, long_variance, sharpe)
from eth_sol_model import PairLedger
from test_eth_sol_baseline import NoSignalArchive, PersistentSignalArchive
from audit_eth_sol_complete import (audit_snapshot, independent_ols,bind_result,
                                   independent_selection,independent_verdict,audit_report_math)
import eth_sol_complete_batch as batch


def bars(price=100, volume=100000):
    return [[price,price,price,price,volume] for _ in range(2)]


def result_fixture():
    daily=[[i*1440,10000+i*3,10000+i*2] for i in range(367)]
    cycles=[dict(start=(92+i*8)*1440,end=(93+i*8)*1440,completed=True,net=10) for i in range(33)]
    book=dict(cash=11098,positions=[0,0],stop=None,insufficient=None,cycles=cycles)
    return dict(parameters=[1.5,12],start=1704067200000,end=1735689600000,
        processed_minutes=366*1440,requested_minutes=366*1440,
        base=copy.deepcopy(book),stress=dict(copy.deepcopy(book),cash=10732),
        daily=daily,active_weeks=[list(range(2800,2900)),list(range(2800,2900))])


class CompleteTests(unittest.TestCase):
    def test_alert_not_permanent_withdrawal(self):
        a=Alert()
        for i in range(480):
            self.assertTrue(a.observe(-1,(i+1)*5,lambda:True))
        self.assertTrue(a.active);self.assertEqual(a.first_legacy_withdrawal,2400)
        for i in range(480):a.observe(1,2400+(i+1)*5,lambda:True)
        self.assertFalse(a.active)
        self.assertEqual(a.first_legacy_withdrawal,2400)

    def test_zero_window_does_not_clear(self):
        a=Alert()
        for i in range(480):a.observe(-1,(i+1)*5,lambda:True)
        for i in range(480):a.observe(0,2400+(i+1)*5,lambda:True)
        self.assertTrue(a.active)

    def test_failed_review_is_not_ignored(self):
        a=Alert();results=[a.observe(-1,(i+1)*5,lambda:False) for i in range(480)]
        self.assertFalse(results[-1]);self.assertTrue(a.active)

    def test_old_safety_unchanged(self):
        p=PairLedger()
        for i in range(480):
            p.cash-=.01;p.close_minute((i+1)*5,[100,100],True)
        self.assertEqual(p.stopped,'SAFETY_WITHDRAWAL')

    def test_ledger_review_rejects_cash_mutation(self):
        p=CompleteLedger();self.assertTrue(p.review());p.cash+=1
        self.assertFalse(p.review())

    def test_live_alert_review_and_cash(self):
        p=CompleteLedger();p.decision(0,[103,100],(0,1,.01),10000)
        p.process(1,bars());p.process(2,bars());self.assertTrue(p.review())
        for i in range(480):
            p.close_minute((i+1)*5,[100+i*.0001,100],True)
        self.assertIsNone(p.stopped);self.assertTrue(p.alert.active)
        self.assertTrue(p.review())

    def test_delay_only_first_attempt_and_fixed_exit_deadline(self):
        p=CompleteLedger(extra_delay=1);p.decision(0,[103,100],(0,1,.01),10000)
        p.process(1,bars());self.assertTrue(p.flat)
        p.process(2,bars());self.assertNotEqual(p.q[0],0);self.assertEqual(p.q[1],0)
        p.process(3,bars());self.assertNotEqual(p.q[1],0)
        p.begin_exit(4,'TEST');self.assertEqual(p.exit_deadline,9)
        p.process(5,bars());self.assertNotEqual(p.q[0],0)
        p.process(6,bars());self.assertEqual(p.q[0],0)
        p.process(7,bars());self.assertTrue(p.flat);self.assertTrue(p.review())

    def test_partial_entry_aborts_and_costs_count(self):
        p=CompleteLedger();p.decision(0,[103,100],(0,1,.01),1000)
        p.process(1,bars(volume=10));p.process(2,bars())
        self.assertTrue(p.flat);self.assertLess(p.cash,10000)
        self.assertFalse(p.cycles[0]['completed']);self.assertTrue(p.review())

    def test_unclosed_liability_never_erased(self):
        p=CompleteLedger();p.decision(0,[103,100],(0,1,.01),10000)
        p.process(1,bars());p.process(2,bars());q=list(p.q);p.begin_exit(3,'TEST')
        for m in range(4,10):p.process(m,bars(volume=0))
        self.assertEqual(p.q,q);self.assertIsNotNone(p.insufficient)

    def test_attribution_identity_with_funding(self):
        for stress in (False,True):
            p=CompleteLedger(stress);p.decision(0,[103,100],(0,1,.01),1000)
            p.process(1,bars());p.process(2,bars());p.fund(3,{0:.001,1:-.001},[100,100])
            p.begin_exit(4,'TEST');p.process(5,bars(101));p.process(6,bars(99))
            self.assertAlmostEqual(p.cash-10000,float(p.gross_cash-p.slippage)-p.fees-p.funding,places=7)
            self.assertTrue(p.review())

    def test_no_q1_terminal_exit_or_state_reset(self):
        a=PersistentSignalArchive();sim=Simulation(a,0,200*60000,[2,24]);sim.run_to(10)
        self.assertFalse(sim.paths[0].flat)
        snap=json.loads(json.dumps(sim.snapshot()))
        resumed=Simulation.restore(a,snap)
        self.assertEqual(resumed.paths[0].q,sim.paths[0].q)
        self.assertEqual(resumed.paths[0].peak,sim.paths[0].peak)
        sim.run_to(200);resumed.run_to(200)
        self.assertEqual(sim.snapshot(),resumed.snapshot())
        self.assertTrue(sim.paths[0].flat)

    def test_checkpoint_pending_queue(self):
        a=PersistentSignalArchive();s=Simulation(a,0,100*60000,[2,24]).run_to(1)
        snap=json.loads(json.dumps(s.snapshot()));r=Simulation.restore(a,snap)
        self.assertTrue(r.paths[0].queue);self.assertTrue(r.paths[0].flat)
        s.run_to(100);r.run_to(100);self.assertEqual(s.result(),r.result())

    def test_future_prices_cannot_change_prefix(self):
        class Future(PersistentSignalArchive):
            def bars(self,t,kind):
                return [[2000]*4+([100000] if kind=='trade' else []) for _ in (0,1)] if t>=10*60000 else super().bars(t,kind)
        a=Simulation(PersistentSignalArchive(),0,100*60000,[2,24]).run_to(10)
        b=Simulation(Future(),0,100*60000,[2,24]).run_to(10)
        self.assertEqual(a.snapshot(),b.snapshot())

    def test_bad_input_propagates(self):
        class Missing(NoSignalArchive):
            def bars(self,t,kind):raise ValueError('missing')
        with self.assertRaisesRegex(ValueError,'missing'):
            Simulation(Missing(),0,100*60000,[2,24]).run_to(10)

    def test_linked_hard_stop_never_resets(self):
        a=NoSignalArchive();s=Simulation(a,0,100*60000,[2,24])
        s.paths[1].cash-=900;s.paths[1].audit_cash-=900
        s.run_to(10);self.assertTrue(all(p.stopped for p in s.paths))
        original=s.paths[1].cash;s.run_to(100)
        self.assertEqual(s.paths[1].cash,original)
        self.assertLess(len(s.paths[0].events),10)

    def test_negative_baseline_does_not_block_selection(self):
        def row(net,stop=None,turnover=1):
            return dict(last_liquidation=[10000+net]*2,base=dict(stop=stop,insufficient=None,traded_notional=turnover),
                stress=dict(stop=stop,insufficient=None,traded_notional=turnover))
        self.assertEqual(select([row(-100),row(-10)]),1)
        self.assertEqual(select([row(-100,'risk'),row(-10)]),1)
        self.assertEqual(select([row(10),row(10)]),0)
        self.assertEqual(select([row(-100),row(10,turnover=3),row(10,turnover=1)]),2)
        self.assertIsNone(select([row(100,'risk')]))

    def test_development_positive_control(self):
        c=result_fixture();b=result_fixture()
        b['daily']=[[r[0],r[1]-r[0]/1440,r[2]-r[0]/1440] for r in b['daily']]
        d=economic_decision(c,b,c,1)
        self.assertEqual(d['status'],'DEVELOPMENT_SUPPORTED');self.assertTrue(d['learned_gain_claim'])

    def test_fixed_winner_does_not_claim_learning(self):
        c=result_fixture();d=economic_decision(c,c,c,0)
        self.assertEqual(d['status'],'DEVELOPMENT_SUPPORTED');self.assertFalse(d['learned_gain_claim'])

    def test_no_gain_rejected(self):
        c=result_fixture();self.assertIn('LEARNING_GAIN_HURDLE',economic_decision(c,c,c,1)['failures'])

    def test_zero_trades_rejected(self):
        c=result_fixture()
        for key in ('base','stress'):c[key]['cycles']=[]
        self.assertEqual(economic_decision(c,c,c,0)['status'],'INSUFFICIENT_EVENT_DIVERSITY')

    def test_risk_and_insufficiency_precedence(self):
        c=result_fixture();c['stress']['stop']='RISK'
        self.assertEqual(economic_decision(c,c,c,0)['status'],'NO_GO_RISK')
        c['stress']['insufficient']='NO_EXIT'
        self.assertEqual(economic_decision(c,c,c,0)['status'],'TECHNICAL_OR_EXECUTION_INSUFFICIENT')

    def test_net_and_quarter_and_delay_hurdles(self):
        c=result_fixture();c['daily']=[ [r[0],r[1],10000+r[0]/1440*.1] for r in c['daily']]
        self.assertIn('ABSOLUTE_NET_HURDLE',economic_decision(c,c,c,0)['failures'])
        c=result_fixture();lat=result_fixture();lat['stress']['cash']=9999
        self.assertIn('LATENCY_SENSITIVITY',economic_decision(c,c,lat,0)['failures'])
        c=result_fixture();next(r for r in c['daily'] if r[0]==182*1440)[2]=9900
        self.assertIn('QUARTER_STABILITY',economic_decision(c,c,c,0)['failures'])

    def test_concentration_rejected(self):
        c=result_fixture();c['stress']['cycles'][0]['net']=10000
        self.assertEqual(economic_decision(c,c,c,0)['status'],'INSUFFICIENT_EVENT_DIVERSITY')

    def test_week_tail_and_full_year_retained(self):
        c=result_fixture();v=metrics(c)['stress']
        self.assertEqual(len(v['weekly']),39)
        self.assertAlmostEqual(sum(v['weekly'])+v['tail_net'],v['net'])
        self.assertAlmostEqual(sum(v['quarter_net']),v['net'])

    def test_invalid_variance_never_zero_wait(self):
        self.assertIsNone(long_variance([0]*39,4))
        c=result_fixture();d=economic_decision(c,c,c,0)
        self.assertEqual(confirmation_plan(d)['status'],'NO_AFFORDABLE_CONFIRMATION_EXIT')

    def test_unsupported_has_no_observation_plan(self):
        self.assertEqual(confirmation_plan({'status':'NO_GO_ECONOMIC'})['status'],'NOT_JUSTIFIED_BY_DEVELOPMENT')

    def test_stopped_sharpe_not_credible(self):
        self.assertIsNone(sharpe([10000+i for i in range(50)],True)['naive'])

    def test_independent_full_audit_and_mutation(self):
        a=PersistentSignalArchive()
        for delay in (0,1):
            s=Simulation(a,0,60*60000,[2,24],delay).run_to(60)
            snapshot=json.loads(json.dumps(s.snapshot()))
            audited=audit_snapshot(a,snapshot)
            bind_result(snapshot,audited,s.result())
            self.assertEqual(audited[0]['completed_episodes'],s.paths[0].episodes)
            for field in ('cash','fees','slippage','dd'):
                mutated=copy.deepcopy(snapshot);mutated['paths'][0][field]=float(mutated['paths'][0][field])+1
                with self.assertRaises(ValueError):audit_snapshot(a,mutated)

    def test_summary_cannot_replace_audited_book(self):
        a=PersistentSignalArchive();s=Simulation(a,0,60*60000,[2,24]).run_to(60)
        snapshot=json.loads(json.dumps(s.snapshot()));audited=audit_snapshot(a,snapshot)
        altered=copy.deepcopy(s.result());altered['stress']['cash']+=1
        with self.assertRaisesRegex(ValueError,'SUMMARY_MONEY'):bind_result(snapshot,audited,altered)

    def test_independent_decision_matches_predeclared_cases(self):
        c=result_fixture();b=result_fixture();lat=result_fixture()
        cases=[(c,b,lat,0)]
        for field,value in (('cash',9999),('stop','RESEARCH_DRAWDOWN_8_PERCENT'),('insufficient','RESIDUAL'),('cycles',[])):
            x=copy.deepcopy(c);x['stress'][field]=value;cases.append((x,b,lat,0))
        cases.append((c,b,lat,1))  # No gain from selecting a nonbaseline vector.
        for case in cases:
            self.assertEqual(independent_verdict(*case),economic_decision(*case)['status'])

    def test_independent_selection_matches_negative_and_ties(self):
        def r(score,turnover,stop=None):
            return dict(last_liquidation=[10000+score]*2,
                        base=dict(stop=stop,insufficient=None,traded_notional=turnover),
                        stress=dict(stop=stop,insufficient=None,traded_notional=turnover))
        for rows in ([r(-3,10),r(-2,20)], [r(-2,100),r(-2,10)],
                     [r(-10,10),r(-1,20),r(-1,10)], [r(5,10,'RISK')]):
            self.assertEqual(independent_selection(rows),select(rows))

    def test_independent_report_math_and_mutation(self):
        c=result_fixture();b=result_fixture();d=economic_decision(c,b,c,0)
        p=confirmation_plan(d);results=dict(candidate=c,baseline=b,latency=c)
        audit_report_math(d,p,results)
        altered=copy.deepcopy(d);altered['candidate']['stress']['tail_net']+=1
        with self.assertRaisesRegex(ValueError,'REPORTED_WEEK_AND_TAIL'):audit_report_math(altered,p,results)

    def test_independent_conditional_horizon(self):
        c=result_fixture()
        for i,row in enumerate(c['daily']):
            row[1]+=math.sin(i/9)*2;row[2]+=math.sin(i/7)*2
        c['base']['cash']=c['daily'][-1][1];c['stress']['cash']=c['daily'][-1][2]
        d=economic_decision(c,c,c,0);self.assertEqual(d['status'],'DEVELOPMENT_SUPPORTED')
        p=confirmation_plan(d);audit_report_math(d,p,dict(candidate=c,baseline=c,latency=c))
        altered=copy.deepcopy(p);altered['cases'][0]['weeks']+=1
        with self.assertRaisesRegex(ValueError,'HORIZON_ARITHMETIC'):audit_report_math(d,altered,dict(candidate=c,baseline=c,latency=c))

    def test_artifact_writes_are_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'evidence.json';batch.save(p,{'first':True})
            with self.assertRaises(FileExistsError):batch.save(p,{'second':True})
            self.assertEqual(batch.read(p),{'first':True})

    def test_second_market_attempt_is_rejected_before_simulation(self):
        with tempfile.TemporaryDirectory() as d, patch.object(batch,'ROOT',Path(d)), \
             patch.object(batch,'guard',return_value={}),patch.object(batch,'preserve'), \
             patch.object(batch,'verify_model'),patch.object(batch,'resources'):
            batch.save(Path(d)/'market-attempt.json',{'consumed':True})
            with self.assertRaisesRegex(ValueError,'ONE_MARKET_ATTEMPT_ALREADY_CONSUMED'):batch.market()

    def test_frozen_source_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as d,patch.object(batch,'ROOT',Path(d)):
            p=Path(d)/'source.json';batch.save(p,{'original':True})
            batch.save(Path(d)/'model-freeze.json',{'sources':{str(p):'0'*64}})
            with self.assertRaisesRegex(ValueError,'FROZEN_MODEL_SOURCE_CHANGED'):batch.verify_model()

    def test_independent_checkpoint_and_continuation(self):
        a=PersistentSignalArchive();s=Simulation(a,0,200*60000,[2,24]).run_to(10)
        audit_snapshot(a,json.loads(json.dumps(s.snapshot())))
        r=Simulation.restore(a,json.loads(json.dumps(s.snapshot()))).run_to(200)
        audit_snapshot(a,json.loads(json.dumps(r.snapshot())))

    def test_independent_no_missing_signal(self):
        a=PersistentSignalArchive();s=Simulation(a,0,60*60000,[2,24]).run_to(60)
        snapshot=json.loads(json.dumps(s.snapshot()))
        snapshot['paths'][0]['events']=[e for e in snapshot['paths'][0]['events'] if e['kind']!='entry_requested']
        with self.assertRaisesRegex(ValueError,'CAUSAL_ENTRY'):
            audit_snapshot(a,snapshot)

    def test_independent_estimation_and_no_signal(self):
        self.assertIsNone(independent_ols(NoSignalArchive.warmup))
        a=NoSignalArchive();s=Simulation(a,0,1500*60000,[2,24]).run_to(1500)
        self.assertEqual(audit_snapshot(a,json.loads(json.dumps(s.snapshot())))[0]['fills'],0)

    def test_independent_adverse_path_alert_not_exit(self):
        class Adverse(PersistentSignalArchive):
            def bars(self,t,kind):
                eth=100-max(0,t/60000)*.0005
                return [[price]*4+([100000] if kind=='trade' else []) for price in (eth,100)]
        a=Adverse();s=Simulation(a,0,2700*60000,[2,24]).run_to(2700)
        audited=audit_snapshot(a,json.loads(json.dumps(s.snapshot())))
        self.assertIsNotNone(audited[0]['first_legacy_withdrawal']);self.assertIsNone(s.paths[0].stopped)

    def test_independent_funding_and_hard_risk(self):
        class Shock(PersistentSignalArchive):
            def bars(self,t,kind):
                # Joint +80% is hedged and leaves ~11% short-leg cushion;
                # +100% actually breaches the unchanged 8% distance rule.
                price=200 if t>=10*60000 else 100
                return [[price]*4+([100000] if kind=='trade' else []) for _ in (0,1)]
            def rates(self,t):return {0:.001,1:-.001} if t==3*60000 else {}
        a=Shock();s=Simulation(a,0,60*60000,[2,24]).run_to(60)
        audited=audit_snapshot(a,json.loads(json.dumps(s.snapshot())))
        self.assertTrue(s.paths[0].stopped);self.assertTrue(audited[0]['stop_events'])

    def test_independent_partial_entry(self):
        class Thin(PersistentSignalArchive):
            def bars(self,t,kind):
                return [[100]*4+([10 if t==60000 else 100000] if kind=='trade' else []) for _ in (0,1)]
        a=Thin();s=Simulation(a,0,60*60000,[2,24]).run_to(60)
        audit_snapshot(a,json.loads(json.dumps(s.snapshot())))


if __name__=='__main__':unittest.main()
