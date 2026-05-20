#!/usr/bin/env python3
"""
模型版本注册与激活脚本。

目标：
1. 将每次训练产物（.cbm + report）注册为可追溯版本；
2. 基于门槛（AUC / Delta AUC）决定是否激活为当前线上版本；
3. 维护 index 清单与历史版本保留上限。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - 非 POSIX 环境兜底
    fcntl = None


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc_compact() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 使用原子替换避免写一半进程中断导致 JSON 损坏。
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_name(raw: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip())
    value = value.strip("._-")
    return value or "unknown_model"


def atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def load_index(index_path: Path) -> List[Dict[str, Any]]:
    if not index_path.exists():
        return []
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
    except json.JSONDecodeError:
        pass
    return []


class FileLock:
    """
    轻量文件锁（仅用于索引更新的临界区）。

    说明：
    1. POSIX 环境使用 flock；非 POSIX 环境降级为无锁（单进程仍可运行）；
    2. 锁文件与 index.json 同目录，避免跨文件系统行为不一致。
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd = None

    def __enter__(self) -> "FileLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = self._lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is None:
            return
        try:
            self._fd.flush()
            os.fsync(self._fd.fileno())
        except OSError:
            pass
        if fcntl is not None:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self._fd.close()
        self._fd = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ai-trade 模型版本注册/激活工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="注册训练产物并按门槛激活")
    register.add_argument("--model_file", required=True, help="训练产物模型文件（.cbm）")
    register.add_argument("--integrator_report", required=True, help="integrator_report.json 路径")
    register.add_argument("--miner_report", default="", help="可选：miner_report.json 路径")
    register.add_argument("--walkforward_report", default="", help="可选：walkforward_report.json 路径")
    register.add_argument(
        "--replay_validation_report",
        default="",
        help="可选：replay_validation_report.json 路径",
    )
    register.add_argument("--registry_dir", default="./data/models/registry", help="模型注册目录")
    register.add_argument("--max_versions", type=int, default=20, help="历史版本最大保留数")
    register.add_argument(
        "--active_model_path",
        default="./data/models/integrator_latest.cbm",
        help="激活模型写入路径",
    )
    register.add_argument(
        "--active_report_path",
        default="./data/research/integrator_report.json",
        help="激活报告写入路径",
    )
    register.add_argument(
        "--active_miner_report_path",
        default="./data/research/miner_report.json",
        help="激活 miner 报告写入路径（供运行期稳定引用）",
    )
    register.add_argument(
        "--active_meta_path",
        default="./data/models/integrator_active.json",
        help="激活元信息写入路径",
    )
    register.add_argument("--min_auc_mean", type=float, default=0.50, help="最小 AUC 均值门槛")
    register.add_argument(
        "--min_delta_auc_vs_baseline",
        type=float,
        default=0.0,
        help="最小 Delta AUC（相对 baseline）门槛",
    )
    register.add_argument(
        "--min_split_trained_count",
        type=int,
        default=1,
        help="最小训练成功 split 数门槛",
    )
    register.add_argument(
        "--min_split_trained_ratio",
        type=float,
        default=0.5,
        help="最小训练成功 split 比例门槛",
    )
    register.add_argument(
        "--activate_on_pass",
        action="store_true",
        help="门槛通过后自动激活为当前版本",
    )
    register.add_argument(
        "--require_walkforward_positive",
        action="store_true",
        help="要求 walk-forward 满足净收益门槛后才允许激活",
    )
    register.add_argument(
        "--min_walkforward_avg_split_return",
        type=float,
        default=0.0,
        help="walk-forward 平均 split 收益最低门槛",
    )
    register.add_argument(
        "--min_walkforward_enabled_avg_split_return",
        type=float,
        default=0.0,
        help="walk-forward 启用 split 平均收益最低门槛",
    )
    register.add_argument(
        "--min_walkforward_traded_avg_split_return",
        type=float,
        default=0.0,
        help="walk-forward 交易 split 平均收益最低门槛",
    )
    register.add_argument(
        "--walkforward_focus_bucket",
        default="",
        help="可选：以指定 regime bucket 作为主链 walk-forward 通过口径（例如 S5 使用 trend）",
    )
    register.add_argument(
        "--walkforward_min_focus_bucket_bars",
        type=int,
        default=0,
        help="focus bucket 生效所需最小 bars",
    )
    register.add_argument(
        "--walkforward_min_focus_bucket_trades",
        type=int,
        default=0,
        help="focus bucket 最小交易次数",
    )
    register.add_argument(
        "--walkforward_min_focus_bucket_sharpe",
        type=float,
        default=0.0,
        help="focus bucket 最小 Sharpe",
    )
    register.add_argument(
        "--require_replay_validation_pass",
        action="store_true",
        help="要求 replay validation 状态为 pass 后才允许激活",
    )
    register.add_argument(
        "--registration_out",
        default="",
        help="可选：将本次注册结果单独输出到 JSON 文件",
    )
    return parser.parse_args()


