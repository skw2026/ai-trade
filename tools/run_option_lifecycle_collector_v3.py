#!/usr/bin/env python3
"""Run restartable, rotating Bybit BTC option sticky-lifecycle captures."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any, Dict, Sequence

import capture_bybit_option_lifecycle_v3 as collector
import prune_microstructure_capture as retention


HEALTH_SCHEMA_VERSION = "option_lifecycle_collector_health_v3"
LATEST_SCHEMA_VERSION = "option_lifecycle_latest_segment_v3"
CAPTURE_SCRIPT_NAME = "capture_bybit_option_lifecycle_v3.py"
_ACTIVE_PROCESS: subprocess.Popen[Any] | None = None
_SHUTDOWN_REQUESTED = False


def _request_shutdown(signum: int, _frame: Any) -> None:
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = True
    if _ACTIVE_PROCESS is not None and _ACTIVE_PROCESS.poll() is None:
        _ACTIVE_PROCESS.send_signal(signum)


def run_segment(command: Sequence[str]) -> None:
    global _ACTIVE_PROCESS
    process = subprocess.Popen(command)
    _ACTIVE_PROCESS = process
    try:
        return_code = process.wait()
    finally:
        _ACTIVE_PROCESS = None
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)


def atomic_write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    collector.atomic_write_json(path, payload)


def utc_segment_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def segment_command(args: argparse.Namespace, *, root: pathlib.Path,
                    duration_sec: float) -> tuple[Sequence[str], pathlib.Path]:
    segment_id = utc_segment_id()
    raw = root / "raw" / collector.BASE_COIN / f"{segment_id}.jsonl.xz"
    features = root / "features" / collector.BASE_COIN / f"{segment_id}.csv"
    report = root / "reports" / collector.BASE_COIN / f"{segment_id}.json"
    return ([
        sys.executable,
        str(pathlib.Path(__file__).resolve().parent / CAPTURE_SCRIPT_NAME),
        "--raw", str(raw), "--features", str(features), "--report", str(report),
        "--state", str(root / "tracking_state.json"), "--capture-root", str(root),
        "--policy", str(pathlib.Path(args.policy).resolve()),
        "--manifest", str(pathlib.Path(args.manifest).resolve()),
        "--duration-sec", str(duration_sec),
        "--poll-interval-sec", str(args.poll_interval_sec), "--base-url", args.base_url,
    ], report)


def _health_payload(*, state: str, policy: Dict[str, Any], manifest: Dict[str, Any],
                    segment_started_epoch_ms: int, consecutive_failures: int,
                    **extra: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "state": state,
        "experiment_id": policy["experiment_id"],
        "capture_schema_version": collector.SCHEMA_VERSION,
        "snapshot_schema_version": collector.SNAPSHOT_SCHEMA_VERSION,
        "state_schema_version": collector.STATE_SCHEMA_VERSION,
        "policy_canonical_sha256": collector.canonical_sha256(policy),
        "manifest_canonical_sha256": collector.canonical_sha256(manifest),
        "raw_codec": collector.RAW_CODEC,
        "base_coin": collector.BASE_COIN,
        "settle_coin": collector.SETTLE_COIN,
        "segment_started_epoch_ms": segment_started_epoch_ms,
        "consecutive_failures": consecutive_failures,
    }
    payload.update(extra)
    return payload


def run(args: argparse.Namespace) -> int:
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = False
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    policy, manifest = collector.load_contract(pathlib.Path(args.policy), pathlib.Path(args.manifest))
    root = pathlib.Path(args.root).resolve()
    if root.name != collector.CAPTURE_ROOT_NAME:
        raise ValueError("v3 lifecycle collector root mismatch")
    root.mkdir(parents=True, exist_ok=True)
    health, latest = root / "collector_health.json", root / "latest_segment.json"
    completed = successes = failures = 0
    while args.max_segments <= 0 or completed < args.max_segments:
        duration = args.bootstrap_segment_duration_sec if completed == 0 else args.segment_duration_sec
        command, report_path = segment_command(args, root=root, duration_sec=duration)
        started = int(time.time() * 1000)
        atomic_write_json(health, _health_payload(
            state="capturing", policy=policy, manifest=manifest,
            segment_started_epoch_ms=started, consecutive_failures=failures,
        ))
        try:
            run_segment(command)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") != "PASS":
                raise RuntimeError("v3 lifecycle capture report did not pass")
            completed_at, failures = int(time.time() * 1000), 0
            relative = report_path.relative_to(root)
            state_payload = collector.load_state(
                root / "tracking_state.json", policy=policy, manifest=manifest
            )
            active = state_payload.get("active_lifecycle")
            atomic_write_json(latest, {
                "schema_version": LATEST_SCHEMA_VERSION,
                "report": relative.as_posix(), "report_payload": report,
                "completed_epoch_ms": completed_at,
                "active_lifecycle_id": active.get("lifecycle_id") if active else None,
                "state_revision": int(state_payload["revision"]),
            })
            atomic_write_json(health, _health_payload(
                state="healthy", policy=policy, manifest=manifest,
                segment_started_epoch_ms=started, consecutive_failures=0,
                last_success_epoch_ms=completed_at, latest_report=relative.as_posix(),
                active_lifecycle_id=active.get("lifecycle_id") if active else None,
                state_revision=int(state_payload["revision"]),
            ))
            retention.prune_capture_root(
                root, retention_seconds=args.retention_hours * 3600,
                expected_root_name=collector.CAPTURE_ROOT_NAME,
            )
            successes += 1
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError,
                subprocess.CalledProcessError) as exc:
            failures += 1
            atomic_write_json(health, _health_payload(
                state="degraded", policy=policy, manifest=manifest,
                segment_started_epoch_ms=started, consecutive_failures=failures,
                last_failure_epoch_ms=int(time.time() * 1000), error=str(exc),
            ))
            if args.max_segments <= 0:
                time.sleep(min(float(args.max_backoff_sec), 2.0 ** min(failures, 6)))
        completed += 1
        if _SHUTDOWN_REQUESTED:
            break
    return 0 if successes > 0 else 2


def healthcheck(args: argparse.Namespace) -> int:
    try:
        policy, manifest = collector.load_contract(pathlib.Path(args.policy), pathlib.Path(args.manifest))
        payload = json.loads((pathlib.Path(args.root) / "collector_health.json").read_text(encoding="utf-8"))
        reference = int(payload.get("last_success_epoch_ms") or payload.get("segment_started_epoch_ms") or 0)
        age_ms = int(time.time() * 1000) - reference
        valid = bool(
            payload.get("schema_version") == HEALTH_SCHEMA_VERSION
            and payload.get("experiment_id") == policy["experiment_id"]
            and payload.get("capture_schema_version") == collector.SCHEMA_VERSION
            and payload.get("snapshot_schema_version") == collector.SNAPSHOT_SCHEMA_VERSION
            and payload.get("state_schema_version") == collector.STATE_SCHEMA_VERSION
            and payload.get("policy_canonical_sha256") == collector.canonical_sha256(policy)
            and payload.get("manifest_canonical_sha256") == collector.canonical_sha256(manifest)
            and payload.get("raw_codec") == collector.RAW_CODEC
            and payload.get("state") in {"capturing", "healthy"}
            and 0 <= age_ms <= args.max_stale_sec * 1000
        )
        return 0 if valid else 1
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "healthcheck"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True)
        command.add_argument("--policy", required=True)
        command.add_argument("--manifest", required=True)
        if name == "run":
            command.add_argument("--segment-duration-sec", type=float, default=905.0)
            command.add_argument("--bootstrap-segment-duration-sec", type=float, default=65.0)
            command.add_argument("--poll-interval-sec", type=float, default=60.0)
            command.add_argument("--retention-hours", type=int, default=960)
            command.add_argument("--max-backoff-sec", type=int, default=60)
            command.add_argument("--max-segments", type=int, default=0)
            command.add_argument("--base-url", default=collector.BASE_URL)
        else:
            command.add_argument("--max-stale-sec", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.action == "run":
        if min(args.segment_duration_sec, args.bootstrap_segment_duration_sec,
               args.poll_interval_sec, args.retention_hours) <= 0:
            raise ValueError("durations and retention must be positive")
        return run(args)
    return healthcheck(args)


if __name__ == "__main__":
    raise SystemExit(main())
