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
                    "metrics_oos": {
                        "primary_objective": "model_net_edge_bps_after_cost",
                        "mean_model_net_edge_bps": 0.35,
                        "positive_model_net_edge_ratio": 0.65,
                        "random_label_auc_mean": 0.50,
                        "random_label_auc_max": 0.53,
                        "random_label_trials": 5,
                    },
                    "governance": {
                        "primary_objective": "model_net_edge_bps_after_cost",
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
                },
            )
            replay = write_json(
                root / "replay_validation_report.json",
                {
                    "status": "pass",
                    "activation_gate": {"status": "pass"},
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
                        "integrator_policy_canary_count": 3,
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
