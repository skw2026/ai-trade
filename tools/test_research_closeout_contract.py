#!/usr/bin/env python3
"""Regression contract for the two retired research schedules (no YAML dependency).

This deliberately checks the existing block-style workflow syntax. It is not a
GitHub expression interpreter or evidence that a remote workflow was published.
"""

import ast
import json
import os
import pathlib
import re
import subprocess
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def triggers(source):
    match = re.search(r"(?m)^on:\s*\n(?P<body>(?:(?:[ \t].*|)\n)*)", source)
    if match is None:
        raise ValueError("Expected explicit block-style workflow triggers")
    return set(re.findall(r"(?m)^  ([a-z_]+):", match["body"]))


class ResearchCloseoutContractTest(unittest.TestCase):
    def test_research_is_explicit_only(self):
        source = read(".github/workflows/closed-loop.yml")
        self.assertEqual(triggers(source), {"push", "workflow_dispatch"})
        self.assertNotRegex(source, r"(?m)^\s*(?:-\s*)?cron:")
        self.assertIn('      - "research/**"', source)
        self.assertIn("if: ${{ github.event_name == 'workflow_dispatch' || github.event_name == 'push' }}", source)

    def test_closed_candidate_keeps_manual_and_successful_cd_verification(self):
        source = read(".github/workflows/option-lifecycle-v4.yml")
        self.assertEqual(triggers(source), {"workflow_dispatch", "workflow_run"})
        self.assertNotRegex(source, r"(?m)^\s*(?:-\s*)?cron:")
        self.assertIn("      - CD", source)
        self.assertIn("      - completed", source)
        self.assertIn("(github.event_name == 'workflow_run' && github.event.workflow_run.conclusion == 'success')", source)
        self.assertNotIn("github.event_name != 'workflow_run'", source)
        self.assertIn("audit_option_candidate_closure.py", source)

    def test_trigger_check_detects_reintroduced_schedule(self):
        source = read(".github/workflows/closed-loop.yml")
        mutated = source.replace("on:\n", 'on:\n  schedule:\n    - cron: "0 * * * *"\n', 1)
        self.assertEqual(triggers(mutated), {"push", "workflow_dispatch", "schedule"})
        with self.assertRaises(ValueError):
            triggers("on: [push, schedule]\n")

    def test_manual_entry_is_not_a_promotion_bypass(self):
        registry = json.loads(read("config/option_candidate_closure_v1.json"))
        self.assertEqual(registry["status"], "CLOSED")
        self.assertEqual(registry["closure_anchor"]["run_id"], 34685429648)
        for key in ("reopening_allowed", "new_identity_alone_permits_reopening",
                    "promotion_authority", "demo_activation_authorized", "live_activation_authorized"):
            self.assertIs(registry[key], False, key)

    def test_safety_and_capture_services_still_exist(self):
        compose = read("docker-compose.prod.yml")
        for service in ("ai-trade", "watchdog", "scheduler", "market-alpha-collector",
                        "cross-venue-alpha-collector", "option-vrp-collector"):
            self.assertRegex(compose, rf"(?m)^  {re.escape(service)}:$")
        self.assertIn("SCHEDULER_ACTION: ${SCHEDULER_ACTION:-assess}", compose)
        self.assertIn("AI_TRADE_WATCH_SCHEDULER: ${AI_TRADE_WATCH_SCHEDULER:-true}", compose)
        self.assertIn("tools/scheduler_healthcheck.sh", compose)
        smoke = read(".github/workflows/smoke.yml")
        self.assertEqual(triggers(smoke), {"workflow_dispatch", "workflow_run"})
        self.assertIn("      - CD", smoke)

    def test_historical_limits_are_not_relaxed_for_closeout(self):
        source = read("tools/audit_option_c2_acceptance.py")
        self.assertIn("'historical_verification_supported': False", source)
        self.assertIn("NOT_QUALIFIED_UNSUPPORTED_PROOF_CLASSES", source)
        state = read("docs/CURRENT_STATE.md").split("## 历史状态快照", 1)[0]
        self.assertIn("NO_QUALIFIED_CANDIDATE", state)
        self.assertIn("NOT_QUALIFIED", state)
        self.assertIn("停止默认补证", state)

    def test_readonly_runtime_probe_is_bounded_and_has_no_actuator(self):
        source = read(".github/workflows/bybit-demo-readonly.yml")
        bodies = re.findall(r"python3 - <<'PY'\n(.*?)^          PY$", source, re.M | re.S)
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            ast.parse(textwrap.dedent(body))
        runtime = textwrap.dedent(bodies[0])
        self.assertIn("'snapshot_is_atomic': False", runtime)
        self.assertIn("len(text.encode()) > 3500", runtime)
        self.assertIn("'OBSERVED_NOT_FULL_RUNTIME_ACCEPTANCE'", runtime)
        self.assertIn("['docker', 'inspect'", runtime)
        self.assertIn("['docker', 'logs'", runtime)
        for forbidden in ("'restart'", "'stop'", "'exec'", "'kill'", "'--repair-runtime-contamination'", "'.env.runtime'"):
            self.assertNotIn(forbidden, runtime)
        self.assertIn('test -n "${ECS_FINGERPRINT}"', source)
        self.assertIn('test -s "${KEY_DIR}/known_hosts"', source)
        self.assertIn('if [[ "${observed}" == "${ECS_FINGERPRINT}" ]]', source)
        self.assertNotIn('if [[ -n "${ECS_FINGERPRINT}" ]]', source)

    def test_engineering_branch_is_pinned_archive_only(self):
        source = read(".github/workflows/bybit-demo-readonly.yml")
        branch = "fix/replay-ordering-closeout-20260920"
        manifest = "188458003f4c50c86dd4f309a9f85b0c9fca71b9ec1d346c0d2b4466c5957495"
        self.assertIn("      - " + branch, source)
        self.assertIn("READONLY_ARCHIVE_ONLY: ${{ github.ref_name == '" + branch +
                      "' && 'true' || 'false' }}", source)
        for field, value in (("READONLY_REPLAY_RUN_ID", "35498278569-1"),
                             ("READONLY_MANIFEST_SHA", manifest)):
            self.assertIn(field + ": ${{ github.ref_name == '" + branch +
                          "' && '" + value + "' ||", source)
        # Execute just the shell preflight, before key files, SSH or network.
        match = re.search(r"          umask 077\n(.*?)          KEY_DIR=", source, re.S)
        self.assertIsNotNone(match)
        script = "set -euo pipefail\n" + textwrap.dedent(match[1])
        env = {"PATH": os.environ["PATH"], "READONLY_ARCHIVE_ONLY": "true",
               "READONLY_RUN_ID": "123-1", "READONLY_CODE_SHA": "a" * 64,
               "READONLY_REPLAY_RUN_ID": "35498278569-1", "READONLY_MANIFEST_SHA": manifest,
               "ECS_HOST": "example.invalid", "ECS_USER": "readonly", "ECS_PORT": "22",
               "ECS_FINGERPRINT": "SHA256:" + "a" * 43}
        self.assertEqual(subprocess.run(["bash", "-c", script], env=env,
                                       capture_output=True, timeout=5).returncode, 0)
        for change in ({"READONLY_REPLAY_RUN_ID": ""}, {"READONLY_MANIFEST_SHA": ""},
                       {"READONLY_REPLAY_RUN_ID": "34761067389-1"},
                       {"READONLY_MANIFEST_SHA": "b" * 64}):
            with self.subTest(change=change):
                self.assertNotEqual(subprocess.run(["bash", "-c", script], env={**env, **change},
                                                   capture_output=True, timeout=5).returncode, 0)


if __name__ == "__main__":
    unittest.main()
