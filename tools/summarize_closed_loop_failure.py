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
_RUNNER_ERROR_MARKERS = (
    "[ERROR]",
    "error",
    "failed",
    "exception",
    "traceback",
    "cannot",
    "unbound variable",
    "syntax error",
    "identity mismatch",
    "missing report:",
    "command not found",
    "permission denied",
    "no such file or directory",
)
_RUNNER_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|token)(\s*[:=]\s*)(\S+)"
)

UPSTREAM_REPORTS = {
    "market_alpha_development": (
        "market_alpha_development_report.json",
        {"PASS", "FAIL", "NOT_READY"},
    ),
    "liquidation_information_set_experiment": (
        "liquidation_information_set_experiment.json",
        {"COMPLETE", "NOT_READY", "INVALID_INPUT"},
    ),
    "maker_execution_opportunity_experiment": (
        "maker_execution_opportunity_experiment.json",
        {"COMPLETE", "NOT_READY"},
    ),
    "maker_execution_learnability_experiment": (
        "maker_execution_learnability_experiment.json",
        {"COMPLETE", "NOT_READY"},
    ),
    "maker_subsecond_information_experiment": (
        "maker_subsecond_information_experiment.json",
        {"COMPLETE", "NOT_READY"},
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


def _sanitize_runner_line(raw_line: str) -> str:
    line = _RUNNER_SECRET_PATTERN.sub(r"\1\2<redacted>", raw_line)
    line = re.sub(r"https?://\S+", "<url>", line, flags=re.IGNORECASE)
    line = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s|]+)", "<path>", line)
    line = re.sub(r"\b[0-9a-f]{32,}\b", "<digest>", line, flags=re.IGNORECASE)
    line = re.sub(r"\b[A-Za-z0-9_+/=-]{48,}\b", "<opaque>", line)
    line = re.sub(r"[\x00-\x1f\x7f]+", " ", line).replace("::", ":")
    return " ".join(line.split())[:320]


