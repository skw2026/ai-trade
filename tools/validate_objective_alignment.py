#!/usr/bin/env python3
"""Validate proxy-score alignment with complete executable net utility."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import pathlib
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

from decision_evidence_common import (
    canonical_sha256,
    file_sha256,
    validate_verified_benchmark_report,
)


SUBSYSTEMS = ("miner", "market_alpha", "microstructure", "online_tuner")
EVIDENCE_SCHEMA_VERSION = "candidate_alignment_evidence_v1"
REPORT_SCHEMA_VERSION = "objective_alignment_validation_v1"
EXPECTED_PERMUTATION_UNIT = "candidate_aggregate_utility"
EXPECTED_UTILITY_SOURCE = "complete_execution_replay"
SCORE_DIRECTIONS = ("higher_is_better", "lower_is_better")
EXACT_PERMUTATION_LIMIT = 10000
EXPECTED_ALIGNMENT_POLICY = {
    "min_candidates": 8,
    "min_independent_blocks": 5,
    "alpha": 0.05,
    "permutation_trials": 10000,
}


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _missing(values: Sequence[str]) -> list[str]:
    return sorted(set(str(value) for value in values if str(value)))


def average_ranks(values: Sequence[float]) -> list[float]:
    """Return one-based ranks, assigning the average rank to ties."""

    indexed = sorted(enumerate(values), key=lambda item: (float(item[1]), item[0]))
    ranks = [0.0] * len(indexed)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        rank = ((cursor + 1) + end) / 2.0
        for offset in range(cursor, end):
            ranks[indexed[offset][0]] = rank
        cursor = end
    return ranks


def spearman_rho(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("Spearman inputs must be non-empty and equal length")
    left_rank = average_ranks(left)
    right_rank = average_ranks(right)
    left_mean = sum(left_rank) / len(left_rank)
    right_mean = sum(right_rank) / len(right_rank)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_rank, right_rank)
    )
    left_scale = sum((value - left_mean) ** 2 for value in left_rank)
    right_scale = sum((value - right_mean) ** 2 for value in right_rank)
    denominator = math.sqrt(left_scale * right_scale)
    return numerator / denominator if denominator > 0.0 else 0.0


def _permutation_result(
    *,
    benchmark_id: str,
    subsystem: str,
    candidate_ids: Sequence[str],
    normalized_scores: Sequence[float],
    utilities: Sequence[float],
    configured_trials: int,
    observed_rho: float,
) -> dict[str, Any]:
    candidate_count = len(candidate_ids)
    exhaustive_count = math.factorial(candidate_count)
    exceedance_count = 0
    if exhaustive_count <= EXACT_PERMUTATION_LIMIT:
        trials = exhaustive_count
        method = "exact_enumeration"
        permutations = itertools.permutations(utilities)
        for permuted in permutations:
            if spearman_rho(normalized_scores, permuted) >= observed_rho:
                exceedance_count += 1
    else:
        trials = configured_trials
        method = "deterministic_sha256_order"
        utility_by_candidate = dict(zip(candidate_ids, utilities))
        for trial in range(trials):
            ordered = sorted(
                candidate_ids,
                key=lambda candidate_id: hashlib.sha256(
                    (
                        f"{benchmark_id}:alignment:{subsystem}:"
                        f"{trial}:{candidate_id}"
                    ).encode("ascii")
                ).digest(),
            )
            permuted = [utility_by_candidate[candidate_id] for candidate_id in ordered]
            if spearman_rho(normalized_scores, permuted) >= observed_rho:
                exceedance_count += 1
    return {
        "unit": EXPECTED_PERMUTATION_UNIT,
        "method": method,
        "configured_trials": configured_trials,
        "trials": trials,
        "exceedance_count": exceedance_count,
        "p_value_formula": "(1+exceedance_count)/(1+trials)",
        "p_value": (1.0 + exceedance_count) / (1.0 + trials),
    }


def _alignment_policy(config: Any) -> tuple[dict[str, Any] | None, list[str]]:
    missing: list[str] = []
    if not isinstance(config, Mapping):
        return None, ["config"]
    if config.get("schema_version") != "decision_evidence_validation_v1":
        missing.append("config.schema_version=decision_evidence_validation_v1")
    raw = config.get("alignment")
    if not isinstance(raw, Mapping):
        return None, ["config.alignment"]
    min_candidates = raw.get("min_candidates")
    min_blocks = raw.get("min_independent_blocks")
    alpha = raw.get("alpha")
    trials = raw.get("permutation_trials")
    if not _is_integer(min_candidates) or min_candidates <= 0:
        missing.append("config.alignment.min_candidates")
    if not _is_integer(min_blocks) or min_blocks <= 0:
        missing.append("config.alignment.min_independent_blocks")
    if not _is_finite_number(alpha) or not 0.0 < float(alpha) <= 1.0:
        missing.append("config.alignment.alpha")
    if not _is_integer(trials) or trials <= 0:
        missing.append("config.alignment.permutation_trials")
    if dict(raw) != EXPECTED_ALIGNMENT_POLICY:
        missing.append("config.alignment=frozen_v1_contract")
    if missing:
        return None, missing
    return {
        "min_candidates": int(min_candidates),
        "min_independent_blocks": int(min_blocks),
        "alpha": float(alpha),
        "permutation_trials": int(trials),
        "rho_required": ">0",
    }, []


def _benchmark_blocks(
    benchmark_report: Any,
) -> tuple[str | None, list[dict[str, Any]], list[str]]:
    missing: list[str] = []
    if not isinstance(benchmark_report, Mapping):
        return None, [], ["benchmark_report"]
    benchmark_id = benchmark_report.get("benchmark_id")
    if benchmark_report.get("identity_status") != "VERIFIED":
        missing.append("benchmark.identity_status=VERIFIED")
    if not _is_sha256(benchmark_id):
        missing.append("benchmark.benchmark_id")
    identity = benchmark_report.get("canonical_identity")
    universe = identity.get("evaluation_universe") if isinstance(identity, Mapping) else None
    raw_blocks = universe.get("blocks") if isinstance(universe, Mapping) else None
    if not isinstance(raw_blocks, list) or not raw_blocks:
        return benchmark_id if isinstance(benchmark_id, str) else None, [], _missing(
            [*missing, "benchmark.evaluation_universe.blocks"]
        )

    blocks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    intervals: list[tuple[int, int, str]] = []
    for index, raw in enumerate(raw_blocks):
        prefix = f"benchmark.evaluation_universe.blocks[{index}]"
        if not isinstance(raw, Mapping):
            missing.append(prefix)
            continue
        block_id = raw.get("block_id")
        start = raw.get("start_timestamp_ms")
        end = raw.get("end_timestamp_ms")
        event_sha = raw.get("event_sha256")
        if not _is_non_empty_string(block_id):
            missing.append(f"{prefix}.block_id")
        elif block_id in seen_ids:
            missing.append(f"{prefix}.block_id=unique")
        else:
            seen_ids.add(block_id)
        if not (_is_integer(start) and _is_integer(end) and start >= 0 and start <= end):
            missing.append(f"{prefix}.interval")
        elif _is_non_empty_string(block_id):
            intervals.append((start, end, block_id))
        if not _is_sha256(event_sha):
            missing.append(f"{prefix}.event_sha256")
        if (
            _is_non_empty_string(block_id)
            and _is_integer(start)
            and _is_integer(end)
            and start >= 0
            and start <= end
            and _is_sha256(event_sha)
        ):
            blocks.append(
                {
                    "block_id": block_id,
                    "start_timestamp_ms": start,
                    "end_timestamp_ms": end,
                    "event_sha256": event_sha,
                }
            )
    previous: tuple[int, int, str] | None = None
    for interval in sorted(intervals):
        if previous is not None and interval[0] <= previous[1]:
            missing.append("benchmark.evaluation_universe.non_overlapping")
        if previous is None or interval[1] > previous[1]:
            previous = interval
    blocks.sort(key=lambda item: item["block_id"])
    return benchmark_id if isinstance(benchmark_id, str) else None, blocks, _missing(missing)


def _empty_section(thresholds: Mapping[str, Any], missing: Sequence[str]) -> dict[str, Any]:
    return {
        "status": "UNVERIFIABLE",
        "candidate_count": 0,
        "independent_block_count": 0,
        "rho": None,
        "p_value": None,
        "thresholds": dict(thresholds),
        "permutation": None,
        "candidate_audit": [],
        "missing_fields": _missing(missing),
    }


def _validate_subsystem(
    *,
    subsystem: str,
    raw_section: Any,
    benchmark_id: str,
    benchmark_blocks: Sequence[Mapping[str, Any]],
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw_section, Mapping):
        return _empty_section(thresholds, [f"subsystems.{subsystem}"])

    missing: list[str] = []
    raw_adapter_missing = raw_section.get("adapter_missing_fields", [])
    if isinstance(raw_adapter_missing, list):
        missing.extend(f"adapter.{value}" for value in raw_adapter_missing)
    elif raw_adapter_missing:
        missing.append("adapter_missing_fields")

    permutation_unit = raw_section.get(
        "permutation_unit", EXPECTED_PERMUTATION_UNIT
    )
    if permutation_unit != EXPECTED_PERMUTATION_UNIT:
        missing.append(f"permutation_unit={EXPECTED_PERMUTATION_UNIT}")

    raw_candidates = raw_section.get("candidates")
    if not isinstance(raw_candidates, list):
        return _empty_section(thresholds, [*missing, "candidates"])
    if len(raw_candidates) < thresholds["min_candidates"]:
        missing.append(f"candidate_count>={thresholds['min_candidates']}")
    if len(benchmark_blocks) < thresholds["min_independent_blocks"]:
        missing.append(
            f"independent_block_count>={thresholds['min_independent_blocks']}"
        )

    expected_by_id = {str(block["block_id"]): block for block in benchmark_blocks}
    expected_ids = set(expected_by_id)
    seen_candidate_ids: set[str] = set()
    seen_score_directions: set[str] = set()
    audit_with_order: list[tuple[str, int, dict[str, Any]]] = []
    valid_rows: list[tuple[str, float, float]] = []
    for candidate_index, raw_candidate in enumerate(raw_candidates):
        candidate_prefix = f"candidates[{candidate_index}]"
        candidate_missing: list[str] = []
        if not isinstance(raw_candidate, Mapping):
            missing.append(candidate_prefix)
            audit_with_order.append(
                ("", candidate_index, {"candidate_id": None, "missing_fields": [candidate_prefix]})
            )
            continue

        raw_declared_missing = raw_candidate.get("missing_fields", [])
        if isinstance(raw_declared_missing, list):
            candidate_missing.extend(
                f"{candidate_prefix}.{value}" for value in raw_declared_missing
            )
        elif raw_declared_missing:
            candidate_missing.append(f"{candidate_prefix}.missing_fields")

        candidate_id = raw_candidate.get("candidate_id")
        if not _is_non_empty_string(candidate_id):
            candidate_missing.append(f"{candidate_prefix}.candidate_id")
            sort_id = ""
        else:
            sort_id = candidate_id
            if candidate_id in seen_candidate_ids:
                candidate_missing.append(f"{candidate_prefix}.candidate_id=unique")
            seen_candidate_ids.add(candidate_id)
        candidate_benchmark = raw_candidate.get("benchmark_id")
        if candidate_benchmark is not None and candidate_benchmark != benchmark_id:
            candidate_missing.append(f"{candidate_prefix}.benchmark_id")

        internal_score = raw_candidate.get("internal_score")
        if not _is_finite_number(internal_score):
            candidate_missing.append(f"{candidate_prefix}.internal_score")
            finite_score = None
        else:
            finite_score = float(internal_score)
        direction = raw_candidate.get("score_direction")
        if direction not in SCORE_DIRECTIONS:
            candidate_missing.append(f"{candidate_prefix}.score_direction")
            normalized_score = None
        else:
            seen_score_directions.add(direction)
            if finite_score is None:
                normalized_score = None
            else:
                normalized_score = (
                    finite_score if direction == "higher_is_better" else -finite_score
                )

        raw_blocks = raw_candidate.get("blocks")
        block_audit: list[dict[str, Any]] = []
        utilities: list[float] = []
        observed_ids: list[str] = []
        if not isinstance(raw_blocks, list):
            candidate_missing.append(f"{candidate_prefix}.blocks")
            raw_blocks = []
        for block_index, raw_block in enumerate(raw_blocks):
            block_prefix = f"{candidate_prefix}.blocks[{block_index}]"
            block_missing: list[str] = []
            if not isinstance(raw_block, Mapping):
                candidate_missing.append(block_prefix)
                block_audit.append(
                    {
                        "block_id": None,
                        "executable_net_utility": None,
                        "missing_fields": [block_prefix],
                    }
                )
                continue
            block_id = raw_block.get("block_id")
            expected = expected_by_id.get(block_id) if isinstance(block_id, str) else None
            if not _is_non_empty_string(block_id):
                block_missing.append(f"{block_prefix}.block_id")
            else:
                if block_id in observed_ids:
                    block_missing.append(f"{block_prefix}.block_id=unique")
                observed_ids.append(block_id)
                if expected is None:
                    block_missing.append(f"{block_prefix}.block_id=frozen_benchmark")
            if expected is not None:
                for field in ("start_timestamp_ms", "end_timestamp_ms"):
                    if field in raw_block and raw_block.get(field) != expected[field]:
                        block_missing.append(f"{block_prefix}.{field}=frozen_benchmark")
                if raw_block.get("event_sha256") != expected["event_sha256"]:
                    block_missing.append(f"{block_prefix}.event_sha256=frozen_benchmark")
            elif not _is_sha256(raw_block.get("event_sha256")):
                block_missing.append(f"{block_prefix}.event_sha256")
            if raw_block.get("independent_oos") is not True:
                block_missing.append(f"{block_prefix}.independent_oos=true")
            if raw_block.get("execution_path_complete") is not True:
                block_missing.append(f"{block_prefix}.execution_path_complete=true")
            if raw_block.get("utility_source") != EXPECTED_UTILITY_SOURCE:
                block_missing.append(
                    f"{block_prefix}.utility_source={EXPECTED_UTILITY_SOURCE}"
                )
            utility = raw_block.get("executable_net_utility")
            if not _is_finite_number(utility):
                block_missing.append(f"{block_prefix}.executable_net_utility")
                finite_utility = None
            else:
                finite_utility = float(utility)
                utilities.append(finite_utility)
            candidate_missing.extend(block_missing)
            block_audit.append(
                {
                    "block_id": block_id if isinstance(block_id, str) else None,
                    "start_timestamp_ms": expected.get("start_timestamp_ms") if expected else None,
                    "end_timestamp_ms": expected.get("end_timestamp_ms") if expected else None,
                    "event_sha256": raw_block.get("event_sha256"),
                    "independent_oos": raw_block.get("independent_oos") is True,
                    "execution_path_complete": raw_block.get("execution_path_complete") is True,
                    "utility_source": raw_block.get("utility_source"),
                    "executable_net_utility": finite_utility,
                    "missing_fields": _missing(block_missing),
                }
            )
        if set(observed_ids) != expected_ids or len(observed_ids) != len(expected_ids):
            candidate_missing.append(f"{candidate_prefix}.blocks=frozen_block_set")

        aggregate_utility = (
            sum(utilities) / len(utilities)
            if not candidate_missing and len(utilities) == len(expected_ids)
            else None
        )
        candidate_audit = {
            "candidate_id": candidate_id if isinstance(candidate_id, str) else None,
            "internal_score": finite_score,
            "score_direction": direction,
            "normalized_internal_score": normalized_score,
            "aggregate_executable_net_utility": aggregate_utility,
            "aggregate_method": "mean_block_executable_net_utility",
            "blocks": sorted(
                block_audit,
                key=lambda item: (item["block_id"] is None, item["block_id"] or ""),
            ),
            "missing_fields": _missing(candidate_missing),
        }
        audit_with_order.append((sort_id, candidate_index, candidate_audit))
        missing.extend(candidate_missing)
        if (
            not candidate_missing
            and isinstance(candidate_id, str)
            and normalized_score is not None
            and aggregate_utility is not None
        ):
            valid_rows.append((candidate_id, normalized_score, aggregate_utility))

    if len(seen_score_directions) > 1:
        missing.append("score_direction=consistent_within_subsystem")

    candidate_audit = [item[2] for item in sorted(audit_with_order)]
    missing = _missing(missing)
    base = {
        "candidate_count": len(raw_candidates),
        "independent_block_count": len(benchmark_blocks),
        "thresholds": dict(thresholds),
        "candidate_audit": candidate_audit,
        "missing_fields": missing,
    }
    if missing or len(valid_rows) != len(raw_candidates):
        return {
            "status": "UNVERIFIABLE",
            **base,
            "rho": None,
            "p_value": None,
            "permutation": None,
        }

    valid_rows.sort(key=lambda item: item[0])
    candidate_ids = [item[0] for item in valid_rows]
    normalized_scores = [item[1] for item in valid_rows]
    aggregate_utilities = [item[2] for item in valid_rows]
    rho = spearman_rho(normalized_scores, aggregate_utilities)
    permutation = _permutation_result(
        benchmark_id=benchmark_id,
        subsystem=subsystem,
        candidate_ids=candidate_ids,
        normalized_scores=normalized_scores,
        utilities=aggregate_utilities,
        configured_trials=int(thresholds["permutation_trials"]),
        observed_rho=rho,
    )
    p_value = float(permutation["p_value"])
    return {
        "status": (
            "ALIGNED"
            if rho > 0.0 and p_value <= float(thresholds["alpha"])
            else "NOT_ALIGNED"
        ),
        **base,
        "rho": rho,
        "p_value": p_value,
        "permutation": permutation,
    }


def validate_alignment(
    evidence: Any,
    benchmark_report: Any,
    config: Any,
    *,
    validation_config_sha256: str | None = None,
) -> dict[str, Any]:
    benchmark_verification = validate_verified_benchmark_report(
        benchmark_report,
        validation_policy=config,
        validation_config_sha256=validation_config_sha256,
    )
    expected_benchmark_id, blocks, benchmark_missing = _benchmark_blocks(
        benchmark_report
    )
    if benchmark_verification["benchmark_id"] is not None:
        expected_benchmark_id = benchmark_verification["benchmark_id"]
    thresholds, config_missing = _alignment_policy(config)
    safe_thresholds = thresholds or {
        "min_candidates": None,
        "min_independent_blocks": None,
        "alpha": None,
        "permutation_trials": None,
        "rho_required": ">0",
    }
    top_missing = [
        *benchmark_verification["errors"],
        *benchmark_missing,
        *config_missing,
    ]
    actual_benchmark_id = evidence.get("benchmark_id") if isinstance(evidence, Mapping) else None
    raw_subsystems = evidence.get("subsystems") if isinstance(evidence, Mapping) else None
    if not isinstance(evidence, Mapping):
        top_missing.append("evidence")
    elif evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        top_missing.append(f"evidence.schema_version={EVIDENCE_SCHEMA_VERSION}")
    if actual_benchmark_id != expected_benchmark_id:
        top_missing.append("evidence.benchmark_id=frozen_benchmark")
    if not isinstance(raw_subsystems, Mapping):
        top_missing.append("evidence.subsystems")
        raw_subsystems = {}
    top_missing = _missing(top_missing)

    sections: dict[str, dict[str, Any]] = {}
    for subsystem in SUBSYSTEMS:
        if top_missing or thresholds is None or expected_benchmark_id is None:
            section_missing = list(top_missing)
            if subsystem not in raw_subsystems:
                section_missing.append(f"subsystems.{subsystem}")
            sections[subsystem] = _empty_section(safe_thresholds, section_missing)
        else:
            sections[subsystem] = _validate_subsystem(
                subsystem=subsystem,
                raw_section=raw_subsystems.get(subsystem),
                benchmark_id=expected_benchmark_id,
                benchmark_blocks=blocks,
                thresholds=thresholds,
            )

    statuses = [sections[name]["status"] for name in SUBSYSTEMS]
    if "UNVERIFIABLE" in statuses:
        overall = "UNVERIFIABLE"
    elif all(status == "ALIGNED" for status in statuses):
        overall = "ALIGNED"
    else:
        overall = "NOT_ALIGNED"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark_id": expected_benchmark_id,
        "expected_benchmark_id": expected_benchmark_id,
        "actual_benchmark_id": actual_benchmark_id,
        "overall_status": overall,
        "benchmark_verification": benchmark_verification,
        "thresholds": safe_thresholds,
        "missing_fields": top_missing,
        "subsystems": sections,
    }


def validate_alignment_report_artifact(
    report: Any,
    benchmark_report: Any,
    validation_policy: Any,
    *,
    validation_config_sha256: str | None,
) -> dict[str, Any]:
    """Rebuild an alignment report from its candidate/block audit evidence."""

    errors: list[str] = []
    benchmark_verification = validate_verified_benchmark_report(
        benchmark_report,
        validation_policy=validation_policy,
        validation_config_sha256=validation_config_sha256,
    )
    errors.extend(benchmark_verification["errors"])
    if not isinstance(report, Mapping):
        errors.append("alignment_report")
        return {"verified": False, "status": None, "errors": _missing(errors)}
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        errors.append(f"alignment.schema_version={REPORT_SCHEMA_VERSION}")
    if report.get("overall_status") not in {"ALIGNED", "NOT_ALIGNED"}:
        errors.append("alignment.overall_status=complete_evidence")
    expected_benchmark_id = benchmark_verification["benchmark_id"]
    for field in ("benchmark_id", "expected_benchmark_id", "actual_benchmark_id"):
        if report.get(field) != expected_benchmark_id:
            errors.append(f"alignment.{field}=frozen_benchmark")
    raw_subsystems = report.get("subsystems")
    if not isinstance(raw_subsystems, Mapping) or set(raw_subsystems) != set(SUBSYSTEMS):
        errors.append("alignment.subsystems=fixed_four")
        raw_subsystems = {}

    evidence_subsystems: dict[str, dict[str, Any]] = {}
    for subsystem in SUBSYSTEMS:
        raw_section = raw_subsystems.get(subsystem)
        prefix = f"alignment.subsystems.{subsystem}"
        if not isinstance(raw_section, Mapping):
            errors.append(prefix)
            continue
        candidate_audit = raw_section.get("candidate_audit")
        permutation = raw_section.get("permutation")
        if not isinstance(candidate_audit, list) or not candidate_audit:
            errors.append(f"{prefix}.candidate_audit")
            continue
        if not isinstance(permutation, Mapping):
            errors.append(f"{prefix}.permutation")
            continue
        candidates: list[dict[str, Any]] = []
        for candidate_index, candidate in enumerate(candidate_audit):
            candidate_prefix = f"{prefix}.candidate_audit[{candidate_index}]"
            if not isinstance(candidate, Mapping):
                errors.append(candidate_prefix)
                continue
            if candidate.get("missing_fields") != []:
                errors.append(f"{candidate_prefix}.missing_fields=empty")
            raw_blocks = candidate.get("blocks")
            if not isinstance(raw_blocks, list) or not raw_blocks:
                errors.append(f"{candidate_prefix}.blocks")
                continue
            blocks = []
            for block_index, block in enumerate(raw_blocks):
                block_prefix = f"{candidate_prefix}.blocks[{block_index}]"
                if not isinstance(block, Mapping):
                    errors.append(block_prefix)
                    continue
                if block.get("missing_fields") != []:
                    errors.append(f"{block_prefix}.missing_fields=empty")
                blocks.append(
                    {
                        "block_id": block.get("block_id"),
                        "start_timestamp_ms": block.get("start_timestamp_ms"),
                        "end_timestamp_ms": block.get("end_timestamp_ms"),
                        "event_sha256": block.get("event_sha256"),
                        "independent_oos": block.get("independent_oos"),
                        "execution_path_complete": block.get(
                            "execution_path_complete"
                        ),
                        "utility_source": block.get("utility_source"),
                        "executable_net_utility": block.get(
                            "executable_net_utility"
                        ),
                    }
                )
            candidates.append(
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "internal_score": candidate.get("internal_score"),
                    "score_direction": candidate.get("score_direction"),
                    "blocks": blocks,
                }
            )
        evidence_subsystems[subsystem] = {
            "permutation_unit": permutation.get("unit"),
            "candidates": candidates,
        }

    if not errors:
        reconstructed = validate_alignment(
            {
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "benchmark_id": expected_benchmark_id,
                "subsystems": evidence_subsystems,
            },
            benchmark_report,
            validation_policy,
            validation_config_sha256=validation_config_sha256,
        )
        try:
            if canonical_sha256(report) != canonical_sha256(reconstructed):
                errors.append("alignment.artifact_derived_state_mismatch")
        except (TypeError, ValueError):
            errors.append("alignment.artifact_canonical_json")
    return {
        "verified": not errors,
        "status": report.get("overall_status"),
        "errors": _missing(errors),
    }


def _stable_candidate_id(subsystem: str, identity: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return f"{subsystem}:{hashlib.sha256(encoded).hexdigest()}"


def _proxy_candidate(
    subsystem: str,
    identity: Mapping[str, Any],
    score: Any,
) -> dict[str, Any] | None:
    if not _is_finite_number(score):
        return None
    return {
        "candidate_id": _stable_candidate_id(subsystem, identity),
        "source_candidate_identity": dict(identity),
        "internal_score": float(score),
        "score_direction": "higher_is_better",
        "missing_fields": [
            "blocks",
            "candidate_level_complete_execution_utility",
        ],
    }


def _adapt_miner(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, Mapping) or not isinstance(report.get("factors"), list):
        return []
    result = []
    for factor in report["factors"]:
        if not isinstance(factor, Mapping):
            continue
        identity = {
            "factor_set_version": report.get("factor_set_version"),
            "expression": factor.get("expression"),
        }
        candidate = _proxy_candidate("miner", identity, factor.get("objective_score"))
        if candidate is not None:
            result.append(candidate)
    return result


def _adapt_market_alpha(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, Mapping):
        return []
    screen = report.get("economic_screen")
    reports = screen.get("reports") if isinstance(screen, Mapping) else None
    if not isinstance(reports, list):
        return []
    result = []
    for feature_report in reports:
        if not isinstance(feature_report, Mapping) or not isinstance(
            feature_report.get("variants"), list
        ):
            continue
        for variant in feature_report["variants"]:
            if not isinstance(variant, Mapping):
                continue
            identity = {
                "feature_set": feature_report.get("feature_set"),
                "variant": variant.get("variant"),
            }
            score = variant.get("model_net_edge_lcb_bps")
            candidate = _proxy_candidate("market_alpha", identity, score)
            if candidate is not None:
                result.append(candidate)
    return result


def _adapt_microstructure(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, Mapping) or not isinstance(
        report.get("architectures"), Mapping
    ):
        return []
    result = []
    for architecture_id, architecture in sorted(report["architectures"].items()):
        if not isinstance(architecture, Mapping):
            continue
        stress = architecture.get("oos_stress_cost_by_split")
        score = stress.get("lcb_bps") if isinstance(stress, Mapping) else None
        candidate = _proxy_candidate(
            "microstructure", {"architecture_id": architecture_id}, score
        )
        if candidate is not None:
            result.append(candidate)
    return result


def _explicit_candidates(report: Any) -> list[dict[str, Any]] | None:
    if not isinstance(report, Mapping):
        return None
    evidence = report.get("candidate_alignment_evidence")
    if isinstance(evidence, Mapping) and isinstance(evidence.get("candidates"), list):
        return [dict(item) if isinstance(item, Mapping) else item for item in evidence["candidates"]]
    if isinstance(report.get("alignment_candidates"), list):
        return [dict(item) if isinstance(item, Mapping) else item for item in report["alignment_candidates"]]
    return None


def adapt_current_reports(
    *,
    benchmark_id: str,
    miner_report: Any,
    market_alpha_report: Any,
    microstructure_report: Any,
    online_tuner_report: Any,
) -> dict[str, Any]:
    reports = {
        "miner": miner_report,
        "market_alpha": market_alpha_report,
        "microstructure": microstructure_report,
        "online_tuner": online_tuner_report,
    }
    extractors = {
        "miner": _adapt_miner,
        "market_alpha": _adapt_market_alpha,
        "microstructure": _adapt_microstructure,
        "online_tuner": lambda report: [],
    }
    sections: dict[str, dict[str, Any]] = {}
    for subsystem in SUBSYSTEMS:
        source = reports[subsystem]
        explicit = _explicit_candidates(source)
        if explicit is not None:
            sections[subsystem] = {
                "adapter_source_schema_version": source.get("schema_version"),
                "permutation_unit": EXPECTED_PERMUTATION_UNIT,
                "adapter_missing_fields": [],
                "candidates": explicit,
            }
            continue
        source_missing = [] if isinstance(source, Mapping) else ["source_report"]
        sections[subsystem] = {
            "adapter_source_schema_version": (
                source.get("schema_version") if isinstance(source, Mapping) else None
            ),
            "permutation_unit": EXPECTED_PERMUTATION_UNIT,
            "adapter_missing_fields": [
                *source_missing,
                "benchmark_bound_block_set",
                "candidate_level_complete_execution_path",
                "candidate_level_complete_execution_utility",
            ],
            "candidates": extractors[subsystem](source),
        }
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "benchmark_id": benchmark_id,
        "adapter": "current_closed_loop_reports_v1",
        "subsystems": sections,
    }


def _read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_optional(path: str | None) -> Any:
    if not path:
        return None
    try:
        return _read_json(pathlib.Path(path))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"adapter_read_error": str(exc)}


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
            json.dump(payload, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        temporary.replace(path)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-report", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--evidence")
    parser.add_argument("--miner-report")
    parser.add_argument("--market-alpha-report")
    parser.add_argument("--microstructure-report")
    parser.add_argument("--online-tuner-report")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    benchmark = _read_optional(args.benchmark_report)
    policy = _read_optional(args.config)
    try:
        policy_sha256 = file_sha256(pathlib.Path(args.config))
    except OSError:
        policy_sha256 = None
    if args.evidence:
        evidence_payload = _read_optional(args.evidence)
    else:
        benchmark_id = benchmark.get("benchmark_id") if isinstance(benchmark, Mapping) else None
        evidence_payload = adapt_current_reports(
            benchmark_id=benchmark_id,
            miner_report=_read_optional(args.miner_report),
            market_alpha_report=_read_optional(args.market_alpha_report),
            microstructure_report=_read_optional(args.microstructure_report),
            online_tuner_report=_read_optional(args.online_tuner_report),
        )
    report = validate_alignment(
        evidence_payload,
        benchmark,
        policy,
        validation_config_sha256=policy_sha256,
    )
    _write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    if report["overall_status"] == "ALIGNED":
        return 0
    return 2 if report["overall_status"] == "UNVERIFIABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
