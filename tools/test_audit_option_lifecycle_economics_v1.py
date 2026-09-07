#!/usr/bin/env python3

from __future__ import annotations

import copy
import csv
import json
import lzma
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import audit_option_lifecycle_economics_v1 as economics
import capture_bybit_option_lifecycle_v4 as capture


CAPTURE_POLICY = ROOT / "config/option_lifecycle_capture_v4.json"
CAPTURE_MANIFEST = ROOT / "config/option_lifecycle_capture_manifest_v4.json"
PAYOFF_POLICY = ROOT / "config/option_lifecycle_payoff_v2.json"
PAYOFF_MANIFEST = ROOT / "config/option_lifecycle_payoff_manifest_v2.json"
ECONOMIC_POLICY = ROOT / "config/option_lifecycle_economic_v1.json"
ECONOMIC_MANIFEST = ROOT / "config/option_lifecycle_economic_manifest_v1.json"


def instrument(symbol: str, delivery: int, side: str) -> dict:
    return {
        "symbol": symbol,
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "deliveryTime": str(delivery),
        "optionsType": side,
        "lotSizeFilter": {"minOrderQty": "0.01", "qtyStep": "0.01"},
        "priceFilter": {"tickSize": "5"},
        "deliveryFeeRate": "0.00015",
    }


def ticker(symbol: str, side: str, premium: float) -> dict:
    return {
        "symbol": symbol,
        "indexPrice": "80000",
        "bid1Price": str(premium),
        "ask1Price": str(premium + 10),
        "bid1Size": "1",
        "ask1Size": "1",
        "delta": "0.5" if side == "Call" else "-0.4",
        "bid1Iv": "0.5",
        "ask1Iv": "0.6",
        "markPrice": str(premium + 5),
        "markIv": "0.55",
        "underlyingPrice": "80000",
        "openInterest": "10",
        "volume24h": "10",
        "turnover24h": "1000",
        "gamma": "0.1",
        "vega": "1",
        "theta": "-1",
    }


