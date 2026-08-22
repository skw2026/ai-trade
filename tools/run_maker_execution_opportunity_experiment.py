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
    "583feeed9fc4ce9810ca824546bb551b02723e3544f1020d621f0800731cac6a"
)
DECISION_CONTINUE = "CONTINUE_TO_MAKER_LEARNABILITY_EXPERIMENT"
DECISION_STOP = "STOP_MAKER_EXECUTION_FAMILY"


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
        and actions.get("fill_timeout_seconds") == 5
    ):
        failures.append("actions")
    fill_proxy = policy.get("fill_proxy")
    if not (
        isinstance(fill_proxy, Mapping)
        and fill_proxy.get("method")
        == "opposite_aggressor_quote_volume_and_top_of_book_trade_through_v1"
        and float(fill_proxy.get("queue_depth_multiplier", 0.0)) == 1.25
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
    gates = policy.get("decision_gates")
    if not (
        isinstance(gates, Mapping)
        and gates.get("minimum_oos_trades") == 30
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


def build_maker_action_returns(
    series: Mapping[str, np.ndarray],
    *,
    horizons_seconds: Sequence[int],
    placement_latency_seconds: int,
    fill_timeout_seconds: int,
    queue_depth_multiplier: float,
    base_cost_bps: float,
) -> Tuple[np.ndarray, np.ndarray, List[Dict[str, Any]], Dict[str, Any]]:
    """Build base-net outcomes only for conservatively inferred full fills."""

    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    best_bid = np.asarray(series["best_bid"], dtype=np.float64)
    best_ask = np.asarray(series["best_ask"], dtype=np.float64)
    best_bid_size = np.asarray(series["best_bid_size"], dtype=np.float64)
    best_ask_size = np.asarray(series["best_ask_size"], dtype=np.float64)
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
        and np.all(buy_quote_volume >= 0.0)
        and np.all(sell_quote_volume >= 0.0)
    ):
        raise ValueError("maker opportunity market inputs are invalid")
    latency = int(placement_latency_seconds)
    timeout = int(fill_timeout_seconds)
    queue_multiplier = float(queue_depth_multiplier)
    cost = float(base_cost_bps)
    if latency <= 0 or timeout <= 0 or queue_multiplier < 1.0 or cost <= 0.0:
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
        placement_timestamp = int(decision_timestamp) + latency * 1000
        placement_index = positions.get(placement_timestamp)
        if placement_index is None:
            continue
        direction_fills: Dict[str, Tuple[int, float]] = {}
        for direction in ("long", "short"):
            if direction == "long":
                posted_price = float(best_bid[placement_index])
                queue_quote = (
                    posted_price
                    * float(best_bid_size[placement_index])
                    * queue_multiplier
                )
            else:
                posted_price = float(best_ask[placement_index])
                queue_quote = (
                    posted_price
                    * float(best_ask_size[placement_index])
                    * queue_multiplier
                )
            cumulative_opposite_quote = 0.0
            for offset in range(1, timeout + 1):
                probe_timestamp = placement_timestamp + offset * 1000
                probe_index = positions.get(probe_timestamp)
                if probe_index is None:
                    break
                if direction == "long":
                    cumulative_opposite_quote += float(sell_quote_volume[probe_index])
                    traded_through = float(best_bid[probe_index]) < posted_price
                else:
                    cumulative_opposite_quote += float(buy_quote_volume[probe_index])
                    traded_through = float(best_ask[probe_index]) > posted_price
                if traded_through and cumulative_opposite_quote >= queue_quote:
                    direction_fills[direction] = (probe_index, posted_price)
                    decision_fill_directions += 1
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
    )
    embargo = (
        int(actions_policy["placement_latency_seconds"])
        + int(actions_policy["fill_timeout_seconds"])
        + max(int(value) for value in actions_policy["horizons_seconds"])
    )
    split_policy = policy["splits"]
    splits = development.build_time_splits(
        timestamps,
        n_splits=int(split_policy["count"]),
        train_window_seconds=int(split_policy["train_window_seconds"]),
        validation_window_seconds=int(split_policy["validation_window_seconds"]),
        test_window_seconds=int(split_policy["test_window_seconds"]),
        rolling_step_seconds=int(split_policy["rolling_step_seconds"]),
        embargo_seconds=embargo,
    )
    oracle = build_oracle(
        timestamps=timestamps,
        outcomes=outcomes,
        fill_timestamps=fill_timestamps,
        actions=actions,
        splits=splits,
        policy=policy,
    )
    decision, reasons = decide(oracle, policy)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE",
        "fully_verifiable": True,
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
            "splits": [dataclasses.asdict(split) for split in splits],
        },
        "fill_audit": fill_audit,
        "hindsight_oracle": oracle,
        "research_decision": decision,
        "reason_codes": reasons,
        "next_action": {
            DECISION_CONTINUE: "preregister_fill_aware_maker_learnability_experiment",
            DECISION_STOP: "close_maker_execution_family_and_change_horizon_or_payoff",
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
