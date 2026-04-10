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

    def test_inherited_fail_section_blocks_overall_status(self):
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

            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["overall_status"], "FAIL")
            self.assertEqual(
                payload["fail_reasons"],
                ["miner: legacy miner failure"],
            )
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

    def test_runtime_not_evaluated_execution_is_exposed_and_warned(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS_WITH_ACTIONS",
                        "runtime_validation_mode": "POLICY_FLAT_PROTECTION",
                        "protection_status": "PASS",
                        "execution_status": "NOT_EVALUATED",
                        "market_context_status": "RANGE_ONLY",
                        "account_sync_status": "NOISY_WHILE_FLAT",
                        "protection_fail_reasons": [],
                        "execution_fail_reasons": [],
                        "warn_reasons": [
                            "当前窗口未出现 TREND 样本：runtime 通过仅代表保护逻辑通过，执行质量仍处于等待趋势样本阶段",
                            "权益变化与已实现净盈亏偏差较大且无执行活动，建议检查资金同步/统计口径: gap_usd=120.0",
                        ],
                        "metrics": {"runtime_status_count": 80},
                        "account_pnl": {"samples": 80},
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
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            runtime = payload["sections"]["runtime"]
            self.assertEqual(payload["runtime_verdict"], "PASS_WITH_ACTIONS")
            self.assertEqual(
                payload["runtime_health_status"], "PASS_WITH_ACTIONS"
            )
            self.assertEqual(
                payload["promotion_readiness_status"], "NOT_EVALUATED"
            )
            self.assertEqual(runtime["runtime_validation_mode"], "POLICY_FLAT_PROTECTION")
            self.assertEqual(runtime["protection_status"], "PASS")
            self.assertEqual(runtime["execution_status"], "NOT_EVALUATED")
            self.assertEqual(runtime["market_context_status"], "RANGE_ONLY")
            self.assertEqual(runtime["account_sync_status"], "NOISY_WHILE_FLAT")
            self.assertTrue(
                any("执行质量未完成验证" in item for item in runtime["warn_reasons"])
            )
            self.assertTrue(
                any("等待趋势样本阶段" in item for item in runtime["warn_reasons"])
            )

    def test_walkforward_trend_bucket_low_participation_is_fail(self):
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
                            "traded_split_count": 2,
                            "total_trades": 2,
                            "total_bars": 4800,
                            "avg_split_sharpe": 0.10,
                            "regime_bucket_summary": {
                                "trend": {"bars": 1200, "trades": 0, "sharpe": 1.2},
                                "range": {"bars": 2000, "trades": 2, "sharpe": -0.5},
                                "extreme": {"bars": 1600, "trades": 0, "sharpe": -1.0},
                            },
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
                    "--walkforward_min_trend_bucket_bars",
                    "1000",
                    "--walkforward_min_trend_bucket_trades",
                    "1",
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["sections"]["walkforward"]["status"], "fail")
            self.assertTrue(
                any("walk-forward TREND 桶交易次数未达门槛" in x for x in payload["fail_reasons"])
            )

    def test_trend_validation_is_reported_and_run_id_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            walkforward_report = root / "walkforward_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS_WITH_ACTIONS",
                        "runtime_validation_mode": "POLICY_FLAT_PROTECTION",
                        "protection_status": "PASS",
                        "execution_status": "NOT_EVALUATED",
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
                            "traded_split_count": 3,
                            "total_trades": 10,
                            "total_bars": 4800,
                            "avg_split_sharpe": 0.10,
                            "regime_bucket_summary": {
                                "trend": {"bars": 1200, "trades": 5, "sharpe": 1.2},
                                "range": {"bars": 2000, "trades": 5, "sharpe": -0.5},
                                "extreme": {"bars": 1600, "trades": 0, "sharpe": -1.0},
                            },
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
                    "--run_id",
                    "20260406T000000Z",
                    "--runtime_assess_report",
                    str(runtime_assess),
                    "--walkforward_report",
                    str(walkforward_report),
                    "--trend_validation_min_bars",
                    "1000",
                    "--trend_validation_min_trades",
                    "1",
                    "--trend_validation_min_sharpe",
                    "0.0",
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "20260406T000000Z")
            self.assertEqual(payload["trend_readiness_status"], "PASS")
            self.assertEqual(payload["sections"]["trend_validation"]["status"], "pass")
            self.assertEqual(
                payload["sections"]["trend_validation"]["summary"]["bars"], 1200
            )

    def test_trend_validation_negative_trend_sharpe_is_fail(self):
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
                        "runtime_validation_mode": "EXECUTION_ACTIVE",
                        "protection_status": "PASS",
                        "execution_status": "PASS",
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
                            "traded_split_count": 5,
                            "total_trades": 12,
                            "total_bars": 4800,
                            "avg_split_sharpe": 0.10,
                            "regime_bucket_summary": {
                                "trend": {"bars": 1500, "trades": 4, "sharpe": -0.2},
                                "range": {"bars": 2000, "trades": 8, "sharpe": -0.5},
                                "extreme": {"bars": 1300, "trades": 0, "sharpe": -1.0},
                            },
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
                    "--trend_validation_min_bars",
                    "1000",
                    "--trend_validation_min_trades",
                    "1",
                    "--trend_validation_min_sharpe",
                    "0.0",
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["trend_readiness_status"], "FAIL")
            self.assertEqual(payload["sections"]["trend_validation"]["status"], "fail")
            self.assertTrue(
                any(
                    "trend-validation TREND 桶 Sharpe 未达门槛" in x
                    for x in payload["fail_reasons"]
                )
            )

    def test_replay_validation_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            replay_report = root / "replay_validation_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS_WITH_ACTIONS",
                        "runtime_validation_mode": "POLICY_FLAT_PROTECTION",
                        "protection_status": "PASS",
                        "execution_status": "NOT_EVALUATED",
                        "metrics": {"runtime_status_count": 80},
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "target_bucket": "trend",
                        "symbol": "BTCUSDT",
                        "selection": {"segments_ran": 4, "coverage_targets_met": True},
                        "aggregate_summary": {
                            "execution_active_runs": 4,
                            "execution_pass_runs": 4,
                            "total_fills": 3,
                            "mean_realized_net_per_fill": 0.0,
                            "mean_filtered_cost_ratio_avg": 0.24,
                        },
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
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
                    "--replay_validation_report",
                    str(replay_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["replay_readiness_status"], "PASS")
            replay_section = payload["sections"]["replay_validation"]
            self.assertEqual(replay_section["status"], "pass")
            self.assertEqual(replay_section["summary"]["total_fills"], 3)
            self.assertEqual(replay_section["aggregate_summary"]["total_fills"], 3)

    def test_replay_validation_fail_blocks_overall_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            replay_report = root / "replay_validation_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "runtime_validation_mode": "EXECUTION_ACTIVE",
                        "protection_status": "PASS",
                        "execution_status": "PASS",
                        "metrics": {"runtime_status_count": 80},
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "target_bucket": "trend",
                        "symbol": "BTCUSDT",
                        "selection": {"segments_ran": 2, "coverage_targets_met": False},
                        "aggregate_summary": {
                            "execution_active_runs": 1,
                            "execution_pass_runs": 1,
                            "total_fills": 1,
                            "mean_realized_net_per_fill": -0.02,
                        },
                        "aggregate_validation": {
                            "status": "fail",
                            "fail_reasons": ["total_fills=1 < 3"],
                            "warn_reasons": [],
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
                    "--replay_validation_report",
                    str(replay_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["overall_status"], "FAIL")
            self.assertEqual(payload["replay_readiness_status"], "FAIL")
            self.assertIn("replay_validation: total_fills=1 < 3", payload["fail_reasons"])

    def test_registry_gate_details_are_exposed_and_split_top_level_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            registry_report = root / "model_registry_entry.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "runtime_validation_mode": "EXECUTION_ACTIVE",
                        "protection_status": "PASS",
                        "execution_status": "PASS",
                        "metrics": {"runtime_status_count": 80},
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            registry_report.write_text(
                json.dumps(
                    {
                        "entry_id": "entry_1",
                        "model_version": "integrator_v_test",
                        "activated": False,
                        "gate": {
                            "pass": False,
                            "min_auc_mean": 0.48,
                            "min_delta_auc_vs_baseline": 0.0,
                            "min_split_trained_count": 1,
                            "min_split_trained_ratio": 0.5,
                            "fail_reasons": [
                                "governance: auc_stdev=0.120000 > max_auc_stdev=0.080000",
                                "governance: random_label_auc=0.580000 > max_random_label_auc=0.550000",
                            ],
                            "warn_reasons": [
                                "governance: random_label_auc_max=0.610000 > soft_cap=0.580000",
                            ],
                            "metric_summary": {
                                "auc_mean": 0.513,
                                "delta_auc_vs_baseline": 0.032,
                                "split_trained_count": 5,
                                "split_count": 5,
                                "split_trained_ratio": 1.0,
                            },
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
                    "--registry_report",
                    str(registry_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["overall_status"], "FAIL")
            self.assertEqual(payload["runtime_verdict"], "PASS")
            self.assertEqual(payload["runtime_health_status"], "PASS")
            self.assertEqual(payload["promotion_readiness_status"], "FAIL")
            registry = payload["sections"]["registry"]
            self.assertEqual(registry["status"], "fail")
            self.assertEqual(
                registry["gate_fail_reasons"],
                [
                    "governance: auc_stdev=0.120000 > max_auc_stdev=0.080000",
                    "governance: random_label_auc=0.580000 > max_random_label_auc=0.550000",
                ],
            )
            self.assertEqual(
                registry["gate_warn_reasons"],
                [
                    "governance: random_label_auc_max=0.610000 > soft_cap=0.580000",
                ],
            )
            self.assertEqual(
                registry["gate_metric_summary"]["auc_mean"],
                0.513,
            )


if __name__ == "__main__":
    unittest.main()