class OptionLifecycleEconomicsV1Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capture_policy, cls.capture_manifest = capture.load_contract(
            CAPTURE_POLICY, CAPTURE_MANIFEST
        )
        cls.start = int(cls.capture_manifest["observation_start_epoch_ms"])

    def lifecycle(self, index: int, selected: int, delivery: int, premium: float) -> dict:
        call = f"BTC-L{index}-80000-C-USDT"
        put = f"BTC-L{index}-80000-P-USDT"
        instruments = capture._instrument_map(
            [instrument(call, delivery, "Call"), instrument(put, delivery, "Put")]
        )
        tickers = capture._ticker_map(
            [ticker(call, "Call", premium), ticker(put, "Put", premium)]
        )
        rows = [
            {
                "symbol": symbol,
                "deliveryTime": delivery,
                "strike": 80000.0,
                "optionsType": side,
                "dteDays": 1.0,
                "moneyness": 0.0,
                "indexPrice": "80000",
                "bid1Price": str(premium),
                "ask1Price": str(premium + 10),
                "bid1Size": "1",
                "ask1Size": "1",
            }
            for symbol, side in ((call, "Call"), (put, "Put"))
        ]
        lifecycle = capture.select_lifecycle(
            now_epoch_ms=selected,
            rows=rows,
            instruments=instruments,
            policy=self.capture_policy,
        )
        self.assertIsNotNone(lifecycle)
        return lifecycle

    def snapshot(
        self,
        timestamp: int,
        lifecycle: dict,
        *,
        premium: float,
        delivery_price: float | None = None,
    ) -> dict:
        tracked = [
            {
                **contract,
                **ticker(contract["symbol"], contract["optionsType"], premium),
                "observation_status": "OBSERVED",
            }
            for contract in lifecycle["contracts"]
        ]
        deliveries = []
        if delivery_price is not None:
            deliveries = [
                {
                    "symbol": symbol,
                    "deliveryTime": lifecycle["delivery_time_epoch_ms"],
                    "deliveryPrice": str(delivery_price),
                    "deliveryPriceNumeric": delivery_price,
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "settleCoin": "USDT",
                    "lifecycleIdentitySha256": lifecycle["identity_sha256"],
                }
                for symbol in lifecycle["symbols"]
            ]
        return {
            "schema_version": capture.SNAPSHOT_SCHEMA_VERSION,
            "experiment_id": self.capture_policy["experiment_id"],
            "policy_canonical_sha256": capture.canonical_sha256(
                self.capture_policy
            ),
            "manifest_canonical_sha256": capture.canonical_sha256(
                self.capture_manifest
            ),
            "timestamp_epoch_ms": timestamp,
            "poll_started_epoch_ms": timestamp,
            "snapshot_completed_epoch_ms": timestamp + 1000,
            "discovery_contract": self.capture_policy["discovery_contract"],
            "tracking_contract": self.capture_policy["tracking_contract"],
            "discovery_options": [
                {
                    "symbol": contract["symbol"],
                    "deliveryTime": lifecycle["delivery_time_epoch_ms"],
                    "strike": lifecycle["strike"],
                    "optionsType": contract["optionsType"],
                    "dteDays": 1.0,
                    "moneyness": 0.0,
                }
                for contract in lifecycle["contracts"]
            ],
            "active_lifecycle": lifecycle,
            "tracked_options": tracked,
            "delivery_prices": deliveries,
            "hedge_ticker": {"bid1Price": "79999", "ask1Price": "80001"},
            "hedge_orderbook_l1": {
                "ts": timestamp + 500,
                "b": [["79999", "1"]],
                "a": [["80001", "1"]],
            },
            "lifecycle_phase": (
                "DELIVERY_OBSERVED" if deliveries else "ACTIVE"
            ),
        }

    def archive(self, root: pathlib.Path, lifecycle_count: int, premium: float) -> int:
        for directory in ("raw/BTC", "features/BTC", "reports/BTC"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        selected = self.start
        final_delivery = selected
        segment_index = 0
        for lifecycle_index in range(lifecycle_count):
            delivery = selected + 720000
            lifecycle = self.lifecycle(
                lifecycle_index, selected, delivery, premium
            )
            timestamps = list(range(selected, delivery + 1, 180000))
            snapshots = [
                self.snapshot(
                    timestamp,
                    lifecycle,
                    premium=premium,
                    delivery_price=80000.0 if timestamp == delivery else None,
                )
                for timestamp in timestamps
            ]
            for part in (snapshots[:3], snapshots[3:]):
                segment = f"20260908T{segment_index:06d}.000000Z"
                segment_index += 1
                raw = root / "raw/BTC" / f"{segment}.jsonl.xz"
                feature = root / "features/BTC" / f"{segment}.csv"
                report = root / "reports/BTC" / f"{segment}.json"
                with lzma.open(raw, "wt", encoding="utf-8") as handle:
                    for snapshot in part:
                        handle.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
                with feature.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=capture.OUTPUT_FIELDS)
                    writer.writeheader()
                    for snapshot in part:
                        writer.writerow(
                            {"timestamp_epoch_ms": snapshot["timestamp_epoch_ms"]}
                        )
                state = capture.initial_state(
                    policy=self.capture_policy, manifest=self.capture_manifest
                )
                report_payload = capture.build_report(
                    root=root,
                    raw_path=raw,
                    feature_path=feature,
                    features=[
                        {
                            "poll_latency_ms": 1000,
                            "tracked_observed_count": 2,
                            "paired_delivery_evidence_count": len(
                                snapshot["delivery_prices"]
                            ),
                        }
                        for snapshot in part
                    ],
                    started=part[0]["timestamp_epoch_ms"],
                    completed=part[-1]["timestamp_epoch_ms"],
                    termination_reason="duration_complete",
                    policy=self.capture_policy,
                    manifest=self.capture_manifest,
                    state_before_sha256=capture.canonical_sha256(state),
                    state_after=state,
                )
                report.write_text(json.dumps(report_payload), encoding="utf-8")
            selected = delivery + 60000
            final_delivery = delivery
        return final_delivery

    def run_audit(self, root: pathlib.Path, generated: int) -> dict:
        return economics.audit(
            root=root,
            capture_policy_path=CAPTURE_POLICY,
            capture_manifest_path=CAPTURE_MANIFEST,
            payoff_policy_path=PAYOFF_POLICY,
            payoff_manifest_path=PAYOFF_MANIFEST,
            economic_policy_path=ECONOMIC_POLICY,
            economic_manifest_path=ECONOMIC_MANIFEST,
            generated_at_epoch_ms=generated,
        )

    def test_one_lifecycle_reports_one_of_six_without_profitability_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / capture.CAPTURE_ROOT_NAME
            delivery = self.archive(root, 1, premium=500.0)
            report = self.run_audit(root, delivery + 1000)
        self.assertEqual(
            report["decision"],
            "WAIT_FOR_MULTI_LIFECYCLE_ECONOMIC_EVIDENCE",
        )
        self.assertEqual(report["progress"]["complete_lifecycle_count"], 1)
        self.assertEqual(report["progress"]["minimum_complete_lifecycles"], 6)
        self.assertFalse(report["economic_evidence"])
        self.assertFalse(report["profitability_claim_allowed"])
        self.assertFalse(report["demo_activation_authorized"])

    def test_six_positive_lifecycles_pass_for_demo_review_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / capture.CAPTURE_ROOT_NAME
            delivery = self.archive(root, 6, premium=500.0)
            report = self.run_audit(root, delivery + 1000)
        self.assertEqual(
            report["decision"],
            "PASS_MULTI_LIFECYCLE_ECONOMICS_FOR_DEMO_REVIEW_ONLY",
        )
        self.assertEqual(report["progress"]["complete_lifecycle_count"], 6)
        self.assertTrue(report["economic_evidence"])
        self.assertTrue(report["demo_review_eligible"])
        self.assertFalse(report["demo_activation_authorized"])
        short = report["action_aggregates"]["short_selected_straddle"]
        self.assertGreater(short["stress_mean_lcb_bps"], 0.0)
        self.assertEqual(short["stress_positive_ratio"], 1.0)

    def test_checksum_drift_invalidates_entire_economic_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / capture.CAPTURE_ROOT_NAME
            delivery = self.archive(root, 1, premium=500.0)
            raw = next((root / "raw/BTC").glob("*.xz"))
            raw.write_bytes(raw.read_bytes() + b"drift")
            report = self.run_audit(root, delivery + 1000)
        self.assertEqual(
            report["decision"], "INVALID_MULTI_LIFECYCLE_ECONOMIC_ARCHIVE"
        )

    def test_frozen_policy_drift_is_rejected(self):
        policy = json.loads(ECONOMIC_POLICY.read_text(encoding="utf-8"))
        policy["aggregation_contract"]["minimum_complete_lifecycles"] = 5
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen policy identity"):
                economics.load_contract(
                    path, ECONOMIC_MANIFEST, PAYOFF_POLICY, PAYOFF_MANIFEST
                )

    def test_cli_without_archive_returns_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = pathlib.Path(temporary)
            output = temp / "economic.json"
            root = temp / capture.CAPTURE_ROOT_NAME
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/audit_option_lifecycle_economics_v1.py"),
                    "--root",
                    str(root),
                    "--capture-policy",
                    str(CAPTURE_POLICY),
                    "--capture-manifest",
                    str(CAPTURE_MANIFEST),
                    "--payoff-policy",
                    str(PAYOFF_POLICY),
                    "--payoff-manifest",
                    str(PAYOFF_MANIFEST),
                    "--economic-policy",
                    str(ECONOMIC_POLICY),
                    "--economic-manifest",
                    str(ECONOMIC_MANIFEST),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            report["decision"],
            "WAIT_FOR_MULTI_LIFECYCLE_ECONOMIC_EVIDENCE",
        )


if __name__ == "__main__":
    unittest.main()
