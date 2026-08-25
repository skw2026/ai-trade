#!/usr/bin/env python3
"""Capture settlement-bound Bybit BTC/USDT option observations for forward payoff audits."""

from __future__ import annotations

import argparse
import json
import lzma
import pathlib
import time
from typing import Any, Callable, Dict, Mapping, Sequence

import capture_bybit_option_vrp as legacy


SCHEMA_VERSION = "bybit_btc_option_vrp_capture_v2"
SNAPSHOT_SCHEMA_VERSION = "bybit_btc_option_vrp_snapshot_v2"
ARTIFACT_PATH_CONTRACT = legacy.ARTIFACT_PATH_CONTRACT
BASE_URL = legacy.BASE_URL
BASE_COIN = "BTC"
QUOTE_COIN = "USDT"
SETTLE_COIN = "USDT"
HEDGE_SYMBOL = legacy.HEDGE_SYMBOL
CAPTURE_ROOT_NAME = "bybit_btc_option_vrp_v2"
RAW_CODEC = "xz_lzma_preset1"
OUTPUT_FIELDS = legacy.OUTPUT_FIELDS
SCOPE_CONTRACT = {
    "venue": "bybit",
    "category": "option",
    "base_coin": BASE_COIN,
    "quote_coin": QUOTE_COIN,
    "settle_coin": SETTLE_COIN,
    "hedge_category": "linear",
    "hedge_symbol": HEDGE_SYMBOL,
}
SCOPE_IDENTITY_SHA256 = legacy.canonical_sha256(SCOPE_CONTRACT)

canonical_sha256 = legacy.canonical_sha256
sha256_file = legacy.sha256_file
atomic_write_json = legacy.atomic_write_json
fetch_json = legacy.fetch_json
result_list = legacy.result_list


def _positive_float(value: Any, *, field: str) -> float:
    result = legacy._float(value)
    if result <= 0.0:
        raise ValueError(f"{field} must be positive")
    return result


