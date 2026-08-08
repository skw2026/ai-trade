#!/usr/bin/env python3
"""Advance a frozen microstructure candidate through future-only gates.

The development screen is allowed to create a candidate, but it is never
promotion evidence.  This tool copies a passing candidate into an immutable
registry and then evaluates that exact model and threshold on fixed, future
selection and final-holdout intervals.  The final replay gate regenerates the
holdout features from the checksum-bound raw websocket messages.  A candidate
can become eligible for demo incubation only after all three gates pass; this
tool never authorizes live-money promotion.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import dataclasses
import fcntl
import hashlib
import json
import math
import os
import pathlib
import shutil
import statistics
import tempfile
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Sequence, Tuple

import numpy as np

import collect_bybit_microstructure as collector
import run_microstructure_alpha_development as development


SCHEMA_VERSION = "microstructure_alpha_lifecycle_v1"
STATE_SCHEMA_VERSION = "microstructure_alpha_lifecycle_state_v1"
EVENT_SCHEMA_VERSION = "microstructure_alpha_lifecycle_event_v1"
CHECKPOINT_SCHEMA_VERSION = "microstructure_alpha_lifecycle_checkpoint_v1"
CANDIDATE_MANIFEST_SCHEMA_VERSION = "microstructure_alpha_candidate_manifest_v1"
TERMINAL_PHASES = {"rejected", "demo_ready"}
FROZEN_PHASES = {
    "selection_collecting",
    "holdout_collecting",
    "replay_pending",
    "demo_ready",
}


class LifecycleError(RuntimeError):
    """A lifecycle identity, integrity, or economic contract failed."""


class LifecycleNotReady(RuntimeError):
    """The fixed future domain does not yet contain enough data."""


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    return development.canonical_sha256(payload)


def read_json_object(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LifecycleError(f"JSON payload is not an object: {path}")
    return payload


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def copy_verified(source: pathlib.Path, destination: pathlib.Path, digest: str) -> None:
    if not source.is_file() or len(digest) != 64 or sha256_file(source) != digest:
        raise LifecycleError(f"source artifact identity mismatch: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = pathlib.Path(handle.name)
    try:
        shutil.copyfile(source, temporary)
        if sha256_file(temporary) != digest:
            raise LifecycleError(f"copied artifact identity mismatch: {source}")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def registry_lock(root: pathlib.Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "lifecycle.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclasses.dataclass(frozen=True)
class RegistryPaths:
    root: pathlib.Path

    @property
    def state(self) -> pathlib.Path:
        return self.root / "state.json"

    @property
    def ledger(self) -> pathlib.Path:
        return self.root / "events.jsonl"

    @property
    def checkpoint(self) -> pathlib.Path:
        return self.root / "checkpoint.json"

    def candidate_dir(self, candidate_id: str) -> pathlib.Path:
        if len(candidate_id) != 64 or any(ch not in "0123456789abcdef" for ch in candidate_id):
            raise LifecycleError("candidate id is not a lowercase SHA-256")
        path = (self.root / "candidates" / candidate_id).resolve()
        candidates_root = (self.root / "candidates").resolve()
        if path.parent != candidates_root:
            raise LifecycleError("candidate path escaped registry root")
        return path


def _event_hash_payload(event: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in event.items() if key != "event_hash"}


def read_event_chain(paths: RegistryPaths) -> List[Dict[str, Any]]:
    if not paths.ledger.is_file():
        if paths.checkpoint.exists() or paths.state.exists():
            raise LifecycleError("lifecycle ledger missing while checkpoint/state exists")
        return []
    events: List[Dict[str, Any]] = []
    previous_hash = ""
    with paths.ledger.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise LifecycleError(f"blank lifecycle ledger line: {line_number}")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LifecycleError(
                    f"invalid lifecycle ledger JSON at line {line_number}"
                ) from exc
            if not isinstance(event, dict):
                raise LifecycleError(f"lifecycle event is not an object: {line_number}")
            if event.get("schema_version") != EVENT_SCHEMA_VERSION:
                raise LifecycleError(f"lifecycle event schema mismatch: {line_number}")
            if int(event.get("sequence") or 0) != line_number:
                raise LifecycleError(f"lifecycle event sequence mismatch: {line_number}")
            if str(event.get("previous_event_hash") or "") != previous_hash:
                raise LifecycleError(f"lifecycle previous hash mismatch: {line_number}")
            actual_hash = canonical_sha256(_event_hash_payload(event))
            if str(event.get("event_hash") or "") != actual_hash:
                raise LifecycleError(f"lifecycle event hash mismatch: {line_number}")
            state = event.get("state")
            if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA_VERSION:
                raise LifecycleError(f"lifecycle event state invalid: {line_number}")
            events.append(event)
            previous_hash = actual_hash
    if not events:
        raise LifecycleError("lifecycle ledger is empty")
    if not paths.checkpoint.is_file():
        raise LifecycleError("lifecycle checkpoint missing")
    checkpoint = read_json_object(paths.checkpoint)
    expected_checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "event_count": len(events),
        "last_event_hash": previous_hash,
        "state_sha256": canonical_sha256(events[-1]["state"]),
    }
    if checkpoint != expected_checkpoint:
        raise LifecycleError("lifecycle checkpoint mismatch; deletion/truncation detected")
    if not paths.state.is_file():
        raise LifecycleError("lifecycle state missing")
    state = read_json_object(paths.state)
    if state != events[-1]["state"]:
        raise LifecycleError("lifecycle state does not match append-only ledger")
    return events


def append_transition(
    paths: RegistryPaths,
    events: Sequence[Mapping[str, Any]],
    *,
    transition: str,
    state: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> Dict[str, Any]:
    sequence = len(events) + 1
    previous_hash = str(events[-1].get("event_hash") or "") if events else ""
    event: Dict[str, Any] = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "sequence": sequence,
        "previous_event_hash": previous_hash,
        "transition": transition,
        "candidate_id": state.get("candidate_id"),
        "state": dict(state),
        "evidence": dict(evidence),
    }
    event["event_hash"] = canonical_sha256(event)
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    with paths.ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    atomic_write_json(paths.state, state)
    atomic_write_json(
        paths.checkpoint,
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "event_count": sequence,
            "last_event_hash": event["event_hash"],
            "state_sha256": canonical_sha256(state),
        },
    )
    return event


def artifact_ref(path: pathlib.Path) -> Dict[str, Any]:
    if not path.is_file():
        raise LifecycleError(f"candidate artifact missing: {path}")
    return {"path": str(path.resolve()), "sha256": sha256_file(path)}


def validate_development_candidate(
    report_path: pathlib.Path,
    manifest_path: pathlib.Path,
    model_path: pathlib.Path,
) -> Dict[str, Any]:
    report = read_json_object(report_path)
    manifest = read_json_object(manifest_path)
    if report.get("schema_version") != development.SCHEMA_VERSION:
        raise LifecycleError("development report schema mismatch")
    if manifest.get("schema_version") != CANDIDATE_MANIFEST_SCHEMA_VERSION:
        raise LifecycleError("development candidate manifest schema mismatch")
    if report.get("status") == "NOT_READY":
        if not (
            manifest.get("status") == "rejected"
            and manifest.get("candidate_id") is None
            and manifest.get("promotion_evidence") is False
            and manifest.get("promotion_eligible") is False
        ):
            raise LifecycleError(
                "not-ready development candidate manifest isolation contract failed"
            )
        reasons = report.get("failures")
        reason = (
            str(reasons[0]).strip()
            if isinstance(reasons, list) and reasons
            else "forward development capture is not ready"
        )
        raise LifecycleNotReady(f"development candidate not ready: {reason}")
    if not (
        report.get("status") == "PASS"
        and report.get("fully_verifiable") is True
        and report.get("research_domain") == "forward_development_only"
        and report.get("promotion_evidence") is False
        and report.get("promotion_eligible") is False
        and report.get("economic_screen", {}).get("development_passed") is True
        and report.get("negative_control", {}).get("method")
        == "deterministic_oos_prediction_time_permutation"
        and report.get("negative_control", {}).get("fully_verifiable") is True
        and report.get("negative_control", {}).get("passed") is True
        and int(report.get("negative_control", {}).get("trial_count") or 0) >= 5
        and report.get("validation_contract", {}).get("score_threshold_floor_bps")
        is None
        and report.get("validation_contract", {}).get(
            "negative_model_score_threshold_permitted"
        )
        is True
        and report.get("validation_contract", {}).get(
            "threshold_viability_contract"
        )
        == "realized_base_and_stress_net_lcb_positive_in_nested_validation"
    ):
        raise LifecycleError("development candidate has not passed its isolated economic gate")
    capture_merge = report.get("capture_merge_contract")
    capture_merge_audit = report.get("data", {}).get("capture_merge_audit")
    if not isinstance(capture_merge, dict) or capture_merge != development.CAPTURE_MERGE_CONTRACT:
        raise LifecycleError("development capture merge contract mismatch")
    try:
        development.validate_capture_merge_audit(capture_merge_audit)
    except ValueError as exc:
        raise LifecycleError("development capture merge audit contract failed") from exc
    if not (
        manifest.get("status") == "development_candidate_frozen"
        and manifest.get("research_domain") == "forward_development_only"
        and manifest.get("promotion_evidence") is False
        and manifest.get("promotion_eligible") is False
    ):
        raise LifecycleError("development candidate manifest isolation contract failed")
    candidate_id = str(manifest.get("candidate_id") or "")
    identity = manifest.get("identity_contract")
    if not isinstance(identity, dict) or canonical_sha256(identity) != candidate_id:
        raise LifecycleError("development candidate identity hash mismatch")
    expected_report_hash = str(manifest.get("development_report", {}).get("sha256") or "")
    if expected_report_hash != sha256_file(report_path):
        raise LifecycleError("development report checksum mismatch")
    frozen = report.get("frozen_candidate")
    if not isinstance(frozen, dict):
        raise LifecycleError("development report has no frozen model")
    frozen_identity = dict(frozen)
    frozen_identity.pop("model_path", None)
    expected_identity = {
        "source_assessment_sha256": report.get("source_assessment", {}).get("sha256"),
        "capture_merge_contract": capture_merge,
        "capture_merge_audit": capture_merge_audit,
        "target_contract": report.get("target_contract"),
        "validation_contract": report.get("validation_contract"),
        "feature_names": report.get("data", {}).get("feature_names"),
        "model_contract": report.get("model_contract"),
        "frozen_candidate": frozen_identity,
    }
    if identity != expected_identity:
        raise LifecycleError("development candidate identity contract mismatch")
    model_hash = str(frozen.get("model_sha256") or "")
    if len(model_hash) != 64 or not model_path.is_file() or sha256_file(model_path) != model_hash:
        raise LifecycleError("frozen development model checksum mismatch")
    cutoff_ms = int(report.get("source_assessment", {}).get("development_cutoff_ms") or 0)
    target = report.get("target_contract")
    validation = report.get("validation_contract")
    feature_names = report.get("data", {}).get("feature_names")
    if cutoff_ms <= 0 or not isinstance(target, dict) or not isinstance(validation, dict):
        raise LifecycleError("development cutoff/contract is incomplete")
    if not isinstance(feature_names, list) or not feature_names:
        raise LifecycleError("development feature contract is incomplete")
    actions = target.get("actions")
    if not isinstance(actions, list) or not actions:
        raise LifecycleError("development action contract is incomplete")
    horizons = [int(item.get("horizon_seconds") or 0) for item in actions if isinstance(item, dict)]
    if len(horizons) != len(actions) or min(horizons) <= 0:
        raise LifecycleError("development action horizons are invalid")
    execution_latency = int(target.get("execution_latency_seconds") or 0)
    if execution_latency <= 0:
        raise LifecycleError("development execution latency is invalid")
    return {
        "candidate_id": candidate_id,
        "report": report,
        "manifest": manifest,
        "model_sha256": model_hash,
        "development_cutoff_ms": cutoff_ms,
        "embargo_seconds": max(horizons) + execution_latency,
    }


def validate_candidate_artifacts(paths: RegistryPaths, state: Mapping[str, Any]) -> Dict[str, pathlib.Path]:
    candidate_id = str(state.get("candidate_id") or "")
    candidate_dir = paths.candidate_dir(candidate_id)
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, dict):
        raise LifecycleError("lifecycle state candidate artifacts missing")
    resolved: Dict[str, pathlib.Path] = {}
    for name in ("development_report", "candidate_manifest", "model"):
        ref = artifacts.get(name)
        if not isinstance(ref, dict):
            raise LifecycleError(f"lifecycle artifact reference missing: {name}")
        path = pathlib.Path(str(ref.get("path") or "")).resolve()
        try:
            path.relative_to(candidate_dir)
        except ValueError as exc:
            raise LifecycleError(f"lifecycle artifact escaped candidate directory: {name}") from exc
        expected = str(ref.get("sha256") or "")
        if not path.is_file() or len(expected) != 64 or sha256_file(path) != expected:
            raise LifecycleError(f"immutable candidate artifact mismatch: {name}")
        resolved[name] = path
    candidate = validate_development_candidate(
        resolved["development_report"], resolved["candidate_manifest"], resolved["model"]
    )
    if candidate["candidate_id"] != candidate_id:
        raise LifecycleError("registered candidate id mismatch")
    return resolved


def register_candidate(
    paths: RegistryPaths,
    events: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    *,
    source_report: pathlib.Path,
    source_manifest: pathlib.Path,
    source_model: pathlib.Path,
    selection_duration_seconds: int,
    holdout_duration_seconds: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    candidate_id = str(candidate["candidate_id"])
    if any(str(event.get("candidate_id") or "") == candidate_id for event in events):
        raise LifecycleError("a consumed/rejected candidate id cannot be registered again")
    candidate_dir = paths.candidate_dir(candidate_id)
    candidates_root = candidate_dir.parent
    candidates_root.mkdir(parents=True, exist_ok=True)
    if candidate_dir.exists():
        raise LifecycleError("uncommitted or duplicate candidate directory already exists")
    report_target = candidate_dir / "development_report.json"
    manifest_target = candidate_dir / "candidate_manifest.json"
    model_target = candidate_dir / "model.cbm"
    temporary_dir = pathlib.Path(
        tempfile.mkdtemp(prefix=f".{candidate_id}.", dir=candidates_root)
    )
    try:
        temporary_report = temporary_dir / report_target.name
        temporary_manifest = temporary_dir / manifest_target.name
        temporary_model = temporary_dir / model_target.name
        copy_verified(source_model, temporary_model, str(candidate["model_sha256"]))
        # Normalize only artifact locations after copying into the immutable
        # registry.  Candidate identity deliberately excludes filesystem
        # paths, so this preserves the candidate id while ensuring later
        # reports do not depend on a garbage-collected run directory.
        registered_report = read_json_object(source_report)
        registered_report["frozen_candidate"]["model_path"] = str(
            model_target.resolve()
        )
        atomic_write_json(temporary_report, registered_report)
        registered_manifest = read_json_object(source_manifest)
        registered_manifest["development_report"] = {
            "path": str(report_target.resolve()),
            "sha256": sha256_file(temporary_report),
        }
        atomic_write_json(temporary_manifest, registered_manifest)
        temporary_dir.replace(candidate_dir)
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise
    cutoff_ms = int(candidate["development_cutoff_ms"])
    embargo_seconds = int(candidate["embargo_seconds"])
    selection_start = cutoff_ms + embargo_seconds * 1000
    selection_end = selection_start + int(selection_duration_seconds) * 1000
    holdout_start = selection_end + embargo_seconds * 1000
    holdout_end = holdout_start + int(holdout_duration_seconds) * 1000
    state: Dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "phase": "selection_collecting",
        "development_cutoff_ms": cutoff_ms,
        "embargo_seconds": embargo_seconds,
        "selection_start_ms": selection_start,
        "selection_end_ms": selection_end,
        "holdout_start_ms": holdout_start,
        "holdout_end_ms": holdout_end,
        "artifacts": {
            "development_report": artifact_ref(report_target),
            "candidate_manifest": artifact_ref(manifest_target),
            "model": artifact_ref(model_target),
        },
        "evidence": {},
        "demo_entry_eligible": False,
        "live_promotion_eligible": False,
    }
    event = append_transition(
        paths,
        events,
        transition="development_candidate_registered",
        state=state,
        evidence={
            "development_report_sha256": sha256_file(report_target),
            "candidate_manifest_sha256": sha256_file(manifest_target),
            "model_sha256": sha256_file(model_target),
        },
    )
    return state, [*events, event]


def load_frozen_model(path: pathlib.Path) -> Any:
    if development.CatBoostRegressor is None:
        raise LifecycleError("catboost is required; use ai-trade-research image")
    model = development.CatBoostRegressor()
    model.load_model(str(path))
    return model


def action_contract(report: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[int]]:
    actions = report.get("target_contract", {}).get("actions", [])
    if not isinstance(actions, list) or not actions:
        raise LifecycleError("registered action contract missing")
    normalized: List[Dict[str, Any]] = []
    horizons: List[int] = []
    for item in actions:
        if not isinstance(item, dict):
            raise LifecycleError("registered action contract item invalid")
        direction = str(item.get("direction") or "")
        horizon = int(item.get("horizon_seconds") or 0)
        if direction not in {"long", "short"} or horizon <= 0:
            raise LifecycleError("registered action direction/horizon invalid")
        normalized.append({"direction": direction, "horizon_seconds": horizon})
        horizons.append(horizon)
    return normalized, sorted(set(horizons))


def prepare_model_inputs(
    series: Mapping[str, np.ndarray], report: Mapping[str, Any]
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[Dict[str, Any]]]:
    timestamps = np.asarray(series["timestamp"], dtype=np.int64)
    features, feature_names = development.build_causal_features(series)
    expected_features = list(report.get("data", {}).get("feature_names", []))
    if feature_names != expected_features:
        raise LifecycleError("frozen model feature order/name contract mismatch")
    actions, horizons = action_contract(report)
    target = report.get("target_contract", {})
    outcomes, generated_actions = development.build_joint_action_returns(
        series,
        horizons_seconds=horizons,
        execution_latency_seconds=int(target.get("execution_latency_seconds") or 0),
        additional_round_trip_cost_bps=float(
            target.get("additional_round_trip_cost_bps") or 0.0
        ),
    )
    if generated_actions != actions:
        raise LifecycleError("frozen model joint-action ordering contract mismatch")
    eligible = np.all(np.isfinite(features), axis=1) & np.all(np.isfinite(outcomes), axis=1)
    return timestamps[eligible], features[eligible], outcomes[eligible], actions


def fixed_policy_episodes(
    *,
    timestamps: np.ndarray,
    prediction: np.ndarray,
    outcomes: np.ndarray,
    actions: Sequence[Mapping[str, Any]],
    threshold_bps: float,
    base_cost_bps: float,
    stress_cost_multiplier: float,
    execution_latency_seconds: int,
) -> List[Dict[str, Any]]:
    episodes: List[Dict[str, Any]] = []
    next_allowed_ms = -1
    for index, raw_timestamp in enumerate(timestamps):
        timestamp = int(raw_timestamp)
        if timestamp < next_allowed_ms:
            continue
        row_prediction = np.asarray(prediction[index], dtype=np.float64)
        if not np.all(np.isfinite(row_prediction)):
            continue
        action_index = int(np.argmax(row_prediction))
        predicted_edge = float(row_prediction[action_index])
        if predicted_edge < threshold_bps:
            continue
        realized = float(outcomes[index, action_index])
        if not math.isfinite(realized):
            continue
        action = actions[action_index]
        horizon = int(action["horizon_seconds"])
        episodes.append(
            {
                "timestamp_ms": timestamp,
                "action": f"{action['direction']}_{horizon}s",
                "predicted_edge_bps": predicted_edge,
                "base_net_edge_bps": realized,
                "stress_net_edge_bps": realized
                - base_cost_bps * (stress_cost_multiplier - 1.0),
            }
        )
        next_allowed_ms = timestamp + (execution_latency_seconds + horizon) * 1000
    return episodes


def block_means(episodes: Sequence[Mapping[str, Any]], field: str, block_seconds: int) -> List[float]:
    buckets: Dict[int, List[float]] = {}
    block_ms = int(block_seconds) * 1000
    for item in episodes:
        bucket = int(item["timestamp_ms"]) // block_ms
        buckets.setdefault(bucket, []).append(float(item[field]))
    return [statistics.fmean(buckets[key]) for key in sorted(buckets)]


def evaluate_domain(
    *,
    series: Mapping[str, np.ndarray],
    report: Mapping[str, Any],
    model: Any,
    start_ms: int,
    end_ms: int,
    domain: str,
    min_trades: int,
    block_seconds: int,
    min_blocks: int,
    min_positive_blocks_ratio: float,
    min_row_density: float,
) -> Dict[str, Any]:
    timestamps, features, outcomes, actions = prepare_model_inputs(series, report)
    indices = np.flatnonzero((timestamps >= start_ms) & (timestamps < end_ms))
    expected_rows = max(1, math.ceil((end_ms - start_ms) / 1000.0))
    row_density = len(indices) / expected_rows
    latest_eligible = int(timestamps[-1]) if len(timestamps) else 0
    if latest_eligible < end_ms - 1000:
        raise LifecycleNotReady(
            f"{domain} eligible data ends at {latest_eligible}, requires {end_ms - 1000}"
        )
    if row_density < min_row_density:
        raise LifecycleError(
            f"{domain} row density {row_density:.6f} below {min_row_density:.6f}"
        )
    if not len(indices):
        raise LifecycleError(f"{domain} contains no eligible rows")
    frozen = report.get("frozen_candidate", {})
    target = report.get("target_contract", {})
    threshold = float(frozen.get("policy_threshold_bps"))
    prediction = np.asarray(model.predict(features[indices]), dtype=np.float64)
    if prediction.ndim == 1:
        prediction = prediction.reshape(-1, 1)
    if prediction.shape != outcomes[indices].shape:
        raise LifecycleError(
            f"frozen model output shape {prediction.shape} != target {outcomes[indices].shape}"
        )
    episodes = fixed_policy_episodes(
        timestamps=timestamps[indices],
        prediction=prediction,
        outcomes=outcomes[indices],
        actions=actions,
        threshold_bps=threshold,
        base_cost_bps=float(target.get("additional_round_trip_cost_bps") or 0.0),
        stress_cost_multiplier=float(target.get("stress_cost_multiplier") or 0.0),
        execution_latency_seconds=int(target.get("execution_latency_seconds") or 0),
    )
    base_values = [float(item["base_net_edge_bps"]) for item in episodes]
    stress_values = [float(item["stress_net_edge_bps"]) for item in episodes]
    base_blocks = block_means(episodes, "base_net_edge_bps", block_seconds)
    stress_blocks = block_means(episodes, "stress_net_edge_bps", block_seconds)
    base_by_trade = development.summarize_edges(base_values)
    stress_by_trade = development.summarize_edges(stress_values)
    base_by_block = development.summarize_edges(base_blocks)
    stress_by_block = development.summarize_edges(stress_blocks)
    positive_blocks_ratio = (
        sum(value > 0.0 for value in base_blocks) / len(base_blocks)
        if base_blocks
        else 0.0
    )
    passed = bool(
        len(episodes) >= min_trades
        and len(base_blocks) >= min_blocks
        and positive_blocks_ratio >= min_positive_blocks_ratio
        and (base_by_trade.get("lcb_bps") or float("-inf")) > 0.0
        and (stress_by_trade.get("lcb_bps") or float("-inf")) > 0.0
        and (base_by_block.get("lcb_bps") or float("-inf")) > 0.0
        and (stress_by_block.get("lcb_bps") or float("-inf")) > 0.0
    )
    action_counts: Dict[str, int] = {}
    for item in episodes:
        key = str(item["action"])
        action_counts[key] = action_counts.get(key, 0) + 1
    economic_identity = {
        "domain": domain,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "policy_threshold_bps": threshold,
        "row_count": len(indices),
        "episodes": episodes,
        "base_by_trade": base_by_trade,
        "stress_by_trade": stress_by_trade,
        "base_by_block": base_by_block,
        "stress_by_block": stress_by_block,
        "positive_blocks_ratio": positive_blocks_ratio,
        "action_counts": action_counts,
    }
    return {
        "schema_version": "microstructure_alpha_future_domain_v1",
        "status": "PASS" if passed else "FAIL",
        "fully_verifiable": True,
        "research_domain": domain,
        "candidate_id": None,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "row_count": len(indices),
        "row_density": row_density,
        "policy_frozen": True,
        "threshold_tuning_permitted": False,
        "policy_threshold_bps": threshold,
        "episode_count": len(episodes),
        "action_counts": action_counts,
        "base_cost_by_trade": base_by_trade,
        "stress_cost_by_trade": stress_by_trade,
        "base_cost_by_time_block": base_by_block,
        "stress_cost_by_time_block": stress_by_block,
        "positive_base_edge_block_ratio": positive_blocks_ratio,
        "gates": {
            "minimum_trades": min_trades,
            "time_block_seconds": block_seconds,
            "minimum_time_blocks": min_blocks,
            "minimum_positive_blocks_ratio": min_positive_blocks_ratio,
            "minimum_row_density": min_row_density,
            "base_and_stress_trade_lcb_must_be_positive": True,
            "base_and_stress_block_lcb_must_be_positive": True,
        },
        "economic_identity_sha256": canonical_sha256(economic_identity),
        "episodes": episodes,
    }


def write_evidence(candidate_dir: pathlib.Path, name: str, report: Mapping[str, Any]) -> pathlib.Path:
    path = candidate_dir / name
    if path.exists():
        existing = read_json_object(path)
        if existing != report:
            raise LifecycleError(f"immutable evidence artifact mismatch: {path}")
        return path
    atomic_write_json(path, report)
    return path


def transition_with_evidence(
    paths: RegistryPaths,
    events: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    *,
    transition: str,
    next_phase: str,
    evidence_name: str,
    evidence_report: Mapping[str, Any],
    demo_entry_eligible: bool = False,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    candidate_dir = paths.candidate_dir(str(state["candidate_id"]))
    evidence_path = write_evidence(candidate_dir, evidence_name, evidence_report)
    updated = json.loads(json.dumps(state))
    updated["phase"] = next_phase
    updated["demo_entry_eligible"] = demo_entry_eligible
    updated["live_promotion_eligible"] = False
    updated.setdefault("evidence", {})[transition] = artifact_ref(evidence_path)
    event = append_transition(
        paths,
        events,
        transition=transition,
        state=updated,
        evidence=artifact_ref(evidence_path),
    )
    return updated, [*events, event]


def csv_rows(path: pathlib.Path) -> List[Dict[str, float | int]]:
    rows: List[Dict[str, float | int]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != ("timestamp", *development.REQUIRED_FIELDS[1:]):
            raise LifecycleError(f"feature replay CSV schema mismatch: {path}")
        for row in reader:
            parsed: Dict[str, float | int] = {"timestamp": int(row["timestamp"])}
            for name in development.REQUIRED_FIELDS[1:]:
                parsed[name] = float(row[name])
            rows.append(parsed)
    return rows


def rows_equal(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]) -> bool:
    if len(left) != len(right):
        return False
    for expected, actual in zip(left, right):
        if int(expected["timestamp"]) != int(actual["timestamp"]):
            return False
        for name in development.REQUIRED_FIELDS[1:]:
            if not math.isclose(
                float(expected[name]), float(actual[name]), rel_tol=0.0, abs_tol=1e-12
            ):
                return False
    return True


def series_from_rows(rows: Iterable[Mapping[str, Any]]) -> Dict[str, np.ndarray]:
    by_timestamp: Dict[int, Tuple[float, ...]] = {}
    for row in rows:
        timestamp = int(row["timestamp"])
        values = tuple(float(row[name]) for name in development.REQUIRED_FIELDS[1:])
        previous = by_timestamp.get(timestamp)
        if previous is not None and previous != values:
            raise LifecycleError(f"conflicting replay row at timestamp={timestamp}")
        by_timestamp[timestamp] = values
    if not by_timestamp:
        raise LifecycleError("raw replay produced no rows")
    timestamps = np.asarray(sorted(by_timestamp), dtype=np.int64)
    matrix = np.asarray([by_timestamp[int(ts)] for ts in timestamps], dtype=np.float64)
    return {
        "timestamp": timestamps,
        **{
            name: matrix[:, index]
            for index, name in enumerate(development.REQUIRED_FIELDS[1:])
        },
    }


def replay_holdout(
    *,
    assessment: Mapping[str, Any],
    registered_report: Mapping[str, Any],
    model: Any,
    candidate_id: str,
    start_ms: int,
    end_ms: int,
    expected_economic_hash: str,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    target = registered_report.get("target_contract", {})
    actions, _ = action_contract(registered_report)
    maximum_horizon_ms = max(int(item["horizon_seconds"]) for item in actions) * 1000
    latency_ms = int(target.get("execution_latency_seconds") or 0) * 1000
    replay_start = start_ms - 60_000
    replay_end = end_ms + maximum_horizon_ms + latency_ms
    regenerated: List[Mapping[str, Any]] = []
    segment_evidence: List[Dict[str, Any]] = []
    for item in assessment.get("segments", []):
        if not isinstance(item, dict):
            raise LifecycleError("capture segment manifest item invalid during replay")
        first = int(item.get("first_timestamp_ms") or 0)
        last = int(item.get("last_timestamp_ms") or 0)
        if last < replay_start or first >= replay_end:
            continue
        raw_path = pathlib.Path(str(item.get("raw_path") or ""))
        feature_path = pathlib.Path(str(item.get("feature_path") or ""))
        if sha256_file(raw_path) != str(item.get("raw_sha256") or ""):
            raise LifecycleError(f"raw replay checksum mismatch: {raw_path}")
        if sha256_file(feature_path) != str(item.get("feature_sha256") or ""):
            raise LifecycleError(f"feature replay checksum mismatch: {feature_path}")
        replay_rows, raw_count = collector.replay_jsonl(
            raw_path, symbol="SOLUSDT", bucket_ms=1000
        )
        expected_rows = csv_rows(feature_path)
        if not rows_equal(expected_rows, replay_rows):
            raise LifecycleError(f"raw-to-feature replay parity failed: {raw_path}")
        expected_raw_count = int(item.get("raw_message_count") or 0)
        if expected_raw_count and raw_count != expected_raw_count:
            raise LifecycleError(f"raw replay message-count mismatch: {raw_path}")
        regenerated.extend(replay_rows)
        segment_evidence.append(
            {
                "raw_path": str(raw_path.resolve()),
                "raw_sha256": sha256_file(raw_path),
                "feature_path": str(feature_path.resolve()),
                "feature_sha256": sha256_file(feature_path),
                "raw_message_count": raw_count,
                "feature_row_count": len(replay_rows),
            }
        )
    if not segment_evidence:
        raise LifecycleError("no checksum-bound raw segments cover final holdout")
    replay_series = series_from_rows(regenerated)
    economic = evaluate_domain(
        series=replay_series,
        report=registered_report,
        model=model,
        start_ms=start_ms,
        end_ms=end_ms,
        domain="untouched_final_holdout",
        min_trades=int(args.min_trades),
        block_seconds=int(args.block_seconds),
        min_blocks=int(args.min_blocks),
        min_positive_blocks_ratio=float(args.min_positive_blocks_ratio),
        min_row_density=float(args.min_row_density),
    )
    deterministic = economic["economic_identity_sha256"] == expected_economic_hash
    status = "PASS" if deterministic and economic["status"] == "PASS" else "FAIL"
    return {
        "schema_version": "microstructure_alpha_raw_replay_v1",
        "status": status,
        "fully_verifiable": True,
        "candidate_id": candidate_id,
        "research_domain": "untouched_final_holdout_replay",
        "raw_to_feature_parity": True,
        "fixed_model_prediction_economics_deterministic": deterministic,
        "expected_economic_identity_sha256": expected_economic_hash,
        "actual_economic_identity_sha256": economic["economic_identity_sha256"],
        "collector_code": artifact_ref(pathlib.Path(collector.__file__).resolve()),
        "segments": segment_evidence,
        "economic_replay": economic,
        "demo_entry_eligible": status == "PASS",
        "live_promotion_eligible": False,
    }


def hydrate_candidate(
    paths: RegistryPaths,
    state: Mapping[str, Any],
    *,
    report_output: pathlib.Path,
    manifest_output: pathlib.Path,
    model_output: pathlib.Path,
) -> None:
    artifacts = validate_candidate_artifacts(paths, state)
    copy_verified(
        artifacts["development_report"],
        report_output,
        str(state["artifacts"]["development_report"]["sha256"]),
    )
    copy_verified(
        artifacts["candidate_manifest"],
        manifest_output,
        str(state["artifacts"]["candidate_manifest"]["sha256"]),
    )
    copy_verified(
        artifacts["model"], model_output, str(state["artifacts"]["model"]["sha256"])
    )


def prepare(args: argparse.Namespace, paths: RegistryPaths) -> int:
    with registry_lock(paths.root):
        events = read_event_chain(paths)
        if not events:
            print(json.dumps({"status": "NEEDS_DEVELOPMENT", "reason": "registry_empty"}))
            return 3
        state = dict(events[-1]["state"])
        if state.get("phase") == "rejected":
            print(
                json.dumps(
                    {
                        "status": "NEEDS_DEVELOPMENT",
                        "reason": "previous_candidate_rejected",
                        "candidate_id": state.get("candidate_id"),
                    }
                )
            )
            return 3
        hydrate_candidate(
            paths,
            state,
            report_output=pathlib.Path(args.development_report).resolve(),
            manifest_output=pathlib.Path(args.candidate_manifest).resolve(),
            model_output=pathlib.Path(args.model).resolve(),
        )
        print(
            json.dumps(
                {
                    "status": "HYDRATED",
                    "candidate_id": state.get("candidate_id"),
                    "phase": state.get("phase"),
                }
            )
        )
        return 0


def lifecycle_report(
    *,
    state: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
    status: str,
    failures: Sequence[str],
    not_ready_reason: str | None = None,
) -> Dict[str, Any]:
    phase = str(state.get("phase") or "unregistered") if state else "unregistered"
    next_gate = {
        "unregistered": "passing_development_candidate_required",
        "selection_collecting": "collect_fixed_future_selection_domain",
        "holdout_collecting": "collect_untouched_final_holdout",
        "replay_pending": "deterministic_raw_to_feature_replay",
        "rejected": "new_development_candidate_with_fresh_future_domains",
        "demo_ready": "demo_incubation_only",
    }.get(phase, "integrity_review_required")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "fully_verifiable": bool(state) and not failures,
        "candidate_id": state.get("candidate_id") if state else None,
        "phase": phase,
        "research_domain_contract": {
            "development": "frozen_and_non_promotional",
            "selection": "fixed_future_interval_no_threshold_tuning",
            "holdout": "untouched_fixed_future_interval_consumed_once",
            "replay": "checksum_bound_raw_to_feature_determinism",
        },
        "registry": {
            "event_count": len(events),
            "last_event_hash": events[-1].get("event_hash") if events else None,
            "state_sha256": canonical_sha256(state) if state else None,
        },
        "state": dict(state) if state else None,
        "not_ready_reason": not_ready_reason,
        "failures": list(failures),
        "next_gate": next_gate,
        "promotion_evidence": phase == "demo_ready",
        "promotion_eligible": False,
        "demo_entry_eligible": phase == "demo_ready",
        "live_promotion_eligible": False,
    }


def advance(args: argparse.Namespace, paths: RegistryPaths) -> Tuple[Dict[str, Any], int]:
    failures: List[str] = []
    not_ready_reason: str | None = None
    state: Dict[str, Any] | None = None
    events: List[Dict[str, Any]] = []
    with registry_lock(paths.root):
        try:
            events = read_event_chain(paths)
            if events:
                state = dict(events[-1]["state"])
            if state is None or state.get("phase") == "rejected":
                candidate = validate_development_candidate(
                    pathlib.Path(args.development_report).resolve(),
                    pathlib.Path(args.candidate_manifest).resolve(),
                    pathlib.Path(args.model).resolve(),
                )
                state, events = register_candidate(
                    paths,
                    events,
                    candidate,
                    source_report=pathlib.Path(args.development_report).resolve(),
                    source_manifest=pathlib.Path(args.candidate_manifest).resolve(),
                    source_model=pathlib.Path(args.model).resolve(),
                    selection_duration_seconds=int(args.selection_duration_seconds),
                    holdout_duration_seconds=int(args.holdout_duration_seconds),
                )
            artifacts = validate_candidate_artifacts(paths, state)
            registered_report = read_json_object(artifacts["development_report"])
            assessment = development.validate_capture_assessment(
                pathlib.Path(args.capture_assessment).resolve()
            )
            series = development.load_capture_rows(assessment)
            model = load_frozen_model(artifacts["model"])
            candidate_id = str(state["candidate_id"])
            candidate_dir = paths.candidate_dir(candidate_id)

            if state.get("phase") == "selection_collecting":
                selection = evaluate_domain(
                    series=series,
                    report=registered_report,
                    model=model,
                    start_ms=int(state["selection_start_ms"]),
                    end_ms=int(state["selection_end_ms"]),
                    domain="independent_forward_selection",
                    min_trades=int(args.min_trades),
                    block_seconds=int(args.block_seconds),
                    min_blocks=int(args.min_blocks),
                    min_positive_blocks_ratio=float(args.min_positive_blocks_ratio),
                    min_row_density=float(args.min_row_density),
                )
                selection["candidate_id"] = candidate_id
                next_phase = "holdout_collecting" if selection["status"] == "PASS" else "rejected"
                transition = "selection_passed" if selection["status"] == "PASS" else "selection_rejected"
                state, events = transition_with_evidence(
                    paths,
                    events,
                    state,
                    transition=transition,
                    next_phase=next_phase,
                    evidence_name="selection_report.json",
                    evidence_report=selection,
                )

            if state.get("phase") == "holdout_collecting":
                holdout = evaluate_domain(
                    series=series,
                    report=registered_report,
                    model=model,
                    start_ms=int(state["holdout_start_ms"]),
                    end_ms=int(state["holdout_end_ms"]),
                    domain="untouched_final_holdout",
                    min_trades=int(args.min_trades),
                    block_seconds=int(args.block_seconds),
                    min_blocks=int(args.min_blocks),
                    min_positive_blocks_ratio=float(args.min_positive_blocks_ratio),
                    min_row_density=float(args.min_row_density),
                )
                holdout["candidate_id"] = candidate_id
                next_phase = "replay_pending" if holdout["status"] == "PASS" else "rejected"
                transition = "final_holdout_passed" if holdout["status"] == "PASS" else "final_holdout_rejected"
                state, events = transition_with_evidence(
                    paths,
                    events,
                    state,
                    transition=transition,
                    next_phase=next_phase,
                    evidence_name="final_holdout_report.json",
                    evidence_report=holdout,
                )

            if state.get("phase") == "replay_pending":
                holdout_ref = state.get("evidence", {}).get("final_holdout_passed")
                if not isinstance(holdout_ref, dict):
                    raise LifecycleError("final holdout evidence missing before replay")
                holdout_report = read_json_object(pathlib.Path(str(holdout_ref.get("path") or "")))
                if sha256_file(pathlib.Path(str(holdout_ref.get("path") or ""))) != str(
                    holdout_ref.get("sha256") or ""
                ):
                    raise LifecycleError("final holdout evidence checksum mismatch")
                replay = replay_holdout(
                    assessment=assessment,
                    registered_report=registered_report,
                    model=model,
                    candidate_id=candidate_id,
                    start_ms=int(state["holdout_start_ms"]),
                    end_ms=int(state["holdout_end_ms"]),
                    expected_economic_hash=str(
                        holdout_report.get("economic_identity_sha256") or ""
                    ),
                    args=args,
                )
                next_phase = "demo_ready" if replay["status"] == "PASS" else "rejected"
                transition = "raw_replay_passed" if replay["status"] == "PASS" else "raw_replay_rejected"
                state, events = transition_with_evidence(
                    paths,
                    events,
                    state,
                    transition=transition,
                    next_phase=next_phase,
                    evidence_name="raw_replay_report.json",
                    evidence_report=replay,
                    demo_entry_eligible=replay["status"] == "PASS",
                )
        except (development.CaptureNotReady, LifecycleNotReady) as exc:
            not_ready_reason = str(exc)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, LifecycleError) as exc:
            failures.append(str(exc))

        if failures:
            status = "FAIL"
            exit_code = 2
        elif state and state.get("phase") == "demo_ready":
            status = "PASS"
            exit_code = 0
        elif state and state.get("phase") == "rejected":
            status = "FAIL"
            failures.append("candidate rejected by a fixed future economic/replay gate")
            exit_code = 2
        else:
            status = "NOT_READY"
            exit_code = 2
        report = lifecycle_report(
            state=state,
            events=events,
            status=status,
            failures=failures,
            not_ready_reason=not_ready_reason,
        )
        return report, exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "advance"))
    parser.add_argument("--registry-root", required=True)
    parser.add_argument("--development-report", required=True)
    parser.add_argument("--candidate-manifest", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--capture-assessment", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--selection-duration-seconds", type=int, default=21600)
    parser.add_argument("--holdout-duration-seconds", type=int, default=21600)
    parser.add_argument("--min-trades", type=int, default=30)
    parser.add_argument("--block-seconds", type=int, default=3600)
    parser.add_argument("--min-blocks", type=int, default=4)
    parser.add_argument("--min-positive-blocks-ratio", type=float, default=0.60)
    parser.add_argument("--min-row-density", type=float, default=0.80)
    args = parser.parse_args()
    if args.action == "advance" and (not args.capture_assessment or not args.output):
        parser.error("advance requires --capture-assessment and --output")
    if min(args.selection_duration_seconds, args.holdout_duration_seconds) <= 0:
        parser.error("future-domain durations must be positive")
    if args.min_trades <= 0 or args.block_seconds <= 0 or args.min_blocks < 2:
        parser.error("economic gate counts/duration are invalid")
    if not 0.5 <= args.min_positive_blocks_ratio <= 1.0:
        parser.error("min-positive-blocks-ratio must be in [0.5,1.0]")
    if not 0.0 < args.min_row_density <= 1.0:
        parser.error("min-row-density must be in (0,1]")
    return args


def main() -> int:
    args = parse_args()
    paths = RegistryPaths(pathlib.Path(args.registry_root).resolve())
    if args.action == "prepare":
        try:
            return prepare(args, paths)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, LifecycleError) as exc:
            print(json.dumps({"status": "FAIL", "failures": [str(exc)]}))
            return 2
    report, exit_code = advance(args, paths)
    output = pathlib.Path(args.output).resolve()
    atomic_write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
