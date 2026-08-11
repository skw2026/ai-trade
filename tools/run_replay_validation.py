#!/usr/bin/env python3
"""
Run replay validation on archived TREND segments.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import pathlib
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    from config_policy_contract import config_value, policy_payload
except ModuleNotFoundError:  # pragma: no cover - package import in unit tests
    from tools.config_policy_contract import config_value, policy_payload


FEATURE_COLUMNS = [
    "ema_diff",
    "zscore_48",
    "mom_12",
    "mom_48",
    "ret_1",
    "range_pct",
    "vol_12",
]

DEFAULT_MAX_SEGMENTS = 16
DEFAULT_MIN_SEGMENT_BARS = 40
REPLAY_WARMUP_CONTEXT_BARS = 96
MIN_RECOMMENDED_EXECUTION_ACTIVE_RUNS = 4
MIN_RECOMMENDED_EXECUTION_PASS_RUNS = 4
MIN_RECOMMENDED_TOTAL_FILLS = 20
MIN_POSITIVE_FILLED_SEGMENT_RATIO = 0.55
SELECTION_CORPUS_SCHEMA_VERSION = "replay_selection_manifest_v3"
SELECTION_CORPUS_EVIDENCE_DOMAIN = "selection_validation"
SELECTION_SAMPLING_POLICY = "chronological_quantiles_without_outcome_v2"
TREND_THRESHOLD_QUANTILE = 0.50
EXTREME_THRESHOLD_QUANTILE = 0.90


@dataclass
class RegimeThresholds:
    trend_abs_ema_diff: float
    trend_abs_mom_48: float
    extreme_vol_12: float
    extreme_range_pct: float


@dataclass
class FeatureRow:
    timestamp: int
    close: float
    volume: float
    features: dict[str, float]
    open: float = float("nan")
    high: float = float("nan")
    low: float = float("nan")


@dataclass
class ReplaySegment:
    start_index: int
    end_index: int
    start_timestamp: int
    end_timestamp: int
    bars: int


def safe_float(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float("nan")


def quantile(values: list[float], q: float, fallback: float) -> float:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return float(fallback)
    if q <= 0.0:
        return float(finite[0])
    if q >= 1.0:
        return float(finite[-1])
    pos = (len(finite) - 1) * q
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return float(finite[low])
    weight = pos - low
    return float(finite[low] * (1.0 - weight) + finite[high] * weight)


def load_feature_rows(path: pathlib.Path) -> list[FeatureRow]:
    rows: list[FeatureRow] = []
    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        required_columns = {"timestamp", "open", "high", "low", "close", "volume"}
        missing_columns = required_columns - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(
                "feature csv 缺少权威 replay OHLCV 列: "
                + ",".join(sorted(missing_columns))
            )
        for raw in reader:
            timestamp_raw = raw.get("timestamp", "")
            if not timestamp_raw.isdigit():
                continue
            features = {name: safe_float(raw.get(name, "")) for name in FEATURE_COLUMNS}
            open_price = safe_float(raw.get("open", ""))
            high_price = safe_float(raw.get("high", ""))
            low_price = safe_float(raw.get("low", ""))
            close_price = safe_float(raw.get("close", ""))
            volume = safe_float(raw.get("volume", ""))
            if (
                not all(
                    math.isfinite(value)
                    for value in (
                        open_price,
                        high_price,
                        low_price,
                        close_price,
                        volume,
                    )
                )
                or min(open_price, high_price, low_price, close_price) <= 0.0
                or volume < 0.0
                or high_price < max(open_price, close_price, low_price)
                or low_price > min(open_price, close_price, high_price)
            ):
                raise ValueError(
                    f"feature csv OHLCV 非法: timestamp={timestamp_raw}"
                )
            rows.append(
                FeatureRow(
                    timestamp=int(timestamp_raw),
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=volume,
                    features=features,
                )
            )
    return rows


def derive_regime_thresholds(rows: list[FeatureRow]) -> RegimeThresholds:
    ema_diff = [abs(row.features["ema_diff"]) for row in rows]
    mom_48 = [abs(row.features["mom_48"]) for row in rows]
    vol_12 = [row.features["vol_12"] for row in rows]
    range_pct = [row.features["range_pct"] for row in rows]
    return RegimeThresholds(
        # Joint above-median strength plus aligned direction remains a strict
        # TREND definition without making the validation corpus disappear
        # after an ordinary volatility regime shift.
        trend_abs_ema_diff=max(
            5e-4, quantile(ema_diff, TREND_THRESHOLD_QUANTILE, 5e-4)
        ),
        trend_abs_mom_48=max(
            2e-3, quantile(mom_48, TREND_THRESHOLD_QUANTILE, 2e-3)
        ),
        extreme_vol_12=max(
            1.5e-3,
            quantile(vol_12, EXTREME_THRESHOLD_QUANTILE, 1.5e-3),
        ),
        extreme_range_pct=max(
            3e-3,
            quantile(range_pct, EXTREME_THRESHOLD_QUANTILE, 3e-3),
        ),
    )


def classify_regime_bucket(row: FeatureRow, thresholds: RegimeThresholds) -> str:
    ema_diff = row.features["ema_diff"]
    mom_48 = row.features["mom_48"]
    vol_12 = row.features["vol_12"]
    range_pct = row.features["range_pct"]

    if (
        math.isfinite(vol_12)
        and vol_12 >= thresholds.extreme_vol_12
    ) or (
        math.isfinite(range_pct)
        and range_pct >= thresholds.extreme_range_pct
    ):
        return "extreme"

    if (
        math.isfinite(ema_diff)
        and math.isfinite(mom_48)
        and abs(ema_diff) >= thresholds.trend_abs_ema_diff
        and abs(mom_48) >= thresholds.trend_abs_mom_48
        and ema_diff * mom_48 > 0.0
    ):
        return "trend"

    return "range"


def infer_base_interval_ms(rows: list[FeatureRow]) -> int:
    deltas: list[int] = []
    for prev, curr in zip(rows, rows[1:]):
        delta = curr.timestamp - prev.timestamp
        if delta > 0:
            deltas.append(delta)
    if not deltas:
        return 300_000
    return int(statistics.median(deltas))


def find_segments(
    rows: list[FeatureRow], thresholds: RegimeThresholds, target_bucket: str, base_interval_ms: int
) -> list[ReplaySegment]:
    segments: list[ReplaySegment] = []
    current_start: int | None = None
    for idx, row in enumerate(rows):
        bucket = classify_regime_bucket(row, thresholds)
        contiguous = False
        if current_start is not None and idx > 0:
            contiguous = rows[idx].timestamp - rows[idx - 1].timestamp == base_interval_ms
        if bucket == target_bucket:
            if current_start is None or not contiguous:
                if current_start is not None:
                    start_row = rows[current_start]
                    end_row = rows[idx - 1]
                    segments.append(
                        ReplaySegment(
                            start_index=current_start,
                            end_index=idx - 1,
                            start_timestamp=start_row.timestamp,
                            end_timestamp=end_row.timestamp,
                            bars=idx - current_start,
                        )
                    )
                current_start = idx
        elif current_start is not None:
            start_row = rows[current_start]
            end_row = rows[idx - 1]
            segments.append(
                ReplaySegment(
                    start_index=current_start,
                    end_index=idx - 1,
                    start_timestamp=start_row.timestamp,
                    end_timestamp=end_row.timestamp,
                    bars=idx - current_start,
                )
            )
            current_start = None
    if current_start is not None:
        start_row = rows[current_start]
        end_row = rows[-1]
        segments.append(
            ReplaySegment(
                start_index=current_start,
                end_index=len(rows) - 1,
                start_timestamp=start_row.timestamp,
                end_timestamp=end_row.timestamp,
                bars=len(rows) - current_start,
            )
        )
    return sorted(segments, key=lambda item: item.bars, reverse=True)


def build_segment_priority_payload(
    segment: ReplaySegment,
    rows: list[FeatureRow],
    thresholds: RegimeThresholds,
    *,
    target_bucket: str,
    volume_baseline: float,
) -> dict[str, float]:
    segment_rows = rows[segment.start_index : segment.end_index + 1]
    avg_abs_ema_diff = finite_mean(
        [abs(row.features["ema_diff"]) for row in segment_rows]
    ) or 0.0
    avg_abs_mom_48 = finite_mean(
        [abs(row.features["mom_48"]) for row in segment_rows]
    ) or 0.0
    avg_vol_12 = finite_mean([row.features["vol_12"] for row in segment_rows]) or 0.0
    avg_range_pct = finite_mean([row.features["range_pct"] for row in segment_rows]) or 0.0
    avg_volume = finite_mean([row.volume for row in segment_rows]) or 0.0

    start_close = segment_rows[0].close if segment_rows else float("nan")
    end_close = segment_rows[-1].close if segment_rows else float("nan")
    if (
        math.isfinite(start_close)
        and math.isfinite(end_close)
        and abs(start_close) > 1e-12
    ):
        price_return_abs = abs(end_close / start_close - 1.0)
    else:
        price_return_abs = 0.0

    if target_bucket == "trend":
        strength_score = 0.5 * (
            avg_abs_ema_diff / max(thresholds.trend_abs_ema_diff, 1e-9)
        ) + 0.5 * (avg_abs_mom_48 / max(thresholds.trend_abs_mom_48, 1e-9))
        path_scale = max(thresholds.trend_abs_mom_48, 1e-9)
    elif target_bucket == "extreme":
        strength_score = 0.5 * (
            avg_vol_12 / max(thresholds.extreme_vol_12, 1e-9)
        ) + 0.5 * (
            avg_range_pct / max(thresholds.extreme_range_pct, 1e-9)
        )
        path_scale = max(thresholds.extreme_range_pct, 1e-9)
    else:
        quiet_trend_score = 1.0 / (
            1.0
            + 0.5 * (avg_abs_ema_diff / max(thresholds.trend_abs_ema_diff, 1e-9))
            + 0.5 * (avg_abs_mom_48 / max(thresholds.trend_abs_mom_48, 1e-9))
        )
        quiet_range_score = 1.0 / (
            1.0 + avg_range_pct / max(thresholds.extreme_range_pct, 1e-9)
        )
        strength_score = quiet_trend_score + quiet_range_score
        path_scale = max(thresholds.trend_abs_mom_48, 1e-9)

    path_score = min(3.0, price_return_abs / path_scale)
    liquidity_score = min(3.0, avg_volume / max(volume_baseline, 1e-9))
    length_score = min(3.0, segment.bars / max(1.0, float(DEFAULT_MIN_SEGMENT_BARS)))
    priority_score = (
        strength_score + 0.35 * path_score + 0.15 * liquidity_score + 0.10 * length_score
    )
    return {
        "priority_score": float(priority_score),
        "strength_score": float(strength_score),
        "path_score": float(path_score),
        "liquidity_score": float(liquidity_score),
        "length_score": float(length_score),
        "avg_abs_ema_diff": float(avg_abs_ema_diff),
        "avg_abs_mom_48": float(avg_abs_mom_48),
        "avg_vol_12": float(avg_vol_12),
        "avg_range_pct": float(avg_range_pct),
        "avg_volume": float(avg_volume),
        "price_return_abs": float(price_return_abs),
    }


def rank_replay_segments(
    segments: list[ReplaySegment],
    rows: list[FeatureRow],
    thresholds: RegimeThresholds,
    *,
    target_bucket: str,
) -> list[ReplaySegment]:
    positive_volumes = [row.volume for row in rows if math.isfinite(row.volume) and row.volume > 0.0]
    volume_baseline = finite_median(positive_volumes) or 1.0
    scored_segments: list[tuple[ReplaySegment, dict[str, float]]] = []
    for segment in segments:
        scored_segments.append(
            (
                segment,
                build_segment_priority_payload(
                    segment,
                    rows,
                    thresholds,
                    target_bucket=target_bucket,
                    volume_baseline=volume_baseline,
                ),
            )
        )
    scored_segments.sort(
        key=lambda item: (
            item[1]["priority_score"],
            item[1]["strength_score"],
            item[0].bars,
            -item[0].start_timestamp,
        ),
        reverse=True,
    )
    return [segment for segment, _ in scored_segments]


def write_replay_csv(
    rows: list[FeatureRow],
    segment: ReplaySegment,
    symbol: str,
    output_path: pathlib.Path,
    default_interval_ms: int,
    warmup_context_bars: int = REPLAY_WARMUP_CONTEXT_BARS,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    replay_start_index = max(
        0, segment.start_index - max(0, int(warmup_context_bars))
    )
    actual_warmup_bars = segment.start_index - replay_start_index
    with output_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "timestamp",
                "symbol",
                "open",
                "high",
                "low",
                "price",
                "volume",
                "interval_ms",
                "funding_rate_per_interval",
                "execution_enabled",
            ]
        )
        previous_timestamp: int | None = None
        for row_index in range(replay_start_index, segment.end_index + 1):
            row = rows[row_index]
            interval_ms = default_interval_ms
            if previous_timestamp is not None:
                interval_ms = max(1, row.timestamp - previous_timestamp)
            writer.writerow(
                [
                    row.timestamp,
                    symbol,
                    f"{row.open:.10f}",
                    f"{row.high:.10f}",
                    f"{row.low:.10f}",
                    f"{row.close:.10f}",
                    f"{row.volume:.10f}",
                    interval_ms,
                    "",
                    1 if row_index >= segment.start_index else 0,
                ]
            )
            previous_timestamp = row.timestamp
    return actual_warmup_bars


def isoformat_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).isoformat()


def segment_to_payload(
    segment: ReplaySegment,
    *,
    rows: list[FeatureRow] | None = None,
    thresholds: RegimeThresholds | None = None,
    target_bucket: str | None = None,
) -> dict[str, Any]:
    payload = {
        "start_index": segment.start_index,
        "end_index": segment.end_index,
        "bars": segment.bars,
        "start_timestamp": segment.start_timestamp,
        "end_timestamp": segment.end_timestamp,
        "start_time_utc": isoformat_ms(segment.start_timestamp),
        "end_time_utc": isoformat_ms(segment.end_timestamp),
    }
    if rows is not None and thresholds is not None and target_bucket:
        positive_volumes = [row.volume for row in rows if math.isfinite(row.volume) and row.volume > 0.0]
        volume_baseline = finite_median(positive_volumes) or 1.0
        payload.update(
            build_segment_priority_payload(
                segment,
                rows,
                thresholds,
                target_bucket=target_bucket,
                volume_baseline=volume_baseline,
            )
        )
        payload.update(build_segment_market_attribution(segment, rows))
    return payload


def build_segment_market_attribution(
    segment: ReplaySegment,
    rows: list[FeatureRow],
) -> dict[str, Any]:
    segment_rows = rows[segment.start_index : segment.end_index + 1]
    closes = [row.close for row in segment_rows if math.isfinite(row.close) and row.close > 0.0]
    if len(closes) < 2:
        return {
            "start_close": None,
            "end_close": None,
            "close_return": None,
            "dominant_direction": 0,
            "dominant_direction_label": "flat",
            "close_path_mfe": None,
            "close_path_mae": None,
            "close_path_efficiency": None,
            "long_close_mfe": None,
            "long_close_mae": None,
            "short_close_mfe": None,
            "short_close_mae": None,
        }

    start_close = closes[0]
    end_close = closes[-1]
    close_return = end_close / start_close - 1.0
    dominant_direction = 1 if close_return > 0.0 else -1 if close_return < 0.0 else 0
    long_returns = [close / start_close - 1.0 for close in closes]
    short_returns = [start_close / close - 1.0 for close in closes]
    long_mfe = max(long_returns)
    long_mae = min(long_returns)
    short_mfe = max(short_returns)
    short_mae = min(short_returns)
    if dominant_direction > 0:
        close_path_mfe = long_mfe
        close_path_mae = long_mae
    elif dominant_direction < 0:
        close_path_mfe = short_mfe
        close_path_mae = short_mae
    else:
        close_path_mfe = 0.0
        close_path_mae = 0.0
    close_path_efficiency = (
        abs(close_return) / close_path_mfe
        if close_path_mfe and close_path_mfe > 1e-12
        else None
    )
    return {
        "start_close": float(start_close),
        "end_close": float(end_close),
        "close_return": float(close_return),
        "dominant_direction": dominant_direction,
        "dominant_direction_label": (
            "long" if dominant_direction > 0 else "short" if dominant_direction < 0 else "flat"
        ),
        "close_path_mfe": float(close_path_mfe),
        "close_path_mae": float(close_path_mae),
        "close_path_efficiency": float(close_path_efficiency)
        if close_path_efficiency is not None
        else None,
        "long_close_mfe": float(long_mfe),
        "long_close_mae": float(long_mae),
        "short_close_mfe": float(short_mfe),
        "short_close_mae": float(short_mae),
    }


def load_corpus_manifest(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_corpus_manifest(
    path: pathlib.Path,
    *,
    feature_csv: pathlib.Path,
    symbol: str = "",
    target_bucket: str,
    base_interval_ms: int,
    thresholds: RegimeThresholds,
    max_segments: int,
    min_segment_bars: int,
    selected_segments: list[ReplaySegment],
) -> None:
    chronological_segments = sorted(
        selected_segments,
        key=lambda item: (item.start_timestamp, item.end_timestamp),
    )
    sample_count = min(max(1, max_segments), len(chronological_segments))
    if sample_count <= 1:
        sampling_quantiles = [0.5]
    else:
        sampling_quantiles = [
            index / float(sample_count - 1)
            for index in range(sample_count)
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "schema_version": SELECTION_CORPUS_SCHEMA_VERSION,
        "evidence_domain": SELECTION_CORPUS_EVIDENCE_DOMAIN,
        "candidate_set_frozen": True,
        "symbol": str(symbol).strip().upper(),
        "generated_at": now_utc_iso(),
        "source_feature_csv": str(feature_csv),
        "source_feature_sha256": (
            hashlib.sha256(feature_csv.read_bytes()).hexdigest()
            if feature_csv.is_file()
            else ""
        ),
        "target_bucket": target_bucket,
        "base_interval_ms": int(base_interval_ms),
        "thresholds": {
            "trend_abs_ema_diff": thresholds.trend_abs_ema_diff,
            "trend_abs_mom_48": thresholds.trend_abs_mom_48,
            "extreme_vol_12": thresholds.extreme_vol_12,
            "extreme_range_pct": thresholds.extreme_range_pct,
        },
        "constraints": {
            "max_segments": int(max(1, max_segments)),
            "min_segment_bars": int(max(1, min_segment_bars)),
        },
        "selection_policy": SELECTION_SAMPLING_POLICY,
        "threshold_policy": {
            "trend_quantile": TREND_THRESHOLD_QUANTILE,
            "extreme_quantile": EXTREME_THRESHOLD_QUANTILE,
            "fit_domain": SELECTION_CORPUS_EVIDENCE_DOMAIN,
            "holdout_refit_forbidden": True,
        },
        "sampling_quantiles": sampling_quantiles,
        "selection_domain_eligible_segment_count": len(chronological_segments),
        "selection_domain_segments": [
            segment_to_payload(segment) for segment in chronological_segments
        ],
        # Selection-domain audit compatibility only. Final holdout never
        # resolves these timestamps; it consumes frozen thresholds/quantiles.
        "segments": [
            segment_to_payload(segment) for segment in chronological_segments
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def validate_selection_corpus_manifest(
    manifest: dict[str, Any],
    *,
    symbol: str,
    target_bucket: str,
    base_interval_ms: int,
    holdout_feature_csv: pathlib.Path,
) -> list[str]:
    reasons: list[str] = []
    if manifest.get("schema_version") != SELECTION_CORPUS_SCHEMA_VERSION:
        reasons.append(
            "selection corpus manifest schema_version invalid: "
            f"expected={SELECTION_CORPUS_SCHEMA_VERSION}"
        )
    if manifest.get("evidence_domain") != SELECTION_CORPUS_EVIDENCE_DOMAIN:
        reasons.append(
            "selection corpus manifest evidence_domain must be "
            f"{SELECTION_CORPUS_EVIDENCE_DOMAIN}"
        )
    if manifest.get("candidate_set_frozen") is not True:
        reasons.append("selection corpus manifest candidate_set_frozen must be true")

    expected_symbol = str(symbol).strip().upper()
    manifest_symbol = str(manifest.get("symbol") or "").strip().upper()
    if not expected_symbol or manifest_symbol != expected_symbol:
        reasons.append(
            "selection corpus manifest symbol mismatch: "
            f"manifest={manifest_symbol or 'missing'}, expected={expected_symbol or 'missing'}"
        )

    manifest_bucket = str(manifest.get("target_bucket") or "").strip().lower()
    if manifest_bucket != str(target_bucket).strip().lower():
        reasons.append(
            "selection corpus manifest target_bucket mismatch: "
            f"manifest={manifest_bucket or 'missing'}, requested={target_bucket}"
        )

    manifest_interval = manifest.get("base_interval_ms")
    if (
        not isinstance(manifest_interval, int)
        or manifest_interval != int(base_interval_ms)
    ):
        reasons.append(
            "selection corpus manifest base_interval_ms mismatch: "
            f"manifest={manifest_interval}, current={int(base_interval_ms)}"
        )

    source_sha256 = str(manifest.get("source_feature_sha256") or "").strip().lower()
    if len(source_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in source_sha256
    ):
        reasons.append(
            "selection corpus manifest source_feature_sha256 missing or invalid"
        )
    source_feature_text = str(
        manifest.get("source_feature_csv") or ""
    ).strip()
    source_feature_path = (
        pathlib.Path(source_feature_text).expanduser().resolve(strict=False)
        if source_feature_text
        else None
    )
    holdout_path = holdout_feature_csv.expanduser().resolve(strict=False)
    if source_feature_path is None:
        reasons.append("selection corpus manifest source_feature_csv missing")
    elif source_feature_path == holdout_path:
        reasons.append(
            "selection corpus manifest was derived from final holdout feature csv"
        )
    elif not source_feature_path.is_file():
        reasons.append(
            "selection corpus manifest source_feature_csv not found: "
            f"{source_feature_path}"
        )
    elif (
        len(source_sha256) == 64
        and not any(char not in "0123456789abcdef" for char in source_sha256)
        and hashlib.sha256(source_feature_path.read_bytes()).hexdigest()
        != source_sha256
    ):
        reasons.append(
            "selection corpus manifest source feature checksum mismatch"
        )

    if manifest.get("selection_policy") != SELECTION_SAMPLING_POLICY:
        reasons.append(
            "selection corpus manifest selection_policy must be "
            f"{SELECTION_SAMPLING_POLICY}"
        )
    threshold_policy = manifest.get("threshold_policy")
    if not isinstance(threshold_policy, dict):
        reasons.append("selection corpus manifest threshold_policy missing")
    else:
        if threshold_policy.get("trend_quantile") != TREND_THRESHOLD_QUANTILE:
            reasons.append(
                "selection corpus manifest trend threshold quantile mismatch"
            )
        if threshold_policy.get("extreme_quantile") != EXTREME_THRESHOLD_QUANTILE:
            reasons.append(
                "selection corpus manifest extreme threshold quantile mismatch"
            )
        if threshold_policy.get("holdout_refit_forbidden") is not True:
            reasons.append(
                "selection corpus manifest holdout_refit_forbidden must be true"
            )
    thresholds = manifest.get("thresholds")
    if not isinstance(thresholds, dict):
        reasons.append("selection corpus manifest thresholds missing")
    else:
        for key in (
            "trend_abs_ema_diff",
            "trend_abs_mom_48",
            "extreme_vol_12",
            "extreme_range_pct",
        ):
            value = thresholds.get(key)
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                reasons.append(
                    f"selection corpus manifest threshold {key} invalid"
                )
    quantiles = manifest.get("sampling_quantiles")
    if not isinstance(quantiles, list) or not quantiles:
        reasons.append(
            "selection corpus manifest sampling_quantiles must be non-empty"
        )
    else:
        previous = -1.0
        for value in quantiles:
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0.0
                or float(value) > 1.0
                or float(value) <= previous
            ):
                reasons.append(
                    "selection corpus manifest sampling_quantiles must be "
                    "strictly increasing values in [0,1]"
                )
                break
            previous = float(value)
    return reasons


def thresholds_from_manifest(manifest: dict[str, Any]) -> RegimeThresholds:
    raw = manifest.get("thresholds")
    if not isinstance(raw, dict):
        raise RuntimeError("selection corpus manifest thresholds missing")
    return RegimeThresholds(
        trend_abs_ema_diff=float(raw["trend_abs_ema_diff"]),
        trend_abs_mom_48=float(raw["trend_abs_mom_48"]),
        extreme_vol_12=float(raw["extreme_vol_12"]),
        extreme_range_pct=float(raw["extreme_range_pct"]),
    )


def select_segments_by_frozen_quantiles(
    segments: list[ReplaySegment],
    quantiles: list[float],
) -> list[ReplaySegment]:
    chronological = sorted(
        segments,
        key=lambda item: (item.start_timestamp, item.end_timestamp),
    )
    if not chronological:
        return []
    selected_indices: list[int] = []
    last_index = len(chronological) - 1
    for value in quantiles:
        index = int(round(float(value) * last_index))
        if index not in selected_indices:
            selected_indices.append(index)
    return [chronological[index] for index in selected_indices]


def resolve_corpus_segments(
    rows: list[FeatureRow],
    manifest: dict[str, Any],
    *,
    target_bucket: str,
    base_interval_ms: int,
) -> tuple[list[ReplaySegment], list[str]]:
    warnings: list[str] = []
    manifest_bucket = str(manifest.get("target_bucket") or "").lower()
    if manifest_bucket and manifest_bucket != target_bucket.lower():
        warnings.append(
            "corpus manifest 目标桶不匹配: "
            f"manifest={manifest_bucket}, requested={target_bucket.lower()}"
        )
        return [], warnings

    manifest_interval = int(manifest.get("base_interval_ms") or 0)
    if manifest_interval > 0 and manifest_interval != int(base_interval_ms):
        warnings.append(
            "corpus manifest 基础间隔与当前数据不一致: "
            f"manifest={manifest_interval}, current={int(base_interval_ms)}"
        )

    index_by_timestamp = {row.timestamp: idx for idx, row in enumerate(rows)}
    segments_raw = manifest.get("segments", [])
    if not isinstance(segments_raw, list):
        warnings.append("corpus manifest 缺少 segments 列表")
        return [], warnings

    resolved: list[ReplaySegment] = []
    for idx, item in enumerate(segments_raw, start=1):
        if not isinstance(item, dict):
            warnings.append(f"corpus segment #{idx} 不是对象，已跳过")
            continue
        start_timestamp = item.get("start_timestamp")
        end_timestamp = item.get("end_timestamp")
        if not isinstance(start_timestamp, int) or not isinstance(end_timestamp, int):
            warnings.append(f"corpus segment #{idx} 时间戳无效，已跳过")
            continue
        start_index = index_by_timestamp.get(start_timestamp)
        end_index = index_by_timestamp.get(end_timestamp)
        if start_index is None or end_index is None:
            warnings.append(
                f"corpus segment #{idx} 无法在当前 feature csv 中解析: "
                f"{start_timestamp}->{end_timestamp}"
            )
            continue
        if start_index > end_index:
            warnings.append(f"corpus segment #{idx} 起止索引倒置，已跳过")
            continue
        contiguous = True
        for current in range(start_index + 1, end_index + 1):
            if rows[current].timestamp - rows[current - 1].timestamp != int(base_interval_ms):
                contiguous = False
                break
        if not contiguous:
            warnings.append(f"corpus segment #{idx} 在当前数据中已不连续，已跳过")
            continue
        resolved.append(
            ReplaySegment(
                start_index=start_index,
                end_index=end_index,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                bars=end_index - start_index + 1,
            )
        )
    return resolved, warnings


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_replay_segments(
    rows: list[FeatureRow],
    thresholds: RegimeThresholds,
    *,
    feature_csv: pathlib.Path,
    target_bucket: str,
    base_interval_ms: int,
    max_segments: int,
    min_segment_bars: int,
    corpus_manifest: pathlib.Path | None,
    refresh_corpus_manifest: bool,
    final_holdout: bool = False,
    symbol: str = "",
) -> tuple[list[ReplaySegment], list[ReplaySegment], dict[str, Any], list[str]]:
    if final_holdout:
        selection: dict[str, Any] = {
            "selection_mode": "selection_manifest_holdout",
            "eligible_segment_count": 0,
            "requested_max_segments": max(1, max_segments),
            "corpus_manifest": str(corpus_manifest) if corpus_manifest else "",
            "corpus_loaded": False,
            "corpus_written": False,
            "corpus_refreshed": False,
            "corpus_auto_refreshed": False,
            "corpus_refresh_reasons": [],
            "corpus_resolved_segment_count": 0,
            "dynamic_appended_segment_count": 0,
            "candidate_set_frozen": True,
            "evidence_domain": SELECTION_CORPUS_EVIDENCE_DOMAIN,
        }
        if refresh_corpus_manifest:
            raise RuntimeError(
                "final holdout 禁止 refresh corpus manifest；"
                "必须消费 selection_validation 域预注册清单"
            )
        if corpus_manifest is None or not corpus_manifest.is_file():
            raise RuntimeError(
                "final holdout 缺少 selection_validation 域预注册 corpus manifest"
            )

        manifest = load_corpus_manifest(corpus_manifest)
        contract_reasons = validate_selection_corpus_manifest(
            manifest,
            symbol=symbol,
            target_bucket=target_bucket,
            base_interval_ms=base_interval_ms,
            holdout_feature_csv=feature_csv,
        )
        if contract_reasons:
            raise RuntimeError(
                "final holdout selection corpus contract invalid: "
                + "; ".join(contract_reasons)
            )

        frozen_thresholds = thresholds_from_manifest(manifest)
        eligible_segments = [
            segment
            for segment in find_segments(
                rows,
                frozen_thresholds,
                target_bucket,
                base_interval_ms,
            )
            if segment.bars >= max(1, min_segment_bars)
        ]
        if not eligible_segments:
            raise RuntimeError(
                "final holdout has no segment satisfying the frozen "
                "selection-domain policy"
            )
        quantiles = [float(value) for value in manifest["sampling_quantiles"]]
        resolved_segments = select_segments_by_frozen_quantiles(
            eligible_segments,
            quantiles,
        )
        if not resolved_segments:
            raise RuntimeError(
                "final holdout frozen sampling policy resolved no segment"
            )

        selection["corpus_loaded"] = True
        selection["corpus_resolved_segment_count"] = len(resolved_segments)
        selection["eligible_segment_count"] = len(eligible_segments)
        selection["registered_segment_count"] = len(quantiles)
        selection["selection_policy"] = manifest.get("selection_policy")
        selection["sampling_quantiles"] = quantiles
        selection["threshold_source"] = SELECTION_CORPUS_EVIDENCE_DOMAIN
        selection["frozen_thresholds"] = thresholds_to_payload(
            frozen_thresholds
        )
        selection["selection_source_feature_csv"] = str(
            manifest.get("source_feature_csv") or ""
        )
        selection["selection_source_feature_sha256"] = str(
            manifest.get("source_feature_sha256") or ""
        )
        selection["max_segments_ignored_for_frozen_candidate_set"] = False
        return (
            list(resolved_segments),
            list(eligible_segments),
            selection,
            [],
        )

    warnings: list[str] = []
    all_segments = find_segments(rows, thresholds, target_bucket, base_interval_ms)
    eligible = rank_replay_segments(
        [segment for segment in all_segments if segment.bars >= max(1, min_segment_bars)],
        rows,
        thresholds,
        target_bucket=target_bucket,
    )

    selection: dict[str, Any] = {
        "selection_mode": "dynamic_top_n",
        "eligible_segment_count": len(eligible),
        "requested_max_segments": max(1, max_segments),
        "corpus_manifest": str(corpus_manifest) if corpus_manifest else "",
        "corpus_loaded": False,
        "corpus_written": False,
        "corpus_refreshed": False,
        "corpus_auto_refreshed": False,
        "corpus_refresh_reasons": [],
        "corpus_resolved_segment_count": 0,
        "dynamic_appended_segment_count": 0,
    }

    if corpus_manifest and corpus_manifest.is_file() and not refresh_corpus_manifest:
        try:
            manifest = load_corpus_manifest(corpus_manifest)
            resolved_segments, corpus_warnings = resolve_corpus_segments(
                rows,
                manifest,
                target_bucket=target_bucket,
                base_interval_ms=base_interval_ms,
            )
            selection["corpus_loaded"] = True
            selection["corpus_refresh_reasons"] = list(corpus_warnings)
            selection["corpus_resolved_segment_count"] = len(resolved_segments)
            if corpus_warnings and eligible:
                selected = eligible[: max(1, max_segments)]
                write_corpus_manifest(
                    corpus_manifest,
                    feature_csv=feature_csv,
                    target_bucket=target_bucket,
                    base_interval_ms=base_interval_ms,
                    thresholds=thresholds,
                    max_segments=max_segments,
                    min_segment_bars=min_segment_bars,
                    selected_segments=selected,
                )
                selection["selection_mode"] = "dynamic_top_n_auto_refresh"
                selection["corpus_written"] = True
                selection["corpus_refreshed"] = True
                selection["corpus_auto_refreshed"] = True
                return selected, eligible, selection, warnings
            warnings.extend(corpus_warnings)
            if resolved_segments:
                selected = list(resolved_segments[: max(1, max_segments)])
                if len(selected) < max(1, max_segments):
                    selected_keys = {
                        (segment.start_timestamp, segment.end_timestamp)
                        for segment in selected
                    }
                    appended_segments = [
                        segment
                        for segment in eligible
                        if (segment.start_timestamp, segment.end_timestamp)
                        not in selected_keys
                    ][: max(1, max_segments) - len(selected)]
                    if appended_segments:
                        selected.extend(appended_segments)
                        selection["dynamic_appended_segment_count"] = len(
                            appended_segments
                        )
                        selection["selection_mode"] = "corpus_manifest_plus_dynamic"
                    else:
                        selection["selection_mode"] = "corpus_manifest"
                else:
                    selection["selection_mode"] = "corpus_manifest"
                return (
                    selected,
                    eligible,
                    selection,
                    warnings,
                )
            warnings.append("corpus manifest 未解析到有效片段，回退到动态选段")
        except Exception as exc:
            warnings.append(f"corpus manifest 读取失败，回退到动态选段: {exc}")

    if not eligible and all_segments:
        eligible = rank_replay_segments(
            [all_segments[0]],
            rows,
            thresholds,
            target_bucket=target_bucket,
        )
        warnings.append(
            f"未找到 bars >= {min_segment_bars} 的 {target_bucket} 片段，退化为最长片段 {all_segments[0].bars} bars"
        )
    selected = eligible[: max(1, max_segments)]
    if not selected:
        raise RuntimeError(f"未找到可用的 {target_bucket} replay 片段")

    corpus_existed_before_write = bool(corpus_manifest and corpus_manifest.exists())
    if corpus_manifest:
        write_corpus_manifest(
            corpus_manifest,
            feature_csv=feature_csv,
            target_bucket=target_bucket,
            base_interval_ms=base_interval_ms,
            thresholds=thresholds,
            max_segments=max_segments,
            min_segment_bars=min_segment_bars,
            selected_segments=selected,
        )
        selection["corpus_written"] = True
        selection["corpus_refreshed"] = bool(refresh_corpus_manifest or corpus_existed_before_write)
    return selected, eligible, selection, warnings


def run_command(command: list[str], output_path: pathlib.Path) -> int:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output_path.write_text(result.stdout, encoding="utf-8")
    return int(result.returncode)


def create_fresh_replay_state_dir(segment_dir: pathlib.Path) -> pathlib.Path:
    state_dir = segment_dir / "state"
    try:
        state_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise RuntimeError(
            "replay segment state directory already exists; refusing WAL "
            f"reuse: {state_dir}"
        ) from exc
    return state_dir


def replay_segment_identity(
    *,
    symbol: str,
    target_bucket: str,
    base_interval_ms: int,
    segment: ReplaySegment,
    replay_csv_sha256: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "replay_segment_identity_v1",
        "symbol": symbol,
        "target_bucket": target_bucket,
        "base_interval_ms": int(base_interval_ms),
        "start_timestamp_ms": int(segment.start_timestamp),
        "end_timestamp_ms": int(segment.end_timestamp),
        "bars": int(segment.bars),
        "replay_csv_sha256": replay_csv_sha256,
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


def should_stop_after_coverage(
    recommended_coverage_met: bool,
    force_all_frozen_segments: bool,
) -> bool:
    return bool(recommended_coverage_met) and not force_all_frozen_segments


EXACT_BLOCK_REQUIRED_FIELDS = (
    "block_id",
    "symbol",
    "start_timestamp_ms",
    "end_timestamp_ms",
    "event_sha256",
    "segment_identity_sha256",
    "replay_csv",
)


def _is_sha256(value: object) -> bool:
    raw = str(value or "")
    return len(raw) == 64 and all(char in "0123456789abcdef" for char in raw)


def inspect_exact_replay_csv(
    replay_csv: pathlib.Path,
    *,
    symbol: str,
    target_bucket: str,
) -> tuple[dict[str, Any], list[str]]:
    inspection: dict[str, Any] = {
        "replay_csv": str(replay_csv),
        "actual_event_sha256": "",
        "actual_segment_identity_sha256": "",
        "actual_start_timestamp_ms": None,
        "actual_end_timestamp_ms": None,
        "actual_base_interval_ms": None,
        "actual_bars": 0,
    }
    errors: list[str] = []
    if not replay_csv.is_file():
        return inspection, ["replay_csv_missing"]

    try:
        inspection["actual_event_sha256"] = hashlib.sha256(
            replay_csv.read_bytes()
        ).hexdigest()
        with replay_csv.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)
            fieldnames = set(reader.fieldnames or [])
            required_columns = {"timestamp", "symbol", "interval_ms"}
            missing_columns = sorted(required_columns - fieldnames)
            if missing_columns:
                return inspection, [
                    "replay_csv_missing_columns:" + ",".join(missing_columns)
                ]
            rows = list(reader)
    except (OSError, csv.Error, UnicodeError) as exc:
        return inspection, [f"replay_csv_read_failed:{type(exc).__name__}"]

    if not rows:
        return inspection, ["replay_csv_empty"]
    normalized_symbol = symbol.strip().upper()
    timestamps: list[int] = []
    intervals: list[int] = []
    active_timestamps: list[int] = []
    for row_index, row in enumerate(rows):
        row_symbol = str(row.get("symbol") or "").strip().upper()
        if row_symbol != normalized_symbol:
            errors.append(
                f"row[{row_index}].symbol_mismatch:{row_symbol}!={normalized_symbol}"
            )
        try:
            timestamp = int(str(row.get("timestamp") or ""))
            interval_ms = int(str(row.get("interval_ms") or ""))
        except ValueError:
            errors.append(f"row[{row_index}].timestamp_or_interval_invalid")
            continue
        if interval_ms <= 0:
            errors.append(f"row[{row_index}].interval_nonpositive")
            continue
        timestamps.append(timestamp)
        intervals.append(interval_ms)
        execution_enabled = str(row.get("execution_enabled", "1")).strip().lower()
        if execution_enabled not in {"0", "false", "1", "true"}:
            errors.append(f"row[{row_index}].execution_enabled_invalid")
            continue
        if execution_enabled not in {"0", "false"}:
            active_timestamps.append(timestamp)

    if errors:
        return inspection, list(dict.fromkeys(errors))
    if not active_timestamps:
        return inspection, ["replay_csv_no_execution_enabled_rows"]
    if len(set(intervals)) != 1:
        errors.append("replay_csv_interval_not_constant")
    if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
        errors.append("replay_csv_timestamps_not_strictly_increasing")
    base_interval_ms = intervals[0]
    if any(
        current - previous != base_interval_ms
        for previous, current in zip(active_timestamps, active_timestamps[1:])
    ):
        errors.append("execution_rows_not_contiguous")
    if errors:
        return inspection, errors

    segment = ReplaySegment(
        start_index=0,
        end_index=len(active_timestamps) - 1,
        start_timestamp=active_timestamps[0],
        end_timestamp=active_timestamps[-1],
        bars=len(active_timestamps),
    )
    identity = replay_segment_identity(
        symbol=normalized_symbol,
        target_bucket=target_bucket,
        base_interval_ms=base_interval_ms,
        segment=segment,
        replay_csv_sha256=str(inspection["actual_event_sha256"]),
    )
    inspection.update(
        {
            "actual_segment_identity_sha256": identity["sha256"],
            "actual_start_timestamp_ms": segment.start_timestamp,
            "actual_end_timestamp_ms": segment.end_timestamp,
            "actual_base_interval_ms": base_interval_ms,
            "actual_bars": segment.bars,
            "segment_identity": identity,
            "segment": segment,
        }
    )
    return inspection, []


def preflight_exact_block_plan(
    plan_path: pathlib.Path,
    *,
    fallback_target_bucket: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    plan_metadata: dict[str, Any] = {
        "path": str(plan_path),
        "sha256": "",
        "benchmark_id": "",
        "target_bucket": fallback_target_bucket,
        "read_only": True,
    }
    audits: list[dict[str, Any]] = []
    prepared: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    if not plan_path.is_file():
        return plan_metadata, audits, prepared, ["exact_block_plan_missing"]
    try:
        plan_metadata["sha256"] = hashlib.sha256(
            plan_path.read_bytes()
        ).hexdigest()
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return plan_metadata, audits, prepared, [
            f"exact_block_plan_invalid_json:{type(exc).__name__}"
        ]
    if not isinstance(payload, dict):
        return plan_metadata, audits, prepared, ["exact_block_plan_not_object"]
    if payload.get("schema_version") != "exact_replay_block_plan_v1":
        validation_errors.append("exact_block_plan_schema_version_invalid")
    benchmark_id = str(payload.get("benchmark_id") or "").strip()
    if not _is_sha256(benchmark_id):
        validation_errors.append("exact_block_plan_benchmark_id_invalid")
    plan_metadata["benchmark_id"] = benchmark_id
    target_bucket = str(
        payload.get("target_bucket") or fallback_target_bucket
    ).strip().lower()
    if target_bucket not in {"trend", "range", "extreme"}:
        validation_errors.append("exact_block_plan_target_bucket_invalid")
    plan_metadata["target_bucket"] = target_bucket
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        validation_errors.append("exact_block_plan_blocks_missing")
        return plan_metadata, audits, prepared, validation_errors

    seen_block_ids: set[str] = set()
    seen_cells: set[tuple[str, int, int]] = set()
    seen_segment_identities: set[str] = set()
    for index, raw_block in enumerate(raw_blocks):
        audit: dict[str, Any] = {
            "plan_index": index,
            "block_id": "",
            "symbol": "",
            "start_timestamp_ms": None,
            "end_timestamp_ms": None,
            "expected_event_sha256": "",
            "actual_event_sha256": "",
            "expected_segment_identity_sha256": "",
            "actual_segment_identity_sha256": "",
            "replay_csv": "",
            "command": [],
            "assess_command": [],
            "trade_bot_exit_code": None,
            "assess_exit_code": None,
            "episode_execution_evidence": None,
            "execution_attempt_count": 0,
            "execution_status": "NOT_RUN",
            "errors": [],
        }
        audits.append(audit)
        if not isinstance(raw_block, dict):
            audit["errors"].append("block_not_object")
            validation_errors.append(f"block[{index}].block_not_object")
            continue
        missing_fields = [
            field
            for field in EXACT_BLOCK_REQUIRED_FIELDS
            if raw_block.get(field) is None or str(raw_block.get(field)).strip() == ""
        ]
        if missing_fields:
            error = "missing_fields:" + ",".join(missing_fields)
            audit["errors"].append(error)
            validation_errors.append(f"block[{index}].{error}")
            continue
        block_id = str(raw_block["block_id"]).strip()
        symbol = str(raw_block["symbol"]).strip().upper()
        event_sha256 = str(raw_block["event_sha256"]).strip()
        segment_identity_sha256 = str(
            raw_block["segment_identity_sha256"]
        ).strip()
        audit.update(
            {
                "block_id": block_id,
                "symbol": symbol,
                "expected_event_sha256": event_sha256,
                "expected_segment_identity_sha256": segment_identity_sha256,
            }
        )
        try:
            start_timestamp_ms = int(raw_block["start_timestamp_ms"])
            end_timestamp_ms = int(raw_block["end_timestamp_ms"])
        except (TypeError, ValueError):
            audit["errors"].append("timestamp_invalid")
            validation_errors.append(f"block[{index}].timestamp_invalid")
            continue
        audit["start_timestamp_ms"] = start_timestamp_ms
        audit["end_timestamp_ms"] = end_timestamp_ms
        if start_timestamp_ms > end_timestamp_ms:
            audit["errors"].append("timestamp_range_invalid")
        if not _is_sha256(event_sha256):
            audit["errors"].append("event_sha256_invalid")
        if not _is_sha256(segment_identity_sha256):
            audit["errors"].append("segment_identity_sha256_invalid")
        cell = (symbol, start_timestamp_ms, end_timestamp_ms)
        if block_id in seen_block_ids:
            audit["errors"].append("duplicate_block_id")
        if cell in seen_cells:
            audit["errors"].append("duplicate_block_interval")
        if segment_identity_sha256 in seen_segment_identities:
            audit["errors"].append("duplicate_segment_identity")
        seen_block_ids.add(block_id)
        seen_cells.add(cell)
        seen_segment_identities.add(segment_identity_sha256)

        replay_csv_raw = pathlib.Path(str(raw_block["replay_csv"]))
        replay_csv = (
            replay_csv_raw
            if replay_csv_raw.is_absolute()
            else (plan_path.parent / replay_csv_raw).resolve()
        )
        audit["replay_csv"] = str(replay_csv)
        inspection, inspection_errors = inspect_exact_replay_csv(
            replay_csv,
            symbol=symbol,
            target_bucket=target_bucket,
        )
        audit["actual_event_sha256"] = inspection["actual_event_sha256"]
        audit["actual_segment_identity_sha256"] = inspection[
            "actual_segment_identity_sha256"
        ]
        audit["actual_start_timestamp_ms"] = inspection[
            "actual_start_timestamp_ms"
        ]
        audit["actual_end_timestamp_ms"] = inspection["actual_end_timestamp_ms"]
        audit["actual_base_interval_ms"] = inspection[
            "actual_base_interval_ms"
        ]
        audit["actual_bars"] = inspection["actual_bars"]
        audit["errors"].extend(inspection_errors)
        if inspection["actual_event_sha256"] != event_sha256:
            audit["errors"].append("event_sha256_mismatch")
        if inspection["actual_start_timestamp_ms"] != start_timestamp_ms:
            audit["errors"].append("start_timestamp_mismatch")
        if inspection["actual_end_timestamp_ms"] != end_timestamp_ms:
            audit["errors"].append("end_timestamp_mismatch")
        if (
            inspection["actual_segment_identity_sha256"]
            != segment_identity_sha256
        ):
            audit["errors"].append("segment_identity_sha256_mismatch")
        audit["errors"] = list(dict.fromkeys(audit["errors"]))
        for error in audit["errors"]:
            validation_errors.append(f"block[{index}].{error}")
        if not audit["errors"]:
            prepared.append(
                {
                    "audit": audit,
                    "replay_csv": replay_csv,
                    "segment": inspection["segment"],
                    "segment_identity": inspection["segment_identity"],
                }
            )
    return (
        plan_metadata,
        audits,
        prepared,
        list(dict.fromkeys(validation_errors)),
    )


def execute_replay_csv(
    *,
    block_id: str,
    symbol: str,
    segment_index: int,
    segment_payload: dict[str, Any],
    replay_csv: pathlib.Path,
    segment_identity: dict[str, Any],
    segment_dir: pathlib.Path,
    root: pathlib.Path,
    base_config: pathlib.Path,
    trade_bot: pathlib.Path,
    assess_stage: str,
    min_runtime_status: int,
    execution_policy_identity: dict[str, Any],
    trade_bot_sha256: str,
    warmup_context_bars: int,
) -> dict[str, Any]:
    segment_dir.mkdir(parents=True, exist_ok=True)
    state_dir = create_fresh_replay_state_dir(segment_dir)
    runtime_log = segment_dir / "runtime.log"
    runtime_assess = segment_dir / "runtime_assess.json"
    replay_csv_sha256 = hashlib.sha256(replay_csv.read_bytes()).hexdigest()
    if replay_csv_sha256 != segment_identity.get("replay_csv_sha256"):
        raise RuntimeError("replay csv changed after identity validation")
    trade_cmd = [
        str(trade_bot),
        f"--config={base_config}",
        "--exchange=bybit",
        f"--data_path={state_dir}",
        f"--replay_market_data={replay_csv}",
        "--replay_timestamp_column=timestamp",
        "--replay_symbol_column=symbol",
        "--replay_price_column=price",
        "--replay_volume_column=volume",
        "--replay_interval_column=interval_ms",
        "--replay_funding_rate_column=funding_rate_per_interval",
        f"--replay_default_interval_ms={segment_identity['base_interval_ms']}",
    ]
    trade_exit = run_command(trade_cmd, runtime_log)
    assess_cmd = [
        sys.executable,
        str(root / "tools" / "assess_run_log.py"),
        "--log",
        str(runtime_log),
        "--stage",
        assess_stage,
        "--min_runtime_status",
        str(max(1, min_runtime_status)),
        "--json_out",
        str(runtime_assess),
        "--segment-identity-sha256",
        str(segment_identity["sha256"]),
        "--execution-policy-identity-json",
        json.dumps(
            execution_policy_identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    assess_exit = subprocess.run(assess_cmd, check=False).returncode
    assess_payload: dict[str, Any] = {}
    if runtime_assess.is_file():
        assess_payload = json.loads(runtime_assess.read_text(encoding="utf-8"))
    assess_summary = summarize_assess(assess_payload) if assess_payload else {}
    return {
        "block_id": block_id,
        "symbol": symbol,
        "segment_index": segment_index,
        "segment": segment_payload,
        "replay_csv": str(replay_csv),
        "replay_csv_sha256": replay_csv_sha256,
        "segment_identity": segment_identity,
        "segment_identity_sha256": segment_identity["sha256"],
        "execution_policy_identity": execution_policy_identity,
        "trade_bot_sha256": trade_bot_sha256,
        "state_dir": str(state_dir),
        "state_isolation": "fresh_segment_wal",
        "warmup_context_bars": warmup_context_bars,
        "warmup_context_execution_disabled": True,
        "runtime_log": str(runtime_log),
        "runtime_assess": str(runtime_assess),
        "command": trade_cmd,
        "assess_command": assess_cmd,
        "trade_bot_exit_code": trade_exit,
        "assess_exit_code": int(assess_exit),
        "assess_summary": assess_summary,
        "episode_execution_evidence": assess_payload.get(
            "episode_execution_evidence"
        ),
    }


def summarize_assess(assess_payload: dict[str, Any]) -> dict[str, Any]:
    metrics = assess_payload.get("metrics", {})
    execution_attribution = assess_payload.get("execution_attribution", {})
    fills_attribution: dict[str, Any] = {}
    if isinstance(execution_attribution, dict):
        fills = execution_attribution.get("fills", {})
        if isinstance(fills, dict):
            quality_by_symbol = fills.get("quality_by_symbol", {})
            fills_attribution = {
                "total": fills.get("total"),
                "fee_usd": fills.get("fee_usd"),
                "notional_abs_usd": fills.get("notional_abs_usd"),
                "maker_count": fills.get("maker_count"),
                "taker_count": fills.get("taker_count"),
                "unknown_liquidity_count": fills.get("unknown_liquidity_count"),
                "quality_by_symbol": quality_by_symbol
                if isinstance(quality_by_symbol, dict)
                else {},
            }
    return {
        "verdict": assess_payload.get("verdict"),
        "runtime_validation_mode": assess_payload.get("runtime_validation_mode"),
        "protection_status": assess_payload.get("protection_status"),
        "execution_status": assess_payload.get("execution_status"),
        "market_context_status": assess_payload.get("market_context_status"),
        "episode_execution_evidence": assess_payload.get(
            "episode_execution_evidence"
        ),
        "execution_activity_count": metrics.get("execution_activity_count"),
        "funnel_fills_runtime_count": metrics.get("funnel_fills_runtime_count"),
        "regime_trend_runtime_count": metrics.get("regime_trend_runtime_count"),
        "realized_net_per_fill": metrics.get("realized_net_per_fill"),
        "filtered_cost_ratio_avg": metrics.get("filtered_cost_ratio_avg"),
        "fee_bps_per_fill": metrics.get("fee_bps_per_fill"),
        "entry_edge_gap_avg_bps": metrics.get("entry_edge_gap_avg_bps"),
        "entry_gate_observed_filtered_ratio_avg": metrics.get(
            "entry_gate_observed_filtered_ratio_avg"
        ),
        # Replay emits the same per-exit capture evidence as live runtime.  Keep
        # it in the compact summary so the economic gate can prefer actual
        # trade episodes over the much coarser whole-segment path proxy.
        "exit_capture_sample_count": metrics.get("exit_capture_sample_count"),
        "exit_capture_low_count": metrics.get("exit_capture_low_count"),
        "exit_capture_low_ratio": metrics.get("exit_capture_low_ratio"),
        "exit_capture_mean_path_mfe_bps": metrics.get(
            "exit_capture_mean_path_mfe_bps"
        ),
        "exit_capture_mean_captured_gross_bps": metrics.get(
            "exit_capture_mean_captured_gross_bps"
        ),
        "exit_capture_mean_captured_net_bps": metrics.get(
            "exit_capture_mean_captured_net_bps"
        ),
        "exit_capture_mean_fee_bps": metrics.get("exit_capture_mean_fee_bps"),
        "exit_capture_mean_capture_ratio": metrics.get(
            "exit_capture_mean_capture_ratio"
        ),
        "execution_attribution_fill_count": metrics.get(
            "execution_attribution_fill_count"
        ),
        "execution_attribution_main_fill_count": metrics.get(
            "execution_attribution_main_fill_count"
        ),
        "execution_attribution_probe_fill_count": metrics.get(
            "execution_attribution_probe_fill_count"
        ),
        "execution_attribution_maker_fill_count": metrics.get(
            "execution_attribution_maker_fill_count"
        ),
        "execution_attribution_taker_fill_count": metrics.get(
            "execution_attribution_taker_fill_count"
        ),
        "execution_attribution_fee_usd": metrics.get("execution_attribution_fee_usd"),
        "execution_attribution_quality_fill_count": metrics.get(
            "execution_attribution_quality_fill_count"
        ),
        "execution_attribution_realized_net_usd": metrics.get(
            "execution_attribution_realized_net_usd"
        ),
        "execution_attribution_realized_net_per_fill": metrics.get(
            "execution_attribution_realized_net_per_fill"
        ),
        "execution_attribution_main_fee_usd": metrics.get(
            "execution_attribution_main_fee_usd"
        ),
        "execution_attribution_probe_fee_usd": metrics.get(
            "execution_attribution_probe_fee_usd"
        ),
        "execution_attribution_runtime_fill_window_count": metrics.get(
            "execution_attribution_runtime_fill_window_count"
        ),
        "execution_attribution_runtime_realized_net_delta_usd": metrics.get(
            "execution_attribution_runtime_realized_net_delta_usd"
        ),
        "execution_attribution_runtime_fee_delta_usd": metrics.get(
            "execution_attribution_runtime_fee_delta_usd"
        ),
        "execution_attribution_worst_symbol": metrics.get(
            "execution_attribution_worst_symbol"
        ),
        "execution_attribution_worst_symbol_realized_net_per_fill": metrics.get(
            "execution_attribution_worst_symbol_realized_net_per_fill"
        ),
        "execution_attribution_best_symbol": metrics.get(
            "execution_attribution_best_symbol"
        ),
        "execution_attribution_best_symbol_realized_net_per_fill": metrics.get(
            "execution_attribution_best_symbol_realized_net_per_fill"
        ),
        "replay_terminal_settlement_done_count": metrics.get(
            "replay_terminal_settlement_done_count"
        ),
        "replay_terminal_settlement_failed_count": metrics.get(
            "replay_terminal_settlement_failed_count"
        ),
        "replay_terminal_realized_net_usd": metrics.get(
            "replay_terminal_realized_net_usd"
        ),
        "replay_terminal_fee_usd": metrics.get("replay_terminal_fee_usd"),
        "replay_terminal_funding_paid_usd": metrics.get(
            "replay_terminal_funding_paid_usd"
        ),
        "fills_attribution": fills_attribution,
        "warn_reasons": assess_payload.get("warn_reasons", []),
        "fail_reasons": assess_payload.get("fail_reasons", []),
    }


def number_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return 0


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if not math.isfinite(float(numerator)) or not math.isfinite(float(denominator)):
        return None
    if abs(float(denominator)) <= 1e-12:
        return None
    return float(numerator) / float(denominator)


def quantile_from_runs(
    run_summaries: list[dict[str, Any]],
    field_path: tuple[str, ...],
    q: float,
) -> float | None:
    values: list[float] = []
    for run in run_summaries:
        current: Any = run
        for part in field_path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        value = number_or_none(current)
        if value is not None:
            values.append(value)
    if not values:
        return None
    return quantile(values, q, values[0])


def build_run_economics_attribution(run: dict[str, Any]) -> dict[str, Any]:
    summary = run.get("assess_summary", {})
    if not isinstance(summary, dict):
        summary = {}
    segment = run.get("segment", {})
    if not isinstance(segment, dict):
        segment = {}

    fill_count = int_or_zero(summary.get("execution_attribution_fill_count"))
    quality_fill_count = int_or_zero(
        summary.get("execution_attribution_quality_fill_count")
    )
    terminal_done_count = int_or_zero(
        summary.get("replay_terminal_settlement_done_count")
    )
    terminal_failed_count = int_or_zero(
        summary.get("replay_terminal_settlement_failed_count")
    )
    realized_net_usd = number_or_none(
        summary.get("replay_terminal_realized_net_usd")
    )
    fee_usd = number_or_none(summary.get("replay_terminal_fee_usd"))
    funding_paid_usd = number_or_none(
        summary.get("replay_terminal_funding_paid_usd")
    )

    incomplete_reasons: list[str] = []
    if terminal_done_count != 1:
        incomplete_reasons.append(
            f"terminal_settlement_done_count={terminal_done_count} != 1"
        )
    if terminal_failed_count > 0:
        incomplete_reasons.append(
            f"terminal_settlement_failed_count={terminal_failed_count} > 0"
        )
    if realized_net_usd is None:
        incomplete_reasons.append("terminal_realized_net_usd_missing")
    if fee_usd is None:
        incomplete_reasons.append("terminal_fee_usd_missing")
    if funding_paid_usd is None:
        incomplete_reasons.append("terminal_funding_paid_usd_missing")
    if quality_fill_count != fill_count:
        incomplete_reasons.append(
            "execution_attribution_quality_fill_count="
            f"{quality_fill_count} != fill_count={fill_count}"
        )

    economics_complete = not incomplete_reasons
    realized_net_per_fill = (
        float(realized_net_usd) / float(fill_count)
        if economics_complete and realized_net_usd is not None and fill_count > 0
        else None
    )
    exact_realized_net_usd = (
        float(realized_net_usd)
        if economics_complete and realized_net_usd is not None
        else 0.0
    )
    exact_fee_usd = (
        float(fee_usd) if economics_complete and fee_usd is not None else 0.0
    )
    exact_funding_paid_usd = (
        float(funding_paid_usd)
        if economics_complete and funding_paid_usd is not None
        else 0.0
    )
    fee_per_fill_usd = (
        exact_fee_usd / fill_count
        if economics_complete and fill_count > 0
        else 0.0
    )
    estimated_net_before_fee_usd = exact_realized_net_usd + exact_fee_usd
    estimated_gross_pnl_usd = (
        estimated_net_before_fee_usd + exact_funding_paid_usd
    )
    fills_attribution = summary.get("fills_attribution", {})
    if not isinstance(fills_attribution, dict):
        fills_attribution = {}
    notional_abs_usd = number_or_none(fills_attribution.get("notional_abs_usd"))
    notional_abs_per_fill_usd = (
        float(notional_abs_usd) / fill_count
        if notional_abs_usd is not None and fill_count > 0
        else None
    )
    reported_fee_bps_per_fill = number_or_none(summary.get("fee_bps_per_fill"))
    derived_fee_bps_per_fill = (
        fee_per_fill_usd / notional_abs_per_fill_usd * 10_000.0
        if notional_abs_per_fill_usd is not None
        and notional_abs_per_fill_usd > 1e-12
        and fee_per_fill_usd > 0.0
        else None
    )
    fee_bps_per_fill = (
        derived_fee_bps_per_fill
        if derived_fee_bps_per_fill is not None
        else reported_fee_bps_per_fill
    )

    return {
        "symbol": run.get("symbol"),
        "segment_index": run.get("segment_index"),
        "runtime_validation_mode": summary.get("runtime_validation_mode"),
        "execution_status": summary.get("execution_status"),
        "market_context_status": summary.get("market_context_status"),
        "economics_complete": economics_complete,
        "economics_incomplete_reasons": incomplete_reasons,
        "accounting_source": (
            "replay_terminal_account_plus_fill_attribution"
            if economics_complete
            else "incomplete"
        ),
        "terminal_settlement_done_count": terminal_done_count,
        "terminal_settlement_failed_count": terminal_failed_count,
        "fill_count": fill_count,
        "quality_fill_count": quality_fill_count,
        "execution_activity_count": int_or_zero(summary.get("execution_activity_count")),
        "realized_net_per_fill": realized_net_per_fill,
        "realized_net_usd": exact_realized_net_usd,
        "realized_net_usd_est": exact_realized_net_usd,
        "fee_usd": exact_fee_usd,
        "funding_paid_usd": exact_funding_paid_usd,
        "fee_per_fill_usd": fee_per_fill_usd,
        "notional_abs_usd": notional_abs_usd,
        "notional_abs_per_fill_usd": notional_abs_per_fill_usd,
        "reported_fee_bps_per_fill": reported_fee_bps_per_fill,
        "derived_fee_bps_per_fill": derived_fee_bps_per_fill,
        "estimated_net_before_fee_usd": estimated_net_before_fee_usd,
        "estimated_gross_pnl_usd": estimated_gross_pnl_usd,
        "estimated_gross_per_fill_usd": (
            estimated_gross_pnl_usd / fill_count if fill_count > 0 else 0.0
        ),
        "filtered_cost_ratio_avg": number_or_none(
            summary.get("filtered_cost_ratio_avg")
        ),
        "fee_bps_per_fill": fee_bps_per_fill,
        "entry_edge_gap_avg_bps": number_or_none(
            summary.get("entry_edge_gap_avg_bps")
        ),
        "entry_gate_observed_filtered_ratio_avg": number_or_none(
            summary.get("entry_gate_observed_filtered_ratio_avg")
        ),
        "exit_capture_sample_count": int_or_zero(
            summary.get("exit_capture_sample_count")
        ),
        "exit_capture_low_count": int_or_zero(
            summary.get("exit_capture_low_count")
        ),
        "exit_capture_low_ratio": number_or_none(
            summary.get("exit_capture_low_ratio")
        ),
        "exit_capture_mean_path_mfe_bps": number_or_none(
            summary.get("exit_capture_mean_path_mfe_bps")
        ),
        "exit_capture_mean_captured_gross_bps": number_or_none(
            summary.get("exit_capture_mean_captured_gross_bps")
        ),
        "exit_capture_mean_captured_net_bps": number_or_none(
            summary.get("exit_capture_mean_captured_net_bps")
        ),
        "exit_capture_mean_fee_bps": number_or_none(
            summary.get("exit_capture_mean_fee_bps")
        ),
        "exit_capture_mean_capture_ratio": number_or_none(
            summary.get("exit_capture_mean_capture_ratio")
        ),
        "main_fill_count": int_or_zero(
            summary.get("execution_attribution_main_fill_count")
        ),
        "probe_fill_count": int_or_zero(
            summary.get("execution_attribution_probe_fill_count")
        ),
        "maker_fill_count": int_or_zero(
            summary.get("execution_attribution_maker_fill_count")
        ),
        "taker_fill_count": int_or_zero(
            summary.get("execution_attribution_taker_fill_count")
        ),
        "worst_symbol": summary.get("execution_attribution_worst_symbol"),
        "worst_symbol_realized_net_per_fill": number_or_none(
            summary.get("execution_attribution_worst_symbol_realized_net_per_fill")
        ),
        "best_symbol": summary.get("execution_attribution_best_symbol"),
        "best_symbol_realized_net_per_fill": number_or_none(
            summary.get("execution_attribution_best_symbol_realized_net_per_fill")
        ),
        "segment_bars": segment.get("bars"),
        "segment_strength_score": number_or_none(segment.get("strength_score")),
        "segment_path_score": number_or_none(segment.get("path_score")),
        "segment_liquidity_score": number_or_none(segment.get("liquidity_score")),
        "segment_price_return_abs": number_or_none(segment.get("price_return_abs")),
        "segment_avg_abs_ema_diff": number_or_none(segment.get("avg_abs_ema_diff")),
        "segment_avg_abs_mom_48": number_or_none(segment.get("avg_abs_mom_48")),
        "segment_avg_vol_12": number_or_none(segment.get("avg_vol_12")),
        "segment_avg_range_pct": number_or_none(segment.get("avg_range_pct")),
        "segment_close_return": number_or_none(segment.get("close_return")),
        "segment_dominant_direction": int_or_zero(segment.get("dominant_direction")),
        "segment_dominant_direction_label": segment.get("dominant_direction_label"),
        "segment_close_path_mfe": number_or_none(segment.get("close_path_mfe")),
        "segment_close_path_mae": number_or_none(segment.get("close_path_mae")),
        "segment_close_path_efficiency": number_or_none(
            segment.get("close_path_efficiency")
        ),
        "segment_long_close_mfe": number_or_none(segment.get("long_close_mfe")),
        "segment_long_close_mae": number_or_none(segment.get("long_close_mae")),
        "segment_short_close_mfe": number_or_none(segment.get("short_close_mfe")),
        "segment_short_close_mae": number_or_none(segment.get("short_close_mae")),
    }


def summarize_economics_attribution(
    economics_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    complete_rows = [
        row for row in economics_rows if row.get("economics_complete") is not False
    ]
    incomplete_rows = [
        row for row in economics_rows if row.get("economics_complete") is False
    ]
    rows_with_fills = [
        row for row in complete_rows if int_or_zero(row.get("fill_count")) > 0
    ]
    net_values = [
        float(row["realized_net_per_fill"])
        for row in rows_with_fills
        if isinstance(row.get("realized_net_per_fill"), (int, float))
    ]
    fee_values = [
        float(row["fee_per_fill_usd"])
        for row in rows_with_fills
        if isinstance(row.get("fee_per_fill_usd"), (int, float))
    ]
    total_fills = sum(int_or_zero(row.get("fill_count")) for row in complete_rows)
    total_realized_net = sum(
        float(row.get("realized_net_usd_est") or 0.0) for row in complete_rows
    )
    total_fee = sum(float(row.get("fee_usd") or 0.0) for row in complete_rows)
    total_funding = sum(
        float(row.get("funding_paid_usd") or 0.0) for row in complete_rows
    )
    total_net_before_fee = sum(
        float(
            row.get("estimated_net_before_fee_usd")
            if row.get("estimated_net_before_fee_usd") is not None
            else (
                float(row.get("estimated_gross_pnl_usd") or 0.0)
                - float(row.get("funding_paid_usd") or 0.0)
            )
        )
        for row in complete_rows
    )
    total_gross = sum(
        float(row.get("estimated_gross_pnl_usd") or 0.0)
        for row in complete_rows
    )
    positive_rows = sum(1 for value in net_values if value > 1e-12)
    negative_rows = sum(1 for value in net_values if value < -1e-12)
    zero_rows = sum(1 for value in net_values if abs(value) <= 1e-12)
    diagnostics: list[str] = []
    if rows_with_fills and positive_rows <= 0 and negative_rows > 0:
        diagnostics.append("all_filled_segments_net_negative")
    if total_fills > 0 and total_fee > abs(total_gross):
        diagnostics.append("fees_exceed_abs_estimated_gross_pnl")
    if rows_with_fills and finite_mean(fee_values) and (finite_mean(fee_values) or 0.0) > abs(
        finite_mean(net_values) or 0.0
    ):
        diagnostics.append("fee_per_fill_exceeds_abs_mean_net_per_fill")
    return {
        "segment_count": len(economics_rows),
        "economics_complete_segment_count": len(complete_rows),
        "economics_incomplete_segment_count": len(incomplete_rows),
        "filled_segment_count": len(rows_with_fills),
        "total_fills": total_fills,
        "positive_filled_segments": positive_rows,
        "negative_filled_segments": negative_rows,
        "zero_filled_segments": zero_rows,
        "total_realized_net_usd_est": total_realized_net,
        "total_fee_usd": total_fee,
        "total_funding_paid_usd": total_funding,
        "total_estimated_net_before_fee_usd": total_net_before_fee,
        "total_estimated_gross_pnl_usd": total_gross,
        "mean_realized_net_per_fill_with_fills": finite_mean(net_values),
        "median_realized_net_per_fill_with_fills": finite_median(net_values),
        "mean_fee_per_fill_usd": finite_mean(fee_values),
        "diagnostics": diagnostics,
    }


def build_exit_capture_report(
    economics_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    rows_with_fills = [
        row
        for row in economics_rows
        if row.get("economics_complete") is not False
        and int_or_zero(row.get("fill_count")) > 0
    ]
    samples: list[dict[str, Any]] = []
    for row in rows_with_fills:
        fill_count = max(1, int_or_zero(row.get("fill_count")))
        fee_per_fill = float(row.get("fee_per_fill_usd") or 0.0)
        fee_bps = number_or_none(row.get("fee_bps_per_fill"))
        gross_per_fill = float(row.get("estimated_gross_per_fill_usd") or 0.0)
        net_per_fill = number_or_none(row.get("realized_net_per_fill"))
        path_mfe = number_or_none(row.get("segment_close_path_mfe"))
        path_efficiency = number_or_none(row.get("segment_close_path_efficiency"))
        if (
            fee_per_fill <= 0.0
            or fee_bps is None
            or fee_bps <= 0.0
            or path_mfe is None
            or path_mfe <= 0.0
        ):
            continue
        notional_per_fill = fee_per_fill / (fee_bps / 10_000.0)
        path_mfe_bps = path_mfe * 10_000.0
        path_mfe_potential_per_fill = path_mfe * notional_per_fill
        path_fee_coverage = safe_ratio(path_mfe_bps, fee_bps)
        gross_capture_ratio = safe_ratio(gross_per_fill, path_mfe_potential_per_fill)
        gross_fee_coverage = safe_ratio(gross_per_fill, fee_per_fill)
        if (
            path_fee_coverage is None
            or gross_capture_ratio is None
            or gross_fee_coverage is None
        ):
            continue
        samples.append(
            {
                "symbol": row.get("symbol"),
                "segment_index": row.get("segment_index"),
                "fill_count": fill_count,
                "net_per_fill_usd": net_per_fill,
                "gross_per_fill_usd": gross_per_fill,
                "fee_per_fill_usd": fee_per_fill,
                "fee_bps_per_fill": fee_bps,
                "segment_close_path_mfe": path_mfe,
                "segment_close_path_mfe_bps": path_mfe_bps,
                "segment_close_path_efficiency": path_efficiency,
                "estimated_notional_per_fill_usd": notional_per_fill,
                "path_mfe_potential_per_fill_usd": path_mfe_potential_per_fill,
                "path_fee_coverage_ratio": path_fee_coverage,
                "gross_capture_of_path_mfe": gross_capture_ratio,
                "gross_fee_coverage_ratio": gross_fee_coverage,
            }
        )

    path_fee_coverages = [
        float(item["path_fee_coverage_ratio"]) for item in samples
    ]
    gross_capture_ratios = [
        float(item["gross_capture_of_path_mfe"]) for item in samples
    ]
    gross_fee_coverages = [
        float(item["gross_fee_coverage_ratio"]) for item in samples
    ]
    fee_bps_values = [float(item["fee_bps_per_fill"]) for item in samples]
    path_mfe_bps_values = [
        float(item["segment_close_path_mfe_bps"]) for item in samples
    ]
    low_capture_count = sum(
        1
        for item in samples
        if float(item["path_fee_coverage_ratio"]) >= 2.0
        and float(item["gross_capture_of_path_mfe"]) < 0.35
    )
    fee_bound_count = sum(
        1 for item in samples if float(item["path_fee_coverage_ratio"]) < 1.25
    )
    gross_cost_bound_count = sum(
        1 for item in samples if float(item["gross_fee_coverage_ratio"]) < 1.0
    )

    proxy_diagnostics: list[str] = []
    if rows_with_fills and not samples:
        proxy_diagnostics.append("insufficient_exit_capture_samples")
    if samples and fee_bound_count >= max(1, math.ceil(len(samples) * 0.5)):
        proxy_diagnostics.append("path_mfe_does_not_cover_fee_buffer")
    if samples and low_capture_count >= max(1, math.ceil(len(samples) * 0.5)):
        proxy_diagnostics.append("path_mfe_covers_cost_but_gross_capture_low")
    if samples and gross_cost_bound_count >= max(1, math.ceil(len(samples) * 0.5)):
        proxy_diagnostics.append("gross_edge_does_not_cover_current_fee")

    live_sample_count = sum(
        int_or_zero(row.get("exit_capture_sample_count"))
        for row in rows_with_fills
    )
    live_low_count = sum(
        int_or_zero(row.get("exit_capture_low_count"))
        for row in rows_with_fills
    )

    def live_weighted_mean(field: str) -> float | None:
        weighted_sum = 0.0
        weight = 0
        for row in rows_with_fills:
            samples_in_row = int_or_zero(row.get("exit_capture_sample_count"))
            value = number_or_none(row.get(field))
            if samples_in_row <= 0 or value is None:
                continue
            weighted_sum += value * samples_in_row
            weight += samples_in_row
        return weighted_sum / weight if weight > 0 else None

    live_low_ratio = (
        float(live_low_count) / float(live_sample_count)
        if live_sample_count > 0
        else None
    )
    live_mean_capture = live_weighted_mean("exit_capture_mean_capture_ratio")
    live_mean_net_bps = live_weighted_mean("exit_capture_mean_captured_net_bps")
    live_mean_gross_bps = live_weighted_mean(
        "exit_capture_mean_captured_gross_bps"
    )
    live_mean_path_mfe_bps = live_weighted_mean("exit_capture_mean_path_mfe_bps")
    live_mean_fee_bps = live_weighted_mean("exit_capture_mean_fee_bps")

    authoritative_source = (
        "order_episode_runtime" if live_sample_count > 0 else "segment_path_proxy"
    )
    diagnostics: list[str] = []
    if live_sample_count > 0:
        if live_low_ratio is not None and live_low_ratio >= 0.50:
            diagnostics.append("path_mfe_covers_cost_but_gross_capture_low")
        if live_mean_net_bps is not None and live_mean_net_bps <= 0.0:
            diagnostics.append("captured_net_edge_non_positive")
    else:
        diagnostics.extend(proxy_diagnostics)

    if "path_mfe_covers_cost_but_gross_capture_low" in diagnostics:
        primary_diagnosis = "exit_capture_low"
    elif "path_mfe_does_not_cover_fee_buffer" in diagnostics:
        primary_diagnosis = "fee_bound_or_low_path"
    elif (
        "gross_edge_does_not_cover_current_fee" in diagnostics
        or "captured_net_edge_non_positive" in diagnostics
    ):
        primary_diagnosis = "execution_cost_bound"
    elif live_sample_count <= 0 and not samples:
        primary_diagnosis = "insufficient_samples"
    else:
        primary_diagnosis = "no_obvious_exit_capture_issue"

    authoritative_sample_count = (
        live_sample_count if live_sample_count > 0 else len(samples)
    )
    authoritative_low_count = (
        live_low_count if live_sample_count > 0 else low_capture_count
    )
    authoritative_mean_capture = (
        live_mean_capture
        if live_sample_count > 0
        else finite_mean(gross_capture_ratios)
    )

    return {
        "status": "action_required" if diagnostics else "pass",
        "primary_diagnosis": primary_diagnosis,
        "diagnostics": diagnostics,
        "authoritative_source": authoritative_source,
        "authoritative_sample_count": authoritative_sample_count,
        "authoritative_mean_capture_ratio": authoritative_mean_capture,
        "proxy_diagnostics": proxy_diagnostics,
        "filled_segment_count": len(rows_with_fills),
        "sample_count": authoritative_sample_count,
        "low_capture_segment_count": authoritative_low_count,
        "low_capture_sample_count": authoritative_low_count,
        "fee_bound_segment_count": fee_bound_count,
        "gross_cost_bound_segment_count": gross_cost_bound_count,
        "mean_path_fee_coverage_ratio": finite_mean(path_fee_coverages),
        "median_path_fee_coverage_ratio": finite_median(path_fee_coverages),
        "mean_gross_capture_of_path_mfe": authoritative_mean_capture,
        "median_gross_capture_of_path_mfe": (
            live_mean_capture
            if live_sample_count > 0
            else finite_median(gross_capture_ratios)
        ),
        "mean_gross_fee_coverage_ratio": finite_mean(gross_fee_coverages),
        "mean_fee_bps_per_fill": (
            live_mean_fee_bps
            if live_sample_count > 0
            else finite_mean(fee_bps_values)
        ),
        "mean_path_mfe_bps": (
            live_mean_path_mfe_bps
            if live_sample_count > 0
            else finite_mean(path_mfe_bps_values)
        ),
        "mean_captured_gross_bps": live_mean_gross_bps,
        "mean_captured_net_bps": live_mean_net_bps,
        "live_low_capture_ratio": live_low_ratio,
        "segment_proxy": {
            "sample_count": len(samples),
            "low_capture_segment_count": low_capture_count,
            "mean_gross_capture_of_path_mfe": finite_mean(gross_capture_ratios),
            "median_gross_capture_of_path_mfe": finite_median(
                gross_capture_ratios
            ),
            "mean_path_mfe_bps": finite_mean(path_mfe_bps_values),
            "mean_fee_bps_per_fill": finite_mean(fee_bps_values),
        },
        "samples": samples,
    }


def summarize_cost_adjusted_rows(
    economics_rows: list[dict[str, Any]],
    *,
    fee_multiplier: float,
    min_gross_over_adjusted_fee_per_fill_usd: float = float("-inf"),
) -> dict[str, Any]:
    selected_rows: list[dict[str, Any]] = []
    for row in economics_rows:
        if row.get("economics_complete") is False:
            continue
        fill_count = int_or_zero(row.get("fill_count"))
        if fill_count <= 0:
            continue
        gross = float(row.get("estimated_gross_pnl_usd") or 0.0)
        fee = float(row.get("fee_usd") or 0.0)
        funding = float(row.get("funding_paid_usd") or 0.0)
        gross_per_fill = gross / fill_count
        adjusted_fee_per_fill = fee * fee_multiplier / fill_count
        funding_per_fill = funding / fill_count
        edge_after_adjusted_fee = (
            gross_per_fill - funding_per_fill - adjusted_fee_per_fill
        )
        if edge_after_adjusted_fee >= min_gross_over_adjusted_fee_per_fill_usd:
            selected_rows.append(row)

    net_values: list[float] = []
    total_fills = 0
    total_gross = 0.0
    total_adjusted_fee = 0.0
    total_funding = 0.0
    for row in selected_rows:
        fill_count = int_or_zero(row.get("fill_count"))
        gross = float(row.get("estimated_gross_pnl_usd") or 0.0)
        adjusted_fee = float(row.get("fee_usd") or 0.0) * fee_multiplier
        funding = float(row.get("funding_paid_usd") or 0.0)
        adjusted_net = gross - funding - adjusted_fee
        total_fills += fill_count
        total_gross += gross
        total_adjusted_fee += adjusted_fee
        total_funding += funding
        if fill_count > 0:
            net_values.append(adjusted_net / fill_count)

    positive_rows = sum(1 for value in net_values if value > 1e-12)
    negative_rows = sum(1 for value in net_values if value < -1e-12)
    zero_rows = sum(1 for value in net_values if abs(value) <= 1e-12)
    return {
        "selected_segment_count": len(selected_rows),
        "total_fills": total_fills,
        "fee_multiplier": float(fee_multiplier),
        "min_gross_over_adjusted_fee_per_fill_usd": (
            None
            if not math.isfinite(min_gross_over_adjusted_fee_per_fill_usd)
            else float(min_gross_over_adjusted_fee_per_fill_usd)
        ),
        "total_estimated_gross_pnl_usd": total_gross,
        "total_adjusted_fee_usd": total_adjusted_fee,
        "total_funding_paid_usd": total_funding,
        "total_adjusted_realized_net_usd_est": (
            total_gross - total_funding - total_adjusted_fee
        ),
        "mean_adjusted_realized_net_per_fill": finite_mean(net_values),
        "median_adjusted_realized_net_per_fill": finite_median(net_values),
        "positive_segments": positive_rows,
        "negative_segments": negative_rows,
        "zero_segments": zero_rows,
    }


def build_execution_cost_plan(
    economics_rows: list[dict[str, Any]],
    *,
    min_total_fills: int,
    min_mean_realized_net_per_fill: float,
    exit_capture: dict[str, Any],
) -> dict[str, Any]:
    filled_rows = [
        row
        for row in economics_rows
        if row.get("economics_complete") is not False
        and int_or_zero(row.get("fill_count")) > 0
    ]
    if not filled_rows:
        return {
            "status": "fail",
            "activation_recommendation": "block",
            "primary_action": "collect_execution_fills",
            "diagnostics": ["no_filled_segments"],
            "filled_segment_count": 0,
            "total_fills": 0,
            "candidate_plans": [],
        }

    total_fills = sum(int_or_zero(row.get("fill_count")) for row in filled_rows)
    total_gross = sum(float(row.get("estimated_gross_pnl_usd") or 0.0) for row in filled_rows)
    total_fee = sum(float(row.get("fee_usd") or 0.0) for row in filled_rows)
    total_funding = sum(
        float(row.get("funding_paid_usd") or 0.0) for row in filled_rows
    )
    total_net_before_fee = total_gross - total_funding
    current_net_per_fill = safe_ratio(
        total_net_before_fee - total_fee, float(total_fills)
    )
    break_even_fee_multiplier = safe_ratio(total_net_before_fee, total_fee)
    fee_reduction_required_pct = None
    if break_even_fee_multiplier is not None and break_even_fee_multiplier < 1.0:
        fee_reduction_required_pct = (1.0 - break_even_fee_multiplier) * 100.0

    candidate_specs = [
        (
            "current_cost",
            1.0,
            False,
            "当前 replay 成本口径",
        ),
        (
            "maker_first_fee_x0.75",
            0.75,
            True,
            "候选：优先 maker / 降低 taker 暴露后按 75% 当前费用复算",
        ),
        (
            "maker_first_fee_x0.5",
            0.50,
            True,
            "候选：激进 maker-only / 更低交易成本假设，需重跑验证",
        ),
    ]
    candidate_plans: list[dict[str, Any]] = []
    for name, fee_multiplier, requires_rerun, description in candidate_specs:
        scenario = summarize_cost_adjusted_rows(
            filled_rows,
            fee_multiplier=fee_multiplier,
        )
        mean_net = scenario.get("mean_adjusted_realized_net_per_fill")
        scenario_status = (
            "pass"
            if int(scenario["total_fills"]) >= max(1, min_total_fills)
            and isinstance(mean_net, (int, float))
            and float(mean_net) >= float(min_mean_realized_net_per_fill)
            and int(scenario["positive_segments"]) > 0
            else "fail"
        )
        candidate_plans.append(
            {
                **scenario,
                "name": name,
                "description": description,
                "requires_rerun": bool(requires_rerun),
                "status": scenario_status,
            }
        )

    current_plan = candidate_plans[0]
    deployable_candidates = [
        item for item in candidate_plans[1:] if item.get("status") == "pass"
    ]
    diagnostics: list[str] = []
    if current_plan.get("status") != "pass":
        diagnostics.append("current_cost_not_deployable")
    if fee_reduction_required_pct is not None:
        diagnostics.append("fee_reduction_required")
    if deployable_candidates:
        diagnostics.append("lower_cost_execution_candidate_requires_rerun")
    else:
        diagnostics.append("no_lower_cost_execution_candidate_positive")

    exit_primary = str(exit_capture.get("primary_diagnosis") or "")
    if exit_primary and exit_primary not in {"no_obvious_exit_capture_issue", "insufficient_samples"}:
        diagnostics.append(f"exit_capture:{exit_primary}")

    if current_plan.get("status") == "pass":
        status = "pass"
        primary_action = "allow_current_cost_after_registry_gate"
        activation_recommendation = "allow"
    elif deployable_candidates:
        status = "candidate_requires_rerun"
        primary_action = "rerun_replay_with_lower_cost_execution"
        activation_recommendation = "block_until_rerun_passes"
    elif exit_primary == "exit_capture_low":
        status = "fail"
        primary_action = "improve_exit_capture_before_more_fee_tuning"
        activation_recommendation = "block"
    else:
        status = "fail"
        primary_action = "raise_edge_or_reduce_turnover_before_activation"
        activation_recommendation = "block"

    return {
        "status": status,
        "activation_recommendation": activation_recommendation,
        "primary_action": primary_action,
        "diagnostics": diagnostics,
        "filled_segment_count": len(filled_rows),
        "total_fills": total_fills,
        "total_estimated_gross_pnl_usd": total_gross,
        "total_fee_usd": total_fee,
        "total_funding_paid_usd": total_funding,
        "total_estimated_net_before_fee_usd": total_net_before_fee,
        "current_net_per_fill_usd": current_net_per_fill,
        "break_even_fee_multiplier": break_even_fee_multiplier,
        "fee_reduction_required_pct": fee_reduction_required_pct,
        "candidate_plan_count": len(candidate_plans),
        "deployable_candidate_requires_rerun_count": len(deployable_candidates),
        "best_candidate": deployable_candidates[0] if deployable_candidates else current_plan,
        "candidate_plans": candidate_plans,
    }


def build_cost_sensitivity_report(
    economics_rows: list[dict[str, Any]],
    *,
    min_total_fills: int,
    min_mean_realized_net_per_fill: float,
) -> dict[str, Any]:
    filled_rows = [
        row
        for row in economics_rows
        if row.get("economics_complete") is not False
        and int_or_zero(row.get("fill_count")) > 0
    ]
    total_fee = sum(float(row.get("fee_usd") or 0.0) for row in filled_rows)
    total_funding = sum(
        float(row.get("funding_paid_usd") or 0.0) for row in filled_rows
    )
    total_gross = sum(
        float(row.get("estimated_gross_pnl_usd") or 0.0) for row in filled_rows
    )
    total_net_before_fee = total_gross - total_funding
    break_even_fee_multiplier = (
        total_net_before_fee / total_fee if total_fee > 1e-12 else None
    )

    scenarios: list[dict[str, Any]] = []
    for fee_multiplier in (0.0, 0.25, 0.50, 0.75, 1.0, 1.25):
        scenario = summarize_cost_adjusted_rows(
            filled_rows,
            fee_multiplier=fee_multiplier,
        )
        mean_net = scenario.get("mean_adjusted_realized_net_per_fill")
        scenario["name"] = f"fee_x{fee_multiplier:g}"
        scenario["description"] = "仅调整费用倍率，不过滤片段"
        scenario["diagnostic_only"] = True
        scenario["status"] = (
            "pass"
            if int(scenario["total_fills"]) >= max(1, min_total_fills)
            and isinstance(mean_net, (int, float))
            and float(mean_net) >= float(min_mean_realized_net_per_fill)
            and int(scenario["positive_segments"]) > 0
            else "fail"
        )
        scenarios.append(scenario)

    for margin_name, margin in (
        ("gross_gt_adjusted_fee", 0.0),
        ("gross_gt_adjusted_fee_plus_25pct_fee", 0.25),
        ("gross_gt_adjusted_fee_plus_50pct_fee", 0.50),
    ):
        for fee_multiplier in (1.0, 0.5):
            selected_rows = []
            for row in filled_rows:
                fill_count = int_or_zero(row.get("fill_count"))
                if fill_count <= 0:
                    continue
                adjusted_fee_per_fill = (
                    float(row.get("fee_usd") or 0.0) * fee_multiplier / fill_count
                )
                min_edge = adjusted_fee_per_fill * margin
                funding_per_fill = (
                    float(row.get("funding_paid_usd") or 0.0) / fill_count
                )
                gross_per_fill = (
                    float(row.get("estimated_gross_pnl_usd") or 0.0) / fill_count
                )
                if (
                    gross_per_fill - funding_per_fill - adjusted_fee_per_fill
                    >= min_edge
                ):
                    selected_rows.append(row)
            scenario = summarize_cost_adjusted_rows(
                selected_rows,
                fee_multiplier=fee_multiplier,
            )
            mean_net = scenario.get("mean_adjusted_realized_net_per_fill")
            scenario["name"] = f"{margin_name}_fee_x{fee_multiplier:g}"
            scenario["description"] = (
                "按估算 gross edge 覆盖调整后费用的幅度过滤片段"
            )
            scenario["diagnostic_only"] = True
            scenario["status"] = (
                "pass"
                if int(scenario["total_fills"]) >= max(1, min_total_fills)
                and isinstance(mean_net, (int, float))
                and float(mean_net) >= float(min_mean_realized_net_per_fill)
                and int(scenario["positive_segments"]) > 0
                else "fail"
            )
            scenarios.append(scenario)

    pass_scenarios = [item for item in scenarios if item.get("status") == "pass"]
    diagnostics: list[str] = []
    if filled_rows and break_even_fee_multiplier is not None:
        if break_even_fee_multiplier < 1.0:
            diagnostics.append("current_cost_above_break_even")
        if break_even_fee_multiplier < 0.5:
            diagnostics.append("requires_large_fee_reduction")
    if not pass_scenarios:
        diagnostics.append("no_cost_sensitivity_scenario_positive")
    current_cost_scenario = next(
        (item for item in scenarios if item.get("name") == "fee_x1"),
        None,
    )
    return {
        "status": "diagnostic_pass" if pass_scenarios else "fail",
        "current_cost_status": (
            current_cost_scenario.get("status")
            if isinstance(current_cost_scenario, dict)
            else "unknown"
        ),
        "diagnostics": diagnostics,
        "filled_segment_count": len(filled_rows),
        "total_estimated_gross_pnl_usd": total_gross,
        "total_fee_usd": total_fee,
        "total_funding_paid_usd": total_funding,
        "total_estimated_net_before_fee_usd": total_net_before_fee,
        "break_even_fee_multiplier": break_even_fee_multiplier,
        "pass_scenario_count": len(pass_scenarios),
        "scenarios": scenarios,
    }


def evaluate_replay_policy(
    name: str,
    description: str,
    run_summaries: list[dict[str, Any]],
    filters: dict[str, Any],
    *,
    diagnostic_only: bool,
    min_execution_active_runs: int,
    min_execution_pass_runs: int,
    min_total_fills: int,
    min_mean_realized_net_per_fill: float,
    min_break_even_fee_multiplier: float,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    for run in run_summaries:
        summary = run.get("assess_summary", {})
        segment = run.get("segment", {})
        if not isinstance(summary, dict):
            summary = {}
        if not isinstance(segment, dict):
            segment = {}
        keep = True
        for key, threshold in filters.items():
            if key == "min_strength_score":
                keep = keep and (
                    (number_or_none(segment.get("strength_score")) or float("-inf"))
                    >= float(threshold)
                )
            elif key == "min_liquidity_score":
                keep = keep and (
                    (number_or_none(segment.get("liquidity_score")) or float("-inf"))
                    >= float(threshold)
                )
            elif key == "max_avg_range_pct":
                keep = keep and (
                    (number_or_none(segment.get("avg_range_pct")) or float("inf"))
                    <= float(threshold)
                )
            elif key == "max_avg_vol_12":
                keep = keep and (
                    (number_or_none(segment.get("avg_vol_12")) or float("inf"))
                    <= float(threshold)
                )
            elif key == "max_filtered_cost_ratio_avg":
                keep = keep and (
                    (
                        number_or_none(summary.get("filtered_cost_ratio_avg"))
                        or float("inf")
                    )
                    <= float(threshold)
                )
            elif key == "execution_active_only":
                keep = keep and summary.get("runtime_validation_mode") == "EXECUTION_ACTIVE"
        if keep:
            selected.append(run)

    aggregate_summary, aggregate_validation = aggregate_run_summaries(
        selected,
        min_execution_active_runs=min_execution_active_runs,
        min_execution_pass_runs=min_execution_pass_runs,
        min_total_fills=min_total_fills,
        min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
        warn_mean_filtered_cost_ratio=1.0,
    )
    economics = [
        run.get("economics_attribution") or build_run_economics_attribution(run)
        for run in selected
    ]
    economics_summary = summarize_economics_attribution(economics)
    total_gross = float(economics_summary.get("total_estimated_gross_pnl_usd") or 0.0)
    total_fee = float(economics_summary.get("total_fee_usd") or 0.0)
    total_funding = float(
        economics_summary.get("total_funding_paid_usd") or 0.0
    )
    total_net_before_fee = total_gross - total_funding
    break_even_fee_multiplier = (
        1_000_000_000.0
        if abs(total_fee) <= 1e-12 and total_net_before_fee > 0.0
        else safe_ratio(total_net_before_fee, total_fee)
    )
    fee_stress = summarize_cost_adjusted_rows(
        economics,
        fee_multiplier=float(min_break_even_fee_multiplier),
    )
    fee_stress_mean_net = fee_stress.get("mean_adjusted_realized_net_per_fill")
    fee_stress_status = (
        "pass"
        if int(fee_stress.get("total_fills") or 0) >= max(1, min_total_fills)
        and isinstance(fee_stress_mean_net, (int, float))
        and float(fee_stress_mean_net) >= float(min_mean_realized_net_per_fill)
        and int(fee_stress.get("positive_segments") or 0) > 0
        else "fail"
    )
    validation_status = str(aggregate_validation.get("status", "")).lower()
    candidate_fail_reasons: list[str] = []
    if (
        break_even_fee_multiplier is None
        or float(break_even_fee_multiplier) < float(min_break_even_fee_multiplier)
    ):
        candidate_fail_reasons.append(
            "break_even_fee_multiplier "
            f"{break_even_fee_multiplier} < min_break_even_fee_multiplier={float(min_break_even_fee_multiplier)}"
        )
    if fee_stress_status != "pass":
        candidate_fail_reasons.append(
            f"fee_x{float(min_break_even_fee_multiplier):g}_stress_status={fee_stress_status}"
        )
    pass_status = (
        validation_status in {"pass", "pass_with_actions"}
        and not aggregate_validation.get("quality_fail_reasons")
        and bool(aggregate_validation.get("minimum_coverage_targets_met"))
        and int(aggregate_summary.get("total_fills") or 0) >= max(1, min_total_fills)
        and (
            aggregate_summary.get("mean_realized_net_per_fill_with_fills")
            is not None
        )
        and float(aggregate_summary["mean_realized_net_per_fill_with_fills"])
        >= float(min_mean_realized_net_per_fill)
        and int(aggregate_summary.get("positive_realized_net_with_fills_runs") or 0)
        > 0
        and not candidate_fail_reasons
    )
    return {
        "name": name,
        "description": description,
        "diagnostic_only": bool(diagnostic_only),
        "filters": filters,
        "status": "pass" if pass_status else "fail",
        "fail_reasons": []
        if pass_status
        else list(
            dict.fromkeys(
                [
                    *(
                        str(item)
                        for item in aggregate_validation.get("fail_reasons", [])
                        if str(item).strip()
                    ),
                    *candidate_fail_reasons,
                ]
            )
        ),
        "selected_segments": len(selected),
        "aggregate_summary": aggregate_summary,
        "aggregate_validation": aggregate_validation,
        "economics_summary": economics_summary,
        "cost_stress": {
            **fee_stress,
            "name": f"fee_x{float(min_break_even_fee_multiplier):g}",
            "status": fee_stress_status,
        },
        "break_even_fee_multiplier": break_even_fee_multiplier,
        "deployable_config": {
            "candidate_name": name,
            "filters": filters,
            # Any filter selected from this evaluation corpus is a discovery
            # result, not promotion evidence. It must be materialized into the
            # runtime policy and replayed on an untouched corpus first.
            "requires_rerun": bool(filters),
            "min_break_even_fee_multiplier": float(min_break_even_fee_multiplier),
        },
    }


def build_replay_execution_optimizer(
    run_summaries: list[dict[str, Any]],
    *,
    min_execution_active_runs: int,
    min_execution_pass_runs: int,
    min_total_fills: int,
    min_mean_realized_net_per_fill: float,
    min_break_even_fee_multiplier: float,
) -> dict[str, Any]:
    if not run_summaries:
        return {
            "status": "fail",
            "fail_reasons": ["no_replay_runs"],
            "warn_reasons": [],
            "min_break_even_fee_multiplier": float(min_break_even_fee_multiplier),
            "candidate_count": 0,
            "pass_candidate_count": 0,
            "diagnostic_pass_candidate_count": 0,
            "best_candidate": None,
            "candidates": [],
        }

    strength_q50 = quantile_from_runs(run_summaries, ("segment", "strength_score"), 0.50)
    strength_q75 = quantile_from_runs(run_summaries, ("segment", "strength_score"), 0.75)
    liquidity_q50 = quantile_from_runs(run_summaries, ("segment", "liquidity_score"), 0.50)
    range_q50 = quantile_from_runs(run_summaries, ("segment", "avg_range_pct"), 0.50)
    vol_q50 = quantile_from_runs(run_summaries, ("segment", "avg_vol_12"), 0.50)
    filtered_cost_q50 = quantile_from_runs(
        run_summaries, ("assess_summary", "filtered_cost_ratio_avg"), 0.50
    )

    policy_specs: list[tuple[str, str, dict[str, Any], bool]] = [
        (
            "baseline_all",
            "当前 replay 配置，不过滤任何片段",
            {},
            False,
        ),
    ]
    if strength_q50 is not None:
        policy_specs.append(
            (
                "trend_strength_q50",
                "仅保留趋势强度不低于本轮中位数的片段",
                {"min_strength_score": strength_q50},
                False,
            )
        )
    if strength_q75 is not None:
        policy_specs.append(
            (
                "trend_strength_q75",
                "仅保留趋势强度不低于本轮 75 分位的片段",
                {"min_strength_score": strength_q75},
                False,
            )
        )
    if liquidity_q50 is not None:
        policy_specs.append(
            (
                "liquidity_q50",
                "仅保留流动性不低于本轮中位数的片段",
                {"min_liquidity_score": liquidity_q50},
                False,
            )
        )
    if range_q50 is not None:
        policy_specs.append(
            (
                "quiet_range_q50",
                "仅保留平均区间波动不高于本轮中位数的片段",
                {"max_avg_range_pct": range_q50},
                False,
            )
        )
    if vol_q50 is not None:
        policy_specs.append(
            (
                "low_vol_q50",
                "仅保留短期波动不高于本轮中位数的片段",
                {"max_avg_vol_12": vol_q50},
                False,
            )
        )
    if strength_q50 is not None and liquidity_q50 is not None:
        policy_specs.append(
            (
                "strong_liquid_q50",
                "趋势强度和流动性同时不低于中位数",
                {
                    "min_strength_score": strength_q50,
                    "min_liquidity_score": liquidity_q50,
                },
                False,
            )
        )
    if strength_q50 is not None and range_q50 is not None:
        policy_specs.append(
            (
                "strong_quiet_q50",
                "趋势强度不低于中位数，同时区间波动不高于中位数",
                {
                    "min_strength_score": strength_q50,
                    "max_avg_range_pct": range_q50,
                },
                False,
            )
        )
    if filtered_cost_q50 is not None:
        policy_specs.append(
            (
                "diagnostic_low_cost_q50",
                "诊断项：仅保留事后 filtered_cost_ratio 不高于中位数的片段",
                {"max_filtered_cost_ratio_avg": filtered_cost_q50},
                True,
            )
        )
        policy_specs.append(
            (
                "diagnostic_execution_active_low_cost_q50",
                "诊断项：仅保留已经进入 EXECUTION_ACTIVE 且 filtered_cost_ratio 较低的片段",
                {
                    "execution_active_only": True,
                    "max_filtered_cost_ratio_avg": filtered_cost_q50,
                },
                True,
            )
        )

    candidates = [
        evaluate_replay_policy(
            name,
            description,
            run_summaries,
            filters,
            diagnostic_only=diagnostic_only,
            min_execution_active_runs=min_execution_active_runs,
            min_execution_pass_runs=min_execution_pass_runs,
            min_total_fills=min_total_fills,
            min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
            min_break_even_fee_multiplier=min_break_even_fee_multiplier,
        )
        for name, description, filters, diagnostic_only in policy_specs
    ]
    deployable_candidates = [
        candidate
        for candidate in candidates
        if not candidate["diagnostic_only"]
        and not bool(candidate.get("deployable_config", {}).get("requires_rerun"))
    ]
    rerun_candidates = [
        candidate
        for candidate in candidates
        if not candidate["diagnostic_only"]
        and bool(candidate.get("deployable_config", {}).get("requires_rerun"))
    ]
    pass_candidates = [
        candidate
        for candidate in deployable_candidates
        if candidate.get("status") == "pass"
    ]
    diagnostic_pass_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("diagnostic_only") and candidate.get("status") == "pass"
    ]

    def candidate_rank(candidate: dict[str, Any]) -> tuple[float, int, int]:
        summary = candidate.get("aggregate_summary", {})
        mean_net = summary.get("mean_realized_net_per_fill_with_fills")
        if not isinstance(mean_net, (int, float)):
            mean_net = float("-inf")
        return (
            float(mean_net),
            int(summary.get("total_fills") or 0),
            int(candidate.get("selected_segments") or 0),
        )

    ranked_candidates = sorted(
        candidates,
        key=candidate_rank,
        reverse=True,
    )
    fail_reasons: list[str] = []
    warn_reasons: list[str] = []
    if not pass_candidates:
        fail_reasons.append(
            "no_deployable_prefilter_candidate_positive_after_costs"
        )
    if diagnostic_pass_candidates and not pass_candidates:
        warn_reasons.append(
            "only_diagnostic_post_run_filters_found_positive; do not promote without rerun"
        )
    if any(candidate.get("status") == "pass" for candidate in rerun_candidates):
        warn_reasons.append(
            "filtered_candidate_found_but_requires_materialization_and_independent_rerun"
        )
    if pass_candidates:
        warn_reasons.append(
            f"deployable_candidates_pass_fee_stress_x{float(min_break_even_fee_multiplier):g}"
        )
    return {
        "status": "pass" if pass_candidates else "fail",
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "min_break_even_fee_multiplier": float(min_break_even_fee_multiplier),
        "candidate_count": len(candidates),
        "deployable_candidate_count": len(deployable_candidates),
        "candidate_requires_rerun_count": len(rerun_candidates),
        "pass_candidate_count": len(pass_candidates),
        "diagnostic_pass_candidate_count": len(diagnostic_pass_candidates),
        "best_candidate": ranked_candidates[0] if ranked_candidates else None,
        "best_deployable_candidate": sorted(
            deployable_candidates,
            key=candidate_rank,
            reverse=True,
        )[0]
        if deployable_candidates
        else None,
        "best_candidate_requiring_rerun": sorted(
            rerun_candidates,
            key=candidate_rank,
            reverse=True,
        )[0]
        if rerun_candidates
        else None,
        "candidates": candidates,
    }


def build_replay_economics_report(
    run_summaries: list[dict[str, Any]],
    *,
    min_execution_active_runs: int,
    min_execution_pass_runs: int,
    min_total_fills: int,
    min_mean_realized_net_per_fill: float,
    min_break_even_fee_multiplier: float,
) -> dict[str, Any]:
    economics_rows = [
        run.get("economics_attribution") or build_run_economics_attribution(run)
        for run in run_summaries
    ]
    exit_capture = build_exit_capture_report(economics_rows)
    return {
        "attribution_summary": summarize_economics_attribution(economics_rows),
        "cost_sensitivity": build_cost_sensitivity_report(
            economics_rows,
            min_total_fills=min_total_fills,
            min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
        ),
        "exit_capture": exit_capture,
        "execution_cost_plan": build_execution_cost_plan(
            economics_rows,
            min_total_fills=min_total_fills,
            min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
            exit_capture=exit_capture,
        ),
        "runs": economics_rows,
        "optimizer": build_replay_execution_optimizer(
            run_summaries,
            min_execution_active_runs=min_execution_active_runs,
            min_execution_pass_runs=min_execution_pass_runs,
            min_total_fills=min_total_fills,
            min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
            min_break_even_fee_multiplier=min_break_even_fee_multiplier,
        ),
    }


def select_deployable_optimizer_candidate(optimizer: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(optimizer, dict):
        return None
    if str(optimizer.get("status", "")).strip().lower() != "pass":
        return None
    candidate = optimizer.get("best_deployable_candidate")
    if not isinstance(candidate, dict):
        candidate = optimizer.get("best_candidate")
    if not isinstance(candidate, dict):
        return None
    if candidate.get("diagnostic_only"):
        return None
    deployable_config = candidate.get("deployable_config", {})
    if not isinstance(deployable_config, dict) or bool(
        deployable_config.get("requires_rerun")
    ):
        return None
    if str(candidate.get("status", "")).strip().lower() != "pass":
        return None
    return candidate


def build_activation_gate_report(
    *,
    aggregate_validation: dict[str, Any],
    economics_report: dict[str, Any],
    symbol_reports: dict[str, dict[str, Any]],
    source_symbol: str,
) -> dict[str, Any]:
    fail_reasons: list[str] = []
    warn_reasons: list[str] = []

    optimizer = economics_report.get("optimizer", {})
    if not isinstance(optimizer, dict):
        optimizer = {}
    selected_candidate = select_deployable_optimizer_candidate(optimizer)

    aggregate_status = str(aggregate_validation.get("status", "")).strip().lower()
    aggregate_fail_reasons = [
        str(item).strip()
        for item in aggregate_validation.get("fail_reasons", [])
        if str(item).strip()
    ]
    if aggregate_status == "fail":
        if not aggregate_fail_reasons:
            aggregate_fail_reasons.append("aggregate_validation.status=fail")
        fail_reasons.extend(aggregate_fail_reasons)
    elif aggregate_status == "pass_with_actions":
        warn_reasons.extend(
            str(item)
            for item in aggregate_validation.get("warn_reasons", [])
            if str(item).strip()
        )

    if str(optimizer.get("status", "")).strip().lower() == "fail":
        fail_reasons.append("execution_optimizer.status=fail")

    cost_plan = economics_report.get("execution_cost_plan", {})
    if not isinstance(cost_plan, dict):
        cost_plan = {}
    cost_plan_status = str(cost_plan.get("status", "")).strip().lower()
    if cost_plan_status == "fail":
        fail_reasons.append("execution_cost_plan.status=fail")
    elif cost_plan_status == "candidate_requires_rerun":
        warn_reasons.append(
            "execution_cost_plan.candidate_requires_rerun: lower-cost candidate needs replay rerun"
        )

    tradeability = aggregate_validation.get("symbol_tradeability", {})
    if not isinstance(tradeability, dict):
        tradeability = {}
    critical_symbols = {
        str(item).strip().upper()
        for item in symbol_reports
        if str(item).strip()
    }
    source_symbol_normalized = str(source_symbol or "").strip().upper()
    if source_symbol_normalized:
        critical_symbols.add(source_symbol_normalized)

    if critical_symbols:
        for symbol in sorted(critical_symbols):
            symbol_report = symbol_reports.get(symbol, {})
            if not isinstance(symbol_report, dict):
                continue
            exit_capture = symbol_report.get("exit_capture", {})
            if not isinstance(exit_capture, dict):
                continue
            sample_count = int_or_zero(exit_capture.get("sample_count"))
            if sample_count <= 0:
                continue
            primary = str(exit_capture.get("primary_diagnosis", "")).strip()
            mean_capture = number_or_none(
                exit_capture.get("mean_gross_capture_of_path_mfe")
            )
            if primary == "exit_capture_low":
                fail_reasons.append(f"{symbol}: exit_capture_low")
            if mean_capture is not None and mean_capture < 0.10:
                reason = (
                    f"{symbol}: mean_gross_capture_of_path_mfe={mean_capture:.6f} < 0.100000"
                )
                fail_reasons.append(reason)
    else:
        exit_capture = economics_report.get("exit_capture", {})
        if isinstance(exit_capture, dict) and int_or_zero(exit_capture.get("sample_count")) > 0:
            primary = str(exit_capture.get("primary_diagnosis", "")).strip()
            mean_capture = number_or_none(
                exit_capture.get("mean_gross_capture_of_path_mfe")
            )
            if primary == "exit_capture_low":
                fail_reasons.append("exit_capture_low")
            if mean_capture is not None and mean_capture < 0.10:
                reason = f"mean_gross_capture_of_path_mfe={mean_capture:.6f} < 0.100000"
                fail_reasons.append(reason)

    status = "pass"
    if fail_reasons:
        status = "fail"
    elif warn_reasons:
        status = "pass_with_actions"
    return {
        "status": status,
        "fail_reasons": list(dict.fromkeys(fail_reasons)),
        "warn_reasons": list(dict.fromkeys(warn_reasons)),
        "basis": "aggregate_validation",
        "selected_candidate": selected_candidate,
        "raw_aggregate_status": aggregate_status,
        "raw_aggregate_fail_reasons": aggregate_fail_reasons,
    }


def finite_mean(values: list[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return None
    return float(sum(finite) / len(finite))


def finite_median(values: list[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return None
    return float(statistics.median(finite))


def derive_recommended_coverage_thresholds(
    *,
    min_execution_active_runs: int,
    min_execution_pass_runs: int,
    min_total_fills: int,
) -> dict[str, int]:
    return {
        "min_execution_active_runs": max(
            max(1, min_execution_active_runs),
            MIN_RECOMMENDED_EXECUTION_ACTIVE_RUNS,
        ),
        "min_execution_pass_runs": max(
            max(1, min_execution_pass_runs),
            MIN_RECOMMENDED_EXECUTION_PASS_RUNS,
        ),
        "min_total_fills": max(max(1, min_total_fills), MIN_RECOMMENDED_TOTAL_FILLS),
    }


def aggregate_run_summaries(
    run_summaries: list[dict[str, Any]],
    *,
    min_execution_active_runs: int,
    min_execution_pass_runs: int,
    min_total_fills: int,
    min_mean_realized_net_per_fill: float,
    warn_mean_filtered_cost_ratio: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    summaries = [run.get("assess_summary", {}) for run in run_summaries]
    economics_rows = [
        run.get("economics_attribution") or build_run_economics_attribution(run)
        for run in run_summaries
    ]
    complete_economics_rows = [
        row for row in economics_rows if row.get("economics_complete") is True
    ]
    incomplete_economics_rows = [
        row for row in economics_rows if row.get("economics_complete") is not True
    ]
    recommended_thresholds = derive_recommended_coverage_thresholds(
        min_execution_active_runs=min_execution_active_runs,
        min_execution_pass_runs=min_execution_pass_runs,
        min_total_fills=min_total_fills,
    )
    execution_active_runs = sum(
        1 for summary in summaries if summary.get("runtime_validation_mode") == "EXECUTION_ACTIVE"
    )
    execution_pass_runs = sum(1 for summary in summaries if summary.get("execution_status") == "PASS")
    protection_pass_runs = sum(1 for summary in summaries if summary.get("protection_status") == "PASS")
    trend_present_runs = sum(
        1 for summary in summaries if summary.get("market_context_status") == "TREND_PRESENT"
    )
    pass_with_actions_runs = sum(1 for summary in summaries if summary.get("verdict") == "PASS_WITH_ACTIONS")
    failed_runs = sum(1 for summary in summaries if summary.get("verdict") == "FAIL")
    total_execution_activity_count = sum(
        int(summary.get("execution_activity_count") or 0) for summary in summaries
    )
    total_fills = sum(
        int_or_zero(row.get("fill_count")) for row in complete_economics_rows
    )
    total_realized_net_usd_est = sum(
        float(row.get("realized_net_usd") or 0.0)
        for row in complete_economics_rows
    )
    weighted_realized_net_per_fill = (
        total_realized_net_usd_est / total_fills if total_fills > 0 else None
    )
    realized_net_values = [
        float(row["realized_net_per_fill"])
        for row in complete_economics_rows
        if isinstance(row.get("realized_net_per_fill"), (int, float))
    ]
    realized_net_values_with_fills = [
        float(row["realized_net_per_fill"])
        for row in complete_economics_rows
        if int_or_zero(row.get("fill_count")) > 0
        and isinstance(row.get("realized_net_per_fill"), (int, float))
    ]
    zero_realized_net_with_fills_runs = sum(
        1
        for row in complete_economics_rows
        if int_or_zero(row.get("fill_count")) > 0
        and isinstance(row.get("realized_net_per_fill"), (int, float))
        and abs(float(row["realized_net_per_fill"])) <= 1e-12
    )
    nonzero_realized_net_with_fills_runs = sum(
        1
        for row in complete_economics_rows
        if int_or_zero(row.get("fill_count")) > 0
        and isinstance(row.get("realized_net_per_fill"), (int, float))
        and abs(float(row["realized_net_per_fill"])) > 1e-12
    )
    positive_realized_net_with_fills_runs = sum(
        1 for value in realized_net_values_with_fills if value > 1e-12
    )
    negative_realized_net_with_fills_runs = sum(
        1 for value in realized_net_values_with_fills if value < -1e-12
    )
    filled_realized_net_runs = (
        positive_realized_net_with_fills_runs
        + negative_realized_net_with_fills_runs
        + zero_realized_net_with_fills_runs
    )
    positive_filled_segment_ratio = safe_ratio(
        positive_realized_net_with_fills_runs,
        filled_realized_net_runs,
    )
    filtered_cost_values = [
        float(summary["filtered_cost_ratio_avg"])
        for summary in summaries
        if isinstance(summary.get("filtered_cost_ratio_avg"), (int, float))
    ]
    aggregate_summary = {
        "segment_count": len(run_summaries),
        "economics_complete_runs": len(complete_economics_rows),
        "economics_incomplete_runs": len(incomplete_economics_rows),
        "execution_active_runs": execution_active_runs,
        "execution_pass_runs": execution_pass_runs,
        "protection_pass_runs": protection_pass_runs,
        "trend_present_runs": trend_present_runs,
        "pass_with_actions_runs": pass_with_actions_runs,
        "failed_runs": failed_runs,
        "total_execution_activity_count": total_execution_activity_count,
        "total_fills": total_fills,
        "mean_realized_net_per_fill": weighted_realized_net_per_fill,
        "segment_mean_realized_net_per_fill": finite_mean(realized_net_values),
        "total_realized_net_usd_est": total_realized_net_usd_est,
        "aggregation_weight": "fill_count",
        "median_realized_net_per_fill": finite_median(realized_net_values),
        "mean_realized_net_per_fill_with_fills": finite_mean(
            realized_net_values_with_fills
        ),
        "median_realized_net_per_fill_with_fills": finite_median(
            realized_net_values_with_fills
        ),
        "zero_realized_net_with_fills_runs": zero_realized_net_with_fills_runs,
        "nonzero_realized_net_with_fills_runs": nonzero_realized_net_with_fills_runs,
        "positive_realized_net_with_fills_runs": positive_realized_net_with_fills_runs,
        "negative_realized_net_with_fills_runs": negative_realized_net_with_fills_runs,
        "filled_realized_net_runs": filled_realized_net_runs,
        "positive_filled_segment_ratio": positive_filled_segment_ratio,
        "mean_filtered_cost_ratio_avg": finite_mean(filtered_cost_values),
        "max_filtered_cost_ratio_avg": max(filtered_cost_values) if filtered_cost_values else None,
    }

    fail_reasons: list[str] = []
    warn_reasons: list[str] = []
    coverage_fail_reasons: list[str] = []
    quality_fail_reasons: list[str] = []

    minimum_thresholds = {
        "min_execution_active_runs": max(1, min_execution_active_runs),
        "min_execution_pass_runs": max(1, min_execution_pass_runs),
        "min_total_fills": max(1, min_total_fills),
        "min_mean_realized_net_per_fill": float(min_mean_realized_net_per_fill),
        "min_positive_filled_segment_ratio": MIN_POSITIVE_FILLED_SEGMENT_RATIO,
        "warn_mean_filtered_cost_ratio": float(warn_mean_filtered_cost_ratio),
    }
    recommended_coverage_targets_met = has_met_replay_coverage_targets(
        aggregate_summary,
        min_execution_active_runs=recommended_thresholds["min_execution_active_runs"],
        min_execution_pass_runs=recommended_thresholds["min_execution_pass_runs"],
        min_total_fills=recommended_thresholds["min_total_fills"],
    )

    if incomplete_economics_rows:
        detail = "; ".join(
            ",".join(str(reason) for reason in row.get("economics_incomplete_reasons", []))
            or "unknown"
            for row in incomplete_economics_rows[:3]
        )
        coverage_fail_reasons.append(
            "replay economics attribution incomplete: "
            f"runs={len(incomplete_economics_rows)}, details={detail}"
        )

    if execution_active_runs < minimum_thresholds["min_execution_active_runs"]:
        coverage_fail_reasons.append(
            "execution_active_runs="
            f"{execution_active_runs} < {minimum_thresholds['min_execution_active_runs']}"
        )
    if execution_pass_runs < max(1, min_execution_pass_runs):
        coverage_fail_reasons.append(
            "execution_pass_runs="
            f"{execution_pass_runs} < {minimum_thresholds['min_execution_pass_runs']}"
        )
    if total_fills < minimum_thresholds["min_total_fills"]:
        coverage_fail_reasons.append(
            f"total_fills={total_fills} < {minimum_thresholds['min_total_fills']}"
        )

    mean_realized_net_per_fill = aggregate_summary.get("mean_realized_net_per_fill")
    if isinstance(mean_realized_net_per_fill, (int, float)):
        if mean_realized_net_per_fill < min_mean_realized_net_per_fill:
            quality_fail_reasons.append(
                "mean_realized_net_per_fill="
                f"{mean_realized_net_per_fill:.6f} < {min_mean_realized_net_per_fill:.6f}"
            )
        elif mean_realized_net_per_fill < 0.0:
            warn_reasons.append(
                "replay 聚合 realized_net_per_fill 仍为负，覆盖通过但执行经济性未转正: "
                f"mean_realized_net_per_fill={mean_realized_net_per_fill:.6f}"
            )
    else:
        warn_reasons.append("无有效 realized_net_per_fill 样本，需结合 per-segment 结果复核")
    if total_fills > 0 and zero_realized_net_with_fills_runs > 0:
        if nonzero_realized_net_with_fills_runs <= 0:
            warn_reasons.append(
                "replay 已有成交但 realized_net_per_fill 全为 0，"
                "当前只能证明执行/保护链路可触发，尚不能证明扣费后经济性；"
                "建议接入手续费/滑点/平仓净值口径"
            )
        elif zero_realized_net_with_fills_runs > nonzero_realized_net_with_fills_runs:
            warn_reasons.append(
                "replay 多数成交片段 realized_net_per_fill 为 0，"
                "建议复核手续费/滑点/平仓净值口径: "
                f"zero_runs={zero_realized_net_with_fills_runs}, "
                f"nonzero_runs={nonzero_realized_net_with_fills_runs}"
            )
    if (
        recommended_coverage_targets_met
        and total_fills >= recommended_thresholds["min_total_fills"]
    ):
        median_net_with_fills = aggregate_summary.get(
            "median_realized_net_per_fill_with_fills"
        )
        if isinstance(median_net_with_fills, (int, float)):
            if median_net_with_fills < min_mean_realized_net_per_fill:
                quality_fail_reasons.append(
                    "median_realized_net_per_fill_with_fills="
                    f"{median_net_with_fills:.6f} < {min_mean_realized_net_per_fill:.6f}"
                )
        else:
            quality_fail_reasons.append(
                "median_realized_net_per_fill_with_fills 缺失，ROBUST 覆盖下无法证明净收益稳定性"
            )
        if (
            nonzero_realized_net_with_fills_runs > 0
            and isinstance(positive_filled_segment_ratio, (int, float))
            and positive_filled_segment_ratio
            < minimum_thresholds["min_positive_filled_segment_ratio"]
        ):
            quality_fail_reasons.append(
                "positive_filled_segment_ratio="
                f"{positive_filled_segment_ratio:.6f} < "
                f"{minimum_thresholds['min_positive_filled_segment_ratio']:.6f}; "
                f"positive_runs={positive_realized_net_with_fills_runs}, "
                f"negative_runs={negative_realized_net_with_fills_runs}, "
                f"zero_runs={zero_realized_net_with_fills_runs}"
            )
        if (
            negative_realized_net_with_fills_runs > 0
            and positive_realized_net_with_fills_runs <= 0
        ):
            quality_fail_reasons.append(
                "replay ROBUST 覆盖下所有有成交片段 realized_net_per_fill 均未转正: "
                f"negative_runs={negative_realized_net_with_fills_runs}, "
                f"zero_runs={zero_realized_net_with_fills_runs}, "
                f"positive_runs={positive_realized_net_with_fills_runs}"
            )

    mean_filtered_cost_ratio = aggregate_summary.get("mean_filtered_cost_ratio_avg")
    if isinstance(mean_filtered_cost_ratio, (int, float)) and (
        mean_filtered_cost_ratio >= warn_mean_filtered_cost_ratio
    ):
        warn_reasons.append(
            "平均 ORDER_FILTERED_COST 偏高: "
            f"mean_filtered_cost_ratio_avg={mean_filtered_cost_ratio:.4f}"
        )
    if pass_with_actions_runs > 0:
        warn_reasons.append(
            f"存在 {pass_with_actions_runs} 个 PASS_WITH_ACTIONS 片段，需复核 entry gate / cost filter"
        )
    if failed_runs > 0:
        warn_reasons.append(f"存在 {failed_runs} 个 FAIL 片段，需检查单段日志与 assess 口径")

    fail_reasons.extend(coverage_fail_reasons)
    fail_reasons.extend(quality_fail_reasons)

    minimum_coverage_targets_met = not coverage_fail_reasons
    if minimum_coverage_targets_met and not recommended_coverage_targets_met:
        warn_reasons.append(
            "replay 覆盖仅达到最小门槛，建议继续补足更稳健的 execution 样本: "
            "recommended_active_runs>="
            f"{recommended_thresholds['min_execution_active_runs']}, "
            "recommended_pass_runs>="
            f"{recommended_thresholds['min_execution_pass_runs']}, "
            f"recommended_total_fills>={recommended_thresholds['min_total_fills']}"
        )

    status = "pass"
    if fail_reasons:
        status = "fail"
    elif warn_reasons:
        status = "pass_with_actions"

    aggregate_validation = {
        "status": status,
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "coverage_fail_reasons": coverage_fail_reasons,
        "quality_fail_reasons": quality_fail_reasons,
        "thresholds": minimum_thresholds,
        "recommended_thresholds": recommended_thresholds,
        "minimum_coverage_targets_met": minimum_coverage_targets_met,
        "recommended_coverage_targets_met": recommended_coverage_targets_met,
        "coverage_strength_status": (
            "INSUFFICIENT"
            if not minimum_coverage_targets_met
            else "ROBUST"
            if recommended_coverage_targets_met
            else "MINIMUM_ONLY"
        ),
    }
    return aggregate_summary, aggregate_validation


def has_met_replay_coverage_targets(
    aggregate_summary: dict[str, Any],
    *,
    min_execution_active_runs: int,
    min_execution_pass_runs: int,
    min_total_fills: int,
) -> bool:
    return (
        int(aggregate_summary.get("execution_active_runs") or 0) >=
        max(1, min_execution_active_runs)
        and int(aggregate_summary.get("execution_pass_runs") or 0) >=
        max(1, min_execution_pass_runs)
        and int(aggregate_summary.get("total_fills") or 0) >=
        max(1, min_total_fills)
    )


def normalize_symbols(raw_symbols: str, fallback_symbol: str) -> list[str]:
    source = raw_symbols if raw_symbols.strip() else fallback_symbol
    symbols: list[str] = []
    for raw_item in source.replace(";", ",").split(","):
        symbol = raw_item.strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        symbols.append((fallback_symbol or "BTCUSDT").strip().upper())
    return symbols


def resolve_path(raw_path: str, root: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(raw_path)
    if path.is_absolute():
        return path
    return (root / path).resolve()


def build_economic_objective_contract(
    args: argparse.Namespace,
    *,
    root: pathlib.Path,
    execution_policy_sha256: str,
    trade_bot_sha256: str,
) -> dict[str, Any]:
    implementation_paths = {
        "replay_runner": pathlib.Path(__file__).resolve(),
        "runtime_assessor": (root / "tools" / "assess_run_log.py").resolve(),
        "policy_contract": (
            root / "tools" / "config_policy_contract.py"
        ).resolve(),
    }
    governance_contract = (
        root / "config" / "closed_loop_contract.json"
    ).resolve()
    payload: dict[str, Any] = {
        "schema_version": "economic_objective_contract_v1",
        "primary_metric": "mean_realized_net_per_fill",
        "authoritative_execution": "cpp_trade_bot_replay",
        "fill_model": "next_bar_ohlc_first_touch_v1",
        "terminal_position_policy": "force_close_and_charge_exit_cost",
        "funding_policy": "per_bar_rate_from_replay_dataset",
        "accounting_source": "replay_terminal_account_state",
        "gross_pnl_formula": "realized_net_plus_fee_plus_funding_paid",
        "fee_sensitivity_formula": "gross_minus_funding_paid_minus_scaled_fee",
        "funding_sensitivity_policy": "fixed_while_scaling_fee",
        "fill_count_source": "all_fill_applied_events_current_boot",
        "terminal_settlement_evidence_required": True,
        "incomplete_economics_policy": "hard_fail",
        "state_isolation_policy": "fresh_wal_per_symbol_segment",
        "cost_policy_source": "execution_policy_v2",
        "execution_policy_sha256": execution_policy_sha256,
        "trade_bot_sha256": trade_bot_sha256,
        "selection_and_final_share_contract": True,
        "thresholds": {
            "assess_stage": str(args.assess_stage).strip().upper(),
            "min_runtime_status": int(args.min_runtime_status),
            "min_execution_active_runs": int(
                args.min_execution_active_runs
            ),
            "min_execution_pass_runs": int(
                args.min_execution_pass_runs
            ),
            "min_total_fills": int(args.min_total_fills),
            "min_mean_realized_net_per_fill": float(
                args.min_mean_realized_net_per_fill
            ),
            "min_break_even_fee_multiplier": float(
                args.min_break_even_fee_multiplier
            ),
            "warn_mean_filtered_cost_ratio": float(
                args.warn_mean_filtered_cost_ratio
            ),
            "min_tradable_symbols": int(args.min_tradable_symbols),
            "min_positive_filled_segment_ratio": (
                MIN_POSITIVE_FILLED_SEGMENT_RATIO
            ),
        },
        "segment_sampling": {
            "target_bucket": str(args.target_bucket).strip().lower(),
            "selection_policy": SELECTION_SAMPLING_POLICY,
            "trend_threshold_quantile": TREND_THRESHOLD_QUANTILE,
            "max_segments": int(args.max_segments),
            "min_segment_bars": int(args.min_segment_bars),
            "warmup_context_bars": REPLAY_WARMUP_CONTEXT_BARS,
            "warmup_context_execution_disabled": True,
            "final_outcome_ranking_forbidden": True,
        },
        "implementation_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in implementation_paths.items()
        },
        "governance_contract": {
            "path": str(governance_contract),
            "sha256": (
                hashlib.sha256(governance_contract.read_bytes()).hexdigest()
                if governance_contract.is_file()
                else ""
            ),
        },
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


def build_baseline_candidate_identity(
    args: argparse.Namespace,
    *,
    root: pathlib.Path,
    base_config: pathlib.Path,
    trade_bot: pathlib.Path,
) -> dict[str, Any]:
    execution_policy = policy_payload(base_config)
    trade_bot_sha256 = hashlib.sha256(trade_bot.read_bytes()).hexdigest()
    economic_objective_contract = build_economic_objective_contract(
        args,
        root=root,
        execution_policy_sha256=str(execution_policy["sha256"]),
        trade_bot_sha256=trade_bot_sha256,
    )
    identity: dict[str, Any] = {
        "candidate_type": "baseline_runtime_v1",
        "candidate_version": (
            "baseline_" + str(execution_policy["sha256"])[:16]
        ),
        "model_version": "baseline_no_integrator_model",
        "base_config_path": str(base_config),
        "base_config_sha256": hashlib.sha256(base_config.read_bytes()).hexdigest(),
        "execution_policy": execution_policy,
        "economic_objective_contract": economic_objective_contract,
        "trade_bot_sha256": trade_bot_sha256,
        "config_binds_candidate": True,
        "integrator_model_required": False,
    }
    identity["identity_sha256"] = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return identity


def inspect_feature_time_range(path: pathlib.Path) -> tuple[int, int, int]:
    timestamps: list[int] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if "timestamp" not in (reader.fieldnames or []):
            raise ValueError(f"{path} missing timestamp column")
        for line_number, row in enumerate(reader, start=2):
            try:
                timestamps.append(int(row["timestamp"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"{path}:{line_number} invalid timestamp"
                ) from exc
    if not timestamps:
        raise ValueError(f"{path} has no timestamp rows")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError(f"{path} timestamps must be strictly increasing")
    deltas = [
        right - left for left, right in zip(timestamps, timestamps[1:])
    ]
    if not deltas:
        raise ValueError(f"{path} cannot infer bar interval")
    return timestamps[0], timestamps[-1], int(statistics.median(deltas))


def claim_final_holdouts(
    ledger_path: pathlib.Path,
    *,
    experiment_id: str,
    candidate_identity_sha256: str,
    holdouts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not experiment_id.strip():
        raise ValueError("final holdout experiment_id is required")
    if len(candidate_identity_sha256) != 64:
        raise ValueError("final holdout candidate identity checksum is invalid")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = ledger_path.with_suffix(
        ledger_path.suffix + ".checkpoint.json"
    )
    claimed: list[dict[str, Any]] = []
    with ledger_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        existing: list[dict[str, Any]] = []
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{ledger_path}:{line_number} invalid JSON"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"{ledger_path}:{line_number} entry is not object"
                )
            reported_entry_sha256 = str(
                value.get("entry_sha256") or ""
            ).strip()
            hash_payload = dict(value)
            hash_payload.pop("entry_sha256", None)
            computed_entry_sha256 = hashlib.sha256(
                json.dumps(
                    hash_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            expected_previous = (
                str(existing[-1].get("entry_sha256") or "")
                if existing
                else "0" * 64
            )
            if (
                value.get("schema_version")
                != "final_holdout_consumption_v2"
                or value.get("previous_entry_sha256") != expected_previous
                or reported_entry_sha256 != computed_entry_sha256
            ):
                raise ValueError(
                    f"{ledger_path}:{line_number} holdout ledger hash chain invalid"
                )
            existing.append(value)
        if existing:
            if not checkpoint_path.is_file():
                raise RuntimeError(
                    "holdout ledger checkpoint missing; refuse unverified history"
                )
            checkpoint = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
            if (
                checkpoint.get("schema_version")
                != "final_holdout_checkpoint_v1"
                or int(checkpoint.get("entry_count") or 0) != len(existing)
                or checkpoint.get("tail_entry_sha256")
                != existing[-1].get("entry_sha256")
            ):
                raise RuntimeError(
                    "holdout ledger checkpoint mismatch; deletion/truncation detected"
                )
        elif checkpoint_path.exists():
            raise RuntimeError(
                "holdout ledger missing but checkpoint exists; deletion detected"
            )

        pending_entries: list[dict[str, Any]] = []
        validated_history = list(existing)
        for raw_claim in holdouts:
            claim = {
                "schema_version": "final_holdout_consumption_v2",
                "experiment_id": experiment_id,
                "candidate_identity_sha256": candidate_identity_sha256,
                "symbol": str(raw_claim["symbol"]).strip().upper(),
                "bar_interval_ms": int(raw_claim["bar_interval_ms"]),
                "holdout_start_ts_ms": int(raw_claim["holdout_start_ts_ms"]),
                "holdout_end_ts_ms": int(raw_claim["holdout_end_ts_ms"]),
                "dataset_path": str(raw_claim["dataset_path"]),
                "dataset_sha256": str(raw_claim["dataset_sha256"]),
                "opened_at_utc": now_utc_iso(),
                "status": "opened_before_evaluation",
                "previous_entry_sha256": (
                    str(validated_history[-1].get("entry_sha256") or "")
                    if validated_history
                    else "0" * 64
                ),
            }
            same_experiment = [
                item
                for item in validated_history
                if str(item.get("experiment_id") or "") == experiment_id
                and str(item.get("symbol") or "").strip().upper()
                == claim["symbol"]
            ]
            if same_experiment:
                raise RuntimeError(
                    "final holdout experiment already consumed; replay requires "
                    "a new experiment id and fresh evidence: "
                    f"experiment_id={experiment_id}, symbol={claim['symbol']}"
                )
            for item in validated_history:
                if (
                    str(item.get("symbol") or "").strip().upper()
                    != claim["symbol"]
                    or int(item.get("bar_interval_ms") or 0)
                    != claim["bar_interval_ms"]
                ):
                    continue
                old_start = int(item.get("holdout_start_ts_ms") or 0)
                old_end = int(item.get("holdout_end_ts_ms") or 0)
                if not (
                    claim["holdout_end_ts_ms"] < old_start
                    or claim["holdout_start_ts_ms"] > old_end
                ):
                    raise RuntimeError(
                        "final holdout overlaps consumed evidence: "
                        f"symbol={claim['symbol']}, "
                        f"new={claim['holdout_start_ts_ms']}:"
                        f"{claim['holdout_end_ts_ms']}, "
                        f"existing={old_start}:{old_end}"
                    )
            claim["entry_sha256"] = hashlib.sha256(
                json.dumps(
                    claim,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            validated_history.append(claim)
            pending_entries.append(claim)

        if pending_entries:
            handle.seek(0, os.SEEK_END)
            handle.write(
                "".join(
                    json.dumps(
                        entry,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for entry in pending_entries
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
            existing.extend(pending_entries)
            claimed.extend(pending_entries)
        checkpoint = {
            "schema_version": "final_holdout_checkpoint_v1",
            "entry_count": len(existing),
            "tail_entry_sha256": (
                existing[-1]["entry_sha256"] if existing else ""
            ),
            "updated_at_utc": now_utc_iso(),
        }
        checkpoint_tmp = checkpoint_path.with_suffix(
            checkpoint_path.suffix + ".tmp"
        )
        with checkpoint_tmp.open("w", encoding="utf-8") as checkpoint_handle:
            checkpoint_handle.write(
                json.dumps(
                    checkpoint,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            checkpoint_handle.flush()
            os.fsync(checkpoint_handle.fileno())
        os.replace(checkpoint_tmp, checkpoint_path)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return claimed


def parse_feature_csv_by_symbol(
    raw_mapping: str,
    root: pathlib.Path,
) -> dict[str, pathlib.Path]:
    mapping: dict[str, pathlib.Path] = {}
    for raw_item in raw_mapping.replace(";", ",").split(","):
        item = raw_item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                "feature_csv_by_symbol 项必须使用 SYMBOL=PATH 格式: "
                f"{item}"
            )
        symbol_raw, path_raw = item.split("=", 1)
        symbol = symbol_raw.strip().upper()
        path_text = path_raw.strip()
        if not symbol or not path_text:
            raise ValueError(
                "feature_csv_by_symbol 项必须包含非空 SYMBOL 和 PATH: "
                f"{item}"
            )
        mapping[symbol] = resolve_path(path_text, root)
    return mapping


def corpus_manifest_for_symbol(
    corpus_manifest: pathlib.Path | None,
    symbol: str,
    *,
    per_symbol: bool,
) -> pathlib.Path | None:
    if corpus_manifest is None:
        return None
    if not per_symbol:
        return corpus_manifest
    suffix = corpus_manifest.suffix or ".json"
    return corpus_manifest.with_name(f"{corpus_manifest.stem}_{symbol}{suffix}")


def build_frozen_corpus_binding(
    corpus_manifest: pathlib.Path | None,
    *,
    symbols: list[str],
    per_symbol: bool,
) -> dict[str, Any]:
    if corpus_manifest is None:
        raise ValueError("frozen corpus manifest is required")
    per_symbol_binding: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        path = corpus_manifest_for_symbol(
            corpus_manifest,
            symbol,
            per_symbol=per_symbol,
        )
        if path is None or not path.is_file():
            raise FileNotFoundError(
                f"{symbol} frozen corpus manifest missing: {path}"
            )
        payload = load_corpus_manifest(path)
        per_symbol_binding[symbol] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "schema_version": payload.get("schema_version"),
            "evidence_domain": payload.get("evidence_domain"),
            "candidate_set_frozen": payload.get("candidate_set_frozen"),
            "source_feature_csv": payload.get("source_feature_csv"),
            "source_feature_sha256": payload.get("source_feature_sha256"),
            "target_bucket": payload.get("target_bucket"),
            "thresholds": payload.get("thresholds"),
            "sampling_quantiles": payload.get("sampling_quantiles"),
        }
    binding: dict[str, Any] = {
        "schema_version": "frozen_replay_corpus_binding_v1",
        "per_symbol": per_symbol_binding,
    }
    binding["binding_sha256"] = hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return binding


def validate_prevalidated_selection_report(
    report_path: pathlib.Path,
    *,
    candidate_identity: dict[str, Any],
    symbols: list[str],
    selection_feature_csv: pathlib.Path | None,
    selection_feature_csv_by_symbol: dict[str, pathlib.Path],
    final_feature_csv: pathlib.Path,
    final_feature_csv_by_symbol: dict[str, pathlib.Path],
    frozen_corpus_binding: dict[str, Any],
    target_bucket: str,
) -> dict[str, Any]:
    if not report_path.is_file():
        raise FileNotFoundError(
            f"prevalidated selection report missing: {report_path}"
        )
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("prevalidated selection report must be a JSON object")

    reasons: list[str] = []
    expected_identity_sha256 = str(
        candidate_identity.get("identity_sha256") or ""
    )
    reported_identity = payload.get("candidate_identity")
    if not isinstance(reported_identity, dict):
        reasons.append("candidate identity missing")
    else:
        reported_identity_sha256 = str(
            reported_identity.get("identity_sha256") or ""
        )
        identity_payload = dict(reported_identity)
        identity_payload.pop("identity_sha256", None)
        computed_identity_sha256 = hashlib.sha256(
            json.dumps(
                identity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if reported_identity_sha256 != computed_identity_sha256:
            reasons.append("reported candidate identity checksum is invalid")
        if reported_identity_sha256 != expected_identity_sha256:
            reasons.append("candidate identity does not match frozen candidate")
        if reported_identity != candidate_identity:
            reasons.append("candidate identity payload does not match")

    activation_gate = payload.get("activation_gate")
    activation_status = (
        str(activation_gate.get("status") or "").strip().lower()
        if isinstance(activation_gate, dict)
        else ""
    )
    if activation_status not in {"pass", "pass_with_actions"}:
        reasons.append("selection activation gate did not pass")
    if str(payload.get("status") or "").strip().lower() not in {
        "pass",
        "pass_with_actions",
    }:
        reasons.append("selection report status did not pass")
    if str(payload.get("target_bucket") or "").strip().lower() != str(
        target_bucket
    ).strip().lower():
        reasons.append("target bucket mismatch")
    if payload.get("real_market_replay") is not True:
        reasons.append("selection evidence is not real-market replay")

    execution_contract = payload.get("execution_evidence_contract")
    if not isinstance(execution_contract, dict) or (
        execution_contract.get("schema_version")
        != "replay_execution_prescreen_v1"
    ):
        reasons.append("execution evidence contract missing or invalid")

    reported_binding = payload.get("frozen_corpus_binding")
    if reported_binding != frozen_corpus_binding:
        reasons.append("frozen corpus binding mismatch")

    report_symbols = payload.get("symbols")
    if report_symbols != symbols:
        reasons.append("selection report symbol set/order mismatch")
    symbol_reports = payload.get("symbol_reports")
    if not isinstance(symbol_reports, dict):
        reasons.append("selection symbol reports missing")
        symbol_reports = {}
    expected_selection_hashes: dict[str, str] = {}
    for symbol in symbols:
        expected_selection_path = selection_feature_csv_by_symbol.get(
            symbol,
            selection_feature_csv,
        )
        if expected_selection_path is None or not expected_selection_path.is_file():
            reasons.append(f"{symbol} selection feature csv missing")
            continue
        final_path = final_feature_csv_by_symbol.get(symbol, final_feature_csv)
        if (
            expected_selection_path.resolve(strict=False)
            == final_path.resolve(strict=False)
        ):
            reasons.append(f"{symbol} selection and final datasets are identical")
        expected_hash = hashlib.sha256(
            expected_selection_path.read_bytes()
        ).hexdigest()
        expected_selection_hashes[symbol] = expected_hash
        symbol_report = symbol_reports.get(symbol)
        if not isinstance(symbol_report, dict):
            reasons.append(f"{symbol} selection symbol report missing")
            continue
        reported_path_text = str(symbol_report.get("feature_csv") or "")
        reported_path = pathlib.Path(reported_path_text).expanduser().resolve(
            strict=False
        )
        if reported_path != expected_selection_path.resolve(strict=False):
            reasons.append(f"{symbol} selection feature path mismatch")
        if str(symbol_report.get("feature_sha256") or "") != expected_hash:
            reasons.append(f"{symbol} selection feature checksum mismatch")
        symbol_gate = symbol_report.get("aggregate_validation")
        if not isinstance(symbol_gate, dict) or str(
            symbol_gate.get("status") or ""
        ).strip().lower() not in {"pass", "pass_with_actions"}:
            reasons.append(f"{symbol} aggregate selection validation did not pass")

    holdout_consumption = payload.get("holdout_consumption")
    if isinstance(holdout_consumption, dict) and (
        bool(holdout_consumption.get("claimed_before_evaluation"))
        or bool(str(holdout_consumption.get("ledger_path") or "").strip())
    ):
        reasons.append("selection evidence must not consume a final-holdout ledger")

    if reasons:
        raise RuntimeError(
            "prevalidated selection evidence rejected; final holdout remains "
            "unopened: " + "; ".join(reasons)
        )
    return {
        "schema_version": "prevalidated_selection_binding_v1",
        "path": str(report_path),
        "sha256": report_sha256,
        "status": activation_status,
        "candidate_identity_sha256": expected_identity_sha256,
        "frozen_corpus_binding_sha256": frozen_corpus_binding.get(
            "binding_sha256"
        ),
        "selection_feature_sha256_by_symbol": expected_selection_hashes,
        "report": payload,
    }


def thresholds_to_payload(thresholds: RegimeThresholds) -> dict[str, float]:
    return {
        "trend_abs_ema_diff": thresholds.trend_abs_ema_diff,
        "trend_abs_mom_48": thresholds.trend_abs_mom_48,
        "extreme_vol_12": thresholds.extreme_vol_12,
        "extreme_range_pct": thresholds.extreme_range_pct,
    }


def build_symbol_tradeability(
    symbol_reports: dict[str, dict[str, Any]],
    *,
    min_mean_realized_net_per_fill: float,
    min_tradable_symbols: int,
    source_symbol: str = "",
) -> dict[str, Any]:
    decisions: dict[str, dict[str, Any]] = {}
    tradable_symbols: list[str] = []
    execution_covered_symbols: list[str] = []
    quarantined_symbols: list[str] = []
    insufficient_symbols: list[str] = []

    for symbol, symbol_report in symbol_reports.items():
        summary = symbol_report.get("aggregate_summary", {})
        if not isinstance(summary, dict):
            summary = {}
        validation = symbol_report.get("aggregate_validation", {})
        if not isinstance(validation, dict):
            validation = {}
        thresholds = validation.get("thresholds", {})
        if not isinstance(thresholds, dict):
            thresholds = {}
        min_total_fills = int_or_zero(thresholds.get("min_total_fills")) or 1
        coverage_fail_reasons = [
            str(item)
            for item in validation.get("coverage_fail_reasons", [])
            if str(item).strip()
        ]
        quality_fail_reasons = [
            str(item)
            for item in validation.get("quality_fail_reasons", [])
            if str(item).strip()
        ]
        fail_reasons = [
            str(item)
            for item in validation.get("fail_reasons", [])
            if str(item).strip()
        ]
        coverage_strength = str(
            validation.get("coverage_strength_status", "")
        ).upper()
        total_fills = int_or_zero(summary.get("total_fills"))
        positive_runs = int_or_zero(
            summary.get("positive_realized_net_with_fills_runs")
        )
        negative_runs = int_or_zero(
            summary.get("negative_realized_net_with_fills_runs")
        )
        zero_runs = int_or_zero(summary.get("zero_realized_net_with_fills_runs"))
        mean_net = number_or_none(summary.get("mean_realized_net_per_fill"))
        mean_net_with_fills = number_or_none(
            summary.get("mean_realized_net_per_fill_with_fills")
        )
        median_net_with_fills = number_or_none(
            summary.get("median_realized_net_per_fill_with_fills")
        )
        positive_ratio = number_or_none(summary.get("positive_filled_segment_ratio"))
        if positive_ratio is None and zero_runs > 0:
            positive_ratio = safe_ratio(
                positive_runs,
                positive_runs + negative_runs + zero_runs,
            )
        economic_value = (
            median_net_with_fills
            if median_net_with_fills is not None
            else mean_net_with_fills
            if mean_net_with_fills is not None
            else mean_net
        )
        coverage_ok = (
            bool(validation.get("minimum_coverage_targets_met"))
            and coverage_strength != "INSUFFICIENT"
            and not coverage_fail_reasons
            and total_fills >= min_total_fills
        )
        median_ok = (
            median_net_with_fills is not None
            and median_net_with_fills >= float(min_mean_realized_net_per_fill)
        )
        positive_ratio_ok = (
            positive_ratio is not None
            and positive_ratio >= MIN_POSITIVE_FILLED_SEGMENT_RATIO
        )
        economic_ok = (
            economic_value is not None
            and economic_value >= float(min_mean_realized_net_per_fill)
            and median_ok
            and positive_ratio_ok
            and not quality_fail_reasons
        )
        all_filled_runs_negative = positive_runs <= 0 and negative_runs > 0
        reasons: list[str] = []
        if not coverage_ok:
            reasons.extend(coverage_fail_reasons or fail_reasons)
            if not reasons:
                reasons.append("symbol_replay_coverage_insufficient")
            decision_status = "insufficient"
            insufficient_symbols.append(symbol)
        else:
            execution_covered_symbols.append(symbol)

        if coverage_ok and (not economic_ok or all_filled_runs_negative):
            reasons.extend(quality_fail_reasons or fail_reasons)
            if all_filled_runs_negative and not any(
                "均未转正" in reason or "all" in reason.lower()
                for reason in reasons
            ):
                reasons.append(
                    "symbol_replay_all_filled_segments_net_negative: "
                    f"positive_runs={positive_runs}, negative_runs={negative_runs}"
                )
            if not reasons:
                if not median_ok:
                    reasons.append(
                        "symbol_replay_median_realized_net_per_fill_with_fills_below_threshold"
                    )
                elif not positive_ratio_ok:
                    reasons.append(
                        "symbol_replay_positive_filled_segment_ratio_below_threshold"
                    )
                else:
                    reasons.append(
                        "symbol_replay_median_realized_net_per_fill_with_fills_missing_or_below_threshold"
                    )
            decision_status = "quarantined"
            quarantined_symbols.append(symbol)
        elif coverage_ok:
            decision_status = "tradable"
            tradable_symbols.append(symbol)

        decisions[symbol] = {
            "status": decision_status,
            "coverage_ok": coverage_ok,
            "economic_ok": economic_ok,
            "coverage_strength_status": coverage_strength,
            "total_fills": total_fills,
            "positive_realized_net_with_fills_runs": positive_runs,
            "negative_realized_net_with_fills_runs": negative_runs,
            "mean_realized_net_per_fill": mean_net,
            "mean_realized_net_per_fill_with_fills": mean_net_with_fills,
            "median_realized_net_per_fill_with_fills": median_net_with_fills,
            "positive_filled_segment_ratio": positive_ratio,
            "thresholds": {
                "min_total_fills": min_total_fills,
                "min_mean_realized_net_per_fill": float(
                    min_mean_realized_net_per_fill
                ),
                "min_positive_filled_segment_ratio": MIN_POSITIVE_FILLED_SEGMENT_RATIO,
            },
            "reasons": reasons,
        }

    min_tradable = max(1, int(min_tradable_symbols))
    fail_reasons: list[str] = []
    warn_reasons: list[str] = []
    source_symbol_normalized = str(source_symbol or "").strip().upper()
    if len(tradable_symbols) < min_tradable:
        fail_reasons.append(
            "tradable_symbol_count="
            f"{len(tradable_symbols)} < min_tradable_symbols={min_tradable}"
        )
    if source_symbol_normalized and source_symbol_normalized not in {
        item.upper() for item in execution_covered_symbols
    }:
        fail_reasons.append(
            f"source_symbol_not_execution_covered={source_symbol_normalized}"
        )
    if insufficient_symbols:
        warn_reasons.append(
            "symbol_replay_coverage_insufficient="
            + ",".join(insufficient_symbols)
        )
    if quarantined_symbols:
        warn_reasons.append(
            "symbol_replay_quarantined=" + ",".join(quarantined_symbols)
        )

    return {
        "status": "pass" if not fail_reasons else "fail",
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "tradable_symbols": tradable_symbols,
        "execution_covered_symbols": execution_covered_symbols,
        "quarantined_symbols": quarantined_symbols,
        "insufficient_symbols": insufficient_symbols,
        "tradable_symbol_count": len(tradable_symbols),
        "execution_covered_symbol_count": len(execution_covered_symbols),
        "quarantined_symbol_count": len(quarantined_symbols),
        "insufficient_symbol_count": len(insufficient_symbols),
        "min_tradable_symbols": min_tradable,
        "decisions": decisions,
    }


def merge_symbol_validations(
    aggregate_validation: dict[str, Any],
    symbol_reports: dict[str, dict[str, Any]],
    *,
    min_mean_realized_net_per_fill: float = 0.0,
    min_tradable_symbols: int = 1,
    source_symbol: str = "",
    final_holdout: bool = False,
) -> dict[str, Any]:
    merged = dict(aggregate_validation)
    raw_aggregate_fail_reasons = [
        str(item)
        for item in merged.get("fail_reasons", [])
        if str(item).strip()
    ]
    aggregate_status = str(merged.get("status", "")).strip().lower()
    fail_reasons: list[str] = (
        list(raw_aggregate_fail_reasons) if final_holdout else []
    )
    if final_holdout and aggregate_status == "fail" and not fail_reasons:
        fail_reasons.append("aggregate_validation.status=fail")
    warn_reasons = list(merged.get("warn_reasons", []))
    symbol_quarantine_reasons: list[str] = []
    symbol_fail_reasons: list[str] = []
    tradeability = build_symbol_tradeability(
        symbol_reports,
        min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
        min_tradable_symbols=min_tradable_symbols,
        source_symbol=source_symbol,
    )
    decisions = tradeability.get("decisions", {})
    if not isinstance(decisions, dict):
        decisions = {}
    for reason in tradeability.get("warn_reasons", []):
        reason_text = str(reason).strip()
        if reason_text:
            warn_reasons.append(
                f"symbol_tradeability_observation: {reason_text}"
                if final_holdout
                else reason_text
            )
    if final_holdout:
        for reason in tradeability.get("fail_reasons", []):
            reason_text = str(reason).strip()
            if reason_text:
                warn_reasons.append(
                    f"symbol_tradeability_observation: {reason_text}"
                )

    for symbol, symbol_report in symbol_reports.items():
        validation = symbol_report.get("aggregate_validation", {})
        if not isinstance(validation, dict):
            continue
        decision = decisions.get(symbol, {})
        if not isinstance(decision, dict):
            decision = {}
        status = str(validation.get("status", "")).lower()
        if status == "fail":
            for reason in validation.get("fail_reasons", []):
                item = f"{symbol}: {reason}"
                if decision.get("status") == "quarantined":
                    symbol_quarantine_reasons.append(item)
                symbol_fail_reasons.append(item)
        elif status == "pass_with_actions":
            for reason in validation.get("warn_reasons", []):
                warn_reasons.append(f"{symbol}: {reason}")

    suppressed_aggregate_fail_reasons: list[str] = []
    if final_holdout:
        fail_reasons.extend(symbol_fail_reasons)
    else:
        tradeability_status = str(tradeability.get("status", "")).lower()
        non_quarantined_symbol_fail_reasons = [
            reason
            for reason in symbol_fail_reasons
            if reason not in symbol_quarantine_reasons
        ]
        if tradeability_status == "pass":
            if aggregate_status not in {"pass", "pass_with_actions"}:
                suppressed_aggregate_fail_reasons.append(
                    f"aggregate_validation status={aggregate_status or 'unknown'}"
                )
            suppressed_aggregate_fail_reasons.extend(raw_aggregate_fail_reasons)
            suppressed_aggregate_fail_reasons.extend(
                non_quarantined_symbol_fail_reasons
            )
            if suppressed_aggregate_fail_reasons:
                warn_reasons.append(
                    "aggregate_validation_failed_but_symbol_tradeability_passed: "
                    + "; ".join(suppressed_aggregate_fail_reasons)
                )
        else:
            fail_reasons.extend(raw_aggregate_fail_reasons)
            fail_reasons.extend(non_quarantined_symbol_fail_reasons)
            fail_reasons.extend(
                str(reason).strip()
                for reason in tradeability.get("fail_reasons", [])
                if str(reason).strip()
            )
            fail_reasons.extend(symbol_quarantine_reasons)

    if fail_reasons:
        merged["status"] = "fail"
        if any(
            str(
                symbol_report.get("aggregate_validation", {}).get(
                    "coverage_strength_status", ""
                )
            )
            == "INSUFFICIENT"
            for symbol_report in symbol_reports.values()
            if isinstance(symbol_report.get("aggregate_validation", {}), dict)
        ):
            merged["coverage_strength_status"] = "INSUFFICIENT"
    elif final_holdout and aggregate_status in {"pass", "pass_with_actions"}:
        merged["status"] = "pass_with_actions" if warn_reasons else "pass"
    elif not final_holdout and str(tradeability.get("status", "")).lower() == "pass":
        merged["status"] = "pass_with_actions" if warn_reasons else "pass"
    elif warn_reasons and str(merged.get("status", "")).lower() == "pass":
        merged["status"] = "pass_with_actions"
    elif str(merged.get("status", "")).lower() == "pass_with_actions" and not warn_reasons:
        merged["status"] = "pass"
    merged["fail_reasons"] = list(dict.fromkeys(fail_reasons))
    merged["warn_reasons"] = list(dict.fromkeys(warn_reasons))
    merged["symbol_tradeability"] = tradeability
    merged["tradable_symbols"] = tradeability.get("tradable_symbols", [])
    merged["quarantined_symbols"] = tradeability.get("quarantined_symbols", [])
    merged["insufficient_symbols"] = tradeability.get("insufficient_symbols", [])
    merged["symbol_quarantine_reasons"] = symbol_quarantine_reasons
    merged["suppressed_aggregate_fail_reasons"] = suppressed_aggregate_fail_reasons
    return merged


def run_replay_for_symbol(
    *,
    symbol: str,
    output_dir: pathlib.Path,
    rows: list[FeatureRow],
    thresholds: RegimeThresholds,
    selected_segments: list[ReplaySegment],
    target_bucket: str,
    base_interval_ms: int,
    root: pathlib.Path,
    base_config: pathlib.Path,
    trade_bot: pathlib.Path,
    assess_stage: str,
    min_runtime_status: int,
    min_execution_active_runs: int,
    min_execution_pass_runs: int,
    min_total_fills: int,
    min_mean_realized_net_per_fill: float,
    min_break_even_fee_multiplier: float,
    warn_mean_filtered_cost_ratio: float,
    force_all_frozen_segments: bool = False,
    execution_policy_identity: dict[str, Any] | None = None,
    trade_bot_sha256: str = "",
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    run_summaries: list[dict[str, Any]] = []
    stopped_early = False
    stop_reason = ""
    recommended_thresholds = derive_recommended_coverage_thresholds(
        min_execution_active_runs=min_execution_active_runs,
        min_execution_pass_runs=min_execution_pass_runs,
        min_total_fills=min_total_fills,
    )
    effective_execution_policy_identity = (
        execution_policy_identity
        if isinstance(execution_policy_identity, dict)
        else policy_payload(base_config)
    )
    effective_trade_bot_sha256 = trade_bot_sha256 or hashlib.sha256(
        trade_bot.read_bytes()
    ).hexdigest()

    for idx, segment in enumerate(selected_segments, start=1):
        segment_dir = output_dir / f"segment_{idx:02d}"
        segment_dir.mkdir(parents=True, exist_ok=True)
        replay_csv = segment_dir / "replay_market.csv"
        warmup_context_bars = write_replay_csv(
            rows,
            segment,
            symbol,
            replay_csv,
            base_interval_ms,
        )
        replay_csv_sha256 = hashlib.sha256(replay_csv.read_bytes()).hexdigest()
        segment_identity = replay_segment_identity(
            symbol=symbol,
            target_bucket=target_bucket,
            base_interval_ms=base_interval_ms,
            segment=segment,
            replay_csv_sha256=replay_csv_sha256,
        )
        run_payload = execute_replay_csv(
            block_id="",
            symbol=symbol,
            segment_index=idx,
            segment_payload=segment_to_payload(
                segment,
                rows=rows,
                thresholds=thresholds,
                target_bucket=target_bucket,
            ),
            replay_csv=replay_csv,
            segment_identity=segment_identity,
            segment_dir=segment_dir,
            root=root,
            base_config=base_config,
            trade_bot=trade_bot,
            assess_stage=assess_stage,
            min_runtime_status=min_runtime_status,
            execution_policy_identity=effective_execution_policy_identity,
            trade_bot_sha256=effective_trade_bot_sha256,
            warmup_context_bars=warmup_context_bars,
        )
        run_payload["economics_attribution"] = build_run_economics_attribution(
            run_payload
        )

        run_summaries.append(run_payload)
        aggregate_summary, _ = aggregate_run_summaries(
            run_summaries,
            min_execution_active_runs=min_execution_active_runs,
            min_execution_pass_runs=min_execution_pass_runs,
            min_total_fills=min_total_fills,
            min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
            warn_mean_filtered_cost_ratio=warn_mean_filtered_cost_ratio,
        )
        recommended_coverage_met = has_met_replay_coverage_targets(
            aggregate_summary,
            min_execution_active_runs=recommended_thresholds[
                "min_execution_active_runs"
            ],
            min_execution_pass_runs=recommended_thresholds[
                "min_execution_pass_runs"
            ],
            min_total_fills=recommended_thresholds["min_total_fills"],
        )
        if should_stop_after_coverage(
            recommended_coverage_met,
            force_all_frozen_segments,
        ):
            stopped_early = True
            stop_reason = "recommended_coverage_targets_met"
            break

    aggregate_summary, aggregate_validation = aggregate_run_summaries(
        run_summaries,
        min_execution_active_runs=min_execution_active_runs,
        min_execution_pass_runs=min_execution_pass_runs,
        min_total_fills=min_total_fills,
        min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
        warn_mean_filtered_cost_ratio=warn_mean_filtered_cost_ratio,
    )
    economics_report = build_replay_economics_report(
        run_summaries,
        min_execution_active_runs=min_execution_active_runs,
        min_execution_pass_runs=min_execution_pass_runs,
        min_total_fills=min_total_fills,
        min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
        min_break_even_fee_multiplier=min_break_even_fee_multiplier,
    )
    symbol_selection = {
        "segments_ran": len(run_summaries),
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
        "force_all_frozen_segments": force_all_frozen_segments,
        "coverage_targets_met": has_met_replay_coverage_targets(
            aggregate_summary,
            min_execution_active_runs=min_execution_active_runs,
            min_execution_pass_runs=min_execution_pass_runs,
            min_total_fills=min_total_fills,
        ),
        "minimum_coverage_targets_met": has_met_replay_coverage_targets(
            aggregate_summary,
            min_execution_active_runs=min_execution_active_runs,
            min_execution_pass_runs=min_execution_pass_runs,
            min_total_fills=min_total_fills,
        ),
        "recommended_coverage_targets_met": has_met_replay_coverage_targets(
            aggregate_summary,
            min_execution_active_runs=recommended_thresholds[
                "min_execution_active_runs"
            ],
            min_execution_pass_runs=recommended_thresholds[
                "min_execution_pass_runs"
            ],
            min_total_fills=recommended_thresholds["min_total_fills"],
        ),
    }
    return (
        run_summaries,
        symbol_selection,
        aggregate_summary,
        aggregate_validation,
        economics_report,
    )


def exact_mode_mutual_exclusion_errors(args: argparse.Namespace) -> list[str]:
    conflicting = {
        "feature_csv_by_symbol": args.feature_csv_by_symbol,
        "selection_feature_csv": args.selection_feature_csv,
        "selection_feature_csv_by_symbol": args.selection_feature_csv_by_symbol,
        "corpus_manifest": args.corpus_manifest,
        "require_candidate_identity": args.require_candidate_identity,
        "prevalidated_selection_report": args.prevalidated_selection_report,
        "holdout_ledger": args.holdout_ledger,
        "experiment_id": args.experiment_id,
    }
    return [
        f"exact_block_plan_mutually_exclusive:{name}"
        for name, value in conflicting.items()
        if bool(value)
    ]


def run_exact_block_plan(
    args: argparse.Namespace,
    *,
    root: pathlib.Path,
    plan_path: pathlib.Path,
    output_dir: pathlib.Path,
    base_config: pathlib.Path,
    trade_bot: pathlib.Path,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "replay_validation_report.json"
    try:
        plan_metadata, audits, prepared, validation_errors = (
            preflight_exact_block_plan(
                plan_path,
                fallback_target_bucket=args.target_bucket,
            )
        )
    except Exception as exc:
        plan_metadata = {
            "path": str(plan_path),
            "sha256": "",
            "benchmark_id": "",
            "target_bucket": args.target_bucket,
            "read_only": True,
        }
        audits = []
        prepared = []
        validation_errors = [
            f"exact_block_plan_preflight_failed:{type(exc).__name__}:{exc}"
        ]
    validation_errors = [
        *exact_mode_mutual_exclusion_errors(args),
        *validation_errors,
    ]
    execution_policy_identity: dict[str, Any] = {}
    trade_bot_sha256 = ""
    if not base_config.is_file():
        validation_errors.append("base_config_missing")
    else:
        try:
            execution_policy_identity = policy_payload(base_config)
        except (OSError, ValueError) as exc:
            validation_errors.append(
                f"execution_policy_identity_invalid:{type(exc).__name__}"
            )
    if not trade_bot.is_file():
        validation_errors.append("trade_bot_missing")
    else:
        try:
            trade_bot_sha256 = hashlib.sha256(trade_bot.read_bytes()).hexdigest()
        except OSError as exc:
            validation_errors.append(
                f"trade_bot_identity_failed:{type(exc).__name__}"
            )

    executed_block_count = 0
    if not validation_errors and len(prepared) == len(audits):
        for prepared_block in prepared:
            audit = prepared_block["audit"]
            segment = prepared_block["segment"]
            segment_identity = prepared_block["segment_identity"]
            segment_dir = output_dir / f"exact_block_{int(audit['plan_index']) + 1:03d}"
            audit["execution_attempt_count"] = 1
            executed_block_count += 1
            try:
                run_payload = execute_replay_csv(
                    block_id=str(audit["block_id"]),
                    symbol=str(audit["symbol"]),
                    segment_index=int(audit["plan_index"]) + 1,
                    segment_payload={
                        "block_id": audit["block_id"],
                        "start_timestamp": segment.start_timestamp,
                        "end_timestamp": segment.end_timestamp,
                        "bars": segment.bars,
                    },
                    replay_csv=prepared_block["replay_csv"],
                    segment_identity=segment_identity,
                    segment_dir=segment_dir,
                    root=root,
                    base_config=base_config,
                    trade_bot=trade_bot,
                    assess_stage=args.assess_stage,
                    min_runtime_status=args.min_runtime_status,
                    execution_policy_identity=execution_policy_identity,
                    trade_bot_sha256=trade_bot_sha256,
                    warmup_context_bars=0,
                )
                audit.update(
                    {
                        "command": run_payload["command"],
                        "assess_command": run_payload["assess_command"],
                        "trade_bot_exit_code": run_payload[
                            "trade_bot_exit_code"
                        ],
                        "assess_exit_code": run_payload["assess_exit_code"],
                        "episode_execution_evidence": run_payload[
                            "episode_execution_evidence"
                        ],
                        "runtime_log": run_payload["runtime_log"],
                        "runtime_assess": run_payload["runtime_assess"],
                        "state_dir": run_payload["state_dir"],
                        "execution_policy_identity": run_payload[
                            "execution_policy_identity"
                        ],
                        "trade_bot_sha256": run_payload["trade_bot_sha256"],
                    }
                )
                post_execution_sha256 = hashlib.sha256(
                    prepared_block["replay_csv"].read_bytes()
                ).hexdigest()
                audit["post_execution_event_sha256"] = post_execution_sha256
                if post_execution_sha256 != audit["expected_event_sha256"]:
                    audit["errors"].append("event_sha256_changed_during_execution")
                if int(audit["trade_bot_exit_code"]) != 0:
                    audit["errors"].append("trade_bot_exit_nonzero")
                if int(audit["assess_exit_code"]) != 0:
                    audit["errors"].append("assess_exit_nonzero")
                if not isinstance(audit["episode_execution_evidence"], dict):
                    audit["errors"].append("episode_execution_evidence_missing")
                audit["execution_status"] = (
                    "EXECUTED" if not audit["errors"] else "FAILED"
                )
            except Exception as exc:
                audit["errors"].append(
                    f"execution_failed:{type(exc).__name__}:{exc}"
                )
                audit["execution_status"] = "FAILED"
            for error in audit["errors"]:
                validation_errors.append(
                    f"block[{audit['plan_index']}].{error}"
                )

    validation_errors = list(dict.fromkeys(validation_errors))
    status = (
        "VERIFIED"
        if (
            not validation_errors
            and bool(audits)
            and executed_block_count == len(audits)
            and all(item["execution_status"] == "EXECUTED" for item in audits)
        )
        else "UNVERIFIABLE"
    )
    report = {
        "schema_version": "exact_replay_block_audit_v1",
        "generated_at_utc": now_utc_iso(),
        "mode": "exact_block_plan",
        "status": status,
        "promotion_authority": False,
        "selection_bypassed": True,
        "final_holdout_bypassed": True,
        "coverage_early_stop_disabled": True,
        "mutation_targets_accessed": [],
        "exact_block_plan": plan_metadata,
        "base_config": str(base_config),
        "execution_policy_identity": execution_policy_identity,
        "trade_bot": str(trade_bot),
        "trade_bot_sha256": trade_bot_sha256,
        "planned_block_count": len(audits),
        "executed_block_count": executed_block_count,
        "validation_errors": validation_errors,
        "blocks": audits,
        "runs": audits,
    }
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(str(report_path))
    return 0 if status == "VERIFIED" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Run TREND replay validation on archived feature data.")
    parser.add_argument(
        "--feature_csv",
        default="data/research/feature_store_5m.tmp.csv",
        help="包含 close/volume/feature 列的特征 CSV",
    )
    parser.add_argument(
        "--feature_csv_by_symbol",
        default="",
        help="逗号分隔 SYMBOL=feature_csv 映射；命中时按目标币对自己的特征数据 replay",
    )
    parser.add_argument(
        "--selection_feature_csv",
        default="",
        help="selection_validation 域特征 CSV；仅用于冻结阈值和采样策略",
    )
    parser.add_argument(
        "--selection_feature_csv_by_symbol",
        default="",
        help="逗号分隔 SYMBOL=selection_feature_csv 映射",
    )
    parser.add_argument(
        "--base_config",
        default="config/bybit.replay.assess.maker_first.yaml",
        help="replay 运行配置模板",
    )
    parser.add_argument(
        "--trade_bot",
        default="build/trade_bot",
        help="trade_bot 可执行文件路径",
    )
    parser.add_argument(
        "--output_dir",
        default="data/reports/replay_validation/latest",
        help="replay 验证输出目录",
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="写入 replay CSV 的 symbol")
    parser.add_argument(
        "--symbols",
        default="",
        help="逗号分隔的 replay symbol 列表；为空时回退 --symbol",
    )
    parser.add_argument(
        "--source_symbol",
        default="",
        help="feature_csv 对应的源行情币对；为空时回退 --symbol",
    )
    parser.add_argument(
        "--target_bucket",
        choices=("trend", "range", "extreme"),
        default="trend",
        help="要验证的 regime bucket",
    )
    parser.add_argument(
        "--max_segments",
        type=int,
        default=DEFAULT_MAX_SEGMENTS,
        help="最多验证多少个片段",
    )
    parser.add_argument(
        "--min_segment_bars",
        type=int,
        default=DEFAULT_MIN_SEGMENT_BARS,
        help="片段最少 bars",
    )
    parser.add_argument(
        "--corpus_manifest",
        default="",
        help="可选：固定 replay 片段 manifest；存在时优先使用，不存在时动态生成并写入",
    )
    parser.add_argument(
        "--assess_stage",
        choices=("DEPLOY", "S3", "S5"),
        default="S3",
        help="对 replay 运行日志应用哪种 assess 口径",
    )
    parser.add_argument(
        "--min_runtime_status",
        type=int,
        default=10,
        help="replay assess 的最小 RUNTIME_STATUS 条数",
    )
    parser.add_argument(
        "--min_execution_active_runs",
        type=int,
        default=1,
        help="replay 聚合判定要求至少多少个片段进入 EXECUTION_ACTIVE",
    )
    parser.add_argument(
        "--min_execution_pass_runs",
        type=int,
        default=1,
        help="replay 聚合判定要求至少多少个片段 execution_status=PASS",
    )
    parser.add_argument(
        "--min_total_fills",
        type=int,
        default=3,
        help="replay 聚合判定要求所有片段合计至少多少个 fills",
    )
    parser.add_argument(
        "--min_mean_realized_net_per_fill",
        type=float,
        default=0.0,
        help="replay 聚合判定的 realized_net_per_fill 均值下限",
    )
    parser.add_argument(
        "--min_break_even_fee_multiplier",
        type=float,
        default=1.25,
        help="replay optimizer 可部署候选要求的毛利/费用安全垫下限",
    )
    parser.add_argument(
        "--warn_mean_filtered_cost_ratio",
        type=float,
        default=0.80,
        help="replay 聚合 warning 的 filtered_cost_ratio_avg 均值阈值",
    )
    parser.add_argument(
        "--min_tradable_symbols",
        type=int,
        default=1,
        help="多币对 replay 至少需要多少个币对满足覆盖与净收益条件；失败但覆盖充分的币对会进入隔离名单",
    )
    parser.add_argument(
        "--candidate_model",
        default="",
        help="本轮 replay 实际加载的候选模型；提供后会独立计算身份哈希",
    )
    parser.add_argument(
        "--candidate_report",
        default="",
        help="本轮 replay 实际加载的 Integrator 报告；须与 candidate_model 同时提供",
    )
    parser.add_argument(
        "--require_candidate_identity",
        action="store_true",
        help="要求 exact candidate 先通过 selection，再允许读取 final holdout",
    )
    parser.add_argument(
        "--allow_baseline_candidate_identity",
        action="store_true",
        help="无 Integrator 模型时，以 trade_bot + runtime config 绑定 baseline candidate",
    )
    parser.add_argument(
        "--prevalidated_selection_report",
        default="",
        help=(
            "开发域冻结语料后，在独立 selection 域生成的 exact-candidate "
            "replay_validation_report.json；严格 final 模式必填"
        ),
    )
    parser.add_argument(
        "--holdout_ledger",
        default="",
        help="append-only final holdout 消费账本；严格候选模式必填",
    )
    parser.add_argument(
        "--experiment_id",
        default="",
        help="final holdout 实验 ID；同 ID 仅允许完全相同 payload 幂等重试",
    )
    parser.add_argument(
        "--force-all-frozen-segments",
        action="store_true",
        help="运行冻结 manifest 的全部 segment，禁止达到 coverage 后提前停止",
    )
    parser.add_argument(
        "--exact-block-plan",
        default="",
        help="只读执行 verified benchmark 导出的 exact replay block plan",
    )
    args = parser.parse_args()

    root = pathlib.Path(__file__).resolve().parent.parent
    output_dir = resolve_path(args.output_dir, root)
    base_config = resolve_path(args.base_config, root)
    trade_bot = resolve_path(args.trade_bot, root)
    if args.exact_block_plan:
        return run_exact_block_plan(
            args,
            root=root,
            plan_path=resolve_path(args.exact_block_plan, root),
            output_dir=output_dir,
            base_config=base_config,
            trade_bot=trade_bot,
        )

    feature_csv = resolve_path(args.feature_csv, root)
    feature_csv_by_symbol = parse_feature_csv_by_symbol(
        args.feature_csv_by_symbol,
        root,
    )
    selection_feature_csv = (
        resolve_path(args.selection_feature_csv, root)
        if args.selection_feature_csv
        else None
    )
    selection_feature_csv_by_symbol = parse_feature_csv_by_symbol(
        args.selection_feature_csv_by_symbol,
        root,
    )
    holdout_ledger = (
        resolve_path(args.holdout_ledger, root)
        if args.holdout_ledger
        else None
    )
    prevalidated_selection_report = (
        resolve_path(args.prevalidated_selection_report, root)
        if args.prevalidated_selection_report
        else None
    )
    corpus_manifest = None
    if args.corpus_manifest:
        corpus_manifest = (
            resolve_path(args.corpus_manifest, root)
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    if not feature_csv.is_file():
        raise FileNotFoundError(f"feature csv 不存在: {feature_csv}")
    if not base_config.is_file():
        raise FileNotFoundError(f"base config 不存在: {base_config}")
    if not trade_bot.is_file():
        raise FileNotFoundError(f"trade_bot 不存在: {trade_bot}")
    candidate_identity: dict[str, Any] | None = None
    if bool(args.candidate_model) != bool(args.candidate_report):
        raise ValueError("--candidate_model 与 --candidate_report 必须同时提供")
    if args.allow_baseline_candidate_identity and args.candidate_model:
        raise ValueError(
            "baseline candidate identity 与 Integrator candidate 不可同时启用"
        )
    if args.candidate_model:
        candidate_model = resolve_path(args.candidate_model, root)
        candidate_report = resolve_path(args.candidate_report, root)
        if not candidate_model.is_file():
            raise FileNotFoundError(f"candidate model 不存在: {candidate_model}")
        if not candidate_report.is_file():
            raise FileNotFoundError(f"candidate report 不存在: {candidate_report}")
        candidate_report_payload = json.loads(
            candidate_report.read_text(encoding="utf-8")
        )
        config_text = base_config.read_text(encoding="utf-8")
        config_binds_candidate = (
            str(args.candidate_model) in config_text
            and str(args.candidate_report) in config_text
        )
        if not config_binds_candidate:
            raise ValueError(
                "replay config 未绑定传入的 candidate model/report 路径"
            )
        execution_policy = policy_payload(base_config)
        trade_bot_sha256 = hashlib.sha256(trade_bot.read_bytes()).hexdigest()
        economic_objective_contract = build_economic_objective_contract(
            args,
            root=root,
            execution_policy_sha256=str(execution_policy["sha256"]),
            trade_bot_sha256=trade_bot_sha256,
        )
        candidate_identity = {
            "model_version": str(
                candidate_report_payload.get("model_version") or ""
            ),
            "model_path": str(candidate_model),
            "model_sha256": hashlib.sha256(candidate_model.read_bytes()).hexdigest(),
            "integrator_report_path": str(candidate_report),
            "integrator_report_sha256": hashlib.sha256(
                candidate_report.read_bytes()
            ).hexdigest(),
            "base_config_path": str(base_config),
            "base_config_sha256": hashlib.sha256(base_config.read_bytes()).hexdigest(),
            "execution_policy": execution_policy,
            "economic_objective_contract": economic_objective_contract,
            "runtime_config_sha256": str(
                config_value(
                    base_config,
                    "integrator.shadow.source_runtime_config_sha256",
                )
            ),
            "trade_bot_sha256": trade_bot_sha256,
            "config_binds_candidate": True,
        }
        candidate_identity["identity_sha256"] = hashlib.sha256(
            json.dumps(
                candidate_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    elif args.allow_baseline_candidate_identity:
        candidate_identity = build_baseline_candidate_identity(
            args,
            root=root,
            base_config=base_config,
            trade_bot=trade_bot,
        )
    if args.require_candidate_identity and candidate_identity is None:
        raise RuntimeError(
            "exact candidate identity is required before selection/final replay"
        )
    if args.require_candidate_identity and (
        holdout_ledger is None or not args.experiment_id.strip()
    ):
        raise RuntimeError(
            "strict candidate replay requires holdout ledger and experiment id"
        )
    if (
        args.require_candidate_identity
        and prevalidated_selection_report is None
    ):
        raise RuntimeError(
            "strict final replay requires a prevalidated selection report; "
            "final holdout remains unopened"
        )

    symbols = normalize_symbols(args.symbols, args.symbol)
    source_symbol = str(args.source_symbol or args.symbol).strip().upper()
    warnings: list[str] = []
    run_summaries: list[dict[str, Any]] = []
    symbol_reports: dict[str, dict[str, Any]] = {}
    symbol_contexts: dict[str, dict[str, Any]] = {}
    source_symbols: dict[str, str] = {}
    per_symbol_source: dict[str, dict[str, Any]] = {}
    per_symbol_segments_ran: dict[str, int] = {}
    per_symbol_eligible_segment_count: dict[str, int] = {}
    per_symbol_coverage_targets_met: dict[str, bool] = {}
    per_symbol_recommended_coverage_targets_met: dict[str, bool] = {}
    base_interval_ms_by_symbol: dict[str, int] = {}
    thresholds_by_symbol: dict[str, dict[str, float]] = {}
    available_segments_by_symbol: dict[str, list[dict[str, Any]]] = {}
    selection_candidate_runs: list[dict[str, Any]] = []
    selection_candidate_symbol_reports: dict[str, dict[str, Any]] = {}

    use_per_symbol_corpus = bool(
        feature_csv_by_symbol or selection_feature_csv_by_symbol
    )
    frozen_corpus_binding: dict[str, Any] | None = None
    prevalidated_selection_binding: dict[str, Any] | None = None
    if prevalidated_selection_report is not None:
        if candidate_identity is None:
            raise RuntimeError(
                "prevalidated selection report requires exact candidate identity"
            )
        frozen_corpus_binding = build_frozen_corpus_binding(
            corpus_manifest,
            symbols=symbols,
            per_symbol=use_per_symbol_corpus,
        )
        prevalidated_selection_binding = (
            validate_prevalidated_selection_report(
                prevalidated_selection_report,
                candidate_identity=candidate_identity,
                symbols=symbols,
                selection_feature_csv=selection_feature_csv,
                selection_feature_csv_by_symbol=(
                    selection_feature_csv_by_symbol
                ),
                final_feature_csv=feature_csv,
                final_feature_csv_by_symbol=feature_csv_by_symbol,
                frozen_corpus_binding=frozen_corpus_binding,
                target_bucket=args.target_bucket,
            )
        )

    # Freeze every symbol's selection policy before opening any final-holdout CSV.
    # A strict three-domain final run reuses a verified development-frozen
    # corpus and selection proof, so it must never enter this refit loop.
    selection_symbols = (
        [] if prevalidated_selection_binding is not None else symbols
    )
    for symbol in selection_symbols:
        symbol_selection_csv = selection_feature_csv_by_symbol.get(
            symbol,
            selection_feature_csv,
        )
        if symbol_selection_csv is None or not symbol_selection_csv.is_file():
            raise FileNotFoundError(
                f"{symbol} selection feature csv 不存在"
            )
        selection_rows = load_feature_rows(symbol_selection_csv)
        if not selection_rows:
            raise RuntimeError(
                f"{symbol} selection feature csv 无有效行: "
                f"{symbol_selection_csv}"
            )
        selection_thresholds = derive_regime_thresholds(selection_rows)
        selection_interval_ms = infer_base_interval_ms(selection_rows)
        selection_segments = [
            segment
            for segment in find_segments(
                selection_rows,
                selection_thresholds,
                args.target_bucket,
                selection_interval_ms,
            )
            if segment.bars >= max(1, args.min_segment_bars)
        ]
        if not selection_segments:
            raise RuntimeError(
                f"{symbol} selection_validation 域无满足条件的 "
                f"{args.target_bucket} 片段"
            )
        symbol_corpus_manifest = corpus_manifest_for_symbol(
            corpus_manifest,
            symbol,
            per_symbol=use_per_symbol_corpus,
        )
        if symbol_corpus_manifest is None:
            raise RuntimeError(
                f"{symbol} 缺少 selection corpus manifest 输出路径"
            )
        write_corpus_manifest(
            symbol_corpus_manifest,
            feature_csv=symbol_selection_csv,
            symbol=symbol,
            target_bucket=args.target_bucket,
            base_interval_ms=selection_interval_ms,
            thresholds=selection_thresholds,
            max_segments=max(1, args.max_segments),
            min_segment_bars=max(1, args.min_segment_bars),
            selected_segments=selection_segments,
        )
        manifest = load_corpus_manifest(symbol_corpus_manifest)
        frozen_selection_segments = select_segments_by_frozen_quantiles(
            selection_segments,
            [float(value) for value in manifest["sampling_quantiles"]],
        )
        if not frozen_selection_segments:
            raise RuntimeError(
                f"{symbol} exact candidate selection replay resolved no segment"
            )
        selection_output_dir = output_dir / "selection_validation" / symbol
        (
            symbol_selection_runs,
            symbol_selection_execution,
            symbol_selection_summary,
            symbol_selection_validation,
            symbol_selection_economics,
        ) = run_replay_for_symbol(
            symbol=symbol,
            output_dir=selection_output_dir,
            rows=selection_rows,
            thresholds=selection_thresholds,
            selected_segments=frozen_selection_segments,
            target_bucket=args.target_bucket,
            base_interval_ms=selection_interval_ms,
            root=root,
            base_config=base_config,
            trade_bot=trade_bot,
            assess_stage=args.assess_stage,
            min_runtime_status=max(1, args.min_runtime_status),
            min_execution_active_runs=args.min_execution_active_runs,
            min_execution_pass_runs=args.min_execution_pass_runs,
            min_total_fills=args.min_total_fills,
            min_mean_realized_net_per_fill=(
                args.min_mean_realized_net_per_fill
            ),
            min_break_even_fee_multiplier=(
                args.min_break_even_fee_multiplier
            ),
            warn_mean_filtered_cost_ratio=(
                args.warn_mean_filtered_cost_ratio
            ),
            force_all_frozen_segments=args.force_all_frozen_segments,
        )
        selection_candidate_runs.extend(symbol_selection_runs)
        selection_candidate_symbol_reports[symbol] = {
            "symbol": symbol,
            "evidence_domain": SELECTION_CORPUS_EVIDENCE_DOMAIN,
            "feature_csv": str(symbol_selection_csv),
            "feature_sha256": hashlib.sha256(
                symbol_selection_csv.read_bytes()
            ).hexdigest(),
            "base_interval_ms": selection_interval_ms,
            "thresholds": thresholds_to_payload(selection_thresholds),
            "selection": symbol_selection_execution,
            "aggregate_summary": symbol_selection_summary,
            "aggregate_validation": symbol_selection_validation,
            "execution_economics": (
                symbol_selection_economics["attribution_summary"]
            ),
            "cost_sensitivity": symbol_selection_economics[
                "cost_sensitivity"
            ],
            "exit_capture": symbol_selection_economics["exit_capture"],
            "execution_cost_plan": symbol_selection_economics[
                "execution_cost_plan"
            ],
            "execution_optimizer": symbol_selection_economics["optimizer"],
            "runs": symbol_selection_runs,
        }

    if prevalidated_selection_binding is not None:
        prevalidated_report = prevalidated_selection_binding["report"]
        prevalidated_runs = prevalidated_report.get("runs")
        prevalidated_symbol_reports = prevalidated_report.get(
            "symbol_reports"
        )
        if not isinstance(prevalidated_runs, list) or not isinstance(
            prevalidated_symbol_reports,
            dict,
        ):
            raise RuntimeError(
                "prevalidated selection evidence payload became invalid; "
                "final holdout remains unopened"
            )
        selection_candidate_runs = prevalidated_runs
        selection_candidate_symbol_reports = prevalidated_symbol_reports

    if frozen_corpus_binding is None:
        frozen_corpus_binding = build_frozen_corpus_binding(
            corpus_manifest,
            symbols=symbols,
            per_symbol=use_per_symbol_corpus,
        )

    # The exact Integrator candidate must clear selection before any final
    # holdout CSV is opened. Alpha probes remain diagnostics and cannot
    # substitute for this executable-candidate gate.
    selection_candidate_summary, selection_candidate_validation = (
        aggregate_run_summaries(
            selection_candidate_runs,
            min_execution_active_runs=args.min_execution_active_runs,
            min_execution_pass_runs=args.min_execution_pass_runs,
            min_total_fills=args.min_total_fills,
            min_mean_realized_net_per_fill=(
                args.min_mean_realized_net_per_fill
            ),
            warn_mean_filtered_cost_ratio=(
                args.warn_mean_filtered_cost_ratio
            ),
        )
    )
    selection_candidate_validation = merge_symbol_validations(
        selection_candidate_validation,
        selection_candidate_symbol_reports,
        min_mean_realized_net_per_fill=(
            args.min_mean_realized_net_per_fill
        ),
        min_tradable_symbols=args.min_tradable_symbols,
        source_symbol=source_symbol,
        final_holdout=True,
    )
    selection_candidate_economics = build_replay_economics_report(
        selection_candidate_runs,
        min_execution_active_runs=args.min_execution_active_runs,
        min_execution_pass_runs=args.min_execution_pass_runs,
        min_total_fills=args.min_total_fills,
        min_mean_realized_net_per_fill=(
            args.min_mean_realized_net_per_fill
        ),
        min_break_even_fee_multiplier=args.min_break_even_fee_multiplier,
    )
    selection_candidate_gate = build_activation_gate_report(
        aggregate_validation=selection_candidate_validation,
        economics_report=selection_candidate_economics,
        symbol_reports=selection_candidate_symbol_reports,
        source_symbol=source_symbol,
    )
    selection_candidate_manifest = {
        "schema_version": "exact_candidate_selection_manifest_v1",
        "generated_at_utc": now_utc_iso(),
        "candidate_identity": candidate_identity,
        "candidate_identity_sha256": (
            candidate_identity.get("identity_sha256", "")
            if isinstance(candidate_identity, dict)
            else ""
        ),
        "evidence_domain": SELECTION_CORPUS_EVIDENCE_DOMAIN,
        "frozen_corpus_binding": frozen_corpus_binding,
        "prevalidated_selection_binding": (
            {
                key: value
                for key, value in prevalidated_selection_binding.items()
                if key != "report"
            }
            if prevalidated_selection_binding is not None
            else None
        ),
        "status": selection_candidate_gate["status"],
        "activation_gate": selection_candidate_gate,
        "aggregate_summary": selection_candidate_summary,
        "aggregate_validation": selection_candidate_validation,
        "symbol_reports": selection_candidate_symbol_reports,
    }
    selection_candidate_manifest_path = (
        output_dir / "selection_candidate_manifest.json"
    )
    selection_candidate_manifest_path.write_text(
        json.dumps(
            selection_candidate_manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if (
        args.require_candidate_identity
        and selection_candidate_gate["status"] == "fail"
    ):
        raise RuntimeError(
            "exact candidate failed selection-validation; "
            "final holdout remains unopened: "
            + "; ".join(selection_candidate_gate["fail_reasons"])
        )

    holdout_claims: list[dict[str, Any]] = []
    if holdout_ledger is not None:
        pending_claims: list[dict[str, Any]] = []
        for symbol in symbols:
            symbol_feature_csv = feature_csv_by_symbol.get(
                symbol, feature_csv
            )
            if not symbol_feature_csv.is_file():
                raise FileNotFoundError(
                    f"{symbol} final holdout feature csv missing before claim: "
                    f"{symbol_feature_csv}"
                )
            start_ts, end_ts, interval_ms = inspect_feature_time_range(
                symbol_feature_csv
            )
            pending_claims.append(
                {
                    "symbol": symbol,
                    "bar_interval_ms": interval_ms,
                    "holdout_start_ts_ms": start_ts,
                    "holdout_end_ts_ms": end_ts,
                    "dataset_path": str(symbol_feature_csv),
                    "dataset_sha256": hashlib.sha256(
                        symbol_feature_csv.read_bytes()
                    ).hexdigest(),
                }
            )
        holdout_claims = claim_final_holdouts(
            holdout_ledger,
            experiment_id=args.experiment_id,
            candidate_identity_sha256=(
                str(candidate_identity.get("identity_sha256", ""))
                if isinstance(candidate_identity, dict)
                else ""
            ),
            holdouts=pending_claims,
        )

    for symbol in symbols:
        symbol_feature_csv = feature_csv_by_symbol.get(symbol, feature_csv)
        symbol_source = symbol if symbol in feature_csv_by_symbol else source_symbol
        source_symbols[symbol] = symbol_source
        source_matches_target = symbol_source == symbol
        per_symbol_source[symbol] = {
            "source_symbol": symbol_source,
            "feature_csv": str(symbol_feature_csv),
            "source_symbol_matches_target": source_matches_target,
            "real_market_replay": source_matches_target,
        }
        if not symbol_feature_csv.is_file():
            raise FileNotFoundError(
                f"{symbol} feature csv 不存在: {symbol_feature_csv}"
            )
        rows = load_feature_rows(symbol_feature_csv)
        if not rows:
            raise RuntimeError(f"{symbol} feature csv 无有效行: {symbol_feature_csv}")
        base_interval_ms = infer_base_interval_ms(rows)
        symbol_corpus_manifest = corpus_manifest_for_symbol(
            corpus_manifest,
            symbol,
            per_symbol=use_per_symbol_corpus,
        )
        if symbol_corpus_manifest is None:
            raise RuntimeError(f"{symbol} selection corpus manifest missing")
        manifest = load_corpus_manifest(symbol_corpus_manifest)
        thresholds = thresholds_from_manifest(manifest)
        selected, eligible, symbol_base_selection, symbol_warnings = (
            select_replay_segments(
                rows,
                thresholds,
                feature_csv=symbol_feature_csv,
                target_bucket=args.target_bucket,
                base_interval_ms=base_interval_ms,
                max_segments=max(1, args.max_segments),
                min_segment_bars=max(1, args.min_segment_bars),
                corpus_manifest=symbol_corpus_manifest,
                refresh_corpus_manifest=False,
                final_holdout=True,
                symbol=symbol,
            )
        )
        warnings.extend(f"{symbol}: {reason}" for reason in symbol_warnings)
        per_symbol_eligible_segment_count[symbol] = len(eligible)
        base_interval_ms_by_symbol[symbol] = base_interval_ms
        thresholds_by_symbol[symbol] = thresholds_to_payload(thresholds)
        available_segments_by_symbol[symbol] = [
            segment_to_payload(segment)
            for segment in selected
        ]
        symbol_contexts[symbol] = {
            "feature_csv": symbol_feature_csv,
            "rows": rows,
            "thresholds": thresholds,
            "base_interval_ms": base_interval_ms,
            "selected": selected,
            "eligible": eligible,
            "base_selection": symbol_base_selection,
            "available_segments": available_segments_by_symbol[symbol],
        }

    unmatched_symbols = [
        symbol
        for symbol, source in source_symbols.items()
        if source != symbol
    ]
    if unmatched_symbols:
        warnings.append(
            "multi-symbol replay 当前仍有目标币对复用非本币对 feature_csv: "
            f"unmatched_symbols={','.join(unmatched_symbols)}；"
            "这些结果适合验证执行链路和配置覆盖，不等同于目标币对真实历史行情验证"
        )
    real_market_replay = not unmatched_symbols

    for symbol in symbols:
        context = symbol_contexts[symbol]
        symbol_output_dir = output_dir if len(symbols) == 1 else output_dir / symbol
        (
            symbol_runs,
            symbol_selection,
            symbol_aggregate_summary,
            symbol_aggregate_validation,
            symbol_economics_report,
        ) = run_replay_for_symbol(
            symbol=symbol,
            output_dir=symbol_output_dir,
            rows=context["rows"],
            thresholds=context["thresholds"],
            selected_segments=context["selected"],
            target_bucket=args.target_bucket,
            base_interval_ms=context["base_interval_ms"],
            root=root,
            base_config=base_config,
            trade_bot=trade_bot,
            assess_stage=args.assess_stage,
            min_runtime_status=max(1, args.min_runtime_status),
            min_execution_active_runs=args.min_execution_active_runs,
            min_execution_pass_runs=args.min_execution_pass_runs,
            min_total_fills=args.min_total_fills,
            min_mean_realized_net_per_fill=args.min_mean_realized_net_per_fill,
            min_break_even_fee_multiplier=args.min_break_even_fee_multiplier,
            warn_mean_filtered_cost_ratio=args.warn_mean_filtered_cost_ratio,
            force_all_frozen_segments=args.force_all_frozen_segments,
        )
        symbol_selection = {
            **context["base_selection"],
            **symbol_selection,
        }
        run_summaries.extend(symbol_runs)
        per_symbol_segments_ran[symbol] = int(symbol_selection["segments_ran"])
        per_symbol_coverage_targets_met[symbol] = bool(
            symbol_selection["minimum_coverage_targets_met"]
        )
        per_symbol_recommended_coverage_targets_met[symbol] = bool(
            symbol_selection["recommended_coverage_targets_met"]
        )
        symbol_reports[symbol] = {
            "symbol": symbol,
            "output_dir": str(symbol_output_dir),
            "source": per_symbol_source[symbol],
            "feature_csv": str(context["feature_csv"]),
            "feature_sha256": hashlib.sha256(
                context["feature_csv"].read_bytes()
            ).hexdigest(),
            "base_interval_ms": context["base_interval_ms"],
            "thresholds": thresholds_by_symbol[symbol],
            "available_segments": context["available_segments"],
            "selection": symbol_selection,
            "aggregate_summary": symbol_aggregate_summary,
            "aggregate_validation": symbol_aggregate_validation,
            "execution_economics": symbol_economics_report["attribution_summary"],
            "cost_sensitivity": symbol_economics_report["cost_sensitivity"],
            "exit_capture": symbol_economics_report["exit_capture"],
            "execution_cost_plan": symbol_economics_report["execution_cost_plan"],
            "execution_optimizer": symbol_economics_report["optimizer"],
            "runs": symbol_runs,
        }

    aggregate_summary, aggregate_validation = aggregate_run_summaries(
        run_summaries,
        min_execution_active_runs=args.min_execution_active_runs,
        min_execution_pass_runs=args.min_execution_pass_runs,
        min_total_fills=args.min_total_fills,
        min_mean_realized_net_per_fill=args.min_mean_realized_net_per_fill,
        warn_mean_filtered_cost_ratio=args.warn_mean_filtered_cost_ratio,
    )
    aggregate_validation = merge_symbol_validations(
        aggregate_validation,
        symbol_reports,
        min_mean_realized_net_per_fill=args.min_mean_realized_net_per_fill,
        min_tradable_symbols=args.min_tradable_symbols,
        source_symbol=source_symbols.get(symbols[0], source_symbol),
        final_holdout=True,
    )
    economics_report = build_replay_economics_report(
        run_summaries,
        min_execution_active_runs=args.min_execution_active_runs,
        min_execution_pass_runs=args.min_execution_pass_runs,
        min_total_fills=args.min_total_fills,
        min_mean_realized_net_per_fill=args.min_mean_realized_net_per_fill,
        min_break_even_fee_multiplier=args.min_break_even_fee_multiplier,
    )
    activation_gate = build_activation_gate_report(
        aggregate_validation=aggregate_validation,
        economics_report=economics_report,
        symbol_reports=symbol_reports,
        source_symbol=source_symbols.get(symbols[0], source_symbol),
    )
    recommended_thresholds = derive_recommended_coverage_thresholds(
        min_execution_active_runs=args.min_execution_active_runs,
        min_execution_pass_runs=args.min_execution_pass_runs,
        min_total_fills=args.min_total_fills,
    )
    first_symbol = symbols[0]
    first_context = symbol_contexts[first_symbol]
    first_selection = first_context["base_selection"]

    report = {
        "execution_evidence_contract": {
            "schema_version": "replay_execution_prescreen_v1",
            "evidence_role": "offline_conservative_execution_prescreen",
            "fill_model": "next_bar_ohlc_touch_at_limit_no_queue_position",
            "production_promotion_authority": False,
            "live_candidate_episode_canary_required": True,
        },
        "feature_csv": str(feature_csv),
        "feature_csv_by_symbol": {
            symbol: str(path)
            for symbol, path in feature_csv_by_symbol.items()
        },
        "selection_feature_csv": (
            str(selection_feature_csv) if selection_feature_csv else ""
        ),
        "selection_feature_csv_by_symbol": {
            symbol: str(path)
            for symbol, path in selection_feature_csv_by_symbol.items()
        },
        "per_symbol_source": per_symbol_source,
        "source_symbols": source_symbols,
        "source_symbol_matches_target": real_market_replay,
        "real_market_replay": real_market_replay,
        "base_config": str(base_config),
        "candidate_identity": candidate_identity,
        "frozen_corpus_binding": frozen_corpus_binding,
        "prevalidated_selection_binding": (
            {
                key: value
                for key, value in prevalidated_selection_binding.items()
                if key != "report"
            }
            if prevalidated_selection_binding is not None
            else None
        ),
        "selection_candidate_manifest": {
            "path": str(selection_candidate_manifest_path),
            "sha256": hashlib.sha256(
                selection_candidate_manifest_path.read_bytes()
            ).hexdigest(),
            "candidate_identity_sha256": (
                selection_candidate_manifest.get(
                    "candidate_identity_sha256", ""
                )
            ),
            "status": selection_candidate_manifest.get("status"),
            "evidence_domain": selection_candidate_manifest.get(
                "evidence_domain"
            ),
            "activation_gate": selection_candidate_gate,
        },
        "holdout_consumption": {
            "schema_version": "final_holdout_consumption_binding_v1",
            "ledger_path": str(holdout_ledger) if holdout_ledger else "",
            "experiment_id": args.experiment_id,
            "claimed_before_evaluation": bool(holdout_claims),
            "claims": holdout_claims,
        },
        "trade_bot": str(trade_bot),
        "target_bucket": args.target_bucket,
        "source_symbol": source_symbols.get(first_symbol, source_symbol),
        "symbol": symbols[0],
        "symbols": symbols,
        "base_interval_ms": first_context["base_interval_ms"],
        "base_interval_ms_by_symbol": base_interval_ms_by_symbol,
        "thresholds": thresholds_by_symbol[first_symbol],
        "thresholds_by_symbol": thresholds_by_symbol,
        "available_segments": available_segments_by_symbol[first_symbol],
        "available_segments_by_symbol": available_segments_by_symbol,
        "selection": {
            **first_selection,
            "selection_mode": "per_symbol"
            if len(symbols) > 1 or bool(feature_csv_by_symbol)
            else first_selection.get("selection_mode"),
            "segments_ran": len(run_summaries),
            "per_symbol_selection": {
                symbol: symbol_report.get("selection", {})
                for symbol, symbol_report in symbol_reports.items()
            },
            "per_symbol_eligible_segment_count": per_symbol_eligible_segment_count,
            "per_symbol_segments_ran": per_symbol_segments_ran,
            "stopped_early": all(
                bool(item.get("selection", {}).get("stopped_early"))
                for item in symbol_reports.values()
            ),
            "stop_reason": "recommended_coverage_targets_met"
            if all(
                bool(item.get("selection", {}).get("stopped_early"))
                for item in symbol_reports.values()
            )
            else "",
            "per_symbol_coverage_targets_met": per_symbol_coverage_targets_met,
            "per_symbol_recommended_coverage_targets_met": (
                per_symbol_recommended_coverage_targets_met
            ),
            "coverage_targets_met": has_met_replay_coverage_targets(
                aggregate_summary,
                min_execution_active_runs=args.min_execution_active_runs,
                min_execution_pass_runs=args.min_execution_pass_runs,
                min_total_fills=args.min_total_fills,
            ),
            "minimum_coverage_targets_met": has_met_replay_coverage_targets(
                aggregate_summary,
                min_execution_active_runs=args.min_execution_active_runs,
                min_execution_pass_runs=args.min_execution_pass_runs,
                min_total_fills=args.min_total_fills,
            ),
            "recommended_coverage_targets_met": has_met_replay_coverage_targets(
                aggregate_summary,
                min_execution_active_runs=recommended_thresholds["min_execution_active_runs"],
                min_execution_pass_runs=recommended_thresholds["min_execution_pass_runs"],
                min_total_fills=recommended_thresholds["min_total_fills"],
            ),
        },
        "warnings": warnings,
        "status": activation_gate.get("status"),
        "activation_gate": activation_gate,
        "aggregate_summary": aggregate_summary,
        "aggregate_validation": aggregate_validation,
        "execution_economics": economics_report["attribution_summary"],
        "cost_sensitivity": economics_report["cost_sensitivity"],
        "exit_capture": economics_report["exit_capture"],
        "exit_capture_by_symbol": {
            symbol: symbol_report.get("exit_capture", {})
            for symbol, symbol_report in symbol_reports.items()
        },
        "execution_cost_plan": economics_report["execution_cost_plan"],
        "execution_optimizer": economics_report["optimizer"],
        "symbol_reports": symbol_reports,
        "runs": run_summaries,
    }
    report_path = output_dir / "replay_validation_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    optimization_report_path = output_dir / "replay_optimization_report.json"
    optimization_report = {
        "target_bucket": args.target_bucket,
        "symbols": symbols,
        "real_market_replay": real_market_replay,
        "execution_economics": economics_report["attribution_summary"],
        "cost_sensitivity": economics_report["cost_sensitivity"],
        "exit_capture": economics_report["exit_capture"],
        "execution_cost_plan": economics_report["execution_cost_plan"],
        "execution_optimizer": economics_report["optimizer"],
        "per_symbol": {
            symbol: {
                "execution_economics": symbol_report.get("execution_economics", {}),
                "cost_sensitivity": symbol_report.get("cost_sensitivity", {}),
                "exit_capture": symbol_report.get("exit_capture", {}),
                "execution_cost_plan": symbol_report.get("execution_cost_plan", {}),
                "execution_optimizer": symbol_report.get("execution_optimizer", {}),
            }
            for symbol, symbol_report in symbol_reports.items()
        },
    }
    optimization_report_path.write_text(
        json.dumps(optimization_report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(str(report_path))
    return (
        0
        if str(activation_gate.get("status", "")).strip().lower()
        in {"pass", "pass_with_actions"}
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
