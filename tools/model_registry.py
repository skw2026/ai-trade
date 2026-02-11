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
        "--activate_on_pass",
        action="store_true",
        help="门槛通过后自动激活为当前版本",
    )
    register.add_argument(
        "--registration_out",
        default="",
        help="可选：将本次注册结果单独输出到 JSON 文件",
    )
    return parser.parse_args()


def gate_integrator_report(
    report: Dict[str, Any], min_auc_mean: float, min_delta_auc_vs_baseline: float
) -> Tuple[bool, List[str], Dict[str, Any]]:
    metrics = report.get("metrics_oos", {})
    auc_mean = metrics.get("auc_mean")
    delta_auc = metrics.get("delta_auc_vs_baseline")
    trained_count = metrics.get("split_trained_count")
    split_count = metrics.get("split_count")

    fail_reasons: List[str] = []

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

    gate_pass = len(fail_reasons) == 0
    summary = {
        "auc_mean": auc_mean,
        "delta_auc_vs_baseline": delta_auc,
        "split_trained_count": trained_count,
        "split_count": split_count,
    }
    return gate_pass, fail_reasons, summary


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
    model_file = Path(args.model_file)
    integrator_report_path = Path(args.integrator_report)
    miner_report_path = Path(args.miner_report) if args.miner_report else None

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

    gate_pass, gate_fail_reasons, metric_summary = gate_integrator_report(
        report, args.min_auc_mean, args.min_delta_auc_vs_baseline
    )

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

    activated = bool(args.activate_on_pass and gate_pass)
    active_model_path = Path(args.active_model_path)
    active_report_path = Path(args.active_report_path)
    active_meta_path = Path(args.active_meta_path)

    if activated:
        atomic_copy(model_file, active_model_path)
        atomic_copy(integrator_report_path, active_report_path)
        active_payload = {
            "active_entry_id": entry_id,
            "model_version": model_version,
            "feature_schema_version": feature_schema_version,
            "factor_set_version": factor_set_version,
            "activated_at_utc": created_at,
            "model_sha256": model_sha,
            "report_sha256": report_sha,
            "gate": {
                "pass": gate_pass,
                "min_auc_mean": args.min_auc_mean,
                "min_delta_auc_vs_baseline": args.min_delta_auc_vs_baseline,
                "fail_reasons": gate_fail_reasons,
            },
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
        "gate": {
            "pass": gate_pass,
            "min_auc_mean": args.min_auc_mean,
            "min_delta_auc_vs_baseline": args.min_delta_auc_vs_baseline,
            "fail_reasons": gate_fail_reasons,
            "metric_summary": metric_summary,
        },
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
