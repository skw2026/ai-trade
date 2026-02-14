#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import unittest


def load_assess_module():
    module_path = pathlib.Path(__file__).with_name("assess_run_log.py")
    spec = importlib.util.spec_from_file_location("assess_run_log", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ASSESS = load_assess_module()


class AssessRunLogTest(unittest.TestCase):
    def test_extract_strategy_mix_series_active(self):
        text = (
            "2026-02-14 15:02:18 [INFO] RUNTIME_STATUS: ticks=200, trade_ok=true, "
            "trading_halted=false, account={equity=168072.3, drawdown_pct=0.000212, "
            "notional=-2035.11, realized_pnl=-12.5, fees=3.4, realized_net=-15.9}, "
            "strategy_mix={latest_trend_notional=450.0, latest_defensive_notional=-180.0, "
            "latest_blended_notional=270.0, avg_abs_trend_notional=420.0, "
            "avg_abs_defensive_notional=160.0, avg_abs_blended_notional=260.0, "
            "samples=18}, integrator_mode=shadow\n"
        )
        series = ASSESS.extract_strategy_mix_series(text)
        self.assertEqual(series["runtime_count"], 1.0)
        self.assertEqual(series["nonzero_window_count"], 1.0)
        self.assertEqual(series["defensive_active_count"], 1.0)
        self.assertAlmostEqual(series["avg_abs_trend_notional"], 420.0)
        self.assertAlmostEqual(series["avg_abs_defensive_notional"], 160.0)
        self.assertGreater(series["avg_defensive_share"], 0.0)

    def test_extract_strategy_mix_series_ignore_zero_samples(self):
        text = (
            "2026-02-14 15:02:18 [INFO] RUNTIME_STATUS: ticks=180, trade_ok=true, "
            "trading_halted=false, account={equity=168070.0, drawdown_pct=0.000210, "
            "notional=-1800.0, realized_pnl=-10.0, fees=2.0, realized_net=-12.0}, "
            "strategy_mix={latest_trend_notional=300.0, latest_defensive_notional=-120.0, "
            "latest_blended_notional=180.0, avg_abs_trend_notional=0.0, "
            "avg_abs_defensive_notional=20.0, avg_abs_blended_notional=0.0, "
            "samples=0}, integrator_mode=shadow\n"
            "2026-02-14 15:02:38 [INFO] RUNTIME_STATUS: ticks=200, trade_ok=true, "
            "trading_halted=false, account={equity=168071.0, drawdown_pct=0.000211, "
            "notional=-1820.0, realized_pnl=-11.0, fees=2.3, realized_net=-13.3}, "
            "strategy_mix={latest_trend_notional=320.0, latest_defensive_notional=0.0, "
            "latest_blended_notional=320.0, avg_abs_trend_notional=300.0, "
            "avg_abs_defensive_notional=0.0, avg_abs_blended_notional=300.0, "
            "samples=12}, integrator_mode=shadow\n"
        )
        series = ASSESS.extract_strategy_mix_series(text)
        self.assertEqual(series["runtime_count"], 2.0)
        self.assertEqual(series["nonzero_window_count"], 1.0)
        self.assertEqual(series["defensive_active_count"], 0.0)

    def test_assess_contains_strategy_mix_metrics(self):
        text = (
            "2026-02-14 15:02:18 [INFO] RUNTIME_STATUS: ticks=200, trade_ok=true, "
            "trading_halted=false, account={equity=168072.3, drawdown_pct=0.000212, "
            "notional=-2035.11, realized_pnl=-12.5, fees=3.4, realized_net=-15.9}, "
            "strategy_mix={latest_trend_notional=450.0, latest_defensive_notional=-180.0, "
            "latest_blended_notional=270.0, avg_abs_trend_notional=420.0, "
            "avg_abs_defensive_notional=160.0, avg_abs_blended_notional=260.0, "
            "samples=18}, integrator_mode=shadow\n"
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["DEPLOY"], min_runtime_status=1)
        metrics = report["metrics"]
        self.assertEqual(metrics["strategy_mix_runtime_count"], 1)
        self.assertEqual(metrics["strategy_mix_defensive_active_count"], 1)
        self.assertGreater(metrics["strategy_mix_avg_abs_defensive_notional"], 0.0)
        self.assertEqual(report["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
