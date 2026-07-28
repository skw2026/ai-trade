#!/usr/bin/env python3
"""Derive replay candidate YAML from the exact runtime policy."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

try:
    from config_policy_contract import policy_sha256
except ModuleNotFoundError:  # pragma: no cover
    from tools.config_policy_contract import policy_sha256


SHADOW_OVERRIDES = {
    "model_report_path",
    "model_path",
    "require_model_file",
    "require_active_meta",
    "require_gate_pass",
    "candidate_validation_mode",
    "source_runtime_config_sha256",
}


def derive_candidate_config(
    runtime_text: str,
    *,
    model_path: str,
    report_path: str,
    source_runtime_config_sha256: str = "",
) -> str:
    source_runtime_config_sha256 = (
        source_runtime_config_sha256
        or hashlib.sha256(runtime_text.encode("utf-8")).hexdigest()
    )
    if re.fullmatch(r"[0-9a-f]{64}", source_runtime_config_sha256) is None:
        raise ValueError("source runtime config SHA-256 非法")
    lines = runtime_text.splitlines()
    output: list[str] = []
    top_section = ""
    in_shadow = False
    found_system_mode = False
    found_integrator = False
    found_shadow = False
    key_pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")

    def append_shadow_contract() -> None:
        output.extend(
            [
                f"    model_report_path: {json.dumps(report_path)}",
                f"    model_path: {json.dumps(model_path)}",
                "    require_model_file: true",
                "    require_active_meta: false",
                "    require_gate_pass: false",
                "    candidate_validation_mode: true",
                "    source_runtime_config_sha256: "
                f"{json.dumps(source_runtime_config_sha256)}",
            ]
        )

    for raw_line in lines:
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        key_match = key_pattern.match(stripped) if stripped else None
        key = key_match.group(1) if key_match is not None else ""

        if in_shadow and stripped and not stripped.startswith("#") and indent <= 2:
            append_shadow_contract()
            in_shadow = False

        if indent == 0 and key:
            top_section = key
            if key == "integrator":
                found_integrator = True

        if top_section == "system" and indent == 2 and key == "mode":
            output.append('  mode: "replay"')
            found_system_mode = True
            continue

        if top_section == "integrator" and indent == 2 and key == "shadow":
            found_shadow = True
            in_shadow = True
            output.append(raw_line)
            continue

        if in_shadow and indent == 4 and key in SHADOW_OVERRIDES:
            continue

        output.append(raw_line)

    if in_shadow:
        append_shadow_contract()
    missing = []
    if not found_system_mode:
        missing.append("system.mode")
    if not found_integrator:
        missing.append("integrator")
    if not found_shadow:
        missing.append("integrator.shadow")
    if missing:
        raise ValueError("runtime config 缺少候选派生字段: " + ",".join(missing))
    return "\n".join(output) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    runtime_path = Path(args.runtime_config)
    output_path = Path(args.output)
    runtime_text = runtime_path.read_text(encoding="utf-8")
    candidate_text = derive_candidate_config(
        runtime_text,
        model_path=args.model,
        report_path=args.report,
        source_runtime_config_sha256=hashlib.sha256(
            runtime_path.read_bytes()
        ).hexdigest(),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(candidate_text, encoding="utf-8")

    runtime_policy = policy_sha256(runtime_path)
    candidate_policy = policy_sha256(output_path)
    if runtime_policy != candidate_policy:
        raise ValueError(
            "candidate replay execution policy differs from runtime: "
            f"runtime={runtime_policy} replay={candidate_policy}"
        )
    print(candidate_policy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
