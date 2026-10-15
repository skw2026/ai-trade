from decimal import Decimal
import math
import unittest

from eth_sol_model import PairLedger, execution_price, estimate, floor_quantity, score


def bars(price=100,volume=100000):return [[price,price,price,price,volume] for _ in (0,1)]


def open_pair(stress=False):
    p=PairLedger(stress)
    model=(0,1,.01)
    p.decision(0,[103,100],model,10000)
    p.process(1,bars());p.process(2,bars())
    return p


class ModelTests(unittest.TestCase):
    def test_sequential_no_immediate_fill(self):
        p=PairLedger();p.decision(0,[103,100],(0,1,.01),10000)
        self.assertTrue(p.flat);p.process(1,bars())
        self.assertLess(p.q[0],0);self.assertEqual(p.q[1],0)
        p.process(2,bars());self.assertGreater(p.q[1],0);self.assertTrue(p.complete)

    def test_no_entry_below_threshold(self):
        p=PairLedger();p.decision(0,[100,100],(0,1,.01),1000)
        self.assertFalse(p.queue)

    def test_price_adversity(self):
        b=[100,101,99,100,1000]
        self.assertGreater(execution_price(b,1,True),execution_price(b,1,False))
        self.assertLess(execution_price(b,-1,True),execution_price(b,-1,False))
        self.assertEqual(execution_price(b,1),100.02)

    def test_quantity_step(self):
        self.assertEqual(floor_quantity(.199,.1),.1)
        self.assertEqual(floor_quantity(.199,.01),.19)

    def test_partial_first_cancels_other_unwinds(self):
        p=PairLedger();p.decision(0,[103,100],(0,1,.01),10000)
        p.process(1,bars(volume=10));self.assertEqual(p.failed_entries,1)
        self.assertEqual(p.q[1],0);self.assertTrue(p.exiting)
        p.process(2,bars());self.assertTrue(p.flat);self.assertEqual(p.episodes,0)
        self.assertLess(p.cash,10000)

    def test_second_failure_reverses_first_with_fees(self):
        p=PairLedger();p.decision(0,[103,100],(0,1,.01),10000);p.process(1,bars())
        p.process(2,bars(volume=0));p.process(3,bars())
        self.assertTrue(p.flat);self.assertEqual(len([e for e in p.events if e['kind']=='fill']),2)
        self.assertGreater(p.fees,0);self.assertEqual(p.episodes,0)

    def test_exit_is_sequential_and_complete_episode(self):
        p=open_pair();p.begin_exit(3,'test');p.process(4,bars())
        self.assertEqual(p.q[0],0);self.assertGreater(p.q[1],0)
        p.process(5,bars());self.assertTrue(p.flat);self.assertEqual(p.episodes,1)

    def test_unfilled_exit_retains_liability(self):
        p=open_pair();old=list(p.q);p.begin_exit(3,'test')
        for minute in range(4,10):p.process(minute,bars(volume=0))
        self.assertEqual(p.insufficient,'RESIDUAL_EXPOSURE_UNCLOSED');self.assertEqual(p.q,old)

    def test_funding_before_fill_and_sign(self):
        p=PairLedger();p.decision(0,[103,100],(0,1,.01),10000)
        p.fund(1,{0:.001},[100,100]);self.assertEqual(p.funding,0)
        p.process(1,bars());p.fund(2,{0:.001},[100,100])
        self.assertLess(p.funding,0)
        p.process(2,bars());before=p.funding;p.fund(3,{1:.001},[100,100]);self.assertGreater(p.funding,before)

    def test_decimal_cash_replay(self):
        p=open_pair(True);p.fund(3,{0:.0001,1:-.0002},[100,100]);p.begin_exit(4,'test')
        p.process(5,bars(101));p.process(6,bars(99))
        cash=Decimal(10000);q=[Decimal(0),Decimal(0)]
        for e in p.events:
            if e['kind']=='fill':
                d,price,fee=map(lambda k:Decimal(str(e[k])),('delta','price','fee'))
                q[e['leg']]+=d;cash-=d*price+fee
            elif e['kind']=='funding':
                cash-=q[e['leg']]*Decimal(str(e['mark']))*Decimal(str(e['rate']))
        self.assertEqual(q,[0,0]);self.assertAlmostEqual(float(cash),p.cash,places=8)

    def test_model_frozen_until_exit(self):
        p=open_pair();frozen=p.model
        p.decision(5,[103,100],(10,2,.01),10000)
        self.assertEqual(p.model,frozen);self.assertFalse(p.exiting)

    def test_residual_and_horizon_exits(self):
        p=open_pair();p.decision(5,[100,100],(0,1,.01),10000);self.assertTrue(p.exiting)
        p=open_pair();p.decision(1441,[103,100],(0,1,.01),10000);self.assertTrue(p.exiting)

    def test_terminal_no_new_entry_and_close(self):
        p=PairLedger();p.decision(90,[103,100],(0,1,.01),100);self.assertFalse(p.queue)
        p=open_pair();p.decision(95,[103,100],(0,1,.01),100);self.assertTrue(p.exiting)

    def test_safety_two_negative_windows_permanent(self):
        p=PairLedger()
        for i in range(480):
            p.cash-=.01;p.close_minute((i+1)*5,[100,100],True)
        self.assertEqual(p.stopped,'SAFETY_WITHDRAWAL');self.assertTrue(p.safety.withdrawn)
        p.decision(2401,[103,100],(0,1,.01),10000);self.assertFalse(p.queue)

    def test_drawdown_and_no_future_extreme_cancels_scheduled_fill(self):
        p=open_pair();p.peak=10000;p.cash-=1000
        before=list(p.q);reasons=p.risk(3,[[100,100,100,100]]*2,defer=True)
        self.assertIn('RESEARCH_DRAWDOWN_8_PERCENT',reasons);self.assertIsNone(p.stopped)
        self.assertEqual(p.q,before)
        for reason in reasons:p.stop(3,reason)
        self.assertTrue(p.exiting)

    def test_invalid_bars_rejected(self):
        for b in [[0,1,0,1,10],[100,99,98,100,10],[100,100,100,100,-1]]:
            with self.assertRaises(ValueError):execution_price(b,1)

    def test_isolated_distance_not_hidden_by_total_equity(self):
        p=PairLedger();p.q=[-1,0];p.cash=10100;p.collateral=[100,0];p.leg_cash=[100,0]
        p.risk(0,[[190,190,190,190],[100,100,100,100]])
        self.assertLess(p.dd,.08)
        self.assertEqual(p.stopped,'REFERENCE_LIQUIDATION_DISTANCE_UNDER_8_PERCENT')

    def test_estimator_and_degenerate(self):
        hours=[(math.exp(1+1.2*(3+i*.0001)+.001*math.sin(i)),math.exp(3+i*.0001)) for i in range(1440)]
        model=estimate(hours);self.assertIsNotNone(model);self.assertAlmostEqual(model[1],1.2,places=3)
        self.assertIsNone(estimate([(100,100)]*1440))
        with self.assertRaises(ValueError):estimate(hours[:-1])
        self.assertTrue(math.isfinite(score(model,hours[-1])))


if __name__=='__main__':unittest.main()
