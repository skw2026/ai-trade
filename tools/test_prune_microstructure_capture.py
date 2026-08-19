#!/usr/bin/env python3

import json
import os
import pathlib
import tempfile
import unittest

import prune_microstructure_capture as retention


class CaptureRetentionTest(unittest.TestCase):
    def write_segment(
        self, root: pathlib.Path, segment_id: str, *, mtime: float, complete: bool = True
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        raw = root / "raw" / "SOLUSDT" / f"{segment_id}.jsonl.gz"
        features = root / "features" / "SOLUSDT" / f"{segment_id}.csv"
        report = root / "reports" / "SOLUSDT" / f"{segment_id}.json"
        for path in (raw, features, report):
            path.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"raw")
        features.write_bytes(b"features")
        if complete:
            report.write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "raw": {"path": f"/app/data/raw/{raw.name}"},
                        "features": {"path": f"/app/data/features/{features.name}"},
                    }
                ),
                encoding="utf-8",
            )
        else:
            report.write_text("{}", encoding="utf-8")
        for path in (raw, features, report):
            os.utime(path, (mtime, mtime))
        return raw, features, report

    def write_upgraded_segment(
        self, root: pathlib.Path, segment_id: str, *, mtime: float
    ) -> tuple[pathlib.Path, ...]:
        raw, source_feature, source_report = self.write_segment(
            root, segment_id, mtime=mtime
        )
        upgraded_id = f"{segment_id}.bybit_cross_asset_microstructure_v3"
        upgraded_feature = (
            root / "features" / "SOLUSDT" / f"{upgraded_id}.csv"
        )
        upgraded_report = root / "reports" / "SOLUSDT" / f"{upgraded_id}.json"
        upgraded_feature.write_bytes(b"upgraded-features")
        upgraded_report.write_text(
            json.dumps(
                {
                    "schema_version": "bybit_cross_asset_microstructure_v3",
                    "status": "PASS",
                    "raw": {
                        "path": f"/app/data/raw/{raw.name}",
                        "sha256": "1" * 64,
                    },
                    "features": {
                        "path": f"/app/data/features/{upgraded_feature.name}",
                        "sha256": "2" * 64,
                    },
                    "deterministic_raw_replay_upgrade": {
                        "source_schema_version": "bybit_cross_asset_microstructure_v2",
                        "target_schema_version": "bybit_cross_asset_microstructure_v3",
                        "raw_payload_mutated": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        for path in (upgraded_feature, upgraded_report):
            os.utime(path, (mtime, mtime))
        return raw, source_feature, source_report, upgraded_feature, upgraded_report

    def test_removes_only_complete_expired_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "microstructure"
            old = self.write_segment(root, "old", mtime=100)
            fresh = self.write_segment(root, "fresh", mtime=900)
            invalid = self.write_segment(root, "invalid", mtime=100, complete=False)

            report = retention.prune_capture_root(
                root,
                retention_seconds=500,
                now_epoch=1000,
                expected_root_name="microstructure",
            )

            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["segments_removed"], 1)
            self.assertEqual(report["segments_preserved"], 1)
            self.assertGreater(report["bytes_removed"], 0)
            self.assertTrue(all(not path.exists() for path in old))
            self.assertTrue(all(path.exists() for path in fresh))
            self.assertTrue(all(path.exists() for path in invalid))
            self.assertEqual(len(report["segments_skipped"]), 1)

    def test_rejects_root_name_drift_without_deleting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "wrong"
            segment = self.write_segment(root, "old", mtime=100)
            with self.assertRaisesRegex(ValueError, "root name mismatch"):
                retention.prune_capture_root(
                    root,
                    retention_seconds=500,
                    now_epoch=1000,
                    expected_root_name="microstructure",
                )
            self.assertTrue(all(path.exists() for path in segment))

    def test_removes_expired_source_and_deterministic_upgrade_bundles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "microstructure"
            artifacts = self.write_upgraded_segment(root, "old", mtime=100)

            report = retention.prune_capture_root(
                root,
                retention_seconds=500,
                now_epoch=1000,
                expected_root_name="microstructure",
            )

            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["segments_removed"], 2)
            self.assertEqual(report["segments_skipped"], [])
            self.assertTrue(all(not path.exists() for path in artifacts))

    def test_removes_expired_orphaned_deterministic_upgrade_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "microstructure"
            artifacts = self.write_upgraded_segment(root, "old", mtime=100)
            for path in artifacts[:3]:
                path.unlink()

            report = retention.prune_capture_root(
                root,
                retention_seconds=500,
                now_epoch=1000,
                expected_root_name="microstructure",
            )

            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["segments_removed"], 1)
            self.assertEqual(report["segments_skipped"], [])
            self.assertTrue(all(not path.exists() for path in artifacts))

    def test_preserves_upgrade_when_source_identity_does_not_bind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "microstructure"
            artifacts = self.write_upgraded_segment(root, "old", mtime=100)
            for path in artifacts[:3]:
                path.unlink()
            upgraded_report = artifacts[-1]
            payload = json.loads(upgraded_report.read_text(encoding="utf-8"))
            payload["raw"]["path"] = "/app/data/raw/different.jsonl.gz"
            upgraded_report.write_text(json.dumps(payload), encoding="utf-8")
            os.utime(upgraded_report, (100, 100))

            report = retention.prune_capture_root(
                root,
                retention_seconds=500,
                now_epoch=1000,
                expected_root_name="microstructure",
            )

            self.assertEqual(report["segments_removed"], 0)
            self.assertEqual(len(report["segments_skipped"]), 1)
            self.assertIn(
                "upgrade raw filename does not bind source segment",
                report["segments_skipped"][0]["reason"],
            )
            self.assertTrue(artifacts[-2].exists())
            self.assertTrue(upgraded_report.exists())

    def test_missing_root_is_non_destructive_noop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "binance_sol_microstructure"
            report = retention.prune_capture_root(
                root,
                retention_seconds=96 * 3600,
                now_epoch=1000,
                expected_root_name="binance_sol_microstructure",
            )
            self.assertEqual(report["status"], "NOT_PRESENT")
            self.assertEqual(report["segments_removed"], 0)


if __name__ == "__main__":
    unittest.main()
