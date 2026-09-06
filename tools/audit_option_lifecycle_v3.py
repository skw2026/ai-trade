#!/usr/bin/env python3
"""Replay v3 raw evidence and audit the first sticky option lifecycle."""

from __future__ import annotations

import argparse
import collections
import json
import lzma
import math
import os
import pathlib
import re
import tempfile
import time
from typing import Any, Dict, Mapping, Sequence

import capture_bybit_option_lifecycle_v3 as capture


SCHEMA_VERSION = "option_lifecycle_audit_v3"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def _number(value: Any, *, field: str, positive: bool = False,
            nonnegative: bool = False) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} is not finite")
    if positive and result <= 0.0:
        raise ValueError(f"{field} must be positive")
    if nonnegative and result < 0.0:
        raise ValueError(f"{field} must be nonnegative")
    return result


def _safe_artifact(root: pathlib.Path, relative: Any,
                   expected_parent: pathlib.Path) -> pathlib.Path:
    if not isinstance(relative, str) or pathlib.PurePosixPath(relative).is_absolute():
        raise ValueError("artifact path is invalid")
    path = (root / relative).resolve()
    if path.parent != expected_parent.resolve() or path.is_symlink() or not path.is_file():
        raise ValueError("artifact path is missing or unsafe")
    return path


def _identity(lifecycle: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "delivery_time_epoch_ms": int(lifecycle["delivery_time_epoch_ms"]),
        "strike": float(lifecycle["strike"]),
        "symbols": list(map(str, lifecycle["symbols"])),
        "settle_coin": capture.SETTLE_COIN,
    }


def _validate_snapshot(snapshot: Mapping[str, Any], *, policy: Mapping[str, Any],
                       manifest: Mapping[str, Any]) -> int:
    if snapshot.get("schema_version") != capture.SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("snapshot schema mismatch")
    if snapshot.get("experiment_id") != policy["experiment_id"]:
        raise ValueError("snapshot experiment mismatch")
    if snapshot.get("policy_canonical_sha256") != capture.canonical_sha256(policy):
        raise ValueError("snapshot policy mismatch")
    if snapshot.get("manifest_canonical_sha256") != capture.canonical_sha256(manifest):
        raise ValueError("snapshot manifest mismatch")
    timestamp = int(snapshot.get("timestamp_epoch_ms") or 0)
    started = int(snapshot.get("poll_started_epoch_ms") or 0)
    completed = int(snapshot.get("snapshot_completed_epoch_ms") or 0)
    if timestamp <= 0 or timestamp != started or completed < started:
        raise ValueError("snapshot timestamps are invalid")
    if completed - started > int(policy["timestamp_contract"]["maximum_poll_latency_seconds"]) * 1000:
        raise ValueError("snapshot poll latency exceeds contract")
    lifecycle = snapshot.get("active_lifecycle")
    tracked = snapshot.get("tracked_options")
    deliveries = snapshot.get("delivery_prices")
    if not isinstance(tracked, list) or not isinstance(deliveries, list):
        raise ValueError("snapshot lifecycle rows are missing")
    if lifecycle is None:
        if tracked or deliveries:
            raise ValueError("snapshot rows exist without lifecycle identity")
        return timestamp
    if not isinstance(lifecycle, Mapping):
        raise ValueError("snapshot lifecycle identity is invalid")
    identity = _identity(lifecycle)
    if lifecycle.get("identity_sha256") != capture.canonical_sha256(identity):
        raise ValueError("snapshot lifecycle identity digest mismatch")
    symbols = identity["symbols"]
    if len(symbols) != 2 or len(set(symbols)) != 2:
        raise ValueError("snapshot lifecycle symbol pair is invalid")
    if {symbol.split("-")[-2].upper() for symbol in symbols} != {"C", "P"}:
        raise ValueError("snapshot lifecycle is not a call/put pair")
    tracked_symbols = [str(row.get("symbol") or "") for row in tracked if isinstance(row, Mapping)]
    if len(tracked_symbols) != len(tracked) or sorted(tracked_symbols) != sorted(symbols):
        raise ValueError("snapshot tracked rows do not bind exact lifecycle pair")
    if int(lifecycle.get("selected_epoch_ms") or 0) < int(manifest["observation_start_epoch_ms"]):
        raise ValueError("snapshot lifecycle selection predates observation boundary")
    if int(lifecycle.get("selected_epoch_ms") or 0) > timestamp:
        raise ValueError("snapshot lifecycle selection is in the future")
    for row in tracked:
        symbol = str(row["symbol"])
        expected_side = "call" if symbol.split("-")[-2].upper() == "C" else "put"
        if (int(row.get("deliveryTime") or 0) != identity["delivery_time_epoch_ms"]
                or _number(row.get("strike"), field="tracked.strike", positive=True) != identity["strike"]
                or str(row.get("optionsType") or "").lower() != expected_side
                or row.get("baseCoin") != capture.BASE_COIN
                or row.get("quoteCoin") != capture.QUOTE_COIN
                or row.get("settleCoin") != capture.SETTLE_COIN
                or row.get("observation_status") not in {"OBSERVED", "TICKER_MISSING", "INSTRUMENT_INACTIVE"}):
            raise ValueError("snapshot tracked contract identity mismatch")
    delivery_symbols = [str(row.get("symbol") or "") for row in deliveries if isinstance(row, Mapping)]
    if len(delivery_symbols) != len(deliveries) or len(set(delivery_symbols)) != len(delivery_symbols):
        raise ValueError("snapshot delivery rows are invalid or duplicated")
    if any(symbol not in symbols for symbol in delivery_symbols):
        raise ValueError("snapshot delivery row escapes lifecycle")
    prices = set()
    for row in deliveries:
        if int(row.get("deliveryTime") or 0) != identity["delivery_time_epoch_ms"]:
            raise ValueError("snapshot delivery timestamp mismatch")
        if row.get("lifecycleIdentitySha256") != lifecycle["identity_sha256"]:
            raise ValueError("snapshot delivery identity mismatch")
        prices.add(_number(row.get("deliveryPrice"), field="delivery.price", positive=True))
    if len(prices) > 1:
        raise ValueError("snapshot paired delivery prices conflict")
    return timestamp


