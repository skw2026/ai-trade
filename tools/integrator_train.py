#!/usr/bin/env python3
"""
Integrator 离线训练工具（Stage R2）。

目标：
1. 读取 Miner 产出的因子表达式；
2. 基于时序安全切分（Rolling / TimeSeriesSplit）训练 CatBoost；
3. 产出可审计训练报告（model_version / feature_schema_version /
   metrics_oos / feature_importance）。

注意：
- 该脚本是离线研究工具，不会直接驱动线上下单；
- 标签对齐口径固定：t 时刻特征，预测 t+1...t+h 的收益方向；
- 可选成本带标签会丢弃净收益不足的中性样本，避免训练目标偏离执行经济性。
"""

from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:
    from catboost import CatBoostClassifier
except ImportError:  # pragma: no cover
    CatBoostClassifier = None


def log_info(message: str) -> None:
    print(f"[INFO] {message}")


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def to_iso_utc(timestamp_ms: int) -> str:
    return dt.datetime.utcfromtimestamp(timestamp_ms / 1000.0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def sanitize_array(values: np.ndarray) -> np.ndarray:
    out = values.astype(np.float64, copy=True)
    out[~np.isfinite(out)] = np.nan
    return out


def finite_json_number(value: float) -> float | None:
    return float(value) if math.isfinite(float(value)) else None


def build_feature_transform(
    features: np.ndarray,
    feature_names: Sequence[str],
    feature_clip_quantile: float,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    transformed = sanitize_array(features)
    q = max(0.0, min(0.49, float(feature_clip_quantile)))
    clip_bounds: List[Dict[str, Any]] = []
    if q <= 0.0:
        return transformed, {
            "feature_clip_requested": False,
            "feature_clipping_enabled": False,
            "feature_normalization_enabled": False,
            "normalization_method": "none",
            "clip_quantile": 0.0,
            "enabled_clip_bound_count": 0,
            "enabled_normalization_count": 0,
            "normalization_max_abs": None,
            "clip_bounds": [],
        }

    normalization_max_abs = 8.0
    for index, name in enumerate(feature_names):
        column = transformed[:, index]
        finite = np.isfinite(column)
        finite_values = column[finite]
        raw_min = float(np.min(finite_values)) if finite_values.size > 0 else float("nan")
        raw_max = float(np.max(finite_values)) if finite_values.size > 0 else float("nan")
        enabled = False
        lower = float("nan")
        upper = float("nan")
        clipped_low = 0
        clipped_high = 0
        if finite_values.size >= 20:
            lower = float(np.nanquantile(finite_values, q))
            upper = float(np.nanquantile(finite_values, 1.0 - q))
            enabled = (
                math.isfinite(lower)
                and math.isfinite(upper)
                and lower <= upper
                and (lower > raw_min or upper < raw_max)
            )
        if enabled:
            clipped_low = int(np.sum(finite & (column < lower)))
            clipped_high = int(np.sum(finite & (column > upper)))
            column[finite] = np.clip(column[finite], lower, upper)
            transformed[:, index] = column
        clipped_finite_values = column[finite]
        center = float("nan")
        scale = float("nan")
        normalization_enabled = False
        normalized_low = 0
        normalized_high = 0
        if clipped_finite_values.size >= 20:
            center = float(np.nanmedian(clipped_finite_values))
            q25 = float(np.nanquantile(clipped_finite_values, 0.25))
            q75 = float(np.nanquantile(clipped_finite_values, 0.75))
            robust_scale = (q75 - q25) / 1.349 if math.isfinite(q75 - q25) else float("nan")
            std_scale = float(np.nanstd(clipped_finite_values))
            if math.isfinite(robust_scale) and robust_scale > 1e-12:
                scale = robust_scale
            elif math.isfinite(std_scale) and std_scale > 1e-12:
                scale = std_scale
            elif math.isfinite(center):
                scale = max(abs(center), 1.0)
            normalization_enabled = (
                math.isfinite(center)
                and math.isfinite(scale)
                and scale > 1e-12
            )
        if normalization_enabled:
            normalized = (column[finite] - center) / scale
            normalized_low = int(np.sum(normalized < -normalization_max_abs))
            normalized_high = int(np.sum(normalized > normalization_max_abs))
            column[finite] = np.clip(
                normalized,
                -normalization_max_abs,
                normalization_max_abs,
            )
            transformed[:, index] = column
        clip_bounds.append(
            {
                "feature": name,
                "enabled": bool(enabled),
                "lower": finite_json_number(lower),
                "upper": finite_json_number(upper),
                "finite_count": int(finite_values.size),
                "raw_min": finite_json_number(raw_min),
                "raw_max": finite_json_number(raw_max),
                "clipped_low_count": clipped_low,
                "clipped_high_count": clipped_high,
                "normalization_enabled": bool(normalization_enabled),
                "normalization_method": "median_iqr" if normalization_enabled else "none",
                "center": finite_json_number(center),
                "scale": finite_json_number(scale),
                "normalized_max_abs": normalization_max_abs if normalization_enabled else None,
                "normalized_clipped_low_count": normalized_low,
                "normalized_clipped_high_count": normalized_high,
            }
        )
    enabled_bound_count = sum(1 for item in clip_bounds if bool(item.get("enabled")))
    enabled_normalization_count = sum(
        1 for item in clip_bounds if bool(item.get("normalization_enabled"))
    )
    return transformed, {
        "feature_clip_requested": True,
        "feature_clipping_enabled": enabled_bound_count > 0,
        "feature_normalization_enabled": enabled_normalization_count > 0,
        "normalization_method": "median_iqr",
        "clip_quantile": q,
        "enabled_clip_bound_count": enabled_bound_count,
        "enabled_normalization_count": enabled_normalization_count,
        "normalization_max_abs": normalization_max_abs,
        "clip_bounds": clip_bounds,
    }


def apply_feature_transform(
    features: np.ndarray,
    feature_names: Sequence[str],
    transform: Dict[str, Any],
) -> np.ndarray:
    transformed = sanitize_array(features)
    bounds = transform.get("clip_bounds", [])
    if not isinstance(bounds, list) or not bounds:
        return transformed
    bounds_by_name = {
        str(item.get("feature")): item
        for item in bounds
        if isinstance(item, dict) and item.get("feature")
    }
    for index, name in enumerate(feature_names):
        item = bounds_by_name.get(name)
        if not isinstance(item, dict):
            continue
        column = transformed[:, index]
        finite = np.isfinite(column)
        if bool(item.get("enabled")):
            lower = item.get("lower")
            upper = item.get("upper")
            if lower is not None and upper is not None:
                column[finite] = np.clip(column[finite], float(lower), float(upper))
        if bool(item.get("normalization_enabled")):
            center = item.get("center")
            scale = item.get("scale")
            max_abs = item.get("normalized_max_abs")
            if center is not None and scale is not None and float(scale) > 1e-12:
                normalized = (column[finite] - float(center)) / float(scale)
                if max_abs is not None:
                    normalized = np.clip(
                        normalized,
                        -float(max_abs),
                        float(max_abs),
                    )
                column[finite] = normalized
        transformed[:, index] = column
    return transformed


def ts_delay(x: np.ndarray, window: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=np.float64)
    if window <= 0:
        return out
    out[window:] = x[:-window]
    return out


def ts_delta(x: np.ndarray, window: int) -> np.ndarray:
    if window <= 0:
        return np.full_like(x, np.nan, dtype=np.float64)
    return x - ts_delay(x, window)


def ts_rank(x: np.ndarray, window: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=np.float64)
    if window <= 0:
        return out
    n = len(x)
    for i in range(window - 1, n):
        sample = x[i - window + 1 : i + 1]
        if not np.all(np.isfinite(sample)):
            continue
        last = sample[-1]
        smaller = float(np.sum(sample < last))
        equal = float(np.sum(sample == last))
        # 与 C++ OnlineFeatureEngine 对齐：tie-aware 百分位秩。
        out[i] = (smaller + 0.5 * equal) / float(window)
    return out


def ts_corr(x: np.ndarray, y: np.ndarray, window: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=np.float64)
    if window <= 1 or len(x) != len(y):
        return out
    n = len(x)
    for i in range(window - 1, n):
        xs = x[i - window + 1 : i + 1]
        ys = y[i - window + 1 : i + 1]
        if not np.all(np.isfinite(xs)) or not np.all(np.isfinite(ys)):
            continue
        std_x = float(np.std(xs))
        std_y = float(np.std(ys))
        if std_x <= 0.0 or std_y <= 0.0:
            continue
        corr = float(np.corrcoef(xs, ys)[0, 1])
        if math.isfinite(corr):
            out[i] = corr
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(values, np.nan, dtype=np.float64)
    if period <= 0:
        return out
    alpha = 2.0 / (period + 1.0)
    running = np.nan
    for i, value in enumerate(values):
        if not math.isfinite(float(value)):
            continue
        if not math.isfinite(running):
            running = float(value)
        else:
            running = alpha * float(value) + (1.0 - alpha) * running
        out[i] = running
    return out


def rsi(close: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(close, np.nan, dtype=np.float64)
    if period <= 1 or len(close) <= period:
        return out
    delta = np.full_like(close, np.nan, dtype=np.float64)
    delta[1:] = close[1:] - close[:-1]
    gains = np.where(delta > 0.0, delta, 0.0)
    losses = np.where(delta < 0.0, -delta, 0.0)
    for i in range(period, len(close)):
        g = gains[i - period + 1 : i + 1]
        l = losses[i - period + 1 : i + 1]
        if not np.all(np.isfinite(g)) or not np.all(np.isfinite(l)):
            continue
        avg_gain = float(np.mean(g))
        avg_loss = float(np.mean(l))
        if avg_loss <= 1e-12:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


class SafeExpressionEvaluator:
    """
    受限表达式求值器：
    - 允许 Name: open/high/low/close/volume
    - 允许运算: + - * / 一元 ±
    - 允许函数: ts_delay/ts_delta/ts_rank/ts_corr/abs
    """

    def __init__(self, series: Dict[str, np.ndarray]) -> None:
        self.series = series
        self.length = len(next(iter(series.values())))

    def evaluate(self, expression: str) -> np.ndarray:
        node = ast.parse(expression, mode="eval")
        result = self._eval(node.body)
        if isinstance(result, np.ndarray):
            if result.shape[0] != self.length:
                raise ValueError(f"表达式结果长度异常: {expression}")
            return sanitize_array(result)
        # 标量常量表达式会扩展为同长度向量。
        return np.full(self.length, float(result), dtype=np.float64)

    def _to_array(self, value: np.ndarray | float) -> np.ndarray:
        if isinstance(value, np.ndarray):
            return value.astype(np.float64, copy=False)
        return np.full(self.length, float(value), dtype=np.float64)

    @staticmethod
    def _to_window(value: np.ndarray | float) -> int:
        if isinstance(value, np.ndarray):
            finite = value[np.isfinite(value)]
            if finite.size == 0:
                return 0
            return int(round(float(finite[0])))
        return int(round(float(value)))

    def _eval(self, node: ast.AST) -> np.ndarray | float:
        if isinstance(node, ast.Name):
            if node.id not in self.series:
                raise ValueError(f"不支持的变量名: {node.id}")
            return self.series[node.id]

        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return float(node.value)
            raise ValueError(f"不支持的常量: {node.value}")

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = self._eval(node.operand)
            if isinstance(node.op, ast.UAdd):
                return operand
            return -self._to_array(operand) if isinstance(operand, np.ndarray) else -operand

        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            left = self._eval(node.left)
            right = self._eval(node.right)
            lhs = self._to_array(left)
            rhs = self._to_array(right)
            if isinstance(node.op, ast.Add):
                return lhs + rhs
            if isinstance(node.op, ast.Sub):
                return lhs - rhs
            if isinstance(node.op, ast.Mult):
                return lhs * rhs
            # 除法采用安全处理，避免 inf 污染。
            out = np.full(self.length, np.nan, dtype=np.float64)
            valid = np.isfinite(lhs) & np.isfinite(rhs) & (np.abs(rhs) > 1e-12)
            out[valid] = lhs[valid] / rhs[valid]
            return out

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = node.func.id
            args = [self._eval(arg) for arg in node.args]
            if fn == "abs":
                if len(args) != 1:
                    raise ValueError("abs 仅支持 1 个参数")
                return np.abs(self._to_array(args[0]))
            if fn == "ts_delay":
                if len(args) != 2:
                    raise ValueError("ts_delay 需要 2 个参数")
                return ts_delay(self._to_array(args[0]), self._to_window(args[1]))
            if fn == "ts_delta":
                if len(args) != 2:
                    raise ValueError("ts_delta 需要 2 个参数")
                return ts_delta(self._to_array(args[0]), self._to_window(args[1]))
            if fn == "ts_rank":
                if len(args) != 2:
                    raise ValueError("ts_rank 需要 2 个参数")
                return ts_rank(self._to_array(args[0]), self._to_window(args[1]))
            if fn == "ts_corr":
                if len(args) != 3:
                    raise ValueError("ts_corr 需要 3 个参数")
                return ts_corr(
                    self._to_array(args[0]),
                    self._to_array(args[1]),
                    self._to_window(args[2]),
                )
            raise ValueError(f"不支持的函数: {fn}")

        raise ValueError(f"不支持的表达式节点: {ast.dump(node)}")


def load_ohlcv_csv(csv_path: pathlib.Path) -> Dict[str, np.ndarray]:
    if not csv_path.exists():
        raise FileNotFoundError(f"未找到输入文件: {csv_path}")

    timestamps: List[int] = []
    opens: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    closes: List[float] = []
    volumes: List[float] = []

    with csv_path.open("r", newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        required = {"open", "high", "low", "close", "volume"}
        headers = {h.lower() for h in (reader.fieldnames or [])}
        if not required.issubset(headers):
            raise ValueError("CSV 缺少必需列: open/high/low/close/volume")
        ts_key = None
        for candidate in ("timestamp", "ts", "time"):
            if candidate in headers:
                ts_key = candidate
                break

        for row in reader:
            ts_ms = int(row[ts_key]) if ts_key is not None else len(timestamps)
            timestamps.append(ts_ms)
            opens.append(float(row["open"]))
            highs.append(float(row["high"]))
            lows.append(float(row["low"]))
            closes.append(float(row["close"]))
            volumes.append(float(row["volume"]))

    return {
        "timestamp": np.asarray(timestamps, dtype=np.int64),
        "open": np.asarray(opens, dtype=np.float64),
        "high": np.asarray(highs, dtype=np.float64),
        "low": np.asarray(lows, dtype=np.float64),
        "close": np.asarray(closes, dtype=np.float64),
        "volume": np.asarray(volumes, dtype=np.float64),
    }


def validate_time_axis(timestamp: np.ndarray, bar_interval_ms: int) -> Dict[str, int]:
    if len(timestamp) < 2:
        raise ValueError("OHLCV 时间轴至少需要 2 根 bar")
    interval = int(bar_interval_ms)
    if interval <= 0:
        raise ValueError("bar_interval_ms 必须 > 0")
    deltas = np.diff(timestamp.astype(np.int64))
    duplicate_count = int(np.sum(deltas == 0))
    non_monotonic_count = int(np.sum(deltas < 0))
    gap_count = int(np.sum(deltas != interval))
    if duplicate_count or non_monotonic_count or gap_count:
        raise ValueError(
            "OHLCV 时间轴不满足严格固定周期: "
            f"duplicates={duplicate_count}, non_monotonic={non_monotonic_count}, "
            f"gap_or_interval_mismatch={gap_count}, interval_ms={interval}"
        )
    return {
        "rows": int(len(timestamp)),
        "duplicate_count": duplicate_count,
        "non_monotonic_count": non_monotonic_count,
        "gap_or_interval_mismatch_count": gap_count,
        "bar_interval_ms": interval,
    }


@dataclass
class FactorSpec:
    expression: str
    invert_signal: bool


def load_factor_specs(
    report_path: pathlib.Path,
    top_k: int,
    *,
    expected_horizon_bars: int,
    expected_execution_latency_bars: int,
) -> Tuple[str, List[FactorSpec]]:
    with report_path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)

    factor_set_version = str(payload.get("factor_set_version", "unknown_factor_set"))
    miner_horizon = int(payload.get("predict_horizon_bars", -1))
    miner_latency = int(payload.get("execution_latency_bars", -1))
    miner_purge = int(payload.get("purge_bars", -1))
    required_purge = int(expected_horizon_bars) + int(
        expected_execution_latency_bars
    )
    if (
        miner_horizon != int(expected_horizon_bars)
        or miner_latency != int(expected_execution_latency_bars)
        or miner_purge < required_purge
    ):
        raise ValueError(
            "miner/integrator 标签时间契约不一致: "
            f"miner_horizon={miner_horizon}, expected_horizon={expected_horizon_bars}, "
            f"miner_latency={miner_latency}, "
            f"expected_latency={expected_execution_latency_bars}, "
            f"miner_purge={miner_purge}, required_purge={required_purge}"
        )
    factors = payload.get("factors", [])
    specs: List[FactorSpec] = []
    used = set()
    for item in factors:
        expr = str(item.get("expression", "")).strip()
        if not expr or expr in used:
            continue
        specs.append(FactorSpec(expression=expr, invert_signal=bool(item.get("invert_signal", False))))
        used.add(expr)
        if len(specs) >= top_k:
            break

    if not specs:
        raise ValueError("miner_report 中未找到可用因子表达式")
    return factor_set_version, specs


def build_feature_matrix(
    series: Dict[str, np.ndarray], factor_specs: Sequence[FactorSpec]
) -> Tuple[np.ndarray, List[str], np.ndarray]:
    evaluator = SafeExpressionEvaluator(
        {
            "open": series["open"],
            "high": series["high"],
            "low": series["low"],
            "close": series["close"],
            "volume": series["volume"],
        }
    )

    feature_arrays: List[np.ndarray] = []
    feature_names: List[str] = []

    for index, spec in enumerate(factor_specs):
        values = evaluator.evaluate(spec.expression)
        if spec.invert_signal:
            values = -values
        feature_arrays.append(values)
        feature_names.append(f"miner_{index:02d}")

    close = series["close"]
    volume = series["volume"]
    ret_1 = ts_delta(close, 1) / (np.abs(ts_delay(close, 1)) + 1e-9)
    ret_3 = ts_delta(close, 3) / (np.abs(ts_delay(close, 3)) + 1e-9)
    vol_delta_1 = ts_delta(volume, 1)
    rsi_14 = rsi(close, 14)
    macd_line = ema(close, 12) - ema(close, 26)
    macd_signal = ema(macd_line, 9)
    macd_hist = macd_line - macd_signal

    classical_features = {
        "ret_1": ret_1,
        "ret_3": ret_3,
        "vol_delta_1": vol_delta_1,
        "rsi_14": rsi_14,
        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
    }
    for name, values in classical_features.items():
        feature_names.append(name)
        feature_arrays.append(values)

    matrix = np.column_stack(feature_arrays)
    matrix = sanitize_array(matrix)
    return matrix, feature_names, ret_1


def build_label(
    close: np.ndarray,
    horizon: int,
    label_round_trip_cost_bps: float = 0.0,
    label_min_net_edge_bps: float = 0.0,
    execution_latency_bars: int = 1,
) -> Tuple[np.ndarray, np.ndarray]:
    n = len(close)
    forward_return = np.full(n, np.nan, dtype=np.float64)
    if horizon <= 0:
        return forward_return, np.full(n, np.nan, dtype=np.float64)
    latency = max(0, int(execution_latency_bars))
    # feature@t only earns returns after the declared execution latency.
    for i in range(n - horizon - latency):
        base = close[i + latency]
        future = close[i + horizon + latency]
        if not math.isfinite(float(base)) or abs(base) < 1e-12 or not math.isfinite(float(future)):
            continue
        forward_return[i] = future / base - 1.0
    threshold = (
        max(0.0, float(label_round_trip_cost_bps))
        + max(0.0, float(label_min_net_edge_bps))
    ) / 10000.0
    if threshold <= 0.0:
        label = np.where(np.isfinite(forward_return), (forward_return > 0.0).astype(np.float64), np.nan)
        return label, forward_return

    label = np.full(n, np.nan, dtype=np.float64)
    finite = np.isfinite(forward_return)
    label[finite & (forward_return > threshold)] = 1.0
    label[finite & (forward_return < -threshold)] = 0.0
    return label, forward_return


def build_next_bar_returns(ret_1: np.ndarray) -> np.ndarray:
    return build_execution_bar_returns(ret_1, execution_latency_bars=0)


def build_execution_bar_returns(
    ret_1: np.ndarray,
    execution_latency_bars: int,
) -> np.ndarray:
    result = np.full(len(ret_1), np.nan, dtype=np.float64)
    shift = max(0, int(execution_latency_bars)) + 1
    if len(ret_1) > shift:
        result[:-shift] = ret_1[shift:]
    return result


def build_label_policy_summary(
    label: np.ndarray,
    forward_return: np.ndarray,
    label_round_trip_cost_bps: float,
    label_min_net_edge_bps: float,
    valid_mask: np.ndarray,
) -> Dict[str, Any]:
    finite_forward = np.isfinite(forward_return)
    finite_label = np.isfinite(label)
    neutral_mask = finite_forward & ~finite_label
    valid_label = finite_label & valid_mask
    threshold_bps = max(0.0, float(label_round_trip_cost_bps)) + max(
        0.0, float(label_min_net_edge_bps)
    )
    return {
        "round_trip_cost_bps": float(label_round_trip_cost_bps),
        "min_net_edge_bps": float(label_min_net_edge_bps),
        "threshold_bps": threshold_bps,
        "finite_forward_return_count": int(np.sum(finite_forward)),
        "neutral_dropped_count": int(np.sum(neutral_mask)),
        "raw_positive_label_count": int(np.sum(finite_label & (label == 1.0))),
        "raw_negative_label_count": int(np.sum(finite_label & (label == 0.0))),
        "valid_positive_label_count": int(np.sum(valid_label & (label == 1.0))),
        "valid_negative_label_count": int(np.sum(valid_label & (label == 0.0))),
        "sample_count_after_filter": int(np.sum(valid_label)),
    }


def auc_score(y_true: np.ndarray, y_score: np.ndarray) -> float:
    mask = np.isfinite(y_true) & np.isfinite(y_score)
    y = y_true[mask].astype(np.int32)
    s = y_score[mask]
    pos = int(np.sum(y == 1))
    neg = int(np.sum(y == 0))
    if pos == 0 or neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, len(s) + 1, dtype=np.float64)
    pos_rank_sum = float(np.sum(ranks[y == 1]))
    return (pos_rank_sum - pos * (pos + 1) / 2.0) / float(pos * neg)


def logloss_score(y_true: np.ndarray, y_score: np.ndarray) -> float:
    mask = np.isfinite(y_true) & np.isfinite(y_score)
    if np.sum(mask) == 0:
        return float("nan")
    y = y_true[mask]
    p = np.clip(y_score[mask], 1e-6, 1.0 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def accuracy_score(y_true: np.ndarray, y_score: np.ndarray) -> float:
    mask = np.isfinite(y_true) & np.isfinite(y_score)
    if np.sum(mask) == 0:
        return float("nan")
    y = y_true[mask].astype(np.int32)
    pred = (y_score[mask] >= 0.5).astype(np.int32)
    return float(np.mean(pred == y))


def median_ignore_nan(values: Sequence[float]) -> float:
    finite = [v for v in values if math.isfinite(v)]
    return float(statistics.median(finite)) if finite else float("nan")


def summarize_model_net_objective(
    score: np.ndarray,
    next_bar_return: np.ndarray,
    round_trip_cost_bps: float,
    confidence_threshold: float = 0.5,
) -> Dict[str, Any]:
    if len(score) != len(next_bar_return):
        raise ValueError("score and next_bar_return must align")
    one_way_cost_bps = max(0.0, float(round_trip_cost_bps)) / 2.0
    position = 0.0
    gross_bps: List[float] = []
    net_bps: List[float] = []
    turnover_total = 0.0
    trade_count = 0
    active_positions: List[float] = []
    for raw_score, raw_return in zip(score, next_bar_return):
        if not (math.isfinite(float(raw_score)) and math.isfinite(float(raw_return))):
            continue
        directional_confidence = 2.0 * float(raw_score) - 1.0
        target = (
            math.copysign(1.0, directional_confidence)
            if abs(directional_confidence) >= max(0.0, float(confidence_threshold))
            else 0.0
        )
        turnover = abs(target - position)
        if turnover > 1e-12:
            trade_count += 1
        gross = target * float(raw_return) * 10000.0
        net = gross - turnover * one_way_cost_bps
        gross_bps.append(gross)
        net_bps.append(net)
        active_positions.append(target)
        turnover_total += turnover
        position = target
    if net_bps and abs(position) > 1e-12:
        net_bps[-1] -= abs(position) * one_way_cost_bps
        turnover_total += abs(position)
        trade_count += 1
        position = 0.0
    if not net_bps:
        return {
            "model_net_objective_sample_count": 0,
            "mean_model_gross_edge_bps": float("nan"),
            "mean_model_net_edge_bps": float("nan"),
            "median_model_net_edge_bps": float("nan"),
            "positive_model_net_edge_ratio": float("nan"),
            "long_signal_ratio": float("nan"),
            "short_signal_ratio": float("nan"),
            "round_trip_cost_bps": float(round_trip_cost_bps),
            "trade_count": 0,
            "turnover": 0.0,
            "active_bar_count": 0,
            "positive_net_bar_count": 0,
            "evaluated_bar_count": 0,
            "total_model_gross_edge_bps": 0.0,
            "total_model_net_edge_bps": 0.0,
            "net_bps_sum_squares": 0.0,
            "terminal_position_closed": True,
        }
    net_array = np.asarray(net_bps, dtype=np.float64)
    gross_array = np.asarray(gross_bps, dtype=np.float64)
    total_net_bps = float(np.sum(net_array))
    total_gross_bps = float(np.sum(gross_array))
    turnover_denominator = max(1e-12, turnover_total)
    positions = np.asarray(active_positions, dtype=np.float64)
    sample_count = len(net_bps)
    active_mask = np.abs(positions) > 1e-12
    active_bar_count = int(np.sum(active_mask))
    positive_net_bar_count = int(np.sum((net_array > 0.0) & active_mask))
    return {
        "model_net_objective_sample_count": sample_count,
        "mean_model_gross_edge_bps": total_gross_bps / turnover_denominator,
        "mean_model_net_edge_bps": total_net_bps / turnover_denominator,
        "total_model_gross_edge_bps": total_gross_bps,
        "total_model_net_edge_bps": total_net_bps,
        "median_model_net_edge_bps": float(np.median(net_array)),
        "positive_model_net_edge_ratio": (
            float(positive_net_bar_count) / float(active_bar_count)
            if active_bar_count > 0
            else float("nan")
        ),
        "long_signal_ratio": float(np.mean(positions > 0.0)),
        "short_signal_ratio": float(np.mean(positions < 0.0)),
        "round_trip_cost_bps": float(round_trip_cost_bps),
        "one_way_cost_bps": one_way_cost_bps,
        "trade_count": trade_count,
        "turnover": turnover_total,
        "active_bar_count": active_bar_count,
        "positive_net_bar_count": positive_net_bar_count,
        "evaluated_bar_count": sample_count,
        "net_bps_sum_squares": float(np.sum(net_array * net_array)),
        "terminal_position_closed": position == 0.0,
        "objective_definition": (
            "net_bps_per_unit_turnover_after_terminal_close"
        ),
    }


def apply_model_score_gain(score: np.ndarray, score_gain: float) -> np.ndarray:
    """Mirror the C++ runtime: sigmoid(raw CatBoost logit * score_gain)."""
    values = np.asarray(score, dtype=np.float64)
    clipped_probability = np.clip(values, 1e-12, 1.0 - 1e-12)
    raw_logit = np.log(clipped_probability / (1.0 - clipped_probability))
    amplified = np.clip(raw_logit * float(score_gain), -60.0, 60.0)
    return 1.0 / (1.0 + np.exp(-amplified))


def summarize_model_episode_objective(
    score: np.ndarray,
    execution_bar_return: np.ndarray,
    round_trip_cost_bps: float,
    confidence_threshold: float,
    holding_bars: int,
) -> Dict[str, Any]:
    """Evaluate non-overlapping entries held for the label horizon.

    Canary mode only opens from a flat account and exits through its protection
    lifecycle.  Treating every low-confidence bar as an immediate flat order
    over-counts turnover and does not match that runtime policy.  This prescreen
    therefore opens only on an eligible score, ignores overlapping entries, and
    realizes one round-trip cost when the fixed diagnostic horizon completes.
    """
    if len(score) != len(execution_bar_return):
        raise ValueError("score and execution_bar_return must align")
    horizon = max(1, int(holding_bars))
    cost_bps = max(0.0, float(round_trip_cost_bps))
    gross_values: List[float] = []
    net_values: List[float] = []
    directions: List[float] = []
    active_bar_count = 0
    index = 0
    while index + horizon <= len(score):
        raw_score = float(score[index])
        confidence = 2.0 * raw_score - 1.0
        if (
            not math.isfinite(raw_score)
            or abs(confidence) < max(0.0, float(confidence_threshold))
        ):
            index += 1
            continue
        path = np.asarray(
            execution_bar_return[index : index + horizon],
            dtype=np.float64,
        )
        if len(path) != horizon or not np.all(np.isfinite(path)):
            index += 1
            continue
        direction = math.copysign(1.0, confidence)
        gross_return = float(np.prod(1.0 + direction * path) - 1.0)
        gross_bps = gross_return * 10000.0
        gross_values.append(gross_bps)
        net_values.append(gross_bps - cost_bps)
        directions.append(direction)
        active_bar_count += horizon
        index += horizon

    episode_count = len(net_values)
    if episode_count == 0:
        return {
            "model_net_objective_sample_count": int(len(score)),
            "mean_model_gross_edge_bps": 0.0,
            "mean_model_net_edge_bps": 0.0,
            "total_model_gross_edge_bps": 0.0,
            "total_model_net_edge_bps": 0.0,
            "median_model_net_edge_bps": 0.0,
            "positive_model_net_edge_ratio": float("nan"),
            "long_signal_ratio": 0.0,
            "short_signal_ratio": 0.0,
            "round_trip_cost_bps": cost_bps,
            "trade_count": 0,
            "turnover": 0.0,
            "active_bar_count": 0,
            "positive_trade_count": 0,
            "positive_net_bar_count": 0,
            "evaluated_bar_count": int(len(score)),
            "net_bps_sum_squares": 0.0,
            "terminal_position_closed": True,
            "holding_bars": horizon,
            "objective_definition": "non_overlapping_fixed_horizon_episodes",
        }

    gross_array = np.asarray(gross_values, dtype=np.float64)
    net_array = np.asarray(net_values, dtype=np.float64)
    direction_array = np.asarray(directions, dtype=np.float64)
    turnover = float(episode_count * 2)
    positive_trade_count = int(np.sum(net_array > 0.0))
    return {
        "model_net_objective_sample_count": int(len(score)),
        "mean_model_gross_edge_bps": float(np.sum(gross_array)) / turnover,
        "mean_model_net_edge_bps": float(np.sum(net_array)) / turnover,
        "total_model_gross_edge_bps": float(np.sum(gross_array)),
        "total_model_net_edge_bps": float(np.sum(net_array)),
        "median_model_net_edge_bps": float(np.median(net_array)),
        "positive_model_net_edge_ratio": (
            float(positive_trade_count) / float(episode_count)
        ),
        "long_signal_ratio": float(np.mean(direction_array > 0.0)),
        "short_signal_ratio": float(np.mean(direction_array < 0.0)),
        "round_trip_cost_bps": cost_bps,
        "trade_count": episode_count,
        "turnover": turnover,
        "active_bar_count": active_bar_count,
        "positive_trade_count": positive_trade_count,
        "positive_net_bar_count": positive_trade_count,
        "evaluated_bar_count": int(len(score)),
        "net_bps_sum_squares": float(np.sum(net_array * net_array)),
        "terminal_position_closed": True,
        "holding_bars": horizon,
        "objective_definition": "non_overlapping_fixed_horizon_episodes",
    }


@dataclass
class SplitRange:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def build_splits(
    sample_count: int,
    method: str,
    n_splits: int,
    train_window: int,
    test_window: int,
    step_window: int,
    purge_bars: int = 0,
) -> List[SplitRange]:
    if n_splits <= 0:
        raise ValueError("n_splits 必须大于 0")
    if test_window <= 0:
        raise ValueError("test_window 必须大于 0")
    if sample_count <= test_window + 10:
        raise ValueError("可用样本不足，无法做时序切分")

    splits: List[SplitRange] = []
    method = method.lower()
    if method == "timeseriessplit":
        first_train_end = sample_count - n_splits * test_window
        if first_train_end <= 10:
            raise ValueError("样本不足以构建 TimeSeriesSplit")
        for idx in range(n_splits):
            test_start = first_train_end + idx * test_window
            train_start = 0
            train_end = test_start - max(0, int(purge_bars))
            test_end = test_start + test_window
            if test_end > sample_count:
                break
            splits.append(SplitRange(train_start, train_end, test_start, test_end))
        return splits

    if method != "rolling":
        raise ValueError(f"不支持的 split_method: {method}")

    if train_window <= 0:
        train_window = max(50, int(sample_count * 0.6))
    if step_window <= 0:
        step_window = test_window

    purge = max(0, int(purge_bars))
    max_splits = (
        sample_count - train_window - test_window - purge
    ) // step_window + 1
    if max_splits <= 0:
        raise ValueError("rolling 参数导致无法切分，请增大样本或减小窗口")
    use_splits = min(n_splits, max_splits)
    first_test_start = sample_count - (use_splits * step_window + test_window - step_window)
    first_test_start = max(first_test_start, train_window + purge)

    for idx in range(use_splits):
        test_start = first_test_start + idx * step_window
        test_end = test_start + test_window
        train_end = test_start - purge
        train_start = train_end - train_window
        if train_start < 0 or test_end > sample_count:
            continue
        splits.append(SplitRange(train_start, train_end, test_start, test_end))
    if not splits:
        raise ValueError("rolling 未生成有效切分")
    return splits


def purge_splits_by_raw_index(
    splits: Sequence[SplitRange],
    raw_indices: np.ndarray,
    label_lookahead_bars: int,
    rolling_train_window: int,
    method: str,
) -> List[SplitRange]:
    purged: List[SplitRange] = []
    lookahead = max(0, int(label_lookahead_bars))
    for split in splits:
        test_raw_start = int(raw_indices[split.test_start])
        # A label rooted at raw row i ends at i+lookahead and must end before test.
        raw_cutoff = test_raw_start - lookahead
        train_end = min(
            split.train_end,
            int(np.searchsorted(raw_indices, raw_cutoff, side="left")),
        )
        train_start = split.train_start
        if method.lower() == "rolling" and rolling_train_window > 0:
            train_start = train_end - int(rolling_train_window)
        if train_start < 0 or train_end - train_start <= 10:
            continue
        purged.append(
            SplitRange(
                train_start=train_start,
                train_end=train_end,
                test_start=split.test_start,
                test_end=split.test_end,
            )
        )
    return purged


def mean_ignore_nan(values: Sequence[float]) -> float:
    finite = [v for v in values if math.isfinite(v)]
    return float(statistics.mean(finite)) if finite else float("nan")


def stdev_ignore_nan(values: Sequence[float]) -> float:
    finite = [v for v in values if math.isfinite(v)]
    if len(finite) <= 1:
        return float("nan")
    return float(statistics.stdev(finite))


def student_t_975(df: int) -> float:
    # Two-sided 95% critical values. OOS split counts are normally small, so
    # using a normal approximation here would materially overstate confidence.
    critical = (
        12.706,
        4.303,
        3.182,
        2.776,
        2.571,
        2.447,
        2.365,
        2.306,
        2.262,
        2.228,
        2.201,
        2.179,
        2.160,
        2.145,
        2.131,
        2.120,
        2.110,
        2.101,
        2.093,
        2.086,
        2.080,
        2.074,
        2.069,
        2.064,
        2.060,
        2.056,
        2.052,
        2.048,
        2.045,
        2.042,
    )
    if df <= 0:
        return float("nan")
    if df <= len(critical):
        return critical[df - 1]
    return 1.96


def class_count(values: np.ndarray) -> Dict[int, int]:
    finite = values[np.isfinite(values)].astype(np.int32)
    result: Dict[int, int] = {}
    for cls in finite:
        key = int(cls)
        result[key] = result.get(key, 0) + 1
    return result


def split_temporal_train_validation(
    x: np.ndarray,
    y: np.ndarray,
    validation_fraction: float,
    min_validation_samples: int,
    purge_samples: int = 0,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None, Dict[str, int]]:
    sample_count = int(len(x))
    metadata = {
        "train_fit_count": sample_count,
        "validation_count": 0,
        "validation_start": sample_count,
        "purge_count": 0,
    }
    if sample_count <= 0 or validation_fraction <= 0.0 or min_validation_samples <= 0:
        return x, y, None, None, metadata

    candidate_validation = max(
        int(round(sample_count * validation_fraction)),
        int(min_validation_samples),
    )
    max_validation = sample_count - max(int(min_validation_samples), 2)
    if max_validation <= 0:
        return x, y, None, None, metadata
    candidate_validation = min(candidate_validation, max_validation)

    for validation_size in range(candidate_validation, int(min_validation_samples) - 1, -1):
        validation_start = sample_count - validation_size
        fit_size = validation_start - max(0, int(purge_samples))
        if fit_size < max(int(min_validation_samples), 2):
            continue
        x_fit = x[:fit_size]
        y_fit = y[:fit_size]
        x_val = x[validation_start:]
        y_val = y[validation_start:]
        if len(class_count(y_fit).keys()) < 2:
            continue
        if len(class_count(y_val).keys()) < 2:
            continue
        metadata["train_fit_count"] = int(len(x_fit))
        metadata["validation_count"] = int(len(x_val))
        metadata["validation_start"] = int(validation_start)
        metadata["purge_count"] = int(validation_start - fit_size)
        return x_fit, y_fit, x_val, y_val, metadata

    return x, y, None, None, metadata


def split_raw_temporal_train_validation(
    raw_features: np.ndarray,
    label: np.ndarray,
    raw_start: int,
    raw_end: int,
    validation_fraction: float,
    min_validation_samples: int,
    purge_bars: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None, Dict[str, int]]:
    raw_start = max(0, int(raw_start))
    raw_end = min(len(raw_features), int(raw_end))
    valid = np.isfinite(label) & np.all(np.isfinite(raw_features), axis=1)
    full_indices = np.flatnonzero(valid & (np.arange(len(valid)) >= raw_start) & (
        np.arange(len(valid)) < raw_end
    ))
    metadata = {
        "raw_train_start": raw_start,
        "raw_train_end_exclusive": raw_end,
        "train_fit_count": int(len(full_indices)),
        "validation_count": 0,
        "validation_start_raw": raw_end,
        "purge_count_raw": 0,
    }
    x_full = raw_features[full_indices]
    y_full = label[full_indices]
    raw_count = raw_end - raw_start
    if (
        raw_count <= 0
        or validation_fraction <= 0.0
        or min_validation_samples <= 0
    ):
        return x_full, y_full, None, None, metadata

    candidate_validation_raw = max(
        int(round(raw_count * validation_fraction)),
        int(min_validation_samples),
    )
    max_validation_raw = raw_count - max(int(min_validation_samples), 2)
    candidate_validation_raw = min(candidate_validation_raw, max_validation_raw)
    for validation_raw_size in range(
        candidate_validation_raw,
        int(min_validation_samples) - 1,
        -1,
    ):
        validation_start_raw = raw_end - validation_raw_size
        fit_end_raw = validation_start_raw - max(0, int(purge_bars))
        if fit_end_raw <= raw_start:
            continue
        fit_indices = np.flatnonzero(
            valid
            & (np.arange(len(valid)) >= raw_start)
            & (np.arange(len(valid)) < fit_end_raw)
        )
        validation_indices = np.flatnonzero(
            valid
            & (np.arange(len(valid)) >= validation_start_raw)
            & (np.arange(len(valid)) < raw_end)
        )
        if (
            len(fit_indices) < max(int(min_validation_samples), 2)
            or len(validation_indices) < int(min_validation_samples)
        ):
            continue
        y_fit = label[fit_indices]
        y_val = label[validation_indices]
        if len(class_count(y_fit)) < 2 or len(class_count(y_val)) < 2:
            continue
        metadata.update(
            {
                "train_fit_count": int(len(fit_indices)),
                "validation_count": int(len(validation_indices)),
                "validation_start_raw": int(validation_start_raw),
                "purge_count_raw": int(max(0, purge_bars)),
            }
        )
        return (
            raw_features[fit_indices],
            y_fit,
            raw_features[validation_indices],
            y_val,
            metadata,
        )
    return x_full, y_full, None, None, metadata


def build_catboost_classifier(
    *,
    random_seed: int,
    iterations: int,
    depth: int,
    learning_rate: float,
    l2_leaf_reg: float,
    random_strength: float,
    subsample: float,
    rsm: float,
) -> CatBoostClassifier:
    return CatBoostClassifier(
        loss_function="Logloss",
        eval_metric="AUC",
        random_seed=random_seed,
        iterations=iterations,
        depth=depth,
        learning_rate=learning_rate,
        l2_leaf_reg=l2_leaf_reg,
        random_strength=random_strength,
        bootstrap_type="Bernoulli",
        subsample=subsample,
        rsm=rsm,
        verbose=False,
        allow_writing_files=False,
    )


def run_random_label_control_trials(
    *,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    y_test: np.ndarray,
    random_seed: int,
    iterations: int,
    depth: int,
    learning_rate: float,
    l2_leaf_reg: float,
    random_strength: float,
    subsample: float,
    rsm: float,
    trials: int,
) -> List[float]:
    auc_values: List[float] = []
    if trials <= 0:
        return auc_values
    for trial_idx in range(trials):
        shuffled_y = y_train.copy()
        rng = np.random.default_rng(int(random_seed) + 20260222 + trial_idx)
        rng.shuffle(shuffled_y)
        control_model = build_catboost_classifier(
            random_seed=int(random_seed) + 17 + trial_idx,
            iterations=iterations,
            depth=max(2, depth // 2),
            learning_rate=learning_rate,
            l2_leaf_reg=max(3.0, l2_leaf_reg),
            random_strength=max(1.0, random_strength),
            subsample=subsample,
            rsm=rsm,
        )
        control_model.fit(x_train, shuffled_y)
        control_score = control_model.predict_proba(x_test)[:, 1]
        auc_values.append(auc_score(y_test, control_score))
    return auc_values


def evaluate_governance(
    metrics_oos: Dict[str, float],
    min_auc_mean: float,
    min_delta_auc_vs_baseline: float,
    min_split_trained_count: int,
    min_split_trained_ratio: float,
    max_auc_stdev: float,
    max_train_test_auc_gap: float,
    run_random_label_control: bool,
    max_random_label_auc: float,
    min_mean_model_net_edge_bps: float,
    min_positive_model_net_edge_ratio: float,
    min_model_net_total_trades: int = 0,
    min_model_net_active_bars: int = 0,
    min_positive_model_net_splits_ratio: float = 0.0,
    min_model_net_edge_lcb_bps: float = float("-inf"),
) -> Tuple[bool, List[str], List[str]]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    mean_model_net_edge_bps = metrics_oos.get("mean_model_net_edge_bps", float("nan"))
    positive_model_net_edge_ratio = metrics_oos.get(
        "positive_model_net_edge_ratio", float("nan")
    )
    auc_mean = metrics_oos.get("auc_mean", float("nan"))
    delta_auc = metrics_oos.get("delta_auc_vs_baseline", float("nan"))
    split_trained_count = metrics_oos.get("split_trained_count", float("nan"))
    split_trained_ratio = metrics_oos.get("split_trained_ratio", float("nan"))
    split_trained_count_int = (
        int(round(split_trained_count)) if math.isfinite(split_trained_count) else 0
    )
    auc_stdev = metrics_oos.get("auc_stdev", float("nan"))
    train_test_auc_gap_mean = metrics_oos.get("train_test_auc_gap_mean", float("nan"))
    random_label_auc = metrics_oos.get("random_label_auc", float("nan"))
    random_label_auc_mean = metrics_oos.get("random_label_auc_mean", random_label_auc)
    random_label_auc_max = metrics_oos.get("random_label_auc_max", random_label_auc)
    total_trades = metrics_oos.get("model_net_total_trades", float("nan"))
    active_bars = metrics_oos.get("model_net_active_bar_count", float("nan"))
    positive_splits_ratio = metrics_oos.get(
        "positive_model_net_edge_ratio_by_split", float("nan")
    )
    net_edge_lcb = metrics_oos.get("model_net_edge_lcb_bps", float("nan"))

    if not math.isfinite(mean_model_net_edge_bps):
        fail_reasons.append("缺少或无效 metrics_oos.mean_model_net_edge_bps")
    elif mean_model_net_edge_bps < min_mean_model_net_edge_bps:
        fail_reasons.append(
            "mean_model_net_edge_bps="
            f"{mean_model_net_edge_bps:.6f} < "
            f"min_mean_model_net_edge_bps={min_mean_model_net_edge_bps:.6f}"
        )

    if not math.isfinite(positive_model_net_edge_ratio):
        fail_reasons.append("缺少或无效 metrics_oos.positive_model_net_edge_ratio")
    elif positive_model_net_edge_ratio < min_positive_model_net_edge_ratio:
        fail_reasons.append(
            "positive_model_net_edge_ratio="
            f"{positive_model_net_edge_ratio:.6f} < "
            f"min_positive_model_net_edge_ratio={min_positive_model_net_edge_ratio:.6f}"
        )

    if not math.isfinite(total_trades) or int(total_trades) < min_model_net_total_trades:
        fail_reasons.append(
            f"model_net_total_trades={int(total_trades) if math.isfinite(total_trades) else 0} "
            f"< min_model_net_total_trades={min_model_net_total_trades}"
        )
    if not math.isfinite(active_bars) or int(active_bars) < min_model_net_active_bars:
        fail_reasons.append(
            f"model_net_active_bar_count={int(active_bars) if math.isfinite(active_bars) else 0} "
            f"< min_model_net_active_bars={min_model_net_active_bars}"
        )
    if (
        not math.isfinite(positive_splits_ratio)
        or positive_splits_ratio < min_positive_model_net_splits_ratio
    ):
        fail_reasons.append(
            "positive_model_net_edge_ratio_by_split="
            f"{positive_splits_ratio:.6f} < "
            f"min_positive_model_net_splits_ratio={min_positive_model_net_splits_ratio:.6f}"
        )
    if not math.isfinite(net_edge_lcb) or net_edge_lcb < min_model_net_edge_lcb_bps:
        fail_reasons.append(
            f"model_net_edge_lcb_bps={net_edge_lcb:.6f} < "
            f"min_model_net_edge_lcb_bps={min_model_net_edge_lcb_bps:.6f}"
        )

    if not math.isfinite(auc_mean):
        warn_reasons.append("缺少或无效 metrics_oos.auc_mean")
    elif auc_mean < min_auc_mean:
        warn_reasons.append(
            f"auc_mean={auc_mean:.6f} < min_auc_mean={min_auc_mean:.6f}"
        )

    if not math.isfinite(delta_auc):
        warn_reasons.append("缺少或无效 metrics_oos.delta_auc_vs_baseline")
    elif delta_auc < min_delta_auc_vs_baseline:
        warn_reasons.append(
            "delta_auc_vs_baseline="
            f"{delta_auc:.6f} < min_delta_auc_vs_baseline={min_delta_auc_vs_baseline:.6f}"
        )

    if not math.isfinite(split_trained_count):
        fail_reasons.append("缺少或无效 metrics_oos.split_trained_count")
    elif int(round(split_trained_count)) < min_split_trained_count:
        fail_reasons.append(
            "split_trained_count="
            f"{int(round(split_trained_count))} < min_split_trained_count={min_split_trained_count}"
        )

    if not math.isfinite(split_trained_ratio):
        fail_reasons.append("缺少或无效 metrics_oos.split_trained_ratio")
    elif split_trained_ratio < min_split_trained_ratio:
        fail_reasons.append(
            "split_trained_ratio="
            f"{split_trained_ratio:.6f} < min_split_trained_ratio={min_split_trained_ratio:.6f}"
        )

    if split_trained_count_int >= 2 and not math.isfinite(auc_stdev):
        fail_reasons.append("缺少或无效 metrics_oos.auc_stdev")
    elif math.isfinite(auc_stdev) and auc_stdev > max_auc_stdev:
        fail_reasons.append(f"auc_stdev={auc_stdev:.6f} > max_auc_stdev={max_auc_stdev:.6f}")

    if split_trained_count_int >= 1 and not math.isfinite(train_test_auc_gap_mean):
        fail_reasons.append("缺少或无效 metrics_oos.train_test_auc_gap_mean")
    elif math.isfinite(train_test_auc_gap_mean) and train_test_auc_gap_mean > max_train_test_auc_gap:
        fail_reasons.append(
            "train_test_auc_gap_mean="
            f"{train_test_auc_gap_mean:.6f} > max_train_test_auc_gap={max_train_test_auc_gap:.6f}"
        )

    if run_random_label_control:
        if not math.isfinite(random_label_auc_mean):
            fail_reasons.append("缺少或无效 metrics_oos.random_label_auc_mean")
        elif random_label_auc_mean > max_random_label_auc:
            fail_reasons.append(
                "random_label_auc_mean="
                f"{random_label_auc_mean:.6f} > max_random_label_auc={max_random_label_auc:.6f}"
            )
        elif (
            math.isfinite(random_label_auc_max)
            and random_label_auc_max > max_random_label_auc + 0.03
        ):
            warn_reasons.append(
                "random_label_auc_max="
                f"{random_label_auc_max:.6f} > soft_cap={max_random_label_auc + 0.03:.6f}"
            )

    return len(fail_reasons) == 0, fail_reasons, warn_reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="CatBoost Integrator 离线训练（R2）")
    parser.add_argument("--csv", required=True, help="研究数据 CSV 路径（OHLCV）")
    parser.add_argument(
        "--training_symbol",
        default="BTCUSDT",
        help="该模型唯一允许消费的交易币对",
    )
    parser.add_argument(
        "--bar_interval_ms",
        type=int,
        default=300000,
        help="训练 OHLCV bar 周期（毫秒），必须与线上完整 K 线一致",
    )
    parser.add_argument("--source_venue", required=True)
    parser.add_argument("--source_category", required=True)
    parser.add_argument("--price_type", required=True)
    parser.add_argument("--volume_unit", required=True)
    parser.add_argument("--miner_report", required=True, help="miner_report.json 路径")
    parser.add_argument("--output", required=True, help="Integrator 训练报告输出路径（JSON）")
    parser.add_argument("--model_out", required=True, help="CatBoost 模型输出路径（.cbm）")
    parser.add_argument("--top_k", type=int, default=10, help="使用的 Miner 因子数量")
    parser.add_argument("--predict_horizon_bars", type=int, default=1, help="标签预测步长 h")
    parser.add_argument(
        "--split_method",
        choices=("rolling", "timeseriessplit"),
        default="rolling",
        help="时序切分方式",
    )
    parser.add_argument("--n_splits", type=int, default=5, help="切分数")
    parser.add_argument("--train_window_bars", type=int, default=0, help="rolling 训练窗口")
    parser.add_argument("--test_window_bars", type=int, default=120, help="测试窗口")
    parser.add_argument("--rolling_step_bars", type=int, default=120, help="rolling 步长")
    parser.add_argument("--iterations", type=int, default=180, help="CatBoost 迭代次数")
    parser.add_argument("--depth", type=int, default=4, help="CatBoost 树深")
    parser.add_argument("--learning_rate", type=float, default=0.03, help="CatBoost 学习率")
    parser.add_argument("--l2_leaf_reg", type=float, default=30.0, help="CatBoost L2 正则")
    parser.add_argument(
        "--random_strength",
        type=float,
        default=2.0,
        help="CatBoost 随机强度（增大可抑制过拟合）",
    )
    parser.add_argument(
        "--subsample",
        type=float,
        default=0.80,
        help="CatBoost 行采样比例 (0,1]",
    )
    parser.add_argument(
        "--rsm",
        type=float,
        default=0.80,
        help="CatBoost 列采样比例 (0,1]",
    )
    parser.add_argument(
        "--validation_fraction",
        type=float,
        default=0.15,
        help="训练窗口内尾部验证集比例（0=关闭，启用早停）",
    )
    parser.add_argument(
        "--min_validation_samples",
        type=int,
        default=60,
        help="训练窗口内验证集最小样本数",
    )
    parser.add_argument(
        "--early_stopping_rounds",
        type=int,
        default=30,
        help="训练窗口内验证集早停轮数（需 validation_fraction > 0）",
    )
    parser.add_argument("--random_seed", type=int, default=42, help="随机种子")
    parser.add_argument("--min_auc_mean", type=float, default=0.50, help="治理门槛：最小 AUC 均值")
    parser.add_argument(
        "--min_delta_auc_vs_baseline",
        type=float,
        default=0.0,
        help="治理门槛：最小 Delta AUC（相对 baseline）",
    )
    parser.add_argument(
        "--min_split_trained_count",
        type=int,
        default=1,
        help="治理门槛：最小成功训练 split 数",
    )
    parser.add_argument(
        "--min_split_trained_ratio",
        type=float,
        default=0.5,
        help="治理门槛：最小成功训练 split 比例",
    )
    parser.add_argument(
        "--max_auc_stdev",
        type=float,
        default=0.08,
        help="治理门槛：AUC 标准差上限（过滤不稳定模型）",
    )
    parser.add_argument(
        "--max_train_test_auc_gap",
        type=float,
        default=0.10,
        help="治理门槛：train/test AUC gap 上限（抑制过拟合）",
    )
    parser.add_argument(
        "--disable_random_label_control",
        action="store_true",
        help="关闭随机标签对照测试（默认启用）",
    )
    parser.add_argument(
        "--max_random_label_auc",
        type=float,
        default=0.55,
        help="治理门槛：随机标签对照 AUC 上限",
    )
    parser.add_argument(
        "--random_label_iterations",
        type=int,
        default=80,
        help="随机标签对照模型迭代次数（用于控制开销）",
    )
    parser.add_argument(
        "--random_label_trials",
        type=int,
        default=5,
        help="随机标签对照重复次数（统计均值/波动，降低单次噪声）",
    )
    parser.add_argument(
        "--execution_latency_bars",
        type=int,
        default=1,
        help="feature bar 收盘到可执行持仓之间的延迟 bar 数",
    )
    parser.add_argument(
        "--model_confidence_threshold",
        type=float,
        default=0.5,
        help=(
            "经济预筛与运行时一致的方向置信阈值，定义为 abs(2*p_up-1)；"
            "0.1 对应 p_up >= 0.55 或 <= 0.45"
        ),
    )
    parser.add_argument(
        "--model_score_gain",
        type=float,
        default=1.0,
        help="与 integrator.shadow.score_gain 一致的 CatBoost raw-logit 放大倍数",
    )
    parser.add_argument(
        "--label_round_trip_cost_bps",
        type=float,
        default=0.0,
        help="训练标签成本带：round-trip 成本估计 bps；>0 时成本带内样本丢弃",
    )
    parser.add_argument(
        "--label_min_net_edge_bps",
        type=float,
        default=0.0,
        help="训练标签额外净边际 bps；与 round-trip cost 叠加为标签阈值",
    )
    parser.add_argument(
        "--min_mean_model_net_edge_bps",
        type=float,
        default=0.0,
        help="治理主目标：模型方向在 OOS 上扣除 round-trip cost 后的最小平均净 edge bps",
    )
    parser.add_argument(
        "--min_positive_model_net_edge_ratio",
        type=float,
        default=0.50,
        help="治理主目标：模型 OOS 净 edge 为正的最小样本比例",
    )
    parser.add_argument(
        "--min_model_net_total_trades",
        type=int,
        default=20,
        help="治理主目标：OOS 模型经济预筛最小换仓事件数",
    )
    parser.add_argument(
        "--min_model_net_active_bars",
        type=int,
        default=100,
        help="治理主目标：OOS 模型持仓活跃 bar 下限",
    )
    parser.add_argument(
        "--min_positive_model_net_splits_ratio",
        type=float,
        default=0.50,
        help="治理主目标：成本后为正的 OOS split 比例下限",
    )
    parser.add_argument(
        "--min_model_net_edge_lcb_bps",
        type=float,
        default=0.0,
        help="治理主目标：OOS 每 bar 净收益 95%% 正态下置信界下限",
    )
    parser.add_argument(
        "--feature_clip_quantile",
        type=float,
        default=0.0,
        help="特征稳健裁剪分位数；0 表示关闭，0.001 表示按 0.1%%/99.9%% 裁剪",
    )
    parser.add_argument(
        "--fail_on_governance",
        action="store_true",
        help="兼容旧调用；治理失败现在始终返回非零并禁止发布模型",
    )
    parser.add_argument(
        "--min_samples",
        type=int,
        default=120,
        help="最小有效样本数（小样本烟囱测试可调低）",
    )
    args = parser.parse_args()
    args.training_symbol = str(args.training_symbol).strip().upper()
    if not args.training_symbol:
        parser.error("--training_symbol 不能为空")
    if args.bar_interval_ms <= 0:
        parser.error("--bar_interval_ms 必须 > 0")
    if (
        args.source_venue != "bybit"
        or args.source_category != "linear"
        or args.price_type != "trade_price"
        or args.volume_unit != "base_asset"
    ):
        parser.error(
            "training data contract must be "
            "bybit/linear/trade_price/base_asset"
        )

    if np is None:
        raise SystemExit(
            "[ERROR] 未安装 numpy。请先安装研究依赖：\n"
            "  pip install -r tools/requirements-research.txt\n"
            "或使用 docker compose 的 ai-trade-research 服务运行。"
        )

    if CatBoostClassifier is None:
        raise SystemExit(
            "[ERROR] 未安装 catboost。请先安装研究依赖：\n"
            "  pip install -r tools/requirements-research.txt\n"
            "或使用 docker compose 的 ai-trade-research 服务运行。"
        )

    csv_path = pathlib.Path(args.csv)
    miner_report_path = pathlib.Path(args.miner_report)
    output_path = pathlib.Path(args.output)
    model_out_path = pathlib.Path(args.model_out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    model_out_path.parent.mkdir(parents=True, exist_ok=True)
    if model_out_path.exists():
        raise FileExistsError(
            f"拒绝覆盖已有模型产物，避免治理失败复用旧文件: {model_out_path}"
        )
    if not (0.0 <= float(args.min_auc_mean) <= 1.0):
        raise ValueError("--min_auc_mean 必须在 [0,1] 范围")
    if not (0.0 <= float(args.min_split_trained_ratio) <= 1.0):
        raise ValueError("--min_split_trained_ratio 必须在 [0,1] 范围")
    if int(args.min_split_trained_count) <= 0:
        raise ValueError("--min_split_trained_count 必须大于 0")
    if float(args.max_auc_stdev) < 0.0:
        raise ValueError("--max_auc_stdev 不能为负数")
    if float(args.max_train_test_auc_gap) < 0.0:
        raise ValueError("--max_train_test_auc_gap 不能为负数")
    if not (0.0 <= float(args.max_random_label_auc) <= 1.0):
        raise ValueError("--max_random_label_auc 必须在 [0,1] 范围")
    if int(args.random_label_iterations) <= 0:
        raise ValueError("--random_label_iterations 必须大于 0")
    if int(args.random_label_trials) <= 0:
        raise ValueError("--random_label_trials 必须大于 0")
    if float(args.label_round_trip_cost_bps) < 0.0:
        raise ValueError("--label_round_trip_cost_bps 不能为负数")
    if float(args.label_min_net_edge_bps) < 0.0:
        raise ValueError("--label_min_net_edge_bps 不能为负数")
    if not (0.0 <= float(args.min_positive_model_net_edge_ratio) <= 1.0):
        raise ValueError("--min_positive_model_net_edge_ratio 必须在 [0,1] 范围")
    if not (0.0 <= float(args.feature_clip_quantile) < 0.5):
        raise ValueError("--feature_clip_quantile 必须在 [0,0.5) 范围")
    if float(args.l2_leaf_reg) < 0.0:
        raise ValueError("--l2_leaf_reg 不能为负数")
    if float(args.random_strength) < 0.0:
        raise ValueError("--random_strength 不能为负数")
    if not (0.0 < float(args.subsample) <= 1.0):
        raise ValueError("--subsample 必须在 (0,1] 范围")
    if not (0.0 < float(args.rsm) <= 1.0):
        raise ValueError("--rsm 必须在 (0,1] 范围")
    if not (0.0 <= float(args.validation_fraction) < 1.0):
        raise ValueError("--validation_fraction 必须在 [0,1) 范围")
    if int(args.min_validation_samples) < 0:
        raise ValueError("--min_validation_samples 不能为负数")
    if int(args.early_stopping_rounds) <= 0:
        raise ValueError("--early_stopping_rounds 必须大于 0")
    if int(args.execution_latency_bars) < 1:
        raise ValueError("--execution_latency_bars 必须大于等于 1")
    if not (0.0 <= float(args.model_confidence_threshold) <= 1.0):
        raise ValueError("--model_confidence_threshold 必须在 [0,1] 范围")
    if float(args.model_score_gain) <= 0.0:
        raise ValueError("--model_score_gain 必须大于 0")
    if (
        args.split_method == "rolling"
        and int(args.rolling_step_bars) < int(args.test_window_bars)
    ):
        raise ValueError(
            "--rolling_step_bars 必须 >= --test_window_bars，禁止重叠 OOS 窗口"
        )
    if int(args.min_model_net_total_trades) <= 0:
        raise ValueError("--min_model_net_total_trades 必须大于 0")
    if int(args.min_model_net_active_bars) <= 0:
        raise ValueError("--min_model_net_active_bars 必须大于 0")
    if not (0.0 <= float(args.min_positive_model_net_splits_ratio) <= 1.0):
        raise ValueError("--min_positive_model_net_splits_ratio 必须在 [0,1] 范围")

    series = load_ohlcv_csv(csv_path)
    time_axis_quality = validate_time_axis(
        series["timestamp"],
        int(args.bar_interval_ms),
    )
    factor_set_version, factor_specs = load_factor_specs(
        miner_report_path,
        max(1, args.top_k),
        expected_horizon_bars=int(args.predict_horizon_bars),
        expected_execution_latency_bars=int(args.execution_latency_bars),
    )
    log_info(f"INTEGRATOR_START: bars={len(series['close'])}, factors={len(factor_specs)}")

    raw_features, feature_names, ret_1 = build_feature_matrix(series, factor_specs)
    label, forward_return = build_label(
        series["close"],
        args.predict_horizon_bars,
        label_round_trip_cost_bps=float(args.label_round_trip_cost_bps),
        label_min_net_edge_bps=float(args.label_min_net_edge_bps),
        execution_latency_bars=int(args.execution_latency_bars),
    )
    valid_mask = np.isfinite(label) & np.all(np.isfinite(raw_features), axis=1)
    label_policy = build_label_policy_summary(
        label=label,
        forward_return=forward_return,
        label_round_trip_cost_bps=float(args.label_round_trip_cost_bps),
        label_min_net_edge_bps=float(args.label_min_net_edge_bps),
        valid_mask=valid_mask,
    )
    log_info(
        "INTEGRATOR_LABEL_POLICY: "
        f"horizon_bars={int(args.predict_horizon_bars)}, "
        f"threshold_bps={label_policy['threshold_bps']:.6f}, "
        f"neutral_dropped={label_policy['neutral_dropped_count']}, "
        f"valid_pos={label_policy['valid_positive_label_count']}, "
        f"valid_neg={label_policy['valid_negative_label_count']}"
    )

    X_raw = raw_features[valid_mask]
    y = label[valid_mask]
    execution_bar_return = build_execution_bar_returns(
        ret_1,
        execution_latency_bars=int(args.execution_latency_bars),
    )
    label_lookahead_bars = (
        int(args.predict_horizon_bars) + int(args.execution_latency_bars)
    )

    if args.split_method == "rolling" and args.train_window_bars > 0:
        required = (
            args.train_window_bars
            + args.test_window_bars
            + max(0, args.n_splits - 1) * max(1, args.rolling_step_bars)
        )
        required += label_lookahead_bars
        if len(raw_features) < required:
            raise ValueError(
                "rolling 参数与样本规模不匹配："
                f"至少需要 {required} 条原始 bar，当前仅 {len(raw_features)}。"
                "可增大历史数据，或降低 train/test/step/n_splits。"
            )

    if len(X_raw) < args.min_samples:
        raise ValueError(
            f"有效样本过少: {len(X_raw)}，小于 --min_samples={args.min_samples}。"
            "若仅做功能验证可下调 --min_samples；正式验收建议使用更长历史样本。"
        )

    splits = build_splits(
        sample_count=len(raw_features),
        method=args.split_method,
        n_splits=args.n_splits,
        train_window=args.train_window_bars,
        test_window=args.test_window_bars,
        step_window=args.rolling_step_bars,
        purge_bars=label_lookahead_bars,
    )
    if not splits:
        raise ValueError("按原始 bar 时间轴执行 label purge 后没有可用时序切分")

    split_reports: List[dict] = []
    auc_values: List[float] = []
    train_auc_values: List[float] = []
    validation_auc_values: List[float] = []
    train_test_auc_gap_values: List[float] = []
    logloss_values: List[float] = []
    acc_values: List[float] = []
    baseline_auc_values: List[float] = []
    best_iteration_values: List[float] = []
    model_net_edge_split_values: List[float] = []
    model_net_total_gross_bps = 0.0
    model_net_total_net_bps = 0.0
    model_net_total_turnover = 0.0
    model_net_total_trades = 0
    model_net_active_bar_count = 0
    model_net_positive_trade_count = 0
    model_net_evaluated_bar_count = 0
    model_net_bps_sum_squares = 0.0
    model_net_economic_split_count = 0
    model_net_positive_split_count = 0
    trained_split_count = 0
    first_trained_split: Dict[str, np.ndarray] | None = None
    raw_indices = np.arange(len(raw_features))
    finite_feature_mask = np.all(np.isfinite(raw_features), axis=1)
    previous_test_end = -1

    for split_id, split in enumerate(splits, start=1):
        if split.test_start < previous_test_end:
            raise ValueError(
                "检测到重叠 OOS 窗口，拒绝重复计算同一市场证据: "
                f"previous_test_end={previous_test_end}, test_start={split.test_start}"
            )
        previous_test_end = split.test_end
        train_mask = (
            valid_mask
            & (raw_indices >= split.train_start)
            & (raw_indices < split.train_end)
        )
        test_mask = (
            valid_mask
            & (raw_indices >= split.test_start)
            & (raw_indices < split.test_end)
        )
        economic_test_mask = (
            finite_feature_mask
            & np.isfinite(execution_bar_return)
            & (raw_indices >= split.test_start)
            & (raw_indices < split.test_end)
        )
        X_train_raw = raw_features[train_mask]
        y_train = label[train_mask]
        X_test_raw = raw_features[test_mask]
        y_test = label[test_mask]
        test_ret1 = ret_1[test_mask]
        train_counts = class_count(y_train)
        test_counts = class_count(y_test)
        train_range = {
            "start": int(split.train_start),
            "end_exclusive": int(split.train_end),
            "ts_start": to_iso_utc(int(series["timestamp"][split.train_start])),
            "ts_end": to_iso_utc(int(series["timestamp"][split.train_end - 1])),
        }
        test_range = {
            "start": int(split.test_start),
            "end_exclusive": int(split.test_end),
            "ts_start": to_iso_utc(int(series["timestamp"][split.test_start])),
            "ts_end": to_iso_utc(int(series["timestamp"][split.test_end - 1])),
        }

        if len(train_counts.keys()) < 2:
            log_info(
                "INTEGRATOR_SPLIT_SKIPPED: "
                f"id={split_id}, reason=train_single_class, train_class_counts={train_counts}"
            )
            split_reports.append(
                {
                    "split_id": split_id,
                    "status": "skipped",
                    "skip_reason": "train_single_class",
                    "train_class_counts": train_counts,
                    "test_class_counts": test_counts,
                    "train_range": train_range,
                    "test_range": test_range,
                }
            )
            continue

        (
            X_fit_raw,
            y_fit,
            X_val_raw,
            y_val,
            fit_meta,
        ) = split_raw_temporal_train_validation(
            raw_features,
            label,
            raw_start=split.train_start,
            raw_end=split.train_end,
            validation_fraction=float(args.validation_fraction),
            min_validation_samples=int(args.min_validation_samples),
            purge_bars=label_lookahead_bars,
        )
        X_fit, split_feature_transform = build_feature_transform(
            X_fit_raw,
            feature_names,
            feature_clip_quantile=float(args.feature_clip_quantile),
        )
        X_val = (
            apply_feature_transform(
                X_val_raw,
                feature_names,
                split_feature_transform,
            )
            if X_val_raw is not None
            else None
        )
        X_test = apply_feature_transform(
            X_test_raw,
            feature_names,
            split_feature_transform,
        )
        X_economic_test = apply_feature_transform(
            raw_features[economic_test_mask],
            feature_names,
            split_feature_transform,
        )
        test_execution_return = execution_bar_return[economic_test_mask]
        model = build_catboost_classifier(
            random_seed=int(args.random_seed),
            iterations=int(args.iterations),
            depth=int(args.depth),
            learning_rate=float(args.learning_rate),
            l2_leaf_reg=float(args.l2_leaf_reg),
            random_strength=float(args.random_strength),
            subsample=float(args.subsample),
            rsm=float(args.rsm),
        )
        fit_kwargs = {}
        validation_auc = float("nan")
        if X_val is not None and y_val is not None:
            fit_kwargs = {
                "eval_set": (X_val, y_val),
                "use_best_model": True,
                "early_stopping_rounds": int(args.early_stopping_rounds),
            }
        model.fit(X_fit, y_fit, **fit_kwargs)
        train_score = model.predict_proba(X_fit)[:, 1]
        score = (
            model.predict_proba(X_test)[:, 1]
            if len(X_test) > 0
            else np.asarray([], dtype=np.float64)
        )
        if X_val is not None and y_val is not None:
            validation_score = model.predict_proba(X_val)[:, 1]
            validation_auc = auc_score(y_val, validation_score)
        baseline_score = np.where(np.isfinite(test_ret1), np.where(test_ret1 > 0.0, 0.9, 0.1), 0.5)
        economic_score = (
            model.predict_proba(X_economic_test)[:, 1]
            if len(X_economic_test) > 0
            else np.asarray([], dtype=np.float64)
        )
        economic_policy_score = apply_model_score_gain(
            economic_score,
            float(args.model_score_gain),
        )
        net_objective = summarize_model_episode_objective(
            score=economic_policy_score,
            execution_bar_return=test_execution_return,
            round_trip_cost_bps=float(args.label_round_trip_cost_bps),
            confidence_threshold=float(args.model_confidence_threshold),
            holding_bars=int(args.predict_horizon_bars),
        )
        split_mean_net = float(net_objective.get("mean_model_net_edge_bps", float("nan")))
        if math.isfinite(split_mean_net):
            model_net_edge_split_values.append(split_mean_net)
            model_net_economic_split_count += 1
            if split_mean_net > 0.0:
                model_net_positive_split_count += 1
        model_net_total_gross_bps += float(
            net_objective.get("total_model_gross_edge_bps", 0.0)
        )
        model_net_total_net_bps += float(
            net_objective.get("total_model_net_edge_bps", 0.0)
        )
        model_net_total_turnover += float(net_objective.get("turnover", 0.0))
        model_net_total_trades += int(net_objective.get("trade_count", 0))
        model_net_active_bar_count += int(
            net_objective.get("active_bar_count", 0)
        )
        model_net_positive_trade_count += int(
            net_objective.get("positive_trade_count", 0)
        )
        model_net_evaluated_bar_count += int(
            net_objective.get("evaluated_bar_count", 0)
        )
        model_net_bps_sum_squares += float(
            net_objective.get("net_bps_sum_squares", 0.0)
        )

        train_auc = auc_score(y_fit, train_score)
        auc = auc_score(y_test, score)
        validation_auc_values.append(validation_auc)
        ll = logloss_score(y_test, score)
        acc = accuracy_score(y_test, score)
        base_auc = auc_score(y_test, baseline_score)
        best_iteration = model.get_best_iteration()
        if isinstance(best_iteration, int) and best_iteration >= 0:
            best_iteration_values.append(float(best_iteration + 1))

        train_auc_values.append(train_auc)
        auc_values.append(auc)
        if math.isfinite(train_auc) and math.isfinite(auc):
            train_test_auc_gap_values.append(max(0.0, train_auc - auc))
        logloss_values.append(ll)
        acc_values.append(acc)
        baseline_auc_values.append(base_auc)
        trained_split_count += 1
        if first_trained_split is None:
            if len(X_test) > 0:
                first_trained_split = {
                    "x_train": X_fit,
                    "y_train": y_fit,
                    "x_test": X_test,
                    "y_test": y_test,
                }

        split_reports.append(
            {
                "split_id": split_id,
                "status": "trained",
                "train_class_counts": train_counts,
                "test_class_counts": test_counts,
                "train_range": train_range,
                "test_range": test_range,
                "metrics": {
                    "train_auc": train_auc,
                    "validation_auc": validation_auc,
                    "auc": auc,
                    "logloss": ll,
                    "accuracy": acc,
                    "baseline_auc": base_auc,
                    "best_iteration": int(best_iteration + 1) if isinstance(best_iteration, int) and best_iteration >= 0 else None,
                    "net_objective": net_objective,
                    "net_objective_evaluation_scope": (
                        "all_finite_execution_bar_rows_on_raw_test_axis"
                    ),
                },
                "fit_window": fit_meta,
                "feature_transform_scope": "split_fit_only_before_purged_validation",
            }
        )
        log_info(
            "INTEGRATOR_SPLIT: "
            f"id={split_id}, train=[{split.train_start},{split.train_end}), "
            f"test=[{split.test_start},{split.test_end}), "
            f"train_auc={train_auc:.6f}, validation_auc={validation_auc:.6f}, "
            f"auc={auc:.6f}, baseline_auc={base_auc:.6f}, "
            f"mean_net_edge_bps={split_mean_net:.6f}"
        )

    if trained_split_count == 0:
        raise ValueError(
            "所有时序切分都因 train_single_class 被跳过，无法完成离线训练。"
            "建议：1) 增加历史样本；2) 缩短 predict_horizon_bars；"
            "3) 调整 train/test/step 窗口参数。"
        )

    full_counts = class_count(y)
    if len(full_counts.keys()) < 2:
        raise ValueError(
            f"全量有效样本标签只有单一类别: {full_counts}。"
            "无法训练最终模型，请增加样本或调整标签口径。"
        )

    random_label_auc = float("nan")
    random_label_auc_mean = float("nan")
    random_label_auc_stdev = float("nan")
    random_label_auc_max = float("nan")
    random_label_auc_values: List[float] = []
    run_random_label_control = not bool(args.disable_random_label_control)
    if run_random_label_control and first_trained_split is not None:
        control = first_trained_split
        random_label_auc_values = run_random_label_control_trials(
            x_train=control["x_train"],
            y_train=control["y_train"],
            x_test=control["x_test"],
            y_test=control["y_test"],
            random_seed=int(args.random_seed),
            iterations=min(int(args.iterations), int(args.random_label_iterations)),
            depth=int(args.depth),
            learning_rate=float(args.learning_rate),
            l2_leaf_reg=float(args.l2_leaf_reg),
            random_strength=float(args.random_strength),
            subsample=float(args.subsample),
            rsm=float(args.rsm),
            trials=int(args.random_label_trials),
        )
        random_label_auc_mean = mean_ignore_nan(random_label_auc_values)
        random_label_auc_stdev = stdev_ignore_nan(random_label_auc_values)
        finite_control_auc = [v for v in random_label_auc_values if math.isfinite(v)]
        if finite_control_auc:
            random_label_auc_max = float(max(finite_control_auc))
        random_label_auc = random_label_auc_mean
        log_info(
            "INTEGRATOR_RANDOM_LABEL_CONTROL: "
            f"trials={int(args.random_label_trials)}, mean_auc={random_label_auc_mean:.6f}, "
            f"max_auc={random_label_auc_max:.6f}, max_allowed={float(args.max_random_label_auc):.6f}"
        )

    # Fit the deployable transform without looking at the temporal validation tail.
    (
        X_final_fit_raw,
        y_final_fit,
        X_final_val_raw,
        y_final_val,
        final_fit_meta,
    ) = split_raw_temporal_train_validation(
        raw_features,
        label,
        raw_start=0,
        raw_end=len(raw_features),
        validation_fraction=float(args.validation_fraction),
        min_validation_samples=int(args.min_validation_samples),
        purge_bars=label_lookahead_bars,
    )
    transform_fit_source = (
        X_final_fit_raw if X_final_val_raw is not None else X_raw
    )
    _, feature_transform = build_feature_transform(
        transform_fit_source,
        feature_names,
        feature_clip_quantile=float(args.feature_clip_quantile),
    )
    X = apply_feature_transform(X_raw, feature_names, feature_transform)
    X_final_fit = apply_feature_transform(
        X_final_fit_raw,
        feature_names,
        feature_transform,
    )
    X_final_val = (
        apply_feature_transform(
            X_final_val_raw,
            feature_names,
            feature_transform,
        )
        if X_final_val_raw is not None
        else None
    )
    if feature_transform.get("feature_clipping_enabled"):
        log_info(
            "INTEGRATOR_FEATURE_TRANSFORM: "
            f"scope=final_train_only, "
            f"clip_quantile={float(args.feature_clip_quantile):.6f}, "
            f"clipped_features={feature_transform.get('enabled_clip_bound_count', 0)}/"
            f"{len(feature_names)}"
        )
    if feature_transform.get("feature_normalization_enabled"):
        log_info(
            "INTEGRATOR_FEATURE_NORMALIZATION: "
            f"scope=final_train_only, "
            f"method={feature_transform.get('normalization_method')}, "
            f"normalized_features={feature_transform.get('enabled_normalization_count', 0)}/"
            f"{len(feature_names)}, "
            f"max_abs={feature_transform.get('normalization_max_abs')}"
        )
    final_iterations = int(args.iterations)
    if X_final_val is not None and y_final_val is not None:
        tune_model = build_catboost_classifier(
            random_seed=int(args.random_seed),
            iterations=int(args.iterations),
            depth=int(args.depth),
            learning_rate=float(args.learning_rate),
            l2_leaf_reg=float(args.l2_leaf_reg),
            random_strength=float(args.random_strength),
            subsample=float(args.subsample),
            rsm=float(args.rsm),
        )
        tune_model.fit(
            X_final_fit,
            y_final_fit,
            eval_set=(X_final_val, y_final_val),
            use_best_model=True,
            early_stopping_rounds=int(args.early_stopping_rounds),
        )
        tuned_best_iteration = tune_model.get_best_iteration()
        if isinstance(tuned_best_iteration, int) and tuned_best_iteration >= 0:
            final_iterations = min(int(args.iterations), tuned_best_iteration + 1)
    final_model = build_catboost_classifier(
        random_seed=int(args.random_seed),
        iterations=final_iterations,
        depth=int(args.depth),
        learning_rate=float(args.learning_rate),
        l2_leaf_reg=float(args.l2_leaf_reg),
        random_strength=float(args.random_strength),
        subsample=float(args.subsample),
        rsm=float(args.rsm),
    )
    final_model.fit(X, y)

    importance = final_model.get_feature_importance()
    feature_importance = sorted(
        [
            {
                "feature": name,
                "importance": float(score),
                "source": "miner" if name.startswith("miner_") else "classic",
            }
            for name, score in zip(feature_names, importance)
        ],
        key=lambda item: item["importance"],
        reverse=True,
    )

    schema_seed = "feature_transform=clip_plus_robust_norm_v2|" + "|".join(feature_names)
    schema_hash = hashlib.sha256(schema_seed.encode("utf-8")).hexdigest()[:16]
    model_hash_seed = (
        f"{schema_hash}|{args.split_method}|{args.predict_horizon_bars}|"
        f"{int(args.execution_latency_bars)}|"
        f"{float(args.label_round_trip_cost_bps):.6f}|"
        f"{float(args.label_min_net_edge_bps):.6f}|"
        f"{float(args.model_confidence_threshold):.6f}|"
        f"{float(args.model_score_gain):.6f}|"
        f"{float(args.feature_clip_quantile):.6f}|"
        f"{args.random_seed}|{int(time.time() * 1000)}"
    )
    model_hash = hashlib.sha256(model_hash_seed.encode("utf-8")).hexdigest()[:16]
    model_version = f"integrator_cb_v1_{model_hash}"
    feature_schema_version = f"feature_schema_v1_{schema_hash}"

    turnover_denominator = max(1e-12, model_net_total_turnover)
    mean_net_per_bar = (
        model_net_total_net_bps / float(model_net_evaluated_bar_count)
        if model_net_evaluated_bar_count > 0
        else float("nan")
    )
    model_net_edge_lcb_bps = float("nan")
    split_net_mean = mean_ignore_nan(model_net_edge_split_values)
    split_net_stdev = stdev_ignore_nan(model_net_edge_split_values)
    if (
        len(model_net_edge_split_values) >= 2
        and math.isfinite(split_net_mean)
        and math.isfinite(split_net_stdev)
    ):
        split_standard_error = split_net_stdev / math.sqrt(
            float(len(model_net_edge_split_values))
        )
        model_net_edge_lcb_bps = split_net_mean - student_t_975(
            len(model_net_edge_split_values) - 1
        ) * split_standard_error

    metrics_oos = {
        "primary_objective": "aggregate_model_net_bps_per_unit_turnover_after_cost",
        "primary_objective_definition": (
            "aggregate non-overlapping OOS fixed-horizon canary episode PnL "
            "divided by aggregate round-trip turnover after declared cost"
        ),
        "model_economic_episode_holding_bars": int(args.predict_horizon_bars),
        "evidence_tier": "offline_model_economic_prescreen",
        "authoritative_promotion_evidence": "live_candidate_episode_canary",
        "required_offline_prescreen": "independent_cpp_replay_next_bar_ohlc_touch",
        "mean_model_gross_edge_bps": (
            model_net_total_gross_bps / turnover_denominator
        ),
        "mean_model_net_edge_bps": (
            model_net_total_net_bps / turnover_denominator
        ),
        "mean_model_net_edge_bps_per_round_trip": (
            2.0 * model_net_total_net_bps / turnover_denominator
        ),
        "median_model_net_edge_bps": median_ignore_nan(
            model_net_edge_split_values
        ),
        "positive_model_net_edge_ratio": (
            float(model_net_positive_trade_count)
            / float(model_net_total_trades)
            if model_net_total_trades > 0
            else float("nan")
        ),
        "model_net_objective_sample_count": model_net_evaluated_bar_count,
        "model_net_total_gross_edge_bps": model_net_total_gross_bps,
        "model_net_total_net_edge_bps": model_net_total_net_bps,
        "model_net_total_turnover": model_net_total_turnover,
        "model_net_total_trades": model_net_total_trades,
        "model_net_active_bar_count": model_net_active_bar_count,
        "model_net_positive_trade_count": model_net_positive_trade_count,
        "model_net_evaluated_bar_count": model_net_evaluated_bar_count,
        "model_net_mean_per_bar_bps": mean_net_per_bar,
        "model_net_edge_lcb_bps": model_net_edge_lcb_bps,
        "model_net_edge_lcb_method": "non_overlapping_oos_split_student_t_95",
        "mean_model_net_edge_bps_by_split": split_net_mean,
        "model_net_edge_bps_split_stdev": split_net_stdev,
        "positive_model_net_edge_ratio_by_split": (
            float(model_net_positive_split_count)
            / float(model_net_economic_split_count)
            if model_net_economic_split_count > 0
            else float("nan")
        ),
        "oos_economic_split_count": model_net_economic_split_count,
        "oos_test_window_bar_count": int(
            sum(split.test_end - split.test_start for split in splits)
        ),
        "oos_duplicate_bar_count": 0,
        "oos_duplicate_bar_ratio": 0.0,
        "net_objective_round_trip_cost_bps": float(args.label_round_trip_cost_bps),
        "model_confidence_threshold": float(args.model_confidence_threshold),
        "model_score_gain": float(args.model_score_gain),
        "train_auc_mean": mean_ignore_nan(train_auc_values),
        "train_auc_stdev": stdev_ignore_nan(train_auc_values),
        "auc_mean": mean_ignore_nan(auc_values),
        "auc_stdev": stdev_ignore_nan(auc_values),
        "validation_auc_mean": mean_ignore_nan(validation_auc_values),
        "train_test_auc_gap_mean": mean_ignore_nan(train_test_auc_gap_values),
        "logloss_mean": mean_ignore_nan(logloss_values),
        "accuracy_mean": mean_ignore_nan(acc_values),
        "baseline_auc_mean": mean_ignore_nan(baseline_auc_values),
        "best_iteration_mean": mean_ignore_nan(best_iteration_values),
        "delta_auc_vs_baseline": mean_ignore_nan(auc_values)
        - mean_ignore_nan(baseline_auc_values),
        "random_label_auc": random_label_auc,
        "random_label_auc_mean": random_label_auc_mean,
        "random_label_auc_stdev": random_label_auc_stdev,
        "random_label_auc_max": random_label_auc_max,
        "random_label_trials": int(args.random_label_trials) if run_random_label_control else 0,
        "split_trained_count": trained_split_count,
        "split_count": len(split_reports),
        "split_trained_ratio": (
            float(trained_split_count) / float(len(split_reports))
            if len(split_reports) > 0
            else float("nan")
        ),
        "splits": split_reports,
    }
    governance_pass, governance_fail_reasons, governance_warn_reasons = evaluate_governance(
        metrics_oos=metrics_oos,
        min_auc_mean=float(args.min_auc_mean),
        min_delta_auc_vs_baseline=float(args.min_delta_auc_vs_baseline),
        min_split_trained_count=int(args.min_split_trained_count),
        min_split_trained_ratio=float(args.min_split_trained_ratio),
        max_auc_stdev=float(args.max_auc_stdev),
        max_train_test_auc_gap=float(args.max_train_test_auc_gap),
        run_random_label_control=run_random_label_control,
        max_random_label_auc=float(args.max_random_label_auc),
        min_mean_model_net_edge_bps=float(args.min_mean_model_net_edge_bps),
        min_positive_model_net_edge_ratio=float(args.min_positive_model_net_edge_ratio),
        min_model_net_total_trades=int(args.min_model_net_total_trades),
        min_model_net_active_bars=int(args.min_model_net_active_bars),
        min_positive_model_net_splits_ratio=float(
            args.min_positive_model_net_splits_ratio
        ),
        min_model_net_edge_lcb_bps=float(args.min_model_net_edge_lcb_bps),
    )
    governance = {
        "pass": governance_pass,
        "fail_reasons": governance_fail_reasons,
        "warn_reasons": governance_warn_reasons,
        "primary_objective": "aggregate_model_net_bps_per_unit_turnover_after_cost",
        "thresholds": {
            "min_mean_model_net_edge_bps": float(args.min_mean_model_net_edge_bps),
            "min_positive_model_net_edge_ratio": float(
                args.min_positive_model_net_edge_ratio
            ),
            "min_model_net_total_trades": int(args.min_model_net_total_trades),
            "min_model_net_active_bars": int(args.min_model_net_active_bars),
            "min_positive_model_net_splits_ratio": float(
                args.min_positive_model_net_splits_ratio
            ),
            "min_model_net_edge_lcb_bps": float(
                args.min_model_net_edge_lcb_bps
            ),
            "model_confidence_threshold": float(
                args.model_confidence_threshold
            ),
            "model_score_gain": float(args.model_score_gain),
            "min_auc_mean": float(args.min_auc_mean),
            "min_delta_auc_vs_baseline": float(args.min_delta_auc_vs_baseline),
            "min_split_trained_count": int(args.min_split_trained_count),
            "min_split_trained_ratio": float(args.min_split_trained_ratio),
            "max_auc_stdev": float(args.max_auc_stdev),
            "max_train_test_auc_gap": float(args.max_train_test_auc_gap),
            "run_random_label_control": run_random_label_control,
            "max_random_label_auc": float(args.max_random_label_auc),
            "random_label_trials": int(args.random_label_trials),
        },
    }
    model_artifact_status = "published" if governance_pass else "rejected_not_published"
    if governance_pass:
        temporary_model_path = model_out_path.with_name(
            f".{model_out_path.name}.tmp-{os.getpid()}"
        )
        try:
            final_model.save_model(str(temporary_model_path))
            os.replace(temporary_model_path, model_out_path)
        finally:
            temporary_model_path.unlink(missing_ok=True)

    report_payload = {
        "model_version": model_version,
        "feature_schema_version": feature_schema_version,
        "factor_set_version": factor_set_version,
        "model_type": "catboost_classifier",
        "created_at_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": {
            "csv_path": str(csv_path),
            "csv_sha256": file_sha256(csv_path),
            "research_domain": "development",
            "training_symbol": args.training_symbol,
            "bar_interval_ms": int(args.bar_interval_ms),
            "online_bar_source": "closed_ohlcv",
            "source_venue": args.source_venue,
            "source_category": args.source_category,
            "price_type": args.price_type,
            "volume_unit": args.volume_unit,
            "miner_report_path": str(miner_report_path),
            "sample_count_after_filter": int(len(X_raw)),
            "raw_bar_count": int(len(raw_features)),
            "predict_horizon_bars": int(args.predict_horizon_bars),
            "execution_latency_bars": int(args.execution_latency_bars),
            "model_confidence_threshold": float(
                args.model_confidence_threshold
            ),
            "model_score_gain": float(args.model_score_gain),
            "label_policy": label_policy,
            "time_axis_quality": time_axis_quality,
        },
        "feature_transform": feature_transform,
        "anti_leakage": {
            "feature_time": "t",
            "label_time": (
                f"close[t+{int(args.execution_latency_bars)}] -> "
                f"close[t+{int(args.execution_latency_bars) + int(args.predict_horizon_bars)}]"
            ),
            "economic_return_time": (
                f"close[t+{int(args.execution_latency_bars)}] -> "
                f"close[t+{int(args.execution_latency_bars) + 1}]"
            ),
            "split_method": args.split_method,
            "random_kfold_forbidden": True,
            "window_boundary_logged": True,
            "split_axis": "raw_bar_index_before_label_filter",
            "oos_windows_non_overlapping": True,
            "label_lookahead_bars": label_lookahead_bars,
            "purge_bars": label_lookahead_bars,
            "purge_uses_original_row_indices": True,
            "inner_validation_purge_bars": label_lookahead_bars,
            "feature_transform_scope": "split_fit_only_before_purged_validation",
            "final_feature_transform_scope": (
                "final_fit_only_before_purged_validation_tail"
            ),
            "random_label_control_enabled": run_random_label_control,
            "online_feature_contract": {
                "symbol_scope": "single_symbol",
                "training_symbol": args.training_symbol,
                "bar_interval_ms": int(args.bar_interval_ms),
                "ohlcv_semantics": "closed_bar",
                "source_venue": args.source_venue,
                "source_category": args.source_category,
                "price_type": args.price_type,
                "volume_unit": args.volume_unit,
                "live_ticker_as_training_bar_forbidden": True,
                "production_history_bootstrap_required": True,
                "replay_history_bootstrap_forbidden": True,
            },
        },
        "train_config": {
            "split_method": args.split_method,
            "n_splits": int(args.n_splits),
            "train_window_bars": int(args.train_window_bars),
            "test_window_bars": int(args.test_window_bars),
            "rolling_step_bars": int(args.rolling_step_bars),
            "iterations": int(args.iterations),
            "depth": int(args.depth),
            "learning_rate": float(args.learning_rate),
            "l2_leaf_reg": float(args.l2_leaf_reg),
            "random_strength": float(args.random_strength),
            "subsample": float(args.subsample),
            "rsm": float(args.rsm),
            "validation_fraction": float(args.validation_fraction),
            "min_validation_samples": int(args.min_validation_samples),
            "early_stopping_rounds": int(args.early_stopping_rounds),
            "final_model_iterations": int(final_iterations),
            "random_seed": int(args.random_seed),
            "label_round_trip_cost_bps": float(args.label_round_trip_cost_bps),
            "label_min_net_edge_bps": float(args.label_min_net_edge_bps),
            "execution_latency_bars": int(args.execution_latency_bars),
            "model_confidence_threshold": float(
                args.model_confidence_threshold
            ),
            "model_score_gain": float(args.model_score_gain),
            "min_mean_model_net_edge_bps": float(args.min_mean_model_net_edge_bps),
            "min_positive_model_net_edge_ratio": float(
                args.min_positive_model_net_edge_ratio
            ),
            "min_model_net_total_trades": int(args.min_model_net_total_trades),
            "min_model_net_active_bars": int(args.min_model_net_active_bars),
            "min_positive_model_net_splits_ratio": float(
                args.min_positive_model_net_splits_ratio
            ),
            "min_model_net_edge_lcb_bps": float(
                args.min_model_net_edge_lcb_bps
            ),
            "feature_clip_quantile": float(args.feature_clip_quantile),
        },
        "metrics_oos": metrics_oos,
        "governance": governance,
        "feature_importance": feature_importance,
        "feature_names": feature_names,
        "model_out": str(model_out_path) if governance_pass else None,
        "model_artifact_status": model_artifact_status,
        "final_fit_window": final_fit_meta,
    }

    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(report_payload, fp, ensure_ascii=False, indent=2)

    log_info(
        "INTEGRATOR_DONE: "
        f"model_version={model_version}, feature_schema_version={feature_schema_version}, "
        f"output={output_path}, "
        f"model_out={model_out_path if governance_pass else 'none'}"
    )
    log_info(
        "INTEGRATOR_GOVERNANCE: "
        f"pass={str(governance_pass).lower()}, "
        f"fail_reasons={len(governance_fail_reasons)}, "
        f"warn_reasons={len(governance_warn_reasons)}"
    )
    if not governance_pass:
        print(
            "[ERROR] 治理门槛未通过: " + "; ".join(governance_fail_reasons),
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
