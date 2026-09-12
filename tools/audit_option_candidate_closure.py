#!/usr/bin/env python3
"""Verify a pinned STOP artifact and enforce a versioned, irreversible closure.

The batch economic auditor stays unchanged. This governance layer never grants
promotion, even if a later diagnostic batch passes. No market/account API calls.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import pathlib
import re
import zipfile
from typing import Any

import audit_option_lifecycle_economics_v1 as economics
import capture_bybit_option_lifecycle_v4 as capture


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = "option_candidate_closure_audit_v1"
REGISTRY_SHA256 = "61806c18be984ce4d68582bf6b2e91f1912edae4a87293e8ad6f76ba06b0f3b2"
AUTHORITY_FIELDS = ("promotion_authority", "demo_activation_authorized", "live_activation_authorized")
CLAIM_FIELDS = ("profitability_claim_allowed", "sharpe_claim_allowed", "drawdown_claim_allowed")
FILES = {"report.json", "payoff.json", "economics.json"}
MAX_BYTES = 2 * 1024 * 1024
MONEY_FIELDS = (
    "gross_pnl_usdt", "option_spread_cost_usdt", "option_fee_usdt",
    "delivery_fee_usdt", "hedge_spread_cost_usdt", "hedge_fee_usdt",
    "stress_increment_usdt", "base_net_pnl_usdt", "stress_net_pnl_usdt",
    "capital_normalizer_usdt", "base_net_bps", "stress_net_bps",
)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON field")
        result[key] = value
    return result


def decode(raw: bytes) -> dict[str, Any]:
    require(len(raw) <= MAX_BYTES, "JSON byte budget exceeded")
    def invalid_constant(_: str) -> None:
        raise ValueError("nonfinite JSON constant")
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    require(isinstance(value, dict), "JSON object required")
    return value


def read(path: pathlib.Path) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "file byte budget exceeded")
    return raw


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def number(value: Any) -> float:
    require(type(value) in (int, float) and math.isfinite(value), "finite numeric field required")
    return float(value)


def equal(left: Any, right: Any) -> None:
    require(math.isclose(number(left), number(right), rel_tol=1e-10, abs_tol=1e-8),
            "economic accounting or aggregate mismatch")


def no_authority(report: dict[str, Any], *, claims: bool = True) -> None:
    for field in AUTHORITY_FIELDS + (CLAIM_FIELDS if claims else ()):
        require(report.get(field) is False, "forbidden or missing claim/authority")


def load_registry(path: pathlib.Path) -> dict[str, Any]:
    registry = decode(read(path))
    require(capture.canonical_sha256(registry) == REGISTRY_SHA256, "closure registry identity mismatch")
    require(registry["status"] == "CLOSED" and registry["reopening_allowed"] is False,
            "closure cannot be reopened")
    no_authority(registry, claims=False)
    return registry


def load_anchor(raw: bytes, registry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    require(sha(raw) == registry["closure_anchor"]["artifact_zip_sha256"], "anchor ZIP digest mismatch")
    reports, hashes = {}, {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        require(len(entries) == 3 and {entry.filename for entry in entries} == FILES,
                "anchor ZIP must contain exactly the three aggregate reports")
        require(sum(entry.file_size for entry in entries) <= MAX_BYTES, "uncompressed ZIP byte budget exceeded")
        for entry in entries:
            require((entry.external_attr >> 16) & 0o170000 != 0o120000, "ZIP symlinks forbidden")
            payload = archive.read(entry)
            reports[entry.filename] = decode(payload)
            hashes[entry.filename] = sha(payload)
    return reports, hashes


def validate_economics(report: dict[str, Any], registry: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    require(report["schema_version"] == economics.SCHEMA_VERSION, "economic report schema mismatch")
    identities = report["identities"]
    require(identities["experiment_id"] == registry["candidate_experiment_id"], "unregistered candidate identity")
    for report_key, registry_key in (("policy_canonical_sha256", "economic_policy_canonical_sha256"),
                                     ("manifest_canonical_sha256", "economic_manifest_canonical_sha256")):
        require(identities[report_key] == registry[registry_key], "candidate contract identity mismatch")
    require(re.fullmatch(r"[0-9a-f]{40}", identities["executed_release_sha"]) is not None, "release SHA invalid")
    require(re.fullmatch(r"[0-9a-f]{64}", identities["archive_input_set_sha256"]) is not None, "archive SHA invalid")
    no_authority(report)
    require(report["archive_integrity"]["invalid_segment_count"] == 0, "invalid current or anchor archive")
    rows = report["lifecycles"]
    require(isinstance(rows, list) and 0 < len(rows) <= 10000, "lifecycle population invalid")
    ids = [row["lifecycle_id"] for row in rows]
    require(all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) for value in ids)
            and len(ids) == len(set(ids)), "invalid or duplicate lifecycle identity")
    require(all(row["state"] in {"complete", "pending", "insufficient"} for row in rows), "unknown lifecycle state")
    require(rows == sorted(rows, key=lambda row: row["selected_epoch_ms"]), "lifecycle order drift")
    completed = [row for row in rows if row["state"] == "complete"]
    for state, field in (("complete", "complete_lifecycle_count"), ("pending", "pending_lifecycle_count"),
                         ("insufficient", "insufficient_lifecycle_count")):
        require(type(report["progress"][field]) is int and report["progress"][field] == sum(row["state"] == state for row in rows),
                "lifecycle progress mismatch")
    require(report["progress"]["observed_lifecycle_count"] == len(rows), "observed count mismatch")
    require(report["progress"]["minimum_complete_lifecycles"] == policy["aggregation_contract"]["minimum_complete_lifecycles"], "minimum drift")
    for row in completed:
        actions = row["actions"]
        require([action["action_id"] for action in actions] == policy["source_contract"]["action_ids"], "action population mismatch")
        require(row["paired_delivery_evidence_valid"] is True, "paired delivery invalid")
        for action in actions:
            if action["action_id"] == "no_trade":
                for field in ("gross_pnl_usdt", "base_net_pnl_usdt", "stress_net_pnl_usdt", "base_net_bps", "stress_net_bps"):
                    equal(action[field], 0.0)
                continue
            values = {field: number(action[field]) for field in MONEY_FIELDS}
            costs = sum(values[field] for field in MONEY_FIELDS[1:6])
            require(all(values[field] >= 0.0 for field in MONEY_FIELDS[1:7]), "negative cost")
            require(values["capital_normalizer_usdt"] > 0.0, "nonpositive notional normalizer")
            equal(values["base_net_pnl_usdt"], values["gross_pnl_usdt"] - costs)
            equal(values["stress_net_pnl_usdt"], values["base_net_pnl_usdt"] - values["stress_increment_usdt"])
            for kind in ("base", "stress"):
                equal(values[f"{kind}_net_bps"], values[f"{kind}_net_pnl_usdt"] / values["capital_normalizer_usdt"] * 10000.0)
        equal(actions[1]["gross_pnl_usdt"] + actions[2]["gross_pnl_usdt"], 0.0)
    aggregates = economics.aggregate_actions(completed, policy["aggregation_contract"]) if completed else {}
    require(set(report["action_aggregates"]) == set(aggregates), "aggregate action population mismatch")
    for action_id, expected in aggregates.items():
        require(set(report["action_aggregates"][action_id]) == set(expected), "aggregate field mismatch")
        for field, value in expected.items():
            equal(report["action_aggregates"][action_id][field], value)
    require(report["decision"] in policy["decision_contract"].values(), "unknown diagnostic decision")
    return completed


def audit(*, registry_path: pathlib.Path, anchor_zip: pathlib.Path, current_path: pathlib.Path) -> dict[str, Any]:
    registry = load_registry(registry_path)
    policy, _, _ = economics.load_contract(
        ROOT / "config/option_lifecycle_economic_v1.json", ROOT / "config/option_lifecycle_economic_manifest_v1.json",
        ROOT / "config/option_lifecycle_payoff_v2.json", ROOT / "config/option_lifecycle_payoff_manifest_v2.json")
    reports, hashes = load_anchor(read(anchor_zip), registry)
    anchor = reports["economics.json"]
    completed = validate_economics(anchor, registry, policy)
    expected = registry["closure_anchor"]
    require(len(completed) == expected["complete_lifecycle_count"] and anchor["progress"]["insufficient_lifecycle_count"] == 0,
            "anchor completed cohort mismatch")
    require(anchor["decision"] == expected["decision"] and anchor["reason_code"] == "STRESS_EDGE_FUTILITY_BOUND_NONPOSITIVE"
            and anchor["demo_review_eligible"] is False and anchor["economic_evidence"] is True,
            "anchor is not a completed economic STOP")
    primary = registry["primary_action_id"]
    require(anchor["action_aggregates"][primary]["stress_mean_ucb_bps"] <= 0.0, "anchor futility condition failed")
    source = policy["source_contract"]
    for name, schema, policy_sha, manifest_sha in (
        ("report.json", "option_lifecycle_audit_v4", source["capture_policy_canonical_sha256"], source["capture_manifest_canonical_sha256"]),
        ("payoff.json", "option_lifecycle_payoff_audit_v2", source["payoff_policy_canonical_sha256"], source["payoff_manifest_canonical_sha256"]),
        ("economics.json", economics.SCHEMA_VERSION, registry["economic_policy_canonical_sha256"], registry["economic_manifest_canonical_sha256"]),
    ):
        report = reports[name]
        require(report["schema_version"] == schema, "anchor schema mismatch")
        require(report["identities"]["executed_release_sha"] == expected["executed_release_sha"], "anchor release mismatch")
        require(report["identities"]["policy_canonical_sha256"] == policy_sha and report["identities"]["manifest_canonical_sha256"] == manifest_sha,
                "anchor source contract mismatch")
        no_authority(report, claims=name == "economics.json")
        if name == "payoff.json":
            require(all(report.get(field) is False for field in ("profitability_evidence", "sharpe_evidence", "drawdown_evidence")), "payoff claims evidence")
    require(reports["report.json"]["decision"] == "PASS_FIRST_COMPLETE_LIFECYCLE_FOR_PAYOFF_RECONSTRUCTION_ONLY", "anchor lifecycle not complete")
    require(reports["payoff.json"]["decision"] == "RECONCILED_FIRST_LIFECYCLE_PAYOFF_FOR_DIAGNOSTIC_ONLY", "anchor payoff not reconciled")
    require(reports["payoff.json"]["lifecycle"]["lifecycle_id"] == completed[0]["lifecycle_id"], "first lifecycle cross-report mismatch")
    require(len(reports["payoff.json"]["actions"]) == len(completed[0]["actions"]), "first payoff action population mismatch")
    for action, original in zip(completed[0]["actions"], reports["payoff.json"]["actions"]):
        require(action["action_id"] == original["action_id"], "first payoff action mismatch")
        for field in ("gross_pnl_usdt", "base_net_pnl_usdt", "stress_net_pnl_usdt"):
            equal(action[field], original[field])
    current_raw = read(current_path)
    current = decode(current_raw)
    validate_economics(current, registry, policy)
    selected = []
    for row in completed:
        action = next(value for value in row["actions"] if value["action_id"] == primary)
        selected.append({"lifecycle_id": row["lifecycle_id"], "selected_epoch_ms": row["selected_epoch_ms"],
                         "delivery_time_epoch_ms": row["delivery_time_epoch_ms"],
                         "primary_payoff": {key: action[key] for key in MONEY_FIELDS}})
    return {
        "schema_version": SCHEMA, "decision": "CLOSED_CANDIDATE_NO_REOPEN",
        "candidate_experiment_id": registry["candidate_experiment_id"], "registry_canonical_sha256": REGISTRY_SHA256,
        "closure_anchor": expected, "anchor_report_file_sha256": hashes,
        "anchor_archive_input_set_sha256": anchor["identities"]["archive_input_set_sha256"],
        "current_report_sha256": sha(current_raw), "current_executed_release_sha": current["identities"]["executed_release_sha"],
        "current_batch_decision": current["decision"], "current_complete_lifecycle_count": current["progress"]["complete_lifecycle_count"],
        "closure_evidence_verified": True, "closure_latched": True, "demo_review_eligible": False,
        "anchor_action_aggregates": anchor["action_aggregates"], "anchor_lifecycles": selected,
        "full_cashflow_attribution_complete": False, "account_risk_qualified": False,
        **{field: False for field in AUTHORITY_FIELDS + CLAIM_FIELDS},
    }


def annotations(report: dict[str, Any]) -> list[str]:
    # Eight bounded records, with only validated aggregate research data: no raw
    # archive rows, arbitrary report fields, paths, credentials or account data.
    header = {key: value for key, value in report.items() if key not in {"anchor_action_aggregates", "anchor_lifecycles"}}
    records = [{"kind": "closure", "data": header}, {"kind": "aggregates", "data": report["anchor_action_aggregates"]}]
    records += [{"kind": "lifecycle", "data": row} for row in report["anchor_lifecycles"]]
    lines = []
    for record in records:
        rendered = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
        require(len(rendered.encode()) <= 3500, "public annotation byte budget exceeded")
        escaped = rendered.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        lines.append(f"::notice title=Option candidate closure::{escaped}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=pathlib.Path, required=True)
    parser.add_argument("--anchor-zip", type=pathlib.Path, required=True)
    parser.add_argument("--current", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--emit-annotations", action="store_true")
    args = parser.parse_args()
    try:
        require(args.output.resolve() not in {args.registry.resolve(), args.anchor_zip.resolve(), args.current.resolve()}, "output cannot overwrite input")
        report = audit(registry_path=args.registry, anchor_zip=args.anchor_zip, current_path=args.current)
        lines = annotations(report) if args.emit_annotations else []
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
        print(f"candidate closure failed closed: {type(exc).__name__}: {exc}")
        return 2
    economics._atomic_write(args.output, report)
    for line in lines:
        print(line)
    print(report["decision"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
