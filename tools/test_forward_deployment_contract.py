#!/usr/bin/env python3
"""Real, offline Docker contract self-check using temporary synthetic evidence only.

Does not pull images, connect to an exchange, inspect accounts, or alter a gate.
The original capture source is exercised without changing its frozen hashes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid


IMAGE = "ghcr.io/skw2026/ai-trade-research@sha256:96e70a394c8a74264e5ea30a4bdbcb2b08aed313e587296b757299d98de93eba"
SOURCES = {
    "forward_evidence.py": "9f4c31dac311ea14a3b3f42de31b7a872f289902f7f49412b7be05e10388e070",
    "collect_bybit_microstructure.py": "90f4bb9c417781f3d9d93b39897b74061dc226a827861b0f425af8f13502d101",
}
LABEL = "ai-trade.synthetic-contract"
IMPORT = "import sys; sys.path.insert(0,'/forward'); import forward_evidence; import websockets"
WRITE_FIXTURE = """
import gzip, json, os, sys
from pathlib import Path
sys.path.insert(0, '/forward')
import forward_evidence as forward
import websockets
os.umask(0o077)
root = Path('/evidence/segment-000')
root.mkdir(mode=0o700)
raw = root / 'receive.jsonl.gz'
state = forward.Replay()
with gzip.open(raw, 'xt', encoding='utf-8') as output:
    for symbol in forward.SYMBOLS:
        for topic in ('orderbook.50.' + symbol, 'publicTrade.' + symbol):
            payload = {'topic': topic, 'type': 'snapshot', 'ts': 1000, 'cts': 999}
            if topic.startswith('orderbook.'):
                payload['data'] = {'s': symbol, 'u': 1, 'seq': 1,
                                   'b': [['99', '2']], 'a': [['101', '2']]}
            else:
                payload['data'] = [{'s': symbol, 'T': 1000, 'S': 'Buy',
                                    'p': '100', 'v': '1', 'i': symbol}]
            sequence = state.count + 1
            row = {'sequence': sequence, 'receive_utc_ns': 1800000000000000000 + sequence * 1000000,
                   'receive_monotonic_ns': 100000000000 + sequence * 1000000,
                   'wire': json.dumps(payload)}
            output.write(forward.canonical(row) + '\\n')
            state.accept(row)
receipt = {'schema_version': forward.SCHEMA, 'status': 'SEALED', 'segment_index': 0,
           'raw_sha256': forward.digest(raw), 'raw_bytes': raw.stat().st_size,
           'observation': state.summary(), 'previous_receipt_sha256': None,
           'source_sha256': forward.digest('/forward/forward_evidence.py'), 'economic_evidence': False}
forward.write_json(root / 'receipt.json', receipt)
assert os.getuid() == 65534 and os.getgid() == 65534
assert root.stat().st_mode & 0o777 == 0o700
for name in ('receive.jsonl.gz', 'receipt.json'):
    item = (root / name).stat()
    assert item.st_uid == 65534 and item.st_gid == 65534 and item.st_mode & 0o777 == 0o600
print(json.dumps(receipt, sort_keys=True))
"""
CLEAN_FIXTURE = """
import os, shutil
from pathlib import Path
target = Path('/evidence/segment-000')
if target.exists():
    assert not target.is_symlink() and target.stat().st_uid == os.getuid() == 65534
    assert set(p.name for p in target.iterdir()) <= {'receive.jsonl.gz', 'receipt.json'}
    shutil.rmtree(str(target))
