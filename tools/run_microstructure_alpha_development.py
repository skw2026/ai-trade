#!/usr/bin/env python3
"""Development-only cost-aware order-book/trade-flow economic screen.

The probe consumes only the checksum-bound segment manifest emitted by
``assess_microstructure_capture.py``.  It learns the direction and exit horizon
jointly: every output is one (long|short, holding horizon) action whose label is
the executable quote-to-quote return after explicit fees/slippage.  Thresholds
are selected on a purged nested validation window and evaluated on disjoint
forward OOS windows.  A PASS here is development evidence only and can never be
used as promotion or final-holdout evidence.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import math
import pathlib
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np

try:
    import catboost
    from catboost import CatBoostRegressor
except ImportError:  # pragma: no cover - exercised by the research image
    catboost = None
    CatBoostRegressor = None


SCHEMA_VERSION = "microstructure_alpha_development_v2"
ASSESSMENT_SCHEMA_VERSION = "microstructure_capture_assessment_v1"
CAPTURE_MERGE_CONTRACT = {
    "method": "drop_shared_adjacent_boundary_buckets_v1",
    "segment_order": "strictly_chronological_manifest_order",
    "allowed_duplicate_scope": "exact_shared_endpoint_of_two_adjacent_segments_only",
    "boundary_action": "drop_entire_shared_one_second_bucket",
    "non_boundary_action": "fail_closed",
    "maximum_segments_per_boundary": 2,
}
REQUIRED_FIELDS = (
    "timestamp",
    "best_bid",
    "best_ask",
    "mid",
    "spread_bps",
    "microprice",
    "book_imbalance_l1",
    "book_imbalance_l5",
    "book_imbalance_l20",
    "depth_slope",
    "book_update_count",
    "trade_count",
    "buy_quote_volume",
    "sell_quote_volume",
    "trade_imbalance",
)


class CaptureNotReady(RuntimeError):
    """The immutable forward-development capture has not passed readiness."""


@dataclasses.dataclass(frozen=True)
class TimeSplit:
    split_id: int
    fit_start_ms: int
    fit_end_ms: int
    validation_start_ms: int
    validation_end_ms: int
    test_start_ms: int
    test_end_ms: int


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json_object(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON payload is not an object: {path}")
    return payload


def validate_capture_assessment(path: pathlib.Path) -> Dict[str, Any]:
    payload = read_json_object(path)
    failures: List[str] = []
    if payload.get("schema_version") != ASSESSMENT_SCHEMA_VERSION:
        failures.append("capture assessment schema mismatch")
    if payload.get("status") != "PASS" or payload.get("development_screen_ready") is not True:
        failures.append("capture assessment has not passed the 24h readiness gate")
    if payload.get("research_domain") != "forward_development_only":
        failures.append("capture assessment is not forward-development-only")
    if payload.get("promotion_evidence") is not False or payload.get("promotion_eligible") is not False:
        failures.append("capture assessment promotion isolation contract failed")
    try:
        coverage_ms = int(payload.get("coverage_ms") or 0)
        minimum_coverage_ms = int(payload.get("minimum_coverage_ms") or 0)
        latest_timestamp_ms = int(payload.get("latest_exchange_timestamp_ms") or 0)
    except (TypeError, ValueError):
        coverage_ms = minimum_coverage_ms = latest_timestamp_ms = 0
    if (
        minimum_coverage_ms <= 0
        or coverage_ms < minimum_coverage_ms
        or latest_timestamp_ms <= 0
    ):
        failures.append("capture assessment coverage/timestamp contract failed")
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        failures.append("capture assessment has no checksum-bound segment manifest")
    if failures:
        raise CaptureNotReady("; ".join(failures))
    return payload


def _row_values(row: Mapping[str, str]) -> Tuple[float, ...]:
    values: List[float] = []
    for name in REQUIRED_FIELDS[1:]:
        value = float(row[name])
        if not math.isfinite(value):
            raise ValueError(f"non-finite microstructure value: {name}")
        values.append(value)
    return tuple(values)


def validate_capture_merge_audit(audit: Any) -> Dict[str, Any]:
    if not isinstance(audit, dict):
        raise ValueError("capture merge audit is missing")
    try:
        input_segments = int(audit.get("input_segment_count", 0))
        manifest_rows = int(audit.get("manifest_feature_row_count", -1))
        output_rows = int(audit.get("output_feature_row_count", -1))
        dropped = int(audit.get("dropped_boundary_bucket_count", -1))
        shared = int(audit.get("shared_adjacent_boundary_bucket_count", -1))
        conflicting = int(audit.get("conflicting_shared_boundary_bucket_count", -1))
        identical = int(audit.get("identical_shared_boundary_bucket_count", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError("capture merge audit counts are invalid") from exc
    timestamp_hash = str(audit.get("dropped_boundary_timestamps_sha256") or "")
    hash_valid = len(timestamp_hash) == 64
    if hash_valid:
        try:
            int(timestamp_hash, 16)
        except ValueError:
            hash_valid = False
    first_dropped = audit.get("first_dropped_boundary_timestamp_ms")
    last_dropped = audit.get("last_dropped_boundary_timestamp_ms")
    boundary_range_valid = bool(
        (dropped == 0 and first_dropped is None and last_dropped is None)
        or (
            dropped > 0
            and isinstance(first_dropped, int)
            and isinstance(last_dropped, int)
            and 0 <= first_dropped <= last_dropped
        )
    )
    if not (
        audit.get("method") == CAPTURE_MERGE_CONTRACT["method"]
        and input_segments > 0
        and manifest_rows > 0
        and output_rows > 0
        and dropped >= 0
        and shared == dropped
        and conflicting >= 0
        and identical >= 0
        and conflicting + identical == dropped
        and manifest_rows - output_rows == 2 * dropped
        and hash_valid
        and boundary_range_valid
    ):
        raise ValueError("capture merge audit contract failed")
    return audit


def load_capture_rows(assessment: Mapping[str, Any]) -> Dict[str, Any]:
    """Load attested features and deterministically remove partial restart buckets."""
    manifest = assessment.get("segments", [])
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("capture segment manifest is empty")
    segment_bounds: List[Tuple[int, int]] = []
    for item in manifest:
        if not isinstance(item, dict):
            raise ValueError("capture segment manifest item is not an object")
        try:
            first = int(item.get("first_timestamp_ms", -1))
            last = int(item.get("last_timestamp_ms", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("capture segment manifest timestamp is invalid") from exc
        if first < 0 or last < first:
            raise ValueError("capture segment manifest interval is invalid")
        if segment_bounds:
            previous_first, previous_last = segment_bounds[-1]
            if first < previous_first:
                raise ValueError("capture segment manifest is not chronological")
            if first < previous_last:
                raise ValueError(
                    "non-boundary capture segment overlap: "
                    f"previous_last={previous_last} current_first={first}"
                )
        segment_bounds.append((first, last))

    by_timestamp: Dict[int, Tuple[float, ...]] = {}
    owner_by_timestamp: Dict[int, int] = {}
    dropped_boundaries: set[int] = set()
    conflicting_boundary_count = 0
    identical_boundary_count = 0
    manifest_feature_row_count = 0
    for segment_index, item in enumerate(manifest):
        path = pathlib.Path(str(item.get("feature_path") or ""))
        expected_hash = str(item.get("feature_sha256") or "")
        if not path.is_file() or len(expected_hash) != 64:
            raise ValueError(f"capture feature artifact is missing: {path}")
        if sha256_file(path) != expected_hash:
            raise ValueError(f"capture feature checksum mismatch: {path}")
        segment_count = 0
        first_timestamp = None
        last_timestamp = None
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            missing = sorted(set(REQUIRED_FIELDS) - fields)
            if missing:
                raise ValueError(f"capture feature CSV missing columns {missing}: {path}")
            for row in reader:
                timestamp = int(row["timestamp"])
                if last_timestamp is not None and timestamp <= last_timestamp:
                    raise ValueError(
                        f"capture feature timestamps are not strictly increasing: {path}"
                    )
                values = _row_values(row)
                if values[0] <= 0.0 or values[1] <= values[0] or values[2] <= 0.0:
                    raise ValueError(f"invalid executable quote at timestamp={timestamp}")
                if timestamp in dropped_boundaries:
                    raise ValueError(
                        f"capture boundary timestamp appears in more than two segments: {timestamp}"
                    )
                previous = by_timestamp.get(timestamp)
                if previous is not None:
                    previous_owner = owner_by_timestamp[timestamp]
                    previous_last = segment_bounds[previous_owner][1]
                    current_first = segment_bounds[segment_index][0]
                    exact_adjacent_boundary = bool(
                        previous_owner == segment_index - 1
                        and previous_last == timestamp
                        and current_first == timestamp
                    )
                    if not exact_adjacent_boundary:
                        raise ValueError(
                            f"non-boundary duplicate feature row at timestamp={timestamp}"
                        )
                    if previous == values:
                        identical_boundary_count += 1
                    else:
                        conflicting_boundary_count += 1
                    # A restart can leave two independently aggregated partial
                    # representations of the same second.  Neither row is a
                    # lossless full bucket, so choosing either one would inject
                    # a segment-order-dependent feature.  Remove the entire
                    # shared bucket and retain a checksum-bound audit below.
                    del by_timestamp[timestamp]
                    dropped_boundaries.add(timestamp)
                else:
                    by_timestamp[timestamp] = values
                    owner_by_timestamp[timestamp] = segment_index
                segment_count += 1
                first_timestamp = timestamp if first_timestamp is None else first_timestamp
                last_timestamp = timestamp
        if segment_count != int(item.get("feature_row_count", -1)):
            raise ValueError(f"capture feature row-count mismatch: {path}")
        if first_timestamp != int(item.get("first_timestamp_ms", -1)):
            raise ValueError(f"capture first timestamp mismatch: {path}")
        if last_timestamp != int(item.get("last_timestamp_ms", -1)):
            raise ValueError(f"capture last timestamp mismatch: {path}")
        manifest_feature_row_count += segment_count
    if not by_timestamp:
        raise ValueError("capture manifest produced no feature rows")
    timestamps = np.asarray(sorted(by_timestamp), dtype=np.int64)
    matrix = np.asarray([by_timestamp[int(ts)] for ts in timestamps], dtype=np.float64)
    dropped_sorted = sorted(dropped_boundaries)
    merge_audit = {
        "method": CAPTURE_MERGE_CONTRACT["method"],
        "input_segment_count": len(manifest),
        "manifest_feature_row_count": manifest_feature_row_count,
        "shared_adjacent_boundary_bucket_count": len(dropped_sorted),
        "conflicting_shared_boundary_bucket_count": conflicting_boundary_count,
        "identical_shared_boundary_bucket_count": identical_boundary_count,
        "dropped_boundary_bucket_count": len(dropped_sorted),
        "dropped_boundary_timestamps_sha256": canonical_sha256(
            {"timestamps_ms": dropped_sorted}
        ),
        "first_dropped_boundary_timestamp_ms": (
            dropped_sorted[0] if dropped_sorted else None
        ),
        "last_dropped_boundary_timestamp_ms": (
            dropped_sorted[-1] if dropped_sorted else None
        ),
        "output_feature_row_count": len(timestamps),
    }
    validate_capture_merge_audit(merge_audit)
    return {
        "timestamp": timestamps,
        "capture_merge_audit": merge_audit,
        **{
            name: matrix[:, index]
            for index, name in enumerate(REQUIRED_FIELDS[1:])
        },
    }


def exact_lag(values: np.ndarray, timestamps: np.ndarray, lag_seconds: int) -> np.ndarray:
    output = np.full(len(values), np.nan, dtype=np.float64)
    positions = {int(timestamp): index for index, timestamp in enumerate(timestamps)}
    lag_ms = int(lag_seconds) * 1000
    for index, timestamp in enumerate(timestamps):
        previous = positions.get(int(timestamp) - lag_ms)
        if previous is not None:
            output[index] = float(values[previous])
    return output


def build_causal_features(series: Mapping[str, np.ndarray]) -> Tuple[np.ndarray, List[str]]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    mid = np.asarray(series["mid"], dtype=np.float64)
    microprice = np.asarray(series["microprice"], dtype=np.float64)
    imbalance_l1 = np.asarray(series["book_imbalance_l1"], dtype=np.float64)
    imbalance_l5 = np.asarray(series["book_imbalance_l5"], dtype=np.float64)
    imbalance_l20 = np.asarray(series["book_imbalance_l20"], dtype=np.float64)
    trade_imbalance = np.asarray(series["trade_imbalance"], dtype=np.float64)
    micro_dislocation = (microprice / mid - 1.0) * 10000.0
    names: List[str] = []
    arrays: List[np.ndarray] = []

    def add(name: str, values: Iterable[float]) -> None:
        names.append(name)
        arrays.append(np.asarray(values, dtype=np.float64))

    add("micro_spread_bps", series["spread_bps"])
    add("micro_microprice_dislocation_bps", micro_dislocation)
    add("micro_book_imbalance_l1", imbalance_l1)
    add("micro_book_imbalance_l5", imbalance_l5)
    add("micro_book_imbalance_l20", imbalance_l20)
    add("micro_depth_slope", series["depth_slope"])
    add("micro_trade_imbalance", trade_imbalance)
    add("micro_log_book_updates", np.log1p(series["book_update_count"]))
    add("micro_log_trade_count", np.log1p(series["trade_count"]))
    add("micro_log_buy_quote_volume", np.log1p(series["buy_quote_volume"]))
    add("micro_log_sell_quote_volume", np.log1p(series["sell_quote_volume"]))
    for lag in (1, 5, 20, 60):
        lag_mid = exact_lag(mid, timestamps, lag)
        lag_micro = exact_lag(micro_dislocation, timestamps, lag)
        lag_l1 = exact_lag(imbalance_l1, timestamps, lag)
        lag_l5 = exact_lag(imbalance_l5, timestamps, lag)
        lag_trade = exact_lag(trade_imbalance, timestamps, lag)
        add(f"micro_mid_return_{lag}s", mid / lag_mid - 1.0)
        add(f"micro_dislocation_delta_{lag}s", micro_dislocation - lag_micro)
        add(f"micro_book_l1_delta_{lag}s", imbalance_l1 - lag_l1)
        add(f"micro_book_l5_delta_{lag}s", imbalance_l5 - lag_l5)
        add(f"micro_trade_imbalance_delta_{lag}s", trade_imbalance - lag_trade)
    day_phase = np.mod(timestamps, 86_400_000) / 86_400_000.0
    add("micro_time_day_sin", np.sin(2.0 * math.pi * day_phase))
    add("micro_time_day_cos", np.cos(2.0 * math.pi * day_phase))
    return np.column_stack(arrays), names


def build_joint_action_returns(
    series: Mapping[str, np.ndarray],
    *,
    horizons_seconds: Sequence[int],
    execution_latency_seconds: int,
    additional_round_trip_cost_bps: float,
) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
    """Return executable long/short outcomes for each candidate exit horizon."""
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    best_bid = np.asarray(series["best_bid"], dtype=np.float64)
    best_ask = np.asarray(series["best_ask"], dtype=np.float64)
    positions = {int(timestamp): index for index, timestamp in enumerate(timestamps)}
    actions = [
        {"direction": direction, "horizon_seconds": int(horizon)}
        for direction in ("long", "short")
        for horizon in horizons_seconds
    ]
    outcomes = np.full((len(timestamps), len(actions)), np.nan, dtype=np.float64)
    latency_ms = int(execution_latency_seconds) * 1000
    for row_index, timestamp in enumerate(timestamps):
        entry_index = positions.get(int(timestamp) + latency_ms)
        if entry_index is None:
            continue
        for action_index, action in enumerate(actions):
            exit_index = positions.get(
                int(timestamp) + latency_ms + int(action["horizon_seconds"]) * 1000
            )
            if exit_index is None:
                continue
            if action["direction"] == "long":
                gross_bps = (
                    best_bid[exit_index] / best_ask[entry_index] - 1.0
                ) * 10000.0
            else:
                gross_bps = (
                    best_bid[entry_index] / best_ask[exit_index] - 1.0
                ) * 10000.0
            outcomes[row_index, action_index] = (
                gross_bps - float(additional_round_trip_cost_bps)
            )
    return outcomes, actions


def build_time_splits(
    timestamps: np.ndarray,
    *,
    n_splits: int,
    train_window_seconds: int,
    validation_window_seconds: int,
    test_window_seconds: int,
    rolling_step_seconds: int,
    embargo_seconds: int,
) -> List[TimeSplit]:
    if rolling_step_seconds < test_window_seconds:
        raise ValueError("overlapping OOS test windows are forbidden")
    if validation_window_seconds >= train_window_seconds:
        raise ValueError("validation window must be smaller than train window")
    latest_end = int(np.max(timestamps)) + 1000
    first_test_start = latest_end - (
        (n_splits - 1) * rolling_step_seconds + test_window_seconds
    ) * 1000
    splits: List[TimeSplit] = []
    for split_id in range(n_splits):
        test_start = first_test_start + split_id * rolling_step_seconds * 1000
        test_end = test_start + test_window_seconds * 1000
        validation_end = test_start - embargo_seconds * 1000
        validation_start = validation_end - validation_window_seconds * 1000
        fit_end = validation_start - embargo_seconds * 1000
        fit_start = validation_end - train_window_seconds * 1000
        splits.append(
            TimeSplit(
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


def t_critical_975(sample_count: int) -> float:
    # Conservative two-sided 95% Student-t values; linear interpolation is not
    # needed for a gate whose only use is a lower confidence bound.
    table = {
        2: 12.706,
        3: 4.303,
        4: 3.182,
        5: 2.776,
        6: 2.571,
        7: 2.447,
        8: 2.365,
        9: 2.306,
        10: 2.262,
        12: 2.201,
        15: 2.145,
        20: 2.093,
        30: 2.045,
        60: 2.000,
    }
    if sample_count <= 1:
        return float("inf")
    eligible = [key for key in table if key <= sample_count]
    return table[max(eligible)] if eligible else 1.96


def summarize_edges(values: Sequence[float]) -> Dict[str, Any]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return {"count": 0, "mean_bps": None, "stdev_bps": None, "lcb_bps": None}
    mean = statistics.fmean(finite)
    stdev = statistics.stdev(finite) if len(finite) > 1 else float("inf")
    lcb = (
        mean - t_critical_975(len(finite)) * stdev / math.sqrt(len(finite))
        if math.isfinite(stdev)
        else float("-inf")
    )
    return {
        "count": len(finite),
        "mean_bps": mean,
        "stdev_bps": stdev if math.isfinite(stdev) else None,
        "lcb_bps": lcb if math.isfinite(lcb) else None,
        "positive_ratio": sum(value > 0.0 for value in finite) / len(finite),
    }


def summarize_score_distribution(values: Sequence[float]) -> Dict[str, Any]:
    finite = np.asarray(
        [float(value) for value in values if math.isfinite(float(value))],
        dtype=np.float64,
    )
    if len(finite) == 0:
        return {"count": 0, "minimum_bps": None, "maximum_bps": None, "quantiles_bps": {}}
    return {
        "count": int(len(finite)),
        "minimum_bps": float(np.min(finite)),
        "maximum_bps": float(np.max(finite)),
        "quantiles_bps": {
            str(quantile): float(np.quantile(finite, quantile))
            for quantile in (0.5, 0.8, 0.9, 0.95, 0.98)
        },
    }


def fit_stress_profitability_transform(
    outcomes: np.ndarray,
    *,
    base_cost_bps: float,
    stress_cost_multiplier: float,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Build a fit-domain-only target that learns economically useful tails.

    Raw one-second forward returns are dominated by the many observations that
    cannot recover spread, fees, and slippage.  A joint MultiRMSE model trained
    directly on those returns therefore tends to minimize loss with an almost
    constant prediction.  For each action we instead standardize the indicator
    that its *stressed-cost* return is positive.  Standardization gives every
    action unit target variance without using validation/test observations.

    The model output is not trusted as PnL.  ``reconstruct_base_net_scores``
    converts it back to a fit-domain conditional-mean proxy in bps, and every
    threshold is still accepted only by realized raw base/stress returns in the
    nested and untouched future gates.
    """
    matrix = np.asarray(outcomes, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] < 1 or matrix.shape[1] < 1:
        raise ValueError("stress-profitability transform requires a non-empty matrix")
    if not np.all(np.isfinite(matrix)):
        raise ValueError("stress-profitability transform outcomes must be finite")
    base_cost = float(base_cost_bps)
    multiplier = float(stress_cost_multiplier)
    if not math.isfinite(base_cost) or base_cost <= 0.0:
        raise ValueError("stress-profitability transform base cost must be positive")
    if not math.isfinite(multiplier) or multiplier <= 1.0:
        raise ValueError("stress-profitability transform multiplier must exceed one")

    stress_increment = base_cost * (multiplier - 1.0)
    profitable = matrix > stress_increment
    transformed = np.zeros_like(matrix, dtype=np.float64)
    action_statistics: List[Dict[str, Any]] = []
    for action_index in range(matrix.shape[1]):
        labels = profitable[:, action_index].astype(np.float64)
        positive_count = int(np.sum(labels))
        nonpositive_count = int(len(labels) - positive_count)
        positive_rate = float(positive_count / len(labels))
        scale = math.sqrt(positive_rate * (1.0 - positive_rate))
        learnable = bool(positive_count > 0 and nonpositive_count > 0 and scale > 0.0)
        if learnable:
            transformed[:, action_index] = (labels - positive_rate) / scale
        positive_values = matrix[profitable[:, action_index], action_index]
        nonpositive_values = matrix[~profitable[:, action_index], action_index]
        # A missing class is explicitly non-learnable.  Its fallback mean keeps
        # inference finite and constant; future economics can never manufacture
        # evidence from a class that was absent in the fit domain.
        positive_mean = (
            float(np.mean(positive_values))
            if len(positive_values)
            else float(np.mean(matrix[:, action_index]))
        )
        nonpositive_mean = (
            float(np.mean(nonpositive_values))
            if len(nonpositive_values)
            else float(np.mean(matrix[:, action_index]))
        )
        action_statistics.append(
            {
                "action_index": action_index,
                "row_count": int(len(labels)),
                "positive_count": positive_count,
                "nonpositive_count": nonpositive_count,
                "positive_rate": positive_rate,
                "standardization_scale": scale,
                "positive_mean_base_net_bps": positive_mean,
                "nonpositive_mean_base_net_bps": nonpositive_mean,
                "learnable": learnable,
            }
        )
    return transformed, {
        "method": "fit_only_standardized_stress_profitability_v1",
        "profitability_hurdle": "base_net_return_bps_gt_stress_incremental_cost_bps",
        "stress_incremental_cost_bps": stress_increment,
        "inference_reconstruction": "clipped_probability_times_fit_class_conditional_base_net_means",
        "validation_or_test_statistics_used": False,
        "action_statistics": action_statistics,
    }


