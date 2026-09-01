#!/usr/bin/env python3
"""Replay settlement-bound option captures into a frozen sequential VRP payoff audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import math
import pathlib
import random
import statistics
import time
from typing import Any, Dict, Iterable, Mapping, Sequence

import capture_bybit_option_vrp_v2 as capture


SCHEMA_VERSION = "option_variance_risk_premium_sequential_payoff_audit_v1"
POLICY_SCHEMA_VERSION = "option_variance_risk_premium_sequential_payoff_policy_v1"
MANIFEST_SCHEMA_VERSION = "option_variance_risk_premium_sequential_payoff_manifest_v1"
FROZEN_POLICY_IDENTITY_SHA256 = "e1902110278fb2c72ec091a73f2cdb38ba394dfbc4741864ca85b9c3d08a17ee"
FROZEN_MANIFEST_IDENTITY_SHA256 = "446625e67754f1fd07e149e4ff5bd1623677138aef028e40ce0d35b8a0284a9d"
FROZEN_POLICY_IDENTITY_SHA256_V2 = "6f23634e0f5e6a708d76387f6552e9089a0ef830bbb82790300d97ececd5530b"
FROZEN_MANIFEST_IDENTITY_SHA256_V2 = "13b62a179c2e3131762918063bfecfb1a2f9c853693144d0dc2a8428b2f58aeb"
FROZEN_CONTRACTS = {
    FROZEN_POLICY_IDENTITY_SHA256: {
        "manifest_sha256": FROZEN_MANIFEST_IDENTITY_SHA256,
        "experiment_id": "btc_bybit_usdt_option_vrp_sequential_payoff_v1",
        "policy_path": "config/option_variance_risk_premium_sequential_payoff.json",
        "action_ids": ["no_trade", "short_atm_straddle_7d", "long_atm_straddle_7d"],
    },
    FROZEN_POLICY_IDENTITY_SHA256_V2: {
        "manifest_sha256": FROZEN_MANIFEST_IDENTITY_SHA256_V2,
        "experiment_id": "btc_bybit_usdt_option_vrp_1d_sequential_payoff_v2",
        "policy_path": "config/option_variance_risk_premium_sequential_payoff_v2.json",
        "action_ids": ["no_trade", "short_atm_straddle_1d", "long_atm_straddle_1d"],
    },
}


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return payload


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any, *, field: str, positive: bool = False, nonnegative: bool = False) -> float:
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


def load_frozen_contract(
    policy_path: pathlib.Path, manifest_path: pathlib.Path
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    policy, manifest = read_json(policy_path), read_json(manifest_path)
    policy_identity = capture.canonical_sha256(policy)
    manifest_identity = capture.canonical_sha256(manifest)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("option VRP sequential policy schema mismatch")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("option VRP sequential manifest schema mismatch")
    frozen = FROZEN_CONTRACTS.get(policy_identity)
    if frozen is None:
        raise ValueError(f"option VRP sequential policy identity mismatch: {policy_identity}")
    if manifest_identity != frozen["manifest_sha256"]:
        raise ValueError(f"option VRP sequential manifest identity mismatch: {manifest_identity}")
    if manifest.get("policy_canonical_sha256") != policy_identity:
        raise ValueError("manifest policy identity mismatch")
    if (
        policy.get("experiment_id") != frozen["experiment_id"]
        or manifest.get("experiment_id") != frozen["experiment_id"]
        or manifest.get("policy_path") != frozen["policy_path"]
    ):
        raise ValueError("frozen experiment identity or policy path mismatch")
    scope = policy.get("capture_contract", {})
    if manifest.get("capture_scope_identity_sha256") != scope.get("scope_identity_sha256"):
        raise ValueError("manifest capture scope mismatch")
    if int(manifest.get("observation_start_epoch_ms") or 0) <= 0:
        raise ValueError("manifest observation start is invalid")
    authorities = policy.get("authorities", {})
    if any(bool(authorities.get(name)) for name in (
        "promotion_authority", "demo_activation_authorized", "live_activation_authorized"
    )):
        raise ValueError("option VRP sequential policy cannot grant activation authority")
    if any(bool(manifest.get(name)) for name in (
        "promotion_authority", "demo_activation_authorized", "live_activation_authorized"
    )):
        raise ValueError("option VRP sequential manifest cannot grant activation authority")
    actions = policy.get("actions")
    if not isinstance(actions, list) or [row.get("action_id") for row in actions] != frozen["action_ids"]:
        raise ValueError("frozen action order mismatch")
    entry_contract = policy.get("entry_contract", {})
    cadence = entry_contract.get("expected_expiry_cluster_cadence_days")
    if cadence is not None:
        boundaries = entry_contract.get("boundary_entry_dte_days")
        reviews = policy.get("sequential_reviews")
        if (
            not isinstance(boundaries, list)
            or not boundaries
            or not isinstance(reviews, list)
            or not reviews
            or float(cadence) <= 0.0
            or max(float(value) for value in boundaries)
            + int(reviews[0]["minimum_completed_expiries"]) * float(cadence)
            > int(reviews[0]["day"])
        ):
            raise ValueError("first sequential review is infeasible for the frozen expiry cadence")
    return policy, manifest


def _safe_artifact(root: pathlib.Path, recorded: Any, expected_parent: pathlib.Path) -> pathlib.Path:
    relative = pathlib.Path(str(recorded or ""))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("capture artifact path is unsafe")
    resolved = (root / relative).resolve()
    if resolved.parent != expected_parent.resolve() or not resolved.is_file() or resolved.is_symlink():
        raise ValueError("capture artifact is missing or unsafe")
    return resolved


def _merged_duration_ms(intervals: Sequence[tuple[int, int]]) -> int:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if start <= 0 or end < start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def _validate_snapshot(snapshot: Mapping[str, Any], policy: Mapping[str, Any]) -> int:
    scope = policy["capture_contract"]
    if snapshot.get("schema_version") != scope["snapshot_schema_version"]:
        raise ValueError("snapshot schema mismatch")
    if snapshot.get("scope_identity_sha256") != scope["scope_identity_sha256"]:
        raise ValueError("snapshot scope mismatch")
    if snapshot.get("delivery_query_status") != "PASS":
        raise ValueError("snapshot delivery query did not pass")
    selection = snapshot.get("selection_contract")
    expected_selection = {
        "minimum_dte_days": float(scope["minimum_dte_days"]),
        "maximum_dte_days": float(scope["maximum_dte_days"]),
        "maximum_absolute_moneyness": float(scope["maximum_absolute_moneyness"]),
        "scope_identity_sha256": scope["scope_identity_sha256"],
        "settle_coin": scope["settle_coin"],
    }
    if selection != expected_selection:
        raise ValueError("snapshot selection contract drift")
    timestamp = int(snapshot.get("timestamp_epoch_ms") or 0)
    if timestamp <= 0:
        raise ValueError("snapshot timestamp is invalid")
    options = snapshot.get("scoped_options")
    deliveries = snapshot.get("delivery_prices")
    if not isinstance(options, list) or not isinstance(deliveries, list):
        raise ValueError("snapshot option or delivery rows are invalid")
    for row in options:
        if not isinstance(row, Mapping):
            raise ValueError("snapshot option row is invalid")
        if any(str(row.get(field) or "").upper() != expected for field, expected in (
            ("baseCoin", "BTC"), ("quoteCoin", "USDT"), ("settleCoin", "USDT")
        )):
            raise ValueError("snapshot option unit scope mismatch")
        _number(row.get("deliveryTime"), field="option.deliveryTime", positive=True)
        _number(row.get("minOrderQty"), field="option.minOrderQty", positive=True)
        _number(row.get("qtyStep"), field="option.qtyStep", positive=True)
        delivery_fee_rate = _number(row.get("deliveryFeeRate"), field="option.deliveryFeeRate", positive=True)
        if abs(delivery_fee_rate - float(policy["cost_contract"]["delivery_fee_rate"])) > 1e-12:
            raise ValueError("snapshot delivery fee contract drift")
    for row in deliveries:
        if not isinstance(row, Mapping):
            raise ValueError("snapshot delivery row is invalid")
        if row.get("settleCoin") != scope["settle_coin"] or row.get("scopeIdentitySha256") != scope["scope_identity_sha256"]:
            raise ValueError("delivery scope mismatch")
        _number(row.get("deliveryTime"), field="delivery.deliveryTime", positive=True)
        _number(row.get("deliveryPrice"), field="delivery.deliveryPrice", positive=True)
    return timestamp


def replay_capture_root(
    root: pathlib.Path, *, policy: Mapping[str, Any], manifest: Mapping[str, Any]
) -> Dict[str, Any]:
    scope = policy["capture_contract"]
    root = root.expanduser().resolve()
    if root.name != scope["capture_root_name"]:
        raise ValueError("capture root identity mismatch")
    observation_start = int(manifest["observation_start_epoch_ms"])
    result: Dict[str, Any] = {
        "root": str(root), "root_present": root.is_dir(), "valid_segment_count": 0,
        "invalid_segment_count": 0, "invalid_segments": [], "ignored_pre_observation_snapshot_count": 0,
        "ignored_pre_observation_segment_count": 0,
        "eligible_snapshot_count": 0, "duplicate_snapshot_count": 0, "successful_poll_count": 0,
        "checksum_bound_seconds": 0.0, "first_eligible_epoch_ms": None, "last_eligible_epoch_ms": None,
        "ordered_inputs": [], "snapshots": [], "delivery_evidence": {},
    }
    reports_root = root / "reports" / capture.BASE_COIN
    if not reports_root.is_dir():
        return result
    intervals: list[tuple[int, int]] = []
    snapshot_by_timestamp: Dict[int, Dict[str, Any]] = {}
    snapshot_identity: Dict[int, str] = {}
    delivery_evidence: Dict[tuple[str, int, str], float] = {}
    for report_path in sorted(reports_root.glob("*.json")):
        try:
            report = read_json(report_path)
            if report.get("schema_version") != scope["capture_schema_version"] or report.get("status") != "PASS":
                raise ValueError("capture report contract mismatch")
            if report.get("snapshot_schema_version") != scope["snapshot_schema_version"]:
                raise ValueError("capture snapshot contract mismatch")
            if report.get("scope_identity_sha256") != scope["scope_identity_sha256"]:
                raise ValueError("capture report scope mismatch")
            if report.get("capture_root_name") != scope["capture_root_name"]:
                raise ValueError("capture report root mismatch")
            if report.get("settle_coin") != scope["settle_coin"]:
                raise ValueError("capture report settle coin mismatch")
            if report.get("quality", {}).get("delivery_query_status") != "PASS":
                raise ValueError("capture report delivery query did not pass")
            coverage = report.get("coverage")
            if not isinstance(coverage, Mapping):
                raise ValueError("capture coverage missing")
            segment_start = int(coverage.get("capture_started_epoch_ms") or 0)
            segment_end = int(coverage.get("capture_completed_epoch_ms") or 0)
            if segment_start <= 0 or segment_end < segment_start:
                raise ValueError("capture coverage invalid")
            if segment_end < observation_start:
                result["ignored_pre_observation_segment_count"] += 1
                continue
            if report.get("raw_codec") != scope["raw_codec"]:
                raise ValueError("capture raw codec mismatch")
            raw_meta, feature_meta = report.get("raw"), report.get("features")
            if not isinstance(raw_meta, Mapping) or not isinstance(feature_meta, Mapping):
                raise ValueError("capture artifact metadata missing")
            raw_path = _safe_artifact(root, raw_meta.get("path"), root / "raw" / capture.BASE_COIN)
            feature_path = _safe_artifact(root, feature_meta.get("path"), root / "features" / capture.BASE_COIN)
            if raw_path.suffix != ".xz":
                raise ValueError("capture raw path does not use XZ")
            if sha256_file(raw_path) != raw_meta.get("sha256") or sha256_file(feature_path) != feature_meta.get("sha256"):
                raise ValueError("capture artifact checksum mismatch")
            previous_timestamp = 0
            segment_snapshots: Dict[int, Dict[str, Any]] = {}
            segment_identities: Dict[int, str] = {}
            segment_delivery: Dict[tuple[str, int, str], float] = {}
            segment_pre_observation = 0
            segment_line_count = 0
            with lzma.open(raw_path, "rt", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    segment_line_count += 1
                    snapshot = json.loads(line)
                    if not isinstance(snapshot, dict):
                        raise ValueError(f"snapshot is not an object at line {line_number}")
                    timestamp = _validate_snapshot(snapshot, policy)
                    if timestamp < previous_timestamp:
                        raise ValueError("snapshot time moved backwards")
                    previous_timestamp = timestamp
                    if timestamp < segment_start or timestamp > segment_end:
                        raise ValueError("snapshot timestamp escapes report coverage")
                    if timestamp < observation_start:
                        segment_pre_observation += 1
                        continue
                    identity = capture.canonical_sha256(snapshot)
                    if timestamp in segment_identities and segment_identities[timestamp] != identity:
                        raise ValueError("conflicting duplicate snapshot timestamp within segment")
                    segment_identities[timestamp] = identity
                    segment_snapshots[timestamp] = snapshot
                    for row in snapshot["delivery_prices"]:
                        key = (str(row["symbol"]), int(row["deliveryTime"]), str(row["settleCoin"]))
                        price = _number(row["deliveryPrice"], field="delivery.deliveryPrice", positive=True)
                        if key in segment_delivery and segment_delivery[key] != price:
                            raise ValueError("conflicting delivery evidence within segment")
                        segment_delivery[key] = price
            if (
                segment_line_count <= 0
                or int(raw_meta.get("snapshot_count") or 0) != segment_line_count
                or int(coverage.get("successful_poll_count") or 0) != segment_line_count
                or int(feature_meta.get("row_count") or 0) != segment_line_count
            ):
                raise ValueError("capture report poll or row count mismatch")
            for timestamp, identity in segment_identities.items():
                if timestamp in snapshot_identity and snapshot_identity[timestamp] != identity:
                    raise ValueError("conflicting duplicate snapshot timestamp")
            for key, price in segment_delivery.items():
                if key in delivery_evidence and delivery_evidence[key] != price:
                    raise ValueError("conflicting delivery evidence")
            segment_eligible = sum(timestamp not in snapshot_identity for timestamp in segment_snapshots)
            result["duplicate_snapshot_count"] += len(segment_snapshots) - segment_eligible
            result["ignored_pre_observation_snapshot_count"] += segment_pre_observation
            for timestamp, snapshot in segment_snapshots.items():
                if timestamp not in snapshot_identity:
                    snapshot_identity[timestamp] = segment_identities[timestamp]
                    snapshot_by_timestamp[timestamp] = snapshot
            delivery_evidence.update(segment_delivery)
            if segment_eligible:
                intervals.append((max(segment_start, observation_start), segment_end))
            result["ordered_inputs"].append({
                "report": report_path.relative_to(root).as_posix(),
                "report_sha256": sha256_file(report_path),
                "raw": raw_path.relative_to(root).as_posix(), "raw_sha256": raw_meta["sha256"],
                "features": feature_path.relative_to(root).as_posix(), "features_sha256": feature_meta["sha256"],
                "eligible_snapshot_count": segment_eligible,
            })
            result["valid_segment_count"] += 1
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            result["invalid_segment_count"] += 1
            result["invalid_segments"].append({"report": report_path.name, "reason": str(exc)})
    snapshots = [snapshot_by_timestamp[key] for key in sorted(snapshot_by_timestamp)]
    result["snapshots"] = snapshots
    result["eligible_snapshot_count"] = len(snapshots)
    result["successful_poll_count"] = len(snapshots)
    result["checksum_bound_seconds"] = _merged_duration_ms(intervals) / 1000.0
    if snapshots:
        result["first_eligible_epoch_ms"] = int(snapshots[0]["timestamp_epoch_ms"])
        result["last_eligible_epoch_ms"] = int(snapshots[-1]["timestamp_epoch_ms"])
    result["delivery_evidence"] = {
        f"{symbol}|{delivery_time}|{settle}": price
        for (symbol, delivery_time, settle), price in sorted(delivery_evidence.items())
    }
    return result


def option_leg_economics(
    *, position_sign: int, quantity: float, bid: float, ask: float, index_price: float,
    strike: float, delivery_price: float, option_type: str, tick_size: float,
    option_fee_rate: float, fee_cap_fraction: float, delivery_fee_rate: float,
    delivery_fee_cap_fraction: float,
    stress_slippage_ticks: float,
) -> Dict[str, float]:
    values = (quantity, bid, ask, index_price, strike, delivery_price, tick_size)
    if position_sign not in {-1, 1} or any(not math.isfinite(value) for value in values):
        raise ValueError("option leg economics inputs are invalid")
    if quantity <= 0.0 or bid <= 0.0 or ask < bid or index_price <= 0.0 or strike <= 0.0 or delivery_price <= 0.0 or tick_size <= 0.0:
        raise ValueError("option leg economics inputs are outside the contract")
    side = option_type.lower()
    if side == "call":
        intrinsic = max(delivery_price - strike, 0.0)
    elif side == "put":
        intrinsic = max(strike - delivery_price, 0.0)
    else:
        raise ValueError("option type is invalid")
    midpoint = (bid + ask) / 2.0
    execution_price = ask if position_sign > 0 else bid
    gross_pnl = position_sign * quantity * (intrinsic - midpoint)
    spread_cost = quantity * abs(execution_price - midpoint)
    option_fee = quantity * min(index_price * option_fee_rate, execution_price * fee_cap_fraction)
    delivery_fee = quantity * min(
        delivery_price * delivery_fee_rate, intrinsic * delivery_fee_cap_fraction
    ) if intrinsic > 0.0 else 0.0
    stress_increment = quantity * tick_size * stress_slippage_ticks
    base_net = gross_pnl - spread_cost - option_fee - delivery_fee
    return {
        "intrinsic_per_btc": intrinsic, "gross_pnl": gross_pnl, "spread_cost": spread_cost,
        "option_fee": option_fee, "delivery_fee": delivery_fee, "stress_increment": stress_increment,
        "base_net": base_net, "stress_net": base_net - stress_increment,
    }


def _round_quantity(value: float, step: float) -> float:
    magnitude = math.floor(abs(value) / step + 0.5) * step
    return math.copysign(magnitude, value) if magnitude else 0.0


def hedge_ledger(
    *, targets: Sequence[Mapping[str, float]], final_quote: Mapping[str, float],
    fee_rate: float, quantity_step: float, minimum_trade_quantity: float,
    stress_slippage_bps: float,
) -> Dict[str, Any]:
    position = 0.0
    ledger: list[Dict[str, float | str]] = []
    gross_pnl = spread_cost = hedge_fee = stress_increment = 0.0
    rows: list[Mapping[str, float]] = list(targets) + [dict(final_quote, target=0.0)]
    for index, row in enumerate(rows):
        target = _round_quantity(_number(row.get("target"), field="hedge.target"), quantity_step)
        change = target - position
        if index < len(rows) - 1 and abs(change) + 1e-12 < minimum_trade_quantity:
            continue
        if abs(change) < 1e-12:
            continue
        bid = _number(row.get("bid"), field="hedge.bid", positive=True)
        ask = _number(row.get("ask"), field="hedge.ask", positive=True)
        if ask < bid:
            raise ValueError("hedge quote is crossed")
        midpoint = (bid + ask) / 2.0
        execution = ask if change > 0.0 else bid
        trade_gross = -change * midpoint
        trade_spread = abs(change) * abs(execution - midpoint)
        trade_fee = abs(change) * execution * fee_rate
        trade_stress = abs(change) * midpoint * stress_slippage_bps / 10000.0
        gross_pnl += trade_gross
        spread_cost += trade_spread
        hedge_fee += trade_fee
        stress_increment += trade_stress
        ledger.append({
            "timestamp_epoch_ms": int(row.get("timestamp_epoch_ms") or 0),
            "side": "buy" if change > 0.0 else "sell", "quantity_btc": abs(change),
            "position_after_btc": target, "bid": bid, "ask": ask, "midpoint": midpoint,
            "execution_price": execution, "gross_cashflow_at_mid": trade_gross,
            "spread_cost": trade_spread, "fee": trade_fee, "stress_increment": trade_stress,
        })
        position = target
    if abs(position) > 1e-12:
        raise ValueError("residual hedge position was not closed")
    base_net = gross_pnl - spread_cost - hedge_fee
    return {
        "ledger": ledger, "gross_pnl": gross_pnl, "spread_cost": spread_cost,
        "hedge_fee": hedge_fee, "stress_increment": stress_increment,
        "base_net": base_net, "stress_net": base_net - stress_increment,
        "residual_quantity_btc": position,
    }


def _option_pairs(snapshot: Mapping[str, Any], delivery_time: int) -> Dict[float, Dict[str, Mapping[str, Any]]]:
    pairs: Dict[float, Dict[str, Mapping[str, Any]]] = {}
    for row in snapshot.get("scoped_options", []):
        if int(row.get("deliveryTime") or 0) != delivery_time:
            continue
        side = str(row.get("optionsType") or "").lower()
        if side not in {"call", "put"}:
            continue
        strike = _number(row.get("strike"), field="option.strike", positive=True)
        pairs.setdefault(strike, {})[side] = row
    return {strike: sides for strike, sides in pairs.items() if set(sides) == {"call", "put"}}


def _hedge_quote(snapshot: Mapping[str, Any], policy: Mapping[str, Any]) -> Dict[str, float]:
    ticker = snapshot.get("hedge_ticker")
    book = snapshot.get("hedge_orderbook_l1")
    if not isinstance(ticker, Mapping) or not isinstance(book, Mapping):
        raise ValueError("hedge quote is missing")
    bids, asks = book.get("b"), book.get("a")
    if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
        raise ValueError("hedge order book BBO is missing")
    bid = _number(bids[0][0], field="hedge.bid", positive=True)
    ask = _number(asks[0][0], field="hedge.ask", positive=True)
    ticker_bid = _number(ticker.get("bid1Price"), field="hedge.tickerBid", positive=True)
    ticker_ask = _number(ticker.get("ask1Price"), field="hedge.tickerAsk", positive=True)
    if ask < bid or ticker_ask < ticker_bid:
        raise ValueError("hedge BBO is crossed")
    timestamp = int(snapshot["timestamp_epoch_ms"])
    book_timestamp = int(book.get("ts") or timestamp)
    maximum_age = int(policy["hedge_contract"]["maximum_hedge_book_age_seconds"]) * 1000
    if abs(timestamp - book_timestamp) > maximum_age:
        raise ValueError("hedge book is stale")
    return {"timestamp_epoch_ms": timestamp, "bid": bid, "ask": ask}


def _delivery_price(
    delivery_evidence: Mapping[str, float], symbols: Sequence[str], delivery_time: int, settle_coin: str
) -> float | None:
    prices = {
        float(delivery_evidence[f"{symbol}|{delivery_time}|{settle_coin}"])
        for symbol in symbols if f"{symbol}|{delivery_time}|{settle_coin}" in delivery_evidence
    }
    if not prices:
        return None
    if len(prices) != 1 or len([
        symbol for symbol in symbols if f"{symbol}|{delivery_time}|{settle_coin}" in delivery_evidence
    ]) != len(symbols):
        raise ValueError("paired option delivery evidence is incomplete or conflicting")
    return next(iter(prices))


def build_episode(
    *, snapshots: Sequence[Mapping[str, Any]], delivery_evidence: Mapping[str, float],
    policy: Mapping[str, Any], action: Mapping[str, Any], delivery_time: int,
    target_dte_days: float | None = None,
) -> Dict[str, Any]:
    action_id = str(action["action_id"])
    if action_id == "no_trade":
        return {"action_id": action_id, "delivery_time_epoch_ms": delivery_time, "state": "complete", "base_net_bps": 0.0, "stress_net_bps": 0.0}
    target_dte = float(target_dte_days if target_dte_days is not None else action["target_entry_dte_days"])
    crossing_gap = int(policy["entry_contract"]["maximum_crossing_gap_seconds"]) * 1000
    prior: tuple[int, Mapping[str, Any]] | None = None
    entry: Mapping[str, Any] | None = None
    crossed_target = False
    for snapshot in snapshots:
        timestamp = int(snapshot["timestamp_epoch_ms"])
        if timestamp >= delivery_time:
            break
        pairs = _option_pairs(snapshot, delivery_time)
        if not pairs:
            continue
        dte = (delivery_time - timestamp) / 86400000.0
        if dte > target_dte:
            prior = (timestamp, snapshot)
            continue
        crossed_target = True
        if prior is not None and timestamp - prior[0] <= crossing_gap:
            entry = snapshot
        break
    if entry is None:
        state = "missed_entry" if crossed_target else "awaiting_entry_window"
        return {"action_id": action_id, "delivery_time_epoch_ms": delivery_time, "target_dte_days": target_dte, "state": state}
    pairs = _option_pairs(entry, delivery_time)
    entry_index_values = [
        _number(row.get("indexPrice"), field="option.indexPrice", positive=True)
        for sides in pairs.values() for row in sides.values()
    ]
    entry_index = statistics.median(entry_index_values)
    strike, sides = min(pairs.items(), key=lambda item: (abs(item[0] / entry_index - 1.0), item[0]))
    quantity = _number(action["quantity_btc_per_leg"], field="action.quantity", positive=True)
    required_side = "ask1Size" if action["position_side"] == "long" else "bid1Size"
    minimum_size = float(policy["entry_contract"]["minimum_ask_size_btc"] if action["position_side"] == "long" else policy["entry_contract"]["minimum_bid_size_btc"])
    if any(_number(row.get(required_side), field=f"option.{required_side}", nonnegative=True) < max(quantity, minimum_size) for row in sides.values()):
        return {"action_id": action_id, "delivery_time_epoch_ms": delivery_time, "target_dte_days": target_dte, "state": "insufficient_entry_size"}
    for row in sides.values():
        minimum_quantity = _number(row.get("minOrderQty"), field="option.minOrderQty", positive=True)
        quantity_step = _number(row.get("qtyStep"), field="option.qtyStep", positive=True)
        if quantity + 1e-12 < minimum_quantity or abs(quantity / quantity_step - round(quantity / quantity_step)) > 1e-9:
            raise ValueError("action quantity does not match option lot contract")
    symbols = [str(sides[side]["symbol"]) for side in ("call", "put")]
    delivery_price = _delivery_price(delivery_evidence, symbols, delivery_time, policy["capture_contract"]["settle_coin"])
    if delivery_price is None:
        return {
            "action_id": action_id, "delivery_time_epoch_ms": delivery_time, "target_dte_days": target_dte,
            "entry_timestamp_epoch_ms": int(entry["timestamp_epoch_ms"]), "state": "pending_delivery",
        }
    before_delivery = [snapshot for snapshot in snapshots if int(entry["timestamp_epoch_ms"]) <= int(snapshot["timestamp_epoch_ms"]) <= delivery_time]
    if not before_delivery:
        return {"action_id": action_id, "delivery_time_epoch_ms": delivery_time, "state": "pending_final_hedge_quote"}
    final_snapshot = before_delivery[-1]
    maximum_gap = int(policy["hedge_contract"]["maximum_snapshot_gap_seconds"]) * 1000
    if delivery_time - int(final_snapshot["timestamp_epoch_ms"]) > maximum_gap:
        return {"action_id": action_id, "delivery_time_epoch_ms": delivery_time, "state": "pending_final_hedge_quote"}
    position_sign = 1 if action["position_side"] == "long" else -1
    costs = policy["cost_contract"]
    option_legs = []
    for side in ("call", "put"):
        row = sides[side]
        option_legs.append(option_leg_economics(
            position_sign=position_sign, quantity=quantity,
            bid=_number(row.get("bid1Price"), field="option.bid", positive=True),
            ask=_number(row.get("ask1Price"), field="option.ask", positive=True),
            index_price=entry_index, strike=strike, delivery_price=delivery_price, option_type=side,
            tick_size=_number(row.get("tickSize"), field="option.tickSize", positive=True),
            option_fee_rate=float(costs["option_taker_fee_rate"]), fee_cap_fraction=float(costs["option_fee_cap_fraction"]),
            delivery_fee_rate=float(costs["delivery_fee_rate"]),
            delivery_fee_cap_fraction=float(costs["delivery_fee_cap_fraction_of_intrinsic"]),
            stress_slippage_ticks=float(costs["stress_option_slippage_ticks"]),
        ))
    hedge_targets: list[Dict[str, float]] = []
    interval_ms = int(policy["hedge_contract"]["rebalance_interval_seconds"]) * 1000
    last_hedge = 0
    for snapshot in before_delivery:
        timestamp = int(snapshot["timestamp_epoch_ms"])
        if last_hedge and timestamp - last_hedge < interval_ms:
            continue
        current_pairs = _option_pairs(snapshot, delivery_time)
        current = current_pairs.get(strike)
        if current is None or any(str(current[side].get("symbol")) != str(sides[side].get("symbol")) for side in ("call", "put")):
            continue
        dte = (delivery_time - timestamp) / 86400000.0
        if dte < float(policy["hedge_contract"]["last_option_delta_dte_days"]):
            continue
        delta = sum(_number(current[side].get("delta"), field=f"option.{side}.delta") for side in ("call", "put"))
        quote = _hedge_quote(snapshot, policy)
        quote["target"] = -position_sign * quantity * delta
        hedge_targets.append(quote)
        last_hedge = timestamp
    if not hedge_targets:
        return {"action_id": action_id, "delivery_time_epoch_ms": delivery_time, "state": "pending_hedge_timeline"}
    final_quote = _hedge_quote(final_snapshot, policy)
    hedge = hedge_ledger(
        targets=hedge_targets, final_quote=final_quote,
        fee_rate=float(costs["linear_taker_fee_rate"]),
        quantity_step=float(policy["hedge_contract"]["quantity_step_btc"]),
        minimum_trade_quantity=float(policy["hedge_contract"]["minimum_trade_quantity_btc"]),
        stress_slippage_bps=float(costs["stress_hedge_slippage_bps"]),
    )
    gross = sum(row["gross_pnl"] for row in option_legs) + float(hedge["gross_pnl"])
    option_spread = sum(row["spread_cost"] for row in option_legs)
    option_fee = sum(row["option_fee"] for row in option_legs)
    delivery_fee = sum(row["delivery_fee"] for row in option_legs)
    hedge_spread = float(hedge["spread_cost"])
    hedge_fee = float(hedge["hedge_fee"])
    stress_increment = sum(row["stress_increment"] for row in option_legs) + float(hedge["stress_increment"])
    base_net = gross - option_spread - option_fee - delivery_fee - hedge_spread - hedge_fee
    stress_net = base_net - stress_increment
    capital = entry_index * quantity
    if capital <= 0.0 or any(not math.isfinite(value) for value in (
        gross, option_spread, option_fee, delivery_fee, hedge_spread, hedge_fee, stress_increment, base_net, stress_net
    )):
        raise ValueError("episode economics are invalid")
    return {
        "action_id": action_id, "delivery_time_epoch_ms": delivery_time, "target_dte_days": target_dte,
        "entry_timestamp_epoch_ms": int(entry["timestamp_epoch_ms"]), "entry_index_price": entry_index,
        "strike": strike, "symbols": symbols, "delivery_price": delivery_price, "quantity_btc_per_leg": quantity,
        "state": "complete", "gross_pnl_usdt": gross, "option_spread_cost_usdt": option_spread,
        "option_fee_usdt": option_fee, "delivery_fee_usdt": delivery_fee,
        "hedge_gross_pnl_usdt": hedge["gross_pnl"], "hedge_spread_cost_usdt": hedge_spread,
        "hedge_fee_usdt": hedge_fee, "stress_increment_usdt": stress_increment,
        "base_net_pnl_usdt": base_net, "stress_net_pnl_usdt": stress_net,
        "capital_normalizer_usdt": capital, "gross_bps": gross / capital * 10000.0,
        "base_net_bps": base_net / capital * 10000.0, "stress_net_bps": stress_net / capital * 10000.0,
        "identity_residual_usdt": base_net - (gross - option_spread - option_fee - delivery_fee - hedge_spread - hedge_fee),
        "hedge_ledger": hedge["ledger"], "residual_hedge_quantity_btc": hedge["residual_quantity_btc"],
    }


def bootstrap_mean_bounds(
    values: Sequence[float], *, confidence_level: float, iterations: int, seed: int
) -> tuple[float | None, float | None]:
    cleaned = [float(value) for value in values if math.isfinite(float(value))]
    if not cleaned:
        return None, None
    if len(cleaned) == 1:
        return cleaned[0], cleaned[0]
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choice(cleaned) for _ in cleaned) for _ in range(iterations))
    alpha = 1.0 - confidence_level
    lower_index = max(0, min(len(means) - 1, int(math.floor(alpha * len(means)))))
    upper_index = max(0, min(len(means) - 1, int(math.ceil(confidence_level * len(means))) - 1))
    return means[lower_index], means[upper_index]


def summarize_episodes(episodes: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]) -> Dict[str, Any]:
    complete = [row for row in episodes if row.get("state") == "complete" and row.get("action_id") != "no_trade"]
    statistics_contract = policy["statistics_contract"]
    stress = [float(row["stress_net_bps"]) for row in complete]
    base = [float(row["base_net_bps"]) for row in complete]
    gross = [float(row["gross_bps"]) for row in complete]
    lcb, ucb = bootstrap_mean_bounds(
        stress, confidence_level=float(statistics_contract["confidence_level"]),
        iterations=int(statistics_contract["bootstrap_iterations"]), seed=int(statistics_contract["bootstrap_seed"]),
    )
    return {
        "completed_expiry_count": len(complete), "pending_or_missed_count": len(episodes) - len(complete),
        "gross_mean_bps": statistics.mean(gross) if gross else None,
        "base_mean_bps": statistics.mean(base) if base else None,
        "stress_mean_bps": statistics.mean(stress) if stress else None,
        "stress_median_bps": statistics.median(stress) if stress else None,
        "stress_lcb_bps": lcb, "stress_ucb_bps": ucb,
        "positive_expiry_ratio": sum(value > 0.0 for value in stress) / len(stress) if stress else None,
        "worst_expiry_bps": min(stress) if stress else None,
        "tail_mean_bps": statistics.mean(sorted(stress)[:max(1, math.ceil(len(stress) * 0.2))]) if stress else None,
    }


def sequential_decision(
    *, now_epoch_ms: int, manifest: Mapping[str, Any], capture_summary: Mapping[str, Any],
    primary_summary: Mapping[str, Any], boundary_summaries: Mapping[str, Mapping[str, Any]],
    policy: Mapping[str, Any], episode_invalid_count: int = 0,
) -> Dict[str, Any]:
    decisions = policy["decision_contract"]
    if int(capture_summary.get("invalid_segment_count") or 0) > 0 or episode_invalid_count > 0:
        return {"decision": decisions["invalid_decision"], "reason_code": "DATA_INVALID", "review_day": None}
    start = int(manifest["observation_start_epoch_ms"])
    reached: Mapping[str, Any] | None = None
    for review in policy["sequential_reviews"]:
        if now_epoch_ms < start + int(review["day"]) * 86400000:
            continue
        if (
            float(capture_summary.get("checksum_bound_seconds") or 0.0) >= float(review["minimum_checksum_bound_seconds"])
            and int(capture_summary.get("successful_poll_count") or 0) >= int(review["minimum_successful_polls"])
            and int(primary_summary.get("completed_expiry_count") or 0) >= int(review["minimum_completed_expiries"])
        ):
            reached = review
    if reached is None:
        return {"decision": decisions["pending_decision"], "reason_code": "INCONCLUSIVE", "review_day": None}
    review_day = int(reached["day"])
    optimistic = primary_summary.get("stress_ucb_bps")
    if optimistic is not None and float(optimistic) <= 0.0:
        return {"decision": decisions["futility_decision"], "reason_code": "GROSS_EDGE_ABSENT", "review_day": review_day}
    if review_day < 35:
        return {"decision": decisions["continue_decision"], "reason_code": "INCONCLUSIVE", "review_day": review_day}
    gross_mean = primary_summary.get("gross_mean_bps")
    if gross_mean is None or float(gross_mean) <= 0.0:
        return {"decision": decisions["futility_decision"], "reason_code": "GROSS_EDGE_ABSENT", "review_day": review_day}
    required_lcb = float(policy["statistics_contract"]["minimum_stress_lcb_bps"])
    if primary_summary.get("stress_lcb_bps") is None or float(primary_summary["stress_lcb_bps"]) <= required_lcb:
        return {"decision": decisions["execution_stop_decision"], "reason_code": "EXECUTION_COST_DOMINATES", "review_day": review_day}
    positive = float(primary_summary.get("positive_expiry_ratio") or 0.0)
    worst = float(primary_summary.get("worst_expiry_bps") or -math.inf)
    statistics_contract = policy["statistics_contract"]
    if positive < float(statistics_contract["minimum_positive_expiry_ratio"]) or worst < float(statistics_contract["minimum_worst_expiry_bps"]):
        return {"decision": decisions["tail_stop_decision"], "reason_code": "TAIL_UNSTABLE", "review_day": review_day}
    minimum_boundary = float(statistics_contract["minimum_boundary_stress_lcb_bps"])
    if not boundary_summaries or any(
        summary.get("stress_lcb_bps") is None or float(summary["stress_lcb_bps"]) <= minimum_boundary
        for summary in boundary_summaries.values()
    ):
        return {"decision": decisions["tail_stop_decision"], "reason_code": "TAIL_UNSTABLE", "review_day": review_day}
    return {"decision": decisions["final_pass_decision"], "reason_code": "PASS", "review_day": review_day}


def build_audit(
    *, policy: Mapping[str, Any], manifest: Mapping[str, Any], replay: Mapping[str, Any], now_epoch_ms: int,
    policy_path: pathlib.Path, manifest_path: pathlib.Path,
) -> Dict[str, Any]:
    snapshots = replay["snapshots"]
    delivery_times = sorted({
        int(row.get("deliveryTime") or 0)
        for snapshot in snapshots for row in snapshot.get("scoped_options", [])
        if int(row.get("deliveryTime") or 0) > 0
    })
    actions = {str(row["action_id"]): row for row in policy["actions"]}
    episodes: Dict[str, list[Dict[str, Any]]] = {}
    episode_errors: list[Dict[str, Any]] = []

    def audited_episode(
        action: Mapping[str, Any], delivery_time: int, target_dte_days: float | None = None
    ) -> Dict[str, Any]:
        try:
            return build_episode(
                snapshots=snapshots, delivery_evidence=replay["delivery_evidence"], policy=policy,
                action=action, delivery_time=delivery_time, target_dte_days=target_dte_days,
            )
        except ValueError as exc:
            error = {
                "action_id": str(action["action_id"]), "delivery_time_epoch_ms": delivery_time,
                "target_dte_days": target_dte_days, "state": "data_invalid", "reason": str(exc),
            }
            episode_errors.append(error)
            return error

    for action_id, action in actions.items():
        if action_id == "no_trade":
            episodes[action_id] = [audited_episode(
                action, delivery_time
            ) for delivery_time in delivery_times if delivery_time <= now_epoch_ms]
            continue
        episodes[action_id] = [audited_episode(
            action, delivery_time
        ) for delivery_time in delivery_times]
    primary_id = str(policy["statistics_contract"]["primary_action_id"])
    primary_summary = summarize_episodes(episodes[primary_id], policy)
    boundary_episodes: Dict[str, list[Dict[str, Any]]] = {}
    boundary_summaries: Dict[str, Dict[str, Any]] = {}
    for dte in policy["entry_contract"]["boundary_entry_dte_days"]:
        key = f"entry_dte_{float(dte):g}"
        boundary_episodes[key] = [audited_episode(
            actions[primary_id], delivery_time, float(dte)
        ) for delivery_time in delivery_times]
        boundary_summaries[key] = summarize_episodes(boundary_episodes[key], policy)
    decision = sequential_decision(
        now_epoch_ms=now_epoch_ms, manifest=manifest, capture_summary=replay,
        primary_summary=primary_summary, boundary_summaries=boundary_summaries, policy=policy,
        episode_invalid_count=len(episode_errors),
    )
    input_manifest = {
        "policy_canonical_sha256": capture.canonical_sha256(policy),
        "manifest_canonical_sha256": capture.canonical_sha256(manifest),
        "ordered_capture_inputs": replay["ordered_inputs"],
    }
    return {
        "schema_version": SCHEMA_VERSION, "status": "COMPLETE", "generated_epoch_ms": int(time.time() * 1000),
        "evaluated_now_epoch_ms": now_epoch_ms, "research_domain": "forward_development_only",
        "promotion_evidence": False, "promotion_eligible": False, "promotion_authority": False,
        "demo_activation_authorized": False, "live_activation_authorized": False,
        "policy": {"path": str(policy_path), "canonical_sha256": capture.canonical_sha256(policy), "identity_verified": True},
        "observation_manifest": {"path": str(manifest_path), **dict(manifest), "canonical_sha256": capture.canonical_sha256(manifest), "identity_verified": True},
        "input_manifest": input_manifest, "input_manifest_canonical_sha256": capture.canonical_sha256(input_manifest),
        "capture_replay": {key: value for key, value in replay.items() if key not in {"snapshots", "delivery_evidence"}},
        "delivery_evidence_count": len(replay["delivery_evidence"]), "episodes": episodes,
        "boundary_episodes": boundary_episodes, "episode_invalid_count": len(episode_errors),
        "episode_errors": episode_errors, "primary_summary": primary_summary,
        "boundary_summaries": boundary_summaries, **decision,
        "next_action": "collect_checksum_bound_v2_evidence" if decision["decision"] in {
            policy["decision_contract"]["pending_decision"], policy["decision_contract"]["continue_decision"]
        } else "close_option_vrp_family" if decision["decision"] != policy["decision_contract"]["final_pass_decision"] else "freeze_new_independent_model_comparison_plan",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--now-epoch-ms", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy_path, manifest_path = pathlib.Path(args.config), pathlib.Path(args.manifest)
    policy, manifest = load_frozen_contract(policy_path, manifest_path)
    replay = replay_capture_root(pathlib.Path(args.capture_root), policy=policy, manifest=manifest)
    report = build_audit(
        policy=policy, manifest=manifest, replay=replay, now_epoch_ms=args.now_epoch_ms or int(time.time() * 1000),
        policy_path=policy_path, manifest_path=manifest_path,
    )
    capture.atomic_write_json(pathlib.Path(args.output), report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
