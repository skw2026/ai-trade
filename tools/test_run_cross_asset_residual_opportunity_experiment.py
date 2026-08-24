#!/usr/bin/env python3

from __future__ import annotations

import json
import math
import pathlib
import sys
import tempfile
import unittest

import numpy as np


TOOLS_DIR = pathlib.Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import run_cross_asset_residual_opportunity_experiment as experiment
import run_maker_execution_opportunity_experiment as maker
import run_microstructure_alpha_development as development


class CrossAssetResidualOpportunityExperimentTest(unittest.TestCase):
    @staticmethod
    def series(row_count: int = 26000) -> dict[str, np.ndarray]:
        timestamps = np.arange(row_count, dtype=np.int64) * 1000
        axis = np.arange(row_count, dtype=np.float64)
        btc_return = 0.00001 * np.sin(axis / 17.0)
        eth_return = 0.000012 * np.cos(axis / 23.0)
        sol_return = 0.25 * btc_return + 0.75 * eth_return
        btc_mid = 60000.0 * np.exp(np.cumsum(btc_return))
        eth_mid = 3000.0 * np.exp(np.cumsum(eth_return))
        sol_mid = 150.0 * np.exp(np.cumsum(sol_return))
        output: dict[str, np.ndarray] = {
            "timestamp": timestamps,
            "best_bid": sol_mid * (1.0 - 0.5 / 20000.0),
            "best_ask": sol_mid * (1.0 + 0.5 / 20000.0),
            "best_bid_size": np.full(row_count, 10.0),
            "best_ask_size": np.full(row_count, 11.0),
            "bid_depth_l5": np.full(row_count, 50.0),
            "ask_depth_l5": np.full(row_count, 55.0),
            "buy_quote_volume": np.full(row_count, 1000.0),
            "sell_quote_volume": np.full(row_count, 900.0),
            "btc_mid": btc_mid,
            "btc_spread_bps": np.full(row_count, 0.5),
            "btc_best_bid_size": np.full(row_count, 2.0),
            "btc_best_ask_size": np.full(row_count, 2.1),
            "btc_bid_depth_l5": np.full(row_count, 10.0),
            "btc_ask_depth_l5": np.full(row_count, 10.5),
            "btc_buy_quote_volume": np.full(row_count, 2000.0),
            "btc_sell_quote_volume": np.full(row_count, 1900.0),
            "eth_mid": eth_mid,
            "eth_spread_bps": np.full(row_count, 0.6),
            "eth_best_bid_size": np.full(row_count, 3.0),
            "eth_best_ask_size": np.full(row_count, 3.1),
            "eth_bid_depth_l5": np.full(row_count, 15.0),
            "eth_ask_depth_l5": np.full(row_count, 15.5),
            "eth_buy_quote_volume": np.full(row_count, 1800.0),
            "eth_sell_quote_volume": np.full(row_count, 1700.0),
        }
        return output

    @staticmethod
    def policy() -> dict:
        return experiment.validate_policy(
            TOOLS_DIR.parent
            / "config"
            / "cross_asset_residual_opportunity_experiment.json"
        )

    @staticmethod
    def parent_manifest(series: dict[str, np.ndarray]) -> dict:
        primary = []
        for split_id in range(6):
            start = (15000 + split_id * 1000) * 1000
            primary.append(
                development.TimeSplit(
                    split_id=split_id,
                    fit_start_ms=start,
                    fit_end_ms=start + 1201 * 1000,
                    validation_start_ms=start + 1201 * 1000,
                    validation_end_ms=start + 2201 * 1000,
                    test_start_ms=start + 2201 * 1000,
                    test_end_ms=start + 3201 * 1000,
                )
            )
        offsets = (0, -3600, -7200, -10800)
        shifted = {
            str(offset): [maker._shift_split(split, offset) for split in primary]
            for offset in offsets
        }
        frozen_start = min(
            split.fit_start_ms for values in shifted.values() for split in values
        )
        frozen_end = max(split.test_end_ms for split in primary)
        manifest = {
            "schema_version": maker.BASELINE_AUDIT_SCHEMA_VERSION,
            "created_at_utc": "2026-08-24T00:00:00Z",
            "policy_identity_sha256": maker.BASELINE_POLICY_IDENTITY_SHA256,
            "experiment_id": maker.BASELINE_EXPERIMENT_ID,
            "split_calendar_source": "initial_freeze",
            "baseline_audit_identity_sha256": None,
            "frozen_domain": maker._series_domain_identity(
                series, start_ms=frozen_start, end_ms=frozen_end
            ),
            "primary_splits": [vars(split) for split in primary],
            "boundary_splits": {
                key: [vars(split) for split in values]
                for key, values in shifted.items()
            },
            "independent_forward": {
                "start_ms": frozen_end,
                "end_ms": frozen_end + 86400 * 1000,
                "observation_end_ms": frozen_end + 86713 * 1000,
                "block_seconds": 14400,
                "block_count": 6,
                "observed_before_freeze": False,
            },
        }
        manifest["identity_sha256"] = experiment.common.canonical_sha256(manifest)
        return manifest

    def test_frozen_policy_and_cost_contract(self):
        policy = self.policy()
        self.assertEqual(
            policy["mechanism"]["hedge_weight_method"],
            "fit_only_minimum_one_second_mid_log_return_residual_variance_grid_v1",
        )
        self.assertFalse(
            policy["mechanism"][
                "test_or_boundary_outcomes_used_for_weight_selection"
            ]
        )
        self.assertEqual(experiment.base_cost_bps(policy), 26.0)
        self.assertEqual(experiment.base_cost_bps(policy, target_only=True), 13.0)
        self.assertFalse(policy["authorities"]["demo_activation_authorized"])

    def test_context_bid_ask_is_reconstructed_from_mid_and_spread(self):
        series = self.series(1200)
        quotes = experiment.reconstruct_quotes(series)
        expected_bid = series["btc_mid"][0] * (1.0 - 0.5 / 20000.0)
        expected_ask = series["btc_mid"][0] * (1.0 + 0.5 / 20000.0)
        self.assertAlmostEqual(quotes["btc"]["bid"][0], expected_bid)
        self.assertAlmostEqual(quotes["btc"]["ask"][0], expected_ask)
        self.assertTrue(np.all(quotes["eth"]["ask"] > quotes["eth"]["bid"]))

    def test_long_and_short_returns_use_entry_notional_denominator(self):
        self.assertAlmostEqual(experiment._long_return_bps(100.0, 101.0), 100.0)
        self.assertAlmostEqual(experiment._short_return_bps(100.0, 99.0), 100.0)
        self.assertAlmostEqual(experiment._long_return_bps(100.0, 99.0), -100.0)
        self.assertAlmostEqual(experiment._short_return_bps(100.0, 101.0), -100.0)

    def test_hedge_weight_uses_fit_only_variance_grid(self):
        series = self.series(1800)
        weight, audit = experiment.estimate_fit_only_btc_weight(
            series, np.arange(1800, dtype=np.int64), self.policy()
        )
        self.assertEqual(weight, 0.25)
        self.assertGreaterEqual(audit["fit_return_count"], 1000)
        self.assertFalse(audit["test_outcomes_used"])

    def test_non_overlapping_oracle_uses_full_multi_leg_stress_cost(self):
        series = self.series(2000)
        # Preserve the fit-window weight process, then create a large relative
        # SOL move in the test interval while BTC/ETH remain unchanged.
        for index in range(1300, 2000):
            multiplier = math.exp((index - 1300) * 0.0002)
            mid = 150.0 * multiplier
            series["best_bid"][index] = mid * (1.0 - 0.5 / 20000.0)
            series["best_ask"][index] = mid * (1.0 + 0.5 / 20000.0)
            series["btc_mid"][index] = series["btc_mid"][1299]
            series["eth_mid"][index] = series["eth_mid"][1299]
        split = development.TimeSplit(
            split_id=0,
            fit_start_ms=0,
            fit_end_ms=1201 * 1000,
            validation_start_ms=1201 * 1000,
            validation_end_ms=1300 * 1000,
            test_start_ms=1300 * 1000,
            test_end_ms=1600 * 1000,
        )
        report = experiment.evaluate_split_oracle(
            series=series,
            quotes=experiment.reconstruct_quotes(series),
            split=split,
            policy=self.policy(),
        )
        self.assertGreater(report["trade_count"], 0)
        self.assertEqual(report["base_explicit_cost_bps"], 26.0)
        self.assertEqual(report["stress_explicit_cost_bps"], 32.5)
        self.assertTrue(all(value > 0.0 for value in report["stress_edges_bps"]))
        self.assertEqual(report["trade_count"], 1)
        self.assertEqual(report["mean_position_lifetime_seconds"], 300.0)

    def test_manifest_binds_parent_target_and_all_cross_asset_fields(self):
        series = self.series()
        parent = self.parent_manifest(series)
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            parent_path = root / "parent.json"
            audit_path = root / "residual.json"
            parent_path.write_text(json.dumps(parent), encoding="utf-8")
            manifest, created = experiment.load_or_create_frozen_audit_manifest(
                audit_path,
                series=series,
                policy=self.policy(),
                parent_manifest_path=parent_path,
            )
            self.assertTrue(created)
            self.assertEqual(
                manifest["parent_target_domain_identity_sha256"],
                parent["frozen_domain"]["identity_sha256"],
            )
            self.assertEqual(
                set(manifest["frozen_domain"]["field_sha256"]),
                set(experiment.DOMAIN_FIELDS),
            )

            context_drift = {key: value.copy() for key, value in series.items()}
            context_drift["btc_mid"][18000] += 1.0
            with self.assertRaisesRegex(ValueError, "frozen domain drift"):
                experiment.load_or_create_frozen_audit_manifest(
                    audit_path,
                    series=context_drift,
                    policy=self.policy(),
                    parent_manifest_path=parent_path,
                )

            target_drift = {key: value.copy() for key, value in series.items()}
            target_drift["best_bid"][10000] += 0.01
            with self.assertRaisesRegex(ValueError, "parent maker target"):
                experiment.load_or_create_frozen_audit_manifest(
                    audit_path,
                    series=target_drift,
                    policy=self.policy(),
                    parent_manifest_path=parent_path,
                )

    def test_decision_stops_before_forward_when_primary_or_boundary_fails(self):
        decision, reasons = experiment.decide_stability(
            {
                "state": "AWAITING_FORWARD",
                "primary_oracle": {"opportunity_proven": False},
                "boundary_sensitivity": {"passed": False},
            }
        )
        self.assertEqual(decision, experiment.DECISION_STOP)
        self.assertIn("frozen_primary_residual_opportunity_failed", reasons)
        self.assertIn("residual_boundary_sensitivity_failed", reasons)

    def test_decision_waits_only_after_primary_and_boundary_pass(self):
        decision, reasons = experiment.decide_stability(
            {
                "state": "AWAITING_FORWARD",
                "primary_oracle": {"opportunity_proven": True},
                "boundary_sensitivity": {"passed": True},
            }
        )
        self.assertEqual(decision, experiment.DECISION_WAIT)
        self.assertEqual(reasons, ["independent_24h_residual_forward_window_incomplete"])


if __name__ == "__main__":
    unittest.main()
