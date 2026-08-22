#!/usr/bin/env python3
"""Run the frozen Bybit-control vs SOL all-liquidation information-set A/B."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import pathlib
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np

import assess_liquidation_capture as liquidation_assessor
import collect_bybit_liquidations as liquidation_collector
import run_cross_venue_information_set_experiment as common
import run_microstructure_alpha_development as development


SCHEMA_VERSION = "liquidation_information_set_experiment_v1"
POLICY_SCHEMA_VERSION = "liquidation_information_set_experiment_policy_v1"
ARCHITECTURE_ID = common.ARCHITECTURE_ID
FROZEN_POLICY_IDENTITY_SHA256 = "5d9f97e44a27d7e2cbb5f2d2946b7fa5996c6d5cc893346aa797a939dacd4930"
ExperimentNotReady = common.ExperimentNotReady

_CAPTURE_FAILURE_CODES = {
    "invalid_segment_contract",
    "collector_health",
    "minimum_forward_capture_duration",
    "capture_freshness",
}


def validate_policy(path: pathlib.Path) -> Dict[str, Any]:
    policy = common.read_json(path)
    failures: List[str] = []
    if common.canonical_sha256(policy) != FROZEN_POLICY_IDENTITY_SHA256:
        failures.append("policy_identity")
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        failures.append("schema_version")
    if policy.get("research_domain") != "development_only" or policy.get("promotion_evidence") is not False:
        failures.append("research_domain")
    if policy.get("architecture_id") != ARCHITECTURE_ID:
        failures.append("architecture")
    alignment = policy.get("liquidation_alignment", {})
    if not (
        isinstance(alignment, dict)
        and alignment.get("lag_seconds") == 1
        and alignment.get("method") == "complete_connected_previous_second_bucket"
        and alignment.get("rolling_windows_seconds") == [1, 5, 20, 60]
        and alignment.get("time_since_cap_seconds") == 60
        and alignment.get("future_fill_permitted") is False
        and alignment.get("backfill_permitted") is False
    ):
        failures.append("liquidation_alignment")
    actions = policy.get("actions", {})
    if not (
        isinstance(actions, dict)
        and actions.get("directions") == ["long", "short"]
        and actions.get("horizons_seconds") == [15, 30, 60, 120, 300]
        and actions.get("execution_latency_seconds") == 1
    ):
        failures.append("actions")
    costs = policy.get("costs", {})
    if not (
        isinstance(costs, dict)
        and float(costs.get("additional_round_trip_cost_bps", 0.0)) == 11.0
        and float(costs.get("stress_cost_multiplier", 0.0)) == 1.25
    ):
        failures.append("costs")
    splits = policy.get("splits", {})
    if not (
        isinstance(splits, dict) and splits.get("count") == 6
        and splits.get("train_window_seconds") == 21600
        and splits.get("validation_window_seconds") == 14400
        and splits.get("test_window_seconds") == 14400
        and splits.get("rolling_step_seconds") == 14400
        and splits.get("model_selection_window_seconds") == 3600
        and splits.get("minimum_window_rows") == 3600
    ):
        failures.append("splits")
    if policy.get("authorities") != {
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }:
        failures.append("authorities")
    if failures:
        raise ValueError("frozen liquidation experiment policy mismatch: " + ",".join(failures))
    return policy


def validate_liquidation_assessment(path: pathlib.Path) -> Dict[str, Any]:
    payload = common.read_json(path)
    if not (
        payload.get("schema_version") == liquidation_assessor.SCHEMA_VERSION
        and payload.get("status") == "PASS"
        and payload.get("fully_verifiable") is True
        and payload.get("development_screen_ready") is True
        and payload.get("research_domain") == "forward_development_only"
        and payload.get("promotion_evidence") is False
        and payload.get("promotion_eligible") is False
        and payload.get("promotion_authority") is False
        and payload.get("demo_activation_authorized") is False
        and payload.get("live_activation_authorized") is False
        and payload.get("source") == "bybit_public_websocket_v5_all_liquidation"
        and payload.get("symbol") == liquidation_collector.SYMBOL
        and payload.get("alignment_contract") == liquidation_collector.ALIGNMENT_CONTRACT
        and isinstance(payload.get("segments"), list)
        and payload["segments"]
    ):
        raise ExperimentNotReady("liquidation capture has not passed readiness")
    return payload


def load_liquidation_rows(assessment: Mapping[str, Any]) -> Tuple[Dict[str, np.ndarray], List[Tuple[int, int]]]:
    buckets: Dict[int, np.ndarray] = {}
    intervals: List[Tuple[int, int]] = []
    previous_start = -1
    for item in assessment["segments"]:
        if not isinstance(item, dict):
            raise ValueError("liquidation segment manifest item is invalid")
        path = pathlib.Path(str(item.get("feature_path") or ""))
        report_path = pathlib.Path(str(item.get("report_path") or ""))
        start = int(item.get("capture_started_epoch_ms", -1))
        end = int(item.get("capture_completed_epoch_ms", -1))
        if not (
            item.get("capture_schema_version") == liquidation_collector.SCHEMA_VERSION
            and item.get("symbol") == liquidation_collector.SYMBOL
            and start >= previous_start and end > start
            and path.is_file() and report_path.is_file()
            and common.sha256_file(path) == item.get("feature_sha256")
            and common.sha256_file(report_path) == item.get("report_sha256")
        ):
            raise ValueError("liquidation segment identity/ordering mismatch")
        row_count = event_count = 0
        last_timestamp = -1
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != liquidation_collector.OUTPUT_FIELDS:
                raise ValueError("liquidation feature columns/order mismatch")
            for raw in reader:
                timestamp = int(raw["timestamp"])
                if timestamp <= last_timestamp:
                    raise ValueError("liquidation timestamps are not increasing")
                values = np.asarray([float(raw[field]) for field in liquidation_collector.OUTPUT_FIELDS[1:]], dtype=np.float64)
                counts = int(values[0]) + int(values[3])
                if (
                    counts <= 0
                    or values[0] != float(int(values[0]))
                    or values[3] != float(int(values[3]))
                    or not np.all(np.isfinite(values))
                    or np.min(values) < 0.0
                ):
                    raise ValueError("liquidation feature values are invalid")
                buckets[timestamp] = buckets.get(timestamp, np.zeros(6, dtype=np.float64)) + values
                event_count += counts
                row_count += 1
                last_timestamp = timestamp
        if row_count != int(item.get("feature_row_count", -1)) or event_count != int(item.get("liquidation_event_count", -1)):
            raise ValueError("liquidation segment row/event count mismatch")
        intervals.append((start, end))
        previous_start = start
    merged = liquidation_assessor.merge_intervals(intervals)
    recorded = assessment.get("coverage_intervals")
    expected = [{"start_epoch_ms": start, "end_epoch_ms": end} for start, end in merged]
    if recorded != expected:
        raise ValueError("liquidation coverage interval audit mismatch")
    timestamps = np.asarray(sorted(buckets), dtype=np.int64)
    matrix = np.asarray([buckets[int(ts)] for ts in timestamps], dtype=np.float64) if len(timestamps) else np.empty((0, 6), dtype=np.float64)
    return {
        "timestamp": timestamps,
        **{field: matrix[:, index] for index, field in enumerate(liquidation_collector.OUTPUT_FIELDS[1:])},
    }, merged


def _fully_covered(starts: np.ndarray, ends: np.ndarray, intervals: Sequence[Tuple[int, int]]) -> np.ndarray:
    result = np.zeros(len(starts), dtype=bool)
    for start, end in intervals:
        result |= (starts >= int(start)) & (ends <= int(end))
    return result


def _range_sums(event_timestamps: np.ndarray, values: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    prefix = np.concatenate(([0.0], np.cumsum(values, dtype=np.float64)))
    left = np.searchsorted(event_timestamps, starts, side="left")
    right = np.searchsorted(event_timestamps, ends, side="right")
    return prefix[right] - prefix[left]


def build_liquidation_features(
    control: Mapping[str, np.ndarray], sidecar: Mapping[str, np.ndarray], *,
    coverage_intervals: Sequence[Tuple[int, int]], lag_seconds: int,
    rolling_windows_seconds: Sequence[int], time_since_cap_seconds: int,
) -> Tuple[np.ndarray, List[str], Dict[str, Any]]:
    decisions = np.asarray(control["timestamp"], dtype=np.int64)
    source = decisions - int(lag_seconds) * 1000
    lookback_seconds = max(max(int(value) for value in rolling_windows_seconds), int(time_since_cap_seconds))
    covered = _fully_covered(
        source - (lookback_seconds - 1) * 1000,
        source + 1000,
        coverage_intervals,
    )
    event_timestamps = np.asarray(sidecar["timestamp"], dtype=np.int64)
    raw = {field: np.asarray(sidecar[field], dtype=np.float64) for field in liquidation_collector.OUTPUT_FIELDS[1:]}
    names: List[str] = []
    columns: List[np.ndarray] = []
    for window in rolling_windows_seconds:
        starts = source - (int(window) - 1) * 1000
        long_count = _range_sums(event_timestamps, raw["long_liquidation_count"], starts, source)
        long_qty = _range_sums(event_timestamps, raw["long_liquidation_qty"], starts, source)
        long_notional = _range_sums(event_timestamps, raw["long_liquidation_notional"], starts, source)
        short_count = _range_sums(event_timestamps, raw["short_liquidation_count"], starts, source)
        short_qty = _range_sums(event_timestamps, raw["short_liquidation_qty"], starts, source)
        short_notional = _range_sums(event_timestamps, raw["short_liquidation_notional"], starts, source)
        total_notional = long_notional + short_notional
        imbalance = np.divide(short_notional - long_notional, total_notional, out=np.zeros_like(total_notional), where=total_notional > 0.0)
        for metric, values in (
            ("long_count", long_count), ("long_qty", long_qty), ("long_notional", long_notional),
            ("short_count", short_count), ("short_qty", short_qty), ("short_notional", short_notional),
            ("signed_notional_imbalance", imbalance),
        ):
            values = np.asarray(values, dtype=np.float64)
            values[~covered] = np.nan
            names.append(f"liquidation_lag{lag_seconds}_{metric}_sum_{window}s")
            columns.append(values)
    for label, count_field in (
        ("long", "long_liquidation_count"),
        ("short", "short_liquidation_count"),
        ("any", ""),
    ):
        event_mask = (
            raw["long_liquidation_count"] + raw["short_liquidation_count"] > 0
            if not count_field else raw[count_field] > 0
        )
        relevant = event_timestamps[event_mask]
        positions = np.searchsorted(relevant, source, side="right") - 1
        since = np.full(len(source), float(time_since_cap_seconds), dtype=np.float64)
        present = positions >= 0
        since[present] = np.minimum(
            (source[present] - relevant[positions[present]]) / 1000.0,
            float(time_since_cap_seconds),
        )
        if len(relevant):
            lower_bound = source - (int(time_since_cap_seconds) - 1) * 1000
            outside_history = present & (
                relevant[np.maximum(positions, 0)] < lower_bound
            )
            since[outside_history] = float(time_since_cap_seconds)
        since[~covered] = np.nan
        names.append(f"liquidation_lag{lag_seconds}_seconds_since_{label}")
        columns.append(since)
    matrix = np.column_stack(columns)
    return matrix, names, {
        "method": "complete_connected_previous_second_bucket_with_causal_rolling_v1",
        "lag_seconds": int(lag_seconds),
        "rolling_windows_seconds": [int(value) for value in rolling_windows_seconds],
        "time_since_cap_seconds": int(time_since_cap_seconds),
        "required_history_seconds": lookback_seconds,
        "control_row_count": len(decisions),
        "aligned_row_count": int(np.sum(covered)),
        "uncovered_row_count": int(np.sum(~covered)),
        "zero_event_bucket_semantics": "zero_only_inside_checksum_bound_continuous_connection_interval",
        "future_fill_permitted": False,
        "backfill_permitted": False,
        "source_timestamp_relation": "liquidation_bucket_start=decision_timestamp-1000ms",
    }


def run_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    policy_path = pathlib.Path(args.config).resolve()
    control_path = pathlib.Path(args.control_assessment).resolve()
    treatment_path = pathlib.Path(args.treatment_assessment).resolve()
    policy = validate_policy(policy_path)
    control_assessment = development.validate_capture_assessment(control_path)
    liquidation_assessment = validate_liquidation_assessment(treatment_path)
    control = development.load_capture_rows(control_assessment)
    sidecar, intervals = load_liquidation_rows(liquidation_assessment)
    control_features, control_feature_names = development.build_causal_features(control)
    if len(control_feature_names) != development.FROZEN_TARGET_ARCHITECTURE_FEATURE_COUNT:
        raise ValueError("control feature contract is not the frozen 242-column set")
    alignment = policy["liquidation_alignment"]
    liquidation_features, liquidation_feature_names, alignment_audit = build_liquidation_features(
        control, sidecar, coverage_intervals=intervals,
        lag_seconds=int(alignment["lag_seconds"]),
        rolling_windows_seconds=alignment["rolling_windows_seconds"],
        time_since_cap_seconds=int(alignment["time_since_cap_seconds"]),
    )
    treatment_features = np.column_stack((control_features, liquidation_features))
    outcomes, actions = development.build_joint_action_returns(
        control, horizons_seconds=policy["actions"]["horizons_seconds"],
        execution_latency_seconds=int(policy["actions"]["execution_latency_seconds"]),
        additional_round_trip_cost_bps=float(policy["costs"]["additional_round_trip_cost_bps"]),
    )
    timestamps = np.asarray(control["timestamp"], dtype=np.int64)
    eligible = np.all(np.isfinite(control_features), axis=1) & np.all(np.isfinite(liquidation_features), axis=1) & np.all(np.isfinite(outcomes), axis=1)
    timestamps, control_features, treatment_features, outcomes = timestamps[eligible], control_features[eligible], treatment_features[eligible], outcomes[eligible]
    if len(timestamps) < int(policy["splits"]["minimum_window_rows"]):
        raise ExperimentNotReady("insufficient common causal rows")
    if not (
        np.array_equal(treatment_features[:, : control_features.shape[1]], control_features)
        and len(actions) == development.FROZEN_TARGET_ARCHITECTURE_ACTION_COUNT
    ):
        raise ValueError("single-variable A/B contract failed")
    embargo = max(policy["actions"]["horizons_seconds"]) + int(policy["actions"]["execution_latency_seconds"])
    splits = development.build_time_splits(
        timestamps, n_splits=int(policy["splits"]["count"]),
        train_window_seconds=int(policy["splits"]["train_window_seconds"]),
        validation_window_seconds=int(policy["splits"]["validation_window_seconds"]),
        test_window_seconds=int(policy["splits"]["test_window_seconds"]),
        rolling_step_seconds=int(policy["splits"]["rolling_step_seconds"]), embargo_seconds=embargo,
    )
    control_arm = common.evaluate_arm(arm_id="A_CONTROL", timestamps=timestamps, features=control_features, outcomes=outcomes, actions=actions, splits=splits, policy=policy)
    treatment_arm = common.evaluate_arm(arm_id="B_TREATMENT", timestamps=timestamps, features=treatment_features, outcomes=outcomes, actions=actions, splits=splits, policy=policy)
    oracle = common.build_oracle(timestamps, outcomes, actions, splits, policy)
    paired = common.build_paired_delta(control_arm, treatment_arm, policy)
    decision, reasons = common.decide(oracle=oracle, treatment=treatment_arm, paired=paired, policy=policy)
    fully_verifiable = decision != "NOT_READY"
    return {
        "schema_version": SCHEMA_VERSION, "status": "COMPLETE" if fully_verifiable else "NOT_READY",
        "fully_verifiable": fully_verifiable, "research_domain": "forward_development_only",
        "promotion_evidence": False, "promotion_eligible": False, "promotion_authority": False,
        "demo_activation_authorized": False, "live_activation_authorized": False,
        "experiment_id": policy["experiment_id"],
        "experiment_policy": {"path": str(policy_path), "sha256": common.sha256_file(policy_path), "identity_sha256": common.canonical_sha256(policy)},
        "inputs": {"control_assessment": {"path": str(control_path), "sha256": common.sha256_file(control_path)},
                   "treatment_assessment": {"path": str(treatment_path), "sha256": common.sha256_file(treatment_path)}},
        "single_variable_audit": {
            "changed_variable": policy["single_variable_change"],
            "control_feature_count": len(control_feature_names),
            "treatment_feature_count": len(control_feature_names) + len(liquidation_feature_names),
            "control_feature_names_sha256": common.canonical_sha256({"names": control_feature_names}),
            "liquidation_feature_names": liquidation_feature_names,
            "control_prefix_bytes_identical": True, "outcome_matrix_shared": True,
            "split_calendar_shared": True, "model_hyperparameters_shared": True,
            "actions_costs_latency_shared": True, "liquidation_alignment": alignment_audit,
        },
        "common_domain": {"row_count": len(timestamps), "first_timestamp_ms": int(timestamps[0]),
                          "last_timestamp_ms": int(timestamps[-1]), "timestamp_sha256": common.array_sha256(timestamps),
                          "outcomes_sha256": common.array_sha256(outcomes), "splits": [dataclasses.asdict(split) for split in splits]},
        "hindsight_oracle": oracle, "arms": {"control": control_arm, "treatment": treatment_arm},
        "paired_treatment_minus_control": paired, "research_decision": decision, "reason_codes": reasons,
        "next_action": {
            "STOP_CURRENT_RESEARCH_FAMILY": "close_sol_15_to_300_second_action_target_family",
            "STOP_INFORMATION_SOURCE": "close_bybit_sol_all_liquidation_information_source_and_current_family",
            "CONTINUE_TO_SECOND_INDEPENDENT_24H": "collect_and_run_second_non_overlapping_window",
            "NOT_READY": "complete_first_common_capture_window",
        }[decision],
    }


def _capture_readiness(path: pathlib.Path) -> Dict[str, Any]:
    try:
        payload = common.read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"artifact_status": "MISSING_OR_INVALID"}
    result: Dict[str, Any] = {"artifact_status": "PRESENT"}
    status = payload.get("status")
    if status in {"PASS", "FAIL", "NOT_READY"}:
        result["status"] = status
    for field in (
        "coverage_ms",
        "minimum_coverage_ms",
        "freshness_age_ms",
        "feature_row_count",
        "liquidation_event_count",
    ):
        value = payload.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            result[field] = value
    coverage = result.get("coverage_ms")
    minimum = result.get("minimum_coverage_ms")
    if isinstance(coverage, (int, float)) and isinstance(minimum, (int, float)) and minimum > 0:
        result["missing_coverage_ms"] = max(0, minimum - coverage)
        result["coverage_ratio"] = max(0.0, min(float(coverage) / float(minimum), 1.0))
    health = payload.get("collector_health")
    if isinstance(health, Mapping) and health.get("status") in {"PASS", "FAIL"}:
        result["collector_health_status"] = health["status"]
    failures = payload.get("failures")
    if isinstance(failures, list):
        result["failure_codes"] = [
            value
            for value in failures
            if isinstance(value, str) and value in _CAPTURE_FAILURE_CODES
        ]
    return result


def not_ready_report(
    args: argparse.Namespace,
    reason_code: str,
    *,
    not_ready_stage: str,
    status: str = "NOT_READY",
) -> Dict[str, Any]:
    config = pathlib.Path(args.config).resolve()
    try:
        policy = common.read_json(config) if config.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError):
        policy = {}
    splits, actions, alignment = policy.get("splits", {}), policy.get("actions", {}), policy.get("liquidation_alignment", {})
    embargo = max(actions.get("horizons_seconds", [300])) + int(actions.get("execution_latency_seconds", 1))
    required = ((int(splits.get("count", 6)) - 1) * int(splits.get("rolling_step_seconds", 14400))
                + int(splits.get("test_window_seconds", 14400)) + int(splits.get("validation_window_seconds", 14400))
                + int(splits.get("train_window_seconds", 21600)) + 2 * embargo
                + int(alignment.get("time_since_cap_seconds", 60)))
    control_readiness = _capture_readiness(pathlib.Path(args.control_assessment).resolve())
    liquidation_readiness = _capture_readiness(pathlib.Path(args.treatment_assessment).resolve())
    reason_codes = [reason_code]
    for value in liquidation_readiness.get("failure_codes", []):
        if value not in reason_codes:
            reason_codes.append(value)
    return {
        "schema_version": SCHEMA_VERSION, "status": status, "fully_verifiable": False,
        "research_domain": "forward_development_only", "promotion_evidence": False,
        "promotion_eligible": False, "promotion_authority": False,
        "demo_activation_authorized": False, "live_activation_authorized": False,
        "research_decision": "NOT_READY", "reason_codes": reason_codes,
        "not_ready_stage": not_ready_stage,
        "capture_readiness": {
            "control": control_readiness,
            "liquidation": liquidation_readiness,
        },
        "minimum_common_span_seconds_for_frozen_splits": required,
        "next_action": "continue_liquidation_capture",
        "experiment_policy": {"path": str(config), "sha256": common.sha256_file(config) if config.is_file() else None},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-assessment", required=True)
    parser.add_argument("--treatment-assessment", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--research-domain", default="development", choices=("development",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = run_experiment(args)
    except development.CaptureNotReady:
        report = not_ready_report(
            args,
            "control_capture_not_ready",
            not_ready_stage="control_capture",
        )
    except ExperimentNotReady as exc:
        message = str(exc)
        if message == "liquidation capture has not passed readiness":
            reason_code = "liquidation_capture_not_ready"
            stage = "liquidation_capture"
        elif message == "insufficient common causal rows" or message.startswith("split "):
            reason_code = "insufficient_common_causal_rows"
            stage = "common_causal_domain"
        else:
            reason_code = "experiment_input_not_ready"
            stage = "experiment_input"
        report = not_ready_report(args, reason_code, not_ready_stage=stage)
    except Exception:
        report = not_ready_report(
            args,
            "invalid_input",
            not_ready_stage="invalid_input",
            status="INVALID_INPUT",
        )
    common.atomic_write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report.get("status") == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
