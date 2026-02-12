#!/usr/bin/env python3
"""
运行日志自动验收脚本（S3/S5）。

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
    require_evolution_init: bool
    max_trading_halted_true_ratio: float
    gate_warn_min_windows: int
    gate_warn_max_fail_ratio: float


STAGE_RULES: Dict[str, StageRule] = {
    "S3": StageRule(
        name="S3",
        min_runtime_status=10,
        require_gate_window=True,
        require_evolution_init=False,
        max_trading_halted_true_ratio=0.20,
        gate_warn_min_windows=20,
        gate_warn_max_fail_ratio=0.90,
    ),
    "S5": StageRule(
        name="S5",
        min_runtime_status=50,
        require_gate_window=True,
        require_evolution_init=True,
        max_trading_halted_true_ratio=0.10,
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

    for m in RUNTIME_ACCOUNT_RE.finditer(text):
        try:
            timestamps.append(dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"))
            equities.append(float(m.group("equity")))
            drawdowns.append(float(m.group("drawdown_pct")))
            notionals.append(float(m.group("notional")))
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
    }


def assess(text: str, stage: StageRule, min_runtime_status: int) -> Dict[str, object]:
    account_pnl = extract_runtime_account_series(text)
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
        "runtime_account_samples": account_pnl["samples"],
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

    if metrics["runtime_status_count"] < min_runtime_status:
        fail_reasons.append(
            f"RUNTIME_STATUS 条数不足: {metrics['runtime_status_count']} < {min_runtime_status}"
        )
    if metrics["critical_count"] > 0:
        fail_reasons.append(f"出现 CRITICAL: {metrics['critical_count']}")
    if metrics["trading_halted_true_ratio"] > stage.max_trading_halted_true_ratio:
        fail_reasons.append(
            "trading_halted=true 占比超阈值: "
            f"{metrics['trading_halted_true_ratio']:.4f} > "
            f"{stage.max_trading_halted_true_ratio:.4f}"
        )
    if metrics["ws_unhealthy_count"] > 0:
        fail_reasons.append(f"运行态 WS 健康检查失败次数: {metrics['ws_unhealthy_count']}")

    if stage.require_gate_window and gate_window_count <= 0:
        fail_reasons.append("未检测到 Gate 窗口判定（GATE_CHECK_PASSED/FAILED）")

    if (
        stage.require_evolution_init
        and metrics["self_evolution_init_count"] <= 0
        and metrics["self_evolution_action_count"] <= 0
    ):
        fail_reasons.append("未检测到 SELF_EVOLUTION_INIT/SELF_EVOLUTION_ACTION")

    # 软告警：不阻断阶段，但需要后续参数/策略动作
    if metrics["reconcile_mismatch_count"] > 0 and metrics["reconcile_autoresync_count"] <= 0:
        warn_reasons.append("出现对账不一致但未观察到 AUTORESYNC")
    gate_runtime_impact = (
        metrics["trading_halted_true_count"] > 0
        or metrics["gate_reduce_only_true_count"] > 0
        or metrics["gate_halted_true_count"] > 0
    )
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
        and metrics["trading_halted_true_ratio"] <= stage.max_trading_halted_true_ratio
    ):
        warn_reasons.append(
            "出现短暂 trading_halted=true，但占比在阈值内，建议复核 Gate/对账参数"
        )
    if (
        stage.name == "S5"
        and metrics["self_evolution_action_count"] <= 0
        and metrics["self_evolution_init_count"] > 0
    ):
        warn_reasons.append("未观测到 SELF_EVOLUTION_ACTION，建议检查 update_interval 与样本门槛")

    integrator_takeover_mode_count = (
        metrics["integrator_mode_canary_count"] + metrics["integrator_mode_active_count"]
    )
    if integrator_takeover_mode_count > 0 and metrics["integrator_policy_applied_count"] <= 0:
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
    if min_runtime_status <= 0:
        print("[ERROR] --min_runtime_status 必须大于 0", file=sys.stderr)
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
