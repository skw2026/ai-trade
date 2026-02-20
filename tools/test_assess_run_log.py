#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import datetime as dt
import re
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
        funnel_enqueued: int = 0,
        funnel_fills: int = 0,
        strategy_mix_samples: int = 1,
        strategy_mix_latest_trend: float = 180.0,
        strategy_mix_latest_defensive: float = -60.0,
        strategy_mix_latest_blended: float = 120.0,
        strategy_mix_avg_abs_trend: float = 180.0,
        strategy_mix_avg_abs_defensive: float = 60.0,
        strategy_mix_avg_abs_blended: float = 120.0,
        filtered_cost_ratio: float = 0.0,
        filtered_cost_near_miss_ratio: float = 0.0,
        passed_cost_near_miss_ratio: float = 0.0,
        entry_edge_gap_avg_bps: float = 0.0,
        realized_net_per_fill: float = 0.0,
        fee_bps_per_fill: float = 0.0,
        maker_fills: int = 0,
        taker_fills: int = 0,
        unknown_fills: int = 0,
        explicit_liquidity_fills: int = 0,
        fee_sign_fallback_fills: int = 0,
        unknown_fill_ratio: float = 0.0,
        explicit_liquidity_fill_ratio: float = 0.0,
        fee_sign_fallback_fill_ratio: float = 0.0,
        maker_fee_bps: float = 0.0,
        taker_fee_bps: float = 0.0,
        maker_fill_ratio: float = 0.0,
        entry_gate_near_miss_tolerance_bps: float = 0.0,
        entry_gate_near_miss_maker_allow: bool = False,
        entry_gate_near_miss_maker_max_gap_bps: float = 0.0,
        entry_gate_observed_filtered_ratio: float = 0.0,
        entry_gate_observed_near_miss_ratio: float = 0.0,
        entry_gate_observed_near_miss_allowed_ratio: float = 0.0,
        concentration_top1_share: float = 0.0,
        concentration_symbol_count: int = 0,
        execution_quality_guard_active: bool = False,
        execution_quality_guard_penalty_bps: float = 0.0,
        reconcile_anomaly_streak: int = 0,
        reconcile_anomaly_reduce_only: bool = False,
        equity: float = 100000.0,
        realized_pnl: float = 0.0,
        fees: float = 0.0,
        realized_net: float = 0.0,
        prefix: str = "",
    ) -> str:
        reduce_only_text = "true" if reduce_only else "false"
        trading_halted_text = "true" if trading_halted else "false"
        public_ws_healthy_text = "true" if public_ws_healthy else "false"
        private_ws_healthy_text = "true" if private_ws_healthy else "false"
        execution_quality_guard_active_text = (
            "true" if execution_quality_guard_active else "false"
        )
        reconcile_anomaly_reduce_only_text = (
            "true" if reconcile_anomaly_reduce_only else "false"
        )
        entry_gate_near_miss_maker_allow_text = (
            "true" if entry_gate_near_miss_maker_allow else "false"
        )
        ts = (dt.datetime(2026, 2, 14, 15, 0, 0) + dt.timedelta(seconds=tick)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return prefix + (
            f"{ts} [INFO] RUNTIME_STATUS: ticks={tick}, "
            f"trade_ok=true, trading_halted={trading_halted_text}, "
            "ws={market_channel=public_ws, fill_channel=private_ws, "
            f"public_ws_healthy={public_ws_healthy_text}, private_ws_healthy={private_ws_healthy_text}"
            "}, "
            f"account={{equity={equity:.6f}, drawdown_pct=0.000100, "
            f"notional={notional:.6f}, realized_pnl={realized_pnl:.6f}, "
            f"fees={fees:.6f}, realized_net={realized_net:.6f}}}, "
            "concentration={gross_notional_usd=1000.0, top1_abs_notional_usd=800.0, "
            f"top1_symbol=BTCUSDT, top1_share={concentration_top1_share}, "
            f"symbol_count={concentration_symbol_count}}}, "
            "funnel_window={raw=1, risk_adjusted=1, intents_generated=1, "
            "intents_filtered_inactive_symbol=0, intents_filtered_min_notional=0, "
            "intents_filtered_fee_aware=0, throttled=0, "
            f"enqueued={funnel_enqueued}, async_ok=0, async_failed=0, fills={funnel_fills}, "
            "gate_alerts=0, evolution_updates=0, evolution_rollbacks=0, evolution_skipped=0, "
            "entry_edge_samples=1, entry_edge_avg_bps=2.0, entry_required_avg_bps=1.0}, "
            "strategy_mix={latest_trend_notional="
            f"{strategy_mix_latest_trend}, latest_defensive_notional={strategy_mix_latest_defensive}, "
            f"latest_blended_notional={strategy_mix_latest_blended}, avg_abs_trend_notional={strategy_mix_avg_abs_trend}, "
            f"avg_abs_defensive_notional={strategy_mix_avg_abs_defensive}, avg_abs_blended_notional={strategy_mix_avg_abs_blended}, "
            f"samples={strategy_mix_samples}}}, "
            "entry_gate={enabled=true, round_trip_cost_bps=13.0, "
            "min_expected_edge_bps=1.0, required_edge_cap_bps=8.0, "
            f"near_miss_tolerance_bps={entry_gate_near_miss_tolerance_bps}, "
            "near_miss_maker_allow="
            f"{entry_gate_near_miss_maker_allow_text}, "
            "near_miss_maker_max_gap_bps="
            f"{entry_gate_near_miss_maker_max_gap_bps}, "
            "quality_guard_penalty_bps=0.0, "
            f"observed_filtered_ratio={entry_gate_observed_filtered_ratio}, "
            f"observed_near_miss_ratio={entry_gate_observed_near_miss_ratio}, "
            f"observed_near_miss_allowed_ratio={entry_gate_observed_near_miss_allowed_ratio}}}, "
            "execution_window={filtered_cost_ratio="
            f"{filtered_cost_ratio}, filtered_cost_near_miss_ratio={filtered_cost_near_miss_ratio}, "
            f"passed_cost_near_miss_ratio={passed_cost_near_miss_ratio}, "
            f"entry_edge_gap_avg_bps={entry_edge_gap_avg_bps}, realized_net_delta_usd=0.0, "
            f"realized_net_per_fill={realized_net_per_fill}, fee_delta_usd=0.0, "
            f"fee_bps_per_fill={fee_bps_per_fill}, maker_fills={maker_fills}, "
            f"taker_fills={taker_fills}, unknown_fills={unknown_fills}, "
            f"explicit_liquidity_fills={explicit_liquidity_fills}, "
            f"fee_sign_fallback_fills={fee_sign_fallback_fills}, "
            f"unknown_fill_ratio={unknown_fill_ratio}, "
            f"explicit_liquidity_fill_ratio={explicit_liquidity_fill_ratio}, "
            f"fee_sign_fallback_fill_ratio={fee_sign_fallback_fill_ratio}, "
            f"maker_fee_bps={maker_fee_bps}, taker_fee_bps={taker_fee_bps}, "
            f"maker_fill_ratio={maker_fill_ratio}}}, "
            "execution_quality_guard={enabled=true, "
            f"active={execution_quality_guard_active_text}, bad_streak=0, good_streak=0, "
            "min_fills=12, trigger_streak=2, release_streak=2, "
            "min_realized_net_per_fill_usd=-0.005, max_fee_bps_per_fill=8.0, "
            f"applied_penalty_bps={execution_quality_guard_penalty_bps}}}, "
            "reconcile_runtime={anomaly_streak="
            f"{reconcile_anomaly_streak}, healthy_streak=0, "
            f"anomaly_reduce_only={reconcile_anomaly_reduce_only_text}, "
            "anomaly_reduce_only_threshold=3, anomaly_halt_threshold=6, "
            "anomaly_resume_threshold=3}, "
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
            "samples=18}, execution_window={filtered_cost_ratio=0.0, "
            "realized_net_delta_usd=0.0, realized_net_per_fill=0.0, fee_delta_usd=0.0, "
            "fee_bps_per_fill=0.0}, integrator_mode=shadow\n"
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
            "samples=18}, execution_window={filtered_cost_ratio=0.0, "
            "realized_net_delta_usd=0.0, realized_net_per_fill=0.0, fee_delta_usd=0.0, "
            "fee_bps_per_fill=0.0}, integrator_mode=shadow\n"
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["DEPLOY"], min_runtime_status=1)
        metrics = report["metrics"]
        self.assertEqual(metrics["strategy_mix_runtime_count"], 1)
        self.assertEqual(metrics["strategy_mix_defensive_active_count"], 1)
        self.assertGreater(metrics["strategy_mix_avg_abs_defensive_notional"], 0.0)
        self.assertEqual(metrics["execution_window_runtime_count"], 1)
        self.assertEqual(metrics["filtered_cost_ratio"], 0.0)
        self.assertEqual(report["verdict"], "PASS")

    def test_assess_extracts_execution_window_metrics(self):
        runtime = (
            self._runtime_line(
                20,
                0.0,
                filtered_cost_ratio=0.95,
                realized_net_per_fill=-1.2,
                fee_bps_per_fill=8.5,
                maker_fills=1,
                taker_fills=2,
                unknown_fills=0,
                explicit_liquidity_fills=3,
                fee_sign_fallback_fills=0,
                unknown_fill_ratio=0.0,
                explicit_liquidity_fill_ratio=1.0,
                fee_sign_fallback_fill_ratio=0.0,
                maker_fee_bps=-0.5,
                taker_fee_bps=9.5,
                maker_fill_ratio=0.333333,
            )
            + self._runtime_line(
                40,
                0.0,
                filtered_cost_ratio=0.85,
                realized_net_per_fill=0.3,
                fee_bps_per_fill=7.5,
                maker_fills=2,
                taker_fills=1,
                unknown_fills=0,
                explicit_liquidity_fills=2,
                fee_sign_fallback_fills=1,
                unknown_fill_ratio=0.0,
                explicit_liquidity_fill_ratio=2.0 / 3.0,
                fee_sign_fallback_fill_ratio=1.0 / 3.0,
                maker_fee_bps=-0.5,
                taker_fee_bps=8.8,
                maker_fill_ratio=0.666667,
            )
        )
        report = ASSESS.assess(runtime, ASSESS.STAGE_RULES["S3"], min_runtime_status=2)
        metrics = report["metrics"]
        self.assertEqual(metrics["execution_window_runtime_count"], 2)
        self.assertAlmostEqual(metrics["filtered_cost_ratio_avg"], 0.9, places=6)
        self.assertAlmostEqual(metrics["filtered_cost_ratio"], 0.85, places=6)
        self.assertAlmostEqual(metrics["realized_net_per_fill"], -0.45, places=6)
        self.assertAlmostEqual(metrics["fee_bps_per_fill"], 8.0, places=6)
        self.assertAlmostEqual(
            metrics["execution_window_maker_fill_ratio_avg"], 0.5, places=6
        )
        self.assertAlmostEqual(
            metrics["execution_window_maker_fee_bps_avg"], -0.5, places=6
        )
        self.assertAlmostEqual(
            metrics["execution_window_taker_fee_bps_avg"], 9.15, places=6
        )
        self.assertAlmostEqual(
            metrics["execution_window_explicit_liquidity_fills_avg"], 2.5, places=6
        )
        self.assertAlmostEqual(
            metrics["execution_window_fee_sign_fallback_fills_avg"], 0.5, places=6
        )
        self.assertAlmostEqual(
            metrics["execution_window_unknown_fill_ratio_avg"], 0.0, places=6
        )
        self.assertAlmostEqual(
            metrics["execution_window_explicit_liquidity_fill_ratio_avg"],
            (1.0 + (2.0 / 3.0)) / 2.0,
            places=6,
        )
        self.assertAlmostEqual(
            metrics["execution_window_fee_sign_fallback_fill_ratio_avg"],
            ((0.0 + (1.0 / 3.0)) / 2.0),
            places=6,
        )
        self.assertEqual(metrics["execution_window_liquidity_source_runtime_count"], 2)

    def test_assess_extracts_entry_gate_and_near_miss_metrics(self):
        runtime = (
            self._runtime_line(
                20,
                0.0,
                filtered_cost_ratio=0.60,
                filtered_cost_near_miss_ratio=0.20,
                passed_cost_near_miss_ratio=0.05,
                entry_edge_gap_avg_bps=0.35,
                entry_gate_near_miss_tolerance_bps=0.30,
                entry_gate_observed_filtered_ratio=0.45,
                entry_gate_observed_near_miss_ratio=0.25,
                entry_gate_observed_near_miss_allowed_ratio=0.08,
            )
            + self._runtime_line(
                40,
                0.0,
                filtered_cost_ratio=0.40,
                filtered_cost_near_miss_ratio=0.10,
                passed_cost_near_miss_ratio=0.02,
                entry_edge_gap_avg_bps=0.15,
                entry_gate_near_miss_tolerance_bps=0.10,
                entry_gate_observed_filtered_ratio=0.35,
                entry_gate_observed_near_miss_ratio=0.15,
                entry_gate_observed_near_miss_allowed_ratio=0.04,
            )
        )
        text = (
            "2026-02-14 15:00:00 [INFO] GATE_CHECK_PASSED: raw_signals=4, order_intents=2, effective_signals=4, fills=1\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S3"], min_runtime_status=2)
        metrics = report["metrics"]
        self.assertEqual(metrics["entry_gate_runtime_count"], 2)
        self.assertAlmostEqual(
            metrics["entry_gate_near_miss_tolerance_bps_avg"], 0.20, places=6
        )
        self.assertAlmostEqual(
            metrics["entry_gate_observed_filtered_ratio_avg"], 0.40, places=6
        )
        self.assertAlmostEqual(
            metrics["entry_gate_observed_near_miss_ratio_avg"], 0.20, places=6
        )
        self.assertAlmostEqual(
            metrics["entry_gate_observed_near_miss_ratio"], 0.15, places=6
        )
        self.assertAlmostEqual(
            metrics["entry_gate_observed_near_miss_allowed_ratio_avg"], 0.06, places=6
        )
        self.assertAlmostEqual(
            metrics["entry_gate_observed_near_miss_allowed_ratio"], 0.04, places=6
        )
        self.assertAlmostEqual(metrics["filtered_cost_near_miss_ratio_avg"], 0.15, places=6)
        self.assertAlmostEqual(metrics["passed_cost_near_miss_ratio_avg"], 0.035, places=6)
        self.assertAlmostEqual(metrics["entry_edge_gap_avg_bps"], 0.25, places=6)

    def test_assess_warn_on_high_concentration(self):
        runtime = (
            self._runtime_line(20, 0.0, concentration_top1_share=0.92, concentration_symbol_count=3)
            + self._runtime_line(
                40, 0.0, concentration_top1_share=0.95, concentration_symbol_count=3
            )
        )
        text = (
            "2026-02-14 15:00:00 [INFO] GATE_CHECK_PASSED: raw_signals=4, order_intents=2, effective_signals=4, fills=1\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S3"], min_runtime_status=2)
        metrics = report["metrics"]
        self.assertEqual(report["verdict"], "PASS_WITH_ACTIONS")
        self.assertEqual(metrics["concentration_runtime_count"], 2)
        self.assertAlmostEqual(metrics["concentration_top1_share_avg"], 0.935, places=6)
        self.assertAlmostEqual(metrics["concentration_top1_share_max"], 0.95, places=6)
        self.assertEqual(metrics["concentration_high_count"], 2)
        self.assertTrue(any("仓位集中度偏高" in x for x in report["warn_reasons"]))

    def test_assess_extracts_quality_guard_and_reconcile_metrics(self):
        runtime = (
            self._runtime_line(
                20,
                0.0,
                execution_quality_guard_active=True,
                execution_quality_guard_penalty_bps=1.5,
                reconcile_anomaly_streak=2,
                reconcile_anomaly_reduce_only=True,
            )
            + self._runtime_line(
                40,
                0.0,
                execution_quality_guard_active=False,
                execution_quality_guard_penalty_bps=0.0,
                reconcile_anomaly_streak=0,
                reconcile_anomaly_reduce_only=False,
            )
        )
        text = (
            "2026-02-14 15:00:00 [INFO] EXECUTION_QUALITY_GUARD_ENTER: bad_streak=2\n"
            "2026-02-14 15:00:20 [INFO] OMS_RECONCILE_ANOMALY_STREAK: streak=2\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S3"], min_runtime_status=2)
        metrics = report["metrics"]
        self.assertEqual(metrics["execution_quality_guard_runtime_count"], 2)
        self.assertEqual(metrics["execution_quality_guard_active_count"], 1)
        self.assertEqual(metrics["execution_quality_guard_enter_count"], 1)
        self.assertAlmostEqual(
            metrics["execution_quality_guard_penalty_bps_avg"], 0.75, places=6
        )
        self.assertEqual(metrics["reconcile_runtime_count"], 2)
        self.assertEqual(metrics["reconcile_anomaly_streak_nonzero_count"], 1)
        self.assertEqual(metrics["reconcile_anomaly_reduce_only_true_count"], 1)
        self.assertEqual(metrics["reconcile_anomaly_event_count"], 1)

    def test_assess_warn_on_low_explicit_liquidity_ratio(self):
        runtime = self._runtime_line(
            20,
            0.0,
            maker_fills=1,
            taker_fills=1,
            unknown_fills=0,
            explicit_liquidity_fills=0,
            fee_sign_fallback_fills=2,
            unknown_fill_ratio=0.0,
            explicit_liquidity_fill_ratio=0.0,
            fee_sign_fallback_fill_ratio=1.0,
            maker_fee_bps=-0.5,
            taker_fee_bps=8.5,
            maker_fill_ratio=0.5,
        )
        text = (
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=2, order_intents=2, effective_signals=2, fills=2\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S3"], min_runtime_status=1)
        self.assertEqual(report["verdict"], "PASS_WITH_ACTIONS")
        self.assertTrue(
            any("显式流动性标签覆盖率偏低" in x for x in report["warn_reasons"])
        )
        self.assertTrue(
            any("fee 符号兜底占比偏高" in x for x in report["warn_reasons"])
        )

    def test_assess_warn_on_missing_liquidity_source_fields(self):
        runtime = self._runtime_line(
            20,
            0.0,
            maker_fills=1,
            taker_fills=1,
            unknown_fills=0,
            explicit_liquidity_fills=2,
            fee_sign_fallback_fills=0,
            unknown_fill_ratio=0.0,
            explicit_liquidity_fill_ratio=1.0,
            fee_sign_fallback_fill_ratio=0.0,
            maker_fee_bps=-0.5,
            taker_fee_bps=8.5,
            maker_fill_ratio=0.5,
        )
        runtime = re.sub(
            r"explicit_liquidity_fills=\d+, fee_sign_fallback_fills=\d+, "
            r"unknown_fill_ratio=-?[0-9]+(?:\.[0-9]+)?, "
            r"explicit_liquidity_fill_ratio=-?[0-9]+(?:\.[0-9]+)?, "
            r"fee_sign_fallback_fill_ratio=-?[0-9]+(?:\.[0-9]+)?, ",
            "",
            runtime,
        )
        text = (
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=2, order_intents=2, effective_signals=2, fills=2\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S3"], min_runtime_status=1)
        self.assertEqual(report["verdict"], "PASS_WITH_ACTIONS")
        self.assertTrue(
            any("未观测到流动性来源细分字段" in x for x in report["warn_reasons"])
        )

    def test_s5_warn_when_evolution_actions_without_effective_updates(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                0.0,
                funnel_enqueued=1 if i == 0 else 0,
                funnel_fills=1 if i == 1 else 0,
            )
            for i in range(60)
        )
        actions = "".join(
            "2026-02-14 15:00:10 [INFO] SELF_EVOLUTION_ACTION: type=skipped, bucket=RANGE, "
            "reason=EVOLUTION_COUNTERFACTUAL_IMPROVEMENT_TOO_SMALL, counterfactual_search=true, "
            "learnability={enabled=true, passed=true, t_stat=1.200000, samples=140}\n"
            for _ in range(35)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=10, order_intents=1, effective_signals=10, fills=1\n"
            "2026-02-14 15:30:01 [INFO] BYBIT_SUBMIT: order_type=Limit, symbol=BTCUSDT\n"
            + actions
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S5"], min_runtime_status=50)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(
            any("SELF_EVOLUTION 有评估无有效更新" in x for x in report["fail_reasons"])
        )
        self.assertTrue(
            any("SELF_EVOLUTION 长时间仅评估未更新" in x for x in report["warn_reasons"])
        )

    def test_s5_no_warn_when_evolution_has_effective_updates(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                0.0,
                funnel_enqueued=1 if i == 0 else 0,
                funnel_fills=1 if i == 1 else 0,
            )
            for i in range(60)
        )
        update_action = (
            "2026-02-14 15:00:10 [INFO] SELF_EVOLUTION_ACTION: type=updated, bucket=RANGE, "
            "reason=EVOLUTION_COUNTERFACTUAL_INCREASE_TREND, counterfactual_search=true, "
            "learnability={enabled=true, passed=true, t_stat=1.200000, samples=140}\n"
        )
        non_update_actions = "".join(
            "2026-02-14 15:00:10 [INFO] SELF_EVOLUTION_ACTION: type=skipped, bucket=RANGE, "
            "reason=EVOLUTION_COUNTERFACTUAL_IMPROVEMENT_TOO_SMALL, counterfactual_search=true, "
            "learnability={enabled=true, passed=true, t_stat=1.200000, samples=140}\n"
            for _ in range(34)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=10, order_intents=1, effective_signals=10, fills=1\n"
            "2026-02-14 15:30:01 [INFO] BYBIT_SUBMIT: order_type=Limit, symbol=BTCUSDT\n"
            + update_action
            + non_update_actions
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S5"], min_runtime_status=50)
        self.assertEqual(report["verdict"], "PASS")
        self.assertFalse(
            any("SELF_EVOLUTION 长时间仅评估未更新" in x for x in report["warn_reasons"])
        )

    def test_s5_fail_when_realized_net_per_fill_below_threshold(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                0.0,
                funnel_enqueued=1,
                funnel_fills=1,
                realized_net_per_fill=-0.02,
                fee_bps_per_fill=6.0,
                maker_fills=1,
                taker_fills=1,
                unknown_fills=0,
                explicit_liquidity_fills=2,
                fee_sign_fallback_fills=0,
                unknown_fill_ratio=0.0,
                explicit_liquidity_fill_ratio=1.0,
                fee_sign_fallback_fill_ratio=0.0,
                maker_fee_bps=-0.2,
                taker_fee_bps=6.5,
                maker_fill_ratio=0.5,
            )
            for i in range(60)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=10, order_intents=10, effective_signals=10, fills=10\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S5"], min_runtime_status=50)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(
            any("执行净收益质量未达标" in x for x in report["fail_reasons"])
        )

    def test_s5_fail_when_equity_change_below_threshold(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                0.0,
                funnel_enqueued=1,
                funnel_fills=1,
                realized_net_per_fill=0.01,
                equity=100000.0 - float(i) * 2.0,
                realized_net=0.0,
            )
            for i in range(60)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=10, order_intents=10, effective_signals=10, fills=10\n"
            + runtime
        )
        report = ASSESS.assess(
            text,
            ASSESS.STAGE_RULES["S5"],
            min_runtime_status=50,
            s5_min_equity_change_usd=-50.0,
            s5_min_equity_change_samples=10,
        )
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(
            any("权益变化未达标" in x for x in report["fail_reasons"])
        )

    def test_s5_fail_when_equity_realized_gap_above_threshold(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                0.0,
                funnel_enqueued=1,
                funnel_fills=1,
                realized_net_per_fill=0.01,
                equity=100000.0 + float(i) * 3.0,
                realized_net=0.0,
            )
            for i in range(60)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=10, order_intents=10, effective_signals=10, fills=10\n"
            + runtime
        )
        report = ASSESS.assess(
            text,
            ASSESS.STAGE_RULES["S5"],
            min_runtime_status=50,
            s5_max_equity_vs_realized_gap_usd=120.0,
            s5_min_equity_change_samples=10,
        )
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(
            any("权益与已实现净盈亏偏差过大" in x for x in report["fail_reasons"])
        )

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

    def test_s5_rebase_when_start_not_flat(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                180.0 if i == 0 else 0.0,
                funnel_enqueued=1 if i == 1 else 0,
                funnel_fills=1 if i == 2 else 0,
            )
            for i in range(60)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=4, order_intents=4, effective_signals=4, fills=1\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S5"], min_runtime_status=50)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["fail_reasons"], [])
        self.assertEqual(report["metrics"]["flat_start_rebase_applied_count"], 1)
        self.assertTrue(bool(report.get("flat_start_rebased")))

    def test_s5_rebase_when_start_not_flat_with_compose_prefix(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                180.0 if i == 0 else 0.0,
                funnel_enqueued=1 if i == 1 else 0,
                funnel_fills=1 if i == 2 else 0,
                prefix="ai-trade  | ",
            )
            for i in range(60)
        )
        text = (
            "ai-trade  | 2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "ai-trade  | 2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=4, order_intents=4, effective_signals=4, fills=1\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S5"], min_runtime_status=50)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["fail_reasons"], [])
        self.assertEqual(report["metrics"]["flat_start_rebase_applied_count"], 1)
        self.assertTrue(bool(report.get("flat_start_rebased")))

    def test_s5_fail_when_no_flat_sample(self):
        runtime = "".join(self._runtime_line(20 + i * 20, 180.0) for i in range(60))
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

    def test_s5_pass_with_execution_activity(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                0.0,
                funnel_enqueued=1 if i == 0 else 0,
                funnel_fills=1 if i == 1 else 0,
            )
            for i in range(60)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:00:10 [INFO] SELF_EVOLUTION_ACTION: type=skipped, bucket=RANGE, reason=EVOLUTION_WINDOW_PNL_TOO_SMALL\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=10, order_intents=1, effective_signals=10, fills=1\n"
            "2026-02-14 15:30:01 [INFO] BYBIT_SUBMIT: order_type=Limit, symbol=BTCUSDT\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S5"], min_runtime_status=50)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["fail_reasons"], [])

    def test_s5_fail_when_no_strategy_mix_window(self):
        runtime = "".join(
            self._runtime_line(
                20 + i * 20,
                0.0,
                funnel_enqueued=1 if i == 0 else 0,
                funnel_fills=1 if i == 1 else 0,
                strategy_mix_samples=0,
                strategy_mix_latest_trend=0.0,
                strategy_mix_latest_defensive=0.0,
                strategy_mix_latest_blended=0.0,
                strategy_mix_avg_abs_trend=0.0,
                strategy_mix_avg_abs_defensive=0.0,
                strategy_mix_avg_abs_blended=0.0,
            )
            for i in range(60)
        )
        text = (
            "2026-02-14 15:00:00 [INFO] SELF_EVOLUTION_INIT: trend_weight=0.5, defensive_weight=0.5, update_interval_ticks=600\n"
            "2026-02-14 15:00:10 [INFO] SELF_EVOLUTION_ACTION: type=skipped, bucket=RANGE, reason=EVOLUTION_WINDOW_PNL_TOO_SMALL\n"
            "2026-02-14 15:30:00 [INFO] GATE_CHECK_PASSED: raw_signals=10, order_intents=1, effective_signals=10, fills=1\n"
            "2026-02-14 15:30:01 [INFO] BYBIT_SUBMIT: order_type=Limit, symbol=BTCUSDT\n"
            + runtime
        )
        report = ASSESS.assess(text, ASSESS.STAGE_RULES["S5"], min_runtime_status=50)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(
            any("未检测到有效策略信号窗口" in x for x in report["fail_reasons"])
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
