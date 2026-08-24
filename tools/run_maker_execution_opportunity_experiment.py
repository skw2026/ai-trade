#!/usr/bin/env python3
"""Evaluate a conservative, fill-aware maker-entry hindsight opportunity bound.

This experiment deliberately runs before any model fitting.  It asks whether the
existing forward microstructure capture contains stable net opportunity when a
passive entry is credited only after both visible queue consumption and a strict
top-of-book trade-through.  The proxy is diagnostic and cannot authorize demo
or live trading.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
import pathlib
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np

import run_cross_venue_information_set_experiment as common
import run_microstructure_alpha_development as development


SCHEMA_VERSION = "maker_execution_opportunity_experiment_v1"
POLICY_SCHEMA_VERSION = "maker_execution_opportunity_policy_v1"
FROZEN_POLICY_IDENTITY_SHA256 = (
    "dc36fcb7344341f602b6a649bad88c831ec6c3e234b34f0cee43cca9d42ecbac"
)
DECISION_CONTINUE = "CONTINUE_TO_MAKER_LEARNABILITY_EXPERIMENT"
DECISION_STOP = "STOP_MAKER_EXECUTION_FAMILY"
DECISION_WAIT = "WAIT_FOR_INDEPENDENT_MAKER_FORWARD_WINDOW"
AUDIT_MANIFEST_SCHEMA_VERSION = "maker_opportunity_frozen_audit_v1"


def validate_policy(path: pathlib.Path) -> Dict[str, Any]:
    policy = common.read_json(path)
    failures: List[str] = []
    if common.canonical_sha256(policy) != FROZEN_POLICY_IDENTITY_SHA256:
        failures.append("policy_identity")
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        failures.append("schema_version")
    if not (
        policy.get("research_domain") == "development_only"
        and policy.get("promotion_evidence") is False
    ):
        failures.append("research_domain")
    actions = policy.get("actions")
    if not (
        isinstance(actions, Mapping)
        and actions.get("directions") == ["long", "short"]
        and actions.get("horizons_seconds") == [15, 30, 60, 120, 300]
        and actions.get("placement_latency_seconds") == 1
        and actions.get("fill_timeout_seconds") == 12
        and float(actions.get("maker_price_offset_bps", -1.0)) == 0.3
        and float(actions.get("price_tick_size", 0.0)) == 0.01
        and actions.get("post_only_timeout_seconds") == 6
        and actions.get("reprice_max_attempts") == 1
        and float(actions.get("reprice_bps", -1.0)) == 0.15
    ):
        failures.append("actions")
    fill_proxy = policy.get("fill_proxy")
    if not (
        isinstance(fill_proxy, Mapping)
        and fill_proxy.get("method")
        == "opposite_aggressor_quote_volume_and_top_of_book_trade_through_v1"
        and float(fill_proxy.get("queue_depth_multiplier", 0.0)) == 1.25
        and fill_proxy.get("resting_queue_depth_source")
        == "same_side_l5_cumulative_base_depth_at_placement"
        and fill_proxy.get("strict_trade_through_required") is True
        and fill_proxy.get("same_second_fill_permitted") is False
        and fill_proxy.get("partial_fill_permitted") is False
        and fill_proxy.get("fill_proxy_role")
        == "outcome_only_non_promotional_oracle"
        and fill_proxy.get("fill_proxy_used_as_model_feature") is False
    ):
        failures.append("fill_proxy")
    costs = policy.get("costs")
    if not (
        isinstance(costs, Mapping)
        and float(costs.get("maker_entry_fee_bps", 0.0)) == 2.75
        and float(costs.get("taker_exit_fee_bps", 0.0)) == 5.5
        and float(costs.get("exit_slippage_bps", 0.0)) == 1.0
        and float(costs.get("stress_cost_multiplier", 0.0)) == 1.25
    ):
        failures.append("costs")
    splits = policy.get("splits")
    if not (
        isinstance(splits, Mapping)
        and splits.get("count") == 6
        and splits.get("train_window_seconds") == 21600
        and splits.get("validation_window_seconds") == 14400
        and splits.get("test_window_seconds") == 14400
        and splits.get("rolling_step_seconds") == 14400
    ):
        failures.append("splits")
    stability = policy.get("stability_audit")
    if not (
        isinstance(stability, Mapping)
        and stability.get("manifest_schema_version")
        == AUDIT_MANIFEST_SCHEMA_VERSION
        and stability.get("boundary_offsets_seconds")
        == [0, -3600, -7200, -10800]
        and float(stability.get("minimum_boundary_pass_ratio", -1.0)) == 0.75
        and stability.get("independent_forward_window_seconds") == 86400
        and stability.get("forward_block_seconds") == 14400
        and stability.get("minimum_forward_blocks") == 6
        and float(stability.get("minimum_forward_row_ratio", -1.0)) == 0.95
        and stability.get("minimum_forward_trades") == 100
        and float(stability.get("minimum_positive_forward_block_ratio", -1.0))
        == 0.6
        and float(stability.get("minimum_forward_stress_lcb_bps", -1.0))
        == 0.0
        and stability.get("require_exact_frozen_domain") is True
    ):
        failures.append("stability_audit")
    gates = policy.get("decision_gates")
    if not (
        isinstance(gates, Mapping)
        and gates.get("minimum_oos_trades") == 100
        and float(gates.get("minimum_positive_split_ratio", -1.0)) == 0.6
        and float(gates.get("minimum_oracle_stress_lcb_bps", -1.0)) == 0.0
    ):
        failures.append("decision_gates")
    if policy.get("authorities") != {
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }:
        failures.append("authorities")
    if failures:
        raise ValueError(
            "frozen maker opportunity policy mismatch: " + ",".join(failures)
        )
    return policy


def _total_base_cost_bps(policy: Mapping[str, Any]) -> float:
    costs = policy["costs"]
    return sum(
        float(costs[field])
        for field in (
            "maker_entry_fee_bps",
            "taker_exit_fee_bps",
            "exit_slippage_bps",
        )
    )


def _shift_split(split: development.TimeSplit, offset_seconds: int) -> development.TimeSplit:
    offset_ms = int(offset_seconds) * 1000
    values = dataclasses.asdict(split)
    return development.TimeSplit(
        **{
            key: int(value) + (0 if key == "split_id" else offset_ms)
            for key, value in values.items()
        }
    )


def _series_domain_identity(
    series: Mapping[str, np.ndarray], *, start_ms: int, end_ms: int
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    mask = (timestamps >= int(start_ms)) & (timestamps < int(end_ms))
    indices = np.flatnonzero(mask)
    if not len(indices):
        raise ValueError("frozen maker audit domain is empty")
    fields = (
        "timestamp",
        "best_bid",
        "best_ask",
        "best_bid_size",
        "best_ask_size",
        "bid_depth_l5",
        "ask_depth_l5",
        "buy_quote_volume",
        "sell_quote_volume",
    )
    field_hashes = {
        name: common.array_sha256(np.asarray(series[name])[indices]) for name in fields
    }
    payload = {
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "row_count": int(len(indices)),
        "first_timestamp_ms": int(timestamps[indices[0]]),
        "last_timestamp_ms": int(timestamps[indices[-1]]),
        "field_sha256": field_hashes,
    }
    payload["identity_sha256"] = common.canonical_sha256(payload)
    return payload


def create_frozen_audit_manifest(
    *,
    series: Mapping[str, np.ndarray],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    actions = policy["actions"]
    split_policy = policy["splits"]
    stability = policy["stability_audit"]
    embargo = (
        int(actions["placement_latency_seconds"])
        + int(actions["fill_timeout_seconds"])
        + max(int(value) for value in actions["horizons_seconds"])
    )
    primary = development.build_time_splits(
        timestamps,
        n_splits=int(split_policy["count"]),
        train_window_seconds=int(split_policy["train_window_seconds"]),
        validation_window_seconds=int(split_policy["validation_window_seconds"]),
        test_window_seconds=int(split_policy["test_window_seconds"]),
        rolling_step_seconds=int(split_policy["rolling_step_seconds"]),
        embargo_seconds=embargo,
    )
    offsets = [int(value) for value in stability["boundary_offsets_seconds"]]
    shifted = {
        str(offset): [_shift_split(split, offset) for split in primary]
        for offset in offsets
    }
    frozen_start_ms = min(
        split.fit_start_ms for splits in shifted.values() for split in splits
    )
    frozen_end_ms = max(split.test_end_ms for split in primary)
    forward_start_ms = frozen_end_ms
    forward_end_ms = forward_start_ms + int(
        stability["independent_forward_window_seconds"]
    ) * 1000
    observation_tail_seconds = embargo
    manifest: Dict[str, Any] = {
        "schema_version": AUDIT_MANIFEST_SCHEMA_VERSION,
        "created_at_utc": dt.datetime.now(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "policy_identity_sha256": FROZEN_POLICY_IDENTITY_SHA256,
        "experiment_id": policy["experiment_id"],
        "frozen_domain": _series_domain_identity(
            series, start_ms=frozen_start_ms, end_ms=frozen_end_ms
        ),
        "primary_splits": [dataclasses.asdict(split) for split in primary],
        "boundary_splits": {
            key: [dataclasses.asdict(split) for split in splits]
            for key, splits in shifted.items()
        },
        "independent_forward": {
            "start_ms": forward_start_ms,
            "end_ms": forward_end_ms,
            "observation_end_ms": forward_end_ms
            + observation_tail_seconds * 1000,
            "block_seconds": int(stability["forward_block_seconds"]),
            "block_count": int(stability["minimum_forward_blocks"]),
            "observed_before_freeze": False,
        },
    }
    manifest["identity_sha256"] = common.canonical_sha256(manifest)
    return manifest


def load_or_create_frozen_audit_manifest(
    path: pathlib.Path,
    *,
    series: Mapping[str, np.ndarray],
    policy: Mapping[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    created = False
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = create_frozen_audit_manifest(series=series, policy=policy)
        common.atomic_write_json(path, manifest)
        created = True
    manifest = common.read_json(path)
    unsigned = {key: value for key, value in manifest.items() if key != "identity_sha256"}
    if not (
        manifest.get("schema_version") == AUDIT_MANIFEST_SCHEMA_VERSION
        and manifest.get("policy_identity_sha256") == FROZEN_POLICY_IDENTITY_SHA256
        and manifest.get("experiment_id") == policy["experiment_id"]
        and manifest.get("identity_sha256") == common.canonical_sha256(unsigned)
    ):
        raise ValueError("frozen maker opportunity audit manifest identity mismatch")
    primary = manifest.get("primary_splits")
    boundary = manifest.get("boundary_splits")
    if not (
        isinstance(primary, list)
        and len(primary) == int(policy["splits"]["count"])
        and isinstance(boundary, Mapping)
        and list(boundary) == [
            str(int(value)) for value in policy["stability_audit"]["boundary_offsets_seconds"]
        ]
    ):
        raise ValueError("frozen maker opportunity audit split manifest mismatch")
    frozen = manifest.get("frozen_domain")
    if not isinstance(frozen, Mapping):
        raise ValueError("frozen maker opportunity audit domain missing")
    actual = _series_domain_identity(
        series,
        start_ms=int(frozen["start_ms"]),
        end_ms=int(frozen["end_ms"]),
    )
    if actual != frozen:
        raise ValueError("frozen maker opportunity audit domain drift")
    return manifest, created


def _splits_from_manifest(values: Sequence[Mapping[str, Any]]) -> List[development.TimeSplit]:
    return [
        development.TimeSplit(
            split_id=int(value["split_id"]),
            fit_start_ms=int(value["fit_start_ms"]),
            fit_end_ms=int(value["fit_end_ms"]),
            validation_start_ms=int(value["validation_start_ms"]),
            validation_end_ms=int(value["validation_end_ms"]),
            test_start_ms=int(value["test_start_ms"]),
            test_end_ms=int(value["test_end_ms"]),
        )
        for value in values
    ]


def _forward_splits(manifest: Mapping[str, Any]) -> List[development.TimeSplit]:
    forward = manifest["independent_forward"]
    start_ms = int(forward["start_ms"])
    block_ms = int(forward["block_seconds"]) * 1000
    return [
        development.TimeSplit(
            split_id=index,
            fit_start_ms=start_ms,
            fit_end_ms=start_ms,
            validation_start_ms=start_ms,
            validation_end_ms=start_ms,
            test_start_ms=start_ms + index * block_ms,
            test_end_ms=start_ms + (index + 1) * block_ms,
        )
        for index in range(int(forward["block_count"]))
    ]


def build_maker_action_returns(
    series: Mapping[str, np.ndarray],
    *,
    horizons_seconds: Sequence[int],
    placement_latency_seconds: int,
    fill_timeout_seconds: int,
    queue_depth_multiplier: float,
    base_cost_bps: float,
    maker_price_offset_bps: float = 0.0,
    price_tick_size: float = 0.0,
    post_only_timeout_seconds: int | None = None,
    reprice_max_attempts: int = 0,
    reprice_bps: float = 0.0,
) -> Tuple[np.ndarray, np.ndarray, List[Dict[str, Any]], Dict[str, Any]]:
    """Build base-net outcomes only for conservatively inferred full fills."""

    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    best_bid = np.asarray(series["best_bid"], dtype=np.float64)
    best_ask = np.asarray(series["best_ask"], dtype=np.float64)
    best_bid_size = np.asarray(series["best_bid_size"], dtype=np.float64)
    best_ask_size = np.asarray(series["best_ask_size"], dtype=np.float64)
    bid_depth_l5 = np.asarray(
        series.get("bid_depth_l5", best_bid_size), dtype=np.float64
    )
    ask_depth_l5 = np.asarray(
        series.get("ask_depth_l5", best_ask_size), dtype=np.float64
    )
    buy_quote_volume = np.asarray(series["buy_quote_volume"], dtype=np.float64)
    sell_quote_volume = np.asarray(series["sell_quote_volume"], dtype=np.float64)
    lengths = {
        len(values)
        for values in (
            timestamps,
            best_bid,
            best_ask,
            best_bid_size,
            best_ask_size,
            bid_depth_l5,
            ask_depth_l5,
            buy_quote_volume,
            sell_quote_volume,
        )
    }
    if lengths != {len(timestamps)} or len(timestamps) == 0:
        raise ValueError("maker opportunity input arrays are not aligned")
    numeric = np.column_stack(
        (
            best_bid,
            best_ask,
            best_bid_size,
            best_ask_size,
            bid_depth_l5,
            ask_depth_l5,
            buy_quote_volume,
            sell_quote_volume,
        )
    )
    if not (
        np.all(np.diff(timestamps) > 0)
        and np.all(np.isfinite(numeric))
        and np.all(best_bid > 0.0)
        and np.all(best_ask >= best_bid)
        and np.all(best_bid_size > 0.0)
        and np.all(best_ask_size > 0.0)
        and np.all(bid_depth_l5 >= best_bid_size)
        and np.all(ask_depth_l5 >= best_ask_size)
        and np.all(buy_quote_volume >= 0.0)
        and np.all(sell_quote_volume >= 0.0)
    ):
        raise ValueError("maker opportunity market inputs are invalid")
    latency = int(placement_latency_seconds)
    timeout = int(fill_timeout_seconds)
    queue_multiplier = float(queue_depth_multiplier)
    cost = float(base_cost_bps)
    offset_ratio = float(maker_price_offset_bps) / 10000.0
    tick_size = float(price_tick_size)
    attempts = int(reprice_max_attempts)
    reprice_ratio = float(reprice_bps) / 10000.0
    attempt_timeout = int(post_only_timeout_seconds or timeout)
    if (
        latency <= 0
        or timeout <= 0
        or queue_multiplier < 1.0
        or cost <= 0.0
        or offset_ratio < 0.0
        or tick_size < 0.0
        or attempts < 0
        or reprice_ratio < 0.0
        or attempt_timeout <= 0
        or attempt_timeout * (attempts + 1) != timeout
    ):
        raise ValueError("maker opportunity execution contract is invalid")
    horizons = [int(value) for value in horizons_seconds]
    if not horizons or any(value <= 0 for value in horizons):
        raise ValueError("maker opportunity horizons are invalid")

    actions = [
        {"direction": direction, "horizon_seconds": horizon}
        for direction in ("long", "short")
        for horizon in horizons
    ]
    outcomes = np.full((len(timestamps), len(actions)), np.nan, dtype=np.float64)
    fill_timestamps = np.full(outcomes.shape, -1, dtype=np.int64)
    positions = {int(timestamp): index for index, timestamp in enumerate(timestamps)}
    decision_fill_directions = 0

    for row_index, decision_timestamp in enumerate(timestamps):
        direction_fills: Dict[str, Tuple[int, float]] = {}
        for direction in ("long", "short"):
            for attempt in range(attempts + 1):
                placement_timestamp = (
                    int(decision_timestamp)
                    + latency * 1000
                    + attempt * attempt_timeout * 1000
                )
                placement_index = positions.get(placement_timestamp)
                if placement_index is None:
                    break
                if direction == "long":
                    reference_price = float(best_bid[placement_index]) * (
                        1.0 + reprice_ratio * attempt
                    )
                    raw_posted_price = reference_price * (1.0 - offset_ratio)
                    posted_price = (
                        math.floor((raw_posted_price + 1.0e-12) / tick_size)
                        * tick_size
                        if tick_size > 0.0
                        else raw_posted_price
                    )
                    queue_size = float(bid_depth_l5[placement_index])
                else:
                    reference_price = float(best_ask[placement_index]) * (
                        1.0 - reprice_ratio * attempt
                    )
                    raw_posted_price = reference_price * (1.0 + offset_ratio)
                    posted_price = (
                        math.ceil((raw_posted_price - 1.0e-12) / tick_size)
                        * tick_size
                        if tick_size > 0.0
                        else raw_posted_price
                    )
                    queue_size = float(ask_depth_l5[placement_index])
                queue_quote = posted_price * queue_size * queue_multiplier
                cumulative_opposite_quote = 0.0
                for offset in range(1, attempt_timeout + 1):
                    probe_timestamp = placement_timestamp + offset * 1000
                    probe_index = positions.get(probe_timestamp)
                    if probe_index is None:
                        break
                    if direction == "long":
                        cumulative_opposite_quote += float(
                            sell_quote_volume[probe_index]
                        )
                        traded_through = float(best_bid[probe_index]) < posted_price
                    else:
                        cumulative_opposite_quote += float(buy_quote_volume[probe_index])
                        traded_through = float(best_ask[probe_index]) > posted_price
                    if traded_through and cumulative_opposite_quote >= queue_quote:
                        direction_fills[direction] = (probe_index, posted_price)
                        decision_fill_directions += 1
                        break
                if direction in direction_fills:
                    break

        for action_index, action in enumerate(actions):
            fill = direction_fills.get(str(action["direction"]))
            if fill is None:
                continue
            fill_index, fill_price = fill
            fill_timestamp = int(timestamps[fill_index])
            exit_index = positions.get(
                fill_timestamp + int(action["horizon_seconds"]) * 1000
            )
            if exit_index is None:
                continue
            if action["direction"] == "long":
                gross_bps = (float(best_bid[exit_index]) / fill_price - 1.0) * 10000.0
            else:
                gross_bps = (fill_price / float(best_ask[exit_index]) - 1.0) * 10000.0
            outcomes[row_index, action_index] = gross_bps - cost
            fill_timestamps[row_index, action_index] = fill_timestamp

    finite = np.isfinite(outcomes)
    return outcomes, fill_timestamps, actions, {
        "decision_row_count": int(len(timestamps)),
        "filled_decision_count": int(np.sum(np.any(finite, axis=1))),
        "filled_action_count": int(np.sum(finite)),
        "filled_direction_count": int(decision_fill_directions),
        "fill_proxy_method": (
            "opposite_aggressor_quote_volume_and_top_of_book_trade_through_v1"
        ),
        "same_second_fill_permitted": False,
        "partial_fill_permitted": False,
        "maker_price_offset_bps": float(maker_price_offset_bps),
        "price_tick_size": tick_size,
        "price_quantization": "passive_floor_buy_ceil_sell",
        "resting_queue_depth_source": (
            "same_side_l5_cumulative_base_depth_at_placement"
        ),
        "post_only_timeout_seconds": attempt_timeout,
        "reprice_max_attempts": attempts,
        "reprice_bps": float(reprice_bps),
    }


def evaluate_fill_aware_oracle(
    *,
    timestamps: np.ndarray,
    outcomes: np.ndarray,
    fill_timestamps: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    indices: np.ndarray,
    base_cost_bps: float,
    stress_cost_multiplier: float,
) -> Dict[str, Any]:
    stress_increment = float(base_cost_bps) * (
        float(stress_cost_multiplier) - 1.0
    )
    base_edges: List[float] = []
    stress_edges: List[float] = []
    action_counts: Dict[str, int] = {}
    fill_latency_seconds: List[float] = []
    next_allowed_ms = -1
    for raw_index in np.asarray(indices, dtype=np.int64):
        row_index = int(raw_index)
        decision_timestamp = int(timestamps[row_index])
        if decision_timestamp < next_allowed_ms:
            continue
        row = np.asarray(outcomes[row_index], dtype=np.float64)
        allowed = np.flatnonzero(np.isfinite(row) & ((row - stress_increment) > 0.0))
        if not len(allowed):
            continue
        action_index = int(allowed[int(np.argmax(row[allowed]))])
        fill_timestamp = int(fill_timestamps[row_index, action_index])
        if fill_timestamp <= decision_timestamp:
            raise ValueError("maker oracle fill timestamp is invalid")
        action = actions[action_index]
        horizon = int(action["horizon_seconds"])
        base_edge = float(row[action_index])
        base_edges.append(base_edge)
        stress_edges.append(base_edge - stress_increment)
        key = f"{action['direction']}_{horizon}s"
        action_counts[key] = action_counts.get(key, 0) + 1
        fill_latency_seconds.append((fill_timestamp - decision_timestamp) / 1000.0)
        next_allowed_ms = fill_timestamp + horizon * 1000
    return {
        "base_cost": development.summarize_edges(base_edges),
        "stress_cost": development.summarize_edges(stress_edges),
        "action_counts": action_counts,
        "mean_fill_latency_seconds": (
            float(np.mean(fill_latency_seconds)) if fill_latency_seconds else None
        ),
    }


def build_oracle(
    *,
    timestamps: np.ndarray,
    outcomes: np.ndarray,
    fill_timestamps: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    splits: Sequence[development.TimeSplit],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    reports: List[Dict[str, Any]] = []
    base_means: List[float] = []
    stress_means: List[float] = []
    trade_count = 0
    base_cost = _total_base_cost_bps(policy)
    multiplier = float(policy["costs"]["stress_cost_multiplier"])
    for split in splits:
        indices = development.indices_between(
            timestamps, split.test_start_ms, split.test_end_ms
        )
        if not len(indices):
            raise ValueError(f"maker oracle split {split.split_id} has no test rows")
        report = evaluate_fill_aware_oracle(
            timestamps=timestamps,
            outcomes=outcomes,
            fill_timestamps=fill_timestamps,
            actions=actions,
            indices=indices,
            base_cost_bps=base_cost,
            stress_cost_multiplier=multiplier,
        )
        base_mean = report["base_cost"].get("mean_bps")
        stress_mean = report["stress_cost"].get("mean_bps")
        base_means.append(float(base_mean) if base_mean is not None else 0.0)
        stress_means.append(float(stress_mean) if stress_mean is not None else 0.0)
        trade_count += int(report["base_cost"].get("count") or 0)
        reports.append({"split_id": int(split.split_id), **report})
    base_summary = development.summarize_edges(base_means)
    stress_summary = development.summarize_edges(stress_means)
    positive_ratio = sum(value > 0.0 for value in stress_means) / len(stress_means)
    gates = policy["decision_gates"]
    opportunity_proven = bool(
        len(reports) == int(policy["splits"]["count"])
        and trade_count >= int(gates["minimum_oos_trades"])
        and positive_ratio >= float(gates["minimum_positive_split_ratio"])
        and (stress_summary.get("lcb_bps") or float("-inf"))
        > float(gates["minimum_oracle_stress_lcb_bps"])
    )
    return {
        "method": "six_split_non_overlapping_fill_aware_hindsight_upper_bound_v1",
        "fully_verifiable": len(reports) == int(policy["splits"]["count"]),
        "opportunity_proven": opportunity_proven,
        "trade_count": trade_count,
        "positive_stress_split_ratio": positive_ratio,
        "base_cost_by_split": base_summary,
        "stress_cost_by_split": stress_summary,
        "split_reports": reports,
        "promotion_evidence": False,
    }


def decide(oracle: Mapping[str, Any], policy: Mapping[str, Any]) -> Tuple[str, List[str]]:
    if oracle.get("fully_verifiable") is not True:
        return DECISION_STOP, ["maker_oracle_incomplete"]
    if oracle.get("opportunity_proven") is True:
        return DECISION_CONTINUE, ["maker_oracle_all_first_window_gates_passed"]
    reasons: List[str] = []
    gates = policy["decision_gates"]
    if int(oracle.get("trade_count") or 0) < int(gates["minimum_oos_trades"]):
        reasons.append("maker_oracle_trade_count_below_minimum")
    if float(oracle.get("positive_stress_split_ratio") or 0.0) < float(
        gates["minimum_positive_split_ratio"]
    ):
        reasons.append("maker_oracle_positive_split_ratio_below_minimum")
    stress = oracle.get("stress_cost_by_split")
    stress_lcb = stress.get("lcb_bps") if isinstance(stress, Mapping) else None
    if stress_lcb is None or float(stress_lcb) <= float(
        gates["minimum_oracle_stress_lcb_bps"]
    ):
        reasons.append("maker_oracle_stress_lcb_not_positive")
    return DECISION_STOP, reasons or ["maker_oracle_not_proven"]


def evaluate_stability_audit(
    *,
    manifest: Mapping[str, Any],
    manifest_created: bool,
    timestamps: np.ndarray,
    outcomes: np.ndarray,
    fill_timestamps: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    primary = build_oracle(
        timestamps=timestamps,
        outcomes=outcomes,
        fill_timestamps=fill_timestamps,
        actions=actions,
        splits=_splits_from_manifest(manifest["primary_splits"]),
        policy=policy,
    )
    boundary_reports: List[Dict[str, Any]] = []
    for offset in policy["stability_audit"]["boundary_offsets_seconds"]:
        oracle = build_oracle(
            timestamps=timestamps,
            outcomes=outcomes,
            fill_timestamps=fill_timestamps,
            actions=actions,
            splits=_splits_from_manifest(manifest["boundary_splits"][str(int(offset))]),
            policy=policy,
        )
        boundary_reports.append(
            {
                "offset_seconds": int(offset),
                "opportunity_proven": bool(oracle["opportunity_proven"]),
                "trade_count": int(oracle["trade_count"]),
                "positive_stress_split_ratio": float(
                    oracle["positive_stress_split_ratio"]
                ),
                "stress_cost_by_split": oracle["stress_cost_by_split"],
            }
        )
    boundary_pass_ratio = sum(
        item["opportunity_proven"] for item in boundary_reports
    ) / len(boundary_reports)

    stability = policy["stability_audit"]
    forward_contract = manifest["independent_forward"]
    forward_start = int(forward_contract["start_ms"])
    forward_end = int(forward_contract["end_ms"])
    forward_observation_end = int(forward_contract["observation_end_ms"])
    forward_indices = development.indices_between(
        timestamps, forward_start, forward_end
    )
    expected_rows = int(stability["independent_forward_window_seconds"])
    row_ratio = len(forward_indices) / expected_rows if expected_rows else 0.0
    observation_complete = bool(
        int(timestamps[-1]) >= forward_observation_end
        and row_ratio >= float(stability["minimum_forward_row_ratio"])
    )
    forward_oracle: Dict[str, Any] | None = None
    forward_proven = False
    if observation_complete:
        forward_oracle = build_oracle(
            timestamps=timestamps,
            outcomes=outcomes,
            fill_timestamps=fill_timestamps,
            actions=actions,
            splits=_forward_splits(manifest),
            policy=policy,
        )
        stress_lcb = forward_oracle["stress_cost_by_split"].get("lcb_bps")
        forward_proven = bool(
            int(forward_oracle["trade_count"])
            >= int(stability["minimum_forward_trades"])
            and float(forward_oracle["positive_stress_split_ratio"])
            >= float(stability["minimum_positive_forward_block_ratio"])
            and stress_lcb is not None
            and float(stress_lcb)
            > float(stability["minimum_forward_stress_lcb_bps"])
        )
    boundary_proven = boundary_pass_ratio >= float(
        stability["minimum_boundary_pass_ratio"]
    )
    stable = bool(
        observation_complete
        and primary["opportunity_proven"]
        and boundary_proven
        and forward_proven
    )
    return {
        "manifest_created_this_run": bool(manifest_created),
        "manifest_identity_sha256": manifest["identity_sha256"],
        "state": "COMPLETE" if observation_complete else "AWAITING_FORWARD",
        "primary_oracle": primary,
        "boundary_sensitivity": {
            "reports": boundary_reports,
            "pass_ratio": boundary_pass_ratio,
            "minimum_pass_ratio": float(stability["minimum_boundary_pass_ratio"]),
            "passed": boundary_proven,
            "diagnostic_only": True,
        },
        "independent_forward": {
            **dict(forward_contract),
            "observed_row_count": int(len(forward_indices)),
            "expected_row_count": expected_rows,
            "row_ratio": row_ratio,
            "observation_complete": observation_complete,
            "oracle": forward_oracle,
            "passed": forward_proven,
        },
        "stable_opportunity_proven": stable,
    }


def decide_stability(audit: Mapping[str, Any]) -> Tuple[str, List[str]]:
    if audit.get("state") != "COMPLETE":
        return DECISION_WAIT, ["independent_24h_forward_window_incomplete"]
    if audit.get("stable_opportunity_proven") is True:
        return DECISION_CONTINUE, ["frozen_boundary_and_forward_opportunity_gates_passed"]
    reasons: List[str] = []
    primary = audit.get("primary_oracle", {})
    boundary = audit.get("boundary_sensitivity", {})
    forward = audit.get("independent_forward", {})
    if not isinstance(primary, Mapping) or primary.get("opportunity_proven") is not True:
        reasons.append("frozen_primary_opportunity_failed")
    if not isinstance(boundary, Mapping) or boundary.get("passed") is not True:
        reasons.append("boundary_sensitivity_failed")
    if not isinstance(forward, Mapping) or forward.get("passed") is not True:
        reasons.append("independent_forward_opportunity_failed")
    return DECISION_STOP, reasons or ["stable_maker_opportunity_not_proven"]


def run_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = pathlib.Path(args.config).resolve()
    assessment_path = pathlib.Path(args.control_assessment).resolve()
    policy = validate_policy(config_path)
    assessment = development.validate_capture_assessment(assessment_path)
    series = development.load_capture_rows(assessment)
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    actions_policy = policy["actions"]
    fill_policy = policy["fill_proxy"]
    base_cost = _total_base_cost_bps(policy)
    outcomes, fill_timestamps, actions, fill_audit = build_maker_action_returns(
        series,
        horizons_seconds=actions_policy["horizons_seconds"],
        placement_latency_seconds=int(actions_policy["placement_latency_seconds"]),
        fill_timeout_seconds=int(actions_policy["fill_timeout_seconds"]),
        queue_depth_multiplier=float(fill_policy["queue_depth_multiplier"]),
        base_cost_bps=base_cost,
        maker_price_offset_bps=float(actions_policy["maker_price_offset_bps"]),
        price_tick_size=float(actions_policy["price_tick_size"]),
        post_only_timeout_seconds=int(
            actions_policy["post_only_timeout_seconds"]
        ),
        reprice_max_attempts=int(actions_policy["reprice_max_attempts"]),
        reprice_bps=float(actions_policy["reprice_bps"]),
    )
    audit_manifest_path = pathlib.Path(args.audit_manifest).resolve()
    audit_manifest, manifest_created = load_or_create_frozen_audit_manifest(
        audit_manifest_path, series=series, policy=policy
    )
    stability_audit = evaluate_stability_audit(
        manifest=audit_manifest,
        manifest_created=manifest_created,
        timestamps=timestamps,
        outcomes=outcomes,
        fill_timestamps=fill_timestamps,
        actions=actions,
        policy=policy,
    )
    oracle = stability_audit["primary_oracle"]
    decision, reasons = decide_stability(stability_audit)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE",
        "fully_verifiable": stability_audit["state"] == "COMPLETE",
        "research_domain": "forward_development_only",
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
            "control_assessment_path": str(assessment_path),
            "control_assessment_sha256": common.sha256_file(assessment_path),
            "frozen_audit_manifest_path": str(audit_manifest_path),
            "frozen_audit_manifest_sha256": common.sha256_file(audit_manifest_path),
            "frozen_audit_manifest_identity_sha256": audit_manifest[
                "identity_sha256"
            ],
        },
        "execution_contract": {
            "base_cost_bps": base_cost,
            "stress_cost_multiplier": float(policy["costs"]["stress_cost_multiplier"]),
            "fill_proxy": dict(fill_policy),
            "actions": dict(actions_policy),
        },
        "common_domain": {
            "row_count": int(len(timestamps)),
            "first_timestamp_ms": int(timestamps[0]),
            "last_timestamp_ms": int(timestamps[-1]),
            "timestamp_sha256": common.array_sha256(timestamps),
            "splits": list(audit_manifest["primary_splits"]),
        },
        "fill_audit": fill_audit,
        "hindsight_oracle": oracle,
        "stability_audit": stability_audit,
        "research_decision": decision,
        "reason_codes": reasons,
        "next_action": {
            DECISION_CONTINUE: "preregister_fill_aware_maker_learnability_experiment",
            DECISION_STOP: "close_maker_execution_family_and_change_horizon_or_payoff",
            DECISION_WAIT: "collect_unseen_24h_forward_window_without_changing_contract",
        }[decision],
    }


def not_ready_report(args: argparse.Namespace, reason_code: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "NOT_READY",
        "fully_verifiable": False,
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "research_decision": "NOT_READY",
        "reason_codes": [reason_code],
        "next_action": "complete_control_capture_or_fix_experiment_input",
        "experiment_policy": {"path": str(pathlib.Path(args.config).resolve())},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-assessment", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--audit-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--research-domain", default="development", choices=("development",)
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = run_experiment(args)
    except development.CaptureNotReady:
        report = not_ready_report(args, "control_capture_not_ready")
    except Exception:
        report = not_ready_report(args, "invalid_input")
    common.atomic_write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report.get("status") == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
