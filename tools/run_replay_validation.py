#!/usr/bin/env python3
"""
Run replay validation on archived TREND segments.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


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
MIN_RECOMMENDED_EXECUTION_ACTIVE_RUNS = 4
MIN_RECOMMENDED_EXECUTION_PASS_RUNS = 4
MIN_RECOMMENDED_TOTAL_FILLS = 20
MIN_POSITIVE_FILLED_SEGMENT_RATIO = 0.55


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
        for raw in reader:
            timestamp_raw = raw.get("timestamp", "")
            if not timestamp_raw.isdigit():
                continue
            features = {name: safe_float(raw.get(name, "")) for name in FEATURE_COLUMNS}
            rows.append(
                FeatureRow(
                    timestamp=int(timestamp_raw),
                    close=safe_float(raw.get("close", "")),
                    volume=max(0.0, safe_float(raw.get("volume", ""))),
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
        trend_abs_ema_diff=max(5e-4, quantile(ema_diff, 0.65, 5e-4)),
        trend_abs_mom_48=max(2e-3, quantile(mom_48, 0.65, 2e-3)),
        extreme_vol_12=max(1.5e-3, quantile(vol_12, 0.90, 1.5e-3)),
        extreme_range_pct=max(3e-3, quantile(range_pct, 0.90, 3e-3)),
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
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "timestamp",
                "symbol",
                "price",
                "volume",
                "interval_ms",
                "funding_rate_per_interval",
            ]
        )
        previous_timestamp: int | None = None
        for row in rows[segment.start_index : segment.end_index + 1]:
            interval_ms = default_interval_ms
            if previous_timestamp is not None:
                interval_ms = max(1, row.timestamp - previous_timestamp)
            writer.writerow(
                [
                    row.timestamp,
                    symbol,
                    f"{row.close:.10f}",
                    f"{row.volume:.10f}",
                    interval_ms,
                    "",
                ]
            )
            previous_timestamp = row.timestamp


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
    target_bucket: str,
    base_interval_ms: int,
    thresholds: RegimeThresholds,
    max_segments: int,
    min_segment_bars: int,
    selected_segments: list[ReplaySegment],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "generated_at": now_utc_iso(),
        "source_feature_csv": str(feature_csv),
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
        "segments": [segment_to_payload(segment) for segment in selected_segments],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


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
) -> tuple[list[ReplaySegment], list[ReplaySegment], dict[str, Any], list[str]]:
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

    fill_count = int_or_zero(summary.get("funnel_fills_runtime_count"))
    attribution_fill_count = int_or_zero(summary.get("execution_attribution_fill_count"))
    if fill_count <= 0 and attribution_fill_count > 0:
        fill_count = attribution_fill_count

    realized_net_per_fill = number_or_none(summary.get("realized_net_per_fill"))
    realized_net_usd = (
        float(realized_net_per_fill) * float(fill_count)
        if realized_net_per_fill is not None and fill_count > 0
        else 0.0
    )
    fee_usd = number_or_none(summary.get("execution_attribution_fee_usd"))
    if fee_usd is None:
        fee_usd = number_or_none(
            summary.get("execution_attribution_runtime_fee_delta_usd")
        )
    fee_usd = float(fee_usd or 0.0)
    fee_per_fill_usd = fee_usd / fill_count if fill_count > 0 else 0.0
    estimated_gross_pnl_usd = realized_net_usd + fee_usd
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
        "fill_count": fill_count,
        "execution_activity_count": int_or_zero(summary.get("execution_activity_count")),
        "realized_net_per_fill": realized_net_per_fill,
        "realized_net_usd_est": realized_net_usd,
        "fee_usd": fee_usd,
        "fee_per_fill_usd": fee_per_fill_usd,
        "notional_abs_usd": notional_abs_usd,
        "notional_abs_per_fill_usd": notional_abs_per_fill_usd,
        "reported_fee_bps_per_fill": reported_fee_bps_per_fill,
        "derived_fee_bps_per_fill": derived_fee_bps_per_fill,
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
    rows_with_fills = [
        row for row in economics_rows if int_or_zero(row.get("fill_count")) > 0
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
    total_fills = sum(int_or_zero(row.get("fill_count")) for row in economics_rows)
    total_realized_net = sum(
        float(row.get("realized_net_usd_est") or 0.0) for row in economics_rows
    )
    total_fee = sum(float(row.get("fee_usd") or 0.0) for row in economics_rows)
    total_gross = sum(
        float(row.get("estimated_gross_pnl_usd") or 0.0)
        for row in economics_rows
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
        "filled_segment_count": len(rows_with_fills),
        "total_fills": total_fills,
        "positive_filled_segments": positive_rows,
        "negative_filled_segments": negative_rows,
        "zero_filled_segments": zero_rows,
        "total_realized_net_usd_est": total_realized_net,
        "total_fee_usd": total_fee,
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
        row for row in economics_rows if int_or_zero(row.get("fill_count")) > 0
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

    diagnostics: list[str] = []
    if rows_with_fills and not samples:
        diagnostics.append("insufficient_exit_capture_samples")
    if samples and fee_bound_count >= max(1, math.ceil(len(samples) * 0.5)):
        diagnostics.append("path_mfe_does_not_cover_fee_buffer")
    if samples and low_capture_count >= max(1, math.ceil(len(samples) * 0.5)):
        diagnostics.append("path_mfe_covers_cost_but_gross_capture_low")
    if samples and gross_cost_bound_count >= max(1, math.ceil(len(samples) * 0.5)):
        diagnostics.append("gross_edge_does_not_cover_current_fee")

    if "path_mfe_covers_cost_but_gross_capture_low" in diagnostics:
        primary_diagnosis = "exit_capture_low"
    elif "path_mfe_does_not_cover_fee_buffer" in diagnostics:
        primary_diagnosis = "fee_bound_or_low_path"
    elif "gross_edge_does_not_cover_current_fee" in diagnostics:
        primary_diagnosis = "execution_cost_bound"
    elif not samples:
        primary_diagnosis = "insufficient_samples"
    else:
        primary_diagnosis = "no_obvious_exit_capture_issue"

    return {
        "status": "action_required" if diagnostics else "pass",
        "primary_diagnosis": primary_diagnosis,
        "diagnostics": diagnostics,
        "filled_segment_count": len(rows_with_fills),
        "sample_count": len(samples),
        "low_capture_segment_count": low_capture_count,
        "fee_bound_segment_count": fee_bound_count,
        "gross_cost_bound_segment_count": gross_cost_bound_count,
        "mean_path_fee_coverage_ratio": finite_mean(path_fee_coverages),
        "median_path_fee_coverage_ratio": finite_median(path_fee_coverages),
        "mean_gross_capture_of_path_mfe": finite_mean(gross_capture_ratios),
        "median_gross_capture_of_path_mfe": finite_median(gross_capture_ratios),
        "mean_gross_fee_coverage_ratio": finite_mean(gross_fee_coverages),
        "mean_fee_bps_per_fill": finite_mean(fee_bps_values),
        "mean_path_mfe_bps": finite_mean(path_mfe_bps_values),
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
        fill_count = int_or_zero(row.get("fill_count"))
        if fill_count <= 0:
            continue
        gross = float(row.get("estimated_gross_pnl_usd") or 0.0)
        fee = float(row.get("fee_usd") or 0.0)
        gross_per_fill = gross / fill_count
        adjusted_fee_per_fill = fee * fee_multiplier / fill_count
        edge_after_adjusted_fee = gross_per_fill - adjusted_fee_per_fill
        if edge_after_adjusted_fee >= min_gross_over_adjusted_fee_per_fill_usd:
            selected_rows.append(row)

    net_values: list[float] = []
    total_fills = 0
    total_gross = 0.0
    total_adjusted_fee = 0.0
    for row in selected_rows:
        fill_count = int_or_zero(row.get("fill_count"))
        gross = float(row.get("estimated_gross_pnl_usd") or 0.0)
        adjusted_fee = float(row.get("fee_usd") or 0.0) * fee_multiplier
        adjusted_net = gross - adjusted_fee
        total_fills += fill_count
        total_gross += gross
        total_adjusted_fee += adjusted_fee
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
        "total_adjusted_realized_net_usd_est": total_gross - total_adjusted_fee,
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
        row for row in economics_rows if int_or_zero(row.get("fill_count")) > 0
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
    current_net_per_fill = safe_ratio(total_gross - total_fee, float(total_fills))
    break_even_fee_multiplier = safe_ratio(total_gross, total_fee)
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
        row for row in economics_rows if int_or_zero(row.get("fill_count")) > 0
    ]
    total_fee = sum(float(row.get("fee_usd") or 0.0) for row in filled_rows)
    total_gross = sum(
        float(row.get("estimated_gross_pnl_usd") or 0.0) for row in filled_rows
    )
    break_even_fee_multiplier = (
        total_gross / total_fee if total_fee > 1e-12 else None
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
                gross_per_fill = (
                    float(row.get("estimated_gross_pnl_usd") or 0.0) / fill_count
                )
                if gross_per_fill - adjusted_fee_per_fill >= min_edge:
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
    validation_status = str(aggregate_validation.get("status", "")).lower()
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
    )
    return {
        "name": name,
        "description": description,
        "diagnostic_only": bool(diagnostic_only),
        "filters": filters,
        "status": "pass" if pass_status else "fail",
        "selected_segments": len(selected),
        "aggregate_summary": aggregate_summary,
        "aggregate_validation": aggregate_validation,
        "economics_summary": economics_summary,
    }


def build_replay_execution_optimizer(
    run_summaries: list[dict[str, Any]],
    *,
    min_execution_active_runs: int,
    min_execution_pass_runs: int,
    min_total_fills: int,
    min_mean_realized_net_per_fill: float,
) -> dict[str, Any]:
    if not run_summaries:
        return {
            "status": "fail",
            "fail_reasons": ["no_replay_runs"],
            "warn_reasons": [],
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
        )
        for name, description, filters, diagnostic_only in policy_specs
    ]
    deployable_candidates = [
        candidate for candidate in candidates if not candidate["diagnostic_only"]
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
    return {
        "status": "pass" if pass_candidates else "fail",
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "candidate_count": len(candidates),
        "deployable_candidate_count": len(deployable_candidates),
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
        "candidates": candidates,
    }


def build_replay_economics_report(
    run_summaries: list[dict[str, Any]],
    *,
    min_execution_active_runs: int,
    min_execution_pass_runs: int,
    min_total_fills: int,
    min_mean_realized_net_per_fill: float,
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
        ),
    }


def build_activation_gate_report(
    *,
    aggregate_validation: dict[str, Any],
    economics_report: dict[str, Any],
    symbol_reports: dict[str, dict[str, Any]],
    source_symbol: str,
) -> dict[str, Any]:
    fail_reasons: list[str] = []
    warn_reasons: list[str] = []

    aggregate_status = str(aggregate_validation.get("status", "")).strip().lower()
    if aggregate_status == "fail":
        fail_reasons.extend(
            str(item)
            for item in aggregate_validation.get("fail_reasons", [])
            if str(item).strip()
        )
        if not fail_reasons:
            fail_reasons.append("aggregate_validation.status=fail")
    elif aggregate_status == "pass_with_actions":
        warn_reasons.extend(
            str(item)
            for item in aggregate_validation.get("warn_reasons", [])
            if str(item).strip()
        )

    optimizer = economics_report.get("optimizer", {})
    if not isinstance(optimizer, dict):
        optimizer = {}
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
        for item in tradeability.get("tradable_symbols", [])
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
                fail_reasons.append(
                    f"{symbol}: mean_gross_capture_of_path_mfe={mean_capture:.6f} < 0.100000"
                )
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
                fail_reasons.append(
                    f"mean_gross_capture_of_path_mfe={mean_capture:.6f} < 0.100000"
                )

    status = "pass"
    if fail_reasons:
        status = "fail"
    elif warn_reasons:
        status = "pass_with_actions"
    return {
        "status": status,
        "fail_reasons": list(dict.fromkeys(fail_reasons)),
        "warn_reasons": list(dict.fromkeys(warn_reasons)),
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
    total_fills = sum(int(summary.get("funnel_fills_runtime_count") or 0) for summary in summaries)
    realized_net_values = [
        float(summary["realized_net_per_fill"])
        for summary in summaries
        if isinstance(summary.get("realized_net_per_fill"), (int, float))
    ]
    realized_net_values_with_fills = [
        float(summary["realized_net_per_fill"])
        for summary in summaries
        if int(summary.get("funnel_fills_runtime_count") or 0) > 0
        and isinstance(summary.get("realized_net_per_fill"), (int, float))
    ]
    zero_realized_net_with_fills_runs = sum(
        1
        for summary in summaries
        if int(summary.get("funnel_fills_runtime_count") or 0) > 0
        and isinstance(summary.get("realized_net_per_fill"), (int, float))
        and abs(float(summary["realized_net_per_fill"])) <= 1e-12
    )
    nonzero_realized_net_with_fills_runs = sum(
        1
        for summary in summaries
        if int(summary.get("funnel_fills_runtime_count") or 0) > 0
        and isinstance(summary.get("realized_net_per_fill"), (int, float))
        and abs(float(summary["realized_net_per_fill"])) > 1e-12
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
        "execution_active_runs": execution_active_runs,
        "execution_pass_runs": execution_pass_runs,
        "protection_pass_runs": protection_pass_runs,
        "trend_present_runs": trend_present_runs,
        "pass_with_actions_runs": pass_with_actions_runs,
        "failed_runs": failed_runs,
        "total_execution_activity_count": total_execution_activity_count,
        "total_fills": total_fills,
        "mean_realized_net_per_fill": finite_mean(realized_net_values),
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
        elif not economic_ok or all_filled_runs_negative:
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
        else:
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
        item.upper() for item in tradable_symbols
    }:
        fail_reasons.append(f"source_symbol_not_tradable={source_symbol_normalized}")
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
        "quarantined_symbols": quarantined_symbols,
        "insufficient_symbols": insufficient_symbols,
        "tradable_symbol_count": len(tradable_symbols),
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
) -> dict[str, Any]:
    merged = dict(aggregate_validation)
    raw_aggregate_fail_reasons = [
        str(item)
        for item in merged.get("fail_reasons", [])
        if str(item).strip()
    ]
    aggregate_status = str(merged.get("status", "")).strip().lower()
    fail_reasons: list[str] = []
    warn_reasons = list(merged.get("warn_reasons", []))
    symbol_quarantine_reasons: list[str] = []
    non_quarantined_symbol_fail_reasons: list[str] = []
    tradeability = build_symbol_tradeability(
        symbol_reports,
        min_mean_realized_net_per_fill=min_mean_realized_net_per_fill,
        min_tradable_symbols=min_tradable_symbols,
        source_symbol=source_symbol,
    )
    tradeability_status = str(tradeability.get("status", "")).lower()
    decisions = tradeability.get("decisions", {})
    if not isinstance(decisions, dict):
        decisions = {}
    for reason in tradeability.get("warn_reasons", []):
        reason_text = str(reason).strip()
        if reason_text:
            warn_reasons.append(reason_text)

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
                else:
                    non_quarantined_symbol_fail_reasons.append(item)
        elif status == "pass_with_actions":
            for reason in validation.get("warn_reasons", []):
                warn_reasons.append(f"{symbol}: {reason}")

    suppressed_aggregate_fail_reasons: list[str] = []
    if tradeability_status == "pass":
        if aggregate_status not in {"pass", "pass_with_actions"}:
            suppressed_aggregate_fail_reasons.append(
                f"aggregate_validation status={aggregate_status or 'unknown'}"
            )
        suppressed_aggregate_fail_reasons.extend(raw_aggregate_fail_reasons)
        suppressed_aggregate_fail_reasons.extend(non_quarantined_symbol_fail_reasons)
        if suppressed_aggregate_fail_reasons:
            warn_reasons.append(
                "aggregate_validation_failed_but_symbol_tradeability_passed: "
                + "; ".join(suppressed_aggregate_fail_reasons)
            )
    else:
        fail_reasons.extend(raw_aggregate_fail_reasons)
        fail_reasons.extend(non_quarantined_symbol_fail_reasons)
        for reason in tradeability.get("fail_reasons", []):
            reason_text = str(reason).strip()
            if reason_text:
                fail_reasons.append(reason_text)
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
    elif tradeability_status == "pass":
        merged["status"] = "pass_with_actions" if warn_reasons else "pass"
    elif warn_reasons and str(merged.get("status", "")).lower() == "pass":
        merged["status"] = "pass_with_actions"
    elif str(merged.get("status", "")).lower() == "pass_with_actions" and not warn_reasons:
        merged["status"] = "pass"
    merged["fail_reasons"] = fail_reasons
    merged["warn_reasons"] = warn_reasons
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
    warn_mean_filtered_cost_ratio: float,
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

    for idx, segment in enumerate(selected_segments, start=1):
        segment_dir = output_dir / f"segment_{idx:02d}"
        segment_dir.mkdir(parents=True, exist_ok=True)
        replay_csv = segment_dir / "replay_market.csv"
        runtime_log = segment_dir / "runtime.log"
        runtime_assess = segment_dir / "runtime_assess.json"
        write_replay_csv(rows, segment, symbol, replay_csv, base_interval_ms)

        trade_cmd = [
            str(trade_bot),
            f"--config={base_config}",
            "--exchange=bybit",
            f"--replay_market_data={replay_csv}",
            "--replay_timestamp_column=timestamp",
            "--replay_symbol_column=symbol",
            "--replay_price_column=price",
            "--replay_volume_column=volume",
            "--replay_interval_column=interval_ms",
            "--replay_funding_rate_column=funding_rate_per_interval",
            f"--replay_default_interval_ms={base_interval_ms}",
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
        ]
        assess_exit = subprocess.run(assess_cmd, check=False).returncode
        assess_payload: dict[str, Any] = {}
        if runtime_assess.is_file():
            assess_payload = json.loads(runtime_assess.read_text(encoding="utf-8"))
        assess_summary = summarize_assess(assess_payload) if assess_payload else {}
        run_payload = {
            "symbol": symbol,
            "segment_index": idx,
            "segment": segment_to_payload(
                segment,
                rows=rows,
                thresholds=thresholds,
                target_bucket=target_bucket,
            ),
            "replay_csv": str(replay_csv),
            "runtime_log": str(runtime_log),
            "runtime_assess": str(runtime_assess),
            "trade_bot_exit_code": trade_exit,
            "assess_exit_code": int(assess_exit),
            "assess_summary": assess_summary,
        }
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
        if has_met_replay_coverage_targets(
            aggregate_summary,
            min_execution_active_runs=recommended_thresholds[
                "min_execution_active_runs"
            ],
            min_execution_pass_runs=recommended_thresholds[
                "min_execution_pass_runs"
            ],
            min_total_fills=recommended_thresholds["min_total_fills"],
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
    )
    symbol_selection = {
        "segments_ran": len(run_summaries),
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
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
        "--refresh_corpus_manifest",
        action="store_true",
        help="忽略已有 corpus manifest，重新按当前 feature csv 选段并覆盖写入",
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
    args = parser.parse_args()

    root = pathlib.Path(__file__).resolve().parent.parent
    feature_csv = resolve_path(args.feature_csv, root)
    feature_csv_by_symbol = parse_feature_csv_by_symbol(
        args.feature_csv_by_symbol,
        root,
    )
    base_config = resolve_path(args.base_config, root)
    trade_bot = resolve_path(args.trade_bot, root)
    output_dir = resolve_path(args.output_dir, root)
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

    use_per_symbol_corpus = bool(feature_csv_by_symbol)
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
        thresholds = derive_regime_thresholds(rows)
        base_interval_ms = infer_base_interval_ms(rows)
        segments = find_segments(rows, thresholds, args.target_bucket, base_interval_ms)
        ranked_segments = rank_replay_segments(
            segments,
            rows,
            thresholds,
            target_bucket=args.target_bucket,
        )
        symbol_corpus_manifest = corpus_manifest_for_symbol(
            corpus_manifest,
            symbol,
            per_symbol=use_per_symbol_corpus,
        )
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
                refresh_corpus_manifest=bool(args.refresh_corpus_manifest),
            )
        )
        warnings.extend(f"{symbol}: {reason}" for reason in symbol_warnings)
        per_symbol_eligible_segment_count[symbol] = len(eligible)
        base_interval_ms_by_symbol[symbol] = base_interval_ms
        thresholds_by_symbol[symbol] = thresholds_to_payload(thresholds)
        available_segments_by_symbol[symbol] = [
            segment_to_payload(
                segment,
                rows=rows,
                thresholds=thresholds,
                target_bucket=args.target_bucket,
            )
            for segment in ranked_segments[:10]
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
            warn_mean_filtered_cost_ratio=args.warn_mean_filtered_cost_ratio,
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
    )
    economics_report = build_replay_economics_report(
        run_summaries,
        min_execution_active_runs=args.min_execution_active_runs,
        min_execution_pass_runs=args.min_execution_pass_runs,
        min_total_fills=args.min_total_fills,
        min_mean_realized_net_per_fill=args.min_mean_realized_net_per_fill,
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
        "feature_csv": str(feature_csv),
        "feature_csv_by_symbol": {
            symbol: str(path)
            for symbol, path in feature_csv_by_symbol.items()
        },
        "per_symbol_source": per_symbol_source,
        "source_symbols": source_symbols,
        "source_symbol_matches_target": real_market_replay,
        "real_market_replay": real_market_replay,
        "base_config": str(base_config),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
