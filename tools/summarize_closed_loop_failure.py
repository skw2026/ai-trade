#!/usr/bin/env python3
"""Emit a sanitized, public-safe summary of Closed Loop evidence."""

from __future__ import annotations

import argparse
import json
import math
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

UPSTREAM_REPORTS = {
    "market_alpha_development": (
        "market_alpha_development_report.json",
        {"PASS", "FAIL", "NOT_READY"},
    ),
    "liquidation_information_set_experiment": (
        "liquidation_information_set_experiment.json",
        {"COMPLETE", "NOT_READY", "INVALID_INPUT"},
    ),
    "microstructure_alpha_development": (
        "microstructure_alpha_development_report.json",
        {"PASS", "FAIL", "NOT_READY"},
    ),
    "microstructure_regime_evidence": (
        "microstructure_alpha_regime_evidence_audit.json",
        {
            "RECORDED",
            "DUPLICATE",
            "SKIPPED_OVERLAP",
            "COLLECTING",
            "STAGE_REVIEW_REQUIRED",
            "UNVERIFIABLE",
        },
    ),
    "microstructure_alpha_lifecycle": (
        "microstructure_alpha_lifecycle_report.json",
        {"PASS", "FAIL", "NOT_READY"},
    ),
    "alpha_source_route": (
        "alpha_source_route_report.json",
        {"PASS", "FAIL", "NOT_READY"},
    ),
    "decision_benchmark_build": (
        "decision_benchmark_build_report.json",
        {"VERIFIED", "UNVERIFIABLE"},
    ),
    "decision_candidate_preflight": (
        "decision_candidate_preflight_report.json",
        {"VERIFIED", "UNVERIFIABLE"},
    ),
}


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


def _append_safe_token(result: list[str], value: Any, *, limit: int = 12) -> None:
    if (
        len(result) < limit
        and isinstance(value, str)
        and _SAFE_TOKEN.fullmatch(value)
        and value not in result
    ):
        result.append(value)


def _safe_number(value: Any) -> int | float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        return value
    return None


