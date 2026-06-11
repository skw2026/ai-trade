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
    "strategy_diagnose",
]

# A section inherited from the previous report is useful context, but some
# sections are stale gates rather than current-run evidence. In assess runs the
# registry is not re-run; carrying its old fail reasons into the new top-level
# status makes fresh replay/runtime evidence look like it failed for old data.
INHERITED_SECTIONS_EXCLUDED_FROM_CURRENT_GATE = {"registry"}

CANARY_MIN_REPLAY_TOTAL_FILLS = 20
CANARY_MIN_POSITIVE_FILLED_SEGMENT_RATIO = 0.55
EXIT_CAPTURE_MIN_SAMPLES = 10
EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE = 0.10


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


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
    data = payload.get("data", {})
    feature_transform = payload.get("feature_transform", {})
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
        "predict_horizon_bars": data.get("predict_horizon_bars") if isinstance(data, dict) else None,
        "label_policy": data.get("label_policy") if isinstance(data, dict) else None,
        "feature_transform": {
            "feature_clipping_enabled": feature_transform.get("feature_clipping_enabled"),
            "feature_normalization_enabled": feature_transform.get(
                "feature_normalization_enabled"
            ),
            "clip_quantile": feature_transform.get("clip_quantile"),
            "normalization_method": feature_transform.get("normalization_method"),
            "normalization_max_abs": feature_transform.get("normalization_max_abs"),
            "enabled_clip_bound_count": feature_transform.get("enabled_clip_bound_count"),
            "enabled_normalization_count": feature_transform.get(
                "enabled_normalization_count"
            ),
            "clip_bound_count": len(feature_transform.get("clip_bounds", []))
            if isinstance(feature_transform.get("clip_bounds"), list)
            else 0,
        }
        if isinstance(feature_transform, dict)
        else None,
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
    gate_fail_reasons = [str(item) for item in gate.get("fail_reasons", []) if str(item)]
    if not gate_pass:
        for item in gate_fail_reasons:
            if item not in fails:
                fails.append(item)
    return {
        "status": status,
        "fail_reasons": fails,
        "warn_reasons": warnings,
        "entry_id": payload.get("entry_id"),
        "model_version": payload.get("model_version"),
        "gate_pass": gate_pass,
        "activated": activated,
        "gate_fail_reasons": gate_fail_reasons,
        "gate_warn_reasons": gate.get("warn_reasons", []),
        "gate_metric_summary": gate.get("metric_summary", {}),
        "gate_external": gate.get("external", {}),
        "gate_thresholds": {
            "min_auc_mean": gate.get("min_auc_mean"),
            "min_delta_auc_vs_baseline": gate.get("min_delta_auc_vs_baseline"),
            "min_split_trained_count": gate.get("min_split_trained_count"),
            "min_split_trained_ratio": gate.get("min_split_trained_ratio"),
            "require_walkforward_positive": gate.get("require_walkforward_positive"),
            "min_walkforward_avg_split_return": gate.get(
                "min_walkforward_avg_split_return"
            ),
            "min_walkforward_enabled_avg_split_return": gate.get(
                "min_walkforward_enabled_avg_split_return"
            ),
            "min_walkforward_traded_avg_split_return": gate.get(
                "min_walkforward_traded_avg_split_return"
            ),
            "require_replay_validation_pass": gate.get("require_replay_validation_pass"),
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
        if market_context_status == "TREND_TRANSIENT":
            extra = "运行保护通过，但当前窗口仅出现短暂 TREND 样本，执行质量未完成稳定趋势验证"
        elif market_context_status == "TREND_CANDIDATE":
            extra = "运行保护通过，但当前窗口仅出现 TREND_CANDIDATE，执行质量仍需等待确认趋势样本"
        elif market_context_status in {"RANGE_ONLY", "EXTREME_ONLY", "RANGE_EXTREME_ONLY"}:
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
        "execution_attribution": payload.get("execution_attribution", {}),
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
    min_avg_split_return: float = 0.0,
    min_enabled_avg_split_return: float = 0.0,
    min_traded_avg_split_return: float = 0.0,
    min_traded_split_count: int = 0,
    min_total_trades: int = 0,
    min_trend_bucket_bars: int = 0,
    min_trend_bucket_trades: int = 0,
    focus_bucket: str = "",
    min_focus_bucket_bars: int = 0,
    min_focus_bucket_trades: int = 0,
    min_focus_bucket_sharpe: float = 0.0,
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
    avg_split_return = summary.get("avg_split_return")
    enabled_avg_split_return = summary.get("enabled_avg_split_return")
    traded_avg_split_return = summary.get("traded_avg_split_return")
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
    focus_bucket_name = str(focus_bucket or "").strip().lower()
    focus_bucket_pass = False
    focus_validation: Dict[str, Any] = {}
    if focus_bucket_name:
        focus_payload = (
            regime_bucket_summary.get(focus_bucket_name, {})
            if isinstance(regime_bucket_summary, dict)
            else {}
        )
        if not isinstance(focus_payload, dict):
            focus_payload = {}
        focus_bars = int(focus_payload.get("bars", 0) or 0)
        focus_trades = int(focus_payload.get("trades", 0) or 0)
        focus_sharpe = focus_payload.get("sharpe")
        focus_fail_reasons: List[str] = []
        if focus_bars < int(min_focus_bucket_bars):
            focus_fail_reasons.append(
                f"{focus_bucket_name} bucket bars={focus_bars} < {int(min_focus_bucket_bars)}"
            )
        if focus_trades < int(min_focus_bucket_trades):
            focus_fail_reasons.append(
                f"{focus_bucket_name} bucket trades={focus_trades} < {int(min_focus_bucket_trades)}"
            )
        if not isinstance(focus_sharpe, (int, float)):
            focus_fail_reasons.append(f"{focus_bucket_name} bucket sharpe missing")
        elif float(focus_sharpe) < float(min_focus_bucket_sharpe):
            focus_fail_reasons.append(
                f"{focus_bucket_name} bucket sharpe={float(focus_sharpe):.6f} < {float(min_focus_bucket_sharpe):.6f}"
            )
        focus_bucket_pass = not focus_fail_reasons
        focus_validation = {
            "bucket": focus_bucket_name,
            "status": "pass" if focus_bucket_pass else "fail",
            "fail_reasons": focus_fail_reasons,
            "bars": focus_bars,
            "trades": focus_trades,
            "sharpe": focus_sharpe,
            "thresholds": {
                "min_bars": int(min_focus_bucket_bars),
                "min_trades": int(min_focus_bucket_trades),
                "min_sharpe": float(min_focus_bucket_sharpe),
            },
        }
        if not focus_bucket_pass:
            fails.extend(focus_fail_reasons)
    if trend_bars >= int(min_trend_bucket_bars) and trend_trades < int(min_trend_bucket_trades):
        fails.append(
            "walk-forward TREND 桶交易次数未达门槛: "
            f"trend_trades={trend_trades} < {int(min_trend_bucket_trades)}, "
            f"trend_bars={trend_bars}, required_bars>={int(min_trend_bucket_bars)}"
        )
    if isinstance(avg_split_sharpe, (int, float)):
        if avg_split_sharpe < min_avg_split_sharpe:
            reason = (
                "walk-forward 平均 Sharpe 未达门槛: "
                f"{float(avg_split_sharpe):.6f} < {float(min_avg_split_sharpe):.6f}"
            )
            fails.append(reason)
    else:
        warns.append("walk-forward 缺少 avg_split_sharpe，无法评估收益质量")
    return_checks = [
        ("avg_split_return", "平均 split 收益", avg_split_return, min_avg_split_return),
        (
            "enabled_avg_split_return",
            "启用 split 平均收益",
            enabled_avg_split_return,
            min_enabled_avg_split_return,
        ),
        (
            "traded_avg_split_return",
            "交易 split 平均收益",
            traded_avg_split_return,
            min_traded_avg_split_return,
        ),
    ]
    for metric_name, label, metric_value, threshold in return_checks:
        if isinstance(metric_value, (int, float)):
            if float(metric_value) < float(threshold):
                reason = (
                    f"walk-forward {label}未达门槛: "
                    f"{float(metric_value):.6f} < {float(threshold):.6f}"
                )
                fails.append(reason)
        elif float(threshold) != 0.0:
            warns.append(f"walk-forward 缺少 {metric_name}，无法评估净收益质量")
    status = "pass" if not fails else "fail"
    return {
        "status": status,
        "fail_reasons": fails,
        "warn_reasons": warns,
        "rows": payload.get("rows"),
        "summary": summary,
        "focus_bucket_validation": focus_validation,
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
    feature_build = payload.get("feature_build", {})
    if not isinstance(feature_build, dict):
        feature_build = {}
    execution_optimizer = payload.get("execution_optimizer", {})
    if not isinstance(execution_optimizer, dict):
        execution_optimizer = {}
    cost_sensitivity = payload.get("cost_sensitivity", {})
    if not isinstance(cost_sensitivity, dict):
        cost_sensitivity = {}
    exit_capture = payload.get("exit_capture", {})
    if not isinstance(exit_capture, dict):
        exit_capture = {}
    execution_cost_plan = payload.get("execution_cost_plan", {})
    if not isinstance(execution_cost_plan, dict):
        execution_cost_plan = {}
    activation_gate = payload.get("activation_gate", {})
    if not isinstance(activation_gate, dict):
        activation_gate = {}

    status_raw = str(aggregate_validation.get("status", "")).lower()
    top_status_raw = str(payload.get("status", "")).strip().lower()
    if status_raw == "pass_with_actions":
        status = "pass"
    elif status_raw in {"pass", "fail"}:
        status = status_raw
    else:
        status = "fail"

    raw_aggregate_fail_reasons = [
        str(item)
        for item in aggregate_validation.get("fail_reasons", [])
        if str(item).strip()
    ]
    skip_reason = str(
        payload.get("skip_reason") or selection.get("stop_reason") or ""
    ).strip()
    selection_mode = str(selection.get("selection_mode", "")).strip()
    validation_skipped = bool(payload.get("validation_skipped")) or (
        selection_mode == "not_run"
        and skip_reason in {"feature_store_missing", "command_failed", "not_run"}
    )
    if validation_skipped:
        raw_aggregate_fail_reasons.append(
            "replay-validation skipped/not_run: "
            f"reason={skip_reason or 'unknown'}"
        )
    if status_raw == "fail" and not raw_aggregate_fail_reasons:
        raw_aggregate_fail_reasons.append("replay-validation aggregate_validation.status=fail")
    if top_status_raw and top_status_raw not in {"pass", "pass_with_actions"}:
        raw_aggregate_fail_reasons.append(
            f"replay-validation status={top_status_raw} != pass"
        )
    fail_reasons: List[str] = []
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
        raw_aggregate_fail_reasons.append(
            "replay-validation 缺少 aggregate_validation.status"
        )
    optimizer_status = str(execution_optimizer.get("status", "")).lower()
    if optimizer_status == "fail":
        status = "fail"
        fail_reasons.append("replay execution_optimizer status=fail")
    cost_plan_status = str(execution_cost_plan.get("status", "")).lower()
    if cost_plan_status == "fail":
        status = "fail"
        fail_reasons.append("replay execution_cost_plan status=fail")
    elif cost_plan_status == "candidate_requires_rerun":
        warn_reasons.append(
            "replay execution_cost_plan 仅找到需重跑验证的低成本执行候选，不能直接上线"
        )
    activation_gate_status = str(activation_gate.get("status", "")).lower()
    if activation_gate_status == "fail":
        for reason in activation_gate.get("fail_reasons", []):
            reason_text = str(reason).strip()
            if reason_text:
                fail_reasons.append(f"replay activation_gate: {reason_text}")
    elif activation_gate_status == "pass_with_actions":
        for reason in activation_gate.get("warn_reasons", []):
            reason_text = str(reason).strip()
            if reason_text:
                warn_reasons.append(f"replay activation_gate: {reason_text}")
    failed_feature_symbols = [
        str(item)
        for item in feature_build.get("failed_symbols", [])
        if str(item).strip()
    ]
    missing_feature_symbols = [
        str(item)
        for item in feature_build.get("missing_symbols", [])
        if str(item).strip()
    ]
    tradeability = aggregate_validation.get("symbol_tradeability", {})
    if not isinstance(tradeability, dict):
        tradeability = {}
    tradeability_status = str(tradeability.get("status", "")).strip().lower()
    tradable_symbols = {
        str(item).strip().upper()
        for item in tradeability.get("tradable_symbols", [])
        if str(item).strip()
    }
    quarantined_symbols = {
        str(item).strip().upper()
        for item in tradeability.get("quarantined_symbols", [])
        if str(item).strip()
    }
    decisions = tradeability.get("decisions", {})
    if isinstance(decisions, dict):
        for symbol, decision in decisions.items():
            if not isinstance(decision, dict):
                continue
            if str(decision.get("status", "")).strip().lower() == "quarantined":
                symbol_text = str(symbol).strip().upper()
                if symbol_text:
                    quarantined_symbols.add(symbol_text)
    source_symbol = str(payload.get("source_symbol", "")).strip().upper()
    source_quarantined = bool(source_symbol and source_symbol in quarantined_symbols)
    critical_feature_symbols = set(tradable_symbols)
    if source_symbol:
        critical_feature_symbols.add(source_symbol)
    failed_feature_set = {item.strip().upper() for item in failed_feature_symbols}
    missing_feature_set = {item.strip().upper() for item in missing_feature_symbols}
    critical_failed_features = sorted(critical_feature_symbols & failed_feature_set)
    critical_missing_features = sorted(critical_feature_symbols & missing_feature_set)
    if critical_failed_features:
        fail_reasons.append(
            "replay real-market feature 构建失败且命中 source/tradable symbols="
            + ",".join(critical_failed_features)
        )
    elif failed_feature_symbols:
        warn_reasons.append(
            "replay real-market feature 构建失败: symbols="
            + ",".join(sorted(set(failed_feature_symbols)))
        )
    if critical_missing_features:
        fail_reasons.append(
            "replay real-market feature 缺失且命中 source/tradable symbols="
            + ",".join(critical_missing_features)
        )
    elif missing_feature_symbols:
        warn_reasons.append(
            "replay real-market feature 缺失，已回退 source feature: symbols="
            + ",".join(sorted(set(missing_feature_symbols)))
        )
    if source_symbol and source_symbol in quarantined_symbols:
        fail_reasons.append(
            f"replay source_symbol={source_symbol} is quarantined by symbol_tradeability"
        )
    decision_schema_complete = True
    if tradeability and tradable_symbols:
        if not isinstance(decisions, dict):
            decisions = {}
            decision_schema_complete = False
            fail_reasons.append("replay symbol_tradeability.decisions missing")
        for symbol in sorted(tradable_symbols):
            decision = decisions.get(symbol, {})
            if not isinstance(decision, dict) or not decision:
                decision_schema_complete = False
                fail_reasons.append(
                    f"replay symbol_tradeability decision missing for {symbol}"
                )
                continue
            if as_float(decision.get("median_realized_net_per_fill_with_fills")) is None:
                decision_schema_complete = False
                fail_reasons.append(
                    "replay symbol_tradeability "
                    f"median_realized_net_per_fill_with_fills missing for {symbol}"
                )
            if as_float(decision.get("positive_filled_segment_ratio")) is None:
                decision_schema_complete = False
                fail_reasons.append(
                    "replay symbol_tradeability "
                    f"positive_filled_segment_ratio missing for {symbol}"
                )
            if as_int(decision.get("total_fills")) <= 0:
                decision_schema_complete = False
                fail_reasons.append(
                    f"replay symbol_tradeability total_fills missing for {symbol}"
                )
    suppressed_aggregate_fail_reasons: List[str] = []
    if (
        raw_aggregate_fail_reasons
        and tradeability_status == "pass"
        and not source_quarantined
        and tradable_symbols
        and decision_schema_complete
    ):
        suppressed_aggregate_fail_reasons = list(raw_aggregate_fail_reasons)
        warn_reasons.append(
            "replay aggregate fail reasons suppressed because symbol_tradeability passed: "
            + "; ".join(suppressed_aggregate_fail_reasons)
        )
    else:
        fail_reasons.extend(raw_aggregate_fail_reasons)
    if fail_reasons:
        status = "fail"
    else:
        status = "pass"
    readiness_status = "FAIL" if status == "fail" else (
        "PASS_WITH_ACTIONS" if warn_reasons else "PASS"
    )

    return {
        "status": status,
        "fail_reasons": fail_reasons if status == "fail" else [],
        "warn_reasons": warn_reasons,
        "readiness_status": readiness_status,
        "target_bucket": payload.get("target_bucket"),
        "source_symbol": payload.get("source_symbol"),
        "source_symbols": payload.get("source_symbols", {}),
        "source_symbol_matches_target": payload.get("source_symbol_matches_target"),
        "real_market_replay": payload.get("real_market_replay"),
        "per_symbol_source": payload.get("per_symbol_source", {}),
        "feature_build": feature_build,
        "feature_csv_by_symbol": payload.get("feature_csv_by_symbol", {}),
        "symbol": payload.get("symbol"),
        "symbols": payload.get("symbols", []),
        "selection": selection,
        "summary": aggregate_summary,
        "aggregate_summary": aggregate_summary,
        "aggregate_validation": aggregate_validation,
        "symbol_tradeability": tradeability,
        "suppressed_aggregate_fail_reasons": suppressed_aggregate_fail_reasons,
        "activation_gate": activation_gate,
        "execution_economics": payload.get("execution_economics", {}),
        "cost_sensitivity": cost_sensitivity,
        "exit_capture": exit_capture,
        "exit_capture_by_symbol": payload.get("exit_capture_by_symbol", {}),
        "execution_cost_plan": execution_cost_plan,
        "execution_optimizer": execution_optimizer,
    }


def assess_strategy_diagnose(path: Path) -> Dict[str, Any]:
    payload = read_json(path)
    status_raw = str(payload.get("status", "")).strip().lower()
    readiness_status = str(
        payload.get("readiness_status", status_raw.upper() if status_raw else "UNKNOWN")
    ).upper()
    fail_reasons = [
        str(item) for item in payload.get("fail_reasons", []) if str(item).strip()
    ]
    warn_reasons = [
        str(item) for item in payload.get("warn_reasons", []) if str(item).strip()
    ]

    if status_raw == "pass":
        status = "pass"
    elif status_raw in {"skipped", "insufficient_samples"}:
        status = "pass"
        if not warn_reasons:
            warn_reasons.append(f"strategy_diagnose status={status_raw}")
    elif status_raw in {"fail", "action_required"}:
        status = "fail"
        if not fail_reasons:
            fail_reasons.append(f"strategy_diagnose status={status_raw}")
    else:
        status = "fail"
        fail_reasons.append("strategy_diagnose missing/unknown status")

    return {
        "status": status,
        "readiness_status": readiness_status,
        "fail_reasons": fail_reasons if status == "fail" else [],
        "warn_reasons": warn_reasons,
        "diagnose_status": status_raw or "unknown",
        "target": payload.get("target", {}),
        "aggregate": payload.get("aggregate", {}),
        "by_symbol": payload.get("by_symbol", {}),
        "diagnostics": payload.get("diagnostics", []),
        "recommendations": payload.get("recommendations", []),
    }


def unique_symbols(raw_value: Any) -> List[str]:
    if raw_value is None:
        return []
    raw_items: List[Any]
    if isinstance(raw_value, list):
        raw_items = raw_value
    elif isinstance(raw_value, str):
        raw_items = raw_value.split(",")
    else:
        raw_items = [raw_value]
    symbols: List[str] = []
    for item in raw_items:
        symbol = str(item).strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def assess_replay_live_symbol_alignment(
    runtime_section: Dict[str, Any],
    replay_section: Dict[str, Any],
) -> Dict[str, Any]:
    if not runtime_section or not replay_section:
        return {
            "status": "pass",
            "readiness_status": "NOT_EVALUATED",
            "fail_reasons": [],
            "warn_reasons": [],
            "live_trend_symbols": [],
            "live_trend_candidate_symbols": [],
            "replay_symbols": [],
            "uncovered_live_trend_symbols": [],
            "uncovered_live_trend_candidate_symbols": [],
        }

    metrics = runtime_section.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}

    live_trend_symbols = unique_symbols(
        metrics.get("regime_change_trend_symbols", [])
    )
    live_candidate_symbols = unique_symbols(
        metrics.get("regime_change_trend_candidate_symbols", [])
    )

    replay_symbols = unique_symbols(replay_section.get("symbols"))
    replay_symbols.extend(
        symbol
        for symbol in unique_symbols(replay_section.get("symbol"))
        if symbol not in replay_symbols
    )

    uncovered_trend = [
        symbol for symbol in live_trend_symbols if symbol not in replay_symbols
    ]
    uncovered_candidates = [
        symbol
        for symbol in live_candidate_symbols
        if symbol not in replay_symbols and symbol not in uncovered_trend
    ]
    recommended_symbols: List[str] = []
    for symbol in replay_symbols + live_trend_symbols + live_candidate_symbols:
        if symbol and symbol not in recommended_symbols:
            recommended_symbols.append(symbol)
    missing_recommended_symbols = [
        symbol for symbol in recommended_symbols if symbol not in replay_symbols
    ]
    recommended_symbols_csv = ",".join(recommended_symbols)

    warn_reasons: List[str] = []
    if live_trend_symbols and replay_symbols and uncovered_trend:
        warn_reasons.append(
            "replay-validation 目标币对未覆盖 live TREND 符号: "
            f"replay={','.join(replay_symbols)}, "
            f"live_trend={','.join(live_trend_symbols)}；"
            "replay 结果不能代表本轮 live TREND 执行，应切换或扩展 replay 目标"
            + (
                f": recommended_replay_symbols={recommended_symbols_csv}"
                if recommended_symbols_csv
                else ""
            )
        )
    elif (
        not live_trend_symbols
        and live_candidate_symbols
        and replay_symbols
        and uncovered_candidates
    ):
        warn_reasons.append(
            "replay-validation 目标币对未覆盖 live TREND_CANDIDATE 符号: "
            f"replay={','.join(replay_symbols)}, "
            f"live_trend_candidate={','.join(live_candidate_symbols)}；"
            "若下一轮仍缺 live TREND，应优先用这些候选币对做 replay 验证"
            + (
                f": recommended_replay_symbols={recommended_symbols_csv}"
                if recommended_symbols_csv
                else ""
            )
        )

    readiness_status = "PASS" if not warn_reasons else "PASS_WITH_ACTIONS"
    if not replay_symbols or not (live_trend_symbols or live_candidate_symbols):
        readiness_status = "NOT_EVALUATED"

    return {
        "status": "pass",
        "readiness_status": readiness_status,
        "fail_reasons": [],
        "warn_reasons": warn_reasons,
        "target_bucket": replay_section.get("target_bucket"),
        "live_trend_symbols": live_trend_symbols,
        "live_trend_candidate_symbols": live_candidate_symbols,
        "replay_symbols": replay_symbols,
        "recommended_replay_symbols": recommended_symbols,
        "recommended_replay_symbols_csv": recommended_symbols_csv,
        "missing_recommended_replay_symbols": missing_recommended_symbols,
        "uncovered_live_trend_symbols": uncovered_trend,
        "uncovered_live_trend_candidate_symbols": uncovered_candidates,
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
        inherited_candidate = dict(candidate)
        inherited_candidate["_inherited_from_report"] = str(inherit_report_path)
        inherited_candidate["_current_run_gate"] = (
            name not in INHERITED_SECTIONS_EXCLUDED_FROM_CURRENT_GATE
        )
        sections[name] = inherited_candidate
        inherited.append(name)
    return inherited, ""


def assess_run_manifest(path: Path, expected_run_id: str) -> Dict[str, Any]:
    payload = read_json(path)
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    run_id = str(payload.get("run_id", "")).strip()
    if expected_run_id:
        if not run_id:
            fail_reasons.append(
                f"run manifest run_id missing; expected={expected_run_id}"
            )
        elif run_id != expected_run_id:
            fail_reasons.append(
                f"run manifest run_id mismatch: manifest={run_id}, report={expected_run_id}"
            )
    elif not run_id:
        warn_reasons.append("run manifest missing run_id")
    for key in ("git", "config_hashes", "replay_validation"):
        if key not in payload:
            warn_reasons.append(f"run manifest missing {key}")
    return {
        "status": "fail" if fail_reasons else "pass",
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "manifest": payload,
    }


def classify_runtime_validation(runtime_section: Dict[str, Any]) -> str:
    if not isinstance(runtime_section, dict) or not runtime_section:
        return "NOT_EVALUATED"
    verdict = str(runtime_section.get("verdict", "")).upper()
    execution_status = str(runtime_section.get("execution_status", "")).upper()
    mode = str(runtime_section.get("runtime_validation_mode", "")).upper()
    market_context = str(runtime_section.get("market_context_status", "")).upper()
    metrics = runtime_section.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    fills = max(
        as_int(metrics.get("funnel_fills_runtime_count")),
        as_int(metrics.get("trend_candidate_probe_fill_count")),
    )
    if verdict == "FAIL":
        return "RUNTIME_FAIL"
    if execution_status == "PASS" and fills > 0:
        return "EXECUTION_VALIDATED_WITH_FILLS"
    if execution_status == "NOT_EVALUATED" and (
        mode == "POLICY_FLAT_PROTECTION" or market_context in {"RANGE_ONLY", "EXTREME_ONLY", "RANGE_EXTREME_ONLY"}
    ):
        return "PROTECTION_PASS_NO_TRADE_VALIDATION"
    if verdict == "PASS_WITH_ACTIONS":
        return "RUNTIME_PASS_WITH_ACTIONS_NOT_TRADING_PROOF"
    if verdict == "PASS":
        return "RUNTIME_HEALTH_PASS"
    return "UNKNOWN"


def assess_feature_parity(runtime_section: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(runtime_section, dict) or not runtime_section:
        return {
            "status": "pass",
            "readiness_status": "NOT_EVALUATED",
            "fail_reasons": [],
            "warn_reasons": [],
        }
    metrics = runtime_section.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    feature_metric_keys = (
        "integrator_feature_sanitized_count",
        "integrator_feature_sanitized_by_feature",
        "integrator_feature_sanitized_by_symbol",
        "feature_nonfinite_count",
        "feature_large_abs_line_ratio",
        "feature_max_abs_by_feature",
    )
    if not any(key in metrics for key in feature_metric_keys):
        return {
            "status": "pass",
            "readiness_status": "NOT_EVALUATED",
            "fail_reasons": [],
            "warn_reasons": [],
        }
    sanitized_count = as_int(metrics.get("integrator_feature_sanitized_count"))
    nonfinite_count = as_int(metrics.get("feature_nonfinite_count"))
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    if sanitized_count > 0:
        fail_reasons.append(
            "live/replay feature parity failed: "
            f"integrator_feature_sanitized_count={sanitized_count} > 0"
        )
    if nonfinite_count > 0:
        fail_reasons.append(
            f"live feature stream has non-finite values: feature_nonfinite_count={nonfinite_count} > 0"
        )
    large_line_ratio = as_float(metrics.get("feature_large_abs_line_ratio"))
    if large_line_ratio is not None and large_line_ratio > 0.0:
        warn_reasons.append(
            f"live feature large-abs line ratio={large_line_ratio:.6f}; check miner/live normalization"
        )
    return {
        "status": "fail" if fail_reasons else "pass",
        "readiness_status": "FAIL" if fail_reasons else "PASS",
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "integrator_feature_sanitized_count": sanitized_count,
        "integrator_feature_sanitized_by_feature": metrics.get(
            "integrator_feature_sanitized_by_feature", {}
        ),
        "integrator_feature_sanitized_by_symbol": metrics.get(
            "integrator_feature_sanitized_by_symbol", {}
        ),
        "feature_nonfinite_count": nonfinite_count,
        "feature_large_abs_line_ratio": large_line_ratio,
        "feature_max_abs_by_feature": metrics.get("feature_max_abs_by_feature", {}),
    }


def assess_exit_capture(replay_section: Dict[str, Any], runtime_section: Dict[str, Any]) -> Dict[str, Any]:
    replay_exit = replay_section.get("exit_capture", {}) if isinstance(replay_section, dict) else {}
    if not isinstance(replay_exit, dict):
        replay_exit = {}
    replay_exit_by_symbol = (
        replay_section.get("exit_capture_by_symbol", {})
        if isinstance(replay_section, dict)
        else {}
    )
    if not isinstance(replay_exit_by_symbol, dict):
        replay_exit_by_symbol = {}
    tradeability = (
        replay_section.get("symbol_tradeability", {})
        if isinstance(replay_section, dict)
        else {}
    )
    if not isinstance(tradeability, dict):
        tradeability = {}
    critical_symbols = set(unique_symbols(tradeability.get("tradable_symbols", [])))
    source_symbol = (
        str(replay_section.get("source_symbol", "")).strip().upper()
        if isinstance(replay_section, dict)
        else ""
    )
    if source_symbol:
        critical_symbols.add(source_symbol)
    selected_symbol_exit: Dict[str, Dict[str, Any]] = {}
    for symbol in sorted(critical_symbols):
        item = replay_exit_by_symbol.get(symbol)
        if isinstance(item, dict):
            selected_symbol_exit[symbol] = item
    runtime_metrics = runtime_section.get("metrics", {}) if isinstance(runtime_section, dict) else {}
    if not isinstance(runtime_metrics, dict):
        runtime_metrics = {}
    live_exit_keys = (
        "exit_capture_sample_count",
        "exit_capture_low_ratio",
        "exit_capture_mean_captured_net_bps",
        "exit_capture_mean_capture_ratio",
    )
    has_exit_capture_data = bool(replay_exit) or any(key in runtime_metrics for key in live_exit_keys)

    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    replay_sample_count = as_int(replay_exit.get("sample_count"))
    primary_diagnosis = str(replay_exit.get("primary_diagnosis", "")).strip()
    low_capture_segments = as_int(replay_exit.get("low_capture_segment_count"))
    mean_capture = as_float(replay_exit.get("mean_gross_capture_of_path_mfe"))
    if selected_symbol_exit:
        replay_sample_count = sum(
            as_int(item.get("sample_count")) for item in selected_symbol_exit.values()
        )
        symbol_mean_captures = [
            as_float(item.get("mean_gross_capture_of_path_mfe"))
            for item in selected_symbol_exit.values()
        ]
        symbol_mean_captures = [
            item for item in symbol_mean_captures if item is not None
        ]
        if symbol_mean_captures:
            mean_capture = min(symbol_mean_captures)
        low_capture_segments = sum(
            as_int(item.get("low_capture_segment_count"))
            for item in selected_symbol_exit.values()
        )
        for symbol, item in selected_symbol_exit.items():
            symbol_samples = as_int(item.get("sample_count"))
            symbol_diagnosis = str(item.get("primary_diagnosis", "")).strip()
            symbol_mean_capture = as_float(
                item.get("mean_gross_capture_of_path_mfe")
            )
            if symbol_samples > 0 and symbol_diagnosis == "exit_capture_low":
                fail_reasons.append(
                    f"replay {symbol} exit_capture_low: path MFE covers cost but gross capture is too low"
                )
            if (
                symbol_samples > 0
                and symbol_mean_capture is not None
                and symbol_mean_capture < EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE
            ):
                fail_reasons.append(
                    f"replay {symbol} mean_gross_capture_of_path_mfe="
                    f"{symbol_mean_capture:.6f} < "
                    f"{EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE:.6f}"
                )
    if (
        not selected_symbol_exit
        and replay_sample_count > 0
        and primary_diagnosis == "exit_capture_low"
    ):
        fail_reasons.append(
            "replay exit_capture_low: path MFE covers cost but gross capture is too low"
        )
    if (
        not selected_symbol_exit
        and replay_sample_count > 0
        and mean_capture is not None
        and mean_capture < EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE
    ):
        fail_reasons.append(
            "replay mean_gross_capture_of_path_mfe="
            f"{mean_capture:.6f} < {EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE:.6f}"
        )

    runtime_exit_samples = as_int(runtime_metrics.get("exit_capture_sample_count"))
    runtime_low_ratio = as_float(runtime_metrics.get("exit_capture_low_ratio"))
    runtime_mean_net_bps = as_float(runtime_metrics.get("exit_capture_mean_captured_net_bps"))
    if runtime_exit_samples > 0:
        if runtime_low_ratio is not None and runtime_low_ratio > 0.50:
            fail_reasons.append(
                f"live exit_capture_low_ratio={runtime_low_ratio:.6f} > 0.500000"
            )
        if runtime_mean_net_bps is not None and runtime_mean_net_bps <= 0.0:
            fail_reasons.append(
                f"live exit_capture_mean_captured_net_bps={runtime_mean_net_bps:.6f} <= 0"
            )
    elif replay_sample_count == 0 and has_exit_capture_data:
        warn_reasons.append("exit_capture not evaluated: no replay/live filled samples")

    return {
        "status": "fail" if fail_reasons else "pass",
        "readiness_status": "FAIL"
        if fail_reasons
        else (
            "NOT_EVALUATED"
            if not has_exit_capture_data or (replay_sample_count == 0 and runtime_exit_samples == 0)
            else "PASS"
        ),
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "replay": {
            "sample_count": replay_sample_count,
            "primary_diagnosis": primary_diagnosis,
            "low_capture_segment_count": low_capture_segments,
            "mean_gross_capture_of_path_mfe": mean_capture,
            "mean_path_fee_coverage_ratio": replay_exit.get("mean_path_fee_coverage_ratio"),
            "median_path_fee_coverage_ratio": replay_exit.get("median_path_fee_coverage_ratio"),
            "selected_by_symbol": selected_symbol_exit,
        },
        "live": {
            "sample_count": runtime_exit_samples,
            "low_ratio": runtime_low_ratio,
            "mean_captured_net_bps": runtime_mean_net_bps,
            "mean_capture_ratio": runtime_metrics.get("exit_capture_mean_capture_ratio"),
        },
    }


def assess_canary_validation(
    replay_section: Dict[str, Any],
    runtime_section: Dict[str, Any],
) -> Dict[str, Any]:
    aggregate = replay_section.get("aggregate_summary", {}) if isinstance(replay_section, dict) else {}
    if not isinstance(aggregate, dict):
        aggregate = {}
    tradeability = replay_section.get("symbol_tradeability", {}) if isinstance(replay_section, dict) else {}
    if not isinstance(tradeability, dict):
        tradeability = {}
    has_tradeability = bool(tradeability)
    tradable_symbols = unique_symbols(tradeability.get("tradable_symbols", []))
    quarantined_symbols = unique_symbols(tradeability.get("quarantined_symbols", []))
    source_symbol = str(replay_section.get("source_symbol", "")).strip().upper() if isinstance(replay_section, dict) else ""

    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    if not replay_section:
        return {
            "status": "pass",
            "readiness_status": "NOT_EVALUATED",
            "validation_mode": "NOT_EVALUATED",
            "fail_reasons": [],
            "warn_reasons": [],
            "recommended_live_symbols": [],
            "quarantined_symbols": [],
            "source_symbol": source_symbol,
            "replay_thresholds": {
                "min_median_realized_net_per_fill_with_fills": 0.0,
                "min_positive_filled_segment_ratio": CANARY_MIN_POSITIVE_FILLED_SEGMENT_RATIO,
            },
            "replay_metrics": {},
            "live_thresholds": {
                "min_round_trips_before_promotion": 30,
                "fee_stress_multiplier_required": 1.25,
                "max_single_trade_profit_share": 0.30,
            },
            "live_metrics": {},
        }
    if not has_tradeability:
        return {
            "status": "pass",
            "readiness_status": "NOT_EVALUATED",
            "validation_mode": "NOT_EVALUATED",
            "fail_reasons": [],
            "warn_reasons": [],
            "recommended_live_symbols": [],
            "quarantined_symbols": quarantined_symbols,
            "source_symbol": source_symbol,
            "replay_thresholds": {
                "min_median_realized_net_per_fill_with_fills": 0.0,
                "min_positive_filled_segment_ratio": CANARY_MIN_POSITIVE_FILLED_SEGMENT_RATIO,
            },
            "replay_metrics": {
                "basis": "not_evaluated_missing_symbol_tradeability",
                "median_realized_net_per_fill_with_fills": as_float(
                    aggregate.get("median_realized_net_per_fill_with_fills")
                ),
                "positive_filled_segment_ratio": as_float(
                    aggregate.get("positive_filled_segment_ratio")
                ),
                "total_fills": aggregate.get("total_fills"),
            },
            "live_thresholds": {
                "min_round_trips_before_promotion": 30,
                "fee_stress_multiplier_required": 1.25,
                "max_single_trade_profit_share": 0.30,
            },
            "live_metrics": {},
        }
    if has_tradeability and not tradable_symbols:
        fail_reasons.append("canary has no replay tradable_symbols")
    if source_symbol and source_symbol in quarantined_symbols:
        fail_reasons.append(
            f"canary source_symbol={source_symbol} is quarantined; do not use it as live/source"
        )

    replay_metric_basis = "aggregate_summary"
    median_net = as_float(aggregate.get("median_realized_net_per_fill_with_fills"))
    positive_ratio = as_float(aggregate.get("positive_filled_segment_ratio"))
    total_fills: Any = aggregate.get("total_fills")
    decisions = tradeability.get("decisions", {}) if has_tradeability else {}
    if tradable_symbols:
        replay_metric_basis = "symbol_tradeability.tradable_symbols_min"
        median_net = None
        positive_ratio = None
        total_fills = 0
        tradable_medians: List[float] = []
        tradable_positive_ratios: List[float] = []
        tradable_total_fills = 0
        if not isinstance(decisions, dict):
            decisions = {}
            fail_reasons.append("canary symbol_tradeability.decisions missing")
        for symbol in tradable_symbols:
            decision = decisions.get(symbol, {})
            if not isinstance(decision, dict) or not decision:
                fail_reasons.append(
                    f"canary symbol_tradeability decision missing for {symbol}"
                )
                continue
            item_median = as_float(decision.get("median_realized_net_per_fill_with_fills"))
            item_positive_ratio = as_float(decision.get("positive_filled_segment_ratio"))
            if item_median is not None:
                tradable_medians.append(item_median)
            else:
                fail_reasons.append(
                    f"canary symbol_tradeability median_realized_net_per_fill_with_fills missing for {symbol}"
                )
            if item_positive_ratio is not None:
                tradable_positive_ratios.append(item_positive_ratio)
            else:
                fail_reasons.append(
                    f"canary symbol_tradeability positive_filled_segment_ratio missing for {symbol}"
                )
            tradable_total_fills += as_int(decision.get("total_fills"))
        if tradable_medians:
            median_net = min(tradable_medians)
        if tradable_positive_ratios:
            positive_ratio = min(tradable_positive_ratios)
        if tradable_total_fills > 0:
            total_fills = tradable_total_fills
    if median_net is not None and median_net <= 0.0:
        fail_reasons.append(
            f"canary replay {replay_metric_basis} median net per fill={median_net:.6f} <= 0"
        )
    if positive_ratio is not None and positive_ratio < CANARY_MIN_POSITIVE_FILLED_SEGMENT_RATIO:
        fail_reasons.append(
            f"canary replay {replay_metric_basis} positive filled segment ratio={positive_ratio:.6f} < {CANARY_MIN_POSITIVE_FILLED_SEGMENT_RATIO:.6f}"
        )

    runtime_metrics = runtime_section.get("metrics", {}) if isinstance(runtime_section, dict) else {}
    if not isinstance(runtime_metrics, dict):
        runtime_metrics = {}
    live_fills = max(
        as_int(runtime_metrics.get("funnel_fills_runtime_count")),
        as_int(runtime_metrics.get("trend_candidate_probe_fill_count")),
    )
    live_net = as_float(runtime_metrics.get("realized_net_per_fill"))
    if live_fills <= 0:
        warn_reasons.append(
            "canary live execution not evaluated: no live fills; do not treat policy-flat as trading success"
        )
    elif live_net is not None and live_net <= 0.0:
        fail_reasons.append(f"canary live realized_net_per_fill={live_net:.6f} <= 0")

    validation_mode = "NOT_EVALUATED"
    if len(tradable_symbols) == 1:
        validation_mode = "SINGLE_SYMBOL_CANARY"
    elif len(tradable_symbols) > 1:
        validation_mode = "MULTI_SYMBOL_CANARY"

    return {
        "status": "fail" if fail_reasons else "pass",
        "readiness_status": "FAIL" if fail_reasons else ("PASS_WITH_ACTIONS" if warn_reasons else "PASS"),
        "validation_mode": validation_mode,
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "recommended_live_symbols": tradable_symbols,
        "quarantined_symbols": quarantined_symbols,
        "source_symbol": source_symbol,
        "replay_thresholds": {
            "min_median_realized_net_per_fill_with_fills": 0.0,
            "min_positive_filled_segment_ratio": CANARY_MIN_POSITIVE_FILLED_SEGMENT_RATIO,
        },
        "replay_metrics": {
            "basis": replay_metric_basis,
            "median_realized_net_per_fill_with_fills": median_net,
            "positive_filled_segment_ratio": positive_ratio,
            "total_fills": total_fills,
        },
        "live_thresholds": {
            "min_round_trips_before_promotion": 30,
            "fee_stress_multiplier_required": 1.25,
            "max_single_trade_profit_share": 0.30,
        },
        "live_metrics": {
            "fills": live_fills,
            "realized_net_per_fill": live_net,
        },
    }


def section_readiness(section: Dict[str, Any]) -> str:
    if not isinstance(section, dict) or not section:
        return "NOT_EVALUATED"
    return str(section.get("readiness_status", section.get("status", "unknown"))).upper()


def assess_trading_convergence(
    runtime_section: Dict[str, Any],
    replay_section: Dict[str, Any],
    strategy_diagnose_section: Dict[str, Any],
    feature_parity_section: Dict[str, Any],
    exit_capture_section: Dict[str, Any],
    canary_validation_section: Dict[str, Any],
) -> Dict[str, Any]:
    if not runtime_section and not replay_section:
        return {
            "status": "pass",
            "readiness_status": "NOT_EVALUATED",
            "fail_reasons": [],
            "warn_reasons": [],
            "blockers": [],
            "thresholds": {},
            "metrics": {},
        }

    runtime_metrics = runtime_section.get("metrics", {}) if isinstance(runtime_section, dict) else {}
    if not isinstance(runtime_metrics, dict):
        runtime_metrics = {}
    live_fills = max(
        as_int(runtime_metrics.get("funnel_fills_runtime_count")),
        as_int(runtime_metrics.get("trend_candidate_probe_fill_count")),
    )
    live_net = as_float(runtime_metrics.get("realized_net_per_fill"))
    runtime_class = classify_runtime_validation(runtime_section)

    canary_metrics = (
        canary_validation_section.get("replay_metrics", {})
        if isinstance(canary_validation_section, dict)
        else {}
    )
    if not isinstance(canary_metrics, dict):
        canary_metrics = {}
    replay_summary = (
        replay_section.get("aggregate_summary", {})
        if isinstance(replay_section, dict)
        else {}
    )
    if not isinstance(replay_summary, dict):
        replay_summary = {}
    replay_total_fills = as_int(
        canary_metrics.get("total_fills", replay_summary.get("total_fills"))
    )
    replay_positive_ratio = as_float(
        canary_metrics.get(
            "positive_filled_segment_ratio",
            replay_summary.get("positive_filled_segment_ratio"),
        )
    )
    replay_median_net = as_float(
        canary_metrics.get(
            "median_realized_net_per_fill_with_fills",
            replay_summary.get("median_realized_net_per_fill_with_fills"),
        )
    )

    exit_replay = (
        exit_capture_section.get("replay", {})
        if isinstance(exit_capture_section, dict)
        else {}
    )
    if not isinstance(exit_replay, dict):
        exit_replay = {}
    exit_sample_count = as_int(exit_replay.get("sample_count"))
    exit_mean_capture = as_float(exit_replay.get("mean_gross_capture_of_path_mfe"))

    blocker_statuses: List[str] = []
    replay_status = section_readiness(replay_section)
    strategy_diagnose_status = section_readiness(strategy_diagnose_section)
    canary_status = section_readiness(canary_validation_section)
    feature_parity_status = section_readiness(feature_parity_section)
    exit_capture_status = section_readiness(exit_capture_section)

    if strategy_diagnose_section:
        if strategy_diagnose_status == "FAIL":
            blocker_statuses.append("NOT_CONVERGED_STRATEGY_RAW_EDGE_FAIL")
        elif strategy_diagnose_status == "ACTION_REQUIRED":
            blocker_statuses.append("NOT_CONVERGED_STRATEGY_RAW_EDGE_ACTION_REQUIRED")
        elif strategy_diagnose_status != "PASS":
            blocker_statuses.append("NOT_CONVERGED_STRATEGY_RAW_EDGE_NOT_VERIFIED")
    if replay_status == "FAIL" or canary_status == "FAIL":
        blocker_statuses.append("NOT_CONVERGED_REPLAY_CANARY_FAIL")
    if replay_total_fills < CANARY_MIN_REPLAY_TOTAL_FILLS:
        blocker_statuses.append("NOT_CONVERGED_REPLAY_SAMPLE_INSUFFICIENT")
    if replay_median_net is not None and replay_median_net <= 0.0:
        blocker_statuses.append("NOT_CONVERGED_REPLAY_MEDIAN_NET_NOT_POSITIVE")
    if (
        replay_positive_ratio is None
        or replay_positive_ratio < CANARY_MIN_POSITIVE_FILLED_SEGMENT_RATIO
    ):
        blocker_statuses.append("NOT_CONVERGED_REPLAY_POSITIVE_RATIO_LOW")
    if feature_parity_status != "PASS":
        blocker_statuses.append("NOT_CONVERGED_FEATURE_PARITY_NOT_VERIFIED")
    if exit_capture_status == "FAIL":
        blocker_statuses.append("NOT_CONVERGED_EXIT_CAPTURE_FAIL")
    elif exit_capture_status != "PASS":
        blocker_statuses.append("NOT_CONVERGED_EXIT_CAPTURE_NOT_VERIFIED")
    if exit_sample_count < EXIT_CAPTURE_MIN_SAMPLES:
        blocker_statuses.append("NOT_CONVERGED_EXIT_CAPTURE_SAMPLE_INSUFFICIENT")
    if (
        live_fills <= 0
        or runtime_class == "PROTECTION_PASS_NO_TRADE_VALIDATION"
    ):
        blocker_statuses.append("NOT_CONVERGED_NO_LIVE_FILLS")
    elif live_net is not None and live_net <= 0.0:
        blocker_statuses.append("NOT_CONVERGED_LIVE_NET_NOT_POSITIVE")

    blockers = list(dict.fromkeys(blocker_statuses))
    readiness_status = (
        "CONVERGED_CANARY_VALIDATED_WITH_LIVE_FILLS"
        if not blockers
        else blockers[0]
    )
    warn_reasons: List[str] = []
    if blockers:
        warn_reasons.append(
            "trading convergence not reached: " + ",".join(blockers)
        )
    return {
        "status": "pass",
        "readiness_status": readiness_status,
        "fail_reasons": [],
        "warn_reasons": warn_reasons,
        "blockers": blockers,
        "thresholds": {
            "min_replay_total_fills": CANARY_MIN_REPLAY_TOTAL_FILLS,
            "min_replay_positive_filled_segment_ratio": (
                CANARY_MIN_POSITIVE_FILLED_SEGMENT_RATIO
            ),
            "min_exit_capture_samples": EXIT_CAPTURE_MIN_SAMPLES,
            "min_exit_capture_mean_gross_capture_of_path_mfe": (
                EXIT_CAPTURE_MIN_MEAN_GROSS_CAPTURE_OF_PATH_MFE
            ),
        },
        "metrics": {
            "runtime_validation_class": runtime_class,
            "strategy_diagnose_status": strategy_diagnose_status,
            "live_fills": live_fills,
            "live_realized_net_per_fill": live_net,
            "replay_total_fills": replay_total_fills,
            "replay_median_realized_net_per_fill_with_fills": replay_median_net,
            "replay_positive_filled_segment_ratio": replay_positive_ratio,
            "feature_parity_status": feature_parity_status,
            "exit_capture_status": exit_capture_status,
            "exit_capture_sample_count": exit_sample_count,
            "exit_capture_mean_gross_capture_of_path_mfe": exit_mean_capture,
            "replay_readiness_status": replay_status,
            "canary_validation_status": canary_status,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成闭环汇总报告")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    parser.add_argument("--pipeline_name", default="ai-trade-closed-loop", help="流水线名称")
    parser.add_argument("--run_id", default="", help="可选：闭环运行 ID")
    parser.add_argument("--run_manifest", default="", help="run_manifest.json 路径")
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
        "--strategy_diagnose_report",
        default="",
        help="strategy_diagnose_report.json 路径",
    )
    parser.add_argument(
        "--walkforward_min_avg_sharpe",
        type=float,
        default=0.0,
        help="walk-forward 平均 Sharpe 最低门槛（默认 0.0，低于即 FAIL）",
    )
    parser.add_argument(
        "--walkforward_min_avg_split_return",
        type=float,
        default=0.0,
        help="walk-forward 平均 split 收益最低门槛（默认 0.0，低于即 FAIL）",
    )
    parser.add_argument(
        "--walkforward_min_enabled_avg_split_return",
        type=float,
        default=0.0,
        help="walk-forward 启用 split 平均收益最低门槛（默认 0.0，低于即 FAIL）",
    )
    parser.add_argument(
        "--walkforward_min_traded_avg_split_return",
        type=float,
        default=0.0,
        help="walk-forward 交易 split 平均收益最低门槛（默认 0.0，低于即 FAIL）",
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
    run_manifest_payload: Dict[str, Any] = {}

    if args.run_manifest:
        manifest_path = Path(args.run_manifest)
        if manifest_path.is_file():
            sections["run_manifest"] = assess_run_manifest(manifest_path, args.run_id)
            manifest = sections["run_manifest"].get("manifest", {})
            if isinstance(manifest, dict):
                run_manifest_payload = manifest
        else:
            sections["run_manifest"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {manifest_path}"],
            }

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
                min_avg_split_return=float(args.walkforward_min_avg_split_return),
                min_enabled_avg_split_return=float(
                    args.walkforward_min_enabled_avg_split_return
                ),
                min_traded_avg_split_return=float(
                    args.walkforward_min_traded_avg_split_return
                ),
                min_traded_split_count=int(args.walkforward_min_traded_split_count),
                min_total_trades=int(args.walkforward_min_total_trades),
                min_trend_bucket_bars=int(args.walkforward_min_trend_bucket_bars),
                min_trend_bucket_trades=int(args.walkforward_min_trend_bucket_trades),
                focus_bucket="trend"
                if (
                    int(args.trend_validation_min_bars) > 0
                    or int(args.trend_validation_min_trades) > 0
                    or float(args.trend_validation_min_sharpe) != 0.0
                )
                else "",
                min_focus_bucket_bars=int(args.trend_validation_min_bars),
                min_focus_bucket_trades=int(args.trend_validation_min_trades),
                min_focus_bucket_sharpe=float(args.trend_validation_min_sharpe),
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

    if args.strategy_diagnose_report:
        strategy_diagnose_path = Path(args.strategy_diagnose_report)
        if strategy_diagnose_path.is_file():
            sections["strategy_diagnose"] = assess_strategy_diagnose(strategy_diagnose_path)
        else:
            sections["strategy_diagnose"] = {
                "status": "fail",
                "fail_reasons": [f"文件不存在: {strategy_diagnose_path}"],
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
    inherited_sections_excluded_from_gate = [
        name
        for name in inherited_sections
        if name in INHERITED_SECTIONS_EXCLUDED_FROM_CURRENT_GATE
    ]

    replay_alignment = assess_replay_live_symbol_alignment(
        sections.get("runtime", {}),
        sections.get("replay_validation", {}),
    )
    if replay_alignment.get("readiness_status") != "NOT_EVALUATED":
        sections["replay_symbol_alignment"] = replay_alignment

    runtime_section_for_derived = sections.get("runtime", {})
    replay_section_for_derived = sections.get("replay_validation", {})
    feature_parity_for_derived: Dict[str, Any] = {}
    if runtime_section_for_derived:
        feature_parity_for_derived = assess_feature_parity(runtime_section_for_derived)
        sections["feature_parity"] = feature_parity_for_derived
    if replay_section_for_derived:
        runtime_for_derived = (
            runtime_section_for_derived
            if isinstance(runtime_section_for_derived, dict)
            else {}
        )
        sections["exit_capture"] = assess_exit_capture(
            replay_section_for_derived,
            runtime_for_derived,
        )
        sections["canary_validation"] = assess_canary_validation(
            replay_section_for_derived,
            runtime_for_derived,
        )
    if runtime_section_for_derived or replay_section_for_derived:
        sections["trading_convergence"] = assess_trading_convergence(
            runtime_section_for_derived,
            replay_section_for_derived,
            sections.get("strategy_diagnose", {}),
            feature_parity_for_derived or sections.get("feature_parity", {}),
            sections.get("exit_capture", {}),
            sections.get("canary_validation", {}),
        )

    for section_name, section in sections.items():
        if section_name in inherited_sections_excluded_from_gate:
            continue
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
                "last_notional_usd": runtime_account_pnl.get("last_notional_usd"),
                "last_abs_notional_usd": runtime_account_pnl.get(
                    "last_abs_notional_usd"
                ),
                "start_flat": runtime_account_pnl.get("start_flat"),
                "end_flat": runtime_account_pnl.get("end_flat"),
                "account_counter_reset_count": runtime_account_pnl.get(
                    "account_counter_reset_count"
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

    runtime_validation_class = classify_runtime_validation(runtime_section)

    registry_section = sections.get("registry", {})
    promotion_readiness_status = "NOT_EVALUATED"
    if "registry" in inherited_sections_excluded_from_gate:
        promotion_readiness_status = "NOT_EVALUATED"
    elif isinstance(registry_section, dict) and registry_section:
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

    replay_symbol_alignment_section = sections.get("replay_symbol_alignment", {})
    replay_symbol_alignment_status = "NOT_EVALUATED"
    if (
        isinstance(replay_symbol_alignment_section, dict)
        and replay_symbol_alignment_section
    ):
        replay_symbol_alignment_status = str(
            replay_symbol_alignment_section.get(
                "readiness_status",
                replay_symbol_alignment_section.get("status", "unknown"),
            )
        ).upper()

    strategy_diagnose_section = sections.get("strategy_diagnose", {})
    strategy_diagnose_status = "NOT_EVALUATED"
    if isinstance(strategy_diagnose_section, dict) and strategy_diagnose_section:
        strategy_diagnose_status = str(
            strategy_diagnose_section.get(
                "readiness_status",
                strategy_diagnose_section.get("status", "unknown"),
            )
        ).upper()

    feature_parity_section = sections.get("feature_parity", {})
    feature_parity_status = "NOT_EVALUATED"
    if isinstance(feature_parity_section, dict) and feature_parity_section:
        feature_parity_status = str(
            feature_parity_section.get(
                "readiness_status", feature_parity_section.get("status", "unknown")
            )
        ).upper()

    exit_capture_section = sections.get("exit_capture", {})
    exit_capture_status = "NOT_EVALUATED"
    if isinstance(exit_capture_section, dict) and exit_capture_section:
        exit_capture_status = str(
            exit_capture_section.get(
                "readiness_status", exit_capture_section.get("status", "unknown")
            )
        ).upper()

    canary_validation_section = sections.get("canary_validation", {})
    canary_validation_status = "NOT_EVALUATED"
    if isinstance(canary_validation_section, dict) and canary_validation_section:
        canary_validation_status = str(
            canary_validation_section.get(
                "readiness_status", canary_validation_section.get("status", "unknown")
            )
        ).upper()

    trading_convergence_section = sections.get("trading_convergence", {})
    trading_convergence_status = "NOT_EVALUATED"
    if isinstance(trading_convergence_section, dict) and trading_convergence_section:
        trading_convergence_status = str(
            trading_convergence_section.get(
                "readiness_status",
                trading_convergence_section.get("status", "unknown"),
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
        "strategy_success_status": overall_status,
        "runtime_verdict": runtime_verdict,
        "runtime_validation_mode": runtime_validation_mode,
        "runtime_validation_class": runtime_validation_class,
        "runtime_health_status": runtime_health_status,
        "promotion_readiness_status": promotion_readiness_status,
        "trend_readiness_status": trend_readiness_status,
        "replay_readiness_status": replay_readiness_status,
        "replay_symbol_alignment_status": replay_symbol_alignment_status,
        "strategy_diagnose_status": strategy_diagnose_status,
        "feature_parity_status": feature_parity_status,
        "exit_capture_status": exit_capture_status,
        "canary_validation_status": canary_validation_status,
        "trading_convergence_status": trading_convergence_status,
        "trading_convergence_readiness_status": trading_convergence_status,
        "status_semantics": {
            "workflow_success_meaning": (
                "GitHub workflow success only means the job completed and artifacts "
                "were uploaded; use overall_status for strategy success."
            ),
            "strategy_success_field": "overall_status",
            "runtime_health_field": "runtime_health_status",
            "runtime_validation_class": runtime_validation_class,
            "promotion_field": "promotion_readiness_status",
            "replay_field": "replay_readiness_status",
            "strategy_diagnose_field": "strategy_diagnose_status",
            "policy_flat_warning": (
                "PROTECTION_PASS_NO_TRADE_VALIDATION only proves safe flat behavior; "
                "it is not evidence of trading convergence."
            ),
            "trading_convergence_field": "trading_convergence_status",
        },
        "run_manifest": run_manifest_payload,
        "account_outcome": account_outcome,
        "sections": sections,
        "inherit": {
            "source_report": inherit_source_report,
            "status": inherit_status or "ok",
            "inherited_sections": inherited_sections,
            "current_gate_excluded_sections": inherited_sections_excluded_from_gate,
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
