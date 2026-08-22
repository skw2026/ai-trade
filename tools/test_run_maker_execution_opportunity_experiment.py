#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import sys
import unittest

import numpy as np


TOOLS_DIR = pathlib.Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import run_maker_execution_opportunity_experiment as experiment


class MakerExecutionOpportunityExperimentTest(unittest.TestCase):
    @staticmethod
    def series(row_count: int = 10) -> dict[str, np.ndarray]:
        return {
            "timestamp": np.arange(row_count, dtype=np.int64) * 1000,
            "best_bid": np.full(row_count, 100.0, dtype=np.float64),
            "best_ask": np.full(row_count, 100.2, dtype=np.float64),
            "best_bid_size": np.ones(row_count, dtype=np.float64),
            "best_ask_size": np.ones(row_count, dtype=np.float64),
            "buy_quote_volume": np.zeros(row_count, dtype=np.float64),
            "sell_quote_volume": np.zeros(row_count, dtype=np.float64),
        }

    def test_frozen_policy_identity_and_safety_contract(self):
        policy = experiment.validate_policy(
            TOOLS_DIR.parent / "config" / "maker_execution_opportunity_experiment.json"
        )

        self.assertFalse(policy["promotion_evidence"])
        self.assertEqual(policy["splits"]["count"], 6)
        self.assertFalse(policy["fill_proxy"]["same_second_fill_permitted"])
        self.assertFalse(policy["fill_proxy"]["fill_proxy_used_as_model_feature"])
        self.assertFalse(policy["authorities"]["demo_activation_authorized"])

    def test_long_fill_requires_queue_consumption_and_strict_trade_through(self):
        series = self.series()
        series["sell_quote_volume"][1] = 1000.0  # placement second is ignored
        series["sell_quote_volume"][2] = 125.0
        series["best_bid"][2] = 99.9
        series["best_bid"][4] = 101.0
        series["best_ask"][4] = 101.2

        outcomes, fills, actions, audit = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[2],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
        )

        self.assertEqual(actions[0], {"direction": "long", "horizon_seconds": 2})
        self.assertEqual(fills[0, 0], 2000)
        self.assertAlmostEqual(outcomes[0, 0], 90.75)
        self.assertTrue(np.isnan(outcomes[0, 1]))
        self.assertGreaterEqual(audit["filled_decision_count"], 1)
        self.assertFalse(audit["same_second_fill_permitted"])

    def test_touch_without_trade_through_or_queue_volume_does_not_fill(self):
        series = self.series()
        series["sell_quote_volume"][2] = 124.99
        series["best_bid"][2] = 99.9
        outcomes, _, _, _ = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[2],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
        )
        self.assertTrue(np.isnan(outcomes[0, 0]))

        series["sell_quote_volume"][2] = 1000.0
        series["best_bid"][2] = 100.0
        outcomes, _, _, _ = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[2],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
        )
        self.assertTrue(np.isnan(outcomes[0, 0]))

    def test_short_fill_uses_buy_volume_and_ask_trade_through(self):
        series = self.series()
        series["buy_quote_volume"][2] = 126.0
        series["best_ask"][2] = 100.3
        series["best_ask"][4] = 99.2
        series["best_bid"][4] = 99.0

        outcomes, fills, actions, _ = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[2],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
        )

        self.assertEqual(actions[1], {"direction": "short", "horizon_seconds": 2})
        self.assertEqual(fills[0, 1], 2000)
        expected = (100.2 / 99.2 - 1.0) * 10000.0 - 9.25
        self.assertAlmostEqual(outcomes[0, 1], expected)

    def test_oracle_selects_only_positive_stress_filled_actions_and_nonoverlaps(self):
        timestamps = np.arange(6, dtype=np.int64) * 1000
        outcomes = np.full((6, 2), np.nan, dtype=np.float64)
        fills = np.full((6, 2), -1, dtype=np.int64)
        outcomes[0] = [5.0, 2.0]
        fills[0] = [1000, 1000]
        outcomes[1, 0] = 20.0  # skipped because the first 2s action is still open
        fills[1, 0] = 2000
        outcomes[4, 1] = 6.0
        fills[4, 1] = 5000
        actions = [
            {"direction": "long", "horizon_seconds": 2},
            {"direction": "short", "horizon_seconds": 1},
        ]

        report = experiment.evaluate_fill_aware_oracle(
            timestamps=timestamps,
            outcomes=outcomes,
            fill_timestamps=fills,
            actions=actions,
            indices=np.arange(6, dtype=np.int64),
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )

        self.assertEqual(report["base_cost"]["count"], 2)
        self.assertEqual(report["action_counts"], {"long_2s": 1, "short_1s": 1})
        self.assertAlmostEqual(report["stress_cost"]["mean_bps"], 4.5)

    def test_decision_continues_only_after_all_oracle_gates(self):
        policy = {
            "decision_gates": {
                "minimum_oos_trades": 30,
                "minimum_positive_split_ratio": 0.6,
                "minimum_oracle_stress_lcb_bps": 0.0,
            }
        }
        passed = {
            "fully_verifiable": True,
            "opportunity_proven": True,
            "trade_count": 60,
            "positive_stress_split_ratio": 1.0,
            "stress_cost_by_split": {"lcb_bps": 0.25},
        }
        decision, reasons = experiment.decide(passed, policy)
        self.assertEqual(decision, experiment.DECISION_CONTINUE)
        self.assertEqual(reasons, ["maker_oracle_all_first_window_gates_passed"])

        failed = {
            **passed,
            "opportunity_proven": False,
            "trade_count": 20,
            "positive_stress_split_ratio": 0.5,
            "stress_cost_by_split": {"lcb_bps": -0.1},
        }
        decision, reasons = experiment.decide(failed, policy)
        self.assertEqual(decision, experiment.DECISION_STOP)
        self.assertEqual(
            reasons,
            [
                "maker_oracle_trade_count_below_minimum",
                "maker_oracle_positive_split_ratio_below_minimum",
                "maker_oracle_stress_lcb_not_positive",
            ],
        )


if __name__ == "__main__":
    unittest.main()
