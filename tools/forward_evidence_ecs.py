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
import stat
import subprocess
import sys
import time

NAME = "ai-trade-forward-evidence-v1"
ROOT = Path("/opt/ai-trade/data/research/forward_evidence_v1")
FILES = {"forward_evidence.py", "collect_bybit_microstructure.py"}
SCHEMA = "forward_ecs_delivery_v1"
STARTUP_REPAIR_BATCH = "1efbac875f971a1c72840e528d27d9e788634d71"
STARTUP_SOURCE_HASHES = {
    "forward_evidence.py": "9f4c31dac311ea14a3b3f42de31b7a872f289902f7f49412b7be05e10388e070",
    "collect_bybit_microstructure.py": "90f4bb9c417781f3d9d93b39897b74061dc226a827861b0f425af8f13502d101",
}
INVENTORY_IMAGE = "ghcr.io/skw2026/ai-trade-research@sha256:96e70a394c8a74264e5ea30a4bdbcb2b08aed313e587296b757299d98de93eba"
INVENTORY_TOPICS = tuple(kind + symbol for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
                         for kind in ("orderbook.50.", "publicTrade."))
INVENTORY_MAX_SEGMENTS = 97
INVENTORY_MAX_BYTES = 2 * 1024**3


def inventory_directory(path):
    """Open every path component without following symlinks (including parents)."""
    path = Path(path)
    need(path.is_absolute() and ".." not in path.parts, "INVENTORY_PATH_OUTSIDE_BATCH")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def inventory_file(path, limit, deadline, *, parse=False):
    """Hash only regular files, using bounded streaming; never import capture code."""
    parent = inventory_directory(Path(path).parent)
    try:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        need(stat.S_ISREG(before.st_mode), "INVENTORY_NOT_REGULAR_FILE")
        need(0 <= before.st_size <= limit, "INVENTORY_FILE_SIZE_LIMIT")
        digest, size, content = hashlib.sha256(), 0, []
        while True:
            need(time.monotonic() <= deadline, "INVENTORY_TIME_LIMIT")
            chunk = handle.read(min(1024 * 1024, limit + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            need(size <= limit, "INVENTORY_FILE_SIZE_LIMIT")
            digest.update(chunk)
            if parse:
                content.append(chunk)
        after = os.fstat(handle.fileno())
        need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
             == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
             and size == before.st_size, "INVENTORY_FILE_CHANGED")
    metadata = {"sha256": digest.hexdigest(), "bytes": size, "uid": before.st_uid,
                "gid": before.st_gid, "mode": oct(stat.S_IMODE(before.st_mode))}
    if parse:
        value = json.loads(b"".join(content))
        need(isinstance(value, dict), "INVENTORY_JSON_NOT_OBJECT")
        return metadata, value
    return metadata


def inventory_entries(path, limit):
    fd = inventory_directory(path)
    try:
        entries = []
        with os.scandir(fd) as iterator:
            for entry in iterator:
                need(len(entries) < limit, "INVENTORY_DIRECTORY_ENTRY_LIMIT")
                entries.append(entry.name)
        return sorted(entries)
    finally:
        os.close(fd)


def inventory_container():
    template = ('{"id":{{json .Id}},"image":{{json .Config.Image}},"image_id":{{json .Image}},'
                '"user":{{json .Config.User}},"command":{{json .Config.Cmd}},'
                '"entrypoint":{{json .Config.Entrypoint}},"mounts":{{json .Mounts}},'
                '"readonly":{{json .HostConfig.ReadonlyRootfs}},"caps":{{json .HostConfig.CapDrop}},'
                '"cap_add":{{json .HostConfig.CapAdd}},"security_opt":{{json .HostConfig.SecurityOpt}},'
                '"memory":{{json .HostConfig.Memory}},"nano_cpus":{{json .HostConfig.NanoCpus}},'
                '"pids_limit":{{json .HostConfig.PidsLimit}},'
                '"privileged":{{json .HostConfig.Privileged}},"restart":{{json .HostConfig.RestartPolicy.Name}},'
                '"running":{{json .State.Running}},"exit_code":{{json .State.ExitCode}},'
                '"oom_killed":{{json .State.OOMKilled}},"restarts":{{json .RestartCount}}}')
    checked = command(["docker", "inspect", "--format", template, NAME])
    need(checked.returncode == 0 and len(checked.stdout) <= 1024 * 1024, "INVENTORY_CONTAINER_UNAVAILABLE")
    item = json.loads(checked.stdout)
    need(isinstance(item, dict), "INVENTORY_CONTAINER_INVALID")
    return item


def inventory_image_platform(image):
    checked = command(["docker", "image", "inspect", "--format",
                       '{"os":{{json .Os}},"architecture":{{json .Architecture}}}', image])
    need(checked.returncode == 0 and len(checked.stdout) <= 4096, "INVENTORY_IMAGE_PLATFORM_UNAVAILABLE")
    platform = json.loads(checked.stdout)
    need(isinstance(platform, dict) and set(platform) == {"os", "architecture"}
         and all(isinstance(v, str) and re.fullmatch(r"[a-z0-9_]{1,24}", v) for v in platform.values()),
         "INVENTORY_IMAGE_PLATFORM_INVALID")
    return platform


def inventory(batch, image):
    """One frozen-batch diagnostic inventory; not replay, recovery or admission."""
    need(batch == ROOT / STARTUP_REPAIR_BATCH, "INVENTORY_BATCH_IDENTITY")
    deadline = time.monotonic() + 150
    anomalies, identity_issues = [], []

    def issue(scope, code, *, identity=False):
        anomalies.append({"scope": scope, "code": code})
        if identity:
            identity_issues.append(code)

    def read(relative, limit=1024 * 1024, *, parse=False, identity=False):
        try:
            return inventory_file(batch / relative, limit, deadline, parse=parse)
        except (OSError, ValueError) as error:
            code = str(error) if isinstance(error, ValueError) and re.fullmatch(r"[A-Z_]+", str(error)) else type(error).__name__
            issue(relative, code, identity=identity)
            return (None, {}) if parse else None

    # A parent symlink is rejected even if the final leaf itself is not a link.
    fd = inventory_directory(batch)
    os.close(fd)
    before = inventory_container()
    platform = inventory_image_platform(INVENTORY_IMAGE)
    expected_mounts = {(str(batch / "code"), "/forward", False),
                       (str(batch / "capture"), "/evidence", True)}
    mounts = before.get("mounts", [])
    binds = {(m.get("Source"), m.get("Destination"), m.get("RW")) for m in mounts if m.get("Type") == "bind"}
    mount_identity = binds == expected_mounts and len([m for m in mounts if m.get("Type") == "bind"]) == 2 \
        and all(m.get("Type") == "bind" or (m.get("Type") == "tmpfs" and m.get("Destination") == "/tmp") for m in mounts)
    container_identity = (image == INVENTORY_IMAGE and before.get("image") == INVENTORY_IMAGE
                          and before.get("image_id") == INVENTORY_IMAGE.split("@", 1)[1]
                          and before.get("user") == "65534:65534" and mount_identity
                          and before.get("readonly") is True and before.get("privileged") is False
                          and before.get("caps") == ["ALL"] and before.get("restart") == "no"
                          and before.get("cap_add") in (None, [])
                          and before.get("security_opt") in (["no-new-privileges"], ["no-new-privileges:true"])
                          and before.get("memory") == 512 * 1024**2
                          and before.get("nano_cpus") == 500_000_000 and before.get("pids_limit") == 64
                          and before.get("entrypoint") == ["python3"]
                          and before.get("command") == ["/forward/forward_evidence.py", "run", "--root=/evidence"])
    if not container_identity:
        issue("container", "CONTAINER_IDENTITY_MISMATCH", identity=True)
    if before.get("running") is not False:
        issue("container", "CONTAINER_NOT_TERMINAL")
    if before.get("exit_code") != 0 or before.get("oom_killed") is not False or before.get("restarts") != 0:
        issue("container", "CONTAINER_EXIT_OR_RESTART_ANOMALY")

    source = {}
    for name, expected in sorted(STARTUP_SOURCE_HASHES.items()):
        found = read("code/" + name, 256 * 1024, identity=True)
        source[name] = found
        if found and found["sha256"] != expected:
            issue("code/" + name, "SOURCE_HASH_MISMATCH", identity=True)
    started_meta, started = read("capture/started.json", parse=True, identity=True)
    health_meta, health = read("capture/health.json", parse=True)
    if (started.get("schema_version") != "forward_receive_evidence_v1"
            or started.get("source_sha256") != STARTUP_SOURCE_HASHES["forward_evidence.py"]
            or started.get("book_source_sha256") != STARTUP_SOURCE_HASHES["collect_bybit_microstructure.py"]):
        issue("started", "STARTED_SOURCE_IDENTITY_MISMATCH", identity=True)
    if (started.get("maximum_seconds") != 86400 or started.get("maximum_bytes") != INVENTORY_MAX_BYTES
            or started.get("maximum_segments") != INVENTORY_MAX_SEGMENTS
            or started.get("research_domain") != "prospective_development"
            or any(started.get(k) is not False for k in ("economic_evidence", "account_access", "order_submission"))):
        issue("started", "STARTED_SCOPE_MISMATCH", identity=True)
    completed = health.get("completed_segments")
    if type(completed) is not int or not 0 <= completed <= INVENTORY_MAX_SEGMENTS:
        issue("health", "INVALID_COMPLETED_COUNT")
        completed = None
    health_status = health.get("status")
    if health_status not in ("BOUNDED_COLLECTION_COMPLETE", "STOPPED_ERROR"):
        issue("health", "HEALTH_NOT_TERMINAL")
    entries = inventory_entries(batch / "capture", INVENTORY_MAX_SEGMENTS + 8)
    indices = []
    for name in entries:
        if re.fullmatch(r"segment-[0-9]{3}", name) and int(name[-3:]) < INVENTORY_MAX_SEGMENTS:
            indices.append(int(name[-3:]))
        elif name not in ("started.json", "health.json", "failure.json", "health.json.tmp"):
            issue("capture", "UNEXPECTED_ENTRY_" + hashlib.sha256(name.encode()).hexdigest()[:16])
    if "health.json.tmp" in entries:
        issue("capture", "UNCOMMITTED_HEALTH_PRESENT")
    failure_meta, failure = (read("capture/failure.json", parse=True) if "failure.json" in entries else (None, {}))
    if "failure.json" in entries or health_status == "STOPPED_ERROR":
        issue("capture", "CAPTURE_REPORTED_FAILURE")
    stop_reason = failure.get("reason_code", health.get("reason_code"))
    if not isinstance(stop_reason, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,100}", stop_reason):
        stop_reason = "EXACT_BOUND_NOT_RECORDED" if health_status == "BOUNDED_COLLECTION_COMPLETE" else "UNKNOWN"

    segments, missing, total, hashed = [], [], 0, 0
    previous_receipt, previous_last = None, None
    for index in range(max(indices + ([completed - 1] if completed else []) + [-1]) + 1):
        need(time.monotonic() <= deadline, "INVENTORY_TIME_LIMIT")
        relative = "capture/segment-%03d" % index
        record = {"segment_index": index, "present": index in indices}
        if index not in indices:
            missing.append(index)
            issue(relative, "SEGMENT_MISSING")
            previous_receipt, previous_last = None, None
            segments.append(record)
            continue
        try:
            children = inventory_entries(batch / relative, 8)
        except (OSError, ValueError):
            issue(relative, "SEGMENT_DIRECTORY_UNREADABLE_OR_UNSAFE", identity=True)
            previous_receipt, previous_last = None, None
            segments.append(record)
            continue
        record["partial_present"] = "receive.partial.jsonl.gz" in children
        for name in children:
            if name not in ("receipt.json", "receive.jsonl.gz", "receive.partial.jsonl.gz"):
                issue(relative, "UNEXPECTED_SEGMENT_ENTRY_" + hashlib.sha256(name.encode()).hexdigest()[:16])
        if record["partial_present"]:
            issue(relative, "PARTIAL_RAW_PRESENT")
        if completed is not None and index >= completed:
            issue(relative, "SEGMENT_BEYOND_REPORTED_SEALED_COUNT")
        receipt_meta, receipt = read(relative + "/receipt.json", parse=True)
        record["receipt"] = receipt_meta
        raw = None
        for filename in ("receive.jsonl.gz", "receive.partial.jsonl.gz"):
            if filename not in children:
                if filename == "receive.jsonl.gz":
                    issue(relative, "SEALED_RAW_MISSING")
                continue
            found = read(relative + "/" + filename, min(129 * 1024**2, INVENTORY_MAX_BYTES - hashed), identity=True)
            if found:
                hashed += found["bytes"]
                if found["uid"] != 65534 or found["gid"] != 65534:
                    issue(relative, "RAW_OWNER_MISMATCH", identity=True)
            if filename == "receive.jsonl.gz":
                raw = found
                total += found["bytes"] if found else 0
            else:
                record["partial_raw"] = found
        record["raw"] = raw
        if (receipt.get("schema_version") != "forward_receive_evidence_v1"
                or receipt.get("status") != "SEALED" or type(receipt.get("segment_index")) is not int
                or receipt.get("segment_index") != index
                or receipt.get("economic_evidence") is not False):
            issue(relative, "RECEIPT_SCHEMA_OR_INDEX_MISMATCH")
        if receipt.get("source_sha256") != STARTUP_SOURCE_HASHES["forward_evidence.py"]:
            issue(relative, "RECEIPT_SOURCE_MISMATCH", identity=True)
        if receipt.get("previous_receipt_sha256") != previous_receipt:
            issue(relative, "PREVIOUS_RECEIPT_HASH_MISMATCH")
        record["previous_receipt_sha256"] = receipt.get("previous_receipt_sha256") \
            if isinstance(receipt.get("previous_receipt_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", receipt["previous_receipt_sha256"]) else None
        if raw and (receipt.get("raw_sha256") != raw["sha256"] or type(receipt.get("raw_bytes")) is not int
                    or receipt["raw_bytes"] != raw["bytes"]):
            issue(relative, "RAW_HASH_OR_SIZE_MISMATCH")
        if receipt_meta and (receipt_meta["uid"] != 65534 or receipt_meta["gid"] != 65534):
            issue(relative, "RECEIPT_OWNER_MISMATCH", identity=True)
        observation = receipt.get("observation", {})
        observation = observation if isinstance(observation, dict) else {}
        summary = {}
        for key in ("messages", "first_receive_epoch_ms", "last_receive_epoch_ms", "elapsed_monotonic_ms"):
            value = observation.get(key)
            if type(value) is int and value >= 0:
                summary[key] = value
            else:
                issue(relative, "OBSERVATION_FIELD_INVALID_" + key.upper())
        counts = observation.get("topic_counts")
        if isinstance(counts, dict) and set(counts) == set(INVENTORY_TOPICS) and all(type(v) is int and v > 0 for v in counts.values()):
            summary["topic_counts"] = counts
            if sum(counts.values()) != summary.get("messages"):
                issue(relative, "TOPIC_TOTAL_MISMATCH")
        else:
            issue(relative, "TOPIC_COUNTS_INVALID")
        replay_hash = observation.get("replay_sha256")
        if isinstance(replay_hash, str) and re.fullmatch(r"[0-9a-f]{64}", replay_hash):
            summary["claimed_replay_sha256"] = replay_hash
        else:
            issue(relative, "CLAIMED_REPLAY_HASH_INVALID")
        first, last = summary.get("first_receive_epoch_ms"), summary.get("last_receive_epoch_ms")
        if first is not None and last is not None:
            if first > last:
                issue(relative, "RECEIPT_TIME_RANGE_REVERSED")
            if previous_last is not None:
                record["claimed_gap_from_previous_receive_ms"] = first - previous_last
                if first < previous_last:
                    issue(relative, "CROSS_SEGMENT_TIME_OVERLAP")
            previous_last = last
        else:
            previous_last = None
        record["claimed_observation"] = summary
        previous_receipt = receipt_meta["sha256"] if receipt_meta else None
        segments.append(record)
    if completed != len(indices) or health.get("raw_bytes") != total:
        issue("health", "REPORTED_TOTALS_MISMATCH")
    health_after, _ = read("capture/health.json", parse=True)
    if health_after != health_meta or inventory_container() != before:
        issue("snapshot", "STATE_CHANGED_DURING_INVENTORY", identity=True)
    snapshot = {"schema_version": "forward_inventory_diagnostic_v1", "status": "DIAGNOSTIC_NOT_ADMITTED",
                "capture_commit_sha": STARTUP_REPAIR_BATCH, "observed_epoch_ms": time.time_ns() // 1_000_000,
                "identity_bound": not identity_issues, "source_files": source,
                "container": {"identity_matches": container_identity, "mounts_match": mount_identity,
                              "id": before.get("id") if isinstance(before.get("id"), str) and re.fullmatch(r"[0-9a-f]{64}", before["id"]) else None,
                              "running": before.get("running"), "exit_code": before.get("exit_code"),
                              "restarts": before.get("restarts"), "expected_user": "65534:65534",
                              "observed_user": before.get("user") if isinstance(before.get("user"), str) and re.fullmatch(r"[0-9]{1,10}:[0-9]{1,10}", before["user"]) else None,
                              "expected_image": INVENTORY_IMAGE},
                "image_platform": platform,
                "started_file": started_meta, "health_file": health_meta, "failure_file": failure_meta,
                "reported_started_epoch_ms": started.get("started_epoch_ms") if type(started.get("started_epoch_ms")) is int else None,
                "reported_terminal_epoch_ms": health.get("updated_epoch_ms") if type(health.get("updated_epoch_ms")) is int else None,
                "reported_health_status": health_status if health_status in ("BOUNDED_COLLECTION_COMPLETE", "STOPPED_ERROR", "CAPTURING", "SEGMENT_SEALED") else "UNKNOWN",
                "reported_stop_reason": stop_reason, "reported_completed_segments": completed,
                "observed_segment_directories": len(indices), "missing_segments": missing,
                "raw_bytes_hashed": hashed, "observed_final_raw_bytes": total, "segments": segments,
                "anomalies": anomalies, "raw_replay_performed": False, "backup_restored": False,
                "continuous_coverage_verified": False, "economic_evidence": False, "research_admitted": False}
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"snapshot": snapshot, "snapshot_sha256": hashlib.sha256(encoded).hexdigest()}


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


def replay_spec(image, code, data):
    need(data.is_dir() and not data.is_symlink(), "REPLAY_DIRECTORY_IDENTITY")
    owner = data.stat()
    need(owner.st_uid > 0, "REPLAY_OWNER_MUST_BE_UNPRIVILEGED")
    for name in ("receipt.json", "receive.jsonl.gz"):
        path = data / name
        need(path.is_file() and not path.is_symlink() and path.stat().st_uid == owner.st_uid
             and path.stat().st_gid == owner.st_gid, "REPLAY_FILE_OWNER_MISMATCH")
    return ["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=512m", "--cpus=0.5",
            "--user", "%d:%d" % (owner.st_uid, owner.st_gid),
            "--mount", "type=bind,source=%s,target=/forward,readonly" % code,
            "--mount", "type=bind,source=%s,target=/evidence,readonly" % data,
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python3", image,
            "/forward/forward_evidence.py", "verify", "--root=/evidence"]


def diagnose(batch, image=None):
    """Bounded metadata only; no market rows, account data or raw logs exported."""
    state = command(["docker", "inspect", "--format",
                     '{{json .State}}', NAME])
    need(state.returncode == 0, "DIAGNOSTIC_CONTAINER_MISSING")
    item = json.loads(state.stdout)
    logs = command(["docker", "logs", "--tail", "20", NAME])
    content = logs.stdout + logs.stderr
    paths = {}
    for relative in ("code", "code/forward_evidence.py", "capture", "capture/started.json", "capture/health.json",
                     "capture/segment-000", "capture/segment-000/receipt.json", "capture/segment-000/receive.jsonl.gz"):
        path = batch / relative
        if path.exists():
            stat = path.stat()
            paths[relative] = {"mode": oct(stat.st_mode & 0o777), "uid": stat.st_uid,
                               "gid": stat.st_gid, "bytes": stat.st_size}
        else:
            paths[relative] = {"present": False}
    user = command(["docker", "inspect", "--format", '{{json .Config.User}}', NAME])
    result = {"running": item.get("Running"), "exit_code": item.get("ExitCode"),
            "oom_killed": item.get("OOMKilled"), "container_user": json.loads(user.stdout),
            "permission_denied_starting_script": b"can't open file '/forward/forward_evidence.py'" in content
            and b"Permission denied" in content, "log_sha256": hashlib.sha256(content).hexdigest(),
            "paths": paths}
    segment = batch / "capture/segment-000"
    if image and segment.is_dir():
        # Access-only diagnosis. Does not execute the failed replay/checker.
        script = ("import os,json; print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),"
                  "'receipt_readable':os.access('/evidence/receipt.json',os.R_OK),"
                  "'raw_readable':os.access('/evidence/receive.jsonl.gz',os.R_OK)}))")
        base = ["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--memory=128m", "--pids-limit=32", "--cpus=0.5",
                "--mount", "type=bind,source=%s,target=/evidence,readonly" % segment,
                "--entrypoint", "python3"]
        probes = {}
        for label, args in (("original_default_user", []), ("capture_owner", ["--user", "65534:65534"])):
            checked = command(base + args + [image, "-c", script])
            need(checked.returncode == 0, "REPLAY_ACCESS_DIAGNOSIS_FAILED")
            probes[label] = json.loads(checked.stdout)
        result["replay_access_only"] = probes
    return result


def readable_code_directory(path):
    path.mkdir(mode=0o755)
    # mkdir's mode is masked by the caller's 077 umask; code is public/read-only.
    path.chmod(0o755)


def repair_startup(batch, image):
    """One exact pre-capture permission correction; never resume failed market data."""
    need(batch == ROOT / STARTUP_REPAIR_BATCH, "REPAIR_BATCH_IDENTITY")
    for path in (batch, batch / "code", batch / "capture"):
        need(path.is_dir() and not path.is_symlink(), "REPAIR_PATH_IDENTITY")
    before = diagnose(batch)
    need(before["running"] is False and before["exit_code"] == 2
         and before["permission_denied_starting_script"] and before["container_user"] == "65534:65534",
         "REPAIR_NOT_CONFIRMED_STARTUP_FAILURE")
    need(not list((batch / "capture").iterdir()), "REPAIR_CAPTURE_ALREADY_STARTED")
    need(before["paths"]["code"]["mode"] == "0o700" and before["paths"]["code"]["uid"] == 0,
         "REPAIR_PERMISSION_MISMATCH")
    for name, expected in STARTUP_SOURCE_HASHES.items():
        need(not (batch / "code" / name).is_symlink() and sha(batch / "code" / name) == expected,
             "REPAIR_SOURCE_CHANGED")
    config = command(["docker", "inspect", NAME])
    need(config.returncode == 0, "REPAIR_CONTAINER_MISSING")
    item = json.loads(config.stdout)[0]
    mounts = {(m["Source"], m["Destination"], m["RW"]) for m in item["Mounts"] if m["Type"] == "bind"}
    need(item["Config"]["Image"] == image and item["HostConfig"]["ReadonlyRootfs"]
         and mounts == {(str(batch / "code"), "/forward", False), (str(batch / "capture"), "/evidence", True)},
         "REPAIR_CONTAINER_IDENTITY")
    marker = batch / "permission-repair.json"
    with marker.open("x") as handle:
        json.dump({"before": before, "source_sha256": STARTUP_SOURCE_HASHES,
                   "started_epoch_ms": time.time_ns() // 1_000_000}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    (batch / "code").chmod(0o755)
    checked = command(["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--user", "65534:65534",
                       "--memory=128m", "--pids-limit=32", "--cpus=0.5",
                       "--mount", "type=bind,source=%s,target=/forward,readonly" % (batch / "code"),
                       "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python3", image,
                       "-c", "import sys; sys.path.insert(0,'/forward'); import forward_evidence; import websockets"], 30)
    need(checked.returncode == 0, "REPAIR_IMPORT_PREFLIGHT_FAILED")
    need(command(["docker", "start", NAME]).returncode == 0, "REPAIR_CONTAINER_START_FAILED")
    return {"status": "PERMISSION_CORRECTED_AWAITING_ORIGINAL_VERIFY", "source_unchanged": True,
            "network_disabled_import_passed": True, "code_mode": oct((batch / "code").stat().st_mode & 0o777)}


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
        readable_code_directory(code)
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
    elif mode == "repair-startup":
        result["repair"] = repair_startup(batch, image)
        result["status"] = "PERMISSION_CORRECTED_NOT_ACCEPTED"
    elif mode == "inventory":
        result["inventory"] = inventory(batch, image)
        result["status"] = "INVENTORY_DIAGNOSTIC_ONLY"
    elif mode in ("inspect", "verify", "diagnose"):
        result["status"] = "INSPECTED"
        health = batch / "capture/health.json"
        if health.is_file():
            result["capture"] = read_json(health)
        if mode == "diagnose":
            result["diagnostic"] = diagnose(batch, image)
        if mode == "verify":
            need(health.is_file(), "CAPTURE_HEALTH_MISSING")
            need(result["capture"].get("status") in ("CAPTURING", "SEGMENT_SEALED", "BOUNDED_COLLECTION_COMPLETE"), "CAPTURE_STOPPED_ERROR")
            need(result["capture"].get("completed_segments", 0) > 0, "NO_SEALED_SEGMENT")
            code, data = batch / "code", batch / "capture/segment-000"
            argv = replay_spec(image, code, data)
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
    parser.add_argument("--mode", required=True, choices=("inspect", "start", "verify", "diagnose", "repair-startup", "inventory"))
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
