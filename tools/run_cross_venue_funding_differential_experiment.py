#!/usr/bin/env python3
"""Audit a no-model Bybit/Binance perpetual funding differential upper bound."""

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
import run_funding_basis_carry_opportunity_experiment as carry
import run_microstructure_alpha_development as development


SCHEMA_VERSION = "cross_venue_funding_differential_experiment_v1"
POLICY_SCHEMA_VERSION = "cross_venue_funding_differential_policy_v1"
AUDIT_SCHEMA_VERSION = "cross_venue_funding_differential_frozen_audit_v1"
PARENT_AUDIT_SCHEMA_VERSION = "funding_basis_carry_frozen_audit_v1"
FROZEN_POLICY_IDENTITY_SHA256 = (
    "573142b7512e72403da06fa26da97e220ea599cff900d1fbb5dfa94df88487e0"
)
DECISION_CONTINUE = "CONTINUE_TO_RAW_CROSS_VENUE_BBO_FORWARD_VALIDATION"
DECISION_STOP = "STOP_CROSS_VENUE_FUNDING_DIFFERENTIAL_FAMILY"

REQUIRED_FIELDS = (
    "timestamp",
    "bybit_perpetual_open",
    "bybit_perpetual_high",
    "bybit_perpetual_low",
    "bybit_perpetual_close",
    "bybit_perpetual_volume",
    "bybit_perpetual_turnover",
    "bybit_mark_open",
    "bybit_mark_high",
    "bybit_mark_low",
    "bybit_mark_close",
    "binance_perpetual_open",
    "binance_perpetual_high",
    "binance_perpetual_low",
    "binance_perpetual_close",
    "binance_perpetual_volume",
    "binance_perpetual_turnover",
    "binance_mark_open",
    "binance_mark_high",
    "binance_mark_low",
    "binance_mark_close",
    "bybit_funding_rate",
    "bybit_funding_mark",
    "bybit_funding_timestamp",
    "binance_funding_rate",
    "binance_funding_mark",
    "binance_funding_timestamp",
)
FUNDING_FIELDS = {
    "bybit_funding_rate",
    "bybit_funding_mark",
    "bybit_funding_timestamp",
    "binance_funding_rate",
    "binance_funding_mark",
    "binance_funding_timestamp",
}


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
        == "bybit_binance_sol_perpetual_funding_differential_opportunity_v1"
    ):
        failures.append("research_domain")
    data = policy.get("data_contract")
    if not (
        isinstance(data, Mapping)
        and data.get("symbol") == "SOLUSDT"
        and data.get("venues") == ["bybit", "binance"]
        and data.get("contract_type") == "linear_perpetual"
        and int(data.get("interval_minutes", 0)) == 5
        and int(data.get("lookback_days", 0)) == 140
        and data.get("funding_alignment")
        == "exact_venue_settlement_timestamp_once_only"
        and data.get("missing_bar_policy")
        == "four_series_exact_inner_join_no_fill"
        and data.get("parent_split_source") == PARENT_AUDIT_SCHEMA_VERSION
    ):
        failures.append("data_contract")
    mechanism = policy.get("mechanism")
    if not (
        isinstance(mechanism, Mapping)
        and mechanism.get("directions")
        == ["long_bybit_short_binance", "long_binance_short_bybit"]
        and float(mechanism.get("reference_entry_notional", 0.0)) == 1.0
        and mechanism.get("matched_base_quantity_across_venues") is True
        and mechanism.get("independent_margin_coverage_required") is True
        and mechanism.get("instant_cross_venue_transfer_assumed") is False
        and mechanism.get("shared_margin_assumed") is False
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
        and float(execution.get("bybit_half_spread_bps", -1.0)) == 0.5
        and float(execution.get("binance_half_spread_bps", -1.0)) == 0.5
        and float(execution.get("bybit_taker_fee_bps_per_fill", -1.0)) == 5.5
        and float(execution.get("binance_taker_fee_bps_per_fill", -1.0)) == 5.5
        and float(execution.get("bybit_slippage_bps_per_fill", -1.0)) == 1.0
        and float(execution.get("binance_slippage_bps_per_fill", -1.0)) == 1.0
        and float(execution.get("intervenue_leg_risk_bps_per_round_trip", -1.0))
        == 2.0
        and float(execution.get("stress_execution_cost_multiplier", 0.0)) == 1.25
        and execution.get("account_vip_discount_assumed") is False
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
        "inherit_parent_absolute_splits": True,
        "allow_rolling_recut": False,
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
        "minimum_funding_events_per_venue": 30,
    }:
        failures.append("decision_gates")
    if policy.get("authorities") != {
        "historical_continuation_target": "raw_cross_venue_perpetual_bbo_forward_capture",
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }:
        failures.append("authorities")
    if failures:
        raise ValueError(
            "cross-venue funding differential policy mismatch: " + ",".join(failures)
        )
    return policy