def validate_stress_profitability_transform(
    transform: Mapping[str, Any],
    *,
    action_count: int,
    expected_row_count: int | None = None,
) -> List[Dict[str, Any]]:
    if not (
        isinstance(transform, dict)
        and transform.get("method")
        == "fit_only_standardized_stress_profitability_v1"
        and transform.get("profitability_hurdle")
        == "base_net_return_bps_gt_stress_incremental_cost_bps"
        and transform.get("inference_reconstruction")
        == "clipped_probability_times_fit_class_conditional_base_net_means"
        and transform.get("validation_or_test_statistics_used") is False
    ):
        raise ValueError("stress-profitability transform contract is invalid")
    hurdle = float(transform.get("stress_incremental_cost_bps"))
    items = transform.get("action_statistics")
    if (
        not math.isfinite(hurdle)
        or hurdle <= 0.0
        or not isinstance(items, list)
        or len(items) != int(action_count)
    ):
        raise ValueError("stress-profitability transform shape/hurdle is invalid")
    for action_index, item in enumerate(items):
        if not isinstance(item, dict) or int(item.get("action_index", -1)) != action_index:
            raise ValueError("stress-profitability action statistics are invalid")
        try:
            row_count = int(item.get("row_count"))
            positive_count = int(item.get("positive_count"))
            nonpositive_count = int(item.get("nonpositive_count"))
            rate = float(item.get("positive_rate"))
            scale = float(item.get("standardization_scale"))
            positive_mean = float(item.get("positive_mean_base_net_bps"))
            nonpositive_mean = float(item.get("nonpositive_mean_base_net_bps"))
        except (TypeError, ValueError) as exc:
            raise ValueError("stress-profitability action statistic type is invalid") from exc
        expected_rate = positive_count / row_count if row_count > 0 else float("nan")
        expected_scale = (
            math.sqrt(expected_rate * (1.0 - expected_rate))
            if math.isfinite(expected_rate) and 0.0 <= expected_rate <= 1.0
            else float("nan")
        )
        expected_learnable = positive_count > 0 and nonpositive_count > 0
        if not (
            row_count > 0
            and positive_count >= 0
            and nonpositive_count >= 0
            and positive_count + nonpositive_count == row_count
            and (expected_row_count is None or row_count == int(expected_row_count))
            and math.isfinite(rate)
            and math.isclose(rate, expected_rate, rel_tol=0.0, abs_tol=1e-15)
            and math.isfinite(scale)
            and math.isclose(scale, expected_scale, rel_tol=0.0, abs_tol=1e-15)
            and math.isfinite(positive_mean)
            and math.isfinite(nonpositive_mean)
            and item.get("learnable") is expected_learnable
            and (positive_count == 0 or positive_mean > hurdle)
            and (nonpositive_count == 0 or nonpositive_mean <= hurdle)
        ):
            raise ValueError("stress-profitability action statistic contract failed")
    return items


