#!/usr/bin/env python3
"""Select one independently gated alpha route without cross-source leakage."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import tempfile
from typing import Any, Dict, Mapping


SCHEMA_VERSION = "alpha_source_route_v1"


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def artifact(path: pathlib.Path) -> Dict[str, Any]:
    return {"path": str(path.resolve()), "sha256": sha256_file(path)}


def market_status(payload: Mapping[str, Any]) -> str:
    if (
        payload.get("schema_version") == "market_alpha_development_verification_v1"
        and payload.get("status") == "PASS"
        and payload.get("fully_verifiable") is True
        and payload.get("economic_screen", {}).get("development_passed") is True
        and payload.get("promotion_evidence") is False
        and payload.get("promotion_eligible") is False
    ):
        return "READY"
    raw = str(payload.get("status") or "").upper()
    return "NOT_READY" if raw == "NOT_READY" else "REJECTED"


def micro_status(payload: Mapping[str, Any]) -> str:
    candidate_id = str(payload.get("candidate_id") or "")
    if (
        payload.get("schema_version") == "microstructure_alpha_lifecycle_v1"
        and payload.get("status") == "PASS"
        and payload.get("fully_verifiable") is True
        and payload.get("phase") == "demo_ready"
        and payload.get("promotion_eligible") is False
        and payload.get("demo_entry_eligible") is True
        and payload.get("live_promotion_eligible") is False
        and len(candidate_id) == 64
        and all(character in "0123456789abcdef" for character in candidate_id)
    ):
        return "READY"
    raw = str(payload.get("status") or "").upper()
    return "NOT_READY" if raw == "NOT_READY" else "REJECTED"


def select(
    market_path: pathlib.Path,
    lifecycle_path: pathlib.Path,
) -> Dict[str, Any]:
    market = read_json(market_path)
    micro = read_json(lifecycle_path)
    market_readiness = market_status(market)
    micro_readiness = micro_status(micro)
    # This precedence is fixed before either source's future holdout is seen.
    # We never compare returns across their incompatible research domains.
    if micro_readiness == "READY":
        selected = "microstructure_demo"
        status = "PASS"
        reason = "fixed_precedence_demo_ready_microstructure"
    elif market_readiness == "READY":
        selected = "legacy_integrator"
        status = "PASS"
        reason = "legacy_integrator_only_ready_route"
    else:
        selected = None
        status = (
            "NOT_READY"
            if "NOT_READY" in {market_readiness, micro_readiness}
            else "FAIL"
        )
        reason = "no_independently_gated_alpha_source_ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "selected_route": selected,
        "selection_policy": {
            "method": "fixed_predeclared_precedence",
            "precedence": ["microstructure_demo", "legacy_integrator"],
            "cross_source_return_comparison_permitted": False,
            "nonselected_source_failure_blocks_selected_route": False,
            "live_promotion_eligible": False,
        },
        "sources": {
            "legacy_integrator": {
                "readiness": market_readiness,
                "evidence": artifact(market_path),
                "evidence_domain": "development_only_then_legacy_route_gates",
            },
            "microstructure_demo": {
                "readiness": micro_readiness,
                "candidate_id": micro.get("candidate_id"),
                "phase": micro.get("phase"),
                "evidence": artifact(lifecycle_path),
                "evidence_domain": "development_selection_holdout_raw_replay",
            },
        },
        "reason": reason,
        "demo_only": selected == "microstructure_demo",
        "live_promotion_eligible": False,
    }


def atomic_write(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-alpha-report", required=True)
    parser.add_argument("--microstructure-lifecycle-report", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = pathlib.Path(args.output).resolve()
    try:
        payload = select(
            pathlib.Path(args.market_alpha_report).resolve(),
            pathlib.Path(args.microstructure_lifecycle_report).resolve(),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAIL",
            "selected_route": None,
            "reason": f"alpha_source_evidence_invalid:{exc}",
            "live_promotion_eligible": False,
        }
    atomic_write(output, payload)
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
