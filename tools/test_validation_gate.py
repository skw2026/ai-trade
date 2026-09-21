#!/usr/bin/env python3
"""Negative gate scenarios below are intentional tests, not project failures."""
import fcntl
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import validation_gate as gate

GATE = pathlib.Path(__file__).resolve().with_name("validation_gate.py")


class ValidationGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.state = self.root / "state.json"
        self.script = self.root / "validation.py"
        self.script.write_text("raise SystemExit(1)\n")
        self.command = [sys.executable, str(self.script)]
        self.review_file = self.root / "review.json"

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(GATE), "--state", str(self.state), *args],
                              capture_output=True, text=True, timeout=8, cwd=cwd)

    def state_value(self):
        return json.loads(self.state.read_text())

    def fail(self):
        result = self.cli("run", "--label", "fixed acceptance", "--", *self.command)
        self.assertEqual(result.returncode, 1, result.stderr)
        return self.state_value()["active"]

    def review_record(self):
        return {"failure_id": self.state_value()["active"]["failure_id"],
                "decision": "retry", "goal": "deterministic fixture",
                "expected": "exit zero", "observed": "exit one", "root_cause": "fixture exits one",
                "root_cause_status": "confirmed", "evidence": ["validation.py"],
                "next_action": "repair fixture then rerun identical command once",
                "scope_unchanged": True, "acceptance_unchanged": True,
                "path_verdict": "viable", "structural_issue": False,
                "path_review": {"goal_alignment": "same goal", "data_feasibility": "offline fixture",
                                "method_feasibility": "controlled exit", "budget_and_exit": "one retry"}}

    def submit_review(self, record):
        self.review_file.write_text(json.dumps(record))
        return self.cli("review", "--file", str(self.review_file))

    def test_failure_blocks_rerun_and_dependent_work(self):
        self.fail()
        self.assertEqual(self.cli("status").returncode, 2)
        self.assertEqual(self.cli("retry", "--", *self.command).returncode, 2)
        marker = self.root / "must-not-exist"
        result = self.cli("run", "--label", "downstream", "--", sys.executable, "-c",
                          f"from pathlib import Path; Path({str(marker)!r}).touch()")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(marker.exists())
        self.assertEqual(len(self.state_value()["history"]), 1)

    def test_unknown_cause_and_scope_or_criteria_changes_cannot_unlock(self):
        self.fail()
        for change in ({"root_cause_status": "unknown"}, {"scope_unchanged": False},
                       {"acceptance_unchanged": False}, {"path_verdict": "unproven"},
                       {"evidence": []}, {"failure_id": "stale"}, {"path_review": {}}):
            with self.subTest(change=change):
                self.assertEqual(self.submit_review({**self.review_record(), **change}).returncode, 2)
                self.assertEqual(self.state_value()["status"], "BLOCKED")

    def test_review_allows_only_same_command_retry_and_retains_history(self):
        self.fail()
        self.assertEqual(self.submit_review(self.review_record()).returncode, 0)
        self.assertEqual(self.cli("run", "--label", "skip original", "--", *self.command).returncode, 2)
        self.assertEqual(self.cli("retry", "--", sys.executable, "-c", "pass").returncode, 2)
        self.script.write_text("raise SystemExit(0)\n")
        self.assertEqual(self.cli("retry", "--", *self.command).returncode, 0)
        state = self.state_value()
        self.assertEqual(state["status"], "READY")
        self.assertEqual([h["event"] for h in state["history"]], ["validation", "review", "validation"])
        self.assertEqual(self.cli("retry", "--", *self.command).returncode, 2)

    def test_second_failure_and_first_structural_failure_require_route_reassessment(self):
        self.fail()
        self.assertEqual(self.submit_review({**self.review_record(), "structural_issue": True}).returncode, 2)
        self.assertEqual(self.submit_review(self.review_record()).returncode, 0)
        self.assertEqual(self.cli("retry", "--", *self.command).returncode, 1)
        self.assertEqual(self.state_value()["active"]["failure_count"], 2)
        self.assertEqual(self.submit_review(self.review_record()).returncode, 2)
        record = self.review_record()
        record["route_reassessment"] = {key: "fixture diagnosis evidence" for key in
            ("old_route_failure", "replacement", "feasibility_evidence", "budget", "stop_condition")}
        self.assertEqual(self.submit_review(record).returncode, 0)

    def test_review_edit_after_approval_is_rejected(self):
        self.fail()
        self.assertEqual(self.submit_review(self.review_record()).returncode, 0)
        self.review_file.write_text(self.review_file.read_text() + "\n")
        self.assertEqual(self.cli("retry", "--", *self.command).returncode, 2)

    def test_retry_cannot_change_working_directory(self):
        self.fail()
        self.assertEqual(self.submit_review(self.review_record()).returncode, 0)
        self.script.write_text("raise SystemExit(0)\n")
        self.assertEqual(self.cli("retry", "--", *self.command, cwd=self.root).returncode, 2)
        self.assertEqual(self.state_value()["status"], "RETRY_APPROVED")
        self.assertEqual(self.cli("retry", "--", *self.command).returncode, 0)

    def test_stop_does_not_require_fabricating_a_confirmed_cause(self):
        self.fail()
        record = {**self.review_record(), "decision": "stop", "root_cause_status": "unknown"}
        self.assertEqual(self.submit_review(record).returncode, 0)
        self.assertEqual(self.state_value()["status"], "HALTED")
        self.assertEqual(self.cli("run", "--label", "bypass", "--", *self.command).returncode, 2)

    def test_corrupt_or_interrupted_state_cannot_be_implicitly_reset(self):
        self.state.write_text("corrupt json")
        self.assertEqual(self.cli("status").returncode, 2)
        self.assertEqual(self.state.read_text(), "corrupt json")
        self.state.write_text(json.dumps({"schema_version": 1, "status": "RUNNING", "active": {}, "history": []}))
        self.assertEqual(self.cli("run", "--label", "bypass", "--", *self.command).returncode, 2)

    def test_timeout_is_a_failure_not_permission_to_retry(self):
        self.script.write_text("import time; time.sleep(5)\n")
        result = self.cli("run", "--label", "timeout", "--timeout", "1", "--", *self.command)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(self.state_value()["status"], "BLOCKED")

    def test_concurrent_validation_is_rejected(self):
        with self.state.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.cli("run", "--label", "concurrent", "--", *self.command).returncode, 2)
        self.assertFalse(self.state.exists())

    def test_raw_command_arguments_are_not_archived(self):
        result = self.cli("run", "--label", "redaction", "--", sys.executable, "-c",
                          "private_value='synthetic-secret-marker'; raise SystemExit(1)")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("synthetic-secret-marker", self.state.read_text())

    def test_reviewed_repair_build_cannot_clear_original_acceptance(self):
        original = self.fail()["command_sha256"]
        command = ["cmake", "--build", "synthetic-build"]
        record = {**self.review_record(), "repair_build_argv": command,
                  "repair_build_cwd": str(pathlib.Path.cwd().resolve())}
        self.assertEqual(self.submit_review(record).returncode, 0)
        self.assertEqual(self.cli("retry", "--", *self.command).returncode, 2)
        self.assertEqual(self.cli("repair-build", "--", "cmake", "--build", "wrong").returncode, 2)
        state = gate.load(self.state)
        with mock.patch.object(gate.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(gate.repair_build(self.state, state, command, 10), 0)
            run.assert_called_once()
        self.assertEqual(state["status"], "RETRY_APPROVED")
        self.assertEqual(state["active"]["command_sha256"], original)
        self.assertEqual(self.cli("repair-build", "--", *command).returncode, 2)
        self.assertEqual(self.cli("run", "--label", "downstream", "--", *self.command).returncode, 2)
        self.script.write_text("raise SystemExit(0)\n")
        self.assertEqual(self.cli("retry", "--", *self.command).returncode, 0)

    def test_failed_repair_requires_new_review_and_route_reassessment(self):
        self.fail()
        command = ["cmake", "--build", "synthetic-build"]
        record = {**self.review_record(), "repair_build_argv": command,
                  "repair_build_cwd": str(pathlib.Path.cwd().resolve())}
        self.assertEqual(self.submit_review(record).returncode, 0)
        state = gate.load(self.state)
        with mock.patch.object(gate.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertEqual(gate.repair_build(self.state, state, command, 10), 1)
        self.assertEqual(state["status"], "BLOCKED")
        self.assertEqual(state["active"]["failure_count"], 2)
        self.assertEqual(self.submit_review(self.review_record()).returncode, 2)

    def test_repair_requires_build_command_and_exact_review_identity(self):
        self.fail()
        record = {**self.review_record(), "repair_build_argv": ["echo", "skip"],
                  "repair_build_cwd": str(pathlib.Path.cwd().resolve())}
        self.assertEqual(self.submit_review(record).returncode, 2)
        record["repair_build_argv"] = ["cmake", "--build", "synthetic"]
        self.assertEqual(self.submit_review(record).returncode, 0)
        self.review_file.write_text(self.review_file.read_text()+"\n")
        self.assertEqual(self.cli("repair-build", "--", *record["repair_build_argv"]).returncode, 2)


if __name__ == "__main__":
    unittest.main()
