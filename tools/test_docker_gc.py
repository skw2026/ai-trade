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

    def _write_fake_docker_with_retention(
        self, root: pathlib.Path
    ) -> tuple[pathlib.Path, pathlib.Path]:
        log_path = root / "docker.calls.log"
        fake_docker = root / "fake-docker-retention.sh"
        fake_docker.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
echo "$@" >> "${FAKE_DOCKER_LOG}"
cmd1="${1:-}"
cmd2="${2:-}"
cmd3="${3:-}"
cmd4="${4:-}"

if [[ "${cmd1}" == "info" ]]; then
  exit 0
fi
if [[ "${cmd1}" == "system" && "${cmd2}" == "df" ]]; then
  exit 0
fi
if [[ "${cmd1}" == "container" && "${cmd2}" == "prune" ]]; then
  exit 0
fi
if [[ "${cmd1}" == "image" && "${cmd2}" == "prune" ]]; then
  exit 0
fi
if [[ "${cmd1}" == "builder" && "${cmd2}" == "prune" ]]; then
  exit 0
fi
if [[ "${cmd1}" == "network" && "${cmd2}" == "prune" ]]; then
  exit 0
fi
if [[ "${cmd1}" == "volume" && "${cmd2}" == "prune" ]]; then
  exit 0
fi
if [[ "${cmd1}" == "ps" && "${cmd2}" == "--format" ]]; then
  echo "ghcr.io/skw2026/ai-trade:live"
  exit 0
fi
if [[ "${cmd1}" == "image" && "${cmd2}" == "ls" && "${cmd3}" == "--format" ]]; then
  echo "ghcr.io/skw2026/ai-trade"
  echo "ghcr.io/skw2026/ai-trade-research"
  echo "docker"
  exit 0
fi
if [[ "${cmd1}" == "image" && "${cmd2}" == "ls" && "${cmd4}" == "--format" ]]; then
  repo="${cmd3}"
  case "${repo}" in
    ghcr.io/skw2026/ai-trade)
      echo "ghcr.io/skw2026/ai-trade:live"
      echo "ghcr.io/skw2026/ai-trade:new"
      echo "ghcr.io/skw2026/ai-trade:old"
      ;;
    ghcr.io/skw2026/ai-trade-research)
      echo "ghcr.io/skw2026/ai-trade-research:r2"
      echo "ghcr.io/skw2026/ai-trade-research:r1"
      ;;
  esac
  exit 0
fi
if [[ "${cmd1}" == "image" && "${cmd2}" == "inspect" ]]; then
  ref="${@: -1}"
  case "${ref}" in
    ghcr.io/skw2026/ai-trade:live)
      echo "2026-02-20T03:00:00Z"
      ;;
    ghcr.io/skw2026/ai-trade:new)
      echo "2026-02-20T02:00:00Z"
      ;;
    ghcr.io/skw2026/ai-trade:old)
      echo "2026-02-19T02:00:00Z"
      ;;
    ghcr.io/skw2026/ai-trade-research:r2)
      echo "2026-02-20T01:00:00Z"
      ;;
    ghcr.io/skw2026/ai-trade-research:r1)
      echo "2026-02-19T01:00:00Z"
      ;;
  esac
  exit 0
fi
if [[ "${cmd1}" == "image" && "${cmd2}" == "rm" ]]; then
  exit 0
fi
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

    def test_gc_keep_recent_tags_prunes_old_tags(self):
        with tempfile.TemporaryDirectory() as td:
            temp = pathlib.Path(td)
            fake_docker, log_path = self._write_fake_docker_with_retention(temp)

            env = os.environ.copy()
            env.update(
                {
                    "DOCKER_GC_DOCKER_BIN": str(fake_docker),
                    "FAKE_DOCKER_LOG": str(log_path),
                    "DOCKER_GC_ENABLED": "true",
                    "DOCKER_GC_DRY_RUN": "false",
                    "DOCKER_GC_UNTIL": "120h",
                    "DOCKER_GC_KEEP_RECENT_TAGS": "2",
                    "DOCKER_GC_KEEP_REPO_MATCHERS": "ai-trade,ai-trade-research",
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
            self.assertIn("image rm ghcr.io/skw2026/ai-trade:old", calls)
            self.assertNotIn("image rm ghcr.io/skw2026/ai-trade:new", calls)
            self.assertNotIn("image rm ghcr.io/skw2026/ai-trade:live", calls)


if __name__ == "__main__":
    unittest.main()
