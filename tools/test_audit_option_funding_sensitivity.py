#!/usr/bin/env python3
"""Offline accounting, source identity and no-promotion counterexamples."""
import copy
from decimal import Decimal
import pathlib
import subprocess
import sys
import tempfile
import unittest

import audit_option_funding_sensitivity as audit


TS = 1788710400000


def payload():
    return {"retCode": 0, "time": TS + 120000, "result": {"category": "linear", "symbol": "BTCUSDT",
        "list": [[str(t), "100", "110", "90", "105"] for t in (TS - 60000, TS, TS + 60000)]}}


def store_json(root, value, suffix):
    raw = audit.public.encoded(value)
    path = root / (audit.public.digest(raw) + suffix)
    audit.public.sample.persist_new_or_identical(path, raw)
    return path


def store_response(root, request, value):
    (root / "raw").mkdir(exist_ok=True)
    raw = audit.public.encoded(value)
    identity = audit.public.digest(raw)
    audit.public.sample.persist_new_or_identical(root / "raw" / (identity + ".raw"), raw)
    return {**request, "raw_sha256": identity, "raw_bytes": len(raw)}


def funding_fixture(root, start=TS - 120000, end=TS + 120000):
    report = {"schema_version": "option_lifecycle_funding_source_check_v1", "window_start_ms": start,
              "window_end_ms": end, "event_count": 1, "events": [{"ts_ms": TS, "settled_rate": "0.0001"}],
              "authorities": audit.public.AUTHORITIES.copy(), "requests": {}}
    for key in ("cashflows_qualified", "causal_hedge_positions_qualified", "economic_qualification",
                "margin_model_qualified", "settlement_marks_qualified"):
        report[key] = False
    middle = (start + end) // 2
    for key, left, right in (("full", start, end), ("left", start, middle), ("right", middle + 1, end)):
        request = {"path": "/v5/market/funding/history", "params": {"category": "linear", "symbol": "BTCUSDT",
            "startTime": left, "endTime": right, "limit": 200}}
        values = [{"symbol": "BTCUSDT", "fundingRateTimestamp": str(TS), "fundingRate": "0.0001"}] if left <= TS <= right else []
        report["requests"][key] = store_response(root, request,
            {"retCode": 0, "time": end, "result": {"category": "linear", "list": values}})
    return report


