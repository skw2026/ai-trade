#!/usr/bin/env python3
"""Negative gate scenarios below are intentional tests, not project failures."""
import fcntl
import copy
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

    def release_review(self):
        # All states, APIs and gh execution here are synthetic and isolated.
        state = {"schema_version": 1, "status": "BLOCKED", "history": [{"event": "original_failure"}],
                 "active": {"failure_id": "original-failure", "failure_count": 1, "label": "exact-source-cd",
                            "command_sha256": gate.command_hash(
                                ["gh", "run", "watch", "100", "--exit-status", "--interval", "30"],
                                str(pathlib.Path.cwd().resolve()))}}
        gate.save(self.state, state)
        record = self.review_record()
        record["corrective_release"] = {
            "repo": gate.RELEASE_REPO, "branch": "main", "workflow": ".github/workflows/cd.yml",
            "acceptance": "exact-cd-v1", "authorization": "synthetic explicit approval",
            "original_run_id": 100, "original_sha": "a" * 40, "cwd": str(pathlib.Path.cwd().resolve())}
        return record

    def approved_release(self, prepared=True):
        self.assertEqual(self.submit_review(self.release_review()).returncode, 0)
        state = gate.load(self.state)
        if prepared:
            state["active"]["release_prepared"] = True
            gate.save(self.state, state)
        return state

    def release_responses(self):
        old = {"id": 100, "head_sha": "a" * 40, "repository": {"full_name": gate.RELEASE_REPO},
               "head_repository": {"full_name": gate.RELEASE_REPO}, "head_branch": "main", "event": "push",
               "path": ".github/workflows/cd.yml", "run_attempt": 1,
               "status": "completed", "conclusion": "failure"}
        new = {**copy.deepcopy(old), "id": 200, "head_sha": "b" * 40, "conclusion": "success"}
        jobs = [{"name": name, "conclusion": "success", "steps": [
                    {"name": step, "conclusion": "success"} for step in steps]}
                for name, steps in gate.RELEASE_STEPS.items()]
        return [old, new, {"status": "ahead", "merge_base_commit": {"sha": "a" * 40}},
                copy.deepcopy(new), {"total_count": len(jobs), "jobs": jobs}]

    def test_release_review_cannot_replace_arbitrary_command_or_contract(self):
        record = self.release_review()
        for field, value in (("repo", "wrong/repo"), ("branch", "other"), ("workflow", "ci.yml"),
                             ("acceptance", "weaker"), ("original_run_id", 101),
                             ("original_sha", "short"), ("cwd", "/tmp"), ("authorization", "")):
            altered = copy.deepcopy(record)
            altered["corrective_release"][field] = value
            self.assertEqual(self.submit_review(altered).returncode, 2, field)
        state = gate.load(self.state)
        state["active"]["command_sha256"] = "not-a-gh-watch-command"
        gate.save(self.state, state)
        self.assertEqual(self.submit_review(record).returncode, 2)

    def test_release_prepare_is_fixed_full_suite_one_use_and_not_acceptance(self):
        state = self.approved_release(prepared=False)
        original = state["active"]["command_sha256"]
        names = ["validation_stop_gate_test", "offline_learning_harness_test"] + [f"fixture{i}" for i in range(108)]
        inventory = mock.Mock(stdout=json.dumps({"tests": [{"name": n} for n in names]}))
        with mock.patch.object(gate.subprocess, "run", side_effect=[inventory, mock.Mock(returncode=0)]) as run:
            self.assertEqual(gate.release_prepare(self.state, state, 600), 0)
            self.assertEqual(run.call_args.args[0], ["ctest", "--test-dir", "build", "--output-on-failure", "--no-tests=error"])
        self.assertEqual(state["status"], "RETRY_APPROVED")
        self.assertEqual(state["active"]["command_sha256"], original)
        with self.assertRaises(ValueError):
            gate.release_prepare(self.state, state, 600)
        self.assertEqual(self.cli("run", "--label", "skip", "--", *self.command).returncode, 2)
        self.assertEqual(self.cli("retry", "--", "gh", "run", "watch", "100",
                                  "--exit-status", "--interval", "30").returncode, 2)

    def test_release_preparation_missing_tests_or_failure_blocks(self):
        for failure in (mock.Mock(stdout='{"tests": []}'), OSError("synthetic")):
            state = self.approved_release(prepared=False)
            with mock.patch.object(gate.subprocess, "run", side_effect=[failure]):
                self.assertNotEqual(gate.release_prepare(self.state, state, 600), 0)
            self.assertEqual(state["status"], "BLOCKED")
            self.assertEqual(state["active"]["failure_count"], 2)

    def test_release_requires_preparation_review_integrity_and_original_directory(self):
        state = self.approved_release(prepared=False)
        with self.assertRaisesRegex(ValueError, "preparation"):
            gate.release_retry(self.state, state, "b" * 40, 200, 60)
        state["active"]["release_prepared"] = True
        for source, run_id in (("a" * 40, 200), ("b" * 40, 100), ("invalid", 200)):
            with self.assertRaises(ValueError):
                gate.release_retry(self.state, state, source, run_id, 60)
        self.assertEqual(self.cli("release-retry", "--sha", "b" * 40, "--run-id", "200", cwd=self.root).returncode, 2)
        self.review_file.write_text(self.review_file.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "review changed"):
            gate.release_retry(self.state, state, "b" * 40, 200, 60)

    def test_release_success_retains_original_failure_and_exact_new_binding(self):
        state = self.approved_release()
        with mock.patch.object(gate, "release_api", side_effect=self.release_responses()), \
             mock.patch.object(gate.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(gate.release_retry(self.state, state, "b" * 40, 200, 60), 0)
            self.assertEqual(run.call_args.args[0], ["gh", "run", "watch", "200", "--repo", gate.RELEASE_REPO,
                                                    "--exit-status", "--interval", "30"])
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["history"][0]["event"], "original_failure")
        self.assertEqual(state["history"][-1]["retry_of"], "original-failure")
        self.assertEqual(state["history"][-1]["binding"]["new_sha"], "b" * 40)
        with self.assertRaises(ValueError):
            gate.release_retry(self.state, state, "c" * 40, 300, 60)

    def test_release_metadata_mutations_or_missing_skipped_steps_block(self):
        mutations = [(1, field, value) for field, value in (
            ("id", 201), ("head_sha", "c" * 40), ("repository", {"full_name": "other/repo"}),
            ("head_repository", {"full_name": "other/repo"}), ("head_branch", "other"),
            ("event", "workflow_dispatch"), ("path", ".github/workflows/ci.yml"), ("run_attempt", 2))]
        mutations += [(0, "conclusion", "success"), (2, "status", "diverged"),
                      (3, "conclusion", "failure"), (4, "total_count", 3), (4, "jobs", [])]
        for index, field, value in mutations:
            with self.subTest(field=field, value=value):
                state = self.approved_release()
                responses = self.release_responses()
                responses[index][field] = value
                with mock.patch.object(gate, "release_api", side_effect=responses), \
                     mock.patch.object(gate.subprocess, "run", return_value=mock.Mock(returncode=0)):
                    self.assertEqual(gate.release_retry(self.state, state, "b" * 40, 200, 60), 1)
                self.assertEqual(state["status"], "BLOCKED")
                self.assertEqual(state["active"]["failure_count"], 2)
        for conclusion in ("skipped", "failure", None):
            payload = self.release_responses()[-1]
            payload["jobs"][0]["steps"][4]["conclusion"] = conclusion
            with self.assertRaisesRegex(ValueError, "required step"):
                gate.check_release_jobs(payload)

    def test_release_watch_failure_and_interruption_never_pass(self):
        for outcome in (mock.Mock(returncode=1), subprocess.TimeoutExpired("gh", 60), KeyboardInterrupt()):
            state = self.approved_release()
            with mock.patch.object(gate, "release_api", side_effect=self.release_responses()), \
                 mock.patch.object(gate.subprocess, "run", side_effect=[outcome]):
                self.assertNotEqual(gate.release_retry(self.state, state, "b" * 40, 200, 60), 0)
            self.assertEqual(state["status"], "BLOCKED")
            self.assertEqual(self.submit_review(self.review_record()).returncode, 2)

    def test_halted_gate_has_no_corrective_release_escape(self):
        state = self.approved_release()
        state["status"] = "HALTED"
        gate.save(self.state, state)
        for action in (lambda: gate.release_prepare(self.state, state, 60),
                       lambda: gate.release_retry(self.state, state, "b" * 40, 200, 60)):
            with self.assertRaises(ValueError):
                action()


if __name__ == "__main__":
    unittest.main()
