#!/usr/bin/env python3
"""Build a current-run, file-backed decision-evidence benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import re
import tempfile
from typing import Any

from decision_evidence_common import (
    BENCHMARK_SCHEMA_VERSION,
    canonical_sha256,
    file_sha256,
)
from run_replay_validation import (
    ReplaySegment,
    load_feature_rows,
    replay_segment_identity,
    write_replay_csv,
)
from validate_decision_benchmark import validate_files


BUILD_REPORT_SCHEMA = "decision_evidence_benchmark_build_v1"
CANDIDATE_PREFLIGHT_SCHEMA = "integrator_candidate_preflight_v1"
PRIMARY_OBJECTIVE = "aggregate_model_net_bps_per_unit_turnover_after_cost"
VALID_REGIMES = {"trend", "range", "extreme"}


def _atomic_write_json(path: pathlib.Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(
            payload,
            handle,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        handle.write("\n")
        handle.flush()
    temporary.replace(path)


def _read_json_object(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def validate_integrator_candidate(
    candidate_model: pathlib.Path, candidate_report: pathlib.Path
) -> dict[str, Any]:
    errors: list[str] = []
    model = pathlib.Path(candidate_model).resolve(strict=False)
    report_path = pathlib.Path(candidate_report).resolve(strict=False)
    if not model.is_file() or model.stat().st_size <= 0:
        errors.append("candidate.model_missing_or_empty")
    try:
        report = _read_json_object(report_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        report = {}
        errors.append(f"candidate.report_invalid:{type(exc).__name__}")

    for field in ("model_version", "feature_schema_version", "factor_set_version"):
        if not _is_nonempty_string(report.get(field)):
            errors.append(f"candidate.report.{field}_missing")
    feature_names = report.get("feature_names")
    if (
        not isinstance(feature_names, list)
        or not feature_names
        or any(not _is_nonempty_string(item) for item in feature_names)
        or len(set(feature_names)) != len(feature_names)
    ):
        errors.append("candidate.report.feature_names_invalid")
    if not isinstance(report.get("feature_transform"), dict):
        errors.append("candidate.report.feature_transform_missing")

    data = report.get("data")
    if not isinstance(data, dict):
        data = {}
        errors.append("candidate.report.data_missing")
    required_data = {
        "csv_path": None,
        "training_symbol": None,
        "online_bar_source": "closed_ohlcv",
        "source_venue": "bybit",
        "source_category": "linear",
        "price_type": "trade_price",
        "volume_unit": "base_asset",
    }
    for field, expected in required_data.items():
        value = data.get(field)
        if not _is_nonempty_string(value) or (
            expected is not None and value != expected
        ):
            errors.append(f"candidate.report.data.{field}_invalid")
    interval = data.get("bar_interval_ms")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        errors.append("candidate.report.data.bar_interval_ms_invalid")

    metrics = report.get("metrics_oos")
    if not isinstance(metrics, dict):
        metrics = {}
        errors.append("candidate.report.metrics_oos_missing")
    if metrics.get("primary_objective") != PRIMARY_OBJECTIVE:
        errors.append("candidate.report.metrics_oos.primary_objective_invalid")
    if not _finite_number(metrics.get("mean_model_net_edge_bps_per_round_trip")):
        errors.append("candidate.report.metrics_oos.mean_net_edge_missing")
    for field in ("split_trained_count", "split_count"):
        value = metrics.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"candidate.report.metrics_oos.{field}_invalid")

    governance = report.get("governance")
    if not isinstance(governance, dict):
        governance = {}
        errors.append("candidate.report.governance_missing")
    if governance.get("pass") is not True:
        errors.append("candidate.report.governance.pass_not_true")
    if governance.get("primary_objective") != PRIMARY_OBJECTIVE:
        errors.append("candidate.report.governance.primary_objective_invalid")
    if report.get("model_artifact_status") != "published":
        errors.append("candidate.report.model_artifact_status_not_published")

    return {
        "schema_version": CANDIDATE_PREFLIGHT_SCHEMA,
        "status": "VERIFIED" if not errors else "UNVERIFIABLE",
        "model_version": str(report.get("model_version") or ""),
        "candidate_model": {
            "path": str(model),
            "sha256": file_sha256(model) if model.is_file() else "",
        },
        "candidate_report": {
            "path": str(report_path),
            "sha256": file_sha256(report_path) if report_path.is_file() else "",
        },
        "errors": errors,
    }


def _component(name: str, files: list[tuple[str, pathlib.Path]]) -> dict[str, Any]:
    identities = [
        {
            "logical_name": logical_name,
            "path": str(path.resolve()),
            "sha256": file_sha256(path),
        }
        for logical_name, path in sorted(files, key=lambda item: item[0])
    ]
    logical_identity = [
        {"logical_name": item["logical_name"], "sha256": item["sha256"]}
        for item in identities
    ]
    return {
        "logical_id": f"{name}:{canonical_sha256(logical_identity)}",
        "files": identities,
    }


def _normalize_mapping(
    mapping: dict[str, pathlib.Path] | None,
) -> dict[str, pathlib.Path]:
    return {
        str(symbol).strip().upper(): pathlib.Path(path).resolve(strict=False)
        for symbol, path in (mapping or {}).items()
        if str(symbol).strip()
    }


def parse_symbol_mapping(raw: str) -> dict[str, pathlib.Path]:
    mapping: dict[str, pathlib.Path] = {}
    for item in (part.strip() for part in str(raw or "").split(",")):
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"symbol mapping requires SYMBOL=PATH: {item}")
        symbol, path = item.split("=", 1)
        symbol = symbol.strip().upper()
        if not symbol or not path.strip() or symbol in mapping:
            raise ValueError(f"invalid or duplicate symbol mapping: {item}")
        mapping[symbol] = pathlib.Path(path.strip())
    return mapping


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "item"


def _resolve_sources(
    symbols: set[str],
    feature_csv: pathlib.Path,
    corpus_manifest: pathlib.Path,
    feature_mapping: dict[str, pathlib.Path],
    corpus_mapping: dict[str, pathlib.Path],
) -> tuple[dict[str, pathlib.Path], dict[str, pathlib.Path]]:
    features = dict(feature_mapping)
    corpora = dict(corpus_mapping)
    if len(symbols) == 1:
        symbol = next(iter(symbols))
        features.setdefault(symbol, feature_csv.resolve(strict=False))
        corpora.setdefault(symbol, corpus_manifest.resolve(strict=False))
    if set(features) != symbols:
        raise ValueError(
            "feature symbol mapping mismatch: "
            f"expected={sorted(symbols)},actual={sorted(features)}"
        )
    if set(corpora) != symbols:
        raise ValueError(
            "corpus symbol mapping mismatch: "
            f"expected={sorted(symbols)},actual={sorted(corpora)}"
        )
    return features, corpora


def _producer_binding_sha256(binding: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_frozen_corpus_binding(
    replay_report: dict[str, Any], symbols: set[str]
) -> dict[str, pathlib.Path]:
    binding = replay_report.get("frozen_corpus_binding")
    if not isinstance(binding, dict):
        raise ValueError("replay frozen corpus binding missing")
    if binding.get("schema_version") != "frozen_replay_corpus_binding_v1":
        raise ValueError("replay frozen corpus binding schema invalid")
    declared_binding_sha = binding.get("binding_sha256")
    unsigned_binding = {
        key: value for key, value in binding.items() if key != "binding_sha256"
    }
    actual_binding_sha = _producer_binding_sha256(unsigned_binding)
    if declared_binding_sha != actual_binding_sha:
        raise ValueError("replay frozen corpus binding hash mismatch")
    raw_per_symbol = binding.get("per_symbol")
    if not isinstance(raw_per_symbol, dict) or set(raw_per_symbol) != symbols:
        raise ValueError(
            "replay frozen corpus binding symbols mismatch: "
            f"expected={sorted(symbols)},actual="
            f"{sorted(raw_per_symbol) if isinstance(raw_per_symbol, dict) else []}"
        )
    corpus_paths: dict[str, pathlib.Path] = {}
    bound_fields = (
        "schema_version",
        "evidence_domain",
        "candidate_set_frozen",
        "source_feature_csv",
        "source_feature_sha256",
        "target_bucket",
        "thresholds",
        "sampling_quantiles",
    )
    for symbol in sorted(symbols):
        item = raw_per_symbol.get(symbol)
        if not isinstance(item, dict):
            raise ValueError(f"replay frozen corpus binding invalid for {symbol}")
        path_text = item.get("path")
        expected_sha = item.get("sha256")
        if not _is_nonempty_string(path_text):
            raise ValueError(f"replay frozen corpus path missing for {symbol}")
        path = pathlib.Path(path_text).expanduser().resolve(strict=False)
        if not path.is_file():
            raise ValueError(f"replay frozen corpus path missing for {symbol}: {path}")
        actual_sha = file_sha256(path)
        if expected_sha != actual_sha:
            raise ValueError(f"replay frozen corpus hash mismatch for {symbol}")
        payload = _read_json_object(path)
        for field in bound_fields:
            if item.get(field) != payload.get(field):
                raise ValueError(
                    f"replay frozen corpus metadata mismatch for {symbol}:{field}"
                )
        corpus_paths[symbol] = path
    return corpus_paths


def _write_replay_split_identity(
    replay_report: dict[str, Any], output_dir: pathlib.Path
) -> pathlib.Path:
    raw_binding = replay_report.get("frozen_corpus_binding")
    raw_per_symbol = (
        raw_binding.get("per_symbol") if isinstance(raw_binding, dict) else {}
    )
    corpus_identity_fields = (
        "sha256",
        "schema_version",
        "evidence_domain",
        "candidate_set_frozen",
        "source_feature_sha256",
        "target_bucket",
        "thresholds",
        "sampling_quantiles",
    )
    corpus_identities = {
        symbol: {
            key: item.get(key)
            for key in corpus_identity_fields
        }
        for symbol, item in sorted(raw_per_symbol.items())
        if isinstance(symbol, str) and isinstance(item, dict)
    }
    raw_runs = replay_report.get("runs")
    runs = []
    for run in raw_runs if isinstance(raw_runs, list) else []:
        if not isinstance(run, dict) or not isinstance(run.get("segment"), dict):
            continue
        segment = run["segment"]
        runs.append(
            {
                "symbol": str(run.get("symbol") or "").strip().upper(),
                "segment": {
                    "start_timestamp": segment.get("start_timestamp"),
                    "end_timestamp": segment.get("end_timestamp"),
                    "target_bucket": segment.get("target_bucket"),
                },
            }
        )
    candidate = replay_report.get("candidate_identity")
    candidate_identity = {
        key: candidate.get(key)
        for key in (
            "model_version",
            "model_sha256",
            "integrator_report_sha256",
        )
        if isinstance(candidate, dict) and key in candidate
    }
    identity_path = output_dir / "paired_inputs" / "replay_validation_identity.json"
    _atomic_write_json(
        identity_path,
        {
            "schema_version": "decision_evidence_replay_split_identity_v1",
            "status": replay_report.get("status"),
            "target_bucket": replay_report.get("target_bucket"),
            "base_interval_ms": replay_report.get("base_interval_ms"),
            "base_interval_ms_by_symbol": replay_report.get(
                "base_interval_ms_by_symbol"
            ),
            "candidate_identity": candidate_identity,
            "frozen_corpus_binding": {
                "schema_version": raw_binding.get("schema_version")
                if isinstance(raw_binding, dict)
                else None,
                "per_symbol": corpus_identities,
            },
            "runs": sorted(
                runs,
                key=lambda item: (
                    item["segment"]["start_timestamp"],
                    item["segment"]["end_timestamp"],
                    item["symbol"],
                    str(item["segment"]["target_bucket"]),
                ),
            ),
        },
    )
    return identity_path


def _build_universe(
    *,
    replay_report: dict[str, Any],
    feature_paths: dict[str, pathlib.Path],
    corpus_paths: dict[str, pathlib.Path],
    output_dir: pathlib.Path,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    raw_runs = replay_report.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError("replay report runs missing")
    source_segments: list[dict[str, Any]] = []
    target_default = str(replay_report.get("target_bucket") or "").lower()
    for index, run in enumerate(raw_runs):
        if not isinstance(run, dict):
            raise ValueError(f"replay run[{index}] is not an object")
        symbol = str(run.get("symbol") or "").strip().upper()
        segment = run.get("segment")
        if not symbol or not isinstance(segment, dict):
            raise ValueError(f"replay run[{index}] identity missing")
        start = segment.get("start_timestamp")
        end = segment.get("end_timestamp")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start > end
        ):
            raise ValueError(f"replay run[{index}] interval invalid")
        regime = str(segment.get("target_bucket") or target_default).strip().lower()
        if regime not in VALID_REGIMES:
            raise ValueError(f"replay run[{index}] entry regime invalid")
        source_identity = {
            "symbol": symbol,
            "start_timestamp_ms": start,
            "end_timestamp_ms": end,
            "entry_regime": regime,
        }
        source_segments.append(
            {
                **source_identity,
                "source_segment_id": "segment-"
                + canonical_sha256(source_identity)[:16],
            }
        )

    seen_source_ids: set[str] = set()
    previous_by_symbol: dict[str, dict[str, Any]] = {}
    for source in sorted(
        source_segments,
        key=lambda item: (
            item["symbol"],
            item["start_timestamp_ms"],
            item["end_timestamp_ms"],
            item["entry_regime"],
        ),
    ):
        source_id = source["source_segment_id"]
        if source_id in seen_source_ids:
            raise ValueError(f"duplicate replay run interval for {source['symbol']}")
        seen_source_ids.add(source_id)
        previous = previous_by_symbol.get(source["symbol"])
        if (
            previous is not None
            and source["start_timestamp_ms"] <= previous["end_timestamp_ms"]
        ):
            raise ValueError(
                f"replay run intervals overlap within symbol {source['symbol']}"
            )
        previous_by_symbol[source["symbol"]] = source

    source_rows: dict[str, tuple[list[Any], dict[int, int], int, str]] = {}
    paired_corpora: dict[str, str] = {}
    for symbol in sorted({item["symbol"] for item in source_segments}):
        feature_path = feature_paths[symbol]
        corpus_path = corpus_paths[symbol]
        if not feature_path.is_file():
            raise ValueError(f"feature csv missing for {symbol}")
        corpus = _read_json_object(corpus_path)
        if corpus.get("candidate_set_frozen") is not True:
            raise ValueError(f"corpus is not frozen for {symbol}")
        corpus_symbol = str(corpus.get("symbol") or "").upper()
        if corpus_symbol != symbol:
            raise ValueError(f"corpus symbol mismatch for {symbol}")
        base_interval = corpus.get("base_interval_ms")
        if not isinstance(base_interval, int) or base_interval <= 0:
            raise ValueError(f"corpus interval invalid for {symbol}")
        target_bucket = str(corpus.get("target_bucket") or "").lower()
        if target_bucket not in VALID_REGIMES:
            raise ValueError(f"corpus target bucket invalid for {symbol}")
        rows = load_feature_rows(feature_path)
        timestamp_to_index: dict[int, int] = {}
        for row_index, row in enumerate(rows):
            if row.timestamp in timestamp_to_index:
                raise ValueError(f"duplicate feature timestamp for {symbol}")
            timestamp_to_index[row.timestamp] = row_index
        source_rows[symbol] = (
            rows,
            timestamp_to_index,
            base_interval,
            target_bucket,
        )
        paired_corpus_path = output_dir / "paired_inputs" / symbol / "corpus.json"
        _atomic_write_json(
            paired_corpus_path,
            {
                "schema_version": "decision_evidence_execution_corpus_v1",
                "candidate_set_frozen": True,
                "symbol": symbol,
                "target_bucket": target_bucket,
                "base_interval_ms": base_interval,
                "source_feature_csv": str(feature_path.resolve()),
                "source_feature_sha256": file_sha256(feature_path),
                "source_selection_corpus_sha256": file_sha256(corpus_path),
            },
        )
        paired_corpora[symbol] = str(paired_corpus_path.resolve())

    boundaries: set[int] = set()
    for source in source_segments:
        _, timestamp_to_index, base_interval, corpus_target_bucket = source_rows[
            source["symbol"]
        ]
        if source["entry_regime"] != corpus_target_bucket:
            raise ValueError(
                f"replay segment/corpus target bucket mismatch for "
                f"{source['source_segment_id']}"
            )
        start = source["start_timestamp_ms"]
        end = source["end_timestamp_ms"]
        if start not in timestamp_to_index or end not in timestamp_to_index:
            raise ValueError(
                f"feature interval missing for {source['source_segment_id']}:"
                f"{source['symbol']}"
            )
        if timestamp_to_index[start] > timestamp_to_index[end]:
            raise ValueError(
                f"feature interval invalid for {source['source_segment_id']}"
            )
        timestamps = [
            row.timestamp
            for row in source_rows[source["symbol"]][0][
                timestamp_to_index[start] : timestamp_to_index[end] + 1
            ]
        ]
        if any(
            current - previous != base_interval
            for previous, current in zip(timestamps, timestamps[1:])
        ):
            raise ValueError(
                f"feature interval not contiguous for {source['source_segment_id']}"
            )
        source["base_interval_ms"] = base_interval
        boundaries.add(start)
        boundaries.add(end + base_interval)

    atomic_windows: list[dict[str, Any]] = []
    sorted_boundaries = sorted(boundaries)
    for start, next_boundary in zip(sorted_boundaries, sorted_boundaries[1:]):
        active = [
            source
            for source in source_segments
            if source["start_timestamp_ms"] <= start
            and source["end_timestamp_ms"] + source["base_interval_ms"]
            >= next_boundary
        ]
        if not active:
            continue
        active_by_symbol: dict[str, dict[str, Any]] = {}
        for source in active:
            if source["symbol"] in active_by_symbol:
                raise ValueError(
                    f"ambiguous active replay segment for {source['symbol']}"
                )
            active_by_symbol[source["symbol"]] = source
        intervals = {source["base_interval_ms"] for source in active}
        if len(intervals) != 1:
            raise ValueError(
                "active per-symbol replay intervals use incompatible bar intervals"
            )
        base_interval = next(iter(intervals))
        if (next_boundary - start) % base_interval != 0:
            raise ValueError("replay boundary is not aligned to common bar interval")
        end = next_boundary - base_interval
        signature = tuple(
            (
                source["symbol"],
                source["entry_regime"],
                source["source_segment_id"],
            )
            for source in sorted(active, key=lambda item: item["symbol"])
        )
        if (
            atomic_windows
            and atomic_windows[-1]["signature"] == signature
            and atomic_windows[-1]["end_timestamp_ms"] + base_interval == start
        ):
            atomic_windows[-1]["end_timestamp_ms"] = end
        else:
            atomic_windows.append(
                {
                    "start_timestamp_ms": start,
                    "end_timestamp_ms": end,
                    "base_interval_ms": base_interval,
                    "signature": signature,
                    "active": sorted(active, key=lambda item: item["symbol"]),
                }
            )

    blocks: list[dict[str, Any]] = []
    source_to_blocks: dict[str, list[str]] = {
        item["source_segment_id"]: [] for item in source_segments
    }
    for window in atomic_windows:
        start = window["start_timestamp_ms"]
        end = window["end_timestamp_ms"]
        active = window["active"]
        block_cells = [
            {"symbol": item["symbol"], "entry_regime": item["entry_regime"]}
            for item in active
        ]
        block_id = "block-" + canonical_sha256(
            {
                "start_timestamp_ms": start,
                "end_timestamp_ms": end,
                "cells": block_cells,
            }
        )[:16]
        executions: list[dict[str, Any]] = []
        cells: list[dict[str, str]] = []
        for run in active:
            symbol = run["symbol"]
            regime = run["entry_regime"]
            rows, timestamp_to_index, base_interval, _ = source_rows[symbol]
            if start not in timestamp_to_index or end not in timestamp_to_index:
                raise ValueError(f"feature interval missing for {block_id}:{symbol}")
            start_index = timestamp_to_index[start]
            end_index = timestamp_to_index[end]
            timestamps = [row.timestamp for row in rows[start_index : end_index + 1]]
            if start_index > end_index or any(
                current - previous != base_interval
                for previous, current in zip(timestamps, timestamps[1:])
            ):
                raise ValueError(f"feature interval not contiguous for {block_id}:{symbol}")
            execution_path = (
                output_dir
                / "execution_inputs"
                / f"{_safe_name(block_id)}-{_safe_name(symbol)}.csv"
            )
            segment = ReplaySegment(
                start_index=start_index,
                end_index=end_index,
                start_timestamp=start,
                end_timestamp=end,
                bars=end_index - start_index + 1,
            )
            warmup = write_replay_csv(
                rows,
                segment,
                symbol,
                execution_path,
                base_interval,
                warmup_context_bars=0,
            )
            if warmup != 0:
                raise ValueError("benchmark materialization unexpectedly used warmup")
            event_sha = file_sha256(execution_path)
            identity = replay_segment_identity(
                symbol=symbol,
                target_bucket=regime,
                base_interval_ms=base_interval,
                segment=ReplaySegment(0, segment.bars - 1, start, end, segment.bars),
                replay_csv_sha256=event_sha,
            )
            source_to_blocks[run["source_segment_id"]].append(block_id)
            executions.append(
                {
                    "execution_id": f"{block_id}:{symbol}",
                    "symbol": symbol,
                    "path": str(execution_path.resolve()),
                    "event_sha256": event_sha,
                    "segment_identity_sha256": identity["sha256"],
                }
            )
            cells.append({"symbol": symbol, "entry_regime": regime})
        if len(executions) == 1:
            block_path = pathlib.Path(executions[0]["path"])
        else:
            block_path = output_dir / "execution_inputs" / f"{block_id}.json"
            _atomic_write_json(
                block_path,
                {
                    "schema_version": "decision_evidence_event_bundle_v1",
                    "block_id": block_id,
                    "executions": [
                        {
                            "execution_id": item["execution_id"],
                            "symbol": item["symbol"],
                            "event_sha256": item["event_sha256"],
                        }
                        for item in executions
                    ],
                },
            )
        blocks.append(
            {
                "block_id": block_id,
                "start_timestamp_ms": start,
                "end_timestamp_ms": end,
                "path": str(block_path.resolve()),
                "event_sha256": file_sha256(block_path),
                "cells": cells,
                "executions": executions,
                "source_segment_ids": sorted(
                    item["source_segment_id"] for item in active
                ),
            }
        )
    blocks_by_id = {block["block_id"]: block for block in blocks}
    coverage_segments = []
    for source in sorted(
        source_segments,
        key=lambda item: (
            item["start_timestamp_ms"],
            item["end_timestamp_ms"],
            item["symbol"],
            item["entry_regime"],
        ),
    ):
        block_ids = source_to_blocks[source["source_segment_id"]]
        covered = [blocks_by_id[block_id] for block_id in block_ids]
        fully_materialized = bool(covered) and (
            covered[0]["start_timestamp_ms"] == source["start_timestamp_ms"]
            and covered[-1]["end_timestamp_ms"] == source["end_timestamp_ms"]
            and all(
                current["start_timestamp_ms"]
                == previous["end_timestamp_ms"] + source["base_interval_ms"]
                for previous, current in zip(covered, covered[1:])
            )
        )
        coverage_segments.append(
            {
                "source_segment_id": source["source_segment_id"],
                "symbol": source["symbol"],
                "entry_regime": source["entry_regime"],
                "start_timestamp_ms": source["start_timestamp_ms"],
                "end_timestamp_ms": source["end_timestamp_ms"],
                "block_ids": block_ids,
                "fully_materialized": fully_materialized,
            }
        )
    if any(not item["fully_materialized"] for item in coverage_segments):
        raise ValueError("common block calendar did not fully materialize source segments")
    coverage = {
        "schema_version": "decision_evidence_common_block_calendar_v1",
        "source_segment_count": len(source_segments),
        "atomic_block_count": len(blocks),
        "source_segments_fully_materialized": sum(
            1 for item in coverage_segments if item["fully_materialized"]
        ),
        "source_segments": coverage_segments,
    }
    return blocks, paired_corpora, coverage


def build_decision_benchmark(
    *,
    replay_report: pathlib.Path,
    feature_csv: pathlib.Path,
    corpus_manifest: pathlib.Path,
    runtime_config: pathlib.Path,
    replay_config: pathlib.Path,
    candidate_model: pathlib.Path,
    candidate_report: pathlib.Path,
    validation_config: pathlib.Path,
    trade_bot: pathlib.Path,
    output_dir: pathlib.Path,
    manifest_path: pathlib.Path,
    build_report_path: pathlib.Path,
    feature_csv_by_symbol: dict[str, pathlib.Path] | None = None,
    corpus_manifest_by_symbol: dict[str, pathlib.Path] | None = None,
) -> dict[str, Any]:
    output_dir = pathlib.Path(output_dir).resolve(strict=False)
    manifest_path = pathlib.Path(manifest_path).resolve(strict=False)
    build_report_path = pathlib.Path(build_report_path).resolve(strict=False)
    manifest_path.unlink(missing_ok=True)
    report: dict[str, Any] = {
        "schema_version": BUILD_REPORT_SCHEMA,
        "status": "UNVERIFIABLE",
        "manifest": str(manifest_path),
        "candidate_preflight": {},
        "paired_inputs": {},
        "validation": {"identity_status": "UNVERIFIABLE", "drifts": []},
        "errors": [],
    }
    required = {
        "replay_report": pathlib.Path(replay_report),
        "runtime_config": pathlib.Path(runtime_config),
        "replay_config": pathlib.Path(replay_config),
        "candidate_model": pathlib.Path(candidate_model),
        "candidate_report": pathlib.Path(candidate_report),
        "validation_config": pathlib.Path(validation_config),
        "trade_bot": pathlib.Path(trade_bot),
    }
    for name, path in required.items():
        if not path.is_file():
            report["errors"].append(f"input.{name}_missing")
    candidate_preflight = validate_integrator_candidate(
        required["candidate_model"], required["candidate_report"]
    )
    report["candidate_preflight"] = candidate_preflight
    report["errors"].extend(candidate_preflight["errors"])
    if report["errors"]:
        _atomic_write_json(build_report_path, report)
        return report

    try:
        replay = _read_json_object(required["replay_report"])
        candidate_identity = replay.get("candidate_identity")
        if not isinstance(candidate_identity, dict):
            raise ValueError("replay candidate identity missing")
        for field, expected in (
            ("model_version", candidate_preflight["model_version"]),
            ("model_sha256", candidate_preflight["candidate_model"]["sha256"]),
            (
                "integrator_report_sha256",
                candidate_preflight["candidate_report"]["sha256"],
            ),
        ):
            if candidate_identity.get(field) != expected:
                raise ValueError(f"replay candidate identity mismatch: {field}")
        raw_runs = replay.get("runs")
        symbols = {
            str(run.get("symbol") or "").strip().upper()
            for run in raw_runs
            if isinstance(raw_runs, list) and isinstance(run, dict)
        }
        symbols.discard("")
        if not symbols:
            raise ValueError("replay report symbols missing")
        bound_corpora = _validate_frozen_corpus_binding(replay, symbols)
        selected_corpus_mapping = _normalize_mapping(corpus_manifest_by_symbol)
        if not selected_corpus_mapping:
            selected_corpus_mapping = dict(bound_corpora)
        features, corpora = _resolve_sources(
            symbols,
            pathlib.Path(feature_csv),
            pathlib.Path(corpus_manifest),
            _normalize_mapping(feature_csv_by_symbol),
            selected_corpus_mapping,
        )
        for symbol in sorted(symbols):
            if corpora[symbol] != bound_corpora[symbol]:
                raise ValueError(
                    f"replay frozen corpus path mismatch for {symbol}: "
                    f"expected={bound_corpora[symbol]},actual={corpora[symbol]}"
                )
            if file_sha256(corpora[symbol]) != file_sha256(bound_corpora[symbol]):
                raise ValueError(f"replay frozen corpus hash mismatch for {symbol}")
        blocks, paired_corpora, calendar_coverage = _build_universe(
            replay_report=replay,
            feature_paths=features,
            corpus_paths=corpora,
            output_dir=output_dir,
        )
        replay_split_identity = _write_replay_split_identity(replay, output_dir)
        data_files = [
            (f"execution:{item['execution_id']}", pathlib.Path(item["path"]))
            for block in blocks
            for item in block["executions"]
        ]
        components = {
            "data": _component("data", data_files),
            "split": _component(
                "split",
                [("replay_validation_report", replay_split_identity)]
                + [(f"corpus:{symbol}", path) for symbol, path in corpora.items()],
            ),
            "cost": _component(
                "cost",
                [
                    ("replay_candidate_config", required["replay_config"]),
                    ("runtime_config", required["runtime_config"]),
                ],
            ),
            "features": _component(
                "features",
                [(f"feature:{symbol}", path) for symbol, path in features.items()],
            ),
            "actions": _component(
                "actions",
                [
                    ("replay_policy", required["replay_config"]),
                    ("runtime_policy", required["runtime_config"]),
                ],
            ),
            "baseline_policy": _component(
                "baseline_policy",
                [
                    ("candidate_model", required["candidate_model"]),
                    ("candidate_report", required["candidate_report"]),
                ],
            ),
            "run_config": _component(
                "run_config",
                [
                    ("decision_evidence_validation", required["validation_config"]),
                    ("runtime_config", required["runtime_config"]),
                ],
            ),
            "implementation": _component(
                "implementation",
                [
                    ("benchmark_builder", pathlib.Path(__file__)),
                    (
                        "paired_evolution_runner",
                        pathlib.Path(__file__).with_name("run_paired_evolution_replay.py"),
                    ),
                    (
                        "replay_validation_runner",
                        pathlib.Path(__file__).with_name("run_replay_validation.py"),
                    ),
                    ("trade_bot", required["trade_bot"]),
                ],
            ),
        }
        manifest = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "components": components,
            "evaluation_universe": {
                "blocks": blocks,
                "calendar_coverage": calendar_coverage,
            },
        }
        _atomic_write_json(manifest_path, manifest)
        validation = validate_files(
            manifest_path,
            output_dir,
            required["validation_config"],
        )
        report["validation"] = validation
        report["paired_inputs"] = {
            "feature_csv": str(features[next(iter(symbols))].resolve())
            if len(symbols) == 1
            else "",
            "corpus_manifest": paired_corpora[next(iter(symbols))]
            if len(symbols) == 1
            else "",
            "feature_csv_by_symbol": {
                symbol: str(path.resolve()) for symbol, path in sorted(features.items())
            },
            "corpus_manifest_by_symbol": dict(sorted(paired_corpora.items())),
            "source_corpus_manifest_by_symbol": {
                symbol: str(path.resolve())
                for symbol, path in sorted(corpora.items())
            },
        }
        if validation.get("identity_status") != "VERIFIED":
            report["errors"].append("self_validation_failed")
        else:
            report["status"] = "VERIFIED"
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        report["errors"].append(f"build_failed:{type(exc).__name__}:{exc}")
    _atomic_write_json(build_report_path, report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-preflight-only", action="store_true")
    parser.add_argument("--replay-report", default="")
    parser.add_argument("--feature-csv", default="")
    parser.add_argument("--feature-csv-by-symbol", default="")
    parser.add_argument("--corpus-manifest", default="")
    parser.add_argument("--corpus-manifest-by-symbol", default="")
    parser.add_argument("--runtime-config", default="")
    parser.add_argument("--replay-config", default="")
    parser.add_argument("--candidate-model", required=True)
    parser.add_argument("--candidate-report", required=True)
    parser.add_argument("--validation-config", default="")
    parser.add_argument("--trade-bot", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--manifest", default="")
    parser.add_argument("--build-report", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.candidate_preflight_only:
        report = validate_integrator_candidate(
            pathlib.Path(args.candidate_model), pathlib.Path(args.candidate_report)
        )
        _atomic_write_json(pathlib.Path(args.build_report), report)
        return 0 if report["status"] == "VERIFIED" else 2
    report = build_decision_benchmark(
        replay_report=pathlib.Path(args.replay_report),
        feature_csv=pathlib.Path(args.feature_csv),
        feature_csv_by_symbol=parse_symbol_mapping(args.feature_csv_by_symbol),
        corpus_manifest=pathlib.Path(args.corpus_manifest),
        corpus_manifest_by_symbol=parse_symbol_mapping(
            args.corpus_manifest_by_symbol
        ),
        runtime_config=pathlib.Path(args.runtime_config),
        replay_config=pathlib.Path(args.replay_config),
        candidate_model=pathlib.Path(args.candidate_model),
        candidate_report=pathlib.Path(args.candidate_report),
        validation_config=pathlib.Path(args.validation_config),
        trade_bot=pathlib.Path(args.trade_bot),
        output_dir=pathlib.Path(args.output_dir),
        manifest_path=pathlib.Path(args.manifest),
        build_report_path=pathlib.Path(args.build_report),
    )
    return 0 if report["status"] == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
