#!/usr/bin/env python3
"""Run the frozen microstructure policy on live public data for demo only.

The process never owns credentials and never submits orders.  It validates the
append-only lifecycle registry, loads only a ``demo_ready`` candidate, rebuilds
the exact causal features used by research, and atomically publishes a short-
lived target-position signal for the C++ risk/execution pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import dataclasses
import datetime as dt
import hashlib
import json
import math
import pathlib
import ssl
import tempfile
import time
from typing import Any, Deque, Dict, Iterable, Mapping, Sequence

import numpy as np

import collect_bybit_microstructure as collector
import run_microstructure_alpha_development as development
import run_microstructure_alpha_lifecycle as lifecycle


SIGNAL_SCHEMA_VERSION = "microstructure_demo_signal_v2"
HEALTH_SCHEMA_VERSION = "microstructure_demo_policy_health_v1"
DEFAULT_SIGNAL_MAX_STALE_MS = 10_000


class DemoPolicyError(RuntimeError):
    """The lifecycle or online-inference contract is unsafe to consume."""


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
    temporary.replace(path)


def artifact_path(
    registry_root: pathlib.Path,
    candidate_id: str,
    filename: str,
    reference: Mapping[str, Any],
) -> pathlib.Path:
    """Resolve an artifact locally instead of trusting container-specific paths."""
    path = (registry_root / "candidates" / candidate_id / filename).resolve()
    expected_parent = (registry_root / "candidates" / candidate_id).resolve()
    if path.parent != expected_parent:
        raise DemoPolicyError(f"candidate artifact escaped registry: {filename}")
    expected_hash = str(reference.get("sha256") or "")
    if len(expected_hash) != 64 or not path.is_file() or sha256_file(path) != expected_hash:
        raise DemoPolicyError(f"candidate artifact identity mismatch: {filename}")
    return path


@dataclasses.dataclass
class CandidateBundle:
    candidate_id: str
    state_sha256: str
    model_sha256: str
    development_report_sha256: str
    feature_names: list[str]
    actions: list[Dict[str, Any]]
    threshold_bps: float
    execution_latency_seconds: int
    report: Dict[str, Any]
    model: Any


def load_demo_candidate(registry_root: pathlib.Path) -> CandidateBundle | None:
    registry_root = registry_root.resolve()
    paths = lifecycle.RegistryPaths(registry_root)
    events = lifecycle.read_event_chain(paths)
    if not events:
        return None
    state = events[-1].get("state")
    if not isinstance(state, dict):
        raise DemoPolicyError("lifecycle event has no state")
    if state.get("phase") != "demo_ready":
        return None
    if state.get("demo_entry_eligible") is not True:
        raise DemoPolicyError("demo_ready state is not demo-entry eligible")
    if state.get("live_promotion_eligible") is not False:
        raise DemoPolicyError("microstructure candidate must remain live-ineligible")
    candidate_id = str(state.get("candidate_id") or "")
    if len(candidate_id) != 64 or any(ch not in "0123456789abcdef" for ch in candidate_id):
        raise DemoPolicyError("demo candidate id is not a lowercase SHA-256")
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, dict):
        raise DemoPolicyError("demo candidate artifact map is missing")
    report_ref = artifacts.get("development_report")
    manifest_ref = artifacts.get("candidate_manifest")
    model_ref = artifacts.get("model")
    if not all(isinstance(item, dict) for item in (report_ref, manifest_ref, model_ref)):
        raise DemoPolicyError("demo candidate artifact reference is incomplete")
    report_path = artifact_path(
        registry_root, candidate_id, "development_report.json", report_ref
    )
    manifest_path = artifact_path(
        registry_root, candidate_id, "candidate_manifest.json", manifest_ref
    )
    model_path = artifact_path(registry_root, candidate_id, "model.cbm", model_ref)
    validated = lifecycle.validate_development_candidate(
        report_path, manifest_path, model_path
    )
    if validated.get("candidate_id") != candidate_id:
        raise DemoPolicyError("validated development candidate id mismatch")

    replay_ref = state.get("evidence", {}).get("raw_replay_passed")
    if not isinstance(replay_ref, dict):
        raise DemoPolicyError("demo candidate has no raw replay evidence")
    replay_path = artifact_path(
        registry_root, candidate_id, "raw_replay_report.json", replay_ref
    )
    replay = lifecycle.read_json_object(replay_path)
    if not (
        replay.get("status") == "PASS"
        and replay.get("candidate_id") == candidate_id
        and replay.get("raw_to_feature_parity") is True
        and replay.get("fixed_model_prediction_economics_deterministic") is True
        and replay.get("demo_entry_eligible") is True
        and replay.get("live_promotion_eligible") is False
    ):
        raise DemoPolicyError("raw replay demo-entry contract failed")

    report = lifecycle.read_json_object(report_path)
    feature_names = report.get("data", {}).get("feature_names")
    actions, _ = lifecycle.action_contract(report)
    frozen = report.get("frozen_candidate", {})
    target = report.get("target_contract", {})
    threshold = float(frozen.get("policy_threshold_bps"))
    latency = int(target.get("execution_latency_seconds") or 0)
    if not isinstance(feature_names, list) or not feature_names:
        raise DemoPolicyError("demo feature contract is empty")
    if not math.isfinite(threshold) or latency < 1:
        raise DemoPolicyError("demo threshold/latency contract is invalid")
    return CandidateBundle(
        candidate_id=candidate_id,
        state_sha256=development.canonical_sha256(state),
        model_sha256=str(model_ref["sha256"]),
        development_report_sha256=str(report_ref["sha256"]),
        feature_names=[str(name) for name in feature_names],
        actions=actions,
        threshold_bps=threshold,
        execution_latency_seconds=latency,
        report=report,
        model=lifecycle.load_frozen_model(model_path),
    )


def series_from_rows(rows: Iterable[Mapping[str, Any]]) -> Dict[str, np.ndarray]:
    normalized = list(rows)
    if not normalized:
        raise DemoPolicyError("online feature history is empty")
    timestamps = np.asarray([int(row["timestamp"]) for row in normalized], dtype=np.int64)
    if np.any(np.diff(timestamps) <= 0):
        raise DemoPolicyError("online feature timestamps are not strictly increasing")
    return {
        "timestamp": timestamps,
        **{
            name: np.asarray([float(row[name]) for row in normalized], dtype=np.float64)
            for name in development.REQUIRED_FIELDS[1:]
        },
    }


class DemoPolicyEngine:
    def __init__(
        self,
        *,
        signal_output: pathlib.Path,
        history_seconds: int = 900,
    ) -> None:
        self.signal_output = signal_output
        self.history: Deque[Dict[str, Any]] = collections.deque(
            maxlen=max(120, int(history_seconds))
        )
        self.candidate: CandidateBundle | None = None
        self.active_action: Dict[str, Any] | None = None
        self.active_until_exchange_ms = 0

    def set_candidate(self, candidate: CandidateBundle | None) -> None:
        previous_id = self.candidate.candidate_id if self.candidate else None
        next_id = candidate.candidate_id if candidate else None
        if previous_id != next_id:
            self.active_action = None
            self.active_until_exchange_ms = 0
        self.candidate = candidate

    def _payload(
        self,
        *,
        status: str,
        exchange_timestamp_ms: int,
        action: Mapping[str, Any] | None,
        reason: str,
    ) -> Dict[str, Any]:
        candidate = self.candidate
        now_ms = int(time.time() * 1000)
        return {
            "schema_version": SIGNAL_SCHEMA_VERSION,
            "status": status,
            "source": "bybit_public_websocket_v5",
            "symbol": "SOLUSDT",
            "generated_at_epoch_ms": now_ms,
            "exchange_timestamp_ms": int(exchange_timestamp_ms),
            "candidate_id": candidate.candidate_id if candidate else None,
            "lifecycle_state_sha256": candidate.state_sha256 if candidate else None,
            "model_sha256": candidate.model_sha256 if candidate else None,
            "development_report_sha256": (
                candidate.development_report_sha256 if candidate else None
            ),
            "action": dict(action) if action is not None else None,
            "active_until_exchange_ms": int(self.active_until_exchange_ms),
            "reason": reason,
            "demo_entry_eligible": candidate is not None,
            "live_promotion_eligible": False,
        }

    def publish_fail_closed(self, reason: str, exchange_timestamp_ms: int = 0) -> None:
        self.active_action = None
        self.active_until_exchange_ms = 0
        atomic_write_json(
            self.signal_output,
            self._payload(
                status="FAIL_CLOSED",
                exchange_timestamp_ms=exchange_timestamp_ms,
                action=None,
                reason=reason,
            ),
        )

    def on_row(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        parsed = {"timestamp": int(row["timestamp"])}
        for name in development.REQUIRED_FIELDS[1:]:
            value = float(row[name])
            if not math.isfinite(value):
                raise DemoPolicyError(f"online feature is non-finite: {name}")
            parsed[name] = value
        if self.history and parsed["timestamp"] <= self.history[-1]["timestamp"]:
            raise DemoPolicyError("online feature row is duplicate or regressed")
        self.history.append(parsed)
        timestamp = int(parsed["timestamp"])
        if self.candidate is None:
            payload = self._payload(
                status="NOT_READY",
                exchange_timestamp_ms=timestamp,
                action=None,
                reason="lifecycle_candidate_not_demo_ready",
            )
            atomic_write_json(self.signal_output, payload)
            return payload

        if self.active_action is not None and timestamp < self.active_until_exchange_ms:
            payload = self._payload(
                status="ACTIVE",
                exchange_timestamp_ms=timestamp,
                action=self.active_action,
                reason="frozen_action_holding_window",
            )
            atomic_write_json(self.signal_output, payload)
            return payload
        self.active_action = None
        self.active_until_exchange_ms = 0

        series = series_from_rows(self.history)
        features, feature_names = development.build_causal_features(series)
        if feature_names != self.candidate.feature_names:
            raise DemoPolicyError("online/offline feature name-order parity failed")
        row_features = np.asarray(features[-1], dtype=np.float64)
        if not np.all(np.isfinite(row_features)):
            payload = self._payload(
                status="FLAT",
                exchange_timestamp_ms=timestamp,
                action=None,
                reason="causal_feature_warmup",
            )
            atomic_write_json(self.signal_output, payload)
            return payload
        raw_prediction = np.asarray(
            self.candidate.model.predict(row_features.reshape(1, -1)),
            dtype=np.float64,
        )
        try:
            prediction = development.reconstruct_base_net_scores(
                raw_prediction,
                self.candidate.report.get("frozen_candidate", {}).get(
                    "target_transform", {}
                ),
            ).reshape(-1)
        except ValueError as exc:
            raise DemoPolicyError("frozen model score reconstruction failed") from exc
        if prediction.shape != (len(self.candidate.actions),) or not np.all(
            np.isfinite(prediction)
        ):
            raise DemoPolicyError("frozen model online output contract failed")
        action_index = int(np.argmax(prediction))
        predicted_edge = float(prediction[action_index])
        if predicted_edge < self.candidate.threshold_bps:
            payload = self._payload(
                status="FLAT",
                exchange_timestamp_ms=timestamp,
                action=None,
                reason="predicted_edge_below_frozen_threshold",
            )
            atomic_write_json(self.signal_output, payload)
            return payload
        frozen_action = self.candidate.actions[action_index]
        direction = 1 if frozen_action["direction"] == "long" else -1
        horizon = int(frozen_action["horizon_seconds"])
        self.active_until_exchange_ms = timestamp + (
            self.candidate.execution_latency_seconds + horizon
        ) * 1000
        self.active_action = {
            "started_exchange_ms": timestamp,
            "direction": direction,
            "horizon_seconds": horizon,
            "action_index": action_index,
            "predicted_net_edge_bps": predicted_edge,
            "policy_threshold_bps": self.candidate.threshold_bps,
            "execution_latency_seconds": self.candidate.execution_latency_seconds,
        }
        payload = self._payload(
            status="ACTIVE",
            exchange_timestamp_ms=timestamp,
            action=self.active_action,
            reason="frozen_policy_threshold_passed",
        )
        atomic_write_json(self.signal_output, payload)
        return payload


class StreamingFeatureRows:
    """Finalize one-second buckets behind a one-second exchange watermark."""

    def __init__(self, symbol: str = "SOLUSDT", retention_seconds: int = 1200) -> None:
        self.aggregator = collector.MicrostructureAggregator(symbol=symbol, bucket_ms=1000)
        self.last_emitted_timestamp = -1
        self.retention_ms = max(120_000, int(retention_seconds) * 1000)

    def process(self, message: Mapping[str, Any]) -> list[Dict[str, Any]]:
        if not self.aggregator.process(message) or not self.aggregator.buckets:
            return []
        maximum_bucket = max(self.aggregator.buckets)
        watermark = maximum_bucket - 1000
        output = [
            dict(row)
            for row in self.aggregator.rows()
            if self.last_emitted_timestamp < int(row["timestamp"]) <= watermark
        ]
        if output:
            self.last_emitted_timestamp = int(output[-1]["timestamp"])
        prune_before = maximum_bucket - self.retention_ms
        for timestamp in list(self.aggregator.buckets):
            if timestamp < prune_before:
                del self.aggregator.buckets[timestamp]
        return output


def ssl_context() -> ssl.SSLContext:
    paths = ssl.get_default_verify_paths()
    fallback = pathlib.Path("/etc/ssl/cert.pem")
    return (
        ssl.create_default_context(cafile=str(fallback))
        if paths.cafile is None and fallback.is_file()
        else ssl.create_default_context()
    )


async def run_live(args: argparse.Namespace) -> int:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - research image contract
        raise DemoPolicyError("live demo policy requires websockets") from exc
    registry_root = pathlib.Path(args.registry_root).resolve()
    signal_output = pathlib.Path(args.signal_output).resolve()
    health_output = pathlib.Path(args.health_output).resolve()
    engine = DemoPolicyEngine(
        signal_output=signal_output, history_seconds=args.history_seconds
    )
    stream = StreamingFeatureRows(retention_seconds=args.history_seconds + 300)
    last_candidate_refresh = 0.0
    processed_messages = 0
    last_exchange_timestamp = 0

    async with websockets.connect(
        args.url,
        ssl=ssl_context(),
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=8 * 1024 * 1024,
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "args": ["orderbook.50.SOLUSDT", "publicTrade.SOLUSDT"],
                },
                separators=(",", ":"),
            )
        )
        while args.max_messages <= 0 or processed_messages < args.max_messages:
            now = time.monotonic()
            if now - last_candidate_refresh >= args.candidate_refresh_seconds:
                try:
                    engine.set_candidate(load_demo_candidate(registry_root))
                except (OSError, ValueError, TypeError, json.JSONDecodeError,
                        lifecycle.LifecycleError, DemoPolicyError) as exc:
                    # Never allow the previously loaded model to resume inference
                    # after a lifecycle/checksum refresh has failed.
                    engine.set_candidate(None)
                    engine.publish_fail_closed(
                        f"candidate_integrity_error:{exc}", last_exchange_timestamp
                    )
                    atomic_write_json(
                        health_output,
                        {
                            "schema_version": HEALTH_SCHEMA_VERSION,
                            "state": "degraded",
                            "last_heartbeat_epoch_ms": int(time.time() * 1000),
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(min(5.0, args.candidate_refresh_seconds))
                last_candidate_refresh = now
            raw = await asyncio.wait_for(socket.recv(), timeout=10.0)
            if not isinstance(raw, str):
                continue
            message = json.loads(raw)
            if not isinstance(message, dict) or not message.get("topic"):
                continue
            processed_messages += 1
            for row in stream.process(message):
                last_exchange_timestamp = int(row["timestamp"])
                try:
                    engine.on_row(row)
                except (ValueError, TypeError, DemoPolicyError) as exc:
                    engine.publish_fail_closed(
                        f"online_inference_error:{exc}", last_exchange_timestamp
                    )
            atomic_write_json(
                health_output,
                {
                    "schema_version": HEALTH_SCHEMA_VERSION,
                    "state": "active" if engine.candidate else "waiting_candidate",
                    "last_heartbeat_epoch_ms": int(time.time() * 1000),
                    "latest_exchange_timestamp_ms": last_exchange_timestamp,
                    "candidate_id": (
                        engine.candidate.candidate_id if engine.candidate else None
                    ),
                    "processed_message_count": processed_messages,
                    "live_promotion_eligible": False,
                },
            )
    return 0


def healthcheck(args: argparse.Namespace) -> int:
    now_ms = int(time.time() * 1000)
    try:
        health = json.loads(pathlib.Path(args.health_output).read_text(encoding="utf-8"))
        heartbeat = int(health.get("last_heartbeat_epoch_ms") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1
    age = now_ms - heartbeat
    return 0 if health.get("state") in {"active", "waiting_candidate"} and 0 <= age <= args.max_stale_ms else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--registry-root", required=True)
    run_parser.add_argument("--signal-output", required=True)
    run_parser.add_argument("--health-output", required=True)
    run_parser.add_argument("--history-seconds", type=int, default=900)
    run_parser.add_argument("--candidate-refresh-seconds", type=float, default=5.0)
    run_parser.add_argument("--max-messages", type=int, default=0)
    run_parser.add_argument("--url", default=collector.DEFAULT_URL)
    health_parser = subparsers.add_parser("healthcheck")
    health_parser.add_argument("--health-output", required=True)
    health_parser.add_argument("--max-stale-ms", type=int, default=DEFAULT_SIGNAL_MAX_STALE_MS)
    args = parser.parse_args()
    if args.action == "run" and (
        args.history_seconds < 120 or args.candidate_refresh_seconds <= 0.0
    ):
        parser.error("history/refresh settings are invalid")
    return args


def main() -> int:
    args = parse_args()
    if args.action == "healthcheck":
        return healthcheck(args)
    try:
        return asyncio.run(run_live(args))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, asyncio.TimeoutError,
            DemoPolicyError) as exc:
        atomic_write_json(
            pathlib.Path(args.health_output),
            {
                "schema_version": HEALTH_SCHEMA_VERSION,
                "state": "degraded",
                "last_heartbeat_epoch_ms": int(time.time() * 1000),
                "error": str(exc),
            },
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
