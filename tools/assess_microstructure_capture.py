#!/usr/bin/env python3
"""Fail-closed quality/readiness gate for forward microstructure captures."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import time
from typing import Any, Dict, Iterable, List, Tuple


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def merge_duration_ms(intervals: Iterable[Tuple[int, int]]) -> int:
    merged: List[List[int]] = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if not merged or start > merged[-1][1] + 1000:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start + 1000 for start, end in merged)


def resolve_artifact(root: pathlib.Path, recorded: str, kind: str, symbol: str) -> pathlib.Path:
    recorded_path = pathlib.Path(recorded)
    if recorded_path.is_file():
        return recorded_path
    candidate = root / kind / symbol / recorded_path.name
    return candidate


def assess_collector_health(
    root: pathlib.Path,
    *,
    symbol: str,
    now_ms: int,
    max_stale_ms: int,
) -> Dict[str, Any]:
    path = root / "collector_health.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = str(payload.get("state") or "").strip().lower()
        recorded_symbol = str(payload.get("symbol") or "").strip().upper()
        reference_ms = int(
            payload.get("last_success_epoch_ms")
            or payload.get("segment_started_epoch_ms")
            or 0
        )
        age_ms = now_ms - reference_ms if reference_ms > 0 else None
        fresh = bool(
            payload.get("schema_version") == "microstructure_collector_health_v1"
            and state in {"capturing", "healthy"}
            and recorded_symbol == symbol
            and age_ms is not None
            and 0 <= age_ms <= max_stale_ms
        )
        return {
            "status": "PASS" if fresh else "FAIL",
            "state": state or None,
            "symbol": recorded_symbol or None,
            "reference_epoch_ms": reference_ms or None,
            "age_ms": age_ms,
            "path": str(path),
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "FAIL",
            "state": None,
            "symbol": None,
            "reference_epoch_ms": None,
            "age_ms": None,
            "path": str(path),
            "error": str(exc),
        }


def assess(args: argparse.Namespace) -> Dict[str, Any]:
    root = pathlib.Path(args.root).resolve()
    now_ms = int(args.now_epoch_ms or time.time() * 1000)
    collector_health = assess_collector_health(
        root,
        symbol=args.symbol,
        now_ms=now_ms,
        max_stale_ms=args.max_stale_sec * 1000,
    )
    report_paths = sorted((root / "reports" / args.symbol).glob("*.json"))
    intervals: List[Tuple[int, int]] = []
    total_rows = 0
    total_messages = 0
    total_book_updates = 0
    total_trades = 0
    invalid: List[str] = []
    segments: List[Dict[str, Any]] = []
    latest_exchange_timestamp = 0
    for report_path in report_paths:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            raw = payload["raw"]
            features = payload["features"]
            start = int(features["first_timestamp"])
            end = int(features["last_timestamp"])
            raw_path = resolve_artifact(root, str(raw["path"]), "raw", args.symbol)
            feature_path = resolve_artifact(
                root, str(features["path"]), "features", args.symbol
            )
            contract_ok = bool(
                payload.get("schema_version") == "bybit_microstructure_v1"
                and payload.get("status") == "PASS"
                and payload.get("research_domain") == "forward_development_only"
                and payload.get("promotion_evidence") is False
                and payload.get("promotion_eligible") is False
                and raw_path.is_file()
                and feature_path.is_file()
                and sha256_file(raw_path) == raw.get("sha256")
                and sha256_file(feature_path) == features.get("sha256")
            )
            if not contract_ok:
                raise ValueError("contract/checksum mismatch")
            intervals.append((start, end))
            latest_exchange_timestamp = max(latest_exchange_timestamp, end)
            total_rows += int(features.get("row_count", 0))
            total_messages += int(raw.get("message_count", 0))
            quality = payload.get("quality", {})
            total_book_updates += int(quality.get("book_update_count", 0))
            total_trades += int(quality.get("trade_count", 0))
            segments.append(
                {
                    "report_path": str(report_path.resolve()),
                    "report_sha256": sha256_file(report_path),
                    "raw_path": str(raw_path.resolve()),
                    "raw_sha256": str(raw.get("sha256") or ""),
                    "raw_message_count": int(raw.get("message_count", 0)),
                    "feature_path": str(feature_path.resolve()),
                    "feature_sha256": str(features.get("sha256") or ""),
                    "first_timestamp_ms": start,
                    "last_timestamp_ms": end,
                    "feature_row_count": int(features.get("row_count", 0)),
                }
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid.append(f"{report_path.name}:{exc}")

    coverage_ms = merge_duration_ms(intervals)
    freshness_age_ms = now_ms - latest_exchange_timestamp if latest_exchange_timestamp else None
    expected_rows = coverage_ms / 1000.0
    row_density = total_rows / expected_rows if expected_rows > 0 else 0.0
    capture_in_progress = bool(
        not intervals
        and collector_health.get("status") == "PASS"
        and collector_health.get("state") in {"capturing", "healthy"}
    )
    failures = []
    if invalid:
        failures.append("invalid_segment_contract")
    if coverage_ms < args.min_capture_duration_sec * 1000:
        failures.append("minimum_forward_capture_duration")
    if not capture_in_progress:
        if freshness_age_ms is None or freshness_age_ms < 0 or freshness_age_ms > args.max_stale_sec * 1000:
            failures.append("capture_freshness")
        if row_density < args.min_row_density:
            failures.append("feature_row_density")
        if total_book_updates <= 0:
            failures.append("book_updates_missing")
        if total_trades <= 0:
            failures.append("public_trades_missing")
        if not intervals and collector_health.get("status") != "PASS":
            failures.append("collector_health")
    return {
        "schema_version": "microstructure_capture_assessment_v1",
        "status": "PASS" if not failures else "FAIL",
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "development_screen_ready": not failures,
        "capture_in_progress": capture_in_progress,
        "collector_health": collector_health,
        "symbol": args.symbol,
        "segment_count": len(report_paths),
        "valid_segment_count": len(report_paths) - len(invalid),
        "coverage_ms": coverage_ms,
        "minimum_coverage_ms": args.min_capture_duration_sec * 1000,
        "latest_exchange_timestamp_ms": latest_exchange_timestamp or None,
        "freshness_age_ms": freshness_age_ms,
        "feature_row_count": total_rows,
        "feature_row_density": row_density,
        "raw_message_count": total_messages,
        "book_update_count": total_book_updates,
        "trade_count": total_trades,
        # This checksum-bound manifest is the only input contract accepted by
        # the downstream development economic screen.  It prevents that screen
        # from silently globbing a different or subsequently mutated capture.
        "segments": segments,
        "failures": failures,
        "invalid_segments": invalid,
        "next_gate": "development_economic_screen" if not failures else "continue_forward_capture",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--symbol", default="SOLUSDT", choices=("SOLUSDT",))
    parser.add_argument("--min-capture-duration-sec", type=int, default=86400)
    parser.add_argument("--max-stale-sec", type=int, default=1800)
    parser.add_argument("--min-row-density", type=float, default=0.80)
    parser.add_argument("--now-epoch-ms", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = assess(args)
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
