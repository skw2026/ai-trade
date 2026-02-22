#!/usr/bin/env python3

import importlib.util
import math
import pathlib
import sys
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parent


def load_module(name: str):
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TRAIN = load_module("integrator_train")


@unittest.skipIf(TRAIN.np is None, "numpy is required")
class IntegratorTrainTest(unittest.TestCase):
    def test_build_label_uses_t_plus_1_base(self):
        close = TRAIN.np.asarray([100.0, 101.0, 102.0, 103.0, 104.0], dtype=TRAIN.np.float64)
        label, forward = TRAIN.build_label(close, horizon=2)
        self.assertAlmostEqual(float(forward[0]), 103.0 / 101.0 - 1.0, places=12)
        self.assertAlmostEqual(float(forward[1]), 104.0 / 102.0 - 1.0, places=12)
        self.assertTrue(math.isnan(float(forward[2])))
        self.assertTrue(math.isnan(float(forward[3])))
        self.assertTrue(math.isnan(float(forward[4])))
        self.assertEqual(int(label[0]), 1)
        self.assertEqual(int(label[1]), 1)

    def test_evaluate_governance_extended_thresholds(self):
        metrics_ok = {
            "auc_mean": 0.58,
            "delta_auc_vs_baseline": 0.03,
            "split_trained_count": 4,
            "split_trained_ratio": 0.8,
            "auc_stdev": 0.03,
            "train_test_auc_gap_mean": 0.04,
            "random_label_auc": 0.50,
        }
        passed, reasons = TRAIN.evaluate_governance(
            metrics_oos=metrics_ok,
            min_auc_mean=0.55,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=2,
            min_split_trained_ratio=0.5,
            max_auc_stdev=0.08,
            max_train_test_auc_gap=0.10,
            run_random_label_control=True,
            max_random_label_auc=0.55,
        )
        self.assertTrue(passed)
        self.assertEqual(reasons, [])

        metrics_gap_bad = dict(metrics_ok)
        metrics_gap_bad["train_test_auc_gap_mean"] = 0.18
        passed, reasons = TRAIN.evaluate_governance(
            metrics_oos=metrics_gap_bad,
            min_auc_mean=0.55,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=2,
            min_split_trained_ratio=0.5,
            max_auc_stdev=0.08,
            max_train_test_auc_gap=0.10,
            run_random_label_control=True,
            max_random_label_auc=0.55,
        )
        self.assertFalse(passed)
        self.assertTrue(any("train_test_auc_gap_mean" in reason for reason in reasons))

        metrics_missing_stdev = dict(metrics_ok)
        metrics_missing_stdev["split_trained_count"] = 3
        metrics_missing_stdev["auc_stdev"] = float("nan")
        passed, reasons = TRAIN.evaluate_governance(
            metrics_oos=metrics_missing_stdev,
            min_auc_mean=0.55,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=2,
            min_split_trained_ratio=0.5,
            max_auc_stdev=0.08,
            max_train_test_auc_gap=0.10,
            run_random_label_control=True,
            max_random_label_auc=0.55,
        )
        self.assertFalse(passed)
        self.assertTrue(any("auc_stdev" in reason for reason in reasons))

        metrics_random_bad = dict(metrics_ok)
        metrics_random_bad["random_label_auc"] = 0.61
        passed, reasons = TRAIN.evaluate_governance(
            metrics_oos=metrics_random_bad,
            min_auc_mean=0.55,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=2,
            min_split_trained_ratio=0.5,
            max_auc_stdev=0.08,
            max_train_test_auc_gap=0.10,
            run_random_label_control=True,
            max_random_label_auc=0.55,
        )
        self.assertFalse(passed)
        self.assertTrue(any("random_label_auc" in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
