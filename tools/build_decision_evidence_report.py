#!/usr/bin/env python3
"""Build a fail-closed research decision from three independent evidence reports."""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

from decision_evidence_common import (
    file_sha256,
    validate_verified_benchmark_report,
)
from experiment_budget_ledger import audit_next_experiment
from validate_evolution_uplift import validate_evolution_uplift_report_artifact
from validate_objective_alignment import validate_alignment_report_artifact


REPORT_SCHEMA_VERSION = "decision_evidence_report_v1"
BENCHMARK_SCHEMA_VERSION = "decision_evidence_benchmark_validation_v1"
ALIGNMENT_SCHEMA_VERSION = "objective_alignment_validation_v1"
UPLIFT_SCHEMA_VERSION = "evolution_uplift_validation_v1"
LEDGER_SCHEMA_VERSION = "experiment_budget_ledger_decision_v1"
SUBSYSTEMS = ("miner", "market_alpha", "microstructure", "online_tuner")
ALIGNMENT_STATUSES = frozenset({"ALIGNED", "NOT_ALIGNED", "UNVERIFIABLE"})
UPLIFT_STATUSES = frozenset({"UPLIFT_PROVEN", "NOT_PROVEN", "UNVERIFIABLE"})
LEDGER_DECISIONS = frozenset(
    {"ALLOW_NEXT_EXPERIMENT", "STOP_CURRENT_FAMILY", "BLOCK_INVALID_LEDGER"}
)
EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
LEDGER_REAUDIT_FIELDS = (
    "schema_version",
    "operation",
    "decision",
    "appended",
    "experiment_id",
    "registration_verified",
    "benchmark_verified",
    "hypothesis_family_id",
    "information_set_id",
    "benchmark_id",
    "expected_benchmark_id",
    "actual_benchmark_id",
    "validation_policy_sha256",
    "remaining_budgets",
    "registration_nonce",
    "actual_proposal_sha256",
    "registered_proposal_sha256",
    "registration_record_hash",
    "result_source_path",
    "ledger_record_count",
    "ledger_tail_record_hash",
    "checkpoint_recovery_required",
    "mismatches",
    "reasons",
)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_experiment_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and EXPERIMENT_ID_RE.fullmatch(value) is not None
    )


def _copy_report(report: Any) -> Any:
    return copy.deepcopy(report)


def _validator_exception_section(
    channel: str,
    report: Any,
    expected_benchmark_id: str | None,
    exc: Exception,
) -> dict[str, Any]:
    reason = f"{channel.upper()}_VALIDATOR_EXCEPTION"
    return {
        "status": "UNVERIFIABLE",
        "source_status": None,
        "expected_benchmark_id": expected_benchmark_id,
        "actual_benchmark_id": None,
        "benchmark_match": False if expected_benchmark_id is not None else None,
        "validation_errors": [
            f"{channel}.validator_exception:{type(exc).__name__}"
        ],
        "reason_codes": [reason],
        "report": _copy_report(report),
    }


def _identity_values(
    report: Mapping[str, Any], expected_benchmark_id: str | None
) -> tuple[str | None, bool | None]:
    declared = report.get("benchmark_id")
    actual = declared if isinstance(declared, str) else None
    if expected_benchmark_id is None:
        return actual, None
    candidates = (
        report.get("benchmark_id"),
        report.get("expected_benchmark_id"),
        report.get("actual_benchmark_id"),
    )
    declared_values = [value for value in candidates if value is not None]
    matches = (
        _is_sha256(declared)
        and declared == expected_benchmark_id
        and all(
            _is_sha256(value) and value == expected_benchmark_id
            for value in declared_values
        )
    )
    if not matches:
        for value in candidates:
            if value is not None and value != expected_benchmark_id:
                actual = value if isinstance(value, str) else None
                break
    return actual, matches


