#!/usr/bin/env python3
"""Development-only cost-aware order-book/trade-flow economic screen.

The probe consumes only the checksum-bound segment manifest emitted by
``assess_microstructure_capture.py``.  It learns every sufficiently supported
(long|short, holding horizon) action with an independent classifier for the
rare event that executable return remains positive under stressed costs.  A
fit-only two-state economic reconstruction converts event probabilities back
to comparable quote-to-quote return scores.  Early stopping uses a purged tail
inside each fit window; the outer validation window remains untouched until
economic threshold selection and is followed by a disjoint forward OOS window.
A PASS here is development evidence only and can never be used as promotion or
final-holdout evidence.
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
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np

import collect_bybit_microstructure as collector

try:
    import catboost
    from catboost import CatBoostClassifier, CatBoostRanker, CatBoostRegressor, Pool
except ImportError:  # pragma: no cover - exercised by the research image
    catboost = None
    CatBoostClassifier = None
    CatBoostRanker = None
    CatBoostRegressor = None
    Pool = None


SCHEMA_VERSION = "microstructure_alpha_development_v8"
ASSESSMENT_SCHEMA_VERSION = "microstructure_capture_assessment_v1"
TARGET_ARCHITECTURE_COMPARISON_SCHEMA_VERSION = (
    "microstructure_target_architecture_comparison_v1"
)
TARGET_ARCHITECTURE_IDS = (
    "binary_stress_event_baseline",
    "direct_stress_utility_regression",
    "two_stage_opportunity_action",
    "joint_action_ranker",
)
FROZEN_TARGET_ARCHITECTURE_FEATURE_COUNT = 242
FROZEN_TARGET_ARCHITECTURE_ACTION_COUNT = 10
FROZEN_TARGET_ARCHITECTURE_SPLIT_COUNT = 6
CAUSAL_FEATURE_LAGS_SECONDS = (1, 5, 20, 60, 120, 300)
REGIME_FEATURE_WINDOWS_SECONDS = (20, 60, 120, 300)
MAX_CAUSAL_FEATURE_LOOKBACK_SECONDS = max(CAUSAL_FEATURE_LAGS_SECONDS)
MIN_CAUSAL_FEATURE_HISTORY_ROWS = MAX_CAUSAL_FEATURE_LOOKBACK_SECONDS + 1
CAUSAL_FEATURE_CONTRACT = {
    "revision": "order_flow_cross_asset_regime_v1",
    "exchange_time_lags_seconds": list(CAUSAL_FEATURE_LAGS_SECONDS),
    "regime_windows_seconds": list(REGIME_FEATURE_WINDOWS_SECONDS),
    "maximum_lookback_seconds": MAX_CAUSAL_FEATURE_LOOKBACK_SECONDS,
    "rolling_interval_policy": "every_exchange_second_required",
    "missing_or_non_finite_policy": "non_finite_until_complete_exact_window",
    "realized_volatility": "root_mean_square_one_second_mid_return_bps",
    "trend_efficiency": "signed_window_return_bps_over_absolute_one_second_path_bps",
    "normalized_return": "window_return_bps_over_realized_volatility_times_sqrt_window",
    "cross_asset_scope": "same_exchange_second_btc_eth_context",
    "future_values_permitted": False,
}
CAPTURE_MERGE_CONTRACT = {
    "method": "drop_shared_adjacent_boundary_buckets_v1",
    "segment_order": "strictly_chronological_manifest_order",
    "allowed_duplicate_scope": "exact_shared_endpoint_of_two_adjacent_segments_only",
    "boundary_action": "drop_entire_shared_one_second_bucket",
    "non_boundary_action": "fail_closed",
    "maximum_segments_per_boundary": 2,
}
REQUIRED_FIELDS = collector.OUTPUT_FIELDS


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


def integer_index_sha256(values: Sequence[int]) -> str:
    array = np.asarray(values, dtype="<i8")
    digest = hashlib.sha256()
    digest.update(str(array.shape).encode("ascii"))
    digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


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
    if not (
        payload.get("symbols") == list(collector.CAPTURE_SYMBOLS)
        and payload.get("cross_asset_alignment_contract")
        == collector.CROSS_ASSET_ALIGNMENT_CONTRACT
    ):
        failures.append("capture assessment cross-asset alignment contract failed")
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
        if not (
            item.get("capture_schema_version") == collector.SCHEMA_VERSION
            and item.get("symbols") == list(collector.CAPTURE_SYMBOLS)
        ):
            raise ValueError("capture segment cross-asset contract mismatch")
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


def exact_rolling_sum(
    values: np.ndarray, timestamps: np.ndarray, window_seconds: int
) -> np.ndarray:
    """Return a causal sum only when every second in the window is present."""
    if int(window_seconds) <= 0:
        raise ValueError("rolling window must be positive")
    array = np.asarray(values, dtype=np.float64)
    output = np.full(len(array), np.nan, dtype=np.float64)
    positions = {int(timestamp): index for index, timestamp in enumerate(timestamps)}
    finite = np.isfinite(array)
    prefix = np.concatenate(
        ([0.0], np.cumsum(np.where(finite, array, 0.0), dtype=np.float64))
    )
    valid_prefix = np.concatenate(([0], np.cumsum(finite, dtype=np.int64)))
    offset_ms = (int(window_seconds) - 1) * 1000
    for index, timestamp in enumerate(timestamps):
        start = positions.get(int(timestamp) - offset_ms)
        if (
            start is not None
            and index - start + 1 == int(window_seconds)
            and valid_prefix[index + 1] - valid_prefix[start] == int(window_seconds)
        ):
            output[index] = prefix[index + 1] - prefix[start]
    return output


def exact_rolling_mean(
    values: np.ndarray, timestamps: np.ndarray, window_seconds: int
) -> np.ndarray:
    return exact_rolling_sum(values, timestamps, window_seconds) / float(
        window_seconds
    )


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

    def add_price_regime_features(
        feature_prefix: str,
        price_returns: Mapping[int, np.ndarray],
    ) -> None:
        one_second_return_bps = np.asarray(
            price_returns[1], dtype=np.float64
        ) * 10000.0
        for window in REGIME_FEATURE_WINDOWS_SECONDS:
            mean_square = exact_rolling_mean(
                np.square(one_second_return_bps), timestamps, window
            )
            realized_volatility = np.sqrt(np.maximum(mean_square, 0.0))
            absolute_path = exact_rolling_sum(
                np.abs(one_second_return_bps), timestamps, window
            )
            window_return_bps = (
                np.asarray(price_returns[window], dtype=np.float64) * 10000.0
            )
            complete = (
                np.isfinite(realized_volatility)
                & np.isfinite(absolute_path)
                & np.isfinite(window_return_bps)
            )
            trend_efficiency = np.full(len(timestamps), np.nan, dtype=np.float64)
            moving = complete & (absolute_path > 0.0)
            trend_efficiency[moving] = window_return_bps[moving] / absolute_path[moving]
            trend_efficiency[complete & (absolute_path == 0.0)] = 0.0
            normalized_return = np.full(len(timestamps), np.nan, dtype=np.float64)
            volatility_scale = realized_volatility * math.sqrt(float(window))
            variable = complete & (volatility_scale > 0.0)
            normalized_return[variable] = (
                window_return_bps[variable] / volatility_scale[variable]
            )
            normalized_return[complete & (volatility_scale == 0.0)] = 0.0
            add(
                f"{feature_prefix}_realized_volatility_{window}s_bps",
                realized_volatility,
            )
            add(
                f"{feature_prefix}_signed_trend_efficiency_{window}s",
                trend_efficiency,
            )
            add(
                f"{feature_prefix}_normalized_return_{window}s",
                normalized_return,
            )

    add("micro_spread_bps", series["spread_bps"])
    add("micro_microprice_dislocation_bps", micro_dislocation)
    add("micro_book_imbalance_l1", imbalance_l1)
    add("micro_book_imbalance_l5", imbalance_l5)
    add("micro_book_imbalance_l20", imbalance_l20)
    add("micro_depth_slope", series["depth_slope"])
    add(
        "micro_log_top_depth_quote",
        np.log1p(
            mid
            * (
                np.asarray(series["best_bid_size"], dtype=np.float64)
                + np.asarray(series["best_ask_size"], dtype=np.float64)
            )
        ),
    )
    add(
        "micro_log_depth_l5_quote",
        np.log1p(
            mid
            * (
                np.asarray(series["bid_depth_l5"], dtype=np.float64)
                + np.asarray(series["ask_depth_l5"], dtype=np.float64)
            )
        ),
    )
    add(
        "micro_log_depth_l20_quote",
        np.log1p(
            mid
            * (
                np.asarray(series["bid_depth_l20"], dtype=np.float64)
                + np.asarray(series["ask_depth_l20"], dtype=np.float64)
            )
        ),
    )
    add("micro_book_flow_imbalance", series["book_flow_imbalance"])
    add("micro_log_book_flow_quote_volume", np.log1p(series["book_flow_quote_volume"]))
    add("micro_book_ofi", series["book_ofi"])
    add("micro_book_mid_range_bps", series["book_mid_range_bps"])
    add("micro_trade_imbalance", trade_imbalance)
    add("micro_trade_vwap_dislocation_bps", series["trade_vwap_dislocation_bps"])
    add("micro_log_book_updates", np.log1p(series["book_update_count"]))
    add("micro_log_trade_count", np.log1p(series["trade_count"]))
    add("micro_log_buy_quote_volume", np.log1p(series["buy_quote_volume"]))
    add("micro_log_sell_quote_volume", np.log1p(series["sell_quote_volume"]))
    target_book_flow_volume = np.asarray(
        series["book_flow_quote_volume"], dtype=np.float64
    )
    target_book_flow_signed = (
        np.asarray(series["book_flow_imbalance"], dtype=np.float64)
        * target_book_flow_volume
    )
    target_buy_quote = np.asarray(series["buy_quote_volume"], dtype=np.float64)
    target_sell_quote = np.asarray(series["sell_quote_volume"], dtype=np.float64)
    for lag in CAUSAL_FEATURE_LAGS_SECONDS:
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
        if lag > 1:
            book_flow_abs = exact_rolling_sum(
                target_book_flow_volume, timestamps, lag
            )
            trade_quote = exact_rolling_sum(
                target_buy_quote + target_sell_quote, timestamps, lag
            )
            add(
                f"micro_book_flow_imbalance_{lag}s",
                np.divide(
                    exact_rolling_sum(target_book_flow_signed, timestamps, lag),
                    book_flow_abs,
                    out=np.zeros_like(book_flow_abs),
                    where=book_flow_abs > 0.0,
                ),
            )
            add(
                f"micro_book_ofi_mean_{lag}s",
                exact_rolling_mean(
                    np.asarray(series["book_ofi"], dtype=np.float64),
                    timestamps,
                    lag,
                ),
            )
            add(
                f"micro_trade_imbalance_{lag}s",
                np.divide(
                    exact_rolling_sum(
                        target_buy_quote - target_sell_quote, timestamps, lag
                    ),
                    trade_quote,
                    out=np.zeros_like(trade_quote),
                    where=trade_quote > 0.0,
                ),
            )
    target_returns: Dict[int, np.ndarray] = {
        lag: mid / exact_lag(mid, timestamps, lag) - 1.0
        for lag in CAUSAL_FEATURE_LAGS_SECONDS
    }
    add_price_regime_features("micro", target_returns)
    for symbol in collector.CONTEXT_SYMBOLS:
        prefix = collector.context_prefix(symbol)
        context_mid = np.asarray(series[f"{prefix}_mid"], dtype=np.float64)
        context_microprice = np.asarray(
            series[f"{prefix}_microprice"], dtype=np.float64
        )
        context_l1 = np.asarray(
            series[f"{prefix}_book_imbalance_l1"], dtype=np.float64
        )
        context_l5 = np.asarray(
            series[f"{prefix}_book_imbalance_l5"], dtype=np.float64
        )
        context_l20 = np.asarray(
            series[f"{prefix}_book_imbalance_l20"], dtype=np.float64
        )
        context_trade = np.asarray(
            series[f"{prefix}_trade_imbalance"], dtype=np.float64
        )
        context_book_flow_volume = np.asarray(
            series[f"{prefix}_book_flow_quote_volume"], dtype=np.float64
        )
        context_book_flow_signed = (
            np.asarray(series[f"{prefix}_book_flow_imbalance"], dtype=np.float64)
            * context_book_flow_volume
        )
        context_buy_quote = np.asarray(
            series[f"{prefix}_buy_quote_volume"], dtype=np.float64
        )
        context_sell_quote = np.asarray(
            series[f"{prefix}_sell_quote_volume"], dtype=np.float64
        )
        context_dislocation = (context_microprice / context_mid - 1.0) * 10000.0
        add(f"cross_asset_{prefix}_spread_bps", series[f"{prefix}_spread_bps"])
        add(
            f"cross_asset_{prefix}_microprice_dislocation_bps",
            context_dislocation,
        )
        add(f"cross_asset_{prefix}_book_imbalance_l1", context_l1)
        add(f"cross_asset_{prefix}_book_imbalance_l5", context_l5)
        add(f"cross_asset_{prefix}_book_imbalance_l20", context_l20)
        add(f"cross_asset_{prefix}_depth_slope", series[f"{prefix}_depth_slope"])
        add(
            f"cross_asset_{prefix}_log_top_depth_quote",
            np.log1p(
                context_mid
                * (
                    np.asarray(
                        series[f"{prefix}_best_bid_size"], dtype=np.float64
                    )
                    + np.asarray(
                        series[f"{prefix}_best_ask_size"], dtype=np.float64
                    )
                )
            ),
        )
        add(
            f"cross_asset_{prefix}_log_depth_l5_quote",
            np.log1p(
                context_mid
                * (
                    np.asarray(series[f"{prefix}_bid_depth_l5"], dtype=np.float64)
                    + np.asarray(
                        series[f"{prefix}_ask_depth_l5"], dtype=np.float64
                    )
                )
            ),
        )
        add(
            f"cross_asset_{prefix}_log_depth_l20_quote",
            np.log1p(
                context_mid
                * (
                    np.asarray(
                        series[f"{prefix}_bid_depth_l20"], dtype=np.float64
                    )
                    + np.asarray(
                        series[f"{prefix}_ask_depth_l20"], dtype=np.float64
                    )
                )
            ),
        )
        add(
            f"cross_asset_{prefix}_book_flow_imbalance",
            series[f"{prefix}_book_flow_imbalance"],
        )
        add(
            f"cross_asset_{prefix}_log_book_flow_quote_volume",
            np.log1p(series[f"{prefix}_book_flow_quote_volume"]),
        )
        add(f"cross_asset_{prefix}_book_ofi", series[f"{prefix}_book_ofi"])
        add(
            f"cross_asset_{prefix}_book_mid_range_bps",
            series[f"{prefix}_book_mid_range_bps"],
        )
        add(f"cross_asset_{prefix}_trade_imbalance", context_trade)
        add(
            f"cross_asset_{prefix}_trade_vwap_dislocation_bps",
            series[f"{prefix}_trade_vwap_dislocation_bps"],
        )
        add(
            f"cross_asset_{prefix}_log_book_updates",
            np.log1p(series[f"{prefix}_book_update_count"]),
        )
        add(
            f"cross_asset_{prefix}_log_trade_count",
            np.log1p(series[f"{prefix}_trade_count"]),
        )
        add(
            f"cross_asset_{prefix}_log_buy_quote_volume",
            np.log1p(series[f"{prefix}_buy_quote_volume"]),
        )
        add(
            f"cross_asset_{prefix}_log_sell_quote_volume",
            np.log1p(series[f"{prefix}_sell_quote_volume"]),
        )
        context_returns: Dict[int, np.ndarray] = {
            lag: context_mid / exact_lag(context_mid, timestamps, lag) - 1.0
            for lag in CAUSAL_FEATURE_LAGS_SECONDS
        }
        for lag in CAUSAL_FEATURE_LAGS_SECONDS:
            context_return = context_returns[lag]
            add(f"cross_asset_{prefix}_mid_return_{lag}s", context_return)
            add(
                f"cross_asset_sol_minus_{prefix}_return_{lag}s",
                target_returns[lag] - context_return,
            )
            add(
                f"cross_asset_{prefix}_dislocation_delta_{lag}s",
                context_dislocation
                - exact_lag(context_dislocation, timestamps, lag),
            )
            add(
                f"cross_asset_{prefix}_book_l1_delta_{lag}s",
                context_l1 - exact_lag(context_l1, timestamps, lag),
            )
            add(
                f"cross_asset_{prefix}_book_l5_delta_{lag}s",
                context_l5 - exact_lag(context_l5, timestamps, lag),
            )
            add(
                f"cross_asset_{prefix}_trade_imbalance_delta_{lag}s",
                context_trade - exact_lag(context_trade, timestamps, lag),
            )
            if lag > 1:
                book_flow_abs = exact_rolling_sum(
                    context_book_flow_volume, timestamps, lag
                )
                trade_quote = exact_rolling_sum(
                    context_buy_quote + context_sell_quote, timestamps, lag
                )
                add(
                    f"cross_asset_{prefix}_book_flow_imbalance_{lag}s",
                    np.divide(
                        exact_rolling_sum(
                            context_book_flow_signed, timestamps, lag
                        ),
                        book_flow_abs,
                        out=np.zeros_like(book_flow_abs),
                        where=book_flow_abs > 0.0,
                    ),
                )
                add(
                    f"cross_asset_{prefix}_book_ofi_mean_{lag}s",
                    exact_rolling_mean(
                        np.asarray(
                            series[f"{prefix}_book_ofi"], dtype=np.float64
                        ),
                        timestamps,
                        lag,
                    ),
                )
                add(
                    f"cross_asset_{prefix}_trade_imbalance_{lag}s",
                    np.divide(
                        exact_rolling_sum(
                            context_buy_quote - context_sell_quote,
                            timestamps,
                            lag,
                        ),
                        trade_quote,
                        out=np.zeros_like(trade_quote),
                        where=trade_quote > 0.0,
                    ),
                )
        add_price_regime_features(f"cross_asset_{prefix}", context_returns)
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
        fit_start = fit_end - train_window_seconds * 1000
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


def summarize_numeric_distribution(values: Sequence[float]) -> Dict[str, Any]:
    finite = np.asarray(
        [float(value) for value in values if math.isfinite(float(value))],
        dtype=np.float64,
    )
    if len(finite) == 0:
        return {"count": 0, "minimum": None, "maximum": None, "quantiles": {}}
    return {
        "count": int(len(finite)),
        "minimum": float(np.min(finite)),
        "maximum": float(np.max(finite)),
        "quantiles": {
            str(quantile): float(np.quantile(finite, quantile))
            for quantile in (0.5, 0.8, 0.9, 0.95, 0.98)
        },
    }


def summarize_binary_ranking(
    labels: Sequence[float], probabilities: Sequence[float]
) -> Dict[str, Any]:
    """Report discrimination without allowing the diagnostic to select policy."""
    target = np.asarray(labels, dtype=np.float64)
    score = np.asarray(probabilities, dtype=np.float64)
    if target.ndim != 1 or score.ndim != 1 or len(target) != len(score):
        raise ValueError("binary ranking input shape mismatch")
    if not (
        np.all(np.isfinite(target))
        and np.all(np.isfinite(score))
        and np.all((target == 0.0) | (target == 1.0))
        and np.all(score >= -1e-12)
        and np.all(score <= 1.0 + 1e-12)
    ):
        raise ValueError("binary ranking inputs are invalid")
    count = int(len(target))
    positive_count = int(np.sum(target))
    negative_count = count - positive_count
    prevalence = float(positive_count / count) if count else None
    if not count:
        return {
            "count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "prevalence": None,
            "roc_auc": None,
            "average_precision": None,
            "average_precision_lift": None,
        }

    # Group equal scores so ROC-AUC awards ties exactly half credit and AP is
    # invariant to the original row order inside a tied group.
    ascending = np.argsort(score, kind="mergesort")
    sorted_score = score[ascending]
    sorted_target = target[ascending]
    favorable_pairs = 0.0
    negatives_below = 0
    offset = 0
    while offset < count:
        end = offset + 1
        while end < count and sorted_score[end] == sorted_score[offset]:
            end += 1
        group = sorted_target[offset:end]
        group_positives = int(np.sum(group))
        group_negatives = len(group) - group_positives
        favorable_pairs += group_positives * (
            negatives_below + 0.5 * group_negatives
        )
        negatives_below += group_negatives
        offset = end
    roc_auc = (
        favorable_pairs / (positive_count * negative_count)
        if positive_count and negative_count
        else None
    )

    average_precision = None
    if positive_count:
        descending = np.argsort(-score, kind="mergesort")
        sorted_score = score[descending]
        sorted_target = target[descending]
        true_positives = 0
        false_positives = 0
        previous_recall = 0.0
        area = 0.0
        offset = 0
        while offset < count:
            end = offset + 1
            while end < count and sorted_score[end] == sorted_score[offset]:
                end += 1
            group = sorted_target[offset:end]
            group_positives = int(np.sum(group))
            true_positives += group_positives
            false_positives += len(group) - group_positives
            recall = true_positives / positive_count
            precision = true_positives / (true_positives + false_positives)
            area += (recall - previous_recall) * precision
            previous_recall = recall
            offset = end
        average_precision = area
    return {
        "count": count,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "prevalence": prevalence,
        "roc_auc": roc_auc,
        "average_precision": average_precision,
        "average_precision_lift": (
            average_precision / prevalence
            if average_precision is not None and prevalence
            else None
        ),
    }


def summarize_event_ranking_by_action(
    labels: np.ndarray,
    probabilities: np.ndarray,
    transform: Mapping[str, Any],
) -> Dict[str, Any]:
    """Bind rare-event ranking diagnostics to the frozen action indices."""
    target = np.asarray(labels, dtype=np.float64)
    score = np.asarray(probabilities, dtype=np.float64)
    action_indices = [int(value) for value in transform["model_action_indices"]]
    if not (
        target.ndim == 2
        and score.ndim == 2
        and target.shape == score.shape
        and target.shape[1] == len(action_indices)
    ):
        raise ValueError("event ranking matrix shape mismatch")
    actions = transform["actions"]
    return {
        "contract": (
            "validation_or_test_discrimination_diagnostic_only;"
            "never_used_for_fit_threshold_or_promotion"
        ),
        "promotion_evidence": False,
        "by_action": {
            str(action_index): {
                "action": actions[action_index],
                **summarize_binary_ranking(
                    target[:, column], score[:, column]
                ),
            }
            for column, action_index in enumerate(action_indices)
        },
    }


def frozen_target_architecture_shape_failures(
    *, feature_count: int, action_count: int, split_count: int
) -> List[str]:
    failures: List[str] = []
    if int(feature_count) != FROZEN_TARGET_ARCHITECTURE_FEATURE_COUNT:
        failures.append("frozen_feature_count_242_required")
    if int(action_count) != FROZEN_TARGET_ARCHITECTURE_ACTION_COUNT:
        failures.append("frozen_action_count_10_required")
    if int(split_count) != FROZEN_TARGET_ARCHITECTURE_SPLIT_COUNT:
        failures.append("frozen_split_count_6_required")
    return failures


def build_stress_net_utility_targets(
    outcomes: np.ndarray,
    *,
    base_cost_bps: float,
    stress_cost_multiplier: float,
) -> np.ndarray:
    matrix = np.asarray(outcomes, dtype=np.float64)
    base_cost = float(base_cost_bps)
    multiplier = float(stress_cost_multiplier)
    if matrix.ndim != 2 or not np.all(np.isfinite(matrix)):
        raise ValueError("stress-net utility outcomes are invalid")
    if not math.isfinite(base_cost) or base_cost <= 0.0:
        raise ValueError("stress-net utility base cost must be positive")
    if not math.isfinite(multiplier) or multiplier <= 1.0:
        raise ValueError("stress-net utility multiplier must exceed one")
    return matrix - base_cost * (multiplier - 1.0)


def build_two_stage_targets(stress_utilities: np.ndarray) -> Dict[str, Any]:
    matrix = np.asarray(stress_utilities, dtype=np.float64)
    if (
        matrix.ndim != 2
        or matrix.shape[1] <= 0
        or not np.all(np.isfinite(matrix))
    ):
        raise ValueError("two-stage stress utilities are invalid")
    opportunity = np.max(matrix, axis=1) > 0.0
    opportunity_rows = np.flatnonzero(opportunity)
    # np.argmax deliberately resolves ties using the first predeclared action.
    best_action = np.argmax(matrix[opportunity_rows], axis=1).astype(np.int64)
    return {
        "opportunity": opportunity.astype(np.float64),
        "opportunity_row_indices": opportunity_rows,
        "best_action": best_action,
        "class_indices": sorted(set(int(value) for value in best_action)),
        "tie_break_contract": "first_predeclared_action",
        "validation_or_test_statistics_used": False,
    }


def restore_ordered_action_probabilities(
    probabilities: np.ndarray,
    *,
    class_labels: Sequence[int],
    action_count: int,
) -> np.ndarray:
    matrix = np.asarray(probabilities, dtype=np.float64)
    labels = [int(value) for value in class_labels]
    count = int(action_count)
    if (
        matrix.ndim != 2
        or matrix.shape[1] != len(labels)
        or count <= 0
        or len(set(labels)) != len(labels)
        or any(value < 0 or value >= count for value in labels)
        or not np.all(np.isfinite(matrix))
        or np.any(matrix < -1e-12)
        or np.any(matrix > 1.0 + 1e-12)
    ):
        raise ValueError("conditional action probabilities are invalid")
    restored = np.zeros((len(matrix), count), dtype=np.float64)
    for column, action_index in enumerate(labels):
        restored[:, action_index] = np.clip(matrix[:, column], 0.0, 1.0)
    return restored


def build_joint_ranker_dataset(
    features: np.ndarray,
    stress_utilities: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    feature_matrix = np.asarray(features, dtype=np.float64)
    utility_matrix = np.asarray(stress_utilities, dtype=np.float64)
    action_count = len(actions)
    if (
        feature_matrix.ndim != 2
        or utility_matrix.ndim != 2
        or len(feature_matrix) != len(utility_matrix)
        or utility_matrix.shape[1] != action_count
        or action_count <= 0
        or not np.all(np.isfinite(feature_matrix))
        or not np.all(np.isfinite(utility_matrix))
    ):
        raise ValueError("joint ranker dataset shape is invalid")
    maximum_horizon = max(int(item.get("horizon_seconds") or 0) for item in actions)
    if maximum_horizon <= 0:
        raise ValueError("joint ranker action horizon is invalid")
    descriptors: List[Tuple[float, float]] = []
    for item in actions:
        direction = str(item.get("direction") or "")
        horizon = int(item.get("horizon_seconds") or 0)
        if direction not in {"long", "short"} or horizon <= 0:
            raise ValueError("joint ranker action contract is invalid")
        descriptors.append(
            (
                1.0 if direction == "long" else -1.0,
                math.log1p(horizon) / math.log1p(maximum_horizon),
            )
        )
    repeated = np.repeat(feature_matrix, action_count, axis=0)
    tiled_descriptors = np.tile(
        np.asarray(descriptors, dtype=np.float64), (len(feature_matrix), 1)
    )
    expanded = np.column_stack((repeated, tiled_descriptors))
    target = utility_matrix.reshape(-1)
    group_id = np.repeat(np.arange(len(feature_matrix), dtype=np.int64), action_count)
    return expanded, target, group_id


def reshape_joint_ranker_predictions(
    predictions: np.ndarray, *, row_count: int, action_count: int
) -> np.ndarray:
    values = np.asarray(predictions, dtype=np.float64)
    rows = int(row_count)
    actions = int(action_count)
    if (
        values.ndim != 1
        or rows < 0
        or actions <= 0
        or len(values) != rows * actions
        or not np.all(np.isfinite(values))
    ):
        raise ValueError("joint ranker prediction shape is invalid")
    return values.reshape(rows, actions)


def fit_joint_policy_target(
    outcomes: np.ndarray,
    *,
    actions: Sequence[Mapping[str, Any]],
    base_cost_bps: float,
    stress_cost_multiplier: float,
    minimum_profitable_events: int = 1,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Build independent fit-only stressed-profitability event targets.

    Executable positive returns are a rare right-tail event in the collected
    market.  Optimizing a conditional return quantile still concentrated the
    model near the roughly -12 bps unconditional cost floor and produced zero
    profitable diagnostic OOS trades in every development split.  The target
    here is therefore the exact event required by the economic gate: an
    action's base-net return must exceed the incremental stress cost.  Only
    actions with enough fit-only positive and negative examples are modeled.

    The event classifier's probability is converted to a bps score using only
    the fit window's positive/negative conditional means.  Validation and test
    outcomes never influence the transform, and all acceptance decisions still
    use the original untransformed executable base/stress returns.
    """
    matrix = np.asarray(outcomes, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] < 1 or matrix.shape[1] < 1:
        raise ValueError("joint-policy target requires a non-empty matrix")
    if not np.all(np.isfinite(matrix)):
        raise ValueError("joint-policy target outcomes must be finite")
    base_cost = float(base_cost_bps)
    multiplier = float(stress_cost_multiplier)
    if not math.isfinite(base_cost) or base_cost <= 0.0:
        raise ValueError("joint-policy target base cost must be positive")
    if not math.isfinite(multiplier) or multiplier <= 1.0:
        raise ValueError("joint-policy target multiplier must exceed one")
    minimum_events = int(minimum_profitable_events)
    if minimum_events < 1:
        raise ValueError("joint-policy minimum profitable events must be positive")

    stress_increment = base_cost * (multiplier - 1.0)
    if len(actions) != matrix.shape[1]:
        raise ValueError("joint-policy action/outcome shape mismatch")
    normalized_actions: List[Dict[str, Any]] = []
    for item in actions:
        direction = str(item.get("direction") or "")
        horizon = int(item.get("horizon_seconds") or 0)
        if direction not in {"long", "short"} or horizon <= 0:
            raise ValueError("joint-policy action contract is invalid")
        normalized_actions.append(
            {"direction": direction, "horizon_seconds": horizon}
        )

    active_targets: List[np.ndarray] = []
    model_action_indices: List[int] = []
    action_statistics: List[Dict[str, Any]] = []
    for action_index in range(matrix.shape[1]):
        values = matrix[:, action_index]
        profitable = values > stress_increment
        profitable_count = int(np.sum(profitable))
        unprofitable_count = int(len(values) - profitable_count)
        profitable_mean = (
            float(np.mean(values[profitable])) if profitable_count else None
        )
        unprofitable_mean = (
            float(np.mean(values[~profitable])) if unprofitable_count else None
        )
        learnable = bool(
            profitable_count >= minimum_events
            and unprofitable_count >= minimum_events
            and profitable_mean is not None
            and unprofitable_mean is not None
            and profitable_mean > unprofitable_mean
        )
        if learnable:
            model_action_indices.append(action_index)
            active_targets.append(profitable.astype(np.float64))
        action_statistics.append(
            {
                "action_index": action_index,
                "row_count": int(len(values)),
                "raw_mean_base_net_bps": float(np.mean(values)),
                "raw_minimum_base_net_bps": float(np.min(values)),
                "raw_maximum_base_net_bps": float(np.max(values)),
                "stress_profitable_count": profitable_count,
                "stress_unprofitable_count": unprofitable_count,
                "stress_profitable_rate": float(profitable_count / len(values)),
                "stress_profitable_mean_base_net_bps": profitable_mean,
                "stress_unprofitable_mean_base_net_bps": unprofitable_mean,
                "learnable": learnable,
            }
        )
    targets = (
        np.column_stack(active_targets)
        if active_targets
        else np.empty((len(matrix), 0), dtype=np.float64)
    )
    return targets, {
        "method": "fit_only_stress_profitability_event_v5",
        "training_objective": "independent_stress_cost_profitable_event",
        "actions": normalized_actions,
        "stress_incremental_cost_bps": stress_increment,
        "event_definition": (
            "executable_base_net_return_bps_gt_stress_incremental_cost_bps"
        ),
        "minimum_profitable_events_per_action": minimum_events,
        "minimum_unprofitable_events_per_action": minimum_events,
        "available_action_indices": model_action_indices,
        "model_action_indices": model_action_indices,
        "model_output_count": len(model_action_indices),
        "target_encoding": "binary_zero_one",
        "inference_reconstruction": (
            "fit_only_event_conditional_expected_base_net_bps"
        ),
        "validation_or_test_statistics_used": False,
        "action_statistics": action_statistics,
    }


