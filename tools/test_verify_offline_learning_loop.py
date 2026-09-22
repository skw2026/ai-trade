#!/usr/bin/env python3
"""Fast harness regression; actual CatBoost/native acceptance is mandatory in CD."""
import csv
import pathlib
import tempfile
import unittest
from unittest import mock

import verify_offline_learning_loop as loop

ROOT = pathlib.Path(__file__).resolve().parents[1]


class OfflineLearningLoopTest(unittest.TestCase):
    def test_generator_prefix_does_not_depend_on_future_length(self):
        self.assertEqual(loop.generate(350), loop.generate(900)[:350])

    def test_negative_controls_do_not_change_development(self):
        reference = loop.generate(loop.TRAIN_BARS)
        for mode in ("learnable", "noise", "drift"):
            self.assertEqual(reference, loop.generate(loop.START + 1536, mode)[:loop.TRAIN_BARS])

    def test_volume_cue_precedes_return_by_two_bars(self):
        rows = loop.generate(900)
        correct = 0
        for i in range(2, len(rows)):
            cue = rows[i - 2][5] - 1000
            ret = rows[i][4] / rows[i - 1][4] - 1
            correct += cue * ret > 0
        self.assertEqual(correct, len(rows) - 2)
        self.assertLess(loop.TRAIN_BARS - 3 + 2, loop.START)
        self.assertGreaterEqual(loop.EMBARGO, 2)

    def trace(self, directory, direction, corrupted=False):
        rows = loop.generate(5)
        qty = 40 / rows[1][4]
        entry = rows[1][4] * (1 + direction * 0.0001)
        exit_price = rows[2][4] * (1 - direction * 0.0001)
        fee = qty * (entry + exit_price) * 0.00055
        funding = direction * qty * rows[2][4] * 0.000025
        net = direction * qty * (exit_price - entry) - fee - funding
        records = [dict(index=0, applied=1, direction=direction, weight=0.5,
                        action="none", episodes=0, fills=0, net=0, fee=0, funding=0, qty=0),
                   dict(index=2, applied=0, direction=0, weight=0.5,
                        action="none", episodes=1, fills=2, net=net + int(corrupted),
                        fee=fee, funding=funding, qty=0)]
        path = pathlib.Path(directory) / "trace.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
        return path, rows

    def test_independent_ledger_long_and_short(self):
        for direction in (-1, 1):
            with tempfile.TemporaryDirectory() as directory:
                trace, rows = self.trace(directory, direction)
                result, _ = loop.summarize(trace, rows, 5.5)
                self.assertEqual(result["episodes"], 1)
                self.assertFalse(result["offline_control_accepted"])
                self.assertEqual(result["weight_changes"], 0)
                self.assertTrue(result["native_ledger_matched"])

    def test_accounting_error_cannot_be_masked(self):
        with tempfile.TemporaryDirectory() as directory:
            trace, rows = self.trace(directory, -1, corrupted=True)
            with self.assertRaisesRegex(AssertionError, "independent net ledger mismatch"):
                loop.summarize(trace, rows, 5.5)

    def test_missing_real_training_dependency_is_not_skip_or_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(loop.train, "CatBoostClassifier", None):
                with self.assertRaisesRegex(AssertionError, "real numpy/CatBoost required"):
                    loop.verify(pathlib.Path("absent"), pathlib.Path(directory) / "out")

    def test_each_failed_case_stops_before_dependent_scenarios(self):
        with self.assertRaisesRegex(AssertionError, "learnable control rejected"):
            loop.check_case("positive", {"offline_control_accepted": False}, {})
        with self.assertRaisesRegex(AssertionError, "noise wrongly qualified"):
            loop.check_case("noise", {"offline_control_accepted": True}, {})
        with self.assertRaisesRegex(AssertionError, "rollback and freeze"):
            loop.check_case("drift", {"rollbacks": 0, "cooldown_evaluations": 1}, {})

    def test_learning_acceptance_blocks_deploy_and_runs_without_network(self):
        workflow = (ROOT / ".github/workflows/cd.yml").read_text()
        block = workflow.split("name: Verify Offline Learning Loop", 1)[1].split("- name:", 1)[0]
        self.assertIn("tools/validation_gate.py run", block)
        self.assertIn("--network none", block)
        self.assertIn("--read-only", block)
        self.assertNotIn("continue-on-error", block)
        self.assertLess(workflow.index("name: Verify Offline Learning Loop"),
                        workflow.index("name: Compute Deploy Gate"))
        dockerfile = (ROOT / "Dockerfile").read_text()
        runtime, research = dockerfile.split("FROM research-dependencies AS research", 1)
        self.assertNotIn("/app/offline_learning_driver", runtime)
        self.assertIn("/app/offline_learning_driver", research)


if __name__ == "__main__":
    unittest.main()
