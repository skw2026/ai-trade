#!/usr/bin/env python3
"""Fail-closed readiness assessment for sparse Bybit liquidation capture."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import time
from typing import Any, Dict, Iterable, List, Tuple

import collect_bybit_liquidations as collector
import run_liquidation_collector as supervisor


SCHEMA_VERSION = "liquidation_capture_assessment_v1"

_VALUE_ERROR_REASON_CODES = (
    ("artifact path escapes capture root", "artifact_path_contract"),
    ("contract/checksum mismatch", "contract_or_checksum_mismatch"),
    ("feature columns/order mismatch", "feature_schema_mismatch"),
    ("feature timestamps are not increasing", "feature_timestamp_order"),
    ("invalid sparse liquidation row", "feature_value_contract"),
    ("feature row count mismatch", "feature_row_count_mismatch"),
    ("raw message count mismatch", "raw_message_count_mismatch"),
    ("raw replay row count mismatch", "raw_replay_row_count_mismatch"),
    ("raw replay feature mismatch", "raw_replay_feature_mismatch"),
    ("event count mismatch", "event_count_mismatch"),
    ("liquidation quality audit mismatch", "quality_audit_mismatch"),
    ("empty sparse bounds mismatch", "empty_sparse_bounds_mismatch"),
    ("feature bounds mismatch", "feature_bounds_mismatch"),
)


def invalid_segment_reason_code(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "artifact_missing"
    if isinstance(exc, PermissionError):
        return "artifact_permission"
    if isinstance(exc, json.JSONDecodeError):
        return "report_json_invalid"
    if isinstance(exc, KeyError):
        return "report_field_missing"
    if isinstance(exc, TypeError):
        return "report_type_invalid"
    message = str(exc)
    for expected, reason_code in _VALUE_ERROR_REASON_CODES:
        if expected in message:
            return reason_code
    if isinstance(exc, OSError):
        return "artifact_io_error"
    return "unknown_contract_mismatch"


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def merge_intervals(intervals: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    merged: List[List[int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def resolve_artifact(root: pathlib.Path, recorded: str, kind: str) -> pathlib.Path:
    path = pathlib.Path(recorded)
    expected = (root / kind / collector.SYMBOL / path.name).resolve()
    candidate = path.resolve() if path.is_absolute() else expected
    if candidate != expected:
        raise ValueError("artifact path escapes capture root")
    return candidate


def assess_health(root: pathlib.Path, now_ms: int, max_stale_ms: int) -> Dict[str, Any]:
    path = root / "collector_health.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        reference = int(payload.get("last_success_epoch_ms") or payload.get("segment_started_epoch_ms") or 0)
        age = now_ms - reference if reference else None
        passed = bool(
            payload.get("schema_version") == supervisor.SCHEMA_VERSION
            and payload.get("capture_schema_version") == collector.SCHEMA_VERSION
            and payload.get("symbol") == collector.SYMBOL
            and payload.get("state") in {"capturing", "healthy"}
            and age is not None and 0 <= age <= max_stale_ms
        )
        return {"status": "PASS" if passed else "FAIL", "state": payload.get("state"),
                "reference_epoch_ms": reference or None, "age_ms": age, "path": str(path)}
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "FAIL", "path": str(path), "error": str(exc)}


def _validate_feature_file(
    path: pathlib.Path, expected_rows: int
) -> Tuple[List[Dict[str, float | int]], int, int]:
    rows: List[Dict[str, float | int]] = []
    row_count = event_count = last_timestamp = 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != collector.OUTPUT_FIELDS:
            raise ValueError("feature columns/order mismatch")
        previous = -1
        for raw in reader:
            timestamp = int(raw["timestamp"])
            if timestamp <= previous:
                raise ValueError("feature timestamps are not increasing")
            previous = last_timestamp = timestamp
            counts = int(raw["long_liquidation_count"]) + int(raw["short_liquidation_count"])
            numeric = [float(raw[field]) for field in collector.OUTPUT_FIELDS[1:]]
            if (
                counts <= 0
                or min(numeric) < 0.0
                or not float(raw["long_liquidation_count"]).is_integer()
                or not float(raw["short_liquidation_count"]).is_integer()
            ):
                raise ValueError("invalid sparse liquidation row")
            rows.append(
                {
                    "timestamp": timestamp,
                    **{
                        field: (
                            int(float(raw[field]))
                            if field.endswith("_count")
                            else float(raw[field])
                        )
                        for field in collector.OUTPUT_FIELDS[1:]
                    },
                }
            )
            event_count += counts
            row_count += 1
    if row_count != expected_rows:
        raise ValueError("feature row count mismatch")
    return rows, event_count, last_timestamp


def assess(args: argparse.Namespace) -> Dict[str, Any]:
    root = pathlib.Path(args.root).resolve()
    now_ms = int(args.now_epoch_ms or time.time() * 1000)
    health = assess_health(root, now_ms, args.max_stale_sec * 1000)
    report_paths = sorted((root / "reports" / collector.SYMBOL).glob("*.json"))
    intervals: List[Tuple[int, int]] = []
    segments: List[Dict[str, Any]] = []
    invalid: List[str] = []
    invalid_reason_counts: Dict[str, int] = {}
    total_rows = total_messages = total_events = 0
    latest_completed = 0
    for report_path in report_paths:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            raw, features, coverage, quality = payload["raw"], payload["features"], payload["coverage"], payload["quality"]
            raw_path = resolve_artifact(root, str(raw["path"]), "raw")
            feature_path = resolve_artifact(root, str(features["path"]), "features")
            start = int(coverage["capture_started_epoch_ms"])
            end = int(coverage["capture_completed_epoch_ms"])
            if not (
                payload.get("schema_version") == collector.SCHEMA_VERSION
                and payload.get("status") == "PASS"
                and payload.get("research_domain") == "forward_development_only"
                and payload.get("promotion_evidence") is False
                and payload.get("promotion_eligible") is False
                and payload.get("promotion_authority") is False
                and payload.get("demo_activation_authorized") is False
                and payload.get("live_activation_authorized") is False
                and payload.get("source") == "bybit_public_websocket_v5_all_liquidation"
                and payload.get("symbol") == collector.SYMBOL
                and payload.get("alignment_contract") == collector.ALIGNMENT_CONTRACT
                and coverage.get("connection_continuous") is True
                and int(coverage.get("duration_ms") or 0) == end - start > 0
                and raw_path.is_file() and feature_path.is_file()
                and sha256_file(raw_path) == raw.get("sha256")
                and sha256_file(feature_path) == features.get("sha256")
            ):
                raise ValueError("contract/checksum mismatch")
            feature_rows, events, last_event = _validate_feature_file(
                feature_path, int(features.get("row_count", -1))
            )
            rows = len(feature_rows)
            replayed_rows, replayed_messages = collector.replay_jsonl(raw_path)
            if replayed_messages != int(raw.get("message_count", -1)):
                raise ValueError("raw message count mismatch")
            if len(replayed_rows) != rows:
                raise ValueError("raw replay row count mismatch")
            for expected_row, actual_row in zip(replayed_rows, feature_rows):
                for field in collector.OUTPUT_FIELDS:
                    if float(expected_row[field]) != float(actual_row[field]):
                        raise ValueError("raw replay feature mismatch")
            if events != int(quality.get("liquidation_event_count", -1)):
                raise ValueError("event count mismatch")
            if (
                rows != int(quality.get("event_bucket_count", -1))
                or sum(int(row["long_liquidation_count"]) for row in replayed_rows)
                != int(quality.get("long_liquidation_count", -1))
                or sum(int(row["short_liquidation_count"]) for row in replayed_rows)
                != int(quality.get("short_liquidation_count", -1))
            ):
                raise ValueError("liquidation quality audit mismatch")
            if rows == 0 and (features.get("first_timestamp") is not None or features.get("last_timestamp") is not None):
                raise ValueError("empty sparse bounds mismatch")
            if rows and last_event != int(features.get("last_timestamp") or -1):
                raise ValueError("feature bounds mismatch")
            intervals.append((start, end))
            latest_completed = max(latest_completed, end)
            total_rows += rows
            total_messages += int(raw.get("message_count") or 0)
            total_events += events
            segments.append(
                {"capture_schema_version": collector.SCHEMA_VERSION, "symbol": collector.SYMBOL,
                 "report_path": str(report_path.resolve()), "report_sha256": sha256_file(report_path),
                 "raw_path": str(raw_path), "raw_sha256": str(raw["sha256"]),
                 "raw_message_count": int(raw.get("message_count") or 0),
                 "feature_path": str(feature_path), "feature_sha256": str(features["sha256"]),
                 "feature_row_count": rows, "liquidation_event_count": events,
                 "capture_started_epoch_ms": start, "capture_completed_epoch_ms": end},
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid.append(f"{report_path.name}:{exc}")
            reason_code = invalid_segment_reason_code(exc)
            invalid_reason_counts[reason_code] = invalid_reason_counts.get(reason_code, 0) + 1
    merged = merge_intervals(intervals)
    coverage_ms = sum(end - start for start, end in merged)
    freshness = now_ms - latest_completed if latest_completed else None
    failures: List[str] = []
    if invalid:
        failures.append("invalid_segment_contract")
    if health.get("status") != "PASS":
        failures.append("collector_health")
    if coverage_ms < args.min_capture_duration_sec * 1000:
        failures.append("minimum_forward_capture_duration")
    if freshness is None or freshness < 0 or freshness > args.max_stale_sec * 1000:
        failures.append("capture_freshness")
    return {
        "schema_version": SCHEMA_VERSION, "status": "PASS" if not failures else "NOT_READY",
        "fully_verifiable": not invalid and bool(segments), "research_domain": "forward_development_only",
        "promotion_evidence": False, "promotion_eligible": False, "promotion_authority": False,
        "demo_activation_authorized": False, "live_activation_authorized": False,
        "development_screen_ready": not failures, "symbol": collector.SYMBOL,
        "source": "bybit_public_websocket_v5_all_liquidation", "alignment_contract": collector.ALIGNMENT_CONTRACT,
        "collector_health": health, "coverage_ms": coverage_ms,
        "minimum_coverage_ms": args.min_capture_duration_sec * 1000,
        "coverage_intervals": [{"start_epoch_ms": start, "end_epoch_ms": end} for start, end in merged],
        "latest_capture_completed_epoch_ms": latest_completed or None, "freshness_age_ms": freshness,
        "feature_row_count": total_rows, "raw_message_count": total_messages,
        "liquidation_event_count": total_events, "segments": segments,
        "report_file_count": len(report_paths),
        "valid_segment_count": len(segments),
        "invalid_segment_count": len(invalid),
        "invalid_segment_reason_counts": dict(sorted(invalid_reason_counts.items())),
        "invalid_segments": invalid, "failures": failures,
        "next_action": "run_frozen_liquidation_information_set_experiment" if not failures else "continue_liquidation_capture",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-capture-duration-sec", type=int, default=86400)
    parser.add_argument("--max-stale-sec", type=int, default=1800)
    parser.add_argument("--now-epoch-ms", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = assess(args)
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
