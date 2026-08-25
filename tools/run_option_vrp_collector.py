#!/usr/bin/env python3
"""Run restartable, rotating public Bybit BTC option VRP captures."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, Sequence

import capture_bybit_option_vrp_v2 as collector
import prune_microstructure_capture as retention


SCHEMA_VERSION = "option_vrp_collector_health_v2"


def atomic_write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def utc_segment_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def segment_command(args: argparse.Namespace, *, root: pathlib.Path, duration_sec: float) -> tuple[Sequence[str], pathlib.Path]:
    segment_id = utc_segment_id()
    raw = root / "raw" / collector.BASE_COIN / f"{segment_id}.jsonl.xz"
    features = root / "features" / collector.BASE_COIN / f"{segment_id}.csv"
    report = root / "reports" / collector.BASE_COIN / f"{segment_id}.json"
    return ([
        sys.executable, str(pathlib.Path(__file__).resolve().parent / "capture_bybit_option_vrp_v2.py"),
        "--raw", str(raw), "--features", str(features), "--report", str(report),
        "--capture-root", str(root), "--duration-sec", str(duration_sec),
        "--poll-interval-sec", str(args.poll_interval_sec), "--base-url", args.base_url,
        "--minimum-dte-days", str(args.minimum_dte_days), "--maximum-dte-days", str(args.maximum_dte_days),
        "--maximum-absolute-moneyness", str(args.maximum_absolute_moneyness),
    ], report)


def run(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    health, latest = root / "collector_health.json", root / "latest_segment.json"
    completed = successes = failures = 0
    while args.max_segments <= 0 or completed < args.max_segments:
        duration = args.bootstrap_segment_duration_sec if completed == 0 else args.segment_duration_sec
        command, report_path = segment_command(args, root=root, duration_sec=duration)
        started = int(time.time() * 1000)
        atomic_write_json(health, {
            "schema_version": SCHEMA_VERSION, "state": "capturing", "base_coin": collector.BASE_COIN,
            "settle_coin": collector.SETTLE_COIN,
            "capture_schema_version": collector.SCHEMA_VERSION,
            "snapshot_schema_version": collector.SNAPSHOT_SCHEMA_VERSION,
            "scope_identity_sha256": collector.SCOPE_IDENTITY_SHA256,
            "raw_codec": collector.RAW_CODEC,
            "delivery_query_status": "PENDING", "segment_started_epoch_ms": started,
            "consecutive_failures": failures,
        })
        try:
            subprocess.run(command, check=True)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") != "PASS":
                raise RuntimeError("capture report did not pass")
            completed_at = int(time.time() * 1000)
            failures = 0
            relative = report_path.relative_to(root)
            atomic_write_json(latest, {
                "schema_version": "option_vrp_latest_segment_v2", "base_coin": collector.BASE_COIN,
                "settle_coin": collector.SETTLE_COIN,
                "capture_schema_version": collector.SCHEMA_VERSION,
                "snapshot_schema_version": collector.SNAPSHOT_SCHEMA_VERSION,
                "scope_identity_sha256": collector.SCOPE_IDENTITY_SHA256,
                "raw_codec": collector.RAW_CODEC,
                "report": str(relative),
                "report_payload": report, "completed_epoch_ms": completed_at,
            })
            atomic_write_json(health, {
                "schema_version": SCHEMA_VERSION, "state": "healthy", "base_coin": collector.BASE_COIN,
                "settle_coin": collector.SETTLE_COIN,
                "capture_schema_version": collector.SCHEMA_VERSION,
                "snapshot_schema_version": collector.SNAPSHOT_SCHEMA_VERSION,
                "scope_identity_sha256": collector.SCOPE_IDENTITY_SHA256,
                "raw_codec": collector.RAW_CODEC,
                "delivery_query_status": report.get("quality", {}).get("delivery_query_status"),
                "segment_started_epoch_ms": started,
                "last_success_epoch_ms": completed_at, "consecutive_failures": 0, "latest_report": str(relative),
            })
            retention.prune_capture_root(root, retention_seconds=args.retention_hours * 3600, expected_root_name=root.name)
            successes += 1
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
            failures += 1
            atomic_write_json(health, {
                "schema_version": SCHEMA_VERSION, "state": "degraded", "base_coin": collector.BASE_COIN,
                "settle_coin": collector.SETTLE_COIN,
                "capture_schema_version": collector.SCHEMA_VERSION,
                "snapshot_schema_version": collector.SNAPSHOT_SCHEMA_VERSION,
                "scope_identity_sha256": collector.SCOPE_IDENTITY_SHA256,
                "raw_codec": collector.RAW_CODEC,
                "delivery_query_status": "FAIL", "segment_started_epoch_ms": started,
                "last_failure_epoch_ms": int(time.time() * 1000), "consecutive_failures": failures, "error": str(exc),
            })
            if args.max_segments <= 0:
                time.sleep(min(float(args.max_backoff_sec), 2.0 ** min(failures, 6)))
        completed += 1
    return 0 if successes > 0 else 2


def healthcheck(args: argparse.Namespace) -> int:
    try:
        payload = json.loads((pathlib.Path(args.root) / "collector_health.json").read_text(encoding="utf-8"))
        reference = int(payload.get("last_success_epoch_ms") or payload.get("segment_started_epoch_ms") or 0)
        age_ms = int(time.time() * 1000) - reference
        valid = bool(
            payload.get("schema_version") == SCHEMA_VERSION
            and payload.get("capture_schema_version") == collector.SCHEMA_VERSION
            and payload.get("snapshot_schema_version") == collector.SNAPSHOT_SCHEMA_VERSION
            and payload.get("scope_identity_sha256") == collector.SCOPE_IDENTITY_SHA256
            and payload.get("raw_codec") == collector.RAW_CODEC
            and payload.get("base_coin") == collector.BASE_COIN
            and payload.get("settle_coin") == collector.SETTLE_COIN
            and payload.get("delivery_query_status") in {"PENDING", "PASS"}
            and payload.get("state") in {"capturing", "healthy"}
            and 0 <= age_ms <= args.max_stale_sec * 1000
        )
        return 0 if valid else 1
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--root", required=True)
    runner.add_argument("--segment-duration-sec", type=float, default=905.0)
    runner.add_argument("--bootstrap-segment-duration-sec", type=float, default=65.0)
    runner.add_argument("--poll-interval-sec", type=float, default=60.0)
    runner.add_argument("--retention-hours", type=int, default=240)
    runner.add_argument("--max-backoff-sec", type=int, default=60)
    runner.add_argument("--max-segments", type=int, default=0)
    runner.add_argument("--base-url", default=collector.BASE_URL)
    runner.add_argument("--minimum-dte-days", type=float, default=0.5)
    runner.add_argument("--maximum-dte-days", type=float, default=10.0)
    runner.add_argument("--maximum-absolute-moneyness", type=float, default=0.1)
    check = commands.add_parser("healthcheck")
    check.add_argument("--root", required=True)
    check.add_argument("--max-stale-sec", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.action == "run":
        if min(args.segment_duration_sec, args.bootstrap_segment_duration_sec, args.poll_interval_sec, args.retention_hours) <= 0:
            raise ValueError("durations and retention must be positive")
        return run(args)
    return healthcheck(args)


if __name__ == "__main__":
    raise SystemExit(main())
