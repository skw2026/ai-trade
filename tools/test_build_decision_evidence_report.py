#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from functools import lru_cache
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import build_decision_evidence_report as builder  # noqa: E402
import experiment_budget_ledger as ledger  # noqa: E402
import test_validate_evolution_uplift as uplift_fixtures  # noqa: E402
import validate_objective_alignment as alignment_validator  # noqa: E402


SUBSYSTEMS = ("miner", "market_alpha", "microstructure", "online_tuner")
UNSET = object()


def validation_config():
    return {
        "schema_version": "decision_evidence_validation_v1",
        "alignment": {
            "min_candidates": 8,
            "min_independent_blocks": 5,
            "alpha": 0.05,
            "permutation_trials": 10000,
        },
        "uplift": {
            "min_independent_blocks": 8,
            "block_coverage": 1,
            "bootstrap_trials": 10000,
            "lcb": 0.95,
        },
        "failure_budgets": {"family": 3, "information_set": 8},
        "seed": {
            "source": "benchmark_id+channel",
            "cli_override_allowed": False,
        },
    }


VALIDATION_CONFIG = validation_config()
VALIDATION_CONFIG_BYTES = json.dumps(
    VALIDATION_CONFIG,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
    allow_nan=False,
).encode("ascii")
VALIDATION_CONFIG_SHA256 = hashlib.sha256(VALIDATION_CONFIG_BYTES).hexdigest()
_BASE_BLOCKS = copy.deepcopy(
    uplift_fixtures.benchmark_report()["canonical_identity"]
    ["evaluation_universe"]["blocks"]
)
_BENCHMARK_REPORT = uplift_fixtures.benchmark_from_blocks(
    _BASE_BLOCKS,
    policy=VALIDATION_CONFIG,
    policy_sha256=VALIDATION_CONFIG_SHA256,
)
BENCHMARK_ID = _BENCHMARK_REPORT["benchmark_id"]


def benchmark_report():
    return copy.deepcopy(_BENCHMARK_REPORT)


@lru_cache(maxsize=None)
def _alignment_report_cached(status_tuple):
    statuses = dict(zip(SUBSYSTEMS, status_tuple))
    blocks = benchmark_report()["canonical_identity"]["evaluation_universe"]["blocks"]
    subsystems = {}
    for subsystem in SUBSYSTEMS:
        utilities = (
            list(range(7, -1, -1))
            if statuses[subsystem] == "NOT_ALIGNED"
            else list(range(8))
        )
        candidates = []
        for candidate_index, utility in enumerate(utilities):
            candidates.append(
                {
                    "candidate_id": f"{subsystem}-candidate-{candidate_index:02d}",
                    "internal_score": float(candidate_index),
                    "score_direction": "higher_is_better",
                    "blocks": [
                        {
                            "block_id": block["block_id"],
                            "start_timestamp_ms": block["start_timestamp_ms"],
                            "end_timestamp_ms": block["end_timestamp_ms"],
                            "event_sha256": block["event_sha256"],
                            "independent_oos": True,
                            "execution_path_complete": True,
                            "utility_source": "complete_execution_replay",
                            "executable_net_utility": float(utility) + block_index / 100.0,
                        }
                        for block_index, block in enumerate(blocks)
                    ],
                }
            )
        if statuses[subsystem] == "UNVERIFIABLE":
            candidates.pop()
        subsystems[subsystem] = {
            "permutation_unit": "candidate_aggregate_utility",
            "candidates": candidates,
        }
    return alignment_validator.validate_alignment(
        {
            "schema_version": "candidate_alignment_evidence_v1",
            "benchmark_id": BENCHMARK_ID,
            "subsystems": subsystems,
        },
        benchmark_report(),
        VALIDATION_CONFIG,
        validation_config_sha256=VALIDATION_CONFIG_SHA256,
    )


def alignment_report(statuses=None):
    statuses = statuses or {name: "ALIGNED" for name in SUBSYSTEMS}
    return copy.deepcopy(
        _alignment_report_cached(tuple(statuses[name] for name in SUBSYSTEMS))
    )