def validate_joint_policy_transform(
    transform: Mapping[str, Any],
    *,
    action_count: int,
    expected_row_count: int | None = None,
) -> List[Dict[str, Any]]:
    if not (
        isinstance(transform, dict)
        and transform.get("method")
        == "fit_only_stress_profitability_event_v5"
        and transform.get("training_objective")
        == "independent_stress_cost_profitable_event"
        and transform.get("event_definition")
        == "executable_base_net_return_bps_gt_stress_incremental_cost_bps"
        and transform.get("target_encoding") == "binary_zero_one"
        and transform.get("inference_reconstruction")
        == "fit_only_event_conditional_expected_base_net_bps"
        and transform.get("validation_or_test_statistics_used") is False
    ):
        raise ValueError("joint-policy transform contract is invalid")
    hurdle = float(transform.get("stress_incremental_cost_bps"))
    minimum_profitable = int(transform.get("minimum_profitable_events_per_action", -1))
    minimum_unprofitable = int(
        transform.get("minimum_unprofitable_events_per_action", -1)
    )
    actions = transform.get("actions")
    available_action_indices = transform.get("available_action_indices")
    model_action_indices = transform.get("model_action_indices")
    model_output_count = int(transform.get("model_output_count", -1))
    items = transform.get("action_statistics")
    if (
        not math.isfinite(hurdle)
        or hurdle <= 0.0
        or minimum_profitable < 1
        or minimum_unprofitable < 1
        or not isinstance(actions, list)
        or len(actions) != int(action_count)
        or not isinstance(available_action_indices, list)
        or not isinstance(model_action_indices, list)
        or model_output_count != len(model_action_indices)
        or not isinstance(items, list)
        or len(items) != int(action_count)
    ):
        raise ValueError("joint-policy transform shape/hurdle is invalid")
    for item in actions:
        if not (
            isinstance(item, dict)
            and str(item.get("direction") or "") in {"long", "short"}
            and int(item.get("horizon_seconds") or 0) > 0
        ):
            raise ValueError("joint-policy transform action contract is invalid")
    normalized_available_indices = [
        int(value) for value in available_action_indices
    ]
    normalized_model_indices = [int(value) for value in model_action_indices]
    if not (
        normalized_available_indices
        == sorted(set(normalized_available_indices))
        and all(
            0 <= value < int(action_count)
            for value in normalized_available_indices
        )
        and normalized_model_indices == sorted(set(normalized_model_indices))
        and all(0 <= value < int(action_count) for value in normalized_model_indices)
        and set(normalized_model_indices).issubset(normalized_available_indices)
    ):
        raise ValueError("joint-policy model action indices are invalid")
    fit_row_count: int | None = None
    expected_model_indices: List[int] = []
    for action_index, item in enumerate(items):
        if not (
            isinstance(item, dict)
            and int(item.get("action_index", -1)) == action_index
        ):
            raise ValueError("joint-policy action statistics are invalid")
        try:
            row_count = int(item.get("row_count"))
            raw_mean = float(item.get("raw_mean_base_net_bps"))
            raw_minimum = float(item.get("raw_minimum_base_net_bps"))
            raw_maximum = float(item.get("raw_maximum_base_net_bps"))
            profitable_count = int(item.get("stress_profitable_count"))
            unprofitable_count = int(item.get("stress_unprofitable_count"))
            profitable_rate = float(item.get("stress_profitable_rate"))
        except (TypeError, ValueError) as exc:
            raise ValueError("joint-policy action statistic type is invalid") from exc
        profitable_mean_raw = item.get("stress_profitable_mean_base_net_bps")
        unprofitable_mean_raw = item.get(
            "stress_unprofitable_mean_base_net_bps"
        )
        profitable_mean = (
            float(profitable_mean_raw) if profitable_mean_raw is not None else None
        )
        unprofitable_mean = (
            float(unprofitable_mean_raw)
            if unprofitable_mean_raw is not None
            else None
        )
        if fit_row_count is None:
            fit_row_count = row_count
        expected_rate = profitable_count / row_count if row_count > 0 else float("nan")
        learnable = bool(
            profitable_count >= minimum_profitable
            and unprofitable_count >= minimum_unprofitable
            and profitable_mean is not None
            and unprofitable_mean is not None
            and math.isfinite(profitable_mean)
            and math.isfinite(unprofitable_mean)
            and profitable_mean > unprofitable_mean
        )
        if not (
            row_count > 0
            and row_count == fit_row_count
            and (expected_row_count is None or row_count == int(expected_row_count))
            and 0 <= profitable_count <= row_count
            and unprofitable_count == row_count - profitable_count
            and math.isclose(
                profitable_rate,
                expected_rate,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
            and all(
                math.isfinite(value)
                for value in (raw_mean, raw_minimum, raw_maximum)
            )
            and raw_minimum <= raw_mean <= raw_maximum
            and (profitable_count > 0) is (profitable_mean is not None)
            and (unprofitable_count > 0) is (unprofitable_mean is not None)
            and (
                profitable_mean is None
                or (math.isfinite(profitable_mean) and profitable_mean > hurdle)
            )
            and (
                unprofitable_mean is None
                or (
                    math.isfinite(unprofitable_mean)
                    and unprofitable_mean <= hurdle
                )
            )
            and item.get("learnable") is learnable
        ):
            raise ValueError("joint-policy action statistic contract failed")
        if learnable:
            expected_model_indices.append(action_index)
    if expected_model_indices != normalized_available_indices:
        raise ValueError("joint-policy active action mapping contract failed")
    return items


def select_model_action_indices(
    transform: Mapping[str, Any], action_indices: Sequence[int]
) -> Dict[str, Any]:
    """Bind fit-only statistics to the exact action models being serialized."""
    action_count = len(transform.get("action_statistics", []))
    validate_joint_policy_transform(transform, action_count=action_count)
    selected = [int(value) for value in action_indices]
    available = [int(value) for value in transform["available_action_indices"]]
    if not (
        selected == sorted(set(selected))
        and selected
        and set(selected).issubset(available)
    ):
        raise ValueError("selected joint-policy model actions are invalid")
    frozen = json.loads(json.dumps(transform))
    frozen["model_action_indices"] = selected
    frozen["model_output_count"] = len(selected)
    validate_joint_policy_transform(frozen, action_count=action_count)
    return frozen


def transform_joint_policy_targets(
    outcomes: np.ndarray, transform: Mapping[str, Any]
) -> np.ndarray:
    """Apply the frozen fit-domain event definition to another domain."""
    matrix = np.asarray(outcomes, dtype=np.float64)
    if matrix.ndim != 2:
        raise ValueError("joint-policy target transform shape mismatch")
    statistics_by_action = validate_joint_policy_transform(
        transform, action_count=matrix.shape[1]
    )
    if not np.all(np.isfinite(matrix)):
        raise ValueError("joint-policy target outcomes must be finite")
    columns: List[np.ndarray] = []
    for action_index in transform["model_action_indices"]:
        columns.append(
            (
                matrix[:, int(action_index)]
                > float(transform["stress_incremental_cost_bps"])
            ).astype(np.float64)
        )
    return (
        np.column_stack(columns)
        if columns
        else np.empty((len(matrix), 0), dtype=np.float64)
    )


def reconstruct_base_net_scores(
    raw_prediction: np.ndarray, transform: Mapping[str, Any]
) -> np.ndarray:
    """Convert event probabilities to fit-only expected base-net bps."""
    prediction = np.asarray(raw_prediction, dtype=np.float64)
    action_count = len(transform.get("action_statistics", []))
    statistics_by_action = validate_joint_policy_transform(
        transform, action_count=action_count
    )
    active_indices = [int(value) for value in transform["model_action_indices"]]
    if prediction.ndim == 1:
        prediction = (
            prediction.reshape(-1, 1)
            if len(active_indices) == 1
            else prediction.reshape(1, -1)
        )
    if prediction.ndim != 2 or prediction.shape[1] != len(active_indices):
        raise ValueError("joint-policy active prediction shape mismatch")
    if not (
        np.all(np.isfinite(prediction))
        and np.all(prediction >= -1e-12)
        and np.all(prediction <= 1.0 + 1e-12)
    ):
        raise ValueError("joint-policy active predictions are invalid")
    result = np.empty((len(prediction), action_count), dtype=np.float64)
    model_column_by_action = {
        action_index: model_column
        for model_column, action_index in enumerate(active_indices)
    }
    for action_index, item in enumerate(statistics_by_action):
        if action_index in model_column_by_action:
            probability = np.clip(
                prediction[:, model_column_by_action[action_index]], 0.0, 1.0
            )
            unprofitable_mean = float(
                item["stress_unprofitable_mean_base_net_bps"]
            )
            profitable_mean = float(item["stress_profitable_mean_base_net_bps"])
            result[:, action_index] = (
                unprofitable_mean
                + probability * (profitable_mean - unprofitable_mean)
            )
        else:
            result[:, action_index] = float(item["raw_mean_base_net_bps"])
    if not np.all(np.isfinite(result)):
        raise ValueError("reconstructed base-net policy score is non-finite")
    return result


def base_net_score_to_event_probability(
    score_bps: float,
    transform: Mapping[str, Any],
    action_index: int,
) -> float:
    """Invert an active action's fit-only affine economic reconstruction."""
    statistics = validate_joint_policy_transform(
        transform, action_count=len(transform.get("action_statistics", []))
    )
    active_indices = [int(value) for value in transform["model_action_indices"]]
    normalized_action_index = int(action_index)
    if normalized_action_index not in active_indices:
        raise ValueError("event probability threshold action is not modeled")
    score = float(score_bps)
    item = statistics[normalized_action_index]
    lower = float(item["stress_unprofitable_mean_base_net_bps"])
    upper = float(item["stress_profitable_mean_base_net_bps"])
    if not math.isfinite(score) or not upper > lower:
        raise ValueError("event probability threshold reconstruction is invalid")
    probability = (score - lower) / (upper - lower)
    if not -1e-9 <= probability <= 1.0 + 1e-9:
        raise ValueError("event probability threshold is outside fit score range")
    return float(np.clip(probability, 0.0, 1.0))


def predict_base_net_scores(
    model: Any, features: np.ndarray, transform: Mapping[str, Any]
) -> Tuple[np.ndarray, np.ndarray]:
    """Run independent event classifiers and reconstruct comparable bps scores."""
    active_count = int(transform.get("model_output_count", -1))
    models = list(model) if isinstance(model, (list, tuple)) else [model]
    if active_count <= 0 or len(models) != active_count:
        raise ValueError("independent action model count mismatch")
    raw_columns: List[np.ndarray] = []
    for action_model in models:
        probabilities = np.asarray(
            action_model.predict_proba(features), dtype=np.float64
        )
        if probabilities.ndim == 2 and probabilities.shape[1] == 2:
            column = probabilities[:, 1]
        elif probabilities.ndim == 1:
            column = probabilities
        else:
            raise ValueError("independent action probability shape mismatch")
        if column.ndim != 1 or len(column) != len(features):
            raise ValueError("independent action model prediction shape mismatch")
        raw_columns.append(column)
    raw_prediction = np.column_stack(raw_columns)
    return reconstruct_base_net_scores(raw_prediction, transform), raw_prediction


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
    allowed_action_indices: Sequence[int] | None = None,
) -> Dict[str, Any]:
    action_count = len(actions)
    allowed = (
        list(range(action_count))
        if allowed_action_indices is None
        else [int(value) for value in allowed_action_indices]
    )
    if not (
        len(set(allowed)) == len(allowed)
        and all(0 <= value < action_count for value in allowed)
    ):
        raise ValueError("allowed joint-policy action indices are invalid")
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
        if not allowed:
            continue
        action_index = allowed[
            int(np.argmax(row_prediction[np.asarray(allowed, dtype=np.int64)]))
        ]
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


