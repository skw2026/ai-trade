#!/usr/bin/env python3
"""Adapt the pinned first V4 lifecycle into the offline account ledger.

This is a checksum-valid public-market counterfactual adapter, not an account
statement. It reconstructs the frozen option fills and delta-hedge executions,
but deliberately leaves funding cashflows and exchange margin snapshots absent.
The downstream ledger must therefore remain INSUFFICIENT_EVIDENCE.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
import pathlib
import statistics
from typing import Any, Mapping, Sequence

import audit_option_candidate_closure as closure
import audit_option_funding_sensitivity as funding
import audit_option_lifecycle_economics_v1 as lifecycle_economics
import audit_option_lifecycle_payoff_v4 as payoff
import audit_option_lifecycle_v4 as lifecycle_audit
import audit_option_subaccount_ledger as ledger
import audit_option_vrp_sequential_payoff as payoff_math
import capture_bybit_option_lifecycle_v4 as capture


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLOSURE_EVIDENCE = ROOT / "docs/reviews/2026-09-12-option-candidate-closure-result.evidence.json"
CLOSURE_EVIDENCE_SHA256 = "eee37c222b2a1c6d80e301434fa506f6d7b1a67889f4375a9d258ef9a17c0ebf"
FUNDING_EVIDENCE = ROOT / "docs/reviews/2026-09-13-option-funding-sensitivity-result.evidence.json"
FUNDING_EVIDENCE_SHA256 = "489767c56d52052dccbe402ce2ce133e236aca0167735f048f4b205d3f1d2ad4"
REGISTRY = ROOT / "config/option_candidate_closure_v1.json"
CAPTURE_POLICY = ROOT / "config/option_lifecycle_capture_v4.json"
CAPTURE_MANIFEST = ROOT / "config/option_lifecycle_capture_manifest_v4.json"
PAYOFF_POLICY = ROOT / "config/option_lifecycle_payoff_v2.json"
PAYOFF_MANIFEST = ROOT / "config/option_lifecycle_payoff_manifest_v2.json"
PRIMARY_ACTION = "short_selected_straddle"
TARGET_LIFECYCLE = "btc-usdt-1788768000000-79750-a9f8f42224b2"
DECISION = "C2_FIRST_LIFECYCLE_ADAPTED_ACCOUNTING_INCOMPLETE"
AUTHORITIES = {
    "promotion_authority": False,
    "demo_activation_authorized": False,
    "live_activation_authorized": False,
    "order_submission_authorized": False,
}


class EvidenceGap(ValueError):
    """A bounded source is insufficient, rather than uncertainties being filled."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def text_number(value: Any) -> str:
    result = Decimal(str(value))
    require(result.is_finite(), "nonfinite adapter number")
    return format(result, "f")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(ledger.canonical(value)).hexdigest()


def load_pinned_target() -> tuple[dict[str, Any], dict[str, Any]]:
    raw = closure.read(CLOSURE_EVIDENCE)
    require(closure.sha(raw) == CLOSURE_EVIDENCE_SHA256,
            "pinned closure evidence identity mismatch")
    report = closure.decode(raw)
    registry = closure.load_registry(REGISTRY)
    closure.no_authority(report)
    require(report.get("decision") == "CLOSED_CANDIDATE_NO_REOPEN"
            and report.get("closure_latched") is True,
            "candidate closure is not latched")
    require(report.get("candidate_experiment_id") == registry["candidate_experiment_id"],
            "closure candidate identity mismatch")
    rows = [row for row in report["anchor_lifecycles"]
            if row.get("lifecycle_id") == TARGET_LIFECYCLE]
    require(len(rows) == 1, "pinned first lifecycle is missing or duplicated")
    target = rows[0]
    require(target["selected_epoch_ms"] == min(
        row["selected_epoch_ms"] for row in report["anchor_lifecycles"]),
        "target is not the first closed lifecycle")
    require(report["closure_anchor"]["executed_release_sha"] ==
            registry["closure_anchor"]["executed_release_sha"],
            "closure release identity mismatch")
    return target, report


