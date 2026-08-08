#!/usr/bin/env python3

import importlib.util
import hashlib
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


def artifact(path: pathlib.Path) -> dict:
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


class ClosedLoopMechanismAuditTest(unittest.TestCase):
    def test_existing_unresolved_route_does_not_fall_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            route = write_json(
                root / "route.json",
                {
                    "schema_version": "alpha_source_route_v1",
                    "status": "NOT_READY",
                    "selected_route": None,
                    "reason": "no_independently_gated_alpha_source_ready",
                },
            )
            args = type(
                "Args",
                (),
                {
                    "integrator_report": "",
                    "registry_report": "",
                    "runtime_assess_report": "",
                    "replay_validation_report": "",
                    "replay_optimization_report": "",
                    "strategy_diagnose_report": "",
                    "alpha_mechanism_probe_report": "",
                    "alpha_source_route_report": str(route),
                    "microstructure_alpha_lifecycle_report": "",
                    "microstructure_demo_binding_report": "",
                    "run_manifest": "",
                    "control_cost_bps": 3.5,
                    "min_live_policy_applied": 1,
                    "min_replay_total_fills": 20,
                },
            )()

            report = AUDIT.build_report(args)

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["selected_alpha_route"], "unresolved")
            self.assertIn("alpha_source_route", report["checks"])

    def test_microstructure_route_uses_its_own_frozen_evidence_chain(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            candidate_id = "a" * 64
            development = write_json(
                root / "development.json",
                {
                    "schema_version": "microstructure_alpha_development_v2",
                    "status": "PASS",
                    "fully_verifiable": True,
                    "research_domain": "forward_development_only",
                    "promotion_evidence": False,
                    "promotion_eligible": False,
                    "economic_screen": {"development_passed": True},
                    "capture_merge_contract": {
                        "method": "drop_shared_adjacent_boundary_buckets_v1",
                        "boundary_action": "drop_entire_shared_one_second_bucket",
                        "non_boundary_action": "fail_closed",
                    },
                    "data": {
                        "capture_merge_audit": {
                            "method": "drop_shared_adjacent_boundary_buckets_v1",
                            "input_segment_count": 1,
                            "manifest_feature_row_count": 100,
                            "output_feature_row_count": 100,
                            "shared_adjacent_boundary_bucket_count": 0,
                            "conflicting_shared_boundary_bucket_count": 0,
                            "identical_shared_boundary_bucket_count": 0,
                            "dropped_boundary_bucket_count": 0,
                            "dropped_boundary_timestamps_sha256": "b" * 64,
                        }
                    },
                    "negative_control": {
                        "method": "deterministic_oos_prediction_time_permutation",
                        "fully_verifiable": True,
                        "passed": True,
                        "trial_count": 7,
                    },
                    "target_contract": {
                        "objective": "joint_direction_and_exit_horizon_executable_net_return",
                        "overlapping_episodes_forbidden": True,
                    },
                    "model_contract": {
                        "training_target": "fit_only_standardized_stress_profitability_indicator",
                        "target_normalization": "per_action_zero_mean_unit_variance_on_fit_domain_only",
                        "inference_score": "fit_class_conditional_expected_base_net_return_bps",
                        "economic_acceptance_target": "untransformed_executable_base_and_stress_net_return",
                        "validation_or_test_target_statistics_used_for_fit": False,
                    },
                    "validation_contract": {
                        "method": "rolling_purged_nested_validation",
                        "score_threshold_floor_bps": None,
                        "negative_model_score_threshold_permitted": True,
                        "threshold_viability_contract": "realized_base_and_stress_net_lcb_positive_in_nested_validation",
                        "oos_windows_non_overlapping": True,
                    },
                },
            )
            def future(path: pathlib.Path, domain: str, episodes: int):
                return write_json(
                    path,
                    {
                        "schema_version": "microstructure_alpha_future_domain_v1",
                        "status": "PASS",
                        "fully_verifiable": True,
                        "candidate_id": candidate_id,
                        "research_domain": domain,
                        "policy_frozen": True,
                        "threshold_tuning_permitted": False,
                        "episode_count": episodes,
                    },
                )
            selection = future(root / "selection.json", "independent_forward_selection", 24)
            holdout = future(root / "holdout.json", "untouched_final_holdout", 25)
            raw_replay = write_json(
                root / "raw_replay.json",
                {
                    "schema_version": "microstructure_alpha_raw_replay_v1",
                    "status": "PASS",
                    "fully_verifiable": True,
                    "candidate_id": candidate_id,
                    "research_domain": "untouched_final_holdout_replay",
                    "raw_to_feature_parity": True,
                    "fixed_model_prediction_economics_deterministic": True,
                    "economic_replay": {"episode_count": 25},
                    "demo_entry_eligible": True,
                    "live_promotion_eligible": False,
                },
            )
            state = {
                "candidate_id": candidate_id,
                "phase": "demo_ready",
                "demo_entry_eligible": True,
                "live_promotion_eligible": False,
                "artifacts": {"development_report": artifact(development)},
                "evidence": {
                    "selection_passed": artifact(selection),
                    "final_holdout_passed": artifact(holdout),
                    "raw_replay_passed": artifact(raw_replay),
                },
            }
            lifecycle = write_json(
                root / "lifecycle.json",
                {
                    "schema_version": "microstructure_alpha_lifecycle_v1",
                    "status": "PASS",
                    "fully_verifiable": True,
                    "candidate_id": candidate_id,
                    "phase": "demo_ready",
                    "state": state,
                    "promotion_eligible": False,
                    "demo_entry_eligible": True,
                    "live_promotion_eligible": False,
                },
            )
            route = write_json(
                root / "route.json",
                {
                    "schema_version": "alpha_source_route_v1",
                    "status": "PASS",
                    "selected_route": "microstructure_demo",
                    "selection_policy": {
                        "method": "fixed_predeclared_precedence",
                        "cross_source_return_comparison_permitted": False,
                        "nonselected_source_failure_blocks_selected_route": False,
                    },
                    "sources": {
                        "microstructure_demo": {
                            "readiness": "READY",
                            "candidate_id": candidate_id,
                        }
                    },
                    "demo_only": True,
                    "live_promotion_eligible": False,
                },
            )
            binding = write_json(
                root / "binding.json",
                {
                    "schema_version": "microstructure_demo_binding_v1",
                    "status": "PASS",
                    "selected_route": "microstructure_demo",
                    "candidate_id": candidate_id,
                    "signal_status": "FLAT",
                    "health_age_ms": 100,
                    "signal_age_ms": 100,
                    "demo_entry_eligible": True,
                    "live_promotion_eligible": False,
                },
            )
            runtime = write_json(
                root / "runtime.json",
                {
                    "metrics": {
                        "integrator_mode_canary_count": 10,
                        "microstructure_demo_signal_accepted_count": 1,
                        "microstructure_demo_accepted_candidate_ids": [candidate_id],
                        "integrator_policy_applied_count": 0,
                        "integrator_policy_complete_episode_count": 0,
                        "integrator_policy_unique_filled_order_count": 0,
                        "integrator_policy_proposed_candidate_ids": [],
                        "integrator_policy_filled_candidate_ids": [],
                    }
                },
            )
            args = type(
                "Args",
                (),
                {
                    "integrator_report": "",
                    "registry_report": "",
                    "runtime_assess_report": str(runtime),
                    "replay_validation_report": "",
                    "replay_optimization_report": "",
                    "strategy_diagnose_report": "",
                    "alpha_mechanism_probe_report": "",
                    "alpha_source_route_report": str(route),
                    "microstructure_alpha_lifecycle_report": str(lifecycle),
                    "microstructure_demo_binding_report": str(binding),
                    "run_manifest": "",
                    "control_cost_bps": 3.5,
                    "min_live_policy_applied": 1,
                    "min_replay_total_fills": 20,
                },
            )()

            report = AUDIT.build_report(args)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["selected_alpha_route"], "microstructure_demo")
            self.assertEqual(report["checks"]["target_consistency"]["status"], "pass")
            self.assertEqual(
                report["checks"]["alpha_mechanism_probe"]["status"],
                "not_applicable",
            )

    def test_microstructure_runtime_rejects_wrong_accepted_candidate(self):
        candidate_id = "a" * 64
        report = AUDIT.audit_microstructure_model_influence(
            {
                "metrics": {
                    "integrator_mode_canary_count": 1,
                    "microstructure_demo_signal_accepted_count": 1,
                    "microstructure_demo_accepted_candidate_ids": ["b" * 64],
                }
            },
            candidate_id,
            1,
        )

        self.assertEqual(report["status"], "fail")
        self.assertTrue(
            any("outside the selected route" in reason for reason in report["fail_reasons"])
        )

    def test_report_only_preserves_failed_verdict_without_failing_process(self):
        with tempfile.TemporaryDirectory() as td:
            output = pathlib.Path(td) / "mechanism_report.json"
            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "closed_loop_mechanism_audit.py",
                    "--output",
                    str(output),
                    "--report-only",
                ]
                code = AUDIT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["conclusion"], "MECHANISM_NOT_PROVEN")

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
