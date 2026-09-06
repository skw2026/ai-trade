#!/usr/bin/env python3

from __future__ import annotations

import copy
import csv
import json
import lzma
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import audit_option_lifecycle_v3 as audit
import capture_bybit_option_lifecycle_v3 as capture
import run_option_lifecycle_collector_v3 as runner


POLICY_PATH = ROOT / "config/option_lifecycle_capture_v3.json"
MANIFEST_PATH = ROOT / "config/option_lifecycle_capture_manifest_v3.json"


def instrument(symbol: str, delivery: int, side: str) -> dict:
    return {
        "symbol": symbol, "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
        "deliveryTime": str(delivery), "optionsType": side,
        "lotSizeFilter": {"minOrderQty": "0.01", "qtyStep": "0.01"},
        "priceFilter": {"tickSize": "5"}, "deliveryFeeRate": "0.00015",
    }


def ticker(symbol: str, *, index: str = "80000", bid: str = "100",
           ask: str = "110", delta: str = "0.5") -> dict:
    return {
        "symbol": symbol, "indexPrice": index, "bid1Price": bid, "ask1Price": ask,
        "bid1Size": "1", "ask1Size": "1", "delta": delta,
        "bid1Iv": "0.5", "ask1Iv": "0.6", "markPrice": "105", "markIv": "0.55",
        "underlyingPrice": index, "openInterest": "10", "volume24h": "10",
        "turnover24h": "1000", "gamma": "0.1", "vega": "1", "theta": "-1",
    }


