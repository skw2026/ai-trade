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
                                "metrics": {"runtime_status_count": 88},
                            },
                            "replay_validation": {
                                "status": "pass",
                                "readiness_status": "PASS",
                                "target_bucket": "trend",
                                "symbol": "BTCUSDT",
                                "selection": {
                                    "segments_ran": 4,
                                    "coverage_targets_met": True,
                                },
                                "aggregate_summary": {
                                    "execution_active_runs": 4,
                                    "execution_pass_runs": 4,
                                    "total_fills": 3,
                                    "mean_realized_net_per_fill": 0.0,
                                    "mean_filtered_cost_ratio_avg": 0.25,
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
            self.assertEqual(replay_metrics["segments_ran"], 4)
            self.assertEqual(replay_metrics["execution_active_runs"], 4)
            self.assertEqual(replay_metrics["total_fills"], 3)
            self.assertAlmostEqual(replay_metrics["mean_filtered_cost_ratio_avg"], 0.25)


if __name__ == "__main__":
    unittest.main()
