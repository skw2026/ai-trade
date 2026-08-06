#!/usr/bin/env python3
"""Development-only probe for cost-aware scalar learning targets.

This command deliberately does not write a CatBoost model.  Its output is
diagnostic evidence from the development domain only; a promising variant must
still pass the independent selection domain and untouched final holdout before
it can become a deployable candidate.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import pathlib
import statistics
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import numpy as np

try:
    from catboost import CatBoostRegressor
except ImportError:  # pragma: no cover - exercised by the research image
    CatBoostRegressor = None

from integrator_train import (
    apply_feature_transform,
    auc_score,
    build_execution_bar_returns,
    build_feature_matrix,
    build_feature_transform,
    build_label,
    build_splits,
    load_factor_specs,
    load_ohlcv_csv,
    split_raw_temporal_train_validation,
    student_t_975,
    summarize_model_episode_objective,
    validate_time_axis,
)


RUNTIME_ENTRY_RAW_SCORE = math.log(3.0)
PROBE_VARIANTS = (
    "continuous_return_rmse",
    "continuous_return_huber",
    "continuous_return_huber_side_calibrated",
    "continuous_return_cross_asset_residual_huber_side_calibrated",
    "continuous_return_path_huber",
    "ternary_action_rmse",
    "path_utility_huber",
)
RESIDUAL_VARIANT = (
    "continuous_return_cross_asset_residual_huber_side_calibrated"
)
DEFAULT_PROBE_VARIANTS = tuple(
    variant for variant in PROBE_VARIANTS if variant != RESIDUAL_VARIANT
)
DIRECTION_MODES = ("both", "long_only", "short_only")
FEATURE_SETS = (
    "baseline",
    "expanded_ohlcv_v1",
    "expanded_derivatives_v1",
    "expanded_market_alpha_v1",
    "expanded_market_alpha_derivatives_v1",
)


def assert_development_only_path(path: pathlib.Path, label: str) -> None:
    lowered = str(path).lower()
    if "development" not in lowered or any(
        token in lowered for token in ("selection", "holdout", "final_test")
    ):
        raise ValueError(
            f"{label} must be explicitly development-only and must not reference "
            "selection/holdout/final_test"
        )


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_miner_development_contract(path: pathlib.Path, horizon_bars: int) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "optimization_domain": "development_train",
        "validation_domain": "development_validation_diagnostic_only",
        "validation_feedback_used": False,
        "predict_horizon_bars": int(horizon_bars),
        "execution_latency_bars": 1,
    }
    mismatches = {
        key: {"expected": expected, "actual": payload.get(key)}
        for key, expected in required.items()
        if payload.get(key) != expected
    }
    if mismatches:
        raise ValueError(f"miner development-domain contract mismatch: {mismatches}")


def lagged_return(values: np.ndarray, bars: int) -> np.ndarray:
    result = np.full(len(values), np.nan, dtype=np.float64)
    window = max(1, int(bars))
    if len(values) > window:
        base = np.asarray(values[:-window], dtype=np.float64)
        future = np.asarray(values[window:], dtype=np.float64)
        valid = np.isfinite(base) & np.isfinite(future) & (np.abs(base) > 1e-12)
        computed = np.full(len(base), np.nan, dtype=np.float64)
        computed[valid] = future[valid] / base[valid] - 1.0
        result[window:] = computed
    return result


def rolling_moments(values: np.ndarray, bars: int) -> Tuple[np.ndarray, np.ndarray]:
    """Causal full-window mean/std, including only bars at or before t."""
    raw = np.asarray(values, dtype=np.float64)
    window = max(2, int(bars))
    finite = np.isfinite(raw)
    clean = np.where(finite, raw, 0.0)
    prefix = np.concatenate(([0.0], np.cumsum(clean)))
    prefix_sq = np.concatenate(([0.0], np.cumsum(clean * clean)))
    prefix_count = np.concatenate(([0], np.cumsum(finite.astype(np.int64))))
    result_mean = np.full(len(raw), np.nan, dtype=np.float64)
    result_std = np.full(len(raw), np.nan, dtype=np.float64)
    if len(raw) < window:
        return result_mean, result_std
    totals = prefix[window:] - prefix[:-window]
    totals_sq = prefix_sq[window:] - prefix_sq[:-window]
    counts = prefix_count[window:] - prefix_count[:-window]
    valid_window = counts == window
    means = totals / float(window)
    variance = np.maximum(0.0, totals_sq / float(window) - means * means)
    destination = np.arange(window - 1, len(raw))
    result_mean[destination[valid_window]] = means[valid_window]
    result_std[destination[valid_window]] = np.sqrt(variance[valid_window])
    return result_mean, result_std


def build_expanded_ohlcv_features(
    series: Dict[str, np.ndarray],
) -> Tuple[np.ndarray, List[str]]:
    """Causal, closed-bar-only features that can be ported to C++ exactly."""
    timestamp = np.asarray(series["timestamp"], dtype=np.float64)
    open_price = np.asarray(series["open"], dtype=np.float64)
    high = np.asarray(series["high"], dtype=np.float64)
    low = np.asarray(series["low"], dtype=np.float64)
    close = np.asarray(series["close"], dtype=np.float64)
    volume = np.asarray(series["volume"], dtype=np.float64)
    names: List[str] = []
    arrays: List[np.ndarray] = []

    def add(name: str, values: np.ndarray) -> None:
        names.append(name)
        arrays.append(np.asarray(values, dtype=np.float64))

    return_1 = lagged_return(close, 1)
    for window in (6, 12, 24, 48, 96, 288):
        add(f"expanded_return_{window}", lagged_return(close, window))
    for window in (12, 48, 288):
        mean_return, std_return = rolling_moments(return_1, window)
        price_mean, _ = rolling_moments(close, window)
        volume_mean, volume_std = rolling_moments(volume, window)
        add(f"expanded_return_mean_{window}", mean_return)
        add(f"expanded_return_std_{window}", std_return)
        add(
            f"expanded_price_to_mean_{window}",
            close / np.where(np.abs(price_mean) > 1e-12, price_mean, np.nan) - 1.0,
        )
        add(
            f"expanded_volume_zscore_{window}",
            (volume - volume_mean)
            / np.where(volume_std > 1e-12, volume_std, np.nan),
        )

    safe_open = np.where(np.abs(open_price) > 1e-12, open_price, np.nan)
    safe_close = np.where(np.abs(close) > 1e-12, close, np.nan)
    candle_range = high - low
    add("expanded_candle_body", (close - open_price) / safe_open)
    add("expanded_candle_range", candle_range / safe_close)
    add(
        "expanded_close_location",
        (close - low) / np.where(candle_range > 1e-12, candle_range, np.nan) - 0.5,
    )
    milliseconds_per_day = 86_400_000.0
    day_phase = np.mod(timestamp, milliseconds_per_day) / milliseconds_per_day
    week_phase = np.mod(timestamp, milliseconds_per_day * 7.0) / (
        milliseconds_per_day * 7.0
    )
    add("expanded_time_day_sin", np.sin(2.0 * math.pi * day_phase))
    add("expanded_time_day_cos", np.cos(2.0 * math.pi * day_phase))
    add("expanded_time_week_sin", np.sin(2.0 * math.pi * week_phase))
    add("expanded_time_week_cos", np.cos(2.0 * math.pi * week_phase))
    return np.column_stack(arrays), names


def load_derivatives_features(
    path: pathlib.Path,
    expected_timestamps: np.ndarray,
) -> Tuple[np.ndarray, List[str]]:
    columns = (
        "premium_index_close",
        "open_interest",
        "long_account_ratio",
        "short_account_ratio",
        "funding_rate",
    )
    timestamps: List[int] = []
    values: Dict[str, List[float]] = {name: [] for name in columns}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", *columns}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("derivatives CSV is missing required columns")
        for row in reader:
            timestamps.append(int(row["timestamp"]))
            for name in columns:
                raw = str(row.get(name, "")).strip()
                values[name].append(float(raw) if raw else float("nan"))
    actual_timestamps = np.asarray(timestamps, dtype=np.int64)
    expected = np.asarray(expected_timestamps, dtype=np.int64)
    if len(actual_timestamps) != len(expected) or not np.array_equal(actual_timestamps, expected):
        raise ValueError("derivatives/OHLCV timestamp axes differ")

    premium = np.asarray(values["premium_index_close"], dtype=np.float64)
    open_interest = np.asarray(values["open_interest"], dtype=np.float64)
    long_ratio = np.asarray(values["long_account_ratio"], dtype=np.float64)
    short_ratio = np.asarray(values["short_account_ratio"], dtype=np.float64)
    funding = np.asarray(values["funding_rate"], dtype=np.float64)
    imbalance = long_ratio - short_ratio
    names: List[str] = []
    arrays: List[np.ndarray] = []

    def add(name: str, data: np.ndarray) -> None:
        names.append(name)
        arrays.append(np.asarray(data, dtype=np.float64))

    add("derivative_premium", premium)
    add("derivative_open_interest", np.log(np.where(open_interest > 0.0, open_interest, np.nan)))
    add("derivative_account_imbalance", imbalance)
    add("derivative_funding_rate", funding)
    for window in (12, 48, 288):
        premium_mean, premium_std = rolling_moments(premium, window)
        imbalance_mean, imbalance_std = rolling_moments(imbalance, window)
        add(f"derivative_premium_delta_{window}", premium - np.roll(premium, window))
        arrays[-1][:window] = np.nan
        add(
            f"derivative_premium_zscore_{window}",
            (premium - premium_mean)
            / np.where(premium_std > 1e-12, premium_std, np.nan),
        )
        add(f"derivative_oi_return_{window}", lagged_return(open_interest, window))
        add(f"derivative_imbalance_delta_{window}", imbalance - np.roll(imbalance, window))
        arrays[-1][:window] = np.nan
        add(
            f"derivative_imbalance_zscore_{window}",
            (imbalance - imbalance_mean)
            / np.where(imbalance_std > 1e-12, imbalance_std, np.nan),
        )
    return np.column_stack(arrays), names


def load_market_alpha_features(
    path: pathlib.Path,
    expected_timestamps: np.ndarray,
    anchor_close: np.ndarray,
) -> Tuple[np.ndarray, List[str]]:
    """Load exact-axis Binance trade-flow/cross-asset bars as causal features."""
    source_fields = tuple(
        f"binance_{symbol}_{field}"
        for symbol in ("sol", "btc", "eth")
        for field in ("close", "quote_volume", "trade_count", "taker_buy_quote_volume")
    )
    timestamps: List[int] = []
    values: Dict[str, List[float]] = {name: [] for name in source_fields}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", *source_fields}
        if not required.issubset(set(reader.fieldnames or [])):
            missing = sorted(required.difference(set(reader.fieldnames or [])))
            raise ValueError(f"market alpha CSV is missing required columns: {missing}")
        for row in reader:
            timestamps.append(int(row["timestamp"]))
            for name in source_fields:
                raw = str(row.get(name, "")).strip()
                values[name].append(float(raw) if raw else float("nan"))
    actual = np.asarray(timestamps, dtype=np.int64)
    expected = np.asarray(expected_timestamps, dtype=np.int64)
    anchor = np.asarray(anchor_close, dtype=np.float64)
    if len(actual) != len(expected) or not np.array_equal(actual, expected):
        raise ValueError("market alpha/OHLCV timestamp axes differ")
    if len(anchor) != len(expected):
        raise ValueError("anchor close/OHLCV timestamp axes differ")

    arrays: List[np.ndarray] = []
    names: List[str] = []

    def add(name: str, data: np.ndarray) -> None:
        names.append(name)
        arrays.append(np.asarray(data, dtype=np.float64))

    close = {
        symbol: np.asarray(values[f"binance_{symbol}_close"], dtype=np.float64)
        for symbol in ("sol", "btc", "eth")
    }
    quote_volume = {
        symbol: np.asarray(values[f"binance_{symbol}_quote_volume"], dtype=np.float64)
        for symbol in ("sol", "btc", "eth")
    }
    trade_count = {
        symbol: np.asarray(values[f"binance_{symbol}_trade_count"], dtype=np.float64)
        for symbol in ("sol", "btc", "eth")
    }
    taker_buy = {
        symbol: np.asarray(
            values[f"binance_{symbol}_taker_buy_quote_volume"], dtype=np.float64
        )
        for symbol in ("sol", "btc", "eth")
    }
    imbalance = {
        symbol: 2.0 * taker_buy[symbol]
        / np.where(quote_volume[symbol] > 1e-12, quote_volume[symbol], np.nan)
        - 1.0
        for symbol in ("sol", "btc", "eth")
    }

    basis = close["sol"] / np.where(np.abs(anchor) > 1e-12, anchor, np.nan) - 1.0
    add("market_sol_cross_venue_basis", basis)
    add("market_sol_taker_imbalance", imbalance["sol"])
    add("market_sol_log_quote_volume", np.log1p(quote_volume["sol"]))
    add("market_sol_log_trade_count", np.log1p(trade_count["sol"]))
    for window in (12, 48, 288):
        basis_mean, basis_std = rolling_moments(basis, window)
        flow_mean, flow_std = rolling_moments(imbalance["sol"], window)
        add(
            f"market_sol_basis_zscore_{window}",
            (basis - basis_mean) / np.where(basis_std > 1e-12, basis_std, np.nan),
        )
        add(
            f"market_sol_flow_zscore_{window}",
            (imbalance["sol"] - flow_mean)
            / np.where(flow_std > 1e-12, flow_std, np.nan),
        )

    for symbol in ("btc", "eth"):
        add(f"market_{symbol}_taker_imbalance", imbalance[symbol])
        for window in (1, 6, 12, 48):
            add(f"market_{symbol}_return_{window}", lagged_return(close[symbol], window))

    anchor_return_1 = lagged_return(anchor, 1)
    btc_return_1 = lagged_return(close["btc"], 1)
    eth_return_1 = lagged_return(close["eth"], 1)
    add("market_cross_asset_residual_1", anchor_return_1 - 0.5 * (btc_return_1 + eth_return_1))
    add("market_btc_eth_dispersion_1", btc_return_1 - eth_return_1)
    for window in (12, 48):
        anchor_return = lagged_return(anchor, window)
        btc_return = lagged_return(close["btc"], window)
        eth_return = lagged_return(close["eth"], window)
        add(
            f"market_cross_asset_residual_{window}",
            anchor_return - 0.5 * (btc_return + eth_return),
        )
        add(f"market_btc_eth_dispersion_{window}", btc_return - eth_return)
    return np.column_stack(arrays), names


def load_market_alpha_closes(
    path: pathlib.Path,
    expected_timestamps: np.ndarray,
) -> Dict[str, np.ndarray]:
    """Load exact-axis cross-asset closes used only to construct research labels."""
    fields = tuple(f"binance_{symbol}_close" for symbol in ("btc", "eth"))
    timestamps: List[int] = []
    values: Dict[str, List[float]] = {name: [] for name in fields}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", *fields}
        if not required.issubset(set(reader.fieldnames or [])):
            missing = sorted(required.difference(set(reader.fieldnames or [])))
            raise ValueError(
                f"market alpha CSV is missing residual-label columns: {missing}"
            )
        for row in reader:
            timestamps.append(int(row["timestamp"]))
            for name in fields:
                raw = str(row.get(name, "")).strip()
                values[name].append(float(raw) if raw else float("nan"))
    actual = np.asarray(timestamps, dtype=np.int64)
    expected = np.asarray(expected_timestamps, dtype=np.int64)
    if len(actual) != len(expected) or not np.array_equal(actual, expected):
        raise ValueError("market alpha/OHLCV timestamp axes differ")
    return {
        symbol: np.asarray(values[f"binance_{symbol}_close"], dtype=np.float64)
        for symbol in ("btc", "eth")
    }


def build_cross_asset_residual_forward_return(
    anchor_forward_return: np.ndarray,
    cross_asset_closes: Dict[str, np.ndarray],
    *,
    horizon_bars: int,
    execution_latency_bars: int,
) -> np.ndarray:
    """Remove equal-weight BTC/ETH beta from the SOL development target.

    The future cross-asset values are labels, never features.  Candidate
    calibration and OOS scoring continue to use absolute realized SOL return,
    so relative outperformance alone cannot pass the economic screen.
    """
    anchor = np.asarray(anchor_forward_return, dtype=np.float64)
    component_returns: List[np.ndarray] = []
    for symbol in ("btc", "eth"):
        if symbol not in cross_asset_closes:
            raise ValueError(f"missing cross-asset close series: {symbol}")
        _, future_return = build_label(
            np.asarray(cross_asset_closes[symbol], dtype=np.float64),
            int(horizon_bars),
            execution_latency_bars=int(execution_latency_bars),
        )
        if len(future_return) != len(anchor):
            raise ValueError("cross-asset/anchor forward-return axes differ")
        component_returns.append(future_return)
    market_return = 0.5 * (component_returns[0] + component_returns[1])
    residual = np.full(len(anchor), np.nan, dtype=np.float64)
    valid = np.isfinite(anchor) & np.isfinite(market_return)
    residual[valid] = anchor[valid] - market_return[valid]
    return residual


def build_target(
    forward_return: np.ndarray,
    *,
    variant: str,
    threshold_bps: float,
    target_clip: float,
) -> np.ndarray:
    """Map realized horizon return to the scalar consumed by runtime sigmoid."""
    if variant not in PROBE_VARIANTS:
        raise ValueError(f"unsupported probe variant: {variant}")
    if not math.isfinite(threshold_bps) or threshold_bps <= 0.0:
        raise ValueError("threshold_bps must be finite and > 0")
    if not math.isfinite(target_clip) or target_clip < RUNTIME_ENTRY_RAW_SCORE:
        raise ValueError("target_clip must be finite and >= log(3)")

    values = np.asarray(forward_return, dtype=np.float64)
    target = np.full(len(values), np.nan, dtype=np.float64)
    finite = np.isfinite(values)
    return_bps = values[finite] * 10000.0
    if variant.startswith("continuous_return_"):
        scaled = return_bps / threshold_bps * RUNTIME_ENTRY_RAW_SCORE
        target[finite] = np.clip(scaled, -target_clip, target_clip)
        return target

    threshold = float(threshold_bps)
    action = np.zeros(len(return_bps), dtype=np.float64)
    action[return_bps > threshold] = RUNTIME_ENTRY_RAW_SCORE
    action[return_bps < -threshold] = -RUNTIME_ENTRY_RAW_SCORE
    target[finite] = action
    return target


def build_path_first_touch_outcomes(
    series: Dict[str, np.ndarray],
    *,
    execution_latency_bars: int,
    horizon_bars: int,
    take_profit_bps: float,
    stop_loss_bps: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Build causal-entry, conservative same-bar first-touch outcomes."""
    close = np.asarray(series["close"], dtype=np.float64)
    high = np.asarray(series["high"], dtype=np.float64)
    low = np.asarray(series["low"], dtype=np.float64)
    n = len(close)
    long_gross = np.full(n, np.nan, dtype=np.float64)
    short_gross = np.full(n, np.nan, dtype=np.float64)
    long_duration = np.zeros(n, dtype=np.int32)
    short_duration = np.zeros(n, dtype=np.int32)
    latency = max(1, int(execution_latency_bars))
    horizon = max(1, int(horizon_bars))
    tp = float(take_profit_bps)
    sl = float(stop_loss_bps)
    if tp <= 0.0 or sl <= 0.0:
        raise ValueError("path take-profit/stop-loss bps must be positive")

    for anchor in range(0, n - latency - horizon):
        entry_index = anchor + latency
        entry = float(close[entry_index])
        if not math.isfinite(entry) or entry <= 0.0:
            continue
        long_result = None
        short_result = None
        long_steps = horizon
        short_steps = horizon
        for step in range(1, horizon + 1):
            bar = entry_index + step
            if not (math.isfinite(float(high[bar])) and math.isfinite(float(low[bar]))):
                break
            high_bps = (float(high[bar]) / entry - 1.0) * 10000.0
            low_bps = (float(low[bar]) / entry - 1.0) * 10000.0
            # If both barriers are touched inside one OHLC bar, assume the stop
            # was hit first. This avoids optimistic intrabar ordering leakage.
            if long_result is None:
                if low_bps <= -sl:
                    long_result, long_steps = -sl, step
                elif high_bps >= tp:
                    long_result, long_steps = tp, step
            if short_result is None:
                if high_bps >= sl:
                    short_result, short_steps = -sl, step
                elif low_bps <= -tp:
                    short_result, short_steps = tp, step
            if long_result is not None and short_result is not None:
                break
        terminal = float(close[entry_index + horizon])
        if not math.isfinite(terminal) or terminal <= 0.0:
            continue
        if long_result is None:
            long_result = (terminal / entry - 1.0) * 10000.0
        if short_result is None:
            short_result = (entry / terminal - 1.0) * 10000.0
        long_gross[anchor] = float(long_result)
        short_gross[anchor] = float(short_result)
        long_duration[anchor] = int(long_steps)
        short_duration[anchor] = int(short_steps)
    return long_gross, short_gross, long_duration, short_duration


