#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np


TOOLS_DIR = pathlib.Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

import run_maker_subsecond_information_experiment as experiment  # noqa: E402


def quarter(timestamp: int, offset: float) -> dict[str, float | int]:
    row: dict[str, float | int] = {"timestamp": timestamp}
    for _, prefix in experiment.SYMBOL_PREFIXES:
        row.update(
            {
                f"{prefix}mid": 100.0 + offset,
                f"{prefix}microprice": 100.01 + offset,
                f"{prefix}spread_bps": 1.0 + offset,
                f"{prefix}book_imbalance_l1": 0.1 + offset,
                f"{prefix}book_imbalance_l5": 0.2 + offset,
                f"{prefix}book_imbalance_l20": 0.3 + offset,
                f"{prefix}best_bid_size": 10.0 + offset,
                f"{prefix}best_ask_size": 11.0 + offset,
                f"{prefix}depth_slope": 2.0 + offset,
                f"{prefix}book_ofi": -0.2 + offset,
                f"{prefix}book_flow_imbalance": -0.1 + offset,
                f"{prefix}trade_imbalance": 0.05 + offset,
                f"{prefix}book_update_count": 4.0 + offset,
                f"{prefix}trade_count": 2.0 + offset,
                f"{prefix}book_mid_range_bps": 0.5 + offset,
                f"{prefix}book_flow_quote_volume": 1000.0 + offset,
                f"{prefix}buy_quote_volume": 600.0 + offset,
                f"{prefix}sell_quote_volume": 400.0 + offset,
            }
        )
    return row


