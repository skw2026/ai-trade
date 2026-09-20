#!/usr/bin/env python3
"""Compare ordered replay fill/state events and terminal settlement, fail closed.

Log timestamps are ignored; generated order/fill IDs are mapped by first
appearance so identity relationships remain checked. No state or money field
is dropped. This is not a profitability or all-runtime-state certificate.
"""
import argparse
import hashlib
import json
import math
import pathlib

FILL_FIELDS = set("fill_id client_order_id symbol direction qty price fee liquidity "
                  "order_state_before order_state_after order_filled_qty_before "
                  "order_filled_qty_after local_qty_before avg_entry_price_before "
                  "local_qty_after oms_net_qty_before oms_net_qty_after "
                  "account_already_reflected".split())
NUMERIC_FIELDS = set("direction qty price fee order_filled_qty_before "
                     "order_filled_qty_after local_qty_before avg_entry_price_before "
                     "local_qty_after oms_net_qty_before oms_net_qty_after".split())
TERMINAL_FIELDS = set("position_count realized_net_usd fees_usd funding_paid_usd".split())


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def fields(text, required):
    parts = [part.strip().split("=", 1) for part in text.strip().split(",")]
    if any(len(part) != 2 or not part[1] for part in parts):
        raise ValueError("malformed trace fields")
    result = dict(parts)
    if len(result) != len(parts) or set(result) != required:
        raise ValueError("missing, duplicate or unreviewed trace fields")
    return result


def normalize(text):
    fills, terminals = [], []
    identities = {key: {} for key in ("fill_id", "client_order_id")}
    for line in text.splitlines():
        if "REPLAY_TERMINAL_SETTLEMENT_FAILED:" in line:
            raise ValueError("terminal settlement failed")
        if "FILL_APPLIED:" in line:
            if terminals:
                raise ValueError("fill observed after terminal settlement")
            event = fields(line.split("FILL_APPLIED:", 1)[1], FILL_FIELDS)
            if any(not math.isfinite(float(event[key])) for key in NUMERIC_FIELDS):
                raise ValueError("nonfinite fill field")
            for key, mapping in identities.items():
                original = event[key]
                mapping.setdefault(original, str(len(mapping)))
                event[key] = mapping[original]
            fills.append(event)
        if "REPLAY_TERMINAL_SETTLEMENT_DONE:" in line:
            terminal = fields(line.split("REPLAY_TERMINAL_SETTLEMENT_DONE:", 1)[1],
                              TERMINAL_FIELDS)
            if terminal["position_count"] != "0" or any(
                    not math.isfinite(float(value)) for value in terminal.values()):
                raise ValueError("terminal not flat or nonfinite")
            terminals.append(terminal)
    if not fills or len(terminals) != 1:
        raise ValueError("need nonempty fills and exactly one terminal settlement")
    return {"fills": fills, "terminal": terminals[0]}


def audit(paths):
    if len(paths) < 2:
        raise ValueError("need at least two independent runs")
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("each run must use a distinct log file")
    runs, traces = [], []
    for path in paths:
        raw = path.read_bytes()
        trace = normalize(raw.decode())
        traces.append(trace)
        runs.append({"log": str(path), "log_sha256": digest(raw),
                     "fills": len(trace["fills"]),
                     "trace_sha256": digest(json.dumps(trace, sort_keys=True).encode())})
    equal = all(trace == traces[0] for trace in traces[1:])
    return {"schema_version": "replay_trace_comparison_v1",
            "status": "PASS" if equal else "FAIL", "runs": runs,
            "comparison": "all emitted FILL_APPLIED fields plus terminal settlement; "
                          "IDs canonicalized by first appearance; log timestamps ignored",
            "order_state_before_included": True,
            "all_runtime_state_certified": False, "profitability_claim": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=pathlib.Path)
    args = parser.parse_args()
    try:
        report = audit(args.logs)
    except (ValueError, OSError) as exc:
        report = {"status": "FAIL", "reason": str(exc)}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
