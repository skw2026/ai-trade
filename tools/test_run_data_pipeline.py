#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("run_data_pipeline.py")
    spec = importlib.util.spec_from_file_location("run_data_pipeline", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PIPELINE = load_module()


class RunDataPipelineTest(unittest.TestCase):
    def test_load_yaml_minimal(self):
        with tempfile.TemporaryDirectory() as td:
            config = pathlib.Path(td) / "data_pipeline.yaml"
            config.write_text(
                "common:\n"
                "  symbol: BTCUSDT\n"
                "archive:\n"
                "  enabled: false\n",
                encoding="utf-8",
            )
            payload = PIPELINE.load_yaml(config)
            self.assertEqual(payload["common"]["symbol"], "BTCUSDT")
            self.assertEqual(payload["archive"]["enabled"], False)

    def test_main_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = root / "data_pipeline.yaml"
            run_dir = root / "run"
            config.write_text(
                "common:\n"
                "  symbol: BTCUSDT\n"
                "  interval_minutes: 5\n"
                "  category: linear\n"
                "paths:\n"
                "  ohlcv_csv: data/research/ohlcv_5m.csv\n"
                "  feature_csv: data/research/feature_store_5m.csv\n"
                "  backtest_report: data/research/walkforward_report.json\n"
                "archive:\n"
                "  enabled: true\n"
                "incremental:\n"
                "  enabled: true\n"
                "gap_fill:\n"
                "  enabled: true\n"
                "feature_store:\n"
                "  enabled: true\n"
                "walkforward:\n"
                "  enabled: true\n",
                encoding="utf-8",
            )

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "run_data_pipeline.py",
                    "--config",
                    str(config),
                    "--run-dir",
                    str(run_dir),
                    "--dry-run",
                ]
                code = PIPELINE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            report_path = run_dir / "data_pipeline_report.json"
            self.assertTrue(report_path.exists())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "PLANNED")
            self.assertEqual(len(report["steps"]), 5)
            planned_count = sum(1 for item in report["steps"] if item["status"] == "planned")
            self.assertEqual(planned_count, 5)


if __name__ == "__main__":
    unittest.main()