def build_path_utility_target(
    long_gross_bps: np.ndarray,
    short_gross_bps: np.ndarray,
    *,
    round_trip_cost_bps: float,
    minimum_net_edge_bps: float,
    target_clip: float,
) -> np.ndarray:
    long_net = np.asarray(long_gross_bps, dtype=np.float64) - float(round_trip_cost_bps)
    short_net = np.asarray(short_gross_bps, dtype=np.float64) - float(round_trip_cost_bps)
    target = np.full(len(long_net), np.nan, dtype=np.float64)
    finite = np.isfinite(long_net) & np.isfinite(short_net)
    target[finite] = 0.0
    choose_long = finite & (long_net >= short_net) & (
        long_net > float(minimum_net_edge_bps)
    )
    choose_short = finite & (short_net > long_net) & (
        short_net > float(minimum_net_edge_bps)
    )
    scale = max(1.0, float(round_trip_cost_bps) + float(minimum_net_edge_bps))
    target[choose_long] = np.clip(
        long_net[choose_long] / scale * RUNTIME_ENTRY_RAW_SCORE,
        0.0,
        float(target_clip),
    )
    target[choose_short] = -np.clip(
        short_net[choose_short] / scale * RUNTIME_ENTRY_RAW_SCORE,
        0.0,
        float(target_clip),
    )
    return target


