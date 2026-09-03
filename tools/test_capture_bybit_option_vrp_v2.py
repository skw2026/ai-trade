#!/usr/bin/env python3

import json
import lzma
import pathlib
import sys
import tempfile
import time
import unittest
from argparse import Namespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import capture_bybit_option_vrp_v2 as capture
import run_option_vrp_collector as runner


class OptionVrpCaptureV2Test(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000_000
        self.expiry = self.now + 2 * 86_400_000
        self.instruments = []
        self.tickers = []
        for side in ("Call", "Put"):
            symbol = f"BTC-17JAN27-100000-{side[0]}-USDT"
            self.instruments.append({
                "symbol": symbol,
                "deliveryTime": str(self.expiry),
                "optionsType": side,
                "baseCoin": "BTC",
                "quoteCoin": "USDT",
                "settleCoin": "USDT",
                "deliveryFeeRate": "0.00015",
                "priceFilter": {"tickSize": "5"},
                "lotSizeFilter": {"minOrderQty": "0.01", "qtyStep": "0.01"},
            })
            self.tickers.append({
                "symbol": symbol,
                "bid1Price": "950",
                "ask1Price": "1000",
                "bid1Size": "2",
                "ask1Size": "2",
                "markIv": "0.45",
                "indexPrice": "100000",
                "underlyingPrice": "100010",
                "volume24h": "50",
                "delta": "0.5" if side == "Call" else "-0.5",
            })
        self.usdc_symbol = "BTC-17JAN27-100000-C"
        self.instruments.append({
            "symbol": self.usdc_symbol,
            "deliveryTime": str(self.expiry),
            "optionsType": "Call",
            "baseCoin": "BTC",
            "quoteCoin": "USDC",
            "settleCoin": "USDC",
            "deliveryFeeRate": "0.00015",
            "lotSizeFilter": {"minOrderQty": "0.01", "qtyStep": "0.01"},
        })
        self.tickers.append({
            "symbol": self.usdc_symbol,
            "bid1Price": "950",
            "ask1Price": "1000",
            "bid1Size": "2",
            "ask1Size": "2",
            "indexPrice": "100000",
        })

    def delivery_rows(self):
        return [
            {
                "symbol": instrument["symbol"],
                "deliveryPrice": "101000",
                "deliveryTime": str(self.expiry),
            }
            for instrument in self.instruments
            if instrument["settleCoin"] == "USDT"
        ]

    def test_normalize_binds_usdt_contract_units_and_delivery_identity(self):
        snapshot, feature = capture.normalize_snapshot(
            now_epoch_ms=self.now,
            instruments=self.instruments,
            tickers=self.tickers,
            trades=[],
            hedge_ticker=[{"bid1Price": "99990", "ask1Price": "100010"}],
            hedge_orderbook={"result": {"b": [["99990", "1"]], "a": [["100010", "1"]], "ts": self.now}},
            hv7=[{"value": "0.4"}],
            hv30=[{"value": "0.35"}],
            delivery=self.delivery_rows(),
            seen_exec_ids=set(),
            minimum_dte_days=0.5,
            maximum_dte_days=10.0,
            maximum_absolute_moneyness=0.1,
        )
        self.assertEqual(snapshot["schema_version"], capture.SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(snapshot["scope_contract"], capture.SCOPE_CONTRACT)
        self.assertEqual(snapshot["scope_identity_sha256"], capture.SCOPE_IDENTITY_SHA256)
        self.assertEqual(feature["scoped_two_sided_contract_count"], 2)
        self.assertNotIn(self.usdc_symbol, {row["symbol"] for row in snapshot["scoped_options"]})
        for row in snapshot["scoped_options"]:
            self.assertEqual(row["baseCoin"], "BTC")
            self.assertEqual(row["quoteCoin"], "USDT")
            self.assertEqual(row["settleCoin"], "USDT")
            self.assertEqual(row["minOrderQty"], "0.01")
            self.assertEqual(row["qtyStep"], "0.01")
            self.assertEqual(row["deliveryFeeRate"], "0.00015")
        self.assertEqual(snapshot["delivery_times"], [self.expiry])
        self.assertEqual(len(snapshot["delivery_prices"]), 2)
        self.assertTrue(all(row["settleCoin"] == "USDT" for row in snapshot["delivery_prices"]))

    def test_normalize_rejects_conflicting_delivery_timestamp(self):
        bad = self.delivery_rows()
        bad[0]["deliveryTime"] = str(self.expiry + 1)
        with self.assertRaisesRegex(ValueError, "delivery identity mismatch"):
            capture.normalize_snapshot(
                now_epoch_ms=self.now,
                instruments=self.instruments,
                tickers=self.tickers,
                trades=[],
                hedge_ticker=[],
                hedge_orderbook={"result": {}},
                hv7=[],
                hv30=[],
                delivery=bad,
                seen_exec_ids=set(),
                minimum_dte_days=0.5,
                maximum_dte_days=10.0,
                maximum_absolute_moneyness=0.1,
            )

    def test_capture_requests_explicit_usdt_delivery_scope(self):
        calls = []

        def fetcher(path, params, *, base_url):
            calls.append((path, dict(params), base_url))
            if path.endswith("instruments-info"):
                rows = self.instruments
            elif path.endswith("recent-trade"):
                rows = []
            elif path.endswith("historical-volatility"):
                rows = [{"value": "0.4"}]
            elif path.endswith("delivery-price"):
                rows = self.delivery_rows()
            elif path.endswith("orderbook"):
                return {"retCode": 0, "result": {"b": [["99990", "1"]], "a": [["100010", "1"]], "ts": self.now}}
            elif params.get("category") == "linear":
                rows = [{"bid1Price": "99990", "ask1Price": "100010"}]
            else:
                rows = self.tickers
            return {"retCode": 0, "result": {"list": rows}}

        with tempfile.TemporaryDirectory() as temporary:
            raw = pathlib.Path(temporary) / "segment.jsonl.xz"
            rows, _, _, deliveries, termination_reason = capture.capture_live(
                raw_output=raw,
                duration_sec=0.0,
                poll_interval_sec=60.0,
                base_url="https://example.invalid",
                minimum_dte_days=0.5,
                maximum_dte_days=10.0,
                maximum_absolute_moneyness=0.1,
                fetcher=fetcher,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(deliveries, [self.expiry])
            self.assertEqual(termination_reason, "duration_complete")
            with lzma.open(raw, "rt", encoding="utf-8") as handle:
                payload = json.loads(handle.readline())
            self.assertEqual(payload["scope_identity_sha256"], capture.SCOPE_IDENTITY_SHA256)
        delivery_calls = [params for path, params, _ in calls if path.endswith("delivery-price")]
        self.assertEqual(delivery_calls, [{"category": "option", "baseCoin": "BTC", "settleCoin": "USDT", "limit": 200}])

    def test_capture_preserves_valid_prefix_after_transient_poll_failure(self):
        calls = 0

        def fetcher(path, params, *, base_url):
            nonlocal calls
            calls += 1
            if calls == 9:
                raise RuntimeError("transient public API failure")
            if path.endswith("instruments-info"):
                rows = self.instruments
            elif path.endswith("recent-trade"):
                rows = []
            elif path.endswith("historical-volatility"):
                rows = [{"value": "0.4"}]
            elif path.endswith("delivery-price"):
                rows = self.delivery_rows()
            elif path.endswith("orderbook"):
                return {"retCode": 0, "result": {"b": [["99990", "1"]], "a": [["100010", "1"]], "ts": self.now}}
            elif params.get("category") == "linear":
                rows = [{"bid1Price": "99990", "ask1Price": "100010"}]
            else:
                rows = self.tickers
            return {"retCode": 0, "result": {"list": rows}}

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / capture.CAPTURE_ROOT_NAME
            raw = root / "raw" / capture.BASE_COIN / "segment.jsonl.xz"
            features = root / "features" / capture.BASE_COIN / "segment.csv"
            rows, started, completed, deliveries, termination_reason = capture.capture_live(
                raw_output=raw,
                duration_sec=1.0,
                poll_interval_sec=0.001,
                base_url="https://example.invalid",
                minimum_dte_days=0.5,
                maximum_dte_days=10.0,
                maximum_absolute_moneyness=0.1,
                fetcher=fetcher,
            )
            self.assertEqual(len(rows), 1)
            self.assertGreater(started, 0)
            self.assertEqual(completed, started)
            self.assertEqual(deliveries, [self.expiry])
            self.assertEqual(termination_reason, "transient_request_failure")
            with lzma.open(raw, "rt", encoding="utf-8") as handle:
                self.assertEqual(len(handle.readlines()), 1)
            capture.legacy._write_feature_csv(features, rows)
            report = capture.build_report(
                capture_root=root,
                raw_path=raw,
                feature_path=features,
                feature_rows=rows,
                started_epoch_ms=started,
                completed_epoch_ms=completed,
                delivery_times=deliveries,
                base_url="https://example.invalid",
                poll_interval_sec=0.001,
                minimum_dte_days=0.5,
                maximum_dte_days=10.0,
                maximum_absolute_moneyness=0.1,
                capture_termination_reason=termination_reason,
            )
            self.assertEqual(
                report["quality"]["capture_termination_reason"],
                "transient_request_failure",
            )
            self.assertTrue(report["quality"]["partial_segment_preserved"])
            self.assertEqual(report["coverage"]["duration_ms"], 0)

    def test_capture_does_not_mask_transient_failure_before_first_snapshot(self):
        def fetcher(path, params, *, base_url):
            if path.endswith("instruments-info"):
                return {"retCode": 0, "result": {"list": self.instruments}}
            raise RuntimeError("transient public API failure")

        with tempfile.TemporaryDirectory() as temporary:
            raw = pathlib.Path(temporary) / "segment.jsonl.xz"
            with self.assertRaisesRegex(RuntimeError, "transient public API failure"):
                capture.capture_live(
                    raw_output=raw,
                    duration_sec=1.0,
                    poll_interval_sec=0.001,
                    base_url="https://example.invalid",
                    minimum_dte_days=0.5,
                    maximum_dte_days=10.0,
                    maximum_absolute_moneyness=0.1,
                    fetcher=fetcher,
                )

    def test_runner_command_and_health_bind_v2_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / capture.CAPTURE_ROOT_NAME
            args = Namespace(
                poll_interval_sec=60.0,
                base_url=capture.BASE_URL,
                minimum_dte_days=0.5,
                maximum_dte_days=10.0,
                maximum_absolute_moneyness=0.1,
            )
            command, report = runner.segment_command(args, root=root, duration_sec=65.0)
            self.assertTrue(any(str(part).endswith("capture_bybit_option_vrp_v2.py") for part in command))
            self.assertTrue(any(str(part).endswith(".jsonl.xz") for part in command))
            self.assertEqual(report.parent, root / "reports" / capture.BASE_COIN)
            root.mkdir(parents=True)
            (root / "collector_health.json").write_text(json.dumps({
                "schema_version": runner.SCHEMA_VERSION,
                "capture_schema_version": capture.SCHEMA_VERSION,
                "snapshot_schema_version": capture.SNAPSHOT_SCHEMA_VERSION,
                "scope_identity_sha256": capture.SCOPE_IDENTITY_SHA256,
                "raw_codec": capture.RAW_CODEC,
                "base_coin": capture.BASE_COIN,
                "settle_coin": capture.SETTLE_COIN,
                "delivery_query_status": "PASS",
                "state": "healthy",
                "last_success_epoch_ms": int(time.time() * 1000),
            }), encoding="utf-8")
            self.assertEqual(runner.healthcheck(Namespace(root=str(root), max_stale_sec=1800)), 0)


if __name__ == "__main__":
    unittest.main()
