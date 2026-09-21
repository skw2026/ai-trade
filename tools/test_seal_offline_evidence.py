#!/usr/bin/env python3
"""Synthetic archive integrity checks; never reads the project history."""
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import seal_offline_evidence as seal


class EvidenceSealTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root/"fixture.tar.gz"

    def archive(self, name="evidence.txt", contents=b"synthetic evidence", duplicate=False,
                symlink=False, wrong_record=False):
        record = {"sha256":hashlib.sha256(contents).hexdigest(), "size":len(contents)}
        if wrong_record:
            record["sha256"] = "0"*64
        metadata = json.dumps({"schema_version":"closed_mvp_evidence_archive_v1",
                               "files":{name:record}}).encode()
        with tarfile.open(self.path,"w:gz") as archive:
            info = tarfile.TarInfo(seal.META)
            info.size = len(metadata)
            archive.addfile(info,io.BytesIO(metadata))
            for _ in range(2 if duplicate else 1):
                info = tarfile.TarInfo(name)
                if symlink:
                    info.type = tarfile.SYMTYPE
                    info.linkname = "outside"
                    archive.addfile(info)
                else:
                    info.size = len(contents)
                    archive.addfile(info,io.BytesIO(contents))
        return seal.sha(self.path)

    def test_complete_archive_verified_without_extraction(self):
        result = seal.verify(self.path,self.archive())
        self.assertTrue(result["verified"])
        self.assertFalse(result["extracted_or_executed"])
        self.assertEqual(result["file_count"],1)
        self.assertEqual(list(self.root.iterdir()),[self.path])

    def test_external_archive_digest_required(self):
        self.archive()
        with self.assertRaisesRegex(ValueError,"ARCHIVE_SHA256"):
            seal.verify(self.path,"0"*64)

    def test_modified_member_rejected(self):
        digest = self.archive(wrong_record=True)
        with self.assertRaisesRegex(ValueError,"MEMBER_SHA256"):
            seal.verify(self.path,digest)

    def test_duplicate_member_rejected(self):
        digest = self.archive(duplicate=True)
        with self.assertRaisesRegex(ValueError,"DUPLICATE_MEMBER"):
            seal.verify(self.path,digest)

    def test_symlink_rejected(self):
        digest = self.archive(symlink=True)
        with self.assertRaisesRegex(ValueError,"NON_REGULAR_MEMBER"):
            seal.verify(self.path,digest)

    def test_path_escape_rejected(self):
        digest = self.archive(name="../outside")
        with self.assertRaisesRegex(ValueError,"UNSAFE_NAME"):
            seal.verify(self.path,digest)

    def test_pack_is_exclusive_and_source_is_unchanged(self):
        source = self.root/"tools/seal_offline_evidence.py"
        source.parent.mkdir()
        source.write_text("synthetic source, never executed\n")
        before = seal.sha(source)
        with mock.patch.object(seal,"ROOT",self.root), \
             mock.patch.object(seal,"STAGE",self.root/"output"), \
             mock.patch.object(seal,"historical_identity",return_value={}):
            result = seal.pack()
            self.assertEqual(result["file_count"],1)
            self.assertEqual(seal.sha(source),before)
            with self.assertRaisesRegex(ValueError,"ARCHIVE_ALREADY_EXISTS"):
                seal.pack()


if __name__ == "__main__":
    unittest.main()
