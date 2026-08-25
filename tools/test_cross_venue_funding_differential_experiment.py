#!/usr/bin/env python3

import argparse
import csv
import json
import pathlib
import tempfile
import unittest

import numpy as np

import fetch_bybit_derivatives_history as bybit
import fetch_cross_venue_funding_history as fetcher
import run_cross_venue_funding_differential_experiment as experiment
import run_cross_venue_information_set_experiment as common
import run_funding_basis_carry_opportunity_experiment as carry


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "cross_venue_funding_differential_opportunity_experiment.json"


class CrossVenueFundingDifferentialExperimentTest(unittest.TestCase):
    def make_parent(self, path: pathlib.Path, timestamps: np.ndarray) -> dict:
        primary = carry.build_carry_time_splits(
            timestamps,
            n_splits=6,
            train_window_seconds=30 * 86_400,
            validation_window_seconds=7 * 86_400,
            test_window_seconds=14 * 86_400,
            rolling_step_seconds=14 * 86_400,
            embargo_seconds=86_400,
        )
        boundary = {
            str(offset): [vars(carry.shift_split(split, offset)) for split in primary]
            for offset in (0, -1, -2, -3)
        }
        frozen_start = min(
            int(split["fit_start_ms"])
            for values in boundary.values()
            for split in values
        )
        frozen_end = max(split.test_end_ms for split in primary)
        payload = {
            "schema_version": experiment.PARENT_AUDIT_SCHEMA_VERSION,
            "created_at_utc": "2026-08-25T00:00:00Z",
            "policy_identity_sha256": carry.FROZEN_POLICY_IDENTITY_SHA256,
            "experiment_id": "synthetic_parent_carry",
            "split_calendar_source": "synthetic_exact_parent",
            "frozen_domain": {
                "start_ms": frozen_start,
                "end_ms": frozen_end,
                "row_count": int((frozen_end - frozen_start) // 300_000),
            },
            "primary_splits": [vars(split) for split in primary],
            "boundary_splits": boundary,
        }
        payload["identity_sha256"] = common.canonical_sha256(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def write_history(
        self,
        path: pathlib.Path,
        *,
        bybit_rate: float,
        binance_rate: float,
    ) -> np.ndarray:
        count = 140 * 24 * 12 + 1
        timestamps = np.arange(count, dtype=np.int64) * 300_000 + 1_700_000_000_000
        headers = list(experiment.REQUIRED_FIELDS)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for index, timestamp in enumerate(timestamps):
                event = index % 96 == 0
                row = {field: "100" for field in headers}
                row["timestamp"] = str(int(timestamp))
                row["bybit_perpetual_volume"] = "1000"
                row["bybit_perpetual_turnover"] = "100000"
                row["binance_perpetual_volume"] = "1000"
                row["binance_perpetual_turnover"] = "100000"
                row["bybit_funding_rate"] = str(bybit_rate) if event else ""
                row["bybit_funding_mark"] = "100" if event else ""
                row["bybit_funding_timestamp"] = (
                    str(int(timestamp)) if event else ""
                )
                row["binance_funding_rate"] = str(binance_rate) if event else ""
                row["binance_funding_mark"] = "100" if event else ""
                row["binance_funding_timestamp"] = (
                    str(int(timestamp) + 4) if event else ""
                )
                writer.writerow(row)
        return timestamps

    def write_data_report(
        self, path: pathlib.Path, history: pathlib.Path, parent: dict
    ) -> None:
        payload = {
            "schema_version": "cross_venue_funding_history_v1",
            "status": "PASS",
            "output_sha256": common.sha256_file(history),
            "causality": {
                "funding_alignment": "exact_venue_settlement_timestamp_once_only",
                "asof_funding_fill": False,
                "original_funding_event_timestamp_preserved": True,
            },
            "parent_audit_manifest": {
                "identity_sha256": parent["identity_sha256"]
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def args(self, root: pathlib.Path) -> argparse.Namespace:
        return argparse.Namespace(
            history_csv=str(root / "history.csv"),
            data_report=str(root / "data_report.json"),
            parent_audit_manifest=str(root / "parent.json"),
            config=str(CONFIG),
            audit_manifest=str(root / "audit.json"),
            output=str(root / "report.json"),
            research_domain="development",
        )

    def prepare(
        self, root: pathlib.Path, *, bybit_rate: float, binance_rate: float
    ) -> argparse.Namespace:
        history = root / "history.csv"
        timestamps = self.write_history(
            history, bybit_rate=bybit_rate, binance_rate=binance_rate
        )
        parent = self.make_parent(root / "parent.json", timestamps)
        self.write_data_report(root / "data_report.json", history, parent)
        return self.args(root)

    def test_policy_is_frozen_and_has_no_activation_authority(self):
        policy = experiment.validate_policy(CONFIG)
        self.assertEqual(
            common.canonical_sha256(policy), experiment.FROZEN_POLICY_IDENTITY_SHA256
        )
        self.assertFalse(policy["authorities"]["demo_activation_authorized"])
        self.assertFalse(policy["authorities"]["live_activation_authorized"])
        self.assertTrue(policy["splits"]["inherit_parent_absolute_splits"])
        self.assertFalse(policy["splits"]["allow_rolling_recut"])

    def test_fetch_join_keeps_each_venue_settlement_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "joined.csv"
            bars = [
                bybit.Point(0, (100.0, 101.0, 99.0, 100.0, 10.0, 1000.0)),
                bybit.Point(300_000, (100.0, 101.0, 99.0, 100.0, 10.0, 1000.0)),
            ]
            marks = [
                bybit.Point(0, (100.0, 101.0, 99.0, 100.0)),
                bybit.Point(300_000, (100.0, 101.0, 99.0, 100.0)),
            ]
            audit = fetcher.write_joined_csv(
                output,
                bars,
                marks,
                [bybit.Point(0, (0.001,))],
                bars,
                marks,
                [bybit.Point(300_004, (-0.002, 101.0))],
            )
            with output.open("r", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(float(rows[0]["bybit_funding_rate"]), 0.001)
            self.assertEqual(float(rows[0]["bybit_funding_mark"]), 100.0)
            self.assertEqual(int(rows[0]["bybit_funding_timestamp"]), 0)
            self.assertEqual(rows[0]["binance_funding_rate"], "")
            self.assertEqual(float(rows[1]["binance_funding_rate"]), -0.002)
            self.assertEqual(float(rows[1]["binance_funding_mark"]), 101.0)
            self.assertEqual(int(rows[1]["binance_funding_timestamp"]), 300_004)
            self.assertEqual(audit["bybit_funding_event_count"], 1)
            self.assertEqual(audit["binance_funding_event_count"], 1)

    def test_entry_settlement_excluded_and_exit_settlement_included(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.prepare(root, bybit_rate=0.004, binance_rate=-0.004)
            series = experiment.load_series(pathlib.Path(args.history_csv))
            timestamps = series["timestamp"]
            entry_index = 96
            exit_index = 192
            positions = {int(value): index for index, value in enumerate(timestamps)}
            events = {
                venue: experiment.venue_events(series, venue)
                for venue in ("bybit", "binance")
            }
            outcome = experiment.candidate_outcome(
                series=series,
                positions=positions,
                events=events,
                entry_timestamp=int(timestamps[entry_index]),
                exit_timestamp=int(timestamps[exit_index]),
                direction="long_binance_short_bybit",
                policy=experiment.validate_policy(CONFIG),
            )
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["bybit_funding_event_count"], 1)
            self.assertEqual(outcome["binance_funding_event_count"], 1)
            self.assertGreater(outcome["funding_bps"], 70.0)

    def test_large_actual_funding_differential_continues_only_to_raw_bbo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.prepare(root, bybit_rate=0.004, binance_rate=-0.004)
            report = experiment.run_experiment(args)
            self.assertEqual(report["status"], "COMPLETE")
            self.assertEqual(report["research_decision"], experiment.DECISION_CONTINUE)
            self.assertTrue(report["hindsight_oracle"]["opportunity_proven"])
            self.assertGreaterEqual(report["hindsight_oracle"]["trade_count"], 12)
            self.assertEqual(
                report["hindsight_oracle"]["method"],
                "six_parent_split_exact_weighted_interval_cross_venue_hindsight_upper_bound_v1",
            )
            self.assertEqual(
                report["stability_audit"]["parent_audit_identity_sha256"],
                json.loads((root / "parent.json").read_text())["identity_sha256"],
            )
            self.assertFalse(report["execution_contract"]["historical_price_is_executable_bbo"])
            self.assertFalse(report["demo_activation_authorized"])
            self.assertFalse(report["live_activation_authorized"])

    def test_no_funding_differential_stops_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.prepare(root, bybit_rate=0.0001, binance_rate=0.0001)
            report = experiment.run_experiment(args)
            self.assertEqual(report["research_decision"], experiment.DECISION_STOP)
            self.assertFalse(report["hindsight_oracle"]["opportunity_proven"])
            self.assertIn(
                "historical_cross_venue_funding_upper_bound_failed",
                report["reason_codes"],
            )

    def test_frozen_domain_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.prepare(root, bybit_rate=0.004, binance_rate=-0.004)
            policy = experiment.validate_policy(CONFIG)
            parent = experiment.validate_parent_manifest(root / "parent.json")
            series = experiment.load_series(root / "history.csv")
            experiment.load_or_create_manifest(
                root / "audit.json", series=series, policy=policy, parent=parent
            )
            changed = {key: value.copy() for key, value in series.items()}
            changed["bybit_perpetual_open"][len(changed["timestamp"]) // 2] += 1.0
            with self.assertRaisesRegex(ValueError, "frozen domain drift"):
                experiment.load_or_create_manifest(
                    root / "audit.json",
                    series=changed,
                    policy=policy,
                    parent=parent,
                )


if __name__ == "__main__":
    unittest.main()
