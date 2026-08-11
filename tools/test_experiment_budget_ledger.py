#!/usr/bin/env python3

import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


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


LEDGER = load_module()


class ExperimentBudgetLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.ledger_path = self.root / "experiments.jsonl"
        self.config_path = self.root / "decision_evidence_validation.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "schema_version": "decision_evidence_validation_v1",
                    "failure_budgets": {"family": 3, "information_set": 8},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.benchmark_id = "b" * 64
        self.information_set_definition = {
            "display_name": "order-flow information",
            "signals": ["book_imbalance", "trade_flow"],
        }
        self.family_definition = {
            "display_name": "depth interaction",
            "claim": "depth interaction improves stress-net utility",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def registration(
        self,
        experiment_id="experiment-001",
        registered_at="2026-08-11T00:00:00Z",
        information_set_definition=None,
        family_definition=None,
        changed_dimensions=None,
    ):
        information = copy.deepcopy(
            information_set_definition or self.information_set_definition
        )
        family = copy.deepcopy(family_definition or self.family_definition)
        return {
            "experiment_id": experiment_id,
            "benchmark_id": self.benchmark_id,
            "information_set_definition": information,
            "information_set_id": LEDGER.stable_definition_id(information),
            "hypothesis_family_definition": family,
            "hypothesis_family_id": LEDGER.stable_definition_id(family),
            "changed_dimensions": changed_dimensions or ["target_transform"],
            "expected_direction": "increase_stress_net_utility",
            "stop_condition": "stop_after_family_failure_budget",
            "registered_at": registered_at,
        }

    def register(self, registration):
        return LEDGER.register_experiment(
            self.ledger_path, self.config_path, registration
        )

    def observe(self, experiment_id, outcome, observed_at):
        return LEDGER.observe_experiment(
            self.ledger_path,
            self.config_path,
            {
                "experiment_id": experiment_id,
                "outcome": outcome,
                "observed_at": observed_at,
            },
        )

    def test_empty_ledger_registers_and_reports_both_remaining_budgets(self):
        request = self.registration()
        report = self.register(request)

        self.assertEqual(report["decision"], "ALLOW_NEXT_EXPERIMENT")
        self.assertTrue(report["appended"])
        self.assertEqual(report["remaining_budgets"], {"family": 3, "information_set": 8})
        records = LEDGER.audit_ledger(self.ledger_path)["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["sequence"], 1)
        self.assertEqual(records[0]["previous_hash"], "0" * 64)
        self.assertEqual(
            records[0]["record_hash"], LEDGER.record_hash(records[0])
        )

        renamed = copy.deepcopy(self.family_definition)
        renamed["display_name"] = "renamed family"
        self.assertEqual(
            LEDGER.stable_definition_id(self.family_definition),
            LEDGER.stable_definition_id(renamed),
        )

    def test_register_rejects_invalid_identity_and_dimension_without_append(
        self,
    ):
        first = self.registration()
        self.assertEqual(self.register(first)["decision"], "ALLOW_NEXT_EXPERIMENT")

        cases = []
        missing = self.registration("experiment-missing", "2026-08-11T00:01:00Z")
        missing.pop("expected_direction")
        cases.append((missing, "BLOCK_INVALID_LEDGER"))
        wrong_id = self.registration("experiment-hash", "2026-08-11T00:01:00Z")
        wrong_id["hypothesis_family_id"] = "a" * 64
        cases.append((wrong_id, "BLOCK_INVALID_LEDGER"))
        duplicate = self.registration("experiment-001", "2026-08-11T00:01:00Z")
        cases.append((duplicate, "BLOCK_INVALID_LEDGER"))
        drift = self.registration("experiment-drift", "2026-08-11T00:01:00Z")
        drift["benchmark_id"] = "c" * 64
        cases.append((drift, "BLOCK_INVALID_LEDGER"))
        multi = self.registration(
            "experiment-multi",
            "2026-08-11T00:01:00Z",
            changed_dimensions=["feature_set", "target_transform"],
        )
        cases.append((multi, "STOP_CURRENT_FAMILY"))

        for request, expected in cases:
            before = self.ledger_path.read_bytes()
            report = self.register(request)
            self.assertEqual(report["decision"], expected, report)
            self.assertFalse(report["appended"])
            self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_outcomes_consume_budget_once_and_threshold_observation_is_appended_before_stop(self):
        family_id = LEDGER.stable_definition_id(self.family_definition)
        for number, outcome in enumerate(
            ["SUPPORTED", "FALSIFIED", "INCONCLUSIVE", "FALSIFIED"], start=1
        ):
            registered_at = f"2026-08-11T00:{number * 2:02d}:00Z"
            observed_at = f"2026-08-11T00:{number * 2 + 1:02d}:00Z"
            experiment_id = f"experiment-{number:03d}"
            self.assertEqual(
                self.register(
                    self.registration(experiment_id, registered_at)
                )["decision"],
                "ALLOW_NEXT_EXPERIMENT",
            )
            observation = self.observe(experiment_id, outcome, observed_at)
            expected = "STOP_CURRENT_FAMILY" if number == 4 else "ALLOW_NEXT_EXPERIMENT"
            self.assertEqual(observation["decision"], expected)
            self.assertTrue(observation["appended"])

        audit = LEDGER.audit_next_experiment(
            self.ledger_path,
            self.config_path,
            {
                "hypothesis_family_id": family_id,
                "information_set_id": LEDGER.stable_definition_id(
                    self.information_set_definition
                ),
            },
        )
        self.assertEqual(audit["decision"], "STOP_CURRENT_FAMILY")
        self.assertEqual(audit["remaining_budgets"]["family"], 0)
        before = self.ledger_path.read_bytes()
        blocked = self.register(
            self.registration("experiment-after-stop", "2026-08-11T00:10:00Z")
        )
        self.assertEqual(blocked["decision"], "STOP_CURRENT_FAMILY")
        self.assertFalse(blocked["appended"])
        self.assertEqual(self.ledger_path.read_bytes(), before)

        repeated = self.observe(
            "experiment-004", "FALSIFIED", "2026-08-11T00:11:00Z"
        )
        self.assertEqual(repeated["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(repeated["appended"])
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_eighth_information_failure_stops_renamed_information_set(self):
        information_id = LEDGER.stable_definition_id(
            self.information_set_definition
        )
        last_report = None
        for number in range(8):
            family = {
                "display_name": f"family {number}",
                "claim": f"independent claim {number}",
            }
            experiment_id = f"information-failure-{number}"
            minute = number * 2
            request = self.registration(
                experiment_id,
                f"2026-08-11T01:{minute:02d}:00Z",
                family_definition=family,
            )
            self.assertEqual(self.register(request)["decision"], "ALLOW_NEXT_EXPERIMENT")
            last_report = self.observe(
                experiment_id,
                "INCONCLUSIVE",
                f"2026-08-11T01:{minute + 1:02d}:00Z",
            )
        self.assertEqual(last_report["decision"], "STOP_CURRENT_FAMILY")
        self.assertEqual(last_report["remaining_budgets"]["information_set"], 0)

        renamed_information = copy.deepcopy(self.information_set_definition)
        renamed_information["display_name"] = "renamed information set"
        request = self.registration(
            "information-after-stop",
            "2026-08-11T01:20:00Z",
            information_set_definition=renamed_information,
            family_definition={"claim": "fresh family"},
        )
        self.assertEqual(request["information_set_id"], information_id)
        before = self.ledger_path.read_bytes()
        report = self.register(request)
        self.assertEqual(report["decision"], "STOP_CURRENT_FAMILY")
        self.assertFalse(report["appended"])
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_invalid_time_and_outcome_are_blocked_without_mutation(self):
        invalid = self.registration(registered_at="2026-08-11 00:00:00")
        report = self.register(invalid)
        self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER")
        self.assertFalse(self.ledger_path.exists())

        self.assertEqual(
            self.register(self.registration())["decision"],
            "ALLOW_NEXT_EXPERIMENT",
        )
        before = self.ledger_path.read_bytes()
        for outcome, observed_at in (
            ("POSITIVE", "2026-08-11T00:01:00Z"),
            ("FALSIFIED", "2026-08-11T00:00:00Z"),
        ):
            report = self.observe("experiment-001", outcome, observed_at)
            self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER")
            self.assertFalse(report["appended"])
            self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_edit_delete_reorder_and_previous_hash_tampering_are_detected(self):
        for experiment_id, minute in (("experiment-001", 0), ("experiment-002", 2)):
            self.assertEqual(
                self.register(
                    self.registration(
                        experiment_id, f"2026-08-11T00:{minute:02d}:00Z"
                    )
                )["decision"],
                "ALLOW_NEXT_EXPERIMENT",
            )
            self.assertEqual(
                self.observe(
                    experiment_id,
                    "SUPPORTED",
                    f"2026-08-11T00:{minute + 1:02d}:00Z",
                )["decision"],
                "ALLOW_NEXT_EXPERIMENT",
            )
        pristine = self.ledger_path.read_bytes()
        lines = pristine.decode("ascii").splitlines()

        mutations = {
            "edit": [lines[0].replace("experiment-001", "experiment-xyz"), *lines[1:]],
            "delete": lines[:-1],
            "reorder": [lines[1], lines[0], *lines[2:]],
            "previous": [
                lines[0],
                json.dumps(
                    {**json.loads(lines[1]), "previous_hash": "f" * 64},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                *lines[2:],
            ],
        }
        for name, mutated_lines in mutations.items():
            self.ledger_path.write_bytes(
                ("\n".join(mutated_lines) + "\n").encode("ascii")
            )
            report = LEDGER.audit_next_experiment(
                self.ledger_path, self.config_path, None
            )
            self.assertEqual(report["decision"], "BLOCK_INVALID_LEDGER", name)
            self.assertFalse(report["appended"])
            self.ledger_path.write_bytes(pristine)

    def test_cli_register_and_audit_next_emit_machine_readable_decisions(self):
        request = self.registration()
        command = [
            sys.executable,
            str(pathlib.Path(LEDGER.__file__)),
            "register",
            "--ledger",
            str(self.ledger_path),
            "--config",
            str(self.config_path),
            "--request-json",
            json.dumps(request),
        ]
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout)["decision"],
            "ALLOW_NEXT_EXPERIMENT",
        )

        audited = subprocess.run(
            [
                sys.executable,
                str(pathlib.Path(LEDGER.__file__)),
                "audit-next",
                "--ledger",
                str(self.ledger_path),
                "--config",
                str(self.config_path),
                "--hypothesis-family-id",
                request["hypothesis_family_id"],
                "--information-set-id",
                request["information_set_id"],
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(audited.returncode, 0, audited.stderr)
        self.assertEqual(json.loads(audited.stdout)["decision"], "ALLOW_NEXT_EXPERIMENT")


if __name__ == "__main__":
    unittest.main()
