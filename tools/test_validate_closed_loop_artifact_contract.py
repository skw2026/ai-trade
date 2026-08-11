#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import pathlib
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


class ValidateClosedLoopArtifactContractTest(unittest.TestCase):
    def build_rejected_full_run(self, artifact_dir: pathlib.Path):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        action_contract = contract["actions"]["full"]
        rejection_contract = action_contract["route_rejection_contract"]
        optional = set(rejection_contract["optional_artifacts"])
        run_id = "run-rejected"
        step_records = [
            {
                "run_id": run_id,
                "action": "full",
                "step": "alpha_source_route",
                "kind": "required",
                "result": "fail",
                "exit_code": 2,
                "blocked_by_prior_failure": False,
            }
        ]
        step_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
            "step_status"
        ]
        step_path.write_text(
            "\n".join(json.dumps(item) for item in step_records) + "\n",
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
            step_path = artifact_dir / VALIDATOR.LOCAL_ARTIFACT_FILENAMES[
                "step_status"
            ]
            record = json.loads(step_path.read_text(encoding="utf-8"))
            record["kind"] = "observation"
            step_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            manifest["artifacts"]["step_status"]["sha256"] = hashlib.sha256(
                step_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            failures = VALIDATOR.validate_artifact_contract(
                manifest_path, artifact_dir, CONTRACT_PATH
            )

        self.assertIn("alpha_source_route:invalid", failures)


if __name__ == "__main__":
    unittest.main()
