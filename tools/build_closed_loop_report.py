#!/usr/bin/env python3
"""
构建闭环汇总报告：
- 汇总 Miner / Integrator / Registry / Runtime 验收结果
- 输出单份 JSON，便于人工审阅与后续自动化处理
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

INHERITABLE_SECTION_NAMES = [
    "miner",
    "baseline",
    "data_quality",
    "integrator",
    "registry",
    "data_pipeline",
    "walkforward",
    "trend_validation",
    "replay_validation",
]


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def status_tuple(ok: bool, reason: str = "") -> Tuple[str, List[str]]:
    if ok:
        return "pass", []
    if reason:
        return "fail", [reason]
    return "fail", []


def assess_miner(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    factors = payload.get("factors", [])
    not_worse = payload.get("oos_not_worse_than_random")
    top_ic = payload.get("top_factor_oos_abs_ic")

    ok = isinstance(factors, list) and len(factors) > 0 and bool(not_worse)
    status, fails = status_tuple(ok, "Miner 因子为空或 OOS 未通过随机基线")
    return {
        "status": status,
        "fail_reasons": fails,
        "factor_set_version": payload.get("factor_set_version"),
        "factor_count": len(factors) if isinstance(factors, list) else 0,
        "oos_not_worse_than_random": bool(not_worse),
        "top_factor_oos_abs_ic": top_ic,
    }


def assess_integrator(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    metrics = payload.get("metrics_oos", {})
    auc_mean = metrics.get("auc_mean")
    split_trained = metrics.get("split_trained_count")
    split_count = metrics.get("split_count")
    delta_auc = metrics.get("delta_auc_vs_baseline")

    ok = (
        isinstance(payload.get("model_version"), str)
        and bool(payload.get("model_version"))
        and isinstance(auc_mean, (float, int))
        and isinstance(split_trained, int)
        and isinstance(split_count, int)
        and split_trained > 0
        and split_count >= split_trained
    )
    status, fails = status_tuple(ok, "Integrator 报告缺失关键字段或 split 训练计数异常")
    return {
        "status": status,
        "fail_reasons": fails,
        "model_version": payload.get("model_version"),
        "feature_schema_version": payload.get("feature_schema_version"),
        "auc_mean": auc_mean,
        "delta_auc_vs_baseline": delta_auc,
        "split_trained_count": split_trained,
        "split_count": split_count,
    }


def assess_baseline(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    available = bool(payload.get("baseline_available", False))
    warns: List[str] = []
    if not available:
        warns.append("未冻结到可用 baseline（可能首次训练或未生成 active 模型）")
    return {
        "status": "pass",
        "fail_reasons": [],
        "warn_reasons": warns,
        "baseline_available": available,
        "baseline_status": payload.get("status"),
        "model_meta": payload.get("model_meta", {}),
    }


def assess_data_quality(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    gate_pass = bool(payload.get("gate_pass", False))
    status, fails = status_tuple(gate_pass, "数据质量门禁未通过")
    detail_fails = payload.get("fail_reasons", [])
    if not gate_pass and isinstance(detail_fails, list):
        fails.extend([str(item) for item in detail_fails])
    return {
        "status": status,
        "fail_reasons": fails if gate_pass is False else [],
        "warn_reasons": payload.get("warn_reasons", []),
        "gate_pass": gate_pass,
        "summary": payload.get("summary", {}),
    }


def assess_registry(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    gate = payload.get("gate", {})
    gate_pass = bool(gate.get("pass", False))
    activated = bool(payload.get("activated", False))
    ok = gate_pass
    status, fails = status_tuple(ok, "模型注册门槛未通过（gate.pass=false）")
    warnings: List[str] = [str(item) for item in gate.get("warn_reasons", []) if str(item)]
    if gate_pass and not activated:
        warnings.append("注册门槛通过但未激活（可能是未开启 activate_on_pass）")
    return {
        "status": status,
        "fail_reasons": fails,
        "warn_reasons": warnings,
        "entry_id": payload.get("entry_id"),
        "model_version": payload.get("model_version"),
        "gate_pass": gate_pass,
        "activated": activated,
        "gate_fail_reasons": gate.get("fail_reasons", []),
        "gate_warn_reasons": gate.get("warn_reasons", []),
        "gate_metric_summary": gate.get("metric_summary", {}),
        "gate_thresholds": {
            "min_auc_mean": gate.get("min_auc_mean"),
            "min_delta_auc_vs_baseline": gate.get("min_delta_auc_vs_baseline"),
            "min_split_trained_count": gate.get("min_split_trained_count"),
            "min_split_trained_ratio": gate.get("min_split_trained_ratio"),
        },
    }


def assess_runtime(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    verdict = str(payload.get("verdict", "FAIL"))
    ok = verdict in {"PASS", "PASS_WITH_ACTIONS"}
    status, fails = status_tuple(ok, f"运行验收未通过: verdict={verdict}")
    warns: List[str] = []
    if verdict == "PASS_WITH_ACTIONS":
        warns.append("运行验收为 PASS_WITH_ACTIONS，建议执行告警项整改")
    for item in payload.get("warn_reasons", []):
        item_text = str(item)
        if item_text and item_text not in warns:
            warns.append(item_text)
    execution_status = str(payload.get("execution_status", ""))
    if execution_status == "NOT_EVALUATED":
        market_context_status = str(payload.get("market_context_status", ""))
        if market_context_status in {"RANGE_ONLY", "EXTREME_ONLY", "RANGE_EXTREME_ONLY"}:
            extra = "运行保护通过，但当前窗口未形成可交易趋势样本，执行质量未完成验证"
        else:
            extra = "运行保护通过，但执行质量未完成验证"
        if extra not in warns:
            warns.append(extra)
    return {
        "status": status,
        "fail_reasons": fails,
        "warn_reasons": warns,
        "stage": payload.get("stage"),
        "verdict": verdict,
        "runtime_validation_mode": payload.get("runtime_validation_mode"),
        "protection_status": payload.get("protection_status"),
        "execution_status": payload.get("execution_status"),
        "market_context_status": payload.get("market_context_status"),
        "account_sync_status": payload.get("account_sync_status"),
        "protection_fail_reasons": payload.get("protection_fail_reasons", []),
        "execution_fail_reasons": payload.get("execution_fail_reasons", []),
        "metrics": payload.get("metrics", {}),
        "account_pnl": payload.get("account_pnl", {}),
    }


def assess_data_pipeline(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    status_raw = str(payload.get("status", "")).upper()
    steps = payload.get("steps", [])
    if not isinstance(steps, list):
        steps = []
    failed_steps = [
        str(step.get("name", "unknown"))
        for step in steps
        if isinstance(step, dict)
        and bool(step.get("enabled", False))
        and str(step.get("status", "")).lower() == "fail"
    ]
    warns: List[str] = []
    if status_raw == "PLANNED":
        warns.append("数据加速链路为 dry-run（PLANNED），未执行真实更新")
    ok = status_raw in {"PASS", "PLANNED"} and not failed_steps
    status, fails = status_tuple(ok, f"数据加速链路未通过: status={status_raw or 'UNKNOWN'}")
    if failed_steps:
        fails.append("失败步骤: " + ", ".join(failed_steps))
    return {
        "status": status,
        "fail_reasons": fails,
        "warn_reasons": warns,
        "pipeline_status": status_raw or "UNKNOWN",
        "step_count": len(steps),
        "failed_step_count": len(failed_steps),
        "failed_steps": failed_steps,
        "outputs": payload.get("outputs", {}),
    }


def assess_walkforward(
    path: Path,
    min_avg_split_sharpe: float = 0.0,
    min_traded_split_count: int = 0,
    min_total_trades: int = 0,
    min_trend_bucket_bars: int = 0,
    min_trend_bucket_trades: int = 0,
) -> Dict[str, Any]:
    payload = read_json(path)
    summary = payload.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    valid_split_count = summary.get("valid_split_count", 0)
    traded_split_count = summary.get("traded_split_count", 0)
    total_trades = summary.get("total_trades", 0)
    total_bars = summary.get("total_bars", 0)
    avg_split_sharpe = summary.get("avg_split_sharpe")
    regime_bucket_summary = summary.get("regime_bucket_summary", {})
    trend_bucket = regime_bucket_summary.get("trend", {}) if isinstance(regime_bucket_summary, dict) else {}
    fails: List[str] = []
    warns: List[str] = []
    if not (isinstance(valid_split_count, int) and valid_split_count > 0):
        fails.append("walk-forward 报告无有效 split")
    if not (isinstance(total_bars, int) and total_bars > 0):
        fails.append("walk-forward 报告无有效 bars")
    if (
        isinstance(traded_split_count, int)
        and traded_split_count < int(min_traded_split_count)
    ):
        fails.append(
            "walk-forward 交易活跃 split 数未达门槛: "
            f"{int(traded_split_count)} < {int(min_traded_split_count)}"
        )
    if isinstance(total_trades, int) and total_trades < int(min_total_trades):
        fails.append(
            "walk-forward 总交易次数未达门槛: "
            f"{int(total_trades)} < {int(min_total_trades)}"
        )
    trend_bars = int(trend_bucket.get("bars", 0)) if isinstance(trend_bucket, dict) else 0
    trend_trades = int(trend_bucket.get("trades", 0)) if isinstance(trend_bucket, dict) else 0
    if trend_bars >= int(min_trend_bucket_bars) and trend_trades < int(min_trend_bucket_trades):
        fails.append(
            "walk-forward TREND 桶交易次数未达门槛: "
            f"trend_trades={trend_trades} < {int(min_trend_bucket_trades)}, "
            f"trend_bars={trend_bars}, required_bars>={int(min_trend_bucket_bars)}"
        )
    if isinstance(avg_split_sharpe, (int, float)):
        if avg_split_sharpe < min_avg_split_sharpe:
            fails.append(
                "walk-forward 平均 Sharpe 未达门槛: "
                f"{float(avg_split_sharpe):.6f} < {float(min_avg_split_sharpe):.6f}"
            )
    else:
        warns.append("walk-forward 缺少 avg_split_sharpe，无法评估收益质量")
    status = "pass" if not fails else "fail"
    return {
        "status": status,
        "fail_reasons": fails,
        "warn_reasons": warns,
        "rows": payload.get("rows"),
        "summary": summary,
    }


def assess_trend_validation(
    path: Path,
    min_trend_bucket_sharpe: float = 0.0,
    min_trend_bucket_bars: int = 0,
    min_trend_bucket_trades: int = 0,
) -> Dict[str, Any]:
    payload = read_json(path)
    summary = payload.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    regime_bucket_summary = summary.get("regime_bucket_summary", {})
    trend_bucket = (
        regime_bucket_summary.get("trend", {})
        if isinstance(regime_bucket_summary, dict)
        else {}
    )
    if not isinstance(trend_bucket, dict):
        trend_bucket = {}

    trend_bars = int(trend_bucket.get("bars", 0) or 0)
    trend_trades = int(trend_bucket.get("trades", 0) or 0)
    trend_sharpe = trend_bucket.get("sharpe")
    warns: List[str] = []
    fails: List[str] = []

    if trend_bars < int(min_trend_bucket_bars):
        warns.append(
            "trend-validation TREND 桶 bars 未达建议门槛: "
            f"{trend_bars} < {int(min_trend_bucket_bars)}"
        )
    else:
        if trend_trades < int(min_trend_bucket_trades):
            fails.append(
                "trend-validation TREND 桶交易次数未达门槛: "
                f"{trend_trades} < {int(min_trend_bucket_trades)}"
            )
        if not isinstance(trend_sharpe, (int, float)):
            fails.append("trend-validation 缺少 TREND 桶 Sharpe")
        elif float(trend_sharpe) < float(min_trend_bucket_sharpe):
            fails.append(
                "trend-validation TREND 桶 Sharpe 未达门槛: "
                f"{float(trend_sharpe):.6f} < {float(min_trend_bucket_sharpe):.6f}"
            )

    status = "pass" if not fails else "fail"
    readiness_status = (
        "NOT_EVALUATED" if trend_bars < int(min_trend_bucket_bars) else status.upper()
    )
    return {
        "status": status,
        "fail_reasons": fails,
        "warn_reasons": warns,
        "readiness_status": readiness_status,
        "summary": {
            "bars": trend_bars,
            "trades": trend_trades,
            "sharpe": trend_sharpe,
            "avg_bar_return": trend_bucket.get("avg_bar_return"),
            "avg_turnover": trend_bucket.get("avg_turnover"),
            "splits": trend_bucket.get("splits"),
            "traded_splits": trend_bucket.get("traded_splits"),
        },
        "thresholds": {
            "min_trend_bucket_sharpe": float(min_trend_bucket_sharpe),
            "min_trend_bucket_bars": int(min_trend_bucket_bars),
            "min_trend_bucket_trades": int(min_trend_bucket_trades),
        },
    }


def assess_replay_validation(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    aggregate_validation = payload.get("aggregate_validation", {})
    if not isinstance(aggregate_validation, dict):
        aggregate_validation = {}
    aggregate_summary = payload.get("aggregate_summary", {})
    if not isinstance(aggregate_summary, dict):
        aggregate_summary = {}
    selection = payload.get("selection", {})
    if not isinstance(selection, dict):
        selection = {}

    status_raw = str(aggregate_validation.get("status", "")).lower()
    if status_raw == "pass_with_actions":
        status = "pass"
    elif status_raw in {"pass", "fail"}:
        status = status_raw
    else:
        status = "fail"

    fail_reasons = [str(item) for item in aggregate_validation.get("fail_reasons", [])]
    warn_reasons = [str(item) for item in aggregate_validation.get("warn_reasons", [])]
    for item in payload.get("warnings", []):
        item_text = str(item)
        if item_text and item_text not in warn_reasons:
            warn_reasons.append(item_text)
    if status_raw == "pass_with_actions":
        extra = "replay-validation 为 PASS_WITH_ACTIONS，建议继续优化趋势 execution / cost filter"
        if extra not in warn_reasons:
            warn_reasons.append(extra)
    if status_raw not in {"pass", "pass_with_actions", "fail"}:
        fail_reasons.append("replay-validation 缺少 aggregate_validation.status")

    return {
        "status": status,
        "fail_reasons": fail_reasons if status == "fail" else [],
        "warn_reasons": warn_reasons,
        "readiness_status": str(status_raw or "unknown").upper(),
        "target_bucket": payload.get("target_bucket"),
        "symbol": payload.get("symbol"),
        "selection": selection,
        "summary": aggregate_summary,
        "aggregate_summary": aggregate_summary,
        "aggregate_validation": aggregate_validation,
    }


def parse_section_names(raw: str) -> List[str]:
    if not raw:
        return list(INHERITABLE_SECTION_NAMES)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    unique: List[str] = []
    for item in values:
        if item not in unique:
            unique.append(item)
    return unique


def inherit_sections(
    sections: Dict[str, Dict[str, Any]],
    inherit_report_path: Path,
    inherit_section_names: List[str],
) -> Tuple[List[str], str]:
    if not inherit_report_path.is_file():
        return [], f"inherit report not found: {inherit_report_path}"

    try:
        payload = read_json(inherit_report_path)
    except Exception as exc:  # pragma: no cover - defensive guard
        return [], f"failed to read inherit report: {inherit_report_path}: {exc}"

    report_sections = payload.get("sections")
    if not isinstance(report_sections, dict):
        return [], f"inherit report has no sections object: {inherit_report_path}"

    inherited: List[str] = []
    for name in inherit_section_names:
        if name in sections:
            continue
        candidate = report_sections.get(name)
        if not isinstance(candidate, dict):
            continue
        sections[name] = candidate
        inherited.append(name)
    return inherited, ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成闭环汇总报告")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    parser.add_argument("--pipeline_name", default="ai-trade-closed-loop", help="流水线名称")
    parser.add_argument("--run_id", default="", help="可选：闭环运行 ID")
    parser.add_argument("--miner_report", default="", help="miner_report.json 路径")
    parser.add_argument("--baseline_report", default="", help="baseline_report.json 路径")
    parser.add_argument("--data_quality_report", default="", help="data_quality_report.json 路径")
    parser.add_argument("--integrator_report", default="", help="integrator_report.json 路径")
    parser.add_argument("--registry_report", default="", help="model_registry 结果 JSON 路径")
    parser.add_argument("--runtime_assess_report", default="", help="assess_run_log 输出 JSON 路径")
    parser.add_argument("--data_pipeline_report", default="", help="data_pipeline_report.json 路径")
    parser.add_argument("--walkforward_report", default="", help="walkforward_report.json 路径")
    parser.add_argument(
        "--replay_validation_report",
        default="",
        help="replay_validation_report.json 路径",
    )
    parser.add_argument(
        "--walkforward_min_avg_sharpe",
        type=float,
        default=0.0,
        help="walk-forward 平均 Sharpe 最低门槛（默认 0.0，低于即 FAIL）",
    )
    parser.add_argument(
        "--walkforward_min_traded_split_count",
        type=int,
        default=0,
        help="walk-forward 最小交易活跃 split 数（默认 0）",
    )
    parser.add_argument(
        "--walkforward_min_total_trades",
        type=int,
        default=0,
        help="walk-forward 最小总交易次数（默认 0）",
    )
    parser.add_argument(
        "--walkforward_min_trend_bucket_bars",
        type=int,
        default=0,
        help="walk-forward TREND 桶最小 bars 门槛（达到后开始要求最小交易数）",
    )
    parser.add_argument(
        "--walkforward_min_trend_bucket_trades",
        type=int,
        default=0,
        help="walk-forward TREND 桶最小交易次数（默认 0）",
    )
    parser.add_argument(
        "--trend_validation_min_sharpe",
        type=float,
        default=0.0,
        help="trend-validation TREND 桶 Sharpe 最低门槛（默认 0.0）",
    )
    parser.add_argument(
        "--trend_validation_min_bars",
        type=int,
        default=0,
        help="trend-validation TREND 桶最小 bars 门槛（默认 0）",
    )
    parser.add_argument(
        "--trend_validation_min_trades",
        type=int,
        default=0,
        help="trend-validation TREND 桶最小交易次数（默认 0）",
    )
    parser.add_argument(
        "--inherit_report",
        default="",
        help="可选：从历史 closed_loop_report 继承缺失 sections（默认仅补离线段）",
    )
    parser.add_argument(
        "--inherit_sections",
        default="",
        help=(
            "可选：继承 section 名称，逗号分隔；默认 "
            + ",".join(INHERITABLE_SECTION_NAMES)
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sections: Dict[str, Dict[str, Any]] = {}
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []

    if args.miner_report:
        miner_path = Path(args.miner_report)
        if miner_path.is_file():
            sections["miner"] = assess_miner(miner_path)
        else:
            sections["miner"] = {"status": "fail", "fail_reasons": [f"文件不存在: {miner_path}"]}

    if args.baseline_report:
        baseline_path = Path(args.baseline_report)
        if baseline_path.is_file():
            sections["baseline"] = assess_baseline(baseline_path)
        else:
            sections["baseline"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {baseline_path}"],
            }

    if args.data_quality_report:
        dq_path = Path(args.data_quality_report)
        if dq_path.is_file():
            sections["data_quality"] = assess_data_quality(dq_path)
        else:
            sections["data_quality"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {dq_path}"],
            }

    if args.integrator_report:
        integrator_path = Path(args.integrator_report)
        if integrator_path.is_file():
            sections["integrator"] = assess_integrator(integrator_path)
        else:
            sections["integrator"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {integrator_path}"],
            }

    if args.registry_report:
        registry_path = Path(args.registry_report)
        if registry_path.is_file():
            sections["registry"] = assess_registry(registry_path)
        else:
            sections["registry"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {registry_path}"],
            }

    if args.runtime_assess_report:
        runtime_path = Path(args.runtime_assess_report)
        if runtime_path.is_file():
            sections["runtime"] = assess_runtime(runtime_path)
        else:
            sections["runtime"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {runtime_path}"],
            }

    if args.data_pipeline_report:
        data_pipeline_path = Path(args.data_pipeline_report)
        if data_pipeline_path.is_file():
            sections["data_pipeline"] = assess_data_pipeline(data_pipeline_path)
        else:
            sections["data_pipeline"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {data_pipeline_path}"],
            }

    if args.walkforward_report:
        walkforward_path = Path(args.walkforward_report)
        if walkforward_path.is_file():
            sections["walkforward"] = assess_walkforward(
                walkforward_path,
                min_avg_split_sharpe=float(args.walkforward_min_avg_sharpe),
                min_traded_split_count=int(args.walkforward_min_traded_split_count),
                min_total_trades=int(args.walkforward_min_total_trades),
                min_trend_bucket_bars=int(args.walkforward_min_trend_bucket_bars),
                min_trend_bucket_trades=int(args.walkforward_min_trend_bucket_trades),
            )
            sections["trend_validation"] = assess_trend_validation(
                walkforward_path,
                min_trend_bucket_sharpe=float(args.trend_validation_min_sharpe),
                min_trend_bucket_bars=int(args.trend_validation_min_bars),
                min_trend_bucket_trades=int(args.trend_validation_min_trades),
            )
        else:
            sections["walkforward"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {walkforward_path}"],
            }
            sections["trend_validation"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {walkforward_path}"],
            }

    if args.replay_validation_report:
        replay_path = Path(args.replay_validation_report)
        if replay_path.is_file():
            sections["replay_validation"] = assess_replay_validation(replay_path)
        else:
            sections["replay_validation"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {replay_path}"],
            }

    inherited_sections: List[str] = []
    inherit_status = ""
    inherit_source_report = ""
    if args.inherit_report:
        inherit_path = Path(args.inherit_report)
        if inherit_path.resolve() != Path(args.output).resolve():
            inherit_source_report = str(inherit_path)
            inherited_sections, inherit_status = inherit_sections(
                sections=sections,
                inherit_report_path=inherit_path,
                inherit_section_names=parse_section_names(args.inherit_sections),
            )
        else:
            inherit_status = "inherit report equals output path, skip"

    for section_name, section in sections.items():
        if section.get("status") == "fail":
            for item in section.get("fail_reasons", []):
                fail_reasons.append(f"{section_name}: {item}")
        for item in section.get("warn_reasons", []):
            warn_reasons.append(f"{section_name}: {item}")

    account_outcome: Dict[str, Any] = {}
    runtime_section = sections.get("runtime", {})
    runtime_verdict = None
    runtime_validation_mode = None
    runtime_health_status = "NOT_EVALUATED"
    if isinstance(runtime_section, dict):
        runtime_verdict = runtime_section.get("verdict")
        runtime_validation_mode = runtime_section.get("runtime_validation_mode")
        runtime_health_status = str(runtime_verdict or runtime_section.get("status", "unknown")).upper()
        runtime_account_pnl = runtime_section.get("account_pnl", {})
        if isinstance(runtime_account_pnl, dict):
            account_outcome = {
                "first_sample_utc": runtime_account_pnl.get("first_sample_utc"),
                "last_sample_utc": runtime_account_pnl.get("last_sample_utc"),
                "first_equity_usd": runtime_account_pnl.get("first_equity_usd"),
                "last_equity_usd": runtime_account_pnl.get("last_equity_usd"),
                "equity_change_usd": runtime_account_pnl.get("equity_change_usd"),
                "equity_change_pct": runtime_account_pnl.get("equity_change_pct"),
                "day_start_equity_usd": runtime_account_pnl.get("day_start_equity_usd"),
                "equity_change_vs_day_start_usd": runtime_account_pnl.get(
                    "equity_change_vs_day_start_usd"
                ),
                "equity_change_vs_day_start_pct": runtime_account_pnl.get(
                    "equity_change_vs_day_start_pct"
                ),
                "max_equity_usd_observed": runtime_account_pnl.get(
                    "max_equity_usd_observed"
                ),
                "peak_to_last_drawdown_pct": runtime_account_pnl.get(
                    "peak_to_last_drawdown_pct"
                ),
                "max_drawdown_pct_observed": runtime_account_pnl.get(
                    "max_drawdown_pct_observed"
                ),
                "max_abs_notional_usd_observed": runtime_account_pnl.get(
                    "max_abs_notional_usd_observed"
                ),
                "samples": runtime_account_pnl.get("samples"),
                "fee_samples": runtime_account_pnl.get("fee_samples"),
                "first_realized_pnl_usd": runtime_account_pnl.get(
                    "first_realized_pnl_usd"
                ),
                "last_realized_pnl_usd": runtime_account_pnl.get(
                    "last_realized_pnl_usd"
                ),
                "realized_pnl_change_usd": runtime_account_pnl.get(
                    "realized_pnl_change_usd"
                ),
                "first_fee_usd": runtime_account_pnl.get("first_fee_usd"),
                "last_fee_usd": runtime_account_pnl.get("last_fee_usd"),
                "fee_change_usd": runtime_account_pnl.get("fee_change_usd"),
                "first_realized_net_pnl_usd": runtime_account_pnl.get(
                    "first_realized_net_pnl_usd"
                ),
                "last_realized_net_pnl_usd": runtime_account_pnl.get(
                    "last_realized_net_pnl_usd"
                ),
                "realized_net_pnl_change_usd": runtime_account_pnl.get(
                    "realized_net_pnl_change_usd"
                ),
            }

    registry_section = sections.get("registry", {})
    promotion_readiness_status = "NOT_EVALUATED"
    if isinstance(registry_section, dict) and registry_section:
        if registry_section.get("status") == "fail":
            promotion_readiness_status = "FAIL"
        elif bool(registry_section.get("gate_pass")) and bool(
            registry_section.get("activated")
        ):
            promotion_readiness_status = "PASS"
        elif bool(registry_section.get("gate_pass")):
            promotion_readiness_status = "PASS_NOT_ACTIVATED"
        else:
            promotion_readiness_status = "FAIL"

    trend_validation_section = sections.get("trend_validation", {})
    trend_readiness_status = "NOT_EVALUATED"
    if isinstance(trend_validation_section, dict) and trend_validation_section:
        trend_readiness_status = str(
            trend_validation_section.get(
                "readiness_status", trend_validation_section.get("status", "unknown")
            )
        ).upper()

    replay_validation_section = sections.get("replay_validation", {})
    replay_readiness_status = "NOT_EVALUATED"
    if isinstance(replay_validation_section, dict) and replay_validation_section:
        replay_readiness_status = str(
            replay_validation_section.get(
                "readiness_status", replay_validation_section.get("status", "unknown")
            )
        ).upper()

    overall_status = "PASS"
    if fail_reasons:
        overall_status = "FAIL"
    elif warn_reasons:
        overall_status = "PASS_WITH_ACTIONS"

    report = {
        "run_id": args.run_id or None,
        "pipeline_name": args.pipeline_name,
        "generated_at_utc": now_utc_iso(),
        "overall_status": overall_status,
        "runtime_verdict": runtime_verdict,
        "runtime_validation_mode": runtime_validation_mode,
        "runtime_health_status": runtime_health_status,
        "promotion_readiness_status": promotion_readiness_status,
        "trend_readiness_status": trend_readiness_status,
        "replay_readiness_status": replay_readiness_status,
        "account_outcome": account_outcome,
        "sections": sections,
        "inherit": {
            "source_report": inherit_source_report,
            "status": inherit_status or "ok",
            "inherited_sections": inherited_sections,
        },
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"CLOSED_LOOP_REPORT: {out_path}")
    print(f"OVERALL_STATUS: {overall_status}")
    if account_outcome:
        print("ACCOUNT_OUTCOME:")
        for key, value in account_outcome.items():
            print(f"  - {key}: {value}")
    if fail_reasons:
        print("FAIL_REASONS:")
        for item in fail_reasons:
            print(f"  - {item}")
    if warn_reasons:
        print("WARN_REASONS:")
        for item in warn_reasons:
            print(f"  - {item}")

    return 0 if overall_status in {"PASS", "PASS_WITH_ACTIONS"} else 1


if __name__ == "__main__":
    sys.exit(main())
