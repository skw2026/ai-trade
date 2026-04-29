#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("build_periodic_summary.py")
    spec = importlib.util.spec_from_file_location("build_periodic_summary", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SUMMARY = load_module()


class BuildPeriodicSummaryTest(unittest.TestCase):
    def test_summary_includes_latest_replay_metrics_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            reports_root = root / "reports"
            out_dir = root / "summary"
            run_dir = reports_root / "20260407T010203Z"
            run_dir.mkdir(parents=True, exist_ok=True)

            (run_dir / "closed_loop_report.json").write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-07T01:02:03Z",
                        "overall_status": "PASS_WITH_ACTIONS",
                        "account_outcome": {"equity_change_usd": 1.0},
                        "warn_reasons": [],
                        "sections": {
                            "runtime": {
                                "status": "pass",
                                "verdict": "PASS_WITH_ACTIONS",
                                "metrics": {
                                    "runtime_status_count": 88,
                                    "fill_overfill_drop_count": 1,
                                    "fill_duplicate_drop_count": 2,
                                    "bybit_exec_dedup_drop_count": 3,
                                    "trend_candidate_probe_signal_count": 5,
                                    "trend_candidate_probe_skip_count": 7,
                                    "trend_candidate_probe_skip_trend_ratio_count": 4,
                                    "trend_candidate_probe_skip_cooldown_count": 2,
                                    "execution_attribution_probe_maker_fill_count": 3,
                                    "execution_attribution_probe_taker_fill_count": 0,
                                    "execution_attribution_main_maker_fill_count": 5,
                                    "execution_attribution_main_taker_fill_count": 1,
                                    "execution_attribution_probe_maker_fee_usd": 0.06,
                                    "execution_attribution_main_taker_fee_usd": 0.15,
                                },
                            },
                            "replay_validation": {
                                "status": "pass",
                                "readiness_status": "PASS",
                                "target_bucket": "trend",
                                "source_symbol": "SOLUSDT",
                                "source_symbols": {"SOLUSDT": "SOLUSDT"},
                                "source_symbol_matches_target": True,
                                "real_market_replay": True,
                                "symbol": "SOLUSDT",
                                "selection": {
                                    "segments_ran": 4,
                                    "coverage_targets_met": True,
                                    "minimum_coverage_targets_met": True,
                                    "recommended_coverage_targets_met": False,
                                },
                                "aggregate_summary": {
                                    "execution_active_runs": 4,
                                    "execution_pass_runs": 4,
                                    "total_fills": 3,
                                    "mean_realized_net_per_fill": 0.0,
                                    "mean_filtered_cost_ratio_avg": 0.25,
                                },
                                "aggregate_validation": {
                                    "coverage_strength_status": "MINIMUM_ONLY"
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "build_periodic_summary.py",
                    "--reports-root",
                    str(reports_root),
                    "--out-dir",
                    str(out_dir),
                    "--now-utc",
                    "2026-04-07T08:00:00Z",
                ]
                code = SUMMARY.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            daily_latest = json.loads((out_dir / "daily_latest.json").read_text(encoding="utf-8"))
            replay_metrics = daily_latest["replay_metrics"]
            self.assertEqual(replay_metrics["source_run_id"], "20260407T010203Z")
            self.assertEqual(replay_metrics["status"], "pass")
            self.assertEqual(replay_metrics["readiness_status"], "PASS")
            self.assertTrue(replay_metrics["real_market_replay"])
            self.assertTrue(replay_metrics["source_symbol_matches_target"])
            self.assertEqual(replay_metrics["segments_ran"], 4)
            self.assertTrue(replay_metrics["minimum_coverage_targets_met"])
            self.assertFalse(replay_metrics["recommended_coverage_targets_met"])
            self.assertEqual(replay_metrics["coverage_strength_status"], "MINIMUM_ONLY")
            self.assertEqual(replay_metrics["execution_active_runs"], 4)
            self.assertEqual(replay_metrics["total_fills"], 3)
            self.assertAlmostEqual(replay_metrics["mean_filtered_cost_ratio_avg"], 0.25)
            runtime_metrics = daily_latest["runtime_metrics"]
            self.assertEqual(runtime_metrics["fill_overfill_drop_count"], 1)
            self.assertEqual(runtime_metrics["fill_duplicate_drop_count"], 2)
            self.assertEqual(runtime_metrics["bybit_exec_dedup_drop_count"], 3)
            self.assertEqual(runtime_metrics["trend_candidate_probe_signal_count"], 5)
            self.assertEqual(runtime_metrics["trend_candidate_probe_skip_count"], 7)
            self.assertEqual(
                runtime_metrics["trend_candidate_probe_skip_trend_ratio_count"],
                4,
            )
            self.assertEqual(
                runtime_metrics["trend_candidate_probe_skip_cooldown_count"],
                2,
            )
            self.assertEqual(
                runtime_metrics["execution_attribution_probe_maker_fill_count"], 3
            )
            self.assertEqual(
                runtime_metrics["execution_attribution_probe_taker_fill_count"], 0
            )
            self.assertEqual(
                runtime_metrics["execution_attribution_main_maker_fill_count"], 5
            )
            self.assertEqual(
                runtime_metrics["execution_attribution_main_taker_fill_count"], 1
            )
            self.assertAlmostEqual(
                runtime_metrics["execution_attribution_probe_maker_fee_usd"], 0.06
            )
            self.assertAlmostEqual(
                runtime_metrics["execution_attribution_main_taker_fee_usd"], 0.15
            )

    def test_account_summary_clips_samples_to_summary_window(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            reports_root = root / "reports"
            out_dir = root / "summary"

            run1 = reports_root / "20260423T110000Z"
            run1.mkdir(parents=True, exist_ok=True)
            (run1 / "closed_loop_report.json").write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-23T11:00:00Z",
                        "overall_status": "PASS",
                        "account_outcome": {
                            "first_sample_utc": "2026-04-23T01:00:00Z",
                            "first_equity_usd": 1000.0,
                            "last_sample_utc": "2026-04-23T11:00:00Z",
                            "last_equity_usd": 1100.0,
                            "equity_change_usd": 100.0,
                            "realized_net_pnl_change_usd": 2.0,
                            "fee_change_usd": 1.0,
                        },
                        "warn_reasons": [],
                        "sections": {},
                    }
                ),
                encoding="utf-8",
            )

            run2 = reports_root / "20260424T100000Z"
            run2.mkdir(parents=True, exist_ok=True)
            (run2 / "closed_loop_report.json").write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-24T10:00:00Z",
                        "overall_status": "PASS",
                        "account_outcome": {
                            "first_sample_utc": "2026-04-24T09:00:00Z",
                            "first_equity_usd": 1200.0,
                            "last_sample_utc": "2026-04-24T10:00:00Z",
                            "last_equity_usd": 1300.0,
                            "equity_change_usd": 100.0,
                            "realized_net_pnl_change_usd": 3.0,
                            "fee_change_usd": 2.0,
                        },
                        "warn_reasons": [],
                        "sections": {},
                    }
                ),
                encoding="utf-8",
            )

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "build_periodic_summary.py",
                    "--reports-root",
                    str(reports_root),
                    "--out-dir",
                    str(out_dir),
                    "--now-utc",
                    "2026-04-24T10:00:00Z",
                ]
                code = SUMMARY.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            daily_latest = json.loads(
                (out_dir / "daily_latest.json").read_text(encoding="utf-8")
            )
            account = daily_latest["account_outcome"]
            self.assertEqual(account["earliest_sample_utc"], "2026-04-23T11:00:00Z")
            self.assertEqual(account["first_equity_usd"], 1100.0)
            self.assertEqual(account["latest_sample_utc"], "2026-04-24T10:00:00Z")
            self.assertEqual(account["last_equity_usd"], 1300.0)
            self.assertEqual(account["equity_change_usd"], 200.0)
            self.assertEqual(account["sample_points_total"], 4)
            self.assertEqual(account["sample_points_used"], 3)
            self.assertEqual(account["sample_points_outside_window"], 1)
            self.assertTrue(account["window_clipped"])
            self.assertEqual(account["sum_run_realized_net_change_usd"], 5.0)
            self.assertEqual(account["sum_run_fee_change_usd"], 3.0)


if __name__ == "__main__":
    unittest.main()
