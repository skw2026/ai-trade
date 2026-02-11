#!/usr/bin/env python3
"""
冻结当前线上基线模型快照（D1）。

目的：
1. 在新一轮离线训练前，把当前 active 模型/报告固化到本次 run 目录；
2. 生成 baseline_report.json，供闭环汇总与后续人工审计。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import sys
from typing import Any, Dict, Optional


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_copy(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="冻结当前线上基线模型")
    parser.add_argument("--active_model", default="./data/models/integrator_latest.cbm")
    parser.add_argument("--active_report", default="./data/research/integrator_report.json")
    parser.add_argument("--active_meta", default="./data/models/integrator_active.json")
    parser.add_argument("--output_dir", required=True, help="基线快照输出目录")
    parser.add_argument("--output_report", required=True, help="baseline_report.json 输出路径")
    return parser.parse_args()


def maybe_read_meta(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def main() -> int:
    args = parse_args()
    active_model = pathlib.Path(args.active_model)
    active_report = pathlib.Path(args.active_report)
    active_meta = pathlib.Path(args.active_meta)
    output_dir = pathlib.Path(args.output_dir)
    output_report = pathlib.Path(args.output_report)

    fail_reasons = []
    warn_reasons = []

    has_model = active_model.is_file()
    has_report = active_report.is_file()
    if not has_model:
        fail_reasons.append(f"active_model 不存在: {active_model}")
    if not has_report:
        fail_reasons.append(f"active_report 不存在: {active_report}")

    baseline_available = has_model and has_report
    payload: Dict[str, Any] = {
        "generated_at_utc": now_utc_iso(),
        "baseline_available": baseline_available,
        "status": "available" if baseline_available else "skipped",
        "fail_reasons": [] if baseline_available else fail_reasons,
        "warn_reasons": warn_reasons,
        "active_sources": {
            "active_model": str(active_model),
            "active_report": str(active_report),
            "active_meta": str(active_meta),
        },
    }

    if baseline_available:
        output_dir.mkdir(parents=True, exist_ok=True)
        model_dst = output_dir / "baseline_model.cbm"
        report_dst = output_dir / "baseline_integrator_report.json"
        meta_dst = output_dir / "baseline_active_meta.json"
        safe_copy(active_model, model_dst)
        safe_copy(active_report, report_dst)
        if active_meta.is_file():
            safe_copy(active_meta, meta_dst)
        else:
            warn_reasons.append(f"active_meta 不存在，跳过复制: {active_meta}")

        report_json = read_json(report_dst)
        metrics_oos = report_json.get("metrics_oos", {})
        payload.update(
            {
                "snapshot": {
                    "dir": str(output_dir),
                    "model_file": str(model_dst),
                    "report_file": str(report_dst),
                    "meta_file": str(meta_dst) if meta_dst.is_file() else "",
                },
                "checksums": {
                    "model_sha256": sha256_file(model_dst),
                    "report_sha256": sha256_file(report_dst),
                    "meta_sha256": sha256_file(meta_dst) if meta_dst.is_file() else "",
                },
                "model_meta": {
                    "model_version": report_json.get("model_version"),
                    "feature_schema_version": report_json.get("feature_schema_version"),
                    "factor_set_version": report_json.get("factor_set_version"),
                    "auc_mean": metrics_oos.get("auc_mean"),
                    "delta_auc_vs_baseline": metrics_oos.get("delta_auc_vs_baseline"),
                },
                "active_meta_json": maybe_read_meta(meta_dst),
            }
        )

    output_report.parent.mkdir(parents=True, exist_ok=True)
    output_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "[INFO] BASELINE_FREEZE: "
        f"status={payload['status']}, baseline_available={payload['baseline_available']}, "
        f"output={output_report}"
    )
    if fail_reasons and not baseline_available:
        for item in fail_reasons:
            print(f"[WARN] BASELINE_SKIP_REASON: {item}")
    for item in warn_reasons:
        print(f"[WARN] BASELINE_WARN: {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
