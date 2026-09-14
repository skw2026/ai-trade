#!/usr/bin/env python3
"""Offline USDT Cross account/active-order reference, never exchange-qualified.

One-way BTCUSDT and BTC USDT options. Explicit rule, price and order inputs;
no network, credentials, transfers, production integration or promotion.
"""
from __future__ import annotations

import argparse
import copy
import pathlib
import re
from decimal import Decimal

import audit_bybit_readonly_evidence as wire
import audit_option_subaccount_ledger as ledger
import bybit_cross_margin_reference as ref

ZERO, ONE = Decimal(0), Decimal(1)
SCHEMA = "bybit_cross_account_input_v1"
AUTHORITY = {"promotion_authority": False, "order_submission": False,
             "account_mode_change": False, "production_integrated": False}
require, fields = ledger.require, ledger.fields


def dec(value, *, positive=False, nonnegative=False):
    return ledger.number(value, "reference number", positive=positive, nonnegative=nonnegative)


def text(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: text(val) for key, val in value.items()}
    if isinstance(value, list):
        return [text(val) for val in value]
    return value


def risk_tier(value, rules):
    tiers = rules["tiers"]
    require(isinstance(tiers, list) and 0 < len(tiers) <= 100, "RISK_TIERS_REQUIRED")
    prev_limit = prev_rate = deduction = ZERO
    selected = None
    lev = dec(rules["leverage"], positive=True)
    require(lev >= 1, "LEVERAGE_BELOW_ONE")
    for tier in tiers:
        fields(tier, {"limit_usdt", "mmr", "mm_deduction_usdt", "max_leverage"}, "risk tier")
        limit, rate = dec(tier["limit_usdt"], positive=True), dec(tier["mmr"], positive=True)
        maximum = dec(tier["max_leverage"], positive=True)
        require(limit > prev_limit and prev_rate <= rate < 1 and maximum >= 1, "RISK_TIER_ORDER_INVALID")
        deduction += prev_limit * (rate - prev_rate)
        require(dec(tier["mm_deduction_usdt"], nonnegative=True) == deduction, "RISK_DEDUCTION_MISMATCH")
        if selected is None and value <= limit:
            require(lev <= maximum and ONE / lev >= rate, "LEVERAGE_EXCEEDS_EFFECTIVE_TIER")
            selected = rate, deduction
        prev_limit, prev_rate = limit, rate
    require(selected is not None, "RISK_TIER_COVERAGE_MISSING")
    return selected


def validate_rules(rules):
    fields(rules, {"schema_version", "basis", "source_id", "historical_applicability_qualified",
                   "option_factors", "linear", "usdt_collateral_ratio"}, "rules")
    require(rules["schema_version"] == "bybit_cross_rules_v1" and
            rules["basis"] in ("synthetic_fixture", "current_rule_research") and
            isinstance(rules["source_id"], str) and 0 < len(rules["source_id"]) <= 200 and
            rules["historical_applicability_qualified"] is False, "UNVERIFIED_RULE_BASIS_REQUIRED")
    require(dec(rules["usdt_collateral_ratio"]) == 1, "ONLY_UNIT_USDT_COLLATERAL_SUPPORTED")
    fields(rules["option_factors"], set(ref.BTC_REFERENCE), "option factors")
    require(rules["option_factors"]["historical_applicability_qualified"] is False, "OPTION_HISTORY_NOT_QUALIFIED")
    ref.factors(rules["option_factors"])
    linear = rules["linear"]
    fields(linear, {"leverage", "open_fee_rate", "close_reserve_fee_rate", "tiers"}, "linear rules")
    for name in ("open_fee_rate", "close_reserve_fee_rate"):
        require(0 <= dec(linear[name]) < 1, "INVALID_LINEAR_FEE_RATE")
    risk_tier(ZERO, linear)