def transform_stress_profitability_targets(
    outcomes: np.ndarray, transform: Mapping[str, Any]
) -> np.ndarray:
    """Apply fit-only normalization to another domain without refitting it."""
    matrix = np.asarray(outcomes, dtype=np.float64)
    if matrix.ndim != 2:
        raise ValueError("stress-profitability target transform shape mismatch")
    statistics_by_action = validate_stress_profitability_transform(
        transform, action_count=matrix.shape[1]
    )
    hurdle = float(transform.get("stress_incremental_cost_bps"))
    if not math.isfinite(hurdle):
        raise ValueError("stress-profitability target hurdle is invalid")
    result = np.zeros_like(matrix, dtype=np.float64)
    for action_index, item in enumerate(statistics_by_action):
        if not isinstance(item, dict) or int(item.get("action_index", -1)) != action_index:
            raise ValueError("stress-profitability action statistics are invalid")
        rate = float(item.get("positive_rate"))
        scale = float(item.get("standardization_scale"))
        if not (0.0 <= rate <= 1.0) or not math.isfinite(scale) or scale < 0.0:
            raise ValueError("stress-profitability action normalization is invalid")
        if item.get("learnable") is True:
            if scale <= 0.0:
                raise ValueError("learnable stress-profitability action has zero scale")
            labels = (matrix[:, action_index] > hurdle).astype(np.float64)
            result[:, action_index] = (labels - rate) / scale
    return result