def load_pinned_funding_rates() -> tuple[dict[int, str], str]:
    raw = closure.read(FUNDING_EVIDENCE)
    require(closure.sha(raw) == FUNDING_EVIDENCE_SHA256,
            "pinned funding evidence identity mismatch")
    report = closure.decode(raw)
    require(report.get("schema_version") == "option_funding_sensitivity_v1"
            and report.get("decision") == "DIAGNOSTIC_ONLY_C2_INCOMPLETE"
            and report.get("candidate_state") == "CLOSED",
            "pinned funding evidence contract mismatch")
    require(report.get("cashflows_qualified") is False
            and report.get("settlement_marks_qualified") is False
            and report.get("economic_qualification") is False
            and report.get("new_candidate_registered") is False
            and report.get("forward_wait_started") is False,
            "pinned funding evidence overclaims qualification")
    require(report.get("authorities") == AUTHORITIES,
            "pinned funding evidence authority changed")
    events = report.get("funding_events")
    require(isinstance(events, list) and report.get("event_count") == len(events),
            "pinned funding event population mismatch")
    rates: dict[int, str] = {}
    for row in events:
        require(isinstance(row, dict) and set(row) == {"ts_ms", "settled_rate"},
                "pinned funding event fields changed")
        timestamp = ledger.integer(row["ts_ms"], "funding timestamp")
        rate = row["settled_rate"]
        require(timestamp not in rates and isinstance(rate, str),
                "duplicate or malformed pinned funding event")
        ledger.number(rate, "settled funding rate")
        rates[timestamp] = rate
    require(bool(rates), "pinned funding evidence is empty")
    return rates, FUNDING_EVIDENCE_SHA256


def _availability(snapshot: Mapping[str, Any]) -> int:
    completed = ledger.integer(snapshot["snapshot_completed_epoch_ms"],
                               "snapshot completion")
    book_time = ledger.integer(snapshot["hedge_orderbook_l1"]["ts"],
                               "hedge book time")
    return max(completed, book_time)


