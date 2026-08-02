#!/usr/bin/env python3

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "prune_release_storage.py"


class PruneReleaseStorageTest(unittest.TestCase):
    def run_pruner(
        self,
        release_root: pathlib.Path,
        target_release: pathlib.Path,
        current_link: pathlib.Path,
        previous_release: pathlib.Path | None = None,
        active_release_id: str = "active-run",
        keep_releases: int = 3,
        keep_runtime_compose: int = 2,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--release-root",
                str(release_root),
                "--target-release",
                str(target_release),
                "--current-link",
                str(current_link),
                "--previous-release",
                str(previous_release or target_release),
                "--active-release-id",
                active_release_id,
                "--keep-releases",
                str(keep_releases),
                "--keep-runtime-compose",
                str(keep_runtime_compose),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_prunes_stale_artifacts_and_preserves_transaction_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            release_root = pathlib.Path(tmp) / "ai-trade"
            releases_root = release_root / "releases"
            incoming_root = release_root / "incoming"
            unpack_root = release_root / ".release-unpack"
            runtime_root = release_root / "data" / "deploy-runtime-compose"
            for directory in (releases_root, incoming_root, unpack_root, runtime_root):
                directory.mkdir(parents=True, exist_ok=True)

            current_release = releases_root / "current-sha"
            target_release = releases_root / "target-sha"
            retained_rollback = releases_root / "rollback-new"
            stale_release = releases_root / "stale-old"
            for index, release in enumerate(
                (stale_release, retained_rollback, current_release, target_release),
                start=1,
            ):
                release.mkdir()
                content = release / "release_manifest.json"
                content.write_text("{}\n", encoding="utf-8")
                os.utime(release, (index, index))

            current_link = release_root / "current"
            current_link.symlink_to(current_release)

            active_incoming = incoming_root / "active-run"
            stale_incoming = incoming_root / "failed-run"
            active_unpack = unpack_root / "active-run"
            stale_unpack = unpack_root / "interrupted-run"
            for directory in (
                active_incoming,
                stale_incoming,
                active_unpack,
                stale_unpack,
            ):
                directory.mkdir()
                (directory / "payload").write_text("x", encoding="utf-8")

            for index in range(5):
                compose = runtime_root / f"runtime-{index}.yml"
                compose.write_text("services: {}\n", encoding="utf-8")
                os.utime(compose, (index + 1, index + 1))

            for release in releases_root.iterdir():
                if release.is_dir():
                    for child in release.iterdir():
                        child.chmod(0o444)
                    release.chmod(0o555)

            result = self.run_pruner(
                release_root,
                target_release,
                current_link,
                previous_release=current_release,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertTrue(current_release.is_dir())
            self.assertTrue(target_release.is_dir())
            self.assertTrue(retained_rollback.is_dir())
            self.assertFalse(stale_release.exists())
            self.assertTrue(current_link.is_symlink())
            self.assertTrue(active_incoming.is_dir())
            self.assertFalse(stale_incoming.exists())
            self.assertTrue(active_unpack.is_dir())
            self.assertFalse(stale_unpack.exists())
            self.assertEqual(
                sorted(path.name for path in runtime_root.iterdir()),
                ["runtime-3.yml", "runtime-4.yml"],
            )

    def test_rejects_target_outside_release_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            release_root = pathlib.Path(tmp) / "ai-trade"
            (release_root / "releases").mkdir(parents=True)
            outside = pathlib.Path(tmp) / "outside"
            outside.mkdir()

            result = self.run_pruner(
                release_root,
                outside,
                release_root / "current",
                previous_release=outside,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outside immutable release root", result.stderr)

    def test_rejects_unsafe_active_release_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            release_root = pathlib.Path(tmp) / "ai-trade"
            target = release_root / "releases" / "target"
            target.mkdir(parents=True)

            result = self.run_pruner(
                release_root,
                target,
                release_root / "current",
                previous_release=target,
                active_release_id="../outside",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe characters", result.stderr)

    def test_preserves_legacy_previous_release_without_current_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            release_root = pathlib.Path(tmp) / "ai-trade"
            releases_root = release_root / "releases"
            target = releases_root / "target"
            previous = releases_root / "legacy-rollback"
            unrelated = releases_root / "unrelated"
            for release in (target, previous, unrelated):
                release.mkdir(parents=True)

            result = self.run_pruner(
                release_root,
                target,
                release_root / "current",
                previous_release=previous,
                keep_releases=2,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertTrue(target.is_dir())
            self.assertTrue(previous.is_dir())
            self.assertFalse(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
