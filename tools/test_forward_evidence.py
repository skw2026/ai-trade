#!/usr/bin/env python3
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from types import SimpleNamespace

import forward_evidence as forward
import forward_evidence_ecs as ecs


def rows():
    result = []
    for symbol in forward.SYMBOLS:
        for topic in ("orderbook.50." + symbol, "publicTrade." + symbol):
            payload = {"topic": topic, "type": "snapshot", "ts": 1000, "cts": 999}
            if topic.startswith("orderbook"):
                payload["data"] = {"s": symbol, "u": 1, "seq": 1,
                                   "b": [["99", "2"]], "a": [["101", "2"]]}
            else:
                payload["data"] = [{"s": symbol, "T": 1000, "S": "Buy", "p": "100", "v": "1", "i": symbol}]
            index = len(result) + 1
            result.append({"sequence": index, "receive_utc_ns": 1_800_000_000_000_000_000 + index * 1_000_000,
                           "receive_monotonic_ns": 100_000_000_000 + index * 1_000_000,
                           "wire": json.dumps(payload)})
    return result


def make_segment(root):
    raw = root / "receive.jsonl.gz"
    with gzip.open(raw, "wt") as handle:
        for row in rows():
            handle.write(forward.canonical(row) + "\n")
    observation = forward.replay(raw)
    receipt = {"schema_version": forward.SCHEMA, "status": "SEALED", "raw_sha256": forward.digest(raw),
               "observation": observation, "economic_evidence": False}
    forward.write_json(root / "receipt.json", receipt)
    return receipt