def reconstruct_base_net_scores(
    raw_prediction: np.ndarray, transform: Mapping[str, Any]
) -> np.ndarray:
    """Map standardized profitability predictions to a bps-scale policy score."""
    prediction = np.asarray(raw_prediction, dtype=np.float64)
    if prediction.ndim == 1:
        prediction = prediction.reshape(-1, 1)
    if prediction.ndim != 2:
        raise ValueError("stress-profitability prediction transform shape mismatch")
    statistics_by_action = validate_stress_profitability_transform(
        transform, action_count=prediction.shape[1]
    )
    result = np.empty_like(prediction, dtype=np.float64)
    for action_index, item in enumerate(statistics_by_action):
        if not isinstance(item, dict) or int(item.get("action_index", -1)) != action_index:
            raise ValueError("stress-profitability prediction statistics are invalid")
        rate = float(item.get("positive_rate"))
        scale = float(item.get("standardization_scale"))
        positive_mean = float(item.get("positive_mean_base_net_bps"))
        nonpositive_mean = float(item.get("nonpositive_mean_base_net_bps"))
        if not all(
            math.isfinite(value)
            for value in (rate, scale, positive_mean, nonpositive_mean)
        ) or not (0.0 <= rate <= 1.0 and scale >= 0.0):
            raise ValueError("stress-profitability prediction statistics are non-finite")
        implied_probability = np.clip(
            rate + prediction[:, action_index] * scale, 0.0, 1.0
        )
        result[:, action_index] = (
            implied_probability * positive_mean
            + (1.0 - implied_probability) * nonpositive_mean
        )
    if not np.all(np.isfinite(result)):
        raise ValueError("reconstructed base-net policy score is non-finite")
    return result