class OptionLifecycleV3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy, cls.manifest = capture.load_contract(POLICY_PATH, MANIFEST_PATH)
        cls.start = int(cls.manifest["observation_start_epoch_ms"])

    def pair(self, delivery: int, strike: int = 80000):
        call = f"BTC-7SEP26-{strike}-C-USDT"
        put = f"BTC-7SEP26-{strike}-P-USDT"
        instruments = capture._instrument_map([
            instrument(call, delivery, "Call"), instrument(put, delivery, "Put")
        ])
        tickers = capture._ticker_map([ticker(call), ticker(put, delta="-0.5")])
        return call, put, instruments, tickers

    def test_deterministic_selection_and_sticky_missing_evidence(self):
        delivery = self.start + 86400000
        call, put, instruments, tickers = self.pair(delivery)
        rows = capture.discovery_rows(
            now_epoch_ms=self.start, instruments=instruments, tickers=tickers,
            policy=self.policy,
        )
        lifecycle = capture.select_lifecycle(
            now_epoch_ms=self.start, rows=list(reversed(rows)), instruments=instruments,
            policy=self.policy,
        )
        self.assertEqual(lifecycle["symbols"], [call, put])
        # Discovery would now reject the pair, but tracking must preserve both identities.
        far_tickers = capture._ticker_map([ticker(call, index="200000")])
        self.assertEqual(capture.discovery_rows(
            now_epoch_ms=delivery - 1000, instruments=instruments,
            tickers=far_tickers, policy=self.policy,
        ), [])
        tracked = capture.tracked_rows(
            lifecycle=lifecycle, instruments=instruments, tickers=far_tickers
        )
        self.assertEqual([row["symbol"] for row in tracked], [call, put])
        self.assertEqual([row["observation_status"] for row in tracked], ["OBSERVED", "TICKER_MISSING"])
        self.assertNotIn("bid1Price", tracked[1])

    def test_paired_delivery_deduplicates_and_rejects_conflicts(self):
        delivery = self.start + 86400000
        call, put, instruments, tickers = self.pair(delivery)
        lifecycle = capture.select_lifecycle(
            now_epoch_ms=self.start,
            rows=capture.discovery_rows(now_epoch_ms=self.start, instruments=instruments,
                                        tickers=tickers, policy=self.policy),
            instruments=instruments, policy=self.policy,
        )
        rows = [
            {"symbol": call, "deliveryTime": delivery, "deliveryPrice": "80100"},
            {"symbol": call, "deliveryTime": delivery, "deliveryPrice": "80100"},
            {"symbol": put, "deliveryTime": delivery, "deliveryPrice": "80100"},
        ]
        self.assertEqual(len(capture.delivery_rows(lifecycle=lifecycle, rows=rows)), 2)
        rows[1]["deliveryPrice"] = "80200"
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            capture.delivery_rows(lifecycle=lifecycle, rows=rows)

    def test_contract_rejects_authority_and_policy_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = pathlib.Path(temporary)
            policy = copy.deepcopy(self.policy)
            manifest = copy.deepcopy(self.manifest)
            policy["authorities"]["demo_activation_authorized"] = True
            manifest["policy_canonical_sha256"] = capture.canonical_sha256(policy)
            policy_path, manifest_path = temp / "p.json", temp / "m.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen policy identity"):
                capture.load_contract(policy_path, manifest_path)
            manifest["policy_canonical_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "policy identity"):
                capture.load_contract(policy_path, manifest_path)

    def _snapshot(self, timestamp: int, lifecycle: dict, *, delivery: bool = False,
                  missing: bool = False) -> dict:
        tracked = [] if missing else [
            {**contract, **ticker(contract["symbol"], delta="0.5" if contract["optionsType"] == "Call" else "-0.5"),
             "observation_status": "OBSERVED"}
            for contract in lifecycle["contracts"]
        ]
        if missing:
            tracked = [{**contract, "observation_status": "TICKER_MISSING"}
                       for contract in lifecycle["contracts"]]
        deliveries = []
        if delivery:
            deliveries = [{
                "symbol": symbol, "deliveryTime": lifecycle["delivery_time_epoch_ms"],
                "deliveryPrice": "80100", "deliveryPriceNumeric": 80100.0,
                "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
                "lifecycleIdentitySha256": lifecycle["identity_sha256"],
            } for symbol in lifecycle["symbols"]]
        return {
            "schema_version": capture.SNAPSHOT_SCHEMA_VERSION,
            "experiment_id": self.policy["experiment_id"],
            "policy_canonical_sha256": capture.canonical_sha256(self.policy),
            "manifest_canonical_sha256": capture.canonical_sha256(self.manifest),
            "timestamp_epoch_ms": timestamp, "poll_started_epoch_ms": timestamp,
            "snapshot_completed_epoch_ms": timestamp + 1000,
            "discovery_contract": self.policy["discovery_contract"],
            "tracking_contract": self.policy["tracking_contract"],
            "discovery_options": [{
                "symbol": contract["symbol"], "deliveryTime": lifecycle["delivery_time_epoch_ms"],
                "strike": lifecycle["strike"], "optionsType": contract["optionsType"],
                "dteDays": lifecycle["selection_dte_days"],
                "moneyness": lifecycle["selection_moneyness"],
            } for contract in lifecycle["contracts"]],
            "active_lifecycle": lifecycle,
            "tracked_options": tracked, "delivery_prices": deliveries,
            "hedge_ticker": {"bid1Price": "79999", "ask1Price": "80001"},
            "hedge_orderbook_l1": {"ts": timestamp + 500, "b": [["79999", "1"]], "a": [["80001", "1"]]},
            "lifecycle_phase": "DELIVERY_OBSERVED" if delivery else "ACTIVE",
        }

    def _archive(self, root: pathlib.Path, snapshots_by_segment: list[list[dict]]) -> None:
        for directory in ("raw/BTC", "features/BTC", "reports/BTC"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        for index, snapshots in enumerate(snapshots_by_segment):
            segment = f"20260906T120{index}00.000000Z"
            raw = root / "raw/BTC" / f"{segment}.jsonl.xz"
            feature = root / "features/BTC" / f"{segment}.csv"
            report = root / "reports/BTC" / f"{segment}.json"
            with lzma.open(raw, "wt", encoding="utf-8") as handle:
                for snapshot in snapshots:
                    handle.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
            with feature.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=capture.OUTPUT_FIELDS)
                writer.writeheader()
                for snapshot in snapshots:
                    writer.writerow({"timestamp_epoch_ms": snapshot["timestamp_epoch_ms"]})
            state = capture.initial_state(policy=self.policy, manifest=self.manifest)
            features = [{"poll_latency_ms": 1000, "tracked_observed_count": 2,
                         "paired_delivery_evidence_count": len(snapshot["delivery_prices"])}
                        for snapshot in snapshots]
            payload = capture.build_report(
                root=root, raw_path=raw, feature_path=feature, features=features,
                started=snapshots[0]["timestamp_epoch_ms"],
                completed=snapshots[-1]["timestamp_epoch_ms"],
                termination_reason="duration_complete", policy=self.policy,
                manifest=self.manifest, state_before_sha256=capture.canonical_sha256(state),
                state_after=state,
            )
            report.write_text(json.dumps(payload), encoding="utf-8")

    def lifecycle(self, delivery: int) -> dict:
        _, _, instruments, tickers = self.pair(delivery)
        return capture.select_lifecycle(
            now_epoch_ms=self.start,
            rows=capture.discovery_rows(now_epoch_ms=self.start, instruments=instruments,
                                        tickers=tickers, policy=self.policy),
            instruments=instruments, policy=self.policy,
        )

    def test_audit_startup_pass_waits_for_delivery(self):
        delivery = self.start + 86400000
        lifecycle = self.lifecycle(delivery)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / capture.CAPTURE_ROOT_NAME
            self._archive(root, [
                [self._snapshot(self.start, lifecycle), self._snapshot(self.start + 60000, lifecycle)],
                [self._snapshot(self.start + 120000, lifecycle)],
            ])
            report = audit.audit(
                root=root, policy_path=POLICY_PATH, manifest_path=MANIFEST_PATH,
                generated_at_epoch_ms=self.start + 120000,
            )
            self.assertEqual(report["startup_gate"]["status"], "PASS")
            self.assertTrue(report["startup_gate"]["sticky_cross_segment_continuity"])
            self.assertEqual(report["decision"], "WAIT_FOR_FIRST_COMPLETE_LIFECYCLE")

    def test_audit_complete_lifecycle_passes_and_checksum_drift_fails(self):
        delivery = self.start + 86400000
        lifecycle = self.lifecycle(delivery)
        timestamps = list(range(self.start, delivery + 1, 180000))
        snapshots = [
            self._snapshot(timestamp, lifecycle, delivery=timestamp == delivery)
            for timestamp in timestamps
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / capture.CAPTURE_ROOT_NAME
            self._archive(root, [snapshots[:240], snapshots[240:]])
            report = audit.audit(
                root=root, policy_path=POLICY_PATH, manifest_path=MANIFEST_PATH,
                generated_at_epoch_ms=delivery + 1000,
            )
            self.assertEqual(report["decision"], "PASS_FIRST_COMPLETE_LIFECYCLE_FOR_PAYOFF_RECONSTRUCTION_ONLY")
            raw = next((root / "raw/BTC").glob("*.xz"))
            raw.write_bytes(raw.read_bytes() + b"drift")
            report = audit.audit(
                root=root, policy_path=POLICY_PATH, manifest_path=MANIFEST_PATH,
                generated_at_epoch_ms=delivery + 1000,
            )
            self.assertEqual(report["decision"], "INVALID_OPTION_LIFECYCLE_ARCHIVE")

    def test_runner_binds_state_and_frozen_contract(self):
        args = type("Args", (), {
            "poll_interval_sec": 60, "base_url": capture.BASE_URL,
            "policy": str(POLICY_PATH), "manifest": str(MANIFEST_PATH),
        })()
        command, _ = runner.segment_command(
            args, root=pathlib.Path("/tmp") / capture.CAPTURE_ROOT_NAME,
            duration_sec=65,
        )
        rendered = " ".join(map(str, command))
        self.assertIn("capture_bybit_option_lifecycle_v3.py", rendered)
        self.assertIn("tracking_state.json", rendered)
        self.assertIn(str(POLICY_PATH), rendered)
        self.assertIn(str(MANIFEST_PATH), rendered)


if __name__ == "__main__":
    unittest.main()