def summarize_path_episode_objective(
    *,
    score: np.ndarray,
    long_gross_bps: np.ndarray,
    short_gross_bps: np.ndarray,
    long_duration: np.ndarray,
    short_duration: np.ndarray,
    round_trip_cost_bps: float,
    confidence_threshold: float,
) -> Dict[str, Any]:
    if not (
        len(score)
        == len(long_gross_bps)
        == len(short_gross_bps)
        == len(long_duration)
        == len(short_duration)
    ):
        raise ValueError("path objective arrays must align")
    gross_values: List[float] = []
    net_values: List[float] = []
    directions: List[int] = []
    active_bars = 0
    index = 0
    while index < len(score):
        probability = float(score[index])
        confidence = 2.0 * probability - 1.0
        if not math.isfinite(probability) or abs(confidence) < float(confidence_threshold):
            index += 1
            continue
        direction = 1 if confidence > 0.0 else -1
        gross = float(long_gross_bps[index] if direction > 0 else short_gross_bps[index])
        duration = int(long_duration[index] if direction > 0 else short_duration[index])
        if not math.isfinite(gross) or duration <= 0:
            index += 1
            continue
        gross_values.append(gross)
        net_values.append(gross - float(round_trip_cost_bps))
        directions.append(direction)
        active_bars += duration
        index += max(1, duration)
    count = len(net_values)
    turnover = float(count * 2)
    if count == 0:
        return {
            "model_net_objective_sample_count": int(len(score)),
            "mean_model_gross_edge_bps": 0.0,
            "mean_model_net_edge_bps": 0.0,
            "total_model_gross_edge_bps": 0.0,
            "total_model_net_edge_bps": 0.0,
            "model_net_total_turnover": 0.0,
            "trade_count": 0,
            "turnover": 0.0,
            "active_bar_count": 0,
            "positive_trade_count": 0,
            "evaluated_bar_count": int(len(score)),
            "objective_definition": "non_overlapping_path_first_touch_episodes",
        }
    gross_array = np.asarray(gross_values, dtype=np.float64)
    net_array = np.asarray(net_values, dtype=np.float64)
    positive = int(np.sum(net_array > 0.0))
    return {
        "model_net_objective_sample_count": int(len(score)),
        "mean_model_gross_edge_bps": float(np.sum(gross_array)) / turnover,
        "mean_model_net_edge_bps": float(np.sum(net_array)) / turnover,
        "total_model_gross_edge_bps": float(np.sum(gross_array)),
        "total_model_net_edge_bps": float(np.sum(net_array)),
        "median_model_net_edge_bps": float(np.median(net_array)),
        "positive_model_net_edge_ratio": float(positive) / float(count),
        "long_signal_ratio": float(np.mean(np.asarray(directions) > 0)),
        "short_signal_ratio": float(np.mean(np.asarray(directions) < 0)),
        "round_trip_cost_bps": float(round_trip_cost_bps),
        "trade_count": count,
        "turnover": turnover,
        "active_bar_count": active_bars,
        "positive_trade_count": positive,
        "evaluated_bar_count": int(len(score)),
        "net_bps_sum_squares": float(np.sum(net_array * net_array)),
        "terminal_position_closed": True,
        "objective_definition": "non_overlapping_path_first_touch_episodes",
    }


