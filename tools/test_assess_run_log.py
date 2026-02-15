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
    @staticmethod
    def _runtime_line(
        tick: int,
        notional: float,
        reduce_only: bool = False,
        trading_halted: bool = False,
        public_ws_healthy: bool = True,
        private_ws_healthy: bool = True,
    ) -> str:
        reduce_only_text = "true" if reduce_only else "false"
        trading_halted_text = "true" if trading_halted else "false"
        public_ws_healthy_text = "true" if public_ws_healthy else "false"
        private_ws_healthy_text = "true" if private_ws_healthy else "false"
        return (
            f"2026-02-14 15:02:{tick % 60:02d} [INFO] RUNTIME_STATUS: ticks={tick}, "
            f"trade_ok=true, trading_halted={trading_halted_text}, "
            "ws={market_channel=public_ws, fill_channel=private_ws, "
            f"public_ws_healthy={public_ws_healthy_text}, private_ws_healthy={private_ws_healthy_text}"
            "}, "
            "account={equity=100000.0, drawdown_pct=0.000100, "
            f"notional={notional:.6f}, realized_pnl=0.0, fees=0.0, realized_net=0.0}}, "
            "strategy_mix={latest_trend_notional=0.0, latest_defensive_notional=0.0, "
            "latest_blended_notional=0.0, avg_abs_trend_notional=0.0, "
            "avg_abs_defensive_notional=0.0, avg_abs_blended_notional=0.0, samples=0}, "
            "integrator_mode=shadow, "
            f"gate_runtime={{enabled=true, fail_streak=0, pass_streak=0, reduce_only={reduce_only_text}, "
            "reduce_only_cooldown_ticks=0, gate_halted=false, halt_cooldown_ticks=0, flat_ticks=0}}\n"
        )

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

    def test_s5_fail_when_no_gate_pass(self):
        runtime = "".join(
            self._runtime_line(20 + i * 20, 0.0, reduce_only=(i % 2 == 0))
            for i in range(60)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_FAILED: raw_signals=0, order_intents=0, effective_signals=0, fills=0, fail_reasons=[FAIL_LOW_ACTIVITY_SIGNALS,FAIL_LOW_ACTIVITY_FILLS]\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S5"], min_runtime_status=50)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(
            any("未检测到 GATE_CHECK_PASSED" in x for x in report["fail_reasons"])
        )

    def test_s5_fail_when_start_not_flat(self):
        runtime = "".join(
            self._runtime_line(20 + i * 20, 180.0 if i == 0 else 0.0)
            for i in range(60)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=4, order_intents=4, effective_signals=4, fills=1\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S5"], min_runtime_status=50)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(
            any("运行窗口起点非平仓状态" in x for x in report["fail_reasons"])
        )

    def test_deploy_ignores_soft_warns(self):
        runtime = "".join(
            self._runtime_line(20 + i * 20, 0.0, reduce_only=True)
            for i in range(12)
        )
        text = (
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_FAILED: raw_signals=0, order_intents=0, effective_signals=0, fills=0, fail_reasons=[FAIL_LOW_ACTIVITY_SIGNALS,FAIL_LOW_ACTIVITY_FILLS]\n"
            "2026-02-14 15:30:01 [INFO] GATE_CHECK_FAILED: raw_signals=0, order_intents=0, effective_signals=0, fills=0, fail_reasons=[FAIL_LOW_ACTIVITY_SIGNALS,FAIL_LOW_ACTIVITY_FILLS]\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["DEPLOY"], min_runtime_status=2)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["warn_reasons"], [])

    def test_deploy_ignores_strategy_fail_like_signals(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                180.0 if i == 0 else 0.0,
                reduce_only=True,
                trading_halted=True,
            )
            for i in range(12)
        )
        text = (
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_FAILED: raw_signals=0, order_intents=0, effective_signals=0, fills=0, fail_reasons=[FAIL_LOW_ACTIVITY_SIGNALS,FAIL_LOW_ACTIVITY_FILLS]\n"
            "2026-02-14 15:30:01 [INFO] GATE_CHECK_FAILED: raw_signals=0, order_intents=0, effective_signals=0, fills=0, fail_reasons=[FAIL_LOW_ACTIVITY_SIGNALS,FAIL_LOW_ACTIVITY_FILLS]\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["DEPLOY"], min_runtime_status=2)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["fail_reasons"], [])
        self.assertEqual(report["warn_reasons"], [])

    def test_deploy_fail_on_ws_unhealthy(self):
        text = self._runtime_line(20, 0.0, public_ws_healthy=False)
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["DEPLOY"], min_runtime_status=1)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(any("WS 健康检查失败次数" in x for x in report["fail_reasons"]))

    def test_deploy_fail_on_critical(self):
        text = "2026-02-14 15:30:01 [CRITICAL] fatal error\n" + self._runtime_line(20, 0.0)
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["DEPLOY"], min_runtime_status=1)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(any("出现 CRITICAL" in x for x in report["fail_reasons"]))

    def test_deploy_allows_zero_runtime_status(self):
        text = "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
        report = ASSESS.assess(
            text,
            ASSESS.STAGE_RULES["DEPLOY"],
            min_runtime_status=ASSESS.STAGE_RULES["DEPLOY"].min_runtime_status,
        )
        self.assertEqual(report["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
