#!/usr/bin/env python3

import pathlib
import tempfile
import unittest

import run_market_alpha_development as runner


class MarketAlphaDevelopmentRunnerTest(unittest.TestCase):
    def test_residual_variant_is_only_run_with_market_alpha_features(self):
        variants = ["continuous_return_huber", runner.RESIDUAL_VARIANT]
        self.assertEqual(
            runner.variants_for_feature_set(variants, "expanded_ohlcv_v1"),
            ["continuous_return_huber"],
        )
        self.assertEqual(
            runner.variants_for_feature_set(variants, "expanded_market_alpha_v1"),
            variants,
        )

    def test_domain_guard_rejects_selection_and_unnamed_input(self):
        with self.assertRaisesRegex(ValueError, "development"):
            runner.ensure_development_input(pathlib.Path("research_selection.csv"))
        with self.assertRaisesRegex(ValueError, "development"):
            runner.ensure_development_input(pathlib.Path("ohlcv.csv"))
        runner.ensure_development_input(pathlib.Path("research_development_ohlcv.csv"))

    def test_verification_never_grants_promotion(self):
        probe = {
            "status": "diagnostic_complete",
            "promotion_evidence": False,
            "data": {"feature_set": "expanded_market_alpha_v1"},
            "variants": [
                {
                    "variant": "continuous_return_huber",
                    "metrics_development_oos": {
                        "passes_development_economic_screen": True,
                        "mean_model_net_edge_bps": 2.0,
                        "model_net_edge_lcb_bps": 1.0,
                        "model_net_total_trades": 30,
                        "positive_model_net_edge_ratio_by_split": 0.7,
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            anchor = pathlib.Path(temp_dir) / "research_development.csv"
            anchor.write_text("timestamp,close\n1,1\n", encoding="utf-8")
            result = runner.build_verification(
                anchor_path=anchor,
                market_report={"status": "PASS"},
                trade_report={"status": "PASS"},
                probe_reports=[probe],
            )
        self.assertTrue(result["fully_verifiable"])
        self.assertTrue(result["economic_screen"]["development_passed"])
        self.assertFalse(result["promotion_evidence"])
        self.assertFalse(result["promotion_eligible"])
        self.assertEqual(result["next_gate"], "independent_selection_required")


if __name__ == "__main__":
    unittest.main()
