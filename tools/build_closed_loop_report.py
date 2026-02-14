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
    warnings: List[str] = []
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
    }


def assess_runtime(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    verdict = str(payload.get("verdict", "FAIL"))
    ok = verdict in {"PASS", "PASS_WITH_ACTIONS"}
    status, fails = status_tuple(ok, f"运行验收未通过: verdict={verdict}")
    warns: List[str] = []
    if verdict == "PASS_WITH_ACTIONS":
        warns.append("运行验收为 PASS_WITH_ACTIONS，建议执行告警项整改")
    return {
        "status": status,
        "fail_reasons": fails,
        "warn_reasons": warns,
        "stage": payload.get("stage"),
        "verdict": verdict,
        "metrics": payload.get("metrics", {}),
        "account_pnl": payload.get("account_pnl", {}),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成闭环汇总报告")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    parser.add_argument("--pipeline_name", default="ai-trade-closed-loop", help="流水线名称")
    parser.add_argument("--miner_report", default="", help="miner_report.json 路径")
    parser.add_argument("--baseline_report", default="", help="baseline_report.json 路径")
    parser.add_argument("--data_quality_report", default="", help="data_quality_report.json 路径")
    parser.add_argument("--integrator_report", default="", help="integrator_report.json 路径")
    parser.add_argument("--registry_report", default="", help="model_registry 结果 JSON 路径")
    parser.add_argument("--runtime_assess_report", default="", help="assess_run_log 输出 JSON 路径")
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

    for section_name, section in sections.items():
        if section.get("status") == "fail":
            for item in section.get("fail_reasons", []):
                fail_reasons.append(f"{section_name}: {item}")
        for item in section.get("warn_reasons", []):
            warn_reasons.append(f"{section_name}: {item}")

    account_outcome: Dict[str, Any] = {}
    runtime_section = sections.get("runtime", {})
    if isinstance(runtime_section, dict):
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

    overall_status = "PASS"
    if fail_reasons:
        overall_status = "FAIL"
    elif warn_reasons:
        overall_status = "PASS_WITH_ACTIONS"

    report = {
        "pipeline_name": args.pipeline_name,
        "generated_at_utc": now_utc_iso(),
        "overall_status": overall_status,
        "account_outcome": account_outcome,
        "sections": sections,
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