def raw_score_to_probability(raw_score: np.ndarray) -> np.ndarray:
    raw = np.asarray(raw_score, dtype=np.float64)
    return 1.0 / (1.0 + np.exp(-np.clip(raw, -60.0, 60.0)))


def constrain_probability_direction(
    probability: np.ndarray, direction_mode: str
) -> np.ndarray:
    if direction_mode not in DIRECTION_MODES:
        raise ValueError(f"unsupported direction mode: {direction_mode}")
    constrained = np.asarray(probability, dtype=np.float64).copy()
    if direction_mode == "long_only":
        constrained[np.isfinite(constrained) & (constrained < 0.5)] = 0.5
    elif direction_mode == "short_only":
        constrained[np.isfinite(constrained) & (constrained > 0.5)] = 0.5
    return constrained


def select_nested_validation_scale(
    *,
    raw_prediction: np.ndarray,
    execution_bar_return: np.ndarray,
    quantiles: Sequence[float],
    round_trip_cost_bps: float,
    confidence_threshold: float,
    holding_bars: int,
    min_trades: int,
    direction_modes: Sequence[str] = ("both",),
) -> Tuple[float, Dict[str, Any]]:
    finite_abs = np.abs(np.asarray(raw_prediction, dtype=np.float64))
    finite_abs = finite_abs[np.isfinite(finite_abs)]
    candidates: List[Dict[str, Any]] = []
    if len(finite_abs) == 0:
        return 0.0, {"status": "no_finite_validation_prediction", "candidates": []}
    for quantile in quantiles:
        if not 0.0 < float(quantile) < 1.0:
            raise ValueError("calibration quantiles must be in (0,1)")
        raw_threshold = float(np.quantile(finite_abs, float(quantile)))
        scale = RUNTIME_ENTRY_RAW_SCORE / max(raw_threshold, 1e-12)
        probability = raw_score_to_probability(raw_prediction * scale)
        for direction_mode in direction_modes:
            objective = summarize_model_episode_objective(
                score=constrain_probability_direction(
                    probability, str(direction_mode)
                ),
                execution_bar_return=execution_bar_return,
                round_trip_cost_bps=round_trip_cost_bps,
                confidence_threshold=confidence_threshold,
                holding_bars=holding_bars,
            )
            trade_count = int(objective.get("trade_count", 0))
            mean_net = float(objective.get("mean_model_net_edge_bps", 0.0))
            positive_ratio = float(
                objective.get("positive_model_net_edge_ratio", float("nan"))
            )
            eligible = bool(
                trade_count >= int(min_trades)
                and mean_net > 0.0
                and math.isfinite(positive_ratio)
                and positive_ratio >= 0.5
            )
            candidates.append(
                {
                    "quantile": float(quantile),
                    "direction_mode": str(direction_mode),
                    "raw_threshold": raw_threshold,
                    "raw_score_scale": scale,
                    "eligible": eligible,
                    "net_objective": objective,
                }
            )
    eligible_candidates = [item for item in candidates if item["eligible"]]
    if not eligible_candidates:
        return 0.0, {
            "status": "no_validation_candidate_passed",
            "selected": None,
            "candidates": candidates,
        }
    selected = max(
        eligible_candidates,
        key=lambda item: (
            float(item["net_objective"]["mean_model_net_edge_bps"]),
            int(item["net_objective"]["trade_count"]),
            float(item["quantile"]),
            str(item["direction_mode"]),
        ),
    )
    return float(selected["raw_score_scale"]), {
        "status": "selected_on_nested_validation",
        "selected": {
            "quantile": selected["quantile"],
            "direction_mode": selected["direction_mode"],
            "raw_threshold": selected["raw_threshold"],
            "raw_score_scale": selected["raw_score_scale"],
            "net_objective": selected["net_objective"],
        },
        "candidates": candidates,
    }


