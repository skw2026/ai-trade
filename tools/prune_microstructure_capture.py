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
UPGRADE_SOURCE_SCHEMA_VERSION = "bybit_cross_asset_microstructure_v2"
UPGRADE_TARGET_SCHEMA_VERSION = "bybit_cross_asset_microstructure_v3"


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


def _expired_bundle_files(
    *,
    report: pathlib.Path,
    payload: Dict[str, Any],
    raw_root: pathlib.Path,
    feature_root: pathlib.Path,
) -> tuple[pathlib.Path, ...]:
    symbol_dir = report.parent
    symbol = symbol_dir.name
    raw = payload["raw"]
    features = payload["features"]
    raw_name = pathlib.Path(str(raw["path"])).name
    feature_name = pathlib.Path(str(features["path"])).name
    segment_id = report.stem
    raw_parent = raw_root / symbol
    feature_parent = feature_root / symbol

    if payload.get("status") != "PASS":
        raise ValueError("report status is not PASS")

    upgrade = payload.get("deterministic_raw_replay_upgrade")
    is_deterministic_upgrade = bool(
        payload.get("schema_version") == UPGRADE_TARGET_SCHEMA_VERSION
        and isinstance(upgrade, dict)
        and upgrade.get("source_schema_version") == UPGRADE_SOURCE_SCHEMA_VERSION
        and upgrade.get("target_schema_version") == UPGRADE_TARGET_SCHEMA_VERSION
        and upgrade.get("raw_payload_mutated") is False
    )
    if not is_deterministic_upgrade:
        if raw_name != f"{segment_id}.jsonl.gz":
            raise ValueError("raw filename does not bind report")
        if feature_name != f"{segment_id}.csv":
            raise ValueError("feature filename does not bind report")
        raw_path = raw_parent / raw_name
        feature_path = feature_parent / feature_name
        if not _safe_regular_file(raw_path, raw_parent):
            raise ValueError("raw artifact is missing or unsafe")
        if not _safe_regular_file(feature_path, feature_parent):
            raise ValueError("feature artifact is missing or unsafe")
        return report, raw_path, feature_path

    upgrade_suffix = f".{UPGRADE_TARGET_SCHEMA_VERSION}"
    if not segment_id.endswith(upgrade_suffix):
        raise ValueError("upgrade report filename does not bind target schema")
    source_segment_id = segment_id[: -len(upgrade_suffix)]
    if not source_segment_id:
        raise ValueError("upgrade source segment identity is empty")
    if raw_name != f"{source_segment_id}.jsonl.gz":
        raise ValueError("upgrade raw filename does not bind source segment")
    if feature_name != f"{segment_id}.csv":
        raise ValueError("upgrade feature filename does not bind report")

    upgraded_feature = feature_parent / feature_name
    if not _safe_regular_file(upgraded_feature, feature_parent):
        raise ValueError("upgrade feature artifact is missing or unsafe")

    # The upgraded report intentionally shares the immutable raw artifact with
    # its v2 source report. Remove the upgraded pair first and let the ordinary
    # source bundle own that shared raw file. If the source report was already
    # removed by an older pruner, finish the orphan cleanup using only the
    # strictly derived source names.
    source_report = symbol_dir / f"{source_segment_id}.json"
    if source_report.exists() or source_report.is_symlink():
        if not _safe_regular_file(source_report, symbol_dir):
            raise ValueError("upgrade source report is unsafe")
        return report, upgraded_feature

    originals = [report, upgraded_feature]
    for candidate, parent in (
        (raw_parent / raw_name, raw_parent),
        (feature_parent / f"{source_segment_id}.csv", feature_parent),
    ):
        if candidate.exists() or candidate.is_symlink():
            if not _safe_regular_file(candidate, parent):
                raise ValueError("orphaned upgrade source artifact is unsafe")
            originals.append(candidate)
    return tuple(originals)


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
            segment_id = report.stem
            originals = _expired_bundle_files(
                report=report,
                payload=payload,
                raw_root=raw_root,
                feature_root=feature_root,
            )
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
