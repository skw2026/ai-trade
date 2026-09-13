#!/usr/bin/env python3
"""Synthetic adapter tests; none of these fixtures are account evidence."""

from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import adapt_option_lifecycle_subaccount_v1 as adapter
import audit_option_candidate_closure as closure
import capture_bybit_option_lifecycle_v4 as capture
import test_audit_option_lifecycle_economics_v1 as fixtures


ROOT = pathlib.Path(__file__).resolve().parents[1]


class RealSymbolArchive(fixtures.OptionLifecycleEconomicsV1Test):
    __unittest_skip__ = True
    __unittest_skip_why__ = "fixture helper; exercised by AdapterTest"
    completion_after_delivery = False
    shallow_hedge_depth = False

    def lifecycle(self, index: int, selected: int, delivery: int,
                  premium: float) -> dict:
        expiry = dt.datetime.fromtimestamp(delivery / 1000, dt.timezone.utc)
        token = f"{expiry.day}{expiry.strftime('%b%y').upper()}"
        call = f"BTC-{token}-80000-C-USDT"
        put = f"BTC-{token}-80000-P-USDT"
        instruments = capture._instrument_map([
            fixtures.instrument(call, delivery, "Call"),
            fixtures.instrument(put, delivery, "Put"),
        ])
        tickers = capture._ticker_map([
            fixtures.ticker(call, "Call", premium),
            fixtures.ticker(put, "Put", premium),
        ])
        rows = []
        for symbol, side in ((call, "Call"), (put, "Put")):
            rows.append({
                "symbol": symbol, "deliveryTime": delivery, "strike": 80000.0,
                "optionsType": side, "dteDays": 1.0, "moneyness": 0.0,
                "indexPrice": "80000", "bid1Price": str(premium),
                "ask1Price": str(premium + 10), "bid1Size": "1",
                "ask1Size": "1",
            })
        result = capture.select_lifecycle(
            now_epoch_ms=selected, rows=rows, instruments=instruments,
            policy=self.capture_policy)
        self.assertIsNotNone(result)
        return result

    def snapshot(self, timestamp: int, lifecycle: dict, *, premium: float,
                 delivery_price: float | None = None) -> dict:
        result = super().snapshot(timestamp, lifecycle, premium=premium,
                                  delivery_price=delivery_price)
        result["snapshot_completed_epoch_ms"] = timestamp
        result["hedge_orderbook_l1"]["ts"] = timestamp
        if self.completion_after_delivery and timestamp == lifecycle["delivery_time_epoch_ms"]:
            result["snapshot_completed_epoch_ms"] = timestamp + 1
        if self.shallow_hedge_depth:
            result["hedge_orderbook_l1"]["b"][0][1] = "0.001"
            result["hedge_orderbook_l1"]["a"][0][1] = "0.001"
            for row in result["tracked_options"]:
                row["delta"] = "0.5"
        return result


class AdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.case = RealSymbolArchive()
        self.case.capture_policy, self.case.capture_manifest = capture.load_contract(
            fixtures.CAPTURE_POLICY, fixtures.CAPTURE_MANIFEST)
        self.case.start = int(self.case.capture_manifest["observation_start_epoch_ms"])

    def make_archive(self, parent: pathlib.Path,
                     case: RealSymbolArchive | None = None
                     ) -> tuple[pathlib.Path, dict, dict[int, str]]:
        active = case or self.case
        root = parent / capture.CAPTURE_ROOT_NAME
        delivery = active.archive(root, 1, premium=100.0)
        economic = active.run_audit(root, delivery + 1000)
        lifecycle = economic["lifecycles"][0]
        primary = next(row for row in lifecycle["actions"]
                       if row["action_id"] == adapter.PRIMARY_ACTION)
        target = {
            "lifecycle_id": lifecycle["lifecycle_id"],
            "selected_epoch_ms": lifecycle["selected_epoch_ms"],
            "delivery_time_epoch_ms": lifecycle["delivery_time_epoch_ms"],
            "primary_payoff": {field: primary[field]
                               for field in closure.MONEY_FIELDS},
        }
        rates = {lifecycle["selected_epoch_ms"] + 180000: "0.0001"}
        return root, target, rates

    def test_reconstructs_frozen_path_but_preserves_accounting_gaps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, rates = self.make_archive(pathlib.Path(temporary))
            ledger_input, report = adapter.adapt(
                root=root, target=target, rates=rates)
        self.assertEqual(report["decision"], adapter.DECISION)
        self.assertEqual(report["ledger_status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("MARGIN_EVIDENCE_MISSING", report["known_gaps"])
        self.assertIn("SCHEDULED_FUNDING_MISSING", report["known_gaps"])
        self.assertIn("EXACT_FUNDING_SETTLEMENT_MARKS_MISSING",
                      report["known_gaps"])
        self.assertFalse(any(report["authorities"].values()))
        self.assertEqual(report["ledger_input_sha256"],
                         adapter.sha256_json(ledger_input))
        self.assertTrue(all("margin" not in row for row in ledger_input["events"]))
        self.assertFalse(any(row["type"] == "FUNDING"
                             for row in ledger_input["events"]))
        self.assertEqual(len(ledger_input["funding_schedule"]), 1)
        self.assertEqual(ledger_input["events"][0]["type"], "MARK")
        self.assertEqual(ledger_input["events"][-1]["type"], "DELIVERY")
        self.assertEqual([row["seq"] for row in ledger_input["events"]],
                         list(range(len(ledger_input["events"]))))

    def test_raw_payoff_must_match_pinned_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, rates = self.make_archive(pathlib.Path(temporary))
            changed = copy.deepcopy(target)
            changed["primary_payoff"]["base_net_pnl_usdt"] += 1
            with self.assertRaisesRegex(ValueError, "accounting or aggregate mismatch"):
                adapter.adapt(root=root, target=changed, rates=rates)

    def test_funding_boundary_cannot_be_silently_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, _ = self.make_archive(pathlib.Path(temporary))
            with self.assertRaisesRegex(ValueError, "no settled funding boundary"):
                adapter.adapt(root=root, target=target, rates={})

    def test_post_delivery_observation_is_an_evidence_gap(self):
        case = RealSymbolArchive()
        case.capture_policy, case.capture_manifest = capture.load_contract(
            fixtures.CAPTURE_POLICY, fixtures.CAPTURE_MANIFEST)
        case.start = int(case.capture_manifest["observation_start_epoch_ms"])
        case.completion_after_delivery = True
        with tempfile.TemporaryDirectory() as temporary:
            root, target, rates = self.make_archive(pathlib.Path(temporary), case)
            with self.assertRaisesRegex(adapter.EvidenceGap,
                                        "AVAILABLE_AFTER_DELIVERY"):
                adapter.adapt(root=root, target=target, rates=rates)

    def test_hedge_trade_cannot_exceed_observed_l1_depth(self):
        case = RealSymbolArchive()
        case.capture_policy, case.capture_manifest = capture.load_contract(
            fixtures.CAPTURE_POLICY, fixtures.CAPTURE_MANIFEST)
        case.start = int(case.capture_manifest["observation_start_epoch_ms"])
        case.shallow_hedge_depth = True
        with tempfile.TemporaryDirectory() as temporary:
            root, target, rates = self.make_archive(pathlib.Path(temporary), case)
            with self.assertRaisesRegex(adapter.EvidenceGap,
                                        "HEDGE_EXECUTION_DEPTH_INSUFFICIENT"):
                adapter.adapt(root=root, target=target, rates=rates)

    def test_repository_closure_identity_selects_exact_first_lifecycle(self):
        target, report = adapter.load_pinned_target()
        self.assertEqual(target["lifecycle_id"], adapter.TARGET_LIFECYCLE)
        self.assertTrue(report["closure_latched"])
        self.assertFalse(report["full_cashflow_attribution_complete"])

    def test_committed_funding_evidence_remains_diagnostic_only(self):
        rates, identity = adapter.load_pinned_funding_rates()
        self.assertEqual(identity, adapter.FUNDING_EVIDENCE_SHA256)
        self.assertEqual(len(rates), 18)
        target, _ = adapter.load_pinned_target()
        self.assertEqual(sum(target["selected_epoch_ms"] <= timestamp <=
                             target["delivery_time_epoch_ms"]
                             for timestamp in rates), 3)

    def test_cli_fails_closed_when_raw_archive_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            ledger_output = directory / "ledger.json"
            ledger_output.write_text("preserve-previous-output")
            command = [
                sys.executable, str(ROOT / "tools/adapt_option_lifecycle_subaccount_v1.py"),
                "--root", str(directory / capture.CAPTURE_ROOT_NAME),
                "--ledger-output", str(ledger_output),
                "--report-output", str(directory / "report.json"),
            ]
            run = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            report = json.loads((directory / "report.json").read_text())
            self.assertEqual(report["decision"],
                             "INVALID_OPTION_LIFECYCLE_SUBACCOUNT_ADAPTER")
            self.assertFalse(report["ledger_output_written"])
            self.assertEqual(ledger_output.read_text(), "preserve-previous-output")
            self.assertFalse(any(report["authorities"].values()))

    def test_cli_never_overwrites_an_input_with_an_error_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            funding_source = directory / "funding.json"
            funding_source.write_text("preserve-me")
            command = [
                sys.executable, str(ROOT / "tools/adapt_option_lifecycle_subaccount_v1.py"),
                "--root", str(directory / capture.CAPTURE_ROOT_NAME),
                "--funding-source", str(funding_source),
                "--ledger-output", str(directory / "ledger.json"),
                "--report-output", str(funding_source),
            ]
            run = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(funding_source.read_text(), "preserve-me")
            self.assertFalse((directory / "ledger.json").exists())


def load_tests(loader: unittest.TestLoader, _: unittest.TestSuite,
               __: str | None) -> unittest.TestSuite:
    return loader.loadTestsFromTestCase(AdapterTest)


if __name__ == "__main__":
    unittest.main()
