#!/usr/bin/env python3
"""Validate downloaded Closed Loop artifacts against the versioned contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


LOCAL_ARTIFACT_FILENAMES = {
    "step_status": "step_status.jsonl",
    "baseline_report": "baseline_report.json",
    "data_quality_report": "data_quality_report.json",
    "walkforward_report": "walkforward_report.json",
    "feature_store": "feature_store_5m.csv",
    "research_domain_split_report": "research_domain_split_report.json",
    "feature_parity_report": "feature_parity_report.json",
    "research_selection_feature_store": "research_selection_feature_5m.csv",
    "research_holdout_feature_store": "research_holdout_feature_5m.csv",
    "miner_report": "miner_report.json",
    "integrator_report": "integrator_report.json",
    "integrator_model": "integrator_latest.cbm",
    "model_registry_entry": "model_registry_entry.json",
    "replay_validation_report": "replay_validation_report.json",
    "selection_candidate_manifest": "selection_candidate_manifest.json",
    "replay_optimization_report": "replay_optimization_report.json",
    "strategy_diagnose_report": "strategy_diagnose_report.json",
    "alpha_mechanism_probe_report": "alpha_mechanism_probe_report.json",
    "market_alpha_development_report": "market_alpha_development_report.json",
    "microstructure_capture_upgrade_report": (
        "microstructure_capture_upgrade_report.json"
    ),
    "microstructure_capture_report": "microstructure_capture_report.json",
    "microstructure_alpha_development_report": (
        "microstructure_alpha_development_report.json"
    ),
    "microstructure_alpha_candidate_manifest": (
        "microstructure_alpha_candidate_manifest.json"
    ),
    "microstructure_alpha_model": "microstructure_alpha_development.cbm",
    "microstructure_alpha_lifecycle_report": (
        "microstructure_alpha_lifecycle_report.json"
    ),
    "alpha_source_route_report": "alpha_source_route_report.json",
    "decision_benchmark_validation": "decision_benchmark_validation.json",
    "objective_alignment_validation": "objective_alignment_validation.json",
    "paired_evolution_replay": "paired_evolution_replay.json",
    "evolution_uplift_validation": "evolution_uplift_validation.json",
    "experiment_budget_audit": "experiment_budget_audit.json",
    "decision_evidence_report": "decision_evidence_report.json",
    "microstructure_demo_binding_report": (
        "microstructure_demo_binding_report.json"
    ),
    "alpha_candidate_manifest": "alpha_candidate_manifest.json",
    "strategy_candidate_manifest": "strategy_candidate_manifest.json",
    "replay_candidate_config": "replay_candidate_config.yaml",
    "replay_validation_feature_build_report": "replay_feature_build_report.json",
    "replay_validation_command_log": "replay_validation_command.log",
    "runtime_log": "runtime.log",
    "runtime_assess_report": "runtime_assess.json",
    "trade_ledger_report": "trade_ledger_report.json",
    "closed_loop_mechanism_report": "closed_loop_mechanism_report.json",
    "activation_transaction": "activation_transaction.json",
    "activation_decision": "activation_decision.json",
}


def read_json_object(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return payload


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_step_records(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(record, dict):
            return []
        records.append(record)
    return records


def valid_route_rejection(
    route_payload: Dict[str, Any],
    step_records: List[Dict[str, Any]],
    route_rejection_contract: Dict[str, Any],
    run_id: str,
    action: str,
) -> bool:
    step_name = str(route_rejection_contract.get("step") or "")
    matches = [
        record
        for record in step_records
        if str(record.get("step") or "") == step_name
    ]
    if len(matches) != 1:
        return False
    route_step = matches[0]
    exit_code = route_step.get("exit_code")
    return bool(
        route_payload.get("schema_version") == "alpha_source_route_v1"
        and route_payload.get("status") == "FAIL"
        and not str(route_payload.get("selected_route") or "")
        and step_name == "alpha_source_route"
        and str(route_step.get("run_id") or "") == run_id
        and str(route_step.get("action") or "").strip().lower() == action
        and route_step.get("kind") == "required"
        and route_step.get("result") == "fail"
        and isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code != 0
        and route_step.get("blocked_by_prior_failure") is False
    )


def validate_artifact_contract(
    manifest_path: Path,
    artifact_dir: Path,
    contract_path: Path,
) -> List[str]:
    failures: List[str] = []
    try:
        manifest = read_json_object(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"manifest:invalid:{exc}"]
    try:
        contract = read_json_object(contract_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"contract:invalid:{exc}"]

    action = str(manifest.get("action") or "").strip().lower()
    actions = contract.get("actions", {})
    expected = actions.get(action, {}) if isinstance(actions, dict) else {}
    required_artifacts = expected.get("required_artifacts", [])
    required_steps = expected.get("required_steps", [])
    route_contracts = expected.get("route_contracts", {})
    route_rejection_contract = expected.get("route_rejection_contract", {})
    artifact_contract = manifest.get("artifact_contract", {})
    if not isinstance(artifact_contract, dict):
        failures.append("artifact_contract:missing")
        artifact_contract = {}
    if artifact_contract.get("schema_version") != contract.get("schema_version"):
        failures.append("artifact_contract:schema")
    if artifact_contract.get("contract_sha256") != file_sha256(contract_path):
        failures.append("artifact_contract:sha256")
    if artifact_contract.get("action") != action:
        failures.append("artifact_contract:action")
    if artifact_contract.get("required_artifacts") != required_artifacts:
        failures.append("artifact_contract:required_artifacts")
    if artifact_contract.get("required_steps") != required_steps:
        failures.append("artifact_contract:required_steps")
    if artifact_contract.get("route_contracts", {}) != route_contracts:
        failures.append("artifact_contract:route_contracts")
    if (
        artifact_contract.get("route_rejection_contract", {})
        != route_rejection_contract
    ):
        failures.append("artifact_contract:route_rejection_contract")
    if not required_artifacts or not required_steps:
        failures.append(f"artifact_contract:unknown_action:{action}")

    artifacts = manifest.get("artifacts", {})
    if not isinstance(artifacts, dict):
        failures.append("manifest:artifacts")
        artifacts = {}
    effective_required_artifacts = list(required_artifacts)
    if route_contracts:
        optional_on_rejection = route_rejection_contract.get("optional_artifacts")
        if (
            route_rejection_contract.get("step") != "alpha_source_route"
            or not isinstance(optional_on_rejection, list)
            or not all(
                isinstance(item, str) and item in required_artifacts
                for item in optional_on_rejection
            )
        ):
            failures.append("alpha_source_route:rejection_contract")
            optional_on_rejection = []
        route_path = artifact_dir / LOCAL_ARTIFACT_FILENAMES[
            "alpha_source_route_report"
        ]
        try:
            route_payload = read_json_object(route_path) if route_path.is_file() else {}
        except (OSError, json.JSONDecodeError, ValueError):
            route_payload = {}
        selected_route = str(route_payload.get("selected_route") or "")
        route_passed = bool(
            route_payload.get("schema_version") == "alpha_source_route_v1"
            and route_payload.get("status") == "PASS"
            and selected_route in route_contracts
        )
        step_records = read_step_records(
            artifact_dir / LOCAL_ARTIFACT_FILENAMES["step_status"]
        )
        route_rejected = valid_route_rejection(
            route_payload,
            step_records,
            route_rejection_contract,
            str(manifest.get("run_id") or ""),
            action,
        )
        if route_passed:
            selected_contract = route_contracts[selected_route]
            route_artifacts = selected_contract.get("required_artifacts", [])
            route_steps = selected_contract.get("required_steps", [])
            if not isinstance(route_artifacts, list) or not isinstance(
                route_steps, list
            ):
                failures.append(f"alpha_source_route:contract:{selected_route}")
            else:
                effective_required_artifacts.extend(route_artifacts)
        elif route_rejected:
            optional = set(optional_on_rejection)
            effective_required_artifacts = [
                name for name in effective_required_artifacts if name not in optional
            ]
        else:
            failures.append("alpha_source_route:invalid")

    for name in effective_required_artifacts:
        if name not in artifacts:
            failures.append(f"{name}:required_not_manifested")
    for name, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            failures.append(f"{name}:invalid_manifest_artifact")
            continue
        filename = LOCAL_ARTIFACT_FILENAMES.get(name)
        if filename is None:
            failures.append(f"{name}:unknown_manifest_artifact")
            continue
        manifest_artifact_path = str(artifact.get("path") or "").strip()
        if (
            not manifest_artifact_path
            or Path(manifest_artifact_path).name != filename
        ):
            failures.append(f"{name}:path")
        path = artifact_dir / filename
        if not path.is_file():
            failures.append(f"{name}:missing")
            continue
        actual = file_sha256(path)
        expected_hash = str(artifact.get("sha256") or "")
        if not expected_hash or actual != expected_hash:
            failures.append(f"{name}:sha256")
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path(".artifacts/run_manifest.json")
    )
    parser.add_argument("--artifact-dir", type=Path, default=Path(".artifacts"))
    parser.add_argument(
        "--contract", type=Path, default=Path("config/closed_loop_contract.json")
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    failures = validate_artifact_contract(
        args.manifest,
        args.artifact_dir,
        args.contract,
    )
    if failures:
        print(
            "[closed-loop] artifact contract mismatch: " + ",".join(failures),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
