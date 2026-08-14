#!/usr/bin/env python3

import argparse
import gzip
import json
import pathlib
import tempfile
import unittest

import assess_binance_microstructure_capture as assessment
import collect_binance_microstructure as collector
import run_binance_microstructure_collector as supervisor
from test_collect_binance_microstructure import depth, trade


class BinanceMicrostructureRuntimeTest(unittest.TestCase):
    def _capture(self, root: pathlib.Path):
        report_dir = root / "reports" / collector.SYMBOL
        raw_dir = root / "raw" / collector.SYMBOL
        feature_dir = root / "features" / collector.SYMBOL
        report_dir.mkdir(parents=True)
        raw_dir.mkdir(parents=True)
        feature_dir.mkdir(parents=True)
        raw = raw_dir / "segment.jsonl.gz"
        features = feature_dir / "segment.csv"
        report = report_dir / "segment.json"
        with gzip.open(raw, "wt", encoding="utf-8") as handle:
            for message in (
                depth(999, 10),
                trade(1200, 20, False),
                depth(2999, 11),
                trade(2200, 21, True),
                depth(3999, 12),
                trade(3200, 22, False),
            ):
                handle.write(json.dumps(message) + "\n")
        rows, count = collector.replay_jsonl(raw)
        collector.write_feature_csv(features, rows)
        payload = collector.build_capture_report(
            raw_path=raw,
            feature_path=features,
            rows=rows,
            raw_count=count,
            public_url=collector.PUBLIC_URL,
            market_url=collector.MARKET_URL,
        )
        report.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        health = {
            "schema_version": supervisor.SCHEMA_VERSION,
            "state": "healthy",
            "symbol": collector.SYMBOL,
            "capture_schema_version": collector.SCHEMA_VERSION,
            "last_success_epoch_ms": 2100,
        }
        (root / "collector_health.json").write_text(
            json.dumps(health) + "\n", encoding="utf-8"
        )
        return raw, features, payload

    def test_assessment_passes_checksum_bound_ready_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            _, _, payload = self._capture(root)
            args = argparse.Namespace(
                root=str(root),
                min_capture_duration_sec=2,
                max_stale_sec=10,
                min_row_density=0.8,
                now_epoch_ms=2500,
            )
            result = assessment.assess(args)
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["fully_verifiable"])
            self.assertFalse(result["promotion_eligible"])
            self.assertEqual(result["coverage_ms"], 3000)
            self.assertEqual(result["segments"][0]["raw_sha256"], payload["raw"]["sha256"])

    def test_assessment_rejects_mutated_feature_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            _, features, _ = self._capture(root)
            features.write_text("mutated\n", encoding="utf-8")
            result = assessment.assess(
                argparse.Namespace(
                    root=str(root),
                    min_capture_duration_sec=2,
                    max_stale_sec=10,
                    min_row_density=0.8,
                    now_epoch_ms=2500,
                )
            )
            self.assertEqual(result["status"], "NOT_READY")
            self.assertIn("invalid_segment_contract", result["failures"])
            self.assertFalse(result["promotion_eligible"])

    def test_supervisor_uses_short_bootstrap_and_public_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            command, report = supervisor.segment_command(
                root=pathlib.Path(temp),
                duration_sec=65.0,
                public_url=collector.PUBLIC_URL,
                market_url=collector.MARKET_URL,
            )
            self.assertIn("collect_binance_microstructure.py", " ".join(command))
            self.assertIn(collector.PUBLIC_URL, command)
            self.assertIn(collector.MARKET_URL, command)
            self.assertTrue(str(report).endswith(".json"))


if __name__ == "__main__":
    unittest.main()