def replay_capture_root(root: pathlib.Path, *, policy: Mapping[str, Any],
                        manifest: Mapping[str, Any]) -> Dict[str, Any]:
    root = root.expanduser().resolve()
    if root.name != capture.CAPTURE_ROOT_NAME:
        raise ValueError("v3 lifecycle capture root mismatch")
    result: Dict[str, Any] = {
        "root_present": root.is_dir(), "valid_segment_count": 0,
        "invalid_segment_count": 0, "eligible_snapshot_count": 0,
        "duplicate_snapshot_count": 0, "ignored_pre_observation_snapshot_count": 0,
        "snapshots": [], "snapshot_segment": {}, "input_identities": [],
    }
    reports_root = root / "reports" / capture.BASE_COIN
    if not reports_root.is_dir():
        return result
    policy_sha = capture.canonical_sha256(policy)
    manifest_sha = capture.canonical_sha256(manifest)
    observation_start = int(manifest["observation_start_epoch_ms"])
    by_timestamp: Dict[int, Dict[str, Any]] = {}
    identities: Dict[int, str] = {}
    segment_by_timestamp: Dict[int, str] = {}
    for report_path in sorted(reports_root.glob("*.json")):
        try:
            if report_path.is_symlink() or not report_path.is_file():
                raise ValueError("capture report path is unsafe")
            report = capture.read_json(report_path)
            if report.get("schema_version") != capture.SCHEMA_VERSION or report.get("status") != "PASS":
                raise ValueError("capture report contract mismatch")
            if report.get("snapshot_schema_version") != capture.SNAPSHOT_SCHEMA_VERSION:
                raise ValueError("capture report snapshot schema mismatch")
            if report.get("state_schema_version") != capture.STATE_SCHEMA_VERSION:
                raise ValueError("capture report state schema mismatch")
            if report.get("capture_root_name") != capture.CAPTURE_ROOT_NAME:
                raise ValueError("capture report root mismatch")
            if report.get("raw_codec") != capture.RAW_CODEC:
                raise ValueError("capture report codec mismatch")
            if report.get("experiment_id") != policy["experiment_id"]:
                raise ValueError("capture report experiment mismatch")
            if report.get("policy_canonical_sha256") != policy_sha or report.get("manifest_canonical_sha256") != manifest_sha:
                raise ValueError("capture report frozen identity mismatch")
            coverage, raw_meta, feature_meta = report.get("coverage"), report.get("raw"), report.get("features")
            if not all(isinstance(value, Mapping) for value in (coverage, raw_meta, feature_meta)):
                raise ValueError("capture report artifact metadata missing")
            start = int(coverage.get("capture_started_epoch_ms") or 0)
            end = int(coverage.get("capture_completed_epoch_ms") or 0)
            if start <= 0 or end < start:
                raise ValueError("capture report coverage invalid")
            raw_path = _safe_artifact(root, raw_meta.get("path"), root / "raw" / capture.BASE_COIN)
            feature_path = _safe_artifact(root, feature_meta.get("path"), root / "features" / capture.BASE_COIN)
            if raw_path.name != f"{report_path.stem}.jsonl.xz" or feature_path.name != f"{report_path.stem}.csv":
                raise ValueError("capture artifact filenames do not bind report")
            if capture.sha256_file(raw_path) != raw_meta.get("sha256") or capture.sha256_file(feature_path) != feature_meta.get("sha256"):
                raise ValueError("capture artifact checksum mismatch")
            lines = 0
            segment_rows: Dict[int, Dict[str, Any]] = {}
            prior = 0
            with lzma.open(raw_path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    lines += 1
                    snapshot = json.loads(line)
                    if not isinstance(snapshot, dict):
                        raise ValueError("snapshot is not an object")
                    timestamp = _validate_snapshot(snapshot, policy=policy, manifest=manifest)
                    if timestamp < prior or timestamp < start or timestamp > end:
                        raise ValueError("snapshot timestamp escapes ordered report coverage")
                    prior = timestamp
                    if timestamp < observation_start:
                        result["ignored_pre_observation_snapshot_count"] += 1
                        continue
                    if timestamp in segment_rows:
                        if capture.canonical_sha256(segment_rows[timestamp]) != capture.canonical_sha256(snapshot):
                            raise ValueError("conflicting duplicate snapshot timestamp within segment")
                        result["duplicate_snapshot_count"] += 1
                    segment_rows[timestamp] = snapshot
            if lines <= 0 or any(int(value or 0) != lines for value in (
                coverage.get("successful_poll_count"), raw_meta.get("snapshot_count"), feature_meta.get("row_count")
            )):
                raise ValueError("capture report row counts conflict")
            for timestamp, snapshot in segment_rows.items():
                digest = capture.canonical_sha256(snapshot)
                if timestamp in identities:
                    if identities[timestamp] != digest:
                        raise ValueError("conflicting duplicate snapshot timestamp")
                    result["duplicate_snapshot_count"] += 1
                    continue
                identities[timestamp] = digest
                by_timestamp[timestamp] = snapshot
                segment_by_timestamp[timestamp] = report_path.stem
            result["input_identities"].append({
                "report_sha256": capture.sha256_file(report_path),
                "raw_sha256": str(raw_meta["sha256"]),
                "features_sha256": str(feature_meta["sha256"]),
                "eligible_snapshot_count": len(segment_rows),
            })
            result["valid_segment_count"] += 1
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError, lzma.LZMAError):
            result["invalid_segment_count"] += 1
    timestamps = sorted(by_timestamp)
    result["snapshots"] = [by_timestamp[timestamp] for timestamp in timestamps]
    result["snapshot_segment"] = {str(timestamp): segment_by_timestamp[timestamp] for timestamp in timestamps}
    result["eligible_snapshot_count"] = len(timestamps)
    return result