def evaluate_hindsight_oracle(
    *,
    timestamps: np.ndarray,
    realized_base: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    base_cost_bps: float,
    stress_cost_multiplier: float,
    execution_latency_seconds: int,
) -> Dict[str, Any]:
    """Measure an explicitly non-promotional OOS opportunity upper bound.

    The oracle uses realized outcomes as predictions, selects the best action at
    each timestamp, and trades only when the realized stress-net edge is
    strictly positive.  It preserves the production non-overlap rule, but it
    is hindsight by construction and must never influence candidate selection.
    """
    base_cost = float(base_cost_bps)
    multiplier = float(stress_cost_multiplier)
    if not math.isfinite(base_cost) or base_cost <= 0.0:
        raise ValueError("hindsight oracle base cost must be positive")
    if not math.isfinite(multiplier) or multiplier <= 1.0:
        raise ValueError("hindsight oracle stress multiplier must exceed one")
    stress_increment = base_cost * (multiplier - 1.0)
    strict_threshold = math.nextafter(stress_increment, math.inf)
    objective = evaluate_joint_policy(
        timestamps=timestamps,
        prediction=np.asarray(realized_base, dtype=np.float64),
        realized_base=np.asarray(realized_base, dtype=np.float64),
        actions=actions,
        threshold_bps=strict_threshold,
        base_cost_bps=base_cost,
        stress_cost_multiplier=multiplier,
        execution_latency_seconds=execution_latency_seconds,
    )
    objective.pop("base_edges_bps", None)
    objective.pop("stress_edges_bps", None)
    return {
        "method": "non_overlapping_oos_hindsight_joint_action_oracle",
        "selection_scope": "oos_hindsight_upper_bound",
        "strict_positive_stress_net_required": True,
        "minimum_base_net_edge_bps": stress_increment,
        "promotion_evidence": False,
        "promotion_eligible": False,
        "objective": objective,
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
    allowed_action_indices: Sequence[int] | None = None,
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
            allowed_action_indices=allowed_action_indices,
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


def build_learnability_diagnostic(
    *,
    split_reports: Sequence[Mapping[str, Any]],
    required_split_count: int,
    permutation_trials: int,
    permutation_seed: int,
    permutation_minimum_excess_lcb_bps: float,
    minimum_oracle_trades: int,
    minimum_positive_splits_ratio: float,
) -> Dict[str, Any]:
    """Separate opportunity existence from model learnability without gating.

    The oracle asks whether profitable OOS episodes existed at all.  The
    diagnostic policy then uses the already-declared non-promotional nested
    threshold and compares its OOS timing against deterministic permutations.
    Neither result is allowed to alter the economic screen or promotion state.
    """
    required = int(required_split_count)
    trials = int(permutation_trials)
    if required <= 0 or trials <= 0:
        raise ValueError("learnability diagnostic split/trial counts must be positive")
    minimum_trades = int(minimum_oracle_trades)
    minimum_positive_ratio = float(minimum_positive_splits_ratio)
    if minimum_trades <= 0:
        raise ValueError("learnability diagnostic minimum oracle trades must be positive")
    if not 0.0 <= minimum_positive_ratio <= 1.0:
        raise ValueError("learnability diagnostic positive split ratio is invalid")

    oracle_base_means: List[float] = []
    oracle_stress_means: List[float] = []
    oracle_trade_count = 0
    oracle_action_counts: Dict[str, int] = {}
    diagnostic_base_means: List[float] = []
    diagnostic_stress_means: List[float] = []
    diagnostic_trade_count = 0
    diagnostic_action_counts: Dict[str, int] = {}
    control_base_means: List[List[float]] = [[] for _ in range(trials)]
    control_stress_means: List[List[float]] = [[] for _ in range(trials)]

    def merge_counts(target: Dict[str, int], source: Any) -> None:
        if not isinstance(source, Mapping):
            return
        for key, raw_value in source.items():
            value = int(raw_value)
            if value > 0:
                target[str(key)] = target.get(str(key), 0) + value

    for item in split_reports:
        if item.get("status") != "trained":
            continue
        oracle = item.get("hindsight_oracle", {})
        oracle_objective = (
            oracle.get("objective", {}) if isinstance(oracle, Mapping) else {}
        )
        oracle_base = oracle_objective.get("base_cost", {})
        oracle_stress = oracle_objective.get("stress_cost", {})
        if isinstance(oracle_base, Mapping) and isinstance(oracle_stress, Mapping):
            base_mean = oracle_base.get("mean_bps")
            stress_mean = oracle_stress.get("mean_bps")
            if base_mean is not None and stress_mean is not None:
                oracle_base_means.append(float(base_mean))
                oracle_stress_means.append(float(stress_mean))
                oracle_trade_count += int(oracle_base.get("count") or 0)
                merge_counts(oracle_action_counts, oracle_objective.get("action_counts"))

        diagnostic = item.get("diagnostic_oos_objective", {})
        diagnostic_base = (
            diagnostic.get("base_cost", {})
            if isinstance(diagnostic, Mapping)
            else {}
        )
        diagnostic_stress = (
            diagnostic.get("stress_cost", {})
            if isinstance(diagnostic, Mapping)
            else {}
        )
        if isinstance(diagnostic_base, Mapping) and isinstance(
            diagnostic_stress, Mapping
        ):
            base_mean = diagnostic_base.get("mean_bps")
            stress_mean = diagnostic_stress.get("mean_bps")
            if base_mean is not None and stress_mean is not None:
                diagnostic_base_means.append(float(base_mean))
                diagnostic_stress_means.append(float(stress_mean))
                diagnostic_trade_count += int(diagnostic_base.get("count") or 0)
                merge_counts(
                    diagnostic_action_counts, diagnostic.get("action_counts")
                )

        controls = item.get(
            "diagnostic_oos_prediction_permutation_controls", []
        )
        if not isinstance(controls, list) or len(controls) != trials:
            continue
        for trial, control in enumerate(controls):
            if not isinstance(control, Mapping) or int(control.get("trial", -1)) != trial:
                continue
            base = control.get("base_cost", {})
            stress = control.get("stress_cost", {})
            if not isinstance(base, Mapping) or not isinstance(stress, Mapping):
                continue
            base_mean = base.get("mean_bps")
            stress_mean = stress.get("mean_bps")
            if base_mean is not None:
                control_base_means[trial].append(float(base_mean))
            if stress_mean is not None:
                control_stress_means[trial].append(float(stress_mean))

    oracle_base_summary = summarize_edges(oracle_base_means)
    oracle_stress_summary = summarize_edges(oracle_stress_means)
    diagnostic_base_summary = summarize_edges(diagnostic_base_means)
    diagnostic_stress_summary = summarize_edges(diagnostic_stress_means)
    oracle_positive_split_ratio = (
        sum(value > 0.0 for value in oracle_stress_means)
        / len(oracle_stress_means)
        if oracle_stress_means
        else 0.0
    )
    oracle_fully_verifiable = bool(
        oracle_base_summary["count"] == required
        and oracle_stress_summary["count"] == required
    )
    oracle_opportunity_proven = bool(
        oracle_fully_verifiable
        and oracle_trade_count >= minimum_trades
        and oracle_positive_split_ratio >= minimum_positive_ratio
        and (oracle_stress_summary["lcb_bps"] or float("-inf")) > 0.0
    )
    signal_control = summarize_prediction_permutation_controls(
        base_means_by_trial=control_base_means,
        stress_means_by_trial=control_stress_means,
        required_split_count=required,
        candidate_base_split_lcb_bps=diagnostic_base_summary["lcb_bps"],
        candidate_stress_split_lcb_bps=diagnostic_stress_summary["lcb_bps"],
        minimum_excess_lcb_bps=float(permutation_minimum_excess_lcb_bps),
        seed=int(permutation_seed),
    )
    diagnostic_fully_verifiable = bool(
        diagnostic_base_summary["count"] == required
        and diagnostic_stress_summary["count"] == required
        and signal_control["fully_verifiable"]
    )
    fully_verifiable = oracle_fully_verifiable and diagnostic_fully_verifiable
    signal_proven = bool(diagnostic_fully_verifiable and signal_control["passed"])
    if not fully_verifiable:
        verdict = "INCOMPLETE"
        next_experiment = "complete_oracle_and_diagnostic_null_evidence"
    elif not oracle_opportunity_proven:
        verdict = "ORACLE_OPPORTUNITY_NOT_PROVEN"
        next_experiment = "collect_additional_non_overlapping_market_regimes"
    elif signal_proven:
        verdict = "MODEL_SIGNAL_PROVEN"
        next_experiment = "review_economic_gate_without_using_hindsight_evidence"
    else:
        verdict = "MODEL_SIGNAL_NOT_PROVEN"
        next_experiment = "compare_frozen_target_architectures_on_identical_oos_splits"

    return {
        "schema_version": "microstructure_alpha_learnability_v1",
        "method": "oos_hindsight_oracle_plus_diagnostic_threshold_permutation",
        "fully_verifiable": fully_verifiable,
        "promotion_evidence": False,
        "promotion_eligible": False,
        "influences_development_passed": False,
        "required_split_count": required,
        "oracle": {
            "method": "non_overlapping_oos_hindsight_joint_action_oracle",
            "fully_verifiable": oracle_fully_verifiable,
            "opportunity_proven": oracle_opportunity_proven,
            "minimum_trade_count": minimum_trades,
            "minimum_positive_splits_ratio": minimum_positive_ratio,
            "trade_count": oracle_trade_count,
            "positive_stress_edge_split_ratio": oracle_positive_split_ratio,
            "oos_base_cost_by_split": oracle_base_summary,
            "oos_stress_cost_by_split": oracle_stress_summary,
            "action_counts": oracle_action_counts,
        },
        "diagnostic_policy": {
            "selection_scope": "nested_validation_non_promotional_diagnostic",
            "fully_verifiable": diagnostic_fully_verifiable,
            "signal_proven": signal_proven,
            "trade_count": diagnostic_trade_count,
            "oos_base_cost_by_split": diagnostic_base_summary,
            "oos_stress_cost_by_split": diagnostic_stress_summary,
            "action_counts": diagnostic_action_counts,
            "prediction_permutation_control": signal_control,
        },
        "verdict": verdict,
        "next_experiment": next_experiment,
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
    allowed_action_indices: Sequence[int] | None = None,
) -> Dict[str, Any]:
    prediction_matrix = np.asarray(prediction, dtype=np.float64)
    if (
        prediction_matrix.ndim != 2
        or prediction_matrix.shape[1] != len(actions)
    ):
        raise ValueError("nested action calibration prediction shape mismatch")
    allowed = (
        list(range(len(actions)))
        if allowed_action_indices is None
        else [int(value) for value in allowed_action_indices]
    )
    if not (
        allowed
        and len(set(allowed)) == len(allowed)
        and all(0 <= value < len(actions) for value in allowed)
    ):
        raise ValueError("nested action calibration allowed actions are invalid")
    maximum = np.max(prediction_matrix[:, allowed], axis=1)
    finite = maximum[np.isfinite(maximum)]
    if len(finite) == 0:
        return {"selected": None, "candidates": [], "reason": "no_finite_predictions"}
    candidates: List[Dict[str, Any]] = []
    action_score_distributions: List[Dict[str, Any]] = []
    for action_index in allowed:
        action = actions[action_index]
        action_scores = prediction_matrix[:, action_index]
        finite_action_scores = action_scores[np.isfinite(action_scores)]
        action_score_distributions.append(
            {
                "action_index": action_index,
                "direction": str(action["direction"]),
                "horizon_seconds": int(action["horizon_seconds"]),
                "distribution": summarize_score_distribution(finite_action_scores),
            }
        )
        if len(finite_action_scores) == 0:
            continue
        thresholds = sorted(
            {
                # CatBoost shrinkage can preserve useful ranking while shifting
                # all scores below zero.  Each action gets an independent rank
                # threshold so a high-base-rate 300s action cannot suppress a
                # rarer short-horizon hypothesis before economic validation.
                float(np.quantile(finite_action_scores, quantile))
                for quantile in quantiles
            }
        )
        for threshold in thresholds:
            report = evaluate_joint_policy(
                timestamps=timestamps,
                prediction=prediction_matrix,
                realized_base=realized_base,
                actions=actions,
                threshold_bps=threshold,
                base_cost_bps=base_cost_bps,
                stress_cost_multiplier=stress_cost_multiplier,
                execution_latency_seconds=execution_latency_seconds,
                allowed_action_indices=[action_index],
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
                    "action_index": action_index,
                    "direction": str(action["direction"]),
                    "horizon_seconds": int(action["horizon_seconds"]),
                    "threshold_bps": threshold,
                    "trade_count": base["count"],
                    "mean_base_net_bps": base["mean_bps"],
                    "base_net_lcb_bps": base["lcb_bps"],
                    "stress_net_lcb_bps": stress["lcb_bps"],
                    "action_counts": report["action_counts"],
                    "viable": viable,
                }
            )
    viable_candidates = [item for item in candidates if item["viable"]]
    diagnostic_candidates = [
        item
        for item in candidates
        if int(item["trade_count"]) >= int(min_trades)
        and item["base_net_lcb_bps"] is not None
        and item["stress_net_lcb_bps"] is not None
    ]
    selected = (
        max(
            viable_candidates,
            key=lambda item: (
                float(item["stress_net_lcb_bps"]),
                float(item["base_net_lcb_bps"]),
                -float(item["threshold_bps"]),
                -int(item["action_index"]),
            ),
        )
        if viable_candidates
        else None
    )
    diagnostic_selected = (
        max(
            diagnostic_candidates,
            key=lambda item: (
                float(item["stress_net_lcb_bps"]),
                float(item["base_net_lcb_bps"]),
                -float(item["threshold_bps"]),
                -int(item["action_index"]),
            ),
        )
        if diagnostic_candidates
        else None
    )
    return {
        "selected": selected,
        "diagnostic_selected": diagnostic_selected,
        "diagnostic_selection_contract": (
            "best_nested_validation_stress_lcb_with_minimum_trades;"
            "non_promotional_never_used_by_economic_gate"
        ),
        "candidates": candidates,
        "score_distribution": summarize_score_distribution(finite),
        "action_score_distributions": action_score_distributions,
        "score_threshold_floor_bps": None,
        "reason": "positive_stress_lcb" if selected else "no_positive_stress_lcb_threshold",
    }


