#!/usr/bin/env python3

"""Evaluate a staged model activation against cumulative live canary evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List


ACTIVE_STATUSES = {
    "activated_pending_validation",
    "canary_pending_evidence",
}
TERMINAL_STATUSES = {
    "committed",
    "rolled_back",
    "rolled_back_service_stopped",
}
HARD_SAFETY_METRICS = (
    "critical_count",
    "trading_halted_event_count",
    "trade_health_halted_count",
    "adapter_trade_not_ok_count",
    "reconcile_anomaly_halt_enter_count",
    "reconcile_anomaly_halted_true_count",
    "reconcile_autoresync_count",
    "force_reduce_only_active_count",
    "reconcile_reduce_only_active_count",
    "fill_overfill_drop_count",
    "fill_unmapped_drop_count",
    "integrator_episode_closure_wal_failed_count",
    "integrator_episode_identity_invalid_count",
    "policy_flat_residual_position_count",
    "tp_attach_failed_count",
    "self_evolution_state_restore_failed_count",
    "self_evolution_state_persist_failed_count",
)
ACTIVATION_POLICY_SCHEMA = "closed_loop_activation_policy_v1"
T_CRITICAL_975 = (
    0.0,
    12.706,
    4.303,
    3.182,
    2.776,
    2.571,
    2.447,
    2.365,
    2.306,
    2.262,
    2.228,
    2.201,
    2.179,
    2.160,
    2.145,
    2.131,
    2.120,
    2.110,
    2.101,
    2.093,
    2.086,
    2.080,
    2.074,
    2.069,
    2.064,
    2.060,
    2.056,
    2.052,
    2.048,
    2.045,
    2.042,
)

RUNTIME_EVIDENCE_SHORTFALL_MARKERS = (
    "样本不足",
    "sample insufficient",
    "insufficient sample",
    "episode insufficient",
    "complete canary episodes",
    "candidate fill insufficient",
    "no candidate fill",
    "execution not evaluated",
)


def runtime_failure_is_evidence_shortfall(runtime: Dict[str, Any]) -> bool:
    reasons = runtime.get("fail_reasons", [])
    if not isinstance(reasons, list) or not reasons:
        return False
    normalized = [str(reason or "").strip().lower() for reason in reasons]
    return all(
        reason
        and any(marker in reason for marker in RUNTIME_EVIDENCE_SHORTFALL_MARKERS)
        for reason in normalized
    )


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: dt.datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_object(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def write_object(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def as_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def student_t_mean_lcb95(values: List[float]) -> float | None:
    """One-sided promotion guard using the lower edge of a 95% two-sided CI."""
    count = len(values)
    if count < 2:
        return None
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    standard_error = math.sqrt(max(0.0, variance) / count)
    degrees_of_freedom = count - 1
    critical = (
        T_CRITICAL_975[degrees_of_freedom]
        if degrees_of_freedom < len(T_CRITICAL_975)
        else 1.96
    )
    return mean - critical * standard_error


def parse_utc(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def frozen_activation_policy(state: Dict[str, Any]) -> Dict[str, Any]:
    policy = state.get("activation_policy")
    if not isinstance(policy, dict):
        raise ValueError("activation transaction frozen policy missing")
    normalized = {
        "schema_version": str(policy.get("schema_version", "")).strip(),
        "min_complete_episodes": as_int(policy.get("min_complete_episodes")),
        "min_positive_episode_ratio": as_float(
            policy.get("min_positive_episode_ratio")
        ),
        "min_mean_realized_net_per_fill_usd": as_float(
            policy.get("min_mean_realized_net_per_fill_usd")
        ),
        "max_pending_hours": as_float(policy.get("max_pending_hours")),
    }
    if normalized["schema_version"] != ACTIVATION_POLICY_SCHEMA:
        raise ValueError("activation transaction frozen policy schema is invalid")
    if normalized["min_complete_episodes"] <= 0:
        raise ValueError("activation transaction min_complete_episodes is invalid")
    if not 0.0 <= normalized["min_positive_episode_ratio"] <= 1.0:
        raise ValueError("activation transaction positive ratio is invalid")
    if normalized["max_pending_hours"] < 0.0:
        raise ValueError("activation transaction max_pending_hours is invalid")
    expected_sha = str(state.get("activation_policy_sha256", "")).strip()
    actual_sha = canonical_sha256(normalized)
    if len(expected_sha) != 64 or expected_sha != actual_sha:
        raise ValueError("activation transaction frozen policy hash mismatch")
    return normalized


def candidate_identity(state: Dict[str, Any]) -> Dict[str, str]:
    candidate = state.get("candidate", {})
    if not isinstance(candidate, dict):
        return {}
    identity = candidate.get("identity", {})
    if not isinstance(identity, dict):
        identity = {}
    artifacts = candidate.get("artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}

    def artifact_hash(name: str) -> str:
        item = artifacts.get(name, {})
        return str(item.get("sha256", "")).strip() if isinstance(item, dict) else ""

    return {
        "model_version": str(candidate.get("model_version", "")).strip(),
        "model_sha256": str(
            identity.get("model_sha256") or artifact_hash("model")
        ).strip(),
        "report_sha256": str(
            identity.get("report_sha256") or artifact_hash("report")
        ).strip(),
        "runtime_config_sha256": str(
            identity.get("runtime_config_sha256", "")
        ).strip(),
        "trade_bot_sha256": str(identity.get("trade_bot_sha256", "")).strip(),
        "training_symbol": str(candidate.get("training_symbol", "")).strip().upper(),
        "bar_interval_ms": str(as_int(candidate.get("bar_interval_ms"))),
    }


def verify_active_artifacts(state: Dict[str, Any]) -> List[str]:
    candidate = state.get("candidate", {})
    artifacts = candidate.get("artifacts", {}) if isinstance(candidate, dict) else {}
    if not isinstance(artifacts, dict):
        return ["candidate artifact set missing"]
    reasons: List[str] = []
    for name in ("model", "report", "miner_report", "active_meta"):
        item = artifacts.get(name, {})
        if not isinstance(item, dict):
            reasons.append(f"candidate artifact metadata missing: {name}")
            continue
        path = Path(str(item.get("path", "")))
        expected = str(item.get("sha256", "")).strip()
        if not path.is_file():
            reasons.append(f"active candidate artifact missing: {name}")
        elif len(expected) != 64 or sha256_file(path) != expected:
            reasons.append(f"active candidate artifact hash mismatch: {name}")
    return reasons


def runtime_identity(metrics: Dict[str, Any]) -> Dict[str, str]:
    return {
        "model_version": str(
            metrics.get("integrator_model_version_latest", "")
        ).strip(),
        "model_sha256": str(
            metrics.get("integrator_model_sha256_latest", "")
        ).strip(),
        "report_sha256": str(
            metrics.get("integrator_report_sha256_latest", "")
        ).strip(),
        "runtime_config_sha256": str(
            metrics.get("integrator_runtime_config_sha256_latest", "")
        ).strip(),
        "trade_bot_sha256": str(
            metrics.get("integrator_trade_bot_sha256_latest", "")
        ).strip(),
        "training_symbol": str(
            metrics.get("integrator_feature_training_symbol_latest", "")
        ).strip().upper(),
        "bar_interval_ms": str(
            as_int(metrics.get("integrator_feature_bar_interval_ms_latest"))
        ),
    }


def merge_candidate_episodes(
    state: Dict[str, Any],
    metrics: Dict[str, Any],
    model_version: str,
    *,
    expected_identity: Dict[str, str],
    runtime_boot_id: str,
) -> tuple[Dict[str, Dict[str, Any]], List[str]]:
    evidence = state.setdefault("evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
        state["evidence"] = evidence
    episodes = evidence.get("episodes", {})
    if not isinstance(episodes, dict):
        episodes = {}
    invalid_reasons: List[str] = []
    transaction_id = str(state.get("run_id", "")).strip()
    created_at = parse_utc(state.get("created_at_utc"))

    def normalized_episode(event: Dict[str, Any]) -> Dict[str, Any] | None:
        episode_id = str(event.get("position_episode_id", "")).strip()
        if (
            not episode_id
            or str(event.get("candidate_id", "")).strip() != model_version
            or str(event.get("model_version", "")).strip() != model_version
            or str(event.get("mode", "")).strip().lower() != "canary"
            or event.get("evidence_complete") is not True
        ):
            return None
        if (
            str(event.get("policy_reason", "")).strip()
            != "canary_independent_signal"
        ):
            # Aligned/scaled baseline trades do not establish incremental
            # model economics and are intentionally non-authoritative.
            return None
        fill_event_count = as_int(event.get("fill_event_count"))
        unique_order_count = as_int(event.get("unique_order_count"))
        symbol = str(event.get("symbol", "")).strip().upper()
        if fill_event_count < 2 or unique_order_count < 2 or not symbol:
            invalid_reasons.append(
                f"candidate episode {episode_id} has incomplete fill lifecycle"
            )
            return None
        identity_checks = {
            "activation_transaction_id": transaction_id,
            "evidence_boot_id": runtime_boot_id,
            "runtime_config_sha256": expected_identity.get(
                "runtime_config_sha256", ""
            ),
            "trade_bot_sha256": expected_identity.get("trade_bot_sha256", ""),
        }
        for key, expected_value in identity_checks.items():
            if (
                not expected_value
                or str(event.get(key, "")).strip() != expected_value
            ):
                invalid_reasons.append(
                    f"candidate episode {episode_id} identity mismatch: {key}"
                )
                return None
        if event.get("recovered_after_restart") is not False:
            invalid_reasons.append(
                f"candidate episode {episode_id} is restart-recovered evidence"
            )
            return None
        closed_at = parse_utc(event.get("closed_at_utc"))
        if (
            closed_at is None
            or created_at is None
            or closed_at < created_at
        ):
            invalid_reasons.append(
                f"candidate episode {episode_id} close time is outside transaction"
            )
            return None
        return {
            "position_episode_id": episode_id,
            "candidate_id": model_version,
            "model_version": model_version,
            "mode": "canary",
            "policy_reason": "canary_independent_signal",
            "symbol": symbol,
            "realized_net_usd": as_float(event.get("realized_net_usd")),
            "funding_paid_usd": as_float(event.get("funding_paid_usd")),
            "fill_event_count": fill_event_count,
            "unique_order_count": unique_order_count,
            "evidence_complete": True,
            **identity_checks,
            "closed_at_utc": str(event.get("closed_at_utc", "")).strip(),
            "recovered_after_restart": False,
        }

    validated_existing: Dict[str, Dict[str, Any]] = {}
    for raw_episode in episodes.values():
        if not isinstance(raw_episode, dict):
            invalid_reasons.append("persisted candidate episode is not an object")
            continue
        normalized = normalized_episode(raw_episode)
        if normalized is not None:
            validated_existing[normalized["position_episode_id"]] = normalized
    episodes = validated_existing
    events = metrics.get("integrator_policy_closed_episode_events", [])
    if not isinstance(events, list):
        events = []
    for event in events:
        if not isinstance(event, dict):
            continue
        normalized = normalized_episode(event)
        if normalized is None:
            continue
        episode_id = normalized["position_episode_id"]
        existing = episodes.get(episode_id)
        if existing is not None and existing != normalized:
            invalid_reasons.append(
                f"candidate episode {episode_id} payload changed across assessments"
            )
            continue
        episodes[episode_id] = normalized
    evidence["episodes"] = episodes
    return episodes, invalid_reasons


def mismatched_candidate_fill_count(
    metrics: Dict[str, Any], model_version: str
) -> int:
    candidate_ids = metrics.get("integrator_policy_filled_candidate_ids", [])
    if not isinstance(candidate_ids, list):
        return 0
    return sum(
        1
        for value in candidate_ids
        if str(value).strip() and str(value).strip() != model_version
    )


def evaluate(
    state: Dict[str, Any],
    runtime: Dict[str, Any],
    *,
    mechanism: Dict[str, Any] | None = None,
    min_complete_episodes: int,
    min_positive_episode_ratio: float,
    min_mean_realized_net_per_fill_usd: float,
    max_pending_hours: float,
    now: dt.datetime | None = None,
) -> Dict[str, Any]:
    current = now or utc_now()
    if state.get("schema_version") != "closed_loop_activation_transaction_v2":
        raise ValueError("activation transaction schema is not v2")
    status = str(state.get("status", "")).strip()
    if status in TERMINAL_STATUSES:
        return {
            "schema_version": "closed_loop_activation_decision_v1",
            "decision": "no_pending_transaction",
            "transaction_status": status,
            "evaluated_at_utc": utc_iso(current),
        }
    if status not in ACTIVE_STATUSES:
        raise ValueError(f"activation transaction is not evaluable: status={status}")

    policy = frozen_activation_policy(state)
    requested_policy = {
        "schema_version": ACTIVATION_POLICY_SCHEMA,
        "min_complete_episodes": int(min_complete_episodes),
        "min_positive_episode_ratio": float(min_positive_episode_ratio),
        "min_mean_realized_net_per_fill_usd": (
            float(min_mean_realized_net_per_fill_usd)
        ),
        "max_pending_hours": float(max_pending_hours),
    }
    if canonical_sha256(requested_policy) != state["activation_policy_sha256"]:
        raise ValueError(
            "activation policy drift detected; assess must use thresholds frozen by full"
        )
    min_complete_episodes = int(policy["min_complete_episodes"])
    min_positive_episode_ratio = float(
        policy["min_positive_episode_ratio"]
    )
    min_mean_realized_net_per_fill_usd = float(
        policy["min_mean_realized_net_per_fill_usd"]
    )
    max_pending_hours = float(policy["max_pending_hours"])

    expected = candidate_identity(state)
    model_version = expected.get("model_version", "")
    hard_fail_reasons = verify_active_artifacts(state)
    candidate = state.get("candidate", {})
    artifacts = candidate.get("artifacts", {}) if isinstance(candidate, dict) else {}
    for artifact_name, identity_name in (
        ("model", "model_sha256"),
        ("report", "report_sha256"),
    ):
        item = artifacts.get(artifact_name, {}) if isinstance(artifacts, dict) else {}
        artifact_sha = (
            str(item.get("sha256", "")).strip() if isinstance(item, dict) else ""
        )
        if artifact_sha and artifact_sha != expected.get(identity_name):
            hard_fail_reasons.append(
                f"candidate {artifact_name} identity differs from staged artifact"
            )
    pending_reasons: List[str] = []
    for key in (
        "model_version",
        "model_sha256",
        "report_sha256",
        "runtime_config_sha256",
        "trade_bot_sha256",
        "training_symbol",
    ):
        if not expected.get(key):
            hard_fail_reasons.append(f"candidate expected identity missing: {key}")
    if as_int(expected.get("bar_interval_ms")) <= 0:
        hard_fail_reasons.append("candidate expected identity missing: bar_interval_ms")

    metrics = runtime.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
        pending_reasons.append("runtime metrics missing")
    observed = runtime_identity(metrics)
    identity_complete = all(
        observed.get(key)
        for key in (
            "model_version",
            "model_sha256",
            "report_sha256",
            "runtime_config_sha256",
            "trade_bot_sha256",
            "training_symbol",
        )
    ) and as_int(observed.get("bar_interval_ms")) > 0
    identity_match = identity_complete and observed == expected
    if identity_complete and not identity_match:
        hard_fail_reasons.append("runtime four-part/model feature identity mismatch")
    elif not identity_complete:
        pending_reasons.append("runtime candidate identity incomplete")

    runtime_boot_id = str(metrics.get("runtime_boot_id_latest", "")).strip()
    evidence = state.setdefault("evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
        state["evidence"] = evidence
    expected_boot_id = str(evidence.get("runtime_boot_id", "")).strip()
    if not runtime_boot_id:
        hard_fail_reasons.append("runtime boot identity missing")
    elif expected_boot_id and expected_boot_id != runtime_boot_id:
        hard_fail_reasons.append(
            "runtime boot changed during pending validation: "
            f"{expected_boot_id} -> {runtime_boot_id}"
        )
    elif not expected_boot_id:
        evidence["runtime_boot_id"] = runtime_boot_id

    for metric_name in HARD_SAFETY_METRICS:
        if metric_name not in metrics:
            hard_fail_reasons.append(
                f"runtime hard safety metric missing: {metric_name}"
            )
            continue
        value = as_int(metrics.get(metric_name))
        if value > 0:
            hard_fail_reasons.append(
                f"runtime hard safety metric {metric_name}={value}"
            )
    if not isinstance(
        metrics.get("integrator_policy_filled_candidate_ids"), list
    ):
        hard_fail_reasons.append(
            "runtime candidate fill identity evidence missing"
        )
    mismatched_fills = mismatched_candidate_fill_count(metrics, model_version)
    if mismatched_fills > 0:
        hard_fail_reasons.append(
            f"runtime contains {mismatched_fills} fill(s) from another candidate"
        )

    episodes, episode_identity_failures = merge_candidate_episodes(
        state,
        metrics,
        model_version,
        expected_identity=expected,
        runtime_boot_id=runtime_boot_id,
    )
    hard_fail_reasons.extend(episode_identity_failures)
    episode_values = [
        as_float(event.get("realized_net_usd"))
        for event in episodes.values()
        if isinstance(event, dict)
    ]
    episode_count = len(episode_values)
    total_net = sum(episode_values)
    total_funding_paid = sum(
        as_float(event.get("funding_paid_usd"))
        for event in episodes.values()
        if isinstance(event, dict)
    )
    mean_net = total_net / episode_count if episode_count else 0.0
    positive_count = sum(1 for value in episode_values if value > 0.0)
    positive_ratio = positive_count / episode_count if episode_count else 0.0
    total_fill_events = sum(
        max(1, as_int(event.get("fill_event_count")))
        for event in episodes.values()
        if isinstance(event, dict)
    )
    mean_net_per_fill = (
        total_net / total_fill_events if total_fill_events > 0 else 0.0
    )
    episode_net_per_fill_values = [
        as_float(event.get("realized_net_usd"))
        / max(1, as_int(event.get("fill_event_count")))
        for event in episodes.values()
        if isinstance(event, dict)
    ]
    mean_episode_net_per_fill = (
        sum(episode_net_per_fill_values) / len(episode_net_per_fill_values)
        if episode_net_per_fill_values
        else 0.0
    )
    mean_episode_net_per_fill_lcb95 = student_t_mean_lcb95(
        episode_net_per_fill_values
    )

    created_at = parse_utc(state.get("created_at_utc"))
    pending_age_hours = (
        max(0.0, (current - created_at).total_seconds() / 3600.0)
        if created_at is not None
        else None
    )
    if created_at is None:
        hard_fail_reasons.append("activation transaction created_at_utc is invalid")

    if episode_count < min_complete_episodes:
        pending_reasons.append(
            f"complete canary episodes {episode_count} < {min_complete_episodes}"
        )
    elif mean_episode_net_per_fill_lcb95 is None:
        pending_reasons.append(
            "canary episode-level net-per-fill confidence interval unavailable"
        )
    elif (
        mean_episode_net_per_fill_lcb95
        <= min_mean_realized_net_per_fill_usd
    ):
        hard_fail_reasons.append(
            "canary mean realized net per fill 95% LCB failed: "
            f"{mean_episode_net_per_fill_lcb95:.8f} <= "
            f"{min_mean_realized_net_per_fill_usd:.8f}"
        )
    elif positive_ratio < min_positive_episode_ratio:
        hard_fail_reasons.append(
            "canary positive episode ratio failed: "
            f"{positive_ratio:.6f} < {min_positive_episode_ratio:.6f}"
        )

    verdict = str(runtime.get("verdict", "")).strip().upper()
    if verdict not in {"PASS", "PASS_WITH_ACTIONS"}:
        reason = f"runtime verdict not committable: {verdict or 'missing'}"
        if verdict == "FAIL" and runtime_failure_is_evidence_shortfall(runtime):
            pending_reasons.append(reason + " (evidence shortfall only)")
        else:
            hard_fail_reasons.append(reason)
    mechanism_status = str(
        (mechanism or {}).get("status", "")
    ).strip().lower()
    if episode_count >= min_complete_episodes:
        if mechanism_status not in {"pass", "pass_with_actions"}:
            hard_fail_reasons.append(
                "closed-loop mechanism audit did not pass at promotion sample: "
                f"{mechanism_status or 'missing'}"
            )
    if (
        pending_reasons
        and pending_age_hours is not None
        and max_pending_hours > 0.0
        and pending_age_hours > max_pending_hours
    ):
        hard_fail_reasons.append(
            "canary validation deadline exceeded with unresolved conditions: "
            f"age_hours={pending_age_hours:.3f} > {max_pending_hours:.3f}; "
            + "; ".join(pending_reasons)
        )

    if hard_fail_reasons:
        decision = "rollback"
    elif pending_reasons:
        decision = "pending"
    else:
        decision = "commit"

    result = {
        "schema_version": "closed_loop_activation_decision_v1",
        "decision": decision,
        "transaction_run_id": state.get("run_id"),
        "transaction_status": status,
        "candidate_model_version": model_version,
        "candidate_identity": expected,
        "activation_policy_sha256": state["activation_policy_sha256"],
        "evaluated_at_utc": utc_iso(current),
        "runtime_verdict": verdict or None,
        "mechanism_status": mechanism_status or None,
        "identity_complete": identity_complete,
        "identity_match": identity_match if identity_complete else None,
        "hard_fail_reasons": hard_fail_reasons,
        "pending_reasons": pending_reasons,
        "thresholds": policy,
        "evidence": {
            "complete_episode_count": episode_count,
            "positive_episode_count": positive_count,
            "positive_episode_ratio": positive_ratio,
            "total_realized_net_usd": total_net,
            "total_funding_paid_usd": total_funding_paid,
            "mean_realized_net_per_episode_usd": mean_net,
            "total_fill_event_count": total_fill_events,
            "mean_realized_net_per_fill_usd": mean_net_per_fill,
            "mean_episode_realized_net_per_fill_usd": (
                mean_episode_net_per_fill
            ),
            "mean_episode_realized_net_per_fill_lcb95_usd": (
                mean_episode_net_per_fill_lcb95
            ),
            "net_per_fill_confidence_method": (
                "episode_normalized_student_t_two_sided_95_lower_bound"
            ),
            "pending_age_hours": pending_age_hours,
            "mismatched_candidate_fill_count": mismatched_fills,
        },
    }
    state["latest_evaluation"] = result
    state["updated_at_utc"] = utc_iso(current)
    if decision == "pending":
        state["status"] = "canary_pending_evidence"
        state.setdefault("history", []).append(
            {
                "status": "canary_pending_evidence",
                "at_utc": utc_iso(current),
                "complete_episode_count": episode_count,
            }
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--runtime-assess", required=True)
    parser.add_argument("--mechanism-audit", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-complete-episodes", type=int, default=30)
    parser.add_argument("--min-positive-episode-ratio", type=float, default=0.50)
    parser.add_argument(
        "--min-mean-realized-net-per-fill-usd", type=float, default=0.0
    )
    parser.add_argument("--max-pending-hours", type=float, default=72.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_complete_episodes <= 0:
        raise SystemExit("--min-complete-episodes must be positive")
    if not 0.0 <= args.min_positive_episode_ratio <= 1.0:
        raise SystemExit("--min-positive-episode-ratio must be in [0,1]")
    state_path = Path(args.state)
    runtime_path = Path(args.runtime_assess)
    output_path = Path(args.output)
    state = read_object(state_path)
    runtime = read_object(runtime_path)
    mechanism = (
        read_object(Path(args.mechanism_audit))
        if args.mechanism_audit and Path(args.mechanism_audit).is_file()
        else {}
    )
    result = evaluate(
        state,
        runtime,
        mechanism=mechanism,
        min_complete_episodes=args.min_complete_episodes,
        min_positive_episode_ratio=args.min_positive_episode_ratio,
        min_mean_realized_net_per_fill_usd=(
            args.min_mean_realized_net_per_fill_usd
        ),
        max_pending_hours=args.max_pending_hours,
    )
    write_object(state_path, state)
    write_object(output_path, result)
    print(f"ACTIVATION_DECISION: {result['decision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