def evaluate_joint_policy(
    *,
    timestamps: np.ndarray,
    prediction: np.ndarray,
    realized_base: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    threshold_bps: float,
    base_cost_bps: float,
    stress_cost_multiplier: float,
    execution_latency_seconds: int,
) -> Dict[str, Any]:
    base_edges: List[float] = []
    stress_edges: List[float] = []
    action_counts: Dict[str, int] = {}
    next_allowed_ms = -1
    for index in range(len(timestamps)):
        timestamp = int(timestamps[index])
        if timestamp < next_allowed_ms:
            continue
        row_prediction = np.asarray(prediction[index], dtype=np.float64)
        if not np.all(np.isfinite(row_prediction)):
            continue
        action_index = int(np.argmax(row_prediction))
        predicted_edge = float(row_prediction[action_index])
        if predicted_edge < threshold_bps:
            continue
        realized = float(realized_base[index, action_index])
        if not math.isfinite(realized):
            continue
        action = actions[action_index]
        horizon = int(action["horizon_seconds"])
        key = f"{action['direction']}_{horizon}s"
        action_counts[key] = action_counts.get(key, 0) + 1
        base_edges.append(realized)
        stress_edges.append(
            realized - float(base_cost_bps) * (float(stress_cost_multiplier) - 1.0)
        )
        next_allowed_ms = timestamp + (
            int(execution_latency_seconds) + horizon
        ) * 1000
    return {
        "threshold_bps": threshold_bps if math.isfinite(threshold_bps) else None,
        "base_cost": summarize_edges(base_edges),
        "stress_cost": summarize_edges(stress_edges),
        "action_counts": action_counts,
        "base_edges_bps": base_edges,
        "stress_edges_bps": stress_edges,
    }


def evaluate_prediction_permutation_controls(
    *,
    timestamps: np.ndarray,
    prediction: np.ndarray,
    realized_base: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    threshold_bps: float,
    base_cost_bps: float,
    stress_cost_multiplier: float,
    execution_latency_seconds: int,
    trials: int,
    seed: int,
) -> List[Dict[str, Any]]:
    """Destroy prediction/outcome timing while preserving score/action marginals."""
    if trials <= 0:
        raise ValueError("permutation control trials must be positive")
    rng = np.random.default_rng(int(seed))
    controls: List[Dict[str, Any]] = []
    for trial in range(int(trials)):
        permutation = rng.permutation(len(prediction))
        report = evaluate_joint_policy(
            timestamps=timestamps,
            prediction=np.asarray(prediction)[permutation],
            realized_base=realized_base,
            actions=actions,
            threshold_bps=threshold_bps,
            base_cost_bps=base_cost_bps,
            stress_cost_multiplier=stress_cost_multiplier,
            execution_latency_seconds=execution_latency_seconds,
        )
        report.pop("base_edges_bps", None)
        report.pop("stress_edges_bps", None)
        report["trial"] = trial
        controls.append(report)
    return controls


def summarize_prediction_permutation_controls(
    *,
    base_means_by_trial: Sequence[Sequence[float]],
    stress_means_by_trial: Sequence[Sequence[float]],
    required_split_count: int,
    candidate_base_split_lcb_bps: float | None,
    candidate_stress_split_lcb_bps: float | None,
    minimum_excess_lcb_bps: float,
    seed: int,
) -> Dict[str, Any]:
    trial_summaries: List[Dict[str, Any]] = []
    fully_verifiable = bool(required_split_count > 0)
    for trial, (base_values, stress_values) in enumerate(
        zip(base_means_by_trial, stress_means_by_trial)
    ):
        base = summarize_edges(base_values)
        stress = summarize_edges(stress_values)
        complete = bool(
            base["count"] == required_split_count
            and stress["count"] == required_split_count
        )
        fully_verifiable = fully_verifiable and complete
        trial_summaries.append(
            {
                "trial": trial,
                "complete": complete,
                "base_cost_by_split": base,
                "stress_cost_by_split": stress,
            }
        )
    if not trial_summaries:
        fully_verifiable = False
    finite_base_lcbs = [
        float(item["base_cost_by_split"]["lcb_bps"])
        for item in trial_summaries
        if item["base_cost_by_split"]["lcb_bps"] is not None
    ]
    finite_stress_lcbs = [
        float(item["stress_cost_by_split"]["lcb_bps"])
        for item in trial_summaries
        if item["stress_cost_by_split"]["lcb_bps"] is not None
    ]
    maximum_base_lcb = max(finite_base_lcbs, default=float("inf"))
    maximum_stress_lcb = max(finite_stress_lcbs, default=float("inf"))
    required_base_lcb = max(0.0, maximum_base_lcb) + float(
        minimum_excess_lcb_bps
    )
    required_stress_lcb = max(0.0, maximum_stress_lcb) + float(
        minimum_excess_lcb_bps
    )
    passed = bool(
        fully_verifiable
        and candidate_base_split_lcb_bps is not None
        and candidate_stress_split_lcb_bps is not None
        and float(candidate_base_split_lcb_bps) > required_base_lcb
        and float(candidate_stress_split_lcb_bps) > required_stress_lcb
    )
    return {
        "method": "deterministic_oos_prediction_time_permutation",
        "contract": (
            "preserve_prediction_score_and_action_marginals_destroy_feature_outcome_timing"
        ),
        "seed": int(seed),
        "trial_count": len(trial_summaries),
        "required_split_count": int(required_split_count),
        "minimum_excess_lcb_bps": float(minimum_excess_lcb_bps),
        "fully_verifiable": fully_verifiable,
        "passed": passed,
        "maximum_control_base_split_lcb_bps": (
            maximum_base_lcb if math.isfinite(maximum_base_lcb) else None
        ),
        "maximum_control_stress_split_lcb_bps": (
            maximum_stress_lcb if math.isfinite(maximum_stress_lcb) else None
        ),
        "required_candidate_base_split_lcb_bps": (
            required_base_lcb if math.isfinite(required_base_lcb) else None
        ),
        "required_candidate_stress_split_lcb_bps": (
            required_stress_lcb if math.isfinite(required_stress_lcb) else None
        ),
        "candidate_base_split_lcb_bps": candidate_base_split_lcb_bps,
        "candidate_stress_split_lcb_bps": candidate_stress_split_lcb_bps,
        "trial_summaries": trial_summaries,
    }


