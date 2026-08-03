#!/usr/bin/env python3
"""Create auditable development, selection, and final-blind research domains."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import pathlib
import statistics
from typing import Any


SCHEMA_VERSION = "research_domain_split_v2"


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_csv(path: pathlib.Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        if "timestamp" not in fields:
            raise ValueError(f"{path} missing timestamp column")
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError(f"{path} has no rows")
    timestamps: list[int] = []
    for index, row in enumerate(rows, start=2):
        try:
            timestamps.append(int(row["timestamp"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{index} invalid timestamp") from exc
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError(f"{path} timestamps must be strictly increasing")
    return fields, rows


def write_csv(
    path: pathlib.Path, fields: list[str], rows: list[dict[str, str]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def infer_interval_ms(rows: list[dict[str, str]]) -> int:
    timestamps = [int(row["timestamp"]) for row in rows]
    deltas = [
        right - left
        for left, right in zip(timestamps, timestamps[1:])
        if right > left
    ]
    if not deltas:
        raise ValueError("cannot infer research bar interval")
    return int(statistics.median(deltas))


def load_consumed_holdout_end(
    ledger_path: pathlib.Path | None,
    *,
    symbol: str,
    interval_ms: int,
) -> tuple[int | None, int]:
    if ledger_path is None:
        return None, 0
    checkpoint_path = ledger_path.with_suffix(
        ledger_path.suffix + ".checkpoint.json"
    )
    if not ledger_path.is_file():
        if checkpoint_path.exists():
            raise ValueError(
                "holdout ledger missing but checkpoint exists; deletion detected"
            )
        return None, 0

    entries: list[dict[str, Any]] = []
    previous_sha256 = "0" * 64
    for line_number, raw_line in enumerate(
        ledger_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{ledger_path}:{line_number} invalid holdout ledger JSON"
            ) from exc
        if not isinstance(entry, dict):
            raise ValueError(
                f"{ledger_path}:{line_number} holdout ledger entry is not object"
            )
        reported_sha256 = str(entry.get("entry_sha256") or "").strip()
        hash_payload = dict(entry)
        hash_payload.pop("entry_sha256", None)
        computed_sha256 = hashlib.sha256(
            json.dumps(
                hash_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            entry.get("schema_version") != "final_holdout_consumption_v2"
            or entry.get("previous_entry_sha256") != previous_sha256
            or reported_sha256 != computed_sha256
        ):
            raise ValueError(
                f"{ledger_path}:{line_number} holdout ledger hash chain invalid"
            )
        entries.append(entry)
        previous_sha256 = reported_sha256

    if entries:
        if not checkpoint_path.is_file():
            raise ValueError(
                "holdout ledger checkpoint missing; refuse unverified history"
            )
        try:
            checkpoint = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError as exc:
            raise ValueError("holdout ledger checkpoint is invalid JSON") from exc
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("schema_version")
            != "final_holdout_checkpoint_v1"
            or int(checkpoint.get("entry_count") or 0) != len(entries)
            or checkpoint.get("tail_entry_sha256")
            != entries[-1].get("entry_sha256")
        ):
            raise ValueError(
                "holdout ledger checkpoint mismatch; deletion/truncation detected"
            )
    elif checkpoint_path.exists():
        raise ValueError(
            "holdout ledger missing but checkpoint exists; deletion detected"
        )

    latest_end: int | None = None
    matching_entries = 0
    for entry in entries:
        if (
            str(entry.get("symbol") or "").strip().upper()
            != symbol.strip().upper()
            or int(entry.get("bar_interval_ms") or 0) != interval_ms
        ):
            continue
        end_ts = int(entry.get("holdout_end_ts_ms") or 0)
        if end_ts <= 0:
            raise ValueError(
                f"{ledger_path} invalid holdout_end_ts_ms"
            )
        matching_entries += 1
        latest_end = end_ts if latest_end is None else max(latest_end, end_ts)
    return latest_end, matching_entries


def split_domains(
    *,
    raw_csv: pathlib.Path,
    feature_csv: pathlib.Path,
    development_csv: pathlib.Path,
    selection_feature_csv: pathlib.Path,
    holdout_feature_csv: pathlib.Path,
    report_path: pathlib.Path,
    selection_bars: int,
    holdout_bars: int,
    embargo_bars: int,
    min_development_bars: int,
    min_selection_feature_bars: int,
    min_holdout_feature_bars: int,
    symbol: str = "",
    holdout_ledger_path: pathlib.Path | None = None,
    development_feature_csv: pathlib.Path | None = None,
) -> dict[str, Any]:
    if selection_bars <= 0 or holdout_bars <= 0 or embargo_bars < 0:
        raise ValueError(
            "selection_bars and holdout_bars must be positive; "
            "embargo_bars must be non-negative"
        )
    raw_fields, raw_rows = load_csv(raw_csv)
    feature_fields, feature_rows = load_csv(feature_csv)
    effective_symbol = str(symbol).strip().upper()
    if not effective_symbol and "symbol" in raw_fields:
        effective_symbol = str(raw_rows[-1].get("symbol") or "").strip().upper()
    if not effective_symbol:
        raise ValueError("research domain split requires symbol identity")
    interval_ms = infer_interval_ms(raw_rows)
    consumed_holdout_end_ts, consumed_holdout_count = (
        load_consumed_holdout_end(
            holdout_ledger_path,
            symbol=effective_symbol,
            interval_ms=interval_ms,
        )
    )
    required = (
        min_development_bars
        + selection_bars
        + holdout_bars
        + 2 * embargo_bars
    )
    if len(raw_rows) < required:
        raise ValueError(
            f"raw rows={len(raw_rows)} below required={required} "
            "(development + embargo + selection + embargo + holdout)"
        )

    holdout_start_index = len(raw_rows) - holdout_bars
    selection_end_index = holdout_start_index - embargo_bars
    selection_start_index = selection_end_index - selection_bars
    development_end_index = selection_start_index - embargo_bars
    development_rows = raw_rows[:development_end_index]
    selection_embargo_rows = raw_rows[
        development_end_index:selection_start_index
    ]
    selection_raw_rows = raw_rows[selection_start_index:selection_end_index]
    holdout_embargo_rows = raw_rows[selection_end_index:holdout_start_index]
    holdout_raw_rows = raw_rows[holdout_start_index:]
    development_end_ts = int(development_rows[-1]["timestamp"])
    selection_start_ts = int(selection_raw_rows[0]["timestamp"])
    selection_end_ts = int(selection_raw_rows[-1]["timestamp"])
    holdout_start_ts = int(holdout_raw_rows[0]["timestamp"])
    holdout_end_ts = int(holdout_raw_rows[-1]["timestamp"])
    if (
        consumed_holdout_end_ts is not None
        and selection_start_ts <= consumed_holdout_end_ts
    ):
        fresh_bars = sum(
            1
            for row in raw_rows
            if int(row["timestamp"]) > consumed_holdout_end_ts
        )
        raise ValueError(
            "selection/final evidence overlaps consumed final: "
            f"symbol={effective_symbol}, interval_ms={interval_ms}, "
            f"selection_start={selection_start_ts}, "
            f"holdout_start={holdout_start_ts}, "
            f"last_consumed_end={consumed_holdout_end_ts}, "
            f"fresh_bars={fresh_bars}, required_fresh_bars="
            f"{selection_bars + embargo_bars + holdout_bars}"
        )
    selection_feature_rows = [
        row
        for row in feature_rows
        if selection_start_ts <= int(row["timestamp"]) <= selection_end_ts
    ]
    holdout_feature_rows = [
        row
        for row in feature_rows
        if holdout_start_ts <= int(row["timestamp"]) <= holdout_end_ts
    ]
    development_feature_rows = [
        row
        for row in feature_rows
        if int(row["timestamp"]) <= development_end_ts
    ]
    if len(development_rows) < min_development_bars:
        raise ValueError(
            f"development rows={len(development_rows)} below "
            f"minimum={min_development_bars}"
        )
    if len(selection_feature_rows) < min_selection_feature_bars:
        raise ValueError(
            f"selection feature rows={len(selection_feature_rows)} below "
            f"minimum={min_selection_feature_bars}"
        )
    if len(holdout_feature_rows) < min_holdout_feature_bars:
        raise ValueError(
            f"holdout feature rows={len(holdout_feature_rows)} below "
            f"minimum={min_holdout_feature_bars}"
        )

    selection_embargo_start_ts = (
        int(selection_embargo_rows[0]["timestamp"])
        if selection_embargo_rows
        else None
    )
    selection_embargo_end_ts = (
        int(selection_embargo_rows[-1]["timestamp"])
        if selection_embargo_rows
        else None
    )
    holdout_embargo_start_ts = (
        int(holdout_embargo_rows[0]["timestamp"])
        if holdout_embargo_rows
        else None
    )
    holdout_embargo_end_ts = (
        int(holdout_embargo_rows[-1]["timestamp"])
        if holdout_embargo_rows
        else None
    )
    if not (
        development_end_ts < selection_start_ts
        and selection_end_ts < holdout_start_ts
    ):
        raise ValueError("research domains overlap")

    write_csv(development_csv, raw_fields, development_rows)
    if development_feature_csv is not None:
        if not development_feature_rows:
            raise ValueError("development feature domain has no rows")
        write_csv(
            development_feature_csv,
            feature_fields,
            development_feature_rows,
        )
    write_csv(selection_feature_csv, feature_fields, selection_feature_rows)
    write_csv(holdout_feature_csv, feature_fields, holdout_feature_rows)
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "status": "PASS",
        "contract": {
            "factor_selection_domain": "development",
            "model_fit_domain": "development",
            "candidate_selection_domain": "selection_validation",
            "diagnostic_tuning_domain": "selection_validation",
            "economic_validation_domain": "untouched_final_holdout",
            "embargo_required": True,
            "domains_overlap": False,
            "holdout_must_not_influence_candidate_selection": True,
            "holdout_consumption_ledger_required": True,
            "final_holdout_disjoint_from_prior_experiments": True,
            "selection_disjoint_from_prior_final_experiments": True,
            "prior_final_reuse_policy": (
                "historical_training_only_never_selection_or_final"
            ),
        },
        "parameters": {
            "symbol": effective_symbol,
            "bar_interval_ms": interval_ms,
            "selection_bars": selection_bars,
            "holdout_bars": holdout_bars,
            "embargo_bars": embargo_bars,
            "min_development_bars": min_development_bars,
            "min_selection_feature_bars": min_selection_feature_bars,
            "min_holdout_feature_bars": min_holdout_feature_bars,
        },
        "holdout_consumption": {
            "ledger_path": (
                str(holdout_ledger_path) if holdout_ledger_path else ""
            ),
            "prior_matching_entry_count": consumed_holdout_count,
            "last_consumed_holdout_end_ts_ms": consumed_holdout_end_ts,
            "current_holdout_is_fresh": (
                consumed_holdout_end_ts is None
                or selection_start_ts > consumed_holdout_end_ts
            ),
        },
        "boundaries": {
            "development_start_ts_ms": int(development_rows[0]["timestamp"]),
            "development_end_ts_ms": development_end_ts,
            "selection_embargo_start_ts_ms": selection_embargo_start_ts,
            "selection_embargo_end_ts_ms": selection_embargo_end_ts,
            "selection_start_ts_ms": selection_start_ts,
            "selection_end_ts_ms": selection_end_ts,
            "holdout_embargo_start_ts_ms": holdout_embargo_start_ts,
            "holdout_embargo_end_ts_ms": holdout_embargo_end_ts,
            "holdout_start_ts_ms": holdout_start_ts,
            "holdout_end_ts_ms": holdout_end_ts,
        },
        "rows": {
            "raw_total": len(raw_rows),
            "feature_total": len(feature_rows),
            "development": len(development_rows),
            "development_feature": len(development_feature_rows),
            "selection_embargo": len(selection_embargo_rows),
            "selection_raw": len(selection_raw_rows),
            "selection_feature": len(selection_feature_rows),
            "holdout_embargo": len(holdout_embargo_rows),
            "holdout_raw": len(holdout_raw_rows),
            "holdout_feature": len(holdout_feature_rows),
        },
        "artifacts": {
            "raw_source": {
                "path": str(raw_csv),
                "sha256": file_sha256(raw_csv),
            },
            "feature_source": {
                "path": str(feature_csv),
                "sha256": file_sha256(feature_csv),
            },
            "development_csv": {
                "path": str(development_csv),
                "sha256": file_sha256(development_csv),
            },
            "development_feature_csv": (
                {
                    "path": str(development_feature_csv),
                    "sha256": file_sha256(development_feature_csv),
                }
                if development_feature_csv is not None
                else None
            ),
            "selection_feature_csv": {
                "path": str(selection_feature_csv),
                "sha256": file_sha256(selection_feature_csv),
            },
            "holdout_feature_csv": {
                "path": str(holdout_feature_csv),
                "sha256": file_sha256(holdout_feature_csv),
            },
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Split research data into development, selection-validation, "
            "and untouched final holdout domains"
        )
    )
    parser.add_argument("--raw-csv", required=True)
    parser.add_argument("--feature-csv", required=True)
    parser.add_argument("--development-csv", required=True)
    parser.add_argument("--development-feature-csv", default="")
    parser.add_argument("--selection-feature-csv", required=True)
    parser.add_argument("--holdout-feature-csv", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--selection-bars", type=int, default=8640)
    parser.add_argument("--holdout-bars", type=int, default=8640)
    parser.add_argument("--embargo-bars", type=int, default=288)
    parser.add_argument("--min-development-bars", type=int, default=20000)
    parser.add_argument("--min-selection-feature-bars", type=int, default=4000)
    parser.add_argument("--min-holdout-feature-bars", type=int, default=4000)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--holdout-ledger", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = split_domains(
        raw_csv=pathlib.Path(args.raw_csv),
        feature_csv=pathlib.Path(args.feature_csv),
        development_csv=pathlib.Path(args.development_csv),
        selection_feature_csv=pathlib.Path(args.selection_feature_csv),
        holdout_feature_csv=pathlib.Path(args.holdout_feature_csv),
        report_path=pathlib.Path(args.report),
        selection_bars=args.selection_bars,
        holdout_bars=args.holdout_bars,
        embargo_bars=args.embargo_bars,
        min_development_bars=args.min_development_bars,
        min_selection_feature_bars=args.min_selection_feature_bars,
        min_holdout_feature_bars=args.min_holdout_feature_bars,
        symbol=args.symbol,
        holdout_ledger_path=(
            pathlib.Path(args.holdout_ledger)
            if args.holdout_ledger
            else None
        ),
        development_feature_csv=(
            pathlib.Path(args.development_feature_csv)
            if args.development_feature_csv
            else None
        ),
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
