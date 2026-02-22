#!/usr/bin/env python3
"""
Detect and fill missing kline timestamps for a local OHLCV CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import time
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def normalize_interval_minutes(raw: str) -> int:
    value = raw.strip()
    if value.isdigit() and int(value) > 0:
        return int(value)
    raise ValueError(f"invalid interval minutes: {raw}")


def read_csv(path: pathlib.Path) -> Dict[int, Candle]:
    if not path.exists():
        return {}
    out: Dict[int, Candle] = {}
    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            try:
                candle = Candle(
                    timestamp_ms=int(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            except (ValueError, KeyError):
                continue
            out[candle.timestamp_ms] = candle
    return out


def write_csv(path: pathlib.Path, candles: Sequence[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            writer.writerow(
                [
                    c.timestamp_ms,
                    f"{c.open:.8f}",
                    f"{c.high:.8f}",
                    f"{c.low:.8f}",
                    f"{c.close:.8f}",
                    f"{c.volume:.8f}",
                ]
            )


def detect_missing_timestamps(timestamps: Sequence[int], interval_ms: int) -> List[int]:
    if not timestamps:
        return []
    ordered = sorted(set(int(item) for item in timestamps))
    missing: List[int] = []
    prev = ordered[0]
    for current in ordered[1:]:
        expected = prev + interval_ms
        while expected < current:
            missing.append(expected)
            expected += interval_ms
        prev = current
    return missing


def group_missing_ranges(missing: Sequence[int], interval_ms: int) -> List[Tuple[int, int]]:
    if not missing:
        return []
    ordered = sorted(missing)
    ranges: List[Tuple[int, int]] = []
    start = ordered[0]
    end = ordered[0]
    for ts in ordered[1:]:
        if ts == end + interval_ms:
            end = ts
            continue
        ranges.append((start, end))
        start = ts
        end = ts
    ranges.append((start, end))
    return ranges


def request_bybit_range(
    *,
    base_url: str,
    category: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    timeout_sec: float,
) -> List[Candle]:
    params = {
        "category": category,
        "symbol": symbol.upper(),
        "interval": interval,
        "start": str(start_ms),
        "end": str(end_ms),
        "limit": "1000",
    }
    endpoint = f"{base_url.rstrip('/')}/v5/market/kline?{urlencode(params)}"
    req = Request(endpoint, headers={"User-Agent": "ai-trade-gap-fill/1.0"})
    with urlopen(req, timeout=timeout_sec) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(
            f"Bybit API error: retCode={payload.get('retCode')}, retMsg={payload.get('retMsg')}"
        )
    out: List[Candle] = []
    for row in payload.get("result", {}).get("list", []):
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            candle = Candle(
                timestamp_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        except (TypeError, ValueError):
            continue
        if start_ms <= candle.timestamp_ms <= end_ms:
            out.append(candle)
    out.sort(key=lambda item: item.timestamp_ms)
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill missing OHLCV timestamp gaps")
    parser.add_argument("--input", required=True, help="input OHLCV csv")
    parser.add_argument("--output", default="", help="output csv, default overwrite input")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="5", help="minutes")
    parser.add_argument("--category", default="linear")
    parser.add_argument("--base-url", default="https://api.bybit.com")
    parser.add_argument("--timeout-sec", type=float, default=12.0)
    parser.add_argument("--sleep-ms", type=int, default=50)
    parser.add_argument("--max-ranges", type=int, default=200)
    parser.add_argument("--strict", action="store_true", help="fail when any gap remains")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = pathlib.Path(args.input)
    output_path = pathlib.Path(args.output) if args.output else input_path
    interval_min = normalize_interval_minutes(args.interval)
    interval_ms = interval_min * 60 * 1000

    candles_by_ts = read_csv(input_path)
    if not candles_by_ts:
        print("[WARN] no rows in input csv, skip gap fill")
        return 0

    missing = detect_missing_timestamps(sorted(candles_by_ts.keys()), interval_ms)
    ranges = group_missing_ranges(missing, interval_ms)
    if len(ranges) > int(args.max_ranges):
        ranges = ranges[: int(args.max_ranges)]

    fetched = 0
    failed_ranges = 0
    if not args.dry_run:
        for start_ms, end_ms in ranges:
            try:
                rows = request_bybit_range(
                    base_url=args.base_url,
                    category=args.category,
                    symbol=args.symbol,
                    interval=str(interval_min),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    timeout_sec=float(args.timeout_sec),
                )
            except (RuntimeError, HTTPError, URLError) as exc:
                failed_ranges += 1
                print(f"[WARN] gap fetch failed: start={start_ms}, end={end_ms}, err={exc}")
                continue
            for candle in rows:
                if candle.timestamp_ms not in candles_by_ts:
                    candles_by_ts[candle.timestamp_ms] = candle
                    fetched += 1
            sleep_sec = max(0, int(args.sleep_ms)) / 1000.0
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        ordered = sorted(candles_by_ts.values(), key=lambda item: item.timestamp_ms)
        write_csv(output_path, ordered)

    remaining_missing = detect_missing_timestamps(sorted(candles_by_ts.keys()), interval_ms)
    summary = {
        "input": str(input_path),
        "output": str(output_path),
        "symbol": args.symbol.upper(),
        "interval_minutes": interval_min,
        "missing_before": len(missing),
        "missing_ranges_before": len(group_missing_ranges(missing, interval_ms)),
        "processed_ranges": len(ranges),
        "failed_ranges": failed_ranges,
        "fetched_rows": fetched,
        "missing_after": len(remaining_missing),
        "dry_run": bool(args.dry_run),
    }
    if args.report:
        report_path = pathlib.Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))

    if args.strict and summary["missing_after"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
