#!/usr/bin/env python3

import csv
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("strategy_diagnose.py")
    spec = importlib.util.spec_from_file_location("strategy_diagnose", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STRATEGY_DIAGNOSE = load_module()


FEATURE_FIELDS = [
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


OHLCV_FIELDS = ["timestamp", "open", "high", "low", "close", "volume"]


def write_fixture(
    root: pathlib.Path,
    *,
    forward_return: float,
    path_high: float,
    close_step: float = 0.0,
) -> tuple[pathlib.Path, pathlib.Path]:
    feature_path = root / "feature_store_5m.csv"
    ohlcv_path = root / "ohlcv_5m.csv"
    start_ts = 1_700_000_000_000
    step = 300_000
    with feature_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=FEATURE_FIELDS)
        writer.writeheader()
        for index in range(48):
            writer.writerow(
                {
                    "timestamp": start_ts + index * step,
                    "close": 100.0,
                    "volume": 10.0,
                    "ret_1": 0.0,
                    "ret_3": 0.0,
                    "ret_12": 0.0,
                    "ema_fast": 100.2,
                    "ema_slow": 100.0,
                    "ema_diff": 0.002,
                    "vol_12": 0.001,
                    "vol_48": 0.001,
                    "zscore_48": 1.0,
                    "mom_12": 0.004,
                    "mom_48": 0.010,
                    "range_pct": 0.001,
                    "vol_chg_12": 0.0,
                    "forward_return": forward_return,
                }
            )
    with ohlcv_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=OHLCV_FIELDS)
        writer.writeheader()
        for index in range(72):
            close = 100.0 + float(index) * float(close_step)
            writer.writerow(
                {
                    "timestamp": start_ts + index * step,
                    "open": close,
                    "high": max(path_high, close),
                    "low": min(99.8, close),
                    "close": close,
                    "volume": 10.0,
                }
            )
    return feature_path, ohlcv_path


class StrategyDiagnoseTest(unittest.TestCase):
    def test_detects_raw_edge_but_low_mfe_capture(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            feature_path, ohlcv_path = write_fixture(
                root, forward_return=0.002, path_high=105.0
            )
            output = root / "strategy_diagnose_report.json"

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "strategy_diagnose.py",
                    "--output",
                    str(output),
                    "--symbol",
                    "SOLUSDT",
                    "--feature_csv",
                    str(feature_path),
                    "--ohlcv_csv",
                    str(ohlcv_path),
                    "--round-trip-cost-bps",
                    "13",
                    "--min-samples",
                    "30",
                ]
                code = STRATEGY_DIAGNOSE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "action_required")
            self.assertEqual(
                payload["aggregate"]["confirmed_trend"]["sample_count"], 48
            )
            self.assertGreater(
                payload["aggregate"]["confirmed_trend"]["mean_mfe_cost_coverage_ratio"],
                1.2,
            )
            self.assertTrue(
                any(
                    item["code"] == "path_mfe_available_but_capture_low"
                    for item in payload["diagnostics"]
                )
            )

    def test_detects_no_raw_net_edge_after_cost(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            feature_path, ohlcv_path = write_fixture(
                root, forward_return=0.0005, path_high=100.2
            )
            output = root / "strategy_diagnose_report.json"

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "strategy_diagnose.py",
                    "--output",
                    str(output),
                    "--symbol",
                    "BTCUSDT",
                    "--feature_csv",
                    str(feature_path),
                    "--ohlcv_csv",
                    str(ohlcv_path),
                    "--round-trip-cost-bps",
                    "13",
                    "--min-samples",
                    "30",
                ]
                code = STRATEGY_DIAGNOSE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "fail")
            self.assertLess(
                payload["aggregate"]["confirmed_trend"]["mean_net_forward_bps"], 0.0
            )
            self.assertTrue(
                any(
                    item["code"] == "confirmed_trend_raw_edge_non_positive"
                    for item in payload["diagnostics"]
                )
            )

    def test_alpha_tournament_finds_inverse_candidate_when_follow_is_negative(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            feature_path, ohlcv_path = write_fixture(
                root, forward_return=-0.002, path_high=100.2, close_step=-0.2
            )
            output = root / "strategy_diagnose_report.json"

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "strategy_diagnose.py",
                    "--output",
                    str(output),
                    "--symbol",
                    "BTCUSDT",
                    "--feature_csv",
                    str(feature_path),
                    "--ohlcv_csv",
                    str(ohlcv_path),
                    "--forward-bars",
                    "12",
                    "--tournament-horizons",
                    "12",
                    "--round-trip-cost-bps",
                    "13",
                    "--min-samples",
                    "30",
                ]
                code = STRATEGY_DIAGNOSE.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            tournament = payload["alpha_tournament"]
            self.assertEqual(tournament["status"], "pass")
            best = tournament["best_candidate"]
            self.assertEqual(best["base_name"], "confirmed_trend_inverse")
            self.assertEqual(best["direction_mode"], "inverse")
            self.assertGreater(best["summary"]["mean_net_forward_bps"], 0.0)
            self.assertTrue(
                any(
                    reason
                    == "alpha_tournament_found_viable_candidate_but_current_strategy_not_aligned"
                    for reason in payload["warn_reasons"]
                )
            )


if __name__ == "__main__":
    unittest.main()