def _benchmark_section(
    report: Any,
    validation_policy: Any,
    validation_config_sha256: str | None,
) -> tuple[dict[str, Any], str | None]:
    raw = _copy_report(report)
    verification = validate_verified_benchmark_report(
        report,
        validation_policy=validation_policy,
        validation_config_sha256=validation_config_sha256,
    )
    errors = list(verification["errors"])
    actual_id = verification["benchmark_id"]
    verified = not errors
    section = {
        "status": "VERIFIED" if verified else "UNVERIFIABLE",
        "benchmark_id": actual_id if isinstance(actual_id, str) else None,
        "validation_errors": errors,
        "reason_codes": [] if verified else ["BENCHMARK_UNVERIFIABLE"],
        "verification": verification,
        "report": raw,
    }
    return section, section["benchmark_id"] if verified else None


def _base_child_section(
    report: Any,
    expected_benchmark_id: str | None,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    raw = _copy_report(report)
    if not isinstance(report, Mapping):
        return {
            "status": "UNVERIFIABLE",
            "source_status": None,
            "expected_benchmark_id": expected_benchmark_id,
            "actual_benchmark_id": None,
            "benchmark_match": None if expected_benchmark_id is None else False,
            "validation_errors": ["report is not an object"],
            "reason_codes": [],
            "report": raw,
        }, None
    actual, identity_match = _identity_values(report, expected_benchmark_id)
    return {
        "status": "UNVERIFIABLE",
        "source_status": None,
        "expected_benchmark_id": expected_benchmark_id,
        "actual_benchmark_id": actual,
        "benchmark_match": identity_match,
        "validation_errors": [],
        "reason_codes": [],
        "report": raw,
    }, report


def _alignment_section(
    report: Any,
    expected_benchmark_id: str | None,
    benchmark_report: Any,
    validation_policy: Any,
    validation_config_sha256: str | None,
) -> dict[str, Any]:
    section, payload = _base_child_section(report, expected_benchmark_id)
    if payload is None:
        section["reason_codes"] = ["ALIGNMENT_INPUT_UNVERIFIABLE"]
        return section
    if payload.get("schema_version") != ALIGNMENT_SCHEMA_VERSION:
        section["validation_errors"].append("alignment schema is invalid")
    raw_subsystems = payload.get("subsystems")
    if not isinstance(raw_subsystems, Mapping):
        section["validation_errors"].append("alignment subsystems are missing")
        raw_subsystems = {}

    statuses: dict[str, Any] = {}
    reasons: list[str] = []
    for subsystem in SUBSYSTEMS:
        raw_subsystem = raw_subsystems.get(subsystem)
        status = raw_subsystem.get("status") if isinstance(raw_subsystem, Mapping) else None
        statuses[subsystem] = status
        label = subsystem.upper()
        if status == "UNVERIFIABLE":
            reasons.append(f"ALIGNMENT_{label}_UNVERIFIABLE")
        elif status == "NOT_ALIGNED":
            reasons.append(f"ALIGNMENT_{label}_NOT_ALIGNED")
        elif status != "ALIGNED":
            reasons.append(f"ALIGNMENT_{label}_UNKNOWN_STATUS")

    if any(status not in ALIGNMENT_STATUSES for status in statuses.values()):
        computed_status = "UNVERIFIABLE"
    elif any(status == "UNVERIFIABLE" for status in statuses.values()):
        computed_status = "UNVERIFIABLE"
    elif any(status == "NOT_ALIGNED" for status in statuses.values()):
        computed_status = "NOT_ALIGNED"
    else:
        computed_status = "ALIGNED"
    source_status = payload.get("overall_status")
    section["source_status"] = source_status
    if section["benchmark_match"] is False:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["ALIGNMENT_BENCHMARK_MISMATCH"]
        return section
    if source_status != computed_status:
        section["validation_errors"].append(
            "alignment overall_status is inconsistent with subsystem statuses"
        )
    artifact_audit = validate_alignment_report_artifact(
        payload,
        benchmark_report,
        validation_policy,
        validation_config_sha256=validation_config_sha256,
    )
    section["artifact_audit"] = artifact_audit
    section["validation_errors"].extend(artifact_audit["errors"])
    if section["validation_errors"]:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = [
            *reasons,
            "ALIGNMENT_INPUT_UNVERIFIABLE",
        ]
        return section
    section["status"] = computed_status
    section["reason_codes"] = reasons
    return section


def _uplift_section(
    report: Any,
    expected_benchmark_id: str | None,
    benchmark_report: Any,
    validation_policy: Any,
    validation_config_sha256: str | None,
) -> dict[str, Any]:
    section, payload = _base_child_section(report, expected_benchmark_id)
    if payload is None:
        section["reason_codes"] = ["UPLIFT_INPUT_UNVERIFIABLE"]
        return section
    if payload.get("schema_version") != UPLIFT_SCHEMA_VERSION:
        section["validation_errors"].append("uplift schema is invalid")
    source_status = payload.get("status")
    section["source_status"] = source_status
    if (
        section["benchmark_match"] is False
        and payload.get("schema_version") == UPLIFT_SCHEMA_VERSION
    ):
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["UPLIFT_BENCHMARK_MISMATCH"]
        return section
    artifact_audit = validate_evolution_uplift_report_artifact(
        payload,
        benchmark_report,
        validation_policy,
        validation_config_sha256=validation_config_sha256,
    )
    section["artifact_audit"] = artifact_audit
    section["validation_errors"].extend(artifact_audit["errors"])
    if section["validation_errors"]:
        section["status"] = "UNVERIFIABLE"
        source_reason = (
            "UPLIFT_UNVERIFIABLE"
            if source_status == "UNVERIFIABLE"
            else (
                "UPLIFT_UNKNOWN_STATUS"
                if source_status not in UPLIFT_STATUSES
                else None
            )
        )
        section["reason_codes"] = [
            *([source_reason] if source_reason else []),
            "UPLIFT_INPUT_UNVERIFIABLE",
        ]
        return section
    if source_status not in UPLIFT_STATUSES:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["UPLIFT_UNKNOWN_STATUS"]
        return section
    section["status"] = source_status
    section["reason_codes"] = {
        "UPLIFT_PROVEN": [],
        "NOT_PROVEN": ["UPLIFT_NOT_PROVEN"],
        "UNVERIFIABLE": ["UPLIFT_UNVERIFIABLE"],
    }[source_status]
    return section


def _ledger_section(
    report: Any,
    authoritative_reaudit: Any,
    expected_benchmark_id: str | None,
    validation_config_sha256: str | None,
) -> dict[str, Any]:
    section, payload = _base_child_section(
        authoritative_reaudit, expected_benchmark_id
    )
    section["report"] = _copy_report(report)
    section["authoritative_reaudit"] = _copy_report(authoritative_reaudit)
    experiment_id = payload.get("experiment_id") if payload is not None else None
    registration_verified = (
        payload.get("registration_verified") if payload is not None else None
    )
    section["experiment_id"] = (
        experiment_id if isinstance(experiment_id, str) else None
    )
    section["registration_verified"] = registration_verified
    registration_audit = {
        "experiment_id": section["experiment_id"],
        "experiment_id_valid": _is_experiment_id(experiment_id),
        "registration_verified": registration_verified,
        "expected_benchmark_id": expected_benchmark_id,
        "actual_benchmark_id": section["actual_benchmark_id"],
        "benchmark_match": section["benchmark_match"],
        "mismatches": [],
        "verified": False,
    }
    section["registration_audit"] = registration_audit
    if payload is None:
        section["validation_errors"].append(
            "authoritative ledger re-audit is required"
        )
        section["reason_codes"] = ["LEDGER_INPUT_UNVERIFIABLE"]
        return section
    input_mismatches: list[str] = []
    if not isinstance(report, Mapping):
        input_mismatches.append("ledger input report is not an object")
    else:
        input_actual_benchmark_id, input_benchmark_match = _identity_values(
            report, expected_benchmark_id
        )
        section["actual_benchmark_id"] = input_actual_benchmark_id
        section["benchmark_match"] = input_benchmark_match
        registration_audit["actual_benchmark_id"] = input_actual_benchmark_id
        registration_audit["benchmark_match"] = input_benchmark_match
        for field in LEDGER_REAUDIT_FIELDS:
            if report.get(field) != payload.get(field):
                input_mismatches.append(
                    f"ledger input report differs from authoritative re-audit:{field}"
                )
    section["input_report_mismatches"] = input_mismatches
    section["validation_errors"].extend(input_mismatches)
    if payload.get("schema_version") != LEDGER_SCHEMA_VERSION:
        section["validation_errors"].append("ledger schema is invalid")
    if payload.get("operation") != "audit-next":
        section["validation_errors"].append("ledger report is not audit-next")
    if payload.get("appended") is not False:
        section["validation_errors"].append("audit-next must not append")
    if payload.get("benchmark_verified") is not True:
        section["validation_errors"].append("ledger benchmark is not verified")
    if payload.get("validation_policy_sha256") != validation_config_sha256:
        section["validation_errors"].append(
            "ledger validation policy does not match selected config bytes"
        )
    if payload.get("mismatches") != []:
        section["validation_errors"].append("ledger proposal mismatches are present")
    source_status = payload.get("decision")
    section["source_status"] = source_status
    source_reasons = payload.get("reasons")
    if not isinstance(source_reasons, list) or not all(
        isinstance(reason, str) for reason in source_reasons
    ):
        section["validation_errors"].append("ledger reasons are invalid")
    if section["validation_errors"]:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = [
            "LEDGER_BENCHMARK_MISMATCH"
            if section["benchmark_match"] is False
            else "LEDGER_INPUT_UNVERIFIABLE"
        ]
        return section
    if source_status not in LEDGER_DECISIONS:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["LEDGER_UNKNOWN_STATUS"]
        return section
    if section["benchmark_match"] is False:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["LEDGER_BENCHMARK_MISMATCH"]
        return section
    if source_status == "BLOCK_INVALID_LEDGER":
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["LEDGER_BLOCK_INVALID_LEDGER"]
        return section

    registration_mismatches: list[str] = []
    if registration_verified is not True:
        registration_mismatches.append("registration_verified is not true")
    if not _is_experiment_id(experiment_id):
        registration_mismatches.append("experiment_id is invalid")
    if source_status == "ALLOW_NEXT_EXPERIMENT" and source_reasons:
        registration_mismatches.append(
            "ALLOW_NEXT_EXPERIMENT carries identity or validation reasons"
        )
    for field in (
        "registration_nonce",
        "actual_proposal_sha256",
        "registered_proposal_sha256",
        "registration_record_hash",
        "ledger_tail_record_hash",
        "hypothesis_family_id",
        "information_set_id",
    ):
        if not _is_sha256(payload.get(field)):
            registration_mismatches.append(f"{field} is invalid")
    if payload.get("actual_proposal_sha256") != payload.get(
        "registered_proposal_sha256"
    ):
        registration_mismatches.append(
            "audit-next proposal does not match preregistered proposal"
        )
    result_source_path = payload.get("result_source_path")
    if not isinstance(result_source_path, str) or not pathlib.Path(
        result_source_path
    ).is_absolute():
        registration_mismatches.append("result_source_path is invalid")
    ledger_record_count = payload.get("ledger_record_count")
    if (
        not isinstance(ledger_record_count, int)
        or isinstance(ledger_record_count, bool)
        or ledger_record_count <= 0
    ):
        registration_mismatches.append("ledger_record_count is invalid")
    remaining_budgets = payload.get("remaining_budgets")
    if not isinstance(remaining_budgets, Mapping) or set(remaining_budgets) != {
        "family",
        "information_set",
    } or not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in remaining_budgets.values()
    ):
        registration_mismatches.append("remaining_budgets are invalid")
    else:
        family_remaining = int(remaining_budgets["family"])
        information_remaining = int(remaining_budgets["information_set"])
        if source_status == "ALLOW_NEXT_EXPERIMENT":
            if family_remaining <= 0 or information_remaining <= 0:
                registration_mismatches.append(
                    "ALLOW_NEXT_EXPERIMENT requires both remaining budgets positive"
                )
            if source_reasons != []:
                registration_mismatches.append(
                    "ALLOW_NEXT_EXPERIMENT requires empty canonical reasons"
                )
        elif source_status == "STOP_CURRENT_FAMILY":
            if family_remaining != 0 and information_remaining != 0:
                registration_mismatches.append(
                    "STOP_CURRENT_FAMILY requires an exhausted budget"
                )
            if source_reasons != ["failure budget is exhausted"]:
                registration_mismatches.append(
                    "STOP_CURRENT_FAMILY requires canonical exhausted-budget reason"
                )
    if payload.get("checkpoint_recovery_required") is not False:
        registration_mismatches.append("checkpoint recovery is required")
    registration_audit["mismatches"] = registration_mismatches
    registration_audit["verified"] = not registration_mismatches
    if registration_mismatches:
        section["validation_errors"].extend(registration_mismatches)
        section["status"] = "UNVERIFIABLE"
        reason_codes = []
        if registration_verified is not True:
            reason_codes.append("LEDGER_REGISTRATION_UNVERIFIABLE")
        if not _is_experiment_id(experiment_id):
            reason_codes.append("LEDGER_EXPERIMENT_ID_INVALID")
        if source_status == "ALLOW_NEXT_EXPERIMENT" and source_reasons:
            reason_codes.append("LEDGER_REGISTRATION_IDENTITY_MISMATCH")
        section["reason_codes"] = reason_codes
        return section

    section["status"] = source_status
    section["reason_codes"] = (
        ["LEDGER_STOP_CURRENT_FAMILY"]
        if source_status == "STOP_CURRENT_FAMILY"
        else []
    )
    return section


