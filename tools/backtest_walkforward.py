#!/usr/bin/env python3
"""
Run a simple walk-forward backtest on feature-store CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
from dataclasses import dataclass
from typing import Dict, List

import numpy as np


FEATURE_COLUMNS = [
    "ema_diff",
    "zscore_48",
    "mom_12",
    "mom_48",
    "ret_1",
    "range_pct",
    "vol_12",
]


@dataclass
class SplitResult:
    split_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    bars: int
    trades: int
    avg_turnover: float
    total_return: float
    sharpe: float
    max_drawdown: float


def annualization_factor(interval_minutes: int) -> float:
    bars_per_year = (365.0 * 24.0 * 60.0) / float(max(1, interval_minutes))
    return math.sqrt(bars_per_year)


def safe_float(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        return float("nan")


def load_feature_rows(path: pathlib.Path) -> Dict[str, np.ndarray]:
    arrays: Dict[str, List[float]] = {"timestamp": []}
    for col in FEATURE_COLUMNS:
        arrays[col] = []
    arrays["forward_return"] = []

    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            ts = row.get("timestamp", "")
            if not ts.isdigit():
                continue
            arrays["timestamp"].append(float(int(ts)))
            for col in FEATURE_COLUMNS:
                arrays[col].append(safe_float(row.get(col, "nan")))
            arrays["forward_return"].append(safe_float(row.get("forward_return", "nan")))

    out: Dict[str, np.ndarray] = {}
    for key, values in arrays.items():
        dtype = np.float64
        if key == "timestamp":
            dtype = np.int64
        out[key] = np.asarray(values, dtype=dtype)
    return out


def fit_linear_ridge(x: np.ndarray, y: np.ndarray, l2: float) -> np.ndarray:
    if x.ndim != 2:
        raise ValueError("x must be 2D")
    n, m = x.shape
    if n == 0:
        return np.zeros((m + 1,), dtype=np.float64)
    x_aug = np.hstack([np.ones((n, 1), dtype=np.float64), x])
    eye = np.eye(m + 1, dtype=np.float64)
    eye[0, 0] = 0.0
    lhs = x_aug.T @ x_aug + l2 * eye
    rhs = x_aug.T @ y
    try:
        w = np.linalg.solve(lhs, rhs)
    except np.linalg.LinAlgError:
        w = np.linalg.lstsq(lhs, rhs, rcond=None)[0]
    return w


def predict_linear(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return np.asarray([], dtype=np.float64)
    x_aug = np.hstack([np.ones((x.shape[0], 1), dtype=np.float64), x])
    return x_aug @ w


def compute_max_drawdown(equity_curve: List[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0.0:
            dd = 1.0 - (eq / peak)
            if dd > max_dd:
                max_dd = dd
    return max_dd


def run_split(
    *,
    split_index: int,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    y_test: np.ndarray,
    train_start: int,
    train_end: int,
    test_start: int,
    test_end: int,
    fee_bps: float,
    slippage_bps: float,
    signal_threshold: float,
    max_leverage: float,
    pred_scale: float,
    interval_minutes: int,
) -> SplitResult:
    valid_train = np.all(np.isfinite(x_train), axis=1) & np.isfinite(y_train)
    x_train_v = x_train[valid_train]
    y_train_v = y_train[valid_train]
    if x_train_v.shape[0] < max(20, x_train.shape[1] * 3):
        return SplitResult(
            split_index=split_index,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            bars=0,
            trades=0,
            avg_turnover=0.0,
            total_return=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
        )

    w = fit_linear_ridge(x_train_v, y_train_v, l2=1e-6)
    pred = predict_linear(x_test, w)

    fees = (fee_bps + slippage_bps) / 10000.0
    equity = 1.0
    position = 0.0
    turnovers: List[float] = []
    bar_returns: List[float] = []
    equity_curve = [equity]
    trades = 0

    for i in range(x_test.shape[0]):
        r = y_test[i]
        p = pred[i]
        if not (math.isfinite(float(r)) and math.isfinite(float(p))):
            continue
        if abs(p) < signal_threshold:
            target = 0.0
        else:
            confidence = min(1.0, abs(p) / max(pred_scale, 1e-8))
            target = math.copysign(confidence * max_leverage, p)

        turnover = abs(target - position)
        if turnover > 1e-12:
            trades += 1
        pnl = position * float(r) - turnover * fees
        bar_returns.append(pnl)
        turnovers.append(turnover)
        equity *= max(1e-6, 1.0 + pnl)
        position = target
        equity_curve.append(equity)

    bars = len(bar_returns)
    if bars == 0:
        return SplitResult(
            split_index=split_index,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            bars=0,
            trades=trades,
            avg_turnover=0.0,
            total_return=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
        )

    ret_arr = np.asarray(bar_returns, dtype=np.float64)
    mean = float(np.mean(ret_arr))
    std = float(np.std(ret_arr))
    ann = annualization_factor(interval_minutes)
    sharpe = (mean / std * ann) if std > 1e-12 else 0.0
    return SplitResult(
        split_index=split_index,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        bars=bars,
        trades=trades,
        avg_turnover=float(np.mean(np.asarray(turnovers, dtype=np.float64))) if turnovers else 0.0,
        total_return=equity - 1.0,
        sharpe=sharpe,
        max_drawdown=compute_max_drawdown(equity_curve),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward backtest on feature store")
    parser.add_argument("--features", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--train-window", type=int, default=2400)
    parser.add_argument("--test-window", type=int, default=480)
    parser.add_argument("--step-window", type=int, default=480)
    parser.add_argument("--fee-bps", type=float, default=6.0)
    parser.add_argument("--slippage-bps", type=float, default=1.5)
    parser.add_argument("--signal-threshold", type=float, default=0.0002)
    parser.add_argument("--max-leverage", type=float, default=1.5)
    parser.add_argument("--pred-scale", type=float, default=0.002)
    parser.add_argument("--interval-minutes", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    feature_path = pathlib.Path(args.features)
    output_path = pathlib.Path(args.output)
    data = load_feature_rows(feature_path)
    n = int(data["timestamp"].size)
    if n < max(args.train_window + args.test_window, 100):
        print("[ERROR] not enough rows for walk-forward")
        return 2

    x = np.column_stack([data[col] for col in FEATURE_COLUMNS]).astype(np.float64)
    y = data["forward_return"].astype(np.float64)

    split_results: List[SplitResult] = []
    split_index = 0
    start = int(args.train_window)
    test_window = max(20, int(args.test_window))
    step = max(10, int(args.step_window))

    while start + test_window <= n:
        train_start = start - int(args.train_window)
        train_end = start
        test_start = start
        test_end = start + test_window

        split = run_split(
            split_index=split_index,
            x_train=x[train_start:train_end],
            y_train=y[train_start:train_end],
            x_test=x[test_start:test_end],
            y_test=y[test_start:test_end],
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            fee_bps=float(args.fee_bps),
            slippage_bps=float(args.slippage_bps),
            signal_threshold=float(args.signal_threshold),
            max_leverage=float(args.max_leverage),
            pred_scale=float(args.pred_scale),
            interval_minutes=max(1, int(args.interval_minutes)),
        )
        split_results.append(split)
        split_index += 1
        start += step

    valid = [item for item in split_results if item.bars > 0]
    if not valid:
        print("[ERROR] all splits invalid after filtering")
        return 2

    total_bars = sum(item.bars for item in valid)
    total_trades = sum(item.trades for item in valid)
    avg_sharpe = float(np.mean(np.asarray([item.sharpe for item in valid], dtype=np.float64)))
    avg_ret = float(np.mean(np.asarray([item.total_return for item in valid], dtype=np.float64)))
    worst_dd = max(item.max_drawdown for item in valid)
    avg_turnover = float(
        np.mean(np.asarray([item.avg_turnover for item in valid], dtype=np.float64))
    )

    report = {
        "features": str(feature_path),
        "rows": n,
        "config": {
            "train_window": int(args.train_window),
            "test_window": int(args.test_window),
            "step_window": int(args.step_window),
            "fee_bps": float(args.fee_bps),
            "slippage_bps": float(args.slippage_bps),
            "signal_threshold": float(args.signal_threshold),
            "max_leverage": float(args.max_leverage),
            "pred_scale": float(args.pred_scale),
            "interval_minutes": int(args.interval_minutes),
        },
        "summary": {
            "split_count": len(split_results),
            "valid_split_count": len(valid),
            "total_bars": total_bars,
            "total_trades": total_trades,
            "avg_split_return": avg_ret,
            "avg_split_sharpe": avg_sharpe,
            "worst_split_max_drawdown": worst_dd,
            "avg_turnover": avg_turnover,
        },
        "splits": [
            {
                "split_index": item.split_index,
                "train_start": item.train_start,
                "train_end": item.train_end,
                "test_start": item.test_start,
                "test_end": item.test_end,
                "bars": item.bars,
                "trades": item.trades,
                "avg_turnover": item.avg_turnover,
                "total_return": item.total_return,
                "sharpe": item.sharpe,
                "max_drawdown": item.max_drawdown,
            }
            for item in split_results
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] walk-forward report: {output_path}")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
