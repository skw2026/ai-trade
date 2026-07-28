#!/usr/bin/env python3

import importlib.util
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "materialize_release_compose.py"
SPEC = importlib.util.spec_from_file_location("materialize_release_compose", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class MaterializeReleaseComposeTest(unittest.TestCase):
    def test_materializes_safe_mount_topology_and_is_idempotent(self):
        source = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
        materialized = MODULE.materialize(source)

        self.assertEqual(materialized.count(MODULE.PROJECT_MOUNT_RO), 2)
        self.assertEqual(materialized.count(MODULE.DATA_MOUNT), 3)
        self.assertEqual(materialized.count(MODULE.ENV_MOUNT), 2)
        self.assertNotIn(MODULE.ENV_MOUNT_OLD, materialized)
        self.assertIn(MODULE.SCHEDULER_ENV_RELEASE, materialized)
        self.assertEqual(MODULE.materialize(materialized), materialized)

    def test_cli_creates_readonly_parent_child_mountpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = root / "source.yml"
            output = root / "runtime" / "compose.yml"
            release = root / "release"
            release.mkdir()
            source.write_text(
                (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            result = MODULE.main
            old_parse_args = MODULE.parse_args
            try:
                MODULE.parse_args = lambda: type(
                    "Args",
                    (),
                    {
                        "input": str(source),
                        "output": str(output),
                        "release_dir": str(release),
                    },
                )()
                self.assertEqual(result(), 0)
            finally:
                MODULE.parse_args = old_parse_args

            self.assertTrue((release / "data").is_dir())
            self.assertIn(MODULE.ENV_MOUNT, output.read_text(encoding="utf-8"))

    def test_upgrades_legacy_nested_env_mount_for_rollback(self):
        source = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
        legacy = MODULE.materialize(source)
        legacy = legacy.replace(MODULE.ENV_MOUNT, MODULE.ENV_MOUNT_OLD)
        legacy = legacy.replace(
            MODULE.SCHEDULER_ENV_RELEASE,
            MODULE.SCHEDULER_ENV_SOURCE,
        )

        upgraded = MODULE.materialize(legacy)

        self.assertNotIn(MODULE.ENV_MOUNT_OLD, upgraded)
        self.assertEqual(upgraded.count(MODULE.ENV_MOUNT), 2)
        self.assertIn(MODULE.SCHEDULER_ENV_RELEASE, upgraded)

    def test_rejects_partial_or_ambiguous_project_mount_contract(self):
        source = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
        broken = source.replace(MODULE.PROJECT_MOUNT, MODULE.PROJECT_MOUNT_RO, 1)
        with self.assertRaisesRegex(ValueError, "project mount contract"):
            MODULE.materialize(broken)


if __name__ == "__main__":
    unittest.main()
