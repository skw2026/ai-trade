#!/usr/bin/env python3
"""
Diagnose raw strategy edge before blaming live execution.

The closed-loop pipeline already knows whether replay/runtime produced fills.
This tool answers the earlier question: did the trend signal itself have enough
forward edge after round-trip costs, and did the price path offer MFE that the
current exit logic failed to capture?
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


MIN_TREND_ABS_EMA_DIFF = 5e-4
MIN_TREND_ABS_MOM_48 = 2e-3
MIN_EXTREME_VOL_12 = 1.5e-3
MIN_EXTREME_RANGE_PCT = 3e-3
LOW_CAPTURE_RATIO = 0.10


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_float(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_symbol(value: str, fallback: str = "SOURCE") -> str:
    symbol = str(value or "").strip().upper()
    return symbol or fallback


def parse_horizons(raw: str, default_horizon: int) -> List[int]:
    values: List[int] = []
    for part in str(raw or "").replace(";", ",").split(","):
        item = part.strip()
        if not item:
            continue
        horizon = safe_int(item, 0)
        if horizon > 0 and horizon not in values:
            values.append(horizon)
    if not values:
        values.append(max(1, int(default_horizon)))
    return values


def quantile(values: Iterable[float], q: float, default: float) -> float:
    clean = sorted(float(item) for item in values if math.isfinite(float(item)))
    if not clean:
        return default
    if len(clean) == 1:
        return clean[0]
    pos = max(0.0, min(1.0, float(q))) * float(len(clean) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return clean[lo]
    frac = pos - float(lo)
    return clean[lo] * (1.0 - frac) + clean[hi] * frac


def mean(values: List[float]) -> float | None:
    clean = [item for item in values if math.isfinite(item)]
    if not clean:
        return None
    return sum(clean) / float(len(clean))


def median(values: List[float]) -> float | None:
    clean = sorted(item for item in values if math.isfinite(item))
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def percentile(values: List[float], q: float) -> float | None:
    clean = [item for item in values if math.isfinite(item)]
    if not clean:
        return None
    return quantile(clean, q, clean[0])


def parse_mapping(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in str(raw or "").split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        symbol, path = item.split("=", 1)
        symbol = normalize_symbol(symbol, "")
        path = path.strip()
        if symbol and path:
            out[symbol] = path
    return out


def parse_path_entries(entries: List[str], default_symbol: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    plain_index = 0
    for raw in entries:
        item = str(raw or "").strip()
        if not item:
            continue
        if "=" in item:
            symbol, path = item.split("=", 1)
            symbol = normalize_symbol(symbol, "")
            if symbol and path.strip():
                out[symbol] = path.strip()
            continue
        symbol = default_symbol if plain_index == 0 else f"{default_symbol}_{plain_index}"
        out[normalize_symbol(symbol)] = item
        plain_index += 1
    return out


def infer_ohlcv_path(feature_path: str) -> str:
    path = Path(feature_path)
    sibling = path.with_name("ohlcv_5m.csv")
    return str(sibling) if sibling.is_file() else ""


def load_feature_rows(path: Path) -> List[Dict[str, float]]:
    rows: List[Dict[str, float]] = []
    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for raw in reader:
            timestamp = safe_int(raw.get("timestamp"), -1)
            if timestamp < 0:
                continue
            close = safe_float(raw.get("close"))
            ema_diff = safe_float(raw.get("ema_diff"))
            mom_48 = safe_float(raw.get("mom_48"))
            forward_return = safe_float(raw.get("forward_return"))
            if not all(math.isfinite(x) for x in (close, ema_diff, mom_48, forward_return)):
                continue
            rows.append(
                {
                    "timestamp": float(timestamp),
                    "close": close,
                    "ret_1": safe_float(raw.get("ret_1"), math.nan),
                    "ret_3": safe_float(raw.get("ret_3"), math.nan),
                    "ret_12": safe_float(raw.get("ret_12"), math.nan),
                    "ema_diff": ema_diff,
                    "mom_12": safe_float(raw.get("mom_12"), math.nan),
                    "mom_48": mom_48,
                    "vol_12": safe_float(raw.get("vol_12"), math.nan),
                    "range_pct": safe_float(raw.get("range_pct"), math.nan),
                    "zscore_48": safe_float(raw.get("zscore_48"), math.nan),
                    "forward_return": forward_return,
                }
            )
    return rows


def load_ohlcv_rows(path: Path) -> List[Dict[str, float]]:
    rows: List[Dict[str, float]] = []
    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for raw in reader:
            timestamp = safe_int(raw.get("timestamp"), -1)
            close = safe_float(raw.get("close"))
            high = safe_float(raw.get("high"))
            low = safe_float(raw.get("low"))
            if timestamp < 0 or not all(math.isfinite(x) for x in (close, high, low)):
                continue
            rows.append(
                {
                    "timestamp": float(timestamp),
                    "close": close,
                    "high": high,
                    "low": low,
                }
            )
    rows.sort(key=lambda item: item["timestamp"])
    return rows


def derive_thresholds(rows: List[Dict[str, float]]) -> Dict[str, float]:
    return {
        "trend_abs_ema_diff": max(
            MIN_TREND_ABS_EMA_DIFF,
            quantile((abs(row["ema_diff"]) for row in rows), 0.65, MIN_TREND_ABS_EMA_DIFF),
        ),
        "trend_abs_mom_48": max(
            MIN_TREND_ABS_MOM_48,
            quantile((abs(row["mom_48"]) for row in rows), 0.65, MIN_TREND_ABS_MOM_48),
        ),
        "extreme_vol_12": max(
            MIN_EXTREME_VOL_12,
            quantile((row["vol_12"] for row in rows), 0.90, MIN_EXTREME_VOL_12),
        ),
        "extreme_range_pct": max(
            MIN_EXTREME_RANGE_PCT,
            quantile((row["range_pct"] for row in rows), 0.90, MIN_EXTREME_RANGE_PCT),
        ),
    }


def classify_row(row: Dict[str, float], thresholds: Dict[str, float]) -> Tuple[str, int, float]:
    ema_diff = row["ema_diff"]
    mom_48 = row["mom_48"]
    vol_12 = row.get("vol_12", math.nan)
    range_pct = row.get("range_pct", math.nan)
    if (
        math.isfinite(vol_12)
        and vol_12 >= thresholds["extreme_vol_12"]
    ) or (
        math.isfinite(range_pct)
        and range_pct >= thresholds["extreme_range_pct"]
    ):
        return "extreme", 0, 0.0
    if ema_diff * mom_48 <= 0.0:
        return "range", 0, 0.0
    direction = 1 if ema_diff > 0.0 and mom_48 > 0.0 else -1
    ema_ratio = abs(ema_diff) / max(thresholds["trend_abs_ema_diff"], 1e-12)
    mom_ratio = abs(mom_48) / max(thresholds["trend_abs_mom_48"], 1e-12)
    trend_ratio = min(ema_ratio, mom_ratio)
    if trend_ratio >= 1.0:
        return "trend", direction, trend_ratio
    return "range", direction, trend_ratio


def raw_trend_direction(row: Dict[str, float]) -> int:
    ema_diff = row["ema_diff"]
    mom_48 = row["mom_48"]
    if ema_diff > 0.0 and mom_48 > 0.0:
        return 1
    if ema_diff < 0.0 and mom_48 < 0.0:
        return -1
    return 0


def numeric_direction(value: float, min_abs: float) -> int:
    if not math.isfinite(value) or abs(value) < float(min_abs):
        return 0
    return 1 if value > 0.0 else -1


def candidate_base_direction(
    row: Dict[str, float],
    *,
    classified_direction: int,
    raw_direction: int,
    spec: Dict[str, Any],
) -> int:
    source = str(spec.get("signal_source", "trend")).strip().lower()
    min_abs = float(spec.get("min_signal_abs", 0.0))
    if source == "trend":
        return classified_direction if classified_direction else raw_direction
    if source == "raw_trend":
        return raw_direction
    if source in {"ret_1", "ret_3", "ret_12", "mom_12", "mom_48", "zscore_48"}:
        return numeric_direction(safe_float(row.get(source), math.nan), min_abs)
    return 0


def close_forward_return(
    row: Dict[str, float],
    ohlcv_rows: List[Dict[str, float]],
    index_by_timestamp: Dict[int, int],
    horizon_bars: int,
) -> float | None:
    idx = index_by_timestamp.get(int(row["timestamp"]))
    if idx is None:
        return None
    end_idx = idx + max(1, int(horizon_bars))
    if end_idx >= len(ohlcv_rows):
        return None
    entry_close = ohlcv_rows[idx]["close"]
    future_close = ohlcv_rows[end_idx]["close"]
    if entry_close <= 0.0 or future_close <= 0.0:
        return None
    return future_close / entry_close - 1.0


def path_metrics(
    row: Dict[str, float],
    direction: int,
    ohlcv_rows: List[Dict[str, float]],
    index_by_timestamp: Dict[int, int],
    forward_bars: int,
) -> Dict[str, float]:
    if direction == 0 or not ohlcv_rows:
        return {}
    idx = index_by_timestamp.get(int(row["timestamp"]))
    if idx is None:
        return {}
    end = min(len(ohlcv_rows), idx + 1 + max(1, int(forward_bars)))
    future = ohlcv_rows[idx + 1 : end]
    if not future:
        return {}
    entry_close = ohlcv_rows[idx]["close"]
    if entry_close <= 0.0:
        return {}
    max_high = max(item["high"] for item in future)
    min_low = min(item["low"] for item in future)
    if direction > 0:
        mfe_bps = (max_high / entry_close - 1.0) * 10000.0
        mae_bps = (min_low / entry_close - 1.0) * 10000.0
    else:
        mfe_bps = (entry_close / min_low - 1.0) * 10000.0 if min_low > 0.0 else math.nan
        mae_bps = (entry_close / max_high - 1.0) * 10000.0 if max_high > 0.0 else math.nan
    return {"path_mfe_bps": mfe_bps, "path_mae_bps": mae_bps}


def build_directional_record(
    row: Dict[str, float],
    *,
    bucket: str,
    direction: int,
    trend_ratio: float,
    horizon_bars: int,
    configured_forward_bars: int,
    round_trip_cost_bps: float,
    ohlcv_rows: List[Dict[str, float]],
    index_by_timestamp: Dict[int, int],
) -> Dict[str, float] | None:
    if direction == 0:
        return None
    forward_return = close_forward_return(
        row,
        ohlcv_rows,
        index_by_timestamp,
        horizon_bars,
    )
    if forward_return is None:
        if int(horizon_bars) != int(configured_forward_bars):
            return None
        forward_return = row["forward_return"]
    gross_forward_bps = float(direction) * float(forward_return) * 10000.0
    record = {
        "timestamp": row["timestamp"],
        "bucket": bucket,
        "direction": float(direction),
        "trend_ratio": trend_ratio,
        "horizon_bars": float(horizon_bars),
        "gross_forward_bps": gross_forward_bps,
        "net_forward_bps": gross_forward_bps - round_trip_cost_bps,
    }
    record.update(
        path_metrics(
            row,
            direction,
            ohlcv_rows,
            index_by_timestamp,
            horizon_bars,
        )
    )
    path_mfe = record.get("path_mfe_bps", math.nan)
    if math.isfinite(path_mfe) and path_mfe > 0.0:
        record["gross_capture_of_path_mfe"] = max(0.0, gross_forward_bps) / path_mfe
    return record


def summarize_records(records: List[Dict[str, float]], round_trip_cost_bps: float) -> Dict[str, Any]:
    gross = [item["gross_forward_bps"] for item in records]
    net = [item["net_forward_bps"] for item in records]
    mfe = [item["path_mfe_bps"] for item in records if math.isfinite(item.get("path_mfe_bps", math.nan))]
    mae = [item["path_mae_bps"] for item in records if math.isfinite(item.get("path_mae_bps", math.nan))]
    captures = [
        item["gross_capture_of_path_mfe"]
        for item in records
        if math.isfinite(item.get("gross_capture_of_path_mfe", math.nan))
    ]
    net_positive = [item for item in net if item > 0.0]
    mean_mfe = mean(mfe)
    median_mfe = median(mfe)
    return {
        "sample_count": len(records),
        "mean_gross_forward_bps": mean(gross),
        "median_gross_forward_bps": median(gross),
        "mean_net_forward_bps": mean(net),
        "median_net_forward_bps": median(net),
        "p25_net_forward_bps": percentile(net, 0.25),
        "p75_net_forward_bps": percentile(net, 0.75),
        "positive_net_ratio": (len(net_positive) / float(len(net))) if net else None,
        "mean_path_mfe_bps": mean_mfe,
        "median_path_mfe_bps": median_mfe,
        "mean_path_mae_bps": mean(mae),
        "median_path_mae_bps": median(mae),
        "mean_mfe_cost_coverage_ratio": (
            mean_mfe / round_trip_cost_bps
            if mean_mfe is not None and round_trip_cost_bps > 0.0
            else None
        ),
        "median_mfe_cost_coverage_ratio": (
            median_mfe / round_trip_cost_bps
            if median_mfe is not None and round_trip_cost_bps > 0.0
            else None
        ),
        "mean_gross_capture_of_path_mfe": mean(captures),
        "median_gross_capture_of_path_mfe": median(captures),
    }


def candidate_status(
    summary: Dict[str, Any],
    *,
    min_samples: int,
    min_mean_net_edge_bps: float,
    min_positive_net_ratio: float,
) -> Tuple[str, List[str]]:
    fail_reasons: List[str] = []
    sample_count = int(summary.get("sample_count") or 0)
    mean_net = summary.get("mean_net_forward_bps")
    positive_ratio = summary.get("positive_net_ratio")
    if sample_count < int(min_samples):
        fail_reasons.append(f"sample_count={sample_count} < min_samples={min_samples}")
    if not isinstance(mean_net, (int, float)) or float(mean_net) <= float(min_mean_net_edge_bps):
        fail_reasons.append(
            "mean_net_forward_bps "
            f"{mean_net} <= min_mean_net_edge_bps={float(min_mean_net_edge_bps)}"
        )
    if not isinstance(positive_ratio, (int, float)) or float(positive_ratio) < float(min_positive_net_ratio):
        fail_reasons.append(
            "positive_net_ratio "
            f"{positive_ratio} < min_positive_net_ratio={float(min_positive_net_ratio)}"
        )
    return ("pass" if not fail_reasons else "fail", fail_reasons)


def alpha_candidate_rank(candidate: Dict[str, Any]) -> Tuple[int, float, float, int]:
    summary = candidate.get("summary", {})
    mean_net = summary.get("mean_net_forward_bps")
    positive_ratio = summary.get("positive_net_ratio")
    sample_count = int(summary.get("sample_count") or 0)
    return (
        1 if candidate.get("status") == "pass" else 0,
        float(mean_net) if isinstance(mean_net, (int, float)) else float("-inf"),
        float(positive_ratio) if isinstance(positive_ratio, (int, float)) else float("-inf"),
        sample_count,
    )


def build_alpha_tournament(
    symbol: str,
    feature_rows: List[Dict[str, float]],
    thresholds: Dict[str, float],
    ohlcv_rows: List[Dict[str, float]],
    index_by_timestamp: Dict[int, int],
    *,
    forward_bars: int,
    tournament_horizons: List[int],
    round_trip_cost_bps: float,
    candidate_trend_ratio: float,
    confirmed_trend_ratio: float,
    min_samples: int,
    min_mean_net_edge_bps: float,
    min_positive_net_ratio: float,
) -> Dict[str, Any]:
    specs: List[Dict[str, Any]] = [
        {
            "name": "confirmed_trend_follow",
            "signal_source": "trend",
            "target_bucket": "trend",
            "direction_mode": "follow",
            "min_trend_ratio": confirmed_trend_ratio,
            "description": "当前 confirmed trend 顺势入场",
        },
        {
            "name": "confirmed_trend_inverse",
            "signal_source": "trend",
            "target_bucket": "trend",
            "direction_mode": "inverse",
            "min_trend_ratio": confirmed_trend_ratio,
            "description": "confirmed trend 反向入场，用于发现反预测",
        },
        {
            "name": "candidate_trend_follow",
            "signal_source": "trend",
            "target_bucket": "trend_or_candidate",
            "direction_mode": "follow",
            "min_trend_ratio": candidate_trend_ratio,
            "description": "更早的 candidate trend 顺势入场",
        },
        {
            "name": "candidate_trend_inverse",
            "signal_source": "trend",
            "target_bucket": "trend_or_candidate",
            "direction_mode": "inverse",
            "min_trend_ratio": candidate_trend_ratio,
            "description": "更早的 candidate trend 反向入场",
        },
        {
            "name": "range_weak_trend_follow",
            "signal_source": "raw_trend",
            "target_bucket": "range",
            "direction_mode": "follow",
            "min_trend_ratio": 0.0,
            "description": "RANGE 内弱趋势顺势",
        },
        {
            "name": "range_weak_trend_reversal",
            "signal_source": "raw_trend",
            "target_bucket": "range",
            "direction_mode": "inverse",
            "min_trend_ratio": 0.0,
            "description": "RANGE 内弱趋势反转",
        },
        {
            "name": "all_directional_follow",
            "signal_source": "raw_trend",
            "target_bucket": "all",
            "direction_mode": "follow",
            "min_trend_ratio": 0.0,
            "description": "所有同向信号样本",
        },
        {
            "name": "all_directional_inverse",
            "signal_source": "raw_trend",
            "target_bucket": "all",
            "direction_mode": "inverse",
            "min_trend_ratio": 0.0,
            "description": "所有同向信号反向样本",
        },
        {
            "name": "momentum_12_follow",
            "signal_source": "mom_12",
            "target_bucket": "all",
            "direction_mode": "follow",
            "min_trend_ratio": 0.0,
            "min_signal_abs": 5e-4,
            "description": "12-bar momentum 顺势候选",
        },
        {
            "name": "momentum_12_reversal",
            "signal_source": "mom_12",
            "target_bucket": "all",
            "direction_mode": "inverse",
            "min_trend_ratio": 0.0,
            "min_signal_abs": 5e-4,
            "description": "12-bar momentum 反转候选",
        },
        {
            "name": "micro_ret3_follow",
            "signal_source": "ret_3",
            "target_bucket": "all",
            "direction_mode": "follow",
            "min_trend_ratio": 0.0,
            "min_signal_abs": 2e-4,
            "description": "3-bar 短动量顺势候选",
        },
        {
            "name": "micro_ret3_reversal",
            "signal_source": "ret_3",
            "target_bucket": "all",
            "direction_mode": "inverse",
            "min_trend_ratio": 0.0,
            "min_signal_abs": 2e-4,
            "description": "3-bar 短动量反转候选",
        },
        {
            "name": "range_zscore_follow",
            "signal_source": "zscore_48",
            "target_bucket": "range",
            "direction_mode": "follow",
            "min_trend_ratio": 0.0,
            "min_signal_abs": 0.75,
            "description": "RANGE 内 zscore 突破延续候选",
        },
        {
            "name": "range_zscore_reversal",
            "signal_source": "zscore_48",
            "target_bucket": "range",
            "direction_mode": "inverse",
            "min_trend_ratio": 0.0,
            "min_signal_abs": 0.75,
            "description": "RANGE 内 zscore 均值回归候选",
        },
        {
            "name": "breakout_zscore_follow",
            "signal_source": "zscore_48",
            "target_bucket": "all",
            "direction_mode": "follow",
            "min_trend_ratio": 0.0,
            "min_signal_abs": 1.25,
            "description": "全市场 zscore 突破延续候选",
        },
        {
            "name": "breakout_zscore_reversal",
            "signal_source": "zscore_48",
            "target_bucket": "all",
            "direction_mode": "inverse",
            "min_trend_ratio": 0.0,
            "min_signal_abs": 1.25,
            "description": "全市场 zscore 极值反转候选",
        },
    ]

    candidates: List[Dict[str, Any]] = []
    for horizon in tournament_horizons:
        for spec in specs:
            records: List[Dict[str, float]] = []
            for row in feature_rows:
                bucket, classified_direction, trend_ratio = classify_row(row, thresholds)
                raw_direction = raw_trend_direction(row)
                if raw_direction == 0:
                    continue
                target_bucket = str(spec["target_bucket"])
                min_trend_ratio = float(spec["min_trend_ratio"])
                if target_bucket == "trend" and bucket != "trend":
                    continue
                if target_bucket == "range" and bucket != "range":
                    continue
                if target_bucket == "trend_or_candidate" and trend_ratio < min_trend_ratio:
                    continue
                if target_bucket in {"trend", "range"} and trend_ratio < min_trend_ratio:
                    continue
                base_direction = candidate_base_direction(
                    row,
                    classified_direction=classified_direction,
                    raw_direction=raw_direction,
                    spec=spec,
                )
                if base_direction == 0:
                    continue
                direction = base_direction
                if str(spec["direction_mode"]) == "inverse":
                    direction = -direction
                record = build_directional_record(
                    row,
                    bucket=bucket,
                    direction=direction,
                    trend_ratio=trend_ratio,
                    horizon_bars=horizon,
                    configured_forward_bars=forward_bars,
                    round_trip_cost_bps=round_trip_cost_bps,
                    ohlcv_rows=ohlcv_rows,
                    index_by_timestamp=index_by_timestamp,
                )
                if record is not None:
                    records.append(record)
            summary = summarize_records(records, round_trip_cost_bps)
            status, fail_reasons = candidate_status(
                summary,
                min_samples=min_samples,
                min_mean_net_edge_bps=min_mean_net_edge_bps,
                min_positive_net_ratio=min_positive_net_ratio,
            )
            candidate_name = f"{spec['name']}_h{horizon}"
            candidates.append(
                {
                    "name": candidate_name,
                    "symbol": symbol,
                    "base_name": spec["name"],
                    "description": spec["description"],
                    "horizon_bars": int(horizon),
                    "target_bucket": spec["target_bucket"],
                    "direction_mode": spec["direction_mode"],
                    "min_trend_ratio": min_trend_ratio,
                    "status": status,
                    "fail_reasons": fail_reasons,
                    "summary": summary,
                    "deployable_config": {
                        "symbol": symbol,
                        "signal_variant": spec["name"],
                        "direction_mode": spec["direction_mode"],
                        "signal_source": spec.get("signal_source", "trend"),
                        "target_bucket": spec["target_bucket"],
                        "predict_horizon_bars": int(horizon),
                        "min_trend_ratio": min_trend_ratio,
                        "min_signal_abs": float(spec.get("min_signal_abs", 0.0)),
                        "round_trip_cost_bps": float(round_trip_cost_bps),
                    },
                }
            )

    ranked = sorted(candidates, key=alpha_candidate_rank, reverse=True)
    pass_candidates = [item for item in ranked if item.get("status") == "pass"]
    return {
        "schema_version": "alpha_viability_tournament_v1",
        "status": "pass" if pass_candidates else "fail",
        "symbol": symbol,
        "candidate_count": len(candidates),
        "pass_candidate_count": len(pass_candidates),
        "horizons": [int(item) for item in tournament_horizons],
        "thresholds": {
            "min_samples": int(min_samples),
            "min_mean_net_edge_bps": float(min_mean_net_edge_bps),
            "min_positive_net_ratio": float(min_positive_net_ratio),
        },
        "best_candidate": ranked[0] if ranked else None,
        "pass_candidates": pass_candidates[:10],
        "ranked_candidates": ranked[:20],
        "fail_reasons": []
        if pass_candidates
        else ["no_alpha_candidate_positive_after_cost"],
    }


def build_cost_views(
    *,
    primary_cost_bps: float,
    maker_cost_bps: float | None,
    stress_cost_multiplier: float,
    stress_cost_bps: float | None,
) -> List[Dict[str, Any]]:
    views: List[Dict[str, Any]] = []

    def add(name: str, cost: float, description: str) -> None:
        if not math.isfinite(cost) or cost <= 0.0:
            return
        for view in views:
            if abs(float(view["round_trip_cost_bps"]) - float(cost)) < 1e-9:
                aliases = view.setdefault("aliases", [])
                if name not in aliases:
                    aliases.append(name)
                return
        views.append(
            {
                "name": name,
                "round_trip_cost_bps": float(cost),
                "description": description,
            }
        )

    add("primary", primary_cost_bps, "主链标签/安全成本口径")
    if maker_cost_bps is not None and math.isfinite(maker_cost_bps) and maker_cost_bps > 0.0:
        add("maker", maker_cost_bps, "maker-first 真实执行成本口径")
        stress_cost = (
            float(stress_cost_bps)
            if stress_cost_bps is not None and math.isfinite(float(stress_cost_bps))
            else float(maker_cost_bps) * max(1.0, float(stress_cost_multiplier))
        )
        add("maker_stress", stress_cost, "maker-first 压力成本口径")
    return views


def combine_alpha_tournaments(symbol_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    horizons: List[int] = []
    for item in symbol_items:
        tournament = item.get("alpha_tournament", {})
        if not isinstance(tournament, dict):
            continue
        for horizon in tournament.get("horizons", []):
            horizon_int = safe_int(horizon, 0)
            if horizon_int > 0 and horizon_int not in horizons:
                horizons.append(horizon_int)
        for candidate in tournament.get("ranked_candidates", []):
            if isinstance(candidate, dict):
                candidates.append(candidate)
    ranked = sorted(candidates, key=alpha_candidate_rank, reverse=True)
    pass_candidates = [item for item in ranked if item.get("status") == "pass"]
    return {
        "schema_version": "alpha_viability_tournament_v1",
        "status": "pass" if pass_candidates else "fail",
        "candidate_count": len(candidates),
        "pass_candidate_count": len(pass_candidates),
        "horizons": sorted(horizons),
        "best_candidate": ranked[0] if ranked else None,
        "pass_candidates": pass_candidates[:10],
        "ranked_candidates": ranked[:20],
        "fail_reasons": []
        if pass_candidates
        else ["no_alpha_candidate_positive_after_cost"],
    }


def combine_alpha_tournament_cost_views(
    symbol_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    by_view: Dict[str, List[Dict[str, Any]]] = {}
    view_meta: Dict[str, Dict[str, Any]] = {}
    for item in symbol_items:
        cost_views = item.get("alpha_tournament_by_cost", {})
        if not isinstance(cost_views, dict):
            continue
        for name, tournament in cost_views.items():
            if not isinstance(tournament, dict):
                continue
            by_view.setdefault(str(name), []).append({"alpha_tournament": tournament})
            view_meta[str(name)] = {
                "round_trip_cost_bps": tournament.get("round_trip_cost_bps"),
                "description": tournament.get("cost_description"),
            }

    combined: Dict[str, Any] = {}
    for name, items in sorted(by_view.items()):
        tournament = combine_alpha_tournaments(items)
        tournament["cost_view"] = name
        tournament["round_trip_cost_bps"] = view_meta.get(name, {}).get(
            "round_trip_cost_bps"
        )
        tournament["cost_description"] = view_meta.get(name, {}).get("description")
        combined[name] = tournament
    return combined


def diagnose_symbol(
    symbol: str,
    feature_path: Path,
    ohlcv_path: Path | None,
    *,
    forward_bars: int,
    round_trip_cost_bps: float,
    candidate_trend_ratio: float,
    confirmed_trend_ratio: float,
    tournament_horizons: List[int],
    min_samples: int,
    min_mean_net_edge_bps: float,
    min_positive_net_ratio: float,
    cost_views: List[Dict[str, Any]],
) -> Dict[str, Any]:
    feature_rows = load_feature_rows(feature_path)
    thresholds = derive_thresholds(feature_rows)
    ohlcv_rows: List[Dict[str, float]] = []
    index_by_timestamp: Dict[int, int] = {}
    if ohlcv_path is not None and ohlcv_path.is_file():
        ohlcv_rows = load_ohlcv_rows(ohlcv_path)
        index_by_timestamp = {
            int(row["timestamp"]): index for index, row in enumerate(ohlcv_rows)
        }

    all_records: List[Dict[str, float]] = []
    candidate_records: List[Dict[str, float]] = []
    confirmed_records: List[Dict[str, float]] = []
    direction_counts = {"long": 0, "short": 0}
    bucket_counts = {"trend": 0, "range": 0, "extreme": 0}

    for row in feature_rows:
        bucket, direction, trend_ratio = classify_row(row, thresholds)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        gross_forward_bps = direction * row["forward_return"] * 10000.0 if direction else 0.0
        record = {
            "timestamp": row["timestamp"],
            "bucket": bucket,
            "direction": float(direction),
            "trend_ratio": trend_ratio,
            "gross_forward_bps": gross_forward_bps,
            "net_forward_bps": gross_forward_bps - round_trip_cost_bps if direction else 0.0,
        }
        record.update(
            path_metrics(
                row,
                direction,
                ohlcv_rows,
                index_by_timestamp,
                forward_bars,
            )
        )
        path_mfe = record.get("path_mfe_bps", math.nan)
        if math.isfinite(path_mfe) and path_mfe > 0.0:
            record["gross_capture_of_path_mfe"] = max(0.0, gross_forward_bps) / path_mfe
        all_records.append(record)
        if direction > 0:
            direction_counts["long"] += 1
        elif direction < 0:
            direction_counts["short"] += 1
        if direction and trend_ratio >= candidate_trend_ratio:
            candidate_records.append(record)
        if direction and trend_ratio >= confirmed_trend_ratio and bucket == "trend":
            confirmed_records.append(record)

    primary_tournament = build_alpha_tournament(
        symbol,
        feature_rows,
        thresholds,
        ohlcv_rows,
        index_by_timestamp,
        forward_bars=forward_bars,
        tournament_horizons=tournament_horizons,
        round_trip_cost_bps=round_trip_cost_bps,
        candidate_trend_ratio=candidate_trend_ratio,
        confirmed_trend_ratio=confirmed_trend_ratio,
        min_samples=min_samples,
        min_mean_net_edge_bps=min_mean_net_edge_bps,
        min_positive_net_ratio=min_positive_net_ratio,
    )
    primary_tournament["cost_view"] = "primary"
    primary_tournament["round_trip_cost_bps"] = float(round_trip_cost_bps)
    primary_tournament["cost_description"] = "主链标签/安全成本口径"

    tournaments_by_cost: Dict[str, Any] = {}
    for view in cost_views:
        name = str(view.get("name", "")).strip() or "cost"
        cost = safe_float(view.get("round_trip_cost_bps"), math.nan)
        if not math.isfinite(cost) or cost <= 0.0:
            continue
        if name == "primary" and abs(cost - round_trip_cost_bps) < 1e-9:
            tournament = dict(primary_tournament)
        else:
            tournament = build_alpha_tournament(
                symbol,
                feature_rows,
                thresholds,
                ohlcv_rows,
                index_by_timestamp,
                forward_bars=forward_bars,
                tournament_horizons=tournament_horizons,
                round_trip_cost_bps=cost,
                candidate_trend_ratio=candidate_trend_ratio,
                confirmed_trend_ratio=confirmed_trend_ratio,
                min_samples=min_samples,
                min_mean_net_edge_bps=min_mean_net_edge_bps,
                min_positive_net_ratio=min_positive_net_ratio,
            )
        tournament["cost_view"] = name
        tournament["round_trip_cost_bps"] = cost
        tournament["cost_description"] = view.get("description")
        tournaments_by_cost[name] = tournament

    return {
        "symbol": symbol,
        "feature_csv": str(feature_path),
        "ohlcv_csv": str(ohlcv_path) if ohlcv_path is not None else "",
        "feature_rows": len(feature_rows),
        "thresholds": thresholds,
        "direction_counts": direction_counts,
        "bucket_counts": bucket_counts,
        "all": summarize_records(all_records, round_trip_cost_bps),
        "candidate_trend": summarize_records(candidate_records, round_trip_cost_bps),
        "confirmed_trend": summarize_records(confirmed_records, round_trip_cost_bps),
        "alpha_tournament": primary_tournament,
        "alpha_tournament_by_cost": tournaments_by_cost,
    }


def combine_summaries(items: List[Dict[str, Any]], bucket: str) -> Dict[str, Any]:
    # Aggregate from symbol summaries by sample-weighted means where possible.
    total = sum(int(item.get(bucket, {}).get("sample_count") or 0) for item in items)
    if total <= 0:
        return {"sample_count": 0}
    out: Dict[str, Any] = {"sample_count": total}
    metric_names = [
        "mean_gross_forward_bps",
        "median_gross_forward_bps",
        "mean_net_forward_bps",
        "median_net_forward_bps",
        "positive_net_ratio",
        "mean_path_mfe_bps",
        "median_path_mfe_bps",
        "mean_path_mae_bps",
        "median_path_mae_bps",
        "mean_mfe_cost_coverage_ratio",
        "median_mfe_cost_coverage_ratio",
        "mean_gross_capture_of_path_mfe",
        "median_gross_capture_of_path_mfe",
    ]
    for name in metric_names:
        weighted = 0.0
        weight = 0
        values: List[float] = []
        for item in items:
            summary = item.get(bucket, {})
            if not isinstance(summary, dict):
                continue
            count = int(summary.get("sample_count") or 0)
            value = summary.get(name)
            if isinstance(value, (int, float)) and math.isfinite(float(value)) and count > 0:
                weighted += float(value) * float(count)
                weight += count
                values.append(float(value))
        out[name] = (weighted / float(weight)) if weight > 0 else None
        if name.startswith("median_") and values:
            out[name] = median(values)
    return out


def classify_diagnosis(
    aggregate: Dict[str, Dict[str, Any]],
    *,
    min_samples: int,
    min_mean_net_edge_bps: float,
    min_positive_net_ratio: float,
    min_mfe_cost_coverage: float,
) -> Tuple[str, List[str], List[str], List[Dict[str, str]]]:
    confirmed = aggregate.get("confirmed_trend", {})
    candidate = aggregate.get("candidate_trend", {})
    confirmed_samples = int(confirmed.get("sample_count") or 0)
    candidate_samples = int(candidate.get("sample_count") or 0)
    confirmed_mean_net = confirmed.get("mean_net_forward_bps")
    candidate_mean_net = candidate.get("mean_net_forward_bps")
    confirmed_positive = confirmed.get("positive_net_ratio")
    candidate_positive = candidate.get("positive_net_ratio")
    confirmed_mfe_cover = confirmed.get("mean_mfe_cost_coverage_ratio")
    confirmed_capture = confirmed.get("mean_gross_capture_of_path_mfe")

    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    diagnostics: List[Dict[str, str]] = []
    status = "pass"

    if confirmed_samples < min_samples:
        status = "insufficient_samples"
        warn_reasons.append(
            f"confirmed_trend sample_count={confirmed_samples} < min_samples={min_samples}"
        )
        diagnostics.append(
            {
                "code": "confirmed_trend_sample_insufficient",
                "message": "confirmed TREND 样本不足，不能用 live/replay fill 数判断策略收敛",
            }
        )
    elif not isinstance(confirmed_mean_net, (int, float)) or float(confirmed_mean_net) <= min_mean_net_edge_bps:
        status = "fail"
        fail_reasons.append(
            "confirmed_trend mean_net_forward_bps "
            f"{confirmed_mean_net} <= min_mean_net_edge_bps={min_mean_net_edge_bps}"
        )
        diagnostics.append(
            {
                "code": "confirmed_trend_raw_edge_non_positive",
                "message": "确认趋势信号扣除双边成本后没有稳定正边际，继续调执行参数不会解决根因",
            }
        )
    if (
        confirmed_samples >= min_samples
        and isinstance(confirmed_positive, (int, float))
        and float(confirmed_positive) < min_positive_net_ratio
    ):
        status = "fail"
        fail_reasons.append(
            "confirmed_trend positive_net_ratio "
            f"{float(confirmed_positive):.6f} < {min_positive_net_ratio:.6f}"
        )
        diagnostics.append(
            {
                "code": "confirmed_trend_positive_ratio_low",
                "message": "确认趋势信号正净边际占比偏低，说明 alpha 分布不够稳",
            }
        )
    if (
        candidate_samples >= min_samples
        and isinstance(candidate_mean_net, (int, float))
        and float(candidate_mean_net) > min_mean_net_edge_bps
        and isinstance(candidate_positive, (int, float))
        and float(candidate_positive) >= min_positive_net_ratio
        and (
            confirmed_samples < min_samples
            or not isinstance(confirmed_mean_net, (int, float))
            or float(confirmed_mean_net) <= min_mean_net_edge_bps
        )
    ):
        status = "action_required"
        warn_reasons.append(
            "candidate_trend has positive raw edge but confirmed_trend does not; "
            "trend confirmation may be too late or too strict"
        )
        diagnostics.append(
            {
                "code": "candidate_edge_confirmed_gate_lag",
                "message": "候选趋势有净边际但确认趋势没有，优先改趋势确认/入场时点，而不是继续等 live 样本",
            }
        )
    if (
        confirmed_samples >= min_samples
        and isinstance(confirmed_mfe_cover, (int, float))
        and float(confirmed_mfe_cover) >= min_mfe_cost_coverage
        and isinstance(confirmed_capture, (int, float))
        and float(confirmed_capture) < LOW_CAPTURE_RATIO
    ):
        status = "action_required"
        fail_reasons.append(
            "confirmed_trend path MFE covers cost but capture is low: "
            f"mean_mfe_cost_coverage_ratio={float(confirmed_mfe_cover):.6f}, "
            f"mean_gross_capture_of_path_mfe={float(confirmed_capture):.6f}"
        )
        diagnostics.append(
            {
                "code": "path_mfe_available_but_capture_low",
                "message": "行情路径给过足够利润空间，但 forward/exit 捕获率低，优先重做退出/跟踪止盈/最小持仓",
            }
        )

    if status == "pass":
        diagnostics.append(
            {
                "code": "raw_edge_viable",
                "message": "确认趋势原始净边际达标，后续才应重点看执行、成本和风控",
            }
        )
    return status, fail_reasons, warn_reasons, diagnostics


def build_recommendations(status: str, diagnostics: List[Dict[str, str]]) -> List[str]:
    codes = {item.get("code", "") for item in diagnostics}
    recommendations: List[str] = []
    if "confirmed_trend_raw_edge_non_positive" in codes or "confirmed_trend_positive_ratio_low" in codes:
        recommendations.append("暂停继续放宽 live 执行阈值，先重做 alpha/特征/标签，目标是 confirmed_trend 扣成本后为正。")
    if "candidate_edge_confirmed_gate_lag" in codes:
        recommendations.append("把趋势确认拆成入场用 candidate、加仓/留仓用 confirmed，避免等到 edge 衰减后才进场。")
    if "path_mfe_available_but_capture_low" in codes:
        recommendations.append("优先改退出结构：增加 MFE trailing、分段止盈或最大回吐约束，而不是只调入场频率。")
    if "confirmed_trend_sample_insufficient" in codes:
        recommendations.append("下一轮不要只等 12/24h，先用 replay 扩展 candidate/confirmed 样本，验证趋势门槛覆盖率。")
    if status == "pass":
        recommendations.append("原始趋势边际可用，下一步聚焦 replay execution_cost_plan、live fill 质量和 exit_capture。")
    return recommendations


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    default_symbol = normalize_symbol(args.symbol)
    feature_paths = parse_path_entries(args.feature_csv, default_symbol)
    feature_paths.update(parse_mapping(args.feature_csv_by_symbol))
    ohlcv_paths = parse_path_entries(args.ohlcv_csv, default_symbol)
    ohlcv_paths.update(parse_mapping(args.ohlcv_csv_by_symbol))
    tournament_horizons = parse_horizons(
        args.tournament_horizons,
        int(args.forward_bars),
    )
    cost_views = build_cost_views(
        primary_cost_bps=float(args.round_trip_cost_bps),
        maker_cost_bps=args.maker_round_trip_cost_bps,
        stress_cost_multiplier=float(args.stress_cost_multiplier),
        stress_cost_bps=args.stress_round_trip_cost_bps,
    )

    by_symbol: Dict[str, Any] = {}
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    for symbol, feature_path_text in sorted(feature_paths.items()):
        feature_path = Path(feature_path_text)
        if not feature_path.is_file():
            fail_reasons.append(f"feature_csv missing: {symbol}={feature_path}")
            continue
        ohlcv_path_text = ohlcv_paths.get(symbol) or infer_ohlcv_path(feature_path_text)
        ohlcv_path = Path(ohlcv_path_text) if ohlcv_path_text else None
        if ohlcv_path is None or not ohlcv_path.is_file():
            warn_reasons.append(f"ohlcv_csv missing for {symbol}; MFE/MAE not evaluated")
            ohlcv_path = None
        by_symbol[symbol] = diagnose_symbol(
            symbol,
            feature_path,
            ohlcv_path,
            forward_bars=int(args.forward_bars),
            round_trip_cost_bps=float(args.round_trip_cost_bps),
            candidate_trend_ratio=float(args.candidate_trend_ratio),
            confirmed_trend_ratio=float(args.confirmed_trend_ratio),
            tournament_horizons=tournament_horizons,
            min_samples=int(args.min_samples),
            min_mean_net_edge_bps=float(args.min_mean_net_edge_bps),
            min_positive_net_ratio=float(args.min_positive_net_ratio),
            cost_views=cost_views,
        )

    if not by_symbol:
        return {
            "schema_version": "strategy_diagnose_v1",
            "generated_at_utc": now_utc_iso(),
            "status": "skipped" if not fail_reasons else "fail",
            "fail_reasons": fail_reasons,
            "warn_reasons": warn_reasons or ["no feature_csv available"],
            "aggregate": {},
            "by_symbol": {},
            "diagnostics": [],
            "recommendations": ["先生成 feature_store_5m.csv，再运行 strategy_diagnose。"],
        }

    symbol_items = list(by_symbol.values())
    aggregate = {
        "all": combine_summaries(symbol_items, "all"),
        "candidate_trend": combine_summaries(symbol_items, "candidate_trend"),
        "confirmed_trend": combine_summaries(symbol_items, "confirmed_trend"),
    }
    alpha_tournament = combine_alpha_tournaments(symbol_items)
    alpha_tournament_by_cost = combine_alpha_tournament_cost_views(symbol_items)
    status, local_fails, local_warns, diagnostics = classify_diagnosis(
        aggregate,
        min_samples=int(args.min_samples),
        min_mean_net_edge_bps=float(args.min_mean_net_edge_bps),
        min_positive_net_ratio=float(args.min_positive_net_ratio),
        min_mfe_cost_coverage=float(args.min_mfe_cost_coverage),
    )
    fail_reasons.extend(local_fails)
    warn_reasons.extend(local_warns)
    if alpha_tournament.get("status") == "pass" and status in {"fail", "action_required"}:
        warn_reasons.append(
            "alpha_tournament_found_viable_candidate_but_current_strategy_not_aligned"
        )
    elif alpha_tournament.get("status") == "fail":
        fail_reasons.extend(
            str(item)
            for item in alpha_tournament.get("fail_reasons", [])
            if str(item).strip() and str(item) not in fail_reasons
        )
    maker_view = alpha_tournament_by_cost.get("maker")
    maker_stress_view = alpha_tournament_by_cost.get("maker_stress")
    if isinstance(maker_view, dict) and maker_view.get("status") == "pass":
        warn_reasons.append("maker_cost_alpha_candidate_exists; require maker_stress before activation")
    if isinstance(maker_stress_view, dict) and maker_stress_view.get("status") == "pass":
        warn_reasons.append("maker_stress_alpha_candidate_exists; inspect deployable_config before strategy switch")
    return {
        "schema_version": "strategy_diagnose_v1",
        "generated_at_utc": now_utc_iso(),
        "status": status,
        "readiness_status": status.upper(),
        "target": {
            "forward_bars": int(args.forward_bars),
            "round_trip_cost_bps": float(args.round_trip_cost_bps),
            "maker_round_trip_cost_bps": args.maker_round_trip_cost_bps,
            "stress_cost_multiplier": float(args.stress_cost_multiplier),
            "stress_round_trip_cost_bps": args.stress_round_trip_cost_bps,
            "candidate_trend_ratio": float(args.candidate_trend_ratio),
            "confirmed_trend_ratio": float(args.confirmed_trend_ratio),
            "min_samples": int(args.min_samples),
            "min_mean_net_edge_bps": float(args.min_mean_net_edge_bps),
            "min_positive_net_ratio": float(args.min_positive_net_ratio),
            "min_mfe_cost_coverage": float(args.min_mfe_cost_coverage),
        },
        "cost_views": cost_views,
        "aggregate": aggregate,
        "alpha_tournament": alpha_tournament,
        "alpha_tournament_by_cost": alpha_tournament_by_cost,
        "by_symbol": by_symbol,
        "diagnostics": diagnostics,
        "recommendations": build_recommendations(status, diagnostics),
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose raw trend strategy edge")
    parser.add_argument("--output", required=True, help="output JSON path")
    parser.add_argument("--symbol", default="SOURCE", help="default symbol for plain paths")
    parser.add_argument(
        "--feature_csv",
        action="append",
        default=[],
        help="feature_store CSV path or SYMBOL=path; may be repeated",
    )
    parser.add_argument(
        "--feature_csv_by_symbol",
        default="",
        help="comma-separated SYMBOL=feature_store.csv mapping",
    )
    parser.add_argument(
        "--ohlcv_csv",
        action="append",
        default=[],
        help="OHLCV CSV path or SYMBOL=path; may be repeated",
    )
    parser.add_argument(
        "--ohlcv_csv_by_symbol",
        default="",
        help="comma-separated SYMBOL=ohlcv.csv mapping",
    )
    parser.add_argument("--forward-bars", type=int, default=12)
    parser.add_argument(
        "--tournament-horizons",
        default="",
        help="comma-separated horizons for alpha viability tournament; empty uses --forward-bars",
    )
    parser.add_argument("--round-trip-cost-bps", type=float, default=13.0)
    parser.add_argument(
        "--maker-round-trip-cost-bps",
        type=float,
        default=None,
        help="Optional maker-first cost view for alpha tournament",
    )
    parser.add_argument(
        "--stress-cost-multiplier",
        type=float,
        default=1.25,
        help="Stress multiplier applied to maker cost when stress cost is not explicit",
    )
    parser.add_argument(
        "--stress-round-trip-cost-bps",
        type=float,
        default=None,
        help="Optional explicit stress cost view for alpha tournament",
    )
    parser.add_argument("--candidate-trend-ratio", type=float, default=0.60)
    parser.add_argument("--confirmed-trend-ratio", type=float, default=1.00)
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--min-mean-net-edge-bps", type=float, default=0.0)
    parser.add_argument("--min-positive-net-ratio", type=float, default=0.50)
    parser.add_argument("--min-mfe-cost-coverage", type=float, default=1.20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"STRATEGY_DIAGNOSE_REPORT: {output}")
    print(f"STRATEGY_DIAGNOSE_STATUS: {report.get('status')}")
    for reason in report.get("fail_reasons", []):
        print(f"FAIL: {reason}")
    for reason in report.get("warn_reasons", []):
        print(f"WARN: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
