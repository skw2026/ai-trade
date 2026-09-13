#!/usr/bin/env python3
"""Failure-contract tests for the release-bound C2 reporting boundary."""

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

import adapt_option_lifecycle_subaccount_v1 as adapter
import summarize_option_lifecycle_adapter as reporting


ROOT = pathlib.Path(__file__).resolve().parents[1]
RELEASE = "a" * 40


class SummaryTest(unittest.TestCase):
    def report(self):
        result = adapter._invalid_report("HEDGE_OBSERVATION_AVAILABLE_AFTER_DELIVERY", evidence_gap=True)
        result.update(target_lifecycle_id=adapter.TARGET_LIFECYCLE,
                      ledger_output_written=False, provenance={
            "executed_release_sha": RELEASE,
            "closure_evidence_sha256": adapter.CLOSURE_EVIDENCE_SHA256,
            "adapter_engine_sha256": reporting.digest(pathlib.Path(adapter.__file__)),
            "ledger_engine_sha256": reporting.digest(pathlib.Path(adapter.ledger.__file__)),
        }, gap_context={"snapshot_ts_ms": 1000, "available_ts_ms": 1001,
                        "delivery_ts_ms": 1000, "affected_trade_count": 1,
                        "archive_input_set_sha256": "b" * 64,
                        "target_snapshot_set_sha256": "c" * 64})
        return result

    def test_gap_is_reportable_without_accounting_pass(self):
        result = reporting.summary(self.report(), expected_release=RELEASE)
        self.assertEqual(result["reason"], "HEDGE_OBSERVATION_AVAILABLE_AFTER_DELIVERY")
        self.assertFalse(result["cashflows_qualified"])
        self.assertFalse(result["forward_wait_started"])
        self.assertIn('"available_ts_ms":1001', reporting.annotation(result))

    def test_release_and_engine_mismatch_rejected(self):
        for field in ("executed_release_sha", "adapter_engine_sha256", "ledger_engine_sha256"):
            report = self.report()
            report["provenance"][field] = "d" * len(report["provenance"][field])
            with self.subTest(field=field), self.assertRaises(ValueError):
                reporting.summary(report, expected_release=RELEASE)

    def test_authorities_and_claims_require_literal_false(self):
        for key in reporting.FALSE_FIELDS:
            report = self.report()
            report[key] = 0
            with self.subTest(key=key), self.assertRaises(ValueError):
                reporting.summary(report, expected_release=RELEASE)
        for value in (True, 0, "false"):
            report = self.report()
            report["authorities"]["order_submission_authorized"] = value
            with self.assertRaises(ValueError):
                reporting.summary(report, expected_release=RELEASE)

    def test_unknown_fields_cannot_escape_into_public_summary(self):
        report = self.report()
        report["raw_rows"] = "do-not-publish"
        report["gap_context"]["credential"] = "do-not-publish"
        result = reporting.summary(report, expected_release=RELEASE)
        self.assertNotIn("do-not-publish", reporting.annotation(result))

    def test_invalid_and_gap_cannot_report_written_ledger(self):
        for decision in ("C2_FIRST_LIFECYCLE_SOURCE_EVIDENCE_GAP",
                         "INVALID_OPTION_LIFECYCLE_SUBACCOUNT_ADAPTER"):
            report = self.report()
            report.update(decision=decision, ledger_output_written=True)
            with self.assertRaises(ValueError):
                reporting.summary(report, expected_release=RELEASE)

    def test_wrong_target_or_unexpected_decision_rejected(self):
        for field, value in (("target_lifecycle_id", "renamed"), ("decision", "PASS")):
            report = self.report()
            report[field] = value
            with self.assertRaises(ValueError):
                reporting.summary(report, expected_release=RELEASE)

    def test_real_cli_absent_archive_binds_release_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            path = directory / "report.json"
            run = subprocess.run([
                sys.executable, str(ROOT / "tools/adapt_option_lifecycle_subaccount_v1.py"),
                "--root", str(directory / "bybit_btc_option_lifecycle_v4"),
                "--ledger-output", str(directory / "ledger.json"),
                "--report-output", str(path), "--executed-release-sha", RELEASE,
            ], capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            result = reporting.summary(json.loads(path.read_text()), expected_release=RELEASE)
            self.assertEqual(result["reason"], "V4 capture root is absent")
            self.assertFalse((directory / "ledger.json").exists())

    def test_incomplete_result_reconciles_frozen_amount_and_funding_population(self):
        target, _ = adapter.load_pinned_target()
        rates, _ = adapter.load_pinned_funding_rates()
        boundaries = [{"ts_ms": timestamp, "settled_rate": rate}
                      for timestamp, rate in sorted(rates.items())
                      if target["selected_epoch_ms"] <= timestamp <= target["delivery_time_epoch_ms"]]
        report = self.report()
        report.update(decision=adapter.DECISION, ledger_output_written=True,
                      ledger_status="INSUFFICIENT_EVIDENCE", ledger_input_sha256="e" * 64,
                      ledger_file_sha256="f" * 64,
                      ledger_base_pnl_usdt=str(target["primary_payoff"]["base_net_pnl_usdt"]),
                      known_gaps=["MARGIN_EVIDENCE_MISSING", "SCHEDULED_FUNDING_MISSING"], source={
            "frozen_primary": target["primary_payoff"], "archive_input_set_sha256": "b" * 64,
            "target_snapshot_set_sha256": "c" * 64, "target_snapshot_count": 100,
            "timeline_snapshot_count": 99, "hedge_trade_count": 20,
            "maximum_reconstructed_hedge_position_btc": "0.01",
            "settled_funding_boundaries": boundaries, "funding_boundary_count": 3,
            "exit_liquidity_unqualified_checkpoint_count": 0,
            "first_exit_liquidity_gap_ts_ms": None,
        })
        report["provenance"].update(funding_source_sha256=adapter.FUNDING_EVIDENCE_SHA256,
                                    funding_provenance="pinned_committed_diagnostic_evidence")
        self.assertEqual(reporting.summary(report, expected_release=RELEASE)["decision"], adapter.DECISION)
        # Account averages can produce >18 fractional digits at the ledger's
        # 60-digit internal precision, without violating external input rules.
        report["ledger_base_pnl_usdt"] += "00000000000000000000000000000001"
        self.assertEqual(reporting.summary(report, expected_release=RELEASE)["decision"], adapter.DECISION)
        for field, value in (("ledger_base_pnl_usdt", "1"), ("ledger_base_pnl_usdt", -2.284818),
                             ("ledger_status", "PASS"),
                             ("known_gaps", [])):
            changed = copy.deepcopy(report)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                reporting.summary(changed, expected_release=RELEASE)
        report["source"]["settled_funding_boundaries"] = []
        with self.assertRaises(ValueError):
            reporting.summary(report, expected_release=RELEASE)

    def test_actual_deployment_copy_recipe_loads_pinned_dependencies(self):
        workflow = (ROOT / ".github/workflows/cd.yml").read_text()
        recipe = workflow.split('          cp -a config observability ops tools', 1)[1]
        recipe = '          cp -a config observability ops tools' + recipe.split(
            '          python3 deploy/materialize_release_compose.py', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            environment = {**os.environ, "PAYLOAD_DIR": temporary,
                           "PYTHONDONTWRITEBYTECODE": "1"}
            copied = subprocess.run(["bash", "-euc", textwrap.dedent(recipe)], cwd=ROOT,
                                    env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(copied.returncode, 0, copied.stderr)
            loaded = subprocess.run([
                sys.executable, "-c",
                "import adapt_option_lifecycle_subaccount_v1 as a; "
                "a.load_pinned_target(); a.load_pinned_funding_rates(); print('pinned evidence loaded')",
            ], cwd=pathlib.Path(temporary) / "tools", env=environment,
                capture_output=True, text=True, timeout=20)
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            self.assertIn("pinned evidence loaded", loaded.stdout)


if __name__ == "__main__":
    unittest.main()
