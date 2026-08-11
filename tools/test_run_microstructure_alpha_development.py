#!/usr/bin/env python3

import copy
import argparse
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
    series = {
        "timestamp": timestamp,
        "best_bid": mid - 0.005,
        "best_ask": mid + 0.005,
        "best_bid_size": np.full(row_count, 10.0),
        "best_ask_size": np.full(row_count, 8.0),
        "bid_depth_l5": np.full(row_count, 40.0),
        "ask_depth_l5": np.full(row_count, 35.0),
        "bid_depth_l20": np.full(row_count, 120.0),
        "ask_depth_l20": np.full(row_count, 110.0),
        "mid": mid,
        "spread_bps": np.full(row_count, 1.0),
        "microprice": mid + np.sin(np.arange(row_count)) * 0.002,
        "book_imbalance_l1": np.sin(np.arange(row_count) / 7.0),
        "book_imbalance_l5": np.sin(np.arange(row_count) / 11.0),
        "book_imbalance_l20": np.sin(np.arange(row_count) / 17.0),
        "depth_slope": np.full(row_count, 2.0),
        "book_update_count": np.full(row_count, 3.0),
        "book_flow_imbalance": np.sin(np.arange(row_count) / 8.0),
        "book_flow_quote_volume": np.full(row_count, 1000.0),
        "book_ofi": np.sin(np.arange(row_count) / 6.0),
        "book_mid_range_bps": np.full(row_count, 0.5),
        "trade_count": np.ones(row_count),
        "buy_quote_volume": np.full(row_count, 20.0),
        "sell_quote_volume": np.full(row_count, 10.0),
        "buy_base_volume": np.full(row_count, 0.2),
        "sell_base_volume": np.full(row_count, 0.1),
        "trade_imbalance": np.full(row_count, 1.0 / 3.0),
        "trade_vwap_dislocation_bps": np.full(row_count, 0.1),
    }
    for symbol, scale in (("BTCUSDT", 10.0), ("ETHUSDT", 5.0)):
        prefix = probe.collector.context_prefix(symbol)
        context_mid = mid * scale + np.sin(np.arange(row_count) / (13.0 + scale))
        series.update(
            {
                f"{prefix}_mid": context_mid,
                f"{prefix}_spread_bps": np.full(row_count, 0.8 + scale / 100.0),
                f"{prefix}_microprice": context_mid + np.cos(np.arange(row_count)) * 0.003,
                f"{prefix}_best_bid_size": np.full(row_count, 12.0),
                f"{prefix}_best_ask_size": np.full(row_count, 11.0),
                f"{prefix}_bid_depth_l5": np.full(row_count, 50.0),
                f"{prefix}_ask_depth_l5": np.full(row_count, 45.0),
                f"{prefix}_bid_depth_l20": np.full(row_count, 150.0),
                f"{prefix}_ask_depth_l20": np.full(row_count, 140.0),
                f"{prefix}_book_imbalance_l1": np.sin(np.arange(row_count) / 5.0),
                f"{prefix}_book_imbalance_l5": np.sin(np.arange(row_count) / 9.0),
                f"{prefix}_book_imbalance_l20": np.sin(np.arange(row_count) / 15.0),
                f"{prefix}_depth_slope": np.full(row_count, 2.5),
                f"{prefix}_book_update_count": np.full(row_count, 4.0),
                f"{prefix}_book_flow_imbalance": np.sin(np.arange(row_count) / 10.0),
                f"{prefix}_book_flow_quote_volume": np.full(row_count, 2000.0),
                f"{prefix}_book_ofi": np.sin(np.arange(row_count) / 12.0),
                f"{prefix}_book_mid_range_bps": np.full(row_count, 0.4),
                f"{prefix}_trade_count": np.full(row_count, 2.0),
                f"{prefix}_buy_quote_volume": np.full(row_count, 40.0 * scale),
                f"{prefix}_sell_quote_volume": np.full(row_count, 30.0 * scale),
                f"{prefix}_buy_base_volume": np.full(row_count, 0.4),
                f"{prefix}_sell_base_volume": np.full(row_count, 0.3),
                f"{prefix}_trade_imbalance": np.full(row_count, 1.0 / 7.0),
                f"{prefix}_trade_vwap_dislocation_bps": np.full(row_count, 0.05),
            }
        )
    return series


