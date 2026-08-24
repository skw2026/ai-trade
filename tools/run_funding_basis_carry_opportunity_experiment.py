#!/usr/bin/env python3
"""Audit a no-model long-spot/short-perpetual funding/basis carry upper bound."""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import math
import pathlib
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np

import run_cross_venue_information_set_experiment as common
import run_microstructure_alpha_development as development


SCHEMA_VERSION = "funding_basis_carry_opportunity_experiment_v1"
POLICY_SCHEMA_VERSION = "funding_basis_carry_opportunity_policy_v1"
AUDIT_SCHEMA_VERSION = "funding_basis_carry_frozen_audit_v1"
FROZEN_POLICY_IDENTITY_SHA256 = (
    "ced92861240ae2882b544edf9541029e4b758eb3667ef6f37ba804bd3526a2db"
)
DECISION_CONTINUE = "CONTINUE_TO_RAW_BBO_FORWARD_CARRY_VALIDATION"
DECISION_STOP = "STOP_FUNDING_BASIS_CARRY_FAMILY"

REQUIRED_FIELDS = (
    "timestamp",
    "spot_open",
    "spot_high",
    "spot_low",
    "spot_close",
    "spot_volume",
    "spot_turnover",
    "perpetual_open",
    "perpetual_high",
    "perpetual_low",
    "perpetual_close",
    "perpetual_volume",
    "perpetual_turnover",
    "mark_open",
    "mark_high",
    "mark_low",
    "mark_close",
    "funding_rate",
)


def validate_policy(path: pathlib.Path) -> Dict[str, Any]:
    policy = common.read_json(path)
    failures: List[str] = []
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        failures.append("schema_version")
    if common.canonical_sha256(policy) != FROZEN_POLICY_IDENTITY_SHA256:
        failures.append("identity_sha256")
    if not (
        policy.get("research_domain") == "development_only"
        and policy.get("promotion_evidence") is False
        and policy.get("experiment_id")
        == "bybit_sol_spot_perpetual_funding_basis_carry_opportunity_v1"
    ):
        failures.append("research_domain")
    data = policy.get("data_contract")
    if not (
        isinstance(data, Mapping)
        and data.get("provider") == "bybit"
        and data.get("symbol") == "SOLUSDT"
        and data.get("spot_category") == "spot"
        and data.get("perpetual_category") == "linear"
        and int(data.get("interval_minutes", 0)) == 5
        and int(data.get("lookback_days", 0)) == 140
        and data.get("funding_alignment") == "exact_settlement_timestamp_once_only"
        and data.get("missing_bar_policy") == "exact_inner_join_no_fill"
    ):
        failures.append("data_contract")
    mechanism = policy.get("mechanism")
    if not (
        isinstance(mechanism, Mapping)
        and mechanism.get("position") == "long_spot_short_linear_perpetual"
        and mechanism.get("reverse_carry_allowed") is False
        and mechanism.get("borrowing_assumed") is False
        and float(mechanism.get("spot_entry_notional", 0.0)) == 1.0
        and mechanism.get("perpetual_base_quantity")
        == "match_spot_base_quantity"
        and mechanism.get("funding_event_interval")
        == "entry_timestamp_exclusive_exit_timestamp_inclusive"
    ):
        failures.append("mechanism")
    execution = policy.get("execution")
    if not (
        isinstance(execution, Mapping)
        and execution.get("horizons_hours") == [24, 72, 168]
        and int(execution.get("entry_latency_bars", 0)) == 1
        and execution.get("one_outstanding_position") is True
        and execution.get("price_proxy")
        == "next_bar_open_plus_minus_frozen_half_spread"
        and float(execution.get("spot_half_spread_bps", 0.0)) == 2.0
        and float(execution.get("perpetual_half_spread_bps", 0.0)) == 0.5
        and float(execution.get("spot_taker_fee_bps_per_fill", 0.0)) == 10.0
        and float(execution.get("perpetual_taker_fee_bps_per_fill", 0.0)) == 5.5
        and float(execution.get("spot_slippage_bps_per_fill", 0.0)) == 1.5
        and float(execution.get("perpetual_slippage_bps_per_fill", 0.0)) == 1.0
        and float(execution.get("stress_execution_cost_multiplier", 0.0)) == 1.25
        and execution.get("historical_proxy_can_authorize_demo") is False
    ):
        failures.append("execution")
    if policy.get("capital_cost") != {
        "gross_capital_multiplier": 2.0,
        "base_annual_rate": 0.05,
        "stress_annual_rate": 0.075,
        "day_count": 365.0,
    }:
        failures.append("capital_cost")
    if policy.get("splits") != {
        "count": 6,
        "train_window_days": 30,
        "validation_window_days": 7,
        "test_window_days": 14,
        "rolling_step_days": 14,
        "embargo_days": 1,
    }:
        failures.append("splits")
    stability = policy.get("stability_audit")
    if not (
        isinstance(stability, Mapping)
        and stability.get("manifest_schema_version") == AUDIT_SCHEMA_VERSION
        and stability.get("boundary_offsets_days") == [0, -1, -2, -3]
        and float(stability.get("minimum_boundary_pass_ratio", 0.0)) == 0.75
        and stability.get("require_exact_frozen_domain") is True
    ):
        failures.append("stability_audit")
    if policy.get("decision_gates") != {
        "minimum_oos_trades": 12,
        "minimum_positive_split_ratio": 0.6,
        "minimum_oracle_stress_lcb_bps": 0.0,
        "minimum_funding_events": 30,
    }:
        failures.append("decision_gates")
    if policy.get("authorities") != {
        "historical_continuation_target": "raw_spot_perpetual_bbo_forward_capture",
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }:
        failures.append("authorities")
    if failures:
        raise ValueError("funding/basis carry policy mismatch: " + ",".join(failures))
    return policy


