#!/usr/bin/env python3

import importlib.util
import math
import pathlib
import sys
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parent


def load_module(name: str):
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TRAIN = load_module("integrator_train")


@unittest.skipIf(TRAIN.np is None, "numpy is required")
class IntegratorTrainTest(unittest.TestCase):
    def test_ts_rank_matches_online_feature_engine_semantics(self):
        values = TRAIN.np.asarray([1.0, 2.0, 3.0, 4.0, 5.0], dtype=TRAIN.np.float64)
        rank = TRAIN.ts_rank(values, window=5)
        self.assertAlmostEqual(float(rank[-1]), 0.9, places=12)

        flat = TRAIN.np.asarray([7.0, 7.0, 7.0, 7.0, 7.0], dtype=TRAIN.np.float64)
        flat_rank = TRAIN.ts_rank(flat, window=5)
        self.assertAlmostEqual(float(flat_rank[-1]), 0.5, places=12)

    def test_build_label_uses_t_plus_1_base(self):
        close = TRAIN.np.asarray([100.0, 101.0, 102.0, 103.0, 104.0], dtype=TRAIN.np.float64)
        label, forward = TRAIN.build_label(close, horizon=2)
        self.assertAlmostEqual(float(forward[0]), 103.0 / 101.0 - 1.0, places=12)
        self.assertAlmostEqual(float(forward[1]), 104.0 / 102.0 - 1.0, places=12)
        self.assertTrue(math.isnan(float(forward[2])))
        self.assertTrue(math.isnan(float(forward[3])))
        self.assertTrue(math.isnan(float(forward[4])))
        self.assertEqual(int(label[0]), 1)
        self.assertEqual(int(label[1]), 1)

    def test_build_label_drops_cost_band_neutral_samples(self):
        close = TRAIN.np.asarray(
            [100.0, 100.0, 100.03, 100.20, 99.80, 99.50],
            dtype=TRAIN.np.float64,
        )
        label, forward = TRAIN.build_label(
            close,
            horizon=1,
            label_round_trip_cost_bps=10.0,
            label_min_net_edge_bps=2.0,
        )
        self.assertAlmostEqual(float(forward[0]), 100.03 / 100.0 - 1.0, places=12)
        self.assertTrue(math.isnan(float(label[0])))
        self.assertEqual(int(label[1]), 1)
        self.assertEqual(int(label[2]), 0)

        valid_mask = TRAIN.np.isfinite(label)
        summary = TRAIN.build_label_policy_summary(
            label=label,
            forward_return=forward,
            label_round_trip_cost_bps=10.0,
            label_min_net_edge_bps=2.0,
            valid_mask=valid_mask,
        )
        self.assertEqual(summary["threshold_bps"], 12.0)
        self.assertEqual(summary["neutral_dropped_count"], 1)
        self.assertEqual(summary["valid_positive_label_count"], 1)
        self.assertEqual(summary["valid_negative_label_count"], 2)

    def test_feature_transform_clips_extreme_values_and_reports_bounds(self):
        feature = TRAIN.np.asarray([[float(i)] for i in range(100)], dtype=TRAIN.np.float64)
        feature[0, 0] = -1000.0
        feature[-1, 0] = 1000.0
        transformed, report = TRAIN.build_feature_transform(
            feature,
            ["miner_00"],
            feature_clip_quantile=0.05,
        )
        self.assertTrue(report["feature_clipping_enabled"])
        self.assertTrue(report["feature_normalization_enabled"])
        bound = report["clip_bounds"][0]
        self.assertTrue(bound["enabled"])
        self.assertTrue(bound["normalization_enabled"])
        self.assertGreater(transformed[0, 0], -8.0)
        self.assertLess(transformed[-1, 0], 8.0)
        self.assertGreater(bound["clipped_low_count"], 0)
        self.assertGreater(bound["clipped_high_count"], 0)
        self.assertIsNotNone(bound["center"])
        self.assertIsNotNone(bound["scale"])
        self.assertGreater(bound["scale"], 0.0)

    def test_feature_transform_normalizes_raw_price_scale_features(self):
        price_scale = TRAIN.np.asarray(
            [[70000.0 + float(i)] for i in range(100)],
            dtype=TRAIN.np.float64,
        )
        transformed, report = TRAIN.build_feature_transform(
            price_scale,
            ["miner_02"],
            feature_clip_quantile=0.01,
        )
        self.assertTrue(report["feature_normalization_enabled"])
        bound = report["clip_bounds"][0]
        self.assertTrue(bound["normalization_enabled"])
        self.assertGreater(bound["raw_max"], 70000.0)
        self.assertLessEqual(float(TRAIN.np.nanmax(TRAIN.np.abs(transformed))), 8.0)

    def test_split_temporal_train_validation_uses_tail_and_preserves_classes(self):
        x = TRAIN.np.arange(20, dtype=TRAIN.np.float64).reshape(-1, 1)
        y = TRAIN.np.asarray([0, 1] * 10, dtype=TRAIN.np.float64)
        x_fit, y_fit, x_val, y_val, meta = TRAIN.split_temporal_train_validation(
            x,
            y,
            validation_fraction=0.2,
            min_validation_samples=4,
        )
        self.assertIsNotNone(x_val)
        self.assertIsNotNone(y_val)
        self.assertEqual(meta["train_fit_count"], 16)
        self.assertEqual(meta["validation_count"], 4)
        self.assertEqual(int(x_fit.shape[0]), 16)
        self.assertEqual(int(x_val.shape[0]), 4)
        self.assertEqual(TRAIN.class_count(y_fit), {0: 8, 1: 8})
        self.assertEqual(TRAIN.class_count(y_val), {0: 2, 1: 2})

    def test_split_temporal_train_validation_disables_when_tail_single_class(self):
        x = TRAIN.np.arange(12, dtype=TRAIN.np.float64).reshape(-1, 1)
        y = TRAIN.np.asarray([0, 1, 0, 1, 0, 1, 0, 1, 1, 1, 1, 1], dtype=TRAIN.np.float64)
        x_fit, y_fit, x_val, y_val, meta = TRAIN.split_temporal_train_validation(
            x,
            y,
            validation_fraction=0.25,
            min_validation_samples=3,
        )
        self.assertIsNone(x_val)
        self.assertIsNone(y_val)
        self.assertEqual(meta["train_fit_count"], 12)
        self.assertEqual(meta["validation_count"], 0)
        self.assertEqual(int(x_fit.shape[0]), 12)
        self.assertEqual(TRAIN.class_count(y_fit), {0: 4, 1: 8})

    def test_evaluate_governance_extended_thresholds(self):
        metrics_ok = {
            "auc_mean": 0.58,
            "delta_auc_vs_baseline": 0.03,
            "split_trained_count": 4,
            "split_trained_ratio": 0.8,
            "auc_stdev": 0.03,
            "train_test_auc_gap_mean": 0.04,
            "random_label_auc": 0.50,
            "random_label_auc_mean": 0.50,
            "random_label_auc_max": 0.53,
        }
        passed, reasons, warns = TRAIN.evaluate_governance(
            metrics_oos=metrics_ok,
            min_auc_mean=0.55,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=2,
            min_split_trained_ratio=0.5,
            max_auc_stdev=0.08,
            max_train_test_auc_gap=0.10,
            run_random_label_control=True,
            max_random_label_auc=0.55,
        )
        self.assertTrue(passed)
        self.assertEqual(reasons, [])
        self.assertEqual(warns, [])

        metrics_gap_bad = dict(metrics_ok)
        metrics_gap_bad["train_test_auc_gap_mean"] = 0.18
        passed, reasons, warns = TRAIN.evaluate_governance(
            metrics_oos=metrics_gap_bad,
            min_auc_mean=0.55,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=2,
            min_split_trained_ratio=0.5,
            max_auc_stdev=0.08,
            max_train_test_auc_gap=0.10,
            run_random_label_control=True,
            max_random_label_auc=0.55,
        )
        self.assertFalse(passed)
        self.assertTrue(any("train_test_auc_gap_mean" in reason for reason in reasons))
        self.assertEqual(warns, [])

        metrics_missing_stdev = dict(metrics_ok)
        metrics_missing_stdev["split_trained_count"] = 3
        metrics_missing_stdev["auc_stdev"] = float("nan")
        passed, reasons, warns = TRAIN.evaluate_governance(
            metrics_oos=metrics_missing_stdev,
            min_auc_mean=0.55,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=2,
            min_split_trained_ratio=0.5,
            max_auc_stdev=0.08,
            max_train_test_auc_gap=0.10,
            run_random_label_control=True,
            max_random_label_auc=0.55,
        )
        self.assertFalse(passed)
        self.assertTrue(any("auc_stdev" in reason for reason in reasons))
        self.assertEqual(warns, [])

        metrics_random_bad = dict(metrics_ok)
        metrics_random_bad["random_label_auc"] = 0.61
        metrics_random_bad["random_label_auc_mean"] = 0.61
        passed, reasons, warns = TRAIN.evaluate_governance(
            metrics_oos=metrics_random_bad,
            min_auc_mean=0.55,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=2,
            min_split_trained_ratio=0.5,
            max_auc_stdev=0.08,
            max_train_test_auc_gap=0.10,
            run_random_label_control=True,
            max_random_label_auc=0.55,
        )
        self.assertFalse(passed)
        self.assertTrue(any("random_label_auc_mean" in reason for reason in reasons))
        self.assertEqual(warns, [])

        metrics_random_spike = dict(metrics_ok)
        metrics_random_spike["random_label_auc_max"] = 0.59
        passed, reasons, warns = TRAIN.evaluate_governance(
            metrics_oos=metrics_random_spike,
            min_auc_mean=0.55,
            min_delta_auc_vs_baseline=0.0,
            min_split_trained_count=2,
            min_split_trained_ratio=0.5,
            max_auc_stdev=0.08,
            max_train_test_auc_gap=0.10,
            run_random_label_control=True,
            max_random_label_auc=0.55,
        )
        self.assertTrue(passed)
        self.assertEqual(reasons, [])
        self.assertTrue(any("random_label_auc_max" in reason for reason in warns))

    def test_run_random_label_control_trials_returns_requested_count(self):
        if TRAIN.CatBoostClassifier is None:
            self.skipTest("catboost is required")
        x_train = TRAIN.np.arange(24, dtype=TRAIN.np.float64).reshape(-1, 2)
        y_train = TRAIN.np.asarray([0, 1] * 6, dtype=TRAIN.np.float64)
        x_test = TRAIN.np.arange(12, dtype=TRAIN.np.float64).reshape(-1, 2)
        y_test = TRAIN.np.asarray([0, 1, 0, 1, 0, 1], dtype=TRAIN.np.float64)
        aucs = TRAIN.run_random_label_control_trials(
            x_train=x_train,
            y_train=y_train,
            x_test=x_test,
            y_test=y_test,
            random_seed=42,
            iterations=4,
            depth=2,
            learning_rate=0.03,
            l2_leaf_reg=3.0,
            random_strength=1.0,
            subsample=0.8,
            rsm=0.8,
            trials=3,
        )
        self.assertEqual(len(aucs), 3)


if __name__ == "__main__":
    unittest.main()
