#!/usr/bin/env python3
"""Fetch a closed-bar Bybit OHLCV history with a single-venue data contract."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import pathlib
import time
from dataclasses import dataclass
from typing import Callable, Dict, List
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


def utc_ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def parse_utc_date(raw: str) -> dt.datetime:
    return dt.datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)


def interval_ms(interval: str) -> int:
    value = int(interval)
    if value <= 0:
        raise ValueError("interval must be positive minutes")
    return value * 60_000


def request_server_time_ms(*, base_url: str, timeout_sec: float) -> int:
    endpoint = f"{base_url.rstrip('/')}/v5/market/time"
    request = Request(
        endpoint, headers={"User-Agent": "ai-trade-bybit-history/1.0"}
    )
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"Bybit HTTP error: status={exc.code}, body={body}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Bybit network error: {exc}") from exc
    if int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(
            "Bybit API error: "
            f"retCode={payload.get('retCode')}, retMsg={payload.get('retMsg')}"
        )
    result = payload.get("result", {})
    candidates = [(payload.get("time"), 1.0)]
    if isinstance(result, dict):
        candidates.extend(
            [
                (result.get("timeNano"), 1.0 / 1_000_000.0),
                (result.get("timeSecond"), 1000.0),
            ]
        )
    for value, multiplier in candidates:
        try:
            parsed = int(int(value) * multiplier)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    raise RuntimeError("Bybit server time response is missing a valid timestamp")


def resolve_closed_end_ms(
    *,
    requested_end_ms: int,
    server_time_ms: int,
    bar_ms: int,
) -> tuple[int, int]:
    if requested_end_ms <= 0 or server_time_ms <= 0 or bar_ms <= 0:
        raise ValueError("closed history boundary inputs must be positive")
    server_boundary_ms = server_time_ms - (server_time_ms % bar_ms)
    return min(requested_end_ms, server_boundary_ms), server_boundary_ms


def request_page(
    *,
    base_url: str,
    category: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int,
    timeout_sec: float,
) -> List[Candle]:
    params = {
        "category": category,
        "symbol": symbol,
        "interval": interval,
        "start": str(start_ms),
        "end": str(end_ms),
        "limit": str(limit),
    }
    endpoint = f"{base_url.rstrip('/')}/v5/market/kline?{urlencode(params)}"
    request = Request(
        endpoint, headers={"User-Agent": "ai-trade-bybit-history/1.0"}
    )
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"Bybit HTTP error: status={exc.code}, body={body}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Bybit network error: {exc}") from exc

    if int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(
            "Bybit API error: "
            f"retCode={payload.get('retCode')}, retMsg={payload.get('retMsg')}"
        )
    candles: List[Candle] = []
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
        if (
            candle.open <= 0.0
            or candle.high <= 0.0
            or candle.low <= 0.0
            or candle.close <= 0.0
            or candle.volume < 0.0
            or candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
        ):
            raise RuntimeError(
                f"invalid Bybit OHLCV row at {candle.timestamp_ms}"
            )
        candles.append(candle)
    return candles


def collect_history(
    *,
    start_ms: int,
    end_ms_exclusive: int,
    bar_ms: int,
    page_limit: int,
    request: Callable[[int, int, int], List[Candle]],
    sleep_sec: float = 0.0,
) -> tuple[List[Candle], int]:
    if start_ms >= end_ms_exclusive:
        raise ValueError("history start must precede end")
    by_timestamp: Dict[int, Candle] = {}
    cursor_end = end_ms_exclusive - 1
    pages = 0
    expected_pages = max(
        1,
        math.ceil((end_ms_exclusive - start_ms) / bar_ms / page_limit),
    )
    max_pages = expected_pages + 5
    while cursor_end >= start_ms:
        pages += 1
        if pages > max_pages:
            raise RuntimeError("Bybit history pagination exceeded safety bound")
        page = request(start_ms, cursor_end, page_limit)
        eligible = [
            candle
            for candle in page
            if start_ms <= candle.timestamp_ms < end_ms_exclusive
            and candle.timestamp_ms + bar_ms <= end_ms_exclusive
        ]
        for candle in eligible:
            by_timestamp[candle.timestamp_ms] = candle
        if not page:
            break
        oldest = min(candle.timestamp_ms for candle in page)
        if oldest <= start_ms:
            break
        next_cursor = oldest - 1
        if next_cursor >= cursor_end:
            raise RuntimeError("Bybit history pagination did not advance")
        cursor_end = next_cursor
        if sleep_sec > 0.0:
            time.sleep(sleep_sec)
    return sorted(by_timestamp.values(), key=lambda item: item.timestamp_ms), pages


def write_csv(path: pathlib.Path, candles: List[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for candle in candles:
            writer.writerow(
                [
                    candle.timestamp_ms,
                    f"{candle.open:.8f}",
                    f"{candle.high:.8f}",
                    f"{candle.low:.8f}",
                    f"{candle.close:.8f}",
                    f"{candle.volume:.8f}",
                ]
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Bybit historical klines")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="5")
    parser.add_argument("--category", default="linear")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--base-url", default="https://api.bybit.com")
    parser.add_argument("--page-limit", type=int, default=1000)
    parser.add_argument("--timeout-sec", type=float, default=15.0)
    parser.add_argument("--sleep-sec", type=float, default=0.02)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    requested_end = parse_utc_date(args.end_date) if args.end_date else now
    bar_ms = interval_ms(args.interval)
    server_time_ms = request_server_time_ms(
        base_url=args.base_url,
        timeout_sec=args.timeout_sec,
    )
    requested_end_ms = utc_ms(requested_end)
    closed_end_ms, server_closed_boundary_ms = resolve_closed_end_ms(
        requested_end_ms=requested_end_ms,
        server_time_ms=server_time_ms,
        bar_ms=bar_ms,
    )
    effective_end = dt.datetime.fromtimestamp(
        closed_end_ms / 1000.0,
        tz=dt.timezone.utc,
    )
    start = (
        parse_utc_date(args.start_date)
        if args.start_date
        else effective_end - dt.timedelta(days=max(1, args.days))
    )
    start_ms = utc_ms(start)
    limit = max(1, min(1000, args.page_limit))

    def fetch(page_start_ms: int, page_end_ms: int, page_limit: int) -> List[Candle]:
        return request_page(
            base_url=args.base_url,
            category=args.category,
            symbol=args.symbol.upper(),
            interval=str(int(args.interval)),
            start_ms=page_start_ms,
            end_ms=page_end_ms,
            limit=page_limit,
            timeout_sec=args.timeout_sec,
        )

    candles, pages = collect_history(
        start_ms=start_ms,
        end_ms_exclusive=closed_end_ms,
        bar_ms=bar_ms,
        page_limit=limit,
        request=fetch,
        sleep_sec=max(0.0, args.sleep_sec),
    )
    if not candles:
        raise RuntimeError("Bybit history returned no closed candles")
    write_csv(pathlib.Path(args.output), candles)
    summary = {
        "status": "PASS",
        "provider": "bybit",
        "venue": "bybit",
        "category": args.category,
        "base_url": args.base_url.rstrip("/"),
        "price_type": "trade_price",
        "volume_unit": "base_asset",
        "bar_semantics": "closed_ohlcv",
        "symbol": args.symbol.upper(),
        "interval_minutes": int(args.interval),
        "start_ms": start_ms,
        "requested_end_ms": requested_end_ms,
        "server_time_ms": server_time_ms,
        "closed_boundary_ms": server_closed_boundary_ms,
        "end_ms_exclusive": closed_end_ms,
        "page_count": pages,
        "rows_total": len(candles),
        "first_timestamp": candles[0].timestamp_ms,
        "last_timestamp": candles[-1].timestamp_ms,
        "output": args.output,
    }
    report = pathlib.Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