def _scope_instruments(instruments: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    scoped: list[Dict[str, Any]] = []
    for raw in instruments:
        row = dict(raw)
        if (
            str(row.get("baseCoin") or "").upper() != BASE_COIN
            or str(row.get("quoteCoin") or "").upper() != QUOTE_COIN
            or str(row.get("settleCoin") or "").upper() != SETTLE_COIN
        ):
            continue
        symbol = str(row.get("symbol") or "")
        delivery_time = int(_positive_float(row.get("deliveryTime"), field=f"{symbol}.deliveryTime"))
        lot = row.get("lotSizeFilter")
        if not isinstance(lot, Mapping):
            raise ValueError(f"{symbol}.lotSizeFilter is missing")
        _positive_float(lot.get("minOrderQty"), field=f"{symbol}.minOrderQty")
        _positive_float(lot.get("qtyStep"), field=f"{symbol}.qtyStep")
        _positive_float(row.get("deliveryFeeRate"), field=f"{symbol}.deliveryFeeRate")
        if delivery_time <= 0:
            raise ValueError(f"{symbol}.deliveryTime is invalid")
        scoped.append(row)
    return scoped


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
    scoped_instruments = _scope_instruments(instruments)
    if not scoped_instruments:
        raise ValueError("no BTC/USDT/USDT option instruments")
    instrument_by_symbol = {str(row["symbol"]): row for row in scoped_instruments}
    selected_symbols = set(instrument_by_symbol)
    scoped_tickers = [row for row in tickers if str(row.get("symbol") or "") in selected_symbols]
    scoped_trades = [row for row in trades if str(row.get("symbol") or "") in selected_symbols]
    snapshot, feature = legacy.normalize_snapshot(
        now_epoch_ms=now_epoch_ms,
        instruments=scoped_instruments,
        tickers=scoped_tickers,
        trades=scoped_trades,
        hedge_ticker=hedge_ticker,
        hedge_orderbook=hedge_orderbook,
        hv7=hv7,
        hv30=hv30,
        delivery=[],
        seen_exec_ids=seen_exec_ids,
        minimum_dte_days=minimum_dte_days,
        maximum_dte_days=maximum_dte_days,
        maximum_absolute_moneyness=maximum_absolute_moneyness,
    )
    for option in snapshot["scoped_options"]:
        instrument = instrument_by_symbol[str(option["symbol"])]
        lot = instrument["lotSizeFilter"]
        option.update({
            "baseCoin": BASE_COIN,
            "quoteCoin": QUOTE_COIN,
            "settleCoin": SETTLE_COIN,
            "minOrderQty": lot["minOrderQty"],
            "qtyStep": lot["qtyStep"],
            "deliveryFeeRate": instrument["deliveryFeeRate"],
        })

    normalized_delivery: list[Dict[str, Any]] = []
    for raw in delivery:
        symbol = str(raw.get("symbol") or "")
        instrument = instrument_by_symbol.get(symbol)
        if instrument is None:
            continue
        delivery_time = int(_positive_float(raw.get("deliveryTime"), field=f"{symbol}.deliveryTime"))
        expected_time = int(_positive_float(instrument.get("deliveryTime"), field=f"{symbol}.instrumentDeliveryTime"))
        if delivery_time != expected_time:
            raise ValueError(f"delivery identity mismatch: {symbol}")
        delivery_price = _positive_float(raw.get("deliveryPrice"), field=f"{symbol}.deliveryPrice")
        normalized_delivery.append({
            "symbol": symbol,
            "deliveryPrice": str(raw.get("deliveryPrice")),
            "deliveryTime": delivery_time,
            "baseCoin": BASE_COIN,
            "quoteCoin": QUOTE_COIN,
            "settleCoin": SETTLE_COIN,
            "scopeIdentitySha256": SCOPE_IDENTITY_SHA256,
            "deliveryPriceNumeric": delivery_price,
        })
    by_expiry_strike: Dict[tuple[int, str], set[float]] = {}
    for row in normalized_delivery:
        parts = str(row["symbol"]).split("-")
        strike = parts[2] if len(parts) >= 4 else ""
        by_expiry_strike.setdefault((int(row["deliveryTime"]), strike), set()).add(float(row["deliveryPriceNumeric"]))
    if any(len(values) != 1 for values in by_expiry_strike.values()):
        raise ValueError("call/put delivery price mismatch")

    snapshot.update({
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "scope_contract": dict(SCOPE_CONTRACT),
        "scope_identity_sha256": SCOPE_IDENTITY_SHA256,
        "delivery_query_status": "PASS",
        "delivery_prices": normalized_delivery,
        "delivery_times": sorted({int(row["deliveryTime"]) for row in normalized_delivery}),
    })
    snapshot["selection_contract"].update({
        "scope_identity_sha256": SCOPE_IDENTITY_SHA256,
        "settle_coin": SETTLE_COIN,
    })
    return snapshot, feature


def capture_live(
    *,
    raw_output: pathlib.Path,
    duration_sec: float,
    poll_interval_sec: float,
    base_url: str,
    minimum_dte_days: float,
    maximum_dte_days: float,
    maximum_absolute_moneyness: float,
    fetcher: Callable[..., Dict[str, Any]] = fetch_json,
) -> tuple[list[Dict[str, Any]], int, int, list[int]]:
    instruments_payload = fetcher(
        "/v5/market/instruments-info",
        {"category": "option", "baseCoin": BASE_COIN, "limit": 1000},
        base_url=base_url,
    )
    instruments = result_list(instruments_payload)
    if not _scope_instruments(instruments):
        raise RuntimeError("Bybit returned no active BTC/USDT/USDT option instruments")
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    features: list[Dict[str, Any]] = []
    delivery_times: set[int] = set()
    seen_exec_ids: set[str] = set()
    started_epoch_ms = int(time.time() * 1000)
    deadline = time.monotonic() + duration_sec
    if raw_output.suffix != ".xz":
        raise ValueError("v2 raw output must use the frozen XZ codec")
    with lzma.open(raw_output, "wt", encoding="utf-8", preset=1) as handle:
        while True:
            now_ms = int(time.time() * 1000)
            tickers = result_list(fetcher("/v5/market/tickers", {"category": "option", "baseCoin": BASE_COIN}, base_url=base_url))
            trades = result_list(fetcher("/v5/market/recent-trade", {"category": "option", "baseCoin": BASE_COIN, "limit": 1000}, base_url=base_url))
            hedge_ticker = result_list(fetcher("/v5/market/tickers", {"category": "linear", "symbol": HEDGE_SYMBOL}, base_url=base_url))
            hedge_orderbook = fetcher("/v5/market/orderbook", {"category": "linear", "symbol": HEDGE_SYMBOL, "limit": 1}, base_url=base_url)
            hv7 = result_list(fetcher("/v5/market/historical-volatility", {"category": "option", "baseCoin": BASE_COIN, "quoteCoin": QUOTE_COIN, "period": 7}, base_url=base_url))
            hv30 = result_list(fetcher("/v5/market/historical-volatility", {"category": "option", "baseCoin": BASE_COIN, "quoteCoin": QUOTE_COIN, "period": 30}, base_url=base_url))
            delivery = result_list(fetcher(
                "/v5/market/delivery-price",
                {"category": "option", "baseCoin": BASE_COIN, "settleCoin": SETTLE_COIN, "limit": 200},
                base_url=base_url,
            ))
            snapshot, feature = normalize_snapshot(
                now_epoch_ms=now_ms,
                instruments=instruments,
                tickers=tickers,
                trades=trades,
                hedge_ticker=hedge_ticker,
                hedge_orderbook=hedge_orderbook,
                hv7=hv7,
                hv30=hv30,
                delivery=delivery,
                seen_exec_ids=seen_exec_ids,
                minimum_dte_days=minimum_dte_days,
                maximum_dte_days=maximum_dte_days,
                maximum_absolute_moneyness=maximum_absolute_moneyness,
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
    *,
    capture_root: pathlib.Path,
    raw_path: pathlib.Path,
    feature_path: pathlib.Path,
    feature_rows: Sequence[Mapping[str, Any]],
    started_epoch_ms: int,
    completed_epoch_ms: int,
    delivery_times: Sequence[int],
    base_url: str,
    poll_interval_sec: float,
    minimum_dte_days: float,
    maximum_dte_days: float,
    maximum_absolute_moneyness: float,
) -> Dict[str, Any]:
    root = capture_root.resolve()
    if root.name != CAPTURE_ROOT_NAME:
        raise ValueError(f"v2 capture root must end with {CAPTURE_ROOT_NAME}")
    report = legacy.build_report(
        capture_root=root,
        raw_path=raw_path,
        feature_path=feature_path,
        feature_rows=feature_rows,
        started_epoch_ms=started_epoch_ms,
        completed_epoch_ms=completed_epoch_ms,
        delivery_times=delivery_times,
        base_url=base_url,
        poll_interval_sec=poll_interval_sec,
        minimum_dte_days=minimum_dte_days,
        maximum_dte_days=maximum_dte_days,
        maximum_absolute_moneyness=maximum_absolute_moneyness,
    )
    report.update({
        "schema_version": SCHEMA_VERSION,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "scope_contract": dict(SCOPE_CONTRACT),
        "scope_identity_sha256": SCOPE_IDENTITY_SHA256,
        "capture_root_name": CAPTURE_ROOT_NAME,
        "raw_codec": RAW_CODEC,
    })
    report["selection_contract"].update({
        "scope_identity_sha256": SCOPE_IDENTITY_SHA256,
        "settle_coin": SETTLE_COIN,
    })
    report["quality"].update({
        "delivery_query_status": "PASS",
        "delivery_identity_contract": "symbol_delivery_time_settle_coin_v2",
    })
    report["next_gate"] = "frozen_option_vrp_sequential_payoff_contract"
    return report


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
    root = pathlib.Path(args.capture_root)
    raw_path, feature_path = pathlib.Path(args.raw), pathlib.Path(args.features)
    rows, started, completed, deliveries = capture_live(
        raw_output=raw_path,
        duration_sec=args.duration_sec,
        poll_interval_sec=args.poll_interval_sec,
        base_url=args.base_url,
        minimum_dte_days=args.minimum_dte_days,
        maximum_dte_days=args.maximum_dte_days,
        maximum_absolute_moneyness=args.maximum_absolute_moneyness,
    )
    legacy._write_feature_csv(feature_path, rows)
    report = build_report(
        capture_root=root,
        raw_path=raw_path,
        feature_path=feature_path,
        feature_rows=rows,
        started_epoch_ms=started,
        completed_epoch_ms=completed,
        delivery_times=deliveries,
        base_url=args.base_url,
        poll_interval_sec=args.poll_interval_sec,
        minimum_dte_days=args.minimum_dte_days,
        maximum_dte_days=args.maximum_dte_days,
        maximum_absolute_moneyness=args.maximum_absolute_moneyness,
    )
    atomic_write_json(pathlib.Path(args.report), report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
