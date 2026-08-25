#!/usr/bin/env python3

import gzip
import json
import pathlib
import sys
import tempfile
import time
import unittest
from argparse import Namespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import audit_option_variance_risk_premium_feasibility as audit
import capture_bybit_option_vrp as capture
import run_option_vrp_collector as runner


class OptionVrpCaptureTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000_000
        expiry = self.now + 2 * 86400000
        self.instruments = []
        self.tickers = []
        for side in ("Call", "Put"):
            symbol = f"BTC-17JAN27-100000-{side[0]}-USDT"
            self.instruments.append({
                "symbol": symbol, "deliveryTime": str(expiry), "optionsType": side,
                "deliveryFeeRate": "0.00015", "priceFilter": {"tickSize": "5"},
                "lotSizeFilter": {"minOrderQty": "0.01"},
            })
            self.tickers.append({
                "symbol": symbol, "bid1Price": "950", "ask1Price": "1000",
                "bid1Size": "2", "ask1Size": "2", "markIv": "0.45",
                "indexPrice": "100000", "underlyingPrice": "100010", "volume24h": "50",
            })

    def test_normalize_snapshot_binds_scope_trades_and_hedge(self):
        seen = set()
        snapshot, feature = capture.normalize_snapshot(
            now_epoch_ms=self.now, instruments=self.instruments, tickers=self.tickers,
            trades=[{"symbol": self.tickers[0]["symbol"], "execId": "e1", "price": "975"}],
            hedge_ticker=[{"bid1Price": "99990", "ask1Price": "100010"}],
            hedge_orderbook={"result": {"b": [["99990", "1"]], "a": [["100010", "1"]], "ts": self.now}},
            hv7=[{"value": "0.4"}], hv30=[{"value": "0.35"}], delivery=[], seen_exec_ids=seen,
            minimum_dte_days=0.5, maximum_dte_days=10.0, maximum_absolute_moneyness=0.1,
        )
        self.assertEqual(feature["scoped_two_sided_contract_count"], 2)
        self.assertEqual(feature["atm_pair_count"], 1)
        self.assertEqual(feature["new_trade_count"], 1)
        self.assertEqual(snapshot["hedge_orderbook_l1"]["b"], [["99990", "1"]])
        _, replay = capture.normalize_snapshot(
            now_epoch_ms=self.now, instruments=self.instruments, tickers=self.tickers,
            trades=[{"symbol": self.tickers[0]["symbol"], "execId": "e1"}],
            hedge_ticker=[], hedge_orderbook={"result": {}}, hv7=[], hv30=[], delivery=[], seen_exec_ids=seen,
            minimum_dte_days=0.5, maximum_dte_days=10.0, maximum_absolute_moneyness=0.1,
        )
        self.assertEqual(replay["new_trade_count"], 0)

    def test_capture_audit_rejects_mutated_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "bybit_btc_option_vrp"
            segment = "20270115T000000.000000Z"
            raw = root / "raw" / "BTC" / f"{segment}.jsonl.gz"
            features = root / "features" / "BTC" / f"{segment}.csv"
            report = root / "reports" / "BTC" / f"{segment}.json"
            raw.parent.mkdir(parents=True)
            features.parent.mkdir(parents=True)
            report.parent.mkdir(parents=True)
            with gzip.open(raw, "wt", encoding="utf-8") as handle:
                handle.write("{}\n")
            features.write_text("timestamp_epoch_ms\n1\n", encoding="utf-8")
            payload = {
                "schema_version": capture.SCHEMA_VERSION, "status": "PASS",
                "selection_contract": {
                    "minimum_dte_days": 0.5, "maximum_dte_days": 10.0,
                    "maximum_absolute_moneyness": 0.1, "poll_interval_seconds": 60.0,
                },
                "coverage": {"capture_started_epoch_ms": self.now - 10000, "capture_completed_epoch_ms": self.now, "successful_poll_count": 1},
                "raw": {"path": f"raw/BTC/{segment}.jsonl.gz", "sha256": capture.sha256_file(raw)},
                "features": {"path": f"features/BTC/{segment}.csv", "sha256": capture.sha256_file(features)},
                "quality": {"delivery_times_observed": [self.now - 1000]},
            }
            report.write_text(json.dumps(payload), encoding="utf-8")
            scope = {
                "minimum_dte_days": 0.5, "maximum_dte_days": 10.0,
                "maximum_absolute_moneyness": 0.1, "poll_interval_seconds": 60,
            }
            valid = audit.audit_capture_root(root, now_epoch_ms=self.now, expected_scope=scope)
            self.assertEqual(valid["valid_segment_count"], 1)
            self.assertEqual(valid["completed_expiries_with_delivery"], 1)
            features.write_text("mutated", encoding="utf-8")
            invalid = audit.audit_capture_root(root, now_epoch_ms=self.now)
            self.assertEqual(invalid["invalid_segment_count"], 1)
            self.assertEqual(invalid["checksum_bound_seconds"], 0.0)

    def test_runner_command_and_health_bind_capture_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "bybit_btc_option_vrp"
            args = Namespace(
                poll_interval_sec=60.0, base_url=capture.BASE_URL,
                minimum_dte_days=0.5, maximum_dte_days=10.0,
                maximum_absolute_moneyness=0.1,
            )
            command, report = runner.segment_command(args, root=root, duration_sec=65.0)
            self.assertIn("--poll-interval-sec", command)
            self.assertIn("60.0", command)
            self.assertEqual(report.parent, root / "reports" / "BTC")
            root.mkdir(parents=True)
            (root / "collector_health.json").write_text(json.dumps({
                "schema_version": runner.SCHEMA_VERSION,
                "capture_schema_version": capture.SCHEMA_VERSION,
                "base_coin": capture.BASE_COIN,
                "state": "healthy",
                "last_success_epoch_ms": int(time.time() * 1000),
            }), encoding="utf-8")
            self.assertEqual(runner.healthcheck(Namespace(root=str(root), max_stale_sec=1800)), 0)


if __name__ == "__main__":
    unittest.main()
