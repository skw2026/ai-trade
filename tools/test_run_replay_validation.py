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
        )

        self.assertEqual(report["optimizer"]["status"], "pass")
        self.assertGreaterEqual(report["optimizer"]["pass_candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