def make_inventory_batch(root, count=3):
    batch = root / ecs.STARTUP_REPAIR_BATCH
    code, capture = batch / "code", batch / "capture"
    code.mkdir(parents=True)
    capture.mkdir()
    for name in ecs.FILES:
        (code / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    started = {"schema_version": forward.SCHEMA, "started_epoch_ms": 1_800_000_000_000,
               "source_sha256": ecs.STARTUP_SOURCE_HASHES["forward_evidence.py"],
               "book_source_sha256": ecs.STARTUP_SOURCE_HASHES["collect_bybit_microstructure.py"],
               "maximum_seconds": 86400, "maximum_bytes": ecs.INVENTORY_MAX_BYTES,
               "maximum_segments": 97, "research_domain": "prospective_development",
               "economic_evidence": False, "account_access": False, "order_submission": False}
    (capture / "started.json").write_text(json.dumps(started))
    previous, total = None, 0
    for index in range(count):
        directory = capture / ("segment-%03d" % index)
        directory.mkdir()
        receipt = make_segment(directory)
        receipt.update(segment_index=index, previous_receipt_sha256=previous,
                       raw_bytes=(directory / "receive.jsonl.gz").stat().st_size,
                       source_sha256=ecs.STARTUP_SOURCE_HASHES["forward_evidence.py"])
        for field in ("first_receive_epoch_ms", "last_receive_epoch_ms"):
            receipt["observation"][field] += index * 1_000
        (directory / "receipt.json").write_text(json.dumps(receipt))
        previous = forward.digest(directory / "receipt.json")
        total += receipt["raw_bytes"]
    (capture / "health.json").write_text(json.dumps({"status": "BOUNDED_COLLECTION_COMPLETE",
        "completed_segments": count, "raw_bytes": total, "updated_epoch_ms": 1_800_000_100_000}))
    container = {"id": "a" * 64, "image": ecs.INVENTORY_IMAGE,
                 "image_id": ecs.INVENTORY_IMAGE.split("@", 1)[1], "user": "65534:65534",
                 "command": ["/forward/forward_evidence.py", "run", "--root=/evidence"],
                 "entrypoint": ["python3"], "readonly": True, "caps": ["ALL"],
                 "cap_add": None, "security_opt": ["no-new-privileges"], "memory": 512 * 1024**2,
                 "nano_cpus": 500_000_000, "pids_limit": 64,
                 "privileged": False, "restart": "no", "running": False,
                 "exit_code": 0, "oom_killed": False, "restarts": 0,
                 "mounts": [{"Type": "bind", "Source": str(code), "Destination": "/forward", "RW": False},
                            {"Type": "bind", "Source": str(capture), "Destination": "/evidence", "RW": True}]}
    return batch, container


class ForwardInventoryDiagnosticTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.batch, self.container = make_inventory_batch(self.root)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(ecs, "ROOT", self.root))
        self.container_probe = self.stack.enter_context(patch.object(ecs, "inventory_container", return_value=self.container))
        self.stack.enter_context(patch.object(ecs, "inventory_image_platform", return_value={"os": "linux", "architecture": "amd64"}))
        # Synthetic host ownership: still execute all real no-follow, bounded reads.
        original_file = ecs.inventory_file
        def target_owner(*args, **kwargs):
            result = original_file(*args, **kwargs)
            metadata = result[0] if isinstance(result, tuple) else result
            metadata.update(uid=65534, gid=65534)
            return result
        self.stack.enter_context(patch.object(ecs, "inventory_file", side_effect=target_owner))
        self.stack.enter_context(patch.object(ecs, "command", side_effect=AssertionError("unexpected command")))
        self.stack.enter_context(patch.object(forward, "replay", side_effect=AssertionError("inventory must not replay")))
        self.stack.enter_context(patch.object(forward, "verify_segment", side_effect=AssertionError("inventory must not verify")))
        self.stack.enter_context(patch.object(ecs, "replay_spec", side_effect=AssertionError("inventory must not launch replay")))

    def collect(self):
        return ecs.inventory(self.batch, ecs.INVENTORY_IMAGE)

    def codes(self, result):
        return {item["code"] for item in result["snapshot"]["anomalies"]}

    def receipt(self, index=1):
        path = self.batch / ("capture/segment-%03d/receipt.json" % index)
        return path, json.loads(path.read_text())

    def test_frozen_inventory_is_read_only_and_never_admission(self):
        before = {str(p): forward.digest(p) for p in self.batch.rglob("*") if p.is_file()}
        result = self.collect()
        snapshot = result["snapshot"]
        after = {str(p): forward.digest(p) for p in self.batch.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(snapshot["status"], "DIAGNOSTIC_NOT_ADMITTED")
        self.assertTrue(snapshot["identity_bound"])
        self.assertEqual(snapshot["anomalies"], [])
        self.assertEqual(snapshot["reported_stop_reason"], "EXACT_BOUND_NOT_RECORDED")
        self.assertEqual(snapshot["observed_segment_directories"], 3)
        self.assertEqual(snapshot["segments"][1]["claimed_gap_from_previous_receive_ms"], 995)
        for field in ("raw_replay_performed", "backup_restored", "continuous_coverage_verified", "economic_evidence", "research_admitted"):
            self.assertFalse(snapshot[field])
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(result["snapshot_sha256"], hashlib.sha256(canonical).hexdigest())

    def test_wrong_batch_and_parent_symlink_rejected(self):
        with self.assertRaisesRegex(ValueError, "INVENTORY_BATCH_IDENTITY"):
            ecs.inventory(self.root / "different", ecs.INVENTORY_IMAGE)
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with patch.object(ecs, "ROOT", link):
            with self.assertRaises(OSError):
                ecs.inventory(link / ecs.STARTUP_REPAIR_BATCH, ecs.INVENTORY_IMAGE)

    def test_raw_symlink_is_not_followed(self):
        raw = self.batch / "capture/segment-001/receive.jsonl.gz"
        raw.unlink()
        raw.symlink_to(self.batch / "code/forward_evidence.py")
        result = self.collect()
        self.assertIsNone(result["snapshot"]["segments"][1]["raw"])
        self.assertFalse(result["snapshot"]["identity_bound"])
        self.assertTrue(result["snapshot"]["anomalies"])

    def test_wrong_source_started_identity_and_container_are_retained(self):
        (self.batch / "code/forward_evidence.py").write_text("changed")
        started = self.batch / "capture/started.json"
        value = json.loads(started.read_text())
        value["book_source_sha256"] = "0" * 64
        started.write_text(json.dumps(value))
        self.container["user"] = "0:0"
        result = self.collect()
        self.assertFalse(result["snapshot"]["identity_bound"])
        self.assertTrue({"SOURCE_HASH_MISMATCH", "STARTED_SOURCE_IDENTITY_MISMATCH", "CONTAINER_IDENTITY_MISMATCH"} <= self.codes(result))
        self.assertEqual(len(result["snapshot"]["segments"]), 3)

    def test_bad_hash_size_chain_and_boolean_index_are_reported(self):
        path, value = self.receipt()
        value.update(raw_sha256="0" * 64, raw_bytes=1, previous_receipt_sha256="0" * 64, segment_index=True)
        path.write_text(json.dumps(value))
        result = self.collect()
        self.assertTrue({"RAW_HASH_OR_SIZE_MISMATCH", "PREVIOUS_RECEIPT_HASH_MISMATCH", "RECEIPT_SCHEMA_OR_INDEX_MISMATCH"} <= self.codes(result))

    def test_missing_and_partial_segment_remain_visible(self):
        missing = self.batch / "capture/segment-001"
        missing.rename(self.root / "held-segment")
        partial = self.batch / "capture/segment-003"
        partial.mkdir()
        (partial / "receive.partial.jsonl.gz").write_bytes(b"partial")
        result = self.collect()
        self.assertEqual(result["snapshot"]["missing_segments"], [1])
        self.assertTrue({"SEGMENT_MISSING", "PARTIAL_RAW_PRESENT", "SEALED_RAW_MISSING", "SEGMENT_BEYOND_REPORTED_SEALED_COUNT"} <= self.codes(result))
        self.assertEqual(result["snapshot"]["segments"][3]["partial_raw"]["bytes"], 7)

    def test_running_or_failed_capture_does_not_become_healthy(self):
        self.container.update(running=True, exit_code=2)
        failure = {"status": "STOPPED_ERROR", "reason_code": "STREAM_IDLE_TIMEOUT", "completed_segments": 3}
        (self.batch / "capture/failure.json").write_text(json.dumps(failure))
        result = self.collect()
        self.assertTrue({"CONTAINER_NOT_TERMINAL", "CONTAINER_EXIT_OR_RESTART_ANOMALY", "CAPTURE_REPORTED_FAILURE"} <= self.codes(result))
        self.assertEqual(result["snapshot"]["reported_stop_reason"], "STREAM_IDLE_TIMEOUT")

    def test_extra_capabilities_or_changed_resource_limit_lose_identity(self):
        for key, value in (("cap_add", ["SYS_ADMIN"]), ("security_opt", []),
                           ("memory", 0), ("nano_cpus", 0), ("pids_limit", -1)):
            with self.subTest(field=key):
                original = self.container[key]
                self.container[key] = value
                result = self.collect()
                self.assertFalse(result["snapshot"]["identity_bound"])
                self.assertIn("CONTAINER_IDENTITY_MISMATCH", self.codes(result))
                self.container[key] = original

    def test_diagnostic_never_exports_arbitrary_receipt_strings(self):
        path, value = self.receipt()
        value["secret"] = "DO_NOT_EXPORT_TEST_MARKER"
        value["previous_receipt_sha256"] = "DO_NOT_EXPORT_TEST_MARKER"
        value["observation"]["messages"] = "DO_NOT_EXPORT_TEST_MARKER"
        path.write_text(json.dumps(value))
        result = self.collect()
        self.assertNotIn("DO_NOT_EXPORT_TEST_MARKER", json.dumps(result))
        self.assertIn("OBSERVATION_FIELD_INVALID_MESSAGES", self.codes(result))

    def test_container_change_during_inventory_loses_identity_binding(self):
        changed = dict(self.container, id="b" * 64)
        self.container_probe.side_effect = [self.container, changed]
        result = self.collect()
        self.assertFalse(result["snapshot"]["identity_bound"])
        self.assertIn("STATE_CHANGED_DURING_INVENTORY", self.codes(result))

    def test_single_file_read_bound_and_no_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "INVENTORY_FILE_SIZE_LIMIT"):
            ecs.inventory_file(self.batch / "capture/health.json", 2, float("inf"))
        with self.assertRaisesRegex(ValueError, "INVENTORY_PATH_OUTSIDE_BATCH"):
            ecs.inventory_directory(self.batch / "..")

    def test_out_of_range_directory_is_not_walked(self):
        outside = self.batch / "capture/segment-999"
        outside.symlink_to(self.root, target_is_directory=True)
        result = self.collect()
        self.assertEqual(result["snapshot"]["observed_segment_directories"], 3)
        self.assertTrue(any(code.startswith("UNEXPECTED_ENTRY_") for code in self.codes(result)))

    def test_image_platform_reads_only_fixed_fields(self):
        with patch.object(ecs, "command", return_value=SimpleNamespace(returncode=0, stdout=b'{"os":"linux","architecture":"amd64"}')) as invoked:
            # Exit the fixture's platform stub only for this bounded parser test.
            function = globals()["ORIGINAL_INVENTORY_IMAGE_PLATFORM"]
            self.assertEqual(function(ecs.INVENTORY_IMAGE), {"os": "linux", "architecture": "amd64"})
            rendered = " ".join(invoked.call_args.args[0])
            self.assertNotIn("Env", rendered)
            self.assertNotIn("logs", rendered)