class MicrostructureAlphaDevelopmentTest(unittest.TestCase):
    def test_binary_ranking_diagnostic_handles_perfect_and_tied_scores(self):
        perfect = probe.summarize_binary_ranking(
            [0.0, 1.0, 0.0, 1.0], [0.1, 0.9, 0.2, 0.8]
        )
        self.assertEqual(perfect["roc_auc"], 1.0)
        self.assertEqual(perfect["average_precision"], 1.0)
        self.assertEqual(perfect["average_precision_lift"], 2.0)

        tied = probe.summarize_binary_ranking(
            [0.0, 1.0, 0.0, 1.0], [0.5, 0.5, 0.5, 0.5]
        )
        self.assertEqual(tied["roc_auc"], 0.5)
        self.assertEqual(tied["average_precision"], 0.5)
        self.assertEqual(tied["average_precision_lift"], 1.0)

    def test_binary_ranking_diagnostic_rejects_non_probabilities(self):
        with self.assertRaisesRegex(ValueError, "inputs are invalid"):
            probe.summarize_binary_ranking([0.0, 1.0], [0.2, 1.2])

    def test_exact_rolling_sum_requires_every_exchange_second(self):
        values = np.asarray([1.0, 2.0, 4.0, 8.0])
        timestamps = np.asarray([0, 1000, 3000, 4000], dtype=np.int64)

        result = probe.exact_rolling_sum(values, timestamps, 2)

        self.assertTrue(np.isnan(result[0]))
        self.assertEqual(result[1], 3.0)
        self.assertTrue(np.isnan(result[2]))
        self.assertEqual(result[3], 12.0)

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
            "capture_schema_version": probe.collector.SCHEMA_VERSION,
            "symbols": list(probe.collector.CAPTURE_SYMBOLS),
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
            "capture_schema_version": probe.collector.SCHEMA_VERSION,
            "symbols": list(probe.collector.CAPTURE_SYMBOLS),
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
                "symbols": list(probe.collector.CAPTURE_SYMBOLS),
                "cross_asset_alignment_contract": (
                    probe.collector.CROSS_ASSET_ALIGNMENT_CONTRACT
                ),
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
        mutated["btc_mid"][250:] *= 3.0
        mutated["btc_microprice"][250:] *= 3.0
        mutated["eth_trade_imbalance"][250:] *= -1.0
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

    def test_fit_only_stress_event_targets_are_reconstructed_in_bps(self):
        outcomes = np.asarray(
            [
                [5.0, -1.0],
                [6.0, -2.0],
                [-1.0, 4.0],
                [-2.0, -3.0],
            ],
            dtype=np.float64,
        )
        targets, transform = probe.fit_joint_policy_target(
            outcomes,
            actions=[
                {"direction": "long", "horizon_seconds": 15},
                {"direction": "short", "horizon_seconds": 15},
            ],
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )

        np.testing.assert_array_equal(
            targets,
            np.asarray([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]]),
        )
        self.assertFalse(transform["validation_or_test_statistics_used"])
        self.assertEqual(transform["stress_incremental_cost_bps"], 1.0)
        self.assertEqual(transform["target_encoding"], "binary_zero_one")
        self.assertEqual(transform["model_action_indices"], [0, 1])
        self.assertEqual(transform["model_output_count"], 2)
        self.assertEqual(
            [
                item["stress_profitable_count"]
                for item in transform["action_statistics"]
            ],
            [2, 1],
        )

        # Zero event probability is exactly the fit-only nonprofitable mean.
        reconstructed = probe.reconstruct_base_net_scores(
            np.zeros((1, 2), dtype=np.float64), transform
        )
        expected_unprofitable_means = [
            item["stress_unprofitable_mean_base_net_bps"]
            for item in transform["action_statistics"]
        ]
        np.testing.assert_allclose(reconstructed[0], expected_unprofitable_means)
        reconstructed_positive = probe.reconstruct_base_net_scores(
            np.ones((1, 2), dtype=np.float64), transform
        )
        np.testing.assert_allclose(
            reconstructed_positive[0],
            [
                item["stress_profitable_mean_base_net_bps"]
                for item in transform["action_statistics"]
            ],
        )
        self.assertAlmostEqual(
            probe.base_net_score_to_event_probability(
                float(reconstructed_positive[0, 0]), transform, 0
            ),
            1.0,
        )
        self.assertAlmostEqual(
            probe.base_net_score_to_event_probability(
                float(reconstructed[0, 0]), transform, 0
            ),
            0.0,
        )

        shifted = outcomes.copy()
        shifted[:, 0] = 1000.0
        original_rate = transform["action_statistics"][0]["stress_profitable_rate"]
        transformed_validation = probe.transform_joint_policy_targets(
            shifted, transform
        )
        self.assertEqual(
            transform["action_statistics"][0]["stress_profitable_rate"],
            original_rate,
        )
        self.assertTrue(np.all(transformed_validation[:, 0] == 1.0))
        np.testing.assert_allclose(transformed_validation[:, 1], targets[:, 1])

        tampered = copy.deepcopy(transform)
        tampered["action_statistics"][1]["stress_profitable_rate"] = 0.99
        with self.assertRaisesRegex(ValueError, "action statistic contract failed"):
            probe.reconstruct_base_net_scores(
                np.zeros((1, 2), dtype=np.float64), tampered
            )

    def test_constant_fit_window_has_no_active_model_output(self):
        outcomes = np.asarray(
            [[-8.0], [-8.0], [-8.0], [-8.0], [-8.0]], dtype=np.float64
        )
        targets, transform = probe.fit_joint_policy_target(
            outcomes,
            actions=[{"direction": "long", "horizon_seconds": 15}],
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )

        self.assertEqual(targets.shape, (5, 0))
        self.assertFalse(transform["action_statistics"][0]["learnable"])
        self.assertEqual(transform["model_output_count"], 0)
        self.assertEqual(transform["model_action_indices"], [])

    def test_single_active_catboost_output_is_normalized_to_two_dimensions(self):
        _, transform = probe.fit_joint_policy_target(
            np.asarray([[3.0], [-2.0], [4.0], [-3.0]]),
            actions=[{"direction": "long", "horizon_seconds": 15}],
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )

        class SingleOutputModel:
            def predict_proba(self, features):
                positive = np.linspace(0.1, 0.9, len(features))
                return np.column_stack((1.0 - positive, positive))

        scores, raw = probe.predict_base_net_scores(
            SingleOutputModel(), np.zeros((3, 2)), transform
        )
        self.assertEqual(raw.shape, (3, 1))
        self.assertEqual(scores.shape, (3, 1))
        self.assertTrue(np.all(np.isfinite(scores)))

    @unittest.skipIf(probe.CatBoostClassifier is None, "catboost is unavailable")
    def test_real_catboost_learns_independent_stress_event_scores_in_bps(self):
        rng = np.random.default_rng(20260811)
        features = rng.normal(size=(2400, 6))
        noise = rng.normal(scale=0.25, size=(2400, 2))
        outcomes = np.column_stack(
            (
                4.0 * features[:, 0] + 2.0 * features[:, 2],
                -3.0 * features[:, 1] + 1.5 * features[:, 3],
            )
        ) + noise
        actions = [
            {"direction": "long", "horizon_seconds": 15},
            {"direction": "short", "horizon_seconds": 15},
        ]
        fit_targets, transform = probe.fit_joint_policy_target(
            outcomes[:1800],
            actions=actions,
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )
        validation_targets = probe.transform_joint_policy_targets(
            outcomes[1800:], transform
        )
        args = argparse.Namespace(
            iterations=80,
            depth=4,
            learning_rate=0.08,
            l2_leaf_reg=3.0,
            random_strength=0.0,
            random_seed=7,
            early_stopping_rounds=10,
            min_fit_profitable_events=16,
        )
        models = probe.fit_independent_action_models(
            fit_features=features[:1800],
            fit_targets=fit_targets,
            model_selection_features=features[1800:],
            model_selection_targets=validation_targets,
            transform=transform,
            args=args,
        )
        scores, raw = probe.predict_base_net_scores(
            models, features[1800:], transform
        )

        self.assertEqual(raw.shape, (600, 2))
        self.assertEqual(scores.shape, (600, 2))
        self.assertGreater(np.corrcoef(scores[:, 0], outcomes[1800:, 0])[0, 1], 0.7)
        self.assertGreater(np.corrcoef(scores[:, 1], outcomes[1800:, 1])[0, 1], 0.7)

    @unittest.skipIf(probe.CatBoostClassifier is None, "catboost is unavailable")
    def test_real_catboost_accepts_zero_positive_validation_window(self):
        rng = np.random.default_rng(20260812)
        fit_features = rng.normal(size=(800, 4))
        validation_features = rng.normal(size=(200, 4))
        fit_outcomes = np.where(
            fit_features[:, 0] > 0.8, 4.0, -2.0
        ).reshape(-1, 1)
        validation_outcomes = np.full((200, 1), -2.0)
        actions = [{"direction": "long", "horizon_seconds": 60}]
        fit_targets, transform = probe.fit_joint_policy_target(
            fit_outcomes,
            actions=actions,
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
            minimum_profitable_events=16,
        )
        validation_targets = probe.transform_joint_policy_targets(
            validation_outcomes, transform
        )
        args = argparse.Namespace(
            iterations=30,
            depth=3,
            learning_rate=0.08,
            l2_leaf_reg=3.0,
            random_strength=0.0,
            random_seed=7,
            early_stopping_rounds=5,
            min_fit_profitable_events=16,
        )

        models = probe.fit_independent_action_models(
            fit_features=fit_features,
            fit_targets=fit_targets,
            model_selection_features=validation_features,
            model_selection_targets=validation_targets,
            transform=transform,
            args=args,
        )
        _, probabilities = probe.predict_base_net_scores(
            models, validation_features, transform
        )

        self.assertEqual(probabilities.shape, (200, 1))
        self.assertLess(float(np.median(probabilities)), 0.5)

    def test_independent_event_targets_preserve_every_supported_action(self):
        actions = [
            {"direction": "long", "horizon_seconds": 15},
            {"direction": "long", "horizon_seconds": 300},
            {"direction": "short", "horizon_seconds": 15},
            {"direction": "short", "horizon_seconds": 300},
        ]
        outcomes = np.asarray(
            [
                [3.0, 30.0, 2.0, -5.0],
                [-2.0, -3.0, 4.0, 40.0],
                [-2.0, 8.0, -3.0, 9.0],
                [-2.0, -3.0, -4.0, -5.0],
            ]
        )

        targets, transform = probe.fit_joint_policy_target(
            outcomes,
            actions=actions,
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )

        self.assertEqual(transform["model_action_indices"], [0, 1, 2, 3])
        np.testing.assert_array_equal(np.sum(targets, axis=0), [1.0, 2.0, 2.0, 2.0])
        self.assertTrue(np.all((targets == 0.0) | (targets == 1.0)))

    def test_sparse_stress_event_action_is_excluded_using_fit_only_counts(self):
        outcomes = np.column_stack(
            (
                np.asarray([4.0] * 8 + [-2.0] * 8),
                np.asarray([4.0] + [-2.0] * 15),
            )
        )
        targets, transform = probe.fit_joint_policy_target(
            outcomes,
            actions=[
                {"direction": "long", "horizon_seconds": 15},
                {"direction": "short", "horizon_seconds": 15},
            ],
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
            minimum_profitable_events=4,
        )

        self.assertEqual(transform["model_action_indices"], [0])
        self.assertEqual(transform["model_output_count"], 1)
        self.assertEqual(targets.shape, (16, 1))
        self.assertTrue(transform["action_statistics"][0]["learnable"])
        self.assertFalse(transform["action_statistics"][1]["learnable"])

    def test_frozen_transform_serializes_only_consensus_action_model(self):
        outcomes = np.asarray(
            [[-4.0, 2.0], [-2.0, 4.0], [1.0, -3.0], [3.0, -1.0]]
        )
        _, transform = probe.fit_joint_policy_target(
            outcomes,
            actions=[
                {"direction": "long", "horizon_seconds": 15},
                {"direction": "short", "horizon_seconds": 15},
            ],
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )

        frozen = probe.select_model_action_indices(transform, [1])

        self.assertEqual(frozen["available_action_indices"], [0, 1])
        self.assertEqual(frozen["model_action_indices"], [1])
        self.assertEqual(frozen["model_output_count"], 1)
        targets = probe.transform_joint_policy_targets(outcomes, frozen)
        self.assertEqual(targets.shape, (4, 1))

    def test_nested_calibration_uses_ranked_negative_scores_without_weakening_economics(self):
        timestamps = np.arange(100, dtype=np.int64) * 1000
        prediction = np.linspace(-10.0, -1.0, 100).reshape(-1, 1)
        realized = np.where(prediction >= -2.8, 10.0, -10.0)
        report = probe.select_nested_threshold(
            timestamps=timestamps,
            prediction=prediction,
            realized_base=realized,
            actions=[{"direction": "long", "horizon_seconds": 1}],
            quantiles=[0.5, 0.8],
            min_trades=8,
            base_cost_bps=1.0,
            stress_cost_multiplier=1.25,
            execution_latency_seconds=1,
        )

        self.assertIsNotNone(report["selected"])
        self.assertLess(report["selected"]["threshold_bps"], 0.0)
        self.assertGreater(report["selected"]["base_net_lcb_bps"], 0.0)
        self.assertGreater(report["selected"]["stress_net_lcb_bps"], 0.0)
        self.assertIsNone(report["score_threshold_floor_bps"])

    def test_failed_economic_gate_keeps_non_promotional_forward_diagnostic(self):
        timestamps = np.arange(100, dtype=np.int64) * 1000
        prediction = np.linspace(0.0, 1.0, 100).reshape(-1, 1)
        realized = np.full((100, 1), -5.0)

        report = probe.select_nested_threshold(
            timestamps=timestamps,
            prediction=prediction,
            realized_base=realized,
            actions=[{"direction": "long", "horizon_seconds": 1}],
            quantiles=[0.5, 0.8],
            min_trades=8,
            base_cost_bps=1.0,
            stress_cost_multiplier=1.25,
            execution_latency_seconds=1,
        )

        self.assertIsNone(report["selected"])
        self.assertIsNotNone(report["diagnostic_selected"])
        self.assertIn("non_promotional", report["diagnostic_selection_contract"])

    def test_nested_calibration_does_not_let_high_baseline_action_hide_alpha(self):
        timestamps = np.arange(100, dtype=np.int64) * 1000
        prediction = np.column_stack(
            (np.full(100, 20.0), np.linspace(0.0, 10.0, 100))
        )
        realized = np.column_stack(
            (
                np.full(100, -10.0),
                np.where(prediction[:, 1] >= 8.0, 10.0, -10.0),
            )
        )
        report = probe.select_nested_threshold(
            timestamps=timestamps,
            prediction=prediction,
            realized_base=realized,
            actions=[
                {"direction": "long", "horizon_seconds": 1},
                {"direction": "short", "horizon_seconds": 1},
            ],
            quantiles=[0.5, 0.8],
            min_trades=8,
            base_cost_bps=1.0,
            stress_cost_multiplier=1.25,
            execution_latency_seconds=1,
        )

        self.assertEqual(report["selected"]["action_index"], 1)
        self.assertEqual(report["selected"]["direction"], "short")
        self.assertGreater(report["selected"]["stress_net_lcb_bps"], 0.0)
        self.assertEqual(len(report["action_score_distributions"]), 2)

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
            self.assertEqual(split.fit_end_ms - split.fit_start_ms, 20000000)
            self.assertLessEqual(split.fit_end_ms + 301000, split.validation_start_ms)
            self.assertLessEqual(split.validation_end_ms + 301000, split.test_start_ms)
        self.assertLessEqual(splits[0].test_end_ms, splits[1].test_start_ms)

    def test_fit_internal_model_selection_is_purged_from_model_fit(self):
        timestamps = np.arange(0, 30_000, 1000, dtype=np.int64)
        split = probe.TimeSplit(
            split_id=0,
            fit_start_ms=0,
            fit_end_ms=20_000,
            validation_start_ms=22_000,
            validation_end_ms=25_000,
            test_start_ms=27_000,
            test_end_ms=30_000,
        )

        model_fit, model_selection, contract = (
            probe.build_fit_internal_model_selection_indices(
                timestamps,
                split,
                model_selection_window_seconds=5,
                embargo_seconds=2,
            )
        )

        self.assertEqual(timestamps[model_fit].tolist(), list(range(0, 13_000, 1000)))
        self.assertEqual(
            timestamps[model_selection].tolist(), list(range(15_000, 20_000, 1000))
        )
        self.assertEqual(contract["model_fit_end_ms"], 13_000)
        self.assertEqual(contract["model_selection_start_ms"], 15_000)
        self.assertEqual(contract["embargo_seconds"], 2)

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
                self.output_count = 0

            def set_params(self, **kwargs):
                self.iterations = int(kwargs.get("iterations", self.iterations))

            def fit(self, features, targets, **kwargs):
                target_matrix = np.asarray(targets)
                self.output_count = (
                    target_matrix.shape[1] if target_matrix.ndim == 2 else 1
                )
                return self

            def predict_proba(self, features):
                positive = np.full(len(features), 0.99)
                return np.column_stack((1.0 - positive, positive))

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
            # The fit windows contain both profitable and nonprofitable action
            # events; nested validation and OOS remain in the profitable regime
            # so the fake multi-output regressor exercises the full lifecycle.
            mid = 100.0 + np.maximum(
                0, np.arange(5000, dtype=np.float64) - 3600.0
            ) * 0.02
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
                    "model_selection_window_seconds": 300,
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
                    "min_fit_profitable_events": 1,
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
                probe,
                "build_model",
                side_effect=lambda unused, action_index=0: FakeMultiModel(),
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
