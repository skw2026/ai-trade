#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DIGEST_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BOOT_PATTERNS = (
    re.compile(r"boot=\{id=([^,}]+),\s*startup_utc=([^}]+)\}"),
    re.compile(r"PROCESS_START:\s*boot_id=([^,\s]+),\s*startup_utc=([^,\s]+)"),
)


def parse_iso_utc(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        body = value[:-1]
        if "." in body:
            head, fraction = body.split(".", 1)
            value = f"{head}.{(fraction + '000000')[:6]}+00:00"
        else:
            value = body + "+00:00"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_container_state(raw: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "image": "",
        "revision": "",
        "image_id": "",
        "started_at": "",
        "status": "",
        "restart_count": None,
    }
    parts = raw.split("|")
    if len(parts) < 6:
        return result
    result.update(
        {
            "image": parts[0].strip(),
            "revision": parts[1].strip(),
            "image_id": parts[2].strip(),
            "started_at": parts[3].strip(),
            "status": parts[4].strip(),
        }
    )
    if result["revision"] == "<no value>":
        result["revision"] = ""
    try:
        result["restart_count"] = int(parts[5].strip())
    except ValueError:
        pass
    return result


def parse_boot_evidence(runtime_log: str) -> tuple[list[str], list[str]]:
    matches: list[tuple[str, str]] = []
    for pattern in BOOT_PATTERNS:
        matches.extend(pattern.findall(runtime_log))
    boot_ids = sorted({boot_id.strip() for boot_id, _ in matches if boot_id.strip()})
    startup_values = sorted(
        {startup.strip() for _, startup in matches if startup.strip()}
    )
    return boot_ids, startup_values


def evaluate_smoke_freshness(
    artifacts_dir: Path,
    *,
    trigger_mode: str,
    expected_sha: str,
    expected_run_id: str,
    max_age_seconds: int,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    trigger_mode = trigger_mode.strip() or "unknown"
    expected_sha = expected_sha.strip()
    expected_run_id = expected_run_id.strip()
    fail_reasons: list[str] = []
    warn_reasons: list[str] = []

    download_status = load_json(artifacts_dir / "smoke_download_status.json")
    release_manifest = load_json(artifacts_dir / "release_manifest.json")
    run_manifest = load_json(artifacts_dir / "run_manifest.json")
    runtime_assess = load_json(artifacts_dir / "runtime_assess.json")
    closed_loop_report = load_json(artifacts_dir / "closed_loop_report.json")
    runtime_log = read_text(artifacts_dir / "runtime.log")
    env_image = read_text(artifacts_dir / "env_image.txt")
    container = parse_container_state(
        read_text(artifacts_dir / "container_state.txt")
    )

    if not download_status or download_status.get("status") != "DONE":
        fail_reasons.append("smoke_artifact_download_incomplete")
    elif download_status.get("run_id") != expected_run_id:
        fail_reasons.append("smoke_artifact_download_run_id_mismatch")
    for name, payload in (
        ("release_manifest", release_manifest),
        ("run_manifest", run_manifest),
        ("runtime_assess", runtime_assess),
        ("closed_loop_report", closed_loop_report),
    ):
        if payload is None:
            fail_reasons.append(f"missing_or_invalid_{name}")
    if not runtime_log:
        fail_reasons.append("missing_runtime_log")

    container_image = str(container["image"])
    container_revision = str(container["revision"])
    container_started_at = str(container["started_at"])
    container_status = str(container["status"])
    container_restart_count = container["restart_count"]
    if not container_image:
        fail_reasons.append("missing_container_image")
    elif not DIGEST_IMAGE_RE.fullmatch(container_image):
        fail_reasons.append("container_image_not_digest_pinned")
    if not container_revision:
        fail_reasons.append("missing_container_image_revision")
    if not SHA256_RE.fullmatch(str(container["image_id"])):
        fail_reasons.append("missing_or_invalid_container_image_id")
    if not container_started_at:
        fail_reasons.append("missing_container_started_at")
    if container_status != "running":
        fail_reasons.append(f"container_status={container_status or '<missing>'}")
    if container_restart_count is None:
        fail_reasons.append("missing_container_restart_count")
    elif container_restart_count > 0:
        warn_reasons.append(f"container_restart_count={container_restart_count}")

    release_sha = ""
    release_runtime_image = ""
    if release_manifest is not None:
        if release_manifest.get("schema_version") != "ai_trade_release_manifest_v1":
            fail_reasons.append("release_manifest_schema_mismatch")
        release_sha = str(release_manifest.get("git_sha") or "").strip()
        images = release_manifest.get("images")
        if isinstance(images, dict):
            release_runtime_image = str(images.get("runtime") or "").strip()
        if not release_sha:
            fail_reasons.append("missing_release_git_sha")
        if not DIGEST_IMAGE_RE.fullmatch(release_runtime_image):
            fail_reasons.append("release_runtime_image_not_digest_pinned")

    if env_image != container_image:
        fail_reasons.append("env_runtime_image_mismatch")
    if release_runtime_image != container_image:
        fail_reasons.append("release_runtime_image_mismatch")
    if release_sha != container_revision:
        fail_reasons.append("container_revision_release_sha_mismatch")

    runtime_verdict = ""
    if runtime_assess is not None:
        runtime_stage = str(runtime_assess.get("stage") or "").upper()
        runtime_verdict = str(runtime_assess.get("verdict") or "").upper()
        if runtime_stage != "SMOKE":
            fail_reasons.append("runtime_assess_stage_mismatch")
        if runtime_verdict not in {"PASS", "PASS_WITH_ACTIONS"}:
            fail_reasons.append(
                f"runtime_assess_verdict={runtime_verdict or '<missing>'}"
            )

    if closed_loop_report is not None:
        if closed_loop_report.get("run_id") != expected_run_id:
            fail_reasons.append("closed_loop_report_run_id_mismatch")
        report_verdict = str(
            closed_loop_report.get("runtime_verdict") or ""
        ).upper()
        if report_verdict != runtime_verdict:
            fail_reasons.append("closed_loop_report_runtime_verdict_mismatch")

    if run_manifest is not None:
        if run_manifest.get("run_id") != expected_run_id:
            fail_reasons.append("run_manifest_run_id_mismatch")
        if str(run_manifest.get("stage") or "").upper() != "SMOKE":
            fail_reasons.append("run_manifest_stage_mismatch")
        manifest_release = run_manifest.get("release")
        manifest_runtime = run_manifest.get("runtime")
        manifest_git = run_manifest.get("git")
        if (
            not isinstance(manifest_git, dict)
            or str(manifest_git.get("commit") or "") != release_sha
        ):
            fail_reasons.append("run_manifest_git_commit_mismatch")
        if not isinstance(manifest_release, dict):
            fail_reasons.append("run_manifest_release_missing")
        else:
            if str(manifest_release.get("git_sha") or "") != release_sha:
                fail_reasons.append("run_manifest_release_sha_mismatch")
            expected_release_dir = f"/opt/ai-trade/releases/{release_sha}"
            if str(manifest_release.get("directory") or "") != expected_release_dir:
                fail_reasons.append("run_manifest_release_directory_mismatch")
            if not HEX_SHA256_RE.fullmatch(
                str(manifest_release.get("runner_sha256") or "")
            ):
                fail_reasons.append("run_manifest_runner_sha256_invalid")
        if not isinstance(manifest_runtime, dict):
            fail_reasons.append("run_manifest_runtime_missing")
        else:
            if str(manifest_runtime.get("image_ref") or "") != container_image:
                fail_reasons.append("run_manifest_runtime_image_mismatch")
            if str(manifest_runtime.get("image_revision") or "") != release_sha:
                fail_reasons.append("run_manifest_runtime_revision_mismatch")
            if str(manifest_runtime.get("image_id") or "") != container["image_id"]:
                fail_reasons.append("run_manifest_runtime_image_id_mismatch")

    boot_ids, startup_values = parse_boot_evidence(runtime_log)
    runtime_startup_utc = startup_values[0] if len(startup_values) == 1 else ""
    if len(boot_ids) != 1:
        fail_reasons.append(f"runtime_boot_id_unique_count={len(boot_ids)}")
    if len(startup_values) != 1:
        fail_reasons.append(
            f"runtime_startup_utc_unique_count={len(startup_values)}"
        )

    container_started_dt = parse_iso_utc(container_started_at)
    runtime_started_dt = parse_iso_utc(runtime_startup_utc)
    container_age_seconds = None
    startup_container_delta_seconds = None
    if container_started_at and container_started_dt is None:
        fail_reasons.append("invalid_container_started_at")
    if runtime_startup_utc and runtime_started_dt is None:
        fail_reasons.append("invalid_runtime_startup_utc")
    if container_started_dt is not None:
        container_age_seconds = max(
            0, int((now_utc - container_started_dt).total_seconds())
        )
    if container_started_dt is not None and runtime_started_dt is not None:
        startup_container_delta_seconds = int(
            abs((runtime_started_dt - container_started_dt).total_seconds())
        )
        if startup_container_delta_seconds > 300:
            fail_reasons.append(
                "runtime_startup_vs_container_started_delta_seconds="
                f"{startup_container_delta_seconds}"
            )

    if trigger_mode == "workflow_run":
        if not expected_sha:
            fail_reasons.append("missing_expected_sha")
        if release_sha != expected_sha:
            fail_reasons.append("release_git_sha_expected_sha_mismatch")
        if not expected_run_id:
            fail_reasons.append("missing_expected_run_id")
        if (
            container_age_seconds is not None
            and container_age_seconds > max_age_seconds
        ):
            fail_reasons.append(
                f"container_age_seconds={container_age_seconds} > {max_age_seconds}"
            )

    status = "FAIL" if fail_reasons else (
        "PASS" if trigger_mode == "workflow_run" else "NOT_EVALUATED_MANUAL"
    )
    return {
        "status": status,
        "identity_mode": "release_manifest+oci_revision+digest",
        "trigger_mode": trigger_mode,
        "expected_sha": expected_sha or None,
        "expected_run_id": expected_run_id or None,
        "release_git_sha": release_sha or None,
        "release_runtime_image": release_runtime_image or None,
        "container_image": container_image or None,
        "container_image_revision": container_revision or None,
        "container_image_id": container["image_id"] or None,
        "env_runtime_image": env_image or None,
        "container_started_at_utc": container_started_at or None,
        "container_status": container_status or None,
        "container_restart_count": container_restart_count,
        "container_age_seconds": container_age_seconds,
        "deploy_freshness_max_age_seconds": max_age_seconds,
        "runtime_boot_id": boot_ids[0] if len(boot_ids) == 1 else None,
        "runtime_boot_id_unique_count": len(boot_ids),
        "runtime_startup_utc": runtime_startup_utc or None,
        "startup_vs_container_started_delta_seconds": startup_container_delta_seconds,
        "runtime_verdict": runtime_verdict or None,
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Smoke artifacts against the deployed immutable release."
    )
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--trigger-mode", required=True)
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=5400)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = evaluate_smoke_freshness(
        args.artifacts_dir,
        trigger_mode=args.trigger_mode,
        expected_sha=args.expected_sha,
        expected_run_id=args.expected_run_id,
        max_age_seconds=args.max_age_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if payload["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
