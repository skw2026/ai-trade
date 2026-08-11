#!/usr/bin/env python3
"""Validate a frozen decision-evidence benchmark manifest."""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
from typing import Any

from decision_evidence_common import (
    REPORT_SCHEMA_VERSION,
    file_sha256,
    validate_benchmark,
)


EXPECTED_CONFIG = {
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


def _read_json_object(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _invalid_input_report(component: str, field: str, actual: Any) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "identity_status": "UNVERIFIABLE",
        "drifts": [
            {
                "component": component,
                "logical_name": "",
                "field": field,
                "expected": "valid JSON object",
                "actual": actual,
            }
        ],
    }


def validate_files(
    manifest_path: pathlib.Path,
    root: pathlib.Path,
    config_path: pathlib.Path,
) -> dict[str, Any]:
    try:
        manifest = _read_json_object(manifest_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _invalid_input_report("manifest", "input", str(exc))

    report = validate_benchmark(manifest, root)
    try:
        config = _read_json_object(config_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        config = None
        config_actual: Any = str(exc)
    else:
        config_actual = config

    if config != EXPECTED_CONFIG:
        report.pop("benchmark_id", None)
        report.pop("canonical_identity", None)
        report["identity_status"] = "UNVERIFIABLE"
        report["drifts"].append(
            {
                "component": "validation_config",
                "logical_name": "decision_evidence_validation",
                "field": "contract",
                "expected": EXPECTED_CONFIG,
                "actual": config_actual,
            }
        )
        report["drifts"].sort(
            key=lambda item: (
                item["component"], item["logical_name"], item["field"]
            )
        )
    elif config_path.is_file():
        report["validation_config_sha256"] = file_sha256(config_path)
    return report


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(payload, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="benchmark manifest JSON")
    parser.add_argument("--root", required=True, help="root for relative manifest paths")
    parser.add_argument("--config", required=True, help="frozen validation policy JSON")
    parser.add_argument("--output", required=True, help="validation report JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = validate_files(
        pathlib.Path(args.manifest),
        pathlib.Path(args.root),
        pathlib.Path(args.config),
    )
    _write_json(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report["identity_status"] == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
