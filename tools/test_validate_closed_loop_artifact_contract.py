#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name(
        "validate_closed_loop_artifact_contract.py"
    )
    spec = importlib.util.spec_from_file_location(
        "validate_closed_loop_artifact_contract", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_module()
ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "config" / "closed_loop_contract.json"
DECISIVE_STEPS_AND_ARTIFACTS = {
    "decision_benchmark_validation": "decision_benchmark_validation.json",
    "objective_alignment_validation": "objective_alignment_validation.json",
    "paired_evolution_replay": "paired_evolution_replay.json",
    "evolution_uplift_validation": "evolution_uplift_validation.json",
    "experiment_budget_audit": "experiment_budget_audit.json",
    "decision_evidence_report": "decision_evidence_report.json",
}
DECISIVE_STEPS = tuple(DECISIVE_STEPS_AND_ARTIFACTS)


def load_public_summary_module():
    module_path = pathlib.Path(__file__).with_name(
        "summarize_closed_loop_failure.py"
    )
    spec = importlib.util.spec_from_file_location(
        "summarize_closed_loop_failure", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DownloadClosedLoopReportsContractTest(unittest.TestCase):
    def test_downloads_decisive_evidence_and_diagnostics(self):
        source = (ROOT / "tools" / "download_closed_loop_reports.sh").read_text(
            encoding="utf-8"
        )
        for step, filename in DECISIVE_STEPS_AND_ARTIFACTS.items():
            self.assertIn(
                f'fetch_report "${{REMOTE_BASE}}/{filename}" '
                f'".artifacts/{filename}" "{step}" "json"',
                source,
            )
        diagnostics = {
            "data_pipeline/data_pipeline_report.json": (
                "data_pipeline_report.json",
                "data_pipeline_report",
            ),
            "decision_evidence_benchmark.json": (
                "decision_evidence_benchmark.json",
                "decision_evidence_benchmark",
            ),
            "decision_benchmark_build/build_report.json": (
                "decision_benchmark_build_report.json",
                "decision_benchmark_build_report",
            ),
            "decision_benchmark_build/candidate_preflight.json": (
                "decision_candidate_preflight_report.json",
                "decision_candidate_preflight_report",
            ),
            "experiment_budget_proposal.json": (
                "experiment_budget_proposal.json",
                "experiment_budget_proposal",
            ),
            "microstructure_alpha_regime_evidence_audit.json": (
                "microstructure_alpha_regime_evidence_audit.json",
                "microstructure_alpha_regime_evidence_audit",
            ),
        }
        for remote, (local, label) in diagnostics.items():
            self.assertIn(
                f'fetch_report "${{REMOTE_BASE}}/{remote}" '
                f'".artifacts/{local}" "{label}" "json"',
                source,
            )

    def test_maps_regime_evidence_audit_from_manifest_to_downloaded_artifact(self):
        self.assertEqual(
            VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
                "microstructure_alpha_regime_evidence_audit"
            ],
            "microstructure_alpha_regime_evidence_audit.json",
        )

    def test_market_alpha_manifest_and_flattened_download_names_are_explicit(self):
        self.assertEqual(
            VALIDATOR.MANIFEST_ARTIFACT_BASENAMES[
                "market_alpha_development_report"
            ],
            "market_alpha_verification_h12.json",
        )
        self.assertEqual(
            VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
                "market_alpha_development_report"
            ],
            "market_alpha_development_report.json",
        )
        self.assertEqual(
            VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
                "maker_execution_opportunity_experiment"
            ],
            "maker_execution_opportunity_experiment.json",
        )
        self.assertEqual(
            VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
                "maker_execution_learnability_experiment"
            ],
            "maker_execution_learnability_experiment.json",
        )
        self.assertEqual(
            VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
                "maker_subsecond_information_experiment"
            ],
            "maker_subsecond_information_experiment.json",
        )

    def test_downloader_always_emits_sanitized_public_failure_summary(self):
        source = (ROOT / "tools" / "download_closed_loop_reports.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'fetch_report "${REMOTE_BASE}/closed_loop_runner_command.log" '
            '".artifacts/closed_loop_runner_command.log" '
            '"closed_loop_runner_command_log" "text"',
            source,
        )
        self.assertRegex(
            source,
            re.compile(
                r"emit_public_summary\(\) \{\n"
                r"\s+python3 tools/summarize_closed_loop_failure.py "
                r"-d \.artifacts -a \|\| true\n"
                r"\}\n"
                r"trap emit_public_summary EXIT"
            ),
        )

    def test_closed_loop_summary_receives_liquidation_experiment(self):
        source = (ROOT / "tools" / "closed_loop_runner.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'SUMMARY_ARGS+=(--liquidation_information_set_experiment_report '
            '"${LIQUIDATION_EXPERIMENT_REPORT_PATH}")',
            source,
        )
        self.assertIn(
            'SUMMARY_ARGS+=(--maker_execution_opportunity_experiment_report '
            '"${MAKER_OPPORTUNITY_EXPERIMENT_REPORT_PATH}")',
            source,
        )
        self.assertIn(
            'SUMMARY_ARGS+=(--maker_execution_learnability_experiment_report '
            '"${MAKER_LEARNABILITY_EXPERIMENT_REPORT_PATH}")',
            source,
        )
        self.assertIn(
            'SUMMARY_ARGS+=(--maker_subsecond_information_experiment_report '
            '"${MAKER_SUBSECOND_EXPERIMENT_REPORT_PATH}")',
            source,
        )