def _alpha_route_observation(report: Any) -> dict[str, Any]:
    status = report.get("status") if isinstance(report, Mapping) else None
    return {
        "status": status or "UNAVAILABLE",
        "affects_research_decision": False,
        "report": _copy_report(report),
    }


def _has_unverifiable_section(sections: Sequence[Mapping[str, Any]]) -> bool:
    return any(section.get("status") == "UNVERIFIABLE" for section in sections)


def build_report(
    benchmark_report: Any,
    alignment_report: Any,
    uplift_report: Any,
    ledger_report: Any,
    alpha_route_report: Any = None,
    *,
    validation_policy: Any,
    validation_config_sha256: str | None,
    authoritative_ledger_reaudit: Any = None,
) -> dict[str, Any]:
    """Build a deterministic decision without mutating source reports or runtime state."""

    try:
        benchmark, expected_benchmark_id = _benchmark_section(
            benchmark_report,
            validation_policy,
            validation_config_sha256,
        )
    except Exception as exc:
        benchmark = _validator_exception_section(
            "benchmark", benchmark_report, None, exc
        )
        benchmark["benchmark_id"] = None
        expected_benchmark_id = None
    try:
        alignment = _alignment_section(
            alignment_report,
            expected_benchmark_id,
            benchmark_report,
            validation_policy,
            validation_config_sha256,
        )
    except Exception as exc:
        alignment = _validator_exception_section(
            "alignment", alignment_report, expected_benchmark_id, exc
        )
    try:
        uplift = _uplift_section(
            uplift_report,
            expected_benchmark_id,
            benchmark_report,
            validation_policy,
            validation_config_sha256,
        )
    except Exception as exc:
        uplift = _validator_exception_section(
            "uplift", uplift_report, expected_benchmark_id, exc
        )
    try:
        ledger = _ledger_section(
            ledger_report,
            authoritative_ledger_reaudit,
            expected_benchmark_id,
            validation_config_sha256,
        )
    except Exception as exc:
        ledger = _validator_exception_section(
            "ledger", ledger_report, expected_benchmark_id, exc
        )
    sections = (benchmark, alignment, uplift, ledger)
    reason_codes = [
        reason
        for section in sections
        for reason in section.get("reason_codes", [])
    ]
    if benchmark["status"] == "UNVERIFIABLE":
        reason_codes = list(benchmark["reason_codes"])
    if _has_unverifiable_section(sections):
        decision = "STOP"
    elif (
        alignment["status"] == "NOT_ALIGNED"
        or uplift["status"] == "NOT_PROVEN"
        or ledger["status"] == "STOP_CURRENT_FAMILY"
    ):
        decision = "CHANGE_INFORMATION_SET"
    elif (
        benchmark["status"] == "VERIFIED"
        and alignment["status"] == "ALIGNED"
        and uplift["status"] == "UPLIFT_PROVEN"
        and ledger["status"] == "ALLOW_NEXT_EXPERIMENT"
    ):
        decision = "CONTINUE"
    else:
        decision = "STOP"
        reason_codes.append("DECISION_TABLE_UNRECOGNIZED_STATE")
    if decision == "CONTINUE":
        reason_codes = ["DECISIVE_EVIDENCE_ALL_PASSED"]
    result = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark_id": expected_benchmark_id,
        "research_decision": decision,
        "research_decision_only": True,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "reason_codes": reason_codes,
        "benchmark": benchmark,
        "alignment": alignment,
        "uplift": uplift,
        "ledger": ledger,
        "alpha_route_observation": _alpha_route_observation(alpha_route_report),
    }
    if decision == "CONTINUE":
        result["authorized_experiment_id"] = ledger["experiment_id"]
    return result


