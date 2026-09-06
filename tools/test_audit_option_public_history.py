#!/usr/bin/env python3
"""Offline counterexamples for public source qualification; no live requests."""
import copy
import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import audit_option_public_history as audit
from test_audit_option_historical_sample import record


ROOT = pathlib.Path(__file__).resolve().parents[1]
CALL = "BTC-2SEP26-78750-C-USDT"
PUT = "BTC-2SEP26-78750-P-USDT"
START, END = 1788220800000, 1788336000000


def fixture():
    sample = record() + record().replace(CALL.encode(), PUT.encode())
    case = {"schema_version": "option_public_history_case_v1", "start_ms": START, "end_ms": END,
            "option_symbols": [CALL, PUT], "sample_date": "2026-09-01", "sample_offset_minute": 0,
            "sample_raw_sha256": audit.digest(sample)}
    def response(category, values, **extra):
        return {"retCode": 0, "time": END + 60000, "result": {"category": category, "list": values, **extra}}
    rate = lambda when: {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(when)}
    middle = (START + END) // 2
    payloads = {"funding_full": response("linear", [rate(END), rate(middle), rate(START)]),
                "funding_left": response("linear", [rate(middle), rate(START)]),
                "funding_right": response("linear", [rate(END)]),
                "mark_context": response("linear", [[str(END), "77437", "77472.3", "77437", "77460.2"]], symbol="BTCUSDT")}
    for index, symbol in enumerate((CALL, PUT)):
        payloads[f"delivery_{index}"] = response("option", [{"symbol": symbol, "deliveryTime": str(END), "deliveryPrice": "77562.17"}], nextPageCursor="")
        payloads[f"instrument_{index}"] = {"retCode": 110023, "retMsg": "The contract is not available for trades.",
                                           "time": END + 60000, "result": {"category": "option", "list": []}}
    return case, payloads, sample


def as_raw(payloads):
    return {key: audit.encoded(value) for key, value in payloads.items()}


