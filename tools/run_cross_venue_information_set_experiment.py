#!/usr/bin/env python3
"""Run the frozen control-vs-external-venue information-set experiment."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import math
import pathlib
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np

import assess_binance_microstructure_capture as external_assessor
import collect_binance_microstructure as external_collector
import run_microstructure_alpha_development as development


SCHEMA_VERSION = "cross_venue_information_set_experiment_v1"
POLICY_SCHEMA_VERSION = "cross_venue_information_set_experiment_policy_v1"
ARCHITECTURE_ID = "direct_stress_utility_regression"
FROZEN_POLICY_IDENTITY_SHA256 = (
    "c3539aa6e8374a6b3df1eed41a57d6789d1e143eccae09b06b48c9a53ff78918"
)


class ExperimentNotReady(RuntimeError):
    pass


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def array_sha256(values: np.ndarray) -> str:
    array = np.ascontiguousarray(values)
    digest = hashlib.sha256()
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(str(array.shape).encode("ascii"))
    digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON payload is not an object: {path}")
    return payload


def validate_policy(path: pathlib.Path) -> Dict[str, Any]:
    policy = read_json(path)
    failures: List[str] = []
    if canonical_sha256(policy) != FROZEN_POLICY_IDENTITY_SHA256:
        failures.append("policy_identity")
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        failures.append("schema_version")
    if policy.get("research_domain") != "development_only":
        failures.append("research_domain")
    if policy.get("promotion_evidence") is not False:
        failures.append("promotion_evidence")
    if policy.get("architecture_id") != ARCHITECTURE_ID:
        failures.append("architecture")
    alignment = policy.get("external_alignment", {})
    if not (
        isinstance(alignment, dict)
        and alignment.get("lag_seconds") == 1
        and alignment.get("method") == "exact_exchange_second_inner_join"
        and alignment.get("future_fill_permitted") is False
        and alignment.get("backfill_permitted") is False
    ):
        failures.append("external_alignment")
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
        isinstance(splits, dict)
        and splits.get("count") == 6
        and splits.get("train_window_seconds") == 21600
        and splits.get("validation_window_seconds") == 14400
        and splits.get("test_window_seconds") == 14400
        and splits.get("rolling_step_seconds") == 14400
        and splits.get("model_selection_window_seconds") == 3600
    ):
        failures.append("splits")
    authorities = policy.get("authorities", {})
    if authorities != {
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }:
        failures.append("authorities")
    if failures:
        raise ValueError("frozen experiment policy mismatch: " + ",".join(failures))
    return policy


def validate_external_assessment(path: pathlib.Path) -> Dict[str, Any]:
    payload = read_json(path)
    if not (
        payload.get("schema_version") == external_assessor.SCHEMA_VERSION
        and payload.get("status") == "PASS"
        and payload.get("fully_verifiable") is True
        and payload.get("development_screen_ready") is True
        and payload.get("research_domain") == "forward_development_only"
        and payload.get("promotion_evidence") is False
        and payload.get("promotion_eligible") is False
        and payload.get("demo_activation_authorized") is False
        and payload.get("live_activation_authorized") is False
        and payload.get("symbol") == external_collector.SYMBOL
        and payload.get("source") == "binance_usdm_public_websocket"
        and payload.get("alignment_contract") == external_collector.ALIGNMENT_CONTRACT
        and isinstance(payload.get("segments"), list)
        and payload["segments"]
    ):
        raise ExperimentNotReady("external venue capture has not passed readiness")
    return payload


def load_external_rows(assessment: Mapping[str, Any]) -> Dict[str, np.ndarray]:
    rows_by_timestamp: Dict[int, Tuple[float, ...]] = {}
    owner_bounds: Dict[int, Tuple[int, int]] = {}
    dropped: set[int] = set()
    ordered_segments = assessment.get("segments")
    if not isinstance(ordered_segments, list) or not ordered_segments:
        raise ValueError("external segment manifest is empty")
    previous_start = -1
    previous_end = -1
    for segment_index, item in enumerate(ordered_segments):
        if not isinstance(item, dict):
            raise ValueError("external segment manifest item is invalid")
        path = pathlib.Path(str(item.get("feature_path") or ""))
        expected_hash = str(item.get("feature_sha256") or "")
        start = int(item.get("first_timestamp_ms", -1))
        end = int(item.get("last_timestamp_ms", -1))
        if not (
            item.get("capture_schema_version") == external_collector.SCHEMA_VERSION
            and item.get("symbol") == external_collector.SYMBOL
            and path.is_file()
            and len(expected_hash) == 64
            and sha256_file(path) == expected_hash
            and 0 <= start <= end
            and start >= previous_start
            and (segment_index == 0 or start >= previous_end)
        ):
            raise ValueError("external segment identity/ordering mismatch")
        count = 0
        local_first = local_last = None
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != external_collector.OUTPUT_FIELDS:
                raise ValueError("external feature columns/order mismatch")
            for raw in reader:
                timestamp = int(raw["timestamp"])
                if local_last is not None and timestamp <= local_last:
                    raise ValueError("external feature timestamps are not increasing")
                values = tuple(float(raw[name]) for name in external_collector.OUTPUT_FIELDS[1:])
                if not all(math.isfinite(value) for value in values):
                    raise ValueError("external feature value is non-finite")
                if timestamp in dropped:
                    raise ValueError("external timestamp appears in more than two segments")
                if timestamp in rows_by_timestamp:
                    prior_owner = owner_bounds[timestamp]
                    if not (
                        segment_index > 0
                        and prior_owner == (previous_start, previous_end)
                        and timestamp == start == previous_end
                    ):
                        raise ValueError("external non-boundary duplicate timestamp")
                    rows_by_timestamp.pop(timestamp)
                    owner_bounds.pop(timestamp)
                    dropped.add(timestamp)
                else:
                    rows_by_timestamp[timestamp] = values
                    owner_bounds[timestamp] = (start, end)
                count += 1
                local_first = timestamp if local_first is None else local_first
                local_last = timestamp
        if not (
            count == int(item.get("feature_row_count", -1))
            and local_first == start
            and local_last == end
        ):
            raise ValueError("external segment row-count/bounds mismatch")
        previous_start, previous_end = start, end
    if not rows_by_timestamp:
        raise ValueError("external capture contains no usable rows")
    timestamps = np.asarray(sorted(rows_by_timestamp), dtype=np.int64)
    matrix = np.asarray([rows_by_timestamp[int(ts)] for ts in timestamps], dtype=np.float64)
    return {
        "timestamp": timestamps,
        **{
            name: matrix[:, index]
            for index, name in enumerate(external_collector.OUTPUT_FIELDS[1:])
        },
    }


def build_external_features(
    control: Mapping[str, np.ndarray], external: Mapping[str, np.ndarray], *, lag_seconds: int
) -> Tuple[np.ndarray, List[str], Dict[str, Any]]:
    control_timestamps = np.asarray(control["timestamp"], dtype=np.int64)
    external_timestamps = np.asarray(external["timestamp"], dtype=np.int64)
    positions = {int(ts): index for index, ts in enumerate(external_timestamps)}
    lag_ms = int(lag_seconds) * 1000
    source_indices = np.asarray(
        [positions.get(int(ts) - lag_ms, -1) for ts in control_timestamps],
        dtype=np.int64,
    )
    available = source_indices >= 0
    names: List[str] = []
    columns: List[np.ndarray] = []

    def add(name: str, values: np.ndarray) -> None:
        names.append(name)
        columns.append(np.asarray(values, dtype=np.float64))

    for field in external_collector.OUTPUT_FIELDS[1:]:
        values = np.full(len(control_timestamps), np.nan, dtype=np.float64)
        values[available] = np.asarray(external[field], dtype=np.float64)[source_indices[available]]
        add(f"binance_lag1_{field}", values)

    def external_value(field: str) -> np.ndarray:
        return columns[names.index(f"binance_lag1_{field}")]

    external_mid = external_value("mid")
    control_mid = np.asarray(control["mid"], dtype=np.float64)
    add("cross_venue_mid_basis_bps", (external_mid / control_mid - 1.0) * 10000.0)
    add(
        "cross_venue_spread_delta_bps",
        external_value("spread_bps") - np.asarray(control["spread_bps"], dtype=np.float64),
    )
    for level in ("l1", "l5", "l20"):
        field = f"book_imbalance_{level}"
        add(
            f"cross_venue_{field}_delta",
            external_value(field) - np.asarray(control[field], dtype=np.float64),
        )
    add(
        "cross_venue_trade_imbalance_delta",
        external_value("trade_imbalance")
        - np.asarray(control["trade_imbalance"], dtype=np.float64),
    )
    add(
        "binance_lag1_microprice_dislocation_bps",
        (external_value("microprice") / external_mid - 1.0) * 10000.0,
    )
    prior_positions = {int(ts): index for index, ts in enumerate(external_timestamps)}
    external_return = np.full(len(control_timestamps), np.nan, dtype=np.float64)
    for row_index, source_index in enumerate(source_indices):
        if source_index < 0:
            continue
        prior_index = prior_positions.get(int(external_timestamps[source_index]) - 1000)
        if prior_index is not None:
            external_return[row_index] = (
                float(external["mid"][source_index]) / float(external["mid"][prior_index]) - 1.0
            ) * 10000.0
    add("binance_lag1_mid_return_1s_bps", external_return)
    matrix = np.column_stack(columns)
    audit = {
        "method": "exact_exchange_second_t_minus_one_v1",
        "lag_seconds": int(lag_seconds),
        "control_row_count": len(control_timestamps),
        "aligned_row_count": int(np.sum(np.all(np.isfinite(matrix), axis=1))),
        "missing_external_row_count": int(np.sum(~available)),
        "future_fill_permitted": False,
        "backfill_permitted": False,
        "source_timestamp_relation": "external_bucket_start=decision_timestamp-1000ms",
    }
    return matrix, names, audit


def model_args(policy: Mapping[str, Any]) -> SimpleNamespace:
    model = policy["model"]
    return SimpleNamespace(**{key: model[key] for key in model})


def _mean_or_zero(summary: Mapping[str, Any]) -> float | None:
    value = summary.get("mean_bps")
    if value is not None:
        return float(value)
    return 0.0 if int(summary.get("count") or 0) == 0 else None


def validate_split_row_coverage(
    *,
    split_id: int,
    model_fit: Sequence[Any],
    model_selection: Sequence[Any],
    validation: Sequence[Any],
    test: Sequence[Any],
    split_policy: Mapping[str, Any],
) -> Dict[str, Dict[str, int]]:
    minimum = int(split_policy["minimum_window_rows"])
    model_selection_minimum = development.minimum_internal_model_selection_rows(
        minimum_window_rows=minimum,
        model_selection_window_seconds=int(
            split_policy["model_selection_window_seconds"]
        ),
        train_window_seconds=int(split_policy["train_window_seconds"]),
    )
    actual_rows = {
        "model_fit": len(model_fit),
        "model_selection": len(model_selection),
        "validation": len(validation),
        "test": len(test),
    }
    minimum_rows = {
        "model_fit": minimum,
        "model_selection": model_selection_minimum,
        "validation": minimum,
        "test": minimum,
    }
    failures = [
        f"{window}={actual_rows[window]}<{minimum_rows[window]}"
        for window in actual_rows
        if actual_rows[window] < minimum_rows[window]
    ]
    if failures:
        raise ExperimentNotReady(
            f"split {split_id} has insufficient common rows: " + ",".join(failures)
        )
    return {"actual_rows": actual_rows, "minimum_rows": minimum_rows}


def evaluate_arm(
    *,
    arm_id: str,
    timestamps: np.ndarray,
    features: np.ndarray,
    outcomes: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    splits: Sequence[development.TimeSplit],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    costs, split_policy = policy["costs"], policy["splits"]
    calibration, negative = policy["calibration"], policy["negative_control"]
    args = model_args(policy)
    split_reports: List[Dict[str, Any]] = []
    embargo = max(int(item["horizon_seconds"]) for item in actions) + int(
        policy["actions"]["execution_latency_seconds"]
    )
    for split in splits:
        try:
            model_fit, model_selection, selection_contract = (
                development.build_fit_internal_model_selection_indices(
                    timestamps,
                    split,
                    model_selection_window_seconds=int(
                        split_policy["model_selection_window_seconds"]
                    ),
                    embargo_seconds=embargo,
                )
            )
            validation = development.indices_between(
                timestamps, split.validation_start_ms, split.validation_end_ms
            )
            test = development.indices_between(
                timestamps, split.test_start_ms, split.test_end_ms
            )
            row_coverage = validate_split_row_coverage(
                split_id=int(split.split_id),
                model_fit=model_fit,
                model_selection=model_selection,
                validation=validation,
                test=test,
                split_policy=split_policy,
            )
            fit_utility = development.build_stress_net_utility_targets(
                outcomes[model_fit],
                base_cost_bps=float(costs["additional_round_trip_cost_bps"]),
                stress_cost_multiplier=float(costs["stress_cost_multiplier"]),
            )
            selection_utility = development.build_stress_net_utility_targets(
                outcomes[model_selection],
                base_cost_bps=float(costs["additional_round_trip_cost_bps"]),
                stress_cost_multiplier=float(costs["stress_cost_multiplier"]),
            )
            predictions = development.fit_predict_experimental_architecture(
                architecture_id=ARCHITECTURE_ID,
                fit_features=features[model_fit],
                fit_stress_utilities=fit_utility,
                model_selection_features=features[model_selection],
                model_selection_stress_utilities=selection_utility,
                validation_features=features[validation],
                test_features=features[test],
                actions=actions,
                args=args,
            )
            report = development.evaluate_target_architecture_split(
                architecture_id=ARCHITECTURE_ID,
                split_id=int(split.split_id),
                score_units=str(predictions["score_units"]),
                validation_timestamps=timestamps[validation],
                validation_prediction=np.asarray(predictions["validation_prediction"]),
                validation_realized_base=outcomes[validation],
                test_timestamps=timestamps[test],
                test_prediction=np.asarray(predictions["test_prediction"]),
                test_realized_base=outcomes[test],
                actions=actions,
                quantiles=calibration["quantiles"],
                min_trades=int(calibration["minimum_trades"]),
                base_cost_bps=float(costs["additional_round_trip_cost_bps"]),
                stress_cost_multiplier=float(costs["stress_cost_multiplier"]),
                execution_latency_seconds=int(policy["actions"]["execution_latency_seconds"]),
                permutation_trials=int(negative["permutation_trials"]),
                permutation_seed=int(negative["permutation_seed"]),
                model_diagnostics={
                    **dict(predictions.get("model_diagnostics", {})),
                    "fit_internal_selection_contract": selection_contract,
                    "split_row_coverage": row_coverage,
                },
            )
        except ExperimentNotReady:
            raise
        except Exception as exc:
            report = {
                "status": "training_or_evaluation_error",
                "reason": f"{type(exc).__name__}:{exc}",
                "promotion_evidence": False,
                "promotion_eligible": False,
            }
        split_reports.append(
            {
                "split_id": int(split.split_id),
                "architectures": {ARCHITECTURE_ID: report},
            }
        )
    aggregate = development.aggregate_target_architecture_comparison(
        split_reports=split_reports,
        architecture_ids=[ARCHITECTURE_ID],
        required_split_count=len(splits),
        permutation_trials=int(negative["permutation_trials"]),
        permutation_seed=int(negative["permutation_seed"]),
        permutation_minimum_excess_lcb_bps=float(negative["minimum_excess_lcb_bps"]),
        frozen_contract_failures=[],
    )
    return {
        "arm_id": arm_id,
        "feature_count": int(features.shape[1]),
        "feature_matrix_sha256": array_sha256(features),
        "aggregate": aggregate,
        "split_reports": split_reports,
    }


def architecture_summary(arm: Mapping[str, Any]) -> Mapping[str, Any]:
    return arm["aggregate"]["architecture_summaries"][ARCHITECTURE_ID]


def build_paired_delta(
    control: Mapping[str, Any], treatment: Mapping[str, Any], policy: Mapping[str, Any]
) -> Dict[str, Any]:
    trials = int(policy["negative_control"]["permutation_trials"])
    actual_base: List[float] = []
    actual_stress: List[float] = []
    null_base: List[List[float]] = [[] for _ in range(trials)]
    null_stress: List[List[float]] = [[] for _ in range(trials)]
    failures: List[str] = []
    for split_id, (control_split, treatment_split) in enumerate(
        zip(control["split_reports"], treatment["split_reports"])
    ):
        left = control_split["architectures"].get(ARCHITECTURE_ID, {})
        right = treatment_split["architectures"].get(ARCHITECTURE_ID, {})
        if left.get("status") != "evaluated" or right.get("status") != "evaluated":
            failures.append(f"split_{split_id}_not_evaluated")
            continue
        left_obj, right_obj = left["oos_objective"], right["oos_objective"]
        left_base = _mean_or_zero(left_obj["base_cost"])
        right_base = _mean_or_zero(right_obj["base_cost"])
        left_stress = _mean_or_zero(left_obj["stress_cost"])
        right_stress = _mean_or_zero(right_obj["stress_cost"])
        if None in (left_base, right_base, left_stress, right_stress):
            failures.append(f"split_{split_id}_missing_economics")
            continue
        actual_base.append(float(right_base) - float(left_base))
        actual_stress.append(float(right_stress) - float(left_stress))
        left_controls = left.get("oos_prediction_permutation_controls", [])
        right_controls = right.get("oos_prediction_permutation_controls", [])
        if len(left_controls) != trials or len(right_controls) != trials:
            failures.append(f"split_{split_id}_missing_null_controls")
            continue
        for trial in range(trials):
            left_null, right_null = left_controls[trial], right_controls[trial]
            for key, destination in (("base_cost", null_base), ("stress_cost", null_stress)):
                left_mean = _mean_or_zero(left_null[key])
                right_mean = _mean_or_zero(right_null[key])
                if left_mean is None or right_mean is None:
                    failures.append(f"split_{split_id}_trial_{trial}_missing_{key}")
                    continue
                destination[trial].append(float(right_mean) - float(left_mean))
    base = development.summarize_edges(actual_base)
    stress = development.summarize_edges(actual_stress)
    null_summaries = [
        {
            "trial": trial,
            "base_delta": development.summarize_edges(null_base[trial]),
            "stress_delta": development.summarize_edges(null_stress[trial]),
        }
        for trial in range(trials)
    ]
    null_lcbs = [
        float(item["stress_delta"]["lcb_bps"])
        for item in null_summaries
        if item["stress_delta"]["lcb_bps"] is not None
    ]
    maximum_null = max(null_lcbs) if null_lcbs else None
    candidate_lcb = stress.get("lcb_bps")
    passed = bool(
        not failures
        and base["count"] == 6
        and stress["count"] == 6
        and all(item["stress_delta"]["count"] == 6 for item in null_summaries)
        and candidate_lcb is not None
        and maximum_null is not None
        and candidate_lcb
        > maximum_null
        + float(policy["negative_control"]["minimum_excess_lcb_bps"])
    )
    return {
        "method": "paired_identical_oos_split_mean_delta_with_paired_permutation_v1",
        "fully_verifiable": not failures and stress["count"] == 6,
        "base_cost_delta_by_split": base,
        "stress_cost_delta_by_split": stress,
        "permutation_null": {
            "trial_count": trials,
            "trials": null_summaries,
            "maximum_null_stress_delta_lcb_bps": maximum_null,
            "passed": passed,
        },
        "failures": sorted(set(failures)),
    }


def build_oracle(
    timestamps: np.ndarray,
    outcomes: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    splits: Sequence[development.TimeSplit],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    reports: List[Dict[str, Any]] = []
    base_means: List[float] = []
    stress_means: List[float] = []
    trades = 0
    for split in splits:
        indices = development.indices_between(
            timestamps, split.test_start_ms, split.test_end_ms
        )
        report = development.evaluate_hindsight_oracle(
            timestamps=timestamps[indices],
            realized_base=outcomes[indices],
            actions=actions,
            base_cost_bps=float(policy["costs"]["additional_round_trip_cost_bps"]),
            stress_cost_multiplier=float(policy["costs"]["stress_cost_multiplier"]),
            execution_latency_seconds=int(policy["actions"]["execution_latency_seconds"]),
        )
        objective = report["objective"]
        base_mean = _mean_or_zero(objective["base_cost"])
        stress_mean = _mean_or_zero(objective["stress_cost"])
        if base_mean is not None and stress_mean is not None:
            base_means.append(base_mean)
            stress_means.append(stress_mean)
        trades += int(objective["base_cost"].get("count") or 0)
        reports.append({"split_id": int(split.split_id), **report})
    base, stress = development.summarize_edges(base_means), development.summarize_edges(stress_means)
    gates = policy["decision_gates"]
    positive_ratio = (
        sum(value > 0.0 for value in stress_means) / len(stress_means)
        if stress_means
        else 0.0
    )
    proven = bool(
        len(stress_means) == len(splits)
        and trades >= int(gates["minimum_oos_trades"])
        and positive_ratio >= float(gates["minimum_positive_split_ratio"])
        and (stress.get("lcb_bps") or float("-inf"))
        > float(gates["minimum_oracle_stress_lcb_bps"])
    )
    return {
        "method": "same_oos_non_overlapping_hindsight_upper_bound",
        "fully_verifiable": len(stress_means) == len(splits),
        "opportunity_proven": proven,
        "trade_count": trades,
        "positive_stress_split_ratio": positive_ratio,
        "base_cost_by_split": base,
        "stress_cost_by_split": stress,
        "split_reports": reports,
        "promotion_evidence": False,
    }


def decide(
    *, oracle: Mapping[str, Any], treatment: Mapping[str, Any], paired: Mapping[str, Any], policy: Mapping[str, Any]
) -> Tuple[str, List[str]]:
    treatment_summary = architecture_summary(treatment)
    reasons: List[str] = []
    if not (
        oracle.get("fully_verifiable") is True
        and treatment_summary.get("fully_verifiable") is True
        and paired.get("fully_verifiable") is True
    ):
        return "NOT_READY", ["incomplete_decisive_evidence"]
    if oracle.get("opportunity_proven") is not True:
        return "STOP_CURRENT_RESEARCH_FAMILY", ["stress_cost_oracle_not_positive"]
    stress_lcb = treatment_summary["oos_stress_cost_by_split"].get("lcb_bps")
    stress_positive_ratio = treatment_summary["oos_stress_cost_by_split"].get(
        "positive_ratio"
    )
    trade_count = int(treatment_summary.get("trade_count") or 0)
    permutation_passed = treatment_summary["prediction_permutation_control"].get("passed") is True
    paired_lcb = paired["stress_cost_delta_by_split"].get("lcb_bps")
    paired_permutation = paired["permutation_null"].get("passed") is True
    if stress_lcb is None or stress_lcb <= float(
        policy["decision_gates"]["minimum_treatment_stress_lcb_bps"]
    ):
        reasons.append("treatment_stress_lcb_not_positive")
    if not permutation_passed:
        reasons.append("treatment_does_not_beat_time_permutation")
    if treatment_summary.get("signal_proven") is not True:
        reasons.append("treatment_signal_not_proven")
    if trade_count < int(policy["decision_gates"]["minimum_oos_trades"]):
        reasons.append("treatment_trade_count_below_minimum")
    if stress_positive_ratio is None or stress_positive_ratio < float(
        policy["decision_gates"]["minimum_positive_split_ratio"]
    ):
        reasons.append("treatment_positive_split_ratio_below_minimum")
    if paired_lcb is None or paired_lcb <= float(
        policy["decision_gates"]["minimum_paired_delta_stress_lcb_bps"]
    ):
        reasons.append("paired_treatment_minus_control_lcb_not_positive")
    if not paired_permutation:
        reasons.append("paired_delta_does_not_beat_permutation")
    if reasons:
        return "STOP_INFORMATION_SOURCE", reasons
    return "CONTINUE_TO_SECOND_INDEPENDENT_24H", ["all_first_window_gates_passed"]


def run_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    policy_path = pathlib.Path(args.config).resolve()
    control_path = pathlib.Path(args.control_assessment).resolve()
    treatment_path = pathlib.Path(args.treatment_assessment).resolve()
    policy = validate_policy(policy_path)
    control_assessment = development.validate_capture_assessment(control_path)
    external_assessment = validate_external_assessment(treatment_path)
    control = development.load_capture_rows(control_assessment)
    external = load_external_rows(external_assessment)
    control_features, control_feature_names = development.build_causal_features(control)
    if len(control_feature_names) != development.FROZEN_TARGET_ARCHITECTURE_FEATURE_COUNT:
        raise ValueError("control feature contract is not the frozen 242-column set")
    external_features, external_feature_names, alignment_audit = build_external_features(
        control,
        external,
        lag_seconds=int(policy["external_alignment"]["lag_seconds"]),
    )
    treatment_features = np.column_stack((control_features, external_features))
    outcomes, actions = development.build_joint_action_returns(
        control,
        horizons_seconds=policy["actions"]["horizons_seconds"],
        execution_latency_seconds=int(policy["actions"]["execution_latency_seconds"]),
        additional_round_trip_cost_bps=float(policy["costs"]["additional_round_trip_cost_bps"]),
    )
    timestamps = np.asarray(control["timestamp"], dtype=np.int64)
    eligible = (
        np.all(np.isfinite(control_features), axis=1)
        & np.all(np.isfinite(external_features), axis=1)
        & np.all(np.isfinite(outcomes), axis=1)
    )
    timestamps = timestamps[eligible]
    control_features = control_features[eligible]
    treatment_features = treatment_features[eligible]
    outcomes = outcomes[eligible]
    if len(timestamps) < int(policy["splits"]["minimum_window_rows"]):
        raise ExperimentNotReady("insufficient common causal rows")
    if not (
        np.array_equal(treatment_features[:, : control_features.shape[1]], control_features)
        and len(actions) == development.FROZEN_TARGET_ARCHITECTURE_ACTION_COUNT
    ):
        raise ValueError("single-variable A/B contract failed")
    embargo = max(policy["actions"]["horizons_seconds"]) + int(
        policy["actions"]["execution_latency_seconds"]
    )
    splits = development.build_time_splits(
        timestamps,
        n_splits=int(policy["splits"]["count"]),
        train_window_seconds=int(policy["splits"]["train_window_seconds"]),
        validation_window_seconds=int(policy["splits"]["validation_window_seconds"]),
        test_window_seconds=int(policy["splits"]["test_window_seconds"]),
        rolling_step_seconds=int(policy["splits"]["rolling_step_seconds"]),
        embargo_seconds=embargo,
    )
    control_arm = evaluate_arm(
        arm_id="A_CONTROL",
        timestamps=timestamps,
        features=control_features,
        outcomes=outcomes,
        actions=actions,
        splits=splits,
        policy=policy,
    )
    treatment_arm = evaluate_arm(
        arm_id="B_TREATMENT",
        timestamps=timestamps,
        features=treatment_features,
        outcomes=outcomes,
        actions=actions,
        splits=splits,
        policy=policy,
    )
    oracle = build_oracle(timestamps, outcomes, actions, splits, policy)
    paired = build_paired_delta(control_arm, treatment_arm, policy)
    decision, reasons = decide(
        oracle=oracle, treatment=treatment_arm, paired=paired, policy=policy
    )
    fully_verifiable = decision != "NOT_READY"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE" if fully_verifiable else "NOT_READY",
        "fully_verifiable": fully_verifiable,
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "experiment_id": policy["experiment_id"],
        "experiment_policy": {
            "path": str(policy_path),
            "sha256": sha256_file(policy_path),
            "identity_sha256": canonical_sha256(policy),
        },
        "inputs": {
            "control_assessment": {"path": str(control_path), "sha256": sha256_file(control_path)},
            "treatment_assessment": {"path": str(treatment_path), "sha256": sha256_file(treatment_path)},
        },
        "single_variable_audit": {
            "changed_variable": policy["single_variable_change"],
            "control_feature_count": len(control_feature_names),
            "treatment_feature_count": len(control_feature_names) + len(external_feature_names),
            "control_feature_names_sha256": canonical_sha256({"names": control_feature_names}),
            "external_feature_names": external_feature_names,
            "control_prefix_bytes_identical": True,
            "outcome_matrix_shared": True,
            "split_calendar_shared": True,
            "model_hyperparameters_shared": True,
            "actions_costs_latency_shared": True,
            "external_alignment": alignment_audit,
        },
        "common_domain": {
            "row_count": len(timestamps),
            "first_timestamp_ms": int(timestamps[0]),
            "last_timestamp_ms": int(timestamps[-1]),
            "timestamp_sha256": array_sha256(timestamps),
            "outcomes_sha256": array_sha256(outcomes),
            "splits": [dataclasses.asdict(split) for split in splits],
        },
        "hindsight_oracle": oracle,
        "arms": {"control": control_arm, "treatment": treatment_arm},
        "paired_treatment_minus_control": paired,
        "research_decision": decision,
        "reason_codes": reasons,
        "next_action": {
            "STOP_CURRENT_RESEARCH_FAMILY": "close_action_target_family_without_more_model_tuning",
            "STOP_INFORMATION_SOURCE": "close_binance_sol_l2_trade_information_source",
            "CONTINUE_TO_SECOND_INDEPENDENT_24H": "collect_and_run_second_non_overlapping_window",
            "NOT_READY": "complete_first_common_capture_window",
        }[decision],
    }


def not_ready_report(args: argparse.Namespace, reason: str, status: str = "NOT_READY") -> Dict[str, Any]:
    config = pathlib.Path(args.config).resolve()
    try:
        raw_policy = read_json(config) if config.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError):
        raw_policy = {}
    splits = raw_policy.get("splits", {})
    actions = raw_policy.get("actions", {})
    embargo = max(actions.get("horizons_seconds", [300])) + int(
        actions.get("execution_latency_seconds", 1)
    )
    required_seconds = (
        (int(splits.get("count", 6)) - 1) * int(splits.get("rolling_step_seconds", 14400))
        + int(splits.get("test_window_seconds", 14400))
        + int(splits.get("validation_window_seconds", 14400))
        + int(splits.get("train_window_seconds", 21600))
        + 2 * embargo
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "fully_verifiable": False,
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "research_decision": "NOT_READY",
        "reason_codes": [reason],
        "minimum_common_span_seconds_for_frozen_splits": required_seconds,
        "next_action": "continue_external_venue_capture",
        "experiment_policy": {
            "path": str(config),
            "sha256": sha256_file(config) if config.is_file() else None,
        },
    }


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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
    except (development.CaptureNotReady, ExperimentNotReady) as exc:
        report = not_ready_report(args, str(exc))
    except Exception as exc:
        report = not_ready_report(
            args, f"{type(exc).__name__}:{exc}", status="INVALID_INPUT"
        )
    atomic_write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report.get("status") == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
