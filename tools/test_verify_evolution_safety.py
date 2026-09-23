#!/usr/bin/env python3
import unittest
import verify_evolution_safety as safety


class SafetyAuditTest(unittest.TestCase):
    def row(self, **override):
        row = dict(before='0.5', weight='0.5', delayed_signal='0', model_signal='0',
                   price='100', rolled_back='0', safety_withdrawn='0', action='none')
        row.update(override)
        return row

    def test_empty(self):
        with self.assertRaisesRegex(AssertionError, 'empty'):
            safety.audit([], 12)

    def test_early_latch(self):
        with self.assertRaisesRegex(AssertionError, 'early'):
            safety.audit([self.row(safety_withdrawn='1')], 12)

    def test_unattributed_weight(self):
        with self.assertRaisesRegex(AssertionError, 'early'):
            safety.audit([self.row(weight='0.55')], 12)

    def test_future_signal(self):
        with self.assertRaisesRegex(AssertionError, 'latency'):
            safety.audit([self.row(delayed_signal='80')], 12)

    def test_baseline_restore(self):
        with self.assertRaisesRegex(AssertionError, 'baseline'):
            safety.audit([self.row(rolled_back='1')], 12)


if __name__ == '__main__':
    unittest.main()
