"""Synthetic Decimal reconciliation controls, no retained-market data reads."""
from decimal import Decimal as D
import math
from pathlib import Path
import tempfile
import unittest
import audit_bounded_learning as a
import run_bounded_learning as b

class DecimalAuditTest(unittest.TestCase):
    def test_flat_roundtrip(self):
        book=a.Book();book.trade(D(2500),D(100),D(100));book.trade(D(0),D(100),D(100))
        self.assertEqual(book.q,0)
        self.assertEqual(book.cash,10000-book.fees-book.slip)

    def test_signed_funding(self):
        for qty in (D(2),D(-2)):
            book=a.Book();book.q=qty;book.fund(D(100),D('.001'))
            self.assertEqual(book.funding,qty/10)

    def test_independent_synthetic_path_both_costs(self):
        for stress in (False,True):
            floating=b.Wallet(2 if stress else 1);decimal=a.Book(stress)
            for i in range(150):
                price=100+math.sin(i*.17);mark=price+.02
                target=2500 if i%3==0 else -1250 if i%3==1 else 0
                rate=.0001 if i%17==0 else 0
                floating.fund(mark,rate);decimal.fund(D(str(mark)),D(str(rate)))
                floating.trade(target,price,mark);decimal.trade(D(target),D(str(price)),D(str(mark)))
                floating.observe(mark+.1,mark-.1);decimal.observe(D(str(mark+.1)),D(str(mark-.1)))
                decimal.check(floating.snapshot(mark),D(str(mark)))

    def test_tampered_snapshot_rejected(self):
        book=a.Book();snapshot=b.Wallet().snapshot(100);snapshot['cash']-=1
        with self.assertRaisesRegex(ValueError,'DECIMAL_MISMATCH:cash'):book.check(snapshot,D(100))

    def test_scope_updates_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);b.save(root/'result.json',dict(controller_updates=1))
            with self.assertRaisesRegex(ValueError,'ZERO_UPDATE'):a.audit(root)

    def test_trace_identity_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);b.save(root/'trace.jsonl',dict(synthetic=True))
            b.save(root/'result.json',dict(controller_updates=0,trace_sha256='0'*64))
            with self.assertRaisesRegex(ValueError,'TRACE_IDENTITY'):a.audit(root)

if __name__=='__main__':unittest.main()
