#!/usr/bin/env python3

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import multiprocessing
import pathlib
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
        self.benchmark_id = "b" * 64
        self.information_set_definition = {
            "data": "d" * 64,
            "features": "f" * 64,
            "actions": "a" * 64,
        }
        self.family_definition = {
            "mechanism": "lead-lag",
            "target": "net-utility",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def add_seconds(timestamp, seconds):
        parsed = dt.datetime.fromisoformat(timestamp[:-1] + "+00:00")
        return (parsed + dt.timedelta(seconds=seconds)).isoformat().replace(
            "+00:00", "Z"
        )

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

    def registration(
        self,
        experiment_id="experiment-001",
        registered_at="2026-08-11T00:00:00Z",
        information_set_definition=None,
        family_definition=None,
        display_name="lead lag v1",
        changed_dimensions=None,
        earliest_result_identity="e" * 64,
        result_source_identity="d" * 64,
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
            "registered_at": registered_at,
            "earliest_result_at": self.add_seconds(registered_at, 30),
            "earliest_result_identity": earliest_result_identity,
            "result_source_identity": result_source_identity,
        }

    def register(self, registration):
        return LEDGER.register_experiment(
            self.ledger_path, self.config_path, registration
        )

    def observe(
        self,
        experiment_id,
        outcome,
        observed_at,
        result_identity="e" * 64,
        result_source_identity="d" * 64,
    ):
        return LEDGER.observe_experiment(
            self.ledger_path,
            self.config_path,
            {
                "experiment_id": experiment_id,
                "outcome": outcome,
                "observed_at": observed_at,
                "result_identity": result_identity,
                "result_source_identity": result_source_identity,
            },
        )

    def ledger_bytes(self):
        return self.ledger_path.read_bytes() if self.ledger_path.exists() else b""

    def test_register_writes_authoritative_schema_and_bound_identity(self):
        request = self.registration()
        report = self.register(request)

        self.assertEqual(report["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(report["appended"])
        self.assertTrue(report["registration_verified"])
        self.assertEqual(
            report["remaining_budgets"], {"family": 3, "information_set": 8}
        )
        record = LEDGER.audit_ledger(self.ledger_path)["records"][0]
        self.assertEqual(record["schema_version"], "decision_experiment_ledger_v1")
        self.assertEqual(record["record_type"], "register")
        self.assertEqual(record["sequence"], 1)
        self.assertEqual(record["previous_record_hash"], "0" * 64)
        self.assertEqual(record["display_name"], "lead lag v1")
        self.assertEqual(record["changed_dimensions"], request["changed_dimensions"])
        self.assertEqual(record["stop_condition"], request["stop_condition"])
        self.assertEqual(record["record_hash"], LEDGER.record_hash(record))

        renamed = self.registration(
            "experiment-rename",
            "2026-08-11T00:01:00Z",
            display_name="presentation only",
        )
        self.assertEqual(
            renamed["hypothesis_family_id"], request["hypothesis_family_id"]
        )
        other_information = {**self.information_set_definition, "data": "c" * 64}
        other = self.registration(
            "experiment-other-information",
            "2026-08-11T00:02:00Z",
            information_set_definition=other_information,
        )
        self.assertNotEqual(
            other["hypothesis_family_id"], request["hypothesis_family_id"]
        )

    def test_registration_schema_and_preregistration_are_strict(self):
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
            "registered_at",
            "earliest_result_at",
            "earliest_result_identity",
            "result_source_identity",
        ):
            missing = copy.deepcopy(base)
            missing.pop(field)
            invalid_requests.append((f"missing-{field}", missing))

        wrong_family = copy.deepcopy(base)
        wrong_family["hypothesis_family_id"] = LEDGER.canonical_sha256(
            wrong_family["hypothesis_family_definition"]
        )
        invalid_requests.append(("family-not-bound-to-information", wrong_family))
        equal_time = copy.deepcopy(base)
        equal_time["earliest_result_at"] = equal_time["registered_at"]
        invalid_requests.append(("equal-result-time", equal_time))
        result_before = copy.deepcopy(base)
        result_before["earliest_result_at"] = "2026-08-10T23:59:59Z"
        invalid_requests.append(("result-before-registration", result_before))
        free_identity = copy.deepcopy(base)
        free_identity["earliest_result_identity"] = "future-result"
        invalid_requests.append(("free-result-identity", free_identity))
        bad_dimension = copy.deepcopy(base)
        bad_dimension["changed_dimensions"] = ["target"]
        invalid_requests.append(("unstructured-dimension", bad_dimension))
        unchanged = copy.deepcopy(base)
        unchanged["changed_dimensions"][0]["after"] = "a"
        invalid_requests.append(("unchanged-dimension", unchanged))
        bad_operator = copy.deepcopy(base)
        bad_operator["stop_condition"]["operator"] = "approximately"
        invalid_requests.append(("bad-stop-operator", bad_operator))
        nonfinite = copy.deepcopy(base)
        nonfinite["stop_condition"]["value"] = float("nan")
        invalid_requests.append(("nonfinite-stop-value", nonfinite))

        for name, request in invalid_requests:
            with self.subTest(name=name):
                before = self.ledger_bytes()
                report = self.register(request)
                self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER", report)
                self.assertFalse(report["appended"])
                self.assertEqual(self.ledger_bytes(), before)

        multi = self.registration(
            changed_dimensions=[
                {"name": "target", "before": "a", "after": "b"},
                {"name": "features", "before": 1, "after": 2},
            ]
        )
        report = self.register(multi)
        self.assertEqual(report["decision"], "STOP_CURRENT_FAMILY")
        self.assertFalse(report["appended"])
        self.assertEqual(self.ledger_bytes(), b"")

    def test_config_must_equal_frozen_contract_and_policy_identity(self):
        for name, mutate in (
            ("family-budget", lambda value: value["failure_budgets"].update(family=100)),
            ("information-budget", lambda value: value["failure_budgets"].update(information_set=100)),
            ("schema", lambda value: value.update(schema_version="drifted")),
            ("seed", lambda value: value["seed"].update(cli_override_allowed=True)),
            ("numeric-type", lambda value: value["uplift"].update(block_coverage=True)),
        ):
            with self.subTest(name=name):
                drifted = copy.deepcopy(self.frozen_config)
                mutate(drifted)
                self.config_path.write_text(json.dumps(drifted), encoding="utf-8")
                with self.assertRaises(LEDGER.LedgerValidationError):
                    LEDGER.validation_policy_sha256(self.config_path)
                report = self.register(self.registration())
                self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER")
                self.assertFalse(report["appended"])
                self.assertEqual(self.ledger_bytes(), b"")
                self.config_path.write_bytes(FROZEN_CONFIG_PATH.read_bytes())

        wrong_policy = self.registration()
        wrong_policy["validation_policy_sha256"] = "c" * 64
        report = self.register(wrong_policy)
        self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER")
        self.assertEqual(self.ledger_bytes(), b"")

        valid = self.registration()
        self.assertEqual(self.register(valid)["decision"], "ALLOW_NEXT_EXPERIMENT")
        drifted = copy.deepcopy(self.frozen_config)
        drifted["failure_budgets"]["family"] = 100
        self.config_path.write_text(json.dumps(drifted), encoding="utf-8")
        report = LEDGER.audit_next_experiment(
            self.ledger_path, self.config_path, valid
        )
        self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(report["registration_verified"])

    def test_audit_next_requires_exact_pending_registration(self):
        proposal = self.registration()
        empty = LEDGER.audit_next_experiment(
            self.ledger_path, self.config_path, proposal
        )
        self.assertEqual(empty["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(empty["registration_verified"])
        self.assertEqual(empty["experiment_id"], proposal["experiment_id"])

        self.assertEqual(self.register(proposal)["decision"], "ALLOW_NEXT_EXPERIMENT")
        exact = LEDGER.audit_next_experiment(
            self.ledger_path, self.config_path, proposal
        )
        self.assertEqual(exact["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(exact["registration_verified"])
        self.assertEqual(exact["benchmark_id"], self.benchmark_id)

        mismatches = []
        unknown = copy.deepcopy(proposal)
        unknown["experiment_id"] = "unknown-experiment"
        mismatches.append(("unknown", unknown))
        incomplete = copy.deepcopy(proposal)
        incomplete.pop("stop_condition")
        mismatches.append(("incomplete", incomplete))
        changed = copy.deepcopy(proposal)
        changed["changed_dimensions"][0]["after"] = "c"
        mismatches.append(("dimension", changed))
        direction = copy.deepcopy(proposal)
        direction["expected_direction"] = "decrease"
        mismatches.append(("direction", direction))
        stop = copy.deepcopy(proposal)
        stop["stop_condition"]["value"] = 1.0
        mismatches.append(("stop", stop))
        drift = copy.deepcopy(proposal)
        drift["benchmark_id"] = "c" * 64
        mismatches.append(("benchmark", drift))
        renamed_family = copy.deepcopy(proposal)
        renamed_family["hypothesis_family_definition"]["mechanism"] = "momentum"
        renamed_family["hypothesis_family_id"] = self.family_id(
            renamed_family["information_set_id"],
            renamed_family["hypothesis_family_definition"],
        )
        mismatches.append(("family", renamed_family))

        for name, candidate in mismatches:
            with self.subTest(name=name):
                report = LEDGER.audit_next_experiment(
                    self.ledger_path, self.config_path, candidate
                )
                self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER", report)
                self.assertFalse(report["registration_verified"])

        observed = self.observe(
            proposal["experiment_id"], "SUPPORTED", "2026-08-11T00:01:00Z"
        )
        self.assertEqual(observed["decision"], "ALLOW_NEXT_EXPERIMENT")
        consumed = LEDGER.audit_next_experiment(
            self.ledger_path, self.config_path, proposal
        )
        self.assertEqual(consumed["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(consumed["registration_verified"])

    def test_duplicate_benchmark_drift_and_repeated_observe_never_mutate(self):
        proposal = self.registration()
        self.assertEqual(self.register(proposal)["decision"], "ALLOW_NEXT_EXPERIMENT")
        before = self.ledger_bytes()

        duplicate = self.register(proposal)
        self.assertEqual(duplicate["decision"], "BLOCK_INVALID_LEDGER")
        drifted = self.registration(
            "experiment-drift", "2026-08-11T00:01:00Z"
        )
        drifted["benchmark_id"] = "c" * 64
        drifted_report = self.register(drifted)
        self.assertEqual(drifted_report["decision"], "BLOCK_INVALID_LEDGER")
        self.assertEqual(drifted_report["benchmark_id"], self.benchmark_id)
        self.assertEqual(drifted_report["expected_benchmark_id"], self.benchmark_id)
        self.assertEqual(drifted_report["actual_benchmark_id"], "c" * 64)
        self.assertEqual(self.ledger_bytes(), before)

        first = self.observe(
            proposal["experiment_id"], "SUPPORTED", "2026-08-11T00:01:00Z"
        )
        self.assertTrue(first["appended"])
        observed = self.ledger_bytes()
        repeated = self.observe(
            proposal["experiment_id"], "FALSIFIED", "2026-08-11T00:02:00Z"
        )
        self.assertEqual(repeated["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(repeated["appended"])
        self.assertEqual(self.ledger_bytes(), observed)

    def test_observe_binds_result_source_identity_and_supports_future_result(self):
        proposal = self.registration(earliest_result_identity="not_available")
        self.assertEqual(self.register(proposal)["decision"], "ALLOW_NEXT_EXPERIMENT")
        before = self.ledger_bytes()

        too_early = self.observe(
            proposal["experiment_id"],
            "FALSIFIED",
            "2026-08-11T00:00:20Z",
            "not_available",
        )
        self.assertEqual(too_early["decision"], "BLOCK_INVALID_LEDGER")
        wrong_source = self.observe(
            proposal["experiment_id"],
            "SUPPORTED",
            "2026-08-11T00:01:00Z",
            "f" * 64,
            "c" * 64,
        )
        self.assertEqual(wrong_source["decision"], "BLOCK_INVALID_LEDGER")
        self.assertEqual(self.ledger_bytes(), before)

        positive = self.observe(
            proposal["experiment_id"],
            "SUPPORTED",
            "2026-08-11T00:01:00Z",
            "f" * 64,
        )
        self.assertEqual(positive["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(positive["appended"])

        fixed = self.registration(
            "fixed-result", "2026-08-11T00:02:00Z", earliest_result_identity="a" * 64
        )
        self.assertEqual(self.register(fixed)["decision"], "ALLOW_NEXT_EXPERIMENT")
        wrong_identity = self.observe(
            fixed["experiment_id"], "FALSIFIED", "2026-08-11T00:03:00Z", "f" * 64
        )
        self.assertEqual(wrong_identity["decision"], "BLOCK_INVALID_LEDGER")

        negative = self.observe(
            fixed["experiment_id"],
            "FALSIFIED",
            "2026-08-11T00:03:00Z",
            "a" * 64,
        )
        self.assertEqual(negative["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(negative["appended"])
        observe_record = LEDGER.audit_ledger(self.ledger_path)["records"][-1]
        self.assertEqual(observe_record["record_type"], "observe")
        self.assertEqual(observe_record["result_identity"], "a" * 64)
        self.assertEqual(
            observe_record["result_source_identity"],
            fixed["result_source_identity"],
        )

    def test_failure_budgets_stop_after_appended_threshold_and_do_not_reset(self):
        outcomes = ("SUPPORTED", "FALSIFIED", "INCONCLUSIVE")
        for number, outcome in enumerate(outcomes, start=1):
            registered_at = f"2026-08-11T00:{(number - 1) * 2:02d}:00Z"
            experiment_id = f"experiment-{number:03d}"
            request = self.registration(experiment_id, registered_at)
            self.assertEqual(self.register(request)["decision"], "ALLOW_NEXT_EXPERIMENT")
            report = self.observe(
                experiment_id,
                outcome,
                f"2026-08-11T00:{(number - 1) * 2 + 1:02d}:00Z",
            )
            self.assertEqual(report["decision"], "ALLOW_NEXT_EXPERIMENT")

        pending = self.registration(
            "experiment-pending", "2026-08-11T00:06:00Z", display_name="renamed"
        )
        self.assertEqual(self.register(pending)["decision"], "ALLOW_NEXT_EXPERIMENT")
        threshold = self.registration("experiment-threshold", "2026-08-11T00:07:00Z")
        self.assertEqual(self.register(threshold)["decision"], "ALLOW_NEXT_EXPERIMENT")
        threshold_report = self.observe(
            "experiment-threshold", "FALSIFIED", "2026-08-11T00:08:00Z"
        )
        self.assertEqual(threshold_report["decision"], "STOP_CURRENT_FAMILY")
        self.assertTrue(threshold_report["appended"])
        self.assertEqual(threshold_report["remaining_budgets"]["family"], 0)

        audit = LEDGER.audit_next_experiment(
            self.ledger_path, self.config_path, pending
        )
        self.assertEqual(audit["decision"], "STOP_CURRENT_FAMILY")
        self.assertTrue(audit["registration_verified"])
        before = self.ledger_bytes()
        blocked = self.register(
            self.registration(
                "experiment-after-stop",
                "2026-08-11T00:09:00Z",
                display_name="another presentation",
            )
        )
        self.assertEqual(blocked["decision"], "STOP_CURRENT_FAMILY")
        self.assertFalse(blocked["appended"])
        self.assertEqual(self.ledger_bytes(), before)

    def test_eighth_information_set_failure_stops_fresh_family(self):
        information_id = self.information_set_id(self.information_set_definition)
        last_report = None
        for number in range(8):
            family = {"mechanism": f"claim-{number}", "target": "net-utility"}
            request = self.registration(
                f"information-{number}",
                f"2026-08-11T01:{number * 2:02d}:00Z",
                family_definition=family,
                display_name=f"family {number}",
            )
            self.assertEqual(request["information_set_id"], information_id)
            self.assertEqual(self.register(request)["decision"], "ALLOW_NEXT_EXPERIMENT")
            last_report = self.observe(
                request["experiment_id"],
                "INCONCLUSIVE",
                f"2026-08-11T01:{number * 2 + 1:02d}:00Z",
            )
        self.assertEqual(last_report["decision"], "STOP_CURRENT_FAMILY")
        self.assertEqual(last_report["remaining_budgets"]["information_set"], 0)

        before = self.ledger_bytes()
        fresh_family = self.registration(
            "information-after-stop",
            "2026-08-11T01:20:00Z",
            family_definition={"mechanism": "fresh", "target": "net-utility"},
            display_name="renamed information presentation",
        )
        report = self.register(fresh_family)
        self.assertEqual(report["decision"], "STOP_CURRENT_FAMILY")
        self.assertFalse(report["appended"])
        self.assertEqual(self.ledger_bytes(), before)

    def test_tamper_delete_reorder_and_previous_record_hash_are_blocked(self):
        for experiment_id, minute in (("experiment-001", 0), ("experiment-002", 2)):
            request = self.registration(
                experiment_id, f"2026-08-11T00:{minute:02d}:00Z"
            )
            self.assertEqual(self.register(request)["decision"], "ALLOW_NEXT_EXPERIMENT")
            self.assertEqual(
                self.observe(
                    experiment_id,
                    "SUPPORTED",
                    f"2026-08-11T00:{minute + 1:02d}:00Z",
                )["decision"],
                "ALLOW_NEXT_EXPERIMENT",
            )
        pristine = self.ledger_bytes()
        lines = pristine.decode("ascii").splitlines()
        changed = json.loads(lines[0])
        changed["display_name"] = "edited"
        previous = json.loads(lines[1])
        previous["previous_record_hash"] = "f" * 64
        mutations = {
            "edit": [
                json.dumps(changed, sort_keys=True, separators=(",", ":")),
                *lines[1:],
            ],
            "delete": lines[:-1],
            "reorder": [lines[1], lines[0], *lines[2:]],
            "previous": [
                lines[0],
                json.dumps(previous, sort_keys=True, separators=(",", ":")),
                *lines[2:],
            ],
        }
        for name, mutated_lines in mutations.items():
            with self.subTest(name=name):
                self.ledger_path.write_bytes(
                    ("\n".join(mutated_lines) + "\n").encode("ascii")
                )
                with self.assertRaises(LEDGER.LedgerValidationError):
                    LEDGER.audit_ledger(self.ledger_path)
                self.ledger_path.write_bytes(pristine)

    def test_checkpoint_failure_reports_committed_append_and_recovers(self):
        proposal = self.registration()
        with mock.patch.object(
            LEDGER, "_write_checkpoint", side_effect=OSError("checkpoint fault")
        ):
            report = self.register(proposal)

        self.assertEqual(report["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(report["appended"])
        self.assertTrue(report["checkpoint_recovery_required"])
        self.assertEqual(len(self.ledger_bytes().splitlines()), 1)

        recovered = LEDGER.audit_next_experiment(
            self.ledger_path, self.config_path, proposal
        )
        self.assertEqual(recovered["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(recovered["registration_verified"])
        self.assertTrue(recovered["checkpoint_recovered"])
        checkpoint = self.ledger_path.with_suffix(
            self.ledger_path.suffix + ".checkpoint.json"
        )
        self.assertTrue(checkpoint.is_file())
        self.assertFalse(
            self.ledger_path.with_suffix(
                self.ledger_path.suffix + ".recovery.json"
            ).exists()
        )
        LEDGER.audit_ledger(self.ledger_path)

    def test_append_failure_before_commit_is_clean_and_retryable(self):
        proposal = self.registration()
        with mock.patch.object(
            LEDGER, "_durable_append", side_effect=OSError("append fault")
        ):
            report = self.register(proposal)

        self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(report["appended"])
        self.assertEqual(self.ledger_bytes(), b"")
        self.assertFalse(
            self.ledger_path.with_suffix(
                self.ledger_path.suffix + ".recovery.json"
            ).exists()
        )
        retried = self.register(proposal)
        self.assertEqual(retried["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(retried["appended"])

    def test_cross_process_register_and_observe_keep_contiguous_chain(self):
        module_path = str(pathlib.Path(LEDGER.__file__))
        context = multiprocessing.get_context("spawn")

        register_commands = []
        requests = []
        for number in range(4):
            request = self.registration(
                f"concurrent-{number}",
                f"2026-08-11T02:{number * 2:02d}:00Z",
                family_definition={
                    "mechanism": f"concurrent-{number}",
                    "target": "net-utility",
                },
            )
            requests.append(request)
            proposal_path = self.root / f"register-{number}.json"
            proposal_path.write_text(json.dumps(request), encoding="utf-8")
            register_commands.append(
                [
                    sys.executable,
                    module_path,
                    "register",
                    "--ledger",
                    str(self.ledger_path),
                    "--config",
                    str(self.config_path),
                    "--proposal",
                    str(proposal_path),
                ]
            )

        queue = context.Queue()
        processes = [
            context.Process(
                target=run_cli_after_delay,
                args=(command, number * 0.35, queue),
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
            observation = {
                "experiment_id": request["experiment_id"],
                "outcome": "SUPPORTED",
                "observed_at": f"2026-08-11T03:{number:02d}:00Z",
                    "result_identity": request["earliest_result_identity"],
                    "result_source_identity": request["result_source_identity"],
            }
            proposal_path = self.root / f"observe-{number}.json"
            proposal_path.write_text(json.dumps(observation), encoding="utf-8")
            observe_commands.append(
                [
                    sys.executable,
                    module_path,
                    "observe",
                    "--ledger",
                    str(self.ledger_path),
                    "--config",
                    str(self.config_path),
                    "--proposal",
                    str(proposal_path),
                ]
            )
        processes = [
            context.Process(
                target=run_cli_after_delay,
                args=(command, number * 0.35, queue),
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
            [record["sequence"] for record in state["records"]], list(range(1, 9))
        )
        self.assertEqual(len(state["registrations"]), 4)
        self.assertEqual(len(state["observations"]), 4)

    def test_cli_proposal_file_register_observe_and_audit(self):
        register_proposal = self.registration()
        register_path = self.root / "register.json"
        register_path.write_text(json.dumps(register_proposal), encoding="utf-8")
        base = [
            sys.executable,
            str(pathlib.Path(LEDGER.__file__)),
        ]
        registered = subprocess.run(
            base
            + [
                "register",
                "--ledger",
                str(self.ledger_path),
                "--config",
                str(self.config_path),
                "--proposal",
                str(register_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertTrue(json.loads(registered.stdout)["appended"])

        audited = subprocess.run(
            base
            + [
                "audit-next",
                "--ledger",
                str(self.ledger_path),
                "--config",
                str(self.config_path),
                "--proposal",
                str(register_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        audit_report = json.loads(audited.stdout)
        self.assertEqual(audited.returncode, 0, audited.stderr)
        self.assertEqual(audit_report["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(audit_report["registration_verified"])

        observation_path = self.root / "observe.json"
        observation_path.write_text(
            json.dumps(
                {
                    "experiment_id": register_proposal["experiment_id"],
                    "outcome": "SUPPORTED",
                    "observed_at": "2026-08-11T00:01:00Z",
                    "result_identity": register_proposal[
                        "earliest_result_identity"
                    ],
                    "result_source_identity": register_proposal[
                        "result_source_identity"
                    ],
                }
            ),
            encoding="utf-8",
        )
        observed = subprocess.run(
            base
            + [
                "observe",
                "--ledger",
                str(self.ledger_path),
                "--config",
                str(self.config_path),
                "--proposal",
                str(observation_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertTrue(json.loads(observed.stdout)["appended"])


if __name__ == "__main__":
    unittest.main()