"""


class ContractFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ContractFailure(code)


def execute(argv, timeout=45):
    return subprocess.run(argv, capture_output=True, check=False, timeout=timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, choices=(IMAGE,))
    parser.add_argument("--platform", required=True, choices=("linux/amd64",))
    args = parser.parse_args(argv)
    token, checks, current = uuid.uuid4().hex, [], "local_image_identity"
    temporary, data, last_result, failure_result, retained = None, None, None, None, None
    error = None
    sequence = 0

    def container(code, evidence, command, *, default_user=False, writable=False):
        nonlocal sequence, last_result
        sequence += 1
        name = "forward-contract-" + token + "-%d" % sequence
        spec = ["docker", "run", "--pull=never", "--platform", args.platform, "--rm", "--name", name,
                "--label", LABEL + "=" + token, "--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=512m", "--cpus=0.5",
                "--log-opt=max-size=1m", "--log-opt=max-file=1", "--env", "PYTHONDONTWRITEBYTECODE=1"]
        if not default_user:
            spec += ["--user", "65534:65534"]
        if code is not None:
            spec += ["--mount", "type=bind,source=%s,target=/forward,readonly" % code]
        if evidence is not None:
            spec += ["--mount", "type=bind,source=%s,target=/evidence%s" % (evidence, "" if writable else ",readonly")]
        spec += ["--entrypoint", "python3", args.image] + command
        try:
            last_result = execute(spec)
        except subprocess.TimeoutExpired:
            # Terminate only a container carrying this invocation's exact random label.
            observed = execute(["docker", "inspect", "--format", '{{index .Config.Labels "' + LABEL + '"}}', name], 10)
            if observed.returncode == 0:
                require(observed.stdout.strip().decode() == token, "TIMEOUT_CONTAINER_IDENTITY_CHANGED")
                removed = execute(["docker", "rm", "-f", name], 10)
                require(removed.returncode == 0, "TIMEOUT_CONTAINER_NOT_REMOVED")
            raise ContractFailure("CONTAINER_TIMEOUT") from None
        return last_result

    def expected_failure(result, markers, label):
        require(result.returncode != 0 and any(marker in result.stderr for marker in markers), label)
        checks.append(label)

    try:
        require(os.getuid() != 65534, "FIXTURE_HOST_OWNER_MUST_DIFFER_FROM_WRITER")
        found = execute(["docker", "image", "inspect", "--format",
                         '{"os":{{json .Os}},"architecture":{{json .Architecture}},"id":{{json .Id}}}', args.image], 15)
        require(found.returncode == 0, "PINNED_IMAGE_NOT_LOCAL_NO_PULL_ATTEMPTED")
        platform = json.loads(found.stdout)
        require(platform.get("os") == "linux" and platform.get("architecture") == "amd64", "IMAGE_PLATFORM_MISMATCH")
        temporary = Path(tempfile.mkdtemp(prefix="forward-contract-")).resolve()
        temporary.chmod(0o700)
        code, bad_code, data, unsealed = (temporary / name for name in ("code", "private-code", "synthetic", "unsealed"))
        for path, mode in ((code, 0o755), (bad_code, 0o700), (data, 0o777), (unsealed, 0o755)):
            path.mkdir()
            path.chmod(mode)
        # Writable synthetic staging is nested inside a host-private 0700 directory.
        # The writer creates its own 0700 child; no host chown or added caps needed.
        for filename, expected in SOURCES.items():
            source = Path(__file__).with_name(filename).read_bytes()
            require(hashlib.sha256(source).hexdigest() == expected, "FROZEN_SOURCE_HASH_MISMATCH")
            for directory in (code, bad_code):
                target = directory / filename
                target.write_bytes(source)
                target.chmod(0o444)
        current = "code_0700_import_rejected"
        result = container(bad_code, None, ["-B", "-c", IMPORT])
        expected_failure(result, (b"PermissionError", b"Permission denied", b"ModuleNotFoundError"), current)
        current = "unsealed_evidence_rejected"
        result = container(code, unsealed, ["-B", "/forward/forward_evidence.py", "verify", "--root=/evidence"])
        expected_failure(result, (b"FileNotFoundError",), current)
        current = "nonroot_import_and_private_write"
        result = container(code, data, ["-B", "-c", WRITE_FIXTURE], writable=True)
        require(result.returncode == 0, "NONROOT_SYNTHETIC_WRITE_FAILED")
        written = json.loads(result.stdout)
        require(written.get("status") == "SEALED", "SYNTHETIC_RECEIPT_MISSING")
        checks.append(current)
        current = "default_root_without_caps_read_rejected"
        assert_root_and_verify = ("import os,runpy,sys; assert os.getuid()==0; "
                                  "sys.argv=['/forward/forward_evidence.py','verify','--root=/evidence']; "
                                  "sys.path.insert(0,'/forward'); "
                                  "runpy.run_path('/forward/forward_evidence.py',run_name='__main__')")
        result = container(code, data / "segment-000", ["-B", "-c", assert_root_and_verify], default_user=True)
        expected_failure(result, (b"PermissionError", b"Permission denied"), current)
        current = "same_uid_offline_replay"
        result = container(code, data / "segment-000", ["-B", "/forward/forward_evidence.py", "verify", "--root=/evidence"])
        require(result.returncode == 0 and json.loads(result.stdout) == written, "SAME_UID_REPLAY_MISMATCH")
        checks.append(current)
    except Exception as exc:
        error = str(exc) if isinstance(exc, ContractFailure) else type(exc).__name__
        failure_result = last_result
    finally:
        if temporary is not None:
            try:
                if data is not None and data.exists():
                    cleanup = container(None, data, ["-B", "-c", CLEAN_FIXTURE], writable=True)
                    require(cleanup.returncode == 0, "SYNTHETIC_CLEANUP_FAILED")
                shutil.rmtree(str(temporary))
            except Exception as exc:
                retained = str(temporary)
                if error is None:
                    current, failure_result = "synthetic_cleanup", last_result
                error = error or (str(exc) if isinstance(exc, ContractFailure) else type(exc).__name__)
    report = {"status": "SYNTHETIC_DEPLOYMENT_CONTRACT_PASSED" if error is None else "SYNTHETIC_DEPLOYMENT_CONTRACT_FAILED",
              "checks": checks, "image": args.image, "platform": args.platform,
              "market_connections": 0, "account_access": False, "production_replay": False,
              "gate_modified": False, "synthetic_files_removed": temporary is not None and retained is None}
    if error is not None:
        report.update(failed_step=current, reason_code=error)
        if failure_result is not None:
            report.update(child_returncode=failure_result.returncode, stderr_sha256=hashlib.sha256(failure_result.stderr).hexdigest())
        if retained:
            report["retained_synthetic_directory"] = retained
    print(json.dumps(report, sort_keys=True))
    return 0 if error is None else 2


if __name__ == "__main__":
    sys.exit(main())
