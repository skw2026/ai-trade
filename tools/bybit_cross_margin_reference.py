#!/usr/bin/env python3
"""Decimal reference arithmetic, not an exchange-qualified margin engine.

USDT options / one-way linear positions, current published rule examples only.
No account access, historical parameter inference, portfolio offsets, liquidation
simulation, order netting, borrowing, or conversion into qualified C2 snapshots.
Official sources and unsupported cases: docs/plans/2026-09-13-cross-margin-reference.md.
"""
from decimal import Decimal, localcontext
from functools import wraps

from audit_bybit_readonly_evidence import number, require

ZERO, ONE = Decimal(0), Decimal(1)
BTC_REFERENCE = {
    "underlying": "BTC", "settlement": "USDT", "source_updated": "2026-05-22",
    "historical_applicability_qualified": False,
    "mm_factor": "0.03", "max_im_factor": "0.10", "min_im_factor": "0.05",
    "liquidation_fee_rate": "0.002", "taker_fee_rate": "0.0003", "premium_fee_cap": "0.07",
}


def exact(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with localcontext() as ctx:
            ctx.prec = 100
            return fn(*args, **kwargs)
    return call


def positive(text, *, zero=False):
    value = number(text)
    require(value >= 0 if zero else value > 0, "NONNEGATIVE_OR_POSITIVE_VALUE_REQUIRED")
    return value


def factors(rules):
    require(rules.get("underlying") == "BTC" and rules.get("settlement") == "USDT",
            "ONLY_BTC_USDT_OPTIONS_SUPPORTED")
    keys = ("mm_factor", "max_im_factor", "min_im_factor", "liquidation_fee_rate", "taker_fee_rate", "premium_fee_cap")
    values = {key: positive(rules.get(key), zero=True) for key in keys}
    require(all(v < 1 for v in values.values()) and
            0 < values["mm_factor"] <= values["min_im_factor"] <= values["max_im_factor"] and
            values["premium_fee_cap"] > 0, "OPTION_FACTORS_INVALID")
    return values


def option_unit(kind, index, mark, strike, price, rules):
    require(kind in ("call", "put"), "OPTION_KIND_UNSUPPORTED")
    index, mark, strike, price = positive(index), positive(mark, zero=True), positive(strike), positive(price, zero=True)
    f = factors(rules)
    otm = max(ZERO, strike - index if kind == "call" else index - strike)
    mm = max(f["mm_factor"] * index, f["mm_factor"] * mark) + mark + f["liquidation_fee_rate"] * index
    im = max(max(f["max_im_factor"] * index - otm, f["min_im_factor"] * index) + max(price, mark), mm)
    fee = min(f["taker_fee_rate"] * index, f["premium_fee_cap"] * price)
    return im, mm, fee, price


@exact
def option_position(*, kind, qty, index, mark, strike, entry, rules):
    """Signed BTC qty. Long premium is already cash-paid, not position IM."""
    size = number(qty)
    im, mm, _, _ = option_unit(kind, index, mark, strike, entry, rules)
    return {"im_usdt": im * abs(size) if size < 0 else ZERO,
            "mm_usdt": mm * abs(size) if size < 0 else ZERO,
            "option_value_usdt": size * positive(mark, zero=True)}


@exact
def option_order(*, kind, action, qty, index, mark, strike, price, rules,
                 closing_position_size=None, closing_position_im=None,
                 account_position_im=None, margin_balance=None):
    """One isolated order calculation; caller must resolve open/close and netting.

    Buy-to-close requires positive ABS position size and explicit account inputs.
    Aggregating multiple closes can double-count released IM and is unsupported.
    """
    size = positive(qty)
    im, mm, unit_fee, price = option_unit(kind, index, mark, strike, price, rules)
    premium, fee = size * price, size * unit_fee
    if action == "buy_to_open":
        occupied = premium + fee
    elif action == "sell_to_open":
        occupied = im * size + fee - premium
    elif action == "buy_to_close":
        position_size = positive(closing_position_size)
        position_im = positive(closing_position_im)
        total_im = positive(account_position_im)
        balance = positive(margin_balance, zero=True)
        require(size <= position_size and position_im <= total_im, "CLOSE_ORDER_INPUT_INCONSISTENT")
        release = size / position_size * min(balance / total_im, ONE) * position_im
        occupied = max(ZERO, premium + fee - release)
    else:
        raise ValueError("OPTION_ORDER_ACTION_UNSUPPORTED")
    return {"order_im_usdt": occupied, "fee_reserve_usdt": fee,
            "premium_usdt": premium, "standalone_short_mm_usdt": size * mm}


@exact
def linear_position(*, qty, mark, entry, leverage, taker_fee_rate, tiers):
    """No open orders / hedge mode. Tiers must be explicitly supplied, not live defaults.

    Base margin and position-tab estimated close fee are separate fields: never
    silently equate position-tab MM with an observed account total MM.
    """
    size, mark, entry = number(qty), positive(mark), positive(entry)
    leverage, fee_rate = positive(leverage), positive(taker_fee_rate, zero=True)
    require(leverage >= 1 and fee_rate < 1 and isinstance(tiers, list) and tiers, "LINEAR_INPUT_INVALID")
    value = abs(size) * mark
    previous_limit, previous_rate, deduction = ZERO, ZERO, ZERO
    selected = None
    for tier in tiers:
        limit, rate = positive(tier.get("limit_usdt")), positive(tier.get("mmr"))
        max_leverage = positive(tier.get("max_leverage"))
        supplied_deduction = positive(tier.get("mm_deduction_usdt"), zero=True)
        require(limit > previous_limit and previous_rate <= rate < 1 and max_leverage >= 1,
                "RISK_TIER_ORDER_INVALID")
        deduction += previous_limit * (rate - previous_rate)
        require(supplied_deduction == deduction, "RISK_TIER_DEDUCTION_MISMATCH")
        if selected is None and value <= limit:
            selected = rate, deduction, max_leverage
        previous_limit, previous_rate = limit, rate
    require(selected is not None, "RISK_TIER_COVERAGE_MISSING")
    rate, deduction, max_leverage = selected
    require(leverage <= max_leverage and ONE / leverage >= rate, "LEVERAGE_EXCEEDS_RISK_TIER")
    im, mm = value / leverage, value * rate - deduction
    close_fee = abs(size) * entry * (ONE - (ONE if size >= 0 else -ONE) / leverage) * fee_rate
    return {"value_usdt": value, "im_base_usdt": im, "mm_base_usdt": mm,
            "estimated_close_fee_usdt": close_fee, "im_position_tab_usdt": im + close_fee,
            "mm_position_tab_usdt": mm + close_fee, "perp_upl_usdt": size * (mark - entry)}


@exact
def cross_balances(*, margin_mode, currency, wallet, perp_upl, option_value, usdt_usd,
                   collateral_ratio, liabilities, other_assets, open_orders):
    """Narrow no-order, USDT-only balance identity; not an IM/MM ratio engine."""
    require(margin_mode == "REGULAR_MARGIN" and currency == "USDT", "CROSS_USDT_SCOPE_REQUIRED")
    require(other_assets == [] and open_orders == [], "OTHER_ASSETS_OR_ORDER_OCCUPANCY_UNSUPPORTED")
    require(number(liabilities) == 0, "LIABILITIES_NOT_ALLOWED")
    # Collateral curves, haircut and order-loss terms intentionally unsupported.
    require(number(collateral_ratio) == ONE, "NONUNIT_COLLATERAL_RATIO_UNSUPPORTED")
    wallet = positive(wallet, zero=True)
    upl, options, fx = number(perp_upl), number(option_value), positive(usdt_usd)
    return {"margin_balance_usd": (wallet + upl) * fx,
            "equity_usd": (wallet + upl + options) * fx,
            "historical_margin_qualified": False, "account_margin_rates_computed": False}
