#!/usr/bin/env python3
"""Prune complete, expired research capture segments as atomic bundles."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import time
import uuid
from typing import Any, Dict


SCHEMA_VERSION = "microstructure_capture_retention_v1"


def _safe_regular_file(path: pathlib.Path, parent: pathlib.Path) -> bool:
    return bool(
        path.parent.resolve() == parent.resolve()
        and path.is_file()
        and not path.is_symlink()
    )


def _tree_size(path: pathlib.Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def prune_capture_root(
    root: pathlib.Path,
    *,
    retention_seconds: int,
    now_epoch: float | None = None,
    expected_root_name: str | None = None,
) -> Dict[str, Any]:
    if retention_seconds <= 0:
        raise ValueError("retention_seconds must be positive")
    root = root.expanduser()
    if root.is_symlink():
        raise ValueError("capture root must not be a symlink")
    root = root.resolve()
    if expected_root_name and root.name != expected_root_name:
        raise ValueError(
            f"capture root name mismatch: expected={expected_root_name} actual={root.name}"
        )
    if root == pathlib.Path(root.anchor) or len(root.parts) < 4:
        raise ValueError(f"capture root is too broad: {root}")
    cutoff = float(now_epoch if now_epoch is not None else time.time()) - int(
        retention_seconds
    )
    summary: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "retention_seconds": int(retention_seconds),
        "cutoff_epoch": cutoff,
        "status": "PASS",
        "segments_removed": 0,
        "bytes_removed": 0,
        "segments_preserved": 0,
        "segments_skipped": [],
    }
    if not root.exists():
        summary["status"] = "NOT_PRESENT"
        return summary
    if not root.is_dir():
        raise ValueError("capture root is not a directory")

    raw_root = root / "raw"
    feature_root = root / "features"
    reports_root = root / "reports"
    trash_root = root / ".retention-trash"

    # A previous process may have crashed after moving an already-expired
    # complete bundle out of the authoritative reports tree. Those staged
    # bundles are safe to finish deleting before selecting more segments.
    if trash_root.exists():
        if trash_root.is_symlink() or not trash_root.is_dir():
            raise ValueError("retention trash path is invalid")
        for staged in sorted(trash_root.iterdir()):
            if staged.is_symlink() or not staged.is_dir():
                raise ValueError("retention trash contains an unsafe entry")
            summary["bytes_removed"] += _tree_size(staged)
            shutil.rmtree(staged)

    if not reports_root.is_dir():
        if trash_root.is_dir() and not any(trash_root.iterdir()):
            trash_root.rmdir()
        return summary

    for report in sorted(reports_root.glob("*/*.json")):
        symbol_dir = report.parent
        if not _safe_regular_file(report, symbol_dir):
            summary["segments_skipped"].append(
                {"report": str(report), "reason": "unsafe_report_path"}
            )
            continue
        if report.stat().st_mtime >= cutoff:
            summary["segments_preserved"] += 1
            continue
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
            raw_name = pathlib.Path(str(payload["raw"]["path"])).name
            feature_name = pathlib.Path(str(payload["features"]["path"])).name
            segment_id = report.stem
            if payload.get("status") != "PASS":
                raise ValueError("report status is not PASS")
            if raw_name != f"{segment_id}.jsonl.gz":
                raise ValueError("raw filename does not bind report")
            if feature_name != f"{segment_id}.csv":
                raise ValueError("feature filename does not bind report")
            raw_parent = raw_root / symbol_dir.name
            feature_parent = feature_root / symbol_dir.name
            raw = raw_parent / raw_name
            feature = feature_parent / feature_name
            if not _safe_regular_file(raw, raw_parent):
                raise ValueError("raw artifact is missing or unsafe")
            if not _safe_regular_file(feature, feature_parent):
                raise ValueError("feature artifact is missing or unsafe")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            summary["segments_skipped"].append(
                {"report": str(report), "reason": str(exc)}
            )
            continue

        trash_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        transaction = trash_root / (
            f"{symbol_dir.name}-{segment_id}-{uuid.uuid4().hex}"
        )
        transaction.mkdir(mode=0o700)
        originals = (report, raw, feature)
        moved: list[tuple[pathlib.Path, pathlib.Path]] = []
        try:
            segment_bytes = sum(item.stat().st_size for item in originals)
            for original in originals:
                staged = transaction / original.name
                os.replace(original, staged)
                moved.append((original, staged))
        except OSError:
            for original, staged in reversed(moved):
                if staged.exists() and not original.exists():
                    os.replace(staged, original)
            transaction.rmdir()
            raise
        shutil.rmtree(transaction)
        summary["segments_removed"] += 1
        summary["bytes_removed"] += segment_bytes

    if trash_root.is_dir() and not any(trash_root.iterdir()):
        trash_root.rmdir()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--expected-root-name", required=True)
    parser.add_argument("--retention-hours", type=int, default=96)
    parser.add_argument("--now-epoch", type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = prune_capture_root(
        pathlib.Path(args.root),
        retention_seconds=args.retention_hours * 3600,
        now_epoch=args.now_epoch,
        expected_root_name=args.expected_root_name,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
