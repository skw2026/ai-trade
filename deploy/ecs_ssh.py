#!/usr/bin/env python3
"""Pinned OpenSSH transport shared by deployment and its post-CD checks.

keyscan is discovery, never authority: only the existing SHA256 pin is trusted.
Credentials and forwarded values are never printed or placed in command argv.
"""
import base64
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


ALGORITHMS = {
    "ssh-ed25519": "ssh-ed25519",
    "ecdsa-sha2-nistp256": "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384": "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521": "ecdsa-sha2-nistp521",
    "ssh-rsa": "rsa-sha2-512,rsa-sha2-256",
}
PREFLIGHT = """set -euo pipefail
test -d /opt/ai-trade && test -w /opt/ai-trade && test -x /opt/ai-trade || { echo ECS_DEPLOY_ROOT_NOT_WRITABLE >&2; exit 1; }
test -r /opt/ai-trade/.env.runtime || { echo ECS_RUNTIME_ENV_NOT_READABLE >&2; exit 1; }
if test -e /opt/ai-trade/incoming; then
  test -d /opt/ai-trade/incoming && test -w /opt/ai-trade/incoming && test -x /opt/ai-trade/incoming || { echo ECS_INCOMING_NOT_WRITABLE >&2; exit 1; }
fi
for tool in bash tar sha256sum python3 docker; do command -v "$tool" >/dev/null || { echo ECS_REQUIRED_TOOL_MISSING >&2; exit 1; }; done
docker info >/dev/null 2>&1 || { echo ECS_DOCKER_NOT_ACCESSIBLE >&2; exit 1; }
echo ECS_DEPLOY_READONLY_PREFLIGHT_PASS
"""


class TransportError(Exception):
    """Messages are fixed reason codes, never external output or credentials."""


def require(ok, reason):
    if not ok:
        raise TransportError(reason)


def configuration(env):
    host = env.get("ECS_TRANSPORT_HOST", "").strip()
    user = env.get("ECS_TRANSPORT_USER", "").strip()
    port = env.get("ECS_TRANSPORT_PORT", "22").strip()
    pin = env.get("ECS_TRANSPORT_FINGERPRINT", "").strip()
    key = env.get("ECS_TRANSPORT_KEY", "").replace("\r", "")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*|:[0-9a-fA-F:]+", host), "ECS_HOST_INVALID")
    require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user), "ECS_USER_INVALID")
    require(port.isascii() and port.isdigit() and 1 <= int(port) <= 65535, "ECS_PORT_INVALID")
    require(re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", pin), "ECS_TRUSTED_FINGERPRINT_REQUIRED")
    require(key.strip(), "ECS_SSH_KEY_REQUIRED")
    return host, user, str(int(port)), pin, key


def pinned_keys(scanned, pin, host, port):
    """Discard every unpinned key, even when one other scanned key matches."""
    selected = {}
    host_field = host if port == "22" else f"[{host}]:{port}"
    for line in scanned.splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[1] not in ALGORITHMS:
            continue
        try:
            blob = base64.b64decode(fields[2], validate=True)
            length = int.from_bytes(blob[:4], "big")
            if blob[4:4 + length].decode("ascii") != fields[1]:
                continue
        except (ValueError, UnicodeError):
            continue
        digest = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        if digest == pin:
            selected[fields[1]] = f"{host_field} {fields[1]} {fields[2]}\n"
    require(selected, "ECS_HOST_FINGERPRINT_MISMATCH")
    return "".join(selected.values()), ",".join(ALGORITHMS[k] for k in selected)


def timeout_seconds(value):
    match = re.fullmatch(r"([1-9][0-9]*)([smh]?)", value)
    require(match, "ECS_TIMEOUT_INVALID")
    seconds = int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]
    require(seconds <= 9000, "ECS_TIMEOUT_INVALID")
    return seconds


def script_payload(script, forwarded, env):
    require(script.strip() and "\x00" not in script, "ECS_SCRIPT_REQUIRED")
    exports = []
    for name in filter(None, (item.strip() for item in forwarded.split(","))):
        require(re.fullmatch(r"[A-Z_][A-Z0-9_]*", name), "ECS_ENV_NAME_INVALID")
        require(name in env and "\x00" not in env[name], "ECS_ENV_VALUE_MISSING")
        exports.append(f"export {name}={shlex.quote(env[name])}\n")
    return ("set -euo pipefail\n" + "".join(exports) + script + "\n").encode()


def failure_reason(code, stderr):
    if code != 255:
        for reason in ("ECS_DEPLOY_ROOT_NOT_WRITABLE", "ECS_RUNTIME_ENV_NOT_READABLE",
                       "ECS_INCOMING_NOT_WRITABLE", "ECS_REQUIRED_TOOL_MISSING", "ECS_DOCKER_NOT_ACCESSIBLE"):
            if reason.encode() in stderr.splitlines():
                return reason
        return "ECS_REMOTE_COMMAND_FAILED"
    text = stderr.lower()
    if b"host key verification failed" in text or b"host identification has changed" in text:
        return "ECS_HOST_KEY_REJECTED"
    if b"permission denied" in text or b"authentication failed" in text:
        return "ECS_SSH_AUTH_FAILED"
    return "ECS_SSH_CONNECTION_FAILED"


