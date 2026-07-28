#!/usr/bin/env python3

import argparse
from pathlib import Path


PROJECT_MOUNT = "      - ${AI_TRADE_PROJECT_DIR:-.}:/opt/ai-trade"
PROJECT_MOUNT_RO = f"{PROJECT_MOUNT}:ro"
DATA_MOUNT = (
    "      - ${AI_TRADE_DATA_DIR:-/opt/ai-trade/data}:"
    "/opt/ai-trade/data"
)
ENV_MOUNT_OLD = (
    "      - ${AI_TRADE_ENV_FILE_HOST:-/opt/ai-trade/.env.runtime}:"
    "/opt/ai-trade/.env.runtime:ro"
)
ENV_MOUNT = (
    "      - ${AI_TRADE_ENV_FILE_HOST:-/opt/ai-trade/.env.runtime}:"
    "/run/ai-trade/.env.runtime:ro"
)
SCHEDULER_ENV_SOURCE = (
    "      AI_TRADE_ENV_FILE: ${AI_TRADE_ENV_FILE:-.env.runtime}"
)
SCHEDULER_ENV_RELEASE = (
    "      AI_TRADE_ENV_FILE: "
    "${AI_TRADE_ENV_FILE_CONTAINER:-/run/ai-trade/.env.runtime}"
)
SCHEDULER_IMAGE = (
    "      AI_TRADE_RESEARCH_IMAGE: "
    "${AI_TRADE_RESEARCH_IMAGE:-ai-trade-research:latest}"
)
SCHEDULER_DATA_ENV = (
    "      AI_TRADE_DATA_DIR: "
    "${AI_TRADE_DATA_DIR:-/opt/ai-trade/data}"
)
SCHEDULER_HOST_ENV = (
    "      AI_TRADE_ENV_FILE_HOST: "
    "${AI_TRADE_ENV_FILE_HOST:-/opt/ai-trade/.env.runtime}"
)


def replace_once(content: str, old: str, new: str, contract: str) -> str:
    count = content.count(old)
    if count == 0 and content.count(new) > 0:
        return content
    if count != 1:
        raise ValueError(f"{contract} changed: {old!r}, count={count}")
    return content.replace(old, new)


def replace_line_once(content: str, old: str, new: str, contract: str) -> str:
    lines = content.splitlines()
    count = lines.count(old)
    if count == 0 and new in lines:
        return content
    if count != 1:
        raise ValueError(f"{contract} changed: {old!r}, count={count}")
    return "\n".join(new if line == old else line for line in lines) + "\n"


def materialize(content: str) -> str:
    replacements = {
        "      - ${AI_TRADE_PROJECT_DIR:-.}/data:/app/data":
            "      - ${AI_TRADE_DATA_DIR:-/opt/ai-trade/data}:/app/data",
        "      - ${AI_TRADE_PROJECT_DIR:-.}/data:/opt/ai-trade/data":
            DATA_MOUNT,
        "      - ${AI_TRADE_PROJECT_DIR:-.}/config:/opt/ai-trade/config":
            "      - ${AI_TRADE_PROJECT_DIR:-.}/config:/opt/ai-trade/config:ro",
    }
    for old, new in replacements.items():
        content = replace_line_once(
            content,
            old,
            new,
            "release compose source contract",
        )

    source_mount_count = sum(
        1 for line in content.splitlines() if line == PROJECT_MOUNT
    )
    if source_mount_count:
        if source_mount_count != 2:
            raise ValueError("release compose project mount contract changed")
        content = content.replace(
            PROJECT_MOUNT,
            "\n".join((PROJECT_MOUNT_RO, DATA_MOUNT, ENV_MOUNT)),
        )
    else:
        if content.count(PROJECT_MOUNT_RO) != 2:
            raise ValueError("release compose readonly project mount contract changed")
        content = content.replace(ENV_MOUNT_OLD, ENV_MOUNT)

    content = replace_once(
        content,
        SCHEDULER_ENV_SOURCE,
        SCHEDULER_ENV_RELEASE,
        "release compose scheduler env path contract",
    )

    if content.count(SCHEDULER_DATA_ENV) == 0:
        content = replace_once(
            content,
            SCHEDULER_IMAGE,
            "\n".join(
                (SCHEDULER_IMAGE, SCHEDULER_DATA_ENV, SCHEDULER_HOST_ENV)
            ),
            "release compose scheduler environment contract",
        )

    if ENV_MOUNT_OLD in content:
        raise ValueError("release compose still contains nested env-file mount")
    if content.count(DATA_MOUNT) != 3:
        raise ValueError("release compose persistent data mount count is invalid")
    if content.count(ENV_MOUNT) != 2:
        raise ValueError("release compose env-file mount count is invalid")
    if content.count(SCHEDULER_ENV_RELEASE) != 1:
        raise ValueError("release compose scheduler env-file path is invalid")
    return content


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Materialize an immutable-release production Compose file."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output")
    parser.add_argument("--release-dir", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path
    release_dir = Path(args.release_dir)
    content = materialize(input_path.read_text(encoding="utf-8"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    # The child bind mount target must pre-exist inside the read-only parent bind.
    (release_dir / "data").mkdir(parents=True, exist_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
