#!/usr/bin/env python3
"""Read retained research metadata only; absence is NEVER non-use proof.

No network, raw prices, account files, environment files or filesystem writes.
The CLI pins the existing release and data root. Enforce process limits as well.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

SCHEMA = "research_input_usage_inventory_v1"
NAMES = frozenset(("market_alpha_history_report.json", "research_domain_split_report.json",
                   "final_holdout_consumption.jsonl", "experiment_budget_ledger.jsonl"))
SYMBOLS = frozenset(("BTCUSDT", "ETHUSDT", "SOLUSDT", "ARBUSDT"))
MAX_FILE, MAX_TOTAL = 4 * 1024 * 1024, 64 * 1024 * 1024
MAX_FILES, MAX_ENTRIES, MAX_SECONDS = 3000, 100000, 30
TARGET_START, TARGET_END = 1684627200000, 1746489599999


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_FIELD")
        result[key] = value
    return result


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_relative(root_fd, parts, maximum=MAX_FILE):
    """No-follow descriptors for every path component, not just the leaf."""
    if not parts or any(p in ("", ".", "..") or "/" in p for p in parts):
        raise ValueError("UNSAFE_PATH")
    parent = os.dup(root_fd)
    try:
        for name in parts[:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise ValueError("FILE_TYPE_OR_SIZE")
            raw = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            if (len(raw) > maximum or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise ValueError("FILE_CHANGED_OR_SIZE")
            return raw
    finally:
        os.close(parent)


def interval(symbol, first, last):
    if (not isinstance(symbol, str) or symbol not in SYMBOLS
            or type(first) is not int or type(last) is not int
            or not 1420070400000 <= first <= last < 1830297600000):
        raise ValueError("UNKNOWN_SYMBOL_OR_TIME_AXIS")
    return {"symbol": symbol, "first_timestamp_ms": first, "last_timestamp_ms": last,
            "target_eth_overlap": symbol == "ETHUSDT" and first <= TARGET_END and last >= TARGET_START}


def extract(record):
    """Allowlist symbols/time axes; never echo arbitrary nested values."""
    if not isinstance(record, dict):
        raise ValueError("METADATA_OBJECT_REQUIRED")
    kind = record.get("schema_version")
    if kind == "market_alpha_history_v1":
        quality, symbols = record["quality"], record["symbols"]
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 4:
            raise ValueError("SYMBOL_LIST_INVALID")
        return [interval(s, quality["first_timestamp"], quality["last_timestamp"]) for s in symbols]
    if kind == "research_domain_split_v2":
        bounds = record["boundaries"]
        return [interval(record["parameters"]["symbol"], bounds["development_start_ts_ms"],
                         bounds["holdout_end_ts_ms"])]
    if kind == "final_holdout_consumption_v2":
        return [interval(record["symbol"], record["holdout_start_ts_ms"], record["holdout_end_ts_ms"])]
    # Hash-only experiment ledgers aren't silently treated as unused history.
    raise ValueError("UNRECOGNIZED_METADATA")


def inventory(root):
    root = Path(root)
    root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    started = time.monotonic()
    result = {"schema_version": SCHEMA, "diagnosis_completed": False,
              "coverage": "RETAINED_ALLOWLISTED_METADATA_ONLY",
              "independent_confirmation_admitted": False, "non_use_proven": False,
              "account_access": False, "filesystem_writes": False,
              "visited_entries": 0, "bytes_read": 0, "files": [], "errors": [],
              "missing_scope_directories": [], "scope_ledgers_present": [],
              "target_eth_exposure_found": False}
    def fail(code):
        result["errors"].append(code)
    def check_limits():
        if (result["visited_entries"] > MAX_ENTRIES or len(result["files"]) >= MAX_FILES
                or result["bytes_read"] >= MAX_TOTAL or time.monotonic()-started > MAX_SECONDS):
            raise ValueError("INVENTORY_LIMIT")
    def inspect(path):
        parts = path.relative_to(root).parts
        row = {"basename": path.name, "relative_path_sha256": digest("/".join(parts).encode())}
        try:
            raw = read_relative(root_fd, parts, min(MAX_FILE, MAX_TOTAL-result["bytes_read"]))
            result["bytes_read"] += len(raw)
            row.update(sha256=digest(raw), bytes=len(raw), intervals=[], unparsed_records=0)
            if path.name.endswith(".jsonl"):
                result["scope_ledgers_present"].append(path.name)
                records = [json.loads(line, object_pairs_hook=unique)
                           for line in raw.splitlines() if line.strip()]
            else:
                records = [json.loads(raw, object_pairs_hook=unique)]
            for record in records:
                try:
                    row["intervals"].extend(extract(record))
                except (ValueError, TypeError, KeyError):
                    row["unparsed_records"] += 1
            row["record_count"] = len(records)
            row["status"] = "PARTIAL" if row["unparsed_records"] else "METADATA_READ"
            if any(i["target_eth_overlap"] for i in row["intervals"]):
                result["target_eth_exposure_found"] = True
        except (OSError, ValueError, TypeError, KeyError):
            row["status"] = "UNREADABLE_OR_UNSAFE"
            fail("ALLOWLISTED_FILE_UNREADABLE_OR_UNSAFE")
        result["files"].append(row)
    try:
        for relative in ("research", "reports/closed_loop", "reports/replay_validation"):
            scope = root / relative
            if not scope.is_dir() or scope.is_symlink():
                result["missing_scope_directories"].append(relative)
                continue
            for directory, dirs, files in os.walk(scope, followlinks=False,
                                                   onerror=lambda e: fail("DIRECTORY_READ_ERROR")):
                result["visited_entries"] += len(dirs) + len(files)
                check_limits()
                dirs[:] = sorted(name for name in dirs if not (Path(directory)/name).is_symlink())
                for name in sorted(set(files) & NAMES):
                    check_limits()
                    inspect(Path(directory)/name)
        ledger = root/"models/final_holdout_consumption.jsonl"
        if ledger.exists() or ledger.is_symlink():
            check_limits()
            inspect(ledger)
        result["diagnosis_completed"] = not result["errors"]
    except (OSError, ValueError):
        fail("INVENTORY_ABORTED_OR_LIMIT")
    finally:
        os.close(root_fd)
    result["scope_ledgers_present"] = sorted(set(result["scope_ledgers_present"]))
    result["elapsed_seconds"] = round(time.monotonic()-started, 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-release-sha", required=True)
    args = parser.parse_args(argv)
    try:
        sha = args.expected_release_sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("RELEASE_SHA_INVALID")
        release = Path("/opt/ai-trade/current").resolve(strict=True)
        if release != Path("/opt/ai-trade/releases")/sha:
            raise ValueError("RELEASE_IDENTITY_MISMATCH")
        fd = os.open(str(release), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            manifest = json.loads(read_relative(fd, ("release_manifest.json",), 65536), object_pairs_hook=unique)
        finally:
            os.close(fd)
        if manifest.get("git_sha") != sha:
            raise ValueError("RELEASE_MANIFEST_MISMATCH")
        report = inventory(Path("/opt/ai-trade/data"))
        report["existing_release_sha"] = sha
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 0 if report["diagnosis_completed"] else 2
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"schema_version": SCHEMA, "diagnosis_completed": False,
                          "reason_code": "IDENTITY_OR_INVENTORY_ERROR",
                          "independent_confirmation_admitted": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