def run_checked(argv, *, data=None, timeout=60, reason=None, quiet=False):
    # stderr can include hosts/paths; only emit fixed diagnostic categories.
    with tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(argv, input=data, stdout=subprocess.DEVNULL if quiet else None,
                                    stderr=errors, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise TransportError("ECS_TRANSPORT_TIMEOUT") from None
        if result.returncode:
            errors.seek(0, 2)
            errors.seek(max(0, errors.tell() - 16384))
            raise TransportError(reason or failure_reason(result.returncode, errors.read()))


class Connection:
    def __init__(self, directory, env):
        host, user, port, pin, key = configuration(env)
        self.host, self.user, self.port = host, user, port
        directory = Path(directory)
        key_path = directory / "identity"
        key_path.write_text(key.rstrip("\n") + "\n")
        key_path.chmod(0o600)
        run_checked(["ssh-keygen", "-y", "-P", "", "-f", str(key_path)],
                    reason="ECS_SSH_KEY_INVALID", quiet=True)
        try:
            scan = subprocess.run(["ssh-keyscan", "-T", "10", "-p", port,
                                   "-t", "ed25519,ecdsa,rsa", host], capture_output=True,
                                  timeout=40, check=False)
        except subprocess.TimeoutExpired:
            raise TransportError("ECS_HOST_KEY_SCAN_TIMEOUT") from None
        require(scan.returncode == 0, "ECS_HOST_KEY_SCAN_FAILED")
        known, algorithms = pinned_keys(scan.stdout.decode(errors="replace"), pin, host, port)
        known_path = directory / "known_hosts"
        known_path.write_text(known)
        known_path.chmod(0o600)
        self.options = ["-F", "/dev/null", "-i", str(key_path)]
        for option in (
            "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
            "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null",
            f"UserKnownHostsFile={known_path}", f"HostKeyAlgorithms={algorithms}",
            "UpdateHostKeys=no", "VerifyHostKeyDNS=no", "ConnectionAttempts=1",
            "ConnectTimeout=15", "ServerAliveInterval=15", "ServerAliveCountMax=3",
        ):
            self.options.extend(["-o", option])
        print("::notice title=ECS transport::PINNED_HOST_KEY_SELECTED", flush=True)

    def execute(self, payload, timeout):
        run_checked(["ssh", *self.options, "-p", self.port, f"{self.user}@{self.host}",
                     "bash -s"], data=payload, timeout=timeout)

    def upload(self, source, release_id, timeout):
        require(re.fullmatch(r"[0-9]+-[0-9]+-[0-9a-f]{40}", release_id), "ECS_RELEASE_ID_INVALID")
        source = Path(source)
        require(source.name == release_id, "ECS_BUNDLE_SOURCE_MISMATCH")
        files = [source / "deploy_bundle.tgz", source / "deploy_bundle.tgz.sha256"]
        require(all(p.is_file() and not p.is_symlink() for p in files), "ECS_BUNDLE_FILES_MISSING")
        remote = "/opt/ai-trade/incoming/" + release_id
        self.execute(f"set -euo pipefail\nmkdir -p -- {shlex.quote(remote)}\n".encode(), 60)
        address = f"[{self.host}]" if ":" in self.host else self.host
        run_checked(["scp", *self.options, "-P", self.port, *map(str, files),
                     f"{self.user}@{address}:{remote}/"], timeout=timeout,
                    reason="ECS_BUNDLE_UPLOAD_FAILED")


def main():
    env = os.environ
    try:
        if len(sys.argv) > 1:
            require(len(sys.argv) == 6 and sys.argv[1] == "pin-known-hosts", "ECS_ARGUMENTS_INVALID")
            path, host, port, pin = sys.argv[2:]
            require(re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", pin), "ECS_TRUSTED_FINGERPRINT_REQUIRED")
            require(not Path(path).is_symlink(), "ECS_KNOWN_HOSTS_SYMLINK")
            known, algorithms = pinned_keys(Path(path).read_text(), pin, host, port)
            Path(path).write_text(known)
            print(algorithms)
            return 0
        # Fail closed before keyscan or any SSH if a required input is absent.
        configuration(env)
        mode = env.get("ECS_TRANSPORT_MODE", "script")
        require(mode in ("script", "preflight", "upload"), "ECS_MODE_INVALID")
        timeout = timeout_seconds(env.get("ECS_TRANSPORT_TIMEOUT", "10m"))
        payload = None
        if mode == "script":
            payload = script_payload(env.get("ECS_TRANSPORT_SCRIPT", ""),
                                     env.get("ECS_TRANSPORT_ENVS", ""), env)
        with tempfile.TemporaryDirectory(prefix="ai-trade-ssh-") as directory:
            connection = Connection(directory, env)
            if mode == "preflight":
                connection.execute(PREFLIGHT.encode(), min(timeout, 120))
            elif mode == "upload":
                connection.upload(env.get("ECS_TRANSPORT_SOURCE", ""),
                                  env.get("ECS_TRANSPORT_RELEASE_ID", ""), timeout)
            else:
                connection.execute(payload, timeout)
        return 0
    except TransportError as error:
        print(f"::error title=ECS transport::{error}", flush=True)
        return 1
    except OSError:
        print("::error title=ECS transport::ECS_LOCAL_TRANSPORT_IO_FAILED", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
