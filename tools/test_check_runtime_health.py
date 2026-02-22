#!/usr/bin/env python3

import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_runtime_health.sh"


class CheckRuntimeHealthScriptTest(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue(SCRIPT.is_file())

    def test_script_shell_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_help_works_without_docker(self):
        result = subprocess.run(
            [str(SCRIPT), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertIn("--compose-file", result.stdout)
        self.assertIn("--log-since", result.stdout)


if __name__ == "__main__":
    unittest.main()
