#!/usr/bin/env python3

import csv
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

import run_microstructure_alpha_development as probe


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthetic_series(row_count: int = 400) -> dict[str, np.ndarray]:
    timestamp = np.arange(row_count, dtype=np.int64) * 1000
    mid = 100.0 + np.arange(row_count, dtype=np.float64) * 0.001
    return {
        "timestamp": timestamp,
        "best_bid": mid - 0.005,
        "best_ask": mid + 0.005,
        "mid": mid,
        "spread_bps": np.full(row_count, 1.0),
        "microprice": mid + np.sin(np.arange(row_count)) * 0.002,
        "book_imbalance_l1": np.sin(np.arange(row_count) / 7.0),
        "book_imbalance_l5": np.sin(np.arange(row_count) / 11.0),
        "book_imbalance_l20": np.sin(np.arange(row_count) / 17.0),
        "depth_slope": np.full(row_count, 2.0),
        "book_update_count": np.full(row_count, 3.0),
        "trade_count": np.ones(row_count),
        "buy_quote_volume": np.full(row_count, 20.0),
        "sell_quote_volume": np.full(row_count, 10.0),
        "trade_imbalance": np.full(row_count, 1.0 / 3.0),
    }


class MicrostructureAlphaDevelopmentTest(unittest.TestCase):
    def write_custom_feature_segment(
        self,
        root: pathlib.Path,
        name: str,
        row_indices: list[int],
        overrides=None,
    ) -> dict:
        path = root / "features" / "SOLUSDT" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        series = synthetic_series(max(row_indices) + 1)
        overrides = overrides or {}
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=probe.REQUIRED_FIELDS)
            writer.writeheader()
            for index in row_indices:
                row = {name: series[name][index] for name in probe.REQUIRED_FIELDS}
                row.update(overrides.get(index, {}))
                writer.writerow(row)
        return {
            "feature_path": str(path),
            "feature_sha256": sha256(path),
            "feature_row_count": len(row_indices),
            "first_timestamp_ms": row_indices[0] * 1000,
            "last_timestamp_ms": row_indices[-1] * 1000,
        }

    def write_feature_segment(self, root: pathlib.Path) -> tuple[pathlib.Path, dict]:
        path = root / "features" / "SOLUSDT" / "development-segment.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        series = synthetic_series(4)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=probe.REQUIRED_FIELDS)
            writer.writeheader()
            for index in range(4):
                writer.writerow(
                    {name: series[name][index] for name in probe.REQUIRED_FIELDS}
                )
        item = {
            "feature_path": str(path),
            "feature_sha256": sha256(path),
            "feature_row_count": 4,
            "first_timestamp_ms": 0,
            "last_timestamp_ms": 3000,
        }
        return path, item

    def test_checksum_bound_segment_loading_fails_on_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            path, item = self.write_feature_segment(root)
            loaded = probe.load_capture_rows({"segments": [item]})
            self.assertEqual(loaded["timestamp"].tolist(), [0, 1000, 2000, 3000])

            path.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                probe.load_capture_rows({"segments": [item]})

    def test_adjacent_shared_boundary_bucket_is_dropped_and_audited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            first = self.write_custom_feature_segment(
                root, "segment-1.csv", [0, 1]
            )
            second = self.write_custom_feature_segment(
                root,
                "segment-2.csv",
                [1, 2],
                overrides={1: {"trade_count": 99.0}},
            )

            loaded = probe.load_capture_rows({"segments": [first, second]})

            self.assertEqual(loaded["timestamp"].tolist(), [0, 2000])
            audit = loaded["capture_merge_audit"]
            self.assertEqual(audit["method"], probe.CAPTURE_MERGE_CONTRACT["method"])
            self.assertEqual(audit["manifest_feature_row_count"], 4)
            self.assertEqual(audit["shared_adjacent_boundary_bucket_count"], 1)
            self.assertEqual(audit["conflicting_shared_boundary_bucket_count"], 1)
            self.assertEqual(audit["identical_shared_boundary_bucket_count"], 0)
            self.assertEqual(audit["output_feature_row_count"], 2)
            self.assertEqual(
                audit["dropped_boundary_timestamps_sha256"],
                probe.canonical_sha256({"timestamps_ms": [1000]}),
            )

    def test_identical_adjacent_shared_boundary_is_also_dropped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            first = self.write_custom_feature_segment(
                root, "segment-1.csv", [0, 1]
            )
            second = self.write_custom_feature_segment(
                root, "segment-2.csv", [1, 2]
            )

            loaded = probe.load_capture_rows({"segments": [first, second]})

            self.assertEqual(loaded["timestamp"].tolist(), [0, 2000])
            audit = loaded["capture_merge_audit"]
            self.assertEqual(audit["identical_shared_boundary_bucket_count"], 1)
            self.assertEqual(audit["conflicting_shared_boundary_bucket_count"], 0)

    def test_non_boundary_segment_overlap_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            first = self.write_custom_feature_segment(
                root, "segment-1.csv", [0, 1, 2]
            )
            second = self.write_custom_feature_segment(
                root, "segment-2.csv", [1, 2, 3]
            )

            with self.assertRaisesRegex(ValueError, "non-boundary capture segment overlap"):
                probe.load_capture_rows({"segments": [first, second]})

    def test_capture_assessment_is_development_only_and_not_promotion_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "assessment.json"
            payload = {
                "schema_version": probe.ASSESSMENT_SCHEMA_VERSION,
                "status": "PASS",
                "development_screen_ready": True,
                "research_domain": "forward_development_only",
                "promotion_evidence": False,
                "promotion_eligible": False,
                "coverage_ms": 86_400_000,
                "minimum_coverage_ms": 86_400_000,
                "latest_exchange_timestamp_ms": 86_399_000,
                "segments": [{"feature_path": "development.csv"}],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(probe.validate_capture_assessment(path), payload)

            payload["promotion_evidence"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(probe.CaptureNotReady):
                probe.validate_capture_assessment(path)

    def test_features_are_causal_under_future_mutation(self):
        original = synthetic_series()
        mutated = {key: value.copy() for key, value in original.items()}
        mutated["mid"][250:] *= 2.0
        mutated["microprice"][250:] *= 2.0
        first, names = probe.build_causal_features(original)
        second, second_names = probe.build_causal_features(mutated)
        self.assertEqual(names, second_names)
        np.testing.assert_allclose(first[:250], second[:250], equal_nan=True)

    def test_joint_target_uses_executable_quotes_latency_and_cost(self):
        series = synthetic_series(5)
        outcomes, actions = probe.build_joint_action_returns(
            series,
            horizons_seconds=[1],
            execution_latency_seconds=1,
            additional_round_trip_cost_bps=2.0,
        )
        self.assertEqual(
            actions,
            [
                {"direction": "long", "horizon_seconds": 1},
                {"direction": "short", "horizon_seconds": 1},
            ],
        )
        expected_long = (
            series["best_bid"][2] / series["best_ask"][1] - 1.0
        ) * 10000.0 - 2.0
        expected_short = (
            series["best_bid"][1] / series["best_ask"][2] - 1.0
        ) * 10000.0 - 2.0
        self.assertAlmostEqual(outcomes[0, 0], expected_long)
        self.assertAlmostEqual(outcomes[0, 1], expected_short)
        self.assertTrue(np.isnan(outcomes[-1]).all())

    def test_policy_selects_direction_and_exit_jointly_without_overlap(self):
        timestamps = np.arange(7, dtype=np.int64) * 1000
        actions = [
            {"direction": "long", "horizon_seconds": 2},
            {"direction": "short", "horizon_seconds": 1},
        ]
        prediction = np.tile(np.asarray([[5.0, 1.0]]), (7, 1))
        realized = np.tile(np.asarray([[3.0, -2.0]]), (7, 1))
        report = probe.evaluate_joint_policy(
            timestamps=timestamps,
            prediction=prediction,
            realized_base=realized,
            actions=actions,
            threshold_bps=2.0,
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
            execution_latency_seconds=1,
        )
        # Entries occur at t=0,3,6; the selected two-second exit plus one-second
        # latency prevents overlapping economic episodes.
        self.assertEqual(report["base_cost"]["count"], 3)
        self.assertEqual(report["action_counts"], {"long_2s": 3})
        self.assertAlmostEqual(report["stress_cost"]["mean_bps"], 2.0)

    def test_time_splits_are_purged_and_oos_windows_do_not_overlap(self):
        timestamps = np.arange(100000, dtype=np.int64) * 1000
        splits = probe.build_time_splits(
            timestamps,
            n_splits=3,
            train_window_seconds=20000,
            validation_window_seconds=5000,
            test_window_seconds=6000,
            rolling_step_seconds=6000,
            embargo_seconds=301,
        )
        for split in splits:
            self.assertLessEqual(split.fit_end_ms + 301000, split.validation_start_ms)
            self.assertLessEqual(split.validation_end_ms + 301000, split.test_start_ms)
        self.assertLessEqual(splits[0].test_end_ms, splits[1].test_start_ms)

    def test_prediction_permutation_control_rejects_unconditional_drift(self):
        report = probe.summarize_prediction_permutation_controls(
            base_means_by_trial=[[2.0, 2.0] for _ in range(7)],
            stress_means_by_trial=[[1.0, 1.0] for _ in range(7)],
            required_split_count=2,
            candidate_base_split_lcb_bps=2.0,
            candidate_stress_split_lcb_bps=1.0,
            minimum_excess_lcb_bps=0.0,
            seed=1,
        )

        self.assertTrue(report["fully_verifiable"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["maximum_control_base_split_lcb_bps"], 2.0)

    def test_not_ready_capture_still_writes_fail_closed_audit_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            assessment = root / "capture.json"
            output = root / "development.json"
            manifest = root / "candidate.json"
            assessment.write_text(
                json.dumps(
                    {
                        "schema_version": probe.ASSESSMENT_SCHEMA_VERSION,
                        "status": "FAIL",
                        "development_screen_ready": False,
                        "research_domain": "forward_development_only",
                        "promotion_evidence": False,
                        "promotion_eligible": False,
                        "segments": [],
                    }
                ),
                encoding="utf-8",
            )
            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "run_microstructure_alpha_development.py",
                    "--capture-assessment",
                    str(assessment),
                    "--output",
                    str(output),
                    "--candidate-manifest-output",
                    str(manifest),
                    "--model-output",
                    str(root / "model.cbm"),
                ]
                status = probe.main()
            finally:
                sys.argv = old_argv
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.read_text())["status"], "NOT_READY")
            self.assertEqual(json.loads(manifest.read_text())["status"], "rejected")

    def test_complete_probe_freezes_model_bound_development_candidate(self):
        class FakeMultiModel:
            def __init__(self):
                self.iterations = 5

            def set_params(self, **kwargs):
                self.iterations = int(kwargs.get("iterations", self.iterations))

            def fit(self, features, targets, **kwargs):
                self.action_count = targets.shape[1]
                return self

            def predict(self, features):
                prediction = np.zeros((len(features), self.action_count))
                prediction[:, 1] = 20.0
                return prediction

            def get_best_iteration(self):
                return self.iterations - 1

            def save_model(self, path):
                pathlib.Path(path).write_bytes(b"frozen-microstructure-model")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            assessment_path = root / "capture.json"
            assessment_path.write_text("{}", encoding="utf-8")
            series = synthetic_series(5000)
            # Make the long 10-second action decisively positive after executable
            # spread, base cost, and stressed cost.
            mid = 100.0 + np.arange(5000, dtype=np.float64) * 0.02
            series["mid"] = mid
            series["best_bid"] = mid - 0.005
            series["best_ask"] = mid + 0.005
            series["microprice"] = mid
            series["capture_merge_audit"] = {
                "method": probe.CAPTURE_MERGE_CONTRACT["method"],
                "input_segment_count": 1,
                "manifest_feature_row_count": 5000,
                "shared_adjacent_boundary_bucket_count": 0,
                "conflicting_shared_boundary_bucket_count": 0,
                "identical_shared_boundary_bucket_count": 0,
                "dropped_boundary_bucket_count": 0,
                "dropped_boundary_timestamps_sha256": probe.canonical_sha256(
                    {"timestamps_ms": []}
                ),
                "first_dropped_boundary_timestamp_ms": None,
                "last_dropped_boundary_timestamp_ms": None,
                "output_feature_row_count": 5000,
            }
            args = type(
                "Args",
                (),
                {
                    "capture_assessment": str(assessment_path),
                    "model_output": str(root / "candidate.cbm"),
                    "horizons_seconds": [5, 10],
                    "execution_latency_seconds": 1,
                    "additional_round_trip_cost_bps": 1.0,
                    "stress_cost_multiplier": 1.25,
                    "min_eligible_rows": 3000,
                    "n_splits": 2,
                    "train_window_seconds": 1500,
                    "validation_window_seconds": 300,
                    "test_window_seconds": 300,
                    "rolling_step_seconds": 300,
                    "min_window_rows": 200,
                    "calibration_quantiles": [0.5, 0.8],
                    "min_calibration_trades": 5,
                    "min_oos_trades": 10,
                    "min_positive_splits_ratio": 1.0,
                    "iterations": 5,
                    "depth": 2,
                    "learning_rate": 0.1,
                    "l2_leaf_reg": 1.0,
                    "random_strength": 0.0,
                    "random_seed": 1,
                    "early_stopping_rounds": 2,
                },
            )()
            assessment = {
                "coverage_ms": 5_000_000,
                "valid_segment_count": 1,
            }
            with mock.patch.object(
                probe, "validate_capture_assessment", return_value=assessment
            ), mock.patch.object(
                probe, "load_capture_rows", return_value=series
            ), mock.patch.object(
                probe, "build_model", side_effect=lambda unused: FakeMultiModel()
            ), mock.patch.object(
                probe,
                "evaluate_prediction_permutation_controls",
                side_effect=lambda **kwargs: [
                    {
                        "trial": trial,
                        "base_cost": {"mean_bps": -2.0},
                        "stress_cost": {"mean_bps": -2.25},
                    }
                    for trial in range(kwargs["trials"])
                ],
            ):
                report = probe.run_probe(args)

            self.assertTrue(report["fully_verifiable"])
            self.assertTrue(report["economic_screen"]["development_passed"])
            self.assertTrue(report["negative_control"]["passed"])
            self.assertEqual(len(report["frozen_candidate"]["model_sha256"]), 64)
            self.assertFalse(report["promotion_eligible"])


if __name__ == "__main__":
    unittest.main()
