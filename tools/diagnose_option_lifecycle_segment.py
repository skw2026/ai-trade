#!/usr/bin/env python3
"""Read one pinned V4 segment; emit timing/checksum diagnostics, never acceptance.

No network, account access, archive replay, input repair, or filesystem writes.
The caller must also enforce a process time/memory limit. CLI paths are fixed;
only a timestamp-shaped report name, its digest and a deployed SHA are accepted.
"""

import argparse
import hashlib
import io
import json
import lzma
from pathlib import Path
import re


SCHEMA = "option_lifecycle_segment_diagnostic_v1"
ROOT = Path("/opt/ai-trade/data/research/bybit_btc_option_lifecycle_v4")
CURRENT = Path("/opt/ai-trade/current")
POLICY_SHA = "3057e78a46208d72ec4990ea9604744f0ed211e90b3c5f407a1132ee0927d253"
MAX_FILE = 16 * 1024 * 1024
MAX_LINE = 1024 * 1024
MAX_DECODED = 64 * 1024 * 1024


class DiagnosticError(Exception):
    """Only fixed codes, never external exception messages or file contents."""


def require(condition, code):
    if not condition:
        raise DiagnosticError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_bounded(path, maximum):
    require(path.is_file() and not path.is_symlink(), "FILE_MISSING_OR_UNSAFE")
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    require(len(data) <= maximum, "FILE_BYTE_LIMIT")
    return data


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "DUPLICATE_JSON_FIELD")
        result[key] = value
    return result


def decode(data):
    value = json.loads(data, object_pairs_hook=unique)
    require(isinstance(value, dict), "JSON_OBJECT_REQUIRED")
    return value


def diagnose(root, release, report_name, report_sha256, expected_release_sha):
    require(re.fullmatch(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\.json", report_name),
            "REPORT_NAME_INVALID")
    require(re.fullmatch(r"[0-9a-f]{64}", report_sha256), "REPORT_SHA_INVALID")
    require(re.fullmatch(r"[0-9a-f]{40}", expected_release_sha), "RELEASE_SHA_INVALID")
    release = release.resolve()
    require(release.name == expected_release_sha, "DEPLOYED_RELEASE_MISMATCH")
    root = root.resolve()
    policy = decode(read_bounded(release / "config/option_lifecycle_capture_v4.json", MAX_LINE))
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    require(digest(canonical) == POLICY_SHA, "FROZEN_POLICY_MISMATCH")
    limit = policy["timestamp_contract"]["maximum_poll_latency_seconds"] * 1000
    require(limit == 120000, "FROZEN_TIME_LIMIT_MISMATCH")
    report_path = root / "reports/BTC" / report_name
    require(report_path.resolve() == report_path, "REPORT_PATH_UNSAFE")
    report_bytes = read_bounded(report_path, MAX_LINE)
    require(digest(report_bytes) == report_sha256, "PINNED_REPORT_CHANGED")
    report = decode(report_bytes)
    require(report.get("schema_version") == "bybit_btc_option_lifecycle_capture_v4",
            "REPORT_SCHEMA_MISMATCH")
    require(report.get("policy_canonical_sha256") == POLICY_SHA, "REPORT_POLICY_MISMATCH")
    stem = report_name[:-5]
    artifact_bytes = {}
    hashes = {}
    for kind, suffix in (("raw", ".jsonl.xz"), ("features", ".csv")):
        relative = f"{kind}/BTC/{stem}{suffix}"
        require(report[kind].get("path") == relative, "ARTIFACT_PATH_MISMATCH")
        path = root / relative
        require(path.resolve() == path, "ARTIFACT_PATH_UNSAFE")
        data = read_bounded(path, MAX_FILE)
        hashes[kind] = digest(data)
        require(hashes[kind] == report[kind].get("sha256"), "ARTIFACT_CHECKSUM_MISMATCH")
        artifact_bytes[kind] = data
    rows = decoded_bytes = violations = 0
    observed_max = 0
    examples = []
    with lzma.open(io.BytesIO(artifact_bytes["raw"]), "rb") as handle:
        while True:
            line = handle.readline(MAX_LINE + 1)
            if not line:
                break
            decoded_bytes += len(line)
            rows += 1
            require(len(line) <= MAX_LINE and decoded_bytes <= MAX_DECODED and rows <= 10000,
                    "DECOMPRESSION_BUDGET_EXCEEDED")
            snapshot = decode(line)
            start = snapshot.get("poll_started_epoch_ms")
            end = snapshot.get("snapshot_completed_epoch_ms")
            require(type(start) is int and type(end) is int and start > 0 and end >= start,
                    "SNAPSHOT_TIMESTAMPS_INVALID")
            require(snapshot.get("timestamp_epoch_ms") == start, "SNAPSHOT_TIMESTAMP_MISMATCH")
            latency = end - start
            observed_max = max(observed_max, latency)
            if latency > limit:
                violations += 1
                if len(examples) < 10:
                    examples.append({"line": rows, "poll_started_epoch_ms": start,
                                     "snapshot_completed_epoch_ms": end, "poll_latency_ms": latency})
    require(rows > 0, "RAW_SNAPSHOTS_EMPTY")
    counts_match = all(type(value) is int and value == rows for value in (
        report["coverage"].get("successful_poll_count"), report["raw"].get("snapshot_count"),
        report["features"].get("row_count")))
    return {
        "schema_version": SCHEMA, "scope": "SINGLE_SEGMENT_DIAGNOSIS_NOT_ARCHIVE_ACCEPTANCE",
        "executed_release_sha": expected_release_sha,
        "report": report_name, "report_sha256": report_sha256,
        "artifact_sha256": hashes, "artifact_checksums_match": True,
        "capture_status_pass": report.get("status") == "PASS", "snapshot_count": rows,
        "report_counts_match_raw": counts_match,
        "metadata_max_latency_matches_raw": report["quality"].get("maximum_poll_latency_ms") == observed_max,
        "maximum_poll_latency_ms": observed_max, "limit_ms": limit,
        "over_limit_snapshot_count": violations, "over_limit_examples": examples,
        "timing_diagnosis": "POLL_LATENCY_EXCEEDS_FROZEN_CONTRACT" if violations else "NO_TIMING_VIOLATION_FOUND",
        "other_snapshot_contracts_checked": False, "archive_acceptance_evaluated": False,
        "grants_trading_authority": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--expected-release-sha", required=True)
    args = parser.parse_args()
    try:
        result = diagnose(ROOT, CURRENT, args.report, args.report_sha256, args.expected_release_sha)
    except (DiagnosticError, OSError, ValueError, TypeError, KeyError, AttributeError,
            lzma.LZMAError, EOFError, MemoryError) as exc:
        code = str(exc) if isinstance(exc, DiagnosticError) else type(exc).__name__.upper()
        print(json.dumps({"schema_version": SCHEMA, "diagnosis_completed": False,
                          "reason_code": code, "archive_acceptance_evaluated": False}))
        return 2
    # Zero means this diagnostic completed, even when timing is disqualified.
    result["diagnosis_completed"] = True
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
