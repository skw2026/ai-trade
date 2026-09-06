#!/usr/bin/env python3

import json
import lzma
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import audit_option_archive_lifecycle as lifecycle
import audit_option_vrp_sequential_payoff as sequential
import capture_bybit_option_vrp_v2 as capture


ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "option_variance_risk_premium_sequential_payoff_v2.json"
MANIFEST = ROOT / "config" / "option_variance_risk_premium_sequential_payoff_manifest_v2.json"


class OptionArchiveLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy, cls.manifest = sequential.load_frozen_contract(POLICY, MANIFEST)
        cls.start = int(cls.manifest["observation_start_epoch_ms"]) + 1000
        cls.end = cls.start + 120000
        cls.symbols = ["BTC-2SEP26-78750-C-USDT", "BTC-2SEP26-78750-P-USDT"]

    def case_payload(self):
        return {
            "schema_version": lifecycle.CASE_SCHEMA_VERSION,
            "lifecycle_id": "fixture-pair",
            "experiment_id": self.policy["experiment_id"],
            "window": {"start_epoch_ms": self.start, "end_epoch_ms": self.end},
            "option_contract": {
                "symbols": self.symbols, "delivery_time_epoch_ms": self.end,
                "settle_coin": "USDT", "minimum_executable_size_btc": 0.01,
            },
            "hedge_contract": {
                "symbol": "BTCUSDT", "minimum_executable_size_btc": 0.001,
                "maximum_book_age_seconds": 120,
            },
            "coverage_contract": {
                "expected_poll_interval_seconds": 60,
                "maximum_snapshot_gap_seconds": 180,
                "all_observed_snapshots_must_be_qualified": True,
                "require_paired_delivery_evidence": True,
            },
            "research_domain": "development_only", "economic_evidence": False,
            "promotion_authority": False, "demo_activation_authorized": False,
            "live_activation_authorized": False,
        }

    def option_rows(self, *, bad_size=False, delivery_time=None):
        delivery_time = self.end if delivery_time is None else delivery_time
        rows = []
        for symbol, side, delta in zip(self.symbols, ("Call", "Put"), ("0.55", "-0.45")):
            rows.append({
                "symbol": symbol, "deliveryTime": delivery_time, "strike": "78750",
                "optionsType": side, "baseCoin": "BTC", "quoteCoin": "USDT",
                "settleCoin": "USDT", "minOrderQty": "0.01", "qtyStep": "0.01",
                "deliveryFeeRate": "0.00015", "tickSize": "5", "bid1Price": "950",
                "ask1Price": "1000", "bid1Size": "0" if bad_size else "2",
                "ask1Size": "2", "indexPrice": "78000", "delta": delta,
            })
        return rows

    def snapshot(self, timestamp, *, include_pair=True, bad_size=False,
                 stale_hedge=False, delivery=False, delivery_time=None):
        delivery_time = self.end if delivery_time is None else delivery_time
        delivery_rows = []
        if delivery:
            delivery_rows = [{
                "symbol": symbol, "deliveryPrice": "77562.17", "deliveryTime": delivery_time,
                "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
                "scopeIdentitySha256": capture.SCOPE_IDENTITY_SHA256,
            } for symbol in self.symbols]
        return {
            "schema_version": capture.SNAPSHOT_SCHEMA_VERSION,
            "timestamp_epoch_ms": timestamp,
            "scope_contract": capture.SCOPE_CONTRACT,
            "scope_identity_sha256": capture.SCOPE_IDENTITY_SHA256,
            "delivery_query_status": "PASS",
            "selection_contract": {
                "minimum_dte_days": 0.5, "maximum_dte_days": 10.0,
                "maximum_absolute_moneyness": 0.1,
                "scope_identity_sha256": capture.SCOPE_IDENTITY_SHA256,
                "settle_coin": "USDT",
            },
            "scoped_options": self.option_rows(
                bad_size=bad_size, delivery_time=delivery_time
            ) if include_pair else [],
            "delivery_prices": delivery_rows,
            "hedge_ticker": {"bid1Price": "77990", "ask1Price": "78010"},
            "hedge_orderbook_l1": {
                "b": [["77990", "1"]], "a": [["78010", "1"]],
                "ts": timestamp - (121000 if stale_hedge else 0),
            },
        }

    def write_segment(self, root, snapshots):
        raw = root / "raw" / "BTC" / "fixture.jsonl.xz"
        features = root / "features" / "BTC" / "fixture.csv"
        report = root / "reports" / "BTC" / "fixture.json"
        for path in (raw, features, report):
            path.parent.mkdir(parents=True, exist_ok=True)
        with lzma.open(raw, "wt", encoding="utf-8", preset=1) as handle:
            for snapshot in snapshots:
                handle.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
        features.write_text(
            "timestamp_epoch_ms\n" + "\n".join(str(row["timestamp_epoch_ms"]) for row in snapshots) + "\n",
            encoding="utf-8",
        )
        payload = {
            "schema_version": capture.SCHEMA_VERSION,
            "snapshot_schema_version": capture.SNAPSHOT_SCHEMA_VERSION,
            "scope_identity_sha256": capture.SCOPE_IDENTITY_SHA256,
            "capture_root_name": capture.CAPTURE_ROOT_NAME, "status": "PASS",
            "settle_coin": "USDT", "raw_codec": capture.RAW_CODEC,
            "coverage": {
                "capture_started_epoch_ms": snapshots[0]["timestamp_epoch_ms"],
                "capture_completed_epoch_ms": snapshots[-1]["timestamp_epoch_ms"],
                "successful_poll_count": len(snapshots),
            },
            "raw": {"path": raw.relative_to(root).as_posix(),
                    "sha256": sequential.sha256_file(raw), "snapshot_count": len(snapshots)},
            "features": {"path": features.relative_to(root).as_posix(),
                         "sha256": sequential.sha256_file(features), "row_count": len(snapshots)},
            "quality": {"delivery_query_status": "PASS"},
        }
        report.write_text(json.dumps(payload), encoding="utf-8")
        return raw

    def run_audit(self, mutate_snapshots=None, mutate_case=None, tamper=False):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / capture.CAPTURE_ROOT_NAME
            snapshots = [
                self.snapshot(self.start), self.snapshot(self.start + 60000),
                self.snapshot(self.end, delivery=True),
            ]
            if mutate_snapshots:
                snapshots = mutate_snapshots(snapshots)
            raw = self.write_segment(root, snapshots)
            if tamper:
                with raw.open("ab") as handle:
                    handle.write(b"tamper")
            case = self.case_payload()
            if mutate_case:
                mutate_case(case)
            case_path = base / "case.json"
            case_path.write_text(json.dumps(case), encoding="utf-8")
            return lifecycle.audit_lifecycle(
                root=root, policy_path=POLICY, manifest_path=MANIFEST,
                case_path=case_path, executed_release_sha="a" * 40,
                generated_at_epoch_ms=1,
            ), str(root)

    def test_complete_lifecycle_passes_archive_only(self):
        report, _ = self.run_audit()
        self.assertEqual(report["decision"], lifecycle.PASS_DECISION)
        self.assertEqual(report["lifecycle_coverage"]["qualified_snapshot_count"], 3)
        self.assertTrue(report["settlement"]["paired_delivery_evidence_valid"])
        self.assertFalse(report["economic_evidence"])
        self.assertFalse(report["demo_activation_authorized"])

    def test_missing_terminal_pair_fails_closed(self):
        def mutate(rows):
            rows[-1] = self.snapshot(self.end, include_pair=False, delivery=True)
            return rows
        report, _ = self.run_audit(mutate)
        self.assertEqual(report["decision"], lifecycle.INSUFFICIENT_DECISION)
        self.assertEqual(report["reason_code"], "OBSERVED_SNAPSHOTS_NOT_FULLY_QUALIFIED")
        self.assertEqual(report["lifecycle_coverage"]["unqualified_snapshot_count"], 1)

    def test_bad_option_size_and_stale_hedge_are_distinguished(self):
        def mutate(rows):
            rows[0] = self.snapshot(self.start, bad_size=True)
            rows[1] = self.snapshot(self.start + 60000, stale_hedge=True)
            return rows
        report, _ = self.run_audit(mutate)
        reasons = report["lifecycle_coverage"]["reason_counts"]
        self.assertEqual(reasons["OPTION_INSUFFICIENT_BBO_SIZE"], 2)
        self.assertEqual(reasons["HEDGE_BOOK_STALE"], 1)

    def test_internal_and_edge_gaps_are_explicit(self):
        def mutate(rows):
            delivery_time = self.start + 300000
            rows[0] = self.snapshot(self.start, delivery_time=delivery_time)
            rows[1] = self.snapshot(self.start + 240000, delivery_time=delivery_time)
            rows[2] = self.snapshot(
                delivery_time, delivery=True, delivery_time=delivery_time
            )
            return rows
        def extend(case):
            case["window"]["end_epoch_ms"] = self.start + 300000
            case["option_contract"]["delivery_time_epoch_ms"] = self.start + 300000
        report, _ = self.run_audit(mutate, extend)
        self.assertEqual(report["decision"], lifecycle.INSUFFICIENT_DECISION)
        self.assertEqual(report["lifecycle_coverage"]["coverage_gap_count"], 1)
        self.assertEqual(report["lifecycle_coverage"]["coverage_gaps_preview"][0]["kind"], "INTERNAL_GAP")

    def test_checksum_tamper_is_technical_invalidity(self):
        report, _ = self.run_audit(tamper=True)
        self.assertEqual(report["decision"], lifecycle.INVALID_DECISION)
        self.assertEqual(report["archive_integrity"]["invalid_segment_count"], 1)

    def test_case_before_freeze_and_authority_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "before the frozen observation"):
            self.run_audit(mutate_case=lambda case: case["window"].update(
                start_epoch_ms=int(self.manifest["observation_start_epoch_ms"]) - 1
            ))
        with self.assertRaisesRegex(ValueError, "cannot grant activation"):
            self.run_audit(mutate_case=lambda case: case.update(live_activation_authorized=True))

    def test_report_is_aggregate_only(self):
        report, root_path = self.run_audit()
        rendered = json.dumps(report)
        self.assertNotIn(root_path, rendered)
        self.assertNotIn("scoped_options", rendered)
        self.assertNotIn("ordered_inputs", rendered)
        self.assertNotIn(".jsonl.xz", rendered)
        self.assertNotIn("invalid_segments", rendered)

    def test_workflow_exposes_only_aggregate_index_without_authentication(self):
        workflow = (ROOT / ".github" / "workflows" / "option-archive-lifecycle.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("artifact_suffix=", workflow)
        self.assertIn("steps.validate.outputs.artifact_suffix", workflow)
        self.assertIn("reason_counts", workflow)
        self.assertNotIn("GITHUB_TOKEN", workflow)


if __name__ == "__main__":
    unittest.main()
