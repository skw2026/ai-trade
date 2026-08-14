#!/usr/bin/env python3
"""Fail-closed readiness assessment for Binance SOLUSDT research capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import time
from typing import Any, Dict, Iterable, List, Tuple

import collect_binance_microstructure as collector
import run_binance_microstructure_collector as supervisor


SCHEMA_VERSION = "binance_microstructure_capture_assessment_v1"


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


def resolve_artifact(
    root: pathlib.Path, recorded: str, kind: str
) -> pathlib.Path:
    path = pathlib.Path(recorded)
    if path.is_file():
        return path.resolve()
    return (root / kind / collector.SYMBOL / path.name).resolve()


def assess_health(root: pathlib.Path, now_ms: int, max_stale_ms: int) -> Dict[str, Any]:
    path = root / "collector_health.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        reference = int(
            payload.get("last_success_epoch_ms")
            or payload.get("segment_started_epoch_ms")
            or 0
        )
        age = now_ms - reference if reference else None
        passed = bool(
            payload.get("schema_version") == supervisor.SCHEMA_VERSION
            and payload.get("capture_schema_version") == collector.SCHEMA_VERSION
            and payload.get("symbol") == collector.SYMBOL
            and payload.get("state") in {"capturing", "healthy"}
            and age is not None
            and 0 <= age <= max_stale_ms
        )
        return {
            "status": "PASS" if passed else "FAIL",
            "state": payload.get("state"),
            "reference_epoch_ms": reference or None,
            "age_ms": age,
            "path": str(path),
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "FAIL", "path": str(path), "error": str(exc)}


def assess(args: argparse.Namespace) -> Dict[str, Any]:
    root = pathlib.Path(args.root).resolve()
    now_ms = int(args.now_epoch_ms or time.time() * 1000)
    health = assess_health(root, now_ms, args.max_stale_sec * 1000)
    report_paths = sorted((root / "reports" / collector.SYMBOL).glob("*.json"))
    intervals: List[Tuple[int, int]] = []
    segments: List[Dict[str, Any]] = []
    invalid: List[str] = []
    total_rows = total_messages = total_book_updates = total_trades = 0
    latest_timestamp = 0
    for report_path in report_paths:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            raw = payload["raw"]
            features = payload["features"]
            raw_path = resolve_artifact(root, str(raw["path"]), "raw")
            feature_path = resolve_artifact(root, str(features["path"]), "features")
            start = int(features["first_timestamp"])
            end = int(features["last_timestamp"])
            if not (
                payload.get("schema_version") == collector.SCHEMA_VERSION
                and payload.get("status") == "PASS"
                and payload.get("research_domain") == "forward_development_only"
                and payload.get("promotion_evidence") is False
                and payload.get("promotion_eligible") is False
                and payload.get("demo_activation_authorized") is False
                and payload.get("live_activation_authorized") is False
                and payload.get("source") == "binance_usdm_public_websocket"
                and payload.get("symbol") == collector.SYMBOL
                and payload.get("alignment_contract") == collector.ALIGNMENT_CONTRACT
                and raw_path.is_file()
                and feature_path.is_file()
                and sha256_file(raw_path) == raw.get("sha256")
                and sha256_file(feature_path) == features.get("sha256")
                and start >= 0
                and end >= start
            ):
                raise ValueError("contract/checksum mismatch")
            quality = payload.get("quality")
            if not isinstance(quality, dict):
                raise ValueError("quality is missing")
            rows = int(features.get("row_count") or 0)
            messages = int(raw.get("message_count") or 0)
            book_updates = int(quality.get("book_update_count") or 0)
            trades = int(quality.get("trade_count") or 0)
            if min(rows, messages, book_updates, trades) <= 0:
                raise ValueError("segment has empty market evidence")
            intervals.append((start, end))
            latest_timestamp = max(latest_timestamp, end)
            total_rows += rows
            total_messages += messages
            total_book_updates += book_updates
            total_trades += trades
            segments.append(
                {
                    "capture_schema_version": collector.SCHEMA_VERSION,
                    "symbol": collector.SYMBOL,
                    "report_path": str(report_path.resolve()),
                    "report_sha256": sha256_file(report_path),
                    "raw_path": str(raw_path),
                    "raw_sha256": str(raw["sha256"]),
                    "raw_message_count": messages,
                    "feature_path": str(feature_path),
                    "feature_sha256": str(features["sha256"]),
                    "first_timestamp_ms": start,
                    "last_timestamp_ms": end,
                    "feature_row_count": rows,
                }
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid.append(f"{report_path.name}:{exc}")
    coverage_ms = merge_duration_ms(intervals)
    expected_rows = coverage_ms / 1000.0
    density = total_rows / expected_rows if expected_rows > 0 else 0.0
    freshness = now_ms - latest_timestamp if latest_timestamp else None
    failures: List[str] = []
    if invalid:
        failures.append("invalid_segment_contract")
    if health.get("status") != "PASS":
        failures.append("collector_health")
    if coverage_ms < args.min_capture_duration_sec * 1000:
        failures.append("minimum_forward_capture_duration")
    if freshness is None or freshness < 0 or freshness > args.max_stale_sec * 1000:
        failures.append("capture_freshness")
    if density < args.min_row_density:
        failures.append("feature_row_density")
    if total_book_updates <= 0:
        failures.append("book_updates_missing")
    if total_trades <= 0:
        failures.append("aggregate_trades_missing")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if not failures else "NOT_READY",
        "fully_verifiable": not invalid and bool(segments),
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "development_screen_ready": not failures,
        "symbol": collector.SYMBOL,
        "source": "binance_usdm_public_websocket",
        "alignment_contract": collector.ALIGNMENT_CONTRACT,
        "collector_health": health,
        "coverage_ms": coverage_ms,
        "minimum_coverage_ms": args.min_capture_duration_sec * 1000,
        "latest_exchange_timestamp_ms": latest_timestamp or None,
        "freshness_age_ms": freshness,
        "feature_row_count": total_rows,
        "feature_row_density": density,
        "raw_message_count": total_messages,
        "book_update_count": total_book_updates,
        "trade_count": total_trades,
        "segments": segments,
        "invalid_segments": invalid,
        "failures": failures,
        "next_action": (
            "run_frozen_cross_venue_information_set_experiment"
            if not failures
            else "continue_external_venue_capture"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
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