def _market_alpha_diagnostics(
    report: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    reasons: list[str] = []
    gates = report.get("data_gates")
    if isinstance(gates, Mapping):
        for name in (
            "cross_market_cross_asset_history",
            "bybit_trade_archive_sample",
        ):
            if gates.get(name) != "PASS":
                _append_safe_token(reasons, f"data_gate.{name}")
    economic = report.get("economic_screen")
    if (
        isinstance(economic, Mapping)
        and economic.get("development_passed") is not True
    ):
        _append_safe_token(reasons, "economic_screen.no_variant_passed")
    return reasons, {}


def _microstructure_alpha_diagnostics(
    report: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    reasons = _safe_tokens(report.get("failures"))
    economic = report.get("economic_screen")
    if not isinstance(economic, Mapping):
        return reasons, {}

    trained = _safe_number(economic.get("trained_split_count"))
    required = _safe_number(economic.get("required_split_count"))
    if trained is None or required is None or trained < required:
        _append_safe_token(reasons, "economic_screen.insufficient_trained_splits")

    base_trade = economic.get("oos_base_cost_by_trade")
    trade_count = (
        _safe_number(base_trade.get("count"))
        if isinstance(base_trade, Mapping)
        else None
    )
    minimum_trades = _safe_number(economic.get("minimum_oos_trades"))
    if trade_count is None or minimum_trades is None or trade_count < minimum_trades:
        _append_safe_token(reasons, "economic_screen.minimum_oos_trades")

    positive_ratio = _safe_number(economic.get("positive_base_edge_split_ratio"))
    minimum_ratio = _safe_number(economic.get("minimum_positive_splits_ratio"))
    if (
        positive_ratio is None
        or minimum_ratio is None
        or positive_ratio < minimum_ratio
    ):
        _append_safe_token(reasons, "economic_screen.minimum_positive_splits_ratio")

    consensus = _safe_number(economic.get("action_consensus_ratio"))
    minimum_consensus = _safe_number(economic.get("minimum_action_consensus_ratio"))
    if (
        consensus is None
        or minimum_consensus is None
        or consensus < minimum_consensus
    ):
        _append_safe_token(reasons, "economic_screen.minimum_action_consensus_ratio")

    base_split = economic.get("oos_base_cost_by_split")
    stress_split = economic.get("oos_stress_cost_by_split")
    base_lcb = (
        _safe_number(base_split.get("lcb_bps"))
        if isinstance(base_split, Mapping)
        else None
    )
    stress_lcb = (
        _safe_number(stress_split.get("lcb_bps"))
        if isinstance(stress_split, Mapping)
        else None
    )
    if base_lcb is None or base_lcb <= 0:
        _append_safe_token(reasons, "economic_screen.base_split_lcb_not_positive")
    if stress_lcb is None or stress_lcb <= 0:
        _append_safe_token(reasons, "economic_screen.stress_split_lcb_not_positive")
    if economic.get("prediction_permutation_control_passed") is not True:
        _append_safe_token(reasons, "economic_screen.permutation_control_failed")

    metrics = {
        "oos_trade_count": trade_count,
        "base_split_lcb_bps": base_lcb,
        "stress_split_lcb_bps": stress_lcb,
        "positive_split_ratio": positive_ratio,
        "action_consensus_ratio": consensus,
    }
    return reasons, {key: value for key, value in metrics.items() if value is not None}


def _upstream_section(name: str, report: Mapping[str, Any] | None) -> dict[str, Any]:
    allowed_statuses = UPSTREAM_REPORTS[name][1]
    status = report.get("status") if report is not None else None
    section: dict[str, Any] = {
        "artifact": "PRESENT" if report is not None else "MISSING_OR_INVALID",
        "status": status if status in allowed_statuses else "UNAVAILABLE",
        "reason_codes": [],
    }
    if report is None:
        return section

    reasons: list[str] = []
    metrics: dict[str, Any] = {}
    if name == "market_alpha_development":
        reasons, metrics = _market_alpha_diagnostics(report)
        section["gate_status"] = (
            "READY"
            if report.get("fully_verifiable") is True
            and isinstance(report.get("economic_screen"), Mapping)
            and report["economic_screen"].get("development_passed") is True
            else "REJECTED"
        )
    elif name == "liquidation_information_set_experiment":
        reasons = _safe_tokens(report.get("reason_codes"))
        allowed_decisions = {
            "STOP_CURRENT_RESEARCH_FAMILY",
            "STOP_INFORMATION_SOURCE",
            "CONTINUE_TO_SECOND_INDEPENDENT_24H",
        }
        research_decision = report.get("research_decision")
        contract_ok = (
            report.get("schema_version")
            == "liquidation_information_set_experiment_v1"
            and report.get("status") == "COMPLETE"
            and report.get("fully_verifiable") is True
            and report.get("research_domain") == "forward_development_only"
            and report.get("promotion_evidence") is False
            and report.get("promotion_eligible") is False
            and report.get("promotion_authority") is False
            and report.get("demo_activation_authorized") is False
            and report.get("live_activation_authorized") is False
            and research_decision in allowed_decisions
        )
        section["gate_status"] = "COMPLETE" if contract_ok else "NOT_READY"
        section["research_decision"] = (
            research_decision if research_decision in allowed_decisions else None
        )
        section["research_observation_only"] = True
        section["promotion_authority"] = False
        section["demo_activation_authorized"] = False
        section["live_activation_authorized"] = False

        common = report.get("common_domain")
        hindsight = report.get("hindsight_oracle")
        arms = report.get("arms")
        treatment = arms.get("treatment") if isinstance(arms, Mapping) else None
        aggregate = (
            treatment.get("aggregate") if isinstance(treatment, Mapping) else None
        )
        architectures = (
            aggregate.get("architectures")
            if isinstance(aggregate, Mapping)
            else None
        )
        direct = (
            architectures.get("direct_stress_utility_regression")
            if isinstance(architectures, Mapping)
            else None
        )
        paired = report.get("paired_treatment_minus_control")
        oracle_stress = (
            hindsight.get("stress_cost_by_split")
            if isinstance(hindsight, Mapping)
            else None
        )
        treatment_stress = (
            direct.get("oos_stress_cost_by_split")
            if isinstance(direct, Mapping)
            else None
        )
        paired_stress = (
            paired.get("stress_cost_delta_by_split")
            if isinstance(paired, Mapping)
            else None
        )
        permutation = (
            paired.get("permutation_null") if isinstance(paired, Mapping) else None
        )
        metrics = {
            "common_row_count": (
                _safe_number(common.get("row_count"))
                if isinstance(common, Mapping)
                else None
            ),
            "oracle_stress_lcb_bps": (
                _safe_number(oracle_stress.get("lcb_bps"))
                if isinstance(oracle_stress, Mapping)
                else None
            ),
            "treatment_trade_count": (
                _safe_number(direct.get("trade_count"))
                if isinstance(direct, Mapping)
                else None
            ),
            "treatment_stress_lcb_bps": (
                _safe_number(treatment_stress.get("lcb_bps"))
                if isinstance(treatment_stress, Mapping)
                else None
            ),
            "paired_delta_stress_lcb_bps": (
                _safe_number(paired_stress.get("lcb_bps"))
                if isinstance(paired_stress, Mapping)
                else None
            ),
            "paired_permutation_passed": (
                permutation.get("passed")
                if isinstance(permutation, Mapping)
                and isinstance(permutation.get("passed"), bool)
                else None
            ),
        }
        section["metrics"] = {
            key: value for key, value in metrics.items() if value is not None
        }
    elif name == "microstructure_alpha_development":
        reasons, metrics = _microstructure_alpha_diagnostics(report)
        section["gate_status"] = (
            "READY"
            if report.get("fully_verifiable") is True
            and isinstance(report.get("economic_screen"), Mapping)
            and report["economic_screen"].get("development_passed") is True
            else "REJECTED"
        )
    elif name == "microstructure_regime_evidence":
        reasons = _safe_tokens(report.get("reason_codes"))
        accepted_count = report.get("accepted_batch_count")
        independent_hours = report.get("independent_oos_hours")
        section["accepted_batch_count"] = (
            accepted_count
            if isinstance(accepted_count, int)
            and not isinstance(accepted_count, bool)
            and accepted_count >= 0
            else 0
        )
        section["independent_oos_hours"] = (
            independent_hours
            if isinstance(independent_hours, (int, float))
            and not isinstance(independent_hours, bool)
            and math.isfinite(float(independent_hours))
            and independent_hours >= 0
            else 0.0
        )
        section["research_observation_only"] = (
            report.get("research_observation_only") is True
        )
        section["promotion_authority"] = report.get("promotion_authority") is True
        section["demo_activation_authorized"] = (
            report.get("demo_activation_authorized") is True
        )
        section["live_activation_authorized"] = (
            report.get("live_activation_authorized") is True
        )
        section["stage_review_required"] = (
            report.get("stage_review_required") is True
        )
        next_action = report.get("next_action")
        section["next_action"] = (
            next_action
            if isinstance(next_action, str) and _SAFE_TOKEN.fullmatch(next_action)
            else None
        )
    elif name == "microstructure_alpha_lifecycle":
        _append_safe_token(reasons, report.get("not_ready_reason"))
        for token in _safe_tokens(report.get("failures")):
            _append_safe_token(reasons, token)
        phase = report.get("phase")
        if isinstance(phase, str) and _SAFE_TOKEN.fullmatch(phase):
            section["phase"] = phase
        section["demo_entry_eligible"] = report.get("demo_entry_eligible") is True
        section["live_promotion_eligible"] = (
            report.get("live_promotion_eligible") is True
        )
    elif name == "alpha_source_route":
        _append_safe_token(reasons, report.get("reason"))
        selected_route = report.get("selected_route")
        section["selected_route"] = (
            selected_route
            if selected_route in {"microstructure_demo", "legacy_integrator"}
            else None
        )
        sources = report.get("sources")
        if isinstance(sources, Mapping):
            section["source_readiness"] = {
                source: (
                    details.get("readiness")
                    if isinstance(details, Mapping)
                    and details.get("readiness") in {"READY", "NOT_READY", "REJECTED"}
                    else "UNAVAILABLE"
                )
                for source in ("legacy_integrator", "microstructure_demo")
                for details in [sources.get(source)]
            }
    elif name == "decision_benchmark_build":
        reasons = _safe_tokens(report.get("errors"))
        preflight = report.get("candidate_preflight")
        if isinstance(preflight, Mapping):
            for token in _safe_tokens(preflight.get("errors")):
                _append_safe_token(reasons, token)
    elif name == "decision_candidate_preflight":
        reasons = _safe_tokens(report.get("errors"))

    section["reason_codes"] = reasons
    if metrics:
        section["metrics"] = metrics
    return section


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
        "upstream": {},
        "decisive": {},
        "authorities": {"promotion": False, "demo": False, "live": False},
    }
    for name, (filename, _) in UPSTREAM_REPORTS.items():
        summary["upstream"][name] = _upstream_section(
            name, _read_json(artifact_dir / filename)
        )
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
    )
    reasons = ",".join(reasons.split(",")[:16]) or "none"
    upstream = summary.get("upstream", {})
    upstream_statuses = ",".join(
        f"{name}="
        f"{upstream.get(name, {}).get('gate_status', upstream.get(name, {}).get('status', 'UNAVAILABLE'))}"
        for name in UPSTREAM_REPORTS
    )
    upstream_reasons = ",".join(
        reason
        for name in UPSTREAM_REPORTS
        for reason in upstream.get(name, {}).get("reason_codes", [])
    )
    upstream_reasons = ",".join(upstream_reasons.split(",")[:20]) or "none"
    information_set_decision = (
        upstream.get("liquidation_information_set_experiment", {}).get(
            "research_decision", "UNAVAILABLE"
        )
        or "UNAVAILABLE"
    )
    return (
        f"failed_steps={failed_steps}; upstream={upstream_statuses}; "
        f"information_set_decision={information_set_decision}; "
        f"upstream_reasons={upstream_reasons}; decisive={statuses}; reasons={reasons}"
    )


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
