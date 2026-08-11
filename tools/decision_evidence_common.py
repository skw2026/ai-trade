#!/usr/bin/env python3
"""Shared deterministic identity helpers for decision-evidence validation."""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any


REQUIRED_COMPONENTS = (
    "data",
    "split",
    "cost",
    "features",
    "actions",
    "baseline_policy",
    "run_config",
    "implementation",
)

BENCHMARK_SCHEMA_VERSION = "decision_evidence_benchmark_v1"
REPORT_SCHEMA_VERSION = "decision_evidence_benchmark_validation_v1"


def canonical_json_bytes(value: object) -> bytes:
    """Return the repository's canonical, ASCII-only JSON representation."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_file_sha256(path: pathlib.Path) -> str | None:
    try:
        return file_sha256(path) if path.is_file() else None
    except OSError:
        return None


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_timestamp_ms(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _resolve_path(root: pathlib.Path, declared_path: Any) -> pathlib.Path | None:
    if not _is_non_empty_string(declared_path):
        return None
    path = pathlib.Path(declared_path)
    return path if path.is_absolute() else root / path


def _drift(
    component: str,
    logical_name: str,
    field: str,
    expected: Any,
    actual: Any,
) -> dict[str, Any]:
    return {
        "component": component,
        "logical_name": logical_name,
        "field": field,
        "expected": expected,
        "actual": actual,
    }


def _drift_sort_key(item: dict[str, Any]) -> tuple[str, str, str, bytes, bytes]:
    return (
        item["component"],
        item["logical_name"],
        item["field"],
        canonical_json_bytes(item["expected"]),
        canonical_json_bytes(item["actual"]),
    )


def _validate_components(
    manifest: dict[str, Any], root: pathlib.Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    drifts: list[dict[str, Any]] = []
    identity: dict[str, Any] = {}
    components = manifest.get("components")
    if not isinstance(components, dict):
        components = {}
        drifts.append(
            _drift("components", "", "shape", "object", manifest.get("components"))
        )

    unexpected = sorted(set(components) - set(REQUIRED_COMPONENTS))
    for component in unexpected:
        drifts.append(_drift(component, "", "component", None, "unexpected"))

    for component in REQUIRED_COMPONENTS:
        payload = components.get(component)
        if not isinstance(payload, dict):
            drifts.append(_drift(component, "", "component", "present", None))
            continue

        logical_id = payload.get("logical_id")
        if not _is_non_empty_string(logical_id):
            drifts.append(
                _drift(component, "", "logical_id", "non-empty string", logical_id)
            )

        files = payload.get("files")
        if not isinstance(files, list) or not files:
            drifts.append(_drift(component, "", "files", "non-empty array", files))
            files = []

        identity_files: list[dict[str, str]] = []
        seen_logical_names: set[str] = set()
        for index, entry in enumerate(files):
            if not isinstance(entry, dict):
                drifts.append(_drift(component, f"#{index}", "shape", "object", entry))
                continue
            logical_name_value = entry.get("logical_name")
            logical_name = (
                logical_name_value
                if isinstance(logical_name_value, str)
                else f"#{index}"
            )
            if not _is_non_empty_string(logical_name_value):
                drifts.append(
                    _drift(
                        component,
                        logical_name,
                        "logical_name",
                        "non-empty string",
                        logical_name_value,
                    )
                )
            elif logical_name_value in seen_logical_names:
                drifts.append(
                    _drift(
                        component,
                        logical_name,
                        "logical_name",
                        "unique",
                        logical_name_value,
                    )
                )
            else:
                seen_logical_names.add(logical_name_value)

            expected_sha = entry.get("sha256")
            expected_sha_valid = _is_sha256(expected_sha)
            if not expected_sha_valid:
                drifts.append(
                    _drift(
                        component,
                        logical_name,
                        "sha256",
                        "64 lowercase hex",
                        expected_sha,
                    )
                )

            declared_path = entry.get("path")
            path = _resolve_path(root, declared_path)
            if path is None:
                drifts.append(
                    _drift(
                        component,
                        logical_name,
                        "path",
                        "non-empty path",
                        declared_path,
                    )
                )
                continue

            actual_sha = _existing_file_sha256(path)
            if expected_sha_valid and actual_sha != expected_sha:
                drifts.append(_drift(component, logical_name, "sha256", expected_sha, actual_sha))
            if (
                _is_non_empty_string(logical_name_value)
                and expected_sha_valid
                and actual_sha == expected_sha
            ):
                identity_files.append(
                    {"logical_name": logical_name_value, "sha256": actual_sha}
                )

        if _is_non_empty_string(logical_id):
            identity[component] = {
                "logical_id": logical_id,
                "files": sorted(identity_files, key=lambda item: item["logical_name"]),
            }

    return drifts, identity


def _validation_policy_binding(
    manifest: dict[str, Any],
    root: pathlib.Path,
    *,
    validation_policy: dict[str, Any] | None,
    validation_config_sha256: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    """Bind the validation policy to the frozen run-config component.

    The caller may provide a separately selected config (the CLI case), but it
    is accepted only when its bytes are the bytes declared by the benchmark's
    run_config component.  Direct callers load the same declared file.
    """

    drifts: list[dict[str, Any]] = []
    components = manifest.get("components")
    run_config = components.get("run_config") if isinstance(components, dict) else None
    files = run_config.get("files") if isinstance(run_config, dict) else None
    bindings = [
        item
        for item in files if isinstance(item, dict)
        and item.get("logical_name") == "decision_evidence_validation"
    ] if isinstance(files, list) else []
    if len(bindings) != 1:
        drifts.append(
            _drift(
                "validation_config",
                "decision_evidence_validation",
                "run_config_binding",
                "exactly one run_config file",
                len(bindings),
            )
        )
        return drifts, None, None

    binding = bindings[0]
    bound_path = _resolve_path(root, binding.get("path"))
    bound_sha = _existing_file_sha256(bound_path) if bound_path is not None else None
    declared_sha = binding.get("sha256")
    if bound_sha is None or not _is_sha256(declared_sha) or bound_sha != declared_sha:
        drifts.append(
            _drift(
                "validation_config",
                "decision_evidence_validation",
                "sha256",
                declared_sha,
                bound_sha,
            )
        )
        return drifts, None, None

    try:
        bound_policy = json.loads(bound_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        drifts.append(
            _drift(
                "validation_config",
                "decision_evidence_validation",
                "content",
                "valid JSON object",
                type(exc).__name__,
            )
        )
        return drifts, None, None

    selected_policy = validation_policy
    selected_sha = validation_config_sha256
    if selected_policy is None:
        selected_policy = bound_policy
        selected_sha = bound_sha
    if not isinstance(selected_policy, dict):
        drifts.append(
            _drift(
                "validation_config",
                "decision_evidence_validation",
                "content",
                "JSON object",
                selected_policy,
            )
        )
        return drifts, None, None
    if selected_sha != bound_sha:
        drifts.append(
            _drift(
                "validation_config",
                "decision_evidence_validation",
                "selected_sha256",
                bound_sha,
                selected_sha,
            )
        )
        return drifts, None, None
    if selected_policy != bound_policy:
        drifts.append(
            _drift(
                "validation_config",
                "decision_evidence_validation",
                "selected_policy",
                bound_policy,
                selected_policy,
            )
        )
        return drifts, None, None
    try:
        canonical_json_bytes(selected_policy)
    except (TypeError, ValueError) as exc:
        drifts.append(
            _drift(
                "validation_config",
                "decision_evidence_validation",
                "canonical_content",
                "finite canonical JSON",
                type(exc).__name__,
            )
        )
        return drifts, None, None
    return drifts, {"sha256": bound_sha, "policy": selected_policy}, bound_sha


def _validate_evaluation_universe(
    manifest: dict[str, Any], root: pathlib.Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    drifts: list[dict[str, Any]] = []
    universe = manifest.get("evaluation_universe")
    if not isinstance(universe, dict):
        drifts.append(
            _drift("evaluation_universe", "", "shape", "object", universe)
        )
        return drifts, {"blocks": []}

    blocks = universe.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        drifts.append(
            _drift("evaluation_universe", "", "blocks", "non-empty array", blocks)
        )
        return drifts, {"blocks": []}

    identity_blocks: list[dict[str, Any]] = []
    seen_block_ids: set[str] = set()
    intervals: list[tuple[int, int, str]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            drifts.append(
                _drift("evaluation_universe", f"#{index}", "shape", "object", block)
            )
            continue

        block_id_value = block.get("block_id")
        block_id = block_id_value if isinstance(block_id_value, str) else f"#{index}"
        block_id_valid = _is_non_empty_string(block_id_value)
        if not block_id_valid:
            drifts.append(
                _drift(
                    "evaluation_universe",
                    block_id,
                    "block_id",
                    "non-empty string",
                    block_id_value,
                )
            )
        elif block_id_value in seen_block_ids:
            drifts.append(
                _drift(
                    "evaluation_universe",
                    block_id,
                    "block_id",
                    "unique",
                    block_id_value,
                )
            )
        else:
            seen_block_ids.add(block_id_value)

        start = block.get("start_timestamp_ms")
        end = block.get("end_timestamp_ms")
        valid_time_range = (
            _is_timestamp_ms(start) and _is_timestamp_ms(end) and start <= end
        )
        if not valid_time_range:
            drifts.append(
                _drift(
                    "evaluation_universe",
                    block_id,
                    "time_range",
                    "non-negative integer start_timestamp_ms <= end_timestamp_ms",
                    {"start_timestamp_ms": start, "end_timestamp_ms": end},
                )
            )
        elif block_id_valid:
            intervals.append((start, end, block_id_value))

        cells_value = block.get("cells")
        if not isinstance(cells_value, list) or not cells_value:
            drifts.append(
                _drift(
                    "evaluation_universe",
                    block_id,
                    "cells",
                    "non-empty array",
                    cells_value,
                )
            )
            cells_value = []
        cells: list[dict[str, str]] = []
        seen_cells: set[tuple[str, str]] = set()
        for cell in cells_value:
            if not isinstance(cell, dict):
                drifts.append(
                    _drift(
                        "evaluation_universe", block_id, "cells", "objects", cell
                    )
                )
                continue
            symbol = cell.get("symbol")
            regime = cell.get("entry_regime")
            if not (_is_non_empty_string(symbol) and _is_non_empty_string(regime)):
                drifts.append(
                    _drift(
                        "evaluation_universe",
                        block_id,
                        "cells",
                        "non-empty symbol and entry_regime",
                        cell,
                    )
                )
                continue
            key = (symbol, regime)
            if key in seen_cells:
                drifts.append(
                    _drift(
                        "evaluation_universe",
                        block_id,
                        "cells",
                        "unique symbol/entry_regime",
                        {"symbol": symbol, "entry_regime": regime},
                    )
                )
                continue
            seen_cells.add(key)
            cells.append({"symbol": symbol, "entry_regime": regime})

        expected_sha = block.get("event_sha256")
        expected_sha_valid = _is_sha256(expected_sha)
        if not expected_sha_valid:
            drifts.append(
                _drift(
                    "evaluation_universe",
                    block_id,
                    "event_sha256",
                    "64 lowercase hex",
                    expected_sha,
                )
            )
        declared_path = block.get("path")
        path = _resolve_path(root, declared_path)
        if path is None:
            drifts.append(
                _drift(
                    "evaluation_universe",
                    block_id,
                    "path",
                    "non-empty path",
                    declared_path,
                )
            )
            actual_sha = None
        else:
            actual_sha = _existing_file_sha256(path)
        if expected_sha_valid and actual_sha != expected_sha:
            drifts.append(
                _drift(
                    "evaluation_universe",
                    block_id,
                    "event_sha256",
                    expected_sha,
                    actual_sha,
                )
            )

        regimes_by_symbol: dict[str, list[str]] = {}
        for cell in cells:
            regimes_by_symbol.setdefault(cell["symbol"], []).append(
                cell["entry_regime"]
            )
        canonical_executions: list[dict[str, Any]] = []
        raw_executions = block.get("executions")
        if raw_executions is None and len(regimes_by_symbol) == 1:
            symbol = next(iter(regimes_by_symbol))
            if expected_sha_valid and actual_sha == expected_sha:
                canonical_executions.append(
                    {
                        "execution_id": f"{block_id_value}:{symbol}",
                        "symbol": symbol,
                        "planned_entry_regimes": sorted(regimes_by_symbol[symbol]),
                        "event_sha256": actual_sha,
                    }
                )
        elif not isinstance(raw_executions, list) or not raw_executions:
            drifts.append(
                _drift(
                    "evaluation_universe",
                    block_id,
                    "executions",
                    "one isolated execution per cell symbol",
                    raw_executions,
                )
            )
        else:
            seen_execution_ids: set[str] = set()
            seen_execution_symbols: set[str] = set()
            for execution_index, execution in enumerate(raw_executions):
                execution_name = f"{block_id}:#{execution_index}"
                if not isinstance(execution, dict):
                    drifts.append(
                        _drift(
                            "evaluation_universe",
                            execution_name,
                            "executions",
                            "object",
                            execution,
                        )
                    )
                    continue
                symbol = execution.get("symbol")
                execution_id = execution.get("execution_id")
                expected_execution_id = (
                    f"{block_id_value}:{symbol}"
                    if block_id_valid and _is_non_empty_string(symbol)
                    else "block_id:symbol"
                )
                execution_name = (
                    execution_id if _is_non_empty_string(execution_id)
                    else execution_name
                )
                if execution_id != expected_execution_id:
                    drifts.append(
                        _drift(
                            "evaluation_universe",
                            execution_name,
                            "execution_id",
                            expected_execution_id,
                            execution_id,
                        )
                    )
                elif execution_id in seen_execution_ids:
                    drifts.append(
                        _drift(
                            "evaluation_universe",
                            execution_name,
                            "execution_id",
                            "unique",
                            execution_id,
                        )
                    )
                else:
                    seen_execution_ids.add(execution_id)
                if not _is_non_empty_string(symbol):
                    drifts.append(
                        _drift(
                            "evaluation_universe",
                            execution_name,
                            "executions",
                            "non-empty symbol",
                            symbol,
                        )
                    )
                    continue
                if symbol in seen_execution_symbols:
                    drifts.append(
                        _drift(
                            "evaluation_universe",
                            execution_name,
                            "executions",
                            "one execution per symbol",
                            symbol,
                        )
                    )
                else:
                    seen_execution_symbols.add(symbol)
                execution_expected_sha = execution.get("event_sha256")
                execution_path = _resolve_path(root, execution.get("path"))
                execution_actual_sha = (
                    _existing_file_sha256(execution_path)
                    if execution_path is not None
                    else None
                )
                if not _is_sha256(execution_expected_sha):
                    drifts.append(
                        _drift(
                            "evaluation_universe",
                            execution_name,
                            "event_sha256",
                            "64 lowercase hex",
                            execution_expected_sha,
                        )
                    )
                elif execution_actual_sha != execution_expected_sha:
                    drifts.append(
                        _drift(
                            "evaluation_universe",
                            execution_name,
                            "event_sha256",
                            execution_expected_sha,
                            execution_actual_sha,
                        )
                    )
                if symbol not in regimes_by_symbol:
                    drifts.append(
                        _drift(
                            "evaluation_universe",
                            execution_name,
                            "executions",
                            sorted(regimes_by_symbol),
                            symbol,
                        )
                    )
                if (
                    execution_id == expected_execution_id
                    and symbol in regimes_by_symbol
                    and _is_sha256(execution_expected_sha)
                    and execution_actual_sha == execution_expected_sha
                ):
                    canonical_executions.append(
                        {
                            "execution_id": execution_id,
                            "symbol": symbol,
                            "planned_entry_regimes": sorted(
                                regimes_by_symbol[symbol]
                            ),
                            "event_sha256": execution_actual_sha,
                        }
                    )
            if seen_execution_symbols != set(regimes_by_symbol):
                drifts.append(
                    _drift(
                        "evaluation_universe",
                        block_id,
                        "executions",
                        sorted(regimes_by_symbol),
                        sorted(seen_execution_symbols),
                    )
                )

        if (
            block_id_valid
            and valid_time_range
            and expected_sha_valid
            and actual_sha == expected_sha
        ):
            identity_blocks.append(
                {
                    "block_id": block_id_value,
                    "start_timestamp_ms": start,
                    "end_timestamp_ms": end,
                    "event_sha256": actual_sha,
                    "cells": sorted(
                        cells,
                        key=lambda item: (item["symbol"], item["entry_regime"]),
                    ),
                    "executions": sorted(
                        canonical_executions,
                        key=lambda item: item["execution_id"],
                    ),
                }
            )

    previous: tuple[int, int, str] | None = None
    for interval in sorted(intervals, key=lambda item: (item[0], item[1], item[2])):
        if previous is not None and interval[0] <= previous[1]:
            drifts.append(
                _drift(
                    "evaluation_universe",
                    interval[2],
                    "overlap",
                    {"after_end_timestamp_ms": previous[1]},
                    {
                        "start_timestamp_ms": interval[0],
                        "overlaps_block_id": previous[2],
                    },
                )
            )
            if interval[1] > previous[1]:
                previous = interval
        else:
            previous = interval

    return drifts, {
        "blocks": sorted(identity_blocks, key=lambda item: item["block_id"])
    }


def validate_benchmark(
    manifest: dict,
    root: pathlib.Path,
    *,
    validation_policy: dict[str, Any] | None = None,
    validation_config_sha256: str | None = None,
) -> dict:
    """Validate file-backed identities and return a deterministic report."""

    root = pathlib.Path(root)
    if not isinstance(manifest, dict):
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "identity_status": "UNVERIFIABLE",
            "drifts": [_drift("manifest", "", "shape", "object", manifest)],
        }

    drifts: list[dict[str, Any]] = []
    schema_version = manifest.get("schema_version")
    if schema_version != BENCHMARK_SCHEMA_VERSION:
        drifts.append(
            _drift(
                "manifest",
                "",
                "schema_version",
                BENCHMARK_SCHEMA_VERSION,
                schema_version,
            )
        )

    component_drifts, components_identity = _validate_components(manifest, root)
    universe_drifts, universe_identity = _validate_evaluation_universe(manifest, root)
    policy_drifts, policy_identity, policy_sha256 = _validation_policy_binding(
        manifest,
        root,
        validation_policy=validation_policy,
        validation_config_sha256=validation_config_sha256,
    )
    drifts.extend(component_drifts)
    drifts.extend(universe_drifts)
    drifts.extend(policy_drifts)
    drifts.sort(key=_drift_sort_key)

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "identity_status": "UNVERIFIABLE" if drifts else "VERIFIED",
        "drifts": drifts,
    }
    if policy_sha256 is not None:
        report["validation_config_sha256"] = policy_sha256
    if not drifts:
        identity = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "components": components_identity,
            "evaluation_universe": universe_identity,
            "validation_policy": policy_identity,
        }
        report["benchmark_id"] = canonical_sha256(identity)
        report["canonical_identity"] = identity
    return report