class PublicClosedLoopFailureSummaryTest(unittest.TestCase):
    def test_summary_exposes_sanitized_runner_errors(self):
        summary_module = load_public_summary_module()
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            (artifact_dir / "closed_loop_runner_command.log").write_text(
                "\n".join(
                    (
                        "ordinary progress api_key=must-not-leak",
                        "[ERROR] identity mismatch token=must-not-leak",
                        "missing report: /opt/ai-trade/run/report.json",
                    )
                ),
                encoding="utf-8",
            )

            summary = summary_module.build_summary(artifact_dir)
            annotation = summary_module._annotation(summary)
            encoded = json.dumps(summary, sort_keys=True)

        self.assertEqual(len(summary["runner_errors"]), 2)
        self.assertIn("identity mismatch", annotation)
        self.assertIn("<redacted>", encoded)
        self.assertNotIn("must-not-leak", encoded)
        self.assertNotIn("ordinary progress", encoded)
        self.assertNotIn("/opt/ai-trade", encoded)

    def test_summary_exposes_only_fixed_statuses_and_sanitized_reason_codes(self):
        summary_module = load_public_summary_module()
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            (artifact_dir / "closed_loop_download_status.json").write_text(
                json.dumps(
                    {
                        "status": "DONE",
                        "downloaded_count": 10,
                        "invalid_count": 1,
                        "missing": ["runtime_log"],
                        "invalid": ["artifact_contract"],
                        "remote_base": "/opt/ai-trade/private/path",
                    }
                ),
                encoding="utf-8",
            )
            (artifact_dir / "step_status.jsonl").write_text(
                "".join(
                    json.dumps(record, sort_keys=True) + "\n"
                    for record in (
                        {
                            "step": "decision_benchmark_validation",
                            "kind": "observation",
                            "result": "pass",
                            "exit_code": 0,
                            "blocked_by_prior_failure": False,
                        },
                        {
                            "step": "paired_evolution_replay",
                            "kind": "observation",
                            "result": "fail",
                            "exit_code": 2,
                            "blocked_by_prior_failure": False,
                            "raw_error": "api_secret=must-not-leak",
                        },
                    )
                ),
                encoding="utf-8",
            )
            reports = {
                "decision_benchmark_validation.json": {
                    "identity_status": "VERIFIED",
                    "drifts": [],
                },
                "objective_alignment_validation.json": {
                    "overall_status": "NOT_ALIGNED",
                    "missing_fields": ["subsystems.miner.blocks"],
                },
                "paired_evolution_replay.json": {
                    "status": "UNVERIFIABLE",
                    "mismatches": [
                        "input.replay_report_sha256_mismatch",
                        "/opt/private/api_secret=must-not-leak",
                    ],
                },
                "evolution_uplift_validation.json": {
                    "status": "UNVERIFIABLE",
                    "missing_evidence": ["paired.arms.adaptive.report"],
                },
                "experiment_budget_audit.json": {
                    "decision": "BLOCK_INVALID_LEDGER",
                    "reasons": ["registration_missing", "token must-not-leak"],
                },
                "decision_evidence_report.json": {
                    "research_decision": "STOP",
                    "reason_codes": ["UPLIFT_INPUT_UNVERIFIABLE"],
                    "promotion_authority": False,
                    "demo_activation_authorized": False,
                    "live_activation_authorized": False,
                    "stage_review_required": False,
                    "next_action": "collect_next_fully_non_overlapping_oos_regime",
                },
            }
            for filename, payload in reports.items():
                (artifact_dir / filename).write_text(
                    json.dumps(payload), encoding="utf-8"
                )

            summary = summary_module.build_summary(artifact_dir)
            encoded = json.dumps(summary, sort_keys=True)

        self.assertEqual(summary["download"]["status"], "DONE")
        self.assertEqual(
            summary["failed_steps"],
            [
                {
                    "step": "paired_evolution_replay",
                    "kind": "observation",
                    "result": "fail",
                    "exit_code": 2,
                    "blocked_by_prior_failure": False,
                }
            ],
        )
        self.assertEqual(
            summary["decisive"]["paired_evolution_replay"]["reason_codes"],
            ["input.replay_report_sha256_mismatch"],
        )
        self.assertEqual(
            summary["decisive"]["experiment_budget_audit"]["reason_codes"],
            ["registration_missing"],
        )
        self.assertFalse(summary["authorities"]["demo"])
        self.assertNotIn("/opt/ai-trade", encoded)
        self.assertNotIn("api_secret", encoded)
        self.assertNotIn("must-not-leak", encoded)

    def test_summary_exposes_liquidation_not_ready_stage_and_progress(self):
        summary_module = load_public_summary_module()
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            (artifact_dir / "liquidation_information_set_experiment.json").write_text(
                json.dumps(
                    {
                        "schema_version": "liquidation_information_set_experiment_v1",
                        "status": "NOT_READY",
                        "fully_verifiable": False,
                        "research_domain": "forward_development_only",
                        "promotion_evidence": False,
                        "promotion_eligible": False,
                        "promotion_authority": False,
                        "demo_activation_authorized": False,
                        "live_activation_authorized": False,
                        "research_decision": "NOT_READY",
                        "reason_codes": [
                            "liquidation_capture_not_ready",
                            "minimum_forward_capture_duration",
                        ],
                        "not_ready_stage": "liquidation_capture",
                        "capture_readiness": {
                            "control": {"status": "PASS"},
                            "liquidation": {
                                "status": "NOT_READY",
                                "coverage_ms": 120_000_000,
                                "minimum_coverage_ms": 126_000_000,
                                "missing_coverage_ms": 6_000_000,
                                "coverage_ratio": 120 / 126,
                                "freshness_age_ms": 12_000,
                                "report_file_count": 140,
                                "valid_segment_count": 0,
                                "invalid_segment_count": 140,
                            },
                        },
                        "minimum_common_span_seconds_for_frozen_splits": 123_062,
                    }
                ),
                encoding="utf-8",
            )
            summary = summary_module.build_summary(artifact_dir)
            annotation = summary_module._annotation(summary)

        section = summary["upstream"]["liquidation_information_set_experiment"]
        self.assertEqual(section["not_ready_stage"], "liquidation_capture")
        self.assertEqual(section["control_capture_status"], "PASS")
        self.assertEqual(section["liquidation_capture_status"], "NOT_READY")
        self.assertEqual(section["metrics"]["liquidation_coverage_seconds"], 120_000)
        self.assertEqual(
            section["metrics"]["liquidation_missing_coverage_seconds"], 6_000
        )
        self.assertEqual(section["metrics"]["liquidation_report_file_count"], 140)
        self.assertEqual(section["metrics"]["liquidation_valid_segment_count"], 0)
        self.assertEqual(section["metrics"]["liquidation_invalid_segment_count"], 140)
        self.assertIn("information_set_progress=stage:liquidation_capture", annotation)
        self.assertIn("liquidation_capture_not_ready", annotation)

    def test_summary_exposes_maker_opportunity_decision_and_economics(self):
        summary_module = load_public_summary_module()
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            (artifact_dir / "maker_execution_opportunity_experiment.json").write_text(
                json.dumps(
                    {
                        "schema_version": "maker_execution_opportunity_experiment_v1",
                        "status": "COMPLETE",
                        "fully_verifiable": True,
                        "research_domain": "forward_development_only",
                        "promotion_evidence": False,
                        "promotion_eligible": False,
                        "promotion_authority": False,
                        "demo_activation_authorized": False,
                        "live_activation_authorized": False,
                        "research_decision": (
                            "CONTINUE_TO_MAKER_LEARNABILITY_EXPERIMENT"
                        ),
                        "reason_codes": [
                            "maker_oracle_all_first_window_gates_passed"
                        ],
                        "common_domain": {"row_count": 123456},
                        "fill_audit": {
                            "filled_decision_count": 321,
                            "filled_action_count": 1234,
                        },
                        "hindsight_oracle": {
                            "trade_count": 55,
                            "positive_stress_split_ratio": 1.0,
                            "base_cost_by_split": {"lcb_bps": 1.5},
                            "stress_cost_by_split": {"lcb_bps": 0.25},
                        },
                        "stability_audit": {
                            "boundary_sensitivity": {"pass_ratio": 0.75},
                            "independent_forward": {
                                "row_ratio": 0.5,
                                "observation_complete": False,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            summary = summary_module.build_summary(artifact_dir)
            annotation = summary_module._annotation(summary)

        section = summary["upstream"]["maker_execution_opportunity_experiment"]
        self.assertEqual(section["gate_status"], "COMPLETE")
        self.assertEqual(
            section["research_decision"],
            "CONTINUE_TO_MAKER_LEARNABILITY_EXPERIMENT",
        )
        self.assertEqual(section["metrics"]["filled_action_count"], 1234)
        self.assertEqual(section["metrics"]["oracle_stress_lcb_bps"], 0.25)
        self.assertEqual(section["metrics"]["boundary_pass_ratio"], 0.75)
        self.assertFalse(section["metrics"]["forward_observation_complete"])
        self.assertIn(
            "maker_opportunity_decision=CONTINUE_TO_MAKER_LEARNABILITY_EXPERIMENT",
            annotation,
        )
        self.assertIn("oracle_stress_lcb_bps:0.25", annotation)
        self.assertIn("forward_row_ratio:0.5", annotation)
        self.assertIn("forward_observation_complete:false", annotation)

    def test_summary_exposes_maker_learnability_architecture_economics(self):
        summary_module = load_public_summary_module()
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            architectures = {}
            for architecture_id, passed, stress_lcb in (
                ("sequential_hurdle_tail_action_value", True, 1.25),
            ):
                architectures[architecture_id] = {
                    "fully_verifiable": True,
                    "trade_count": 45,
                    "positive_stress_split_ratio": 1.0 if passed else 0.0,
                    "oos_base_cost_by_split": {"lcb_bps": stress_lcb + 1.0},
                    "oos_stress_cost_by_split": {"lcb_bps": stress_lcb},
                    "prediction_permutation_control": {"passed": passed},
                    "maker_decision_gate_passed": passed,
                }
            (
                artifact_dir / "maker_execution_learnability_experiment.json"
            ).write_text(
                json.dumps(
                    {
                        "schema_version": (
                            "maker_execution_learnability_experiment_v1"
                        ),
                        "status": "COMPLETE",
                        "fully_verifiable": True,
                        "research_domain": "forward_development_only",
                        "promotion_evidence": False,
                        "promotion_eligible": False,
                        "promotion_authority": False,
                        "demo_activation_authorized": False,
                        "live_activation_authorized": False,
                        "diagnostic_leader_is_preregistered": False,
                        "research_decision": (
                            "CONTINUE_TO_INDEPENDENT_MAKER_FORWARD_VALIDATION"
                        ),
                        "diagnostic_leader_id": (
                            "sequential_hurdle_tail_action_value"
                        ),
                        "reason_codes": ["maker_learnability_gate_passed"],
                        "data": {"eligible_row_count": 120000},
                        "architecture_comparison": {
                            "architectures": architectures
                        },
                    }
                ),
                encoding="utf-8",
            )
            summary = summary_module.build_summary(artifact_dir)
            annotation = summary_module._annotation(summary)

        section = summary["upstream"][
            "maker_execution_learnability_experiment"
        ]
        self.assertEqual(section["gate_status"], "COMPLETE")
        self.assertEqual(
            section["diagnostic_leader_id"],
            "sequential_hurdle_tail_action_value",
        )
        self.assertEqual(section["metrics"]["hurdle_tail_stress_lcb_bps"], 1.25)
        self.assertTrue(section["metrics"]["hurdle_tail_maker_gate_passed"])
        self.assertIn(
            "maker_learnability_decision="
            "CONTINUE_TO_INDEPENDENT_MAKER_FORWARD_VALIDATION",
            annotation,
        )
        self.assertIn(
            "maker_learnability_leader=sequential_hurdle_tail_action_value",
            annotation,
        )
        self.assertIn("hurdle_tail_stress_lcb_bps:1.25", annotation)

    def test_summary_exposes_subsecond_information_increment(self):
        summary_module = load_public_summary_module()
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            architectures = {
                "one_second_decomposed_baseline": {
                    "trade_count": 40,
                    "oos_base_cost_by_split": {"lcb_bps": -2.0},
                    "oos_stress_cost_by_split": {"lcb_bps": -3.0},
                    "prediction_permutation_control": {"passed": False},
                },
                "subsecond_queue_decomposed_treatment": {
                    "trade_count": 55,
                    "oos_base_cost_by_split": {"lcb_bps": 2.0},
                    "oos_stress_cost_by_split": {"lcb_bps": 1.0},
                    "prediction_permutation_control": {"passed": True},
                },
            }
            (
                artifact_dir / "maker_subsecond_information_experiment.json"
            ).write_text(
                json.dumps(
                    {
                        "schema_version": "maker_subsecond_information_experiment_v1",
                        "status": "COMPLETE",
                        "fully_verifiable": True,
                        "research_domain": "forward_development_only",
                        "promotion_evidence": False,
                        "promotion_eligible": False,
                        "promotion_authority": False,
                        "demo_activation_authorized": False,
                        "live_activation_authorized": False,
                        "independent_forward_validation_required": True,
                        "research_decision": (
                            "CONTINUE_TO_INDEPENDENT_SUBSECOND_MAKER_FORWARD_VALIDATION"
                        ),
                        "reason_codes": ["subsecond_information_gate_passed"],
                        "data": {
                            "subsecond_aligned_eligible_row_count": 110000,
                            "subsecond_aligned_row_ratio": 0.92,
                        },
                        "architecture_comparison": {
                            "architectures": architectures
                        },
                        "incremental_information_diagnostics": {
                            "treatment_positive_stress_split_ratio": 1.0,
                            "positive_profitability_roc_auc_gain_split_ratio": 1.0,
                            "treatment_fill_roc_auc_by_split": {"mean_bps": 0.7},
                            "treatment_profitability_roc_auc_by_split": {
                                "mean_bps": 0.6
                            },
                            "profitability_roc_auc_gain_by_split": {
                                "mean_bps": 0.03
                            },
                            "stress_mean_improvement_by_split": {
                                "mean_bps": 3.0
                            },
                            "decision_gate_passed": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            summary = summary_module.build_summary(artifact_dir)
            annotation = summary_module._annotation(summary)
        section = summary["upstream"]["maker_subsecond_information_experiment"]
        self.assertEqual(section["gate_status"], "COMPLETE")
        self.assertEqual(section["metrics"]["treatment_stress_lcb_bps"], 1.0)
        self.assertEqual(section["metrics"]["profitability_auc_gain"], 0.03)
        self.assertIn(
            "maker_subsecond_decision="
            "CONTINUE_TO_INDEPENDENT_SUBSECOND_MAKER_FORWARD_VALIDATION",
            annotation,
        )
        self.assertIn("treatment_stress_lcb_bps:1.0", annotation)

    def test_summary_exposes_actionable_upstream_alpha_and_candidate_diagnostics(self):
        summary_module = load_public_summary_module()
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            reports = {
                "market_alpha_development_report.json": {
                    "status": "FAIL",
                    "fully_verifiable": True,
                    "data_gates": {
                        "cross_market_cross_asset_history": "PASS",
                        "bybit_trade_archive_sample": "FAIL",
                    },
                    "economic_screen": {
                        "development_passed": False,
                        "feature_set_count": 2,
                        "variant_result_count": 6,
                    },
                    "next_gate": "remain_in_development_and_reject_candidate",
                    "private_error": "/opt/api_secret=must-not-leak",
                },
                "microstructure_alpha_development_report.json": {
                    "status": "FAIL",
                    "fully_verifiable": True,
                    "economic_screen": {
                        "development_passed": False,
                        "trained_split_count": 6,
                        "required_split_count": 6,
                        "oos_base_cost_by_trade": {"count": 17},
                        "oos_base_cost_by_split": {"lcb_bps": -1.25},
                        "oos_stress_cost_by_split": {"lcb_bps": -2.5},
                        "positive_base_edge_split_ratio": 0.33,
                        "minimum_oos_trades": 24,
                        "minimum_positive_splits_ratio": 0.5,
                        "action_consensus_ratio": 0.67,
                        "minimum_action_consensus_ratio": 0.5,
                        "prediction_permutation_control_passed": True,
                    },
                    "failures": [
                        "source_capture_incomplete",
                        "/opt/api_secret=must-not-leak",
                    ],
                    "next_gate": "reject_microstructure_candidate_and_remain_in_development",
                },
                "microstructure_alpha_regime_evidence_audit.json": {
                    "schema_version": "microstructure_regime_evidence_audit_v1",
                    "status": "SKIPPED_OVERLAP",
                    "reason_codes": ["oos_regime_overlaps_accepted_evidence"],
                    "accepted_batch_count": 1,
                    "independent_oos_hours": 24.0,
                    "research_observation_only": True,
                    "promotion_authority": False,
                    "demo_activation_authorized": False,
                    "live_activation_authorized": False,
                    "stage_review_required": False,
                    "next_action": "collect_next_fully_non_overlapping_oos_regime",
                    "private_path": "/opt/api_secret=must-not-leak",
                },
                "microstructure_alpha_lifecycle_report.json": {
                    "status": "NOT_READY",
                    "fully_verifiable": True,
                    "phase": "selection_collecting",
                    "not_ready_reason": "selection_window_incomplete",
                    "failures": [],
                    "demo_entry_eligible": False,
                    "live_promotion_eligible": False,
                },
                "alpha_source_route_report.json": {
                    "status": "NOT_READY",
                    "selected_route": None,
                    "reason": "no_independently_gated_alpha_source_ready",
                    "sources": {
                        "legacy_integrator": {"readiness": "REJECTED"},
                        "microstructure_demo": {"readiness": "NOT_READY"},
                    },
                    "live_promotion_eligible": False,
                },
                "liquidation_information_set_experiment.json": {
                    "schema_version": "liquidation_information_set_experiment_v1",
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
                        "paired_treatment_minus_control_lcb_not_positive",
                        "/opt/api_secret=must-not-leak",
                    ],
                    "common_domain": {"row_count": 123456},
                    "hindsight_oracle": {
                        "opportunity_proven": True,
                        "trade_count": 72,
                        "positive_stress_split_ratio": 0.5,
                        "base_cost_by_split": {"lcb_bps": 4.0},
                        "stress_cost_by_split": {"lcb_bps": 1.25},
                    },
                    "arms": {
                        "control": {
                            "aggregate": {
                                "architectures": {
                                    "direct_stress_utility_regression": {
                                        "trade_count": 37,
                                        "oos_base_cost_by_split": {
                                            "lcb_bps": -0.25
                                        },
                                        "oos_stress_cost_by_split": {
                                            "lcb_bps": -1.0,
                                            "positive_ratio": 0.33,
                                        },
                                        "prediction_permutation_control": {
                                            "passed": False
                                        },
                                    }
                                }
                            }
                        },
                        "treatment": {
                            "aggregate": {
                                "architectures": {
                                    "direct_stress_utility_regression": {
                                        "trade_count": 41,
                                        "oos_base_cost_by_split": {
                                            "lcb_bps": 0.1
                                        },
                                        "oos_stress_cost_by_split": {
                                            "lcb_bps": -0.75,
                                            "positive_ratio": 0.5,
                                        },
                                        "prediction_permutation_control": {
                                            "passed": True
                                        },
                                    }
                                }
                            }
                        }
                    },
                    "paired_treatment_minus_control": {
                        "base_cost_delta_by_split": {"lcb_bps": -0.1},
                        "stress_cost_delta_by_split": {"lcb_bps": -0.5},
                        "permutation_null": {"passed": False},
                    },
                },
                "decision_benchmark_build_report.json": {
                    "status": "UNVERIFIABLE",
                    "errors": [
                        "input.candidate_model_missing",
                        "/opt/api_secret=must-not-leak",
                    ],
                    "candidate_preflight": {
                        "status": "UNVERIFIABLE",
                        "errors": ["candidate.model_missing_or_empty"],
                    },
                },
                "decision_candidate_preflight_report.json": {
                    "status": "UNVERIFIABLE",
                    "errors": [
                        "candidate.report.model_version_missing",
                        "api_secret=must-not-leak",
                    ],
                },
            }
            for filename, payload in reports.items():
                (artifact_dir / filename).write_text(
                    json.dumps(payload), encoding="utf-8"
                )

            summary = summary_module.build_summary(artifact_dir)
            annotation = summary_module._annotation(summary)
            encoded = json.dumps(summary, sort_keys=True)

        upstream = summary["upstream"]
        self.assertEqual(upstream["market_alpha_development"]["status"], "FAIL")
        self.assertEqual(
            upstream["market_alpha_development"]["reason_codes"],
            [
                "data_gate.bybit_trade_archive_sample",
                "economic_screen.no_variant_passed",
            ],
        )
        self.assertEqual(
            upstream["microstructure_alpha_development"]["reason_codes"],
            [
                "source_capture_incomplete",
                "economic_screen.minimum_oos_trades",
                "economic_screen.minimum_positive_splits_ratio",
                "economic_screen.base_split_lcb_not_positive",
                "economic_screen.stress_split_lcb_not_positive",
            ],
        )
        self.assertEqual(
            upstream["microstructure_alpha_development"]["metrics"],
            {
                "oos_trade_count": 17,
                "base_split_lcb_bps": -1.25,
                "stress_split_lcb_bps": -2.5,
                "positive_split_ratio": 0.33,
                "action_consensus_ratio": 0.67,
            },
        )
        self.assertEqual(
            upstream["microstructure_regime_evidence"],
            {
                "artifact": "PRESENT",
                "status": "SKIPPED_OVERLAP",
                "reason_codes": ["oos_regime_overlaps_accepted_evidence"],
                "accepted_batch_count": 1,
                "independent_oos_hours": 24.0,
                "research_observation_only": True,
                "promotion_authority": False,
                "demo_activation_authorized": False,
                "live_activation_authorized": False,
                "stage_review_required": False,
                "next_action": "collect_next_fully_non_overlapping_oos_regime",
            },
        )
        self.assertEqual(
            upstream["microstructure_alpha_lifecycle"]["reason_codes"],
            ["selection_window_incomplete"],
        )
        self.assertEqual(
            upstream["alpha_source_route"]["reason_codes"],
            ["no_independently_gated_alpha_source_ready"],
        )
        self.assertEqual(
            upstream["liquidation_information_set_experiment"],
            {
                "artifact": "PRESENT",
                "status": "COMPLETE",
                "reason_codes": [
                    "paired_treatment_minus_control_lcb_not_positive"
                ],
                "gate_status": "COMPLETE",
                "research_decision": "STOP_INFORMATION_SOURCE",
                "research_observation_only": True,
                "promotion_authority": False,
                "demo_activation_authorized": False,
                "live_activation_authorized": False,
                "metrics": {
                    "common_row_count": 123456,
                    "oracle_trade_count": 72,
                    "oracle_positive_split_ratio": 0.5,
                    "oracle_base_lcb_bps": 4.0,
                    "oracle_stress_lcb_bps": 1.25,
                    "control_trade_count": 37,
                    "control_positive_split_ratio": 0.33,
                    "control_base_lcb_bps": -0.25,
                    "control_stress_lcb_bps": -1.0,
                    "control_permutation_passed": False,
                    "treatment_trade_count": 41,
                    "treatment_positive_split_ratio": 0.5,
                    "treatment_base_lcb_bps": 0.1,
                    "treatment_stress_lcb_bps": -0.75,
                    "treatment_permutation_passed": True,
                    "paired_delta_base_lcb_bps": -0.1,
                    "paired_delta_stress_lcb_bps": -0.5,
                    "paired_permutation_passed": False,
                },
            },
        )
        self.assertEqual(
            upstream["decision_candidate_preflight"]["reason_codes"],
            ["candidate.report.model_version_missing"],
        )
        self.assertIn("STOP_INFORMATION_SOURCE", annotation)
        self.assertIn("paired_treatment_minus_control_lcb_not_positive", annotation)
        self.assertIn("oracle_stress_lcb_bps:1.25", annotation)
        self.assertIn("control_permutation_passed:false", annotation)
        self.assertIn("treatment_permutation_passed:true", annotation)
        self.assertIn("paired_permutation_passed:false", annotation)
        self.assertIn("input.candidate_model_missing", annotation)
        self.assertIn("economic_screen.minimum_oos_trades", annotation)
        self.assertNotIn("/opt", encoded)
        self.assertNotIn("api_secret", encoded)
        self.assertNotIn("must-not-leak", encoded)


class ValidateClosedLoopArtifactContractTest(unittest.TestCase):
    @staticmethod
    def write_step_records(
        artifact_dir: pathlib.Path,
        manifest_path: pathlib.Path,
        manifest: dict,
        records: list,
    ) -> None:
        step_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
            "step_status"
        ]
        step_path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        manifest["artifacts"]["step_status"]["sha256"] = hashlib.sha256(
            step_path.read_bytes()
        ).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    @staticmethod
    def read_step_records(artifact_dir: pathlib.Path) -> list:
        step_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
            "step_status"
        ]
        return [
            json.loads(line)
            for line in step_path.read_text(encoding="utf-8").splitlines()
        ]

    def build_rejected_full_run(self, artifact_dir: pathlib.Path):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        action_contract = contract["actions"]["full"]
        rejection_contract = action_contract["route_rejection_contract"]
        optional = set(rejection_contract["optional_artifacts"])
        run_id = "run-rejected"
        step_records = []
        for index, step in enumerate(action_contract["required_steps"]):
            after_rejection = bool(step_records) and any(
                record["step"] == "alpha_source_route" for record in step_records
            )
            record = {
                "recorded_at_utc": f"2026-08-12T00:00:{index:02d}Z",
                "run_id": run_id,
                "action": "full",
                "step": step,
                "kind": "required",
                "result": "skipped" if after_rejection else "pass",
                "exit_code": None if after_rejection else 0,
                "blocked_by_prior_failure": after_rejection,
                "research_decision_only": False,
                "duration_ms": 0 if after_rejection else 1,
            }
            if step == "alpha_source_route":
                record.update(result="fail", exit_code=2, duration_ms=1)
            step_records.append(record)
            if step == "alpha_source_route":
                for decisive_step in DECISIVE_STEPS:
                    step_records.append(
                        {
                            "recorded_at_utc": "2026-08-12T00:01:00Z",
                            "run_id": run_id,
                            "action": "full",
                            "step": decisive_step,
                            "kind": "route",
                            "result": "skipped",
                            "exit_code": None,
                            "blocked_by_prior_failure": False,
                            "research_decision_only": False,
                            "duration_ms": 0,
                        }
                    )
        step_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
            "step_status"
        ]
        step_path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                for item in step_records
            ),
            encoding="utf-8",
        )
        route_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
            "alpha_source_route_report"
        ]
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
        artifacts = {}
        for name in action_contract["required_artifacts"]:
            if name in optional:
                continue
            path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[name]
            if name not in {"step_status", "alpha_source_route_report"}:
                path.write_text(name, encoding="utf-8")
            artifacts[name] = {
                "path": (
                    "/remote/"
                    + VALIDATOR.MANIFEST_ARTIFACT_BASENAMES.get(name, path.name)
                ),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        manifest = {
            "run_id": run_id,
            "action": "full",
            "artifact_contract": {
                "schema_version": contract["schema_version"],
                "contract_sha256": hashlib.sha256(
                    CONTRACT_PATH.read_bytes()
                ).hexdigest(),
                "action": "full",
                "required_artifacts": action_contract["required_artifacts"],
                "required_steps": action_contract["required_steps"],
                "route_contracts": action_contract["route_contracts"],
                "route_rejection_contract": rejection_contract,
            },
            "artifacts": artifacts,
        }
        manifest_path = artifact_dir / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path, manifest

    def build_legacy_full_run(self, artifact_dir: pathlib.Path):
        manifest_path, manifest = self.build_rejected_full_run(artifact_dir)
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        action_contract = contract["actions"]["full"]
        route_contract = action_contract["route_contracts"]["legacy_integrator"]
        route_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
            "alpha_source_route_report"
        ]
        route_path.write_text(
            json.dumps(
                {
                    "schema_version": "alpha_source_route_v1",
                    "status": "PASS",
                    "selected_route": "legacy_integrator",
                }
            ),
            encoding="utf-8",
        )
        records_by_step = {
            record["step"]: record
            for record in self.read_step_records(artifact_dir)
        }
        effective_steps = list(action_contract["required_steps"])
        insertion = effective_steps.index("alpha_source_route") + 1
        effective_steps[insertion:insertion] = route_contract["required_steps"]
        records = []
        for index, step in enumerate(effective_steps):
            record = records_by_step.get(
                step,
                {
                    "recorded_at_utc": f"2026-08-12T01:00:{index:02d}Z",
                    "run_id": "run-rejected",
                    "action": "full",
                    "step": step,
                    "kind": "required",
                    "result": "pass",
                    "exit_code": 0,
                    "blocked_by_prior_failure": False,
                    "research_decision_only": False,
                    "duration_ms": 1,
                },
            )
            if step in DECISIVE_STEPS:
                record.update(
                    kind="observation",
                    result="fail",
                    exit_code=2,
                    blocked_by_prior_failure=False,
                    research_decision_only=True,
                    duration_ms=1,
                )
            else:
                record.update(
                    kind="required",
                    result="pass",
                    exit_code=0,
                    blocked_by_prior_failure=False,
                    research_decision_only=False,
                    duration_ms=1,
                )
            records.append(record)
        self.write_step_records(artifact_dir, manifest_path, manifest, records)
        required = list(
            dict.fromkeys(
                action_contract["required_artifacts"]
                + route_contract["required_artifacts"]
            )
        )
        for name in required:
            path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[name]
            if name == "step_status":
                pass
            elif name == "alpha_source_route_report":
                pass
            else:
                path.write_text(name, encoding="utf-8")
            manifest["artifacts"][name] = {
                "path": "/remote/"
                + VALIDATOR.MANIFEST_ARTIFACT_BASENAMES.get(name, path.name),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path, manifest

    def test_declared_route_rejection_allows_only_contractual_downstream_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            manifest_path, _ = self.build_rejected_full_run(artifact_dir)

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertEqual(failures, [])

    def test_full_contract_requires_decisive_observations_only_for_legacy_candidate(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        full = contract["actions"]["full"]
        optional = set(full["route_rejection_contract"]["optional_artifacts"])
        legacy = full["route_contracts"]["legacy_integrator"]

        for name, filename in DECISIVE_STEPS_AND_ARTIFACTS.items():
            with self.subTest(name=name):
                self.assertNotIn(name, full["required_steps"])
                self.assertNotIn(name, full["required_artifacts"])
                self.assertIn(name, legacy["required_steps"])
                self.assertIn(name, legacy["required_artifacts"])
                self.assertNotIn(name, optional)
                self.assertEqual(VALIDATOR.LOCAL_ARTIFACT_FILENAMES[name], filename)

    def test_each_decisive_artifact_is_required_and_sha256_verified(self):
        for name, filename in DECISIVE_STEPS_AND_ARTIFACTS.items():
            with self.subTest(name=name, failure="missing"):
                with tempfile.TemporaryDirectory() as td:
                    artifact_dir = pathlib.Path(td)
                    manifest_path, manifest = self.build_legacy_full_run(
                        artifact_dir
                    )
                    manifest["artifacts"].pop(name)
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                    failures = VALIDATOR.validate_artifact_contract(
                        manifest_path, artifact_dir, CONTRACT_PATH
                    )

                    self.assertIn(f"{name}:required_not_manifested", failures)

            with self.subTest(name=name, failure="sha256"):
                with tempfile.TemporaryDirectory() as td:
                    artifact_dir = pathlib.Path(td)
                    manifest_path, _ = self.build_legacy_full_run(artifact_dir)
                    (artifact_dir / filename).write_text(
                        "tampered", encoding="utf-8"
                    )

                    failures = VALIDATOR.validate_artifact_contract(
                        manifest_path, artifact_dir, CONTRACT_PATH
                    )

                    self.assertIn(f"{name}:sha256", failures)

    def test_manifest_artifact_path_must_match_fixed_filename(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            manifest_path, manifest = self.build_legacy_full_run(artifact_dir)
            manifest["artifacts"]["decision_evidence_report"]["path"] = (
                "/remote/not-the-decision-report.json"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertIn("decision_evidence_report:path", failures)

    def test_declared_route_rejection_does_not_hide_upstream_artifact_loss(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            manifest_path, manifest = self.build_rejected_full_run(artifact_dir)
            manifest["artifacts"].pop("baseline_report")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertIn("baseline_report:required_not_manifested", failures)

    def test_route_rejection_requires_matching_failed_gate_ledger_record(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            manifest_path, manifest = self.build_rejected_full_run(artifact_dir)
            records = self.read_step_records(artifact_dir)
            next(
                record
                for record in records
                if record["step"] == "alpha_source_route"
            )["kind"] = "observation"
            self.write_step_records(
                artifact_dir, manifest_path, manifest, records
            )

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertIn("alpha_source_route:invalid", failures)

    def test_each_decisive_step_requires_exactly_one_terminal_record(self):
        for target in DECISIVE_STEPS:
            with self.subTest(step=target, mutation="deleted"):
                with tempfile.TemporaryDirectory() as td:
                    artifact_dir = pathlib.Path(td)
                    manifest_path, manifest = self.build_legacy_full_run(
                        artifact_dir
                    )
                    records = [
                        record
                        for record in self.read_step_records(artifact_dir)
                        if record["step"] != target
                    ]
                    self.write_step_records(
                        artifact_dir, manifest_path, manifest, records
                    )

                    failures = VALIDATOR.validate_artifact_contract(
                        manifest_path, artifact_dir, CONTRACT_PATH
                    )

                    self.assertIn(f"step_status:{target}:missing", failures)

        for target in DECISIVE_STEPS:
            with self.subTest(step=target, mutation="duplicated"):
                with tempfile.TemporaryDirectory() as td:
                    artifact_dir = pathlib.Path(td)
                    manifest_path, manifest = self.build_legacy_full_run(
                        artifact_dir
                    )
                    records = self.read_step_records(artifact_dir)
                    target_record = next(
                        record for record in records if record["step"] == target
                    )
                    records.append(dict(target_record))
                    self.write_step_records(
                        artifact_dir, manifest_path, manifest, records
                    )

                    failures = VALIDATOR.validate_artifact_contract(
                        manifest_path, artifact_dir, CONTRACT_PATH
                    )

                    self.assertIn(
                        f"step_status:{target}:duplicate", failures
                    )

    def test_decisive_steps_must_preserve_fixed_execution_order(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            manifest_path, manifest = self.build_legacy_full_run(artifact_dir)
            records = self.read_step_records(artifact_dir)
            first = next(
                index
                for index, record in enumerate(records)
                if record["step"] == DECISIVE_STEPS[0]
            )
            second = next(
                index
                for index, record in enumerate(records)
                if record["step"] == DECISIVE_STEPS[1]
            )
            records[first], records[second] = records[second], records[first]
            self.write_step_records(
                artifact_dir, manifest_path, manifest, records
            )

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertIn("step_status:decisive_order", failures)

    def test_decisive_terminal_record_identity_and_flags_are_fail_closed(self):
        mutations = (
            ("blocked_by_prior_failure", True, "blocked_by_prior_failure"),
            ("research_decision_only", False, "research_decision_only"),
            ("run_id", "wrong-run", "run_id"),
            ("action", "deploy", "action"),
            ("kind", "route", "kind"),
            ("result", "skipped", "result"),
            ("exit_code", 0, "exit_code"),
        )
        target = DECISIVE_STEPS[0]
        for field, value, reason in mutations:
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as td:
                    artifact_dir = pathlib.Path(td)
                    manifest_path, manifest = self.build_legacy_full_run(
                        artifact_dir
                    )
                    records = self.read_step_records(artifact_dir)
                    next(
                        record for record in records if record["step"] == target
                    )[field] = value
                    self.write_step_records(
                        artifact_dir, manifest_path, manifest, records
                    )

                    failures = VALIDATOR.validate_artifact_contract(
                        manifest_path, artifact_dir, CONTRACT_PATH
                    )

                    self.assertIn(
                        f"step_status:{target}:{reason}", failures
                    )

    def test_step_status_requires_canonical_jsonl_and_valid_record_schema(self):
        mutations = (
            ("invalid_json", "invalid_json:2"),
            ("noncanonical", "noncanonical:2"),
            ("invalid_schema", "invalid_record:2"),
        )
        for mutation, reason in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as td:
                    artifact_dir = pathlib.Path(td)
                    manifest_path, manifest = self.build_rejected_full_run(
                        artifact_dir
                    )
                    step_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
                        "step_status"
                    ]
                    lines = step_path.read_text(encoding="utf-8").splitlines()
                    if mutation == "invalid_json":
                        lines[1] = "{invalid"
                    elif mutation == "invalid_schema":
                        record = json.loads(lines[1])
                        record.pop("research_decision_only")
                        lines[1] = json.dumps(
                            record, ensure_ascii=False, sort_keys=True
                        )
                    else:
                        lines[1] = json.dumps(
                            json.loads(lines[1]),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    step_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    manifest["artifacts"]["step_status"]["sha256"] = (
                        hashlib.sha256(step_path.read_bytes()).hexdigest()
                    )
                    manifest_path.write_text(
                        json.dumps(manifest), encoding="utf-8"
                    )

                    failures = VALIDATOR.validate_artifact_contract(
                        manifest_path, artifact_dir, CONTRACT_PATH
                    )

                    self.assertIn(f"step_status:{reason}", failures)

        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            manifest_path, manifest = self.build_rejected_full_run(artifact_dir)
            step_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
                "step_status"
            ]
            step_path.write_bytes(b"\xff\n")
            manifest["artifacts"]["step_status"]["sha256"] = hashlib.sha256(
                step_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertIn("step_status:unreadable", failures)

    def test_step_status_manifest_path_and_hash_are_verified(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            manifest_path, manifest = self.build_rejected_full_run(artifact_dir)
            manifest["artifacts"]["step_status"]["path"] = (
                "/remote/not-step-status.jsonl"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertIn("step_status:path", failures)

        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            manifest_path, _ = self.build_rejected_full_run(artifact_dir)
            step_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
                "step_status"
            ]
            step_path.write_text(
                step_path.read_text(encoding="utf-8") + "{}\n",
                encoding="utf-8",
            )

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertIn("step_status:sha256", failures)


if __name__ == "__main__":
    unittest.main()
