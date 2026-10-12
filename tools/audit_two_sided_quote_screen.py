#!/usr/bin/env python3
"""Independent rational ledger audit on normalized (not raw-market) inputs."""
import argparse
from decimal import Decimal, localcontext
from fractions import Fraction as F
import json
from pathlib import Path


def require(ok, code):
    if not ok:
        raise ValueError(code)


def close(actual, expected):
    with localcontext() as ctx:
        ctx.prec = 65
        reference = Decimal(expected.numerator) / Decimal(expected.denominator)
        observed = Decimal(actual)
        require(observed.is_finite(), "NONFINITE_RESULT")
        require(abs(observed - reference) <= Decimal("1e-30") * max(Decimal(1), abs(reference)), "INDEPENDENT_LEDGER_MISMATCH")


def account(model, contract, stress):
    bp, sp, bid, ask = (F(model[k]) for k in ("buy_price_ratio", "sell_price_ratio", "exit_bid_ratio", "exit_ask_ratio"))
    bc, sc = F(model["buy_cap_fraction"]), F(model["sell_cap_fraction"])
    require(0 <= bc <= 1 and 0 <= sc <= 1 and 0 < bp < sp and 0 < bid < ask, "MODEL_INPUT_RANGE")
    mult = F(contract["stress_multiplier"]) if stress else F(1)
    maker, taker, slip = (F(contract[k]) * mult / 10000 for k in ("maker_fee_bps", "taker_fee_bps", "liquidation_slip_bps"))
    candidates = {(F(0), F(0)), (bc, F(0)), (F(0), sc), (bc, sc)}
    # Independently intersect the inventory-zero line with each rectangle edge.
    for amount in (bc, sc):
        if amount <= bc and amount <= sc:
            candidates.add((amount, amount))
    values = []
    for bought, sold in candidates:
        receipts = sold * sp
        payments = bought * bp
        fees = (receipts + payments) * maker
        if bought > sold:
            proceeds = (bought - sold) * bid * (1 - slip)
            receipts += proceeds
            fees += proceeds * taker
        if sold > bought:
            cost = (sold - bought) * ask * (1 + slip)
            payments += cost
            fees += cost * taker
        values.append((receipts - payments - fees) * 10000)
    return min(values), max(values)


def audit(screen, contract):
    require(screen["status"] == "SCREEN_COMPLETED" and screen["replayed_segments"] == contract["segments"], "SCREEN_COVERAGE")
    require(screen["actual_fills_identified"] is False and screen["profitability_proven"] is False
            and screen["g4_passed"] is False and screen["account_access"] is False, "NO_QUALIFICATION")
    totals = {s: {arm: {cost: [F(0), F(0)] for cost in ("base_bps", "stress_bps")} for arm in ("fixed", "adaptive", "paired")}
              for s in ["ALL"] + contract["symbols"]}
    counts = {s: 0 for s in totals}
    keys, audited = set(), 0
    for cycle in screen["normalized_ledger"]:
        symbol = cycle["symbol"]
        require(symbol in contract["symbols"] and 0 <= cycle["segment"] < contract["segments"], "LEDGER_IDENTITY")
        key = (cycle["segment"], cycle["cycle"], symbol)
        require(key not in keys, "DUPLICATE_CYCLE")
        keys.add(key)
        counts[symbol] += 1
        counts["ALL"] += 1
        for stress, cost in ((False, "base_bps"), (True, "stress_bps")):
            arms = {}
            for arm in ("fixed", "adaptive"):
                row = cycle["arms"][arm]
                interval = account(row["model"], contract, stress)
                require(interval[0] <= 0 <= interval[1], "ZERO_FILL_OMITTED")
                for i in (0, 1):
                    close(row[cost][i], interval[i])
                    totals[symbol][arm][cost][i] += interval[i]
                    totals["ALL"][arm][cost][i] += interval[i]
                arms[arm] = interval
                audited += 1
            diff = (arms["adaptive"][0] - arms["fixed"][1], arms["adaptive"][1] - arms["fixed"][0])
            for i in (0, 1):
                close(cycle["paired_outer"][cost][i], diff[i])
                totals[symbol]["paired"][cost][i] += diff[i]
                totals["ALL"]["paired"][cost][i] += diff[i]
    require(set(screen["summary"]) == set(totals), "MISSING_FROZEN_SYMBOL")
    for symbol, arms in totals.items():
        observed = screen["summary"][symbol]
        require(observed["cycles"] == counts[symbol], "CYCLE_COUNT")
        for arm, costs in arms.items():
            for cost, interval in costs.items():
                actual = observed["paired_" + cost + "_sum"] if arm == "paired" else observed["arms"][arm][cost + "_sum"]
                for i in (0, 1):
                    close(actual[i], interval[i])
    counters = screen["counts"]
    require(counters["scheduled"] == counts["ALL"] + counters["funding_excluded"] + counters["quote_excluded"], "EXCLUSION_ACCOUNTING")
    for k, value in counters.items():
        require(sum(screen["counts_by_symbol"][s][k] for s in contract["symbols"]) == value, "PER_SYMBOL_COUNT")
    expected = "INSUFFICIENT_EXECUTION_IDENTIFICATION"
    if not counts["ALL"] or counters["quote_excluded"]:
        expected = "INSUFFICIENT_INPUT_COVERAGE"
    elif totals["ALL"]["adaptive"]["base_bps"][1] == 0:
        expected = "NO_POSITIVE_OPPORTUNITY_UNDER_MODEL"
    require(screen["verdict"] == expected, "VERDICT_SCOPE")
    return {"status": "INDEPENDENT_RATIONAL_AUDIT_PASS", "audited_arm_cost_ledgers": audited,
            "cycles": counts["ALL"], "absolute_relative_tolerance": "1e-30", "verdict": expected,
            "raw_market_required": False, "fill_identification_verified": False, "g4_passed": False}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt")
    args = p.parse_args()
    contract = json.loads(Path("docs/plans/2026-10-12-two-sided-quote-screen.contract.json").read_text())
    receipt = json.loads(Path(args.receipt).read_text())
    print(json.dumps(audit(receipt["screen"], contract), sort_keys=True))