def load_series(path: pathlib.Path) -> Dict[str, np.ndarray]:
    columns: Dict[str, List[float | int]] = {field: [] for field in REQUIRED_FIELDS}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in REQUIRED_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError("carry CSV missing fields: " + ",".join(missing))
        for row in reader:
            columns["timestamp"].append(int(row["timestamp"]))
            for field in REQUIRED_FIELDS[1:]:
                text = str(row[field]).strip()
                columns[field].append(float(text) if text else float("nan"))
    series: Dict[str, np.ndarray] = {
        "timestamp": np.asarray(columns["timestamp"], dtype=np.int64)
    }
    for field in REQUIRED_FIELDS[1:]:
        series[field] = np.asarray(columns[field], dtype=np.float64)
    timestamps = series["timestamp"]
    if len(timestamps) < 2 or not np.all(np.diff(timestamps) > 0):
        raise ValueError("carry timestamps must be strictly increasing")
    for field in REQUIRED_FIELDS[1:-1]:
        values = series[field]
        if not np.all(np.isfinite(values)) or np.any(values < 0.0):
            raise ValueError(f"carry field is invalid: {field}")
    funding = series["funding_rate"]
    if np.any(np.isinf(funding)):
        raise ValueError("funding rate contains infinity")
    return series


def domain_identity(
    series: Mapping[str, np.ndarray], *, start_ms: int, end_ms: int
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    indices = np.flatnonzero((timestamps >= int(start_ms)) & (timestamps < int(end_ms)))
    if not len(indices):
        raise ValueError("carry frozen domain is empty")
    payload: Dict[str, Any] = {
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "row_count": int(len(indices)),
        "first_timestamp_ms": int(timestamps[indices[0]]),
        "last_timestamp_ms": int(timestamps[indices[-1]]),
        "field_sha256": {
            field: common.array_sha256(np.asarray(series[field])[indices])
            for field in REQUIRED_FIELDS
        },
    }
    payload["identity_sha256"] = common.canonical_sha256(payload)
    return payload


def shift_split(split: development.TimeSplit, days: int) -> development.TimeSplit:
    delta = int(days) * 86_400_000
    return development.TimeSplit(
        split_id=split.split_id,
        fit_start_ms=split.fit_start_ms + delta,
        fit_end_ms=split.fit_end_ms + delta,
        validation_start_ms=split.validation_start_ms + delta,
        validation_end_ms=split.validation_end_ms + delta,
        test_start_ms=split.test_start_ms + delta,
        test_end_ms=split.test_end_ms + delta,
    )


def splits_from_payload(values: Sequence[Mapping[str, Any]]) -> List[development.TimeSplit]:
    return [
        development.TimeSplit(**{key: int(value[key]) for key in development.TimeSplit.__dataclass_fields__})
        for value in values
    ]


def build_carry_time_splits(
    timestamps: np.ndarray,
    *,
    n_splits: int,
    train_window_seconds: int,
    validation_window_seconds: int,
    test_window_seconds: int,
    rolling_step_seconds: int,
    embargo_seconds: int,
) -> List[development.TimeSplit]:
    if rolling_step_seconds < test_window_seconds:
        raise ValueError("overlapping carry OOS test windows are forbidden")
    if validation_window_seconds >= train_window_seconds:
        raise ValueError("carry validation window must be smaller than train window")
    latest_end = int(np.max(timestamps)) + 300_000
    first_test_start = latest_end - (
        (n_splits - 1) * rolling_step_seconds + test_window_seconds
    ) * 1000
    splits: List[development.TimeSplit] = []
    for split_id in range(n_splits):
        test_start = first_test_start + split_id * rolling_step_seconds * 1000
        test_end = test_start + test_window_seconds * 1000
        validation_end = test_start - embargo_seconds * 1000
        validation_start = validation_end - validation_window_seconds * 1000
        fit_end = validation_start - embargo_seconds * 1000
        fit_start = fit_end - train_window_seconds * 1000
        splits.append(
            development.TimeSplit(
                split_id=split_id,
                fit_start_ms=fit_start,
                fit_end_ms=fit_end,
                validation_start_ms=validation_start,
                validation_end_ms=validation_end,
                test_start_ms=test_start,
                test_end_ms=test_end,
            )
        )
    return splits


def create_manifest(
    series: Mapping[str, np.ndarray], policy: Mapping[str, Any]
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    split = policy["splits"]
    primary = build_carry_time_splits(
        timestamps,
        n_splits=int(split["count"]),
        train_window_seconds=int(split["train_window_days"]) * 86_400,
        validation_window_seconds=int(split["validation_window_days"]) * 86_400,
        test_window_seconds=int(split["test_window_days"]) * 86_400,
        rolling_step_seconds=int(split["rolling_step_days"]) * 86_400,
        embargo_seconds=int(split["embargo_days"]) * 86_400,
    )
    offsets = [int(value) for value in policy["stability_audit"]["boundary_offsets_days"]]
    boundary = {
        str(offset): [shift_split(item, offset) for item in primary] for offset in offsets
    }
    frozen_start = min(item.fit_start_ms for values in boundary.values() for item in values)
    frozen_end = max(item.test_end_ms for item in primary)
    if int(timestamps[0]) > frozen_start or int(timestamps[-1]) < frozen_end - 300_000:
        raise development.CaptureNotReady("carry history does not cover frozen split calendar")
    manifest: Dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "policy_identity_sha256": FROZEN_POLICY_IDENTITY_SHA256,
        "experiment_id": policy["experiment_id"],
        "split_calendar_source": "deterministic_latest_closed_development_domain_v1",
        "frozen_domain": domain_identity(series, start_ms=frozen_start, end_ms=frozen_end),
        "primary_splits": [vars(item) for item in primary],
        "boundary_splits": {
            key: [vars(item) for item in values] for key, values in boundary.items()
        },
    }
    manifest["identity_sha256"] = common.canonical_sha256(manifest)
    return manifest


def load_or_create_manifest(
    path: pathlib.Path,
    *,
    series: Mapping[str, np.ndarray],
    policy: Mapping[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    created = False
    if not path.is_file():
        manifest = create_manifest(series, policy)
        common.atomic_write_json(path, manifest)
        created = True
    manifest = common.read_json(path)
    unsigned = {key: value for key, value in manifest.items() if key != "identity_sha256"}
    offsets = [str(int(value)) for value in policy["stability_audit"]["boundary_offsets_days"]]
    if not (
        manifest.get("schema_version") == AUDIT_SCHEMA_VERSION
        and manifest.get("policy_identity_sha256") == FROZEN_POLICY_IDENTITY_SHA256
        and manifest.get("experiment_id") == policy["experiment_id"]
        and manifest.get("identity_sha256") == common.canonical_sha256(unsigned)
        and len(manifest.get("primary_splits", [])) == int(policy["splits"]["count"])
        and sorted(manifest.get("boundary_splits", {}).keys(), key=int)
        == sorted(offsets, key=int)
    ):
        raise ValueError("carry frozen audit manifest mismatch")
    frozen = manifest.get("frozen_domain")
    if not isinstance(frozen, Mapping):
        raise ValueError("carry frozen domain missing")
    actual = domain_identity(
        series, start_ms=int(frozen["start_ms"]), end_ms=int(frozen["end_ms"])
    )
    if actual != frozen:
        raise ValueError("carry frozen domain drift")
    return manifest, created


def quote(open_price: float, half_spread_bps: float, side: str) -> float:
    multiplier = 1.0 + (half_spread_bps / 10_000.0) * (1.0 if side == "ask" else -1.0)
    return float(open_price) * multiplier


def candidate_outcome(
    *,
    series: Mapping[str, np.ndarray],
    positions: Mapping[int, int],
    funding_timestamps: Sequence[int],
    funding_indices: Sequence[int],
    entry_timestamp: int,
    exit_timestamp: int,
    policy: Mapping[str, Any],
) -> Dict[str, float | int] | None:
    entry_index = positions.get(int(entry_timestamp))
    exit_index = positions.get(int(exit_timestamp))
    if entry_index is None or exit_index is None:
        return None
    execution = policy["execution"]
    spot_entry = quote(
        series["spot_open"][entry_index], float(execution["spot_half_spread_bps"]), "ask"
    )
    spot_exit = quote(
        series["spot_open"][exit_index], float(execution["spot_half_spread_bps"]), "bid"
    )
    perp_entry = quote(
        series["perpetual_open"][entry_index],
        float(execution["perpetual_half_spread_bps"]),
        "bid",
    )
    perp_exit = quote(
        series["perpetual_open"][exit_index],
        float(execution["perpetual_half_spread_bps"]),
        "ask",
    )
    if min(spot_entry, spot_exit, perp_entry, perp_exit) <= 0.0:
        return None
    quantity = float(policy["mechanism"]["spot_entry_notional"]) / spot_entry
    start = bisect.bisect_right(funding_timestamps, int(entry_timestamp))
    end = bisect.bisect_right(funding_timestamps, int(exit_timestamp))
    funding_cash = 0.0
    for funding_index in funding_indices[start:end]:
        funding_cash += (
            quantity
            * float(series["mark_open"][funding_index])
            * float(series["funding_rate"][funding_index])
        )
    gross_cash = (
        quantity * (spot_exit - spot_entry)
        + quantity * (perp_entry - perp_exit)
        + funding_cash
    )
    spot_rate = (
        float(execution["spot_taker_fee_bps_per_fill"])
        + float(execution["spot_slippage_bps_per_fill"])
    ) / 10_000.0
    perp_rate = (
        float(execution["perpetual_taker_fee_bps_per_fill"])
        + float(execution["perpetual_slippage_bps_per_fill"])
    ) / 10_000.0
    execution_cost_cash = quantity * (
        spot_rate * (spot_entry + spot_exit) + perp_rate * (perp_entry + perp_exit)
    )
    duration_days = (int(exit_timestamp) - int(entry_timestamp)) / 86_400_000.0
    capital = policy["capital_cost"]
    base_capital_cost = (
        float(capital["gross_capital_multiplier"])
        * float(capital["base_annual_rate"])
        * duration_days
        / float(capital["day_count"])
    )
    stress_capital_cost = (
        float(capital["gross_capital_multiplier"])
        * float(capital["stress_annual_rate"])
        * duration_days
        / float(capital["day_count"])
    )
    denominator = float(policy["mechanism"]["spot_entry_notional"])
    base_bps = (gross_cash - execution_cost_cash - base_capital_cost) / denominator * 10_000.0
    stress_bps = (
        gross_cash
        - execution_cost_cash * float(execution["stress_execution_cost_multiplier"])
        - stress_capital_cost
    ) / denominator * 10_000.0
    return {
        "base_bps": base_bps,
        "stress_bps": stress_bps,
        "gross_bps": gross_cash / denominator * 10_000.0,
        "execution_cost_bps": execution_cost_cash / denominator * 10_000.0,
        "funding_bps": funding_cash / denominator * 10_000.0,
        "funding_event_count": end - start,
    }


def evaluate_split(
    *,
    series: Mapping[str, np.ndarray],
    split: development.TimeSplit,
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    positions = {int(value): index for index, value in enumerate(timestamps)}
    event_indices = np.flatnonzero(np.isfinite(series["funding_rate"]))
    event_timestamps = [int(timestamps[index]) for index in event_indices]
    horizons = [int(value) for value in policy["execution"]["horizons_hours"]]
    latency_ms = int(policy["execution"]["entry_latency_bars"]) * 300_000
    test_indices = development.indices_between(
        timestamps, split.test_start_ms, split.test_end_ms
    )
    positive_intervals: List[Dict[str, Any]] = []
    maximum_candidate: Dict[str, Any] | None = None
    observable = 0
    for raw_index in np.asarray(test_indices, dtype=np.int64):
        decision_timestamp = int(timestamps[int(raw_index)])
        entry_timestamp = decision_timestamp + latency_ms
        if entry_timestamp not in positions:
            continue
        candidates: List[Tuple[float, int, Dict[str, float | int]]] = []
        for horizon in horizons:
            exit_timestamp = entry_timestamp + horizon * 3_600_000
            if exit_timestamp >= split.test_end_ms:
                continue
            outcome = candidate_outcome(
                series=series,
                positions=positions,
                funding_timestamps=event_timestamps,
                funding_indices=event_indices,
                entry_timestamp=entry_timestamp,
                exit_timestamp=exit_timestamp,
                policy=policy,
            )
            if outcome is not None:
                candidates.append((float(outcome["stress_bps"]), horizon, outcome))
        if not candidates:
            continue
        observable += 1
        stress_bps, horizon, best = max(candidates, key=lambda item: (item[0], -item[1]))
        interval = {
            "entry_timestamp_ms": entry_timestamp,
            "exit_timestamp_ms": entry_timestamp + horizon * 3_600_000,
            "horizon_hours": horizon,
            **best,
        }
        if maximum_candidate is None or float(interval["stress_bps"]) > float(
            maximum_candidate["stress_bps"]
        ):
            maximum_candidate = interval
        if stress_bps > 0.0:
            positive_intervals.append(interval)

    # Exact weighted-interval scheduling makes this a true one-position
    # hindsight upper bound.  A first-positive greedy policy can reject a
    # mechanism merely because an early weak trade blocks a later strong one.
    intervals = sorted(
        positive_intervals,
        key=lambda item: (
            int(item["exit_timestamp_ms"]),
            int(item["entry_timestamp_ms"]),
            int(item["horizon_hours"]),
        ),
    )
    end_times = [int(item["exit_timestamp_ms"]) for item in intervals]
    predecessors = [
        bisect.bisect_right(end_times, int(item["entry_timestamp_ms"]), 0, index) - 1
        for index, item in enumerate(intervals)
    ]
    values = [0.0] * (len(intervals) + 1)
    take = [False] * len(intervals)
    for index, item in enumerate(intervals, start=1):
        include = float(item["stress_bps"]) + values[predecessors[index - 1] + 1]
        exclude = values[index - 1]
        if include > exclude:
            values[index] = include
            take[index - 1] = True
        else:
            values[index] = exclude
    selected: List[Dict[str, Any]] = []
    cursor = len(intervals)
    while cursor > 0:
        if take[cursor - 1] and values[cursor] > values[cursor - 1]:
            selected.append(intervals[cursor - 1])
            cursor = predecessors[cursor - 1] + 1
        else:
            cursor -= 1
    selected.reverse()
    base_edges = [float(item["base_bps"]) for item in selected]
    stress_edges = [float(item["stress_bps"]) for item in selected]
    funding_edges = [float(item["funding_bps"]) for item in selected]
    funding_events = sum(int(item["funding_event_count"]) for item in selected)
    action_counts: Dict[str, int] = {}
    for item in selected:
        key = f"hold_{int(item['horizon_hours'])}h"
        action_counts[key] = action_counts.get(key, 0) + 1
    return {
        "split_id": int(split.split_id),
        "observable_decision_count": observable,
        "positive_candidate_count": len(positive_intervals),
        "trade_count": len(base_edges),
        "funding_event_count": funding_events,
        "action_counts": action_counts,
        "base_cost": development.summarize_edges(base_edges),
        "stress_cost": development.summarize_edges(stress_edges),
        "funding_component": development.summarize_edges(funding_edges),
        "maximum_candidate": (
            None
            if maximum_candidate is None
            else {
                "base_bps": float(maximum_candidate["base_bps"]),
                "stress_bps": float(maximum_candidate["stress_bps"]),
                "gross_bps": float(maximum_candidate["gross_bps"]),
                "execution_cost_bps": float(maximum_candidate["execution_cost_bps"]),
                "funding_bps": float(maximum_candidate["funding_bps"]),
                "funding_event_count": int(maximum_candidate["funding_event_count"]),
                "horizon_hours": int(maximum_candidate["horizon_hours"]),
            }
        ),
    }


def build_oracle(
    *,
    series: Mapping[str, np.ndarray],
    splits: Sequence[development.TimeSplit],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    reports = [evaluate_split(series=series, split=split, policy=policy) for split in splits]
    base_means = [float(item["base_cost"].get("mean_bps") or 0.0) for item in reports]
    stress_means = [float(item["stress_cost"].get("mean_bps") or 0.0) for item in reports]
    trade_count = sum(int(item["trade_count"]) for item in reports)
    funding_event_count = sum(int(item["funding_event_count"]) for item in reports)
    positive_ratio = sum(value > 0.0 for value in stress_means) / len(stress_means)
    base_summary = development.summarize_edges(base_means)
    stress_summary = development.summarize_edges(stress_means)
    maximum_candidates = [
        item["maximum_candidate"]
        for item in reports
        if isinstance(item.get("maximum_candidate"), Mapping)
    ]
    maximum_candidate = (
        max(maximum_candidates, key=lambda item: float(item["stress_bps"]))
        if maximum_candidates
        else None
    )
    gates = policy["decision_gates"]
    proven = bool(
        trade_count >= int(gates["minimum_oos_trades"])
        and funding_event_count >= int(gates["minimum_funding_events"])
        and positive_ratio >= float(gates["minimum_positive_split_ratio"])
        and stress_summary.get("lcb_bps") is not None
        and float(stress_summary["lcb_bps"])
        > float(gates["minimum_oracle_stress_lcb_bps"])
    )
    return {
        "method": "six_split_exact_weighted_interval_hindsight_no_model_upper_bound_v2",
        "fully_verifiable": len(reports) == int(policy["splits"]["count"]),
        "opportunity_proven": proven,
        "trade_count": trade_count,
        "funding_event_count": funding_event_count,
        "positive_stress_split_ratio": positive_ratio,
        "base_cost_by_split": base_summary,
        "stress_cost_by_split": stress_summary,
        "maximum_candidate": maximum_candidate,
        "split_reports": reports,
        "promotion_evidence": False,
    }


def run_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = pathlib.Path(args.config).resolve()
    data_path = pathlib.Path(args.carry_csv).resolve()
    data_report_path = pathlib.Path(args.data_report).resolve()
    manifest_path = pathlib.Path(args.audit_manifest).resolve()
    policy = validate_policy(config_path)
    data_report = common.read_json(data_report_path)
    if not (
        data_report.get("schema_version") == "bybit_carry_history_v1"
        and data_report.get("status") == "PASS"
        and data_report.get("output_sha256") == common.sha256_file(data_path)
        and data_report.get("causality", {}).get("funding_alignment")
        == "exact_settlement_timestamp_once_only"
        and data_report.get("causality", {}).get("asof_funding_fill") is False
    ):
        raise development.CaptureNotReady("carry data report is not verifiable")
    series = load_series(data_path)
    manifest, created = load_or_create_manifest(
        manifest_path, series=series, policy=policy
    )
    primary = build_oracle(
        series=series,
        splits=splits_from_payload(manifest["primary_splits"]),
        policy=policy,
    )
    boundary_reports: List[Dict[str, Any]] = []
    for offset in policy["stability_audit"]["boundary_offsets_days"]:
        oracle = build_oracle(
            series=series,
            splits=splits_from_payload(manifest["boundary_splits"][str(int(offset))]),
            policy=policy,
        )
        boundary_reports.append(
            {
                "offset_days": int(offset),
                "opportunity_proven": bool(oracle["opportunity_proven"]),
                "trade_count": int(oracle["trade_count"]),
                "funding_event_count": int(oracle["funding_event_count"]),
                "positive_stress_split_ratio": float(oracle["positive_stress_split_ratio"]),
                "stress_cost_by_split": oracle["stress_cost_by_split"],
            }
        )
    boundary_ratio = sum(item["opportunity_proven"] for item in boundary_reports) / len(boundary_reports)
    boundary_passed = boundary_ratio >= float(
        policy["stability_audit"]["minimum_boundary_pass_ratio"]
    )
    if primary["opportunity_proven"] and boundary_passed:
        decision = DECISION_CONTINUE
        reasons = ["historical_carry_upper_bound_and_boundary_gates_passed"]
        next_action = "collect_preregistered_raw_spot_perpetual_bbo_forward_evidence"
    else:
        decision = DECISION_STOP
        reasons = []
        if not primary["opportunity_proven"]:
            reasons.append("historical_carry_upper_bound_failed")
        if not boundary_passed:
            reasons.append("carry_boundary_sensitivity_failed")
        next_action = "close_funding_basis_carry_family_and_change_mechanism"
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE",
        "fully_verifiable": True,
        "research_domain": "historical_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "experiment_id": policy["experiment_id"],
        "experiment_policy": {
            "path": str(config_path),
            "sha256": common.sha256_file(config_path),
            "identity_sha256": common.canonical_sha256(policy),
        },
        "input": {
            "carry_csv_path": str(data_path),
            "carry_csv_sha256": common.sha256_file(data_path),
            "data_report_path": str(data_report_path),
            "data_report_sha256": common.sha256_file(data_report_path),
            "frozen_audit_manifest_path": str(manifest_path),
            "frozen_audit_manifest_sha256": common.sha256_file(manifest_path),
            "frozen_audit_manifest_identity_sha256": manifest["identity_sha256"],
        },
        "data_contract": dict(policy["data_contract"]),
        "execution_contract": {
            "mechanism": dict(policy["mechanism"]),
            "execution": dict(policy["execution"]),
            "capital_cost": dict(policy["capital_cost"]),
            "historical_price_is_executable_bbo": False,
            "historical_proxy_can_authorize_demo": False,
        },
        "common_domain": {
            "row_count": int(len(timestamps)),
            "first_timestamp_ms": int(timestamps[0]),
            "last_timestamp_ms": int(timestamps[-1]),
            "funding_event_count": int(np.count_nonzero(np.isfinite(series["funding_rate"]))),
            "timestamp_sha256": common.array_sha256(timestamps),
            "splits": list(manifest["primary_splits"]),
        },
        "hindsight_oracle": primary,
        "stability_audit": {
            "manifest_created_this_run": created,
            "manifest_identity_sha256": manifest["identity_sha256"],
            "boundary_sensitivity": {
                "reports": boundary_reports,
                "pass_ratio": boundary_ratio,
                "minimum_pass_ratio": float(
                    policy["stability_audit"]["minimum_boundary_pass_ratio"]
                ),
                "passed": boundary_passed,
            },
        },
        "research_decision": decision,
        "reason_codes": reasons,
        "next_action": next_action,
    }


def not_ready_report(args: argparse.Namespace, reason: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "NOT_READY",
        "fully_verifiable": False,
        "research_domain": "historical_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "research_decision": "NOT_READY",
        "reason_codes": [reason],
        "next_action": "complete_verifiable_carry_history",
        "experiment_policy": {"path": str(pathlib.Path(args.config).resolve())},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--carry-csv", required=True)
    parser.add_argument("--data-report", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--audit-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--research-domain", default="development", choices=("development",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = run_experiment(args)
    except development.CaptureNotReady as exc:
        report = not_ready_report(args, f"capture_not_ready:{exc}")
    except Exception as exc:
        report = not_ready_report(args, f"invalid_input:{type(exc).__name__}:{exc}")
    common.atomic_write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report.get("status") == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