def _runner_log_lines(path: pathlib.Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _runner_error_lines(path: pathlib.Path) -> list[str]:
    result: list[str] = []
    for raw_line in reversed(_runner_log_lines(path)):
        lowered = raw_line.lower()
        if not any(marker.lower() in lowered for marker in _RUNNER_ERROR_MARKERS):
            continue
        line = _sanitize_runner_line(raw_line)
        if line and line not in result:
            result.append(line)
        if len(result) >= 8:
            break
    return list(reversed(result))


def _runner_tail_lines(path: pathlib.Path) -> list[str]:
    result: list[str] = []
    for raw_line in reversed(_runner_log_lines(path)):
        line = _sanitize_runner_line(raw_line)
        if line and line not in result:
            result.append(line)
        if len(result) >= 8:
            break
    return list(reversed(result))


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

        not_ready_stage = report.get("not_ready_stage")
        if not_ready_stage in {
            "control_capture",
            "liquidation_capture",
            "common_causal_domain",
            "experiment_input",
            "invalid_input",
        }:
            section["not_ready_stage"] = not_ready_stage

        common = report.get("common_domain")
        hindsight = report.get("hindsight_oracle")
        arms = report.get("arms")
        control = arms.get("control") if isinstance(arms, Mapping) else None
        treatment = arms.get("treatment") if isinstance(arms, Mapping) else None

        def direct_architecture(arm: Any) -> Mapping[str, Any] | None:
            aggregate = arm.get("aggregate") if isinstance(arm, Mapping) else None
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
            return direct if isinstance(direct, Mapping) else None

        control_direct = direct_architecture(control)
        treatment_direct = direct_architecture(treatment)
        paired = report.get("paired_treatment_minus_control")
        oracle_base = (
            hindsight.get("base_cost_by_split")
            if isinstance(hindsight, Mapping)
            else None
        )
        oracle_stress = (
            hindsight.get("stress_cost_by_split")
            if isinstance(hindsight, Mapping)
            else None
        )
        control_base = (
            control_direct.get("oos_base_cost_by_split")
            if isinstance(control_direct, Mapping)
            else None
        )
        control_stress = (
            control_direct.get("oos_stress_cost_by_split")
            if isinstance(control_direct, Mapping)
            else None
        )
        control_permutation = (
            control_direct.get("prediction_permutation_control")
            if isinstance(control_direct, Mapping)
            else None
        )
        treatment_base = (
            treatment_direct.get("oos_base_cost_by_split")
            if isinstance(treatment_direct, Mapping)
            else None
        )
        treatment_stress = (
            treatment_direct.get("oos_stress_cost_by_split")
            if isinstance(treatment_direct, Mapping)
            else None
        )
        treatment_permutation = (
            treatment_direct.get("prediction_permutation_control")
            if isinstance(treatment_direct, Mapping)
            else None
        )
        paired_base = (
            paired.get("base_cost_delta_by_split")
            if isinstance(paired, Mapping)
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
            "oracle_trade_count": (
                _safe_number(hindsight.get("trade_count"))
                if isinstance(hindsight, Mapping)
                else None
            ),
            "oracle_positive_split_ratio": (
                _safe_number(hindsight.get("positive_stress_split_ratio"))
                if isinstance(hindsight, Mapping)
                else None
            ),
            "oracle_base_lcb_bps": (
                _safe_number(oracle_base.get("lcb_bps"))
                if isinstance(oracle_base, Mapping)
                else None
            ),
            "oracle_stress_lcb_bps": (
                _safe_number(oracle_stress.get("lcb_bps"))
                if isinstance(oracle_stress, Mapping)
                else None
            ),
            "control_trade_count": (
                _safe_number(control_direct.get("trade_count"))
                if isinstance(control_direct, Mapping)
                else None
            ),
            "control_positive_split_ratio": (
                _safe_number(control_stress.get("positive_ratio"))
                if isinstance(control_stress, Mapping)
                else None
            ),
            "control_base_lcb_bps": (
                _safe_number(control_base.get("lcb_bps"))
                if isinstance(control_base, Mapping)
                else None
            ),
            "control_stress_lcb_bps": (
                _safe_number(control_stress.get("lcb_bps"))
                if isinstance(control_stress, Mapping)
                else None
            ),
            "control_permutation_passed": (
                control_permutation.get("passed")
                if isinstance(control_permutation, Mapping)
                and isinstance(control_permutation.get("passed"), bool)
                else None
            ),
            "treatment_trade_count": (
                _safe_number(treatment_direct.get("trade_count"))
                if isinstance(treatment_direct, Mapping)
                else None
            ),
            "treatment_positive_split_ratio": (
                _safe_number(treatment_stress.get("positive_ratio"))
                if isinstance(treatment_stress, Mapping)
                else None
            ),
            "treatment_base_lcb_bps": (
                _safe_number(treatment_base.get("lcb_bps"))
                if isinstance(treatment_base, Mapping)
                else None
            ),
            "treatment_stress_lcb_bps": (
                _safe_number(treatment_stress.get("lcb_bps"))
                if isinstance(treatment_stress, Mapping)
                else None
            ),
            "treatment_permutation_passed": (
                treatment_permutation.get("passed")
                if isinstance(treatment_permutation, Mapping)
                and isinstance(treatment_permutation.get("passed"), bool)
                else None
            ),
            "paired_delta_base_lcb_bps": (
                _safe_number(paired_base.get("lcb_bps"))
                if isinstance(paired_base, Mapping)
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
        capture_readiness = report.get("capture_readiness")
        control_progress = (
            capture_readiness.get("control")
            if isinstance(capture_readiness, Mapping)
            else None
        )
        liquidation_progress = (
            capture_readiness.get("liquidation")
            if isinstance(capture_readiness, Mapping)
            else None
        )
        if isinstance(control_progress, Mapping):
            control_status = control_progress.get("status")
            if control_status in {"PASS", "FAIL", "NOT_READY"}:
                section["control_capture_status"] = control_status
        if isinstance(liquidation_progress, Mapping):
            liquidation_status = liquidation_progress.get("status")
            if liquidation_status in {"PASS", "FAIL", "NOT_READY"}:
                section["liquidation_capture_status"] = liquidation_status
            for source, target, divisor in (
                ("coverage_ms", "liquidation_coverage_seconds", 1000.0),
                ("minimum_coverage_ms", "liquidation_minimum_coverage_seconds", 1000.0),
                ("missing_coverage_ms", "liquidation_missing_coverage_seconds", 1000.0),
                ("coverage_ratio", "liquidation_coverage_ratio", 1.0),
                ("freshness_age_ms", "liquidation_freshness_seconds", 1000.0),
                ("feature_row_count", "liquidation_feature_row_count", 1.0),
                ("liquidation_event_count", "liquidation_event_count", 1.0),
                ("report_file_count", "liquidation_report_file_count", 1.0),
                ("valid_segment_count", "liquidation_valid_segment_count", 1.0),
                ("invalid_segment_count", "liquidation_invalid_segment_count", 1.0),
            ):
                value = _safe_number(liquidation_progress.get(source))
                if value is not None:
                    metrics[target] = value / divisor
        required_span = _safe_number(
            report.get("minimum_common_span_seconds_for_frozen_splits")
        )
        if required_span is not None:
            metrics["minimum_common_span_seconds"] = required_span
        section["metrics"] = {
            key: value for key, value in metrics.items() if value is not None
        }
    elif name == "maker_execution_opportunity_experiment":
        reasons = _safe_tokens(report.get("reason_codes"))
        decision = report.get("research_decision")
        allowed_decisions = {
            "CONTINUE_TO_MAKER_LEARNABILITY_EXPERIMENT",
            "STOP_MAKER_EXECUTION_FAMILY",
            "WAIT_FOR_INDEPENDENT_MAKER_FORWARD_WINDOW",
        }
        contract_ok = (
            report.get("schema_version")
            == "maker_execution_opportunity_experiment_v1"
            and report.get("status") == "COMPLETE"
            and isinstance(report.get("fully_verifiable"), bool)
            and report.get("research_domain") == "forward_development_only"
            and report.get("promotion_evidence") is False
            and report.get("promotion_eligible") is False
            and report.get("promotion_authority") is False
            and report.get("demo_activation_authorized") is False
            and report.get("live_activation_authorized") is False
            and decision in allowed_decisions
            and (
                decision != "CONTINUE_TO_MAKER_LEARNABILITY_EXPERIMENT"
                or report.get("fully_verifiable") is True
            )
        )
        section["gate_status"] = "COMPLETE" if contract_ok else "NOT_READY"
        section["research_decision"] = (
            decision if decision in allowed_decisions else None
        )
        section["research_observation_only"] = True
        section["promotion_authority"] = False
        section["demo_activation_authorized"] = False
        section["live_activation_authorized"] = False
        common = report.get("common_domain")
        fill = report.get("fill_audit")
        oracle = report.get("hindsight_oracle")
        base = (
            oracle.get("base_cost_by_split")
            if isinstance(oracle, Mapping)
            else None
        )
        stress = (
            oracle.get("stress_cost_by_split")
            if isinstance(oracle, Mapping)
            else None
        )
        stability = report.get("stability_audit")
        boundary = (
            stability.get("boundary_sensitivity")
            if isinstance(stability, Mapping)
            else None
        )
        forward = (
            stability.get("independent_forward")
            if isinstance(stability, Mapping)
            else None
        )
        metrics = {
            "common_row_count": (
                _safe_number(common.get("row_count"))
                if isinstance(common, Mapping)
                else None
            ),
            "filled_decision_count": (
                _safe_number(fill.get("filled_decision_count"))
                if isinstance(fill, Mapping)
                else None
            ),
            "filled_action_count": (
                _safe_number(fill.get("filled_action_count"))
                if isinstance(fill, Mapping)
                else None
            ),
            "oracle_trade_count": (
                _safe_number(oracle.get("trade_count"))
                if isinstance(oracle, Mapping)
                else None
            ),
            "oracle_positive_split_ratio": (
                _safe_number(oracle.get("positive_stress_split_ratio"))
                if isinstance(oracle, Mapping)
                else None
            ),
            "oracle_base_lcb_bps": (
                _safe_number(base.get("lcb_bps"))
                if isinstance(base, Mapping)
                else None
            ),
            "oracle_stress_lcb_bps": (
                _safe_number(stress.get("lcb_bps"))
                if isinstance(stress, Mapping)
                else None
            ),
            "boundary_pass_ratio": (
                _safe_number(boundary.get("pass_ratio"))
                if isinstance(boundary, Mapping)
                else None
            ),
            "forward_row_ratio": (
                _safe_number(forward.get("row_ratio"))
                if isinstance(forward, Mapping)
                else None
            ),
            "forward_observation_complete": (
                forward.get("observation_complete")
                if isinstance(forward, Mapping)
                and isinstance(forward.get("observation_complete"), bool)
                else None
            ),
        }
        section["metrics"] = {
            key: value for key, value in metrics.items() if value is not None
        }
    elif name == "maker_execution_learnability_experiment":
        reasons = _safe_tokens(report.get("reason_codes"))
        decision = report.get("research_decision")
        allowed_decisions = {
            "CONTINUE_TO_INDEPENDENT_MAKER_FORWARD_VALIDATION",
            "STOP_MAKER_LEARNABILITY_FAMILY",
            "STOP_MAKER_LEARNABILITY_UPSTREAM_NOT_PROVEN",
        }
        leader = report.get("diagnostic_leader_id")
        allowed_leaders = {"sequential_hurdle_tail_action_value"}
        contract_ok = (
            report.get("schema_version")
            == "maker_execution_learnability_experiment_v1"
            and report.get("status") == "COMPLETE"
            and report.get("fully_verifiable") is True
            and report.get("research_domain") == "forward_development_only"
            and report.get("promotion_evidence") is False
            and report.get("promotion_eligible") is False
            and report.get("promotion_authority") is False
            and report.get("demo_activation_authorized") is False
            and report.get("live_activation_authorized") is False
            and report.get("diagnostic_leader_is_preregistered") is False
            and decision in allowed_decisions
            and (leader is None or leader in allowed_leaders)
        )
        section["gate_status"] = "COMPLETE" if contract_ok else "NOT_READY"
        section["research_decision"] = (
            decision if decision in allowed_decisions else None
        )
        section["diagnostic_leader_id"] = (
            leader if leader in allowed_leaders else None
        )
        section["research_observation_only"] = True
        section["promotion_authority"] = False
        section["demo_activation_authorized"] = False
        section["live_activation_authorized"] = False
        data = report.get("data")
        comparison = report.get("architecture_comparison")
        architectures = (
            comparison.get("architectures")
            if isinstance(comparison, Mapping)
            else None
        )
        metrics = {
            "eligible_row_count": (
                _safe_number(data.get("eligible_row_count"))
                if isinstance(data, Mapping)
                else None
            )
        }
        for architecture_id, prefix in (
            ("sequential_hurdle_tail_action_value", "hurdle_tail"),
        ):
            architecture = (
                architectures.get(architecture_id)
                if isinstance(architectures, Mapping)
                else None
            )
            base = (
                architecture.get("oos_base_cost_by_split")
                if isinstance(architecture, Mapping)
                else None
            )
            stress = (
                architecture.get("oos_stress_cost_by_split")
                if isinstance(architecture, Mapping)
                else None
            )
            control = (
                architecture.get("prediction_permutation_control")
                if isinstance(architecture, Mapping)
                else None
            )
            metrics[f"{prefix}_trade_count"] = (
                _safe_number(architecture.get("trade_count"))
                if isinstance(architecture, Mapping)
                else None
            )
            metrics[f"{prefix}_positive_split_ratio"] = (
                _safe_number(architecture.get("positive_stress_split_ratio"))
                if isinstance(architecture, Mapping)
                else None
            )
            metrics[f"{prefix}_base_lcb_bps"] = (
                _safe_number(base.get("lcb_bps"))
                if isinstance(base, Mapping)
                else None
            )
            metrics[f"{prefix}_stress_lcb_bps"] = (
                _safe_number(stress.get("lcb_bps"))
                if isinstance(stress, Mapping)
                else None
            )
            metrics[f"{prefix}_permutation_passed"] = (
                control.get("passed")
                if isinstance(control, Mapping)
                and isinstance(control.get("passed"), bool)
                else None
            )
            metrics[f"{prefix}_maker_gate_passed"] = (
                architecture.get("maker_decision_gate_passed")
                if isinstance(architecture, Mapping)
                and isinstance(architecture.get("maker_decision_gate_passed"), bool)
                else None
            )
        section["metrics"] = {
            key: value for key, value in metrics.items() if value is not None
        }
    elif name == "maker_subsecond_information_experiment":
        reasons = _safe_tokens(report.get("reason_codes"))
        decision = report.get("research_decision")
        allowed_decisions = {
            "CONTINUE_TO_INDEPENDENT_SUBSECOND_MAKER_FORWARD_VALIDATION",
            "STOP_MAKER_INFORMATION_SET",
            "STOP_SUBSECOND_EXPERIMENT_UPSTREAM_NOT_PROVEN",
        }
        contract_ok = (
            report.get("schema_version")
            == "maker_subsecond_information_experiment_v1"
            and report.get("status") == "COMPLETE"
            and report.get("fully_verifiable") is True
            and report.get("research_domain") == "forward_development_only"
            and report.get("promotion_evidence") is False
            and report.get("promotion_eligible") is False
            and report.get("promotion_authority") is False
            and report.get("demo_activation_authorized") is False
            and report.get("live_activation_authorized") is False
            and decision in allowed_decisions
        )
        section["gate_status"] = "COMPLETE" if contract_ok else "NOT_READY"
        section["research_decision"] = (
            decision if decision in allowed_decisions else None
        )
        section["research_observation_only"] = True
        section["promotion_authority"] = False
        section["demo_activation_authorized"] = False
        section["live_activation_authorized"] = False
        data = report.get("data")
        comparison = report.get("architecture_comparison")
        architectures = (
            comparison.get("architectures")
            if isinstance(comparison, Mapping)
            else None
        )
        diagnostics = report.get("incremental_information_diagnostics")
        metrics = {
            "aligned_row_count": (
                _safe_number(data.get("subsecond_aligned_eligible_row_count"))
                if isinstance(data, Mapping)
                else None
            ),
            "aligned_row_ratio": (
                _safe_number(data.get("subsecond_aligned_row_ratio"))
                if isinstance(data, Mapping)
                else None
            ),
            "positive_stress_split_ratio": (
                _safe_number(
                    diagnostics.get("treatment_positive_stress_split_ratio")
                )
                if isinstance(diagnostics, Mapping)
                else None
            ),
            "positive_profitability_auc_gain_split_ratio": (
                _safe_number(
                    diagnostics.get(
                        "positive_profitability_roc_auc_gain_split_ratio"
                    )
                )
                if isinstance(diagnostics, Mapping)
                else None
            ),
            "decision_gate_passed": (
                diagnostics.get("decision_gate_passed")
                if isinstance(diagnostics, Mapping)
                and isinstance(diagnostics.get("decision_gate_passed"), bool)
                else None
            ),
            "stress_lcb_improvement_bps": (
                _safe_number(diagnostics.get("stress_lcb_improvement_bps"))
                if isinstance(diagnostics, Mapping)
                else None
            ),
        }
        for key, report_key in (
            ("fill_roc_auc", "treatment_fill_roc_auc_by_split"),
            (
                "profitability_roc_auc",
                "treatment_profitability_roc_auc_by_split",
            ),
            ("profitability_auc_gain", "profitability_roc_auc_gain_by_split"),
            ("stress_mean_improvement_bps", "stress_mean_improvement_by_split"),
        ):
            summary = (
                diagnostics.get(report_key)
                if isinstance(diagnostics, Mapping)
                else None
            )
            metrics[key] = (
                _safe_number(summary.get("mean_bps"))
                if isinstance(summary, Mapping)
                else None
            )
        for variant_id, prefix in (
            ("one_second_decomposed_baseline", "baseline"),
            ("subsecond_queue_decomposed_treatment", "treatment"),
        ):
            architecture = (
                architectures.get(variant_id)
                if isinstance(architectures, Mapping)
                else None
            )
            base = (
                architecture.get("oos_base_cost_by_split")
                if isinstance(architecture, Mapping)
                else None
            )
            stress = (
                architecture.get("oos_stress_cost_by_split")
                if isinstance(architecture, Mapping)
                else None
            )
            control = (
                architecture.get("prediction_permutation_control")
                if isinstance(architecture, Mapping)
                else None
            )
            metrics[f"{prefix}_trade_count"] = (
                _safe_number(architecture.get("trade_count"))
                if isinstance(architecture, Mapping)
                else None
            )
            metrics[f"{prefix}_base_lcb_bps"] = (
                _safe_number(base.get("lcb_bps"))
                if isinstance(base, Mapping)
                else None
            )
            metrics[f"{prefix}_stress_lcb_bps"] = (
                _safe_number(stress.get("lcb_bps"))
                if isinstance(stress, Mapping)
                else None
            )
            metrics[f"{prefix}_permutation_passed"] = (
                control.get("passed")
                if isinstance(control, Mapping)
                and isinstance(control.get("passed"), bool)
                else None
            )
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
        "runner_errors": _runner_error_lines(
            artifact_dir / "closed_loop_runner_command.log"
        ),
        "runner_tail": _runner_tail_lines(
            artifact_dir / "closed_loop_runner_command.log"
        ),
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
    runner_errors = " | ".join(
        str(item) for item in summary.get("runner_errors", [])[:4]
    ) or "none"
    runner_tail = " | ".join(
        str(item) for item in summary.get("runner_tail", [])[-6:]
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
    information_set = upstream.get("liquidation_information_set_experiment", {})
    progress_parts: list[str] = []
    stage = information_set.get("not_ready_stage")
    if isinstance(stage, str) and _SAFE_TOKEN.fullmatch(stage):
        progress_parts.append(f"stage:{stage}")
    information_metrics = information_set.get("metrics")
    if isinstance(information_metrics, Mapping):
        for key in (
            "liquidation_coverage_seconds",
            "liquidation_minimum_coverage_seconds",
            "liquidation_missing_coverage_seconds",
            "liquidation_coverage_ratio",
            "liquidation_freshness_seconds",
            "liquidation_report_file_count",
            "liquidation_valid_segment_count",
            "liquidation_invalid_segment_count",
            "minimum_common_span_seconds",
            "common_row_count",
            "oracle_trade_count",
            "oracle_positive_split_ratio",
            "oracle_base_lcb_bps",
            "oracle_stress_lcb_bps",
            "control_trade_count",
            "control_positive_split_ratio",
            "control_base_lcb_bps",
            "control_stress_lcb_bps",
            "treatment_trade_count",
            "treatment_positive_split_ratio",
            "treatment_base_lcb_bps",
            "treatment_stress_lcb_bps",
            "paired_delta_base_lcb_bps",
            "paired_delta_stress_lcb_bps",
        ):
            value = _safe_number(information_metrics.get(key))
            if value is not None:
                progress_parts.append(f"{key}:{value}")
        for key in (
            "control_permutation_passed",
            "treatment_permutation_passed",
            "paired_permutation_passed",
        ):
            value = information_metrics.get(key)
            if isinstance(value, bool):
                progress_parts.append(f"{key}:{str(value).lower()}")
    information_set_progress = ",".join(progress_parts) or "UNAVAILABLE"
    maker_opportunity = upstream.get("maker_execution_opportunity_experiment", {})
    maker_opportunity_decision = (
        maker_opportunity.get("research_decision", "UNAVAILABLE") or "UNAVAILABLE"
    )
    maker_progress_parts: list[str] = []
    maker_metrics = maker_opportunity.get("metrics")
    if isinstance(maker_metrics, Mapping):
        for key in (
            "common_row_count",
            "filled_decision_count",
            "filled_action_count",
            "oracle_trade_count",
            "oracle_positive_split_ratio",
            "oracle_base_lcb_bps",
            "oracle_stress_lcb_bps",
            "boundary_pass_ratio",
            "forward_row_ratio",
        ):
            value = _safe_number(maker_metrics.get(key))
            if value is not None:
                maker_progress_parts.append(f"{key}:{value}")
        forward_complete = maker_metrics.get("forward_observation_complete")
        if isinstance(forward_complete, bool):
            maker_progress_parts.append(
                "forward_observation_complete:"
                + str(forward_complete).lower()
            )
    maker_opportunity_progress = ",".join(maker_progress_parts) or "UNAVAILABLE"
    maker_learnability = upstream.get(
        "maker_execution_learnability_experiment", {}
    )
    maker_learnability_decision = (
        maker_learnability.get("research_decision", "UNAVAILABLE")
        or "UNAVAILABLE"
    )
    maker_learnability_leader = (
        maker_learnability.get("diagnostic_leader_id", "UNAVAILABLE")
        or "none"
    )
    maker_learnability_progress_parts: list[str] = []
    maker_learnability_metrics = maker_learnability.get("metrics")
    if isinstance(maker_learnability_metrics, Mapping):
        for key in (
            "eligible_row_count",
            "hurdle_tail_trade_count",
            "hurdle_tail_positive_split_ratio",
            "hurdle_tail_base_lcb_bps",
            "hurdle_tail_stress_lcb_bps",
        ):
            value = _safe_number(maker_learnability_metrics.get(key))
            if value is not None:
                maker_learnability_progress_parts.append(f"{key}:{value}")
        for key in (
            "hurdle_tail_permutation_passed",
            "hurdle_tail_maker_gate_passed",
        ):
            value = maker_learnability_metrics.get(key)
            if isinstance(value, bool):
                maker_learnability_progress_parts.append(
                    f"{key}:{str(value).lower()}"
                )
    maker_learnability_progress = (
        ",".join(maker_learnability_progress_parts) or "UNAVAILABLE"
    )
    maker_subsecond = upstream.get("maker_subsecond_information_experiment", {})
    maker_subsecond_decision = (
        maker_subsecond.get("research_decision", "UNAVAILABLE") or "UNAVAILABLE"
    )
    maker_subsecond_progress_parts: list[str] = []
    maker_subsecond_metrics = maker_subsecond.get("metrics")
    if isinstance(maker_subsecond_metrics, Mapping):
        for key in (
            "aligned_row_count",
            "aligned_row_ratio",
            "baseline_trade_count",
            "baseline_base_lcb_bps",
            "baseline_stress_lcb_bps",
            "treatment_trade_count",
            "treatment_base_lcb_bps",
            "treatment_stress_lcb_bps",
            "positive_stress_split_ratio",
            "fill_roc_auc",
            "profitability_roc_auc",
            "profitability_auc_gain",
            "positive_profitability_auc_gain_split_ratio",
            "stress_mean_improvement_bps",
            "stress_lcb_improvement_bps",
        ):
            value = _safe_number(maker_subsecond_metrics.get(key))
            if value is not None:
                maker_subsecond_progress_parts.append(f"{key}:{value}")
        for key in (
            "baseline_permutation_passed",
            "treatment_permutation_passed",
            "decision_gate_passed",
        ):
            value = maker_subsecond_metrics.get(key)
            if isinstance(value, bool):
                maker_subsecond_progress_parts.append(
                    f"{key}:{str(value).lower()}"
                )
    maker_subsecond_progress = (
        ",".join(maker_subsecond_progress_parts) or "UNAVAILABLE"
    )
    return (
        f"failed_steps={failed_steps}; runner_errors={runner_errors}; "
        f"runner_tail={runner_tail}; "
        f"upstream={upstream_statuses}; "
        f"information_set_decision={information_set_decision}; "
        f"information_set_progress={information_set_progress}; "
        f"maker_opportunity_decision={maker_opportunity_decision}; "
        f"maker_opportunity_progress={maker_opportunity_progress}; "
        f"maker_learnability_decision={maker_learnability_decision}; "
        f"maker_learnability_leader={maker_learnability_leader}; "
        f"maker_learnability_progress={maker_learnability_progress}; "
        f"maker_subsecond_decision={maker_subsecond_decision}; "
        f"maker_subsecond_progress={maker_subsecond_progress}; "
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
        level = (
            "error"
            if summary["failed_steps"] or summary["runner_errors"]
            else "notice"
        )
        print(f"::{level} title=Closed Loop evidence summary::{_annotation(summary)}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
