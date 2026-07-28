#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("closed_loop_mechanism_audit.py")
    spec = importlib.util.spec_from_file_location("closed_loop_mechanism_audit", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AUDIT = load_module()


def write_json(path: pathlib.Path, payload) -> pathlib.Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ClosedLoopMechanismAuditTest(unittest.TestCase):
    def test_auc_shadow_pipeline_is_not_mechanism_proven(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            integrator = write_json(
                root / "integrator_report.json",
                {
                    "model_version": "candidate-v1",
                    "data": {
                        "training_symbol": "SOLUSDT",
                        "bar_interval_ms": 300000,
                        "online_bar_source": "closed_ohlcv",
                        "source_venue": "bybit",
                        "source_category": "linear",
                        "price_type": "trade_price",
                        "volume_unit": "base_asset",
                    },
                    "metrics_oos": {
                        "auc_mean": 0.56,
                        "random_label_auc_mean": 0.50,
                        "random_label_auc_max": 0.53,
                        "random_label_trials": 5,
                    },
                    "governance": {
                        "thresholds": {
                            "run_random_label_control": True,
                            "max_random_label_auc": 0.55,
                            "min_auc_mean": 0.50,
                        }
                    },
                    "train_config": {
                        "label_round_trip_cost_bps": 13.0,
                        "label_min_net_edge_bps": 1.3,
                    },
                },
            )
            replay = write_json(
                root / "replay_validation_report.json",
                {
                    "status": "pass",
                    "source_symbol": "SOLUSDT",
                    "execution_evidence_contract": {
                        "evidence_role": "offline_conservative_execution_prescreen",
                        "production_promotion_authority": False,
                        "live_candidate_episode_canary_required": True,
                    },
                    "activation_gate": {"status": "pass"},
                    "execution_economics": {
                        "mean_realized_net_per_fill_with_fills": 0.004,
                    },
                    "exit_capture": {"mean_fee_bps_per_fill": 3.5},
                    "aggregate_summary": {
                        "total_fills": 40,
                        "mean_realized_net_per_fill_with_fills": 0.004,
                        "positive_filled_segment_ratio": 0.58,
                    },
                },
            )
            runtime = write_json(
                root / "runtime_assess.json",
                {
                    "metrics": {
                        "integrator_mode_shadow_count": 100,
                        "integrator_mode_canary_count": 0,
                        "integrator_mode_active_count": 0,
                        "integrator_policy_applied_count": 0,
                        "integrator_shadow_scored_runtime_count": 100,
                    }
                },
            )
            registry = write_json(
                root / "registry.json",
                {"gate_pass": True, "activation_gate": {"status": "pass"}},
            )
            alpha_probe = write_json(
                root / "alpha_mechanism_probe_report.json",
                {
                    "status": "pass_with_actions",
                    "mechanism_control_status": "pass",
                    "market_alpha_family_status": "fail",
                    "candidate_search": {
                        "pass_candidate_count": 0,
                        "best_candidate": {"name": "trend_follow"},
                    },
                },
            )

            args = type(
                "Args",
                (),
                {
                    "integrator_report": str(integrator),
                    "registry_report": str(registry),
                    "runtime_assess_report": str(runtime),
                    "replay_validation_report": str(replay),
                    "replay_optimization_report": "",
                    "strategy_diagnose_report": "",
                    "alpha_mechanism_probe_report": str(alpha_probe),
                    "run_manifest": "",
                    "control_cost_bps": None,
                    "min_live_policy_applied": 1,
                    "min_replay_total_fills": 20,
                },
            )()
            report = AUDIT.build_report(args)

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["conclusion"], "MECHANISM_NOT_PROVEN")
            self.assertEqual(report["checks"]["negative_control"]["status"], "pass")
            self.assertEqual(report["checks"]["positive_control"]["status"], "pass")
            self.assertEqual(report["checks"]["alpha_mechanism_probe"]["status"], "fail")
            self.assertEqual(report["checks"]["target_consistency"]["status"], "fail")
            self.assertEqual(report["checks"]["model_influence"]["status"], "fail")
            self.assertTrue(
                any(
                    "primary objective" in item
                    for item in report["checks"]["target_consistency"]["fail_reasons"]
                )
            )

    def test_net_objective_canary_pipeline_can_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            integrator = write_json(
                root / "integrator_report.json",
                {
                    "model_version": "candidate-v1",
                    "data": {
                        "training_symbol": "SOLUSDT",
                        "bar_interval_ms": 300000,
                        "online_bar_source": "closed_ohlcv",
                        "source_venue": "bybit",
                        "source_category": "linear",
                        "price_type": "trade_price",
                        "volume_unit": "base_asset",
                        "time_axis_quality": {"pass": True},
                    },
                    "metrics_oos": {
                        "primary_objective": AUDIT.EXPECTED_MODEL_OBJECTIVE,
                        "evidence_tier": "offline_model_economic_prescreen",
                        "authoritative_promotion_evidence": "live_candidate_episode_canary",
                        "required_offline_prescreen": (
                            "independent_cpp_replay_next_bar_ohlc_touch"
                        ),
                        "mean_model_net_edge_bps": 0.35,
                        "positive_model_net_edge_ratio": 0.65,
                        "model_net_total_trades": 40,
                        "model_net_active_bar_count": 400,
                        "model_net_edge_lcb_bps": 0.02,
                        "oos_duplicate_bar_ratio": 0.0,
                        "random_label_auc_mean": 0.50,
                        "random_label_auc_max": 0.53,
                        "random_label_trials": 5,
                    },
                    "governance": {
                        "primary_objective": AUDIT.EXPECTED_MODEL_OBJECTIVE,
                        "thresholds": {
                            "run_random_label_control": True,
                            "max_random_label_auc": 0.55,
                            "min_mean_model_net_edge_bps": 0.0,
                            "min_positive_model_net_edge_ratio": 0.50,
                        }
                    },
                    "train_config": {
                        "label_round_trip_cost_bps": 3.5,
                        "label_min_net_edge_bps": 0.5,
                    },
                    "anti_leakage": {
                        "split_axis": "raw_bar_index_before_label_filter",
                        "oos_windows_non_overlapping": True,
                    },
                },
            )
            replay = write_json(
                root / "replay_validation_report.json",
                {
                    "status": "pass",
                    "source_symbol": "SOLUSDT",
                    "execution_evidence_contract": {
                        "schema_version": "replay_execution_prescreen_v1",
                        "evidence_role": "offline_conservative_execution_prescreen",
                        "fill_model": "next_bar_ohlc_touch_at_limit_no_queue_position",
                        "production_promotion_authority": False,
                        "live_candidate_episode_canary_required": True,
                    },
                    "activation_gate": {"status": "pass"},
                    "candidate_identity": {
                        "model_version": "candidate-v1",
                        "model_sha256": "model-sha",
                        "integrator_report_sha256": "report-sha",
                        "config_binds_candidate": True,
                    },
                    "execution_economics": {
                        "mean_realized_net_per_fill_with_fills": 0.01,
                    },
                    "exit_capture": {"mean_fee_bps_per_fill": 3.5},
                    "aggregate_summary": {
                        "total_fills": 40,
                        "mean_realized_net_per_fill_with_fills": 0.01,
                        "positive_filled_segment_ratio": 0.70,
                    },
                },
            )
            runtime = write_json(
                root / "runtime_assess.json",
                {
                    "metrics": {
                        "funnel_fills_runtime_count": 2,
                        "integrator_mode_shadow_count": 0,
                        "integrator_mode_canary_count": 50,
                        "integrator_mode_active_count": 0,
                        "integrator_policy_applied_count": 3,
                        "integrator_policy_proposed_count": 4,
                        "integrator_policy_enqueued_count": 3,
                        "integrator_policy_filled_count": 2,
                        "integrator_policy_unique_filled_order_count": 2,
                        "integrator_policy_complete_episode_count": 2,
                        "integrator_policy_canary_count": 3,
                        "integrator_feature_training_symbol_latest": "SOLUSDT",
                        "integrator_feature_bar_interval_ms_latest": 300000,
                        "integrator_history_bootstrap_count": 1,
                    }
                },
            )
            registry = write_json(
                root / "registry.json",
                {
                    "gate_pass": True,
                    "model_version": "candidate-v1",
                    "activation_gate": {"status": "pass"},
                    "checksums": {
                        "model_sha256": "model-sha",
                        "integrator_report_sha256": "report-sha",
                    },
                },
            )
            alpha_probe = write_json(
                root / "alpha_mechanism_probe_report.json",
                {
                    "status": "pass",
                    "mechanism_control_status": "pass",
                    "market_alpha_family_status": "pass",
                    "candidate_search": {
                        "pass_candidate_count": 1,
                        "best_candidate": {"name": "mom12_follow"},
                    },
                },
            )

            args = type(
                "Args",
                (),
                {
                    "integrator_report": str(integrator),
                    "registry_report": str(registry),
                    "runtime_assess_report": str(runtime),
                    "replay_validation_report": str(replay),
                    "replay_optimization_report": "",
                    "strategy_diagnose_report": "",
                    "alpha_mechanism_probe_report": str(alpha_probe),
                    "run_manifest": "",
                    "control_cost_bps": None,
                    "min_live_policy_applied": 1,
                    "min_replay_total_fills": 20,
                },
            )()
            report = AUDIT.build_report(args)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["conclusion"], "MECHANISM_PROVEN")


if __name__ == "__main__":
    unittest.main()
