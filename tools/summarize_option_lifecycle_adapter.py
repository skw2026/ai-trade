#!/usr/bin/env python3
"""Validate and emit bounded, release-bound C2 research summary annotations."""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import pathlib
import re
from typing import Any

import adapt_option_lifecycle_subaccount_v1 as adapter


FALSE_FIELDS = (
    "cashflows_qualified", "historical_data_qualified", "account_risk_qualified",
    "economic_qualification", "actual_account_performance", "profitability_claim_allowed",
    "sharpe_claim_allowed", "drawdown_claim_allowed", "new_candidate_registered",
    "forward_wait_started",
)
GAP_REASONS = {
    "ENTRY_OBSERVATION_AVAILABLE_AFTER_DELIVERY",
    "HEDGE_OBSERVATION_AVAILABLE_AFTER_DELIVERY",
    "HEDGE_EXECUTION_DEPTH_INSUFFICIENT",
}


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def summary(report: dict[str, Any], *, expected_release: str) -> dict[str, Any]:
    require(re.fullmatch(r"[0-9a-f]{40}", expected_release) is not None,
            "invalid expected release")
    require(report.get("schema_version") == "option_lifecycle_subaccount_adapter_v1",
            "adapter schema mismatch")
    require(report.get("candidate_state") == "CLOSED", "candidate not closed")
    require(report.get("target_lifecycle_id") == adapter.TARGET_LIFECYCLE,
            "wrong target lifecycle")
    require(all(report.get(key) is False for key in FALSE_FIELDS),
            "forbidden qualification or claim")
    require(set(report.get("authorities", {})) == set(adapter.AUTHORITIES)
            and all(value is False for value in report["authorities"].values()),
            "forbidden authority")
    provenance = report["provenance"]
    expected = {
        "executed_release_sha": expected_release,
        "closure_evidence_sha256": adapter.CLOSURE_EVIDENCE_SHA256,
        "adapter_engine_sha256": digest(pathlib.Path(adapter.__file__)),
        "ledger_engine_sha256": digest(pathlib.Path(adapter.ledger.__file__)),
    }
    require(all(provenance.get(key) == value for key, value in expected.items()),
            "release or engine identity mismatch")
    result = {
        "decision": report["decision"], "target_lifecycle_id": adapter.TARGET_LIFECYCLE,
        "candidate_state": "CLOSED", "provenance": expected,
        **{key: False for key in FALSE_FIELDS}, "authorities": adapter.AUTHORITIES.copy(),
    }
    if report["decision"] == adapter.DECISION:
        require(report.get("ledger_output_written") is True
                and report.get("ledger_status") == "INSUFFICIENT_EVIDENCE",
                "incomplete ledger contract mismatch")
        require(provenance.get("funding_source_sha256") == adapter.FUNDING_EVIDENCE_SHA256
                and provenance.get("funding_provenance") == "pinned_committed_diagnostic_evidence",
                "funding identity mismatch")
        target, _ = adapter.load_pinned_target()
        source = report["source"]
        for key in adapter.closure.MONEY_FIELDS:
            adapter.closure.equal(source["frozen_primary"][key], target["primary_payoff"][key])
        adapter.ledger.close(
            adapter.ledger.number(report["ledger_base_pnl_usdt"], "ledger base PnL"),
            Decimal(str(target["primary_payoff"]["base_net_pnl_usdt"])),
            "ledger base PnL")
        for key in ("archive_input_set_sha256", "target_snapshot_set_sha256"):
            require(re.fullmatch(r"[0-9a-f]{64}", source[key]) is not None,
                    "invalid raw input identity")
            result[key] = source[key]
        for key in ("target_snapshot_count", "timeline_snapshot_count", "hedge_trade_count"):
            require(type(source[key]) is int and 0 <= source[key] <= 1000000,
                    "invalid source count")
            result[key] = source[key]
        count = source["exit_liquidity_unqualified_checkpoint_count"]
        first = source["first_exit_liquidity_gap_ts_ms"]
        require(type(count) is int and 0 <= count <= 1000000, "invalid liquidity gap count")
        require((count == 0 and first is None) or
                (count > 0 and type(first) is int
                 and target["selected_epoch_ms"] <= first <= target["delivery_time_epoch_ms"]),
                "invalid first liquidity gap time")
        result.update(exit_liquidity_unqualified_checkpoint_count=count,
                      first_exit_liquidity_gap_ts_ms=first)
        rates, _ = adapter.load_pinned_funding_rates()
        boundaries = [{"ts_ms": timestamp, "settled_rate": rate}
                      for timestamp, rate in sorted(rates.items())
                      if target["selected_epoch_ms"] <= timestamp <= target["delivery_time_epoch_ms"]]
        require(source["settled_funding_boundaries"] == boundaries
                and source["funding_boundary_count"] == len(boundaries),
                "funding boundary mismatch")
        gaps = report["known_gaps"]
        require(isinstance(gaps, list) and len(gaps) <= 40
                and all(isinstance(gap, str) and re.fullmatch(r"[A-Z_]{1,100}", gap) for gap in gaps)
                and {"MARGIN_EVIDENCE_MISSING", "SCHEDULED_FUNDING_MISSING"} <= set(gaps),
                "missing or unsafe gap list")
        require((count > 0) == ("EXIT_BBO_DEPTH_INSUFFICIENT" in gaps),
                "liquidity gap count and qualification disagree")
        require(re.fullmatch(r"[0-9a-f]{64}", report["ledger_input_sha256"]) is not None,
                "invalid ledger identity")
        require(re.fullmatch(r"[0-9a-f]{64}", report["ledger_file_sha256"]) is not None,
                "invalid ledger file identity")
        result.update({
            "ledger_input_sha256": report["ledger_input_sha256"],
            "ledger_file_sha256": report["ledger_file_sha256"],
            "ledger_status": "INSUFFICIENT_EVIDENCE",
            "ledger_base_pnl_usdt": adapter.text_number(report["ledger_base_pnl_usdt"]),
            "maximum_reconstructed_hedge_position_btc": adapter.text_number(
                source["maximum_reconstructed_hedge_position_btc"]),
            "settled_funding_boundaries": boundaries, "known_gaps": gaps,
        })
    elif report["decision"] == "C2_FIRST_LIFECYCLE_SOURCE_EVIDENCE_GAP":
        require(report.get("ledger_output_written") is False, "gap cannot write ledger")
        require(report.get("reason") in GAP_REASONS, "unknown gap reason")
        context = report["gap_context"]
        safe = {}
        for key in ("snapshot_ts_ms", "available_ts_ms", "delivery_ts_ms", "affected_trade_count"):
            if key in context:
                require(type(context[key]) is int and 0 <= context[key] <= 10**15,
                        "invalid gap time or count")
                safe[key] = context[key]
        for key in ("required_quantity_btc", "available_quantity_btc"):
            if key in context:
                safe[key] = adapter.text_number(context[key])
        for key in ("archive_input_set_sha256", "target_snapshot_set_sha256"):
            require(re.fullmatch(r"[0-9a-f]{64}", context[key]) is not None,
                    "missing gap raw identity")
            safe[key] = context[key]
        result.update(reason=report["reason"], gap_context=safe)
    elif report["decision"] == "INVALID_OPTION_LIFECYCLE_SUBACCOUNT_ADAPTER":
        require(report.get("ledger_output_written") is False, "invalid input cannot write ledger")
        reason = report.get("reason", "")
        result["reason"] = (reason if isinstance(reason, str) and
                            re.fullmatch(r"[A-Za-z0-9 _.,:()=+/-]{1,200}", reason)
                            else "INVALID_INPUT_DETAILS_WITHHELD")
    else:
        raise ValueError("unexpected adapter decision")
    return result


def annotation(value: dict[str, Any]) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    require(len(rendered.encode()) <= 6000, "summary byte budget exceeded")
    return "::notice title=Option C2 adapter::" + rendered.replace("%", "%25").replace(
        "\r", "%0D").replace("\n", "%0A")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=pathlib.Path)
    parser.add_argument("--lifecycle-report", required=True, type=pathlib.Path)
    parser.add_argument("--expected-release", default="")
    args = parser.parse_args()
    try:
        lifecycle = json.loads(args.lifecycle_report.read_bytes())
        require(lifecycle.get("schema_version") == "option_lifecycle_audit_v4",
                "lifecycle report schema mismatch")
        release = lifecycle["identities"]["executed_release_sha"]
        require(not args.expected_release or release == args.expected_release,
                "lifecycle expected release mismatch")
        value = summary(json.loads(args.report.read_bytes()), expected_release=release)
        value["report_file_sha256"] = digest(args.report)
        print(annotation(value))
        return 2 if value["decision"].startswith("INVALID_") else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"C2 summary rejected: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