def _read_report(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"read_error": f"{type(exc).__name__}:{exc}"}


def _write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.tmp.",
            delete=False,
        ) as handle:
            temporary = pathlib.Path(handle.name)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
        temporary.replace(path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-report", required=True)
    parser.add_argument("--alignment-report", required=True)
    parser.add_argument("--uplift-report", required=True)
    parser.add_argument("--ledger-report", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--ledger-proposal", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--alpha-route-report")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = pathlib.Path(args.config)
    validation_policy = _read_report(config_path)
    try:
        validation_config_sha256 = file_sha256(config_path)
    except OSError:
        validation_config_sha256 = None
    benchmark_report = _read_report(pathlib.Path(args.benchmark_report))
    ledger_proposal = _read_report(pathlib.Path(args.ledger_proposal))
    try:
        authoritative_ledger_reaudit = audit_next_experiment(
            args.ledger,
            config_path,
            ledger_proposal,
            benchmark_report,
        )
    except Exception as exc:
        authoritative_ledger_reaudit = {
            "reaudit_error": f"{type(exc).__name__}"
        }
    report = build_report(
        benchmark_report,
        _read_report(pathlib.Path(args.alignment_report)),
        _read_report(pathlib.Path(args.uplift_report)),
        _read_report(pathlib.Path(args.ledger_report)),
        (
            _read_report(pathlib.Path(args.alpha_route_report))
            if args.alpha_route_report
            else None
        ),
        validation_policy=validation_policy,
        validation_config_sha256=validation_config_sha256,
        authoritative_ledger_reaudit=authoritative_ledger_reaudit,
    )
    _write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    if report["research_decision"] == "CONTINUE":
        return 0
    return 2 if report["research_decision"] == "STOP" else 1


if __name__ == "__main__":
    raise SystemExit(main())