class MakerSubsecondInformationExperimentTest(unittest.TestCase):
    def test_frozen_policy_validates(self) -> None:
        policy = experiment.validate_policy(
            ROOT / "config" / "maker_subsecond_information_experiment.json"
        )
        self.assertEqual(
            experiment.FROZEN_POLICY_IDENTITY_SHA256,
            experiment.common.canonical_sha256(policy),
        )
        self.assertEqual(len(experiment.SUBSECOND_FEATURE_NAMES), 51)

    def test_complete_quarters_build_finite_features(self) -> None:
        rows = [
            quarter(1_000 + index * 250, float(index)) for index in range(4)
        ]
        timestamp, values = experiment.summarize_subsecond_quarters(rows)
        self.assertEqual(timestamp, 1_000)
        self.assertEqual(values.shape, (51,))
        self.assertTrue(np.all(np.isfinite(values)))
        self.assertGreater(values[0], 0.0)
        self.assertAlmostEqual(values[2], 3.0)
        self.assertGreater(values[12], 0.25)

    def test_incomplete_quarters_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "four quarters"):
            experiment.summarize_subsecond_quarters(
                [quarter(1_000, 0.0), quarter(1_250, 1.0)]
            )
        with self.assertRaisesRegex(ValueError, "incomplete or misaligned"):
            experiment.summarize_subsecond_quarters(
                [
                    quarter(1_000, 0.0),
                    quarter(1_250, 1.0),
                    quarter(1_500, 2.0),
                    quarter(2_000, 3.0),
                ]
            )

    def test_raw_replay_is_checksum_and_message_count_bound(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            raw_path = pathlib.Path(td) / "segment.jsonl"
            raw_path.write_text("{}\n{}\n", encoding="utf-8")
            assessment = {
                "segments": [
                    {
                        "capture_schema_version": (
                            experiment.collector.SCHEMA_VERSION
                        ),
                        "symbols": list(experiment.collector.CAPTURE_SYMBOLS),
                        "raw_path": str(raw_path),
                        "raw_sha256": experiment.common.sha256_file(raw_path),
                        "raw_message_count": 2,
                    }
                ]
            }
            replay_rows = [
                quarter(1_000 + index * 250, float(index))
                for index in range(4)
            ]
            with mock.patch.object(
                experiment.collector,
                "replay_jsonl",
                return_value=(replay_rows, 2),
            ):
                result = experiment.load_subsecond_features(
                    assessment, bucket_ms=250
                )
            self.assertEqual(result["timestamp"].tolist(), [1_000])
            self.assertEqual(result["features"].shape, (1, 51))
            self.assertEqual(result["audit"]["raw_message_count"], 2)
            assessment["segments"][0]["raw_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                experiment.load_subsecond_features(assessment, bucket_ms=250)

    def test_decomposed_training_rows_keep_fill_outcomes_out_of_features(self) -> None:
        actions = [
            {"direction": direction, "horizon_seconds": horizon}
            for direction in ("long", "short")
            for horizon in (15, 30)
        ]
        features = np.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
        fills = np.asarray(
            [[10, 10, -1, -1], [-1, -1, 20, 20]], dtype=np.int64
        )
        outcomes = np.asarray(
            [[2.0, -1.0, np.nan, np.nan], [np.nan, np.nan, 3.0, -2.0]]
        )
        utilities = np.nan_to_num(outcomes, nan=0.0) - np.isfinite(outcomes) * 1.0
        fill_x, fill_y = experiment.build_fill_training_rows(
            features, fills, actions
        )
        profit_x, profit_y = experiment.build_profitability_training_rows(
            features, outcomes, utilities, actions
        )
        self.assertEqual(fill_x.shape, (4, 3))
        self.assertEqual(fill_y.tolist(), [1.0, 0.0, 0.0, 1.0])
        self.assertEqual(profit_x.shape, (4, 4))
        self.assertEqual(profit_y.tolist(), [1.0, 0.0, 1.0, 0.0])
        self.assertTrue(np.array_equal(fill_x[:, :2], np.repeat(features, 2, axis=0)))

    @unittest.skipIf(
        experiment.development.CatBoostClassifier is None,
        "catboost is available in the research image",
    )
    def test_decomposed_model_fits_and_predicts_all_actions(self) -> None:
        policy = experiment.validate_policy(
            ROOT / "config" / "maker_subsecond_information_experiment.json"
        )
        actions = [
            {"direction": direction, "horizon_seconds": horizon}
            for direction in ("long", "short")
            for horizon in (15, 30)
        ]
        rng = np.random.default_rng(7)
        features = rng.normal(size=(180, 6))
        fills = np.full((180, 4), -1, dtype=np.int64)
        outcomes = np.full((180, 4), np.nan, dtype=np.float64)
        for row in range(180):
            for action in range(4):
                if (row + action) % 3:
                    fills[row, action] = 1_000_000 + row
                    outcomes[row, action] = (
                        5.0 if (row + action) % 2 else -5.0
                    )
        utilities = np.nan_to_num(outcomes, nan=0.0) - np.isfinite(outcomes) * 2.0
        fitted, diagnostics = experiment.fit_decomposed_model(
            fit_features=features[:120],
            fit_outcomes=outcomes[:120],
            fit_utilities=utilities[:120],
            fit_fills=fills[:120],
            selection_features=features[120:150],
            selection_outcomes=outcomes[120:150],
            selection_utilities=utilities[120:150],
            selection_fills=fills[120:150],
            actions=actions,
            policy=policy,
            seed_offset=0,
        )
        prediction = experiment.predict_decomposed_scores(
            fitted, features[150:], actions
        )
        self.assertEqual(prediction["score"].shape, (30, 4))
        self.assertTrue(np.all(prediction["score"] >= 0.0))
        self.assertTrue(np.all(prediction["score"] <= 1.0))
        self.assertEqual(diagnostics["market_feature_count"], 6)

    def test_decision_gate_requires_economics_and_incremental_discrimination(self) -> None:
        policy = experiment.validate_policy(
            ROOT / "config" / "maker_subsecond_information_experiment.json"
        )
        treatment_summary = {
            "fully_verifiable": True,
            "trade_count": 60,
            "oos_base_cost_by_split": {"lcb_bps": 2.0},
            "oos_stress_cost_by_split": {"lcb_bps": 1.0},
            "prediction_permutation_control": {"passed": True},
        }
        comparison = {
            "fully_verifiable": True,
            "architectures": {
                experiment.VARIANT_IDS[0]: {
                    "fully_verifiable": True,
                    "trade_count": 60,
                    "oos_stress_cost_by_split": {"lcb_bps": -1.0},
                },
                experiment.VARIANT_IDS[1]: treatment_summary,
            },
        }
        split_reports = []
        for split_id in range(6):
            architectures = {}
            for variant_id, stress, profit_auc in (
                (experiment.VARIANT_IDS[0], 0.5, 0.55),
                (experiment.VARIANT_IDS[1], 2.0, 0.58),
            ):
                architectures[variant_id] = {
                    "oos_objective": {
                        "stress_cost": {"mean_bps": stress},
                    },
                    "oos_decomposition_ranking": {
                        "fill": {"roc_auc": 0.65},
                        "profitability_conditional_on_fill": {
                            "roc_auc": profit_auc
                        },
                    },
                }
            split_reports.append(
                {"split_id": split_id, "architectures": architectures}
            )
        decision, reasons, diagnostics = experiment.add_decision_gates(
            comparison, split_reports=split_reports, policy=policy
        )
        self.assertEqual(decision, experiment.DECISION_CONTINUE)
        self.assertEqual(reasons, ["subsecond_information_gate_passed"])
        self.assertTrue(diagnostics["decision_gate_passed"])
        treatment_summary["prediction_permutation_control"]["passed"] = False
        decision, reasons, _ = experiment.add_decision_gates(
            comparison, split_reports=split_reports, policy=policy
        )
        self.assertEqual(decision, experiment.DECISION_STOP)
        self.assertIn("subsecond_permutation_control_failed", reasons)


if __name__ == "__main__":
    unittest.main()
