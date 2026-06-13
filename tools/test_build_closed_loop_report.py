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
            self.assertEqual(payload["overall_status"], "PASS_WITH_ACTIONS")
            self.assertEqual(
                payload["trading_convergence_status"],
                "NOT_CONVERGED_REPLAY_SAMPLE_INSUFFICIENT",
            )
            self.assertIn("runtime", payload["sections"])
            self.assertIn("miner", payload["sections"])
            self.assertIn("integrator", payload["sections"])
            self.assertIn("data_pipeline", payload["sections"])
            self.assertEqual(
                payload["inherit"]["inherited_sections"],
                ["miner", "integrator", "data_pipeline"],
            )

    def test_assess_inherited_registry_is_context_not_current_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            replay_report = root / "replay_validation_report.json"
            inherit_report = root / "previous_closed_loop_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "runtime_validation_mode": "EXECUTION_ACTIVE",
                        "protection_status": "PASS",
                        "execution_status": "PASS",
                        "metrics": {
                            "runtime_status_count": 80,
                            "funnel_fills_runtime_count": 2,
                            "realized_net_per_fill": 0.01,
                            "integrator_feature_sanitized_count": 0,
                            "feature_nonfinite_count": 0,
                        },
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass_with_actions",
                        "target_bucket": "trend",
                        "source_symbol": "SOLUSDT",
                        "symbol": "SOLUSDT",
                        "symbols": ["SOLUSDT"],
                        "selection": {"segments_ran": 8, "coverage_targets_met": True},
                        "aggregate_summary": {
                            "execution_active_runs": 8,
                            "execution_pass_runs": 8,
                            "total_fills": 24,
                            "median_realized_net_per_fill_with_fills": 0.01,
                            "positive_filled_segment_ratio": 0.75,
                            "mean_realized_net_per_fill": 0.01,
                        },
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "status": "pass",
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": [],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "total_fills": 24,
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.75,
                                    }
                                },
                            },
                        },
                        "exit_capture": {
                            "status": "pass",
                            "sample_count": 12,
                            "mean_gross_capture_of_path_mfe": 0.15,
                            "low_capture_segment_count": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            inherit_report.write_text(
                json.dumps(
                    {
                        "overall_status": "FAIL",
                        "sections": {
                            "registry": {
                                "status": "fail",
                                "fail_reasons": [
                                    "replay_validation: source_symbol_not_tradable=SOLUSDT",
                                    "replay_validation: tradable_symbol_count=0 < min_tradable_symbols=1",
                                ],
                                "gate_pass": False,
                                "activated": False,
                            },
                            "replay_validation": {
                                "status": "fail",
                                "fail_reasons": ["old replay should not override current replay"],
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
                    "--replay_validation_report",
                    str(replay_report),
                    "--inherit_report",
                    str(inherit_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["overall_status"], "PASS")
            self.assertEqual(payload["promotion_readiness_status"], "NOT_EVALUATED")
            self.assertEqual(payload["sections"]["replay_validation"]["status"], "pass")
            self.assertEqual(payload["sections"]["registry"]["status"], "fail")
            self.assertFalse(payload["sections"]["registry"]["_current_run_gate"])
            self.assertIn("registry", payload["inherit"]["inherited_sections"])
            self.assertEqual(
                payload["inherit"]["current_gate_excluded_sections"], ["registry"]
            )
            self.assertFalse(
                any(reason.startswith("registry:") for reason in payload["fail_reasons"])
            )
            self.assertFalse(
                any("old replay should not override" in reason for reason in payload["fail_reasons"])
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

    def test_strategy_diagnose_action_required_blocks_convergence(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            replay_report = root / "replay_validation_report.json"
            strategy_report = root / "strategy_diagnose_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "runtime_validation_mode": "EXECUTION_ACTIVE",
                        "execution_status": "PASS",
                        "metrics": {
                            "runtime_status_count": 80,
                            "funnel_fills_runtime_count": 2,
                            "realized_net_per_fill": 0.01,
                        },
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "symbol": "SOLUSDT",
                        "symbols": ["SOLUSDT"],
                        "aggregate_summary": {
                            "total_fills": 24,
                            "median_realized_net_per_fill_with_fills": 0.01,
                            "positive_filled_segment_ratio": 0.75,
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
            strategy_report.write_text(
                json.dumps(
                    {
                        "status": "action_required",
                        "readiness_status": "ACTION_REQUIRED",
                        "fail_reasons": [
                            "confirmed_trend path MFE covers cost but capture is low"
                        ],
                        "aggregate": {
                            "confirmed_trend": {
                                "sample_count": 48,
                                "mean_net_forward_bps": 7.0,
                                "positive_net_ratio": 1.0,
                                "mean_gross_capture_of_path_mfe": 0.04,
                            }
                        },
                        "diagnostics": [
                            {"code": "path_mfe_available_but_capture_low"}
                        ],
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
                    "--strategy_diagnose_report",
                    str(strategy_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["strategy_diagnose_status"], "ACTION_REQUIRED")
            self.assertEqual(payload["overall_status"], "FAIL")
            self.assertIn(
                "NOT_CONVERGED_STRATEGY_RAW_EDGE_ACTION_REQUIRED",
                payload["sections"]["trading_convergence"]["blockers"],
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

    def test_walkforward_negative_split_returns_are_fail(self):
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
                            "traded_split_count": 5,
                            "total_trades": 25,
                            "total_bars": 4800,
                            "avg_split_sharpe": 0.30,
                            "avg_split_return": -0.0002,
                            "enabled_avg_split_return": -0.0004,
                            "traded_avg_split_return": -0.0006,
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
                any("walk-forward 平均 split 收益未达门槛" in x for x in payload["fail_reasons"])
            )
            self.assertTrue(
                any("walk-forward 启用 split 平均收益未达门槛" in x for x in payload["fail_reasons"])
            )
            self.assertTrue(
                any("walk-forward 交易 split 平均收益未达门槛" in x for x in payload["fail_reasons"])
            )

    def test_walkforward_focus_bucket_does_not_downgrade_negative_returns(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            walkforward_report = root / "walkforward_report.json"
            walkforward_report.write_text(
                json.dumps(
                    {
                        "rows": 5000,
                        "summary": {
                            "valid_split_count": 12,
                            "traded_split_count": 5,
                            "total_trades": 25,
                            "total_bars": 4800,
                            "avg_split_sharpe": 0.30,
                            "avg_split_return": -0.0002,
                            "enabled_avg_split_return": -0.0004,
                            "traded_avg_split_return": -0.0006,
                            "regime_bucket_summary": {
                                "trend": {
                                    "bars": 1500,
                                    "trades": 4,
                                    "sharpe": 2.0,
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_walkforward(
                walkforward_report,
                focus_bucket="trend",
                min_focus_bucket_bars=1000,
                min_focus_bucket_trades=1,
                min_focus_bucket_sharpe=0.0,
            )

            self.assertEqual(section["focus_bucket_validation"]["status"], "pass")
            self.assertEqual(section["status"], "fail")
            self.assertEqual(section["warn_reasons"], [])
            self.assertTrue(
                any("walk-forward 平均 split 收益未达门槛" in x for x in section["fail_reasons"])
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

    def test_account_outcome_exposes_open_position_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS_WITH_ACTIONS",
                        "runtime_validation_mode": "EXECUTION_ACTIVE",
                        "protection_status": "PASS",
                        "execution_status": "PASS",
                        "account_sync_status": "OPEN_POSITION_GAP",
                        "metrics": {"runtime_status_count": 80},
                        "account_pnl": {
                            "samples": 80,
                            "last_notional_usd": 377.256,
                            "last_abs_notional_usd": 377.256,
                            "start_flat": True,
                            "end_flat": False,
                            "account_counter_reset_count": 1,
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
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["sections"]["runtime"]["account_sync_status"],
                "OPEN_POSITION_GAP",
            )
            self.assertEqual(payload["account_outcome"]["last_notional_usd"], 377.256)
            self.assertEqual(
                payload["account_outcome"]["last_abs_notional_usd"], 377.256
            )
            self.assertTrue(payload["account_outcome"]["start_flat"])
            self.assertFalse(payload["account_outcome"]["end_flat"])
            self.assertEqual(
                payload["account_outcome"]["account_counter_reset_count"], 1
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
                        "source_symbol": "SOLUSDT",
                        "source_symbols": {"SOLUSDT": "SOLUSDT"},
                        "source_symbol_matches_target": True,
                        "real_market_replay": True,
                        "per_symbol_source": {
                            "SOLUSDT": {
                                "source_symbol": "SOLUSDT",
                                "feature_csv": "data/SOLUSDT/feature_store_5m.csv",
                                "source_symbol_matches_target": True,
                                "real_market_replay": True,
                            }
                        },
                        "feature_csv_by_symbol": {
                            "SOLUSDT": "data/SOLUSDT/feature_store_5m.csv"
                        },
                        "symbol": "SOLUSDT",
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
            self.assertTrue(replay_section["real_market_replay"])
            self.assertTrue(replay_section["source_symbol_matches_target"])
            self.assertEqual(
                replay_section["per_symbol_source"]["SOLUSDT"]["source_symbol"],
                "SOLUSDT",
            )
            self.assertEqual(replay_section["summary"]["total_fills"], 3)
            self.assertEqual(replay_section["aggregate_summary"]["total_fills"], 3)

    def test_canary_validation_uses_tradable_symbol_metrics_not_quarantined_aggregate(self):
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
                        "metrics": {
                            "runtime_status_count": 80,
                            "funnel_fills_runtime_count": 4,
                            "realized_net_per_fill": 0.01,
                            "regime_change_trend_symbols": ["SOLUSDT"],
                            "regime_change_trend_candidate_symbols": ["SOLUSDT"],
                        },
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "target_bucket": "trend",
                        "source_symbol": "SOLUSDT",
                        "symbols": ["SOLUSDT", "ETHUSDT"],
                        "symbol": "SOLUSDT",
                        "selection": {"segments_ran": 8, "coverage_targets_met": True},
                        "aggregate_summary": {
                            "execution_active_runs": 8,
                            "execution_pass_runs": 6,
                            "total_fills": 12,
                            "mean_realized_net_per_fill": -0.01,
                            "median_realized_net_per_fill_with_fills": -0.02,
                            "positive_filled_segment_ratio": 0.25,
                        },
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": ["ETHUSDT"],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.008,
                                        "positive_filled_segment_ratio": 0.75,
                                        "total_fills": 8,
                                    },
                                    "ETHUSDT": {
                                        "status": "quarantined",
                                        "median_realized_net_per_fill_with_fills": -0.05,
                                        "positive_filled_segment_ratio": 0.0,
                                        "total_fills": 4,
                                    },
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
            self.assertEqual(payload["overall_status"], "PASS_WITH_ACTIONS")
            canary = payload["sections"]["canary_validation"]
            self.assertEqual(canary["readiness_status"], "PASS")
            self.assertEqual(
                payload["trading_convergence_status"],
                "NOT_CONVERGED_REPLAY_SAMPLE_INSUFFICIENT",
            )
            self.assertEqual(canary["recommended_live_symbols"], ["SOLUSDT"])
            self.assertEqual(
                canary["replay_metrics"]["basis"],
                "symbol_tradeability.tradable_symbols_min",
            )
            self.assertEqual(
                canary["replay_metrics"]["median_realized_net_per_fill_with_fills"],
                0.008,
            )

    def test_replay_optimizer_failure_blocks_replay_section(self):
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
                        "symbols": ["BTCUSDT"],
                        "aggregate_summary": {
                            "execution_active_runs": 4,
                            "execution_pass_runs": 4,
                            "total_fills": 6,
                            "mean_realized_net_per_fill": 0.001,
                        },
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                        },
                        "execution_optimizer": {
                            "status": "fail",
                            "fail_reasons": [
                                "no_deployable_prefilter_candidate_positive_after_costs"
                            ],
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
            replay_section = payload["sections"]["replay_validation"]
            self.assertEqual(replay_section["status"], "fail")
            self.assertIn(
                "replay_validation: replay execution_optimizer status=fail",
                payload["fail_reasons"],
            )

    def test_replay_live_symbol_alignment_warns_on_uncovered_live_trend(self):
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
                        "market_context_status": "TREND_PRESENT",
                        "metrics": {
                            "runtime_status_count": 80,
                            "regime_change_trend_symbols": ["BNBUSDT", "SOLUSDT"],
                            "regime_change_trend_candidate_symbols": [
                                "BNBUSDT",
                                "ETHUSDT",
                                "SOLUSDT",
                                "XRPUSDT",
                            ],
                        },
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
                            "total_fills": 6,
                            "mean_realized_net_per_fill": 0.0,
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
            self.assertEqual(payload["overall_status"], "PASS_WITH_ACTIONS")
            self.assertEqual(
                payload["replay_symbol_alignment_status"], "PASS_WITH_ACTIONS"
            )
            alignment = payload["sections"]["replay_symbol_alignment"]
            self.assertEqual(
                alignment["uncovered_live_trend_symbols"], ["BNBUSDT", "SOLUSDT"]
            )
            self.assertEqual(
                alignment["recommended_replay_symbols"],
                ["BTCUSDT", "BNBUSDT", "SOLUSDT", "ETHUSDT", "XRPUSDT"],
            )
            self.assertEqual(
                alignment["recommended_replay_symbols_csv"],
                "BTCUSDT,BNBUSDT,SOLUSDT,ETHUSDT,XRPUSDT",
            )
            self.assertEqual(
                alignment["missing_recommended_replay_symbols"],
                ["BNBUSDT", "SOLUSDT", "ETHUSDT", "XRPUSDT"],
            )
            self.assertTrue(
                any("未覆盖 live TREND 符号" in item for item in payload["warn_reasons"])
            )
            self.assertTrue(
                any(
                    "recommended_replay_symbols=BTCUSDT,BNBUSDT,SOLUSDT,ETHUSDT,XRPUSDT"
                    in item
                    for item in payload["warn_reasons"]
                )
            )

    def test_replay_live_symbol_alignment_passes_when_replay_covers_live_trend(self):
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
                        "market_context_status": "TREND_PRESENT",
                        "metrics": {
                            "runtime_status_count": 80,
                            "regime_change_trend_symbols": ["SOLUSDT"],
                            "regime_change_trend_candidate_symbols": ["SOLUSDT"],
                        },
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "target_bucket": "trend",
                        "symbol": "SOLUSDT",
                        "selection": {"segments_ran": 4, "coverage_targets_met": True},
                        "aggregate_summary": {
                            "execution_active_runs": 4,
                            "execution_pass_runs": 4,
                            "total_fills": 6,
                            "mean_realized_net_per_fill": 0.0,
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
            self.assertEqual(payload["overall_status"], "PASS_WITH_ACTIONS")
            self.assertEqual(payload["replay_symbol_alignment_status"], "PASS")
            alignment = payload["sections"]["replay_symbol_alignment"]
            self.assertEqual(alignment["uncovered_live_trend_symbols"], [])
            self.assertEqual(alignment["recommended_replay_symbols"], ["SOLUSDT"])
            self.assertEqual(alignment["missing_recommended_replay_symbols"], [])
            self.assertFalse(
                any(
                    item.startswith("replay_symbol_alignment:")
                    for item in payload["warn_reasons"]
                )
            )

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

    def test_feature_parity_missing_metrics_is_not_evaluated(self):
        section = REPORT.assess_feature_parity(
            {"metrics": {"runtime_status_count": 10}}
        )

        self.assertEqual(section["status"], "pass")
        self.assertEqual(section["readiness_status"], "NOT_EVALUATED")

    def test_canary_tradeability_requires_symbol_decision_metrics(self):
        section = REPORT.assess_canary_validation(
            {
                "source_symbol": "SOLUSDT",
                "aggregate_summary": {
                    "median_realized_net_per_fill_with_fills": 0.02,
                    "positive_filled_segment_ratio": 0.80,
                    "total_fills": 10,
                },
                "symbol_tradeability": {
                    "status": "pass",
                    "tradable_symbols": ["SOLUSDT"],
                    "quarantined_symbols": [],
                    "decisions": {},
                },
            },
            {},
        )

        self.assertEqual(section["status"], "fail")
        self.assertIn(
            "canary symbol_tradeability decision missing for SOLUSDT",
            section["fail_reasons"],
        )
        self.assertEqual(
            section["replay_metrics"]["basis"],
            "symbol_tradeability.tradable_symbols_min",
        )

    def test_exit_capture_low_mean_fails_without_low_segment_counter(self):
        section = REPORT.assess_exit_capture(
            {
                "exit_capture": {
                    "sample_count": 3,
                    "mean_gross_capture_of_path_mfe": 0.05,
                }
            },
            {},
        )

        self.assertEqual(section["status"], "fail")
        self.assertIn(
            "replay mean_gross_capture_of_path_mfe=0.050000 < 0.100000",
            section["fail_reasons"],
        )

    def test_exit_capture_uses_source_symbol_when_aggregate_is_contaminated(self):
        section = REPORT.assess_exit_capture(
            {
                "source_symbol": "SOLUSDT",
                "symbol_tradeability": {
                    "tradable_symbols": ["SOLUSDT"],
                    "quarantined_symbols": ["ETHUSDT"],
                },
                "exit_capture": {
                    "sample_count": 20,
                    "primary_diagnosis": "exit_capture_low",
                    "mean_gross_capture_of_path_mfe": 0.03,
                },
                "exit_capture_by_symbol": {
                    "SOLUSDT": {
                        "sample_count": 8,
                        "primary_diagnosis": "ok",
                        "mean_gross_capture_of_path_mfe": 0.22,
                    },
                    "ETHUSDT": {
                        "sample_count": 12,
                        "primary_diagnosis": "exit_capture_low",
                        "mean_gross_capture_of_path_mfe": 0.02,
                    },
                },
            },
            {},
        )

        self.assertEqual(section["status"], "pass")
        self.assertEqual(section["fail_reasons"], [])
        self.assertEqual(section["replay"]["sample_count"], 8)
        self.assertEqual(
            section["replay"]["selected_by_symbol"]["SOLUSDT"]["sample_count"],
            8,
        )

    def test_replay_validation_suppresses_aggregate_fail_when_tradeable_canary_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass_with_actions",
                        "source_symbol": "SOLUSDT",
                        "aggregate_validation": {
                            "status": "fail",
                            "fail_reasons": ["aggregate net negative"],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "status": "pass",
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": ["ETHUSDT"],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.02,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    },
                                    "ETHUSDT": {"status": "quarantined"},
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_replay_validation(replay_report)

            self.assertEqual(section["status"], "pass")
            self.assertEqual(section["fail_reasons"], [])
            self.assertIn(
                "aggregate net negative",
                "; ".join(section["suppressed_aggregate_fail_reasons"]),
            )

    def test_replay_validation_skip_report_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass_with_actions",
                        "validation_skipped": True,
                        "skip_reason": "feature_store_missing",
                        "selection": {
                            "selection_mode": "not_run",
                            "stop_reason": "feature_store_missing",
                        },
                        "aggregate_validation": {
                            "status": "pass_with_actions",
                            "fail_reasons": [],
                            "warn_reasons": ["skipped"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_replay_validation(replay_report)

            self.assertEqual(section["status"], "fail")
            self.assertIn(
                "replay-validation skipped/not_run: reason=feature_store_missing",
                section["fail_reasons"],
            )

    def test_trading_convergence_requires_live_fills(self):
        section = REPORT.assess_trading_convergence(
            {
                "verdict": "PASS",
                "execution_status": "NOT_EVALUATED",
                "runtime_validation_mode": "POLICY_FLAT_PROTECTION",
                "market_context_status": "RANGE_ONLY",
                "metrics": {"funnel_fills_runtime_count": 0},
            },
            {"aggregate_summary": {"total_fills": 20}},
            {},
            {"readiness_status": "PASS"},
            {
                "readiness_status": "PASS",
                "replay": {
                    "sample_count": 10,
                    "mean_gross_capture_of_path_mfe": 0.20,
                },
            },
            {
                "readiness_status": "PASS",
                "replay_metrics": {
                    "total_fills": 20,
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.60,
                },
            },
        )

        self.assertEqual(section["readiness_status"], "NOT_CONVERGED_NO_LIVE_FILLS")

    def test_trading_convergence_passes_with_replay_exit_parity_and_live_fills(self):
        section = REPORT.assess_trading_convergence(
            {
                "verdict": "PASS",
                "execution_status": "PASS",
                "runtime_validation_mode": "EXECUTION_ACTIVE",
                "metrics": {
                    "funnel_fills_runtime_count": 4,
                    "realized_net_per_fill": 0.01,
                },
            },
            {"aggregate_summary": {"total_fills": 20}},
            {},
            {"readiness_status": "PASS"},
            {
                "readiness_status": "PASS",
                "replay": {
                    "sample_count": 10,
                    "mean_gross_capture_of_path_mfe": 0.20,
                },
            },
            {
                "readiness_status": "PASS",
                "replay_metrics": {
                    "total_fills": 20,
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.60,
                },
            },
        )

        self.assertEqual(
            section["readiness_status"],
            "CONVERGED_CANARY_VALIDATED_WITH_LIVE_FILLS",
        )
        self.assertEqual(section["blockers"], [])

    def test_run_manifest_expected_run_id_missing_fails(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = pathlib.Path(td) / "run_manifest.json"
            manifest.write_text(json.dumps({"git": {}}), encoding="utf-8")

            section = REPORT.assess_run_manifest(manifest, "gha-1-1")

            self.assertEqual(section["status"], "fail")
            self.assertIn(
                "run manifest run_id missing; expected=gha-1-1",
                section["fail_reasons"],
            )

    def test_replay_command_failure_drives_next_action_plan(self):
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
                        "market_context_status": "RANGE_ONLY",
                        "metrics": {"runtime_status_count": 80},
                        "account_pnl": {"samples": 80},
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "fail",
                        "validation_skipped": True,
                        "skip_reason": "command_failed",
                        "target_bucket": "trend",
                        "source_symbol": "ETHUSDT",
                        "symbol": "ETHUSDT",
                        "symbols": ["ETHUSDT", "BTCUSDT"],
                        "fail_reasons": [
                            "replay validation command failed: exit_code=2"
                        ],
                        "selection": {
                            "selection_mode": "not_run",
                            "stop_reason": "command_failed",
                            "segments_ran": 0,
                        },
                        "aggregate_summary": {"total_fills": 0},
                        "aggregate_validation": {
                            "status": "fail",
                            "fail_reasons": [
                                "replay validation command failed: exit_code=2"
                            ],
                            "warn_reasons": [],
                        },
                        "failure_diagnostics": {
                            "schema_version": "replay_command_failure_v1",
                            "exit_code": 2,
                            "command_log_path": "data/reports/x/replay_validation/replay_validation_command.log",
                            "command_output_tail_line_count": 3,
                            "command_output_tail": ["Traceback", "boom"],
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
            self.assertEqual(
                payload["next_action_plan"]["first_blocking_layer"],
                "replay_execution",
            )
            self.assertEqual(
                payload["next_action_plan"]["primary_next_action"],
                "inspect_replay_failure_diagnostics_and_fix_replay_command",
            )
            self.assertTrue(
                payload["convergence_layers"]["replay_command_failure"]["present"]
            )
            self.assertTrue(
                payload["convergence_layers"]["replay_command_failure"][
                    "has_failure_diagnostics"
                ]
            )
            self.assertEqual(
                payload["sections"]["replay_validation"]["failure_diagnostics"][
                    "exit_code"
                ],
                2,
            )


if __name__ == "__main__":
    unittest.main()
