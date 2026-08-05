#!/usr/bin/env python3
"""Fetch and align Binance cross-venue/cross-asset 5m market-alpha history.

The output is intentionally bound to an existing development OHLCV axis.  A
source bar is usable only after its close_time, and every requested source must
cover every anchor row; partial data fails closed instead of being forward
filled.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import ssl
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.request import Request, urlopen


SCHEMA_VERSION = "market_alpha_history_v1"
DEFAULT_BASE_URL = "https://data.binance.vision"
DEFAULT_SYMBOLS = ("SOLUSDT", "BTCUSDT", "ETHUSDT")
OUTPUT_FIELDS = (
    "close",
    "quote_volume",
    "trade_count",
    "taker_buy_quote_volume",
)


@dataclass(frozen=True)
class AggregateBar:
    open_time_ms: int
    close_time_ms: int
    close: float
    quote_volume: float
    trade_count: int
    taker_buy_quote_volume: float


@dataclass(frozen=True)
class ArchiveArtifact:
    symbol: str
    month: str
    url: str
    checksum_url: str
    sha256: str
    bytes: int
    rows: int
    bars: Tuple[AggregateBar, ...]


def parse_timestamp_ms(raw: str) -> int:
    value = int(raw)
    # Binance spot archives switched to microseconds in 2025. Futures archives
    # are still milliseconds, but normalizing here keeps the parser explicit.
    if value >= 10**15:
        value //= 1000
    return value


def parse_archive_zip(blob: bytes) -> List[AggregateBar]:
    bars: List[AggregateBar] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise ValueError(f"zip CRC failure: {bad_member}")
        members = [name for name in archive.namelist() if not name.endswith("/")]
        if len(members) != 1:
            raise ValueError("archive must contain exactly one data file")
        with archive.open(members[0], "r") as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
            reader = csv.reader(text)
            for row in reader:
                if not row or not row[0].strip().isdigit() or len(row) < 11:
                    continue
                try:
                    bar = AggregateBar(
                        open_time_ms=parse_timestamp_ms(row[0]),
                        close_time_ms=parse_timestamp_ms(row[6]),
                        close=float(row[4]),
                        quote_volume=float(row[7]),
                        trade_count=int(row[8]),
                        taker_buy_quote_volume=float(row[10]),
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid Binance kline row: {row[:11]}") from exc
                if (
                    bar.open_time_ms <= 0
                    or bar.close_time_ms < bar.open_time_ms
                    or bar.close <= 0.0
                    or bar.quote_volume < 0.0
                    or bar.trade_count < 0
                    or not 0.0 <= bar.taker_buy_quote_volume <= bar.quote_volume + 1e-6
                ):
                    raise ValueError(f"invalid Binance kline values: {bar}")
                bars.append(bar)
    bars.sort(key=lambda item: item.open_time_ms)
    if any(
        right.open_time_ms <= left.open_time_ms
        for left, right in zip(bars, bars[1:])
    ):
        raise ValueError("archive timestamps must be strictly increasing")
    return bars


def iter_months(start_ms: int, end_ms: int) -> Iterable[str]:
    if start_ms > end_ms:
        raise ValueError("start timestamp must be <= end timestamp")
    start = dt.datetime.fromtimestamp(start_ms / 1000.0, dt.timezone.utc)
    end = dt.datetime.fromtimestamp(end_ms / 1000.0, dt.timezone.utc)
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            year += 1
            month = 1


def build_monthly_archive_url(
    *, base_url: str, symbol: str, interval: str, month: str
) -> str:
    symbol = symbol.strip().upper()
    if not symbol or interval != "5m":
        raise ValueError("non-empty symbol and interval=5m are required")
    return (
        f"{base_url.rstrip('/')}/data/futures/um/monthly/klines/"
        f"{symbol}/{interval}/{symbol}-{interval}-{month}.zip"
    )


def parse_checksum(text: str, expected_filename: str) -> str:
    fields = text.strip().split()
    if len(fields) < 2 or fields[-1].lstrip("*") != expected_filename:
        raise ValueError("checksum filename does not match archive")
    digest = fields[0].lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("invalid SHA-256 checksum")
    return digest


def download_bytes(url: str, timeout_sec: float) -> bytes:
    request = Request(url, headers={"User-Agent": "ai-trade-market-alpha/1.0"})
    paths = ssl.get_default_verify_paths()
    fallback = pathlib.Path("/etc/ssl/cert.pem")
    context = (
        ssl.create_default_context(cafile=str(fallback))
        if paths.cafile is None and fallback.is_file()
        else ssl.create_default_context()
    )
    with urlopen(request, timeout=timeout_sec, context=context) as response:
        return response.read()


def atomic_write(path: pathlib.Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = pathlib.Path(handle.name)
        handle.write(payload)
    temporary.replace(path)


def fetch_archive(
    *,
    symbol: str,
    month: str,
    interval: str,
    base_url: str,
    cache_dir: pathlib.Path,
    timeout_sec: float,
    refresh: bool,
) -> ArchiveArtifact:
    url = build_monthly_archive_url(
        base_url=base_url, symbol=symbol, interval=interval, month=month
    )
    checksum_url = f"{url}.CHECKSUM"
    filename = url.rsplit("/", 1)[-1]
    cached_zip = cache_dir / symbol.upper() / filename
    cached_checksum = cached_zip.with_suffix(cached_zip.suffix + ".CHECKSUM")
    if refresh or not cached_zip.is_file() or not cached_checksum.is_file():
        blob = download_bytes(url, timeout_sec)
        checksum_blob = download_bytes(checksum_url, timeout_sec)
        atomic_write(cached_zip, blob)
        atomic_write(cached_checksum, checksum_blob)
    else:
        blob = cached_zip.read_bytes()
        checksum_blob = cached_checksum.read_bytes()
    expected = parse_checksum(checksum_blob.decode("utf-8"), filename)
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise ValueError(
            f"archive checksum mismatch for {symbol} {month}: "
            f"expected={expected} actual={actual}"
        )
    bars = parse_archive_zip(blob)
    if not bars:
        raise ValueError(f"empty archive for {symbol} {month}")
    return ArchiveArtifact(
        symbol=symbol.upper(),
        month=month,
        url=url,
        checksum_url=checksum_url,
        sha256=actual,
        bytes=len(blob),
        rows=len(bars),
        bars=tuple(bars),
    )


def load_anchor_axis(path: pathlib.Path, interval_ms: int) -> Tuple[List[int], List[float]]:
    timestamps: List[int] = []
    closes: List[float] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "close"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("anchor OHLCV CSV must contain timestamp and close")
        for row in reader:
            timestamps.append(int(row["timestamp"]))
            closes.append(float(row["close"]))
    if len(timestamps) < 2:
        raise ValueError("anchor OHLCV CSV requires at least two rows")
    if any(right - left != interval_ms for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("anchor OHLCV axis is not contiguous at the configured interval")
    if any(value <= 0.0 for value in closes):
        raise ValueError("anchor close values must be positive")
    return timestamps, closes


def merge_artifacts(
    artifacts: Sequence[ArchiveArtifact],
) -> Dict[str, Dict[int, AggregateBar]]:
    merged: Dict[str, Dict[int, AggregateBar]] = {}
    for artifact in artifacts:
        destination = merged.setdefault(artifact.symbol, {})
        for bar in artifact.bars:
            existing = destination.get(bar.open_time_ms)
            if existing is not None and existing != bar:
                raise ValueError(
                    f"conflicting duplicate bar: {artifact.symbol} {bar.open_time_ms}"
                )
            destination[bar.open_time_ms] = bar
    return merged


def validate_and_write_aligned(
    *,
    output_path: pathlib.Path,
    timestamps: Sequence[int],
    bars_by_symbol: Mapping[str, Mapping[int, AggregateBar]],
    symbols: Sequence[str],
    interval_ms: int,
) -> Dict[str, object]:
    missing: Dict[str, List[int]] = {}
    late_close: Dict[str, List[int]] = {}
    for symbol in symbols:
        source = bars_by_symbol.get(symbol, {})
        missing[symbol] = [timestamp for timestamp in timestamps if timestamp not in source]
        late_close[symbol] = [
            timestamp
            for timestamp in timestamps
            if timestamp in source
            and source[timestamp].close_time_ms >= timestamp + interval_ms
        ]
    failures = {
        symbol: {
            "missing_count": len(missing[symbol]),
            "missing_sample": missing[symbol][:5],
            "invalid_close_time_count": len(late_close[symbol]),
            "invalid_close_time_sample": late_close[symbol][:5],
        }
        for symbol in symbols
        if missing[symbol] or late_close[symbol]
    }
    if failures:
        raise ValueError(f"market alpha coverage failed closed: {failures}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=output_path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        writer = csv.writer(handle)
        header = ["timestamp"]
        for symbol in symbols:
            prefix = symbol.lower().replace("usdt", "")
            header.extend(f"binance_{prefix}_{field}" for field in OUTPUT_FIELDS)
        writer.writerow(header)
        for timestamp in timestamps:
            row: List[object] = [timestamp]
            for symbol in symbols:
                bar = bars_by_symbol[symbol][timestamp]
                row.extend(
                    [
                        f"{bar.close:.12g}",
                        f"{bar.quote_volume:.12g}",
                        bar.trade_count,
                        f"{bar.taker_buy_quote_volume:.12g}",
                    ]
                )
            writer.writerow(row)
    temporary.replace(output_path)
    return {
        "row_count": len(timestamps),
        "first_timestamp": int(timestamps[0]),
        "last_timestamp": int(timestamps[-1]),
        "missing_by_symbol": {symbol: 0 for symbol in symbols},
        "availability_lag_ms": interval_ms,
        "timestamp_semantics": "bar_open_time; fields become usable after open_time+interval",
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
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--interval", default="5m", choices=("5m",))
    parser.add_argument("--interval-ms", type=int, default=300000)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--research-domain", default="development", choices=("development",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.research_domain != "development":
        raise ValueError("market alpha history is development-only")
    symbols = tuple(
        dict.fromkeys(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    )
    if symbols != DEFAULT_SYMBOLS:
        raise ValueError(f"symbols must be exactly {','.join(DEFAULT_SYMBOLS)}")
    if args.interval_ms != 300000:
        raise ValueError("only the audited 5m/300000ms contract is supported")
    anchor_path = pathlib.Path(args.ohlcv_csv)
    output_path = pathlib.Path(args.output)
    report_path = pathlib.Path(args.report)
    cache_dir = pathlib.Path(args.cache_dir)
    timestamps, _ = load_anchor_axis(anchor_path, args.interval_ms)
    months = tuple(iter_months(timestamps[0], timestamps[-1]))
    jobs = [(symbol, month) for symbol in symbols for month in months]
    artifacts: List[ArchiveArtifact] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 12))) as executor:
        futures = {
            executor.submit(
                fetch_archive,
                symbol=symbol,
                month=month,
                interval=args.interval,
                base_url=args.base_url,
                cache_dir=cache_dir,
                timeout_sec=args.timeout_sec,
                refresh=args.refresh,
            ): (symbol, month)
            for symbol, month in jobs
        }
        for future in as_completed(futures):
            artifacts.append(future.result())
    artifacts.sort(key=lambda item: (item.symbol, item.month))
    quality = validate_and_write_aligned(
        output_path=output_path,
        timestamps=timestamps,
        bars_by_symbol=merge_artifacts(artifacts),
        symbols=symbols,
        interval_ms=args.interval_ms,
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "research_domain": "development_only",
        "promotion_evidence": False,
        "source": "binance_public_data_futures_um_monthly_klines",
        "source_base_url": args.base_url,
        "symbols": list(symbols),
        "interval": args.interval,
        "quality": quality,
        "anchor": {
            "path": str(anchor_path),
            "sha256": sha256_file(anchor_path),
        },
        "output": {
            "path": str(output_path),
            "sha256": sha256_file(output_path),
        },
        "archives": [
            {
                "symbol": item.symbol,
                "month": item.month,
                "url": item.url,
                "checksum_url": item.checksum_url,
                "sha256": item.sha256,
                "bytes": item.bytes,
                "rows": item.rows,
            }
            for item in artifacts
        ],
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
