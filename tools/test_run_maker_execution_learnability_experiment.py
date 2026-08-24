#!/usr/bin/env python3

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from copy import deepcopy

import numpy as np


TOOLS_DIR = pathlib.Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import run_cross_venue_information_set_experiment as common
import run_maker_execution_learnability_experiment as experiment


class MakerExecutionLearnabilityExperimentTest(unittest.TestCase):
    def setUp(self):
        self.policy_path = (
            TOOLS_DIR.parent
            / "config"
            / "maker_execution_learnability_experiment.json"
        )
        self.policy = experiment.validate_policy(self.policy_path)

    def test_frozen_policy_keeps_fill_proxy_out_of_features_and_authority_off(self):
        self.assertEqual(self.policy["architectures"], list(experiment.ARCHITECTURE_IDS))
        self.assertFalse(self.policy["features"]["fill_proxy_used_as_model_feature"])
        self.assertFalse(self.policy["features"]["future_values_permitted"])
        self.assertFalse(self.policy["authorities"]["demo_activation_authorized"])
        self.assertEqual(experiment.total_base_cost_bps(self.policy), 9.25)

    def test_observable_mask_requires_every_fill_probe_and_exit(self):
        timestamps = np.arange(11, dtype=np.int64) * 1000
        mask = experiment.build_observable_decision_mask(
            timestamps,
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            horizons_seconds=[2],
        )
        self.assertEqual(np.flatnonzero(mask).tolist(), [0, 1, 2, 3, 4, 5])

        timestamps = np.delete(timestamps, 3)
        mask = experiment.build_observable_decision_mask(
            timestamps,
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            horizons_seconds=[2],
        )
        self.assertFalse(mask[0])

    def test_unfilled_utility_is_zero_and_fill_bears_stress_increment(self):
        outcomes = np.asarray([[np.nan, 5.0], [-2.0, np.nan]], dtype=np.float64)
        utilities = experiment.build_stress_utility_targets(
            outcomes,
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )
        np.testing.assert_allclose(utilities, [[0.0, 4.0], [-3.0, 0.0]])

    def test_policy_occupies_until_timeout_or_realized_settlement(self):
        timestamps = np.arange(15, dtype=np.int64) * 1000
        scores = np.ones((15, 1), dtype=np.float64)
        outcomes = np.full((15, 1), np.nan, dtype=np.float64)
        fills = np.full((15, 1), -1, dtype=np.int64)
        settlements = np.full((15, 1), -1, dtype=np.int64)
        outcomes[6, 0] = 5.0
        fills[6, 0] = 8000
        settlements[6, 0] = 9000
        outcomes[9, 0] = 7.0
        fills[9, 0] = 11000
        settlements[9, 0] = 12000
        actions = [{"direction": "long", "horizon_seconds": 2}]

        report = experiment.evaluate_maker_policy(
            timestamps=timestamps,
            prediction=scores,
            realized_base=outcomes,
            fill_timestamps=fills,
            settlement_timestamps=settlements,
            actions=actions,
            score_threshold=0.5,
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
            placement_latency_seconds=1,
            fill_timeout_seconds=5,
        )

        self.assertEqual(report["order_count"], 4)
        self.assertEqual(report["unfilled_order_count"], 2)
        self.assertEqual(report["filled_order_count"], 2)
        self.assertEqual(report["action_counts"], {"long_2s": 2})
        self.assertAlmostEqual(report["stress_cost"]["mean_bps"], 5.0)

    def test_nested_threshold_uses_only_filled_trade_economics(self):
        timestamps = np.arange(20, dtype=np.int64) * 1000
        scores = np.arange(20, dtype=np.float64).reshape(-1, 1)
        outcomes = np.full((20, 1), np.nan, dtype=np.float64)
        fills = np.full((20, 1), -1, dtype=np.int64)
        settlements = np.full((20, 1), -1, dtype=np.int64)
        for index in (7, 16):
            outcomes[index, 0] = 6.0
            fills[index, 0] = timestamps[index] + 2000
            settlements[index, 0] = fills[index, 0] + 1000
        report = experiment.select_nested_maker_threshold(
            timestamps=timestamps,
            prediction=scores,
            realized_base=outcomes,
            fill_timestamps=fills,
            settlement_timestamps=settlements,
            actions=[{"direction": "long", "horizon_seconds": 1}],
            quantiles=[0.0, 0.5],
            minimum_trades=1,
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
            placement_latency_seconds=1,
            fill_timeout_seconds=5,
            score_units="test_score",
        )
        self.assertIsNotNone(report["diagnostic_selected"])
        self.assertEqual(report["score_units"], "test_score")
        self.assertTrue(
            all(item["trade_count"] <= item["order_count"] for item in report["candidates"])
        )

    def test_upstream_report_is_bound_to_assessment_timestamps_and_policy(self):
        timestamps = np.arange(5, dtype=np.int64) * 1000
        with tempfile.TemporaryDirectory() as raw_dir:
            root = pathlib.Path(raw_dir)
            assessment = root / "assessment.json"
            assessment.write_text("{}\n", encoding="utf-8")
            report = {
                "schema_version": "maker_execution_opportunity_experiment_v1",
                "status": "COMPLETE",
                "fully_verifiable": True,
                "promotion_evidence": False,
                "promotion_eligible": False,
                "promotion_authority": False,
                "demo_activation_authorized": False,
                "live_activation_authorized": False,
                "experiment_policy": {
                    "identity_sha256": self.policy["upstream"][
                        "required_opportunity_policy_identity_sha256"
                    ]
                },
                "input": {
                    "control_assessment_sha256": common.sha256_file(assessment)
                },
                "execution_contract": {
                    "base_cost_bps": 9.25,
                    "stress_cost_multiplier": 1.25,
                    "fill_proxy": {"fill_proxy_used_as_model_feature": False},
                },
                "common_domain": {
                    "row_count": len(timestamps),
                    "timestamp_sha256": common.array_sha256(timestamps),
                    "splits": [{} for _ in range(6)],
                },
                "research_decision": "CONTINUE_TO_MAKER_LEARNABILITY_EXPERIMENT",
            }
            report_path = root / "opportunity.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")

            validated = experiment.validate_upstream_report(
                report_path,
                assessment_path=assessment,
                timestamps=timestamps,
                policy=self.policy,
            )
            self.assertEqual(
                validated["research_decision"],
                "CONTINUE_TO_MAKER_LEARNABILITY_EXPERIMENT",
            )

            report["common_domain"]["timestamp_sha256"] = "0" * 64
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "common_domain"):
                experiment.validate_upstream_report(
                    report_path,
                    assessment_path=assessment,
                    timestamps=timestamps,
                    policy=self.policy,
                )

    def test_decision_selects_only_architecture_passing_economics_and_control(self):
        summaries = {}
        for architecture_id in experiment.ARCHITECTURE_IDS:
            passed = architecture_id == "sequential_hurdle_tail_action_value"
            summaries[architecture_id] = {
                "fully_verifiable": True,
                "trade_count": 120,
                "oos_base_cost_by_split": {"lcb_bps": 2.0 if passed else -1.0},
                "oos_stress_cost_by_split": {"lcb_bps": 1.0 if passed else -2.0},
                "prediction_permutation_control": {"passed": passed},
            }
        comparison = {"fully_verifiable": True, "architectures": summaries}
        split_reports = []
        for split_id in range(6):
            architectures = {}
            for architecture_id in experiment.ARCHITECTURE_IDS:
                passed = architecture_id == "sequential_hurdle_tail_action_value"
                architectures[architecture_id] = {
                    "oos_objective": {
                        "stress_cost": {"count": 10, "mean_bps": 1.0 if passed else -1.0}
                    }
                }
            split_reports.append({"split_id": split_id, "architectures": architectures})

        decision, leader, reasons = experiment.add_maker_decision_gates(
            comparison, split_reports=split_reports, policy=self.policy
        )
        self.assertEqual(decision, experiment.DECISION_CONTINUE)
        self.assertEqual(leader, "sequential_hurdle_tail_action_value")
        self.assertEqual(reasons, ["maker_learnability_gate_passed"])
        self.assertFalse(comparison["maker_diagnostic_leader_is_preregistered"])

    @unittest.skipIf(
        experiment.development.catboost is None,
        "catboost is available in CI and the research image",
    )
    def test_sequential_hurdle_tail_architecture_fits_with_frozen_interface(self):
        rng = np.random.default_rng(20260822)
        fit_features = rng.normal(size=(120, 8))
        selection_features = rng.normal(size=(40, 8))
        validation_features = rng.normal(size=(20, 8))
        test_features = rng.normal(size=(20, 8))

        def utilities(features):
            signal = features[:, 0]
            return np.column_stack(
                (
                    np.where(signal >= 0.0, 3.0, -2.0),
                    np.where(signal < 0.0, 3.0, -2.0),
                )
            )

        policy = deepcopy(self.policy)
        policy["model"].update(
            {"iterations": 10, "depth": 2, "early_stopping_rounds": 3}
        )
        actions = [
            {"direction": "long", "horizon_seconds": 15},
            {"direction": "short", "horizon_seconds": 15},
        ]
        fit_timestamps = np.arange(len(fit_features), dtype=np.int64) * 1000
        selection_timestamps = (
            np.arange(len(selection_features), dtype=np.int64) * 1000
        )
        fit_fills = np.tile((fit_timestamps + 2000).reshape(-1, 1), (1, 2))
        selection_fills = np.tile(
            (selection_timestamps + 2000).reshape(-1, 1), (1, 2)
        )
        fit_settlements = fit_fills + 5000
        result = experiment.fit_predict_sequential_hurdle_tail_architecture(
            fit_features=fit_features,
            fit_timestamps=fit_timestamps,
            fit_stress_utilities=utilities(fit_features),
            fit_fill_timestamps=fit_fills,
            fit_settlement_timestamps=fit_settlements,
            model_selection_features=selection_features,
            model_selection_stress_utilities=utilities(selection_features),
            model_selection_fill_timestamps=selection_fills,
            validation_features=validation_features,
            test_features=test_features,
            actions=actions,
            policy=policy,
        )
        self.assertEqual(result["validation_prediction"].shape, (20, 2))
        self.assertEqual(result["test_prediction"].shape, (20, 2))
        self.assertTrue(np.all(np.isfinite(result["test_prediction"])))

    def test_hurdle_action_value_prices_unfilled_occupancy_and_no_order(self):
        action = {"direction": "long", "horizon_seconds": 15}
        model = experiment.SequentialHurdleTailModel(
            actions=[action],
            action_models=[
                experiment.HurdleTailActionModel(
                    fill_model=None,
                    fill_constant=0.25,
                    utility_model=None,
                    utility_constant=4.0,
                    mean_fill_latency_seconds=2.0,
                    mean_position_lifetime_seconds=15.0,
                )
            ],
            opportunity_cost_bps_per_second=0.12,
            placement_latency_seconds=1,
            fill_timeout_seconds=5,
        )
        prediction = experiment.predict_sequential_hurdle_tail_action_value(
            model, np.zeros((3, 2), dtype=np.float64)
        )
        expected_occupancy = 0.25 * 18.0 + 0.75 * 6.0
        self.assertAlmostEqual(prediction[0, 0], 0.25 * 4.0 - 0.12 * expected_occupancy)
        self.assertLess(prediction[0, 0], 0.0)


if __name__ == "__main__":
    unittest.main()
