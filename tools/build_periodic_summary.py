#!/usr/bin/env python3
"""
按时间窗口聚合闭环报告，生成日报/周报摘要。

输入：
1. data/reports/closed_loop/<RUN_ID>/closed_loop_report.json

输出：
1. data/reports/closed_loop/summary/daily_<YYYYMMDD>.json
2. data/reports/closed_loop/summary/weekly_<YYYYWww>.json
3. data/reports/closed_loop/summary/daily_latest.json
4. data/reports/closed_loop/summary/weekly_latest.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成闭环日报/周报摘要")
    parser.add_argument(
        "--reports-root",
        default="./data/reports/closed_loop",
        help="闭环报告根目录（默认 ./data/reports/closed_loop）",
    )
    parser.add_argument(
        "--out-dir",
        default="./data/reports/closed_loop/summary",
        help="摘要输出目录（默认 ./data/reports/closed_loop/summary）",
    )
    parser.add_argument(
        "--daily-window-hours",
        type=int,
        default=24,
        help="日报窗口小时数（默认 24）",
    )
    parser.add_argument(
        "--weekly-window-hours",
        type=int,
        default=24 * 7,
        help="周报窗口小时数（默认 168）",
    )
    parser.add_argument(
        "--now-utc",
        default="",
        help="可选：当前 UTC 时间（ISO8601，例如 2026-02-12T08:00:00Z）",
    )
    return parser.parse_args()


def parse_utc(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return None


def to_iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_float(value: Any) -> Optional[float]:
    if isinstance(value, (float, int)):
        return float(value)
    return None


def to_int(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    return None


def load_report(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_reports(reports_root: Path) -> Iterable[Dict[str, Any]]:
    if not reports_root.exists():
        return
    for child in sorted(reports_root.iterdir()):
        if not child.is_dir():
            continue
        if not RUN_ID_RE.match(child.name):
            continue
        report_path = child / "closed_loop_report.json"
        if not report_path.is_file():
            continue
        try:
            payload = load_report(report_path)
        except Exception:
            continue
        generated_at_utc = parse_utc(str(payload.get("generated_at_utc", "")))
        if generated_at_utc is None:
            try:
                generated_at_utc = dt.datetime.strptime(
                    child.name, "%Y%m%dT%H%M%SZ"
                ).replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
        yield {
            "run_id": child.name,
            "path": str(report_path),
            "generated_at_utc": generated_at_utc,
            "report": payload,
        }


def compute_account_summary(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    earliest_ts: Optional[dt.datetime] = None
    earliest_eq: Optional[float] = None
    latest_ts: Optional[dt.datetime] = None
    latest_eq: Optional[float] = None

    max_drawdown: Optional[float] = None
    max_abs_notional: Optional[float] = None
    max_equity_observed: Optional[float] = None
    peak_to_last_drawdown: Optional[float] = None
    pnl_changes: List[float] = []
    realized_net_changes: List[float] = []
    fee_changes: List[float] = []

    for item in runs:
        account = item["report"].get("account_outcome", {})
        if not isinstance(account, dict):
            continue

        first_ts = parse_utc(str(account.get("first_sample_utc", "")))
        first_eq = to_float(account.get("first_equity_usd"))
        if first_ts is not None and first_eq is not None:
            if earliest_ts is None or first_ts < earliest_ts:
                earliest_ts = first_ts
                earliest_eq = first_eq

        last_ts = parse_utc(str(account.get("last_sample_utc", "")))
        last_eq = to_float(account.get("last_equity_usd"))
        if last_ts is not None and last_eq is not None:
            if latest_ts is None or last_ts > latest_ts:
                latest_ts = last_ts
                latest_eq = last_eq

        drawdown = to_float(account.get("max_drawdown_pct_observed"))
        if drawdown is not None:
            max_drawdown = drawdown if max_drawdown is None else max(max_drawdown, drawdown)

        abs_notional = to_float(account.get("max_abs_notional_usd_observed"))
        if abs_notional is not None:
            max_abs_notional = (
                abs_notional if max_abs_notional is None else max(max_abs_notional, abs_notional)
            )

        eq_obs = to_float(account.get("max_equity_usd_observed"))
        if eq_obs is not None:
            max_equity_observed = (
                eq_obs if max_equity_observed is None else max(max_equity_observed, eq_obs)
            )

        peak_draw = to_float(account.get("peak_to_last_drawdown_pct"))
        if peak_draw is not None:
            peak_to_last_drawdown = (
                peak_draw if peak_to_last_drawdown is None else max(peak_to_last_drawdown, peak_draw)
            )

        change = to_float(account.get("equity_change_usd"))
        if change is not None:
            pnl_changes.append(change)
        realized_net_change = to_float(account.get("realized_net_pnl_change_usd"))
        if realized_net_change is not None:
            realized_net_changes.append(realized_net_change)
        fee_change = to_float(account.get("fee_change_usd"))
        if fee_change is not None:
            fee_changes.append(fee_change)

    net_change = None
    net_change_pct = None
    if earliest_eq is not None and latest_eq is not None:
        net_change = latest_eq - earliest_eq
        if abs(earliest_eq) > 1e-12:
            net_change_pct = net_change / earliest_eq

    avg_change = sum(pnl_changes) / len(pnl_changes) if pnl_changes else None
    avg_realized_net_change = (
        sum(realized_net_changes) / len(realized_net_changes)
        if realized_net_changes
        else None
    )
    avg_fee_change = sum(fee_changes) / len(fee_changes) if fee_changes else None

    return {
        "earliest_sample_utc": to_iso_utc(earliest_ts) if earliest_ts else None,
        "latest_sample_utc": to_iso_utc(latest_ts) if latest_ts else None,
        "first_equity_usd": earliest_eq,
        "last_equity_usd": latest_eq,
        "equity_change_usd": net_change,
        "equity_change_pct": net_change_pct,
        "avg_run_equity_change_usd": avg_change,
        "avg_run_realized_net_change_usd": avg_realized_net_change,
        "avg_run_fee_change_usd": avg_fee_change,
        "max_drawdown_pct_observed": max_drawdown,
        "max_abs_notional_usd_observed": max_abs_notional,
        "max_equity_usd_observed": max_equity_observed,
        "peak_to_last_drawdown_pct": peak_to_last_drawdown,
    }


def compute_runtime_summary(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest_item: Optional[Dict[str, Any]] = None
    latest_runtime: Optional[Dict[str, Any]] = None
    latest_metrics: Optional[Dict[str, Any]] = None
    for item in reversed(runs):
        runtime = item["report"].get("sections", {}).get("runtime", {})
        if not isinstance(runtime, dict):
            continue
        metrics = runtime.get("metrics", {})
        if not isinstance(metrics, dict):
            continue
        latest_item = item
        latest_runtime = runtime
        latest_metrics = metrics
        break

    if latest_item is None or latest_runtime is None or latest_metrics is None:
        return {
            "aggregation_mode": "latest_run_snapshot",
            "source_run_id": None,
            "source_generated_at_utc": None,
            "source_runtime_verdict": None,
            "gate_check_failed_count": 0,
            "gate_check_passed_count": 0,
            "gate_check_fail_ratio": None,
            "gate_alert_count": 0,
            "trading_halted_true_count": 0,
            "trading_halted_true_ratio": None,
            "runtime_status_count": 0,
            "reconcile_mismatch_count": 0,
            "reconcile_autoresync_count": 0,
            "self_evolution_action_count": 0,
            "self_evolution_virtual_action_count": 0,
            "self_evolution_counterfactual_action_count": 0,
            "self_evolution_counterfactual_update_count": 0,
            "self_evolution_factor_ic_action_count": 0,
            "self_evolution_learnability_skip_count": 0,
            "self_evolution_learnability_pass_count": 0,
            "flat_start_rebase_applied_count": 0,
            "integrator_policy_applied_count": 0,
            "integrator_policy_canary_count": 0,
            "integrator_policy_active_count": 0,
            "integrator_policy_applied_ratio": None,
            "integrator_mode_shadow_count": 0,
            "integrator_mode_canary_count": 0,
            "integrator_mode_active_count": 0,
            "filtered_cost_ratio": None,
            "filtered_cost_ratio_avg": None,
            "realized_net_per_fill": None,
            "fee_bps_per_fill": None,
            "execution_window_maker_fill_ratio_avg": None,
            "execution_window_maker_fee_bps_avg": None,
            "execution_window_taker_fee_bps_avg": None,
            "execution_quality_guard_active_count": 0,
            "execution_quality_guard_active_ratio": None,
            "execution_quality_guard_penalty_bps_avg": None,
            "reconcile_anomaly_reduce_only_true_count": 0,
            "reconcile_anomaly_reduce_only_true_ratio": None,
            "strategy_mix_runtime_count": 0,
            "strategy_mix_nonzero_window_count": 0,
            "strategy_mix_defensive_active_count": 0,
            "strategy_mix_avg_abs_trend_notional": None,
            "strategy_mix_avg_abs_defensive_notional": None,
            "strategy_mix_avg_abs_blended_notional": None,
            "strategy_mix_avg_defensive_share": None,
        }

    gate_failed = to_int(latest_metrics.get("gate_check_failed_count")) or 0
    gate_passed = to_int(latest_metrics.get("gate_check_passed_count")) or 0
    gate_alerts = to_int(latest_metrics.get("gate_alert_count")) or 0
    trading_halted_true = to_int(latest_metrics.get("trading_halted_true_count")) or 0
    runtime_status = to_int(latest_metrics.get("runtime_status_count")) or 0
    reconcile_mismatch = to_int(latest_metrics.get("reconcile_mismatch_count")) or 0
    reconcile_autoresync = to_int(latest_metrics.get("reconcile_autoresync_count")) or 0
    evolution_action = to_int(latest_metrics.get("self_evolution_action_count")) or 0
    evolution_virtual_action = (
        to_int(latest_metrics.get("self_evolution_virtual_action_count")) or 0
    )
    evolution_counterfactual_action = (
        to_int(latest_metrics.get("self_evolution_counterfactual_action_count")) or 0
    )
    evolution_counterfactual_update = (
        to_int(latest_metrics.get("self_evolution_counterfactual_update_count")) or 0
    )
    evolution_factor_ic_action = (
        to_int(latest_metrics.get("self_evolution_factor_ic_action_count")) or 0
    )
    evolution_learnability_skip = (
        to_int(latest_metrics.get("self_evolution_learnability_skip_count")) or 0
    )
    evolution_learnability_pass = (
        to_int(latest_metrics.get("self_evolution_learnability_pass_count")) or 0
    )
    flat_start_rebase_applied = (
        to_int(latest_metrics.get("flat_start_rebase_applied_count")) or 0
    )
    integrator_policy_applied = (
        to_int(latest_metrics.get("integrator_policy_applied_count")) or 0
    )
    integrator_policy_canary = (
        to_int(latest_metrics.get("integrator_policy_canary_count")) or 0
    )
    integrator_policy_active = (
        to_int(latest_metrics.get("integrator_policy_active_count")) or 0
    )
    integrator_mode_shadow = to_int(latest_metrics.get("integrator_mode_shadow_count")) or 0
    integrator_mode_canary = to_int(latest_metrics.get("integrator_mode_canary_count")) or 0
    integrator_mode_active = to_int(latest_metrics.get("integrator_mode_active_count")) or 0
    filtered_cost_ratio = to_float(latest_metrics.get("filtered_cost_ratio"))
    filtered_cost_ratio_avg = to_float(latest_metrics.get("filtered_cost_ratio_avg"))
    realized_net_per_fill = to_float(latest_metrics.get("realized_net_per_fill"))
    fee_bps_per_fill = to_float(latest_metrics.get("fee_bps_per_fill"))
    execution_window_maker_fill_ratio_avg = to_float(
        latest_metrics.get("execution_window_maker_fill_ratio_avg")
    )
    execution_window_maker_fee_bps_avg = to_float(
        latest_metrics.get("execution_window_maker_fee_bps_avg")
    )
    execution_window_taker_fee_bps_avg = to_float(
        latest_metrics.get("execution_window_taker_fee_bps_avg")
    )
    execution_quality_guard_active_count = (
        to_int(latest_metrics.get("execution_quality_guard_active_count")) or 0
    )
    execution_quality_guard_active_ratio = to_float(
        latest_metrics.get("execution_quality_guard_active_ratio")
    )
    execution_quality_guard_penalty_bps_avg = to_float(
        latest_metrics.get("execution_quality_guard_penalty_bps_avg")
    )
    reconcile_anomaly_reduce_only_true_count = (
        to_int(latest_metrics.get("reconcile_anomaly_reduce_only_true_count")) or 0
    )
    reconcile_anomaly_reduce_only_true_ratio = to_float(
        latest_metrics.get("reconcile_anomaly_reduce_only_true_ratio")
    )
    strategy_mix_runtime_count = (
        to_int(latest_metrics.get("strategy_mix_runtime_count")) or 0
    )
    strategy_mix_nonzero_window_count = (
        to_int(latest_metrics.get("strategy_mix_nonzero_window_count")) or 0
    )
    strategy_mix_defensive_active_count = (
        to_int(latest_metrics.get("strategy_mix_defensive_active_count")) or 0
    )
    strategy_mix_avg_abs_trend_notional = to_float(
        latest_metrics.get("strategy_mix_avg_abs_trend_notional")
    )
    strategy_mix_avg_abs_defensive_notional = to_float(
        latest_metrics.get("strategy_mix_avg_abs_defensive_notional")
    )
    strategy_mix_avg_abs_blended_notional = to_float(
        latest_metrics.get("strategy_mix_avg_abs_blended_notional")
    )
    strategy_mix_avg_defensive_share = to_float(
        latest_metrics.get("strategy_mix_avg_defensive_share")
    )

    total_gate = gate_failed + gate_passed
    gate_fail_ratio = to_float(latest_metrics.get("gate_check_fail_ratio"))
    if gate_fail_ratio is None:
        gate_fail_ratio = gate_failed / total_gate if total_gate > 0 else None

    trading_halted_ratio = to_float(latest_metrics.get("trading_halted_true_ratio"))
    if trading_halted_ratio is None:
        trading_halted_ratio = (
            trading_halted_true / runtime_status if runtime_status > 0 else None
        )

    integrator_policy_applied_ratio = to_float(
        latest_metrics.get("integrator_policy_applied_ratio")
    )
    if integrator_policy_applied_ratio is None:
        integrator_policy_applied_ratio = (
            integrator_policy_applied / runtime_status if runtime_status > 0 else None
        )

    return {
        "aggregation_mode": "latest_run_snapshot",
        "source_run_id": latest_item["run_id"],
        "source_generated_at_utc": to_iso_utc(latest_item["generated_at_utc"]),
        "source_runtime_verdict": latest_runtime.get("verdict"),
        "gate_check_failed_count": gate_failed,
        "gate_check_passed_count": gate_passed,
        "gate_check_fail_ratio": gate_fail_ratio,
        "gate_alert_count": gate_alerts,
        "trading_halted_true_count": trading_halted_true,
        "trading_halted_true_ratio": trading_halted_ratio,
        "runtime_status_count": runtime_status,
        "reconcile_mismatch_count": reconcile_mismatch,
        "reconcile_autoresync_count": reconcile_autoresync,
        "self_evolution_action_count": evolution_action,
        "self_evolution_virtual_action_count": evolution_virtual_action,
        "self_evolution_counterfactual_action_count": evolution_counterfactual_action,
        "self_evolution_counterfactual_update_count": evolution_counterfactual_update,
        "self_evolution_factor_ic_action_count": evolution_factor_ic_action,
        "self_evolution_learnability_skip_count": evolution_learnability_skip,
        "self_evolution_learnability_pass_count": evolution_learnability_pass,
        "flat_start_rebase_applied_count": flat_start_rebase_applied,
        "integrator_policy_applied_count": integrator_policy_applied,
        "integrator_policy_canary_count": integrator_policy_canary,
        "integrator_policy_active_count": integrator_policy_active,
        "integrator_policy_applied_ratio": integrator_policy_applied_ratio,
        "integrator_mode_shadow_count": integrator_mode_shadow,
        "integrator_mode_canary_count": integrator_mode_canary,
        "integrator_mode_active_count": integrator_mode_active,
        "filtered_cost_ratio": filtered_cost_ratio,
        "filtered_cost_ratio_avg": filtered_cost_ratio_avg,
        "realized_net_per_fill": realized_net_per_fill,
        "fee_bps_per_fill": fee_bps_per_fill,
        "execution_window_maker_fill_ratio_avg": execution_window_maker_fill_ratio_avg,
        "execution_window_maker_fee_bps_avg": execution_window_maker_fee_bps_avg,
        "execution_window_taker_fee_bps_avg": execution_window_taker_fee_bps_avg,
        "execution_quality_guard_active_count": execution_quality_guard_active_count,
        "execution_quality_guard_active_ratio": execution_quality_guard_active_ratio,
        "execution_quality_guard_penalty_bps_avg": execution_quality_guard_penalty_bps_avg,
        "reconcile_anomaly_reduce_only_true_count": reconcile_anomaly_reduce_only_true_count,
        "reconcile_anomaly_reduce_only_true_ratio": reconcile_anomaly_reduce_only_true_ratio,
        "strategy_mix_runtime_count": strategy_mix_runtime_count,
        "strategy_mix_nonzero_window_count": strategy_mix_nonzero_window_count,
        "strategy_mix_defensive_active_count": strategy_mix_defensive_active_count,
        "strategy_mix_avg_abs_trend_notional": strategy_mix_avg_abs_trend_notional,
        "strategy_mix_avg_abs_defensive_notional": strategy_mix_avg_abs_defensive_notional,
        "strategy_mix_avg_abs_blended_notional": strategy_mix_avg_abs_blended_notional,
        "strategy_mix_avg_defensive_share": strategy_mix_avg_defensive_share,
    }


def top_warn_reasons(runs: List[Dict[str, Any]], top_n: int = 10) -> List[Dict[str, Any]]:
    counter: Counter[str] = Counter()
    for item in runs:
        warns = item["report"].get("warn_reasons", [])
        if isinstance(warns, list):
            for w in warns:
                if isinstance(w, str) and w.strip():
                    counter[w.strip()] += 1
    return [{"reason": reason, "count": count} for reason, count in counter.most_common(top_n)]


def build_period_summary(
    *,
    label: str,
    window_hours: int,
    now_utc: dt.datetime,
    all_runs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    since = now_utc - dt.timedelta(hours=window_hours)
    runs = [x for x in all_runs if x["generated_at_utc"] >= since]
    runs.sort(key=lambda x: x["generated_at_utc"])

    status_counts = Counter()
    for item in runs:
        overall = str(item["report"].get("overall_status", "UNKNOWN"))
        status_counts[overall] += 1

    if not runs:
        overall_status = "NO_DATA"
    elif status_counts.get("FAIL", 0) > 0:
        overall_status = "FAIL"
    elif status_counts.get("PASS_WITH_ACTIONS", 0) > 0:
        overall_status = "PASS_WITH_ACTIONS"
    else:
        overall_status = "PASS"

    latest = runs[-1] if runs else None
    latest_run = {
        "run_id": latest["run_id"] if latest else None,
        "generated_at_utc": to_iso_utc(latest["generated_at_utc"]) if latest else None,
        "overall_status": latest["report"].get("overall_status") if latest else None,
        "path": latest["path"] if latest else None,
    }

    runs_brief = [
        {
            "run_id": item["run_id"],
            "generated_at_utc": to_iso_utc(item["generated_at_utc"]),
            "overall_status": item["report"].get("overall_status"),
            "equity_change_usd": item["report"].get("account_outcome", {}).get("equity_change_usd"),
            "equity_change_pct": item["report"].get("account_outcome", {}).get("equity_change_pct"),
        }
        for item in runs[-50:]
    ]

    return {
        "summary_type": label,
        "generated_at_utc": to_iso_utc(now_utc),
        "window_hours": window_hours,
        "window_start_utc": to_iso_utc(since),
        "window_end_utc": to_iso_utc(now_utc),
        "overall_status": overall_status,
        "run_count": len(runs),
        "status_counts": {
            "PASS": status_counts.get("PASS", 0),
            "PASS_WITH_ACTIONS": status_counts.get("PASS_WITH_ACTIONS", 0),
            "FAIL": status_counts.get("FAIL", 0),
        },
        "latest_run": latest_run,
        "account_outcome": compute_account_summary(runs),
        "runtime_metrics": compute_runtime_summary(runs),
        "top_warn_reasons": top_warn_reasons(runs),
        "runs_brief": runs_brief,
    }


def write_summary(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    reports_root = Path(args.reports_root)
    out_dir = Path(args.out_dir)
    now_utc = parse_utc(args.now_utc) if args.now_utc else None
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)

    if args.daily_window_hours <= 0 or args.weekly_window_hours <= 0:
        print("[ERROR] window hours 必须 > 0", file=sys.stderr)
        return 2

    all_runs = list(iter_reports(reports_root))
    all_runs.sort(key=lambda x: x["generated_at_utc"])

    daily = build_period_summary(
        label="daily",
        window_hours=args.daily_window_hours,
        now_utc=now_utc,
        all_runs=all_runs,
    )
    weekly = build_period_summary(
        label="weekly",
        window_hours=args.weekly_window_hours,
        now_utc=now_utc,
        all_runs=all_runs,
    )

    daily_tag = now_utc.strftime("%Y%m%d")
    iso_year, iso_week, _ = now_utc.isocalendar()
    weekly_tag = f"{iso_year}W{iso_week:02d}"

    daily_path = out_dir / f"daily_{daily_tag}.json"
    weekly_path = out_dir / f"weekly_{weekly_tag}.json"
    daily_latest = out_dir / "daily_latest.json"
    weekly_latest = out_dir / "weekly_latest.json"

    write_summary(daily_path, daily)
    write_summary(weekly_path, weekly)
    shutil.copy2(daily_path, daily_latest)
    shutil.copy2(weekly_path, weekly_latest)

    print(f"DAILY_SUMMARY: {daily_path}")
    print(f"WEEKLY_SUMMARY: {weekly_path}")
    print(f"DAILY_LATEST: {daily_latest}")
    print(f"WEEKLY_LATEST: {weekly_latest}")
    print(
        "SUMMARY_STATUS: daily={daily}, weekly={weekly}".format(
            daily=daily.get("overall_status"),
            weekly=weekly.get("overall_status"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
