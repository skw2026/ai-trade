#!/usr/bin/env python3
"""Fixed ECS public-evidence sidecar; never reads account/runtime env files."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

NAME = "ai-trade-forward-evidence-v1"
ROOT = Path("/opt/ai-trade/data/research/forward_evidence_v1")
FILES = {"forward_evidence.py", "collect_bybit_microstructure.py"}
SCHEMA = "forward_ecs_delivery_v1"


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def command(argv, timeout=30):
    return subprocess.run(argv, capture_output=True, timeout=timeout, check=False)


def read_json(path):
    need(path.is_file() and not path.is_symlink() and path.stat().st_size < 1024 * 1024, "REPORT_FILE_INVALID")
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned_release(expected):
    need(re.fullmatch(r"[0-9a-f]{40}", expected), "INVALID_RELEASE_SHA")
    release = Path("/opt/ai-trade/current").resolve(strict=True)
    need(release == Path("/opt/ai-trade/releases") / expected, "RELEASE_CHANGED")
    manifest = read_json(release / "release_manifest.json")
    need(manifest.get("git_sha") == expected, "RELEASE_MANIFEST_IDENTITY")
    checked = command(["python3", str(release / "deploy/release_integrity.py"), "--release-dir", str(release)], 60)
    need(checked.returncode == 0, "RELEASE_TREE_INTEGRITY")
    image = manifest["images"]["research"]
    need(isinstance(image, str) and re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}", image), "RESEARCH_IMAGE_NOT_PINNED")
    return image


def runtime_state():
    template = ('{"running":{{json .State.Running}},"restarts":{{json .RestartCount}},'
                '"image_id":{{json .Image}},"cmd":{{json .Config.Cmd}},'
                '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"unknown"{{end}}}')
    report = {}
    for name in ("ai-trade", "ai-trade-market-alpha-collector", NAME):
        result = command(["docker", "inspect", "--format", template, name])
        if result.returncode:
            report[name] = {"present": False}
            continue
        item = json.loads(result.stdout)
        record = {key: item[key] for key in ("running", "restarts", "image_id", "health")}
        record["present"] = True
        record["cmd_sha256"] = hashlib.sha256(json.dumps(item["cmd"]).encode()).hexdigest()
        if name.endswith("market-alpha-collector"):
            for arg in item["cmd"] or []:
                if re.fullmatch(r"--retention-days=[0-9]+", arg):
                    record["retention_days"] = int(arg.split("=")[1])
        report[name] = record
    logs = command(["docker", "logs", "--since", "5m", "--tail", "3000", "ai-trade"])
    lines = [line for line in (logs.stdout + logs.stderr).decode(errors="replace").splitlines()
             if "RUNTIME_STATUS:" in line]
    flags = dict(re.findall(r"\b(operator_reduce_only|force_reduce_only|trading_halted)=(true|false)", lines[-1])) if lines else {}
    return report, flags


def create_directory(path, mode=0o700):
    path = Path(path)
    for parent in reversed(path.parents):
        need(not parent.is_symlink(), "OUTPUT_PARENT_SYMLINK")
    need(not path.is_symlink(), "OUTPUT_SYMLINK")
    path.mkdir(mode=mode, parents=True, exist_ok=False)


def run_spec(image, code, data, uid, gid):
    return ["docker", "run", "-d", "--name", NAME, "--restart=no", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64",
            "--memory=512m", "--cpus=0.5", "--log-opt=max-size=5m", "--log-opt=max-file=2",
            "--user", "%d:%d" % (uid, gid), "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
            "--mount", "type=bind,source=%s,target=/forward,readonly" % code,
            "--mount", "type=bind,source=%s,target=/evidence" % data,
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python3", image,
            "/forward/forward_evidence.py", "run", "--root=/evidence"]


def execute(mode, expected, commit, bundle):
    need(re.fullmatch(r"[0-9a-f]{40}", commit), "INVALID_CODE_SHA")
    image = pinned_release(expected)
    containers, flags = runtime_state()
    ntp = command(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    memory = re.search(r"^MemAvailable:\s+(\d+) kB$", Path("/proc/meminfo").read_text(), re.M)
    result = {"schema_version": SCHEMA, "mode": mode, "commit_sha": commit,
              "existing_release_sha": expected, "observed_epoch_ms": time.time_ns() // 1_000_000,
              "containers": containers, "runtime_flags": flags,
              "disk_free_bytes": shutil.disk_usage("/opt/ai-trade/data").free,
              "ntp_synchronized": ntp.returncode == 0 and ntp.stdout.strip() == b"yes",
              "memory_available_bytes": int(memory.group(1)) * 1024 if memory else None,
              "economic_evidence": False, "account_access": False, "order_submission": False,
              "research_image": image, "backup_off_host_verified": False}
    need(containers["ai-trade"].get("running") is True, "TRADING_SERVICE_NOT_RUNNING")
    need(flags.get("operator_reduce_only") == "true" and flags.get("force_reduce_only") == "true",
         "DEMO_ENTRY_LOCK_NOT_VERIFIED")
    batch = ROOT / commit
    if mode == "start":
        need(not containers[NAME].get("present"), "SIDECAR_ALREADY_EXISTS")
        need(result["disk_free_bytes"] >= 10 * 1024**3, "DISK_PREFLIGHT_RESERVE")
        need(result["ntp_synchronized"], "HOST_TIME_NOT_SYNCHRONIZED")
        need(result["memory_available_bytes"] is not None and result["memory_available_bytes"] >= 1024**3,
             "MEMORY_PREFLIGHT_RESERVE")
        image_check = command(["docker", "image", "inspect", "--format", "{{json .Config.Env}}", image])
        need(image_check.returncode == 0, "PINNED_RESEARCH_IMAGE_NOT_LOCAL")
        names = [value.split("=", 1)[0] for value in json.loads(image_check.stdout) or []]
        need(not any(re.search(r"KEY|SECRET|TOKEN|PASSWORD", name, re.I) for name in names), "IMAGE_CREDENTIAL_ENV")
        contents = json.loads(base64.b64decode(bundle, validate=True))
        need(isinstance(contents, dict) and set(contents) == FILES, "SOURCE_BUNDLE_FILES")
        need(all(isinstance(value, str) and len(value.encode()) < 256 * 1024 for value in contents.values()), "SOURCE_BUNDLE_SIZE")
        create_directory(batch)
        code, data = batch / "code", batch / "capture"
        code.mkdir(mode=0o755)
        data.mkdir(mode=0o700)
        uid, gid = (os.getuid(), os.getgid()) if os.getuid() else (65534, 65534)
        if os.getuid() == 0:
            os.chown(data, uid, gid)
        for name, content in contents.items():
            path = code / name
            with path.open("x") as handle:
                handle.write(content)
            path.chmod(0o444)
        # The fixed image is already deployed. No pull, account env, docker socket,
        # main project mount, service restart or candidate registry is exposed.
        started = command(run_spec(image, code, data, uid, gid))
        need(started.returncode == 0, "SIDECAR_START_FAILED")
        result["status"] = "SIDECAR_STARTED_NOT_ACCEPTED"
        result["source_sha256"] = {name: sha(code / name) for name in sorted(FILES)}
    elif mode in ("inspect", "verify"):
        result["status"] = "INSPECTED"
        health = batch / "capture/health.json"
        if health.is_file():
            result["capture"] = read_json(health)
        if mode == "verify":
            need(health.is_file(), "CAPTURE_HEALTH_MISSING")
            need(result["capture"].get("status") in ("CAPTURING", "SEGMENT_SEALED", "BOUNDED_COLLECTION_COMPLETE"), "CAPTURE_STOPPED_ERROR")
            need(result["capture"].get("completed_segments", 0) > 0, "NO_SEALED_SEGMENT")
            code, data = batch / "code", batch / "capture/segment-000"
            argv = ["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=512m", "--cpus=0.5",
                    "--mount", "type=bind,source=%s,target=/forward,readonly" % code,
                    "--mount", "type=bind,source=%s,target=/evidence,readonly" % data,
                    "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python3", image,
                    "/forward/forward_evidence.py", "verify", "--root=/evidence"]
            replayed = command(argv, 90)
            need(replayed.returncode == 0, "ISOLATED_REPLAY_FAILED")
            receipt = json.loads(replayed.stdout)
            need(receipt.get("status") == "SEALED", "REPLAY_RECEIPT_INVALID")
            result["first_segment"] = receipt
            result["status"] = "FIRST_SEGMENT_REPLAY_VERIFIED"
            result["source_sha256"] = {name: sha(code / name) for name in sorted(FILES)}
    else:
        raise ValueError("UNKNOWN_MODE")
    after, after_flags = runtime_state()
    need(after["ai-trade"] == containers["ai-trade"] and after_flags.get("operator_reduce_only") == "true"
         and after_flags.get("force_reduce_only") == "true", "TRADING_IDENTITY_OR_LOCK_CHANGED")
    need(pinned_release(expected) == image, "RELEASE_CHANGED_AFTER_OPERATION")
    result["trading_identity_unchanged"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("inspect", "start", "verify"))
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--bundle-base64", default="")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = execute(args.mode, args.expected_release_sha, args.commit_sha, args.bundle_base64)
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and str(error).isupper() else type(error).__name__
        result = {"schema_version": SCHEMA, "status": "INCOMPLETE", "reason_code": code,
                  "economic_evidence": False, "account_access": False, "order_submission": False}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 2 if result["status"] == "INCOMPLETE" else 0


if __name__ == "__main__":
    sys.exit(main())
