#!/usr/bin/env python3

import datetime as dt
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evaluate_demo_incubation",
    ROOT / "tools" / "evaluate_demo_incubation.py",
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class DemoIncubationTest(unittest.TestCase):
    def write_json(self, path: pathlib.Path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def policy(self):
        return {
            "schema_version": module.POLICY_SCHEMA,
            "environment": {
                "system_mode": "paper",
                "exchange_platform": "bybit",
                "testnet": False,
                "demo_trading": True,
                "self_evolution_enabled": True,
            },
            "min_observation_hours": 24,
            "min_observation_count": 1,
            "min_distinct_trading_days": 2,
            "min_complete_closed_lots": 5,
            "min_positive_closed_lot_ratio": 0.6,
            "min_total_closed_lot_net_usd": 0.0,
            "min_mean_closed_lot_net_lcb95_usd": 0.0,
            "min_account_equity_change_usd": 0.0,
            "min_account_realized_net_change_usd": 0.0,
            "max_drawdown_pct": 0.05,
            "min_effective_learning_updates": 2,
            "min_learning_update_days": 2,
            "min_learnability_passed_updates": 2,
            "max_learning_rollback_ratio": 0.2,
            "require_state_restore_after_restart": True,
            "require_latest_flat": True,
            "required_latest_overall_status": "PASS",
            "required_latest_runtime_verdict": "PASS",
            "required_latest_replay_status": "PASS",
            "required_latest_mechanism_statuses": ["PASS", "PASS_WITH_ACTIONS"],
            "required_latest_convergence_status": (
                "CONVERGED_CANARY_VALIDATED_WITH_LIVE_FILLS"
            ),
        }

    def config_text(self, demo=True):
        return f"""
system:
  mode: paper
  primary_symbol: SOLUSDT
exchange:
  platform: bybit
  bybit:
    testnet: false
    demo_trading: {'true' if demo else 'false'}
risk:
  max_abs_notional_usd: 100
execution:
  entry_fee_bps: 5.5
strategy:
  signal_notional_usd: 10
integrator:
  enabled: true
self_evolution:
  enabled: true
regime:
  enabled: true
universe:
  enabled: true
"""

    def make_inputs(self, root: pathlib.Path, *, demo=True, run_id="run-1"):
        policy_path = root / "policy.json"
        state_path = root / "state.json"
        config_path = root / "runtime.yaml"
        manifest_path = root / "manifest.json"
        report_path = root / "report.json"
        runtime_path = root / "runtime.json"
        ledger_path = root / "ledger.json"
        log_path = root / "runtime.log"
        self.write_json(policy_path, self.policy())
        config_path.write_text(self.config_text(demo), encoding="utf-8")
        self.write_json(
            manifest_path,
            {
                "run_id": run_id,
                "release": {"git_sha": "a" * 40},
                "git": {"commit": "a" * 40},
            },
        )
        self.write_json(
            report_path,
            {
                "run_id": run_id,
                "generated_at_utc": "2026-07-16T00:00:00Z",
                "overall_status": "PASS",
                "replay_readiness_status": "PASS",
                "closed_loop_mechanism_status": "PASS",
                "trading_convergence_status": (
                    "CONVERGED_CANARY_VALIDATED_WITH_LIVE_FILLS"
                ),
                "sections": {},
            },
        )
        metrics = {name: 0 for name in module.HARD_SAFETY_METRICS}
        metrics["runtime_boot_id_latest"] = "boot-1"
        self.write_json(runtime_path, {"verdict": "PASS", "metrics": metrics})
        lots = []
        for index in range(5):
            day = 1 if index < 3 else 2
            lots.append(
                {
                    "symbol": "SOLUSDT",
                    "side": "LONG",
                    "opened_at_utc": f"2026-07-{day:02d}T00:00:00Z",
                    "closed_at_utc": f"2026-07-{day:02d}T01:00:00Z",
                    "opening_fill_id": f"open-{index}",
                    "closing_fill_id": f"close-{index}",
                    "qty": 1.0,
                    "net_pnl_usd": 1.0,
                }
            )
        self.write_json(
            ledger_path,
            {
                "schema_version": "trade_ledger_v1",
                "quality": {
                    "conflicting_duplicate_count": 0,
                    "malformed_fill_count": 0,
                    "position_reconciliation_mismatch_count": 0,
                },
                "closed_lots": lots,
            },
        )
        log_path.write_text(
            "2026-07-01 00:00:00 [INFO] RUNTIME_STATUS: ticks=1, "
            "boot={id=boot-1, startup_utc=2026-07-01T00:00:00Z}, "
            "account={equity=10000, drawdown_pct=0.01, notional=0, "
            "realized_pnl=0, fees=0, realized_net=0}\n"
            "2026-07-01 01:00:00 [INFO] SELF_EVOLUTION_ACTION: "
            "type=updated, bucket=RANGE, reason=EVOLUTION_WEIGHT_INCREASE_TREND, "
            "learnability={enabled=true, passed=true, t_stat=1.0, samples=100}\n"
            "2026-07-02 01:00:00 [INFO] SELF_EVOLUTION_ACTION: "
            "type=updated, bucket=TREND, reason=EVOLUTION_WEIGHT_DECREASE_TREND, "
            "learnability={enabled=true, passed=true, t_stat=1.0, samples=100}\n"
            "2026-07-16 00:00:00 [INFO] RUNTIME_STATUS: ticks=2, "
            "boot={id=boot-1, startup_utc=2026-07-01T00:00:00Z}, "
            "account={equity=10005, drawdown_pct=0.01, notional=0, "
            "realized_pnl=6, fees=1, realized_net=5}\n",
            encoding="utf-8",
        )
        return {
            "policy_path": policy_path,
            "state_path": state_path,
            "config_path": config_path,
            "manifest_path": manifest_path,
            "closed_loop_report_path": report_path,
            "runtime_assess_path": runtime_path,
            "trade_ledger_path": ledger_path,
            "runtime_log_path": log_path,
            "now": dt.datetime(2026, 7, 16, tzinfo=dt.timezone.utc),
        }

    def test_profitable_stable_demo_is_only_eligible_for_manual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_inputs(pathlib.Path(tmp))
            state, report = module.evaluate_and_update(**args)
            self.assertEqual(report["decision"], module.ELIGIBLE)
            self.assertFalse(report["auto_live_switch"])
            self.assertFalse(report["mainnet_runtime_enabled"])
            self.assertEqual(report["evidence"]["complete_closed_lot_count"], 5)
            self.assertGreater(
                report["evidence"]["mean_closed_lot_net_lcb95_usd"], 0
            )
            self.assertEqual(len(state["generations"]), 1)

    def test_non_demo_configuration_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_inputs(pathlib.Path(tmp), demo=False)
            _, report = module.evaluate_and_update(**args)
            self.assertEqual(report["decision"], module.BLOCKED)
            self.assertTrue(
                any("not the frozen Bybit Demo" in reason for reason in report["hard_block_reasons"])
            )

    def test_same_run_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            args = self.make_inputs(root)
            state, _ = module.evaluate_and_update(**args)
            module.write_object(args["state_path"], state)
            report = json.loads(args["closed_loop_report_path"].read_text())
            report["overall_status"] = "FAIL"
            self.write_json(args["closed_loop_report_path"], report)
            with self.assertRaisesRegex(ValueError, "run evidence mutated"):
                module.evaluate_and_update(**args)

    def test_hard_safety_event_blocks_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            args = self.make_inputs(root)
            runtime = json.loads(args["runtime_assess_path"].read_text())
            runtime["metrics"]["critical_count"] = 1
            self.write_json(args["runtime_assess_path"], runtime)
            _, report = module.evaluate_and_update(**args)
            self.assertEqual(report["decision"], module.BLOCKED)
            self.assertEqual(report["evidence"]["hard_safety_event_count"], 1)

    def test_restart_without_persisted_weight_restore_stays_incubating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            args = self.make_inputs(root)
            state, _ = module.evaluate_and_update(**args)
            module.write_object(args["state_path"], state)

            report = json.loads(args["closed_loop_report_path"].read_text())
            report["run_id"] = "run-2"
            report["generated_at_utc"] = "2026-07-17T00:00:00Z"
            self.write_json(args["closed_loop_report_path"], report)
            manifest = json.loads(args["manifest_path"].read_text())
            manifest["run_id"] = "run-2"
            self.write_json(args["manifest_path"], manifest)
            runtime = json.loads(args["runtime_assess_path"].read_text())
            runtime["metrics"]["runtime_boot_id_latest"] = "boot-2"
            self.write_json(args["runtime_assess_path"], runtime)
            args["runtime_log_path"].write_text(
                "2026-07-17 00:00:00 [INFO] RUNTIME_STATUS: ticks=1, "
                "boot={id=boot-2, startup_utc=2026-07-17T00:00:00Z}, "
                "account={equity=10006, drawdown_pct=0.01, notional=0, "
                "realized_pnl=7, fees=1, realized_net=6}\n",
                encoding="utf-8",
            )
            args["now"] = dt.datetime(2026, 7, 17, tzinfo=dt.timezone.utc)

            _, incubation = module.evaluate_and_update(**args)
            self.assertEqual(incubation["decision"], module.INCUBATING)
            self.assertEqual(
                incubation["evidence"]["missing_restart_restore_boot_ids"],
                ["boot-2"],
            )


if __name__ == "__main__":
    unittest.main()