def load_series(path: pathlib.Path) -> Dict[str, np.ndarray]:
    columns: Dict[str, List[float | int]] = {field: [] for field in REQUIRED_FIELDS}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in REQUIRED_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError("cross-venue funding CSV missing fields: " + ",".join(missing))
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
        raise ValueError("cross-venue timestamps must be strictly increasing")
    for field in REQUIRED_FIELDS[1:]:
        values = series[field]
        if field in FUNDING_FIELDS:
            if np.any(np.isinf(values)):
                raise ValueError(f"funding field contains infinity: {field}")
            continue
        if not np.all(np.isfinite(values)) or np.any(values < 0.0):
            raise ValueError(f"cross-venue field is invalid: {field}")
    for venue in ("bybit", "binance"):
        rate = np.isfinite(series[f"{venue}_funding_rate"])
        mark = np.isfinite(series[f"{venue}_funding_mark"])
        event_time = np.isfinite(series[f"{venue}_funding_timestamp"])
        if not (
            np.array_equal(rate, mark)
            and np.array_equal(rate, event_time)
            and not np.any(series[f"{venue}_funding_mark"][mark] <= 0.0)
        ):
            raise ValueError(f"{venue} funding rate/mark alignment is invalid")
        event_values = series[f"{venue}_funding_timestamp"][event_time]
        event_buckets = timestamps[event_time]
        if (
            np.any(event_values < event_buckets)
            or np.any(event_values >= event_buckets + 300_000)
            or (len(event_values) > 1 and not np.all(np.diff(event_values) > 0))
        ):
            raise ValueError(f"{venue} original funding timestamps are invalid")
    return series


def domain_identity(
    series: Mapping[str, np.ndarray], *, start_ms: int, end_ms: int
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    indices = np.flatnonzero((timestamps >= int(start_ms)) & (timestamps < int(end_ms)))
    if not len(indices):
        raise ValueError("cross-venue frozen domain is empty")
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


def validate_parent_manifest(path: pathlib.Path) -> Dict[str, Any]:
    parent = common.read_json(path)
    unsigned = {key: value for key, value in parent.items() if key != "identity_sha256"}
    if not (
        parent.get("schema_version") == PARENT_AUDIT_SCHEMA_VERSION
        and parent.get("identity_sha256") == common.canonical_sha256(unsigned)
        and len(parent.get("primary_splits", [])) == 6
        and isinstance(parent.get("boundary_splits"), Mapping)
    ):
        raise ValueError("parent carry audit manifest mismatch")
    if sorted(parent["boundary_splits"].keys(), key=int) != ["-3", "-2", "-1", "0"]:
        raise ValueError("parent carry boundary split mismatch")
    return parent


def create_manifest(
    series: Mapping[str, np.ndarray],
    policy: Mapping[str, Any],
    parent: Mapping[str, Any],
) -> Dict[str, Any]:
    primary = parent["primary_splits"]
    boundary = parent["boundary_splits"]
    frozen_start = min(
        int(split["fit_start_ms"])
        for values in boundary.values()
        for split in values
    )
    frozen_end = max(int(split["test_end_ms"]) for split in primary)
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    if int(timestamps[0]) > frozen_start or int(timestamps[-1]) < frozen_end - 300_000:
        raise development.CaptureNotReady(
            "cross-venue history does not cover parent absolute split calendar"
        )
    manifest: Dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "policy_identity_sha256": FROZEN_POLICY_IDENTITY_SHA256,
        "experiment_id": policy["experiment_id"],
        "split_calendar_source": "exact_parent_funding_basis_carry_absolute_splits_v1",
        "parent_audit_identity_sha256": parent["identity_sha256"],
        "frozen_domain": domain_identity(series, start_ms=frozen_start, end_ms=frozen_end),
        "primary_splits": primary,
        "boundary_splits": boundary,
    }
    manifest["identity_sha256"] = common.canonical_sha256(manifest)
    return manifest


