#!/usr/bin/env python3
"""Synthetic private-backup checks; never use real evidence, accounts or keys."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import forward_evidence_backup as backup


class ForwardBackupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="synthetic-forward-backup-")
        cls.root = Path(cls.temporary.name).resolve()
        cls.keys = []
        for index in range(2):
            key, cert = cls.root / (str(index) + ".key"), cls.root / (str(index) + ".pem")
            result = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                                     "-keyout", str(key), "-out", str(cert), "-days", "1",
                                     "-subj", "/CN=synthetic-test-only"], capture_output=True, timeout=60)
            if result.returncode:
                raise RuntimeError("SYNTHETIC_CERTIFICATE_GENERATION_FAILED")
            key.chmod(0o600)
            cls.keys.append((key, cert))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.case.cleanup)
        self.case_root = Path(self.case.name)
        self.batch = self.case_root / "batch"
        self.original = {}
        for name in backup.FILE_LIMITS:
            path = self.batch / name
            path.parent.mkdir(parents=True, exist_ok=True)
            value = ("synthetic-not-executable:" + name).encode()
            path.write_bytes(value)
            self.original[name] = value
        self.expected = {name: hashlib.sha256(value).hexdigest() for name, value in self.original.items()}
        self.destination = self.case_root / "restored"

    def encrypted(self, plain=None):
        return backup.encrypt(backup.pack(self.batch) if plain is None else plain, self.keys[0][1].read_bytes())

    def restore(self, cipher, key_index=0):
        key, certificate = self.keys[key_index]
        return backup.restore(cipher, key, certificate, self.destination, self.expected)

    def custom_tar(self, change):
        original = backup.pack(self.batch)
        with tarfile.open(fileobj=io.BytesIO(original), mode="r:") as archive:
            members = [(item, archive.extractfile(item).read()) for item in archive]
        members = change(members)
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for item, value in members:
                archive.addfile(item, io.BytesIO(value))
        return output.getvalue()

    def test_roundtrip_exact_files_hashes_and_no_replay(self):
        report = self.restore(self.encrypted())
        self.assertEqual(report["status"], "RESTORED_HASH_BOUND_NOT_REPLAYED")
        for name, value in self.original.items():
            path = self.destination / name
            self.assertEqual(path.read_bytes(), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual({str(p.relative_to(self.destination)) for p in self.destination.rglob("*") if p.is_file()},
                         set(self.original) | {backup.MANIFEST})
        self.assertFalse(report["raw_replay_performed"])
        self.assertFalse(report["economic_evidence"])
        self.assertFalse(report["research_admitted"])

    def test_pack_is_deterministic_and_never_executes_source(self):
        self.assertEqual(backup.pack(self.batch), backup.pack(self.batch))
        namespace = {"__name__": "forward_backup"}
        exec(compile(Path(backup.__file__).read_text(), "forward_backup", "exec"), namespace)
        self.assertEqual(namespace["pack"](self.batch), backup.pack(self.batch))

    def test_pack_missing_symlink_parent_link_hardlink_and_oversize_rejected(self):
        name = "capture/started.json"
        path = self.batch / name
        path.unlink()
        with self.assertRaises(backup.BackupError):
            backup.pack(self.batch)
        elsewhere = self.case_root / "elsewhere"
        elsewhere.write_bytes(b"private")
        path.symlink_to(elsewhere)
        with self.assertRaises(backup.BackupError):
            backup.pack(self.batch)
        path.unlink()
        os.link(elsewhere, path)
        with self.assertRaisesRegex(backup.BackupError, "NOT_SINGLE_REGULAR_FILE"):
            backup.pack(self.batch)
        path.unlink()
        path.write_bytes(b"x" * (backup.FILE_LIMITS[name] + 1))
        with self.assertRaisesRegex(backup.BackupError, "SIZE_LIMIT"):
            backup.pack(self.batch)
        alias = self.case_root / "alias"
        alias.symlink_to(self.batch, target_is_directory=True)
        with self.assertRaises(backup.BackupError):
            backup.pack(alias)

    def test_wrong_key_and_damaged_cipher_create_no_destination(self):
        cipher = self.encrypted()
        for value, index in ((cipher, 1), (cipher[:20], 0), (cipher[:-1] + bytes([cipher[-1] ^ 1]), 0)):
            with self.subTest(index=index, size=len(value)):
                with self.assertRaises(backup.BackupError):
                    self.restore(value, index)
                self.assertFalse(self.destination.exists())

    def test_missing_duplicate_traversal_absolute_symlink_and_hardlink_members_rejected(self):
        def altered(kind):
            def change(members):
                if kind == "missing":
                    return members[1:]
                if kind == "duplicate":
                    return members + members[:1]
                item, data = members[0]
                if kind == "traversal":
                    item.name = "../outside"
                elif kind == "absolute":
                    item.name = "/tmp/outside"
                else:
                    item.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                    item.linkname, item.size, data = "../outside", 0, b""
                members[0] = (item, data)
                return members
            return change
        for kind in ("missing", "duplicate", "traversal", "absolute", "symlink", "hardlink"):
            with self.subTest(kind=kind):
                with self.assertRaises(backup.BackupError):
                    self.restore(self.encrypted(self.custom_tar(altered(kind))))
                self.assertFalse(self.destination.exists())

    def test_plaintext_and_manifest_tamper_external_hash_binding(self):
        files = dict(self.original)
        name = next(iter(files))
        files[name] += b"tampered"
        files[backup.MANIFEST] = backup.canonical({"schema_version": backup.SCHEMA,
            "files": {n: {"sha256": backup.sha(v), "bytes": len(v)} for n, v in files.items()}})
        with self.assertRaisesRegex(backup.BackupError, "ORIGINAL_HASH_MISMATCH"):
            self.restore(self.encrypted(backup.archive_bytes(files)))
        self.assertFalse(self.destination.exists())

    def test_trailing_archive_or_bytes_rejected(self):
        plain = backup.pack(self.batch)
        with self.assertRaisesRegex(backup.BackupError, "NONCANONICAL"):
            self.restore(self.encrypted(plain + b"not-an-original"))

    def test_archive_member_size_checked_before_extract(self):
        cipher = self.encrypted()
        limits = dict(backup.FILE_LIMITS)
        limits["capture/started.json"] = 1
        with patch.object(backup, "FILE_LIMITS", limits):
            with self.assertRaisesRegex(backup.BackupError, "ARCHIVE_MEMBER_SIZE_LIMIT"):
                self.restore(cipher)
        self.assertFalse(self.destination.exists())

    def test_non_rsa_certificate_rejected(self):
        key, certificate = self.case_root / "ec.key", self.case_root / "ec.pem"
        result = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                                 "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key),
                                 "-out", str(certificate), "-days", "1", "-subj", "/CN=synthetic-only"],
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, "SYNTHETIC_EC_GENERATION_FAILED")
        with self.assertRaises(backup.BackupError):
            backup.encrypt(b"synthetic", certificate.read_bytes())

    def test_existing_destination_and_symlink_parent_rejected(self):
        cipher = self.encrypted()
        self.destination.mkdir()
        (self.destination / "user-file").write_bytes(b"keep")
        with self.assertRaises(backup.BackupError):
            self.restore(cipher)
        self.assertEqual((self.destination / "user-file").read_bytes(), b"keep")
        alias = self.case_root / "parent-link"
        alias.symlink_to(self.case_root, target_is_directory=True)
        self.destination = alias / "new"
        with self.assertRaises(backup.BackupError):
            self.restore(cipher)
        self.assertFalse((self.case_root / "new").exists())

    def test_key_permissions_and_secret_in_certificate_rejected(self):
        key = self.case_root / "public-readable.key"
        key.write_bytes(self.keys[0][0].read_bytes())
        key.chmod(0o644)
        with self.assertRaisesRegex(backup.BackupError, "PRIVATE_KEY_PERMISSIONS"):
            backup.restore(self.encrypted(), key, self.keys[0][1], self.destination, self.expected)
        with self.assertRaisesRegex(backup.BackupError, "CERTIFICATE_INVALID"):
            backup.encrypt(b"synthetic", self.keys[0][0].read_bytes() + self.keys[0][1].read_bytes())

    def test_expected_hashes_exact_and_no_error_plaintext_leak(self):
        self.expected["unexpected"] = "a" * 64
        with self.assertRaises(backup.BackupError) as error:
            self.restore(self.encrypted())
        self.assertRegex(str(error.exception), r"^BACKUP_[A-Z_]+$")
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
