#!/usr/bin/env python3
"""Generate the immutable Python-side golden vectors for C++ feature parity."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import pathlib
import sys

import numpy as np


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "integrator_train_for_parity", ROOT / "tools" / "integrator_train.py"
)
assert SPEC and SPEC.loader
TRAIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TRAIN
SPEC.loader.exec_module(TRAIN)

EXPRESSIONS = {
    "ret_1": "ts_delta(close,1)/(abs(ts_delay(close,1))+1e-9)",
    "ret_3": "ts_delta(close,3)/(abs(ts_delay(close,3))+1e-9)",
    "vol_delta_1": "ts_delta(volume,1)",
    "rsi_14": "rsi(close,14)",
    "macd_line": "ema(close,12)-ema(close,26)",
    "macd_signal": "ema(ema(close,12)-ema(close,26),9)",
    "macd_hist": "(ema(close,12)-ema(close,26))-ema(ema(close,12)-ema(close,26),9)",
    "rank_12": "ts_rank(close,12)",
    "corr_12": "ts_corr(close,volume,12)",
}
CHECKPOINTS = (48, 96, 128)


def build_bars(count: int = 128) -> list[dict[str, float | int]]:
    rows: list[dict[str, float | int]] = []
    previous_close = 100.0
    for index in range(count):
        close = (
            100.0
            + 0.05 * index
            + 2.0 * math.sin(index / 7.0)
            + 0.8 * math.cos(index / 3.0)
        )
        open_price = previous_close + 0.15 * math.sin(index / 4.0)
        high = max(open_price, close) + 0.25 + 0.03 * (index % 5)
        low = min(open_price, close) - 0.25 - 0.02 * (index % 7)
        volume = 1000.0 + 5.0 * index + 100.0 * math.sin(index / 5.0)
        rows.append(
            {
                "timestamp": 1_700_000_000_000 + index * 300_000,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
        previous_close = close
    return rows


def generate(output_dir: pathlib.Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    bars = build_bars()
    bars_path = output_dir / "feature_parity_bars_v1.csv"
    with bars_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        for row in bars:
            writer.writerow(
                {
                    key: (
                        str(value)
                        if key == "timestamp"
                        else f"{float(value):.15g}"
                    )
                    for key, value in row.items()
                }
            )

    series = {
        name: np.asarray([float(row[name]) for row in bars], dtype=np.float64)
        for name in ("open", "high", "low", "close", "volume")
    }
    evaluator = TRAIN.SafeExpressionEvaluator(series)
    close = series["close"]
    volume = series["volume"]
    macd_line = TRAIN.ema(close, 12) - TRAIN.ema(close, 26)
    macd_signal = TRAIN.ema(macd_line, 9)
    values = {
        "ret_1": TRAIN.ts_delta(close, 1)
        / (np.abs(TRAIN.ts_delay(close, 1)) + 1e-9),
        "ret_3": TRAIN.ts_delta(close, 3)
        / (np.abs(TRAIN.ts_delay(close, 3)) + 1e-9),
        "vol_delta_1": TRAIN.ts_delta(volume, 1),
        "rsi_14": TRAIN.rsi(close, 14),
        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_line - macd_signal,
        "rank_12": evaluator.evaluate("ts_rank(close,12)"),
        "corr_12": evaluator.evaluate("ts_corr(close,volume,12)"),
    }
    expected_path = output_dir / "feature_parity_expected_v1.tsv"
    with expected_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(
            ["sample_count", "feature", "expression", "expected_value", "tolerance"]
        )
        for checkpoint in CHECKPOINTS:
            for name, expression in EXPRESSIONS.items():
                expected = float(values[name][checkpoint - 1])
                if not math.isfinite(expected):
                    raise ValueError(
                        f"non-finite parity fixture: {checkpoint}/{name}"
                    )
                writer.writerow(
                    [checkpoint, name, expression, f"{expected:.17g}", "1e-9"]
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "tools" / "fixtures"),
    )
    args = parser.parse_args()
    generate(pathlib.Path(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
