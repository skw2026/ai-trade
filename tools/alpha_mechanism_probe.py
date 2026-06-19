#!/usr/bin/env python3
"""
Probe whether the closed-loop alpha mechanism can distinguish learnable edge
from noise on the same market return distribution used by strategy diagnose.

This is deliberately not a deployable strategy. It is a mechanism proof layer:
- oracle/noisy-oracle controls should pass after cost on holdout;
- random control should not pass;
- real feature candidates are reported separately and may fail.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


SCHEMA_VERSION = "alpha_mechanism_probe_v1"
CANDIDATE_MANIFEST_SCHEMA_VERSION = "alpha_candidate_manifest_v1"
OBJECTIVE_FIXED_FORWARD = "fixed_forward"
OBJECTIVE_PATH_FIRST_TOUCH = "path_first_touch"


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_float(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def normalize_symbol(value: str, fallback: str = "SOURCE") -> str:
    symbol = str(value or "").strip().upper()
    return symbol or fallback


def parse_feature_paths(entries: List[str], default_symbol: str) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    plain_index = 0
    for raw in entries:
        item = str(raw or "").strip()
        if not item:
            continue
        if "=" in item:
            symbol, path = item.split("=", 1)
            symbol = normalize_symbol(symbol, "")
            if symbol and path.strip():
                out[symbol] = Path(path.strip())
            continue
        symbol = default_symbol if plain_index == 0 else f"{default_symbol}_{plain_index}"
        out[normalize_symbol(symbol)] = Path(item)
        plain_index += 1
    return out


def quantile(values: Iterable[float], q: float, default: float = 0.0) -> float:
    clean = sorted(item for item in values if math.isfinite(item))
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


def load_feature_rows(symbol: str, path: Path) -> List[Dict[str, float]]:
    rows: List[Dict[str, float]] = []
    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for raw in reader:
            timestamp = safe_float(raw.get("timestamp"), math.nan)
            forward_return = safe_float(raw.get("forward_return"), math.nan)
            if not math.isfinite(timestamp) or not math.isfinite(forward_return):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": timestamp,
                    "close": safe_float(raw.get("close"), math.nan),
                    "volume": safe_float(raw.get("volume"), math.nan),
                    "forward_return": forward_return,
                    "ret_1": safe_float(raw.get("ret_1"), math.nan),
                    "ret_3": safe_float(raw.get("ret_3"), math.nan),
                    "ret_12": safe_float(raw.get("ret_12"), math.nan),
                    "ema_diff": safe_float(raw.get("ema_diff"), math.nan),
                    "mom_12": safe_float(raw.get("mom_12"), math.nan),
                    "mom_48": safe_float(raw.get("mom_48"), math.nan),
                    "zscore_48": safe_float(raw.get("zscore_48"), math.nan),
                    "vol_12": safe_float(raw.get("vol_12"), math.nan),
                    "vol_48": safe_float(raw.get("vol_48"), math.nan),
                    "range_pct": safe_float(raw.get("range_pct"), math.nan),
                    "vol_chg_12": safe_float(raw.get("vol_chg_12"), math.nan),
                }
            )
    rows.sort(key=lambda item: (str(item["symbol"]), item["timestamp"]))
    return rows


def attach_path_metrics(rows: List[Dict[str, Any]], *, horizon_bars: int) -> None:
    """Attach future close-path metrics used by path-aware alpha objectives."""
    if int(horizon_bars) <= 0:
        return
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_symbol.setdefault(str(row.get("symbol", "SOURCE")), []).append(row)

    for symbol_rows in by_symbol.values():
        symbol_rows.sort(key=lambda item: float(item.get("timestamp", 0.0)))
        closes = [safe_float(row.get("close"), math.nan) for row in symbol_rows]
        for index, row in enumerate(symbol_rows):
            row["path_ready"] = False
            row["path_horizon_bars"] = 0
            row["path_forward_bps_series"] = []
            entry = closes[index]
            if not math.isfinite(entry) or entry <= 0.0:
                continue
            future = closes[index + 1 : index + 1 + int(horizon_bars)]
            if len(future) < int(horizon_bars) or any(not math.isfinite(item) for item in future):
                continue
            path = [(item / entry - 1.0) * 10000.0 for item in future]
            row["path_ready"] = True
            row["path_horizon_bars"] = int(horizon_bars)
            row["path_forward_bps_series"] = path
            row["path_long_mfe_bps"] = max(path)
            row["path_long_mae_bps"] = min(path)


def split_rows(
    rows: List[Dict[str, float]],
    *,
    holdout_fraction: float,
    min_train_samples: int,
    min_holdout_samples: int,
) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
    n = len(rows)
    if n <= min_train_samples + min_holdout_samples:
        return rows, []
    holdout = max(min_holdout_samples, int(round(float(n) * float(holdout_fraction))))
    holdout = min(holdout, n - min_train_samples)
    split = n - holdout
    return rows[:split], rows[split:]


def direction_from_score(score: float, threshold: float) -> int:
    if not math.isfinite(score) or abs(score) == 0.0 or abs(score) < threshold:
        return 0
    return 1 if score > 0.0 else -1


def path_objective_gross_bps(
    row: Dict[str, Any],
    signal: int,
    *,
    path_take_profit_bps: float,
    path_stop_loss_bps: float,
) -> Tuple[float | None, str | None, float | None, float | None]:
    if signal == 0:
        return None, None, None, None
    path = row.get("path_forward_bps_series")
    if not isinstance(path, list) or not path:
        return None, None, None, None
    directed: List[float] = []
    for item in path:
        value = safe_float(item, math.nan)
        if math.isfinite(value):
            directed.append(float(signal) * value)
    if not directed:
        return None, None, None, None
    mfe = max(directed)
    mae = min(directed)
    for value in directed:
        if value >= float(path_take_profit_bps):
            return float(path_take_profit_bps), "take_profit", mfe, mae
        if value <= -float(path_stop_loss_bps):
            return -float(path_stop_loss_bps), "stop_loss", mfe, mae
    return directed[-1], "horizon", mfe, mae


def objective_oracle_signal(
    row: Dict[str, Any],
    *,
    objective_mode: str,
    round_trip_cost_bps: float,
    min_mean_net_bps: float,
    path_take_profit_bps: float,
    path_stop_loss_bps: float,
) -> int:
    if objective_mode == OBJECTIVE_PATH_FIRST_TOUCH:
        long_gross, _, _, _ = path_objective_gross_bps(
            row,
            1,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        )
        short_gross, _, _, _ = path_objective_gross_bps(
            row,
            -1,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        )
        choices = [
            (1, long_gross if long_gross is not None else float("-inf")),
            (-1, short_gross if short_gross is not None else float("-inf")),
        ]
        signal, gross = max(choices, key=lambda item: item[1])
        if not math.isfinite(gross) or gross <= float(round_trip_cost_bps) + float(min_mean_net_bps):
            return 0
        return signal
    return direction_from_score(float(row["forward_return"]), 0.0)


def objective_signal_score(
    row: Dict[str, Any],
    *,
    objective_mode: str,
    round_trip_cost_bps: float,
    min_mean_net_bps: float,
    path_take_profit_bps: float,
    path_stop_loss_bps: float,
) -> float:
    if objective_mode == OBJECTIVE_PATH_FIRST_TOUCH:
        signal = objective_oracle_signal(
            row,
            objective_mode=objective_mode,
            round_trip_cost_bps=round_trip_cost_bps,
            min_mean_net_bps=min_mean_net_bps,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        )
        if signal == 0:
            return 0.0
        gross, _, _, _ = path_objective_gross_bps(
            row,
            signal,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        )
        if gross is None:
            return 0.0
        return float(signal) * max(0.0, float(gross) - float(round_trip_cost_bps))
    return float(row["forward_return"]) * 10000.0


def summarize_net(
    rows: List[Dict[str, Any]],
    signals: List[int],
    *,
    round_trip_cost_bps: float,
    objective_mode: str = OBJECTIVE_FIXED_FORWARD,
    path_take_profit_bps: float = 16.0,
    path_stop_loss_bps: float = 8.0,
) -> Dict[str, Any]:
    gross: List[float] = []
    net: List[float] = []
    mfe_values: List[float] = []
    mae_values: List[float] = []
    coverage_values: List[float] = []
    capture_values: List[float] = []
    exit_counts: Dict[str, int] = {}
    for row, signal in zip(rows, signals):
        if signal == 0:
            continue
        if objective_mode == OBJECTIVE_PATH_FIRST_TOUCH:
            gross_out, exit_reason, mfe, mae = path_objective_gross_bps(
                row,
                signal,
                path_take_profit_bps=path_take_profit_bps,
                path_stop_loss_bps=path_stop_loss_bps,
            )
            if gross_out is None:
                continue
            gross_bps = float(gross_out)
            exit_counts[str(exit_reason or "unknown")] = exit_counts.get(str(exit_reason or "unknown"), 0) + 1
            if mfe is not None and math.isfinite(float(mfe)):
                mfe_values.append(float(mfe))
                if float(round_trip_cost_bps) > 0.0:
                    coverage_values.append(float(mfe) / float(round_trip_cost_bps))
                if float(mfe) > 0.0:
                    capture_values.append(gross_bps / float(mfe))
            if mae is not None and math.isfinite(float(mae)):
                mae_values.append(float(mae))
        else:
            gross_bps = float(signal) * float(row["forward_return"]) * 10000.0
        gross.append(gross_bps)
        net.append(gross_bps - float(round_trip_cost_bps))
    positives = [item for item in net if item > 0.0]
    summary: Dict[str, Any] = {
        "sample_count": len(net),
        "mean_gross_bps": mean(gross),
        "median_gross_bps": median(gross),
        "mean_net_bps": mean(net),
        "median_net_bps": median(net),
        "positive_ratio": (len(positives) / float(len(net))) if net else None,
    }
    if objective_mode == OBJECTIVE_PATH_FIRST_TOUCH:
        summary.update(
            {
                "objective_mode": OBJECTIVE_PATH_FIRST_TOUCH,
                "path_take_profit_bps": float(path_take_profit_bps),
                "path_stop_loss_bps": float(path_stop_loss_bps),
                "mean_path_mfe_bps": mean(mfe_values),
                "median_path_mfe_bps": median(mfe_values),
                "mean_path_mae_bps": mean(mae_values),
                "median_path_mae_bps": median(mae_values),
                "mean_mfe_cost_coverage_ratio": mean(coverage_values),
                "mean_gross_capture_of_path_mfe": mean(capture_values),
                "exit_reason_counts": exit_counts,
            }
        )
        total = float(len(net)) if net else 0.0
        if total > 0.0:
            summary["take_profit_hit_ratio"] = float(exit_counts.get("take_profit", 0)) / total
            summary["stop_loss_hit_ratio"] = float(exit_counts.get("stop_loss", 0)) / total
            summary["horizon_exit_ratio"] = float(exit_counts.get("horizon", 0)) / total
    return summary


def objective_status(
    summary: Dict[str, Any],
    *,
    min_samples: int,
    min_mean_net_bps: float,
    min_positive_ratio: float,
    min_mfe_cost_coverage: float = 0.0,
) -> Tuple[str, List[str]]:
    fail_reasons: List[str] = []
    sample_count = int(summary.get("sample_count") or 0)
    mean_net = summary.get("mean_net_bps")
    positive_ratio = summary.get("positive_ratio")
    if sample_count < int(min_samples):
        fail_reasons.append(f"sample_count={sample_count} < min_samples={min_samples}")
    if not isinstance(mean_net, (int, float)) or float(mean_net) <= float(min_mean_net_bps):
        fail_reasons.append(
            f"mean_net_bps={mean_net} <= min_mean_net_bps={float(min_mean_net_bps)}"
        )
    if not isinstance(positive_ratio, (int, float)) or float(positive_ratio) < float(min_positive_ratio):
        fail_reasons.append(
            f"positive_ratio={positive_ratio} < min_positive_ratio={float(min_positive_ratio)}"
        )
    coverage = summary.get("mean_mfe_cost_coverage_ratio")
    if (
        float(min_mfe_cost_coverage) > 0.0
        and isinstance(coverage, (int, float))
        and float(coverage) < float(min_mfe_cost_coverage)
    ):
        fail_reasons.append(
            "mean_mfe_cost_coverage_ratio="
            f"{coverage} < min_mfe_cost_coverage={float(min_mfe_cost_coverage)}"
        )
    return ("pass" if not fail_reasons else "fail", fail_reasons)


def score_for_spec(row: Dict[str, float], spec: Dict[str, Any]) -> float:
    source = str(spec.get("source", "")).strip()
    mode = str(spec.get("mode", "follow")).strip()
    if source == "trend":
        ema = safe_float(row.get("ema_diff"), math.nan)
        mom = safe_float(row.get("mom_48"), math.nan)
        if not math.isfinite(ema) or not math.isfinite(mom) or ema * mom <= 0.0:
            return math.nan
        sign = 1.0 if ema > 0.0 else -1.0
        score = sign * min(abs(ema) / 5e-4, abs(mom) / 2e-3)
    elif source == "volatility_breakout":
        ret_3 = safe_float(row.get("ret_3"), math.nan)
        vol_12 = safe_float(row.get("vol_12"), math.nan)
        vol_48 = safe_float(row.get("vol_48"), math.nan)
        if not all(math.isfinite(item) for item in (ret_3, vol_12, vol_48)):
            return math.nan
        expansion = max(0.0, vol_12 / max(abs(vol_48), 1e-12) - 1.0)
        score = ret_3 * (1.0 + expansion)
    elif source == "volatility_contraction_breakout":
        ret_3 = safe_float(row.get("ret_3"), math.nan)
        vol_12 = safe_float(row.get("vol_12"), math.nan)
        vol_48 = safe_float(row.get("vol_48"), math.nan)
        if not all(math.isfinite(item) for item in (ret_3, vol_12, vol_48)):
            return math.nan
        contraction = max(0.0, vol_48 / max(abs(vol_12), 1e-12) - 1.0)
        score = ret_3 * (1.0 + contraction)
    elif source == "range_expansion_follow":
        ret_1 = safe_float(row.get("ret_1"), math.nan)
        range_pct = safe_float(row.get("range_pct"), math.nan)
        vol_12 = safe_float(row.get("vol_12"), math.nan)
        if not all(math.isfinite(item) for item in (ret_1, range_pct, vol_12)):
            return math.nan
        range_pressure = range_pct / max(abs(vol_12), 1e-12)
        score = ret_1 * range_pressure
    elif source == "quiet_range_reversion":
        zscore = safe_float(row.get("zscore_48"), math.nan)
        vol_12 = safe_float(row.get("vol_12"), math.nan)
        vol_48 = safe_float(row.get("vol_48"), math.nan)
        range_pct = safe_float(row.get("range_pct"), math.nan)
        if not all(math.isfinite(item) for item in (zscore, vol_12, vol_48, range_pct)):
            return math.nan
        quietness = max(0.0, vol_48 / max(abs(vol_12), 1e-12) - 1.0)
        range_penalty = 1.0 / max(1.0, range_pct / max(abs(vol_12), 1e-12))
        score = -zscore * (1.0 + quietness) * range_penalty
    elif source == "ret_vol_adjusted":
        ret_12 = safe_float(row.get("ret_12"), math.nan)
        vol_12 = safe_float(row.get("vol_12"), math.nan)
        if not all(math.isfinite(item) for item in (ret_12, vol_12)):
            return math.nan
        score = ret_12 / max(abs(vol_12), 1e-12)
    else:
        score = safe_float(row.get(source), math.nan)
    if mode == "inverse":
        score = -score
    return score


def candidate_specs() -> List[Dict[str, Any]]:
    return [
        {"name": "trend_follow", "family": "legacy_trend", "source": "trend", "mode": "follow"},
        {"name": "trend_inverse", "family": "legacy_trend", "source": "trend", "mode": "inverse"},
        {"name": "mom12_follow", "family": "legacy_momentum", "source": "mom_12", "mode": "follow"},
        {"name": "mom12_inverse", "family": "legacy_momentum", "source": "mom_12", "mode": "inverse"},
        {"name": "ret3_follow", "family": "legacy_return", "source": "ret_3", "mode": "follow"},
        {"name": "ret3_inverse", "family": "legacy_return", "source": "ret_3", "mode": "inverse"},
        {"name": "ret1_inverse", "family": "legacy_return", "source": "ret_1", "mode": "inverse"},
        {"name": "zscore_follow", "family": "legacy_reversion", "source": "zscore_48", "mode": "follow"},
        {"name": "zscore_inverse", "family": "legacy_reversion", "source": "zscore_48", "mode": "inverse"},
        {
            "name": "vol_breakout_follow",
            "family": "volatility_breakout",
            "source": "volatility_breakout",
            "mode": "follow",
        },
        {
            "name": "vol_breakout_inverse",
            "family": "volatility_breakout",
            "source": "volatility_breakout",
            "mode": "inverse",
        },
        {
            "name": "vol_contraction_breakout_follow",
            "family": "volatility_contraction_breakout",
            "source": "volatility_contraction_breakout",
            "mode": "follow",
        },
        {
            "name": "range_expansion_follow",
            "family": "range_expansion",
            "source": "range_expansion_follow",
            "mode": "follow",
        },
        {
            "name": "quiet_range_reversion",
            "family": "range_reversion",
            "source": "quiet_range_reversion",
            "mode": "follow",
        },
        {
            "name": "ret_vol_adjusted_follow",
            "family": "risk_adjusted_momentum",
            "source": "ret_vol_adjusted",
            "mode": "follow",
        },
        {
            "name": "ret_vol_adjusted_inverse",
            "family": "risk_adjusted_momentum",
            "source": "ret_vol_adjusted",
            "mode": "inverse",
        },
    ]


def evaluate_candidate(
    rows: List[Dict[str, Any]],
    spec: Dict[str, Any],
    threshold: float,
    *,
    round_trip_cost_bps: float,
    objective_mode: str = OBJECTIVE_FIXED_FORWARD,
    path_take_profit_bps: float = 16.0,
    path_stop_loss_bps: float = 8.0,
) -> Dict[str, Any]:
    signals = [
        direction_from_score(score_for_spec(row, spec), float(threshold))
        for row in rows
    ]
    summary = summarize_net(
        rows,
        signals,
        round_trip_cost_bps=round_trip_cost_bps,
        objective_mode=objective_mode,
        path_take_profit_bps=path_take_profit_bps,
        path_stop_loss_bps=path_stop_loss_bps,
    )
    return {
        "spec": spec,
        "threshold": float(threshold),
        "summary": summary,
    }


def thresholds_for_scores(scores: List[float]) -> List[float]:
    clean_abs = [abs(item) for item in scores if math.isfinite(item)]
    if not clean_abs:
        return [0.0]
    values = [
        0.0,
        quantile(clean_abs, 0.25),
        quantile(clean_abs, 0.50),
        quantile(clean_abs, 0.75),
    ]
    out: List[float] = []
    for value in values:
        if math.isfinite(value) and value not in out:
            out.append(value)
    return out


def rank_key(candidate: Dict[str, Any]) -> Tuple[int, float, float, int]:
    summary = candidate.get("holdout", {}).get("summary", {})
    status = candidate.get("holdout_status")
    mean_net = summary.get("mean_net_bps")
    positive = summary.get("positive_ratio")
    samples = int(summary.get("sample_count") or 0)
    return (
        1 if status == "pass" else 0,
        float(mean_net) if isinstance(mean_net, (int, float)) else float("-inf"),
        float(positive) if isinstance(positive, (int, float)) else float("-inf"),
        samples,
    )


def run_candidate_search(
    train_rows: List[Dict[str, Any]],
    holdout_rows: List[Dict[str, Any]],
    *,
    round_trip_cost_bps: float,
    min_samples: int,
    min_mean_net_bps: float,
    min_positive_ratio: float,
    min_mfe_cost_coverage: float,
    objective_mode: str,
    path_take_profit_bps: float,
    path_stop_loss_bps: float,
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for spec in candidate_specs():
        scores = [score_for_spec(row, spec) for row in train_rows]
        best_train: Dict[str, Any] | None = None
        for threshold in thresholds_for_scores(scores):
            train_eval = evaluate_candidate(
                train_rows,
                spec,
                threshold,
                round_trip_cost_bps=round_trip_cost_bps,
                objective_mode=objective_mode,
                path_take_profit_bps=path_take_profit_bps,
                path_stop_loss_bps=path_stop_loss_bps,
            )
            summary = train_eval["summary"]
            mean_net = summary.get("mean_net_bps")
            sample_count = int(summary.get("sample_count") or 0)
            key = (
                float(mean_net) if isinstance(mean_net, (int, float)) else float("-inf"),
                sample_count,
            )
            if best_train is None:
                best_train = train_eval
                best_key = key
            elif key > best_key:
                best_train = train_eval
                best_key = key
        if best_train is None:
            continue
        holdout_eval = evaluate_candidate(
            holdout_rows,
            spec,
            float(best_train["threshold"]),
            round_trip_cost_bps=round_trip_cost_bps,
            objective_mode=objective_mode,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        )
        holdout_status, holdout_fails = objective_status(
            holdout_eval["summary"],
            min_samples=min_samples,
            min_mean_net_bps=min_mean_net_bps,
            min_positive_ratio=min_positive_ratio,
            min_mfe_cost_coverage=min_mfe_cost_coverage,
        )
        candidates.append(
            {
                "name": spec["name"],
                "spec": spec,
                "threshold": float(best_train["threshold"]),
                "train": best_train,
                "holdout": holdout_eval,
                "holdout_status": holdout_status,
                "holdout_fail_reasons": holdout_fails,
            }
        )

    ranked = sorted(candidates, key=rank_key, reverse=True)
    pass_candidates = [item for item in ranked if item.get("holdout_status") == "pass"]
    return {
        "status": "pass" if pass_candidates else "fail",
        "candidate_count": len(candidates),
        "pass_candidate_count": len(pass_candidates),
        "best_candidate": ranked[0] if ranked else None,
        "pass_candidates": pass_candidates[:10],
        "ranked_candidates": ranked[:20],
        "fail_reasons": [] if pass_candidates else ["no_market_alpha_candidate_passed_holdout_after_cost"],
    }


def build_candidate_manifest(
    candidate_search: Dict[str, Any],
    *,
    symbols: List[str],
    round_trip_cost_bps: float,
    min_mean_net_bps: float,
    min_positive_ratio: float,
    min_mfe_cost_coverage: float,
    objective_mode: str,
    path_horizon_bars: int,
    path_take_profit_bps: float,
    path_stop_loss_bps: float,
) -> Dict[str, Any]:
    pass_candidates = candidate_search.get("pass_candidates", [])
    if not isinstance(pass_candidates, list):
        pass_candidates = []
    selected = pass_candidates[0] if pass_candidates else None
    best = candidate_search.get("best_candidate")
    if not isinstance(best, dict):
        best = None

    rejected = []
    ranked = candidate_search.get("ranked_candidates", [])
    if isinstance(ranked, list):
        for item in ranked[:20]:
            if not isinstance(item, dict):
                continue
            spec = item.get("spec", {})
            if not isinstance(spec, dict):
                spec = {}
            rejected.append(
                {
                    "name": item.get("name"),
                    "family": spec.get("family"),
                    "source": spec.get("source"),
                    "mode": spec.get("mode"),
                    "holdout_status": item.get("holdout_status"),
                    "holdout_fail_reasons": item.get("holdout_fail_reasons", []),
                    "holdout_summary": (
                        item.get("holdout", {}).get("summary", {})
                        if isinstance(item.get("holdout"), dict)
                        else {}
                    ),
                }
            )

    manifest: Dict[str, Any] = {
        "schema_version": CANDIDATE_MANIFEST_SCHEMA_VERSION,
        "generated_at_utc": now_utc_iso(),
        "status": "pass" if isinstance(selected, dict) else "fail",
        "symbols": symbols,
        "round_trip_cost_bps": float(round_trip_cost_bps),
        "objective": {
            "mode": objective_mode,
            "path_horizon_bars": int(path_horizon_bars),
            "path_take_profit_bps": float(path_take_profit_bps),
            "path_stop_loss_bps": float(path_stop_loss_bps),
            "min_mean_net_bps": float(min_mean_net_bps),
            "min_positive_ratio": float(min_positive_ratio),
            "min_mfe_cost_coverage": float(min_mfe_cost_coverage),
        },
        "selected_candidate": None,
        "best_rejected_candidate": best,
        "rejected_candidates": rejected,
        "fail_reasons": [] if isinstance(selected, dict) else list(candidate_search.get("fail_reasons", [])),
    }
    if not isinstance(selected, dict):
        return manifest

    spec = selected.get("spec", {})
    if not isinstance(spec, dict):
        spec = {}
    holdout = selected.get("holdout", {})
    if not isinstance(holdout, dict):
        holdout = {}
    manifest["selected_candidate"] = {
        "name": selected.get("name"),
        "family": spec.get("family"),
        "source": spec.get("source"),
        "mode": spec.get("mode"),
        "threshold": selected.get("threshold"),
        "holdout_summary": holdout.get("summary", {}),
        "deployable_config": {
            "signal_family": spec.get("family"),
            "signal_source": spec.get("source"),
            "direction_mode": spec.get("mode"),
            "score_threshold": selected.get("threshold"),
            "round_trip_cost_bps": float(round_trip_cost_bps),
            "objective_mode": objective_mode,
            "path_horizon_bars": int(path_horizon_bars),
            "path_take_profit_bps": float(path_take_profit_bps),
            "path_stop_loss_bps": float(path_stop_loss_bps),
            "min_mfe_cost_coverage": float(min_mfe_cost_coverage),
            "symbols": symbols,
        },
    }
    return manifest


def oracle_control(
    rows: List[Dict[str, Any]],
    *,
    round_trip_cost_bps: float,
    min_samples: int,
    min_mean_net_bps: float,
    min_positive_ratio: float,
    min_mfe_cost_coverage: float,
    objective_mode: str,
    path_take_profit_bps: float,
    path_stop_loss_bps: float,
) -> Dict[str, Any]:
    signals = [
        objective_oracle_signal(
            row,
            objective_mode=objective_mode,
            round_trip_cost_bps=round_trip_cost_bps,
            min_mean_net_bps=min_mean_net_bps,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        )
        for row in rows
    ]
    summary = summarize_net(
        rows,
        signals,
        round_trip_cost_bps=round_trip_cost_bps,
        objective_mode=objective_mode,
        path_take_profit_bps=path_take_profit_bps,
        path_stop_loss_bps=path_stop_loss_bps,
    )
    status, fails = objective_status(
        summary,
        min_samples=min_samples,
        min_mean_net_bps=min_mean_net_bps,
        min_positive_ratio=min_positive_ratio,
        min_mfe_cost_coverage=min_mfe_cost_coverage,
    )
    return {
        "status": status,
        "fail_reasons": fails,
        "summary": summary,
        "control_type": f"oracle_{objective_mode}_direction",
    }


def noisy_oracle_control(
    train_rows: List[Dict[str, Any]],
    holdout_rows: List[Dict[str, Any]],
    *,
    round_trip_cost_bps: float,
    min_samples: int,
    min_mean_net_bps: float,
    min_positive_ratio: float,
    min_mfe_cost_coverage: float,
    objective_mode: str,
    path_take_profit_bps: float,
    path_stop_loss_bps: float,
) -> Dict[str, Any]:
    train_bps = [
        objective_signal_score(
            row,
            objective_mode=objective_mode,
            round_trip_cost_bps=round_trip_cost_bps,
            min_mean_net_bps=min_mean_net_bps,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        )
        for row in train_rows
    ]
    scale = max(1.0, abs(quantile(train_bps, 0.75) - quantile(train_bps, 0.25)))
    rng = random.Random(20260619)

    def score(row: Dict[str, Any]) -> float:
        return objective_signal_score(
            row,
            objective_mode=objective_mode,
            round_trip_cost_bps=round_trip_cost_bps,
            min_mean_net_bps=min_mean_net_bps,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        ) + rng.gauss(0.0, scale * 0.35)

    train_scores = [score(row) for row in train_rows]
    holdout_scores = [score(row) for row in holdout_rows]
    candidates: List[Dict[str, Any]] = []
    for threshold in thresholds_for_scores(train_scores):
        train_signals = [direction_from_score(item, threshold) for item in train_scores]
        holdout_signals = [direction_from_score(item, threshold) for item in holdout_scores]
        train_summary = summarize_net(
            train_rows,
            train_signals,
            round_trip_cost_bps=round_trip_cost_bps,
            objective_mode=objective_mode,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        )
        holdout_summary = summarize_net(
            holdout_rows,
            holdout_signals,
            round_trip_cost_bps=round_trip_cost_bps,
            objective_mode=objective_mode,
            path_take_profit_bps=path_take_profit_bps,
            path_stop_loss_bps=path_stop_loss_bps,
        )
        candidates.append(
            {
                "threshold": float(threshold),
                "train": {"summary": train_summary},
                "holdout": {"summary": holdout_summary},
            }
        )
    def control_rank(item: Dict[str, Any]) -> Tuple[int, float, int]:
        train_summary = item["train"]["summary"]
        holdout_summary = item["holdout"]["summary"]
        mean_net = train_summary.get("mean_net_bps")
        train_sample_count = int(train_summary.get("sample_count") or 0)
        holdout_sample_count = int(holdout_summary.get("sample_count") or 0)
        return (
            1 if train_sample_count >= min_samples and holdout_sample_count >= min_samples else 0,
            float(mean_net) if isinstance(mean_net, (int, float)) else float("-inf"),
            min(train_sample_count, holdout_sample_count),
        )

    best = max(candidates, key=control_rank)
    status, fails = objective_status(
        best["holdout"]["summary"],
        min_samples=min_samples,
        min_mean_net_bps=min_mean_net_bps,
        min_positive_ratio=min_positive_ratio,
        min_mfe_cost_coverage=min_mfe_cost_coverage,
    )
    best["status"] = status
    best["fail_reasons"] = fails
    best["control_type"] = "noisy_oracle_score_selected_on_train"
    return best


def random_control(
    rows: List[Dict[str, Any]],
    *,
    round_trip_cost_bps: float,
    min_samples: int,
    min_mean_net_bps: float,
    min_positive_ratio: float,
    min_mfe_cost_coverage: float,
    objective_mode: str,
    path_take_profit_bps: float,
    path_stop_loss_bps: float,
) -> Dict[str, Any]:
    rng = random.Random(42)
    signals = [1 if rng.random() >= 0.5 else -1 for _ in rows]
    summary = summarize_net(
        rows,
        signals,
        round_trip_cost_bps=round_trip_cost_bps,
        objective_mode=objective_mode,
        path_take_profit_bps=path_take_profit_bps,
        path_stop_loss_bps=path_stop_loss_bps,
    )
    objective, objective_fails = objective_status(
        summary,
        min_samples=min_samples,
        min_mean_net_bps=min_mean_net_bps,
        min_positive_ratio=min_positive_ratio,
        min_mfe_cost_coverage=min_mfe_cost_coverage,
    )
    unexpected = objective == "pass"
    return {
        "status": "fail" if unexpected else "pass",
        "fail_reasons": ["random_control_passed_net_objective_unexpectedly"] if unexpected else [],
        "objective_status": objective,
        "objective_fail_reasons": objective_fails,
        "summary": summary,
        "control_type": "random_direction",
    }


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    feature_paths = parse_feature_paths(args.feature_csv, normalize_symbol(args.symbol))
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    objective_mode = str(args.objective_mode)
    all_rows: List[Dict[str, Any]] = []
    train_rows: List[Dict[str, Any]] = []
    holdout_rows: List[Dict[str, Any]] = []
    by_symbol: Dict[str, Any] = {}
    for symbol, path in sorted(feature_paths.items()):
        if not path.is_file():
            fail_reasons.append(f"feature_csv missing: {symbol}={path}")
            continue
        rows = load_feature_rows(symbol, path)
        if objective_mode == OBJECTIVE_PATH_FIRST_TOUCH:
            attach_path_metrics(rows, horizon_bars=int(args.path_horizon_bars))
        all_rows.extend(rows)
        symbol_train, symbol_holdout = split_rows(
            rows,
            holdout_fraction=float(args.holdout_fraction),
            min_train_samples=int(args.min_train_samples),
            min_holdout_samples=int(args.min_holdout_samples),
        )
        train_rows.extend(symbol_train)
        holdout_rows.extend(symbol_holdout)
        by_symbol[symbol] = {
            "feature_csv": str(path),
            "row_count": len(rows),
            "train_count": len(symbol_train),
            "holdout_count": len(symbol_holdout),
            "path_ready_count": sum(1 for row in rows if bool(row.get("path_ready"))),
        }

    train_rows.sort(key=lambda item: (str(item["symbol"]), float(item["timestamp"])))
    holdout_rows.sort(key=lambda item: (str(item["symbol"]), float(item["timestamp"])))
    if not holdout_rows:
        fail_reasons.append(
            f"insufficient rows for holdout: rows={len(all_rows)}, "
            f"min_train={args.min_train_samples}, min_holdout={args.min_holdout_samples}"
        )

    controls: Dict[str, Any] = {}
    candidate_search: Dict[str, Any] = {}
    candidate_manifest: Dict[str, Any] = {}
    if holdout_rows:
        controls = {
            "positive_oracle": oracle_control(
                holdout_rows,
                round_trip_cost_bps=float(args.round_trip_cost_bps),
                min_samples=int(args.min_holdout_samples),
                min_mean_net_bps=float(args.min_mean_net_bps),
                min_positive_ratio=float(args.min_positive_ratio),
                min_mfe_cost_coverage=float(args.min_mfe_cost_coverage),
                objective_mode=objective_mode,
                path_take_profit_bps=float(args.path_take_profit_bps),
                path_stop_loss_bps=float(args.path_stop_loss_bps),
            ),
            "positive_noisy_oracle": noisy_oracle_control(
                train_rows,
                holdout_rows,
                round_trip_cost_bps=float(args.round_trip_cost_bps),
                min_samples=int(args.min_holdout_samples),
                min_mean_net_bps=float(args.min_mean_net_bps),
                min_positive_ratio=float(args.min_positive_ratio),
                min_mfe_cost_coverage=float(args.min_mfe_cost_coverage),
                objective_mode=objective_mode,
                path_take_profit_bps=float(args.path_take_profit_bps),
                path_stop_loss_bps=float(args.path_stop_loss_bps),
            ),
            "negative_random": random_control(
                holdout_rows,
                round_trip_cost_bps=float(args.round_trip_cost_bps),
                min_samples=int(args.min_holdout_samples),
                min_mean_net_bps=float(args.min_mean_net_bps),
                min_positive_ratio=float(args.min_positive_ratio),
                min_mfe_cost_coverage=float(args.min_mfe_cost_coverage),
                objective_mode=objective_mode,
                path_take_profit_bps=float(args.path_take_profit_bps),
                path_stop_loss_bps=float(args.path_stop_loss_bps),
            ),
        }
        candidate_search = run_candidate_search(
            train_rows,
            holdout_rows,
            round_trip_cost_bps=float(args.round_trip_cost_bps),
            min_samples=int(args.min_holdout_samples),
            min_mean_net_bps=float(args.min_mean_net_bps),
            min_positive_ratio=float(args.min_positive_ratio),
            min_mfe_cost_coverage=float(args.min_mfe_cost_coverage),
            objective_mode=objective_mode,
            path_take_profit_bps=float(args.path_take_profit_bps),
            path_stop_loss_bps=float(args.path_stop_loss_bps),
        )
        candidate_manifest = build_candidate_manifest(
            candidate_search,
            symbols=sorted(by_symbol.keys()),
            round_trip_cost_bps=float(args.round_trip_cost_bps),
            min_mean_net_bps=float(args.min_mean_net_bps),
            min_positive_ratio=float(args.min_positive_ratio),
            min_mfe_cost_coverage=float(args.min_mfe_cost_coverage),
            objective_mode=objective_mode,
            path_horizon_bars=int(args.path_horizon_bars),
            path_take_profit_bps=float(args.path_take_profit_bps),
            path_stop_loss_bps=float(args.path_stop_loss_bps),
        )
    else:
        candidate_manifest = {
            "schema_version": CANDIDATE_MANIFEST_SCHEMA_VERSION,
            "generated_at_utc": now_utc_iso(),
            "status": "fail",
            "symbols": sorted(by_symbol.keys()),
            "selected_candidate": None,
            "best_rejected_candidate": None,
            "rejected_candidates": [],
            "fail_reasons": ["insufficient_holdout_samples"],
        }

    control_failures = [
        name
        for name, control in controls.items()
        if isinstance(control, dict) and control.get("status") != "pass"
    ]
    mechanism_control_status = "pass" if controls and not control_failures else "fail"
    market_alpha_status = str(candidate_search.get("status", "not_evaluated"))
    if control_failures:
        fail_reasons.extend(f"{name}: status={controls[name].get('status')}" for name in control_failures)
    if market_alpha_status == "fail":
        warn_reasons.extend(str(item) for item in candidate_search.get("fail_reasons", []))

    if fail_reasons:
        status = "fail"
        readiness = "FAIL"
    elif market_alpha_status == "pass":
        status = "pass"
        readiness = "PASS"
    else:
        status = "pass_with_actions"
        readiness = "PASS_WITH_ACTIONS"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": now_utc_iso(),
        "status": status,
        "readiness_status": readiness,
        "mechanism_control_status": mechanism_control_status,
        "market_alpha_family_status": market_alpha_status,
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "target": {
            "round_trip_cost_bps": float(args.round_trip_cost_bps),
            "objective_mode": objective_mode,
            "path_horizon_bars": int(args.path_horizon_bars),
            "path_take_profit_bps": float(args.path_take_profit_bps),
            "path_stop_loss_bps": float(args.path_stop_loss_bps),
            "holdout_fraction": float(args.holdout_fraction),
            "min_train_samples": int(args.min_train_samples),
            "min_holdout_samples": int(args.min_holdout_samples),
            "min_mean_net_bps": float(args.min_mean_net_bps),
            "min_positive_ratio": float(args.min_positive_ratio),
            "min_mfe_cost_coverage": float(args.min_mfe_cost_coverage),
        },
        "data": {
            "row_count": len(all_rows),
            "train_count": len(train_rows),
            "holdout_count": len(holdout_rows),
            "by_symbol": by_symbol,
        },
        "controls": controls,
        "candidate_search": candidate_search,
        "deployable_candidate_manifest": candidate_manifest,
        "next_actions": [
            "If mechanism_control_status fails, fix the objective/probe before any strategy tuning.",
            "If mechanism_control_status passes but market_alpha_family_status fails, replace the alpha family instead of tuning execution thresholds.",
            "Only run long live windows after a holdout-positive market alpha candidate also passes replay stress.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe alpha mechanism controls")
    parser.add_argument("--output", required=True)
    parser.add_argument("--symbol", default="SOURCE")
    parser.add_argument(
        "--feature_csv",
        action="append",
        default=[],
        help="feature_store CSV path or SYMBOL=path; may be repeated",
    )
    parser.add_argument("--round-trip-cost-bps", type=float, default=3.5)
    parser.add_argument(
        "--objective-mode",
        choices=[OBJECTIVE_FIXED_FORWARD, OBJECTIVE_PATH_FIRST_TOUCH],
        default=OBJECTIVE_FIXED_FORWARD,
        help="net-edge objective for controls and market candidates",
    )
    parser.add_argument("--path-horizon-bars", type=int, default=12)
    parser.add_argument("--path-take-profit-bps", type=float, default=16.0)
    parser.add_argument("--path-stop-loss-bps", type=float, default=8.0)
    parser.add_argument("--holdout-fraction", type=float, default=0.30)
    parser.add_argument("--min-train-samples", type=int, default=200)
    parser.add_argument("--min-holdout-samples", type=int, default=100)
    parser.add_argument("--min-mean-net-bps", type=float, default=0.0)
    parser.add_argument("--min-positive-ratio", type=float, default=0.50)
    parser.add_argument("--min-mfe-cost-coverage", type=float, default=0.0)
    parser.add_argument(
        "--candidate-manifest-output",
        default="",
        help="optional alpha_candidate_manifest_v1 output path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.candidate_manifest_output:
        manifest = report.get("deployable_candidate_manifest", {})
        manifest_output = Path(args.candidate_manifest_output)
        manifest_output.parent.mkdir(parents=True, exist_ok=True)
        manifest_output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "status": report["status"],
                "mechanism_control_status": report["mechanism_control_status"],
                "market_alpha_family_status": report["market_alpha_family_status"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] in {"pass", "pass_with_actions"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
