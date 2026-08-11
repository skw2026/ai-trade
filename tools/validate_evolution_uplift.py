#!/usr/bin/env python3
"""Validate paired self-evolution uplift using complete block replay evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

from decision_evidence_common import (
    file_sha256,
    validate_verified_benchmark_report,
)


REPORT_SCHEMA_VERSION = "evolution_uplift_validation_v1"
PAIRED_SCHEMA_VERSION = "paired_evolution_replay_v1"
BENCHMARK_SCHEMA_VERSION = "decision_evidence_benchmark_validation_v1"
EPISODE_SCHEMA_VERSION = "episode_execution_evidence_v1"
POLICY_SCHEMA_VERSION = "execution_policy_v2"
EXPECTED_POLICY_DIFFERENCE = {
    "path": "self_evolution.enabled",
    "frozen": False,
    "adaptive": True,
}
EXPECTED_UPLIFT_POLICY = {
    "min_independent_blocks": 8,
    "block_coverage": 1,
    "bootstrap_trials": 10000,
    "lcb": 0.95,
}
EPISODE_EVIDENCE_EPSILON = 1e-6
EPISODE_LINEAGE_FIELDS = (
    "decision_id",
    "candidate_id",
    "model_version",
    "mode",
    "position_episode_id",
)
ZERO_TRADE_FORBIDDEN_FIELDS = {
    "account_pnl",
    "account_realized_net_usd",
    "diagnostic_net_utility",
    "executable_net_utility",
    "realized_net_usd",
    "realized_pnl_usd",
    "self_evolution_update_count",
    "utility",
    "virtual_pnl",
    "virtual_pnl_usd",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _missing(values: Sequence[str]) -> list[str]:
    return sorted(set(str(value) for value in values if str(value)))


def bootstrap_draw_index(
    benchmark_id: str,
    trial: int,
    draw: int,
    block_count: int,
) -> int:
    if not _is_sha256(benchmark_id):
        raise ValueError("benchmark_id must be a lowercase SHA-256")
    if not _is_integer(trial) or trial < 0:
        raise ValueError("trial must be a non-negative integer")
    if not _is_integer(draw) or draw < 0:
        raise ValueError("draw must be a non-negative integer")
    if not _is_integer(block_count) or block_count <= 0:
        raise ValueError("block_count must be a positive integer")
    digest = hashlib.sha256(
        f"{benchmark_id}:uplift:{trial}:{draw}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], "big") % block_count


def block_bootstrap_statistics(
    block_deltas: Sequence[float],
    *,
    benchmark_id: str,
    trials: int,
) -> list[float]:
    if not block_deltas or not all(_is_finite_number(value) for value in block_deltas):
        raise ValueError("block deltas must be non-empty finite numbers")
    if not _is_integer(trials) or trials <= 0:
        raise ValueError("trials must be a positive integer")
    values = [float(value) for value in block_deltas]
    count = len(values)
    statistics: list[float] = []
    for trial in range(trials):
        selected_sum = 0.0
        for draw in range(count):
            selected_sum += values[
                bootstrap_draw_index(benchmark_id, trial, draw, count)
            ]
        statistics.append(selected_sum / count)
    return statistics


def lower_confidence_bound(
    values: Sequence[float], *, confidence: float
) -> tuple[float, int]:
    if not values or not all(_is_finite_number(value) for value in values):
        raise ValueError("bootstrap values must be non-empty finite numbers")
    if not _is_finite_number(confidence) or not 0.0 < float(confidence) <= 1.0:
        raise ValueError("confidence must be in (0, 1]")
    ordered = sorted(float(value) for value in values)
    index = math.floor((1.0 - float(confidence)) * (len(ordered) - 1))
    return ordered[index], index


def _uplift_policy(config: Any) -> tuple[dict[str, Any] | None, list[str]]:
    missing: list[str] = []
    if not isinstance(config, Mapping):
        return None, ["config"]
    if config.get("schema_version") != "decision_evidence_validation_v1":
        missing.append("config.schema_version=decision_evidence_validation_v1")
    raw = config.get("uplift")
    if not isinstance(raw, Mapping):
        return None, _missing([*missing, "config.uplift"])
    min_blocks = raw.get("min_independent_blocks")
    coverage = raw.get("block_coverage")
    trials = raw.get("bootstrap_trials")
    confidence = raw.get("lcb")
    if not _is_integer(min_blocks) or min_blocks <= 0:
        missing.append("config.uplift.min_independent_blocks")
    if not _is_finite_number(coverage) or float(coverage) != 1.0:
        missing.append("config.uplift.block_coverage")
    if not _is_integer(trials) or trials <= 0:
        missing.append("config.uplift.bootstrap_trials")
    if not _is_finite_number(confidence) or not 0.0 < float(confidence) <= 1.0:
        missing.append("config.uplift.lcb")
    if dict(raw) != EXPECTED_UPLIFT_POLICY:
        missing.append("config.uplift=frozen_v1_contract")
    if missing:
        return None, _missing(missing)
    return {
        "min_independent_blocks": int(min_blocks),
        "block_coverage": float(coverage),
        "bootstrap_trials": int(trials),
        "lcb": float(confidence),
        "lcb_required": ">0",
    }, []


def _benchmark_universe(
    benchmark_report: Any,
) -> tuple[str | None, list[dict[str, Any]], list[str]]:
    missing: list[str] = []
    if not isinstance(benchmark_report, Mapping):
        return None, [], ["benchmark_report"]
    if benchmark_report.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        missing.append(f"benchmark.schema_version={BENCHMARK_SCHEMA_VERSION}")
    if benchmark_report.get("identity_status") != "VERIFIED":
        missing.append("benchmark.identity_status=VERIFIED")
    if benchmark_report.get("drifts") != []:
        missing.append("benchmark.drifts=empty")
    benchmark_id = benchmark_report.get("benchmark_id")
    if not _is_sha256(benchmark_id):
        missing.append("benchmark.benchmark_id")
    identity = benchmark_report.get("canonical_identity")
    universe = identity.get("evaluation_universe") if isinstance(identity, Mapping) else None
    raw_blocks = universe.get("blocks") if isinstance(universe, Mapping) else None
    if not isinstance(raw_blocks, list) or not raw_blocks:
        return (
            benchmark_id if isinstance(benchmark_id, str) else None,
            [],
            _missing([*missing, "benchmark.evaluation_universe.blocks"]),
        )

    blocks: list[dict[str, Any]] = []
    seen_block_ids: set[str] = set()
    intervals: list[tuple[int, int, str]] = []
    for index, raw_block in enumerate(raw_blocks):
        prefix = f"benchmark.evaluation_universe.blocks[{index}]"
        if not isinstance(raw_block, Mapping):
            missing.append(prefix)
            continue
        block_id = raw_block.get("block_id")
        start = raw_block.get("start_timestamp_ms")
        end = raw_block.get("end_timestamp_ms")
        event_sha = raw_block.get("event_sha256")
        if not _is_non_empty_string(block_id):
            missing.append(f"{prefix}.block_id")
        elif block_id in seen_block_ids:
            missing.append(f"{prefix}.block_id=unique")
        else:
            seen_block_ids.add(str(block_id))
        if not (
            _is_integer(start)
            and _is_integer(end)
            and start >= 0
            and start <= end
        ):
            missing.append(f"{prefix}.interval")
        elif _is_non_empty_string(block_id):
            intervals.append((int(start), int(end), str(block_id)))
        if not _is_sha256(event_sha):
            missing.append(f"{prefix}.event_sha256")
        raw_cells = raw_block.get("cells")
        if not isinstance(raw_cells, list) or not raw_cells:
            missing.append(f"{prefix}.cells")
            raw_cells = []
        cells: list[dict[str, str]] = []
        seen_cells: set[tuple[str, str]] = set()
        for cell_index, raw_cell in enumerate(raw_cells):
            cell_prefix = f"{prefix}.cells[{cell_index}]"
            if not isinstance(raw_cell, Mapping):
                missing.append(cell_prefix)
                continue
            symbol = raw_cell.get("symbol")
            regime = raw_cell.get("entry_regime")
            if not _is_non_empty_string(symbol):
                missing.append(f"{cell_prefix}.symbol")
            if not _is_non_empty_string(regime):
                missing.append(f"{cell_prefix}.entry_regime")
            if _is_non_empty_string(symbol) and _is_non_empty_string(regime):
                key = (str(symbol), str(regime))
                if key in seen_cells:
                    missing.append(f"{prefix}.cells=unique")
                else:
                    seen_cells.add(key)
                    cells.append({"symbol": key[0], "entry_regime": key[1]})
        regimes_by_symbol: dict[str, list[str]] = {}
        for cell in cells:
            regimes_by_symbol.setdefault(cell["symbol"], []).append(
                cell["entry_regime"]
            )
        executions: list[dict[str, Any]] = []
        raw_executions = raw_block.get("executions")
        if raw_executions is not None:
            if not isinstance(raw_executions, list) or not raw_executions:
                missing.append(f"{prefix}.executions")
                raw_executions = []
            seen_execution_ids: set[str] = set()
            seen_execution_symbols: set[str] = set()
            for execution_index, raw_execution in enumerate(raw_executions):
                execution_prefix = f"{prefix}.executions[{execution_index}]"
                if not isinstance(raw_execution, Mapping):
                    missing.append(execution_prefix)
                    continue
                execution_id = raw_execution.get("execution_id")
                symbol = raw_execution.get("symbol")
                planned_regimes = raw_execution.get("planned_entry_regimes")
                execution_event_sha = raw_execution.get("event_sha256")
                expected_execution_id = f"{block_id}:{symbol}"
                expected_regimes = sorted(regimes_by_symbol.get(str(symbol), []))
                if execution_id != expected_execution_id:
                    missing.append(f"{execution_prefix}.execution_id")
                elif execution_id in seen_execution_ids:
                    missing.append(f"{execution_prefix}.execution_id=unique")
                else:
                    seen_execution_ids.add(str(execution_id))
                if not _is_non_empty_string(symbol):
                    missing.append(f"{execution_prefix}.symbol")
                elif symbol in seen_execution_symbols:
                    missing.append(f"{execution_prefix}.symbol=unique")
                else:
                    seen_execution_symbols.add(str(symbol))
                if planned_regimes != expected_regimes:
                    missing.append(f"{execution_prefix}.planned_entry_regimes")
                if not _is_sha256(execution_event_sha):
                    missing.append(f"{execution_prefix}.event_sha256")
                if (
                    execution_id == expected_execution_id
                    and _is_non_empty_string(symbol)
                    and planned_regimes == expected_regimes
                    and _is_sha256(execution_event_sha)
                ):
                    executions.append(
                        {
                            "execution_id": str(execution_id),
                            "symbol": str(symbol),
                            "planned_entry_regimes": list(expected_regimes),
                            "event_sha256": str(execution_event_sha),
                        }
                    )
            if seen_execution_symbols != set(regimes_by_symbol):
                missing.append(f"{prefix}.executions.coverage")
        if (
            _is_non_empty_string(block_id)
            and _is_integer(start)
            and _is_integer(end)
            and start >= 0
            and start <= end
            and _is_sha256(event_sha)
            and cells
        ):
            blocks.append(
                {
                    "block_id": str(block_id),
                    "start_timestamp_ms": int(start),
                    "end_timestamp_ms": int(end),
                    "event_sha256": str(event_sha),
                    "cells": cells,
                    "executions": executions,
                }
            )
    previous: tuple[int, int, str] | None = None
    for interval in sorted(intervals):
        if previous is not None and interval[0] <= previous[1]:
            missing.append("benchmark.evaluation_universe.non_overlapping")
        if previous is None or interval[1] > previous[1]:
            previous = interval
    return (
        benchmark_id if isinstance(benchmark_id, str) else None,
        blocks,
        _missing(missing),
    )


def _policy_identity(
    raw_identity: Any,
    *,
    prefix: str,
) -> tuple[dict[str, Any], str, list[str]]:
    missing: list[str] = []
    if not isinstance(raw_identity, Mapping):
        return {}, "", [prefix]
    if raw_identity.get("schema_version") != POLICY_SCHEMA_VERSION:
        missing.append(f"{prefix}.schema_version={POLICY_SCHEMA_VERSION}")
    policy = raw_identity.get("policy")
    if not isinstance(policy, Mapping):
        return {}, "", _missing([*missing, f"{prefix}.policy"])
    policy_dict = dict(policy)
    actual_sha = raw_identity.get("sha256")
    expected_sha = canonical_sha256(policy_dict)
    if actual_sha != expected_sha:
        missing.append(f"{prefix}.sha256")
    return policy_dict, expected_sha, _missing(missing)


def _validate_episode(
    episode: Any,
    *,
    prefix: str,
    block_id: str,
    segment_sha256: str,
    policy_sha256: str,
    planned_cells: set[tuple[str, str]],
) -> tuple[dict[str, Any] | None, list[str]]:
    missing: list[str] = []
    if not isinstance(episode, Mapping):
        return None, [prefix]
    episode_id = episode.get("evaluator_episode_id")
    symbol = episode.get("symbol")
    entry_regime = episode.get("entry_regime")
    utility = episode.get("executable_net_utility")
    first_fill_id = episode.get("first_fill_id")
    if not _is_non_empty_string(first_fill_id):
        missing.append(f"{prefix}.first_fill_id")
    if not _is_sha256(episode_id):
        missing.append(f"{prefix}.evaluator_episode_id")
    if (
        _is_sha256(segment_sha256)
        and _is_non_empty_string(symbol)
        and _is_non_empty_string(first_fill_id)
    ):
        try:
            expected_episode_id = hashlib.sha256(
                f"{segment_sha256}:{symbol}:{first_fill_id}".encode("ascii")
            ).hexdigest()
        except UnicodeEncodeError:
            missing.append(f"{prefix}.evaluator_episode_id.preimage_ascii")
        else:
            if episode_id != expected_episode_id:
                missing.append(f"{prefix}.evaluator_episode_id")
    if episode.get("segment_identity_sha256") != segment_sha256:
        missing.append(f"{prefix}.segment_identity_sha256")
    cell = (str(symbol or ""), str(entry_regime or ""))
    if not (_is_non_empty_string(symbol) and _is_non_empty_string(entry_regime)):
        missing.append(f"{prefix}.cell_identity")
    elif cell not in planned_cells:
        missing.append(
            f"{prefix}.unplanned_cell:{cell[0]}:{cell[1]}"
        )
    if episode.get("execution_path_complete") is not True:
        missing.append(f"{prefix}.execution_path_complete")
    if episode.get("utility_source") != "complete_execution_replay":
        missing.append(f"{prefix}.utility_source=complete_execution_replay")
    if not _is_finite_number(utility):
        missing.append(f"{prefix}.executable_net_utility")
    if episode.get("missing_path_evidence") != []:
        missing.append(f"{prefix}.missing_path_evidence=empty")
    if episode.get("identity_mismatches") != []:
        missing.append(f"{prefix}.identity_mismatches=empty")
    episode_policy = episode.get("execution_policy_identity")
    if not isinstance(episode_policy, Mapping) or episode_policy.get("sha256") != policy_sha256:
        missing.append(f"{prefix}.execution_policy_identity")
    for field in (
        "candidate_lineage",
        "position_episode",
        "exit_capture",
        "terminal_settlement",
    ):
        if not isinstance(episode.get(field), Mapping) or not episode.get(field):
            missing.append(f"{prefix}.{field}")
    for field in ("fill_ids", "client_order_ids", "fills"):
        value = episode.get(field)
        if not isinstance(value, list) or not value:
            missing.append(f"{prefix}.{field}")
    fills = episode.get("fills")
    fill_rows = fills if isinstance(fills, list) else []
    declared_fill_ids = episode.get("fill_ids")
    declared_client_order_ids = episode.get("client_order_ids")
    actual_fill_ids: list[str] = []
    actual_client_order_ids: list[str] = []
    for fill_index, fill in enumerate(fill_rows):
        if not isinstance(fill, Mapping):
            missing.append(f"{prefix}.fills[{fill_index}]")
            continue
        fill_id = fill.get("fill_id")
        client_order_id = fill.get("client_order_id")
        if not _is_non_empty_string(fill_id):
            missing.append(f"{prefix}.fills[{fill_index}].fill_id")
        else:
            actual_fill_ids.append(str(fill_id))
        if not _is_non_empty_string(client_order_id):
            missing.append(f"{prefix}.fills[{fill_index}].client_order_id")
        else:
            actual_client_order_ids.append(str(client_order_id))
        for state_field in ("order_state_before", "order_state_after"):
            if str(fill.get(state_field) or "").lower() in {"", "missing"}:
                missing.append(
                    f"{prefix}.fills[{fill_index}].{state_field}"
                )
        for numeric_field in ("direction", "qty", "price", "fee"):
            if not _is_finite_number(fill.get(numeric_field)):
                missing.append(
                    f"{prefix}.fills[{fill_index}].{numeric_field}"
                )
    if len(actual_fill_ids) != len(set(actual_fill_ids)):
        missing.append(f"{prefix}.fill_id_unique")
    if fill_rows and first_fill_id != fill_rows[0].get("fill_id"):
        missing.append(f"{prefix}.first_fill_id")
    if declared_fill_ids != actual_fill_ids:
        missing.append(f"{prefix}.fill_ids")
    if declared_client_order_ids != actual_client_order_ids:
        missing.append(f"{prefix}.client_order_ids")
    for field in ("realized_pnl_usd", "fee_usd", "funding_paid_usd"):
        if not _is_finite_number(episode.get(field)):
            missing.append(f"{prefix}.{field}")
    lineage = episode.get("candidate_lineage")
    if isinstance(lineage, Mapping) and not all(
        _is_non_empty_string(lineage.get(field))
        for field in EPISODE_LINEAGE_FIELDS
    ):
        missing.append(f"{prefix}.candidate_lineage.fields")
    if isinstance(lineage, Mapping):
        if episode.get("runtime_position_episode_id") != lineage.get(
            "position_episode_id"
        ):
            missing.append(f"{prefix}.candidate_lineage.position_episode_id")
        for fill_index, fill in enumerate(fill_rows):
            fill_lineage = fill.get("candidate_lineage") if isinstance(fill, Mapping) else None
            if not isinstance(fill_lineage, Mapping) or any(
                fill_lineage.get(field) != lineage.get(field)
                for field in EPISODE_LINEAGE_FIELDS
            ):
                missing.append(
                    f"{prefix}.fills[{fill_index}].candidate_lineage"
                )
    position = episode.get("position_episode")
    client_order_ids = episode.get("client_order_ids")
    client_order_rows = client_order_ids if isinstance(client_order_ids, list) else []
    if isinstance(position, Mapping) and (
        position.get("evidence_complete") is not True
        or position.get("symbol") != symbol
        or position.get("fill_event_count") != len(fill_rows)
        or position.get("unique_order_count")
        != len(set(str(value) for value in client_order_rows))
    ):
        missing.append(f"{prefix}.position_episode.fields")
    if isinstance(position, Mapping) and isinstance(lineage, Mapping):
        if any(
            position.get(field) != lineage.get(field)
            for field in EPISODE_LINEAGE_FIELDS
        ):
            missing.append(f"{prefix}.position_episode.identity")
    exit_capture = episode.get("exit_capture")
    if isinstance(exit_capture, Mapping):
        last_fill = (
            fill_rows[-1]
            if fill_rows and isinstance(fill_rows[-1], Mapping)
            else {}
        )
        if (
            not last_fill
            or exit_capture.get("client_order_id")
            != last_fill.get("client_order_id")
            or exit_capture.get("symbol") != symbol
        ):
            missing.append(f"{prefix}.exit_capture.identity")
        if (
            _is_finite_number(episode.get("realized_pnl_usd"))
            and (
                not _is_finite_number(exit_capture.get("realized_pnl_usd"))
                or not math.isclose(
                    float(exit_capture["realized_pnl_usd"]),
                    float(episode["realized_pnl_usd"]),
                    rel_tol=0.0,
                    abs_tol=EPISODE_EVIDENCE_EPSILON,
                )
            )
        ):
            missing.append(f"{prefix}.exit_capture.realized_pnl_usd")
    terminal = episode.get("terminal_settlement")
    if isinstance(terminal, Mapping) and (
        terminal.get("done_count") != 1
        or terminal.get("failed_count") != 0
        or terminal.get("position_count") != 0
        or terminal.get("segment_identity_sha256") != segment_sha256
        or not all(
            _is_finite_number(terminal.get(field))
            for field in ("realized_net_usd", "fees_usd", "funding_paid_usd")
        )
    ):
        missing.append(f"{prefix}.terminal_settlement.fields")
    if (
        fill_rows
        and _is_finite_number(episode.get("fee_usd"))
        and all(
            isinstance(fill, Mapping) and _is_finite_number(fill.get("fee"))
            for fill in fill_rows
        )
        and not math.isclose(
            float(episode["fee_usd"]),
            sum(abs(float(fill["fee"])) for fill in fill_rows),
            rel_tol=0.0,
            abs_tol=EPISODE_EVIDENCE_EPSILON,
        )
    ):
        missing.append(f"{prefix}.fee_sum")
    if all(
        _is_finite_number(episode.get(field))
        for field in (
            "realized_pnl_usd",
            "fee_usd",
            "funding_paid_usd",
            "executable_net_utility",
        )
    ) and not math.isclose(
        float(episode["executable_net_utility"]),
        float(episode["realized_pnl_usd"])
        - float(episode["fee_usd"])
        - float(episode["funding_paid_usd"]),
        rel_tol=0.0,
        abs_tol=EPISODE_EVIDENCE_EPSILON,
    ):
        missing.append(f"{prefix}.executable_net_utility_formula")
    if (
        isinstance(position, Mapping)
        and _is_finite_number(utility)
        and (
            not _is_finite_number(position.get("realized_net_usd"))
            or not math.isclose(
                float(position["realized_net_usd"]),
                float(utility),
                rel_tol=0.0,
                abs_tol=EPISODE_EVIDENCE_EPSILON,
            )
        )
    ):
        missing.append(f"{prefix}.position_episode.realized_net_usd")
    if (
        isinstance(position, Mapping)
        and _is_finite_number(episode.get("funding_paid_usd"))
        and (
            not _is_finite_number(position.get("funding_paid_usd"))
            or not math.isclose(
                float(position["funding_paid_usd"]),
                float(episode["funding_paid_usd"]),
                rel_tol=0.0,
                abs_tol=EPISODE_EVIDENCE_EPSILON,
            )
        )
    ):
        missing.append(f"{prefix}.position_episode.funding_paid_usd")
    if missing:
        return None, _missing(missing)
    normalized = dict(episode)
    normalized.update(
        {
            "block_id": block_id,
            "symbol": cell[0],
            "entry_regime": cell[1],
            "executable_net_utility": float(utility),
        }
    )
    return normalized, []


def _audit_arm(
    *,
    arm_name: str,
    raw_arm: Any,
    expected_enabled: bool,
    expected_blocks: list[dict[str, Any]],
    plan_by_id: dict[str, dict[str, Any]],
    common_policy: dict[str, Any],
    trade_bot_sha256: str,
    initial_weights_sha256: str,
    initial_state_sha256: str,
) -> tuple[dict[str, Any], dict[tuple[str, str, str], tuple[float, int]], list[str]]:
    missing: list[str] = []
    expected_ids = [block["block_id"] for block in expected_blocks]
    base = {
        "status": "UNVERIFIABLE",
        "expected_block_ids": expected_ids,
        "executed_block_ids": [],
        "coverage_ratio": 0.0,
        "expected_execution_count": len(expected_ids),
        "verified_execution_count": 0,
        "execution_coverage_ratio": 0.0,
        "episodes": [],
        "zero_trade_block_ids": [],
        "zero_trade_execution_ids": [],
        "blocks": [],
        "block_audit": [],
        "missing_evidence": [],
    }
    if not isinstance(raw_arm, Mapping):
        missing.append(f"paired.arms.{arm_name}")
        base["missing_evidence"] = _missing(missing)
        return base, {}, base["missing_evidence"]
    prefix = f"paired.arms.{arm_name}"
    if raw_arm.get("infrastructure_status") != "VERIFIED":
        missing.append(f"{prefix}.infrastructure_status=VERIFIED")
    if raw_arm.get("mismatches") != []:
        missing.append(f"{prefix}.mismatches=empty")
    business_status = raw_arm.get("business_gate_status")
    report_identity = raw_arm.get("report")
    if not isinstance(report_identity, Mapping):
        missing.append(f"{prefix}.report")
    else:
        if report_identity.get("schema_version") != "exact_replay_block_audit_v1":
            missing.append(f"{prefix}.report.schema_version")
        if not _is_sha256(report_identity.get("sha256")):
            missing.append(f"{prefix}.report.sha256")
        if business_status == "PASSED":
            if raw_arm.get("exit_code") != 0 or report_identity.get("status") != "VERIFIED":
                missing.append(f"{prefix}.business_report_consistency")
        elif business_status == "FAILED":
            if raw_arm.get("exit_code") != 2 or report_identity.get("status") != "UNVERIFIABLE":
                missing.append(f"{prefix}.business_report_consistency")
        else:
            missing.append(f"{prefix}.business_gate_status")
    declared_trade_bot = raw_arm.get("trade_bot_sha256")
    if declared_trade_bot is not None and declared_trade_bot != trade_bot_sha256:
        missing.append(f"{prefix}.trade_bot_sha256")
    config_identity = raw_arm.get("config")
    raw_policy_identity = (
        config_identity.get("policy") if isinstance(config_identity, Mapping) else None
    )
    arm_policy, arm_policy_sha, policy_missing = _policy_identity(
        raw_policy_identity,
        prefix=f"{prefix}.config.policy",
    )
    missing.extend(policy_missing)
    if not isinstance(config_identity, Mapping) or not _is_sha256(
        config_identity.get("sha256")
    ):
        missing.append(f"{prefix}.config.sha256")
    if arm_policy.get("self_evolution.enabled") is not expected_enabled:
        missing.append(f"{prefix}.self_evolution.enabled={str(expected_enabled).lower()}")
    arm_common = {
        key: value
        for key, value in arm_policy.items()
        if key != "self_evolution.enabled"
    }
    if arm_common != common_policy:
        missing.append(f"{prefix}.policy_difference_not_only_enabled")

    declared_expected_ids = raw_arm.get("expected_block_ids")
    executed_ids = raw_arm.get("executed_block_ids")
    counts = raw_arm.get("block_execution_counts")
    if declared_expected_ids != expected_ids:
        missing.append(f"{prefix}.expected_block_ids")
    if executed_ids != expected_ids:
        missing.append(f"{prefix}.executed_block_ids")
    if not isinstance(counts, Mapping) or dict(counts) != {
        block_id: 1 for block_id in expected_ids
    }:
        missing.append(f"{prefix}.block_execution_counts")
    actual_executed = executed_ids if isinstance(executed_ids, list) else []
    base["executed_block_ids"] = list(actual_executed)
    base["coverage_ratio"] = (
        len(set(actual_executed) & set(expected_ids)) / len(expected_ids)
        if expected_ids
        else 0.0
    )

    raw_blocks = raw_arm.get("blocks")
    if not isinstance(raw_blocks, list):
        missing.append(f"{prefix}.blocks")
        raw_blocks = []
    block_by_id: dict[str, Mapping[str, Any]] = {}
    for raw_block in raw_blocks:
        if not isinstance(raw_block, Mapping):
            missing.append(f"{prefix}.blocks.object")
            continue
        block_id = raw_block.get("block_id")
        if not _is_non_empty_string(block_id):
            missing.append(f"{prefix}.blocks.block_id")
        elif block_id in block_by_id:
            missing.append(f"{prefix}.blocks.block_id=unique")
        else:
            block_by_id[str(block_id)] = raw_block
    if list(block_by_id) != expected_ids:
        missing.append(f"{prefix}.blocks.coverage")

    seen_episode_ids: set[str] = set()
    first_fill_segments: dict[str, str] = {}
    normalized_episodes: list[dict[str, Any]] = []
    aggregation: dict[tuple[str, str, str], tuple[float, int]] = {}
    zero_trade_blocks: list[str] = []
    block_audit: list[dict[str, Any]] = []
    for expected in expected_blocks:
        block_id = expected["block_id"]
        block_prefix = f"{prefix}.blocks.{block_id}"
        raw_block = block_by_id.get(block_id)
        block_missing: list[str] = []
        if raw_block is None:
            block_missing.append(block_prefix)
            block_audit.append(
                {
                    "block_id": block_id,
                    "status": "UNVERIFIABLE",
                    "episode_count": 0,
                    "zero_trade": False,
                    "missing_evidence": block_missing,
                }
            )
            missing.extend(block_missing)
            continue
        plan = plan_by_id.get(block_id, {})
        if raw_block.get("symbol") != plan.get("symbol"):
            block_missing.append(f"{block_prefix}.symbol")
        if raw_block.get("event_sha256") != expected["event_sha256"]:
            block_missing.append(f"{block_prefix}.event_sha256")
        segment_sha = plan.get("segment_identity_sha256")
        if not _is_sha256(segment_sha):
            block_missing.append(f"{block_prefix}.planned_segment_identity_sha256")
        if raw_block.get("segment_identity_sha256") != segment_sha:
            block_missing.append(f"{block_prefix}.segment_identity_sha256")
        if raw_block.get("initial_weights_sha256") != initial_weights_sha256:
            block_missing.append(f"{block_prefix}.initial_weights_sha256")
        if raw_block.get("initial_evolution_state_sha256") != initial_state_sha256:
            block_missing.append(f"{block_prefix}.initial_evolution_state_sha256")
        if raw_block.get("historical_state_loaded") is not False:
            block_missing.append(f"{block_prefix}.historical_state_loaded=false")
        if raw_block.get("continued_from_block_id") not in (None, ""):
            block_missing.append(f"{block_prefix}.continued_from_block_id=empty")
        if raw_block.get("trade_bot_exit_code") != 0:
            block_missing.append(f"{block_prefix}.trade_bot_exit_code=0")
        if raw_block.get("assess_exit_code") not in (0, 1):
            block_missing.append(f"{block_prefix}.assess_exit_code")
        evidence = raw_block.get("episode_execution_evidence")
        planned_cells = {
            (cell["symbol"], cell["entry_regime"]) for cell in expected["cells"]
        }
        block_episodes: list[dict[str, Any]] = []
        zero_trade = False
        if not isinstance(evidence, Mapping):
            block_missing.append(f"{block_prefix}.episode_ledger")
        else:
            if evidence.get("schema_version") != EPISODE_SCHEMA_VERSION:
                block_missing.append(f"{block_prefix}.episode_ledger.schema_version")
            if evidence.get("segment_identity_sha256") != segment_sha:
                block_missing.append(f"{block_prefix}.episode_ledger.segment_identity_sha256")
            evidence_policy = evidence.get("execution_policy_identity")
            if not isinstance(evidence_policy, Mapping) or evidence_policy.get("sha256") != arm_policy_sha:
                block_missing.append(f"{block_prefix}.episode_ledger.policy_identity")
            episodes = evidence.get("episodes")
            if not isinstance(episodes, list):
                block_missing.append(f"{block_prefix}.episode_ledger.episodes")
                episodes = []
            episode_count = evidence.get("episode_count")
            complete_count = evidence.get("complete_episode_count")
            terminal_settlement = evidence.get("terminal_settlement")
            terminal_complete = (
                isinstance(terminal_settlement, Mapping)
                and terminal_settlement.get("done_count") == 1
                and terminal_settlement.get("failed_count") == 0
                and terminal_settlement.get("position_count") == 0
                and terminal_settlement.get("segment_identity_sha256")
                == segment_sha
                and all(
                    _is_finite_number(terminal_settlement.get(field))
                    for field in (
                        "realized_net_usd",
                        "fees_usd",
                        "funding_paid_usd",
                    )
                )
            )
            if not terminal_complete:
                block_missing.append(
                    f"{block_prefix}.episode_ledger.terminal_settlement"
                )
            if episodes:
                if episode_count != len(episodes):
                    block_missing.append(f"{block_prefix}.episode_ledger.episode_count")
                if complete_count != len(episodes):
                    block_missing.append(f"{block_prefix}.episode_ledger.complete_episode_count")
                if evidence.get("execution_path_complete") is not True:
                    block_missing.append(f"{block_prefix}.episode_ledger.execution_path_complete")
                if evidence.get("aggregate_only_rejected") is not False:
                    block_missing.append(f"{block_prefix}.episode_ledger.aggregate_only_rejected")
                if evidence.get("missing_path_evidence") != []:
                    block_missing.append(f"{block_prefix}.episode_ledger.missing_path_evidence")
                if raw_block.get("no_trade_zero_utility") is not False:
                    block_missing.append(f"{block_prefix}.no_trade_zero_utility=false")
                for episode_index, episode in enumerate(episodes):
                    raw_episode_id = (
                        str(episode.get("evaluator_episode_id") or "")
                        if isinstance(episode, Mapping)
                        else ""
                    )
                    duplicate_episode_id = bool(
                        raw_episode_id and raw_episode_id in seen_episode_ids
                    )
                    if duplicate_episode_id:
                        block_missing.append(
                            f"{block_prefix}.episode_id_duplicate:{raw_episode_id}"
                        )
                    elif raw_episode_id:
                        seen_episode_ids.add(raw_episode_id)
                    normalized, episode_missing = _validate_episode(
                        episode,
                        prefix=f"{block_prefix}.episodes[{episode_index}]",
                        block_id=block_id,
                        segment_sha256=str(segment_sha or ""),
                        policy_sha256=arm_policy_sha,
                        planned_cells=planned_cells,
                    )
                    block_missing.extend(episode_missing)
                    if normalized is not None and not duplicate_episode_id:
                        first_fill_id = str(normalized["first_fill_id"])
                        block_episodes.append(normalized)
                        previous_segment = first_fill_segments.get(first_fill_id)
                        if (
                            previous_segment is not None
                            and previous_segment != segment_sha
                        ):
                            block_missing.append(
                                f"{block_prefix}.first_fill_id_cross_segment_reuse:"
                                f"{first_fill_id}"
                            )
                        else:
                            first_fill_segments[first_fill_id] = str(segment_sha)
                        if (
                            isinstance(terminal_settlement, Mapping)
                            and normalized.get("terminal_settlement")
                            != terminal_settlement
                        ):
                            block_missing.append(
                                f"{block_prefix}.episodes[{episode_index}]."
                                "terminal_settlement_identity"
                            )
                if terminal_complete and len(block_episodes) == len(episodes):
                    terminal_expectations = {
                        "realized_net_usd": sum(
                            float(episode["executable_net_utility"])
                            for episode in block_episodes
                        ),
                        "fees_usd": sum(
                            float(episode["fee_usd"])
                            for episode in block_episodes
                        ),
                        "funding_paid_usd": sum(
                            float(episode["funding_paid_usd"])
                            for episode in block_episodes
                        ),
                    }
                    for field, expected_value in terminal_expectations.items():
                        if not math.isclose(
                            float(terminal_settlement[field]),
                            expected_value,
                            rel_tol=0.0,
                            abs_tol=EPISODE_EVIDENCE_EPSILON,
                        ):
                            block_missing.append(
                                f"{block_prefix}.episode_ledger.terminal_{field}"
                            )
            else:
                aggregate_pollution = sorted(
                    ZERO_TRADE_FORBIDDEN_FIELDS & set(evidence)
                )
                if aggregate_pollution:
                    block_missing.append(
                        f"{block_prefix}.episode_ledger.aggregate_pollution:"
                        + ",".join(aggregate_pollution)
                    )
                zero_terminal_complete = terminal_complete and all(
                    math.isclose(
                        float(terminal_settlement[field]),
                        0.0,
                        rel_tol=0.0,
                        abs_tol=EPISODE_EVIDENCE_EPSILON,
                    )
                    for field in (
                        "realized_net_usd",
                        "fees_usd",
                        "funding_paid_usd",
                    )
                )
                if terminal_complete and not zero_terminal_complete:
                    block_missing.append(
                        f"{block_prefix}.episode_ledger.terminal_nonzero"
                    )
                zero_trade = (
                    episode_count == 0
                    and complete_count == 0
                    and evidence.get("execution_path_complete") is False
                    and evidence.get("aggregate_only_rejected") is True
                    and evidence.get("missing_path_evidence") == ["fills"]
                    and raw_block.get("no_trade_zero_utility") is True
                    and zero_terminal_complete
                    and not aggregate_pollution
                )
                if not zero_trade:
                    block_missing.append(f"{block_prefix}.episode_ledger.zero_trade_contract")
        for episode in block_episodes:
            key = (
                block_id,
                str(episode["symbol"]),
                str(episode["entry_regime"]),
            )
            previous_utility, previous_count = aggregation.get(key, (0.0, 0))
            aggregation[key] = (
                previous_utility + float(episode["executable_net_utility"]),
                previous_count + 1,
            )
        normalized_episodes.extend(block_episodes)
        if zero_trade:
            zero_trade_blocks.append(block_id)
        block_missing = _missing(block_missing)
        missing.extend(block_missing)
        block_audit.append(
            {
                "block_id": block_id,
                "status": "VERIFIED" if not block_missing else "UNVERIFIABLE",
                "episode_count": len(block_episodes),
                "zero_trade": zero_trade,
                "event_sha256": raw_block.get("event_sha256"),
                "segment_identity_sha256": raw_block.get(
                    "segment_identity_sha256"
                ),
                "missing_evidence": block_missing,
            }
        )

    assess_exit_codes = [
        raw_block.get("assess_exit_code")
        for raw_block in raw_blocks
        if isinstance(raw_block, Mapping)
    ]
    if business_status == "PASSED" and any(code != 0 for code in assess_exit_codes):
        missing.append(f"{prefix}.business_gate_status_vs_blocks")
    if business_status == "FAILED" and 1 not in assess_exit_codes:
        missing.append(f"{prefix}.business_gate_status_vs_blocks")

    normalized_episodes.sort(
        key=lambda item: (item["block_id"], item["evaluator_episode_id"])
    )
    verified_block_ids = [
        block["block_id"]
        for block in block_audit
        if block["status"] == "VERIFIED"
    ]
    base.update(
        {
            "status": "VERIFIED" if not missing else "UNVERIFIABLE",
            "episodes": normalized_episodes,
            "zero_trade_block_ids": zero_trade_blocks,
            "zero_trade_execution_ids": list(zero_trade_blocks),
            "blocks": block_audit,
            "block_audit": block_audit,
            "verified_execution_count": len(verified_block_ids),
            "execution_coverage_ratio": (
                len(verified_block_ids) / len(expected_ids) if expected_ids else 0.0
            ),
            "missing_evidence": _missing(missing),
            "policy_sha256": arm_policy_sha,
            "trade_bot_sha256": trade_bot_sha256,
        }
    )
    return base, aggregation, base["missing_evidence"]


def _audit_execution_ledger(
    *,
    evidence: Any,
    prefix: str,
    block_id: str,
    execution_id: str,
    segment_sha256: str,
    symbol: str,
    planned_entry_regimes: list[str],
    policy_sha256: str,
    no_trade_declared: Any,
    seen_episode_ids: set[str],
    first_fill_segments: dict[str, str],
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    missing: list[str] = []
    normalized_episodes: list[dict[str, Any]] = []
    if not isinstance(evidence, Mapping):
        return [], False, [f"{prefix}.episode_ledger"]
    if evidence.get("schema_version") != EPISODE_SCHEMA_VERSION:
        missing.append(f"{prefix}.episode_ledger.schema_version")
    if evidence.get("segment_identity_sha256") != segment_sha256:
        missing.append(f"{prefix}.episode_ledger.segment_identity_sha256")
    evidence_policy = evidence.get("execution_policy_identity")
    if (
        not isinstance(evidence_policy, Mapping)
        or evidence_policy.get("sha256") != policy_sha256
    ):
        missing.append(f"{prefix}.episode_ledger.policy_identity")
    episodes = evidence.get("episodes")
    if not isinstance(episodes, list):
        missing.append(f"{prefix}.episode_ledger.episodes")
        episodes = []
    episode_count = evidence.get("episode_count")
    complete_count = evidence.get("complete_episode_count")
    terminal = evidence.get("terminal_settlement")
    terminal_complete = (
        isinstance(terminal, Mapping)
        and terminal.get("done_count") == 1
        and terminal.get("failed_count") == 0
        and terminal.get("position_count") == 0
        and terminal.get("segment_identity_sha256") == segment_sha256
        and all(
            _is_finite_number(terminal.get(field))
            for field in ("realized_net_usd", "fees_usd", "funding_paid_usd")
        )
    )
    if not terminal_complete:
        missing.append(f"{prefix}.episode_ledger.terminal_settlement")
    planned_cells = {
        (symbol, entry_regime) for entry_regime in planned_entry_regimes
    }
    if episodes:
        if episode_count != len(episodes):
            missing.append(f"{prefix}.episode_ledger.episode_count")
        if complete_count != len(episodes):
            missing.append(f"{prefix}.episode_ledger.complete_episode_count")
        if evidence.get("execution_path_complete") is not True:
            missing.append(f"{prefix}.episode_ledger.execution_path_complete")
        if evidence.get("aggregate_only_rejected") is not False:
            missing.append(f"{prefix}.episode_ledger.aggregate_only_rejected")
        if evidence.get("missing_path_evidence") != []:
            missing.append(f"{prefix}.episode_ledger.missing_path_evidence")
        if no_trade_declared is not False:
            missing.append(f"{prefix}.no_trade_zero_utility=false")
        for episode_index, episode in enumerate(episodes):
            episode_prefix = f"{prefix}.episodes[{episode_index}]"
            if isinstance(episode, Mapping):
                if episode.get("symbol") != symbol:
                    missing.append(f"{episode_prefix}.execution_symbol")
                if episode.get("entry_regime") not in planned_entry_regimes:
                    missing.append(f"{episode_prefix}.planned_entry_regimes")
            raw_episode_id = (
                str(episode.get("evaluator_episode_id") or "")
                if isinstance(episode, Mapping)
                else ""
            )
            duplicate_episode_id = bool(
                raw_episode_id and raw_episode_id in seen_episode_ids
            )
            if duplicate_episode_id:
                missing.append(f"{prefix}.episode_id_duplicate:{raw_episode_id}")
            elif raw_episode_id:
                seen_episode_ids.add(raw_episode_id)
            normalized, episode_missing = _validate_episode(
                episode,
                prefix=episode_prefix,
                block_id=block_id,
                segment_sha256=segment_sha256,
                policy_sha256=policy_sha256,
                planned_cells=planned_cells,
            )
            missing.extend(episode_missing)
            if normalized is None or duplicate_episode_id:
                continue
            if normalized["symbol"] != symbol:
                missing.append(f"{episode_prefix}.execution_symbol")
                continue
            if normalized["entry_regime"] not in planned_entry_regimes:
                missing.append(f"{episode_prefix}.planned_entry_regimes")
                continue
            first_fill_id = str(normalized["first_fill_id"])
            previous_segment = first_fill_segments.get(first_fill_id)
            if previous_segment is not None and previous_segment != segment_sha256:
                missing.append(
                    f"{prefix}.first_fill_id_cross_segment_reuse:{first_fill_id}"
                )
            else:
                first_fill_segments[first_fill_id] = segment_sha256
            if isinstance(terminal, Mapping) and normalized.get(
                "terminal_settlement"
            ) != terminal:
                missing.append(f"{episode_prefix}.terminal_settlement_identity")
            normalized["execution_id"] = execution_id
            normalized_episodes.append(normalized)
        if terminal_complete and len(normalized_episodes) == len(episodes):
            expected_terminal = {
                "realized_net_usd": sum(
                    float(episode["executable_net_utility"])
                    for episode in normalized_episodes
                ),
                "fees_usd": sum(
                    float(episode["fee_usd"])
                    for episode in normalized_episodes
                ),
                "funding_paid_usd": sum(
                    float(episode["funding_paid_usd"])
                    for episode in normalized_episodes
                ),
            }
            for field, expected_value in expected_terminal.items():
                if not math.isclose(
                    float(terminal[field]),
                    expected_value,
                    rel_tol=0.0,
                    abs_tol=EPISODE_EVIDENCE_EPSILON,
                ):
                    missing.append(f"{prefix}.episode_ledger.terminal_{field}")
        return normalized_episodes, False, _missing(missing)

    aggregate_pollution = sorted(ZERO_TRADE_FORBIDDEN_FIELDS & set(evidence))
    if aggregate_pollution:
        missing.append(
            f"{prefix}.episode_ledger.aggregate_pollution:"
            + ",".join(aggregate_pollution)
        )
    zero_terminal_complete = terminal_complete and all(
        math.isclose(
            float(terminal[field]),
            0.0,
            rel_tol=0.0,
            abs_tol=EPISODE_EVIDENCE_EPSILON,
        )
        for field in ("realized_net_usd", "fees_usd", "funding_paid_usd")
    )
    if terminal_complete and not zero_terminal_complete:
        missing.append(f"{prefix}.episode_ledger.terminal_nonzero")
    zero_trade = (
        episode_count == 0
        and complete_count == 0
        and evidence.get("execution_path_complete") is False
        and evidence.get("aggregate_only_rejected") is True
        and evidence.get("missing_path_evidence") == ["fills"]
        and no_trade_declared is True
        and zero_terminal_complete
        and not aggregate_pollution
    )
    if not zero_trade:
        missing.append(f"{prefix}.episode_ledger.zero_trade_contract")
    return [], zero_trade, _missing(missing)


def _audit_multi_execution_arm(
    *,
    arm_name: str,
    raw_arm: Any,
    expected_enabled: bool,
    expected_blocks: list[dict[str, Any]],
    plan_by_id: dict[str, dict[str, Any]],
    common_policy: dict[str, Any],
    trade_bot_sha256: str,
    initial_weights_sha256: str,
    initial_state_sha256: str,
) -> tuple[
    dict[str, Any],
    dict[tuple[str, str, str], tuple[float, int]],
    list[str],
]:
    prefix = f"paired.arms.{arm_name}"
    expected_block_ids = [block["block_id"] for block in expected_blocks]
    expected_execution_count = sum(
        len(block.get("executions", [])) for block in expected_blocks
    )
    base = {
        "status": "UNVERIFIABLE",
        "expected_block_ids": expected_block_ids,
        "executed_block_ids": [],
        "coverage_ratio": 0.0,
        "expected_execution_count": expected_execution_count,
        "verified_execution_count": 0,
        "execution_coverage_ratio": 0.0,
        "episodes": [],
        "zero_trade_block_ids": [],
        "zero_trade_execution_ids": [],
        "blocks": [],
        "block_audit": [],
        "missing_evidence": [],
    }
    if not isinstance(raw_arm, Mapping):
        base["missing_evidence"] = [prefix]
        return base, {}, base["missing_evidence"]
    missing: list[str] = []
    if raw_arm.get("infrastructure_status") != "VERIFIED":
        missing.append(f"{prefix}.infrastructure_status=VERIFIED")
    if raw_arm.get("mismatches") != []:
        missing.append(f"{prefix}.mismatches=empty")
    business_status = raw_arm.get("business_gate_status")
    report_identity = raw_arm.get("report")
    if not isinstance(report_identity, Mapping):
        missing.append(f"{prefix}.report")
    else:
        if report_identity.get("schema_version") != "exact_replay_block_audit_v1":
            missing.append(f"{prefix}.report.schema_version")
        if not _is_sha256(report_identity.get("sha256")):
            missing.append(f"{prefix}.report.sha256")
        expected_report_status = (
            (0, "VERIFIED") if business_status == "PASSED" else (2, "UNVERIFIABLE")
        )
        if business_status not in {"PASSED", "FAILED"}:
            missing.append(f"{prefix}.business_gate_status")
        elif (
            raw_arm.get("exit_code") != expected_report_status[0]
            or report_identity.get("status") != expected_report_status[1]
        ):
            missing.append(f"{prefix}.business_report_consistency")
    if raw_arm.get("trade_bot_sha256") not in (None, trade_bot_sha256):
        missing.append(f"{prefix}.trade_bot_sha256")
    config_identity = raw_arm.get("config")
    raw_policy_identity = (
        config_identity.get("policy") if isinstance(config_identity, Mapping) else None
    )
    arm_policy, arm_policy_sha, policy_missing = _policy_identity(
        raw_policy_identity,
        prefix=f"{prefix}.config.policy",
    )
    missing.extend(policy_missing)
    if not isinstance(config_identity, Mapping) or not _is_sha256(
        config_identity.get("sha256")
    ):
        missing.append(f"{prefix}.config.sha256")
    if arm_policy.get("self_evolution.enabled") is not expected_enabled:
        missing.append(
            f"{prefix}.self_evolution.enabled={str(expected_enabled).lower()}"
        )
    if {
        key: value
        for key, value in arm_policy.items()
        if key != "self_evolution.enabled"
    } != common_policy:
        missing.append(f"{prefix}.policy_difference_not_only_enabled")
    if raw_arm.get("expected_block_ids") != expected_block_ids:
        missing.append(f"{prefix}.expected_block_ids")
    if raw_arm.get("executed_block_ids") != expected_block_ids:
        missing.append(f"{prefix}.executed_block_ids")
    if raw_arm.get("block_execution_counts") != {
        block_id: 1 for block_id in expected_block_ids
    }:
        missing.append(f"{prefix}.block_execution_counts")

    raw_blocks = raw_arm.get("blocks")
    if not isinstance(raw_blocks, list):
        missing.append(f"{prefix}.blocks")
        raw_blocks = []
    raw_block_by_id: dict[str, Mapping[str, Any]] = {}
    for raw_block in raw_blocks:
        if not isinstance(raw_block, Mapping):
            missing.append(f"{prefix}.blocks.object")
            continue
        block_id = raw_block.get("block_id")
        if not _is_non_empty_string(block_id):
            missing.append(f"{prefix}.blocks.block_id")
        elif block_id in raw_block_by_id:
            missing.append(f"{prefix}.blocks.block_id=unique")
        else:
            raw_block_by_id[str(block_id)] = raw_block
    if set(raw_block_by_id) != set(expected_block_ids):
        missing.append(f"{prefix}.blocks.coverage")

    aggregation: dict[tuple[str, str, str], tuple[float, int]] = {}
    normalized_episodes: list[dict[str, Any]] = []
    zero_trade_execution_ids: list[str] = []
    verified_execution_count = 0
    verified_block_ids: list[str] = []
    block_audit: list[dict[str, Any]] = []
    seen_episode_ids: set[str] = set()
    first_fill_segments: dict[str, str] = {}
    seen_state_dirs: set[str] = set()
    assess_exit_codes: list[Any] = []
    for expected_block in expected_blocks:
        block_id = expected_block["block_id"]
        block_prefix = f"{prefix}.blocks.{block_id}"
        raw_block = raw_block_by_id.get(block_id)
        block_missing: list[str] = []
        execution_audits: list[dict[str, Any]] = []
        if raw_block is None:
            block_missing.append(block_prefix)
        else:
            for field in (
                "start_timestamp_ms",
                "end_timestamp_ms",
                "event_sha256",
                "cells",
            ):
                if raw_block.get(field) != expected_block[field]:
                    block_missing.append(f"{block_prefix}.{field}")
        plan = plan_by_id.get(block_id, {})
        expected_executions = expected_block.get("executions")
        expected_executions = (
            expected_executions if isinstance(expected_executions, list) else []
        )
        planned_by_id = plan.get("executions_by_id")
        planned_by_id = planned_by_id if isinstance(planned_by_id, Mapping) else {}
        raw_executions = raw_block.get("executions") if raw_block is not None else []
        if not isinstance(raw_executions, list):
            block_missing.append(f"{block_prefix}.executions")
            raw_executions = []
        raw_execution_by_id: dict[str, Mapping[str, Any]] = {}
        for execution_index, raw_execution in enumerate(raw_executions):
            execution_id = (
                raw_execution.get("execution_id")
                if isinstance(raw_execution, Mapping)
                else None
            )
            if not _is_non_empty_string(execution_id):
                block_missing.append(
                    f"{block_prefix}.executions[{execution_index}].execution_id"
                )
            elif execution_id in raw_execution_by_id:
                block_missing.append(
                    f"{block_prefix}.execution_id_duplicate:{execution_id}"
                )
            else:
                raw_execution_by_id[str(execution_id)] = raw_execution
        expected_execution_ids = {
            execution["execution_id"] for execution in expected_executions
        }
        actual_execution_ids = set(raw_execution_by_id)
        if actual_execution_ids != expected_execution_ids:
            block_missing.append(f"{block_prefix}.execution_coverage")
            for extra_id in sorted(actual_execution_ids - expected_execution_ids):
                block_missing.append(f"{block_prefix}.execution_extra:{extra_id}")
        for expected_execution in expected_executions:
            execution_id = expected_execution["execution_id"]
            execution_prefix = f"{block_prefix}.{execution_id}"
            execution_missing: list[str] = []
            raw_execution = raw_execution_by_id.get(execution_id)
            planned_execution = planned_by_id.get(execution_id)
            normalized_for_execution: list[dict[str, Any]] = []
            zero_trade = False
            if raw_execution is None:
                execution_missing.append(f"{execution_prefix}.missing")
            elif not isinstance(planned_execution, Mapping):
                execution_missing.append(f"{execution_prefix}.planned_identity")
            else:
                for field in (
                    "execution_id",
                    "symbol",
                    "planned_entry_regimes",
                    "event_sha256",
                    "segment_identity_sha256",
                ):
                    if raw_execution.get(field) != planned_execution.get(field):
                        execution_missing.append(f"{execution_prefix}.{field}")
                execution_policy = raw_execution.get("execution_policy_identity")
                execution_policy_payload, execution_policy_sha, execution_policy_missing = (
                    _policy_identity(
                        execution_policy,
                        prefix=f"{execution_prefix}.execution_policy_identity",
                    )
                )
                execution_missing.extend(execution_policy_missing)
                if (
                    execution_policy_payload != arm_policy
                    or execution_policy_sha != arm_policy_sha
                ):
                    execution_missing.append(
                        f"{execution_prefix}.execution_policy_identity"
                    )
                if raw_execution.get("trade_bot_sha256") != trade_bot_sha256:
                    execution_missing.append(f"{execution_prefix}.trade_bot_sha256")
                if raw_execution.get("initial_weights_sha256") != initial_weights_sha256:
                    execution_missing.append(
                        f"{execution_prefix}.initial_weights_sha256"
                    )
                if (
                    raw_execution.get("initial_evolution_state_sha256")
                    != initial_state_sha256
                ):
                    execution_missing.append(
                        f"{execution_prefix}.initial_evolution_state_sha256"
                    )
                if raw_execution.get("historical_state_loaded") is not False:
                    execution_missing.append(
                        f"{execution_prefix}.historical_state_loaded=false"
                    )
                if raw_execution.get("continued_from_block_id") not in (None, ""):
                    execution_missing.append(
                        f"{execution_prefix}.continued_from_block_id=empty"
                    )
                state_dir = raw_execution.get("state_dir")
                if not _is_non_empty_string(state_dir):
                    execution_missing.append(f"{execution_prefix}.state_dir")
                elif state_dir in seen_state_dirs:
                    execution_missing.append(f"{execution_prefix}.state_dir=unique")
                else:
                    seen_state_dirs.add(str(state_dir))
                if raw_execution.get("trade_bot_exit_code") != 0:
                    execution_missing.append(f"{execution_prefix}.trade_bot_exit_code=0")
                assess_exit = raw_execution.get("assess_exit_code")
                assess_exit_codes.append(assess_exit)
                if assess_exit not in (0, 1):
                    execution_missing.append(f"{execution_prefix}.assess_exit_code")
                normalized_for_execution, zero_trade, ledger_missing = (
                    _audit_execution_ledger(
                        evidence=raw_execution.get("episode_execution_evidence"),
                        prefix=execution_prefix,
                        block_id=block_id,
                        execution_id=execution_id,
                        segment_sha256=str(
                            planned_execution.get("segment_identity_sha256") or ""
                        ),
                        symbol=str(planned_execution.get("symbol") or ""),
                        planned_entry_regimes=list(
                            planned_execution.get("planned_entry_regimes") or []
                        ),
                        policy_sha256=arm_policy_sha,
                        no_trade_declared=raw_execution.get(
                            "no_trade_zero_utility"
                        ),
                        seen_episode_ids=seen_episode_ids,
                        first_fill_segments=first_fill_segments,
                    )
                )
                execution_missing.extend(ledger_missing)
            execution_missing = _missing(execution_missing)
            block_missing.extend(execution_missing)
            if not execution_missing:
                verified_execution_count += 1
                if zero_trade:
                    zero_trade_execution_ids.append(execution_id)
                for episode in normalized_for_execution:
                    key = (
                        block_id,
                        str(episode["symbol"]),
                        str(episode["entry_regime"]),
                    )
                    previous_utility, previous_count = aggregation.get(
                        key, (0.0, 0)
                    )
                    aggregation[key] = (
                        previous_utility
                        + float(episode["executable_net_utility"]),
                        previous_count + 1,
                    )
                normalized_episodes.extend(normalized_for_execution)
            execution_audits.append(
                {
                    "execution_id": execution_id,
                    "status": (
                        "VERIFIED" if not execution_missing else "UNVERIFIABLE"
                    ),
                    "symbol": expected_execution["symbol"],
                    "planned_entry_regimes": expected_execution[
                        "planned_entry_regimes"
                    ],
                    "event_sha256": (
                        raw_execution.get("event_sha256")
                        if isinstance(raw_execution, Mapping)
                        else None
                    ),
                    "segment_identity_sha256": (
                        raw_execution.get("segment_identity_sha256")
                        if isinstance(raw_execution, Mapping)
                        else None
                    ),
                    "episode_count": len(normalized_for_execution),
                    "zero_trade": zero_trade,
                    "missing_evidence": execution_missing,
                }
            )
        block_missing = _missing(block_missing)
        if not block_missing:
            verified_block_ids.append(block_id)
        block_audit.append(
            {
                "block_id": block_id,
                "status": "VERIFIED" if not block_missing else "UNVERIFIABLE",
                "event_sha256": (
                    raw_block.get("event_sha256")
                    if isinstance(raw_block, Mapping)
                    else None
                ),
                "episode_count": sum(
                    execution["episode_count"] for execution in execution_audits
                ),
                "execution_count": len(execution_audits),
                "executions": execution_audits,
                "missing_evidence": block_missing,
            }
        )
        missing.extend(block_missing)
    if business_status == "PASSED" and any(code != 0 for code in assess_exit_codes):
        missing.append(f"{prefix}.business_gate_status_vs_executions")
    if business_status == "FAILED" and 1 not in assess_exit_codes:
        missing.append(f"{prefix}.business_gate_status_vs_executions")
    normalized_episodes.sort(
        key=lambda episode: (
            episode["block_id"],
            episode.get("execution_id", ""),
            episode["evaluator_episode_id"],
        )
    )
    missing = _missing(missing)
    base.update(
        {
            "status": "VERIFIED" if not missing else "UNVERIFIABLE",
            "executed_block_ids": verified_block_ids,
            "coverage_ratio": (
                len(verified_block_ids) / len(expected_block_ids)
                if expected_block_ids
                else 0.0
            ),
            "verified_execution_count": verified_execution_count,
            "execution_coverage_ratio": (
                verified_execution_count / expected_execution_count
                if expected_execution_count
                else 0.0
            ),
            "episodes": normalized_episodes,
            "zero_trade_block_ids": [
                block["block_id"]
                for block in block_audit
                if block["status"] == "VERIFIED"
                and block["executions"]
                and all(execution["zero_trade"] for execution in block["executions"])
            ],
            "zero_trade_execution_ids": zero_trade_execution_ids,
            "blocks": block_audit,
            "block_audit": block_audit,
            "missing_evidence": missing,
            "policy_sha256": arm_policy_sha,
            "trade_bot_sha256": trade_bot_sha256,
        }
    )
    return base, aggregation, missing


def _aggregate_groups(
    cells: list[dict[str, Any]], *, field: str
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for cell in cells:
        key = str(cell[field])
        bucket = grouped.setdefault(
            key,
            {
                field: key,
                "cell_count": 0,
                "frozen_utility": 0.0,
                "adaptive_utility": 0.0,
                "delta": 0.0,
            },
        )
        bucket["cell_count"] += 1
        bucket["frozen_utility"] += float(cell["frozen_utility"])
        bucket["adaptive_utility"] += float(cell["adaptive_utility"])
        bucket["delta"] += float(cell["delta"])
    return [grouped[key] for key in sorted(grouped)]


def validate_evolution_uplift(
    paired_manifest: Any,
    benchmark_report: Any,
    config: Any,
    *,
    validation_config_sha256: str | None = None,
) -> dict[str, Any]:
    benchmark_verification = validate_verified_benchmark_report(
        benchmark_report,
        validation_policy=config,
        validation_config_sha256=validation_config_sha256,
    )
    expected_benchmark_id, benchmark_blocks, benchmark_missing = _benchmark_universe(
        benchmark_report
    )
    if benchmark_verification["benchmark_id"] is not None:
        expected_benchmark_id = benchmark_verification["benchmark_id"]
    thresholds, config_missing = _uplift_policy(config)
    safe_thresholds = thresholds or {
        "min_independent_blocks": None,
        "block_coverage": None,
        "bootstrap_trials": None,
        "lcb": None,
        "lcb_required": ">0",
    }
    missing = [
        *benchmark_verification["errors"],
        *benchmark_missing,
        *config_missing,
    ]
    paired = paired_manifest if isinstance(paired_manifest, Mapping) else {}
    actual_benchmark_id = paired.get("benchmark_id")
    if not isinstance(paired_manifest, Mapping):
        missing.append("paired_manifest")
    if paired.get("schema_version") != PAIRED_SCHEMA_VERSION:
        missing.append(f"paired.schema_version={PAIRED_SCHEMA_VERSION}")
    if paired.get("status") != "VERIFIED":
        missing.append("paired.status=VERIFIED")
    if paired.get("mismatches") != []:
        missing.append("paired.mismatches=empty")
    if paired.get("promotion_authority") is not False:
        missing.append("paired.promotion_authority=false")
    if paired.get("demo_activation_authorized") is not False:
        missing.append("paired.demo_activation_authorized=false")
    if paired.get("live_activation_authorized") is not False:
        missing.append("paired.live_activation_authorized=false")
    if actual_benchmark_id != expected_benchmark_id:
        missing.append("paired.benchmark_id=frozen_benchmark")

    expected_ids = [block["block_id"] for block in benchmark_blocks]
    benchmark_by_id = {block["block_id"]: block for block in benchmark_blocks}
    if thresholds is not None and len(benchmark_blocks) < thresholds["min_independent_blocks"]:
        missing.append("benchmark.minimum_independent_blocks")

    exact_plan = paired.get("exact_block_plan")
    plan_by_id: dict[str, dict[str, Any]] = {}
    multi_execution_mode = False
    if not isinstance(exact_plan, Mapping):
        missing.append("paired.exact_block_plan")
    else:
        if exact_plan.get("benchmark_id") != expected_benchmark_id:
            missing.append("paired.exact_block_plan.benchmark_id")
        if exact_plan.get("read_only") is not True:
            missing.append("paired.exact_block_plan.read_only=true")
        if not _is_sha256(exact_plan.get("sha256")):
            missing.append("paired.exact_block_plan.sha256")
        if exact_plan.get("expected_block_ids") != expected_ids:
            missing.append("paired.exact_block_plan.expected_block_ids")
        raw_plan_blocks = exact_plan.get("blocks")
        if not isinstance(raw_plan_blocks, list):
            missing.append("paired.exact_block_plan.blocks")
            raw_plan_blocks = []
        for index, raw_plan in enumerate(raw_plan_blocks):
            prefix = f"paired.exact_block_plan.blocks[{index}]"
            if not isinstance(raw_plan, Mapping):
                missing.append(prefix)
                continue
            block_id = raw_plan.get("block_id")
            if not _is_non_empty_string(block_id):
                missing.append(f"{prefix}.block_id")
                continue
            if block_id in plan_by_id:
                missing.append(f"{prefix}.block_id=unique")
                continue
            plan_by_id[str(block_id)] = dict(raw_plan)
            if isinstance(raw_plan.get("executions"), list):
                multi_execution_mode = True
        if list(plan_by_id) != expected_ids:
            missing.append("paired.exact_block_plan.blocks.coverage")
        if multi_execution_mode and exact_plan.get("schema_version") != (
            "exact_replay_block_plan_v2"
        ):
            missing.append("paired.exact_block_plan.schema_version=v2")
        for block_id in expected_ids:
            planned = plan_by_id.get(block_id)
            expected = benchmark_by_id[block_id]
            if planned is None:
                continue
            for field in (
                "start_timestamp_ms",
                "end_timestamp_ms",
                "event_sha256",
                "cells",
            ):
                if planned.get(field) != expected[field]:
                    missing.append(f"paired.exact_block_plan.{block_id}.{field}")
            if multi_execution_mode:
                expected_executions = expected.get("executions")
                expected_executions = (
                    expected_executions
                    if isinstance(expected_executions, list)
                    else []
                )
                expected_execution_by_id = {
                    item["execution_id"]: item for item in expected_executions
                }
                raw_executions = planned.get("executions")
                if not isinstance(raw_executions, list):
                    missing.append(
                        f"paired.exact_block_plan.{block_id}.executions"
                    )
                    raw_executions = []
                planned_execution_by_id: dict[str, dict[str, Any]] = {}
                for execution_index, raw_execution in enumerate(raw_executions):
                    execution_prefix = (
                        f"paired.exact_block_plan.{block_id}."
                        f"executions[{execution_index}]"
                    )
                    if not isinstance(raw_execution, Mapping):
                        missing.append(execution_prefix)
                        continue
                    execution_id = raw_execution.get("execution_id")
                    if not _is_non_empty_string(execution_id):
                        missing.append(f"{execution_prefix}.execution_id")
                        continue
                    if execution_id in planned_execution_by_id:
                        missing.append(f"{execution_prefix}.execution_id_duplicate")
                        continue
                    planned_execution_by_id[str(execution_id)] = dict(raw_execution)
                if set(planned_execution_by_id) != set(expected_execution_by_id):
                    missing.append(
                        f"paired.exact_block_plan.{block_id}.execution_coverage"
                    )
                for execution_id, expected_execution in expected_execution_by_id.items():
                    planned_execution = planned_execution_by_id.get(execution_id)
                    if planned_execution is None:
                        continue
                    for field in (
                        "execution_id",
                        "symbol",
                        "planned_entry_regimes",
                        "event_sha256",
                    ):
                        if planned_execution.get(field) != expected_execution[field]:
                            missing.append(
                                f"paired.exact_block_plan.{block_id}."
                                f"{execution_id}.{field}"
                            )
                    if (
                        planned_execution.get("start_timestamp_ms")
                        != expected["start_timestamp_ms"]
                        or planned_execution.get("end_timestamp_ms")
                        != expected["end_timestamp_ms"]
                    ):
                        missing.append(
                            f"paired.exact_block_plan.{block_id}."
                            f"{execution_id}.interval"
                        )
                    if not _is_sha256(
                        planned_execution.get("segment_identity_sha256")
                    ):
                        missing.append(
                            f"paired.exact_block_plan.{block_id}."
                            f"{execution_id}.segment_identity_sha256"
                        )
                    if not _is_non_empty_string(
                        planned_execution.get("replay_csv")
                    ):
                        missing.append(
                            f"paired.exact_block_plan.{block_id}."
                            f"{execution_id}.replay_csv"
                        )
                planned["executions_by_id"] = planned_execution_by_id
            else:
                if not _is_sha256(planned.get("segment_identity_sha256")):
                    missing.append(
                        f"paired.exact_block_plan.{block_id}.segment_identity_sha256"
                    )
                if not _is_non_empty_string(planned.get("replay_csv")):
                    missing.append(f"paired.exact_block_plan.{block_id}.replay_csv")

    trade_bot = paired.get("trade_bot")
    trade_bot_sha = trade_bot.get("sha256") if isinstance(trade_bot, Mapping) else None
    if not _is_sha256(trade_bot_sha):
        missing.append("paired.trade_bot.sha256")
        trade_bot_sha = ""

    common_identity = paired.get("common_policy")
    common_policy: dict[str, Any] = {}
    if not isinstance(common_identity, Mapping):
        missing.append("paired.common_policy")
    else:
        if common_identity.get("schema_version") != "paired_common_execution_policy_v1":
            missing.append("paired.common_policy.schema_version")
        raw_common = common_identity.get("policy")
        if not isinstance(raw_common, Mapping):
            missing.append("paired.common_policy.policy")
        else:
            common_policy = dict(raw_common)
            if common_identity.get("sha256") != canonical_sha256(common_policy):
                missing.append("paired.common_policy.sha256")
        if common_identity.get("excluded_paths") != ["self_evolution.enabled"]:
            missing.append("paired.common_policy.excluded_paths")
    if paired.get("policy_differences") != [EXPECTED_POLICY_DIFFERENCE]:
        missing.append("paired.policy_differences=only_self_evolution.enabled")

    weights = paired.get("initial_weights")
    weights_payload = weights.get("payload") if isinstance(weights, Mapping) else None
    weights_sha = weights.get("sha256") if isinstance(weights, Mapping) else None
    if not isinstance(weights_payload, Mapping) or weights_sha != canonical_sha256(
        dict(weights_payload) if isinstance(weights_payload, Mapping) else {}
    ):
        missing.append("paired.initial_weights.identity")
        weights_sha = ""
    elif (
        set(weights_payload) != {"trend", "defensive"}
        or not all(_is_finite_number(value) for value in weights_payload.values())
        or any(float(value) < 0.0 for value in weights_payload.values())
        or not math.isclose(
            sum(float(value) for value in weights_payload.values()),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        missing.append("paired.initial_weights.payload")
    elif (
        common_policy.get("self_evolution.initial_trend_weight")
        != weights_payload.get("trend")
        or common_policy.get("self_evolution.initial_defensive_weight")
        != weights_payload.get("defensive")
    ):
        missing.append("paired.initial_weights.policy_mismatch")
    state = paired.get("initial_evolution_state")
    state_payload = state.get("payload") if isinstance(state, Mapping) else None
    state_sha = state.get("sha256") if isinstance(state, Mapping) else None
    if not isinstance(state_payload, Mapping) or state_sha != canonical_sha256(
        dict(state_payload) if isinstance(state_payload, Mapping) else {}
    ):
        missing.append("paired.initial_evolution_state.identity")
        state_sha = ""
    if not isinstance(state, Mapping) or state.get("empty") is not True:
        missing.append("paired.initial_evolution_state.empty=true")
    if state_payload != {
        "schema_version": "empty_evolution_state_v1",
        "records": [],
    }:
        missing.append("paired.initial_evolution_state.payload=empty_v1")
    if isinstance(state, Mapping) and (
        state.get("historical_state_loading_allowed") is not False
        or state.get("cross_block_continuation_allowed") is not False
    ):
        missing.append("paired.initial_evolution_state.no_history_or_continuation")

    raw_arms = paired.get("arms")
    raw_arms = raw_arms if isinstance(raw_arms, Mapping) else {}
    if not isinstance(paired.get("arms"), Mapping):
        missing.append("paired.arms")
    arm_reports: dict[str, dict[str, Any]] = {}
    arm_aggregations: dict[
        str, dict[tuple[str, str, str], tuple[float, int]]
    ] = {}
    for arm_name, enabled in (("frozen", False), ("adaptive", True)):
        audit = _audit_multi_execution_arm if multi_execution_mode else _audit_arm
        arm_report, aggregation, arm_missing = audit(
            arm_name=arm_name,
            raw_arm=raw_arms.get(arm_name),
            expected_enabled=enabled,
            expected_blocks=benchmark_blocks,
            plan_by_id=plan_by_id,
            common_policy=common_policy,
            trade_bot_sha256=str(trade_bot_sha or ""),
            initial_weights_sha256=str(weights_sha or ""),
            initial_state_sha256=str(state_sha or ""),
        )
        arm_reports[arm_name] = arm_report
        arm_aggregations[arm_name] = aggregation
        missing.extend(arm_missing)

    aggregation_cells: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    complete_blocks_by_arm = {
        arm_name: {
            block["block_id"]
            for block in arm_reports.get(arm_name, {}).get("block_audit", [])
            if isinstance(block, Mapping) and block.get("status") == "VERIFIED"
        }
        for arm_name in ("frozen", "adaptive")
    }
    for block in benchmark_blocks:
        block_cells: list[dict[str, Any]] = []
        for cell in block["cells"]:
            key = (block["block_id"], cell["symbol"], cell["entry_regime"])
            frozen_utility, frozen_count = arm_aggregations.get("frozen", {}).get(
                key, (0.0, 0)
            )
            adaptive_utility, adaptive_count = arm_aggregations.get(
                "adaptive", {}
            ).get(key, (0.0, 0))
            row = {
                "block_id": block["block_id"],
                "symbol": cell["symbol"],
                "entry_regime": cell["entry_regime"],
                "event_sha256": block["event_sha256"],
                "frozen_episode_count": frozen_count,
                "adaptive_episode_count": adaptive_count,
                "frozen_utility": frozen_utility,
                "adaptive_utility": adaptive_utility,
                "delta": adaptive_utility - frozen_utility,
            }
            block_cells.append(row)
            aggregation_cells.append(row)
        frozen_total = sum(float(cell["frozen_utility"]) for cell in block_cells)
        adaptive_total = sum(float(cell["adaptive_utility"]) for cell in block_cells)
        block_evidence_complete = all(
            block["block_id"] in complete_blocks_by_arm[arm_name]
            for arm_name in ("frozen", "adaptive")
        )
        block_rows.append(
            {
                "block_id": block["block_id"],
                "event_sha256": block["event_sha256"],
                "cell_count": len(block_cells),
                "evidence_complete": block_evidence_complete,
                "frozen_utility": (
                    frozen_total if block_evidence_complete else None
                ),
                "adaptive_utility": (
                    adaptive_total if block_evidence_complete else None
                ),
                "delta": (
                    adaptive_total - frozen_total
                    if block_evidence_complete
                    else None
                ),
                "cells": block_cells,
            }
        )

    expected_count = len(expected_ids)
    frozen_ratio = arm_reports.get("frozen", {}).get("coverage_ratio", 0.0)
    adaptive_ratio = arm_reports.get("adaptive", {}).get("coverage_ratio", 0.0)
    required_coverage = (
        float(thresholds["block_coverage"]) if thresholds is not None else None
    )
    if thresholds is not None and (
        frozen_ratio < thresholds["block_coverage"]
        or adaptive_ratio < thresholds["block_coverage"]
    ):
        missing.append("paired.block_coverage=100_percent")
    expected_execution_count = (
        sum(len(block.get("executions", [])) for block in benchmark_blocks)
        if multi_execution_mode
        else len(benchmark_blocks)
    )
    frozen_execution_ratio = arm_reports.get("frozen", {}).get(
        "execution_coverage_ratio", 0.0
    )
    adaptive_execution_ratio = arm_reports.get("adaptive", {}).get(
        "execution_coverage_ratio", 0.0
    )
    if frozen_execution_ratio < 1.0 or adaptive_execution_ratio < 1.0:
        missing.append("paired.execution_coverage=100_percent")

    missing = _missing(missing)
    bootstrap: dict[str, Any] | None = None
    if (
        not missing
        and thresholds is not None
        and expected_benchmark_id is not None
        and block_rows
    ):
        statistics = block_bootstrap_statistics(
            [float(row["delta"]) for row in block_rows],
            benchmark_id=expected_benchmark_id,
            trials=int(thresholds["bootstrap_trials"]),
        )
        lcb, lcb_index = lower_confidence_bound(
            statistics,
            confidence=float(thresholds["lcb"]),
        )
        bootstrap = {
            "method": "deterministic_sha256_block_resampling_v1",
            "seed_source": "benchmark_id+uplift+trial+draw",
            "sampling_unit": "whole_block_with_all_planned_cells",
            "replacement": True,
            "sample_size_blocks": len(block_rows),
            "trials": len(statistics),
            "confidence": float(thresholds["lcb"]),
            "lcb_index": lcb_index,
            "lower_confidence_bound": lcb,
            "minimum": min(statistics),
            "maximum": max(statistics),
            "mean": sum(statistics) / len(statistics),
            "distribution_sha256": canonical_sha256(statistics),
        }

    if missing:
        status = "UNVERIFIABLE"
    elif bootstrap is not None and bootstrap["lower_confidence_bound"] > 0.0:
        status = "UPLIFT_PROVEN"
    else:
        status = "NOT_PROVEN"
    complete_block_rows = [
        row for row in block_rows if row["evidence_complete"] is True
    ]
    total_frozen = sum(float(row["frozen_utility"]) for row in complete_block_rows)
    total_adaptive = sum(float(row["adaptive_utility"]) for row in complete_block_rows)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "benchmark_id": expected_benchmark_id,
        "expected_benchmark_id": expected_benchmark_id,
        "actual_benchmark_id": actual_benchmark_id,
        "benchmark_verification": benchmark_verification,
        "thresholds": safe_thresholds,
        "identity_audit": {
            "paired_manifest_schema_version": paired.get("schema_version"),
            "paired_manifest_status": paired.get("status"),
            "common_policy_sha256": (
                paired.get("common_policy", {}).get("sha256")
                if isinstance(paired.get("common_policy"), Mapping)
                else None
            ),
            "initial_weights_sha256": weights_sha,
            "initial_evolution_state_sha256": state_sha,
            "trade_bot_sha256": trade_bot_sha,
            "policy_differences": paired.get("policy_differences"),
        },
        "block_coverage": {
            "expected_block_count": expected_count,
            "expected_block_ids": expected_ids,
            "required_ratio": required_coverage,
            "frozen_ratio": frozen_ratio,
            "adaptive_ratio": adaptive_ratio,
            "frozen_executed_block_ids": arm_reports.get("frozen", {}).get(
                "executed_block_ids", []
            ),
            "adaptive_executed_block_ids": arm_reports.get("adaptive", {}).get(
                "executed_block_ids", []
            ),
        },
        "execution_coverage": {
            "expected_execution_count": expected_execution_count,
            "required_ratio": 1.0,
            "frozen_verified_execution_count": arm_reports.get("frozen", {}).get(
                "verified_execution_count", 0
            ),
            "adaptive_verified_execution_count": arm_reports.get(
                "adaptive", {}
            ).get("verified_execution_count", 0),
            "frozen_ratio": frozen_execution_ratio,
            "adaptive_ratio": adaptive_execution_ratio,
        },
        "arms": arm_reports,
        "aggregation_cells": aggregation_cells,
        "blocks": block_rows,
        "assets": _aggregate_groups(aggregation_cells, field="symbol"),
        "entry_regimes": _aggregate_groups(
            aggregation_cells, field="entry_regime"
        ),
        "overall": {
            "frozen_utility": total_frozen,
            "adaptive_utility": total_adaptive,
            "delta": total_adaptive - total_frozen,
            "mean_block_delta": (
                sum(float(row["delta"]) for row in complete_block_rows)
                / len(complete_block_rows)
                if complete_block_rows
                else None
            ),
        },
        "bootstrap": bootstrap,
        "missing_evidence": missing,
    }


def validate_evolution_uplift_report_artifact(
    report: Any,
    benchmark_report: Any,
    validation_policy: Any,
    *,
    validation_config_sha256: str | None,
) -> dict[str, Any]:
    """Verify the self-contained arm/block/cell/bootstrap derivation audit."""

    errors: list[str] = []
    benchmark_verification = validate_verified_benchmark_report(
        benchmark_report,
        validation_policy=validation_policy,
        validation_config_sha256=validation_config_sha256,
    )
    errors.extend(benchmark_verification["errors"])
    benchmark_id, benchmark_blocks, benchmark_missing = _benchmark_universe(
        benchmark_report
    )
    errors.extend(benchmark_missing)
    thresholds, policy_missing = _uplift_policy(validation_policy)
    errors.extend(policy_missing)
    if not isinstance(report, Mapping):
        errors.append("uplift_report")
        return {"verified": False, "status": None, "errors": _missing(errors)}

    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        errors.append(f"uplift.schema_version={REPORT_SCHEMA_VERSION}")
    source_status = report.get("status")
    if source_status not in {"UPLIFT_PROVEN", "NOT_PROVEN"}:
        errors.append("uplift.status=complete_evidence")
    for authority_field in (
        "promotion_authority",
        "demo_activation_authorized",
        "live_activation_authorized",
    ):
        if report.get(authority_field) is not False:
            errors.append(f"uplift.{authority_field}=false")
    expected_benchmark_id = benchmark_verification["benchmark_id"] or benchmark_id
    for field in ("benchmark_id", "expected_benchmark_id", "actual_benchmark_id"):
        if report.get(field) != expected_benchmark_id:
            errors.append(f"uplift.{field}=frozen_benchmark")
    if report.get("missing_evidence") != []:
        errors.append("uplift.missing_evidence=empty")
    if thresholds is None or report.get("thresholds") != thresholds:
        errors.append("uplift.thresholds=frozen_policy")

    expected_by_id = {block["block_id"]: block for block in benchmark_blocks}
    expected_ids = [block["block_id"] for block in benchmark_blocks]
    expected_cells = {
        (block["block_id"], cell["symbol"], cell["entry_regime"]): block
        for block in benchmark_blocks
        for cell in block["cells"]
    }
    multi_execution = any(block.get("executions") for block in benchmark_blocks)
    expected_execution_count = (
        sum(len(block.get("executions", [])) for block in benchmark_blocks)
        if multi_execution
        else len(benchmark_blocks)
    )

    identity_audit = report.get("identity_audit")
    if not isinstance(identity_audit, Mapping):
        errors.append("uplift.identity_audit")
    else:
        if identity_audit.get("paired_manifest_schema_version") != PAIRED_SCHEMA_VERSION:
            errors.append("uplift.identity_audit.paired_manifest_schema_version")
        if identity_audit.get("paired_manifest_status") != "VERIFIED":
            errors.append("uplift.identity_audit.paired_manifest_status")
        for field in (
            "common_policy_sha256",
            "initial_weights_sha256",
            "initial_evolution_state_sha256",
            "trade_bot_sha256",
        ):
            if not _is_sha256(identity_audit.get(field)):
                errors.append(f"uplift.identity_audit.{field}")
        if identity_audit.get("policy_differences") != [EXPECTED_POLICY_DIFFERENCE]:
            errors.append("uplift.identity_audit.policy_differences")

    block_coverage = report.get("block_coverage")
    if not isinstance(block_coverage, Mapping):
        errors.append("uplift.block_coverage")
    else:
        expected_coverage = {
            "expected_block_count": len(expected_ids),
            "expected_block_ids": expected_ids,
            "required_ratio": 1.0,
            "frozen_ratio": 1.0,
            "adaptive_ratio": 1.0,
            "frozen_executed_block_ids": expected_ids,
            "adaptive_executed_block_ids": expected_ids,
        }
        if dict(block_coverage) != expected_coverage:
            errors.append("uplift.block_coverage=complete_frozen_blocks")

    execution_coverage = report.get("execution_coverage")
    if not isinstance(execution_coverage, Mapping):
        errors.append("uplift.execution_coverage")
    else:
        expected_execution_coverage = {
            "expected_execution_count": expected_execution_count,
            "required_ratio": 1.0,
            "frozen_verified_execution_count": expected_execution_count,
            "adaptive_verified_execution_count": expected_execution_count,
            "frozen_ratio": 1.0,
            "adaptive_ratio": 1.0,
        }
        if dict(execution_coverage) != expected_execution_coverage:
            errors.append("uplift.execution_coverage=complete_executions")

    arm_utility: dict[str, dict[tuple[str, str, str], tuple[float, int]]] = {}
    arms = report.get("arms")
    if not isinstance(arms, Mapping) or set(arms) != {"frozen", "adaptive"}:
        errors.append("uplift.arms=frozen_and_adaptive")
        arms = {}
    for arm_name in ("frozen", "adaptive"):
        arm = arms.get(arm_name)
        prefix = f"uplift.arms.{arm_name}"
        aggregation: dict[tuple[str, str, str], list[float | int]] = {}
        arm_utility[arm_name] = {}
        if not isinstance(arm, Mapping):
            errors.append(prefix)
            continue
        if arm.get("status") != "VERIFIED" or arm.get("missing_evidence") != []:
            errors.append(f"{prefix}.status=VERIFIED")
        for field in ("expected_block_ids", "executed_block_ids"):
            if arm.get(field) != expected_ids:
                errors.append(f"{prefix}.{field}")
        if arm.get("coverage_ratio") != 1.0:
            errors.append(f"{prefix}.coverage_ratio")
        if arm.get("expected_execution_count") != expected_execution_count:
            errors.append(f"{prefix}.expected_execution_count")
        if arm.get("verified_execution_count") != expected_execution_count:
            errors.append(f"{prefix}.verified_execution_count")
        if arm.get("execution_coverage_ratio") != 1.0:
            errors.append(f"{prefix}.execution_coverage_ratio")
        if not _is_sha256(arm.get("policy_sha256")) or not _is_sha256(
            arm.get("trade_bot_sha256")
        ):
            errors.append(f"{prefix}.content_identity")

        episodes = arm.get("episodes")
        if not isinstance(episodes, list):
            errors.append(f"{prefix}.episodes")
            episodes = []
        seen_episode_ids: set[str] = set()
        episode_count_by_block: dict[str, int] = {}
        episode_count_by_segment: dict[str, int] = {}
        episode_segments_by_block: dict[str, set[str]] = {}
        for episode_index, episode in enumerate(episodes):
            episode_prefix = f"{prefix}.episodes[{episode_index}]"
            if not isinstance(episode, Mapping):
                errors.append(episode_prefix)
                continue
            episode_id = episode.get("evaluator_episode_id")
            if not _is_sha256(episode_id) or episode_id in seen_episode_ids:
                errors.append(f"{episode_prefix}.evaluator_episode_id")
            else:
                seen_episode_ids.add(str(episode_id))
            key = (
                episode.get("block_id"),
                episode.get("symbol"),
                episode.get("entry_regime"),
            )
            if key not in expected_cells:
                errors.append(f"{episode_prefix}.planned_cell")
                continue
            block_id = str(episode["block_id"])
            expected_block = expected_by_id[block_id]
            normalized_episode, episode_missing = _validate_episode(
                episode,
                prefix=episode_prefix,
                block_id=block_id,
                segment_sha256=str(episode.get("segment_identity_sha256") or ""),
                policy_sha256=str(arm.get("policy_sha256") or ""),
                planned_cells={
                    (cell["symbol"], cell["entry_regime"])
                    for cell in expected_block["cells"]
                },
            )
            if episode_missing or normalized_episode is None:
                errors.extend(episode_missing or [f"{episode_prefix}.complete_execution_evidence"])
                continue
            bucket = aggregation.setdefault(key, [0.0, 0])
            bucket[0] = float(bucket[0]) + float(
                normalized_episode["executable_net_utility"]
            )
            bucket[1] = int(bucket[1]) + 1
            episode_count_by_block[block_id] = episode_count_by_block.get(block_id, 0) + 1
            segment = normalized_episode.get("segment_identity_sha256")
            if _is_sha256(segment):
                episode_count_by_segment[str(segment)] = (
                    episode_count_by_segment.get(str(segment), 0) + 1
                )
                episode_segments_by_block.setdefault(block_id, set()).add(str(segment))

        arm_blocks = arm.get("block_audit")
        if not isinstance(arm_blocks, list) or len(arm_blocks) != len(expected_ids):
            errors.append(f"{prefix}.block_audit")
            arm_blocks = []
        if arm.get("blocks") != arm_blocks:
            errors.append(f"{prefix}.blocks_vs_block_audit")
        observed_block_ids: list[str] = []
        for block_index, block_audit in enumerate(arm_blocks):
            block_prefix = f"{prefix}.block_audit[{block_index}]"
            if not isinstance(block_audit, Mapping):
                errors.append(block_prefix)
                continue
            block_id = block_audit.get("block_id")
            expected_block = expected_by_id.get(block_id)
            observed_block_ids.append(str(block_id))
            if expected_block is None:
                errors.append(f"{block_prefix}.block_id")
                continue
            if (
                block_audit.get("status") != "VERIFIED"
                or block_audit.get("missing_evidence") != []
                or block_audit.get("event_sha256") != expected_block["event_sha256"]
                or block_audit.get("episode_count")
                != episode_count_by_block.get(str(block_id), 0)
            ):
                errors.append(f"{block_prefix}.verified_evidence")
            expected_executions = expected_block.get("executions", [])
            if expected_executions:
                raw_executions = block_audit.get("executions")
                if not isinstance(raw_executions, list) or len(raw_executions) != len(
                    expected_executions
                ):
                    errors.append(f"{block_prefix}.executions")
                    continue
                expected_execution_by_id = {
                    item["execution_id"]: item for item in expected_executions
                }
                if block_audit.get("execution_count") != len(expected_executions):
                    errors.append(f"{block_prefix}.execution_count")
                for execution_index, execution in enumerate(raw_executions):
                    execution_prefix = f"{block_prefix}.executions[{execution_index}]"
                    if not isinstance(execution, Mapping):
                        errors.append(execution_prefix)
                        continue
                    expected_execution = expected_execution_by_id.get(
                        execution.get("execution_id")
                    )
                    if expected_execution is None:
                        errors.append(f"{execution_prefix}.execution_id")
                        continue
                    if (
                        execution.get("status") != "VERIFIED"
                        or execution.get("missing_evidence") != []
                        or execution.get("symbol") != expected_execution["symbol"]
                        or execution.get("planned_entry_regimes")
                        != expected_execution["planned_entry_regimes"]
                        or execution.get("event_sha256")
                        != expected_execution["event_sha256"]
                        or not _is_sha256(execution.get("segment_identity_sha256"))
                        or execution.get("episode_count")
                        != episode_count_by_segment.get(
                            str(execution.get("segment_identity_sha256")), 0
                        )
                        or execution.get("zero_trade")
                        != (execution.get("episode_count") == 0)
                    ):
                        errors.append(f"{execution_prefix}.verified_evidence")
            else:
                block_segment = block_audit.get("segment_identity_sha256")
                if not _is_sha256(block_segment):
                    errors.append(f"{block_prefix}.segment_identity_sha256")
                elif episode_count_by_block.get(str(block_id), 0) > 0 and (
                    episode_segments_by_block.get(str(block_id), set())
                    != {str(block_segment)}
                ):
                    errors.append(f"{block_prefix}.episode_segment_identity")
                if block_audit.get("zero_trade") != (
                    block_audit.get("episode_count") == 0
                ):
                    errors.append(f"{block_prefix}.zero_trade")
        if observed_block_ids != expected_ids:
            errors.append(f"{prefix}.block_audit.coverage")
        arm_utility[arm_name] = {
            key: (float(value[0]), int(value[1])) for key, value in aggregation.items()
        }

    raw_cells = report.get("aggregation_cells")
    cell_by_key: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    if not isinstance(raw_cells, list) or len(raw_cells) != len(expected_cells):
        errors.append("uplift.aggregation_cells")
        raw_cells = []
    for cell_index, cell in enumerate(raw_cells):
        prefix = f"uplift.aggregation_cells[{cell_index}]"
        if not isinstance(cell, Mapping):
            errors.append(prefix)
            continue
        key = (cell.get("block_id"), cell.get("symbol"), cell.get("entry_regime"))
        expected_block = expected_cells.get(key)
        if expected_block is None or key in cell_by_key:
            errors.append(f"{prefix}.planned_cell_unique")
            continue
        cell_by_key[key] = cell
        frozen_utility, frozen_count = arm_utility.get("frozen", {}).get(
            key, (0.0, 0)
        )
        adaptive_utility, adaptive_count = arm_utility.get("adaptive", {}).get(
            key, (0.0, 0)
        )
        if (
            cell.get("event_sha256") != expected_block["event_sha256"]
            or cell.get("frozen_episode_count") != frozen_count
            or cell.get("adaptive_episode_count") != adaptive_count
            or cell.get("frozen_utility") != frozen_utility
            or cell.get("adaptive_utility") != adaptive_utility
            or cell.get("delta") != adaptive_utility - frozen_utility
        ):
            errors.append(f"{prefix}.derived_utility")
    if set(cell_by_key) != set(expected_cells):
        errors.append("uplift.aggregation_cells.coverage")

    raw_blocks = report.get("blocks")
    derived_block_deltas: list[float] = []
    if not isinstance(raw_blocks, list) or len(raw_blocks) != len(expected_ids):
        errors.append("uplift.blocks")
        raw_blocks = []
    for block_index, block in enumerate(raw_blocks):
        prefix = f"uplift.blocks[{block_index}]"
        if not isinstance(block, Mapping):
            errors.append(prefix)
            continue
        block_id = block.get("block_id")
        expected_block = expected_by_id.get(block_id)
        planned_cells = [
            cell_by_key[(block_id, cell["symbol"], cell["entry_regime"])]
            for cell in expected_block["cells"]
            if expected_block is not None
            and (block_id, cell["symbol"], cell["entry_regime"]) in cell_by_key
        ] if expected_block is not None else []
        frozen_total = sum(float(cell["frozen_utility"]) for cell in planned_cells)
        adaptive_total = sum(float(cell["adaptive_utility"]) for cell in planned_cells)
        delta = adaptive_total - frozen_total
        if (
            expected_block is None
            or block.get("event_sha256") != expected_block["event_sha256"]
            or block.get("cell_count") != len(expected_block["cells"])
            or block.get("evidence_complete") is not True
            or block.get("cells") != planned_cells
            or block.get("frozen_utility") != frozen_total
            or block.get("adaptive_utility") != adaptive_total
            or block.get("delta") != delta
        ):
            errors.append(f"{prefix}.derived_block")
        else:
            derived_block_deltas.append(delta)
    if [block.get("block_id") for block in raw_blocks if isinstance(block, Mapping)] != expected_ids:
        errors.append("uplift.blocks.coverage")

    if (
        expected_ids
        and _is_sha256(expected_benchmark_id)
        and len(derived_block_deltas) == len(expected_ids)
        and thresholds is not None
    ):
        statistics = block_bootstrap_statistics(
            derived_block_deltas,
            benchmark_id=str(expected_benchmark_id),
            trials=int(thresholds["bootstrap_trials"]),
        )
        lcb, lcb_index = lower_confidence_bound(
            statistics, confidence=float(thresholds["lcb"])
        )
        expected_bootstrap = {
            "method": "deterministic_sha256_block_resampling_v1",
            "seed_source": "benchmark_id+uplift+trial+draw",
            "sampling_unit": "whole_block_with_all_planned_cells",
            "replacement": True,
            "sample_size_blocks": len(expected_ids),
            "trials": len(statistics),
            "confidence": float(thresholds["lcb"]),
            "lcb_index": lcb_index,
            "lower_confidence_bound": lcb,
            "minimum": min(statistics),
            "maximum": max(statistics),
            "mean": sum(statistics) / len(statistics),
            "distribution_sha256": canonical_sha256(statistics),
        }
        if report.get("bootstrap") != expected_bootstrap:
            errors.append("uplift.bootstrap=derived_block_distribution")
        expected_status = "UPLIFT_PROVEN" if lcb > 0.0 else "NOT_PROVEN"
        if source_status != expected_status:
            errors.append("uplift.status=derived_bootstrap_lcb")
    else:
        errors.append("uplift.bootstrap_evidence_incomplete")

    if len(cell_by_key) == len(expected_cells):
        cells = [cell_by_key[key] for key in sorted(cell_by_key)]
        if report.get("assets") != _aggregate_groups(cells, field="symbol"):
            errors.append("uplift.assets=derived_cells")
        if report.get("entry_regimes") != _aggregate_groups(
            cells, field="entry_regime"
        ):
            errors.append("uplift.entry_regimes=derived_cells")
        complete_blocks = [
            block for block in raw_blocks
            if isinstance(block, Mapping) and block.get("evidence_complete") is True
        ]
        overall = {
            "frozen_utility": sum(float(block["frozen_utility"]) for block in complete_blocks),
            "adaptive_utility": sum(float(block["adaptive_utility"]) for block in complete_blocks),
        }
        overall["delta"] = overall["adaptive_utility"] - overall["frozen_utility"]
        overall["mean_block_delta"] = (
            sum(float(block["delta"]) for block in complete_blocks)
            / len(complete_blocks)
            if complete_blocks
            else None
        )
        if report.get("overall") != overall:
            errors.append("uplift.overall=derived_blocks")

    return {
        "verified": not errors,
        "status": source_status,
        "errors": _missing(errors),
    }


def _read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_optional(path: pathlib.Path) -> Any:
    try:
        return _read_json(path)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"read_error": f"{type(exc).__name__}:{exc}"}


def _write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(
            payload,
            handle,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        handle.write("\n")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paired-manifest", required=True)
    parser.add_argument("--benchmark-report", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        policy_sha256 = file_sha256(pathlib.Path(args.config))
    except OSError:
        policy_sha256 = None
    report = validate_evolution_uplift(
        _read_optional(pathlib.Path(args.paired_manifest)),
        _read_optional(pathlib.Path(args.benchmark_report)),
        _read_optional(pathlib.Path(args.config)),
        validation_config_sha256=policy_sha256,
    )
    _write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    if report["status"] == "UPLIFT_PROVEN":
        return 0
    return 2 if report["status"] == "UNVERIFIABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
