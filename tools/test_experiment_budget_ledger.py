#!/usr/bin/env python3

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import multiprocessing
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


def load_module():
    module_path = pathlib.Path(__file__).with_name("experiment_budget_ledger.py")
    spec = importlib.util.spec_from_file_location(
        "experiment_budget_ledger", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_cli_after_delay(command, delay, queue):
    time.sleep(delay)
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": completed.stdout, "stderr": completed.stderr}
    queue.put((completed.returncode, payload))


LEDGER = load_module()
ROOT = pathlib.Path(__file__).resolve().parents[1]
FROZEN_CONFIG_PATH = ROOT / "config" / "decision_evidence_validation.json"


class ExperimentBudgetLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.ledger_path = self.root / "experiments.jsonl"
        self.config_path = self.root / "decision_evidence_validation.json"
        self.config_path.write_bytes(FROZEN_CONFIG_PATH.read_bytes())
        self.frozen_config = json.loads(
            FROZEN_CONFIG_PATH.read_text(encoding="utf-8")
        )
        self.policy_id = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        self.information_set_definition = {
            "data": "d" * 64,
            "features": "f" * 64,
            "actions": "a" * 64,
        }
        self.family_definition = {
            "mechanism": "lead-lag",
            "target": "net-utility",
        }
        self.benchmark_report = self.verified_benchmark_report()
        self.benchmark_id = self.benchmark_report["benchmark_id"]
        self.benchmark_report_path = self.root / "benchmark_report.json"
        self.benchmark_report_path.write_text(
            json.dumps(self.benchmark_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        for path in self.root.glob("result-*.json"):
            with contextlib_suppress_permission_error():
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.temp_dir.cleanup()

    def verified_benchmark_report(self):
        execution_id = "block-01:BTCUSDT"
        event_sha256 = "e" * 64
        component_names = {
            "data": [(f"execution:{execution_id}", event_sha256)],
            "split": [
                ("corpus:BTCUSDT", "1" * 64),
                ("replay_validation_report", "2" * 64),
            ],
            "cost": [
                ("replay_candidate_config", "3" * 64),
                ("runtime_config", "4" * 64),
            ],
            "features": [("feature:BTCUSDT", "5" * 64)],
            "actions": [
                ("replay_policy", "6" * 64),
                ("runtime_policy", "7" * 64),
            ],
            "baseline_policy": [
                ("candidate_model", "8" * 64),
                ("candidate_report", "9" * 64),
            ],
            "run_config": [
                ("decision_evidence_validation", "a" * 64),
                ("runtime_config", "4" * 64),
            ],
            "implementation": [
                ("benchmark_builder", "b" * 64),
                ("paired_evolution_runner", "c" * 64),
                ("replay_validation_runner", "d" * 64),
                ("trade_bot", "f" * 64),
            ],
        }
        identity = {
            "schema_version": "decision_evidence_benchmark_v1",
            "components": {
                component: {
                    "logical_id": f"{component}-v1",
                    "files": [
                        {
                            "logical_name": logical_name,
                            "sha256": sha256,
                        }
                        for logical_name, sha256 in sorted(component_names[component])
                    ],
                }
                for component in component_names
            },
            "evaluation_universe": {
                "blocks": [
                    {
                        "block_id": "block-01",
                        "start_timestamp_ms": 1000,
                        "end_timestamp_ms": 1999,
                        "event_sha256": event_sha256,
                        "cells": [
                            {"symbol": "BTCUSDT", "entry_regime": "trend"}
                        ],
                        "executions": [
                            {
                                "execution_id": execution_id,
                                "symbol": "BTCUSDT",
                                "planned_entry_regimes": ["trend"],
                                "event_sha256": event_sha256,
                            }
                        ],
                    }
                ]
            },
            "validation_policy": {
                "policy": self.frozen_config,
                "sha256": self.policy_id,
            },
        }
        benchmark_id = LEDGER.canonical_sha256(identity)
        return {
            "schema_version": "decision_evidence_benchmark_validation_v1",
            "identity_status": "VERIFIED",
            "benchmark_id": benchmark_id,
            "canonical_identity": identity,
            "validation_config_sha256": self.policy_id,
            "drifts": [],
        }

    @staticmethod
    def information_set_id(definition):
        return LEDGER.canonical_sha256(definition)

    @staticmethod
    def family_id(information_set_id, definition):
        return LEDGER.canonical_sha256(
            {
                "information_set_id": information_set_id,
                "hypothesis_family_definition": definition,
            }
        )

    def result_path(self, experiment_id):
        return (self.root / f"result-{experiment_id}.json").resolve(strict=False)

    def registration(
        self,
        experiment_id="experiment-001",
        information_set_definition=None,
        family_definition=None,
        display_name="lead lag v1",
        changed_dimensions=None,
    ):
        information = copy.deepcopy(
            information_set_definition or self.information_set_definition
        )
        family = copy.deepcopy(family_definition or self.family_definition)
        information_id = self.information_set_id(information)
        return {
            "experiment_id": experiment_id,
            "benchmark_id": self.benchmark_id,
            "validation_policy_sha256": self.policy_id,
            "information_set_definition": information,
            "information_set_id": information_id,
            "hypothesis_family_definition": family,
            "hypothesis_family_id": self.family_id(information_id, family),
            "display_name": display_name,
            "changed_dimensions": changed_dimensions
            or [{"name": "target", "before": "a", "after": "b"}],
            "expected_direction": "increase",
            "stop_condition": {
                "metric": "stress_lcb",
                "operator": "gt",
                "value": 0.0,
            },
            "result_source_path": str(self.result_path(experiment_id)),
        }

    def register(self, proposal):
        return LEDGER.register_experiment(
            self.ledger_path,
            self.config_path,
            proposal,
            self.benchmark_report,
        )

    def registration_record(self, experiment_id):
        return LEDGER.audit_ledger(self.ledger_path)["registrations"][experiment_id]

    def write_result(self, experiment_id, outcome, result=None):
        registration = self.registration_record(experiment_id)
        payload_without_identity = {
            "schema_version": "decision_experiment_result_v1",
            "experiment_id": experiment_id,
            "registration_nonce": registration["registration_nonce"],
            "outcome": outcome,
            "result": result or {"stress_lcb": 1.25},
        }
        payload = {
            **payload_without_identity,
            "result_identity": LEDGER.canonical_sha256(payload_without_identity),
        }
        path = pathlib.Path(registration["result_source_path"])
        time.sleep(0.002)
        path.write_bytes(LEDGER.canonical_json_bytes(payload) + b"\n")
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return path, payload

    def observe(self, experiment_id):
        return LEDGER.observe_experiment(
            self.ledger_path,
            self.config_path,
            {"experiment_id": experiment_id},
            self.benchmark_report,
        )

    def audit_next(self, proposal):
        return LEDGER.audit_next_experiment(
            self.ledger_path,
            self.config_path,
            proposal,
            self.benchmark_report,
        )

    def ledger_bytes(self):
        return self.ledger_path.read_bytes() if self.ledger_path.exists() else b""

    def test_register_generates_authoritative_time_nonce_and_bound_identity(self):
        request = self.registration()
        before = dt.datetime.now(dt.timezone.utc)
        report = self.register(request)
        after = dt.datetime.now(dt.timezone.utc)

        self.assertEqual(report["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(report["appended"])
        self.assertTrue(report["registration_verified"])
        self.assertEqual(report["benchmark_id"], self.benchmark_id)
        self.assertRegex(report["registration_nonce"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            report["remaining_budgets"], {"family": 3, "information_set": 8}
        )
        record = self.registration_record(request["experiment_id"])
        registered = dt.datetime.fromisoformat(record["registered_at"][:-1] + "+00:00")
        self.assertLessEqual(before, registered)
        self.assertLessEqual(registered, after)
        self.assertEqual(record["registration_nonce"], report["registration_nonce"])
        self.assertEqual(
            record["result_source_path"],
            str(pathlib.Path(request["result_source_path"]).resolve()),
        )
        self.assertNotIn("registered_at", request)
        self.assertNotIn("earliest_result_identity", record)
        persisted = LEDGER.audit_ledger(self.ledger_path)["records"][0]
        self.assertEqual(persisted["record_hash"], LEDGER.record_hash(persisted))

    def test_register_rejects_noncanonical_absolute_result_path(self):
        request = self.registration()
        canonical = pathlib.Path(request["result_source_path"])
        request["result_source_path"] = str(canonical.parent / "unused" / ".." / canonical.name)

        report = self.register(request)

        self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(report["appended"])
        self.assertIn("canonical", " ".join(report["reasons"]))
        self.assertEqual(self.ledger_bytes(), b"")

    def test_registration_schema_forbids_backfilled_time_and_existing_result(self):
        base = self.registration()
        invalid_requests = []
        for field in (
            "experiment_id",
            "benchmark_id",
            "validation_policy_sha256",
            "information_set_definition",
            "information_set_id",
            "hypothesis_family_definition",
            "hypothesis_family_id",
            "display_name",
            "changed_dimensions",
            "expected_direction",
            "stop_condition",
            "result_source_path",
        ):
            missing = copy.deepcopy(base)
            missing.pop(field)
            invalid_requests.append((f"missing-{field}", missing))
        backfilled = copy.deepcopy(base)
        backfilled["registered_at"] = "2020-01-01T00:00:00Z"
        invalid_requests.append(("caller-backfilled-time", backfilled))
        supplied_nonce = copy.deepcopy(base)
        supplied_nonce["registration_nonce"] = "a" * 64
        invalid_requests.append(("caller-supplied-nonce", supplied_nonce))
        wrong_family = copy.deepcopy(base)
        wrong_family["hypothesis_family_id"] = LEDGER.canonical_sha256(
            wrong_family["hypothesis_family_definition"]
        )
        invalid_requests.append(("family-not-bound", wrong_family))
        bad_dimension = copy.deepcopy(base)
        bad_dimension["changed_dimensions"] = ["target"]
        invalid_requests.append(("unstructured-dimension", bad_dimension))
        unchanged = copy.deepcopy(base)
        unchanged["changed_dimensions"][0]["after"] = "a"
        invalid_requests.append(("unchanged-dimension", unchanged))
        relative = copy.deepcopy(base)
        relative["result_source_path"] = "relative-result.json"
        invalid_requests.append(("relative-result-path", relative))

        for name, request in invalid_requests:
            with self.subTest(name=name):
                report = self.register(request)
                self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER", report)
                self.assertFalse(report["appended"])
                self.assertEqual(self.ledger_bytes(), b"")

        occupied = self.registration()
        pathlib.Path(occupied["result_source_path"]).write_text("already exists")
        report = self.register(occupied)
        self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER")
        self.assertIn("must not exist", " ".join(report["reasons"]))

        pathlib.Path(occupied["result_source_path"]).unlink()
        multi = self.registration(
            changed_dimensions=[
                {"name": "target", "before": "a", "after": "b"},
                {"name": "features", "before": 1, "after": 2},
            ]
        )
        report = self.register(multi)
        self.assertEqual(report["decision"], "STOP_CURRENT_FAMILY")
        self.assertFalse(report["appended"])

    def test_each_operation_recomputes_benchmark_and_full_policy_binding(self):
        proposal = self.registration()
        forged = copy.deepcopy(self.benchmark_report)
        forged["benchmark_id"] = "c" * 64
        blocked = LEDGER.register_experiment(
            self.ledger_path, self.config_path, proposal, forged
        )
        self.assertEqual(blocked["decision"], "BLOCK_INVALID_LEDGER")
        self.assertEqual(self.ledger_bytes(), b"")

        policy_drift = copy.deepcopy(self.benchmark_report)
        policy_drift["canonical_identity"]["validation_policy"]["policy"][
            "failure_budgets"
        ]["family"] = 100
        blocked = LEDGER.register_experiment(
            self.ledger_path, self.config_path, proposal, policy_drift
        )
        self.assertEqual(blocked["decision"], "BLOCK_INVALID_LEDGER")

        self.assertEqual(self.register(proposal)["decision"], "ALLOW_NEXT_EXPERIMENT")
        blocked_audit = LEDGER.audit_next_experiment(
            self.ledger_path, self.config_path, proposal, forged
        )
        self.assertEqual(blocked_audit["decision"], "BLOCK_INVALID_LEDGER")
        self.write_result(proposal["experiment_id"], "SUPPORTED")
        blocked_observe = LEDGER.observe_experiment(
            self.ledger_path,
            self.config_path,
            {"experiment_id": proposal["experiment_id"]},
            forged,
        )
        self.assertEqual(blocked_observe["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(blocked_observe["appended"])

        drifted_config = copy.deepcopy(self.frozen_config)
        drifted_config["failure_budgets"]["family"] = 100
        self.config_path.write_text(json.dumps(drifted_config), encoding="utf-8")
        fresh = self.registration("config-drift")
        blocked_config = self.register(fresh)
        self.assertEqual(blocked_config["decision"], "BLOCK_INVALID_LEDGER")
        self.assertIn("frozen contract", " ".join(blocked_config["reasons"]))

    def test_observe_requires_canonical_read_only_nonce_bound_post_registration_artifact(self):
        proposal = self.registration()
        self.assertEqual(self.register(proposal)["decision"], "ALLOW_NEXT_EXPERIMENT")
        before = self.ledger_bytes()
        missing = self.observe(proposal["experiment_id"])
        self.assertEqual(missing["decision"], "BLOCK_INVALID_LEDGER")
        self.assertEqual(self.ledger_bytes(), before)

        record = self.registration_record(proposal["experiment_id"])
        path = pathlib.Path(record["result_source_path"])
        bad_payload = {
            "schema_version": "decision_experiment_result_v1",
            "experiment_id": proposal["experiment_id"],
            "registration_nonce": "f" * 64,
            "outcome": "SUPPORTED",
            "result": {"stress_lcb": 1.0},
        }
        bad_payload["result_identity"] = LEDGER.canonical_sha256(bad_payload)
        path.write_bytes(LEDGER.canonical_json_bytes(bad_payload) + b"\n")
        path.chmod(0o444)
        wrong_nonce = self.observe(proposal["experiment_id"])
        self.assertEqual(wrong_nonce["decision"], "BLOCK_INVALID_LEDGER")
        path.chmod(0o600)
        path.unlink()

        path, payload = self.write_result(proposal["experiment_id"], "SUPPORTED")
        path.chmod(0o644)
        writable = self.observe(proposal["experiment_id"])
        self.assertEqual(writable["decision"], "BLOCK_INVALID_LEDGER")
        path.chmod(0o444)
        supported = self.observe(proposal["experiment_id"])
        self.assertEqual(supported["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(supported["appended"])
        observe_record = LEDGER.audit_ledger(self.ledger_path)["records"][-1]
        self.assertEqual(observe_record["outcome"], "SUPPORTED")
        self.assertEqual(observe_record["result_identity"], payload["result_identity"])
        self.assertEqual(
            observe_record["result_file_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertEqual(observe_record["registration_nonce"], record["registration_nonce"])

    def test_preexisting_or_timestamp_backdated_result_cannot_be_positive_evidence(self):
        proposal = self.registration()
        preexisting = pathlib.Path(proposal["result_source_path"])
        preexisting.write_text("preexisting", encoding="utf-8")
        self.assertEqual(self.register(proposal)["decision"], "BLOCK_INVALID_LEDGER")
        preexisting.unlink()

        self.assertEqual(self.register(proposal)["decision"], "ALLOW_NEXT_EXPERIMENT")
        path, _ = self.write_result(proposal["experiment_id"], "SUPPORTED")
        record = self.registration_record(proposal["experiment_id"])
        registered = dt.datetime.fromisoformat(
            record["registered_at"][:-1] + "+00:00"
        ).timestamp()
        path.chmod(0o600)
        os.utime(path, (registered - 10, registered - 10))
        path.chmod(0o444)
        blocked = self.observe(proposal["experiment_id"])
        self.assertEqual(blocked["decision"], "BLOCK_INVALID_LEDGER")
        self.assertIn("after registration", " ".join(blocked["reasons"]))

    def test_audit_next_requires_exact_pending_registration(self):
        proposal = self.registration()
        empty = self.audit_next(proposal)
        self.assertEqual(empty["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(empty["registration_verified"])

        self.assertEqual(self.register(proposal)["decision"], "ALLOW_NEXT_EXPERIMENT")
        exact = self.audit_next(proposal)
        self.assertEqual(exact["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(exact["registration_verified"])

        for name, field, value in (
            ("unknown", "experiment_id", "unknown"),
            ("dimension", "changed_dimensions", [{"name": "target", "before": "a", "after": "c"}]),
            ("direction", "expected_direction", "decrease"),
            ("benchmark", "benchmark_id", "c" * 64),
        ):
            with self.subTest(name=name):
                changed = copy.deepcopy(proposal)
                changed[field] = value
                report = self.audit_next(changed)
                self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER", report)

        self.write_result(proposal["experiment_id"], "SUPPORTED")
        self.assertEqual(self.observe(proposal["experiment_id"])["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertEqual(self.audit_next(proposal)["decision"], "BLOCK_INVALID_LEDGER")

    def test_failure_budgets_remain_fixed_and_display_name_does_not_reset(self):
        outcomes = ("SUPPORTED", "FALSIFIED", "INCONCLUSIVE")
        for number, outcome in enumerate(outcomes, start=1):
            request = self.registration(f"experiment-{number:03d}")
            self.assertEqual(self.register(request)["decision"], "ALLOW_NEXT_EXPERIMENT")
            self.write_result(request["experiment_id"], outcome)
            report = self.observe(request["experiment_id"])
            self.assertEqual(report["decision"], "ALLOW_NEXT_EXPERIMENT")

        pending = self.registration("experiment-pending", display_name="renamed")
        self.assertEqual(self.register(pending)["decision"], "ALLOW_NEXT_EXPERIMENT")
        threshold = self.registration("experiment-threshold")
        self.assertEqual(self.register(threshold)["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.write_result(threshold["experiment_id"], "FALSIFIED")
        threshold_report = self.observe(threshold["experiment_id"])
        self.assertEqual(threshold_report["decision"], "STOP_CURRENT_FAMILY")
        self.assertEqual(threshold_report["remaining_budgets"]["family"], 0)
        self.assertEqual(self.audit_next(pending)["decision"], "STOP_CURRENT_FAMILY")

        before = self.ledger_bytes()
        blocked = self.register(
            self.registration("experiment-after-stop", display_name="new presentation")
        )
        self.assertEqual(blocked["decision"], "STOP_CURRENT_FAMILY")
        self.assertFalse(blocked["appended"])
        self.assertEqual(self.ledger_bytes(), before)

    def test_eighth_information_set_failure_stops_fresh_family(self):
        last_report = None
        for number in range(8):
            family = {"mechanism": f"claim-{number}", "target": "net-utility"}
            request = self.registration(
                f"information-{number}", family_definition=family
            )
            self.assertEqual(self.register(request)["decision"], "ALLOW_NEXT_EXPERIMENT")
            self.write_result(request["experiment_id"], "INCONCLUSIVE")
            last_report = self.observe(request["experiment_id"])
        self.assertEqual(last_report["decision"], "STOP_CURRENT_FAMILY")
        self.assertEqual(last_report["remaining_budgets"]["information_set"], 0)

        before = self.ledger_bytes()
        fresh = self.registration(
            "information-after-stop",
            family_definition={"mechanism": "fresh", "target": "net-utility"},
        )
        report = self.register(fresh)
        self.assertEqual(report["decision"], "STOP_CURRENT_FAMILY")
        self.assertEqual(self.ledger_bytes(), before)

    def test_hash_chain_tamper_delete_reorder_and_previous_hash_are_blocked(self):
        for experiment_id in ("experiment-001", "experiment-002"):
            request = self.registration(experiment_id)
            self.assertEqual(self.register(request)["decision"], "ALLOW_NEXT_EXPERIMENT")
            self.write_result(experiment_id, "SUPPORTED")
            self.assertEqual(self.observe(experiment_id)["decision"], "ALLOW_NEXT_EXPERIMENT")
        pristine = self.ledger_bytes()
        lines = pristine.decode("ascii").splitlines()
        changed = json.loads(lines[0])
        changed["display_name"] = "edited"
        previous = json.loads(lines[1])
        previous["previous_record_hash"] = "f" * 64
        mutations = {
            "edit": [json.dumps(changed, sort_keys=True, separators=(",", ":")), *lines[1:]],
            "delete": lines[:-1],
            "reorder": [lines[1], lines[0], *lines[2:]],
            "previous": [lines[0], json.dumps(previous, sort_keys=True, separators=(",", ":")), *lines[2:]],
        }
        for name, mutated_lines in mutations.items():
            with self.subTest(name=name):
                self.ledger_path.write_bytes(("\n".join(mutated_lines) + "\n").encode("ascii"))
                with self.assertRaises(LEDGER.LedgerValidationError):
                    LEDGER.audit_ledger(self.ledger_path)
                self.ledger_path.write_bytes(pristine)

    def test_recovery_marker_contains_before_after_and_pending_record(self):
        proposal = self.registration()
        with mock.patch.object(
            LEDGER, "_write_checkpoint", side_effect=OSError("checkpoint fault")
        ):
            report = self.register(proposal)
        self.assertTrue(report["appended"])
        self.assertTrue(report["checkpoint_recovery_required"])
        marker_path = self.ledger_path.with_suffix(".jsonl.recovery.json")
        marker = json.loads(marker_path.read_text(encoding="ascii"))
        self.assertEqual(
            set(marker),
            {"schema_version", "before_checkpoint", "after_checkpoint", "pending_record"},
        )
        self.assertEqual(marker["pending_record"]["record_type"], "register")

        recovered = self.audit_next(proposal)
        self.assertEqual(recovered["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(recovered["checkpoint_recovered"])
        self.assertFalse(marker_path.exists())

    def test_real_subprocess_crash_after_marker_before_append_is_retryable(self):
        proposal = self.registration()
        proposal_path = self.root / "crash-proposal.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        script = """
import importlib.util, json, os, pathlib, sys
module_path, ledger_path, config_path, proposal_path, benchmark_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location('crash_ledger', module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._durable_append = lambda *args, **kwargs: os._exit(93)
module.register_experiment(
    ledger_path,
    config_path,
    json.loads(pathlib.Path(proposal_path).read_text()),
    json.loads(pathlib.Path(benchmark_path).read_text()),
)
"""
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(pathlib.Path(LEDGER.__file__)),
                str(self.ledger_path),
                str(self.config_path),
                str(proposal_path),
                str(self.benchmark_report_path),
            ],
            check=False,
        )
        self.assertEqual(crashed.returncode, 93)
        self.assertEqual(self.ledger_bytes(), b"")
        marker_path = self.ledger_path.with_suffix(".jsonl.recovery.json")
        self.assertTrue(marker_path.is_file())

        retried = self.register(proposal)
        self.assertEqual(retried["decision"], "ALLOW_NEXT_EXPERIMENT", retried)
        self.assertTrue(retried["appended"])
        self.assertTrue(retried["checkpoint_recovered"])
        self.assertFalse(marker_path.exists())

    def test_partial_append_with_recovery_marker_blocks_without_truncating(self):
        proposal = self.registration()
        original = LEDGER._durable_append

        def partial_append(path, content):
            with pathlib.Path(path).open("ab") as handle:
                handle.write(content[: len(content) // 2])
                handle.flush()
                os.fsync(handle.fileno())
            raise OSError("partial append")

        with mock.patch.object(LEDGER, "_durable_append", side_effect=partial_append):
            report = self.register(proposal)
        self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER")
        partial = self.ledger_bytes()
        self.assertTrue(partial)
        self.assertFalse(partial.endswith(b"\n"))
        self.assertTrue(self.ledger_path.with_suffix(".jsonl.recovery.json").exists())
        with self.assertRaises(LEDGER.LedgerValidationError):
            LEDGER.audit_ledger(self.ledger_path)
        self.assertEqual(self.ledger_bytes(), partial)
        self.assertIsNotNone(original)

    def test_cross_process_register_and_observe_keep_contiguous_chain(self):
        module_path = str(pathlib.Path(LEDGER.__file__))
        context = multiprocessing.get_context("spawn")
        common = [
            "--ledger", str(self.ledger_path),
            "--config", str(self.config_path),
            "--benchmark-report", str(self.benchmark_report_path),
        ]
        requests = []
        register_commands = []
        for number in range(4):
            request = self.registration(
                f"concurrent-{number}",
                family_definition={
                    "mechanism": f"concurrent-{number}",
                    "target": "net-utility",
                },
            )
            requests.append(request)
            path = self.root / f"register-{number}.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            register_commands.append(
                [
                    sys.executable,
                    module_path,
                    "register",
                    *common,
                    "--proposal",
                    str(path),
                ]
            )

        queue = context.Queue()
        processes = [
            context.Process(
                target=run_cli_after_delay,
                args=(command, number * 0.02, queue),
            )
            for number, command in enumerate(register_commands)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        register_results = [queue.get(timeout=2) for _ in processes]
        self.assertTrue(all(item[1].get("appended") for item in register_results))

        observe_commands = []
        for number, request in enumerate(requests):
            self.write_result(request["experiment_id"], "SUPPORTED")
            path = self.root / f"observe-{number}.json"
            path.write_text(
                json.dumps({"experiment_id": request["experiment_id"]}),
                encoding="utf-8",
            )
            observe_commands.append(
                [
                    sys.executable,
                    module_path,
                    "observe",
                    *common,
                    "--proposal",
                    str(path),
                ]
            )
        processes = [
            context.Process(
                target=run_cli_after_delay,
                args=(command, number * 0.02, queue),
            )
            for number, command in enumerate(observe_commands)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        observe_results = [queue.get(timeout=2) for _ in processes]
        self.assertTrue(all(item[1].get("appended") for item in observe_results))

        state = LEDGER.audit_ledger(self.ledger_path)
        self.assertEqual(len(state["records"]), 8)
        self.assertEqual(
            [record["sequence"] for record in state["records"]],
            list(range(1, 9)),
        )
        self.assertEqual(len(state["registrations"]), 4)
        self.assertEqual(len(state["observations"]), 4)

    def test_cli_requires_benchmark_report_and_completes_register_audit_observe(self):
        proposal = self.registration()
        proposal_path = self.root / "register.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        base = [sys.executable, str(pathlib.Path(LEDGER.__file__))]
        common = [
            "--ledger", str(self.ledger_path),
            "--config", str(self.config_path),
            "--benchmark-report", str(self.benchmark_report_path),
        ]
        missing_benchmark = subprocess.run(
            base + ["register", "--ledger", str(self.ledger_path), "--config", str(self.config_path), "--proposal", str(proposal_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(missing_benchmark.returncode, 0)

        registered = subprocess.run(
            base + ["register", *common, "--proposal", str(proposal_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)
        register_report = json.loads(registered.stdout)
        self.assertTrue(register_report["appended"])
        audited = subprocess.run(
            base + ["audit-next", *common, "--proposal", str(proposal_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(audited.returncode, 0, audited.stderr)
        self.assertTrue(json.loads(audited.stdout)["registration_verified"])

        self.write_result(proposal["experiment_id"], "SUPPORTED")
        observation_path = self.root / "observe.json"
        observation_path.write_text(
            json.dumps({"experiment_id": proposal["experiment_id"]}),
            encoding="utf-8",
        )
        observed = subprocess.run(
            base + ["observe", *common, "--proposal", str(observation_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertTrue(json.loads(observed.stdout)["appended"])


class contextlib_suppress_permission_error:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _traceback):
        return exc_type is PermissionError


if __name__ == "__main__":
    unittest.main()
