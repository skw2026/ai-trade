#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "write_deployment_diagnostics.py"
SPEC = importlib.util.spec_from_file_location("deployment_diagnostics", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
deployment_diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment_diagnostics)


class DeploymentDiagnosticsTest(unittest.TestCase):
    def _args(self, output: pathlib.Path, phase: str):
        return deployment_diagnostics.parse_args(
            [
                "--output",
                str(output),
                "--run-id",
                "deploy-123-1",
                "--phase",
                phase,
                "--status",
                "FAIL",
                "--reason",
                "readiness_timeout",
                "--release-id",
                "123-1-deadbeef",
                "--git-sha",
                "deadbeef",
                "--target-release",
                "/opt/ai-trade/releases/deadbeef",
                "--current-link",
                str(output.parent / "current"),
                "--previous-release",
                "/opt/ai-trade/releases/previous",
                "--compose-project",
                "ai-trade",
                "--container",
                "ai-trade",
                "--container",
                "microstructure-demo-policy",
            ]
        )

    @mock.patch.object(deployment_diagnostics.subprocess, "run")
    def test_writes_container_state_and_appends_events(self, run_mock):
        running = subprocess.CompletedProcess(
            args=["docker", "inspect", "ai-trade"],
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "State": {
                            "Status": "running",
                            "Running": True,
                            "ExitCode": 0,
                            "OOMKilled": False,
                            "Health": {"Status": "healthy"},
                        },
                        "RestartCount": 2,
                        "Config": {
                            "Image": "ghcr.io/example/runtime@sha256:abc",
                            "Env": ["SECRET=must-not-be-recorded"],
                        },
                        "Image": "sha256:runtime",
                    }
                ]
            ),
            stderr="",
        )
        missing = subprocess.CompletedProcess(
            args=["docker", "inspect", "microstructure-demo-policy"],
            returncode=1,
            stdout="",
            stderr="not found",
        )
        run_mock.side_effect = [running, missing, running, missing]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / "diagnostics.json"
            deployment_diagnostics.write_diagnostics(
                self._args(output, "initial_service_readiness")
            )
            deployment_diagnostics.write_diagnostics(
                self._args(output, "rollback_readiness")
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            payload["schema_version"],
            "ai_trade_deployment_diagnostics_v1",
        )
        self.assertEqual(payload["phase"], "rollback_readiness")
        self.assertEqual(len(payload["events"]), 2)
        containers = payload["events"][0]["containers"]
        self.assertEqual(containers[0]["health_status"], "healthy")
        self.assertEqual(containers[0]["restart_count"], 2)
        self.assertFalse(containers[1]["exists"])
        self.assertNotIn("Env", json.dumps(payload))
        self.assertNotIn("must-not-be-recorded", json.dumps(payload))

    @mock.patch.object(
        deployment_diagnostics.subprocess,
        "run",
        side_effect=FileNotFoundError("docker"),
    )
    def test_docker_unavailable_is_recorded_without_failing(self, _run_mock):
        result = deployment_diagnostics.inspect_container("ai-trade")
        self.assertEqual(result["inspect_result"], "docker_unavailable")
        self.assertFalse(result["exists"])


if __name__ == "__main__":
    unittest.main()
