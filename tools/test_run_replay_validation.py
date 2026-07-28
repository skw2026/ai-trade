#!/usr/bin/env python3

import argparse
import importlib.util
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


def load_replay_module():
    module_path = pathlib.Path(__file__).with_name("run_replay_validation.py")
    spec = importlib.util.spec_from_file_location("run_replay_validation", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPLAY = load_replay_module()


class RunReplayValidationTest(unittest.TestCase):
    @staticmethod
    def _complete_replay_summary(summary):
        fill_count = int(summary.get("funnel_fills_runtime_count") or 0)
        realized_net_per_fill = float(summary.get("realized_net_per_fill") or 0.0)
        fee_usd = float(summary.get("execution_attribution_fee_usd") or 0.0)
        summary.update(
            {
                "execution_attribution_fill_count": fill_count,
                "execution_attribution_quality_fill_count": fill_count,
                "replay_terminal_settlement_done_count": 1,
                "replay_terminal_settlement_failed_count": 0,
                "replay_terminal_realized_net_usd": (
                    realized_net_per_fill * fill_count
                ),
                "replay_terminal_fee_usd": fee_usd,
                "replay_terminal_funding_paid_usd": 0.0,
            }
        )
        return summary

    def test_economic_objective_contract_hash_binds_thresholds(self):
        base_args = {
            "assess_stage": "S5",
            "min_runtime_status": 30,
            "min_execution_active_runs": 3,
            "min_execution_pass_runs": 3,
            "min_total_fills": 20,
            "min_mean_realized_net_per_fill": 0.0,
            "min_break_even_fee_multiplier": 1.25,
            "warn_mean_filtered_cost_ratio": 0.8,
            "min_tradable_symbols": 2,
            "target_bucket": "trend",
            "max_segments": 12,
            "min_segment_bars": 96,
        }
        first = REPLAY.build_economic_objective_contract(
            argparse.Namespace(**base_args),
            root=pathlib.Path(__file__).resolve().parent.parent,
            execution_policy_sha256="a" * 64,
            trade_bot_sha256="b" * 64,
        )
        second = REPLAY.build_economic_objective_contract(
            argparse.Namespace(
                **{
                    **base_args,
                    "min_mean_realized_net_per_fill": 0.01,
                }
            ),
            root=pathlib.Path(__file__).resolve().parent.parent,
            execution_policy_sha256="a" * 64,
            trade_bot_sha256="b" * 64,
        )

        self.assertEqual(
            first["primary_metric"], "mean_realized_net_per_fill"
        )
        self.assertEqual(
            first["accounting_source"], "replay_terminal_account_state"
        )
        self.assertEqual(
            first["fill_count_source"], "all_fill_applied_events_current_boot"
        )
        self.assertTrue(first["terminal_settlement_evidence_required"])
        self.assertEqual(first["incomplete_economics_policy"], "hard_fail")
        self.assertEqual(
            first["state_isolation_policy"], "fresh_wal_per_symbol_segment"
        )
        self.assertTrue(first["selection_and_final_share_contract"])
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_replay_state_directory_is_fresh_and_non_reusable(self):
        with tempfile.TemporaryDirectory() as td:
            segment_dir = pathlib.Path(td) / "segment_01"
            segment_dir.mkdir()

            state_dir = REPLAY.create_fresh_replay_state_dir(segment_dir)

            self.assertTrue(state_dir.is_dir())
            with self.assertRaisesRegex(RuntimeError, "refusing WAL reuse"):
                REPLAY.create_fresh_replay_state_dir(segment_dir)

    def test_final_holdout_uses_frozen_manifest_order_without_future_ranking(self):
        rows = [
            REPLAY.FeatureRow(
                timestamp=1_700_000_000_000 + idx * 300_000,
                close=(100.0, 101.0, 102.0, 180.0, 181.0, 182.0)[idx],
                volume=1000.0,
                features={
                    "ema_diff": 0.0 if idx == 2 else 0.01,
                    "zscore_48": 0.0,
                    "mom_12": 0.01,
                    "mom_48": 0.0 if idx == 2 else 0.02,
                    "ret_1": 0.0,
                    "range_pct": 0.001,
                    "vol_12": 0.001,
                },
            )
            for idx in range(6)
        ]
        thresholds = REPLAY.RegimeThresholds(
            trend_abs_ema_diff=0.005,
            trend_abs_mom_48=0.01,
            extreme_vol_12=0.01,
            extreme_range_pct=0.01,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            feature_csv = tmp_path / "feature_store_5m.csv"
            feature_csv.write_text("timestamp,close,volume\n", encoding="utf-8")
            selection_feature_csv = tmp_path / "selection_feature_store_5m.csv"
            selection_feature_csv.write_text(
                "timestamp,close,volume\n1,100,1\n",
                encoding="utf-8",
            )
            corpus_manifest = tmp_path / "replay_validation_trend_corpus_SOLUSDT.json"
            corpus_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "replay_selection_manifest_v2",
                        "evidence_domain": "selection_validation",
                        "candidate_set_frozen": True,
                        "symbol": "SOLUSDT",
                        "source_feature_csv": str(selection_feature_csv),
                        "source_feature_sha256": hashlib.sha256(
                            selection_feature_csv.read_bytes()
                        ).hexdigest(),
                        "target_bucket": "trend",
                        "base_interval_ms": 300_000,
                        "thresholds": {
                            "trend_abs_ema_diff": 0.005,
                            "trend_abs_mom_48": 0.01,
                            "extreme_vol_12": 0.01,
                            "extreme_range_pct": 0.01,
                        },
                        "selection_policy": (
                            "chronological_quantiles_without_outcome_v1"
                        ),
                        "sampling_quantiles": [0.0, 1.0],
                    }
                ),
                encoding="utf-8",
            )
            manifest_before = corpus_manifest.read_text(encoding="utf-8")

            with mock.patch.object(
                REPLAY,
                "rank_replay_segments",
                side_effect=AssertionError("final holdout must not rank future paths"),
            ):
                selected, eligible, selection, warnings = (
                    REPLAY.select_replay_segments(
                        rows,
                        thresholds,
                        feature_csv=feature_csv,
                        target_bucket="trend",
                        base_interval_ms=300_000,
                        max_segments=1,
                        min_segment_bars=2,
                        corpus_manifest=corpus_manifest,
                        refresh_corpus_manifest=False,
                        final_holdout=True,
                        symbol="SOLUSDT",
                    )
                )

            self.assertEqual(warnings, [])
            self.assertEqual(len(eligible), 2)
            self.assertEqual(len(selected), 2)
            self.assertEqual(selected[0].start_timestamp, rows[0].timestamp)
            self.assertEqual(selected[1].start_timestamp, rows[3].timestamp)
            self.assertTrue(selection["corpus_loaded"])
            self.assertFalse(selection["corpus_written"])
            self.assertFalse(selection["corpus_refreshed"])
            self.assertEqual(selection["selection_mode"], "selection_manifest_holdout")
            self.assertFalse(
                selection["max_segments_ignored_for_frozen_candidate_set"]
            )
            self.assertEqual(
                corpus_manifest.read_text(encoding="utf-8"),
                manifest_before,
            )

    def test_execution_optimizer_fails_when_all_filled_segments_are_net_negative(self):
        run_summaries = [
            {
                "symbol": "BTCUSDT",
                "segment_index": 1,
                "segment": {
                    "bars": 40,
                    "strength_score": 3.0,
                    "liquidity_score": 1.5,
                    "avg_range_pct": 0.001,
                    "avg_vol_12": 0.001,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 2,
                    "execution_activity_count": 2,
                    "realized_net_per_fill": -0.02,
                    "filtered_cost_ratio_avg": 0.2,
                    "execution_attribution_fee_usd": 0.01,
                },
            },
            {
                "symbol": "ETHUSDT",
                "segment_index": 2,
                "segment": {
                    "bars": 40,
                    "strength_score": 2.0,
                    "liquidity_score": 1.0,
                    "avg_range_pct": 0.002,
                    "avg_vol_12": 0.0015,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 1,
                    "execution_activity_count": 1,
                    "realized_net_per_fill": -0.01,
                    "filtered_cost_ratio_avg": 0.3,
                    "execution_attribution_fee_usd": 0.005,
                },
            },
        ]
        for run in run_summaries:
            self._complete_replay_summary(run["assess_summary"])
            run["economics_attribution"] = REPLAY.build_run_economics_attribution(run)

        report = REPLAY.build_replay_economics_report(
            run_summaries,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            min_break_even_fee_multiplier=1.25,
        )

        self.assertEqual(report["optimizer"]["status"], "fail")
        self.assertIn(
            "no_deployable_prefilter_candidate_positive_after_costs",
            report["optimizer"]["fail_reasons"],
        )
        self.assertIn(
            "all_filled_segments_net_negative",
            report["attribution_summary"]["diagnostics"],
        )
        self.assertEqual(report["cost_sensitivity"]["status"], "fail")
        self.assertIn(
            "no_cost_sensitivity_scenario_positive",
            report["cost_sensitivity"]["diagnostics"],
        )

    def test_execution_optimizer_passes_when_prefilter_candidate_is_positive(self):
        run_summaries = [
            {
                "symbol": "BTCUSDT",
                "segment_index": 1,
                "segment": {
                    "bars": 40,
                    "strength_score": 4.0,
                    "liquidity_score": 2.0,
                    "avg_range_pct": 0.001,
                    "avg_vol_12": 0.001,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 2,
                    "execution_activity_count": 2,
                    "realized_net_per_fill": 0.03,
                    "filtered_cost_ratio_avg": 0.1,
                    "execution_attribution_fee_usd": 0.01,
                },
            },
            {
                "symbol": "ETHUSDT",
                "segment_index": 2,
                "segment": {
                    "bars": 40,
                    "strength_score": 1.0,
                    "liquidity_score": 1.0,
                    "avg_range_pct": 0.002,
                    "avg_vol_12": 0.002,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 1,
                    "execution_activity_count": 1,
                    "realized_net_per_fill": -0.01,
                    "filtered_cost_ratio_avg": 0.3,
                    "execution_attribution_fee_usd": 0.005,
                },
            },
        ]
        for run in run_summaries:
            self._complete_replay_summary(run["assess_summary"])
            run["economics_attribution"] = REPLAY.build_run_economics_attribution(run)

        report = REPLAY.build_replay_economics_report(
            run_summaries,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            min_break_even_fee_multiplier=1.25,
        )

        self.assertEqual(report["optimizer"]["status"], "pass")
        self.assertGreaterEqual(report["optimizer"]["pass_candidate_count"], 1)

    def test_execution_optimizer_blocks_positive_net_without_fee_safety_margin(self):
        run_summaries = [
            {
                "symbol": "SOLUSDT",
                "segment_index": 1,
                "segment": {
                    "bars": 40,
                    "strength_score": 4.0,
                    "liquidity_score": 2.0,
                    "avg_range_pct": 0.001,
                    "avg_vol_12": 0.001,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 1,
                    "execution_activity_count": 1,
                    "realized_net_per_fill": 0.01,
                    "filtered_cost_ratio_avg": 0.1,
                    "execution_attribution_fee_usd": 0.10,
                },
            },
        ]
        for run in run_summaries:
            self._complete_replay_summary(run["assess_summary"])
            run["economics_attribution"] = REPLAY.build_run_economics_attribution(run)

        report = REPLAY.build_replay_economics_report(
            run_summaries,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            min_break_even_fee_multiplier=1.25,
        )

        self.assertEqual(report["optimizer"]["status"], "fail")
        best = report["optimizer"]["best_deployable_candidate"]
        self.assertIsNotNone(best)
        self.assertAlmostEqual(best["break_even_fee_multiplier"], 1.1)
        self.assertTrue(
            any(
                "break_even_fee_multiplier" in reason
                for reason in best["fail_reasons"]
            )
        )
        self.assertEqual(best["cost_stress"]["status"], "fail")

    def test_activation_gate_optimizer_cannot_override_aggregate_failure(self):
        selected_candidate = {
            "name": "strong_liquid_q50",
            "diagnostic_only": False,
            "status": "pass",
            "aggregate_summary": {
                "median_realized_net_per_fill_with_fills": 0.002,
                "positive_filled_segment_ratio": 0.70,
                "total_fills": 24,
            },
        }
        activation = REPLAY.build_activation_gate_report(
            aggregate_validation={
                "status": "fail",
                "fail_reasons": [
                    "median_realized_net_per_fill_with_fills=-0.001 < 0.000"
                ],
                "warn_reasons": [],
            },
            economics_report={
                "optimizer": {
                    "status": "pass",
                    "best_deployable_candidate": selected_candidate,
                },
                "execution_cost_plan": {"status": "pass"},
            },
            symbol_reports={},
            source_symbol="SOLUSDT",
        )

        self.assertEqual(activation["status"], "fail")
        self.assertEqual(activation["basis"], "aggregate_validation")
        self.assertEqual(activation["selected_candidate"]["name"], "strong_liquid_q50")
        self.assertIn(
            "median_realized_net_per_fill_with_fills=-0.001 < 0.000",
            activation["fail_reasons"],
        )

    def test_aggregate_run_summaries_fails_when_mean_masks_negative_median(self):
        runs = []
        for realized_net in (-0.002, -0.001, -0.001, 0.020):
            assess_summary = self._complete_replay_summary(
                {
                        "verdict": "PASS",
                        "runtime_validation_mode": "EXECUTION_ACTIVE",
                        "protection_status": "PASS",
                        "execution_status": "PASS",
                        "market_context_status": "TREND_PRESENT",
                        "execution_activity_count": 4,
                        "funnel_fills_runtime_count": 5,
                        "regime_trend_runtime_count": 4,
                        "realized_net_per_fill": realized_net,
                        "filtered_cost_ratio_avg": 0.20,
                }
            )
            runs.append({"assess_summary": assess_summary})

        summary, validation = REPLAY.aggregate_run_summaries(
            runs,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=3,
            min_mean_realized_net_per_fill=0.0,
            warn_mean_filtered_cost_ratio=0.80,
        )

        self.assertGreater(summary["mean_realized_net_per_fill"], 0.0)
        self.assertLess(summary["median_realized_net_per_fill_with_fills"], 0.0)
        self.assertLess(summary["positive_filled_segment_ratio"], 0.55)
        self.assertEqual(validation["coverage_strength_status"], "ROBUST")
        self.assertEqual(validation["status"], "fail")
        self.assertTrue(
            any(
                "median_realized_net_per_fill_with_fills" in reason
                for reason in validation["fail_reasons"]
            )
        )

    def test_aggregate_realized_net_is_weighted_by_fill_count(self):
        runs = [
            {
                "assess_summary": self._complete_replay_summary({
                    "verdict": "PASS",
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "protection_status": "PASS",
                    "execution_status": "PASS",
                    "market_context_status": "TREND_PRESENT",
                    "execution_activity_count": 1,
                    "funnel_fills_runtime_count": 1,
                    "realized_net_per_fill": 1.0,
                    "filtered_cost_ratio_avg": 0.2,
                })
            },
            {
                "assess_summary": self._complete_replay_summary({
                    "verdict": "PASS",
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "protection_status": "PASS",
                    "execution_status": "PASS",
                    "market_context_status": "TREND_PRESENT",
                    "execution_activity_count": 100,
                    "funnel_fills_runtime_count": 100,
                    "realized_net_per_fill": -0.02,
                    "filtered_cost_ratio_avg": 0.2,
                })
            },
        ]
        summary, validation = REPLAY.aggregate_run_summaries(
            runs,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            warn_mean_filtered_cost_ratio=0.8,
        )
        self.assertAlmostEqual(summary["segment_mean_realized_net_per_fill"], 0.49)
        self.assertAlmostEqual(summary["mean_realized_net_per_fill"], -1.0 / 101.0)
        self.assertEqual(summary["aggregation_weight"], "fill_count")
        self.assertEqual(validation["status"], "fail")

    def test_terminal_account_net_overrides_stale_runtime_window(self):
        run = {
            "symbol": "BTCUSDT",
            "segment_index": 1,
            "assess_summary": {
                "runtime_validation_mode": "EXECUTION_ACTIVE",
                "execution_status": "PASS",
                "funnel_fills_runtime_count": 1,
                "realized_net_per_fill": 0.10,
                "execution_attribution_fill_count": 2,
                "execution_attribution_quality_fill_count": 2,
                "execution_attribution_fee_usd": 0.02,
                "replay_terminal_settlement_done_count": 1,
                "replay_terminal_settlement_failed_count": 0,
                "replay_terminal_realized_net_usd": -0.20,
                "replay_terminal_fee_usd": 0.03,
                "replay_terminal_funding_paid_usd": 0.01,
                "fills_attribution": {"notional_abs_usd": 200.0},
            },
        }

        economics = REPLAY.build_run_economics_attribution(run)

        self.assertTrue(economics["economics_complete"])
        self.assertEqual(economics["fill_count"], 2)
        self.assertAlmostEqual(economics["realized_net_usd"], -0.20)
        self.assertAlmostEqual(economics["realized_net_per_fill"], -0.10)
        self.assertAlmostEqual(economics["fee_usd"], 0.03)
        self.assertAlmostEqual(economics["funding_paid_usd"], 0.01)
        self.assertAlmostEqual(economics["estimated_net_before_fee_usd"], -0.17)
        self.assertAlmostEqual(economics["estimated_gross_pnl_usd"], -0.16)
        self.assertEqual(
            economics["accounting_source"],
            "replay_terminal_account_plus_fill_attribution",
        )

    def test_aggregate_rejects_missing_terminal_settlement_evidence(self):
        run = {
            "assess_summary": {
                "verdict": "PASS",
                "runtime_validation_mode": "EXECUTION_ACTIVE",
                "protection_status": "PASS",
                "execution_status": "PASS",
                "market_context_status": "TREND_PRESENT",
                "execution_activity_count": 2,
                "funnel_fills_runtime_count": 2,
                "realized_net_per_fill": 1.0,
                "execution_attribution_fill_count": 2,
                "execution_attribution_quality_fill_count": 2,
            }
        }

        summary, validation = REPLAY.aggregate_run_summaries(
            [run],
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            warn_mean_filtered_cost_ratio=0.8,
        )

        self.assertEqual(summary["economics_incomplete_runs"], 1)
        self.assertEqual(summary["total_fills"], 0)
        self.assertEqual(validation["status"], "fail")
        self.assertTrue(
            any(
                "economics attribution incomplete" in reason
                for reason in validation["fail_reasons"]
            )
        )

    def test_cost_sensitivity_finds_fee_reduction_break_even(self):
        economics_rows = [
            {
                "fill_count": 2,
                "estimated_gross_pnl_usd": 1.2,
                "fee_usd": 2.0,
            },
            {
                "fill_count": 1,
                "estimated_gross_pnl_usd": 0.6,
                "fee_usd": 1.0,
            },
        ]
        report = REPLAY.build_cost_sensitivity_report(
            economics_rows,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
        )
        self.assertEqual(report["status"], "diagnostic_pass")
        self.assertEqual(report["current_cost_status"], "fail")
        self.assertAlmostEqual(report["break_even_fee_multiplier"], 0.6)
        pass_names = {
            item["name"] for item in report["scenarios"] if item["status"] == "pass"
        }
        self.assertIn("fee_x0.5", pass_names)
        self.assertNotIn("fee_x1", pass_names)

    def test_cost_sensitivity_keeps_funding_fixed_while_scaling_fees(self):
        economics_rows = [
            {
                "fill_count": 1,
                "estimated_gross_pnl_usd": 1.2,
                "fee_usd": 1.0,
                "funding_paid_usd": 0.2,
            }
        ]

        report = REPLAY.build_cost_sensitivity_report(
            economics_rows,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
        )
        scenarios = {item["name"]: item for item in report["scenarios"]}

        self.assertAlmostEqual(report["break_even_fee_multiplier"], 1.0)
        self.assertAlmostEqual(report["total_funding_paid_usd"], 0.2)
        self.assertAlmostEqual(
            scenarios["fee_x1"]["mean_adjusted_realized_net_per_fill"], 0.0
        )
        self.assertAlmostEqual(
            scenarios["fee_x0.5"]["mean_adjusted_realized_net_per_fill"], 0.5
        )
        self.assertAlmostEqual(
            scenarios["fee_x0.5"]["total_adjusted_realized_net_usd_est"], 0.5
        )

    def test_exit_capture_flags_low_capture_when_path_mfe_covers_fee(self):
        economics_rows = [
            {
                "symbol": "BTCUSDT",
                "segment_index": 1,
                "fill_count": 1,
                "realized_net_per_fill": -0.8,
                "fee_usd": 1.0,
                "fee_per_fill_usd": 1.0,
                "fee_bps_per_fill": 10.0,
                "estimated_gross_pnl_usd": 0.2,
                "estimated_gross_per_fill_usd": 0.2,
                "segment_close_path_mfe": 0.005,
                "segment_close_path_efficiency": 0.6,
            },
            {
                "symbol": "ETHUSDT",
                "segment_index": 2,
                "fill_count": 1,
                "realized_net_per_fill": -0.7,
                "fee_usd": 1.0,
                "fee_per_fill_usd": 1.0,
                "fee_bps_per_fill": 10.0,
                "estimated_gross_pnl_usd": 0.3,
                "estimated_gross_per_fill_usd": 0.3,
                "segment_close_path_mfe": 0.006,
                "segment_close_path_efficiency": 0.5,
            },
        ]

        report = REPLAY.build_exit_capture_report(economics_rows)

        self.assertEqual(report["primary_diagnosis"], "exit_capture_low")
        self.assertIn(
            "path_mfe_covers_cost_but_gross_capture_low",
            report["diagnostics"],
        )
        self.assertEqual(report["low_capture_segment_count"], 2)
        self.assertGreater(report["mean_path_fee_coverage_ratio"], 2.0)

    def test_execution_cost_plan_marks_lower_cost_candidate_requires_rerun(self):
        economics_rows = [
            {
                "fill_count": 1,
                "realized_net_per_fill": -0.01,
                "fee_usd": 1.0,
                "fee_per_fill_usd": 1.0,
                "fee_bps_per_fill": 10.0,
                "estimated_gross_pnl_usd": 0.99,
                "estimated_gross_per_fill_usd": 0.99,
                "segment_close_path_mfe": 0.003,
            }
        ]
        exit_capture = REPLAY.build_exit_capture_report(economics_rows)

        plan = REPLAY.build_execution_cost_plan(
            economics_rows,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            exit_capture=exit_capture,
        )

        self.assertEqual(plan["status"], "candidate_requires_rerun")
        self.assertEqual(
            plan["primary_action"], "rerun_replay_with_lower_cost_execution"
        )
        self.assertIn("current_cost_not_deployable", plan["diagnostics"])
        self.assertIn(
            "lower_cost_execution_candidate_requires_rerun",
            plan["diagnostics"],
        )
        self.assertAlmostEqual(plan["break_even_fee_multiplier"], 0.99)

    def test_segment_market_attribution_reports_close_path_mfe_mae(self):
        rows = [
            REPLAY.FeatureRow(
                timestamp=1_700_000_000_000 + idx * 300_000,
                close=close,
                volume=1000.0,
                features={
                    "ema_diff": 0.01,
                    "zscore_48": 0.0,
                    "mom_12": 0.01,
                    "mom_48": 0.02,
                    "ret_1": 0.0,
                    "range_pct": 0.001,
                    "vol_12": 0.001,
                },
            )
            for idx, close in enumerate([100.0, 103.0, 101.0, 105.0])
        ]
        segment = REPLAY.ReplaySegment(
            start_index=0,
            end_index=3,
            start_timestamp=rows[0].timestamp,
            end_timestamp=rows[-1].timestamp,
            bars=4,
        )
        attribution = REPLAY.build_segment_market_attribution(segment, rows)
        self.assertEqual(attribution["dominant_direction_label"], "long")
        self.assertAlmostEqual(attribution["close_return"], 0.05)
        self.assertAlmostEqual(attribution["close_path_mfe"], 0.05)
        self.assertAlmostEqual(attribution["close_path_mae"], 0.0)

    def test_symbol_tradeability_cannot_suppress_negative_aggregate_fail(self):
        aggregate_validation = {
            "status": "fail",
            "fail_reasons": ["aggregate median net is negative"],
            "warn_reasons": [],
        }
        symbol_reports = {
            "SOLUSDT": {
                "aggregate_summary": {
                    "total_fills": 8,
                    "positive_realized_net_with_fills_runs": 5,
                    "negative_realized_net_with_fills_runs": 2,
                    "mean_realized_net_per_fill": 0.03,
                    "mean_realized_net_per_fill_with_fills": 0.03,
                    "median_realized_net_per_fill_with_fills": 0.02,
                    "positive_filled_segment_ratio": 0.70,
                },
                "aggregate_validation": {
                    "status": "pass",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "PASS",
                    "fail_reasons": [],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 3},
                },
            },
            "ETHUSDT": {
                "aggregate_summary": {
                    "total_fills": 5,
                    "positive_realized_net_with_fills_runs": 0,
                    "negative_realized_net_with_fills_runs": 5,
                    "mean_realized_net_per_fill": -0.02,
                    "mean_realized_net_per_fill_with_fills": -0.02,
                    "median_realized_net_per_fill_with_fills": -0.02,
                    "positive_filled_segment_ratio": 0.0,
                },
                "aggregate_validation": {
                    "status": "fail",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "PASS",
                    "fail_reasons": ["all filled segments net negative"],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": ["all filled segments net negative"],
                    "thresholds": {"min_total_fills": 3},
                },
            },
        }

        merged = REPLAY.merge_symbol_validations(
            aggregate_validation,
            symbol_reports,
            min_mean_realized_net_per_fill=0.0,
            min_tradable_symbols=1,
            final_holdout=True,
        )

        self.assertEqual(merged["status"], "fail")
        self.assertIn(
            "aggregate median net is negative",
            merged["fail_reasons"],
        )
        self.assertEqual(merged["tradable_symbols"], ["SOLUSDT"])
        self.assertIn("ETHUSDT", merged["quarantined_symbols"])
        self.assertEqual(merged["suppressed_aggregate_fail_reasons"], [])

    def test_symbol_tradeability_does_not_fail_on_non_source_insufficient_symbol(self):
        aggregate_validation = {
            "status": "fail",
            "fail_reasons": ["aggregate coverage insufficient"],
            "warn_reasons": [],
        }
        symbol_reports = {
            "SOLUSDT": {
                "aggregate_summary": {
                    "total_fills": 20,
                    "positive_realized_net_with_fills_runs": 12,
                    "negative_realized_net_with_fills_runs": 8,
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.60,
                },
                "aggregate_validation": {
                    "status": "pass",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "ROBUST",
                    "fail_reasons": [],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 20},
                },
            },
            "ETHUSDT": {
                "aggregate_summary": {"total_fills": 0},
                "aggregate_validation": {
                    "status": "fail",
                    "minimum_coverage_targets_met": False,
                    "coverage_strength_status": "INSUFFICIENT",
                    "fail_reasons": ["total_fills=0 < 20"],
                    "coverage_fail_reasons": ["total_fills=0 < 20"],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 20},
                },
            },
        }

        merged = REPLAY.merge_symbol_validations(
            aggregate_validation,
            symbol_reports,
            min_mean_realized_net_per_fill=0.0,
            min_tradable_symbols=1,
            source_symbol="SOLUSDT",
            final_holdout=True,
        )

        self.assertEqual(merged["status"], "fail")
        self.assertIn("aggregate coverage insufficient", merged["fail_reasons"])
        self.assertIn("ETHUSDT: total_fills=0 < 20", merged["fail_reasons"])
        self.assertEqual(merged["tradable_symbols"], ["SOLUSDT"])
        self.assertIn("ETHUSDT", merged["insufficient_symbols"])
        self.assertTrue(
            any("symbol_replay_coverage_insufficient=ETHUSDT" in reason for reason in merged["warn_reasons"])
        )

    def test_symbol_tradeability_fails_when_source_symbol_is_not_tradable(self):
        symbol_reports = {
            "SOLUSDT": {
                "aggregate_summary": {"total_fills": 0},
                "aggregate_validation": {
                    "status": "fail",
                    "minimum_coverage_targets_met": False,
                    "coverage_strength_status": "INSUFFICIENT",
                    "fail_reasons": ["total_fills=0 < 20"],
                    "coverage_fail_reasons": ["total_fills=0 < 20"],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 20},
                },
            },
            "ETHUSDT": {
                "aggregate_summary": {
                    "total_fills": 20,
                    "positive_realized_net_with_fills_runs": 12,
                    "negative_realized_net_with_fills_runs": 8,
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.60,
                },
                "aggregate_validation": {
                    "status": "pass",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "ROBUST",
                    "fail_reasons": [],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 20},
                },
            },
        }

        merged = REPLAY.merge_symbol_validations(
            {"status": "pass", "fail_reasons": [], "warn_reasons": []},
            symbol_reports,
            min_mean_realized_net_per_fill=0.0,
            min_tradable_symbols=1,
            source_symbol="SOLUSDT",
            final_holdout=True,
        )

        self.assertEqual(merged["status"], "fail")
        self.assertIn(
            "SOLUSDT: total_fills=0 < 20",
            merged["fail_reasons"],
        )

    def test_quarantined_source_symbol_keeps_execution_coverage_separate(self):
        symbol_reports = {
            "SOLUSDT": {
                "aggregate_summary": {
                    "total_fills": 20,
                    "positive_realized_net_with_fills_runs": 4,
                    "negative_realized_net_with_fills_runs": 4,
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.50,
                },
                "aggregate_validation": {
                    "status": "fail",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "ROBUST",
                    "fail_reasons": [
                        "positive_filled_segment_ratio=0.500000 < 0.550000"
                    ],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": [
                        "positive_filled_segment_ratio=0.500000 < 0.550000"
                    ],
                    "thresholds": {"min_total_fills": 20},
                },
            }
        }

        merged = REPLAY.merge_symbol_validations(
            {"status": "fail", "fail_reasons": [], "warn_reasons": []},
            symbol_reports,
            min_mean_realized_net_per_fill=0.0,
            min_tradable_symbols=1,
            source_symbol="SOLUSDT",
            final_holdout=True,
        )

        tradeability = merged["symbol_tradeability"]
        self.assertEqual(tradeability["execution_covered_symbols"], ["SOLUSDT"])
        self.assertEqual(tradeability["tradable_symbols"], [])
        self.assertIn("SOLUSDT", tradeability["quarantined_symbols"])
        self.assertNotIn(
            "source_symbol_not_execution_covered=SOLUSDT",
            merged["fail_reasons"],
        )
        self.assertIn(
            "SOLUSDT: positive_filled_segment_ratio=0.500000 < 0.550000",
            merged["fail_reasons"],
        )

    def test_final_holdout_ledger_rejects_rerun_overlap_and_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ledger = root / "holdout.jsonl"
            claim = {
                "symbol": "SOLUSDT",
                "bar_interval_ms": 300000,
                "holdout_start_ts_ms": 1000,
                "holdout_end_ts_ms": 2000,
                "dataset_path": "/tmp/holdout.csv",
                "dataset_sha256": "a" * 64,
            }
            first = REPLAY.claim_final_holdouts(
                ledger,
                experiment_id="run-1",
                candidate_identity_sha256="b" * 64,
                holdouts=[claim],
            )
            self.assertEqual(len(first), 1)
            self.assertEqual(
                len(ledger.read_text(encoding="utf-8").splitlines()), 1
            )
            self.assertRegex(first[0]["entry_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(
                ledger.with_suffix(".jsonl.checkpoint.json").is_file()
            )

            with self.assertRaisesRegex(
                RuntimeError, "experiment already consumed"
            ):
                REPLAY.claim_final_holdouts(
                    ledger,
                    experiment_id="run-1",
                    candidate_identity_sha256="b" * 64,
                    holdouts=[claim],
                )

            with self.assertRaisesRegex(
                RuntimeError, "overlaps consumed evidence"
            ):
                REPLAY.claim_final_holdouts(
                    ledger,
                    experiment_id="run-2",
                    candidate_identity_sha256="c" * 64,
                    holdouts=[{**claim, "holdout_start_ts_ms": 1500}],
                )

            later = REPLAY.claim_final_holdouts(
                ledger,
                experiment_id="run-3",
                candidate_identity_sha256="c" * 64,
                holdouts=[
                    {
                        **claim,
                        "holdout_start_ts_ms": 3000,
                        "holdout_end_ts_ms": 4000,
                        "dataset_sha256": "d" * 64,
                    }
                ],
            )
            self.assertEqual(len(later), 1)
            self.assertEqual(
                len(ledger.read_text(encoding="utf-8").splitlines()), 2
            )
            ledger_before_conflict = ledger.read_bytes()
            checkpoint_path = ledger.with_suffix(
                ".jsonl.checkpoint.json"
            )
            checkpoint_before_conflict = checkpoint_path.read_bytes()
            with self.assertRaisesRegex(
                RuntimeError, "overlaps consumed evidence"
            ):
                REPLAY.claim_final_holdouts(
                    ledger,
                    experiment_id="run-multi-conflict",
                    candidate_identity_sha256="e" * 64,
                    holdouts=[
                        {
                            **claim,
                            "symbol": "BTCUSDT",
                            "holdout_start_ts_ms": 5000,
                            "holdout_end_ts_ms": 6000,
                            "dataset_sha256": "e" * 64,
                        },
                        {
                            **claim,
                            "holdout_start_ts_ms": 3500,
                            "holdout_end_ts_ms": 4500,
                            "dataset_sha256": "f" * 64,
                        },
                    ],
                )
            self.assertEqual(ledger.read_bytes(), ledger_before_conflict)
            self.assertEqual(
                checkpoint_path.read_bytes(),
                checkpoint_before_conflict,
            )
            lines = ledger.read_text(encoding="utf-8").splitlines()
            ledger.write_text(lines[0] + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError, "checkpoint mismatch"
            ):
                REPLAY.claim_final_holdouts(
                    ledger,
                    experiment_id="run-4",
                    candidate_identity_sha256="e" * 64,
                    holdouts=[
                        {
                            **claim,
                            "holdout_start_ts_ms": 5000,
                            "holdout_end_ts_ms": 6000,
                            "dataset_sha256": "f" * 64,
                        }
                    ],
                )


if __name__ == "__main__":
    unittest.main()
