#!/usr/bin/env python3
"""Verify that a routed demo candidate is bound to a fresh policy sidecar."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import tempfile
import time
from typing import Any, Dict, Mapping


SCHEMA_VERSION = "microstructure_demo_binding_v1"


def is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(args: argparse.Namespace) -> Dict[str, Any]:
    route_path = pathlib.Path(args.route_report).resolve()
    lifecycle_path = pathlib.Path(args.lifecycle_report).resolve()
    health_path = pathlib.Path(args.health).resolve()
    signal_path = pathlib.Path(args.signal).resolve()
    route = read_json(route_path)
    lifecycle = read_json(lifecycle_path)
    health = read_json(health_path)
    signal = read_json(signal_path)
    now_ms = int(args.now_epoch_ms or time.time() * 1000)
    failures = []
    candidate_id = str(lifecycle.get("candidate_id") or "")
    lifecycle_state = lifecycle.get("state", {})
    if not isinstance(lifecycle_state, dict):
        lifecycle_state = {}
    lifecycle_registry = lifecycle.get("registry", {})
    if not isinstance(lifecycle_registry, dict):
        lifecycle_registry = {}
    lifecycle_artifacts = lifecycle_state.get("artifacts", {})
    if not isinstance(lifecycle_artifacts, dict):
        lifecycle_artifacts = {}
    development_reference = lifecycle_artifacts.get("development_report", {})
    model_reference = lifecycle_artifacts.get("model", {})
    if not isinstance(development_reference, dict):
        development_reference = {}
    if not isinstance(model_reference, dict):
        model_reference = {}
    route_evidence = route.get("sources", {}).get("microstructure_demo", {}).get(
        "evidence", {}
    )
    if not (
        route.get("schema_version") == "alpha_source_route_v1"
        and route.get("status") == "PASS"
        and route.get("selected_route") == "microstructure_demo"
        and route.get("live_promotion_eligible") is False
    ):
        failures.append("alpha source route is not microstructure_demo")
    if not (
        route_evidence.get("sha256") == sha256_file(lifecycle_path)
        and lifecycle.get("schema_version")
        == "microstructure_alpha_lifecycle_v1"
        and lifecycle.get("status") == "PASS"
        and lifecycle.get("fully_verifiable") is True
        and lifecycle.get("phase") == "demo_ready"
        and lifecycle.get("promotion_eligible") is False
        and lifecycle.get("demo_entry_eligible") is True
        and lifecycle.get("live_promotion_eligible") is False
        and is_sha256(candidate_id)
        and lifecycle_state.get("candidate_id") == candidate_id
        and lifecycle_state.get("phase") == "demo_ready"
    ):
        failures.append("demo-ready lifecycle identity mismatch")
    health_age_ms = now_ms - int(health.get("last_heartbeat_epoch_ms") or 0)
    if not (
        health.get("schema_version") == "microstructure_demo_policy_health_v1"
        and health.get("state") == "active"
        and health.get("candidate_id") == candidate_id
        and 0 <= health_age_ms <= int(args.max_stale_ms)
    ):
        failures.append("microstructure demo sidecar health is stale or unbound")
    signal_age_ms = now_ms - int(signal.get("generated_at_epoch_ms") or 0)
    try:
        signal_exchange_timestamp_ms = int(
            signal.get("exchange_timestamp_ms") or 0
        )
    except (TypeError, ValueError):
        signal_exchange_timestamp_ms = 0
    signal_exchange_age_ms = now_ms - signal_exchange_timestamp_ms
    active_contract_ok = True
    if signal.get("status") == "ACTIVE":
        action = signal.get("action", {})
        if not isinstance(action, dict):
            action = {}
        try:
            started_ms = int(action.get("started_exchange_ms") or 0)
            exchange_ms = int(signal.get("exchange_timestamp_ms") or 0)
            active_until_ms = int(signal.get("active_until_exchange_ms") or 0)
            latency_seconds = int(action.get("execution_latency_seconds") or 0)
            horizon_seconds = int(action.get("horizon_seconds") or 0)
            direction = int(action.get("direction") or 0)
        except (TypeError, ValueError):
            active_contract_ok = False
        else:
            active_contract_ok = bool(
                direction in {-1, 1}
                and latency_seconds >= 1
                and horizon_seconds >= 1
                and 0 < started_ms <= exchange_ms < active_until_ms
                and active_until_ms
                == started_ms + (latency_seconds + horizon_seconds) * 1000
            )
    if not (
        signal.get("schema_version") == "microstructure_demo_signal_v2"
        and signal.get("status") in {"ACTIVE", "FLAT"}
        and signal.get("source") == "bybit_public_websocket_v5"
        and signal.get("candidate_id") == candidate_id
        and signal.get("lifecycle_state_sha256")
        == lifecycle_registry.get("state_sha256")
        and signal.get("model_sha256") == model_reference.get("sha256")
        and signal.get("development_report_sha256")
        == development_reference.get("sha256")
        and signal.get("demo_entry_eligible") is True
        and signal.get("live_promotion_eligible") is False
        and is_sha256(signal.get("lifecycle_state_sha256"))
        and is_sha256(signal.get("model_sha256"))
        and is_sha256(signal.get("development_report_sha256"))
        and active_contract_ok
        and 0 <= signal_age_ms <= int(args.max_stale_ms)
        and -5_000 <= signal_exchange_age_ms <= int(args.max_stale_ms)
    ):
        failures.append("microstructure demo signal is stale, unsafe, or unbound")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if not failures else "FAIL",
        "candidate_id": candidate_id or None,
        "selected_route": route.get("selected_route"),
        "checked_at_utc": dt.datetime.fromtimestamp(
            now_ms / 1000.0, tz=dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_stale_ms": int(args.max_stale_ms),
        "health_age_ms": health_age_ms,
        "signal_age_ms": signal_age_ms,
        "signal_exchange_age_ms": signal_exchange_age_ms,
        "signal_status": signal.get("status"),
        "artifacts": {
            "alpha_source_route": {
                "path": str(route_path), "sha256": sha256_file(route_path)
            },
            "microstructure_lifecycle": {
                "path": str(lifecycle_path), "sha256": sha256_file(lifecycle_path)
            },
            "sidecar_health": {
                "path": str(health_path), "sha256": sha256_file(health_path)
            },
            "demo_signal": {
                "path": str(signal_path), "sha256": sha256_file(signal_path)
            },
        },
        "failures": failures,
        "demo_entry_eligible": not failures,
        "live_promotion_eligible": False,
    }


def atomic_write(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route-report", required=True)
    parser.add_argument("--lifecycle-report", required=True)
    parser.add_argument("--health", required=True)
    parser.add_argument("--signal", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-stale-ms", type=int, default=10_000)
    parser.add_argument("--now-epoch-ms", type=int, default=0)
    args = parser.parse_args()
    if args.max_stale_ms <= 0:
        parser.error("max-stale-ms must be positive")
    return args


def main() -> int:
    args = parse_args()
    try:
        payload = verify(args)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAIL",
            "failures": [str(exc)],
            "demo_entry_eligible": False,
            "live_promotion_eligible": False,
        }
    atomic_write(pathlib.Path(args.output).resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