def select_nested_joint_threshold(
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
    score_units: str,
    allowed_action_indices: Sequence[int] | None = None,
) -> Dict[str, Any]:
    prediction_matrix = np.asarray(prediction, dtype=np.float64)
    if (
        prediction_matrix.ndim != 2
        or prediction_matrix.shape[1] != len(actions)
    ):
        raise ValueError("joint diagnostic prediction shape mismatch")
    allowed = (
        list(range(len(actions)))
        if allowed_action_indices is None
        else [int(value) for value in allowed_action_indices]
    )
    if not (
        allowed
        and len(set(allowed)) == len(allowed)
        and all(0 <= value < len(actions) for value in allowed)
    ):
        raise ValueError("joint diagnostic allowed actions are invalid")
    row_maximum = np.max(prediction_matrix[:, allowed], axis=1)
    finite = row_maximum[np.isfinite(row_maximum)]
    if len(finite) == 0:
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
        objective = evaluate_joint_policy(
            timestamps=timestamps,
            prediction=prediction_matrix,
            realized_base=realized_base,
            actions=actions,
            threshold_bps=threshold,
            base_cost_bps=base_cost_bps,
            stress_cost_multiplier=stress_cost_multiplier,
            execution_latency_seconds=execution_latency_seconds,
            allowed_action_indices=allowed,
        )
        base = objective["base_cost"]
        stress = objective["stress_cost"]
        viable = bool(
            base["count"] >= int(min_trades)
            and (base["lcb_bps"] or float("-inf")) > 0.0
            and (stress["lcb_bps"] or float("-inf")) > 0.0
        )
        candidates.append(
            {
                "score_threshold": threshold,
                "score_units": str(score_units),
                "trade_count": int(base["count"]),
                "mean_base_net_bps": base["mean_bps"],
                "base_net_lcb_bps": base["lcb_bps"],
                "stress_net_lcb_bps": stress["lcb_bps"],
                "action_counts": objective["action_counts"],
                "viable": viable,
            }
        )
    viable_candidates = [item for item in candidates if item["viable"]]
    diagnostic_candidates = [
        item
        for item in candidates
        if int(item["trade_count"]) >= int(min_trades)
        and item["base_net_lcb_bps"] is not None
        and item["stress_net_lcb_bps"] is not None
    ]

    def candidate_key(item: Mapping[str, Any]) -> Tuple[float, float, float]:
        return (
            float(item["stress_net_lcb_bps"]),
            float(item["base_net_lcb_bps"]),
            -float(item["score_threshold"]),
        )

    return {
        "selected": max(viable_candidates, key=candidate_key)
        if viable_candidates
        else None,
        "diagnostic_selected": max(diagnostic_candidates, key=candidate_key)
        if diagnostic_candidates
        else None,
        "diagnostic_selection_contract": (
            "best_nested_validation_joint_action_stress_lcb_with_minimum_trades;"
            "non_promotional_never_used_by_economic_gate"
        ),
        "candidates": candidates,
        "score_units": str(score_units),
        "score_distribution": summarize_numeric_distribution(finite),
        "allowed_action_indices": allowed,
        "reason": (
            "positive_stress_lcb"
            if viable_candidates
            else "no_positive_stress_lcb_threshold"
        ),
    }


