#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


def load_module():
    module_path = pathlib.Path(__file__).with_name("run_data_pipeline.py")
    spec = importlib.util.spec_from_file_location("run_data_pipeline", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PIPELINE = load_module()


class RunDataPipelineTest(unittest.TestCase):
    def test_source_step_retries_transient_failure_and_audits_attempts(self):
        step = PIPELINE.StepResult(
            name="archive_download",
            enabled=True,
            command=["fetch"],
            max_attempts=3,
            retry_backoff_sec=0.0,
        )
        completed = [
            PIPELINE.subprocess.CompletedProcess(
                ["fetch"], 1, stdout="", stderr="temporary upstream failure"
            ),
            PIPELINE.subprocess.CompletedProcess(
                ["fetch"], 0, stdout="ok", stderr=""
            ),
        ]

        with (
            mock.patch.object(
                PIPELINE.subprocess, "run", side_effect=completed
            ) as runner,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = PIPELINE.run_command(step, dry_run=False)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(len(result.attempts), 2)
        self.assertIn(
            "temporary upstream failure", result.attempts[0]["output_tail"]
        )
        self.assertEqual(result.attempts[1]["return_code"], 0)
        self.assertEqual(runner.call_count, 2)

    def test_source_step_exhaustion_preserves_last_bounded_error(self):
        step = PIPELINE.StepResult(
            name="incremental_update",
            enabled=True,
            command=["fetch"],
            max_attempts=2,
            retry_backoff_sec=0.0,
        )
        failure = PIPELINE.subprocess.CompletedProcess(
            ["fetch"], 1, stdout="", stderr="x" * 5000
        )

        with (
            mock.patch.object(
                PIPELINE.subprocess, "run", return_value=failure
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = PIPELINE.run_command(step, dry_run=False)

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(result.error, "return_code=1")
        self.assertLessEqual(len(result.attempts[-1]["output_tail"]), 4096)

    def test_load_yaml_minimal(self):
        with tempfile.TemporaryDirectory() as td:
            config = pathlib.Path(td) / "data_pipeline.yaml"
            config.write_text(
                "common:\n"
                "  symbol: BTCUSDT\n"
                "archive:\n"
                "  enabled: false\n",
                encoding="utf-8",
            )
            payload = PIPELINE.load_yaml(config)
            self.assertEqual(payload["common"]["symbol"], "BTCUSDT")
            self.assertEqual(payload["archive"]["enabled"], False)

    def test_main_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = root / "data_pipeline.yaml"
            run_dir = root / "run"
            config.write_text(
                "common:\n"
                "  symbol: BTCUSDT\n"
                "  interval_minutes: 5\n"
                "  category: linear\n"
                "paths:\n"
                "  ohlcv_csv: data/research/ohlcv_5m.csv\n"
                "  feature_csv: data/research/feature_store_5m.csv\n"
                "  backtest_report: data/research/walkforward_report.json\n"
                "archive:\n"
                "  enabled: true\n"
                "incremental:\n"
                "  enabled: true\n"
                "gap_fill:\n"
                "  enabled: true\n"
                "feature_store:\n"
                "  enabled: true\n"
                "walkforward:\n"
                "  enabled: true\n",
                encoding="utf-8",
            )

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "run_data_pipeline.py",
                    "--config",
                    str(config),
                    "--run-dir",
                    str(run_dir),
                    "--symbol",
                    "SOLUSDT",
                    "--archive-days",
                    "30",
                    "--skip-walkforward",
                    "--dry-run",
                ]
                code = PIPELINE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            report_path = run_dir / "data_pipeline_report.json"
            self.assertTrue(report_path.exists())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "PLANNED")
            self.assertEqual(report["symbol"], "SOLUSDT")
            self.assertTrue(report["skip_walkforward"])
            self.assertEqual(len(report["steps"]), 5)
            archive_step = next(item for item in report["steps"] if item["name"] == "archive_download")
            archive_cmd = archive_step["command"]
            self.assertIn("tools/fetch_bybit_history.py", archive_cmd)
            self.assertIn("--days", archive_cmd)
            self.assertEqual(archive_cmd[archive_cmd.index("--days") + 1], "30")
            self.assertEqual(archive_step["max_attempts"], 3)
            self.assertEqual(archive_step["attempt_count"], 0)
            self.assertEqual(report["contract"]["venue"], "bybit")
            self.assertTrue(report["contract"]["single_venue_verified"])
            planned_count = sum(1 for item in report["steps"] if item["status"] == "planned")
            skipped_count = sum(1 for item in report["steps"] if item["status"] == "skipped")
            self.assertEqual(planned_count, 4)
            self.assertEqual(skipped_count, 1)
            walk_step = next(item for item in report["steps"] if item["name"] == "walkforward_backtest")
            self.assertEqual(walk_step["status"], "skipped")
            self.assertFalse(walk_step["required"])
            self.assertEqual(walk_step["evidence_role"], "research_benchmark_only")
            walk_cmd = walk_step["command"]
            self.assertIn("--model", walk_cmd)
            self.assertIn("linear", walk_cmd)
            self.assertIn("--catboost-iterations", walk_cmd)
            self.assertIn("--catboost-depth", walk_cmd)
            self.assertIn("--catboost-learning-rate", walk_cmd)
            self.assertIn("--random-seed", walk_cmd)
            gap_step = next(
                item for item in report["steps"] if item["name"] == "gap_fill"
            )
            self.assertIn("--base-url", gap_step["command"])

    def test_source_contract_rejects_mixed_endpoint(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = pathlib.Path(td)
            common = {
                "venue": "bybit",
                "category": "linear",
                "symbol": "SOLUSDT",
                "base_url": "https://api.bybit.com",
                "price_type": "trade_price",
                "volume_unit": "base_asset",
                "bar_semantics": "closed_ohlcv",
            }
            final_ohlcv = run_dir / "ohlcv.csv"
            final_ohlcv.write_text(
                "timestamp,open,high,low,close,volume\n"
                "1700000000000,1,1,1,1,1\n",
                encoding="utf-8",
            )
            (run_dir / "archive_report.json").write_text(
                json.dumps(
                    {
                        **common,
                        "interval_minutes": 5,
                        "server_time_ms": 1_700_000_600_000,
                        "closed_boundary_ms": 1_700_000_400_000,
                        "end_ms_exclusive": 1_700_000_400_000,
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "incremental_report.json").write_text(
                json.dumps(
                    {
                        **common,
                        "interval": "5",
                        "last_timestamp_after": 1_700_000_000_000,
                        "server_time_ms": 1_700_000_300_000,
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "gap_fill_report.json").write_text(
                json.dumps(
                    {
                        **common,
                        "base_url": "https://api-testnet.bybit.com",
                        "interval_minutes": 5,
                    }
                ),
                encoding="utf-8",
            )
            failures = PIPELINE.validate_source_contract(
                run_dir=run_dir,
                enabled_steps={
                    "archive_download": True,
                    "incremental_update": True,
                    "gap_fill": True,
                },
                expected_base_url="https://api.bybit.com",
                expected_category="linear",
                expected_symbol="SOLUSDT",
                expected_interval_minutes=5,
                final_ohlcv_path=final_ohlcv,
            )
            self.assertTrue(any("gap_fill: base_url=" in item for item in failures))

    def test_source_contract_rejects_open_bar_in_final_csv(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = pathlib.Path(td)
            common = {
                "venue": "bybit",
                "category": "linear",
                "symbol": "SOLUSDT",
                "base_url": "https://api.bybit.com",
                "price_type": "trade_price",
                "volume_unit": "base_asset",
                "bar_semantics": "closed_ohlcv",
            }
            (run_dir / "archive_report.json").write_text(
                json.dumps(
                    {
                        **common,
                        "interval_minutes": 5,
                        "server_time_ms": 1_700_000_600_000,
                        "closed_boundary_ms": 1_700_000_400_000,
                        "end_ms_exclusive": 1_700_000_400_000,
                    }
                ),
                encoding="utf-8",
            )
            final_ohlcv = run_dir / "ohlcv.csv"
            final_ohlcv.write_text(
                "timestamp,open,high,low,close,volume\n"
                "1700000500000,1,1,1,1,1\n",
                encoding="utf-8",
            )
            failures = PIPELINE.validate_source_contract(
                run_dir=run_dir,
                enabled_steps={"archive_download": True},
                expected_base_url="https://api.bybit.com",
                expected_category="linear",
                expected_symbol="SOLUSDT",
                expected_interval_minutes=5,
                final_ohlcv_path=final_ohlcv,
            )
            self.assertIn(
                "final OHLCV latest bar is not proven closed",
                failures,
            )

    def test_source_contract_normalizes_string_expected_interval(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = pathlib.Path(td)
            common = {
                "venue": "bybit",
                "category": "linear",
                "symbol": "SOLUSDT",
                "base_url": "https://api.bybit.com",
                "price_type": "trade_price",
                "volume_unit": "base_asset",
                "bar_semantics": "closed_ohlcv",
            }
            (run_dir / "archive_report.json").write_text(
                json.dumps(
                    {
                        **common,
                        "interval_minutes": 5,
                        "server_time_ms": 1_700_000_600_000,
                        "closed_boundary_ms": 1_700_000_400_000,
                        "end_ms_exclusive": 1_700_000_400_000,
                    }
                ),
                encoding="utf-8",
            )
            final_ohlcv = run_dir / "ohlcv.csv"
            final_ohlcv.write_text(
                "timestamp,open,high,low,close,volume\n"
                "1700000000000,1,1,1,1,1\n",
                encoding="utf-8",
            )

            failures = PIPELINE.validate_source_contract(
                run_dir=run_dir,
                enabled_steps={"archive_download": True},
                expected_base_url="https://api.bybit.com",
                expected_category="linear",
                expected_symbol="SOLUSDT",
                expected_interval_minutes="5",
                final_ohlcv_path=final_ohlcv,
            )

            self.assertEqual(failures, [])

    def test_walkforward_failure_is_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = root / "data_pipeline.yaml"
            run_dir = root / "run"
            config.write_text(
                "archive:\n"
                "  enabled: true\n"
                "incremental:\n"
                "  enabled: true\n"
                "gap_fill:\n"
                "  enabled: true\n"
                "feature_store:\n"
                "  enabled: true\n"
                "walkforward:\n"
                "  enabled: true\n",
                encoding="utf-8",
            )

            def fake_run(step, dry_run):
                del dry_run
                step.status = (
                    "fail" if step.name == "walkforward_backtest" else "ok"
                )
                step.return_code = 3 if step.status == "fail" else 0
                return step

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "run_data_pipeline.py",
                    "--config",
                    str(config),
                    "--run-dir",
                    str(run_dir),
                ]
                with mock.patch.object(
                    PIPELINE, "run_command", side_effect=fake_run
                ), mock.patch.object(
                    PIPELINE, "validate_source_contract", return_value=[]
                ):
                    code = PIPELINE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            report = json.loads(
                (run_dir / "data_pipeline_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["diagnostic_status"], "FAIL")
            self.assertFalse(
                report["contract"][
                    "walkforward_authoritative_for_integrator_promotion"
                ]
            )


if __name__ == "__main__":
    unittest.main()
