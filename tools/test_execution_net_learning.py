"""Synthetic-only execution objective, chronology and stop-boundary regressions."""
import copy
import unittest
from execution_net_learning import ExecutionLedger,LearningWindow,Safety,target
from audit_execution_net_learning import Ledger,close
from decimal import Decimal as D
import run_execution_net_learning as runner

def row(i=0,price=10000,close_price=None,rate=0):
    cl=price if close_price is None else close_price
    return dict(timestamp=str(1704067200000+i*300000),open=str(price),price=str(cl),
                mark_open=str(price),mark_close=str(cl),mark_high=str(max(price,cl)),
                mark_low=str(min(price,cl)),funding_rate_per_interval=str(rate))

def rising(i):
    return row(i,10000+i*.7,10000+(i+1)*.7+(i%3)*.1)

def signal(bucket=1):return dict(bucket=bucket,trend=2000,defensive=0)

class ExecutionNetTest(unittest.TestCase):
    def test_unchanged_target_does_not_price_rebalance(self):
        b=ExecutionLedger();b.step(row(),1000);q=b.wallet.qty
        for i in range(1,10):b.step(row(i,10000+i*20),1000)
        self.assertEqual(b.wallet.fills,1);self.assertEqual(b.wallet.qty,q)

    def test_cap_reduction_not_discretionary_top_up(self):
        b=ExecutionLedger();b.step(row(),2500);q=b.wallet.qty
        b.step(row(1,10100),2500)
        self.assertLess(b.wallet.qty,q);self.assertLessEqual(b.wallet.qty*10100,2500)
        fills=b.wallet.fills;b.step(row(2,10000),2500)
        self.assertEqual(b.wallet.fills,fills)

    def test_mark_open_cap_is_respected(self):
        b=ExecutionLedger();r=row();r['mark_open']='10500';b.step(r,2500)
        self.assertLessEqual(b.wallet.qty*10500,2500)

    def test_funding_old_holder_and_short_credit(self):
        b=ExecutionLedger();b.step(row(rate=.01),-1000)
        self.assertEqual(b.wallet.funding,0)
        b.step(row(1,rate=.01),0)
        self.assertAlmostEqual(b.wallet.funding,-10)

    def test_open_funding_risk_prevents_new_order(self):
        b=ExecutionLedger();b.step(row(),2500);fills=b.wallet.fills
        b.step(row(1,rate=1),-2500)
        self.assertGreater(b.wallet.drawdown,.08);self.assertEqual(b.wallet.fills,fills)

    def test_intrabar_risk_cannot_disappear_at_close(self):
        b=ExecutionLedger();r=row();r['mark_low']='5000';b.step(r,2500)
        self.assertGreater(b.wallet.drawdown,.08)

    def test_close_reserve_not_double_charged(self):
        b=ExecutionLedger();receipt=b.step(row(),1000);cash=b.wallet.cash
        reserve=b.liquidation(10000,10000)
        self.assertEqual(b.wallet.cash,cash)
        b.settle(row(),False)
        self.assertAlmostEqual(b.wallet.cash,reserve)
        self.assertAlmostEqual(receipt,b.wallet.cash-10000)

    def test_shared_actual_and_current_candidate_ledgers(self):
        a=ExecutionLedger();s=ExecutionLedger(2);a.step(row(),1000);s.step(row(),1000)
        w=LearningWindow([.5]*3,a,s)
        for i in range(12):
            r=rising(i);w.step(r,signal());a.step(r,target(signal(),[.5]*3));s.step(r,1000)
        for actual,shadow in zip((a,s),w.books['current']):
            self.assertEqual(actual.snapshot(10000),shadow.snapshot(10000))
        self.assertEqual(w.books['current'][0].wallet.fills,1)

    def test_decimal_long_short_funding_tick_and_stress(self):
        for m in (1,2):
            b=ExecutionLedger(m);d=Ledger(m==2)
            for i,t in enumerate((1100,1100,-1250,-1250,0,1000,0)):
                r=row(i,10000+i*.73,10001+i*.57,.0001)
                receipt=b.step(r,t);delta=d.step(r,D(t))
                close(receipt,delta,'test_receipt');d.check(b.snapshot(float(r['mark_close'])),D(r['mark_close']))

    def test_genuine_positive_net_can_update(self):
        w=LearningWindow([.5]*3,ExecutionLedger(),ExecutionLedger(2))
        for i in range(240):w.step(rising(i),signal())
        result=w.finish()
        self.assertTrue(result['updated'],result)
        self.assertEqual(result['weights_after'],[.5,.55,.5])
        self.assertTrue(all(x>0 for x in result['scores'][result['locked']].values()))

    def test_holdout_cannot_reselect_train_winner(self):
        w=LearningWindow([.5]*3,ExecutionLedger(),ExecutionLedger(2))
        for i in range(168):w.step(rising(i),signal())
        z=copy.deepcopy(w);locked=w.locked
        for i in range(168,240):
            w.step(rising(i),signal());z.step(row(i,10118-(i-168),10117-(i-168)),signal())
        self.assertEqual(w.locked,locked);self.assertEqual(z.locked,locked)
        self.assertTrue(w.finish()['updated']);self.assertFalse(z.finish()['updated'])
        self.assertEqual(z.finish()['weights_after'],[.5]*3)

    def test_flat_or_empty_observations_do_not_fill_capacity(self):
        for s in (None,dict(bucket=1,trend=0,defensive=0)):
            w=LearningWindow([.5]*3,ExecutionLedger(),ExecutionLedger(2))
            for i in range(240):w.step(row(i),s)
            result=w.finish();self.assertEqual(result['counts'],[[0,0]]*3)
            self.assertEqual(result['reason'],'CAPACITY_INSUFFICIENT')

    def test_unreachable_bucket_not_chosen_before_holdout(self):
        w=LearningWindow([.5]*3,ExecutionLedger(),ExecutionLedger(2))
        for i in range(168):w.step(rising(i),signal(0 if i>=150 else 1))
        self.assertTrue(w.locked.startswith('1:'))

    def test_holdout_capacity_missing_freezes(self):
        w=LearningWindow([.5]*3,ExecutionLedger(),ExecutionLedger(2))
        for i in range(240):w.step(rising(i),signal(1 if i<168 else 0))
        self.assertEqual(w.finish()['reason'],'CAPACITY_INSUFFICIENT')

    def test_less_loss_never_becomes_positive_edge(self):
        w=LearningWindow([.5]*3,ExecutionLedger(),ExecutionLedger(2))
        for i in range(240):w.step(row(i,10000-i*.7,9999.3-i*.7),signal())
        self.assertFalse(w.finish()['updated']);self.assertEqual(w.finish()['reason'],'NO_ABSOLUTE_NET_EDGE')

    def test_candidate_risk_cannot_be_promoted(self):
        w=LearningWindow([.5]*3,ExecutionLedger(),ExecutionLedger(2))
        for i in range(240):w.step(rising(i),signal())
        w.books[w.locked][0].wallet.drawdown=.08
        self.assertEqual(w.finish()['reason'],'CANDIDATE_REFERENCE_RISK')

    def test_safety_two_losses_and_irreversible_latch(self):
        s=Safety()
        for k in range(2):
            for i in range(240):s.observe(-.01,1)
            self.assertEqual(s.assess()['withdrawn'],k==1)
        with self.assertRaisesRegex(ValueError,'SAFETY_LATCHED'):s.observe(1,1)

    def test_safety_bucket_loss_even_if_total_positive(self):
        s=Safety()
        for k in range(2):
            for i in range(240):s.observe(-1 if i%2 else 2,i%2)
            result=s.assess()
        self.assertTrue(result['withdrawn']);self.assertGreater(result['net_by_decision_bucket_and_total'][3],0)

    def test_confirmation_and_off_grid_forbidden(self):
        with self.assertRaisesRegex(ValueError,'CONFIRMATION'):
            LearningWindow([.5]*3,ExecutionLedger(),ExecutionLedger(2),domain='confirmation')
        with self.assertRaisesRegex(ValueError,'WEIGHT_GRID'):
            LearningWindow([.56]*3,ExecutionLedger(),ExecutionLedger(2))

    def test_incomplete_or_reopened_window_rejected(self):
        w=LearningWindow([.5]*3,ExecutionLedger(),ExecutionLedger(2))
        with self.assertRaisesRegex(ValueError,'INCOMPLETE'):w.finish()
        for i in range(240):w.step(row(i),None)
        with self.assertRaisesRegex(ValueError,'CLOSED'):w.step(row(),None)
        with self.assertRaisesRegex(ValueError,'LOCK_TIME'):w.lock_train(1)

    def test_development_verdict_needs_absolute_and_unit_edge(self):
        books={k:ExecutionLedger() for k in runner.ARMS}
        for b in books.values():b.wallet.cash=10010
        weeks=[dict(fixed=1,adaptive=2,adaptive_stress=1,fixed_unit=1,adaptive_unit=1)]*26
        self.assertEqual(runner.evaluate('COMPLETE',books,1,weeks)[0],'INSUFFICIENT_DEVELOPMENT_EVIDENCE')
        self.assertEqual(runner.evaluate('SAFETY_WITHDRAWAL',books,1,weeks)[0],'NO_GO_SAFETY_WITHDRAWAL')
        self.assertIsNone(runner.evaluate('COMPLETE',books,1,weeks[:25])[1]['paired'])
        for w in weeks:w['adaptive_unit']=2
        self.assertEqual(runner.evaluate('COMPLETE',books,1,weeks)[0],'DEVELOPMENT_SUPPORTED_CONFIRMATION_UNAVAILABLE')
        books['adaptive_stress'].wallet.cash=9999
        self.assertEqual(runner.evaluate('COMPLETE',books,1,weeks)[0],'INSUFFICIENT_DEVELOPMENT_EVIDENCE')

if __name__=='__main__':unittest.main()
