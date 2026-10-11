#!/usr/bin/env python3
"""Synthetic correction-controller checks; no remote execution or real evidence."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import forward_evidence_correction as correction


def synthetic_inventory():
    snapshot = {
        "capture_commit_sha": "1" * 40,
        "container": {"running": False, "expected_user": "65534:65534"},
        "source_files": {"source.py": {"sha256": "2" * 64}},
        "started_file": {"sha256": "3" * 64},
        "health_file": {"sha256": "4" * 64},
        "image_platform": {"os": "linux", "architecture": "amd64"},
        "segments": [{"segment_index": 0, "raw": {"sha256": "5" * 64}}],
        "identity_bound": True, "anomalies": [],
        "reported_health_status": "BOUNDED_COLLECTION_COMPLETE",
        "observed_epoch_ms": 1,
    }
    contract = {"release_sha": "6" * 40, "capture_commit_sha": "1" * 40,
                "inventory_static_sha256": correction.digest(correction.canonical(
                    {key: snapshot[key] for key in correction.STATIC_KEYS}))}
    result = {"inventory": {"snapshot": snapshot}}
    return result, contract


def synthetic_bundle():
    sources = {name: "# synthetic source, must not execute\n" for name in correction.SOURCES}
    contract = {"synthetic_only": True}
    certificate = b"synthetic certificate, never passed to OpenSSL"
    binding = {"contract_sha256": correction.digest(correction.canonical(contract)),
               "workflow_sha": "a" * 40, "run_id": "123456", "run_attempt": "1",
               "source_hashes": {name: correction.digest(value.encode()) for name, value in sources.items()},
               "certificate_sha256": correction.digest(certificate)}
    return {"contract": contract, "binding": binding, "sources": sources,
            "certificate_base64": base64.b64encode(certificate).decode()}


class ForwardCorrectionSyntheticTest(unittest.TestCase):
    def test_canonical_is_stable_and_rejects_nonfinite(self):
        self.assertEqual(correction.canonical({"b": [2, 1], "a": True}), b'{"a":true,"b":[2,1]}')
        self.assertEqual(correction.canonical({"a": True, "b": [2, 1]}),
                         correction.canonical({"b": [2, 1], "a": True}))
        self.assertEqual(correction.digest(b"synthetic"), hashlib.sha256(b"synthetic").hexdigest())
        with self.assertRaises(ValueError):
            correction.canonical({"number": float("nan")})

    def test_exclusive_json_preserves_existing_file_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            marker = root / "marker.json"
            correction.exclusive_json(marker, {"first": True})
            original = marker.read_bytes()
            self.assertEqual(json.loads(original), {"first": True})
            with self.assertRaises(FileExistsError):
                correction.exclusive_json(marker, {"second": True})
            self.assertEqual(marker.read_bytes(), original)
            link = root / "alias.json"
            link.symlink_to(marker)
            with self.assertRaises(FileExistsError):
                correction.exclusive_json(link, {"overwrite": True})
            self.assertEqual(marker.read_bytes(), original)

    def test_bound_inventory_exact_call_and_static_identity(self):
        result, contract = synthetic_inventory()
        execute = Mock(return_value=result)
        self.assertIs(correction.bound_inventory({"execute": execute}, contract), result)
        execute.assert_called_once_with("inventory", contract["release_sha"], contract["capture_commit_sha"], "")
        result["inventory"]["snapshot"]["observed_epoch_ms"] += 1
        self.assertIs(correction.bound_inventory({"execute": execute}, contract), result)

    def test_each_frozen_static_field_change_is_rejected(self):
        for key in correction.STATIC_KEYS:
            with self.subTest(key=key):
                result, contract = synthetic_inventory()
                snapshot = result["inventory"]["snapshot"]
                if key == "container":
                    snapshot[key]["expected_user"] = "0:0"
                elif isinstance(snapshot[key], dict):
                    snapshot[key]["unexpected"] = True
                elif isinstance(snapshot[key], list):
                    snapshot[key].append({"unexpected": True})
                else:
                    snapshot[key] = "f" * 40
                with self.assertRaisesRegex(ValueError, "FROZEN_INVENTORY_CHANGED"):
                    correction.bound_inventory({"execute": Mock(return_value=result)}, contract)

    def test_original_anomaly_or_unbound_identity_rejected(self):
        for field, value in (("anomalies", [{"code": "RAW_HASH_OR_SIZE_MISMATCH"}]),
                             ("identity_bound", False)):
            with self.subTest(field=field):
                result, contract = synthetic_inventory()
                result["inventory"]["snapshot"][field] = value
                with self.assertRaisesRegex(ValueError, "INVENTORY_IDENTITY_OR_CONTENT"):
                    correction.bound_inventory({"execute": Mock(return_value=result)}, contract)

    def test_running_or_nonterminal_capture_rejected_before_hash(self):
        for running, status in ((True, "BOUNDED_COLLECTION_COMPLETE"),
                                (False, "CAPTURING"), (False, "STOPPED_ERROR")):
            with self.subTest(running=running, status=status):
                result, contract = synthetic_inventory()
                snapshot = result["inventory"]["snapshot"]
                snapshot["container"]["running"] = running
                snapshot["reported_health_status"] = status
                with self.assertRaisesRegex(ValueError, "CAPTURE_NOT_TERMINAL"):
                    correction.bound_inventory({"execute": Mock(return_value=result)}, contract)

    def test_remote_wrong_attempt_rejected_before_compilation_or_transport(self):
        for attempt in ("0", "2", "01", 1, None):
            with self.subTest(attempt=attempt):
                bundle = synthetic_bundle()
                bundle["binding"]["run_attempt"] = attempt
                with patch.object(correction.os, "umask"), \
                     patch.object(correction, "exec", create=True) as execution, \
                     patch.object(correction.subprocess, "run") as subprocess_run:
                    result = correction.remote(bundle)
                self.assertEqual(result["status"], "INCOMPLETE")
                self.assertEqual(result["reason_code"], "WORKFLOW_BINDING_INVALID")
                execution.assert_not_called()
                subprocess_run.assert_not_called()

    def test_remote_contract_source_or_certificate_mismatch_rejected(self):
        for kind, expected in (("contract", "CONTRACT_HASH_MISMATCH"),
                               ("source", "CONTROLLER_HASH_MISMATCH"),
                               ("certificate", "CERTIFICATE_HASH_MISMATCH")):
            with self.subTest(kind=kind):
                bundle = synthetic_bundle()
                if kind == "contract":
                    bundle["contract"]["changed"] = True
                elif kind == "source":
                    bundle["sources"][correction.SOURCES[0]] += "# changed\n"
                else:
                    bundle["binding"]["certificate_sha256"] = "b" * 64
                with patch.object(correction.os, "umask"), \
                     patch.object(correction, "exec", create=True), \
                     patch.object(correction.subprocess, "run") as subprocess_run:
                    result = correction.remote(bundle)
                self.assertEqual(result["status"], "INCOMPLETE")
                self.assertEqual(result["reason_code"], expected)
                subprocess_run.assert_not_called()

    def test_remote_error_message_is_sanitized_and_no_marker_consumed(self):
        secret = "https://private.invalid/?token=synthetic-SECRET raw=confidential"
        bundle = synthetic_bundle()
        def populate(_compiled, namespace):
            if namespace["__name__"] == "forward_backup":
                namespace["check_certificate"] = Mock(side_effect=ValueError(secret))
        with patch.object(correction.os, "umask"), \
             patch.object(correction, "exec", create=True, side_effect=populate), \
             patch.object(correction, "exclusive_json") as marker, \
             patch.object(correction.subprocess, "run") as subprocess_run:
            result = correction.remote(bundle)
        self.assertEqual(result["reason_code"], "ValueError")
        self.assertEqual(result["stage"], "binding")
        self.assertNotIn(secret, json.dumps(result))
        for field in ("parent_gate_modified", "economic_evidence", "research_admitted", "account_access", "order_submission"):
            self.assertFalse(result[field])
        marker.assert_not_called()
        subprocess_run.assert_not_called()

    def test_workflow_second_attempt_never_invokes_transport(self):
        context = {"GITHUB_REPOSITORY": "skw2026/ai-trade", "GITHUB_REF": "refs/heads/main",
                   "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ATTEMPT": "2"}
        with patch.dict(os.environ, context, clear=True), patch.object(correction.os, "umask"), \
             patch.object(correction, "Path") as paths, \
             patch.object(correction.subprocess, "run") as subprocess_run:
            with self.assertRaisesRegex(ValueError, "WRONG_WORKFLOW_CONTEXT"):
                correction.workflow()
        paths.return_value.mkdir.assert_called_once_with(parents=True, exist_ok=False)
        subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