def select_nested_threshold(
    *,
    timestamps: np.ndarray,
    prediction: np.ndarray,
    realized_base: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    quantiles: Sequence[float],
    min_trades: int,
    base_cost_bps: float,
    stress_cost_multiplier: float,
    execution_latency_seconds: int,
) -> Dict[str, Any]:
    maximum = np.max(np.asarray(prediction, dtype=np.float64), axis=1)
    finite = maximum[np.isfinite(maximum)]
    if len(finite) == 0:
        return {"selected": None, "candidates": [], "reason": "no_finite_predictions"}
    thresholds = sorted(
        {
            # CatBoost shrinkage can preserve economically useful ranking while
            # shifting every net-target score below zero.  The nested window,
            # not an arbitrary zero score floor, determines whether a ranking
            # threshold has positive realized base/stress economics.  Future
            # OOS and permutation-control gates remain untouched.
            float(np.quantile(finite, quantile))
            for quantile in quantiles
        }
    )
    candidates: List[Dict[str, Any]] = []
    for threshold in thresholds:
        report = evaluate_joint_policy(
            timestamps=timestamps,
            prediction=prediction,
            realized_base=realized_base,
            actions=actions,
            threshold_bps=threshold,
            base_cost_bps=base_cost_bps,
            stress_cost_multiplier=stress_cost_multiplier,
            execution_latency_seconds=execution_latency_seconds,
        )
        base = report["base_cost"]
        stress = report["stress_cost"]
        viable = bool(
            base["count"] >= int(min_trades)
            and (base["lcb_bps"] or float("-inf")) > 0.0
            and (stress["lcb_bps"] or float("-inf")) > 0.0
        )
        candidates.append(
            {
                "threshold_bps": threshold,
                "trade_count": base["count"],
                "mean_base_net_bps": base["mean_bps"],
                "base_net_lcb_bps": base["lcb_bps"],
                "stress_net_lcb_bps": stress["lcb_bps"],
                "viable": viable,
            }
        )
    viable_candidates = [item for item in candidates if item["viable"]]
    selected = (
        max(
            viable_candidates,
            key=lambda item: (
                float(item["stress_net_lcb_bps"]),
                float(item["base_net_lcb_bps"]),
                -float(item["threshold_bps"]),
            ),
        )
        if viable_candidates
        else None
    )
    return {
        "selected": selected,
        "candidates": candidates,
        "score_distribution": summarize_score_distribution(finite),
        "score_threshold_floor_bps": None,
        "reason": "positive_stress_lcb" if selected else "no_positive_stress_lcb_threshold",
    }


def indices_between(timestamps: np.ndarray, start_ms: int, end_ms: int) -> np.ndarray:
    return np.flatnonzero((timestamps >= start_ms) & (timestamps < end_ms))


def build_model(args: argparse.Namespace) -> Any:
    if CatBoostRegressor is None:
        raise RuntimeError("catboost is required; use ai-trade-research image")
    return CatBoostRegressor(
        loss_function="MultiRMSE",
        eval_metric="MultiRMSE",
        iterations=int(args.iterations),
        depth=int(args.depth),
        learning_rate=float(args.learning_rate),
        l2_leaf_reg=float(args.l2_leaf_reg),
        random_strength=float(args.random_strength),
        random_seed=int(args.random_seed),
        allow_writing_files=False,
        verbose=False,
    )


def model_contract(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "library": "catboost",
        "library_version": getattr(catboost, "__version__", None),
        "loss_function": "MultiRMSE",
        "training_target": "fit_only_standardized_stress_profitability_indicator",
        "target_normalization": "per_action_zero_mean_unit_variance_on_fit_domain_only",
        "inference_score": "fit_class_conditional_expected_base_net_return_bps",
        "economic_acceptance_target": "untransformed_executable_base_and_stress_net_return",
        "validation_or_test_target_statistics_used_for_fit": False,
        "iterations": int(args.iterations),
        "depth": int(args.depth),
        "learning_rate": float(args.learning_rate),
        "l2_leaf_reg": float(args.l2_leaf_reg),
        "random_strength": float(args.random_strength),
        "random_seed": int(args.random_seed),
        "early_stopping_rounds": int(args.early_stopping_rounds),
    }


