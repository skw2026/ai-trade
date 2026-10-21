"""Synthetic parity with the frozen Python policy; no market or PnL replay."""
import itertools
import os
from pathlib import Path
import subprocess
import unittest

from fixed_intent_delay import IntentState


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


if __name__ == '__main__':
    unittest.main()
