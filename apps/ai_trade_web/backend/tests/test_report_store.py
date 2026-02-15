#!/usr/bin/env python3

import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
import sys

if str(APP_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(APP_ROOT.parent))

from app.services.report_store import ReportStore, StoreError  # noqa: E402


class ReportStoreTest(unittest.TestCase):
    def _seed(self, base: pathlib.Path) -> ReportStore:
        reports = base / "reports"
        models = base / "models"
        config = base / "config"
        (reports / "summary").mkdir(parents=True, exist_ok=True)
        models.mkdir(parents=True, exist_ok=True)
        config.mkdir(parents=True, exist_ok=True)

        (reports / "latest_runtime_assess.json").write_text(
            json.dumps({"stage": "S3", "verdict": "PASS", "metrics": {}}),
            encoding="utf-8",
        )
        (reports / "latest_closed_loop_report.json").write_text(
            json.dumps({"overall_status": "PASS", "sections": {}}),
            encoding="utf-8",
        )
        (reports / "latest_run_meta.json").write_text(
            json.dumps({"run_id": "20260215T010000Z"}), encoding="utf-8"
        )
        (reports / "summary" / "daily_latest.json").write_text(
            json.dumps({"summary_type": "daily", "overall_status": "PASS"}),
            encoding="utf-8",
        )
        (reports / "summary" / "weekly_latest.json").write_text(
            json.dumps({"summary_type": "weekly", "overall_status": "PASS"}),
            encoding="utf-8",
        )

        run_dir = reports / "20260215T010000Z"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "runtime_assess.json").write_text(
            json.dumps({"stage": "S3", "verdict": "PASS", "metrics": {}}),
            encoding="utf-8",
        )
        (run_dir / "closed_loop_report.json").write_text(
            json.dumps({"overall_status": "PASS", "generated_at_utc": "2026-02-15T01:00:00Z"}),
            encoding="utf-8",
        )

        (models / "integrator_active.json").write_text(
            json.dumps({"model_version": "integrator_cb_v1_test"}),
            encoding="utf-8",
        )
        (config / "bybit.demo.evolution.yaml").write_text(
            "system:\n  id: demo\n", encoding="utf-8"
        )

        return ReportStore(reports_root=reports, models_root=models, config_root=config)

    def test_latest_bundle_ok(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._seed(pathlib.Path(td))
            bundle = store.latest_bundle()
            self.assertIn("runtime_assess", bundle)
            self.assertIn("closed_loop_report", bundle)
            self.assertIn("run_meta", bundle)

    def test_list_runs_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._seed(pathlib.Path(td))
            runs = store.list_runs(limit=10)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["run_id"], "20260215T010000Z")

    def test_run_bundle_invalid_id(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._seed(pathlib.Path(td))
            with self.assertRaises(StoreError):
                store.run_bundle("../bad")

    def test_model_and_config_read(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._seed(pathlib.Path(td))
            model = store.active_model()
            self.assertEqual(model["model_version"], "integrator_cb_v1_test")
            profiles = store.list_config_profiles()
            self.assertIn("bybit.demo.evolution.yaml", profiles)
            profile = store.read_config_profile("bybit.demo.evolution.yaml")
            self.assertIn("system:", profile["content"])


if __name__ == "__main__":
    unittest.main()