def aggregate_target_architecture_comparison(
    *,
    split_reports: Sequence[Mapping[str, Any]],
    architecture_ids: Sequence[str],
    required_split_count: int,
    permutation_trials: int,
    permutation_seed: int,
    permutation_minimum_excess_lcb_bps: float,
    frozen_contract_failures: Sequence[str],
) -> Dict[str, Any]:
    ordered_architectures = [str(value) for value in architecture_ids]
    required = int(required_split_count)
    trials = int(permutation_trials)
    if (
        not ordered_architectures
        or len(set(ordered_architectures)) != len(ordered_architectures)
        or required <= 0
        or trials <= 0
    ):
        raise ValueError("target architecture comparison inputs are invalid")
    report_by_split: Dict[int, Mapping[str, Any]] = {}
    duplicate_split_ids: set[int] = set()
    for report in split_reports:
        split_id = int(report.get("split_id", -1))
        if split_id in report_by_split:
            duplicate_split_ids.add(split_id)
        report_by_split[split_id] = report

    missing: List[Dict[str, Any]] = []
    architecture_summaries: Dict[str, Dict[str, Any]] = {}
    for architecture_offset, architecture_id in enumerate(ordered_architectures):
        base_means: List[float] = []
        stress_means: List[float] = []
        trade_count = 0
        action_counts: Dict[str, int] = {}
        control_base_means: List[List[float]] = [[] for _ in range(trials)]
        control_stress_means: List[List[float]] = [[] for _ in range(trials)]
        complete_split_count = 0
        zero_trade_split_ids: List[int] = []
        for split_id in range(required):
            reason: str | None = None
            normalized_control_means: List[Tuple[float, float]] = []
            zero_trade_split = False
            split_report = report_by_split.get(split_id)
            architecture_report: Mapping[str, Any] = {}
            if split_id in duplicate_split_ids:
                reason = "duplicate_split_report"
            elif not isinstance(split_report, Mapping):
                reason = "missing_split_report"
            else:
                architectures = split_report.get("architectures", {})
                if isinstance(architectures, Mapping):
                    raw_architecture_report = architectures.get(architecture_id)
                    if isinstance(raw_architecture_report, Mapping):
                        architecture_report = raw_architecture_report
                    else:
                        reason = "missing_architecture_report"
                else:
                    reason = "missing_architecture_report"
            if reason is None and architecture_report.get("status") != "evaluated":
                status = str(architecture_report.get("status") or "missing_status")
                detail = str(architecture_report.get("reason") or "")
                reason = f"{status}:{detail}" if detail else status
            objective = architecture_report.get("oos_objective", {})
            base = objective.get("base_cost", {}) if isinstance(objective, Mapping) else {}
            stress = (
                objective.get("stress_cost", {})
                if isinstance(objective, Mapping)
                else {}
            )
            base_mean = base.get("mean_bps") if isinstance(base, Mapping) else None
            stress_mean = (
                stress.get("mean_bps") if isinstance(stress, Mapping) else None
            )
            if reason is None and (base_mean is None or stress_mean is None):
                if (
                    isinstance(base, Mapping)
                    and isinstance(stress, Mapping)
                    and base.get("count") == 0
                    and stress.get("count") == 0
                    and base_mean is None
                    and stress_mean is None
                ):
                    # A nested-validation threshold that takes no OOS position
                    # is an observed fail-closed policy result, not missing
                    # evidence.  Its split return is exactly zero, while the
                    # explicit zero-trade marker prevents signal promotion.
                    base_mean = 0.0
                    stress_mean = 0.0
                    zero_trade_split = True
                else:
                    reason = "missing_actual_economics"
            controls = architecture_report.get(
                "oos_prediction_permutation_controls", []
            )
            if reason is None and (
                not isinstance(controls, list) or len(controls) != trials
            ):
                reason = "missing_permutation_trials"
            if reason is None:
                for trial, control in enumerate(controls):
                    if (
                        not isinstance(control, Mapping)
                        or int(control.get("trial", -1)) != trial
                    ):
                        reason = "invalid_permutation_trial_order"
                        break
                    control_base = control.get("base_cost", {})
                    control_stress = control.get("stress_cost", {})
                    control_base_mean = (
                        control_base.get("mean_bps")
                        if isinstance(control_base, Mapping)
                        else None
                    )
                    control_stress_mean = (
                        control_stress.get("mean_bps")
                        if isinstance(control_stress, Mapping)
                        else None
                    )
                    if control_base_mean is None or control_stress_mean is None:
                        if (
                            isinstance(control_base, Mapping)
                            and isinstance(control_stress, Mapping)
                            and control_base.get("count") == 0
                            and control_stress.get("count") == 0
                            and control_base_mean is None
                            and control_stress_mean is None
                        ):
                            control_base_mean = 0.0
                            control_stress_mean = 0.0
                        else:
                            reason = "missing_permutation_economics"
                            break
                    normalized_control_means.append(
                        (float(control_base_mean), float(control_stress_mean))
                    )
            if reason is not None:
                missing.append(
                    {
                        "architecture_id": architecture_id,
                        "split_id": split_id,
                        "reason": reason,
                    }
                )
                continue
            complete_split_count += 1
            if zero_trade_split:
                zero_trade_split_ids.append(split_id)
            base_means.append(float(base_mean))
            stress_means.append(float(stress_mean))
            trade_count += int(base.get("count") or 0)
            raw_action_counts = objective.get("action_counts", {})
            if isinstance(raw_action_counts, Mapping):
                for key, raw_count in raw_action_counts.items():
                    count = int(raw_count)
                    action_counts[str(key)] = action_counts.get(str(key), 0) + count
            for trial, (control_base_mean, control_stress_mean) in enumerate(
                normalized_control_means
            ):
                control_base_means[trial].append(control_base_mean)
                control_stress_means[trial].append(control_stress_mean)
        base_summary = summarize_edges(base_means)
        stress_summary = summarize_edges(stress_means)
        control_summary = summarize_prediction_permutation_controls(
            base_means_by_trial=control_base_means,
            stress_means_by_trial=control_stress_means,
            required_split_count=required,
            candidate_base_split_lcb_bps=base_summary["lcb_bps"],
            candidate_stress_split_lcb_bps=stress_summary["lcb_bps"],
            minimum_excess_lcb_bps=float(permutation_minimum_excess_lcb_bps),
            seed=int(permutation_seed) + architecture_offset * 10_000_019,
        )
        fully_verifiable = bool(
            complete_split_count == required and control_summary["fully_verifiable"]
        )
        signal_proven = bool(
            fully_verifiable
            and not zero_trade_split_ids
            and (base_summary["lcb_bps"] or float("-inf")) > 0.0
            and (stress_summary["lcb_bps"] or float("-inf")) > 0.0
            and control_summary["passed"]
        )
        architecture_summaries[architecture_id] = {
            "fully_verifiable": fully_verifiable,
            "signal_proven": signal_proven,
            "complete_split_count": complete_split_count,
            "required_split_count": required,
            "trade_count": trade_count,
            "zero_trade_split_ids": zero_trade_split_ids,
            "zero_trade_split_contract": (
                "complete_fail_closed_no_position_policy_return_zero;"
                "signal_proven_forbidden"
            ),
            "action_counts": action_counts,
            "oos_base_cost_by_split": base_summary,
            "oos_stress_cost_by_split": stress_summary,
            "prediction_permutation_control": control_summary,
        }
    contract_failures = [str(value) for value in frozen_contract_failures]
    fully_verifiable = bool(
        not contract_failures
        and not missing
        and len(report_by_split) == required
        and all(
            item["fully_verifiable"] for item in architecture_summaries.values()
        )
    )
    proven = [
        architecture_id
        for architecture_id in ordered_architectures
        if architecture_summaries[architecture_id]["signal_proven"]
    ]
    diagnostic_leader_id: str | None = None
    if fully_verifiable and proven:
        order = {value: index for index, value in enumerate(ordered_architectures)}
        diagnostic_leader_id = max(
            proven,
            key=lambda value: (
                float(
                    architecture_summaries[value]["oos_stress_cost_by_split"][
                        "lcb_bps"
                    ]
                ),
                float(
                    architecture_summaries[value]["oos_base_cost_by_split"][
                        "lcb_bps"
                    ]
                ),
                -order[value],
            ),
        )
    if not fully_verifiable:
        conclusion = "INCOMPLETE_TARGET_ARCHITECTURE_COMPARISON"
        next_experiment = "complete_all_frozen_architecture_split_evidence"
    elif diagnostic_leader_id is None:
        conclusion = "NO_TARGET_ARCHITECTURE_SIGNAL_PROVEN"
        next_experiment = "collect_additional_non_overlapping_market_regimes"
    else:
        conclusion = "TARGET_ARCHITECTURE_SIGNAL_DIAGNOSTICALLY_PROVEN"
        next_experiment = "create_immutable_independent_forward_preregistration"
    return {
        "schema_version": TARGET_ARCHITECTURE_COMPARISON_SCHEMA_VERSION,
        "architecture_ids": ordered_architectures,
        "required_split_count": required,
        "permutation_trial_count": trials,
        "fully_verifiable": fully_verifiable,
        "promotion_evidence": False,
        "promotion_eligible": False,
        "influences_development_passed": False,
        "frozen_contract_failures": contract_failures,
        "missing_architecture_splits": missing,
        "architectures": architecture_summaries,
        "diagnostic_leader_id": diagnostic_leader_id,
        "diagnostic_leader_is_preregistered": False,
        "conclusion": conclusion,
        "next_experiment": next_experiment,
    }


def indices_between(timestamps: np.ndarray, start_ms: int, end_ms: int) -> np.ndarray:
    return np.flatnonzero((timestamps >= start_ms) & (timestamps < end_ms))


def minimum_internal_model_selection_rows(
    *,
    minimum_window_rows: int,
    model_selection_window_seconds: int,
    train_window_seconds: int,
) -> int:
    minimum_rows = int(minimum_window_rows)
    selection_seconds = int(model_selection_window_seconds)
    train_seconds = int(train_window_seconds)
    if minimum_rows <= 0 or selection_seconds <= 0 or train_seconds <= 0:
        raise ValueError("internal model-selection minimum inputs must be positive")
    if selection_seconds >= train_seconds:
        raise ValueError("internal model-selection window must be smaller than train window")
    proportional_rows = math.ceil(
        minimum_rows * selection_seconds / train_seconds
    )
    return max(256, proportional_rows)


def build_fit_internal_model_selection_indices(
    timestamps: np.ndarray,
    split: TimeSplit,
    *,
    model_selection_window_seconds: int,
    embargo_seconds: int,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, int]]:
    """Keep the outer nested-validation window untouched by model fitting."""
    selection_end_ms = int(split.fit_end_ms)
    selection_start_ms = (
        selection_end_ms - int(model_selection_window_seconds) * 1000
    )
    model_fit_end_ms = selection_start_ms - int(embargo_seconds) * 1000
    if model_fit_end_ms <= int(split.fit_start_ms):
        raise ValueError("fit-internal model-selection window exhausts training data")
    model_fit_indices = indices_between(
        timestamps, int(split.fit_start_ms), model_fit_end_ms
    )
    model_selection_indices = indices_between(
        timestamps, selection_start_ms, selection_end_ms
    )
    return model_fit_indices, model_selection_indices, {
        "model_fit_start_ms": int(split.fit_start_ms),
        "model_fit_end_ms": model_fit_end_ms,
        "model_selection_start_ms": selection_start_ms,
        "model_selection_end_ms": selection_end_ms,
        "embargo_seconds": int(embargo_seconds),
    }


def build_model(args: argparse.Namespace, action_index: int = 0) -> Any:
    if CatBoostClassifier is None:
        raise RuntimeError("catboost is required; use ai-trade-research image")
    return CatBoostClassifier(
        loss_function="Logloss",
        eval_metric="AUC",
        boost_from_average=True,
        iterations=int(args.iterations),
        depth=int(args.depth),
        learning_rate=float(args.learning_rate),
        l2_leaf_reg=float(args.l2_leaf_reg),
        random_strength=float(args.random_strength),
        random_seed=int(args.random_seed) + int(action_index) * 1009,
        allow_writing_files=False,
        verbose=False,
    )


def fit_independent_action_models(
    *,
    fit_features: np.ndarray,
    fit_targets: np.ndarray,
    model_selection_features: np.ndarray,
    model_selection_targets: np.ndarray,
    transform: Mapping[str, Any],
    args: argparse.Namespace,
) -> List[Any]:
    """Fit one tree ensemble using only fit-internal model selection data."""
    action_indices = [int(value) for value in transform["model_action_indices"]]
    fit_matrix = np.asarray(fit_targets, dtype=np.float64)
    model_selection_matrix = np.asarray(
        model_selection_targets, dtype=np.float64
    )
    if not (
        fit_matrix.ndim == 2
        and model_selection_matrix.ndim == 2
        and fit_matrix.shape[1] == len(action_indices)
        and model_selection_matrix.shape[1] == len(action_indices)
    ):
        raise ValueError("independent action training target shape mismatch")
    models: List[Any] = []
    for column, action_index in enumerate(action_indices):
        model = build_model(args, action_index=action_index)
        model.fit(
            fit_features,
            fit_matrix[:, column],
            eval_set=(
                model_selection_features,
                model_selection_matrix[:, column],
            ),
            early_stopping_rounds=int(args.early_stopping_rounds),
            verbose=False,
        )
        models.append(model)
    return models


def build_direct_utility_model(args: argparse.Namespace, action_index: int) -> Any:
    if CatBoostRegressor is None:
        raise RuntimeError("catboost regressor is required; use ai-trade-research image")
    return CatBoostRegressor(
        loss_function="RMSE",
        eval_metric="RMSE",
        iterations=int(args.iterations),
        depth=int(args.depth),
        learning_rate=float(args.learning_rate),
        l2_leaf_reg=float(args.l2_leaf_reg),
        random_strength=float(args.random_strength),
        random_seed=int(args.random_seed) + 200_003 + int(action_index) * 1009,
        allow_writing_files=False,
        verbose=False,
    )


def build_opportunity_model(args: argparse.Namespace) -> Any:
    if CatBoostClassifier is None:
        raise RuntimeError("catboost classifier is required; use ai-trade-research image")
    return CatBoostClassifier(
        loss_function="Logloss",
        eval_metric="Logloss",
        boost_from_average=True,
        iterations=int(args.iterations),
        depth=int(args.depth),
        learning_rate=float(args.learning_rate),
        l2_leaf_reg=float(args.l2_leaf_reg),
        random_strength=float(args.random_strength),
        random_seed=int(args.random_seed) + 400_009,
        allow_writing_files=False,
        verbose=False,
    )


