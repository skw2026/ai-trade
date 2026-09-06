#!/usr/bin/env python3
"""Offline-only USDT option/linear account arithmetic, NOT a margin simulator.

Consumes explicit research events and supplied account-level margin snapshots.
No exchange clients, secrets, transfers, orders or promotion path. Even consistent
local input is unverified research, never evidence of actual account performance.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import pathlib
import re
from typing import Any


ZERO = Decimal(0)
ONE = Decimal(1)
TOL = Decimal("0.00000001")
MAX_BYTES = 4 * 1024 * 1024
OPTION = re.compile(r"BTC-\d{1,2}[A-Z]{3}\d{2}-\d+(?:\.\d+)?-[CP]-USDT\Z")
PERMISSIONS = {"design": True, "offline_validation": True, "account_creation": False,
               "account_mode_change": False, "fund_transfer": False,
               "credential_access": False, "order_submission": False,
               "demo_activation": False, "live_activation": False}
SCOPE = {
    "schema_version": "option_subaccount_research_scope_v1",
    "mode": "offline_design_only", "venue": "bybit",
    "account_boundary": "dedicated_subaccount", "margin_mode": "REGULAR_MARGIN",
    "settlement_currency": "USDT", "subaccount_uid": None,
    "approved_capital_usdt": None, "automatic_top_up": False,
    "assume_loss_capped_by_deposit": False,
    "borrowing_policy": "reject_any_observed_liability", "permissions": PERMISSIONS,
}
AUTHORITIES = {"promotion_authority": False, "demo_activation_authorized": False,
               "live_activation_authorized": False, "order_submission_authorized": False}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def fields(row: Any, expected: set[str], name: str, optional: set[str] | None = None) -> None:
    require(isinstance(row, dict), f"{name}: object required")
    keys = set(row)
    require(expected <= keys and keys <= expected | (optional or set()), f"{name}: missing/unknown fields")


def number(value: Any, name: str, *, positive: bool = False,
           nonnegative: bool = False) -> Decimal:
    require(isinstance(value, str) and 0 < len(value) <= 48, f"{name}: decimal string required")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name}: invalid decimal") from error
    require(result.is_finite() and abs(result) <= Decimal("1e18"), f"{name}: invalid magnitude")
    require(result.as_tuple().exponent >= -18, f"{name}: excessive decimal precision")
    require(not positive or result > 0, f"{name}: must be positive")
    require(not nonnegative or result >= 0, f"{name}: must be nonnegative")
    return result


def integer(value: Any, name: str) -> int:
    require(type(value) is int and 0 <= value < 10**15, f"{name}: invalid integer")
    return value


def close(actual: Decimal, expected: Decimal, name: str) -> None:
    require(abs(actual - expected) <= TOL, f"{name}: reconciliation mismatch")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def text_number(value: Decimal) -> str:
    return format(value, "f")


def quote(row: dict[str, Any], now: int, max_age: int) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    fields(row, {"ts_ms", "bid", "ask", "bid_size", "ask_size"}, "quote", {"mark"})
    age = now - integer(row["ts_ms"], "quote.ts_ms")
    require(0 <= age <= max_age, "quote stale or from future")
    bid = number(row["bid"], "bid", nonnegative=True)
    ask = number(row["ask"], "ask", positive=True)
    require(bid <= ask, "crossed quote")
    return bid, ask, number(row["bid_size"], "bid_size", positive=True), number(
        row["ask_size"], "ask_size", positive=True)


def _audit(scope: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    # JSON comparison also rejects 0/1 substituted for booleans.
    require(canonical(scope) == canonical(SCOPE), "offline scope changed or permissions expanded")
    fields(data, {"schema_version", "evidence_kind", "scope_id", "simulation_capital_usdt",
                  "illustrative_limits", "start_ts_ms", "end_ts_ms", "instruments",
                  "funding_schedule", "events"}, "input")
    require(data["schema_version"] == "option_subaccount_ledger_input_v1", "input schema mismatch")
    require(data["evidence_kind"] in ("synthetic_fixture", "local_unverified_research"), "unsupported provenance")
    require(data["scope_id"] == "offline_subaccount", "account scope mismatch")
    capital = number(data["simulation_capital_usdt"], "simulation capital", positive=True)
    limits = data["illustrative_limits"]
    fields(limits, {"im_rate_max", "mm_rate_reduce", "mm_rate_exit", "drawdown_exit",
                    "quote_max_age_ms", "checkpoint_max_gap_ms"}, "illustrative limits")
    im_limit = number(limits["im_rate_max"], "im limit", positive=True)
    mm_reduce = number(limits["mm_rate_reduce"], "mm reduce", positive=True)
    mm_exit = number(limits["mm_rate_exit"], "mm exit", positive=True)
    dd_exit = number(limits["drawdown_exit"], "drawdown exit", positive=True)
    require(im_limit < ONE and mm_reduce < mm_exit < ONE and dd_exit < ONE, "invalid illustrative limits")
    quote_age = integer(limits["quote_max_age_ms"], "quote age")
    gap_limit = integer(limits["checkpoint_max_gap_ms"], "checkpoint gap")
    require(quote_age > 0 and gap_limit > 0, "zero age/gap limit")
    start = integer(data["start_ts_ms"], "start")
    end = integer(data["end_ts_ms"], "end")
    require(end > start, "empty observation interval")
    instruments = data["instruments"]
    require(isinstance(instruments, dict) and 1 <= len(instruments) <= 16, "invalid instrument count")
    for symbol, meta in instruments.items():
        meta_fields = {"kind", "settle_coin", "quantity_unit", "qty_step"}
        fields(meta, meta_fields if meta.get("kind") == "linear_perpetual" else
               meta_fields | {"strike", "expiry_ts_ms"}, "instrument")
        require(meta["settle_coin"] == "USDT" and meta["quantity_unit"] == "BTC", "instrument units mismatch")
        number(meta["qty_step"], "quantity step", positive=True)
        if meta["kind"] == "linear_perpetual":
            require(symbol == "BTCUSDT", "unsupported hedge")
        else:
            require(meta["kind"] in ("call", "put") and bool(OPTION.fullmatch(symbol)), "unsupported option")
            parts = symbol.split("-")
            require(parts[3] == ("C" if meta["kind"] == "call" else "P"), "option side mismatch")
            require(number(meta["strike"], "strike", positive=True) == number(parts[2], "symbol strike"), "strike mismatch")
            require(integer(meta["expiry_ts_ms"], "expiry") > start, "expired at start")
            expiry_date = dt.datetime.fromtimestamp(meta["expiry_ts_ms"] / 1000, dt.timezone.utc).date()
            require(expiry_date == dt.datetime.strptime(parts[1], "%d%b%y").date(), "expiry symbol/date mismatch")

    funding_schedule: dict[str, tuple[int, str]] = {}
    require(isinstance(data["funding_schedule"], list) and len(data["funding_schedule"]) <= 10000, "invalid funding schedule size")
    for row in data["funding_schedule"]:
        fields(row, {"settlement_id", "ts_ms", "symbol"}, "funding schedule")
        key = row["settlement_id"]
        require(isinstance(key, str) and bool(key) and key not in funding_schedule, "duplicate funding schedule")
        when = integer(row["ts_ms"], "funding schedule time")
        require(start <= when <= end and row["symbol"] == "BTCUSDT" and "BTCUSDT" in instruments, "invalid funding schedule")
        require((when, row["symbol"]) not in funding_schedule.values(), "duplicate funding settlement boundary")
        funding_schedule[key] = (when, row["symbol"])

    events = data["events"]
    require(isinstance(events, list) and 2 <= len(events) <= 10000, "invalid event count")
    require(events[0]["type"] == "MARK" and events[0]["ts_ms"] == start, "initial flat checkpoint required")
    require(events[-1]["ts_ms"] == end, "final checkpoint missing")
    cash = capital
    high_water = capital
    max_drawdown = ZERO
    positions = {symbol: ZERO for symbol in instruments}
    averages = positions.copy()
    totals = {name: ZERO for name in ("option_cashflow", "hedge_realized_pnl", "funding", "fees")}
    seen: set[str] = set()
    settled: set[str] = set()
    delivery_prices: dict[int, Decimal] = {}
    funded: set[str] = set()
    checkpoints = []
    missing: set[str] = set()
    breaches: set[str] = set()
    exit_latched = False
    previous_time = start
    for seq, event in enumerate(events):
        event_fields = {"seq", "id", "scope_id", "type", "ts_ms", "valuation"}
        kind_fields = {
            "MARK": set(),
            "FILL": {"symbol", "signed_qty_btc", "price_usdt_per_btc", "fee_usdt", "execution_quote"},
            "FUNDING": {"symbol", "settlement_id", "rate_kind", "position_qty_btc", "rate",
                        "mark_usdt_per_btc", "cash_delta_usdt"},
            "DELIVERY": {"symbol", "price_kind", "delivery_price", "cash_delta_before_fee_usdt", "fee_usdt"},
        }
        require(event["type"] in kind_fields, "unsupported event (transfers/borrowing prohibited)")
        fields(event, event_fields | kind_fields[event["type"]], "event", {"margin"})
        require(event["seq"] == seq and type(event["seq"]) is int, "event sequence mismatch")
        key = event["id"]
        require(isinstance(key, str) and bool(key) and key not in seen, "duplicate event identity")
        seen.add(key)
        require(event["scope_id"] == data["scope_id"], "mixed account events")
        now = integer(event["ts_ms"], "event timestamp")
        require(previous_time <= now <= end, "event time moved backwards or beyond end")
        if now - previous_time > gap_limit:
            missing.add("CHECKPOINT_GAP")
        previous_time = now
        kind = event["type"]
        if kind != "MARK":
            symbol = event["symbol"]
            require(symbol in instruments, "unknown instrument")
            meta = instruments[symbol]
            old = positions[symbol]
            if kind == "FILL":
                require(symbol not in settled and (meta["kind"] == "linear_perpetual" or now < meta["expiry_ts_ms"]), "fill after expiry")
                qty = number(event["signed_qty_btc"], "fill qty")
                require(qty != 0 and qty % number(meta["qty_step"], "qty step") == 0, "invalid lot")
                price = number(event["price_usdt_per_btc"], "fill price", positive=True)
                fee = number(event["fee_usdt"], "fill fee", nonnegative=True)
                bid, ask, bid_size, ask_size = quote(event["execution_quote"], now, quote_age)
                require((qty > 0 and qty <= ask_size and price >= ask) or
                        (qty < 0 and -qty <= bid_size and price <= bid), "fill violates taker price/quantity")
                new = old + qty
                if meta["kind"] == "linear_perpetual":
                    realized = min(abs(old), abs(qty)) * (price - averages[symbol]) * (ONE if old > 0 else -ONE) if old * qty < 0 else ZERO
                    cash += realized
                    totals["hedge_realized_pnl"] += realized
                    if new == 0:
                        averages[symbol] = ZERO
                    elif old * qty >= 0:
                        averages[symbol] = (abs(old) * averages[symbol] + abs(qty) * price) / abs(new)
                    elif old * new < 0:
                        averages[symbol] = price
                else:
                    premium = -qty * price
                    cash += premium
                    totals["option_cashflow"] += premium
                positions[symbol] = new
                cash -= fee
                totals["fees"] += fee
            elif kind == "FUNDING":
                settlement_id = event["settlement_id"]
                require(meta["kind"] == "linear_perpetual" and settlement_id not in funded and
                        funding_schedule.get(settlement_id) == (now, symbol), "funding identity/time mismatch")
                require(event["rate_kind"] == "settled_rate", "predicted funding rate prohibited")
                require(number(event["position_qty_btc"], "funding position") == old, "funding position mismatch")
                rate = number(event["rate"], "settled funding rate")
                require(abs(rate) <= ONE, "funding rate out of scope")
                amount = -old * number(event["mark_usdt_per_btc"], "funding mark", positive=True) * rate
                close(number(event["cash_delta_usdt"], "funding cash delta"), amount, "funding cashflow")
                cash += amount
                totals["funding"] += amount
                funded.add(settlement_id)
            elif kind == "DELIVERY":
                require(meta["kind"] != "linear_perpetual" and symbol not in settled and old != 0 and now == meta["expiry_ts_ms"], "invalid delivery lifecycle")
                require(event["price_kind"] == "official_delivery", "predicted delivery prohibited")
                underlying = number(event["delivery_price"], "delivery price", positive=True)
                require(now not in delivery_prices or delivery_prices[now] == underlying,
                        "inconsistent BTC delivery price at same expiry")
                delivery_prices[now] = underlying
                strike = number(meta["strike"], "strike")
                intrinsic = max(ZERO, underlying - strike if meta["kind"] == "call" else strike - underlying)
                amount = old * intrinsic
                fee = number(event["fee_usdt"], "delivery fee", nonnegative=True)
                close(number(event["cash_delta_before_fee_usdt"], "delivery amount"), amount, "delivery cashflow")
                cash += amount - fee
                totals["option_cashflow"] += amount
                totals["fees"] += fee
                positions[symbol] = ZERO
                settled.add(symbol)

        open_positions = {symbol: qty for symbol, qty in positions.items() if qty != 0}
        marks = event["valuation"]
        require(set(marks) == set(open_positions), "incomplete/extra position valuation")
        nav = bbo_nav = cash
        for symbol, qty in open_positions.items():
            meta = instruments[symbol]
            row = marks[symbol]
            bid, ask, bid_size, ask_size = quote(row, now, quote_age)
            if (qty > 0 and (bid == 0 or qty > bid_size)) or (qty < 0 and -qty > ask_size):
                missing.add("EXIT_BBO_DEPTH_INSUFFICIENT")
            mark = number(row["mark"], "mark", nonnegative=meta["kind"] != "linear_perpetual",
                          positive=meta["kind"] == "linear_perpetual")
            basis = averages[symbol] if meta["kind"] == "linear_perpetual" else ZERO
            nav += qty * (mark - basis)
            bbo_nav += qty * ((bid if qty > 0 else ask) - basis)
            if meta["kind"] != "linear_perpetual" and now > meta["expiry_ts_ms"]:
                missing.add("OVERDUE_OPTION_DELIVERY")
        high_water = max(high_water, nav)
        drawdown = max(ZERO, (high_water - nav) / high_water)
        max_drawdown = max(max_drawdown, drawdown)
        reasons: set[str] = set()
        if nav <= 0 or drawdown >= dd_exit:
            reasons.add("NAV_OR_DRAWDOWN_EXIT")
            exit_latched = True
        if cash < 0:
            reasons.add("NEGATIVE_USDT_WALLET")
            exit_latched = True
        margin = event.get("margin")
        if margin is None:
            missing.add("MARGIN_EVIDENCE_MISSING")
        else:
            fields(margin, {"scope_id", "margin_mode", "ts_ms", "positions_btc", "wallet_usdt",
                            "usdt_usd_index", "total_equity_usd", "total_initial_margin_usd",
                            "total_maintenance_margin_usd", "account_im_rate", "account_mm_rate",
                            "total_available_balance_usd", "liabilities_usdt"}, "margin")
            require(margin["scope_id"] == data["scope_id"] and margin["margin_mode"] == "REGULAR_MARGIN", "margin scope mismatch")
            require(margin["ts_ms"] == now and type(margin["ts_ms"]) is int, "margin must be same-checkpoint post-event evidence")
            require(set(margin["positions_btc"]) == set(open_positions), "margin position set mismatch")
            for symbol, qty in open_positions.items():
                require(number(margin["positions_btc"][symbol], "margin position") == qty, "margin position mismatch")
            close(number(margin["wallet_usdt"], "margin wallet"), cash, "wallet")
            fx = number(margin["usdt_usd_index"], "USDT USD index", positive=True)
            close(number(margin["total_equity_usd"], "account equity"), nav * fx, "account equity")
            im = number(margin["total_initial_margin_usd"], "initial margin", nonnegative=True)
            mm = number(margin["total_maintenance_margin_usd"], "maintenance margin", nonnegative=True)
            imr = number(margin["account_im_rate"], "account IM rate", nonnegative=True)
            mmr = number(margin["account_mm_rate"], "account MM rate", nonnegative=True)
            available = number(margin["total_available_balance_usd"], "available balance")
            debt = number(margin["liabilities_usdt"], "liabilities", nonnegative=True)
            require(im >= mm and (im == 0 or imr > 0) and (mm == 0 or mmr > 0), "inconsistent supplied margin")
            if imr >= im_limit or mmr >= mm_reduce or available < 0:
                reasons.add("ACCOUNT_MARGIN_LIMIT")
            if mmr >= mm_exit or debt > 0:
                reasons.add("ACCOUNT_MARGIN_EXIT_OR_LIABILITY")
                exit_latched = True
        breaches.update(reasons)
        checkpoints.append({"id": key, "ts_ms": now, "cash_usdt": text_number(cash),
                            "nav_usdt": text_number(nav),
                            "bbo_nav_before_future_close_fees_usdt": text_number(bbo_nav),
                            "drawdown": text_number(drawdown), "risk_reasons": sorted(reasons),
                            "exit_review_latched": exit_latched})
    if set(funding_schedule) != funded:
        missing.add("SCHEDULED_FUNDING_MISSING")
    if any(qty != 0 for qty in positions.values()):
        missing.add("OPEN_LIFECYCLE_AT_END")
    close(cash - capital, totals["option_cashflow"] + totals["hedge_realized_pnl"] + totals["funding"] - totals["fees"], "cash identity")
    return {
        "status": "RISK_REJECTED_OFFLINE" if breaches else "INSUFFICIENT_EVIDENCE" if missing else "PASS_OFFLINE_ACCOUNTING_ONLY",
        "input_evidence_kind": data["evidence_kind"], "missing_evidence": sorted(missing),
        "risk_breaches": sorted(breaches), "checkpoints": checkpoints,
        "totals_usdt": {key: text_number(value) for key, value in totals.items()},
        "final_cash_usdt": text_number(cash), "final_nav_usdt": text_number(nav),
        "pnl_on_simulated_capital_usdt": text_number(nav - capital),
        "return_on_simulated_capital": text_number((nav - capital) / capital),
        "max_drawdown_observed_checkpoints_only": text_number(max_drawdown),
        "funding_event_count": len(funded), "exit_review_latched": exit_latched,
    }


def audit(scope: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    try:
        with localcontext() as context:
            context.prec = 60
            result = _audit(scope, data)
    except (ValueError, KeyError, TypeError, InvalidOperation, IndexError, AttributeError, OverflowError) as error:
        result = {"status": "TECHNICALLY_INVALID", "error": str(error)}
    result.update({"schema_version": "option_subaccount_ledger_audit_v1",
                   "authorities": AUTHORITIES.copy(), "economic_qualification": False,
                   "historical_data_qualified": False, "exchange_margin_model_validated": False,
                   "actual_account_performance": False, "production_integrated": False,
                   "remaining_requirements": ["independently_verified_continuous_input_and_funding_schedule",
                       "historical_fee_and_exchange_margin_model", "pathwise_tail_liquidity_and_depeg_stress",
                       "approved_capital_and_account_permissions", "independent_economic_and_forward_qualification"]})
    return result


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def read_input(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(0 < len(raw) <= MAX_BYTES, "input exceeds byte budget or is empty")
    payload = json.loads(raw, object_pairs_hook=unique_object)
    require(isinstance(payload, dict), "JSON object required")
    return payload, hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True, type=pathlib.Path)
    parser.add_argument("--input", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        scope, scope_hash = read_input(args.scope)
        data, input_hash = read_input(args.input)
        result = audit(scope, data)
        result.update({"scope_sha256": scope_hash, "input_sha256": input_hash,
                       "engine_sha256": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()})
    except (OSError, ValueError) as error:
        result = audit({}, {})
        result["error"] = str(error)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "PASS_OFFLINE_ACCOUNTING_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
