#!/usr/bin/env python3

import importlib.util
import argparse
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("model_registry.py")
    spec = importlib.util.spec_from_file_location("model_registry", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REGISTRY = load_module()


class ModelRegistryTest(unittest.TestCase):
    def test_gate_integrator_report_propagates_governance_fail_reasons(self):
        gate_pass, fail_reasons, warn_reasons, summary = REGISTRY.gate_integrator_report(
            report={
                "metrics_oos": {
                    "auc_mean": 0.51,
                    "delta_auc_vs_baseline": 0.02,
                    "split_trained_count": 5,
                    "split_count": 5,
                    "split_trained_ratio": 1.0,
                    "auc_stdev": 0.12,
                    "train_test_auc_gap_mean": 0.18,
                    "random_label_auc": 0.58,
                    "random_label_auc_mean": 0.57,
                    "random_label_auc_stdev": 0.03,
                    "random_label_auc_max": 0.61,
                },
                "governance": {
                    "pass": False,
                    "fail_reasons": [
                        "auc_stdev=0.120000 > max_auc_stdev=0.080000",
                        "random_label_auc=0.580000 > max_random_label_auc=0.550000",
                    ],
                    "warn_reasons": [
                        "random_label_auc_max=0.610000 > soft_cap=0.580000",
                    ],
                },
            },
            min_auc_mean=0.48,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=1,
            min_split_trained_ratio=0.5,
        )

        self.assertFalse(gate_pass)
        self.assertIn("integrator_report.governance.pass=false", fail_reasons)
        self.assertIn(
            "governance: auc_stdev=0.120000 > max_auc_stdev=0.080000",
            fail_reasons,
        )
        self.assertIn(
            "governance: random_label_auc=0.580000 > max_random_label_auc=0.550000",
            fail_reasons,
        )
        self.assertIn(
            "governance: random_label_auc_max=0.610000 > soft_cap=0.580000",
            warn_reasons,
        )
        self.assertEqual(summary["auc_mean"], 0.51)
        self.assertEqual(summary["auc_stdev"], 0.12)
        self.assertEqual(summary["train_test_auc_gap_mean"], 0.18)
        self.assertEqual(summary["random_label_auc"], 0.58)
        self.assertEqual(summary["random_label_auc_mean"], 0.57)
        self.assertEqual(summary["random_label_auc_stdev"], 0.03)
        self.assertEqual(summary["random_label_auc_max"], 0.61)

    def test_replay_fail_prevents_activation_even_when_integrator_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            model_file = root / "integrator_latest.cbm"
            integrator_report = root / "integrator_report.json"
            walkforward_report = root / "walkforward_report.json"
            replay_report = root / "replay_validation_report.json"
            registration_out = root / "registry_out.json"

            model_file.write_bytes(b"fake model")
            integrator_report.write_text(
                json.dumps(
                    {
                        "model_version": "integrator_cb_v1",
                        "feature_schema_version": "feature_schema_v1",
                        "factor_set_version": "factor_set_v1",
                        "metrics_oos": {
                            "auc_mean": 0.56,
                            "delta_auc_vs_baseline": 0.02,
                            "split_trained_count": 5,
                            "split_count": 5,
                            "split_trained_ratio": 1.0,
                        },
                        "governance": {"pass": True, "fail_reasons": [], "warn_reasons": []},
                    }
                ),
                encoding="utf-8",
            )
            walkforward_report.write_text(
                json.dumps(
                    {
                        "summary": {
                            "avg_split_return": 0.001,
                            "enabled_avg_split_return": 0.001,
                            "traded_avg_split_return": 0.001,
                            "traded_split_count": 4,
                            "total_trades": 20,
                        }
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "fail",
                        "fail_reasons": ["ROBUST filled replay segments are all net-negative"],
                        "warn_reasons": [],
                        "aggregate_validation": {
                            "execution_active_runs": 10,
                            "execution_pass_runs": 10,
                            "total_fills": 10,
                            "negative_realized_net_with_fills_runs": 10,
                            "mean_realized_net_per_fill": -0.001,
                        },
                    }
                ),
                encoding="utf-8",
            )

            code = REGISTRY.run_register(
                argparse.Namespace(
                    model_file=str(model_file),
                    integrator_report=str(integrator_report),
                    miner_report="",
                    walkforward_report=str(walkforward_report),
                    replay_validation_report=str(replay_report),
                    registry_dir=str(root / "registry"),
                    max_versions=20,
                    active_model_path=str(root / "active" / "integrator_latest.cbm"),
                    active_report_path=str(root / "active" / "integrator_report.json"),
                    active_miner_report_path=str(root / "active" / "miner_report.json"),
                    active_meta_path=str(root / "active" / "integrator_active.json"),
                    min_auc_mean=0.50,
                    min_delta_auc_vs_baseline=0.0,
                    min_split_trained_count=1,
                    min_split_trained_ratio=0.5,
                    activate_on_pass=True,
                    require_walkforward_positive=True,
                    min_walkforward_avg_split_return=0.0,
                    min_walkforward_enabled_avg_split_return=0.0,
                    min_walkforward_traded_avg_split_return=0.0,
                    require_replay_validation_pass=True,
                    registration_out=str(registration_out),
                )
            )

            self.assertEqual(code, 0)
            payload = json.loads(registration_out.read_text(encoding="utf-8"))
            self.assertFalse(payload["activated"])
            self.assertFalse(payload["gate"]["pass"])
            self.assertFalse((root / "active" / "integrator_latest.cbm").exists())
            self.assertTrue(
                any(
                    "replay_validation: replay_validation status=fail != pass" in reason
                    for reason in payload["gate"]["fail_reasons"]
                )
            )
            self.assertTrue(
                any(
                    "ROBUST filled replay segments are all net-negative" in reason
                    for reason in payload["gate"]["fail_reasons"]
                )
            )

    def test_replay_optimizer_fail_prevents_activation_even_when_replay_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            model_file = root / "integrator_latest.cbm"
            integrator_report = root / "integrator_report.json"
            replay_report = root / "replay_validation_report.json"
            registration_out = root / "registry_out.json"

            model_file.write_bytes(b"fake model")
            integrator_report.write_text(
                json.dumps(
                    {
                        "model_version": "integrator_cb_v1",
                        "feature_schema_version": "feature_schema_v1",
                        "factor_set_version": "factor_set_v1",
                        "metrics_oos": {
                            "auc_mean": 0.56,
                            "delta_auc_vs_baseline": 0.02,
                            "split_trained_count": 5,
                            "split_count": 5,
                            "split_trained_ratio": 1.0,
                        },
                        "governance": {
                            "pass": True,
                            "fail_reasons": [],
                            "warn_reasons": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
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

            code = REGISTRY.run_register(
                argparse.Namespace(
                    model_file=str(model_file),
                    integrator_report=str(integrator_report),
                    miner_report="",
                    walkforward_report="",
                    replay_validation_report=str(replay_report),
                    registry_dir=str(root / "registry"),
                    max_versions=20,
                    active_model_path=str(root / "active" / "integrator_latest.cbm"),
                    active_report_path=str(root / "active" / "integrator_report.json"),
                    active_miner_report_path=str(root / "active" / "miner_report.json"),
                    active_meta_path=str(root / "active" / "integrator_active.json"),
                    min_auc_mean=0.50,
                    min_delta_auc_vs_baseline=0.0,
                    min_split_trained_count=1,
                    min_split_trained_ratio=0.5,
                    activate_on_pass=True,
                    require_walkforward_positive=False,
                    min_walkforward_avg_split_return=0.0,
                    min_walkforward_enabled_avg_split_return=0.0,
                    min_walkforward_traded_avg_split_return=0.0,
                    require_replay_validation_pass=True,
                    registration_out=str(registration_out),
                )
            )

            self.assertEqual(code, 0)
            payload = json.loads(registration_out.read_text(encoding="utf-8"))
            self.assertFalse(payload["activated"])
            self.assertTrue(
                any(
                    "replay_validation: replay execution_optimizer status=fail" in reason
                    for reason in payload["gate"]["fail_reasons"]
                )
            )

    def test_replay_source_symbol_quarantine_prevents_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "source_symbol": "BTCUSDT",
                        "source_symbols": {
                            "BTCUSDT": "BTCUSDT",
                            "ETHUSDT": "ETHUSDT",
                        },
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "tradable_symbols": ["ETHUSDT", "SOLUSDT"],
                                "quarantined_symbols": ["BTCUSDT"],
                                "decisions": {
                                    "BTCUSDT": {"status": "quarantined"},
                                    "ETHUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    },
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    },
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertEqual(warn_reasons, [])
            self.assertIn(
                "replay source_symbol=BTCUSDT is quarantined by symbol_tradeability",
                fail_reasons,
            )
            self.assertEqual(summary["source_symbol"], "BTCUSDT")

    def test_replay_economic_gate_uses_tradable_symbol_metrics_before_aggregate(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "source_symbol": "SOLUSDT",
                        "activation_gate": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                        },
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "median_realized_net_per_fill_with_fills": -0.02,
                            "positive_filled_segment_ratio": 0.25,
                            "symbol_tradeability": {
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": ["ETHUSDT"],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.008,
                                        "positive_filled_segment_ratio": 0.75,
                                        "total_fills": 20,
                                    },
                                    "ETHUSDT": {
                                        "status": "quarantined",
                                        "median_realized_net_per_fill_with_fills": -0.05,
                                        "positive_filled_segment_ratio": 0.0,
                                    },
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertTrue(passed)
            self.assertEqual(fail_reasons, [])
            self.assertEqual(warn_reasons, [])
            self.assertEqual(
                summary["economic_gate_basis"],
                "symbol_tradeability.tradable_symbols_min",
            )

    def test_replay_tradeability_pass_suppresses_aggregate_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "fail",
                        "source_symbol": "SOLUSDT",
                        "activation_gate": {
                            "status": "pass_with_actions",
                            "fail_reasons": [],
                            "warn_reasons": [
                                "aggregate_validation_failed_but_symbol_tradeability_passed: aggregate median net negative"
                            ],
                        },
                        "aggregate_validation": {
                            "status": "fail",
                            "fail_reasons": ["aggregate median net negative"],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "status": "pass",
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": ["ETHUSDT"],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.75,
                                        "total_fills": 20,
                                    },
                                    "ETHUSDT": {
                                        "status": "quarantined",
                                        "median_realized_net_per_fill_with_fills": -0.05,
                                        "positive_filled_segment_ratio": 0.0,
                                    },
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertTrue(passed)
            self.assertEqual(fail_reasons, [])
            self.assertTrue(warn_reasons)
            self.assertIn(
                "aggregate median net negative",
                "; ".join(summary["suppressed_aggregate_fail_reasons"]),
            )

    def test_replay_activation_gate_blocking_warning_prevents_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass_with_actions",
                        "source_symbol": "SOLUSDT",
                        "activation_gate": {
                            "status": "pass_with_actions",
                            "fail_reasons": [],
                            "warn_reasons": [
                                "execution_cost_plan.candidate_requires_rerun: lower-cost candidate needs replay rerun"
                            ],
                        },
                        "aggregate_validation": {
                            "status": "pass_with_actions",
                            "fail_reasons": [],
                            "warn_reasons": [
                                "execution_cost_plan.candidate_requires_rerun"
                            ],
                            "symbol_tradeability": {
                                "status": "pass",
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": [],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    }
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertTrue(warn_reasons)
            self.assertIn(
                "replay activation_gate pass_with_actions has blocking warnings",
                "; ".join(fail_reasons),
            )
            self.assertEqual(summary["activation_gate"]["status"], "pass_with_actions")

    def test_replay_tradeability_requires_symbol_decision_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "source_symbol": "SOLUSDT",
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "status": "pass",
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": [],
                                "decisions": {},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertEqual(warn_reasons, [])
            self.assertIn(
                "replay symbol_tradeability decision missing for SOLUSDT",
                fail_reasons,
            )
            self.assertEqual(
                summary["economic_gate_basis"],
                "symbol_tradeability.tradable_symbols_min",
            )

    def test_replay_exit_capture_low_prevents_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "source_symbol": "SOLUSDT",
                        "exit_capture": {
                            "sample_count": 5,
                            "primary_diagnosis": "exit_capture_low",
                            "mean_gross_capture_of_path_mfe": 0.05,
                        },
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": [],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    }
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertEqual(warn_reasons, [])
            self.assertIn(
                "replay exit_capture_low: path MFE covers cost but gross capture is too low",
                fail_reasons,
            )
            self.assertEqual(summary["exit_capture"]["sample_count"], 5)

    def test_replay_skip_report_prevents_activation(self):
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

            passed, fail_reasons, _, _ = REGISTRY.gate_replay_validation_report(
                replay_report,
                True,
            )

            self.assertFalse(passed)
            self.assertIn(
                "replay_validation skipped/not_run: reason=feature_store_missing",
                fail_reasons,
            )

    def test_replay_feature_build_failure_on_tradable_symbol_prevents_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "source_symbol": "SOLUSDT",
                        "feature_build": {"failed_symbols": ["SOLUSDT"]},
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
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    }
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, _, _ = REGISTRY.gate_replay_validation_report(
                replay_report,
                True,
            )

            self.assertFalse(passed)
            self.assertIn(
                "replay real-market feature build failed for source/tradable symbols=SOLUSDT",
                fail_reasons,
            )

    def test_walkforward_focus_bucket_does_not_waive_global_negative_returns(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            walkforward_report = root / "walkforward_report.json"
            walkforward_report.write_text(
                json.dumps(
                    {
                        "summary": {
                            "avg_split_return": -0.001,
                            "enabled_avg_split_return": -0.001,
                            "traded_avg_split_return": -0.001,
                            "traded_split_count": 5,
                            "total_trades": 10,
                            "regime_bucket_summary": {
                                "trend": {
                                    "bars": 1500,
                                    "trades": 4,
                                    "sharpe": 2.0,
                                },
                                "range": {"bars": 2000, "trades": 6, "sharpe": -2.0},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = REGISTRY.gate_walkforward_report(
                walkforward_report,
                True,
                0.0,
                0.0,
                0.0,
                focus_bucket="trend",
                min_focus_bucket_bars=1000,
                min_focus_bucket_trades=1,
                min_focus_bucket_sharpe=0.0,
            )

            self.assertFalse(passed)
            self.assertTrue(fail_reasons)
            self.assertEqual(warn_reasons, [])
            self.assertEqual(
                summary["focus_bucket_validation"]["status"],
                "pass",
            )


if __name__ == "__main__":
    unittest.main()