def select_nested_validation_path_scale(
    *,
    raw_prediction: np.ndarray,
    long_gross_bps: np.ndarray,
    short_gross_bps: np.ndarray,
    long_duration: np.ndarray,
    short_duration: np.ndarray,
    quantiles: Sequence[float],
    round_trip_cost_bps: float,
    confidence_threshold: float,
    min_trades: int,
) -> Tuple[float, Dict[str, Any]]:
    finite_abs = np.abs(np.asarray(raw_prediction, dtype=np.float64))
    finite_abs = finite_abs[np.isfinite(finite_abs)]
    candidates: List[Dict[str, Any]] = []
    if len(finite_abs) == 0:
        return 0.0, {"status": "no_finite_validation_prediction", "candidates": []}
    for quantile in quantiles:
        raw_threshold = float(np.quantile(finite_abs, float(quantile)))
        scale = RUNTIME_ENTRY_RAW_SCORE / max(raw_threshold, 1e-12)
        objective = summarize_path_episode_objective(
            score=raw_score_to_probability(raw_prediction * scale),
            long_gross_bps=long_gross_bps,
            short_gross_bps=short_gross_bps,
            long_duration=long_duration,
            short_duration=short_duration,
            round_trip_cost_bps=round_trip_cost_bps,
            confidence_threshold=confidence_threshold,
        )
        trades = int(objective.get("trade_count", 0))
        mean_net = float(objective.get("mean_model_net_edge_bps", 0.0))
        positive = float(objective.get("positive_model_net_edge_ratio", float("nan")))
        eligible = bool(
            trades >= int(min_trades)
            and mean_net > 0.0
            and math.isfinite(positive)
            and positive >= 0.5
        )
        candidates.append(
            {
                "quantile": float(quantile),
                "raw_threshold": raw_threshold,
                "raw_score_scale": scale,
                "eligible": eligible,
                "net_objective": objective,
            }
        )
    eligible_candidates = [item for item in candidates if item["eligible"]]
    if not eligible_candidates:
        return 0.0, {
            "status": "no_validation_candidate_passed",
            "selected": None,
            "candidates": candidates,
        }
    selected = max(
        eligible_candidates,
        key=lambda item: (
            float(item["net_objective"]["mean_model_net_edge_bps"]),
            int(item["net_objective"]["trade_count"]),
            float(item["quantile"]),
        ),
    )
    return float(selected["raw_score_scale"]), {
        "status": "selected_on_nested_validation",
        "selected": {
            "quantile": selected["quantile"],
            "raw_threshold": selected["raw_threshold"],
            "raw_score_scale": selected["raw_score_scale"],
            "net_objective": selected["net_objective"],
        },
        "candidates": candidates,
    }


def mean_finite(values: Iterable[float]) -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return float(statistics.mean(finite)) if finite else float("nan")


def stdev_finite(values: Iterable[float]) -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return float(statistics.stdev(finite)) if len(finite) >= 2 else float("nan")


