#!/usr/bin/env python3
"""Test whether checksum-bound 250ms queue paths repair maker fill toxicity.

The six OOS windows, maker fill proxy, costs, and occupancy contract remain
unchanged.  The only information-set change is a deterministic 250ms replay of
the already captured raw L50/public-trade messages.  The model explicitly
separates fill probability from positive stress-net utility conditional on a
fill.  This is development-only evidence and cannot activate trading.
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

import collect_bybit_microstructure as collector
import run_cross_venue_information_set_experiment as common
import run_maker_execution_learnability_experiment as learnability
import run_maker_execution_opportunity_experiment as maker
import run_microstructure_alpha_development as development


SCHEMA_VERSION = "maker_subsecond_information_experiment_v1"
POLICY_SCHEMA_VERSION = "maker_subsecond_information_policy_v1"
FROZEN_POLICY_IDENTITY_SHA256 = (
    "ab09595e712d35bce87a5599a55ebd5ff680183de05dbb13b6415422166ed219"
)
VARIANT_IDS = (
    "one_second_decomposed_baseline",
    "subsecond_queue_decomposed_treatment",
)
DECISION_CONTINUE = "CONTINUE_TO_INDEPENDENT_SUBSECOND_MAKER_FORWARD_VALIDATION"
DECISION_STOP = "STOP_MAKER_INFORMATION_SET"
DECISION_UPSTREAM_STOP = "STOP_SUBSECOND_EXPERIMENT_UPSTREAM_NOT_PROVEN"

SYMBOL_PREFIXES = (("sol", ""), ("btc", "btc_"), ("eth", "eth_"))
SUBSECOND_METRICS = (
    "mid_log_path_bps",
    "microprice_log_path_bps",
    "spread_change_bps",
    "book_imbalance_l1_change",
    "book_imbalance_l5_change",
    "book_imbalance_l20_change",
    "best_bid_size_log_change",
    "best_ask_size_log_change",
    "depth_slope_change",
    "book_ofi_acceleration",
    "book_flow_imbalance_acceleration",
    "trade_imbalance_acceleration",
    "book_update_last_quarter_share",
    "trade_last_quarter_share",
    "book_mid_range_last_quarter_share",
    "book_flow_volume_last_quarter_share",
    "aggressive_quote_last_quarter_share",
)
SUBSECOND_FEATURE_NAMES = tuple(
    f"{symbol}_{metric}"
    for symbol, _ in SYMBOL_PREFIXES
    for metric in SUBSECOND_METRICS
)


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
        and policy.get("single_variable_change")
        == "append_checksum_bound_250ms_queue_dynamics_from_existing_raw_capture"
    ):
        failures.append("research_domain")
    upstream = policy.get("upstream")
    if not (
        isinstance(upstream, Mapping)
        and upstream.get("required_learnability_decision")
        == learnability.DECISION_STOP
        and upstream.get("required_learnability_policy_identity_sha256")
        == learnability.FROZEN_POLICY_IDENTITY_SHA256
        and upstream.get("required_capture_schema_version") == collector.SCHEMA_VERSION
    ):
        failures.append("upstream")
    features = policy.get("features")
    if not (
        isinstance(features, Mapping)
        and features.get("baseline_causal_contract_revision")
        == development.CAUSAL_FEATURE_CONTRACT["revision"]
        and features.get("expected_baseline_feature_count")
        == development.FROZEN_TARGET_ARCHITECTURE_FEATURE_COUNT
        and features.get("subsecond_bucket_ms") == 250
        and features.get("required_quarters_per_second") == 4
        and features.get("subsecond_feature_contract_revision")
        == "raw_queue_path_250ms_v1"
        and features.get("expected_subsecond_feature_count")
        == len(SUBSECOND_FEATURE_NAMES)
        and float(features.get("minimum_aligned_row_ratio", -1.0)) == 0.8
        and features.get("fill_proxy_used_as_model_feature") is False
        and features.get("future_values_permitted") is False
    ):
        failures.append("features")
    architecture = policy.get("architecture")
    if not (
        isinstance(architecture, Mapping)
        and architecture.get("id")
        == "fill_probability_times_conditional_positive_stress_utility"
        and architecture.get("comparison_variants") == list(VARIANT_IDS)
        and architecture.get("fill_model_target")
        == "conservative_full_fill_within_timeout"
        and architecture.get("toxicity_model_target")
        == "positive_stress_net_utility_conditional_on_fill"
        and architecture.get("action_features")
        == ["direction_sign", "log2_horizon_ratio"]
        and architecture.get("score")
        == "fill_probability_times_conditional_positive_probability"
    ):
        failures.append("architecture")
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
        and model.get("iterations") == 160
        and model.get("depth") == 4
        and float(model.get("learning_rate", 0.0)) == 0.035
        and float(model.get("l2_leaf_reg", 0.0)) == 30.0
        and float(model.get("random_strength", 0.0)) == 2.0
        and model.get("random_seed") == 20260822
        and model.get("early_stopping_rounds") == 20
        and model.get("conditional_positive_class_weighting") == "balanced"
    ):
        failures.append("model")
    control = policy.get("negative_control")
    if not (
        isinstance(control, Mapping)
        and control.get("permutation_trials") == 7
        and control.get("permutation_seed") == 20260823
        and float(control.get("minimum_excess_lcb_bps", -1.0)) == 0.0
        and control.get("treatment_control")
        == "permute_only_subsecond_rows_at_oos_inference"
    ):
        failures.append("negative_control")
    gates = policy.get("decision_gates")
    expected_gates = {
        "minimum_oos_trades": 30,
        "minimum_positive_stress_split_ratio": 0.6,
        "minimum_base_lcb_bps": 0.0,
        "minimum_stress_lcb_bps": 0.0,
        "minimum_stress_lcb_improvement_bps": 0.0,
        "minimum_treatment_fill_roc_auc": 0.6,
        "minimum_treatment_profitability_roc_auc": 0.55,
        "minimum_profitability_roc_auc_gain": 0.01,
        "minimum_positive_profitability_auc_gain_split_ratio": 0.6,
        "subsecond_permutation_control_required": True,
    }
    if not isinstance(gates, Mapping) or dict(gates) != expected_gates:
        failures.append("decision_gates")
    if policy.get("authorities") != _false_authorities():
        failures.append("authorities")
    if failures:
        raise ValueError(
            "frozen maker subsecond policy mismatch: " + ",".join(failures)
        )
    return policy


def _value(row: Mapping[str, Any], prefix: str, field: str) -> float:
    value = float(row[f"{prefix}{field}"])
    if not math.isfinite(value):
        raise ValueError(f"non-finite quarter feature: {prefix}{field}")
    return value


def _log_path(first: float, last: float) -> float:
    if first <= 0.0 or last <= 0.0:
        raise ValueError("subsecond positive state feature is invalid")
    return math.log(last / first) * 10000.0


def _last_share(values: Sequence[float]) -> float:
    if any(value < 0.0 or not math.isfinite(value) for value in values):
        raise ValueError("subsecond activity feature is invalid")
    total = float(sum(values))
    return float(values[-1] / total) if total > 0.0 else 0.0


def summarize_subsecond_quarters(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[int, np.ndarray]:
    """Collapse four complete causal quarter buckets into one decision row."""

    if len(rows) != 4:
        raise ValueError("a subsecond decision row requires four quarters")
    ordered = sorted(rows, key=lambda item: int(item["timestamp"]))
    timestamps = [int(row["timestamp"]) for row in ordered]
    second = timestamps[0] - timestamps[0] % 1000
    if timestamps != [second, second + 250, second + 500, second + 750]:
        raise ValueError("subsecond quarters are incomplete or misaligned")
    output: List[float] = []
    for _, prefix in SYMBOL_PREFIXES:
        first, last = ordered[0], ordered[-1]
        output.extend(
            [
                _log_path(_value(first, prefix, "mid"), _value(last, prefix, "mid")),
                _log_path(
                    _value(first, prefix, "microprice"),
                    _value(last, prefix, "microprice"),
                ),
                _value(last, prefix, "spread_bps")
                - _value(first, prefix, "spread_bps"),
                _value(last, prefix, "book_imbalance_l1")
                - _value(first, prefix, "book_imbalance_l1"),
                _value(last, prefix, "book_imbalance_l5")
                - _value(first, prefix, "book_imbalance_l5"),
                _value(last, prefix, "book_imbalance_l20")
                - _value(first, prefix, "book_imbalance_l20"),
                _log_path(
                    _value(first, prefix, "best_bid_size"),
                    _value(last, prefix, "best_bid_size"),
                ),
                _log_path(
                    _value(first, prefix, "best_ask_size"),
                    _value(last, prefix, "best_ask_size"),
                ),
                _value(last, prefix, "depth_slope")
                - _value(first, prefix, "depth_slope"),
            ]
        )
        for field in ("book_ofi", "book_flow_imbalance", "trade_imbalance"):
            previous = [_value(row, prefix, field) for row in ordered[:3]]
            output.append(_value(last, prefix, field) - float(np.mean(previous)))
        for field in (
            "book_update_count",
            "trade_count",
            "book_mid_range_bps",
            "book_flow_quote_volume",
        ):
            output.append(
                _last_share([_value(row, prefix, field) for row in ordered])
            )
        aggressive = [
            _value(row, prefix, "buy_quote_volume")
            + _value(row, prefix, "sell_quote_volume")
            for row in ordered
        ]
        output.append(_last_share(aggressive))
    matrix = np.asarray(output, dtype=np.float64)
    if matrix.shape != (len(SUBSECOND_FEATURE_NAMES),) or not np.all(
        np.isfinite(matrix)
    ):
        raise ValueError("subsecond feature vector contract failed")
    return second, matrix


def load_subsecond_features(
    assessment: Mapping[str, Any],
    *,
    bucket_ms: int,
) -> Dict[str, Any]:
    """Replay only checksum-attested raw segments; never glob mutable inputs."""

    if int(bucket_ms) != 250:
        raise ValueError("only the frozen 250ms replay is supported")
    manifest = assessment.get("segments")
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("capture raw segment manifest is empty")
    by_timestamp: Dict[int, np.ndarray] = {}
    owner_by_timestamp: Dict[int, int] = {}
    dropped_boundaries: set[int] = set()
    incomplete_quarter_seconds = 0
    raw_message_count = 0
    quarter_row_count = 0
    raw_identities: List[Dict[str, Any]] = []
    for segment_index, item in enumerate(manifest):
        if not isinstance(item, Mapping):
            raise ValueError("capture raw segment item is invalid")
        if not (
            item.get("capture_schema_version") == collector.SCHEMA_VERSION
            and item.get("symbols") == list(collector.CAPTURE_SYMBOLS)
        ):
            raise ValueError("capture raw segment schema mismatch")
        raw_path = pathlib.Path(str(item.get("raw_path") or ""))
        expected_hash = str(item.get("raw_sha256") or "")
        expected_count = int(item.get("raw_message_count", -1))
        if not raw_path.is_file() or len(expected_hash) != 64:
            raise ValueError(f"capture raw artifact is missing: {raw_path}")
        if common.sha256_file(raw_path) != expected_hash:
            raise ValueError(f"capture raw checksum mismatch: {raw_path}")
        rows, observed_count = collector.replay_jsonl(
            raw_path,
            symbol=collector.TARGET_SYMBOL,
            context_symbols=collector.CONTEXT_SYMBOLS,
            bucket_ms=250,
        )
        if observed_count != expected_count:
            raise ValueError(f"capture raw message-count mismatch: {raw_path}")
        raw_message_count += observed_count
        quarter_row_count += len(rows)
        raw_identities.append(
            {
                "raw_sha256": expected_hash,
                "raw_message_count": expected_count,
            }
        )
        quarters_by_second: Dict[int, List[Mapping[str, Any]]] = {}
        for row in rows:
            timestamp = int(row["timestamp"])
            second = timestamp - timestamp % 1000
            quarters_by_second.setdefault(second, []).append(row)
        for second, quarters in sorted(quarters_by_second.items()):
            try:
                timestamp, values = summarize_subsecond_quarters(quarters)
            except ValueError:
                incomplete_quarter_seconds += 1
                continue
            if timestamp in dropped_boundaries:
                raise ValueError(
                    "subsecond boundary appears in more than two raw segments"
                )
            if timestamp in by_timestamp:
                previous_owner = owner_by_timestamp[timestamp]
                if previous_owner != segment_index - 1:
                    raise ValueError("non-adjacent duplicate subsecond decision row")
                del by_timestamp[timestamp]
                del owner_by_timestamp[timestamp]
                dropped_boundaries.add(timestamp)
            else:
                by_timestamp[timestamp] = values
                owner_by_timestamp[timestamp] = segment_index
    if not by_timestamp:
        raise development.CaptureNotReady("raw replay produced no complete quarters")
    timestamps = np.asarray(sorted(by_timestamp), dtype=np.int64)
    matrix = np.vstack([by_timestamp[int(value)] for value in timestamps])
    if not np.all(np.diff(timestamps) > 0) or not np.all(np.isfinite(matrix)):
        raise ValueError("subsecond replay output is invalid")
    return {
        "timestamp": timestamps,
        "features": matrix,
        "feature_names": list(SUBSECOND_FEATURE_NAMES),
        "audit": {
            "method": "checksum_bound_raw_replay_250ms_complete_quarters_v1",
            "capture_schema_version": collector.SCHEMA_VERSION,
            "input_segment_count": len(manifest),
            "raw_message_count": raw_message_count,
            "quarter_row_count": quarter_row_count,
            "complete_second_count": len(timestamps),
            "incomplete_quarter_second_count": incomplete_quarter_seconds,
            "dropped_shared_boundary_second_count": len(dropped_boundaries),
            "dropped_shared_boundary_timestamp_sha256": common.canonical_sha256(
                {"timestamps_ms": sorted(dropped_boundaries)}
            ),
            "raw_manifest_identity_sha256": common.canonical_sha256(
                {"segments": raw_identities}
            ),
            "output_timestamp_sha256": common.array_sha256(timestamps),
        },
    }


def validate_upstream_report(
    path: pathlib.Path,
    *,
    assessment_path: pathlib.Path,
    policy: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[development.TimeSplit]]:
    report = common.read_json(path)
    failures: List[str] = []
    terminal_decisions = {
        learnability.DECISION_CONTINUE,
        learnability.DECISION_STOP,
        learnability.DECISION_UPSTREAM_STOP,
    }
    if not (
        report.get("schema_version") == learnability.SCHEMA_VERSION
        and report.get("status") == "COMPLETE"
        and report.get("fully_verifiable") is True
        and report.get("promotion_evidence") is False
        and report.get("promotion_eligible") is False
        and report.get("research_decision") in terminal_decisions
    ):
        failures.append("status")
    if any(report.get(key) is not False for key in _false_authorities()):
        failures.append("authorities")
    experiment_policy = report.get("experiment_policy")
    if not (
        isinstance(experiment_policy, Mapping)
        and experiment_policy.get("identity_sha256")
        == policy["upstream"]["required_learnability_policy_identity_sha256"]
    ):
        failures.append("learnability_policy_identity")
    source = report.get("input")
    if not (
        isinstance(source, Mapping)
        and source.get("control_assessment_sha256")
        == common.sha256_file(assessment_path)
    ):
        failures.append("assessment_identity")
    splits: List[development.TimeSplit] = []
    if report.get("research_decision") == policy["upstream"][
        "required_learnability_decision"
    ]:
        raw_splits = report.get("split_reports")
        if not isinstance(raw_splits, list) or len(raw_splits) != int(
            policy["splits"]["count"]
        ):
            failures.append("splits")
        else:
            try:
                for expected_id, item in enumerate(raw_splits):
                    partition = item["shared_partition_identity"]
                    contract = partition["time_contract"]
                    split = development.TimeSplit(**contract)
                    if split.split_id != expected_id:
                        raise ValueError("split id")
                    splits.append(split)
                split_policy = policy["splits"]
                for split in splits:
                    if not (
                        split.fit_end_ms - split.fit_start_ms
                        == int(split_policy["train_window_seconds"]) * 1000
                        and split.validation_end_ms - split.validation_start_ms
                        == int(split_policy["validation_window_seconds"]) * 1000
                        and split.test_end_ms - split.test_start_ms
                        == int(split_policy["test_window_seconds"]) * 1000
                    ):
                        raise ValueError("split duration")
                for previous, current in zip(splits, splits[1:]):
                    if current.test_start_ms - previous.test_start_ms != int(
                        split_policy["rolling_step_seconds"]
                    ) * 1000:
                        raise ValueError("rolling step")
            except (KeyError, TypeError, ValueError):
                failures.append("split_contract")
    if failures:
        raise ValueError(
            "maker learnability upstream contract mismatch: " + ",".join(failures)
        )
    return report, splits


@dataclasses.dataclass
class BinaryProbabilityModel:
    model: Any | None
    constant: float | None
    best_iteration: int | None


def fit_binary_probability_model(
    fit_features: np.ndarray,
    fit_labels: np.ndarray,
    selection_features: np.ndarray,
    selection_labels: np.ndarray,
    *,
    policy: Mapping[str, Any],
    seed_offset: int,
    balanced: bool,
) -> BinaryProbabilityModel:
    labels = np.asarray(fit_labels, dtype=np.float64)
    selection = np.asarray(selection_labels, dtype=np.float64)
    if not (
        len(labels) == len(fit_features)
        and len(selection) == len(selection_features)
        and np.all((labels == 0.0) | (labels == 1.0))
        and np.all((selection == 0.0) | (selection == 1.0))
    ):
        raise ValueError("binary decomposition training data is invalid")
    unique = np.unique(labels)
    if len(unique) == 1:
        return BinaryProbabilityModel(None, float(unique[0]), None)
    if development.CatBoostClassifier is None:
        raise RuntimeError("catboost classifier is required; use research image")
    args = policy["model"]
    kwargs: Dict[str, Any] = {
        "loss_function": "Logloss",
        "eval_metric": "Logloss",
        "boost_from_average": True,
        "iterations": int(args["iterations"]),
        "depth": int(args["depth"]),
        "learning_rate": float(args["learning_rate"]),
        "l2_leaf_reg": float(args["l2_leaf_reg"]),
        "random_strength": float(args["random_strength"]),
        "random_seed": int(args["random_seed"]) + int(seed_offset),
        "allow_writing_files": False,
        "verbose": False,
    }
    if balanced:
        kwargs["auto_class_weights"] = "Balanced"
    model = development.CatBoostClassifier(**kwargs)
    model.fit(
        np.asarray(fit_features, dtype=np.float32),
        labels,
        eval_set=(np.asarray(selection_features, dtype=np.float32), selection),
        early_stopping_rounds=int(args["early_stopping_rounds"]),
        verbose=False,
    )
    best = model.get_best_iteration()
    return BinaryProbabilityModel(
        model=model,
        constant=None,
        best_iteration=int(best) + 1 if isinstance(best, int) and best >= 0 else None,
    )


def predict_binary_probability(
    fitted: BinaryProbabilityModel, features: np.ndarray
) -> np.ndarray:
    if fitted.constant is not None:
        return np.full(len(features), fitted.constant, dtype=np.float64)
    if fitted.model is None:
        raise ValueError("binary probability model is missing")
    probability = np.asarray(
        fitted.model.predict_proba(np.asarray(features, dtype=np.float32)),
        dtype=np.float64,
    )[:, 1]
    if not np.all(np.isfinite(probability)):
        raise ValueError("binary probability prediction is invalid")
    return probability


def _action_descriptors(
    actions: Sequence[Mapping[str, Any]],
) -> np.ndarray:
    descriptors = np.asarray(
        [
            [
                1.0 if str(action["direction"]) == "long" else -1.0,
                math.log2(float(action["horizon_seconds"]) / 15.0),
            ]
            for action in actions
        ],
        dtype=np.float32,
    )
    if descriptors.shape != (len(actions), 2) or not np.all(
        np.isfinite(descriptors)
    ):
        raise ValueError("maker action descriptors are invalid")
    return descriptors


def _direction_action_indices(
    actions: Sequence[Mapping[str, Any]],
) -> Tuple[int, int]:
    indices = []
    for direction in ("long", "short"):
        matches = [
            index
            for index, action in enumerate(actions)
            if str(action["direction"]) == direction
        ]
        if not matches:
            raise ValueError(f"maker actions missing {direction}")
        indices.append(matches[0])
    return int(indices[0]), int(indices[1])


def build_fill_training_rows(
    features: np.ndarray,
    fill_timestamps: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
) -> Tuple[np.ndarray, np.ndarray]:
    matrix = np.asarray(features, dtype=np.float32)
    fills = np.asarray(fill_timestamps, dtype=np.int64)
    long_index, short_index = _direction_action_indices(actions)
    direction = np.tile(np.asarray([1.0, -1.0], dtype=np.float32), len(matrix))
    stacked = np.column_stack((np.repeat(matrix, 2, axis=0), direction))
    labels = np.column_stack(
        (fills[:, long_index] >= 0, fills[:, short_index] >= 0)
    ).reshape(-1)
    return stacked, labels.astype(np.float64)


def build_profitability_training_rows(
    features: np.ndarray,
    outcomes: np.ndarray,
    utilities: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
) -> Tuple[np.ndarray, np.ndarray]:
    matrix = np.asarray(features, dtype=np.float32)
    realized = np.asarray(outcomes, dtype=np.float64)
    stress = np.asarray(utilities, dtype=np.float64)
    if realized.shape != stress.shape or realized.shape[1] != len(actions):
        raise ValueError("maker profitability targets are not aligned")
    row_indices, action_indices = np.nonzero(np.isfinite(realized))
    if not len(row_indices):
        raise development.CaptureNotReady("no filled maker actions for toxicity model")
    descriptors = _action_descriptors(actions)
    stacked = np.column_stack(
        (matrix[row_indices], descriptors[action_indices])
    ).astype(np.float32, copy=False)
    labels = (stress[row_indices, action_indices] > 0.0).astype(np.float64)
    return stacked, labels


@dataclasses.dataclass
class DecomposedModel:
    fill: BinaryProbabilityModel
    profitability: BinaryProbabilityModel
    market_feature_count: int


def fit_decomposed_model(
    *,
    fit_features: np.ndarray,
    fit_outcomes: np.ndarray,
    fit_utilities: np.ndarray,
    fit_fills: np.ndarray,
    selection_features: np.ndarray,
    selection_outcomes: np.ndarray,
    selection_utilities: np.ndarray,
    selection_fills: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    seed_offset: int,
) -> Tuple[DecomposedModel, Dict[str, Any]]:
    fill_x, fill_y = build_fill_training_rows(fit_features, fit_fills, actions)
    fill_selection_x, fill_selection_y = build_fill_training_rows(
        selection_features, selection_fills, actions
    )
    profit_x, profit_y = build_profitability_training_rows(
        fit_features, fit_outcomes, fit_utilities, actions
    )
    profit_selection_x, profit_selection_y = build_profitability_training_rows(
        selection_features,
        selection_outcomes,
        selection_utilities,
        actions,
    )
    fill_model = fit_binary_probability_model(
        fill_x,
        fill_y,
        fill_selection_x,
        fill_selection_y,
        policy=policy,
        seed_offset=seed_offset,
        balanced=False,
    )
    profit_model = fit_binary_probability_model(
        profit_x,
        profit_y,
        profit_selection_x,
        profit_selection_y,
        policy=policy,
        seed_offset=seed_offset + 100_003,
        balanced=True,
    )
    return (
        DecomposedModel(fill_model, profit_model, fit_features.shape[1]),
        {
            "market_feature_count": int(fit_features.shape[1]),
            "fill_fit_row_count": int(len(fill_y)),
            "fill_fit_positive_count": int(np.sum(fill_y)),
            "profitability_fit_row_count": int(len(profit_y)),
            "profitability_fit_positive_count": int(np.sum(profit_y)),
            "fill_best_iteration": fill_model.best_iteration,
            "profitability_best_iteration": profit_model.best_iteration,
            "conditional_positive_class_weighting": "balanced",
        },
    )


def predict_decomposed_scores(
    fitted: DecomposedModel,
    features: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
) -> Dict[str, np.ndarray]:
    matrix = np.asarray(features, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != fitted.market_feature_count:
        raise ValueError("decomposed inference feature contract mismatch")
    fill_x = np.column_stack(
        (
            np.repeat(matrix, 2, axis=0),
            np.tile(np.asarray([1.0, -1.0], dtype=np.float32), len(matrix)),
        )
    )
    fill_direction = predict_binary_probability(fitted.fill, fill_x).reshape(
        len(matrix), 2
    )
    descriptors = _action_descriptors(actions)
    profit_x = np.column_stack(
        (
            np.repeat(matrix, len(actions), axis=0),
            np.tile(descriptors, (len(matrix), 1)),
        )
    )
    profitability = predict_binary_probability(
        fitted.profitability, profit_x
    ).reshape(len(matrix), len(actions))
    direction_columns = np.asarray(
        [0 if str(action["direction"]) == "long" else 1 for action in actions],
        dtype=np.int64,
    )
    fill_by_action = fill_direction[:, direction_columns]
    score = fill_by_action * profitability
    if not np.all(np.isfinite(score)):
        raise ValueError("decomposed maker score is invalid")
    return {
        "score": score,
        "fill_probability_by_direction": fill_direction,
        "fill_probability_by_action": fill_by_action,
        "conditional_positive_probability": profitability,
    }


def summarize_decomposition_ranking(
    *,
    prediction: Mapping[str, np.ndarray],
    outcomes: np.ndarray,
    utilities: np.ndarray,
    fill_timestamps: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    fills = np.asarray(fill_timestamps, dtype=np.int64)
    long_index, short_index = _direction_action_indices(actions)
    fill_labels = np.column_stack(
        (fills[:, long_index] >= 0, fills[:, short_index] >= 0)
    ).reshape(-1)
    fill_scores = np.asarray(
        prediction["fill_probability_by_direction"], dtype=np.float64
    ).reshape(-1)
    realized = np.asarray(outcomes, dtype=np.float64)
    finite = np.isfinite(realized)
    profitability_labels = (np.asarray(utilities)[finite] > 0.0).astype(np.float64)
    profitability_scores = np.asarray(
        prediction["conditional_positive_probability"], dtype=np.float64
    )[finite]
    return {
        "fill": development.summarize_binary_ranking(fill_labels, fill_scores),
        "profitability_conditional_on_fill": development.summarize_binary_ranking(
            profitability_labels, profitability_scores
        ),
    }


def evaluate_treatment_feature_permutation_controls(
    *,
    fitted: DecomposedModel,
    baseline_features: np.ndarray,
    subsecond_features: np.ndarray,
    timestamps: np.ndarray,
    outcomes: np.ndarray,
    fills: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    threshold: float,
    policy: Mapping[str, Any],
    split_id: int,
) -> List[Dict[str, Any]]:
    control = policy["negative_control"]
    rng = np.random.default_rng(
        int(control["permutation_seed"]) + int(split_id) * 1_000_003
    )
    execution = policy["execution"]
    reports: List[Dict[str, Any]] = []
    for trial in range(int(control["permutation_trials"])):
        permutation = rng.permutation(len(subsecond_features))
        treatment = np.column_stack(
            (baseline_features, subsecond_features[permutation])
        )
        prediction = predict_decomposed_scores(fitted, treatment, actions)["score"]
        report = learnability.evaluate_maker_policy(
            timestamps=timestamps,
            prediction=prediction,
            realized_base=outcomes,
            fill_timestamps=fills,
            actions=actions,
            score_threshold=threshold,
            base_cost_bps=learnability.total_base_cost_bps(policy),
            stress_cost_multiplier=float(execution["stress_cost_multiplier"]),
            placement_latency_seconds=int(execution["placement_latency_seconds"]),
            fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
        )
        report.pop("base_edges_bps", None)
        report.pop("stress_edges_bps", None)
        report["trial"] = trial
        reports.append(report)
    return reports


def evaluate_variant_split(
    *,
    variant_id: str,
    split_id: int,
    fit_baseline: np.ndarray,
    fit_subsecond: np.ndarray,
    fit_outcomes: np.ndarray,
    fit_utilities: np.ndarray,
    fit_fills: np.ndarray,
    selection_baseline: np.ndarray,
    selection_subsecond: np.ndarray,
    selection_outcomes: np.ndarray,
    selection_utilities: np.ndarray,
    selection_fills: np.ndarray,
    validation_baseline: np.ndarray,
    validation_subsecond: np.ndarray,
    validation_timestamps: np.ndarray,
    validation_outcomes: np.ndarray,
    validation_utilities: np.ndarray,
    validation_fills: np.ndarray,
    test_baseline: np.ndarray,
    test_subsecond: np.ndarray,
    test_timestamps: np.ndarray,
    test_outcomes: np.ndarray,
    test_utilities: np.ndarray,
    test_fills: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    started = time.monotonic()
    treatment = variant_id == VARIANT_IDS[1]

    def compose(baseline: np.ndarray, subsecond: np.ndarray) -> np.ndarray:
        return (
            np.column_stack((baseline, subsecond)).astype(np.float32, copy=False)
            if treatment
            else np.asarray(baseline, dtype=np.float32)
        )

    fit_features = compose(fit_baseline, fit_subsecond)
    selection_features = compose(selection_baseline, selection_subsecond)
    validation_features = compose(validation_baseline, validation_subsecond)
    test_features = compose(test_baseline, test_subsecond)
    fitted, diagnostics = fit_decomposed_model(
        fit_features=fit_features,
        fit_outcomes=fit_outcomes,
        fit_utilities=fit_utilities,
        fit_fills=fit_fills,
        selection_features=selection_features,
        selection_outcomes=selection_outcomes,
        selection_utilities=selection_utilities,
        selection_fills=selection_fills,
        actions=actions,
        policy=policy,
        seed_offset=int(split_id) * 10_009 + (1_000_003 if treatment else 0),
    )
    validation_prediction = predict_decomposed_scores(
        fitted, validation_features, actions
    )
    test_prediction = predict_decomposed_scores(fitted, test_features, actions)
    execution = policy["execution"]
    calibration_policy = policy["calibration"]
    base_cost = learnability.total_base_cost_bps(policy)
    calibration = learnability.select_nested_maker_threshold(
        timestamps=validation_timestamps,
        prediction=validation_prediction["score"],
        realized_base=validation_outcomes,
        fill_timestamps=validation_fills,
        actions=actions,
        quantiles=calibration_policy["threshold_quantiles"],
        minimum_trades=int(calibration_policy["minimum_validation_trades"]),
        base_cost_bps=base_cost,
        stress_cost_multiplier=float(execution["stress_cost_multiplier"]),
        placement_latency_seconds=int(execution["placement_latency_seconds"]),
        fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
        score_units="fill_probability_times_positive_probability",
    )
    selected = calibration.get("diagnostic_selected")
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
    objective = learnability.evaluate_maker_policy(
        timestamps=test_timestamps,
        prediction=test_prediction["score"],
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
    if treatment:
        controls = evaluate_treatment_feature_permutation_controls(
            fitted=fitted,
            baseline_features=test_baseline,
            subsecond_features=test_subsecond,
            timestamps=test_timestamps,
            outcomes=test_outcomes,
            fills=test_fills,
            actions=actions,
            threshold=threshold,
            policy=policy,
            split_id=split_id,
        )
        control_contract = "permute_only_subsecond_rows_at_oos_inference"
    else:
        controls = learnability.evaluate_maker_permutation_controls(
            timestamps=test_timestamps,
            prediction=test_prediction["score"],
            realized_base=test_outcomes,
            fill_timestamps=test_fills,
            actions=actions,
            score_threshold=threshold,
            base_cost_bps=base_cost,
            stress_cost_multiplier=float(execution["stress_cost_multiplier"]),
            placement_latency_seconds=int(execution["placement_latency_seconds"]),
            fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
            trials=int(policy["negative_control"]["permutation_trials"]),
            seed=int(policy["negative_control"]["permutation_seed"])
            + int(split_id) * 1_000_003,
        )
        control_contract = "permute_complete_prediction_rows_at_oos"
    return {
        "status": "evaluated",
        "variant_id": variant_id,
        "score_units": "fill_probability_times_positive_probability",
        "nested_calibration": calibration,
        "model_diagnostics": diagnostics,
        "oos_decomposition_ranking": summarize_decomposition_ranking(
            prediction=test_prediction,
            outcomes=test_outcomes,
            utilities=test_utilities,
            fill_timestamps=test_fills,
            actions=actions,
        ),
        "oos_objective": objective,
        "oos_prediction_permutation_controls": controls,
        "permutation_control_contract": control_contract,
        "promotion_evidence": False,
        "promotion_eligible": False,
    }


def _mean_metric(
    split_reports: Sequence[Mapping[str, Any]], variant_id: str, path: Sequence[str]
) -> Tuple[List[float], Dict[str, Any]]:
    values: List[float] = []
    for report in split_reports:
        value: Any = report.get("architectures", {}).get(variant_id, {})
        parent: Any = None
        for key in path:
            parent = value
            value = value.get(key) if isinstance(value, Mapping) else None
        if (
            value is None
            and path
            and path[-1] == "mean_bps"
            and isinstance(parent, Mapping)
            and parent.get("count") == 0
        ):
            value = 0.0
        if value is not None:
            values.append(float(value))
    return values, development.summarize_edges(values)


def add_decision_gates(
    comparison: Dict[str, Any],
    *,
    split_reports: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> Tuple[str, List[str], Dict[str, Any]]:
    baseline = comparison["architectures"][VARIANT_IDS[0]]
    treatment = comparison["architectures"][VARIANT_IDS[1]]
    baseline_stress, _ = _mean_metric(
        split_reports, VARIANT_IDS[0], ("oos_objective", "stress_cost", "mean_bps")
    )
    treatment_stress, _ = _mean_metric(
        split_reports, VARIANT_IDS[1], ("oos_objective", "stress_cost", "mean_bps")
    )
    fill_auc, fill_auc_summary = _mean_metric(
        split_reports,
        VARIANT_IDS[1],
        ("oos_decomposition_ranking", "fill", "roc_auc"),
    )
    baseline_profit_auc, _ = _mean_metric(
        split_reports,
        VARIANT_IDS[0],
        (
            "oos_decomposition_ranking",
            "profitability_conditional_on_fill",
            "roc_auc",
        ),
    )
    treatment_profit_auc, treatment_profit_auc_summary = _mean_metric(
        split_reports,
        VARIANT_IDS[1],
        (
            "oos_decomposition_ranking",
            "profitability_conditional_on_fill",
            "roc_auc",
        ),
    )
    if not (
        len(baseline_stress)
        == len(treatment_stress)
        == len(fill_auc)
        == len(baseline_profit_auc)
        == len(treatment_profit_auc)
        == int(policy["splits"]["count"])
    ):
        return DECISION_STOP, ["subsecond_comparison_incomplete"], {
            "fully_verifiable": False
        }
    stress_gain = [
        treatment_value - baseline_value
        for treatment_value, baseline_value in zip(treatment_stress, baseline_stress)
    ]
    profit_auc_gain = [
        treatment_value - baseline_value
        for treatment_value, baseline_value in zip(
            treatment_profit_auc, baseline_profit_auc
        )
    ]
    positive_stress_ratio = sum(value > 0.0 for value in treatment_stress) / len(
        treatment_stress
    )
    positive_profit_auc_gain_ratio = sum(
        value > 0.0 for value in profit_auc_gain
    ) / len(profit_auc_gain)
    diagnostic = {
        "fully_verifiable": bool(comparison.get("fully_verifiable")),
        "treatment_positive_stress_split_ratio": positive_stress_ratio,
        "treatment_fill_roc_auc_by_split": fill_auc_summary,
        "baseline_profitability_roc_auc_by_split": development.summarize_edges(
            baseline_profit_auc
        ),
        "treatment_profitability_roc_auc_by_split": treatment_profit_auc_summary,
        "profitability_roc_auc_gain_by_split": development.summarize_edges(
            profit_auc_gain
        ),
        "positive_profitability_roc_auc_gain_split_ratio": (
            positive_profit_auc_gain_ratio
        ),
        "stress_mean_improvement_by_split": development.summarize_edges(stress_gain),
    }
    gates = policy["decision_gates"]
    failures: List[str] = []
    base_lcb = treatment["oos_base_cost_by_split"].get("lcb_bps")
    stress_lcb = treatment["oos_stress_cost_by_split"].get("lcb_bps")
    baseline_stress_lcb = baseline["oos_stress_cost_by_split"].get("lcb_bps")
    stress_lcb_improvement = (
        float(stress_lcb) - float(baseline_stress_lcb)
        if stress_lcb is not None and baseline_stress_lcb is not None
        else None
    )
    diagnostic["stress_lcb_improvement_bps"] = stress_lcb_improvement
    fill_auc_mean = fill_auc_summary.get("mean_bps")
    profit_auc_mean = treatment_profit_auc_summary.get("mean_bps")
    profit_auc_gain_mean = diagnostic["profitability_roc_auc_gain_by_split"].get(
        "mean_bps"
    )
    checks = (
        (comparison.get("fully_verifiable") is True, "comparison_incomplete"),
        (
            int(treatment.get("trade_count") or 0)
            >= int(gates["minimum_oos_trades"]),
            "insufficient_oos_trades",
        ),
        (
            positive_stress_ratio
            >= float(gates["minimum_positive_stress_split_ratio"]),
            "positive_stress_split_ratio_failed",
        ),
        (
            base_lcb is not None
            and float(base_lcb) > float(gates["minimum_base_lcb_bps"]),
            "base_lcb_failed",
        ),
        (
            stress_lcb is not None
            and float(stress_lcb) > float(gates["minimum_stress_lcb_bps"]),
            "stress_lcb_failed",
        ),
        (
            stress_lcb_improvement is not None
            and float(stress_lcb_improvement)
            > float(gates["minimum_stress_lcb_improvement_bps"]),
            "stress_improvement_failed",
        ),
        (
            fill_auc_mean is not None
            and float(fill_auc_mean)
            >= float(gates["minimum_treatment_fill_roc_auc"]),
            "fill_discrimination_failed",
        ),
        (
            profit_auc_mean is not None
            and float(profit_auc_mean)
            >= float(gates["minimum_treatment_profitability_roc_auc"]),
            "profitability_discrimination_failed",
        ),
        (
            profit_auc_gain_mean is not None
            and float(profit_auc_gain_mean)
            >= float(gates["minimum_profitability_roc_auc_gain"]),
            "profitability_auc_gain_failed",
        ),
        (
            positive_profit_auc_gain_ratio
            >= float(
                gates["minimum_positive_profitability_auc_gain_split_ratio"]
            ),
            "profitability_auc_gain_stability_failed",
        ),
        (
            treatment["prediction_permutation_control"].get("passed") is True,
            "subsecond_permutation_control_failed",
        ),
    )
    failures.extend(reason for passed, reason in checks if not passed)
    diagnostic["decision_gate_passed"] = not failures
    return (
        (DECISION_CONTINUE, ["subsecond_information_gate_passed"], diagnostic)
        if not failures
        else (DECISION_STOP, failures, diagnostic)
    )


def run_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = pathlib.Path(args.config).resolve()
    assessment_path = pathlib.Path(args.control_assessment).resolve()
    learnability_path = pathlib.Path(args.learnability_report).resolve()
    policy = validate_policy(config_path)
    assessment = development.validate_capture_assessment(assessment_path)
    upstream, splits = validate_upstream_report(
        learnability_path, assessment_path=assessment_path, policy=policy
    )
    if upstream.get("research_decision") != policy["upstream"][
        "required_learnability_decision"
    ]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "COMPLETE",
            "fully_verifiable": True,
            "research_domain": "forward_development_only",
            "promotion_evidence": False,
            "promotion_eligible": False,
            **_false_authorities(),
            "research_decision": DECISION_UPSTREAM_STOP,
            "reason_codes": ["maker_learnability_did_not_close"],
            "next_action": "follow_maker_learnability_decision",
        }
    series = development.load_capture_rows(assessment)
    raw_timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    baseline_features, baseline_names = development.build_causal_features(series)
    feature_policy = policy["features"]
    if len(baseline_names) != int(feature_policy["expected_baseline_feature_count"]):
        raise ValueError("baseline feature count drift")
    subsecond = load_subsecond_features(
        assessment, bucket_ms=int(feature_policy["subsecond_bucket_ms"])
    )
    subsecond_timestamps = np.asarray(subsecond["timestamp"], dtype=np.int64)
    subsecond_matrix = np.asarray(subsecond["features"], dtype=np.float64)
    subsecond_positions = {
        int(timestamp): index for index, timestamp in enumerate(subsecond_timestamps)
    }
    alignment = np.asarray(
        [subsecond_positions.get(int(timestamp), -1) for timestamp in raw_timestamps],
        dtype=np.int64,
    )
    execution = policy["execution"]
    base_cost = learnability.total_base_cost_bps(policy)
    outcomes, fill_timestamps, actions, fill_audit = maker.build_maker_action_returns(
        series,
        horizons_seconds=execution["horizons_seconds"],
        placement_latency_seconds=int(execution["placement_latency_seconds"]),
        fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
        queue_depth_multiplier=float(execution["queue_depth_multiplier"]),
        base_cost_bps=base_cost,
    )
    observable = learnability.build_observable_decision_mask(
        raw_timestamps,
        placement_latency_seconds=int(execution["placement_latency_seconds"]),
        fill_timeout_seconds=int(execution["fill_timeout_seconds"]),
        horizons_seconds=execution["horizons_seconds"],
    )
    baseline_eligible = observable & np.all(np.isfinite(baseline_features), axis=1)
    aligned = alignment >= 0
    baseline_eligible_count = int(np.sum(baseline_eligible))
    if baseline_eligible_count == 0:
        raise development.CaptureNotReady("baseline eligible rows are empty")
    aligned_ratio = float(
        np.sum(baseline_eligible & aligned) / baseline_eligible_count
    )
    if aligned_ratio < float(feature_policy["minimum_aligned_row_ratio"]):
        raise development.CaptureNotReady(
            f"subsecond aligned row ratio {aligned_ratio:.6f} < minimum"
        )
    eligible = baseline_eligible & aligned
    timestamps = raw_timestamps[eligible]
    baseline_features = baseline_features[eligible]
    subsecond_features = subsecond_matrix[alignment[eligible]]
    outcomes = outcomes[eligible]
    fill_timestamps = fill_timestamps[eligible]
    if len(timestamps) < 60000:
        raise development.CaptureNotReady("subsecond eligible rows < 60000")
    utilities = learnability.build_stress_utility_targets(
        outcomes,
        base_cost_bps=base_cost,
        stress_cost_multiplier=float(execution["stress_cost_multiplier"]),
    )
    embargo = (
        int(execution["placement_latency_seconds"])
        + int(execution["fill_timeout_seconds"])
        + max(int(value) for value in execution["horizons_seconds"])
    )
    split_policy = policy["splits"]
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
        variant_reports: Dict[str, Dict[str, Any]] = {}
        if (
            len(model_fit) < minimum
            or len(model_selection) < minimum_selection
            or len(validation) < minimum
            or len(test) < minimum
        ):
            variant_reports = {
                variant_id: {
                    "status": "insufficient_rows",
                    "reason": json.dumps(row_counts, sort_keys=True),
                    "promotion_evidence": False,
                    "promotion_eligible": False,
                }
                for variant_id in VARIANT_IDS
            }
        else:
            for variant_id in VARIANT_IDS:
                try:
                    variant_reports[variant_id] = evaluate_variant_split(
                        variant_id=variant_id,
                        split_id=int(split.split_id),
                        fit_baseline=baseline_features[model_fit],
                        fit_subsecond=subsecond_features[model_fit],
                        fit_outcomes=outcomes[model_fit],
                        fit_utilities=utilities[model_fit],
                        fit_fills=fill_timestamps[model_fit],
                        selection_baseline=baseline_features[model_selection],
                        selection_subsecond=subsecond_features[model_selection],
                        selection_outcomes=outcomes[model_selection],
                        selection_utilities=utilities[model_selection],
                        selection_fills=fill_timestamps[model_selection],
                        validation_baseline=baseline_features[validation],
                        validation_subsecond=subsecond_features[validation],
                        validation_timestamps=timestamps[validation],
                        validation_outcomes=outcomes[validation],
                        validation_utilities=utilities[validation],
                        validation_fills=fill_timestamps[validation],
                        test_baseline=baseline_features[test],
                        test_subsecond=subsecond_features[test],
                        test_timestamps=timestamps[test],
                        test_outcomes=outcomes[test],
                        test_utilities=utilities[test],
                        test_fills=fill_timestamps[test],
                        actions=actions,
                        policy=policy,
                    )
                except Exception as exc:
                    variant_reports[variant_id] = {
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
                "architectures": variant_reports,
            }
        )
    control = policy["negative_control"]
    comparison = development.aggregate_target_architecture_comparison(
        split_reports=split_reports,
        architecture_ids=VARIANT_IDS,
        required_split_count=int(split_policy["count"]),
        permutation_trials=int(control["permutation_trials"]),
        permutation_seed=int(control["permutation_seed"]),
        permutation_minimum_excess_lcb_bps=float(
            control["minimum_excess_lcb_bps"]
        ),
        frozen_contract_failures=[],
    )
    decision, reason_codes, diagnostics = add_decision_gates(
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
            "learnability_report_path": str(learnability_path),
            "learnability_report_sha256": common.sha256_file(learnability_path),
            "learnability_decision": upstream["research_decision"],
        },
        "data": {
            "raw_one_second_row_count": int(len(raw_timestamps)),
            "baseline_eligible_row_count": int(np.sum(baseline_eligible)),
            "subsecond_aligned_eligible_row_count": int(len(timestamps)),
            "subsecond_aligned_row_ratio": aligned_ratio,
            "baseline_feature_count": len(baseline_names),
            "subsecond_feature_count": len(SUBSECOND_FEATURE_NAMES),
            "subsecond_feature_names": list(SUBSECOND_FEATURE_NAMES),
            "subsecond_feature_names_sha256": common.canonical_sha256(
                {"feature_names": list(SUBSECOND_FEATURE_NAMES)}
            ),
            "eligible_timestamp_sha256": common.array_sha256(timestamps),
            "raw_replay_audit": subsecond["audit"],
        },
        "feature_contract": {
            "baseline": development.CAUSAL_FEATURE_CONTRACT,
            "subsecond_revision": feature_policy[
                "subsecond_feature_contract_revision"
            ],
            "decision_time": "end_of_exchange_second_after_four_complete_quarters",
            "raw_capture_checksum_required": True,
            "fill_proxy_used_as_model_feature": False,
            "future_values_permitted": False,
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
        "incremental_information_diagnostics": diagnostics,
        "split_reports": split_reports,
        "research_decision": decision,
        "reason_codes": reason_codes,
        "next_action": (
            "freeze_subsecond_candidate_and_collect_independent_forward_window"
            if decision == DECISION_CONTINUE
            else "close_current_maker_information_set_and_change_payoff_or_horizon"
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
        "next_action": "fix_input_or_complete_checksum_bound_raw_capture",
        "experiment_policy": {"path": str(pathlib.Path(args.config).resolve())},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-assessment", required=True)
    parser.add_argument("--learnability-report", required=True)
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
    except development.CaptureNotReady as exc:
        report = not_ready_report(args, f"capture_not_ready:{exc}")
    except Exception as exc:
        report = not_ready_report(args, f"invalid_input:{type(exc).__name__}:{exc}")
    common.atomic_write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report.get("status") == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
