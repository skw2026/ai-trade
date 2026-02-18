#!/usr/bin/env python3

import pathlib
import unittest
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class WebStaticTest(unittest.TestCase):
    def test_static_assets_exist(self):
        static_dir = APP_ROOT / "static"
        for name in ("index.html", "styles.css", "app.js"):
            path = static_dir / name
            self.assertTrue(path.exists(), f"missing static asset: {path}")

    def test_index_references_assets(self):
        content = (APP_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/static/styles.css", content)
        self.assertIn("/static/app.js", content)
        self.assertIn("trend-evolution-virtual-actions", content)
        self.assertIn("trend-evolution-counterfactual-actions", content)
        self.assertIn("trend-evolution-counterfactual-updates", content)
        self.assertIn("trend-evolution-factor-ic-actions", content)
        self.assertIn("trend-evolution-effective-updates", content)
        self.assertIn("trend-evolution-fallback-used", content)
        self.assertIn("trend-evolution-learnability-skips", content)
        self.assertIn("trend-flat-start-rebases", content)
        self.assertIn("trend-entry-regime-adjust", content)
        self.assertIn("trend-entry-volatility-adjust", content)
        self.assertIn("trend-entry-liquidity-adjust", content)
        self.assertIn("trend-maker-fill-ratio", content)
        self.assertIn("trend-unknown-fill-ratio", content)
        self.assertIn("trend-explicit-liquidity-fill-ratio", content)
        self.assertIn("trend-fee-bps-per-fill", content)
        self.assertIn("trend-quality-guard-active", content)
        self.assertIn("trend-reconcile-anomaly-ro", content)

    def test_main_has_ui_route_and_static_mount(self):
        main_py = (APP_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('app.mount("/static"', main_py)
        self.assertIn('@app.get("/", include_in_schema=False)', main_py)
        self.assertIn('@app.get("/ui", include_in_schema=False)', main_py)


if __name__ == "__main__":
    unittest.main()
