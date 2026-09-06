#!/usr/bin/env python3
"""Bounded public-source qualification, not a backtest or trading workflow.

Exactly eight public requests: full/partition funding, two explicit delivery
queries, two explicit instrument queries, one contextual mark candle. Preserve
raw bytes for replay; never replace settlement marks or BBO with candle values.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
from typing import Any
from urllib.parse import urlencode

import audit_option_historical_sample as sample
import audit_option_subaccount_ledger as ledger


MAX_RESPONSE_BYTES = 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}\Z")
API = "https://api.bybit.com"
require = ledger.require
number = ledger.number
AUTHORITIES = ledger.AUTHORITIES.copy()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encoded(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def decode(raw: bytes) -> dict[str, Any]:
    require(0 < len(raw) <= MAX_RESPONSE_BYTES, "empty or oversized public response")
    value = json.loads(raw, object_pairs_hook=ledger.unique_object)
    require(isinstance(value, dict), "JSON object required")
    return value


def epoch(value: Any) -> int:
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,14}", value) is not None, "invalid source timestamp")
    result = int(value)
    require(result > 0, "zero source timestamp")
    return result


def request_plan(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ledger.fields(case, {"schema_version", "start_ms", "end_ms", "option_symbols", "sample_date",
                         "sample_offset_minute", "sample_raw_sha256"}, "case")
    require(case["schema_version"] == "option_public_history_case_v1", "case schema mismatch")
    start, end = ledger.integer(case["start_ms"], "start"), ledger.integer(case["end_ms"], "end")
    require(60000 < end - start <= 7 * 86400000, "case must span more than a minute and at most seven days")
    symbols = case["option_symbols"]
    require(isinstance(symbols, list) and len(symbols) == 2, "one explicit call/put pair required")
    require(all(isinstance(s, str) and sample.SYMBOL.fullmatch(s) for s in symbols), "unsupported option symbols")
    call, put = (symbol.split("-") for symbol in symbols)
    require(call[3] == "C" and put[3] == "P" and call[:3] == put[:3], "mismatched call/put pair")
    expiry_date = dt.datetime.strptime(call[1], "%d%b%y").date()
    require(dt.datetime.fromtimestamp(end / 1000, dt.timezone.utc).date() == expiry_date, "case end/expiry date mismatch")
    _, minute = sample.request_contract(case["sample_date"], case["sample_offset_minute"], symbols)
    require(start <= minute and minute + 60000 <= end, "sample outside requested lifecycle")
    require(isinstance(case["sample_raw_sha256"], str) and SHA.fullmatch(case["sample_raw_sha256"]), "invalid sample hash")
    middle = start + (end - start) // 2
    plan = {}
    for key, left, right in (("funding_full", start, end), ("funding_left", start, middle),
                             ("funding_right", middle + 1, end)):
        plan[key] = {"path": "/v5/market/funding/history", "params": {
            "category": "linear", "symbol": "BTCUSDT", "startTime": left, "endTime": right, "limit": 200}}
    for index, symbol in enumerate(symbols):
        plan[f"delivery_{index}"] = {"path": "/v5/market/delivery-price", "params": {
            "category": "option", "symbol": symbol, "settleCoin": "USDT", "limit": 200}}
        plan[f"instrument_{index}"] = {"path": "/v5/market/instruments-info", "params": {
            "category": "option", "symbol": symbol, "limit": 1000}}
    minute_end = end // 60000 * 60000
    plan["mark_context"] = {"path": "/v5/market/mark-price-kline", "params": {
        "category": "linear", "symbol": "BTCUSDT", "interval": "1",
        "start": minute_end, "end": minute_end, "limit": 1}}
    return plan


def fetch(request: dict[str, Any]) -> bytes:
    # request comes only from request_plan(), never a user-provided URL.
    require(request["path"] in {"/v5/market/funding/history", "/v5/market/delivery-price",
                                 "/v5/market/instruments-info", "/v5/market/mark-price-kline"}, "endpoint not allowed")
    url = API + request["path"] + "?" + urlencode(request["params"])
    response = subprocess.run([
        "curl", "--disable", "--fail", "--silent", "--show-error", "--proto", "=https",
        "--connect-timeout", "10", "--max-time", "25", "--max-filesize", str(MAX_RESPONSE_BYTES), url,
    ], capture_output=True, timeout=30, check=False)
    require(response.returncode == 0, f"public transport failed: curl {response.returncode}")
    require(0 < len(response.stdout) <= MAX_RESPONSE_BYTES, "empty or oversized transport body")
    return response.stdout


def rows(payload: dict[str, Any], category: str, end: int) -> list[Any]:
    require(type(payload["retCode"]) is int and payload["retCode"] == 0, "public API returned error")
    require(type(payload["time"]) is int and payload["time"] >= end, "source response predates requested history")
    result = payload["result"]
    require(result["category"] == category and isinstance(result["list"], list), "response category/list mismatch")
    require(not result.get("nextPageCursor"), "unconsumed response cursor")
    return result["list"]


def funding(payload: dict[str, Any], request: dict[str, Any]) -> dict[int, str]:
    params = request["params"]
    values = rows(payload, "linear", params["endTime"])
    require(len(values) < params["limit"], "funding page saturated; exhaustion unproven")
    found: dict[int, str] = {}
    for row in values:
        require(row["symbol"] == "BTCUSDT", "wrong funding symbol")
        when = epoch(row["fundingRateTimestamp"])
        require(params["startTime"] <= when <= params["endTime"], "funding outside query interval")
        require(when not in found, "duplicate funding boundary")
        rate = number(row["fundingRate"], "funding rate")
        require(abs(rate) <= 1, "funding rate outside scope")
        found[when] = format(rate, "f")
    return found


def assess(case: dict[str, Any], raw: dict[str, bytes], sample_raw: bytes) -> dict[str, Any]:
    plan = request_plan(case)
    require(set(raw) == set(plan), "missing/extra public response")
    require(digest(sample_raw) == case["sample_raw_sha256"], "historical sample hash mismatch")
    sample_report = sample.audit_sample(sample_raw, date=case["sample_date"], offset=case["sample_offset_minute"],
                                        symbols=case["option_symbols"])
    require(sample_report["status"] == "PASS_SAMPLE_SCHEMA_ONLY", "option sample failed schema checks")
    payloads = {key: decode(value) for key, value in raw.items()}
    all_rates = funding(payloads["funding_full"], plan["funding_full"])
    left = funding(payloads["funding_left"], plan["funding_left"])
    right = funding(payloads["funding_right"], plan["funding_right"])
    require(not set(left) & set(right), "funding partitions overlap")
    require({key: number(value, "funding rate") for key, value in all_rates.items()} ==
            {key: number(value, "funding rate") for key, value in {**left, **right}.items()},
            "full/partition funding disagreement")
    deliveries, metadata = [], []
    for index, symbol in enumerate(case["option_symbols"]):
        delivered = rows(payloads[f"delivery_{index}"], "option", case["end_ms"])
        require(len(delivered) <= 1, "ambiguous delivery response")
        if delivered:
            row = delivered[0]
            require(row["symbol"] == symbol and epoch(row["deliveryTime"]) == case["end_ms"], "delivery identity/time mismatch")
            value = number(row["deliveryPrice"], "delivery price", positive=True)
            deliveries.append({"symbol": symbol, "delivery_time_ms": case["end_ms"], "settle_coin_query": "USDT",
                               "delivery_price": format(value, "f"), "source": f"delivery_{index}"})
        payload = payloads[f"instrument_{index}"]
        require(type(payload.get("retCode")) is int, "instrument retCode invalid")
        if payload["retCode"] != 0:
            metadata.append({"symbol": symbol, "status": "UNAVAILABLE_FROM_THIS_REQUEST", "ret_code": payload["retCode"]})
        else:
            values = rows(payload, "option", case["end_ms"])
            require(len(values) <= 1, "ambiguous instrument response")
            if not values:
                metadata.append({"symbol": symbol, "status": "EMPTY_FROM_THIS_REQUEST"})
            else:
                row = values[0]
                require(row["symbol"] == symbol and row["baseCoin"] == "BTC" and
                        row["quoteCoin"] == row["settleCoin"] == "USDT" and
                        epoch(row["deliveryTime"]) == case["end_ms"], "instrument identity/units mismatch")
                for name in ("minOrderQty", "qtyStep"):
                    number(row["lotSizeFilter"][name], name, positive=True)
                number(row["deliveryFeeRate"], "delivery fee rate", nonnegative=True)
                metadata.append({"symbol": symbol, "status": "QUERY_TIME_METADATA_ONLY",
                                 "historical_effective_version_qualified": False})
    require(len({number(row["delivery_price"], "delivery price") for row in deliveries}) <= 1, "paired delivery disagreement")
    context = rows(payloads["mark_context"], "linear", case["end_ms"])
    require(payloads["mark_context"]["result"]["symbol"] == "BTCUSDT", "wrong mark symbol")
    require(len(context) <= 1, "ambiguous mark candle")
    mark = None
    if context:
        row = context[0]
        require(isinstance(row, list) and len(row) == 5, "invalid mark candle fields")
        require(epoch(row[0]) == plan["mark_context"]["params"]["start"], "mark candle timestamp mismatch")
        open_, high, low, close = [number(value, "mark OHLC", positive=True) for value in row[1:]]
        require(low <= min(open_, close) <= max(open_, close) <= high, "invalid mark OHLC order")
        mark = {"minute_start_ms": epoch(row[0]), "open": str(open_), "high": str(high), "low": str(low),
                "close": str(close), "settlement_mark_qualified": False, "hedge_bbo_qualified": False}
    _, sample_start = sample.request_contract(case["sample_date"], case["sample_offset_minute"], case["option_symbols"])
    sample_end = sample_start + 60000
    gaps = [{"start_ms": left, "end_ms_exclusive": right} for left, right in (
        (case["start_ms"], sample_start), (sample_end, case["end_ms"])) if right > left]
    requirements = ["continuous_held_option_and_hedge_bbo_with_sizes", "historical_instrument_units_versions",
                    "exact_funding_settlement_marks_or_account_cashflows", "historical_fees_and_margin_model",
                    "licensed_continuous_dataset_access", "causal_positions_and_full_nav_lifecycle"]
    if len(deliveries) != 2:
        requirements.append("missing_paired_delivery")
    if not all_rates:
        requirements.append("no_funding_records_returned")
    ordered = sorted(all_rates)
    return {"schema_version": "option_public_history_qualification_v1", "audit_completed": True,
            "qualification_decision": "INSUFFICIENT_HISTORICAL_EVIDENCE", "research_domain": "development_only",
            "case_sha256": digest(encoded(case)),
            "sample": {key: sample_report[key] for key in ("raw_sha256", "status", "message_count",
                "qualified_observations_by_symbol", "local_span_seconds", "exchange_span_seconds")},
            "funding": {"partition_consistent": True, "retrieval_not_page_saturated": True,
                "same_provider_consistency_not_independent_truth": True,
                "events": [{"settlement_id": f"bybit:linear:BTCUSDT:{when}", "symbol": "BTCUSDT", "ts_ms": when,
                            "rate": all_rates[when], "rate_kind": "settled_rate", "source": "funding_full"} for when in ordered],
                "observed_gaps_ms": [right - left for left, right in zip(ordered, ordered[1:])],
                "historical_interval_policy_qualified": False, "cashflows_qualified": False},
            "deliveries": deliveries, "instrument_queries": metadata, "mark_candle_context": mark,
            "coverage": {"requested_start_ms": case["start_ms"], "requested_end_ms": case["end_ms"],
                "sample_request_envelope_ms": [sample_start, sample_end], "sample_envelope_is_not_continuity_proof": True,
                "certain_missing_windows_outside_sample": gaps,
                "certain_missing_duration_ms": sum(row["end_ms_exclusive"] - row["start_ms"] for row in gaps)},
            "missing_requirements": requirements, "historical_data_qualified": False, "economic_qualification": False,
            "payoff_evidence": False, "actual_account_performance": False, "authorities": AUTHORITIES.copy()}


def read_bounded(path: pathlib.Path, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    require(not path.is_symlink(), "symlink evidence not allowed")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(0 < len(raw) <= limit, "empty or oversized evidence file")
    return raw


def save_bundle(root: pathlib.Path, case: dict[str, Any], raw: dict[str, bytes], sample_raw: bytes) -> pathlib.Path:
    plan = request_plan(case)
    require(set(raw) == set(plan) and digest(sample_raw) == case["sample_raw_sha256"], "bundle inputs mismatch")
    require(not root.is_symlink() and not (root / "raw").is_symlink(), "symlink output directory")
    (root / "raw").mkdir(parents=True, exist_ok=True)
    for content in [*raw.values(), sample_raw]:
        sample.persist_new_or_identical(root / "raw" / (digest(content) + ".raw"), content)
    manifest = {"schema_version": "option_public_history_bundle_v1", "case": case,
                "sample_raw_sha256": digest(sample_raw), "requests": {
                    key: {**plan[key], "raw_sha256": digest(content), "raw_bytes": len(content)} for key, content in raw.items()}}
    content = encoded(manifest)
    path = root / (digest(content) + ".bundle.json")
    sample.persist_new_or_identical(path, content)
    return path


def load_bundle(path: pathlib.Path) -> tuple[dict[str, Any], dict[str, bytes], bytes]:
    content = read_bounded(path)
    require(path.name == digest(content) + ".bundle.json", "bundle manifest hash mismatch")
    manifest = decode(content)
    ledger.fields(manifest, {"schema_version", "case", "sample_raw_sha256", "requests"}, "bundle")
    require(manifest["schema_version"] == "option_public_history_bundle_v1", "bundle schema mismatch")
    case = manifest["case"]
    plan = request_plan(case)
    require(set(manifest["requests"]) == set(plan), "bundle request set mismatch")
    require(not (path.parent / "raw").is_symlink(), "symlink raw directory")
    def get(identity: str, limit: int) -> bytes:
        require(isinstance(identity, str) and SHA.fullmatch(identity), "invalid raw hash")
        raw = read_bounded(path.parent / "raw" / (identity + ".raw"), limit)
        require(digest(raw) == identity, "raw checksum mismatch")
        return raw
    raw = {}
    for key, request in manifest["requests"].items():
        ledger.fields(request, {"path", "params", "raw_sha256", "raw_bytes"}, "request record")
        require(ledger.canonical({"path": request["path"], "params": request["params"]}) == ledger.canonical(plan[key]), "request identity mismatch")
        raw[key] = get(request["raw_sha256"], MAX_RESPONSE_BYTES)
        require(type(request["raw_bytes"]) is int and len(raw[key]) == request["raw_bytes"], "raw length mismatch")
    require(manifest["sample_raw_sha256"] == case["sample_raw_sha256"], "bundle sample identity mismatch")
    return case, raw, get(manifest["sample_raw_sha256"], sample.MAX_RAW_BYTES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--case", type=pathlib.Path, help="Collect exactly eight unauthenticated public responses")
    mode.add_argument("--replay", type=pathlib.Path, help="Replay hash-addressed bundle, without network")
    parser.add_argument("--sample-raw", type=pathlib.Path)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        if args.case:
            require(args.sample_raw is not None, "--sample-raw required for collection")
            case = decode(read_bounded(args.case))
            plan = request_plan(case)
            sample_raw = read_bounded(args.sample_raw, sample.MAX_RAW_BYTES)
            require(digest(sample_raw) == case["sample_raw_sha256"], "sample mismatch before fetch")
            raw = {key: fetch(request) for key, request in plan.items()}
            bundle = save_bundle(args.output_dir, case, raw, sample_raw)
        else:
            require(args.sample_raw is None, "replay reads only its bound sample")
            bundle = args.replay
            case, raw, sample_raw = load_bundle(bundle)
        report = assess(case, raw, sample_raw)
        report.update({"bundle_sha256": digest(read_bounded(bundle)), "bundle_path": str(bundle),
                       "public_origin_checked_this_invocation": bool(args.case),
                       "acquisition": "public_https_fetch" if args.case else "local_checksum_replay_origin_unverified",
                       "sample_origin": "preexisting_local_hash_bound_sample",
                       "engine_sha256": {pathlib.Path(module.__file__).name: digest(pathlib.Path(module.__file__).read_bytes())
                           for module in (sample, ledger)}, "audit_engine_sha256": digest(pathlib.Path(__file__).read_bytes())})
        require(not args.output_dir.is_symlink(), "symlink report directory")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        content = encoded(report)
        report_path = args.output_dir / (digest(content) + ".qualification.json")
        sample.persist_new_or_identical(report_path, content)
        print(json.dumps({"report_path": str(report_path), **report}, indent=2, allow_nan=False))
        # A complete INSUFFICIENT report is a successful audit, never a strategy PASS.
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError,
            ArithmeticError, subprocess.SubprocessError) as error:
        print(json.dumps({"audit_completed": False, "qualification_decision": "TECHNICALLY_INVALID_OR_SOURCE_UNAVAILABLE",
                          "error": str(error), "historical_data_qualified": False, "payoff_evidence": False,
                          "economic_qualification": False, "authorities": AUTHORITIES}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
