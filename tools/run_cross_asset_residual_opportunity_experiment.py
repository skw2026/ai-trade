#!/usr/bin/env python3
"""Audit a fit-only, dollar-neutral SOL/BTC/ETH residual payoff before modeling.

The experiment is development-only.  It uses fit-window hedge weights, observed
or reconstructable taker quotes, full multi-leg costs, non-overlapping hindsight
actions, frozen absolute OOS splits, shifted-boundary diagnostics, and an
independent forward window.  It can never authorize demo or live trading.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np

import run_cross_venue_information_set_experiment as common
import run_maker_execution_opportunity_experiment as maker
import run_microstructure_alpha_development as development


SCHEMA_VERSION = "cross_asset_residual_opportunity_experiment_v1"
POLICY_SCHEMA_VERSION = "cross_asset_residual_opportunity_policy_v1"
FROZEN_POLICY_IDENTITY_SHA256 = (
    "5557603ae912ba50c28b007ddc3050e2d3fa5dd9672547923d3729eec6664ad7"
)
AUDIT_MANIFEST_SCHEMA_VERSION = "cross_asset_residual_frozen_audit_v1"
DECISION_CONTINUE = "CONTINUE_TO_CROSS_ASSET_RESIDUAL_LEARNABILITY_EXPERIMENT"
DECISION_STOP = "STOP_CROSS_ASSET_RESIDUAL_FAMILY"
DECISION_WAIT = "WAIT_FOR_INDEPENDENT_CROSS_ASSET_RESIDUAL_FORWARD_WINDOW"


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
        == "bybit_sol_btc_eth_dollar_neutral_residual_opportunity_v1"
        and policy.get("source_information_set")
        == "bybit_sol_btc_eth_l50_public_trades_v3"
    ):
        failures.append("research_domain")
    mechanism = policy.get("mechanism")
    if not (
        isinstance(mechanism, Mapping)
        and mechanism.get("target_symbol") == "SOLUSDT"
        and mechanism.get("hedge_symbols") == ["BTCUSDT", "ETHUSDT"]
        and float(mechanism.get("target_notional", 0.0)) == 1.0
        and float(mechanism.get("hedge_total_notional", 0.0)) == 1.0
        and mechanism.get("position_contract")
        == "long_or_short_target_against_opposite_convex_hedge_basket"
        and mechanism.get("hedge_weight_method")
        == "fit_only_minimum_one_second_mid_log_return_residual_variance_grid_v1"
        and float(mechanism.get("btc_weight_minimum", -1.0)) == 0.0
        and float(mechanism.get("btc_weight_maximum", -1.0)) == 1.0
        and float(mechanism.get("btc_weight_step", -1.0)) == 0.05
        and int(mechanism.get("minimum_fit_return_count", 0)) == 1000
        and mechanism.get("weight_tie_break") == "smallest_btc_weight"
        and mechanism.get("test_or_boundary_outcomes_used_for_weight_selection")
        is False
    ):
        failures.append("mechanism")
    execution = policy.get("execution")
    if not (
        isinstance(execution, Mapping)
        and execution.get("directions") == ["long_residual", "short_residual"]
        and execution.get("horizons_seconds") == [15, 30, 60, 120, 300]
        and int(execution.get("entry_latency_seconds", 0)) == 1
        and execution.get("entry_execution")
        == "simultaneous_taker_bid_ask_all_legs"
        and execution.get("exit_execution")
        == "simultaneous_taker_bid_ask_all_legs"
        and execution.get("context_quote_reconstruction")
        == "mid_plus_minus_half_reported_spread_v1"
        and execution.get("one_outstanding_portfolio") is True
        and execution.get("occupancy_release") == "realized_exit_timestamp"
    ):
        failures.append("execution")
    costs = policy.get("costs")
    if not (
        isinstance(costs, Mapping)
        and float(costs.get("taker_fee_bps_per_fill", 0.0)) == 5.5
        and float(costs.get("slippage_bps_per_fill", 0.0)) == 1.0
        and costs.get("spread_source")
        == "implicit_in_observed_or_reconstructed_bid_ask"
        and float(costs.get("base_cost_gross_notional_multiplier", 0.0)) == 2.0
        and float(costs.get("stress_cost_multiplier", 0.0)) == 1.25
    ):
        failures.append("costs")
    split_calendar = policy.get("split_calendar")
    if split_calendar != {
        "source": "inherit_exact_absolute_maker_v2_primary_and_boundary_splits",
        "parent_manifest_schema_version": maker.BASELINE_AUDIT_SCHEMA_VERSION,
        "parent_experiment_id": maker.BASELINE_EXPERIMENT_ID,
        "parent_policy_identity_sha256": maker.BASELINE_POLICY_IDENTITY_SHA256,
    }:
        failures.append("split_calendar")
    splits = policy.get("splits")
    if splits != {
        "count": 6,
        "train_window_seconds": 21600,
        "validation_window_seconds": 14400,
        "test_window_seconds": 14400,
        "rolling_step_seconds": 14400,
    }:
        failures.append("splits")
    stability = policy.get("stability_audit")
    if not (
        isinstance(stability, Mapping)
        and stability.get("manifest_schema_version")
        == AUDIT_MANIFEST_SCHEMA_VERSION
        and stability.get("boundary_offsets_seconds") == [0, -3600, -7200, -10800]
        and float(stability.get("minimum_boundary_pass_ratio", 0.0)) == 0.75
        and int(stability.get("independent_forward_window_seconds", 0)) == 86400
        and int(stability.get("forward_block_seconds", 0)) == 14400
        and int(stability.get("minimum_forward_blocks", 0)) == 6
        and float(stability.get("minimum_forward_row_ratio", 0.0)) == 0.95
        and int(stability.get("minimum_forward_trades", 0)) == 100
        and float(stability.get("minimum_positive_forward_block_ratio", 0.0))
        == 0.6
        and float(stability.get("minimum_forward_stress_lcb_bps", -1.0)) == 0.0
        and stability.get("require_exact_frozen_domain") is True
        and stability.get("require_parent_target_domain_identity") is True
    ):
        failures.append("stability_audit")
    gates = policy.get("decision_gates")
    if gates != {
        "minimum_oos_trades": 100,
        "minimum_positive_split_ratio": 0.6,
        "minimum_oracle_stress_lcb_bps": 0.0,
    }:
        failures.append("decision_gates")
    controls = policy.get("diagnostic_controls")
    if controls != {
        "target_only_all_taker": True,
        "hedge_time_shift_seconds": -3600,
        "controls_can_authorize_continuation": False,
    }:
        failures.append("diagnostic_controls")
    authorities = policy.get("authorities")
    if authorities != {
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }:
        failures.append("authorities")
    if failures:
        raise ValueError("cross-asset residual policy mismatch: " + ",".join(failures))
    return policy


DOMAIN_FIELDS = (
    "timestamp",
    "best_bid",
    "best_ask",
    "best_bid_size",
    "best_ask_size",
    "bid_depth_l5",
    "ask_depth_l5",
    "buy_quote_volume",
    "sell_quote_volume",
    "btc_mid",
    "btc_spread_bps",
    "btc_best_bid_size",
    "btc_best_ask_size",
    "btc_bid_depth_l5",
    "btc_ask_depth_l5",
    "btc_buy_quote_volume",
    "btc_sell_quote_volume",
    "eth_mid",
    "eth_spread_bps",
    "eth_best_bid_size",
    "eth_best_ask_size",
    "eth_bid_depth_l5",
    "eth_ask_depth_l5",
    "eth_buy_quote_volume",
    "eth_sell_quote_volume",
)


def series_domain_identity(
    series: Mapping[str, np.ndarray], *, start_ms: int, end_ms: int
) -> Dict[str, Any]:
    missing = [field for field in DOMAIN_FIELDS if field not in series]
    if missing:
        raise development.CaptureNotReady(
            "cross-asset residual capture fields missing: " + ",".join(missing)
        )
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    indices = np.flatnonzero((timestamps >= int(start_ms)) & (timestamps < int(end_ms)))
    if not len(indices):
        raise ValueError("cross-asset residual frozen domain is empty")
    payload: Dict[str, Any] = {
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "row_count": int(len(indices)),
        "first_timestamp_ms": int(timestamps[indices[0]]),
        "last_timestamp_ms": int(timestamps[indices[-1]]),
        "field_sha256": {
            field: common.array_sha256(np.asarray(series[field])[indices])
            for field in DOMAIN_FIELDS
        },
    }
    payload["identity_sha256"] = common.canonical_sha256(payload)
    return payload


def reconstruct_quotes(series: Mapping[str, np.ndarray]) -> Dict[str, Dict[str, np.ndarray]]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    sol_bid = np.asarray(series["best_bid"], dtype=np.float64)
    sol_ask = np.asarray(series["best_ask"], dtype=np.float64)
    quotes: Dict[str, Dict[str, np.ndarray]] = {
        "sol": {"bid": sol_bid, "ask": sol_ask}
    }
    for prefix in ("btc", "eth"):
        mid = np.asarray(series[f"{prefix}_mid"], dtype=np.float64)
        spread = np.asarray(series[f"{prefix}_spread_bps"], dtype=np.float64)
        half = spread / 20000.0
        quotes[prefix] = {"bid": mid * (1.0 - half), "ask": mid * (1.0 + half)}
    lengths = {len(timestamps)}
    for pair in quotes.values():
        lengths.update(len(values) for values in pair.values())
        if not (
            np.all(np.isfinite(pair["bid"]))
            and np.all(np.isfinite(pair["ask"]))
            and np.all(pair["bid"] > 0.0)
            and np.all(pair["ask"] > pair["bid"])
        ):
            raise ValueError("cross-asset residual quote reconstruction is invalid")
    if lengths != {len(timestamps)} or not np.all(np.diff(timestamps) > 0):
        raise ValueError("cross-asset residual quote arrays are not aligned")
    return quotes


def estimate_fit_only_btc_weight(
    series: Mapping[str, np.ndarray],
    indices: np.ndarray,
    policy: Mapping[str, Any],
) -> Tuple[float, Dict[str, Any]]:
    rows = np.asarray(indices, dtype=np.int64)
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)[rows]
    sol_mid = 0.5 * (
        np.asarray(series["best_bid"], dtype=np.float64)[rows]
        + np.asarray(series["best_ask"], dtype=np.float64)[rows]
    )
    btc_mid = np.asarray(series["btc_mid"], dtype=np.float64)[rows]
    eth_mid = np.asarray(series["eth_mid"], dtype=np.float64)[rows]
    contiguous = np.diff(timestamps) == 1000
    sol_return = np.diff(np.log(sol_mid))[contiguous]
    btc_return = np.diff(np.log(btc_mid))[contiguous]
    eth_return = np.diff(np.log(eth_mid))[contiguous]
    mechanism = policy["mechanism"]
    minimum_count = int(mechanism["minimum_fit_return_count"])
    if len(sol_return) < minimum_count:
        raise development.CaptureNotReady(
            f"cross-asset residual contiguous fit returns {len(sol_return)} < {minimum_count}"
        )
    lower = float(mechanism["btc_weight_minimum"])
    upper = float(mechanism["btc_weight_maximum"])
    step = float(mechanism["btc_weight_step"])
    count = int(round((upper - lower) / step))
    candidates = [round(lower + index * step, 12) for index in range(count + 1)]
    scored = []
    for weight in candidates:
        residual = sol_return - weight * btc_return - (1.0 - weight) * eth_return
        scored.append((float(np.var(residual, ddof=1)), float(weight)))
    variance, weight = min(scored, key=lambda item: (item[0], item[1]))
    return weight, {
        "method": mechanism["hedge_weight_method"],
        "btc_weight": weight,
        "eth_weight": 1.0 - weight,
        "fit_return_count": int(len(sol_return)),
        "residual_variance": variance,
        "grid_count": len(candidates),
        "test_outcomes_used": False,
    }


def base_cost_bps(policy: Mapping[str, Any], *, target_only: bool = False) -> float:
    costs = policy["costs"]
    per_fill = float(costs["taker_fee_bps_per_fill"]) + float(
        costs["slippage_bps_per_fill"]
    )
    gross = 1.0 if target_only else float(costs["base_cost_gross_notional_multiplier"])
    return 2.0 * per_fill * gross


def _long_return_bps(entry_ask: float, exit_bid: float) -> float:
    return (float(exit_bid) / float(entry_ask) - 1.0) * 10000.0


def _short_return_bps(entry_bid: float, exit_ask: float) -> float:
    return (1.0 - float(exit_ask) / float(entry_bid)) * 10000.0


def evaluate_split_oracle(
    *,
    series: Mapping[str, np.ndarray],
    quotes: Mapping[str, Mapping[str, np.ndarray]],
    split: development.TimeSplit,
    policy: Mapping[str, Any],
    fixed_btc_weight: float | None = None,
    target_only: bool = False,
    context_shift_seconds: int = 0,
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    positions = {int(value): index for index, value in enumerate(timestamps)}
    fit_indices = development.indices_between(
        timestamps, split.fit_start_ms, split.fit_end_ms
    )
    if fixed_btc_weight is None:
        if not len(fit_indices):
            raise ValueError("cross-asset residual split has no fit rows")
        btc_weight, weight_audit = estimate_fit_only_btc_weight(
            series, fit_indices, policy
        )
    else:
        btc_weight = float(fixed_btc_weight)
        weight_audit = {
            "method": "frozen_manifest_forward_weight",
            "btc_weight": btc_weight,
            "eth_weight": 1.0 - btc_weight,
            "fit_return_count": None,
            "residual_variance": None,
            "grid_count": None,
            "test_outcomes_used": False,
        }
    if not 0.0 <= btc_weight <= 1.0:
        raise ValueError("cross-asset residual hedge weight is invalid")
    eth_weight = 1.0 - btc_weight
    test_indices = development.indices_between(
        timestamps, split.test_start_ms, split.test_end_ms
    )
    if not len(test_indices):
        raise ValueError(f"cross-asset residual split {split.split_id} has no test rows")
    execution = policy["execution"]
    latency = int(execution["entry_latency_seconds"])
    horizons = [int(value) for value in execution["horizons_seconds"]]
    directions = list(execution["directions"])
    cost = base_cost_bps(policy, target_only=target_only)
    stress_increment = cost * (
        float(policy["costs"]["stress_cost_multiplier"]) - 1.0
    )
    base_edges: List[float] = []
    stress_edges: List[float] = []
    lifetimes: List[float] = []
    action_counts: Dict[str, int] = {}
    observable_decisions = 0
    next_allowed_ms = -1
    shift_ms = int(context_shift_seconds) * 1000
    for raw_index in np.asarray(test_indices, dtype=np.int64):
        row_index = int(raw_index)
        decision_timestamp = int(timestamps[row_index])
        if decision_timestamp < next_allowed_ms:
            continue
        entry_timestamp = decision_timestamp + latency * 1000
        entry_index = positions.get(entry_timestamp)
        context_entry_index = positions.get(entry_timestamp + shift_ms)
        if entry_index is None or (not target_only and context_entry_index is None):
            continue
        candidates: List[Tuple[float, float, str, int, int]] = []
        for horizon in horizons:
            exit_timestamp = entry_timestamp + horizon * 1000
            exit_index = positions.get(exit_timestamp)
            context_exit_index = positions.get(exit_timestamp + shift_ms)
            if exit_index is None or (not target_only and context_exit_index is None):
                continue
            for direction in directions:
                if direction == "long_residual":
                    target_return = _long_return_bps(
                        quotes["sol"]["ask"][entry_index],
                        quotes["sol"]["bid"][exit_index],
                    )
                    hedge_return = 0.0
                    if not target_only:
                        hedge_return = btc_weight * _short_return_bps(
                            quotes["btc"]["bid"][context_entry_index],
                            quotes["btc"]["ask"][context_exit_index],
                        ) + eth_weight * _short_return_bps(
                            quotes["eth"]["bid"][context_entry_index],
                            quotes["eth"]["ask"][context_exit_index],
                        )
                elif direction == "short_residual":
                    target_return = _short_return_bps(
                        quotes["sol"]["bid"][entry_index],
                        quotes["sol"]["ask"][exit_index],
                    )
                    hedge_return = 0.0
                    if not target_only:
                        hedge_return = btc_weight * _long_return_bps(
                            quotes["btc"]["ask"][context_entry_index],
                            quotes["btc"]["bid"][context_exit_index],
                        ) + eth_weight * _long_return_bps(
                            quotes["eth"]["ask"][context_entry_index],
                            quotes["eth"]["bid"][context_exit_index],
                        )
                else:
                    raise ValueError("cross-asset residual direction is invalid")
                base_edge = target_return + hedge_return - cost
                candidates.append(
                    (
                        base_edge - stress_increment,
                        base_edge,
                        direction,
                        horizon,
                        exit_timestamp,
                    )
                )
        if not candidates:
            continue
        observable_decisions += 1
        eligible = [candidate for candidate in candidates if candidate[0] > 0.0]
        if not eligible:
            continue
        stress_edge, base_edge, direction, horizon, settlement_timestamp = max(
            eligible, key=lambda item: (item[0], -item[3], item[2])
        )
        base_edges.append(float(base_edge))
        stress_edges.append(float(stress_edge))
        lifetimes.append(float(horizon))
        action_key = f"{direction}_{horizon}s"
        action_counts[action_key] = action_counts.get(action_key, 0) + 1
        next_allowed_ms = int(settlement_timestamp)
    return {
        "base_cost": development.summarize_edges(base_edges),
        "stress_cost": development.summarize_edges(stress_edges),
        "base_edges_bps": base_edges,
        "stress_edges_bps": stress_edges,
        "observable_decision_count": observable_decisions,
        "trade_count": len(base_edges),
        "action_counts": action_counts,
        "mean_position_lifetime_seconds": (
            float(np.mean(lifetimes)) if lifetimes else None
        ),
        "hedge_weight": weight_audit,
        "target_only": bool(target_only),
        "context_shift_seconds": int(context_shift_seconds),
        "base_explicit_cost_bps": cost,
        "stress_explicit_cost_bps": cost + stress_increment,
    }


def build_oracle(
    *,
    series: Mapping[str, np.ndarray],
    quotes: Mapping[str, Mapping[str, np.ndarray]],
    splits: Sequence[development.TimeSplit],
    policy: Mapping[str, Any],
    fixed_btc_weight: float | None = None,
    target_only: bool = False,
    context_shift_seconds: int = 0,
) -> Dict[str, Any]:
    reports: List[Dict[str, Any]] = []
    base_means: List[float] = []
    stress_means: List[float] = []
    trade_count = 0
    for split in splits:
        report = evaluate_split_oracle(
            series=series,
            quotes=quotes,
            split=split,
            policy=policy,
            fixed_btc_weight=fixed_btc_weight,
            target_only=target_only,
            context_shift_seconds=context_shift_seconds,
        )
        base_mean = report["base_cost"].get("mean_bps")
        stress_mean = report["stress_cost"].get("mean_bps")
        base_means.append(float(base_mean) if base_mean is not None else 0.0)
        stress_means.append(float(stress_mean) if stress_mean is not None else 0.0)
        trade_count += int(report["trade_count"])
        report.pop("base_edges_bps", None)
        report.pop("stress_edges_bps", None)
        reports.append({"split_id": int(split.split_id), **report})
    base_summary = development.summarize_edges(base_means)
    stress_summary = development.summarize_edges(stress_means)
    positive_ratio = sum(value > 0.0 for value in stress_means) / len(stress_means)
    gates = policy["decision_gates"]
    opportunity_proven = bool(
        len(reports) == int(policy["splits"]["count"])
        and trade_count >= int(gates["minimum_oos_trades"])
        and positive_ratio >= float(gates["minimum_positive_split_ratio"])
        and stress_summary.get("lcb_bps") is not None
        and float(stress_summary["lcb_bps"])
        > float(gates["minimum_oracle_stress_lcb_bps"])
    )
    return {
        "method": "fit_only_weight_six_split_non_overlapping_residual_hindsight_upper_bound_v1",
        "fully_verifiable": len(reports) == int(policy["splits"]["count"]),
        "opportunity_proven": opportunity_proven,
        "trade_count": trade_count,
        "positive_stress_split_ratio": positive_ratio,
        "base_cost_by_split": base_summary,
        "stress_cost_by_split": stress_summary,
        "split_reports": reports,
        "promotion_evidence": False,
    }


def create_frozen_audit_manifest(
    *,
    series: Mapping[str, np.ndarray],
    policy: Mapping[str, Any],
    parent_manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    primary = maker._splits_from_manifest(parent_manifest["primary_splits"])
    boundary = {
        str(int(offset)): maker._splits_from_manifest(
            parent_manifest["boundary_splits"][str(int(offset))]
        )
        for offset in policy["stability_audit"]["boundary_offsets_seconds"]
    }
    frozen_start_ms = min(
        split.fit_start_ms for values in boundary.values() for split in values
    )
    primary_end_ms = max(split.test_end_ms for split in primary)
    outcome_tail_seconds = int(policy["execution"]["entry_latency_seconds"]) + max(
        int(value) for value in policy["execution"]["horizons_seconds"]
    )
    frozen_end_ms = primary_end_ms + outcome_tail_seconds * 1000
    if int(timestamps[-1]) < frozen_end_ms - 1000:
        raise development.CaptureNotReady(
            "cross-asset residual inherited split outcome tail is incomplete"
        )
    last_primary = primary[-1]
    forward_fit = development.indices_between(
        timestamps, last_primary.fit_start_ms, last_primary.fit_end_ms
    )
    forward_weight, forward_weight_audit = estimate_fit_only_btc_weight(
        series, forward_fit, policy
    )
    forward_start_ms = int(timestamps[-1]) + 1000
    stability = policy["stability_audit"]
    forward_end_ms = forward_start_ms + int(
        stability["independent_forward_window_seconds"]
    ) * 1000
    parent_frozen = parent_manifest["frozen_domain"]
    manifest: Dict[str, Any] = {
        "schema_version": AUDIT_MANIFEST_SCHEMA_VERSION,
        "created_at_utc": dt.datetime.now(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "policy_identity_sha256": FROZEN_POLICY_IDENTITY_SHA256,
        "experiment_id": policy["experiment_id"],
        "split_calendar_source": "maker_v2_absolute_primary_and_boundary_splits",
        "parent_audit_identity_sha256": parent_manifest["identity_sha256"],
        "parent_target_domain_identity_sha256": parent_frozen["identity_sha256"],
        "frozen_domain": series_domain_identity(
            series, start_ms=frozen_start_ms, end_ms=frozen_end_ms
        ),
        "primary_splits": [
            vars(split) if hasattr(split, "__dict__") else split for split in primary
        ],
        "boundary_splits": {
            key: [vars(split) if hasattr(split, "__dict__") else split for split in values]
            for key, values in boundary.items()
        },
        "independent_forward": {
            "start_ms": forward_start_ms,
            "end_ms": forward_end_ms,
            "observation_end_ms": forward_end_ms + outcome_tail_seconds * 1000,
            "block_seconds": int(stability["forward_block_seconds"]),
            "block_count": int(stability["minimum_forward_blocks"]),
            "observed_before_freeze": False,
            "btc_weight": forward_weight,
            "eth_weight": 1.0 - forward_weight,
            "weight_source": "last_primary_split_fit_window",
            "weight_audit": forward_weight_audit,
        },
    }
    # vars() returns the exact seven integer TimeSplit fields and is stable under
    # canonical JSON hashing.  Copy into plain dicts to avoid accidental aliases.
    manifest["primary_splits"] = [dict(value) for value in manifest["primary_splits"]]
    manifest["boundary_splits"] = {
        key: [dict(value) for value in values]
        for key, values in manifest["boundary_splits"].items()
    }
    manifest["identity_sha256"] = common.canonical_sha256(manifest)
    return manifest


def load_or_create_frozen_audit_manifest(
    path: pathlib.Path,
    *,
    series: Mapping[str, np.ndarray],
    policy: Mapping[str, Any],
    parent_manifest_path: pathlib.Path,
) -> Tuple[Dict[str, Any], bool]:
    parent = maker.load_baseline_audit_manifest(parent_manifest_path, policy=policy)
    parent_frozen = parent.get("frozen_domain")
    if not isinstance(parent_frozen, Mapping):
        raise ValueError("parent maker frozen target domain is missing")
    actual_parent = maker._series_domain_identity(
        series,
        start_ms=int(parent_frozen["start_ms"]),
        end_ms=int(parent_frozen["end_ms"]),
    )
    if actual_parent != parent_frozen:
        raise ValueError("parent maker target frozen domain drift")
    created = False
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = create_frozen_audit_manifest(
            series=series, policy=policy, parent_manifest=parent
        )
        common.atomic_write_json(path, manifest)
        created = True
    manifest = common.read_json(path)
    unsigned = {key: value for key, value in manifest.items() if key != "identity_sha256"}
    if not (
        manifest.get("schema_version") == AUDIT_MANIFEST_SCHEMA_VERSION
        and manifest.get("policy_identity_sha256") == FROZEN_POLICY_IDENTITY_SHA256
        and manifest.get("experiment_id") == policy["experiment_id"]
        and manifest.get("identity_sha256") == common.canonical_sha256(unsigned)
        and manifest.get("parent_audit_identity_sha256") == parent["identity_sha256"]
        and manifest.get("parent_target_domain_identity_sha256")
        == parent_frozen["identity_sha256"]
        and manifest.get("primary_splits") == parent.get("primary_splits")
        and manifest.get("boundary_splits") == parent.get("boundary_splits")
    ):
        raise ValueError("cross-asset residual frozen manifest mismatch")
    frozen = manifest.get("frozen_domain")
    if not isinstance(frozen, Mapping):
        raise ValueError("cross-asset residual frozen domain is missing")
    actual = series_domain_identity(
        series,
        start_ms=int(frozen["start_ms"]),
        end_ms=int(frozen["end_ms"]),
    )
    if actual != frozen:
        raise ValueError("cross-asset residual frozen domain drift")
    forward = manifest.get("independent_forward")
    if not (
        isinstance(forward, Mapping)
        and 0.0 <= float(forward.get("btc_weight", -1.0)) <= 1.0
        and math.isclose(
            float(forward.get("btc_weight", 0.0))
            + float(forward.get("eth_weight", 0.0)),
            1.0,
        )
    ):
        raise ValueError("cross-asset residual forward hedge weight is invalid")
    return manifest, created


def evaluate_stability_audit(
    *,
    manifest: Mapping[str, Any],
    manifest_created: bool,
    series: Mapping[str, np.ndarray],
    quotes: Mapping[str, Mapping[str, np.ndarray]],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    primary = build_oracle(
        series=series,
        quotes=quotes,
        splits=maker._splits_from_manifest(manifest["primary_splits"]),
        policy=policy,
    )
    boundary_reports: List[Dict[str, Any]] = []
    for offset in policy["stability_audit"]["boundary_offsets_seconds"]:
        oracle = build_oracle(
            series=series,
            quotes=quotes,
            splits=maker._splits_from_manifest(
                manifest["boundary_splits"][str(int(offset))]
            ),
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
    forward_indices = development.indices_between(
        timestamps,
        int(forward_contract["start_ms"]),
        int(forward_contract["end_ms"]),
    )
    expected_rows = int(stability["independent_forward_window_seconds"])
    row_ratio = len(forward_indices) / expected_rows if expected_rows else 0.0
    observation_complete = bool(
        int(timestamps[-1]) >= int(forward_contract["observation_end_ms"])
        and row_ratio >= float(stability["minimum_forward_row_ratio"])
    )
    forward_oracle: Dict[str, Any] | None = None
    forward_proven = False
    if observation_complete:
        forward_oracle = build_oracle(
            series=series,
            quotes=quotes,
            splits=maker._forward_splits(manifest),
            policy=policy,
            fixed_btc_weight=float(forward_contract["btc_weight"]),
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
    reasons: List[str] = []
    primary = audit.get("primary_oracle", {})
    boundary = audit.get("boundary_sensitivity", {})
    if not isinstance(primary, Mapping) or primary.get("opportunity_proven") is not True:
        reasons.append("frozen_primary_residual_opportunity_failed")
    if not isinstance(boundary, Mapping) or boundary.get("passed") is not True:
        reasons.append("residual_boundary_sensitivity_failed")
    if reasons:
        return DECISION_STOP, reasons
    if audit.get("state") != "COMPLETE":
        return DECISION_WAIT, ["independent_24h_residual_forward_window_incomplete"]
    if audit.get("stable_opportunity_proven") is True:
        return DECISION_CONTINUE, [
            "frozen_boundary_and_forward_residual_opportunity_gates_passed"
        ]
    return DECISION_STOP, ["independent_residual_forward_opportunity_failed"]


def build_diagnostic_controls(
    *,
    series: Mapping[str, np.ndarray],
    quotes: Mapping[str, Mapping[str, np.ndarray]],
    manifest: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    splits = maker._splits_from_manifest(manifest["primary_splits"])
    controls = policy["diagnostic_controls"]
    return {
        "target_only_all_taker": build_oracle(
            series=series,
            quotes=quotes,
            splits=splits,
            policy=policy,
            target_only=True,
        ),
        "time_shifted_hedge": build_oracle(
            series=series,
            quotes=quotes,
            splits=splits,
            policy=policy,
            context_shift_seconds=int(controls["hedge_time_shift_seconds"]),
        ),
        "controls_can_authorize_continuation": False,
    }


def run_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = pathlib.Path(args.config).resolve()
    assessment_path = pathlib.Path(args.control_assessment).resolve()
    parent_manifest_path = pathlib.Path(args.parent_audit_manifest).resolve()
    audit_manifest_path = pathlib.Path(args.audit_manifest).resolve()
    policy = validate_policy(config_path)
    assessment = development.validate_capture_assessment(assessment_path)
    series = development.load_capture_rows(assessment)
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    quotes = reconstruct_quotes(series)
    manifest, manifest_created = load_or_create_frozen_audit_manifest(
        audit_manifest_path,
        series=series,
        policy=policy,
        parent_manifest_path=parent_manifest_path,
    )
    stability = evaluate_stability_audit(
        manifest=manifest,
        manifest_created=manifest_created,
        series=series,
        quotes=quotes,
        policy=policy,
    )
    decision, reasons = decide_stability(stability)
    diagnostics = build_diagnostic_controls(
        series=series,
        quotes=quotes,
        manifest=manifest,
        policy=policy,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE",
        "fully_verifiable": stability["state"] == "COMPLETE",
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
            "parent_audit_manifest_path": str(parent_manifest_path),
            "parent_audit_manifest_sha256": common.sha256_file(parent_manifest_path),
            "frozen_audit_manifest_path": str(audit_manifest_path),
            "frozen_audit_manifest_sha256": common.sha256_file(audit_manifest_path),
            "frozen_audit_manifest_identity_sha256": manifest["identity_sha256"],
            "parent_target_domain_identity_verified": True,
        },
        "execution_contract": {
            "mechanism": dict(policy["mechanism"]),
            "execution": dict(policy["execution"]),
            "costs": dict(policy["costs"]),
            "base_explicit_cost_bps": base_cost_bps(policy),
            "stress_explicit_cost_bps": base_cost_bps(policy)
            * float(policy["costs"]["stress_cost_multiplier"]),
        },
        "common_domain": {
            "row_count": int(len(timestamps)),
            "first_timestamp_ms": int(timestamps[0]),
            "last_timestamp_ms": int(timestamps[-1]),
            "timestamp_sha256": common.array_sha256(timestamps),
            "splits": list(manifest["primary_splits"]),
        },
        "hindsight_oracle": stability["primary_oracle"],
        "diagnostic_controls": diagnostics,
        "stability_audit": stability,
        "research_decision": decision,
        "reason_codes": reasons,
        "next_action": {
            DECISION_CONTINUE: (
                "preregister_cross_asset_residual_learnability_experiment"
            ),
            DECISION_STOP: "close_cross_asset_residual_family_and_change_mechanism",
            DECISION_WAIT: "collect_unseen_24h_residual_forward_without_changes",
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
        "next_action": "complete_input_or_fix_frozen_identity",
        "experiment_policy": {"path": str(pathlib.Path(args.config).resolve())},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-assessment", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--parent-audit-manifest", required=True)
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
