#!/usr/bin/env python3

import copy
import hashlib
import json
import math
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import validate_objective_alignment as alignment  # noqa: E402


SUBSYSTEMS = ("miner", "market_alpha", "microstructure", "online_tuner")


def canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def config_bytes(policy=None):
    return json.dumps(
        policy if policy is not None else config(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def config_sha256(policy=None):
    return hashlib.sha256(config_bytes(policy)).hexdigest()


def benchmark_report(block_count=5, policy=None, policy_sha256=None):
    policy = copy.deepcopy(policy if policy is not None else config())
    policy_sha256 = policy_sha256 or config_sha256(policy)
    blocks = []
    for index in range(block_count):
        blocks.append(
            {
                "block_id": f"block-{index + 1:02d}",
                "start_timestamp_ms": index * 1000,
                "end_timestamp_ms": index * 1000 + 999,
                "event_sha256": f"{index + 2:064x}",
                "cells": [{"symbol": "BTCUSDT", "entry_regime": "trend"}],
            }
        )
    canonical_identity = {
        "schema_version": "decision_evidence_benchmark_v1",
        "components": {
            name: {
                "logical_id": f"{name}-v1",
                "files": [{"logical_name": name, "sha256": f"{index + 100:064x}"}],
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


def config():
    return {
        "schema_version": "decision_evidence_validation_v1",
        "alignment": {
            "min_candidates": 8,
            "min_independent_blocks": 5,
            "alpha": 0.05,
            "permutation_trials": 10000,
        },
    }


def candidates(scores=None, utilities=None, direction="higher_is_better"):
    scores = list(scores if scores is not None else range(8))
    utilities = list(utilities if utilities is not None else range(8))
    blocks = benchmark_report()["canonical_identity"]["evaluation_universe"]["blocks"]
    result = []
    for index, (score, utility) in enumerate(zip(scores, utilities)):
        result.append(
            {
                "candidate_id": f"candidate-{index:02d}",
                "internal_score": score,
                "score_direction": direction,
                "blocks": [
                    {
                        "block_id": block["block_id"],
                        "start_timestamp_ms": block["start_timestamp_ms"],
                        "end_timestamp_ms": block["end_timestamp_ms"],
                        "independent_oos": True,
                        "event_sha256": block["event_sha256"],
                        "execution_path_complete": True,
                        "utility_source": "complete_execution_replay",
                        "executable_net_utility": float(utility) + block_index / 100.0,
                    }
                    for block_index, block in enumerate(blocks)
                ],
            }
        )
    return result


def evidence(candidate_payload=None):
    payload = candidate_payload if candidate_payload is not None else candidates()
    return {
        "schema_version": "candidate_alignment_evidence_v1",
        "benchmark_id": benchmark_report()["benchmark_id"],
        "subsystems": {
            subsystem: {
                "permutation_unit": "candidate_aggregate_utility",
                "candidates": copy.deepcopy(payload),
            }
            for subsystem in SUBSYSTEMS
        },
    }


class ObjectiveAlignmentValidationTest(unittest.TestCase):
    def validate(self, payload=None, benchmark=None, policy=None):
        selected_policy = policy if policy is not None else config()
        return alignment.validate_alignment(
            payload if payload is not None else evidence(),
            benchmark if benchmark is not None else benchmark_report(),
            selected_policy,
            validation_config_sha256=config_sha256(selected_policy),
        )

    def test_all_fixed_subsystems_align_and_lower_direction_and_ties_are_normalized(self):
        payload = evidence()
        payload["subsystems"]["market_alpha"]["candidates"] = candidates(
            scores=range(8, 0, -1),
            utilities=range(8),
            direction="lower_is_better",
        )
        tied = [1, 1, 2, 2, 3, 3, 4, 4]
        payload["subsystems"]["microstructure"]["candidates"] = candidates(
            scores=tied,
            utilities=tied,
        )

        report = self.validate(payload)

        self.assertEqual(tuple(report["subsystems"]), SUBSYSTEMS)
        for subsystem in SUBSYSTEMS:
            section = report["subsystems"][subsystem]
            self.assertEqual(section["status"], "ALIGNED")
            self.assertGreater(section["rho"], 0.0)
            self.assertLessEqual(section["p_value"], 0.05)
            self.assertEqual(section["candidate_count"], 8)
            self.assertEqual(section["independent_block_count"], 5)
            self.assertEqual(section["permutation"]["unit"], "candidate_aggregate_utility")
            self.assertEqual(section["permutation"]["trials"], 10000)
            self.assertEqual(len(section["candidate_audit"]), 8)
            self.assertEqual(len(section["candidate_audit"][0]["blocks"]), 5)
        market_scores = [
            item["normalized_internal_score"]
            for item in report["subsystems"]["market_alpha"]["candidate_audit"]
        ]
        self.assertEqual(market_scores, [-8.0, -7.0, -6.0, -5.0, -4.0, -3.0, -2.0, -1.0])
        self.assertAlmostEqual(report["subsystems"]["microstructure"]["rho"], 1.0)
        self.assertEqual(alignment.average_ranks([1.0, 1.0, 3.0]), [1.5, 1.5, 3.0])

    def test_complete_negative_or_insignificant_evidence_is_not_aligned(self):
        payload = evidence()
        payload["subsystems"]["miner"]["candidates"] = candidates(
            scores=range(8), utilities=range(7, -1, -1)
        )
        payload["subsystems"]["online_tuner"]["candidates"] = candidates(
            scores=range(8), utilities=[0, 7, 1, 6, 2, 5, 3, 4]
        )

        report = self.validate(payload)

        self.assertEqual(report["subsystems"]["miner"]["status"], "NOT_ALIGNED")
        self.assertLess(report["subsystems"]["miner"]["rho"], 0.0)
        self.assertEqual(report["subsystems"]["online_tuner"]["status"], "NOT_ALIGNED")
        self.assertGreater(report["subsystems"]["online_tuner"]["p_value"], 0.05)
        self.assertEqual(report["subsystems"]["market_alpha"]["status"], "ALIGNED")

    def test_incomplete_candidate_evidence_is_unverifiable_with_precise_missing_fields(self):
        mutations = {
            "too_few_candidates": lambda section: section.__setitem__("candidates", section["candidates"][:7]),
            "duplicate_candidate_id": lambda section: section["candidates"][1].__setitem__("candidate_id", "candidate-00"),
            "missing_direction": lambda section: section["candidates"][0].pop("score_direction"),
            "mixed_directions": lambda section: section["candidates"][0].__setitem__("score_direction", "lower_is_better"),
            "non_finite_score": lambda section: section["candidates"][0].__setitem__("internal_score", math.nan),
            "missing_utility": lambda section: section["candidates"][0]["blocks"][0].pop("executable_net_utility"),
            "non_finite_utility": lambda section: section["candidates"][0]["blocks"][0].__setitem__("executable_net_utility", math.inf),
            "too_few_blocks": lambda section: [candidate.__setitem__("blocks", candidate["blocks"][:4]) for candidate in section["candidates"]],
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                payload = evidence()
                mutate(payload["subsystems"]["miner"])
                section = self.validate(payload)["subsystems"]["miner"]
                self.assertEqual(section["status"], "UNVERIFIABLE")
                self.assertTrue(section["missing_fields"])
                self.assertIsNone(section["rho"])
                self.assertIsNone(section["p_value"])

        mismatched = evidence()
        mismatched["benchmark_id"] = "f" * 64
        report = self.validate(mismatched)
        self.assertTrue(
            all(section["status"] == "UNVERIFIABLE" for section in report["subsystems"].values())
        )
        self.assertEqual(report["expected_benchmark_id"], benchmark_report()["benchmark_id"])
        self.assertEqual(report["actual_benchmark_id"], "f" * 64)

    def test_block_universe_identity_independence_and_permutation_unit_are_fail_closed(self):
        cases = {}

        selected = evidence()
        for candidate in selected["subsystems"]["miner"]["candidates"]:
            candidate["blocks"] = candidate["blocks"][:-1]
        cases["candidate_selected_blocks"] = selected

        event_drift = evidence()
        event_drift["subsystems"]["miner"]["candidates"][0]["blocks"][0]["event_sha256"] = "e" * 64
        cases["event_drift"] = event_drift

        interval_drift = evidence()
        interval_drift["subsystems"]["miner"]["candidates"][0]["blocks"][0]["end_timestamp_ms"] += 1
        cases["interval_drift"] = interval_drift

        dependent = evidence()
        dependent["subsystems"]["miner"]["candidates"][0]["blocks"][0]["independent_oos"] = False
        cases["dependent_block"] = dependent

        wrong_unit = evidence()
        wrong_unit["subsystems"]["miner"]["permutation_unit"] = "per_block_rows"
        cases["wrong_permutation_unit"] = wrong_unit

        for name, payload in cases.items():
            with self.subTest(name=name):
                section = self.validate(payload)["subsystems"]["miner"]
                self.assertEqual(section["status"], "UNVERIFIABLE")
                self.assertTrue(section["missing_fields"])

        overlapping = benchmark_report()
        overlapping["canonical_identity"]["evaluation_universe"]["blocks"][1]["start_timestamp_ms"] = 999
        report = self.validate(benchmark=overlapping)
        self.assertTrue(
            all(section["status"] == "UNVERIFIABLE" for section in report["subsystems"].values())
        )
        self.assertIn("benchmark.evaluation_universe.non_overlapping", report["missing_fields"])

    def test_alignment_policy_cannot_drift_from_the_frozen_v1_contract(self):
        drifted = config()
        drifted["alignment"]["permutation_trials"] = 9999

        report = self.validate(policy=drifted)

        self.assertEqual(report["overall_status"], "UNVERIFIABLE")
        self.assertIn("config.alignment=frozen_v1_contract", report["missing_fields"])
        self.assertTrue(
            all(section["status"] == "UNVERIFIABLE" for section in report["subsystems"].values())
        )

    def test_benchmark_canonical_identity_and_full_policy_bytes_are_fail_closed(self):
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
                report = alignment.validate_alignment(
                    evidence(),
                    benchmark,
                    policy,
                    validation_config_sha256=policy_sha,
                )
                self.assertEqual(report["overall_status"], "UNVERIFIABLE")
                self.assertTrue(
                    any(expected in item for item in report["missing_fields"]),
                    report["missing_fields"],
                )

    def test_exported_artifact_validator_rejects_skeleton_and_derived_stat_tamper(self):
        benchmark = benchmark_report()
        policy = config()
        report = self.validate(benchmark=benchmark, policy=policy)
        verified = alignment.validate_alignment_report_artifact(
            report,
            benchmark,
            policy,
            validation_config_sha256=config_sha256(policy),
        )
        self.assertTrue(verified["verified"], verified["errors"])

        for field, mutate in (
            ("candidate_audit", lambda item: item["subsystems"]["miner"].pop("candidate_audit")),
            ("rho", lambda item: item["subsystems"]["miner"].__setitem__("rho", 0.123)),
            ("permutation", lambda item: item["subsystems"]["miner"]["permutation"].__setitem__("p_value", 0.000001)),
        ):
            with self.subTest(field=field):
                forged = copy.deepcopy(report)
                mutate(forged)
                audit = alignment.validate_alignment_report_artifact(
                    forged,
                    benchmark,
                    policy,
                    validation_config_sha256=config_sha256(policy),
                )
                self.assertFalse(audit["verified"])
                self.assertTrue(audit["errors"])

    def test_proxy_metrics_cannot_be_declared_as_executable_utility(self):
        for source in ("ic", "auc", "rmse", "oracle", "train_score", "virtual_pnl"):
            with self.subTest(source=source):
                payload = evidence()
                block = payload["subsystems"]["miner"]["candidates"][0]["blocks"][0]
                block["utility_source"] = source
                section = self.validate(payload)["subsystems"]["miner"]
                self.assertEqual(section["status"], "UNVERIFIABLE")
                self.assertIn(
                    "candidates[0].blocks[0].utility_source=complete_execution_replay",
                    section["missing_fields"],
                )

    def test_current_report_adapter_preserves_proxy_candidates_but_never_invents_utility(self):
        adapted = alignment.adapt_current_reports(
            benchmark_id=benchmark_report()["benchmark_id"],
            miner_report={
                "factor_set_version": "f-v1",
                "factors": [{"expression": "close/ema", "objective_score": 0.4}],
            },
            market_alpha_report={
                "schema_version": "market_alpha_development_verification_v1",
                "economic_screen": {
                    "reports": [
                        {
                            "feature_set": "expanded_market_alpha_v1",
                            "variants": [
                                {
                                    "variant": "continuous_return_huber",
                                    "model_net_edge_lcb_bps": 0.7,
                                }
                            ],
                        }
                    ]
                },
            },
            microstructure_report={
                "schema_version": "microstructure_target_architecture_comparison_v1",
                "architectures": {
                    "direct_net_utility": {
                        "oos_stress_cost_by_split": {"lcb_bps": 0.2}
                    }
                },
            },
            online_tuner_report={
                "metrics": {
                    "self_evolution_factor_ic_action_count": 4,
                    "self_evolution_effective_update_count": 3,
                }
            },
        )

        self.assertEqual(tuple(adapted["subsystems"]), SUBSYSTEMS)
        self.assertEqual(
            adapted["subsystems"]["miner"]["candidates"][0]["internal_score"], 0.4
        )
        self.assertNotIn(
            "executable_net_utility",
            adapted["subsystems"]["miner"]["candidates"][0],
        )
        for subsystem in SUBSYSTEMS:
            section = adapted["subsystems"][subsystem]
            self.assertIn("adapter_missing_fields", section)
            self.assertIn("candidate_level_complete_execution_utility", section["adapter_missing_fields"])

        report = self.validate(adapted)
        for subsystem in SUBSYSTEMS:
            self.assertEqual(report["subsystems"][subsystem]["status"], "UNVERIFIABLE")
            self.assertTrue(report["subsystems"][subsystem]["missing_fields"])

    def test_missing_one_subsystem_does_not_remove_other_results_and_sampling_is_deterministic(self):
        payload = evidence()
        del payload["subsystems"]["miner"]
        first = self.validate(payload)
        second = self.validate(payload)

        self.assertEqual(first, second)
        self.assertEqual(first["subsystems"]["miner"]["status"], "UNVERIFIABLE")
        for subsystem in SUBSYSTEMS[1:]:
            self.assertEqual(first["subsystems"][subsystem]["status"], "ALIGNED")
            self.assertEqual(first["subsystems"][subsystem]["permutation"]["method"], "deterministic_sha256_order")

    def test_small_permutation_space_is_exhaustively_enumerated(self):
        result = alignment._permutation_result(
            benchmark_id=benchmark_report()["benchmark_id"],
            subsystem="miner",
            candidate_ids=["a", "b", "c"],
            normalized_scores=[1.0, 2.0, 3.0],
            utilities=[1.0, 2.0, 3.0],
            configured_trials=10000,
            observed_rho=1.0,
        )
        self.assertEqual(result["method"], "exact_enumeration")
        self.assertEqual(result["trials"], math.factorial(3))
        self.assertEqual(result["exceedance_count"], 1)
        self.assertEqual(result["p_value"], 2.0 / 7.0)

    def test_cli_writes_report_and_returns_nonzero_for_unverifiable_adapter_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            paths = {}
            payloads = {
                "benchmark": benchmark_report(),
                "config": config(),
                "miner": {"factors": []},
                "market": {"economic_screen": {"reports": []}},
                "micro": {"architectures": {}},
                "tuner": {"metrics": {}},
            }
            for name, payload in payloads.items():
                path = root / f"{name}.json"
                encoded = json.dumps(payload).encode("utf-8")
                path.write_bytes(encoded)
                paths[name] = path
            policy_sha = hashlib.sha256(paths["config"].read_bytes()).hexdigest()
            payloads["benchmark"] = benchmark_report(policy_sha256=policy_sha)
            paths["benchmark"].write_text(json.dumps(payloads["benchmark"]), encoding="utf-8")
            output = root / "alignment.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(alignment.__file__)),
                    "--benchmark-report",
                    str(paths["benchmark"]),
                    "--config",
                    str(paths["config"]),
                    "--miner-report",
                    str(paths["miner"]),
                    "--market-alpha-report",
                    str(paths["market"]),
                    "--microstructure-report",
                    str(paths["micro"]),
                    "--online-tuner-report",
                    str(paths["tuner"]),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2, completed.stderr)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["overall_status"], "UNVERIFIABLE")
            self.assertEqual(tuple(written["subsystems"]), SUBSYSTEMS)


if __name__ == "__main__":
    unittest.main()
