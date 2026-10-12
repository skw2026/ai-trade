#!/usr/bin/env python3
"""Fail-closed local completion tests using only synthetic in-memory artifacts."""
import copy
import io
import json
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import forward_correction_local as local


class LocalCorrectionGuardTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.head, self.dirty = "a" * 40, b""
        self.contract = {"inventory_static_sha256": "b" * 64, "parent_gate_sha256": "c" * 64,
                         "image": "synthetic@sha256:" + "d" * 64, "platform": "linux/amd64",
                         "expected_file_hashes": {"synthetic": "e" * 64}}
        self.sources = {str(Path("tools", name)): ("synthetic-source:" + name).encode()
                        for name in local.correction.SOURCES}
        self.attempt = {"workflow_sha": self.head,
                        "contract_sha256": local.correction.digest(local.correction.canonical(self.contract)),
                        "certificate_sha256": "f" * 64,
                        "source_hashes": {name: local.correction.digest(self.sources[str(Path("tools", name))])
                                          for name in local.correction.SOURCES}}
        self.run = {"head_sha": self.head, "head_branch": "main", "event": "workflow_dispatch",
                    "run_attempt": 1, "path": ".github/workflows/forward-correction.yml",
                    "repository": {"full_name": "skw2026/ai-trade"}, "status": "completed",
                    "conclusion": "success", "id": 12345}
        step_names = ("Synthetic backup and controller checks", "Native Linux deployment lifecycle regression",
                      "Same originals precise-version correction", "Retain ciphertext and sanitized receipt")
        self.jobs = {"jobs": [{"conclusion": "success", "run_id": 12345, "run_attempt": 1,
                               "steps": [{"name": name, "conclusion": "success"} for name in step_names]}]}
        self.binding = dict(self.attempt, run_id="12345", run_attempt="1")
        self.cipher = b"synthetic-ciphertext-no-real-key"
        self.first = {"raw_sha256": "0" * 64, "status": "SEALED"}
        self.receipt = {"binding": self.binding, "status": "REMOTE_REPLAY_VERIFIED_ENCRYPTED_BACKUP_READY",
                        "transport_exit": 0, "trading_identity_unchanged": True,
                        "inventory_static_sha256": self.contract["inventory_static_sha256"],
                        "runtime_flags": {"operator_reduce_only": "true", "force_reduce_only": "true"},
                        "ciphertext_sha256": local.correction.digest(self.cipher), "ciphertext_bytes": len(self.cipher),
                        "first_segment": self.first}
        self.reads = {str(local.ROOT / "attempt-start.json"): self.attempt,
                      str(local.ROOT / "github-run.json"): self.run,
                      str(local.ROOT / "github-jobs.json"): self.jobs,
                      str(local.ROOT / "download/remote-receipt.json"): self.receipt,
                      str(local.ROOT / "download/run-binding.json"): self.binding,
                      "/synthetic/correction/restored/capture/segment-000/receipt.json": self.first}
        self.bytes = dict(self.sources)
        self.bytes[str(local.ROOT / "download/first-segment.cms")] = self.cipher
        self.stack.enter_context(patch.object(local, "read", side_effect=lambda p: self.reads[str(p)]))
        self.stack.enter_context(patch.object(Path, "read_bytes", autospec=True, side_effect=lambda p: self.bytes[str(p)]))
        self.stack.enter_context(patch.object(Path, "resolve", return_value=Path("/synthetic/correction")))
        def git(argv, **kwargs):
            if argv == ["git", "rev-parse", "HEAD"]:
                return self.head + "\n"
            if argv == ["git", "status", "--porcelain"]:
                return self.dirty
            raise AssertionError("unexpected non-git process")
        self.stack.enter_context(patch.object(local.subprocess, "check_output", side_effect=git))
        self.process = self.stack.enter_context(patch.object(local.subprocess, "run", return_value=
            SimpleNamespace(returncode=0, stdout=json.dumps(self.first).encode())))
        self.protected = self.stack.enter_context(patch.object(local, "protected", return_value=201))
        self.restore = self.stack.enter_context(patch.object(local.backup, "restore", return_value={"synthetic": True}))
        self.replay_spec = self.stack.enter_context(patch.object(local.ecs, "replay_spec", return_value=
            ["docker", "run", "--rm", "--network=none", "synthetic-image"]))
        self.write = self.stack.enter_context(patch.object(local.correction, "exclusive_json"))

    def rejected_before_recovery(self, code):
        with self.assertRaisesRegex(ValueError, code):
            local.complete(self.contract)
        self.restore.assert_not_called()
        self.process.assert_not_called()
        self.replay_spec.assert_not_called()
        self.write.assert_not_called()

    def test_changed_contract_rejected(self):
        self.contract["extra"] = "changed"
        self.rejected_before_recovery("LOCAL_CONTRACT_CHANGED")

    def test_changed_head_rejected(self):
        self.head = "9" * 40
        self.rejected_before_recovery("LOCAL_CONTROLLER_CHANGED")

    def test_dirty_worktree_rejected(self):
        self.dirty = b" M tools/controller.py\n"
        self.rejected_before_recovery("LOCAL_CONTROLLER_CHANGED")

    def test_changed_source_hash_rejected(self):
        self.bytes[str(Path("tools", local.correction.SOURCES[0]))] += b"changed"
        self.rejected_before_recovery("LOCAL_SOURCE_CHANGED")

    def test_wrong_github_run_identity_rejected(self):
        original = copy.deepcopy(self.run)
        variants = {"head_sha": "8" * 40, "head_branch": "other", "event": "push", "run_attempt": 2,
                    "path": ".github/workflows/other.yml", "repository": {"full_name": "other/repo"},
                    "status": "in_progress", "conclusion": "failure"}
        for key, value in variants.items():
            with self.subTest(key=key):
                self.run.clear()
                self.run.update(copy.deepcopy(original))
                self.run[key] = value
                self.rejected_before_recovery("GITHUB_RUN_IDENTITY_OR_RESULT")

    def test_empty_jobs_rejected(self):
        self.jobs["jobs"] = []
        self.rejected_before_recovery("GITHUB_REQUIRED_STEP_NOT_SUCCESS")

    def test_empty_or_missing_required_steps_rejected(self):
        original = copy.deepcopy(self.jobs["jobs"][0]["steps"])
        for steps in ([], original[:-1]):
            with self.subTest(count=len(steps)):
                self.jobs["jobs"][0]["steps"] = steps
                self.rejected_before_recovery("GITHUB_REQUIRED_STEP_NOT_SUCCESS")

    def test_wrong_job_run_or_attempt_rejected(self):
        job = self.jobs["jobs"][0]
        job["run_id"] = 99999
        self.rejected_before_recovery("GITHUB_REQUIRED_STEP_NOT_SUCCESS")
        job["run_id"], job["run_attempt"] = 12345, 2
        self.rejected_before_recovery("GITHUB_REQUIRED_STEP_NOT_SUCCESS")

    def test_skipped_required_step_rejected(self):
        self.jobs["jobs"][0]["steps"][0]["conclusion"] = "skipped"
        self.rejected_before_recovery("GITHUB_REQUIRED_STEP_NOT_SUCCESS")

    def test_off_host_receipt_mismatch_records_incomplete_terminal(self):
        self.process.return_value.stdout = json.dumps({"different": True}).encode()
        with redirect_stdout(io.StringIO()):
            code = local.complete(self.contract)
        self.assertEqual(code, 2)
        self.restore.assert_called_once()
        self.process.assert_called_once()
        self.assertEqual(self.write.call_count, 2)
        destination, result = self.write.call_args.args
        self.assertEqual(destination, local.ROOT / "terminal-result.json")
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["reason_code"], "OFF_HOST_REPLAY_MISMATCH")
        self.assertEqual(result["parent_status"], "HALTED")
        self.assertFalse(result["economic_evidence"])
        self.assertFalse(result["research_admitted"])

    def test_synthetic_success_stays_first_segment_only(self):
        with redirect_stdout(io.StringIO()):
            code = local.complete(self.contract)
        self.assertEqual(code, 0)
        result = self.write.call_args.args[1]
        self.assertEqual(result["status"], "CORRECTED_FIRST_SEGMENT_DELIVERY_VERIFIED")
        self.assertTrue(result["off_host_replay_identical"])
        self.assertFalse(result["whole_batch_replayed"])
        self.assertFalse(result["whole_batch_backed_up"])
        self.assertFalse(result["economic_evidence"])
        self.assertEqual(result["parent_status"], "HALTED")
        argv = self.process.call_args.args[0]
        self.assertIn("--pull=never", argv)
        self.assertEqual(argv[argv.index("--platform") + 1], "linux/amd64")
        self.assertIn("--network=none", argv)


