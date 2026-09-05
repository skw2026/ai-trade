#!/usr/bin/env python3
"""Bounded, unauthenticated historical sample qualification; never payoff evidence.

Source contract: https://docs.tardis.dev/historical-data-details/bybit-options
Only the free first UTC day of a month and one minute may be requested. Native
Bybit WebSocket names are intentionally distinct from the REST ticker schema.
"""

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import pathlib
import re
import subprocess
from typing import Any
import urllib.parse


MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_DECODED_BYTES = 64 * 1024 * 1024
SYMBOL = re.compile(r"BTC-\d{1,2}[A-Z]{3}\d{2}-\d+(?:\.\d+)?-[CP]-USDT\Z")
AUTHORITIES = {"promotion_authority": False, "demo_activation_authorized": False,
               "live_activation_authorized": False}
REMAINING_REQUIREMENTS = [
    "continuous_held_instrument_history_and_gap_inventory",
    "historical_instrument_units_and_settlement_currency",
    "actual_delivery_prices_not_predicted_delivery_prices",
    "hedge_orderbook_and_actual_funding_settlements",
    "historical_fee_and_margin_contract",
    "dataset_license_and_budget_approval",
]


def request_contract(date: str, offset: int, symbols: list[str]) -> tuple[str, int]:
    day = dt.date.fromisoformat(date)
    if day.day != 1 or not 0 <= offset < 1440:
        raise ValueError("Only one minute on the free first UTC day is allowed")
    if not 1 <= len(symbols) <= 4 or len(set(symbols)) != len(symbols):
        raise ValueError("Supply one to four distinct option symbols")
    if any(not SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise ValueError("Only explicit BTC USDT call/put symbols are allowed")
    start = dt.datetime.combine(day, dt.time(), dt.timezone.utc)
    minute_start = int(start.timestamp() * 1000) + offset * 60000
    query = urllib.parse.urlencode({
        "from": day.isoformat(), "offset": offset,
        "filters": json.dumps([{"channel": "tickers", "symbols": symbols}],
                              separators=(",", ":")),
    })
    return "https://api.tardis.dev/v1/data-feeds/bybit-options?" + query, minute_start


def fetch_free_sample(url: str) -> bytes:
    # No account credentials, authentication headers, paid dates or arbitrary URL.
    if not url.startswith("https://api.tardis.dev/v1/data-feeds/bybit-options?"):
        raise ValueError("Unexpected historical source")
    response = subprocess.run([
        "curl", "--disable", "--fail", "--silent", "--show-error", "--location", "--globoff",
        "--connect-timeout", "10", "--max-time", "40", "--max-filesize",
        str(MAX_RAW_BYTES), url,
    ], capture_output=True, timeout=45, check=False)
    if response.returncode:
        raise RuntimeError(f"Public sample request failed (curl {response.returncode})")
    if not response.stdout or len(response.stdout) > MAX_RAW_BYTES:
        raise ValueError("Historical response is empty or exceeds byte budget")
    return response.stdout


def decode_sample(raw: bytes) -> str:
    if not raw or len(raw) > MAX_RAW_BYTES:
        raise ValueError("Raw sample is empty or too large")
    if raw.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            decoded = stream.read(MAX_DECODED_BYTES + 1)
    else:
        decoded = raw
    if len(decoded) > MAX_DECODED_BYTES:
        raise ValueError("Decoded sample exceeds byte budget")
    return decoded.decode("utf-8")


def finite_number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"Invalid {key}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Non-finite {key}")
    return result


