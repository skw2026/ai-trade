"""Fixed synthetic controls. A PASS is engineering evidence, never market alpha."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import unittest

from execution_net_learning import TRAIN, WINDOW, target
from native_execution_reward import NativeLedger, NativeSession, NativeLearningWindow as LearningWindow

DRIVER = None
RESULTS = {}


def row(i=0, opening=10000, closing=None, rate=0):
    closing = opening if closing is None else closing
    return dict(_synthetic=True, timestamp=1000000+i*300000, open=opening, price=closing,
                mark_open=opening, mark_close=closing, mark_high=max(opening, closing),
                mark_low=min(opening, closing), funding_rate_per_interval=rate)


def fixture(i, mode):
    """Predeclared deterministic equations, without seeds or parameter selection."""
    sign = 1 if i % 2 == 0 else -1
    signal = dict(bucket=1, trend=sign*1000, defensive=-sign*1100)
    opening = 10000 if sign > 0 else 10100
    closing = 10100 if sign > 0 else 10000
    if mode == 'cost':
        opening = closing = 10000
    elif mode == 'reverse' and i >= TRAIN:
        opening, closing = closing, opening
    elif mode == 'scale':
        opening, closing = 10000+i*.7, 10000+(i+1)*.7+(i % 3)*.1
        signal = dict(bucket=1, trend=2000, defensive=0)
    # A constant funding rate on alternating carried positions supplies variance.
    # It is NOT exactly zero-sum: different marks leave a small net funding cost.
    # Direction, price cycle, cost model, grid and statistical threshold are fixed.
    # Keep the original constant-return case separately as a rejection control.
    rate = .0001 if mode in ('positive', 'reverse') else 0
    return row(i, opening, closing, rate), signal


def unit_target(signal, weights):
    desired = target(signal, weights)
    return math.copysign(100, desired) if abs(desired) > 1e-8 else 0


def learning_control(driver, mode):
    with NativeSession(driver) as session:
        base, stress = NativeLedger(session), NativeLedger(session, 2)
        weights = [.5]*3
        window = LearningWindow(weights, base, stress)
        prefix = hashlib.sha256()
        units = None
        locked = None
        for i in range(WINDOW):
            r, signal = fixture(i, mode)
            if i < TRAIN:
                prefix.update(json.dumps([r, signal], sort_keys=True).encode())
            window.step(r, signal)
            base.bucket = stress.bucket = signal['bucket']
            base.step(r, target(signal, weights))
            stress.step(r, target(signal, weights))
            # Compare actual/current candidate native accounts at every observation.
            for actual, shadow in zip((base, stress), window.books['current']):
                for key in ('qty', 'margin_cash', 'liquidation', 'fees', 'funding', 'drawdown', 'fills'):
                    assert abs(actual.state[key]-shadow.state[key]) < 5e-8, (i, key)
            if i == TRAIN-1:
                locked = window.locked
                assert locked is not None
                # Both equal-notional controls inherit the same actual carried position.
                units = [(b.copy(), b.copy()) for b in (base, stress)]
                unit_receipts = [[], []]
                chosen = window.vectors[locked]
            elif i >= TRAIN:
                assert window.locked == locked
                for j, (selected, fixed) in enumerate(units):
                    a = selected.step(r, unit_target(signal, chosen))
                    b = fixed.step(r, unit_target(signal, weights))
                    unit_receipts[j].append(a-b)
            assert base.version == stress.version == 0
        result = window.finish()
        unit_gain = [sum(x) for x in unit_receipts]
        directional = result['updated'] and min(unit_gain) > 0
        # This classifier is only a synthetic diagnostic, not a promotion authority.
        classification = ('DIRECTIONAL_EXECUTION_GAIN' if directional else
                          'EXPOSURE_SCALING_ONLY' if result['updated'] and max(abs(x) for x in unit_gain) < 1e-8
                          else 'NO_ACCEPTED_UPDATE')
        out = dict(mode=mode, input_prefix_sha256=prefix.hexdigest(), locked=locked,
                   result=result, unit_net_gain=unit_gain, classification=classification,
                   applied_before_window_close=False)
        if mode == 'positive':
            assert directional, out
            fixed = [b.copy() for b in (base, stress)]
            for b in (base, stress):
                b.version = 1
            next_receipts = [[], []]
            for i in range(WINDOW, 2*WINDOW):
                r, signal = fixture(i, mode)
                for j, (actual, comparison) in enumerate(zip((base, stress), fixed)):
                    actual.bucket = comparison.bucket = signal['bucket']
                    gain = actual.step(r, target(signal, result['weights_after']))
                    control = comparison.step(r, target(signal, weights))
                    next_receipts[j].append(gain-control)
            out['next_window_net_gain'] = [sum(x) for x in next_receipts]
            assert min(out['next_window_net_gain']) > 0
            assert result['weights_after'] == [.5, .55, .5]
        elif mode in ('cost', 'reverse', 'degenerate'):
            assert not result['updated'], out
            assert result['weights_after'] == weights
            if mode == 'degenerate':
                assert result['net_t'] == 0 and min(result['scores'][locked].values()) > 0
                assert out['input_prefix_sha256'] == '7e66da4f4751f4fefcedafa1e9bb2182c5393e5f575e090bb5e83c59388b1641'
        elif mode == 'scale':
            assert result['updated'] and classification == 'EXPOSURE_SCALING_ONLY', out
        out.update(audited_receipts=session.audit_count, journal_sha256=session.chain,
                   source='NATIVE_ACCOUNT_OMS_RISK_EXECUTION', synthetic_only=True)
        return out


class NativeRewardTest(unittest.TestCase):
    def setUp(self):
        self.session = NativeSession(DRIVER)
        self.addCleanup(self.session.close)
        self.ledger = NativeLedger(self.session)

    def event(self, kind, *args, **kwargs):
        return self.session.event(self.ledger.book, kind, *args, **kwargs)

    def order(self, target_value=1000, **kwargs):
        return self.event('TARGET', target_value, order=self.session.identity('o'), **kwargs)['intent']

    def test_driver_requires_explicit_synthetic_mode(self):
        result = subprocess.run([DRIVER], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)

    def test_long_short_add_reduce_flip_funding_and_stress(self):
        for multiplier in (1, 2):
            b = NativeLedger(self.session, multiplier)
            for i, desired in enumerate((1000, 1800, 500, -1200, -1800, -500, 0)):
                b.step(row(i, 10000+i*7, 10002+i*7, .0001), desired)
            self.assertEqual(b.state['qty'], 0)
            self.assertGreater(b.state['fees'], 0)
            self.assertGreater(b.state['fills'], 7)  # close then open, not a single flip

    def test_first_entry_and_exit_reserve_not_double_debit(self):
        b = self.ledger
        reward = b.step(row(), 1000)
        self.assertLess(reward, 0)
        self.assertGreater(b.state['exit_reserve'], 0)
        before = b.state['liquidation']
        b.settle(row())
        self.assertEqual(b.state['qty'], 0)
        self.assertAlmostEqual(b.state['margin_cash'], before, places=8)
        self.assertAlmostEqual(reward, b.state['margin_cash']-10000, places=8)

    def test_bucket_weight_switch_records_real_cost_and_prior_position(self):
        b = self.ledger
        b.step(row(), 1000)
        fees = b.state['fees']
        equity, reserve = b.state['equity'], b.state['exit_reserve']
        b.bucket, b.version = 2, 7
        delta = b.step(row(1, 10000), 500)
        # This reduction realizes an already reserved exit: no second deduction.
        self.assertAlmostEqual(delta, 0, places=8)
        self.assertGreater(b.state['fees'], fees)
        self.assertAlmostEqual(equity-b.state['equity'], reserve-b.state['exit_reserve'], places=8)
        old_q = b.state['qty']
        b.mark(10100, 10100)
        self.assertAlmostEqual(b.state['equity'] - (b.state['margin_cash'] + old_q*(10000-b.state['average'])), old_q*100)

    def test_no_fill_has_no_price_profit(self):
        order = self.order()
        self.event('REJECT', order=order['id'])
        self.ledger.mark(11000, 11000)
        self.assertEqual(self.ledger.state['liquidation'], 10000)
        self.assertEqual(self.ledger.state['fills'], 0)

    def test_target_unchanged_no_rebalance_but_cap_reduces(self):
        b = self.ledger
        b.step(row(), 1000)
        fills = b.state['fills']
        b.step(row(1, 10100), 1000)
        self.assertEqual(b.state['fills'], fills)
        b.step(row(2), 2500)
        b.step(row(3, 10500), 2500)
        self.assertLessEqual(abs(b.state['qty'])*10500, 2500)
        qty, fills = b.state['qty'], b.state['fills']
        b.step(row(4), 2500)
        self.assertEqual((b.state['qty'], b.state['fills']), (qty, fills))

    def test_mark_quote_basis_cap(self):
        r = row()
        r['mark_open'] = r['mark_high'] = r['mark_low'] = r['mark_close'] = 10500
        self.ledger.step(r, 2500)
        self.assertLessEqual(self.ledger.state['qty']*10500, 2500)

    def test_funding_old_holder_short_receives(self):
        b = self.ledger
        b.step(row(rate=.01), -1000)
        self.assertEqual(b.state['funding'], 0)
        b.step(row(1, rate=.01), 0)
        self.assertAlmostEqual(b.state['funding'], -10)

    def test_partial_cancel_ack_late_fill_keeps_origin_version(self):
        order = self.order(version=3, bucket=2)
        self.event('FILL', .04, 10001, .220022, 10000, order=order['id'], fill='part', side=1)
        self.assertEqual(self.ledger.state['pending'], 1)
        self.assertIsNone(self.order(1500))
        self.event('CANCEL', order=order['id'])
        self.event('ACK', order=order['id'])
        self.event('LOCK')
        result = self.event('FILL', .06, 10001, .330033, 10000,
                            order=order['id'], fill='late', side=1, version=9, bucket=0)
        self.assertEqual((result['decision_version'], result['decision_bucket']), ('3', '2'))
        self.assertEqual(result['pending'], 0)
        self.assertAlmostEqual(result['qty'], .1)
        self.assertIsNone(self.order(2000))
        self.assertTrue(self.order(0)['reduce_only'])

    def test_event_and_fill_duplicates_and_conflicts(self):
        order = self.order()
        args = dict(order=order['id'], fill='f1', side=1, eid='event1', timestamp=100)
        self.event('FILL', .1, 10001, .550055, 10000, **args)
        same = self.event('FILL', .1, 10001, .550055, 10000, **args)
        self.assertTrue(same['duplicate'])
        self.assertEqual(same['reward'], 0)
        self.event('FILL', .1, 10002, .550055, 10000, expect_error=True, **args)
        args.update(eid='redelivery', timestamp=0)
        result = self.event('FILL', .1, 10001, .550055, 10000, **args)
        self.assertTrue(result['duplicate'])
        args.update(eid='bad-redelivery')
        self.event('FILL', .1, 10001, .6, 10000, expect_error=True, **args)
        self.assertEqual(self.ledger.state['fills'], 1)

    def test_invalid_and_old_event_rejected_without_state_change(self):
        self.ledger.mark(10000, 10000)
        for price in (0, -1, float('nan'), float('inf')):
            self.event('MARK', price, 10000, expect_error=True)
        self.event('MARK', 10001, 10001, timestamp=0, expect_error=True)
        self.event('FILL', .1, 10000, 0, 10000, order='missing', fill='x', side=1, expect_error=True)
        self.event('TARGET', float('nan'), order='x', expect_error=True)
        self.event('UNLOCK', expect_error=True)
        self.event('MARK', 10000, 10000, bucket=3, expect_error=True)
        self.assertEqual(self.ledger.state['liquidation'], 10000)

    def test_overfill_wrong_side_and_rejected_fill_rejected(self):
        order = self.order()
        for qty, side in ((.2, 1), (.1, -1), (.0001, 1)):
            self.event('FILL', qty, 10000, 0, 10000, order=order['id'], fill=self.session.identity('f'), side=side, expect_error=True)
        self.event('ACK', order=order['id'], expect_error=True)
        self.event('REJECT', order=order['id'])
        self.event('FILL', .1, 10000, 0, 10000, order=order['id'], fill='rejected', side=1, expect_error=True)

    def test_clone_keeps_position_peak_lock_pending_and_dedup(self):
        b = self.ledger
        b.step(row(), 1000)
        self.event('FUND', .001, eid='fund-once', timestamp=b.state['time']+1)
        old_time = b.state['time']
        b.mark(10500, 10500)
        order = self.order(0)
        self.event('LOCK')
        clone = b.copy()
        self.assertEqual(clone.last_target, b.last_target)
        for key in ('qty', 'peak', 'drawdown', 'manual_lock', 'pending', 'funding', 'fees', 'events'):
            self.assertEqual(clone.state[key], b.state[key])
        result = self.session.event(clone.book, 'FUND', .001, eid='fund-once', timestamp=old_time)
        self.assertTrue(result['duplicate'])
        clone.full_fill(order)
        self.assertEqual(clone.state['qty'], 0)
        self.assertNotEqual(b.state['qty'], 0)

    def test_restart_retains_pending_dedup_funding_and_risk_latch(self):
        b = self.ledger
        b.step(row(), 2500)
        self.event('FUND', .4, eid='fund-risk', timestamp=b.state['time']+1)
        fund_time = b.state['time']
        self.assertTrue(b.state['hard_lock'])
        b.mark(20000, 20000)  # price recovery cannot clear lifetime stop
        self.assertTrue(b.state['hard_lock'])
        order = self.order(0)
        before = dict(b.state)
        self.session.restart()
        for key in ('qty', 'peak', 'drawdown', 'hard_lock', 'pending', 'fees', 'funding'):
            self.assertEqual(b.state[key], before[key])
        self.event('FUND', .4, eid='fund-risk', timestamp=fund_time)
        self.assertEqual(b.state['funding'], before['funding'])
        b.full_fill(order)
        self.assertEqual(b.state['qty'], 0)
        self.assertIsNone(self.order(-2500))

    def test_intrabar_risk_keeps_marking_and_reduces_next_event(self):
        r = row()
        r['mark_low'] = 5000
        self.ledger.step(r, 2500)
        self.assertTrue(self.ledger.state['hard_lock'])
        self.ledger.step(row(1), -2500)
        self.assertEqual(self.ledger.state['qty'], 0)
        self.assertTrue(self.ledger.state['hard_lock'])

    def test_bad_row_and_market_file_cannot_enter_adapter(self):
        r = row()
        del r['_synthetic']
        with self.assertRaisesRegex(ValueError, 'SYNTHETIC_EVENTS_ONLY'):
            self.ledger.step(r, 1000)
        self.assertEqual(self.ledger.state['events'], 0)

    def test_independent_audit_detects_corruption(self):
        self.ledger.step(row(), 1000)
        receipt = self.session.view(self.ledger.book)
        receipt['economic_cash'] += 1
        with self.assertRaises(AssertionError):
            self.session.books[self.ledger.book].check(receipt)

    def test_three_bucket_lineage_reaches_every_candidate(self):
        base, stress = self.ledger, NativeLedger(self.session, 2)
        window = LearningWindow([.5]*3, base, stress)
        for i in range(3):
            start = len(self.session.journal)
            window.step(row(i), dict(bucket=i, trend=1000*(i+1)/3, defensive=0))
            for line, _ in self.session.journal[start:]:
                fields = line.split()
                if fields[0] == 'EV':
                    self.assertEqual(fields[5], str(i))
            for pair in window.books.values():
                for book in pair:
                    self.assertEqual(book.state['bucket'], i)
        self.assertEqual(window.counts, [[1, 0], [1, 0], [1, 0]])
        before = len(self.session.journal)
        with self.assertRaisesRegex(ValueError, 'DECISION_BUCKET'):
            window.step(row(3), dict(bucket=3, trend=1, defensive=0))
        self.assertEqual(len(self.session.journal), before)

    def test_learning_direction_cost_holdout_and_exposure_controls(self):
        for mode in ('degenerate', 'positive', 'cost', 'reverse', 'scale'):
            RESULTS[mode] = learning_control(DRIVER, mode)
        self.assertEqual(RESULTS['positive']['input_prefix_sha256'], RESULTS['reverse']['input_prefix_sha256'])
        self.assertEqual(RESULTS['positive']['locked'], RESULTS['reverse']['locked'])
        self.assertEqual(RESULTS['positive']['result']['counts'], [[0, 0], [168, 72], [0, 0]])


def main():
    global DRIVER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--driver', required=True)
    parser.add_argument('--report')
    args = parser.parse_args()
    DRIVER = str(Path(args.driver).resolve(strict=True))
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeRewardTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = dict(status='NATIVE_EXECUTION_REWARD_SYNTHETIC_PASS' if result.wasSuccessful() else 'FAIL',
                  tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  skipped=len(result.skipped), controls=RESULTS, g4=False, production_changed=False,
                  driver_sha256=hashlib.sha256(Path(DRIVER).read_bytes()).hexdigest())
    if args.report:
        path = Path(args.report)
        if path.exists():
            # One reviewed identical-command retry, preserving the first FAIL.
            path = path.with_name(path.stem+'-retry'+path.suffix)
        with path.open('x') as f:
            json.dump(report, f, indent=2, sort_keys=True, allow_nan=False)
            f.write('\n')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
