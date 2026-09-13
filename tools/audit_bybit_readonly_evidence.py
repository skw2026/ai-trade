#!/usr/bin/env python3
"""Bounded Bybit public history / existing Demo account evidence, never trading.

Raw account responses stay in a private archive. stdout is an allowlisted,
amount-free summary. Replay needs no credentials or network. Endpoint and query
plans are reconstructed during replay; a digest alone is not source validation.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import pathlib
import re
import ssl
import tempfile
import time
from decimal import Decimal, localcontext
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


SCHEMA = "bybit_readonly_evidence_v1"
DAY = 86_400_000
MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 200
DOMAINS = {"public": "https://api.bybit.com", "demo": "https://api-demo.bybit.com"}
SHA = re.compile(r"[0-9a-f]{64}\Z")
DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,24})(?:\.[0-9]{1,24})?\Z")
KEY_NAMES = ("AI_TRADE_BYBIT_DEMO_API_KEY", "AI_TRADE_BYBIT_DEMO_API_SECRET",
             "AI_TRADE_API_KEY", "AI_TRADE_API_SECRET")


def require(ok: Any, code: str) -> None:
    if not ok:
        raise ValueError(code)


def error_code(exc: Exception) -> str:
    value = str(exc)
    return value if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value) else "SCHEMA_OR_IO_ERROR"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in out, "DUPLICATE_JSON_KEY")
        out[key] = value
    return out


def decode(raw: bytes) -> dict[str, Any]:
    require(0 < len(raw) <= MAX_BYTES, "RESPONSE_SIZE_INVALID")
    result = json.loads(raw, object_pairs_hook=unique_object,
                        parse_constant=lambda _: require(False, "NONFINITE_JSON"))
    require(isinstance(result, dict), "RESPONSE_OBJECT_REQUIRED")
    return result


def number(value: Any) -> Decimal:
    require(isinstance(value, str) and DECIMAL.fullmatch(value), "DECIMAL_INVALID")
    return Decimal(value)


def epoch(value: Any) -> int:
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,14}", value), "TIME_INVALID")
    require(int(value) > 0, "TIME_INVALID")
    return int(value)


def window(start: Any, end: Any) -> None:
    require(type(start) is int and type(end) is int and 0 < start <= end,
            "WINDOW_INVALID")
    require(end - start < 7 * DAY, "WINDOW_EXCEEDS_SEVEN_DAYS")


def plan(source: str, start: int, end: int) -> dict[str, dict[str, Any]]:
    window(start, end)
    require(source in DOMAINS, "SOURCE_NOT_ALLOWED")
    if source == "public":
        return {
            "funding": {"path": "/v5/market/funding/history", "params": {
                "category": "linear", "symbol": "BTCUSDT", "startTime": start,
                "endTime": end, "limit": 200}},
            "instrument": {"path": "/v5/market/instruments-info", "params": {
                "category": "linear", "symbol": "BTCUSDT"}},
            "risk_limit": {"path": "/v5/market/risk-limit", "params": {
                "category": "linear", "symbol": "BTCUSDT"}},
        }
    return {
        "account": {"path": "/v5/account/info", "params": {}},
        "wallet": {"path": "/v5/account/wallet-balance", "params": {
            "accountType": "UNIFIED", "coin": "USDT"}},
        "positions": {"path": "/v5/position/list", "params": {
            "category": "linear", "settleCoin": "USDT", "limit": 200}},
        "open_orders": {"path": "/v5/order/realtime", "params": {
            "category": "linear", "settleCoin": "USDT", "openOnly": 0, "limit": 50}},
        "orders": {"path": "/v5/order/history", "params": {
            "category": "linear", "settleCoin": "USDT", "startTime": start,
            "endTime": end, "limit": 50}},
        "executions": {"path": "/v5/execution/list", "params": {
            "category": "linear", "settleCoin": "USDT", "startTime": start,
            "endTime": end, "limit": 100}},
        # No category/type filter: retain transfers and all USDT cash changes.
        "transactions": {"path": "/v5/account/transaction-log", "params": {
            "accountType": "UNIFIED", "currency": "USDT", "startTime": start,
            "endTime": end, "limit": 50}},
    }


def mark_request(when: int) -> dict[str, Any]:
    require(when > 60_000 and when % 60_000 == 0, "FUNDING_BOUNDARY_INVALID")
    return {"path": "/v5/market/mark-price-kline", "params": {
        "category": "linear", "symbol": "BTCUSDT", "interval": "1",
        "start": when - 60_000, "end": when + 60_000, "limit": 3}}


def validate_request(source: str, request: dict[str, Any]) -> None:
    # Fail closed even when the transport is called outside the collector.
    require(set(request) == {"path", "params"}, "REQUEST_FIELDS_INVALID")
    params = dict(request["params"])
    cursor = params.pop("cursor", "")
    require(isinstance(cursor, str) and len(cursor) <= 2048, "CURSOR_INVALID")
    start, end = params.get("startTime", 1), params.get("endTime", 2)
    allowed = plan(source, start, end)
    stripped = {"path": request["path"], "params": params}
    if source == "public" and request["path"] == "/v5/market/mark-price-kline":
        require(not cursor and type(params.get("start")) is int, "MARK_REQUEST_INVALID")
        require(stripped == mark_request(params["start"] + 60_000), "MARK_REQUEST_INVALID")
    else:
        require(stripped in allowed.values(), "REQUEST_NOT_ALLOWED")
        if source == "public":
            require(not cursor, "PUBLIC_CURSOR_UNEXPECTED")


def credentials(env: dict[str, str], allow_legacy: bool) -> tuple[str, str]:
    pair = tuple(env.get(name, "").strip() for name in KEY_NAMES[:2])
    if not any(pair) and allow_legacy:
        pair = tuple(env.get(name, "").strip() for name in KEY_NAMES[2:])
    require(all(pair), "DEMO_CREDENTIALS_UNAVAILABLE")
    require(all(len(v) <= 4096 and not any(c.isspace() for c in v) for v in pair),
            "DEMO_CREDENTIALS_INVALID")
    return pair  # type: ignore[return-value]


def credential_env(path: pathlib.Path | None) -> dict[str, str]:
    if path is None:
        return {name: os.environ.get(name, "") for name in KEY_NAMES}
    # Parse only credential assignments, never source/execute an env file or
    # expand shell syntax. An explicit file does not mix with process secrets.
    require(path.is_file() and path.stat().st_size <= MAX_BYTES, "ENV_FILE_INVALID")
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if not sep or key not in KEY_NAMES:
            continue
        require(key not in result, "DUPLICATE_CREDENTIAL_ASSIGNMENT")
        value = value.strip()
        if value[:1] in ('"', "'"):
            require(len(value) >= 2 and value[-1] == value[0], "CREDENTIAL_QUOTE_INVALID")
            value = value[1:-1]
        require(not any(c in value for c in "$`\\\r\n"), "CREDENTIAL_EXPANSION_FORBIDDEN")
        result[key] = value
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("REDIRECT_FORBIDDEN")


class Transport:
    def __init__(self, source: str, pair: tuple[str, str] | None = None):
        require(source in DOMAINS, "SOURCE_NOT_ALLOWED")
        require((pair is not None) == (source == "demo"), "CREDENTIAL_SOURCE_MISMATCH")
        self.source, self.pair = source, pair
        paths = ssl.get_default_verify_paths()
        fallback = pathlib.Path("/etc/ssl/cert.pem")
        ctx = (ssl.create_default_context(cafile=str(fallback))
               if paths.cafile is None and fallback.is_file() else ssl.create_default_context())
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ctx), NoRedirect())

    def get(self, request: dict[str, Any]) -> bytes:
        validate_request(self.source, request)
        query = urlencode(request["params"])
        headers = {"User-Agent": "ai-trade-readonly-evidence/1"}
        if self.pair:
            key, secret = self.pair
            timestamp = str(now_ms())
            signature = hmac.new(secret.encode(), (timestamp + key + "5000" + query).encode(),
                                 hashlib.sha256).hexdigest()
            headers.update({"X-BAPI-API-KEY": key, "X-BAPI-SIGN": signature,
                            "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": "5000"})
        url = DOMAINS[self.source] + request["path"] + ("?" + query if query else "")
        try:
            with self.opener.open(Request(url, headers=headers, method="GET"), timeout=20) as response:
                require(response.status == 200, "HTTP_STATUS_INVALID")
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            # Never report response bodies, authenticated URLs or headers.
            raise ValueError(f"HTTP_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise ValueError("TRANSPORT_ERROR") from None
        require(0 < len(raw) <= MAX_BYTES, "RESPONSE_SIZE_INVALID")
        if self.pair:
            require(not any(v.encode() in raw for v in self.pair), "CREDENTIAL_ECHO_REJECTED")
        return raw


def safe_file(path: pathlib.Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)


def safe_read(path: pathlib.Path) -> bytes:
    require(not path.is_symlink() and path.is_file(), "ARCHIVE_FILE_INVALID")
    require(path.stat().st_size <= MAX_BYTES, "ARCHIVE_FILE_OVERSIZED")
    return path.read_bytes()


def result(raw: bytes) -> dict[str, Any]:
    payload = decode(raw)
    require(type(payload.get("retCode")) is int, "API_CODE_INVALID")
    require(payload["retCode"] == 0, "API_ERROR_" + str(payload["retCode"]))
    require(type(payload.get("time")) is int and payload["time"] > 0, "SERVER_TIME_INVALID")
    require(isinstance(payload.get("result"), dict), "API_RESULT_INVALID")
    return payload["result"]


def response_rows(raw: bytes, group: str) -> tuple[list[dict[str, Any]], str]:
    data = result(raw)
    if group == "account":
        require(data.get("marginMode") in ("ISOLATED_MARGIN", "REGULAR_MARGIN", "PORTFOLIO_MARGIN"),
                "MARGIN_MODE_INVALID")
        return [data], ""
    rows = data.get("list")
    require(isinstance(rows, list), "API_LIST_INVALID")
    require(all(isinstance(row, dict) for row in rows), "API_ROW_INVALID")
    if group not in ("transactions", "wallet"):
        require(data.get("category") == "linear", "CATEGORY_MISMATCH")
    cursor = data.get("nextPageCursor", "")
    require(isinstance(cursor, str) and len(cursor) <= 2048, "CURSOR_INVALID")
    return rows, cursor


def collect(root: pathlib.Path, source: str, start: int, end: int, transport: Any) -> pathlib.Path:
    requests = plan(source, start, end)
    require(end <= now_ms() - 60_000, "HISTORY_WINDOW_NOT_CLOSED")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not root.is_symlink(), "ARCHIVE_ROOT_SYMLINK")
    capture = pathlib.Path(tempfile.mkdtemp(prefix=source + "-", dir=root))
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA, "source": source, "start_ms": start, "end_ms": end,
        "domain": DOMAINS[source], "method": "GET", "created_ms": now_ms(),
        "engine_sha256": digest(pathlib.Path(__file__).read_bytes()), "pages": [], "errors": {},
    }

    def checkpoint() -> None:
        temporary = capture / "manifest.next"
        safe_file(temporary, encode(manifest))
        temporary.replace(capture / "manifest.json")

    def fetch(group: str, request: dict[str, Any]) -> bytes:
        require(len(manifest["pages"]) < MAX_PAGES, "CAPTURE_PAGE_LIMIT")
        sent = now_ms()
        raw = transport.get(request)
        received = now_ms()
        name = f"{len(manifest['pages']):04d}.raw"
        safe_file(capture / name, raw)
        manifest["pages"].append({"group": group, "request": request, "file": name,
                                  "sha256": digest(raw), "sent_ms": sent, "received_ms": received})
        checkpoint()  # Preserve bytes even when API/schema validation fails.
        return raw

    for group, request in requests.items():
        try:
            seen: set[str] = set()
            while True:
                rows, cursor = response_rows(fetch(group, request), group)
                if source == "public":
                    require(not cursor, "PUBLIC_PAGINATION_UNEXPECTED")
                    require(len(rows) < request["params"].get("limit", 1000), "PUBLIC_PAGE_SATURATED")
                if not cursor:
                    break
                require(cursor not in seen and rows, "CURSOR_STALLED")
                seen.add(cursor)
                request = {"path": request["path"], "params": {**request["params"], "cursor": cursor}}
        except (ValueError, KeyError, TypeError) as exc:
            # Error details are re-derived from retained raw during replay.
            manifest["errors"][group] = error_code(exc)
            checkpoint()
    if source == "public" and "funding" not in manifest["errors"]:
        raw = safe_read(capture / manifest["pages"][0]["file"])
        for row in response_rows(raw, "funding")[0]:
            try:
                when = epoch(row["fundingRateTimestamp"])
                require(start <= when <= end, "FUNDING_OUTSIDE_WINDOW")
                fetch(f"mark_{when}", mark_request(when))
            except (ValueError, KeyError, TypeError) as exc:
                manifest["errors"]["marks"] = error_code(exc)
                break
    checkpoint()
    return capture


def audit_demo(groups: dict[str, list[Any]], start: int, end: int) -> dict[str, Any]:
    gaps: set[str] = set()
    counts = {name: len(rows) for name, rows in groups.items()}
    account = groups.get("account", [])
    require(len(account) == 1, "ACCOUNT_SNAPSHOT_MISSING")
    mode = account[0]["marginMode"]
    wallet = groups.get("wallet", [])
    require(len(wallet) == 1 and wallet[0].get("accountType") == "UNIFIED", "WALLET_INVALID")
    usdt = [coin for coin in wallet[0].get("coin", []) if coin.get("coin") == "USDT"]
    require(len(usdt) <= 1, "DUPLICATE_WALLET_COIN")
    if not usdt:
        gaps.add("USDT_WALLET_NOT_RETURNED")
    # Account-wide IM/MM are not applicable in UTA isolated mode. Do not turn
    # blank/zero non-applicable fields into Cross margin evidence.
    margin_fields = ("totalInitialMargin", "totalMaintenanceMargin", "totalEquity")
    margin_present = mode != "ISOLATED_MARGIN" and all(wallet[0].get(k) not in (None, "") for k in margin_fields)
    if margin_present:
        for key in margin_fields:
            number(wallet[0][key])
    transactions: dict[str, dict[str, Any]] = {}
    trades: dict[tuple[str, str], dict[str, Any]] = {}
    funding_count = cash_ok = 0
    for row in groups.get("transactions", []):
        require(row.get("currency") == "USDT", "TRANSACTION_CURRENCY_MISMATCH")
        require(start <= epoch(row.get("transactionTime")) <= end, "TRANSACTION_OUTSIDE_WINDOW")
        identity = row.get("id")
        require(isinstance(identity, str) and identity and identity not in transactions,
                "DUPLICATE_OR_MISSING_TRANSACTION_ID")
        transactions[identity] = row
        # The official TRADE examples use funding="" (not applicable). Only
        # this documented, type-specific blank is a structural zero; missing
        # keys or blanks in other accounting fields remain missing evidence.
        funding_value = "0" if row.get("type") == "TRADE" and row.get("funding") == "" else row.get("funding")
        values = (row.get("cashFlow"), funding_value, row.get("fee"), row.get("change"))
        if any(v in (None, "") for v in values):
            gaps.add("CASH_IDENTITY_FIELDS_MISSING")
        else:
            cash, funding, fee, change = (number(v) for v in values)
            require(cash + funding - fee == change, "CASH_IDENTITY_MISMATCH")
            cash_ok += 1
        if row.get("type") == "SETTLEMENT":
            funding_count += 1
        if row.get("type") == "TRADE" and row.get("category") == "linear":
            key = (row.get("symbol"), row.get("tradeId"))
            require(all(isinstance(v, str) and v for v in key) and key not in trades,
                    "DUPLICATE_OR_MISSING_TRADE_ID")
            trades[key] = row
    executions: dict[tuple[str, str], dict[str, Any]] = {}
    matched = 0
    for row in groups.get("executions", []):
        require(start <= epoch(row.get("execTime")) <= end, "EXECUTION_OUTSIDE_WINDOW")
        key = (row.get("symbol"), row.get("execId"))
        require(all(isinstance(v, str) and v for v in key) and key not in executions,
                "DUPLICATE_OR_MISSING_EXECUTION_ID")
        executions[key] = row
        if row.get("execType") != "Trade":
            continue
        tx = trades.get(key)
        if tx is None:
            gaps.add("EXECUTION_WITHOUT_TRANSACTION")
            continue
        if row.get("feeCurrency") not in (None, "", "USDT"):
            gaps.add("EXECUTION_FEE_CURRENCY_UNSUPPORTED")
            continue
        require(row.get("orderId") == tx.get("orderId") and row.get("side") == tx.get("side"),
                "EXECUTION_IDENTITY_MISMATCH")
        for left, right in (("execFee", "fee"), ("execQty", "qty"), ("execPrice", "tradePrice")):
            require(number(row.get(left)) == number(tx.get(right)), "EXECUTION_LEDGER_VALUE_MISMATCH")
        matched += 1
    if set(trades) - set(executions):
        gaps.add("TRANSACTION_WITHOUT_EXECUTION")
    if not transactions:
        gaps.add("NO_TRANSACTION_EVIDENCE")
    if not matched:
        gaps.add("NO_MATCHED_TRADE_EVIDENCE")
    if not funding_count:
        gaps.add("NO_FUNDING_SETTLEMENT_OBSERVED")
    return {
        "counts": counts, "margin_mode": mode, "account_margin_fields_applicable": mode != "ISOLATED_MARGIN",
        "account_margin_fields_present": margin_present, "cash_identity_checked": cash_ok,
        "matched_execution_transactions": matched, "funding_settlement_records": funding_count,
        "gaps": sorted(gaps), "historical_margin_reconstructed": False,
        "snapshot_is_atomic": False, "history_exhaustion_proves_retention": False,
        "c2_option_accounting_qualified": False, "balances_or_cash_amounts_published": False,
    }


def audit_public(groups: dict[str, list[Any]], start: int, end: int) -> dict[str, Any]:
    rates: dict[int, str] = {}
    for row in groups.get("funding", []):
        require(row.get("symbol") == "BTCUSDT", "PUBLIC_SYMBOL_MISMATCH")
        when = epoch(row.get("fundingRateTimestamp"))
        require(start <= when <= end and when not in rates, "FUNDING_TIME_MISMATCH")
        require(abs(number(row.get("fundingRate"))) <= 1, "FUNDING_RATE_INVALID")
        rates[when] = row["fundingRate"]
    candles = 0
    for when in rates:
        rows = groups.get(f"mark_{when}", [])
        require(len(rows) == 3, "MARK_CONTEXT_INCOMPLETE")
        times: set[int] = set()
        for row in rows:
            require(isinstance(row, list) and len(row) == 5, "MARK_ROW_INVALID")
            stamp = epoch(row[0])
            require(stamp not in times, "DUPLICATE_MARK_CANDLE")
            times.add(stamp)
            op, hi, lo, cl = (number(v) for v in row[1:])
            require(0 < lo <= min(op, cl) <= max(op, cl) <= hi, "MARK_OHLC_INVALID")
        require(times == {when - 60_000, when, when + 60_000}, "MARK_TIME_MISMATCH")
        candles += len(rows)
    instruments, risks = groups.get("instrument", []), groups.get("risk_limit", [])
    require(len(instruments) == 1 and instruments[0].get("symbol") == "BTCUSDT" and
            instruments[0].get("settleCoin") == "USDT", "INSTRUMENT_INVALID")
    require(risks and all(row.get("symbol") == "BTCUSDT" for row in risks), "RISK_PARAMETERS_INVALID")
    return {"counts": {name: len(rows) for name, rows in groups.items()},
            "settled_rates": [{"timestamp_ms": when, "rate": rates[when]} for when in sorted(rates)],
            "mark_context_candles": candles, "gaps": [] if rates else ["NO_FUNDING_EVIDENCE"],
            "exact_funding_marks_qualified": False, "historical_margin_parameters_qualified": False,
            "current_instrument_and_risk_parameters_only": True, "c2_option_accounting_qualified": False}


def replay(capture: pathlib.Path, expected_sha: str | None = None) -> dict[str, Any]:
    require(not capture.is_symlink(), "ARCHIVE_ROOT_SYMLINK")
    raw_manifest = safe_read(capture / "manifest.json")
    manifest = decode(raw_manifest)
    if expected_sha is not None:
        require(SHA.fullmatch(expected_sha) and digest(raw_manifest) == expected_sha, "MANIFEST_HASH_MISMATCH")
    require(manifest.get("schema_version") == SCHEMA, "SCHEMA_MISMATCH")
    source, start, end = manifest["source"], manifest["start_ms"], manifest["end_ms"]
    requests = plan(source, start, end)
    require(manifest.get("domain") == DOMAINS[source] and manifest.get("method") == "GET", "SOURCE_IDENTITY_MISMATCH")
    require(type(manifest.get("created_ms")) is int and end <= manifest["created_ms"] - 60_000,
            "HISTORY_WINDOW_NOT_CLOSED")
    require(isinstance(manifest.get("engine_sha256"), str) and SHA.fullmatch(manifest["engine_sha256"]),
            "ENGINE_IDENTITY_INVALID")
    pages = manifest["pages"]
    require(isinstance(pages, list) and len(pages) <= MAX_PAGES, "PAGE_COUNT_INVALID")
    require(isinstance(manifest.get("errors"), dict) and
            set(manifest["errors"]) <= set(requests) | {"marks"}, "ERROR_MANIFEST_INVALID")
    groups: dict[str, list[Any]] = {}
    complete: set[str] = set()
    failed: set[str] = set(manifest["errors"])
    codes = {name: error_code(ValueError(value)) for name, value in manifest["errors"].items()}
    cursors: dict[str, set[str]] = {}
    for index, page in enumerate(pages):
        group, request = page["group"], page["request"]
        require(group not in complete, "PAGE_AFTER_EXHAUSTION")
        if group.startswith("mark_"):
            require(source == "public" and group[5:].isdigit(), "MARK_GROUP_INVALID")
            when = int(group[5:])
            require(any(epoch(row["fundingRateTimestamp"]) == when for row in groups.get("funding", [])),
                    "UNREQUESTED_MARK_BOUNDARY")
            requests[group] = mark_request(when)
        require(group in requests and request == requests[group], "REQUEST_PLAN_MISMATCH")
        validate_request(source, request)
        require(page["file"] == f"{index:04d}.raw", "RAW_PATH_INVALID")
        raw = safe_read(capture / page["file"])
        require(digest(raw) == page["sha256"], "RAW_HASH_MISMATCH")
        require(type(page["sent_ms"]) is int and type(page["received_ms"]) is int and
                manifest["created_ms"] <= page["sent_ms"] <= page["received_ms"], "REQUEST_TIMING_INVALID")
        try:
            data = result(raw)
            server_time = decode(raw)["time"]
            require(page["sent_ms"] - 60_000 <= server_time <= page["received_ms"] + 60_000,
                    "SERVER_CLOCK_MISMATCH")
            if group.startswith("mark_"):
                require(data.get("category") == "linear" and data.get("symbol") == "BTCUSDT", "MARK_IDENTITY_MISMATCH")
                require(server_time >= int(group[5:]) + 120_000, "MARK_CANDLE_NOT_CLOSED")
                rows, cursor = data.get("list"), ""
                require(isinstance(rows, list), "MARK_LIST_INVALID")
            else:
                rows, cursor = response_rows(raw, group)
            require(len(rows) <= request["params"].get("limit", 1000), "PAGE_LIMIT_EXCEEDED")
            if source == "public" and not group.startswith("mark_"):
                require(not cursor and len(rows) < request["params"].get("limit", 1000), "PUBLIC_PAGE_NOT_EXHAUSTED")
            groups.setdefault(group, []).extend(rows)
            if not cursor:
                complete.add(group)
            else:
                require(rows and cursor not in cursors.setdefault(group, set()), "CURSOR_STALLED")
                cursors[group].add(cursor)
                requests[group] = {"path": request["path"], "params": {**request["params"], "cursor": cursor}}
        except (ValueError, KeyError, TypeError) as exc:
            failed.add(group)
            codes[group] = error_code(exc)
    missing = set(plan(source, start, end)) - complete
    summary: dict[str, Any] = {"schema_version": SCHEMA, "source": source,
        "start_ms": start, "end_ms": end, "manifest_sha256": digest(raw_manifest),
        "collector_sha256": manifest["engine_sha256"], "replayer_sha256": digest(pathlib.Path(__file__).read_bytes()),
        "response_pages": len(pages), "complete_groups": sorted(complete),
        "failed_or_incomplete_groups": sorted(failed | missing),
        "error_codes": codes,
        "promotion_authority": False, "order_submission": False, "account_mode_change": False}
    if failed or missing:
        summary.update(status="READONLY_CAPTURE_INCOMPLETE", gaps=["SOURCE_CAPTURE_INCOMPLETE"])
        return summary
    with localcontext() as ctx:
        ctx.prec = 100
        try:
            checks = audit_demo(groups, start, end) if source == "demo" else audit_public(groups, start, end)
        except (ValueError, KeyError, TypeError) as exc:
            summary.update(status="READONLY_CHECKS_FAILED", gaps=[error_code(exc)])
            return summary
    summary.update(checks)
    summary["status"] = "READONLY_CAPTURED_CHECKS_PASS" if not checks["gaps"] else "READONLY_CAPTURED_GAPS"
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "collect", "replay"))
    parser.add_argument("--source", choices=tuple(DOMAINS), default="demo")
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("data/research/bybit_readonly"))
    parser.add_argument("--capture", type=pathlib.Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--env-file", type=pathlib.Path)
    parser.add_argument("--allow-legacy-demo-credentials", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "replay":
            require(args.capture is not None, "CAPTURE_PATH_REQUIRED")
            report = replay(args.capture, args.expected_manifest_sha256)
        else:
            end = args.end_ms if args.end_ms is not None else now_ms() - 120_000
            start = args.start_ms if args.start_ms is not None else end - 7 * DAY + 1
            requests = plan(args.source, start, end)
            if args.action == "plan":
                print(encode({"source": args.source, "domain": DOMAINS[args.source], "method": "GET",
                              "start_ms": start, "end_ms": end, "requests": requests}).decode(), end="")
                return 0
            pair = credentials(credential_env(args.env_file), args.allow_legacy_demo_credentials) if args.source == "demo" else None
            capture = collect(args.root, args.source, start, end, Transport(args.source, pair))
            report = replay(capture)
            safe_file(capture / "summary.json", encode(report))
            # Archive path has no account identifiers; private contents are not printed.
            report["capture_directory"] = str(capture)
        print(encode(report).decode(), end="")
        return 0 if report["status"] in ("READONLY_CAPTURED_CHECKS_PASS", "READONLY_CAPTURED_GAPS") else 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # Static message prevents env values, server messages or account rows
        # escaping through exception repr / tracebacks into CI logs.
        print(encode({"schema_version": SCHEMA, "status": "READONLY_NOT_COMPLETED",
                      "source": args.source, "reason": error_code(exc),
                      "promotion_authority": False}).decode(), end="")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
