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
- 标签对齐口径固定：t 时刻特征，预测 t+1...t+h 的收益方向。
"""

from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import hashlib
import json
import math
import pathlib
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

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


def to_iso_utc(timestamp_ms: int) -> str:
    return dt.datetime.utcfromtimestamp(timestamp_ms / 1000.0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def sanitize_array(values: np.ndarray) -> np.ndarray:
    out = values.astype(np.float64, copy=True)
    out[~np.isfinite(out)] = np.nan
    return out


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
        # 归一化秩：1/window ~ 1.0
        out[i] = (smaller + 0.5 * (equal - 1.0) + 1.0) / float(window)
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


@dataclass
class FactorSpec:
    expression: str
    invert_signal: bool


def load_factor_specs(report_path: pathlib.Path, top_k: int) -> Tuple[str, List[FactorSpec]]:
    with report_path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)

    factor_set_version = str(payload.get("factor_set_version", "unknown_factor_set"))
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


def build_label(close: np.ndarray, horizon: int) -> Tuple[np.ndarray, np.ndarray]:
    n = len(close)
    forward_return = np.full(n, np.nan, dtype=np.float64)
    if horizon <= 0:
        return forward_return, np.full(n, np.nan, dtype=np.float64)
    # 反泄漏口径：t 时刻特征，标签使用 (t+1)->(t+h+1) 的前瞻收益。
    for i in range(n - horizon - 1):
        base = close[i + 1]
        future = close[i + horizon + 1]
        if not math.isfinite(float(base)) or abs(base) < 1e-12 or not math.isfinite(float(future)):
            continue
        forward_return[i] = future / base - 1.0
    label = np.where(np.isfinite(forward_return), (forward_return > 0.0).astype(np.float64), np.nan)
    return label, forward_return


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
            train_start = 0
            train_end = first_train_end + idx * test_window
            test_start = train_end
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

    max_splits = (sample_count - train_window - test_window) // step_window + 1
    if max_splits <= 0:
        raise ValueError("rolling 参数导致无法切分，请增大样本或减小窗口")
    use_splits = min(n_splits, max_splits)
    first_test_start = sample_count - (use_splits * step_window + test_window - step_window)
    first_test_start = max(first_test_start, train_window)

    for idx in range(use_splits):
        test_start = first_test_start + idx * step_window
        test_end = test_start + test_window
        train_end = test_start
        train_start = train_end - train_window
        if train_start < 0 or test_end > sample_count:
            continue
        splits.append(SplitRange(train_start, train_end, test_start, test_end))
    if not splits:
        raise ValueError("rolling 未生成有效切分")
    return splits


def mean_ignore_nan(values: Sequence[float]) -> float:
    finite = [v for v in values if math.isfinite(v)]
    return float(statistics.mean(finite)) if finite else float("nan")


def stdev_ignore_nan(values: Sequence[float]) -> float:
    finite = [v for v in values if math.isfinite(v)]
    if len(finite) <= 1:
        return float("nan")
    return float(statistics.stdev(finite))


def class_count(values: np.ndarray) -> Dict[int, int]:
    finite = values[np.isfinite(values)].astype(np.int32)
    result: Dict[int, int] = {}
    for cls in finite:
        key = int(cls)
        result[key] = result.get(key, 0) + 1
    return result


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
) -> Tuple[bool, List[str]]:
    fail_reasons: List[str] = []
    auc_mean = metrics_oos.get("auc_mean", float("nan"))
    delta_auc = metrics_oos.get("delta_auc_vs_baseline", float("nan"))
    split_trained_count = metrics_oos.get("split_trained_count", float("nan"))
    split_trained_ratio = metrics_oos.get("split_trained_ratio", float("nan"))
    auc_stdev = metrics_oos.get("auc_stdev", float("nan"))
    train_test_auc_gap_mean = metrics_oos.get("train_test_auc_gap_mean", float("nan"))
    random_label_auc = metrics_oos.get("random_label_auc", float("nan"))

    if not math.isfinite(auc_mean):
        fail_reasons.append("缺少或无效 metrics_oos.auc_mean")
    elif auc_mean < min_auc_mean:
        fail_reasons.append(
            f"auc_mean={auc_mean:.6f} < min_auc_mean={min_auc_mean:.6f}"
        )

    if not math.isfinite(delta_auc):
        fail_reasons.append("缺少或无效 metrics_oos.delta_auc_vs_baseline")
    elif delta_auc < min_delta_auc_vs_baseline:
        fail_reasons.append(
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

    if math.isfinite(auc_stdev) and auc_stdev > max_auc_stdev:
        fail_reasons.append(f"auc_stdev={auc_stdev:.6f} > max_auc_stdev={max_auc_stdev:.6f}")

    if math.isfinite(train_test_auc_gap_mean) and train_test_auc_gap_mean > max_train_test_auc_gap:
        fail_reasons.append(
            "train_test_auc_gap_mean="
            f"{train_test_auc_gap_mean:.6f} > max_train_test_auc_gap={max_train_test_auc_gap:.6f}"
        )

    if run_random_label_control:
        if not math.isfinite(random_label_auc):
            fail_reasons.append("缺少或无效 metrics_oos.random_label_auc")
        elif random_label_auc > max_random_label_auc:
            fail_reasons.append(
                "random_label_auc="
                f"{random_label_auc:.6f} > max_random_label_auc={max_random_label_auc:.6f}"
            )

    return len(fail_reasons) == 0, fail_reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="CatBoost Integrator 离线训练（R2）")
    parser.add_argument("--csv", required=True, help="研究数据 CSV 路径（OHLCV）")
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
    parser.add_argument("--iterations", type=int, default=300, help="CatBoost 迭代次数")
    parser.add_argument("--depth", type=int, default=6, help="CatBoost 树深")
    parser.add_argument("--learning_rate", type=float, default=0.05, help="CatBoost 学习率")
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
        "--fail_on_governance",
        action="store_true",
        help="治理门槛不通过时返回非零退出码",
    )
    parser.add_argument(
        "--min_samples",
        type=int,
        default=120,
        help="最小有效样本数（小样本烟囱测试可调低）",
    )
    args = parser.parse_args()

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

    series = load_ohlcv_csv(csv_path)
    factor_set_version, factor_specs = load_factor_specs(miner_report_path, max(1, args.top_k))
    log_info(f"INTEGRATOR_START: bars={len(series['close'])}, factors={len(factor_specs)}")

    features, feature_names, ret_1 = build_feature_matrix(series, factor_specs)
    label, _ = build_label(series["close"], args.predict_horizon_bars)
    valid_mask = np.isfinite(label) & np.all(np.isfinite(features), axis=1)

    X = features[valid_mask]
    y = label[valid_mask]
    ret1_valid = ret_1[valid_mask]
    ts_valid = series["timestamp"][valid_mask]

    if args.split_method == "rolling" and args.train_window_bars > 0:
        required = (
            args.train_window_bars
            + args.test_window_bars
            + max(0, args.n_splits - 1) * max(1, args.rolling_step_bars)
        )
        if len(X) < required:
            raise ValueError(
                "rolling 参数与样本规模不匹配："
                f"至少需要 {required} 条有效样本，当前仅 {len(X)}。"
                "可增大历史数据，或降低 train/test/step/n_splits。"
            )

    if len(X) < args.min_samples:
        raise ValueError(
            f"有效样本过少: {len(X)}，小于 --min_samples={args.min_samples}。"
            "若仅做功能验证可下调 --min_samples；正式验收建议使用更长历史样本。"
        )

    splits = build_splits(
        sample_count=len(X),
        method=args.split_method,
        n_splits=args.n_splits,
        train_window=args.train_window_bars,
        test_window=args.test_window_bars,
        step_window=args.rolling_step_bars,
    )

    split_reports: List[dict] = []
    auc_values: List[float] = []
    train_auc_values: List[float] = []
    train_test_auc_gap_values: List[float] = []
    logloss_values: List[float] = []
    acc_values: List[float] = []
    baseline_auc_values: List[float] = []
    trained_split_count = 0
    first_trained_split: Dict[str, np.ndarray] | None = None

    for split_id, split in enumerate(splits, start=1):
        X_train = X[split.train_start : split.train_end]
        y_train = y[split.train_start : split.train_end]
        X_test = X[split.test_start : split.test_end]
        y_test = y[split.test_start : split.test_end]
        test_ret1 = ret1_valid[split.test_start : split.test_end]
        train_counts = class_count(y_train)
        test_counts = class_count(y_test)

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
                    "train_range": {
                        "start": int(split.train_start),
                        "end_exclusive": int(split.train_end),
                        "ts_start": to_iso_utc(int(ts_valid[split.train_start])),
                        "ts_end": to_iso_utc(int(ts_valid[split.train_end - 1])),
                    },
                    "test_range": {
                        "start": int(split.test_start),
                        "end_exclusive": int(split.test_end),
                        "ts_start": to_iso_utc(int(ts_valid[split.test_start])),
                        "ts_end": to_iso_utc(int(ts_valid[split.test_end - 1])),
                    },
                }
            )
            continue

        model = CatBoostClassifier(
            loss_function="Logloss",
            eval_metric="AUC",
            random_seed=args.random_seed,
            iterations=args.iterations,
            depth=args.depth,
            learning_rate=args.learning_rate,
            verbose=False,
            allow_writing_files=False,
        )
        model.fit(X_train, y_train)
        train_score = model.predict_proba(X_train)[:, 1]
        score = model.predict_proba(X_test)[:, 1]
        baseline_score = np.where(np.isfinite(test_ret1), np.where(test_ret1 > 0.0, 0.9, 0.1), 0.5)

        train_auc = auc_score(y_train, train_score)
        auc = auc_score(y_test, score)
        ll = logloss_score(y_test, score)
        acc = accuracy_score(y_test, score)
        base_auc = auc_score(y_test, baseline_score)

        train_auc_values.append(train_auc)
        auc_values.append(auc)
        if math.isfinite(train_auc) and math.isfinite(auc):
            train_test_auc_gap_values.append(max(0.0, train_auc - auc))
        logloss_values.append(ll)
        acc_values.append(acc)
        baseline_auc_values.append(base_auc)
        trained_split_count += 1
        if first_trained_split is None:
            first_trained_split = {
                "x_train": X_train,
                "y_train": y_train,
                "x_test": X_test,
                "y_test": y_test,
            }

        split_reports.append(
            {
                "split_id": split_id,
                "status": "trained",
                "train_class_counts": train_counts,
                "test_class_counts": test_counts,
                "train_range": {
                    "start": int(split.train_start),
                    "end_exclusive": int(split.train_end),
                    "ts_start": to_iso_utc(int(ts_valid[split.train_start])),
                    "ts_end": to_iso_utc(int(ts_valid[split.train_end - 1])),
                },
                "test_range": {
                    "start": int(split.test_start),
                    "end_exclusive": int(split.test_end),
                    "ts_start": to_iso_utc(int(ts_valid[split.test_start])),
                    "ts_end": to_iso_utc(int(ts_valid[split.test_end - 1])),
                },
                "metrics": {
                    "train_auc": train_auc,
                    "auc": auc,
                    "logloss": ll,
                    "accuracy": acc,
                    "baseline_auc": base_auc,
                },
            }
        )
        log_info(
            "INTEGRATOR_SPLIT: "
            f"id={split_id}, train=[{split.train_start},{split.train_end}), "
            f"test=[{split.test_start},{split.test_end}), "
            f"train_auc={train_auc:.6f}, auc={auc:.6f}, baseline_auc={base_auc:.6f}"
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
    run_random_label_control = not bool(args.disable_random_label_control)
    if run_random_label_control and first_trained_split is not None:
        control = first_trained_split
        y_train_control = control["y_train"].copy()
        rng = np.random.default_rng(int(args.random_seed) + 20260222)
        rng.shuffle(y_train_control)
        control_model = CatBoostClassifier(
            loss_function="Logloss",
            eval_metric="AUC",
            random_seed=int(args.random_seed) + 17,
            iterations=min(int(args.iterations), int(args.random_label_iterations)),
            depth=max(2, int(args.depth) // 2),
            learning_rate=float(args.learning_rate),
            verbose=False,
            allow_writing_files=False,
        )
        control_model.fit(control["x_train"], y_train_control)
        control_score = control_model.predict_proba(control["x_test"])[:, 1]
        random_label_auc = auc_score(control["y_test"], control_score)
        log_info(
            "INTEGRATOR_RANDOM_LABEL_CONTROL: "
            f"auc={random_label_auc:.6f}, max_allowed={float(args.max_random_label_auc):.6f}"
        )

    # 用全部有效样本训练最终模型并导出（供后续影子推理使用）。
    final_model = CatBoostClassifier(
        loss_function="Logloss",
        eval_metric="AUC",
        random_seed=args.random_seed,
        iterations=args.iterations,
        depth=args.depth,
        learning_rate=args.learning_rate,
        verbose=False,
        allow_writing_files=False,
    )
    final_model.fit(X, y)
    final_model.save_model(str(model_out_path))

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

    schema_hash = hashlib.sha256("|".join(feature_names).encode("utf-8")).hexdigest()[:16]
    model_hash_seed = (
        f"{schema_hash}|{args.split_method}|{args.predict_horizon_bars}|{args.random_seed}|{int(time.time() * 1000)}"
    )
    model_hash = hashlib.sha256(model_hash_seed.encode("utf-8")).hexdigest()[:16]
    model_version = f"integrator_cb_v1_{model_hash}"
    feature_schema_version = f"feature_schema_v1_{schema_hash}"

    metrics_oos = {
        "train_auc_mean": mean_ignore_nan(train_auc_values),
        "train_auc_stdev": stdev_ignore_nan(train_auc_values),
        "auc_mean": mean_ignore_nan(auc_values),
        "auc_stdev": stdev_ignore_nan(auc_values),
        "train_test_auc_gap_mean": mean_ignore_nan(train_test_auc_gap_values),
        "logloss_mean": mean_ignore_nan(logloss_values),
        "accuracy_mean": mean_ignore_nan(acc_values),
        "baseline_auc_mean": mean_ignore_nan(baseline_auc_values),
        "delta_auc_vs_baseline": mean_ignore_nan(auc_values)
        - mean_ignore_nan(baseline_auc_values),
        "random_label_auc": random_label_auc,
        "split_trained_count": trained_split_count,
        "split_count": len(split_reports),
        "split_trained_ratio": (
            float(trained_split_count) / float(len(split_reports))
            if len(split_reports) > 0
            else float("nan")
        ),
        "splits": split_reports,
    }
    governance_pass, governance_fail_reasons = evaluate_governance(
        metrics_oos=metrics_oos,
        min_auc_mean=float(args.min_auc_mean),
        min_delta_auc_vs_baseline=float(args.min_delta_auc_vs_baseline),
        min_split_trained_count=int(args.min_split_trained_count),
        min_split_trained_ratio=float(args.min_split_trained_ratio),
        max_auc_stdev=float(args.max_auc_stdev),
        max_train_test_auc_gap=float(args.max_train_test_auc_gap),
        run_random_label_control=run_random_label_control,
        max_random_label_auc=float(args.max_random_label_auc),
    )
    governance = {
        "pass": governance_pass,
        "fail_reasons": governance_fail_reasons,
        "thresholds": {
            "min_auc_mean": float(args.min_auc_mean),
            "min_delta_auc_vs_baseline": float(args.min_delta_auc_vs_baseline),
            "min_split_trained_count": int(args.min_split_trained_count),
            "min_split_trained_ratio": float(args.min_split_trained_ratio),
            "max_auc_stdev": float(args.max_auc_stdev),
            "max_train_test_auc_gap": float(args.max_train_test_auc_gap),
            "run_random_label_control": run_random_label_control,
            "max_random_label_auc": float(args.max_random_label_auc),
        },
    }

    report_payload = {
        "model_version": model_version,
        "feature_schema_version": feature_schema_version,
        "factor_set_version": factor_set_version,
        "model_type": "catboost_classifier",
        "created_at_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": {
            "csv_path": str(csv_path),
            "miner_report_path": str(miner_report_path),
            "sample_count_after_filter": int(len(X)),
            "predict_horizon_bars": int(args.predict_horizon_bars),
        },
        "anti_leakage": {
            "feature_time": "t",
            "label_time": f"t+1..t+{args.predict_horizon_bars + 1}",
            "split_method": args.split_method,
            "random_kfold_forbidden": True,
            "window_boundary_logged": True,
            "random_label_control_enabled": run_random_label_control,
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
            "random_seed": int(args.random_seed),
        },
        "metrics_oos": metrics_oos,
        "governance": governance,
        "feature_importance": feature_importance,
        "feature_names": feature_names,
        "model_out": str(model_out_path),
    }

    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(report_payload, fp, ensure_ascii=False, indent=2)

    log_info(
        "INTEGRATOR_DONE: "
        f"model_version={model_version}, feature_schema_version={feature_schema_version}, "
        f"output={output_path}, model_out={model_out_path}"
    )
    log_info(
        "INTEGRATOR_GOVERNANCE: "
        f"pass={str(governance_pass).lower()}, "
        f"fail_reasons={len(governance_fail_reasons)}"
    )
    if args.fail_on_governance and not governance_pass:
        print(
            "[ERROR] 治理门槛未通过: " + "; ".join(governance_fail_reasons),
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
