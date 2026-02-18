#!/usr/bin/env python3
"""
运行日志自动验收脚本（DEPLOY/S3/S5）。

用途：
1. 对 `run_s3.log` / `run_s5.log` 做统一 PASS/FAIL 判定；
2. 汇总关键运行指标，减少人工翻日志成本；
3. 生成可归档 JSON，便于阶段对比。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict


@dataclass(frozen=True)
class StageRule:
    name: str
    min_runtime_status: int
    require_gate_window: bool
    require_gate_pass: bool
    require_evolution_init: bool
    require_execution_activity: bool
    require_flat_start: bool
    max_start_abs_notional_usd: float
    max_trading_halted_true_ratio: float
    gate_fail_hard_min_windows: int
    gate_fail_hard_max_fail_ratio: float
    gate_warn_min_windows: int
    gate_warn_max_fail_ratio: float
    min_strategy_mix_nonzero_windows: int


STAGE_RULES: Dict[str, StageRule] = {
    "DEPLOY": StageRule(
        name="DEPLOY",
        min_runtime_status=0,
        require_gate_window=False,
        require_gate_pass=False,
        require_evolution_init=False,
        require_execution_activity=False,
        require_flat_start=False,
        max_start_abs_notional_usd=0.0,
        max_trading_halted_true_ratio=0.50,
        gate_fail_hard_min_windows=0,
        gate_fail_hard_max_fail_ratio=1.10,
        gate_warn_min_windows=10,
        gate_warn_max_fail_ratio=0.95,
        min_strategy_mix_nonzero_windows=0,
    ),
    "S3": StageRule(
        name="S3",
        min_runtime_status=10,
        require_gate_window=True,
        require_gate_pass=False,
        require_evolution_init=False,
        require_execution_activity=False,
        require_flat_start=False,
        max_start_abs_notional_usd=0.0,
        max_trading_halted_true_ratio=0.20,
        gate_fail_hard_min_windows=0,
        gate_fail_hard_max_fail_ratio=1.10,
        gate_warn_min_windows=20,
        gate_warn_max_fail_ratio=0.90,
        min_strategy_mix_nonzero_windows=0,
    ),
    "S5": StageRule(
        name="S5",
        min_runtime_status=50,
        require_gate_window=True,
        require_gate_pass=True,
        require_evolution_init=True,
        require_execution_activity=True,
        require_flat_start=True,
        max_start_abs_notional_usd=50.0,
        max_trading_halted_true_ratio=0.10,
        gate_fail_hard_min_windows=10,
        gate_fail_hard_max_fail_ratio=0.90,
        gate_warn_min_windows=8,
        gate_warn_max_fail_ratio=0.85,
        min_strategy_mix_nonzero_windows=1,
    ),
}

MIN_EXPLICIT_LIQUIDITY_FILL_RATIO_WARN = 0.70
MAX_UNKNOWN_FILL_RATIO_WARN = 0.20
MAX_FEE_SIGN_FALLBACK_FILL_RATIO_WARN = 0.30
MIN_S5_EVOLUTION_ACTIONS_FOR_UPDATE_WARN = 30
MIN_S5_LEARNABILITY_PASS_FOR_UPDATE_WARN = 10
DEFAULT_S5_MIN_EFFECTIVE_UPDATES = 1
DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_USD = -0.001
DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS = 10

RUNTIME_ACCOUNT_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?"
    r"RUNTIME_STATUS:.*?equity=(?P<equity>-?[0-9]+(?:\.[0-9]+)?), "
    r"drawdown_pct=(?P<drawdown_pct>-?[0-9]+(?:\.[0-9]+)?), "
    r"notional=(?P<notional>-?[0-9]+(?:\.[0-9]+)?)"
)
RUNTIME_ACCOUNT_REALIZED_RE = re.compile(
    r"RUNTIME_STATUS:.*?account=\{[^}]*?"
    r"realized_pnl=(?P<realized>-?[0-9]+(?:\.[0-9]+)?), "
    r"fees=(?P<fees>-?[0-9]+(?:\.[0-9]+)?), "
    r"realized_net=(?P<net>-?[0-9]+(?:\.[0-9]+)?)"
)
RUNTIME_STRATEGY_MIX_RE = re.compile(
    r"RUNTIME_STATUS:.*?strategy_mix=\{[^}]*?"
    r"latest_trend_notional=(?P<latest_trend>-?[0-9]+(?:\.[0-9]+)?), "
    r"latest_defensive_notional=(?P<latest_defensive>-?[0-9]+(?:\.[0-9]+)?), "
    r"latest_blended_notional=(?P<latest_blended>-?[0-9]+(?:\.[0-9]+)?), "
    r"avg_abs_trend_notional=(?P<avg_trend>[0-9]+(?:\.[0-9]+)?), "
    r"avg_abs_defensive_notional=(?P<avg_defensive>[0-9]+(?:\.[0-9]+)?), "
    r"avg_abs_blended_notional=(?P<avg_blended>[0-9]+(?:\.[0-9]+)?), "
    r"samples=(?P<samples>\d+)"
)
RUNTIME_EXECUTION_WINDOW_RE = re.compile(
    r"RUNTIME_STATUS:.*?execution_window=\{[^}]*?"
    r"filtered_cost_ratio=(?P<filtered_cost_ratio>-?[0-9]+(?:\.[0-9]+)?), "
    r"realized_net_delta_usd=(?P<realized_net_delta_usd>-?[0-9]+(?:\.[0-9]+)?), "
    r"realized_net_per_fill=(?P<realized_net_per_fill>-?[0-9]+(?:\.[0-9]+)?), "
    r"fee_delta_usd=(?P<fee_delta_usd>-?[0-9]+(?:\.[0-9]+)?), "
    r"fee_bps_per_fill=(?P<fee_bps_per_fill>-?[0-9]+(?:\.[0-9]+)?)"
    r"(?:, maker_fills=(?P<maker_fills>\d+), "
    r"taker_fills=(?P<taker_fills>\d+), "
    r"unknown_fills=(?P<unknown_fills>\d+), "
    r"(?:explicit_liquidity_fills=(?P<explicit_liquidity_fills>\d+), "
    r"fee_sign_fallback_fills=(?P<fee_sign_fallback_fills>\d+), "
    r"unknown_fill_ratio=(?P<unknown_fill_ratio>-?[0-9]+(?:\.[0-9]+)?), "
    r"explicit_liquidity_fill_ratio=(?P<explicit_liquidity_fill_ratio>-?[0-9]+(?:\.[0-9]+)?), "
    r"fee_sign_fallback_fill_ratio=(?P<fee_sign_fallback_fill_ratio>-?[0-9]+(?:\.[0-9]+)?), )?"
    r"maker_fee_bps=(?P<maker_fee_bps>-?[0-9]+(?:\.[0-9]+)?), "
    r"taker_fee_bps=(?P<taker_fee_bps>-?[0-9]+(?:\.[0-9]+)?), "
    r"maker_fill_ratio=(?P<maker_fill_ratio>-?[0-9]+(?:\.[0-9]+)?))?"
)
RUNTIME_EXECUTION_QUALITY_GUARD_RE = re.compile(
    r"RUNTIME_STATUS:.*?execution_quality_guard=\{[^}]*?"
    r"enabled=(?P<enabled>true|false), "
    r"active=(?P<active>true|false), "
    r"bad_streak=(?P<bad_streak>-?[0-9]+), "
    r"good_streak=(?P<good_streak>-?[0-9]+), "
    r"min_fills=(?P<min_fills>-?[0-9]+), "
    r"trigger_streak=(?P<trigger_streak>-?[0-9]+), "
    r"release_streak=(?P<release_streak>-?[0-9]+), "
    r"min_realized_net_per_fill_usd=(?P<min_realized_net_per_fill_usd>-?[0-9]+(?:\.[0-9]+)?), "
    r"max_fee_bps_per_fill=(?P<max_fee_bps_per_fill>-?[0-9]+(?:\.[0-9]+)?), "
    r"applied_penalty_bps=(?P<applied_penalty_bps>-?[0-9]+(?:\.[0-9]+)?)"
)
RUNTIME_ENTRY_EDGE_ADJUST_RE = re.compile(
    r"RUNTIME_STATUS:.*?funnel_window=\{[^}]*?"
    r"entry_regime_adjust_avg_bps=(?P<regime_adjust>-?[0-9]+(?:\.[0-9]+)?), "
    r"entry_volatility_adjust_avg_bps=(?P<volatility_adjust>-?[0-9]+(?:\.[0-9]+)?), "
    r"entry_liquidity_adjust_avg_bps=(?P<liquidity_adjust>-?[0-9]+(?:\.[0-9]+)?)"
)
RUNTIME_RECONCILE_RUNTIME_RE = re.compile(
    r"RUNTIME_STATUS:.*?reconcile_runtime=\{[^}]*?"
    r"anomaly_streak=(?P<anomaly_streak>-?[0-9]+), "
    r"healthy_streak=(?P<healthy_streak>-?[0-9]+), "
    r"anomaly_reduce_only=(?P<anomaly_reduce_only>true|false), "
    r"anomaly_reduce_only_threshold=(?P<anomaly_reduce_only_threshold>-?[0-9]+), "
    r"anomaly_halt_threshold=(?P<anomaly_halt_threshold>-?[0-9]+), "
    r"anomaly_resume_threshold=(?P<anomaly_resume_threshold>-?[0-9]+)"
)
LOG_LINE_TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ai-trade 运行日志自动验收")
    parser.add_argument(
        "--log",
        required=True,
        help="运行日志文件路径，例如 ./run_s5.log",
    )
    parser.add_argument(
        "--stage",
        default="S5",
        choices=sorted(STAGE_RULES.keys()),
        help="验收阶段（默认 S5）",
    )
    parser.add_argument(
        "--min_runtime_status",
        type=int,
        default=None,
        help="覆盖阶段默认最小 RUNTIME_STATUS 条数",
    )
    parser.add_argument(
        "--json_out",
        default="",
        help="可选：将结构化结果输出到 JSON 文件",
    )
    parser.add_argument(
        "--s5-min-effective-updates",
        type=int,
        default=DEFAULT_S5_MIN_EFFECTIVE_UPDATES,
        help=(
            "S5 硬门槛：有效学习更新最小次数 "
            "(counterfactual_update + factor_ic_action)"
        ),
    )
    parser.add_argument(
        "--s5-min-realized-net-per-fill-usd",
        type=float,
        default=DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_USD,
        help="S5 硬门槛：单位成交净收益下限（USD/Fill）",
    )
    parser.add_argument(
        "--s5-min-realized-net-per-fill-windows",
        type=int,
        default=DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS,
        help="S5 生效条件：至少 N 个窗口观测到 fills>0 才检查单位成交净收益门槛",
    )
    return parser.parse_args()


def load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, flags=re.MULTILINE))


def max_tick(text: str) -> int:
    matches = re.findall(r"RUNTIME_STATUS:\s*ticks=(\d+)", text)
    if not matches:
        return 0
    return max(int(x) for x in matches)


def extract_runtime_notional_samples(text: str) -> list[tuple[dt.datetime, float]]:
    samples: list[tuple[dt.datetime, float]] = []
    for match in RUNTIME_ACCOUNT_RE.finditer(text):
        try:
            ts = dt.datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
            notional = float(match.group("notional"))
        except ValueError:
            continue
        samples.append((ts, notional))
    return samples


def filter_log_since(text: str, cutoff_ts: dt.datetime) -> str:
    lines_out: list[str] = []
    include_line = False
    for raw_line in text.splitlines():
        ts_match = LOG_LINE_TS_RE.search(raw_line)
        if ts_match:
            try:
                line_ts = dt.datetime.strptime(
                    ts_match.group("ts"), "%Y-%m-%d %H:%M:%S"
                )
                include_line = line_ts >= cutoff_ts
            except ValueError:
                pass
        if include_line:
            lines_out.append(raw_line)
    if not lines_out:
        return ""
    return "\n".join(lines_out) + "\n"


def extract_runtime_account_series(text: str) -> Dict[str, object]:
    timestamps: list[dt.datetime] = []
    equities: list[float] = []
    drawdowns: list[float] = []
    notionals: list[float] = []
    realized_pnls: list[float] = []
    fees: list[float] = []
    realized_nets: list[float] = []

    for m in RUNTIME_ACCOUNT_RE.finditer(text):
        try:
            timestamps.append(dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"))
            equities.append(float(m.group("equity")))
            drawdowns.append(float(m.group("drawdown_pct")))
            notionals.append(float(m.group("notional")))
        except ValueError:
            continue

    for m in RUNTIME_ACCOUNT_REALIZED_RE.finditer(text):
        try:
            realized_pnls.append(float(m.group("realized")))
            fees.append(float(m.group("fees")))
            realized_nets.append(float(m.group("net")))
        except ValueError:
            continue

    if not equities:
        return {
            "samples": 0,
            "first_equity_usd": None,
            "last_equity_usd": None,
            "equity_change_usd": None,
            "equity_change_pct": None,
            "first_sample_utc": None,
            "last_sample_utc": None,
            "day_start_equity_usd": None,
            "equity_change_vs_day_start_usd": None,
            "equity_change_vs_day_start_pct": None,
            "max_equity_usd_observed": None,
            "peak_to_last_drawdown_pct": None,
            "max_drawdown_pct_observed": None,
            "max_abs_notional_usd_observed": None,
            "first_notional_usd": None,
            "first_abs_notional_usd": None,
            "start_flat": None,
            "fee_samples": 0,
            "first_realized_pnl_usd": None,
            "last_realized_pnl_usd": None,
            "realized_pnl_change_usd": None,
            "first_fee_usd": None,
            "last_fee_usd": None,
            "fee_change_usd": None,
            "first_realized_net_pnl_usd": None,
            "last_realized_net_pnl_usd": None,
            "realized_net_pnl_change_usd": None,
        }

    first_equity = equities[0]
    last_equity = equities[-1]
    equity_change = last_equity - first_equity
    equity_change_pct = None
    if abs(first_equity) > 1e-12:
        equity_change_pct = equity_change / first_equity

    first_ts = timestamps[0]
    last_ts = timestamps[-1]
    current_day = last_ts.date()
    day_start_index = 0
    for idx, ts in enumerate(timestamps):
        if ts.date() == current_day:
            day_start_index = idx
            break
    day_start_equity = equities[day_start_index]
    equity_change_vs_day_start = last_equity - day_start_equity
    equity_change_vs_day_start_pct = None
    if abs(day_start_equity) > 1e-12:
        equity_change_vs_day_start_pct = equity_change_vs_day_start / day_start_equity

    max_equity_observed = max(equities)
    peak_to_last_drawdown_pct = None
    if abs(max_equity_observed) > 1e-12:
        peak_to_last_drawdown_pct = (max_equity_observed - last_equity) / max_equity_observed

    max_drawdown_observed = max(drawdowns) if drawdowns else None
    max_abs_notional_observed = max(abs(x) for x in notionals) if notionals else None
    first_notional = notionals[0] if notionals else None
    first_abs_notional = abs(first_notional) if first_notional is not None else None

    first_realized = realized_pnls[0] if realized_pnls else None
    last_realized = realized_pnls[-1] if realized_pnls else None
    realized_change = (
        (last_realized - first_realized)
        if first_realized is not None and last_realized is not None
        else None
    )
    first_fee = fees[0] if fees else None
    last_fee = fees[-1] if fees else None
    fee_change = (
        (last_fee - first_fee)
        if first_fee is not None and last_fee is not None
        else None
    )
    first_realized_net = realized_nets[0] if realized_nets else None
    last_realized_net = realized_nets[-1] if realized_nets else None
    realized_net_change = (
        (last_realized_net - first_realized_net)
        if first_realized_net is not None and last_realized_net is not None
        else None
    )

    return {
        "samples": len(equities),
        "first_equity_usd": first_equity,
        "last_equity_usd": last_equity,
        "equity_change_usd": equity_change,
        "equity_change_pct": equity_change_pct,
        "first_sample_utc": first_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_sample_utc": last_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day_start_equity_usd": day_start_equity,
        "equity_change_vs_day_start_usd": equity_change_vs_day_start,
        "equity_change_vs_day_start_pct": equity_change_vs_day_start_pct,
        "max_equity_usd_observed": max_equity_observed,
        "peak_to_last_drawdown_pct": peak_to_last_drawdown_pct,
        "max_drawdown_pct_observed": max_drawdown_observed,
        "max_abs_notional_usd_observed": max_abs_notional_observed,
        "first_notional_usd": first_notional,
        "first_abs_notional_usd": first_abs_notional,
        "start_flat": bool(first_abs_notional is not None and first_abs_notional <= 1e-6),
        "fee_samples": len(fees),
        "first_realized_pnl_usd": first_realized,
        "last_realized_pnl_usd": last_realized,
        "realized_pnl_change_usd": realized_change,
        "first_fee_usd": first_fee,
        "last_fee_usd": last_fee,
        "fee_change_usd": fee_change,
        "first_realized_net_pnl_usd": first_realized_net,
        "last_realized_net_pnl_usd": last_realized_net,
        "realized_net_pnl_change_usd": realized_net_change,
    }


def extract_strategy_mix_series(text: str) -> Dict[str, float]:
    latest_trend_values: list[float] = []
    latest_defensive_values: list[float] = []
    avg_trend_values: list[float] = []
    avg_defensive_values: list[float] = []
    avg_blended_values: list[float] = []
    sample_values: list[int] = []

    for m in RUNTIME_STRATEGY_MIX_RE.finditer(text):
        try:
            latest_trend_values.append(float(m.group("latest_trend")))
            latest_defensive_values.append(float(m.group("latest_defensive")))
            avg_trend_values.append(float(m.group("avg_trend")))
            avg_defensive_values.append(float(m.group("avg_defensive")))
            avg_blended_values.append(float(m.group("avg_blended")))
            sample_values.append(int(m.group("samples")))
        except ValueError:
            continue

    runtime_count = len(sample_values)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "nonzero_window_count": 0.0,
            "defensive_active_count": 0.0,
            "avg_abs_trend_notional": 0.0,
            "avg_abs_defensive_notional": 0.0,
            "avg_abs_blended_notional": 0.0,
            "avg_defensive_share": 0.0,
        }

    nonzero_window_count = sum(1 for x in sample_values if x > 0)
    defensive_active_count = sum(
        1
        for avg_defensive, window_samples in zip(avg_defensive_values, sample_values)
        if window_samples > 0 and avg_defensive > 1e-9
    )
    avg_abs_trend_notional = sum(avg_trend_values) / runtime_count
    avg_abs_defensive_notional = sum(avg_defensive_values) / runtime_count
    avg_abs_blended_notional = sum(avg_blended_values) / runtime_count
    defensive_share_den = avg_abs_trend_notional + avg_abs_defensive_notional
    avg_defensive_share = (
        avg_abs_defensive_notional / defensive_share_den
        if defensive_share_den > 1e-12
        else 0.0
    )

    return {
        "runtime_count": float(runtime_count),
        "nonzero_window_count": float(nonzero_window_count),
        "defensive_active_count": float(defensive_active_count),
        "avg_abs_trend_notional": avg_abs_trend_notional,
        "avg_abs_defensive_notional": avg_abs_defensive_notional,
        "avg_abs_blended_notional": avg_abs_blended_notional,
        "avg_defensive_share": avg_defensive_share,
    }


def extract_execution_window_series(text: str) -> Dict[str, float]:
    filtered_cost_ratios: list[float] = []
    realized_net_per_fills: list[float] = []
    fee_bps_per_fills: list[float] = []
    maker_fills: list[float] = []
    taker_fills: list[float] = []
    unknown_fills: list[float] = []
    explicit_liquidity_fills: list[float] = []
    fee_sign_fallback_fills: list[float] = []
    unknown_fill_ratios: list[float] = []
    explicit_liquidity_fill_ratios: list[float] = []
    fee_sign_fallback_fill_ratios: list[float] = []
    maker_fee_bps_values: list[float] = []
    taker_fee_bps_values: list[float] = []
    maker_fill_ratios: list[float] = []
    liquidity_source_runtime_count = 0

    for m in RUNTIME_EXECUTION_WINDOW_RE.finditer(text):
        try:
            filtered_cost_ratios.append(float(m.group("filtered_cost_ratio")))
            realized_net_per_fills.append(float(m.group("realized_net_per_fill")))
            fee_bps_per_fills.append(float(m.group("fee_bps_per_fill")))
            maker_fill_value = float(m.group("maker_fills") or 0.0)
            taker_fill_value = float(m.group("taker_fills") or 0.0)
            unknown_fill_value = float(m.group("unknown_fills") or 0.0)
            maker_fills.append(maker_fill_value)
            taker_fills.append(taker_fill_value)
            unknown_fills.append(unknown_fill_value)
            explicit_fill_value = float(m.group("explicit_liquidity_fills") or 0.0)
            fallback_fill_value = float(m.group("fee_sign_fallback_fills") or 0.0)
            explicit_liquidity_fills.append(explicit_fill_value)
            fee_sign_fallback_fills.append(fallback_fill_value)
            if m.group("explicit_liquidity_fills") is not None:
                liquidity_source_runtime_count += 1
            total_liquidity_samples = maker_fill_value + taker_fill_value + unknown_fill_value
            unknown_ratio_value = (
                float(m.group("unknown_fill_ratio"))
                if m.group("unknown_fill_ratio") is not None
                else (unknown_fill_value / total_liquidity_samples if total_liquidity_samples > 0 else 0.0)
            )
            explicit_ratio_value = (
                float(m.group("explicit_liquidity_fill_ratio"))
                if m.group("explicit_liquidity_fill_ratio") is not None
                else 0.0
            )
            fallback_ratio_value = (
                float(m.group("fee_sign_fallback_fill_ratio"))
                if m.group("fee_sign_fallback_fill_ratio") is not None
                else 0.0
            )
            unknown_fill_ratios.append(unknown_ratio_value)
            explicit_liquidity_fill_ratios.append(explicit_ratio_value)
            fee_sign_fallback_fill_ratios.append(fallback_ratio_value)
            maker_fee_bps_values.append(float(m.group("maker_fee_bps") or 0.0))
            taker_fee_bps_values.append(float(m.group("taker_fee_bps") or 0.0))
            maker_fill_ratios.append(float(m.group("maker_fill_ratio") or 0.0))
        except ValueError:
            continue

    runtime_count = len(filtered_cost_ratios)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "filtered_cost_ratio_avg": 0.0,
            "filtered_cost_ratio_latest": 0.0,
            "realized_net_per_fill_avg": 0.0,
            "fee_bps_per_fill_avg": 0.0,
            "maker_fills_avg": 0.0,
            "taker_fills_avg": 0.0,
            "unknown_fills_avg": 0.0,
            "explicit_liquidity_fills_avg": 0.0,
            "fee_sign_fallback_fills_avg": 0.0,
            "unknown_fill_ratio_avg": 0.0,
            "explicit_liquidity_fill_ratio_avg": 0.0,
            "fee_sign_fallback_fill_ratio_avg": 0.0,
            "maker_fee_bps_avg": 0.0,
            "taker_fee_bps_avg": 0.0,
            "maker_fill_ratio_avg": 0.0,
            "liquidity_source_runtime_count": 0.0,
        }

    return {
        "runtime_count": float(runtime_count),
        "filtered_cost_ratio_avg": sum(filtered_cost_ratios) / runtime_count,
        "filtered_cost_ratio_latest": filtered_cost_ratios[-1],
        "realized_net_per_fill_avg": sum(realized_net_per_fills) / runtime_count,
        "fee_bps_per_fill_avg": sum(fee_bps_per_fills) / runtime_count,
        "maker_fills_avg": sum(maker_fills) / runtime_count,
        "taker_fills_avg": sum(taker_fills) / runtime_count,
        "unknown_fills_avg": sum(unknown_fills) / runtime_count,
        "explicit_liquidity_fills_avg": sum(explicit_liquidity_fills) / runtime_count,
        "fee_sign_fallback_fills_avg": sum(fee_sign_fallback_fills) / runtime_count,
        "unknown_fill_ratio_avg": sum(unknown_fill_ratios) / runtime_count,
        "explicit_liquidity_fill_ratio_avg": sum(explicit_liquidity_fill_ratios) / runtime_count,
        "fee_sign_fallback_fill_ratio_avg": sum(fee_sign_fallback_fill_ratios) / runtime_count,
        "maker_fee_bps_avg": sum(maker_fee_bps_values) / runtime_count,
        "taker_fee_bps_avg": sum(taker_fee_bps_values) / runtime_count,
        "maker_fill_ratio_avg": sum(maker_fill_ratios) / runtime_count,
        "liquidity_source_runtime_count": float(liquidity_source_runtime_count),
    }


def extract_execution_quality_guard_series(text: str) -> Dict[str, float]:
    active_flags: list[float] = []
    enabled_flags: list[float] = []
    applied_penalty_bps: list[float] = []
    bad_streaks: list[int] = []
    good_streaks: list[int] = []

    for m in RUNTIME_EXECUTION_QUALITY_GUARD_RE.finditer(text):
        active_flags.append(1.0 if m.group("active") == "true" else 0.0)
        enabled_flags.append(1.0 if m.group("enabled") == "true" else 0.0)
        try:
            applied_penalty_bps.append(float(m.group("applied_penalty_bps")))
            bad_streaks.append(int(m.group("bad_streak")))
            good_streaks.append(int(m.group("good_streak")))
        except ValueError:
            continue

    runtime_count = len(active_flags)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "enabled_count": 0.0,
            "active_count": 0.0,
            "active_ratio": 0.0,
            "applied_penalty_bps_avg": 0.0,
            "bad_streak_max": 0.0,
            "good_streak_max": 0.0,
        }

    return {
        "runtime_count": float(runtime_count),
        "enabled_count": float(sum(enabled_flags)),
        "active_count": float(sum(active_flags)),
        "active_ratio": sum(active_flags) / runtime_count,
        "applied_penalty_bps_avg": sum(applied_penalty_bps) / runtime_count,
        "bad_streak_max": float(max(bad_streaks) if bad_streaks else 0),
        "good_streak_max": float(max(good_streaks) if good_streaks else 0),
    }


def extract_entry_edge_adjust_series(text: str) -> Dict[str, float]:
    regime_adjust_values: list[float] = []
    volatility_adjust_values: list[float] = []
    liquidity_adjust_values: list[float] = []

    for m in RUNTIME_ENTRY_EDGE_ADJUST_RE.finditer(text):
        try:
            regime_adjust_values.append(float(m.group("regime_adjust")))
            volatility_adjust_values.append(float(m.group("volatility_adjust")))
            liquidity_adjust_values.append(float(m.group("liquidity_adjust")))
        except ValueError:
            continue

    runtime_count = len(regime_adjust_values)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "regime_adjust_bps_avg": 0.0,
            "volatility_adjust_bps_avg": 0.0,
            "liquidity_adjust_bps_avg": 0.0,
        }
    return {
        "runtime_count": float(runtime_count),
        "regime_adjust_bps_avg": sum(regime_adjust_values) / runtime_count,
        "volatility_adjust_bps_avg": sum(volatility_adjust_values) / runtime_count,
        "liquidity_adjust_bps_avg": sum(liquidity_adjust_values) / runtime_count,
    }


def extract_reconcile_runtime_series(text: str) -> Dict[str, float]:
    anomaly_streaks: list[int] = []
    reduce_only_flags: list[float] = []

    for m in RUNTIME_RECONCILE_RUNTIME_RE.finditer(text):
        reduce_only_flags.append(
            1.0 if m.group("anomaly_reduce_only") == "true" else 0.0
        )
        try:
            anomaly_streaks.append(int(m.group("anomaly_streak")))
        except ValueError:
            continue

    runtime_count = len(reduce_only_flags)
    if runtime_count <= 0:
        return {
            "runtime_count": 0.0,
            "anomaly_streak_nonzero_count": 0.0,
            "anomaly_streak_max": 0.0,
            "anomaly_reduce_only_true_count": 0.0,
            "anomaly_reduce_only_true_ratio": 0.0,
        }

    anomaly_nonzero_count = sum(1 for x in anomaly_streaks if x > 0)
    reduce_only_true_count = sum(reduce_only_flags)
    return {
        "runtime_count": float(runtime_count),
        "anomaly_streak_nonzero_count": float(anomaly_nonzero_count),
        "anomaly_streak_max": float(max(anomaly_streaks) if anomaly_streaks else 0),
        "anomaly_reduce_only_true_count": float(reduce_only_true_count),
        "anomaly_reduce_only_true_ratio": reduce_only_true_count / runtime_count,
    }


def assess(
    text: str,
    stage: StageRule,
    min_runtime_status: int,
    s5_min_effective_updates: int = DEFAULT_S5_MIN_EFFECTIVE_UPDATES,
    s5_min_realized_net_per_fill_usd: float = DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_USD,
    s5_min_realized_net_per_fill_windows: int = DEFAULT_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS,
) -> Dict[str, object]:
    original_text = text
    flat_start_rebased = False
    flat_start_rebase_cutoff_utc = None
    if stage.require_flat_start:
        notional_samples = extract_runtime_notional_samples(text)
        if notional_samples:
            first_abs_notional = abs(notional_samples[0][1])
            if first_abs_notional > stage.max_start_abs_notional_usd:
                first_flat_sample = next(
                    (
                        sample
                        for sample in notional_samples
                        if abs(sample[1]) <= stage.max_start_abs_notional_usd
                    ),
                    None,
                )
                if first_flat_sample is not None:
                    rebased_text = filter_log_since(text, first_flat_sample[0])
                    if rebased_text.strip():
                        text = rebased_text
                        flat_start_rebased = True
                        flat_start_rebase_cutoff_utc = first_flat_sample[0].strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        )

    account_pnl = extract_runtime_account_series(text)
    strategy_mix = extract_strategy_mix_series(text)
    execution_window = extract_execution_window_series(text)
    execution_quality_guard = extract_execution_quality_guard_series(text)
    entry_edge_adjust = extract_entry_edge_adjust_series(text)
    reconcile_runtime = extract_reconcile_runtime_series(text)
    global_self_evolution_init_count = count(r"SELF_EVOLUTION_INIT", original_text)
    global_self_evolution_action_count = count(r"SELF_EVOLUTION_ACTION", original_text)
    metrics = {
        "runtime_status_count": count(r"RUNTIME_STATUS:", text),
        "max_runtime_tick": max_tick(text),
        "critical_count": count(r"\bCRITICAL\b", text),
        "trading_halted_event_count": count(r"\bTRADING_HALTED\b", text),
        "trading_halted_true_count": count(r"RUNTIME_STATUS:.*trading_halted=true", text),
        "gate_reduce_only_true_count": count(r"RUNTIME_STATUS:.*gate_runtime=.*reduce_only=true", text),
        "gate_halted_true_count": count(r"RUNTIME_STATUS:.*gate_runtime=.*gate_halted=true", text),
        "ws_unhealthy_count": count(
            r"RUNTIME_STATUS:.*(?:public_ws_healthy=false|private_ws_healthy=false)", text
        ),
        "ws_degraded_count": count(r"\bDEGRADED\b", text),
        "gate_check_passed_count": count(r"GATE_CHECK_PASSED", text),
        "gate_check_failed_count": count(r"GATE_CHECK_FAILED", text),
        "gate_alert_count": count(r"GATE_ALERT", text),
        "reconcile_mismatch_count": count(r"OMS_RECONCILE_MISMATCH", text),
        "reconcile_autoresync_count": count(r"OMS_RECONCILE_AUTORESYNC", text),
        "reconcile_deferred_count": count(r"OMS_RECONCILE_DEFERRED", text),
        "reconcile_anomaly_event_count": count(r"OMS_RECONCILE_ANOMALY_STREAK", text),
        "reconcile_anomaly_protection_enter_count": count(
            r"OMS_RECONCILE_ANOMALY_PROTECTION_ENTER", text
        ),
        "reconcile_anomaly_protection_exit_count": count(
            r"OMS_RECONCILE_ANOMALY_PROTECTION_EXIT", text
        ),
        "reconcile_anomaly_halt_enter_count": count(
            r"OMS_RECONCILE_ANOMALY_HALT_ENTER", text
        ),
        "self_evolution_init_count": count(r"SELF_EVOLUTION_INIT", text),
        "self_evolution_action_count": count(r"SELF_EVOLUTION_ACTION", text),
        "self_evolution_init_total_count": global_self_evolution_init_count,
        "self_evolution_action_total_count": global_self_evolution_action_count,
        "self_evolution_virtual_action_count": count(
            r"SELF_EVOLUTION_ACTION:.*pnl_source=virtual", text
        ),
        "self_evolution_counterfactual_action_count": count(
            r"SELF_EVOLUTION_ACTION:.*counterfactual_search=true", text
        ),
        "self_evolution_counterfactual_update_count": count(
            r"SELF_EVOLUTION_ACTION:.*reason=EVOLUTION_COUNTERFACTUAL_(?:INCREASE|DECREASE)_TREND",
            text,
        ),
        "self_evolution_factor_ic_action_count": count(
            r"SELF_EVOLUTION_ACTION:.*reason=EVOLUTION_FACTOR_IC_(?:INCREASE|DECREASE)_TREND",
            text,
        ),
        "self_evolution_counterfactual_fallback_used_count": count(
            r"SELF_EVOLUTION_ACTION:.*counterfactual_fallback=\{enabled=true, used=true\}",
            text,
        ),
        "self_evolution_learnability_skip_count": count(
            r"SELF_EVOLUTION_ACTION:.*reason=EVOLUTION_LEARNABILITY_(?:INSUFFICIENT_SAMPLES|TSTAT_TOO_LOW)",
            text,
        ),
        "self_evolution_learnability_pass_count": count(
            r"SELF_EVOLUTION_ACTION:.*learnability=\{enabled=true, passed=true",
            text,
        ),
        "integrator_policy_applied_count": count(r"INTEGRATOR_POLICY_APPLIED:", text),
        "integrator_policy_canary_count": count(
            r"INTEGRATOR_POLICY_APPLIED:.*mode=canary", text
        ),
        "integrator_policy_active_count": count(
            r"INTEGRATOR_POLICY_APPLIED:.*mode=active", text
        ),
        "integrator_mode_off_count": count(r"RUNTIME_STATUS:.*integrator_mode=off", text),
        "integrator_mode_shadow_count": count(
            r"RUNTIME_STATUS:.*integrator_mode=shadow", text
        ),
        "integrator_mode_canary_count": count(
            r"RUNTIME_STATUS:.*integrator_mode=canary", text
        ),
        "integrator_mode_active_count": count(
            r"RUNTIME_STATUS:.*integrator_mode=active", text
        ),
        "integrator_shadow_scored_runtime_count": count(
            r"RUNTIME_STATUS:.*shadow_window=\{[^}]*scored=(?:[1-9][0-9]*)", text
        ),
        "order_filtered_cost_count": count(r"ORDER_FILTERED_COST:", text),
        "entry_gate_enabled_count": count(r"RUNTIME_STATUS:.*entry_gate=\{enabled=true", text),
        "funnel_enqueued_runtime_count": count(
            r"RUNTIME_STATUS:.*funnel_window=\{[^}]*enqueued=(?:[1-9][0-9]*)",
            text,
        ),
        "funnel_fills_runtime_count": count(
            r"RUNTIME_STATUS:.*funnel_window=\{[^}]*fills=(?:[1-9][0-9]*)",
            text,
        ),
        "bybit_submit_limit_count": count(r"BYBIT_SUBMIT:.*order_type=Limit", text),
        "bybit_submit_market_count": count(r"BYBIT_SUBMIT:.*order_type=Market", text),
        "runtime_account_samples": account_pnl["samples"],
        "start_flat": account_pnl["start_flat"],
        "start_abs_notional_usd": account_pnl["first_abs_notional_usd"],
        "strategy_mix_runtime_count": int(strategy_mix["runtime_count"]),
        "strategy_mix_nonzero_window_count": int(strategy_mix["nonzero_window_count"]),
        "strategy_mix_defensive_active_count": int(
            strategy_mix["defensive_active_count"]
        ),
        "strategy_mix_avg_abs_trend_notional": strategy_mix["avg_abs_trend_notional"],
        "strategy_mix_avg_abs_defensive_notional": strategy_mix[
            "avg_abs_defensive_notional"
        ],
        "strategy_mix_avg_abs_blended_notional": strategy_mix[
            "avg_abs_blended_notional"
        ],
        "strategy_mix_avg_defensive_share": strategy_mix["avg_defensive_share"],
        "execution_window_runtime_count": int(execution_window["runtime_count"]),
        "filtered_cost_ratio": execution_window["filtered_cost_ratio_latest"],
        "filtered_cost_ratio_avg": execution_window["filtered_cost_ratio_avg"],
        "realized_net_per_fill": execution_window["realized_net_per_fill_avg"],
        "fee_bps_per_fill": execution_window["fee_bps_per_fill_avg"],
        "execution_window_maker_fills_avg": execution_window["maker_fills_avg"],
        "execution_window_taker_fills_avg": execution_window["taker_fills_avg"],
        "execution_window_unknown_fills_avg": execution_window["unknown_fills_avg"],
        "execution_window_explicit_liquidity_fills_avg": execution_window[
            "explicit_liquidity_fills_avg"
        ],
        "execution_window_fee_sign_fallback_fills_avg": execution_window[
            "fee_sign_fallback_fills_avg"
        ],
        "execution_window_unknown_fill_ratio_avg": execution_window[
            "unknown_fill_ratio_avg"
        ],
        "execution_window_explicit_liquidity_fill_ratio_avg": execution_window[
            "explicit_liquidity_fill_ratio_avg"
        ],
        "execution_window_fee_sign_fallback_fill_ratio_avg": execution_window[
            "fee_sign_fallback_fill_ratio_avg"
        ],
        "execution_window_maker_fee_bps_avg": execution_window["maker_fee_bps_avg"],
        "execution_window_taker_fee_bps_avg": execution_window["taker_fee_bps_avg"],
        "execution_window_maker_fill_ratio_avg": execution_window[
            "maker_fill_ratio_avg"
        ],
        "execution_window_liquidity_source_runtime_count": int(
            execution_window["liquidity_source_runtime_count"]
        ),
        "execution_quality_guard_runtime_count": int(
            execution_quality_guard["runtime_count"]
        ),
        "execution_quality_guard_enabled_count": int(
            execution_quality_guard["enabled_count"]
        ),
        "execution_quality_guard_active_count": int(
            execution_quality_guard["active_count"]
        ),
        "execution_quality_guard_active_ratio": execution_quality_guard[
            "active_ratio"
        ],
        "execution_quality_guard_penalty_bps_avg": execution_quality_guard[
            "applied_penalty_bps_avg"
        ],
        "execution_quality_guard_bad_streak_max": int(
            execution_quality_guard["bad_streak_max"]
        ),
        "execution_quality_guard_good_streak_max": int(
            execution_quality_guard["good_streak_max"]
        ),
        "entry_edge_adjust_runtime_count": int(entry_edge_adjust["runtime_count"]),
        "entry_regime_adjust_bps_avg": entry_edge_adjust["regime_adjust_bps_avg"],
        "entry_volatility_adjust_bps_avg": entry_edge_adjust[
            "volatility_adjust_bps_avg"
        ],
        "entry_liquidity_adjust_bps_avg": entry_edge_adjust[
            "liquidity_adjust_bps_avg"
        ],
        "execution_quality_guard_enter_count": count(
            r"EXECUTION_QUALITY_GUARD_ENTER", text
        ),
        "execution_quality_guard_exit_count": count(
            r"EXECUTION_QUALITY_GUARD_EXIT", text
        ),
        "reconcile_runtime_count": int(reconcile_runtime["runtime_count"]),
        "reconcile_anomaly_streak_nonzero_count": int(
            reconcile_runtime["anomaly_streak_nonzero_count"]
        ),
        "reconcile_anomaly_streak_max": int(reconcile_runtime["anomaly_streak_max"]),
        "reconcile_anomaly_reduce_only_true_count": int(
            reconcile_runtime["anomaly_reduce_only_true_count"]
        ),
        "reconcile_anomaly_reduce_only_true_ratio": reconcile_runtime[
            "anomaly_reduce_only_true_ratio"
        ],
        "flat_start_rebase_applied_count": 1 if flat_start_rebased else 0,
    }
    metrics["self_evolution_effective_update_count"] = (
        metrics["self_evolution_counterfactual_update_count"]
        + metrics["self_evolution_factor_ic_action_count"]
    )
    if metrics["runtime_status_count"] > 0:
        metrics["trading_halted_true_ratio"] = (
            metrics["trading_halted_true_count"] / metrics["runtime_status_count"]
        )
        metrics["integrator_policy_applied_ratio"] = (
            metrics["integrator_policy_applied_count"] / metrics["runtime_status_count"]
        )
    else:
        metrics["trading_halted_true_ratio"] = 0.0
        metrics["integrator_policy_applied_ratio"] = 0.0

    execution_activity_count = (
        metrics["bybit_submit_limit_count"]
        + metrics["bybit_submit_market_count"]
        + metrics["funnel_enqueued_runtime_count"]
        + metrics["funnel_fills_runtime_count"]
    )
    metrics["execution_activity_count"] = execution_activity_count

    gate_window_count = metrics["gate_check_passed_count"] + metrics["gate_check_failed_count"]
    if gate_window_count > 0:
        metrics["gate_check_fail_ratio"] = (
            metrics["gate_check_failed_count"] / gate_window_count
        )
    else:
        metrics["gate_check_fail_ratio"] = 0.0

    equity_change_usd = account_pnl.get("equity_change_usd")
    realized_net_change_usd = account_pnl.get("realized_net_pnl_change_usd")
    if isinstance(equity_change_usd, (int, float)) and isinstance(
        realized_net_change_usd, (int, float)
    ):
        metrics["equity_vs_realized_net_gap_usd"] = (
            equity_change_usd - realized_net_change_usd
        )
    else:
        metrics["equity_vs_realized_net_gap_usd"] = None

    fail_reasons: list[str] = []
    warn_reasons: list[str] = []

    if metrics["critical_count"] > 0:
        fail_reasons.append(f"出现 CRITICAL: {metrics['critical_count']}")
    if metrics["ws_unhealthy_count"] > 0:
        fail_reasons.append(f"运行态 WS 健康检查失败次数: {metrics['ws_unhealthy_count']}")
    if metrics["runtime_status_count"] < min_runtime_status:
        fail_reasons.append(
            f"RUNTIME_STATUS 条数不足: {metrics['runtime_status_count']} < {min_runtime_status}"
        )

    # DEPLOY 门禁仅看“健康硬指标”，避免冷启动阶段的策略类指标误触发回滚。
    if stage.name != "DEPLOY":
        if metrics["trading_halted_true_ratio"] > stage.max_trading_halted_true_ratio:
            fail_reasons.append(
                "trading_halted=true 占比超阈值: "
                f"{metrics['trading_halted_true_ratio']:.4f} > "
                f"{stage.max_trading_halted_true_ratio:.4f}"
            )

        if stage.require_gate_window and gate_window_count <= 0:
            fail_reasons.append("未检测到 Gate 窗口判定（GATE_CHECK_PASSED/FAILED）")
        if (
            stage.require_gate_pass
            and gate_window_count > 0
            and metrics["gate_check_passed_count"] <= 0
        ):
            fail_reasons.append("未检测到 GATE_CHECK_PASSED（S5 要求至少一个通过窗口）")

        if (
            stage.require_evolution_init
            and metrics["self_evolution_init_total_count"] <= 0
            and metrics["self_evolution_action_total_count"] <= 0
        ):
            fail_reasons.append("未检测到 SELF_EVOLUTION_INIT/SELF_EVOLUTION_ACTION")

        if stage.require_execution_activity and execution_activity_count <= 0:
            fail_reasons.append("未检测到执行活动（BYBIT_SUBMIT/enqueued/fills 全为 0）")
        if (
            stage.min_strategy_mix_nonzero_windows > 0
            and metrics["strategy_mix_nonzero_window_count"]
            < stage.min_strategy_mix_nonzero_windows
        ):
            fail_reasons.append(
                "未检测到有效策略信号窗口（strategy_mix.samples>0），"
                f"S5 至少要求 {stage.min_strategy_mix_nonzero_windows} 个窗口"
            )

        start_abs_notional = account_pnl.get("first_abs_notional_usd")
        if (
            stage.require_flat_start
            and isinstance(start_abs_notional, (int, float))
            and start_abs_notional > stage.max_start_abs_notional_usd
        ):
            fail_reasons.append(
                "运行窗口起点非平仓状态，S5 验收要求平仓起跑: "
                f"abs_notional={start_abs_notional:.6f} > "
                f"threshold={stage.max_start_abs_notional_usd:.6f}"
            )

        if (
            gate_window_count >= stage.gate_fail_hard_min_windows
            and metrics["gate_check_fail_ratio"] > stage.gate_fail_hard_max_fail_ratio
        ):
            fail_reasons.append(
                "Gate 失败率过高（强闭环阻断）: "
                f"fail_ratio={metrics['gate_check_fail_ratio']:.4f}, "
                f"threshold={stage.gate_fail_hard_max_fail_ratio:.4f}"
            )
        if (
            stage.name == "S5"
            and metrics["self_evolution_action_count"]
            >= MIN_S5_EVOLUTION_ACTIONS_FOR_UPDATE_WARN
            and metrics["self_evolution_learnability_pass_count"]
            >= MIN_S5_LEARNABILITY_PASS_FOR_UPDATE_WARN
            and metrics["self_evolution_effective_update_count"]
            < max(0, s5_min_effective_updates)
        ):
            fail_reasons.append(
                "SELF_EVOLUTION 有评估无有效更新（S5 强门禁）: "
                f"action_count={metrics['self_evolution_action_count']}, "
                f"learnability_pass_count={metrics['self_evolution_learnability_pass_count']}, "
                f"effective_update_count={metrics['self_evolution_effective_update_count']}, "
                f"required>={max(0, s5_min_effective_updates)}"
            )
        if (
            stage.name == "S5"
            and metrics["funnel_fills_runtime_count"]
            >= max(0, s5_min_realized_net_per_fill_windows)
            and isinstance(metrics["realized_net_per_fill"], (int, float))
            and metrics["realized_net_per_fill"] < s5_min_realized_net_per_fill_usd
        ):
            fail_reasons.append(
                "执行净收益质量未达标（S5 强门禁）: "
                f"realized_net_per_fill={metrics['realized_net_per_fill']:.6f}, "
                f"threshold={s5_min_realized_net_per_fill_usd:.6f}, "
                f"fill_windows={metrics['funnel_fills_runtime_count']}, "
                f"required_windows>={max(0, s5_min_realized_net_per_fill_windows)}"
            )

    # 软告警：DEPLOY 仅保留硬失败项，避免上线门禁被策略类黄灯误阻断。
    if stage.name != "DEPLOY":
        if (
            metrics["reconcile_mismatch_count"] > 0
            and metrics["reconcile_autoresync_count"] <= 0
        ):
            warn_reasons.append("出现对账不一致但未观察到 AUTORESYNC")
        if (
            gate_window_count >= stage.gate_warn_min_windows
            and metrics["gate_check_fail_ratio"] > stage.gate_warn_max_fail_ratio
        ):
            warn_reasons.append(
                "Gate 失败率偏高，建议复核策略活跃度/门槛参数: "
                f"fail_ratio={metrics['gate_check_fail_ratio']:.4f}, "
                f"threshold={stage.gate_warn_max_fail_ratio:.4f}"
            )
        if (
            metrics["trading_halted_true_count"] > 0
            and metrics["trading_halted_true_ratio"]
            <= stage.max_trading_halted_true_ratio
        ):
            warn_reasons.append(
                "出现短暂 trading_halted=true，但占比在阈值内，建议复核 Gate/对账参数"
            )
        if (
            stage.name == "S5"
            and metrics["self_evolution_action_count"] <= 0
            and metrics["self_evolution_init_count"] > 0
        ):
            warn_reasons.append(
                "未观测到 SELF_EVOLUTION_ACTION，建议检查 update_interval 与样本门槛"
            )
        evolution_effective_update_count = metrics[
            "self_evolution_effective_update_count"
        ]
        if (
            stage.name == "S5"
            and metrics["self_evolution_action_count"]
            >= MIN_S5_EVOLUTION_ACTIONS_FOR_UPDATE_WARN
            and metrics["self_evolution_learnability_pass_count"]
            >= MIN_S5_LEARNABILITY_PASS_FOR_UPDATE_WARN
            and evolution_effective_update_count <= 0
        ):
            warn_reasons.append(
                "SELF_EVOLUTION 长时间仅评估未更新，建议放宽反事实/因子IC更新门槛: "
                f"action_count={metrics['self_evolution_action_count']}, "
                f"learnability_pass_count={metrics['self_evolution_learnability_pass_count']}, "
                f"effective_update_count={evolution_effective_update_count}"
            )
        if (
            metrics["execution_window_runtime_count"] > 0
            and metrics["order_filtered_cost_count"] >= 20
            and metrics["filtered_cost_ratio_avg"] >= 0.90
        ):
            warn_reasons.append(
                "ORDER_FILTERED_COST 占比较高，建议复核 entry gate 参数: "
                f"filtered_cost_ratio_avg={metrics['filtered_cost_ratio_avg']:.4f}"
            )
        if metrics["execution_quality_guard_active_count"] > 0:
            warn_reasons.append(
                "执行质量守卫触发，入场门槛已抬升，建议复核手续费与执行路径: "
                f"active_count={metrics['execution_quality_guard_active_count']}, "
                f"penalty_bps_avg={metrics['execution_quality_guard_penalty_bps_avg']:.4f}"
            )
        if (
            metrics["execution_window_runtime_count"] > 0
            and metrics["execution_window_maker_fill_ratio_avg"] < 0.20
            and metrics["fee_bps_per_fill"] >= 6.0
        ):
            warn_reasons.append(
                "Maker 成交占比偏低且成交成本偏高，建议优化挂单成交质量: "
                f"maker_fill_ratio_avg={metrics['execution_window_maker_fill_ratio_avg']:.4f}, "
                f"fee_bps_per_fill={metrics['fee_bps_per_fill']:.4f}"
            )
        liquidity_classified_fills_avg = (
            metrics["execution_window_maker_fills_avg"]
            + metrics["execution_window_taker_fills_avg"]
            + metrics["execution_window_unknown_fills_avg"]
        )
        if (
            metrics["execution_window_runtime_count"] > 0
            and liquidity_classified_fills_avg >= 1.0
        ):
            if metrics["execution_window_liquidity_source_runtime_count"] <= 0:
                warn_reasons.append(
                    "成交存在但未观测到流动性来源细分字段，建议升级到最新运行时二进制（explicit/fallback 口径）"
                )
            else:
                if (
                    metrics["execution_window_explicit_liquidity_fill_ratio_avg"]
                    < MIN_EXPLICIT_LIQUIDITY_FILL_RATIO_WARN
                ):
                    warn_reasons.append(
                        "显式流动性标签覆盖率偏低，建议排查交易所回报字段/解析链路: "
                        "execution_window_explicit_liquidity_fill_ratio_avg="
                        f"{metrics['execution_window_explicit_liquidity_fill_ratio_avg']:.4f}, "
                        f"threshold={MIN_EXPLICIT_LIQUIDITY_FILL_RATIO_WARN:.4f}"
                    )
                if (
                    metrics["execution_window_unknown_fill_ratio_avg"]
                    > MAX_UNKNOWN_FILL_RATIO_WARN
                ):
                    warn_reasons.append(
                        "Unknown 流动性成交占比偏高，建议检查成交回报完整性: "
                        "execution_window_unknown_fill_ratio_avg="
                        f"{metrics['execution_window_unknown_fill_ratio_avg']:.4f}, "
                        f"threshold={MAX_UNKNOWN_FILL_RATIO_WARN:.4f}"
                    )
                if (
                    metrics["execution_window_fee_sign_fallback_fill_ratio_avg"]
                    > MAX_FEE_SIGN_FALLBACK_FILL_RATIO_WARN
                ):
                    warn_reasons.append(
                        "fee 符号兜底占比偏高，建议优先修复显式流动性标签覆盖: "
                        "execution_window_fee_sign_fallback_fill_ratio_avg="
                        f"{metrics['execution_window_fee_sign_fallback_fill_ratio_avg']:.4f}, "
                        f"threshold={MAX_FEE_SIGN_FALLBACK_FILL_RATIO_WARN:.4f}"
                    )
        if metrics["reconcile_anomaly_reduce_only_true_count"] > 0:
            warn_reasons.append(
                "对账异常保护触发 reduce-only，建议核查回报链路与对账口径: "
                f"reduce_only_true_count={metrics['reconcile_anomaly_reduce_only_true_count']}"
            )

        integrator_takeover_mode_count = (
            metrics["integrator_mode_canary_count"] + metrics["integrator_mode_active_count"]
        )
        if (
            integrator_takeover_mode_count > 0
            and metrics["integrator_policy_applied_count"] <= 0
        ):
            warn_reasons.append("Integrator 处于 canary/active 但未观测到策略接管事件")
        if (
            integrator_takeover_mode_count > 0
            and metrics["integrator_shadow_scored_runtime_count"] <= 0
        ):
            warn_reasons.append("Integrator 处于 canary/active 但未观测到 shadow scored>0")

        if execution_activity_count <= 0:
            gap_usd = metrics.get("equity_vs_realized_net_gap_usd")
            if isinstance(gap_usd, (int, float)) and abs(gap_usd) >= 50.0:
                warn_reasons.append(
                    "权益变化与已实现净盈亏偏差较大且无执行活动，建议检查资金同步/统计口径: "
                    f"gap_usd={gap_usd:.6f}"
                )
    if fail_reasons:
        verdict = "FAIL"
    elif warn_reasons:
        verdict = "PASS_WITH_ACTIONS"
    else:
        verdict = "PASS"

    return {
        "stage": stage.name,
        "verdict": verdict,
        "metrics": metrics,
        "account_pnl": account_pnl,
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "flat_start_rebased": flat_start_rebased,
        "flat_start_rebase_cutoff_utc": flat_start_rebase_cutoff_utc,
    }


def print_report(report: Dict[str, object]) -> None:
    print(f"STAGE: {report['stage']}")
    print(f"VERDICT: {report['verdict']}")
    print("METRICS:")
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    for key in sorted(metrics.keys()):
        print(f"  - {key}: {metrics[key]}")
    if bool(report.get("flat_start_rebased")):
        print(
            "FLAT_START_REBASE: "
            f"applied=true, cutoff_utc={report.get('flat_start_rebase_cutoff_utc')}"
        )

    account_pnl = report.get("account_pnl", {})
    if isinstance(account_pnl, dict):
        print("ACCOUNT_PNL:")
        for key in (
            "samples",
            "first_sample_utc",
            "last_sample_utc",
            "first_equity_usd",
            "last_equity_usd",
            "equity_change_usd",
            "equity_change_pct",
            "day_start_equity_usd",
            "equity_change_vs_day_start_usd",
            "equity_change_vs_day_start_pct",
            "max_equity_usd_observed",
            "peak_to_last_drawdown_pct",
            "max_drawdown_pct_observed",
            "max_abs_notional_usd_observed",
            "first_notional_usd",
            "first_abs_notional_usd",
            "start_flat",
            "fee_samples",
            "first_realized_pnl_usd",
            "last_realized_pnl_usd",
            "realized_pnl_change_usd",
            "first_fee_usd",
            "last_fee_usd",
            "fee_change_usd",
            "first_realized_net_pnl_usd",
            "last_realized_net_pnl_usd",
            "realized_net_pnl_change_usd",
        ):
            print(f"  - {key}: {account_pnl.get(key)}")

    fail_reasons = report["fail_reasons"]
    warn_reasons = report["warn_reasons"]
    assert isinstance(fail_reasons, list)
    assert isinstance(warn_reasons, list)

    if fail_reasons:
        print("FAIL_REASONS:")
        for item in fail_reasons:
            print(f"  - {item}")
    if warn_reasons:
        print("WARN_REASONS:")
        for item in warn_reasons:
            print(f"  - {item}")


def main() -> int:
    args = parse_args()
    log_path = Path(args.log)
    if not log_path.exists():
        print(f"[ERROR] 日志文件不存在: {log_path}", file=sys.stderr)
        return 2

    stage = STAGE_RULES[args.stage]
    min_runtime_status = (
        args.min_runtime_status
        if args.min_runtime_status is not None
        else stage.min_runtime_status
    )
    if min_runtime_status < 0:
        print("[ERROR] --min_runtime_status 必须大于等于 0", file=sys.stderr)
        return 2
    if args.s5_min_effective_updates < 0:
        print("[ERROR] --s5-min-effective-updates 必须大于等于 0", file=sys.stderr)
        return 2
    if args.s5_min_realized_net_per_fill_windows < 0:
        print(
            "[ERROR] --s5-min-realized-net-per-fill-windows 必须大于等于 0",
            file=sys.stderr,
        )
        return 2

    text = load_text(log_path)
    report = assess(
        text,
        stage,
        min_runtime_status,
        s5_min_effective_updates=args.s5_min_effective_updates,
        s5_min_realized_net_per_fill_usd=args.s5_min_realized_net_per_fill_usd,
        s5_min_realized_net_per_fill_windows=args.s5_min_realized_net_per_fill_windows,
    )
    print_report(report)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON written: {out_path}")

    verdict = report["verdict"]
    return 0 if verdict in {"PASS", "PASS_WITH_ACTIONS"} else 1


if __name__ == "__main__":
    sys.exit(main())
