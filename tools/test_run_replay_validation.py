#!/usr/bin/env python3

import argparse
import csv
import importlib.util
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


def load_replay_module():
    module_path = pathlib.Path(__file__).with_name("run_replay_validation.py")
    spec = importlib.util.spec_from_file_location("run_replay_validation", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPLAY = load_replay_module()


class RunReplayValidationTest(unittest.TestCase):
    @staticmethod
    def _write_exact_replay_csv(
        path: pathlib.Path,
        *,
        symbol: str,
        start_timestamp_ms: int,
    ) -> dict:
        rows = [
            {
                "timestamp": start_timestamp_ms + index * 300_000,
                "symbol": symbol,
                "price": 100.0 + index,
                "volume": 10.0,
                "interval_ms": 300_000,
                "funding_rate_per_interval": 0.0,
                "execution_enabled": 1,
            }
            for index in range(2)
        ]
        with path.open("w", encoding="utf-8", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        event_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        segment = REPLAY.ReplaySegment(
            start_index=0,
            end_index=1,
            start_timestamp=rows[0]["timestamp"],
            end_timestamp=rows[-1]["timestamp"],
            bars=2,
        )
        identity = REPLAY.replay_segment_identity(
            symbol=symbol,
            target_bucket="trend",
            base_interval_ms=300_000,
            segment=segment,
            replay_csv_sha256=event_sha256,
        )
        return {
            "symbol": symbol,
            "start_timestamp_ms": segment.start_timestamp,
            "end_timestamp_ms": segment.end_timestamp,
            "event_sha256": event_sha256,
            "segment_identity_sha256": identity["sha256"],
            "replay_csv": path.name,
        }

    @staticmethod
    def _write_exact_plan(path: pathlib.Path, blocks: list[dict]) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": "exact_replay_block_plan_v1",
                    "benchmark_id": "c" * 64,
                    "target_bucket": "trend",
                    "blocks": blocks,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _write_exact_v2_plan(path: pathlib.Path, blocks: list[dict]) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": "exact_replay_block_plan_v2",
                    "benchmark_id": "d" * 64,
                    "target_bucket": "multi",
                    "blocks": blocks,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _exact_main_argv(
        *,
        plan: pathlib.Path,
        output_dir: pathlib.Path,
        extra: list[str] | None = None,
    ) -> list[str]:
        root = pathlib.Path(__file__).resolve().parent.parent
        return [
            "run_replay_validation.py",
            "--exact-block-plan",
            str(plan),
            "--base_config",
            str(root / "config" / "bybit.replay.assess.maker_first.yaml"),
            "--trade_bot",
            str(pathlib.Path(__file__)),
            "--output_dir",
            str(output_dir),
            "--assess_stage",
            "DEPLOY",
            "--min_runtime_status",
            "0",
            *(extra or []),
        ]

    def test_exact_block_plan_is_read_only_ordered_and_runs_every_block_once(self):
        episode_by_segment = {}
        trade_commands = []

        def fake_trade(command, output_path):
            trade_commands.append(command)
            output_path.write_text("runtime\n", encoding="utf-8")
            return 0

        def fake_assess(command, check=False):
            self.assertFalse(check)
            segment_sha = command[
                command.index("--segment-identity-sha256") + 1
            ]
            evidence = {
                "schema_version": "episode_execution_evidence_v1",
                "execution_path_complete": True,
                "episodes": [{"evaluator_episode_id": segment_sha}],
            }
            episode_by_segment[segment_sha] = evidence
            output = pathlib.Path(command[command.index("--json_out") + 1])
            output.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "episode_execution_evidence": evidence,
                        "metrics": {},
                    }
                ),
                encoding="utf-8",
            )
            return mock.Mock(returncode=0)

        forbidden = (
            "find_segments",
            "quantile",
            "derive_regime_thresholds",
            "load_feature_rows",
            "rank_replay_segments",
            "select_replay_segments",
            "write_corpus_manifest",
            "claim_final_holdouts",
            "has_met_replay_coverage_targets",
        )
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            blocks = []
            for block_id, symbol, start in (
                ("block-02", "ETHUSDT", 1_700_100_000_000),
                ("block-01", "BTCUSDT", 1_700_000_000_000),
            ):
                csv_path = tmp / f"{block_id}.csv"
                block = self._write_exact_replay_csv(
                    csv_path,
                    symbol=symbol,
                    start_timestamp_ms=start,
                )
                blocks.append({"block_id": block_id, **block})
            plan_path = tmp / "exact_plan.json"
            self._write_exact_plan(plan_path, blocks)
            plan_before = plan_path.read_bytes()
            csv_before = {
                block["block_id"]: (tmp / block["replay_csv"]).read_bytes()
                for block in blocks
            }
            output_dir = tmp / "output"
            patches = [
                mock.patch.object(
                    REPLAY,
                    name,
                    side_effect=AssertionError(f"exact mode called {name}"),
                )
                for name in forbidden
            ]
            with mock.patch.object(
                REPLAY, "run_command", side_effect=fake_trade
            ), mock.patch.object(
                REPLAY.subprocess, "run", side_effect=fake_assess
            ), mock.patch.object(sys, "argv", self._exact_main_argv(
                plan=plan_path,
                output_dir=output_dir,
            )):
                for patcher in patches:
                    patcher.start()
                try:
                    return_code = REPLAY.main()
                finally:
                    for patcher in reversed(patches):
                        patcher.stop()

            report = json.loads(
                (output_dir / "replay_validation_report.json").read_text(
                    encoding="utf-8"
                )
            )
            plan_after = plan_path.read_bytes()
            csv_after = {
                block["block_id"]: (tmp / block["replay_csv"]).read_bytes()
                for block in blocks
            }

        self.assertEqual(return_code, 0)
        self.assertEqual(report["schema_version"], "exact_replay_block_audit_v1")
        self.assertEqual(report["status"], "VERIFIED")
        self.assertTrue(report["selection_bypassed"])
        self.assertTrue(report["exact_block_plan"]["read_only"])
        self.assertEqual(report["planned_block_count"], 2)
        self.assertEqual(report["executed_block_count"], 2)
        self.assertEqual(
            [item["block_id"] for item in report["blocks"]],
            ["block-02", "block-01"],
        )
        self.assertEqual(len(trade_commands), 2)
        self.assertEqual(
            [item["command"] for item in report["blocks"]],
            trade_commands,
        )
        for planned, audit in zip(blocks, report["blocks"]):
            self.assertEqual(audit["execution_attempt_count"], 1)
            self.assertEqual(audit["trade_bot_exit_code"], 0)
            self.assertEqual(audit["assess_exit_code"], 0)
            self.assertEqual(
                audit["actual_event_sha256"], planned["event_sha256"]
            )
            self.assertEqual(
                audit["actual_segment_identity_sha256"],
                planned["segment_identity_sha256"],
            )
            self.assertEqual(
                audit["episode_execution_evidence"],
                episode_by_segment[planned["segment_identity_sha256"]],
            )
        self.assertEqual(plan_before, plan_after)
        for block in blocks:
            self.assertEqual(
                csv_before[block["block_id"]],
                csv_after[block["block_id"]],
            )

    def test_exact_block_plan_preflight_failures_still_emit_audit(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            replay_csv = tmp / "block.csv"
            valid_block = {
                "block_id": "block-01",
                **self._write_exact_replay_csv(
                    replay_csv,
                    symbol="BTCUSDT",
                    start_timestamp_ms=1_700_000_000_000,
                ),
            }
            cases = {
                "missing": [{key: value for key, value in valid_block.items() if key != "symbol"}],
                "duplicate": [valid_block, dict(valid_block)],
                "event_drift": [{**valid_block, "event_sha256": "0" * 64}],
                "interval_mismatch": [
                    {
                        **valid_block,
                        "end_timestamp_ms": valid_block["end_timestamp_ms"] + 1,
                    }
                ],
                "identity_mismatch": [
                    {**valid_block, "segment_identity_sha256": "f" * 64}
                ],
            }
            for name, blocks in cases.items():
                with self.subTest(name=name):
                    plan_path = tmp / f"{name}.json"
                    self._write_exact_plan(plan_path, blocks)
                    output_dir = tmp / f"output-{name}"
                    with mock.patch.object(
                        REPLAY,
                        "run_command",
                        side_effect=AssertionError("invalid plan executed"),
                    ), mock.patch.object(
                        sys,
                        "argv",
                        self._exact_main_argv(
                            plan=plan_path,
                            output_dir=output_dir,
                        ),
                    ):
                        return_code = REPLAY.main()
                    report = json.loads(
                        (output_dir / "replay_validation_report.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(return_code, 2)
                    self.assertEqual(report["status"], "UNVERIFIABLE")
                    self.assertEqual(report["executed_block_count"], 0)
                    self.assertTrue(report["validation_errors"])
                    self.assertEqual(len(report["blocks"]), len(blocks))

    def test_multi_execution_blocks_are_ordered_isolated_and_fully_covered(self):
        trade_commands = []

        def fake_trade(command, output_path):
            trade_commands.append(command)
            output_path.write_text("runtime\n", encoding="utf-8")
            return 0

        def fake_assess(command, check=False):
            self.assertFalse(check)
            segment_sha = command[
                command.index("--segment-identity-sha256") + 1
            ]
            output = pathlib.Path(command[command.index("--json_out") + 1])
            output.write_text(
                json.dumps(
                    {
                        "verdict": "PASS",
                        "episode_execution_evidence": {
                            "schema_version": "episode_execution_evidence_v1",
                            "segment_identity_sha256": segment_sha,
                            "execution_path_complete": True,
                            "episodes": [{"evaluator_episode_id": segment_sha}],
                        },
                        "metrics": {},
                    }
                ),
                encoding="utf-8",
            )
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            blocks = []
            for block_number, start in enumerate(
                (1_700_000_000_000, 1_700_100_000_000), start=1
            ):
                block_id = f"block-{block_number:02d}"
                executions = []
                for symbol, regimes in (
                    ("BTCUSDT", ["range", "trend"]),
                    ("ETHUSDT", ["defensive"]),
                ):
                    csv_path = tmp / f"{block_id}-{symbol}.csv"
                    execution = self._write_exact_replay_csv(
                        csv_path,
                        symbol=symbol,
                        start_timestamp_ms=start,
                    )
                    executions.append(
                        {
                            "execution_id": f"{block_id}:{symbol}",
                            "planned_entry_regimes": regimes,
                            "target_bucket": "trend",
                            **execution,
                        }
                    )
                blocks.append(
                    {
                        "block_id": block_id,
                        "start_timestamp_ms": start,
                        "end_timestamp_ms": start + 300_000,
                        "event_sha256": hashlib.sha256(
                            f"{block_id}:composite".encode("ascii")
                        ).hexdigest(),
                        "cells": [
                            {"symbol": "BTCUSDT", "entry_regime": "range"},
                            {"symbol": "BTCUSDT", "entry_regime": "trend"},
                            {"symbol": "ETHUSDT", "entry_regime": "defensive"},
                        ],
                        "executions": executions,
                    }
                )
            plan_path = tmp / "multi-plan.json"
            self._write_exact_v2_plan(plan_path, blocks)
            output_dir = tmp / "output"
            with mock.patch.object(
                REPLAY, "run_command", side_effect=fake_trade
            ), mock.patch.object(
                REPLAY.subprocess, "run", side_effect=fake_assess
            ), mock.patch.object(
                sys,
                "argv",
                self._exact_main_argv(plan=plan_path, output_dir=output_dir),
            ):
                return_code = REPLAY.main()
            report = json.loads(
                (output_dir / "replay_validation_report.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(report["planned_block_count"], 2)
        self.assertEqual(report["executed_block_count"], 2)
        self.assertEqual(report["planned_execution_count"], 4)
        self.assertEqual(report["executed_execution_count"], 4)
        self.assertEqual(
            [block["block_id"] for block in report["blocks"]],
            ["block-01", "block-02"],
        )
        self.assertEqual(
            [
                execution["execution_id"]
                for block in report["blocks"]
                for execution in block["executions"]
            ],
            [
                "block-01:BTCUSDT",
                "block-01:ETHUSDT",
                "block-02:BTCUSDT",
                "block-02:ETHUSDT",
            ],
        )
        self.assertEqual(len(trade_commands), 4)
        state_dirs = [
            execution["state_dir"]
            for block in report["blocks"]
            for execution in block["executions"]
        ]
        self.assertEqual(len(state_dirs), len(set(state_dirs)))
        self.assertTrue(
            all(execution["execution_attempt_count"] == 1 for block in report["blocks"] for execution in block["executions"])
        )

    def test_multi_execution_preflight_rejects_coverage_regime_and_identity_drift(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            executions = []
            for symbol, regimes in (
                ("BTCUSDT", ["trend"]),
                ("ETHUSDT", ["defensive"]),
            ):
                execution = self._write_exact_replay_csv(
                    tmp / f"{symbol}.csv",
                    symbol=symbol,
                    start_timestamp_ms=1_700_000_000_000,
                )
                executions.append(
                    {
                        "execution_id": f"block-01:{symbol}",
                        "planned_entry_regimes": regimes,
                        "target_bucket": "trend",
                        **execution,
                    }
                )
            base_block = {
                "block_id": "block-01",
                "start_timestamp_ms": 1_700_000_000_000,
                "end_timestamp_ms": 1_700_000_300_000,
                "event_sha256": "a" * 64,
                "cells": [
                    {"symbol": "BTCUSDT", "entry_regime": "trend"},
                    {"symbol": "ETHUSDT", "entry_regime": "defensive"},
                ],
                "executions": executions,
            }
            cases = {
                "duplicate": ({
                    **base_block,
                    "executions": [executions[0], dict(executions[0])],
                }, "duplicate_execution_id"),
                "missing_coverage": ({
                    **base_block,
                    "executions": [executions[0]],
                }, "cell_coverage_mismatch"),
                "extra_regime": ({
                    **base_block,
                    "executions": [
                        {**executions[0], "planned_entry_regimes": ["trend", "oracle"]},
                        executions[1],
                    ],
                }, "planned_entry_regimes_mismatch"),
                "event_drift": ({
                    **base_block,
                    "executions": [
                        {**executions[0], "event_sha256": "0" * 64},
                        executions[1],
                    ],
                }, "event_sha256_mismatch"),
            }
            for name, (block, expected_error) in cases.items():
                with self.subTest(name=name):
                    plan_path = tmp / f"{name}.json"
                    self._write_exact_v2_plan(plan_path, [block])
                    _, audits, prepared, errors = REPLAY.preflight_exact_block_plan(
                        plan_path,
                        fallback_target_bucket="trend",
                    )
                    self.assertEqual(len(audits), 1)
                    self.assertEqual(prepared, [])
                    self.assertTrue(errors)
                    self.assertTrue(
                        any(expected_error in error for error in errors), errors
                    )

    def test_exact_block_execution_failure_still_emits_command_and_evidence(self):
        def fake_trade(command, output_path):
            output_path.write_text("runtime failed\n", encoding="utf-8")
            return 9

        def fake_assess(command, check=False):
            self.assertFalse(check)
            output = pathlib.Path(command[command.index("--json_out") + 1])
            output.write_text(
                json.dumps(
                    {
                        "verdict": "FAIL",
                        "episode_execution_evidence": {
                            "schema_version": "episode_execution_evidence_v1",
                            "execution_path_complete": False,
                            "episodes": [],
                        },
                        "metrics": {},
                    }
                ),
                encoding="utf-8",
            )
            return mock.Mock(returncode=1)

        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            replay_csv = tmp / "block.csv"
            block = {
                "block_id": "block-01",
                **self._write_exact_replay_csv(
                    replay_csv,
                    symbol="BTCUSDT",
                    start_timestamp_ms=1_700_000_000_000,
                ),
            }
            plan_path = tmp / "plan.json"
            self._write_exact_plan(plan_path, [block])
            output_dir = tmp / "output"
            with mock.patch.object(
                REPLAY, "run_command", side_effect=fake_trade
            ), mock.patch.object(
                REPLAY.subprocess, "run", side_effect=fake_assess
            ), mock.patch.object(
                sys,
                "argv",
                self._exact_main_argv(
                    plan=plan_path,
                    output_dir=output_dir,
                ),
            ):
                return_code = REPLAY.main()
            report = json.loads(
                (output_dir / "replay_validation_report.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(return_code, 2)
        self.assertEqual(report["status"], "UNVERIFIABLE")
        self.assertEqual(report["executed_block_count"], 1)
        audit = report["blocks"][0]
        self.assertTrue(audit["command"])
        self.assertEqual(audit["trade_bot_exit_code"], 9)
        self.assertEqual(audit["execution_attempt_count"], 1)
        self.assertEqual(audit["assess_exit_code"], 1)
        self.assertEqual(
            audit["episode_execution_evidence"]["schema_version"],
            "episode_execution_evidence_v1",
        )
        self.assertIn("trade_bot_exit_nonzero", audit["errors"])
        self.assertIn("assess_exit_nonzero", audit["errors"])

    def test_exact_block_plan_rejects_selection_and_holdout_arguments_without_io(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            replay_csv = tmp / "block.csv"
            block = {
                "block_id": "block-01",
                **self._write_exact_replay_csv(
                    replay_csv,
                    symbol="BTCUSDT",
                    start_timestamp_ms=1_700_000_000_000,
                ),
            }
            plan_path = tmp / "plan.json"
            self._write_exact_plan(plan_path, [block])
            output_dir = tmp / "output"
            forbidden_paths = {
                "selection": tmp / "selection.json",
                "selection_feature": tmp / "selection.csv",
                "selection_feature_map": tmp / "selection-map.csv",
                "feature_map": tmp / "feature-map.csv",
                "holdout": tmp / "holdout.jsonl",
                "corpus": tmp / "corpus.json",
            }
            extra = [
                "--feature_csv_by_symbol",
                f"BTCUSDT={forbidden_paths['feature_map']}",
                "--selection_feature_csv",
                str(forbidden_paths["selection_feature"]),
                "--selection_feature_csv_by_symbol",
                f"BTCUSDT={forbidden_paths['selection_feature_map']}",
                "--prevalidated_selection_report",
                str(forbidden_paths["selection"]),
                "--require_candidate_identity",
                "--holdout_ledger",
                str(forbidden_paths["holdout"]),
                "--experiment_id",
                "experiment-1",
                "--corpus_manifest",
                str(forbidden_paths["corpus"]),
            ]
            with mock.patch.object(
                REPLAY,
                "run_command",
                side_effect=AssertionError("conflicting exact plan executed"),
            ), mock.patch.object(
                REPLAY,
                "parse_feature_csv_by_symbol",
                side_effect=AssertionError("exact mode parsed feature mapping"),
            ), mock.patch.object(
                REPLAY,
                "validate_prevalidated_selection_report",
                side_effect=AssertionError("exact mode read selection report"),
            ), mock.patch.object(
                REPLAY,
                "load_corpus_manifest",
                side_effect=AssertionError("exact mode read corpus manifest"),
            ), mock.patch.object(
                REPLAY,
                "write_corpus_manifest",
                side_effect=AssertionError("exact mode wrote corpus manifest"),
            ), mock.patch.object(
                REPLAY,
                "claim_final_holdouts",
                side_effect=AssertionError("exact mode touched holdout ledger"),
            ), mock.patch.object(
                sys,
                "argv",
                self._exact_main_argv(
                    plan=plan_path,
                    output_dir=output_dir,
                    extra=extra,
                ),
            ):
                return_code = REPLAY.main()
            report = json.loads(
                (output_dir / "replay_validation_report.json").read_text(
                    encoding="utf-8"
                )
            )
            forbidden_path_exists = {
                name: path.exists() for name, path in forbidden_paths.items()
            }
            selection_manifest_exists = (
                output_dir / "selection_candidate_manifest.json"
            ).exists()
            optimization_report_exists = (
                output_dir / "replay_optimization_report.json"
            ).exists()

        self.assertEqual(return_code, 2)
        self.assertEqual(report["status"], "UNVERIFIABLE")
        self.assertEqual(report["executed_block_count"], 0)
        self.assertTrue(
            any("mutually_exclusive" in item for item in report["validation_errors"])
        )
        self.assertFalse(any(forbidden_path_exists.values()))
        self.assertFalse(selection_manifest_exists)
        self.assertFalse(optimization_report_exists)

    def test_summary_passes_episode_evidence_through_without_aggregate_synthesis(self):
        episode_evidence = {
            "schema_version": "episode_execution_evidence_v1",
            "episodes": [{"evaluator_episode_id": "episode-1"}],
            "execution_path_complete": True,
        }
        summary = REPLAY.summarize_assess(
            {
                "metrics": {
                    "account_realized_net_usd": 99.0,
                    "self_evolution_update_count": 7,
                },
                "episode_execution_evidence": episode_evidence,
            }
        )
        self.assertIs(summary["episode_execution_evidence"], episode_evidence)

        aggregate_only = REPLAY.summarize_assess(
            {
                "metrics": {
                    "account_realized_net_usd": 99.0,
                    "self_evolution_update_count": 7,
                }
            }
        )
        self.assertIsNone(aggregate_only["episode_execution_evidence"])

    def test_run_identity_and_episode_evidence_are_emitted_for_every_segment(self):
        rows = [
            REPLAY.FeatureRow(
                timestamp=1_700_000_000_000 + idx * 300_000,
                open=100.0 + idx,
                high=101.0 + idx,
                low=99.0 + idx,
                close=100.5 + idx,
                volume=10.0,
                features={name: 0.0 for name in REPLAY.FEATURE_COLUMNS},
            )
            for idx in range(4)
        ]
        segments = [
            REPLAY.ReplaySegment(
                start_index=index,
                end_index=index + 1,
                start_timestamp=rows[index].timestamp,
                end_timestamp=rows[index + 1].timestamp,
                bars=2,
            )
            for index in (0, 2)
        ]
        thresholds = REPLAY.RegimeThresholds(0.01, 0.01, 0.02, 0.02)
        policy_identity = {
            "schema_version": "execution_policy_v2",
            "sha256": "a" * 64,
            "policy": {"execution.slippage_bps": 2.0},
        }
        episode_evidence = {
            "schema_version": "episode_execution_evidence_v1",
            "episodes": [{"evaluator_episode_id": "episode-1"}],
            "execution_path_complete": True,
        }
        assess_payload = {
            "verdict": "PASS",
            "runtime_validation_mode": "EXECUTION_ACTIVE",
            "execution_status": "PASS",
            "episode_execution_evidence": episode_evidence,
            "metrics": {
                "execution_activity_count": 1,
                "funnel_fills_runtime_count": 1,
                "execution_attribution_fill_count": 1,
                "execution_attribution_quality_fill_count": 1,
                "execution_attribution_fee_usd": 0.01,
                "replay_terminal_settlement_done_count": 1,
                "replay_terminal_settlement_failed_count": 0,
                "replay_terminal_realized_net_usd": 0.02,
                "replay_terminal_fee_usd": 0.01,
                "replay_terminal_funding_paid_usd": 0.0,
            },
        }
        assess_commands = []

        def fake_trade(command, output_path):
            output_path.write_text("runtime\n", encoding="utf-8")
            return 0

        def fake_assess(command, check=False):
            self.assertFalse(check)
            assess_commands.append(command)
            output = pathlib.Path(command[command.index("--json_out") + 1])
            output.write_text(json.dumps(assess_payload), encoding="utf-8")
            return mock.Mock(returncode=0)

        root = pathlib.Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            REPLAY, "run_command", side_effect=fake_trade
        ), mock.patch.object(
            REPLAY.subprocess, "run", side_effect=fake_assess
        ), mock.patch.object(
            REPLAY, "has_met_replay_coverage_targets", return_value=True
        ):
            runs, selection, _, _, _ = REPLAY.run_replay_for_symbol(
                symbol="BTCUSDT",
                output_dir=pathlib.Path(td),
                rows=rows,
                thresholds=thresholds,
                selected_segments=segments,
                target_bucket="trend",
                base_interval_ms=300_000,
                root=root,
                base_config=root / "config" / "bybit.replay.assess.maker_first.yaml",
                trade_bot=pathlib.Path(__file__),
                assess_stage="DEPLOY",
                min_runtime_status=0,
                min_execution_active_runs=1,
                min_execution_pass_runs=1,
                min_total_fills=1,
                min_mean_realized_net_per_fill=0.0,
                min_break_even_fee_multiplier=1.25,
                warn_mean_filtered_cost_ratio=0.8,
                force_all_frozen_segments=True,
                execution_policy_identity=policy_identity,
                trade_bot_sha256="b" * 64,
            )
            self.assertEqual(len(runs), 2)
            self.assertFalse(selection["stopped_early"])
            for run, command in zip(runs, assess_commands):
                replay_path = pathlib.Path(run["replay_csv"])
                replay_sha = hashlib.sha256(replay_path.read_bytes()).hexdigest()
                identity_payload = {
                    "schema_version": "replay_segment_identity_v1",
                    "symbol": run["symbol"],
                    "target_bucket": "trend",
                    "base_interval_ms": 300_000,
                    "start_timestamp_ms": run["segment"]["start_timestamp"],
                    "end_timestamp_ms": run["segment"]["end_timestamp"],
                    "bars": run["segment"]["bars"],
                    "replay_csv_sha256": replay_sha,
                }
                expected_segment_sha = hashlib.sha256(
                    json.dumps(
                        identity_payload,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                self.assertEqual(run["replay_csv_sha256"], replay_sha)
                self.assertEqual(
                    run["segment_identity_sha256"], expected_segment_sha
                )
                self.assertEqual(run["execution_policy_identity"], policy_identity)
                self.assertEqual(run["trade_bot_sha256"], "b" * 64)
                self.assertEqual(run["episode_execution_evidence"], episode_evidence)
                self.assertIn("--segment-identity-sha256", command)
                self.assertIn("--execution-policy-identity-json", command)

    def test_default_replay_still_stops_after_recommended_coverage(self):
        self.assertTrue(REPLAY.should_stop_after_coverage(True, False))
        self.assertFalse(REPLAY.should_stop_after_coverage(True, True))
        self.assertFalse(REPLAY.should_stop_after_coverage(False, False))

    @staticmethod
    def _complete_replay_summary(summary):
        fill_count = int(summary.get("funnel_fills_runtime_count") or 0)
        realized_net_per_fill = float(summary.get("realized_net_per_fill") or 0.0)
        fee_usd = float(summary.get("execution_attribution_fee_usd") or 0.0)
        summary.update(
            {
                "execution_attribution_fill_count": fill_count,
                "execution_attribution_quality_fill_count": fill_count,
                "replay_terminal_settlement_done_count": 1,
                "replay_terminal_settlement_failed_count": 0,
                "replay_terminal_realized_net_usd": (
                    realized_net_per_fill * fill_count
                ),
                "replay_terminal_fee_usd": fee_usd,
                "replay_terminal_funding_paid_usd": 0.0,
            }
        )
        return summary

    def test_economic_objective_contract_hash_binds_thresholds(self):
        base_args = {
            "assess_stage": "S5",
            "min_runtime_status": 30,
            "min_execution_active_runs": 3,
            "min_execution_pass_runs": 3,
            "min_total_fills": 20,
            "min_mean_realized_net_per_fill": 0.0,
            "min_break_even_fee_multiplier": 1.25,
            "warn_mean_filtered_cost_ratio": 0.8,
            "min_tradable_symbols": 2,
            "target_bucket": "trend",
            "max_segments": 12,
            "min_segment_bars": 96,
        }
        first = REPLAY.build_economic_objective_contract(
            argparse.Namespace(**base_args),
            root=pathlib.Path(__file__).resolve().parent.parent,
            execution_policy_sha256="a" * 64,
            trade_bot_sha256="b" * 64,
        )
        second = REPLAY.build_economic_objective_contract(
            argparse.Namespace(
                **{
                    **base_args,
                    "min_mean_realized_net_per_fill": 0.01,
                }
            ),
            root=pathlib.Path(__file__).resolve().parent.parent,
            execution_policy_sha256="a" * 64,
            trade_bot_sha256="b" * 64,
        )

        self.assertEqual(
            first["primary_metric"], "mean_realized_net_per_fill"
        )
        self.assertEqual(
            first["accounting_source"], "replay_terminal_account_state"
        )
        self.assertEqual(
            first["fill_count_source"], "all_fill_applied_events_current_boot"
        )
        self.assertTrue(first["terminal_settlement_evidence_required"])
        self.assertEqual(first["incomplete_economics_policy"], "hard_fail")
        self.assertEqual(
            first["state_isolation_policy"], "fresh_wal_per_symbol_segment"
        )
        self.assertEqual(first["segment_sampling"]["warmup_context_bars"], 96)
        self.assertTrue(
            first["segment_sampling"]["warmup_context_execution_disabled"]
        )
        self.assertTrue(first["selection_and_final_share_contract"])
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_baseline_candidate_identity_binds_binary_config_and_objective(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        args = argparse.Namespace(
            assess_stage="S3",
            min_runtime_status=1,
            min_execution_active_runs=3,
            min_execution_pass_runs=3,
            min_total_fills=20,
            min_mean_realized_net_per_fill=0.0,
            min_break_even_fee_multiplier=1.25,
            warn_mean_filtered_cost_ratio=0.8,
            min_tradable_symbols=1,
            target_bucket="trend",
            max_segments=16,
            min_segment_bars=40,
        )
        identity = REPLAY.build_baseline_candidate_identity(
            args,
            root=root,
            base_config=root / "config" / "bybit.replay.assess.maker_first.yaml",
            trade_bot=pathlib.Path(__file__),
        )

        self.assertEqual(identity["candidate_type"], "baseline_runtime_v1")
        self.assertFalse(identity["integrator_model_required"])
        self.assertEqual(len(identity["trade_bot_sha256"]), 64)
        self.assertEqual(len(identity["identity_sha256"]), 64)

    def test_prevalidated_selection_report_binds_identity_data_and_corpus(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            selection_csv = root / "selection.csv"
            selection_csv.write_text(
                "timestamp,close,volume\n1000,100,1\n",
                encoding="utf-8",
            )
            final_csv = root / "final.csv"
            identity = {
                "candidate_type": "baseline_runtime_v1",
                "trade_bot_sha256": "b" * 64,
            }
            identity["identity_sha256"] = hashlib.sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            corpus_binding = {
                "schema_version": "frozen_replay_corpus_binding_v1",
                "per_symbol": {"SOLUSDT": {"sha256": "c" * 64}},
                "binding_sha256": "d" * 64,
            }
            report = {
                "status": "pass",
                "activation_gate": {"status": "pass"},
                "candidate_identity": identity,
                "target_bucket": "trend",
                "symbols": ["SOLUSDT"],
                "real_market_replay": True,
                "execution_evidence_contract": {
                    "schema_version": "replay_execution_prescreen_v1"
                },
                "frozen_corpus_binding": corpus_binding,
                "symbol_reports": {
                    "SOLUSDT": {
                        "feature_csv": str(selection_csv),
                        "feature_sha256": hashlib.sha256(
                            selection_csv.read_bytes()
                        ).hexdigest(),
                        "aggregate_validation": {"status": "pass"},
                    }
                },
                "holdout_consumption": {
                    "ledger_path": "",
                    "claimed_before_evaluation": False,
                },
                "runs": [],
            }
            report_path = root / "selection_report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")

            binding = REPLAY.validate_prevalidated_selection_report(
                report_path,
                candidate_identity=identity,
                symbols=["SOLUSDT"],
                selection_feature_csv=selection_csv,
                selection_feature_csv_by_symbol={},
                final_feature_csv=final_csv,
                final_feature_csv_by_symbol={},
                frozen_corpus_binding=corpus_binding,
                target_bucket="trend",
            )

            self.assertEqual(binding["status"], "pass")
            self.assertEqual(
                binding["candidate_identity_sha256"],
                identity["identity_sha256"],
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "frozen corpus binding mismatch",
            ):
                REPLAY.validate_prevalidated_selection_report(
                    report_path,
                    candidate_identity=identity,
                    symbols=["SOLUSDT"],
                    selection_feature_csv=selection_csv,
                    selection_feature_csv_by_symbol={},
                    final_feature_csv=final_csv,
                    final_feature_csv_by_symbol={},
                    frozen_corpus_binding={**corpus_binding, "binding_sha256": "e" * 64},
                    target_bucket="trend",
                )

    def test_replay_state_directory_is_fresh_and_non_reusable(self):
        with tempfile.TemporaryDirectory() as td:
            segment_dir = pathlib.Path(td) / "segment_01"
            segment_dir.mkdir()

            state_dir = REPLAY.create_fresh_replay_state_dir(segment_dir)

            self.assertTrue(state_dir.is_dir())
            with self.assertRaisesRegex(RuntimeError, "refusing WAL reuse"):
                REPLAY.create_fresh_replay_state_dir(segment_dir)

    def test_final_holdout_uses_frozen_manifest_order_without_future_ranking(self):
        rows = [
            REPLAY.FeatureRow(
                timestamp=1_700_000_000_000 + idx * 300_000,
                close=(100.0, 101.0, 102.0, 180.0, 181.0, 182.0)[idx],
                volume=1000.0,
                features={
                    "ema_diff": 0.0 if idx == 2 else 0.01,
                    "zscore_48": 0.0,
                    "mom_12": 0.01,
                    "mom_48": 0.0 if idx == 2 else 0.02,
                    "ret_1": 0.0,
                    "range_pct": 0.001,
                    "vol_12": 0.001,
                },
            )
            for idx in range(6)
        ]
        thresholds = REPLAY.RegimeThresholds(
            trend_abs_ema_diff=0.005,
            trend_abs_mom_48=0.01,
            extreme_vol_12=0.01,
            extreme_range_pct=0.01,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            feature_csv = tmp_path / "feature_store_5m.csv"
            feature_csv.write_text("timestamp,close,volume\n", encoding="utf-8")
            selection_feature_csv = tmp_path / "selection_feature_store_5m.csv"
            selection_feature_csv.write_text(
                "timestamp,close,volume\n1,100,1\n",
                encoding="utf-8",
            )
            corpus_manifest = tmp_path / "replay_validation_trend_corpus_SOLUSDT.json"
            corpus_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "replay_selection_manifest_v3",
                        "evidence_domain": "selection_validation",
                        "candidate_set_frozen": True,
                        "symbol": "SOLUSDT",
                        "source_feature_csv": str(selection_feature_csv),
                        "source_feature_sha256": hashlib.sha256(
                            selection_feature_csv.read_bytes()
                        ).hexdigest(),
                        "target_bucket": "trend",
                        "base_interval_ms": 300_000,
                        "thresholds": {
                            "trend_abs_ema_diff": 0.005,
                            "trend_abs_mom_48": 0.01,
                            "extreme_vol_12": 0.01,
                            "extreme_range_pct": 0.01,
                        },
                        "selection_policy": (
                            "chronological_quantiles_without_outcome_v2"
                        ),
                        "threshold_policy": {
                            "trend_quantile": 0.50,
                            "extreme_quantile": 0.90,
                            "fit_domain": "selection_validation",
                            "holdout_refit_forbidden": True,
                        },
                        "sampling_quantiles": [0.0, 1.0],
                    }
                ),
                encoding="utf-8",
            )
            manifest_before = corpus_manifest.read_text(encoding="utf-8")

            with mock.patch.object(
                REPLAY,
                "rank_replay_segments",
                side_effect=AssertionError("final holdout must not rank future paths"),
            ):
                selected, eligible, selection, warnings = (
                    REPLAY.select_replay_segments(
                        rows,
                        thresholds,
                        feature_csv=feature_csv,
                        target_bucket="trend",
                        base_interval_ms=300_000,
                        max_segments=1,
                        min_segment_bars=2,
                        corpus_manifest=corpus_manifest,
                        refresh_corpus_manifest=False,
                        final_holdout=True,
                        symbol="SOLUSDT",
                    )
                )

            self.assertEqual(warnings, [])
            self.assertEqual(len(eligible), 2)
            self.assertEqual(len(selected), 2)
            self.assertEqual(selected[0].start_timestamp, rows[0].timestamp)
            self.assertEqual(selected[1].start_timestamp, rows[3].timestamp)
            self.assertTrue(selection["corpus_loaded"])
            self.assertFalse(selection["corpus_written"])
            self.assertFalse(selection["corpus_refreshed"])
            self.assertEqual(selection["selection_mode"], "selection_manifest_holdout")
            self.assertFalse(
                selection["max_segments_ignored_for_frozen_candidate_set"]
            )
            self.assertEqual(
                corpus_manifest.read_text(encoding="utf-8"),
                manifest_before,
            )

    def test_execution_optimizer_fails_when_all_filled_segments_are_net_negative(self):
        run_summaries = [
            {
                "symbol": "BTCUSDT",
                "segment_index": 1,
                "segment": {
                    "bars": 40,
                    "strength_score": 3.0,
                    "liquidity_score": 1.5,
                    "avg_range_pct": 0.001,
                    "avg_vol_12": 0.001,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 2,
                    "execution_activity_count": 2,
                    "realized_net_per_fill": -0.02,
                    "filtered_cost_ratio_avg": 0.2,
                    "execution_attribution_fee_usd": 0.01,
                },
            },
            {
                "symbol": "ETHUSDT",
                "segment_index": 2,
                "segment": {
                    "bars": 40,
                    "strength_score": 2.0,
                    "liquidity_score": 1.0,
                    "avg_range_pct": 0.002,
                    "avg_vol_12": 0.0015,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 1,
                    "execution_activity_count": 1,
                    "realized_net_per_fill": -0.01,
                    "filtered_cost_ratio_avg": 0.3,
                    "execution_attribution_fee_usd": 0.005,
                },
            },
        ]
        for run in run_summaries:
            self._complete_replay_summary(run["assess_summary"])
            run["economics_attribution"] = REPLAY.build_run_economics_attribution(run)

        report = REPLAY.build_replay_economics_report(
            run_summaries,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            min_break_even_fee_multiplier=1.25,
        )

        self.assertEqual(report["optimizer"]["status"], "fail")
        self.assertIn(
            "no_deployable_prefilter_candidate_positive_after_costs",
            report["optimizer"]["fail_reasons"],
        )
        self.assertIn(
            "all_filled_segments_net_negative",
            report["attribution_summary"]["diagnostics"],
        )
        self.assertEqual(report["cost_sensitivity"]["status"], "fail")
        self.assertIn(
            "no_cost_sensitivity_scenario_positive",
            report["cost_sensitivity"]["diagnostics"],
        )

    def test_execution_optimizer_passes_when_prefilter_candidate_is_positive(self):
        run_summaries = [
            {
                "symbol": "BTCUSDT",
                "segment_index": 1,
                "segment": {
                    "bars": 40,
                    "strength_score": 4.0,
                    "liquidity_score": 2.0,
                    "avg_range_pct": 0.001,
                    "avg_vol_12": 0.001,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 2,
                    "execution_activity_count": 2,
                    "realized_net_per_fill": 0.03,
                    "filtered_cost_ratio_avg": 0.1,
                    "execution_attribution_fee_usd": 0.01,
                },
            },
            {
                "symbol": "ETHUSDT",
                "segment_index": 2,
                "segment": {
                    "bars": 40,
                    "strength_score": 1.0,
                    "liquidity_score": 1.0,
                    "avg_range_pct": 0.002,
                    "avg_vol_12": 0.002,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 1,
                    "execution_activity_count": 1,
                    "realized_net_per_fill": -0.01,
                    "filtered_cost_ratio_avg": 0.3,
                    "execution_attribution_fee_usd": 0.005,
                },
            },
        ]
        for run in run_summaries:
            self._complete_replay_summary(run["assess_summary"])
            run["economics_attribution"] = REPLAY.build_run_economics_attribution(run)

        report = REPLAY.build_replay_economics_report(
            run_summaries,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            min_break_even_fee_multiplier=1.25,
        )

        self.assertEqual(report["optimizer"]["status"], "pass")
        self.assertGreaterEqual(report["optimizer"]["pass_candidate_count"], 1)

    def test_execution_optimizer_blocks_positive_net_without_fee_safety_margin(self):
        run_summaries = [
            {
                "symbol": "SOLUSDT",
                "segment_index": 1,
                "segment": {
                    "bars": 40,
                    "strength_score": 4.0,
                    "liquidity_score": 2.0,
                    "avg_range_pct": 0.001,
                    "avg_vol_12": 0.001,
                },
                "assess_summary": {
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "execution_status": "PASS",
                    "funnel_fills_runtime_count": 1,
                    "execution_activity_count": 1,
                    "realized_net_per_fill": 0.01,
                    "filtered_cost_ratio_avg": 0.1,
                    "execution_attribution_fee_usd": 0.10,
                },
            },
        ]
        for run in run_summaries:
            self._complete_replay_summary(run["assess_summary"])
            run["economics_attribution"] = REPLAY.build_run_economics_attribution(run)

        report = REPLAY.build_replay_economics_report(
            run_summaries,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            min_break_even_fee_multiplier=1.25,
        )

        self.assertEqual(report["optimizer"]["status"], "fail")
        best = report["optimizer"]["best_deployable_candidate"]
        self.assertIsNotNone(best)
        self.assertAlmostEqual(best["break_even_fee_multiplier"], 1.1)
        self.assertTrue(
            any(
                "break_even_fee_multiplier" in reason
                for reason in best["fail_reasons"]
            )
        )
        self.assertEqual(best["cost_stress"]["status"], "fail")

    def test_activation_gate_optimizer_cannot_override_aggregate_failure(self):
        selected_candidate = {
            "name": "strong_liquid_q50",
            "diagnostic_only": False,
            "status": "pass",
            "aggregate_summary": {
                "median_realized_net_per_fill_with_fills": 0.002,
                "positive_filled_segment_ratio": 0.70,
                "total_fills": 24,
            },
        }
        activation = REPLAY.build_activation_gate_report(
            aggregate_validation={
                "status": "fail",
                "fail_reasons": [
                    "median_realized_net_per_fill_with_fills=-0.001 < 0.000"
                ],
                "warn_reasons": [],
            },
            economics_report={
                "optimizer": {
                    "status": "pass",
                    "best_deployable_candidate": selected_candidate,
                },
                "execution_cost_plan": {"status": "pass"},
            },
            symbol_reports={},
            source_symbol="SOLUSDT",
        )

        self.assertEqual(activation["status"], "fail")
        self.assertEqual(activation["basis"], "aggregate_validation")
        self.assertEqual(activation["selected_candidate"]["name"], "strong_liquid_q50")
        self.assertIn(
            "median_realized_net_per_fill_with_fills=-0.001 < 0.000",
            activation["fail_reasons"],
        )

    def test_activation_gate_baseline_candidate_has_no_false_diagnostic_warning(self):
        candidate = {
            "name": "baseline_all",
            "diagnostic_only": False,
            "status": "pass",
            "deployable_config": {"requires_rerun": False},
        }
        activation = REPLAY.build_activation_gate_report(
            aggregate_validation={"status": "pass", "fail_reasons": []},
            economics_report={
                "optimizer": {
                    "status": "pass",
                    "best_deployable_candidate": candidate,
                },
                "execution_cost_plan": {"status": "pass"},
            },
            symbol_reports={},
            source_symbol="",
        )

        self.assertEqual(activation["status"], "pass")
        self.assertEqual(activation["warn_reasons"], [])
        self.assertEqual(activation["selected_candidate"]["name"], "baseline_all")

    def test_aggregate_run_summaries_fails_when_mean_masks_negative_median(self):
        runs = []
        for realized_net in (-0.002, -0.001, -0.001, 0.020):
            assess_summary = self._complete_replay_summary(
                {
                        "verdict": "PASS",
                        "runtime_validation_mode": "EXECUTION_ACTIVE",
                        "protection_status": "PASS",
                        "execution_status": "PASS",
                        "market_context_status": "TREND_PRESENT",
                        "execution_activity_count": 4,
                        "funnel_fills_runtime_count": 5,
                        "regime_trend_runtime_count": 4,
                        "realized_net_per_fill": realized_net,
                        "filtered_cost_ratio_avg": 0.20,
                }
            )
            runs.append({"assess_summary": assess_summary})

        summary, validation = REPLAY.aggregate_run_summaries(
            runs,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=3,
            min_mean_realized_net_per_fill=0.0,
            warn_mean_filtered_cost_ratio=0.80,
        )

        self.assertGreater(summary["mean_realized_net_per_fill"], 0.0)
        self.assertLess(summary["median_realized_net_per_fill_with_fills"], 0.0)
        self.assertLess(summary["positive_filled_segment_ratio"], 0.55)
        self.assertEqual(validation["coverage_strength_status"], "ROBUST")
        self.assertEqual(validation["status"], "fail")
        self.assertTrue(
            any(
                "median_realized_net_per_fill_with_fills" in reason
                for reason in validation["fail_reasons"]
            )
        )

    def test_aggregate_realized_net_is_weighted_by_fill_count(self):
        runs = [
            {
                "assess_summary": self._complete_replay_summary({
                    "verdict": "PASS",
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "protection_status": "PASS",
                    "execution_status": "PASS",
                    "market_context_status": "TREND_PRESENT",
                    "execution_activity_count": 1,
                    "funnel_fills_runtime_count": 1,
                    "realized_net_per_fill": 1.0,
                    "filtered_cost_ratio_avg": 0.2,
                })
            },
            {
                "assess_summary": self._complete_replay_summary({
                    "verdict": "PASS",
                    "runtime_validation_mode": "EXECUTION_ACTIVE",
                    "protection_status": "PASS",
                    "execution_status": "PASS",
                    "market_context_status": "TREND_PRESENT",
                    "execution_activity_count": 100,
                    "funnel_fills_runtime_count": 100,
                    "realized_net_per_fill": -0.02,
                    "filtered_cost_ratio_avg": 0.2,
                })
            },
        ]
        summary, validation = REPLAY.aggregate_run_summaries(
            runs,
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            warn_mean_filtered_cost_ratio=0.8,
        )
        self.assertAlmostEqual(summary["segment_mean_realized_net_per_fill"], 0.49)
        self.assertAlmostEqual(summary["mean_realized_net_per_fill"], -1.0 / 101.0)
        self.assertEqual(summary["aggregation_weight"], "fill_count")
        self.assertEqual(validation["status"], "fail")

    def test_terminal_account_net_overrides_stale_runtime_window(self):
        run = {
            "symbol": "BTCUSDT",
            "segment_index": 1,
            "assess_summary": {
                "runtime_validation_mode": "EXECUTION_ACTIVE",
                "execution_status": "PASS",
                "funnel_fills_runtime_count": 1,
                "realized_net_per_fill": 0.10,
                "execution_attribution_fill_count": 2,
                "execution_attribution_quality_fill_count": 2,
                "execution_attribution_fee_usd": 0.02,
                "replay_terminal_settlement_done_count": 1,
                "replay_terminal_settlement_failed_count": 0,
                "replay_terminal_realized_net_usd": -0.20,
                "replay_terminal_fee_usd": 0.03,
                "replay_terminal_funding_paid_usd": 0.01,
                "fills_attribution": {"notional_abs_usd": 200.0},
            },
        }

        economics = REPLAY.build_run_economics_attribution(run)

        self.assertTrue(economics["economics_complete"])
        self.assertEqual(economics["fill_count"], 2)
        self.assertAlmostEqual(economics["realized_net_usd"], -0.20)
        self.assertAlmostEqual(economics["realized_net_per_fill"], -0.10)
        self.assertAlmostEqual(economics["fee_usd"], 0.03)
        self.assertAlmostEqual(economics["funding_paid_usd"], 0.01)
        self.assertAlmostEqual(economics["estimated_net_before_fee_usd"], -0.17)
        self.assertAlmostEqual(economics["estimated_gross_pnl_usd"], -0.16)
        self.assertEqual(
            economics["accounting_source"],
            "replay_terminal_account_plus_fill_attribution",
        )

    def test_aggregate_rejects_missing_terminal_settlement_evidence(self):
        run = {
            "assess_summary": {
                "verdict": "PASS",
                "runtime_validation_mode": "EXECUTION_ACTIVE",
                "protection_status": "PASS",
                "execution_status": "PASS",
                "market_context_status": "TREND_PRESENT",
                "execution_activity_count": 2,
                "funnel_fills_runtime_count": 2,
                "realized_net_per_fill": 1.0,
                "execution_attribution_fill_count": 2,
                "execution_attribution_quality_fill_count": 2,
            }
        }

        summary, validation = REPLAY.aggregate_run_summaries(
            [run],
            min_execution_active_runs=1,
            min_execution_pass_runs=1,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            warn_mean_filtered_cost_ratio=0.8,
        )

        self.assertEqual(summary["economics_incomplete_runs"], 1)
        self.assertEqual(summary["total_fills"], 0)
        self.assertEqual(validation["status"], "fail")
        self.assertTrue(
            any(
                "economics attribution incomplete" in reason
                for reason in validation["fail_reasons"]
            )
        )

    def test_cost_sensitivity_finds_fee_reduction_break_even(self):
        economics_rows = [
            {
                "fill_count": 2,
                "estimated_gross_pnl_usd": 1.2,
                "fee_usd": 2.0,
            },
            {
                "fill_count": 1,
                "estimated_gross_pnl_usd": 0.6,
                "fee_usd": 1.0,
            },
        ]
        report = REPLAY.build_cost_sensitivity_report(
            economics_rows,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
        )
        self.assertEqual(report["status"], "diagnostic_pass")
        self.assertEqual(report["current_cost_status"], "fail")
        self.assertAlmostEqual(report["break_even_fee_multiplier"], 0.6)
        pass_names = {
            item["name"] for item in report["scenarios"] if item["status"] == "pass"
        }
        self.assertIn("fee_x0.5", pass_names)
        self.assertNotIn("fee_x1", pass_names)

    def test_cost_sensitivity_keeps_funding_fixed_while_scaling_fees(self):
        economics_rows = [
            {
                "fill_count": 1,
                "estimated_gross_pnl_usd": 1.2,
                "fee_usd": 1.0,
                "funding_paid_usd": 0.2,
            }
        ]

        report = REPLAY.build_cost_sensitivity_report(
            economics_rows,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
        )
        scenarios = {item["name"]: item for item in report["scenarios"]}

        self.assertAlmostEqual(report["break_even_fee_multiplier"], 1.0)
        self.assertAlmostEqual(report["total_funding_paid_usd"], 0.2)
        self.assertAlmostEqual(
            scenarios["fee_x1"]["mean_adjusted_realized_net_per_fill"], 0.0
        )
        self.assertAlmostEqual(
            scenarios["fee_x0.5"]["mean_adjusted_realized_net_per_fill"], 0.5
        )
        self.assertAlmostEqual(
            scenarios["fee_x0.5"]["total_adjusted_realized_net_usd_est"], 0.5
        )

    def test_exit_capture_flags_low_capture_when_path_mfe_covers_fee(self):
        economics_rows = [
            {
                "symbol": "BTCUSDT",
                "segment_index": 1,
                "fill_count": 1,
                "realized_net_per_fill": -0.8,
                "fee_usd": 1.0,
                "fee_per_fill_usd": 1.0,
                "fee_bps_per_fill": 10.0,
                "estimated_gross_pnl_usd": 0.2,
                "estimated_gross_per_fill_usd": 0.2,
                "segment_close_path_mfe": 0.005,
                "segment_close_path_efficiency": 0.6,
            },
            {
                "symbol": "ETHUSDT",
                "segment_index": 2,
                "fill_count": 1,
                "realized_net_per_fill": -0.7,
                "fee_usd": 1.0,
                "fee_per_fill_usd": 1.0,
                "fee_bps_per_fill": 10.0,
                "estimated_gross_pnl_usd": 0.3,
                "estimated_gross_per_fill_usd": 0.3,
                "segment_close_path_mfe": 0.006,
                "segment_close_path_efficiency": 0.5,
            },
        ]

        report = REPLAY.build_exit_capture_report(economics_rows)

        self.assertEqual(report["primary_diagnosis"], "exit_capture_low")
        self.assertIn(
            "path_mfe_covers_cost_but_gross_capture_low",
            report["diagnostics"],
        )
        self.assertEqual(report["low_capture_segment_count"], 2)
        self.assertGreater(report["mean_path_fee_coverage_ratio"], 2.0)

    def test_exit_capture_prefers_order_episode_evidence_over_segment_proxy(self):
        economics_rows = [
            {
                "symbol": "SOLUSDT",
                "segment_index": 1,
                "fill_count": 6,
                "realized_net_per_fill": 0.10,
                "fee_usd": 0.30,
                "fee_per_fill_usd": 0.05,
                "fee_bps_per_fill": 4.0,
                "estimated_gross_pnl_usd": 0.90,
                "estimated_gross_per_fill_usd": 0.15,
                # The whole replay segment continued much farther than the
                # actual trade episode, so this proxy deliberately looks poor.
                "segment_close_path_mfe": 0.02,
                "segment_close_path_efficiency": 0.9,
                "exit_capture_sample_count": 4,
                "exit_capture_low_count": 0,
                "exit_capture_low_ratio": 0.0,
                "exit_capture_mean_path_mfe_bps": 36.0,
                "exit_capture_mean_captured_gross_bps": 32.0,
                "exit_capture_mean_captured_net_bps": 26.0,
                "exit_capture_mean_fee_bps": 5.5,
                "exit_capture_mean_capture_ratio": 0.84,
            }
        ]

        report = REPLAY.build_exit_capture_report(economics_rows)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["authoritative_source"], "order_episode_runtime")
        self.assertEqual(report["sample_count"], 4)
        self.assertAlmostEqual(report["mean_gross_capture_of_path_mfe"], 0.84)
        self.assertLess(
            report["segment_proxy"]["mean_gross_capture_of_path_mfe"],
            0.10,
        )
        self.assertEqual(report["proxy_diagnostics"], [
            "path_mfe_covers_cost_but_gross_capture_low"
        ])

    def test_execution_cost_plan_marks_lower_cost_candidate_requires_rerun(self):
        economics_rows = [
            {
                "fill_count": 1,
                "realized_net_per_fill": -0.01,
                "fee_usd": 1.0,
                "fee_per_fill_usd": 1.0,
                "fee_bps_per_fill": 10.0,
                "estimated_gross_pnl_usd": 0.99,
                "estimated_gross_per_fill_usd": 0.99,
                "segment_close_path_mfe": 0.003,
            }
        ]
        exit_capture = REPLAY.build_exit_capture_report(economics_rows)

        plan = REPLAY.build_execution_cost_plan(
            economics_rows,
            min_total_fills=1,
            min_mean_realized_net_per_fill=0.0,
            exit_capture=exit_capture,
        )

        self.assertEqual(plan["status"], "candidate_requires_rerun")
        self.assertEqual(
            plan["primary_action"], "rerun_replay_with_lower_cost_execution"
        )
        self.assertIn("current_cost_not_deployable", plan["diagnostics"])
        self.assertIn(
            "lower_cost_execution_candidate_requires_rerun",
            plan["diagnostics"],
        )
        self.assertAlmostEqual(plan["break_even_fee_multiplier"], 0.99)

    def test_segment_market_attribution_reports_close_path_mfe_mae(self):
        rows = [
            REPLAY.FeatureRow(
                timestamp=1_700_000_000_000 + idx * 300_000,
                close=close,
                volume=1000.0,
                features={
                    "ema_diff": 0.01,
                    "zscore_48": 0.0,
                    "mom_12": 0.01,
                    "mom_48": 0.02,
                    "ret_1": 0.0,
                    "range_pct": 0.001,
                    "vol_12": 0.001,
                },
            )
            for idx, close in enumerate([100.0, 103.0, 101.0, 105.0])
        ]
        segment = REPLAY.ReplaySegment(
            start_index=0,
            end_index=3,
            start_timestamp=rows[0].timestamp,
            end_timestamp=rows[-1].timestamp,
            bars=4,
        )
        attribution = REPLAY.build_segment_market_attribution(segment, rows)
        self.assertEqual(attribution["dominant_direction_label"], "long")
        self.assertAlmostEqual(attribution["close_return"], 0.05)
        self.assertAlmostEqual(attribution["close_path_mfe"], 0.05)
        self.assertAlmostEqual(attribution["close_path_mae"], 0.0)

    def test_replay_csv_prepends_execution_disabled_causal_warmup(self):
        rows = [
            REPLAY.FeatureRow(
                timestamp=1_700_000_000_000 + idx * 300_000,
                open=100.0 + idx,
                high=101.0 + idx,
                low=99.0 + idx,
                close=100.0 + idx,
                volume=10.0,
                features={},
            )
            for idx in range(8)
        ]
        segment = REPLAY.ReplaySegment(
            start_index=5,
            end_index=7,
            start_timestamp=rows[5].timestamp,
            end_timestamp=rows[7].timestamp,
            bars=3,
        )
        with tempfile.TemporaryDirectory() as td:
            output = pathlib.Path(td) / "replay.csv"
            context_bars = REPLAY.write_replay_csv(
                rows,
                segment,
                "SOLUSDT",
                output,
                300_000,
                warmup_context_bars=4,
            )
            with output.open("r", encoding="utf-8") as fp:
                payload = list(csv.DictReader(fp))

        self.assertEqual(context_bars, 4)
        self.assertEqual(len(payload), 7)
        self.assertEqual(
            [row["execution_enabled"] for row in payload],
            ["0", "0", "0", "0", "1", "1", "1"],
        )

    def test_symbol_tradeability_cannot_suppress_negative_aggregate_fail(self):
        aggregate_validation = {
            "status": "fail",
            "fail_reasons": ["aggregate median net is negative"],
            "warn_reasons": [],
        }
        symbol_reports = {
            "SOLUSDT": {
                "aggregate_summary": {
                    "total_fills": 8,
                    "positive_realized_net_with_fills_runs": 5,
                    "negative_realized_net_with_fills_runs": 2,
                    "mean_realized_net_per_fill": 0.03,
                    "mean_realized_net_per_fill_with_fills": 0.03,
                    "median_realized_net_per_fill_with_fills": 0.02,
                    "positive_filled_segment_ratio": 0.70,
                },
                "aggregate_validation": {
                    "status": "pass",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "PASS",
                    "fail_reasons": [],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 3},
                },
            },
            "ETHUSDT": {
                "aggregate_summary": {
                    "total_fills": 5,
                    "positive_realized_net_with_fills_runs": 0,
                    "negative_realized_net_with_fills_runs": 5,
                    "mean_realized_net_per_fill": -0.02,
                    "mean_realized_net_per_fill_with_fills": -0.02,
                    "median_realized_net_per_fill_with_fills": -0.02,
                    "positive_filled_segment_ratio": 0.0,
                },
                "aggregate_validation": {
                    "status": "fail",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "PASS",
                    "fail_reasons": ["all filled segments net negative"],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": ["all filled segments net negative"],
                    "thresholds": {"min_total_fills": 3},
                },
            },
        }

        merged = REPLAY.merge_symbol_validations(
            aggregate_validation,
            symbol_reports,
            min_mean_realized_net_per_fill=0.0,
            min_tradable_symbols=1,
            final_holdout=True,
        )

        self.assertEqual(merged["status"], "fail")
        self.assertIn(
            "aggregate median net is negative",
            merged["fail_reasons"],
        )
        self.assertEqual(merged["tradable_symbols"], ["SOLUSDT"])
        self.assertIn("ETHUSDT", merged["quarantined_symbols"])
        self.assertEqual(merged["suppressed_aggregate_fail_reasons"], [])

    def test_symbol_tradeability_does_not_fail_on_non_source_insufficient_symbol(self):
        aggregate_validation = {
            "status": "fail",
            "fail_reasons": ["aggregate coverage insufficient"],
            "warn_reasons": [],
        }
        symbol_reports = {
            "SOLUSDT": {
                "aggregate_summary": {
                    "total_fills": 20,
                    "positive_realized_net_with_fills_runs": 12,
                    "negative_realized_net_with_fills_runs": 8,
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.60,
                },
                "aggregate_validation": {
                    "status": "pass",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "ROBUST",
                    "fail_reasons": [],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 20},
                },
            },
            "ETHUSDT": {
                "aggregate_summary": {"total_fills": 0},
                "aggregate_validation": {
                    "status": "fail",
                    "minimum_coverage_targets_met": False,
                    "coverage_strength_status": "INSUFFICIENT",
                    "fail_reasons": ["total_fills=0 < 20"],
                    "coverage_fail_reasons": ["total_fills=0 < 20"],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 20},
                },
            },
        }

        merged = REPLAY.merge_symbol_validations(
            aggregate_validation,
            symbol_reports,
            min_mean_realized_net_per_fill=0.0,
            min_tradable_symbols=1,
            source_symbol="SOLUSDT",
            final_holdout=True,
        )

        self.assertEqual(merged["status"], "fail")
        self.assertIn("aggregate coverage insufficient", merged["fail_reasons"])
        self.assertIn("ETHUSDT: total_fills=0 < 20", merged["fail_reasons"])
        self.assertEqual(merged["tradable_symbols"], ["SOLUSDT"])
        self.assertIn("ETHUSDT", merged["insufficient_symbols"])
        self.assertTrue(
            any("symbol_replay_coverage_insufficient=ETHUSDT" in reason for reason in merged["warn_reasons"])
        )

    def test_symbol_tradeability_fails_when_source_symbol_is_not_tradable(self):
        symbol_reports = {
            "SOLUSDT": {
                "aggregate_summary": {"total_fills": 0},
                "aggregate_validation": {
                    "status": "fail",
                    "minimum_coverage_targets_met": False,
                    "coverage_strength_status": "INSUFFICIENT",
                    "fail_reasons": ["total_fills=0 < 20"],
                    "coverage_fail_reasons": ["total_fills=0 < 20"],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 20},
                },
            },
            "ETHUSDT": {
                "aggregate_summary": {
                    "total_fills": 20,
                    "positive_realized_net_with_fills_runs": 12,
                    "negative_realized_net_with_fills_runs": 8,
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.60,
                },
                "aggregate_validation": {
                    "status": "pass",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "ROBUST",
                    "fail_reasons": [],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": [],
                    "thresholds": {"min_total_fills": 20},
                },
            },
        }

        merged = REPLAY.merge_symbol_validations(
            {"status": "pass", "fail_reasons": [], "warn_reasons": []},
            symbol_reports,
            min_mean_realized_net_per_fill=0.0,
            min_tradable_symbols=1,
            source_symbol="SOLUSDT",
            final_holdout=True,
        )

        self.assertEqual(merged["status"], "fail")
        self.assertIn(
            "SOLUSDT: total_fills=0 < 20",
            merged["fail_reasons"],
        )

    def test_quarantined_source_symbol_keeps_execution_coverage_separate(self):
        symbol_reports = {
            "SOLUSDT": {
                "aggregate_summary": {
                    "total_fills": 20,
                    "positive_realized_net_with_fills_runs": 4,
                    "negative_realized_net_with_fills_runs": 4,
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.50,
                },
                "aggregate_validation": {
                    "status": "fail",
                    "minimum_coverage_targets_met": True,
                    "coverage_strength_status": "ROBUST",
                    "fail_reasons": [
                        "positive_filled_segment_ratio=0.500000 < 0.550000"
                    ],
                    "coverage_fail_reasons": [],
                    "quality_fail_reasons": [
                        "positive_filled_segment_ratio=0.500000 < 0.550000"
                    ],
                    "thresholds": {"min_total_fills": 20},
                },
            }
        }

        merged = REPLAY.merge_symbol_validations(
            {"status": "fail", "fail_reasons": [], "warn_reasons": []},
            symbol_reports,
            min_mean_realized_net_per_fill=0.0,
            min_tradable_symbols=1,
            source_symbol="SOLUSDT",
            final_holdout=True,
        )

        tradeability = merged["symbol_tradeability"]
        self.assertEqual(tradeability["execution_covered_symbols"], ["SOLUSDT"])
        self.assertEqual(tradeability["tradable_symbols"], [])
        self.assertIn("SOLUSDT", tradeability["quarantined_symbols"])
        self.assertNotIn(
            "source_symbol_not_execution_covered=SOLUSDT",
            merged["fail_reasons"],
        )
        self.assertIn(
            "SOLUSDT: positive_filled_segment_ratio=0.500000 < 0.550000",
            merged["fail_reasons"],
        )

    def test_final_holdout_ledger_rejects_rerun_overlap_and_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ledger = root / "holdout.jsonl"
            claim = {
                "symbol": "SOLUSDT",
                "bar_interval_ms": 300000,
                "holdout_start_ts_ms": 1000,
                "holdout_end_ts_ms": 2000,
                "dataset_path": "/tmp/holdout.csv",
                "dataset_sha256": "a" * 64,
            }
            first = REPLAY.claim_final_holdouts(
                ledger,
                experiment_id="run-1",
                candidate_identity_sha256="b" * 64,
                holdouts=[claim],
            )
            self.assertEqual(len(first), 1)
            self.assertEqual(
                len(ledger.read_text(encoding="utf-8").splitlines()), 1
            )
            self.assertRegex(first[0]["entry_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(
                ledger.with_suffix(".jsonl.checkpoint.json").is_file()
            )

            with self.assertRaisesRegex(
                RuntimeError, "experiment already consumed"
            ):
                REPLAY.claim_final_holdouts(
                    ledger,
                    experiment_id="run-1",
                    candidate_identity_sha256="b" * 64,
                    holdouts=[claim],
                )

            with self.assertRaisesRegex(
                RuntimeError, "overlaps consumed evidence"
            ):
                REPLAY.claim_final_holdouts(
                    ledger,
                    experiment_id="run-2",
                    candidate_identity_sha256="c" * 64,
                    holdouts=[{**claim, "holdout_start_ts_ms": 1500}],
                )

            later = REPLAY.claim_final_holdouts(
                ledger,
                experiment_id="run-3",
                candidate_identity_sha256="c" * 64,
                holdouts=[
                    {
                        **claim,
                        "holdout_start_ts_ms": 3000,
                        "holdout_end_ts_ms": 4000,
                        "dataset_sha256": "d" * 64,
                    }
                ],
            )
            self.assertEqual(len(later), 1)
            self.assertEqual(
                len(ledger.read_text(encoding="utf-8").splitlines()), 2
            )
            ledger_before_conflict = ledger.read_bytes()
            checkpoint_path = ledger.with_suffix(
                ".jsonl.checkpoint.json"
            )
            checkpoint_before_conflict = checkpoint_path.read_bytes()
            with self.assertRaisesRegex(
                RuntimeError, "overlaps consumed evidence"
            ):
                REPLAY.claim_final_holdouts(
                    ledger,
                    experiment_id="run-multi-conflict",
                    candidate_identity_sha256="e" * 64,
                    holdouts=[
                        {
                            **claim,
                            "symbol": "BTCUSDT",
                            "holdout_start_ts_ms": 5000,
                            "holdout_end_ts_ms": 6000,
                            "dataset_sha256": "e" * 64,
                        },
                        {
                            **claim,
                            "holdout_start_ts_ms": 3500,
                            "holdout_end_ts_ms": 4500,
                            "dataset_sha256": "f" * 64,
                        },
                    ],
                )
            self.assertEqual(ledger.read_bytes(), ledger_before_conflict)
            self.assertEqual(
                checkpoint_path.read_bytes(),
                checkpoint_before_conflict,
            )
            lines = ledger.read_text(encoding="utf-8").splitlines()
            ledger.write_text(lines[0] + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError, "checkpoint mismatch"
            ):
                REPLAY.claim_final_holdouts(
                    ledger,
                    experiment_id="run-4",
                    candidate_identity_sha256="e" * 64,
                    holdouts=[
                        {
                            **claim,
                            "holdout_start_ts_ms": 5000,
                            "holdout_end_ts_ms": 6000,
                            "dataset_sha256": "f" * 64,
                        }
                    ],
                )


if __name__ == "__main__":
    unittest.main()