def price(row, now, max_age, *, allow_zero=False):
    fields(row, {"value", "ts_ms", "source_kind"}, "price")
    stamp = ledger.integer(row["ts_ms"], "price timestamp")
    require(0 <= now - stamp <= max_age, "PRICE_STALE_OR_FUTURE")
    require(row["source_kind"] in ("synthetic", "exchange_response", "local_receipt",
                                   "prior_closed_candle_proxy"), "PRICE_SOURCE_INVALID")
    return dec(row["value"], positive=not allow_zero, nonnegative=allow_zero)


def validate_order(order, instruments, now):
    fields(order, {"id", "symbol", "side", "intent", "remaining_qty", "limit_price", "created_ms"}, "active order")
    require(isinstance(order["id"], str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", order["id"]), "ORDER_ID_INVALID")
    require(order["symbol"] in instruments and order["side"] in ("Buy", "Sell") and
            order["intent"] in ("open", "reduce"), "ORDER_SCOPE_INVALID")
    qty = dec(order["remaining_qty"], positive=True)
    require(qty % dec(instruments[order["symbol"]]["qty_step"], positive=True) == 0, "ORDER_OFF_LOT_GRID")
    dec(order["limit_price"], positive=True)
    require(ledger.integer(order["created_ms"], "order creation") <= now, "FUTURE_ORDER")


@ref.exact
def calculate(state, rules):
    validate_rules(rules)
    fields(state, {"schema_version", "evidence_kind", "scope_id", "margin_mode", "currency", "ts_ms",
                   "price_max_age_ms", "wallet_usdt", "liabilities_usdt", "other_assets", "instruments",
                   "index", "usdt_usd", "marks", "positions", "orders"}, "account state")
    require(state["schema_version"] == SCHEMA and state["evidence_kind"] in
            ("synthetic_fixture", "explicit_research_model"), "RESEARCH_STATE_REQUIRED")
    require(state["scope_id"] == "offline_subaccount" and state["margin_mode"] == "REGULAR_MARGIN" and
            state["currency"] == "USDT", "OFFLINE_CROSS_USDT_SCOPE_REQUIRED")
    require(state["other_assets"] == [] and dec(state["liabilities_usdt"]) == 0, "ASSETS_OR_LIABILITIES_UNSUPPORTED")
    now = ledger.integer(state["ts_ms"], "account timestamp")
    age = ledger.integer(state["price_max_age_ms"], "price max age")
    require(age > 0, "POSITIVE_PRICE_AGE_REQUIRED")
    instruments = state["instruments"]
    require(isinstance(instruments, dict) and 1 <= len(instruments) <= 16, "INSTRUMENTS_REQUIRED")
    for symbol, meta in instruments.items():
        base = {"kind", "settle_coin", "quantity_unit", "qty_step"}
        fields(meta, base if symbol == "BTCUSDT" else base | {"strike", "expiry_ts_ms"}, "instrument")
        require(meta["settle_coin"] == "USDT" and meta["quantity_unit"] == "BTC", "INSTRUMENT_UNITS_INVALID")
        dec(meta["qty_step"], positive=True)
        if symbol == "BTCUSDT":
            require(meta["kind"] == "linear_perpetual", "HEDGE_KIND_INVALID")
        else:
            require(ledger.OPTION.fullmatch(symbol) and meta["kind"] in ("call", "put") and
                    symbol.split("-")[3] == ("C" if meta["kind"] == "call" else "P") and
                    dec(symbol.split("-")[2]) == dec(meta["strike"], positive=True), "OPTION_IDENTITY_INVALID")
            expiry = ledger.integer(meta["expiry_ts_ms"], "expiry")
            expected_date = ledger.dt.datetime.strptime(symbol.split("-")[1], "%d%b%y").date()
            require(ledger.dt.datetime.fromtimestamp(expiry / 1000, ledger.dt.timezone.utc).date() == expected_date,
                    "OPTION_EXPIRY_DATE_INVALID")
    index = price(state["index"], now, age)
    fx = price(state["usdt_usd"], now, age)
    wallet = dec(state["wallet_usdt"])
    positions, orders = state["positions"], state["orders"]
    require(isinstance(positions, dict) and set(positions) <= set(instruments) and
            isinstance(orders, list) and len(orders) <= 1000, "POSITIONS_OR_ORDERS_INVALID")
    needed = set(positions)
    identities = set()
    for order in orders:
        validate_order(order, instruments, now)
        require(order["id"] not in identities, "DUPLICATE_ACTIVE_ORDER")
        identities.add(order["id"])
        needed.add(order["symbol"])
    require(isinstance(state["marks"], dict) and set(state["marks"]) == needed, "MARK_COVERAGE_MISMATCH")
    marks = {symbol: price(row, now, age, allow_zero=symbol != "BTCUSDT") for symbol, row in state["marks"].items()}
    for symbol in needed:
        require(symbol == "BTCUSDT" or now <= instruments[symbol]["expiry_ts_ms"], "EXPIRED_OPTION_POSITION_OR_ORDER")
    pos_details = {}
    pos_im = pos_mm = upl = option_value = ZERO
    for symbol, pos in positions.items():
        fields(pos, {"qty", "entry"}, "position")
        qty, entry = dec(pos["qty"]), dec(pos["entry"], positive=True)
        require(qty != 0 and qty % dec(instruments[symbol]["qty_step"]) == 0, "POSITION_OFF_LOT_GRID_OR_ZERO")
        if symbol == "BTCUSDT":
            linear = rules["linear"]
            value = abs(qty) * marks[symbol]
            rate, deduction = risk_tier(value, linear)
            leverage = dec(linear["leverage"])
            close_fee = abs(qty) * entry * (ONE - (ONE if qty > 0 else -ONE) / leverage) * dec(linear["close_reserve_fee_rate"])
            detail = {"im_usdt": value / leverage + close_fee, "mm_usdt": value * rate - deduction + close_fee,
                      "estimated_close_fee_usdt": close_fee}
            upl += qty * (marks[symbol] - entry)
        else:
            meta = instruments[symbol]
            detail = ref.option_position(kind=meta["kind"], qty=pos["qty"], entry=pos["entry"],
                index=str(index), mark=str(marks[symbol]), strike=meta["strike"], rules=rules["option_factors"])
            option_value += detail["option_value_usdt"]
        pos_im += detail["im_usdt"]
        pos_mm += detail["mm_usdt"]
        pos_details[symbol] = detail
    balance, equity = wallet + upl, wallet + upl + option_value
    reduced: dict[str, Decimal] = {}
    opening_sides: dict[str, set] = {}
    linear_open_notional = ZERO
    for order in orders:
        symbol, qty = order["symbol"], dec(order["remaining_qty"])
        old = dec(positions[symbol]["qty"]) if symbol in positions else ZERO
        signed = qty * (1 if order["side"] == "Buy" else -1)
        require(symbol == "BTCUSDT" or now < instruments[symbol]["expiry_ts_ms"], "ORDER_AT_OR_AFTER_EXPIRY")
        if order["intent"] == "reduce":
            require(old * signed < 0, "REDUCE_ORDER_WRONG_SIDE_OR_NO_POSITION")
            reduced[symbol] = reduced.get(symbol, ZERO) + qty
            require(reduced[symbol] <= abs(old), "REDUCE_ORDERS_OVERRESERVE_POSITION")
            require(symbol == "BTCUSDT" or old < 0, "OPTION_SELL_TO_CLOSE_UNSUPPORTED")
        else:
            require(old * signed >= 0, "OPEN_ORDER_WOULD_CLOSE_OR_REVERSE")
            opening_sides.setdefault(symbol, set()).add(order["side"])
            require(len(opening_sides[symbol]) == 1, "OPPOSING_OPEN_ORDERS_UNSUPPORTED")
            if symbol == "BTCUSDT":
                linear_open_notional += qty * dec(order["limit_price"])
    # Simultaneous increase/reduce for one symbol needs an allocation rule;
    # reject rather than summing mutually inconsistent standalone formulas.
    require(not set(reduced) & set(opening_sides), "MIXED_OPEN_REDUCE_ALLOCATION_UNSUPPORTED")
    linear_value = abs(dec(positions["BTCUSDT"]["qty"])) * marks["BTCUSDT"] if "BTCUSDT" in positions else ZERO
    active_rate, _ = risk_tier(linear_value + linear_open_notional, rules["linear"])
    order_im = order_mm = order_loss = ZERO
    order_details = []
    for order in orders:
        symbol, qty, limit = order["symbol"], dec(order["remaining_qty"]), dec(order["limit_price"])
        if symbol == "BTCUSDT":
            side = ONE if order["side"] == "Buy" else -ONE
            loss = min(ZERO, side * (marks[symbol] - limit) * qty)
            fee_open = qty * limit * dec(rules["linear"]["open_fee_rate"])
            fee_close = qty * limit * (ONE - side / dec(rules["linear"]["leverage"])) * dec(rules["linear"]["close_reserve_fee_rate"])
            im = qty * limit / dec(rules["linear"]["leverage"]) + fee_open + fee_close if order["intent"] == "open" else ZERO
            mm = qty * limit * active_rate + fee_close if order["intent"] == "open" else ZERO
        else:
            meta = instruments[symbol]
            extra = {}
            if order["intent"] == "reduce":
                require(balance >= 0 and pos_im > 0, "CLOSE_RELEASE_UNDEFINED_AT_INSOLVENCY")
                # Keep intermediate Decimal precision; do not round-trip a
                # computed account margin through an external-input parser.
                release = qty / abs(dec(positions[symbol]["qty"])) * min(balance / pos_im, ONE) * pos_details[symbol]["im_usdt"]
                f = ref.factors(rules["option_factors"])
                fee = qty * min(f["taker_fee_rate"] * index, f["premium_fee_cap"] * limit)
                im, mm, loss = max(ZERO, qty * limit + fee - release), ZERO, ZERO
                order_im += im
                order_details.append({"id": order["id"], "im_usdt": im, "mm_usdt": mm, "order_loss_usdt": loss})
                continue
            action = "buy_to_close" if order["intent"] == "reduce" else "buy_to_open" if order["side"] == "Buy" else "sell_to_open"
            detail = ref.option_order(kind=meta["kind"], action=action, qty=order["remaining_qty"],
                price=order["limit_price"], index=str(index), mark=str(marks[symbol]),
                strike=meta["strike"], rules=rules["option_factors"], **extra)
            im, mm, loss = detail["order_im_usdt"], ZERO, ZERO
        order_im += im
        order_mm += mm
        order_loss += loss
        order_details.append({"id": order["id"], "im_usdt": im, "mm_usdt": mm, "order_loss_usdt": loss})
    total_im, total_mm = pos_im + order_im, pos_mm + order_mm
    # No spot orders and unit USDT collateral: haircut loss is structurally 0.
    denominator = balance + order_loss
    imr = total_im / denominator if denominator > 0 else None
    mmr = total_mm / denominator if denominator > 0 else None
    risks = []
    if wallet < 0: risks.append("NEGATIVE_WALLET_BORROWING_REVIEW")
    if equity <= 0: risks.append("NONPOSITIVE_EQUITY")
    if denominator <= 0: risks.append("NONPOSITIVE_MARGIN_DENOMINATOR")
    if imr is not None and imr >= 1: risks.append("IM_RATE_AT_LEAST_ONE")
    if mmr is not None and mmr >= 1: risks.append("MM_RATE_AT_LEAST_ONE")
    result = {"schema_version": "bybit_cross_account_report_v1", "status": "REFERENCE_RISK_REVIEW" if risks else "REFERENCE_CALCULATED",
        "wallet_usdt": wallet, "perp_upl_usdt": upl, "option_value_usdt": option_value,
        "total_equity_usd": equity * fx, "total_margin_balance_usd": balance * fx,
        "total_initial_margin_usd": total_im * fx, "total_maintenance_margin_usd": total_mm * fx,
        "total_available_balance_usd": (balance - total_im) * fx,
        "risk_headroom_after_order_loss_usd": (denominator - total_im) * fx,
        "margin_rate_denominator_usd": denominator * fx, "order_loss_usd": order_loss * fx,
        "haircut_loss_usd": ZERO, "account_im_rate": imr, "account_mm_rate": mmr,
        "position_initial_margin_usdt": pos_im, "position_maintenance_margin_usdt": pos_mm,
        "order_initial_margin_usdt": order_im, "order_maintenance_margin_usdt": order_mm,
        "positions": pos_details, "orders": order_details, "risk_reasons": risks,
        "historical_margin_qualified": False, "exchange_account_reconciled": False,
        "c2_qualified": False, "authorities": AUTHORITY.copy(),
        "remaining_requirements": ["historical_rule_and_fee_applicability", "independent_cross_account_reference",
            "causal_price_and_order_state_qualification", "linear_order_mm_close_reserve_convention"]}
    return text(result)


class OrderBook:
    """Explicit synthetic/research lifecycle; no endpoint or exchange order ID use."""
    def __init__(self):
        self.active = {}
        self.seen = set()
        self.last_ms = 0

    @ref.exact
    def apply(self, event, instruments):
        fields(event, {"type", "ts_ms"}, "order event", {"order", "id", "qty", "price"})
        now = ledger.integer(event["ts_ms"], "order event timestamp")
        require(now >= self.last_ms, "ORDER_EVENT_TIME_REVERSED")
        candidate = copy.deepcopy(self.active)
        if event["type"] == "PLACE":
            fields(event, {"type", "ts_ms", "order"}, "place")
            order = event["order"]
            validate_order(order, instruments, now)
            require(order["created_ms"] == now and order["id"] not in self.seen, "ORDER_ID_REUSED_OR_CREATION_MISMATCH")
            candidate[order["id"]] = copy.deepcopy(order)
        elif event["type"] == "CANCEL":
            fields(event, {"type", "ts_ms", "id"}, "cancel")
            require(event["id"] in candidate, "CANCEL_UNKNOWN_ORDER")
            del candidate[event["id"]]
        elif event["type"] == "FILL":
            fields(event, {"type", "ts_ms", "id", "qty", "price"}, "fill")
            require(event["id"] in candidate, "FILL_UNKNOWN_ORDER")
            order = candidate[event["id"]]
            qty, fill_price = dec(event["qty"], positive=True), dec(event["price"], positive=True)
            require(qty % dec(instruments[order["symbol"]]["qty_step"]) == 0 and
                    qty <= dec(order["remaining_qty"]), "FILL_OFF_GRID_OR_OVERFILL")
            require(fill_price <= dec(order["limit_price"]) if order["side"] == "Buy" else
                    fill_price >= dec(order["limit_price"]), "FILL_WORSE_THAN_LIMIT")
            left = dec(order["remaining_qty"]) - qty
            if left:
                order["remaining_qty"] = str(left)
            else:
                del candidate[event["id"]]
        else:
            raise ValueError("ORDER_EVENT_UNSUPPORTED")
        self.active, self.last_ms = candidate, now
        if event["type"] == "PLACE": self.seen.add(event["order"]["id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=pathlib.Path, required=True)
    parser.add_argument("--rules", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        state, state_hash = ledger.read_input(args.input)
        rules, rules_hash = ledger.read_input(args.rules)
        report = calculate(state, rules)
        report.update(input_sha256=state_hash, rules_sha256=rules_hash,
                      engine_sha256=wire.digest(pathlib.Path(__file__).read_bytes()))
        print(wire.encode(report).decode(), end="")
        return 0
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        print(wire.encode({"status": "CROSS_REFERENCE_INVALID", "reason": wire.error_code(exc),
                           "authorities": AUTHORITY.copy()}).decode(), end="")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
