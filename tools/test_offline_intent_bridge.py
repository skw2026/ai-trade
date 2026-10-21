"""Synthetic parity with the frozen Python policy; no market or PnL replay."""
import itertools
from decimal import Decimal as D
import os
from pathlib import Path
import subprocess
import unittest

from fixed_intent_delay import IntentState
from audit_bounded_learning import Book


class NativePolicyParity(unittest.TestCase):
    def test_frozen_actions_at_edges_and_history(self):
        binary = Path(os.environ['OFFLINE_INTENT_BRIDGE_BINARY']).resolve()
        self.assertTrue(binary.is_file())
        # Probe flat/held score ties, both directions, large and tiny features,
        # including each three-element sequence; no sampled market observations.
        values = (-.001, -.000325, -.0002, 0., .0002, .000325, .001)
        samples = list(itertools.chain.from_iterable(itertools.product(values, repeat=3)))
        state = IntentState()
        expected = [state.update((0., x)) for x in samples]
        fixture = ''.join(f'{i*300000} 0 {x:.17g} 50000\n' for i, x in enumerate(samples))
        result = subprocess.run([str(binary), '--policy-stream'], input=fixture,
                                text=True, capture_output=True, timeout=30, check=True)
        actual = [float(line) for line in result.stdout.splitlines()]
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 1029)


class NativeEventParity(unittest.TestCase):
    """Synthetic event contracts, not market replay or economic qualification."""
    def compare(self, events, multiplier):
        binary = Path(os.environ['OFFLINE_INTENT_BRIDGE_BINARY']).resolve()
        result = subprocess.run([str(binary), '--event-stream', str(multiplier)],
            input='\n'.join(events)+'\n', text=True, capture_output=True, timeout=30, check=True)
        rows = result.stdout.splitlines()
        self.assertEqual(len(rows), len(events))
        book = Book(multiplier == 2)
        policy = IntentState()
        target = 0
        stopped = False
        mark = D(50000)
        funding = {}
        for i, (event, row) in enumerate(zip(events, rows)):
            fields = event.split()
            kind = fields[0]
            if kind == 'U':
                ts = int(fields[1]); rate, value = map(D, fields[2:])
                if ts not in funding:
                    mark = value
                    book.fund(mark, rate)
                    funding[ts] = rate, mark
                else:
                    self.assertEqual(funding[ts], (rate,value))
            elif kind == 'B':
                price, mark = map(D, fields[3:])
                desired = 0 if stopped else policy.update((0., float(fields[2])))
                book.observe(mark,mark)
                if book.dd >= D('.08'):
                    stopped = True; desired = 0
                if desired != target or abs(book.q)*max(price,mark)>2500+D('1e-8') or stopped:
                    capped = min(abs(D(desired)), D(2500)*price/max(price,mark))
                    book.trade(-capped if desired<0 else capped,price,mark)
                target = desired
            elif kind == 'H':
                book.observe(*map(D,fields[1:]))
            elif kind == 'M':
                mark = D(fields[1]); book.observe(mark,mark)
            elif kind == 'X':
                price,mark=map(D,fields[1:]); book.observe(mark,mark)
                book.trade(D(0),price,mark); stopped=True; target=0
            elif kind == 'L':
                stopped=True
            else:
                self.assertEqual(kind,'R')
            stopped = stopped or book.dd>=D('.08')
            expected = (book.q,book.cash,book.value(mark),book.fees,book.funding,
                        book.peak,book.dd,D(int(stopped)))
            actual = list(map(D,row.split()))
            self.assertEqual(len(actual),len(expected))
            for name,a,b in zip(('qty','cash','equity','fees','funding','peak','dd','stopped'),actual,expected):
                tolerance=D('2e-12') if name in ('qty','dd','stopped') else D('1e-8')
                self.assertLessEqual(abs(a-b),tolerance,(multiplier,i,event,name,a,b))
        return actual

    def test_ordered_cost_funding_cap_reversal_and_terminal(self):
        # Features are already-completed bars. B acts at the next open, NOT at
        # the feature bar's close; the full historical scheduler is out of scope.
        events = ['U 0 0.001 50000','B 0 -0.001 50000 50000','H 50100 49900','M 50000',
            'U 300000 0.0001234567890123 60000','B 300000 -0.0002 50000 60000',
            'H 60000 59500','M 60000','U 600000 -0.0000234567890123 50000',
            'B 600000 -0.0002 50000 50000','H 50100 49900','M 50000',
            'U 900000 0.0001 51000','B 900000 0.001 51000 51000','H 51200 50800','M 50900',
            'U 1200000 0.0002 52000','B 1200000 0.0002 52000 52000','H 52100 51800','M 51900',
            'X 51900 51900','B 1500000 -0.001 51900 51900','X 51900 51900']
        for multiplier in (1,2):
            with self.subTest(cost=multiplier):
                final=self.compare(events,multiplier)
                restarted=self.compare(list(itertools.chain.from_iterable((e,'R') for e in events)),multiplier)
                self.assertEqual(final,restarted)
                self.assertEqual(final[0],0)

    def test_intrabar_stop_funding_old_holder_next_open(self):
        events=['U 0 0 50000','B 0 -0.001 50000 50000','H 70000 50000',
                'R','U 300000 .0001234567890123 52000',
                'B 300000 -0.001 52000 52000','R',
                'B 600000 -0.001 52000 52000']
        for multiplier in (1,2):
            with self.subTest(cost=multiplier):
                final=self.compare(events,multiplier)
                self.assertEqual(final[0],0)
                self.assertEqual(final[-1],1)

    def test_duplicate_funding_across_restart(self):
        events=['U 0 0 50000','B 0 -0.001 50000 50000',
                'U 300000 .0000000000123456789 50000','R',
                'U 300000 .0000000000123456789 50000','X 50000 50000']
        for multiplier in (1,2):
            self.compare(events,multiplier)

    def test_stress_only_crossing_retires_both_books(self):
        prefix=['U 0 0 50000','B 0 -0.001 50000 50000','H 67387 50000']
        base=self.compare(prefix,1); stress=self.compare(prefix,2)
        self.assertLess(base[-2],D('.08'))
        self.assertGreaterEqual(stress[-2],D('.08'))
        self.assertEqual((base[-1],stress[-1]),(0,1))
        # Orchestration propagates the first crossing; the bridge does not own
        # the other cost book. Exit stays at next open, with funding first.
        for multiplier in (1,2):
            final=self.compare(prefix+['L','R','U 300000 .0001 52000',
                               'B 300000 -0.001 52000 52000'],multiplier)
            self.assertEqual((final[0],final[-1]),(0,1))

    def test_terminal_exit_larger_than_single_order_limit(self):
        events=['U 0 0 50000','B 0 -0.001 50000 50000','M 52000',
                'X 52000 52000','R','X 52000 52000']
        for multiplier in (1,2):
            final=self.compare(events,multiplier)
            self.assertEqual((final[0],final[-1]),(0,1))


if __name__ == '__main__':
    unittest.main()
