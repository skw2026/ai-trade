#!/usr/bin/env python3
"""Persist bounded, secret-free deployment state for failed CD diagnosis."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SCHEMA_VERSION = "ai_trade_deployment_diagnostics_v1"
MAX_EVENTS = 32


def inspect_container(name: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "exists": False,
        "state": "missing",
        "running": False,
        "health_status": None,
        "exit_code": None,
        "oom_killed": None,
        "restart_count": None,
        "image_ref": None,
        "image_id": None,
    }
    try:
        completed = subprocess.run(
            ["docker", "inspect", name],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        result["inspect_result"] = "docker_unavailable"
        return result
    if completed.returncode != 0:
        result["inspect_result"] = "not_found"
        return result
    try:
        payload = json.loads(completed.stdout)
        details = payload[0]
        if not isinstance(details, dict):
            raise TypeError("docker inspect item is not an object")
    except (IndexError, TypeError, json.JSONDecodeError):
        result["inspect_result"] = "invalid_response"
        return result

    state = details.get("State") or {}
    health = state.get("Health") or {}
    config = details.get("Config") or {}
    result.update(
        {
            "exists": True,
            "state": str(state.get("Status") or "unknown"),
            "running": bool(state.get("Running")),
            "health_status": health.get("Status"),
            "exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "restart_count": details.get("RestartCount"),
            "image_ref": config.get("Image"),
            "image_id": details.get("Image"),
            "inspect_result": "ok",
        }
    )
    return result


def _load_events(output_path: Path, run_id: str) -> list[dict[str, Any]]:
    if not output_path.is_file():
        return []
    try:
        previous = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return []
    events = previous.get("events")
    if (
        previous.get("schema_version") != SCHEMA_VERSION
        or previous.get("run_id") != run_id
        or not isinstance(events, list)
    ):
        return []
    return [event for event in events if isinstance(event, dict)]


def _current_release(current_link: Path) -> str:
    if not (current_link.exists() or current_link.is_symlink()):
        return ""
    return os.path.realpath(current_link)


def write_diagnostics(args: argparse.Namespace) -> dict[str, Any]:
    output_path = Path(args.output)
    event = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "phase": args.phase,
        "status": args.status,
        "reason": args.reason,
        "containers": [inspect_container(name) for name in args.container],
    }
    events = (_load_events(output_path, args.run_id) + [event])[-MAX_EVENTS:]
    document = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id,
        "phase": event["phase"],
        "status": event["status"],
        "reason": event["reason"],
        "release": {
            "release_id": args.release_id,
            "git_sha": args.git_sha,
            "target_release": args.target_release,
            "current_release": _current_release(Path(args.current_link)),
            "previous_release": args.previous_release,
            "compose_project": args.compose_project,
        },
        "events": events,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f"{output_path.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, output_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return document


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--release-id", default="")
    parser.add_argument("--git-sha", default="")
    parser.add_argument("--target-release", default="")
    parser.add_argument("--current-link", default="")
    parser.add_argument("--previous-release", default="")
    parser.add_argument("--compose-project", default="")
    parser.add_argument("--container", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    write_diagnostics(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
