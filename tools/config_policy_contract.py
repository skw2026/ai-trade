#!/usr/bin/env python3
"""Canonical identity for the policy fields shared by replay and live."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


POLICY_SECTIONS = {
    "system",
    "exchange",
    "risk",
    "execution",
    "strategy",
    "integrator",
    "self_evolution",
    "regime",
    "universe",
}

IGNORED_PATHS = {
    # Process mode and state storage are operational isolation controls, not
    # trading-policy inputs. Replay must use a fresh data_path per segment.
    "system.mode",
    "system.data_path",
    "integrator.shadow.model_path",
    "integrator.shadow.model_report_path",
    "integrator.shadow.active_meta_path",
    "integrator.shadow.require_model_file",
    "integrator.shadow.require_active_meta",
    "integrator.shadow.require_gate_pass",
    "integrator.shadow.candidate_validation_mode",
    "integrator.shadow.source_runtime_config_sha256",
}


def strip_inline_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote:
            escaped = True
            continue
        if char in {'"', "'"}:
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            continue
        if char == "#" and not quote:
            return value[:index].rstrip()
    return value.strip()


def normalize_scalar(value: str) -> Any:
    value = strip_inline_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [normalize_scalar(item) for item in inner.split(",")]
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value
    if not number.is_finite():
        return value
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def flatten_config(config_text: str) -> dict[str, Any]:
    stack: list[tuple[int, str]] = []
    flattened: dict[str, Any] = {}
    key_pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s*(.*))?$")

    for line_number, raw_line in enumerate(config_text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ValueError(f"YAML 缩进不能使用 tab: line={line_number}")
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        match = key_pattern.match(raw_line.strip())
        if match is None:
            if raw_line.strip().startswith("- "):
                raise ValueError(
                    f"策略合同暂不接受 block list，请改为 inline list: line={line_number}"
                )
            continue
        key = match.group(1)
        raw_value = (match.group(2) or "").strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path_parts = [item[1] for item in stack] + [key]
        path = ".".join(path_parts)
        if raw_value:
            flattened[path] = normalize_scalar(raw_value)
        else:
            stack.append((indent, key))
    return dict(sorted(flattened.items()))


def extract_policy(config_text: str) -> dict[str, Any]:
    all_values = flatten_config(config_text)
    flattened = {
        path: value
        for path, value in all_values.items()
        if path.split(".", 1)[0] in POLICY_SECTIONS
        and path not in IGNORED_PATHS
    }

    missing = sorted(section for section in POLICY_SECTIONS if not any(
        path == section or path.startswith(section + ".")
        for path in flattened
    ))
    if missing:
        raise ValueError("策略合同缺少顶层 section: " + ",".join(missing))
    return dict(sorted(flattened.items()))


def config_value(config_path: Path, path: str) -> Any:
    values = flatten_config(config_path.read_text(encoding="utf-8"))
    if path not in values:
        raise ValueError(f"配置缺少合同字段: {path}")
    return values[path]


def policy_payload(config_path: Path) -> dict[str, Any]:
    policy = extract_policy(config_path.read_text(encoding="utf-8"))
    canonical_json = json.dumps(
        policy,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": "execution_policy_v2",
        "policy": policy,
        "sha256": hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    }


def policy_sha256(config_path: Path) -> str:
    return str(policy_payload(config_path)["sha256"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    payload = policy_payload(Path(args.config))
    if args.json_out:
        output = Path(args.json_out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    print(payload["sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