def _hedge_reason(snapshot: Mapping[str, Any], *, policy: Mapping[str, Any]) -> str | None:
    try:
        timestamp = int(snapshot["timestamp_epoch_ms"])
        ticker, book = snapshot["hedge_ticker"], snapshot["hedge_orderbook_l1"]
        if not isinstance(ticker, Mapping) or not isinstance(book, Mapping):
            return "HEDGE_BBO_MISSING"
        bids, asks = book["b"], book["a"]
        bid = _number(bids[0][0], field="hedge.bid", positive=True)
        ask = _number(asks[0][0], field="hedge.ask", positive=True)
        bid_size = _number(bids[0][1], field="hedge.bid_size", nonnegative=True)
        ask_size = _number(asks[0][1], field="hedge.ask_size", nonnegative=True)
        ticker_bid = _number(ticker.get("bid1Price"), field="hedge.ticker_bid", positive=True)
        ticker_ask = _number(ticker.get("ask1Price"), field="hedge.ticker_ask", positive=True)
        book_time = int(book["ts"])
        if ask < bid or ticker_ask < ticker_bid:
            return "HEDGE_CROSSED_BBO"
        minimum = float(policy["lifecycle_gate"]["minimum_hedge_bbo_size_btc"])
        if min(bid_size, ask_size) + 1e-12 < minimum:
            return "HEDGE_INSUFFICIENT_SIZE"
        maximum = int(policy["timestamp_contract"]["maximum_absolute_hedge_book_skew_seconds"]) * 1000
        if abs(book_time - timestamp) > maximum:
            return "HEDGE_BOOK_SKEW_EXCEEDS_BOUND"
    except (IndexError, KeyError, TypeError, ValueError):
        return "HEDGE_BBO_INVALID"
    return None


