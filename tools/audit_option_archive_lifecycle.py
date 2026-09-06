#!/usr/bin/env python3
"""Audit whether a checksum-bound option archive covers one complete held lifecycle.

The report is deliberately aggregate-only: raw snapshots, artifact paths, account
state and credentials are never emitted.  This is a data qualification audit, not
an economic backtest or activation decision.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import pathlib
import re
import tempfile
import time
from typing import Any, Dict, Mapping, Sequence

import capture_bybit_option_vrp_v2 as capture
from audit_option_vrp_sequential_payoff import (
    load_frozen_contract,
    replay_capture_root,
    sha256_file,
)


SCHEMA_VERSION = "option_archive_lifecycle_audit_v1"
CASE_SCHEMA_VERSION = "option_archive_lifecycle_case_v1"
PASS_DECISION = "PASS_ARCHIVE_LIFECYCLE_ONLY"
INSUFFICIENT_DECISION = "INSUFFICIENT_ARCHIVE_LIFECYCLE"
INVALID_DECISION = "TECHNICALLY_INVALID_ARCHIVE"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root is not an object: {path.name}")
    return payload


def _number(value: Any, *, field: str, positive: bool = False,
            nonnegative: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} is not finite")
    if positive and result <= 0.0:
        raise ValueError(f"{field} must be positive")
    if nonnegative and result < 0.0:
        raise ValueError(f"{field} must be nonnegative")
    return result


def _integer(value: Any, *, field: str, positive: bool = False) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not an integer") from exc
    if positive and result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_case(case_path: pathlib.Path, *, policy: Mapping[str, Any],
              manifest: Mapping[str, Any]) -> Dict[str, Any]:
    case = read_json(case_path)
    if case.get("schema_version") != CASE_SCHEMA_VERSION:
        raise ValueError("lifecycle case schema mismatch")
    if case.get("experiment_id") != policy.get("experiment_id"):
        raise ValueError("lifecycle case experiment mismatch")
    window = case.get("window")
    option = case.get("option_contract")
    hedge = case.get("hedge_contract")
    coverage = case.get("coverage_contract")
    if not all(isinstance(value, Mapping) for value in (window, option, hedge, coverage)):
        raise ValueError("lifecycle case contracts are missing")
    start = _integer(window.get("start_epoch_ms"), field="window.start", positive=True)
    end = _integer(window.get("end_epoch_ms"), field="window.end", positive=True)
    delivery = _integer(option.get("delivery_time_epoch_ms"), field="option.delivery", positive=True)
    if start < int(manifest["observation_start_epoch_ms"]):
        raise ValueError("lifecycle starts before the frozen observation boundary")
    if end <= start or end != delivery:
        raise ValueError("lifecycle window must end at option delivery")
    symbols = option.get("symbols")
    if (not isinstance(symbols, list) or len(symbols) != 2
            or len(set(map(str, symbols))) != 2):
        raise ValueError("lifecycle must bind one unique call/put symbol pair")
    suffixes = {str(symbol).split("-")[-2].upper() for symbol in symbols}
    if suffixes != {"C", "P"}:
        raise ValueError("lifecycle symbols are not an exact call/put pair")
    if option.get("settle_coin") != policy["capture_contract"]["settle_coin"]:
        raise ValueError("lifecycle settle coin mismatch")
    if hedge.get("symbol") != policy["capture_contract"]["hedge_symbol"]:
        raise ValueError("lifecycle hedge symbol mismatch")
    _number(option.get("minimum_executable_size_btc"), field="option.minimum_size", positive=True)
    _number(hedge.get("minimum_executable_size_btc"), field="hedge.minimum_size", positive=True)
    _integer(hedge.get("maximum_book_age_seconds"), field="hedge.maximum_age", positive=True)
    _integer(coverage.get("expected_poll_interval_seconds"), field="coverage.poll", positive=True)
    _integer(coverage.get("maximum_snapshot_gap_seconds"), field="coverage.maximum_gap", positive=True)
    if coverage.get("all_observed_snapshots_must_be_qualified") is not True:
        raise ValueError("lifecycle qualification must be fail-closed for every observed snapshot")
    if coverage.get("require_paired_delivery_evidence") is not True:
        raise ValueError("lifecycle qualification must require paired delivery evidence")
    if case.get("research_domain") != "development_only" or case.get("economic_evidence") is not False:
        raise ValueError("lifecycle case cannot claim economic evidence")
    if any(case.get(name) is not False for name in (
        "promotion_authority", "demo_activation_authorized", "live_activation_authorized"
    )):
        raise ValueError("lifecycle case cannot grant activation authority")
    return case


def _option_row_reason(row: Mapping[str, Any], *, symbol: str, delivery: int,
                       minimum_size: float) -> str | None:
    try:
        if str(row.get("symbol") or "") != symbol:
            return "OPTION_IDENTITY_MISMATCH"
        if _integer(row.get("deliveryTime"), field="option.delivery", positive=True) != delivery:
            return "OPTION_DELIVERY_MISMATCH"
        expected_side = "call" if symbol.split("-")[-2].upper() == "C" else "put"
        if str(row.get("optionsType") or "").lower() != expected_side:
            return "OPTION_SIDE_MISMATCH"
        bid = _number(row.get("bid1Price"), field="option.bid", positive=True)
        ask = _number(row.get("ask1Price"), field="option.ask", positive=True)
        bid_size = _number(row.get("bid1Size"), field="option.bid_size", nonnegative=True)
        ask_size = _number(row.get("ask1Size"), field="option.ask_size", nonnegative=True)
        if ask < bid:
            return "OPTION_CROSSED_BBO"
        if min(bid_size, ask_size) + 1e-12 < minimum_size:
            return "OPTION_INSUFFICIENT_BBO_SIZE"
        minimum_order = _number(row.get("minOrderQty"), field="option.min_order", positive=True)
        quantity_step = _number(row.get("qtyStep"), field="option.quantity_step", positive=True)
        if minimum_order > minimum_size + 1e-12:
            return "OPTION_MINIMUM_ORDER_TOO_LARGE"
        if abs(minimum_size / quantity_step - round(minimum_size / quantity_step)) > 1e-9:
            return "OPTION_SIZE_NOT_ON_QUANTITY_STEP"
        for field in ("strike", "indexPrice", "tickSize"):
            _number(row.get(field), field=f"option.{field}", positive=True)
        _number(row.get("delta"), field="option.delta")
    except ValueError as exc:
        return str(exc).upper().replace(" ", "_")
    return None


def _hedge_reason(snapshot: Mapping[str, Any], *, timestamp: int,
                  minimum_size: float, maximum_age_ms: int) -> str | None:
    ticker = snapshot.get("hedge_ticker")
    book = snapshot.get("hedge_orderbook_l1")
    if not isinstance(ticker, Mapping) or not isinstance(book, Mapping):
        return "HEDGE_BBO_MISSING"
    bids, asks = book.get("b"), book.get("a")
    if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
        return "HEDGE_BOOK_MISSING"
    try:
        bid = _number(bids[0][0], field="hedge.book_bid", positive=True)
        ask = _number(asks[0][0], field="hedge.book_ask", positive=True)
        bid_size = _number(bids[0][1], field="hedge.book_bid_size", nonnegative=True)
        ask_size = _number(asks[0][1], field="hedge.book_ask_size", nonnegative=True)
        ticker_bid = _number(ticker.get("bid1Price"), field="hedge.ticker_bid", positive=True)
        ticker_ask = _number(ticker.get("ask1Price"), field="hedge.ticker_ask", positive=True)
        book_time = _integer(book.get("ts"), field="hedge.book_time", positive=True)
    except (IndexError, TypeError, ValueError) as exc:
        return str(exc).upper().replace(" ", "_")
    if ask < bid or ticker_ask < ticker_bid:
        return "HEDGE_CROSSED_BBO"
    if min(bid_size, ask_size) + 1e-12 < minimum_size:
        return "HEDGE_INSUFFICIENT_BBO_SIZE"
    if book_time > timestamp:
        return "HEDGE_BOOK_FROM_FUTURE"
    if timestamp - book_time > maximum_age_ms:
        return "HEDGE_BOOK_STALE"
    return None


def _coverage_gaps(timestamps: Sequence[int], *, start: int, end: int,
                   maximum_gap_ms: int) -> list[Dict[str, Any]]:
    if not timestamps:
        return [{"kind": "NO_QUALIFIED_OBSERVATIONS", "start_epoch_ms": start,
                 "end_epoch_ms": end, "duration_seconds": (end - start) / 1000.0}]
    gaps: list[Dict[str, Any]] = []
    if timestamps[0] - start > maximum_gap_ms:
        gaps.append({"kind": "ENTRY_EDGE_GAP", "start_epoch_ms": start,
                     "end_epoch_ms": timestamps[0],
                     "duration_seconds": (timestamps[0] - start) / 1000.0})
    for left, right in zip(timestamps, timestamps[1:]):
        if right - left > maximum_gap_ms:
            gaps.append({"kind": "INTERNAL_GAP", "start_epoch_ms": left,
                         "end_epoch_ms": right,
                         "duration_seconds": (right - left) / 1000.0})
    if end - timestamps[-1] > maximum_gap_ms:
        gaps.append({"kind": "TERMINAL_EDGE_GAP", "start_epoch_ms": timestamps[-1],
                     "end_epoch_ms": end,
                     "duration_seconds": (end - timestamps[-1]) / 1000.0})
    return gaps


def audit_lifecycle(*, root: pathlib.Path, policy_path: pathlib.Path,
                    manifest_path: pathlib.Path, case_path: pathlib.Path,
                    executed_release_sha: str | None = None,
                    generated_at_epoch_ms: int | None = None) -> Dict[str, Any]:
    if executed_release_sha is not None and not SHA_PATTERN.fullmatch(executed_release_sha):
        raise ValueError("executed release SHA is invalid")
    policy, manifest = load_frozen_contract(policy_path, manifest_path)
    case = load_case(case_path, policy=policy, manifest=manifest)
    replay = replay_capture_root(root, policy=policy, manifest=manifest)
    window = case["window"]
    option_contract = case["option_contract"]
    hedge_contract = case["hedge_contract"]
    coverage_contract = case["coverage_contract"]
    start, end = int(window["start_epoch_ms"]), int(window["end_epoch_ms"])
    delivery = int(option_contract["delivery_time_epoch_ms"])
    symbols = [str(value) for value in option_contract["symbols"]]
    option_minimum = float(option_contract["minimum_executable_size_btc"])
    hedge_minimum = float(hedge_contract["minimum_executable_size_btc"])
    maximum_age_ms = int(hedge_contract["maximum_book_age_seconds"]) * 1000
    maximum_gap_ms = int(coverage_contract["maximum_snapshot_gap_seconds"]) * 1000

    reason_counts: collections.Counter[str] = collections.Counter()
    symbol_observation_counts: collections.Counter[str] = collections.Counter()
    qualified_timestamps: list[int] = []
    pair_timestamps: list[int] = []
    hedge_qualified_count = 0
    lifecycle_snapshot_count = 0
    unqualified_snapshot_count = 0
    for snapshot in replay["snapshots"]:
        timestamp = int(snapshot["timestamp_epoch_ms"])
        if timestamp < start or timestamp > end:
            continue
        lifecycle_snapshot_count += 1
        rows: Dict[str, list[Mapping[str, Any]]] = {symbol: [] for symbol in symbols}
        for row in snapshot.get("scoped_options", []):
            if isinstance(row, Mapping) and str(row.get("symbol") or "") in rows:
                rows[str(row["symbol"])].append(row)
        pair_ok = True
        for symbol in symbols:
            symbol_observation_counts[symbol] += int(bool(rows[symbol]))
            if not rows[symbol]:
                reason_counts[f"{symbol.split('-')[-2].upper()}_SYMBOL_MISSING"] += 1
                pair_ok = False
                continue
            if len(rows[symbol]) != 1:
                reason_counts["DUPLICATE_OPTION_SYMBOL"] += 1
                pair_ok = False
                continue
            reason = _option_row_reason(
                rows[symbol][0], symbol=symbol, delivery=delivery,
                minimum_size=option_minimum,
            )
            if reason:
                reason_counts[reason] += 1
                pair_ok = False
        hedge_reason = _hedge_reason(
            snapshot, timestamp=timestamp, minimum_size=hedge_minimum,
            maximum_age_ms=maximum_age_ms,
        )
        hedge_ok = hedge_reason is None
        if hedge_ok:
            hedge_qualified_count += 1
        else:
            reason_counts[str(hedge_reason)] += 1
        if pair_ok:
            pair_timestamps.append(timestamp)
        if pair_ok and hedge_ok:
            qualified_timestamps.append(timestamp)
        else:
            unqualified_snapshot_count += 1

    gaps = _coverage_gaps(
        qualified_timestamps, start=start, end=end, maximum_gap_ms=maximum_gap_ms
    )
    delivery_prices: list[float] = []
    delivery_missing: list[str] = []
    for symbol in symbols:
        key = f"{symbol}|{delivery}|{option_contract['settle_coin']}"
        if key not in replay["delivery_evidence"]:
            delivery_missing.append(symbol)
        else:
            delivery_prices.append(float(replay["delivery_evidence"][key]))
    delivery_valid = (
        not delivery_missing and len(delivery_prices) == len(symbols)
        and len(set(delivery_prices)) == 1 and delivery_prices[0] > 0.0
    )
    if not delivery_valid:
        reason_counts["PAIRED_DELIVERY_EVIDENCE_MISSING_OR_CONFLICTING"] += 1

    invalid_archive = replay["invalid_segment_count"] > 0
    pass_coverage = (
        replay["root_present"]
        and replay["valid_segment_count"] > 0
        and not invalid_archive
        and lifecycle_snapshot_count > 0
        and unqualified_snapshot_count == 0
        and not gaps
        and delivery_valid
    )
    if invalid_archive:
        decision = INVALID_DECISION
        reason_code = "ARCHIVE_INTEGRITY_FAILURE"
    elif pass_coverage:
        decision = PASS_DECISION
        reason_code = "COMPLETE_EXECUTABLE_LIFECYCLE_OBSERVED"
    else:
        decision = INSUFFICIENT_DECISION
        if not replay["root_present"] or replay["valid_segment_count"] == 0:
            reason_code = "ARCHIVE_NOT_AVAILABLE"
        elif lifecycle_snapshot_count == 0:
            reason_code = "LIFECYCLE_WINDOW_NOT_OBSERVED"
        elif unqualified_snapshot_count:
            reason_code = "OBSERVED_SNAPSHOTS_NOT_FULLY_QUALIFIED"
        elif gaps:
            reason_code = "LIFECYCLE_COVERAGE_GAPS"
        else:
            reason_code = "PAIRED_DELIVERY_EVIDENCE_INSUFFICIENT"

    gap_digest = _canonical_sha256(gaps)
    source_digest = _canonical_sha256(replay["ordered_inputs"])
    generated = int(generated_at_epoch_ms or time.time() * 1000)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_epoch_ms": generated,
        "lifecycle_id": case["lifecycle_id"],
        "identities": {
            "case_canonical_sha256": capture.canonical_sha256(case),
            "case_file_sha256": sha256_file(case_path),
            "policy_canonical_sha256": capture.canonical_sha256(policy),
            "manifest_canonical_sha256": capture.canonical_sha256(manifest),
            "archive_input_set_sha256": source_digest,
            "audit_implementation_sha256": sha256_file(pathlib.Path(__file__)),
            "executed_release_sha": executed_release_sha,
        },
        "window": {"start_epoch_ms": start, "end_epoch_ms": end,
                   "duration_seconds": (end - start) / 1000.0},
        "archive_integrity": {
            "root_present": replay["root_present"],
            "valid_segment_count": replay["valid_segment_count"],
            "invalid_segment_count": replay["invalid_segment_count"],
            "eligible_snapshot_count_all_time": replay["eligible_snapshot_count"],
            "duplicate_snapshot_count": replay["duplicate_snapshot_count"],
            "checksum_bound_seconds_all_time": replay["checksum_bound_seconds"],
        },
        "lifecycle_coverage": {
            "observed_snapshot_count": lifecycle_snapshot_count,
            "qualified_snapshot_count": len(qualified_timestamps),
            "unqualified_snapshot_count": unqualified_snapshot_count,
            "paired_option_snapshot_count": len(pair_timestamps),
            "hedge_qualified_snapshot_count": hedge_qualified_count,
            "symbol_observation_counts": dict(sorted(symbol_observation_counts.items())),
            "first_qualified_epoch_ms": qualified_timestamps[0] if qualified_timestamps else None,
            "last_qualified_epoch_ms": qualified_timestamps[-1] if qualified_timestamps else None,
            "first_pair_epoch_ms": pair_timestamps[0] if pair_timestamps else None,
            "last_pair_epoch_ms": pair_timestamps[-1] if pair_timestamps else None,
            "last_pair_dte_days": ((end - pair_timestamps[-1]) / 86400000.0
                                   if pair_timestamps else None),
            "coverage_gap_count": len(gaps),
            "maximum_gap_seconds": max((gap["duration_seconds"] for gap in gaps), default=0.0),
            "coverage_gaps_sha256": gap_digest,
            "coverage_gaps_preview": gaps[:20],
            "reason_counts": dict(sorted(reason_counts.items())),
        },
        "settlement": {
            "paired_delivery_evidence_valid": delivery_valid,
            "missing_symbol_count": len(delivery_missing),
            "delivery_price_usdt": delivery_prices[0] if delivery_valid else None,
        },
        "decision": decision,
        "reason_code": reason_code,
        "economic_evidence": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "limitations": [
            "This report qualifies archive completeness only and is not a profitability backtest.",
            "Aggregate evidence cannot reconstruct missing option or hedge observations.",
            "No account was created, selected, funded, queried or traded by this audit.",
        ],
    }


def _atomic_write(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--policy", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--case", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--executed-release-sha")
    args = parser.parse_args()
    report = audit_lifecycle(
        root=args.root, policy_path=args.policy, manifest_path=args.manifest,
        case_path=args.case, executed_release_sha=args.executed_release_sha,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.output:
        _atomic_write(args.output, report)
    print(rendered)
    return 2 if report["decision"] == INVALID_DECISION else 0


if __name__ == "__main__":
    raise SystemExit(main())
