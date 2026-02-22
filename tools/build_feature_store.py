#!/usr/bin/env python3
"""
Build a lightweight feature store CSV from OHLCV data.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
from typing import Dict, List

import numpy as np


def ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (float(span) + 1.0)
    out = np.empty_like(values, dtype=np.float64)
    out[:] = np.nan
    if values.size == 0:
        return out
    out[0] = values[0]
    for i in range(1, values.size):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if window <= 0 or values.size < window:
        return out
    finite = np.isfinite(values)
    safe_values = np.where(finite, values, 0.0)
    cumsum = np.cumsum(np.insert(safe_values, 0, 0.0))
    ccount = np.cumsum(np.insert(finite.astype(np.int64), 0, 0))
    window_sum = cumsum[window:] - cumsum[:-window]
    window_count = ccount[window:] - ccount[:-window]
    valid = window_count == window
    out_slice = out[window - 1 :]
    out_slice[valid] = window_sum[valid] / float(window)
    return out


def rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    mean = rolling_mean(values, window)
    sq_mean = rolling_mean(values * values, window)
    out = np.full(values.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(mean) & np.isfinite(sq_mean)
    var = np.maximum(0.0, sq_mean - mean * mean)
    out[valid] = np.sqrt(var[valid])
    return out


def shift_return(close: np.ndarray, bars: int) -> np.ndarray:
    out = np.full(close.shape, np.nan, dtype=np.float64)
    if bars <= 0 or close.size <= bars:
        return out
    out[bars:] = close[bars:] / close[:-bars] - 1.0
    return out


def forward_return(close: np.ndarray, bars: int) -> np.ndarray:
    out = np.full(close.shape, np.nan, dtype=np.float64)
    if bars <= 0 or close.size <= bars:
        return out
    out[:-bars] = close[bars:] / close[:-bars] - 1.0
    return out


def load_ohlcv(path: pathlib.Path) -> Dict[str, np.ndarray]:
    ts: List[int] = []
    op: List[float] = []
    hi: List[float] = []
    lo: List[float] = []
    cl: List[float] = []
    vol: List[float] = []
    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            try:
                ts.append(int(row["timestamp"]))
                op.append(float(row["open"]))
                hi.append(float(row["high"]))
                lo.append(float(row["low"]))
                cl.append(float(row["close"]))
                vol.append(float(row["volume"]))
            except (KeyError, ValueError):
                continue
    return {
        "timestamp": np.asarray(ts, dtype=np.int64),
        "open": np.asarray(op, dtype=np.float64),
        "high": np.asarray(hi, dtype=np.float64),
        "low": np.asarray(lo, dtype=np.float64),
        "close": np.asarray(cl, dtype=np.float64),
        "volume": np.asarray(vol, dtype=np.float64),
    }


def build_features(data: Dict[str, np.ndarray], forward_bars: int) -> Dict[str, np.ndarray]:
    close = data["close"]
    high = data["high"]
    low = data["low"]
    volume = data["volume"]

    ret_1 = shift_return(close, 1)
    ret_3 = shift_return(close, 3)
    ret_12 = shift_return(close, 12)
    ema_fast = ema(close, 12)
    ema_slow = ema(close, 48)
    ema_diff = np.where(close != 0.0, (ema_fast - ema_slow) / close, np.nan)
    vol_12 = rolling_std(ret_1, 12)
    vol_48 = rolling_std(ret_1, 48)
    mean_48 = rolling_mean(close, 48)
    std_48 = rolling_std(close, 48)
    zscore_48 = np.where(std_48 > 0.0, (close - mean_48) / std_48, np.nan)
    mom_12 = shift_return(close, 12)
    mom_48 = shift_return(close, 48)
    range_pct = np.where(close != 0.0, (high - low) / close, np.nan)
    vol_chg_12 = np.where(volume > 0.0, shift_return(volume, 12), np.nan)
    fwd = forward_return(close, max(1, int(forward_bars)))

    return {
        "timestamp": data["timestamp"],
        "close": close,
        "volume": volume,
        "ret_1": ret_1,
        "ret_3": ret_3,
        "ret_12": ret_12,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema_diff": ema_diff,
        "vol_12": vol_12,
        "vol_48": vol_48,
        "zscore_48": zscore_48,
        "mom_12": mom_12,
        "mom_48": mom_48,
        "range_pct": range_pct,
        "vol_chg_12": vol_chg_12,
        "forward_return": fwd,
    }


def write_feature_csv(
    path: pathlib.Path,
    features: Dict[str, np.ndarray],
    drop_na: bool,
) -> int:
    ordered_cols = [
        "timestamp",
        "close",
        "volume",
        "ret_1",
        "ret_3",
        "ret_12",
        "ema_fast",
        "ema_slow",
        "ema_diff",
        "vol_12",
        "vol_48",
        "zscore_48",
        "mom_12",
        "mom_48",
        "range_pct",
        "vol_chg_12",
        "forward_return",
    ]
    row_count = int(features["timestamp"].size)
    kept = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(ordered_cols)
        for i in range(row_count):
            row = []
            valid = True
            for col in ordered_cols:
                value = features[col][i]
                if col == "timestamp":
                    row.append(str(int(value)))
                    continue
                if not math.isfinite(float(value)):
                    valid = False
                row.append(f"{float(value):.10f}")
            if drop_na and not valid:
                continue
            writer.writerow(row)
            kept += 1
    return kept


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build feature store from OHLCV csv")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--forward-bars", type=int, default=12)
    parser.add_argument("--keep-na", action="store_true")
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = pathlib.Path(args.input)
    output_path = pathlib.Path(args.output)
    data = load_ohlcv(input_path)
    total_rows = int(data["timestamp"].size)
    if total_rows == 0:
        print("[ERROR] input csv has no valid rows")
        return 2

    feature_map = build_features(data, forward_bars=max(1, int(args.forward_bars)))
    kept_rows = write_feature_csv(output_path, feature_map, drop_na=not args.keep_na)

    summary = {
        "input": str(input_path),
        "output": str(output_path),
        "rows_input": total_rows,
        "rows_output": kept_rows,
        "forward_bars": max(1, int(args.forward_bars)),
        "drop_na": not args.keep_na,
    }
    if args.report:
        report_path = pathlib.Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if kept_rows > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