class FundingSensitivityTest(unittest.TestCase):
    def scenario(self, rate="0.001", base="-2", stress="-3"):
        return audit.sensitivity({TS: rate}, [audit.mark_context(payload(), TS)], {
            "base_net_pnl_usdt": base, "stress_net_pnl_usdt": stress, "delivery_fee_usdt": "0.2"})

    def test_hand_computed_envelope_and_inverse_threshold(self):
        report = self.scenario()
        row = report["scenarios"][1]
        self.assertEqual(Decimal(row["conditional_funding_credit_envelope_usdt"]), Decimal("0.0022"))
        self.assertEqual(Decimal(row["conditional_stress_total_usdt"]), Decimal("-2.9978"))
        self.assertEqual(Decimal(row["conditional_stress_without_any_delivery_fee_usdt"]), Decimal("-2.7978"))
        self.assertEqual(Decimal(row["stress_break_even_uniform_mark_usdt"]), Decimal("150000"))

    def test_negative_rate_uses_favourable_absolute_value_not_actual_cashflow(self):
        self.assertEqual(self.scenario("0.001")["scenarios"], self.scenario("-0.001")["scenarios"])

    def test_zero_rate_has_no_fake_infinite_price(self):
        row = self.scenario("0")["scenarios"][0]
        self.assertEqual(Decimal(row["conditional_funding_credit_envelope_usdt"]), 0)
        self.assertIsNone(row["stress_break_even_uniform_mark_usdt"])
        self.assertIsNone(row["stress_break_even_context_multiplier"])

    def test_positive_sensitivity_never_qualifies_or_reopens(self):
        report = self.scenario(base="-0.0001", stress="-0.0002")
        self.assertGreater(Decimal(report["scenarios"][0]["conditional_stress_total_usdt"]), 0)
        self.assertEqual(report["candidate_state"], "CLOSED")
        self.assertEqual(report["decision"], "DIAGNOSTIC_ONLY_C2_INCOMPLETE")
        for key in ("position_caps_verified", "settlement_marks_qualified", "cashflows_qualified",
                    "account_risk_qualified", "economic_qualification", "forward_wait_started",
                    "profitability_claim_allowed", "sharpe_claim_allowed", "drawdown_claim_allowed"):
            self.assertIs(report[key], False)
        self.assertFalse(any(report["authorities"].values()))

    def test_boundary_budget_and_alignment(self):
        for rates in ({}, {TS + 1: "0.1"}, {TS + i * 60000: "0.1" for i in range(25)}):
            with self.assertRaises(ValueError):
                audit.mark_plan(rates)

    def test_context_is_exactly_three_closed_candles(self):
        for mutation in ("missing", "duplicate", "time", "unclosed", "symbol", "ohlc", "nan", "cursor", "retcode"):
            value = payload()
            rows = value["result"]["list"]
            if mutation == "missing": rows.pop()
            elif mutation == "duplicate": rows[1] = copy.deepcopy(rows[0])
            elif mutation == "time": rows[0][0] = str(TS - 120000)
            elif mutation == "unclosed": value["time"] -= 1
            elif mutation == "symbol": value["result"]["symbol"] = "ETHUSDT"
            elif mutation == "ohlc": rows[0][2] = "95"
            elif mutation == "nan": rows[0][1] = "NaN"
            elif mutation == "cursor": value["result"]["nextPageCursor"] = "more"
            elif mutation == "retcode": value["retCode"] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                audit.mark_context(value, TS)

    def test_context_coverage_and_bad_totals(self):
        with self.assertRaisesRegex(ValueError, "coverage"):
            audit.sensitivity({TS: "0.001"}, [], {})
        with self.assertRaisesRegex(ValueError, "negative"):
            self.scenario(base="1", stress="-1")

    def test_funding_bundle_replays_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = store_json(root, funding_fixture(root), ".qualification.json")
            _, rates, identity = audit.funding_source(path)
            self.assertEqual(rates, {TS: "0.0001"})
            self.assertEqual(identity, path.name.split(".")[0])

    def test_false_funding_events_claims_and_request_are_rejected(self):
        for mutation in ("events", "count", "authority", "qualified", "request", "partitions"):
            with tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                report = funding_fixture(root)
                if mutation == "events": report["events"][0]["settled_rate"] = "0.0002"
                elif mutation == "count": report["event_count"] = True
                elif mutation == "authority": report["authorities"]["promotion_authority"] = True
                elif mutation == "qualified": report["cashflows_qualified"] = True
                elif mutation == "request": report["requests"]["full"]["params"]["symbol"] = "ETHUSDT"
                elif mutation == "partitions": report["requests"].pop("left")
                path = store_json(root, report, ".qualification.json")
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    audit.funding_source(path)

    def test_raw_identity_length_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            request = audit.mark_plan({TS: "0.001"})[str(TS)]
            record = store_response(root, request, payload())
            self.assertEqual(audit.response(root, record, request), payload())
            bad = {**record, "raw_bytes": record["raw_bytes"] + 1}
            with self.assertRaisesRegex(ValueError, "length"):
                audit.response(root, bad, request)
            other = {**record, "raw_sha256": "../escape"}
            with self.assertRaisesRegex(ValueError, "raw hash"):
                audit.response(root, other, request)
            path = root / "raw" / (record["raw_sha256"] + ".raw")
            moved = root / "moved.raw"
            path.rename(moved)
            path.symlink_to(moved)
            with self.assertRaisesRegex(ValueError, "symlink"):
                audit.response(root, record, request)

    def test_corrupt_raw_and_manifest_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            report = funding_fixture(root)
            path = store_json(root, report, ".qualification.json")
            named_wrong = path.with_name("0" * 64 + ".qualification.json")
            path.rename(named_wrong)
            with self.assertRaisesRegex(ValueError, "manifest hash"):
                audit.funding_source(named_wrong)
            named_wrong.rename(path)
            identity = report["requests"]["full"]["raw_sha256"]
            raw = root / "raw" / (identity + ".raw")
            raw.write_bytes(b"{}")
            with self.assertRaisesRegex(ValueError, "checksum"):
                audit.funding_source(path)

    def test_cli_missing_evidence_fails_closed(self):
        result = subprocess.run([sys.executable, audit.__file__, "--funding-source", "/nonexistent/funding.json",
                                 "--mark-bundle", "/nonexistent/marks.json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("INVALID_SENSITIVITY_EVIDENCE", result.stdout)

    def test_end_to_end_replay_and_cross_bundle_identity(self):
        # Public C1 summary is pinned; funding/marks here are deliberately synthetic.
        # Consistent input files must still NEVER certify historical completeness.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            report = funding_fixture(root, start=1788701400000, end=1789200000000)
            funding_path = store_json(root, report, ".qualification.json")
            request = audit.mark_plan({TS: "0.0001"})[str(TS)]
            bundle = {"schema_version": "option_funding_mark_context_bundle_v1",
                "funding_source_sha256": funding_path.name.split(".")[0],
                "requests": {str(TS): store_response(root, request, payload())}}
            mark_path = store_json(root, bundle, ".marks.json")
            result = audit.audit(funding_path, mark_path)
            self.assertEqual(result["event_count"], 1)
            self.assertFalse(result["cashflows_qualified"])
            self.assertEqual(result, audit.audit(funding_path, mark_path))
            bundle["funding_source_sha256"] = "0" * 64
            other_path = store_json(root, bundle, ".marks.json")
            with self.assertRaisesRegex(ValueError, "identity"):
                audit.audit(funding_path, other_path)


if __name__ == "__main__":
    unittest.main()
