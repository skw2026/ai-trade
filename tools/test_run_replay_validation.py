#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


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
    def test_stale_corpus_manifest_is_auto_refreshed_without_warning(self):
        rows = [
            REPLAY.FeatureRow(
                timestamp=1_700_000_000_000 + idx * 300_000,
                close=100.0 + idx,
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
            corpus_manifest = tmp_path / "replay_validation_trend_corpus_SOLUSDT.json"
            corpus_manifest.write_text(
                json.dumps(
                    {
                        "target_bucket": "trend",
                        "base_interval_ms": 300_000,
                        "segments": [
                            {
                                "start_timestamp": 1,
                                "end_timestamp": 2,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            selected, eligible, selection, warnings = REPLAY.select_replay_segments(
                rows,
                thresholds,
                feature_csv=feature_csv,
                target_bucket="trend",
                base_interval_ms=300_000,
                max_segments=2,
                min_segment_bars=2,
                corpus_manifest=corpus_manifest,
                refresh_corpus_manifest=False,
            )

            self.assertEqual(warnings, [])
            self.assertGreaterEqual(len(eligible), 1)
            self.assertEqual(len(selected), 1)
            self.assertTrue(selection["corpus_loaded"])
            self.assertTrue(selection["corpus_written"])
            self.assertTrue(selection["corpus_refreshed"])
            self.assertTrue(selection["corpus_auto_refreshed"])
            self.assertEqual(selection["selection_mode"], "dynamic_top_n_auto_refresh")
            self.assertTrue(selection["corpus_refresh_reasons"])
            refreshed = json.loads(corpus_manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                refreshed["segments"][0]["start_timestamp"],
                rows[0].timestamp,
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

    def test_activation_gate_uses_deployable_optimizer_candidate(self):
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

        self.assertEqual(activation["status"], "pass_with_actions")
        self.assertEqual(
            activation["basis"], "execution_optimizer.best_deployable_candidate"
        )
        self.assertEqual(activation["selected_candidate"]["name"], "strong_liquid_q50")
        self.assertTrue(
            any(
                "aggregate_validation_failed_but_optimizer_candidate_passed" in reason
                for reason in activation["warn_reasons"]
            )
        )

    def test_aggregate_run_summaries_fails_when_mean_masks_negative_median(self):
        runs = []
        for realized_net in (-0.002, -0.001, -0.001, 0.020):
            runs.append(
                {
                    "assess_summary": {
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
                }
            )

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

    def test_symbol_tradeability_pass_suppresses_negative_aggregate_fail(self):
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
        )

        self.assertEqual(merged["status"], "pass_with_actions")
        self.assertEqual(merged["fail_reasons"], [])
        self.assertEqual(merged["tradable_symbols"], ["SOLUSDT"])
        self.assertIn("ETHUSDT", merged["quarantined_symbols"])
        self.assertTrue(merged["suppressed_aggregate_fail_reasons"])
        self.assertTrue(
            any(
                "aggregate_validation_failed_but_symbol_tradeability_passed" in reason
                for reason in merged["warn_reasons"]
            )
        )

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
        )

        self.assertEqual(merged["status"], "pass_with_actions")
        self.assertEqual(merged["fail_reasons"], [])
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
        )

        self.assertEqual(merged["status"], "fail")
        self.assertIn(
            "source_symbol_not_execution_covered=SOLUSDT",
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
            "tradable_symbol_count=0 < min_tradable_symbols=1",
            merged["fail_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
