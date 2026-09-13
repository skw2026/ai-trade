#!/usr/bin/env python3
"""Pinned Demo archive + public settled calendars; no private API or trading.

Conditional coverage of symbols with matched one-way trades, NOT proof of
account-wide retention, exact funding fees, or historical Cross margin.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import tempfile
from decimal import Decimal, localcontext
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

import audit_bybit_readonly_evidence as source

SCHEMA = "bybit_funding_obligations_v1"
SYMBOL = re.compile(r"[A-Z0-9]{1,20}USDT\Z")
EDGE_MS = 5000  # Official inclusion uncertainty near settlement, not a fee tolerance.
require = source.require


def load_demo(capture: pathlib.Path, sha: str) -> tuple[dict, dict]:
    checked = source.replay(capture, sha)
    require(checked.get("source") == "demo" and checked.get("status") in
            ("READONLY_CAPTURED_CHECKS_PASS", "READONLY_CAPTURED_GAPS"), "DEMO_SOURCE_NOT_VERIFIED")
    raw = source.safe_read(capture / "manifest.json")
    require(source.digest(raw) == sha, "MANIFEST_CHANGED_DURING_REPLAY")
    manifest = source.decode(raw)
    groups: dict[str, list] = {}
    for page in manifest["pages"]:
        raw = source.safe_read(capture / page["file"])
        require(source.digest(raw) == page["sha256"], "RAW_CHANGED_DURING_REPLAY")
        rows, _ = source.response_rows(raw, page["group"])
        groups.setdefault(page["group"], []).extend(rows)
    return checked, groups


def symbols_in(groups: dict) -> list[str]:
    symbols = sorted({row.get("symbol", "") for row in groups["executions"]
                      if row.get("execType") == "Trade"})
    require(0 < len(symbols) <= 8 and all(SYMBOL.fullmatch(s) for s in symbols),
            "OBSERVED_SYMBOL_SCOPE_UNSUPPORTED")
    return symbols


def calendar_plan(symbols: list[str], start: int, end: int) -> list[dict]:
    source.window(start, end)
    require(symbols == sorted(set(symbols)) and 0 < len(symbols) <= 8 and
            all(isinstance(s, str) and SYMBOL.fullmatch(s) for s in symbols), "CALENDAR_SYMBOL_INVALID")
    # Whole-window and disjoint day queries must agree. A non-saturated API
    # response is still not independent evidence of historical completeness.
    windows = [(start, end)]
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + source.DAY - 1)
        windows.append((cursor, stop))
        cursor = stop + 1
    return [{"path": "/v5/market/funding/history", "params": {
        "category": "linear", "symbol": symbol, "startTime": left,
        "endTime": right, "limit": 200}}
        for symbol in symbols for left, right in windows]


class PublicCalendarTransport:
    def __init__(self):
        # Reuse TLS/no-proxy/no-redirect policy, never a credential pair.
        self.opener = source.Transport("public").opener

    def get(self, request: dict) -> bytes:
        params = request.get("params", {})
        require(request == calendar_plan([params.get("symbol")], params.get("startTime"),
                                         params.get("endTime"))[0], "PUBLIC_REQUEST_NOT_ALLOWED")
        url = source.DOMAINS["public"] + request["path"] + "?" + urlencode(params)
        try:
            with self.opener.open(Request(url, headers={"User-Agent": "ai-trade-funding-audit/1"},
                                          method="GET"), timeout=20) as response:
                require(response.status == 200, "HTTP_STATUS_INVALID")
                raw = response.read(source.MAX_BYTES + 1)
        except HTTPError as exc:
            raise ValueError(f"HTTP_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise ValueError("PUBLIC_TRANSPORT_ERROR") from None
        require(0 < len(raw) <= source.MAX_BYTES, "RESPONSE_SIZE_INVALID")
        return raw


def collect_calendars(root: pathlib.Path, symbols: list[str], start: int, end: int,
                      demo_sha: str, transport: Any) -> pathlib.Path:
    requests = calendar_plan(symbols, start, end)
    require(end < source.now_ms() - 60_000, "CALENDAR_WINDOW_NOT_CLOSED")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not root.is_symlink(), "ARCHIVE_ROOT_SYMLINK")
    capture = pathlib.Path(tempfile.mkdtemp(prefix="calendar-", dir=root))
    manifest = {"schema_version": SCHEMA, "demo_manifest_sha256": demo_sha,
                "domain": source.DOMAINS["public"], "method": "GET", "symbols": symbols,
                "start_ms": start, "end_ms": end, "created_ms": source.now_ms(),
                "collector_sha256": source.digest(pathlib.Path(__file__).read_bytes()), "pages": []}
    def checkpoint():
        source.safe_file(capture / "manifest.next", source.encode(manifest))
        (capture / "manifest.next").replace(capture / "manifest.json")
    checkpoint()
    for request in requests:
        sent = source.now_ms()
        raw = transport.get(request)
        received = source.now_ms()
        name = f"{len(manifest['pages']):04d}.raw"
        source.safe_file(capture / name, raw)
        manifest["pages"].append({"file": name, "request": request, "sha256": source.digest(raw),
                                  "sent_ms": sent, "received_ms": received})
        checkpoint()  # Keep actual failed payloads privately for diagnosis.
        calendar_rows(raw, request)
    return capture


def calendar_rows(raw: bytes, request: dict) -> dict[int, str]:
    data = source.result(raw)
    require(data.get("category") == "linear" and data.get("nextPageCursor", "") == "",
            "CALENDAR_RESPONSE_INVALID")
    rows = data.get("list")
    require(isinstance(rows, list) and len(rows) < 200, "CALENDAR_EMPTY_SCHEMA_OR_SATURATED")
    params = request["params"]
    rates: dict[int, str] = {}
    for row in rows:
        stamp = source.epoch(row.get("fundingRateTimestamp"))
        require(row.get("symbol") == params["symbol"] and params["startTime"] <= stamp <= params["endTime"]
                and stamp not in rates, "CALENDAR_IDENTITY_OR_TIME_INVALID")
        require(abs(source.number(row.get("fundingRate"))) <= 1, "CALENDAR_RATE_INVALID")
        rates[stamp] = row["fundingRate"]
    return rates


def replay_calendars(capture: pathlib.Path, sha: str, demo: dict, symbols: list[str]) -> tuple[dict, dict]:
    require(not capture.is_symlink(), "ARCHIVE_ROOT_SYMLINK")
    raw = source.safe_read(capture / "manifest.json")
    require(source.SHA.fullmatch(sha) and source.digest(raw) == sha, "CALENDAR_MANIFEST_HASH_MISMATCH")
    manifest = source.decode(raw)
    require(manifest.get("schema_version") == SCHEMA and manifest.get("domain") == source.DOMAINS["public"]
            and manifest.get("method") == "GET" and manifest.get("symbols") == symbols and
            manifest.get("demo_manifest_sha256") == demo["manifest_sha256"] and
            manifest.get("start_ms") == demo["start_ms"] and manifest.get("end_ms") == demo["end_ms"],
            "CALENDAR_SOURCE_MISMATCH")
    require(type(manifest.get("created_ms")) is int and manifest["created_ms"] > demo["end_ms"] + 60_000
            and isinstance(manifest.get("collector_sha256"), str) and
            source.SHA.fullmatch(manifest["collector_sha256"]), "CALENDAR_PROVENANCE_INVALID")
    requests = calendar_plan(symbols, demo["start_ms"], demo["end_ms"])
    require(len(manifest["pages"]) == len(requests), "CALENDAR_PAGE_CHAIN_INCOMPLETE")
    whole: dict[str, dict] = {}
    partitions: dict[str, dict] = {}
    for index, (page, request) in enumerate(zip(manifest["pages"], requests)):
        require(page["file"] == f"{index:04d}.raw" and page["request"] == request,
                "CALENDAR_REQUEST_PLAN_MISMATCH")
        raw = source.safe_read(capture / page["file"])
        require(source.digest(raw) == page["sha256"], "CALENDAR_RAW_HASH_MISMATCH")
        server = source.decode(raw).get("time")
        require(type(page["sent_ms"]) is int and type(page["received_ms"]) is int and
                manifest["created_ms"] <= page["sent_ms"] <= page["received_ms"] and
                type(server) is int and page["sent_ms"] - 60_000 <= server <= page["received_ms"] + 60_000,
                "CALENDAR_CLOCK_INVALID")
        rates = calendar_rows(raw, request)
        symbol = request["params"]["symbol"]
        if symbol not in whole:
            whole[symbol] = rates
        else:
            parts = partitions.setdefault(symbol, {})
            require(not set(parts) & set(rates), "CALENDAR_PARTITION_DUPLICATE")
            parts.update(rates)
    require(whole == partitions, "CALENDAR_PARTITIONS_DISAGREE")
    require(all(whole.values()), "NO_PUBLIC_SETTLED_BOUNDARIES")
    return whole, {"calendar_manifest_sha256": sha, "public_response_pages": len(requests),
                   "calendar_partition_check": True, "calendar_independent_completeness_proven": False}


def audit(groups: dict, calendars: dict[str, dict], start: int, end: int) -> dict:
    """Only infer paths with unique timestamps, matching orders, and signed size.

    Initial size is inferred from the first post-trade size, not a fabricated
    zero. Snapshot positions are deliberately not used across a time gap.
    """
    checks = source.audit_demo(groups, start, end)
    require(checks["matched_execution_transactions"] == checks["trade_execution_records"] and
            not ({"EXECUTION_WITHOUT_TRANSACTION", "TRANSACTION_WITHOUT_EXECUTION"} & set(checks["gaps"])),
            "TRADE_MATCH_COVERAGE_INCOMPLETE")
    txs = {(row.get("symbol"), row.get("tradeId")): row for row in groups["transactions"]
           if row.get("type") == "TRADE" and row.get("category") == "linear"}
    orders: dict[tuple, dict] = {}
    for row in groups["orders"]:
        key = row.get("symbol"), row.get("orderId")
        require(key not in orders, "ORDER_HISTORY_DUPLICATE")
        orders[key] = row
    paths: dict[str, list] = {symbol: [] for symbol in symbols_in(groups)}
    require(set(paths) == set(calendars), "CALENDAR_SYMBOL_SET_MISMATCH")
    gaps: set[str] = {"HISTORY_RETENTION_NOT_PROVEN", "PUBLIC_CALENDAR_NOT_INDEPENDENTLY_COMPLETE"}
    invalid: set[str] = set()
    for row in groups["executions"]:
        symbol = row.get("symbol")
        if row.get("execType") != "Trade":
            if row.get("execType") != "Funding":
                gaps.add("NONTRADE_POSITION_EVENT_UNSUPPORTED")
                invalid.update(paths)
            continue
        tx = txs[(symbol, row["execId"])]
        order = orders.get((symbol, row["orderId"]), {})
        if type(order.get("positionIdx")) is not int or order["positionIdx"] != 0:
            gaps.add("ONE_WAY_ORDER_EVIDENCE_MISSING")
            invalid.add(symbol)
            continue
        stamp = source.epoch(row["execTime"])
        tx_stamp = source.epoch(tx["transactionTime"])
        if stamp != tx_stamp:
            gaps.add("TRADE_TRANSACTION_TIME_MISMATCH")
            invalid.add(symbol)
            continue
        if tx.get("size") in (None, ""):
            gaps.add("SIGNED_POST_TRADE_SIZE_MISSING")
            invalid.add(symbol)
            continue
        size = source.number(tx["size"])
        delta = source.number(row["execQty"]) * (1 if row["side"] == "Buy" else -1)
        paths[symbol].append((stamp, size - delta, size))
    for tx in groups["transactions"]:
        if tx.get("category") == "linear" and tx.get("type") not in ("TRADE", "SETTLEMENT"):
            # Transfers can be benign, but don't silently assume an unknown
            # linear event cannot change a position.
            gaps.add("NONTRADE_POSITION_EVENT_UNSUPPORTED")
            invalid.update(paths)
    counts = {"observed_symbols": len(paths), "matched_trades": checks["matched_execution_transactions"],
              "public_boundaries": sum(len(rates) for rates in calendars.values()),
              "flat_boundaries": 0, "exposed_boundaries": 0, "ambiguous_boundaries": 0,
              "verified_position_paths": 0, "inferred_opening_nonzero_symbols": 0,
              "inferred_ending_nonzero_symbols": 0, "funding_records_observed":
              checks["funding_execution_records"] + checks["funding_settlement_records"]}
    for symbol, path in paths.items():
        path.sort()
        if len({p[0] for p in path}) != len(path):
            gaps.add("SAME_TIMESTAMP_TRADE_ORDER_UNRESOLVED")
            invalid.add(symbol)
        if any(prior[2] != after[1] for prior, after in zip(path, path[1:])):
            gaps.add("POST_TRADE_POSITION_CHAIN_BROKEN")
            invalid.add(symbol)
        if symbol in invalid or not path:
            counts["ambiguous_boundaries"] += len(calendars[symbol])
            continue
        counts["verified_position_paths"] += 1
        counts["inferred_opening_nonzero_symbols"] += path[0][1] != 0
        counts["inferred_ending_nonzero_symbols"] += path[-1][2] != 0
        for when in calendars[symbol]:
            require(start <= when <= end, "BOUNDARY_OUTSIDE_DEMO_WINDOW")
            if when - start <= EDGE_MS or end - when <= EDGE_MS or any(abs(p[0] - when) <= EDGE_MS for p in path):
                counts["ambiguous_boundaries"] += 1
                gaps.add("FUNDING_INCLUSION_WITHIN_FIVE_SECONDS_UNCERTAIN")
                continue
            size = path[0][1]
            for stamp, _, after in path:
                if stamp > when:
                    break
                size = after
            counts["flat_boundaries" if size == 0 else "exposed_boundaries"] += 1
    if counts["funding_records_observed"]:
        status = "FUNDING_RECORDS_REQUIRE_SEPARATE_RECONCILIATION"
        gaps.add("FUNDING_AMOUNTS_NOT_RECONCILED_BY_THIS_TOOL")
    elif counts["exposed_boundaries"]:
        status = "OBSERVED_EXPOSURE_WITHOUT_SETTLEMENT_RECORD"
        gaps.add("EXPOSED_BOUNDARY_WITHOUT_FUNDING_SAMPLE")
    elif counts["ambiguous_boundaries"]:
        status = "FUNDING_OBLIGATIONS_UNRESOLVED"
    else:
        status = "OBSERVED_BOUNDARIES_FLAT_CONDITIONAL"
    return {"schema_version": SCHEMA, "status": status, "counts": counts, "gaps": sorted(gaps),
            "scope": "MATCHED_ONE_WAY_TRADE_SYMBOLS_ONLY", "five_second_inclusion_guard_ms": EDGE_MS,
            "initial_position_assumed_zero": False, "snapshot_used_as_window_endpoint": False,
            "no_funding_obligation_accountwide_proven": False, "funding_fee_model_qualified": False,
            "historical_cross_margin_qualified": False, "c2_option_accounting_qualified": False,
            "private_api_calls": False, "order_submission": False, "promotion_authority": False,
            "balances_or_cash_amounts_published": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect-public", "replay"))
    parser.add_argument("--demo-parent", type=pathlib.Path, required=True)
    parser.add_argument("--demo-sha256", required=True)
    parser.add_argument("--calendar-root", type=pathlib.Path)
    parser.add_argument("--calendar-capture", type=pathlib.Path)
    parser.add_argument("--calendar-sha256")
    args = parser.parse_args()
    try:
        require(args.demo_parent.is_dir() and not args.demo_parent.is_symlink(), "DEMO_PARENT_INVALID")
        children = [p for p in args.demo_parent.iterdir() if p.is_dir() and not p.is_symlink()]
        require(len(children) == 1, "DEMO_PARENT_NOT_UNIQUE")
        demo, groups = load_demo(children[0], args.demo_sha256)
        symbols = symbols_in(groups)
        if args.action == "collect-public":
            require(args.calendar_root is not None and args.calendar_capture is None and
                    args.calendar_sha256 is None, "CALENDAR_OUTPUT_REQUIRED")
            args.calendar_capture = collect_calendars(args.calendar_root, symbols, demo["start_ms"],
                demo["end_ms"], args.demo_sha256, PublicCalendarTransport())
            args.calendar_sha256 = source.digest(source.safe_read(args.calendar_capture / "manifest.json"))
        require(args.calendar_capture is not None and args.calendar_sha256 is not None, "CALENDAR_PIN_REQUIRED")
        calendars, meta = replay_calendars(args.calendar_capture, args.calendar_sha256, demo, symbols)
        with localcontext() as ctx:
            ctx.prec = 100
            report = audit(groups, calendars, demo["start_ms"], demo["end_ms"])
        report.update(meta, demo_manifest_sha256=args.demo_sha256,
                      auditor_sha256=source.digest(pathlib.Path(__file__).read_bytes()),
                      source_replayer_sha256=demo["replayer_sha256"])
        if args.action == "collect-public":
            source.safe_file(args.calendar_capture / "summary.json", source.encode(report))
        print(source.encode(report).decode(), end="")
        return 0
    except (ValueError, OSError, KeyError, TypeError, ArithmeticError) as exc:
        print(source.encode({"schema_version": SCHEMA, "status": "FUNDING_AUDIT_NOT_COMPLETED",
                             "reason": source.error_code(exc), "promotion_authority": False}).decode(), end="")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
