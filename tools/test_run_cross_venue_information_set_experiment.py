#!/usr/bin/env python3

import argparse
import json
import pathlib
import tempfile
import unittest

import numpy as np

import collect_binance_microstructure as external
import run_cross_venue_information_set_experiment as experiment


class CrossVenueInformationSetExperimentTest(unittest.TestCase):
    def setUp(self):
        self.config = pathlib.Path(__file__).resolve().parents[1] / "config" / "cross_venue_information_set_experiment.json"
        self.policy = experiment.validate_policy(self.config)

    def test_frozen_policy_is_research_only_and_single_variable(self):
        self.assertEqual(self.policy["architecture_id"], experiment.ARCHITECTURE_ID)
        self.assertEqual(self.policy["splits"]["count"], 6)
        self.assertEqual(self.policy["costs"]["additional_round_trip_cost_bps"], 11.0)
        self.assertEqual(
            self.policy["single_variable_change"],
            "add_binance_usdm_sol_l20_and_aggtrade_features",
        )
        self.assertEqual(
            self.policy["authorities"],
            {
                "promotion_authority": False,
                "demo_activation_authorized": False,
                "live_activation_authorized": False,
            },
        )

    def test_external_alignment_uses_exact_previous_second_without_backfill(self):
        timestamps = np.asarray([2000, 3000], dtype=np.int64)
        control = {"timestamp": timestamps}
        for field in external.OUTPUT_FIELDS[1:]:
            control[field] = np.asarray([10.0, 10.0], dtype=np.float64)
        control["mid"] = np.asarray([100.0, 100.0])
        control["spread_bps"] = np.asarray([1.0, 1.0])
        control["book_imbalance_l1"] = np.asarray([0.1, 0.1])
        control["book_imbalance_l5"] = np.asarray([0.1, 0.1])
        control["book_imbalance_l20"] = np.asarray([0.1, 0.1])
        control["trade_imbalance"] = np.asarray([0.1, 0.1])
        external_rows = {"timestamp": np.asarray([0, 1000, 3000], dtype=np.int64)}
        for field in external.OUTPUT_FIELDS[1:]:
            external_rows[field] = np.asarray([9.0, 10.0, 30.0], dtype=np.float64)
        external_rows["mid"] = np.asarray([99.0, 100.0, 300.0])
        external_rows["microprice"] = np.asarray([99.0, 100.0, 300.0])
        matrix, names, audit = experiment.build_external_features(
            control, external_rows, lag_seconds=1
        )
        self.assertTrue(np.all(np.isfinite(matrix[0])))
        self.assertTrue(np.any(~np.isfinite(matrix[1])))
        self.assertEqual(audit["aligned_row_count"], 1)
        self.assertEqual(audit["missing_external_row_count"], 1)
        self.assertFalse(audit["future_fill_permitted"])
        self.assertEqual(names[0], "binance_lag1_best_bid")

    @staticmethod
    def _treatment(*, verified=True, signal=True, stress_lcb=1.0, permutation=True):
        return {
            "aggregate": {
                "architecture_summaries": {
                    experiment.ARCHITECTURE_ID: {
                        "fully_verifiable": verified,
                        "signal_proven": signal,
                        "trade_count": 60,
                        "oos_stress_cost_by_split": {
                            "lcb_bps": stress_lcb,
                            "positive_ratio": 1.0,
                        },
                        "prediction_permutation_control": {"passed": permutation},
                    }
                }
            }
        }

    @staticmethod
    def _paired(*, verified=True, lcb=0.5, permutation=True):
        return {
            "fully_verifiable": verified,
            "stress_cost_delta_by_split": {"lcb_bps": lcb},
            "permutation_null": {"passed": permutation},
        }

    def test_decision_table_stops_family_source_or_continues(self):
        decision, _ = experiment.decide(
            oracle={"fully_verifiable": True, "opportunity_proven": False},
            treatment=self._treatment(),
            paired=self._paired(),
            policy=self.policy,
        )
        self.assertEqual(decision, "STOP_CURRENT_RESEARCH_FAMILY")
        decision, reasons = experiment.decide(
            oracle={"fully_verifiable": True, "opportunity_proven": True},
            treatment=self._treatment(stress_lcb=-0.1, permutation=False),
            paired=self._paired(lcb=-0.1, permutation=False),
            policy=self.policy,
        )
        self.assertEqual(decision, "STOP_INFORMATION_SOURCE")
        self.assertEqual(len(reasons), 4)
        decision, _ = experiment.decide(
            oracle={"fully_verifiable": True, "opportunity_proven": True},
            treatment=self._treatment(),
            paired=self._paired(),
            policy=self.policy,
        )
        self.assertEqual(decision, "CONTINUE_TO_SECOND_INDEPENDENT_24H")

    def test_not_ready_report_exposes_true_common_span_and_no_authority(self):
        args = argparse.Namespace(config=str(self.config))
        report = experiment.not_ready_report(args, "collecting")
        self.assertEqual(report["status"], "NOT_READY")
        self.assertGreater(report["minimum_common_span_seconds_for_frozen_splits"], 86400)
        self.assertFalse(report["promotion_authority"])
        self.assertFalse(report["demo_activation_authorized"])
        self.assertFalse(report["live_activation_authorized"])

    def test_invalid_policy_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "policy.json"
            payload = json.loads(self.config.read_text(encoding="utf-8"))
            payload["costs"]["additional_round_trip_cost_bps"] = 1.0
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "costs"):
                experiment.validate_policy(path)

    def test_internal_model_selection_uses_window_scaled_minimum_rows(self):
        audit = experiment.validate_split_row_coverage(
            split_id=0,
            model_fit=np.arange(11_606),
            model_selection=np.arange(1_958),
            validation=np.arange(8_697),
            test=np.arange(8_913),
            split_policy=self.policy["splits"],
        )

        self.assertEqual(audit["minimum_rows"]["model_fit"], 3600)
        self.assertEqual(audit["minimum_rows"]["model_selection"], 600)
        self.assertEqual(audit["actual_rows"]["model_selection"], 1958)

        with self.assertRaisesRegex(
            experiment.ExperimentNotReady,
            "model_selection=599<600",
        ):
            experiment.validate_split_row_coverage(
                split_id=0,
                model_fit=np.arange(11_606),
                model_selection=np.arange(599),
                validation=np.arange(8_697),
                test=np.arange(8_913),
                split_policy=self.policy["splits"],
            )

        with self.assertRaisesRegex(
            experiment.ExperimentNotReady,
            "model_fit=3599<3600",
        ):
            experiment.validate_split_row_coverage(
                split_id=0,
                model_fit=np.arange(3_599),
                model_selection=np.arange(1_958),
                validation=np.arange(8_697),
                test=np.arange(8_913),
                split_policy=self.policy["splits"],
            )

    def test_unlisted_hyperparameter_drift_is_rejected_by_frozen_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "policy.json"
            payload = json.loads(self.config.read_text(encoding="utf-8"))
            payload["model"]["depth"] = 5
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "policy_identity"):
                experiment.validate_policy(path)


if __name__ == "__main__":
    unittest.main()
