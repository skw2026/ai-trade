#!/usr/bin/env python3
"""Aggregate every complete v4 option lifecycle under one frozen economic gate."""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import pathlib
import random
import re
import statistics
import tempfile
import time
from typing import Any, Dict, Mapping, Sequence

import audit_option_lifecycle_payoff_v4 as payoff
import audit_option_lifecycle_v4 as lifecycle_audit
import capture_bybit_option_lifecycle_v4 as capture


SCHEMA_VERSION = "option_lifecycle_economic_audit_v1"
POLICY_SCHEMA_VERSION = "option_lifecycle_economic_policy_v1"
MANIFEST_SCHEMA_VERSION = "option_lifecycle_economic_manifest_v1"
FROZEN_POLICY_CANONICAL_SHA256 = "fbc9851193c8a34e587b81b647f3dea19c44006509b80ee2b8b9c3f39b5c30fa"
FROZEN_MANIFEST_CANONICAL_SHA256 = "d2dc77a9aa24b89337114c6d1a924e30933d7b134182a90949695ef95578d21c"
POLICY_RELATIVE_PATH = "config/option_lifecycle_economic_v1.json"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def _atomic_write(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        pathlib.Path(temporary_name).replace(path)
    finally:
        pathlib.Path(temporary_name).unlink(missing_ok=True)


def load_contract(
    policy_path: pathlib.Path,
    manifest_path: pathlib.Path,
    payoff_policy_path: pathlib.Path,
    payoff_manifest_path: pathlib.Path,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    policy = capture.read_json(policy_path)
    manifest = capture.read_json(manifest_path)
    payoff_policy, payoff_manifest = payoff.load_contract(
        payoff_policy_path, payoff_manifest_path
    )
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("economic policy schema mismatch")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("economic manifest schema mismatch")
    policy_sha = capture.canonical_sha256(policy)
    manifest_sha = capture.canonical_sha256(manifest)
    if policy_sha != FROZEN_POLICY_CANONICAL_SHA256:
        raise ValueError("economic frozen policy identity mismatch")
    if manifest_sha != FROZEN_MANIFEST_CANONICAL_SHA256:
        raise ValueError("economic frozen manifest identity mismatch")
    if manifest.get("policy_canonical_sha256") != policy_sha:
        raise ValueError("economic manifest policy identity mismatch")
    if (
        manifest.get("experiment_id") != policy.get("experiment_id")
        or manifest.get("policy_path") != POLICY_RELATIVE_PATH
    ):
        raise ValueError("economic experiment identity mismatch")
    source = policy.get("source_contract", {})
    expected_source = {
        "capture_experiment_id": "btc_bybit_usdt_option_lifecycle_capture_v4",
        "capture_policy_canonical_sha256": capture.FROZEN_POLICY_CANONICAL_SHA256,
        "capture_manifest_canonical_sha256": capture.FROZEN_MANIFEST_CANONICAL_SHA256,
        "payoff_experiment_id": payoff_policy["experiment_id"],
        "payoff_policy_canonical_sha256": payoff.FROZEN_POLICY_CANONICAL_SHA256,
        "payoff_manifest_canonical_sha256": payoff.FROZEN_MANIFEST_CANONICAL_SHA256,
    }
    if any(source.get(key) != value for key, value in expected_source.items()):
        raise ValueError("economic source contract mismatch")
    action_ids = [str(row.get("action_id") or "") for row in payoff_policy["actions"]]
    if source.get("action_ids") != action_ids:
        raise ValueError("economic action identity mismatch")
    aggregate = policy.get("aggregation_contract", {})
    if int(aggregate.get("minimum_complete_lifecycles") or 0) < 6:
        raise ValueError("economic independent lifecycle minimum is too small")
    if aggregate.get("snapshot_rows_are_independent_samples") is not False:
        raise ValueError("economic snapshot independence is forbidden")
    if int(aggregate.get("bootstrap_samples") or 0) < 1000:
        raise ValueError("economic bootstrap sample count is too small")
    confidence = float(aggregate.get("confidence_level") or 0.0)
    if not 0.5 < confidence < 1.0:
        raise ValueError("economic confidence level is invalid")
    if (
        int(manifest.get("source_capture_observation_start_epoch_ms") or 0)
        != int(payoff_manifest["capture_observation_start_epoch_ms"])
        or int(manifest.get("source_payoff_freeze_epoch_ms") or 0)
        != int(payoff_manifest["payoff_freeze_epoch_ms"])
        or int(manifest.get("aggregation_freeze_epoch_ms") or 0)
        <= int(payoff_manifest["payoff_freeze_epoch_ms"])
    ):
        raise ValueError("economic freeze boundary mismatch")
    if (
        policy.get("research_domain") != "development_only"
        or manifest.get("research_domain") != "development_only"
    ):
        raise ValueError("economic contract is outside development")
    for payload in (policy.get("authorities", {}), manifest):
        if any(
            payload.get(field) is not False
            for field in (
                "promotion_authority",
                "demo_activation_authorized",
                "live_activation_authorized",
            )
        ):
            raise ValueError("economic contract cannot grant activation authority")
    claims = policy.get("claim_contract", {})
    if (
        claims.get("single_or_subminimum_lifecycle_profitability_claim_allowed")
        is not False
        or claims.get("sharpe_claim_allowed") is not False
        or claims.get("drawdown_claim_allowed") is not False
    ):
        raise ValueError("economic contract overclaims statistical evidence")
    return policy, manifest, payoff_policy


def _paired_delivery(
    snapshots: Sequence[Mapping[str, Any]], lifecycle: Mapping[str, Any]
) -> tuple[bool, float | None]:
    valid = False
    price: float | None = None
    symbols = set(map(str, lifecycle["symbols"]))
    for snapshot in snapshots:
        rows = snapshot["delivery_prices"]
        if len(rows) != 2 or {str(row["symbol"]) for row in rows} != symbols:
            continue
        prices = {
            float(row["deliveryPrice"])
            for row in rows
            if math.isfinite(float(row["deliveryPrice"]))
            and float(row["deliveryPrice"]) > 0.0
        }
        if len(prices) == 1:
            valid, price = True, next(iter(prices))
    return valid, price


def evaluate_lifecycle(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    replay: Mapping[str, Any],
    capture_policy: Mapping[str, Any],
    payoff_policy: Mapping[str, Any],
    generated_at_epoch_ms: int,
) -> Dict[str, Any]:
    ordered = sorted(snapshots, key=lambda row: int(row["timestamp_epoch_ms"]))
    lifecycle = ordered[0]["active_lifecycle"]
    lifecycle_id = str(lifecycle["lifecycle_id"])
    selected_at = int(lifecycle["selected_epoch_ms"])
    delivery_time = int(lifecycle["delivery_time_epoch_ms"])
    reasons: collections.Counter[str] = collections.Counter()
    qualified: list[int] = []
    segment_ids: set[str] = set()
    entry_valid = False
    deterministic = lifecycle_audit._selection_is_deterministic(
        ordered[0], capture_policy
    )
    if not deterministic:
        reasons["NONDETERMINISTIC_DISCOVERY_SELECTION"] += 1
    for index, snapshot in enumerate(ordered):
        timestamp = int(snapshot["timestamp_epoch_ms"])
        segment_ids.add(replay["snapshot_segment"][str(timestamp)])
        tracked_reason = lifecycle_audit._tracked_reason(
            snapshot, entry=index == 0, policy=capture_policy
        )
        hedge_reason = lifecycle_audit._hedge_reason(
            snapshot, policy=capture_policy
        )
        if index == 0:
            entry_valid = (
                timestamp == selected_at
                and deterministic
                and tracked_reason is None
                and hedge_reason is None
            )
        if timestamp <= delivery_time:
            if tracked_reason:
                reasons[tracked_reason] += 1
            if hedge_reason:
                reasons[hedge_reason] += 1
            if tracked_reason is None and hedge_reason is None:
                qualified.append(timestamp)
    maximum_gap_ms = max(
        (right - left for left, right in zip(qualified, qualified[1:])),
        default=0,
    )
    terminal_gap_ms = delivery_time - qualified[-1] if qualified else None
    delivery_valid, delivery_price = _paired_delivery(ordered, lifecycle)
    coverage_valid = bool(
        entry_valid
        and len(segment_ids) >= 2
        and len(qualified) >= 2
        and maximum_gap_ms
        <= int(capture_policy["lifecycle_gate"]["maximum_snapshot_gap_seconds"])
        * 1000
        and not reasons
    )
    complete = bool(
        delivery_valid
        and coverage_valid
        and terminal_gap_ms is not None
        and 0 <= terminal_gap_ms
        <= int(capture_policy["lifecycle_gate"]["maximum_terminal_gap_seconds"])
        * 1000
        and generated_at_epoch_ms >= delivery_time
    )
    state = "pending"
    if delivery_valid or generated_at_epoch_ms > delivery_time + 180000:
        state = "complete" if complete else "insufficient"
    result: Dict[str, Any] = {
        "lifecycle_id": lifecycle_id,
        "selected_epoch_ms": selected_at,
        "delivery_time_epoch_ms": delivery_time,
        "snapshot_count": len(ordered),
        "qualified_snapshot_count": len(qualified),
        "distinct_segment_count": len(segment_ids),
        "maximum_internal_gap_seconds": maximum_gap_ms / 1000.0,
        "terminal_gap_seconds": (
            terminal_gap_ms / 1000.0 if terminal_gap_ms is not None else None
        ),
        "paired_delivery_evidence_valid": delivery_valid,
        "state": state,
        "reason_counts": dict(sorted(reasons.items())),
        "actions": [],
    }
    if not complete:
        return result
    timeline = [
        row
        for row in ordered
        if selected_at <= int(row["timestamp_epoch_ms"]) <= delivery_time
    ]
    try:
        actions = [
            payoff._action_result(
                action=action,
                entry=ordered[0],
                timeline=timeline,
                delivery_price=float(delivery_price),
                policy=payoff_policy,
            )
            for action in payoff_policy["actions"]
        ]
    except (IndexError, KeyError, TypeError, ValueError):
        result["state"] = "insufficient"
        result["reason_counts"] = {"PAYOFF_RECONSTRUCTION_FAILURE": 1}
        return result
    by_id = {row["action_id"]: row for row in actions}
    if (
        abs(
            float(by_id["short_selected_straddle"]["gross_pnl_usdt"])
            + float(by_id["long_selected_straddle"]["gross_pnl_usdt"])
        )
        > 1e-8
        or any(
            abs(float(by_id["no_trade"].get(field) or 0.0)) > 1e-12
            for field in ("gross_pnl_usdt", "base_net_pnl_usdt", "stress_net_pnl_usdt")
        )
    ):
        result["state"] = "insufficient"
        result["reason_counts"] = {"PAYOFF_SIGN_OR_NO_TRADE_CONTROL_FAILURE": 1}
        return result
    result["actions"] = actions
    return result


def _bootstrap_interval(
    values: Sequence[float], *, samples: int, confidence: float, seed: int
) -> tuple[float, float]:
    if not values:
        raise ValueError("bootstrap values are empty")
    rng = random.Random(seed)
    count = len(values)
    means = sorted(
        sum(values[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(samples)
    )
    tail = (1.0 - confidence) / 2.0
    lower_index = max(0, min(samples - 1, math.floor(tail * (samples - 1))))
    upper_index = max(
        0, min(samples - 1, math.ceil((1.0 - tail) * (samples - 1)))
    )
    return means[lower_index], means[upper_index]


def _maximum_drawdown(values: Sequence[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def aggregate_actions(
    completed: Sequence[Mapping[str, Any]], contract: Mapping[str, Any]
) -> Dict[str, Any]:
    by_action: Dict[str, list[Mapping[str, Any]]] = collections.defaultdict(list)
    for lifecycle in completed:
        for action in lifecycle["actions"]:
            by_action[str(action["action_id"])].append(action)
    result: Dict[str, Any] = {}
    samples = int(contract["bootstrap_samples"])
    confidence = float(contract["confidence_level"])
    seed = int(contract["bootstrap_seed"])
    threshold = float(contract["positive_threshold_bps"])
    for action_index, (action_id, rows) in enumerate(sorted(by_action.items())):
        base = [float(row["base_net_bps"]) for row in rows]
        stress = [float(row["stress_net_bps"]) for row in rows]
        base_lcb, base_ucb = _bootstrap_interval(
            base, samples=samples, confidence=confidence, seed=seed + action_index * 2
        )
        stress_lcb, stress_ucb = _bootstrap_interval(
            stress,
            samples=samples,
            confidence=confidence,
            seed=seed + action_index * 2 + 1,
        )
        result[action_id] = {
            "lifecycle_count": len(rows),
            "base_mean_bps": statistics.fmean(base),
            "base_median_bps": statistics.median(base),
            "base_positive_ratio": sum(value > threshold for value in base) / len(base),
            "base_mean_lcb_bps": base_lcb,
            "base_mean_ucb_bps": base_ucb,
            "base_max_drawdown_bps": _maximum_drawdown(base),
            "stress_mean_bps": statistics.fmean(stress),
            "stress_median_bps": statistics.median(stress),
            "stress_positive_ratio": sum(value > threshold for value in stress) / len(stress),
            "stress_mean_lcb_bps": stress_lcb,
            "stress_mean_ucb_bps": stress_ucb,
            "stress_max_drawdown_bps": _maximum_drawdown(stress),
            "minimum_stress_net_bps": min(stress),
            "maximum_stress_net_bps": max(stress),
        }
    return result


def audit(
    *,
    root: pathlib.Path,
    capture_policy_path: pathlib.Path,
    capture_manifest_path: pathlib.Path,
    payoff_policy_path: pathlib.Path,
    payoff_manifest_path: pathlib.Path,
    economic_policy_path: pathlib.Path,
    economic_manifest_path: pathlib.Path,
    executed_release_sha: str | None = None,
    generated_at_epoch_ms: int | None = None,
) -> Dict[str, Any]:
    if executed_release_sha is not None and not SHA_PATTERN.fullmatch(executed_release_sha):
        raise ValueError("economic executed release SHA is invalid")
    policy, manifest, payoff_policy = load_contract(
        economic_policy_path,
        economic_manifest_path,
        payoff_policy_path,
        payoff_manifest_path,
    )
    capture_policy, capture_manifest = capture.load_contract(
        capture_policy_path, capture_manifest_path
    )
    generated = int(generated_at_epoch_ms or time.time() * 1000)
    replay = lifecycle_audit.replay_capture_root(
        root, policy=capture_policy, manifest=capture_manifest
    )
    common = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_epoch_ms": generated,
        "identities": {
            "experiment_id": policy["experiment_id"],
            "policy_canonical_sha256": capture.canonical_sha256(policy),
            "manifest_canonical_sha256": capture.canonical_sha256(manifest),
            "archive_input_set_sha256": capture.canonical_sha256(
                replay["input_identities"]
            ),
            "executed_release_sha": executed_release_sha,
        },
        "archive_integrity": {
            "root_present": replay["root_present"],
            "valid_segment_count": replay["valid_segment_count"],
            "invalid_segment_count": replay["invalid_segment_count"],
            "checksum_bound_snapshot_count": replay["eligible_snapshot_count"],
        },
        "profitability_claim_allowed": False,
        "sharpe_claim_allowed": False,
        "drawdown_claim_allowed": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }
    if replay["invalid_segment_count"]:
        return {
            **common,
            "progress": {},
            "lifecycles": [],
            "action_aggregates": {},
            "economic_evidence": False,
            "demo_review_eligible": False,
            "decision": policy["decision_contract"]["invalid_decision"],
            "reason_code": "ARCHIVE_INTEGRITY_FAILURE",
        }
    grouped: Dict[str, list[Mapping[str, Any]]] = collections.defaultdict(list)
    for snapshot in replay["snapshots"]:
        lifecycle = snapshot.get("active_lifecycle")
        if isinstance(lifecycle, Mapping):
            grouped[str(lifecycle["lifecycle_id"])].append(snapshot)
    lifecycles = [
        evaluate_lifecycle(
            snapshots,
            replay=replay,
            capture_policy=capture_policy,
            payoff_policy=payoff_policy,
            generated_at_epoch_ms=generated,
        )
        for snapshots in grouped.values()
    ]
    lifecycles.sort(key=lambda row: int(row["selected_epoch_ms"]))
    completed = [row for row in lifecycles if row["state"] == "complete"]
    pending = [row for row in lifecycles if row["state"] == "pending"]
    insufficient = [row for row in lifecycles if row["state"] == "insufficient"]
    minimum = int(policy["aggregation_contract"]["minimum_complete_lifecycles"])
    progress = {
        "observed_lifecycle_count": len(lifecycles),
        "complete_lifecycle_count": len(completed),
        "pending_lifecycle_count": len(pending),
        "insufficient_lifecycle_count": len(insufficient),
        "minimum_complete_lifecycles": minimum,
        "completion_ratio": min(1.0, len(completed) / minimum),
    }
    aggregates = (
        aggregate_actions(completed, policy["aggregation_contract"])
        if completed
        else {}
    )
    decisions = policy["decision_contract"]
    economic_evidence = len(completed) >= minimum
    demo_review = False
    if insufficient:
        decision = decisions["insufficient_decision"]
        reason_code = "ONE_OR_MORE_LIFECYCLES_HAVE_INSUFFICIENT_COVERAGE"
    elif len(completed) < minimum:
        decision = decisions["wait_decision"]
        reason_code = "MINIMUM_INDEPENDENT_LIFECYCLES_NOT_REACHED"
    else:
        gate = policy["aggregation_contract"]
        primary = aggregates[policy["source_contract"]["primary_action_id"]]
        pass_checks = {
            "base_mean": primary["base_mean_bps"]
            > float(gate["pass_require_base_mean_bps_above"]),
            "stress_mean": primary["stress_mean_bps"]
            > float(gate["pass_require_stress_mean_bps_above"]),
            "stress_median": primary["stress_median_bps"]
            > float(gate["pass_require_stress_median_bps_above"]),
            "stress_positive_ratio": primary["stress_positive_ratio"]
            >= float(gate["pass_minimum_stress_positive_ratio"]),
            "stress_mean_lcb": primary["stress_mean_lcb_bps"]
            > float(gate["pass_require_stress_mean_lcb_bps_above"]),
        }
        if all(pass_checks.values()):
            decision = decisions["pass_decision"]
            reason_code = "FROZEN_STRESS_ECONOMIC_GATES_PASS"
            demo_review = True
        elif primary["stress_mean_ucb_bps"] <= float(
            gate["futility_stress_mean_ucb_bps_at_or_below"]
        ):
            decision = decisions["stop_decision"]
            reason_code = "STRESS_EDGE_FUTILITY_BOUND_NONPOSITIVE"
        else:
            decision = decisions["continue_decision"]
            reason_code = "ECONOMIC_RESULT_INCONCLUSIVE_CONTINUE_FROZEN_CAPTURE"
    return {
        **common,
        "progress": progress,
        "lifecycles": lifecycles,
        "action_aggregates": aggregates,
        "economic_evidence": economic_evidence,
        "demo_review_eligible": demo_review,
        "decision": decision,
        "reason_code": reason_code,
        "limitations": [
            "Lifecycle is the independent unit; snapshots are not independent samples.",
            "A pass permits Demo review only and never activates an account.",
            "Sharpe and drawdown claims require a longer independent chronological return series.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--capture-policy", type=pathlib.Path, required=True)
    parser.add_argument("--capture-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--payoff-policy", type=pathlib.Path, required=True)
    parser.add_argument("--payoff-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--economic-policy", type=pathlib.Path, required=True)
    parser.add_argument("--economic-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--executed-release-sha")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(
            root=args.root,
            capture_policy_path=args.capture_policy,
            capture_manifest_path=args.capture_manifest,
            payoff_policy_path=args.payoff_policy,
            payoff_manifest_path=args.payoff_manifest,
            economic_policy_path=args.economic_policy,
            economic_manifest_path=args.economic_manifest,
            executed_release_sha=args.executed_release_sha,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "identities": {"executed_release_sha": args.executed_release_sha},
            "archive_integrity": {"invalid_segment_count": 1},
            "progress": {},
            "lifecycles": [],
            "action_aggregates": {},
            "economic_evidence": False,
            "profitability_claim_allowed": False,
            "sharpe_claim_allowed": False,
            "drawdown_claim_allowed": False,
            "demo_review_eligible": False,
            "promotion_authority": False,
            "demo_activation_authorized": False,
            "live_activation_authorized": False,
            "decision": "INVALID_MULTI_LIFECYCLE_ECONOMIC_ARCHIVE",
            "reason_code": str(exc),
        }
    _atomic_write(args.output, report)
    return 2 if report["decision"] in {
        "INVALID_MULTI_LIFECYCLE_ECONOMIC_ARCHIVE",
        "INSUFFICIENT_MULTI_LIFECYCLE_COVERAGE",
    } else 0


if __name__ == "__main__":
    raise SystemExit(main())
