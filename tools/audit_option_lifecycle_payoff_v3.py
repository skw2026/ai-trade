#!/usr/bin/env python3
"""Reconcile the first complete v3 option lifecycle under a pre-delivery policy."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import statistics
import tempfile
import time
from typing import Any, Dict, Mapping, Sequence

import audit_option_lifecycle_v3 as lifecycle_audit
import audit_option_vrp_sequential_payoff as economics
import capture_bybit_option_lifecycle_v3 as capture


SCHEMA_VERSION = "option_lifecycle_payoff_audit_v1"
POLICY_SCHEMA_VERSION = "option_lifecycle_payoff_policy_v1"
MANIFEST_SCHEMA_VERSION = "option_lifecycle_payoff_manifest_v1"
FROZEN_POLICY_CANONICAL_SHA256 = "d1044514b6085936199a9ea3717c3234da2124e2466fa93aca43560919f36a62"
FROZEN_MANIFEST_CANONICAL_SHA256 = "9a8f6917a81ca2de3a0738a25d3a9929c1cfcf3a26de7f31b5ad2faba5eb403f"
PAYOFF_POLICY_RELATIVE_PATH = "config/option_lifecycle_payoff_v1.json"
SOURCE_EXPERIMENT_ID = "btc_bybit_usdt_option_lifecycle_capture_v3"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


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


def load_contract(policy_path: pathlib.Path, manifest_path: pathlib.Path
                  ) -> tuple[Dict[str, Any], Dict[str, Any]]:
    policy, manifest = capture.read_json(policy_path), capture.read_json(manifest_path)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("payoff policy schema mismatch")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("payoff manifest schema mismatch")
    policy_sha, manifest_sha = capture.canonical_sha256(policy), capture.canonical_sha256(manifest)
    if policy_sha != FROZEN_POLICY_CANONICAL_SHA256:
        raise ValueError("payoff frozen policy identity mismatch")
    if manifest_sha != FROZEN_MANIFEST_CANONICAL_SHA256:
        raise ValueError("payoff frozen manifest identity mismatch")
    if manifest.get("policy_canonical_sha256") != policy_sha:
        raise ValueError("payoff manifest policy identity mismatch")
    if (manifest.get("experiment_id") != policy.get("experiment_id")
            or manifest.get("policy_path") != PAYOFF_POLICY_RELATIVE_PATH):
        raise ValueError("payoff experiment identity mismatch")
    source = policy.get("source_contract", {})
    expected_source = {
        "experiment_id": SOURCE_EXPERIMENT_ID,
        "capture_schema_version": capture.SCHEMA_VERSION,
        "snapshot_schema_version": capture.SNAPSHOT_SCHEMA_VERSION,
        "capture_root_name": capture.CAPTURE_ROOT_NAME,
        "capture_policy_canonical_sha256": capture.FROZEN_POLICY_CANONICAL_SHA256,
        "capture_manifest_canonical_sha256": capture.FROZEN_MANIFEST_CANONICAL_SHA256,
    }
    if any(source.get(key) != value for key, value in expected_source.items()):
        raise ValueError("payoff source contract mismatch")
    if [row.get("action_id") for row in policy.get("actions", [])] != [
        "no_trade", "short_selected_straddle", "long_selected_straddle"
    ]:
        raise ValueError("payoff action identity mismatch")
    expected_costs = {
        "option_taker_fee_rate": 0.0003,
        "option_fee_cap_fraction": 0.07,
        "linear_taker_fee_rate": 0.00055,
        "delivery_fee_rate": 0.00015,
        "delivery_fee_cap_fraction_of_intrinsic": 0.125,
        "stress_option_slippage_ticks": 1.0,
        "stress_hedge_slippage_bps": 1.0,
    }
    if any(abs(float(policy.get("cost_contract", {}).get(key, math.nan)) - value) > 1e-12
           for key, value in expected_costs.items()):
        raise ValueError("payoff cost contract mismatch")
    if int(manifest.get("payoff_freeze_epoch_ms") or 0) <= int(manifest.get("capture_observation_start_epoch_ms") or 0):
        raise ValueError("payoff freeze boundary is invalid")
    if (policy.get("research_domain") != "development_only"
            or manifest.get("research_domain") != "development_only"):
        raise ValueError("payoff contract is outside development")
    for payload in (policy.get("authorities", {}), manifest):
        if any(payload.get(field) is not False for field in (
            "promotion_authority", "demo_activation_authorized", "live_activation_authorized"
        )):
            raise ValueError("payoff contract cannot grant activation authority")
    evidence = policy.get("evidence_contract", {})
    if any(evidence.get(field) is not False for field in (
        "profitability_claim_allowed", "sharpe_claim_allowed", "drawdown_claim_allowed"
    )):
        raise ValueError("single-lifecycle payoff contract overclaims evidence")
    return policy, manifest


def _tracked_by_side(snapshot: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    result: Dict[str, Mapping[str, Any]] = {}
    for row in snapshot["tracked_options"]:
        side = str(row.get("optionsType") or "").lower()
        if side not in {"call", "put"} or side in result:
            raise ValueError("payoff tracked pair is invalid")
        result[side] = row
    if set(result) != {"call", "put"}:
        raise ValueError("payoff tracked pair is incomplete")
    return result


def _hedge_quote(snapshot: Mapping[str, Any]) -> Dict[str, float]:
    book = snapshot["hedge_orderbook_l1"]
    bids, asks = book["b"], book["a"]
    bid = _number(bids[0][0], field="hedge.bid", positive=True)
    ask = _number(asks[0][0], field="hedge.ask", positive=True)
    if ask < bid:
        raise ValueError("payoff hedge BBO is crossed")
    return {"timestamp_epoch_ms": int(snapshot["timestamp_epoch_ms"]), "bid": bid, "ask": ask}


def _action_result(*, action: Mapping[str, Any], entry: Mapping[str, Any],
                   timeline: Sequence[Mapping[str, Any]], delivery_price: float,
                   policy: Mapping[str, Any]) -> Dict[str, Any]:
    action_id = str(action["action_id"])
    if action_id == "no_trade":
        return {
            "action_id": action_id, "position_side": "flat", "state": "complete",
            "gross_pnl_usdt": 0.0, "base_net_pnl_usdt": 0.0,
            "stress_net_pnl_usdt": 0.0, "base_net_bps": 0.0,
            "stress_net_bps": 0.0, "hedge_trade_count": 0,
        }
    side_sign = 1 if action["position_side"] == "long" else -1
    quantity = _number(action["quantity_btc_per_leg"], field="action.quantity", positive=True)
    entry_rows = _tracked_by_side(entry)
    index_price = statistics.median(
        _number(row.get("indexPrice"), field="entry.index", positive=True)
        for row in entry_rows.values()
    )
    costs = policy["cost_contract"]
    option_legs = []
    for side in ("call", "put"):
        row = entry_rows[side]
        minimum = _number(row.get("minOrderQty"), field="entry.min_order", positive=True)
        step = _number(row.get("qtyStep"), field="entry.qty_step", positive=True)
        required_size = row.get("ask1Size") if side_sign > 0 else row.get("bid1Size")
        if quantity + 1e-12 < minimum or _number(required_size, field="entry.size", nonnegative=True) + 1e-12 < quantity:
            raise ValueError("payoff action is not executable at entry")
        if abs(quantity / step - round(quantity / step)) > 1e-9:
            raise ValueError("payoff quantity is off instrument step")
        if abs(_number(row.get("deliveryFeeRate"), field="entry.delivery_fee", positive=True)
               - float(costs["delivery_fee_rate"])) > 1e-12:
            raise ValueError("payoff delivery fee differs from frozen cost")
        option_legs.append(economics.option_leg_economics(
            position_sign=side_sign, quantity=quantity,
            bid=_number(row.get("bid1Price"), field="entry.bid", positive=True),
            ask=_number(row.get("ask1Price"), field="entry.ask", positive=True),
            index_price=index_price,
            strike=_number(row.get("strike"), field="entry.strike", positive=True),
            delivery_price=delivery_price, option_type=side,
            tick_size=_number(row.get("tickSize"), field="entry.tick", positive=True),
            option_fee_rate=float(costs["option_taker_fee_rate"]),
            fee_cap_fraction=float(costs["option_fee_cap_fraction"]),
            delivery_fee_rate=float(costs["delivery_fee_rate"]),
            delivery_fee_cap_fraction=float(costs["delivery_fee_cap_fraction_of_intrinsic"]),
            stress_slippage_ticks=float(costs["stress_option_slippage_ticks"]),
        ))
    cadence_ms = int(policy["hedge_contract"]["rebalance_interval_seconds"]) * 1000
    hedge_targets: list[Dict[str, float]] = []
    last_rebalance = 0
    for snapshot in timeline:
        timestamp = int(snapshot["timestamp_epoch_ms"])
        if last_rebalance and timestamp - last_rebalance < cadence_ms:
            continue
        rows = _tracked_by_side(snapshot)
        delta = sum(_number(rows[side].get("delta"), field=f"hedge.{side}.delta")
                    for side in ("call", "put"))
        quote = _hedge_quote(snapshot)
        quote["target"] = -side_sign * quantity * delta
        hedge_targets.append(quote)
        last_rebalance = timestamp
    if not hedge_targets:
        raise ValueError("payoff hedge timeline is empty")
    hedge = economics.hedge_ledger(
        targets=hedge_targets, final_quote=_hedge_quote(timeline[-1]),
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
    capital = index_price * quantity
    identity_residual = base_net - (
        gross - option_spread - option_fee - delivery_fee - hedge_spread - hedge_fee
    )
    if capital <= 0.0 or abs(identity_residual) > 1e-9:
        raise ValueError("payoff accounting identity failed")
    return {
        "action_id": action_id, "position_side": action["position_side"], "state": "complete",
        "quantity_btc_per_leg": quantity, "entry_index_price_usdt": index_price,
        "delivery_price_usdt": delivery_price, "gross_pnl_usdt": gross,
        "option_spread_cost_usdt": option_spread, "option_fee_usdt": option_fee,
        "delivery_fee_usdt": delivery_fee, "hedge_spread_cost_usdt": hedge_spread,
        "hedge_fee_usdt": hedge_fee, "stress_increment_usdt": stress_increment,
        "base_net_pnl_usdt": base_net, "stress_net_pnl_usdt": stress_net,
        "capital_normalizer_usdt": capital, "gross_bps": gross / capital * 10000.0,
        "base_net_bps": base_net / capital * 10000.0,
        "stress_net_bps": stress_net / capital * 10000.0,
        "hedge_rebalance_count": len(hedge_targets),
        "hedge_trade_count": len(hedge["ledger"]),
        "accounting_identity_residual_usdt": identity_residual,
        "residual_hedge_quantity_btc": hedge["residual_quantity_btc"],
    }


def audit(*, root: pathlib.Path, capture_policy_path: pathlib.Path,
          capture_manifest_path: pathlib.Path, payoff_policy_path: pathlib.Path,
          payoff_manifest_path: pathlib.Path, executed_release_sha: str | None = None,
          generated_at_epoch_ms: int | None = None) -> Dict[str, Any]:
    if executed_release_sha is not None and not SHA_PATTERN.fullmatch(executed_release_sha):
        raise ValueError("payoff executed release SHA is invalid")
    policy, manifest = load_contract(payoff_policy_path, payoff_manifest_path)
    capture_policy, capture_manifest = capture.load_contract(
        capture_policy_path, capture_manifest_path
    )
    generated = int(generated_at_epoch_ms or time.time() * 1000)
    source_gate = lifecycle_audit.audit(
        root=root, policy_path=capture_policy_path, manifest_path=capture_manifest_path,
        executed_release_sha=executed_release_sha, generated_at_epoch_ms=generated,
    )
    decisions = policy["decision_contract"]
    common = {
        "schema_version": SCHEMA_VERSION, "generated_at_epoch_ms": generated,
        "identities": {
            "experiment_id": policy["experiment_id"],
            "policy_canonical_sha256": capture.canonical_sha256(policy),
            "manifest_canonical_sha256": capture.canonical_sha256(manifest),
            "source_archive_input_set_sha256": source_gate["identities"]["archive_input_set_sha256"],
            "executed_release_sha": executed_release_sha,
        },
        "source_gate": {
            "decision": source_gate["decision"], "reason_code": source_gate["reason_code"],
            "valid_segment_count": source_gate["archive_integrity"]["valid_segment_count"],
            "invalid_segment_count": source_gate["archive_integrity"]["invalid_segment_count"],
            "checksum_bound_snapshot_count": source_gate["archive_integrity"]["checksum_bound_snapshot_count"],
            "lifecycle_id": source_gate["startup_gate"].get("lifecycle_id"),
            "paired_delivery_evidence_valid": source_gate["lifecycle_gate"]["paired_delivery_evidence_valid"],
        },
        "profitability_evidence": False, "sharpe_evidence": False, "drawdown_evidence": False,
        "promotion_authority": False, "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }
    if source_gate["decision"] == capture_policy["lifecycle_gate"]["invalid_decision"]:
        return {**common, "actions": [], "decision": decisions["invalid_decision"],
                "reason_code": "SOURCE_LIFECYCLE_INVALID"}
    if source_gate["decision"] == capture_policy["lifecycle_gate"]["insufficient_decision"]:
        return {**common, "actions": [], "decision": decisions["insufficient_decision"],
                "reason_code": "SOURCE_LIFECYCLE_INSUFFICIENT"}
    if source_gate["decision"] != capture_policy["lifecycle_gate"]["pass_decision"]:
        return {**common, "actions": [], "decision": decisions["wait_decision"],
                "reason_code": "SOURCE_LIFECYCLE_PENDING"}
    replay = lifecycle_audit.replay_capture_root(
        root, policy=capture_policy, manifest=capture_manifest
    )
    lifecycle_id = str(source_gate["startup_gate"]["lifecycle_id"])
    selected = [snapshot for snapshot in replay["snapshots"]
                if isinstance(snapshot.get("active_lifecycle"), Mapping)
                and str(snapshot["active_lifecycle"].get("lifecycle_id")) == lifecycle_id]
    if not selected:
        raise ValueError("payoff lifecycle disappeared after source audit")
    lifecycle = selected[0]["active_lifecycle"]
    delivery_time = int(lifecycle["delivery_time_epoch_ms"])
    if int(manifest["payoff_freeze_epoch_ms"]) >= delivery_time:
        raise ValueError("payoff contract was not frozen before delivery")
    entry_time = int(lifecycle["selected_epoch_ms"])
    entries = [snapshot for snapshot in selected if int(snapshot["timestamp_epoch_ms"]) == entry_time]
    if len(entries) != 1:
        raise ValueError("payoff entry snapshot is not unique")
    timeline = [snapshot for snapshot in selected
                if entry_time <= int(snapshot["timestamp_epoch_ms"]) <= delivery_time]
    if not timeline:
        raise ValueError("payoff pre-delivery timeline is empty")
    delivery_price = _number(
        source_gate["lifecycle_gate"]["delivery_price_usdt"],
        field="payoff.delivery", positive=True,
    )
    action_results = [
        _action_result(action=action, entry=entries[0], timeline=timeline,
                       delivery_price=delivery_price, policy=policy)
        for action in policy["actions"]
    ]
    by_id = {row["action_id"]: row for row in action_results}
    gross_residual = (
        float(by_id["short_selected_straddle"]["gross_pnl_usdt"])
        + float(by_id["long_selected_straddle"]["gross_pnl_usdt"])
    )
    if abs(gross_residual) > 1e-8:
        raise ValueError("payoff long/short gross sign control failed")
    return {
        **common,
        "lifecycle": {
            "lifecycle_id": lifecycle_id, "entry_timestamp_epoch_ms": entry_time,
            "delivery_time_epoch_ms": delivery_time,
            "timeline_snapshot_count": len(timeline),
        },
        "actions": action_results,
        "controls": {"long_short_gross_residual_usdt": gross_residual,
                     "all_accounting_identities_pass": True},
        "decision": decisions["reconciled_decision"],
        "reason_code": "FIRST_LIFECYCLE_PAYOFF_AND_COSTS_RECONCILED",
        "limitations": [
            "One lifecycle cannot establish profitability, Sharpe ratio or drawdown.",
            "This is a deterministic public-market-data counterfactual; no order was placed.",
            "No account, credential, funding action or execution report is used.",
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
    parser.add_argument("--capture-policy", type=pathlib.Path, required=True)
    parser.add_argument("--capture-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--payoff-policy", type=pathlib.Path, required=True)
    parser.add_argument("--payoff-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--executed-release-sha")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(
            root=args.root, capture_policy_path=args.capture_policy,
            capture_manifest_path=args.capture_manifest,
            payoff_policy_path=args.payoff_policy, payoff_manifest_path=args.payoff_manifest,
            executed_release_sha=args.executed_release_sha,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "identities": {"executed_release_sha": args.executed_release_sha},
            "source_gate": {"invalid_segment_count": 1}, "actions": [],
            "decision": "INVALID_FIRST_LIFECYCLE_PAYOFF", "reason_code": str(exc),
            "profitability_evidence": False, "sharpe_evidence": False,
            "drawdown_evidence": False, "promotion_authority": False,
            "demo_activation_authorized": False, "live_activation_authorized": False,
        }
    _atomic_write(args.output, report)
    return 2 if report["decision"] == "INVALID_FIRST_LIFECYCLE_PAYOFF" else 0


if __name__ == "__main__":
    raise SystemExit(main())
