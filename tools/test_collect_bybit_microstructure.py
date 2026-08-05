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
        trade = {
            "topic": "publicTrade.SOLUSDT",
            "type": "snapshot",
            "ts": 1200,
            "data": [
                {"T": 1200, "s": "SOLUSDT", "S": "Buy", "v": "2", "p": "100", "i": "a"},
                {"T": 1300, "s": "SOLUSDT", "S": "Sell", "v": "1", "p": "101", "i": "b"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            raw = pathlib.Path(temp_dir) / "raw.jsonl.gz"
            with gzip.open(raw, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(SNAPSHOT) + "\n")
                handle.write(json.dumps(trade) + "\n")
            first, count = micro.replay_jsonl(raw, symbol="SOLUSDT", bucket_ms=1000)
            second, _ = micro.replay_jsonl(raw, symbol="SOLUSDT", bucket_ms=1000)
            self.assertEqual(first, second)
            self.assertEqual(count, 2)
            trade_row = next(row for row in first if row["timestamp"] == 1000)
            self.assertEqual(trade_row["trade_count"], 2)
            self.assertAlmostEqual(trade_row["trade_imbalance"], 99.0 / 301.0)


if __name__ == "__main__":
    unittest.main()
