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
    require_flat_start: bool
    max_start_abs_notional_usd: float
    max_trading_halted_true_ratio: float
    gate_fail_hard_min_windows: int
    gate_fail_hard_max_fail_ratio: float
    gate_warn_min_windows: int
    gate_warn_max_fail_ratio: float


STAGE_RULES: Dict[str, StageRule] = {
    "DEPLOY": StageRule(
        name="DEPLOY",
        min_runtime_status=0,
        require_gate_window=False,
        require_gate_pass=False,
        require_evolution_init=False,
        require_flat_start=False,
        max_start_abs_notional_usd=0.0,
        max_trading_halted_true_ratio=0.50,
        gate_fail_hard_min_windows=0,
        gate_fail_hard_max_fail_ratio=1.10,
        gate_warn_min_windows=10,
        gate_warn_max_fail_ratio=0.95,
    ),
    "S3": StageRule(
        name="S3",
        min_runtime_status=10,
        require_gate_window=True,
        require_gate_pass=False,
        require_evolution_init=False,
        require_flat_start=False,
        max_start_abs_notional_usd=0.0,
        max_trading_halted_true_ratio=0.20,
        gate_fail_hard_min_windows=0,
        gate_fail_hard_max_fail_ratio=1.10,
        gate_warn_min_windows=20,
        gate_warn_max_fail_ratio=0.90,
    ),
    "S5": StageRule(
        name="S5",
        min_runtime_status=50,
        require_gate_window=True,
        require_gate_pass=True,
        require_evolution_init=True,
        require_flat_start=True,
        max_start_abs_notional_usd=50.0,
        max_trading_halted_true_ratio=0.10,
        gate_fail_hard_min_windows=50,
        gate_fail_hard_max_fail_ratio=0.95,
        gate_warn_min_windows=50,
        gate_warn_max_fail_ratio=0.95,
    ),
}

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


def assess(text: str, stage: StageRule, min_runtime_status: int) -> Dict[str, object]:
    account_pnl = extract_runtime_account_series(text)
    strategy_mix = extract_strategy_mix_series(text)
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
        "self_evolution_init_count": count(r"SELF_EVOLUTION_INIT", text),
        "self_evolution_action_count": count(r"SELF_EVOLUTION_ACTION", text),
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
    }
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
    gate_window_count = metrics["gate_check_passed_count"] + metrics["gate_check_failed_count"]
    if gate_window_count > 0:
        metrics["gate_check_fail_ratio"] = (
            metrics["gate_check_failed_count"] / gate_window_count
        )
    else:
        metrics["gate_check_fail_ratio"] = 0.0

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

    gate_runtime_impact = (
        metrics["trading_halted_true_count"] > 0
        or metrics["gate_reduce_only_true_count"] > 0
        or metrics["gate_halted_true_count"] > 0
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
            and metrics["self_evolution_init_count"] <= 0
            and metrics["self_evolution_action_count"] <= 0
        ):
            fail_reasons.append("未检测到 SELF_EVOLUTION_INIT/SELF_EVOLUTION_ACTION")

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
            and gate_runtime_impact
        ):
            fail_reasons.append(
                "Gate 失败率过高且影响运行态（强闭环阻断）: "
                f"fail_ratio={metrics['gate_check_fail_ratio']:.4f}, "
                f"threshold={stage.gate_fail_hard_max_fail_ratio:.4f}"
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
            and gate_runtime_impact
        ):
            warn_reasons.append(
                "Gate 失败率偏高且已触发运行态限制，建议复核策略活跃度/门槛参数: "
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
    }


def print_report(report: Dict[str, object]) -> None:
    print(f"STAGE: {report['stage']}")
    print(f"VERDICT: {report['verdict']}")
    print("METRICS:")
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    for key in sorted(metrics.keys()):
        print(f"  - {key}: {metrics[key]}")

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

    text = load_text(log_path)
    report = assess(text, stage, min_runtime_status)
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
