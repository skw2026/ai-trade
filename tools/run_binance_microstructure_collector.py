#!/usr/bin/env python3
"""Run restartable rotating Binance SOLUSDT microstructure captures."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, Sequence

import collect_binance_microstructure as collector
import prune_microstructure_capture as retention


SCHEMA_VERSION = "binance_microstructure_collector_health_v1"


def atomic_write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def utc_segment_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def segment_command(
    *, root: pathlib.Path, duration_sec: float, public_url: str, market_url: str
) -> tuple[Sequence[str], pathlib.Path]:
    segment_id = utc_segment_id()
    raw = root / "raw" / collector.SYMBOL / f"{segment_id}.jsonl.gz"
    features = root / "features" / collector.SYMBOL / f"{segment_id}.csv"
    report = root / "reports" / collector.SYMBOL / f"{segment_id}.json"
    return (
        [
            sys.executable,
            str(pathlib.Path(__file__).resolve().parent / "collect_binance_microstructure.py"),
            "live",
            "--raw",
            str(raw),
            "--features",
            str(features),
            "--report",
            str(report),
            "--duration-sec",
            str(duration_sec),
            "--public-url",
            public_url,
            "--market-url",
            market_url,
            "--research-domain",
            "development",
        ],
        report,
    )


def prune(root: pathlib.Path, retention_days: int) -> None:
    retention.prune_capture_root(
        root,
        retention_seconds=retention_days * 86400,
        now_epoch=time.time(),
    )


def run(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    health = root / "collector_health.json"
    latest = root / "latest_segment.json"
    completed = successes = failures = 0
    while args.max_segments <= 0 or completed < args.max_segments:
        duration = (
            args.bootstrap_segment_duration_sec
            if completed == 0
            else args.segment_duration_sec
        )
        command, report_path = segment_command(
            root=root,
            duration_sec=duration,
            public_url=args.public_url,
            market_url=args.market_url,
        )
        started = int(time.time() * 1000)
        atomic_write_json(
            health,
            {
                "schema_version": SCHEMA_VERSION,
                "state": "capturing",
                "symbol": collector.SYMBOL,
                "capture_schema_version": collector.SCHEMA_VERSION,
                "segment_started_epoch_ms": started,
                "consecutive_failures": failures,
            },
        )
        try:
            subprocess.run(command, check=True)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") != "PASS":
                raise RuntimeError("capture report did not pass")
            completed_at = int(time.time() * 1000)
            failures = 0
            successes += 1
            relative = report_path.relative_to(root)
            atomic_write_json(
                latest,
                {
                    "schema_version": "binance_microstructure_latest_segment_v1",
                    "symbol": collector.SYMBOL,
                    "capture_schema_version": collector.SCHEMA_VERSION,
                    "report": str(relative),
                    "report_payload": report,
                    "completed_epoch_ms": completed_at,
                },
            )
            atomic_write_json(
                health,
                {
                    "schema_version": SCHEMA_VERSION,
                    "state": "healthy",
                    "symbol": collector.SYMBOL,
                    "capture_schema_version": collector.SCHEMA_VERSION,
                    "segment_started_epoch_ms": started,
                    "last_success_epoch_ms": completed_at,
                    "consecutive_failures": 0,
                    "latest_report": str(relative),
                },
            )
            prune(root, args.retention_days)
        except (
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
            subprocess.CalledProcessError,
        ) as exc:
            failures += 1
            atomic_write_json(
                health,
                {
                    "schema_version": SCHEMA_VERSION,
                    "state": "degraded",
                    "symbol": collector.SYMBOL,
                    "capture_schema_version": collector.SCHEMA_VERSION,
                    "segment_started_epoch_ms": started,
                    "last_failure_epoch_ms": int(time.time() * 1000),
                    "consecutive_failures": failures,
                    "error": str(exc),
                },
            )
            if args.max_segments <= 0:
                time.sleep(min(float(args.max_backoff_sec), 2.0 ** min(failures, 6)))
        completed += 1
    return 0 if successes > 0 else 2


def healthcheck(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(
            (pathlib.Path(args.root) / "collector_health.json").read_text(encoding="utf-8")
        )
        reference = int(
            payload.get("last_success_epoch_ms")
            or payload.get("segment_started_epoch_ms")
            or 0
        )
        age_ms = int(time.time() * 1000) - reference
        valid = bool(
            payload.get("schema_version") == SCHEMA_VERSION
            and payload.get("capture_schema_version") == collector.SCHEMA_VERSION
            and payload.get("symbol") == collector.SYMBOL
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
    runner.add_argument("--retention-days", type=int, default=3)
    runner.add_argument("--max-backoff-sec", type=int, default=60)
    runner.add_argument("--max-segments", type=int, default=0)
    runner.add_argument("--public-url", default=collector.PUBLIC_URL)
    runner.add_argument("--market-url", default=collector.MARKET_URL)
    check = commands.add_parser("healthcheck")
    check.add_argument("--root", required=True)
    check.add_argument("--max-stale-sec", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.action == "run":
        if min(
            args.segment_duration_sec,
            args.bootstrap_segment_duration_sec,
            args.retention_days,
        ) <= 0:
            raise ValueError("durations and retention must be positive")
        return run(args)
    return healthcheck(args)


if __name__ == "__main__":
    raise SystemExit(main())