def gate_integrator_report(
    report: Dict[str, Any],
    min_auc_mean: float,
    min_delta_auc_vs_baseline: float,
    min_split_trained_count: int,
    min_split_trained_ratio: float,
) -> Tuple[bool, List[str], List[str], Dict[str, Any]]:
    metrics = report.get("metrics_oos", {})
    governance = report.get("governance", {})
    data = report.get("data", {})
    feature_transform = report.get("feature_transform", {})
    auc_mean = metrics.get("auc_mean")
    delta_auc = metrics.get("delta_auc_vs_baseline")
    trained_count = metrics.get("split_trained_count")
    split_count = metrics.get("split_count")
    split_trained_ratio = metrics.get("split_trained_ratio")

    fail_reasons: List[str] = []
    warn_reasons: List[str] = []

    if not isinstance(auc_mean, (float, int)):
        fail_reasons.append("缺少 metrics_oos.auc_mean")
    elif float(auc_mean) < min_auc_mean:
        fail_reasons.append(
            f"auc_mean={float(auc_mean):.6f} < min_auc_mean={min_auc_mean:.6f}"
        )

    if not isinstance(delta_auc, (float, int)):
        fail_reasons.append("缺少 metrics_oos.delta_auc_vs_baseline")
    elif float(delta_auc) < min_delta_auc_vs_baseline:
        fail_reasons.append(
            "delta_auc_vs_baseline="
            f"{float(delta_auc):.6f} < min_delta_auc_vs_baseline={min_delta_auc_vs_baseline:.6f}"
        )

    if not isinstance(trained_count, int) or not isinstance(split_count, int):
        fail_reasons.append("缺少 split_trained_count/split_count")
    elif trained_count <= 0 or split_count <= 0 or trained_count > split_count:
        fail_reasons.append(
            f"split 计数异常: split_trained_count={trained_count}, split_count={split_count}"
        )
    elif trained_count < min_split_trained_count:
        fail_reasons.append(
            "split_trained_count="
            f"{trained_count} < min_split_trained_count={min_split_trained_count}"
        )

    if not isinstance(split_trained_ratio, (float, int)):
        if isinstance(trained_count, int) and isinstance(split_count, int) and split_count > 0:
            split_trained_ratio = float(trained_count) / float(split_count)
        else:
            fail_reasons.append("缺少 metrics_oos.split_trained_ratio")
    if isinstance(split_trained_ratio, (float, int)):
        ratio_value = float(split_trained_ratio)
        if ratio_value < min_split_trained_ratio:
            fail_reasons.append(
                "split_trained_ratio="
                f"{ratio_value:.6f} < min_split_trained_ratio={min_split_trained_ratio:.6f}"
            )

    if isinstance(governance, dict):
        governance_pass = governance.get("pass")
        if isinstance(governance_pass, bool) and not governance_pass:
            fail_reasons.append("integrator_report.governance.pass=false")
            governance_fail_reasons = governance.get("fail_reasons", [])
            if isinstance(governance_fail_reasons, list):
                for item in governance_fail_reasons:
                    item_text = str(item).strip()
                    if item_text:
                        fail_reasons.append(f"governance: {item_text}")
        governance_warn_reasons = governance.get("warn_reasons", [])
        if isinstance(governance_warn_reasons, list):
            for item in governance_warn_reasons:
                item_text = str(item).strip()
                if item_text:
                    warn_reasons.append(f"governance: {item_text}")

    gate_pass = len(fail_reasons) == 0
    summary = {
        "auc_mean": auc_mean,
        "delta_auc_vs_baseline": delta_auc,
        "split_trained_count": trained_count,
        "split_count": split_count,
        "split_trained_ratio": split_trained_ratio,
        "auc_stdev": metrics.get("auc_stdev"),
        "train_test_auc_gap_mean": metrics.get("train_test_auc_gap_mean"),
        "random_label_auc": metrics.get("random_label_auc"),
        "random_label_auc_mean": metrics.get("random_label_auc_mean"),
        "random_label_auc_stdev": metrics.get("random_label_auc_stdev"),
        "random_label_auc_max": metrics.get("random_label_auc_max"),
        "predict_horizon_bars": data.get("predict_horizon_bars") if isinstance(data, dict) else None,
        "label_policy": data.get("label_policy") if isinstance(data, dict) else None,
        "feature_transform": {
            "feature_clipping_enabled": feature_transform.get("feature_clipping_enabled"),
            "clip_quantile": feature_transform.get("clip_quantile"),
            "enabled_clip_bound_count": feature_transform.get("enabled_clip_bound_count"),
            "clip_bound_count": len(feature_transform.get("clip_bounds", []))
            if isinstance(feature_transform.get("clip_bounds"), list)
            else 0,
        }
        if isinstance(feature_transform, dict)
        else None,
    }
    return gate_pass, fail_reasons, warn_reasons, summary


def coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def gate_walkforward_report(
    report_path: Path | None,
    require_report: bool,
    min_avg_split_return: float,
    min_enabled_avg_split_return: float,
    min_traded_avg_split_return: float,
    focus_bucket: str = "",
    min_focus_bucket_bars: int = 0,
    min_focus_bucket_trades: int = 0,
    min_focus_bucket_sharpe: float = 0.0,
) -> Tuple[bool, List[str], List[str], Dict[str, Any]]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    summary: Dict[str, Any] = {}

    if report_path is None:
        if require_report:
            fail_reasons.append("walkforward_report 缺失")
        return len(fail_reasons) == 0, fail_reasons, warn_reasons, summary
    if not report_path.is_file():
        fail_reasons.append(f"walkforward_report 不存在: {report_path}")
        return False, fail_reasons, warn_reasons, summary

    payload = read_json(report_path)
    raw_summary = payload.get("summary", {})
    if not isinstance(raw_summary, dict):
        fail_reasons.append("walkforward_report.summary 缺失或格式错误")
        return False, fail_reasons, warn_reasons, summary
    summary = raw_summary
    focus_validation: Dict[str, Any] = {}
    focus_bucket_name = str(focus_bucket or "").strip().lower()
    focus_bucket_pass = False
    if focus_bucket_name:
        regime_bucket_summary = summary.get("regime_bucket_summary", {})
        bucket_payload = (
            regime_bucket_summary.get(focus_bucket_name, {})
            if isinstance(regime_bucket_summary, dict)
            else {}
        )
        if not isinstance(bucket_payload, dict):
            bucket_payload = {}
        focus_bars = int(bucket_payload.get("bars", 0) or 0)
        focus_trades = int(bucket_payload.get("trades", 0) or 0)
        focus_sharpe = coerce_float(bucket_payload.get("sharpe"))
        focus_fail_reasons: List[str] = []
        if focus_bars < int(min_focus_bucket_bars):
            focus_fail_reasons.append(
                f"{focus_bucket_name} bucket bars={focus_bars} < {int(min_focus_bucket_bars)}"
            )
        if focus_trades < int(min_focus_bucket_trades):
            focus_fail_reasons.append(
                f"{focus_bucket_name} bucket trades={focus_trades} < {int(min_focus_bucket_trades)}"
            )
        if focus_sharpe is None:
            focus_fail_reasons.append(f"{focus_bucket_name} bucket sharpe missing")
        elif focus_sharpe < float(min_focus_bucket_sharpe):
            focus_fail_reasons.append(
                f"{focus_bucket_name} bucket sharpe={focus_sharpe:.6f} < {float(min_focus_bucket_sharpe):.6f}"
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
            fail_reasons.extend(focus_fail_reasons)

    return_checks = [
        ("avg_split_return", min_avg_split_return),
        ("enabled_avg_split_return", min_enabled_avg_split_return),
        ("traded_avg_split_return", min_traded_avg_split_return),
    ]
    for metric_name, threshold in return_checks:
        metric_value = coerce_float(summary.get(metric_name))
        if metric_value is None:
            fail_reasons.append(f"walkforward_report.summary.{metric_name} 缺失")
            continue
        if metric_value < float(threshold):
            reason = (
                f"walkforward {metric_name}={metric_value:.6f} < {float(threshold):.6f}"
            )
            if focus_bucket_pass:
                warn_reasons.append(
                    reason
                    + f"; ignored because {focus_bucket_name} bucket focus gate passed"
                )
            else:
                fail_reasons.append(reason)

    total_trades = summary.get("total_trades")
    traded_split_count = summary.get("traded_split_count")
    if isinstance(total_trades, int) and total_trades <= 0:
        fail_reasons.append(f"walkforward total_trades={total_trades} <= 0")
    if isinstance(traded_split_count, int) and traded_split_count <= 0:
        fail_reasons.append(f"walkforward traded_split_count={traded_split_count} <= 0")

    if focus_validation:
        summary = dict(summary)
        summary["focus_bucket_validation"] = focus_validation
    return len(fail_reasons) == 0, fail_reasons, warn_reasons, summary


def gate_replay_validation_report(
    report_path: Path | None,
    require_report: bool,
) -> Tuple[bool, List[str], List[str], Dict[str, Any]]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    summary: Dict[str, Any] = {}

    if report_path is None:
        if require_report:
            fail_reasons.append("replay_validation_report 缺失")
        return len(fail_reasons) == 0, fail_reasons, warn_reasons, summary
    if not report_path.is_file():
        fail_reasons.append(f"replay_validation_report 不存在: {report_path}")
        return False, fail_reasons, warn_reasons, summary

    payload = read_json(report_path)
    aggregate = payload.get("aggregate_validation", {})
    aggregate_status = ""
    if isinstance(aggregate, dict):
        aggregate_status = str(aggregate.get("status", "")).strip().lower()
    status = str(payload.get("status", aggregate_status)).strip().lower()
    if status != "pass":
        fail_reasons.append(f"replay_validation status={status or 'UNKNOWN'} != pass")

    fail_items = payload.get("fail_reasons", [])
    if not fail_items and isinstance(aggregate, dict):
        fail_items = aggregate.get("fail_reasons", [])
    if isinstance(fail_items, list):
        for item in fail_items:
            item_text = str(item).strip()
            if item_text:
                fail_reasons.append(item_text)

    warn_items = payload.get("warn_reasons", [])
    if not warn_items and isinstance(aggregate, dict):
        warn_items = aggregate.get("warn_reasons", [])
    if isinstance(warn_items, list):
        for item in warn_items:
            item_text = str(item).strip()
            if item_text:
                warn_reasons.append(item_text)

    coverage_strength_status = payload.get("coverage_strength_status")
    if coverage_strength_status is None and isinstance(aggregate, dict):
        coverage_strength_status = aggregate.get("coverage_strength_status")
    execution_optimizer = payload.get("execution_optimizer", {})
    if not isinstance(execution_optimizer, dict):
        execution_optimizer = {}
    optimizer_status = str(execution_optimizer.get("status", "")).strip().lower()
    if status == "pass" and optimizer_status == "fail":
        fail_reasons.append("replay execution_optimizer status=fail")
    execution_cost_plan = payload.get("execution_cost_plan", {})
    if not isinstance(execution_cost_plan, dict):
        execution_cost_plan = {}
    cost_plan_status = str(execution_cost_plan.get("status", "")).strip().lower()
    if status == "pass" and cost_plan_status == "fail":
        fail_reasons.append("replay execution_cost_plan status=fail")
    elif cost_plan_status == "candidate_requires_rerun":
        warn_reasons.append(
            "replay execution_cost_plan found lower-cost candidate requiring rerun"
        )
    summary = {
        "status": payload.get("status", aggregate_status),
        "coverage_strength_status": coverage_strength_status,
        "aggregate_validation": aggregate if isinstance(aggregate, dict) else {},
        "execution_economics": payload.get("execution_economics", {}),
        "cost_sensitivity": payload.get("cost_sensitivity", {}),
        "exit_capture": payload.get("exit_capture", {}),
        "execution_cost_plan": execution_cost_plan,
        "execution_optimizer": execution_optimizer,
    }
    if isinstance(aggregate, dict):
        for key in (
            "execution_active_runs",
            "execution_pass_runs",
            "total_fills",
            "positive_realized_net_with_fills_runs",
            "negative_realized_net_with_fills_runs",
            "mean_realized_net_per_fill",
            "mean_realized_net_per_fill_with_fills",
        ):
            if key in aggregate:
                summary[key] = aggregate.get(key)

    return len(fail_reasons) == 0, fail_reasons, warn_reasons, summary


def prune_old_versions(
    index_entries: List[Dict[str, Any]], registry_dir: Path, max_versions: int
) -> List[Dict[str, Any]]:
    if max_versions <= 0 or len(index_entries) <= max_versions:
        return index_entries

    keep = index_entries[:max_versions]
    drop = index_entries[max_versions:]
    for entry in drop:
        subdir = entry.get("registry_subdir")
        if isinstance(subdir, str) and subdir:
            target = registry_dir / subdir
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
    return keep


def run_register(args: argparse.Namespace) -> int:
    if not (0.0 <= float(args.min_auc_mean) <= 1.0):
        print("[ERROR] --min_auc_mean 必须在 [0,1] 范围", file=sys.stderr)
        return 2
    if not (0.0 <= float(args.min_split_trained_ratio) <= 1.0):
        print("[ERROR] --min_split_trained_ratio 必须在 [0,1] 范围", file=sys.stderr)
        return 2
    if int(args.min_split_trained_count) <= 0:
        print("[ERROR] --min_split_trained_count 必须大于 0", file=sys.stderr)
        return 2

    model_file = Path(args.model_file)
    integrator_report_path = Path(args.integrator_report)
    miner_report_path = Path(args.miner_report) if args.miner_report else None
    walkforward_report_path = Path(args.walkforward_report) if args.walkforward_report else None
    replay_validation_report_path = (
        Path(args.replay_validation_report) if args.replay_validation_report else None
    )

    if not model_file.is_file():
        print(f"[ERROR] model_file 不存在: {model_file}", file=sys.stderr)
        return 2
    if not integrator_report_path.is_file():
        print(f"[ERROR] integrator_report 不存在: {integrator_report_path}", file=sys.stderr)
        return 2
    if miner_report_path is not None and not miner_report_path.is_file():
        print(f"[ERROR] miner_report 不存在: {miner_report_path}", file=sys.stderr)
        return 2

    report = read_json(integrator_report_path)
    model_version = sanitize_name(str(report.get("model_version", "unknown_model")))
    feature_schema_version = str(report.get("feature_schema_version", "unknown_schema"))
    factor_set_version = str(report.get("factor_set_version", "unknown_factor_set"))

    gate_pass, gate_fail_reasons, gate_warn_reasons, metric_summary = gate_integrator_report(
        report,
        args.min_auc_mean,
        args.min_delta_auc_vs_baseline,
        args.min_split_trained_count,
        args.min_split_trained_ratio,
    )
    external_gate_summary: Dict[str, Any] = {}

    if args.require_walkforward_positive or walkforward_report_path is not None:
        (
            walkforward_pass,
            walkforward_fail_reasons,
            walkforward_warn_reasons,
            walkforward_summary,
        ) = gate_walkforward_report(
            walkforward_report_path,
            bool(args.require_walkforward_positive),
            float(args.min_walkforward_avg_split_return),
            float(args.min_walkforward_enabled_avg_split_return),
            float(args.min_walkforward_traded_avg_split_return),
            focus_bucket=str(getattr(args, "walkforward_focus_bucket", "")),
            min_focus_bucket_bars=int(
                getattr(args, "walkforward_min_focus_bucket_bars", 0)
            ),
            min_focus_bucket_trades=int(
                getattr(args, "walkforward_min_focus_bucket_trades", 0)
            ),
            min_focus_bucket_sharpe=float(
                getattr(args, "walkforward_min_focus_bucket_sharpe", 0.0)
            ),
        )
        external_gate_summary["walkforward"] = {
            "pass": walkforward_pass,
            "min_avg_split_return": args.min_walkforward_avg_split_return,
            "min_enabled_avg_split_return": args.min_walkforward_enabled_avg_split_return,
            "min_traded_avg_split_return": args.min_walkforward_traded_avg_split_return,
            "focus_bucket": getattr(args, "walkforward_focus_bucket", ""),
            "min_focus_bucket_bars": getattr(args, "walkforward_min_focus_bucket_bars", 0),
            "min_focus_bucket_trades": getattr(args, "walkforward_min_focus_bucket_trades", 0),
            "min_focus_bucket_sharpe": getattr(args, "walkforward_min_focus_bucket_sharpe", 0.0),
            "summary": walkforward_summary,
        }
        for item in walkforward_fail_reasons:
            gate_fail_reasons.append(f"walkforward: {item}")
        for item in walkforward_warn_reasons:
            gate_warn_reasons.append(f"walkforward: {item}")

    if args.require_replay_validation_pass or replay_validation_report_path is not None:
        (
            replay_pass,
            replay_fail_reasons,
            replay_warn_reasons,
            replay_summary,
        ) = gate_replay_validation_report(
            replay_validation_report_path,
            bool(args.require_replay_validation_pass),
        )
        external_gate_summary["replay_validation"] = {
            "pass": replay_pass,
            "summary": replay_summary,
        }
        for item in replay_fail_reasons:
            gate_fail_reasons.append(f"replay_validation: {item}")
        for item in replay_warn_reasons:
            gate_warn_reasons.append(f"replay_validation: {item}")

    gate_pass = len(gate_fail_reasons) == 0

    created_at = now_utc_iso()
    created_tag = now_utc_compact()
    model_sha = sha256_file(model_file)
    report_sha = sha256_file(integrator_report_path)
    entry_id = f"{created_tag}_{model_version}_{model_sha[:8]}"
    registry_subdir = sanitize_name(entry_id)

    registry_dir = Path(args.registry_dir)
    entry_dir = registry_dir / registry_subdir
    entry_dir.mkdir(parents=True, exist_ok=True)

    model_dst = entry_dir / "integrator_model.cbm"
    report_dst = entry_dir / "integrator_report.json"
    miner_dst = entry_dir / "miner_report.json"
    meta_dst = entry_dir / "metadata.json"

    shutil.copy2(model_file, model_dst)
    shutil.copy2(integrator_report_path, report_dst)
    miner_sha = ""
    if miner_report_path is not None:
        shutil.copy2(miner_report_path, miner_dst)
        miner_sha = sha256_file(miner_report_path)

    gate_payload = {
        "pass": gate_pass,
        "min_auc_mean": args.min_auc_mean,
        "min_delta_auc_vs_baseline": args.min_delta_auc_vs_baseline,
        "min_split_trained_count": args.min_split_trained_count,
        "min_split_trained_ratio": args.min_split_trained_ratio,
        "require_walkforward_positive": bool(args.require_walkforward_positive),
        "min_walkforward_avg_split_return": args.min_walkforward_avg_split_return,
        "min_walkforward_enabled_avg_split_return": (
            args.min_walkforward_enabled_avg_split_return
        ),
        "min_walkforward_traded_avg_split_return": (
            args.min_walkforward_traded_avg_split_return
        ),
        "require_replay_validation_pass": bool(args.require_replay_validation_pass),
        "fail_reasons": gate_fail_reasons,
        "warn_reasons": gate_warn_reasons,
        "metric_summary": metric_summary,
        "external": external_gate_summary,
    }

    activated = bool(args.activate_on_pass and gate_pass)
    active_model_path = Path(args.active_model_path)
    active_report_path = Path(args.active_report_path)
    active_miner_report_path = Path(args.active_miner_report_path)
    active_meta_path = Path(args.active_meta_path)

    if activated:
        atomic_copy(model_file, active_model_path)
        active_report_payload = json.loads(
            json.dumps(report, ensure_ascii=False)
        )
        data_section = active_report_payload.get("data")
        if not isinstance(data_section, dict):
            data_section = {}
            active_report_payload["data"] = data_section
        if miner_report_path is not None:
            atomic_copy(miner_report_path, active_miner_report_path)
            data_section["miner_report_path"] = str(active_miner_report_path)
        write_json(active_report_path, active_report_payload)
        active_payload = {
            "active_entry_id": entry_id,
            "model_version": model_version,
            "feature_schema_version": feature_schema_version,
            "factor_set_version": factor_set_version,
            "activated_at_utc": created_at,
            "model_sha256": model_sha,
            "report_sha256": report_sha,
            "gate": gate_payload,
        }
        write_json(active_meta_path, active_payload)

    entry_payload: Dict[str, Any] = {
        "entry_id": entry_id,
        "registry_subdir": registry_subdir,
        "created_at_utc": created_at,
        "model_version": model_version,
        "feature_schema_version": feature_schema_version,
        "factor_set_version": factor_set_version,
        "artifacts": {
            "model_file": str(model_dst),
            "integrator_report": str(report_dst),
            "miner_report": str(miner_dst) if miner_report_path is not None else "",
        },
        "checksums": {
            "model_sha256": model_sha,
            "integrator_report_sha256": report_sha,
            "miner_report_sha256": miner_sha,
        },
        "sizes": {
            "model_bytes": model_file.stat().st_size,
            "integrator_report_bytes": integrator_report_path.stat().st_size,
            "miner_report_bytes": miner_report_path.stat().st_size if miner_report_path else 0,
        },
        "gate": gate_payload,
        "activated": activated,
    }
    write_json(meta_dst, entry_payload)

    index_path = registry_dir / "index.json"
    index_lock_path = registry_dir / ".index.lock"
    with FileLock(index_lock_path):
        index_entries = load_index(index_path)
        index_entries = [entry for entry in index_entries if entry.get("entry_id") != entry_id]
        index_entries.insert(0, entry_payload)
        index_entries = prune_old_versions(index_entries, registry_dir, args.max_versions)
        write_json(index_path, index_entries)

    if args.registration_out:
        write_json(Path(args.registration_out), entry_payload)

    print(f"MODEL_REGISTRY_ENTRY: {entry_id}")
    print(f"GATE_PASS: {str(gate_pass).lower()}")
    print(f"ACTIVATED: {str(activated).lower()}")
    if gate_fail_reasons:
        print("GATE_FAIL_REASONS:")
        for item in gate_fail_reasons:
            print(f"  - {item}")
    print(f"INDEX_PATH: {index_path}")
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "register":
        return run_register(args)
    print(f"[ERROR] 未知命令: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
