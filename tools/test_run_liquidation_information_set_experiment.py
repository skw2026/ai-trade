#!/usr/bin/env python3

import argparse
import json
import pathlib
import tempfile
import unittest

import numpy as np

import run_liquidation_information_set_experiment as experiment


class LiquidationInformationSetExperimentTest(unittest.TestCase):
    def setUp(self):
        self.config = (
            pathlib.Path(__file__).resolve().parents[1]
            / "config"
            / "liquidation_information_set_experiment.json"
        )

    def test_frozen_contract_changes_only_information_set(self):
        policy = experiment.validate_policy(self.config)
        self.assertEqual(policy["architecture_id"], "direct_stress_utility_regression")
        self.assertEqual(policy["splits"]["count"], 6)
        self.assertEqual(policy["costs"]["additional_round_trip_cost_bps"], 11.0)
        self.assertEqual(
            policy["single_variable_change"],
            "add_bybit_sol_all_liquidation_features",
        )
        self.assertEqual(
            policy["authorities"],
            {
                "promotion_authority": False,
                "demo_activation_authorized": False,
                "live_activation_authorized": False,
            },
        )

    def test_features_use_only_fully_covered_previous_second(self):
        control = {"timestamp": np.asarray([61_000, 62_000, 63_000, 65_000], dtype=np.int64)}
        sidecar = {
            "timestamp": np.asarray([60_000, 62_000], dtype=np.int64),
            "long_liquidation_count": np.asarray([1.0, 0.0]),
            "long_liquidation_qty": np.asarray([2.0, 0.0]),
            "long_liquidation_notional": np.asarray([200.0, 0.0]),
            "short_liquidation_count": np.asarray([0.0, 1.0]),
            "short_liquidation_qty": np.asarray([0.0, 3.0]),
            "short_liquidation_notional": np.asarray([0.0, 303.0]),
        }
        matrix, names, audit = experiment.build_liquidation_features(
            control,
            sidecar,
            coverage_intervals=[(0, 64_000)],
            lag_seconds=1,
            rolling_windows_seconds=[1, 5, 20, 60],
            time_since_cap_seconds=60,
        )

        self.assertEqual(matrix.shape, (4, 31))
        self.assertTrue(np.all(np.isfinite(matrix[:3])))
        self.assertTrue(np.all(~np.isfinite(matrix[3])))
        self.assertEqual(audit["aligned_row_count"], 3)
        self.assertEqual(audit["uncovered_row_count"], 1)
        self.assertEqual(names[0], "liquidation_lag1_long_count_sum_1s")
        self.assertEqual(matrix[0, 0], 1.0)
        self.assertEqual(matrix[1, 0], 0.0)
        self.assertFalse(audit["future_fill_permitted"])
        self.assertFalse(audit["backfill_permitted"])

    def test_policy_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "policy.json"
            payload = json.loads(self.config.read_text(encoding="utf-8"))
            payload["actions"]["horizons_seconds"] = [30]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "policy_identity|actions"):
                experiment.validate_policy(path)

    def test_fully_covered_zero_event_history_is_finite_not_missing(self):
        sidecar = {"timestamp": np.asarray([], dtype=np.int64)}
        for field in (
            "long_liquidation_count",
            "long_liquidation_qty",
            "long_liquidation_notional",
            "short_liquidation_count",
            "short_liquidation_qty",
            "short_liquidation_notional",
        ):
            sidecar[field] = np.asarray([], dtype=np.float64)
        matrix, _, audit = experiment.build_liquidation_features(
            {"timestamp": np.asarray([61_000], dtype=np.int64)},
            sidecar,
            coverage_intervals=[(0, 61_000)],
            lag_seconds=1,
            rolling_windows_seconds=[1, 5, 20, 60],
            time_since_cap_seconds=60,
        )

        self.assertTrue(np.all(np.isfinite(matrix)))
        self.assertTrue(np.all(matrix[0, :28] == 0.0))
        self.assertTrue(np.all(matrix[0, 28:] == 60.0))
        self.assertEqual(audit["aligned_row_count"], 1)

    def test_not_ready_report_exposes_sanitized_capture_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            control = root / "control.json"
            treatment = root / "treatment.json"
            control.write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "coverage_ms": 172_800_000,
                        "minimum_coverage_ms": 86_400_000,
                    }
                ),
                encoding="utf-8",
            )
            treatment.write_text(
                json.dumps(
                    {
                        "status": "NOT_READY",
                        "coverage_ms": 120_000_000,
                        "minimum_coverage_ms": 126_000_000,
                        "freshness_age_ms": 12_000,
                        "feature_row_count": 7,
                        "liquidation_event_count": 11,
                        "collector_health": {"status": "PASS"},
                        "failures": [
                            "minimum_forward_capture_duration",
                            "/opt/private/api_secret=must-not-leak",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report = experiment.not_ready_report(
                argparse.Namespace(
                    config=str(self.config),
                    control_assessment=str(control),
                    treatment_assessment=str(treatment),
                ),
                "liquidation_capture_not_ready",
                not_ready_stage="liquidation_capture",
            )

        self.assertEqual(report["status"], "NOT_READY")
        self.assertEqual(report["not_ready_stage"], "liquidation_capture")
        self.assertEqual(
            report["reason_codes"],
            [
                "liquidation_capture_not_ready",
                "minimum_forward_capture_duration",
            ],
        )
        progress = report["capture_readiness"]["liquidation"]
        self.assertEqual(progress["missing_coverage_ms"], 6_000_000)
        self.assertAlmostEqual(progress["coverage_ratio"], 120 / 126)
        self.assertNotIn("api_secret", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
