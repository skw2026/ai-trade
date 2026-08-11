#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import pathlib
import sys
import tempfile
import unittest


def load_module():
    path = pathlib.Path(__file__).with_name("validate_evolution_uplift.py")
    spec = importlib.util.spec_from_file_location("validate_evolution_uplift", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


UPLIFT = load_module()
BENCHMARK_ID = "1" * 64
TRADE_BOT_SHA256 = "2" * 64


def canonical_sha256(value):
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def benchmark_report(block_count=8, cells=None):
    cells = cells or [{"symbol": "BTCUSDT", "entry_regime": "trend"}]
    blocks = [
        {
            "block_id": f"block-{index + 1:02d}",
            "start_timestamp_ms": index * 1000,
            "end_timestamp_ms": index * 1000 + 999,
            "event_sha256": f"{index + 10:064x}",
            "cells": copy.deepcopy(cells),
        }
        for index in range(block_count)
    ]
    return {
        "schema_version": "decision_evidence_benchmark_validation_v1",
        "identity_status": "VERIFIED",
        "benchmark_id": BENCHMARK_ID,
        "canonical_identity": {
            "schema_version": "decision_evidence_benchmark_v1",
            "components": {},
            "evaluation_universe": {"blocks": blocks},
        },
        "drifts": [],
    }


def config():
    return {
        "schema_version": "decision_evidence_validation_v1",
        "uplift": {
            "min_independent_blocks": 8,
            "block_coverage": 1,
            "bootstrap_trials": 10000,
            "lcb": 0.95,
        },
    }


def policy_payload(enabled):
    policy = {
        "execution.maker_fee_bps": 1.5,
        "execution.slippage_bps": 2.0,
        "risk.max_position_notional_usd": 1000,
        "self_evolution.enabled": enabled,
        "self_evolution.initial_defensive_weight": 0.45,
        "self_evolution.initial_trend_weight": 0.55,
        "self_evolution.update_interval_seconds": 17,
    }
    return {
        "schema_version": "execution_policy_v2",
        "policy": policy,
        "sha256": canonical_sha256(policy),
    }


def full_episode(
    *,
    arm,
    block_index,
    sequence,
    symbol,
    entry_regime,
    utility,
    segment_sha256,
    policy,
):
    open_fill_id = f"{arm}-fill-open-{block_index}-{sequence}"
    close_fill_id = f"{arm}-fill-close-{block_index}-{sequence}"
    open_client_id = f"client-{open_fill_id}"
    close_client_id = f"client-{close_fill_id}"
    return {
        "evaluator_episode_id": f"{arm}-episode-{block_index}-{sequence}",
        "runtime_position_episode_id": f"runtime-{arm}-{block_index}-{sequence}",
        "segment_identity_sha256": segment_sha256,
        "symbol": symbol,
        "entry_regime": entry_regime,
        "first_fill_id": open_fill_id,
        "fill_ids": [open_fill_id, close_fill_id],
        "client_order_ids": [open_client_id, close_client_id],
        "fills": [
            {
                "fill_id": open_fill_id,
                "client_order_id": open_client_id,
                "order_state_before": "NEW",
                "order_state_after": "FILLED",
                "direction": 1,
                "qty": 1.0,
                "price": 100.0,
                "fee": -0.05,
            },
            {
                "fill_id": close_fill_id,
                "client_order_id": close_client_id,
                "order_state_before": "NEW",
                "order_state_after": "FILLED",
                "direction": -1,
                "qty": 1.0,
                "price": 100.0 + utility + 0.1,
                "fee": -0.05,
            },
        ],
        "candidate_lineage": {
            "decision_id": f"decision-{block_index}-{sequence}",
            "candidate_id": "candidate-v1",
            "model_version": "model-v1",
            "position_episode_id": f"runtime-{arm}-{block_index}-{sequence}",
        },
        "position_episode": {
            "evidence_complete": True,
            "symbol": symbol,
            "fill_event_count": 2,
            "unique_order_count": 2,
        },
        "exit_capture": {"observed": True, "client_order_id": close_client_id},
        "execution_policy_identity": copy.deepcopy(policy),
        "terminal_settlement": {
            "done_count": 1,
            "failed_count": 0,
            "realized_net_usd": utility + 0.1,
            "fees_usd": 0.1,
            "funding_paid_usd": 0.0,
        },
        "realized_pnl_usd": utility + 0.1,
        "fee_usd": 0.1,
        "funding_paid_usd": 0.0,
        "executable_net_utility": utility,
        "utility_source": "complete_execution_replay",
        "execution_path_complete": True,
        "missing_path_evidence": [],
    }


def paired_manifest(benchmark=None, frozen_utility=0.0, adaptive_utility=1.0):
    benchmark = benchmark or benchmark_report()
    benchmark_blocks = benchmark["canonical_identity"]["evaluation_universe"]["blocks"]
    frozen_policy = policy_payload(False)
    adaptive_policy = policy_payload(True)
    common_policy = {
        key: value
        for key, value in frozen_policy["policy"].items()
        if key != "self_evolution.enabled"
    }
    initial_weights = {"trend": 0.55, "defensive": 0.45}
    empty_state = {
        "schema_version": "empty_evolution_state_v1",
        "records": [],
    }
    exact_blocks = []
    for index, block in enumerate(benchmark_blocks):
        exact_blocks.append(
            {
                **copy.deepcopy(block),
                "symbol": block["cells"][0]["symbol"],
                "segment_identity_sha256": f"{index + 100:064x}",
                "replay_csv": f"/frozen/replay/block-{index + 1:02d}.csv",
            }
        )
    expected_ids = [block["block_id"] for block in exact_blocks]

    arms = {}
    for arm_name, arm_policy, utility in (
        ("frozen", frozen_policy, frozen_utility),
        ("adaptive", adaptive_policy, adaptive_utility),
    ):
        blocks = []
        for index, planned in enumerate(exact_blocks):
            cell = planned["cells"][0]
            episode = full_episode(
                arm=arm_name,
                block_index=index,
                sequence=0,
                symbol=cell["symbol"],
                entry_regime=cell["entry_regime"],
                utility=float(utility),
                segment_sha256=planned["segment_identity_sha256"],
                policy=arm_policy,
            )
            evidence = {
                "schema_version": "episode_execution_evidence_v1",
                "segment_identity_sha256": planned["segment_identity_sha256"],
                "execution_policy_identity": copy.deepcopy(arm_policy),
                "episode_count": 1,
                "complete_episode_count": 1,
                "execution_path_complete": True,
                "aggregate_only_rejected": False,
                "missing_path_evidence": [],
                "episodes": [episode],
            }
            blocks.append(
                {
                    "block_id": planned["block_id"],
                    "symbol": planned["symbol"],
                    "event_sha256": planned["event_sha256"],
                    "segment_identity_sha256": planned[
                        "segment_identity_sha256"
                    ],
                    "state_dir": f"/{arm_name}/{planned['block_id']}/state",
                    "initial_weights_sha256": canonical_sha256(initial_weights),
                    "initial_evolution_state_sha256": canonical_sha256(empty_state),
                    "historical_state_loaded": False,
                    "continued_from_block_id": None,
                    "trade_bot_exit_code": 0,
                    "assess_exit_code": 0,
                    "episode_execution_evidence": evidence,
                    "no_trade_zero_utility": False,
                }
            )
        arms[arm_name] = {
            "config": {
                "path": f"/{arm_name}.yaml",
                "sha256": f"{3 if arm_name == 'frozen' else 4:064x}",
                "policy": arm_policy,
            },
            "output_dir": f"/{arm_name}",
            "command": ["python3", "run_replay_validation.py"],
            "exit_code": 0,
            "report": {
                "path": f"/{arm_name}/replay_validation_report.json",
                "sha256": f"{5 if arm_name == 'frozen' else 6:064x}",
                "schema_version": "exact_replay_block_audit_v1",
                "status": "VERIFIED",
            },
            "trade_bot_sha256": TRADE_BOT_SHA256,
            "infrastructure_status": "VERIFIED",
            "business_gate_status": "PASSED",
            "expected_block_ids": list(expected_ids),
            "executed_block_ids": list(expected_ids),
            "block_execution_counts": {block_id: 1 for block_id in expected_ids},
            "blocks": blocks,
            "mismatches": [],
        }
    return {
        "schema_version": "paired_evolution_replay_v1",
        "status": "VERIFIED",
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "benchmark_id": benchmark["benchmark_id"],
        "common_policy": {
            "schema_version": "paired_common_execution_policy_v1",
            "excluded_paths": ["self_evolution.enabled"],
            "policy": common_policy,
            "sha256": canonical_sha256(common_policy),
        },
        "initial_weights": {
            "payload": initial_weights,
            "sha256": canonical_sha256(initial_weights),
        },
        "initial_evolution_state": {
            "payload": empty_state,
            "sha256": canonical_sha256(empty_state),
            "empty": True,
            "historical_state_loading_allowed": False,
            "cross_block_continuation_allowed": False,
        },
        "trade_bot": {"path": "/trade_bot", "sha256": TRADE_BOT_SHA256},
        "exact_block_plan": {
            "path": "/exact_block_plan.json",
            "sha256": "7" * 64,
            "read_only": True,
            "benchmark_id": benchmark["benchmark_id"],
            "expected_block_ids": expected_ids,
            "blocks": exact_blocks,
        },
        "policy_differences": [
            {
                "path": "self_evolution.enabled",
                "frozen": False,
                "adaptive": True,
            }
        ],
        "arms": arms,
        "mismatches": [],
    }


class EvolutionUpliftValidationTest(unittest.TestCase):
    def validate(self, paired=None, benchmark=None, policy=None):
        benchmark = benchmark or benchmark_report()
        return UPLIFT.validate_evolution_uplift(
            paired if paired is not None else paired_manifest(benchmark),
            benchmark,
            policy if policy is not None else config(),
        )

    def test_positive_complete_eight_block_pair_proves_uplift(self):
        report = self.validate()

        self.assertEqual(report["status"], "UPLIFT_PROVEN")
        self.assertEqual(report["benchmark_id"], BENCHMARK_ID)
        self.assertFalse(report["promotion_authority"])
        self.assertEqual(report["block_coverage"]["expected_block_count"], 8)
        self.assertEqual(report["block_coverage"]["frozen_ratio"], 1.0)
        self.assertEqual(report["block_coverage"]["adaptive_ratio"], 1.0)
        self.assertEqual(report["bootstrap"]["trials"], 10000)
        self.assertEqual(report["bootstrap"]["lcb_index"], 499)
        self.assertGreater(report["bootstrap"]["lower_confidence_bound"], 0.0)
        self.assertEqual(len(report["arms"]["frozen"]["episodes"]), 8)
        self.assertEqual(len(report["arms"]["adaptive"]["episodes"]), 8)
        self.assertEqual(report["missing_evidence"], [])

    def test_episode_ids_and_counts_need_not_match_and_planned_empty_cells_are_zero(self):
        cells = [
            {"symbol": "BTCUSDT", "entry_regime": "trend"},
            {"symbol": "ETHUSDT", "entry_regime": "range"},
        ]
        benchmark = benchmark_report(cells=cells)
        paired = paired_manifest(benchmark)
        block = paired["arms"]["frozen"]["blocks"][0]
        evidence = block["episode_execution_evidence"]
        first = evidence["episodes"][0]
        first["executable_net_utility"] = 0.25
        first["realized_pnl_usd"] = 0.35
        first["terminal_settlement"]["realized_net_usd"] = 0.35
        first["fills"][1]["price"] = 100.35
        second = full_episode(
            arm="frozen",
            block_index=0,
            sequence=1,
            symbol="BTCUSDT",
            entry_regime="trend",
            utility=0.75,
            segment_sha256=block["segment_identity_sha256"],
            policy=paired["arms"]["frozen"]["config"]["policy"],
        )
        evidence["episodes"].append(second)
        evidence["episode_count"] = 2
        evidence["complete_episode_count"] = 2
        adaptive_episode = paired["arms"]["adaptive"]["blocks"][0][
            "episode_execution_evidence"
        ]["episodes"][0]
        adaptive_episode["executable_net_utility"] = 2.0
        adaptive_episode["realized_pnl_usd"] = 2.1
        adaptive_episode["terminal_settlement"]["realized_net_usd"] = 2.1
        adaptive_episode["fills"][1]["price"] = 102.1

        report = self.validate(paired, benchmark)

        self.assertEqual(report["status"], "UPLIFT_PROVEN")
        cells_by_key = {
            (item["block_id"], item["symbol"], item["entry_regime"]): item
            for item in report["aggregation_cells"]
        }
        btc = cells_by_key[("block-01", "BTCUSDT", "trend")]
        eth = cells_by_key[("block-01", "ETHUSDT", "range")]
        self.assertEqual(btc["frozen_episode_count"], 2)
        self.assertEqual(btc["adaptive_episode_count"], 1)
        self.assertAlmostEqual(btc["delta"], 1.0)
        self.assertEqual(eth["frozen_utility"], 0.0)
        self.assertEqual(eth["adaptive_utility"], 0.0)
        self.assertEqual(len(report["aggregation_cells"]), 16)

    def test_observed_unplanned_cell_is_rejected_not_added_to_universe(self):
        benchmark = benchmark_report()
        paired = paired_manifest(benchmark)
        evidence = paired["arms"]["adaptive"]["blocks"][0][
            "episode_execution_evidence"
        ]
        extra = copy.deepcopy(evidence["episodes"][0])
        extra["evaluator_episode_id"] = "unplanned-episode"
        extra["symbol"] = "DOGEUSDT"
        evidence["episodes"].append(extra)
        evidence["episode_count"] = 2
        evidence["complete_episode_count"] = 2

        report = self.validate(paired, benchmark)

        self.assertEqual(report["status"], "UNVERIFIABLE")
        self.assertTrue(
            any("unplanned_cell" in item for item in report["missing_evidence"])
        )
        self.assertFalse(
            any(item["symbol"] == "DOGEUSDT" for item in report["aggregation_cells"])
        )

    def test_zero_trade_assessor_shape_is_valid_zero_and_aggregate_fields_are_ignored(self):
        benchmark = benchmark_report()
        paired = paired_manifest(benchmark)
        block = paired["arms"]["frozen"]["blocks"][0]
        evidence = block["episode_execution_evidence"]
        evidence.update(
            {
                "episode_count": 0,
                "complete_episode_count": 0,
                "execution_path_complete": False,
                "aggregate_only_rejected": True,
                "missing_path_evidence": ["fills"],
                "episodes": [],
                "account_realized_net_usd": 999999.0,
                "virtual_pnl": 999999.0,
                "self_evolution_update_count": 999,
            }
        )
        block["no_trade_zero_utility"] = True
        block["assess_exit_code"] = 1
        paired["arms"]["frozen"]["business_gate_status"] = "FAILED"
        paired["arms"]["frozen"]["exit_code"] = 2
        paired["arms"]["frozen"]["report"]["status"] = "UNVERIFIABLE"

        report = self.validate(paired, benchmark)

        self.assertEqual(report["status"], "UPLIFT_PROVEN")
        first = report["aggregation_cells"][0]
        self.assertEqual(first["frozen_utility"], 0.0)
        self.assertEqual(first["frozen_episode_count"], 0)

    def test_bootstrap_hash_draws_repeat_and_lcb_is_exact_sorted_index_499(self):
        expected = int.from_bytes(
            hashlib.sha256(f"{BENCHMARK_ID}:uplift:17:3".encode("ascii")).digest()[:8],
            "big",
        ) % 8
        self.assertEqual(
            UPLIFT.bootstrap_draw_index(BENCHMARK_ID, 17, 3, 8), expected
        )
        deltas = [float(index) for index in range(8)]
        first = UPLIFT.block_bootstrap_statistics(
            deltas, benchmark_id=BENCHMARK_ID, trials=10000
        )
        second = UPLIFT.block_bootstrap_statistics(
            deltas, benchmark_id=BENCHMARK_ID, trials=10000
        )
        self.assertEqual(first, second)
        values = [float(index) for index in reversed(range(10000))]
        lcb, index = UPLIFT.lower_confidence_bound(values, confidence=0.95)
        self.assertEqual(index, 499)
        self.assertEqual(lcb, 499.0)

    def test_complete_nonpositive_lcb_is_not_proven(self):
        benchmark = benchmark_report()
        paired = paired_manifest(
            benchmark, frozen_utility=1.0, adaptive_utility=1.0
        )

        report = self.validate(paired, benchmark)

        self.assertEqual(report["status"], "NOT_PROVEN")
        self.assertEqual(report["bootstrap"]["lower_confidence_bound"], 0.0)
        self.assertEqual(report["missing_evidence"], [])

    def test_identity_coverage_policy_and_episode_drifts_are_unverifiable(self):
        mutations = {
            "benchmark": lambda payload: payload.update({"benchmark_id": "f" * 64}),
            "csv": lambda payload: payload["arms"]["adaptive"]["blocks"][0].update(
                {"event_sha256": "e" * 64}
            ),
            "segment": lambda payload: payload["arms"]["adaptive"]["blocks"][0].update(
                {"segment_identity_sha256": "d" * 64}
            ),
            "trade_bot": lambda payload: payload["arms"]["adaptive"].update(
                {"trade_bot_sha256": "c" * 64}
            ),
            "policy": self._drift_adaptive_policy,
            "coverage": self._remove_adaptive_coverage,
            "episode": self._break_episode_path,
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                benchmark = benchmark_report()
                paired = paired_manifest(benchmark)
                mutation(paired)
                report = self.validate(paired, benchmark)
                self.assertEqual(report["status"], "UNVERIFIABLE")
                self.assertTrue(report["missing_evidence"])

    @staticmethod
    def _drift_adaptive_policy(payload):
        identity = payload["arms"]["adaptive"]["config"]["policy"]
        identity["policy"]["risk.max_position_notional_usd"] = 2000
        identity["sha256"] = canonical_sha256(identity["policy"])

    @staticmethod
    def _remove_adaptive_coverage(payload):
        arm = payload["arms"]["adaptive"]
        missing_id = arm["executed_block_ids"].pop()
        arm["block_execution_counts"].pop(missing_id)
        arm["blocks"].pop()

    @staticmethod
    def _break_episode_path(payload):
        episode = payload["arms"]["frozen"]["blocks"][0][
            "episode_execution_evidence"
        ]["episodes"][0]
        episode["execution_path_complete"] = False
        episode["utility_source"] = "unverifiable"
        episode["missing_path_evidence"] = ["terminal_settlement"]

    def test_virtual_or_account_metrics_cannot_replace_episode_ledger(self):
        benchmark = benchmark_report()
        paired = paired_manifest(benchmark)
        block = paired["arms"]["adaptive"]["blocks"][0]
        block["episode_execution_evidence"] = {
            "schema_version": "episode_execution_evidence_v1",
            "segment_identity_sha256": block["segment_identity_sha256"],
            "execution_policy_identity": paired["arms"]["adaptive"]["config"][
                "policy"
            ],
            "account_realized_net_usd": 999.0,
            "virtual_pnl": 888.0,
            "self_evolution_update_count": 7,
        }

        report = self.validate(paired, benchmark)

        self.assertEqual(report["status"], "UNVERIFIABLE")
        self.assertTrue(
            any("episode_ledger" in item for item in report["missing_evidence"])
        )

    def test_insufficient_block_count_or_coverage_is_unverifiable(self):
        benchmark = benchmark_report(block_count=7)
        report = self.validate(paired_manifest(benchmark), benchmark)
        self.assertEqual(report["status"], "UNVERIFIABLE")
        self.assertIn("benchmark.minimum_independent_blocks", report["missing_evidence"])


if __name__ == "__main__":
    unittest.main()
