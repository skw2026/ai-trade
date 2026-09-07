#!/usr/bin/env python3

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "deploy" / "release_integrity.py"


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_release(root: pathlib.Path) -> None:
    (root / "deploy").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "tools").mkdir()
    (root / "docker-compose.prod.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "deploy" / "ecs-deploy.sh").write_text(
        "#!/usr/bin/env bash\n", encoding="utf-8"
    )
    (root / "tools" / "runner.py").write_text("print('ok')\n", encoding="utf-8")
    files = [
        root / "deploy" / "ecs-deploy.sh",
        root / "docker-compose.prod.yml",
        root / "tools" / "runner.py",
    ]
    content = "".join(
        f"{sha256(path)}  {path.relative_to(root).as_posix()}\n"
        for path in sorted(files)
    )
    content_path = root / ".release-content.sha256"
    content_path.write_text(content, encoding="utf-8")
    manifest = {
        "schema_version": "ai_trade_release_manifest_v1",
        "content_manifest": {
            "path": ".release-content.sha256",
            "sha256": sha256(content_path),
        },
    }
    (root / "release_manifest.json").write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )


class ReleaseIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name) / "release"
        self.root.mkdir()
        create_release(self.root)
        self.quarantine = pathlib.Path(self.temp_dir.name) / "quarantine"

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_validator(self, repair: bool = False, summary: pathlib.Path | None = None):
        command = [
            sys.executable,
            str(VALIDATOR),
            "--release-dir",
            str(self.root),
        ]
        if repair:
            command.extend(
                [
                    "--repair-runtime-contamination",
                    "--quarantine-root",
                    str(self.quarantine),
                ]
            )
        if summary is not None:
            command.extend(["--summary-output", str(summary)])
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def test_valid_release_passes(self):
        result = self.run_validator()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RELEASE_TREE_INTEGRITY_OK", result.stdout)
        self.assertIn("files=3", result.stdout)

    def test_python_bytecode_is_quarantined(self):
        cache_file = self.root / "tools" / "__pycache__" / "runner.cpython-312.pyc"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"runtime cache")

        result = self.run_validator(repair=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(cache_file.exists())
        quarantined = list(
            self.quarantine.glob(
                "*/tools/__pycache__/runner.cpython-312.pyc"
            )
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b"runtime cache")

    def test_data_contamination_is_quarantined_but_mountpoint_remains(self):
        runtime_file = self.root / "data" / "reports" / "run.json"
        runtime_file.parent.mkdir(parents=True)
        runtime_file.write_text("{}\n", encoding="utf-8")

        result = self.run_validator(repair=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "data").is_dir())
        self.assertFalse(runtime_file.exists())
        self.assertEqual(
            len(list(self.quarantine.glob("*/data/reports/run.json"))),
            1,
        )

    def test_summary_exposes_only_categories_and_counts(self):
        runtime_file = self.root / "data" / "reports" / "private-run.json"
        runtime_file.parent.mkdir(parents=True)
        runtime_file.write_text("{}\n", encoding="utf-8")
        summary = pathlib.Path(self.temp_dir.name) / "summary.json"

        result = self.run_validator(repair=True, summary=summary)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(summary.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["schema_version"],
            "ai_trade_release_integrity_summary_v1",
        )
        self.assertTrue(payload["valid"])
        self.assertTrue(payload["runtime_contamination_repaired"])
        self.assertEqual(payload["initial"]["unexpected_count"], 1)
        self.assertEqual(payload["final"]["unexpected_count"], 0)
        self.assertNotIn("private-run", json.dumps(payload))

    def test_summary_cannot_contaminate_immutable_release(self):
        summary = self.root / "data" / "reports" / "summary.json"

        result = self.run_validator(summary=summary)

        self.assertEqual(result.returncode, 2)
        self.assertIn("summary output must be outside release", result.stderr)
        self.assertFalse(summary.exists())

    def test_unknown_unexpected_file_is_rejected(self):
        unknown = self.root / "tools" / "injected.txt"
        unknown.write_text("unexpected\n", encoding="utf-8")

        result = self.run_validator(repair=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unexpected=["tools/injected.txt"]', result.stderr)
        self.assertTrue(unknown.exists())
        self.assertFalse(self.quarantine.exists())

    def test_modified_signed_file_is_rejected_without_repair(self):
        signed = self.root / "tools" / "runner.py"
        signed.write_text("print('changed')\n", encoding="utf-8")
        cache_file = self.root / "tools" / "__pycache__" / "runner.pyc"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"runtime cache")

        result = self.run_validator(repair=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('hash_mismatch=["tools/runner.py"]', result.stderr)
        self.assertTrue(cache_file.exists())

    def test_missing_signed_file_is_rejected(self):
        os.unlink(self.root / "tools" / "runner.py")

        result = self.run_validator(repair=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing=["tools/runner.py"]', result.stderr)

    def test_symlink_is_rejected(self):
        link = self.root / "tools" / "runner-link.py"
        link.symlink_to("runner.py")

        result = self.run_validator(repair=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symlinks=["tools/runner-link.py"]', result.stderr)
        self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
