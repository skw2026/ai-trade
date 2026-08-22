#!/usr/bin/env python3

import gzip
import json
import pathlib
import tempfile
import unittest

import collect_bybit_liquidations as collector


class BybitLiquidationCollectorTest(unittest.TestCase):
    def test_replay_buckets_long_and_short_liquidations_by_exchange_time(self):
        messages = [
            {
                "topic": collector.TOPIC,
                "type": "snapshot",
                "ts": 2100,
                "data": [
                    {"T": 1100, "s": collector.SYMBOL, "S": "Buy", "v": "2", "p": "100"},
                    {"T": 1900, "s": collector.SYMBOL, "S": "Sell", "v": "3", "p": "101"},
                    {"T": 2200, "s": collector.SYMBOL, "S": "Buy", "v": "1", "p": "99"},
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            raw = pathlib.Path(temp) / "raw.jsonl.gz"
            with gzip.open(raw, "wt", encoding="utf-8") as handle:
                for message in messages:
                    handle.write(json.dumps(message) + "\n")
            rows, count = collector.replay_jsonl(raw)

        self.assertEqual(count, 1)
        self.assertEqual([row["timestamp"] for row in rows], [1000, 2000])
        # Bybit documents Buy as a long-position liquidation update.
        self.assertEqual(rows[0]["long_liquidation_count"], 1)
        self.assertEqual(rows[0]["short_liquidation_count"], 1)
        self.assertEqual(rows[0]["long_liquidation_notional"], 200.0)
        self.assertEqual(rows[0]["short_liquidation_notional"], 303.0)
        self.assertEqual(rows[1]["long_liquidation_count"], 1)

    def test_empty_connected_segment_is_valid_sparse_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "bybit_sol_liquidations"
            raw = root / "raw" / collector.SYMBOL / "segment.jsonl.gz"
            features = root / "features" / collector.SYMBOL / "segment.csv"
            raw.parent.mkdir(parents=True)
            features.parent.mkdir(parents=True)
            with gzip.open(raw, "wt", encoding="utf-8"):
                pass
            rows, count = collector.replay_jsonl(raw)
            collector.write_feature_csv(features, rows)
            report = collector.build_capture_report(
                capture_root=root,
                raw_path=raw,
                feature_path=features,
                rows=rows,
                raw_count=count,
                capture_started_epoch_ms=1_000,
                capture_completed_epoch_ms=61_000,
                public_url=collector.PUBLIC_URL,
            )

        self.assertEqual(rows, [])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["coverage"]["duration_ms"], 60_000)
        self.assertEqual(report["quality"]["liquidation_event_count"], 0)
        self.assertEqual(
            report["artifact_path_contract"], collector.ARTIFACT_PATH_CONTRACT
        )
        self.assertEqual(
            report["raw"]["path"], f"raw/{collector.SYMBOL}/segment.jsonl.gz"
        )
        self.assertEqual(
            report["features"]["path"], f"features/{collector.SYMBOL}/segment.csv"
        )
        self.assertFalse(report["promotion_evidence"])
        self.assertFalse(report["live_activation_authorized"])

    def test_invalid_side_and_symbol_fail_closed(self):
        for data in (
            [{"T": 1000, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "1"}],
            [{"T": 1000, "s": collector.SYMBOL, "S": "Unknown", "v": "1", "p": "1"}],
        ):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as temp:
                raw = pathlib.Path(temp) / "raw.jsonl"
                raw.write_text(
                    json.dumps({"topic": collector.TOPIC, "data": data}) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "invalid liquidation"):
                    collector.replay_jsonl(raw)


if __name__ == "__main__":
    unittest.main()
