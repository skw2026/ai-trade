#!/usr/bin/env python3
"""Fetch exact-inner-joined Bybit spot/perpetual bars and funding settlements."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import pathlib
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import urlencode

import fetch_bybit_derivatives_history as bybit


SCHEMA_VERSION = "bybit_carry_history_v1"
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


def load_anchor_range(
    path: pathlib.Path,
    lookback_days: int,
    audit_manifest: pathlib.Path | None = None,
) -> Tuple[int, int]:
    timestamps = bybit.load_bar_timestamps(path)
    step_ms = 300_000
    if audit_manifest is not None and audit_manifest.is_file():
        payload = json.loads(audit_manifest.read_text(encoding="utf-8"))
        frozen = payload.get("frozen_domain") if isinstance(payload, dict) else None
        if not isinstance(frozen, dict):
            raise ValueError("carry audit manifest has no frozen domain")
        start_ms = int(frozen["start_ms"])
        # domain end is exclusive; fetch its last bar.
        end_ms = int(frozen["end_ms"]) - step_ms
        if int(timestamps[0]) > start_ms or int(timestamps[-1]) < end_ms:
            raise ValueError("anchor development corpus no longer covers frozen carry domain")
        return start_ms, end_ms
    # Anchor rows are closed bars.  Exclude the final possibly-open interval and
    # use a deterministic trailing window from the development corpus only.
    end_ms = int(timestamps[-1])
    start_ms = max(int(timestamps[0]), end_ms - int(lookback_days) * 86_400_000)
    start_ms = ((start_ms + step_ms - 1) // step_ms) * step_ms
    if end_ms - start_ms < 120 * 86_400_000:
        raise ValueError("anchor development range is shorter than 120 days")
    return start_ms, end_ms


def parse_kline_points(rows: Iterable[Any]) -> List[bybit.Point]:
    points: List[bybit.Point] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        try:
            timestamp_ms = int(row[0])
            values = tuple(float(row[index]) for index in range(1, 7))
        except (TypeError, ValueError):
            continue
        if timestamp_ms > 0 and all(math.isfinite(value) and value >= 0.0 for value in values):
            if all(value > 0.0 for value in values[:4]):
                points.append(bybit.Point(timestamp_ms, values))
    return points


def parse_mark_points(rows: Iterable[Any]) -> List[bybit.Point]:
    points: List[bybit.Point] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            timestamp_ms = int(row[0])
            values = tuple(float(row[index]) for index in range(1, 5))
        except (TypeError, ValueError):
            continue
        if timestamp_ms > 0 and all(
            math.isfinite(value) and value > 0.0 for value in values
        ):
            points.append(bybit.Point(timestamp_ms, values))
    return points


def fetch_klines(
    *,
    base_url: str,
    category: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    timeout_sec: float,
    sleep_sec: float,
) -> Tuple[List[bybit.Point], int]:
    def request_page(page_start: int, page_end: int, page_limit: int) -> Sequence[bybit.Point]:
        params = {
            "category": category,
            "symbol": symbol,
            "interval": "5",
            "start": str(page_start),
            "end": str(page_end),
            "limit": str(page_limit),
        }
        url = f"{base_url.rstrip('/')}/v5/market/kline?{urlencode(params)}"
        payload = bybit.request_json(url, timeout_sec)
        return parse_kline_points(payload.get("result", {}).get("list", []))

    return bybit.collect_backward(
        start_ms=start_ms,
        end_ms=end_ms,
        page_limit=1000,
        request_page=request_page,
        expected_step_ms=300_000,
        sleep_sec=sleep_sec,
    )


def fetch_mark_klines(
    *,
    base_url: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    timeout_sec: float,
    sleep_sec: float,
) -> Tuple[List[bybit.Point], int]:
    def request_page(
        page_start: int, page_end: int, page_limit: int
    ) -> Sequence[bybit.Point]:
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": "5",
            "start": str(page_start),
            "end": str(page_end),
            "limit": str(page_limit),
        }
        url = (
            f"{base_url.rstrip('/')}/v5/market/mark-price-kline?"
            f"{urlencode(params)}"
        )
        payload = bybit.request_json(url, timeout_sec)
        return parse_mark_points(payload.get("result", {}).get("list", []))

    return bybit.collect_backward(
        start_ms=start_ms,
        end_ms=end_ms,
        page_limit=1000,
        request_page=request_page,
        expected_step_ms=300_000,
        sleep_sec=sleep_sec,
    )


def fetch_funding(
    *,
    base_url: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    timeout_sec: float,
    sleep_sec: float,
) -> Tuple[List[bybit.Point], int]:
    return bybit.fetch_time_series(
        base_url=base_url,
        endpoint="funding/history",
        fixed_params={"category": "linear", "symbol": symbol},
        start_ms=start_ms,
        end_ms=end_ms,
        limit=200,
        # Funding intervals can be adjusted per symbol; a one-hour safety
        # bound prevents pagination truncation without assuming today's cadence.
        expected_step_ms=3_600_000,
        parse_rows=lambda rows: bybit.parse_dict_points(
            rows,
            timestamp_key="fundingRateTimestamp",
            value_keys=("fundingRate",),
        ),
        timeout_sec=timeout_sec,
        sleep_sec=sleep_sec,
    )


def write_joined_csv(
    path: pathlib.Path,
    spot: Sequence[bybit.Point],
    perpetual: Sequence[bybit.Point],
    mark: Sequence[bybit.Point],
    funding: Sequence[bybit.Point],
) -> Dict[str, Any]:
    spot_by_ts = {point.timestamp_ms: point.values for point in spot}
    perp_by_ts = {point.timestamp_ms: point.values for point in perpetual}
    mark_by_ts = {point.timestamp_ms: point.values for point in mark}
    funding_by_ts = {point.timestamp_ms: point.values[0] for point in funding}
    timestamps = sorted(
        set(spot_by_ts).intersection(perp_by_ts).intersection(mark_by_ts)
    )
    if len(timestamps) < 2:
        raise ValueError("spot/perpetual exact inner join is empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["timestamp"]
            + [f"spot_{field}" for field in BAR_FIELDS]
            + [f"perpetual_{field}" for field in BAR_FIELDS]
            + ["mark_open", "mark_high", "mark_low", "mark_close"]
            + ["funding_rate"]
        )
        for timestamp in timestamps:
            writer.writerow(
                [timestamp]
                + [f"{value:.12g}" for value in spot_by_ts[timestamp]]
                + [f"{value:.12g}" for value in perp_by_ts[timestamp]]
                + [f"{value:.12g}" for value in mark_by_ts[timestamp]]
                + ([f"{funding_by_ts[timestamp]:.12g}"] if timestamp in funding_by_ts else [""])
            )
    steps = [right - left for left, right in zip(timestamps, timestamps[1:])]
    return {
        "row_count": len(timestamps),
        "start_ms": timestamps[0],
        "end_ms": timestamps[-1],
        "contiguous_step_count": sum(step == 300_000 for step in steps),
        "gap_count": sum(step != 300_000 for step in steps),
        "funding_event_count": sum(timestamp in funding_by_ts for timestamp in timestamps),
        "unmatched_funding_event_count": sum(timestamp not in set(timestamps) for timestamp in funding_by_ts),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor-csv", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--audit-manifest", default="")
    parser.add_argument("--lookback-days", type=int, default=140)
    parser.add_argument("--base-url", default="https://api.bybit.com")
    parser.add_argument("--timeout-sec", type=float, default=15.0)
    parser.add_argument("--sleep-sec", type=float, default=0.04)
    return parser.parse_args()


def run(args: argparse.Namespace) -> Dict[str, Any]:
    anchor = pathlib.Path(args.anchor_csv).resolve()
    output = pathlib.Path(args.output).resolve()
    manifest_path = pathlib.Path(args.audit_manifest).resolve() if args.audit_manifest else None
    start_ms, end_ms = load_anchor_range(
        anchor, int(args.lookback_days), audit_manifest=manifest_path
    )
    symbol = str(args.symbol).upper()
    spot, spot_pages = fetch_klines(
        base_url=args.base_url,
        category="spot",
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    perpetual, perpetual_pages = fetch_klines(
        base_url=args.base_url,
        category="linear",
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    mark, mark_pages = fetch_mark_klines(
        base_url=args.base_url,
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    funding, funding_pages = fetch_funding(
        base_url=args.base_url,
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    if not spot or not perpetual or not mark or not funding:
        raise RuntimeError("Bybit carry source returned an empty series")
    joined = write_joined_csv(output, spot, perpetual, mark, funding)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "provider": "bybit",
        "symbol": symbol,
        "requested": {"start_ms": start_ms, "end_ms": end_ms, "lookback_days": int(args.lookback_days)},
        "source": {
            "spot_bar_count": len(spot),
            "perpetual_bar_count": len(perpetual),
            "mark_bar_count": len(mark),
            "funding_event_count": len(funding),
            "spot_pages": spot_pages,
            "perpetual_pages": perpetual_pages,
            "mark_pages": mark_pages,
            "funding_pages": funding_pages,
        },
        "joined": joined,
        "causality": {
            "bar_alignment": "exact_timestamp_inner_join",
            "funding_alignment": "exact_settlement_timestamp_once_only",
            "asof_funding_fill": False,
        },
        "anchor_csv": str(anchor),
        "anchor_sha256": sha256_file(anchor),
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
