#!/usr/bin/env python3
"""Build a fail-closed research decision from three independent evidence reports."""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any


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


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _copy_report(report: Any) -> Any:
    return copy.deepcopy(report)


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


def _benchmark_section(report: Any) -> tuple[dict[str, Any], str | None]:
    raw = _copy_report(report)
    errors: list[str] = []
    actual_id = report.get("benchmark_id") if isinstance(report, Mapping) else None
    if not isinstance(report, Mapping):
        errors.append("benchmark report is not an object")
    else:
        if report.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
            errors.append("benchmark schema is invalid")
        if report.get("identity_status") != "VERIFIED":
            errors.append("benchmark identity is not VERIFIED")
        if not _is_sha256(actual_id):
            errors.append("benchmark_id is invalid")
        if report.get("drifts") not in (None, []):
            errors.append("benchmark drift is present")
    verified = not errors
    section = {
        "status": "VERIFIED" if verified else "UNVERIFIABLE",
        "benchmark_id": actual_id if isinstance(actual_id, str) else None,
        "validation_errors": errors,
        "reason_codes": [] if verified else ["BENCHMARK_UNVERIFIABLE"],
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
    report: Any, expected_benchmark_id: str | None
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
    if source_status != computed_status:
        section["validation_errors"].append(
            "alignment overall_status is inconsistent with subsystem statuses"
        )
    if section["validation_errors"]:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["ALIGNMENT_INPUT_UNVERIFIABLE"]
        return section
    if section["benchmark_match"] is False:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["ALIGNMENT_BENCHMARK_MISMATCH"]
        return section
    section["status"] = computed_status
    section["reason_codes"] = reasons
    return section


def _uplift_section(
    report: Any, expected_benchmark_id: str | None
) -> dict[str, Any]:
    section, payload = _base_child_section(report, expected_benchmark_id)
    if payload is None:
        section["reason_codes"] = ["UPLIFT_INPUT_UNVERIFIABLE"]
        return section
    if payload.get("schema_version") != UPLIFT_SCHEMA_VERSION:
        section["validation_errors"].append("uplift schema is invalid")
    source_status = payload.get("status")
    section["source_status"] = source_status
    if section["validation_errors"]:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["UPLIFT_INPUT_UNVERIFIABLE"]
        return section
    if source_status not in UPLIFT_STATUSES:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["UPLIFT_UNKNOWN_STATUS"]
        return section
    if section["benchmark_match"] is False:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["UPLIFT_BENCHMARK_MISMATCH"]
        return section
    section["status"] = source_status
    section["reason_codes"] = {
        "UPLIFT_PROVEN": [],
        "NOT_PROVEN": ["UPLIFT_NOT_PROVEN"],
        "UNVERIFIABLE": ["UPLIFT_UNVERIFIABLE"],
    }[source_status]
    return section


def _ledger_section(
    report: Any, expected_benchmark_id: str | None
) -> dict[str, Any]:
    section, payload = _base_child_section(report, expected_benchmark_id)
    if payload is None:
        section["reason_codes"] = ["LEDGER_INPUT_UNVERIFIABLE"]
        return section
    if payload.get("schema_version") != LEDGER_SCHEMA_VERSION:
        section["validation_errors"].append("ledger schema is invalid")
    if payload.get("operation") != "audit-next":
        section["validation_errors"].append("ledger report is not audit-next")
    if payload.get("appended") is not False:
        section["validation_errors"].append("audit-next must not append")
    source_status = payload.get("decision")
    section["source_status"] = source_status
    if section["validation_errors"]:
        section["status"] = "UNVERIFIABLE"
        section["reason_codes"] = ["LEDGER_INPUT_UNVERIFIABLE"]
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
    else:
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
) -> dict[str, Any]:
    """Build a deterministic decision without mutating source reports or runtime state."""

    benchmark, expected_benchmark_id = _benchmark_section(benchmark_report)
    alignment = _alignment_section(alignment_report, expected_benchmark_id)
    uplift = _uplift_section(uplift_report, expected_benchmark_id)
    ledger = _ledger_section(ledger_report, expected_benchmark_id)
    sections = (benchmark, alignment, uplift, ledger)
    reason_codes = [
        reason
        for section in sections
        for reason in section.get("reason_codes", [])
    ]
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
    return {
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


def _read_report(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"read_error": f"{type(exc).__name__}:{exc}"}


def _write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-report", required=True)
    parser.add_argument("--alignment-report", required=True)
    parser.add_argument("--uplift-report", required=True)
    parser.add_argument("--ledger-report", required=True)
    parser.add_argument("--alpha-route-report")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(
        _read_report(pathlib.Path(args.benchmark_report)),
        _read_report(pathlib.Path(args.alignment_report)),
        _read_report(pathlib.Path(args.uplift_report)),
        _read_report(pathlib.Path(args.ledger_report)),
        (
            _read_report(pathlib.Path(args.alpha_route_report))
            if args.alpha_route_report
            else None
        ),
    )
    _write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    if report["research_decision"] == "CONTINUE":
        return 0
    return 2 if report["research_decision"] == "STOP" else 1


if __name__ == "__main__":
    raise SystemExit(main())
