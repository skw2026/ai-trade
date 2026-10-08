#!/usr/bin/env python3
"""Read-only audit of fixed ValidatorQueue snapshots; no signals or market returns.

The pre-Pectra factor is a provider conversion hypothesis, NOT proof of actual
withdrawals, liquid balances, historical publication time or strategy admission.
"""
import argparse
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path

QUEUE_FIELDS = ("entry_queue", "exit_queue")
PECTRA = date(2025, 5, 7)
KNOWN_BAD_END = date(2025, 5, 21)


def validate_rows(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("expected a nonempty list of daily rows")
    previous = None
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise ValueError("daily row needs an ISO date")
        day = date.fromisoformat(row["date"])
        if day.isoformat() != row["date"] or (previous and day <= previous):
            raise ValueError("dates must be canonical, unique and strictly increasing")
        for field in QUEUE_FIELDS:
            value = row.get(field)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < 0):
                raise ValueError("queue values must be finite nonnegative numbers")
        previous = day


def summarize(rows):
    validate_rows(rows)
    dates = [date.fromisoformat(row["date"]) for row in rows]
    missing = []
    for first, last in zip(dates, dates[1:]):
        missing.extend((first + timedelta(days=i)).isoformat()
                       for i in range(1, (last-first).days))
    return {
        "rows": len(rows), "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(), "missing_dates": missing,
        "known_inaccurate_post_pectra_dates": [
            day.isoformat() for day in dates if PECTRA <= day <= KNOWN_BAD_END],
        "fields": sorted(set().union(*(row.keys() for row in rows))),
    }


def compare_pre_pectra(current, past):
    validate_rows(current)
    validate_rows(past)
    if any(date.fromisoformat(row["date"]) >= PECTRA for row in past):
        raise ValueError("factor 32 comparison is restricted to pre-Pectra snapshots")
    index = {row["date"]: row for row in current}
    raw_changes = {field: 0 for field in QUEUE_FIELDS}
    residuals = []
    missing = []
    for old in past:
        new = index.get(old["date"])
        if new is None:
            missing.append(old["date"])
            continue
        for field in sorted(set(old) | set(new)):
            if field in QUEUE_FIELDS:
                raw_changes[field] += old[field] != new[field]
                if new[field] != old[field] * 32:
                    residuals.append({"date": old["date"], "field": field})
            elif field not in old or field not in new or old[field] != new[field]:
                residuals.append({"date": old["date"], "field": field})
    return {"snapshot_rows": len(past), "queue_raw_change_counts": raw_changes,
            "missing_in_current": missing, "residual_differences_after_factor_32": residuals,
            "compared_factor": 32, "raw_data_modified": False}


def load_snapshot(path):
    raw = Path(path).read_bytes()
    if len(raw) > 3_000_000:
        raise ValueError("snapshot exceeds the 3 MB diagnostic limit")
    rows = json.loads(raw)
    validate_rows(rows)
    return rows, {"file": Path(path).name, "bytes": len(raw),
                  "sha256": hashlib.sha256(raw).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True)
    parser.add_argument("--pre-pectra-snapshot", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        current, identity = load_snapshot(args.current)
        comparisons = []
        for path in args.pre_pectra_snapshot:
            past, past_identity = load_snapshot(path)
            comparisons.append({"identity": past_identity, "summary": summarize(past),
                                "comparison": compare_pre_pectra(current, past)})
        report = {"status": "DIAGNOSTIC_ONLY", "current_identity": identity,
                  "current_summary": summarize(current), "snapshots": comparisons,
                  "first_publication_verified": False,
                  "independent_confirmation_admitted": False,
                  "actual_selling_verified": False, "economic_attempts": 0}
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, "Input audit failed: %s\n" % exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