def _tracked_by_symbol(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = snapshot["tracked_options"]
    result = {str(row["symbol"]): row for row in rows}
    require(len(result) == 2 and len(rows) == 2, "tracked option pair is invalid")
    return result


def _valuation(snapshot: Mapping[str, Any], positions: Mapping[str, Decimal],
               event_time: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    tracked = _tracked_by_symbol(snapshot)
    completed = ledger.integer(snapshot["snapshot_completed_epoch_ms"],
                               "snapshot completion")
    require(completed <= event_time, "option ticker used before poll completion")
    for symbol, quantity in positions.items():
        if quantity == 0:
            continue
        if symbol == "BTCUSDT":
            book = snapshot["hedge_orderbook_l1"]
            book_time = ledger.integer(book["ts"], "hedge book time")
            require(book_time <= event_time, "hedge book used before exchange timestamp")
            bid, ask = book["b"][0], book["a"][0]
            result[symbol] = {
                "ts_ms": book_time,
                "bid": text_number(bid[0]), "ask": text_number(ask[0]),
                "bid_size": text_number(bid[1]), "ask_size": text_number(ask[1]),
                "mark": text_number((Decimal(str(bid[0])) + Decimal(str(ask[0]))) / 2),
            }
        else:
            row = tracked[symbol]
            result[symbol] = {
                "ts_ms": completed,
                "bid": text_number(row["bid1Price"]),
                "ask": text_number(row["ask1Price"]),
                "bid_size": text_number(row["bid1Size"]),
                "ask_size": text_number(row["ask1Size"]),
                "mark": text_number(row["markPrice"]),
            }
    return result


def _execution_quote(snapshot: Mapping[str, Any], symbol: str,
                     event_time: int) -> dict[str, Any]:
    if symbol == "BTCUSDT":
        book = snapshot["hedge_orderbook_l1"]
        timestamp = ledger.integer(book["ts"], "hedge book time")
        bid, ask = book["b"][0], book["a"][0]
        require(timestamp <= event_time, "hedge execution quote is future data")
        return {"ts_ms": timestamp, "bid": text_number(bid[0]),
                "ask": text_number(ask[0]), "bid_size": text_number(bid[1]),
                "ask_size": text_number(ask[1])}
    row = _tracked_by_symbol(snapshot)[symbol]
    timestamp = ledger.integer(snapshot["snapshot_completed_epoch_ms"],
                               "snapshot completion")
    require(timestamp <= event_time, "option execution quote predates availability")
    return {"ts_ms": timestamp, "bid": text_number(row["bid1Price"]),
            "ask": text_number(row["ask1Price"]),
            "bid_size": text_number(row["bid1Size"]),
            "ask_size": text_number(row["ask1Size"])}


def _primary_trace(entry: Mapping[str, Any], timeline: Sequence[Mapping[str, Any]],
                   delivery_price: float, policy: Mapping[str, Any]
                   ) -> tuple[dict[str, Any], dict[str, dict[str, float]], dict[str, Any]]:
    action = next(row for row in policy["actions"] if row["action_id"] == PRIMARY_ACTION)
    quantity = payoff._number(action["quantity_btc_per_leg"], field="action.quantity",
                              positive=True)
    rows = payoff._tracked_by_side(entry)
    index_price = statistics.median(
        payoff._number(row["indexPrice"], field="entry.index", positive=True)
        for row in rows.values())
    costs = policy["cost_contract"]
    legs: dict[str, dict[str, float]] = {}
    for side in ("call", "put"):
        row = rows[side]
        legs[side] = payoff_math.option_leg_economics(
            position_sign=-1, quantity=quantity,
            bid=payoff._number(row["bid1Price"], field="entry.bid", positive=True),
            ask=payoff._number(row["ask1Price"], field="entry.ask", positive=True),
            index_price=index_price,
            strike=payoff._number(row["strike"], field="entry.strike", positive=True),
            delivery_price=delivery_price, option_type=side,
            tick_size=payoff._number(row["tickSize"], field="entry.tick", positive=True),
            option_fee_rate=float(costs["option_taker_fee_rate"]),
            fee_cap_fraction=float(costs["option_fee_cap_fraction"]),
            delivery_fee_rate=float(costs["delivery_fee_rate"]),
            delivery_fee_cap_fraction=float(costs["delivery_fee_cap_fraction_of_intrinsic"]),
            stress_slippage_ticks=float(costs["stress_option_slippage_ticks"]),
        )
    cadence = int(policy["hedge_contract"]["rebalance_interval_seconds"]) * 1000
    targets: list[dict[str, float]] = []
    last = 0
    for snapshot in timeline:
        timestamp = int(snapshot["timestamp_epoch_ms"])
        if last and timestamp - last < cadence:
            continue
        current = payoff._tracked_by_side(snapshot)
        delta = sum(payoff._number(current[side]["delta"],
                                   field=f"hedge.{side}.delta")
                    for side in ("call", "put"))
        quote = payoff._hedge_quote(snapshot)
        quote["target"] = quantity * delta
        targets.append(quote)
        last = timestamp
    require(bool(targets), "frozen hedge path is empty")
    hedge = payoff_math.hedge_ledger(
        targets=targets, final_quote=payoff._hedge_quote(timeline[-1]),
        fee_rate=float(costs["linear_taker_fee_rate"]),
        quantity_step=float(policy["hedge_contract"]["quantity_step_btc"]),
        minimum_trade_quantity=float(policy["hedge_contract"]["minimum_trade_quantity_btc"]),
        stress_slippage_bps=float(costs["stress_hedge_slippage_bps"]),
    )
    return action, legs, hedge


def _add_event(events: list[dict[str, Any]], *, kind: str, timestamp: int,
               valuation: dict[str, Any], identity: str, **fields: Any) -> None:
    if events:
        require(timestamp >= events[-1]["ts_ms"], "causal event time moved backwards")
    events.append({"seq": len(events), "id": identity,
                   "scope_id": "offline_subaccount", "type": kind,
                   "ts_ms": timestamp, "valuation": valuation, **fields})


def build_ledger_input(*, root: pathlib.Path, target: Mapping[str, Any],
                       rates: Mapping[int, str]
                       ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    capture_policy, capture_manifest = capture.load_contract(CAPTURE_POLICY, CAPTURE_MANIFEST)
    payoff_policy, _ = payoff.load_contract(PAYOFF_POLICY, PAYOFF_MANIFEST)
    replay = lifecycle_audit.replay_capture_root(
        root, policy=capture_policy, manifest=capture_manifest)
    require(replay["root_present"], "V4 capture root is absent")
    require(replay["invalid_segment_count"] == 0, "V4 archive contains invalid segments")
    selected = [snapshot for snapshot in replay["snapshots"]
                if isinstance(snapshot.get("active_lifecycle"), Mapping)
                and snapshot["active_lifecycle"].get("lifecycle_id") == target["lifecycle_id"]]
    require(bool(selected), "pinned lifecycle snapshots are absent")
    selected.sort(key=lambda row: int(row["timestamp_epoch_ms"]))
    lifecycle = selected[0]["active_lifecycle"]
    start = int(lifecycle["selected_epoch_ms"])
    delivery = int(lifecycle["delivery_time_epoch_ms"])
    require(lifecycle["lifecycle_id"] == target["lifecycle_id"]
            and start == target["selected_epoch_ms"]
            and delivery == target["delivery_time_epoch_ms"],
            "raw lifecycle identity differs from pinned closure")
    timeline = [row for row in selected
                if start <= int(row["timestamp_epoch_ms"]) <= delivery]
    require(timeline and int(timeline[0]["timestamp_epoch_ms"]) == start,
            "unique entry snapshot is missing")
    evaluated = lifecycle_economics.evaluate_lifecycle(
        selected, replay=replay, capture_policy=capture_policy,
        payoff_policy=payoff_policy, generated_at_epoch_ms=delivery + 1)
    require(evaluated["state"] == "complete",
            "pinned lifecycle is not complete in raw archive")
    frozen = next(row for row in evaluated["actions"]
                  if row["action_id"] == PRIMARY_ACTION)
    for field in closure.MONEY_FIELDS:
        closure.equal(frozen[field], target["primary_payoff"][field])

    policy_action, legs, hedge = _primary_trace(
        timeline[0], timeline, float(frozen["delivery_price_usdt"]), payoff_policy)
    quantity = Decimal(str(policy_action["quantity_btc_per_leg"]))
    closure.equal(hedge["gross_pnl"], frozen["gross_pnl_usdt"] -
                  sum(row["gross_pnl"] for row in legs.values()))
    closure.equal(sum(row["option_fee"] for row in legs.values()),
                  frozen["option_fee_usdt"])
    closure.equal(sum(row["delivery_fee"] for row in legs.values()),
                  frozen["delivery_fee_usdt"])
    closure.equal(hedge["spread_cost"], frozen["hedge_spread_cost_usdt"])
    closure.equal(hedge["hedge_fee"], frozen["hedge_fee_usdt"])

    rates_in_lifecycle: dict[int, str] = {}
    for timestamp, rate in rates.items():
        require(type(timestamp) is int and isinstance(rate, str),
                "funding source event is malformed")
        if start <= timestamp <= delivery:
            ledger.number(rate, "settled funding rate")
            rates_in_lifecycle[timestamp] = rate
    require(bool(rates_in_lifecycle), "no settled funding boundary in target lifecycle")

    entry_rows = payoff._tracked_by_side(timeline[0])
    instruments: dict[str, dict[str, Any]] = {}
    for side in ("call", "put"):
        row = entry_rows[side]
        instruments[str(row["symbol"])] = {
            "kind": side, "strike": text_number(row["strike"]),
            "expiry_ts_ms": delivery, "quantity_unit": "BTC",
            "settle_coin": "USDT", "qty_step": text_number(row["qtyStep"]),
        }
    instruments["BTCUSDT"] = {
        "kind": "linear_perpetual", "quantity_unit": "BTC",
        "settle_coin": "USDT",
        "qty_step": text_number(payoff_policy["hedge_contract"]["quantity_step_btc"]),
    }
    positions = {symbol: Decimal(0) for symbol in instruments}
    events: list[dict[str, Any]] = []
    _add_event(events, kind="MARK", timestamp=start, valuation={},
               identity=f"{target['lifecycle_id']}:initial-flat")

    entry_time = _availability(timeline[0])
    if entry_time > delivery:
        raise EvidenceGap("ENTRY_OBSERVATION_AVAILABLE_AFTER_DELIVERY")
    for side in ("call", "put"):
        row = entry_rows[side]
        symbol = str(row["symbol"])
        positions[symbol] = -quantity
        _add_event(
            events, kind="FILL", timestamp=entry_time,
            valuation=_valuation(timeline[0], positions, entry_time),
            identity=f"{target['lifecycle_id']}:option-{side}", symbol=symbol,
            signed_qty_btc=text_number(-quantity),
            price_usdt_per_btc=text_number(row["bid1Price"]),
            fee_usdt=text_number(legs[side]["option_fee"]),
            execution_quote=_execution_quote(timeline[0], symbol, entry_time))

    hedge_by_snapshot: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in hedge["ledger"]:
        hedge_by_snapshot[int(row["timestamp_epoch_ms"])].append(row)
    maximum_position = Decimal(0)
    last_usable_snapshot = timeline[0]
    for snapshot_index, snapshot in enumerate(timeline):
        source_time = int(snapshot["timestamp_epoch_ms"])
        event_time = _availability(snapshot)
        trades = hedge_by_snapshot.pop(source_time, [])
        if event_time > delivery:
            if trades:
                raise EvidenceGap("HEDGE_OBSERVATION_AVAILABLE_AFTER_DELIVERY")
            continue
        last_usable_snapshot = snapshot
        for trade_index, trade in enumerate(trades):
            change = Decimal(str(trade["quantity_btc"])) * (
                Decimal(1) if trade["side"] == "buy" else Decimal(-1))
            quote = _execution_quote(snapshot, "BTCUSDT", event_time)
            available = (Decimal(quote["ask_size"]) if change > 0
                         else Decimal(quote["bid_size"]))
            if abs(change) > available:
                raise EvidenceGap("HEDGE_EXECUTION_DEPTH_INSUFFICIENT")
            positions["BTCUSDT"] += change
            ledger.close(positions["BTCUSDT"],
                         Decimal(str(trade["position_after_btc"])),
                         "hedge position path")
            maximum_position = max(maximum_position, abs(positions["BTCUSDT"]))
            _add_event(
                events, kind="FILL", timestamp=event_time,
                valuation=_valuation(snapshot, positions, event_time),
                identity=f"{target['lifecycle_id']}:hedge-{snapshot_index}-{trade_index}",
                symbol="BTCUSDT", signed_qty_btc=text_number(change),
                price_usdt_per_btc=text_number(trade["execution_price"]),
                fee_usdt=text_number(trade["fee"]), execution_quote=quote)
        if snapshot_index and not trades:
            _add_event(events, kind="MARK", timestamp=event_time,
                       valuation=_valuation(snapshot, positions, event_time),
                       identity=f"{target['lifecycle_id']}:mark-{snapshot_index}")
    require(not hedge_by_snapshot, "hedge trace references a missing raw snapshot")
    require(positions["BTCUSDT"] == 0, "hedge is not flat before delivery")
    require(events[-1]["ts_ms"] <= delivery, "last reconstructed event is post-delivery")

    delivery_price = Decimal(str(frozen["delivery_price_usdt"]))
    strike = Decimal(str(lifecycle["strike"]))
    for side in ("call", "put"):
        row = entry_rows[side]
        symbol = str(row["symbol"])
        intrinsic = max(Decimal(0), delivery_price - strike if side == "call"
                        else strike - delivery_price)
        cash_delta = positions[symbol] * intrinsic
        positions[symbol] = Decimal(0)
        _add_event(
            events, kind="DELIVERY", timestamp=delivery,
            valuation=_valuation(last_usable_snapshot, positions, delivery),
            identity=f"{target['lifecycle_id']}:delivery-{side}", symbol=symbol,
            price_kind="official_delivery", delivery_price=text_number(delivery_price),
            cash_delta_before_fee_usdt=text_number(cash_delta),
            fee_usdt=text_number(legs[side]["delivery_fee"]))

    maximum_poll_latency = int(capture_policy["timestamp_contract"][
        "maximum_poll_latency_seconds"])
    maximum_book_skew = int(capture_policy["timestamp_contract"][
        "maximum_absolute_hedge_book_skew_seconds"])
    maximum_snapshot_gap = int(capture_policy["lifecycle_gate"][
        "maximum_snapshot_gap_seconds"])
    capital = text_number(frozen["capital_normalizer_usdt"])
    ledger_input = {
        "schema_version": "option_subaccount_ledger_input_v1",
        "evidence_kind": "local_unverified_research",
        "scope_id": "offline_subaccount", "simulation_capital_usdt": capital,
        "start_ts_ms": start, "end_ts_ms": delivery,
        "illustrative_limits": {
            "im_rate_max": "0.5", "mm_rate_reduce": "0.6",
            "mm_rate_exit": "0.8", "drawdown_exit": "0.2",
            "quote_max_age_ms": (maximum_poll_latency + maximum_book_skew) * 1000,
            "checkpoint_max_gap_ms": (maximum_snapshot_gap + maximum_poll_latency) * 1000,
        },
        "instruments": instruments,
        "funding_schedule": [
            {"settlement_id": f"bybit-btcusdt-{timestamp}",
             "ts_ms": timestamp, "symbol": "BTCUSDT"}
            for timestamp in sorted(rates_in_lifecycle)],
        "events": events,
    }
    ledger_report = ledger.audit(ledger.SCOPE.copy(), ledger_input)
    require(ledger_report["status"] == "INSUFFICIENT_EVIDENCE",
            "adapter must not produce an accounting or risk pass")
    require("MARGIN_EVIDENCE_MISSING" in ledger_report["missing_evidence"]
            and "SCHEDULED_FUNDING_MISSING" in ledger_report["missing_evidence"],
            "known C2 gaps were not preserved by downstream ledger")
    ledger.close(Decimal(ledger_report["pnl_on_simulated_capital_usdt"]),
                 Decimal(str(frozen["base_net_pnl_usdt"])),
                 "frozen base PnL")
    metadata = {
        "archive_input_set_sha256": capture.canonical_sha256(replay["input_identities"]),
        "target_snapshot_set_sha256": capture.canonical_sha256(selected),
        "valid_segment_count": replay["valid_segment_count"],
        "target_snapshot_count": len(selected),
        "timeline_snapshot_count": len(timeline),
        "funding_boundary_count": len(rates_in_lifecycle),
        "settled_funding_boundaries": [
            {"ts_ms": timestamp, "settled_rate": rates_in_lifecycle[timestamp]}
            for timestamp in sorted(rates_in_lifecycle)],
        "hedge_trade_count": len(hedge["ledger"]),
        "maximum_reconstructed_hedge_position_btc": text_number(maximum_position),
        "frozen_primary": frozen,
    }
    return ledger_input, ledger_report, metadata


def adapt(*, root: pathlib.Path, target: Mapping[str, Any],
          rates: Mapping[int, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger_input, ledger_report, metadata = build_ledger_input(
        root=root, target=target, rates=rates)
    known_gaps = sorted(set(ledger_report["missing_evidence"]) | {
        "EXACT_FUNDING_SETTLEMENT_MARKS_MISSING",
        "HISTORICAL_EXCHANGE_MARGIN_MODEL_UNVALIDATED",
        "OPTION_TICKER_EXCHANGE_TIMESTAMP_MISSING",
        "RAW_TARGET_SNAPSHOT_SET_NOT_PREVIOUSLY_PINNED",
    })
    report = {
        "schema_version": "option_lifecycle_subaccount_adapter_v1",
        "decision": DECISION,
        "candidate_state": "CLOSED",
        "target_lifecycle_id": target["lifecycle_id"],
        "selected_epoch_ms": target["selected_epoch_ms"],
        "delivery_time_epoch_ms": target["delivery_time_epoch_ms"],
        "source": metadata,
        "ledger_input_sha256": sha256_json(ledger_input),
        "ledger_event_count": len(ledger_input["events"]),
        "ledger_status": ledger_report["status"],
        "ledger_totals_usdt": ledger_report["totals_usdt"],
        "ledger_final_cash_usdt": ledger_report["final_cash_usdt"],
        "ledger_base_pnl_usdt": ledger_report["pnl_on_simulated_capital_usdt"],
        "known_gaps": known_gaps,
        "limitations": [
            "Public V4 snapshots reconstruct a counterfactual path, not actual fills or account history.",
            "Option ticker fields have poll completion time but no preserved exchange event timestamp.",
            "Funding boundaries are retained without inventing settlement marks or cash deltas.",
            "No margin snapshot or historical exchange margin model is synthesized.",
            "Illustrative ledger limits are branch checks, not approved risk limits.",
        ],
        "cashflows_qualified": False,
        "historical_data_qualified": False,
        "account_risk_qualified": False,
        "economic_qualification": False,
        "actual_account_performance": False,
        "profitability_claim_allowed": False,
        "sharpe_claim_allowed": False,
        "drawdown_claim_allowed": False,
        "new_candidate_registered": False,
        "forward_wait_started": False,
        "authorities": AUTHORITIES.copy(),
    }
    return ledger_input, report


def _invalid_report(reason: str, *, evidence_gap: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "option_lifecycle_subaccount_adapter_v1",
        "decision": ("C2_FIRST_LIFECYCLE_SOURCE_EVIDENCE_GAP" if evidence_gap
                     else "INVALID_OPTION_LIFECYCLE_SUBACCOUNT_ADAPTER"),
        "candidate_state": "CLOSED", "reason": reason,
        "cashflows_qualified": False, "historical_data_qualified": False,
        "account_risk_qualified": False, "economic_qualification": False,
        "actual_account_performance": False, "profitability_claim_allowed": False,
        "sharpe_claim_allowed": False, "drawdown_claim_allowed": False,
        "new_candidate_registered": False, "forward_wait_started": False,
        "authorities": AUTHORITIES.copy(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=pathlib.Path,
                        help="checksum-bound bybit_btc_option_lifecycle_v4 root")
    parser.add_argument("--funding-source", type=pathlib.Path,
                        help="optional local raw-response qualification; the pinned committed evidence is the default")
    parser.add_argument("--ledger-output", required=True, type=pathlib.Path)
    parser.add_argument("--report-output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    outputs = {args.ledger_output.resolve(), args.report_output.resolve()}
    inputs = {args.root.resolve(), CLOSURE_EVIDENCE.resolve(),
              FUNDING_EVIDENCE.resolve(), REGISTRY.resolve()}
    if args.funding_source is not None:
        inputs.add(args.funding_source.resolve())
    if len(outputs) != 2 or outputs & inputs:
        report = _invalid_report("outputs must be distinct and cannot overwrite inputs")
        print(report["decision"])
        return 2
    try:
        target, closed = load_pinned_target()
        if args.funding_source is None:
            rates, funding_sha = load_pinned_funding_rates()
            funding_provenance = "pinned_committed_diagnostic_evidence"
        else:
            _, rates, funding_sha = funding.funding_source(args.funding_source)
            funding_provenance = "local_checksum_bound_raw_response_replay"
        ledger_input, report = adapt(root=args.root, target=target, rates=rates)
        report["provenance"] = {
            "closure_evidence_sha256": CLOSURE_EVIDENCE_SHA256,
            "closure_source_run_id": closed["source_run_id"],
            "closure_executed_release_sha": closed["closure_anchor"]["executed_release_sha"],
            "funding_source_sha256": funding_sha,
            "funding_provenance": funding_provenance,
            "adapter_engine_sha256": hashlib.sha256(
                pathlib.Path(__file__).read_bytes()).hexdigest(),
            "ledger_engine_sha256": hashlib.sha256(
                pathlib.Path(ledger.__file__).read_bytes()).hexdigest(),
        }
        lifecycle_economics._atomic_write(args.ledger_output, ledger_input)
        report["ledger_output_written"] = True
        lifecycle_economics._atomic_write(args.report_output, report)
        print(report["decision"])
        return 0
    except EvidenceGap as exc:
        report = _invalid_report(str(exc), evidence_gap=True)
        report["ledger_output_written"] = False
        lifecycle_economics._atomic_write(args.report_output, report)
        print(report["decision"])
        return 0
    except (OSError, ValueError, TypeError, KeyError, IndexError,
            ArithmeticError, json.JSONDecodeError) as exc:
        report = _invalid_report(str(exc))
        report["ledger_output_written"] = False
        lifecycle_economics._atomic_write(args.report_output, report)
        print(report["decision"])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