def json_safe(value: Any) -> Any:
    """Recursively replace non-finite numpy/Python scalars with JSON null."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    return value


def aggregate_economic_objectives(
    objectives: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    total_gross = sum(float(item.get("total_model_gross_edge_bps", 0.0)) for item in objectives)
    total_net = sum(float(item.get("total_model_net_edge_bps", 0.0)) for item in objectives)
    turnover = sum(float(item.get("turnover", 0.0)) for item in objectives)
    trades = sum(int(item.get("trade_count", 0)) for item in objectives)
    active_bars = sum(int(item.get("active_bar_count", 0)) for item in objectives)
    positive_trades = sum(int(item.get("positive_trade_count", 0)) for item in objectives)
    evaluated_bars = sum(int(item.get("evaluated_bar_count", 0)) for item in objectives)
    split_edges = [
        float(item.get("mean_model_net_edge_bps", float("nan")))
        for item in objectives
    ]
    finite_split_edges = [value for value in split_edges if math.isfinite(value)]
    split_mean = mean_finite(finite_split_edges)
    split_stdev = stdev_finite(finite_split_edges)
    edge_lcb = float("nan")
    if len(finite_split_edges) >= 2 and math.isfinite(split_stdev):
        edge_lcb = split_mean - student_t_975(len(finite_split_edges) - 1) * (
            split_stdev / math.sqrt(float(len(finite_split_edges)))
        )
    positive_splits = sum(value > 0.0 for value in finite_split_edges)
    return {
        "mean_model_gross_edge_bps": total_gross / turnover if turnover > 0.0 else 0.0,
        "mean_model_net_edge_bps": total_net / turnover if turnover > 0.0 else 0.0,
        "total_model_gross_edge_bps": total_gross,
        "total_model_net_edge_bps": total_net,
        "model_net_total_turnover": turnover,
        "model_net_total_trades": trades,
        "model_net_active_bar_count": active_bars,
        "model_net_positive_trade_count": positive_trades,
        "model_net_evaluated_bar_count": evaluated_bars,
        "positive_model_net_edge_ratio": (
            float(positive_trades) / float(trades) if trades > 0 else None
        ),
        "mean_model_net_edge_bps_by_split": split_mean if math.isfinite(split_mean) else None,
        "model_net_edge_bps_split_stdev": split_stdev if math.isfinite(split_stdev) else None,
        "model_net_edge_lcb_bps": edge_lcb if math.isfinite(edge_lcb) else None,
        "positive_model_net_edge_ratio_by_split": (
            float(positive_splits) / float(len(finite_split_edges))
            if finite_split_edges
            else None
        ),
        "oos_economic_split_count": len(finite_split_edges),
    }


def build_regressor(args: argparse.Namespace, variant: str) -> Any:
    loss_function = "Huber:delta=1.0" if "huber" in variant else "RMSE"
    return CatBoostRegressor(
        loss_function=loss_function,
        eval_metric="RMSE",
        random_seed=int(args.random_seed),
        iterations=int(args.iterations),
        depth=int(args.depth),
        learning_rate=float(args.learning_rate),
        l2_leaf_reg=float(args.l2_leaf_reg),
        random_strength=float(args.random_strength),
        bootstrap_type="Bernoulli",
        subsample=float(args.subsample),
        rsm=float(args.rsm),
        verbose=False,
        allow_writing_files=False,
    )


def run_variant(
    *,
    args: argparse.Namespace,
    variant: str,
    raw_features: np.ndarray,
    feature_names: Sequence[str],
    forward_return: np.ndarray,
    target_forward_return: np.ndarray | None,
    direction_label: np.ndarray,
    execution_bar_return: np.ndarray,
    splits: Sequence[Any],
    threshold_bps: float,
    path_long_gross_bps: np.ndarray,
    path_short_gross_bps: np.ndarray,
    path_long_duration: np.ndarray,
    path_short_duration: np.ndarray,
) -> Dict[str, Any]:
    path_target_variant = variant == "path_utility_huber"
    path_objective_variant = variant in {
        "continuous_return_path_huber",
        "path_utility_huber",
    }
    target = (
        build_path_utility_target(
            path_long_gross_bps,
            path_short_gross_bps,
            round_trip_cost_bps=float(args.label_round_trip_cost_bps),
            minimum_net_edge_bps=float(args.label_min_net_edge_bps),
            target_clip=float(args.target_clip),
        )
        if path_target_variant
        else build_target(
            forward_return if target_forward_return is None else target_forward_return,
            variant=variant,
            threshold_bps=threshold_bps,
            target_clip=float(args.target_clip),
        )
    )
    finite_features = np.all(np.isfinite(raw_features), axis=1)
    target_valid = finite_features & np.isfinite(target)
    economic_valid = finite_features & (
        (
            np.isfinite(path_long_gross_bps)
            & np.isfinite(path_short_gross_bps)
            & (path_long_duration > 0)
            & (path_short_duration > 0)
        )
        if path_objective_variant
        else np.isfinite(execution_bar_return)
    )
    raw_indices = np.arange(len(raw_features))
    objectives: List[Dict[str, Any]] = []
    split_reports: List[Dict[str, Any]] = []
    directional_auc: List[float] = []
    previous_test_end = -1

    for split_id, split in enumerate(splits, start=1):
        if int(split.test_start) < previous_test_end:
            raise ValueError("overlapping OOS windows are forbidden")
        previous_test_end = int(split.test_end)
        (
            x_fit_raw,
            y_fit,
            x_val_raw,
            y_val,
            fit_meta,
        ) = split_raw_temporal_train_validation(
            raw_features,
            target,
            raw_start=int(split.train_start),
            raw_end=int(split.train_end),
            validation_fraction=float(args.validation_fraction),
            min_validation_samples=int(args.min_validation_samples),
            purge_bars=int(args.predict_horizon_bars) + int(args.execution_latency_bars),
        )
        if len(x_fit_raw) < int(args.min_samples):
            split_reports.append({
                "split_id": split_id,
                "status": "skipped",
                "reason": "insufficient_fit_samples",
                "fit_samples": int(len(x_fit_raw)),
            })
            continue

        x_fit, transform = build_feature_transform(
            x_fit_raw,
            feature_names,
            feature_clip_quantile=float(args.feature_clip_quantile),
        )
        x_val = (
            apply_feature_transform(x_val_raw, feature_names, transform)
            if x_val_raw is not None
            else None
        )
        economic_mask = (
            economic_valid
            & (raw_indices >= int(split.test_start))
            & (raw_indices < int(split.test_end))
        )
        x_test = apply_feature_transform(
            raw_features[economic_mask], feature_names, transform
        )
        test_execution_return = execution_bar_return[economic_mask]
        model = build_regressor(args, variant)
        fit_kwargs: Dict[str, Any] = {}
        if x_val is not None and y_val is not None:
            fit_kwargs = {
                "eval_set": (x_val, y_val),
                "use_best_model": True,
                "early_stopping_rounds": int(args.early_stopping_rounds),
            }
        model.fit(x_fit, y_fit, **fit_kwargs)
        raw_prediction = model.predict(x_test)
        raw_score_scale = 1.0
        calibration: Dict[str, Any] = {
            "status": "fixed_economic_target_scale",
            "raw_score_scale": 1.0,
        }
        direction_mode = "both"
        if args.calibration_mode == "nested_validation_quantile":
            validation_start_raw = int(fit_meta.get("validation_start_raw", split.train_end))
            validation_economic_mask = (
                economic_valid
                & (raw_indices >= validation_start_raw)
                & (raw_indices < int(split.train_end))
            )
            if not np.any(validation_economic_mask):
                raw_score_scale = 0.0
                calibration = {
                    "status": "no_validation_economic_rows",
                    "raw_score_scale": 0.0,
                }
            else:
                x_economic_validation = apply_feature_transform(
                    raw_features[validation_economic_mask], feature_names, transform
                )
                raw_validation_prediction = model.predict(x_economic_validation)
                if path_objective_variant:
                    raw_score_scale, calibration = select_nested_validation_path_scale(
                        raw_prediction=raw_validation_prediction,
                        long_gross_bps=path_long_gross_bps[validation_economic_mask],
                        short_gross_bps=path_short_gross_bps[validation_economic_mask],
                        long_duration=path_long_duration[validation_economic_mask],
                        short_duration=path_short_duration[validation_economic_mask],
                        quantiles=args.calibration_quantiles,
                        round_trip_cost_bps=float(args.label_round_trip_cost_bps),
                        confidence_threshold=float(args.model_confidence_threshold),
                        min_trades=int(args.min_calibration_trades),
                    )
                else:
                    raw_score_scale, calibration = select_nested_validation_scale(
                        raw_prediction=raw_validation_prediction,
                        execution_bar_return=execution_bar_return[validation_economic_mask],
                        quantiles=args.calibration_quantiles,
                        round_trip_cost_bps=float(args.label_round_trip_cost_bps),
                        confidence_threshold=float(args.model_confidence_threshold),
                        holding_bars=int(args.predict_horizon_bars),
                        min_trades=int(args.min_calibration_trades),
                        direction_modes=(
                            DIRECTION_MODES
                            if variant in {
                                "continuous_return_huber_side_calibrated",
                                RESIDUAL_VARIANT,
                            }
                            else ("both",)
                        ),
                    )
                calibration["raw_score_scale"] = raw_score_scale
                calibration["validation_start_raw"] = validation_start_raw
                calibration["validation_end_raw_exclusive"] = int(split.train_end)
                selected_calibration = calibration.get("selected")
                if isinstance(selected_calibration, dict):
                    direction_mode = str(
                        selected_calibration.get("direction_mode", "both")
                    )
        probability = constrain_probability_direction(
            raw_score_to_probability(raw_prediction * raw_score_scale),
            direction_mode,
        )
        objective = (
            summarize_path_episode_objective(
                score=probability,
                long_gross_bps=path_long_gross_bps[economic_mask],
                short_gross_bps=path_short_gross_bps[economic_mask],
                long_duration=path_long_duration[economic_mask],
                short_duration=path_short_duration[economic_mask],
                round_trip_cost_bps=float(args.label_round_trip_cost_bps),
                confidence_threshold=float(args.model_confidence_threshold),
            )
            if path_objective_variant
            else summarize_model_episode_objective(
                score=probability,
                execution_bar_return=test_execution_return,
                round_trip_cost_bps=float(args.label_round_trip_cost_bps),
                confidence_threshold=float(args.model_confidence_threshold),
                holding_bars=int(args.predict_horizon_bars),
            )
        )
        objectives.append(objective)

        direction_mask = (
            np.isfinite(direction_label)
            & economic_mask
        )
        direction_indices = np.flatnonzero(direction_mask)
        score_by_raw_index = np.full(len(raw_features), np.nan, dtype=np.float64)
        # Directional rank quality is a model diagnostic and must not collapse
        # to 0.5 merely because the nested economic calibrator fails closed.
        score_by_raw_index[np.flatnonzero(economic_mask)] = raw_prediction
        split_auc = auc_score(
            direction_label[direction_indices],
            score_by_raw_index[direction_indices],
        )
        if math.isfinite(split_auc):
            directional_auc.append(split_auc)
        confidence = np.abs(2.0 * probability - 1.0)
        split_reports.append({
            "split_id": split_id,
            "status": "trained",
            "train_range": [int(split.train_start), int(split.train_end)],
            "test_range": [int(split.test_start), int(split.test_end)],
            "fit_window": fit_meta,
            "fit_sample_count": int(len(x_fit)),
            "test_sample_count": int(len(x_test)),
            "best_iteration": (
                int(model.get_best_iteration()) + 1
                if isinstance(model.get_best_iteration(), int)
                and model.get_best_iteration() >= 0
                else None
            ),
            "directional_auc_on_cost_band_rows": split_auc if math.isfinite(split_auc) else None,
            "raw_prediction_min": float(np.min(raw_prediction)),
            "raw_prediction_max": float(np.max(raw_prediction)),
            "confidence_max": float(np.max(confidence)),
            "calibration": calibration,
            "net_objective": objective,
        })

    aggregate = aggregate_economic_objectives(objectives)
    aggregate.update({
        "directional_auc_mean_on_cost_band_rows": (
            mean_finite(directional_auc) if directional_auc else None
        ),
        "trained_split_count": len(objectives),
        "split_count": len(splits),
        "passes_development_economic_screen": bool(
            aggregate["mean_model_net_edge_bps"] > 0.0
            and aggregate["model_net_total_trades"] >= int(args.min_model_net_total_trades)
            and aggregate["model_net_active_bar_count"] >= int(args.min_model_net_active_bars)
            and (aggregate["positive_model_net_edge_ratio_by_split"] or 0.0)
            >= float(args.min_positive_splits_ratio)
            and (aggregate["model_net_edge_lcb_bps"] or float("-inf")) > 0.0
        ),
    })
    return {
        "variant": variant,
        "target_contract": {
            "neutral_samples_included": True,
            "runtime_entry_raw_score": RUNTIME_ENTRY_RAW_SCORE,
            "threshold_bps": threshold_bps,
            "target_clip": float(args.target_clip),
            "loss_function": "Huber:delta=1.0" if "huber" in variant else "RMSE",
            "calibration_mode": args.calibration_mode,
            "direction_calibration": (
                "nested_validation_long_short_abstention"
                if variant in {
                    "continuous_return_huber_side_calibrated",
                    RESIDUAL_VARIANT,
                }
                else "both_sides"
            ),
            "return_target": (
                "bybit_sol_minus_equal_weight_binance_btc_eth"
                if variant
                == RESIDUAL_VARIANT
                else "bybit_sol_absolute"
            ),
            "economic_evaluation_return": "bybit_sol_absolute_after_real_cost",
            "exit_objective": (
                {
                    "mode": "path_first_touch",
                    "take_profit_bps": float(args.path_take_profit_bps),
                    "stop_loss_bps": float(args.path_stop_loss_bps),
                    "same_bar_conflict": "stop_first_conservative",
                }
                if path_objective_variant
                else {"mode": "fixed_horizon"}
            ),
        },
        "target_sample_count": int(np.sum(target_valid)),
        "metrics_development_oos": aggregate,
        "splits": split_reports,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--miner_report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--training_symbol", default="SOLUSDT")
    parser.add_argument("--bar_interval_ms", type=int, default=300000)
    parser.add_argument("--predict_horizon_bars", type=int, default=12)
    parser.add_argument("--execution_latency_bars", type=int, default=1)
    parser.add_argument("--label_round_trip_cost_bps", type=float, default=13.0)
    parser.add_argument("--label_min_net_edge_bps", type=float, default=1.3)
    parser.add_argument("--path_take_profit_bps", type=float, default=32.0)
    parser.add_argument("--path_stop_loss_bps", type=float, default=20.0)
    parser.add_argument("--model_confidence_threshold", type=float, default=0.5)
    parser.add_argument("--top_k", type=int, default=10)
    parser.add_argument("--feature_set", choices=FEATURE_SETS, default="baseline")
    parser.add_argument("--derivatives_csv", default="")
    parser.add_argument("--market_alpha_csv", default="")
    parser.add_argument("--research_domain", default="development", choices=("development",))
    parser.add_argument("--n_splits", type=int, default=10)
    parser.add_argument("--train_window_bars", type=int, default=17280)
    parser.add_argument("--test_window_bars", type=int, default=2016)
    parser.add_argument("--rolling_step_bars", type=int, default=2016)
    parser.add_argument("--iterations", type=int, default=150)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--learning_rate", type=float, default=0.03)
    parser.add_argument("--l2_leaf_reg", type=float, default=20.0)
    parser.add_argument("--random_strength", type=float, default=2.0)
    parser.add_argument("--subsample", type=float, default=0.75)
    parser.add_argument("--rsm", type=float, default=0.8)
    parser.add_argument("--random_seed", type=int, default=20260301)
    parser.add_argument("--validation_fraction", type=float, default=0.15)
    parser.add_argument("--min_validation_samples", type=int, default=500)
    parser.add_argument("--early_stopping_rounds", type=int, default=30)
    parser.add_argument("--feature_clip_quantile", type=float, default=0.001)
    parser.add_argument("--target_clip", type=float, default=6.0)
    parser.add_argument(
        "--calibration_mode",
        choices=("fixed_economic", "nested_validation_quantile"),
        default="fixed_economic",
    )
    parser.add_argument(
        "--calibration_quantiles",
        default="0.50,0.60,0.70,0.80,0.90,0.95,0.98",
    )
    parser.add_argument("--min_calibration_trades", type=int, default=5)
    parser.add_argument("--min_samples", type=int, default=5000)
    parser.add_argument("--min_model_net_total_trades", type=int, default=20)
    parser.add_argument("--min_model_net_active_bars", type=int, default=100)
    parser.add_argument("--min_positive_splits_ratio", type=float, default=0.5)
    parser.add_argument(
        "--variants",
        default=",".join(DEFAULT_PROBE_VARIANTS),
        help="comma-separated fixed probe variants",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if CatBoostRegressor is None:
        raise SystemExit("[ERROR] catboost is required; use ai-trade-research image")
    variants = [item.strip() for item in str(args.variants).split(",") if item.strip()]
    unknown = [item for item in variants if item not in PROBE_VARIANTS]
    if not variants or unknown:
        raise ValueError(f"invalid variants: {unknown or variants}")
    if len(set(variants)) != len(variants):
        raise ValueError("duplicate variants are forbidden")
    if int(args.execution_latency_bars) < 1:
        raise ValueError("execution_latency_bars must be >= 1")
    if float(args.path_take_profit_bps) <= 0.0 or float(args.path_stop_loss_bps) <= 0.0:
        raise ValueError("path take-profit/stop-loss bps must be > 0")
    if int(args.rolling_step_bars) < int(args.test_window_bars):
        raise ValueError("overlapping OOS test windows are forbidden")
    args.calibration_quantiles = [
        float(item.strip())
        for item in str(args.calibration_quantiles).split(",")
        if item.strip()
    ]
    if not args.calibration_quantiles:
        raise ValueError("calibration_quantiles cannot be empty")
    if len(set(args.calibration_quantiles)) != len(args.calibration_quantiles):
        raise ValueError("duplicate calibration quantiles are forbidden")
    if int(args.min_calibration_trades) <= 0:
        raise ValueError("min_calibration_trades must be > 0")

    csv_path = pathlib.Path(args.csv)
    miner_path = pathlib.Path(args.miner_report)
    assert_development_only_path(csv_path, "OHLCV CSV")
    validate_miner_development_contract(miner_path, int(args.predict_horizon_bars))
    series = load_ohlcv_csv(csv_path)
    time_axis_quality = validate_time_axis(series["timestamp"], int(args.bar_interval_ms))
    factor_set_version, factors = load_factor_specs(
        miner_path,
        max(1, int(args.top_k)),
        expected_horizon_bars=int(args.predict_horizon_bars),
        expected_execution_latency_bars=int(args.execution_latency_bars),
    )
    raw_features, feature_names, ret_1 = build_feature_matrix(series, factors)
    if args.feature_set != "baseline":
        expanded_features, expanded_names = build_expanded_ohlcv_features(series)
        raw_features = np.column_stack((raw_features, expanded_features))
        feature_names = [*feature_names, *expanded_names]
    cross_asset_closes: Dict[str, np.ndarray] | None = None
    if args.feature_set in {
        "expanded_derivatives_v1",
        "expanded_market_alpha_derivatives_v1",
    }:
        if not str(args.derivatives_csv).strip():
            raise ValueError(f"{args.feature_set} requires --derivatives_csv")
        assert_development_only_path(
            pathlib.Path(args.derivatives_csv), "derivatives CSV"
        )
        derivative_features, derivative_names = load_derivatives_features(
            pathlib.Path(args.derivatives_csv), series["timestamp"]
        )
        raw_features = np.column_stack((raw_features, derivative_features))
        feature_names = [*feature_names, *derivative_names]
    if args.feature_set in {
        "expanded_market_alpha_v1",
        "expanded_market_alpha_derivatives_v1",
    }:
        if not str(args.market_alpha_csv).strip():
            raise ValueError(f"{args.feature_set} requires --market_alpha_csv")
        assert_development_only_path(
            pathlib.Path(args.market_alpha_csv), "market alpha CSV"
        )
        market_features, market_names = load_market_alpha_features(
            pathlib.Path(args.market_alpha_csv),
            series["timestamp"],
            series["close"],
        )
        raw_features = np.column_stack((raw_features, market_features))
        feature_names = [*feature_names, *market_names]
        cross_asset_closes = load_market_alpha_closes(
            pathlib.Path(args.market_alpha_csv), series["timestamp"]
        )
    direction_label, forward_return = build_label(
        series["close"],
        int(args.predict_horizon_bars),
        label_round_trip_cost_bps=float(args.label_round_trip_cost_bps),
        label_min_net_edge_bps=float(args.label_min_net_edge_bps),
        execution_latency_bars=int(args.execution_latency_bars),
    )
    residual_forward_return = (
        build_cross_asset_residual_forward_return(
            forward_return,
            cross_asset_closes,
            horizon_bars=int(args.predict_horizon_bars),
            execution_latency_bars=int(args.execution_latency_bars),
        )
        if cross_asset_closes is not None
        else None
    )
    execution_bar_return = build_execution_bar_returns(
        ret_1,
        execution_latency_bars=int(args.execution_latency_bars),
    )
    (
        path_long_gross_bps,
        path_short_gross_bps,
        path_long_duration,
        path_short_duration,
    ) = build_path_first_touch_outcomes(
        series,
        execution_latency_bars=int(args.execution_latency_bars),
        horizon_bars=int(args.predict_horizon_bars),
        take_profit_bps=float(args.path_take_profit_bps),
        stop_loss_bps=float(args.path_stop_loss_bps),
    )
    purge_bars = int(args.predict_horizon_bars) + int(args.execution_latency_bars)
    splits = build_splits(
        sample_count=len(raw_features),
        method="rolling",
        n_splits=int(args.n_splits),
        train_window=int(args.train_window_bars),
        test_window=int(args.test_window_bars),
        step_window=int(args.rolling_step_bars),
        purge_bars=purge_bars,
    )
    threshold_bps = float(args.label_round_trip_cost_bps) + float(args.label_min_net_edge_bps)
    output_path = pathlib.Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    reports: List[Dict[str, Any]] = []
    for variant in variants:
        residual_variant = (
            variant == RESIDUAL_VARIANT
        )
        if residual_variant and residual_forward_return is None:
            raise ValueError(
                "cross-asset residual variant requires a market-alpha feature set"
            )
        reports.append(run_variant(
            args=args,
            variant=variant,
            raw_features=raw_features,
            feature_names=feature_names,
            forward_return=forward_return,
            target_forward_return=(
                residual_forward_return if residual_variant else None
            ),
            direction_label=direction_label,
            execution_bar_return=execution_bar_return,
            splits=splits,
            threshold_bps=threshold_bps,
            path_long_gross_bps=path_long_gross_bps,
            path_short_gross_bps=path_short_gross_bps,
            path_long_duration=path_long_duration,
            path_short_duration=path_short_duration,
        ))
        checkpoint = {
            "schema_version": "economic_target_probe_v1",
            "status": "in_progress",
            "research_domain": "development_only",
            "promotion_evidence": False,
            "completed_variant_count": len(reports),
            "requested_variant_count": len(variants),
            "variants": reports,
        }
        output_path.write_text(
            json.dumps(json_safe(checkpoint), ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
    payload = {
        "schema_version": "economic_target_probe_v1",
        "status": "diagnostic_complete",
        "research_domain": "development_only",
        "promotion_evidence": False,
        "selection_domain_validation_required": True,
        "untouched_final_holdout_required": True,
        "multiple_hypothesis_count": len(variants),
        "nested_calibration_hypothesis_count": (
            len(args.calibration_quantiles)
            if args.calibration_mode == "nested_validation_quantile"
            else 1
        ),
        "data": {
            "csv": str(csv_path),
            "csv_sha256": sha256_file(csv_path),
            "miner_report": str(miner_path),
            "miner_report_sha256": sha256_file(miner_path),
            "training_symbol": str(args.training_symbol).strip().upper(),
            "bar_interval_ms": int(args.bar_interval_ms),
            "row_count": int(len(raw_features)),
            "time_axis_quality": time_axis_quality,
            "factor_set_version": factor_set_version,
            "feature_count": len(feature_names),
            "feature_set": args.feature_set,
            "derivatives_csv": str(args.derivatives_csv) or None,
            "derivatives_csv_sha256": (
                sha256_file(pathlib.Path(args.derivatives_csv))
                if str(args.derivatives_csv).strip()
                else None
            ),
            "market_alpha_csv": str(args.market_alpha_csv) or None,
            "market_alpha_csv_sha256": (
                sha256_file(pathlib.Path(args.market_alpha_csv))
                if str(args.market_alpha_csv).strip()
                else None
            ),
        },
        "split_contract": {
            "method": "rolling",
            "train_window_bars": int(args.train_window_bars),
            "test_window_bars": int(args.test_window_bars),
            "rolling_step_bars": int(args.rolling_step_bars),
            "purge_bars": purge_bars,
            "oos_windows_non_overlapping": True,
            "split_count": len(splits),
        },
        "execution_contract": {
            "predict_horizon_bars": int(args.predict_horizon_bars),
            "execution_latency_bars": int(args.execution_latency_bars),
            "round_trip_cost_bps": float(args.label_round_trip_cost_bps),
            "minimum_net_edge_bps": float(args.label_min_net_edge_bps),
            "path_take_profit_bps": float(args.path_take_profit_bps),
            "path_stop_loss_bps": float(args.path_stop_loss_bps),
            "confidence_definition": "abs(2*sigmoid(raw_score)-1)",
            "confidence_threshold": float(args.model_confidence_threshold),
        },
        "variants": reports,
    }
    output_path.write_text(
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    concise = {
        item["variant"]: item["metrics_development_oos"] for item in reports
    }
    print(json.dumps(json_safe(concise), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
