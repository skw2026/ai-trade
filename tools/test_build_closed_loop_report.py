#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("build_closed_loop_report.py")
    spec = importlib.util.spec_from_file_location("build_closed_loop_report", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPORT = load_module()


class BuildClosedLoopReportTest(unittest.TestCase):
    def test_assess_inherit_offline_sections(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            inherit_report = root / "previous_closed_loop_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "metrics": {"runtime_status_count": 88},
                        "account_pnl": {"samples": 88, "equity_change_usd": 12.3},
                    }
                ),
                encoding="utf-8",
            )
            inherit_report.write_text(
                json.dumps(
                    {
                        "overall_status": "PASS",
                        "sections": {
                            "miner": {"status": "pass", "factor_count": 12},
                            "integrator": {
                                "status": "pass",
                                "model_version": "integrator_v_prev",
                            },
                            "data_pipeline": {
                                "status": "pass",
                                "pipeline_status": "PASS",
                            },
                            "runtime": {"status": "pass", "verdict": "PASS"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "build_closed_loop_report.py",
                    "--output",
                    str(output),
                    "--runtime_assess_report",
                    str(runtime_assess),
                    "--inherit_report",
                    str(inherit_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["overall_status"], "PASS")
            self.assertIn("runtime", payload["sections"])
            self.assertIn("miner", payload["sections"])
            self.assertIn("integrator", payload["sections"])
            self.assertIn("data_pipeline", payload["sections"])
            self.assertEqual(
                payload["inherit"]["inherited_sections"],
                ["miner", "integrator", "data_pipeline"],
            )

    def test_explicit_section_not_overridden_by_inherit(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            integrator_report = root / "integrator_report.json"
            inherit_report = root / "previous_closed_loop_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "metrics": {"runtime_status_count": 50},
                        "account_pnl": {"samples": 50},
                    }
                ),
                encoding="utf-8",
            )
            integrator_report.write_text(
                json.dumps(
                    {
                        "model_version": "integrator_v_new",
                        "feature_schema_version": "feature_schema_v2",
                        "metrics_oos": {
                            "auc_mean": 0.61,
                            "split_trained_count": 3,
                            "split_count": 3,
                            "delta_auc_vs_baseline": 0.02,
                        },
                    }
                ),
                encoding="utf-8",
            )
            inherit_report.write_text(
                json.dumps(
                    {
                        "overall_status": "PASS",
                        "sections": {
                            "integrator": {
                                "status": "pass",
                                "model_version": "integrator_v_prev",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "build_closed_loop_report.py",
                    "--output",
                    str(output),
                    "--runtime_assess_report",
                    str(runtime_assess),
                    "--integrator_report",
                    str(integrator_report),
                    "--inherit_report",
                    str(inherit_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["sections"]["integrator"]["model_version"],
                "integrator_v_new",
            )
            self.assertNotIn("integrator", payload["inherit"]["inherited_sections"])

    def test_inherited_fail_section_is_non_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            inherit_report = root / "previous_closed_loop_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "metrics": {"runtime_status_count": 20},
                        "account_pnl": {"samples": 20},
                    }
                ),
                encoding="utf-8",
            )
            inherit_report.write_text(
                json.dumps(
                    {
                        "overall_status": "FAIL",
                        "sections": {
                            "miner": {
                                "status": "fail",
                                "fail_reasons": ["legacy miner failure"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "build_closed_loop_report.py",
                    "--output",
                    str(output),
                    "--runtime_assess_report",
                    str(runtime_assess),
                    "--inherit_report",
                    str(inherit_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["overall_status"], "PASS")
            self.assertEqual(payload["fail_reasons"], [])
            self.assertIn("miner", payload["sections"])

    def test_walkforward_negative_sharpe_is_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            walkforward_report = root / "walkforward_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "metrics": {"runtime_status_count": 80},
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            walkforward_report.write_text(
                json.dumps(
                    {
                        "rows": 5000,
                        "summary": {
                            "valid_split_count": 12,
                            "total_bars": 4800,
                            "avg_split_sharpe": -0.21,
                        },
                    }
                ),
                encoding="utf-8",
            )

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "build_closed_loop_report.py",
                    "--output",
                    str(output),
                    "--runtime_assess_report",
                    str(runtime_assess),
                    "--walkforward_report",
                    str(walkforward_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["sections"]["walkforward"]["status"], "fail")
            self.assertEqual(payload["overall_status"], "FAIL")
            self.assertTrue(
                any("walk-forward 平均 Sharpe 未达门槛" in x for x in payload["fail_reasons"])
            )

    def test_walkforward_low_activity_is_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            walkforward_report = root / "walkforward_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "metrics": {"runtime_status_count": 80},
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            walkforward_report.write_text(
                json.dumps(
                    {
                        "rows": 5000,
                        "summary": {
                            "valid_split_count": 12,
                            "traded_split_count": 0,
                            "total_trades": 0,
                            "total_bars": 4800,
                            "avg_split_sharpe": 0.10,
                        },
                    }
                ),
                encoding="utf-8",
            )

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "build_closed_loop_report.py",
                    "--output",
                    str(output),
                    "--runtime_assess_report",
                    str(runtime_assess),
                    "--walkforward_report",
                    str(walkforward_report),
                    "--walkforward_min_traded_split_count",
                    "1",
                    "--walkforward_min_total_trades",
                    "1",
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["sections"]["walkforward"]["status"], "fail")
            self.assertEqual(payload["overall_status"], "FAIL")
            self.assertTrue(
                any("walk-forward 交易活跃 split 数未达门槛" in x for x in payload["fail_reasons"])
            )
            self.assertTrue(
                any("walk-forward 总交易次数未达门槛" in x for x in payload["fail_reasons"])
            )


if __name__ == "__main__":
    unittest.main()
