#!/usr/bin/env python3

import argparse
import csv
import gzip
import json
import pathlib
import tempfile
import unittest

import assess_liquidation_capture as assessor
import collect_bybit_liquidations as collector
import run_liquidation_collector as supervisor


class LiquidationCaptureRuntimeTest(unittest.TestCase):
    def _root(self, temp: str, *, start: int, end: int) -> pathlib.Path:
        root = pathlib.Path(temp)
        raw = root / "raw" / collector.SYMBOL / "s.jsonl.gz"
        features = root / "features" / collector.SYMBOL / "s.csv"
        report_path = root / "reports" / collector.SYMBOL / "s.json"
        raw.parent.mkdir(parents=True)
        features.parent.mkdir(parents=True)
        report_path.parent.mkdir(parents=True)
        with gzip.open(raw, "wt", encoding="utf-8"):
            pass
        collector.write_feature_csv(features, [])
        report = collector.build_capture_report(
            raw_path=raw,
            feature_path=features,
            rows=[],
            raw_count=0,
            capture_started_epoch_ms=start,
            capture_completed_epoch_ms=end,
            public_url=collector.PUBLIC_URL,
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")
        (root / "collector_health.json").write_text(
            json.dumps(
                {
                    "schema_version": supervisor.SCHEMA_VERSION,
                    "state": "healthy",
                    "symbol": collector.SYMBOL,
                    "capture_schema_version": collector.SCHEMA_VERSION,
                    "last_success_epoch_ms": end,
                    "consecutive_failures": 0,
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_sparse_zero_event_capture_passes_coverage_and_health(self):
        with tempfile.TemporaryDirectory() as temp:
            start = 1_000_000
            end = start + 86_400_000
            root = self._root(temp, start=start, end=end)
            report = assessor.assess(
                argparse.Namespace(
                    root=str(root),
                    min_capture_duration_sec=86_400,
                    max_stale_sec=1_800,
                    now_epoch_ms=end + 1_000,
                )
            )

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["fully_verifiable"])
        self.assertEqual(report["liquidation_event_count"], 0)
        self.assertEqual(report["coverage_ms"], 86_400_000)

    def test_tampered_feature_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            start = 1_000_000
            end = start + 86_400_000
            root = self._root(temp, start=start, end=end)
            feature = root / "features" / collector.SYMBOL / "s.csv"
            with feature.open("a", encoding="utf-8") as handle:
                handle.write("tampered\n")
            report = assessor.assess(
                argparse.Namespace(
                    root=str(root),
                    min_capture_duration_sec=86_400,
                    max_stale_sec=1_800,
                    now_epoch_ms=end + 1_000,
                )
            )

        self.assertEqual(report["status"], "NOT_READY")
        self.assertIn("invalid_segment_contract", report["failures"])

    def test_self_consistent_feature_rewrite_is_rejected_by_raw_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            start = 1_000_000
            end = start + 86_400_000
            root = self._root(temp, start=start, end=end)
            feature = root / "features" / collector.SYMBOL / "s.csv"
            collector.write_feature_csv(
                feature,
                [
                    {
                        "timestamp": 2_000_000,
                        "long_liquidation_count": 1,
                        "long_liquidation_qty": 1.0,
                        "long_liquidation_notional": 100.0,
                        "short_liquidation_count": 0,
                        "short_liquidation_qty": 0.0,
                        "short_liquidation_notional": 0.0,
                    }
                ],
            )
            report_path = root / "reports" / collector.SYMBOL / "s.json"
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload["features"].update(
                {
                    "sha256": assessor.sha256_file(feature),
                    "row_count": 1,
                    "first_timestamp": 2_000_000,
                    "last_timestamp": 2_000_000,
                }
            )
            payload["quality"].update(
                {"liquidation_event_count": 1, "event_bucket_count": 1}
            )
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            report = assessor.assess(
                argparse.Namespace(
                    root=str(root),
                    min_capture_duration_sec=86_400,
                    max_stale_sec=1_800,
                    now_epoch_ms=end + 1_000,
                )
            )

        self.assertEqual(report["status"], "NOT_READY")
        self.assertTrue(
            any("raw replay row count mismatch" in item for item in report["invalid_segments"])
        )

    def test_connection_gaps_are_not_reported_as_zero_event_coverage(self):
        self.assertEqual(
            assessor.merge_intervals([(1_000, 2_000), (2_001, 3_000)]),
            [(1_000, 2_000), (2_001, 3_000)],
        )


if __name__ == "__main__":
    unittest.main()
