#!/usr/bin/env python3
"""Backfill Bybit public trade archives into causal 5-minute flow bars."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import pathlib
import ssl
import tempfile
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence
from urllib.request import Request, urlopen


SCHEMA_VERSION = "bybit_trade_history_v1"
DEFAULT_BASE_URL = "https://public.bybit.com/trading"


@dataclass
class TradeFlowBar:
    timestamp_ms: int
    trade_count: int = 0
    buy_quote_volume: float = 0.0
    sell_quote_volume: float = 0.0
    large_trade_quote_volume: float = 0.0
    first_price: float = 0.0
    last_price: float = 0.0

    def add(self, *, side: str, size: float, price: float, large_threshold: float) -> None:
        quote = size * price
        if self.trade_count == 0:
            self.first_price = price
        self.last_price = price
        self.trade_count += 1
        if side == "Buy":
            self.buy_quote_volume += quote
        elif side == "Sell":
            self.sell_quote_volume += quote
        else:
            raise ValueError(f"unsupported trade side: {side}")
        if quote >= large_threshold:
            self.large_trade_quote_volume += quote


def parse_date(raw: str) -> dt.date:
    return dt.datetime.strptime(raw, "%Y-%m-%d").date()


def iter_dates(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    if start > end:
        raise ValueError("start date must be <= end date")
    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)


def build_archive_url(*, base_url: str, symbol: str, day: dt.date) -> str:
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol cannot be empty")
    return f"{base_url.rstrip('/')}/{symbol}/{symbol}{day.isoformat()}.csv.gz"


def download_bytes(url: str, timeout_sec: float) -> bytes:
    request = Request(url, headers={"User-Agent": "ai-trade-bybit-trades/1.0"})
    paths = ssl.get_default_verify_paths()
    fallback = pathlib.Path("/etc/ssl/cert.pem")
    context = (
        ssl.create_default_context(cafile=str(fallback))
        if paths.cafile is None and fallback.is_file()
        else ssl.create_default_context()
    )
    with urlopen(request, timeout=timeout_sec, context=context) as response:
        return response.read()


def parse_trade_archive(
    blob: bytes, *, interval_ms: int, large_trade_quote_threshold: float
) -> Dict[int, TradeFlowBar]:
    if interval_ms <= 0 or large_trade_quote_threshold <= 0.0:
        raise ValueError("interval and large trade threshold must be positive")
    bars: Dict[int, TradeFlowBar] = {}
    with gzip.GzipFile(fileobj=io.BytesIO(blob), mode="rb") as compressed:
        text = io.TextIOWrapper(compressed, encoding="utf-8", newline="")
        reader = csv.DictReader(text)
        required = {"timestamp", "symbol", "side", "size", "price"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("Bybit trade archive is missing required columns")
        previous_timestamp_ms = -1
        for row in reader:
            try:
                timestamp_ms = int(float(row["timestamp"]) * 1000.0)
                side = row["side"]
                size = float(row["size"])
                price = float(row["price"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid Bybit trade row") from exc
            if timestamp_ms < previous_timestamp_ms:
                raise ValueError("Bybit trade timestamps are not monotonic")
            previous_timestamp_ms = timestamp_ms
            if timestamp_ms <= 0 or size <= 0.0 or price <= 0.0:
                raise ValueError("Bybit trade values must be positive")
            bucket = timestamp_ms - timestamp_ms % interval_ms
            bar = bars.setdefault(bucket, TradeFlowBar(timestamp_ms=bucket))
            bar.add(
                side=side,
                size=size,
                price=price,
                large_threshold=large_trade_quote_threshold,
            )
    if not bars:
        raise ValueError("Bybit trade archive is empty")
    return bars


def load_anchor_timestamps(path: pathlib.Path, interval_ms: int) -> List[int]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if "timestamp" not in (reader.fieldnames or []):
            raise ValueError("anchor OHLCV CSV is missing timestamp")
        values = [int(row["timestamp"]) for row in reader]
    if len(values) < 2 or any(
        right - left != interval_ms for left, right in zip(values, values[1:])
    ):
        raise ValueError("anchor OHLCV timestamp axis is not contiguous")
    return values


def select_anchor_range(
    timestamps: Sequence[int], start: dt.date, end: dt.date
) -> List[int]:
    start_ms = int(dt.datetime.combine(start, dt.time(), dt.timezone.utc).timestamp() * 1000)
    end_exclusive_ms = int(
        dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), dt.timezone.utc).timestamp()
        * 1000
    )
    selected = [value for value in timestamps if start_ms <= value < end_exclusive_ms]
    if not selected:
        raise ValueError("requested Bybit trade dates do not overlap anchor axis")
    return selected


def write_flow_csv(
    path: pathlib.Path,
    *,
    timestamps: Sequence[int],
    bars: Dict[int, TradeFlowBar],
) -> Dict[str, int]:
    missing = [timestamp for timestamp in timestamps if timestamp not in bars]
    if missing:
        raise ValueError(
            f"Bybit trade-flow coverage failed closed: missing_count={len(missing)} "
            f"sample={missing[:5]}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "bybit_trade_count",
                "bybit_buy_quote_volume",
                "bybit_sell_quote_volume",
                "bybit_trade_imbalance",
                "bybit_large_trade_quote_fraction",
                "bybit_trade_return",
            ]
        )
        for timestamp in timestamps:
            bar = bars[timestamp]
            total = bar.buy_quote_volume + bar.sell_quote_volume
            writer.writerow(
                [
                    timestamp,
                    bar.trade_count,
                    f"{bar.buy_quote_volume:.12g}",
                    f"{bar.sell_quote_volume:.12g}",
                    f"{(bar.buy_quote_volume - bar.sell_quote_volume) / total:.12g}",
                    f"{bar.large_trade_quote_volume / total:.12g}",
                    f"{bar.last_price / bar.first_price - 1.0:.12g}",
                ]
            )
    temporary.replace(path)
    return {
        "row_count": len(timestamps),
        "missing_count": 0,
        "first_timestamp": int(timestamps[0]),
        "last_timestamp": int(timestamps[-1]),
    }


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ohlcv-csv", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--interval-ms", type=int, default=300000)
    parser.add_argument("--large-trade-quote-threshold", type=float, default=10000.0)
    parser.add_argument("--timeout-sec", type=float, default=60.0)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--research-domain", default="development", choices=("development",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.research_domain != "development":
        raise ValueError("Bybit trade history is development-only")
    if args.symbol.strip().upper() != "SOLUSDT" or args.interval_ms != 300000:
        raise ValueError("only the audited SOLUSDT 5m contract is supported")
    start, end = parse_date(args.start_date), parse_date(args.end_date)
    anchor_path = pathlib.Path(args.ohlcv_csv)
    output_path = pathlib.Path(args.output)
    report_path = pathlib.Path(args.report)
    cache_dir = pathlib.Path(args.cache_dir)
    selected = select_anchor_range(
        load_anchor_timestamps(anchor_path, args.interval_ms), start, end
    )
    merged: Dict[int, TradeFlowBar] = {}
    archives = []
    for day in iter_dates(start, end):
        url = build_archive_url(base_url=args.base_url, symbol=args.symbol, day=day)
        cache_path = cache_dir / args.symbol.upper() / url.rsplit("/", 1)[-1]
        if args.refresh or not cache_path.is_file():
            blob = download_bytes(url, args.timeout_sec)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=cache_path.parent, delete=False) as handle:
                temporary = pathlib.Path(handle.name)
                handle.write(blob)
            temporary.replace(cache_path)
        else:
            blob = cache_path.read_bytes()
        daily = parse_trade_archive(
            blob,
            interval_ms=args.interval_ms,
            large_trade_quote_threshold=args.large_trade_quote_threshold,
        )
        for timestamp, bar in daily.items():
            if timestamp in merged:
                raise ValueError(f"duplicate Bybit trade-flow bar: {timestamp}")
            merged[timestamp] = bar
        archives.append(
            {
                "date": day.isoformat(),
                "url": url,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
                "bar_count": len(daily),
            }
        )
    quality = write_flow_csv(output_path, timestamps=selected, bars=merged)
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "research_domain": "development_only",
        "promotion_evidence": False,
        "symbol": "SOLUSDT",
        "interval_ms": args.interval_ms,
        "availability_lag_ms": args.interval_ms,
        "timestamp_semantics": "trade_time floored to bar open; usable only after bar close",
        "large_trade_quote_threshold": args.large_trade_quote_threshold,
        "quality": quality,
        "anchor": {"path": str(anchor_path), "sha256": sha256_file(anchor_path)},
        "output": {"path": str(output_path), "sha256": sha256_file(output_path)},
        "archives": archives,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "PASS", **quality}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
