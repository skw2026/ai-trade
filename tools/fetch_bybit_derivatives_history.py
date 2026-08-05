#!/usr/bin/env python3
"""Fetch public Bybit derivatives context and align it to a closed-bar CSV."""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import pathlib
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Point:
    timestamp_ms: int
    values: Tuple[float, ...]


def request_json(url: str, timeout_sec: float, attempts: int = 4) -> Dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        request = Request(
            url,
            headers={"User-Agent": "ai-trade-derivatives-history/1.0"},
        )
        try:
            with urlopen(request, timeout=timeout_sec) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if int(payload.get("retCode", -1)) != 0:
                raise RuntimeError(
                    f"Bybit API error retCode={payload.get('retCode')} "
                    f"retMsg={payload.get('retMsg')}"
                )
            return payload
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < max(1, attempts):
                time.sleep(min(2.0, 0.25 * (2**attempt)))
    raise RuntimeError(f"Bybit request failed after retries: {url}: {last_error}")


def collect_backward(
    *,
    start_ms: int,
    end_ms: int,
    page_limit: int,
    request_page: Callable[[int, int, int], Sequence[Point]],
    expected_step_ms: int,
    sleep_sec: float,
) -> Tuple[List[Point], int]:
    """Collect descending API pages without trusting opaque cursor stability."""
    if start_ms > end_ms or page_limit <= 0 or expected_step_ms <= 0:
        raise ValueError("invalid collection range")
    values: Dict[int, Point] = {}
    cursor_end = int(end_ms)
    span_count = max(1, math.ceil((end_ms - start_ms + 1) / expected_step_ms))
    max_pages = math.ceil(span_count / page_limit) + 8
    pages = 0
    while cursor_end >= start_ms:
        pages += 1
        if pages > max_pages:
            raise RuntimeError("pagination exceeded safety bound")
        page = list(request_page(start_ms, cursor_end, page_limit))
        eligible = [point for point in page if start_ms <= point.timestamp_ms <= end_ms]
        for point in eligible:
            values[point.timestamp_ms] = point
        if not page:
            break
        oldest = min(point.timestamp_ms for point in page)
        if oldest <= start_ms:
            break
        next_end = oldest - 1
        if next_end >= cursor_end:
            raise RuntimeError("pagination did not advance")
        cursor_end = next_end
        if sleep_sec > 0.0:
            time.sleep(sleep_sec)
    return sorted(values.values(), key=lambda item: item.timestamp_ms), pages


def parse_dict_points(
    rows: Iterable[Any],
    *,
    timestamp_key: str,
    value_keys: Sequence[str],
) -> List[Point]:
    points: List[Point] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            timestamp_ms = int(row[timestamp_key])
            values = tuple(float(row[key]) for key in value_keys)
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp_ms > 0 and all(math.isfinite(value) for value in values):
            points.append(Point(timestamp_ms, values))
    return points


def parse_premium_points(rows: Iterable[Any]) -> List[Point]:
    points: List[Point] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            timestamp_ms = int(row[0])
            close = float(row[4])
        except (TypeError, ValueError):
            continue
        if timestamp_ms > 0 and math.isfinite(close):
            points.append(Point(timestamp_ms, (close,)))
    return points


def fetch_time_series(
    *,
    base_url: str,
    endpoint: str,
    fixed_params: Dict[str, str],
    start_ms: int,
    end_ms: int,
    limit: int,
    expected_step_ms: int,
    parse_rows: Callable[[Iterable[Any]], List[Point]],
    timeout_sec: float,
    sleep_sec: float,
) -> Tuple[List[Point], int]:
    def request_page(page_start: int, page_end: int, page_limit: int) -> Sequence[Point]:
        params = dict(fixed_params)
        params.update(
            {
                "startTime" if endpoint not in {"premium-index-price-kline"} else "start": str(page_start),
                "endTime" if endpoint not in {"premium-index-price-kline"} else "end": str(page_end),
                "limit": str(page_limit),
            }
        )
        url = f"{base_url.rstrip('/')}/v5/market/{endpoint}?{urlencode(params)}"
        payload = request_json(url, timeout_sec)
        return parse_rows(payload.get("result", {}).get("list", []))

    return collect_backward(
        start_ms=start_ms,
        end_ms=end_ms,
        page_limit=limit,
        request_page=request_page,
        expected_step_ms=expected_step_ms,
        sleep_sec=sleep_sec,
    )


def load_bar_timestamps(path: pathlib.Path) -> List[int]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if "timestamp" not in (reader.fieldnames or []):
            raise ValueError("OHLCV CSV missing timestamp")
        timestamps = [int(row["timestamp"]) for row in reader]
    if len(timestamps) < 2 or any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("OHLCV timestamps must be strictly increasing")
    return timestamps


def asof_value(
    points: Sequence[Point],
    timestamps: Sequence[int],
    query_timestamp: int,
) -> Tuple[float, ...] | None:
    index = bisect.bisect_right(timestamps, query_timestamp) - 1
    return points[index].values if index >= 0 else None


