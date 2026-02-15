#!/usr/bin/env python3

import pathlib
import subprocess
import tempfile
import time
import unittest
import os


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "recycle_artifacts.sh"


def run_gc(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


class RecycleArtifactsTest(unittest.TestCase):
    def test_max_age_hours_removes_old_runs_and_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports_root = pathlib.Path(tmp) / "closed_loop"
            summary = reports_root / "summary"
            summary.mkdir(parents=True, exist_ok=True)

            old_run = reports_root / "20260201T010101Z"
            new_run = reports_root / "20260215T010101Z"
            old_run.mkdir(parents=True, exist_ok=True)
            new_run.mkdir(parents=True, exist_ok=True)
            (reports_root / "latest_run_id").write_text("20260215T010101Z\n", encoding="utf-8")

            old_daily = summary / "daily_20260201.json"
            new_daily = summary / "daily_20260215.json"
            old_daily.write_text("{}", encoding="utf-8")
            new_daily.write_text("{}", encoding="utf-8")
            (summary / "daily_latest.json").write_text("{}", encoding="utf-8")

            now = time.time()
            old_ts = now - 5 * 24 * 3600
            os.utime(old_run, (old_ts, old_ts))
            os.utime(old_daily, (old_ts, old_ts))

            result = run_gc(
                "--reports-root",
                str(reports_root),
                "--keep-run-dirs",
                "100",
                "--keep-daily-files",
                "100",
                "--keep-weekly-files",
                "100",
                "--max-age-hours",
                "72",
            )
            self.assertEqual(result.returncode, 0, msg=f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}")

            self.assertFalse(old_run.exists())
            self.assertTrue(new_run.is_dir())
            self.assertFalse(old_daily.exists())
            self.assertTrue(new_daily.is_file())
            self.assertTrue((summary / "daily_latest.json").is_file())

    def test_run_dir_retention_respects_latest_run_id_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports_root = pathlib.Path(tmp) / "closed_loop"
            reports_root.mkdir(parents=True, exist_ok=True)

            run_ids = [
                "20260210T010101Z",
                "20260211T010101Z",
                "20260212T010101Z",
                "20260213T010101Z",
                "20260214T010101Z",
            ]
            for run_id in run_ids:
                run_dir = reports_root / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "closed_loop_report.json").write_text("{}", encoding="utf-8")

            protected_id = "20260211T010101Z"
            (reports_root / "latest_run_id").write_text(protected_id + "\n", encoding="utf-8")

            result = run_gc(
                "--reports-root",
                str(reports_root),
                "--keep-run-dirs",
                "2",
                "--keep-daily-files",
                "0",
                "--keep-weekly-files",
                "0",
            )
            self.assertEqual(result.returncode, 0, msg=f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}")

            self.assertTrue((reports_root / "20260214T010101Z").is_dir())
            self.assertTrue((reports_root / "20260213T010101Z").is_dir())
            self.assertTrue((reports_root / protected_id).is_dir())
            self.assertFalse((reports_root / "20260212T010101Z").exists())
            self.assertFalse((reports_root / "20260210T010101Z").exists())

    def test_summary_cleanup_keeps_latest_alias_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports_root = pathlib.Path(tmp) / "closed_loop"
            summary = reports_root / "summary"
            summary.mkdir(parents=True, exist_ok=True)

            for day in ("20260210", "20260211", "20260212", "20260213"):
                (summary / f"daily_{day}.json").write_text("{}", encoding="utf-8")
            for week in ("2026W06", "2026W07", "2026W08"):
                (summary / f"weekly_{week}.json").write_text("{}", encoding="utf-8")
            (summary / "daily_latest.json").write_text("{}", encoding="utf-8")
            (summary / "weekly_latest.json").write_text("{}", encoding="utf-8")

            result = run_gc(
                "--reports-root",
                str(reports_root),
                "--keep-run-dirs",
                "0",
                "--keep-daily-files",
                "2",
                "--keep-weekly-files",
                "1",
            )
            self.assertEqual(result.returncode, 0, msg=f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}")

            daily_files = sorted(p.name for p in summary.glob("daily_*.json"))
            weekly_files = sorted(p.name for p in summary.glob("weekly_*.json"))
            self.assertEqual(
                daily_files,
                ["daily_20260212.json", "daily_20260213.json", "daily_latest.json"],
            )
            self.assertEqual(
                weekly_files,
                ["weekly_2026W08.json", "weekly_latest.json"],
            )

    def test_log_rotation_keeps_log_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports_root = pathlib.Path(tmp) / "closed_loop"
            reports_root.mkdir(parents=True, exist_ok=True)
            log_file = reports_root / "cron.log"

            content = "".join(f"line-{i:03d}\n" for i in range(80))
            log_file.write_text(content, encoding="utf-8")
            expected_tail = content.encode("utf-8")[-120:]

            result = run_gc(
                "--reports-root",
                str(reports_root),
                "--keep-run-dirs",
                "0",
                "--keep-daily-files",
                "0",
                "--keep-weekly-files",
                "0",
                "--log-file",
                str(log_file),
                "--log-max-bytes",
                "240",
                "--log-keep-bytes",
                "120",
            )
            self.assertEqual(result.returncode, 0, msg=f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}")

            rotated = log_file.read_bytes()
            self.assertLessEqual(len(rotated), 120)
            self.assertEqual(rotated, expected_tail)


if __name__ == "__main__":
    unittest.main()
