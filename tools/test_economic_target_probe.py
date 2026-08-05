#!/usr/bin/env python3

import csv
import math
import pathlib
import tempfile
import unittest

import numpy as np

import economic_target_probe as probe


class EconomicTargetProbeTest(unittest.TestCase):
    def test_continuous_and_ternary_targets_include_neutral_rows(self):
        returns = np.asarray([0.0, 0.001, -0.001, 0.002, np.nan])
        continuous = probe.build_target(
            returns,
            variant="continuous_return_huber",
            threshold_bps=14.3,
            target_clip=6.0,
        )
        ternary = probe.build_target(
            returns,
            variant="ternary_action_rmse",
            threshold_bps=14.3,
            target_clip=6.0,
        )
        self.assertEqual(float(continuous[0]), 0.0)
        self.assertGreater(float(continuous[1]), 0.0)
        self.assertEqual(ternary[:3].tolist(), [0.0, 0.0, 0.0])
        self.assertAlmostEqual(float(ternary[3]), math.log(3.0), places=12)
        self.assertTrue(math.isnan(float(continuous[-1])))

    def test_rolling_moments_are_causal_full_window(self):
        mean, stdev = probe.rolling_moments(np.asarray([1.0, 2.0, 3.0, 4.0]), 3)
        self.assertTrue(math.isnan(float(mean[1])))
        np.testing.assert_allclose(mean[2:], [2.0, 3.0])
        np.testing.assert_allclose(
            stdev[2:],
            [np.std([1.0, 2.0, 3.0]), np.std([2.0, 3.0, 4.0])],
        )

    def test_nested_calibration_fails_closed(self):
        raw = np.asarray([2.0, -2.0] * 5)
        favorable_return = np.asarray([0.01, -0.01] * 5)
        scale, report = probe.select_nested_validation_scale(
            raw_prediction=raw,
            execution_bar_return=favorable_return,
            quantiles=[0.5],
            round_trip_cost_bps=13.0,
            confidence_threshold=0.5,
            holding_bars=1,
            min_trades=5,
        )
        self.assertGreater(scale, 0.0)
        self.assertEqual(report["status"], "selected_on_nested_validation")

        scale, report = probe.select_nested_validation_scale(
            raw_prediction=raw,
            execution_bar_return=-favorable_return,
            quantiles=[0.5],
            round_trip_cost_bps=13.0,
            confidence_threshold=0.5,
            holding_bars=1,
            min_trades=5,
        )
        self.assertEqual(scale, 0.0)
        self.assertEqual(report["status"], "no_validation_candidate_passed")

    def test_derivative_axis_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "derivatives.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "timestamp",
                        "premium_index_close",
                        "open_interest",
                        "long_account_ratio",
                        "short_account_ratio",
                        "funding_rate",
                    ]
                )
                writer.writerow([100, 0.1, 10, 0.6, 0.4, 0.0001])
            with self.assertRaisesRegex(ValueError, "timestamp axes differ"):
                probe.load_derivatives_features(path, np.asarray([101]))

    def test_json_safe_replaces_non_finite_values(self):
        self.assertEqual(
            probe.json_safe({"nan": float("nan"), "inf": np.float64("inf")}),
            {"nan": None, "inf": None},
        )


if __name__ == "__main__":
    unittest.main()
