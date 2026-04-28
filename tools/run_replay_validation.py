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
MIN_RECOMMENDED_TOTAL_FILLS = 6


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
    return payload


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
                selection["corpus_loaded"] = True
                selection["corpus_resolved_segment_count"] = len(resolved_segments)
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
        "warn_reasons": assess_payload.get("warn_reasons", []),
        "fail_reasons": assess_payload.get("fail_reasons", []),
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
        "mean_filtered_cost_ratio_avg": finite_mean(filtered_cost_values),
        "max_filtered_cost_ratio_avg": max(filtered_cost_values) if filtered_cost_values else None,
    }

    fail_reasons: list[str] = []
    warn_reasons: list[str] = []

    minimum_thresholds = {
        "min_execution_active_runs": max(1, min_execution_active_runs),
        "min_execution_pass_runs": max(1, min_execution_pass_runs),
        "min_total_fills": max(1, min_total_fills),
        "min_mean_realized_net_per_fill": float(min_mean_realized_net_per_fill),
        "warn_mean_filtered_cost_ratio": float(warn_mean_filtered_cost_ratio),
    }

    if execution_active_runs < minimum_thresholds["min_execution_active_runs"]:
        fail_reasons.append(
            "execution_active_runs="
            f"{execution_active_runs} < {minimum_thresholds['min_execution_active_runs']}"
        )
    if execution_pass_runs < max(1, min_execution_pass_runs):
        fail_reasons.append(
            "execution_pass_runs="
            f"{execution_pass_runs} < {minimum_thresholds['min_execution_pass_runs']}"
        )
    if total_fills < minimum_thresholds["min_total_fills"]:
        fail_reasons.append(
            f"total_fills={total_fills} < {minimum_thresholds['min_total_fills']}"
        )

    mean_realized_net_per_fill = aggregate_summary.get("mean_realized_net_per_fill")
    if isinstance(mean_realized_net_per_fill, (int, float)):
        if mean_realized_net_per_fill < min_mean_realized_net_per_fill:
            fail_reasons.append(
                "mean_realized_net_per_fill="
                f"{mean_realized_net_per_fill:.6f} < {min_mean_realized_net_per_fill:.6f}"
            )
    else:
        warn_reasons.append("无有效 realized_net_per_fill 样本，需结合 per-segment 结果复核")

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

    minimum_coverage_targets_met = not (
        execution_active_runs < minimum_thresholds["min_execution_active_runs"]
        or execution_pass_runs < minimum_thresholds["min_execution_pass_runs"]
        or total_fills < minimum_thresholds["min_total_fills"]
    )
    recommended_coverage_targets_met = has_met_replay_coverage_targets(
        aggregate_summary,
        min_execution_active_runs=recommended_thresholds["min_execution_active_runs"],
        min_execution_pass_runs=recommended_thresholds["min_execution_pass_runs"],
        min_total_fills=recommended_thresholds["min_total_fills"],
    )
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
        "thresholds": minimum_thresholds,
        "recommended_thresholds": recommended_thresholds,
        "minimum_coverage_targets_met": minimum_coverage_targets_met,
        "recommended_coverage_targets_met": recommended_coverage_targets_met,
        "coverage_strength_status": (
            "INSUFFICIENT"
            if fail_reasons
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


def merge_symbol_validations(
    aggregate_validation: dict[str, Any],
    symbol_reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(aggregate_validation)
    fail_reasons = list(merged.get("fail_reasons", []))
    warn_reasons = list(merged.get("warn_reasons", []))

    for symbol, symbol_report in symbol_reports.items():
        validation = symbol_report.get("aggregate_validation", {})
        if not isinstance(validation, dict):
            continue
        status = str(validation.get("status", "")).lower()
        if status == "fail":
            for reason in validation.get("fail_reasons", []):
                fail_reasons.append(f"{symbol}: {reason}")
        elif status == "pass_with_actions":
            for reason in validation.get("warn_reasons", []):
                warn_reasons.append(f"{symbol}: {reason}")

    if fail_reasons:
        merged["status"] = "fail"
        merged["coverage_strength_status"] = "INSUFFICIENT"
    elif warn_reasons and str(merged.get("status", "")).lower() == "pass":
        merged["status"] = "pass_with_actions"
        if merged.get("coverage_strength_status") == "ROBUST":
            merged["coverage_strength_status"] = "MINIMUM_ONLY"
    merged["fail_reasons"] = fail_reasons
    merged["warn_reasons"] = warn_reasons
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
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
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

        run_summaries.append(
            {
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
                "assess_summary": summarize_assess(assess_payload)
                if assess_payload
                else {},
            }
        )
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
    return run_summaries, symbol_selection, aggregate_summary, aggregate_validation


def main() -> int:
    parser = argparse.ArgumentParser(description="Run TREND replay validation on archived feature data.")
    parser.add_argument(
        "--feature_csv",
        default="data/research/feature_store_5m.tmp.csv",
        help="包含 close/volume/feature 列的特征 CSV",
    )
    parser.add_argument(
        "--base_config",
        default="config/bybit.replay.assess.yaml",
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
        default=-0.005,
        help="replay 聚合判定的 realized_net_per_fill 均值下限",
    )
    parser.add_argument(
        "--warn_mean_filtered_cost_ratio",
        type=float,
        default=0.80,
        help="replay 聚合 warning 的 filtered_cost_ratio_avg 均值阈值",
    )
    args = parser.parse_args()

    root = pathlib.Path(__file__).resolve().parent.parent
    feature_csv = (root / args.feature_csv).resolve() if not pathlib.Path(args.feature_csv).is_absolute() else pathlib.Path(args.feature_csv)
    base_config = (root / args.base_config).resolve() if not pathlib.Path(args.base_config).is_absolute() else pathlib.Path(args.base_config)
    trade_bot = (root / args.trade_bot).resolve() if not pathlib.Path(args.trade_bot).is_absolute() else pathlib.Path(args.trade_bot)
    output_dir = (root / args.output_dir).resolve() if not pathlib.Path(args.output_dir).is_absolute() else pathlib.Path(args.output_dir)
    corpus_manifest = None
    if args.corpus_manifest:
        corpus_manifest = (
            (root / args.corpus_manifest).resolve()
            if not pathlib.Path(args.corpus_manifest).is_absolute()
            else pathlib.Path(args.corpus_manifest)
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    if not feature_csv.is_file():
        raise FileNotFoundError(f"feature csv 不存在: {feature_csv}")
    if not base_config.is_file():
        raise FileNotFoundError(f"base config 不存在: {base_config}")
    if not trade_bot.is_file():
        raise FileNotFoundError(f"trade_bot 不存在: {trade_bot}")

    rows = load_feature_rows(feature_csv)
    if not rows:
        raise RuntimeError(f"feature csv 无有效行: {feature_csv}")
    thresholds = derive_regime_thresholds(rows)
    base_interval_ms = infer_base_interval_ms(rows)
    segments = find_segments(rows, thresholds, args.target_bucket, base_interval_ms)
    ranked_segments = rank_replay_segments(
        segments,
        rows,
        thresholds,
        target_bucket=args.target_bucket,
    )
    selected, eligible, selection, warnings = select_replay_segments(
        rows,
        thresholds,
        feature_csv=feature_csv,
        target_bucket=args.target_bucket,
        base_interval_ms=base_interval_ms,
        max_segments=max(1, args.max_segments),
        min_segment_bars=max(1, args.min_segment_bars),
        corpus_manifest=corpus_manifest,
        refresh_corpus_manifest=bool(args.refresh_corpus_manifest),
    )

    symbols = normalize_symbols(args.symbols, args.symbol)
    source_symbol = str(args.source_symbol or args.symbol).strip().upper()
    if source_symbol and any(symbol != source_symbol for symbol in symbols):
        warnings.append(
            "multi-symbol replay 当前复用同一份 feature_csv: "
            f"source_symbol={source_symbol}, target_symbols={','.join(symbols)}；"
            "该结果适合验证执行链路和配置覆盖，不等同于目标币对真实历史行情验证"
        )
    run_summaries: list[dict[str, Any]] = []
    symbol_reports: dict[str, dict[str, Any]] = {}
    per_symbol_segments_ran: dict[str, int] = {}
    per_symbol_coverage_targets_met: dict[str, bool] = {}
    per_symbol_recommended_coverage_targets_met: dict[str, bool] = {}

    for symbol in symbols:
        symbol_output_dir = output_dir if len(symbols) == 1 else output_dir / symbol
        (
            symbol_runs,
            symbol_selection,
            symbol_aggregate_summary,
            symbol_aggregate_validation,
        ) = run_replay_for_symbol(
            symbol=symbol,
            output_dir=symbol_output_dir,
            rows=rows,
            thresholds=thresholds,
            selected_segments=selected,
            target_bucket=args.target_bucket,
            base_interval_ms=base_interval_ms,
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
            "selection": symbol_selection,
            "aggregate_summary": symbol_aggregate_summary,
            "aggregate_validation": symbol_aggregate_validation,
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
    )
    recommended_thresholds = derive_recommended_coverage_thresholds(
        min_execution_active_runs=args.min_execution_active_runs,
        min_execution_pass_runs=args.min_execution_pass_runs,
        min_total_fills=args.min_total_fills,
    )

    report = {
        "feature_csv": str(feature_csv),
        "base_config": str(base_config),
        "trade_bot": str(trade_bot),
        "target_bucket": args.target_bucket,
        "source_symbol": source_symbol,
        "symbol": symbols[0],
        "symbols": symbols,
        "base_interval_ms": base_interval_ms,
        "thresholds": {
            "trend_abs_ema_diff": thresholds.trend_abs_ema_diff,
            "trend_abs_mom_48": thresholds.trend_abs_mom_48,
            "extreme_vol_12": thresholds.extreme_vol_12,
            "extreme_range_pct": thresholds.extreme_range_pct,
        },
        "available_segments": [
            segment_to_payload(
                segment,
                rows=rows,
                thresholds=thresholds,
                target_bucket=args.target_bucket,
            )
            for segment in ranked_segments[:10]
        ],
        "selection": {
            **selection,
            "segments_ran": len(run_summaries),
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
        "aggregate_summary": aggregate_summary,
        "aggregate_validation": aggregate_validation,
        "symbol_reports": symbol_reports,
        "runs": run_summaries,
    }
    report_path = output_dir / "replay_validation_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(str(report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
