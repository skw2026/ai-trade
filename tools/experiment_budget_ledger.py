#!/usr/bin/env python3
"""Maintain a fail-closed preregistration and experiment-budget ledger."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import stat
import sys
import tempfile
import time
from typing import Any, Iterator, Mapping

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from decision_evidence_common import validate_verified_benchmark_report


RECORD_SCHEMA_VERSION = "decision_experiment_ledger_v1"
CHECKPOINT_SCHEMA_VERSION = "decision_experiment_ledger_checkpoint_v1"
RECOVERY_SCHEMA_VERSION = "decision_experiment_ledger_recovery_v1"
DECISION_SCHEMA_VERSION = "experiment_budget_ledger_decision_v1"
RESULT_SCHEMA_VERSION = "decision_experiment_result_v1"
GENESIS_HASH = "0" * 64
OUTCOMES = frozenset({"SUPPORTED", "FALSIFIED", "INCONCLUSIVE"})
FAILURE_OUTCOMES = frozenset({"FALSIFIED", "INCONCLUSIVE"})
EXPECTED_DIRECTIONS = frozenset({"increase", "decrease"})
STOP_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "eq", "ne"})
DECISION_ALLOW = "ALLOW_NEXT_EXPERIMENT"
DECISION_STOP = "STOP_CURRENT_FAMILY"
DECISION_BLOCK = "BLOCK_INVALID_LEDGER"
DEFAULT_CONFIG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "config"
    / "decision_evidence_validation.json"
)
FROZEN_VALIDATION_CONFIG = {
    "schema_version": "decision_evidence_validation_v1",
    "alignment": {
        "min_candidates": 8,
        "min_independent_blocks": 5,
        "alpha": 0.05,
        "permutation_trials": 10000,
    },
    "uplift": {
        "min_independent_blocks": 8,
        "block_coverage": 1,
        "bootstrap_trials": 10000,
        "lcb": 0.95,
    },
    "failure_budgets": {"family": 3, "information_set": 8},
    "seed": {
        "source": "benchmark_id+channel",
        "cli_override_allowed": False,
    },
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_REGISTRATION_REQUEST_FIELDS = frozenset(
    {
        "experiment_id",
        "benchmark_id",
        "validation_policy_sha256",
        "information_set_definition",
        "information_set_id",
        "hypothesis_family_definition",
        "hypothesis_family_id",
        "display_name",
        "changed_dimensions",
        "expected_direction",
        "stop_condition",
        "result_source_path",
    }
)
_REGISTRATION_SERVER_FIELDS = frozenset({"registered_at", "registration_nonce"})
_REGISTRATION_FIELDS = _REGISTRATION_REQUEST_FIELDS | _REGISTRATION_SERVER_FIELDS
_OBSERVATION_REQUEST_FIELDS = frozenset({"experiment_id"})
_OBSERVATION_FIELDS = frozenset(
    {
        "experiment_id",
        "outcome",
        "observed_at",
        "result_identity",
        "result_source_path",
        "result_file_sha256",
        "registration_nonce",
    }
)
_COMMON_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "sequence",
        "previous_record_hash",
        "record_hash",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "registration_nonce",
        "outcome",
        "result",
        "result_identity",
    }
)


class LedgerValidationError(ValueError):
    """Raised when persisted state or a proposed operation is invalid."""


class MultipleChangedDimensions(LedgerValidationError):
    """Raised when a proposal changes more than one dimension."""


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


def stable_definition_id(definition: object) -> str:
    return canonical_sha256(definition)


def stable_family_id(
    information_set_id: str, hypothesis_family_definition: object
) -> str:
    return canonical_sha256(
        {
            "information_set_id": information_set_id,
            "hypothesis_family_definition": hypothesis_family_definition,
        }
    )


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


def _authoritative_timestamp(last_timestamp: object = None) -> tuple[str, dt.datetime]:
    current = dt.datetime.now(dt.timezone.utc)
    if isinstance(last_timestamp, dt.datetime) and current <= last_timestamp:
        current = last_timestamp + dt.timedelta(microseconds=1)
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z"), current


def _canonical_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise LedgerValidationError(f"{field} must be a non-empty object")
    if "display_name" in value:
        raise LedgerValidationError(f"{field} must not contain display_name")
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LedgerValidationError(f"{field} is not canonical JSON") from exc
    return json.loads(encoded.decode("ascii"))


def _changed_dimensions(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise LedgerValidationError("changed_dimensions must be an array")
    if len(value) > 1:
        raise MultipleChangedDimensions(
            "an experiment may change exactly one dimension"
        )
    if len(value) != 1 or not isinstance(value[0], dict):
        raise LedgerValidationError(
            "an experiment must change exactly one structured dimension"
        )
    dimension = value[0]
    if set(dimension) != {"name", "before", "after"}:
        raise LedgerValidationError(
            "changed dimension fields must be name, before, and after"
        )
    name = _non_empty_string(dimension.get("name"), "changed dimension name")
    try:
        before_bytes = canonical_json_bytes(dimension.get("before"))
        after_bytes = canonical_json_bytes(dimension.get("after"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LedgerValidationError("changed dimension values are invalid") from exc
    if before_bytes == after_bytes:
        raise LedgerValidationError("changed dimension before and after must differ")
    return [
        {
            "name": name,
            "before": json.loads(before_bytes.decode("ascii")),
            "after": json.loads(after_bytes.decode("ascii")),
        }
    ]


def _stop_condition(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"metric", "operator", "value"}:
        raise LedgerValidationError(
            "stop_condition fields must be metric, operator, and value"
        )
    metric = _non_empty_string(value.get("metric"), "stop_condition.metric")
    operator = _non_empty_string(value.get("operator"), "stop_condition.operator")
    if operator not in STOP_OPERATORS:
        raise LedgerValidationError("stop_condition.operator is invalid")
    threshold = value.get("value")
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not math.isfinite(float(threshold))
    ):
        raise LedgerValidationError("stop_condition.value must be finite")
    return {"metric": metric, "operator": operator, "value": threshold}


def _canonical_result_path(value: object) -> str:
    raw = _non_empty_string(value, "result_source_path")
    path = pathlib.Path(raw)
    if not path.is_absolute():
        raise LedgerValidationError("result_source_path must be absolute")
    canonical = path.resolve(strict=False)
    return str(canonical)


def _normalize_registration_request(request: object) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise LedgerValidationError("registration proposal must be an object")
    if set(request) != _REGISTRATION_REQUEST_FIELDS:
        missing = sorted(_REGISTRATION_REQUEST_FIELDS - set(request))
        extra = sorted(set(request) - _REGISTRATION_REQUEST_FIELDS)
        raise LedgerValidationError(
            f"registration proposal fields are invalid; missing={missing}, extra={extra}"
        )
    information_definition = _canonical_object(
        request.get("information_set_definition"), "information_set_definition"
    )
    information_id = _sha256(request.get("information_set_id"), "information_set_id")
    if information_id != stable_definition_id(information_definition):
        raise LedgerValidationError(
            "information_set_id does not match its canonical definition"
        )
    family_definition = _canonical_object(
        request.get("hypothesis_family_definition"),
        "hypothesis_family_definition",
    )
    family_id = _sha256(request.get("hypothesis_family_id"), "hypothesis_family_id")
    if family_id != stable_family_id(information_id, family_definition):
        raise LedgerValidationError(
            "hypothesis_family_id is not bound to information_set_id and definition"
        )
    expected_direction = _non_empty_string(
        request.get("expected_direction"), "expected_direction"
    )
    if expected_direction not in EXPECTED_DIRECTIONS:
        raise LedgerValidationError("expected_direction is invalid")
    return {
        "experiment_id": _experiment_id(request.get("experiment_id")),
        "benchmark_id": _sha256(request.get("benchmark_id"), "benchmark_id"),
        "validation_policy_sha256": _sha256(
            request.get("validation_policy_sha256"), "validation_policy_sha256"
        ),
        "information_set_definition": information_definition,
        "information_set_id": information_id,
        "hypothesis_family_definition": family_definition,
        "hypothesis_family_id": family_id,
        "display_name": _non_empty_string(request.get("display_name"), "display_name"),
        "changed_dimensions": _changed_dimensions(request.get("changed_dimensions")),
        "expected_direction": expected_direction,
        "stop_condition": _stop_condition(request.get("stop_condition")),
        "result_source_path": _canonical_result_path(request.get("result_source_path")),
    }


def _normalize_registration_record(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _REGISTRATION_FIELDS:
        raise LedgerValidationError("registration record fields are invalid")
    request = {key: payload[key] for key in _REGISTRATION_REQUEST_FIELDS}
    normalized = _normalize_registration_request(request)
    registered_at, _ = _utc_timestamp(payload.get("registered_at"), "registered_at")
    nonce = _sha256(payload.get("registration_nonce"), "registration_nonce")
    return {**normalized, "registered_at": registered_at, "registration_nonce": nonce}


def _normalize_observation_request(request: object) -> dict[str, Any]:
    if not isinstance(request, Mapping) or set(request) != _OBSERVATION_REQUEST_FIELDS:
        missing = sorted(_OBSERVATION_REQUEST_FIELDS - set(request or {})) if isinstance(request, Mapping) else sorted(_OBSERVATION_REQUEST_FIELDS)
        extra = sorted(set(request) - _OBSERVATION_REQUEST_FIELDS) if isinstance(request, Mapping) else []
        raise LedgerValidationError(
            f"observation proposal fields are invalid; missing={missing}, extra={extra}"
        )
    return {"experiment_id": _experiment_id(request.get("experiment_id"))}


def _normalize_observation_record(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _OBSERVATION_FIELDS:
        raise LedgerValidationError("observation record fields are invalid")
    outcome = _non_empty_string(payload.get("outcome"), "outcome")
    if outcome not in OUTCOMES:
        raise LedgerValidationError("outcome is invalid")
    observed_at, _ = _utc_timestamp(payload.get("observed_at"), "observed_at")
    return {
        "experiment_id": _experiment_id(payload.get("experiment_id")),
        "outcome": outcome,
        "observed_at": observed_at,
        "result_identity": _sha256(payload.get("result_identity"), "result_identity"),
        "result_source_path": _canonical_result_path(payload.get("result_source_path")),
        "result_file_sha256": _sha256(
            payload.get("result_file_sha256"), "result_file_sha256"
        ),
        "registration_nonce": _sha256(
            payload.get("registration_nonce"), "registration_nonce"
        ),
    }


def _load_validation_policy(
    config_path: pathlib.Path,
) -> tuple[dict[str, int], str, dict[str, Any]]:
    try:
        config_bytes = config_path.read_bytes()
        payload = json.loads(config_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerValidationError("validation policy config is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or canonical_json_bytes(payload) != canonical_json_bytes(FROZEN_VALIDATION_CONFIG)
    ):
        raise LedgerValidationError(
            "validation policy config differs from the frozen contract"
        )
    return (
        dict(FROZEN_VALIDATION_CONFIG["failure_budgets"]),
        hashlib.sha256(config_bytes).hexdigest(),
        payload,
    )


def validation_policy_sha256(config_path: pathlib.Path | str) -> str:
    return _load_validation_policy(pathlib.Path(config_path))[1]


def _load_json_object(source: object, label: str) -> dict[str, Any]:
    if isinstance(source, Mapping):
        try:
            return json.loads(canonical_json_bytes(source).decode("ascii"))
        except (TypeError, ValueError, UnicodeError) as exc:
            raise LedgerValidationError(f"{label} is invalid") from exc
    if isinstance(source, (str, pathlib.Path)):
        try:
            payload = json.loads(pathlib.Path(source).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LedgerValidationError(f"{label} is unreadable") from exc
        if not isinstance(payload, dict):
            raise LedgerValidationError(f"{label} must be an object")
        return payload
    raise LedgerValidationError(f"{label} is required")


def _verify_benchmark_binding(
    benchmark_report: object,
    validation_policy: Mapping[str, Any],
    policy_id: str,
) -> tuple[str, dict[str, Any]]:
    report = _load_json_object(benchmark_report, "benchmark report")
    verification = validate_verified_benchmark_report(
        report,
        validation_policy=dict(validation_policy),
        validation_config_sha256=policy_id,
    )
    if not isinstance(verification, Mapping) or not verification.get("verified"):
        errors = verification.get("errors") if isinstance(verification, Mapping) else None
        detail = "; ".join(str(item) for item in errors) if isinstance(errors, list) else "invalid verifier result"
        raise LedgerValidationError(f"benchmark report is not strictly verified: {detail}")
    benchmark_id = _sha256(verification.get("benchmark_id"), "verified benchmark_id")
    expected_policy_id = verification.get("expected_validation_config_sha256")
    actual_policy_id = verification.get("actual_validation_config_sha256")
    if expected_policy_id != policy_id or actual_policy_id != policy_id:
        raise LedgerValidationError("benchmark validation policy bytes do not match config")
    return benchmark_id, dict(verification)


def _checkpoint_path(ledger_path: pathlib.Path) -> pathlib.Path:
    return ledger_path.with_suffix(ledger_path.suffix + ".checkpoint.json")


def _recovery_path(ledger_path: pathlib.Path) -> pathlib.Path:
    return ledger_path.with_suffix(ledger_path.suffix + ".recovery.json")


def _lock_path(ledger_path: pathlib.Path) -> pathlib.Path:
    return ledger_path.with_suffix(ledger_path.suffix + ".lock")


def _fsync_directory(directory: pathlib.Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: pathlib.Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f"{path.name}.tmp.", delete=False
        ) as handle:
            temporary = pathlib.Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _remove_durable(path: pathlib.Path) -> None:
    if path.exists():
        path.unlink()
        _fsync_directory(path.parent)


@contextlib.contextmanager
def _exclusive_ledger_lock(ledger_path: pathlib.Path) -> Iterator[None]:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_path(ledger_path).open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_ledger_bytes(path: pathlib.Path) -> bytes:
    try:
        return path.read_bytes() if path.is_file() else b""
    except OSError as exc:
        raise LedgerValidationError("ledger is unreadable") from exc


def _record_timestamp(record: Mapping[str, Any]) -> tuple[str, dt.datetime]:
    field = "registered_at" if record.get("record_type") == "register" else "observed_at"
    return _utc_timestamp(record.get(field), field)


def _parse_ledger_bytes(ledger_bytes: bytes) -> dict[str, Any]:
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
    result_paths: set[str] = set()
    definitions: dict[tuple[str, str], object] = {}
    failure_counts_by_family: dict[str, int] = {}
    failure_counts_by_information_set: dict[str, int] = {}
    benchmark_id: str | None = None
    validation_policy_id: str | None = None
    previous_hash = GENESIS_HASH
    previous_time: dt.datetime | None = None

    for line_number, raw_line in enumerate(raw_lines, start=1):
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise LedgerValidationError(f"ledger JSON is invalid at line {line_number}") from exc
        if not isinstance(record, dict):
            raise LedgerValidationError(f"ledger record is not an object at line {line_number}")
        try:
            canonical = canonical_json_bytes(record).decode("ascii")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise LedgerValidationError(f"ledger record is not canonical at line {line_number}") from exc
        if raw_line != canonical:
            raise LedgerValidationError(f"ledger record is not canonical at line {line_number}")
        record_type = record.get("record_type")
        payload_fields = _REGISTRATION_FIELDS if record_type == "register" else _OBSERVATION_FIELDS
        if record_type not in {"register", "observe"} or set(record) != (_COMMON_RECORD_FIELDS | payload_fields):
            raise LedgerValidationError(f"ledger record fields are invalid at line {line_number}")
        if (
            record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(record.get("sequence"), int)
            or isinstance(record.get("sequence"), bool)
            or record.get("sequence") != line_number
            or record.get("previous_record_hash") != previous_hash
            or not _is_sha256(record.get("record_hash"))
            or record.get("record_hash") != record_hash(record)
        ):
            raise LedgerValidationError(f"ledger hash chain is invalid at line {line_number}")
        _, timestamp = _record_timestamp(record)
        if previous_time is not None and timestamp <= previous_time:
            raise LedgerValidationError(
                f"ledger UTC timestamps are not strictly increasing at line {line_number}"
            )
        payload = {key: record[key] for key in payload_fields}
        if record_type == "register":
            normalized = _normalize_registration_record(payload)
            experiment_id = normalized["experiment_id"]
            if experiment_id in registrations:
                raise LedgerValidationError("ledger contains a duplicate experiment_id")
            source_path = normalized["result_source_path"]
            if source_path in result_paths:
                raise LedgerValidationError("ledger reuses a result_source_path")
            result_paths.add(source_path)
            if benchmark_id is None:
                benchmark_id = normalized["benchmark_id"]
            elif normalized["benchmark_id"] != benchmark_id:
                raise LedgerValidationError("ledger contains benchmark drift")
            if validation_policy_id is None:
                validation_policy_id = normalized["validation_policy_sha256"]
            elif normalized["validation_policy_sha256"] != validation_policy_id:
                raise LedgerValidationError("ledger contains validation policy drift")
            for kind in ("information_set", "hypothesis_family"):
                identity = normalized[f"{kind}_id"]
                definition = normalized[f"{kind}_definition"]
                key = (kind, identity)
                if key in definitions and definitions[key] != definition:
                    raise LedgerValidationError(f"ledger contains {kind} identity drift")
                definitions[key] = definition
            registrations[experiment_id] = normalized
        else:
            normalized = _normalize_observation_record(payload)
            experiment_id = normalized["experiment_id"]
            registration = registrations.get(experiment_id)
            if registration is None:
                raise LedgerValidationError("ledger observation has no prior registration")
            if experiment_id in observations:
                raise LedgerValidationError("ledger contains more than one outcome for an experiment")
            _, registered_time = _utc_timestamp(registration["registered_at"], "registered_at")
            if timestamp <= registered_time:
                raise LedgerValidationError("experiment outcome precedes registration")
            if normalized["result_source_path"] != registration["result_source_path"]:
                raise LedgerValidationError("result source path mismatch")
            if normalized["registration_nonce"] != registration["registration_nonce"]:
                raise LedgerValidationError("registration nonce mismatch")
            observations[experiment_id] = normalized
            if normalized["outcome"] in FAILURE_OUTCOMES:
                family_id = registration["hypothesis_family_id"]
                information_id = registration["information_set_id"]
                failure_counts_by_family[family_id] = failure_counts_by_family.get(family_id, 0) + 1
                failure_counts_by_information_set[information_id] = failure_counts_by_information_set.get(information_id, 0) + 1
        records.append(record)
        previous_hash = str(record["record_hash"])
        previous_time = timestamp

    return {
        "records": records,
        "registrations": registrations,
        "observations": observations,
        "benchmark_id": benchmark_id,
        "validation_policy_sha256": validation_policy_id,
        "definitions": definitions,
        "failure_counts_by_family": failure_counts_by_family,
        "failure_counts_by_information_set": failure_counts_by_information_set,
        "tail_record_hash": previous_hash,
        "last_timestamp": previous_time,
        "ledger_bytes": ledger_bytes,
    }


def _checkpoint_payload(
    ledger_bytes: bytes, record_count: int, tail_record_hash: str
) -> dict[str, Any]:
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "record_count": record_count,
        "tail_record_hash": tail_record_hash,
        "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
    }


def _read_canonical_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        content = path.read_bytes()
        payload = json.loads(content.decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerValidationError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict) or content != canonical_json_bytes(payload) + b"\n":
        raise LedgerValidationError(f"{label} is not canonical")
    return payload


def _write_checkpoint(ledger_path: pathlib.Path) -> None:
    ledger_bytes = _read_ledger_bytes(ledger_path)
    state = _parse_ledger_bytes(ledger_bytes)
    checkpoint = _checkpoint_payload(
        ledger_bytes, len(state["records"]), state["tail_record_hash"]
    )
    _atomic_write(_checkpoint_path(ledger_path), canonical_json_bytes(checkpoint) + b"\n")


def _write_recovery_marker(
    ledger_path: pathlib.Path,
    before_checkpoint: Mapping[str, Any],
    after_checkpoint: Mapping[str, Any],
    pending_record: Mapping[str, Any],
) -> None:
    marker = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "before_checkpoint": dict(before_checkpoint),
        "after_checkpoint": dict(after_checkpoint),
        "pending_record": dict(pending_record),
    }
    _atomic_write(_recovery_path(ledger_path), canonical_json_bytes(marker) + b"\n")


def _validate_checkpoint_shape(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "record_count", "tail_record_hash", "ledger_sha256"
    }:
        raise LedgerValidationError(f"{label} shape is invalid")
    if (
        value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or not isinstance(value.get("record_count"), int)
        or isinstance(value.get("record_count"), bool)
        or value["record_count"] < 0
        or not _is_sha256(value.get("tail_record_hash"))
        or not _is_sha256(value.get("ledger_sha256"))
    ):
        raise LedgerValidationError(f"{label} is invalid")
    return dict(value)


def _validate_recovery_marker(marker: object) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(marker, dict) or set(marker) != {
        "schema_version", "before_checkpoint", "after_checkpoint", "pending_record"
    } or marker.get("schema_version") != RECOVERY_SCHEMA_VERSION:
        raise LedgerValidationError("ledger recovery marker shape is invalid")
    before = _validate_checkpoint_shape(marker["before_checkpoint"], "before checkpoint")
    after = _validate_checkpoint_shape(marker["after_checkpoint"], "after checkpoint")
    pending = marker.get("pending_record")
    if not isinstance(pending, dict):
        raise LedgerValidationError("recovery pending record is invalid")
    if (
        pending.get("sequence") != before["record_count"] + 1
        or pending.get("previous_record_hash") != before["tail_record_hash"]
        or pending.get("record_hash") != record_hash(pending)
        or after["record_count"] != pending.get("sequence")
        or after["tail_record_hash"] != pending.get("record_hash")
    ):
        raise LedgerValidationError("recovery pending record does not bind checkpoints")
    return before, after, dict(pending)


def _validate_or_recover_checkpoint(
    ledger_path: pathlib.Path, state: Mapping[str, Any]
) -> bool:
    checkpoint_path = _checkpoint_path(ledger_path)
    recovery_path = _recovery_path(ledger_path)
    actual = _checkpoint_payload(
        state["ledger_bytes"], len(state["records"]), state["tail_record_hash"]
    )

    if recovery_path.is_file():
        marker = _read_canonical_object(recovery_path, "ledger recovery marker")
        before, after, _ = _validate_recovery_marker(marker)
        persisted_checkpoint = (
            _read_canonical_object(checkpoint_path, "ledger checkpoint")
            if checkpoint_path.is_file()
            else None
        )
        if actual == before:
            if persisted_checkpoint is not None and persisted_checkpoint != before:
                raise LedgerValidationError("checkpoint is inconsistent with pre-append recovery state")
            if before["record_count"] == 0 and persisted_checkpoint is not None:
                raise LedgerValidationError("empty ledger has unexpected checkpoint")
            _remove_durable(recovery_path)
            return True
        if actual == after:
            if persisted_checkpoint not in (None, before, after):
                raise LedgerValidationError("checkpoint is inconsistent with committed recovery state")
            _atomic_write(checkpoint_path, canonical_json_bytes(after) + b"\n")
            _remove_durable(recovery_path)
            return True
        raise LedgerValidationError("ledger is neither recovery before nor after state")

    if not state["records"] and not state["ledger_bytes"]:
        if checkpoint_path.exists():
            raise LedgerValidationError("empty ledger has unexpected durable metadata")
        return False
    if not checkpoint_path.is_file():
        raise LedgerValidationError("ledger checkpoint is missing")
    checkpoint = _read_canonical_object(checkpoint_path, "ledger checkpoint")
    if checkpoint != actual:
        raise LedgerValidationError("ledger checkpoint does not match ledger")
    return False


def _load_state_unlocked(ledger_path: pathlib.Path) -> dict[str, Any]:
    state = _parse_ledger_bytes(_read_ledger_bytes(ledger_path))
    state["checkpoint_recovered"] = _validate_or_recover_checkpoint(ledger_path, state)
    return state


def audit_ledger(ledger_path: pathlib.Path | str) -> dict[str, Any]:
    path = pathlib.Path(ledger_path)
    with _exclusive_ledger_lock(path):
        return _load_state_unlocked(path)


def _durable_append(ledger_path: pathlib.Path, content: bytes) -> None:
    descriptor = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(content)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("ledger append made no progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_record_unlocked(
    ledger_path: pathlib.Path,
    state: Mapping[str, Any],
    record_type: str,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    timestamp_field = "registered_at" if record_type == "register" else "observed_at"
    _, timestamp = _utc_timestamp(payload.get(timestamp_field), timestamp_field)
    last_timestamp = state.get("last_timestamp")
    if isinstance(last_timestamp, dt.datetime) and timestamp <= last_timestamp:
        raise LedgerValidationError("new ledger timestamp must be strictly later than history")
    record = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "record_type": record_type,
        "sequence": len(state["records"]) + 1,
        "previous_record_hash": state["tail_record_hash"],
        **payload,
    }
    record["record_hash"] = record_hash(record)
    record_line = canonical_json_bytes(record) + b"\n"
    before_bytes = state["ledger_bytes"]
    after_bytes = before_bytes + record_line
    before_checkpoint = _checkpoint_payload(
        before_bytes, len(state["records"]), state["tail_record_hash"]
    )
    after_checkpoint = _checkpoint_payload(
        after_bytes, record["sequence"], record["record_hash"]
    )
    _write_recovery_marker(
        ledger_path, before_checkpoint, after_checkpoint, record
    )
    try:
        _durable_append(ledger_path, record_line)
    except OSError as exc:
        current = _read_ledger_bytes(ledger_path)
        if current == before_bytes:
            _remove_durable(_recovery_path(ledger_path))
            raise
        if current != after_bytes:
            raise LedgerValidationError(
                "ledger append failed outside a complete before/after state"
            ) from exc

    checkpoint_recovery_required = False
    try:
        _write_checkpoint(ledger_path)
        _remove_durable(_recovery_path(ledger_path))
    except OSError:
        checkpoint_recovery_required = True
    return record, checkpoint_recovery_required


def _validate_policy_binding(state: Mapping[str, Any], policy_id: str) -> None:
    recorded = state.get("validation_policy_sha256")
    if recorded is not None and recorded != policy_id:
        raise LedgerValidationError("ledger validation policy differs from frozen config")


def _validate_benchmark_state(state: Mapping[str, Any], benchmark_id: str) -> None:
    recorded = state.get("benchmark_id")
    if recorded is not None and recorded != benchmark_id:
        raise LedgerValidationError("ledger benchmark differs from verified benchmark")


def _remaining_budgets(
    state: Mapping[str, Any],
    budgets: Mapping[str, int],
    family_id: str,
    information_set_id: str,
) -> dict[str, int]:
    family_used = int(state["failure_counts_by_family"].get(family_id, 0))
    information_used = int(
        state["failure_counts_by_information_set"].get(information_set_id, 0)
    )
    return {
        "family": max(0, int(budgets["family"]) - family_used),
        "information_set": max(0, int(budgets["information_set"]) - information_used),
    }


def _decision_report(
    *,
    operation: str,
    decision: str,
    appended: bool,
    remaining_budgets: Mapping[str, int] | None = None,
    reasons: list[str] | None = None,
    mismatches: list[str] | None = None,
    experiment_id: str | None = None,
    family_id: str | None = None,
    information_set_id: str | None = None,
    benchmark_id: str | None = None,
    expected_benchmark_id: str | None = None,
    actual_benchmark_id: str | None = None,
    validation_policy_sha256: str | None = None,
    benchmark_verified: bool = False,
    registration_verified: bool = False,
    registration_nonce: str | None = None,
    actual_proposal_sha256: str | None = None,
    registered_proposal_sha256: str | None = None,
    registration_record_hash: str | None = None,
    result_source_path: str | None = None,
    ledger_record_count: int | None = None,
    ledger_tail_record_hash: str | None = None,
    checkpoint_recovery_required: bool = False,
    checkpoint_recovered: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "operation": operation,
        "decision": decision,
        "appended": bool(appended),
        "experiment_id": experiment_id,
        "registration_verified": bool(registration_verified),
        "benchmark_verified": bool(benchmark_verified),
        "hypothesis_family_id": family_id,
        "information_set_id": information_set_id,
        "benchmark_id": benchmark_id,
        "expected_benchmark_id": expected_benchmark_id,
        "actual_benchmark_id": actual_benchmark_id,
        "validation_policy_sha256": validation_policy_sha256,
        "remaining_budgets": dict(remaining_budgets) if remaining_budgets is not None else None,
        "registration_nonce": registration_nonce,
        "actual_proposal_sha256": actual_proposal_sha256,
        "registered_proposal_sha256": registered_proposal_sha256,
        "registration_record_hash": registration_record_hash,
        "result_source_path": result_source_path,
        "ledger_record_count": ledger_record_count,
        "ledger_tail_record_hash": ledger_tail_record_hash,
        "checkpoint_recovery_required": bool(checkpoint_recovery_required),
        "checkpoint_recovered": bool(checkpoint_recovered),
        "mismatches": sorted(set(mismatches or [])),
        "reasons": list(reasons or []),
    }


def _raw_report_identity(request: object) -> tuple[str | None, str | None]:
    if not isinstance(request, Mapping):
        return None, None
    experiment = request.get("experiment_id")
    benchmark = request.get("benchmark_id")
    return (
        experiment if isinstance(experiment, str) else None,
        benchmark if isinstance(benchmark, str) else None,
    )


def _path_does_not_exist(path_text: str) -> None:
    try:
        os.lstat(path_text)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LedgerValidationError("result_source_path cannot be inspected") from exc
    raise LedgerValidationError("result_source_path must not exist at registration")


def _registration_request_from_record(registration: Mapping[str, Any]) -> dict[str, Any]:
    return {key: registration[key] for key in _REGISTRATION_REQUEST_FIELDS}


def _read_result_artifact(registration: Mapping[str, Any]) -> dict[str, Any]:
    path = pathlib.Path(registration["result_source_path"])
    try:
        lstat_result = path.lstat()
    except OSError as exc:
        raise LedgerValidationError("registered result artifact is unavailable") from exc
    if stat.S_ISLNK(lstat_result.st_mode) or not stat.S_ISREG(lstat_result.st_mode):
        raise LedgerValidationError("registered result artifact must be a regular non-symlink file")
    if lstat_result.st_mode & 0o222:
        raise LedgerValidationError("registered result artifact must be read-only")
    _, registered_time = _utc_timestamp(registration["registered_at"], "registered_at")
    registered_ns = int(registered_time.timestamp() * 1_000_000_000)
    now_ns = time.time_ns()
    if lstat_result.st_mtime_ns <= registered_ns or lstat_result.st_mtime_ns > now_ns:
        raise LedgerValidationError("result artifact mtime must be after registration")
    birthtime = getattr(lstat_result, "st_birthtime", None)
    if birthtime is not None:
        birth_ns = int(float(birthtime) * 1_000_000_000)
        if birth_ns <= registered_ns or birth_ns > now_ns:
            raise LedgerValidationError("result artifact creation time must be after registration")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise LedgerValidationError("registered result artifact cannot be read safely") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_before != identity_after or before.st_ino != lstat_result.st_ino:
        raise LedgerValidationError("registered result artifact changed while reading")
    content = b"".join(chunks)
    try:
        payload = json.loads(content.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerValidationError("registered result artifact is invalid JSON") from exc
    if not isinstance(payload, dict) or content != canonical_json_bytes(payload) + b"\n":
        raise LedgerValidationError("registered result artifact is not canonical")
    if set(payload) != _RESULT_FIELDS or payload.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise LedgerValidationError("registered result artifact schema is invalid")
    if _experiment_id(payload.get("experiment_id")) != registration["experiment_id"]:
        raise LedgerValidationError("result artifact experiment_id mismatch")
    if _sha256(payload.get("registration_nonce"), "registration_nonce") != registration["registration_nonce"]:
        raise LedgerValidationError("result artifact registration nonce mismatch")
    outcome = _non_empty_string(payload.get("outcome"), "outcome")
    if outcome not in OUTCOMES:
        raise LedgerValidationError("result artifact outcome is invalid")
    raw_result = payload.get("result")
    if not isinstance(raw_result, dict) or not raw_result:
        raise LedgerValidationError("result must be a non-empty object")
    try:
        result = json.loads(canonical_json_bytes(raw_result).decode("ascii"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LedgerValidationError("result is not canonical JSON") from exc
    identity_payload = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "experiment_id": registration["experiment_id"],
        "registration_nonce": registration["registration_nonce"],
        "outcome": outcome,
        "result": result,
    }
    expected_identity = canonical_sha256(identity_payload)
    actual_identity = _sha256(payload.get("result_identity"), "result_identity")
    if actual_identity != expected_identity:
        raise LedgerValidationError("result artifact identity mismatch")
    return {
        "outcome": outcome,
        "result_identity": actual_identity,
        "result_source_path": str(path),
        "result_file_sha256": hashlib.sha256(content).hexdigest(),
        "registration_nonce": registration["registration_nonce"],
    }


def register_experiment(
    ledger_path: pathlib.Path | str,
    config_path: pathlib.Path | str,
    request: object,
    benchmark_report: object = None,
) -> dict[str, Any]:
    path = pathlib.Path(ledger_path)
    experiment_id, actual_benchmark_id = _raw_report_identity(request)
    expected_benchmark_id: str | None = None
    verified_benchmark_id: str | None = None
    policy_id: str | None = None
    checkpoint_recovered = False
    try:
        with _exclusive_ledger_lock(path):
            state = _load_state_unlocked(path)
            checkpoint_recovered = bool(state["checkpoint_recovered"])
            budgets, policy_id, validation_policy = _load_validation_policy(pathlib.Path(config_path))
            verified_benchmark_id, _ = _verify_benchmark_binding(
                benchmark_report, validation_policy, policy_id
            )
            expected_benchmark_id = state["benchmark_id"] or verified_benchmark_id
            _validate_policy_binding(state, policy_id)
            _validate_benchmark_state(state, verified_benchmark_id)
            normalized = _normalize_registration_request(request)
            experiment_id = normalized["experiment_id"]
            actual_benchmark_id = normalized["benchmark_id"]
            family_id = normalized["hypothesis_family_id"]
            information_id = normalized["information_set_id"]
            if normalized["validation_policy_sha256"] != policy_id:
                raise LedgerValidationError("registration validation policy does not match frozen config")
            if actual_benchmark_id != verified_benchmark_id:
                raise LedgerValidationError("registration benchmark_id does not match verified benchmark")
            if experiment_id in state["registrations"]:
                raise LedgerValidationError("experiment_id has already been registered")
            if normalized["result_source_path"] in {
                item["result_source_path"] for item in state["registrations"].values()
            }:
                raise LedgerValidationError("result_source_path has already been registered")
            _path_does_not_exist(normalized["result_source_path"])
            remaining = _remaining_budgets(state, budgets, family_id, information_id)
            if remaining["family"] == 0 or remaining["information_set"] == 0:
                return _decision_report(
                    operation="register", decision=DECISION_STOP, appended=False,
                    remaining_budgets=remaining, reasons=["failure budget is exhausted"],
                    experiment_id=experiment_id, family_id=family_id,
                    information_set_id=information_id, benchmark_id=verified_benchmark_id,
                    expected_benchmark_id=expected_benchmark_id,
                    actual_benchmark_id=actual_benchmark_id,
                    validation_policy_sha256=policy_id, benchmark_verified=True,
                    actual_proposal_sha256=canonical_sha256(normalized),
                    ledger_record_count=len(state["records"]),
                    ledger_tail_record_hash=state["tail_record_hash"],
                    checkpoint_recovered=checkpoint_recovered,
                )
            registered_at, _ = _authoritative_timestamp(state["last_timestamp"])
            nonce = secrets.token_hex(32)
            while any(item["registration_nonce"] == nonce for item in state["registrations"].values()):
                nonce = secrets.token_hex(32)
            payload = {**normalized, "registered_at": registered_at, "registration_nonce": nonce}
            record, recovery_required = _append_record_unlocked(path, state, "register", payload)
            reasons = ["checkpoint write failed; recovery from ledger is required"] if recovery_required else []
            return _decision_report(
                operation="register", decision=DECISION_ALLOW, appended=True,
                remaining_budgets=remaining, reasons=reasons,
                experiment_id=experiment_id, family_id=family_id,
                information_set_id=information_id, benchmark_id=verified_benchmark_id,
                expected_benchmark_id=expected_benchmark_id,
                actual_benchmark_id=actual_benchmark_id,
                validation_policy_sha256=policy_id, benchmark_verified=True,
                registration_verified=True, registration_nonce=nonce,
                actual_proposal_sha256=canonical_sha256(normalized),
                registered_proposal_sha256=canonical_sha256(normalized),
                registration_record_hash=record["record_hash"],
                result_source_path=normalized["result_source_path"],
                ledger_record_count=record["sequence"],
                ledger_tail_record_hash=record["record_hash"],
                checkpoint_recovery_required=recovery_required,
                checkpoint_recovered=checkpoint_recovered,
            )
    except MultipleChangedDimensions as exc:
        return _decision_report(
            operation="register", decision=DECISION_STOP, appended=False,
            reasons=[str(exc)], experiment_id=experiment_id,
            benchmark_id=verified_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
            validation_policy_sha256=policy_id,
            benchmark_verified=verified_benchmark_id is not None,
            checkpoint_recovered=checkpoint_recovered,
        )
    except (LedgerValidationError, OSError, TypeError, ValueError) as exc:
        return _decision_report(
            operation="register", decision=DECISION_BLOCK, appended=False,
            reasons=[str(exc)], mismatches=[str(exc)], experiment_id=experiment_id,
            benchmark_id=verified_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
            validation_policy_sha256=policy_id,
            benchmark_verified=verified_benchmark_id is not None,
            checkpoint_recovered=checkpoint_recovered,
        )


def observe_experiment(
    ledger_path: pathlib.Path | str,
    config_path: pathlib.Path | str,
    request: object,
    benchmark_report: object = None,
) -> dict[str, Any]:
    path = pathlib.Path(ledger_path)
    experiment_id, _ = _raw_report_identity(request)
    expected_benchmark_id: str | None = None
    policy_id: str | None = None
    benchmark_verified = False
    checkpoint_recovered = False
    try:
        with _exclusive_ledger_lock(path):
            state = _load_state_unlocked(path)
            checkpoint_recovered = bool(state["checkpoint_recovered"])
            budgets, policy_id, validation_policy = _load_validation_policy(pathlib.Path(config_path))
            verified_benchmark_id, _ = _verify_benchmark_binding(
                benchmark_report, validation_policy, policy_id
            )
            benchmark_verified = True
            expected_benchmark_id = verified_benchmark_id
            _validate_policy_binding(state, policy_id)
            _validate_benchmark_state(state, verified_benchmark_id)
            normalized_request = _normalize_observation_request(request)
            experiment_id = normalized_request["experiment_id"]
            registration = state["registrations"].get(experiment_id)
            if registration is None:
                raise LedgerValidationError("experiment_id is not registered")
            if experiment_id in state["observations"]:
                raise LedgerValidationError("experiment outcome is already recorded")
            artifact = _read_result_artifact(registration)
            observed_at, _ = _authoritative_timestamp(state["last_timestamp"])
            payload = {"experiment_id": experiment_id, "observed_at": observed_at, **artifact}
            record, recovery_required = _append_record_unlocked(path, state, "observe", payload)
            updated = _parse_ledger_bytes(_read_ledger_bytes(path))
            family_id = registration["hypothesis_family_id"]
            information_id = registration["information_set_id"]
            remaining = _remaining_budgets(updated, budgets, family_id, information_id)
            stopped = remaining["family"] == 0 or remaining["information_set"] == 0
            reasons = ["failure budget is exhausted"] if stopped else []
            if recovery_required:
                reasons.append("checkpoint write failed; recovery from ledger is required")
            registered_request = _registration_request_from_record(registration)
            return _decision_report(
                operation="observe", decision=DECISION_STOP if stopped else DECISION_ALLOW,
                appended=True, remaining_budgets=remaining, reasons=reasons,
                experiment_id=experiment_id, family_id=family_id,
                information_set_id=information_id, benchmark_id=verified_benchmark_id,
                expected_benchmark_id=verified_benchmark_id,
                actual_benchmark_id=registration["benchmark_id"],
                validation_policy_sha256=policy_id, benchmark_verified=True,
                registration_verified=True,
                registration_nonce=registration["registration_nonce"],
                registered_proposal_sha256=canonical_sha256(registered_request),
                registration_record_hash=next(
                    item["record_hash"] for item in state["records"]
                    if item["record_type"] == "register" and item["experiment_id"] == experiment_id
                ),
                result_source_path=registration["result_source_path"],
                ledger_record_count=record["sequence"], ledger_tail_record_hash=record["record_hash"],
                checkpoint_recovery_required=recovery_required,
                checkpoint_recovered=checkpoint_recovered,
            )
    except (LedgerValidationError, OSError, TypeError, ValueError) as exc:
        return _decision_report(
            operation="observe", decision=DECISION_BLOCK, appended=False,
            reasons=[str(exc)], mismatches=[str(exc)], experiment_id=experiment_id,
            benchmark_id=expected_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            validation_policy_sha256=policy_id,
            benchmark_verified=benchmark_verified,
            checkpoint_recovered=checkpoint_recovered,
        )


def audit_next_experiment(
    ledger_path: pathlib.Path | str,
    config_path: pathlib.Path | str,
    proposal: object | None,
    benchmark_report: object = None,
) -> dict[str, Any]:
    path = pathlib.Path(ledger_path)
    experiment_id, actual_benchmark_id = _raw_report_identity(proposal)
    expected_benchmark_id: str | None = None
    policy_id: str | None = None
    benchmark_verified = False
    checkpoint_recovered = False
    try:
        with _exclusive_ledger_lock(path):
            state = _load_state_unlocked(path)
            checkpoint_recovered = bool(state["checkpoint_recovered"])
            budgets, policy_id, validation_policy = _load_validation_policy(pathlib.Path(config_path))
            verified_benchmark_id, _ = _verify_benchmark_binding(
                benchmark_report, validation_policy, policy_id
            )
            benchmark_verified = True
            expected_benchmark_id = verified_benchmark_id
            _validate_policy_binding(state, policy_id)
            _validate_benchmark_state(state, verified_benchmark_id)
            normalized = _normalize_registration_request(proposal)
            experiment_id = normalized["experiment_id"]
            actual_benchmark_id = normalized["benchmark_id"]
            if normalized["validation_policy_sha256"] != policy_id:
                raise LedgerValidationError("proposal validation policy does not match frozen config")
            if actual_benchmark_id != verified_benchmark_id:
                raise LedgerValidationError("proposal benchmark_id does not match verified benchmark")
            registration = state["registrations"].get(experiment_id)
            if registration is None:
                raise LedgerValidationError("audit-next experiment_id has no registration")
            registered_request = _registration_request_from_record(registration)
            actual_proposal_sha = canonical_sha256(normalized)
            registered_proposal_sha = canonical_sha256(registered_request)
            if actual_proposal_sha != registered_proposal_sha:
                raise LedgerValidationError("audit-next proposal does not match preregistration")
            if experiment_id in state["observations"]:
                raise LedgerValidationError("audit-next experiment already has an observation")
            family_id = registration["hypothesis_family_id"]
            information_id = registration["information_set_id"]
            remaining = _remaining_budgets(state, budgets, family_id, information_id)
            stopped = remaining["family"] == 0 or remaining["information_set"] == 0
            registration_record = next(
                item for item in state["records"]
                if item["record_type"] == "register" and item["experiment_id"] == experiment_id
            )
            return _decision_report(
                operation="audit-next", decision=DECISION_STOP if stopped else DECISION_ALLOW,
                appended=False, remaining_budgets=remaining,
                reasons=["failure budget is exhausted"] if stopped else [],
                experiment_id=experiment_id, family_id=family_id,
                information_set_id=information_id, benchmark_id=verified_benchmark_id,
                expected_benchmark_id=verified_benchmark_id,
                actual_benchmark_id=actual_benchmark_id,
                validation_policy_sha256=policy_id, benchmark_verified=True,
                registration_verified=True,
                registration_nonce=registration["registration_nonce"],
                actual_proposal_sha256=actual_proposal_sha,
                registered_proposal_sha256=registered_proposal_sha,
                registration_record_hash=registration_record["record_hash"],
                result_source_path=registration["result_source_path"],
                ledger_record_count=len(state["records"]),
                ledger_tail_record_hash=state["tail_record_hash"],
                checkpoint_recovered=checkpoint_recovered,
            )
    except MultipleChangedDimensions as exc:
        return _decision_report(
            operation="audit-next", decision=DECISION_STOP, appended=False,
            reasons=[str(exc)], experiment_id=experiment_id,
            benchmark_id=expected_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
            validation_policy_sha256=policy_id,
            benchmark_verified=benchmark_verified,
            checkpoint_recovered=checkpoint_recovered,
        )
    except (LedgerValidationError, OSError, TypeError, ValueError) as exc:
        return _decision_report(
            operation="audit-next", decision=DECISION_BLOCK, appended=False,
            reasons=[str(exc)], mismatches=[str(exc)], experiment_id=experiment_id,
            benchmark_id=expected_benchmark_id,
            expected_benchmark_id=expected_benchmark_id,
            actual_benchmark_id=actual_benchmark_id,
            validation_policy_sha256=policy_id,
            benchmark_verified=benchmark_verified,
            checkpoint_recovered=checkpoint_recovered,
        )


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
        raise LedgerValidationError("proposal JSON must be an object")
    return payload


def _proposal_file(path_text: str | None) -> dict[str, Any]:
    if path_text is None:
        return {}
    return _json_argument("@" + path_text)


def _structured_argument(raw: str | None) -> object:
    if raw is None:
        return None
    if raw.startswith("@"):
        raw = pathlib.Path(raw[1:]).read_text(encoding="utf-8")
    return json.loads(raw)


def _add_registration_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--experiment-id")
    parser.add_argument("--benchmark-id")
    parser.add_argument("--validation-policy-sha256")
    parser.add_argument("--information-set-definition")
    parser.add_argument("--information-set-id")
    parser.add_argument("--hypothesis-family-definition")
    parser.add_argument("--hypothesis-family-id")
    parser.add_argument("--display-name")
    parser.add_argument("--changed-dimension", action="append")
    parser.add_argument("--expected-direction")
    parser.add_argument("--stop-condition")
    parser.add_argument("--result-source-path")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--ledger", required=True)
        subparser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
        subparser.add_argument("--benchmark-report", required=True)
        proposal_group = subparser.add_mutually_exclusive_group()
        proposal_group.add_argument("--proposal")
        proposal_group.add_argument("--request-json")

    register_parser = subparsers.add_parser("register")
    common(register_parser)
    _add_registration_arguments(register_parser)

    observe_parser = subparsers.add_parser("observe")
    common(observe_parser)
    observe_parser.add_argument("--experiment-id")

    audit_parser = subparsers.add_parser("audit-next")
    common(audit_parser)
    _add_registration_arguments(audit_parser)
    return parser.parse_args()


def _registration_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "experiment_id": args.experiment_id,
        "benchmark_id": args.benchmark_id,
        "validation_policy_sha256": args.validation_policy_sha256,
        "information_set_definition": _structured_argument(args.information_set_definition),
        "information_set_id": args.information_set_id,
        "hypothesis_family_definition": _structured_argument(args.hypothesis_family_definition),
        "hypothesis_family_id": args.hypothesis_family_id,
        "display_name": args.display_name,
        "changed_dimensions": [_structured_argument(item) for item in (args.changed_dimension or [])],
        "expected_direction": args.expected_direction,
        "stop_condition": _structured_argument(args.stop_condition),
        "result_source_path": args.result_source_path,
    }


def _request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.proposal is not None:
        return _proposal_file(args.proposal)
    if args.request_json is not None:
        return _json_argument(args.request_json)
    if args.operation in {"register", "audit-next"}:
        return _registration_from_args(args)
    return {"experiment_id": args.experiment_id}


def main() -> int:
    args = parse_args()
    try:
        request = _request_from_args(args)
        if args.operation == "register":
            report = register_experiment(args.ledger, args.config, request, args.benchmark_report)
        elif args.operation == "observe":
            report = observe_experiment(args.ledger, args.config, request, args.benchmark_report)
        else:
            report = audit_next_experiment(args.ledger, args.config, request, args.benchmark_report)
    except (LedgerValidationError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        report = _decision_report(
            operation=args.operation, decision=DECISION_BLOCK,
            appended=False, reasons=[str(exc)], mismatches=[str(exc)]
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 2 if report["decision"] == DECISION_BLOCK else 0


if __name__ == "__main__":
    raise SystemExit(main())
