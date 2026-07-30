#!/usr/bin/env python3

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath


CONTENT_MANIFEST_NAME = ".release-content.sha256"
RELEASE_MANIFEST_NAME = "release_manifest.json"
CONTROL_FILES = {CONTENT_MANIFEST_NAME, RELEASE_MANIFEST_NAME}
SHA256_LINE_RE = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")


@dataclasses.dataclass
class IntegrityResult:
    manifest_failures: list[str] = dataclasses.field(default_factory=list)
    missing: list[str] = dataclasses.field(default_factory=list)
    unexpected: list[str] = dataclasses.field(default_factory=list)
    hash_mismatch: list[str] = dataclasses.field(default_factory=list)
    symlinks: list[str] = dataclasses.field(default_factory=list)
    listed_count: int = 0

    @property
    def valid(self) -> bool:
        return not any(
            (
                self.manifest_failures,
                self.missing,
                self.unexpected,
                self.hash_mismatch,
                self.symlinks,
            )
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_manifest_path(raw_path: str) -> str:
    relative = raw_path.removeprefix("./")
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or path.as_posix() in {"", "."}
        or ".." in path.parts
        or "\\" in relative
    ):
        raise ValueError(f"unsafe path: {raw_path!r}")
    return path.as_posix()


def load_content_manifest(path: Path) -> tuple[dict[str, str], list[str]]:
    entries: dict[str, str] = {}
    failures: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {}, [f"content_manifest.read={exc}"]

    for line_number, raw in enumerate(lines, start=1):
        match = SHA256_LINE_RE.fullmatch(raw)
        if match is None:
            failures.append(f"content_manifest.entry[{line_number}]")
            continue
        digest, raw_relative = match.groups()
        try:
            relative = normalize_manifest_path(raw_relative)
        except ValueError:
            failures.append(f"content_manifest.path[{line_number}]")
            continue
        if relative in CONTROL_FILES:
            failures.append(f"content_manifest.control_file[{relative}]")
            continue
        if relative in entries:
            failures.append(f"content_manifest.duplicate[{relative}]")
            continue
        entries[relative] = digest
    return entries, failures


def collect_release_tree(root: Path) -> tuple[set[str], list[str]]:
    files: set[str] = set()
    symlinks: list[str] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            path = current_path / name
            if path.is_symlink():
                symlinks.append(path.relative_to(root).as_posix())
                directories.remove(name)
        for name in filenames:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                symlinks.append(relative)
            elif relative not in CONTROL_FILES:
                files.add(relative)
    return files, sorted(symlinks)


def validate_release(root: Path) -> IntegrityResult:
    result = IntegrityResult()
    if not root.is_dir():
        result.manifest_failures.append("release_dir.missing")
        return result

    release_manifest_path = root / RELEASE_MANIFEST_NAME
    content_manifest_path = root / CONTENT_MANIFEST_NAME
    try:
        release_manifest = json.loads(
            release_manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        result.manifest_failures.append(f"release_manifest.read={exc}")
        return result

    content_metadata = release_manifest.get("content_manifest")
    if not isinstance(content_metadata, dict):
        result.manifest_failures.append("content_manifest.metadata")
        return result
    if content_metadata.get("path") != CONTENT_MANIFEST_NAME:
        result.manifest_failures.append("content_manifest.path")

    try:
        actual_content_digest = sha256_file(content_manifest_path)
    except OSError as exc:
        result.manifest_failures.append(f"content_manifest.read={exc}")
        return result
    if content_metadata.get("sha256") != actual_content_digest:
        result.manifest_failures.append("content_manifest.sha256")

    entries, entry_failures = load_content_manifest(content_manifest_path)
    result.manifest_failures.extend(entry_failures)
    result.listed_count = len(entries)

    actual, result.symlinks = collect_release_tree(root)
    listed = set(entries)
    result.missing = sorted(listed - actual)
    result.unexpected = sorted(actual - listed)

    for relative in sorted(listed & actual):
        try:
            actual_digest = sha256_file(root / relative)
        except OSError as exc:
            result.manifest_failures.append(f"content.read[{relative}]={exc}")
            continue
        if actual_digest != entries[relative]:
            result.hash_mismatch.append(relative)
    return result


def is_runtime_contamination(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.parts and path.parts[0] == "data":
        return True
    return (
        "__pycache__" in path.parts
        and path.suffix.lower() in {".pyc", ".pyo"}
    )


def remove_empty_contamination_dirs(root: Path) -> None:
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        relative = directory.relative_to(root).as_posix()
        if relative == "data":
            continue
        try:
            directory.rmdir()
        except OSError:
            pass


def quarantine_runtime_contamination(
    root: Path,
    quarantine_root: Path,
    unexpected: list[str],
) -> Path:
    try:
        quarantine_root.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("quarantine root must be outside release directory")

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = quarantine_root / f"{root.name}-{timestamp}-{os.getpid()}"
    for relative in unexpected:
        source = root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    remove_empty_contamination_dirs(root)
    return destination


def format_result(result: IntegrityResult) -> str:
    fields = []
    for name in (
        "manifest_failures",
        "missing",
        "unexpected",
        "hash_mismatch",
        "symlinks",
    ):
        values = getattr(result, name)
        if values:
            fields.append(f"{name}={json.dumps(values, ensure_ascii=True)}")
    return " ".join(fields) if fields else "no_failures"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate an immutable release tree against its signed file set."
    )
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--repair-runtime-contamination", action="store_true")
    parser.add_argument("--quarantine-root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.release_dir.resolve()
    result = validate_release(root)
    quarantined: Path | None = None

    if not result.valid and args.repair_runtime_contamination:
        repairable = (
            bool(result.unexpected)
            and not result.manifest_failures
            and not result.missing
            and not result.hash_mismatch
            and not result.symlinks
            and all(is_runtime_contamination(path) for path in result.unexpected)
        )
        if repairable:
            if args.quarantine_root is None:
                print(
                    "RELEASE_TREE_INTEGRITY_INVALID: "
                    "--quarantine-root is required for repair",
                    file=sys.stderr,
                )
                return 1
            try:
                quarantined = quarantine_runtime_contamination(
                    root,
                    args.quarantine_root.resolve(),
                    result.unexpected,
                )
            except (OSError, ValueError) as exc:
                print(
                    f"RELEASE_TREE_INTEGRITY_INVALID: quarantine_failed={exc}",
                    file=sys.stderr,
                )
                return 1
            result = validate_release(root)

    if not result.valid:
        print(
            f"RELEASE_TREE_INTEGRITY_INVALID: {format_result(result)}",
            file=sys.stderr,
        )
        return 1

    quarantine_detail = str(quarantined) if quarantined else "none"
    print(
        "RELEASE_TREE_INTEGRITY_OK: "
        f"release={root} files={result.listed_count} quarantine={quarantine_detail}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
