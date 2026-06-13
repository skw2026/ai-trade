#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("compare_closed_loop_reports.py")
    spec = importlib.util.spec_from_file_location("compare_closed_loop_reports", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COMPARE = load_module()


class CompareClosedLoopReportsTest(unittest.TestCase):
    def test_build_comparison_counts_repeated_blockers(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            run1 = root / "run1"
            run2 = root / "run2"
            run1.mkdir()
            run2.mkdir()
            report1 = run1 / "closed_loop_report.json"
            report2 = run2 / "closed_loop_report.json"
            report1.write_text(
                json.dumps(
                    {
                        "run_id": "run1",
                        "overall_status": "FAIL",
                        "runtime_validation_class": "PROTECTION_PASS_NO_TRADE_VALIDATION",
                        "next_action_plan": {
                            "first_blocking_layer": "replay_execution",
                            "primary_next_action": "inspect_replay_failure_diagnostics_and_fix_replay_command",
                            "primary_reason": "replay_validation command_failed",
                        },
                        "convergence_layers": {
                            "replay_command_failure": {
                                "present": True,
                                "has_failure_diagnostics": False,
                            }
                        },
                        "sections": {
                            "runtime": {
                                "market_context_status": "RANGE_ONLY",
                                "metrics": {"funnel_fills_runtime_count": 0},
                            },
                            "replay_validation": {
                                "status": "fail",
                                "skip_reason": "command_failed",
                                "aggregate_summary": {"total_fills": 0},
                            },
                            "trading_convergence": {
                                "blockers": [
                                    "NOT_CONVERGED_REPLAY_CANARY_FAIL",
                                    "NOT_CONVERGED_NO_LIVE_FILLS",
                                ]
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            report2.write_text(
                json.dumps(
                    {
                        "run_id": "run2",
                        "overall_status": "FAIL",
                        "next_action_plan": {
                            "first_blocking_layer": "strategy_raw_edge",
                            "primary_next_action": "redesign_alpha_label_or_exit_objective_before_widening_live_gates",
                        },
                        "sections": {
                            "runtime": {
                                "metrics": {"trend_candidate_probe_fill_count": 2},
                            },
                            "replay_validation": {
                                "status": "pass",
                                "aggregate_summary": {"total_fills": 24},
                            },
                            "strategy_diagnose": {
                                "aggregate": {
                                    "confirmed_trend": {
                                        "sample_count": 99,
                                        "mean_net_forward_bps": -3.5,
                                        "positive_net_ratio": 0.42,
                                    }
                                }
                            },
                            "trading_convergence": {
                                "blockers": [
                                    "NOT_CONVERGED_STRATEGY_RAW_EDGE_FAIL"
                                ]
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            comparison = COMPARE.build_comparison([report1, report2])

            self.assertEqual(comparison["report_count"], 2)
            self.assertEqual(
                comparison["counts"]["first_blocking_layer"]["replay_execution"],
                1,
            )
            self.assertEqual(
                comparison["counts"]["first_blocking_layer"]["strategy_raw_edge"],
                1,
            )
            self.assertEqual(
                comparison["counts"]["replay_command_failure_without_diagnostics"],
                1,
            )
            self.assertEqual(comparison["latest"]["run_id"], "run2")
            self.assertEqual(comparison["latest"]["live_fills"], 2)
            self.assertEqual(
                comparison["latest"]["confirmed_trend"]["mean_net_forward_bps"],
                -3.5,
            )

    def test_find_report_paths_honors_limit(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = []
            for idx in range(3):
                run_dir = root / f"run{idx}"
                run_dir.mkdir()
                path = run_dir / "closed_loop_report.json"
                path.write_text("{}", encoding="utf-8")
                paths.append(path)

            found = COMPARE.find_report_paths(root, "*/closed_loop_report.json", 2)

            self.assertEqual(len(found), 2)
            self.assertEqual(found[-1].name, "closed_loop_report.json")

    def test_legacy_report_gets_inferred_reason(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report = root / "closed_loop_report.json"
            report.write_text(
                json.dumps(
                    {
                        "run_id": "legacy",
                        "overall_status": "FAIL",
                        "replay_readiness_status": "FAIL",
                        "sections": {
                            "runtime": {
                                "metrics": {"funnel_fills_runtime_count": 0},
                            },
                            "replay_validation": {
                                "status": "fail",
                                "skip_reason": "command_failed",
                                "aggregate_summary": {"total_fills": 0},
                            },
                            "trading_convergence": {
                                "blockers": [
                                    "NOT_CONVERGED_REPLAY_CANARY_FAIL",
                                    "NOT_CONVERGED_NO_LIVE_FILLS",
                                ]
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            row = COMPARE.summarize_report(report)

            self.assertEqual(row["first_blocking_layer"], "replay_execution")
            self.assertEqual(
                row["primary_next_action"],
                "inspect_replay_failure_diagnostics_and_fix_replay_command",
            )
            self.assertEqual(row["primary_reason"], "replay_validation command_failed")
            self.assertTrue(row["replay_command_failed"])


if __name__ == "__main__":
    unittest.main()
