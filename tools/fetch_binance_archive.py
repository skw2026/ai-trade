#!/usr/bin/env python3
"""
Download Binance archive kline files and normalize to:
timestamp,open,high,low,close,volume
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import pathlib
import time
import zipfile
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence
from urllib.error import HTTPError, URLError
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


def log_warn(message: str) -> None:
    print(f"[WARN] {message}")


def parse_date_ymd(raw: str) -> dt.date:
    return dt.datetime.strptime(raw, "%Y-%m-%d").date()


def iter_dates(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def build_daily_archive_url(
    *,
    base_url: str,
    market: str,
    symbol: str,
    interval: str,
    day: dt.date,
) -> str:
    symbol_u = symbol.upper()
    interval_u = interval
    date_text = day.strftime("%Y-%m-%d")
    if market == "futures_um":
        path = (
            f"data/futures/um/daily/klines/{symbol_u}/{interval_u}/"
            f"{symbol_u}-{interval_u}-{date_text}.zip"
        )
    elif market == "futures_cm":
        path = (
            f"data/futures/cm/daily/klines/{symbol_u}/{interval_u}/"
            f"{symbol_u}-{interval_u}-{date_text}.zip"
        )
    elif market == "spot":
        path = (
            f"data/spot/daily/klines/{symbol_u}/{interval_u}/"
            f"{symbol_u}-{interval_u}-{date_text}.zip"
        )
    else:
        raise ValueError(f"unsupported market: {market}")
    return f"{base_url.rstrip('/')}/{path}"


def download_bytes(url: str, timeout_sec: float) -> bytes:
    req = Request(url, headers={"User-Agent": "ai-trade-archive-fetcher/1.0"})
    with urlopen(req, timeout=timeout_sec) as resp:
        return resp.read()


def parse_archive_zip(blob: bytes) -> List[Candle]:
    candles: List[Candle] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        if not names:
            return candles
        with zf.open(names[0], "r") as fp:
            text = fp.read().decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split(",")
        if len(fields) < 6:
            continue
        if not fields[0].strip().isdigit():
            # header row
            continue
        try:
            candles.append(
                Candle(
                    timestamp_ms=int(fields[0]),
                    open=float(fields[1]),
                    high=float(fields[2]),
                    low=float(fields[3]),
                    close=float(fields[4]),
                    volume=float(fields[5]),
                )
            )
        except ValueError:
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
            except (KeyError, ValueError):
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


def normalize_interval(raw: str) -> str:
    value = raw.strip()
    if value in {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}:
        return value
    raise ValueError(f"unsupported interval: {raw}")


def resolve_date_range(
    *, start_date: str | None, end_date: str | None, days: int
) -> tuple[dt.date, dt.date]:
    today = dt.datetime.utcnow().date()
    if end_date:
        end = parse_date_ymd(end_date)
    else:
        end = today
    if start_date:
        start = parse_date_ymd(start_date)
    else:
        days_value = max(1, int(days))
        start = end - dt.timedelta(days=days_value - 1)
    if start > end:
        raise ValueError("start_date must be <= end_date")
    return start, end


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Binance archive daily klines")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="5m")
    parser.add_argument(
        "--market",
        default="futures_um",
        choices=["futures_um", "futures_cm", "spot"],
    )
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--base-url", default="https://data.binance.vision")
    parser.add_argument("--timeout-sec", type=float, default=20.0)
    parser.add_argument("--sleep-ms", type=int, default=60)
    parser.add_argument("--output", default="./data/research/ohlcv_5m.csv")
    parser.add_argument("--report", default="")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    interval = normalize_interval(args.interval)
    output_path = pathlib.Path(args.output)
    start, end = resolve_date_range(
        start_date=args.start_date, end_date=args.end_date, days=args.days
    )

    if args.overwrite:
        merged: Dict[int, Candle] = {}
    else:
        merged = read_csv(output_path)
    existing_before = len(merged)

    requested_days = 0
    downloaded_days = 0
    missing_days = 0
    rows_added = 0

    for day in iter_dates(start, end):
        requested_days += 1
        url = build_daily_archive_url(
            base_url=args.base_url,
            market=args.market,
            symbol=args.symbol,
            interval=interval,
            day=day,
        )
        try:
            blob = download_bytes(url, timeout_sec=float(args.timeout_sec))
        except HTTPError as exc:
            if exc.code == 404:
                missing_days += 1
                log_warn(f"archive missing: {day} ({url})")
                continue
            raise
        except URLError:
            missing_days += 1
            log_warn(f"archive unavailable: {day} ({url})")
            continue
        except Exception as exc:
            missing_days += 1
            log_warn(f"archive download failed: {day}, err={exc}")
            continue

        candles = parse_archive_zip(blob)
        if not candles:
            log_warn(f"archive empty: {day} ({url})")
        else:
            downloaded_days += 1
        for c in candles:
            if c.timestamp_ms not in merged:
                rows_added += 1
            merged[c.timestamp_ms] = c

        sleep_sec = max(0, int(args.sleep_ms)) / 1000.0
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    all_candles = sorted(merged.values(), key=lambda item: item.timestamp_ms)
    write_csv(output_path, all_candles)

    summary = {
        "symbol": args.symbol.upper(),
        "market": args.market,
        "interval": interval,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "requested_days": requested_days,
        "downloaded_days": downloaded_days,
        "missing_days": missing_days,
        "existing_rows_before": existing_before,
        "rows_added": rows_added,
        "rows_total": len(all_candles),
        "output": str(output_path),
    }
    if args.report:
        report_path = pathlib.Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False))
    if len(all_candles) == 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