class PublicHistoryTest(unittest.TestCase):
    def test_complete_audit_is_not_historical_or_economic_pass(self):
        case, payloads, sample = fixture()
        report = audit.assess(case, as_raw(payloads), sample)
        self.assertTrue(report["audit_completed"])
        self.assertEqual(report["qualification_decision"], "INSUFFICIENT_HISTORICAL_EVIDENCE")
        self.assertEqual(len(report["funding"]["events"]), 3)
        self.assertEqual(len(report["deliveries"]), 2)
        self.assertEqual(report["coverage"]["certain_missing_duration_ms"], END - START - 60000)
        self.assertFalse(report["mark_candle_context"]["settlement_mark_qualified"])
        self.assertFalse(report["funding"]["cashflows_qualified"])
        self.assertFalse(report["historical_data_qualified"])
        self.assertFalse(report["economic_qualification"])
        self.assertFalse(report["payoff_evidence"])
        self.assertFalse(any(report["authorities"].values()))
        self.assertEqual(report["instrument_queries"][0]["status"], "UNAVAILABLE_FROM_THIS_REQUEST")

    def test_missing_partition_event_and_conflicting_rate_rejected(self):
        for action in ("remove", "rate"):
            case, payloads, sample = fixture()
            if action == "remove":
                payloads["funding_right"]["result"]["list"] = []
            else:
                payloads["funding_right"]["result"]["list"][0]["fundingRate"] = "0.0002"
            with self.assertRaisesRegex(ValueError, "partition funding disagreement"):
                audit.assess(case, as_raw(payloads), sample)

    def test_duplicate_wrong_symbol_out_of_range_and_saturated_funding(self):
        for action, reason in (("duplicate", "duplicate funding"), ("symbol", "wrong funding symbol"),
                               ("boundary", "outside query"), ("saturated", "page saturated")):
            case, payloads, sample = fixture()
            values = payloads["funding_full"]["result"]["list"]
            if action == "duplicate":
                values.append(copy.deepcopy(values[0]))
            elif action == "symbol":
                values[0]["symbol"] = "ETHUSDT"
            elif action == "boundary":
                values[0]["fundingRateTimestamp"] = str(END + 1)
            else:
                payloads["funding_full"]["result"]["list"] = [values[0]] * 200
            with self.assertRaisesRegex(ValueError, reason):
                audit.assess(case, as_raw(payloads), sample)

    def test_delivery_symbol_time_currency_request_and_price(self):
        for field, value, reason in (("symbol", PUT, "identity/time"), ("deliveryTime", str(END + 1), "identity/time"),
                                     ("deliveryPrice", "77563", "paired delivery"), ("deliveryPrice", "NaN", "magnitude")):
            case, payloads, sample = fixture()
            payloads["delivery_0"]["result"]["list"][0][field] = value
            with self.assertRaisesRegex(ValueError, reason):
                audit.assess(case, as_raw(payloads), sample)
        plan = audit.request_plan(fixture()[0])
        self.assertEqual(plan["delivery_0"]["params"]["settleCoin"], "USDT")
        self.assertEqual(plan["delivery_1"]["params"]["symbol"], PUT)

    def test_equivalent_decimal_representations_are_not_conflicts(self):
        case, payloads, sample = fixture()
        payloads["funding_right"]["result"]["list"][0]["fundingRate"] = "0.0001000"
        payloads["delivery_1"]["result"]["list"][0]["deliveryPrice"] = "77562.1700"
        self.assertTrue(audit.assess(case, as_raw(payloads), sample)["audit_completed"])

    def test_empty_delivery_funding_or_mark_cannot_promote(self):
        case, payloads, sample = fixture()
        for name in ("delivery_0", "delivery_1", "funding_full", "funding_left", "funding_right", "mark_context"):
            payloads[name]["result"]["list"] = []
        report = audit.assess(case, as_raw(payloads), sample)
        self.assertIn("missing_paired_delivery", report["missing_requirements"])
        self.assertIn("no_funding_records_returned", report["missing_requirements"])
        self.assertIsNone(report["mark_candle_context"])
        self.assertFalse(report["historical_data_qualified"])

    def test_current_metadata_not_historical_version(self):
        case, payloads, sample = fixture()
        payloads["instrument_0"] = {"retCode": 0, "time": END + 60000, "result": {"category": "option", "list": [{
            "symbol": CALL, "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
            "deliveryTime": str(END), "deliveryFeeRate": "0.00015",
            "lotSizeFilter": {"minOrderQty": "0.01", "qtyStep": "0.01"}}]}}
        report = audit.assess(case, as_raw(payloads), sample)
        self.assertFalse(report["instrument_queries"][0]["historical_effective_version_qualified"])
        payloads["instrument_0"]["result"]["list"][0]["settleCoin"] = "USDC"
        with self.assertRaisesRegex(ValueError, "units mismatch"):
            audit.assess(case, as_raw(payloads), sample)

    def test_mark_is_context_and_malformed_ohlc_is_rejected(self):
        case, payloads, sample = fixture()
        payloads["mark_context"]["result"]["list"][0][2] = "77000"
        with self.assertRaisesRegex(ValueError, "OHLC order"):
            audit.assess(case, as_raw(payloads), sample)

    def test_source_errors_future_history_and_cursor_rejected(self):
        for field, value, reason in (("retCode", 10001, "API returned error"), ("time", END - 1, "predates")):
            case, payloads, sample = fixture()
            payloads["funding_full"][field] = value
            with self.assertRaisesRegex(ValueError, reason):
                audit.assess(case, as_raw(payloads), sample)
        case, payloads, sample = fixture()
        payloads["delivery_0"]["result"]["nextPageCursor"] = "not-consumed"
        with self.assertRaisesRegex(ValueError, "unconsumed"):
            audit.assess(case, as_raw(payloads), sample)

    def test_case_bounds_pair_and_source_hash(self):
        for field, value in (("end_ms", START + 8 * 86400000), ("option_symbols", [CALL, CALL]),
                             ("option_symbols", [CALL, "ETH-2SEP26-78750-P-USDT"]), ("sample_raw_sha256", "../raw")):
            case, _, _ = fixture()
            case[field] = value
            with self.assertRaises(ValueError):
                audit.request_plan(case)
        case, payloads, sample = fixture()
        with self.assertRaisesRegex(ValueError, "sample hash"):
            audit.assess(case, as_raw(payloads), sample + b" ")

    def test_raw_bytes_and_replay_manifest_are_immutable(self):
        case, payloads, sample = fixture()
        raw = as_raw(payloads)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = audit.save_bundle(root, case, raw, sample)
            recovered = audit.load_bundle(path)
            self.assertEqual(recovered, (case, raw, sample))
            self.assertEqual(audit.assess(*recovered), audit.assess(case, raw, sample))
            raw_path = root / "raw" / (audit.digest(raw["funding_full"]) + ".raw")
            raw_path.write_bytes(b"{}")
            with self.assertRaisesRegex(ValueError, "checksum"):
                audit.load_bundle(path)

    def test_tampered_request_identity_rejected_even_with_new_manifest_hash(self):
        case, payloads, sample = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = audit.save_bundle(root, case, as_raw(payloads), sample)
            manifest = audit.decode(path.read_bytes())
            manifest["requests"]["delivery_0"]["params"]["settleCoin"] = "USDC"
            changed = audit.encoded(manifest)
            changed_path = root / (audit.digest(changed) + ".bundle.json")
            changed_path.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "request identity"):
                audit.load_bundle(changed_path)

    def test_size_duplicate_keys_and_symlink_rejection(self):
        for raw in (b'{"retCode":0,"retCode":1}', b"x" * (audit.MAX_RESPONSE_BYTES + 1)):
            with self.assertRaises(ValueError):
                audit.decode(raw)
        case, payloads, sample = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = audit.save_bundle(root, case, as_raw(payloads), sample)
            link = root / "link.bundle.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "symlink"):
                audit.load_bundle(link)

    def test_transport_is_bounded_no_credentials_or_redirects(self):
        request = audit.request_plan(fixture()[0])["funding_full"]
        with patch.object(audit.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"{}", b"")
            self.assertEqual(audit.fetch(request), b"{}")
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ["curl", "--disable"])
            self.assertIn("--max-filesize", command)
            self.assertNotIn("--location", command)
            self.assertNotIn("--header", command)
            self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_cli_replay_success_is_not_profit_pass_and_never_fetches(self):
        case, payloads, sample = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = audit.save_bundle(root, case, as_raw(payloads), sample)
            command = [sys.executable, str(ROOT / "tools/audit_option_public_history.py"),
                       "--replay", str(path), "--output-dir", str(root / "reports")]
            run = subprocess.run(command, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            report = json.loads(run.stdout)
            self.assertEqual(report["qualification_decision"], "INSUFFICIENT_HISTORICAL_EVIDENCE")
            self.assertFalse(report["public_origin_checked_this_invocation"])
            self.assertFalse(any(report["authorities"].values()))
            with patch.object(sys, "argv", command[1:]), patch.object(audit, "fetch", side_effect=AssertionError("replay must not fetch")) as fetch:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(audit.main(), 0)
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
