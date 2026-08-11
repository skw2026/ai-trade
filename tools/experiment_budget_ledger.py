#!/usr/bin/env python3
"""Maintain the append-only experiment registration and failure-budget ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
from typing import Any, Mapping


RECORD_SCHEMA_VERSION = "experiment_budget_ledger_record_v1"
CHECKPOINT_SCHEMA_VERSION = "experiment_budget_ledger_checkpoint_v1"
DECISION_SCHEMA_VERSION = "experiment_budget_ledger_decision_v1"
GENESIS_HASH = "0" * 64
OUTCOMES = frozenset({"SUPPORTED", "FALSIFIED", "INCONCLUSIVE"})
FAILURE_OUTCOMES = frozenset({"FALSIFIED", "INCONCLUSIVE"})
DECISION_ALLOW = "ALLOW_NEXT_EXPERIMENT"
DECISION_STOP = "STOP_CURRENT_FAMILY"
DECISION_BLOCK = "BLOCK_INVALID_LEDGER"
DEFAULT_CONFIG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "config"
    / "decision_evidence_validation.json"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class LedgerValidationError(ValueError):
    """Raised when the persisted chain or a proposed record is invalid."""


class MultipleChangedDimensions(LedgerValidationError):
    """Raised for a valid-looking proposal that changes several dimensions."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _identity_definition(value: object) -> object:
    """Remove presentation-only names from a stable semantic definition."""

    if isinstance(value, dict):
        return {
            key: _identity_definition(item)
            for key, item in value.items()
            if key != "display_name"
        }
    if isinstance(value, list):
        return [_identity_definition(item) for item in value]
    return value


def stable_definition_id(definition: object) -> str:
    return canonical_sha256(_identity_definition(definition))