@lru_cache(maxsize=None)
def _uplift_report_cached(status):
    benchmark = benchmark_report()
    if status == "UPLIFT_PROVEN":
        paired = uplift_fixtures.paired_manifest(benchmark)
    elif status == "NOT_PROVEN":
        paired = uplift_fixtures.paired_manifest(
            benchmark, frozen_utility=1.0, adaptive_utility=1.0
        )
    else:
        paired = uplift_fixtures.paired_manifest(benchmark)
        paired["arms"]["adaptive"]["blocks"].pop()
    report = uplift_fixtures.UPLIFT.validate_evolution_uplift(
        paired,
        benchmark,
        VALIDATION_CONFIG,
        validation_config_sha256=VALIDATION_CONFIG_SHA256,
    )
    if status not in {"UPLIFT_PROVEN", "NOT_PROVEN", "UNVERIFIABLE"}:
        report["status"] = status
    return report


def uplift_report(status="UPLIFT_PROVEN"):
    return copy.deepcopy(_uplift_report_cached(status))


def forged_ledger_skeleton(
    decision="ALLOW_NEXT_EXPERIMENT",
    *,
    registration_verified=None,
    experiment_id="experiment-001",
):
    if registration_verified is None:
        registration_verified = decision in {
            "ALLOW_NEXT_EXPERIMENT",
            "STOP_CURRENT_FAMILY",
        }
    return {
        "schema_version": "experiment_budget_ledger_decision_v1",
        "operation": "audit-next",
        "decision": decision,
        "appended": False,
        "benchmark_id": BENCHMARK_ID,
        "expected_benchmark_id": BENCHMARK_ID,
        "actual_benchmark_id": BENCHMARK_ID,
        "experiment_id": experiment_id,
        "registration_verified": registration_verified,
        "benchmark_verified": True,
        "validation_policy_sha256": VALIDATION_CONFIG_SHA256,
        "hypothesis_family_id": "2" * 64,
        "information_set_id": "3" * 64,
        "remaining_budgets": {"family": 2, "information_set": 7},
        "checkpoint_recovery_required": False,
        "checkpoint_recovered": False,
        "registration_nonce": "4" * 64,
        "actual_proposal_sha256": "5" * 64,
        "registered_proposal_sha256": "5" * 64,
        "registration_record_hash": "6" * 64,
        "result_source_path": "/tmp/decision-evidence-result.json",
        "ledger_record_count": 1,
        "ledger_tail_record_hash": "7" * 64,
        "mismatches": [],
        "reasons": [],
    }


def alpha_route_report(status="FAIL"):
    return {
        "schema_version": "alpha_source_route_v1",
        "status": status,
        "selected_route": None,
        "reason": "no_independently_gated_alpha_source_ready",
    }


class DecisionEvidenceReportTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = pathlib.Path(self.temp_dir.name).resolve(strict=False)
        self.config_path = self.root / "decision_evidence_validation.json"
        self.config_path.write_bytes(VALIDATION_CONFIG_BYTES)
        self.benchmark_path = self.root / "benchmark.json"
        self.benchmark_path.write_text(
            json.dumps(benchmark_report()), encoding="utf-8"
        )
        self.ledger_path = self.root / "experiments.jsonl"
        self.proposal = self._proposal("experiment-001")
        self.proposal_path = self.root / "proposal.json"
        self.proposal_path.write_text(
            json.dumps(self.proposal), encoding="utf-8"
        )
        registered = ledger.register_experiment(
            self.ledger_path,
            self.config_path,
            self.proposal,
            benchmark_report(),
        )
        self.assertEqual(registered["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.authoritative_ledger_reaudit = ledger.audit_next_experiment(
            self.ledger_path,
            self.config_path,
            self.proposal,
            benchmark_report(),
        )
        self.assertEqual(
            self.authoritative_ledger_reaudit["decision"],
            "ALLOW_NEXT_EXPERIMENT",
        )

    def _proposal(self, experiment_id, *, result_prefix="result"):
        information_definition = {
            "data": "d" * 64,
            "features": "f" * 64,
            "actions": "a" * 64,
        }
        information_id = ledger.stable_definition_id(information_definition)
        family_definition = {
            "mechanism": "lead-lag",
            "target": "net-utility",
        }
        return {
            "experiment_id": experiment_id,
            "benchmark_id": BENCHMARK_ID,
            "validation_policy_sha256": VALIDATION_CONFIG_SHA256,
            "information_set_definition": information_definition,
            "information_set_id": information_id,
            "hypothesis_family_definition": family_definition,
            "hypothesis_family_id": ledger.stable_family_id(
                information_id, family_definition
            ),
            "display_name": "real ledger fixture",
            "changed_dimensions": [
                {"name": "target", "before": "a", "after": "b"}
            ],
            "expected_direction": "increase",
            "stop_condition": {
                "metric": "stress_lcb",
                "operator": "gt",
                "value": 0.0,
            },
            "result_source_path": str(
                (self.root / f"{result_prefix}-{experiment_id}.json").resolve(
                    strict=False
                )
            ),
        }

    def _real_stopped_ledger_reaudit(self):
        cached = getattr(self, "_stopped_reaudit", None)
        if cached is not None:
            return copy.deepcopy(cached)
        ledger_path = self.root / "stopped-experiments.jsonl"
        pending = self._proposal("pending-after-budget", result_prefix="stopped")
        self.assertEqual(
            ledger.register_experiment(
                ledger_path, self.config_path, pending, benchmark_report()
            )["decision"],
            "ALLOW_NEXT_EXPERIMENT",
        )
        for index in range(3):
            proposal = self._proposal(
                f"failed-{index}", result_prefix="stopped"
            )
            self.assertEqual(
                ledger.register_experiment(
                    ledger_path, self.config_path, proposal, benchmark_report()
                )["decision"],
                "ALLOW_NEXT_EXPERIMENT",
            )
            registration = ledger.audit_ledger(ledger_path)["registrations"][
                proposal["experiment_id"]
            ]
            result_without_identity = {
                "schema_version": "decision_experiment_result_v1",
                "experiment_id": proposal["experiment_id"],
                "registration_nonce": registration["registration_nonce"],
                "outcome": "FALSIFIED",
                "result": {"stress_lcb": -1.0},
            }
            result = {
                **result_without_identity,
                "result_identity": ledger.canonical_sha256(
                    result_without_identity
                ),
            }
            result_path = pathlib.Path(proposal["result_source_path"])
            result_path.write_bytes(ledger.canonical_json_bytes(result) + b"\n")
            result_path.chmod(0o444)
            observed = ledger.observe_experiment(
                ledger_path,
                self.config_path,
                {"experiment_id": proposal["experiment_id"]},
                benchmark_report(),
            )
            self.assertIn(
                observed["decision"],
                {"ALLOW_NEXT_EXPERIMENT", "STOP_CURRENT_FAMILY"},
            )
        self._stopped_reaudit = ledger.audit_next_experiment(
            ledger_path,
            self.config_path,
            pending,
            benchmark_report(),
        )
        self.assertEqual(
            self._stopped_reaudit["decision"], "STOP_CURRENT_FAMILY"
        )
        return copy.deepcopy(self._stopped_reaudit)

    def build(
        self,
        benchmark=UNSET,
        alignment=UNSET,
        uplift=UNSET,
        ledger=UNSET,
        alpha_route=UNSET,
        authoritative_ledger_reaudit=UNSET,
    ):
        ledger_input = (
            copy.deepcopy(self.authoritative_ledger_reaudit)
            if ledger is UNSET
            else ledger
        )
        authoritative = (
            copy.deepcopy(self.authoritative_ledger_reaudit)
            if authoritative_ledger_reaudit is UNSET
            else authoritative_ledger_reaudit
        )
        return builder.build_report(
            benchmark_report() if benchmark is UNSET else benchmark,
            alignment_report() if alignment is UNSET else alignment,
            uplift_report() if uplift is UNSET else uplift,
            ledger_input,
            alpha_route_report() if alpha_route is UNSET else alpha_route,
            validation_policy=VALIDATION_CONFIG,
            validation_config_sha256=VALIDATION_CONFIG_SHA256,
            authoritative_ledger_reaudit=authoritative,
        )

    def assert_no_authority(self, report):
        self.assertFalse(report["promotion_authority"])
        self.assertFalse(report["demo_activation_authorized"])
        self.assertFalse(report["live_activation_authorized"])
        self.assertTrue(report["research_decision_only"])

    def test_all_three_decisive_evidence_channels_must_pass_to_continue(self):
        report = self.build()

        self.assertEqual(report["research_decision"], "CONTINUE")
        self.assertEqual(report["reason_codes"], ["DECISIVE_EVIDENCE_ALL_PASSED"])
        self.assertEqual(report["benchmark"]["status"], "VERIFIED")
        self.assertEqual(report["alignment"]["status"], "ALIGNED")
        self.assertEqual(report["uplift"]["status"], "UPLIFT_PROVEN")
        self.assertEqual(report["ledger"]["status"], "ALLOW_NEXT_EXPERIMENT")
        self.assertEqual(report["authorized_experiment_id"], "experiment-001")
        self.assertEqual(report["ledger"]["experiment_id"], "experiment-001")
        self.assertTrue(report["ledger"]["registration_verified"])
        self.assertTrue(report["ledger"]["registration_audit"]["verified"])
        self.assert_no_authority(report)

    def test_direct_api_cannot_continue_without_authoritative_ledger_reaudit(self):
        report = self.build(authoritative_ledger_reaudit=None)

        self.assertEqual(report["research_decision"], "STOP")
        self.assertEqual(report["ledger"]["status"], "UNVERIFIABLE")
        self.assert_no_authority(report)

    def test_forged_all_hex_ledger_skeleton_cannot_replace_authoritative_reaudit(self):
        forged = forged_ledger_skeleton()
        authoritative = copy.deepcopy(forged)
        authoritative["registration_nonce"] = "9" * 64

        report = builder.build_report(
            benchmark_report(),
            alignment_report(),
            uplift_report(),
            forged,
            alpha_route_report(),
            validation_policy=VALIDATION_CONFIG,
            validation_config_sha256=VALIDATION_CONFIG_SHA256,
            authoritative_ledger_reaudit=authoritative,
        )

        self.assertEqual(report["research_decision"], "STOP")
        self.assertFalse(report["ledger"]["registration_audit"]["verified"])
        self.assertTrue(report["ledger"]["validation_errors"])

    def test_budget_decision_must_match_remaining_budget_and_canonical_reason(self):
        for name, authoritative in (
            (
                "allow_with_zero",
                {
                    **forged_ledger_skeleton(),
                    "remaining_budgets": {"family": 0, "information_set": 7},
                },
            ),
            (
                "stop_without_zero",
                {
                    **forged_ledger_skeleton("STOP_CURRENT_FAMILY"),
                    "reasons": ["failure budget is exhausted"],
                },
            ),
        ):
            with self.subTest(name=name):
                report = builder.build_report(
                    benchmark_report(),
                    alignment_report(),
                    uplift_report(),
                    authoritative,
                    alpha_route_report(),
                    validation_policy=VALIDATION_CONFIG,
                    validation_config_sha256=VALIDATION_CONFIG_SHA256,
                    authoritative_ledger_reaudit=authoritative,
                )
                self.assertEqual(report["research_decision"], "STOP")
                self.assertEqual(report["ledger"]["status"], "UNVERIFIABLE")

    def test_child_validator_exception_is_fail_closed(self):
        cases = (
            ("benchmark", "validate_verified_benchmark_report"),
            ("alignment", "validate_alignment_report_artifact"),
            ("uplift", "validate_evolution_uplift_report_artifact"),
        )
        for channel, validator_name in cases:
            with self.subTest(channel=channel), mock.patch.object(
                builder,
                validator_name,
                side_effect=RuntimeError("malformed child"),
            ):
                report = builder.build_report(
                    benchmark_report(),
                    alignment_report(),
                    uplift_report(),
                    self.authoritative_ledger_reaudit,
                    alpha_route_report(),
                    validation_policy=VALIDATION_CONFIG,
                    validation_config_sha256=VALIDATION_CONFIG_SHA256,
                    authoritative_ledger_reaudit=(
                        self.authoritative_ledger_reaudit
                    ),
                )

            self.assertEqual(report["research_decision"], "STOP")
            self.assertEqual(report[channel]["status"], "UNVERIFIABLE")
            self.assert_no_authority(report)

    def test_self_reported_positive_skeletons_and_derived_tampering_cannot_continue(self):
        skeleton_alignment = {
            "schema_version": "objective_alignment_validation_v1",
            "benchmark_id": BENCHMARK_ID,
            "expected_benchmark_id": BENCHMARK_ID,
            "actual_benchmark_id": BENCHMARK_ID,
            "overall_status": "ALIGNED",
            "subsystems": {
                name: {"status": "ALIGNED", "rho": 1.0, "p_value": 0.0}
                for name in SUBSYSTEMS
            },
        }
        skeleton_uplift = {
            "schema_version": "evolution_uplift_validation_v1",
            "status": "UPLIFT_PROVEN",
            "benchmark_id": BENCHMARK_ID,
            "expected_benchmark_id": BENCHMARK_ID,
            "actual_benchmark_id": BENCHMARK_ID,
            "bootstrap": {"lower_confidence_bound": 1.0},
        }
        tampered_uplift = uplift_report()
        tampered_uplift["bootstrap"]["lower_confidence_bound"] = -1.0
        for name, overrides in (
            ("alignment_skeleton", {"alignment": skeleton_alignment}),
            ("uplift_skeleton", {"uplift": skeleton_uplift}),
            ("uplift_derived_tamper", {"uplift": tampered_uplift}),
        ):
            with self.subTest(name=name):
                report = self.build(**overrides)
                self.assertEqual(report["research_decision"], "STOP")
                self.assert_no_authority(report)

    def test_forged_allow_without_verified_pending_registration_stops(self):
        cases = []
        not_verified = copy.deepcopy(self.authoritative_ledger_reaudit)
        not_verified["registration_verified"] = False
        cases.append(("not_verified", not_verified))
        missing_verification = copy.deepcopy(self.authoritative_ledger_reaudit)
        missing_verification.pop("registration_verified")
        cases.append(("missing_verification", missing_verification))
        for value in (None, "", "bad experiment id"):
            cases.append(
                (
                    f"experiment_id={value!r}",
                    {
                        **copy.deepcopy(self.authoritative_ledger_reaudit),
                        "experiment_id": value,
                    },
                )
            )
        mismatch_reason = copy.deepcopy(self.authoritative_ledger_reaudit)
        mismatch_reason["reasons"] = ["audit-next proposal does not match preregistration"]
        cases.append(("identity_mismatch", mismatch_reason))
        cases.append(
            (
                "unverified_stop_current_family",
                {
                    **copy.deepcopy(self.authoritative_ledger_reaudit),
                    "decision": "STOP_CURRENT_FAMILY",
                    "registration_verified": False,
                },
            )
        )

        for name, forged in cases:
            with self.subTest(name=name):
                report = self.build(ledger=forged)
                self.assertEqual(report["research_decision"], "STOP")
                self.assertEqual(report["ledger"]["status"], "UNVERIFIABLE")
                self.assertFalse(report["ledger"]["registration_audit"]["verified"])
                self.assertEqual(report["ledger"]["report"], forged)
                self.assert_no_authority(report)

    def test_real_ledger_cli_audit_only_allows_pending_verified_registration(self):
        repository = pathlib.Path(__file__).resolve().parents[1]
        ledger_tool = repository / "tools" / "experiment_budget_ledger.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir).resolve(strict=False)
            config_path = root / "decision_evidence_validation.json"
            config_path.write_bytes(VALIDATION_CONFIG_BYTES)
            benchmark_path = root / "benchmark.json"
            benchmark_path.write_text(json.dumps(benchmark_report()), encoding="utf-8")
            ledger_path = root / "experiments.jsonl"
            result_path = root / "experiment-result.json"
            policy_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
            information_definition = {
                "data": "d" * 64,
                "features": "f" * 64,
                "actions": "a" * 64,
            }
            information_id = ledger.stable_definition_id(
                information_definition
            )
            family_definition = {
                "mechanism": "lead-lag",
                "target": "net-utility",
            }
            proposal = {
                "experiment_id": "real-experiment-001",
                "benchmark_id": BENCHMARK_ID,
                "validation_policy_sha256": policy_sha,
                "information_set_definition": information_definition,
                "information_set_id": information_id,
                "hypothesis_family_definition": family_definition,
                "hypothesis_family_id": ledger.stable_family_id(
                    information_id, family_definition
                ),
                "display_name": "real ledger integration",
                "changed_dimensions": [
                    {"name": "target", "before": "a", "after": "b"}
                ],
                "expected_direction": "increase",
                "stop_condition": {
                    "metric": "stress_lcb",
                    "operator": "gt",
                    "value": 0.0,
                },
                "result_source_path": str(result_path),
            }
            proposal_path = root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
            registered = subprocess.run(
                [
                    sys.executable,
                    str(ledger_tool),
                    "register",
                    "--ledger",
                    str(ledger_path),
                    "--config",
                    str(config_path),
                    "--benchmark-report",
                    str(benchmark_path),
                    "--proposal",
                    str(proposal_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(registered.returncode, 0, registered.stderr)
            audited = subprocess.run(
                [
                    sys.executable,
                    str(ledger_tool),
                    "audit-next",
                    "--ledger",
                    str(ledger_path),
                    "--config",
                    str(config_path),
                    "--benchmark-report",
                    str(benchmark_path),
                    "--proposal",
                    str(proposal_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(audited.returncode, 0, audited.stderr)
            audit_report = json.loads(audited.stdout)
            self.assertEqual(audit_report["decision"], "ALLOW_NEXT_EXPERIMENT")
            self.assertTrue(audit_report["registration_verified"])
            continued = self.build(
                ledger=audit_report,
                authoritative_ledger_reaudit=audit_report,
            )
            self.assertEqual(continued["research_decision"], "CONTINUE")
            self.assertEqual(
                continued["authorized_experiment_id"],
                proposal["experiment_id"],
            )
            self.assertEqual(continued["ledger"]["report"], audit_report)

            result_identity_payload = {
                "schema_version": "decision_experiment_result_v1",
                "experiment_id": proposal["experiment_id"],
                "registration_nonce": audit_report["registration_nonce"],
                "outcome": "SUPPORTED",
                "result": {"stress_lcb": 0.2},
            }
            result_artifact = {
                **result_identity_payload,
                "result_identity": ledger.canonical_sha256(result_identity_payload),
            }
            result_path.write_bytes(ledger.canonical_json_bytes(result_artifact) + b"\n")
            result_path.chmod(0o444)
            observation_path = root / "observation.json"
            observation_path.write_text(
                json.dumps({"experiment_id": proposal["experiment_id"]}),
                encoding="utf-8",
            )
            observed = subprocess.run(
                [
                    sys.executable,
                    str(ledger_tool),
                    "observe",
                    "--ledger",
                    str(ledger_path),
                    "--config",
                    str(config_path),
                    "--benchmark-report",
                    str(benchmark_path),
                    "--proposal",
                    str(observation_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(observed.returncode, 0, observed.stderr)
            consumed = subprocess.run(
                [
                    sys.executable,
                    str(ledger_tool),
                    "audit-next",
                    "--ledger",
                    str(ledger_path),
                    "--config",
                    str(config_path),
                    "--benchmark-report",
                    str(benchmark_path),
                    "--proposal",
                    str(proposal_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(consumed.returncode, 2, consumed.stderr)
            consumed_report = json.loads(consumed.stdout)
            self.assertEqual(
                consumed_report["decision"], "BLOCK_INVALID_LEDGER"
            )
            self.assertFalse(consumed_report["registration_verified"])
            stopped = self.build(
                ledger=consumed_report,
                authoritative_ledger_reaudit=consumed_report,
            )

        self.assertEqual(stopped["research_decision"], "STOP")
        self.assertEqual(stopped["ledger"]["status"], "UNVERIFIABLE")
        self.assertEqual(stopped["ledger"]["report"], consumed_report)

    def test_complete_negative_evidence_changes_information_set(self):
        cases = []
        statuses = {name: "ALIGNED" for name in SUBSYSTEMS}
        statuses["market_alpha"] = "NOT_ALIGNED"
        cases.append(
            (
                {"alignment": alignment_report(statuses)},
                ["ALIGNMENT_MARKET_ALPHA_NOT_ALIGNED"],
            )
        )
        cases.append(
            ({"uplift": uplift_report("NOT_PROVEN")}, ["UPLIFT_NOT_PROVEN"])
        )
        stopped_ledger = self._real_stopped_ledger_reaudit()
        cases.append(
            (
                {
                    "ledger": stopped_ledger,
                    "authoritative_ledger_reaudit": stopped_ledger,
                },
                ["LEDGER_STOP_CURRENT_FAMILY"],
            )
        )
        for overrides, reasons in cases:
            with self.subTest(reasons=reasons):
                report = self.build(**overrides)
                self.assertEqual(report["research_decision"], "CHANGE_INFORMATION_SET")
                self.assertEqual(report["reason_codes"], reasons)
                self.assert_no_authority(report)

        statuses = {name: "NOT_ALIGNED" for name in SUBSYSTEMS}
        report = self.build(
            alignment=alignment_report(statuses),
            uplift=uplift_report("NOT_PROVEN"),
            ledger=stopped_ledger,
            authoritative_ledger_reaudit=stopped_ledger,
        )
        self.assertEqual(
            report["reason_codes"],
            [
                "ALIGNMENT_MINER_NOT_ALIGNED",
                "ALIGNMENT_MARKET_ALPHA_NOT_ALIGNED",
                "ALIGNMENT_MICROSTRUCTURE_NOT_ALIGNED",
                "ALIGNMENT_ONLINE_TUNER_NOT_ALIGNED",
                "UPLIFT_NOT_PROVEN",
                "LEDGER_STOP_CURRENT_FAMILY",
            ],
        )

    def test_unverifiable_or_unknown_evidence_has_priority_and_stops(self):
        statuses = {name: "ALIGNED" for name in SUBSYSTEMS}
        statuses["miner"] = "UNVERIFIABLE"
        cases = [
            (
                {"alignment": alignment_report(statuses)},
                "ALIGNMENT_MINER_UNVERIFIABLE",
            ),
            ({"uplift": uplift_report("UNVERIFIABLE")}, "UPLIFT_UNVERIFIABLE"),
            (
                {"ledger": forged_ledger_skeleton("BLOCK_INVALID_LEDGER")},
                "LEDGER_INPUT_UNVERIFIABLE",
            ),
            ({"uplift": uplift_report("MAYBE")}, "UPLIFT_UNKNOWN_STATUS"),
            (
                {"ledger": forged_ledger_skeleton("UNKNOWN")},
                "LEDGER_INPUT_UNVERIFIABLE",
            ),
        ]
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                report = self.build(**overrides)
                self.assertEqual(report["research_decision"], "STOP")
                self.assertIn(reason, report["reason_codes"])
                self.assert_no_authority(report)

        invalid_benchmark = benchmark_report()
        invalid_benchmark["identity_status"] = "UNVERIFIABLE"
        report = self.build(benchmark=invalid_benchmark)
        self.assertEqual(report["research_decision"], "STOP")
        self.assertEqual(report["reason_codes"], ["BENCHMARK_UNVERIFIABLE"])

    def test_missing_or_damaged_input_only_invalidates_its_own_section(self):
        good_alignment = alignment_report()
        good_uplift = uplift_report()
        good_ledger = copy.deepcopy(self.authoritative_ledger_reaudit)
        cases = [
            ("benchmark", None, "benchmark", "BENCHMARK_UNVERIFIABLE"),
            ("alignment", None, "alignment", "ALIGNMENT_INPUT_UNVERIFIABLE"),
            (
                "uplift",
                {"read_error": "JSONDecodeError:bad"},
                "uplift",
                "UPLIFT_INPUT_UNVERIFIABLE",
            ),
            ("ledger", [], "ledger", "LEDGER_INPUT_UNVERIFIABLE"),
        ]
        for argument, damaged, section_name, reason in cases:
            with self.subTest(argument=argument):
                kwargs = {
                    "benchmark": benchmark_report(),
                    "alignment": good_alignment,
                    "uplift": good_uplift,
                    "ledger": good_ledger,
                }
                kwargs[argument] = damaged
                report = self.build(**kwargs)
                self.assertEqual(report["research_decision"], "STOP")
                self.assertEqual(report[section_name]["status"], "UNVERIFIABLE")
                self.assertIn(reason, report["reason_codes"])
                if section_name != "alignment":
                    self.assertEqual(report["alignment"]["report"], good_alignment)
                if section_name != "uplift":
                    self.assertEqual(report["uplift"]["report"], good_uplift)
                if section_name != "ledger":
                    self.assertEqual(report["ledger"]["report"], good_ledger)

    def test_each_child_benchmark_mismatch_only_invalidates_that_child(self):
        cases = (
            ("alignment", alignment_report),
            ("uplift", uplift_report),
            ("ledger", lambda: copy.deepcopy(self.authoritative_ledger_reaudit)),
        )
        for section_name, factory in cases:
            with self.subTest(section=section_name):
                mismatched = factory()
                mismatched["benchmark_id"] = "2" * 64
                kwargs = {section_name: mismatched}
                report = self.build(**kwargs)
                section = report[section_name]
                self.assertEqual(report["research_decision"], "STOP")
                self.assertEqual(section["status"], "UNVERIFIABLE")
                self.assertEqual(section["expected_benchmark_id"], BENCHMARK_ID)
                self.assertEqual(section["actual_benchmark_id"], "2" * 64)
                self.assertFalse(section["benchmark_match"])
                for other in {"alignment", "uplift", "ledger"} - {section_name}:
                    self.assertNotEqual(report[other]["status"], "UNVERIFIABLE")
                    self.assertTrue(report[other]["benchmark_match"])

    def test_each_child_must_carry_its_own_primary_benchmark_id(self):
        cases = (
            ("alignment", alignment_report),
            ("uplift", uplift_report),
            ("ledger", lambda: copy.deepcopy(self.authoritative_ledger_reaudit)),
        )
        for section_name, factory in cases:
            with self.subTest(section=section_name):
                missing_id = factory()
                missing_id.pop("benchmark_id")
                report = self.build(**{section_name: missing_id})
                self.assertEqual(report["research_decision"], "STOP")
                self.assertEqual(report[section_name]["status"], "UNVERIFIABLE")
                self.assertFalse(report[section_name]["benchmark_match"])
                self.assertIn(
                    f"{section_name.upper()}_BENCHMARK_MISMATCH",
                    report["reason_codes"],
                )

    def test_failed_alpha_route_is_observation_only_and_never_skips_evidence(self):
        report = self.build(alpha_route=alpha_route_report("FAIL"))

        self.assertEqual(report["research_decision"], "CONTINUE")
        self.assertEqual(report["alpha_route_observation"]["status"], "FAIL")
        self.assertFalse(report["alpha_route_observation"]["affects_research_decision"])
        self.assertEqual(report["alignment"]["status"], "ALIGNED")
        self.assertEqual(report["uplift"]["status"], "UPLIFT_PROVEN")
        self.assertEqual(report["ledger"]["status"], "ALLOW_NEXT_EXPERIMENT")
        self.assertNotIn("SKIPPED_DUE_TO_PRIOR_FAILURE", json.dumps(report))
        self.assert_no_authority(report)

    def test_builder_is_pure_and_cli_atomically_writes_a_machine_readable_report(self):
        inputs = (
            benchmark_report(),
            alignment_report(),
            uplift_report(),
            copy.deepcopy(self.authoritative_ledger_reaudit),
            alpha_route_report(),
        )
        before = copy.deepcopy(inputs)
        builder.build_report(
            *inputs,
            validation_policy=VALIDATION_CONFIG,
            validation_config_sha256=VALIDATION_CONFIG_SHA256,
        )
        self.assertEqual(inputs, before)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            names = ("benchmark", "alignment", "uplift", "ledger", "alpha")
            paths = {}
            for name, payload in zip(names, inputs):
                path = root / f"{name}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                paths[name] = path
            config_path = root / "decision_evidence_validation.json"
            config_path.write_bytes(VALIDATION_CONFIG_BYTES)
            output = root / "decision-evidence.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(builder.__file__)),
                    "--benchmark-report",
                    str(paths["benchmark"]),
                    "--alignment-report",
                    str(paths["alignment"]),
                    "--uplift-report",
                    str(paths["uplift"]),
                    "--ledger-report",
                    str(paths["ledger"]),
                    "--ledger",
                    str(self.ledger_path),
                    "--ledger-proposal",
                    str(self.proposal_path),
                    "--config",
                    str(config_path),
                    "--alpha-route-report",
                    str(paths["alpha"]),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["research_decision"], "CONTINUE")
            self.assertEqual(json.loads(completed.stdout), written)
            self.assertEqual(list(root.glob(f"{output.name}.tmp.*")), [])

            malformed_uplift = uplift_report()
            malformed_uplift["aggregation_cells"][0][
                "frozen_utility"
            ] = "not-a-number"
            paths["uplift"].write_text(
                json.dumps(malformed_uplift), encoding="utf-8"
            )
            failed = subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(builder.__file__)),
                    "--benchmark-report",
                    str(paths["benchmark"]),
                    "--alignment-report",
                    str(paths["alignment"]),
                    "--uplift-report",
                    str(paths["uplift"]),
                    "--ledger-report",
                    str(paths["ledger"]),
                    "--ledger",
                    str(self.ledger_path),
                    "--ledger-proposal",
                    str(self.proposal_path),
                    "--config",
                    str(config_path),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(failed.returncode, 2, failed.stderr)
            stopped = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(stopped["research_decision"], "STOP")
            self.assertEqual(stopped["uplift"]["status"], "UNVERIFIABLE")
            self.assertEqual(stopped["alignment"]["status"], "ALIGNED")
            self.assertEqual(stopped["ledger"]["status"], "ALLOW_NEXT_EXPERIMENT")

    def test_atomic_writer_removes_old_positive_report_when_serialization_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / "decision-evidence.json"
            output.write_text(
                json.dumps({"research_decision": "CONTINUE"}),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                builder._write_json(output, {"non_finite": float("nan")})

            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(f"{output.name}.tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