def build_conditional_action_model(
    args: argparse.Namespace, *, action_count: int
) -> Any:
    if CatBoostClassifier is None:
        raise RuntimeError("catboost classifier is required; use ai-trade-research image")
    return CatBoostClassifier(
        loss_function="MultiClass",
        eval_metric="MultiClass",
        classes_count=int(action_count),
        iterations=int(args.iterations),
        depth=int(args.depth),
        learning_rate=float(args.learning_rate),
        l2_leaf_reg=float(args.l2_leaf_reg),
        random_strength=float(args.random_strength),
        random_seed=int(args.random_seed) + 600_011,
        allow_writing_files=False,
        verbose=False,
    )


def build_joint_action_ranker(args: argparse.Namespace) -> Any:
    if CatBoostRanker is None:
        raise RuntimeError("catboost ranker is required; use ai-trade-research image")
    return CatBoostRanker(
        loss_function="QueryRMSE",
        eval_metric="QueryRMSE",
        iterations=int(args.iterations),
        depth=int(args.depth),
        learning_rate=float(args.learning_rate),
        l2_leaf_reg=float(args.l2_leaf_reg),
        random_strength=float(args.random_strength),
        random_seed=int(args.random_seed) + 800_011,
        allow_writing_files=False,
        verbose=False,
    )


def model_best_iteration(model: Any) -> int | None:
    best = model.get_best_iteration()
    return int(best) + 1 if isinstance(best, int) and best >= 0 else None


def fit_direct_utility_models(
    *,
    fit_features: np.ndarray,
    fit_stress_utilities: np.ndarray,
    model_selection_features: np.ndarray,
    model_selection_stress_utilities: np.ndarray,
    args: argparse.Namespace,
) -> List[Any]:
    fit_targets = np.asarray(fit_stress_utilities, dtype=np.float64)
    selection_targets = np.asarray(
        model_selection_stress_utilities, dtype=np.float64
    )
    if (
        fit_targets.ndim != 2
        or selection_targets.ndim != 2
        or fit_targets.shape[1] != selection_targets.shape[1]
    ):
        raise ValueError("direct utility regression target shape mismatch")
    models: List[Any] = []
    for action_index in range(fit_targets.shape[1]):
        model = build_direct_utility_model(args, action_index)
        model.fit(
            fit_features,
            fit_targets[:, action_index],
            eval_set=(
                model_selection_features,
                selection_targets[:, action_index],
            ),
            early_stopping_rounds=int(args.early_stopping_rounds),
            verbose=False,
        )
        models.append(model)
    return models


def predict_direct_utility_models(
    models: Sequence[Any], features: np.ndarray
) -> np.ndarray:
    columns = [
        np.asarray(model.predict(features), dtype=np.float64).reshape(-1)
        for model in models
    ]
    if not columns:
        raise ValueError("direct utility regression has no action models")
    prediction = np.column_stack(columns)
    if not np.all(np.isfinite(prediction)):
        raise ValueError("direct utility regression predictions are invalid")
    return prediction


@dataclasses.dataclass
class TwoStageArchitectureModel:
    action_count: int
    opportunity_model: Any | None
    opportunity_constant: float | None
    conditional_action_model: Any | None
    conditional_action_constant: np.ndarray | None
    fit_class_indices: List[int]


def fit_two_stage_architecture_model(
    *,
    fit_features: np.ndarray,
    fit_stress_utilities: np.ndarray,
    model_selection_features: np.ndarray,
    model_selection_stress_utilities: np.ndarray,
    args: argparse.Namespace,
) -> TwoStageArchitectureModel:
    fit_targets = build_two_stage_targets(fit_stress_utilities)
    selection_targets = build_two_stage_targets(model_selection_stress_utilities)
    action_count = int(np.asarray(fit_stress_utilities).shape[1])
    opportunity_labels = np.asarray(fit_targets["opportunity"], dtype=np.float64)
    unique_opportunity = np.unique(opportunity_labels)
    opportunity_model: Any | None = None
    opportunity_constant: float | None = None
    if len(unique_opportunity) == 1:
        opportunity_constant = float(unique_opportunity[0])
    else:
        opportunity_model = build_opportunity_model(args)
        opportunity_model.fit(
            fit_features,
            opportunity_labels,
            eval_set=(
                model_selection_features,
                np.asarray(selection_targets["opportunity"], dtype=np.float64),
            ),
            early_stopping_rounds=int(args.early_stopping_rounds),
            verbose=False,
        )

    opportunity_rows = np.asarray(
        fit_targets["opportunity_row_indices"], dtype=np.int64
    )
    best_actions = np.asarray(fit_targets["best_action"], dtype=np.int64)
    conditional_model: Any | None = None
    conditional_constant: np.ndarray | None = None
    unique_actions = np.unique(best_actions)
    if len(opportunity_rows) == 0:
        conditional_constant = np.zeros(action_count, dtype=np.float64)
    elif len(unique_actions) == 1:
        conditional_constant = np.zeros(action_count, dtype=np.float64)
        conditional_constant[int(unique_actions[0])] = 1.0
    else:
        conditional_model = build_conditional_action_model(
            args, action_count=action_count
        )
        fit_kwargs: Dict[str, Any] = {
            "early_stopping_rounds": int(args.early_stopping_rounds),
            "verbose": False,
        }
        selection_opportunity_rows = np.asarray(
            selection_targets["opportunity_row_indices"], dtype=np.int64
        )
        if len(selection_opportunity_rows) > 0:
            fit_kwargs["eval_set"] = (
                np.asarray(model_selection_features)[selection_opportunity_rows],
                np.asarray(selection_targets["best_action"], dtype=np.int64),
            )
        else:
            fit_kwargs.pop("early_stopping_rounds", None)
        conditional_model.fit(
            np.asarray(fit_features)[opportunity_rows],
            best_actions,
            **fit_kwargs,
        )
    return TwoStageArchitectureModel(
        action_count=action_count,
        opportunity_model=opportunity_model,
        opportunity_constant=opportunity_constant,
        conditional_action_model=conditional_model,
        conditional_action_constant=conditional_constant,
        fit_class_indices=[int(value) for value in unique_actions],
    )


def predict_binary_positive_probability(model: Any, features: np.ndarray) -> np.ndarray:
    probabilities = np.asarray(model.predict_proba(features), dtype=np.float64)
    classes = [int(value) for value in np.asarray(model.classes_).reshape(-1)]
    if probabilities.ndim != 2 or probabilities.shape[1] != len(classes):
        raise ValueError("binary opportunity prediction shape mismatch")
    if 1 not in classes:
        return np.zeros(len(features), dtype=np.float64)
    return probabilities[:, classes.index(1)]


def predict_two_stage_architecture(
    model: TwoStageArchitectureModel, features: np.ndarray
) -> np.ndarray:
    row_count = len(features)
    if model.opportunity_model is None:
        if model.opportunity_constant is None:
            raise ValueError("two-stage opportunity model is incomplete")
        opportunity = np.full(row_count, model.opportunity_constant, dtype=np.float64)
    else:
        opportunity = predict_binary_positive_probability(
            model.opportunity_model, features
        )
    if model.conditional_action_model is None:
        if model.conditional_action_constant is None:
            raise ValueError("two-stage conditional action model is incomplete")
        conditional = np.tile(model.conditional_action_constant, (row_count, 1))
    else:
        raw = np.asarray(
            model.conditional_action_model.predict_proba(features), dtype=np.float64
        )
        class_labels = [
            int(value)
            for value in np.asarray(
                model.conditional_action_model.classes_
            ).reshape(-1)
        ]
        conditional = restore_ordered_action_probabilities(
            raw,
            class_labels=class_labels,
            action_count=model.action_count,
        )
    scores = opportunity.reshape(-1, 1) * conditional
    if scores.shape != (row_count, model.action_count) or not np.all(
        np.isfinite(scores)
    ):
        raise ValueError("two-stage score matrix is invalid")
    return scores


def fit_joint_ranker_model(
    *,
    fit_features: np.ndarray,
    fit_stress_utilities: np.ndarray,
    model_selection_features: np.ndarray,
    model_selection_stress_utilities: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    args: argparse.Namespace,
) -> Any:
    if Pool is None:
        raise RuntimeError("catboost pool is required; use ai-trade-research image")
    fit_expanded, fit_target, fit_group = build_joint_ranker_dataset(
        fit_features, fit_stress_utilities, actions
    )
    selection_expanded, selection_target, selection_group = (
        build_joint_ranker_dataset(
            model_selection_features,
            model_selection_stress_utilities,
            actions,
        )
    )
    model = build_joint_action_ranker(args)
    model.fit(
        Pool(fit_expanded, fit_target, group_id=fit_group),
        eval_set=Pool(
            selection_expanded,
            selection_target,
            group_id=selection_group,
        ),
        early_stopping_rounds=int(args.early_stopping_rounds),
        verbose=False,
    )
    return model


def predict_joint_ranker_model(
    model: Any,
    *,
    features: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
) -> np.ndarray:
    placeholder = np.zeros((len(features), len(actions)), dtype=np.float64)
    expanded, _, _ = build_joint_ranker_dataset(features, placeholder, actions)
    return reshape_joint_ranker_predictions(
        np.asarray(model.predict(expanded), dtype=np.float64),
        row_count=len(features),
        action_count=len(actions),
    )


