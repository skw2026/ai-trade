#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
import tempfile
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import fetch_bybit_carry_history as fetcher
import fetch_bybit_derivatives_history as bybit
import run_funding_basis_carry_opportunity_experiment as experiment


class FundingBasisCarryOpportunityExperimentTest(unittest.TestCase):
    @staticmethod
    def policy_path() -> pathlib.Path:
        return TOOLS_DIR.parent / "config" / "funding_basis_carry_opportunity_experiment.json"

    @staticmethod
    def write_history(path: pathlib.Path, funding_rate: float) -> None:
        row_count = 140 * 24 * 12 + 1
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["timestamp"]
                + [f"spot_{field}" for field in fetcher.BAR_FIELDS]
                + [f"perpetual_{field}" for field in fetcher.BAR_FIELDS]
                + ["mark_open", "mark_high", "mark_low", "mark_close", "funding_rate"]
            )
            for index in range(row_count):
                timestamp = index * 300_000
                funding = funding_rate if index > 0 and index % 96 == 0 else ""
                writer.writerow(
                    [timestamp]
                    + [100.0, 100.0, 100.0, 100.0, 10.0, 1000.0]
                    + [100.0, 100.0, 100.0, 100.0, 10.0, 1000.0]
                    + [100.0, 100.0, 100.0, 100.0, funding]
                )

    @staticmethod
    def write_data_report(path: pathlib.Path, history: pathlib.Path) -> None:
        fetcher.atomic_write_json(
            path,
            {
                "schema_version": fetcher.SCHEMA_VERSION,
                "status": "PASS",
                "output_sha256": fetcher.sha256_file(history),
                "causality": {
                    "funding_alignment": "exact_settlement_timestamp_once_only",
                    "asof_funding_fill": False,
                },
            },
        )

    def args(self, root: pathlib.Path) -> argparse.Namespace:
        return argparse.Namespace(
            carry_csv=str(root / "carry.csv"),
            data_report=str(root / "data_report.json"),
            config=str(self.policy_path()),
            audit_manifest=str(root / "audit.json"),
            output=str(root / "output.json"),
            research_domain="development",
        )

    def test_frozen_policy_is_conservative_and_has_no_demo_authority(self):
        policy = experiment.validate_policy(self.policy_path())
        self.assertEqual(policy["mechanism"]["position"], "long_spot_short_linear_perpetual")
        self.assertFalse(policy["mechanism"]["reverse_carry_allowed"])
        self.assertEqual(policy["execution"]["stress_execution_cost_multiplier"], 1.25)
        self.assertFalse(policy["authorities"]["demo_activation_authorized"])

    def test_fetch_join_emits_funding_only_at_exact_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "carry.csv"
            spot = [
                bybit.Point(0, (100.0, 101.0, 99.0, 100.0, 1.0, 100.0)),
                bybit.Point(300_000, (100.0, 101.0, 99.0, 100.0, 1.0, 100.0)),
            ]
            perpetual = list(spot)
            mark = [
                bybit.Point(0, (100.0, 101.0, 99.0, 100.0)),
                bybit.Point(300_000, (100.0, 101.0, 99.0, 100.0)),
            ]
            audit = fetcher.write_joined_csv(
                output,
                spot,
                perpetual,
                mark,
                [bybit.Point(300_000, (0.001,))],
            )
            with output.open("r", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["funding_rate"], "")
            self.assertEqual(float(rows[1]["funding_rate"]), 0.001)
            self.assertEqual(audit["funding_event_count"], 1)

    def test_positive_actual_funding_passes_only_to_raw_bbo_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            history = root / "carry.csv"
            self.write_history(history, 0.003)
            self.write_data_report(root / "data_report.json", history)
            report = experiment.run_experiment(self.args(root))
            self.assertEqual(report["status"], "COMPLETE")
            self.assertEqual(report["research_decision"], experiment.DECISION_CONTINUE)
            self.assertTrue(report["hindsight_oracle"]["opportunity_proven"])
            self.assertEqual(
                report["hindsight_oracle"]["method"],
                "six_split_exact_weighted_interval_hindsight_no_model_upper_bound_v2",
            )
            self.assertGreaterEqual(report["hindsight_oracle"]["trade_count"], 12)
            self.assertTrue(
                all(
                    value % 300_000 == 0
                    for split in report["common_domain"]["splits"]
                    for key, value in split.items()
                    if key.endswith("_ms")
                )
            )
            self.assertFalse(report["execution_contract"]["historical_price_is_executable_bbo"])
            self.assertFalse(report["demo_activation_authorized"])
            self.assertFalse(report["live_activation_authorized"])

    def test_negative_funding_stops_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            history = root / "carry.csv"
            self.write_history(history, -0.001)
            self.write_data_report(root / "data_report.json", history)
            report = experiment.run_experiment(self.args(root))
            self.assertEqual(report["research_decision"], experiment.DECISION_STOP)
            self.assertFalse(report["hindsight_oracle"]["opportunity_proven"])
            self.assertIn("historical_carry_upper_bound_failed", report["reason_codes"])

    def test_frozen_domain_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            history = root / "carry.csv"
            self.write_history(history, 0.003)
            self.write_data_report(root / "data_report.json", history)
            args = self.args(root)
            experiment.run_experiment(args)
            rows = history.read_text(encoding="utf-8").splitlines()
            fields = rows[len(rows) // 2].split(",")
            fields[1] = "101"
            rows[len(rows) // 2] = ",".join(fields)
            history.write_text("\n".join(rows) + "\n", encoding="utf-8")
            self.write_data_report(root / "data_report.json", history)
            with self.assertRaisesRegex(ValueError, "frozen domain drift"):
                experiment.run_experiment(args)


if __name__ == "__main__":
    unittest.main()
