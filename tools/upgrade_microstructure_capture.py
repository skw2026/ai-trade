#!/usr/bin/env python3
"""Deterministically rebuild superseded feature segments from immutable raw data."""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
from typing import Any, Dict, Mapping

import collect_bybit_microstructure as collector


SOURCE_SCHEMA_VERSION = "bybit_cross_asset_microstructure_v2"


def read_object(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"capture report is not an object: {path}")
    return payload


def resolve_artifact(
    root: pathlib.Path, recorded: str, kind: str, symbol: str
) -> pathlib.Path:
    path = pathlib.Path(recorded)
    return path if path.is_file() else root / kind / symbol / path.name


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
    temporary.replace(path)


def valid_existing_upgrade(
    report_path: pathlib.Path,
    *,
    root: pathlib.Path,
    symbol: str,
    raw_sha256: str,
) -> bool:
    try:
        payload = read_object(report_path)
        feature = payload.get("features", {})
        raw = payload.get("raw", {})
        feature_path = resolve_artifact(
            root, str(feature.get("path") or ""), "features", symbol
        )
        upgrade = payload.get("deterministic_raw_replay_upgrade", {})
        return bool(
            payload.get("schema_version") == collector.SCHEMA_VERSION
            and payload.get("status") == "PASS"
            and str(raw.get("sha256") or "") == raw_sha256
            and feature_path.is_file()
            and collector.sha256_file(feature_path) == feature.get("sha256")
            and upgrade.get("source_schema_version") == SOURCE_SCHEMA_VERSION
            and upgrade.get("target_schema_version") == collector.SCHEMA_VERSION
            and upgrade.get("raw_payload_mutated") is False
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def upgrade(root: pathlib.Path, *, symbol: str) -> Dict[str, Any]:
    report_root = root / "reports" / symbol
    feature_root = root / "features" / symbol
    discovered = sorted(report_root.glob("*.json"))
    eligible = 0
    rebuilt = 0
    reused = 0
    failures: list[str] = []
    outputs: list[Dict[str, Any]] = []
    for source_report_path in discovered:
        try:
            source = read_object(source_report_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{source_report_path.name}:{exc}")
            continue
        if source.get("schema_version") != SOURCE_SCHEMA_VERSION:
            continue
        eligible += 1
        try:
            raw = source.get("raw")
            if not isinstance(raw, dict):
                raise ValueError("raw reference is missing")
            raw_path = resolve_artifact(
                root, str(raw.get("path") or ""), "raw", symbol
            )
            raw_sha256 = str(raw.get("sha256") or "")
            if not (
                source.get("status") == "PASS"
                and source.get("research_domain") == "forward_development_only"
                and source.get("promotion_evidence") is False
                and source.get("promotion_eligible") is False
                and source.get("symbols") == list(collector.CAPTURE_SYMBOLS)
                and raw_path.is_file()
                and len(raw_sha256) == 64
            ):
                raise ValueError("source raw identity contract failed")
            stem = source_report_path.stem
            feature_path = feature_root / f"{stem}.{collector.SCHEMA_VERSION}.csv"
            upgraded_report_path = (
                report_root / f"{stem}.{collector.SCHEMA_VERSION}.json"
            )
            if valid_existing_upgrade(
                upgraded_report_path,
                root=root,
                symbol=symbol,
                raw_sha256=raw_sha256,
            ):
                reused += 1
            else:
                if collector.sha256_file(raw_path) != raw_sha256:
                    raise ValueError("source raw checksum mismatch")
                rows, raw_count = collector.replay_jsonl(
                    raw_path,
                    symbol=symbol,
                    context_symbols=collector.CONTEXT_SYMBOLS,
                    bucket_ms=1000,
                )
                if raw_count != int(raw.get("message_count", -1)):
                    raise ValueError("raw replay message-count mismatch")
                collector.write_feature_csv(feature_path, rows)
                report = collector.build_capture_report(
                    raw_path=raw_path,
                    feature_path=feature_path,
                    rows=rows,
                    raw_count=raw_count,
                    symbol=symbol,
                    url=str(source.get("url") or collector.DEFAULT_URL),
                    derived_from_schema_version=SOURCE_SCHEMA_VERSION,
                )
                atomic_write_json(upgraded_report_path, report)
                rebuilt += 1
            outputs.append(
                {
                    "source_report": str(source_report_path.resolve()),
                    "upgraded_report": str(upgraded_report_path.resolve()),
                    "feature_path": str(feature_path.resolve()),
                    "raw_sha256": raw_sha256,
                }
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{source_report_path.name}:{exc}")
    return {
        "schema_version": "microstructure_capture_upgrade_v1",
        "status": "PASS" if not failures else "FAIL",
        "source_schema_version": SOURCE_SCHEMA_VERSION,
        "target_schema_version": collector.SCHEMA_VERSION,
        "raw_payload_mutated": False,
        "eligible_segment_count": eligible,
        "rebuilt_segment_count": rebuilt,
        "reused_segment_count": reused,
        "outputs": outputs,
        "failures": failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--symbol", default=collector.TARGET_SYMBOL, choices=(collector.TARGET_SYMBOL,))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = upgrade(pathlib.Path(args.root).resolve(), symbol=args.symbol)
    atomic_write_json(pathlib.Path(args.output).resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
