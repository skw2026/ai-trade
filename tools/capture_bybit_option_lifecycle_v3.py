#!/usr/bin/env python3
"""Capture discovery and sticky tracked BTC option lifecycles without imputation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import lzma
import math
import os
import pathlib
import tempfile
import time
import urllib.error
from typing import Any, Callable, Dict, Mapping, Sequence

import capture_bybit_option_vrp_v2 as v2


SCHEMA_VERSION = "bybit_btc_option_lifecycle_capture_v3"
SNAPSHOT_SCHEMA_VERSION = "bybit_btc_option_lifecycle_snapshot_v3"
STATE_SCHEMA_VERSION = "bybit_btc_option_lifecycle_state_v3"
POLICY_SCHEMA_VERSION = "option_lifecycle_capture_policy_v1"
MANIFEST_SCHEMA_VERSION = "option_lifecycle_capture_manifest_v1"
CAPTURE_ROOT_NAME = "bybit_btc_option_lifecycle_v3"
RAW_CODEC = "xz_lzma_preset1"
FROZEN_POLICY_CANONICAL_SHA256 = "acdd24bdcc2657e170666da4146b41c14e0ea6cda9a5c18d97052ed8e2c30896"
FROZEN_MANIFEST_CANONICAL_SHA256 = "9573f45d5b13d873675ad5fc7798c5fcf33fc20d7d515727ee6eaa374ef8bc81"
BASE_URL = v2.BASE_URL
BASE_COIN = "BTC"
QUOTE_COIN = "USDT"
SETTLE_COIN = "USDT"
HEDGE_SYMBOL = "BTCUSDT"
OUTPUT_FIELDS = (
    "timestamp_epoch_ms", "snapshot_completed_epoch_ms", "poll_latency_ms",
    "discovery_pair_count", "active_lifecycle_count", "tracked_observed_count",
    "tracked_two_sided_count", "tracked_entry_executable_count",
    "paired_delivery_evidence_count", "hedge_bid", "hedge_ask",
)


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root is not an object: {path.name}")
    return payload


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


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


def load_contract(policy_path: pathlib.Path, manifest_path: pathlib.Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
    policy, manifest = read_json(policy_path), read_json(manifest_path)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("v3 lifecycle policy schema mismatch")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("v3 lifecycle manifest schema mismatch")
    policy_sha = canonical_sha256(policy)
    if policy_sha != FROZEN_POLICY_CANONICAL_SHA256:
        raise ValueError("v3 lifecycle frozen policy identity mismatch")
    if manifest.get("policy_canonical_sha256") != policy_sha:
        raise ValueError("v3 lifecycle manifest policy identity mismatch")
    if canonical_sha256(manifest) != FROZEN_MANIFEST_CANONICAL_SHA256:
        raise ValueError("v3 lifecycle frozen manifest identity mismatch")
    if (manifest.get("experiment_id") != policy.get("experiment_id")
            or manifest.get("policy_path") != "config/option_lifecycle_capture_v3.json"):
        raise ValueError("v3 lifecycle experiment identity mismatch")
    capture = policy.get("capture_contract", {})
    expected = {
        "capture_schema_version": SCHEMA_VERSION,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "capture_root_name": CAPTURE_ROOT_NAME,
        "raw_codec": RAW_CODEC,
        "base_coin": BASE_COIN,
        "quote_coin": QUOTE_COIN,
        "settle_coin": SETTLE_COIN,
        "hedge_symbol": HEDGE_SYMBOL,
    }
    if any(capture.get(key) != value for key, value in expected.items()):
        raise ValueError("v3 lifecycle capture identity mismatch")
    observation_start = int(manifest.get("observation_start_epoch_ms") or 0)
    if observation_start <= 0:
        raise ValueError("v3 lifecycle observation start is invalid")
    if policy.get("research_domain") != "development_only" or manifest.get("research_domain") != "development_only":
        raise ValueError("v3 lifecycle contract is outside development")
    for payload in (policy.get("authorities", {}), manifest):
        if any(payload.get(name) is not False for name in (
            "promotion_authority", "demo_activation_authorized", "live_activation_authorized"
        )):
            raise ValueError("v3 lifecycle contract cannot grant activation authority")
    return policy, manifest


def initial_state(*, policy: Mapping[str, Any], manifest: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "experiment_id": policy["experiment_id"],
        "policy_canonical_sha256": canonical_sha256(policy),
        "manifest_canonical_sha256": canonical_sha256(manifest),
        "revision": 0,
        "active_lifecycle": None,
        "completed_lifecycles": [],
    }


def load_state(path: pathlib.Path, *, policy: Mapping[str, Any],
               manifest: Mapping[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return initial_state(policy=policy, manifest=manifest)
    if path.is_symlink() or not path.is_file():
        raise ValueError("v3 lifecycle state path is unsafe")
    state = read_json(path)
    expected = initial_state(policy=policy, manifest=manifest)
    for field in ("schema_version", "experiment_id", "policy_canonical_sha256", "manifest_canonical_sha256"):
        if state.get(field) != expected[field]:
            raise ValueError(f"v3 lifecycle state {field} mismatch")
    if int(state.get("revision") or 0) < 0:
        raise ValueError("v3 lifecycle state revision is invalid")
    if state.get("active_lifecycle") is not None and not isinstance(state.get("active_lifecycle"), Mapping):
        raise ValueError("v3 lifecycle active state is invalid")
    if not isinstance(state.get("completed_lifecycles"), list):
        raise ValueError("v3 lifecycle completed state is invalid")
    return state


def _instrument_map(instruments: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    scoped: Dict[str, Dict[str, Any]] = {}
    for raw in instruments:
        if any(str(raw.get(field) or "").upper() != expected for field, expected in (
            ("baseCoin", BASE_COIN), ("quoteCoin", QUOTE_COIN), ("settleCoin", SETTLE_COIN)
        )):
            continue
        symbol = str(raw.get("symbol") or "")
        delivery = int(_number(raw.get("deliveryTime"), field=f"{symbol}.deliveryTime", positive=True))
        lot = raw.get("lotSizeFilter")
        price = raw.get("priceFilter")
        if not isinstance(lot, Mapping) or not isinstance(price, Mapping):
            raise ValueError(f"{symbol}.instrument filters are missing")
        _number(lot.get("minOrderQty"), field=f"{symbol}.minOrderQty", positive=True)
        _number(lot.get("qtyStep"), field=f"{symbol}.qtyStep", positive=True)
        _number(price.get("tickSize"), field=f"{symbol}.tickSize", positive=True)
        _number(raw.get("deliveryFeeRate"), field=f"{symbol}.deliveryFeeRate", positive=True)
        row = dict(raw)
        row["deliveryTime"] = delivery
        scoped[symbol] = row
    return scoped


def _ticker_map(tickers: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(row.get("symbol") or ""): dict(row) for row in tickers if row.get("symbol")}


def discovery_rows(*, now_epoch_ms: int, instruments: Mapping[str, Mapping[str, Any]],
                   tickers: Mapping[str, Mapping[str, Any]], policy: Mapping[str, Any]) -> list[Dict[str, Any]]:
    contract = policy["discovery_contract"]
    rows: list[Dict[str, Any]] = []
    for symbol, instrument in instruments.items():
        ticker = tickers.get(symbol)
        if ticker is None:
            continue
        bid = _number(ticker.get("bid1Price") or 0, field=f"{symbol}.bid", nonnegative=True)
        ask = _number(ticker.get("ask1Price") or 0, field=f"{symbol}.ask", nonnegative=True)
        bid_size = _number(ticker.get("bid1Size") or 0, field=f"{symbol}.bidSize", nonnegative=True)
        ask_size = _number(ticker.get("ask1Size") or 0, field=f"{symbol}.askSize", nonnegative=True)
        index = _number(ticker.get("indexPrice") or 0, field=f"{symbol}.index", nonnegative=True)
        delivery = int(instrument["deliveryTime"])
        parts = symbol.split("-")
        strike = _number(parts[2] if len(parts) >= 5 else instrument.get("strike"), field=f"{symbol}.strike", positive=True)
        dte = (delivery - now_epoch_ms) / 86400000.0
        moneyness = strike / index - 1.0 if index > 0.0 else math.inf
        if not (
            float(contract["minimum_dte_days"]) <= dte <= float(contract["maximum_dte_days"])
            and abs(moneyness) <= float(contract["maximum_absolute_moneyness"])
            and bid > 0.0 and ask >= bid
            and bid_size + 1e-12 >= float(contract["minimum_entry_bid_size_btc"])
            and ask_size + 1e-12 >= float(contract["minimum_entry_ask_size_btc"])
        ):
            continue
        rows.append({
            "symbol": symbol, "deliveryTime": delivery, "strike": strike,
            "optionsType": str(instrument.get("optionsType") or ""),
            "dteDays": dte, "moneyness": moneyness, "indexPrice": ticker.get("indexPrice"),
            "bid1Price": ticker.get("bid1Price"), "ask1Price": ticker.get("ask1Price"),
            "bid1Size": ticker.get("bid1Size"), "ask1Size": ticker.get("ask1Size"),
        })
    return rows


def select_lifecycle(*, now_epoch_ms: int, rows: Sequence[Mapping[str, Any]],
                     instruments: Mapping[str, Mapping[str, Any]],
                     policy: Mapping[str, Any]) -> Dict[str, Any] | None:
    pairs: Dict[tuple[int, float], Dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        side = str(row.get("optionsType") or "").lower()
        if side not in {"call", "put"}:
            continue
        key = (int(row["deliveryTime"]), float(row["strike"]))
        pairs.setdefault(key, {})[side] = row
    candidates = [(key, sides) for key, sides in pairs.items() if set(sides) == {"call", "put"}]
    if not candidates:
        return None
    target = float(policy["discovery_contract"]["target_dte_days"])
    (delivery, strike), sides = min(candidates, key=lambda item: (
        abs(float(item[1]["call"]["dteDays"]) - target),
        abs(float(item[1]["call"]["moneyness"])), item[0][0], item[0][1],
    ))
    symbols = [str(sides[side]["symbol"]) for side in ("call", "put")]
    contracts = []
    for symbol in symbols:
        instrument = instruments[symbol]
        contracts.append({
            "symbol": symbol, "deliveryTime": int(instrument["deliveryTime"]),
            "strike": strike, "optionsType": str(instrument.get("optionsType") or ""),
            "baseCoin": BASE_COIN, "quoteCoin": QUOTE_COIN, "settleCoin": SETTLE_COIN,
            "minOrderQty": instrument["lotSizeFilter"]["minOrderQty"],
            "qtyStep": instrument["lotSizeFilter"]["qtyStep"],
            "tickSize": instrument["priceFilter"]["tickSize"],
            "deliveryFeeRate": instrument["deliveryFeeRate"],
        })
    identity = {"delivery_time_epoch_ms": delivery, "strike": strike, "symbols": symbols,
                "settle_coin": SETTLE_COIN}
    return {
        "lifecycle_id": f"btc-usdt-{delivery}-{int(strike)}-{canonical_sha256(identity)[:12]}",
        "identity_sha256": canonical_sha256(identity),
        "selected_epoch_ms": now_epoch_ms,
        "delivery_time_epoch_ms": delivery,
        "strike": strike,
        "symbols": symbols,
        "contracts": contracts,
        "selection_index_price": sides["call"]["indexPrice"],
        "selection_dte_days": sides["call"]["dteDays"],
        "selection_moneyness": sides["call"]["moneyness"],
    }


def tracked_rows(*, lifecycle: Mapping[str, Any] | None,
                 instruments: Mapping[str, Mapping[str, Any]],
                 tickers: Mapping[str, Mapping[str, Any]]) -> list[Dict[str, Any]]:
    if lifecycle is None:
        return []
    stored = {str(row["symbol"]): row for row in lifecycle["contracts"]}
    result: list[Dict[str, Any]] = []
    for symbol in lifecycle["symbols"]:
        contract = stored[str(symbol)]
        instrument = instruments.get(str(symbol))
        ticker = tickers.get(str(symbol))
        status = "OBSERVED" if ticker is not None else (
            "TICKER_MISSING" if instrument is not None else "INSTRUMENT_INACTIVE"
        )
        row: Dict[str, Any] = dict(contract)
        row["observation_status"] = status
        if ticker is not None:
            for field in (
                "bid1Price", "ask1Price", "bid1Size", "ask1Size", "bid1Iv", "ask1Iv",
                "markPrice", "markIv", "indexPrice", "underlyingPrice", "openInterest",
                "volume24h", "turnover24h", "delta", "gamma", "vega", "theta",
            ):
                row[field] = ticker.get(field)
        result.append(row)
    return result


def delivery_rows(*, lifecycle: Mapping[str, Any] | None,
                  rows: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    if lifecycle is None:
        return []
    symbols = set(map(str, lifecycle["symbols"]))
    expected_delivery = int(lifecycle["delivery_time_epoch_ms"])
    result_by_symbol: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        symbol = str(raw.get("symbol") or "")
        if symbol not in symbols:
            continue
        delivery = int(_number(raw.get("deliveryTime"), field=f"{symbol}.delivery", positive=True))
        price = _number(raw.get("deliveryPrice"), field=f"{symbol}.deliveryPrice", positive=True)
        if delivery != expected_delivery:
            raise ValueError("tracked delivery identity mismatch")
        normalized = {
            "symbol": symbol, "deliveryTime": delivery, "deliveryPrice": str(raw.get("deliveryPrice")),
            "deliveryPriceNumeric": price, "baseCoin": BASE_COIN, "quoteCoin": QUOTE_COIN,
            "settleCoin": SETTLE_COIN, "lifecycleIdentitySha256": lifecycle["identity_sha256"],
        }
        previous = result_by_symbol.get(symbol)
        if previous is not None and previous != normalized:
            raise ValueError("conflicting duplicate tracked delivery evidence")
        result_by_symbol[symbol] = normalized
    result = list(result_by_symbol.values())
    prices = {float(row["deliveryPriceNumeric"]) for row in result}
    if len(result) == len(symbols) and len(prices) != 1:
        raise ValueError("tracked call/put delivery prices conflict")
    return sorted(result, key=lambda row: str(row["symbol"]))


def _lifecycle_complete(lifecycle: Mapping[str, Any] | None,
                        deliveries: Sequence[Mapping[str, Any]]) -> bool:
    return bool(lifecycle is not None and len(deliveries) == len(lifecycle["symbols"]))


def _write_feature_csv(path: pathlib.Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        temporary = pathlib.Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in OUTPUT_FIELDS})
    temporary.replace(path)


def capture_live(*, raw_output: pathlib.Path, state: Dict[str, Any], policy: Mapping[str, Any],
                 manifest: Mapping[str, Any], duration_sec: float, poll_interval_sec: float,
                 base_url: str, fetcher: Callable[..., Dict[str, Any]] = v2.fetch_json,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep,
                 ) -> tuple[list[Dict[str, Any]], int, int, str, Dict[str, Any]]:
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    if raw_output.suffix != ".xz":
        raise ValueError("v3 lifecycle raw output must use XZ")
    features: list[Dict[str, Any]] = []
    started = completed = 0
    termination_reason = "duration_complete"
    deadline = monotonic() + duration_sec
    observation_start = int(manifest["observation_start_epoch_ms"])
    policy_sha, manifest_sha = canonical_sha256(policy), canonical_sha256(manifest)
    with lzma.open(raw_output, "wt", encoding="utf-8", preset=1) as handle:
        while True:
            poll_started = int(clock() * 1000)
            try:
                instruments_raw = v2.result_list(fetcher(
                    "/v5/market/instruments-info", {"category": "option", "baseCoin": BASE_COIN, "limit": 1000}, base_url=base_url
                ))
                tickers_raw = v2.result_list(fetcher(
                    "/v5/market/tickers", {"category": "option", "baseCoin": BASE_COIN}, base_url=base_url
                ))
                hedge_ticker_rows = v2.result_list(fetcher(
                    "/v5/market/tickers", {"category": "linear", "symbol": HEDGE_SYMBOL}, base_url=base_url
                ))
                hedge_book_payload = fetcher(
                    "/v5/market/orderbook", {"category": "linear", "symbol": HEDGE_SYMBOL, "limit": 1}, base_url=base_url
                )
                delivery_raw = v2.result_list(fetcher(
                    "/v5/market/delivery-price", {
                        "category": "option", "baseCoin": BASE_COIN, "settleCoin": SETTLE_COIN, "limit": 200,
                    }, base_url=base_url
                ))
                instruments = _instrument_map(instruments_raw)
                tickers = _ticker_map(tickers_raw)
                discovery = discovery_rows(
                    now_epoch_ms=poll_started, instruments=instruments, tickers=tickers, policy=policy
                )
                if state.get("active_lifecycle") is None and poll_started >= observation_start:
                    selected = select_lifecycle(
                        now_epoch_ms=poll_started, rows=discovery, instruments=instruments, policy=policy
                    )
                    if selected is not None:
                        state["active_lifecycle"] = selected
                        state["revision"] = int(state.get("revision") or 0) + 1
                lifecycle = state.get("active_lifecycle")
                tracked = tracked_rows(lifecycle=lifecycle, instruments=instruments, tickers=tickers)
                deliveries = delivery_rows(lifecycle=lifecycle, rows=delivery_raw)
                snapshot_completed = int(clock() * 1000)
                hedge_ticker = dict(hedge_ticker_rows[0]) if hedge_ticker_rows else {}
                hedge_book = hedge_book_payload.get("result", {}) if isinstance(hedge_book_payload.get("result"), Mapping) else {}
                snapshot = {
                    "schema_version": SNAPSHOT_SCHEMA_VERSION,
                    "experiment_id": policy["experiment_id"],
                    "policy_canonical_sha256": policy_sha,
                    "manifest_canonical_sha256": manifest_sha,
                    "timestamp_epoch_ms": poll_started,
                    "poll_started_epoch_ms": poll_started,
                    "snapshot_completed_epoch_ms": snapshot_completed,
                    "discovery_contract": policy["discovery_contract"],
                    "tracking_contract": policy["tracking_contract"],
                    "discovery_options": discovery,
                    "active_lifecycle": lifecycle,
                    "tracked_options": tracked,
                    "delivery_prices": deliveries,
                    "hedge_ticker": hedge_ticker,
                    "hedge_orderbook_l1": hedge_book,
                    "lifecycle_phase": "DELIVERY_OBSERVED" if _lifecycle_complete(lifecycle, deliveries) else (
                        "ACTIVE" if lifecycle is not None else (
                            "PRE_OBSERVATION" if poll_started < observation_start else "AWAITING_CANDIDATE"
                        )
                    ),
                }
            except (OSError, RuntimeError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
                if not features:
                    raise
                termination_reason = "transient_request_failure"
                break
            handle.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
            handle.flush()
            if started == 0:
                started = poll_started
            completed = poll_started
            pair_sides: Dict[tuple[int, float], set[str]] = {}
            for row in discovery:
                key = (int(row["deliveryTime"]), float(row["strike"]))
                pair_sides.setdefault(key, set()).add(str(row.get("optionsType") or "").lower())
            pairs = {key for key, sides in pair_sides.items() if sides == {"call", "put"}}
            tracked_two_sided = sum(
                1 for row in tracked
                if row.get("observation_status") == "OBSERVED"
                and _number(row.get("bid1Price") or 0, field="tracked.bid", nonnegative=True) > 0
                and _number(row.get("ask1Price") or 0, field="tracked.ask", nonnegative=True) > 0
            )
            features.append({
                "timestamp_epoch_ms": poll_started,
                "snapshot_completed_epoch_ms": snapshot_completed,
                "poll_latency_ms": snapshot_completed - poll_started,
                "discovery_pair_count": len(pairs),
                "active_lifecycle_count": int(lifecycle is not None),
                "tracked_observed_count": sum(row.get("observation_status") == "OBSERVED" for row in tracked),
                "tracked_two_sided_count": tracked_two_sided,
                "tracked_entry_executable_count": tracked_two_sided if lifecycle and poll_started == int(lifecycle["selected_epoch_ms"]) else 0,
                "paired_delivery_evidence_count": len(deliveries),
                "hedge_bid": hedge_ticker.get("bid1Price"), "hedge_ask": hedge_ticker.get("ask1Price"),
            })
            if _lifecycle_complete(lifecycle, deliveries):
                completed_row = dict(lifecycle)
                completed_row.update({
                    "completed_epoch_ms": poll_started,
                    "delivery_price": deliveries[0]["deliveryPriceNumeric"],
                })
                history = list(state.get("completed_lifecycles", []))
                if not any(row.get("lifecycle_id") == completed_row["lifecycle_id"] for row in history):
                    history.append(completed_row)
                keep = int(policy["tracking_contract"]["maximum_completed_state_entries"])
                state["completed_lifecycles"] = history[-keep:]
                state["active_lifecycle"] = None
                state["revision"] = int(state.get("revision") or 0) + 1
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleeper(min(poll_interval_sec, remaining))
    return features, started, completed, termination_reason, state


def build_report(*, root: pathlib.Path, raw_path: pathlib.Path, feature_path: pathlib.Path,
                 features: Sequence[Mapping[str, Any]], started: int, completed: int,
                 termination_reason: str, policy: Mapping[str, Any], manifest: Mapping[str, Any],
                 state_before_sha256: str, state_after: Mapping[str, Any]) -> Dict[str, Any]:
    if not features or started <= 0 or completed < started:
        raise ValueError("v3 lifecycle segment has no successful poll")
    root = root.resolve()
    if root.name != CAPTURE_ROOT_NAME:
        raise ValueError("v3 lifecycle capture root mismatch")
    recorded: Dict[str, str] = {}
    for kind, path in (("raw", raw_path), ("features", feature_path)):
        resolved = path.resolve()
        expected_parent = (root / kind / BASE_COIN).resolve()
        if resolved.parent != expected_parent or resolved.is_symlink():
            raise ValueError("v3 lifecycle artifact path is unsafe")
        recorded[kind] = resolved.relative_to(root).as_posix()
    return {
        "schema_version": SCHEMA_VERSION, "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION, "capture_root_name": CAPTURE_ROOT_NAME,
        "raw_codec": RAW_CODEC, "status": "PASS", "research_domain": "development_only",
        "experiment_id": policy["experiment_id"],
        "policy_canonical_sha256": canonical_sha256(policy),
        "manifest_canonical_sha256": canonical_sha256(manifest),
        "coverage": {
            "capture_started_epoch_ms": started, "capture_completed_epoch_ms": completed,
            "successful_poll_count": len(features),
        },
        "raw": {"path": recorded["raw"], "sha256": sha256_file(raw_path), "snapshot_count": len(features)},
        "features": {"path": recorded["features"], "sha256": sha256_file(feature_path), "row_count": len(features)},
        "state_transition": {
            "before_sha256": state_before_sha256, "after_sha256": canonical_sha256(state_after),
            "revision_after": int(state_after["revision"]),
            "active_lifecycle_id_after": (
                state_after["active_lifecycle"].get("lifecycle_id") if state_after.get("active_lifecycle") else None
            ),
        },
        "quality": {
            "capture_termination_reason": termination_reason,
            "partial_segment_preserved": termination_reason != "duration_complete",
            "maximum_poll_latency_ms": max(int(row["poll_latency_ms"]) for row in features),
            "maximum_tracked_observed_count": max(int(row["tracked_observed_count"]) for row in features),
            "maximum_paired_delivery_evidence_count": max(int(row["paired_delivery_evidence_count"]) for row in features),
        },
        "promotion_authority": False, "demo_activation_authorized": False,
        "live_activation_authorized": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=pathlib.Path, required=True)
    parser.add_argument("--features", type=pathlib.Path, required=True)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--state", type=pathlib.Path, required=True)
    parser.add_argument("--capture-root", type=pathlib.Path, required=True)
    parser.add_argument("--policy", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--duration-sec", type=float, default=905.0)
    parser.add_argument("--poll-interval-sec", type=float, default=60.0)
    parser.add_argument("--base-url", default=BASE_URL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration_sec < 0 or args.poll_interval_sec <= 0:
        raise ValueError("v3 lifecycle capture durations are invalid")
    policy, manifest = load_contract(args.policy, args.manifest)
    root = args.capture_root.resolve()
    if root.name != CAPTURE_ROOT_NAME or args.state.resolve().parent != root:
        raise ValueError("v3 lifecycle root or state path mismatch")
    state = load_state(args.state, policy=policy, manifest=manifest)
    state_before_sha = canonical_sha256(state)
    features, started, completed, termination, state_after = capture_live(
        raw_output=args.raw, state=state, policy=policy, manifest=manifest,
        duration_sec=args.duration_sec, poll_interval_sec=args.poll_interval_sec,
        base_url=args.base_url,
    )
    _write_feature_csv(args.features, features)
    report = build_report(
        root=root, raw_path=args.raw, feature_path=args.features, features=features,
        started=started, completed=completed, termination_reason=termination,
        policy=policy, manifest=manifest, state_before_sha256=state_before_sha,
        state_after=state_after,
    )
    atomic_write_json(args.report, report)
    atomic_write_json(args.state, state_after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