def record_hash(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("record_hash", None)
    return canonical_sha256(payload)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerValidationError(f"{field} must be a non-empty string")
    return value


def _experiment_id(value: object) -> str:
    normalized = _non_empty_string(value, "experiment_id")
    if _EXPERIMENT_ID_RE.fullmatch(normalized) is None:
        raise LedgerValidationError("experiment_id is invalid")
    return normalized


def _sha256(value: object, field: str) -> str:
    if not _is_sha256(value):
        raise LedgerValidationError(f"{field} must be a lowercase SHA256")
    return str(value)


def _utc_timestamp(value: object, field: str) -> tuple[str, dt.datetime]:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise LedgerValidationError(f"{field} must be a strict UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LedgerValidationError(f"{field} is not a valid UTC timestamp") from exc
    if parsed.utcoffset() != dt.timedelta(0):
        raise LedgerValidationError(f"{field} must use UTC")
    return value, parsed


def _definition(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise LedgerValidationError(f"{field} must be a non-empty object")
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LedgerValidationError(f"{field} is not canonical JSON") from exc
    identity = _identity_definition(value)
    if not isinstance(identity, dict) or not identity:
        raise LedgerValidationError(
            f"{field} must contain identity fields besides display_name"
        )
    return json.loads(canonical_json_bytes(value).decode("ascii"))


def _changed_dimensions(request: Mapping[str, Any]) -> list[str]:
    raw = request.get("changed_dimensions")
    if raw is None and "changed_dimension" in request:
        raw = [request.get("changed_dimension")]
    if not isinstance(raw, list):
        raise LedgerValidationError("changed_dimensions must be an array")
    dimensions = [
        _non_empty_string(value, "changed_dimension") for value in raw
    ]
    if len(dimensions) > 1:
        raise MultipleChangedDimensions(
            "an experiment may change exactly one dimension"
        )
    if len(dimensions) != 1 or len(set(dimensions)) != 1:
        raise LedgerValidationError(
            "an experiment must change exactly one unique dimension"
        )
    return dimensions


def _normalize_registration(request: object) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise LedgerValidationError("registration request must be an object")
    experiment_id = _experiment_id(request.get("experiment_id"))
    benchmark_id = _sha256(request.get("benchmark_id"), "benchmark_id")
    information_definition = _definition(
        request.get("information_set_definition"),
        "information_set_definition",
    )
    information_id = _sha256(
        request.get("information_set_id"), "information_set_id"
    )
    if information_id != stable_definition_id(information_definition):
        raise LedgerValidationError(
            "information_set_id does not match its canonical definition"
        )
    family_definition = _definition(
        request.get("hypothesis_family_definition"),
        "hypothesis_family_definition",
    )
    family_id = _sha256(
        request.get("hypothesis_family_id"), "hypothesis_family_id"
    )
    if family_id != stable_definition_id(family_definition):
        raise LedgerValidationError(
            "hypothesis_family_id does not match its canonical definition"
        )
    dimensions = _changed_dimensions(request)
    expected_direction = _non_empty_string(
        request.get("expected_direction"), "expected_direction"
    )
    stop_condition = _non_empty_string(
        request.get("stop_condition"), "stop_condition"
    )
    registered_at, registered_time = _utc_timestamp(
        request.get("registered_at"), "registered_at"
    )
    earliest_result_at = request.get("earliest_result_at")
    if earliest_result_at is not None:
        _, result_time = _utc_timestamp(
            earliest_result_at, "earliest_result_at"
        )
        if registered_time >= result_time:
            raise LedgerValidationError(
                "registered_at must precede earliest_result_at"
            )
    return {
        "experiment_id": experiment_id,
        "benchmark_id": benchmark_id,
        "information_set_definition": information_definition,
        "information_set_id": information_id,
        "hypothesis_family_definition": family_definition,
        "hypothesis_family_id": family_id,
        "changed_dimensions": dimensions,
        "expected_direction": expected_direction,
        "stop_condition": stop_condition,
        "registered_at": registered_at,
    }


def _normalize_observation(request: object) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise LedgerValidationError("observation request must be an object")
    experiment_id = _experiment_id(request.get("experiment_id"))
    outcome = _non_empty_string(request.get("outcome"), "outcome")
    if outcome not in OUTCOMES:
        raise LedgerValidationError(
            "outcome must be SUPPORTED, FALSIFIED, or INCONCLUSIVE"
        )
    observed_at, _ = _utc_timestamp(request.get("observed_at"), "observed_at")
    return {
        "experiment_id": experiment_id,
        "outcome": outcome,
        "observed_at": observed_at,
    }


def _read_failure_budgets(config_path: pathlib.Path) -> dict[str, int]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerValidationError("failure budget config is unreadable") from exc
    budgets = payload.get("failure_budgets") if isinstance(payload, dict) else None
    if not isinstance(budgets, dict):
        raise LedgerValidationError("failure_budgets config is missing")
    result: dict[str, int] = {}
    for key in ("family", "information_set"):
        value = budgets.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise LedgerValidationError(f"failure budget {key} is invalid")
        result[key] = value
    return result


def _checkpoint_path(ledger_path: pathlib.Path) -> pathlib.Path:
    return ledger_path.with_suffix(ledger_path.suffix + ".checkpoint.json")


def _validate_checkpoint(
    ledger_path: pathlib.Path,
    ledger_bytes: bytes,
    record_count: int,
    tail_hash: str,
) -> None:
    checkpoint_path = _checkpoint_path(ledger_path)
    if not checkpoint_path.is_file():
        if record_count == 0 and not ledger_bytes:
            return
        raise LedgerValidationError("ledger checkpoint is missing")
    try:
        checkpoint_bytes = checkpoint_path.read_bytes()
        if not checkpoint_bytes.endswith(b"\n"):
            raise LedgerValidationError("ledger checkpoint is not canonical")
        checkpoint = json.loads(checkpoint_bytes.decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerValidationError("ledger checkpoint is unreadable") from exc
    if not isinstance(checkpoint, dict):
        raise LedgerValidationError("ledger checkpoint must be an object")
    if checkpoint_bytes != canonical_json_bytes(checkpoint) + b"\n":
        raise LedgerValidationError("ledger checkpoint is not canonical")
    expected = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "record_count": record_count,
        "tail_record_hash": tail_hash,
        "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
    }
    if checkpoint != expected:
        raise LedgerValidationError("ledger checkpoint does not match ledger")


def _record_timestamp(record: Mapping[str, Any]) -> tuple[str, dt.datetime]:
    field = "registered_at" if record.get("record_type") == "REGISTER" else "observed_at"
    return _utc_timestamp(record.get(field), field)


def audit_ledger(ledger_path: pathlib.Path | str) -> dict[str, Any]:
    path = pathlib.Path(ledger_path)
    try:
        ledger_bytes = path.read_bytes() if path.is_file() else b""
    except OSError as exc:
        raise LedgerValidationError("ledger is unreadable") from exc
    if ledger_bytes and not ledger_bytes.endswith(b"\n"):
        raise LedgerValidationError("ledger must end with a newline")
    try:
        raw_lines = ledger_bytes.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise LedgerValidationError("ledger must contain canonical ASCII JSON") from exc
    if any(not line for line in raw_lines):
        raise LedgerValidationError("ledger contains an empty record")

    records: list[dict[str, Any]] = []
    registrations: dict[str, dict[str, Any]] = {}
    observations: dict[str, dict[str, Any]] = {}
    definitions: dict[tuple[str, str], object] = {}
    failure_counts_by_family: dict[str, int] = {}
    failure_counts_by_information_set: dict[str, int] = {}
    benchmark_id: str | None = None
    previous_hash = GENESIS_HASH
    previous_time: dt.datetime | None = None
    common_keys = {
        "schema_version",
        "sequence",
        "record_type",
        "previous_hash",
        "record_hash",
    }
    registration_keys = {
        "experiment_id",
        "benchmark_id",
        "information_set_definition",
        "information_set_id",
        "hypothesis_family_definition",
        "hypothesis_family_id",
        "changed_dimensions",
        "expected_direction",
        "stop_condition",
        "registered_at",
    }
    observation_keys = {"experiment_id", "outcome", "observed_at"}

    for line_number, raw_line in enumerate(raw_lines, start=1):
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise LedgerValidationError(
                f"ledger JSON is invalid at line {line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise LedgerValidationError(
                f"ledger record is not an object at line {line_number}"
            )
        try:
            canonical = canonical_json_bytes(record).decode("ascii")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise LedgerValidationError(
                f"ledger record is not canonical at line {line_number}"
            ) from exc
        if raw_line != canonical:
            raise LedgerValidationError(
                f"ledger record is not canonical at line {line_number}"
            )
        record_type = record.get("record_type")
        expected_keys = common_keys | (
            registration_keys if record_type == "REGISTER" else observation_keys
        )
        if record_type not in {"REGISTER", "OBSERVE"} or set(record) != expected_keys:
            raise LedgerValidationError(
                f"ledger record fields are invalid at line {line_number}"
            )
        if (
            record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(record.get("sequence"), int)
            or isinstance(record.get("sequence"), bool)
            or record.get("sequence") != line_number
            or record.get("previous_hash") != previous_hash
            or not _is_sha256(record.get("record_hash"))
            or record.get("record_hash") != record_hash(record)
        ):
            raise LedgerValidationError(
                f"ledger hash chain is invalid at line {line_number}"
            )
        _, timestamp = _record_timestamp(record)
        if previous_time is not None and timestamp <= previous_time:
            raise LedgerValidationError(
                f"ledger UTC timestamps are not strictly increasing at line {line_number}"
            )

        if record_type == "REGISTER":
            normalized = _normalize_registration(record)
            experiment_id = normalized["experiment_id"]
            if experiment_id in registrations:
                raise LedgerValidationError("ledger contains a duplicate experiment_id")
            if benchmark_id is None:
                benchmark_id = normalized["benchmark_id"]
            elif normalized["benchmark_id"] != benchmark_id:
                raise LedgerValidationError("ledger contains benchmark drift")
            for kind in ("information_set", "hypothesis_family"):
                identity = normalized[f"{kind}_id"]
                definition = _identity_definition(normalized[f"{kind}_definition"])
                key = (kind, identity)
                if key in definitions and definitions[key] != definition:
                    raise LedgerValidationError(
                        f"ledger contains {kind} identity drift"
                    )
                definitions[key] = definition
            registrations[experiment_id] = normalized
        else:
            normalized_observation = _normalize_observation(record)
            experiment_id = normalized_observation["experiment_id"]
            if experiment_id not in registrations:
                raise LedgerValidationError(
                    "ledger observation has no prior registration"
                )
            if experiment_id in observations:
                raise LedgerValidationError(
                    "ledger contains more than one outcome for an experiment"
                )
            registration = registrations[experiment_id]
            _, registered_time = _utc_timestamp(
                registration["registered_at"], "registered_at"
            )
            if timestamp <= registered_time:
                raise LedgerValidationError(
                    "experiment outcome does not follow registration"
                )
            observations[experiment_id] = normalized_observation
            if normalized_observation["outcome"] in FAILURE_OUTCOMES:
                family_id = registration["hypothesis_family_id"]
                information_id = registration["information_set_id"]
                failure_counts_by_family[family_id] = (
                    failure_counts_by_family.get(family_id, 0) + 1
                )
                failure_counts_by_information_set[information_id] = (
                    failure_counts_by_information_set.get(information_id, 0) + 1
                )

        records.append(record)
        previous_hash = str(record["record_hash"])
        previous_time = timestamp

    _validate_checkpoint(
        path, ledger_bytes, len(records), previous_hash
    )
    return {
        "records": records,
        "registrations": registrations,
        "observations": observations,
        "benchmark_id": benchmark_id,
        "definitions": definitions,
        "failure_counts_by_family": failure_counts_by_family,
        "failure_counts_by_information_set": failure_counts_by_information_set,
        "tail_record_hash": previous_hash,
        "last_timestamp": previous_time,
    }


def _remaining_budgets(
    state: Mapping[str, Any],
    budgets: Mapping[str, int],
    family_id: str | None,
    information_set_id: str | None,
) -> dict[str, int]:
    family_used = int(
        state["failure_counts_by_family"].get(family_id, 0)
        if family_id is not None
        else 0
    )
    information_used = int(
        state["failure_counts_by_information_set"].get(information_set_id, 0)
        if information_set_id is not None
        else 0
    )
    return {
        "family": max(0, int(budgets["family"]) - family_used),
        "information_set": max(
            0, int(budgets["information_set"]) - information_used
        ),
    }


def _decision_report(
    *,
    operation: str,
    decision: str,
    appended: bool,
    remaining_budgets: Mapping[str, int] | None = None,
    reasons: list[str] | None = None,
    experiment_id: str | None = None,
    family_id: str | None = None,
    information_set_id: str | None = None,
    benchmark_id: str | None = None,
    expected_benchmark_id: str | None = None,
    actual_benchmark_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "operation": operation,
        "decision": decision,
        "appended": bool(appended),
        "experiment_id": experiment_id,
        "hypothesis_family_id": family_id,
        "information_set_id": information_set_id,
        "benchmark_id": benchmark_id,
        "expected_benchmark_id": expected_benchmark_id,
        "actual_benchmark_id": actual_benchmark_id,
        "remaining_budgets": (
            dict(remaining_budgets) if remaining_budgets is not None else None
        ),
        "reasons": list(reasons or []),
    }


def _write_checkpoint(ledger_path: pathlib.Path) -> None:
    ledger_bytes = ledger_path.read_bytes()
    lines = ledger_bytes.splitlines()
    tail = json.loads(lines[-1].decode("ascii"))["record_hash"] if lines else GENESIS_HASH
    checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "record_count": len(lines),
        "tail_record_hash": tail,
        "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
    }
    checkpoint_path = _checkpoint_path(ledger_path)
    temporary = checkpoint_path.with_name(
        checkpoint_path.name + f".tmp.{os.getpid()}"
    )
    temporary.write_bytes(canonical_json_bytes(checkpoint) + b"\n")
    os.replace(temporary, checkpoint_path)


def _append_record(
    ledger_path: pathlib.Path,
    state: Mapping[str, Any],
    record_type: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    timestamp_field = "registered_at" if record_type == "REGISTER" else "observed_at"
    _, timestamp = _utc_timestamp(payload.get(timestamp_field), timestamp_field)
    last_timestamp = state.get("last_timestamp")
    if isinstance(last_timestamp, dt.datetime) and timestamp <= last_timestamp:
        raise LedgerValidationError(
            "new ledger timestamp must be strictly later than history"
        )
    record = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "sequence": len(state["records"]) + 1,
        "record_type": record_type,
        "previous_hash": state["tail_record_hash"],
        **payload,
    }
    record["record_hash"] = record_hash(record)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("ab") as handle:
        handle.write(canonical_json_bytes(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    _write_checkpoint(ledger_path)
    audit_ledger(ledger_path)
    return record


def _definition_matches_history(
    state: Mapping[str, Any], kind: str, identity: str, definition: object
) -> bool:
    existing = state["definitions"].get((kind, identity))
    return existing is None or existing == _identity_definition(definition)


def register_experiment(
    ledger_path: pathlib.Path | str,
    config_path: pathlib.Path | str,
    request: object,
) -> dict[str, Any]:
    path = pathlib.Path(ledger_path)
    expected_benchmark_id: str | None = None
    verified_benchmark_id: str | None = None
    actual_benchmark_id = (
        request.get("benchmark_id")
        if isinstance(request, Mapping)
        and isinstance(request.get("benchmark_id"), str)
        else None
    )
    try:
        normalized = _normalize_registration(request)
        actual_benchmark_id = normalized["benchmark_id"]
        budgets = _read_failure_budgets(pathlib.Path(config_path))
        state = audit_ledger(path)
        expected_benchmark_id = state["benchmark_id"]
        verified_benchmark_id = expected_benchmark_id or actual_benchmark_id
        if expected_benchmark_id is None:
            expected_benchmark_id = actual_benchmark_id
        experiment_id = normalized["experiment_id"]
        family_id = normalized["hypothesis_family_id"]
        information_id = normalized["information_set_id"]
        if experiment_id in state["registrations"]:
            raise LedgerValidationError("experiment_id has already been registered")
        if (
            state["benchmark_id"] is not None
            and normalized["benchmark_id"] != state["benchmark_id"]
        ):
            raise LedgerValidationError("benchmark_id drift is forbidden")
        if not _definition_matches_history(
            state,
            "information_set",
            information_id,
            normalized["information_set_definition"],
        ):
            raise LedgerValidationError("information-set identity drift is forbidden")
        if not _definition_matches_history(
            state,
            "hypothesis_family",
            family_id,
            normalized["hypothesis_family_definition"],
        ):
            raise LedgerValidationError("hypothesis-family identity drift is forbidden")
        remaining = _remaining_budgets(
            state, budgets, family_id, information_id
        )
        if remaining["family"] == 0 or remaining["information_set"] == 0:
            return _decision_report(
                operation="register",
                decision=DECISION_STOP,
                appended=False,
                remaining_budgets=remaining,
                reasons=["failure budget is exhausted"],
                experiment_id=experiment_id,
                family_id=family_id,
                information_set_id=information_id,
                benchmark_id=verified_benchmark_id,
                expected_benchmark_id=expected_benchmark_id,
                actual_benchmark_id=actual_benchmark_id,
            )
        _append_record(path, state, "REGISTER", normalized)
        return _decision_report(
            operation="register",
            decision=DECISION_ALLOW,
            appended=True,
            remaining_budgets=remaining,
            experiment_id=experiment_id,
            family_id=family_id,
            information_set_id=information_id,
            benchmark_id=verified_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
        )
    except MultipleChangedDimensions as exc:
        request_map = request if isinstance(request, Mapping) else {}
        return _decision_report(
            operation="register",
            decision=DECISION_STOP,
            appended=False,
            reasons=[str(exc)],
            experiment_id=(
                str(request_map.get("experiment_id"))
                if request_map.get("experiment_id") is not None
                else None
            ),
            benchmark_id=verified_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
        )
    except (LedgerValidationError, OSError, TypeError, ValueError) as exc:
        return _decision_report(
            operation="register",
            decision=DECISION_BLOCK,
            appended=False,
            reasons=[str(exc)],
            benchmark_id=verified_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
        )


def observe_experiment(
    ledger_path: pathlib.Path | str,
    config_path: pathlib.Path | str,
    request: object,
) -> dict[str, Any]:
    path = pathlib.Path(ledger_path)
    expected_benchmark_id: str | None = None
    verified_benchmark_id: str | None = None
    actual_benchmark_id: str | None = None
    try:
        normalized = _normalize_observation(request)
        budgets = _read_failure_budgets(pathlib.Path(config_path))
        state = audit_ledger(path)
        expected_benchmark_id = state["benchmark_id"]
        verified_benchmark_id = expected_benchmark_id
        experiment_id = normalized["experiment_id"]
        registration = state["registrations"].get(experiment_id)
        if registration is None:
            raise LedgerValidationError("experiment_id is not registered")
        actual_benchmark_id = registration["benchmark_id"]
        if experiment_id in state["observations"]:
            raise LedgerValidationError("experiment outcome is already recorded")
        _, observed_time = _utc_timestamp(normalized["observed_at"], "observed_at")
        _, registered_time = _utc_timestamp(
            registration["registered_at"], "registered_at"
        )
        if observed_time <= registered_time:
            raise LedgerValidationError("outcome must follow experiment registration")
        _append_record(path, state, "OBSERVE", normalized)
        updated = audit_ledger(path)
        family_id = registration["hypothesis_family_id"]
        information_id = registration["information_set_id"]
        remaining = _remaining_budgets(
            updated, budgets, family_id, information_id
        )
        stopped = remaining["family"] == 0 or remaining["information_set"] == 0
        return _decision_report(
            operation="observe",
            decision=DECISION_STOP if stopped else DECISION_ALLOW,
            appended=True,
            remaining_budgets=remaining,
            reasons=["failure budget is exhausted"] if stopped else [],
            experiment_id=experiment_id,
            family_id=family_id,
            information_set_id=information_id,
            benchmark_id=verified_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
        )
    except (LedgerValidationError, OSError, TypeError, ValueError) as exc:
        return _decision_report(
            operation="observe",
            decision=DECISION_BLOCK,
            appended=False,
            reasons=[str(exc)],
            benchmark_id=verified_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
        )


def audit_next_experiment(
    ledger_path: pathlib.Path | str,
    config_path: pathlib.Path | str,
    proposal: object | None,
) -> dict[str, Any]:
    expected_benchmark_id: str | None = None
    verified_benchmark_id: str | None = None
    actual_benchmark_id = (
        proposal.get("benchmark_id")
        if isinstance(proposal, Mapping)
        and isinstance(proposal.get("benchmark_id"), str)
        else None
    )
    try:
        budgets = _read_failure_budgets(pathlib.Path(config_path))
        state = audit_ledger(ledger_path)
        expected_benchmark_id = state["benchmark_id"]
        verified_benchmark_id = expected_benchmark_id
        if proposal is None:
            proposal_map: Mapping[str, Any] = {}
        elif isinstance(proposal, Mapping):
            proposal_map = proposal
        else:
            raise LedgerValidationError("audit-next proposal must be an object")
        family_raw = proposal_map.get("hypothesis_family_id")
        information_raw = proposal_map.get("information_set_id")
        family_id = (
            _sha256(family_raw, "hypothesis_family_id")
            if family_raw is not None
            else None
        )
        information_id = (
            _sha256(information_raw, "information_set_id")
            if information_raw is not None
            else None
        )
        benchmark_id = _sha256(proposal_map.get("benchmark_id"), "benchmark_id")
        actual_benchmark_id = benchmark_id
        if expected_benchmark_id is None:
            expected_benchmark_id = benchmark_id
            verified_benchmark_id = benchmark_id
        elif benchmark_id != expected_benchmark_id:
            raise LedgerValidationError("benchmark_id drift is forbidden")
        remaining = _remaining_budgets(
            state, budgets, family_id, information_id
        )
        stopped = remaining["family"] == 0 or remaining["information_set"] == 0
        return _decision_report(
            operation="audit-next",
            decision=DECISION_STOP if stopped else DECISION_ALLOW,
            appended=False,
            remaining_budgets=remaining,
            reasons=["failure budget is exhausted"] if stopped else [],
            family_id=family_id,
            information_set_id=information_id,
            benchmark_id=verified_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
        )
    except (LedgerValidationError, OSError, TypeError, ValueError) as exc:
        return _decision_report(
            operation="audit-next",
            decision=DECISION_BLOCK,
            appended=False,
            reasons=[str(exc)],
            benchmark_id=verified_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
        )


# Short aliases keep the operation names usable by importers as well as the CLI.
register = register_experiment
observe = observe_experiment
audit_next = audit_next_experiment


def _json_argument(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if raw.startswith("@"):
        raw = pathlib.Path(raw[1:]).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise LedgerValidationError("request JSON must be an object")
    return payload


def _definition_argument(raw: str | None) -> object:
    if raw is None:
        return None
    if raw.startswith("@"):
        raw = pathlib.Path(raw[1:]).read_text(encoding="utf-8")
    return json.loads(raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--ledger", required=True)
        subparser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
        subparser.add_argument("--request-json")

    register_parser = subparsers.add_parser("register")
    common(register_parser)
    register_parser.add_argument("--experiment-id")
    register_parser.add_argument("--benchmark-id")
    register_parser.add_argument("--information-set-definition")
    register_parser.add_argument("--information-set-id")
    register_parser.add_argument("--hypothesis-family-definition")
    register_parser.add_argument("--hypothesis-family-id")
    register_parser.add_argument("--changed-dimension", action="append")
    register_parser.add_argument("--expected-direction")
    register_parser.add_argument("--stop-condition")
    register_parser.add_argument("--registered-at")
    register_parser.add_argument("--earliest-result-at")

    observe_parser = subparsers.add_parser("observe")
    common(observe_parser)
    observe_parser.add_argument("--experiment-id")
    observe_parser.add_argument("--outcome")
    observe_parser.add_argument("--observed-at")

    audit_parser = subparsers.add_parser("audit-next")
    common(audit_parser)
    audit_parser.add_argument("--benchmark-id")
    audit_parser.add_argument("--hypothesis-family-id")
    audit_parser.add_argument("--information-set-id")
    return parser.parse_args()


def _request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.request_json is not None:
        return _json_argument(args.request_json)
    if args.operation == "register":
        return {
            "experiment_id": args.experiment_id,
            "benchmark_id": args.benchmark_id,
            "information_set_definition": _definition_argument(
                args.information_set_definition
            ),
            "information_set_id": args.information_set_id,
            "hypothesis_family_definition": _definition_argument(
                args.hypothesis_family_definition
            ),
            "hypothesis_family_id": args.hypothesis_family_id,
            "changed_dimensions": args.changed_dimension,
            "expected_direction": args.expected_direction,
            "stop_condition": args.stop_condition,
            "registered_at": args.registered_at,
            "earliest_result_at": args.earliest_result_at,
        }
    if args.operation == "observe":
        return {
            "experiment_id": args.experiment_id,
            "outcome": args.outcome,
            "observed_at": args.observed_at,
        }
    return {
        key: value
        for key, value in {
            "benchmark_id": args.benchmark_id,
            "hypothesis_family_id": args.hypothesis_family_id,
            "information_set_id": args.information_set_id,
        }.items()
        if value is not None
    }


def main() -> int:
    args = parse_args()
    try:
        request = _request_from_args(args)
        if args.operation == "register":
            report = register_experiment(args.ledger, args.config, request)
        elif args.operation == "observe":
            report = observe_experiment(args.ledger, args.config, request)
        else:
            report = audit_next_experiment(args.ledger, args.config, request)
    except (LedgerValidationError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        report = _decision_report(
            operation=args.operation,
            decision=DECISION_BLOCK,
            appended=False,
            reasons=[str(exc)],
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 2 if report["decision"] == DECISION_BLOCK else 0


if __name__ == "__main__":
    raise SystemExit(main())
