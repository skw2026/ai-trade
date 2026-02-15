#!/usr/bin/env python3

import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "docker_gc.sh"


class DockerGCTest(unittest.TestCase):
    def _write_fake_docker(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        log_path = root / "docker.calls.log"
        fake_docker = root / "fake-docker.sh"
        fake_docker.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
echo "$@" >> "${FAKE_DOCKER_LOG}"
exit 0
""",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        return fake_docker, log_path

    def test_gc_calls_expected_prune_commands(self):
        with tempfile.TemporaryDirectory() as td:
            temp = pathlib.Path(td)
            fake_docker, log_path = self._write_fake_docker(temp)

            env = os.environ.copy()
            env.update(
                {
                    "DOCKER_GC_DOCKER_BIN": str(fake_docker),
                    "FAKE_DOCKER_LOG": str(log_path),
                    "DOCKER_GC_ENABLED": "true",
                    "DOCKER_GC_DRY_RUN": "false",
                    "DOCKER_GC_UNTIL": "120h",
                    "DOCKER_GC_PRUNE_NETWORKS": "true",
                    "DOCKER_GC_PRUNE_VOLUMES": "false",
                }
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            calls = log_path.read_text(encoding="utf-8")
            self.assertIn("info", calls)
            self.assertIn("container prune -f --filter until=120h", calls)
            self.assertIn("image prune -a -f --filter until=120h", calls)
            self.assertIn("builder prune -a -f --filter until=120h", calls)
            self.assertIn("network prune -f --filter until=120h", calls)
            self.assertNotIn("volume prune -f", calls)

    def test_gc_disabled_skips_docker_calls(self):
        with tempfile.TemporaryDirectory() as td:
            temp = pathlib.Path(td)
            fake_docker, log_path = self._write_fake_docker(temp)

            env = os.environ.copy()
            env.update(
                {
                    "DOCKER_GC_DOCKER_BIN": str(fake_docker),
                    "FAKE_DOCKER_LOG": str(log_path),
                    "DOCKER_GC_ENABLED": "false",
                }
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertIn("skipped", result.stdout)
            self.assertFalse(log_path.exists())


if __name__ == "__main__":
    unittest.main()
