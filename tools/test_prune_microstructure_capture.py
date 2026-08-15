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
