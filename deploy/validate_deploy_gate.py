#!/usr/bin/env python3

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


EXPECTED_ACTION = "assess"
EXPECTED_STAGE = "DEPLOY"
OPERATIONAL_STEPS = (
    "s5_learning_switches",
    "runtime_assess",
    "s5_learning_activity",
)
AUDIT_STEP = "mechanism_audit"
REQUIRED_STEPS = (*OPERATIONAL_STEPS, AUDIT_STEP)
REQUIRED_ARTIFACTS = (
    "step_status",
    "runtime_log",
    "runtime_assess_report",
    "trade_ledger_report",
    "closed_loop_mechanism_report",
)
VALID_RUNTIME_VERDICTS = {"PASS", "PASS_WITH_ACTIONS", "FAIL"}


class GateValidationError(ValueError):
    pass


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateValidationError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise GateValidationError(f"{label} must contain a JSON object")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolved_child(path: Path, parent: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise GateValidationError(
            f"{label} is outside the current run directory: {resolved}"
        ) from exc
    return resolved


def validate_artifacts(
    manifest: dict[str, Any],
    run_dir: Path,
) -> dict[str, Path]:
    contract = manifest.get("artifact_contract")
    if not isinstance(contract, dict):
        raise GateValidationError("run_manifest artifact_contract is missing")
    if contract.get("action") != EXPECTED_ACTION:
        raise GateValidationError("run_manifest artifact contract action mismatch")
    if contract.get("required_steps") != list(REQUIRED_STEPS):
        raise GateValidationError("run_manifest required step contract mismatch")
    if contract.get("required_artifacts") != list(REQUIRED_ARTIFACTS):
        raise GateValidationError("run_manifest required artifact contract mismatch")

    contract_run_dir = Path(str(contract.get("run_specific_dir") or ""))
    if contract_run_dir.resolve() != run_dir.resolve():
        raise GateValidationError("run_manifest run-specific directory mismatch")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise GateValidationError("run_manifest artifacts are missing")

    validated: dict[str, Path] = {}
    for name in REQUIRED_ARTIFACTS:
        entry = artifacts.get(name)
        if not isinstance(entry, dict):
            raise GateValidationError(f"required artifact is missing: {name}")
        path_text = str(entry.get("path") or "").strip()
        expected_hash = str(entry.get("sha256") or "").strip().lower()
        if not path_text or len(expected_hash) != 64:
            raise GateValidationError(f"required artifact metadata is invalid: {name}")
        artifact_path = resolved_child(Path(path_text), run_dir, name)
        if not artifact_path.is_file():
            raise GateValidationError(f"required artifact file is missing: {name}")
        actual_hash = sha256_file(artifact_path)
        if actual_hash != expected_hash:
            raise GateValidationError(f"required artifact hash mismatch: {name}")
        validated[name] = artifact_path
    return validated


def read_step_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateValidationError(f"step_status is unreadable: {exc}") from exc
    for line_number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GateValidationError(
                f"step_status line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(record, dict):
            raise GateValidationError(
                f"step_status line {line_number} must be a JSON object"
            )
        records.append(record)
    if not records:
        raise GateValidationError("step_status has no records")
    return records


def validate_step_records(
    records: list[dict[str, Any]],
    expected_run_id: str,
) -> str:
    by_step: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("run_id") != expected_run_id:
            raise GateValidationError("step_status run_id mismatch")
        if record.get("action") != EXPECTED_ACTION:
            raise GateValidationError("step_status action mismatch")
        step = str(record.get("step") or "")
        by_step.setdefault(step, []).append(record)

    for step in REQUIRED_STEPS:
        matches = by_step.get(step, [])
        if len(matches) != 1:
            raise GateValidationError(
                f"step_status must contain exactly one record for {step}"
            )

    for step in OPERATIONAL_STEPS:
        record = by_step[step][0]
        if (
            record.get("kind") != "required"
            or record.get("result") != "pass"
            or record.get("exit_code") != 0
            or record.get("blocked_by_prior_failure") is not False
        ):
            raise GateValidationError(f"operational deploy step did not pass: {step}")

    audit = by_step[AUDIT_STEP][0]
    audit_result = str(audit.get("result") or "")
    if (
        audit.get("kind") != "required"
        or audit_result not in {"pass", "fail"}
        or audit.get("blocked_by_prior_failure") is not False
    ):
        raise GateValidationError("mechanism_audit step evidence is invalid")
    if audit_result == "pass" and audit.get("exit_code") != 0:
        raise GateValidationError("mechanism_audit pass has a non-zero exit code")
    if audit_result == "fail":
        exit_code = audit.get("exit_code")
        if not isinstance(exit_code, int) or exit_code == 0:
            raise GateValidationError("mechanism_audit fail lacks a non-zero exit code")

    for record in records:
        if record.get("kind") != "required":
            continue
        step = str(record.get("step") or "")
        if step == AUDIT_STEP:
            continue
        if record.get("result") != "pass":
            raise GateValidationError(f"unexpected required step failure: {step}")
    return audit_result


def validate_deploy_gate(
    manifest_path: Path,
    step_status_path: Path,
    runtime_assess_path: Path,
    closed_loop_report_path: Path,
    expected_run_id: str,
) -> dict[str, str]:
    if not expected_run_id:
        raise GateValidationError("expected run_id is empty")

    run_dir = manifest_path.resolve().parent
    expected_paths = {
        "step_status": resolved_child(step_status_path, run_dir, "step_status"),
        "runtime_assess_report": resolved_child(
            runtime_assess_path, run_dir, "runtime_assess_report"
        ),
        "closed_loop_report": resolved_child(
            closed_loop_report_path, run_dir, "closed_loop_report"
        ),
    }
    manifest = load_json(manifest_path, "run_manifest")
    if manifest.get("run_id") != expected_run_id:
        raise GateValidationError("run_manifest run_id mismatch")
    if manifest.get("action") != EXPECTED_ACTION:
        raise GateValidationError("run_manifest action mismatch")
    if str(manifest.get("stage") or "").upper() != EXPECTED_STAGE:
        raise GateValidationError("run_manifest stage mismatch")

    artifacts = validate_artifacts(manifest, run_dir)
    for name in ("step_status", "runtime_assess_report"):
        if artifacts[name] != expected_paths[name]:
            raise GateValidationError(f"{name} path does not match the current run")

    records = read_step_records(expected_paths["step_status"])
    audit_result = validate_step_records(records, expected_run_id)

    runtime_assess = load_json(expected_paths["runtime_assess_report"], "runtime_assess")
    verdict = str(runtime_assess.get("verdict") or "").upper()
    if verdict not in VALID_RUNTIME_VERDICTS:
        raise GateValidationError("runtime_assess verdict is invalid")
    if str(runtime_assess.get("stage") or "").upper() != EXPECTED_STAGE:
        raise GateValidationError("runtime_assess stage mismatch")

    report = load_json(expected_paths["closed_loop_report"], "closed_loop_report")
    if report.get("run_id") != expected_run_id:
        raise GateValidationError("closed_loop_report run_id mismatch")
    overall_status = str(report.get("overall_status") or "").upper()
    if not overall_status:
        raise GateValidationError("closed_loop_report overall_status is missing")
    report_verdict = str(report.get("runtime_verdict") or "").upper()
    if report_verdict != verdict:
        raise GateValidationError("closed_loop_report runtime verdict mismatch")

    mechanism_report = load_json(
        artifacts["closed_loop_mechanism_report"],
        "closed_loop_mechanism_report",
    )
    mechanism_status = str(mechanism_report.get("status") or "").lower()
    if mechanism_status not in {"pass", "pass_with_actions", "fail"}:
        raise GateValidationError("closed_loop_mechanism_report status is invalid")
    if audit_result == "pass" and mechanism_status != "pass":
        raise GateValidationError("mechanism_audit step/report status mismatch")
    if audit_result == "fail" and mechanism_status == "pass":
        raise GateValidationError("mechanism_audit step/report status mismatch")

    return {
        "run_id": expected_run_id,
        "runtime_verdict": verdict,
        "overall_status": overall_status,
        "mechanism_audit": audit_result,
        "mechanism_status": mechanism_status,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate fail-closed evidence for the DEPLOY runtime gate."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--step-status", required=True, type=Path)
    parser.add_argument("--runtime-assess", required=True, type=Path)
    parser.add_argument("--closed-loop-report", required=True, type=Path)
    parser.add_argument("--expected-run-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = validate_deploy_gate(
            args.manifest,
            args.step_status,
            args.runtime_assess,
            args.closed_loop_report,
            args.expected_run_id,
        )
    except GateValidationError as exc:
        print(f"DEPLOY_GATE_EVIDENCE_INVALID: {exc}")
        return 1
    print(
        "DEPLOY_GATE_EVIDENCE_OK: "
        f"run_id={result['run_id']} "
        f"runtime_verdict={result['runtime_verdict']} "
        f"overall_status={result['overall_status']} "
        f"mechanism_audit={result['mechanism_audit']} "
        f"mechanism_status={result['mechanism_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
