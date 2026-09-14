#!/usr/bin/env python3
"""Bounded official C2 historical mark/index/funding capture and offline replay.

Public HTTPS GET only. Minute candles are not exact settlement marks or BBO.
Current risk tiers are never certified as historical parameters.
"""
from __future__ import annotations
import argparse
import pathlib
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

import audit_bybit_readonly_evidence as wire
import audit_option_subaccount_ledger as ledger

MINUTE = 60000
SCHEMA = "bybit_c2_market_archive_v1"
require = wire.require


def plan(start, end, options=()):
    wire.window(start, end)
    require(end - start <= 2 * wire.DAY, "C2_WINDOW_EXCEEDS_TWO_DAYS")
    require(isinstance(options, (list, tuple)) and list(options) == sorted(set(options)) and
            len(options) <= 2 and all(ledger.OPTION.fullmatch(s) for s in options), "OPTION_SCOPE_INVALID")
    requests = []
    first, last = start // MINUTE * MINUTE - MINUTE, end // MINUTE * MINUTE
    streams = [("mark", "linear", "BTCUSDT", "/v5/market/mark-price-kline", 1000),
               ("index", "linear", "BTCUSDT", "/v5/market/index-price-kline", 1000)]
    streams += [("option:" + s, "option", s, "/v5/market/mark-price-kline", 500) for s in options]
    for group, category, symbol, path, limit in streams:
        cursor = first
        while cursor <= last:
            stop = min(last, cursor + (limit - 1) * MINUTE)
            requests.append({"group": group, "path": path, "params": {"category": category, "symbol": symbol,
                "interval": "1", "start": cursor, "end": stop, "limit": limit}})
            cursor = stop + MINUTE
    requests += [
        {"group": "funding", "path": "/v5/market/funding/history", "params": {
            "category": "linear", "symbol": "BTCUSDT", "startTime": start, "endTime": end, "limit": 200}},
        {"group": "risk", "path": "/v5/market/risk-limit", "params": {"category": "linear", "symbol": "BTCUSDT"}}]
    return requests


class Transport:
    def __init__(self, requests):
        self.requests = requests
        self.opener = wire.Transport("public").opener

    def get(self, request):
        require(request in self.requests, "MARKET_REQUEST_NOT_ALLOWED")
        url = wire.DOMAINS["public"] + request["path"] + "?" + urlencode(request["params"])
        try:
            with self.opener.open(Request(url, headers={"User-Agent": "ai-trade-c2-history/1"}, method="GET"), timeout=20) as response:
                require(response.status == 200, "HTTP_STATUS_INVALID")
                raw = response.read(wire.MAX_BYTES + 1)
        except HTTPError as exc:
            raise ValueError(f"HTTP_{exc.code}") from None
        except (URLError, OSError, TimeoutError):
            raise ValueError("MARKET_TRANSPORT_ERROR") from None
        require(0 < len(raw) <= wire.MAX_BYTES, "MARKET_RESPONSE_SIZE_INVALID")
        return raw