def audit_sample(raw: bytes, *, date: str, offset: int,
                 symbols: list[str], downloaded: bool = False) -> dict[str, Any]:
    url, minute_start = request_contract(date, offset, symbols)
    states: dict[str, dict[str, Any]] = {}
    accepted: Counter[str] = Counter()
    rejects: Counter[str] = Counter()
    message_count = resets = 0
    local_times: list[int] = []
    exchange_times: list[int] = []
    for line in decode_sample(raw).splitlines():
        if not line.strip():
            states.clear()
            resets += 1
            continue
        try:
            timestamp, separator, body = line.partition(" ")
            local_time = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if local_time.utcoffset() != dt.timedelta(0):
                raise ValueError("Timestamp must be UTC")
            local_ms = int(local_time.timestamp() * 1000)
            if not minute_start <= local_ms < minute_start + 60000:
                raise ValueError("Timestamp outside the requested minute")
            if local_times and local_ms < local_times[-1]:
                raise ValueError("Local timestamp moved backwards")
            local_times.append(local_ms)
            if not separator or not body.strip():
                states.clear()
                resets += 1
                continue
            message = json.loads(body)
            topic = str(message["topic"])
            if not topic.startswith("tickers."):
                raise ValueError("Unexpected channel")
            symbol = topic[len("tickers."):]
            row = message["data"]
            if symbol not in symbols or not isinstance(row, dict):
                raise ValueError("Unexpected symbol or payload")
            if row.get("symbol", symbol) != symbol:
                raise ValueError("Topic and payload symbol disagree")
            exchange_ms = int(message["ts"])
            if exchange_ms <= 0 or abs(exchange_ms - local_ms) > 1000:
                raise ValueError("Missing/stale/future exchange timestamp")
            kind = message.get("type")
            if kind == "snapshot":
                states[symbol] = dict(row)
            elif kind == "delta" and symbol in states:
                states[symbol].update(row)
            else:
                raise ValueError("Delta without a snapshot or unknown message type")
            message_count += 1
            current = states[symbol]
            # Native option WS uses bidPrice/bidSize, NOT REST bid1Price/bid1Size.
            bid, ask, bid_size, ask_size, index, mark, iv, delta, gamma, vega, theta = [
                finite_number(current, key) for key in (
                    "bidPrice", "askPrice", "bidSize", "askSize", "indexPrice",
                    "markPrice", "markPriceIv", "delta", "gamma", "vega", "theta")
            ]
            if (bid <= 0 or ask < bid or min(bid_size, ask_size, index, mark, iv) <= 0
                    or not -1.0 <= delta <= 1.0 or gamma < 0 or vega < 0):
                raise ValueError("Non-executable or invalid option quote")
            accepted[symbol] += 1
            exchange_times.append(exchange_ms)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            # Any rejected observation invalidates the sample-level gate. A
            # later full snapshot may recover state, but never erase a failure.
            states.clear()
            rejects[str(exc)] += 1
    passed = not rejects and all(accepted[symbol] > 0 for symbol in symbols)
    return {
        "schema_version": "option_historical_sample_qualification_v1",
        "research_domain": "development_only",
        "status": "PASS_SAMPLE_SCHEMA_ONLY" if passed else "REJECTED_SAMPLE",
        "source_url": url, "requested_date_utc": date, "offset_minute": offset,
        "acquisition": "unauthenticated_public_sample" if downloaded else "local_unverified_input",
        "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw),
        "requested_symbols": symbols, "message_count": message_count,
        "qualified_observations_by_symbol": dict(accepted),
        "rejection_counts": dict(rejects), "disconnect_markers": resets,
        "local_span_seconds": (max(local_times) - min(local_times)) / 1000 if local_times else 0,
        "exchange_span_seconds": (max(exchange_times) - min(exchange_times)) / 1000 if exchange_times else 0,
        "continuous_history_qualified": False, "payoff_evidence": False,
        "remaining_requirements": REMAINING_REQUIREMENTS, "authorities": AUTHORITIES,
    }


def persist_new_or_identical(path: pathlib.Path, content: bytes) -> None:
    if path.is_symlink():
        raise ValueError("Refusing symlink output")
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError("Refusing to overwrite different evidence")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--offset-minute", type=int, default=0)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--archive", type=pathlib.Path, help="Audit local raw bytes; no network")
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        url, _ = request_contract(args.date, args.offset_minute, args.symbol)
        if args.archive:
            with args.archive.open("rb") as stream:
                raw = stream.read(MAX_RAW_BYTES + 1)
        else:
            raw = fetch_free_sample(url)
        report = audit_sample(raw, date=args.date, offset=args.offset_minute,
                              symbols=args.symbol, downloaded=args.archive is None)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        identity = report["raw_sha256"]
        persist_new_or_identical(args.output_dir / f"{identity}.raw", raw)
        encoded = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2,
                              allow_nan=False) + "\n").encode()
        report_hash = hashlib.sha256(encoded).hexdigest()
        report_path = args.output_dir / f"{report_hash}.qualification.json"
        persist_new_or_identical(report_path, encoded)
        print(json.dumps({"report_path": str(report_path), **report}, ensure_ascii=False))
        return 0 if report["status"] == "PASS_SAMPLE_SCHEMA_ONLY" else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, EOFError) as exc:
        print(json.dumps({"status": "SAMPLE_UNAVAILABLE_OR_INVALID", "error": str(exc),
                          "payoff_evidence": False, "authorities": AUTHORITIES}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
