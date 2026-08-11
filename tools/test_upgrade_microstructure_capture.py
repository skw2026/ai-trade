#!/usr/bin/env python3

import gzip
import hashlib
import json
import pathlib
import tempfile
import unittest

import collect_bybit_microstructure as collector
import upgrade_microstructure_capture as upgrade_tool


def snapshot(symbol: str, mid: float, timestamp: int = 999) -> dict:
    return {
        "topic": f"orderbook.50.{symbol}",
        "type": "snapshot",
        "cts": timestamp,
        "data": {
            "s": symbol,
            "b": [[str(mid - 1.0), "2"]],
            "a": [[str(mid + 1.0), "1"]],
            "u": 1,
            "seq": 1,
        },
    }


def trade(symbol: str, mid: float, timestamp: int = 1200) -> dict:
    return {
        "topic": f"publicTrade.{symbol}",
        "data": [
            {
                "T": timestamp,
                "S": "Buy",
                "v": "1",
                "p": str(mid),
                "i": f"{symbol}-{timestamp}-trade",
            }
        ],
    }


class UpgradeMicrostructureCaptureTest(unittest.TestCase):
    def test_v2_raw_is_replayed_without_mutation_and_then_reused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            raw = root / "raw" / "SOLUSDT" / "segment.jsonl.gz"
            feature = root / "features" / "SOLUSDT" / "segment.csv"
            report = root / "reports" / "SOLUSDT" / "segment.json"
            for path in (raw, feature, report):
                path.parent.mkdir(parents=True, exist_ok=True)
            messages = []
            for symbol, mid in (
                ("SOLUSDT", 100.0),
                ("BTCUSDT", 1000.0),
                ("ETHUSDT", 500.0),
            ):
                messages.extend((snapshot(symbol, mid), trade(symbol, mid)))
                messages.extend(
                    (
                        snapshot(symbol, mid, timestamp=2999),
                        trade(symbol, mid, timestamp=2200),
                    )
                )
            with gzip.open(raw, "wt", encoding="utf-8") as handle:
                for message in messages:
                    handle.write(json.dumps(message) + "\n")
            feature.write_text("legacy\n", encoding="utf-8")
            raw_sha256 = hashlib.sha256(raw.read_bytes()).hexdigest()
            report.write_text(
                json.dumps(
                    {
                        "schema_version": upgrade_tool.SOURCE_SCHEMA_VERSION,
                        "status": "PASS",
                        "research_domain": "forward_development_only",
                        "promotion_evidence": False,
                        "promotion_eligible": False,
                        "symbols": list(collector.CAPTURE_SYMBOLS),
                        "cross_asset_alignment_contract": (
                            collector.CROSS_ASSET_ALIGNMENT_CONTRACT
                        ),
                        "raw": {
                            "path": str(raw),
                            "sha256": raw_sha256,
                            "message_count": len(messages),
                        },
                        "features": {
                            "path": str(feature),
                            "sha256": hashlib.sha256(feature.read_bytes()).hexdigest(),
                            "row_count": 1,
                            "first_timestamp": 0,
                            "last_timestamp": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            first = upgrade_tool.upgrade(root, symbol="SOLUSDT")

            self.assertEqual(first["status"], "PASS")
            self.assertEqual(first["rebuilt_segment_count"], 1)
            self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), raw_sha256)
            upgraded_report = pathlib.Path(first["outputs"][0]["upgraded_report"])
            payload = json.loads(upgraded_report.read_text(encoding="utf-8"))
            upgraded_feature = pathlib.Path(payload["features"]["path"])
            self.assertEqual(payload["schema_version"], collector.SCHEMA_VERSION)
            self.assertFalse(
                payload["deterministic_raw_replay_upgrade"]["raw_payload_mutated"]
            )
            self.assertEqual(
                tuple(upgraded_feature.read_text(encoding="utf-8").splitlines()[0].split(",")),
                collector.OUTPUT_FIELDS,
            )

            second = upgrade_tool.upgrade(root, symbol="SOLUSDT")

            self.assertEqual(second["status"], "PASS")
            self.assertEqual(second["rebuilt_segment_count"], 0)
            self.assertEqual(second["reused_segment_count"], 1)


if __name__ == "__main__":
    unittest.main()
