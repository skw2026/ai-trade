#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import sys
import tempfile
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
            "bid_depth_l5": np.ones(row_count, dtype=np.float64),
            "ask_depth_l5": np.ones(row_count, dtype=np.float64),
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
        self.assertEqual(
            policy["fill_proxy"]["resting_queue_depth_source"],
            "same_side_l5_cumulative_base_depth_at_placement",
        )
        self.assertFalse(policy["authorities"]["demo_activation_authorized"])
        self.assertEqual(
            policy["single_variable_change"],
            "replace_maximum_horizon_occupancy_with_realized_exit_settlement_timestamp",
        )
        self.assertEqual(
            policy["actions"]["exit_execution"],
            "passive_take_profit_horizon_taker_fallback",
        )
        self.assertEqual(policy["actions"]["take_profit_bps"], 10.0)
        self.assertEqual(
            policy["actions"]["occupancy_release"],
            "realized_exit_settlement_timestamp",
        )
        self.assertEqual(policy["costs"]["maker_exit_fee_bps"], 2.75)

    def test_long_fill_requires_queue_consumption_and_strict_trade_through(self):
        series = self.series()
        series["sell_quote_volume"][1] = 1000.0  # placement second is ignored
        series["sell_quote_volume"][2] = 125.0
        series["best_bid"][2] = 99.9
        series["best_bid"][4] = 101.0
        series["best_ask"][4] = 101.2

        outcomes, fills, _, actions, audit = experiment.build_maker_action_returns(
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
        outcomes, _, _, _, _ = experiment.build_maker_action_returns(
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
        outcomes, _, _, _, _ = experiment.build_maker_action_returns(
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

        outcomes, fills, _, actions, _ = experiment.build_maker_action_returns(
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

    def test_runtime_aligned_offset_and_single_reprice_change_posted_price(self):
        series = self.series(12)
        # Initial passive attempt does not fill. The replacement is submitted
        # after 2s, moves 0.15bps toward touch, then applies the 0.30bps maker
        # offset exactly like the runtime adapter.
        series["sell_quote_volume"][4] = 1000.0
        series["bid_depth_l5"][:] = 2.0
        series["best_bid"][4] = 99.0
        series["best_bid"][6] = 101.0
        series["best_ask"][6] = 101.2
        outcomes, fills, _, _, audit = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[2],
            placement_latency_seconds=1,
            fill_timeout_seconds=4,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
            maker_price_offset_bps=0.3,
            price_tick_size=0.01,
            post_only_timeout_seconds=2,
            reprice_max_attempts=1,
            reprice_bps=0.15,
        )
        expected_posted = 99.99
        self.assertEqual(fills[0, 0], 4000)
        expected_edge = (101.0 / expected_posted - 1.0) * 10000.0 - 9.25
        self.assertAlmostEqual(outcomes[0, 0], expected_edge)
        self.assertEqual(audit["reprice_max_attempts"], 1)
        self.assertEqual(audit["maker_price_offset_bps"], 0.3)
        self.assertEqual(audit["price_tick_size"], 0.01)
        self.assertEqual(
            audit["resting_queue_depth_source"],
            "same_side_l5_cumulative_base_depth_at_placement",
        )

    def test_l5_resting_depth_is_used_instead_of_top_size(self):
        series = self.series(8)
        series["bid_depth_l5"][:] = 4.0
        series["sell_quote_volume"][2] = 300.0
        series["best_bid"][2] = 99.0
        outcomes, fills, _, _, _ = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[2],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
        )
        self.assertEqual(fills[0, 0], -1)
        self.assertTrue(np.isnan(outcomes[0, 0]))

    def test_maker_exit_fill_uses_exit_queue_and_lower_round_trip_cost(self):
        series = self.series(12)
        series["sell_quote_volume"][2] = 125.0
        series["best_bid"][2] = 99.9
        # Entry fills at t=2. Horizon=2s, exit placement is t=5 and the
        # passive sell fills at t=6 only after ask trade-through + queue volume.
        series["buy_quote_volume"][6] = 126.0
        series["best_ask"][6] = 100.3

        outcomes, fills, _, actions, audit = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[2],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
            exit_execution="maker_timeout_taker_fallback",
            maker_entry_fee_bps=2.75,
            maker_exit_fee_bps=2.75,
            taker_exit_fee_bps=5.5,
            exit_slippage_bps=1.0,
            exit_placement_latency_seconds=1,
            exit_timeout_seconds=2,
            exit_post_only_timeout_seconds=1,
            exit_reprice_max_attempts=1,
            exit_reprice_bps=0.0,
        )

        self.assertEqual(fills[0, 0], 2000)
        self.assertEqual(actions[0]["settlement_seconds"], 5)
        expected = (100.2 / 100.0 - 1.0) * 10000.0 - 5.5
        self.assertAlmostEqual(outcomes[0, 0], expected)
        self.assertGreaterEqual(audit["maker_exit_action_count"], 1)
        self.assertTrue(audit["stress_increment_uses_maximum_fallback_cost"])

    def test_unfilled_maker_exit_falls_back_to_taker_after_timeout(self):
        series = self.series(12)
        series["sell_quote_volume"][2] = 125.0
        series["best_bid"][2] = 99.9
        series["best_bid"][7] = 101.0
        series["best_ask"][7] = 101.2

        outcomes, _, _, _, audit = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[2],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
            exit_execution="maker_timeout_taker_fallback",
            maker_entry_fee_bps=2.75,
            maker_exit_fee_bps=2.75,
            taker_exit_fee_bps=5.5,
            exit_slippage_bps=1.0,
            exit_placement_latency_seconds=1,
            exit_timeout_seconds=2,
            exit_post_only_timeout_seconds=1,
            exit_reprice_max_attempts=1,
            exit_reprice_bps=0.0,
        )

        expected = (101.0 / 100.0 - 1.0) * 10000.0 - 9.25
        self.assertAlmostEqual(outcomes[0, 0], expected)
        self.assertGreaterEqual(audit["taker_fallback_action_count"], 1)

    def test_first_passage_take_profit_fills_before_horizon_with_maker_cost(self):
        series = self.series(24)
        series["sell_quote_volume"][2] = 125.0
        series["best_bid"][2] = 99.9
        # Entry fills at 100.00.  A 10bps long take-profit is posted at 100.10
        # at t=3 and receives strict ask trade-through plus queue volume at t=5.
        series["buy_quote_volume"][5] = 126.0
        series["best_ask"][5] = 100.2

        outcomes, fills, settlements, actions, audit = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[10],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
            price_tick_size=0.01,
            exit_execution="passive_take_profit_horizon_taker_fallback",
            maker_entry_fee_bps=2.75,
            maker_exit_fee_bps=2.75,
            taker_exit_fee_bps=5.5,
            exit_slippage_bps=1.0,
            exit_placement_latency_seconds=1,
            take_profit_bps=10.0,
        )

        self.assertEqual(fills[0, 0], 2000)
        self.assertEqual(settlements[0, 0], 5000)
        self.assertEqual(actions[0]["settlement_seconds"], 10)
        self.assertAlmostEqual(outcomes[0, 0], 4.5)
        self.assertGreaterEqual(audit["maker_exit_action_count"], 1)
        self.assertEqual(audit["post_only_marketable_fallback_count"], 0)
        self.assertEqual(audit["settled_action_count"], audit["filled_action_count"])
        self.assertEqual(
            audit["occupancy_release"],
            "realized_exit_settlement_timestamp",
        )

    def test_first_passage_take_profit_timeout_uses_horizon_taker_fallback(self):
        series = self.series(24)
        series["sell_quote_volume"][2] = 125.0
        series["best_bid"][2] = 99.9
        series["best_bid"][12] = 100.4
        series["best_ask"][12] = 100.6

        outcomes, _, settlements, _, audit = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[10],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
            price_tick_size=0.01,
            exit_execution="passive_take_profit_horizon_taker_fallback",
            maker_entry_fee_bps=2.75,
            maker_exit_fee_bps=2.75,
            taker_exit_fee_bps=5.5,
            exit_slippage_bps=1.0,
            exit_placement_latency_seconds=1,
            take_profit_bps=10.0,
        )

        self.assertAlmostEqual(outcomes[0, 0], 30.75)
        self.assertEqual(settlements[0, 0], 12000)
        self.assertGreaterEqual(audit["taker_fallback_action_count"], 1)

    def test_marketable_take_profit_is_charged_as_immediate_taker_fallback(self):
        series = self.series(24)
        series["sell_quote_volume"][2] = 125.0
        series["best_bid"][2] = 99.9
        series["best_bid"][3] = 100.2
        series["best_ask"][3] = 100.4

        outcomes, _, settlements, _, audit = experiment.build_maker_action_returns(
            series,
            horizons_seconds=[10],
            placement_latency_seconds=1,
            fill_timeout_seconds=2,
            queue_depth_multiplier=1.25,
            base_cost_bps=9.25,
            price_tick_size=0.01,
            exit_execution="passive_take_profit_horizon_taker_fallback",
            maker_entry_fee_bps=2.75,
            maker_exit_fee_bps=2.75,
            taker_exit_fee_bps=5.5,
            exit_slippage_bps=1.0,
            exit_placement_latency_seconds=1,
            take_profit_bps=10.0,
        )

        self.assertAlmostEqual(outcomes[0, 0], 10.75)
        self.assertEqual(settlements[0, 0], 3000)
        self.assertGreaterEqual(audit["post_only_marketable_fallback_count"], 1)

    def test_oracle_selects_only_positive_stress_filled_actions_and_nonoverlaps(self):
        timestamps = np.arange(6, dtype=np.int64) * 1000
        outcomes = np.full((6, 2), np.nan, dtype=np.float64)
        fills = np.full((6, 2), -1, dtype=np.int64)
        settlements = np.full((6, 2), -1, dtype=np.int64)
        outcomes[0] = [5.0, 2.0]
        fills[0] = [1000, 1000]
        settlements[0] = [3000, 2000]
        outcomes[1, 0] = 20.0  # skipped because the first 2s action is still open
        fills[1, 0] = 2000
        settlements[1, 0] = 4000
        outcomes[4, 1] = 6.0
        fills[4, 1] = 5000
        settlements[4, 1] = 6000
        actions = [
            {"direction": "long", "horizon_seconds": 2},
            {"direction": "short", "horizon_seconds": 1},
        ]

        report = experiment.evaluate_fill_aware_oracle(
            timestamps=timestamps,
            outcomes=outcomes,
            fill_timestamps=fills,
            settlement_timestamps=settlements,
            actions=actions,
            indices=np.arange(6, dtype=np.int64),
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )

        self.assertEqual(report["base_cost"]["count"], 2)
        self.assertEqual(report["action_counts"], {"long_2s": 1, "short_1s": 1})
        self.assertAlmostEqual(report["stress_cost"]["mean_bps"], 4.5)

    def test_oracle_releases_occupancy_at_realized_settlement_not_max_horizon(self):
        timestamps = np.arange(8, dtype=np.int64) * 1000
        outcomes = np.full((8, 1), np.nan, dtype=np.float64)
        fills = np.full((8, 1), -1, dtype=np.int64)
        settlements = np.full((8, 1), -1, dtype=np.int64)
        outcomes[0, 0] = 5.0
        fills[0, 0] = 1000
        settlements[0, 0] = 2000
        outcomes[2, 0] = 6.0
        fills[2, 0] = 3000
        settlements[2, 0] = 4000

        report = experiment.evaluate_fill_aware_oracle(
            timestamps=timestamps,
            outcomes=outcomes,
            fill_timestamps=fills,
            settlement_timestamps=settlements,
            actions=[
                {
                    "direction": "long",
                    "horizon_seconds": 5,
                    "settlement_seconds": 5,
                }
            ],
            indices=np.arange(8, dtype=np.int64),
            base_cost_bps=4.0,
            stress_cost_multiplier=1.25,
        )

        self.assertEqual(report["base_cost"]["count"], 2)
        self.assertEqual(report["mean_position_lifetime_seconds"], 1.0)

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

    def test_frozen_audit_manifest_keeps_absolute_splits_as_capture_advances(self):
        policy = experiment.validate_policy(
            TOOLS_DIR.parent / "config" / "maker_execution_opportunity_experiment.json"
        )
        series = self.series(140_000)
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "audit.json"
            manifest, created = experiment.load_or_create_frozen_audit_manifest(
                path, series=series, policy=policy
            )
            self.assertTrue(created)
            frozen_splits = manifest["primary_splits"]
            extension = self.series(120)
            extension["timestamp"] += int(series["timestamp"][-1]) + 1000
            advanced = {
                name: np.concatenate((values, extension[name]))
                for name, values in series.items()
            }
            loaded, created_again = experiment.load_or_create_frozen_audit_manifest(
                path, series=advanced, policy=policy
            )
            self.assertFalse(created_again)
            self.assertEqual(loaded["primary_splits"], frozen_splits)
            self.assertEqual(
                loaded["independent_forward"]["start_ms"],
                frozen_splits[-1]["test_end_ms"],
            )

    def test_frozen_audit_manifest_rejects_historical_price_drift(self):
        policy = experiment.validate_policy(
            TOOLS_DIR.parent / "config" / "maker_execution_opportunity_experiment.json"
        )
        series = self.series(140_000)
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "audit.json"
            experiment.load_or_create_frozen_audit_manifest(
                path, series=series, policy=policy
            )
            drifted = {name: values.copy() for name, values in series.items()}
            drifted["best_bid"][-100] += 0.01
            with self.assertRaisesRegex(ValueError, "domain drift"):
                experiment.load_or_create_frozen_audit_manifest(
                    path, series=drifted, policy=policy
                )

    def test_v5_inherits_v2_absolute_splits_and_starts_new_unseen_forward(self):
        policy = experiment.validate_policy(
            TOOLS_DIR.parent / "config" / "maker_execution_opportunity_experiment.json"
        )
        series = self.series(140_000)
        baseline_series = self.series(130_000)
        seed = experiment.create_frozen_audit_manifest(
            series=baseline_series, policy=policy
        )
        baseline = {
            "schema_version": experiment.BASELINE_AUDIT_SCHEMA_VERSION,
            "created_at_utc": "2026-08-23T00:00:00Z",
            "policy_identity_sha256": experiment.BASELINE_POLICY_IDENTITY_SHA256,
            "experiment_id": experiment.BASELINE_EXPERIMENT_ID,
            "frozen_domain": seed["frozen_domain"],
            "primary_splits": seed["primary_splits"],
            "boundary_splits": seed["boundary_splits"],
            "independent_forward": seed["independent_forward"],
        }
        baseline["identity_sha256"] = experiment.common.canonical_sha256(baseline)
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            baseline_path = root / "v2.json"
            audit_path = root / "v5.json"
            experiment.common.atomic_write_json(baseline_path, baseline)
            manifest, created = experiment.load_or_create_frozen_audit_manifest(
                audit_path,
                series=series,
                policy=policy,
                baseline_manifest_path=baseline_path,
            )

        self.assertTrue(created)
        self.assertEqual(manifest["primary_splits"], baseline["primary_splits"])
        self.assertEqual(manifest["boundary_splits"], baseline["boundary_splits"])
        self.assertEqual(
            manifest["baseline_audit_identity_sha256"],
            baseline["identity_sha256"],
        )
        self.assertEqual(
            manifest["independent_forward"]["start_ms"],
            int(series["timestamp"][-1]) + 1000,
        )
        self.assertFalse(
            manifest["independent_forward"]["observed_before_freeze"]
        )

    def test_stability_decision_waits_then_fails_closed_or_continues(self):
        waiting = {
            "state": "AWAITING_FORWARD",
            "stable_opportunity_proven": False,
            "primary_oracle": {"opportunity_proven": True},
            "boundary_sensitivity": {"passed": True},
            "independent_forward": {"passed": False},
        }
        self.assertEqual(
            experiment.decide_stability(waiting),
            (
                experiment.DECISION_WAIT,
                ["independent_24h_forward_window_incomplete"],
            ),
        )
        failed = {
            "state": "COMPLETE",
            "stable_opportunity_proven": False,
            "primary_oracle": {"opportunity_proven": True},
            "boundary_sensitivity": {"passed": False},
            "independent_forward": {"passed": False},
        }
        decision, reasons = experiment.decide_stability(failed)
        self.assertEqual(decision, experiment.DECISION_STOP)
        self.assertEqual(
            reasons,
            ["boundary_sensitivity_failed"],
        )
        passed = {
            "state": "COMPLETE",
            "stable_opportunity_proven": True,
            "primary_oracle": {"opportunity_proven": True},
            "boundary_sensitivity": {"passed": True},
            "independent_forward": {"passed": True},
        }
        self.assertEqual(
            experiment.decide_stability(passed)[0], experiment.DECISION_CONTINUE
        )

    def test_stability_decision_stops_before_forward_when_frozen_gates_failed(self):
        audit = {
            "state": "AWAITING_FORWARD",
            "stable_opportunity_proven": False,
            "primary_oracle": {"opportunity_proven": False},
            "boundary_sensitivity": {"passed": False},
            "independent_forward": {"passed": False},
        }

        self.assertEqual(
            experiment.decide_stability(audit),
            (
                experiment.DECISION_STOP,
                [
                    "frozen_primary_opportunity_failed",
                    "boundary_sensitivity_failed",
                ],
            ),
        )


if __name__ == "__main__":
    unittest.main()
