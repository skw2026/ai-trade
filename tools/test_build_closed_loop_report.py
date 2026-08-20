#!/usr/bin/env python3

import importlib.util
import hashlib
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
    def test_data_pipeline_keeps_research_benchmark_failure_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as td:
            report = pathlib.Path(td) / "data_pipeline_report.json"
            report.write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "steps": [
                            {
                                "name": "feature_build",
                                "enabled": True,
                                "required": True,
                                "status": "pass",
                            },
                            {
                                "name": "walkforward_backtest",
                                "enabled": True,
                                "required": False,
                                "status": "fail",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_data_pipeline(report)

            self.assertEqual(section["status"], "pass")
            self.assertEqual(section["failed_required_steps"], [])
            self.assertEqual(
                section["failed_diagnostic_steps"], ["walkforward_backtest"]
            )
            self.assertIn("研究基准诊断未通过", section["warn_reasons"][0])

    def test_fail_closed_short_circuit_is_not_artifact_contract_corruption(self):
        sections = {
            "run_manifest": {
                "status": "fail",
                "fail_reasons": [
                    "closed-loop step failed: market_alpha_development",
                    "closed-loop required step skipped: integrator",
                    "step status ledger missing required steps: replay_validation",
                ],
                "warn_reasons": [],
            },
            "market_alpha_development": {
                "status": "fail",
                "fail_reasons": ["no positive-cost candidate"],
            },
        }

        payload = REPORT.build_convergence_layers(sections)

        artifact = payload["layers"][0]
        self.assertEqual(artifact["name"], "artifact_contract")
        self.assertEqual(artifact["status"], "PASS_WITH_ACTIONS")
        self.assertEqual(payload["first_blocking_layer"], "mechanism_proof")

    def test_fail_closed_short_circuit_does_not_hide_artifact_loss(self):
        section = {
            "status": "fail",
            "fail_reasons": [
                "run manifest missing required full artifacts: baseline_report",
                "closed-loop step failed: alpha_source_route",
                "closed-loop required step skipped: runtime_assess",
            ],
            "warn_reasons": [],
        }

        contract_view = REPORT.manifest_contract_view(section)

        self.assertEqual(contract_view["status"], "fail")
        self.assertEqual(
            contract_view["fail_reasons"],
            ["run manifest missing required full artifacts: baseline_report"],
        )

    def test_market_alpha_development_requires_positive_real_cost_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "market_alpha.json"
            payload = {
                "schema_version": "market_alpha_development_verification_v1",
                "fully_verifiable": True,
                "research_domain": "development_only",
                "promotion_evidence": False,
                "promotion_eligible": False,
                "economic_screen": {"development_passed": False},
                "next_gate": "remain_in_development_and_reject_candidate",
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            failed = REPORT.assess_market_alpha_development(path)
            self.assertEqual(failed["status"], "fail")
            self.assertIn("no cross-market", failed["fail_reasons"][-1])

            payload["economic_screen"]["development_passed"] = True
            payload["next_gate"] = "independent_selection_required"
            path.write_text(json.dumps(payload), encoding="utf-8")
            passed = REPORT.assess_market_alpha_development(path)
            self.assertEqual(passed["status"], "pass")
            self.assertEqual(passed["readiness_status"], "PASS")

    def test_cross_venue_information_set_decision_is_visible_but_non_promotional(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "cross_venue.json"
            payload = {
                "schema_version": "cross_venue_information_set_experiment_v1",
                "status": "COMPLETE",
                "fully_verifiable": True,
                "research_domain": "forward_development_only",
                "promotion_evidence": False,
                "promotion_eligible": False,
                "promotion_authority": False,
                "demo_activation_authorized": False,
                "live_activation_authorized": False,
                "research_decision": "STOP_INFORMATION_SOURCE",
                "reason_codes": [
                    "paired_treatment_minus_control_lcb_not_positive"
                ],
                "common_domain": {"row_count": 123456},
                "hindsight_oracle": {
                    "opportunity_proven": True,
                    "stress_cost_by_split": {"lcb_bps": 1.25},
                },
                "arms": {
                    "treatment": {
                        "aggregate": {
                            "architectures": {
                                "direct_stress_utility_regression": {
                                    "trade_count": 41,
                                    "oos_stress_cost_by_split": {
                                        "lcb_bps": -0.75
                                    },
                                }
                            }
                        }
                    }
                },
                "paired_treatment_minus_control": {
                    "stress_cost_delta_by_split": {"lcb_bps": -0.5},
                    "permutation_null": {"passed": False},
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            section = REPORT.assess_cross_venue_information_set_experiment(path)

            self.assertEqual(section["status"], "pass")
            self.assertEqual(section["readiness_status"], "PASS_WITH_ACTIONS")
            self.assertEqual(section["research_decision"], "STOP_INFORMATION_SOURCE")
            self.assertEqual(section["metrics"]["common_row_count"], 123456)
            self.assertEqual(section["metrics"]["oracle_stress_lcb_bps"], 1.25)
            self.assertEqual(section["metrics"]["treatment_stress_lcb_bps"], -0.75)
            self.assertEqual(section["metrics"]["paired_delta_stress_lcb_bps"], -0.5)
            self.assertFalse(section["authoritative_for_integrator_promotion"])
            self.assertFalse(section["promotion_authority"])
            self.assertFalse(section["demo_activation_authorized"])
            self.assertFalse(section["live_activation_authorized"])

            payload["promotion_authority"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            rejected = REPORT.assess_cross_venue_information_set_experiment(path)
            self.assertEqual(rejected["status"], "fail")
            self.assertIn("authority contract", rejected["fail_reasons"][0])

    def test_microstructure_alpha_requires_stressed_cost_development_pass(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "microstructure_alpha.json"
            payload = {
                "schema_version": "microstructure_alpha_development_v8",
                "status": "PASS",
                "fully_verifiable": True,
                "research_domain": "forward_development_only",
                "promotion_evidence": False,
                "promotion_eligible": False,
                "cross_asset_feature_contract": {
                    "method": "exact_exchange_second_inner_join_v1",
                    "target_symbol": "SOLUSDT",
                    "context_symbols": ["BTCUSDT", "ETHUSDT"],
                    "future_fill_permitted": False,
                    "backfill_permitted": False,
                },
                "economic_screen": {
                    "development_passed": False,
                    "oos_stress_cost_by_split": {"lcb_bps": -1.0},
                },
                "negative_control": {
                    "method": "deterministic_oos_prediction_time_permutation",
                    "fully_verifiable": True,
                    "passed": False,
                    "trial_count": 7,
                },
                "target_architecture_comparison": {
                    "schema_version": "microstructure_target_architecture_comparison_v1",
                    "fully_verifiable": True,
                    "promotion_evidence": False,
                    "promotion_eligible": False,
                    "influences_development_passed": False,
                    "diagnostic_leader_id": "joint_action_ranker",
                    "conclusion": "TARGET_ARCHITECTURE_SIGNAL_DIAGNOSTICALLY_PROVEN",
                },
                "next_gate": "reject_microstructure_candidate_and_remain_in_development",
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            failed = REPORT.assess_microstructure_alpha_development(path)
            self.assertEqual(failed["status"], "fail")
            self.assertTrue(
                any("joint direction/exit" in reason for reason in failed["fail_reasons"])
            )
            self.assertEqual(
                failed["target_architecture_comparison"]["diagnostic_leader_id"],
                "joint_action_ranker",
            )
            self.assertFalse(failed["promotion_eligible"])

            payload["status"] = "NOT_READY"
            payload["fully_verifiable"] = False
            path.write_text(json.dumps(payload), encoding="utf-8")
            not_ready = REPORT.assess_microstructure_alpha_development(path)
            self.assertEqual(not_ready["status"], "fail")
            self.assertEqual(not_ready["readiness_status"], "NOT_READY")

            payload["status"] = "PASS"
            payload["fully_verifiable"] = True
            payload["economic_screen"]["development_passed"] = True
            payload["negative_control"]["passed"] = True
            payload["next_gate"] = (
                "freeze_candidate_and_collect_independent_forward_selection"
            )
            model_path = pathlib.Path(td) / "microstructure.cbm"
            model_path.write_bytes(b"frozen-development-model")
            payload["frozen_candidate"] = {
                "model_path": str(model_path),
                "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            passed = REPORT.assess_microstructure_alpha_development(path)
            self.assertEqual(passed["status"], "pass")
            self.assertFalse(passed["promotion_eligible"])

    def test_microstructure_lifecycle_requires_bound_selection_holdout_and_raw_replay(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            candidate_id = "a" * 64
            evidence = {}
            contracts = {
                "selection_passed": (
                    "microstructure_alpha_future_domain_v1",
                    "independent_forward_selection",
                ),
                "final_holdout_passed": (
                    "microstructure_alpha_future_domain_v1",
                    "untouched_final_holdout",
                ),
                "raw_replay_passed": (
                    "microstructure_alpha_raw_replay_v1",
                    "untouched_final_holdout_replay",
                ),
            }
            for name, (schema, domain) in contracts.items():
                path = root / f"{name}.json"
                item = {
                    "schema_version": schema,
                    "status": "PASS",
                    "candidate_id": candidate_id,
                    "research_domain": domain,
                }
                if name == "raw_replay_passed":
                    item.update(
                        {
                            "raw_to_feature_parity": True,
                            "fixed_model_prediction_economics_deterministic": True,
                            "live_promotion_eligible": False,
                        }
                    )
                path.write_text(json.dumps(item), encoding="utf-8")
                evidence[name] = {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            lifecycle_path = root / "lifecycle.json"
            payload = {
                "schema_version": "microstructure_alpha_lifecycle_v1",
                "status": "PASS",
                "fully_verifiable": True,
                "candidate_id": candidate_id,
                "phase": "demo_ready",
                "state": {
                    "candidate_id": candidate_id,
                    "phase": "demo_ready",
                    "evidence": evidence,
                },
                "demo_entry_eligible": True,
                "live_promotion_eligible": False,
                "promotion_eligible": False,
                "next_gate": "demo_incubation_only",
            }
            lifecycle_path.write_text(json.dumps(payload), encoding="utf-8")
            passed = REPORT.assess_microstructure_alpha_lifecycle(lifecycle_path)
            self.assertEqual(passed["status"], "pass")
            self.assertTrue(passed["demo_entry_eligible"])

            replay_path = pathlib.Path(evidence["raw_replay_passed"]["path"])
            replay_payload = json.loads(replay_path.read_text())
            replay_payload["raw_to_feature_parity"] = False
            replay_path.write_text(json.dumps(replay_payload), encoding="utf-8")
            failed = REPORT.assess_microstructure_alpha_lifecycle(lifecycle_path)
            self.assertEqual(failed["status"], "fail")
            self.assertTrue(
                any("identity mismatch" in reason for reason in failed["fail_reasons"])
            )

    def test_unregistered_microstructure_lifecycle_is_not_ready_without_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            lifecycle_path = pathlib.Path(td) / "lifecycle.json"
            lifecycle_path.write_text(
                json.dumps(
                    {
                        "schema_version": "microstructure_alpha_lifecycle_v1",
                        "status": "NOT_READY",
                        "fully_verifiable": False,
                        "candidate_id": None,
                        "phase": "unregistered",
                        "state": None,
                        "not_ready_reason": "minimum_forward_capture_duration",
                        "demo_entry_eligible": False,
                        "live_promotion_eligible": False,
                        "promotion_eligible": False,
                    }
                ),
                encoding="utf-8",
            )
            result = REPORT.assess_microstructure_alpha_lifecycle(lifecycle_path)
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["readiness_status"], "NOT_READY")
            self.assertFalse(
                any("identity mismatch" in reason for reason in result["fail_reasons"])
            )

    def test_report_only_preserves_failed_strategy_status_without_failing_process(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            mechanism_report = root / "closed_loop_mechanism_report.json"
            mechanism_report.write_text(
                json.dumps(
                    {
                        "status": "fail",
                        "readiness_status": "FAIL",
                        "conclusion": "MECHANISM_NOT_PROVEN",
                        "fail_reasons": ["mechanism evidence is incomplete"],
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
                    "--closed_loop_mechanism_report",
                    str(mechanism_report),
                    "--report-only",
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["overall_status"], "FAIL")
            self.assertEqual(payload["closed_loop_mechanism_status"], "FAIL")

    def test_mechanism_report_becomes_first_blocking_layer(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            runtime_assess = root / "runtime_assess.json"
            mechanism_report = root / "closed_loop_mechanism_report.json"

            runtime_assess.write_text(
                json.dumps(
                    {
                        "stage": "S5",
                        "verdict": "PASS",
                        "metrics": {"runtime_status_count": 88},
                        "account_pnl": {"samples": 88},
                    }
                ),
                encoding="utf-8",
            )
            mechanism_report.write_text(
                json.dumps(
                    {
                        "schema_version": "closed_loop_mechanism_audit_v1",
                        "status": "fail",
                        "readiness_status": "FAIL",
                        "conclusion": "MECHANISM_NOT_PROVEN",
                        "fail_reasons": [
                            "target_consistency: integrator governance still uses AUC"
                        ],
                        "warn_reasons": [],
                        "checks": {
                            "target_consistency": {
                                "status": "fail",
                                "fail_reasons": [
                                    "integrator governance still uses AUC"
                                ],
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
                    "--closed_loop_mechanism_report",
                    str(mechanism_report),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["closed_loop_mechanism_status"], "FAIL")
            self.assertEqual(
                payload["next_action_plan"]["first_blocking_layer"],
                "mechanism_proof",
            )
            self.assertIn("closed_loop_mechanism", payload["sections"])

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

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["sections"]["walkforward"]["status"], "fail")
            self.assertEqual(payload["overall_status"], "PASS_WITH_ACTIONS")
            self.assertTrue(
                any("walk-forward 平均 Sharpe 未达门槛" in x for x in payload["warn_reasons"])
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

    def test_strategy_raw_edge_can_be_suppressed_by_deployable_optimizer_candidate(self):
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
                        "activation_gate": {
                            "basis": "execution_optimizer.best_deployable_candidate",
                            "status": "pass_with_actions",
                            "fail_reasons": [],
                            "selected_candidate": {
                                "name": "strong_liquid_q50",
                                "status": "pass",
                                "diagnostic_only": False,
                                "deployable_config": {
                                    "requires_rerun": False,
                                },
                                "aggregate_summary": {
                                    "total_fills": 34,
                                    "median_realized_net_per_fill_with_fills": 0.003,
                                    "positive_filled_segment_ratio": 0.61,
                                },
                            },
                        },
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
                        "status": "fail",
                        "readiness_status": "FAIL",
                        "fail_reasons": [
                            "confirmed_trend mean_net_forward_bps -12.0 <= min_mean_net_edge_bps=0.0",
                            "confirmed_trend positive_net_ratio 0.35 < 0.500000",
                        ],
                        "aggregate": {
                            "confirmed_trend": {
                                "sample_count": 100,
                                "mean_net_forward_bps": -12.0,
                                "positive_net_ratio": 0.35,
                            }
                        },
                        "diagnostics": [
                            {"code": "confirmed_trend_raw_edge_non_positive"},
                            {"code": "confirmed_trend_positive_ratio_low"},
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

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            strategy = payload["sections"]["strategy_diagnose"]
            self.assertEqual(strategy["status"], "pass")
            self.assertEqual(strategy["readiness_status"], "PASS_WITH_ACTIONS")
            self.assertEqual(strategy["suppression_basis"], "execution_optimizer.best_deployable_candidate")
            self.assertEqual(strategy["fail_reasons"], [])
            self.assertTrue(
                any(
                    "strategy_raw_edge_suppressed_by_optimizer_candidate" in item
                    for item in strategy["warn_reasons"]
                )
            )
            self.assertNotIn(
                "NOT_CONVERGED_STRATEGY_RAW_EDGE_FAIL",
                payload["sections"]["trading_convergence"]["blockers"],
            )
            self.assertNotIn(
                "NOT_CONVERGED_STRATEGY_RAW_EDGE_NOT_VERIFIED",
                payload["sections"]["trading_convergence"]["blockers"],
            )

    def test_unrerun_optimizer_candidate_cannot_suppress_strategy_failure(self):
        replay = {
            "activation_gate": {
                "basis": "execution_optimizer.best_deployable_candidate",
                "selected_candidate": {
                    "status": "pass",
                    "diagnostic_only": False,
                    "deployable_config": {"requires_rerun": True},
                },
            }
        }

        self.assertFalse(
            REPORT.replay_activation_uses_deployable_optimizer_candidate(replay)
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

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["sections"]["walkforward"]["status"], "fail")
            self.assertEqual(payload["overall_status"], "PASS_WITH_ACTIONS")
            self.assertTrue(
                any("walk-forward 交易活跃 split 数未达门槛" in x for x in payload["warn_reasons"])
            )
            self.assertTrue(
                any("walk-forward 总交易次数未达门槛" in x for x in payload["warn_reasons"])
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

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["sections"]["walkforward"]["status"], "fail")
            self.assertEqual(payload["overall_status"], "PASS_WITH_ACTIONS")
            self.assertTrue(
                any("walk-forward 平均 split 收益未达门槛" in x for x in payload["warn_reasons"])
            )
            self.assertTrue(
                any("walk-forward 启用 split 平均收益未达门槛" in x for x in payload["warn_reasons"])
            )
            self.assertTrue(
                any("walk-forward 交易 split 平均收益未达门槛" in x for x in payload["warn_reasons"])
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

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["sections"]["walkforward"]["status"], "fail")
            self.assertTrue(
                any("walk-forward TREND 桶交易次数未达门槛" in x for x in payload["warn_reasons"])
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

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["trend_readiness_status"], "FAIL")
            self.assertEqual(payload["sections"]["trend_validation"]["status"], "fail")
            self.assertTrue(
                any(
                    "trend-validation TREND 桶 Sharpe 未达门槛" in x
                    for x in payload["warn_reasons"]
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

    def test_microstructure_convergence_uses_candidate_attributed_demo_episodes(self):
        candidate_id = "a" * 64
        runtime = {
            "metrics": {
                "integrator_policy_proposed_candidate_ids": [candidate_id] * 30,
                "integrator_policy_filled_candidate_ids": [candidate_id] * 30,
                "integrator_policy_closed_episode_events": [
                    {
                        "candidate_id": candidate_id,
                        "evidence_complete": True,
                        "realized_net_usd": 0.1,
                    }
                    for _ in range(30)
                ],
            }
        }
        lifecycle = {
            "status": "pass",
            "readiness_status": "PASS",
            "candidate_id": candidate_id,
            "evidence": {
                "raw_replay_passed": {
                    "episode_count": 25,
                    "raw_to_feature_parity": True,
                    "fixed_model_prediction_economics_deterministic": True,
                }
            },
        }
        binding = {"status": "pass", "readiness_status": "PASS"}

        section = REPORT.assess_microstructure_trading_convergence(
            runtime, lifecycle, binding
        )

        self.assertEqual(
            section["readiness_status"],
            "CONVERGED_MICROSTRUCTURE_DEMO_PROFITABLE",
        )
        self.assertEqual(section["blockers"], [])
        self.assertAlmostEqual(section["metrics"]["realized_net_usd"], 3.0)

    def test_microstructure_convergence_uses_wal_summary_for_current_runtime_identity(self):
        candidate_id = "a" * 64
        runtime_config_sha = "b" * 64
        trade_bot_sha = "c" * 64
        runtime = {
            "metrics": {
                "process_runtime_config_sha256_latest": runtime_config_sha,
                "process_trade_bot_sha256_latest": trade_bot_sha,
                "integrator_candidate_episode_summaries": [
                    {
                        "candidate_id": candidate_id,
                        "model_version": candidate_id,
                        "runtime_config_sha256": runtime_config_sha,
                        "trade_bot_sha256": trade_bot_sha,
                        "total_episode_count": 30,
                        "complete_episode_count": 30,
                        "positive_episode_count": 18,
                        "realized_net_usd": 1.5,
                        "realized_net_usd_sum_squares": 0.1875,
                    },
                    {
                        "candidate_id": candidate_id,
                        "model_version": candidate_id,
                        "runtime_config_sha256": "d" * 64,
                        "trade_bot_sha256": "e" * 64,
                        "total_episode_count": 100,
                        "complete_episode_count": 100,
                        "positive_episode_count": 100,
                        "realized_net_usd": 100.0,
                        "realized_net_usd_sum_squares": 100.0,
                    },
                ],
                "integrator_policy_closed_episode_events": [],
                "integrator_policy_proposed_candidate_ids": [],
                "integrator_policy_filled_candidate_ids": [],
            }
        }
        lifecycle = {
            "status": "pass",
            "readiness_status": "PASS",
            "candidate_id": candidate_id,
            "evidence": {
                "raw_replay_passed": {
                    "episode_count": 25,
                    "raw_to_feature_parity": True,
                    "fixed_model_prediction_economics_deterministic": True,
                }
            },
        }

        section = REPORT.assess_microstructure_trading_convergence(
            runtime,
            lifecycle,
            {"status": "pass", "readiness_status": "PASS"},
        )

        self.assertEqual(
            section["readiness_status"],
            "CONVERGED_MICROSTRUCTURE_DEMO_PROFITABLE",
        )
        self.assertEqual(
            section["metrics"]["episode_evidence_source"],
            "wal_candidate_runtime_identity_summary",
        )
        self.assertEqual(section["metrics"]["realized_net_usd"], 1.5)

    def test_microstructure_positive_total_with_unstable_returns_does_not_converge(self):
        candidate_id = "a" * 64
        episode_values = [0.2] * 18 + [-0.25] * 12
        runtime = {
            "metrics": {
                "integrator_policy_closed_episode_events": [
                    {
                        "candidate_id": candidate_id,
                        "evidence_complete": True,
                        "realized_net_usd": value,
                    }
                    for value in episode_values
                ],
                "integrator_policy_proposed_candidate_ids": [candidate_id],
                "integrator_policy_filled_candidate_ids": [candidate_id],
            }
        }
        lifecycle = {
            "status": "pass",
            "readiness_status": "PASS",
            "candidate_id": candidate_id,
            "evidence": {
                "raw_replay_passed": {
                    "episode_count": 25,
                    "raw_to_feature_parity": True,
                    "fixed_model_prediction_economics_deterministic": True,
                }
            },
        }

        section = REPORT.assess_microstructure_trading_convergence(
            runtime,
            lifecycle,
            {"status": "pass", "readiness_status": "PASS"},
        )

        self.assertGreater(section["metrics"]["realized_net_usd"], 0.0)
        self.assertGreaterEqual(section["metrics"]["positive_episode_ratio"], 0.55)
        self.assertLess(section["metrics"]["realized_net_95pct_lcb_usd"], 0.0)
        self.assertIn(
            "NOT_CONVERGED_MICROSTRUCTURE_DEMO_NET_LCB_NOT_POSITIVE",
            section["blockers"],
        )

    def test_microstructure_wal_summary_requires_current_process_identity(self):
        candidate_id = "a" * 64
        runtime = {
            "metrics": {
                "integrator_candidate_episode_summaries": [
                    {
                        "candidate_id": candidate_id,
                        "model_version": candidate_id,
                        "runtime_config_sha256": "b" * 64,
                        "trade_bot_sha256": "c" * 64,
                        "total_episode_count": 30,
                        "complete_episode_count": 30,
                        "positive_episode_count": 18,
                        "realized_net_usd": 1.5,
                        "realized_net_usd_sum_squares": 0.1875,
                    }
                ],
                "integrator_policy_closed_episode_events": [],
                "integrator_policy_proposed_candidate_ids": [],
                "integrator_policy_filled_candidate_ids": [],
            }
        }
        lifecycle = {
            "status": "pass",
            "readiness_status": "PASS",
            "candidate_id": candidate_id,
            "evidence": {
                "raw_replay_passed": {
                    "episode_count": 25,
                    "raw_to_feature_parity": True,
                    "fixed_model_prediction_economics_deterministic": True,
                }
            },
        }

        section = REPORT.assess_microstructure_trading_convergence(
            runtime,
            lifecycle,
            {"status": "pass", "readiness_status": "PASS"},
        )

        self.assertIn(
            "NOT_CONVERGED_MICROSTRUCTURE_RUNTIME_IDENTITY_MISSING",
            section["blockers"],
        )
        self.assertNotEqual(
            section["readiness_status"],
            "CONVERGED_MICROSTRUCTURE_DEMO_PROFITABLE",
        )

    def test_microstructure_wal_summary_rejects_candidate_model_mismatch(self):
        candidate_id = "a" * 64
        runtime_config_sha = "b" * 64
        trade_bot_sha = "c" * 64
        runtime = {
            "metrics": {
                "process_runtime_config_sha256_latest": runtime_config_sha,
                "process_trade_bot_sha256_latest": trade_bot_sha,
                "integrator_candidate_episode_summaries": [
                    {
                        "candidate_id": candidate_id,
                        "model_version": "wrong-model-version",
                        "runtime_config_sha256": runtime_config_sha,
                        "trade_bot_sha256": trade_bot_sha,
                        "total_episode_count": 30,
                        "complete_episode_count": 30,
                        "positive_episode_count": 30,
                        "realized_net_usd": 3.0,
                        "realized_net_usd_sum_squares": 0.3,
                    }
                ],
                "integrator_policy_closed_episode_events": [],
                "integrator_policy_proposed_candidate_ids": [candidate_id],
                "integrator_policy_filled_candidate_ids": [candidate_id],
            }
        }
        lifecycle = {
            "status": "pass",
            "readiness_status": "PASS",
            "candidate_id": candidate_id,
            "evidence": {
                "raw_replay_passed": {
                    "episode_count": 25,
                    "raw_to_feature_parity": True,
                    "fixed_model_prediction_economics_deterministic": True,
                }
            },
        }

        section = REPORT.assess_microstructure_trading_convergence(
            runtime,
            lifecycle,
            {"status": "pass", "readiness_status": "PASS"},
        )

        self.assertIn(
            "NOT_CONVERGED_MICROSTRUCTURE_CANDIDATE_ATTRIBUTION_MISMATCH",
            section["blockers"],
        )
        self.assertNotEqual(
            section["readiness_status"],
            "CONVERGED_MICROSTRUCTURE_DEMO_PROFITABLE",
        )

    def test_microstructure_layers_ignore_failed_nonselected_legacy_route(self):
        sections = {
            "alpha_source_route": {
                "status": "pass",
                "readiness_status": "PASS",
                "selected_route": "microstructure_demo",
            },
            "microstructure_alpha_development": {
                "status": "pass",
                "readiness_status": "PASS",
            },
            "microstructure_alpha_lifecycle": {
                "status": "pass",
                "readiness_status": "PASS",
            },
            "microstructure_demo_binding": {
                "status": "pass",
                "readiness_status": "PASS",
            },
            "closed_loop_mechanism": {
                "status": "pass",
                "readiness_status": "PASS",
            },
            "market_alpha_development": {
                "status": "fail",
                "readiness_status": "FAIL",
                "fail_reasons": ["legacy alpha negative"],
                "authoritative_for_integrator_promotion": False,
            },
            "integrator": {
                "status": "fail",
                "readiness_status": "FAIL",
                "fail_reasons": ["legacy model missing"],
                "authoritative_for_integrator_promotion": False,
            },
        }

        layers = REPORT.build_convergence_layers(sections)

        mechanism = next(
            item for item in layers["layers"] if item["name"] == "mechanism_proof"
        )
        self.assertEqual(mechanism["status"], "PASS")
        self.assertNotIn("market_alpha_development", mechanism["section_statuses"])

    def test_run_manifest_applies_selected_microstructure_route_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            contract_path = (
                pathlib.Path(__file__).resolve().parents[1]
                / "config"
                / "closed_loop_contract.json"
            )
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            action_contract = contract["actions"]["train"]
            route_contract = action_contract["route_contracts"][
                "microstructure_demo"
            ]
            route_path = root / "alpha_source_route_report"
            route_path.write_text(
                json.dumps(
                    {
                        "schema_version": "alpha_source_route_v1",
                        "status": "PASS",
                        "selected_route": "microstructure_demo",
                    }
                ),
                encoding="utf-8",
            )
            effective_steps = list(action_contract["required_steps"])
            insertion = effective_steps.index("alpha_source_route") + 1
            effective_steps[insertion:insertion] = route_contract["required_steps"]
            step_records = []
            for step in effective_steps:
                observation_failure = step == "market_alpha_development"
                step_records.append(
                    {
                        "run_id": "run-micro",
                        "action": "train",
                        "step": step,
                        "kind": "observation" if observation_failure else "required",
                        "result": "fail" if observation_failure else "pass",
                        "exit_code": 2 if observation_failure else 0,
                        "blocked_by_prior_failure": False,
                    }
                )
            step_status = root / "step_status"
            step_status.write_text(
                "\n".join(json.dumps(item) for item in step_records) + "\n",
                encoding="utf-8",
            )
            required_artifacts = [
                *action_contract["required_artifacts"],
                *route_contract["required_artifacts"],
            ]
            artifacts = {}
            for name in required_artifacts:
                if name == "step_status":
                    artifact_path = step_status
                elif name == "alpha_source_route_report":
                    artifact_path = route_path
                else:
                    artifact_path = root / name
                    artifact_path.write_text(name, encoding="utf-8")
                artifacts[name] = {
                    "path": str(artifact_path),
                    "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                }
            manifest = root / "run_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "run_id": "run-micro",
                        "action": "train",
                        "git": {"commit": "abc"},
                        "config_hashes": {},
                        "replay_validation": {},
                        "runtime": {
                            "image_id": "sha256:image",
                            "image_revision": "abc",
                        },
                        "artifact_contract": {
                            "schema_version": contract["schema_version"],
                            "contract_sha256": hashlib.sha256(
                                contract_path.read_bytes()
                            ).hexdigest(),
                            "action": "train",
                            "required_artifacts": action_contract[
                                "required_artifacts"
                            ],
                            "required_steps": action_contract["required_steps"],
                            "route_contracts": action_contract["route_contracts"],
                            "route_rejection_contract": action_contract[
                                "route_rejection_contract"
                            ],
                        },
                        "artifacts": artifacts,
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_run_manifest(manifest, "run-micro")

        self.assertEqual(section["status"], "pass", section["fail_reasons"])
        self.assertEqual(section["selected_alpha_route"], "microstructure_demo")
        self.assertIn("microstructure_demo_binding", section["effective_required_steps"])
        self.assertIn(
            "closed-loop observational step not ready: market_alpha_development",
            section["warn_reasons"],
        )

    def test_run_manifest_accepts_declared_fail_closed_route_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            contract_path = (
                pathlib.Path(__file__).resolve().parents[1]
                / "config"
                / "closed_loop_contract.json"
            )
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            action_contract = contract["actions"]["full"]
            rejection_contract = action_contract["route_rejection_contract"]
            optional_artifacts = set(rejection_contract["optional_artifacts"])
            route_path = root / "alpha_source_route_report.json"
            route_path.write_text(
                json.dumps(
                    {
                        "schema_version": "alpha_source_route_v1",
                        "status": "FAIL",
                        "selected_route": None,
                        "reason": "no_independently_gated_alpha_source_ready",
                    }
                ),
                encoding="utf-8",
            )
            step_records = []
            route_failed = False
            for step in action_contract["required_steps"]:
                if step == "alpha_source_route":
                    route_failed = True
                    step_records.append(
                        {
                            "run_id": "run-rejected",
                            "action": "full",
                            "step": step,
                            "kind": "required",
                            "result": "fail",
                            "exit_code": 2,
                            "blocked_by_prior_failure": False,
                        }
                    )
                elif route_failed:
                    step_records.append(
                        {
                            "run_id": "run-rejected",
                            "action": "full",
                            "step": step,
                            "kind": "required",
                            "result": "skipped",
                            "exit_code": None,
                            "blocked_by_prior_failure": True,
                        }
                    )
                else:
                    step_records.append(
                        {
                            "run_id": "run-rejected",
                            "action": "full",
                            "step": step,
                            "kind": "required",
                            "result": "pass",
                            "exit_code": 0,
                            "blocked_by_prior_failure": False,
                        }
                    )
            step_path = root / "step_status.jsonl"
            step_path.write_text(
                "\n".join(json.dumps(item) for item in step_records) + "\n",
                encoding="utf-8",
            )
            artifacts = {}
            for name in action_contract["required_artifacts"]:
                if name in optional_artifacts:
                    continue
                if name == "step_status":
                    artifact_path = step_path
                elif name == "alpha_source_route_report":
                    artifact_path = route_path
                else:
                    artifact_path = root / name
                    artifact_path.write_text(name, encoding="utf-8")
                artifacts[name] = {
                    "path": str(artifact_path),
                    "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                }
            manifest = root / "run_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "run_id": "run-rejected",
                        "action": "full",
                        "git": {"commit": "abc"},
                        "config_hashes": {},
                        "replay_validation": {},
                        "runtime": {
                            "image_id": "sha256:image",
                            "image_revision": "abc",
                        },
                        "artifact_contract": {
                            "schema_version": contract["schema_version"],
                            "contract_sha256": hashlib.sha256(
                                contract_path.read_bytes()
                            ).hexdigest(),
                            "action": "full",
                            "required_artifacts": action_contract[
                                "required_artifacts"
                            ],
                            "required_steps": action_contract["required_steps"],
                            "route_contracts": action_contract["route_contracts"],
                            "route_rejection_contract": rejection_contract,
                        },
                        "artifacts": artifacts,
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_run_manifest(manifest, "run-rejected")
            contract_view = REPORT.manifest_contract_view(section)

        self.assertEqual(
            section["alpha_route_resolution"], "rejected_fail_closed"
        )
        self.assertNotIn(
            "run manifest alpha source route missing or invalid",
            section["fail_reasons"],
        )
        self.assertFalse(
            any(
                reason.startswith("run manifest missing required full artifacts:")
                for reason in section["fail_reasons"]
            )
        )
        self.assertEqual(contract_view["status"], "pass")
        self.assertTrue(contract_view["execution_short_circuit_detected"])

    def test_run_manifest_validates_step_status_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            contract_path = (
                pathlib.Path(__file__).resolve().parents[1]
                / "config"
                / "closed_loop_contract.json"
            )
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            action_contract = contract["actions"]["assess"]
            step_status = root / "step_status.jsonl"
            steps = [
                "s5_learning_switches",
                "runtime_assess",
                "s5_learning_activity",
                "mechanism_audit",
            ]
            step_status.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "action": "assess",
                        "step": "microstructure_forward_data",
                        "kind": "observation",
                        "result": "fail",
                        "exit_code": 2,
                        "blocked_by_prior_failure": False,
                    }
                )
                + "\n"
                + "\n".join(
                    json.dumps(
                        {
                            "run_id": "run-1",
                            "action": "assess",
                            "step": step,
                            "kind": "diagnostic",
                            "result": "pass",
                            "exit_code": 0,
                            "blocked_by_prior_failure": False,
                        }
                    )
                    for step in steps
                )
                + "\n",
                encoding="utf-8",
            )
            artifacts = {}
            for name in (
                "runtime_log",
                "runtime_assess_report",
                "trade_ledger_report",
                "closed_loop_mechanism_report",
            ):
                path = root / name
                path.write_text(name, encoding="utf-8")
                artifacts[name] = {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            artifacts["step_status"] = {
                "path": str(step_status),
                "sha256": hashlib.sha256(step_status.read_bytes()).hexdigest(),
            }
            manifest = root / "run_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "action": "assess",
                        "git": {"commit": "abc"},
                        "config_hashes": {},
                        "replay_validation": {},
                        "runtime": {
                            "image_id": "sha256:image",
                            "image_revision": "abc",
                        },
                        "artifact_contract": {
                            "schema_version": contract["schema_version"],
                            "contract_sha256": hashlib.sha256(
                                contract_path.read_bytes()
                            ).hexdigest(),
                            "action": "assess",
                            "required_artifacts": action_contract[
                                "required_artifacts"
                            ],
                            "required_steps": action_contract["required_steps"],
                        },
                        "artifacts": artifacts,
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_run_manifest(manifest, "run-1")

        self.assertEqual(section["status"], "pass")
        self.assertIn(
            "closed-loop observational step not ready: microstructure_forward_data",
            section["warn_reasons"],
        )

    def test_run_manifest_rejects_contract_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            contract_path = (
                pathlib.Path(__file__).resolve().parents[1]
                / "config"
                / "closed_loop_contract.json"
            )
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            action_contract = contract["actions"]["assess"]
            step_status = root / "step_status.jsonl"
            step_status.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "run_id": "run-1",
                            "action": "assess",
                            "step": step,
                            "kind": "required",
                            "result": "pass",
                            "exit_code": 0,
                            "blocked_by_prior_failure": False,
                        }
                    )
                    for step in action_contract["required_steps"]
                )
                + "\n",
                encoding="utf-8",
            )
            artifacts = {}
            for name in action_contract["required_artifacts"]:
                artifact_path = (
                    step_status if name == "step_status" else root / name
                )
                if name != "step_status":
                    artifact_path.write_text(name, encoding="utf-8")
                artifacts[name] = {
                    "path": str(artifact_path),
                    "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                }
            manifest = root / "run_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "action": "assess",
                        "git": {"commit": "abc"},
                        "config_hashes": {},
                        "replay_validation": {},
                        "runtime": {
                            "image_id": "sha256:image",
                            "image_revision": "abc",
                        },
                        "artifact_contract": {
                            "schema_version": contract["schema_version"],
                            "contract_sha256": "0" * 64,
                            "action": "assess",
                            "required_artifacts": action_contract[
                                "required_artifacts"
                            ],
                            "required_steps": action_contract["required_steps"],
                        },
                        "artifacts": artifacts,
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_run_manifest(manifest, "run-1")

        self.assertEqual(section["status"], "fail")
        self.assertIn(
            "run manifest artifact contract hash mismatch",
            section["fail_reasons"],
        )

    def test_run_manifest_rejects_empty_step_status_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            step_status = root / "step_status.jsonl"
            step_status.write_text("", encoding="utf-8")
            manifest = root / "run_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "action": "assess",
                        "git": {"commit": "abc"},
                        "config_hashes": {},
                        "replay_validation": {},
                        "runtime": {
                            "image_id": "sha256:image",
                            "image_revision": "abc",
                        },
                        "artifacts": {
                            "step_status": {
                                "path": str(step_status),
                                "sha256": hashlib.sha256(
                                    step_status.read_bytes()
                                ).hexdigest(),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_run_manifest(manifest, "run-1")

        self.assertEqual(section["status"], "fail")
        self.assertIn("step status ledger is empty", section["fail_reasons"])

    def test_strategy_candidate_contract_requires_exact_artifact_identity(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = pathlib.Path(td) / "strategy_candidate_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "strategy_candidate_v1",
                        "candidate_id": "model-v1",
                        "status": "activation_pending_runtime",
                        "candidate": {
                            "model_version": "model-v1",
                            "model_sha256": "abc123",
                            "integrator_report_sha256": "report123",
                            "training_symbol": "SOLUSDT",
                            "bar_interval_ms": 300000,
                            "online_bar_source": "closed_ohlcv",
                            "source_venue": "bybit",
                            "source_category": "linear",
                            "price_type": "trade_price",
                            "volume_unit": "base_asset",
                        },
                        "replay_validation": {
                            "candidate_model_version": "model-v1",
                            "candidate_model_sha256": "abc123",
                            "candidate_integrator_report_sha256": "report123",
                            "independent_identity_match": True,
                            "source_symbol": "SOLUSDT",
                            "feature_contract_match": True,
                            "config_binds_candidate": True,
                            "report_config_identity_match": True,
                            "evaluates_current_candidate": True,
                        },
                        "registry": {
                            "model_version": "model-v1",
                            "model_sha256": "abc123",
                            "integrator_report_sha256": "report123",
                            "candidate_identity_match": True,
                            "gate_pass": True,
                            "activated": True,
                        },
                        "runtime": {
                            "model_version_latest": "old-model",
                            "candidate_identity_match": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_strategy_candidate_manifest(manifest)

        self.assertEqual(section["status"], "pass")
        self.assertEqual(
            section["lifecycle_status"],
            "activation_pending_runtime",
        )
        self.assertIn(
            "strategy candidate lifecycle incomplete: "
            "status=activation_pending_runtime",
            section["warn_reasons"],
        )

    def test_strategy_candidate_contract_rejects_replay_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = pathlib.Path(td) / "strategy_candidate_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "strategy_candidate_v1",
                        "candidate_id": "model-v1",
                        "status": "rejected",
                        "candidate": {
                            "model_version": "model-v1",
                            "model_sha256": "abc123",
                            "integrator_report_sha256": "report123",
                            "training_symbol": "SOLUSDT",
                            "bar_interval_ms": 300000,
                            "online_bar_source": "closed_ohlcv",
                            "source_venue": "bybit",
                            "source_category": "linear",
                            "price_type": "trade_price",
                            "volume_unit": "base_asset",
                        },
                        "replay_validation": {
                            "candidate_model_version": "model-v1",
                            "candidate_model_sha256": "different",
                            "candidate_integrator_report_sha256": "report123",
                            "independent_identity_match": False,
                            "source_symbol": "SOLUSDT",
                            "feature_contract_match": True,
                            "config_binds_candidate": True,
                            "report_config_identity_match": True,
                            "evaluates_current_candidate": True,
                        },
                        "registry": {
                            "model_version": "model-v1",
                            "model_sha256": "abc123",
                            "integrator_report_sha256": "report123",
                            "candidate_identity_match": True,
                        },
                        "runtime": {},
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_strategy_candidate_manifest(manifest)

        self.assertEqual(section["status"], "fail")
        self.assertIn(
            "replay candidate model hash differs from candidate",
            section["fail_reasons"],
        )

    def test_strategy_candidate_contract_rejects_replay_symbol_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = pathlib.Path(td) / "strategy_candidate_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "strategy_candidate_v1",
                        "candidate_id": "model-v1",
                        "status": "rejected",
                        "candidate": {
                            "model_version": "model-v1",
                            "model_sha256": "abc123",
                            "integrator_report_sha256": "report123",
                            "training_symbol": "SOLUSDT",
                            "bar_interval_ms": 300000,
                            "online_bar_source": "closed_ohlcv",
                            "source_venue": "bybit",
                            "source_category": "linear",
                            "price_type": "trade_price",
                            "volume_unit": "base_asset",
                        },
                        "replay_validation": {
                            "candidate_model_version": "model-v1",
                            "candidate_model_sha256": "abc123",
                            "candidate_integrator_report_sha256": "report123",
                            "independent_identity_match": True,
                            "source_symbol": "BTCUSDT",
                            "feature_contract_match": False,
                            "config_binds_candidate": True,
                            "report_config_identity_match": True,
                            "evaluates_current_candidate": True,
                        },
                        "registry": {
                            "model_version": "model-v1",
                            "model_sha256": "abc123",
                            "integrator_report_sha256": "report123",
                            "candidate_identity_match": True,
                        },
                        "runtime": {},
                    }
                ),
                encoding="utf-8",
            )

            section = REPORT.assess_strategy_candidate_manifest(manifest)

        self.assertEqual(section["status"], "fail")
        self.assertIn(
            "replay source symbol/bar contract differs from candidate training contract",
            section["fail_reasons"],
        )

    def test_inherited_candidate_is_refreshed_with_current_runtime_evidence(self):
        candidate = {
            "status": "pass",
            "fail_reasons": [],
            "warn_reasons": [
                "strategy candidate lifecycle incomplete: "
                "status=activation_pending_runtime"
            ],
            "candidate_id": "model-v1",
            "lifecycle_status": "activation_pending_runtime",
            "candidate": {
                "model_sha256": "a" * 64,
                "integrator_report_sha256": "b" * 64,
                "training_symbol": "SOLUSDT",
                "bar_interval_ms": 300000,
            },
            "registry": {
                "active_runtime_config_sha256": "c" * 64,
                "active_trade_bot_sha256": "d" * 64,
            },
        }
        runtime = {
            "verdict": "PASS",
            "metrics": {
                "integrator_model_versions": ["model-v1"],
                "integrator_model_version_latest": "model-v1",
                "integrator_model_sha256_latest": "a" * 64,
                "integrator_report_sha256_latest": "b" * 64,
                "integrator_runtime_config_sha256_latest": "c" * 64,
                "integrator_trade_bot_sha256_latest": "d" * 64,
                "integrator_feature_training_symbol_latest": "SOLUSDT",
                "integrator_feature_bar_interval_ms_latest": 300000,
                "integrator_policy_applied_count": 4,
                "integrator_policy_canary_count": 4,
                "integrator_policy_filled_candidate_ids": [
                    "model-v1",
                    "model-v1",
                ],
                "integrator_policy_filled_events": [
                    {
                        "candidate_id": "model-v1",
                        "model_version": "model-v1",
                        "client_order_id": "order-1",
                    },
                    {
                        "candidate_id": "model-v1",
                        "model_version": "model-v1",
                        "client_order_id": "order-1",
                    },
                ],
                "integrator_policy_closed_episode_events": [
                    {
                        "candidate_id": "model-v1",
                        "model_version": "model-v1",
                        "mode": "canary",
                        "position_episode_id": "episode-1",
                        "evidence_complete": True,
                    }
                ],
                "funnel_fills_runtime_count": 2,
            },
        }

        refreshed = REPORT.refresh_strategy_candidate_runtime(candidate, runtime)

        self.assertEqual(refreshed["status"], "pass")
        self.assertEqual(refreshed["lifecycle_status"], "canary_evidence")
        self.assertEqual(refreshed["warn_reasons"], [])
        self.assertTrue(refreshed["runtime"]["candidate_identity_match"])
        self.assertEqual(refreshed["runtime"]["candidate_fill_count"], 2)

    def test_activation_decision_distinguishes_pending_and_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "activation_decision.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "closed_loop_activation_decision_v1",
                        "decision": "pending",
                        "candidate_model_version": "model-v1",
                        "pending_reasons": ["complete canary episodes 2 < 10"],
                        "evidence": {"complete_episode_count": 2},
                    }
                ),
                encoding="utf-8",
            )
            pending = REPORT.assess_activation_decision(path)
            self.assertEqual(pending["status"], "pass")
            self.assertEqual(
                pending["readiness_status"], "CANARY_PENDING_EVIDENCE"
            )
            self.assertEqual(len(pending["warn_reasons"]), 1)

            path.write_text(
                json.dumps(
                    {
                        "schema_version": "closed_loop_activation_decision_v1",
                        "decision": "rollback",
                        "candidate_model_version": "model-v1",
                        "hard_fail_reasons": [
                            "runtime four-part/model feature identity mismatch"
                        ],
                    }
                ),
                encoding="utf-8",
            )
            rolled_back = REPORT.assess_activation_decision(path)
            self.assertEqual(rolled_back["status"], "fail")
            self.assertEqual(rolled_back["readiness_status"], "ROLLED_BACK")

    def test_activation_decision_is_bound_to_transaction_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            decision_path = root / "activation_decision.json"
            transaction_path = root / "activation_transaction.json"
            identity = {
                "model_sha256": "a" * 64,
                "report_sha256": "b" * 64,
                "runtime_config_sha256": "c" * 64,
                "trade_bot_sha256": "d" * 64,
            }
            decision_path.write_text(
                json.dumps(
                    {
                        "schema_version": "closed_loop_activation_decision_v1",
                        "decision": "commit",
                        "transaction_run_id": "run-1",
                        "candidate_model_version": "model-v1",
                        "candidate_identity": identity,
                        "activation_policy_sha256": "e" * 64,
                        "evaluated_at_utc": "2026-07-27T00:10:00Z",
                    }
                ),
                encoding="utf-8",
            )
            transaction_path.write_text(
                json.dumps(
                    {
                        "schema_version": "closed_loop_activation_transaction_v2",
                        "run_id": "run-1",
                        "status": "committed",
                        "activation_policy_sha256": "e" * 64,
                        "candidate": {
                            "model_version": "model-v1",
                            "identity": identity,
                        },
                        "latest_evaluation": {
                            "evaluated_at_utc": "2026-07-27T00:10:00Z"
                        },
                    }
                ),
                encoding="utf-8",
            )
            bound = REPORT.assess_activation_decision(
                decision_path, transaction_path
            )
            self.assertEqual(bound["status"], "pass")
            self.assertTrue(bound["transaction_binding"]["match"])

            transaction = json.loads(
                transaction_path.read_text(encoding="utf-8")
            )
            transaction["candidate"]["identity"]["trade_bot_sha256"] = "f" * 64
            transaction_path.write_text(
                json.dumps(transaction), encoding="utf-8"
            )
            mismatch = REPORT.assess_activation_decision(
                decision_path, transaction_path
            )
            self.assertEqual(mismatch["status"], "fail")
            self.assertFalse(mismatch["transaction_binding"]["match"])

    def test_generic_fills_do_not_prove_candidate_canary_evidence(self):
        candidate = {
            "status": "pass",
            "fail_reasons": [],
            "warn_reasons": [],
            "candidate_id": "model-v1",
            "lifecycle_status": "activation_pending_runtime",
            "candidate": {
                "model_sha256": "a" * 64,
                "integrator_report_sha256": "b" * 64,
                "training_symbol": "SOLUSDT",
                "bar_interval_ms": 300000,
            },
            "registry": {
                "active_runtime_config_sha256": "c" * 64,
                "active_trade_bot_sha256": "d" * 64,
            },
        }
        runtime = {
            "verdict": "PASS",
            "metrics": {
                "integrator_model_versions": ["model-v1"],
                "integrator_model_version_latest": "model-v1",
                "integrator_model_sha256_latest": "a" * 64,
                "integrator_report_sha256_latest": "b" * 64,
                "integrator_runtime_config_sha256_latest": "c" * 64,
                "integrator_trade_bot_sha256_latest": "d" * 64,
                "integrator_feature_training_symbol_latest": "SOLUSDT",
                "integrator_feature_bar_interval_ms_latest": 300000,
                "integrator_policy_applied_count": 3,
                "integrator_policy_canary_count": 3,
                "integrator_policy_filled_candidate_ids": [],
                "funnel_fills_runtime_count": 9,
            },
        }

        refreshed = REPORT.refresh_strategy_candidate_runtime(candidate, runtime)

        self.assertEqual(refreshed["lifecycle_status"], "canary_observing")
        self.assertEqual(refreshed["runtime"]["candidate_fill_count"], 0)
        self.assertIn(
            "strategy candidate lifecycle incomplete: "
            "status=canary_observing",
            refreshed["warn_reasons"],
        )

    def test_matching_version_with_wrong_runtime_hash_rejects_candidate(self):
        candidate = {
            "status": "pass",
            "fail_reasons": [],
            "warn_reasons": [],
            "candidate_id": "model-v1",
            "lifecycle_status": "activation_pending_runtime",
            "candidate": {
                "model_sha256": "a" * 64,
                "integrator_report_sha256": "b" * 64,
                "training_symbol": "SOLUSDT",
                "bar_interval_ms": 300000,
            },
            "registry": {
                "active_runtime_config_sha256": "c" * 64,
                "active_trade_bot_sha256": "d" * 64,
            },
        }
        runtime = {
            "verdict": "PASS",
            "metrics": {
                "integrator_model_version_latest": "model-v1",
                "integrator_model_sha256_latest": "c" * 64,
                "integrator_report_sha256_latest": "b" * 64,
                "integrator_runtime_config_sha256_latest": "c" * 64,
                "integrator_trade_bot_sha256_latest": "d" * 64,
                "integrator_feature_training_symbol_latest": "SOLUSDT",
                "integrator_feature_bar_interval_ms_latest": 300000,
            },
        }

        refreshed = REPORT.refresh_strategy_candidate_runtime(candidate, runtime)

        self.assertEqual(refreshed["status"], "fail")
        self.assertEqual(refreshed["lifecycle_status"], "rejected")
        self.assertFalse(refreshed["runtime"]["candidate_identity_match"])
        self.assertIn(
            "runtime candidate model/report identity mismatch",
            refreshed["fail_reasons"],
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

    def test_decision_evidence_continue_is_research_only_and_never_promotes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            output = root / "closed_loop_report.json"
            decision_evidence = root / "decision_evidence_report.json"
            decision_evidence.write_text(
                json.dumps(
                    {
                        "schema_version": "decision_evidence_report_v1",
                        "benchmark_id": "a" * 64,
                        "research_decision": "CONTINUE",
                        "reason_codes": ["ALL_DECISIVE_EVIDENCE_PROVEN"],
                        "promotion_authority": False,
                        "demo_activation_authorized": False,
                        "live_activation_authorized": False,
                        "research_decision_only": True,
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
                    "--decision_evidence_report",
                    str(decision_evidence),
                ]
                code = REPORT.main()
            finally:
                sys.argv = old_argv

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(payload["research_decision"], "CONTINUE")
        self.assertTrue(payload["research_decision_only"])
        self.assertFalse(payload["promotion_authority"])
        self.assertFalse(payload["demo_activation_authorized"])
        self.assertFalse(payload["live_activation_authorized"])
        self.assertEqual(payload["promotion_readiness_status"], "NOT_EVALUATED")
        section = payload["sections"]["decision_evidence"]
        self.assertEqual(section["status"], "VERIFIED")
        self.assertEqual(section["research_decision"], "CONTINUE")
        self.assertEqual(
            section["reason_codes"], ["ALL_DECISIVE_EVIDENCE_PROVEN"]
        )
        self.assertFalse(section["authoritative_for_integrator_promotion"])

    def test_decision_evidence_missing_corrupt_or_manifest_mismatch_fails_closed(self):
        cases = ("missing", "corrupt", "manifest_mismatch")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                output = root / "closed_loop_report.json"
                decision_evidence = root / "decision_evidence_report.json"
                if case != "missing":
                    decision_evidence.write_text(
                        (
                            "{broken"
                            if case == "corrupt"
                            else json.dumps(
                                {
                                    "schema_version": "decision_evidence_report_v1",
                                    "benchmark_id": "b" * 64,
                                    "research_decision": "CONTINUE",
                                    "reason_codes": [],
                                    "promotion_authority": False,
                                    "demo_activation_authorized": False,
                                    "live_activation_authorized": False,
                                    "research_decision_only": True,
                                }
                            )
                        ),
                        encoding="utf-8",
                    )
                argv = [
                    "build_closed_loop_report.py",
                    "--output",
                    str(output),
                    "--decision_evidence_report",
                    str(decision_evidence),
                ]
                if case == "manifest_mismatch":
                    expected_report = root / "expected_decision_evidence.json"
                    expected_report.write_text(
                        json.dumps(
                            {
                                "schema_version": "decision_evidence_report_v1",
                                "benchmark_id": "a" * 64,
                                "research_decision": "STOP",
                                "reason_codes": ["EXPECTED_STOP"],
                                "promotion_authority": False,
                                "demo_activation_authorized": False,
                                "live_activation_authorized": False,
                                "research_decision_only": True,
                            }
                        ),
                        encoding="utf-8",
                    )
                    manifest = root / "run_manifest.json"
                    manifest.write_text(
                        json.dumps(
                            {
                                "run_id": "run-decision-evidence",
                                "action": "full",
                                "artifacts": {
                                    "decision_evidence_report": {
                                        "path": str(expected_report),
                                        "sha256": hashlib.sha256(
                                            expected_report.read_bytes()
                                        ).hexdigest(),
                                    }
                                },
                                "decision_evidence": {
                                    "research_decision_only": True,
                                    "promotion_authority": False,
                                    "research_decision": "STOP",
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    argv.extend(["--run_manifest", str(manifest)])

                old_argv = sys.argv[:]
                try:
                    sys.argv = argv
                    REPORT.main()
                finally:
                    sys.argv = old_argv

                payload = json.loads(output.read_text(encoding="utf-8"))
                section = payload["sections"]["decision_evidence"]
                self.assertEqual(section["status"], "UNVERIFIABLE")
                self.assertEqual(section["research_decision"], "STOP")
                self.assertTrue(section["research_decision_only"])
                self.assertFalse(section["promotion_authority"])
                self.assertFalse(section["demo_activation_authorized"])
                self.assertFalse(section["live_activation_authorized"])
                self.assertEqual(
                    payload["promotion_readiness_status"], "NOT_EVALUATED"
                )


if __name__ == "__main__":
    unittest.main()
