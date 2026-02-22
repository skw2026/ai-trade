#!/usr/bin/env python3
"""
Incremental market data updater.

The script keeps the interface as "stream", but uses exchange REST polling so it
can run in cron/scheduler without extra websocket dependencies.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import time
from dataclasses import dataclass
from typing import Dict, List, Sequence
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


def log_info(message: str) -> None:
    print(f"[INFO] {message}")


def normalize_interval(raw: str) -> str:
    value = raw.strip().upper()
    if value.isdigit() and int(value) > 0:
        return str(int(value))
    raise ValueError(f"invalid interval minutes: {raw}")


def request_bybit_latest_klines(
    *,
    base_url: str,
    category: str,
    symbol: str,
    interval: str,
    bars: int,
    timeout_sec: float,
) -> List[Candle]:
    params = {
        "category": category,
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": str(max(1, min(1000, int(bars)))),
    }
    endpoint = f"{base_url.rstrip('/')}/v5/market/kline?{urlencode(params)}"
    req = Request(endpoint, headers={"User-Agent": "ai-trade-incremental-updater/1.0"})
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"HTTP error: status={exc.code}, body={body}") from exc
    except URLError as exc:
        raise RuntimeError(f"network error: {exc}") from exc

    if int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(
            f"Bybit API error: retCode={payload.get('retCode')}, retMsg={payload.get('retMsg')}"
        )

    candles: List[Candle] = []
    for row in payload.get("result", {}).get("list", []):
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            candles.append(
                Candle(
                    timestamp_ms=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda item: item.timestamp_ms)
    return candles


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


def merge_candles(existing: Dict[int, Candle], incoming: Sequence[Candle]) -> int:
    added = 0
    for candle in incoming:
        if candle.timestamp_ms not in existing:
            added += 1
        existing[candle.timestamp_ms] = candle
    return added


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incremental market kline updater")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="5", help="Bybit interval minutes")
    parser.add_argument("--category", default="linear")
    parser.add_argument("--bars", type=int, default=240)
    parser.add_argument("--output", default="./data/research/ohlcv_5m.csv")
    parser.add_argument("--iterations", type=int, default=1, help="0 means run forever")
    parser.add_argument("--sleep-sec", type=float, default=5.0)
    parser.add_argument("--timeout-sec", type=float, default=12.0)
    parser.add_argument("--base-url", default="https://api.bybit.com")
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    interval = normalize_interval(args.interval)
    output_path = pathlib.Path(args.output)

    existing = read_csv(output_path)
    loops = 0
    total_added = 0
    total_seen = len(existing)
    last_ts_before = max(existing.keys()) if existing else None

    while True:
        loops += 1
        candles = request_bybit_latest_klines(
            base_url=args.base_url,
            category=args.category,
            symbol=args.symbol,
            interval=interval,
            bars=max(1, int(args.bars)),
            timeout_sec=float(args.timeout_sec),
        )
        added = merge_candles(existing, candles)
        total_added += added
        sorted_candles = sorted(existing.values(), key=lambda item: item.timestamp_ms)
        write_csv(output_path, sorted_candles)
        total_seen = len(sorted_candles)
        newest_ts = sorted_candles[-1].timestamp_ms if sorted_candles else None
        log_info(
            f"loop={loops} added={added} total={total_seen} newest_ts={newest_ts}"
        )

        if args.iterations > 0 and loops >= int(args.iterations):
            break
        sleep_sec = max(0.1, float(args.sleep_sec))
        time.sleep(sleep_sec)

    summary = {
        "symbol": args.symbol.upper(),
        "interval": interval,
        "category": args.category,
        "loops": loops,
        "rows_total": total_seen,
        "rows_added": total_added,
        "last_timestamp_before": last_ts_before,
        "last_timestamp_after": (max(existing.keys()) if existing else None),
        "output": str(output_path),
    }
    if args.report:
        report_path = pathlib.Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
