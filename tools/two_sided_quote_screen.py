#!/usr/bin/env python3
"""One frozen, conditional public-flow quote screen; never an order actuator."""
from decimal import Decimal, localcontext
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

D = Decimal
ZERO, ONE, BPS = D(0), D(1), D(10000)
NS = 1_000_000


def require(ok, code):
    if not ok:
        raise ValueError(code)


def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def number(x):
    require(isinstance(x, str), "DECIMAL_STRING_REQUIRED")
    value = D(x)
    require(value.is_finite() and value > 0, "POSITIVE_FINITE_VALUE_REQUIRED")
    return value


def vertices(buy_cap, sell_cap):
    require(ZERO <= buy_cap <= ONE and ZERO <= sell_cap <= ONE, "CAP_RANGE")
    # Terminal liquidation changes branch at buy == sell. Its intersection with
    # the rectangle's boundary can be an optimum absent from the four corners.
    same = min(buy_cap, sell_cap)
    return sorted({(ZERO, ZERO), (buy_cap, ZERO), (ZERO, sell_cap),
                   (buy_cap, sell_cap), (same, same)})


def payoff(buy, sell, buy_price, sell_price, bid, ask, maker, taker, slip):
    cash = sell * sell_price - buy * buy_price
    maker_cost = maker * (buy * buy_price + sell * sell_price)
    inventory = buy - sell
    liquidation_cash = taker_cost = ZERO
    if inventory > 0:
        liquidation_cash = inventory * bid * (ONE - slip)
        taker_cost = liquidation_cash * taker
    elif inventory < 0:
        liquidation_cash = inventory * ask * (ONE + slip)
        taker_cost = -liquidation_cash * taker
    return cash + liquidation_cash - maker_cost - taker_cost


def bounds(model, contract, stress=False):
    multiplier = D(contract["stress_multiplier"]) if stress else ONE
    fees = [D(contract[k]) / BPS * multiplier for k in
            ("maker_fee_bps", "taker_fee_bps", "liquidation_slip_bps")]
    values = [payoff(b, s, D(model["buy_price_ratio"]), D(model["sell_price_ratio"]),
                     D(model["exit_bid_ratio"]), D(model["exit_ask_ratio"]), *fees) * BPS
              for b, s in vertices(D(model["buy_cap_fraction"]), D(model["sell_cap_fraction"]))]
    return min(values), max(values)


def paired_outer(adaptive, fixed):
    return adaptive[0] - fixed[1], adaptive[1] - fixed[0]


def update(estimate, realized_bps, contract):
    alpha = D(contract["alpha"])
    return tuple((ONE - alpha) * old + alpha * adverse
                 for old, adverse in zip(estimate, (max(ZERO, -realized_bps), max(ZERO, realized_bps))))


def widths(estimate, contract):
    return tuple(max(D(contract["minimum_halfwidth_bps"]),
                     min(D(contract["maximum_halfwidth_bps"]), D(contract["width_intercept_bps"]) + e))
                 for e in estimate)


