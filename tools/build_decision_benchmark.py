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


def _build_universe(
    *,
    replay_report: dict[str, Any],
    feature_paths: dict[str, pathlib.Path],
    corpus_paths: dict[str, pathlib.Path],
    output_dir: pathlib.Path,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    raw_runs = replay_report.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError("replay report runs missing")
    grouped: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
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
        by_symbol = grouped.setdefault((start, end), {})
        if symbol in by_symbol:
            raise ValueError(f"duplicate replay run interval for {symbol}")
        by_symbol[symbol] = {"entry_regime": regime}

    previous_end: int | None = None
    for start, end in sorted(grouped):
        if previous_end is not None and start <= previous_end:
            raise ValueError("replay run intervals overlap")
        previous_end = end

    source_rows: dict[str, tuple[list[Any], dict[int, int], int, str]] = {}
    paired_corpora: dict[str, str] = {}
    for symbol in sorted({symbol for group in grouped.values() for symbol in group}):
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

    blocks: list[dict[str, Any]] = []
    for start, end in sorted(grouped):
        block_id = "block-" + canonical_sha256(
            {"start_timestamp_ms": start, "end_timestamp_ms": end}
        )[:16]
        executions: list[dict[str, Any]] = []
        cells: list[dict[str, str]] = []
        for symbol, run in sorted(grouped[(start, end)].items()):
            rows, timestamp_to_index, base_interval, target_bucket = source_rows[symbol]
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
                target_bucket=target_bucket,
                base_interval_ms=base_interval,
                segment=ReplaySegment(0, segment.bars - 1, start, end, segment.bars),
                replay_csv_sha256=event_sha,
            )
            regime = run["entry_regime"]
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
            }
        )
    return blocks, paired_corpora


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
        features, corpora = _resolve_sources(
            symbols,
            pathlib.Path(feature_csv),
            pathlib.Path(corpus_manifest),
            _normalize_mapping(feature_csv_by_symbol),
            _normalize_mapping(corpus_manifest_by_symbol),
        )
        blocks, paired_corpora = _build_universe(
            replay_report=replay,
            feature_paths=features,
            corpus_paths=corpora,
            output_dir=output_dir,
        )
        data_files = [
            (f"execution:{item['execution_id']}", pathlib.Path(item["path"]))
            for block in blocks
            for item in block["executions"]
        ]
        components = {
            "data": _component("data", data_files),
            "split": _component(
                "split",
                [("replay_validation_report", required["replay_report"])]
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
            "evaluation_universe": {"blocks": blocks},
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