class AuthorizedReservationTest(unittest.TestCase):
    def setUp(self):
        self.contract = {"synthetic": True}
        self.contract_hash = local.correction.digest(local.correction.canonical(self.contract))
        self.source = b"synthetic unchanged remote code"
        self.old = {"workflow_sha": "a" * 40, "contract_sha256": self.contract_hash,
                    "source_hashes": {"forward_evidence_ecs.py": local.correction.digest(self.source)}}
        self.blocked = {"workflow_sha": "a" * 40, "status": "DISPATCH_NOT_EXECUTED_APPROVAL_REQUIRED",
                        "remote_run_created": False, "formal_replay_executed": False, "ciphertext_exported": False}
        self.approval = {"status": "APPROVED_BY_USER", "date": "2026-10-12", "user_reply": "允许",
                         "contract_sha256": self.contract_hash, "original_first_segment_correction": True,
                         "fixed_five_file_ciphertext_to_github_artifact": True, "retention_days": 7,
                         "private_key_local_only": True, "maximum_actual_runs": 1}
        self.reads = {str(local.ROOT / "attempt-start.json"): self.old,
                      str(local.ROOT / "dispatch-blocked.json"): self.blocked,
                      str(local.APPROVAL): self.approval}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(local, "read", side_effect=lambda p: self.reads[str(p)]))
        self.stack.enter_context(patch.object(Path, "read_bytes", autospec=True,
            side_effect=lambda p: self.source if str(p).startswith("tools/")
            else local.correction.canonical(self.reads[str(p)])))

    def test_approved_chain_binds_old_records_without_replacing_them(self):
        before = copy.deepcopy(self.reads)
        chain = local.authorized_chain(self.contract)
        self.assertEqual(set(chain), {"approval_sha256", "previous_attempt_sha256", "previous_dispatch_block_sha256"})
        self.assertEqual(self.reads, before)

    def test_changed_approval_rejected(self):
        self.approval["retention_days"] = 8
        with self.assertRaisesRegex(ValueError, "EXPLICIT_APPROVAL_MISSING_OR_CHANGED"):
            local.authorized_chain(self.contract)

    def test_executed_previous_attempt_rejected(self):
        for field in ("remote_run_created", "formal_replay_executed", "ciphertext_exported"):
            with self.subTest(field=field):
                self.blocked[field] = True
                with self.assertRaisesRegex(ValueError, "PREVIOUS_RESERVATION_NOT_UNEXECUTED"):
                    local.authorized_chain(self.contract)
                self.blocked[field] = False

    def test_changed_previous_source_rejected(self):
        self.source += b"changed"
        with self.assertRaisesRegex(ValueError, "PREVIOUS_REMOTE_IMPLEMENTATION_CHANGED"):
            local.authorized_chain(self.contract)

    def test_existing_real_run_rejected_before_reservation(self):
        with patch.object(local, "protected"), patch.object(local.subprocess, "check_output",
             return_value=b'{"total_count":1,"workflow_runs":[{"id":1}]}'), \
             patch.object(local.correction, "exclusive_json") as writer:
            with self.assertRaisesRegex(ValueError, "CORRECTION_ALREADY_DISPATCHED"):
                local.prepare(self.contract, authorized=True)
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