def funding_overlap(start_ns, end_ns, guard_ms):
    hour, guard = 3_600_000 * NS, guard_ms * NS
    # Include equality at either guard boundary conservatively.
    boundary = ((start_ns - guard + hour - 1) // hour) * hour
    return boundary <= end_ns + guard


def decision_times(first_ns, last_ns, contract):
    return list(range(first_ns + contract["warmup_ms"] * NS,
                      last_ns - (contract["liquidate_ms"] + contract["late_guard_ms"]) * NS + 1,
                      contract["cadence_ms"] * NS))


def validated_trade(trade, symbol, received_ns, contract, seen):
    require(isinstance(trade, dict) and trade.get("s") == symbol, "TRADE_SYMBOL")
    require(type(trade.get("T")) is int and trade["T"] > 0, "TRADE_TIME")
    require(trade.get("S") in ("Buy", "Sell"), "TRADE_AGGRESSOR")
    require(type(trade.get("BT")) is bool and type(trade.get("RPI")) is bool, "TRADE_FLAGS")
    require(isinstance(trade.get("i"), str) and 0 < len(trade["i"]) <= 128, "TRADE_ID")
    identifier = hashlib.sha256((symbol + ":" + trade["i"]).encode()).digest()
    require(identifier not in seen, "DUPLICATE_TRADE_ID")
    seen.add(identifier)
    lag_ms = received_ns // NS - trade["T"]
    require(contract["minimum_trade_lag_ms"] <= lag_ms <= contract["maximum_trade_lag_ms"],
            "TRADE_CLOCK_OR_DELAY_OUTSIDE_MODEL")
    price, quantity = number(trade.get("p")), number(trade.get("v"))
    return (trade["T"] * NS, trade["S"], price, quantity, trade["BT"] or trade["RPI"])


def prepare_segment(path, expected, contract, original, seen):
    """Replay ORIGINAL projection and extract only fixed snapshots; no P&L yet."""
    require(not path.is_symlink() and path.is_dir(), "SEGMENT_DIRECTORY")
    for filename, key in (("receipt.json", "receipt_sha256"), ("receive.jsonl.gz", "raw_sha256")):
        file = path / filename
        require(file.is_file() and not file.is_symlink() and file_hash(file) == expected[key], "SEGMENT_HASH")
    require((path / "receive.jsonl.gz").stat().st_size == expected["raw_bytes"], "SEGMENT_SIZE")
    receipt = json.loads((path / "receipt.json").read_text())
    require(receipt["segment_index"] == expected["index"] and receipt["status"] == "SEALED"
            and receipt["previous_receipt_sha256"] == expected["previous_receipt_sha256"]
            and receipt["raw_sha256"] == expected["raw_sha256"]
            and receipt["raw_bytes"] == expected["raw_bytes"], "RECEIPT_BINDING")
    replay, size = original.Replay(), 0
    offsets = (0, contract["effective_ms"], contract["label_ms"], contract["liquidate_ms"])
    points, decisions, snapshots = [], [], {}
    quotes, trades = {}, {s: [] for s in contract["symbols"]}
    cursor, first_ns = 0, None
    # The exact first ns is learned from the first original row; final ms from
    # receipt is a conservative <=1ms truncation for full-window selection.
    last_ns = receipt["observation"]["last_receive_epoch_ms"] * NS

    def snapshot_until(now, inclusive=False):
        nonlocal cursor
        while cursor < len(points) and (points[cursor] < now or (inclusive and points[cursor] == now)):
            at = points[cursor]
            captured = {}
            for symbol in contract["symbols"]:
                if symbol in quotes and at - quotes[symbol] <= contract["max_book_age_ms"] * NS:
                    book = replay.books[symbol]
                    # Original book prices round-trip to their published decimal
                    # representation. No float price arithmetic enters the ledger.
                    captured[symbol] = (D(str(max(book.bids))), D(str(min(book.asks))))
            snapshots[at] = captured
            cursor += 1

    with gzip.open(path / "receive.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            size += len(line.encode())
            require(size <= original.MAX_RAW_BYTES, "EXPANDED_SEGMENT_LIMIT")
            row = json.loads(line)
            if first_ns is None:
                first_ns = row["receive_utc_ns"]
                decisions = decision_times(first_ns, last_ns, contract)
                points = sorted({t + off * NS for t in decisions for off in offsets})
            snapshot_until(row["receive_utc_ns"])
            replay.accept(row)
            payload = json.loads(row["wire"])
            symbol = payload["topic"].rsplit(".", 1)[1]
            if payload["topic"].startswith("orderbook."):
                for side in ("a", "b"):
                    for price, _size in payload["data"].get(side, []):
                        require(number(price) == D(str(float(price))), "BOOK_PRICE_NOT_LOSSLESS")
                quotes[symbol] = row["receive_utc_ns"]
            else:
                for trade in payload["data"]:
                    trades[symbol].append(validated_trade(trade, symbol, row["receive_utc_ns"], contract, seen))
    snapshot_until(replay.last_wall, inclusive=True)
    require(replay.summary() == receipt["observation"], "ORIGINAL_REPLAY_MISMATCH")
    require(cursor == len(points), "SNAPSHOT_COVERAGE")
    for items in trades.values():
        items.sort(key=lambda x: x[0])
    return {"index": expected["index"], "decisions": decisions, "snapshots": snapshots, "trades": trades,
            "messages": replay.count, "replay_sha256": replay.summary()["replay_sha256"]}


def model_for_cycle(mid, halfwidth, effective, exit_quote, trades, start, contract):
    buy_price, sell_price = mid * (ONE - halfwidth[0] / BPS), mid * (ONE + halfwidth[1] / BPS)
    # Post-only rejection is fixed at simulated effective-time observable BBO.
    buy_accepted, sell_accepted = buy_price < effective[1], sell_price > effective[0]
    quantity = D(contract["reference_notional"]) / mid
    buy_volume = sell_volume = ZERO
    lo, hi = start + contract["effective_ms"] * NS, start + contract["cancel_effective_ms"] * NS
    for matched, side, price, volume, excluded in trades:
        if excluded or not lo <= matched < hi:
            continue
        if buy_accepted and side == "Sell" and price <= buy_price:
            buy_volume += volume
        if sell_accepted and side == "Buy" and price >= sell_price:
            sell_volume += volume
    return {"buy_price_ratio": str(buy_price / mid), "sell_price_ratio": str(sell_price / mid),
            "exit_bid_ratio": str(exit_quote[0] / mid), "exit_ask_ratio": str(exit_quote[1] / mid),
            "buy_cap_fraction": str(min(ONE, buy_volume / quantity)),
            "sell_cap_fraction": str(min(ONE, sell_volume / quantity)),
            "buy_halfwidth_bps": str(halfwidth[0]), "sell_halfwidth_bps": str(halfwidth[1]),
            "post_only_rejected_sides": int(not buy_accepted) + int(not sell_accepted)}


def prepare_models(prepared, contract):
    results = []
    by_symbol = {}
    counts = {"scheduled": 0, "funding_excluded": 0, "quote_excluded": 0,
              "labels_unavailable": 0, "mature_updates": 0}
    for symbol in contract["symbols"]:
        prior_counts = dict(counts)
        estimate = (D(contract["initial_ewma_bps"]),) * 2
        pending = None
        for cycle, t in enumerate(prepared["decisions"]):
            counts["scheduled"] += 1
            if pending is not None:
                mature_at, label = pending
                require(mature_at <= t, "UNMATURED_LEARNING_LABEL")
                estimate = update(estimate, label, contract)
                counts["mature_updates"] += 1
                pending = None
            views = [prepared["snapshots"].get(t + contract[k] * NS, {}).get(symbol)
                     for k in ("effective_ms", "label_ms", "liquidate_ms")]
            decision = prepared["snapshots"].get(t, {}).get(symbol)
            if decision is not None and views[1] is not None:
                mid = sum(decision) / 2
                label = (sum(views[1]) / 2 / mid - ONE) * BPS
                pending = (t + contract["label_ms"] * NS, label)
            else:
                counts["labels_unavailable"] += 1
            if funding_overlap(t, t + contract["liquidate_ms"] * NS, contract["funding_hour_guard_ms"]):
                counts["funding_excluded"] += 1
                continue
            if decision is None or views[0] is None or views[2] is None:
                counts["quote_excluded"] += 1
                continue
            mid = sum(decision) / 2
            arms = {}
            for arm, width in (("fixed", (D(contract["minimum_halfwidth_bps"]),) * 2),
                               ("adaptive", widths(estimate, contract))):
                model = model_for_cycle(mid, width, views[0], views[2], prepared["trades"][symbol], t, contract)
                arms[arm] = {"model": model}
            results.append({"segment": prepared["index"], "cycle": cycle, "symbol": symbol,
                            "arms": arms})
        by_symbol[symbol] = {k: counts[k] - prior_counts[k] for k in counts}
    return results, counts, by_symbol


def evaluate(cycles, contract):
    for cycle in cycles:
        for arm in cycle["arms"].values():
            arm["base_bps"] = [str(v) for v in bounds(arm["model"], contract)]
            arm["stress_bps"] = [str(v) for v in bounds(arm["model"], contract, True)]
        cycle["paired_outer"] = {cost: [str(v) for v in paired_outer(
            tuple(map(D, cycle["arms"]["adaptive"][cost])), tuple(map(D, cycle["arms"]["fixed"][cost])))]
            for cost in ("base_bps", "stress_bps")}
    return cycles


def aggregate(cycles, contract):
    result = {}
    symbols = ["ALL"] + list(contract["symbols"])
    for symbol in symbols:
        items = [c for c in cycles if symbol == "ALL" or c["symbol"] == symbol]
        out = {"cycles": len(items), "arms": {}}
        for arm in ("fixed", "adaptive"):
            summary = {"post_only_rejected_sides": sum(c["arms"][arm]["model"]["post_only_rejected_sides"] for c in items)}
            for side in ("buy", "sell"):
                caps = [D(c["arms"][arm]["model"][side + "_cap_fraction"]) for c in items]
                summary[side + "_positive_capacity_cycles"] = sum(v > 0 for v in caps)
                summary[side + "_capacity_fraction_sum"] = str(sum(caps, ZERO))
            for cost in ("base_bps", "stress_bps"):
                summary[cost + "_sum"] = [str(sum((D(c["arms"][arm][cost][i]) for c in items), ZERO)) for i in (0, 1)]
                summary[cost + "_positive_upper_cycles"] = sum(D(c["arms"][arm][cost][1]) > 0 for c in items)
            out["arms"][arm] = summary
        for cost in ("base_bps", "stress_bps"):
            out["paired_" + cost + "_sum"] = [str(sum((D(c["paired_outer"][cost][i]) for c in items), ZERO)) for i in (0, 1)]
        result[symbol] = out
    return result


def adjudicate(summary, counts):
    if summary["ALL"]["cycles"] == 0 or counts["quote_excluded"]:
        return "INSUFFICIENT_INPUT_COVERAGE"
    upper = D(summary["ALL"]["arms"]["adaptive"]["base_bps_sum"][1])
    return "NO_POSITIVE_OPPORTUNITY_UNDER_MODEL" if upper == 0 else "INSUFFICIENT_EXECUTION_IDENTIFICATION"


def execute(contract, manifest, root, source_root):
    started = time.monotonic()
    require(len(manifest["segments"]) == contract["segments"], "MANIFEST_COUNT")
    require([s["index"] for s in manifest["segments"]] == list(range(contract["segments"])), "MANIFEST_ORDER")
    for name, expected in manifest["source_hashes"].items():
        require(file_hash(Path(source_root) / name) == expected, "CAPTURE_SOURCE_HASH")
    require(file_hash(Path(root) / "started.json") == manifest["started_sha256"], "STARTED_HASH")
    sys.path.insert(0, str(source_root))
    import forward_evidence as original
    require(Path(original.__file__).resolve() == (Path(source_root) / "forward_evidence.py").resolve(), "CAPTURE_IMPORT_IDENTITY")
    seen, previous, replay_hashes, messages = set(), None, [], 0
    cycles, totals = [], {}
    per_symbol_counts = {s: {} for s in contract["symbols"]}
    with localcontext() as ctx:
        ctx.prec = 40
        for expected in manifest["segments"]:
            require(time.monotonic() - started < contract["compute_timeout_seconds"], "COMPUTE_BUDGET")
            require(expected["previous_receipt_sha256"] == previous, "MANIFEST_CHAIN")
            item = prepare_segment(Path(root) / ("segment-%03d" % expected["index"]), expected, contract, original, seen)
            previous = expected["receipt_sha256"]
            replay_hashes.append(item["replay_sha256"])
            messages += item["messages"]
            # Store only normalized fixed inputs, not every segment's trade flow.
            # There is still no P&L evaluation until ALL 94 replays have passed.
            values, counts, by_symbol = prepare_models(item, contract)
            cycles.extend(values)
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
            for symbol, counters in by_symbol.items():
                for key, value in counters.items():
                    per_symbol_counts[symbol][key] = per_symbol_counts[symbol].get(key, 0) + value
            del item
        evaluate(cycles, contract)
        summary = aggregate(cycles, contract)
    return {"schema_version": "two_sided_screen_result_v1", "status": "SCREEN_COMPLETED",
            "verdict": adjudicate(summary, totals), "replayed_segments": len(replay_hashes),
            "messages": messages, "unique_trades": len(seen),
            "replay_hashes": replay_hashes, "counts": totals,
            "counts_by_symbol": per_symbol_counts,
            "summary": summary, "normalized_ledger": cycles,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "quote_coverage_complete": totals.get("quote_excluded", 0) == 0,
            "conditional_model_only": True, "actual_fills_identified": False,
            "profitability_proven": False, "g4_passed": False, "account_access": False}


if __name__ == "__main__":
    bundle = json.load(sys.stdin)
    result = execute(bundle["contract"], bundle["manifest"], "/evidence", "/capture-code")
    print(json.dumps(result, sort_keys=True, allow_nan=False))
