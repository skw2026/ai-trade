#!/usr/bin/env python3
"""Persist only independent, frozen-contract microstructure OOS regimes."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import pathlib
import tempfile
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "microstructure_regime_evidence_ledger_v1"
AUDIT_SCHEMA_VERSION = "microstructure_regime_evidence_audit_v1"
REPORT_SCHEMA_VERSION = "microstructure_alpha_development_v8"
COMPARISON_SCHEMA_VERSION = "microstructure_target_architecture_comparison_v1"
CANDIDATE_MANIFEST_SCHEMA_VERSION = "microstructure_alpha_candidate_manifest_v1"
EXPECTED_ARCHITECTURES = (
    "binary_stress_event_baseline",
    "direct_stress_utility_regression",
    "two_stage_opportunity_action",
    "joint_action_ranker",
)
REQUIRED_SPLIT_COUNT = 6
HEX_DIGITS = frozenset("0123456789abcdef")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in HEX_DIGITS for character in value)
    )


def require_mapping(value: Any, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(reason)
    return value


def require_finite(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(reason)
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(reason)
    return normalized


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = pathlib.Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                temporary_path.unlink()


def atomic_append_canonical_record(path: pathlib.Path, record: Mapping[str, Any]) -> None:
    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        raise ValueError("ledger_canonical_encoding_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = pathlib.Path(handle.name)
            handle.write(existing)
            handle.write(canonical_bytes(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                temporary_path.unlink()


def failure_audit(reason: str) -> dict[str, Any]:
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": "UNVERIFIABLE",
        "reason_codes": [str(reason)],
        "information_set_id": None,
        "evidence_id": None,
        "batch": None,
        "accepted_batch_count": 0,
        "independent_oos_hours": 0.0,
        "next_nonoverlap_test_start_ms": None,
        "stage_review_required": False,
        "stage_review_charter": None,
        "next_action": "repair_evidence_integrity",
        "research_observation_only": True,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }


def review_charter() -> dict[str, Any]:
    return {
        "format": "evidence_driven_roundtable",
        "required_reviews": [
            "objective_and_success_definition",
            "cost_and_execution_accounting",
            "label_learnability_and_oracle_gap",
            "information_set_and_market_regimes",
            "validation_power_and_independence",
            "action_space_and_horizons",
            "system_architecture_and_failure_boundaries",
        ],
        "decision_options": [
            "CONTINUE_FROZEN_INFORMATION_SET",
            "CHANGE_INFORMATION_SET",
            "STOP_CURRENT_RESEARCH_FAMILY",
        ],
        "threshold_relaxation_permitted": False,
    }


def information_set_contract(
    report: Mapping[str, Any], comparison: Mapping[str, Any]
) -> dict[str, Any]:
    shared = require_mapping(
        comparison.get("shared_contract"), "shared_contract_invalid"
    )
    static_shared_fields = (
        "feature_count",
        "ordered_feature_names_sha256",
        "causal_feature_contract",
        "actions",
        "action_count",
        "additional_round_trip_cost_bps",
        "stress_cost_multiplier",
        "execution_latency_seconds",
        "overlapping_episodes_forbidden",
        "split_count",
        "model_hyperparameters",
        "validation_or_test_targets_used_for_fit",
    )
    if any(field not in shared for field in static_shared_fields):
        raise ValueError("shared_contract_incomplete")
    contract = {
        "development_schema_version": report.get("schema_version"),
        "research_domain": report.get("research_domain"),
        "causal_feature_contract": report.get("causal_feature_contract"),
        "cross_asset_feature_contract": report.get("cross_asset_feature_contract"),
        "capture_merge_contract": report.get("capture_merge_contract"),
        "target_contract": report.get("target_contract"),
        "validation_contract": report.get("validation_contract"),
        "model_contract": report.get("model_contract"),
        "comparison_schema_version": comparison.get("schema_version"),
        "architecture_ids": comparison.get("architecture_ids"),
        "required_split_count": comparison.get("required_split_count"),
        "permutation_trial_count": comparison.get("permutation_trial_count"),
        "shared_contract": {field: shared[field] for field in static_shared_fields},
    }
    # Round-tripping catches unsupported values and non-finite floats before an
    # information-set identity can be admitted to the append-only ledger.
    canonical_bytes(contract)
    return contract


def normalized_split_evidence(
    comparison: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    architecture_ids = comparison.get("architecture_ids")
    if architecture_ids != list(EXPECTED_ARCHITECTURES):
        raise ValueError("architecture_set_invalid")
    if comparison.get("required_split_count") != REQUIRED_SPLIT_COUNT:
        raise ValueError("required_split_count_invalid")
    trials = comparison.get("permutation_trial_count")
    if isinstance(trials, bool) or not isinstance(trials, int) or trials <= 0:
        raise ValueError("permutation_trial_count_invalid")
    raw_splits = comparison.get("split_reports")
    if not isinstance(raw_splits, list) or len(raw_splits) != REQUIRED_SPLIT_COUNT:
        raise ValueError("split_report_coverage_invalid")

    shared = require_mapping(comparison.get("shared_contract"), "shared_contract_invalid")
    shared_without_identity = dict(shared)
    shared_identity = shared_without_identity.pop("identity_sha256", None)
    if not is_sha256(shared_identity) or canonical_sha256(shared_without_identity) != shared_identity:
        raise ValueError("shared_contract_identity_invalid")
    partition_contracts = shared.get("partition_identities")
    split_time_contracts = shared.get("split_time_contracts")
    if not isinstance(partition_contracts, list) or len(partition_contracts) != REQUIRED_SPLIT_COUNT:
        raise ValueError("shared_partition_coverage_invalid")
    if not isinstance(split_time_contracts, list) or len(split_time_contracts) != REQUIRED_SPLIT_COUNT:
        raise ValueError("shared_split_time_coverage_invalid")

    intervals: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    previous_end: int | None = None
    for expected_split_id, raw_split in enumerate(raw_splits):
        split = require_mapping(raw_split, "split_report_invalid")
        if split.get("split_id") != expected_split_id:
            raise ValueError("split_report_order_invalid")
        partition = require_mapping(
            split.get("shared_partition_identity"), "partition_identity_invalid"
        )
        partition_without_identity = dict(partition)
        partition_identity = partition_without_identity.pop("identity_sha256", None)
        if (
            not is_sha256(partition_identity)
            or canonical_sha256(partition_without_identity) != partition_identity
        ):
            raise ValueError("partition_identity_hash_invalid")
        if partition != partition_contracts[expected_split_id]:
            raise ValueError("shared_partition_identity_mismatch")
        time_contract = require_mapping(
            partition.get("time_contract"), "split_time_contract_invalid"
        )
        if time_contract != split_time_contracts[expected_split_id]:
            raise ValueError("shared_split_time_mismatch")
        if time_contract.get("split_id") != expected_split_id:
            raise ValueError("split_time_id_invalid")
        start = time_contract.get("test_start_ms")
        end = time_contract.get("test_end_ms")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or end <= start
        ):
            raise ValueError("oos_interval_invalid")
        if previous_end is not None and start != previous_end:
            raise ValueError("oos_intervals_not_contiguous")
        previous_end = end
        intervals.append(
            {
                "split_id": expected_split_id,
                "test_start_ms": start,
                "test_end_ms": end,
                "partition_identity_sha256": partition_identity,
            }
        )

        architectures = require_mapping(
            split.get("architectures"), "split_architectures_invalid"
        )
        if set(architectures) != set(EXPECTED_ARCHITECTURES):
            raise ValueError("split_architecture_coverage_invalid")
        normalized_architectures: dict[str, Any] = {}
        for architecture_id in EXPECTED_ARCHITECTURES:
            architecture = require_mapping(
                architectures.get(architecture_id), "architecture_report_invalid"
            )
            if (
                architecture.get("status") != "evaluated"
                or architecture.get("architecture_id") != architecture_id
                or architecture.get("promotion_evidence") is not False
                or architecture.get("promotion_eligible") is not False
            ):
                raise ValueError("architecture_report_contract_invalid")
            objective = require_mapping(
                architecture.get("oos_objective"), "architecture_objective_invalid"
            )
            base = require_mapping(objective.get("base_cost"), "base_economics_invalid")
            stress = require_mapping(
                objective.get("stress_cost"), "stress_economics_invalid"
            )
            base_count = base.get("count")
            stress_count = stress.get("count")
            if (
                isinstance(base_count, bool)
                or not isinstance(base_count, int)
                or base_count < 0
                or stress_count != base_count
            ):
                raise ValueError("trade_count_invalid")
            base_mean = base.get("mean_bps")
            stress_mean = stress.get("mean_bps")
            if base_count == 0 and base_mean is None and stress_mean is None:
                normalized_base_mean = 0.0
                normalized_stress_mean = 0.0
            else:
                normalized_base_mean = require_finite(
                    base_mean, "base_economics_invalid"
                )
                normalized_stress_mean = require_finite(
                    stress_mean, "stress_economics_invalid"
                )
            controls = architecture.get("oos_prediction_permutation_controls")
            if not isinstance(controls, list) or len(controls) != trials:
                raise ValueError("permutation_control_coverage_invalid")
            normalized_controls = []
            for expected_trial, raw_control in enumerate(controls):
                control = require_mapping(raw_control, "permutation_control_invalid")
                if control.get("trial") != expected_trial:
                    raise ValueError("permutation_control_order_invalid")
                control_base = require_mapping(
                    control.get("base_cost"), "permutation_base_invalid"
                )
                control_stress = require_mapping(
                    control.get("stress_cost"), "permutation_stress_invalid"
                )
                control_count = control_base.get("count")
                if (
                    isinstance(control_count, bool)
                    or not isinstance(control_count, int)
                    or control_count < 0
                    or control_stress.get("count") != control_count
                ):
                    raise ValueError("permutation_trade_count_invalid")
                raw_base_mean = control_base.get("mean_bps")
                raw_stress_mean = control_stress.get("mean_bps")
                if control_count == 0 and raw_base_mean is None and raw_stress_mean is None:
                    control_base_mean = 0.0
                    control_stress_mean = 0.0
                else:
                    control_base_mean = require_finite(
                        raw_base_mean, "permutation_base_invalid"
                    )
                    control_stress_mean = require_finite(
                        raw_stress_mean, "permutation_stress_invalid"
                    )
                normalized_controls.append(
                    {
                        "trial": expected_trial,
                        "trade_count": control_count,
                        "base_mean_bps": control_base_mean,
                        "stress_mean_bps": control_stress_mean,
                    }
                )
            action_counts = objective.get("action_counts", {})
            if not isinstance(action_counts, Mapping) or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in action_counts.values()
            ):
                raise ValueError("action_counts_invalid")
            normalized_architectures[architecture_id] = {
                "trade_count": base_count,
                "base_mean_bps": normalized_base_mean,
                "stress_mean_bps": normalized_stress_mean,
                "action_counts": {
                    str(key): action_counts[key] for key in sorted(action_counts)
                },
                "permutation_controls": normalized_controls,
            }
        evidence.append(
            {
                "split_id": expected_split_id,
                "partition_identity_sha256": partition_identity,
                "architectures": normalized_architectures,
            }
        )
    return intervals, evidence


def extract_evidence(report: Mapping[str, Any], report_sha256: str) -> dict[str, Any]:
    if (
        report.get("schema_version") != REPORT_SCHEMA_VERSION
        or report.get("status") not in {"PASS", "FAIL"}
        or not isinstance(report.get("fully_verifiable"), bool)
        or report.get("research_domain") != "forward_development_only"
        or report.get("promotion_evidence") is not False
        or report.get("promotion_eligible") is not False
    ):
        raise ValueError("development_report_not_fully_verifiable")
    source = require_mapping(report.get("source_assessment"), "source_assessment_invalid")
    source_sha256 = source.get("sha256")
    if not is_sha256(source_sha256):
        raise ValueError("source_assessment_identity_invalid")
    comparison = require_mapping(
        report.get("target_architecture_comparison"), "comparison_missing"
    )
    if (
        comparison.get("schema_version") != COMPARISON_SCHEMA_VERSION
        or comparison.get("fully_verifiable") is not True
        or comparison.get("promotion_evidence") is not False
        or comparison.get("promotion_eligible") is not False
        or comparison.get("influences_development_passed") is not False
        or comparison.get("frozen_contract_failures") != []
        or comparison.get("missing_architecture_splits") != []
    ):
        raise ValueError("comparison_not_fully_verifiable")
    summaries = require_mapping(
        comparison.get("architectures"), "architecture_summaries_invalid"
    )
    if set(summaries) != set(EXPECTED_ARCHITECTURES):
        raise ValueError("architecture_summary_coverage_invalid")
    for architecture_id in EXPECTED_ARCHITECTURES:
        summary = require_mapping(
            summaries.get(architecture_id), "architecture_summary_invalid"
        )
        if (
            summary.get("fully_verifiable") is not True
            or summary.get("complete_split_count") != REQUIRED_SPLIT_COUNT
            or summary.get("required_split_count") != REQUIRED_SPLIT_COUNT
        ):
            raise ValueError("architecture_summary_incomplete")
    shared = require_mapping(
        comparison.get("shared_contract"), "shared_contract_invalid"
    )
    data = require_mapping(report.get("data"), "development_data_invalid")
    feature_names = data.get("feature_names")
    if (
        not isinstance(feature_names, list)
        or not feature_names
        or any(not isinstance(value, str) or not value for value in feature_names)
        or len(set(feature_names)) != len(feature_names)
        or shared.get("feature_count") != len(feature_names)
        or shared.get("ordered_feature_names_sha256")
        != canonical_sha256({"feature_names": feature_names})
    ):
        raise ValueError("feature_contract_binding_invalid")
    target_contract = require_mapping(
        report.get("target_contract"), "target_contract_invalid"
    )
    if (
        shared.get("source_assessment_sha256") != source_sha256
        or shared.get("causal_feature_contract")
        != report.get("causal_feature_contract")
        or shared.get("actions") != target_contract.get("actions")
        or shared.get("action_count") != len(target_contract.get("actions", []))
        or shared.get("additional_round_trip_cost_bps")
        != target_contract.get("additional_round_trip_cost_bps")
        or shared.get("stress_cost_multiplier")
        != target_contract.get("stress_cost_multiplier")
        or shared.get("execution_latency_seconds")
        != target_contract.get("execution_latency_seconds")
        or shared.get("overlapping_episodes_forbidden")
        != target_contract.get("overlapping_episodes_forbidden")
    ):
        raise ValueError("target_contract_binding_invalid")
    model_contract = require_mapping(
        report.get("model_contract"), "model_contract_invalid"
    )
    hyperparameters = require_mapping(
        shared.get("model_hyperparameters"), "model_hyperparameters_invalid"
    )
    if any(model_contract.get(key) != value for key, value in hyperparameters.items()):
        raise ValueError("model_contract_binding_invalid")
    contract = information_set_contract(report, comparison)
    intervals, split_evidence = normalized_split_evidence(comparison)
    information_set_id = canonical_sha256(contract)
    batch = {
        "test_start_ms": intervals[0]["test_start_ms"],
        "test_end_ms": intervals[-1]["test_end_ms"],
        "split_count": len(intervals),
        "oos_duration_ms": sum(
            item["test_end_ms"] - item["test_start_ms"] for item in intervals
        ),
        "intervals": intervals,
    }
    evidence_content = {
        "information_set_id": information_set_id,
        "source_assessment_sha256": source_sha256,
        "development_report_sha256": report_sha256,
        "batch": batch,
        "split_evidence": split_evidence,
        "architecture_signal_proven": {
            architecture_id: summaries[architecture_id].get("signal_proven") is True
            for architecture_id in EXPECTED_ARCHITECTURES
        },
        "comparison_conclusion": comparison.get("conclusion"),
    }
    return {
        **evidence_content,
        "evidence_id": canonical_sha256(evidence_content),
        "information_set_contract": contract,
    }


def record_without_hash(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "record_sha256"}


def load_ledger(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    expected_keys = {
        "schema_version",
        "sequence",
        "previous_record_sha256",
        "record_sha256",
        "information_set_id",
        "information_set_contract",
        "evidence_id",
        "source_assessment_sha256",
        "development_report_sha256",
        "batch",
        "split_evidence",
        "architecture_signal_proven",
        "comparison_conclusion",
        "research_observation_only",
        "promotion_authority",
        "demo_activation_authorized",
        "live_activation_authorized",
    }
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("ledger_read_failed") from exc
    if not raw_lines or any(not line for line in raw_lines):
        raise ValueError("ledger_record_schema_invalid")
    previous_sha256: str | None = None
    for expected_sequence, line in enumerate(raw_lines, start=1):
        try:
            record = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("ledger_record_schema_invalid") from exc
        if not isinstance(record, dict) or set(record) != expected_keys:
            raise ValueError("ledger_record_schema_invalid")
        if line.encode("utf-8") != canonical_bytes(record):
            raise ValueError("ledger_canonical_encoding_invalid")
        if (
            record.get("schema_version") != SCHEMA_VERSION
            or record.get("sequence") != expected_sequence
            or record.get("previous_record_sha256") != previous_sha256
            or record.get("research_observation_only") is not True
            or record.get("promotion_authority") is not False
            or record.get("demo_activation_authorized") is not False
            or record.get("live_activation_authorized") is not False
            or not is_sha256(record.get("record_sha256"))
            or canonical_sha256(record_without_hash(record))
            != record.get("record_sha256")
        ):
            raise ValueError("ledger_record_integrity_invalid")
        previous_sha256 = record["record_sha256"]
        records.append(record)
    return records


def intervals_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        int(left["test_start_ms"]) < int(right["test_end_ms"])
        and int(right["test_start_ms"]) < int(left["test_end_ms"])
    )


def audit_payload(
    *, status: str, evidence: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    total_duration_ms = sum(int(record["batch"]["oos_duration_ms"]) for record in records)
    latest_end = max(
        (int(record["batch"]["test_end_ms"]) for record in records), default=None
    )
    reason = {
        "RECORDED": "independent_oos_regime_recorded",
        "DUPLICATE": "evidence_already_recorded",
        "SKIPPED_OVERLAP": "oos_regime_overlaps_accepted_evidence",
    }[status]
    no_architecture_signal_across_evidence = not any(
        bool(signal)
        for record in records
        for signal in record["architecture_signal_proven"].values()
    )
    stage_review_required = bool(
        len(records) >= 2 and no_architecture_signal_across_evidence
    )
    stage_review_charter = review_charter() if stage_review_required else None
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": status,
        "reason_codes": [reason],
        "information_set_id": evidence["information_set_id"],
        "evidence_id": evidence["evidence_id"],
        "batch": evidence["batch"],
        "accepted_batch_count": len(records),
        "independent_oos_hours": total_duration_ms / 3_600_000.0,
        "next_nonoverlap_test_start_ms": latest_end,
        "stage_review_required": stage_review_required,
        "stage_review_charter": stage_review_charter,
        "next_action": (
            "convene_stage_review_before_more_model_iterations"
            if stage_review_required
            else "collect_next_fully_non_overlapping_oos_regime"
        ),
        "research_observation_only": True,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }


def inspect_ledger(*, ledger_path: pathlib.Path, audit_path: pathlib.Path) -> dict[str, Any]:
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            records = load_ledger(ledger_path)
            total_duration_ms = sum(
                int(record["batch"]["oos_duration_ms"]) for record in records
            )
            no_signal = not any(
                bool(signal)
                for record in records
                for signal in record["architecture_signal_proven"].values()
            )
            review_required = bool(len(records) >= 2 and no_signal)
            audit = {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "status": (
                    "STAGE_REVIEW_REQUIRED" if review_required else "COLLECTING"
                ),
                "reason_codes": [
                    (
                        "independent_evidence_exhausted_current_research_path"
                        if review_required
                        else "awaiting_next_independent_oos_regime"
                    )
                ],
                "information_set_id": (
                    records[0]["information_set_id"] if records else None
                ),
                "evidence_id": None,
                "batch": None,
                "accepted_batch_count": len(records),
                "independent_oos_hours": total_duration_ms / 3_600_000.0,
                "next_nonoverlap_test_start_ms": max(
                    (
                        int(record["batch"]["test_end_ms"])
                        for record in records
                    ),
                    default=None,
                ),
                "stage_review_required": review_required,
                "stage_review_charter": (
                    review_charter() if review_required else None
                ),
                "next_action": (
                    "convene_stage_review_before_more_model_iterations"
                    if review_required
                    else "collect_next_fully_non_overlapping_oos_regime"
                ),
                "research_observation_only": True,
                "promotion_authority": False,
                "demo_activation_authorized": False,
                "live_activation_authorized": False,
            }
            atomic_write_json(audit_path, audit)
            return audit
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) else type(exc).__name__
        audit = failure_audit(reason)
        atomic_write_json(audit_path, audit)
        raise


def write_stage_review_terminal_artifacts(
    *,
    audit_path: pathlib.Path,
    audit: Mapping[str, Any],
    development_output: pathlib.Path,
    candidate_output: pathlib.Path,
) -> None:
    """Materialize the verified no-candidate terminal branch.

    The Full Loop artifact contract still requires a development report and a
    candidate manifest when the independent-evidence preflight stops training.
    These artifacts describe that fail-closed outcome; they never manufacture
    a model or candidate identity.
    """

    expected_reason = "independent_evidence_exhausted_current_research_path"
    if not (
        audit.get("schema_version") == AUDIT_SCHEMA_VERSION
        and audit.get("status") == "STAGE_REVIEW_REQUIRED"
        and audit.get("reason_codes") == [expected_reason]
        and audit.get("stage_review_required") is True
        and isinstance(audit.get("accepted_batch_count"), int)
        and not isinstance(audit.get("accepted_batch_count"), bool)
        and int(audit["accepted_batch_count"]) >= 2
        and require_finite(
            audit.get("independent_oos_hours"),
            "stage_review_independent_oos_hours_invalid",
        )
        >= 48.0
        and audit.get("next_action")
        == "convene_stage_review_before_more_model_iterations"
        and audit.get("research_observation_only") is True
        and audit.get("promotion_authority") is False
        and audit.get("demo_activation_authorized") is False
        and audit.get("live_activation_authorized") is False
    ):
        raise ValueError("stage_review_audit_contract_invalid")
    if len({audit_path, development_output, candidate_output}) != 3:
        raise ValueError("stage_review_output_paths_not_distinct")

    audit_reference = {
        "path": str(audit_path),
        "sha256": sha256_file(audit_path),
    }
    next_gate = str(audit["next_action"])
    development_report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "NOT_READY",
        "fully_verifiable": True,
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "stage_review_required": True,
        "terminal_research_status": "STAGE_REVIEW_REQUIRED",
        "information_set_id": audit.get("information_set_id"),
        "regime_evidence_audit": audit_reference,
        "frozen_candidate": None,
        "failures": [expected_reason],
        "next_gate": next_gate,
        "independent_selection_required": True,
        "untouched_final_holdout_required": True,
    }
    atomic_write_json(development_output, development_report)
    candidate_manifest = {
        "schema_version": CANDIDATE_MANIFEST_SCHEMA_VERSION,
        "status": "rejected",
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "candidate_id": None,
        "identity_contract": {
            "stage_review_required": True,
            "information_set_id": audit.get("information_set_id"),
            "regime_evidence_audit_sha256": audit_reference["sha256"],
        },
        "development_report": {
            "path": str(development_output),
            "sha256": sha256_file(development_output),
        },
        "next_gate": next_gate,
    }
    atomic_write_json(candidate_output, candidate_manifest)


def record_evidence(
    *, report_path: pathlib.Path, ledger_path: pathlib.Path, audit_path: pathlib.Path
) -> dict[str, Any]:
    try:
        raw_report = json.loads(report_path.read_text(encoding="utf-8"))
        report = require_mapping(raw_report, "development_report_invalid")
        evidence = extract_evidence(report, sha256_file(report_path))
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            records = load_ledger(ledger_path)
            if records and any(
                record["information_set_id"] != evidence["information_set_id"]
                or record["information_set_contract"]
                != evidence["information_set_contract"]
                for record in records
            ):
                raise ValueError("information_set_contract_drift")
            if any(record["evidence_id"] == evidence["evidence_id"] for record in records):
                audit = audit_payload(status="DUPLICATE", evidence=evidence, records=records)
            else:
                current_intervals = evidence["batch"]["intervals"]
                overlaps = any(
                    intervals_overlap(current, accepted)
                    for record in records
                    for current in current_intervals
                    for accepted in record["batch"]["intervals"]
                )
                if overlaps:
                    audit = audit_payload(
                        status="SKIPPED_OVERLAP", evidence=evidence, records=records
                    )
                else:
                    previous_sha256 = records[-1]["record_sha256"] if records else None
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "sequence": len(records) + 1,
                        "previous_record_sha256": previous_sha256,
                        "information_set_id": evidence["information_set_id"],
                        "information_set_contract": evidence["information_set_contract"],
                        "evidence_id": evidence["evidence_id"],
                        "source_assessment_sha256": evidence[
                            "source_assessment_sha256"
                        ],
                        "development_report_sha256": evidence[
                            "development_report_sha256"
                        ],
                        "batch": evidence["batch"],
                        "split_evidence": evidence["split_evidence"],
                        "architecture_signal_proven": evidence[
                            "architecture_signal_proven"
                        ],
                        "comparison_conclusion": evidence["comparison_conclusion"],
                        "research_observation_only": True,
                        "promotion_authority": False,
                        "demo_activation_authorized": False,
                        "live_activation_authorized": False,
                    }
                    record["record_sha256"] = canonical_sha256(record)
                    atomic_append_canonical_record(ledger_path, record)
                    records = [*records, record]
                    audit = audit_payload(
                        status="RECORDED", evidence=evidence, records=records
                    )
            atomic_write_json(audit_path, audit)
            return audit
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) else type(exc).__name__
        audit = failure_audit(reason)
        atomic_write_json(audit_path, audit)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record independent frozen-contract microstructure OOS evidence"
    )
    parser.add_argument("--report")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--audit-output", required=True)
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--stage-review-development-output")
    parser.add_argument("--stage-review-candidate-output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    audit_path = pathlib.Path(args.audit_output).resolve()
    try:
        stage_review_outputs = (
            args.stage_review_development_output,
            args.stage_review_candidate_output,
        )
        if bool(stage_review_outputs[0]) != bool(stage_review_outputs[1]):
            raise ValueError("stage_review_outputs_must_be_paired")
        if args.inspect_only:
            if args.report:
                raise ValueError("inspect_only_report_forbidden")
            audit = inspect_ledger(
                ledger_path=pathlib.Path(args.ledger).resolve(),
                audit_path=audit_path,
            )
            if audit["stage_review_required"] and stage_review_outputs[0]:
                write_stage_review_terminal_artifacts(
                    audit_path=audit_path,
                    audit=audit,
                    development_output=pathlib.Path(
                        stage_review_outputs[0]
                    ).resolve(),
                    candidate_output=pathlib.Path(
                        stage_review_outputs[1]
                    ).resolve(),
                )
            exit_code = 3 if audit["stage_review_required"] else 0
        else:
            if any(stage_review_outputs):
                raise ValueError("stage_review_outputs_require_inspect_only")
            if not args.report:
                raise ValueError("report_required")
            audit = record_evidence(
                report_path=pathlib.Path(args.report).resolve(),
                ledger_path=pathlib.Path(args.ledger).resolve(),
                audit_path=audit_path,
            )
            exit_code = 0
    except Exception:
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        except Exception:
            audit = failure_audit("audit_write_failed")
        exit_code = 2
    print(json.dumps(audit, ensure_ascii=False, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
