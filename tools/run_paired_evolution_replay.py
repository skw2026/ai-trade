#!/usr/bin/env python3
"""Run a fail-closed frozen/adaptive replay pair on one exact block plan."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from build_replay_candidate_config import derive_candidate_config
    from config_policy_contract import policy_payload, policy_sha256
    from run_replay_validation import (
        ReplaySegment,
        load_feature_rows,
        replay_segment_identity,
        write_replay_csv,
    )
except ModuleNotFoundError:  # pragma: no cover - package import in tests
    from tools.build_replay_candidate_config import derive_candidate_config
    from tools.config_policy_contract import policy_payload, policy_sha256
    from tools.run_replay_validation import (
        ReplaySegment,
        load_feature_rows,
        replay_segment_identity,
        write_replay_csv,
    )


SCHEMA_VERSION = "paired_evolution_replay_v1"
BENCHMARK_REPORT_SCHEMA = "decision_evidence_benchmark_validation_v1"
EXACT_PLAN_SCHEMA = "exact_replay_block_plan_v1"
EXACT_REPORT_SCHEMA = "exact_replay_block_audit_v1"
MANIFEST_FILENAME = "paired_evolution_replay_manifest.json"
ALLOWED_POLICY_DIFFERENCE = "self_evolution.enabled"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_text(path: pathlib.Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = pathlib.Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: pathlib.Path, payload: dict[str, Any], *, mode: int | None = None) -> None:
    text = json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    _atomic_write_text(path, text, mode=mode)


def _identity(path: pathlib.Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "sha256": ""}
    if path.is_file():
        try:
            result["sha256"] = file_sha256(path)
        except OSError:
            pass
    return result


def _read_json_object(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def derive_arm_config(common_text: str, *, enabled: bool) -> str:
    """Change exactly self_evolution.enabled in an already-derived config."""

    output: list[str] = []
    top_section = ""
    replaced = 0
    key_pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
    for raw_line in common_text.splitlines():
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        match = key_pattern.match(stripped) if stripped else None
        key = match.group(1) if match is not None else ""
        if indent == 0 and key:
            top_section = key
        if top_section == "self_evolution" and indent == 2 and key == "enabled":
            output.append(f"  enabled: {'true' if enabled else 'false'}")
            replaced += 1
        else:
            output.append(raw_line)
    if replaced != 1:
        raise ValueError(
            "common replay config must contain exactly one self_evolution.enabled"
        )
    return "\n".join(output) + "\n"


def _policy_differences(
    frozen_policy: dict[str, Any], adaptive_policy: dict[str, Any]
) -> list[dict[str, Any]]:
    keys = sorted(set(frozen_policy) | set(adaptive_policy))
    return [
        {
            "path": key,
            "frozen": frozen_policy.get(key),
            "adaptive": adaptive_policy.get(key),
        }
        for key in keys
        if frozen_policy.get(key) != adaptive_policy.get(key)
        or (key in frozen_policy) != (key in adaptive_policy)
    ]


def _common_policy_payload(policy: dict[str, Any]) -> dict[str, Any]:
    common = {
        key: value for key, value in policy.items() if key != ALLOWED_POLICY_DIFFERENCE
    }
    return {
        "schema_version": "paired_common_execution_policy_v1",
        "excluded_paths": [ALLOWED_POLICY_DIFFERENCE],
        "policy": common,
        "sha256": canonical_sha256(common),
    }


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _safe_block_filename(index: int, block_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", block_id).strip("._") or "block"
    return f"{index + 1:03d}-{safe}.csv"


def _materialize_exact_plan(
    *,
    benchmark: dict[str, Any],
    feature_csv: pathlib.Path,
    corpus_manifest: pathlib.Path,
    output_dir: pathlib.Path,
) -> tuple[dict[str, Any], list[str]]:
    mismatches: list[str] = []
    benchmark_id = benchmark.get("benchmark_id")
    identity = benchmark.get("canonical_identity")
    universe = identity.get("evaluation_universe") if isinstance(identity, dict) else None
    raw_blocks = universe.get("blocks") if isinstance(universe, dict) else None
    if not isinstance(raw_blocks, list) or not raw_blocks:
        return {
            "schema_version": EXACT_PLAN_SCHEMA,
            "benchmark_id": str(benchmark_id or ""),
            "target_bucket": "",
            "blocks": [],
        }, ["benchmark.canonical_identity.evaluation_universe.blocks_missing"]

    try:
        corpus = _read_json_object(corpus_manifest)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "schema_version": EXACT_PLAN_SCHEMA,
            "benchmark_id": str(benchmark_id or ""),
            "target_bucket": "",
            "blocks": [],
        }, [f"corpus_manifest_invalid:{type(exc).__name__}"]

    target_bucket = str(corpus.get("target_bucket") or "").strip().lower()
    if target_bucket not in {"trend", "range", "extreme"}:
        mismatches.append("corpus_manifest.target_bucket_invalid")
    base_interval_ms = corpus.get("base_interval_ms")
    if not isinstance(base_interval_ms, int) or base_interval_ms <= 0:
        mismatches.append("corpus_manifest.base_interval_ms_invalid")
        base_interval_ms = 300_000
    corpus_symbol = str(corpus.get("symbol") or "").strip().upper()
    if not corpus_symbol:
        mismatches.append("corpus_manifest.symbol_missing")
    if corpus.get("candidate_set_frozen") is not True:
        mismatches.append("corpus_manifest.candidate_set_frozen_not_true")
    declared_feature_sha = str(corpus.get("source_feature_sha256") or "")
    actual_feature_sha = file_sha256(feature_csv)
    if declared_feature_sha != actual_feature_sha:
        mismatches.append("corpus_manifest.source_feature_sha256_mismatch")
    declared_feature_path = str(corpus.get("source_feature_csv") or "").strip()
    if not declared_feature_path:
        mismatches.append("corpus_manifest.source_feature_csv_missing")
    else:
        declared_raw = pathlib.Path(declared_feature_path).expanduser()
        declared = (
            declared_raw
            if declared_raw.is_absolute()
            else corpus_manifest.parent / declared_raw
        ).resolve(strict=False)
        if declared != feature_csv.expanduser().resolve(strict=False):
            mismatches.append("corpus_manifest.source_feature_csv_mismatch")

    try:
        feature_rows = load_feature_rows(feature_csv)
    except (OSError, ValueError, UnicodeError) as exc:
        mismatches.append(f"feature_csv_invalid:{type(exc).__name__}:{exc}")
        feature_rows = []
    timestamp_to_index: dict[int, int] = {}
    for index, row in enumerate(feature_rows):
        if row.timestamp in timestamp_to_index:
            mismatches.append(f"feature_csv.duplicate_timestamp:{row.timestamp}")
        timestamp_to_index[row.timestamp] = index

    materialized_dir = output_dir / "exact_replay_inputs"
    planned_blocks: list[dict[str, Any]] = []
    seen_block_ids: set[str] = set()
    for index, raw_block in enumerate(raw_blocks):
        prefix = f"benchmark.block[{index}]"
        if not isinstance(raw_block, dict):
            mismatches.append(f"{prefix}.not_object")
            continue
        block_id = str(raw_block.get("block_id") or "").strip()
        if not block_id:
            mismatches.append(f"{prefix}.block_id_missing")
            continue
        if block_id in seen_block_ids:
            mismatches.append(f"{prefix}.block_id_duplicate:{block_id}")
            continue
        seen_block_ids.add(block_id)
        start = raw_block.get("start_timestamp_ms")
        end = raw_block.get("end_timestamp_ms")
        if not isinstance(start, int) or not isinstance(end, int) or start > end:
            mismatches.append(f"{prefix}.time_range_invalid")
            continue
        event_sha = raw_block.get("event_sha256")
        if not _valid_sha256(event_sha):
            mismatches.append(f"{prefix}.event_sha256_invalid")
            continue
        cells = raw_block.get("cells")
        if not isinstance(cells, list) or not cells:
            mismatches.append(f"{prefix}.cells_missing")
            continue
        symbols = {
            str(cell.get("symbol") or "").strip().upper()
            for cell in cells
            if isinstance(cell, dict)
        }
        regimes = {
            str(cell.get("entry_regime") or "").strip().lower()
            for cell in cells
            if isinstance(cell, dict)
        }
        if len(symbols) != 1 or "" in symbols:
            mismatches.append(f"{prefix}.single_symbol_required")
            continue
        symbol = next(iter(symbols))
        if symbol != corpus_symbol:
            mismatches.append(f"{prefix}.symbol_mismatch:{symbol}")
            continue
        if len(regimes) != 1 or target_bucket not in regimes:
            mismatches.append(f"{prefix}.target_bucket_mismatch")
            continue
        if start not in timestamp_to_index or end not in timestamp_to_index:
            mismatches.append(f"{prefix}.feature_interval_missing")
            continue
        start_index = timestamp_to_index[start]
        end_index = timestamp_to_index[end]
        if start_index > end_index:
            mismatches.append(f"{prefix}.feature_interval_reordered")
            continue
        timestamps = [row.timestamp for row in feature_rows[start_index : end_index + 1]]
        if any(
            current - previous != base_interval_ms
            for previous, current in zip(timestamps, timestamps[1:])
        ):
            mismatches.append(f"{prefix}.feature_interval_not_contiguous")
            continue
        segment = ReplaySegment(
            start_index=start_index,
            end_index=end_index,
            start_timestamp=start,
            end_timestamp=end,
            bars=end_index - start_index + 1,
        )
        replay_csv = materialized_dir / _safe_block_filename(index, block_id)
        try:
            write_replay_csv(
                feature_rows,
                segment,
                symbol,
                replay_csv,
                base_interval_ms,
                warmup_context_bars=0,
            )
            actual_event_sha = file_sha256(replay_csv)
        except (OSError, ValueError) as exc:
            mismatches.append(f"{prefix}.replay_materialization_failed:{type(exc).__name__}")
            continue
        if actual_event_sha != event_sha:
            mismatches.append(
                f"{prefix}.event_sha256_mismatch:expected={event_sha}:actual={actual_event_sha}"
            )
            continue
        segment_identity = replay_segment_identity(
            symbol=symbol,
            target_bucket=target_bucket,
            base_interval_ms=base_interval_ms,
            segment=ReplaySegment(
                start_index=0,
                end_index=segment.bars - 1,
                start_timestamp=start,
                end_timestamp=end,
                bars=segment.bars,
            ),
            replay_csv_sha256=actual_event_sha,
        )
        planned_blocks.append(
            {
                "block_id": block_id,
                "symbol": symbol,
                "start_timestamp_ms": start,
                "end_timestamp_ms": end,
                "event_sha256": actual_event_sha,
                "segment_identity_sha256": segment_identity["sha256"],
                "replay_csv": str(replay_csv.resolve()),
                "cells": cells,
            }
        )

    if len(planned_blocks) != len(raw_blocks):
        mismatches.append(
            "exact_block_plan.coverage_mismatch:"
            f"expected={len(raw_blocks)}:actual={len(planned_blocks)}"
        )
    return {
        "schema_version": EXACT_PLAN_SCHEMA,
        "benchmark_id": str(benchmark_id or ""),
        "target_bucket": target_bucket,
        "blocks": planned_blocks,
        "source_feature_sha256": actual_feature_sha,
        "source_corpus_manifest_sha256": file_sha256(corpus_manifest),
    }, list(dict.fromkeys(mismatches))


def _arm_command(
    *, config_path: pathlib.Path, output_dir: pathlib.Path, exact_plan: pathlib.Path, trade_bot: pathlib.Path
) -> list[str]:
    return [
        sys.executable,
        str(pathlib.Path(__file__).with_name("run_replay_validation.py")),
        "--exact-block-plan",
        str(exact_plan),
        "--base_config",
        str(config_path),
        "--trade_bot",
        str(trade_bot),
        "--output_dir",
        str(output_dir),
        "--assess_stage",
        "DEPLOY",
        "--min_runtime_status",
        "0",
        "--force-all-frozen-segments",
    ]


def _new_arm(
    name: str,
    *,
    config_path: pathlib.Path,
    output_dir: pathlib.Path,
    exact_plan_path: pathlib.Path,
    trade_bot: pathlib.Path,
) -> dict[str, Any]:
    return {
        "name": name,
        "config": {"path": str(config_path), "sha256": "", "policy": {}},
        "output_dir": str(output_dir),
        "command": _arm_command(
            config_path=config_path,
            output_dir=output_dir,
            exact_plan=exact_plan_path,
            trade_bot=trade_bot,
        ),
        "exit_code": None,
        "report": {
            "path": str(output_dir / "replay_validation_report.json"),
            "sha256": "",
            "schema_version": "",
            "status": "MISSING",
        },
        "infrastructure_status": "UNVERIFIABLE",
        "business_gate_status": "UNKNOWN",
        "expected_block_ids": [],
        "executed_block_ids": [],
        "block_execution_counts": {},
        "blocks": [],
        "mismatches": [],
    }


def _audit_arm_report(
    arm: dict[str, Any],
    *,
    expected_plan: dict[str, Any],
    exact_plan_path: pathlib.Path,
    exact_plan_sha256: str,
    benchmark_id: str,
    expected_policy: dict[str, Any],
    trade_bot_sha256: str,
    initial_weights_sha256: str,
    initial_state_sha256: str,
) -> list[str]:
    name = str(arm["name"])
    prefix = f"arm.{name}"
    mismatches: list[str] = []
    report_path = pathlib.Path(arm["report"]["path"])
    expected_blocks = expected_plan.get("blocks")
    expected_blocks = expected_blocks if isinstance(expected_blocks, list) else []
    expected_ids = [str(block.get("block_id") or "") for block in expected_blocks]
    arm["expected_block_ids"] = expected_ids
    if not report_path.is_file():
        mismatches.append(f"{prefix}.report_missing")
        return mismatches
    try:
        arm["report"]["sha256"] = file_sha256(report_path)
        report = _read_json_object(report_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        mismatches.append(f"{prefix}.report_invalid:{type(exc).__name__}")
        return mismatches
    arm["report"]["schema_version"] = str(report.get("schema_version") or "")
    arm["report"]["status"] = str(report.get("status") or "")
    if report.get("schema_version") != EXACT_REPORT_SCHEMA:
        mismatches.append(f"{prefix}.report_schema_invalid")
    if report.get("mode") != "exact_block_plan":
        mismatches.append(f"{prefix}.report_mode_invalid")
    if report.get("promotion_authority") is not False:
        mismatches.append(f"{prefix}.promotion_authority_not_false")
    for field in (
        "selection_bypassed",
        "final_holdout_bypassed",
        "coverage_early_stop_disabled",
    ):
        if report.get(field) is not True:
            mismatches.append(f"{prefix}.{field}_not_true")
    if report.get("mutation_targets_accessed") not in ([], None):
        mismatches.append(f"{prefix}.mutation_targets_accessed")

    plan_identity = report.get("exact_block_plan")
    if not isinstance(plan_identity, dict):
        mismatches.append(f"{prefix}.exact_block_plan_identity_missing")
    else:
        if pathlib.Path(str(plan_identity.get("path") or "")) != exact_plan_path:
            mismatches.append(f"{prefix}.exact_block_plan_path_mismatch")
        if plan_identity.get("sha256") != exact_plan_sha256:
            mismatches.append(f"{prefix}.exact_block_plan_sha256_mismatch")
        if plan_identity.get("benchmark_id") != benchmark_id:
            mismatches.append(f"{prefix}.benchmark_id_mismatch")
        if plan_identity.get("read_only") is not True:
            mismatches.append(f"{prefix}.exact_block_plan_not_read_only")
    report_policy = report.get("execution_policy_identity")
    if not isinstance(report_policy, dict) or report_policy.get("sha256") != expected_policy.get("sha256"):
        mismatches.append(f"{prefix}.execution_policy_sha256_mismatch")
    if report.get("trade_bot_sha256") != trade_bot_sha256:
        mismatches.append(f"{prefix}.trade_bot_sha256_mismatch")
    reported_trade_bot = pathlib.Path(
        str(report.get("trade_bot") or "")
    ).resolve(strict=False)
    expected_trade_bot = pathlib.Path(
        str(arm["command"][arm["command"].index("--trade_bot") + 1])
    ).resolve(strict=False)
    if reported_trade_bot != expected_trade_bot:
        mismatches.append(f"{prefix}.trade_bot_path_mismatch")
    if pathlib.Path(str(report.get("base_config") or "")) != pathlib.Path(
        str(arm["config"]["path"])
    ):
        mismatches.append(f"{prefix}.base_config_path_mismatch")
    if report.get("planned_block_count") != len(expected_ids):
        mismatches.append(f"{prefix}.planned_block_count_mismatch")
    if report.get("executed_block_count") != len(expected_ids):
        mismatches.append(f"{prefix}.executed_block_count_mismatch")

    raw_blocks = report.get("blocks")
    if not isinstance(raw_blocks, list):
        mismatches.append(f"{prefix}.blocks_missing")
        raw_blocks = []
    executed_ids = [
        str(block.get("block_id") or "")
        for block in raw_blocks
        if isinstance(block, dict) and block.get("execution_attempt_count") == 1
    ]
    counts = collections.Counter(executed_ids)
    arm["executed_block_ids"] = executed_ids
    arm["block_execution_counts"] = dict(sorted(counts.items()))
    if executed_ids != expected_ids:
        mismatches.append(f"{prefix}.executed_block_ids_mismatch")
    if any(counts.get(block_id, 0) != 1 for block_id in expected_ids):
        mismatches.append(f"{prefix}.block_execution_count_not_one")

    business_failed = False
    arm_root = pathlib.Path(str(arm["output_dir"])).resolve(strict=False)
    state_dirs: list[str] = []
    block_summaries: list[dict[str, Any]] = []
    allowed_report_errors: set[str] = set()
    for index, expected in enumerate(expected_blocks):
        if index >= len(raw_blocks) or not isinstance(raw_blocks[index], dict):
            continue
        audit = raw_blocks[index]
        block_prefix = f"{prefix}.block[{index}]"
        block_id = str(expected.get("block_id") or "")
        if audit.get("plan_index") != index:
            mismatches.append(f"{block_prefix}.plan_index_mismatch")
        if audit.get("block_id") != block_id:
            mismatches.append(f"{block_prefix}.block_id_mismatch")
        if audit.get("symbol") != expected.get("symbol"):
            mismatches.append(f"{block_prefix}.symbol_mismatch")
        for field, report_field in (
            ("event_sha256", "expected_event_sha256"),
            ("event_sha256", "actual_event_sha256"),
            ("segment_identity_sha256", "expected_segment_identity_sha256"),
            ("segment_identity_sha256", "actual_segment_identity_sha256"),
        ):
            if audit.get(report_field) != expected.get(field):
                mismatches.append(f"{block_prefix}.{report_field}_mismatch")
        if audit.get("execution_attempt_count") != 1:
            mismatches.append(f"{block_prefix}.execution_attempt_count_not_one")
        if audit.get("trade_bot_exit_code") != 0:
            mismatches.append(f"{block_prefix}.trade_bot_exit_nonzero")
        assess_exit = audit.get("assess_exit_code")
        if assess_exit != 0:
            if assess_exit == 1:
                business_failed = True
                allowed_report_errors.add(f"block[{index}].assess_exit_nonzero")
            else:
                mismatches.append(f"{block_prefix}.assess_exit_invalid")
        expected_execution_status = "FAILED" if assess_exit == 1 else "EXECUTED"
        if audit.get("execution_status") != expected_execution_status:
            mismatches.append(f"{block_prefix}.execution_status_inconsistent")
        evidence = audit.get("episode_execution_evidence")
        no_trade_zero_utility = False
        if not isinstance(evidence, dict):
            mismatches.append(f"{block_prefix}.episode_execution_evidence_missing")
        elif evidence.get("schema_version") != "episode_execution_evidence_v1":
            mismatches.append(f"{block_prefix}.episode_execution_evidence_schema_invalid")
        else:
            if evidence.get("segment_identity_sha256") != expected.get(
                "segment_identity_sha256"
            ):
                mismatches.append(f"{block_prefix}.episode_segment_identity_mismatch")
            evidence_policy = evidence.get("execution_policy_identity")
            if (
                not isinstance(evidence_policy, dict)
                or evidence_policy.get("sha256") != expected_policy.get("sha256")
            ):
                mismatches.append(f"{block_prefix}.episode_policy_identity_mismatch")
            episodes = evidence.get("episodes")
            if not isinstance(episodes, list):
                mismatches.append(f"{block_prefix}.episodes_missing")
                episodes = []
            declared_episode_count = evidence.get("episode_count")
            declared_complete_count = evidence.get("complete_episode_count")
            complete_episodes = [
                episode
                for episode in episodes
                if isinstance(episode, dict)
                and episode.get("execution_path_complete") is True
                and episode.get("utility_source") == "complete_execution_replay"
            ]
            complete_replay = (
                bool(episodes)
                and evidence.get("execution_path_complete") is True
                and declared_episode_count == len(episodes)
                and declared_complete_count == len(episodes)
                and len(complete_episodes) == len(episodes)
            )
            no_trade_zero_utility = (
                episodes == []
                and declared_episode_count == 0
                and declared_complete_count == 0
                and evidence.get("execution_path_complete") is False
                and evidence.get("aggregate_only_rejected") is True
                and evidence.get("missing_path_evidence") == ["fills"]
            )
            if not (complete_replay or no_trade_zero_utility):
                mismatches.append(f"{block_prefix}.execution_path_incomplete")
        audit_policy = audit.get("execution_policy_identity")
        if not isinstance(audit_policy, dict) or audit_policy.get("sha256") != expected_policy.get("sha256"):
            mismatches.append(f"{block_prefix}.execution_policy_sha256_mismatch")
        if audit.get("trade_bot_sha256") != trade_bot_sha256:
            mismatches.append(f"{block_prefix}.trade_bot_sha256_mismatch")
        state_dir = str(audit.get("state_dir") or "")
        state_path = pathlib.Path(state_dir).resolve(strict=False) if state_dir else None
        if state_path is None or arm_root not in state_path.parents:
            mismatches.append(f"{block_prefix}.state_dir_not_isolated")
        expected_state = arm_root / f"exact_block_{index + 1:03d}" / "state"
        if state_path != expected_state:
            mismatches.append(f"{block_prefix}.state_dir_unexpected")
        if state_path is None or not state_path.is_dir():
            mismatches.append(f"{block_prefix}.state_dir_missing")
        if state_dir:
            state_dirs.append(str(state_path))
        runtime_outputs: dict[str, str] = {}
        for output_field in ("runtime_log", "runtime_assess"):
            output_value = str(audit.get(output_field) or "")
            output_path = (
                pathlib.Path(output_value).resolve(strict=False)
                if output_value
                else None
            )
            if (
                output_path is None
                or arm_root not in output_path.parents
                or not output_path.is_file()
            ):
                mismatches.append(f"{block_prefix}.{output_field}_not_isolated")
            expected_output = expected_state.parent / (
                "runtime.log" if output_field == "runtime_log" else "runtime_assess.json"
            )
            if output_path != expected_output:
                mismatches.append(f"{block_prefix}.{output_field}_unexpected")
            runtime_outputs[output_field] = output_value
        command = audit.get("command")
        if not isinstance(command, list):
            mismatches.append(f"{block_prefix}.command_missing")
        else:
            required_args = {
                f"--config={arm['config']['path']}",
                f"--data_path={state_path}" if state_path is not None else "",
                f"--replay_market_data={expected.get('replay_csv')}",
            }
            if not required_args.issubset({str(item) for item in command}):
                mismatches.append(f"{block_prefix}.command_identity_mismatch")
        historical = audit.get("historical_state_loaded", False)
        continued_from = audit.get("continued_from_block_id")
        reported_weight_sha = audit.get("initial_weights_sha256", initial_weights_sha256)
        reported_state_sha = audit.get(
            "initial_evolution_state_sha256", initial_state_sha256
        )
        if historical is not False:
            mismatches.append(f"{block_prefix}.historical_state_loaded")
        if continued_from not in (None, ""):
            mismatches.append(f"{block_prefix}.continued_from_block:{continued_from}")
        if reported_weight_sha != initial_weights_sha256:
            mismatches.append(f"{block_prefix}.initial_weights_sha256_mismatch")
        if reported_state_sha != initial_state_sha256:
            mismatches.append(f"{block_prefix}.initial_evolution_state_sha256_mismatch")
        errors = audit.get("errors")
        errors = errors if isinstance(errors, list) else []
        unexpected_errors = [error for error in errors if error != "assess_exit_nonzero"]
        if unexpected_errors:
            mismatches.append(f"{block_prefix}.execution_errors:{','.join(map(str, unexpected_errors))}")
        block_summaries.append(
            {
                "block_id": block_id,
                "symbol": expected.get("symbol"),
                "event_sha256": expected.get("event_sha256"),
                "segment_identity_sha256": expected.get("segment_identity_sha256"),
                "state_dir": state_dir,
                "runtime_log": runtime_outputs["runtime_log"],
                "runtime_assess": runtime_outputs["runtime_assess"],
                "command": command if isinstance(command, list) else [],
                "assess_command": (
                    audit.get("assess_command")
                    if isinstance(audit.get("assess_command"), list)
                    else []
                ),
                "initial_weights_sha256": initial_weights_sha256,
                "initial_evolution_state_sha256": initial_state_sha256,
                "historical_state_loaded": bool(historical),
                "continued_from_block_id": continued_from or None,
                "trade_bot_exit_code": audit.get("trade_bot_exit_code"),
                "assess_exit_code": assess_exit,
                "episode_execution_evidence": evidence,
                "no_trade_zero_utility": no_trade_zero_utility,
            }
        )
    if len(state_dirs) != len(set(state_dirs)):
        mismatches.append(f"{prefix}.state_dir_reused_across_blocks")
    arm["blocks"] = block_summaries
    arm["business_gate_status"] = "FAILED" if business_failed else "PASSED"

    validation_errors = report.get("validation_errors")
    validation_errors = validation_errors if isinstance(validation_errors, list) else []
    unexpected_validation_errors = [
        str(error) for error in validation_errors if str(error) not in allowed_report_errors
    ]
    if unexpected_validation_errors:
        mismatches.append(
            f"{prefix}.report_validation_errors:" + ",".join(unexpected_validation_errors)
        )
    report_status = report.get("status")
    exit_code = arm.get("exit_code")
    verified_transport = (
        report_status == "VERIFIED" and exit_code == 0 and not business_failed
    )
    audited_business_failure = (
        report_status == "UNVERIFIABLE"
        and exit_code == 2
        and business_failed
        and not unexpected_validation_errors
    )
    if not (verified_transport or audited_business_failure):
        mismatches.append(
            f"{prefix}.command_or_report_status_invalid:exit={exit_code}:status={report_status}"
        )
    arm["infrastructure_status"] = "VERIFIED" if not mismatches else "UNVERIFIABLE"
    arm["mismatches"] = list(mismatches)
    return mismatches


def run_paired_evolution_replay(
    *,
    runtime_config: pathlib.Path,
    candidate_model: pathlib.Path,
    candidate_report: pathlib.Path,
    feature_csv: pathlib.Path,
    corpus_manifest: pathlib.Path,
    trade_bot: pathlib.Path,
    output_dir: pathlib.Path,
    benchmark_report: pathlib.Path,
    process_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    paths = {
        "runtime_config": pathlib.Path(runtime_config).resolve(strict=False),
        "candidate_model": pathlib.Path(candidate_model).resolve(strict=False),
        "candidate_report": pathlib.Path(candidate_report).resolve(strict=False),
        "feature_csv": pathlib.Path(feature_csv).resolve(strict=False),
        "corpus_manifest": pathlib.Path(corpus_manifest).resolve(strict=False),
        "trade_bot": pathlib.Path(trade_bot).resolve(strict=False),
        "benchmark_report": pathlib.Path(benchmark_report).resolve(strict=False),
        "output_dir": pathlib.Path(output_dir).resolve(strict=False),
    }
    output_dir = paths["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / MANIFEST_FILENAME
    config_dir = output_dir / "configs"
    common_config_path = config_dir / "common_replay.yaml"
    frozen_config_path = config_dir / "frozen.yaml"
    adaptive_config_path = config_dir / "adaptive.yaml"
    exact_plan_path = output_dir / "exact_block_plan.json"
    frozen_output = output_dir / "frozen"
    adaptive_output = output_dir / "adaptive"
    mismatches: list[str] = []
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": now_utc_iso(),
        "status": "UNVERIFIABLE",
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "benchmark_id": "",
        "source_runtime_config": _identity(paths["runtime_config"]),
        "common_derived_config": {
            "path": str(common_config_path),
            "sha256": "",
            "policy": {},
        },
        "common_policy": {
            "schema_version": "paired_common_execution_policy_v1",
            "excluded_paths": [ALLOWED_POLICY_DIFFERENCE],
            "policy": {},
            "sha256": "",
        },
        "initial_weights": {"payload": {}, "sha256": ""},
        "initial_evolution_state": {
            "payload": {
                "schema_version": "empty_evolution_state_v1",
                "records": [],
            },
            "sha256": "",
            "empty": True,
            "historical_state_loading_allowed": False,
            "cross_block_continuation_allowed": False,
        },
        "feature_csv": _identity(paths["feature_csv"]),
        "corpus_manifest": _identity(paths["corpus_manifest"]),
        "trade_bot": _identity(paths["trade_bot"]),
        "candidate_model": _identity(paths["candidate_model"]),
        "candidate_report": _identity(paths["candidate_report"]),
        "benchmark_report": _identity(paths["benchmark_report"]),
        "exact_block_plan": {
            "path": str(exact_plan_path),
            "sha256": "",
            "read_only": True,
            "expected_block_ids": [],
            "blocks": [],
        },
        "policy_differences": [],
        "arms": {
            "frozen": _new_arm(
                "frozen",
                config_path=frozen_config_path,
                output_dir=frozen_output,
                exact_plan_path=exact_plan_path,
                trade_bot=paths["trade_bot"],
            ),
            "adaptive": _new_arm(
                "adaptive",
                config_path=adaptive_config_path,
                output_dir=adaptive_output,
                exact_plan_path=exact_plan_path,
                trade_bot=paths["trade_bot"],
            ),
        },
        "mismatches": mismatches,
    }
    empty_state_payload = manifest["initial_evolution_state"]["payload"]
    manifest["initial_evolution_state"]["sha256"] = canonical_sha256(
        empty_state_payload
    )

    for name in (
        "runtime_config",
        "candidate_model",
        "candidate_report",
        "feature_csv",
        "corpus_manifest",
        "trade_bot",
        "benchmark_report",
    ):
        if not paths[name].is_file():
            mismatches.append(f"input.{name}_missing")
    for name, arm_output in (("frozen", frozen_output), ("adaptive", adaptive_output)):
        if arm_output.exists():
            mismatches.append(f"arm.{name}.output_dir_preexisting")

    benchmark: dict[str, Any] = {}
    if paths["benchmark_report"].is_file():
        try:
            benchmark = _read_json_object(paths["benchmark_report"])
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            mismatches.append(f"benchmark_report_invalid:{type(exc).__name__}")
        else:
            if benchmark.get("schema_version") != BENCHMARK_REPORT_SCHEMA:
                mismatches.append("benchmark_report.schema_version_invalid")
            if benchmark.get("identity_status") != "VERIFIED":
                mismatches.append("benchmark_report.identity_status_not_verified")
            if benchmark.get("drifts") != []:
                mismatches.append("benchmark_report.drifts_not_empty")
            benchmark_id = benchmark.get("benchmark_id")
            if not _valid_sha256(benchmark_id):
                mismatches.append("benchmark_report.benchmark_id_invalid")
            else:
                manifest["benchmark_id"] = benchmark_id
                canonical_identity = benchmark.get("canonical_identity")
                if not isinstance(canonical_identity, dict):
                    mismatches.append("benchmark_report.canonical_identity_missing")
                elif canonical_sha256(canonical_identity) != benchmark_id:
                    mismatches.append("benchmark_report.canonical_identity_hash_mismatch")

    arm_policies: dict[str, dict[str, Any]] = {}
    if paths["runtime_config"].is_file():
        try:
            runtime_text = paths["runtime_config"].read_text(encoding="utf-8")
            runtime_sha = file_sha256(paths["runtime_config"])
            runtime_payload = policy_payload(paths["runtime_config"])
            manifest["source_runtime_config"]["policy"] = runtime_payload
            common_text = derive_candidate_config(
                runtime_text,
                model_path=str(paths["candidate_model"]),
                report_path=str(paths["candidate_report"]),
                source_runtime_config_sha256=runtime_sha,
            )
            _atomic_write_text(common_config_path, common_text)
            common_payload = policy_payload(common_config_path)
            if policy_sha256(common_config_path) != common_payload.get("sha256"):
                raise ValueError("common policy identity inconsistent")
            if common_payload.get("sha256") != runtime_payload.get("sha256"):
                mismatches.append("common_replay_policy_differs_from_runtime")
            manifest["common_derived_config"] = {
                "path": str(common_config_path),
                "sha256": file_sha256(common_config_path),
                "policy": common_payload,
            }
            for name, enabled, config_path in (
                ("frozen", False, frozen_config_path),
                ("adaptive", True, adaptive_config_path),
            ):
                arm_text = derive_arm_config(common_text, enabled=enabled)
                _atomic_write_text(config_path, arm_text)
                payload = policy_payload(config_path)
                if policy_sha256(config_path) != payload.get("sha256"):
                    raise ValueError(f"{name} policy identity inconsistent")
                arm_policies[name] = payload
                manifest["arms"][name]["config"] = {
                    "path": str(config_path),
                    "sha256": file_sha256(config_path),
                    "policy": payload,
                }
            frozen_policy = arm_policies["frozen"]["policy"]
            adaptive_policy = arm_policies["adaptive"]["policy"]
            differences = _policy_differences(frozen_policy, adaptive_policy)
            manifest["policy_differences"] = differences
            if differences != [
                {
                    "path": ALLOWED_POLICY_DIFFERENCE,
                    "frozen": False,
                    "adaptive": True,
                }
            ]:
                mismatches.append("unexpected_policy_difference:" + json.dumps(
                    differences,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ))
            frozen_common = _common_policy_payload(frozen_policy)
            adaptive_common = _common_policy_payload(adaptive_policy)
            if frozen_common != adaptive_common:
                mismatches.append("arm.common_policy_mismatch")
            manifest["common_policy"] = frozen_common
            weights_payload = {
                "trend": frozen_policy.get("self_evolution.initial_trend_weight"),
                "defensive": frozen_policy.get(
                    "self_evolution.initial_defensive_weight"
                ),
            }
            values = list(weights_payload.values())
            if (
                not all(
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                    for value in values
                )
                or any(float(value) < 0.0 for value in values)
                or not math.isclose(sum(map(float, values)), 1.0, rel_tol=0.0, abs_tol=1e-9)
            ):
                mismatches.append("initial_weights_invalid")
            manifest["initial_weights"] = {
                "payload": weights_payload,
                "sha256": canonical_sha256(weights_payload),
            }
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            mismatches.append(f"config_derivation_failed:{type(exc).__name__}:{exc}")

    exact_plan: dict[str, Any] = {
        "schema_version": EXACT_PLAN_SCHEMA,
        "benchmark_id": manifest["benchmark_id"],
        "target_bucket": "",
        "blocks": [],
    }
    if (
        benchmark
        and paths["feature_csv"].is_file()
        and paths["corpus_manifest"].is_file()
    ):
        try:
            exact_plan, plan_mismatches = _materialize_exact_plan(
                benchmark=benchmark,
                feature_csv=paths["feature_csv"],
                corpus_manifest=paths["corpus_manifest"],
                output_dir=output_dir,
            )
            mismatches.extend(plan_mismatches)
            _atomic_write_json(exact_plan_path, exact_plan, mode=0o444)
            manifest["exact_block_plan"] = {
                "path": str(exact_plan_path),
                "sha256": file_sha256(exact_plan_path),
                "read_only": True,
                "benchmark_id": exact_plan.get("benchmark_id"),
                "expected_block_ids": [
                    str(item.get("block_id") or "")
                    for item in exact_plan.get("blocks", [])
                    if isinstance(item, dict)
                ],
                "blocks": exact_plan.get("blocks", []),
            }
        except (OSError, ValueError, TypeError) as exc:
            mismatches.append(f"exact_block_plan_derivation_failed:{type(exc).__name__}:{exc}")

    manifest["mismatches"] = list(dict.fromkeys(mismatches))
    _atomic_write_json(manifest_path, manifest)

    if not manifest["mismatches"]:
        runner = process_runner or subprocess.run
        for name in ("frozen", "adaptive"):
            arm = manifest["arms"][name]
            try:
                completed = runner(arm["command"], check=False)
                arm["exit_code"] = int(completed.returncode)
            except Exception as exc:  # preserve the other arm and final manifest
                arm["mismatches"].append(
                    f"arm.{name}.command_failed:{type(exc).__name__}:{exc}"
                )
                arm["infrastructure_status"] = "UNVERIFIABLE"
            manifest["mismatches"].extend(arm["mismatches"])
            _atomic_write_json(manifest_path, manifest)

        for name in ("frozen", "adaptive"):
            arm = manifest["arms"][name]
            arm_mismatches = _audit_arm_report(
                arm,
                expected_plan=exact_plan,
                exact_plan_path=exact_plan_path,
                exact_plan_sha256=manifest["exact_block_plan"]["sha256"],
                benchmark_id=manifest["benchmark_id"],
                expected_policy=arm_policies[name],
                trade_bot_sha256=manifest["trade_bot"]["sha256"],
                initial_weights_sha256=manifest["initial_weights"]["sha256"],
                initial_state_sha256=manifest["initial_evolution_state"]["sha256"],
            )
            manifest["mismatches"].extend(arm_mismatches)

        state_owners: dict[str, str] = {}
        output_owners: dict[str, str] = {}
        for name in ("frozen", "adaptive"):
            for block in manifest["arms"][name]["blocks"]:
                state_dir = str(block.get("state_dir") or "")
                if state_dir and state_dir in state_owners:
                    manifest["mismatches"].append(
                        f"state_dir_reused_across_arms:{state_owners[state_dir]}:{name}:{state_dir}"
                    )
                elif state_dir:
                    state_owners[state_dir] = name
                for output_field in ("runtime_log", "runtime_assess"):
                    output_path = str(block.get(output_field) or "")
                    if output_path and output_path in output_owners:
                        manifest["mismatches"].append(
                            f"{output_field}_reused:{output_owners[output_path]}:{name}:{output_path}"
                        )
                    elif output_path:
                        output_owners[output_path] = name

    immutable_identities = [
        ("source_runtime_config", manifest["source_runtime_config"]),
        ("feature_csv", manifest["feature_csv"]),
        ("corpus_manifest", manifest["corpus_manifest"]),
        ("trade_bot", manifest["trade_bot"]),
        ("candidate_model", manifest["candidate_model"]),
        ("candidate_report", manifest["candidate_report"]),
        ("benchmark_report", manifest["benchmark_report"]),
        ("common_derived_config", manifest["common_derived_config"]),
        ("exact_block_plan", manifest["exact_block_plan"]),
        ("arm.frozen.config", manifest["arms"]["frozen"]["config"]),
        ("arm.adaptive.config", manifest["arms"]["adaptive"]["config"]),
    ]
    for identity_name, identity in immutable_identities:
        expected_sha = str(identity.get("sha256") or "")
        identity_path = pathlib.Path(str(identity.get("path") or ""))
        if expected_sha:
            try:
                actual_sha = file_sha256(identity_path)
            except OSError:
                actual_sha = ""
            if actual_sha != expected_sha:
                manifest["mismatches"].append(
                    f"{identity_name}.sha256_changed:expected={expected_sha}:actual={actual_sha}"
                )
    for index, block in enumerate(exact_plan.get("blocks", [])):
        if not isinstance(block, dict):
            continue
        replay_path = pathlib.Path(str(block.get("replay_csv") or ""))
        expected_sha = str(block.get("event_sha256") or "")
        try:
            actual_sha = file_sha256(replay_path)
        except OSError:
            actual_sha = ""
        if actual_sha != expected_sha:
            manifest["mismatches"].append(
                f"exact_block_plan.block[{index}].event_sha256_changed:"
                f"expected={expected_sha}:actual={actual_sha}"
            )

    manifest["mismatches"] = list(dict.fromkeys(manifest["mismatches"]))
    manifest["status"] = "VERIFIED" if not manifest["mismatches"] else "UNVERIFIABLE"
    _atomic_write_json(manifest_path, manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", required=True)
    parser.add_argument("--candidate-model", required=True)
    parser.add_argument("--candidate-report", required=True)
    parser.add_argument("--feature-csv", required=True)
    parser.add_argument("--corpus-manifest", required=True)
    parser.add_argument("--trade-bot", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--benchmark-report", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = run_paired_evolution_replay(
        runtime_config=pathlib.Path(args.runtime_config),
        candidate_model=pathlib.Path(args.candidate_model),
        candidate_report=pathlib.Path(args.candidate_report),
        feature_csv=pathlib.Path(args.feature_csv),
        corpus_manifest=pathlib.Path(args.corpus_manifest),
        trade_bot=pathlib.Path(args.trade_bot),
        output_dir=pathlib.Path(args.output_dir),
        benchmark_report=pathlib.Path(args.benchmark_report),
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if manifest["status"] == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