def fit_predict_experimental_architecture(
    *,
    architecture_id: str,
    fit_features: np.ndarray,
    fit_stress_utilities: np.ndarray,
    model_selection_features: np.ndarray,
    model_selection_stress_utilities: np.ndarray,
    validation_features: np.ndarray,
    test_features: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    architecture = str(architecture_id)
    if architecture == "direct_stress_utility_regression":
        models = fit_direct_utility_models(
            fit_features=fit_features,
            fit_stress_utilities=fit_stress_utilities,
            model_selection_features=model_selection_features,
            model_selection_stress_utilities=model_selection_stress_utilities,
            args=args,
        )
        return {
            "score_units": "stress_net_utility_bps",
            "validation_prediction": predict_direct_utility_models(
                models, validation_features
            ),
            "test_prediction": predict_direct_utility_models(models, test_features),
            "model_diagnostics": {
                "model_topology": "independent_regressor_per_action",
                "best_iterations_by_action": {
                    str(index): model_best_iteration(model)
                    for index, model in enumerate(models)
                },
            },
        }
    if architecture == "two_stage_opportunity_action":
        model = fit_two_stage_architecture_model(
            fit_features=fit_features,
            fit_stress_utilities=fit_stress_utilities,
            model_selection_features=model_selection_features,
            model_selection_stress_utilities=model_selection_stress_utilities,
            args=args,
        )
        return {
            "score_units": "probability_product",
            "validation_prediction": predict_two_stage_architecture(
                model, validation_features
            ),
            "test_prediction": predict_two_stage_architecture(model, test_features),
            "model_diagnostics": {
                "model_topology": "opportunity_then_conditional_action",
                "fit_class_indices": model.fit_class_indices,
                "opportunity_constant": model.opportunity_constant,
                "conditional_action_constant": (
                    model.conditional_action_constant.tolist()
                    if model.conditional_action_constant is not None
                    else None
                ),
                "opportunity_best_iteration": (
                    model_best_iteration(model.opportunity_model)
                    if model.opportunity_model is not None
                    else None
                ),
                "conditional_action_best_iteration": (
                    model_best_iteration(model.conditional_action_model)
                    if model.conditional_action_model is not None
                    else None
                ),
            },
        }
    if architecture == "joint_action_ranker":
        model = fit_joint_ranker_model(
            fit_features=fit_features,
            fit_stress_utilities=fit_stress_utilities,
            model_selection_features=model_selection_features,
            model_selection_stress_utilities=model_selection_stress_utilities,
            actions=actions,
            args=args,
        )
        return {
            "score_units": "rank_score",
            "validation_prediction": predict_joint_ranker_model(
                model, features=validation_features, actions=actions
            ),
            "test_prediction": predict_joint_ranker_model(
                model, features=test_features, actions=actions
            ),
            "model_diagnostics": {
                "model_topology": "single_grouped_joint_action_ranker",
                "loss_function": "QueryRMSE",
                "best_iteration": model_best_iteration(model),
            },
        }
    raise ValueError(f"unsupported experimental architecture: {architecture}")


def best_iterations_by_action(
    models: Sequence[Any], transform: Mapping[str, Any]
) -> Dict[str, int | None]:
    action_indices = [int(value) for value in transform["model_action_indices"]]
    if len(models) != len(action_indices):
        raise ValueError("independent action best-iteration model count mismatch")
    result: Dict[str, int | None] = {}
    for action_index, model in zip(action_indices, models):
        best = model.get_best_iteration()
        result[str(action_index)] = (
            int(best) + 1 if isinstance(best, int) and best >= 0 else None
        )
    return result


def model_contract(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "library": "catboost",
        "library_version": getattr(catboost, "__version__", None),
        "loss_function": "Logloss",
        "eval_metric": "AUC",
        "boost_from_average": True,
        "ranking_diagnostics": "validation_and_test_roc_auc_average_precision",
        "external_ranking_diagnostics_used_for_fit_or_selection": False,
        "class_weighting": "none",
        "model_topology": "independent_binary_stress_event_classifier_per_action",
        "development_model_scope": "one_model_per_fit_learnable_predeclared_action",
        "early_stopping_scope": "fit_internal_purged_tail",
        "early_stopping_objective": "fit_internal_roc_auc",
        "external_nested_validation_used_for_model_fit_or_early_stopping": False,
        "frozen_model_scope": "single_consensus_action_model",
        "training_target": "model_fit_subwindow_only_stress_cost_profitable_event",
        "estimation_statistic": "stress_profitability_probability",
        "target_encoding": "binary_zero_one",
        "inference_score": (
            "model_fit_subwindow_only_event_conditional_expected_base_net_bps"
        ),
        "policy_selection": "nested_per_action_threshold_then_mode_action_freeze",
        "economic_acceptance_target": "untransformed_executable_base_and_stress_net_return",
        "validation_or_test_target_statistics_used_for_fit": False,
        "iterations": int(args.iterations),
        "depth": int(args.depth),
        "learning_rate": float(args.learning_rate),
        "l2_leaf_reg": float(args.l2_leaf_reg),
        "random_strength": float(args.random_strength),
        "random_seed": int(args.random_seed),
        "per_action_seed": "random_seed_plus_action_index_times_1009",
        "minimum_profitable_events_per_action": int(
            args.min_fit_profitable_events
        ),
        "early_stopping_rounds": int(args.early_stopping_rounds),
    }


def evaluate_target_architecture_split(
    *,
    architecture_id: str,
    split_id: int,
    score_units: str,
    validation_timestamps: np.ndarray,
    validation_prediction: np.ndarray,
    validation_realized_base: np.ndarray,
    test_timestamps: np.ndarray,
    test_prediction: np.ndarray,
    test_realized_base: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    quantiles: Sequence[float],
    min_trades: int,
    base_cost_bps: float,
    stress_cost_multiplier: float,
    execution_latency_seconds: int,
    permutation_trials: int,
    permutation_seed: int,
    allowed_action_indices: Sequence[int] | None = None,
    model_diagnostics: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    calibration = select_nested_joint_threshold(
        timestamps=validation_timestamps,
        prediction=validation_prediction,
        realized_base=validation_realized_base,
        actions=actions,
        quantiles=quantiles,
        min_trades=min_trades,
        base_cost_bps=base_cost_bps,
        stress_cost_multiplier=stress_cost_multiplier,
        execution_latency_seconds=execution_latency_seconds,
        score_units=score_units,
        allowed_action_indices=allowed_action_indices,
    )
    selected = calibration.get("diagnostic_selected")
    if not isinstance(selected, Mapping):
        return {
            "status": "missing_diagnostic_threshold",
            "reason": str(calibration.get("reason") or "no_nested_candidate"),
            "architecture_id": str(architecture_id),
            "score_units": str(score_units),
            "nested_calibration": calibration,
            "model_diagnostics": dict(model_diagnostics or {}),
        }
    threshold = float(selected["score_threshold"])
    objective = evaluate_joint_policy(
        timestamps=test_timestamps,
        prediction=test_prediction,
        realized_base=test_realized_base,
        actions=actions,
        threshold_bps=threshold,
        base_cost_bps=base_cost_bps,
        stress_cost_multiplier=stress_cost_multiplier,
        execution_latency_seconds=execution_latency_seconds,
        allowed_action_indices=allowed_action_indices,
    )
    objective["score_threshold"] = objective.pop("threshold_bps")
    objective["score_units"] = str(score_units)
    objective.pop("base_edges_bps", None)
    objective.pop("stress_edges_bps", None)
    architecture_offset = TARGET_ARCHITECTURE_IDS.index(str(architecture_id))
    controls = evaluate_prediction_permutation_controls(
        timestamps=test_timestamps,
        prediction=test_prediction,
        realized_base=test_realized_base,
        actions=actions,
        threshold_bps=threshold,
        base_cost_bps=base_cost_bps,
        stress_cost_multiplier=stress_cost_multiplier,
        execution_latency_seconds=execution_latency_seconds,
        trials=permutation_trials,
        seed=(
            int(permutation_seed)
            + int(split_id) * 1_000_003
            + architecture_offset * 10_000_019
            + 900_001
        ),
        allowed_action_indices=allowed_action_indices,
    )
    for control in controls:
        control["score_threshold"] = control.pop("threshold_bps", threshold)
        control["score_units"] = str(score_units)
    return {
        "status": "evaluated",
        "architecture_id": str(architecture_id),
        "score_units": str(score_units),
        "nested_calibration": calibration,
        "validation_prediction_score_distribution": summarize_numeric_distribution(
            np.max(np.asarray(validation_prediction), axis=1)
        ),
        "test_prediction_score_distribution": summarize_numeric_distribution(
            np.max(np.asarray(test_prediction), axis=1)
        ),
        "model_diagnostics": dict(model_diagnostics or {}),
        "oos_objective": objective,
        "oos_prediction_permutation_controls": controls,
        "promotion_evidence": False,
        "promotion_eligible": False,
    }


def build_target_architecture_shared_contract(
    *,
    source_assessment_sha256: str,
    feature_names: Sequence[str],
    actions: Sequence[Mapping[str, Any]],
    splits: Sequence[TimeSplit],
    partition_identities: Sequence[Mapping[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    contract = {
        "source_assessment_sha256": str(source_assessment_sha256),
        "feature_count": len(feature_names),
        "ordered_feature_names_sha256": canonical_sha256(
            {"feature_names": [str(value) for value in feature_names]}
        ),
        "causal_feature_contract": CAUSAL_FEATURE_CONTRACT,
        "actions": [dict(item) for item in actions],
        "action_count": len(actions),
        "additional_round_trip_cost_bps": float(
            args.additional_round_trip_cost_bps
        ),
        "stress_cost_multiplier": float(args.stress_cost_multiplier),
        "execution_latency_seconds": int(args.execution_latency_seconds),
        "overlapping_episodes_forbidden": True,
        "split_count": len(splits),
        "split_time_contracts": [dataclasses.asdict(split) for split in splits],
        "partition_identities": [dict(item) for item in partition_identities],
        "model_hyperparameters": {
            "iterations": int(args.iterations),
            "depth": int(args.depth),
            "learning_rate": float(args.learning_rate),
            "l2_leaf_reg": float(args.l2_leaf_reg),
            "random_strength": float(args.random_strength),
            "random_seed": int(args.random_seed),
            "early_stopping_rounds": int(args.early_stopping_rounds),
        },
        "validation_or_test_targets_used_for_fit": False,
    }
    return {**contract, "identity_sha256": canonical_sha256(contract)}


def run_frozen_target_architecture_comparison(
    *,
    timestamps: np.ndarray,
    features: np.ndarray,
    feature_names: Sequence[str],
    outcomes: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    splits: Sequence[TimeSplit],
    source_assessment_sha256: str,
    baseline_cache: Mapping[int, Mapping[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    required_split_count = len(splits)
    permutation_trials = int(getattr(args, "permutation_control_trials", 7))
    permutation_seed = int(getattr(args, "permutation_control_seed", 20260808))
    minimum_excess = float(
        getattr(args, "permutation_control_minimum_excess_lcb_bps", 0.0)
    )
    frozen_failures = frozen_target_architecture_shape_failures(
        feature_count=len(feature_names),
        action_count=len(actions),
        split_count=required_split_count,
    )
    embargo_seconds = max(int(value) for value in args.horizons_seconds) + int(
        args.execution_latency_seconds
    )
    partitions: List[Dict[str, Any]] = []
    split_indices: Dict[int, Dict[str, np.ndarray]] = {}
    for split in splits:
        model_fit_indices, model_selection_indices, _ = (
            build_fit_internal_model_selection_indices(
                timestamps,
                split,
                model_selection_window_seconds=int(
                    args.model_selection_window_seconds
                ),
                embargo_seconds=embargo_seconds,
            )
        )
        validation_indices = indices_between(
            timestamps, split.validation_start_ms, split.validation_end_ms
        )
        test_indices = indices_between(
            timestamps, split.test_start_ms, split.test_end_ms
        )
        indices = {
            "model_fit": model_fit_indices,
            "model_selection": model_selection_indices,
            "nested_validation": validation_indices,
            "oos_test": test_indices,
        }
        split_indices[int(split.split_id)] = indices
        identity = {
            "split_id": int(split.split_id),
            "time_contract": dataclasses.asdict(split),
            "row_counts": {key: int(len(value)) for key, value in indices.items()},
            "row_index_sha256": {
                key: integer_index_sha256(value) for key, value in indices.items()
            },
        }
        identity["identity_sha256"] = canonical_sha256(identity)
        partitions.append(identity)
    shared_contract = build_target_architecture_shared_contract(
        source_assessment_sha256=source_assessment_sha256,
        feature_names=feature_names,
        actions=actions,
        splits=splits,
        partition_identities=partitions,
        args=args,
    )
    if frozen_failures:
        aggregate = aggregate_target_architecture_comparison(
            split_reports=[],
            architecture_ids=TARGET_ARCHITECTURE_IDS,
            required_split_count=required_split_count,
            permutation_trials=permutation_trials,
            permutation_seed=permutation_seed,
            permutation_minimum_excess_lcb_bps=minimum_excess,
            frozen_contract_failures=frozen_failures,
        )
        return {
            **aggregate,
            "shared_contract": shared_contract,
            "split_reports": [],
        }

    comparison_split_reports: List[Dict[str, Any]] = []
    partition_by_split = {
        int(item["split_id"]): item for item in partitions
    }
    base_cost = float(args.additional_round_trip_cost_bps)
    stress_multiplier = float(args.stress_cost_multiplier)
    for split in splits:
        split_id = int(split.split_id)
        indices = split_indices[split_id]
        architecture_reports: Dict[str, Dict[str, Any]] = {}
        baseline = baseline_cache.get(split_id)
        if isinstance(baseline, Mapping):
            try:
                architecture_reports[TARGET_ARCHITECTURE_IDS[0]] = (
                    evaluate_target_architecture_split(
                        architecture_id=TARGET_ARCHITECTURE_IDS[0],
                        split_id=split_id,
                        score_units="base_net_bps",
                        validation_timestamps=timestamps[
                            indices["nested_validation"]
                        ],
                        validation_prediction=np.asarray(
                            baseline["validation_prediction"], dtype=np.float64
                        ),
                        validation_realized_base=outcomes[
                            indices["nested_validation"]
                        ],
                        test_timestamps=timestamps[indices["oos_test"]],
                        test_prediction=np.asarray(
                            baseline["test_prediction"], dtype=np.float64
                        ),
                        test_realized_base=outcomes[indices["oos_test"]],
                        actions=actions,
                        quantiles=args.calibration_quantiles,
                        min_trades=int(args.min_calibration_trades),
                        base_cost_bps=base_cost,
                        stress_cost_multiplier=stress_multiplier,
                        execution_latency_seconds=int(
                            args.execution_latency_seconds
                        ),
                        permutation_trials=permutation_trials,
                        permutation_seed=permutation_seed,
                        allowed_action_indices=baseline[
                            "allowed_action_indices"
                        ],
                        model_diagnostics=baseline.get("model_diagnostics", {}),
                    )
                )
            except Exception as exc:  # isolated diagnostic evidence
                architecture_reports[TARGET_ARCHITECTURE_IDS[0]] = {
                    "status": "evaluation_error",
                    "reason": f"{type(exc).__name__}:{exc}",
                }
        else:
            architecture_reports[TARGET_ARCHITECTURE_IDS[0]] = {
                "status": "missing_baseline_cache",
                "reason": "promotion_path_split_was_not_trained",
            }

        fit_utilities = build_stress_net_utility_targets(
            outcomes[indices["model_fit"]],
            base_cost_bps=base_cost,
            stress_cost_multiplier=stress_multiplier,
        )
        selection_utilities = build_stress_net_utility_targets(
            outcomes[indices["model_selection"]],
            base_cost_bps=base_cost,
            stress_cost_multiplier=stress_multiplier,
        )
        for architecture_id in TARGET_ARCHITECTURE_IDS[1:]:
            started = time.monotonic()
            try:
                predictions = fit_predict_experimental_architecture(
                    architecture_id=architecture_id,
                    fit_features=features[indices["model_fit"]],
                    fit_stress_utilities=fit_utilities,
                    model_selection_features=features[
                        indices["model_selection"]
                    ],
                    model_selection_stress_utilities=selection_utilities,
                    validation_features=features[
                        indices["nested_validation"]
                    ],
                    test_features=features[indices["oos_test"]],
                    actions=actions,
                    args=args,
                )
                diagnostics = dict(predictions.get("model_diagnostics", {}))
                diagnostics["training_and_inference_seconds"] = (
                    time.monotonic() - started
                )
                architecture_reports[architecture_id] = (
                    evaluate_target_architecture_split(
                        architecture_id=architecture_id,
                        split_id=split_id,
                        score_units=str(predictions["score_units"]),
                        validation_timestamps=timestamps[
                            indices["nested_validation"]
                        ],
                        validation_prediction=np.asarray(
                            predictions["validation_prediction"], dtype=np.float64
                        ),
                        validation_realized_base=outcomes[
                            indices["nested_validation"]
                        ],
                        test_timestamps=timestamps[indices["oos_test"]],
                        test_prediction=np.asarray(
                            predictions["test_prediction"], dtype=np.float64
                        ),
                        test_realized_base=outcomes[indices["oos_test"]],
                        actions=actions,
                        quantiles=args.calibration_quantiles,
                        min_trades=int(args.min_calibration_trades),
                        base_cost_bps=base_cost,
                        stress_cost_multiplier=stress_multiplier,
                        execution_latency_seconds=int(
                            args.execution_latency_seconds
                        ),
                        permutation_trials=permutation_trials,
                        permutation_seed=permutation_seed,
                        model_diagnostics=diagnostics,
                    )
                )
            except Exception as exc:  # isolated non-promotional experiment
                architecture_reports[architecture_id] = {
                    "status": "training_or_evaluation_error",
                    "reason": f"{type(exc).__name__}:{exc}",
                    "elapsed_seconds": time.monotonic() - started,
                    "promotion_evidence": False,
                    "promotion_eligible": False,
                }
        comparison_split_reports.append(
            {
                "split_id": split_id,
                "shared_partition_identity": partition_by_split[split_id],
                "architectures": architecture_reports,
            }
        )
    aggregate = aggregate_target_architecture_comparison(
        split_reports=comparison_split_reports,
        architecture_ids=TARGET_ARCHITECTURE_IDS,
        required_split_count=required_split_count,
        permutation_trials=permutation_trials,
        permutation_seed=permutation_seed,
        permutation_minimum_excess_lcb_bps=minimum_excess,
        frozen_contract_failures=frozen_failures,
    )
    return {
        **aggregate,
        "shared_contract": shared_contract,
        "split_reports": comparison_split_reports,
    }


def run_probe(args: argparse.Namespace) -> Dict[str, Any]:
    assessment_path = pathlib.Path(args.capture_assessment).resolve()
    assessment_sha256 = sha256_file(assessment_path)
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
    baseline_comparison_cache: Dict[int, Dict[str, Any]] = {}
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
        hindsight_oracle = evaluate_hindsight_oracle(
            timestamps=timestamps[test_indices],
            realized_base=outcomes[test_indices],
            actions=actions,
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
            execution_latency_seconds=int(args.execution_latency_seconds),
        )
        model_fit_indices, model_selection_indices, model_selection_time_contract = (
            build_fit_internal_model_selection_indices(
                timestamps,
                split,
                model_selection_window_seconds=int(
                    args.model_selection_window_seconds
                ),
                embargo_seconds=embargo_seconds,
            )
        )
        minimum_model_selection_rows = minimum_internal_model_selection_rows(
            minimum_window_rows=minimum,
            model_selection_window_seconds=int(args.model_selection_window_seconds),
            train_window_seconds=int(args.train_window_seconds),
        )
        if (
            len(model_fit_indices) < minimum
            or len(model_selection_indices) < minimum_model_selection_rows
        ):
            failures.append(
                f"split_{split.split_id}_insufficient_fit_internal_model_selection_rows"
            )
            split_reports.append(
                {
                    "split_id": split.split_id,
                    "status": "insufficient_fit_internal_model_selection_rows",
                    "time_contract": dataclasses.asdict(split),
                    "fit_internal_model_selection_time_contract": (
                        model_selection_time_contract
                    ),
                    "fit_rows": len(fit_indices),
                    "model_fit_rows": len(model_fit_indices),
                    "model_selection_rows": len(model_selection_indices),
                    "minimum_model_selection_rows": minimum_model_selection_rows,
                    "validation_rows": len(validation_indices),
                    "test_rows": len(test_indices),
                    "hindsight_oracle": hindsight_oracle,
                }
            )
            continue
        fit_targets, target_transform = fit_joint_policy_target(
            outcomes[model_fit_indices],
            actions=actions,
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
            minimum_profitable_events=int(args.min_fit_profitable_events),
        )
        model_selection_targets = transform_joint_policy_targets(
            outcomes[model_selection_indices], target_transform
        )
        validation_targets = transform_joint_policy_targets(
            outcomes[validation_indices], target_transform
        )
        test_targets = transform_joint_policy_targets(
            outcomes[test_indices], target_transform
        )
        if int(target_transform["model_output_count"]) <= 0:
            failures.append(
                f"split_{split.split_id}_no_supported_stress_event_action"
            )
            split_reports.append(
                {
                    "split_id": split.split_id,
                    "status": "no_supported_stress_event_action",
                    "time_contract": dataclasses.asdict(split),
                    "fit_internal_model_selection_time_contract": (
                        model_selection_time_contract
                    ),
                    "fit_rows": len(fit_indices),
                    "model_fit_rows": len(model_fit_indices),
                    "model_selection_rows": len(model_selection_indices),
                    "minimum_model_selection_rows": minimum_model_selection_rows,
                    "validation_rows": len(validation_indices),
                    "test_rows": len(test_indices),
                    "training_target_transform": target_transform,
                    "hindsight_oracle": hindsight_oracle,
                }
            )
            continue
        try:
            models = fit_independent_action_models(
                fit_features=features[model_fit_indices],
                fit_targets=fit_targets,
                model_selection_features=features[model_selection_indices],
                model_selection_targets=model_selection_targets,
                transform=target_transform,
                args=args,
            )
        except catboost.CatBoostError as exc:
            failures.append(f"split_{split.split_id}_catboost_training_error")
            split_reports.append(
                {
                    "split_id": split.split_id,
                    "status": "catboost_training_error",
                    "time_contract": dataclasses.asdict(split),
                    "fit_internal_model_selection_time_contract": (
                        model_selection_time_contract
                    ),
                    "fit_rows": len(fit_indices),
                    "model_fit_rows": len(model_fit_indices),
                    "model_selection_rows": len(model_selection_indices),
                    "minimum_model_selection_rows": minimum_model_selection_rows,
                    "validation_rows": len(validation_indices),
                    "test_rows": len(test_indices),
                    "training_target_transform": target_transform,
                    "hindsight_oracle": hindsight_oracle,
                    "error": str(exc),
                }
            )
            continue
        validation_prediction, validation_raw_prediction = predict_base_net_scores(
            models, features[validation_indices], target_transform
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
            allowed_action_indices=target_transform["model_action_indices"],
        )
        for candidate in calibration.get("candidates", []):
            candidate["event_probability_threshold"] = (
                base_net_score_to_event_probability(
                    float(candidate["threshold_bps"]),
                    target_transform,
                    int(candidate["action_index"]),
                )
            )
        calibration["threshold_transport_contract"] = (
            "aggregate_event_probability_then_reconstruct_with_final_fit_statistics"
        )
        selected = calibration.get("selected")
        threshold = (
            float(selected["threshold_bps"])
            if isinstance(selected, dict)
            else float("inf")
        )
        allowed_action_indices = (
            [int(selected["action_index"])]
            if isinstance(selected, dict)
            else []
        )
        test_prediction, test_raw_prediction = predict_base_net_scores(
            models, features[test_indices], target_transform
        )
        baseline_comparison_cache[int(split.split_id)] = {
            "validation_prediction": validation_prediction,
            "test_prediction": test_prediction,
            "allowed_action_indices": [
                int(value) for value in target_transform["model_action_indices"]
            ],
            "model_diagnostics": {
                "model_topology": (
                    "independent_binary_stress_event_classifier_per_action"
                ),
                "best_iterations_by_action": best_iterations_by_action(
                    models, target_transform
                ),
            },
        }
        objective = evaluate_joint_policy(
            timestamps=timestamps[test_indices],
            prediction=test_prediction,
            realized_base=outcomes[test_indices],
            actions=actions,
            threshold_bps=threshold,
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
            execution_latency_seconds=int(args.execution_latency_seconds),
            allowed_action_indices=allowed_action_indices,
        )
        diagnostic_selected = calibration.get("diagnostic_selected")
        diagnostic_objective = evaluate_joint_policy(
            timestamps=timestamps[test_indices],
            prediction=test_prediction,
            realized_base=outcomes[test_indices],
            actions=actions,
            threshold_bps=(
                float(diagnostic_selected["threshold_bps"])
                if isinstance(diagnostic_selected, dict)
                else float("inf")
            ),
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
            execution_latency_seconds=int(args.execution_latency_seconds),
            allowed_action_indices=(
                [int(diagnostic_selected["action_index"])]
                if isinstance(diagnostic_selected, dict)
                else []
            ),
        )
        diagnostic_objective.pop("base_edges_bps", None)
        diagnostic_objective.pop("stress_edges_bps", None)
        diagnostic_permutation_controls = evaluate_prediction_permutation_controls(
            timestamps=timestamps[test_indices],
            prediction=test_prediction,
            realized_base=outcomes[test_indices],
            actions=actions,
            threshold_bps=(
                float(diagnostic_selected["threshold_bps"])
                if isinstance(diagnostic_selected, dict)
                else float("inf")
            ),
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
            execution_latency_seconds=int(args.execution_latency_seconds),
            trials=permutation_trials,
            seed=permutation_seed + split.split_id * 1_000_003 + 500_009,
            allowed_action_indices=(
                [int(diagnostic_selected["action_index"])]
                if isinstance(diagnostic_selected, dict)
                else []
            ),
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
            allowed_action_indices=allowed_action_indices,
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
                "fit_internal_model_selection_time_contract": (
                    model_selection_time_contract
                ),
                "fit_rows": len(fit_indices),
                "model_fit_rows": len(model_fit_indices),
                "model_selection_rows": len(model_selection_indices),
                "minimum_model_selection_rows": minimum_model_selection_rows,
                "validation_rows": len(validation_indices),
                "test_rows": len(test_indices),
                "best_iterations_by_action": best_iterations_by_action(
                    models, target_transform
                ),
                "training_target_transform": target_transform,
                "validation_event_ranking_by_action": summarize_event_ranking_by_action(
                    validation_targets,
                    validation_raw_prediction,
                    target_transform,
                ),
                "test_event_ranking_by_action": summarize_event_ranking_by_action(
                    test_targets,
                    test_raw_prediction,
                    target_transform,
                ),
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
                "test_active_model_output_distribution": summarize_numeric_distribution(
                    np.max(test_raw_prediction, axis=1)
                ),
                "oos_objective": objective,
                "diagnostic_oos_objective": diagnostic_objective,
                "diagnostic_oos_is_promotion_evidence": False,
                "diagnostic_oos_prediction_permutation_controls": (
                    diagnostic_permutation_controls
                ),
                "hindsight_oracle": hindsight_oracle,
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
    selected_action_indices = [
        int(selected["action_index"])
        for item in split_reports
        for selected in [item.get("nested_calibration", {}).get("selected")]
        if isinstance(selected, dict)
    ]
    selected_action_counts = {
        str(action_index): selected_action_indices.count(action_index)
        for action_index in sorted(set(selected_action_indices))
    }
    dominant_action_index = (
        min(
            selected_action_counts,
            key=lambda value: (-selected_action_counts[value], int(value)),
        )
        if selected_action_counts
        else None
    )
    action_consensus_ratio = (
        selected_action_counts[dominant_action_index] / len(splits)
        if dominant_action_index is not None and splits
        else 0.0
    )
    minimum_action_consensus_ratio = float(
        getattr(args, "min_action_consensus_ratio", 0.60)
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
    learnability_diagnostic = build_learnability_diagnostic(
        split_reports=split_reports,
        required_split_count=len(splits),
        permutation_trials=permutation_trials,
        permutation_seed=permutation_seed + 500_009,
        permutation_minimum_excess_lcb_bps=(
            permutation_minimum_excess_lcb_bps
        ),
        minimum_oracle_trades=int(args.min_oos_trades),
        minimum_positive_splits_ratio=float(args.min_positive_splits_ratio),
    )
    target_architecture_comparison = run_frozen_target_architecture_comparison(
        timestamps=timestamps,
        features=features,
        feature_names=feature_names,
        outcomes=outcomes,
        actions=actions,
        splits=splits,
        source_assessment_sha256=assessment_sha256,
        baseline_cache=baseline_comparison_cache,
        args=args,
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
        and action_consensus_ratio >= minimum_action_consensus_ratio
        and (base_split_summary["lcb_bps"] or float("-inf")) > 0.0
        and (stress_split_summary["lcb_bps"] or float("-inf")) > 0.0
    )
    frozen_candidate: Dict[str, Any] | None = None
    if development_passed:
        if dominant_action_index is None:
            raise RuntimeError("development passed without a dominant policy action")
        frozen_action_index = int(dominant_action_index)
        selected_probability_thresholds = [
            float(selected["event_probability_threshold"])
            for item in split_reports
            for selected in [item.get("nested_calibration", {}).get("selected")]
            if isinstance(selected, dict)
            and int(selected["action_index"]) == frozen_action_index
        ]
        if not selected_probability_thresholds:
            raise RuntimeError("development passed without a frozen policy threshold")
        frozen_probability_threshold = float(
            statistics.median(selected_probability_thresholds)
        )
        best_iterations = [
            int(item["best_iterations_by_action"][str(frozen_action_index)])
            for item in split_reports
            if isinstance(item.get("best_iterations_by_action"), dict)
            and isinstance(
                item["best_iterations_by_action"].get(str(frozen_action_index)),
                int,
            )
            and int(
                item["best_iterations_by_action"][str(frozen_action_index)]
            )
            > 0
        ]
        final_iterations = int(
            round(statistics.median(best_iterations))
            if best_iterations
            else int(args.iterations)
        )
        _, full_target_transform = fit_joint_policy_target(
            outcomes,
            actions=actions,
            base_cost_bps=float(args.additional_round_trip_cost_bps),
            stress_cost_multiplier=float(args.stress_cost_multiplier),
            minimum_profitable_events=int(args.min_fit_profitable_events),
        )
        if frozen_action_index not in full_target_transform[
            "available_action_indices"
        ]:
            raise RuntimeError(
                "development passed without a learnable consensus action"
            )
        final_target_transform = select_model_action_indices(
            full_target_transform, [frozen_action_index]
        )
        final_targets = transform_joint_policy_targets(
            outcomes, final_target_transform
        )
        final_threshold_bps = float(
            reconstruct_base_net_scores(
                np.asarray([[frozen_probability_threshold]], dtype=np.float64),
                final_target_transform,
            )[0, frozen_action_index]
        )
        final_model = build_model(args, action_index=frozen_action_index)
        final_model.set_params(iterations=final_iterations)
        final_model.fit(features, final_targets[:, 0], verbose=False)
        model_path = pathlib.Path(args.model_output).resolve()
        model_path.parent.mkdir(parents=True, exist_ok=True)
        final_model.save_model(str(model_path))
        frozen_candidate = {
            "model_path": str(model_path),
            "model_sha256": sha256_file(model_path),
            "final_training_row_count": int(len(features)),
            "final_iterations": final_iterations,
            "policy_action_index": frozen_action_index,
            "policy_action": actions[frozen_action_index],
            "policy_event_probability_threshold": frozen_probability_threshold,
            "policy_threshold_bps": final_threshold_bps,
            "action_aggregation": "mode_of_nested_split_selected_actions",
            "threshold_aggregation": (
                "median_nested_event_probability_then_final_fit_bps_reconstruction"
            ),
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
            "sha256": assessment_sha256,
            "coverage_ms": assessment.get("coverage_ms"),
            "segment_count": assessment.get("valid_segment_count"),
            "development_cutoff_ms": assessment.get(
                "latest_exchange_timestamp_ms"
            ),
        },
        "cross_asset_feature_contract": collector.CROSS_ASSET_ALIGNMENT_CONTRACT,
        "causal_feature_contract": CAUSAL_FEATURE_CONTRACT,
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
            "calibration_scope": "independent_per_action_then_economic_selection",
            "frozen_action_aggregation": "mode_of_nested_split_selected_actions",
            "minimum_action_consensus_ratio": minimum_action_consensus_ratio,
            "minimum_fit_profitable_events_per_action": int(
                args.min_fit_profitable_events
            ),
            "fit_internal_model_selection_window_seconds": int(
                args.model_selection_window_seconds
            ),
            "fit_internal_model_selection_minimum_rows": (
                minimum_internal_model_selection_rows(
                    minimum_window_rows=int(args.min_window_rows),
                    model_selection_window_seconds=int(
                        args.model_selection_window_seconds
                    ),
                    train_window_seconds=int(args.train_window_seconds),
                )
            ),
            "fit_internal_model_selection_minimum_rows_contract": (
                "max_256_ceil_min_window_rows_times_selection_over_train_seconds"
            ),
            "external_nested_validation_used_for_model_fit_or_early_stopping": False,
            "score_threshold_floor_bps": None,
            "negative_model_score_threshold_permitted": True,
            "threshold_viability_contract": (
                "realized_base_and_stress_net_lcb_positive_in_nested_validation"
            ),
            "oos_windows_non_overlapping": True,
        },
        "model_contract": model_contract(args),
        "negative_control": permutation_control,
        "learnability_diagnostic": learnability_diagnostic,
        "target_architecture_comparison": target_architecture_comparison,
        "economic_screen": {
            "development_passed": development_passed,
            "trained_split_count": trained_split_count,
            "required_split_count": len(splits),
            "oos_base_cost_by_trade": base_trade_summary,
            "oos_stress_cost_by_trade": stress_trade_summary,
            "oos_base_cost_by_split": base_split_summary,
            "oos_stress_cost_by_split": stress_split_summary,
            "positive_base_edge_split_ratio": positive_split_ratio,
            "selected_action_counts": selected_action_counts,
            "dominant_action_index": (
                int(dominant_action_index)
                if dominant_action_index is not None
                else None
            ),
            "action_consensus_ratio": action_consensus_ratio,
            "minimum_action_consensus_ratio": minimum_action_consensus_ratio,
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
        "cross_asset_feature_contract": report.get("cross_asset_feature_contract"),
        "causal_feature_contract": report.get("causal_feature_contract"),
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
    parser.add_argument("--train-window-seconds", type=int, default=21600)
    parser.add_argument("--validation-window-seconds", type=int, default=14400)
    parser.add_argument("--test-window-seconds", type=int, default=14400)
    parser.add_argument("--rolling-step-seconds", type=int, default=14400)
    parser.add_argument("--model-selection-window-seconds", type=int, default=3600)
    parser.add_argument("--min-eligible-rows", type=int, default=60000)
    parser.add_argument("--min-window-rows", type=int, default=3600)
    parser.add_argument("--calibration-quantiles", default="0.50,0.60,0.70,0.80,0.90,0.95,0.98")
    parser.add_argument("--min-calibration-trades", type=int, default=8)
    parser.add_argument("--min-oos-trades", type=int, default=30)
    parser.add_argument("--min-positive-splits-ratio", type=float, default=0.60)
    parser.add_argument("--min-action-consensus-ratio", type=float, default=0.60)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--learning-rate", type=float, default=0.035)
    parser.add_argument("--l2-leaf-reg", type=float, default=30.0)
    parser.add_argument("--random-strength", type=float, default=2.0)
    parser.add_argument("--random-seed", type=int, default=20260806)
    parser.add_argument("--early-stopping-rounds", type=int, default=20)
    parser.add_argument("--min-fit-profitable-events", type=int, default=64)
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
    if args.min_fit_profitable_events < 16:
        raise ValueError("minimum fit profitable events must be >= 16")
    if not 0 < args.model_selection_window_seconds < args.train_window_seconds:
        raise ValueError(
            "model-selection window must be positive and smaller than train window"
        )
    if not 0.60 <= args.min_action_consensus_ratio <= 1.0:
        raise ValueError("minimum action consensus ratio must be in [0.60, 1]")
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
        "cross_asset_feature_contract": collector.CROSS_ASSET_ALIGNMENT_CONTRACT,
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
