#!/usr/bin/env python3
"""Frozen three-way reference decision over bound local event logs, no replay.

Incomplete or invalid observations fail closed. Neither a process exit 0 nor
positive net returns grant account, out-of-sample, Demo or live qualification.
"""
from collections import defaultdict
from mvp_reference_inputs import load_contract, number, require, strict_json, utc_ms, window

DAY = 86400000


def parse_events(log):
    result = {"bars": [], "fills": [], "terminals": [], "stops": []}
    for line in (log.splitlines() if isinstance(log,str) else log):
        for marker, key in (("REFERENCE_BAR_JSON ", "bars"), ("REFERENCE_FILL_JSON ", "fills"),
                            ("REFERENCE_TERMINAL_JSON ", "terminals"), ("REFERENCE_STOP_JSON ", "stops")):
            if marker in line:
                result[key].append(strict_json(line.split(marker, 1)[1]))
    return result


def decision(label, reason):
    return {"decision": label, "reason": reason, "economic_qualification": False,
            "account_qualification": False, "demo_activation_authorized": False}


def assess(scenarios, contract=None):
    contract = load_contract() if contract is None else contract
    try:
        require(set(scenarios) <= {"base", "stress"} and scenarios, "INVALID_SCENARIOS")
        # An explicitly recorded reference boundary reject dominates an incomplete
        # tail. It does not turn invalid/unbound input archives into valid evidence.
        for run in scenarios.values():
            for stop in run.get("stops", []):
                if stop.get("reason") in ("REJECT_REFERENCE_MAINTENANCE", "REJECT_REFERENCE_DRAWDOWN_LATCH"):
                    return decision("REJECT", stop["reason"])
        for run in scenarios.values():
            require(not run.get("stops"), "REFERENCE_RUNTIME_OR_INTRABAR_STOP")
        require(set(scenarios) == {"base", "stress"}, "BOTH_COST_SCENARIOS_REQUIRED")
        source, start, end = window(contract)
        interval = contract["interval_ms"]
        half = utc_ms("2025-07-01T00:00:00Z")
        failures = []
        for name, run in scenarios.items():
            bars, fills, terminal = run["bars"], run["fills"], run["terminals"]
            expected = list(range(source+interval, end+1, interval))
            require([b["ts"] for b in bars] == expected, "BAR_LOG_COVERAGE")
            require(len(terminal) == 1 and terminal[0]["flat"] is True and
                    terminal[0]["pending"] is False and number(terminal[0]["collateral"]) == 0,
                    "TERMINAL_NOT_FLAT_OR_MISSING")
            daily_signals, daily_orders = defaultdict(int), defaultdict(set)
            boundary = {}
            previous_error = number(0)
            previous_dd = number(0)
            for b in bars:
                require(type(b["signal"]) is bool and type(b["warmup"]) is bool and
                        b["warmup"] == (b["ts"] <= start), "WARMUP_OR_SIGNAL_IDENTITY")
                equity = number(b["equity"], True)
                error = number(b["funding_uncertainty"])
                dd = number(b["drawdown_upper"])
                require(error >= previous_error and previous_dd <= dd < number("0.08"), "RISK_BOUND_OR_UNCERTAINTY")
                previous_error = error
                previous_dd = dd
                boundary[b["ts"]] = (equity, error)
                if not b["warmup"] and b["signal"]:
                    daily_signals[b["ts"]//DAY] += 1
            ids, orders = set(), set()
            last_fill_ts = source
            for f in fills:
                require(type(f["ts"]) is int and start <= f["ts"] <= end and f["ts"] >= last_fill_ts,
                        "FILL_TIME_OUTSIDE_SCREEN")
                last_fill_ts = f["ts"]
                require(isinstance(f["fill"], str) and f["fill"] and f["fill"] not in ids and
                        isinstance(f["order"], str) and f["order"], "DUPLICATE_FILL_ID")
                ids.add(f["fill"])
                number(f["qty"], True); number(f["price"], True)
                require(number(f["fee"]) >= 0, "NEGATIVE_FEE")
                if f["order"] not in orders and not f["order"].startswith("replay-terminal-close-"):
                    daily_orders[f["ts"]//DAY].add(f["order"])
                orders.add(f["order"])
            for day in range(start//DAY, end//DAY):
                if daily_signals[day] < contract["verdict"]["min_signals_per_complete_UTC_day"] or \
                        len(daily_orders[day]) < contract["verdict"]["min_distinct_filled_orders_per_complete_UTC_day"]:
                    failures.append(name + ":LOW_ACTIVITY")
                    break
            equity = number(terminal[0]["equity"], True)
            error = number(terminal[0]["funding_uncertainty"])
            require(error == previous_error and previous_dd <= number(terminal[0]["drawdown_upper"]) < number("0.08"),
                    "TERMINAL_RISK_BOUND")
            initial = number(contract["capital"]["initial_cash"])
            require(half in boundary, "HALF_YEAR_BOUNDARY_MISSING")
            mid, mid_error = boundary[half]
            for net, uncertainty in ((equity-initial, error), (mid-initial, mid_error),
                                     (equity-mid, error-mid_error)):
                if net + uncertainty <= 0:
                    failures.append(name + ":NONPOSITIVE_NET_OR_HALF")
                elif net - uncertainty <= 0:
                    raise ValueError("FUNDING_UNCERTAINTY_CHANGES_SIGN")
        if failures:
            return decision("REJECT", ";".join(sorted(set(failures))))
        return decision("WORTH_FURTHER_REVIEW_NOT_PROFIT_QUALIFIED", "FIXED_REFERENCE_FILTERS_PASSED")
    except (ValueError, KeyError, TypeError) as error:
        return decision("INSUFFICIENT", str(error))
