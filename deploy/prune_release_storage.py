#!/usr/bin/env python3
"""Safely prune stale deployment-control artifacts on the release host."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
from pathlib import Path


SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def direct_child(root: Path, candidate: Path) -> bool:
    try:
        return candidate.parent.resolve() == root.resolve()
    except OSError:
        return False


def make_tree_owner_writable(path: Path) -> None:
    for current_root, directories, files in os.walk(path, topdown=False):
        for name in (*directories, *files):
            child = Path(current_root) / name
            if child.is_symlink():
                continue
            try:
                child.chmod(child.stat().st_mode | stat.S_IWUSR)
            except FileNotFoundError:
                pass
        current = Path(current_root)
        try:
            current.chmod(current.stat().st_mode | stat.S_IWUSR)
        except FileNotFoundError:
            pass


def remove_direct_child(root: Path, target: Path, dry_run: bool) -> None:
    if not direct_child(root, target):
        raise RuntimeError(f"refusing to remove path outside direct root: {target}")
    print(f"[deploy-gc] {'would remove' if dry_run else 'remove'}: {target}")
    if dry_run:
        return
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
        return
    make_tree_owner_writable(target)
    shutil.rmtree(target)


def child_directories(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        entry
        for entry in root.iterdir()
        if entry.is_dir() and not entry.is_symlink()
    ]


def prune_staging_root(
    root: Path,
    active_name: str,
    dry_run: bool,
) -> None:
    for entry in child_directories(root):
        if active_name and entry.name == active_name:
            print(f"[deploy-gc] protect active staging path: {entry}")
            continue
        remove_direct_child(root, entry, dry_run)


def protected_release_paths(
    releases_root: Path,
    current_link: Path,
    target_release: Path,
    previous_release: Path,
) -> set[Path]:
    protected: set[Path] = set()
    for label, candidate in (
        ("current", current_link),
        ("target", target_release),
        ("previous", previous_release),
    ):
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"cannot resolve protected {label} release: {exc}") from exc
        if not direct_child(releases_root, resolved):
            raise RuntimeError(
                f"protected {label} release is outside release root: {resolved}"
            )
        protected.add(resolved)
        print(f"[deploy-gc] protect {label} release: {resolved}")
    return protected


def prune_releases(
    releases_root: Path,
    protected: set[Path],
    keep_count: int,
    dry_run: bool,
) -> None:
    releases = sorted(
        child_directories(releases_root),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    keep_slots = max(0, keep_count - len(protected))
    kept_unprotected = 0
    for release in releases:
        resolved = release.resolve()
        if resolved in protected:
            continue
        if kept_unprotected < keep_slots:
            kept_unprotected += 1
            print(f"[deploy-gc] retain rollback release: {release}")
            continue
        remove_direct_child(releases_root, release, dry_run)


def prune_runtime_compose(root: Path, keep_count: int, dry_run: bool) -> None:
    if not root.is_dir():
        return
    files = sorted(
        [entry for entry in root.iterdir() if entry.is_file() and not entry.is_symlink()],
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for stale in files[keep_count:]:
        remove_direct_child(root, stale, dry_run)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--target-release", required=True, type=Path)
    parser.add_argument("--current-link", required=True, type=Path)
    parser.add_argument("--previous-release", required=True, type=Path)
    parser.add_argument("--active-release-id", default="")
    parser.add_argument("--keep-releases", type=int, default=3)
    parser.add_argument("--keep-runtime-compose", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.keep_releases < 2:
        raise SystemExit("--keep-releases must be at least 2")
    if args.keep_runtime_compose < 0:
        raise SystemExit("--keep-runtime-compose must be non-negative")
    if args.active_release_id and not SAFE_NAME_RE.fullmatch(args.active_release_id):
        raise SystemExit("--active-release-id contains unsafe characters")

    release_root = args.release_root.resolve(strict=True)
    releases_root = release_root / "releases"
    releases_root.mkdir(parents=True, exist_ok=True)
    target_release = args.target_release.resolve(strict=True)
    if not direct_child(releases_root, target_release):
        raise SystemExit(f"target release is outside immutable release root: {target_release}")

    protected = protected_release_paths(
        releases_root,
        args.current_link,
        target_release,
        args.previous_release,
    )
    prune_staging_root(
        release_root / "incoming",
        args.active_release_id,
        args.dry_run,
    )
    prune_staging_root(
        release_root / ".release-unpack",
        args.active_release_id,
        args.dry_run,
    )
    prune_releases(
        releases_root,
        protected,
        args.keep_releases,
        args.dry_run,
    )
    prune_runtime_compose(
        release_root / "data" / "deploy-runtime-compose",
        args.keep_runtime_compose,
        args.dry_run,
    )
    print("[deploy-gc] release storage cleanup completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
