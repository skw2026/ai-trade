#!/usr/bin/env python3
"""Small negative controls for the strict diagnostic, not model qualification."""
import pathlib
import tempfile
import unittest

import audit_strict_learning as audit
import test_verify_offline_learning_loop as fixtures


class StrictDiagnosticTest(unittest.TestCase):
    def test_t_stat_constant_is_not_significant(self):
        self.assertEqual(audit.t_stat([1.0] * 20), 0)
        self.assertEqual(audit.t_stat([]), 0)
        self.assertGreater(audit.t_stat([0.1, 0.2, 0.3, 0.4]), 1.5)

    def test_empty_trace_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'empty strict trace'):
            audit.strict_audit([], 12)

    def test_signal_delay_violation_rejected(self):
        record = dict(before=0.5, weight=0.5, delayed_signal=80)
        with self.assertRaisesRegex(AssertionError, 'latency'):
            audit.strict_audit([record], 12)

    def test_inconsistent_exposure_input_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            audit.loop.write_csv(root / 'positive.csv', audit.loop.generate(5))
            audit.loop.write_csv(root / 'adaptive.csv', audit.loop.generate(6))
            with self.assertRaisesRegex(AssertionError, 'unpaired market path'):
                audit.exposure_attribution(root)

    def test_fixed_notional_does_not_invent_alpha(self):
        fixture = fixtures.OfflineLearningLoopTest()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            trace, bars = fixture.trace(directory, 1)
            for name in ('positive', 'adaptive'):
                audit.loop.write_csv(root / f'{name}.csv', bars)
                # Same actual one-episode fixture in both arms has no uplift.
                (root / f'{name}.trace.csv').write_bytes(trace.read_bytes())
            result = audit.exposure_attribution(root)
            self.assertEqual(result['fixed_notional_net_delta'], 0)
            self.assertEqual(result['verdict'], 'EXPOSURE_SCALING_ONLY')
            self.assertFalse(result['equal_risk_proven'])


if __name__ == '__main__':
    unittest.main()
