#!/usr/bin/env python3
"""Seal the closed 2026-09-21 MVP evidence locally; never execute its strategy.

Fixed allowlist, exclusive output creation, no extraction/network/account APIs.
The archive contains public history, synthetic accounts and research binaries,
NOT an exchange account backup or a deployable release. Old evidence stays put.
"""
import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / ".artifacts/engineering-freeze-20260921"
META = "FREEZE-MANIFEST.json"
OLD_GATE_SHA = "bc576299059d630fcf400de46dc16b38d6c83591c1dc193433a9c32322f6910b"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stream_sha(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024*1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha(path):
    with path.open("rb") as stream:
        return stream_sha(stream)


def safe_name(name):
    path = PurePosixPath(name)
    require(name and not path.is_absolute() and ".." not in path.parts and
            "\\" not in name and path.as_posix() == name and name != ".", "UNSAFE_NAME")
    return name


def file_path(name):
    path = ROOT / safe_name(name)
    require(all(not p.is_symlink() for p in [path, *path.parents]),
            "SYMLINK_NOT_ALLOWED:"+name)
    require(path.is_file(), "MISSING_REGULAR_FILE:"+name)
    return path


def load(path):
    return json.loads(path.read_text())


def write_new(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def historical_identity():
    reference = load(ROOT/"docs/reviews/2026-09-21-mvp-reference.evidence.json")
    history = load(ROOT/"docs/reviews/2026-09-21-mvp-history-screen.evidence.json")
    expected = {}
    for group in (reference["source_sha256"], reference["build_artifact_sha256"],
                  reference["local_artifact_sha256"], history["key_artifact_sha256"],
                  load(ROOT/".artifacts/mvp-reference-history-20260921/archive-inventory.json")["files"]):
        for name, digest in group.items():
            require(name not in expected or expected[name] == digest, "CONFLICTING_OLD_IDENTITY:"+name)
            expected[name] = digest
    expected["docs/plans/2026-09-21-mvp-reference-v1.json"] = reference["contract_sha256"]
    for name in ("config/bybit.replay.mvp.yaml", "config/bybit.replay.mvp-reference.yaml",
                 "tools/collect_mvp_reference_history.py", "tools/audit_mvp_reference_stop.py"):
        expected[name] = history["file_sha256"][name]
    expected[".artifacts/validation-gate/state.json"] = OLD_GATE_SHA
    mismatches = [name for name, digest in expected.items() if sha(file_path(name)) != digest]
    require(not mismatches, "OLD_EVIDENCE_CHANGED:"+json.dumps(mismatches))
    return expected


def payload():
    expected = historical_identity()
    names = set(expected)
    # Capture complete closed directories, including failed responses and claims.
    for base in (".artifacts/mvp-reference-20260921", ".artifacts/mvp-reference-history-20260921"):
        for path in (ROOT/base).rglob("*"):
            require(not path.is_symlink(), "SYMLINK_NOT_ALLOWED")
            if path.is_file():
                names.add(path.relative_to(ROOT).as_posix())
    names.add("tools/seal_offline_evidence.py")
    records = {}
    for name in sorted(names):
        path = file_path(name)
        records[name] = {"sha256":sha(path), "size":path.stat().st_size}
    return {"schema_version":"closed_mvp_evidence_archive_v1", "files":records,
            "historical_identity_records":len(expected),
            "new_strategy_execution":False, "same_machine_copy_not_offsite_backup":True,
            "scope":"closed public history, synthetic evidence and available bound reference binaries"}


def verify(path, expected_sha):
    require(sha(path) == expected_sha, "ARCHIVE_SHA256")
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [safe_name(m.name) for m in members]
        require(len(names) == len(set(names)), "DUPLICATE_MEMBER")
        require(all(m.isfile() for m in members), "NON_REGULAR_MEMBER")
        metadata = archive.extractfile(META)
        require(metadata is not None, "MISSING_MANIFEST")
        manifest = json.load(metadata)
        require(manifest["schema_version"] == "closed_mvp_evidence_archive_v1", "MANIFEST_SCHEMA")
        expected = manifest["files"]
        require(set(names) == set(expected) | {META}, "ARCHIVE_MEMBER_SET")
        total = 0
        for member in members:
            if member.name == META:
                continue
            record = expected[member.name]
            require(member.size == record["size"], "MEMBER_SIZE:"+member.name)
            with archive.extractfile(member) as stream:
                require(stream_sha(stream) == record["sha256"], "MEMBER_SHA256:"+member.name)
            total += member.size
    return {"verified":True, "file_count":len(expected), "payload_bytes":total,
            "archive_sha256":expected_sha, "archive_bytes":path.stat().st_size,
            "extracted_or_executed":False}


def pack():
    manifest = payload()
    STAGE.mkdir(parents=True, exist_ok=True)
    archive_path = STAGE/"closed-mvp-evidence.tar.gz"
    require(not archive_path.exists() and not archive_path.is_symlink(), "ARCHIVE_ALREADY_EXISTS")
    write_new(STAGE/"evidence-payload.json", manifest)
    raw = (json.dumps(manifest, indent=2, sort_keys=True)+"\n").encode()
    with tarfile.open(archive_path, "x:gz", dereference=True) as archive:
        info = tarfile.TarInfo(META)
        info.size, info.mode = len(raw), 0o600
        archive.addfile(info, io.BytesIO(raw))
        for name in manifest["files"]:
            archive.add(file_path(name), arcname=name, recursive=False)
    result = verify(archive_path, sha(archive_path))
    # Source files must also remain unchanged while they are being sealed.
    require(payload() == manifest, "SOURCE_CHANGED_DURING_ARCHIVE")
    result.update(archive_path=str(archive_path.relative_to(ROOT)),
                  historical_identity_records=manifest["historical_identity_records"],
                  payload_manifest_sha256=sha(STAGE/"evidence-payload.json"))
    write_new(STAGE/"archive-receipt.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    commands.add_parser("pack")
    check = commands.add_parser("verify")
    check.add_argument("--archive", required=True, type=Path)
    check.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if args.command == "preflight":
        print(json.dumps({"unchanged_historical_identity_records":len(historical_identity())}))
    elif args.command == "pack":
        print(json.dumps(pack(), sort_keys=True))
    else:
        print(json.dumps(verify(args.archive,args.sha256), sort_keys=True))