def collect(root, start, end, options, transport):
    requests = plan(start, end, options)
    require(end // MINUTE * MINUTE + MINUTE < wire.now_ms() - MINUTE, "MARKET_WINDOW_NOT_CLOSED")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not root.is_symlink(), "ARCHIVE_ROOT_SYMLINK")
    capture = pathlib.Path(tempfile.mkdtemp(prefix="c2-market-", dir=root))
    manifest = {"schema_version": SCHEMA, "start_ms": start, "end_ms": end, "options": list(options),
        "domain": wire.DOMAINS["public"], "method": "GET", "created_ms": wire.now_ms(), "pages": [],
        "collector_sha256": wire.digest(pathlib.Path(__file__).read_bytes())}
    def checkpoint():
        wire.safe_file(capture / "manifest.next", wire.encode(manifest))
        (capture / "manifest.next").replace(capture / "manifest.json")
    checkpoint()
    for request in requests:
        sent = wire.now_ms()
        raw = transport.get(request)
        received = wire.now_ms()
        name = f"{len(manifest['pages']):04d}.raw"
        wire.safe_file(capture / name, raw)
        manifest["pages"].append({"request": request, "file": name, "sha256": wire.digest(raw),
                                  "sent_ms": sent, "received_ms": received})
        checkpoint()
    return capture


def replay(capture, expected_sha):
    require(not capture.is_symlink(), "ARCHIVE_ROOT_SYMLINK")
    raw = wire.safe_read(capture / "manifest.json")
    require(wire.SHA.fullmatch(expected_sha) and wire.digest(raw) == expected_sha, "MARKET_MANIFEST_HASH_MISMATCH")
    manifest = wire.decode(raw)
    require(manifest["schema_version"] == SCHEMA and manifest["domain"] == wire.DOMAINS["public"] and
            manifest["method"] == "GET" and type(manifest["created_ms"]) is int and
            isinstance(manifest.get("collector_sha256"), str) and wire.SHA.fullmatch(manifest["collector_sha256"]),
            "MARKET_MANIFEST_IDENTITY_INVALID")
    start, end = manifest["start_ms"], manifest["end_ms"]
    require(manifest["created_ms"] > end // MINUTE * MINUTE + 2 * MINUTE, "MARKET_CAPTURE_NOT_CLOSED")
    requests = plan(start, end, manifest["options"])
    require(len(manifest["pages"]) == len(requests), "MARKET_PAGE_CHAIN_INCOMPLETE")
    data = {"mark": {}, "index": {}, "funding": {}, "risk": []}
    option_gaps = set()
    for i, (page, request) in enumerate(zip(manifest["pages"], requests)):
        require(page["request"] == request and page["file"] == f"{i:04d}.raw", "MARKET_REQUEST_PLAN_MISMATCH")
        raw = wire.safe_read(capture / page["file"])
        require(wire.digest(raw) == page["sha256"], "MARKET_RAW_HASH_MISMATCH")
        payload = wire.decode(raw)
        server = payload.get("time")
        require(type(page["sent_ms"]) is int and type(page["received_ms"]) is int and
                manifest["created_ms"] <= page["sent_ms"] <= page["received_ms"] and type(server) is int and
                page["sent_ms"] - MINUTE <= server <= page["received_ms"] + MINUTE, "MARKET_CLOCK_INVALID")
        group, params = request["group"], request["params"]
        if group.startswith("option:") and payload.get("retCode") != 0:
            option_gaps.add(group)
            continue
        result = wire.result(raw)
        require(result.get("category") == params["category"] and result.get("nextPageCursor", "") == "",
                "MARKET_CATEGORY_OR_CURSOR_INVALID")
        rows = result.get("list")
        require(isinstance(rows, list) and len(rows) <= params.get("limit", 1000), "MARKET_ROWS_INVALID")
        if group == "risk":
            require(rows and all(row.get("symbol") == "BTCUSDT" for row in rows), "RISK_SYMBOL_INVALID")
            data[group] = rows
        elif group == "funding":
            require(len(rows) < params["limit"], "FUNDING_PAGE_SATURATED")
            for row in rows:
                stamp = wire.epoch(row["fundingRateTimestamp"])
                require(start <= stamp <= end and stamp not in data[group] and row["symbol"] == "BTCUSDT" and
                        abs(wire.number(row["fundingRate"])) <= 1, "FUNDING_IDENTITY_INVALID")
                data[group][stamp] = row["fundingRate"]
        else:
            require(result.get("symbol") == params["symbol"], "CANDLE_SYMBOL_INVALID")
            candles = data.setdefault(group, {})
            expected = set(range(params["start"], params["end"] + MINUTE, MINUTE))
            seen = set()
            for row in rows:
                require(isinstance(row, list) and len(row) == 5, "CANDLE_SHAPE_INVALID")
                stamp = wire.epoch(row[0])
                require(stamp in expected and stamp not in seen and stamp not in candles and stamp + MINUTE <= server,
                        "CANDLE_TIME_DUPLICATE_OR_UNCLOSED")
                op, hi, lo, close = [wire.number(v) for v in row[1:]]
                require(0 <= lo <= min(op, close) <= max(op, close) <= hi and
                        (lo > 0 or group.startswith("option:")), "CANDLE_OHLC_INVALID")
                seen.add(stamp)
                candles[stamp] = {"open": row[1], "high": row[2], "low": row[3], "close": row[4]}
            if group.startswith("option:") and seen != expected:
                option_gaps.add(group)
            else:
                require(seen == expected, "CANDLE_COVERAGE_INCOMPLETE")
    summary = {"schema_version": SCHEMA, "status": "PUBLIC_MARKET_REPLAYED_WITH_GAPS" if option_gaps else "PUBLIC_MARKET_REPLAYED",
        "manifest_sha256": expected_sha, "start_ms": start, "end_ms": end, "response_pages": len(requests),
        "counts": {key: len(rows) for key, rows in data.items()}, "option_history_incomplete": sorted(option_gaps),
        "collector_sha256": manifest["collector_sha256"], "replayer_sha256": wire.digest(pathlib.Path(__file__).read_bytes()),
        "exact_settlement_marks_qualified": False, "historical_risk_tiers_qualified": False,
        "independent_history_completeness_proven": False, "order_submission": False, "promotion_authority": False}
    return data, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "replay"))
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    parser.add_argument("--option-symbol", action="append", default=[])
    parser.add_argument("--root", type=pathlib.Path)
    parser.add_argument("--capture", type=pathlib.Path)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args()
    try:
        if args.action == "collect":
            require(args.root is not None, "ROOT_REQUIRED")
            requests = plan(args.start_ms, args.end_ms, args.option_symbol)
            args.capture = collect(args.root, args.start_ms, args.end_ms, args.option_symbol, Transport(requests))
            args.manifest_sha256 = wire.digest(wire.safe_read(args.capture / "manifest.json"))
        require(args.capture is not None and args.manifest_sha256 is not None, "PINNED_CAPTURE_REQUIRED")
        _, report = replay(args.capture, args.manifest_sha256)
        if args.action == "collect":
            wire.safe_file(args.capture / "summary.json", wire.encode(report))
            report["capture_directory"] = str(args.capture)
        print(wire.encode(report).decode(), end="")
        return 0
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        print(wire.encode({"status": "C2_MARKET_NOT_COMPLETED", "reason": wire.error_code(exc),
                           "promotion_authority": False}).decode(), end="")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
