#!/usr/bin/env python3
"""No-account, no-network transport regressions, also usable as diagnostics."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ecs_ssh", ROOT / "deploy/ecs_ssh.py")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def fixture(kind, byte):
    name = kind.encode()
    blob = len(name).to_bytes(4, "big") + name + (32).to_bytes(4, "big") + bytes([byte]) * 32
    encoded = base64.b64encode(blob).decode()
    pin = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"host.example {kind} {encoded}\n", pin


PINNED, PIN = fixture("ssh-ed25519", 1)
OTHER, _ = fixture("ecdsa-sha2-nistp256", 2)
ENV = {"ECS_TRANSPORT_HOST": "host.example", "ECS_TRANSPORT_USER": "runner",
       "ECS_TRANSPORT_PORT": "22", "ECS_TRANSPORT_FINGERPRINT": PIN,
       "ECS_TRANSPORT_KEY": "synthetic-private-key"}


class PinnedTransportTest(unittest.TestCase):
    def test_only_matching_key_is_trusted_regardless_of_order(self):
        for scan in (OTHER + PINNED, PINNED + OTHER + PINNED):
            known, algorithms = transport.pinned_keys(scan, PIN, "host.example", "22")
            self.assertEqual(known, PINNED)
            self.assertEqual(algorithms, "ssh-ed25519")
            self.assertNotIn(OTHER.strip(), known)

    def test_unmatched_missing_and_malformed_keys_fail_closed(self):
        for scan in ("", OTHER, "host.example ssh-ed25519 INVALID@@@\n", "# comment\n"):
            with self.subTest(scan=scan), self.assertRaisesRegex(transport.TransportError, "FINGERPRINT_MISMATCH"):
                transport.pinned_keys(scan, PIN, "host.example", "22")

    def test_declared_key_type_must_match_wire_type(self):
        with self.assertRaisesRegex(transport.TransportError, "FINGERPRINT_MISMATCH"):
            transport.pinned_keys(PINNED.replace("ssh-ed25519", "ssh-rsa"), PIN, "host.example", "22")

    def test_rsa_uses_sha2_and_nondefault_port_binding(self):
        line, pin = fixture("ssh-rsa", 3)
        known, algorithms = transport.pinned_keys(line, pin, "host.example", "2222")
        self.assertTrue(known.startswith("[host.example]:2222 ssh-rsa "))
        self.assertEqual(algorithms, "rsa-sha2-512,rsa-sha2-256")
        self.assertNotIn("ssh-rsa", algorithms)

    def test_configuration_rejects_unsafe_or_missing_inputs(self):
        cases = {"ECS_TRANSPORT_HOST": ("-oProxyCommand=x", "host;id", "", "a b"),
                 "ECS_TRANSPORT_USER": ("runner;id", "-root", ""),
                 "ECS_TRANSPORT_PORT": ("0", "65536", "22;id", ""),
                 "ECS_TRANSPORT_FINGERPRINT": ("", "MD5:bad", PIN + "extra"),
                 "ECS_TRANSPORT_KEY": ("", " \n")}
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(transport.TransportError):
                    transport.configuration({**ENV, name: value})

    def test_missing_pin_stops_before_any_external_command(self):
        with mock.patch.dict(os.environ, {**ENV, "ECS_TRANSPORT_FINGERPRINT": ""}, clear=True), \
             mock.patch.object(sys, "argv", ["ecs_ssh.py"]), \
             mock.patch.object(transport.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(transport.main(), 1)
            run.assert_not_called()
            self.assertIn("ECS_TRUSTED_FINGERPRINT_REQUIRED", out.getvalue())
            self.assertNotIn(ENV["ECS_TRANSPORT_KEY"], out.getvalue())

    def test_timeout_is_bounded(self):
        self.assertEqual(transport.timeout_seconds("150m"), 9000)
        self.assertEqual(transport.timeout_seconds("2m"), 120)
        for value in ("0", "-1", "151m", "9001", "1d", "1;id"):
            with self.assertRaises(transport.TransportError):
                transport.timeout_seconds(value)

    def test_forwarded_env_is_literal_and_explicit(self):
        value = "a'$(printf BAD)\n spaced value"
        payload = transport.script_payload('printf "%s" "$VALUE"', "VALUE", {"VALUE": value, "UNRELATED": "not-forwarded"})
        result = subprocess.run(["bash", "-s"], input=payload, capture_output=True, check=True)
        self.assertEqual(result.stdout.decode(), value)
        self.assertNotIn(b"UNRELATED", payload)
        for names in ("VALUE;id", "MISSING"):
            with self.assertRaises(transport.TransportError):
                transport.script_payload("true", names, {"VALUE": value})

    def test_connection_isolates_credentials_and_host_key_sources(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, (OTHER + PINNED).encode(), b"")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(transport.subprocess, "run", side_effect=run), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            connection = transport.Connection(tmp, ENV)
            self.assertEqual((Path(tmp) / "known_hosts").read_text(), PINNED)
            self.assertEqual((Path(tmp) / "identity").stat().st_mode & 0o777, 0o600)
            for option in ("StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null", "IdentityAgent=none",
                           "UpdateHostKeys=no", "VerifyHostKeyDNS=no", "BatchMode=yes", "IdentitiesOnly=yes",
                           "HostKeyAlgorithms=ssh-ed25519"):
                self.assertIn(option, connection.options)
            payload = transport.script_payload("true", "SECRET", {"SECRET": "fixture-secret"})
            connection.execute(payload, 60)
            self.assertEqual(calls[-1][-1], "bash -s")
            self.assertNotIn("fixture-secret", repr(calls))
            self.assertNotIn(PIN, out.getvalue())

    def test_host_key_mismatch_never_runs_ssh(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(transport, "run_checked") as checked, \
             mock.patch.object(transport.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, OTHER.encode(), b"")):
            with self.assertRaisesRegex(transport.TransportError, "FINGERPRINT_MISMATCH"):
                transport.Connection(tmp, ENV)
            self.assertEqual(checked.call_count, 1)
            self.assertEqual(checked.call_args.args[0][0], "ssh-keygen")

    def test_invalid_private_key_stops_before_scan(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(transport, "run_checked", side_effect=transport.TransportError("ECS_SSH_KEY_INVALID")), \
             mock.patch.object(transport.subprocess, "run") as run:
            with self.assertRaisesRegex(transport.TransportError, "SSH_KEY_INVALID"):
                transport.Connection(tmp, ENV)
            run.assert_not_called()

    def test_upload_has_exact_two_files_and_flat_run_bound_destination(self):
        release = "123-1-" + "a" * 40
        connection = transport.Connection.__new__(transport.Connection)
        connection.host, connection.user, connection.port, connection.options = "host.example", "runner", "22", []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(connection, "execute") as execute, \
             mock.patch.object(transport, "run_checked") as checked:
            source = Path(tmp) / release
            source.mkdir()
            for name in ("deploy_bundle.tgz", "deploy_bundle.tgz.sha256"):
                (source / name).write_bytes(b"fixture")
            connection.upload(str(source), release, 60)
            self.assertIn(("/opt/ai-trade/incoming/" + release).encode(), execute.call_args.args[0])
            argv = checked.call_args.args[0]
            self.assertEqual(argv[-1], "runner@host.example:/opt/ai-trade/incoming/" + release + "/")
            self.assertEqual([Path(v).name for v in argv[-3:-1]], ["deploy_bundle.tgz", "deploy_bundle.tgz.sha256"])
            for invalid in ("../../outside", "123-1-wrongsha", release + ";id"):
                execute.reset_mock()
                with self.assertRaises(transport.TransportError):
                    connection.upload(str(source), invalid, 60)
                execute.assert_not_called()

    def test_reason_codes_do_not_echo_sensitive_stderr(self):
        self.assertEqual(transport.failure_reason(255, b"Permission denied for private-host"), "ECS_SSH_AUTH_FAILED")
        self.assertEqual(transport.failure_reason(255, b"Host key verification failed"), "ECS_HOST_KEY_REJECTED")
        self.assertEqual(transport.failure_reason(255, b"connect timed out private-host"), "ECS_SSH_CONNECTION_FAILED")
        self.assertEqual(transport.failure_reason(1, b"private remote diagnostic"), "ECS_REMOTE_COMMAND_FAILED")

    def test_scoped_workflows_share_transport_and_pin_downloads(self):
        for name in ("cd", "smoke", "option-archive-lifecycle", "option-lifecycle-v4"):
            text = (ROOT / ".github/workflows" / (name + ".yml")).read_text()
            self.assertIn("uses: ./.github/actions/ecs-ssh", text)
            self.assertNotIn("appleboy/", text)
            self.assertIn("pin-known-hosts", text)
            self.assertIn("GlobalKnownHostsFile=/dev/null", text)
            self.assertIn('HostKeyAlgorithms="${PINNED_HOST_KEY_ALGORITHMS}"', text)
            self.assertIn("StrictHostKeyChecking=yes", text)
        cd = (ROOT / ".github/workflows/cd.yml").read_text()
        self.assertLess(cd.index("Preflight pinned ECS connection"), cd.index("Build and Push Runtime Image"))
        archive = (ROOT / ".github/workflows/option-archive-lifecycle.yml").read_text()
        self.assertNotIn("github.event.before", archive)
        self.assertNotIn("  push:", archive)
        self.assertIn("workflow_run:", archive)
        action = (ROOT / ".github/actions/ecs-ssh/action.yml").read_text()
        self.assertIn("${GITHUB_ACTION_PATH}/../../../deploy/ecs_ssh.py", action)

    def test_preflight_has_no_service_or_account_mutation(self):
        for forbidden in ("docker compose", "restart", "mkdir", "rm ", "cat ", "bybit", "curl", "wget"):
            self.assertNotIn(forbidden, transport.PREFLIGHT)
        self.assertIn("docker info", transport.PREFLIGHT)
        self.assertIn("test -w /opt/ai-trade", transport.PREFLIGHT)


if __name__ == "__main__":
    unittest.main()
