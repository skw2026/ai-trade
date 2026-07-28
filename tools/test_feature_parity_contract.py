#!/usr/bin/env python3

import csv
import importlib.util
import math
import pathlib
import sys
import unittest

import numpy as np


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "integrator_train_for_parity_test", ROOT / "tools" / "integrator_train.py"
)
assert SPEC and SPEC.loader
TRAIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TRAIN
SPEC.loader.exec_module(TRAIN)


class FeatureParityContractTest(unittest.TestCase):
    def test_python_training_semantics_match_checked_in_golden_vectors(self):
        fixture_dir = ROOT / "tools" / "fixtures"
        with (fixture_dir / "feature_parity_bars_v1.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            bars = list(csv.DictReader(handle))
        series = {
            name: np.asarray(
                [float(row[name]) for row in bars], dtype=np.float64
            )
            for name in ("open", "high", "low", "close", "volume")
        }
        evaluator = TRAIN.SafeExpressionEvaluator(series)
        close = series["close"]
        volume = series["volume"]
        macd_line = TRAIN.ema(close, 12) - TRAIN.ema(close, 26)
        macd_signal = TRAIN.ema(macd_line, 9)
        values = {
            "ret_1": TRAIN.ts_delta(close, 1)
            / (np.abs(TRAIN.ts_delay(close, 1)) + 1e-9),
            "ret_3": TRAIN.ts_delta(close, 3)
            / (np.abs(TRAIN.ts_delay(close, 3)) + 1e-9),
            "vol_delta_1": TRAIN.ts_delta(volume, 1),
            "rsi_14": TRAIN.rsi(close, 14),
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_line - macd_signal,
            "rank_12": evaluator.evaluate("ts_rank(close,12)"),
            "corr_12": evaluator.evaluate("ts_corr(close,volume,12)"),
        }
        with (fixture_dir / "feature_parity_expected_v1.tsv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            checks = list(csv.DictReader(handle, delimiter="\t"))

        self.assertGreaterEqual(len(checks), 20)
        for check in checks:
            actual = float(
                values[check["feature"]][int(check["sample_count"]) - 1]
            )
            expected = float(check["expected_value"])
            tolerance = float(check["tolerance"])
            self.assertTrue(math.isfinite(actual), check)
            self.assertAlmostEqual(
                actual,
                expected,
                delta=tolerance,
                msg=f"{check['sample_count']}/{check['feature']}",
            )


if __name__ == "__main__":
    unittest.main()
