#!/usr/bin/env python3
"""
Validate closed-loop report contracts.

Goals:
1) Ensure latest report files keep stable required fields.
2) Catch contract drift before web/control-plane consumers break.
3) Provide a lightweight local/CI gate without external dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_object(payload: Any, label: str, errors: List[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        errors.append(f"{label} 必须是 object")
        return None
    return payload


def _require_key(payload: Dict[str, Any], key: str, label: str, errors: List[str]) -> Any:
    if key not in payload:
        errors.append(f"{label} 缺少字段: {key}")
        return None
    return payload[key]


def _validate_runtime_assess(payload: Dict[str, Any], errors: List[str]) -> None:
    _require_key(payload, "stage", "runtime_assess", errors)
    verdict = _require_key(payload, "verdict", "runtime_assess", errors)
    if verdict is not None and verdict not in {"PASS", "PASS_WITH_ACTIONS", "FAIL"}:
        errors.append(f"runtime_assess.verdict 非法: {verdict}")

    metrics = _require_key(payload, "metrics", "runtime_assess", errors)
    metrics_obj = _require_object(metrics, "runtime_assess.metrics", errors)
    if metrics_obj is not None:
        _require_key(metrics_obj, "runtime_status_count", "runtime_assess.metrics", errors)
        _require_key(metrics_obj, "critical_count", "runtime_assess.metrics", errors)
        _require_key(metrics_obj, "ws_unhealthy_count", "runtime_assess.metrics", errors)
        _require_key(
            metrics_obj,
            "self_evolution_action_count",
            "runtime_assess.metrics",
            errors,
        )
        _require_key(
            metrics_obj,
            "self_evolution_virtual_action_count",
            "runtime_assess.metrics",
            errors,
        )
        _require_key(
            metrics_obj,
            "self_evolution_counterfactual_action_count",
            "runtime_assess.metrics",
            errors,
        )
        _require_key(
            metrics_obj,
            "self_evolution_counterfactual_update_count",
            "runtime_assess.metrics",
            errors,
        )

    fail_reasons = _require_key(payload, "fail_reasons", "runtime_assess", errors)
    if fail_reasons is not None and not isinstance(fail_reasons, list):
        errors.append("runtime_assess.fail_reasons 必须是 list")
    warn_reasons = _require_key(payload, "warn_reasons", "runtime_assess", errors)
    if warn_reasons is not None and not isinstance(warn_reasons, list):
        errors.append("runtime_assess.warn_reasons 必须是 list")


def _validate_closed_loop_report(payload: Dict[str, Any], errors: List[str]) -> None:
    overall = _require_key(payload, "overall_status", "closed_loop_report", errors)
    if overall is not None and overall not in {"PASS", "PASS_WITH_ACTIONS", "FAIL"}:
        errors.append(f"closed_loop_report.overall_status 非法: {overall}")

    sections = _require_key(payload, "sections", "closed_loop_report", errors)
    sections_obj = _require_object(sections, "closed_loop_report.sections", errors)
    if sections_obj is None:
        return

    runtime = _require_key(sections_obj, "runtime", "closed_loop_report.sections", errors)
    runtime_obj = _require_object(runtime, "closed_loop_report.sections.runtime", errors)
    if runtime_obj is None:
        return

    _require_key(runtime_obj, "status", "closed_loop_report.sections.runtime", errors)
    _require_key(runtime_obj, "verdict", "closed_loop_report.sections.runtime", errors)
    metrics = _require_key(runtime_obj, "metrics", "closed_loop_report.sections.runtime", errors)
    _require_object(metrics, "closed_loop_report.sections.runtime.metrics", errors)


def _validate_run_meta(payload: Dict[str, Any], errors: List[str]) -> None:
    run_id = _require_key(payload, "run_id", "run_meta", errors)
    if isinstance(run_id, str) and not RUN_ID_RE.match(run_id):
        errors.append(f"run_meta.run_id 非法: {run_id}")

    _require_key(payload, "action", "run_meta", errors)
    _require_key(payload, "stage", "run_meta", errors)
    overall = _require_key(payload, "overall_status", "run_meta", errors)
    if overall is not None and overall not in {"PASS", "PASS_WITH_ACTIONS", "FAIL", ""}:
        errors.append(f"run_meta.overall_status 非法: {overall}")


def _validate_summary(payload: Dict[str, Any], label: str, errors: List[str]) -> None:
    summary_type = _require_key(payload, "summary_type", label, errors)
    if summary_type is not None and summary_type not in {"daily", "weekly"}:
        errors.append(f"{label}.summary_type 非法: {summary_type}")
    overall = _require_key(payload, "overall_status", label, errors)
    if overall is not None and overall not in {"PASS", "PASS_WITH_ACTIONS", "FAIL"}:
        errors.append(f"{label}.overall_status 非法: {overall}")


def _validate_one(path: Path, kind: str, errors: List[str]) -> None:
    try:
        payload = _load_json(path)
    except FileNotFoundError:
        errors.append(f"文件不存在: {path}")
        return
    except json.JSONDecodeError as exc:
        errors.append(f"JSON 解析失败: {path}: {exc}")
        return

    obj = _require_object(payload, kind, errors)
    if obj is None:
        return

    if kind == "runtime_assess":
        _validate_runtime_assess(obj, errors)
    elif kind == "closed_loop_report":
        _validate_closed_loop_report(obj, errors)
    elif kind == "run_meta":
        _validate_run_meta(obj, errors)
    elif kind in {"daily_summary", "weekly_summary"}:
        _validate_summary(obj, kind, errors)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate closed-loop report contracts")
    parser.add_argument(
        "--reports-root",
        default="./data/reports/closed_loop",
        help="closed-loop reports root",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="validate specific run dir under reports root (e.g. 20260214T143928Z)",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="exit 0 when target files are missing",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.reports_root)

    files: List[tuple[Path, str]] = []
    if args.run_id:
        run_dir = root / args.run_id
        files.extend(
            [
                (run_dir / "runtime_assess.json", "runtime_assess"),
                (run_dir / "closed_loop_report.json", "closed_loop_report"),
            ]
        )
    else:
        files.extend(
            [
                (root / "latest_runtime_assess.json", "runtime_assess"),
                (root / "latest_closed_loop_report.json", "closed_loop_report"),
                (root / "latest_run_meta.json", "run_meta"),
                (root / "summary" / "daily_latest.json", "daily_summary"),
                (root / "summary" / "weekly_latest.json", "weekly_summary"),
            ]
        )

    missing = [path for path, _ in files if not path.exists()]
    if missing and args.allow_missing:
        print("[INFO] reports missing (allow-missing enabled), skip validation:")
        for item in missing:
            print(f"  - {item}")
        return 0

    errors: List[str] = []
    for path, kind in files:
        _validate_one(path, kind, errors)

    if errors:
        print("[ERROR] report contract validation failed:")
        for item in errors:
            print(f"  - {item}")
        return 1

    print("[INFO] report contract validation passed")
    for path, _ in files:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
