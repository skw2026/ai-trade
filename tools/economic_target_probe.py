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
    "ternary_action_rmse",
)
FEATURE_SETS = ("baseline", "expanded_ohlcv_v1", "expanded_derivatives_v1")


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


def raw_score_to_probability(raw_score: np.ndarray) -> np.ndarray:
    raw = np.asarray(raw_score, dtype=np.float64)
    return 1.0 / (1.0 + np.exp(-np.clip(raw, -60.0, 60.0)))


def select_nested_validation_scale(
    *,
    raw_prediction: np.ndarray,
    execution_bar_return: np.ndarray,
    quantiles: Sequence[float],
    round_trip_cost_bps: float,
    confidence_threshold: float,
    holding_bars: int,
    min_trades: int,
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
        objective = summarize_model_episode_objective(
            score=probability,
            execution_bar_return=execution_bar_return,
            round_trip_cost_bps=round_trip_cost_bps,
            confidence_threshold=confidence_threshold,
            holding_bars=holding_bars,
        )
        trade_count = int(objective.get("trade_count", 0))
        mean_net = float(objective.get("mean_model_net_edge_bps", 0.0))
        positive_ratio = float(objective.get("positive_model_net_edge_ratio", float("nan")))
        eligible = bool(
            trade_count >= int(min_trades)
            and mean_net > 0.0
            and math.isfinite(positive_ratio)
            and positive_ratio >= 0.5
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
    loss_function = "Huber:delta=1.0" if variant.endswith("_huber") else "RMSE"
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
    direction_label: np.ndarray,
    execution_bar_return: np.ndarray,
    splits: Sequence[Any],
    threshold_bps: float,
) -> Dict[str, Any]:
    target = build_target(
        forward_return,
        variant=variant,
        threshold_bps=threshold_bps,
        target_clip=float(args.target_clip),
    )
    finite_features = np.all(np.isfinite(raw_features), axis=1)
    target_valid = finite_features & np.isfinite(target)
    economic_valid = finite_features & np.isfinite(execution_bar_return)
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
                raw_score_scale, calibration = select_nested_validation_scale(
                    raw_prediction=raw_validation_prediction,
                    execution_bar_return=execution_bar_return[validation_economic_mask],
                    quantiles=args.calibration_quantiles,
                    round_trip_cost_bps=float(args.label_round_trip_cost_bps),
                    confidence_threshold=float(args.model_confidence_threshold),
                    holding_bars=int(args.predict_horizon_bars),
                    min_trades=int(args.min_calibration_trades),
                )
                calibration["raw_score_scale"] = raw_score_scale
                calibration["validation_start_raw"] = validation_start_raw
                calibration["validation_end_raw_exclusive"] = int(split.train_end)
        probability = raw_score_to_probability(raw_prediction * raw_score_scale)
        objective = summarize_model_episode_objective(
            score=probability,
            execution_bar_return=test_execution_return,
            round_trip_cost_bps=float(args.label_round_trip_cost_bps),
            confidence_threshold=float(args.model_confidence_threshold),
            holding_bars=int(args.predict_horizon_bars),
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
            "loss_function": "Huber:delta=1.0" if variant.endswith("_huber") else "RMSE",
            "calibration_mode": args.calibration_mode,
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
    parser.add_argument("--model_confidence_threshold", type=float, default=0.5)
    parser.add_argument("--top_k", type=int, default=10)
    parser.add_argument("--feature_set", choices=FEATURE_SETS, default="baseline")
    parser.add_argument("--derivatives_csv", default="")
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
        default=",".join(PROBE_VARIANTS),
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
    series = load_ohlcv_csv(csv_path)
    time_axis_quality = validate_time_axis(series["timestamp"], int(args.bar_interval_ms))
    factor_set_version, factors = load_factor_specs(
        miner_path,
        max(1, int(args.top_k)),
        expected_horizon_bars=int(args.predict_horizon_bars),
        expected_execution_latency_bars=int(args.execution_latency_bars),
    )
    raw_features, feature_names, ret_1 = build_feature_matrix(series, factors)
    if args.feature_set in {"expanded_ohlcv_v1", "expanded_derivatives_v1"}:
        expanded_features, expanded_names = build_expanded_ohlcv_features(series)
        raw_features = np.column_stack((raw_features, expanded_features))
        feature_names = [*feature_names, *expanded_names]
    if args.feature_set == "expanded_derivatives_v1":
        if not str(args.derivatives_csv).strip():
            raise ValueError("expanded_derivatives_v1 requires --derivatives_csv")
        derivative_features, derivative_names = load_derivatives_features(
            pathlib.Path(args.derivatives_csv), series["timestamp"]
        )
        raw_features = np.column_stack((raw_features, derivative_features))
        feature_names = [*feature_names, *derivative_names]
    direction_label, forward_return = build_label(
        series["close"],
        int(args.predict_horizon_bars),
        label_round_trip_cost_bps=float(args.label_round_trip_cost_bps),
        label_min_net_edge_bps=float(args.label_min_net_edge_bps),
        execution_latency_bars=int(args.execution_latency_bars),
    )
    execution_bar_return = build_execution_bar_returns(
        ret_1,
        execution_latency_bars=int(args.execution_latency_bars),
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
        reports.append(run_variant(
            args=args,
            variant=variant,
            raw_features=raw_features,
            feature_names=feature_names,
            forward_return=forward_return,
            direction_label=direction_label,
            execution_bar_return=execution_bar_return,
            splits=splits,
            threshold_bps=threshold_bps,
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
            "training_symbol": str(args.training_symbol).strip().upper(),
            "bar_interval_ms": int(args.bar_interval_ms),
            "row_count": int(len(raw_features)),
            "time_axis_quality": time_axis_quality,
            "factor_set_version": factor_set_version,
            "feature_count": len(feature_names),
            "feature_set": args.feature_set,
            "derivatives_csv": str(args.derivatives_csv) or None,
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
