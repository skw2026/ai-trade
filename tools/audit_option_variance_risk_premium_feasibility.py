#!/usr/bin/env python3
"""Audit live option-market feasibility and forward evidence readiness without fitting a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import statistics
import time
from typing import Any, Dict, Mapping, Sequence

import capture_bybit_option_vrp as capture


SCHEMA_VERSION = "option_variance_risk_premium_feasibility_v1"
POLICY_SCHEMA_VERSION = "option_variance_risk_premium_feasibility_policy_v1"
FROZEN_POLICY_IDENTITY_SHA256 = "fa890dfe49b19ee1d033932f9ed9e2e70294d5488a65171a53c65b93f093909e"


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


def _float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


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


def audit_capture_root(
    root: pathlib.Path, *, now_epoch_ms: int, expected_scope: Mapping[str, Any] | None = None
) -> Dict[str, Any]:
    root = root.expanduser().resolve()
    summary: Dict[str, Any] = {
        "root": str(root), "root_present": root.is_dir(), "valid_segment_count": 0,
        "invalid_segment_count": 0, "invalid_segments": [], "checksum_bound_seconds": 0.0,
        "successful_poll_count": 0, "completed_expiries_with_delivery": 0,
        "completed_delivery_times": [], "first_capture_epoch_ms": None, "last_capture_epoch_ms": None,
    }
    reports_root = root / "reports" / capture.BASE_COIN
    if not reports_root.is_dir():
        return summary
    intervals: list[tuple[int, int]] = []
    delivery_times: set[int] = set()
    for report_path in sorted(reports_root.glob("*.json")):
        try:
            payload = read_json(report_path)
            if payload.get("schema_version") != capture.SCHEMA_VERSION or payload.get("status") != "PASS":
                raise ValueError("capture report contract mismatch")
            if expected_scope is not None:
                selection = payload.get("selection_contract")
                expected_selection = {
                    "minimum_dte_days": float(expected_scope["minimum_dte_days"]),
                    "maximum_dte_days": float(expected_scope["maximum_dte_days"]),
                    "maximum_absolute_moneyness": float(expected_scope["maximum_absolute_moneyness"]),
                    "poll_interval_seconds": float(expected_scope["poll_interval_seconds"]),
                }
                if selection != expected_selection:
                    raise ValueError("capture selection contract drift")
            raw_payload, feature_payload = payload.get("raw"), payload.get("features")
            if not isinstance(raw_payload, Mapping) or not isinstance(feature_payload, Mapping):
                raise ValueError("capture artifact contract missing")
            raw_path = _safe_artifact(root, raw_payload.get("path"), root / "raw" / capture.BASE_COIN)
            feature_path = _safe_artifact(root, feature_payload.get("path"), root / "features" / capture.BASE_COIN)
            if sha256_file(raw_path) != raw_payload.get("sha256") or sha256_file(feature_path) != feature_payload.get("sha256"):
                raise ValueError("capture artifact checksum mismatch")
            coverage = payload.get("coverage", {})
            start = int(coverage.get("capture_started_epoch_ms") or 0)
            end = int(coverage.get("capture_completed_epoch_ms") or 0)
            polls = int(coverage.get("successful_poll_count") or 0)
            if start <= 0 or end < start or polls <= 0:
                raise ValueError("capture coverage is invalid")
            intervals.append((start, end))
            summary["successful_poll_count"] += polls
            quality = payload.get("quality", {})
            observed = quality.get("delivery_times_observed", []) if isinstance(quality, Mapping) else []
            if not isinstance(observed, list):
                raise ValueError("delivery time evidence is invalid")
            delivery_times.update(int(value) for value in observed if int(value) > 0)
            summary["valid_segment_count"] += 1
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            summary["invalid_segment_count"] += 1
            summary["invalid_segments"].append({"report": report_path.name, "reason": str(exc)})
    if intervals:
        summary["first_capture_epoch_ms"] = min(start for start, _ in intervals)
        summary["last_capture_epoch_ms"] = max(end for _, end in intervals)
        summary["checksum_bound_seconds"] = _merged_duration_ms(intervals) / 1000.0
    completed = sorted(value for value in delivery_times if value <= now_epoch_ms)
    summary["completed_delivery_times"] = completed
    summary["completed_expiries_with_delivery"] = len(completed)
    return summary


def fetch_live_snapshot(policy: Mapping[str, Any], *, base_url: str, now_epoch_ms: int) -> Dict[str, Any]:
    scope = policy["market_scope"]
    instruments_payload = capture.fetch_json("/v5/market/instruments-info", {"category": "option", "baseCoin": scope["base_coin"], "limit": 1000}, base_url=base_url)
    tickers_payload = capture.fetch_json("/v5/market/tickers", {"category": "option", "baseCoin": scope["base_coin"]}, base_url=base_url)
    trades_payload = capture.fetch_json("/v5/market/recent-trade", {"category": "option", "baseCoin": scope["base_coin"], "limit": 1000}, base_url=base_url)
    hedge_ticker_payload = capture.fetch_json("/v5/market/tickers", {"category": "linear", "symbol": scope["hedge_symbol"]}, base_url=base_url)
    hedge_orderbook_payload = capture.fetch_json("/v5/market/orderbook", {"category": "linear", "symbol": scope["hedge_symbol"], "limit": 1}, base_url=base_url)
    hv7_payload = capture.fetch_json("/v5/market/historical-volatility", {"category": "option", "baseCoin": scope["base_coin"], "quoteCoin": scope["settle_coin"], "period": 7}, base_url=base_url)
    hv30_payload = capture.fetch_json("/v5/market/historical-volatility", {"category": "option", "baseCoin": scope["base_coin"], "quoteCoin": scope["settle_coin"], "period": 30}, base_url=base_url)
    delivery_payload = capture.fetch_json("/v5/market/delivery-price", {"category": "option", "baseCoin": scope["base_coin"], "limit": 200}, base_url=base_url)
    instruments, tickers, trades = map(capture.result_list, (instruments_payload, tickers_payload, trades_payload))
    normalized, feature = capture.normalize_snapshot(
        now_epoch_ms=now_epoch_ms, instruments=instruments, tickers=tickers, trades=trades,
        hedge_ticker=capture.result_list(hedge_ticker_payload), hedge_orderbook=hedge_orderbook_payload,
        hv7=capture.result_list(hv7_payload), hv30=capture.result_list(hv30_payload),
        delivery=capture.result_list(delivery_payload), seen_exec_ids=set(),
        minimum_dte_days=float(scope["minimum_dte_days"]), maximum_dte_days=float(scope["maximum_dte_days"]),
        maximum_absolute_moneyness=float(scope["maximum_absolute_moneyness"]),
    )
    scoped = normalized["scoped_options"]
    pair_rows: Dict[tuple[int, float], Dict[str, Any]] = {}
    for row in scoped:
        key = (int(row["deliveryTime"]), float(row["strike"]))
        bucket = pair_rows.setdefault(key, {"calls": [], "puts": [], "index_prices": []})
        side = "calls" if str(row["optionsType"]).lower() == "call" else "puts"
        bucket[side].append(row)
        bucket["index_prices"].append(_float(row.get("indexPrice")))
    atm_pairs: list[Dict[str, Any]] = []
    by_expiry: Dict[int, list[tuple[float, tuple[int, float], Dict[str, Any]]]] = {}
    for key, bucket in pair_rows.items():
        if not bucket["calls"] or not bucket["puts"]:
            continue
        index = statistics.median([value for value in bucket["index_prices"] if value > 0.0])
        if index <= 0.0:
            continue
        by_expiry.setdefault(key[0], []).append((abs(key[1] / index - 1.0), key, bucket))
    for expiry, candidates in sorted(by_expiry.items()):
        _, (_, strike), bucket = min(candidates, key=lambda item: item[0])
        call, put = bucket["calls"][0], bucket["puts"][0]
        bid = _float(call["bid1Price"]) + _float(put["bid1Price"])
        ask = _float(call["ask1Price"]) + _float(put["ask1Price"])
        ivs = [_float(call.get("markIv")), _float(put.get("markIv"))]
        atm_pairs.append({
            "delivery_time_epoch_ms": expiry, "dte_days": (expiry - now_epoch_ms) / 86400000.0,
            "strike": strike, "index_price": statistics.median(bucket["index_prices"]),
            "straddle_bid": bid, "straddle_ask": ask,
            "straddle_spread_ratio": (ask - bid) / ((ask + bid) / 2.0) if ask + bid > 0.0 else None,
            "mean_mark_iv": statistics.mean([value for value in ivs if value > 0.0]) if any(value > 0.0 for value in ivs) else None,
        })
    source_payloads = {
        "instruments": instruments_payload, "tickers": tickers_payload, "recent_trades": trades_payload,
        "hedge_ticker": hedge_ticker_payload, "hedge_orderbook": hedge_orderbook_payload,
        "historical_volatility_7d": hv7_payload, "historical_volatility_30d": hv30_payload,
        "delivery_price": delivery_payload,
    }
    return {
        "observed_epoch_ms": now_epoch_ms,
        "source_response_sha256": {name: capture.canonical_sha256(payload) for name, payload in source_payloads.items()},
        "source_response_count": len(source_payloads),
        "source_responses": source_payloads,
        "active_contract_count": int(feature["active_contract_count"]),
        "two_sided_contract_count": int(feature["two_sided_contract_count"]),
        "scoped_two_sided_contract_count": int(feature["scoped_two_sided_contract_count"]),
        "scoped_volume_contract_count": int(feature["scoped_volume_contract_count"]),
        "recent_trade_count": len(trades),
        "scoped_recent_trade_count": len(normalized["new_recent_trades"]),
        "scoped_spread_ratio_median": feature["scoped_spread_ratio_median"],
        "scoped_spread_ratio_p90": feature["scoped_spread_ratio_p90"],
        "historical_volatility_7d": feature["historical_volatility_7d"],
        "historical_volatility_30d": feature["historical_volatility_30d"],
        "atm_mark_iv_median": feature["atm_mark_iv_median"],
        "nearest_atm_straddles": atm_pairs,
    }


def build_report(*, policy: Mapping[str, Any], policy_path: pathlib.Path, live: Mapping[str, Any], capture_audit: Mapping[str, Any]) -> Dict[str, Any]:
    liquidity = policy["liquidity_gate"]
    market_checks = {
        "active_contracts": int(live["active_contract_count"]) >= int(liquidity["minimum_active_contracts"]),
        "two_sided_contracts": int(live["two_sided_contract_count"]) >= int(liquidity["minimum_two_sided_contracts"]),
        "scoped_two_sided_contracts": int(live["scoped_two_sided_contract_count"]) >= int(liquidity["minimum_scoped_two_sided_contracts"]),
        "scoped_volume_contracts": int(live["scoped_volume_contract_count"]) >= int(liquidity["minimum_scoped_contracts_with_volume"]),
        "scoped_spread_p90": float(live["scoped_spread_ratio_p90"]) <= float(liquidity["maximum_scoped_spread_ratio_p90"]),
        "recent_trades": int(live["recent_trade_count"]) >= int(liquidity["minimum_recent_trade_count"]),
    }
    capture_gate = policy["forward_capture_gate"]
    capture_checks = {
        "checksum_bound_seconds": float(capture_audit["checksum_bound_seconds"]) >= float(capture_gate["minimum_checksum_bound_seconds"]),
        "completed_expiries_with_delivery": int(capture_audit["completed_expiries_with_delivery"]) >= int(capture_gate["minimum_completed_expiries_with_delivery"]),
        "successful_polls": int(capture_audit["successful_poll_count"]) >= int(capture_gate["minimum_successful_polls"]),
        "all_segment_checksums_valid": int(capture_audit["invalid_segment_count"]) == 0,
    }
    decisions = policy["decision_contract"]
    market_pass = all(market_checks.values())
    capture_pass = all(capture_checks.values())
    decision = decisions["stop_decision"] if not market_pass else decisions["ready_decision"] if capture_pass else decisions["wait_decision"]
    iv = _float(live.get("atm_mark_iv_median"))
    hv = _float(live.get("historical_volatility_30d"))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE",
        "generated_epoch_ms": int(time.time() * 1000),
        "research_domain": "live_snapshot_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "policy": {
            "path": str(policy_path), "schema_version": policy["schema_version"],
            "canonical_sha256": capture.canonical_sha256(policy),
            "frozen_identity_sha256": FROZEN_POLICY_IDENTITY_SHA256,
            "identity_verified": capture.canonical_sha256(policy) == FROZEN_POLICY_IDENTITY_SHA256,
        },
        "verification_boundary": {
            "fully_verifiable_live_snapshot": True,
            "fully_verifiable_historical_payoff": False,
            "historical_capabilities": policy["historical_capability_contract"],
            "reason": "Bybit exposes current option BBO/trades/Greeks but no public expired executable option BBO or archived option orderbook/trade history.",
        },
        "live_market_snapshot": dict(live),
        "market_gate": {"status": "PASS" if market_pass else "FAIL", "checks": market_checks, "contract": dict(liquidity)},
        "forward_capture": dict(capture_audit),
        "forward_capture_gate": {"status": "PASS" if capture_pass else "WAIT", "checks": capture_checks, "contract": dict(capture_gate)},
        "economics": {
            "cost_contract": policy["cost_contract"],
            "observed_atm_iv_minus_30d_hv": iv - hv if iv > 0.0 and hv > 0.0 else None,
            "observed_iv_hv_is_profit_evidence": False,
            "realized_delta_hedged_episode_count": 0,
            "stress_net_utility_lcb": None,
            "profitability_verified": False,
        },
        "decision": decision,
        "next_action": decisions["next_action"] if decision == decisions["wait_decision"] else "run_frozen_option_payoff_audit" if decision == decisions["ready_decision"] else "close_option_vrp_market_family",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", default=capture.BASE_URL)
    parser.add_argument("--now-epoch-ms", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = pathlib.Path(args.config)
    policy = read_json(config_path)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("option VRP policy schema mismatch")
    identity = capture.canonical_sha256(policy)
    if identity != FROZEN_POLICY_IDENTITY_SHA256:
        raise ValueError(f"option VRP frozen policy identity mismatch: {identity}")
    now_ms = args.now_epoch_ms or int(time.time() * 1000)
    live = fetch_live_snapshot(policy, base_url=args.base_url, now_epoch_ms=now_ms)
    capture_audit = audit_capture_root(
        pathlib.Path(args.capture_root), now_epoch_ms=now_ms,
        expected_scope=policy["market_scope"],
    )
    report = build_report(policy=policy, policy_path=config_path, live=live, capture_audit=capture_audit)
    capture.atomic_write_json(pathlib.Path(args.output), report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
