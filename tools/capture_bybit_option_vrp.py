#!/usr/bin/env python3
"""Capture checksum-bound public Bybit BTC option observations for a no-model VRP audit."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import math
import pathlib
import ssl
import statistics
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCHEMA_VERSION = "bybit_btc_option_vrp_capture_v1"
ARTIFACT_PATH_CONTRACT = "capture_root_relative_v1"
BASE_URL = "https://api.bybit.com"
BASE_COIN = "BTC"
SETTLE_COIN = "USDT"
HEDGE_SYMBOL = "BTCUSDT"
OUTPUT_FIELDS = (
    "timestamp_epoch_ms",
    "active_contract_count",
    "two_sided_contract_count",
    "scoped_two_sided_contract_count",
    "scoped_volume_contract_count",
    "atm_pair_count",
    "new_trade_count",
    "hedge_bid",
    "hedge_ask",
    "historical_volatility_7d",
    "historical_volatility_30d",
    "atm_mark_iv_median",
    "scoped_spread_ratio_median",
    "scoped_spread_ratio_p90",
)


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


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
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def fetch_json(path: str, params: Mapping[str, Any], *, base_url: str = BASE_URL, timeout: float = 20.0) -> Dict[str, Any]:
    query = urlencode({key: value for key, value in params.items() if value is not None})
    request = Request(f"{base_url.rstrip('/')}{path}?{query}", headers={"Accept": "application/json", "User-Agent": "ai-trade-option-vrp/1"})
    fallback = pathlib.Path("/etc/ssl/cert.pem")
    paths = ssl.get_default_verify_paths()
    context = ssl.create_default_context(cafile=str(fallback)) if paths.cafile is None and fallback.is_file() else ssl.create_default_context()
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Bybit public request failed: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("retCode") != 0 or not isinstance(payload.get("result"), (dict, list)):
        raise RuntimeError(f"Bybit public request rejected: {path}: {payload.get('retCode') if isinstance(payload, dict) else 'invalid'}")
    return payload


def result_list(payload: Mapping[str, Any]) -> list[Dict[str, Any]]:
    result = payload.get("result")
    rows = result.get("list", []) if isinstance(result, Mapping) else result if isinstance(result, list) else []
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def expiry_from_symbol(symbol: str) -> int | None:
    parts = symbol.upper().split("-")
    if len(parts) < 2:
        return None
    try:
        expiry = dt.datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=dt.timezone.utc, hour=8)
    except ValueError:
        return None
    return int(expiry.timestamp() * 1000)


def normalize_snapshot(
    *,
    now_epoch_ms: int,
    instruments: Sequence[Mapping[str, Any]],
    tickers: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    hedge_ticker: Sequence[Mapping[str, Any]],
    hedge_orderbook: Mapping[str, Any],
    hv7: Sequence[Mapping[str, Any]],
    hv30: Sequence[Mapping[str, Any]],
    delivery: Sequence[Mapping[str, Any]],
    seen_exec_ids: set[str],
    minimum_dte_days: float,
    maximum_dte_days: float,
    maximum_absolute_moneyness: float,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    instrument_by_symbol = {str(row.get("symbol") or ""): dict(row) for row in instruments}
    active = [row for row in tickers if str(row.get("symbol") or "") in instrument_by_symbol]
    two_sided = [row for row in active if _float(row.get("bid1Price")) > 0.0 and _float(row.get("ask1Price")) > 0.0]
    scoped: list[Dict[str, Any]] = []
    spreads: list[float] = []
    for ticker in two_sided:
        symbol = str(ticker.get("symbol") or "")
        instrument = instrument_by_symbol[symbol]
        delivery_ms = int(_float(instrument.get("deliveryTime")))
        dte = (delivery_ms - now_epoch_ms) / 86400000.0
        index_price = _float(ticker.get("indexPrice"))
        strike = _float(instrument.get("optionsType") and instrument.get("symbol", "").split("-")[2] if len(symbol.split("-")) > 2 else 0)
        if strike <= 0.0:
            strike = _float(instrument.get("strike"))
        moneyness = strike / index_price - 1.0 if index_price > 0.0 else math.inf
        if not (minimum_dte_days <= dte <= maximum_dte_days and abs(moneyness) <= maximum_absolute_moneyness):
            continue
        bid, ask = _float(ticker.get("bid1Price")), _float(ticker.get("ask1Price"))
        midpoint = (bid + ask) / 2.0
        spread = (ask - bid) / midpoint if midpoint > 0.0 else math.inf
        row = {
            "symbol": symbol,
            "deliveryTime": delivery_ms,
            "dteDays": dte,
            "strike": strike,
            "moneyness": moneyness,
            "optionsType": str(instrument.get("optionsType") or ""),
            "tickSize": instrument.get("priceFilter", {}).get("tickSize") if isinstance(instrument.get("priceFilter"), Mapping) else None,
            "minOrderQty": instrument.get("lotSizeFilter", {}).get("minOrderQty") if isinstance(instrument.get("lotSizeFilter"), Mapping) else None,
            "deliveryFeeRate": instrument.get("deliveryFeeRate"),
            "bid1Price": ticker.get("bid1Price"),
            "bid1Size": ticker.get("bid1Size"),
            "bid1Iv": ticker.get("bid1Iv"),
            "ask1Price": ticker.get("ask1Price"),
            "ask1Size": ticker.get("ask1Size"),
            "ask1Iv": ticker.get("ask1Iv"),
            "markPrice": ticker.get("markPrice"),
            "markIv": ticker.get("markIv"),
            "indexPrice": ticker.get("indexPrice"),
            "underlyingPrice": ticker.get("underlyingPrice"),
            "openInterest": ticker.get("openInterest"),
            "volume24h": ticker.get("volume24h"),
            "turnover24h": ticker.get("turnover24h"),
            "delta": ticker.get("delta"),
            "gamma": ticker.get("gamma"),
            "vega": ticker.get("vega"),
            "theta": ticker.get("theta"),
            "spreadRatio": spread,
        }
        scoped.append(row)
        spreads.append(spread)

    selected_symbols = {row["symbol"] for row in scoped}
    new_trades: list[Dict[str, Any]] = []
    for trade in trades:
        symbol, exec_id = str(trade.get("symbol") or ""), str(trade.get("execId") or "")
        if symbol not in selected_symbols or not exec_id or exec_id in seen_exec_ids:
            continue
        seen_exec_ids.add(exec_id)
        new_trades.append(dict(trade))

    pairs: Dict[tuple[int, float], set[str]] = {}
    pair_ivs: Dict[tuple[int, float], list[float]] = {}
    for row in scoped:
        key = (int(row["deliveryTime"]), float(row["strike"]))
        pairs.setdefault(key, set()).add(str(row["optionsType"]).lower())
        iv = _float(row.get("markIv"))
        if iv > 0.0:
            pair_ivs.setdefault(key, []).append(iv)
    complete_keys = [key for key, sides in pairs.items() if {"call", "put"}.issubset(sides)]
    atm_ivs = [statistics.mean(pair_ivs[key]) for key in complete_keys if pair_ivs.get(key)]
    hedge = dict(hedge_ticker[0]) if hedge_ticker else {}
    orderbook_result = hedge_orderbook.get("result", {}) if isinstance(hedge_orderbook.get("result"), Mapping) else {}
    hv7_value = _float(hv7[0].get("value")) if hv7 else 0.0
    hv30_value = _float(hv30[0].get("value")) if hv30 else 0.0
    delivery_rows = [dict(row) for row in delivery]
    delivery_times = sorted({value for value in (expiry_from_symbol(str(row.get("symbol") or "")) for row in delivery_rows) if value})
    volume_count = sum(1 for row in scoped if _float(row.get("volume24h")) > 0.0)
    feature = {
        "timestamp_epoch_ms": now_epoch_ms,
        "active_contract_count": len(active),
        "two_sided_contract_count": len(two_sided),
        "scoped_two_sided_contract_count": len(scoped),
        "scoped_volume_contract_count": volume_count,
        "atm_pair_count": len(complete_keys),
        "new_trade_count": len(new_trades),
        "hedge_bid": _float(hedge.get("bid1Price")),
        "hedge_ask": _float(hedge.get("ask1Price")),
        "historical_volatility_7d": hv7_value,
        "historical_volatility_30d": hv30_value,
        "atm_mark_iv_median": statistics.median(atm_ivs) if atm_ivs else 0.0,
        "scoped_spread_ratio_median": statistics.median(spreads) if spreads else 0.0,
        "scoped_spread_ratio_p90": _percentile(spreads, 0.9) or 0.0,
    }
    snapshot = {
        "timestamp_epoch_ms": now_epoch_ms,
        "selection_contract": {
            "minimum_dte_days": minimum_dte_days,
            "maximum_dte_days": maximum_dte_days,
            "maximum_absolute_moneyness": maximum_absolute_moneyness,
        },
        "scoped_options": scoped,
        "new_recent_trades": new_trades,
        "hedge_ticker": hedge,
        "hedge_orderbook_l1": {"b": orderbook_result.get("b", [])[:1], "a": orderbook_result.get("a", [])[:1], "ts": orderbook_result.get("ts")},
        "historical_volatility": {"7d": hv7_value, "30d": hv30_value},
        "delivery_prices": delivery_rows,
        "delivery_times": delivery_times,
        "feature": feature,
    }
    return snapshot, feature


def _write_feature_csv(path: pathlib.Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        temporary = pathlib.Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in OUTPUT_FIELDS})
    temporary.replace(path)


def capture_live(
    *, raw_output: pathlib.Path, duration_sec: float, poll_interval_sec: float, base_url: str,
    minimum_dte_days: float, maximum_dte_days: float, maximum_absolute_moneyness: float,
    fetcher: Callable[..., Dict[str, Any]] = fetch_json,
) -> tuple[list[Dict[str, Any]], int, int, list[int]]:
    instruments_payload = fetcher("/v5/market/instruments-info", {"category": "option", "baseCoin": BASE_COIN, "limit": 1000}, base_url=base_url)
    instruments = result_list(instruments_payload)
    if not instruments:
        raise RuntimeError("Bybit returned no active BTC option instruments")
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    features: list[Dict[str, Any]] = []
    delivery_times: set[int] = set()
    seen_exec_ids: set[str] = set()
    started_epoch_ms = int(time.time() * 1000)
    deadline = time.monotonic() + duration_sec
    with gzip.open(raw_output, "wt", encoding="utf-8") as handle:
        while True:
            now_ms = int(time.time() * 1000)
            tickers = result_list(fetcher("/v5/market/tickers", {"category": "option", "baseCoin": BASE_COIN}, base_url=base_url))
            trades = result_list(fetcher("/v5/market/recent-trade", {"category": "option", "baseCoin": BASE_COIN, "limit": 1000}, base_url=base_url))
            hedge_ticker = result_list(fetcher("/v5/market/tickers", {"category": "linear", "symbol": HEDGE_SYMBOL}, base_url=base_url))
            hedge_orderbook = fetcher("/v5/market/orderbook", {"category": "linear", "symbol": HEDGE_SYMBOL, "limit": 1}, base_url=base_url)
            hv7 = result_list(fetcher("/v5/market/historical-volatility", {"category": "option", "baseCoin": BASE_COIN, "quoteCoin": SETTLE_COIN, "period": 7}, base_url=base_url))
            hv30 = result_list(fetcher("/v5/market/historical-volatility", {"category": "option", "baseCoin": BASE_COIN, "quoteCoin": SETTLE_COIN, "period": 30}, base_url=base_url))
            delivery = result_list(fetcher("/v5/market/delivery-price", {"category": "option", "baseCoin": BASE_COIN, "limit": 200}, base_url=base_url))
            snapshot, feature = normalize_snapshot(
                now_epoch_ms=now_ms, instruments=instruments, tickers=tickers, trades=trades,
                hedge_ticker=hedge_ticker, hedge_orderbook=hedge_orderbook, hv7=hv7, hv30=hv30,
                delivery=delivery, seen_exec_ids=seen_exec_ids, minimum_dte_days=minimum_dte_days,
                maximum_dte_days=maximum_dte_days, maximum_absolute_moneyness=maximum_absolute_moneyness,
            )
            handle.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
            handle.flush()
            features.append(feature)
            delivery_times.update(int(value) for value in snapshot["delivery_times"])
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            time.sleep(min(poll_interval_sec, remaining))
    completed_epoch_ms = int(time.time() * 1000)
    return features, started_epoch_ms, completed_epoch_ms, sorted(delivery_times)


def build_report(
    *, capture_root: pathlib.Path, raw_path: pathlib.Path, feature_path: pathlib.Path,
    feature_rows: Sequence[Mapping[str, Any]], started_epoch_ms: int, completed_epoch_ms: int,
    delivery_times: Sequence[int], base_url: str, poll_interval_sec: float,
    minimum_dte_days: float, maximum_dte_days: float, maximum_absolute_moneyness: float,
) -> Dict[str, Any]:
    if not feature_rows or completed_epoch_ms < started_epoch_ms:
        raise ValueError("capture must contain at least one successful poll")
    root = capture_root.resolve()
    recorded: Dict[str, str] = {}
    for kind, path in (("raw", raw_path), ("features", feature_path)):
        resolved = path.resolve()
        expected_parent = (root / kind / BASE_COIN).resolve()
        if resolved.parent != expected_parent:
            raise ValueError("artifact path escapes capture root")
        recorded[kind] = resolved.relative_to(root).as_posix()
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_path_contract": ARTIFACT_PATH_CONTRACT,
        "status": "PASS",
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "source": "bybit_public_rest_v5_option_and_linear",
        "base_url": base_url,
        "base_coin": BASE_COIN,
        "settle_coin": SETTLE_COIN,
        "hedge_symbol": HEDGE_SYMBOL,
        "selection_contract": {
            "minimum_dte_days": minimum_dte_days,
            "maximum_dte_days": maximum_dte_days,
            "maximum_absolute_moneyness": maximum_absolute_moneyness,
            "poll_interval_seconds": poll_interval_sec,
        },
        "coverage": {
            "capture_started_epoch_ms": int(started_epoch_ms),
            "capture_completed_epoch_ms": int(completed_epoch_ms),
            "duration_ms": int(max(0, completed_epoch_ms - started_epoch_ms)),
            "successful_poll_count": len(feature_rows),
        },
        "raw": {"path": recorded["raw"], "sha256": sha256_file(raw_path), "snapshot_count": len(feature_rows)},
        "features": {"path": recorded["features"], "sha256": sha256_file(feature_path), "row_count": len(feature_rows)},
        "quality": {
            "minimum_scoped_two_sided_contract_count": min(int(row["scoped_two_sided_contract_count"]) for row in feature_rows),
            "minimum_scoped_volume_contract_count": min(int(row["scoped_volume_contract_count"]) for row in feature_rows),
            "maximum_scoped_spread_ratio_p90": max(float(row["scoped_spread_ratio_p90"]) for row in feature_rows),
            "new_trade_count": sum(int(row["new_trade_count"]) for row in feature_rows),
            "delivery_times_observed": sorted({int(value) for value in delivery_times}),
        },
        "next_gate": "minimum_checksum_bound_forward_coverage_and_completed_expiries",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--duration-sec", type=float, default=905.0)
    parser.add_argument("--poll-interval-sec", type=float, default=60.0)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--minimum-dte-days", type=float, default=0.5)
    parser.add_argument("--maximum-dte-days", type=float, default=10.0)
    parser.add_argument("--maximum-absolute-moneyness", type=float, default=0.1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if min(args.duration_sec, args.poll_interval_sec, args.minimum_dte_days, args.maximum_dte_days, args.maximum_absolute_moneyness) <= 0.0:
        raise ValueError("capture durations and selection bounds must be positive")
    if args.minimum_dte_days >= args.maximum_dte_days:
        raise ValueError("minimum DTE must be below maximum DTE")
    raw_path, feature_path = pathlib.Path(args.raw), pathlib.Path(args.features)
    rows, started, completed, deliveries = capture_live(
        raw_output=raw_path, duration_sec=args.duration_sec, poll_interval_sec=args.poll_interval_sec,
        base_url=args.base_url, minimum_dte_days=args.minimum_dte_days,
        maximum_dte_days=args.maximum_dte_days, maximum_absolute_moneyness=args.maximum_absolute_moneyness,
    )
    _write_feature_csv(feature_path, rows)
    report = build_report(
        capture_root=pathlib.Path(args.capture_root), raw_path=raw_path, feature_path=feature_path,
        feature_rows=rows, started_epoch_ms=started, completed_epoch_ms=completed,
        delivery_times=deliveries, base_url=args.base_url,
        poll_interval_sec=args.poll_interval_sec,
        minimum_dte_days=args.minimum_dte_days,
        maximum_dte_days=args.maximum_dte_days,
        maximum_absolute_moneyness=args.maximum_absolute_moneyness,
    )
    atomic_write_json(pathlib.Path(args.report), report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
