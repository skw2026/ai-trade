#!/usr/bin/env python3
"""Emit a sanitized, public-safe summary of Closed Loop evidence."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import tempfile
from typing import Any, Mapping


DECISIVE_REPORTS = {
    "decision_benchmark_validation": (
        "decision_benchmark_validation.json",
        "identity_status",
        {"VERIFIED", "UNVERIFIABLE"},
        "drifts",
    ),
    "objective_alignment_validation": (
        "objective_alignment_validation.json",
        "overall_status",
        {"ALIGNED", "NOT_ALIGNED", "UNVERIFIABLE"},
        "missing_fields",
    ),
    "paired_evolution_replay": (
        "paired_evolution_replay.json",
        "status",
        {"VERIFIED", "UNVERIFIABLE"},
        "mismatches",
    ),
    "evolution_uplift_validation": (
        "evolution_uplift_validation.json",
        "status",
        {"UPLIFT_PROVEN", "NOT_PROVEN", "UNVERIFIABLE"},
        "missing_evidence",
    ),
    "experiment_budget_audit": (
        "experiment_budget_audit.json",
        "decision",
        {
            "ALLOW_NEXT_EXPERIMENT",
            "STOP_CURRENT_FAMILY",
            "BLOCK_INVALID_LEDGER",
        },
        "reasons",
    ),
    "decision_evidence_report": (
        "decision_evidence_report.json",
        "research_decision",
        {"CONTINUE", "CHANGE_INFORMATION_SET", "STOP"},
        "reason_codes",
    ),
}

_SAFE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SAFE_STEP = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_STEP_RESULTS = {"pass", "fail", "skipped"}
_STEP_KINDS = {"required", "diagnostic", "observation", "route"}


def _read_json(path: pathlib.Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _safe_tokens(values: Any, *, limit: int = 12) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
            continue
        if value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def _drift_tokens(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, Mapping):
            candidates = [
                ".".join(
                    str(value.get(field) or "")
                    for field in ("component", "logical_name", "field")
                ).strip(".")
            ]
        else:
            candidates = []
        for candidate in candidates:
            if _SAFE_TOKEN.fullmatch(candidate) and candidate not in result:
                result.append(candidate)
        if len(result) >= 12:
            break
    return result


def _failed_steps(path: pathlib.Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(record, Mapping) or record.get("result") != "fail":
            continue
        step = record.get("step")
        kind = record.get("kind")
        result_value = record.get("result")
        exit_code = record.get("exit_code")
        blocked = record.get("blocked_by_prior_failure")
        if (
            not isinstance(step, str)
            or not _SAFE_STEP.fullmatch(step)
            or kind not in _STEP_KINDS
            or result_value not in _STEP_RESULTS
            or isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or not isinstance(blocked, bool)
        ):
            continue
        result.append(
            {
                "step": step,
                "kind": kind,
                "result": result_value,
                "exit_code": exit_code,
                "blocked_by_prior_failure": blocked,
            }
        )
    return result[:32]


def build_summary(artifact_dir: pathlib.Path) -> dict[str, Any]:
    download = _read_json(artifact_dir / "closed_loop_download_status.json") or {}
    download_status = download.get("status")
    if download_status not in {"DONE", "SKIPPED", "SKIPPED_OVERLAP"}:
        download_status = "UNAVAILABLE"
    downloaded_count = download.get("downloaded_count")
    invalid_count = download.get("invalid_count")
    summary: dict[str, Any] = {
        "schema_version": "closed_loop_public_failure_summary_v1",
        "download": {
            "status": download_status,
            "downloaded_count": (
                downloaded_count
                if isinstance(downloaded_count, int) and downloaded_count >= 0
                else 0
            ),
            "invalid_count": (
                invalid_count
                if isinstance(invalid_count, int) and invalid_count >= 0
                else 0
            ),
            "missing": _safe_tokens(download.get("missing")),
            "invalid": _safe_tokens(download.get("invalid")),
        },
        "failed_steps": _failed_steps(artifact_dir / "step_status.jsonl"),
        "decisive": {},
        "authorities": {"promotion": False, "demo": False, "live": False},
    }
    for step, (filename, status_field, allowed_statuses, reason_field) in (
        DECISIVE_REPORTS.items()
    ):
        report = _read_json(artifact_dir / filename)
        status = report.get(status_field) if report is not None else None
        section = {
            "artifact": "PRESENT" if report is not None else "MISSING_OR_INVALID",
            "status": status if status in allowed_statuses else "UNAVAILABLE",
            "reason_codes": [],
        }
        if report is not None:
            raw_reasons = report.get(reason_field)
            section["reason_codes"] = (
                _drift_tokens(raw_reasons)
                if reason_field == "drifts"
                else _safe_tokens(raw_reasons)
            )
        summary["decisive"][step] = section

    unified = _read_json(artifact_dir / "decision_evidence_report.json") or {}
    summary["authorities"] = {
        "promotion": unified.get("promotion_authority") is True,
        "demo": unified.get("demo_activation_authorized") is True,
        "live": unified.get("live_activation_authorized") is True,
    }
    return summary


def _write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")
    temporary.replace(path)


def _annotation(summary: Mapping[str, Any]) -> str:
    failed_steps = ",".join(
        str(item["step"]) for item in summary.get("failed_steps", [])
    ) or "none"
    decisive = summary.get("decisive", {})
    statuses = ",".join(
        f"{step}={decisive.get(step, {}).get('status', 'UNAVAILABLE')}"
        for step in DECISIVE_REPORTS
    )
    reasons = ",".join(
        reason
        for step in DECISIVE_REPORTS
        for reason in decisive.get(step, {}).get("reason_codes", [])
    ) or "none"
    return f"failed_steps={failed_steps}; decisive={statuses}; reasons={reasons}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-d", "--artifact-dir", required=True)
    parser.add_argument("-a", "--emit-annotation", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifact_dir = pathlib.Path(args.artifact_dir)
    summary = build_summary(artifact_dir)
    _write_json(artifact_dir / "closed_loop_public_summary.json", summary)
    rendered = json.dumps(summary, ensure_ascii=True, sort_keys=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with pathlib.Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write("## Closed Loop evidence summary\n\n")
            handle.write(f"```json\n{rendered}\n```\n")
    if args.emit_annotation:
        level = "error" if summary["failed_steps"] else "notice"
        print(f"::{level} title=Closed Loop evidence summary::{_annotation(summary)}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
