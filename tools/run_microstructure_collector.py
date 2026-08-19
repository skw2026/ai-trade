#!/usr/bin/env python3
"""Run restartable, rotating Bybit microstructure capture segments."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, Sequence

import collect_bybit_microstructure as collector
import prune_microstructure_capture as retention


SCHEMA_VERSION = "microstructure_collector_health_v1"


def atomic_write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def utc_segment_id(now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now(dt.timezone.utc)
    return current.strftime("%Y%m%dT%H%M%S.%fZ")


def prune_old_segments(root: pathlib.Path, retention_days: int, now_epoch: float) -> None:
    if retention_days <= 0:
        return
    retention.prune_capture_root(
        root,
        retention_seconds=retention_days * 86400,
        now_epoch=now_epoch,
    )


def segment_command(
    *,
    root: pathlib.Path,
    symbol: str,
    context_symbols: Sequence[str],
    duration_sec: float,
    url: str,
) -> tuple[Sequence[str], pathlib.Path]:
    segment_id = utc_segment_id()
    raw = root / "raw" / symbol / f"{segment_id}.jsonl.gz"
    features = root / "features" / symbol / f"{segment_id}.csv"
    report = root / "reports" / symbol / f"{segment_id}.json"
    command = [
        sys.executable,
        str(pathlib.Path(__file__).resolve().parent / "collect_bybit_microstructure.py"),
        "live",
        "--raw",
        str(raw),
        "--features",
        str(features),
        "--report",
        str(report),
        "--symbol",
        symbol,
        "--context-symbols",
        ",".join(context_symbols),
        "--duration-sec",
        str(duration_sec),
        "--url",
        url,
        "--research-domain",
        "development",
    ]
    return command, report


def run(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    health_path = root / "collector_health.json"
    latest_path = root / "latest_segment.json"
    consecutive_failures = 0
    completed = 0
    successful = 0
    while args.max_segments <= 0 or completed < args.max_segments:
        duration_sec = (
            args.bootstrap_segment_duration_sec
            if completed == 0
            else args.segment_duration_sec
        )
        command, report_path = segment_command(
            root=root,
            symbol=args.symbol,
            context_symbols=args.context_symbols,
            duration_sec=duration_sec,
            url=args.url,
        )
        started_ms = int(time.time() * 1000)
        atomic_write_json(
            health_path,
            {
                "schema_version": SCHEMA_VERSION,
                "state": "capturing",
                "symbol": args.symbol,
                "symbols": [args.symbol, *args.context_symbols],
                "capture_schema_version": collector.SCHEMA_VERSION,
                "segment_started_epoch_ms": started_ms,
                "consecutive_failures": consecutive_failures,
            },
        )
        try:
            subprocess.run(command, check=True)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") != "PASS":
                raise RuntimeError("capture report did not pass")
            relative_report = report_path.relative_to(root)
            completed_ms = int(time.time() * 1000)
            consecutive_failures = 0
            successful += 1
            atomic_write_json(
                latest_path,
                {
                    "schema_version": "microstructure_latest_segment_v1",
                    "symbol": args.symbol,
                    "symbols": [args.symbol, *args.context_symbols],
                    "capture_schema_version": collector.SCHEMA_VERSION,
                    "report": str(relative_report),
                    "report_payload": report,
                    "completed_epoch_ms": completed_ms,
                },
            )
            atomic_write_json(
                health_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "state": "healthy",
                    "symbol": args.symbol,
                    "symbols": [args.symbol, *args.context_symbols],
                    "capture_schema_version": collector.SCHEMA_VERSION,
                    "segment_started_epoch_ms": started_ms,
                    "last_success_epoch_ms": completed_ms,
                    "consecutive_failures": 0,
                    "latest_report": str(relative_report),
                },
            )
            prune_old_segments(root, args.retention_days, time.time())
        except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
            consecutive_failures += 1
            atomic_write_json(
                health_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "state": "degraded",
                    "symbol": args.symbol,
                    "symbols": [args.symbol, *args.context_symbols],
                    "capture_schema_version": collector.SCHEMA_VERSION,
                    "segment_started_epoch_ms": started_ms,
                    "last_failure_epoch_ms": int(time.time() * 1000),
                    "consecutive_failures": consecutive_failures,
                    "error": str(exc),
                },
            )
            if args.max_segments <= 0:
                time.sleep(min(float(args.max_backoff_sec), 2.0 ** min(consecutive_failures, 6)))
        completed += 1
    return 0 if successful > 0 else 2


def healthcheck(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.root) / "collector_health.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        reference_epoch_ms = int(
            payload.get("last_success_epoch_ms")
            or payload.get("segment_started_epoch_ms")
            or 0
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1
    age_ms = int(time.time() * 1000) - reference_epoch_ms
    contract_ok = bool(
        payload.get("schema_version") == SCHEMA_VERSION
        and payload.get("capture_schema_version") == collector.SCHEMA_VERSION
        and payload.get("symbol") == collector.TARGET_SYMBOL
        and payload.get("symbols") == list(collector.CAPTURE_SYMBOLS)
    )
    return 0 if contract_ok and payload.get("state") in {"healthy", "capturing"} and 0 <= age_ms <= args.max_stale_sec * 1000 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--root", required=True)
    run_parser.add_argument(
        "--symbol", default=collector.TARGET_SYMBOL, choices=(collector.TARGET_SYMBOL,)
    )
    run_parser.add_argument(
        "--context-symbols", default=",".join(collector.CONTEXT_SYMBOLS)
    )
    run_parser.add_argument("--segment-duration-sec", type=float, default=905.0)
    run_parser.add_argument(
        "--bootstrap-segment-duration-sec", type=float, default=65.0
    )
    run_parser.add_argument("--retention-days", type=int, default=3)
    run_parser.add_argument("--max-backoff-sec", type=int, default=60)
    run_parser.add_argument("--max-segments", type=int, default=0)
    run_parser.add_argument(
        "--url", default="wss://stream.bybit.com/v5/public/linear"
    )
    health_parser = subparsers.add_parser("healthcheck")
    health_parser.add_argument("--root", required=True)
    health_parser.add_argument("--max-stale-sec", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.action == "run":
        args.context_symbols = tuple(
            value.strip().upper()
            for value in args.context_symbols.split(",")
            if value.strip()
        )
        if args.context_symbols != collector.CONTEXT_SYMBOLS:
            raise ValueError("context-symbols must be BTCUSDT,ETHUSDT in that order")
        if (
            args.segment_duration_sec <= 0
            or args.bootstrap_segment_duration_sec <= 0
            or args.retention_days <= 0
        ):
            raise ValueError(
                "bootstrap/regular segment duration and retention days must be positive"
            )
        return run(args)
    return healthcheck(args)


if __name__ == "__main__":
    raise SystemExit(main())
