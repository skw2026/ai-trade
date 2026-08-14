#!/usr/bin/env python3

import gzip
import json
import pathlib
import tempfile
import unittest

import collect_binance_microstructure as capture


def depth(timestamp: int, update_id: int, bid: float = 99.0, ask: float = 101.0):
    return {
        "e": "depthUpdate",
        "E": timestamp + 1,
        "T": timestamp,
        "s": "SOLUSDT",
        "U": update_id - 1,
        "u": update_id,
        "pu": update_id - 2,
        "b": [[str(bid), "2"], [str(bid - 1), "3"]],
        "a": [[str(ask), "1"], [str(ask + 1), "4"]],
        "st": 1,
    }


def trade(timestamp: int, trade_id: int, maker: bool):
    return {
        "e": "aggTrade",
        "E": timestamp + 1,
        "s": "SOLUSDT",
        "a": trade_id,
        "p": "100",
        "q": "2",
        "T": timestamp,
        "m": maker,
        "st": 1,
    }


class BinanceMicrostructureTest(unittest.TestCase):
    def test_replay_is_deterministic_and_maps_aggressor_side(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = pathlib.Path(temp) / "capture.jsonl.gz"
            messages = [
                depth(999, 10),
                trade(1200, 20, False),
                trade(1300, 21, True),
                depth(2999, 11, bid=100, ask=102),
                trade(2200, 22, False),
            ]
            with gzip.open(raw, "wt", encoding="utf-8") as handle:
                for message in messages:
                    handle.write(json.dumps(message) + "\n")
            first, count = capture.replay_jsonl(raw)
            second, _ = capture.replay_jsonl(raw)
            self.assertEqual(first, second)
            self.assertEqual(count, len(messages))
            self.assertEqual([row["timestamp"] for row in first], [0, 1000])
            self.assertEqual(first[1]["trade_count"], 2)
            self.assertAlmostEqual(first[1]["trade_imbalance"], 0.0)
            self.assertEqual(tuple(first[1]), capture.OUTPUT_FIELDS)

    def test_partial_snapshot_replacement_removes_old_levels_and_measures_flow(self):
        aggregator = capture.BinanceMicrostructureAggregator()
        aggregator.process(depth(999, 10))
        aggregator.process(depth(1999, 11, bid=100, ask=102))
        book = aggregator.engine.book
        self.assertNotIn(98.0, book.bids)
        self.assertEqual(book.metrics()["best_bid"], 100.0)
        self.assertGreater(book.last_flow_metrics["book_flow_abs_quote"], 0.0)

    def test_rejects_crossed_or_regressing_depth(self):
        aggregator = capture.BinanceMicrostructureAggregator()
        aggregator.process(depth(999, 10))
        with self.assertRaisesRegex(ValueError, "update id"):
            aggregator.process(depth(1099, 10))
        with self.assertRaisesRegex(ValueError, "crossed"):
            capture.BinanceMicrostructureAggregator().process(
                depth(999, 1, bid=101, ask=100)
            )

    def test_filters_post_migration_coin_m_payloads(self):
        message = depth(999, 10)
        message["st"] = 2
        aggregator = capture.BinanceMicrostructureAggregator()
        self.assertFalse(aggregator.process(message))
        self.assertEqual(aggregator.rows(), [])


if __name__ == "__main__":
    unittest.main()