def run_probe(args: argparse.Namespace) -> Dict[str, Any]:
    assessment_path = pathlib.Path(args.capture_assessment).resolve()
    assessment = validate_capture_assessment(assessment_path)
    series = load_capture_rows(assessment)
    capture_merge_audit = validate_capture_merge_audit(
        series.get("capture_merge_audit")
    )
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    features, feature_names = build_causal_features(series)
    outcomes, actions = build_joint_action_returns(
        series,
        horizons_seconds=args.horizons_seconds,
        execution_latency_seconds=int(args.execution_latency_seconds),
        additional_round_trip_cost_bps=float(args.additional_round_trip_cost_bps),
    )
    eligible = np.all(np.isfinite(features), axis=1) & np.all(np.isfinite(outcomes), axis=1)
    timestamps = timestamps[eligible]
    features = features[eligible]
    outcomes = outcomes[eligible]
    if len(timestamps) < int(args.min_eligible_rows):
        raise CaptureNotReady(
            f"eligible microstructure rows {len(timestamps)} < {args.min_eligible_rows}"
        )
    embargo_seconds = max(args.horizons_seconds) + int(args.execution_latency_seconds)
    splits = build_time_splits(
        timestamps,
        n_splits=int(args.n_splits),
        train_window_seconds=int(args.train_window_seconds),
        validation_window_seconds=int(args.validation_window_seconds),
        test_window_seconds=int(args.test_window_seconds),
        rolling_step_seconds=int(args.rolling_step_seconds),
        embargo_seconds=embargo_seconds,
    )
    split_reports: List[Dict[str, Any]] = []
    all_base_edges: List[float] = []
    all_stress_edges: List[float] = []
    split_base_means: List[float] = []
    split_stress_means: List[float] = []
    permutation_trials = int(getattr(args, "permutation_control_trials", 7))
    permutation_seed = int(getattr(args, "permutation_control_seed", 20260808))
    permutation_minimum_excess_lcb_bps = float(
        getattr(args, "permutation_control_minimum_excess_lcb_bps", 0.0)
    )
    permutation_base_means_by_trial: List[List[float]] = [
        [] for _ in range(permutation_trials)
    ]
    permutation_stress_means_by_trial: List[List[float]] = [
        [] for _ in range(permutation_trials)
    ]
    failures: List[str] = []
    for split in splits:
        fit_indices = indices_between(timestamps, split.fit_start_ms, split.fit_end_ms)
        validation_indices = indices_between(
            timestamps, split.validation_start_ms, split.validation_end_ms
        )
        test_indices = indices_between(timestamps, split.test_start_ms, split.test_end_ms)
        minimum = int(args.min_window_rows)
        if min(len(fit_indices), len(validation_indices), len(test_indices)) < minimum:
            failures.append(f"split_{split.split_id}_insufficient_rows")
            split_reports.append(
                {
                    "split_id": split.split_id,
                    "status": "insufficient_rows",
                    "fit_rows": len(fit_indices),
                    "validation_rows": len(validation_indices),
                    "test_rows": len(test_indices),
                }
            )
            continue
        fit_targets, target_transform = fit_stress_profitability_transform(
            outcomes[fit_indices],
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
        )
        validation_targets = transform_stress_profitability_targets(
            outcomes[validation_indices], target_transform
        )
        model = build_model(args)
        model.fit(
            features[fit_indices],
            fit_targets,
            eval_set=(features[validation_indices], validation_targets),
            early_stopping_rounds=int(args.early_stopping_rounds),
            verbose=False,
        )
        validation_raw_prediction = np.asarray(
            model.predict(features[validation_indices]), dtype=np.float64
        )
        validation_prediction = reconstruct_base_net_scores(
            validation_raw_prediction, target_transform
        )
        calibration = select_nested_threshold(
            timestamps=timestamps[validation_indices],
            prediction=validation_prediction,
            realized_base=outcomes[validation_indices],
            actions=actions,
            quantiles=args.calibration_quantiles,
            min_trades=int(args.min_calibration_trades),
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
            execution_latency_seconds=int(args.execution_latency_seconds),
        )
        selected = calibration.get("selected")
        threshold = (
            float(selected["threshold_bps"])
            if isinstance(selected, dict)
            else float("inf")
        )
        test_raw_prediction = np.asarray(
            model.predict(features[test_indices]), dtype=np.float64
        )
        test_prediction = reconstruct_base_net_scores(
            test_raw_prediction, target_transform
        )
        objective = evaluate_joint_policy(
            timestamps=timestamps[test_indices],
            prediction=test_prediction,
            realized_base=outcomes[test_indices],
            actions=actions,
            threshold_bps=threshold,
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
            execution_latency_seconds=int(args.execution_latency_seconds),
        )
        permutation_controls = evaluate_prediction_permutation_controls(
            timestamps=timestamps[test_indices],
            prediction=test_prediction,
            realized_base=outcomes[test_indices],
            actions=actions,
            threshold_bps=threshold,
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
            execution_latency_seconds=int(args.execution_latency_seconds),
            trials=permutation_trials,
            seed=permutation_seed + split.split_id * 1_000_003,
        )
        for trial, control in enumerate(permutation_controls):
            control_base_mean = control["base_cost"].get("mean_bps")
            control_stress_mean = control["stress_cost"].get("mean_bps")
            if control_base_mean is not None:
                permutation_base_means_by_trial[trial].append(
                    float(control_base_mean)
                )
            if control_stress_mean is not None:
                permutation_stress_means_by_trial[trial].append(
                    float(control_stress_mean)
                )
        base = objective["base_cost"]
        stress = objective["stress_cost"]
        all_base_edges.extend(objective.pop("base_edges_bps"))
        all_stress_edges.extend(objective.pop("stress_edges_bps"))
        if base["mean_bps"] is not None:
            split_base_means.append(float(base["mean_bps"]))
            split_stress_means.append(float(stress["mean_bps"]))
        split_reports.append(
            {
                "split_id": split.split_id,
                "status": "trained",
                "time_contract": dataclasses.asdict(split),
                "fit_rows": len(fit_indices),
                "validation_rows": len(validation_indices),
                "test_rows": len(test_indices),
                "best_iteration": (
                    int(model.get_best_iteration()) + 1
                    if isinstance(model.get_best_iteration(), int)
                    and model.get_best_iteration() >= 0
                    else None
                ),
                "training_target_transform": target_transform,
                "nested_calibration": calibration,
                "validation_target_opportunity_distribution": summarize_score_distribution(
                    np.max(outcomes[validation_indices], axis=1)
                ),
                "test_target_opportunity_distribution": summarize_score_distribution(
                    np.max(outcomes[test_indices], axis=1)
                ),
                "test_prediction_score_distribution": summarize_score_distribution(
                    np.max(test_prediction, axis=1)
                ),
                "test_raw_model_output_distribution": summarize_score_distribution(
                    np.max(test_raw_prediction, axis=1)
                ),
                "oos_objective": objective,
                "oos_prediction_permutation_controls": permutation_controls,
            }
        )
    base_trade_summary = summarize_edges(all_base_edges)
    stress_trade_summary = summarize_edges(all_stress_edges)
    base_split_summary = summarize_edges(split_base_means)
    stress_split_summary = summarize_edges(split_stress_means)
    trained_split_count = sum(item.get("status") == "trained" for item in split_reports)
    positive_split_ratio = (
        sum(value > 0.0 for value in split_base_means) / len(split_base_means)
        if split_base_means
        else 0.0
    )
    permutation_control = summarize_prediction_permutation_controls(
        base_means_by_trial=permutation_base_means_by_trial,
        stress_means_by_trial=permutation_stress_means_by_trial,
        required_split_count=len(splits),
        candidate_base_split_lcb_bps=base_split_summary["lcb_bps"],
        candidate_stress_split_lcb_bps=stress_split_summary["lcb_bps"],
        minimum_excess_lcb_bps=permutation_minimum_excess_lcb_bps,
        seed=permutation_seed,
    )
    fully_verifiable = bool(
        trained_split_count == len(splits)
        and not failures
        and permutation_control["fully_verifiable"]
    )
    development_passed = bool(
        fully_verifiable
        and permutation_control["passed"]
        and base_trade_summary["count"] >= int(args.min_oos_trades)
        and positive_split_ratio >= float(args.min_positive_splits_ratio)
        and (base_split_summary["lcb_bps"] or float("-inf")) > 0.0
        and (stress_split_summary["lcb_bps"] or float("-inf")) > 0.0
    )
    frozen_candidate: Dict[str, Any] | None = None
    if development_passed:
        selected_thresholds = [
            float(selected["threshold_bps"])
            for item in split_reports
            for selected in [item.get("nested_calibration", {}).get("selected")]
            if isinstance(selected, dict)
        ]
        if not selected_thresholds:
            raise RuntimeError("development passed without a frozen policy threshold")
        best_iterations = [
            int(item["best_iteration"])
            for item in split_reports
            if isinstance(item.get("best_iteration"), int)
            and int(item["best_iteration"]) > 0
        ]
        final_iterations = int(
            round(statistics.median(best_iterations))
            if best_iterations
            else int(args.iterations)
        )
        final_targets, final_target_transform = fit_stress_profitability_transform(
            outcomes,
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
        )
        final_model = build_model(args)
        final_model.set_params(iterations=final_iterations)
        final_model.fit(features, final_targets, verbose=False)
        model_path = pathlib.Path(args.model_output).resolve()
        model_path.parent.mkdir(parents=True, exist_ok=True)
        final_model.save_model(str(model_path))
        frozen_candidate = {
            "model_path": str(model_path),
            "model_sha256": sha256_file(model_path),
            "final_training_row_count": int(len(features)),
            "final_iterations": final_iterations,
            "policy_threshold_bps": float(statistics.median(selected_thresholds)),
            "threshold_aggregation": "median_of_nested_split_thresholds",
            "target_transform": final_target_transform,
            "model_contract": model_contract(args),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if fully_verifiable else "FAIL",
        "fully_verifiable": fully_verifiable,
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "source_assessment": {
            "path": str(assessment_path),
            "sha256": sha256_file(assessment_path),
            "coverage_ms": assessment.get("coverage_ms"),
            "segment_count": assessment.get("valid_segment_count"),
            "development_cutoff_ms": assessment.get(
                "latest_exchange_timestamp_ms"
            ),
        },
        "capture_merge_contract": CAPTURE_MERGE_CONTRACT,
        "data": {
            "raw_feature_row_count": int(len(series["timestamp"])),
            "capture_merge_audit": capture_merge_audit,
            "eligible_row_count": int(len(timestamps)),
            "first_timestamp_ms": int(timestamps[0]),
            "last_timestamp_ms": int(timestamps[-1]),
            "feature_count": len(feature_names),
            "feature_names": feature_names,
        },
        "target_contract": {
            "objective": "joint_direction_and_exit_horizon_executable_net_return",
            "actions": actions,
            "execution_latency_seconds": int(args.execution_latency_seconds),
            "entry_exit_prices": "long=ask_to_bid;short=bid_to_ask",
            "additional_round_trip_cost_bps": float(args.additional_round_trip_cost_bps),
            "stress_cost_multiplier": float(args.stress_cost_multiplier),
            "overlapping_episodes_forbidden": True,
        },
        "validation_contract": {
            "method": "rolling_purged_nested_validation",
            "n_splits": int(args.n_splits),
            "train_window_seconds": int(args.train_window_seconds),
            "validation_window_seconds": int(args.validation_window_seconds),
            "test_window_seconds": int(args.test_window_seconds),
            "rolling_step_seconds": int(args.rolling_step_seconds),
            "embargo_seconds": embargo_seconds,
            "threshold_quantiles": list(args.calibration_quantiles),
            "action_hypothesis_count": len(actions),
            "nested_threshold_hypothesis_count": len(args.calibration_quantiles),
            "score_threshold_floor_bps": None,
            "negative_model_score_threshold_permitted": True,
            "threshold_viability_contract": (
                "realized_base_and_stress_net_lcb_positive_in_nested_validation"
            ),
            "oos_windows_non_overlapping": True,
        },
        "model_contract": model_contract(args),
        "negative_control": permutation_control,
        "economic_screen": {
            "development_passed": development_passed,
            "trained_split_count": trained_split_count,
            "required_split_count": len(splits),
            "oos_base_cost_by_trade": base_trade_summary,
            "oos_stress_cost_by_trade": stress_trade_summary,
            "oos_base_cost_by_split": base_split_summary,
            "oos_stress_cost_by_split": stress_split_summary,
            "positive_base_edge_split_ratio": positive_split_ratio,
            "minimum_oos_trades": int(args.min_oos_trades),
            "minimum_positive_splits_ratio": float(args.min_positive_splits_ratio),
            "prediction_permutation_control_passed": permutation_control["passed"],
        },
        "split_reports": split_reports,
        "frozen_candidate": frozen_candidate,
        "failures": failures,
        "next_gate": (
            "freeze_candidate_and_collect_independent_forward_selection"
            if development_passed
            else "reject_microstructure_candidate_and_remain_in_development"
        ),
        "independent_selection_required": True,
        "untouched_final_holdout_required": True,
    }