ORIGINAL_INVENTORY_IMAGE_PLATFORM = ecs.inventory_image_platform


class ForwardEvidenceTest(unittest.TestCase):
    def test_replay_runs_as_capture_owner_without_dac_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            make_segment(data)
            spec = ecs.replay_spec("fixed-image", Path("/code"), data)
            stat = data.stat()
            self.assertEqual(spec[spec.index("--user") + 1], "%d:%d" % (stat.st_uid, stat.st_gid))
            self.assertIn("--network=none", spec)
            self.assertIn("--cap-drop=ALL", spec)
            mounts = [spec[i + 1] for i, value in enumerate(spec) if value == "--mount"]
            self.assertEqual(len(mounts), 2)
            self.assertTrue(all(m.endswith(",readonly") for m in mounts))

    def test_replay_rejects_symlink_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            make_segment(data)
            (root / "linked").symlink_to(data, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "REPLAY_DIRECTORY_IDENTITY"):
                ecs.replay_spec("fixed-image", Path("/code"), root / "linked")

    def test_replay_rejects_root_owned_evidence(self):
        with patch.object(Path, "is_dir", return_value=True), patch.object(Path, "is_symlink", return_value=False), \
             patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=0, st_gid=0)):
            with self.assertRaisesRegex(ValueError, "REPLAY_OWNER_MUST_BE_UNPRIVILEGED"):
                ecs.replay_spec("fixed-image", Path("/code"), Path("/evidence"))

    def test_code_readable_under_private_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.umask(0o077)
            try:
                directory = Path(tmp) / "code"
                ecs.readable_code_directory(directory)
                self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
            finally:
                os.umask(old)

    def test_startup_repair_rejects_other_batch(self):
        with self.assertRaisesRegex(ValueError, "REPAIR_BATCH_IDENTITY"):
            ecs.repair_startup(Path("/not-the-frozen-batch"), "unused")

    def test_startup_repair_binds_original_source(self):
        for name, expected in ecs.STARTUP_SOURCE_HASHES.items():
            self.assertEqual(forward.digest(Path(__file__).with_name(name)), expected)

    def test_repeatable_raw_replay_and_live_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = make_segment(root)
            state = forward.Replay()
            for row in rows():
                state.accept(row)
            self.assertEqual(state.summary(), receipt["observation"])
            self.assertEqual(forward.verify_segment(root), receipt)
            self.assertEqual(forward.verify_segment(root), receipt)
            self.assertFalse(receipt["economic_evidence"])

    def test_preserves_actual_wire_and_receiving_clocks(self):
        state = forward.Replay()
        records = rows()
        original = copy.deepcopy(records)
        for row in records:
            state.accept(row)
        self.assertEqual(original, records)
        self.assertEqual(state.summary()["first_receive_epoch_ms"], records[0]["receive_utc_ns"] // 1_000_000)

    def test_reject_missing_channel(self):
        state = forward.Replay()
        for row in rows()[:-1]:
            state.accept(row)
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_CHANNEL"):
            state.summary()

    def test_reject_local_sequence_gap_or_duplicate(self):
        for sequence in (1, 3):
            state = forward.Replay()
            records = rows()
            state.accept(records[0])
            records[1]["sequence"] = sequence
            with self.assertRaisesRegex(ValueError, "LOCAL_SEQUENCE"):
                state.accept(records[1])

    def test_reject_clock_reversal(self):
        for field in ("receive_utc_ns", "receive_monotonic_ns"):
            state = forward.Replay()
            records = rows()
            state.accept(records[0])
            records[1][field] = records[0][field] - 1
            with self.assertRaisesRegex(ValueError, "CLOCK_REVERSED"):
                state.accept(records[1])

    def test_reject_clock_jump(self):
        state = forward.Replay()
        records = rows()
        state.accept(records[0])
        records[1]["receive_utc_ns"] += 3_000_000_000
        with self.assertRaisesRegex(ValueError, "CLOCK_DISCONTINUITY"):
            state.accept(records[1])

    def test_reject_long_receive_gap(self):
        state = forward.Replay()
        records = rows()
        for row in records[1:]:
            row["receive_utc_ns"] += 16_000_000_000
            row["receive_monotonic_ns"] += 16_000_000_000
        for row in records:
            state.accept(row)
        with self.assertRaisesRegex(ValueError, "RECEIVE_GAP"):
            state.summary()

    def test_reject_wrong_symbol_unknown_topic_and_boolean_clock(self):
        for changed, message in (("symbol", "BOOK_SYMBOL"), ("topic", "UNKNOWN_TOPIC"), ("clock", "CLOCK_TYPE")):
            row = rows()[0]
            payload = json.loads(row["wire"])
            if changed == "symbol":
                payload["data"]["s"] = "SOLUSDT"
            elif changed == "topic":
                payload["topic"] = "private.orders"
            else:
                row["receive_utc_ns"] = True
            row["wire"] = json.dumps(payload)
            with self.assertRaisesRegex(ValueError, message):
                forward.Replay().accept(row)

    def test_reject_crossed_book_and_delta_without_snapshot(self):
        for bad in ("crossed", "delta"):
            row = rows()[0]
            payload = json.loads(row["wire"])
            if bad == "crossed":
                payload["data"]["a"] = [["98", "1"]]
            else:
                payload["type"] = "delta"
                payload["data"]["u"] = 2
            row["wire"] = json.dumps(payload)
            with self.assertRaises(ValueError):
                forward.Replay().accept(row)

    def test_raw_tamper_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_segment(root)
            with (root / "receive.jsonl.gz").open("ab") as handle:
                handle.write(b"changed")
            with self.assertRaisesRegex(ValueError, "RAW_HASH_MISMATCH"):
                forward.verify_segment(root)

    def test_receipt_tamper_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = make_segment(root)
            receipt["observation"]["messages"] += 1
            (root / "receipt.json").write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, "REPLAY_MISMATCH"):
                forward.verify_segment(root)

    def test_no_overwrite_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward.write_json(root / "receipt.json", {"a": 1})
            with self.assertRaises(FileExistsError):
                forward.write_json(root / "receipt.json", {"a": 2})
            (root / "link.json").symlink_to(root / "receipt.json")
            with self.assertRaisesRegex(ValueError, "SYMLINK_OUTPUT"):
                forward.write_json(root / "link.json", {}, replace=True)

    def test_duplicate_run_cannot_reset_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "started.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "BATCH_ALREADY_STARTED"):
                forward.run(root)

    def test_disk_failure_stops_and_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(forward.shutil, "disk_usage") as usage:
                usage.return_value.free = 0
                self.assertEqual(forward.run(root, segments=1), 2)
            self.assertEqual(json.loads((root / "failure.json").read_text())["reason_code"], "DISK_RESERVE")
            self.assertEqual(json.loads((root / "health.json").read_text())["status"], "STOPPED_ERROR")

    def test_sidecar_has_only_isolated_mounts_and_bounded_resources(self):
        spec = ecs.run_spec("ghcr.io/skw2026/research@sha256:" + "a" * 64,
                            Path("/fixed/code"), Path("/fixed/data"), 65534, 65534)
        rendered = " ".join(spec)
        for item in ("--read-only", "--cap-drop=ALL", "--restart=no", "--memory=512m", "--cpus=0.5"):
            self.assertIn(item, spec)
        for forbidden in ("docker.sock", ".env.runtime", "/app/data/models", "--privileged", "--env-file"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(spec.count("--mount"), 2)

    def test_parent_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "link").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "OUTPUT_PARENT_SYMLINK"):
                ecs.create_directory(root / "link" / "new")

    def test_workflow_is_manual_only_and_does_not_deploy_trade(self):
        source = (Path(__file__).resolve().parents[1] / ".github/workflows/forward-evidence.yml").read_text()
        self.assertIn("workflow_dispatch:", source)
        self.assertNotIn("  push:", source)
        self.assertNotIn("  schedule:", source)
        self.assertNotIn("docker compose", source)
        self.assertIn("persist-credentials: false", source)
        self.assertIn("deploy/ecs_ssh.py", source)
        self.assertNotIn(".env.runtime", source)


if __name__ == "__main__":
    unittest.main()
