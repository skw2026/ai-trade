#!/usr/bin/env python3
"""Bounded, offline C2 funding sensitivity; never a settlement or risk certificate.

Replay three funding responses and three contextual mark candles per boundary.
The two position caps are hypotheses, NOT limits proved on the archived hedge
path. Even favourable sensitivity cannot qualify C2 or reopen the candidate.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import pathlib
from typing import Any

import audit_option_candidate_closure as closure
import audit_option_public_history as public


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLOSURE = ROOT / "docs/reviews/2026-09-12-option-candidate-closure-result.evidence.json"
CLOSURE_SHA = "eee37c222b2a1c6d80e301434fa506f6d7b1a67889f4375a9d258ef9a17c0ebf"
require, number = public.require, public.number
MAX_EVENTS = 24


def hashed_json(path: pathlib.Path, suffix: str) -> tuple[dict, str]:
    raw = public.read_bounded(path)
    identity = public.digest(raw)
    require(path.name == identity + suffix, "manifest hash mismatch")
    return closure.decode(raw), identity


def response(root: pathlib.Path, record: dict, expected: dict) -> dict:
    public.ledger.fields(record, {"path", "params", "raw_sha256", "raw_bytes"}, "request")
    require(public.ledger.canonical({k: record[k] for k in ("path", "params")}) ==
            public.ledger.canonical(expected), "request identity mismatch")
    identity = record["raw_sha256"]
    require(isinstance(identity, str) and public.SHA.fullmatch(identity), "invalid raw hash")
    require(not (root / "raw").is_symlink(), "symlink raw directory")
    raw = public.read_bounded(root / "raw" / (identity + ".raw"))
    require(public.digest(raw) == identity, "raw checksum mismatch")
    require(type(record["raw_bytes"]) is int and len(raw) == record["raw_bytes"], "raw length mismatch")
    return closure.decode(raw)


def funding_source(path: pathlib.Path) -> tuple[dict, dict[int, str], str]:
    report, identity = hashed_json(path, ".qualification.json")
    require(report["schema_version"] == "option_lifecycle_funding_source_check_v1", "funding schema mismatch")
    for key in ("cashflows_qualified", "causal_hedge_positions_qualified", "economic_qualification",
                "margin_model_qualified", "settlement_marks_qualified"):
        require(report.get(key) is False, "funding evidence has unsupported qualification")
    require(public.ledger.canonical(report["authorities"]) == public.ledger.canonical(public.AUTHORITIES),
            "funding authority changed")
    start = public.ledger.integer(report["window_start_ms"], "window start")
    end = public.ledger.integer(report["window_end_ms"], "window end")
    require(60000 < end - start <= 7 * 86400000, "funding window outside bounded scope")
    middle = (start + end) // 2
    require(set(report["requests"]) == {"full", "left", "right"}, "funding request set mismatch")
    rates = {}
    for key, left, right in (("full", start, end), ("left", start, middle), ("right", middle + 1, end)):
        request = {"path": "/v5/market/funding/history", "params": {
            "category": "linear", "symbol": "BTCUSDT", "startTime": left, "endTime": right, "limit": 200}}
        rates[key] = public.funding(response(path.parent, report["requests"][key], request), request)
    require(not set(rates["left"]) & set(rates["right"]), "overlapping funding partitions")
    require(rates["full"] == {**rates["left"], **rates["right"]}, "funding partition disagreement")
    require(0 < len(rates["full"]) <= MAX_EVENTS, "empty or excessive funding events")
    expected = [{"settled_rate": rate, "ts_ms": ts} for ts, rate in sorted(rates["full"].items())]
    require(public.ledger.canonical(report["events"]) == public.ledger.canonical(expected), "funding event mismatch")
    require(type(report["event_count"]) is int and report["event_count"] == len(expected), "funding count mismatch")
    return report, rates["full"], identity


def mark_plan(rates: dict[int, str]) -> dict[str, dict]:
    require(0 < len(rates) <= MAX_EVENTS, "mark request budget exceeded")
    require(all(type(ts) is int and ts >= 60000 and ts % 60000 == 0 for ts in rates),
            "funding boundary not minute aligned")
    return {str(ts): {"path": "/v5/market/mark-price-kline", "params": {
        "category": "linear", "symbol": "BTCUSDT", "interval": "1",
        "start": ts - 60000, "end": ts + 60000, "limit": 3}} for ts in sorted(rates)}


def mark_context(payload: dict, ts: int) -> dict:
    candles = public.rows(payload, "linear", ts + 120000)
    require(payload["result"]["symbol"] == "BTCUSDT", "wrong mark symbol")
    require(len(candles) == 3, "missing contextual candles")
    parsed = {}
    for row in candles:
        require(isinstance(row, list) and len(row) == 5, "invalid candle fields")
        minute = public.epoch(row[0])
        require(minute not in parsed, "duplicate candle")
        open_, high, low, close = [number(v, "mark OHLC", positive=True) for v in row[1:]]
        require(low <= min(open_, close) <= max(open_, close) <= high, "invalid OHLC order")
        parsed[minute] = (low, high)
    require(set(parsed) == {ts - 60000, ts, ts + 60000}, "context candle timestamps mismatch")
    return {"ts_ms": ts, "context_low_usdt": str(min(v[0] for v in parsed.values())),
            "context_high_usdt": str(max(v[1] for v in parsed.values()))}


def sensitivity(rates: dict[int, str], contexts: list[dict], totals: dict) -> dict:
    plan = mark_plan(rates)
    require(len(contexts) == len(plan) and {str(r["ts_ms"]) for r in contexts} == set(plan),
            "context boundary coverage mismatch")
    rate_sum = sum((abs(number(rate, "settled rate")) for rate in rates.values()), Decimal(0))
    weighted = Decimal(0)
    for row in contexts:
        low = number(row["context_low_usdt"], "context low", positive=True)
        high = number(row["context_high_usdt"], "context high", positive=True)
        require(low <= high, "context low exceeds high")
        weighted += abs(number(rates[row["ts_ms"]], "settled rate")) * high
    base, stress = (number(totals[k], k) for k in ("base_net_pnl_usdt", "stress_net_pnl_usdt"))
    delivery = number(totals["delivery_fee_usdt"], "delivery fee", nonnegative=True)
    require(stress <= base < 0, "sensitivity requires negative closed-candidate totals")
    scenarios = []
    for cap in (Decimal("0.01"), Decimal("0.02")):
        credit = cap * weighted
        scenarios.append({"assumed_absolute_position_cap_btc": str(cap),
            "conditional_funding_credit_envelope_usdt": str(credit),
            "conditional_base_total_usdt": str(base + credit),
            "conditional_stress_total_usdt": str(stress + credit),
            "conditional_stress_without_any_delivery_fee_usdt": str(stress + credit + delivery),
            "base_break_even_uniform_mark_usdt": str(-base / (cap * rate_sum)) if rate_sum else None,
            "stress_break_even_uniform_mark_usdt": str(-stress / (cap * rate_sum)) if rate_sum else None,
            "stress_break_even_context_multiplier": str(-stress / credit) if credit else None})
    return {"schema_version": "option_funding_sensitivity_v1", "decision": "DIAGNOSTIC_ONLY_C2_INCOMPLETE",
        "candidate_state": "CLOSED", "event_count": len(rates), "sum_absolute_settled_rates": str(rate_sum),
        "funding_events": [{"ts_ms": ts, "settled_rate": rate} for ts, rate in sorted(rates.items())],
        "frozen_totals_usdt": totals, "scenarios": scenarios, "mark_contexts": contexts,
        "assumptions": ["position caps are hypotheses, not verified archive extrema or risk limits",
            "settlement marks assumed no higher than surrounding candle highs; this is NOT verified",
            "every returned boundary receives maximum favourable funding, regardless of actual position or sign",
            "counterfactual gross and other costs unchanged; no strategy is simulated",
            "all delivery fees removed only in the explicitly labelled optimistic sensitivity"],
        "limitations": ["candle extrema are context, not settlement prices or BBO",
            "same-provider funding partitions do not independently prove calendar completeness",
            "no causal position reconstruction, margin model, full NAV or risk qualification",
            "aggregate USDT sensitivity does not recompute or preserve the bootstrap decision"],
        "settlement_marks_qualified": False, "position_caps_verified": False, "cashflows_qualified": False,
        "account_risk_qualified": False, "economic_qualification": False, "actual_account_performance": False,
        "profitability_claim_allowed": False, "sharpe_claim_allowed": False, "drawdown_claim_allowed": False,
        "new_candidate_registered": False, "forward_wait_started": False, "authorities": public.AUTHORITIES.copy()}


def audit(funding_path: pathlib.Path, mark_path: pathlib.Path) -> dict:
    funding, rates, funding_sha = funding_source(funding_path)
    bundle, mark_sha = hashed_json(mark_path, ".marks.json")
    public.ledger.fields(bundle, {"schema_version", "funding_source_sha256", "requests"}, "mark bundle")
    require(bundle["schema_version"] == "option_funding_mark_context_bundle_v1" and
            bundle["funding_source_sha256"] == funding_sha, "mark/funding identity mismatch")
    plan = mark_plan(rates)
    require(set(bundle["requests"]) == set(plan), "mark request set mismatch")
    contexts = [mark_context(response(mark_path.parent, bundle["requests"][key], request), int(key))
                for key, request in plan.items()]
    raw = public.read_bounded(CLOSURE)
    require(public.digest(raw) == CLOSURE_SHA, "pinned closure summary changed")
    closed = closure.decode(raw)
    closure.no_authority(closed)
    require(closed["closure_latched"] is True and closed["decision"] == "CLOSED_CANDIDATE_NO_REOPEN",
            "candidate not closed")
    lifecycles = closed["anchor_lifecycles"]
    require(len(lifecycles) == 6 and funding["window_start_ms"] <= min(r["selected_epoch_ms"] for r in lifecycles)
            and funding["window_end_ms"] == max(r["delivery_time_epoch_ms"] for r in lifecycles),
            "funding/closure window mismatch")
    totals = {key: str(sum((Decimal(str(r["primary_payoff"][key])) for r in lifecycles), Decimal(0)))
              for key in ("base_net_pnl_usdt", "stress_net_pnl_usdt", "delivery_fee_usdt")}
    report = sensitivity(rates, contexts, totals)
    report["provenance"] = {"funding_source_sha256": funding_sha, "mark_bundle_sha256": mark_sha,
        "closure_summary_sha256": CLOSURE_SHA, "closure_source_run_id": closed["source_run_id"],
        "local_checksum_replay_not_independent_origin_authentication": True,
        "closure_summary_not_raw_archive_replay": True,
        "engine_sha256": {pathlib.Path(m.__file__).name: public.digest(pathlib.Path(m.__file__).read_bytes())
                          for m in (public, public.ledger, closure)},
        "audit_engine_sha256": public.digest(pathlib.Path(__file__).read_bytes())}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--funding-source", type=pathlib.Path, required=True)
    parser.add_argument("--mark-bundle", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        print(public.encoded(audit(args.funding_source, args.mark_bundle)).decode(), end="")
        return 0
    except (ValueError, KeyError, TypeError, OSError, ArithmeticError) as error:
        print(public.encoded({"decision": "INVALID_SENSITIVITY_EVIDENCE", "reason": str(error),
                              "authorities": public.AUTHORITIES.copy()}).decode(), end="")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
