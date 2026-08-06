#!/usr/bin/env python3

import argparse
import gzip
import hashlib
import json
import pathlib
import tempfile
import time
import unittest

import assess_microstructure_capture as assessment
import run_microstructure_collector as supervisor


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MicrostructureRuntimeTest(unittest.TestCase):
    def write_segment(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        raw = root / "raw" / "SOLUSDT" / "segment.jsonl.gz"
        features = root / "features" / "SOLUSDT" / "segment.csv"
        report = root / "reports" / "SOLUSDT" / "segment.json"
        for path in (raw, features, report):
            path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(raw, "wt", encoding="utf-8") as handle:
            handle.write('{"topic":"orderbook.50.SOLUSDT"}\n')
        features.write_text("timestamp,spread_bps\n1000,1\n2000,1\n", encoding="utf-8")
        report.write_text(
            json.dumps(
                {
                    "schema_version": "bybit_microstructure_v1",
                    "status": "PASS",
                    "research_domain": "forward_development_only",
                    "promotion_evidence": False,
                    "promotion_eligible": False,
                    "raw": {
                        "path": "/app/data/research/microstructure/raw/SOLUSDT/segment.jsonl.gz",
                        "sha256": sha256(raw),
                        "message_count": 10,
                    },
                    "features": {
                        "path": "/app/data/research/microstructure/features/SOLUSDT/segment.csv",
                        "sha256": sha256(features),
                        "row_count": 2,
                        "first_timestamp": 1000,
                        "last_timestamp": 2000,
                    },
                    "quality": {"book_update_count": 8, "trade_count": 3},
                }
            ),
            encoding="utf-8",
        )
        return raw, features, report

    def test_assessment_passes_only_complete_fresh_capture(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            _, features, _ = self.write_segment(root)
            args = argparse.Namespace(
                root=str(root),
                output=str(root / "assessment.json"),
                symbol="SOLUSDT",
                min_capture_duration_sec=2,
                max_stale_sec=10,
                min_row_density=0.8,
                now_epoch_ms=2500,
            )
            payload = assessment.assess(args)
            self.assertEqual(payload["status"], "PASS")
            self.assertTrue(payload["development_screen_ready"])
            self.assertFalse(payload["promotion_eligible"])

            features.write_text("tampered\n", encoding="utf-8")
            failed = assessment.assess(args)
            self.assertEqual(failed["status"], "FAIL")
            self.assertIn("invalid_segment_contract", failed["failures"])

    def test_supervisor_healthcheck_is_freshness_bound(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            health = root / "collector_health.json"
            supervisor.atomic_write_json(
                health,
                {
                    "schema_version": supervisor.SCHEMA_VERSION,
                    "state": "healthy",
                    "last_success_epoch_ms": int(time.time() * 1000),
                },
            )
            args = argparse.Namespace(root=str(root), max_stale_sec=10)
            self.assertEqual(supervisor.healthcheck(args), 0)
            payload = json.loads(health.read_text(encoding="utf-8"))
            payload["last_success_epoch_ms"] = 1
            supervisor.atomic_write_json(health, payload)
            self.assertEqual(supervisor.healthcheck(args), 1)

    def test_assessment_distinguishes_active_first_segment_from_broken_collector(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            supervisor.atomic_write_json(
                root / "collector_health.json",
                {
                    "schema_version": supervisor.SCHEMA_VERSION,
                    "state": "capturing",
                    "symbol": "SOLUSDT",
                    "segment_started_epoch_ms": 1000,
                    "consecutive_failures": 0,
                },
            )
            args = argparse.Namespace(
                root=str(root),
                output=str(root / "assessment.json"),
                symbol="SOLUSDT",
                min_capture_duration_sec=2,
                max_stale_sec=10,
                min_row_density=0.8,
                now_epoch_ms=2500,
            )

            payload = assessment.assess(args)

            self.assertEqual(payload["status"], "FAIL")
            self.assertTrue(payload["capture_in_progress"])
            self.assertEqual(payload["collector_health"]["status"], "PASS")
            self.assertEqual(payload["failures"], ["minimum_forward_capture_duration"])


if __name__ == "__main__":
    unittest.main()
