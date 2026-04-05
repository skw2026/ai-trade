#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("model_registry.py")
    spec = importlib.util.spec_from_file_location("model_registry", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REGISTRY = load_module()


class ModelRegistryTest(unittest.TestCase):
    def test_gate_integrator_report_propagates_governance_fail_reasons(self):
        gate_pass, fail_reasons, summary = REGISTRY.gate_integrator_report(
            report={
                "metrics_oos": {
                    "auc_mean": 0.51,
                    "delta_auc_vs_baseline": 0.02,
                    "split_trained_count": 5,
                    "split_count": 5,
                    "split_trained_ratio": 1.0,
                    "auc_stdev": 0.12,
                    "train_test_auc_gap_mean": 0.18,
                    "random_label_auc": 0.58,
                    "random_label_auc_mean": 0.57,
                    "random_label_auc_stdev": 0.03,
                    "random_label_auc_max": 0.61,
                },
                "governance": {
                    "pass": False,
                    "fail_reasons": [
                        "auc_stdev=0.120000 > max_auc_stdev=0.080000",
                        "random_label_auc=0.580000 > max_random_label_auc=0.550000",
                    ],
                },
            },
            min_auc_mean=0.48,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=1,
            min_split_trained_ratio=0.5,
        )

        self.assertFalse(gate_pass)
        self.assertIn("integrator_report.governance.pass=false", fail_reasons)
        self.assertIn(
            "governance: auc_stdev=0.120000 > max_auc_stdev=0.080000",
            fail_reasons,
        )
        self.assertIn(
            "governance: random_label_auc=0.580000 > max_random_label_auc=0.550000",
            fail_reasons,
        )
        self.assertEqual(summary["auc_mean"], 0.51)
        self.assertEqual(summary["auc_stdev"], 0.12)
        self.assertEqual(summary["train_test_auc_gap_mean"], 0.18)
        self.assertEqual(summary["random_label_auc"], 0.58)
        self.assertEqual(summary["random_label_auc_mean"], 0.57)
        self.assertEqual(summary["random_label_auc_stdev"], 0.03)
        self.assertEqual(summary["random_label_auc_max"], 0.61)


if __name__ == "__main__":
    unittest.main()
