#!/usr/bin/env python3

import hashlib
import importlib.util
import datetime as dt
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evaluate_activation_transaction",
    ROOT / "tools" / "evaluate_activation_transaction.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ActivationDecisionTest(unittest.TestCase):
    def make_state(
        self,
        root: pathlib.Path,
        *,
        min_complete_episodes: int = 5,
        min_positive_episode_ratio: float = 0.5,
        min_mean_realized_net_per_fill_usd: float = 0.0,
        max_pending_hours: float = 0.0,
    ):
        artifacts = {}
        for name in ("model", "report", "miner_report", "active_meta"):
            path = root / name
            path.write_text(name, encoding="utf-8")
            artifacts[name] = {"path": str(path), "sha256": digest(path)}
        policy = {
            "schema_version": MODULE.ACTIVATION_POLICY_SCHEMA,
            "min_complete_episodes": int(min_complete_episodes),
            "min_positive_episode_ratio": float(min_positive_episode_ratio),
            "min_mean_realized_net_per_fill_usd": (
                float(min_mean_realized_net_per_fill_usd)
            ),
            "max_pending_hours": float(max_pending_hours),
        }
        return {
            "schema_version": "closed_loop_activation_transaction_v2",
            "run_id": "run-1",
            "status": "activated_pending_validation",
            "created_at_utc": "2026-07-27T00:00:00Z",
            "activation_policy": policy,
            "activation_policy_sha256": MODULE.canonical_sha256(policy),
            "candidate": {
                "model_version": "model-v1",
                "training_symbol": "SOLUSDT",
                "bar_interval_ms": 300000,
                "identity": {
                    "model_sha256": artifacts["model"]["sha256"],
                    "report_sha256": artifacts["report"]["sha256"],
                    "runtime_config_sha256": "c" * 64,
                    "trade_bot_sha256": "d" * 64,
                },
                "artifacts": artifacts,
            },
        }

    def runtime(self, state, episodes):
        identity = state["candidate"]["identity"]
        hard_safety = {
            metric_name: 0 for metric_name in MODULE.HARD_SAFETY_METRICS
        }
        return {
            "verdict": "PASS",
            "metrics": {
                **hard_safety,
                "integrator_model_version_latest": "model-v1",
                "integrator_model_sha256_latest": identity["model_sha256"],
                "integrator_report_sha256_latest": identity["report_sha256"],
                "integrator_runtime_config_sha256_latest": "c" * 64,
                "integrator_trade_bot_sha256_latest": "d" * 64,
                "integrator_feature_training_symbol_latest": "SOLUSDT",
                "integrator_feature_bar_interval_ms_latest": 300000,
                "runtime_boot_id_latest": "boot-candidate-v1",
                "integrator_policy_filled_candidate_ids": ["model-v1"],
                "integrator_policy_closed_episode_events": episodes,
            },
        }

    @staticmethod
    def episode(index, pnl):
        return {
            "position_episode_id": f"episode-{index}",
            "candidate_id": "model-v1",
            "model_version": "model-v1",
            "mode": "canary",
            "policy_reason": "canary_independent_signal",
            "symbol": "SOLUSDT",
            "realized_net_usd": pnl,
            "funding_paid_usd": 0.01,
            "fill_event_count": 2,
            "unique_order_count": 2,
            "evidence_complete": True,
            "activation_transaction_id": "run-1",
            "evidence_boot_id": "boot-candidate-v1",
            "runtime_config_sha256": "c" * 64,
            "trade_bot_sha256": "d" * 64,
            "closed_at_utc": "2026-07-27T01:00:00Z",
            "recovered_after_restart": False,
        }

    def test_evidence_accumulates_across_windows_then_commits(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(
                pathlib.Path(td),
                min_complete_episodes=5,
                min_positive_episode_ratio=0.6,
            )
            first = MODULE.evaluate(
                state,
                self.runtime(state, [self.episode(i, 0.1) for i in range(3)]),
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.6,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0,
            )
            self.assertEqual(first["decision"], "pending")
            second = MODULE.evaluate(
                state,
                self.runtime(state, [self.episode(i, 0.1) for i in range(2, 6)]),
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.6,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0,
            )
            self.assertEqual(second["decision"], "commit")
            self.assertEqual(second["evidence"]["complete_episode_count"], 6)
            self.assertAlmostEqual(
                second["evidence"]["total_funding_paid_usd"], 0.06
            )

    def test_persisted_episode_funding_mutation_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(pathlib.Path(td))
            first_episode = self.episode(1, 0.1)
            first = MODULE.evaluate(
                state,
                self.runtime(state, [first_episode]),
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0,
            )
            self.assertEqual(first["decision"], "pending")

            changed_episode = self.episode(1, 0.1)
            changed_episode["funding_paid_usd"] = 0.02
            second = MODULE.evaluate(
                state,
                self.runtime(state, [changed_episode]),
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0,
            )
            self.assertEqual(second["decision"], "rollback")
            self.assertTrue(
                any(
                    "payload changed" in reason
                    for reason in second["hard_fail_reasons"]
                )
            )

    def test_negative_economics_rolls_back_after_minimum_sample(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(pathlib.Path(td))
            result = MODULE.evaluate(
                state,
                self.runtime(
                    state,
                    [self.episode(i, -0.1 if i < 4 else 0.1) for i in range(5)]
                ),
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0,
            )
            self.assertEqual(result["decision"], "rollback")
            self.assertTrue(
                any(
                    "mean realized net per fill 95% LCB failed" in reason
                    for reason in result["hard_fail_reasons"]
                )
            )

    def test_positive_point_estimate_with_negative_lcb_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(
                pathlib.Path(td),
                min_complete_episodes=30,
                min_positive_episode_ratio=0.0,
            )
            episodes = [
                self.episode(index, -0.01)
                for index in range(29)
            ]
            episodes.append(self.episode(29, 1.0))
            result = MODULE.evaluate(
                state,
                self.runtime(state, episodes),
                mechanism={"status": "pass"},
                min_complete_episodes=30,
                min_positive_episode_ratio=0.0,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0.0,
            )
            self.assertGreater(
                result["evidence"]["mean_realized_net_per_fill_usd"], 0.0
            )
            self.assertLess(
                result["evidence"][
                    "mean_episode_realized_net_per_fill_lcb95_usd"
                ],
                0.0,
            )
            self.assertEqual(result["decision"], "rollback")

    def test_identity_mismatch_rolls_back_without_waiting(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(pathlib.Path(td))
            runtime = self.runtime(state, [])
            runtime["metrics"]["integrator_trade_bot_sha256_latest"] = "e" * 64
            result = MODULE.evaluate(
                state,
                runtime,
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0,
            )
            self.assertEqual(result["decision"], "rollback")
            self.assertIn(
                "runtime four-part/model feature identity mismatch",
                result["hard_fail_reasons"],
            )

    def test_episode_transaction_identity_mismatch_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(pathlib.Path(td))
            episode = self.episode(1, 0.1)
            episode["activation_transaction_id"] = "another-run"
            result = MODULE.evaluate(
                state,
                self.runtime(state, [episode]),
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0.0,
            )
            self.assertEqual(result["decision"], "rollback")
            self.assertTrue(
                any(
                    "activation_transaction_id" in reason
                    for reason in result["hard_fail_reasons"]
                )
            )

    def test_restart_recovered_episode_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(pathlib.Path(td))
            episode = self.episode(1, 0.1)
            episode["recovered_after_restart"] = True
            result = MODULE.evaluate(
                state,
                self.runtime(state, [episode]),
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0.0,
            )
            self.assertEqual(result["decision"], "rollback")
            self.assertTrue(
                any(
                    "restart-recovered" in reason
                    for reason in result["hard_fail_reasons"]
                )
            )

    def test_episode_without_complete_entry_exit_lifecycle_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(
                pathlib.Path(td),
                min_complete_episodes=1,
                min_positive_episode_ratio=0.0,
            )
            episode = self.episode(1, 0.1)
            episode["fill_event_count"] = 1
            episode["unique_order_count"] = 1
            result = MODULE.evaluate(
                state,
                self.runtime(state, [episode]),
                mechanism={"status": "pass"},
                min_complete_episodes=1,
                min_positive_episode_ratio=0.0,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0.0,
            )
            self.assertEqual(result["decision"], "rollback")
            self.assertTrue(
                any(
                    "incomplete fill lifecycle" in reason
                    for reason in result["hard_fail_reasons"]
                )
            )

    def test_baseline_aligned_canary_is_not_promotion_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(
                pathlib.Path(td), min_complete_episodes=1
            )
            episode = self.episode(1, 1.0)
            episode["policy_reason"] = "canary_applied"
            result = MODULE.evaluate(
                state,
                self.runtime(state, [episode]),
                mechanism={"status": "pass"},
                min_complete_episodes=1,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0.0,
            )
            self.assertEqual(result["decision"], "pending")
            self.assertEqual(result["evidence"]["complete_episode_count"], 0)

    def test_runtime_fail_without_explicit_evidence_shortfall_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(
                pathlib.Path(td),
                min_complete_episodes=5,
                max_pending_hours=1,
            )
            runtime = self.runtime(
                state, [self.episode(i, 0.1) for i in range(5)]
            )
            runtime["verdict"] = "FAIL"
            result = MODULE.evaluate(
                state,
                runtime,
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=1,
                now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
            )
            self.assertEqual(result["decision"], "rollback")
            self.assertTrue(
                any(
                    "runtime verdict not committable" in reason
                    for reason in result["hard_fail_reasons"]
                )
            )

    def test_runtime_fail_for_evidence_shortfall_only_remains_pending(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(
                pathlib.Path(td),
                min_complete_episodes=5,
                max_pending_hours=0,
            )
            runtime = self.runtime(state, [self.episode(1, 0.1)])
            runtime["verdict"] = "FAIL"
            runtime["fail_reasons"] = [
                "candidate fill insufficient sample: complete canary episodes"
            ]
            result = MODULE.evaluate(
                state,
                runtime,
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0,
            )
            self.assertEqual(result["decision"], "pending")
            self.assertEqual(result["hard_fail_reasons"], [])

    def test_policy_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(pathlib.Path(td))
            with self.assertRaisesRegex(ValueError, "policy drift"):
                MODULE.evaluate(
                    state,
                    self.runtime(state, []),
                    mechanism={"status": "pass"},
                    min_complete_episodes=6,
                    min_positive_episode_ratio=0.5,
                    min_mean_realized_net_per_fill_usd=0.0,
                    max_pending_hours=0.0,
                )

    def test_missing_hard_safety_metric_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(pathlib.Path(td))
            runtime = self.runtime(
                state, [self.episode(i, 0.1) for i in range(5)]
            )
            del runtime["metrics"]["critical_count"]
            result = MODULE.evaluate(
                state,
                runtime,
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0.0,
            )
            self.assertEqual(result["decision"], "rollback")
            self.assertIn(
                "runtime hard safety metric missing: critical_count",
                result["hard_fail_reasons"],
            )

    def test_boot_change_during_pending_validation_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.make_state(pathlib.Path(td))
            first = MODULE.evaluate(
                state,
                self.runtime(state, [self.episode(1, 0.1)]),
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0.0,
            )
            self.assertEqual(first["decision"], "pending")
            runtime = self.runtime(state, [self.episode(2, 0.1)])
            runtime["metrics"]["runtime_boot_id_latest"] = "boot-restarted"
            second = MODULE.evaluate(
                state,
                runtime,
                mechanism={"status": "pass"},
                min_complete_episodes=5,
                min_positive_episode_ratio=0.5,
                min_mean_realized_net_per_fill_usd=0.0,
                max_pending_hours=0.0,
            )
            self.assertEqual(second["decision"], "rollback")
            self.assertTrue(
                any(
                    "runtime boot changed" in reason
                    for reason in second["hard_fail_reasons"]
                )
            )


if __name__ == "__main__":
    unittest.main()
