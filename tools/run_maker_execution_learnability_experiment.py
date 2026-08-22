#!/usr/bin/env python3
"""Compare frozen decision-time model architectures under conservative maker fills.

The experiment is development-only.  Future queue consumption and trade-through
are labels/outcomes, never features.  A signal occupies the strategy until its
order times out or until the inferred fill plus holding horizon has elapsed.
Passing this screen can only preregister an independent forward validation.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import pathlib
import time
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np

import run_cross_venue_information_set_experiment as common
import run_maker_execution_opportunity_experiment as maker
import run_microstructure_alpha_development as development


SCHEMA_VERSION = "maker_execution_learnability_experiment_v1"
POLICY_SCHEMA_VERSION = "maker_execution_learnability_policy_v1"
FROZEN_POLICY_IDENTITY_SHA256 = (
    "a8ce70efd90185a650d8980a2de8d02c7e20d1e2bea4233b5512e18bb1a7a109"
)
ARCHITECTURE_IDS = (
    "direct_stress_utility_regression",
    "two_stage_opportunity_action",
    "joint_action_ranker",
)
DECISION_CONTINUE = "CONTINUE_TO_INDEPENDENT_MAKER_FORWARD_VALIDATION"
DECISION_STOP = "STOP_MAKER_LEARNABILITY_FAMILY"
DECISION_UPSTREAM_STOP = "STOP_MAKER_LEARNABILITY_UPSTREAM_NOT_PROVEN"


def _false_authorities() -> Dict[str, bool]:
    return {
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }


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
    upstream = policy.get("upstream")
    if not (
        isinstance(upstream, Mapping)
        and upstream.get("required_opportunity_decision")
        == maker.DECISION_CONTINUE
        and upstream.get("required_opportunity_policy_identity_sha256")
        == maker.FROZEN_POLICY_IDENTITY_SHA256
    ):
        failures.append("upstream")
    features = policy.get("features")
    if not (
        isinstance(features, Mapping)
        and features.get("causal_contract_revision")
        == development.CAUSAL_FEATURE_CONTRACT["revision"]
        and features.get("expected_feature_count")
        == development.FROZEN_TARGET_ARCHITECTURE_FEATURE_COUNT
        and features.get("fill_proxy_used_as_model_feature") is False
        and features.get("future_values_permitted") is False
    ):
        failures.append("features")
    if policy.get("architectures") != list(ARCHITECTURE_IDS):
        failures.append("architectures")
    target = policy.get("target")
    if not (
        isinstance(target, Mapping)
        and float(target.get("unfilled_order_utility_bps", float("nan"))) == 0.0
        and target.get("filled_order_target")
        == "realized_base_net_bps_minus_stress_cost_increment"
        and target.get("architecture_selection_scope")
        == "development_diagnostic_only"
    ):
        failures.append("target")
    execution = policy.get("execution")
    if not (
        isinstance(execution, Mapping)
        and execution.get("placement_latency_seconds") == 1
        and execution.get("fill_timeout_seconds") == 5
        and float(execution.get("queue_depth_multiplier", 0.0)) == 1.25
        and float(execution.get("maker_entry_fee_bps", 0.0)) == 2.75
        and float(execution.get("taker_exit_fee_bps", 0.0)) == 5.5
        and float(execution.get("exit_slippage_bps", 0.0)) == 1.0
        and float(execution.get("stress_cost_multiplier", 0.0)) == 1.25
        and execution.get("horizons_seconds") == [15, 30, 60, 120, 300]
        and execution.get("one_outstanding_order_or_position") is True
    ):
        failures.append("execution")
    splits = policy.get("splits")
    if not (
        isinstance(splits, Mapping)
        and splits.get("count") == 6
        and splits.get("train_window_seconds") == 21600
        and splits.get("validation_window_seconds") == 14400
        and splits.get("test_window_seconds") == 14400
        and splits.get("rolling_step_seconds") == 14400
        and splits.get("model_selection_window_seconds") == 3600
        and splits.get("minimum_window_rows") == 3600
    ):
        failures.append("splits")
    calibration = policy.get("calibration")
    if not (
        isinstance(calibration, Mapping)
        and calibration.get("threshold_quantiles")
        == [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98]
        and calibration.get("minimum_validation_trades") == 8
    ):
        failures.append("calibration")
    model = policy.get("model")
    if not (
        isinstance(model, Mapping)
        and model.get("iterations") == 200
        and model.get("depth") == 4
        and float(model.get("learning_rate", 0.0)) == 0.035
        and float(model.get("l2_leaf_reg", 0.0)) == 30.0
        and float(model.get("random_strength", 0.0)) == 2.0
        and model.get("random_seed") == 20260806
        and model.get("early_stopping_rounds") == 20
    ):
        failures.append("model")
    control = policy.get("negative_control")
    if not (
        isinstance(control, Mapping)
        and control.get("permutation_trials") == 7
        and control.get("permutation_seed") == 20260808
        and float(control.get("minimum_excess_lcb_bps", -1.0)) == 0.0
    ):
        failures.append("negative_control")
    gates = policy.get("decision_gates")
    if not (
        isinstance(gates, Mapping)
        and gates.get("minimum_oos_trades") == 30
        and float(gates.get("minimum_positive_stress_split_ratio", -1.0)) == 0.6
        and float(gates.get("minimum_base_lcb_bps", -1.0)) == 0.0
        and float(gates.get("minimum_stress_lcb_bps", -1.0)) == 0.0
        and gates.get("prediction_permutation_control_required") is True
    ):
        failures.append("decision_gates")
    if policy.get("authorities") != _false_authorities():
        failures.append("authorities")
    if failures:
        raise ValueError(
            "frozen maker learnability policy mismatch: " + ",".join(failures)
        )
    return policy


def total_base_cost_bps(policy: Mapping[str, Any]) -> float:
    execution = policy["execution"]
    return sum(
        float(execution[field])
        for field in (
            "maker_entry_fee_bps",
            "taker_exit_fee_bps",
            "exit_slippage_bps",
        )
    )


def validate_upstream_report(
    path: pathlib.Path,
    *,
    assessment_path: pathlib.Path,
    timestamps: np.ndarray,
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    report = common.read_json(path)
    failures: List[str] = []
    if not (
        report.get("schema_version") == maker.SCHEMA_VERSION
        and report.get("status") == "COMPLETE"
        and report.get("fully_verifiable") is True
        and report.get("promotion_evidence") is False
        and report.get("promotion_eligible") is False
    ):
        failures.append("status")
    if any(report.get(key) is not False for key in _false_authorities()):
        failures.append("authorities")
    experiment_policy = report.get("experiment_policy")
    if not (
        isinstance(experiment_policy, Mapping)
        and experiment_policy.get("identity_sha256")
        == policy["upstream"]["required_opportunity_policy_identity_sha256"]
    ):
        failures.append("opportunity_policy_identity")
    source = report.get("input")
    if not (
        isinstance(source, Mapping)
        and source.get("control_assessment_sha256")
        == common.sha256_file(assessment_path)
    ):
        failures.append("assessment_identity")
    domain = report.get("common_domain")
    if not (
        isinstance(domain, Mapping)
        and int(domain.get("row_count", -1)) == len(timestamps)
        and domain.get("timestamp_sha256") == common.array_sha256(timestamps)
        and isinstance(domain.get("splits"), list)
        and len(domain["splits"]) == int(policy["splits"]["count"])
    ):
        failures.append("common_domain")
    execution = report.get("execution_contract")
    if not (
        isinstance(execution, Mapping)
        and float(execution.get("base_cost_bps", 0.0))
        == total_base_cost_bps(policy)
        and float(execution.get("stress_cost_multiplier", 0.0))
        == float(policy["execution"]["stress_cost_multiplier"])
        and isinstance(execution.get("fill_proxy"), Mapping)
        and execution["fill_proxy"].get("fill_proxy_used_as_model_feature") is False
    ):
        failures.append("execution_contract")
    if failures:
        raise ValueError(
            "maker opportunity report contract mismatch: " + ",".join(failures)
        )
    return report


def build_observable_decision_mask(
    timestamps: np.ndarray,
    *,
    placement_latency_seconds: int,
    fill_timeout_seconds: int,
    horizons_seconds: Sequence[int],
) -> np.ndarray:
    """Require every possible fill probe and corresponding exit to be observed."""

    values = np.asarray(timestamps, dtype=np.int64)
    if len(values) == 0 or not np.all(np.diff(values) > 0):
        raise ValueError("observable decision timestamps are invalid")
    latency = int(placement_latency_seconds)
    timeout = int(fill_timeout_seconds)
    horizons = [int(value) for value in horizons_seconds]
    if latency <= 0 or timeout <= 0 or not horizons or any(value <= 0 for value in horizons):
        raise ValueError("observable decision execution contract is invalid")

    def has_offset(offset_seconds: int) -> np.ndarray:
        targets = values + int(offset_seconds) * 1000
        positions = np.searchsorted(values, targets)
        valid = positions < len(values)
        result = np.zeros(len(values), dtype=bool)
        result[valid] = values[positions[valid]] == targets[valid]
        return result

    observable = has_offset(latency)
    for fill_offset in range(1, timeout + 1):
        relative_fill = latency + fill_offset
        observable &= has_offset(relative_fill)
        for horizon in horizons:
            observable &= has_offset(relative_fill + horizon)
    return observable


def build_stress_utility_targets(
    outcomes: np.ndarray,
    *,
    base_cost_bps: float,
    stress_cost_multiplier: float,
) -> np.ndarray:
    """No fill has zero utility; an inferred fill bears the stress increment."""

    matrix = np.asarray(outcomes, dtype=np.float64)
    if matrix.ndim != 2:
        raise ValueError("maker utility outcomes must be a matrix")
    increment = float(base_cost_bps) * (float(stress_cost_multiplier) - 1.0)
    if not math.isfinite(increment) or increment <= 0.0:
        raise ValueError("maker utility stress increment is invalid")
    utilities = np.zeros(matrix.shape, dtype=np.float64)
    finite = np.isfinite(matrix)
    utilities[finite] = matrix[finite] - increment
    return utilities


def evaluate_maker_policy(
    *,
    timestamps: np.ndarray,
    prediction: np.ndarray,
    realized_base: np.ndarray,
    fill_timestamps: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    score_threshold: float,
    base_cost_bps: float,
    stress_cost_multiplier: float,
    placement_latency_seconds: int,
    fill_timeout_seconds: int,
    allowed_action_indices: Sequence[int] | None = None,
) -> Dict[str, Any]:
    scores = np.asarray(prediction, dtype=np.float64)
    outcomes = np.asarray(realized_base, dtype=np.float64)
    fills = np.asarray(fill_timestamps, dtype=np.int64)
    ts = np.asarray(timestamps, dtype=np.int64)
    action_count = len(actions)
    if not (
        scores.shape == outcomes.shape == fills.shape
        and scores.shape == (len(ts), action_count)
    ):
        raise ValueError("maker policy evaluation arrays are not aligned")
    allowed = (
        list(range(action_count))
        if allowed_action_indices is None
        else [int(value) for value in allowed_action_indices]
    )
    if not (
        allowed
        and len(set(allowed)) == len(allowed)
        and all(0 <= value < action_count for value in allowed)
    ):
        raise ValueError("maker policy allowed actions are invalid")
    threshold = float(score_threshold)
    if not math.isfinite(threshold):
        raise ValueError("maker policy threshold is invalid")
    stress_increment = float(base_cost_bps) * (
        float(stress_cost_multiplier) - 1.0
    )
    next_allowed_ms = -1
    base_edges: List[float] = []
    stress_edges: List[float] = []
    fill_latencies: List[float] = []
    action_counts: Dict[str, int] = {}
    order_count = 0
    unfilled_order_count = 0
    allowed_array = np.asarray(allowed, dtype=np.int64)
    for row_index, raw_timestamp in enumerate(ts):
        decision_timestamp = int(raw_timestamp)
        if decision_timestamp < next_allowed_ms:
            continue
        row_scores = scores[row_index]
        if not np.all(np.isfinite(row_scores[allowed_array])):
            continue
        action_index = allowed[int(np.argmax(row_scores[allowed_array]))]
        if float(row_scores[action_index]) < threshold:
            continue
        order_count += 1
        timeout_timestamp = decision_timestamp + (
            int(placement_latency_seconds) + int(fill_timeout_seconds)
        ) * 1000
        realized = float(outcomes[row_index, action_index])
        fill_timestamp = int(fills[row_index, action_index])
        if not math.isfinite(realized):
            if fill_timestamp != -1:
                raise ValueError("unfilled maker action has a fill timestamp")
            unfilled_order_count += 1
            next_allowed_ms = timeout_timestamp
            continue
        if not decision_timestamp < fill_timestamp <= timeout_timestamp:
            raise ValueError("filled maker action timestamp is outside timeout")
        action = actions[action_index]
        horizon = int(action["horizon_seconds"])
        key = f"{action['direction']}_{horizon}s"
        action_counts[key] = action_counts.get(key, 0) + 1
        base_edges.append(realized)
        stress_edges.append(realized - stress_increment)
        fill_latencies.append((fill_timestamp - decision_timestamp) / 1000.0)
        next_allowed_ms = fill_timestamp + horizon * 1000
    return {
        "score_threshold": threshold,
        "base_cost": development.summarize_edges(base_edges),
        "stress_cost": development.summarize_edges(stress_edges),
        "action_counts": action_counts,
        "order_count": order_count,
        "unfilled_order_count": unfilled_order_count,
        "filled_order_count": len(base_edges),
        "fill_rate": len(base_edges) / order_count if order_count else 0.0,
        "mean_fill_latency_seconds": (
            float(np.mean(fill_latencies)) if fill_latencies else None
        ),
        "base_edges_bps": base_edges,
        "stress_edges_bps": stress_edges,
    }


def select_nested_maker_threshold(
    *,
    timestamps: np.ndarray,
    prediction: np.ndarray,
    realized_base: np.ndarray,
    fill_timestamps: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    quantiles: Sequence[float],
    minimum_trades: int,
    base_cost_bps: float,
    stress_cost_multiplier: float,
    placement_latency_seconds: int,
    fill_timeout_seconds: int,
    score_units: str,
) -> Dict[str, Any]:
    scores = np.asarray(prediction, dtype=np.float64)
    if scores.ndim != 2 or scores.shape[1] != len(actions):
        raise ValueError("maker calibration prediction shape is invalid")
    row_maximum = np.max(scores, axis=1)
    finite = row_maximum[np.isfinite(row_maximum)]
    if not len(finite):
        return {
            "selected": None,
            "diagnostic_selected": None,
            "candidates": [],
            "score_units": str(score_units),
            "reason": "no_finite_predictions",
        }
    thresholds = sorted(
        {float(np.quantile(finite, float(quantile))) for quantile in quantiles}
    )
    candidates: List[Dict[str, Any]] = []
    for threshold in thresholds:
        objective = evaluate_maker_policy(
            timestamps=timestamps,
            prediction=scores,
            realized_base=realized_base,
            fill_timestamps=fill_timestamps,
            actions=actions,
            score_threshold=threshold,
            base_cost_bps=base_cost_bps,
            stress_cost_multiplier=stress_cost_multiplier,
            placement_latency_seconds=placement_latency_seconds,
            fill_timeout_seconds=fill_timeout_seconds,
        )
        base = objective["base_cost"]
        stress = objective["stress_cost"]
        viable = bool(
            int(base["count"]) >= int(minimum_trades)
            and (base["lcb_bps"] or float("-inf")) > 0.0
            and (stress["lcb_bps"] or float("-inf")) > 0.0
        )
        candidates.append(
            {
                "score_threshold": threshold,
                "score_units": str(score_units),
                "trade_count": int(base["count"]),
                "order_count": int(objective["order_count"]),
                "fill_rate": float(objective["fill_rate"]),
                "mean_base_net_bps": base["mean_bps"],
                "base_net_lcb_bps": base["lcb_bps"],
                "stress_net_lcb_bps": stress["lcb_bps"],
                "viable": viable,
            }
        )

    def candidate_key(item: Mapping[str, Any]) -> Tuple[float, float, float]:
        return (
            float(item["stress_net_lcb_bps"]),
            float(item["base_net_lcb_bps"]),
            -float(item["score_threshold"]),
        )

    viable = [item for item in candidates if item["viable"]]
    diagnostic = [
        item
        for item in candidates
        if int(item["trade_count"]) >= int(minimum_trades)
        and item["base_net_lcb_bps"] is not None
        and item["stress_net_lcb_bps"] is not None
    ]
    return {
        "selected": max(viable, key=candidate_key) if viable else None,
        "diagnostic_selected": (
            max(diagnostic, key=candidate_key) if diagnostic else None
        ),
        "diagnostic_selection_contract": (
            "best_nested_validation_stress_lcb_with_minimum_filled_trades;"
            "development_only_non_promotional"
        ),
        "candidates": candidates,
        "score_units": str(score_units),
        "score_distribution": development.summarize_numeric_distribution(finite),
        "reason": "positive_stress_lcb" if viable else "no_positive_stress_lcb",
    }


def evaluate_maker_permutation_controls(
    *,
    timestamps: np.ndarray,
    prediction: np.ndarray,
    realized_base: np.ndarray,
    fill_timestamps: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    score_threshold: float,
    base_cost_bps: float,
    stress_cost_multiplier: float,
    placement_latency_seconds: int,
    fill_timeout_seconds: int,
    trials: int,
    seed: int,
) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(int(seed))
    controls: List[Dict[str, Any]] = []
    for trial in range(int(trials)):
        permutation = rng.permutation(len(prediction))
        report = evaluate_maker_policy(
            timestamps=timestamps,
            prediction=np.asarray(prediction)[permutation],
            realized_base=realized_base,
            fill_timestamps=fill_timestamps,
            actions=actions,
            score_threshold=score_threshold,
            base_cost_bps=base_cost_bps,
            stress_cost_multiplier=stress_cost_multiplier,
            placement_latency_seconds=placement_latency_seconds,
            fill_timeout_seconds=fill_timeout_seconds,
        )
        report.pop("base_edges_bps", None)
        report.pop("stress_edges_bps", None)
        report["trial"] = trial
        controls.append(report)
    return controls


def _model_args(policy: Mapping[str, Any]) -> argparse.Namespace:
    return argparse.Namespace(**dict(policy["model"]))


def evaluate_architecture_split(
    *,
    architecture_id: str,
    split_id: int,
    fit_features: np.ndarray,
    fit_utilities: np.ndarray,
    selection_features: np.ndarray,
    selection_utilities: np.ndarray,
    validation_features: np.ndarray,
    validation_timestamps: np.ndarray,
    validation_outcomes: np.ndarray,
    validation_fills: np.ndarray,
    test_features: np.ndarray,
    test_timestamps: np.ndarray,
    test_outcomes: np.ndarray,
    test_fills: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    started = time.monotonic()
    predictions = development.fit_predict_experimental_architecture(
        architecture_id=architecture_id,
        fit_features=fit_features,
        fit_stress_utilities=fit_utilities,
        model_selection_features=selection_features,
        model_selection_stress_utilities=selection_utilities,
        validation_features=validation_features,
        test_features=test_features,
        actions=actions,
        args=_model_args(policy),
    )
    execution = policy["execution"]
    calibration_policy = policy["calibration"]
    base_cost = total_base_cost_bps(policy)
    calibration = select_nested_maker_threshold(
        timestamps=validation_timestamps,
        prediction=np.asarray(predictions["validation_prediction"], dtype=np.float64),
        realized_base=validation_outcomes,
        fill_timestamps=validation_fills,
        actions=actions,
        quantiles=calibration_policy["threshold_quantiles"],
        minimum_trades=int(calibration_policy["minimum_validation_trades"]),
        base_cost_bps=base_cost,
        stress_cost_multiplier=float(execution["stress_cost_multiplier"]),
        placement_latency_seconds=int(execution["placement_latency_seconds"]),
        fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
        score_units=str(predictions["score_units"]),
    )
    selected = calibration.get("diagnostic_selected")
    diagnostics = dict(predictions.get("model_diagnostics", {}))
    diagnostics["training_and_inference_seconds"] = time.monotonic() - started
    if not isinstance(selected, Mapping):
        return {
            "status": "missing_diagnostic_threshold",
            "reason": str(calibration.get("reason") or "no_nested_candidate"),
            "nested_calibration": calibration,
            "model_diagnostics": diagnostics,
            "promotion_evidence": False,
            "promotion_eligible": False,
        }
    threshold = float(selected["score_threshold"])
    test_prediction = np.asarray(predictions["test_prediction"], dtype=np.float64)
    objective = evaluate_maker_policy(
        timestamps=test_timestamps,
        prediction=test_prediction,
        realized_base=test_outcomes,
        fill_timestamps=test_fills,
        actions=actions,
        score_threshold=threshold,
        base_cost_bps=base_cost,
        stress_cost_multiplier=float(execution["stress_cost_multiplier"]),
        placement_latency_seconds=int(execution["placement_latency_seconds"]),
        fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
    )
    objective.pop("base_edges_bps", None)
    objective.pop("stress_edges_bps", None)
    control = policy["negative_control"]
    architecture_offset = ARCHITECTURE_IDS.index(str(architecture_id))
    controls = evaluate_maker_permutation_controls(
        timestamps=test_timestamps,
        prediction=test_prediction,
        realized_base=test_outcomes,
        fill_timestamps=test_fills,
        actions=actions,
        score_threshold=threshold,
        base_cost_bps=base_cost,
        stress_cost_multiplier=float(execution["stress_cost_multiplier"]),
        placement_latency_seconds=int(execution["placement_latency_seconds"]),
        fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
        trials=int(control["permutation_trials"]),
        seed=(
            int(control["permutation_seed"])
            + int(split_id) * 1_000_003
            + architecture_offset * 10_000_019
        ),
    )
    return {
        "status": "evaluated",
        "architecture_id": str(architecture_id),
        "score_units": str(predictions["score_units"]),
        "nested_calibration": calibration,
        "test_prediction_score_distribution": (
            development.summarize_numeric_distribution(
                np.max(test_prediction, axis=1)
            )
        ),
        "model_diagnostics": diagnostics,
        "oos_objective": objective,
        "oos_prediction_permutation_controls": controls,
        "promotion_evidence": False,
        "promotion_eligible": False,
    }


def add_maker_decision_gates(
    comparison: Dict[str, Any],
    *,
    split_reports: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> Tuple[str, str | None, List[str]]:
    gates = policy["decision_gates"]
    passing: List[str] = []
    reasons: List[str] = []
    for architecture_id in ARCHITECTURE_IDS:
        summary = comparison["architectures"][architecture_id]
        stress_means: List[float] = []
        for split_report in split_reports:
            architecture = split_report.get("architectures", {}).get(
                architecture_id, {}
            )
            objective = architecture.get("oos_objective", {})
            stress = objective.get("stress_cost", {})
            mean = stress.get("mean_bps") if isinstance(stress, Mapping) else None
            count = stress.get("count") if isinstance(stress, Mapping) else None
            if mean is not None:
                stress_means.append(float(mean))
            elif count == 0:
                stress_means.append(0.0)
        positive_ratio = (
            sum(value > 0.0 for value in stress_means) / len(stress_means)
            if stress_means
            else 0.0
        )
        base_lcb = summary["oos_base_cost_by_split"].get("lcb_bps")
        stress_lcb = summary["oos_stress_cost_by_split"].get("lcb_bps")
        permutation_passed = summary["prediction_permutation_control"].get("passed")
        gate_passed = bool(
            summary.get("fully_verifiable") is True
            and int(summary.get("trade_count") or 0)
            >= int(gates["minimum_oos_trades"])
            and positive_ratio
            >= float(gates["minimum_positive_stress_split_ratio"])
            and base_lcb is not None
            and float(base_lcb) > float(gates["minimum_base_lcb_bps"])
            and stress_lcb is not None
            and float(stress_lcb) > float(gates["minimum_stress_lcb_bps"])
            and permutation_passed is True
        )
        summary["positive_stress_split_ratio"] = positive_ratio
        summary["maker_decision_gate_passed"] = gate_passed
        if gate_passed:
            passing.append(architecture_id)
        else:
            reasons.append(f"{architecture_id}_maker_gate_failed")
    order = {value: index for index, value in enumerate(ARCHITECTURE_IDS)}
    leader = (
        max(
            passing,
            key=lambda value: (
                float(
                    comparison["architectures"][value][
                        "oos_stress_cost_by_split"
                    ]["lcb_bps"]
                ),
                float(
                    comparison["architectures"][value]["oos_base_cost_by_split"][
                        "lcb_bps"
                    ]
                ),
                -order[value],
            ),
        )
        if passing
        else None
    )
    comparison["maker_gate_passing_architecture_ids"] = passing
    comparison["maker_diagnostic_leader_id"] = leader
    comparison["maker_diagnostic_leader_is_preregistered"] = False
    if not comparison.get("fully_verifiable"):
        return DECISION_STOP, None, ["maker_architecture_comparison_incomplete"]
    if leader is None:
        return DECISION_STOP, None, reasons or ["no_maker_architecture_gate_passed"]
    return DECISION_CONTINUE, leader, ["maker_learnability_gate_passed"]


def _upstream_splits(
    report: Mapping[str, Any],
    *,
    timestamps: np.ndarray,
    policy: Mapping[str, Any],
) -> List[development.TimeSplit]:
    execution = policy["execution"]
    split_policy = policy["splits"]
    embargo = (
        int(execution["placement_latency_seconds"])
        + int(execution["fill_timeout_seconds"])
        + max(int(value) for value in execution["horizons_seconds"])
    )
    regenerated = development.build_time_splits(
        timestamps,
        n_splits=int(split_policy["count"]),
        train_window_seconds=int(split_policy["train_window_seconds"]),
        validation_window_seconds=int(split_policy["validation_window_seconds"]),
        test_window_seconds=int(split_policy["test_window_seconds"]),
        rolling_step_seconds=int(split_policy["rolling_step_seconds"]),
        embargo_seconds=embargo,
    )
    upstream = report["common_domain"]["splits"]
    if upstream != [dataclasses.asdict(item) for item in regenerated]:
        raise ValueError("maker learnability split contract drifted from opportunity")
    return regenerated


def run_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = pathlib.Path(args.config).resolve()
    assessment_path = pathlib.Path(args.control_assessment).resolve()
    opportunity_path = pathlib.Path(args.opportunity_report).resolve()
    policy = validate_policy(config_path)
    assessment = development.validate_capture_assessment(assessment_path)
    series = development.load_capture_rows(assessment)
    raw_timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    upstream = validate_upstream_report(
        opportunity_path,
        assessment_path=assessment_path,
        timestamps=raw_timestamps,
        policy=policy,
    )
    if upstream.get("research_decision") != policy["upstream"][
        "required_opportunity_decision"
    ]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "COMPLETE",
            "fully_verifiable": True,
            "research_domain": "forward_development_only",
            "promotion_evidence": False,
            "promotion_eligible": False,
            **_false_authorities(),
            "experiment_id": policy["experiment_id"],
            "experiment_policy": {
                "path": str(config_path),
                "sha256": common.sha256_file(config_path),
                "identity_sha256": common.canonical_sha256(policy),
            },
            "input": {
                "control_assessment_path": str(assessment_path),
                "control_assessment_sha256": common.sha256_file(
                    assessment_path
                ),
                "opportunity_report_path": str(opportunity_path),
                "opportunity_report_sha256": common.sha256_file(
                    opportunity_path
                ),
                "opportunity_decision": upstream["research_decision"],
            },
            "research_decision": DECISION_UPSTREAM_STOP,
            "diagnostic_leader_id": None,
            "diagnostic_leader_is_preregistered": False,
            "reason_codes": ["maker_opportunity_not_proven"],
            "next_action": "close_maker_execution_family",
        }

    execution = policy["execution"]
    features, feature_names = development.build_causal_features(series)
    if len(feature_names) != int(policy["features"]["expected_feature_count"]):
        raise ValueError("maker learnability feature count drift")
    base_cost = total_base_cost_bps(policy)
    outcomes, fill_timestamps, actions, fill_audit = maker.build_maker_action_returns(
        series,
        horizons_seconds=execution["horizons_seconds"],
        placement_latency_seconds=int(execution["placement_latency_seconds"]),
        fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
        queue_depth_multiplier=float(execution["queue_depth_multiplier"]),
        base_cost_bps=base_cost,
    )
    observable = build_observable_decision_mask(
        raw_timestamps,
        placement_latency_seconds=int(execution["placement_latency_seconds"]),
        fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
        horizons_seconds=execution["horizons_seconds"],
    )
    eligible = observable & np.all(np.isfinite(features), axis=1)
    timestamps = raw_timestamps[eligible]
    features = features[eligible]
    outcomes = outcomes[eligible]
    fill_timestamps = fill_timestamps[eligible]
    if len(timestamps) < 60000:
        raise development.CaptureNotReady("maker learnability eligible rows < 60000")
    finite_outcomes = np.isfinite(outcomes)
    if not np.array_equal(finite_outcomes, fill_timestamps >= 0):
        raise ValueError("maker filled outcome identity is invalid")
    utilities = build_stress_utility_targets(
        outcomes,
        base_cost_bps=base_cost,
        stress_cost_multiplier=float(execution["stress_cost_multiplier"]),
    )
    splits = _upstream_splits(upstream, timestamps=raw_timestamps, policy=policy)
    split_policy = policy["splits"]
    embargo = (
        int(execution["placement_latency_seconds"])
        + int(execution["fill_timeout_seconds"])
        + max(int(value) for value in execution["horizons_seconds"])
    )
    split_reports: List[Dict[str, Any]] = []
    for split in splits:
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
        minimum = int(split_policy["minimum_window_rows"])
        minimum_selection = development.minimum_internal_model_selection_rows(
            minimum_window_rows=minimum,
            model_selection_window_seconds=int(
                split_policy["model_selection_window_seconds"]
            ),
            train_window_seconds=int(split_policy["train_window_seconds"]),
        )
        row_counts = {
            "model_fit": int(len(model_fit)),
            "model_selection": int(len(model_selection)),
            "nested_validation": int(len(validation)),
            "oos_test": int(len(test)),
        }
        architecture_reports: Dict[str, Dict[str, Any]] = {}
        if (
            len(model_fit) < minimum
            or len(model_selection) < minimum_selection
            or len(validation) < minimum
            or len(test) < minimum
        ):
            architecture_reports = {
                architecture_id: {
                    "status": "insufficient_rows",
                    "reason": json.dumps(row_counts, sort_keys=True),
                    "promotion_evidence": False,
                    "promotion_eligible": False,
                }
                for architecture_id in ARCHITECTURE_IDS
            }
        else:
            for architecture_id in ARCHITECTURE_IDS:
                try:
                    architecture_reports[architecture_id] = (
                        evaluate_architecture_split(
                            architecture_id=architecture_id,
                            split_id=int(split.split_id),
                            fit_features=features[model_fit],
                            fit_utilities=utilities[model_fit],
                            selection_features=features[model_selection],
                            selection_utilities=utilities[model_selection],
                            validation_features=features[validation],
                            validation_timestamps=timestamps[validation],
                            validation_outcomes=outcomes[validation],
                            validation_fills=fill_timestamps[validation],
                            test_features=features[test],
                            test_timestamps=timestamps[test],
                            test_outcomes=outcomes[test],
                            test_fills=fill_timestamps[test],
                            actions=actions,
                            policy=policy,
                        )
                    )
                except Exception as exc:  # isolated architecture evidence
                    architecture_reports[architecture_id] = {
                        "status": "training_or_evaluation_error",
                        "reason": f"{type(exc).__name__}:{exc}",
                        "promotion_evidence": False,
                        "promotion_eligible": False,
                    }
        partition = {
            "split_id": int(split.split_id),
            "time_contract": dataclasses.asdict(split),
            "fit_internal_model_selection_time_contract": selection_contract,
            "row_counts": row_counts,
            "row_index_sha256": {
                "model_fit": development.integer_index_sha256(model_fit),
                "model_selection": development.integer_index_sha256(model_selection),
                "nested_validation": development.integer_index_sha256(validation),
                "oos_test": development.integer_index_sha256(test),
            },
        }
        partition["identity_sha256"] = common.canonical_sha256(partition)
        split_reports.append(
            {
                "split_id": int(split.split_id),
                "shared_partition_identity": partition,
                "architectures": architecture_reports,
            }
        )

    control = policy["negative_control"]
    comparison = development.aggregate_target_architecture_comparison(
        split_reports=split_reports,
        architecture_ids=ARCHITECTURE_IDS,
        required_split_count=int(split_policy["count"]),
        permutation_trials=int(control["permutation_trials"]),
        permutation_seed=int(control["permutation_seed"]),
        permutation_minimum_excess_lcb_bps=float(
            control["minimum_excess_lcb_bps"]
        ),
        frozen_contract_failures=[],
    )
    decision, leader, reason_codes = add_maker_decision_gates(
        comparison, split_reports=split_reports, policy=policy
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE" if comparison["fully_verifiable"] else "NOT_READY",
        "fully_verifiable": bool(comparison["fully_verifiable"]),
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        **_false_authorities(),
        "experiment_id": policy["experiment_id"],
        "experiment_policy": {
            "path": str(config_path),
            "sha256": common.sha256_file(config_path),
            "identity_sha256": common.canonical_sha256(policy),
        },
        "input": {
            "control_assessment_path": str(assessment_path),
            "control_assessment_sha256": common.sha256_file(assessment_path),
            "opportunity_report_path": str(opportunity_path),
            "opportunity_report_sha256": common.sha256_file(opportunity_path),
            "opportunity_decision": upstream["research_decision"],
        },
        "data": {
            "raw_row_count": int(len(raw_timestamps)),
            "observable_row_count": int(np.sum(observable)),
            "eligible_row_count": int(len(timestamps)),
            "feature_count": len(feature_names),
            "ordered_feature_names_sha256": common.canonical_sha256(
                {"feature_names": feature_names}
            ),
            "eligible_timestamp_sha256": common.array_sha256(timestamps),
        },
        "feature_contract": {
            **development.CAUSAL_FEATURE_CONTRACT,
            "fill_proxy_used_as_model_feature": False,
            "future_fill_proxy_scope": "label_and_realized_economics_only",
        },
        "execution_contract": {
            **dict(execution),
            "base_cost_bps": base_cost,
            "stress_cost_increment_bps": base_cost
            * (float(execution["stress_cost_multiplier"]) - 1.0),
            "unfilled_order_utility_bps": 0.0,
            "unfilled_signal_occupancy": "placement_plus_fill_timeout",
            "filled_signal_occupancy": "actual_fill_timestamp_plus_horizon",
        },
        "fill_audit": fill_audit,
        "architecture_comparison": comparison,
        "split_reports": split_reports,
        "research_decision": decision,
        "diagnostic_leader_id": leader,
        "diagnostic_leader_is_preregistered": False,
        "reason_codes": reason_codes,
        "next_action": (
            "preregister_leader_and_collect_independent_forward_maker_window"
            if decision == DECISION_CONTINUE
            else "close_maker_learnability_family_and_change_payoff_or_horizon"
        ),
        "independent_forward_validation_required": decision == DECISION_CONTINUE,
    }


def not_ready_report(args: argparse.Namespace, reason_code: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "NOT_READY",
        "fully_verifiable": False,
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        **_false_authorities(),
        "research_decision": "NOT_READY",
        "reason_codes": [reason_code],
        "next_action": "fix_input_or_complete_maker_opportunity_evidence",
        "experiment_policy": {"path": str(pathlib.Path(args.config).resolve())},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-assessment", required=True)
    parser.add_argument("--opportunity-report", required=True)
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
    except Exception as exc:
        report = not_ready_report(
            args, f"invalid_input:{type(exc).__name__}:{exc}"
        )
    common.atomic_write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report.get("status") == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
