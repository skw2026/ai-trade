#!/usr/bin/env python3
"""Fetch exact-joined Bybit/Binance perpetual bars and funding settlements."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import pathlib
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import fetch_bybit_carry_history as bybit_carry
import fetch_bybit_derivatives_history as bybit
import run_cross_venue_information_set_experiment as common


SCHEMA_VERSION = "cross_venue_funding_history_v1"
PARENT_AUDIT_SCHEMA_VERSION = "funding_basis_carry_frozen_audit_v1"
BAR_FIELDS = ("open", "high", "low", "close", "volume", "turnover")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_parent_range(path: pathlib.Path) -> Tuple[int, int, Dict[str, Any]]:
    payload = common.read_json(path)
    unsigned = {key: value for key, value in payload.items() if key != "identity_sha256"}
    frozen = payload.get("frozen_domain")
    if not (
        payload.get("schema_version") == PARENT_AUDIT_SCHEMA_VERSION
        and payload.get("identity_sha256") == common.canonical_sha256(unsigned)
        and isinstance(frozen, Mapping)
        and isinstance(payload.get("primary_splits"), list)
        and isinstance(payload.get("boundary_splits"), Mapping)
    ):
        raise ValueError("parent carry audit manifest is not verifiable")
    start_ms = int(frozen["start_ms"])
    end_exclusive_ms = int(frozen["end_ms"])
    if start_ms <= 0 or end_exclusive_ms - start_ms < 120 * 86_400_000:
        raise ValueError("parent carry frozen domain is invalid")
    return start_ms, end_exclusive_ms - 300_000, payload


def request_binance_json(url: str, timeout_sec: float, attempts: int = 4) -> Any:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        request = Request(url, headers={"User-Agent": "ai-trade-cross-venue-funding/1.0"})
        try:
            with urlopen(request, timeout=timeout_sec) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, Mapping) and "code" in payload:
                raise RuntimeError(
                    f"Binance API error code={payload.get('code')} msg={payload.get('msg')}"
                )
            return payload
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < max(1, attempts):
                time.sleep(min(2.0, 0.25 * (2**attempt)))
    raise RuntimeError(f"Binance request failed after retries: {url}: {last_error}")


def parse_binance_kline_points(rows: Iterable[Any], *, mark: bool) -> List[bybit.Point]:
    points: List[bybit.Point] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < (5 if mark else 8):
            continue
        try:
            timestamp_ms = int(row[0])
            prices = tuple(float(row[index]) for index in range(1, 5))
            values = prices if mark else prices + (float(row[5]), float(row[7]))
        except (TypeError, ValueError):
            continue
        if timestamp_ms > 0 and all(math.isfinite(value) and value >= 0.0 for value in values):
            if all(value > 0.0 for value in prices):
                points.append(bybit.Point(timestamp_ms, values))
    return points


def collect_binance_forward(
    *,
    base_url: str,
    endpoint: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    timeout_sec: float,
    sleep_sec: float,
    mark: bool,
) -> Tuple[List[bybit.Point], int]:
    values: Dict[int, bybit.Point] = {}
    cursor = int(start_ms)
    pages = 0
    max_pages = math.ceil((end_ms - start_ms + 300_000) / (1500 * 300_000)) + 4
    while cursor <= end_ms:
        pages += 1
        if pages > max_pages:
            raise RuntimeError("Binance kline pagination exceeded safety bound")
        params = {
            "symbol": symbol,
            "interval": "5m",
            "startTime": str(cursor),
            "endTime": str(end_ms),
            "limit": "1500",
        }
        payload = request_binance_json(
            f"{base_url.rstrip('/')}{endpoint}?{urlencode(params)}", timeout_sec
        )
        if not isinstance(payload, list):
            raise RuntimeError("Binance kline response is not a list")
        page = parse_binance_kline_points(payload, mark=mark)
        eligible = [point for point in page if start_ms <= point.timestamp_ms <= end_ms]
        for point in eligible:
            values[point.timestamp_ms] = point
        if not page:
            break
        newest = max(point.timestamp_ms for point in page)
        if newest < cursor:
            raise RuntimeError("Binance kline pagination did not advance")
        cursor = newest + 300_000
        if len(payload) < 1500:
            break
        if sleep_sec > 0.0:
            time.sleep(sleep_sec)
    return sorted(values.values(), key=lambda point: point.timestamp_ms), pages


def fetch_binance_funding(
    *,
    base_url: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    timeout_sec: float,
    sleep_sec: float,
) -> Tuple[List[bybit.Point], int]:
    values: Dict[int, bybit.Point] = {}
    cursor = int(start_ms)
    pages = 0
    max_pages = 16
    while cursor <= end_ms:
        pages += 1
        if pages > max_pages:
            raise RuntimeError("Binance funding pagination exceeded safety bound")
        params = {
            "symbol": symbol,
            "startTime": str(cursor),
            "endTime": str(end_ms),
            "limit": "1000",
        }
        payload = request_binance_json(
            f"{base_url.rstrip('/')}/fapi/v1/fundingRate?{urlencode(params)}",
            timeout_sec,
        )
        if not isinstance(payload, list):
            raise RuntimeError("Binance funding response is not a list")
        page: List[bybit.Point] = []
        for row in payload:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("rateType") or "Regular") != "Regular":
                continue
            try:
                timestamp_ms = int(row["fundingTime"])
                rate = float(row["fundingRate"])
                mark_price = float(row["markPrice"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                timestamp_ms > 0
                and math.isfinite(rate)
                and math.isfinite(mark_price)
                and mark_price > 0.0
            ):
                page.append(bybit.Point(timestamp_ms, (rate, mark_price)))
        for point in page:
            if start_ms <= point.timestamp_ms <= end_ms:
                values[point.timestamp_ms] = point
        if not payload:
            break
        raw_timestamps = [
            int(row["fundingTime"])
            for row in payload
            if isinstance(row, Mapping) and "fundingTime" in row
        ]
        if not raw_timestamps:
            break
        newest = max(raw_timestamps)
        if newest < cursor:
            raise RuntimeError("Binance funding pagination did not advance")
        cursor = newest + 1
        if len(payload) < 1000:
            break
        if sleep_sec > 0.0:
            time.sleep(sleep_sec)
    return sorted(values.values(), key=lambda point: point.timestamp_ms), pages


def write_joined_csv(
    path: pathlib.Path,
    bybit_perpetual: Sequence[bybit.Point],
    bybit_mark: Sequence[bybit.Point],
    bybit_funding: Sequence[bybit.Point],
    binance_perpetual: Sequence[bybit.Point],
    binance_mark: Sequence[bybit.Point],
    binance_funding: Sequence[bybit.Point],
) -> Dict[str, Any]:
    bybit_perp_by_ts = {point.timestamp_ms: point.values for point in bybit_perpetual}
    bybit_mark_by_ts = {point.timestamp_ms: point.values for point in bybit_mark}
    binance_perp_by_ts = {point.timestamp_ms: point.values for point in binance_perpetual}
    binance_mark_by_ts = {point.timestamp_ms: point.values for point in binance_mark}
    def bucket_events(
        points: Sequence[bybit.Point], venue: str
    ) -> Dict[int, bybit.Point]:
        events: Dict[int, bybit.Point] = {}
        for point in points:
            bucket = (int(point.timestamp_ms) // 300_000) * 300_000
            if bucket in events:
                raise ValueError(f"multiple {venue} funding events share one 5m bucket")
            events[bucket] = point
        return events

    bybit_funding_by_bucket = bucket_events(bybit_funding, "Bybit")
    binance_funding_by_bucket = bucket_events(binance_funding, "Binance")
    timestamps = sorted(
        set(bybit_perp_by_ts)
        .intersection(bybit_mark_by_ts)
        .intersection(binance_perp_by_ts)
        .intersection(binance_mark_by_ts)
    )
    if len(timestamps) < 2:
        raise ValueError("cross-venue exact inner join is empty")
    timestamp_set = set(timestamps)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["timestamp"]
            + [f"bybit_perpetual_{field}" for field in BAR_FIELDS]
            + [f"bybit_mark_{field}" for field in ("open", "high", "low", "close")]
            + [f"binance_perpetual_{field}" for field in BAR_FIELDS]
            + [f"binance_mark_{field}" for field in ("open", "high", "low", "close")]
            + [
                "bybit_funding_rate",
                "bybit_funding_mark",
                "bybit_funding_timestamp",
                "binance_funding_rate",
                "binance_funding_mark",
                "binance_funding_timestamp",
            ]
        )
        for timestamp in timestamps:
            bybit_event = bybit_funding_by_bucket.get(timestamp)
            binance_event = binance_funding_by_bucket.get(timestamp)
            writer.writerow(
                [timestamp]
                + [f"{value:.12g}" for value in bybit_perp_by_ts[timestamp]]
                + [f"{value:.12g}" for value in bybit_mark_by_ts[timestamp]]
                + [f"{value:.12g}" for value in binance_perp_by_ts[timestamp]]
                + [f"{value:.12g}" for value in binance_mark_by_ts[timestamp]]
                + [
                    "" if bybit_event is None else f"{bybit_event.values[0]:.12g}",
                    "" if bybit_event is None else f"{bybit_mark_by_ts[timestamp][0]:.12g}",
                    "" if bybit_event is None else str(int(bybit_event.timestamp_ms)),
                    "" if binance_event is None else f"{binance_event.values[0]:.12g}",
                    "" if binance_event is None else f"{binance_event.values[1]:.12g}",
                    "" if binance_event is None else str(int(binance_event.timestamp_ms)),
                ]
            )
    steps = [right - left for left, right in zip(timestamps, timestamps[1:])]
    return {
        "row_count": len(timestamps),
        "start_ms": timestamps[0],
        "end_ms": timestamps[-1],
        "contiguous_step_count": sum(step == 300_000 for step in steps),
        "gap_count": sum(step != 300_000 for step in steps),
        "bybit_funding_event_count": sum(
            ts in bybit_funding_by_bucket for ts in timestamps
        ),
        "binance_funding_event_count": sum(
            ts in binance_funding_by_bucket for ts in timestamps
        ),
        "bybit_unmatched_funding_event_count": sum(
            bucket not in timestamp_set for bucket in bybit_funding_by_bucket
        ),
        "binance_unmatched_funding_event_count": sum(
            bucket not in timestamp_set for bucket in binance_funding_by_bucket
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-audit-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--bybit-base-url", default="https://api.bybit.com")
    parser.add_argument("--binance-base-url", default="https://fapi.binance.com")
    parser.add_argument("--timeout-sec", type=float, default=15.0)
    parser.add_argument("--sleep-sec", type=float, default=0.04)
    return parser.parse_args()


def run(args: argparse.Namespace) -> Dict[str, Any]:
    parent_path = pathlib.Path(args.parent_audit_manifest).resolve()
    output = pathlib.Path(args.output).resolve()
    start_ms, end_ms, parent = load_parent_range(parent_path)
    symbol = str(args.symbol).upper()
    bybit_perpetual, bybit_perpetual_pages = bybit_carry.fetch_klines(
        base_url=args.bybit_base_url,
        category="linear",
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    bybit_mark, bybit_mark_pages = bybit_carry.fetch_mark_klines(
        base_url=args.bybit_base_url,
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    bybit_funding, bybit_funding_pages = bybit_carry.fetch_funding(
        base_url=args.bybit_base_url,
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    binance_perpetual, binance_perpetual_pages = collect_binance_forward(
        base_url=args.binance_base_url,
        endpoint="/fapi/v1/klines",
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
        mark=False,
    )
    binance_mark, binance_mark_pages = collect_binance_forward(
        base_url=args.binance_base_url,
        endpoint="/fapi/v1/markPriceKlines",
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
        mark=True,
    )
    binance_funding, binance_funding_pages = fetch_binance_funding(
        base_url=args.binance_base_url,
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    if not all(
        (
            bybit_perpetual,
            bybit_mark,
            bybit_funding,
            binance_perpetual,
            binance_mark,
            binance_funding,
        )
    ):
        raise RuntimeError("cross-venue funding source returned an empty series")
    joined = write_joined_csv(
        output,
        bybit_perpetual,
        bybit_mark,
        bybit_funding,
        binance_perpetual,
        binance_mark,
        binance_funding,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "symbol": symbol,
        "venues": ["bybit", "binance"],
        "requested": {"start_ms": start_ms, "end_ms": end_ms},
        "source": {
            "bybit_perpetual_bar_count": len(bybit_perpetual),
            "bybit_mark_bar_count": len(bybit_mark),
            "bybit_funding_event_count": len(bybit_funding),
            "binance_perpetual_bar_count": len(binance_perpetual),
            "binance_mark_bar_count": len(binance_mark),
            "binance_funding_event_count": len(binance_funding),
            "bybit_perpetual_pages": bybit_perpetual_pages,
            "bybit_mark_pages": bybit_mark_pages,
            "bybit_funding_pages": bybit_funding_pages,
            "binance_perpetual_pages": binance_perpetual_pages,
            "binance_mark_pages": binance_mark_pages,
            "binance_funding_pages": binance_funding_pages,
        },
        "joined": joined,
        "causality": {
            "bar_alignment": "four_series_exact_timestamp_inner_join",
            "funding_alignment": "exact_venue_settlement_timestamp_once_only",
            "bybit_funding_mark_source": "linear_mark_price_kline_open_at_settlement",
            "binance_funding_mark_source": "funding_history_associated_mark_price",
            "original_funding_event_timestamp_preserved": True,
            "asof_funding_fill": False,
        },
        "parent_audit_manifest": {
            "path": str(parent_path),
            "sha256": sha256_file(parent_path),
            "identity_sha256": parent["identity_sha256"],
        },
        "output": str(output),
        "output_sha256": sha256_file(output),
    }


def main() -> int:
    args = parse_args()
    report_path = pathlib.Path(args.report)
    try:
        report = run(args)
    except Exception as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "NOT_READY",
            "reason_codes": [f"{type(exc).__name__}:{exc}"],
            "output": str(pathlib.Path(args.output).resolve()),
        }
    atomic_write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