def write_aligned_csv(
    *,
    path: pathlib.Path,
    bar_timestamps: Sequence[int],
    premium: Sequence[Point],
    open_interest: Sequence[Point],
    account_ratio: Sequence[Point],
    funding: Sequence[Point],
    slow_publication_delay_ms: int,
) -> Dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    premium_ts = [point.timestamp_ms for point in premium]
    # OI and account-ratio points are conservatively delayed by one source
    # period so a value is never used before its recording window has closed.
    oi_effective = [
        Point(point.timestamp_ms + slow_publication_delay_ms, point.values)
        for point in open_interest
    ]
    ratio_effective = [
        Point(point.timestamp_ms + slow_publication_delay_ms, point.values)
        for point in account_ratio
    ]
    oi_ts = [point.timestamp_ms for point in oi_effective]
    ratio_ts = [point.timestamp_ms for point in ratio_effective]
    funding_ts = [point.timestamp_ms for point in funding]
    missing = {"premium": 0, "open_interest": 0, "account_ratio": 0, "funding": 0}
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "premium_index_close",
                "open_interest",
                "long_account_ratio",
                "short_account_ratio",
                "funding_rate",
            ]
        )
        for timestamp in bar_timestamps:
            premium_value = asof_value(premium, premium_ts, timestamp)
            oi_value = asof_value(oi_effective, oi_ts, timestamp)
            ratio_value = asof_value(ratio_effective, ratio_ts, timestamp)
            funding_value = asof_value(funding, funding_ts, timestamp)
            items = {
                "premium": premium_value,
                "open_interest": oi_value,
                "account_ratio": ratio_value,
                "funding": funding_value,
            }
            for name, value in items.items():
                if value is None:
                    missing[name] += 1
            writer.writerow(
                [
                    timestamp,
                    "" if premium_value is None else f"{premium_value[0]:.12g}",
                    "" if oi_value is None else f"{oi_value[0]:.12g}",
                    "" if ratio_value is None else f"{ratio_value[0]:.12g}",
                    "" if ratio_value is None else f"{ratio_value[1]:.12g}",
                    "" if funding_value is None else f"{funding_value[0]:.12g}",
                ]
            )
    return missing


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ohlcv_csv", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--category", default="linear")
    parser.add_argument("--slow_period", default="1h")
    parser.add_argument("--base_url", default="https://api.bybit.com")
    parser.add_argument("--timeout_sec", type=float, default=15.0)
    parser.add_argument("--sleep_sec", type=float, default=0.04)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.slow_period != "1h":
        raise ValueError("only the audited 1h slow period is supported")
    bar_timestamps = load_bar_timestamps(pathlib.Path(args.ohlcv_csv))
    five_minutes_ms = 300_000
    one_hour_ms = 3_600_000
    # Fetch one slow period before the first bar so as-of alignment can seed the
    # first row without pulling information from the future.
    start_ms = int(bar_timestamps[0]) - one_hour_ms
    end_ms = int(bar_timestamps[-1])
    common = {"category": args.category, "symbol": args.symbol.upper()}
    premium, premium_pages = fetch_time_series(
        base_url=args.base_url,
        endpoint="premium-index-price-kline",
        fixed_params={**common, "interval": "5"},
        start_ms=start_ms,
        end_ms=end_ms,
        limit=1000,
        expected_step_ms=five_minutes_ms,
        parse_rows=parse_premium_points,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    open_interest, oi_pages = fetch_time_series(
        base_url=args.base_url,
        endpoint="open-interest",
        fixed_params={**common, "intervalTime": args.slow_period},
        start_ms=start_ms,
        end_ms=end_ms,
        limit=200,
        expected_step_ms=one_hour_ms,
        parse_rows=lambda rows: parse_dict_points(
            rows, timestamp_key="timestamp", value_keys=("openInterest",)
        ),
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    account_ratio, ratio_pages = fetch_time_series(
        base_url=args.base_url,
        endpoint="account-ratio",
        fixed_params={**common, "period": args.slow_period},
        start_ms=start_ms,
        end_ms=end_ms,
        limit=500,
        expected_step_ms=one_hour_ms,
        parse_rows=lambda rows: parse_dict_points(
            rows,
            timestamp_key="timestamp",
            value_keys=("buyRatio", "sellRatio"),
        ),
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    funding, funding_pages = fetch_time_series(
        base_url=args.base_url,
        endpoint="funding/history",
        fixed_params=common,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=200,
        expected_step_ms=8 * one_hour_ms,
        parse_rows=lambda rows: parse_dict_points(
            rows,
            timestamp_key="fundingRateTimestamp",
            value_keys=("fundingRate",),
        ),
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    series = {
        "premium": premium,
        "open_interest": open_interest,
        "account_ratio": account_ratio,
        "funding": funding,
    }
    empty = [name for name, points in series.items() if not points]
    if empty:
        raise RuntimeError(f"empty derivative series: {','.join(empty)}")
    missing = write_aligned_csv(
        path=pathlib.Path(args.output),
        bar_timestamps=bar_timestamps,
        premium=premium,
        open_interest=open_interest,
        account_ratio=account_ratio,
        funding=funding,
        slow_publication_delay_ms=one_hour_ms,
    )
    report = {
        "schema_version": "bybit_derivatives_history_v1",
        "status": "PASS",
        "provider": "bybit",
        "category": args.category,
        "symbol": args.symbol.upper(),
        "bar_start_ms": bar_timestamps[0],
        "bar_end_ms": bar_timestamps[-1],
        "bar_count": len(bar_timestamps),
        "causality": {
            "premium_alignment": "asof_at_closed_5m_bar_timestamp",
            "open_interest_publication_delay_ms": one_hour_ms,
            "account_ratio_publication_delay_ms": one_hour_ms,
            "funding_alignment": "asof_at_settlement_timestamp",
        },
        "series": {
            name: {
                "point_count": len(points),
                "start_ms": points[0].timestamp_ms,
                "end_ms": points[-1].timestamp_ms,
            }
            for name, points in series.items()
        },
        "pages": {
            "premium": premium_pages,
            "open_interest": oi_pages,
            "account_ratio": ratio_pages,
            "funding": funding_pages,
        },
        "aligned_missing_rows": missing,
        "output": str(pathlib.Path(args.output)),
    }
    report_path = pathlib.Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
