#!/usr/bin/env python3
"""Validate downloaded Closed Loop artifacts against the versioned contract."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


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
    "liquidation_capture_report": "liquidation_capture_report.json",
    "liquidation_information_set_experiment": (
        "liquidation_information_set_experiment.json"
    ),
    "maker_execution_opportunity_experiment": (
        "maker_execution_opportunity_experiment.json"
    ),
    "maker_opportunity_frozen_audit": "maker_opportunity_frozen_audit.json",
    "maker_execution_learnability_experiment": (
        "maker_execution_learnability_experiment.json"
    ),
    "maker_subsecond_information_experiment": (
        "maker_subsecond_information_experiment.json"
    ),
    "microstructure_alpha_development_report": (
        "microstructure_alpha_development_report.json"
    ),
    "microstructure_alpha_regime_evidence_audit": (
        "microstructure_alpha_regime_evidence_audit.json"
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

# Some producers keep a descriptive, versioned name under their run-specific
# subdirectory while the downloader intentionally exposes a stable public
# filename.  Keep the two identities explicit; never infer or accept arbitrary
# basename drift.
MANIFEST_ARTIFACT_BASENAMES = {
    "market_alpha_development_report": "market_alpha_verification_h12.json",
}

DECISIVE_OBSERVATION_STEPS = (
    "decision_benchmark_validation",
    "objective_alignment_validation",
    "paired_evolution_replay",
    "evolution_uplift_validation",
    "experiment_budget_audit",
    "decision_evidence_report",
)

STEP_RECORD_FIELDS = frozenset(
    {
        "recorded_at_utc",
        "run_id",
        "action",
        "step",
        "kind",
        "result",
        "exit_code",
        "blocked_by_prior_failure",
        "research_decision_only",
        "duration_ms",
    }
)


def read_json_object(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return payload


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_step_record(record: Dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def valid_step_record_schema(record: Dict[str, Any]) -> bool:
    if frozenset(record) != STEP_RECORD_FIELDS:
        return False
    for name in (
        "recorded_at_utc",
        "run_id",
        "action",
        "step",
        "kind",
        "result",
    ):
        if not isinstance(record.get(name), str) or not record[name]:
            return False
    try:
        dt.datetime.strptime(record["recorded_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    exit_code = record.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        return False
    duration_ms = record.get("duration_ms")
    return bool(
        isinstance(record.get("blocked_by_prior_failure"), bool)
        and isinstance(record.get("research_decision_only"), bool)
        and isinstance(duration_ms, int)
        and not isinstance(duration_ms, bool)
        and duration_ms >= 0
    )


def audit_step_records(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not path.is_file():
        return [], []
    records: List[Dict[str, Any]] = []
    failures: List[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return [], ["step_status:unreadable"]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            failures.append(f"step_status:invalid_json:{line_number}")
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            failures.append(f"step_status:invalid_json:{line_number}")
            continue
        if not isinstance(record, dict):
            failures.append(f"step_status:invalid_record:{line_number}")
            continue
        if line != canonical_step_record(record):
            failures.append(f"step_status:noncanonical:{line_number}")
        if not valid_step_record_schema(record):
            failures.append(f"step_status:invalid_record:{line_number}")
            continue
        records.append(record)
    return records, failures


def read_step_records(path: Path) -> List[Dict[str, Any]]:
    records, _ = audit_step_records(path)
    return records


def validate_step_record_identity(
    step_records: List[Dict[str, Any]], run_id: str, action: str
) -> List[str]:
    failures: List[str] = []
    for line_number, record in enumerate(step_records, start=1):
        if record.get("run_id") != run_id:
            failures.append(f"step_status:run_id:{line_number}")
        if record.get("action") != action:
            failures.append(f"step_status:action:{line_number}")
    return failures


def validate_step_result_contract(record: Dict[str, Any]) -> List[str]:
    """Validate technical outcomes separately from research business outcomes."""
    failures: List[str] = []
    step = str(record.get("step") or "")
    kind = record.get("kind")
    result = record.get("result")
    exit_code = record.get("exit_code")
    blocked = record.get("blocked_by_prior_failure")
    business_results = {"rejected", "waiting", "not_ready"}
    if kind not in {"required", "diagnostic", "observation", "route"}:
        return [f"step_status:{step}:kind"]
    if result not in {"pass", "fail", "skipped", *business_results}:
        return [f"step_status:{step}:result"]
    if result == "pass":
        if exit_code != 0 or blocked is not False:
            failures.append(f"step_status:{step}:pass_contract")
    elif result == "fail":
        if (
            not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or exit_code == 0
            or blocked is not False
        ):
            failures.append(f"step_status:{step}:fail_contract")
    elif result in business_results:
        if (
            kind != "observation"
            or record.get("research_decision_only") is not True
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or exit_code < 0
            or blocked is not False
        ):
            failures.append(f"step_status:{step}:business_result_contract")
    elif kind == "route":
        if exit_code is not None or blocked is not False:
            failures.append(f"step_status:{step}:route_skip_contract")
    elif kind == "observation":
        if (
            exit_code is not None
            or blocked is not False
            or record.get("research_decision_only") is not True
        ):
            failures.append(f"step_status:{step}:observation_skip_contract")
    elif exit_code is not None or blocked is not True:
        failures.append(f"step_status:{step}:skip_contract")
    return failures


def validate_decisive_observations(
    step_records: List[Dict[str, Any]], run_id: str, action: str
) -> List[str]:
    failures: List[str] = []
    decisive_indices: List[int] = []
    all_steps_present_once = True
    for step in DECISIVE_OBSERVATION_STEPS:
        matches = [
            (index, record)
            for index, record in enumerate(step_records)
            if record.get("step") == step
        ]
        if not matches:
            failures.append(f"step_status:{step}:missing")
            all_steps_present_once = False
            continue
        if len(matches) != 1:
            failures.append(f"step_status:{step}:duplicate")
            all_steps_present_once = False
            continue
        index, record = matches[0]
        decisive_indices.append(index)
        if record.get("run_id") != run_id:
            failures.append(f"step_status:{step}:run_id")
        if record.get("action") != action:
            failures.append(f"step_status:{step}:action")
        if record.get("kind") != "observation":
            failures.append(f"step_status:{step}:kind")
        result = record.get("result")
        if result not in {"pass", "fail", "rejected", "waiting", "not_ready"}:
            failures.append(f"step_status:{step}:result")
        exit_code = record.get("exit_code")
        if (
            not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or (result == "pass" and exit_code != 0)
            or (result == "fail" and exit_code == 0)
            or (result in {"rejected", "waiting", "not_ready"} and exit_code < 0)
        ):
            failures.append(f"step_status:{step}:exit_code")
        if record.get("blocked_by_prior_failure") is not False:
            failures.append(f"step_status:{step}:blocked_by_prior_failure")
        if record.get("research_decision_only") is not True:
            failures.append(f"step_status:{step}:research_decision_only")
    if all_steps_present_once and decisive_indices != sorted(decisive_indices):
        failures.append("step_status:decisive_order")
    return failures


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
    effective_required_steps = list(required_steps)
    step_records, step_record_failures = audit_step_records(
        artifact_dir / LOCAL_ARTIFACT_FILENAMES["step_status"]
    )
    failures.extend(step_record_failures)
    manifest_run_id = str(manifest.get("run_id") or "")
    failures.extend(
        validate_step_record_identity(step_records, manifest_run_id, action)
    )
    for record in step_records:
        failures.extend(validate_step_result_contract(record))
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
        route_rejected = valid_route_rejection(
            route_payload,
            step_records,
            route_rejection_contract,
            manifest_run_id,
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
                try:
                    insertion = effective_required_steps.index(
                        "alpha_source_route"
                    ) + 1
                except ValueError:
                    insertion = len(effective_required_steps)
                effective_required_steps[insertion:insertion] = route_steps
        elif route_rejected:
            optional = set(optional_on_rejection)
            effective_required_artifacts = [
                name for name in effective_required_artifacts if name not in optional
            ]
        else:
            failures.append("alpha_source_route:invalid")

    if action == "full" and all(
        step in effective_required_steps for step in DECISIVE_OBSERVATION_STEPS
    ):
        failures.extend(
            validate_decisive_observations(
                step_records,
                manifest_run_id,
                action,
            )
        )

    step_names = [str(record.get("step") or "") for record in step_records]
    for step in sorted(set(step_names)):
        if step and step_names.count(step) != 1:
            failures.append(f"step_status:{step}:duplicate")
    missing_steps = [
        step for step in effective_required_steps if step not in step_names
    ]
    for step in missing_steps:
        failures.append(f"step_status:{step}:missing")
    for record in step_records:
        if (
            record.get("step") in effective_required_steps
            and record.get("result") == "skipped"
            and record.get("kind") == "observation"
        ):
            failures.append(f"step_status:{record['step']}:required_skipped")
    required_positions = [
        step_names.index(step)
        for step in effective_required_steps
        if step in step_names
    ]
    if required_positions != sorted(required_positions):
        failures.append("step_status:required_order")

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
        expected_manifest_basename = MANIFEST_ARTIFACT_BASENAMES.get(
            name, filename
        )
        if (
            not manifest_artifact_path
            or Path(manifest_artifact_path).name != expected_manifest_basename
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
