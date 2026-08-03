#!/usr/bin/env python3
"""Accumulate release-bound Demo evidence and decide live-test review eligibility.

This evaluator deliberately never enables mainnet trading.  Its strongest result
is eligibility for a separate manual review.  A release, runtime configuration,
or policy change starts a fresh evidence generation so incompatible evidence is
never spliced together.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

try:
    from config_policy_contract import config_value, policy_sha256
except ImportError:  # pragma: no cover - module import from repository root
    from tools.config_policy_contract import config_value, policy_sha256


STATE_SCHEMA = "demo_incubation_state_v1"
REPORT_SCHEMA = "demo_incubation_report_v1"
POLICY_SCHEMA = "demo_incubation_policy_v1"
ELIGIBLE = "ELIGIBLE_FOR_MANUAL_LIVE_TEST_REVIEW"
INCUBATING = "INCUBATING"
BLOCKED = "BLOCKED"

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

TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
TICK_RE = re.compile(r"RUNTIME_STATUS:\s*ticks=(\d+)")
BOOT_RE = re.compile(r"boot=\{id=([^,}\s]+)")
ACCOUNT_RE = re.compile(
    r"account=\{equity=([-+0-9.eE]+),\s*"
    r"drawdown_pct=([-+0-9.eE]+),\s*"
    r"notional=([-+0-9.eE]+),\s*"
    r"realized_pnl=([-+0-9.eE]+),\s*"
    r"fees=([-+0-9.eE]+),\s*"
    r"realized_net=([-+0-9.eE]+)"
)
ACTION_TYPE_RE = re.compile(r"\btype=([^,\s]+)")
ACTION_REASON_RE = re.compile(r"\breason=([^,\s]+)")
LEARNABILITY_PASS_RE = re.compile(
    r"learnability=\{enabled=true,\s*passed=true"
)
HEX_RELEASE_RE = re.compile(r"^[0-9a-f]{7,64}$")

T_CRITICAL_975 = (
    0.0, 12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365,
    2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
    2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069,
    2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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


def as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def write_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def student_t_mean_lcb95(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    standard_error = math.sqrt(max(0.0, variance) / len(values))
    degrees = len(values) - 1
    critical = T_CRITICAL_975[degrees] if degrees < len(T_CRITICAL_975) else 1.96
    return mean - critical * standard_error


def validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise ValueError("demo incubation policy schema is invalid")
    environment = policy.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("demo incubation environment policy missing")
    positive_ints = (
        "min_observation_count",
        "min_distinct_trading_days",
        "min_complete_closed_lots",
        "min_effective_learning_updates",
        "min_learning_update_days",
        "min_learnability_passed_updates",
    )
    if any(as_int(policy.get(name)) <= 0 for name in positive_ints):
        raise ValueError("demo incubation count thresholds must be positive")
    if (as_float(policy.get("min_observation_hours")) or 0.0) <= 0.0:
        raise ValueError("demo incubation observation hours must be positive")
    for name in ("min_positive_closed_lot_ratio", "max_learning_rollback_ratio"):
        value = as_float(policy.get(name))
        if value is None or not 0.0 <= value <= 1.0:
            raise ValueError(f"demo incubation ratio is invalid: {name}")
    max_drawdown = as_float(policy.get("max_drawdown_pct"))
    if max_drawdown is None or max_drawdown <= 0.0:
        raise ValueError("demo incubation max drawdown is invalid")
    statuses = policy.get("required_latest_mechanism_statuses")
    if not isinstance(statuses, list) or not statuses:
        raise ValueError("demo incubation mechanism statuses are invalid")
    if not isinstance(policy.get("require_state_restore_after_restart"), bool):
        raise ValueError("demo incubation restart restore policy is invalid")


def environment_identity(config_path: Path) -> dict[str, Any]:
    return {
        "system_mode": str(config_value(config_path, "system.mode")).lower(),
        "exchange_platform": str(
            config_value(config_path, "exchange.platform")
        ).lower(),
        "testnet": config_value(config_path, "exchange.bybit.testnet"),
        "demo_trading": config_value(
            config_path, "exchange.bybit.demo_trading"
        ),
        "self_evolution_enabled": config_value(
            config_path, "self_evolution.enabled"
        ),
    }


def release_sha(manifest: dict[str, Any]) -> str:
    release = manifest.get("release")
    git = manifest.get("git")
    release_value = (
        str(release.get("git_sha", "")).strip().lower()
        if isinstance(release, dict)
        else ""
    )
    git_value = (
        str(git.get("commit", "")).strip().lower()
        if isinstance(git, dict)
        else ""
    )
    value = release_value or git_value
    if not HEX_RELEASE_RE.fullmatch(value):
        raise ValueError("run manifest release git sha is missing or invalid")
    if release_value and git_value and not (
        release_value.startswith(git_value) or git_value.startswith(release_value)
    ):
        raise ValueError("run manifest release/git identity mismatch")
    return value


def extract_account_samples(log_text: str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        if "RUNTIME_STATUS:" not in line:
            continue
        timestamp = TIMESTAMP_RE.search(line)
        tick = TICK_RE.search(line)
        boot = BOOT_RE.search(line)
        account = ACCOUNT_RE.search(line)
        if not timestamp or not tick or not boot or not account:
            continue
        values = [as_float(account.group(index)) for index in range(1, 7)]
        if any(value is None for value in values):
            continue
        parsed_ts = dt.datetime.strptime(
            timestamp.group(1), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=dt.timezone.utc)
        samples.append(
            {
                "key": f"{boot.group(1)}|{tick.group(1)}",
                "timestamp_utc": utc_iso(parsed_ts),
                "boot_id": boot.group(1),
                "tick": int(tick.group(1)),
                "equity_usd": values[0],
                "drawdown_pct": values[1],
                "notional_usd": values[2],
                "realized_pnl_usd": values[3],
                "fees_usd": values[4],
                "realized_net_usd": values[5],
            }
        )
    return samples


def extract_learning_actions(
    log_text: str, runtime_boot_id: str
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        if "SELF_EVOLUTION_ACTION:" not in line:
            continue
        timestamp = TIMESTAMP_RE.search(line)
        if not timestamp:
            continue
        marker = line.split("SELF_EVOLUTION_ACTION:", 1)[1].strip()
        action_type = ACTION_TYPE_RE.search(marker)
        reason = ACTION_REASON_RE.search(marker)
        parsed_ts = dt.datetime.strptime(
            timestamp.group(1), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=dt.timezone.utc)
        identity = {
            "boot_id": runtime_boot_id,
            "timestamp_utc": utc_iso(parsed_ts),
            "payload": marker,
        }
        actions.append(
            {
                "key": canonical_sha256(identity),
                "timestamp_utc": identity["timestamp_utc"],
                "boot_id": runtime_boot_id,
                "type": action_type.group(1).lower() if action_type else "unknown",
                "reason": reason.group(1) if reason else "",
                "learnability_passed": bool(LEARNABILITY_PASS_RE.search(marker)),
                "payload_sha256": canonical_sha256(marker),
            }
        )
    return actions


def extract_complete_lots(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    if ledger.get("schema_version") != "trade_ledger_v1":
        raise ValueError("trade ledger schema is invalid")
    quality = ledger.get("quality")
    if not isinstance(quality, dict):
        raise ValueError("trade ledger quality is missing")
    if as_int(quality.get("conflicting_duplicate_count")) > 0:
        raise ValueError("trade ledger contains conflicting fill identities")
    if as_int(quality.get("malformed_fill_count")) > 0:
        raise ValueError("trade ledger contains malformed fills")
    if as_int(quality.get("position_reconciliation_mismatch_count")) > 0:
        raise ValueError("trade ledger contains position reconciliation mismatch")
    raw_lots = ledger.get("closed_lots")
    if not isinstance(raw_lots, list):
        raise ValueError("trade ledger closed lots are missing")
    lots: list[dict[str, Any]] = []
    for item in raw_lots:
        if not isinstance(item, dict):
            continue
        opened_at = str(item.get("opened_at_utc", "")).strip()
        closed_at = str(item.get("closed_at_utc", "")).strip()
        opening_fill_id = str(item.get("opening_fill_id", "")).strip()
        closing_fill_id = str(item.get("closing_fill_id", "")).strip()
        net = as_float(item.get("net_pnl_usd"))
        qty = as_float(item.get("qty"))
        if (
            opened_at == "before_evaluation_window"
            or parse_utc(opened_at) is None
            or parse_utc(closed_at) is None
            or not opening_fill_id
            or not closing_fill_id
            or net is None
            or qty is None
            or qty <= 0.0
        ):
            continue
        identity = {
            "symbol": str(item.get("symbol", "")).strip().upper(),
            "opening_fill_id": opening_fill_id,
            "closing_fill_id": closing_fill_id,
            "qty": qty,
            "opened_at_utc": opened_at,
            "closed_at_utc": closed_at,
        }
        lots.append(
            {
                "key": canonical_sha256(identity),
                **identity,
                "side": str(item.get("side", "")).strip().upper(),
                "net_pnl_usd": net,
            }
        )
    return lots


def normalize_observation(
    *,
    policy: dict[str, Any],
    config_path: Path,
    manifest_path: Path,
    closed_loop_report_path: Path,
    runtime_assess_path: Path,
    trade_ledger_path: Path,
    runtime_log_path: Path,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    paths = {
        "config": config_path,
        "run_manifest": manifest_path,
        "closed_loop_report": closed_loop_report_path,
        "runtime_assess": runtime_assess_path,
        "trade_ledger": trade_ledger_path,
        "runtime_log": runtime_log_path,
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise ValueError("demo incubation source artifact missing: " + ",".join(missing))

    manifest = read_object(manifest_path)
    report = read_object(closed_loop_report_path)
    runtime = read_object(runtime_assess_path)
    ledger = read_object(trade_ledger_path)
    log_text = runtime_log_path.read_text(encoding="utf-8", errors="replace")
    environment = environment_identity(config_path)
    release = release_sha(manifest)
    raw_config_sha = file_sha256(config_path)
    execution_policy_sha = policy_sha256(config_path)
    policy_hash = canonical_sha256(policy)
    generation_identity = {
        "release_git_sha": release,
        "runtime_config_sha256": raw_config_sha,
        "execution_policy_sha256": execution_policy_sha,
        "incubation_policy_sha256": policy_hash,
        "environment": environment,
    }
    generation_id = canonical_sha256(generation_identity)

    metrics = runtime.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("runtime assess metrics are missing")
    runtime_boot_id = str(metrics.get("runtime_boot_id_latest", "")).strip()
    if not runtime_boot_id:
        raise ValueError("runtime boot identity is missing")
    run_id = str(report.get("run_id") or manifest.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("closed-loop run identity is missing")

    sections = report.get("sections")
    sections = sections if isinstance(sections, dict) else {}
    replay = sections.get("replay_validation")
    mechanism = sections.get("closed_loop_mechanism")
    replay = replay if isinstance(replay, dict) else {}
    mechanism = mechanism if isinstance(mechanism, dict) else {}
    observation = {
        "run_id": run_id,
        "generated_at_utc": str(report.get("generated_at_utc", "")).strip(),
        "source_sha256": {
            name: file_sha256(path) for name, path in paths.items()
        },
        "runtime_boot_id": runtime_boot_id,
        "overall_status": str(report.get("overall_status", "")).strip().upper(),
        "runtime_verdict": str(runtime.get("verdict", "")).strip().upper(),
        "replay_status": str(
            report.get("replay_readiness_status")
            or replay.get("readiness_status")
            or replay.get("status")
            or ""
        ).strip().upper(),
        "mechanism_status": str(
            report.get("closed_loop_mechanism_status")
            or mechanism.get("readiness_status")
            or mechanism.get("status")
            or ""
        ).strip().upper(),
        "convergence_status": str(
            report.get("trading_convergence_status", "")
        ).strip().upper(),
        "hard_safety_metrics": {
            name: as_int(metrics.get(name)) for name in HARD_SAFETY_METRICS
        },
        "self_evolution_state_restored": bool(
            re.search(
                r"SELF_EVOLUTION_STATE_RESTORED:[^\n]*\bboot_id="
                + re.escape(runtime_boot_id)
                + r"(?:,|\s|$)",
                log_text,
            )
        ),
        "account_samples": extract_account_samples(log_text),
        "learning_actions": extract_learning_actions(log_text, runtime_boot_id),
        "complete_closed_lots": extract_complete_lots(ledger),
    }
    observation["sha256"] = canonical_sha256(observation)
    return generation_id, generation_identity, observation


def empty_state(policy_hash: str) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA,
        "incubation_policy_sha256": policy_hash,
        "active_generation_id": "",
        "generations": {},
        "updated_at_utc": utc_iso(),
    }


def merge_unique(
    observations: list[dict[str, Any]], field: str
) -> tuple[list[dict[str, Any]], list[str]]:
    values: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    for observation in observations:
        entries = observation.get(field)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            if not key:
                continue
            existing = values.get(key)
            if existing is not None and existing != item:
                conflicts.append(f"{field} identity mutated: {key}")
            else:
                values[key] = item
    return list(values.values()), conflicts


def evaluate_generation(
    generation_id: str,
    generation: dict[str, Any],
    policy: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    run_map = generation.get("observations")
    if not isinstance(run_map, dict):
        raise ValueError("demo incubation generation observations are invalid")
    observations = sorted(
        (item for item in run_map.values() if isinstance(item, dict)),
        key=lambda item: str(item.get("generated_at_utc", "")),
    )
    account_samples, account_conflicts = merge_unique(observations, "account_samples")
    learning_actions, learning_conflicts = merge_unique(
        observations, "learning_actions"
    )
    closed_lots, lot_conflicts = merge_unique(observations, "complete_closed_lots")
    integrity_reasons = account_conflicts + learning_conflicts + lot_conflicts

    account_samples.sort(key=lambda item: str(item.get("timestamp_utc", "")))
    first_sample = account_samples[0] if account_samples else None
    last_sample = account_samples[-1] if account_samples else None
    first_ts = parse_utc(first_sample.get("timestamp_utc")) if first_sample else None
    last_ts = parse_utc(last_sample.get("timestamp_utc")) if last_sample else None
    observation_hours = (
        max(0.0, (last_ts - first_ts).total_seconds() / 3600.0)
        if first_ts is not None and last_ts is not None
        else 0.0
    )
    equity_change = (
        float(last_sample["equity_usd"]) - float(first_sample["equity_usd"])
        if first_sample and last_sample
        else None
    )
    max_drawdown = max(
        (float(item.get("drawdown_pct") or 0.0) for item in account_samples),
        default=None,
    )
    latest_abs_notional = (
        abs(float(last_sample["notional_usd"])) if last_sample else None
    )

    samples_by_boot: dict[str, list[dict[str, Any]]] = {}
    for sample in account_samples:
        samples_by_boot.setdefault(str(sample.get("boot_id", "")), []).append(sample)
    realized_net_change = 0.0
    realized_net_boot_count = 0
    for samples in samples_by_boot.values():
        samples.sort(key=lambda item: (as_int(item.get("tick")), str(item.get("timestamp_utc"))))
        if len(samples) < 2:
            continue
        realized_net_change += float(samples[-1]["realized_net_usd"]) - float(
            samples[0]["realized_net_usd"]
        )
        realized_net_boot_count += 1

    lot_values = [float(item["net_pnl_usd"]) for item in closed_lots]
    total_lot_net = sum(lot_values)
    positive_lot_count = sum(value > 0.0 for value in lot_values)
    positive_lot_ratio = (
        positive_lot_count / len(lot_values) if lot_values else 0.0
    )
    mean_lot_net = sum(lot_values) / len(lot_values) if lot_values else None
    mean_lot_net_lcb95 = student_t_mean_lcb95(lot_values)
    trading_days = sorted(
        {
            parsed.date().isoformat()
            for item in closed_lots
            if (parsed := parse_utc(item.get("closed_at_utc"))) is not None
        }
    )

    effective_updates = [
        item for item in learning_actions if item.get("type") == "updated"
    ]
    rollbacks = [
        item for item in learning_actions if item.get("type") == "rolled_back"
    ]
    learning_update_days = sorted(
        {
            parsed.date().isoformat()
            for item in effective_updates
            if (parsed := parse_utc(item.get("timestamp_utc"))) is not None
        }
    )
    learnability_passed_updates = sum(
        item.get("learnability_passed") is True for item in effective_updates
    )
    rollback_denominator = len(effective_updates) + len(rollbacks)
    rollback_ratio = (
        len(rollbacks) / rollback_denominator if rollback_denominator else 0.0
    )

    safety_events: list[dict[str, Any]] = []
    for observation in observations:
        metrics = observation.get("hard_safety_metrics")
        if not isinstance(metrics, dict):
            integrity_reasons.append("hard safety metrics missing from observation")
            continue
        for name in HARD_SAFETY_METRICS:
            value = as_int(metrics.get(name))
            if value > 0:
                safety_events.append(
                    {
                        "run_id": observation.get("run_id"),
                        "metric": name,
                        "value": value,
                    }
                )

    latest = observations[-1] if observations else {}
    observed_boot_ids: list[str] = []
    restored_boot_ids: set[str] = set()
    for observation in observations:
        boot_id = str(observation.get("runtime_boot_id", "")).strip()
        if boot_id and boot_id not in observed_boot_ids:
            observed_boot_ids.append(boot_id)
        if boot_id and observation.get("self_evolution_state_restored") is True:
            restored_boot_ids.add(boot_id)
    missing_restart_restore_boot_ids = [
        boot_id
        for boot_id in observed_boot_ids[1:]
        if boot_id not in restored_boot_ids
    ]
    pending_reasons: list[str] = []
    hard_block_reasons = list(integrity_reasons)
    identity = generation.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    expected_environment = policy["environment"]
    observed_environment = identity.get("environment")
    if observed_environment != expected_environment:
        hard_block_reasons.append(
            "runtime configuration is not the frozen Bybit Demo environment"
        )
    if safety_events:
        hard_block_reasons.append(
            f"hard safety events observed in generation: {len(safety_events)}"
        )

    def require(condition: bool, reason: str) -> None:
        if not condition:
            pending_reasons.append(reason)

    require(
        observation_hours >= float(policy["min_observation_hours"]),
        f"observation_hours={observation_hours:.3f} < {policy['min_observation_hours']}",
    )
    require(
        len(observations) >= int(policy["min_observation_count"]),
        f"observation_count={len(observations)} < {policy['min_observation_count']}",
    )
    if policy.get("require_state_restore_after_restart") is True:
        require(
            not missing_restart_restore_boot_ids,
            "self-evolution state was not restored after restart: "
            + ",".join(missing_restart_restore_boot_ids),
        )
    require(
        len(trading_days) >= int(policy["min_distinct_trading_days"]),
        f"distinct_trading_days={len(trading_days)} < {policy['min_distinct_trading_days']}",
    )
    require(
        len(closed_lots) >= int(policy["min_complete_closed_lots"]),
        f"complete_closed_lots={len(closed_lots)} < {policy['min_complete_closed_lots']}",
    )
    require(
        positive_lot_ratio >= float(policy["min_positive_closed_lot_ratio"]),
        "positive_closed_lot_ratio="
        f"{positive_lot_ratio:.6f} < {policy['min_positive_closed_lot_ratio']}",
    )
    require(
        total_lot_net > float(policy["min_total_closed_lot_net_usd"]),
        f"total_closed_lot_net_usd={total_lot_net:.6f} is not positive",
    )
    require(
        mean_lot_net_lcb95 is not None
        and mean_lot_net_lcb95
        > float(policy["min_mean_closed_lot_net_lcb95_usd"]),
        "mean_closed_lot_net_lcb95_usd is not positive",
    )
    require(
        equity_change is not None
        and equity_change > float(policy["min_account_equity_change_usd"]),
        "account equity change is not positive",
    )
    require(
        realized_net_boot_count > 0
        and realized_net_change
        > float(policy["min_account_realized_net_change_usd"]),
        "account realized net change including funding is not positive",
    )
    require(
        len(effective_updates) >= int(policy["min_effective_learning_updates"]),
        f"effective_learning_updates={len(effective_updates)} < "
        f"{policy['min_effective_learning_updates']}",
    )
    require(
        len(learning_update_days) >= int(policy["min_learning_update_days"]),
        f"learning_update_days={len(learning_update_days)} < "
        f"{policy['min_learning_update_days']}",
    )
    require(
        learnability_passed_updates
        >= int(policy["min_learnability_passed_updates"]),
        f"learnability_passed_updates={learnability_passed_updates} < "
        f"{policy['min_learnability_passed_updates']}",
    )
    require(
        rollback_ratio <= float(policy["max_learning_rollback_ratio"]),
        f"learning_rollback_ratio={rollback_ratio:.6f} > "
        f"{policy['max_learning_rollback_ratio']}",
    )
    if policy.get("require_latest_flat") is True:
        require(
            latest_abs_notional is not None and latest_abs_notional <= 1e-6,
            "latest account position is not flat",
        )
    require(
        str(latest.get("overall_status", ""))
        == str(policy["required_latest_overall_status"]),
        "latest closed-loop overall status did not pass without actions",
    )
    require(
        str(latest.get("runtime_verdict", ""))
        == str(policy["required_latest_runtime_verdict"]),
        "latest runtime verdict is not PASS",
    )
    require(
        str(latest.get("replay_status", ""))
        == str(policy["required_latest_replay_status"]),
        "latest replay readiness is not PASS",
    )
    require(
        str(latest.get("mechanism_status", ""))
        in {str(item) for item in policy["required_latest_mechanism_statuses"]},
        "latest closed-loop mechanism status did not pass",
    )
    require(
        str(latest.get("convergence_status", ""))
        == str(policy["required_latest_convergence_status"]),
        "latest trading convergence status is not live-fill validated",
    )
    if max_drawdown is not None and max_drawdown > float(policy["max_drawdown_pct"]):
        hard_block_reasons.append(
            f"max_drawdown_pct={max_drawdown:.6f} > {policy['max_drawdown_pct']}"
        )

    if hard_block_reasons:
        decision = BLOCKED
    elif pending_reasons:
        decision = INCUBATING
    else:
        decision = ELIGIBLE

    return {
        "schema_version": REPORT_SCHEMA,
        "generated_at_utc": utc_iso(now),
        "decision": decision,
        "auto_live_switch": False,
        "mainnet_runtime_enabled": False,
        "next_transition": (
            "manual_live_test_review" if decision == ELIGIBLE else "continue_demo"
        ),
        "generation_id": generation_id,
        "generation_identity": identity,
        "thresholds": policy,
        "hard_block_reasons": hard_block_reasons,
        "pending_reasons": pending_reasons,
        "latest": {
            "run_id": latest.get("run_id"),
            "generated_at_utc": latest.get("generated_at_utc"),
            "overall_status": latest.get("overall_status"),
            "runtime_verdict": latest.get("runtime_verdict"),
            "replay_status": latest.get("replay_status"),
            "mechanism_status": latest.get("mechanism_status"),
            "convergence_status": latest.get("convergence_status"),
            "flat": latest_abs_notional is not None and latest_abs_notional <= 1e-6,
        },
        "evidence": {
            "observation_count": len(observations),
            "first_sample_utc": utc_iso(first_ts) if first_ts else None,
            "last_sample_utc": utc_iso(last_ts) if last_ts else None,
            "observation_hours": observation_hours,
            "account_sample_count": len(account_samples),
            "account_boot_count": len(samples_by_boot),
            "observed_boot_ids": observed_boot_ids,
            "state_restored_boot_ids": sorted(restored_boot_ids),
            "missing_restart_restore_boot_ids": missing_restart_restore_boot_ids,
            "account_realized_net_boot_count": realized_net_boot_count,
            "account_equity_change_usd": equity_change,
            "account_realized_net_change_usd": realized_net_change,
            "max_drawdown_pct": max_drawdown,
            "latest_abs_notional_usd": latest_abs_notional,
            "complete_closed_lot_count": len(closed_lots),
            "positive_closed_lot_count": positive_lot_count,
            "positive_closed_lot_ratio": positive_lot_ratio,
            "total_closed_lot_net_usd": total_lot_net,
            "mean_closed_lot_net_usd": mean_lot_net,
            "mean_closed_lot_net_lcb95_usd": mean_lot_net_lcb95,
            "net_confidence_method": (
                "closed_lot_student_t_two_sided_95_lower_bound"
            ),
            "distinct_trading_days": trading_days,
            "learning_action_count": len(learning_actions),
            "effective_learning_update_count": len(effective_updates),
            "learning_update_days": learning_update_days,
            "learnability_passed_update_count": learnability_passed_updates,
            "learning_rollback_count": len(rollbacks),
            "learning_rollback_ratio": rollback_ratio,
            "hard_safety_event_count": len(safety_events),
            "hard_safety_events": safety_events[:50],
        },
        "disclaimer": (
            "Demo profitability is evidence for review only and does not guarantee "
            "future live profitability. Mainnet remains disabled."
        ),
    }


def evaluate_and_update(
    *,
    policy_path: Path,
    state_path: Path,
    config_path: Path,
    manifest_path: Path,
    closed_loop_report_path: Path,
    runtime_assess_path: Path,
    trade_ledger_path: Path,
    runtime_log_path: Path,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = now or utc_now()
    policy = read_object(policy_path)
    validate_policy(policy)
    policy_hash = canonical_sha256(policy)
    state = read_object(state_path) if state_path.is_file() else empty_state(policy_hash)
    if state.get("schema_version") != STATE_SCHEMA:
        raise ValueError("demo incubation state schema is invalid")
    if state.get("incubation_policy_sha256") != policy_hash:
        state = empty_state(policy_hash)

    generation_id, identity, observation = normalize_observation(
        policy=policy,
        config_path=config_path,
        manifest_path=manifest_path,
        closed_loop_report_path=closed_loop_report_path,
        runtime_assess_path=runtime_assess_path,
        trade_ledger_path=trade_ledger_path,
        runtime_log_path=runtime_log_path,
    )
    generations = state.setdefault("generations", {})
    if not isinstance(generations, dict):
        raise ValueError("demo incubation state generations are invalid")
    generation = generations.setdefault(
        generation_id,
        {
            "created_at_utc": utc_iso(current),
            "identity": identity,
            "observations": {},
        },
    )
    if generation.get("identity") != identity:
        raise ValueError("demo incubation generation identity mutated")
    observations = generation.setdefault("observations", {})
    if not isinstance(observations, dict):
        raise ValueError("demo incubation observations are invalid")
    run_id = observation["run_id"]
    existing = observations.get(run_id)
    if existing is not None and existing != observation:
        raise ValueError(f"demo incubation run evidence mutated: {run_id}")
    observations[run_id] = observation
    state["active_generation_id"] = generation_id
    state["updated_at_utc"] = utc_iso(current)
    report = evaluate_generation(generation_id, generation, policy, current)
    generation["latest_decision"] = report["decision"]
    generation["updated_at_utc"] = utc_iso(current)
    generation["latest_report_sha256"] = canonical_sha256(report)
    return state, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate longitudinal Bybit Demo incubation evidence"
    )
    parser.add_argument("--policy", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-manifest", required=True)
    parser.add_argument("--closed-loop-report", required=True)
    parser.add_argument("--runtime-assess", required=True)
    parser.add_argument("--trade-ledger", required=True)
    parser.add_argument("--runtime-log", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_path = Path(args.state)
    state, report = evaluate_and_update(
        policy_path=Path(args.policy),
        state_path=state_path,
        config_path=Path(args.config),
        manifest_path=Path(args.run_manifest),
        closed_loop_report_path=Path(args.closed_loop_report),
        runtime_assess_path=Path(args.runtime_assess),
        trade_ledger_path=Path(args.trade_ledger),
        runtime_log_path=Path(args.runtime_log),
    )
    write_object(state_path, state)
    write_object(Path(args.output), report)
    print(
        "DEMO_INCUBATION: "
        f"decision={report['decision']}, "
        f"generation={report['generation_id']}, "
        f"observations={report['evidence']['observation_count']}, "
        f"closed_lots={report['evidence']['complete_closed_lot_count']}, "
        f"learning_updates={report['evidence']['effective_learning_update_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