def load_or_create_manifest(
    path: pathlib.Path,
    *,
    series: Mapping[str, np.ndarray],
    policy: Mapping[str, Any],
    parent: Mapping[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    created = False
    if not path.is_file():
        common.atomic_write_json(path, create_manifest(series, policy, parent))
        created = True
    manifest = common.read_json(path)
    unsigned = {key: value for key, value in manifest.items() if key != "identity_sha256"}
    if not (
        manifest.get("schema_version") == AUDIT_SCHEMA_VERSION
        and manifest.get("policy_identity_sha256") == FROZEN_POLICY_IDENTITY_SHA256
        and manifest.get("experiment_id") == policy["experiment_id"]
        and manifest.get("parent_audit_identity_sha256") == parent["identity_sha256"]
        and manifest.get("identity_sha256") == common.canonical_sha256(unsigned)
        and manifest.get("primary_splits") == parent["primary_splits"]
        and manifest.get("boundary_splits") == parent["boundary_splits"]
    ):
        raise ValueError("cross-venue funding frozen audit manifest mismatch")
    frozen = manifest.get("frozen_domain")
    if not isinstance(frozen, Mapping):
        raise ValueError("cross-venue funding frozen domain missing")
    actual = domain_identity(
        series, start_ms=int(frozen["start_ms"]), end_ms=int(frozen["end_ms"])
    )
    if actual != frozen:
        raise ValueError("cross-venue funding frozen domain drift")
    return manifest, created


def venue_events(
    series: Mapping[str, np.ndarray], venue: str
) -> Tuple[List[int], List[int]]:
    indices = np.flatnonzero(np.isfinite(series[f"{venue}_funding_rate"]))
    event_timestamps = np.asarray(
        series[f"{venue}_funding_timestamp"], dtype=np.float64
    )
    return [int(event_timestamps[index]) for index in indices], [int(index) for index in indices]


def funding_cash(
    *,
    series: Mapping[str, np.ndarray],
    venue: str,
    timestamps: Sequence[int],
    indices: Sequence[int],
    entry_timestamp: int,
    exit_timestamp: int,
    quantity: float,
    side_sign: float,
) -> Tuple[float, int]:
    start = bisect.bisect_right(timestamps, int(entry_timestamp))
    end = bisect.bisect_right(timestamps, int(exit_timestamp))
    cash = 0.0
    for index in indices[start:end]:
        cash += (
            side_sign
            * quantity
            * float(series[f"{venue}_funding_mark"][index])
            * float(series[f"{venue}_funding_rate"][index])
        )
    return cash, end - start


def candidate_outcome(
    *,
    series: Mapping[str, np.ndarray],
    positions: Mapping[int, int],
    events: Mapping[str, Tuple[Sequence[int], Sequence[int]]],
    entry_timestamp: int,
    exit_timestamp: int,
    direction: str,
    policy: Mapping[str, Any],
) -> Dict[str, float | int | str] | None:
    entry_index = positions.get(int(entry_timestamp))
    exit_index = positions.get(int(exit_timestamp))
    if entry_index is None or exit_index is None:
        return None
    execution = policy["execution"]
    if direction == "long_bybit_short_binance":
        bybit_entry_side, bybit_exit_side, bybit_funding_sign = "ask", "bid", -1.0
        binance_entry_side, binance_exit_side, binance_funding_sign = "bid", "ask", 1.0
    elif direction == "long_binance_short_bybit":
        bybit_entry_side, bybit_exit_side, bybit_funding_sign = "bid", "ask", 1.0
        binance_entry_side, binance_exit_side, binance_funding_sign = "ask", "bid", -1.0
    else:
        raise ValueError(f"unknown direction: {direction}")
    bybit_raw_entry = float(series["bybit_perpetual_open"][entry_index])
    binance_raw_entry = float(series["binance_perpetual_open"][entry_index])
    quantity = float(policy["mechanism"]["reference_entry_notional"]) / (
        (bybit_raw_entry + binance_raw_entry) / 2.0
    )
    bybit_entry = carry.quote(
        bybit_raw_entry, float(execution["bybit_half_spread_bps"]), bybit_entry_side
    )
    bybit_exit = carry.quote(
        float(series["bybit_perpetual_open"][exit_index]),
        float(execution["bybit_half_spread_bps"]),
        bybit_exit_side,
    )
    binance_entry = carry.quote(
        binance_raw_entry,
        float(execution["binance_half_spread_bps"]),
        binance_entry_side,
    )
    binance_exit = carry.quote(
        float(series["binance_perpetual_open"][exit_index]),
        float(execution["binance_half_spread_bps"]),
        binance_exit_side,
    )
    if min(bybit_entry, bybit_exit, binance_entry, binance_exit, quantity) <= 0.0:
        return None
    bybit_price_sign = 1.0 if direction == "long_bybit_short_binance" else -1.0
    basis_cash = quantity * (
        bybit_price_sign * (bybit_exit - bybit_entry)
        - bybit_price_sign * (binance_exit - binance_entry)
    )
    bybit_funding, bybit_event_count = funding_cash(
        series=series,
        venue="bybit",
        timestamps=events["bybit"][0],
        indices=events["bybit"][1],
        entry_timestamp=entry_timestamp,
        exit_timestamp=exit_timestamp,
        quantity=quantity,
        side_sign=bybit_funding_sign,
    )
    binance_funding, binance_event_count = funding_cash(
        series=series,
        venue="binance",
        timestamps=events["binance"][0],
        indices=events["binance"][1],
        entry_timestamp=entry_timestamp,
        exit_timestamp=exit_timestamp,
        quantity=quantity,
        side_sign=binance_funding_sign,
    )
    funding_total = bybit_funding + binance_funding
    bybit_rate = (
        float(execution["bybit_taker_fee_bps_per_fill"])
        + float(execution["bybit_slippage_bps_per_fill"])
    ) / 10_000.0
    binance_rate = (
        float(execution["binance_taker_fee_bps_per_fill"])
        + float(execution["binance_slippage_bps_per_fill"])
    ) / 10_000.0
    reference_notional = float(policy["mechanism"]["reference_entry_notional"])
    execution_cost = quantity * (
        bybit_rate * (bybit_entry + bybit_exit)
        + binance_rate * (binance_entry + binance_exit)
    ) + reference_notional * float(
        execution["intervenue_leg_risk_bps_per_round_trip"]
    ) / 10_000.0
    duration_days = (int(exit_timestamp) - int(entry_timestamp)) / 86_400_000.0
    capital = policy["capital_cost"]
    base_capital_cost = (
        reference_notional
        * float(capital["gross_capital_multiplier"])
        * float(capital["base_annual_rate"])
        * duration_days
        / float(capital["day_count"])
    )
    stress_capital_cost = (
        reference_notional
        * float(capital["gross_capital_multiplier"])
        * float(capital["stress_annual_rate"])
        * duration_days
        / float(capital["day_count"])
    )
    gross_cash = basis_cash + funding_total
    base_bps = (
        (gross_cash - execution_cost - base_capital_cost)
        / reference_notional
        * 10_000.0
    )
    stress_bps = (
        (
            gross_cash
            - execution_cost * float(execution["stress_execution_cost_multiplier"])
            - stress_capital_cost
        )
        / reference_notional
        * 10_000.0
    )
    return {
        "direction": direction,
        "base_bps": base_bps,
        "stress_bps": stress_bps,
        "gross_bps": gross_cash / reference_notional * 10_000.0,
        "basis_bps": basis_cash / reference_notional * 10_000.0,
        "funding_bps": funding_total / reference_notional * 10_000.0,
        "bybit_funding_bps": bybit_funding / reference_notional * 10_000.0,
        "binance_funding_bps": binance_funding / reference_notional * 10_000.0,
        "execution_cost_bps": execution_cost / reference_notional * 10_000.0,
        "bybit_funding_event_count": bybit_event_count,
        "binance_funding_event_count": binance_event_count,
    }


def exact_weighted_schedule(intervals: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(
        (dict(item) for item in intervals),
        key=lambda item: (
            int(item["exit_timestamp_ms"]),
            int(item["entry_timestamp_ms"]),
            str(item["direction"]),
            int(item["horizon_hours"]),
        ),
    )
    end_times = [int(item["exit_timestamp_ms"]) for item in ordered]
    predecessors = [
        bisect.bisect_right(end_times, int(item["entry_timestamp_ms"]), 0, index) - 1
        for index, item in enumerate(ordered)
    ]
    values = [0.0] * (len(ordered) + 1)
    choose = [False] * len(ordered)
    for index, item in enumerate(ordered, start=1):
        include = float(item["stress_bps"]) + values[predecessors[index - 1] + 1]
        if include > values[index - 1]:
            values[index] = include
            choose[index - 1] = True
        else:
            values[index] = values[index - 1]
    selected: List[Dict[str, Any]] = []
    cursor = len(ordered)
    while cursor > 0:
        if choose[cursor - 1] and values[cursor] > values[cursor - 1]:
            selected.append(ordered[cursor - 1])
            cursor = predecessors[cursor - 1] + 1
        else:
            cursor -= 1
    selected.reverse()
    return selected


def evaluate_split(
    *,
    series: Mapping[str, np.ndarray],
    split: development.TimeSplit,
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    positions = {int(value): index for index, value in enumerate(timestamps)}
    events = {venue: venue_events(series, venue) for venue in ("bybit", "binance")}
    latency_ms = int(policy["execution"]["entry_latency_bars"]) * 300_000
    positive: List[Dict[str, Any]] = []
    maximum: Dict[str, Any] | None = None
    observable = 0
    for raw_index in np.asarray(
        development.indices_between(timestamps, split.test_start_ms, split.test_end_ms),
        dtype=np.int64,
    ):
        entry_timestamp = int(timestamps[int(raw_index)]) + latency_ms
        if entry_timestamp not in positions:
            continue
        candidates: List[Dict[str, Any]] = []
        for horizon in policy["execution"]["horizons_hours"]:
            exit_timestamp = entry_timestamp + int(horizon) * 3_600_000
            if exit_timestamp >= split.test_end_ms:
                continue
            for direction in policy["mechanism"]["directions"]:
                outcome = candidate_outcome(
                    series=series,
                    positions=positions,
                    events=events,
                    entry_timestamp=entry_timestamp,
                    exit_timestamp=exit_timestamp,
                    direction=str(direction),
                    policy=policy,
                )
                if outcome is not None:
                    candidates.append(
                        {
                            "entry_timestamp_ms": entry_timestamp,
                            "exit_timestamp_ms": exit_timestamp,
                            "horizon_hours": int(horizon),
                            **outcome,
                        }
                    )
        if not candidates:
            continue
        observable += 1
        best = max(
            candidates,
            key=lambda item: (
                float(item["stress_bps"]),
                -int(item["horizon_hours"]),
                str(item["direction"]),
            ),
        )
        if maximum is None or float(best["stress_bps"]) > float(maximum["stress_bps"]):
            maximum = best
        if float(best["stress_bps"]) > 0.0:
            positive.append(best)
    selected = exact_weighted_schedule(positive)
    action_counts: Dict[str, int] = {}
    for item in selected:
        key = f"{item['direction']}_hold_{int(item['horizon_hours'])}h"
        action_counts[key] = action_counts.get(key, 0) + 1
    result: Dict[str, Any] = {
        "split_id": int(split.split_id),
        "observable_decision_count": observable,
        "positive_candidate_count": len(positive),
        "trade_count": len(selected),
        "bybit_funding_event_count": sum(
            int(item["bybit_funding_event_count"]) for item in selected
        ),
        "binance_funding_event_count": sum(
            int(item["binance_funding_event_count"]) for item in selected
        ),
        "action_counts": action_counts,
        "base_cost": development.summarize_edges(
            [float(item["base_bps"]) for item in selected]
        ),
        "stress_cost": development.summarize_edges(
            [float(item["stress_bps"]) for item in selected]
        ),
        "funding_component": development.summarize_edges(
            [float(item["funding_bps"]) for item in selected]
        ),
        "basis_component": development.summarize_edges(
            [float(item["basis_bps"]) for item in selected]
        ),
        "maximum_candidate": None,
    }
    if maximum is not None:
        result["maximum_candidate"] = {
            key: maximum[key]
            for key in (
                "direction",
                "horizon_hours",
                "base_bps",
                "stress_bps",
                "gross_bps",
                "basis_bps",
                "funding_bps",
                "bybit_funding_bps",
                "binance_funding_bps",
                "execution_cost_bps",
                "bybit_funding_event_count",
                "binance_funding_event_count",
            )
        }
    return result


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
    positive_ratio = sum(value > 0.0 for value in stress_means) / len(stress_means)
    source_events = {
        venue: int(np.count_nonzero(np.isfinite(series[f"{venue}_funding_rate"])))
        for venue in ("bybit", "binance")
    }
    stress_summary = development.summarize_edges(stress_means)
    maximum_candidates = [
        item["maximum_candidate"]
        for item in reports
        if isinstance(item.get("maximum_candidate"), Mapping)
    ]
    gates = policy["decision_gates"]
    proven = bool(
        trade_count >= int(gates["minimum_oos_trades"])
        and all(
            count >= int(gates["minimum_funding_events_per_venue"])
            for count in source_events.values()
        )
        and positive_ratio >= float(gates["minimum_positive_split_ratio"])
        and stress_summary.get("lcb_bps") is not None
        and float(stress_summary["lcb_bps"])
        > float(gates["minimum_oracle_stress_lcb_bps"])
    )
    return {
        "method": "six_parent_split_exact_weighted_interval_cross_venue_hindsight_upper_bound_v1",
        "fully_verifiable": len(reports) == int(policy["splits"]["count"]),
        "opportunity_proven": proven,
        "trade_count": trade_count,
        "source_funding_event_count_by_venue": source_events,
        "selected_funding_event_count_by_venue": {
            venue: sum(int(item[f"{venue}_funding_event_count"]) for item in reports)
            for venue in ("bybit", "binance")
        },
        "positive_stress_split_ratio": positive_ratio,
        "base_cost_by_split": development.summarize_edges(base_means),
        "stress_cost_by_split": stress_summary,
        "maximum_candidate": (
            max(maximum_candidates, key=lambda item: float(item["stress_bps"]))
            if maximum_candidates
            else None
        ),
        "split_reports": reports,
        "promotion_evidence": False,
    }


def run_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = pathlib.Path(args.config).resolve()
    data_path = pathlib.Path(args.history_csv).resolve()
    data_report_path = pathlib.Path(args.data_report).resolve()
    parent_path = pathlib.Path(args.parent_audit_manifest).resolve()
    manifest_path = pathlib.Path(args.audit_manifest).resolve()
    policy = validate_policy(config_path)
    parent = validate_parent_manifest(parent_path)
    data_report = common.read_json(data_report_path)
    if not (
        data_report.get("schema_version") == "cross_venue_funding_history_v1"
        and data_report.get("status") == "PASS"
        and data_report.get("output_sha256") == common.sha256_file(data_path)
        and data_report.get("causality", {}).get("funding_alignment")
        == "exact_venue_settlement_timestamp_once_only"
        and data_report.get("causality", {}).get("asof_funding_fill") is False
        and data_report.get("causality", {}).get(
            "original_funding_event_timestamp_preserved"
        )
        is True
        and data_report.get("parent_audit_manifest", {}).get("identity_sha256")
        == parent["identity_sha256"]
    ):
        raise development.CaptureNotReady(
            "cross-venue funding data report is not verifiable"
        )
    series = load_series(data_path)
    manifest, created = load_or_create_manifest(
        manifest_path, series=series, policy=policy, parent=parent
    )
    primary = build_oracle(
        series=series,
        splits=carry.splits_from_payload(manifest["primary_splits"]),
        policy=policy,
    )
    boundary_reports: List[Dict[str, Any]] = []
    for offset in policy["stability_audit"]["boundary_offsets_days"]:
        oracle = build_oracle(
            series=series,
            splits=carry.splits_from_payload(
                manifest["boundary_splits"][str(int(offset))]
            ),
            policy=policy,
        )
        boundary_reports.append(
            {
                "offset_days": int(offset),
                "opportunity_proven": bool(oracle["opportunity_proven"]),
                "trade_count": int(oracle["trade_count"]),
                "positive_stress_split_ratio": float(
                    oracle["positive_stress_split_ratio"]
                ),
                "stress_cost_by_split": oracle["stress_cost_by_split"],
            }
        )
    boundary_ratio = sum(item["opportunity_proven"] for item in boundary_reports) / len(
        boundary_reports
    )
    boundary_passed = boundary_ratio >= float(
        policy["stability_audit"]["minimum_boundary_pass_ratio"]
    )
    if primary["opportunity_proven"] and boundary_passed:
        decision = DECISION_CONTINUE
        reasons = ["historical_cross_venue_funding_upper_bound_and_boundaries_passed"]
        next_action = "collect_preregistered_raw_cross_venue_perpetual_bbo_forward_evidence"
    else:
        decision = DECISION_STOP
        reasons = []
        if not primary["opportunity_proven"]:
            reasons.append("historical_cross_venue_funding_upper_bound_failed")
        if not boundary_passed:
            reasons.append("cross_venue_funding_boundary_sensitivity_failed")
        next_action = "close_cross_venue_funding_differential_family_and_stage_review"
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
            "history_csv_path": str(data_path),
            "history_csv_sha256": common.sha256_file(data_path),
            "data_report_path": str(data_report_path),
            "data_report_sha256": common.sha256_file(data_report_path),
            "parent_audit_manifest_path": str(parent_path),
            "parent_audit_manifest_sha256": common.sha256_file(parent_path),
            "parent_audit_manifest_identity_sha256": parent["identity_sha256"],
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
            "funding_event_count_by_venue": {
                venue: int(
                    np.count_nonzero(np.isfinite(series[f"{venue}_funding_rate"]))
                )
                for venue in ("bybit", "binance")
            },
            "timestamp_sha256": common.array_sha256(timestamps),
            "splits": list(manifest["primary_splits"]),
        },
        "hindsight_oracle": primary,
        "stability_audit": {
            "manifest_created_this_run": created,
            "manifest_identity_sha256": manifest["identity_sha256"],
            "parent_audit_identity_sha256": parent["identity_sha256"],
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
        "next_action": "complete_verifiable_cross_venue_funding_history",
        "experiment_policy": {"path": str(pathlib.Path(args.config).resolve())},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-csv", required=True)
    parser.add_argument("--data-report", required=True)
    parser.add_argument("--parent-audit-manifest", required=True)
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
