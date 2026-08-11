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


def load_assess_module():
    path = pathlib.Path(__file__).with_name("assess_run_log.py")
    spec = importlib.util.spec_from_file_location("assess_run_log_for_uplift", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ASSESS = load_assess_module()
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


def config_bytes(policy=None):
    return json.dumps(
        policy if policy is not None else config(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def config_sha256(policy=None):
    return hashlib.sha256(config_bytes(policy)).hexdigest()


def benchmark_from_blocks(blocks, policy=None, policy_sha256=None):
    policy = copy.deepcopy(policy if policy is not None else config())
    policy_sha256 = policy_sha256 or config_sha256(policy)
    canonical_identity = {
        "schema_version": "decision_evidence_benchmark_v1",
        "components": {
            name: {
                "logical_id": f"{name}-v1",
                "files": [{"logical_name": name, "sha256": f"{index + 500:064x}"}],
            }
            for index, name in enumerate(
                (
                    "data",
                    "split",
                    "cost",
                    "features",
                    "actions",
                    "baseline_policy",
                    "run_config",
                    "implementation",
                )
            )
        },
        "evaluation_universe": {"blocks": blocks},
        "validation_policy": {"sha256": policy_sha256, "policy": policy},
    }
    return {
        "schema_version": "decision_evidence_benchmark_validation_v1",
        "identity_status": "VERIFIED",
        "benchmark_id": canonical_sha256(canonical_identity),
        "validation_config_sha256": policy_sha256,
        "canonical_identity": canonical_identity,
        "drifts": [],
    }


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
    return benchmark_from_blocks(blocks)


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
    runtime_episode_id = f"runtime-{arm}-{block_index}-{sequence}"
    lineage = {
        "decision_id": f"decision-{block_index}-{sequence}",
        "candidate_id": "candidate-v1",
        "model_version": "model-v1",
        "mode": "canary",
        "position_episode_id": runtime_episode_id,
    }
    return {
        "evaluator_episode_id": hashlib.sha256(
            f"{segment_sha256}:{symbol}:{open_fill_id}".encode("ascii")
        ).hexdigest(),
        "runtime_position_episode_id": runtime_episode_id,
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
                "candidate_lineage": copy.deepcopy(lineage),
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
                "candidate_lineage": copy.deepcopy(lineage),
            },
        ],
        "candidate_lineage": lineage,
        "position_episode": {
            "evidence_complete": True,
            **copy.deepcopy(lineage),
            "symbol": symbol,
            "realized_net_usd": utility,
            "funding_paid_usd": 0.0,
            "fill_event_count": 2,
            "unique_order_count": 2,
        },
        "exit_capture": {
            "observed": True,
            "client_order_id": close_client_id,
            "symbol": symbol,
            "realized_pnl_usd": utility + 0.1,
        },
        "execution_policy_identity": copy.deepcopy(policy),
        "terminal_settlement": {
            "done_count": 1,
            "failed_count": 0,
            "realized_net_usd": utility + 0.1,
            "fees_usd": 0.1,
            "funding_paid_usd": 0.0,
            "position_count": 0,
            "segment_identity_sha256": segment_sha256,
        },
        "realized_pnl_usd": utility + 0.1,
        "fee_usd": 0.1,
        "funding_paid_usd": 0.0,
        "executable_net_utility": utility,
        "utility_source": "complete_execution_replay",
        "execution_path_complete": True,
        "missing_path_evidence": [],
        "identity_mismatches": [],
    }


def sync_evidence_terminal(evidence):
    episodes = evidence["episodes"]
    terminal = {
        "done_count": 1,
        "failed_count": 0,
        "failed_reasons": [],
        "realized_net_usd": sum(
            float(episode["executable_net_utility"]) for episode in episodes
        ),
        "fees_usd": sum(float(episode["fee_usd"]) for episode in episodes),
        "funding_paid_usd": sum(
            float(episode["funding_paid_usd"]) for episode in episodes
        ),
        "position_count": 0,
        "segment_identity_sha256": evidence["segment_identity_sha256"],
    }
    evidence["terminal_settlement"] = terminal
    for episode in episodes:
        episode["terminal_settlement"] = copy.deepcopy(terminal)


def episode_evidence(episodes, segment_sha256, policy):
    evidence = {
        "schema_version": "episode_execution_evidence_v1",
        "segment_identity_sha256": segment_sha256,
        "execution_policy_identity": copy.deepcopy(policy),
        "episode_count": len(episodes),
        "complete_episode_count": len(episodes),
        "execution_path_complete": bool(episodes),
        "aggregate_only_rejected": not episodes,
        "missing_path_evidence": [] if episodes else ["fills"],
        "episodes": episodes,
    }
    sync_evidence_terminal(evidence)
    return evidence


def real_assessor_episode_evidence(segment_sha256, policy):
    lines = [
        "2026-08-11 10:00:00 [INFO] REGIME_CHANGE: symbol=BTCUSDT, regime=UPTREND, bucket=trend",
        "2026-08-11 10:00:01 [INFO] INTEGRATOR_POLICY_ENQUEUED: decision_id=d-real, candidate_id=c-real, model_version=m-real, mode=canary, position_episode_id=runtime-real, client_order_id=open-real, symbol=BTCUSDT",
        "2026-08-11 10:00:01 [INFO] BYBIT_SUBMIT: symbol=BTCUSDT, client_order_id=open-real, purpose=0, order_type=Limit, liquidity_preference=maker, reduce_only=false, qty=1.0, price=100.0",
        "2026-08-11 10:00:02 [INFO] FILL_APPLIED: fill_id=fill-open-real, client_order_id=open-real, symbol=BTCUSDT, side=Buy, qty=1.0, price=100.0, fee=-0.020000, liquidity=maker, order_state_before=accepted, order_state_after=filled, local_qty_before=0.0, local_qty_after=1.0",
        "2026-08-11 10:00:02 [INFO] INTEGRATOR_POLICY_FILLED: decision_id=d-real, candidate_id=c-real, model_version=m-real, mode=canary, position_episode_id=runtime-real, client_order_id=open-real, fill_id=fill-open-real, symbol=BTCUSDT, qty=1.0, price=100.0, fee=-0.020000, liquidity=maker",
        "2026-08-11 10:00:03 [INFO] FUNDING_APPLIED: symbol=BTCUSDT, rate_per_interval=0.000100, funding_paid_usd=0.010000, source=market",
        "2026-08-11 10:00:04 [INFO] BYBIT_SUBMIT: symbol=BTCUSDT, client_order_id=close-real, purpose=3, order_type=Market, liquidity_preference=taker, reduce_only=true, qty=1.0",
        "2026-08-11 10:00:05 [INFO] FILL_APPLIED: fill_id=fill-close-real, client_order_id=close-real, symbol=BTCUSDT, side=Sell, qty=1.0, price=100.5, fee=-0.030000, liquidity=taker, order_state_before=accepted, order_state_after=filled, local_qty_before=1.0, local_qty_after=0.0",
        "2026-08-11 10:00:05 [INFO] INTEGRATOR_POLICY_FILLED: decision_id=d-real, candidate_id=c-real, model_version=m-real, mode=canary, position_episode_id=runtime-real, client_order_id=close-real, fill_id=fill-close-real, symbol=BTCUSDT, qty=1.0, price=100.5, fee=-0.030000, liquidity=taker",
        "2026-08-11 10:00:05 [INFO] EXIT_CAPTURE_SAMPLE: symbol=BTCUSDT, client_order_id=close-real, purpose=reduce, realized_pnl_usd=0.5, realized_net_usd=0.47, fee_bps=3.0, round_trip_cost_bps=13.0, capture_ratio=0.625",
        "2026-08-11 10:00:05 [INFO] INTEGRATOR_POLICY_EPISODE_CLOSED: position_episode_id=runtime-real, decision_id=d-real, candidate_id=c-real, model_version=m-real, mode=canary, symbol=BTCUSDT, realized_net_usd=0.44, funding_paid_usd=0.01, fill_event_count=2, unique_order_count=2, evidence_complete=true",
        "2026-08-11 10:00:06 [INFO] REPLAY_TERMINAL_SETTLEMENT_DONE: position_count=0, realized_net_usd=0.44, fees_usd=0.05, funding_paid_usd=0.01",
    ]
    report = ASSESS.assess(
        "\n".join(lines) + "\n",
        ASSESS.STAGE_RULES["DEPLOY"],
        min_runtime_status=0,
        segment_identity_sha256=segment_sha256,
        execution_policy_identity=policy,
    )
    return report["episode_execution_evidence"]


def real_zero_trade_assessor_evidence(segment_sha256, policy):
    report = ASSESS.assess(
        "2026-08-11 10:00:06 [INFO] REPLAY_TERMINAL_SETTLEMENT_DONE: "
        "position_count=0, realized_net_usd=0.0, fees_usd=0.0, "
        "funding_paid_usd=0.0\n",
        ASSESS.STAGE_RULES["DEPLOY"],
        min_runtime_status=0,
        segment_identity_sha256=segment_sha256,
        execution_policy_identity=policy,
    )
    return report["episode_execution_evidence"]


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
            evidence = episode_evidence(
                [episode], planned["segment_identity_sha256"], arm_policy
            )
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


def multi_execution_benchmark_report(block_count=8):
    blocks = []
    for index in range(block_count):
        block_id = f"multi-block-{index + 1:02d}"
        blocks.append(
            {
                "block_id": block_id,
                "start_timestamp_ms": index * 1000,
                "end_timestamp_ms": index * 1000 + 999,
                "event_sha256": f"{index + 10:064x}",
                "cells": [
                    {"symbol": "BTCUSDT", "entry_regime": "range"},
                    {"symbol": "BTCUSDT", "entry_regime": "trend"},
                    {"symbol": "ETHUSDT", "entry_regime": "defensive"},
                ],
                "executions": [
                    {
                        "execution_id": f"{block_id}:BTCUSDT",
                        "symbol": "BTCUSDT",
                        "planned_entry_regimes": ["range", "trend"],
                        "event_sha256": f"{index + 100:064x}",
                    },
                    {
                        "execution_id": f"{block_id}:ETHUSDT",
                        "symbol": "ETHUSDT",
                        "planned_entry_regimes": ["defensive"],
                        "event_sha256": f"{index + 200:064x}",
                    },
                ],
            }
        )
    return benchmark_from_blocks(blocks)


def multi_execution_paired_manifest(benchmark=None, adaptive_utility=1.0):
    benchmark = benchmark or multi_execution_benchmark_report()
    paired = paired_manifest(benchmark)
    benchmark_blocks = benchmark["canonical_identity"]["evaluation_universe"][
        "blocks"
    ]
    plan_blocks = []
    for block_index, block in enumerate(benchmark_blocks):
        executions = []
        for execution_index, execution in enumerate(block["executions"]):
            executions.append(
                {
                    **copy.deepcopy(execution),
                    "target_bucket": "multi",
                    "start_timestamp_ms": block["start_timestamp_ms"],
                    "end_timestamp_ms": block["end_timestamp_ms"],
                    "segment_identity_sha256": (
                        f"{block_index * 10 + execution_index + 300:064x}"
                    ),
                    "replay_csv": (
                        f"/frozen/replay/{execution['execution_id']}.csv"
                    ),
                    "source_feature_sha256": f"{execution_index + 400:064x}",
                    "source_corpus_manifest_sha256": (
                        f"{execution_index + 500:064x}"
                    ),
                }
            )
        plan_blocks.append({**copy.deepcopy(block), "executions": executions})
    paired["exact_block_plan"].update(
        {
            "schema_version": "exact_replay_block_plan_v2",
            "blocks": plan_blocks,
        }
    )

    for arm_name in ("frozen", "adaptive"):
        arm = paired["arms"][arm_name]
        arm_policy = arm["config"]["policy"]
        arm_blocks = []
        for block_index, planned_block in enumerate(plan_blocks):
            execution_rows = []
            for execution_index, planned in enumerate(
                planned_block["executions"]
            ):
                episodes = []
                if arm_name == "frozen" and planned["symbol"] == "BTCUSDT":
                    episodes = [
                        full_episode(
                            arm=arm_name,
                            block_index=block_index,
                            sequence=0,
                            symbol="BTCUSDT",
                            entry_regime="trend",
                            utility=0.0,
                            segment_sha256=planned[
                                "segment_identity_sha256"
                            ],
                            policy=arm_policy,
                        )
                    ]
                elif arm_name == "adaptive" and planned["symbol"] == "BTCUSDT":
                    episodes = [
                        full_episode(
                            arm=arm_name,
                            block_index=block_index,
                            sequence=0,
                            symbol="BTCUSDT",
                            entry_regime="trend",
                            utility=adaptive_utility,
                            segment_sha256=planned[
                                "segment_identity_sha256"
                            ],
                            policy=arm_policy,
                        ),
                        full_episode(
                            arm=arm_name,
                            block_index=block_index,
                            sequence=1,
                            symbol="BTCUSDT",
                            entry_regime="range",
                            utility=adaptive_utility / 2.0,
                            segment_sha256=planned[
                                "segment_identity_sha256"
                            ],
                            policy=arm_policy,
                        ),
                    ]
                elif arm_name == "adaptive":
                    episodes = [
                        full_episode(
                            arm=arm_name,
                            block_index=block_index,
                            sequence=2,
                            symbol="ETHUSDT",
                            entry_regime="defensive",
                            utility=adaptive_utility / 2.0,
                            segment_sha256=planned[
                                "segment_identity_sha256"
                            ],
                            policy=arm_policy,
                        )
                    ]
                evidence = episode_evidence(
                    episodes,
                    planned["segment_identity_sha256"],
                    arm_policy,
                )
                execution_rows.append(
                    {
                        "execution_id": planned["execution_id"],
                        "symbol": planned["symbol"],
                        "planned_entry_regimes": planned[
                            "planned_entry_regimes"
                        ],
                        "event_sha256": planned["event_sha256"],
                        "segment_identity_sha256": planned[
                            "segment_identity_sha256"
                        ],
                        "state_dir": (
                            f"/{arm_name}/{planned_block['block_id']}/"
                            f"execution-{execution_index}/state"
                        ),
                        "initial_weights_sha256": paired["initial_weights"][
                            "sha256"
                        ],
                        "initial_evolution_state_sha256": paired[
                            "initial_evolution_state"
                        ]["sha256"],
                        "historical_state_loaded": False,
                        "continued_from_block_id": None,
                        "trade_bot_exit_code": 0,
                        "assess_exit_code": 0,
                        "execution_policy_identity": copy.deepcopy(arm_policy),
                        "trade_bot_sha256": TRADE_BOT_SHA256,
                        "episode_execution_evidence": evidence,
                        "no_trade_zero_utility": not episodes,
                    }
                )
            arm_blocks.append(
                {
                    "block_id": planned_block["block_id"],
                    "start_timestamp_ms": planned_block[
                        "start_timestamp_ms"
                    ],
                    "end_timestamp_ms": planned_block["end_timestamp_ms"],
                    "event_sha256": planned_block["event_sha256"],
                    "cells": copy.deepcopy(planned_block["cells"]),
                    "executions": execution_rows,
                }
            )
        arm["blocks"] = arm_blocks
    return paired


class EvolutionUpliftValidationTest(unittest.TestCase):
    def validate(self, paired=None, benchmark=None, policy=None):
        benchmark = benchmark or benchmark_report()
        selected_policy = policy if policy is not None else config()
        return UPLIFT.validate_evolution_uplift(
            paired if paired is not None else paired_manifest(benchmark),
            benchmark,
            selected_policy,
            validation_config_sha256=config_sha256(selected_policy),
        )

    def test_positive_complete_eight_block_pair_proves_uplift(self):
        report = self.validate()

        self.assertEqual(report["status"], "UPLIFT_PROVEN")
        self.assertEqual(report["benchmark_id"], benchmark_report()["benchmark_id"])
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

    def test_benchmark_identity_and_complete_policy_byte_drift_are_fail_closed(self):
        canonical_tamper = benchmark_report()
        canonical_tamper["canonical_identity"]["components"]["data"]["logical_id"] = "forged"
        policy_drift = config()
        policy_drift["unrelated_policy_field"] = "drift"
        raw_byte_drift = benchmark_report()
        raw_byte_drift["validation_config_sha256"] = "e" * 64

        cases = (
            (canonical_tamper, config(), config_sha256(), "canonical_identity_hash"),
            (benchmark_report(), policy_drift, config_sha256(policy_drift), "validation_policy_content"),
            (raw_byte_drift, config(), config_sha256(), "validation_config_sha256"),
        )
        for benchmark, policy, policy_sha, expected in cases:
            with self.subTest(expected=expected):
                report = UPLIFT.validate_evolution_uplift(
                    paired_manifest(benchmark),
                    benchmark,
                    policy,
                    validation_config_sha256=policy_sha,
                )
                self.assertEqual(report["status"], "UNVERIFIABLE")
                self.assertTrue(
                    any(expected in item for item in report["missing_evidence"]),
                    report["missing_evidence"],
                )

    def test_exported_artifact_validator_rejects_missing_audits_and_derived_tamper(self):
        benchmark = benchmark_report()
        policy = config()
        report = self.validate(benchmark=benchmark, policy=policy)
        verified = UPLIFT.validate_evolution_uplift_report_artifact(
            report,
            benchmark,
            policy,
            validation_config_sha256=config_sha256(policy),
        )
        self.assertTrue(verified["verified"], verified["errors"])

        for field, mutate in (
            ("arms", lambda item: item.pop("arms")),
            ("blocks", lambda item: item["blocks"].pop()),
            ("cells", lambda item: item["aggregation_cells"].pop()),
            ("bootstrap", lambda item: item["bootstrap"].__setitem__("lower_confidence_bound", -1.0)),
            ("episode_lineage", lambda item: item["arms"]["frozen"]["episodes"][0].__setitem__("first_fill_id", "forged-fill")),
        ):
            with self.subTest(field=field):
                forged = copy.deepcopy(report)
                mutate(forged)
                audit = UPLIFT.validate_evolution_uplift_report_artifact(
                    forged,
                    benchmark,
                    policy,
                    validation_config_sha256=config_sha256(policy),
                )
                self.assertFalse(audit["verified"])
                self.assertTrue(audit["errors"])

    def test_artifact_validator_requires_every_multi_execution_audit(self):
        benchmark = multi_execution_benchmark_report()
        policy = config()
        report = self.validate(
            multi_execution_paired_manifest(benchmark), benchmark, policy
        )
        verified = UPLIFT.validate_evolution_uplift_report_artifact(
            report,
            benchmark,
            policy,
            validation_config_sha256=config_sha256(policy),
        )
        self.assertTrue(verified["verified"], verified["errors"])

        forged = copy.deepcopy(report)
        forged["arms"]["frozen"]["block_audit"][0]["executions"].pop()
        forged["arms"]["frozen"]["blocks"][0]["executions"].pop()
        audit = UPLIFT.validate_evolution_uplift_report_artifact(
            forged,
            benchmark,
            policy,
            validation_config_sha256=config_sha256(policy),
        )
        self.assertFalse(audit["verified"])
        self.assertTrue(
            any("executions" in item for item in audit["errors"]),
            audit["errors"],
        )

    def test_multi_execution_blocks_aggregate_all_symbols_as_one_bootstrap_unit(self):
        benchmark = multi_execution_benchmark_report()
        report = self.validate(
            multi_execution_paired_manifest(benchmark), benchmark
        )

        self.assertEqual(report["status"], "UPLIFT_PROVEN", report["missing_evidence"])
        self.assertEqual(len(report["blocks"]), 8)
        self.assertEqual(len(report["aggregation_cells"]), 24)
        self.assertEqual(report["blocks"][0]["cell_count"], 3)
        self.assertAlmostEqual(report["blocks"][0]["delta"], 2.0)
        self.assertEqual(report["bootstrap"]["sample_size_blocks"], 8)
        self.assertEqual(
            report["bootstrap"]["sampling_unit"],
            "whole_block_with_all_planned_cells",
        )
        self.assertEqual(report["execution_coverage"]["expected_execution_count"], 16)
        self.assertEqual(report["execution_coverage"]["frozen_ratio"], 1.0)
        self.assertEqual(report["execution_coverage"]["adaptive_ratio"], 1.0)
        first_frozen_block = report["arms"]["frozen"]["blocks"][0]
        self.assertEqual(len(first_frozen_block["executions"]), 2)
        self.assertTrue(first_frozen_block["executions"][1]["zero_trade"])
        frozen_eth = next(
            cell
            for cell in report["aggregation_cells"]
            if cell["block_id"] == "multi-block-01"
            and cell["symbol"] == "ETHUSDT"
        )
        self.assertEqual(frozen_eth["frozen_utility"], 0.0)
        self.assertEqual(frozen_eth["frozen_episode_count"], 0)

    def test_multi_execution_complete_nonpositive_blocks_are_not_proven(self):
        benchmark = multi_execution_benchmark_report()
        report = self.validate(
            multi_execution_paired_manifest(benchmark, adaptive_utility=0.0),
            benchmark,
        )

        self.assertEqual(report["status"], "NOT_PROVEN", report["missing_evidence"])
        self.assertEqual(report["bootstrap"]["sample_size_blocks"], 8)
        self.assertEqual(report["bootstrap"]["lower_confidence_bound"], 0.0)

    def test_multi_execution_coverage_and_identity_drift_fail_closed(self):
        def event_drift(paired):
            paired["arms"]["adaptive"]["blocks"][0]["executions"][0][
                "event_sha256"
            ] = "e" * 64

        def segment_drift(paired):
            paired["arms"]["adaptive"]["blocks"][0]["executions"][0][
                "segment_identity_sha256"
            ] = "f" * 64

        def trade_bot_drift(paired):
            paired["arms"]["adaptive"]["blocks"][0]["executions"][0][
                "trade_bot_sha256"
            ] = "d" * 64

        def policy_drift(paired):
            paired["arms"]["adaptive"]["blocks"][0]["executions"][0][
                "execution_policy_identity"
            ]["sha256"] = "c" * 64

        def state_drift(paired):
            paired["arms"]["adaptive"]["blocks"][0]["executions"][0][
                "initial_evolution_state_sha256"
            ] = "b" * 64

        def missing_execution(paired):
            paired["arms"]["adaptive"]["blocks"][0]["executions"].pop()

        def extra_execution(paired):
            extra = copy.deepcopy(
                paired["arms"]["adaptive"]["blocks"][0]["executions"][0]
            )
            extra["execution_id"] = "multi-block-01:DOGEUSDT"
            extra["symbol"] = "DOGEUSDT"
            paired["arms"]["adaptive"]["blocks"][0]["executions"].append(extra)

        def duplicate_execution(paired):
            paired["arms"]["adaptive"]["blocks"][0]["executions"].append(
                copy.deepcopy(
                    paired["arms"]["adaptive"]["blocks"][0]["executions"][0]
                )
            )

        cases = {
            "event_sha256": event_drift,
            "segment_identity_sha256": segment_drift,
            "trade_bot_sha256": trade_bot_drift,
            "execution_policy_identity": policy_drift,
            "initial_evolution_state_sha256": state_drift,
            "execution_coverage": missing_execution,
            "execution_extra": extra_execution,
            "execution_id_duplicate": duplicate_execution,
        }
        for expected, mutate in cases.items():
            with self.subTest(expected=expected):
                benchmark = multi_execution_benchmark_report()
                paired = multi_execution_paired_manifest(benchmark)
                mutate(paired)

                report = self.validate(paired, benchmark)

                self.assertEqual(report["status"], "UNVERIFIABLE")
                self.assertTrue(
                    any(expected in item for item in report["missing_evidence"]),
                    report["missing_evidence"],
                )
                self.assertIsNone(report["blocks"][0]["delta"])

    def test_multi_execution_rejects_wrong_cell_and_polluted_zero_trade(self):
        def wrong_regime(paired):
            episode = paired["arms"]["adaptive"]["blocks"][0][
                "executions"
            ][0]["episode_execution_evidence"]["episodes"][0]
            episode["entry_regime"] = "defensive"

        def polluted_zero(paired):
            evidence = paired["arms"]["frozen"]["blocks"][0][
                "executions"
            ][1]["episode_execution_evidence"]
            evidence["virtual_pnl"] = 999.0

        for expected, mutate in (
            ("planned_entry_regimes", wrong_regime),
            ("aggregate_pollution", polluted_zero),
        ):
            with self.subTest(expected=expected):
                benchmark = multi_execution_benchmark_report()
                paired = multi_execution_paired_manifest(benchmark)
                mutate(paired)

                report = self.validate(paired, benchmark)

                self.assertEqual(report["status"], "UNVERIFIABLE")
                self.assertTrue(
                    any(expected in item for item in report["missing_evidence"]),
                    report["missing_evidence"],
                )

    def test_real_task2_assessor_shape_is_consumed_without_mocked_exit_failure(self):
        benchmark = benchmark_report()
        paired = paired_manifest(benchmark)
        block = paired["arms"]["frozen"]["blocks"][0]
        evidence = real_assessor_episode_evidence(
            block["segment_identity_sha256"],
            paired["arms"]["frozen"]["config"]["policy"],
        )
        block["episode_execution_evidence"] = evidence
        block["assess_exit_code"] = 0

        report = self.validate(paired, benchmark)

        self.assertEqual(report["status"], "UPLIFT_PROVEN")
        self.assertEqual(
            report["arms"]["frozen"]["episodes"][0]["first_fill_id"],
            "fill-open-real",
        )

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
        first["exit_capture"]["realized_pnl_usd"] = 0.35
        first["position_episode"]["realized_net_usd"] = 0.25
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
        sync_evidence_terminal(evidence)
        adaptive_episode = paired["arms"]["adaptive"]["blocks"][0][
            "episode_execution_evidence"
        ]["episodes"][0]
        adaptive_episode["executable_net_utility"] = 2.0
        adaptive_episode["realized_pnl_usd"] = 2.1
        adaptive_episode["exit_capture"]["realized_pnl_usd"] = 2.1
        adaptive_episode["position_episode"]["realized_net_usd"] = 2.0
        adaptive_episode["fills"][1]["price"] = 102.1
        sync_evidence_terminal(
            paired["arms"]["adaptive"]["blocks"][0][
                "episode_execution_evidence"
            ]
        )

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

    def test_zero_trade_assessor_shape_accepts_healthy_or_diagnostic_exit(self):
        for assess_exit in (0, 1):
            with self.subTest(assess_exit=assess_exit):
                benchmark = benchmark_report()
                paired = paired_manifest(benchmark)
                block = paired["arms"]["frozen"]["blocks"][0]
                block["episode_execution_evidence"] = real_zero_trade_assessor_evidence(
                    block["segment_identity_sha256"],
                    paired["arms"]["frozen"]["config"]["policy"],
                )
                block["no_trade_zero_utility"] = True
                block["assess_exit_code"] = assess_exit
                if assess_exit == 1:
                    paired["arms"]["frozen"]["business_gate_status"] = "FAILED"
                    paired["arms"]["frozen"]["exit_code"] = 2
                    paired["arms"]["frozen"]["report"]["status"] = "UNVERIFIABLE"

                report = self.validate(paired, benchmark)

                self.assertEqual(report["status"], "UPLIFT_PROVEN")
                first = report["aggregation_cells"][0]
                self.assertEqual(first["frozen_utility"], 0.0)
                self.assertEqual(first["frozen_episode_count"], 0)

    def test_zero_trade_rejects_aggregate_pollution_or_missing_terminal(self):
        mutations = {
            "aggregate_pollution": lambda evidence: evidence.update(
                {
                    "account_realized_net_usd": 999999.0,
                    "virtual_pnl": 999999.0,
                    "self_evolution_update_count": 999,
                }
            ),
            "terminal_missing": lambda evidence: evidence.pop(
                "terminal_settlement"
            ),
            "terminal_nonzero": lambda evidence: evidence[
                "terminal_settlement"
            ].update({"realized_net_usd": 1.0}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                benchmark = benchmark_report()
                paired = paired_manifest(benchmark)
                block = paired["arms"]["frozen"]["blocks"][0]
                evidence = real_zero_trade_assessor_evidence(
                    block["segment_identity_sha256"],
                    paired["arms"]["frozen"]["config"]["policy"],
                )
                mutate(evidence)
                block["episode_execution_evidence"] = evidence
                block["no_trade_zero_utility"] = True

                report = self.validate(paired, benchmark)

                self.assertEqual(report["status"], "UNVERIFIABLE")
                self.assertTrue(
                    any(name.split("_")[0] in item for item in report["missing_evidence"]),
                    report["missing_evidence"],
                )

    def test_episode_identity_and_economic_component_tampering_is_rejected(self):
        def wrong_episode_id(paired):
            episode = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            episode["evaluator_episode_id"] = "f" * 64

        def missing_first_fill(paired):
            episode = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            episode["first_fill_id"] = ""

        def duplicate_fill(paired):
            episode = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            episode["fills"][1]["fill_id"] = episode["first_fill_id"]
            episode["fill_ids"][1] = episode["first_fill_id"]

        def missing_client_order(paired):
            episode = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            episode["fills"][1]["client_order_id"] = ""

        def missing_order_state(paired):
            episode = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            episode["fills"][0]["order_state_after"] = "missing"

        def mixed_lineage(paired):
            episode = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            episode["fills"][1]["candidate_lineage"]["candidate_id"] = "other"

        def closure_mismatch(paired):
            episode = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            episode["position_episode"]["position_episode_id"] = "other"

        def fee_mismatch(paired):
            episode = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            episode["fee_usd"] = 0.2

        def funding_nonfinite(paired):
            episode = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            episode["funding_paid_usd"] = math.nan

        mutations = {
            "evaluator_episode_id": wrong_episode_id,
            "first_fill_id": missing_first_fill,
            "fill_id_unique": duplicate_fill,
            "client_order_id": missing_client_order,
            "order_state_after": missing_order_state,
            "candidate_lineage": mixed_lineage,
            "position_episode": closure_mismatch,
            "fee_sum": fee_mismatch,
            "funding": funding_nonfinite,
        }
        for expected, mutate in mutations.items():
            with self.subTest(expected=expected):
                benchmark = benchmark_report()
                paired = paired_manifest(benchmark)
                mutate(paired)

                report = self.validate(paired, benchmark)

                self.assertEqual(report["status"], "UNVERIFIABLE")
                self.assertTrue(
                    any(expected in item for item in report["missing_evidence"]),
                    report["missing_evidence"],
                )

    def test_duplicate_evaluator_id_and_cross_segment_fill_reuse_are_rejected(self):
        mutations = {}

        def duplicate_episode_id(paired):
            first = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            second = paired["arms"]["frozen"]["blocks"][1][
                "episode_execution_evidence"
            ]["episodes"][0]
            second["evaluator_episode_id"] = first["evaluator_episode_id"]

        mutations["episode_id_duplicate"] = duplicate_episode_id

        def cross_segment_first_fill(paired):
            first = paired["arms"]["frozen"]["blocks"][0][
                "episode_execution_evidence"
            ]["episodes"][0]
            second = paired["arms"]["frozen"]["blocks"][1][
                "episode_execution_evidence"
            ]["episodes"][0]
            second["first_fill_id"] = first["first_fill_id"]
            second["fill_ids"][0] = first["first_fill_id"]
            second["fills"][0]["fill_id"] = first["first_fill_id"]
            second["evaluator_episode_id"] = hashlib.sha256(
                (
                    f"{second['segment_identity_sha256']}:"
                    f"{second['symbol']}:{second['first_fill_id']}"
                ).encode("ascii")
            ).hexdigest()

        mutations["first_fill_id_cross_segment_reuse"] = cross_segment_first_fill

        for expected, mutate in mutations.items():
            with self.subTest(expected=expected):
                benchmark = benchmark_report()
                paired = paired_manifest(benchmark)
                mutate(paired)

                report = self.validate(paired, benchmark)

                self.assertEqual(report["status"], "UNVERIFIABLE")
                self.assertTrue(
                    any(expected in item for item in report["missing_evidence"]),
                    report["missing_evidence"],
                )

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
