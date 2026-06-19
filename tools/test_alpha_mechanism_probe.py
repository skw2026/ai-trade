#!/usr/bin/env python3

import csv
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("alpha_mechanism_probe.py")
    spec = importlib.util.spec_from_file_location("alpha_mechanism_probe", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE = load_module()


FIELDS = [
    "timestamp",
    "close",
    "volume",
    "ret_1",
    "ret_3",
    "ret_12",
    "ema_fast",
    "ema_slow",
    "ema_diff",
    "vol_12",
    "vol_48",
    "zscore_48",
    "mom_12",
    "mom_48",
    "range_pct",
    "vol_chg_12",
    "forward_return",
]


def write_probe_fixture(path: pathlib.Path, *, aligned: bool) -> pathlib.Path:
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=FIELDS)
        writer.writeheader()
        for index in range(600):
            sign = 1 if index % 4 in {0, 1} else -1
            fwd = sign * 0.003
            feature_sign = sign if aligned else (1 if index % 2 == 0 else -1)
            writer.writerow(
                {
                    "timestamp": 1_700_000_000_000 + index * 300_000,
                    "close": 100.0,
                    "volume": 10.0,
                    "ret_1": feature_sign * 0.001,
                    "ret_3": feature_sign * 0.0015,
                    "ret_12": feature_sign * 0.002,
                    "ema_fast": 100.2,
                    "ema_slow": 100.0,
                    "ema_diff": feature_sign * 0.002,
                    "vol_12": 0.001,
                    "vol_48": 0.001,
                    "zscore_48": feature_sign * 1.5,
                    "mom_12": feature_sign * 0.003,
                    "mom_48": feature_sign * 0.006,
                    "range_pct": 0.001,
                    "vol_chg_12": 0.0,
                    "forward_return": fwd,
                }
            )
    return path


def write_path_capture_fixture(path: pathlib.Path) -> pathlib.Path:
    cycles = 240
    period = 20
    row_count = cycles * period + 20
    closes = [100.0 for _ in range(row_count)]
    entry_rows = {}
    for cycle in range(cycles):
        direction = 1 if cycle % 2 == 0 else -1
        entry = cycle * period + 4
        entry_rows[entry] = direction
        closes[entry] = 100.0
        closes[entry + 1] = 100.12 if direction > 0 else 99.88
        for offset in range(2, 13):
            closes[entry + offset] = 100.0

    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=FIELDS)
        writer.writeheader()
        for index, close in enumerate(closes):
            direction = entry_rows.get(index, 0)
            feature = float(direction) * 0.003 if direction else 0.0
            writer.writerow(
                {
                    "timestamp": 1_700_000_000_000 + index * 300_000,
                    "close": close,
                    "volume": 10.0,
                    "ret_1": feature,
                    "ret_3": feature,
                    "ret_12": feature,
                    "ema_fast": 100.2,
                    "ema_slow": 100.0,
                    "ema_diff": feature,
                    "vol_12": 0.001,
                    "vol_48": 0.001,
                    "zscore_48": feature * 1000.0,
                    "mom_12": feature,
                    "mom_48": feature,
                    "range_pct": 0.001,
                    "vol_chg_12": 0.0,
                    "forward_return": 0.0,
                }
            )
    return path


class AlphaMechanismProbeTest(unittest.TestCase):
    def test_controls_pass_and_real_candidate_can_pass_on_aligned_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            feature_path = write_probe_fixture(root / "features.csv", aligned=True)
            output = root / "probe.json"

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "alpha_mechanism_probe.py",
                    "--output",
                    str(output),
                    "--symbol",
                    "SOLUSDT",
                    "--feature_csv",
                    str(feature_path),
                    "--round-trip-cost-bps",
                    "3.5",
                    "--min-holdout-samples",
                    "100",
                ]
                code = PROBE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["mechanism_control_status"], "pass")
            self.assertEqual(payload["market_alpha_family_status"], "pass")
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["controls"]["negative_random"]["status"], "pass")
            self.assertEqual(payload["deployable_candidate_manifest"]["status"], "pass")
            self.assertIsNotNone(
                payload["deployable_candidate_manifest"]["selected_candidate"]
            )

    def test_controls_pass_but_market_alpha_can_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            feature_path = write_probe_fixture(root / "features.csv", aligned=False)
            output = root / "probe.json"

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "alpha_mechanism_probe.py",
                    "--output",
                    str(output),
                    "--symbol",
                    "BTCUSDT",
                    "--feature_csv",
                    str(feature_path),
                    "--round-trip-cost-bps",
                    "3.5",
                    "--min-holdout-samples",
                    "100",
                ]
                code = PROBE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["mechanism_control_status"], "pass")
            self.assertEqual(payload["market_alpha_family_status"], "fail")
            self.assertEqual(payload["status"], "pass_with_actions")
            self.assertEqual(payload["deployable_candidate_manifest"]["status"], "fail")
            self.assertIsNone(
                payload["deployable_candidate_manifest"]["selected_candidate"]
            )
            self.assertIn(
                "no_market_alpha_candidate_passed_holdout_after_cost",
                payload["warn_reasons"],
            )

    def test_writes_candidate_manifest_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            feature_path = write_probe_fixture(root / "features.csv", aligned=True)
            output = root / "probe.json"
            manifest = root / "alpha_candidate_manifest.json"

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "alpha_mechanism_probe.py",
                    "--output",
                    str(output),
                    "--candidate-manifest-output",
                    str(manifest),
                    "--symbol",
                    "SOLUSDT",
                    "--feature_csv",
                    str(feature_path),
                    "--round-trip-cost-bps",
                    "3.5",
                    "--min-holdout-samples",
                    "100",
                ]
                code = PROBE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest_payload["schema_version"],
                "alpha_candidate_manifest_v1",
            )
            self.assertEqual(manifest_payload["status"], "pass")
            self.assertEqual(manifest_payload["symbols"], ["SOLUSDT"])

    def test_path_first_touch_finds_capture_candidate_without_fixed_horizon_edge(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            feature_path = write_path_capture_fixture(root / "path_features.csv")
            output = root / "probe.json"

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "alpha_mechanism_probe.py",
                    "--output",
                    str(output),
                    "--symbol",
                    "SOLUSDT",
                    "--feature_csv",
                    str(feature_path),
                    "--round-trip-cost-bps",
                    "3.5",
                    "--objective-mode",
                    "path_first_touch",
                    "--path-horizon-bars",
                    "12",
                    "--path-take-profit-bps",
                    "8.0",
                    "--path-stop-loss-bps",
                    "8.0",
                    "--min-mfe-cost-coverage",
                    "1.2",
                    "--min-holdout-samples",
                    "20",
                ]
                code = PROBE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["mechanism_control_status"], "pass")
            self.assertEqual(payload["market_alpha_family_status"], "pass")
            self.assertEqual(payload["target"]["objective_mode"], "path_first_touch")
            selected = payload["deployable_candidate_manifest"]["selected_candidate"]
            self.assertIsNotNone(selected)
            summary = selected["holdout_summary"]
            self.assertGreater(summary["mean_net_bps"], 0.0)
            self.assertGreaterEqual(summary["mean_mfe_cost_coverage_ratio"], 1.2)
            self.assertGreater(summary["take_profit_hit_ratio"], 0.9)


if __name__ == "__main__":
    unittest.main()
