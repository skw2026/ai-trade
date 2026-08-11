#!/usr/bin/env python3

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parent


def load_module(name: str, filename: str):
    path = TOOLS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPLAY = load_module("paired_test_replay_validation", "run_replay_validation.py")
PAIR = load_module("run_paired_evolution_replay", "run_paired_evolution_replay.py")


class PairedEvolutionReplayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.runtime_config = self.root / "runtime.yaml"
        self.runtime_config.write_text(
            """system:
  mode: "demo"
  data_path: "runtime-state"
  primary_symbol: "BTCUSDT"
exchange:
  name: "bybit"
risk:
  max_position_notional_usd: 1000
execution:
  maker_fee_bps: 1.5
  taker_fee_bps: 5.5
  slippage_bps: 2.0
strategy:
  enabled: true
integrator:
  enabled: true
  shadow:
    model_path: "old-model.json"
    model_report_path: "old-report.json"
self_evolution:
  enabled: true
  update_interval_seconds: 17
  min_observations: 23
  virtual_learning_enabled: true
  counterfactual_learning_enabled: true
  counterfactual_min_observations: 31
  initial_trend_weight: 0.55
  initial_defensive_weight: 0.45
regime:
  enabled: true
universe:
  symbols: [BTCUSDT]
""",
            encoding="utf-8",
        )
        self.feature_csv = self.root / "features.csv"
        rows = []
        for index in range(4):
            price = 100.0 + index
            rows.append(
                {
                    "timestamp": 1_700_000_000_000 + index * 300_000,
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price + 0.25,
                    "volume": 10.0 + index,
                    "ema_diff": 0.01,
                    "zscore_48": 0.0,
                    "mom_12": 0.01,
                    "mom_48": 0.02,
                    "ret_1": 0.001,
                    "range_pct": 0.002,
                    "vol_12": 0.001,
                }
            )
        with self.feature_csv.open("w", encoding="utf-8", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        self.corpus_manifest = self.root / "corpus.json"
        self.corpus_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "replay_selection_manifest_v3",
                    "candidate_set_frozen": True,
                    "symbol": "BTCUSDT",
                    "target_bucket": "trend",
                    "base_interval_ms": 300_000,
                    "source_feature_csv": str(self.feature_csv),
                    "source_feature_sha256": self.sha256(self.feature_csv),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.candidate_model = self.root / "candidate.model.json"
        self.candidate_model.write_text('{"model":"candidate"}\n', encoding="utf-8")
        self.candidate_report = self.root / "candidate.report.json"
        self.candidate_report.write_text('{"status":"accepted"}\n', encoding="utf-8")
        self.trade_bot = self.root / "trade_bot"
        self.trade_bot.write_bytes(b"test-trade-bot")
        self.trade_bot.chmod(self.trade_bot.stat().st_mode | stat.S_IXUSR)

        feature_rows = REPLAY.load_feature_rows(self.feature_csv)
        blocks = []
        expected_dir = self.root / "expected"
        for number, (start_index, end_index) in enumerate(((0, 1), (2, 3)), start=1):
            segment = REPLAY.ReplaySegment(
                start_index=start_index,
                end_index=end_index,
                start_timestamp=feature_rows[start_index].timestamp,
                end_timestamp=feature_rows[end_index].timestamp,
                bars=end_index - start_index + 1,
            )
            replay_csv = expected_dir / f"block-{number:02d}.csv"
            REPLAY.write_replay_csv(
                feature_rows,
                segment,
                "BTCUSDT",
                replay_csv,
                300_000,
                warmup_context_bars=0,
            )
            blocks.append(
                {
                    "block_id": f"block-{number:02d}",
                    "start_timestamp_ms": segment.start_timestamp,
                    "end_timestamp_ms": segment.end_timestamp,
                    "event_sha256": self.sha256(replay_csv),
                    "cells": [
                        {"symbol": "BTCUSDT", "entry_regime": "trend"}
                    ],
                }
            )
        canonical_identity = {
            "schema_version": "decision_evidence_benchmark_v1",
            "components": {},
            "evaluation_universe": {"blocks": blocks},
        }
        self.benchmark_id = PAIR.canonical_sha256(canonical_identity)
        self.benchmark_report = self.root / "benchmark-report.json"
        self.benchmark_report.write_text(
            json.dumps(
                {
                    "schema_version": "decision_evidence_benchmark_validation_v1",
                    "identity_status": "VERIFIED",
                    "benchmark_id": self.benchmark_id,
                    "drifts": [],
                    "canonical_identity": canonical_identity,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.output_dir = self.root / "paired"

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def sha256(path: pathlib.Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def run_pair(self, fake_run) -> dict:
        with mock.patch.object(PAIR.subprocess, "run", side_effect=fake_run):
            return PAIR.run_paired_evolution_replay(
                runtime_config=self.runtime_config,
                candidate_model=self.candidate_model,
                candidate_report=self.candidate_report,
                feature_csv=self.feature_csv,
                corpus_manifest=self.corpus_manifest,
                trade_bot=self.trade_bot,
                output_dir=self.output_dir,
                benchmark_report=self.benchmark_report,
            )

    def fake_runner(self, mutate=None, return_codes=None):
        calls = []
        return_codes = return_codes or {}

        def run(command, check=False):
            self.assertFalse(check)
            command = [str(item) for item in command]
            calls.append(command)
            plan_path = pathlib.Path(command[command.index("--exact-block-plan") + 1])
            config_path = pathlib.Path(command[command.index("--base_config") + 1])
            output_dir = pathlib.Path(command[command.index("--output_dir") + 1])
            arm = output_dir.name
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            output_dir.mkdir(parents=True, exist_ok=True)
            policy = PAIR.policy_payload(config_path)
            blocks = []
            for index, planned in enumerate(plan["blocks"]):
                state_dir = output_dir / f"exact_block_{index + 1:03d}" / "state"
                state_dir.mkdir(parents=True, exist_ok=True)
                runtime_log = state_dir.parent / "runtime.log"
                runtime_assess = state_dir.parent / "runtime_assess.json"
                runtime_log.write_text("runtime evidence\n", encoding="utf-8")
                runtime_assess.write_text("{}\n", encoding="utf-8")
                trade_command = [
                    str(self.trade_bot),
                    f"--config={config_path}",
                    f"--data_path={state_dir}",
                    f"--replay_market_data={planned['replay_csv']}",
                ]
                blocks.append(
                    {
                        "plan_index": index,
                        "block_id": planned["block_id"],
                        "symbol": planned["symbol"],
                        "expected_event_sha256": planned["event_sha256"],
                        "actual_event_sha256": planned["event_sha256"],
                        "expected_segment_identity_sha256": planned[
                            "segment_identity_sha256"
                        ],
                        "actual_segment_identity_sha256": planned[
                            "segment_identity_sha256"
                        ],
                        "execution_attempt_count": 1,
                        "execution_status": "EXECUTED",
                        "command": trade_command,
                        "assess_command": [sys.executable, "assess_run_log.py"],
                        "trade_bot_exit_code": 0,
                        "assess_exit_code": 0,
                        "episode_execution_evidence": {
                            "schema_version": "episode_execution_evidence_v1",
                            "segment_identity_sha256": planned[
                                "segment_identity_sha256"
                            ],
                            "execution_policy_identity": policy,
                            "episode_count": 1,
                            "complete_episode_count": 1,
                            "execution_path_complete": True,
                            "aggregate_only_rejected": False,
                            "missing_path_evidence": [],
                            "episodes": [
                                {
                                    "evaluator_episode_id": f"episode-{index}",
                                    "execution_path_complete": True,
                                    "utility_source": "complete_execution_replay",
                                }
                            ],
                        },
                        "state_dir": str(state_dir),
                        "runtime_log": str(runtime_log),
                        "runtime_assess": str(runtime_assess),
                        "execution_policy_identity": policy,
                        "trade_bot_sha256": self.sha256(self.trade_bot),
                        "errors": [],
                    }
                )
            report = {
                "schema_version": "exact_replay_block_audit_v1",
                "mode": "exact_block_plan",
                "status": "VERIFIED",
                "promotion_authority": False,
                "selection_bypassed": True,
                "final_holdout_bypassed": True,
                "coverage_early_stop_disabled": True,
                "mutation_targets_accessed": [],
                "exact_block_plan": {
                    "path": str(plan_path),
                    "sha256": self.sha256(plan_path),
                    "benchmark_id": plan["benchmark_id"],
                    "target_bucket": plan["target_bucket"],
                    "read_only": True,
                },
                "base_config": str(config_path),
                "execution_policy_identity": policy,
                "trade_bot": str(self.trade_bot),
                "trade_bot_sha256": self.sha256(self.trade_bot),
                "planned_block_count": len(blocks),
                "executed_block_count": len(blocks),
                "validation_errors": [],
                "blocks": blocks,
                "runs": blocks,
            }
            if mutate is not None:
                mutate(arm, report)
            report_path = output_dir / "replay_validation_report.json"
            report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
            return mock.Mock(returncode=return_codes.get(arm, 0))

        run.calls = calls
        return run

    def test_runtime_is_derived_once_and_arms_only_differ_by_enabled(self):
        fake_run = self.fake_runner()
        with mock.patch.object(
            PAIR,
            "derive_candidate_config",
            wraps=PAIR.derive_candidate_config,
        ) as derive:
            manifest = self.run_pair(fake_run)

        self.assertEqual(manifest["status"], "VERIFIED")
        self.assertFalse(manifest["promotion_authority"])
        self.assertEqual(derive.call_count, 1)
        self.assertEqual(
            manifest["policy_differences"],
            [
                {
                    "path": "self_evolution.enabled",
                    "frozen": False,
                    "adaptive": True,
                }
            ],
        )
        frozen = manifest["arms"]["frozen"]["config"]["policy"]["policy"]
        adaptive = manifest["arms"]["adaptive"]["config"]["policy"]["policy"]
        for field, expected in {
            "self_evolution.update_interval_seconds": 17,
            "self_evolution.min_observations": 23,
            "self_evolution.virtual_learning_enabled": True,
            "self_evolution.counterfactual_learning_enabled": True,
            "self_evolution.counterfactual_min_observations": 31,
            "self_evolution.initial_trend_weight": 0.55,
            "self_evolution.initial_defensive_weight": 0.45,
        }.items():
            self.assertEqual(frozen[field], expected)
            self.assertEqual(adaptive[field], expected)
        self.assertEqual(
            manifest["initial_weights"]["payload"],
            {"defensive": 0.45, "trend": 0.55},
        )
        self.assertEqual(
            manifest["source_runtime_config"]["policy"]["sha256"],
            manifest["common_derived_config"]["policy"]["sha256"],
        )
        required_identity_keys = {
            "source_runtime_config",
            "common_derived_config",
            "common_policy",
            "initial_weights",
            "initial_evolution_state",
            "trade_bot",
            "candidate_model",
            "candidate_report",
            "benchmark_report",
            "exact_block_plan",
        }
        self.assertTrue(required_identity_keys.issubset(manifest))

    def test_both_arms_consume_same_exact_plan_and_all_blocks_once(self):
        fake_run = self.fake_runner()
        manifest = self.run_pair(fake_run)

        self.assertEqual(len(fake_run.calls), 2)
        plan_paths = []
        forbidden = {
            "--feature_csv",
            "--feature_csv_by_symbol",
            "--selection_feature_csv",
            "--selection_feature_csv_by_symbol",
            "--corpus_manifest",
            "--prevalidated_selection_report",
            "--holdout_ledger",
            "--experiment_id",
            "--require_candidate_identity",
        }
        for command in fake_run.calls:
            plan_paths.append(command[command.index("--exact-block-plan") + 1])
            self.assertIn("--force-all-frozen-segments", command)
            self.assertTrue(forbidden.isdisjoint(command))
        self.assertEqual(len(set(plan_paths)), 1)
        plan_path = pathlib.Path(plan_paths[0])
        self.assertEqual(stat.S_IMODE(plan_path.stat().st_mode), 0o444)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["benchmark_id"], self.benchmark_id)
        self.assertEqual(
            [block["block_id"] for block in plan["blocks"]],
            ["block-01", "block-02"],
        )
        self.assertEqual(
            manifest["exact_block_plan"]["sha256"], self.sha256(plan_path)
        )
        all_state_dirs = []
        all_runtime_outputs = []
        for arm_name, arm in manifest["arms"].items():
            self.assertEqual(arm["expected_block_ids"], ["block-01", "block-02"])
            self.assertEqual(arm["executed_block_ids"], ["block-01", "block-02"])
            self.assertEqual(arm["block_execution_counts"], {
                "block-01": 1,
                "block-02": 1,
            })
            self.assertEqual(arm["infrastructure_status"], "VERIFIED")
            for block in arm["blocks"]:
                self.assertEqual(
                    block["initial_weights_sha256"],
                    manifest["initial_weights"]["sha256"],
                )
                self.assertEqual(
                    block["initial_evolution_state_sha256"],
                    manifest["initial_evolution_state"]["sha256"],
                )
                self.assertFalse(block["historical_state_loaded"])
                self.assertIsNone(block["continued_from_block_id"])
                all_state_dirs.append(block["state_dir"])
                self.assertIn(f"/{arm_name}/", block["state_dir"])
                all_runtime_outputs.extend(
                    [block["runtime_log"], block["runtime_assess"]]
                )
        self.assertEqual(len(all_state_dirs), len(set(all_state_dirs)))
        self.assertEqual(len(all_runtime_outputs), len(set(all_runtime_outputs)))

    def test_runtime_to_common_policy_drift_fails_before_execution(self):
        original = PAIR.derive_candidate_config

        def drifting(*args, **kwargs):
            return original(*args, **kwargs).replace(
                "  max_position_notional_usd: 1000",
                "  max_position_notional_usd: 2000",
            )

        fake_run = self.fake_runner()
        with mock.patch.object(PAIR, "derive_candidate_config", side_effect=drifting):
            manifest = self.run_pair(fake_run)

        self.assertEqual(manifest["status"], "UNVERIFIABLE")
        self.assertEqual(fake_run.calls, [])
        self.assertIn(
            "common_replay_policy_differs_from_runtime",
            manifest["mismatches"],
        )

    def test_policy_drift_fails_preflight_and_still_writes_manifest(self):
        original = PAIR.derive_arm_config

        def drifting(common_text, *, enabled):
            text = original(common_text, enabled=enabled)
            if enabled:
                text = text.replace(
                    "  max_position_notional_usd: 1000",
                    "  max_position_notional_usd: 2000",
                )
            return text

        fake_run = self.fake_runner()
        with mock.patch.object(PAIR, "derive_arm_config", side_effect=drifting):
            manifest = self.run_pair(fake_run)

        self.assertEqual(manifest["status"], "UNVERIFIABLE")
        self.assertEqual(fake_run.calls, [])
        self.assertTrue(
            any("unexpected_policy_difference" in item for item in manifest["mismatches"])
        )
        persisted = json.loads(
            (self.output_dir / "paired_evolution_replay_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted["status"], "UNVERIFIABLE")
        self.assertIsNone(persisted["arms"]["frozen"]["exit_code"])

    def test_state_weight_and_coverage_drift_are_unverifiable(self):
        cases = {
            "history": lambda report: report["blocks"][0].update(
                {"historical_state_loaded": True}
            ),
            "weight": lambda report: report["blocks"][0].update(
                {"initial_weights_sha256": "0" * 64}
            ),
            "continuation": lambda report: report["blocks"][1].update(
                {"continued_from_block_id": "block-01"}
            ),
            "duplicate_state": lambda report: report["blocks"][1].update(
                {"state_dir": report["blocks"][0]["state_dir"]}
            ),
            "missing_block": lambda report: (
                report["blocks"].pop(),
                report.update({"executed_block_count": 1}),
            ),
        }
        for name, mutation in cases.items():
            with self.subTest(name=name):
                output = self.root / f"paired-{name}"
                self.output_dir = output

                def mutate(arm, report):
                    if arm == "adaptive":
                        mutation(report)

                manifest = self.run_pair(self.fake_runner(mutate=mutate))
                self.assertEqual(manifest["status"], "UNVERIFIABLE")
                self.assertTrue(manifest["mismatches"])
                self.assertEqual(
                    json.loads(
                        (output / "paired_evolution_replay_manifest.json").read_text(
                            encoding="utf-8"
                        )
                    )["status"],
                    "UNVERIFIABLE",
                )

    def test_one_arm_command_or_report_failure_does_not_skip_other_arm(self):
        def mutate(arm, report):
            if arm == "frozen":
                report.clear()

        fake_run = self.fake_runner(mutate=mutate, return_codes={"frozen": 9})
        manifest = self.run_pair(fake_run)

        self.assertEqual(len(fake_run.calls), 2)
        self.assertEqual(manifest["status"], "UNVERIFIABLE")
        self.assertEqual(manifest["arms"]["frozen"]["exit_code"], 9)
        self.assertEqual(manifest["arms"]["adaptive"]["exit_code"], 0)
        for arm in manifest["arms"].values():
            self.assertTrue(arm["command"])
            self.assertIn("config", arm)
            self.assertIn("report", arm)
            self.assertIn("sha256", arm["report"])
        self.assertTrue(any("report" in item for item in manifest["mismatches"]))

    def test_business_gate_failure_with_complete_exact_audit_is_not_infrastructure_failure(self):
        def mutate(_arm, report):
            report["status"] = "UNVERIFIABLE"
            report["validation_errors"] = []
            for index, block in enumerate(report["blocks"]):
                block["assess_exit_code"] = 1
                block["execution_status"] = "FAILED"
                block["errors"] = ["assess_exit_nonzero"]
                report["validation_errors"].append(
                    f"block[{index}].assess_exit_nonzero"
                )

        manifest = self.run_pair(
            self.fake_runner(
                mutate=mutate,
                return_codes={"frozen": 2, "adaptive": 2},
            )
        )

        self.assertEqual(manifest["status"], "VERIFIED")
        for arm in manifest["arms"].values():
            self.assertEqual(arm["exit_code"], 2)
            self.assertEqual(arm["infrastructure_status"], "VERIFIED")
            self.assertEqual(arm["business_gate_status"], "FAILED")

    def test_aggregate_only_or_incomplete_episode_evidence_is_unverifiable(self):
        def mutate(arm, report):
            if arm == "adaptive":
                report["blocks"][0]["episode_execution_evidence"] = {
                    "schema_version": "episode_execution_evidence_v1",
                    "execution_path_complete": False,
                    "episodes": [],
                    "aggregate_account_pnl": 99.0,
                    "self_evolution_update_count": 7,
                }

        manifest = self.run_pair(self.fake_runner(mutate=mutate))

        self.assertEqual(manifest["status"], "UNVERIFIABLE")
        self.assertTrue(
            any("execution_path_incomplete" in item for item in manifest["mismatches"])
        )

    def test_audited_zero_trade_block_is_retained_as_zero_not_proxy_utility(self):
        def mutate(_arm, report):
            for index, block in enumerate(report["blocks"]):
                block["assess_exit_code"] = 1
                block["execution_status"] = "FAILED"
                block["errors"] = ["assess_exit_nonzero"]
                evidence = block["episode_execution_evidence"]
                evidence.update(
                    {
                        "episode_count": 0,
                        "complete_episode_count": 0,
                        "execution_path_complete": False,
                        "aggregate_only_rejected": True,
                        "missing_path_evidence": ["fills"],
                        "episodes": [],
                        "account_realized_net_usd": 123.0,
                    }
                )
                report["validation_errors"].append(
                    f"block[{index}].assess_exit_nonzero"
                )
            report["status"] = "UNVERIFIABLE"

        manifest = self.run_pair(
            self.fake_runner(
                mutate=mutate,
                return_codes={"frozen": 2, "adaptive": 2},
            )
        )

        self.assertEqual(manifest["status"], "VERIFIED")
        for arm in manifest["arms"].values():
            self.assertTrue(all(block["no_trade_zero_utility"] for block in arm["blocks"]))

    def test_invalid_benchmark_preflight_is_atomic_and_runs_no_arm(self):
        invalid = json.loads(self.benchmark_report.read_text(encoding="utf-8"))
        invalid["identity_status"] = "UNVERIFIABLE"
        invalid["drifts"] = [{"component": "data"}]
        self.benchmark_report.write_text(json.dumps(invalid), encoding="utf-8")
        fake_run = self.fake_runner()

        manifest = self.run_pair(fake_run)

        self.assertEqual(manifest["status"], "UNVERIFIABLE")
        self.assertEqual(fake_run.calls, [])
        manifest_path = self.output_dir / "paired_evolution_replay_manifest.json"
        self.assertTrue(manifest_path.is_file())
        self.assertFalse((self.output_dir / ".paired_evolution_replay_manifest.tmp").exists())
        self.assertTrue(
            any("benchmark" in item for item in manifest["mismatches"])
        )


if __name__ == "__main__":
    unittest.main()