def _tracked_reason(snapshot: Mapping[str, Any], *, entry: bool,
                    policy: Mapping[str, Any]) -> str | None:
    rows = snapshot["tracked_options"]
    for row in rows:
        if row.get("observation_status") != "OBSERVED":
            return str(row.get("observation_status") or "TRACKED_STATUS_MISSING")
        try:
            _number(row.get("indexPrice"), field="option.index", positive=True)
            _number(row.get("delta"), field="option.delta")
            if entry:
                bid = _number(row.get("bid1Price"), field="option.bid", positive=True)
                ask = _number(row.get("ask1Price"), field="option.ask", positive=True)
                bid_size = _number(row.get("bid1Size"), field="option.bid_size", nonnegative=True)
                ask_size = _number(row.get("ask1Size"), field="option.ask_size", nonnegative=True)
                if ask < bid:
                    return "OPTION_ENTRY_CROSSED_BBO"
                minimum = float(policy["lifecycle_gate"]["minimum_option_entry_size_btc"])
                if min(bid_size, ask_size) + 1e-12 < minimum:
                    return "OPTION_ENTRY_INSUFFICIENT_SIZE"
        except (TypeError, ValueError):
            return "OPTION_TRACKED_FIELDS_INVALID"
    return None


def _selection_is_deterministic(snapshot: Mapping[str, Any],
                                policy: Mapping[str, Any]) -> bool:
    lifecycle = snapshot["active_lifecycle"]
    rows = snapshot.get("discovery_options")
    if not isinstance(rows, list):
        return False
    pairs: Dict[tuple[int, float], Dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        side = str(row.get("optionsType") or "").lower()
        if side not in {"call", "put"}:
            continue
        key = (int(row.get("deliveryTime") or 0),
               _number(row.get("strike"), field="discovery.strike", positive=True))
        if side in pairs.setdefault(key, {}):
            return False
        pairs[key][side] = row
    complete = [(key, sides) for key, sides in pairs.items()
                if set(sides) == {"call", "put"}]
    if not complete:
        return False
    target = float(policy["discovery_contract"]["target_dte_days"])
    (delivery, strike), sides = min(complete, key=lambda item: (
        abs(_number(item[1]["call"].get("dteDays"), field="discovery.dte") - target),
        abs(_number(item[1]["call"].get("moneyness"), field="discovery.moneyness")),
        item[0][0], item[0][1],
    ))
    return bool(
        delivery == int(lifecycle["delivery_time_epoch_ms"])
        and strike == float(lifecycle["strike"])
        and [str(sides[side]["symbol"]) for side in ("call", "put")] == list(lifecycle["symbols"])
    )


def audit(*, root: pathlib.Path, policy_path: pathlib.Path, manifest_path: pathlib.Path,
          executed_release_sha: str | None = None,
          generated_at_epoch_ms: int | None = None) -> Dict[str, Any]:
    if executed_release_sha is not None and not SHA_PATTERN.fullmatch(executed_release_sha):
        raise ValueError("executed release SHA is invalid")
    policy, manifest = capture.load_contract(policy_path, manifest_path)
    replay = replay_capture_root(root, policy=policy, manifest=manifest)
    generated = int(generated_at_epoch_ms or time.time() * 1000)
    reasons: collections.Counter[str] = collections.Counter()
    lifecycle_id = None
    selected = []
    for snapshot in replay["snapshots"]:
        lifecycle = snapshot.get("active_lifecycle")
        if lifecycle is not None:
            lifecycle_id = str(lifecycle["lifecycle_id"])
            break
    if lifecycle_id:
        selected = [snapshot for snapshot in replay["snapshots"]
                    if isinstance(snapshot.get("active_lifecycle"), Mapping)
                    and snapshot["active_lifecycle"].get("lifecycle_id") == lifecycle_id]
    entry_valid = False
    qualified: list[int] = []
    delivery_valid = False
    delivery_price = None
    delivery_time = None
    segment_ids: set[str] = set()
    maximum_internal_gap = 0
    if selected:
        lifecycle = selected[0]["active_lifecycle"]
        delivery_time = int(lifecycle["delivery_time_epoch_ms"])
        deterministic_selection = _selection_is_deterministic(selected[0], policy)
        if not deterministic_selection:
            reasons["NONDETERMINISTIC_DISCOVERY_SELECTION"] += 1
        for index, snapshot in enumerate(selected):
            timestamp = int(snapshot["timestamp_epoch_ms"])
            segment_ids.add(replay["snapshot_segment"][str(timestamp)])
            tracked_reason = _tracked_reason(snapshot, entry=index == 0, policy=policy)
            hedge_reason = _hedge_reason(snapshot, policy=policy)
            if index == 0:
                entry_valid = tracked_reason is None and deterministic_selection
            if tracked_reason:
                reasons[tracked_reason] += 1
            if hedge_reason:
                reasons[hedge_reason] += 1
            if tracked_reason is None and hedge_reason is None and timestamp <= delivery_time:
                qualified.append(timestamp)
            rows = snapshot["delivery_prices"]
            if len(rows) == 2 and {str(row["symbol"]) for row in rows} == set(lifecycle["symbols"]):
                prices = {_number(row["deliveryPrice"], field="delivery.price", positive=True) for row in rows}
                if len(prices) == 1:
                    delivery_valid, delivery_price = True, next(iter(prices))
        maximum_internal_gap = max((right - left for left, right in zip(qualified, qualified[1:])), default=0)
    maximum_gap_ms = int(policy["lifecycle_gate"]["maximum_snapshot_gap_seconds"]) * 1000
    terminal_gap_ms = ((delivery_time - qualified[-1]) if delivery_time and qualified else None)
    cross_segment = len(segment_ids) >= 2
    phase_pass = bool(
        replay["root_present"] and replay["valid_segment_count"] >= 2
        and replay["invalid_segment_count"] == 0 and selected and entry_valid
        and cross_segment and len(qualified) >= 2 and maximum_internal_gap <= maximum_gap_ms
    )
    if replay["invalid_segment_count"]:
        decision, reason_code = policy["lifecycle_gate"]["invalid_decision"], "ARCHIVE_INTEGRITY_FAILURE"
    elif delivery_valid:
        complete = bool(
            phase_pass and terminal_gap_ms is not None
            and 0 <= terminal_gap_ms <= int(policy["lifecycle_gate"]["maximum_terminal_gap_seconds"]) * 1000
            and generated >= int(delivery_time or 0)
        )
        decision = policy["lifecycle_gate"]["pass_decision"] if complete else policy["lifecycle_gate"]["insufficient_decision"]
        reason_code = "FIRST_LIFECYCLE_COMPLETE" if complete else "FIRST_LIFECYCLE_COVERAGE_INSUFFICIENT"
    elif delivery_time is not None and generated > delivery_time + int(policy["lifecycle_gate"]["maximum_terminal_gap_seconds"]) * 1000:
        decision, reason_code = policy["lifecycle_gate"]["insufficient_decision"], "PAIRED_DELIVERY_EVIDENCE_MISSING"
    else:
        decision, reason_code = policy["lifecycle_gate"]["wait_decision"], (
            "ACTIVE_LIFECYCLE_CAPTURED" if phase_pass else "STARTUP_EVIDENCE_PENDING"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_epoch_ms": generated,
        "identities": {
            "experiment_id": policy["experiment_id"],
            "policy_canonical_sha256": capture.canonical_sha256(policy),
            "manifest_canonical_sha256": capture.canonical_sha256(manifest),
            "archive_input_set_sha256": capture.canonical_sha256(replay["input_identities"]),
            "audit_implementation_sha256": capture.sha256_file(pathlib.Path(__file__)),
            "executed_release_sha": executed_release_sha,
        },
        "archive_integrity": {
            "root_present": replay["root_present"],
            "valid_segment_count": replay["valid_segment_count"],
            "invalid_segment_count": replay["invalid_segment_count"],
            "checksum_bound_snapshot_count": replay["eligible_snapshot_count"],
            "duplicate_snapshot_count": replay["duplicate_snapshot_count"],
            "ignored_pre_observation_snapshot_count": replay["ignored_pre_observation_snapshot_count"],
        },
        "startup_gate": {
            "status": "PASS" if phase_pass else ("FAIL" if replay["invalid_segment_count"] else "WAIT"),
            "lifecycle_id": lifecycle_id,
            "exact_pair_selected": bool(selected), "entry_executable": entry_valid,
            "selected_snapshot_count": len(selected), "qualified_snapshot_count": len(qualified),
            "distinct_segment_count": len(segment_ids),
            "sticky_cross_segment_continuity": cross_segment,
            "maximum_internal_gap_seconds": maximum_internal_gap / 1000.0,
            "reason_counts": dict(sorted(reasons.items())),
        },
        "lifecycle_gate": {
            "delivery_time_epoch_ms": delivery_time,
            "last_qualified_epoch_ms": qualified[-1] if qualified else None,
            "terminal_gap_seconds": terminal_gap_ms / 1000.0 if terminal_gap_ms is not None else None,
            "paired_delivery_evidence_valid": delivery_valid,
            "delivery_price_usdt": delivery_price,
        },
        "decision": decision, "reason_code": reason_code,
        "economic_evidence": False, "promotion_authority": False,
        "demo_activation_authorized": False, "live_activation_authorized": False,
        "limitations": [
            "This gate qualifies immutable lifecycle data only; it is not a profitability backtest.",
            "Mutable collector state is excluded from evidentiary reconstruction.",
            "No account, credential, funding action or order is used by this public-data audit.",
        ],
    }


def _atomic_write(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        pathlib.Path(temporary_name).replace(path)
    finally:
        pathlib.Path(temporary_name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--policy", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--executed-release-sha")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(
            root=args.root, policy_path=args.policy, manifest_path=args.manifest,
            executed_release_sha=args.executed_release_sha,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "identities": {"executed_release_sha": args.executed_release_sha},
            "archive_integrity": {"root_present": False, "valid_segment_count": 0,
                                  "invalid_segment_count": 1, "checksum_bound_snapshot_count": 0},
            "startup_gate": {"status": "FAIL", "reason_counts": {"AUDIT_CONTRACT_FAILURE": 1}},
            "lifecycle_gate": {"paired_delivery_evidence_valid": False},
            "decision": "INVALID_OPTION_LIFECYCLE_ARCHIVE", "reason_code": str(exc),
            "economic_evidence": False, "promotion_authority": False,
            "demo_activation_authorized": False, "live_activation_authorized": False,
        }
    _atomic_write(args.output, report)
    return 2 if report["decision"] == "INVALID_OPTION_LIFECYCLE_ARCHIVE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
