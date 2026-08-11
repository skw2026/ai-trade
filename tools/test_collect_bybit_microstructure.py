#!/usr/bin/env python3

import gzip
import json
import pathlib
import tempfile
import unittest

import collect_bybit_microstructure as micro


SNAPSHOT = {
    "topic": "orderbook.50.SOLUSDT",
    "type": "snapshot",
    "ts": 1000,
    "cts": 999,
    "data": {
        "s": "SOLUSDT",
        "b": [["99", "2"], ["98", "3"]],
        "a": [["101", "1"], ["102", "4"]],
        "u": 10,
        "seq": 20,
    },
}


def snapshot(symbol, timestamp=999, mid=100.0):
    return {
        "topic": f"orderbook.50.{symbol}",
        "type": "snapshot",
        "ts": timestamp + 1,
        "cts": timestamp,
        "data": {
            "s": symbol,
            "b": [[str(mid - 1.0), "2"], [str(mid - 2.0), "3"]],
            "a": [[str(mid + 1.0), "1"], [str(mid + 2.0), "4"]],
            "u": 10,
            "seq": 20,
        },
    }


def trades(symbol, timestamp=1200, mid=100.0):
    return {
        "topic": f"publicTrade.{symbol}",
        "type": "snapshot",
        "ts": timestamp,
        "data": [
            {
                "T": timestamp,
                "s": symbol,
                "S": "Buy",
                "v": "2",
                "p": str(mid),
                "i": f"{symbol}-{timestamp}-a",
            },
            {
                "T": timestamp + 100,
                "s": symbol,
                "S": "Sell",
                "v": "1",
                "p": str(mid + 1.0),
                "i": f"{symbol}-{timestamp}-b",
            },
        ],
    }


class MicrostructureTest(unittest.TestCase):
    def test_snapshot_delta_and_size_zero_delete(self):
        book = micro.OrderBook()
        self.assertEqual(book.apply(SNAPSHOT), 999)
        delta = {
            "topic": "orderbook.50.SOLUSDT",
            "type": "delta",
            "ts": 1100,
            "cts": 1099,
            "data": {"b": [["99", "0"], ["100", "4"]], "a": [], "u": 11, "seq": 21},
        }
        book.apply(delta)
        metrics = book.metrics()
        self.assertEqual(metrics["best_bid"], 100.0)
        self.assertNotIn(99.0, book.bids)
        self.assertGreater(metrics["book_imbalance_l1"], 0.0)
        self.assertGreater(book.last_flow_metrics["book_flow_signed_quote"], 0.0)
        self.assertGreater(book.last_flow_metrics["book_flow_abs_quote"], 0.0)
        self.assertGreater(book.last_flow_metrics["book_ofi"], 0.0)

    def test_delta_before_snapshot_fails_closed(self):
        delta = {
            "topic": "orderbook.50.SOLUSDT",
            "type": "delta",
            "ts": 1000,
            "data": {"b": [["99", "1"]], "a": [["101", "1"]], "u": 2, "seq": 2},
        }
        with self.assertRaisesRegex(ValueError, "before snapshot"):
            micro.OrderBook().apply(delta)

    def test_replay_is_deterministic_and_aggregates_taker_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw = pathlib.Path(temp_dir) / "raw.jsonl.gz"
            with gzip.open(raw, "wt", encoding="utf-8") as handle:
                for symbol, mid in (("SOLUSDT", 100.0), ("BTCUSDT", 1000.0), ("ETHUSDT", 500.0)):
                    handle.write(json.dumps(snapshot(symbol, mid=mid)) + "\n")
                    handle.write(json.dumps(trades(symbol, mid=mid)) + "\n")
                    handle.write(
                        json.dumps(snapshot(symbol, timestamp=2999, mid=mid)) + "\n"
                    )
                    handle.write(
                        json.dumps(trades(symbol, timestamp=2200, mid=mid)) + "\n"
                    )
            first, count = micro.replay_jsonl(raw, symbol="SOLUSDT", bucket_ms=1000)
            second, _ = micro.replay_jsonl(raw, symbol="SOLUSDT", bucket_ms=1000)
            self.assertEqual(first, second)
            self.assertEqual(count, 12)
            self.assertEqual([row["timestamp"] for row in first], [0, 1000])
            trade_row = next(row for row in first if row["timestamp"] == 1000)
            self.assertEqual(trade_row["trade_count"], 2)
            self.assertAlmostEqual(trade_row["trade_imbalance"], 99.0 / 301.0)
            self.assertGreater(trade_row["buy_base_volume"], 0.0)
            self.assertGreater(trade_row["sell_base_volume"], 0.0)
            self.assertAlmostEqual(
                trade_row["trade_vwap_dislocation_bps"],
                ((301.0 / 3.0) / 100.0 - 1.0) * 10000.0,
            )
            self.assertEqual(trade_row["btc_trade_count"], 2)
            self.assertEqual(trade_row["eth_mid"], 500.0)
            self.assertEqual(tuple(trade_row), micro.OUTPUT_FIELDS)

    def test_cross_asset_alignment_drops_target_second_missing_context(self):
        aggregator = micro.CrossAssetMicrostructureAggregator()
        aggregator.process(snapshot("SOLUSDT"))
        aggregator.process(trades("SOLUSDT"))
        aggregator.process(snapshot("BTCUSDT", mid=1000.0))
        aggregator.process(trades("BTCUSDT", mid=1000.0))
        self.assertEqual(aggregator.rows(), [])

    def test_late_trade_cannot_copy_a_future_book_into_its_exchange_second(self):
        aggregator = micro.MicrostructureAggregator(symbol="SOLUSDT")
        aggregator.process(snapshot("SOLUSDT", timestamp=999, mid=100.0))
        aggregator.process(snapshot("SOLUSDT", timestamp=2999, mid=200.0))
        aggregator.process(trades("SOLUSDT", timestamp=1500, mid=100.0))

        rows = {int(row["timestamp"]): row for row in aggregator.rows()}

        self.assertEqual(rows[1000]["mid"], 100.0)
        self.assertEqual(rows[2000]["mid"], 200.0)


if __name__ == "__main__":
    unittest.main()