def write_candidate_manifest(
    path: pathlib.Path, report_path: pathlib.Path, report: Mapping[str, Any]
) -> None:
    frozen = report.get("frozen_candidate")
    passed = bool(
        report.get("economic_screen", {}).get("development_passed")
        and isinstance(frozen, dict)
        and len(str(frozen.get("model_sha256") or "")) == 64
    )
    frozen_identity = dict(frozen) if isinstance(frozen, dict) else None
    if isinstance(frozen_identity, dict):
        frozen_identity.pop("model_path", None)
    identity_contract = {
        "source_assessment_sha256": report.get("source_assessment", {}).get("sha256"),
        "capture_merge_contract": report.get("capture_merge_contract"),
        "capture_merge_audit": report.get("data", {}).get("capture_merge_audit"),
        "target_contract": report.get("target_contract"),
        "validation_contract": report.get("validation_contract"),
        "feature_names": report.get("data", {}).get("feature_names"),
        "model_contract": report.get("model_contract"),
        "frozen_candidate": frozen_identity,
    }
    payload = {
        "schema_version": "microstructure_alpha_candidate_manifest_v1",
        "status": "development_candidate_frozen" if passed else "rejected",
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "candidate_id": canonical_sha256(identity_contract) if passed else None,
        "identity_contract": identity_contract,
        "development_report": {
            "path": str(report_path),
            "sha256": sha256_file(report_path),
        },
        "next_gate": report.get("next_gate"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def parse_csv_floats(raw: str, *, minimum: float, maximum: float) -> List[float]:
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not values or len(set(values)) != len(values):
        raise ValueError("CSV float values must be non-empty and unique")
    if any(not math.isfinite(value) or value < minimum or value > maximum for value in values):
        raise ValueError("CSV float value outside allowed range")
    return values


def parse_csv_ints(raw: str) -> List[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values or len(set(values)) != len(values) or any(value <= 0 for value in values):
        raise ValueError("CSV integer values must be positive and unique")
    return sorted(values)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-assessment", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidate-manifest-output", required=True)
    parser.add_argument("--model-output", required=True)
    parser.add_argument("--horizons-seconds", default="15,30,60,120,300")
    parser.add_argument("--execution-latency-seconds", type=int, default=1)
    parser.add_argument("--additional-round-trip-cost-bps", type=float, default=11.0)
    parser.add_argument("--stress-cost-multiplier", type=float, default=1.25)
    parser.add_argument("--n-splits", type=int, default=6)
    parser.add_argument("--train-window-seconds", type=int, default=28800)
    parser.add_argument("--validation-window-seconds", type=int, default=7200)
    parser.add_argument("--test-window-seconds", type=int, default=7200)
    parser.add_argument("--rolling-step-seconds", type=int, default=7200)
    parser.add_argument("--min-eligible-rows", type=int, default=60000)
    parser.add_argument("--min-window-rows", type=int, default=3600)
    parser.add_argument("--calibration-quantiles", default="0.50,0.60,0.70,0.80,0.90,0.95,0.98")
    parser.add_argument("--min-calibration-trades", type=int, default=8)
    parser.add_argument("--min-oos-trades", type=int, default=30)
    parser.add_argument("--min-positive-splits-ratio", type=float, default=0.60)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--learning-rate", type=float, default=0.035)
    parser.add_argument("--l2-leaf-reg", type=float, default=30.0)
    parser.add_argument("--random-strength", type=float, default=2.0)
    parser.add_argument("--random-seed", type=int, default=20260806)
    parser.add_argument("--early-stopping-rounds", type=int, default=20)
    parser.add_argument("--permutation-control-trials", type=int, default=7)
    parser.add_argument("--permutation-control-seed", type=int, default=20260808)
    parser.add_argument(
        "--permutation-control-minimum-excess-lcb-bps", type=float, default=0.0
    )
    parser.add_argument("--research-domain", choices=("development",), default="development")
    args = parser.parse_args()
    args.horizons_seconds = parse_csv_ints(args.horizons_seconds)
    args.calibration_quantiles = parse_csv_floats(
        args.calibration_quantiles, minimum=0.0, maximum=1.0
    )
    if args.execution_latency_seconds < 1:
        raise ValueError("execution latency must be >= 1 second")
    if args.additional_round_trip_cost_bps <= 0.0:
        raise ValueError("additional round-trip cost must be positive")
    if args.stress_cost_multiplier <= 1.0:
        raise ValueError("stress cost multiplier must be > 1")
    if args.permutation_control_trials < 5:
        raise ValueError("permutation control trials must be >= 5")
    if args.permutation_control_minimum_excess_lcb_bps < 0.0:
        raise ValueError("permutation control minimum excess LCB must be non-negative")
    return args


def not_ready_report(args: argparse.Namespace, reason: str) -> Dict[str, Any]:
    assessment_path = pathlib.Path(args.capture_assessment).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "NOT_READY",
        "fully_verifiable": False,
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "source_assessment": {
            "path": str(assessment_path),
            "sha256": sha256_file(assessment_path) if assessment_path.is_file() else None,
        },
        "economic_screen": {"development_passed": False},
        "failures": [reason],
        "next_gate": "continue_forward_capture",
        "independent_selection_required": True,
        "untouched_final_holdout_required": True,
    }


def main() -> int:
    args = parse_args()
    output = pathlib.Path(args.output).resolve()
    manifest = pathlib.Path(args.candidate_manifest_output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = run_probe(args)
        exit_code = 0 if report.get("fully_verifiable") else 2
    except (CaptureNotReady, OSError, ValueError, RuntimeError) as exc:
        report = not_ready_report(args, str(exc))
        exit_code = 2
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    write_candidate_manifest(manifest, output, report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
