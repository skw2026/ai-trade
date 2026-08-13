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

    def test_downloader_always_emits_sanitized_public_failure_summary(self):
        source = (ROOT / "tools" / "download_closed_loop_reports.sh").read_text(
            encoding="utf-8"
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


class PublicClosedLoopFailureSummaryTest(unittest.TestCase):
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
            upstream["decision_candidate_preflight"]["reason_codes"],
            ["candidate.report.model_version_missing"],
        )
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
        step_records = [
            {
                "recorded_at_utc": "2026-08-12T00:00:00Z",
                "run_id": run_id,
                "action": "full",
                "step": "alpha_source_route",
                "kind": "required",
                "result": "fail",
                "exit_code": 2,
                "blocked_by_prior_failure": False,
                "research_decision_only": False,
            }
        ]
        for index, step in enumerate(DECISIVE_STEPS, start=1):
            step_records.append(
                {
                    "recorded_at_utc": f"2026-08-12T00:00:{index:02d}Z",
                    "run_id": run_id,
                    "action": "full",
                    "step": step,
                    "kind": "observation",
                    "result": "fail",
                    "exit_code": 2,
                    "blocked_by_prior_failure": False,
                    "research_decision_only": True,
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
                "path": f"/remote/{path.name}",
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

    def test_declared_route_rejection_allows_only_contractual_downstream_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            artifact_dir = pathlib.Path(td)
            manifest_path, _ = self.build_rejected_full_run(artifact_dir)

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertEqual(failures, [])

    def test_full_contract_requires_decisive_observations_after_route_rejection(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        full = contract["actions"]["full"]
        optional = set(full["route_rejection_contract"]["optional_artifacts"])

        for name, filename in DECISIVE_STEPS_AND_ARTIFACTS.items():
            with self.subTest(name=name):
                self.assertIn(name, full["required_steps"])
                self.assertIn(name, full["required_artifacts"])
                self.assertNotIn(name, optional)
                self.assertEqual(VALIDATOR.LOCAL_ARTIFACT_FILENAMES[name], filename)

    def test_each_decisive_artifact_is_required_and_sha256_verified(self):
        for name, filename in DECISIVE_STEPS_AND_ARTIFACTS.items():
            with self.subTest(name=name, failure="missing"):
                with tempfile.TemporaryDirectory() as td:
                    artifact_dir = pathlib.Path(td)
                    manifest_path, manifest = self.build_rejected_full_run(
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
                    manifest_path, _ = self.build_rejected_full_run(artifact_dir)
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
            manifest_path, manifest = self.build_rejected_full_run(artifact_dir)
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
            records[0]["kind"] = "observation"
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
                    manifest_path, manifest = self.build_rejected_full_run(
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
                    manifest_path, manifest = self.build_rejected_full_run(
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
            manifest_path, manifest = self.build_rejected_full_run(artifact_dir)
            records = self.read_step_records(artifact_dir)
            records[1], records[2] = records[2], records[1]
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
                    manifest_path, manifest = self.build_rejected_full_run(
                        artifact_dir
                    )
                    records = self.read_step_records(artifact_dir)
                    records[1][field] = value
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
