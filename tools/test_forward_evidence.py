#!/usr/bin/env python3
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
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
